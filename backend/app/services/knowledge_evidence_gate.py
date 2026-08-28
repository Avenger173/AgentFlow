"""知识库 K3 的 Evidence Gate。

Gate 是回答模型之前的确定性屏障：它只接收 K2 Retrieval Service 的结果，重新确认每个来源
仍属于资料库当前活动 generation，再依据问题形态检查最低资料覆盖。未通过时不向模型泄露
父块正文，也不会把“没命中”改写成模型臆测。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.database.knowledge_repository import KnowledgeBaseNotFoundError, KnowledgeBaseUnavailableError
from app.database.sqlite import get_connection
from app.schemas.knowledge import (
    KnowledgeAnswerSource,
    KnowledgeEvidenceGateResult,
    KnowledgeRetrievalEvidence,
    KnowledgeRetrievalResult,
)


_COMPARISON_MARKERS = ("对比", "比较", "区别", "差异", "冲突", "分别", "各自", "谁更")


class KnowledgeEvidenceUnavailableError(ValueError):
    """资料库没有可用于二次核验的活动 generation。"""


@dataclass(frozen=True)
class _ActiveEvidenceGeneration:
    """Gate 只需要当前活动 generation 的身份，不读取正文或检索实现对象。"""

    knowledge_base_id: str
    generation_number: int
    generation_id: str


def gate_knowledge_evidence(retrieval: KnowledgeRetrievalResult) -> KnowledgeEvidenceGateResult:
    """核验 K2 证据仍有效，并返回模型可使用的最小来源卡或明确拒答状态。"""

    active = _load_active_generation(retrieval.knowledge_base_id)
    warnings = list(retrieval.diagnostics.warnings)
    required_document_count = _required_document_count(retrieval.query)
    if retrieval.diagnostics.active_index_generation != active.generation_number:
        # 更新/删除切换后，旧证据哪怕内容仍在数据库中，也绝不能继续喂给模型回答。
        warnings.append("资料库索引已更新，请重新检索后再提问。")
        return _insufficient_result(
            retrieval=retrieval,
            active=active,
            required_document_count=required_document_count,
            warnings=warnings,
        )

    sources: list[KnowledgeAnswerSource] = []
    invalid_count = 0
    for evidence in retrieval.evidences:
        if not _evidence_still_active(evidence, active):
            invalid_count += 1
            continue
        sources.append(_to_answer_source(evidence, source_id=f"kb_src_{len(sources) + 1}"))
    if invalid_count:
        warnings.append(f"{invalid_count} 条旧来源未通过当前版本核验，已排除。")

    covered_document_count = len({item.document_id for item in sources})
    if not sources:
        # 检索层可能已经说明 FTS 或语义索引的降级原因，但客户仍需要得到可行动的结论。
        # 这条提示不代表资料中不存在答案，只表示当前可定位证据不足，模型不得补写答案。
        warnings.append("当前资料不足以回答该问题，请补充材料或换一种问法。")
        return _insufficient_result(
            retrieval=retrieval,
            active=active,
            required_document_count=required_document_count,
            warnings=warnings,
        )
    if covered_document_count < required_document_count:
        warnings.append("该问题需要比较多个对象，但当前只覆盖到部分资料；回答只能标为部分依据。")
        evidence_state = "partial"
    else:
        evidence_state = "sufficient"
    return KnowledgeEvidenceGateResult(
        knowledge_base_id=active.knowledge_base_id,
        query=retrieval.query,
        active_index_generation=active.generation_number,
        evidence_state=evidence_state,
        required_document_count=required_document_count,
        covered_document_count=covered_document_count,
        sources=sources,
        warnings=warnings[:8],
    )


def _load_active_generation(knowledge_base_id: str) -> _ActiveEvidenceGeneration:
    """与 K2 相同地只接受资料库当前指针的 ready generation。"""

    with get_connection() as connection:
        base = connection.execute(
            "SELECT status FROM knowledge_bases WHERE knowledge_base_id = ?",
            (knowledge_base_id,),
        ).fetchone()
        if base is None:
            raise KnowledgeBaseNotFoundError("未找到指定资料库。")
        if str(base["status"]) in {"deleting", "deleted"}:
            raise KnowledgeBaseUnavailableError("资料库正在删除或已删除，不能核验来源。")
        generation = connection.execute(
            """
            SELECT generation.index_generation_id, generation.generation_number
            FROM knowledge_bases AS base
            INNER JOIN knowledge_index_generations AS generation
                ON generation.knowledge_base_id = base.knowledge_base_id
                AND generation.generation_number = base.active_index_generation
            WHERE base.knowledge_base_id = ?
                AND base.status IN ('ready', 'partial_failure', 'indexing')
                AND generation.status = 'ready'
            """,
            (knowledge_base_id,),
        ).fetchone()
    if generation is None:
        raise KnowledgeEvidenceUnavailableError("当前资料库没有可核验的活动索引，请先完成本地索引。")
    return _ActiveEvidenceGeneration(
        knowledge_base_id=knowledge_base_id,
        generation_number=int(generation["generation_number"]),
        generation_id=str(generation["index_generation_id"]),
    )


def _required_document_count(query: str) -> int:
    """比较/冲突类问题至少需要两份独立资料；普通问题只要求一份可定位来源。"""

    normalized = query.replace(" ", "")
    return 2 if any(marker in normalized for marker in _COMPARISON_MARKERS) else 1


def _evidence_still_active(
    evidence: KnowledgeRetrievalEvidence,
    active: _ActiveEvidenceGeneration,
) -> bool:
    """按块、版本、父块、来源范围和 generation 快照二次核验，不能只相信客户端传回 ID。"""

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT child.child_chunk_id
            FROM knowledge_child_chunks AS child
            INNER JOIN knowledge_parent_chunks AS parent ON parent.parent_chunk_id = child.parent_chunk_id
            INNER JOIN knowledge_generation_documents AS mapping
                ON mapping.document_version_id = child.document_version_id
            WHERE child.child_chunk_id = ?
                AND child.parent_chunk_id = ?
                AND child.document_id = ?
                AND child.document_version_id = ?
                AND child.knowledge_base_id = ?
                AND child.source_kind = ?
                AND child.source_locator = ?
                AND child.start_char = ?
                AND child.end_char = ?
                AND parent.knowledge_base_id = ?
                AND mapping.index_generation_id = ?
            """,
            (
                evidence.child_chunk_id,
                evidence.parent_chunk_id,
                evidence.document_id,
                evidence.document_version_id,
                active.knowledge_base_id,
                evidence.source.source_kind,
                evidence.source.source_locator,
                evidence.source.start_char,
                evidence.source.end_char,
                active.knowledge_base_id,
                active.generation_id,
            ),
        ).fetchone()
    return row is not None


def _to_answer_source(evidence: KnowledgeRetrievalEvidence, *, source_id: str) -> KnowledgeAnswerSource:
    """把 K2 的内部证据降成最小来源卡；模型全文上下文以后仅从已通过 Gate 的证据另行组装。"""

    return KnowledgeAnswerSource(
        source_id=source_id,
        document_id=evidence.document_id,
        document_version_id=evidence.document_version_id,
        document_name=evidence.document_name,
        source=evidence.source,
        heading_path=evidence.heading_path,
        excerpt=_compact_excerpt(evidence.matched_content),
        retrieval_channels=evidence.retrieval_channels,
    )


def _compact_excerpt(value: str) -> str:
    """来源栏只需可辨认片段；完整父块继续留在受控 Retrieval Service 内部。"""

    normalized = " ".join(value.split())
    return normalized[:900] if len(normalized) <= 900 else normalized[:897] + "..."


def _insufficient_result(
    *,
    retrieval: KnowledgeRetrievalResult,
    active: _ActiveEvidenceGeneration,
    required_document_count: int,
    warnings: list[str],
) -> KnowledgeEvidenceGateResult:
    """统一构造不向模型开放来源的拒答结果。"""

    return KnowledgeEvidenceGateResult(
        knowledge_base_id=active.knowledge_base_id,
        query=retrieval.query,
        active_index_generation=active.generation_number,
        evidence_state="insufficient",
        required_document_count=required_document_count,
        covered_document_count=0,
        sources=[],
        warnings=warnings[:8],
    )
