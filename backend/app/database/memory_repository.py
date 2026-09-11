from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from app.database.sqlite import get_connection
from app.schemas.memory import LongTermMemoryProposal, LongTermMemoryRecord
from app.services.long_term_memory import build_memory_conflict_key


class LongTermMemoryNotFoundError(LookupError):
    """请求的长期记忆不存在或已被删除。"""


class LongTermMemoryProposalNotFoundError(LookupError):
    """请求的长期记忆候选不存在。"""


class LongTermMemoryProposalStateError(ValueError):
    """候选当前状态不允许执行所请求的生命周期转换。"""


def create_long_term_memory(
    *,
    kind: str,
    scope: str,
    title: str,
    summary: str,
    tags: list[str],
    source_task_id: str | None,
    user_confirmed: bool,
    memory_key: str | None = None,
) -> LongTermMemoryRecord:
    """插入一条显式确认的长期记忆。

    SQLite 使用短连接并在单次写入中提交，避免同一桌面端多次请求意外共享事务状态。
    """

    now = _utc_now()
    normalized_memory_key = memory_key or build_memory_conflict_key(
        kind=kind,
        scope=scope,
        title=title,
    )
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
        _insert_long_term_memory(
            connection=connection,
            record=record,
            memory_key=normalized_memory_key,
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
            SET title = ?, summary = ?, tags_json = ?, enabled = ?, memory_key = ?, updated_at = ?
            WHERE memory_id = ?
            """,
            (
                updated.title,
                updated.summary,
                json.dumps(updated.tags, ensure_ascii=False),
                int(updated.enabled),
                build_memory_conflict_key(
                    kind=updated.kind,
                    scope=updated.scope,
                    title=updated.title,
                ),
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

    normalized_scopes = sorted({scope.strip() for scope in scopes if scope and scope.strip()})
    if not normalized_scopes:
        return []
    result_limit = max(1, min(limit, 3))
    candidate_pool_limit = 200
    scope_placeholders = ",".join("?" for _ in normalized_scopes)
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM long_term_memories "
            f"WHERE scope IN ({scope_placeholders}) AND enabled = 1 AND user_confirmed = 1 "
            "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            [*normalized_scopes, candidate_pool_limit],
        ).fetchall()
    # 范围、开关和确认状态必须在 SQL 中先过滤再 LIMIT。若先从全库取最近 200 条，其他项目
    # 的新记录会把当前项目的较早约束挤出候选池，表现为“没有泄漏但错误漏召回”。
    candidates = [_row_to_record(row) for row in rows]
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
    return [item for _, item in scored[:result_limit]]


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


def create_or_reuse_long_term_memory_proposal(
    *,
    proposal_id: str,
    task_id: str,
    kind: str,
    suggested_scope: str,
    title: str,
    summary: str,
    tags: list[str],
    reason: str,
    source_type: str,
    source_id: str,
    source_conversation_id: str | None,
    conflict_key: str,
    fingerprint: str,
) -> LongTermMemoryProposal:
    """以内容指纹持久化候选，并把同键旧待确认项标为已替代。

    ``fingerprint`` 不含任务或会话 ID：同一条稳定事实即使在压缩、任务恢复或网络重试中再次
    被观察到，也只能对应同一候选。候选尚未确认时不会写入正式长期记忆表。
    """

    now = _utc_now()
    with get_connection() as connection:
        existing = connection.execute(
            "SELECT * FROM long_term_memory_proposals WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing is not None:
            return _row_to_proposal(existing)

        pending_rows = connection.execute(
            """
            SELECT * FROM long_term_memory_proposals
            WHERE suggested_scope = ? AND kind = ? AND conflict_key = ? AND status = 'pending'
            ORDER BY updated_at DESC, created_at DESC
            """,
            (suggested_scope, kind, conflict_key),
        ).fetchall()
        replaces_proposal_id = str(pending_rows[0]["proposal_id"]) if pending_rows else ""
        if pending_rows:
            connection.execute(
                """
                UPDATE long_term_memory_proposals
                SET status = 'superseded', replaced_by_proposal_id = ?, updated_at = ?
                WHERE suggested_scope = ? AND kind = ? AND conflict_key = ? AND status = 'pending'
                """,
                (proposal_id, now, suggested_scope, kind, conflict_key),
            )

        active_memory = _find_active_memory_by_key(
            connection=connection,
            kind=kind,
            scope=suggested_scope,
            memory_key=conflict_key,
        )
        replaces_memory_id = active_memory.memory_id if active_memory is not None else ""
        connection.execute(
            """
            INSERT INTO long_term_memory_proposals (
                proposal_id, task_id, kind, suggested_scope, title, summary, tags_json, reason,
                source_type, source_id, source_conversation_id, conflict_key, fingerprint, status,
                replaces_proposal_id, replaced_by_proposal_id, replaces_memory_id, confirmed_memory_id,
                created_at, updated_at, confirmed_at, rejected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, '', ?, '', ?, ?, '', '')
            """,
            (
                proposal_id,
                task_id,
                kind,
                suggested_scope,
                title,
                summary,
                json.dumps(tags, ensure_ascii=False),
                reason,
                source_type,
                source_id,
                source_conversation_id or "",
                conflict_key,
                fingerprint,
                replaces_proposal_id,
                replaces_memory_id,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM long_term_memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("长期记忆候选写入后无法回读。")
    return _row_to_proposal(row)


def get_long_term_memory_proposal(proposal_id: str) -> LongTermMemoryProposal:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM long_term_memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    if row is None:
        raise LongTermMemoryProposalNotFoundError("未找到指定的长期记忆候选。")
    return _row_to_proposal(row)


def list_long_term_memory_proposals(
    *,
    task_id: str | None = None,
    scope: str | None = None,
    statuses: set[str] | None = None,
    limit: int = 200,
) -> list[LongTermMemoryProposal]:
    """列出受控范围内的候选；默认只返回待确认项。"""

    clauses: list[str] = []
    params: list[object] = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if scope:
        clauses.append("suggested_scope = ?")
        params.append(scope)
    normalized_statuses = sorted(statuses or {"pending"})
    placeholders = ",".join("?" for _ in normalized_statuses)
    clauses.append(f"status IN ({placeholders})")
    params.extend(normalized_statuses)
    where = " WHERE " + " AND ".join(clauses)
    params.append(max(1, min(limit, 200)))
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM long_term_memory_proposals"
            f"{where} ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_proposal(row) for row in rows]


def reject_long_term_memory_proposal(proposal_id: str) -> LongTermMemoryProposal:
    """显式拒绝候选；同一拒绝请求可安全重试。"""

    now = _utc_now()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM long_term_memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise LongTermMemoryProposalNotFoundError("未找到指定的长期记忆候选。")
        current = _row_to_proposal(row)
        if current.status == "rejected":
            return current
        if current.status != "pending":
            raise LongTermMemoryProposalStateError("当前候选已确认、过期或被替代，不能再拒绝。")
        connection.execute(
            """
            UPDATE long_term_memory_proposals
            SET status = 'rejected', rejected_at = ?, updated_at = ?
            WHERE proposal_id = ?
            """,
            (now, now, proposal_id),
        )
        updated = connection.execute(
            "SELECT * FROM long_term_memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    if updated is None:
        raise RuntimeError("长期记忆候选拒绝后无法回读。")
    return _row_to_proposal(updated)


def confirm_long_term_memory_proposal(
    *,
    proposal_id: str,
    kind: str,
    scope: str,
    title: str,
    summary: str,
    tags: list[str],
) -> tuple[LongTermMemoryProposal, LongTermMemoryRecord]:
    """确认候选并原子处理同键去重或替代。"""

    now = _utc_now()
    memory_key = build_memory_conflict_key(kind=kind, scope=scope, title=title)
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM long_term_memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise LongTermMemoryProposalNotFoundError("未找到指定的长期记忆候选。")
        proposal = _row_to_proposal(row)
        if proposal.kind != kind:
            raise LongTermMemoryProposalStateError("候选类型不能在确认时修改。")
        if proposal.status == "confirmed":
            if not proposal.confirmed_memory_id:
                raise LongTermMemoryProposalStateError("已确认候选缺少正式记忆关联，已拒绝继续写入。")
            memory_row = connection.execute(
                "SELECT * FROM long_term_memories WHERE memory_id = ?",
                (proposal.confirmed_memory_id,),
            ).fetchone()
            if memory_row is None:
                raise LongTermMemoryProposalStateError("已确认候选的正式记忆已不存在，请重新创建候选。")
            return proposal, _row_to_record(memory_row)
        if proposal.status != "pending":
            raise LongTermMemoryProposalStateError("当前候选已被拒绝、过期或替代，请重新查看待确认列表。")

        existing = _find_active_memory_by_key(
            connection=connection,
            kind=kind,
            scope=scope,
            memory_key=memory_key,
        )
        if existing is not None and (
            existing.title == title
            and existing.summary == summary
            and existing.tags == tags
        ):
            record = existing
            replaces_memory_id = ""
        else:
            record = LongTermMemoryRecord(
                memory_id=f"memory_{uuid4().hex[:12]}",
                kind=kind,
                scope=scope,
                title=title,
                summary=summary,
                tags=tags,
                source_task_id=proposal.task_id or None,
                user_confirmed=True,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            _insert_long_term_memory(connection=connection, record=record, memory_key=memory_key)
            replaces_memory_id = existing.memory_id if existing is not None else ""
            if existing is not None:
                connection.execute(
                    """
                    UPDATE long_term_memories
                    SET enabled = 0, replaced_by_memory_id = ?, updated_at = ?
                    WHERE memory_id = ?
                    """,
                    (record.memory_id, now, existing.memory_id),
                )

        connection.execute(
            """
            UPDATE long_term_memory_proposals
            SET suggested_scope = ?, title = ?, summary = ?, tags_json = ?, conflict_key = ?,
                status = 'confirmed', replaces_memory_id = ?, confirmed_memory_id = ?,
                confirmed_at = ?, updated_at = ?
            WHERE proposal_id = ?
            """,
            (
                scope,
                title,
                summary,
                json.dumps(tags, ensure_ascii=False),
                memory_key,
                replaces_memory_id,
                record.memory_id,
                now,
                now,
                proposal_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM long_term_memory_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
    if updated is None:
        raise RuntimeError("长期记忆候选确认后无法回读。")
    return _row_to_proposal(updated), record


def _insert_long_term_memory(*, connection, record: LongTermMemoryRecord, memory_key: str) -> None:
    connection.execute(
        """
        INSERT INTO long_term_memories (
            memory_id, kind, scope, title, summary, tags_json, source_task_id,
            user_confirmed, enabled, memory_key, replaced_by_memory_id,
            created_at, updated_at, last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
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
            memory_key,
            record.created_at,
            record.updated_at,
            record.last_used_at,
        ),
    )


def _find_active_memory_by_key(*, connection, kind: str, scope: str, memory_key: str) -> LongTermMemoryRecord | None:
    rows = connection.execute(
        """
        SELECT * FROM long_term_memories
        WHERE kind = ? AND scope = ? AND enabled = 1 AND user_confirmed = 1
        ORDER BY updated_at DESC, created_at DESC
        """,
        (kind, scope),
    ).fetchall()
    for row in rows:
        stored_key = str(row["memory_key"] or "")
        legacy_key = build_memory_conflict_key(
            kind=str(row["kind"]),
            scope=str(row["scope"]),
            title=str(row["title"]),
        )
        if (stored_key or legacy_key) == memory_key:
            return _row_to_record(row)
    return None


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


def _row_to_proposal(row) -> LongTermMemoryProposal:
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    return LongTermMemoryProposal(
        proposal_id=str(row["proposal_id"]),
        task_id=str(row["task_id"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        tags=[str(item) for item in tags if isinstance(item, str)],
        suggested_scope=str(row["suggested_scope"]),
        reason=str(row["reason"] or ""),
        status=str(row["status"]),
        source_type=str(row["source_type"]),
        source_id=str(row["source_id"] or ""),
        source_conversation_id=str(row["source_conversation_id"] or "") or None,
        replaces_proposal_id=str(row["replaces_proposal_id"] or "") or None,
        replaced_by_proposal_id=str(row["replaced_by_proposal_id"] or "") or None,
        replaces_memory_id=str(row["replaces_memory_id"] or "") or None,
        confirmed_memory_id=str(row["confirmed_memory_id"] or "") or None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
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
