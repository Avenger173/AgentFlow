"""MEM-6 记忆观测仓储。

观测表只承载固定枚举、计数、时长和受控 memory ID；调用方不允许写入客户正文、标题、
文件名、路径、凭据或 embedding。这些限制在仓储边界再次校验，避免后续调用方误用。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from app.database.sqlite import get_connection
from app.schemas.memory import MemoryObservationRecord


_EVENT_TYPES = {"context", "retrieval", "lifecycle"}
_MEMORY_ID_PATTERN = re.compile(r"^memory_[a-z0-9]{12}$")
_RETRIEVAL_MODES = {
    "",
    "bm25",
    "lexical_fallback",
    "bm25_dense_unavailable",
    "lexical_fallback_dense_unavailable",
    "hybrid_rrf",
    "hybrid_weighted",
    "no_scope",
}
_FALLBACK_REASONS = {"", "fts_unavailable", "dense_unavailable", "fts_unavailable;dense_unavailable"}
_LIFECYCLE_ACTIONS = {"", "conversation_retention"}


def record_memory_observation(
    *,
    event_type: str,
    context_budget_tokens: int = 0,
    context_estimated_tokens: int = 0,
    compaction_count: int = 0,
    summary_message_count: int = 0,
    retrieval_mode: str = "",
    candidate_count: int = 0,
    recalled_memory_ids: list[str] | None = None,
    retrieval_latency_ms: int = 0,
    fallback_reason: str = "",
    lifecycle_action: str = "",
    affected_conversation_count: int = 0,
    affected_proposal_count: int = 0,
) -> MemoryObservationRecord:
    """写入一条经过字段白名单验证的无正文指标。"""

    if event_type not in _EVENT_TYPES:
        raise ValueError("记忆观测事件类型无效。")
    if retrieval_mode not in _RETRIEVAL_MODES:
        raise ValueError("记忆观测检索模式无效。")
    if fallback_reason not in _FALLBACK_REASONS:
        raise ValueError("记忆观测降级原因无效。")
    if lifecycle_action not in _LIFECYCLE_ACTIONS:
        raise ValueError("记忆观测生命周期动作无效。")
    normalized_ids = [item for item in (recalled_memory_ids or []) if _MEMORY_ID_PATTERN.fullmatch(item)]
    if len(normalized_ids) != len(recalled_memory_ids or []) or len(normalized_ids) > 8:
        raise ValueError("记忆观测只能保存至多 8 个受控长期记忆 ID。")
    record = MemoryObservationRecord(
        observation_id=f"memobs_{uuid4().hex[:16]}",
        event_type=event_type,
        observed_at=_utc_now(),
        context_budget_tokens=max(0, int(context_budget_tokens)),
        context_estimated_tokens=max(0, int(context_estimated_tokens)),
        compaction_count=max(0, int(compaction_count)),
        summary_message_count=max(0, int(summary_message_count)),
        retrieval_mode=retrieval_mode,
        candidate_count=max(0, int(candidate_count)),
        recalled_memory_ids=normalized_ids,
        retrieval_latency_ms=max(0, int(retrieval_latency_ms)),
        fallback_reason=fallback_reason,
        lifecycle_action=lifecycle_action,
        affected_conversation_count=max(0, int(affected_conversation_count)),
        affected_proposal_count=max(0, int(affected_proposal_count)),
    )
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO memory_observations (
                observation_id, event_type, observed_at, context_budget_tokens, context_estimated_tokens,
                compaction_count, summary_message_count, retrieval_mode, candidate_count,
                recalled_memory_ids_json, retrieval_latency_ms, fallback_reason, lifecycle_action,
                affected_conversation_count, affected_proposal_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.observation_id,
                record.event_type,
                record.observed_at,
                record.context_budget_tokens,
                record.context_estimated_tokens,
                record.compaction_count,
                record.summary_message_count,
                record.retrieval_mode,
                record.candidate_count,
                json.dumps(record.recalled_memory_ids),
                record.retrieval_latency_ms,
                record.fallback_reason,
                record.lifecycle_action,
                record.affected_conversation_count,
                record.affected_proposal_count,
            ),
        )
    return record


def record_memory_observation_safely(**kwargs) -> MemoryObservationRecord | None:
    """观测写入失败不得中断客户聊天或生命周期维护。"""

    try:
        return record_memory_observation(**kwargs)
    except Exception:
        return None


def list_memory_observations(*, limit: int = 100) -> list[MemoryObservationRecord]:
    """读取最近的无正文诊断记录。"""

    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM memory_observations ORDER BY observed_at DESC, observation_id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [_row_to_observation(row) for row in rows]


def _row_to_observation(row) -> MemoryObservationRecord:
    try:
        recalled_memory_ids = json.loads(str(row["recalled_memory_ids_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        recalled_memory_ids = []
    if not isinstance(recalled_memory_ids, list):
        recalled_memory_ids = []
    return MemoryObservationRecord(
        observation_id=str(row["observation_id"]),
        event_type=str(row["event_type"]),
        observed_at=str(row["observed_at"]),
        context_budget_tokens=max(0, int(row["context_budget_tokens"])),
        context_estimated_tokens=max(0, int(row["context_estimated_tokens"])),
        compaction_count=max(0, int(row["compaction_count"])),
        summary_message_count=max(0, int(row["summary_message_count"])),
        retrieval_mode=str(row["retrieval_mode"] or ""),
        candidate_count=max(0, int(row["candidate_count"])),
        recalled_memory_ids=[item for item in recalled_memory_ids if isinstance(item, str) and _MEMORY_ID_PATTERN.fullmatch(item)],
        retrieval_latency_ms=max(0, int(row["retrieval_latency_ms"])),
        fallback_reason=str(row["fallback_reason"] or ""),
        lifecycle_action=str(row["lifecycle_action"] or ""),
        affected_conversation_count=max(0, int(row["affected_conversation_count"])),
        affected_proposal_count=max(0, int(row["affected_proposal_count"])),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
