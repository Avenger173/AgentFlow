"""知识库 K2 的受控混合检索服务。

本模块只负责从当前活动 generation 取回有限、可追溯的证据包，不生成面向客户的回答，
也不把原文写进任务审计。关键词 FTS 始终可用；只有 generation 已声明语义索引完成、
且本机模型仍可离线加载时，才额外合并 Chroma 候选。这样 K3 可以把本模块作为 Evidence
Gate 的唯一上游，而不会把索引构建中或跨资料库的内容送入模型。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import sqlite3
from threading import RLock
from time import monotonic
from typing import Iterable

from app.database.sqlite import get_connection
from app.database.knowledge_repository import KnowledgeBaseNotFoundError, KnowledgeBaseUnavailableError
from app.schemas.knowledge import (
    KnowledgeRetrievalDiagnostics,
    KnowledgeRetrievalCacheIdentity,
    KnowledgeRetrievalEvidence,
    KnowledgeRetrievalRequest,
    KnowledgeRetrievalResult,
    KnowledgeSourceAnchor,
)
from app.services.knowledge_vector_index import ChromaGenerationIndex, embed_local_texts
from app.services.knowledge_performance import record_knowledge_retrieval_elapsed_ms


RETRIEVAL_PROFILE_VERSION = "hybrid_rrf_v2"
KEYWORD_CANDIDATE_LIMIT = 20
DENSE_CANDIDATE_LIMIT = 20
RRF_K = 60
# FastEmbed 当前 BGE profile 输出单位向量，Chroma 默认 L2 距离可稳定解释为“越小越相关”。
# K3 固定题集实测：必过题的最远首命中约 0.924，无答案题最近候选约 0.969；0.95 留出
# 小幅泛化余量，但不让“总能找到一个最相近段落”的 Dense 特性击穿 Evidence Gate。该值不是
# 业务事实，后续仅能通过扩大标注集并比较 Recall/拒答率后再调整。
DENSE_MAX_L2_DISTANCE = 0.95
# K5.1 只缓存本进程内已计算的检索证据。容量和 TTL 都刻意保守：它改善同一资料库的重复
# 提问/重试，却不会被当成跨会话记忆或替代 SQLite 的事实来源。
RETRIEVAL_CACHE_MAX_ENTRIES = 48
RETRIEVAL_CACHE_TTL_SECONDS = 180.0

_ASCII_OR_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}")
_CHINESE_SEQUENCE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


class KnowledgeRetrievalUnavailableError(ValueError):
    """资料库没有可安全查询的活动 generation，不能用空结果掩盖状态问题。"""


@dataclass(frozen=True)
class _ActiveGeneration:
    """检索路由需要的最小快照，不保存正文、物理路径或模型运行细节。"""

    knowledge_base_id: str
    index_generation_id: str
    generation_number: int
    vector_index_mode: str


@dataclass
class _RankedCandidate:
    """RRF 合并前后的候选状态；正文只在最终 SQLite 回读时取得。"""

    child_chunk_id: str
    score: float = 0.0
    channels: set[str] | None = None

    def add(self, *, rank: int, channel: str) -> None:
        if self.channels is None:
            self.channels = set()
        self.channels.add(channel)
        self.score += 1.0 / (RRF_K + rank)


@dataclass(frozen=True)
class _CachedRetrieval:
    """缓存中的结果只在当前 Python 进程内存活，不写入 SQLite 或任务历史。"""

    stored_at: float
    result: KnowledgeRetrievalResult


_retrieval_cache: OrderedDict[tuple[str, int, str, str, int], _CachedRetrieval] = OrderedDict()
_retrieval_cache_lock = RLock()


def clear_knowledge_retrieval_cache() -> None:
    """清空进程内短缓存，供受控维护与离线回归使用。"""

    with _retrieval_cache_lock:
        _retrieval_cache.clear()


def retrieve_knowledge_evidence(request: KnowledgeRetrievalRequest) -> KnowledgeRetrievalResult:
    """从一个资料库当前 generation 检索有限父块证据，显式报告每次降级原因。"""

    started_at = monotonic()
    try:
        active = _load_active_generation(request.knowledge_base_id)
        cache_identity = _build_cache_identity(active=active, request=request)
        cached = _load_cached_retrieval(cache_identity)
        if cached is not None:
            cached_result, age_ms = cached
            return _with_cache_diagnostics(cached_result, state="hit", age_ms=age_ms)

        result = _retrieve_for_active_generation(active=active, request=request)
        # 索引 generation 是证据有效性的硬边界。若资料库恰好在本次检索中切换，不能把旧
        # generation 的结果塞入新 generation 的缓存；重新读取一次以取得当前快照。
        current_active = _load_active_generation(request.knowledge_base_id)
        if current_active.index_generation_id != active.index_generation_id:
            return _retrieve_after_generation_change(request=request, previous_generation_id=active.index_generation_id)

        if _cacheable_result(result):
            _store_cached_retrieval(cache_identity, result)
        return _with_cache_diagnostics(result, state="miss", age_ms=None)
    finally:
        # K5.8 只记录本次本地路径的真实耗时，不保存问题、资料库 ID、证据、缓存键或错误正文。
        record_knowledge_retrieval_elapsed_ms(round((monotonic() - started_at) * 1_000))


def _retrieve_after_generation_change(
    *,
    request: KnowledgeRetrievalRequest,
    previous_generation_id: str,
) -> KnowledgeRetrievalResult:
    """generation 在读取期间切换时只重试一次，避免把并发重建伪装为旧来源命中。"""

    active = _load_active_generation(request.knowledge_base_id)
    result = _retrieve_for_active_generation(active=active, request=request)
    latest = _load_active_generation(request.knowledge_base_id)
    if latest.index_generation_id != active.index_generation_id:
        raise KnowledgeRetrievalUnavailableError("资料库索引正在更新，请稍后重新提问。")
    if active.index_generation_id == previous_generation_id:
        raise KnowledgeRetrievalUnavailableError("资料库索引状态尚未稳定，请稍后重新提问。")
    cache_identity = _build_cache_identity(active=active, request=request)
    if _cacheable_result(result):
        _store_cached_retrieval(cache_identity, result)
    return _with_cache_diagnostics(result, state="miss", age_ms=None)


def _retrieve_for_active_generation(
    *,
    active: _ActiveGeneration,
    request: KnowledgeRetrievalRequest,
) -> KnowledgeRetrievalResult:
    """执行未缓存的关键词/Dense 候选和父块回读，保持 K2 原有检索语义。"""

    warnings: list[str] = []
    keyword_ids, keyword_fallback_used = _keyword_candidate_ids(active, request.query)
    if keyword_fallback_used:
        warnings.append("关键词索引暂不可用，本次已使用受控逐块搜索。")
    dense_ids: list[str] = []
    dense_available = False
    dense_failed = False

    if active.vector_index_mode == "ready":
        try:
            dense_ids = _dense_candidate_ids(active, request.query)
            dense_available = True
        except Exception:
            # 本地模型缓存、Chroma 目录或嵌入依赖均可能在索引完成后被人为清理。这里不尝试
            # 下载或修复模型，避免一次普通检索悄悄打开联网/磁盘副作用。
            dense_failed = True
            warnings.append("本机语义索引暂不可用，本次已降级为关键词检索。")
    else:
        warnings.append("本机语义索引尚未准备，本次使用关键词检索。")

    ranked = _fuse_ranked_candidates(keyword_ids, dense_ids)
    evidences, parent_deduplicated_count = _load_parent_evidences(
        active=active,
        ranked_candidates=ranked,
        top_k=request.top_k,
    )
    # Dense 已经执行并不代表有可信证据：所有向量命中都可能被距离门槛过滤。此时必须返回
    # no_result，而不是把“计算过相似度”伪装成可回答的 Hybrid 结果。
    if not evidences:
        mode = "no_result"
        if not warnings:
            warnings.append("当前活动资料中没有检索到与问题匹配的内容。")
    elif dense_available:
        mode = "hybrid"
    elif dense_failed:
        mode = "keyword_fallback"
    else:
        mode = "keyword"

    return KnowledgeRetrievalResult(
        knowledge_base_id=active.knowledge_base_id,
        query=request.query,
        evidences=evidences,
        diagnostics=KnowledgeRetrievalDiagnostics(
            mode=mode,
            active_index_generation=active.generation_number,
            keyword_candidate_count=len(keyword_ids),
            dense_candidate_count=len(dense_ids),
            parent_deduplicated_count=parent_deduplicated_count,
            warnings=warnings,
        ),
    )


def _build_cache_identity(
    *,
    active: _ActiveGeneration,
    request: KnowledgeRetrievalRequest,
) -> KnowledgeRetrievalCacheIdentity:
    """从普通查询生成不可逆摘要；缓存和诊断都不需要保存客户问题正文。"""

    return KnowledgeRetrievalCacheIdentity(
        knowledge_base_id=active.knowledge_base_id,
        active_index_generation=active.generation_number,
        retrieval_profile_version=RETRIEVAL_PROFILE_VERSION,
        normalized_query_sha256=sha256(request.query.encode("utf-8")).hexdigest(),
        top_k=request.top_k,
    )


def _cache_key(identity: KnowledgeRetrievalCacheIdentity) -> tuple[str, int, str, str, int]:
    """固定缓存键顺序，避免把 Pydantic 对象或客户正文保留在全局容器中。"""

    return (
        identity.knowledge_base_id,
        identity.active_index_generation,
        identity.retrieval_profile_version,
        identity.normalized_query_sha256,
        identity.top_k,
    )


def _load_cached_retrieval(
    identity: KnowledgeRetrievalCacheIdentity,
) -> tuple[KnowledgeRetrievalResult, int] | None:
    """读取未过期缓存，并返回真实年龄；过期项只在访问时清理。"""

    now = monotonic()
    key = _cache_key(identity)
    with _retrieval_cache_lock:
        cached = _retrieval_cache.get(key)
        if cached is None:
            return None
        age_seconds = now - cached.stored_at
        if age_seconds < 0 or age_seconds > RETRIEVAL_CACHE_TTL_SECONDS:
            _retrieval_cache.pop(key, None)
            return None
        _retrieval_cache.move_to_end(key)
        # Pydantic 深复制保证调用方、Evidence Gate 或未来的 Reranker 不会修改缓存本体。
        return cached.result.model_copy(deep=True), max(0, round(age_seconds * 1000))


def _store_cached_retrieval(identity: KnowledgeRetrievalCacheIdentity, result: KnowledgeRetrievalResult) -> None:
    """写入容量有限的 LRU；索引更新后 generation 不同，旧项不会再被查询命中。"""

    key = _cache_key(identity)
    with _retrieval_cache_lock:
        _retrieval_cache[key] = _CachedRetrieval(
            stored_at=monotonic(),
            result=result.model_copy(deep=True),
        )
        _retrieval_cache.move_to_end(key)
        while len(_retrieval_cache) > RETRIEVAL_CACHE_MAX_ENTRIES:
            _retrieval_cache.popitem(last=False)


def _cacheable_result(result: KnowledgeRetrievalResult) -> bool:
    """临时 FTS/Dense 故障必须下次重试，不能被短缓存放大为三分钟的降级。"""

    return result.diagnostics.mode in {"keyword", "hybrid", "no_result"}


def _with_cache_diagnostics(
    result: KnowledgeRetrievalResult,
    *,
    state: str,
    age_ms: int | None,
) -> KnowledgeRetrievalResult:
    """只在返回副本上标注实际缓存事实，缓存本体保持可复用的基础检索诊断。"""

    diagnostics = result.diagnostics.model_copy(
        update={"local_cache_state": state, "local_cache_age_ms": age_ms}
    )
    return result.model_copy(update={"diagnostics": diagnostics}, deep=True)


def _load_active_generation(knowledge_base_id: str) -> _ActiveGeneration:
    """只接受资料库当前指针所指向的 ready generation。"""

    with get_connection() as connection:
        base = connection.execute(
            "SELECT status, active_index_generation FROM knowledge_bases WHERE knowledge_base_id = ?",
            (knowledge_base_id,),
        ).fetchone()
        if base is None:
            raise KnowledgeBaseNotFoundError("未找到指定资料库。")
        if str(base["status"]) in {"deleting", "deleted"}:
            raise KnowledgeBaseUnavailableError("资料库正在删除或已删除，不能检索。")
        row = connection.execute(
            """
            SELECT generation.index_generation_id, generation.generation_number, generation.vector_index_mode
            FROM knowledge_bases AS base
            INNER JOIN knowledge_index_generations AS generation
                ON generation.knowledge_base_id = base.knowledge_base_id
                AND generation.generation_number = base.active_index_generation
            WHERE base.knowledge_base_id = ?
                AND base.status IN ('ready', 'partial_failure', 'indexing')
                AND base.active_index_generation >= 1
                AND generation.status = 'ready'
                AND generation.keyword_index_mode = 'fts5_cjk'
            """,
            (knowledge_base_id,),
        ).fetchone()
    if row is None:
        raise KnowledgeRetrievalUnavailableError("当前资料库没有可用的活动索引，请先完成本地索引。")
    return _ActiveGeneration(
        knowledge_base_id=knowledge_base_id,
        index_generation_id=str(row["index_generation_id"]),
        generation_number=int(row["generation_number"]),
        vector_index_mode=str(row["vector_index_mode"]),
    )


def _keyword_candidate_ids(active: _ActiveGeneration, query: str) -> tuple[list[str], bool]:
    """从 FTS 的 content 与中文二元词影子字段取得稳定关键词候选。"""

    terms, minimum_matches = _keyword_terms(query)
    match_query = _build_fts_match(terms)
    if not match_query:
        return [], False
    try:
        rows = _query_fts_candidate_rows(active, match_query)
    except sqlite3.DatabaseError:
        # FTS 是可重建派生索引。它异常时不能把资料库误判成“没有答案”，只允许在
        # 当前活动 generation 的受控子块内做有限回读，不扫描路径或历史版本。
        return _controlled_chunk_search(active, terms, minimum_matches), True
    return _filter_keyword_rows(rows, terms=terms, minimum_matches=minimum_matches), False


def _query_fts_candidate_rows(active: _ActiveGeneration, match_query: str) -> list[sqlite3.Row]:
    """读取有限 FTS 候选；独立函数方便用夹具验证故障降级。"""

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT child_chunk_id, cjk_shadow,
                   bm25(knowledge_child_chunks_fts, 1.0, 1.8) AS keyword_rank
            FROM knowledge_child_chunks_fts
            WHERE knowledge_child_chunks_fts MATCH ?
                AND knowledge_base_id = ?
                AND index_generation_id = ?
            ORDER BY keyword_rank ASC, child_chunk_id ASC
            LIMIT ?
            """,
            (match_query, active.knowledge_base_id, active.index_generation_id, KEYWORD_CANDIDATE_LIMIT),
        ).fetchall()
    return rows


def _filter_keyword_rows(
    rows: Iterable[sqlite3.Row],
    *,
    terms: list[str],
    minimum_matches: int,
) -> list[str]:
    """对 OR 初筛结果执行中文覆盖门槛，避免单个泛词造成无答案假命中。"""

    if minimum_matches <= 1:
        return [str(row["child_chunk_id"]) for row in rows]
    # 中文二元词以 OR 做 FTS 初筛是为了容忍问句尾部和口语表达；若没有二次覆盖门槛，
    # “资料”“系统”等泛词却会把无答案题误召回。影子字段是派生索引而非新正文读取，
    # 因而在服务内按至少两个独立命中词筛选，保留召回弹性并显著压低这种噪声。
    filtered: list[str] = []
    term_set = set(terms)
    for row in rows:
        shadow_terms = set(str(row["cjk_shadow"]).split())
        if len(term_set.intersection(shadow_terms)) >= minimum_matches:
            filtered.append(str(row["child_chunk_id"]))
    return filtered


def _controlled_chunk_search(
    active: _ActiveGeneration,
    terms: list[str],
    minimum_matches: int,
) -> list[str]:
    """FTS 不可用时的最小确定性回退，只搜索当前活动 generation 的受控子块。"""

    if not terms:
        return []
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT child.child_chunk_id, child.content
            FROM knowledge_child_chunks AS child
            INNER JOIN knowledge_generation_documents AS mapping
                ON mapping.document_version_id = child.document_version_id
            WHERE child.knowledge_base_id = ? AND mapping.index_generation_id = ?
            ORDER BY child.document_version_id ASC, child.ordinal ASC
            LIMIT 500
            """,
            (active.knowledge_base_id, active.index_generation_id),
        ).fetchall()
    matched: list[str] = []
    for row in rows:
        content = str(row["content"])
        normalized_content = content.lower()
        match_count = sum(
            1
            for term in terms
            if (term in normalized_content if _ASCII_OR_IDENTIFIER.fullmatch(term) else term in content)
        )
        if match_count >= minimum_matches:
            matched.append(str(row["child_chunk_id"]))
            if len(matched) >= KEYWORD_CANDIDATE_LIMIT:
                break
    return matched


def _keyword_terms(query: str) -> tuple[list[str], int]:
    """返回 FTS 词项与中文覆盖门槛；精确标识符永远优先于自然语言碎片。"""

    identifiers = _ASCII_OR_IDENTIFIER.findall(query.lower())
    if identifiers:
        # AF-204、合同号等是最强的客户意图，混入中文虚词会降低精确查找的稳定性。
        return list(dict.fromkeys(identifiers))[:24], 1
    terms: list[str] = []
    for sequence in _CHINESE_SEQUENCE.findall(query):
        if len(sequence) == 1:
            terms.append(sequence)
            continue
        # 影子字段同时存单字与二元词。二元词比逐字命中更有区分度，也不会要求客户了解 FTS。
        terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    unique_terms = list(dict.fromkeys(term for term in terms if term))[:24]
    return unique_terms, min(2, len(unique_terms))


def _build_fts_match(terms: list[str]) -> str:
    """由已清洗词项生成有限 FTS OR 表达式，调用方不能注入 FTS 运算符。"""

    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _dense_candidate_ids(active: _ActiveGeneration, query: str) -> list[str]:
    """只查询对应 generation 的本地 Chroma，并拒绝低相关向量候选。

    向量数据库一定会返回最近邻，但最近邻不等于资料能回答问题。距离门槛在 RRF 前执行，
    使没有字面证据、也没有足够语义相近证据的问题保持无答案，而不是把随机父块送给 K3。
    """

    vectors = embed_local_texts([query], allow_download=False)
    if len(vectors) != 1 or not vectors[0]:
        raise RuntimeError("本机 Embedding 查询向量无效。")
    index = ChromaGenerationIndex(
        knowledge_base_id=active.knowledge_base_id,
        generation_number=active.generation_number,
    )
    try:
        hits = index.query(vectors[0], limit=DENSE_CANDIDATE_LIMIT)
    finally:
        index.close()
    return [item.child_chunk_id for item in hits if item.distance <= DENSE_MAX_L2_DISTANCE]


def _fuse_ranked_candidates(keyword_ids: Iterable[str], dense_ids: Iterable[str]) -> list[_RankedCandidate]:
    """以固定 RRF 融合两条候选队列，保留渠道事实供 K3 与测试解释。"""

    candidates: dict[str, _RankedCandidate] = {}
    for channel, identifiers in (("keyword", keyword_ids), ("dense", dense_ids)):
        for rank, child_chunk_id in enumerate(identifiers, start=1):
            candidate = candidates.setdefault(child_chunk_id, _RankedCandidate(child_chunk_id=child_chunk_id))
            candidate.add(rank=rank, channel=channel)
    return sorted(candidates.values(), key=lambda item: (-item.score, item.child_chunk_id))


def _load_parent_evidences(
    *,
    active: _ActiveGeneration,
    ranked_candidates: list[_RankedCandidate],
    top_k: int,
) -> tuple[list[KnowledgeRetrievalEvidence], int]:
    """回读候选的受控事实，并在父块层去重，避免同一段挤占模型上下文。"""

    if not ranked_candidates:
        return [], 0
    candidate_ids = [item.child_chunk_id for item in ranked_candidates]
    placeholders = ",".join("?" for _ in candidate_ids)
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT child.child_chunk_id, child.parent_chunk_id, child.document_id, child.document_version_id,
                   child.source_kind, child.source_locator, child.start_char, child.end_char,
                   child.content AS child_content,
                   parent.heading_path_json, parent.content AS parent_content,
                   document.display_name
            FROM knowledge_child_chunks AS child
            INNER JOIN knowledge_parent_chunks AS parent ON parent.parent_chunk_id = child.parent_chunk_id
            INNER JOIN knowledge_documents AS document ON document.document_id = child.document_id
            INNER JOIN knowledge_generation_documents AS mapping
                ON mapping.document_version_id = child.document_version_id
            WHERE child.child_chunk_id IN ({placeholders})
                AND child.knowledge_base_id = ?
                AND parent.knowledge_base_id = ?
                AND document.knowledge_base_id = ?
                AND mapping.index_generation_id = ?
            """,
            (*candidate_ids, active.knowledge_base_id, active.knowledge_base_id, active.knowledge_base_id,
             active.index_generation_id),
        ).fetchall()
    rows_by_child = {str(row["child_chunk_id"]): row for row in rows}
    evidences: list[KnowledgeRetrievalEvidence] = []
    seen_parents: set[str] = set()
    duplicate_count = 0
    candidates_by_id = {item.child_chunk_id: item for item in ranked_candidates}
    for child_chunk_id in candidate_ids:
        row = rows_by_child.get(child_chunk_id)
        candidate = candidates_by_id[child_chunk_id]
        if row is None:
            continue
        parent_chunk_id = str(row["parent_chunk_id"])
        if parent_chunk_id in seen_parents:
            duplicate_count += 1
            continue
        seen_parents.add(parent_chunk_id)
        heading_path = _parse_heading_path(str(row["heading_path_json"]))
        source = KnowledgeSourceAnchor(
            document_id=str(row["document_id"]),
            document_version_id=str(row["document_version_id"]),
            source_kind=str(row["source_kind"]),
            source_locator=str(row["source_locator"]),
            start_char=int(row["start_char"]),
            end_char=int(row["end_char"]),
            heading_path=heading_path,
        )
        evidences.append(
            KnowledgeRetrievalEvidence(
                child_chunk_id=child_chunk_id,
                parent_chunk_id=parent_chunk_id,
                document_id=str(row["document_id"]),
                document_version_id=str(row["document_version_id"]),
                document_name=str(row["display_name"]),
                source=source,
                heading_path=heading_path,
                parent_content=_bounded_text(str(row["parent_content"]), 24_000),
                matched_content=_bounded_text(str(row["child_content"]), 12_000),
                retrieval_score=candidate.score,
                retrieval_channels=sorted(candidate.channels or set()),
            )
        )
        if len(evidences) >= top_k:
            break
    return evidences, duplicate_count


def _parse_heading_path(raw_value: str) -> list[str]:
    """DB 中的结构路径不可信时安全降级为空，不能因此中断整次证据回读。"""

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed[:12] if isinstance(item, str) and item.strip()]


def _bounded_text(value: str, maximum: int) -> str:
    """保护后续 LLM 证据预算；正常分块低于上限，异常大块才会被截断。"""

    if len(value) <= maximum:
        return value
    return value[:maximum]
