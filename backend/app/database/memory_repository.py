from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from app.database.sqlite import get_connection
from app.schemas.memory import LongTermMemoryRecord


class LongTermMemoryNotFoundError(LookupError):
    """请求的长期记忆不存在或已被删除。"""


def create_long_term_memory(
    *,
    kind: str,
    scope: str,
    title: str,
    summary: str,
    tags: list[str],
    source_task_id: str | None,
    user_confirmed: bool,
) -> LongTermMemoryRecord:
    """插入一条显式确认的长期记忆。

    SQLite 使用短连接并在单次写入中提交，避免同一桌面端多次请求意外共享事务状态。
    """

    now = _utc_now()
    record = LongTermMemoryRecord(
        memory_id=f"memory_{uuid4().hex[:12]}",
        kind=kind,
        scope=scope,
        title=title,
        summary=summary,
        tags=tags,
        source_task_id=source_task_id or None,
        user_confirmed=user_confirmed,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO long_term_memories (
                memory_id, kind, scope, title, summary, tags_json, source_task_id,
                user_confirmed, enabled, created_at, updated_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.memory_id,
                record.kind,
                record.scope,
                record.title,
                record.summary,
                json.dumps(record.tags, ensure_ascii=False),
                record.source_task_id or "",
                int(record.user_confirmed),
                int(record.enabled),
                record.created_at,
                record.updated_at,
                record.last_used_at,
            ),
        )
    return record


def list_long_term_memories(
    *,
    scope: str | None = None,
    include_disabled: bool = True,
    limit: int = 200,
) -> list[LongTermMemoryRecord]:
    """按范围读取记忆管理列表；默认仍显示已关闭项，方便用户重新启用或删除。"""

    clauses: list[str] = []
    params: list[object] = []
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    if not include_disabled:
        clauses.append("enabled = 1")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 200)))
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM long_term_memories"
            f"{where} ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def get_long_term_memory(memory_id: str) -> LongTermMemoryRecord:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM long_term_memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
    if row is None:
        raise LongTermMemoryNotFoundError("未找到指定的长期记忆。")
    return _row_to_record(row)


def update_long_term_memory(
    memory_id: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    enabled: bool | None = None,
) -> LongTermMemoryRecord:
    """只更新显式提交字段，保留来源任务与原始创建时间用于审计。"""

    existing = get_long_term_memory(memory_id)
    updated = LongTermMemoryRecord(
        memory_id=existing.memory_id,
        kind=existing.kind,
        scope=existing.scope,
        title=title if title is not None else existing.title,
        summary=summary if summary is not None else existing.summary,
        tags=tags if tags is not None else existing.tags,
        source_task_id=existing.source_task_id,
        user_confirmed=existing.user_confirmed,
        enabled=enabled if enabled is not None else existing.enabled,
        created_at=existing.created_at,
        updated_at=_utc_now(),
        last_used_at=existing.last_used_at,
    )
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE long_term_memories
            SET title = ?, summary = ?, tags_json = ?, enabled = ?, updated_at = ?
            WHERE memory_id = ?
            """,
            (
                updated.title,
                updated.summary,
                json.dumps(updated.tags, ensure_ascii=False),
                int(updated.enabled),
                updated.updated_at,
                memory_id,
            ),
        )
    return updated


def delete_long_term_memory(memory_id: str) -> None:
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM long_term_memories WHERE memory_id = ?", (memory_id,)
        )
    if cursor.rowcount == 0:
        raise LongTermMemoryNotFoundError("未找到指定的长期记忆。")


def clear_long_term_memories(scope: str) -> int:
    """按明确范围删除记忆；API 层还要求 confirm=true，防止误清空。"""

    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM long_term_memories WHERE scope = ?", (scope,))
    return max(cursor.rowcount, 0)


def search_long_term_memories(
    *,
    query: str,
    scopes: set[str],
    limit: int = 3,
) -> list[LongTermMemoryRecord]:
    """以标签、标题与摘要做轻量本地检索。

    C2 初版刻意不用向量库或全量 embedding：检索数据很少、每条均为用户确认的短事实，
    关键词和中文二字片段足以服务最小上下文注入，并且结果稳定、可解释、零网络开销。
    """

    candidates = [
        item
        for item in list_long_term_memories(include_disabled=False)
        if item.scope in scopes and item.user_confirmed
    ]
    terms = _search_terms(query)
    scored: list[tuple[int, LongTermMemoryRecord]] = []
    for item in candidates:
        haystack = f"{item.title}\n{item.summary}".lower()
        tag_set = {tag.lower() for tag in item.tags}
        score = 0
        # 全局偏好本身可能没有与本次任务重叠的关键词，但它通常是客户明确希望持续遵从的
        # 表达/交付约束，因此给一个很小的基础分，仍会被明确匹配的项目约束超过。
        if item.kind == "user_preference" and item.scope == "global":
            score = 1
        for term in terms:
            if term in tag_set:
                score += 8
            if term in item.title.lower():
                score += 5
            if term in haystack:
                score += 2
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
    return [item for _, item in scored[: max(1, min(limit, 3))]]


def mark_long_term_memories_used(memory_ids: list[str]) -> None:
    """仅在实际注入计划后更新使用时间，不能把“列表被打开”记成使用。"""

    unique_ids = list(dict.fromkeys(memory_ids))[:3]
    if not unique_ids:
        return
    placeholders = ",".join("?" for _ in unique_ids)
    with get_connection() as connection:
        connection.execute(
            f"UPDATE long_term_memories SET last_used_at = ? WHERE memory_id IN ({placeholders})",
            [_utc_now(), *unique_ids],
        )


def _row_to_record(row) -> LongTermMemoryRecord:
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    return LongTermMemoryRecord(
        memory_id=str(row["memory_id"]),
        kind=str(row["kind"]),
        scope=str(row["scope"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        tags=[str(item) for item in tags if isinstance(item, str)],
        source_task_id=str(row["source_task_id"] or "") or None,
        user_confirmed=bool(row["user_confirmed"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_used_at=str(row["last_used_at"] or ""),
    )


def _search_terms(query: str) -> list[str]:
    normalized = " ".join(query.lower().split())
    terms = set(re.findall(r"[a-z0-9_+-]{2,}", normalized))
    # 中文通常没有空格分词。二字片段是可解释的轻量兜底，且只用于极少量已确认记录，
    # 不等同于 RAG 语义检索。
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        terms.add(segment)
        terms.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return sorted(terms)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
