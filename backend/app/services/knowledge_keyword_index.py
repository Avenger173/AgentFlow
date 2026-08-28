"""知识库 K1.4 的可恢复关键词索引任务服务。

本模块只完成本地 SQLite FTS5 generation 的构建、验证和活动指针切换。Dense/Chroma 是同一
generation 的后续增强：本地 Embedding 模型未明确下载前保持 ``pending``，绝不把关键词
索引写成 Hybrid 成功。所有步骤只读取知识库私有副本和 SQLite 受控块，不调用 LLM 或网络。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import sqlite3
from time import monotonic
from typing import Literal
from uuid import uuid4

from app.database.sqlite import get_connection
from app.database.knowledge_repository import (
    KnowledgeBaseNotFoundError,
    KnowledgeBaseUnavailableError,
    _append_audit_event,
    get_knowledge_base,
    _row_to_document_version,
    parse_knowledge_document_version,
)
from app.schemas.knowledge import KnowledgeIndexJobRecord
from app.services.knowledge_vector_index import (
    ChromaGenerationIndex,
    VectorRecord,
    embed_local_texts,
    vector_index_capability,
)


_CHINESE_SEQUENCE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


class KnowledgeIndexJobNotFoundError(LookupError):
    """索引任务不存在，或调用方使用了错误的稳定 ID。"""


@dataclass(frozen=True)
class _VectorGenerationResult:
    """同一 generation 的向量构建结果，不携带向量、原文或本机目录。"""

    mode: Literal["pending", "ready", "failed"]
    indexed_child_count: int = 0
    reused_child_count: int = 0
    embedded_child_count: int = 0
    failure_summary: str = ""


def create_knowledge_index_job(knowledge_base_id: str) -> KnowledgeIndexJobRecord:
    """为当前候选版本建立新的不可变关键词索引 generation。"""

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        base = connection.execute(
            "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
        ).fetchone()
        if base is None:
            connection.rollback()
            raise KnowledgeBaseNotFoundError("未找到指定资料库。")
        if str(base["status"]) in {"deleting", "deleted"}:
            connection.rollback()
            raise KnowledgeBaseUnavailableError("资料库正在删除或已删除，不能建立索引。")
        active_job = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE knowledge_base_id = ? AND status IN ('queued', 'running')",
            (knowledge_base_id,),
        ).fetchone()
        if active_job is not None:
            connection.rollback()
            return _row_to_job(active_job)

        candidates = connection.execute(
            """
            SELECT version.*
            FROM knowledge_document_versions AS version
            INNER JOIN (
                SELECT document_id, MAX(version_number) AS max_version
                FROM knowledge_document_versions
                WHERE knowledge_base_id = ? AND status IN ('queued', 'parsed', 'failed', 'ready')
                GROUP BY document_id
            ) AS latest
                ON latest.document_id = version.document_id
                AND latest.max_version = version.version_number
            WHERE version.knowledge_base_id = ?
            ORDER BY version.document_id ASC
            """,
            (knowledge_base_id, knowledge_base_id),
        ).fetchall()
        if not candidates:
            connection.rollback()
            raise KnowledgeBaseUnavailableError("当前没有可建立索引的候选资料版本。")
        profile = connection.execute(
            "SELECT profile_json FROM knowledge_index_profiles WHERE profile_id = ?",
            (str(base["default_index_profile_id"]),),
        ).fetchone()
        if profile is None:  # pragma: no cover - 资料库外键应保证不发生。
            connection.rollback()
            raise RuntimeError("资料库缺少默认索引 Profile。")
        reusable_job = _find_reusable_active_index_job(
            connection,
            base=base,
            candidate_version_ids=[str(candidate["document_version_id"]) for candidate in candidates],
            profile_json=str(profile["profile_json"]),
        )
        if reusable_job is not None:
            _append_audit_event(
                connection,
                knowledge_base_id=knowledge_base_id,
                event_type="knowledge_index_job_reused",
                summary="资料和索引配置未变化，继续使用已验证的活动索引。",
                details={
                    "index_job_id": str(reusable_job["index_job_id"]),
                    "index_generation_id": str(reusable_job["index_generation_id"]),
                    "target_generation_number": int(reusable_job["target_generation_number"]),
                },
                created_at=now,
            )
            connection.commit()
            return _row_to_job(reusable_job)
        next_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(generation_number), 0) AS max_number "
                "FROM knowledge_index_generations WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()["max_number"]
        ) + 1
        generation_id = f"kb_gen_{uuid4().hex[:12]}"
        job_id = f"kb_job_{uuid4().hex[:12]}"
        connection.execute(
            """
            INSERT INTO knowledge_index_generations (
                index_generation_id, knowledge_base_id, generation_number, status, index_profile_json,
                keyword_index_mode, vector_index_mode, failure_summary, created_at, activated_at
            ) VALUES (?, ?, ?, 'building', ?, 'fts5_cjk', 'pending', '', ?, '')
            """,
            (generation_id, knowledge_base_id, next_number, str(profile["profile_json"]), now),
        )
        for ordinal, version in enumerate(candidates, start=1):
            connection.execute(
                """
                INSERT INTO knowledge_generation_documents (index_generation_id, document_version_id, ordinal)
                VALUES (?, ?, ?)
                """,
                (generation_id, str(version["document_version_id"]), ordinal),
            )
        connection.execute(
            """
            INSERT INTO knowledge_index_jobs (
                index_job_id, knowledge_base_id, index_generation_id, target_generation_number,
                status, stage, total_document_count, parsed_document_count, indexed_document_count,
                failed_document_count, cancel_requested, failure_summary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', 'queued', ?, 0, 0, 0, 0, '', ?, ?)
            """,
            (job_id, knowledge_base_id, generation_id, next_number, len(candidates), now, now),
        )
        connection.execute(
            "UPDATE knowledge_bases SET status = 'indexing', updated_at = ? WHERE knowledge_base_id = ?",
            (now, knowledge_base_id),
        )
        _append_audit_event(
            connection,
            knowledge_base_id=knowledge_base_id,
            event_type="knowledge_index_job_queued",
            summary="已建立本地关键词索引任务，等待后台处理。",
            details={
                "index_job_id": job_id,
                "index_generation_id": generation_id,
                "document_version_count": len(candidates),
            },
            created_at=now,
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (job_id,)
        ).fetchone()
    if row is None:  # pragma: no cover - commit 后同连接回读不应为空。
        raise RuntimeError("新建知识库索引任务无法回读。")
    return _row_to_job(row)


def _find_reusable_active_index_job(
    connection: sqlite3.Connection,
    *,
    base: sqlite3.Row,
    candidate_version_ids: list[str],
    profile_json: str,
) -> sqlite3.Row | None:
    """返回可安全复用的完整活动索引任务，或明确要求建立新 generation。

    该快路径不是“文件名相同就跳过”：它同时绑定完整版本快照、不可变 Profile 与活动
    generation。部分失败的资料库必须允许客户重试；旧 generation 尚未建立向量而本机模型现已
    准备好时，也必须允许下一次构建补齐语义索引。
    """

    if str(base["status"]) != "ready" or int(base["active_index_generation"]) < 1:
        return None
    generation = connection.execute(
        """
        SELECT * FROM knowledge_index_generations
        WHERE knowledge_base_id = ? AND generation_number = ? AND status = 'ready'
        """,
        (str(base["knowledge_base_id"]), int(base["active_index_generation"])),
    ).fetchone()
    if generation is None or str(generation["index_profile_json"]) != profile_json:
        return None
    active_version_rows = connection.execute(
        """
        SELECT document_version_id FROM knowledge_generation_documents
        WHERE index_generation_id = ? ORDER BY ordinal ASC
        """,
        (str(generation["index_generation_id"]),),
    ).fetchall()
    if [str(row["document_version_id"]) for row in active_version_rows] != candidate_version_ids:
        return None

    vector_mode = str(generation["vector_index_mode"])
    if vector_mode not in {"ready", "disabled"} and vector_index_capability().model_initialized:
        # 客户已经主动准备了本地模型，继续建索引应补齐语义 generation，而不是把旧 pending
        # generation 当作已经完成 Hybrid。
        return None
    completed_job = connection.execute(
        """
        SELECT * FROM knowledge_index_jobs
        WHERE index_generation_id = ? AND status = 'completed'
        ORDER BY completed_at DESC, updated_at DESC LIMIT 1
        """,
        (str(generation["index_generation_id"]),),
    ).fetchone()
    return completed_job


def get_knowledge_index_job(index_job_id: str) -> KnowledgeIndexJobRecord:
    """读取单个任务的持久化事实，进程重启后仍可用于状态页恢复。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
    if row is None:
        raise KnowledgeIndexJobNotFoundError("未找到指定知识库索引任务。")
    return _row_to_job(row)


def list_knowledge_index_jobs(knowledge_base_id: str, *, limit: int = 50) -> list[KnowledgeIndexJobRecord]:
    """列出资料库最近任务，限制数量避免状态页无界读取历史。"""

    # 空列表不能用来掩盖错误资料库 ID；Qt 状态页需要区分“没有任务”和“对象已不存在”。
    get_knowledge_base(knowledge_base_id)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM knowledge_index_jobs WHERE knowledge_base_id = ?
            ORDER BY updated_at DESC, created_at DESC, index_job_id DESC LIMIT ?
            """,
            (knowledge_base_id, max(1, min(limit, 100))),
        ).fetchall()
    return [_row_to_job(row) for row in rows]


def run_knowledge_index_job(index_job_id: str) -> KnowledgeIndexJobRecord:
    """同步执行一个任务；API 层后续仅可在受控后台线程中调用该函数。"""

    started_at = monotonic()
    parse_and_chunk_elapsed_ms = 0
    vector_index_elapsed_ms = 0
    keyword_index_elapsed_ms = 0
    reused_parsed_document_count = 0
    vector_indexed_child_count = 0
    reused_vector_child_count = 0
    embedded_child_count = 0
    job = _claim_job(index_job_id)
    if job.status != "running":
        # 同一任务可能被客户连续点击或由重试 UI 同时受理；只有第一个短事务能把 queued
        # 原子切换为 running，后续调用必须只读返回，不能并发重建同一个 generation。
        return job
    cancelled = _cancel_index_job_if_requested(index_job_id)
    if cancelled is not None:
        return _with_index_job_performance_metrics(
            cancelled,
            reused_parsed_document_count=reused_parsed_document_count,
            parse_and_chunk_elapsed_ms=parse_and_chunk_elapsed_ms,
            vector_index_elapsed_ms=vector_index_elapsed_ms,
            keyword_index_elapsed_ms=keyword_index_elapsed_ms,
            total_elapsed_ms=_elapsed_ms(started_at),
            vector_indexed_child_count=vector_indexed_child_count,
            reused_vector_child_count=reused_vector_child_count,
            embedded_child_count=embedded_child_count,
        )
    version_ids = _job_document_version_ids(index_job_id)
    parsed_count = 0
    failed_count = 0
    partial_ocr_document_count = 0
    partial_ocr_failed_page_count = 0
    partial_ocr_retried_page_count = 0
    for document_version_id in version_ids:
        # 删除资料库会在短事务中写入 cancel_requested。解析单份 PDF/DOCX 不能被强行中断，
        # 但每份材料之间必须立即停下，避免删除中的资料库继续写入候选 generation。
        cancelled = _cancel_index_job_if_requested(index_job_id)
        if cancelled is not None:
            return _with_index_job_performance_metrics(
                cancelled,
                reused_parsed_document_count=reused_parsed_document_count,
                parse_and_chunk_elapsed_ms=parse_and_chunk_elapsed_ms,
                vector_index_elapsed_ms=vector_index_elapsed_ms,
                keyword_index_elapsed_ms=keyword_index_elapsed_ms,
                total_elapsed_ms=_elapsed_ms(started_at),
                vector_indexed_child_count=vector_indexed_child_count,
                reused_vector_child_count=reused_vector_child_count,
                embedded_child_count=embedded_child_count,
            )
        reused_before_parse = _is_parse_and_chunk_reusable(document_version_id)
        parse_started_at = monotonic()
        parsed = parse_knowledge_document_version(
            document_version_id,
            on_ocr_started=lambda: _update_job_progress(
                index_job_id=index_job_id,
                stage="ocr_recognizing",
                parsed_document_count=parsed_count,
                failed_document_count=failed_count,
                reused_parsed_document_count=reused_parsed_document_count,
                parse_and_chunk_elapsed_ms=_elapsed_ms(parse_started_at),
            ),
        )
        parse_and_chunk_elapsed_ms += _elapsed_ms(parse_started_at)
        # 新 generation 会携带未改动的活动版本。它们已经在上一代完成解析并处于 ready，
        # 复用时与本轮刚解析出的 parsed 一样都是可索引成功，不能误记为失败材料。
        if parsed.status in {"parsed", "ready"}:
            parsed_count += 1
            if reused_before_parse:
                reused_parsed_document_count += 1
            if parsed.ocr_failed_page_count:
                partial_ocr_document_count += 1
                partial_ocr_failed_page_count += parsed.ocr_failed_page_count
                partial_ocr_retried_page_count += parsed.ocr_retried_page_count
        else:
            failed_count += 1
        _update_job_progress(
            index_job_id=index_job_id,
            stage="chunking",
            parsed_document_count=parsed_count,
            failed_document_count=failed_count,
            reused_parsed_document_count=reused_parsed_document_count,
            parse_and_chunk_elapsed_ms=parse_and_chunk_elapsed_ms,
        )
        cancelled = _cancel_index_job_if_requested(index_job_id)
        if cancelled is not None:
            return _with_index_job_performance_metrics(
                cancelled,
                reused_parsed_document_count=reused_parsed_document_count,
                parse_and_chunk_elapsed_ms=parse_and_chunk_elapsed_ms,
                vector_index_elapsed_ms=vector_index_elapsed_ms,
                keyword_index_elapsed_ms=keyword_index_elapsed_ms,
                total_elapsed_ms=_elapsed_ms(started_at),
                vector_indexed_child_count=vector_indexed_child_count,
                reused_vector_child_count=reused_vector_child_count,
                embedded_child_count=embedded_child_count,
            )

    if parsed_count == 0:
        return _with_index_job_performance_metrics(
            _finish_without_index(index_job_id, "所有候选材料都未能完成解析。"),
            reused_parsed_document_count=reused_parsed_document_count,
            parse_and_chunk_elapsed_ms=parse_and_chunk_elapsed_ms,
            vector_index_elapsed_ms=vector_index_elapsed_ms,
            keyword_index_elapsed_ms=keyword_index_elapsed_ms,
            total_elapsed_ms=_elapsed_ms(started_at),
            vector_indexed_child_count=vector_indexed_child_count,
            reused_vector_child_count=reused_vector_child_count,
            embedded_child_count=embedded_child_count,
        )
    vector_started_at = monotonic()
    vector_result = _build_vector_generation_if_prepared(index_job_id)
    vector_index_elapsed_ms = _elapsed_ms(vector_started_at)
    vector_indexed_child_count = vector_result.indexed_child_count
    reused_vector_child_count = vector_result.reused_child_count
    embedded_child_count = vector_result.embedded_child_count
    cancelled = _cancel_index_job_if_requested(index_job_id)
    if cancelled is not None:
        if vector_result.mode == "ready":
            _discard_vector_generation(index_job_id)
        return _with_index_job_performance_metrics(
            cancelled,
            reused_parsed_document_count=reused_parsed_document_count,
            parse_and_chunk_elapsed_ms=parse_and_chunk_elapsed_ms,
            vector_index_elapsed_ms=vector_index_elapsed_ms,
            keyword_index_elapsed_ms=keyword_index_elapsed_ms,
            total_elapsed_ms=_elapsed_ms(started_at),
            vector_indexed_child_count=vector_indexed_child_count,
            reused_vector_child_count=reused_vector_child_count,
            embedded_child_count=embedded_child_count,
        )
    keyword_started_at = monotonic()
    completed = _build_and_activate_keyword_generation(
        index_job_id=index_job_id,
        parsed_count=parsed_count,
        failed_count=failed_count,
        partial_ocr_document_count=partial_ocr_document_count,
        partial_ocr_failed_page_count=partial_ocr_failed_page_count,
        partial_ocr_retried_page_count=partial_ocr_retried_page_count,
        vector_result=vector_result,
    )
    keyword_index_elapsed_ms = _elapsed_ms(keyword_started_at)
    return _with_index_job_performance_metrics(
        completed,
        reused_parsed_document_count=reused_parsed_document_count,
        parse_and_chunk_elapsed_ms=parse_and_chunk_elapsed_ms,
        vector_index_elapsed_ms=vector_index_elapsed_ms,
        keyword_index_elapsed_ms=keyword_index_elapsed_ms,
        total_elapsed_ms=_elapsed_ms(started_at),
        vector_indexed_child_count=vector_indexed_child_count,
        reused_vector_child_count=reused_vector_child_count,
        embedded_child_count=embedded_child_count,
    )


def recover_interrupted_knowledge_index_jobs() -> list[str]:
    """启动时安全收束遗留 running 任务，不自动重跑可能处于磁盘写入中的构建。"""

    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE status = 'running'"
        ).fetchall()
    recovered: list[str] = []
    for row in rows:
        job_id = str(row["index_job_id"])
        now = _utc_now()
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (job_id,)
            ).fetchone()
            if current is None or str(current["status"]) != "running":
                connection.rollback()
                continue
            reason = "服务重启时索引任务尚未结束；为避免使用不完整候选，已停止，请显式重试。"
            connection.execute(
                """
                UPDATE knowledge_index_jobs
                SET status = 'failed', stage = 'failed', failure_summary = ?, updated_at = ?, completed_at = ?
                WHERE index_job_id = ?
                """,
                (reason, now, now, job_id),
            )
            connection.execute(
                """
                UPDATE knowledge_index_generations
                SET status = 'failed', failure_summary = ? WHERE index_generation_id = ?
                """,
                (reason, str(current["index_generation_id"])),
            )
            _restore_base_after_failed_generation(connection, current, now)
            _append_audit_event(
                connection,
                knowledge_base_id=str(current["knowledge_base_id"]),
                event_type="knowledge_index_job_interrupted",
                summary="服务重启时停止未完成的知识库索引任务。",
                details={"index_job_id": job_id},
                created_at=now,
            )
            connection.commit()
        recovered.append(job_id)
    return recovered


def _claim_job(index_job_id: str) -> KnowledgeIndexJobRecord:
    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
        if row is None:
            connection.rollback()
            raise KnowledgeIndexJobNotFoundError("未找到指定知识库索引任务。")
        if str(row["status"]) != "queued":
            connection.rollback()
            return _row_to_job(row)
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET status = 'running', stage = 'parsing', updated_at = ?, started_at = ?
            WHERE index_job_id = ?
            """,
            (now, now, index_job_id),
        )
        connection.commit()
        claimed = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
    if claimed is None:  # pragma: no cover
        raise RuntimeError("索引任务状态无法回读。")
    return _row_to_job(claimed)


def _job_document_version_ids(index_job_id: str) -> list[str]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT mapping.document_version_id
            FROM knowledge_generation_documents AS mapping
            INNER JOIN knowledge_index_jobs AS job ON job.index_generation_id = mapping.index_generation_id
            WHERE job.index_job_id = ? ORDER BY mapping.ordinal ASC
            """,
            (index_job_id,),
        ).fetchall()
    return [str(row["document_version_id"]) for row in rows]


def _is_parse_and_chunk_reusable(document_version_id: str) -> bool:
    """判断当前 generation 是否能复用已持久化的解析/分块，而不读取材料正文。

    ``ready`` 与 ``parsed`` 都已经拥有同一 parser/splitter profile 产生的受控块；其他状态
    必须重新尝试解析。这个判断仅用于无正文性能计量，真正的状态准入仍由 Repository 执行。
    """

    with get_connection() as connection:
        row = connection.execute(
            "SELECT status FROM knowledge_document_versions WHERE document_version_id = ?",
            (document_version_id,),
        ).fetchone()
    return row is not None and str(row["status"]) in {"parsed", "ready"}


def _elapsed_ms(started_at: float) -> int:
    """把单调时钟转换为稳定的非负毫秒，供任务状态和历史审计使用。"""

    return max(0, round((monotonic() - started_at) * 1_000))


def _with_index_job_performance_metrics(
    job: KnowledgeIndexJobRecord,
    *,
    reused_parsed_document_count: int,
    parse_and_chunk_elapsed_ms: int,
    vector_index_elapsed_ms: int,
    keyword_index_elapsed_ms: int,
    total_elapsed_ms: int,
    vector_indexed_child_count: int,
    reused_vector_child_count: int,
    embedded_child_count: int,
) -> KnowledgeIndexJobRecord:
    """持久化本次执行已观测的阶段时长，不把进程内瞬时信息留在 API 回执之外。"""

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET reused_parsed_document_count = ?, parse_and_chunk_elapsed_ms = ?,
                vector_index_elapsed_ms = ?, keyword_index_elapsed_ms = ?, total_elapsed_ms = ?,
                vector_indexed_child_count = ?, reused_vector_child_count = ?, embedded_child_count = ?,
                updated_at = ?
            WHERE index_job_id = ?
            """,
            (
                max(0, reused_parsed_document_count),
                max(0, parse_and_chunk_elapsed_ms),
                max(0, vector_index_elapsed_ms),
                max(0, keyword_index_elapsed_ms),
                max(0, total_elapsed_ms),
                max(0, vector_indexed_child_count),
                max(0, reused_vector_child_count),
                max(0, embedded_child_count),
                _utc_now(),
                job.index_job_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (job.index_job_id,)
        ).fetchone()
    if row is None:  # pragma: no cover - Job 已在当前调用中确认存在。
        raise KnowledgeIndexJobNotFoundError("未找到指定知识库索引任务。")
    return _row_to_job(row)


def _update_job_progress(
    *,
    index_job_id: str,
    stage: str,
    parsed_document_count: int,
    failed_document_count: int,
    reused_parsed_document_count: int | None = None,
    parse_and_chunk_elapsed_ms: int | None = None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET stage = ?, parsed_document_count = ?, failed_document_count = ?,
                reused_parsed_document_count = COALESCE(?, reused_parsed_document_count),
                parse_and_chunk_elapsed_ms = COALESCE(?, parse_and_chunk_elapsed_ms), updated_at = ?
            WHERE index_job_id = ? AND status = 'running'
            """,
            (
                stage,
                parsed_document_count,
                failed_document_count,
                max(0, reused_parsed_document_count) if reused_parsed_document_count is not None else None,
                max(0, parse_and_chunk_elapsed_ms) if parse_and_chunk_elapsed_ms is not None else None,
                _utc_now(),
                index_job_id,
            ),
        )


def _build_and_activate_keyword_generation(
    *,
    index_job_id: str,
    parsed_count: int,
    failed_count: int,
    partial_ocr_document_count: int,
    partial_ocr_failed_page_count: int,
    partial_ocr_retried_page_count: int,
    vector_result: _VectorGenerationResult,
) -> KnowledgeIndexJobRecord:
    # FTS 写入与活动 generation 切换属于同一关键词阶段；先持久化阶段再进入可能较长的
    # SQLite 写入，使 Qt/API 能呈现真实工作状态而不是停在上一阶段。
    _update_job_progress(
        index_job_id=index_job_id,
        stage="keyword_indexing",
        parsed_document_count=parsed_count,
        failed_document_count=failed_count,
    )
    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
        if job is None:
            connection.rollback()
            raise KnowledgeIndexJobNotFoundError("未找到指定知识库索引任务。")
        if str(job["status"]) != "running":
            connection.rollback()
            if vector_result.mode == "ready":
                _discard_vector_generation(index_job_id)
            return _row_to_job(job)
        if int(job["cancel_requested"]):
            connection.rollback()
            if vector_result.mode == "ready":
                _discard_vector_generation(index_job_id)
            return _cancel_index_job_if_requested(index_job_id) or _row_to_job(job)
        generation_id = str(job["index_generation_id"])
        connection.execute(
            "DELETE FROM knowledge_child_chunks_fts WHERE index_generation_id = ?", (generation_id,)
        )
        chunks = connection.execute(
            """
            SELECT child.child_chunk_id, child.document_version_id, child.content
            FROM knowledge_child_chunks AS child
            INNER JOIN knowledge_generation_documents AS mapping
                ON mapping.document_version_id = child.document_version_id
            INNER JOIN knowledge_document_versions AS version
                ON version.document_version_id = child.document_version_id
            WHERE mapping.index_generation_id = ? AND version.status IN ('parsed', 'ready')
            ORDER BY child.ordinal ASC
            """,
            (generation_id,),
        ).fetchall()
        for chunk in chunks:
            content = str(chunk["content"])
            connection.execute(
                """
                INSERT INTO knowledge_child_chunks_fts (
                    child_chunk_id, knowledge_base_id, index_generation_id, content, cjk_shadow
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(chunk["child_chunk_id"]),
                    str(job["knowledge_base_id"]),
                    generation_id,
                    content,
                    _cjk_shadow(content),
                ),
            )
        indexed_child_count = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM knowledge_child_chunks_fts WHERE index_generation_id = ?",
                (generation_id,),
            ).fetchone()["total"]
        )
        if indexed_child_count != len(chunks) or not chunks:
            connection.rollback()
            if vector_result.mode == "ready":
                _discard_vector_generation(index_job_id)
            return _finish_without_index(index_job_id, "关键词索引回读数量不一致，未切换活动资料版本。")

        # 向量建成之前 generation 仍只能服务关键词检索。失败不撤回已验证 FTS，但任务和
        # generation 必须如实记录降级，后续 K2 Router 只在 mode=ready 时合并 dense 结果。
        failure_messages = []
        if failed_count:
            failure_messages.append(f"{failed_count} 份候选材料解析失败，已保留其它可索引材料。")
        # OCR 的部分页失败不等于整份材料不可用：成功页已经带着区域来源进入 FTS/向量索引。
        # 任务仍应如实显示 partial_failure，避免客户把“资料库可查询”误解为“扫描件完整无缺”。
        if partial_ocr_document_count:
            retry_text = (
                f"；其中 {partial_ocr_retried_page_count} 页已自动重试一次"
                if partial_ocr_retried_page_count
                else ""
            )
            failure_messages.append(
                f"OCR 有 {partial_ocr_document_count} 份材料共 {partial_ocr_failed_page_count} 页未识别"
                f"，其它成功页已进入索引{retry_text}。"
            )
        if vector_result.failure_summary:
            failure_messages.append(vector_result.failure_summary)
        failure_summary = " ".join(failure_messages)
        completion_status = "partial_failure" if failure_messages else "completed"
        completion_stage = "partial_failure" if failure_messages else "completed"
        connection.execute(
            """
            UPDATE knowledge_index_generations
            SET status = 'ready', vector_index_mode = ?, activated_at = ?
            WHERE index_generation_id = ?
            """,
            (vector_result.mode, now, generation_id),
        )
        connection.execute(
            """
            UPDATE knowledge_index_generations
            SET status = 'superseded'
            WHERE knowledge_base_id = ? AND index_generation_id != ? AND status = 'ready'
            """,
            (str(job["knowledge_base_id"]), generation_id),
        )
        active_versions = connection.execute(
            """
            SELECT version.document_id, version.document_version_id
            FROM knowledge_generation_documents AS mapping
            INNER JOIN knowledge_document_versions AS version
                ON version.document_version_id = mapping.document_version_id
            WHERE mapping.index_generation_id = ? AND version.status IN ('parsed', 'ready')
            """,
            (generation_id,),
        ).fetchall()
        for version in active_versions:
            connection.execute(
                "UPDATE knowledge_documents SET active_version_id = ?, updated_at = ? WHERE document_id = ?",
                (str(version["document_version_id"]), now, str(version["document_id"])),
            )
            connection.execute(
                "UPDATE knowledge_document_versions SET status = 'ready', updated_at = ? WHERE document_version_id = ?",
                (now, str(version["document_version_id"])),
            )
        connection.execute(
            """
            UPDATE knowledge_bases
            SET status = ?, active_index_generation = ?, active_document_version_count = ?, updated_at = ?
            WHERE knowledge_base_id = ?
            """,
            (
                "partial_failure" if failure_messages else "ready",
                int(job["target_generation_number"]),
                len(active_versions),
                now,
                str(job["knowledge_base_id"]),
            ),
        )
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET status = ?, stage = ?, parsed_document_count = ?, indexed_document_count = ?,
                failed_document_count = ?, failure_summary = ?, updated_at = ?, completed_at = ?
            WHERE index_job_id = ?
            """,
            (
                completion_status,
                completion_stage,
                parsed_count,
                len(active_versions),
                failed_count,
                failure_summary,
                now,
                now,
                index_job_id,
            ),
        )
        _append_audit_event(
            connection,
            knowledge_base_id=str(job["knowledge_base_id"]),
            event_type="knowledge_keyword_generation_activated",
            summary=_generation_activation_summary(vector_result.mode),
            details={
                "index_job_id": index_job_id,
                "index_generation_id": generation_id,
                "indexed_document_count": len(active_versions),
                "indexed_child_count": indexed_child_count,
                "vector_index_mode": vector_result.mode,
                "vector_indexed_child_count": vector_result.indexed_child_count,
                "reused_vector_child_count": vector_result.reused_child_count,
                "embedded_child_count": vector_result.embedded_child_count,
                "failed_document_count": failed_count,
            },
            created_at=now,
        )
        connection.commit()
        completed = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
    if completed is None:  # pragma: no cover
        raise RuntimeError("完成知识库索引任务无法回读。")
    return _row_to_job(completed)


def _build_vector_generation_if_prepared(index_job_id: str) -> _VectorGenerationResult:
    """只在客户已确认下载本地模型后，为本次 generation 建立 Chroma 向量。

    FastEmbed 调用始终使用 ``local_files_only``。模型缓存不完整、依赖缺失或 Chroma 回读失败
    都会清理候选目录并降级到关键词索引；原文、向量和底层错误路径不会出现在任务摘要。
    """

    capability = vector_index_capability()
    if not capability.model_initialized:
        return _VectorGenerationResult(mode="pending")
    if not capability.chroma_available or not capability.fastembed_available:
        return _VectorGenerationResult(
            mode="failed",
            failure_summary="本地语义索引依赖不可用，已保留关键词索引。",
        )

    with get_connection() as connection:
        job = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
        if job is None:
            raise KnowledgeIndexJobNotFoundError("未找到指定知识库索引任务。")
        if str(job["status"]) != "running" or int(job["cancel_requested"]):
            return _VectorGenerationResult(mode="pending")
        base = connection.execute(
            "SELECT status, active_index_generation FROM knowledge_bases WHERE knowledge_base_id = ?",
            (str(job["knowledge_base_id"]),),
        ).fetchone()
        if base is None or str(base["status"]) in {"deleting", "deleted"}:
            return _VectorGenerationResult(mode="pending")
        rows = connection.execute(
            """
            SELECT child.child_chunk_id, child.document_version_id, child.content, child.content_sha256
            FROM knowledge_child_chunks AS child
            INNER JOIN knowledge_generation_documents AS mapping
                ON mapping.document_version_id = child.document_version_id
            INNER JOIN knowledge_document_versions AS version
                ON version.document_version_id = child.document_version_id
            WHERE mapping.index_generation_id = ? AND version.status IN ('parsed', 'ready')
            ORDER BY child.ordinal ASC
            """,
            (str(job["index_generation_id"]),),
        ).fetchall()
        knowledge_base_id = str(job["knowledge_base_id"])
        generation_number = int(job["target_generation_number"])
        target_profile = connection.execute(
            "SELECT index_profile_json FROM knowledge_index_generations WHERE index_generation_id = ?",
            (str(job["index_generation_id"]),),
        ).fetchone()
        source_generation = None
        source_child_hashes: dict[str, str] = {}
        if target_profile is not None and int(base["active_index_generation"]) >= 1:
            # 只考虑当前活动代次：它仍然是已验证的 ready generation，且创建新任务后在切换
            # 指针前不会被 supersede。相同 Profile 约束同时绑定 Embedding 模型与归一化方式。
            source_generation = connection.execute(
                """
                SELECT index_generation_id, generation_number
                FROM knowledge_index_generations
                WHERE knowledge_base_id = ? AND generation_number = ? AND status = 'ready'
                  AND vector_index_mode = 'ready' AND index_profile_json = ?
                """,
                (
                    knowledge_base_id,
                    int(base["active_index_generation"]),
                    str(target_profile["index_profile_json"]),
                ),
            ).fetchone()
        if source_generation is not None:
            source_rows = connection.execute(
                """
                SELECT child.child_chunk_id, child.content_sha256
                FROM knowledge_child_chunks AS child
                INNER JOIN knowledge_generation_documents AS mapping
                    ON mapping.document_version_id = child.document_version_id
                WHERE mapping.index_generation_id = ?
                """,
                (str(source_generation["index_generation_id"]),),
            ).fetchall()
            source_child_hashes = {
                str(row["child_chunk_id"]): str(row["content_sha256"])
                for row in source_rows
            }

    if not rows:
        return _VectorGenerationResult(
            mode="failed",
            failure_summary="本地语义索引没有可写入的解析分块，已保留关键词索引。",
        )
    _update_job_progress(
        index_job_id=index_job_id,
        stage="vector_indexing",
        parsed_document_count=len({str(row["document_version_id"]) for row in rows}),
        failed_document_count=0,
    )
    index = ChromaGenerationIndex(
        knowledge_base_id=knowledge_base_id,
        generation_number=generation_number,
    )
    try:
        candidate_ids = [
            str(row["child_chunk_id"])
            for row in rows
            if source_child_hashes.get(str(row["child_chunk_id"])) == str(row["content_sha256"])
        ]
        reused_embeddings = _read_reusable_generation_embeddings(
            knowledge_base_id=knowledge_base_id,
            source_generation_number=(
                int(source_generation["generation_number"]) if source_generation is not None else None
            ),
            candidate_child_chunk_ids=candidate_ids,
        )
        # 只有 ID、内容哈希和 Profile 三重条件都成立且旧 collection 实际回读成功的子块才
        # 能跳过本轮 Embed；目录缺失、损坏或缺少其中一条向量时，单个子块安全回退到新嵌入。
        fresh_rows = [row for row in rows if str(row["child_chunk_id"]) not in reused_embeddings]
        embeddings = embed_local_texts(
            [str(row["content"]) for row in fresh_rows],
            allow_download=False,
        )
        if len(embeddings) != len(fresh_rows) or not all(embedding for embedding in embeddings):
            raise RuntimeError("Embedding 返回数量或维度不一致。")
        embeddings_by_child_id = dict(reused_embeddings)
        embeddings_by_child_id.update(
            {
                str(row["child_chunk_id"]): embedding
                for row, embedding in zip(fresh_rows, embeddings, strict=True)
            }
        )
        records = [
            VectorRecord(
                child_chunk_id=str(row["child_chunk_id"]),
                embedding=embeddings_by_child_id[str(row["child_chunk_id"])],
                knowledge_base_id=knowledge_base_id,
                document_version_id=str(row["document_version_id"]),
            )
            for row in rows
        ]
        if index.upsert(records) < len(records) or not index.verify(
            [record.child_chunk_id for record in records]
        ):
            raise RuntimeError("Chroma generation 回读验证失败。")
        return _VectorGenerationResult(
            mode="ready",
            indexed_child_count=len(records),
            reused_child_count=len(reused_embeddings),
            embedded_child_count=len(fresh_rows),
        )
    except Exception:
        try:
            index.remove_generation_directory()
        except OSError:
            # 原异常已经会降级；目录占用会由资料库删除或后续 generation 清理收束。
            pass
        return _VectorGenerationResult(
            mode="failed",
            failure_summary="本地语义索引构建失败，已保留关键词索引。",
        )
    finally:
        index.close()


def _read_reusable_generation_embeddings(
    *,
    knowledge_base_id: str,
    source_generation_number: int | None,
    candidate_child_chunk_ids: list[str],
) -> dict[str, list[float]]:
    """从上一活动 generation 只读获取候选向量；任何异常都退回本轮重新嵌入。

    这里刻意不把旧向量读取故障升级为整个索引失败：SQLite 事实与 FTS 新 generation 都没有
    被旧 Chroma 目录污染，重新嵌入仍能产出完整一致的目标 collection。该函数不复制目录，
    也不把向量写入 SQLite 或任务审计。
    """

    if source_generation_number is None or not candidate_child_chunk_ids:
        return {}
    source_index = ChromaGenerationIndex(
        knowledge_base_id=knowledge_base_id,
        generation_number=source_generation_number,
    )
    try:
        return source_index.read_embeddings(candidate_child_chunk_ids)
    except Exception:
        return {}
    finally:
        source_index.close()


def _discard_vector_generation(index_job_id: str) -> None:
    """取消或 FTS 验证失败时删除尚未激活的 Chroma generation 目录。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT knowledge_base_id, target_generation_number FROM knowledge_index_jobs WHERE index_job_id = ?",
            (index_job_id,),
        ).fetchone()
    if row is None:
        return
    index = ChromaGenerationIndex(
        knowledge_base_id=str(row["knowledge_base_id"]),
        generation_number=int(row["target_generation_number"]),
    )
    try:
        index.remove_generation_directory()
    except OSError:
        return


def _generation_activation_summary(vector_mode: str) -> str:
    if vector_mode == "ready":
        return "本地关键词和语义索引均已验证，并切换为活动资料版本。"
    if vector_mode == "failed":
        return "本地关键词索引已验证；语义索引构建失败，当前明确降级为关键词检索。"
    return "本地关键词索引已验证并切换为活动资料版本；语义索引仍待本地模型准备。"


def _finish_without_index(index_job_id: str, reason: str) -> KnowledgeIndexJobRecord:
    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
        if job is None:
            connection.rollback()
            raise KnowledgeIndexJobNotFoundError("未找到指定知识库索引任务。")
        if str(job["status"]) != "running":
            connection.rollback()
            return _row_to_job(job)
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET status = 'failed', stage = 'failed', failure_summary = ?, updated_at = ?, completed_at = ?
            WHERE index_job_id = ?
            """,
            (reason, now, now, index_job_id),
        )
        connection.execute(
            """
            UPDATE knowledge_index_generations
            SET status = 'failed', failure_summary = ? WHERE index_generation_id = ?
            """,
            (reason, str(job["index_generation_id"])),
        )
        _restore_base_after_failed_generation(connection, job, now)
        connection.commit()
        failed = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
    if failed is None:  # pragma: no cover
        raise RuntimeError("失败知识库索引任务无法回读。")
    return _row_to_job(failed)


def _restore_base_after_failed_generation(
    connection: sqlite3.Connection,
    job: sqlite3.Row,
    now: str,
) -> None:
    base = connection.execute(
        "SELECT status, active_index_generation FROM knowledge_bases WHERE knowledge_base_id = ?",
        (str(job["knowledge_base_id"]),),
    ).fetchone()
    # 删除请求已经撤销了活动 generation。失败/取消索引只能收束自身候选，不能把资料库
    # 从 deleting 或 deleted 状态重新标回可用。
    if base is None or str(base["status"]) in {"deleting", "deleted"}:
        return
    next_status = "partial_failure" if base is not None and int(base["active_index_generation"]) else "failed"
    connection.execute(
        "UPDATE knowledge_bases SET status = ?, updated_at = ? WHERE knowledge_base_id = ?",
        (next_status, now, str(job["knowledge_base_id"])),
    )


def _cancel_index_job_if_requested(index_job_id: str) -> KnowledgeIndexJobRecord | None:
    """把删除中的 running 任务收束为 cancelled，不激活任何候选 generation。"""

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
        if job is None:
            connection.rollback()
            raise KnowledgeIndexJobNotFoundError("未找到指定知识库索引任务。")
        if str(job["status"]) != "running" or not int(job["cancel_requested"]):
            connection.rollback()
            return None
        reason = "资料库正在删除，已停止未完成的索引任务。"
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET status = 'cancelled', stage = 'cancelled', failure_summary = ?, updated_at = ?, completed_at = ?
            WHERE index_job_id = ?
            """,
            (reason, now, now, index_job_id),
        )
        connection.execute(
            """
            UPDATE knowledge_index_generations
            SET status = 'failed', failure_summary = ?
            WHERE index_generation_id = ? AND status = 'building'
            """,
            (reason, str(job["index_generation_id"])),
        )
        _restore_base_after_failed_generation(connection, job, now)
        _append_audit_event(
            connection,
            knowledge_base_id=str(job["knowledge_base_id"]),
            event_type="knowledge_index_job_cancelled",
            summary="资料库删除请求已停止未完成的索引任务。",
            details={"index_job_id": index_job_id},
            created_at=now,
        )
        connection.commit()
        cancelled = connection.execute(
            "SELECT * FROM knowledge_index_jobs WHERE index_job_id = ?", (index_job_id,)
        ).fetchone()
    if cancelled is None:  # pragma: no cover - 提交后回读不应为空。
        raise RuntimeError("已取消知识库索引任务无法回读。")
    return _row_to_job(cancelled)


def _cjk_shadow(text: str) -> str:
    """生成 FTS 专用中文二元词影子字段，不改变原始块内容或来源范围。"""

    terms: list[str] = []
    for sequence in _CHINESE_SEQUENCE.findall(text):
        terms.extend(sequence)
        terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return " ".join(terms)


def _row_to_job(row: sqlite3.Row) -> KnowledgeIndexJobRecord:
    return KnowledgeIndexJobRecord(
        index_job_id=str(row["index_job_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        target_generation_number=int(row["target_generation_number"]),
        status=str(row["status"]),
        stage=str(row["stage"]),
        total_document_count=int(row["total_document_count"]),
        parsed_document_count=int(row["parsed_document_count"]),
        indexed_document_count=int(row["indexed_document_count"]),
        failed_document_count=int(row["failed_document_count"]),
        reused_parsed_document_count=int(row["reused_parsed_document_count"]),
        parse_and_chunk_elapsed_ms=int(row["parse_and_chunk_elapsed_ms"]),
        vector_index_elapsed_ms=int(row["vector_index_elapsed_ms"]),
        keyword_index_elapsed_ms=int(row["keyword_index_elapsed_ms"]),
        total_elapsed_ms=int(row["total_elapsed_ms"]),
        vector_indexed_child_count=int(row["vector_indexed_child_count"]),
        reused_vector_child_count=int(row["reused_vector_child_count"]),
        embedded_child_count=int(row["embedded_child_count"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        failure_summaries=[str(row["failure_summary"])] if str(row["failure_summary"]) else [],
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
