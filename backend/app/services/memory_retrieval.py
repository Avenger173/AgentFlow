"""长期记忆 MEM-5 的候选融合与本地 Dense 适配。

正式默认路径仍由 Repository 选择 BM25。这里的 Dense Provider 仅复用知识库已确认的
FastEmbed 模型，调用时禁止下载权重；它不创建第二个 embedding Provider 或持久向量库。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Protocol, Sequence

from app.schemas.memory import LongTermMemoryRecord


RRF_K = 60


@dataclass(frozen=True)
class LongTermMemoryRetrievalDiagnostics:
    """无正文的检索事实；MEM-6 再决定是否和如何持久化聚合指标。"""

    mode: str
    bm25_candidate_count: int
    structured_candidate_count: int
    global_preference_candidate_count: int
    dense_candidate_count: int
    fallback_reason: str = ""


@dataclass(frozen=True)
class LongTermMemoryRetrievalResult:
    """Repository 的受控检索回执，记录始终已完成范围与启用状态校验。"""

    records: list[LongTermMemoryRecord]
    diagnostics: LongTermMemoryRetrievalDiagnostics


class MemoryDenseCandidateProvider(Protocol):
    """可选语义候选协议，输入只允许已确认的同范围短事实。"""

    def rank(
        self,
        *,
        query: str,
        candidates: Sequence[LongTermMemoryRecord],
        limit: int,
    ) -> list[str]: ...


class LocalMemoryDenseCandidateProvider:
    """复用知识库 FastEmbed 的进程内 Dense 候选器。

    缓存只保存 memory ID、更新时间和数值向量，不保存来源、路径、完整会话或持久化副本。
    该对象由明确的评测/实验调用方创建；普通 Commander 路径不会隐式加载本地模型。
    """

    def __init__(self) -> None:
        self._vectors: dict[tuple[str, str], list[float]] = {}

    def rank(
        self,
        *,
        query: str,
        candidates: Sequence[LongTermMemoryRecord],
        limit: int,
    ) -> list[str]:
        if not candidates or limit < 1:
            return []
        from app.services.knowledge_vector_index import embed_local_texts

        candidate_keys = [(item.memory_id, item.updated_at) for item in candidates]
        missing = [
            item
            for item, key in zip(candidates, candidate_keys, strict=True)
            if key not in self._vectors
        ]
        for offset in range(0, len(missing), 64):
            batch = missing[offset : offset + 64]
            vectors = embed_local_texts(
                [_memory_embedding_text(item) for item in batch],
                allow_download=False,
            )
            if len(vectors) != len(batch) or any(not vector for vector in vectors):
                raise RuntimeError("本地长期记忆 Dense 候选生成失败。")
            for item, vector in zip(batch, vectors, strict=True):
                self._vectors[(item.memory_id, item.updated_at)] = [float(value) for value in vector]

        query_vectors = embed_local_texts([query], allow_download=False)
        if len(query_vectors) != 1 or not query_vectors[0]:
            raise RuntimeError("本地长期记忆查询向量无效。")
        query_vector = [float(value) for value in query_vectors[0]]
        scored: list[tuple[float, str]] = []
        for item, key in zip(candidates, candidate_keys, strict=True):
            vector = self._vectors[key]
            score = _cosine_similarity(query_vector, vector)
            scored.append((score, item.memory_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [memory_id for _, memory_id in scored[: min(limit, 32)]]


def fuse_ranked_memory_ids_rrf(
    *,
    keyword_ids: Sequence[str],
    dense_ids: Sequence[str],
) -> list[str]:
    """按知识库同款 RRF 融合关键词与 Dense 排名。"""

    scores: dict[str, float] = {}
    for identifiers in (keyword_ids, dense_ids):
        for rank, memory_id in enumerate(identifiers, start=1):
            if memory_id:
                scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (RRF_K + rank)
    return [memory_id for memory_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def fuse_ranked_memory_ids_weighted(
    *,
    keyword_ids: Sequence[str],
    dense_ids: Sequence[str],
    keyword_weight: float = 0.7,
    dense_weight: float = 0.3,
) -> list[str]:
    """提供文档提出的 7:3 加权对照，不改变默认检索策略。"""

    scores: dict[str, float] = {}
    for weight, identifiers in ((keyword_weight, keyword_ids), (dense_weight, dense_ids)):
        for rank, memory_id in enumerate(identifiers, start=1):
            if memory_id:
                scores[memory_id] = scores.get(memory_id, 0.0) + weight / rank
    return [memory_id for memory_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def _memory_embedding_text(item: LongTermMemoryRecord) -> str:
    return "\n".join((item.title, item.summary, " ".join(item.tags)))


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    denominator = sqrt(sum(value * value for value in left)) * sqrt(sum(value * value for value in right))
    if denominator <= 0:
        return -1.0
    return sum(first * second for first, second in zip(left, right, strict=True)) / denominator
