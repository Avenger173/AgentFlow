from __future__ import annotations

import json
from datetime import UTC, datetime
import sqlite3
from uuid import uuid4

from app.database.sqlite import get_connection
from app.memory_search import build_memory_fts_match, build_memory_fts_shadow, build_memory_search_terms
from app.schemas.memory import LongTermMemoryProposal, LongTermMemoryRecord
from app.services.long_term_memory import build_memory_conflict_key
from app.services.memory_retrieval import (
    LongTermMemoryRetrievalDiagnostics,
    LongTermMemoryRetrievalResult,
    MemoryDenseCandidateProvider,
    fuse_ranked_memory_ids_rrf,
    fuse_ranked_memory_ids_weighted,
)


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
            SET title = ?, summary = ?, tags_json = ?, retrieval_shadow = ?, enabled = ?, memory_key = ?, updated_at = ?
            WHERE memory_id = ?
            """,
            (
                updated.title,
                updated.summary,
                json.dumps(updated.tags, ensure_ascii=False),
                build_memory_fts_shadow([updated.title, updated.summary, *updated.tags]),
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
    """返回默认 BM25 路径的最小长期记忆上下文。"""

    return search_long_term_memory_retrieval(
        query=query,
        scopes=scopes,
        limit=limit,
    ).records


def search_long_term_memory_retrieval(
    *,
    query: str,
    scopes: set[str],
    limit: int = 3,
    dense_candidate_provider: MemoryDenseCandidateProvider | None = None,
    fusion_strategy: str = "rrf",
) -> LongTermMemoryRetrievalResult:
    """按范围检索长期记忆，并在明确请求时评测可选 Dense 候选。

    Commander 默认不传 Dense Provider，因此不会在正常聊天中加载 Embedding 模型。评测调用
    可以复用知识库已确认的本地模型；模型、依赖或缓存不可用时结果安全回退到 BM25/词面，
    并且所有候选都先经 SQL 范围、开关和确认状态过滤。
    """

    normalized_scopes = sorted({scope.strip() for scope in scopes if scope and scope.strip()})
    if not normalized_scopes:
        return LongTermMemoryRetrievalResult(
            records=[],
            diagnostics=LongTermMemoryRetrievalDiagnostics(
                mode="no_scope",
                bm25_candidate_count=0,
                structured_candidate_count=0,
                global_preference_candidate_count=0,
                dense_candidate_count=0,
            ),
        )
    if fusion_strategy not in {"rrf", "weighted"}:
        raise ValueError("长期记忆 Hybrid 融合策略只能是 rrf 或 weighted。")

    result_limit = max(1, min(limit, 3))
    fallback_reason = ""
    try:
        bm25_pairs = _search_bm25_candidates(query=query, scopes=normalized_scopes)
        mode = "bm25"
    except sqlite3.DatabaseError:
        # FTS5 是可重建派生索引。异常时仍只扫描已确认的同范围短事实，绝不放宽 scope。
        fallback_records = _load_active_memory_records(scopes=normalized_scopes, limit=10_000)
        bm25_pairs = _score_lexical_candidates(query=query, candidates=fallback_records)
        mode = "lexical_fallback"
        fallback_reason = "fts_unavailable"

    bm25_records = [item for _, item in bm25_pairs]
    records_by_id = {item.memory_id: item for item in bm25_records}
    global_preferences = _load_global_preference_records(scopes=normalized_scopes)
    records_by_id.update({item.memory_id: item for item in global_preferences})
    search_terms = build_memory_search_terms(query)
    structured_ids = [
        item.memory_id
        for item in bm25_records
        if _structured_match_score(item, search_terms) > 0
    ]
    keyword_ids = [item.memory_id for item in bm25_records]
    dense_ids: list[str] = []
    ranked_primary = _merge_ids(structured_ids, keyword_ids)

    if dense_candidate_provider is not None:
        dense_candidates = _load_active_memory_records(scopes=normalized_scopes, limit=10_000)
        records_by_id.update({item.memory_id: item for item in dense_candidates})
        try:
            dense_ids = dense_candidate_provider.rank(
                query=query,
                candidates=dense_candidates,
                limit=32,
            )
            dense_ids = [memory_id for memory_id in dense_ids if memory_id in records_by_id]
            fused = (
                fuse_ranked_memory_ids_rrf(keyword_ids=keyword_ids, dense_ids=dense_ids)
                if fusion_strategy == "rrf"
                else fuse_ranked_memory_ids_weighted(keyword_ids=keyword_ids, dense_ids=dense_ids)
            )
            ranked_primary = _merge_ids(structured_ids, fused)
            mode = f"hybrid_{fusion_strategy}" if mode == "bm25" else f"{mode}_{fusion_strategy}"
        except Exception:
            # 这里禁止自动下载或重试模型。普通记忆读取仍可继续，真实 Dense 准入由 MEM-5
            # 专项评测另行记录，不能因临时依赖问题扩大客户输入或范围。
            fallback_reason = (
                "dense_unavailable"
                if not fallback_reason
                else f"{fallback_reason};dense_unavailable"
            )
            mode = f"{mode}_dense_unavailable"

    # 全局用户偏好是独立通道：没有关键词重叠时仍可被最小上下文采用，但它只会追加在结构化
    # 项目约束和 BM25/Hybrid 候选之后，避免通用表达偏好压过当前项目的明确事实。
    final_ids = _merge_ids(
        ranked_primary,
        [item.memory_id for item in global_preferences],
    )
    records = [records_by_id[memory_id] for memory_id in final_ids if memory_id in records_by_id]
    return LongTermMemoryRetrievalResult(
        records=records[:result_limit],
        diagnostics=LongTermMemoryRetrievalDiagnostics(
            mode=mode,
            bm25_candidate_count=len(bm25_records),
            structured_candidate_count=len(structured_ids),
            global_preference_candidate_count=len(global_preferences),
            dense_candidate_count=len(dense_ids),
            fallback_reason=fallback_reason,
        ),
    )


def _search_bm25_candidates(
    *,
    query: str,
    scopes: list[str],
) -> list[tuple[float, LongTermMemoryRecord]]:
    """从 FTS5 取有限 BM25 候选，并在同一 SQL 中重申长期记忆准入边界。"""

    match_query = build_memory_fts_match(query)
    if not match_query:
        return []
    scope_placeholders = ",".join("?" for _ in scopes)
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT memory.*, bm25(long_term_memory_fts, 1.6, 1.0, 1.4, 1.2) AS bm25_rank
            FROM long_term_memory_fts
            INNER JOIN long_term_memories AS memory
                ON memory.rowid = long_term_memory_fts.rowid
            WHERE long_term_memory_fts MATCH ?
                AND memory.scope IN ({scope_placeholders})
                AND memory.enabled = 1
                AND memory.user_confirmed = 1
            ORDER BY bm25_rank ASC, memory.updated_at DESC, memory.memory_id ASC
            LIMIT 96
            """,
            [match_query, *scopes],
        ).fetchall()
    # SQLite FTS5 的 bm25 值越小越相关；转成正分数仅为了让 fallback 与测试的排序方向一致。
    return [(-float(row["bm25_rank"]), _row_to_record(row)) for row in rows]


def _load_active_memory_records(*, scopes: list[str], limit: int) -> list[LongTermMemoryRecord]:
    """读取已确认且启用的同范围候选，Dense 与 FTS 故障回退共用这道 SQL 边界。"""

    scope_placeholders = ",".join("?" for _ in scopes)
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM long_term_memories "
            f"WHERE scope IN ({scope_placeholders}) AND enabled = 1 AND user_confirmed = 1 "
            "ORDER BY updated_at DESC, created_at DESC, memory_id DESC LIMIT ?",
            [*scopes, max(1, min(limit, 10_000))],
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def _load_global_preference_records(*, scopes: list[str]) -> list[LongTermMemoryRecord]:
    if "global" not in scopes:
        return []
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM long_term_memories
            WHERE scope = 'global' AND kind = 'user_preference'
                AND enabled = 1 AND user_confirmed = 1
            ORDER BY updated_at DESC, created_at DESC, memory_id DESC
            LIMIT 12
            """
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def _score_lexical_candidates(
    *,
    query: str,
    candidates: list[LongTermMemoryRecord],
) -> list[tuple[float, LongTermMemoryRecord]]:
    """FTS 故障时的有限词面回退，保留 C2 的可解释排序语义。"""

    terms = build_memory_search_terms(query)
    scored: list[tuple[float, LongTermMemoryRecord]] = []
    for item in candidates:
        score = _lexical_match_score(item, terms)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].updated_at, pair[1].memory_id), reverse=True)
    return scored


def _structured_match_score(item: LongTermMemoryRecord, terms: list[str]) -> int:
    """为项目范围的精确约束提供独立候选通道。"""

    if item.kind != "project_constraint" or not terms:
        return 0
    title = item.title.lower()
    tags = {tag.lower() for tag in item.tags}
    return sum(8 for term in terms if term in tags) + sum(5 for term in terms if term in title)


def _lexical_match_score(item: LongTermMemoryRecord, terms: list[str]) -> int:
    title = item.title.lower()
    summary = item.summary.lower()
    tags = {tag.lower() for tag in item.tags}
    score = 1 if item.kind == "user_preference" and item.scope == "global" else 0
    for term in terms:
        if term in tags:
            score += 8
        if term in title:
            score += 5
        if term in summary:
            score += 2
    return score


def _merge_ids(*groups: list[str]) -> list[str]:
    return list(dict.fromkeys(memory_id for group in groups for memory_id in group if memory_id))


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
            memory_id, kind, scope, title, summary, tags_json, retrieval_shadow, source_task_id,
            user_confirmed, enabled, memory_key, replaced_by_memory_id,
            created_at, updated_at, last_used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
        """,
        (
            record.memory_id,
            record.kind,
            record.scope,
            record.title,
            record.summary,
            json.dumps(record.tags, ensure_ascii=False),
            build_memory_fts_shadow([record.title, record.summary, *record.tags]),
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
