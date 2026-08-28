"""知识库 K1.2 的 SQLite 事实仓储。

当前管理 Index Profile、资料库元数据、受控副本版本和脱敏审计。分块、FTS 与 Chroma
都尚未接入，不能因为文件已复制就把资料库对外标成“可检索”。
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import sqlite3
import shutil
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4

from app.database.sqlite import get_connection
from app.core.config import settings
from app.schemas.knowledge import (
    KnowledgeBaseRecord,
    KnowledgeDocumentImportItem,
    KnowledgeDocumentImportResponse,
    KnowledgeDocumentRecord,
    KnowledgeDocumentVersionRecord,
    KnowledgeIndexProfile,
)
from app.services.workspace_documents import (
    BINARY_DOCUMENT_SUFFIXES,
    TEXT_DOCUMENT_SUFFIXES,
    WorkspaceDocumentError,
    parse_controlled_document,
    resolve_workspace_document_path,
)
from app.services.knowledge_chunking import (
    SPLITTER_PROFILE_VERSION,
    ParentChildChunkDrafts,
    build_parent_child_chunks,
)


DEFAULT_KNOWLEDGE_INDEX_PROFILE = KnowledgeIndexProfile(
    profile_id="kb_profile_local_hybrid_v1",
    keyword_profile_version="fts5_cjk_v1",
    splitter_profile_version="parent_child_v1",
    embedding_profile_version="fastembed_bge_small_zh_v1",
)


class KnowledgeBaseNotFoundError(LookupError):
    """资料库不存在，或已经不应在客户列表中显示。"""


class KnowledgeBaseConflictError(ValueError):
    """资料库名称或不可变 Profile 标识与既有事实冲突。"""


class KnowledgeBaseUnavailableError(ValueError):
    """资料库正删除或已删除，不能接受新的受控材料。"""


class KnowledgeBaseDeletionPendingError(KnowledgeBaseUnavailableError):
    """删除已受理，但仍在等待索引任务停驻或文件句柄释放。"""


PARSER_PROFILE_VERSION = "workspace_parser_v2_ocr_page_retry"
_KNOWLEDGE_BASE_NAME_LIMIT = 80


def _deleted_knowledge_base_name(name: str, knowledge_base_id: str) -> str:
    """为软删除记录保留可审计的唯一标签，同时释放客户原先使用的资料库名称。"""

    suffix = f" [deleted-{knowledge_base_id[-8:]}]"
    return f"{name[: max(1, _KNOWLEDGE_BASE_NAME_LIMIT - len(suffix))]}{suffix}"


def _release_deleted_knowledge_base_name(
    connection: sqlite3.Connection,
    *,
    name: str,
    updated_at: str,
) -> None:
    """迁移前遗留的 deleted 同名记录也应在下一次创建时释放名称。"""

    rows = connection.execute(
        """
        SELECT knowledge_base_id, name
        FROM knowledge_bases
        WHERE name = ? COLLATE NOCASE AND status = 'deleted'
        """,
        (name,),
    ).fetchall()
    for row in rows:
        connection.execute(
            """
            UPDATE knowledge_bases
            SET name = ?, updated_at = ?
            WHERE knowledge_base_id = ? AND status = 'deleted'
            """,
            (
                _deleted_knowledge_base_name(str(row["name"]), str(row["knowledge_base_id"])),
                updated_at,
                str(row["knowledge_base_id"]),
            ),
        )


def ensure_default_knowledge_index_profile() -> KnowledgeIndexProfile:
    """幂等写入 K1 默认 Profile，并防止同 ID 被静默改成另一套算法。

    这里仅登记 K0 已验证的 Chroma/FastEmbed 组合，不下载模型或创建向量目录。真正的
    可用性会在后续 Indexer 启动前单独诊断，避免“新建空资料库”变成昂贵隐式下载。
    """

    profile = DEFAULT_KNOWLEDGE_INDEX_PROFILE
    with get_connection() as connection:
        _ensure_profile(connection, profile)
    return profile


def create_knowledge_base(*, name: str, description: str = "") -> KnowledgeBaseRecord:
    """创建空资料库及其创建审计；不导入文件、不触发索引任务。"""

    now = _utc_now()
    record = KnowledgeBaseRecord(
        knowledge_base_id=f"kb_{uuid4().hex[:12]}",
        name=name.strip(),
        description=description.strip(),
        status="empty",
        default_index_profile_id=DEFAULT_KNOWLEDGE_INDEX_PROFILE.profile_id,
        active_index_generation=0,
        active_document_version_count=0,
        created_at=now,
        updated_at=now,
    )
    with get_connection() as connection:
        _ensure_profile(connection, DEFAULT_KNOWLEDGE_INDEX_PROFILE)
        # 删除完成后的资料库仅保留脱敏审计事实，不应永久占用客户看不见的名称。这里同时
        # 修复本次版本之前留下的 tombstone；deleting 仍不可复用，避免两条清理链争用同名。
        _release_deleted_knowledge_base_name(connection, name=record.name, updated_at=now)
        existing = connection.execute(
            "SELECT status FROM knowledge_bases WHERE name = ? COLLATE NOCASE",
            (record.name,),
        ).fetchone()
        if existing is not None:
            if str(existing["status"]) == "deleting":
                raise KnowledgeBaseConflictError("同名资料库正在删除，请等待删除完成后再新建。")
            raise KnowledgeBaseConflictError("已存在同名资料库，请换一个名称。")
        try:
            connection.execute(
                """
                INSERT INTO knowledge_bases (
                    knowledge_base_id, name, description, status, default_index_profile_id,
                    active_index_generation, active_document_version_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.knowledge_base_id,
                    record.name,
                    record.description,
                    record.status,
                    record.default_index_profile_id,
                    record.active_index_generation,
                    record.active_document_version_count,
                    record.created_at,
                    record.updated_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # SQLite 的 UNIQUE 约束以 NOCASE 比较资料库名称；提前把底层约束翻译成可理解的
            # 产品错误，API 层后续不必把数据库错误直接展示给客户。
            if "knowledge_bases.name" in str(exc):
                raise KnowledgeBaseConflictError("已存在同名资料库，请换一个名称。") from exc
            raise
        _append_audit_event(
            connection,
            knowledge_base_id=record.knowledge_base_id,
            event_type="knowledge_base_created",
            summary="已创建空资料库，尚未导入任何材料。",
            details={"index_profile_id": record.default_index_profile_id},
            created_at=now,
        )
    return record


def get_knowledge_base(knowledge_base_id: str, *, include_deleted: bool = False) -> KnowledgeBaseRecord:
    """读取单个资料库；默认不把删除中的历史记录当成可用资料库返回。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?",
            (knowledge_base_id,),
        ).fetchone()
    if row is None or (not include_deleted and str(row["status"]) == "deleted"):
        raise KnowledgeBaseNotFoundError("未找到指定资料库。")
    return _row_to_knowledge_base(row)


def list_knowledge_bases(*, include_deleted: bool = False, limit: int = 100) -> list[KnowledgeBaseRecord]:
    """列出资料库元数据，不包含文档名、正文、索引对象或物理路径。"""

    clauses = [] if include_deleted else ["status != 'deleted'"]
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM knowledge_bases"
            f"{where} ORDER BY updated_at DESC, created_at DESC, knowledge_base_id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [_row_to_knowledge_base(row) for row in rows]


def request_knowledge_base_deletion(knowledge_base_id: str) -> KnowledgeBaseRecord:
    """先撤销资料库活动指针并请求停止索引，返回 ``deleting`` 状态。

    这里刻意不直接删文件：正在解析的 PDF/DOCX 仍可能持有副本句柄。后续后台清理器只有在
    所有 Index Job 已停驻后才会删除知识库私有目录和派生索引，workspace 原文件从不触碰。
    """

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
        ).fetchone()
        if row is None or str(row["status"]) == "deleted":
            connection.rollback()
            raise KnowledgeBaseNotFoundError("未找到指定资料库。")
        if str(row["status"]) == "deleting":
            connection.rollback()
            return _row_to_knowledge_base(row)

        # 先让所有新请求失去活动 generation，随后才取消 job。即使进程在此后意外退出，恢复
        # 逻辑也不会再把旧来源当成可查询资料返回。
        connection.execute(
            """
            UPDATE knowledge_bases
            SET status = 'deleting', active_index_generation = 0, active_document_version_count = 0, updated_at = ?
            WHERE knowledge_base_id = ?
            """,
            (now, knowledge_base_id),
        )
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET cancel_requested = 1, updated_at = ?
            WHERE knowledge_base_id = ? AND status = 'running'
            """,
            (now, knowledge_base_id),
        )
        connection.execute(
            """
            UPDATE knowledge_index_jobs
            SET status = 'cancelled', stage = 'cancelled', cancel_requested = 1,
                failure_summary = '资料库正在删除，未开始的索引任务已取消。', updated_at = ?, completed_at = ?
            WHERE knowledge_base_id = ? AND status = 'queued'
            """,
            (now, now, knowledge_base_id),
        )
        connection.execute(
            """
            UPDATE knowledge_index_generations
            SET status = 'failed', failure_summary = '资料库正在删除，候选索引已失效。'
            WHERE knowledge_base_id = ? AND status = 'building'
            """,
            (knowledge_base_id,),
        )
        _append_audit_event(
            connection,
            knowledge_base_id=knowledge_base_id,
            event_type="knowledge_base_deletion_requested",
            summary="已撤销活动资料版本，正在清理知识库私有副本和索引。",
            details={},
            created_at=now,
        )
        connection.commit()
        deleting = connection.execute(
            "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
        ).fetchone()
    if deleting is None:  # pragma: no cover - 同事务更新后回读不应为空。
        raise RuntimeError("删除中的资料库无法回读。")
    return _row_to_knowledge_base(deleting)


def finalize_knowledge_base_deletion(knowledge_base_id: str) -> KnowledgeBaseRecord:
    """清理一个已进入 ``deleting`` 的资料库，成功后仅保留脱敏审计事实。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
        ).fetchone()
        if row is None or str(row["status"]) == "deleted":
            if row is None:
                raise KnowledgeBaseNotFoundError("未找到指定资料库。")
            return _row_to_knowledge_base(row)
        if str(row["status"]) != "deleting":
            raise KnowledgeBaseUnavailableError("资料库尚未进入删除流程。")
        running = connection.execute(
            "SELECT COUNT(*) AS total FROM knowledge_index_jobs WHERE knowledge_base_id = ? AND status = 'running'",
            (knowledge_base_id,),
        ).fetchone()
    if running is not None and int(running["total"]):
        raise KnowledgeBaseDeletionPendingError("正在等待索引任务安全停止后继续清理。")

    try:
        _remove_knowledge_base_private_directories(knowledge_base_id)
    except OSError:
        _record_deletion_cleanup_deferred(knowledge_base_id)
        raise KnowledgeBaseDeletionPendingError(
            "本地资料仍被占用，关闭可能正在读取该资料的程序后会自动重试清理。"
        ) from None

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
        ).fetchone()
        if row is None:
            connection.rollback()
            raise KnowledgeBaseNotFoundError("未找到指定资料库。")
        if str(row["status"]) == "deleted":
            connection.rollback()
            return _row_to_knowledge_base(row)
        running = connection.execute(
            "SELECT COUNT(*) AS total FROM knowledge_index_jobs WHERE knowledge_base_id = ? AND status = 'running'",
            (knowledge_base_id,),
        ).fetchone()
        if running is not None and int(running["total"]):
            connection.rollback()
            raise KnowledgeBaseDeletionPendingError("正在等待索引任务安全停止后继续清理。")
        document_count = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM knowledge_documents WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()["total"]
        )
        generation_count = int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM knowledge_index_generations WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()["total"]
        )

        # FTS 没有外键，必须显式失效；余下业务表按外键顺序删除，审计表则永远保留。
        connection.execute("DELETE FROM knowledge_child_chunks_fts WHERE knowledge_base_id = ?", (knowledge_base_id,))
        connection.execute("DELETE FROM knowledge_index_jobs WHERE knowledge_base_id = ?", (knowledge_base_id,))
        connection.execute("DELETE FROM knowledge_index_generations WHERE knowledge_base_id = ?", (knowledge_base_id,))
        connection.execute("DELETE FROM knowledge_documents WHERE knowledge_base_id = ?", (knowledge_base_id,))
        connection.execute(
            """
            UPDATE knowledge_bases
            SET name = ?, status = 'deleted', active_index_generation = 0,
                active_document_version_count = 0, updated_at = ?
            WHERE knowledge_base_id = ?
            """,
            (_deleted_knowledge_base_name(str(row["name"]), knowledge_base_id), now, knowledge_base_id),
        )
        _append_audit_event(
            connection,
            knowledge_base_id=knowledge_base_id,
            event_type="knowledge_base_deleted",
            summary="知识库私有副本、版本和派生索引已清理完成。",
            details={"document_count": document_count, "generation_count": generation_count},
            created_at=now,
        )
        connection.commit()
        deleted = connection.execute(
            "SELECT * FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
        ).fetchone()
    if deleted is None:  # pragma: no cover - 删除使用软状态，记录不应消失。
        raise RuntimeError("已删除资料库无法回读。")
    return _row_to_knowledge_base(deleted)


def recover_pending_knowledge_base_deletions() -> list[str]:
    """启动时续办未完成清理；仍被占用的资料保持 deleting，等待下一次恢复。"""

    with get_connection() as connection:
        rows = connection.execute(
            "SELECT knowledge_base_id FROM knowledge_bases WHERE status = 'deleting'"
        ).fetchall()
    completed: list[str] = []
    for row in rows:
        knowledge_base_id = str(row["knowledge_base_id"])
        try:
            finalize_knowledge_base_deletion(knowledge_base_id)
        except KnowledgeBaseDeletionPendingError:
            continue
        completed.append(knowledge_base_id)
    return completed


def list_knowledge_documents(knowledge_base_id: str) -> list[KnowledgeDocumentRecord]:
    """返回逻辑文档列表；没有活动索引版本的候选资料仍可显示为待处理。"""

    # 先确认资料库仍存在，避免把空列表误解释成客户选错资料库或系统尚未写入。
    get_knowledge_base(knowledge_base_id)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT document.*,
                   version.status AS active_version_status,
                   COALESCE(version.ocr_page_count, 0) AS active_ocr_page_count,
                   COALESCE(version.ocr_completed_page_count, 0) AS active_ocr_completed_page_count,
                   COALESCE(version.ocr_failed_page_count, 0) AS active_ocr_failed_page_count,
                   COALESCE(version.ocr_retried_page_count, 0) AS active_ocr_retried_page_count,
                   COALESCE(version.failure_summary, '') AS active_failure_summary
            FROM knowledge_documents AS document
            LEFT JOIN knowledge_document_versions AS version
                ON version.document_version_id = document.active_version_id
            WHERE document.knowledge_base_id = ?
            ORDER BY document.updated_at DESC, document.created_at DESC, document.document_id DESC
            """,
            (knowledge_base_id,),
        ).fetchall()
    return [_row_to_document(row) for row in rows]


def list_knowledge_document_versions(document_id: str) -> list[KnowledgeDocumentVersionRecord]:
    """返回一个逻辑文档的版本历史，供后续状态页和失败重试读取。"""

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM knowledge_document_versions
            WHERE document_id = ?
            ORDER BY version_number DESC
            """,
            (document_id,),
        ).fetchall()
    return [_row_to_document_version(row) for row in rows]


def import_workspace_documents_to_knowledge_base(
    *,
    knowledge_base_id: str,
    workspace_document_names: list[str],
) -> KnowledgeDocumentImportResponse:
    """复制已受控 workspace 文件并建立不可变候选版本。

    这个函数故意不解析正文、创建分块或调用索引。每份副本先写入知识库私有临时文件，随后
    与 SQLite 版本记录在同一个短事务边界内完成；复制失败或数据库冲突会清理新文件，原
    workspace 文件始终不被改动。
    """

    if not workspace_document_names:
        raise ValueError("至少选择一份已导入 workspace 的资料。")

    source_paths = [
        resolve_workspace_document_path(
            name,
            allowed_suffixes=TEXT_DOCUMENT_SUFFIXES | BINARY_DOCUMENT_SUFFIXES,
        )
        for name in workspace_document_names
    ]
    if len({path.name for path in source_paths}) != len(source_paths):
        raise ValueError("同一次导入不能重复选择同一份资料。")

    _prepare_knowledge_base_for_material_import(knowledge_base_id)
    items = [
        _import_one_workspace_document(knowledge_base_id=knowledge_base_id, source_path=path)
        for path in source_paths
    ]
    return KnowledgeDocumentImportResponse(knowledge_base_id=knowledge_base_id, items=items)


def _prepare_knowledge_base_for_material_import(knowledge_base_id: str) -> None:
    """确认材料可入库，并修复旧版本误写的“索引中”状态。

    真正的索引任务才拥有 ``indexing`` 状态。导入只产生 queued 版本；若没有活动 generation，
    则资料库应回到 ``empty``，让 Qt 明确展示“待建立索引”并保留客户的下一步操作。
    """

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        base = connection.execute(
            "SELECT status, active_index_generation FROM knowledge_bases WHERE knowledge_base_id = ?",
            (knowledge_base_id,),
        ).fetchone()
        if base is None or str(base["status"]) == "deleted":
            connection.rollback()
            raise KnowledgeBaseNotFoundError("未找到指定资料库。")
        if str(base["status"]) == "deleting":
            connection.rollback()
            raise KnowledgeBaseUnavailableError("资料库正在删除，不能导入新材料。")
        active_job = connection.execute(
            """
            SELECT 1 FROM knowledge_index_jobs
            WHERE knowledge_base_id = ? AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (knowledge_base_id,),
        ).fetchone()
        if active_job is not None:
            connection.rollback()
            raise KnowledgeBaseUnavailableError("资料库索引正在运行，请完成后再导入或修改材料。")
        if int(base["active_index_generation"]) == 0 and str(base["status"]) != "empty":
            connection.execute(
                "UPDATE knowledge_bases SET status = 'empty', updated_at = ? WHERE knowledge_base_id = ?",
                (now, knowledge_base_id),
            )
        connection.commit()


def _import_one_workspace_document(
    *,
    knowledge_base_id: str,
    source_path: Path,
) -> KnowledgeDocumentImportItem:
    """写入一份候选副本；每个文件独立短事务，便于未来单文件失败重试。"""

    document_type = _document_type_from_suffix(source_path.suffix)
    storage_ref = f"kb_store_{uuid4().hex[:16]}"
    target_path = _knowledge_storage_path(
        knowledge_base_id=knowledge_base_id,
        storage_ref=storage_ref,
        suffix=source_path.suffix,
    )
    temporary_path = target_path.with_suffix(f"{target_path.suffix}.part")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    committed = False
    try:
        source_sha256 = _copy_and_hash(source_path=source_path, target_path=temporary_path)
        now = _utc_now()
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            base_row = connection.execute(
                "SELECT status FROM knowledge_bases WHERE knowledge_base_id = ?",
                (knowledge_base_id,),
            ).fetchone()
            if base_row is None or str(base_row["status"]) in {"deleting", "deleted"}:
                raise KnowledgeBaseUnavailableError("资料库已不可用，导入已取消。")

            existing_document = connection.execute(
                """
                SELECT * FROM knowledge_documents
                WHERE knowledge_base_id = ? AND display_name = ?
                """,
                (knowledge_base_id, source_path.name),
            ).fetchone()
            if existing_document is None:
                document_id = f"kb_doc_{uuid4().hex[:12]}"
                version_number = 1
                connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        document_id, knowledge_base_id, display_name, document_type,
                        active_version_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        document_id,
                        knowledge_base_id,
                        source_path.name,
                        document_type,
                        now,
                        now,
                    ),
                )
                outcome = "created"
            else:
                document_id = str(existing_document["document_id"])
                duplicate_row = connection.execute(
                    """
                    SELECT * FROM knowledge_document_versions
                    WHERE document_id = ? AND source_sha256 = ?
                    """,
                    (document_id, source_sha256),
                ).fetchone()
                if duplicate_row is not None:
                    connection.rollback()
                    # 哈希相同表示数据库已有可复用版本；本次只产生了临时复制，不能让它
                    # 留在私有目录里伪装成另一个版本。
                    temporary_path.unlink(missing_ok=True)
                    return KnowledgeDocumentImportItem(
                        workspace_document_name=source_path.name,
                        outcome="duplicate",
                        document=_row_to_document(existing_document),
                        document_version=_row_to_document_version(duplicate_row),
                    )
                version_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version_number), 0) AS max_version "
                        "FROM knowledge_document_versions WHERE document_id = ?",
                        (document_id,),
                    ).fetchone()["max_version"]
                ) + 1
                # 活动 version 只能等后续 generation 验证后切换；这里仅替代尚未完成的候选，
                # 不会覆盖客户当前仍可查询的 ready 版本。
                connection.execute(
                    """
                    UPDATE knowledge_document_versions
                    SET status = 'superseded', updated_at = ?
                    WHERE document_id = ? AND status IN ('queued', 'parsing', 'parsed', 'indexing')
                    """,
                    (now, document_id),
                )
                connection.execute(
                    """
                    UPDATE knowledge_documents
                    SET document_type = ?, updated_at = ? WHERE document_id = ?
                    """,
                    (document_type, now, document_id),
                )
                outcome = "updated"

            version = KnowledgeDocumentVersionRecord(
                document_version_id=f"kb_ver_{uuid4().hex[:12]}",
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                version_number=version_number,
                storage_ref=storage_ref,
                source_sha256=source_sha256,
                document_type=document_type,
                parser_profile_version=PARSER_PROFILE_VERSION,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO knowledge_document_versions (
                    document_version_id, document_id, knowledge_base_id, version_number,
                    storage_ref, storage_suffix, source_sha256, document_type, parser_profile_version, status,
                    extracted_char_count, parent_chunk_count, child_chunk_count, failure_summary,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.document_version_id,
                    version.document_id,
                    version.knowledge_base_id,
                    version.version_number,
                    version.storage_ref,
                    source_path.suffix.lower(),
                    version.source_sha256,
                    version.document_type,
                    version.parser_profile_version,
                    version.status,
                    version.extracted_char_count,
                    version.parent_chunk_count,
                    version.child_chunk_count,
                    version.failure_summary,
                    version.created_at,
                    version.updated_at,
                ),
            )
            # 文件改名是单卷原子操作。失败则事务回滚并删除 .part；成功但 commit 异常也会在
            # 外层清理最终文件，避免数据库与受控副本留下半边状态。导入本身绝不把资料库标成
            # indexing：真正的索引状态只由 create_knowledge_index_job 写入。
            temporary_path.replace(target_path)
            _append_audit_event(
                connection,
                knowledge_base_id=knowledge_base_id,
                event_type="knowledge_document_version_queued",
                summary="已保存受控材料副本，等待解析与索引。",
                details={
                    "document_id": document_id,
                    "document_version_id": version.document_version_id,
                    "source_sha256": source_sha256,
                },
                created_at=now,
            )
            document_row = connection.execute(
                "SELECT * FROM knowledge_documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            if document_row is None:  # pragma: no cover - 同一事务写入后不应发生。
                raise RuntimeError("新建知识库文档无法回读。")
            connection.commit()
            committed = True
            return KnowledgeDocumentImportItem(
                workspace_document_name=source_path.name,
                outcome=outcome,
                document=_row_to_document(document_row),
                document_version=version,
            )
    except Exception:
        # Workspace 原件不会被触及；这里只有本次尚未登记或提交失败的私有候选文件可以清理。
        temporary_path.unlink(missing_ok=True)
        if not committed:
            target_path.unlink(missing_ok=True)
        raise


def parse_knowledge_document_version(
    document_version_id: str,
    *,
    on_ocr_started: Callable[[], None] | None = None,
) -> KnowledgeDocumentVersionRecord:
    """解析一个 queued 候选版本并持久化父子分块。

    K1.3 仍没有后台 job；该同步服务只为后续 Indexer 提供可重复调用的最小单版本边界。它
    只读取知识库私有副本，既不回读 workspace 原件，也不写入向量、FTS、日志正文或模型。
    """

    with get_connection() as connection:
        version_row = connection.execute(
            "SELECT * FROM knowledge_document_versions WHERE document_version_id = ?",
            (document_version_id,),
        ).fetchone()
        if version_row is None:
            raise KnowledgeBaseNotFoundError("未找到指定知识库文档版本。")
        status = str(version_row["status"])
        if status in {"parsed", "ready"}:
            return _row_to_document_version(version_row)
        if status not in {"queued", "failed"}:
            raise KnowledgeBaseUnavailableError("该文档版本当前不能重新解析。")
        connection.execute(
            """
            UPDATE knowledge_document_versions
            SET status = 'parsing', failure_summary = '', ocr_page_count = 0,
                ocr_completed_page_count = 0, ocr_failed_page_count = 0,
                ocr_retried_page_count = 0, updated_at = ?
            WHERE document_version_id = ?
            """,
            (_utc_now(), document_version_id),
        )

    try:
        source_path = _resolve_knowledge_version_path(version_row)
        parsed = parse_controlled_document(source_path, on_ocr_started=on_ocr_started)
        chunk_drafts = build_parent_child_chunks(parsed)
        if not chunk_drafts.parents or not chunk_drafts.children:
            raise WorkspaceDocumentError("文档没有可用于知识库索引的有效文本内容。")
    except (OSError, WorkspaceDocumentError, ValueError) as exc:
        return _mark_document_version_parse_failed(
            document_version_id=document_version_id,
            reason=_safe_parse_failure_summary(exc),
        )

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_row = connection.execute(
            "SELECT * FROM knowledge_document_versions WHERE document_version_id = ?",
            (document_version_id,),
        ).fetchone()
        if current_row is None:
            connection.rollback()
            raise KnowledgeBaseNotFoundError("文档版本在解析期间已被移除。")
        if str(current_row["status"]) != "parsing":
            connection.rollback()
            raise KnowledgeBaseUnavailableError("文档版本状态已变化，已停止写入分块。")
        _replace_document_version_chunks(
            connection,
            version_row=current_row,
            chunk_drafts=chunk_drafts,
            created_at=now,
        )
        partial_ocr_summary = _partial_ocr_summary(
            page_count=parsed.ocr_page_count,
            completed_page_count=parsed.ocr_completed_page_count,
            failed_page_count=parsed.ocr_failed_page_count,
            retried_page_count=parsed.ocr_retried_page_count,
        )
        connection.execute(
            """
            UPDATE knowledge_document_versions
            SET status = 'parsed', extracted_char_count = ?, parent_chunk_count = ?,
                child_chunk_count = ?, ocr_page_count = ?, ocr_completed_page_count = ?,
                ocr_failed_page_count = ?, ocr_retried_page_count = ?, failure_summary = ?, updated_at = ?
            WHERE document_version_id = ?
            """,
            (
                chunk_drafts.extracted_char_count,
                len(chunk_drafts.parents),
                len(chunk_drafts.children),
                parsed.ocr_page_count,
                parsed.ocr_completed_page_count,
                parsed.ocr_failed_page_count,
                parsed.ocr_retried_page_count,
                partial_ocr_summary,
                now,
                document_version_id,
            ),
        )
        _append_audit_event(
            connection,
            knowledge_base_id=str(current_row["knowledge_base_id"]),
            event_type="knowledge_document_version_parsed",
            summary="已完成受控材料解析与来源分块，等待索引。",
            details={
                "document_id": str(current_row["document_id"]),
                "document_version_id": document_version_id,
                "parent_chunk_count": len(chunk_drafts.parents),
                "child_chunk_count": len(chunk_drafts.children),
                "splitter_profile_version": SPLITTER_PROFILE_VERSION,
                "ocr_page_count": parsed.ocr_page_count,
                "ocr_completed_page_count": parsed.ocr_completed_page_count,
                "ocr_failed_page_count": parsed.ocr_failed_page_count,
                "ocr_retried_page_count": parsed.ocr_retried_page_count,
            },
            created_at=now,
        )
        connection.commit()
        updated_row = connection.execute(
            "SELECT * FROM knowledge_document_versions WHERE document_version_id = ?",
            (document_version_id,),
        ).fetchone()
    if updated_row is None:  # pragma: no cover - commit 后同连接回读不应为空。
        raise RuntimeError("解析后的知识库版本无法回读。")
    return _row_to_document_version(updated_row)


def _replace_document_version_chunks(
    connection: sqlite3.Connection,
    *,
    version_row: sqlite3.Row,
    chunk_drafts: ParentChildChunkDrafts,
    created_at: str,
) -> None:
    """在同一短事务替换一个候选版本的全部分块，防止部分写入被当作可索引资料。"""

    document_version_id = str(version_row["document_version_id"])
    connection.execute(
        "DELETE FROM knowledge_child_chunks WHERE document_version_id = ?",
        (document_version_id,),
    )
    connection.execute(
        "DELETE FROM knowledge_parent_chunks WHERE document_version_id = ?",
        (document_version_id,),
    )

    parent_ids = {
        parent.ordinal: f"kb_parent_{uuid4().hex[:16]}" for parent in chunk_drafts.parents
    }
    for parent in chunk_drafts.parents:
        connection.execute(
            """
            INSERT INTO knowledge_parent_chunks (
                parent_chunk_id, document_version_id, document_id, knowledge_base_id, ordinal,
                heading_path_json, source_kind, source_locator, start_char, end_char, content,
                content_sha256, splitter_profile_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parent_ids[parent.ordinal],
                document_version_id,
                str(version_row["document_id"]),
                str(version_row["knowledge_base_id"]),
                parent.ordinal,
                json.dumps(list(parent.heading_path), ensure_ascii=False, separators=(",", ":")),
                parent.source_kind,
                parent.source_locator,
                parent.start_char,
                parent.end_char,
                parent.content,
                parent.content_sha256,
                SPLITTER_PROFILE_VERSION,
                created_at,
            ),
        )

    child_ids = [f"kb_child_{uuid4().hex[:16]}" for _child in chunk_drafts.children]
    parent_by_range = [
        (parent.start_char, parent.end_char, parent_ids[parent.ordinal])
        for parent in chunk_drafts.parents
    ]
    for index, child in enumerate(chunk_drafts.children):
        parent_chunk_id = next(
            (
                parent_id
                for parent_start, parent_end, parent_id in parent_by_range
                if child.start_char >= parent_start and child.end_char <= parent_end
            ),
            None,
        )
        if parent_chunk_id is None:  # pragma: no cover - 分块器契约保证子块不会越出父块。
            raise RuntimeError("子块范围没有对应的父块。")
        connection.execute(
            """
            INSERT INTO knowledge_child_chunks (
                child_chunk_id, parent_chunk_id, document_version_id, document_id, knowledge_base_id,
                ordinal, previous_child_chunk_id, next_child_chunk_id, source_kind, source_locator,
                start_char, end_char, content, content_sha256, splitter_profile_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                child_ids[index],
                parent_chunk_id,
                document_version_id,
                str(version_row["document_id"]),
                str(version_row["knowledge_base_id"]),
                child.ordinal,
                child_ids[index - 1] if index > 0 else "",
                child_ids[index + 1] if index + 1 < len(child_ids) else "",
                child.source_kind,
                child.source_locator,
                child.start_char,
                child.end_char,
                child.content,
                child.content_sha256,
                SPLITTER_PROFILE_VERSION,
                created_at,
            ),
        )


def _mark_document_version_parse_failed(
    *,
    document_version_id: str,
    reason: str,
) -> KnowledgeDocumentVersionRecord:
    """只标记当前候选版本失败；旧 ready 版本和其来源不受这次解析失败影响。"""

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM knowledge_document_versions WHERE document_version_id = ?",
            (document_version_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise KnowledgeBaseNotFoundError("未找到指定知识库文档版本。")
        connection.execute(
            """
            UPDATE knowledge_document_versions
            SET status = 'failed', failure_summary = ?, ocr_page_count = 0,
                ocr_completed_page_count = 0, ocr_failed_page_count = 0,
                ocr_retried_page_count = 0, updated_at = ?
            WHERE document_version_id = ?
            """,
            (reason, now, document_version_id),
        )
        _append_audit_event(
            connection,
            knowledge_base_id=str(row["knowledge_base_id"]),
            event_type="knowledge_document_version_parse_failed",
            summary="受控材料解析失败，未写入任何索引或部分分块。",
            details={
                "document_id": str(row["document_id"]),
                "document_version_id": document_version_id,
            },
            created_at=now,
        )
        connection.commit()
        failed_row = connection.execute(
            "SELECT * FROM knowledge_document_versions WHERE document_version_id = ?",
            (document_version_id,),
        ).fetchone()
    if failed_row is None:  # pragma: no cover - commit 后同连接回读不应为空。
        raise RuntimeError("失败知识库版本无法回读。")
    return _row_to_document_version(failed_row)


def _resolve_knowledge_version_path(version_row: sqlite3.Row) -> Path:
    """从不透明 storage_ref 回读私有副本，兼容 K1.2 尚未保存后缀的候选记录。"""

    knowledge_base_id = str(version_row["knowledge_base_id"])
    storage_ref = str(version_row["storage_ref"])
    stored_suffix = str(version_row["storage_suffix"] or "").lower()
    if stored_suffix:
        candidate = _knowledge_storage_path(
            knowledge_base_id=knowledge_base_id,
            storage_ref=storage_ref,
            suffix=stored_suffix,
        )
        if candidate.is_file():
            return candidate
    sources_dir = (settings.knowledge_storage_dir / knowledge_base_id / "sources").resolve()
    try:
        sources_dir.relative_to(settings.knowledge_storage_dir)
    except ValueError as exc:  # pragma: no cover - 仅防御环境变量被异常配置的情况。
        raise RuntimeError("知识库受控副本目录越出了固定根目录。") from exc
    candidates = [
        path
        for path in sources_dir.glob(f"{storage_ref}.*")
        if path.is_file() and path.suffix.lower() in TEXT_DOCUMENT_SUFFIXES | BINARY_DOCUMENT_SUFFIXES
    ]
    if len(candidates) != 1:
        raise WorkspaceDocumentError("知识库受控副本缺失或格式不明确，无法解析。")
    return candidates[0]


def _safe_parse_failure_summary(error: Exception) -> str:
    """返回可理解但不含路径或正文的失败摘要，满足失败版本的稳定事实契约。"""

    message = str(error).replace("\r", " ").replace("\n", " ").strip()
    if not message:
        return "受控材料解析失败。"
    return f"解析失败：{message[:420]}"


def _partial_ocr_summary(
    *,
    page_count: int,
    completed_page_count: int,
    failed_page_count: int,
    retried_page_count: int,
) -> str:
    """把 OCR 部分完成压缩为客户可理解的版本事实，不记录页内文字或图像。"""

    if page_count < 1 or failed_page_count < 1:
        return ""
    retry_text = f"，已自动重试 {retried_page_count} 页一次" if retried_page_count else ""
    return (
        f"OCR 已识别 {completed_page_count}/{page_count} 页，"
        f"仍有 {failed_page_count} 页未识别{retry_text}；其它页面已可检索。"
    )[:500]


def _remove_knowledge_base_private_directories(knowledge_base_id: str) -> None:
    """删除资料库专属的受控副本和向量 generation 目录。

    两个根目录均由后端配置固定；ID 由服务端生成。仍做 ``relative_to`` 防御校验，保证删除
    代码永远不能因环境变量、调用方或路径拼接异常越出 AgentFlow 的私有数据目录。
    """

    for root in (settings.knowledge_storage_dir, settings.knowledge_vector_storage_dir):
        resolved_root = root.resolve()
        target = (resolved_root / knowledge_base_id).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError as exc:  # pragma: no cover - stable ID 正常情况下无法越界。
            raise RuntimeError("知识库私有清理路径越出了受控根目录。") from exc
        if target.exists():
            shutil.rmtree(target, ignore_errors=False)


def _record_deletion_cleanup_deferred(knowledge_base_id: str) -> None:
    """记录不含路径的清理延期事实，让下次启动可继续而不是静默遗留目录。"""

    now = _utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM knowledge_bases WHERE knowledge_base_id = ?", (knowledge_base_id,)
        ).fetchone()
        if row is not None and str(row["status"]) == "deleting":
            _append_audit_event(
                connection,
                knowledge_base_id=knowledge_base_id,
                event_type="knowledge_base_deletion_deferred",
                summary="知识库文件仍被占用，删除将在后续恢复流程中继续。",
                details={},
                created_at=now,
            )
        connection.commit()


def _knowledge_storage_path(*, knowledge_base_id: str, storage_ref: str, suffix: str) -> Path:
    """构造服务端私有副本路径，并确保永远在固定知识库根目录内。"""

    safe_suffix = suffix.lower()
    if safe_suffix not in TEXT_DOCUMENT_SUFFIXES | BINARY_DOCUMENT_SUFFIXES:
        raise WorkspaceDocumentError("当前不支持该知识库资料格式。")
    root = settings.knowledge_storage_dir
    target = (root / knowledge_base_id / "sources" / f"{storage_ref}{safe_suffix}").resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:  # pragma: no cover - ID 与 suffix 都由服务端生成，仍保留防御边界。
        raise RuntimeError("知识库受控副本路径越出了固定根目录。") from exc
    return target


def _copy_and_hash(*, source_path: Path, target_path: Path) -> str:
    """复制已验证 workspace 文件并计算实际副本哈希，避免源文件在中途变化造成错配。"""

    digest = sha256()
    with source_path.open("rb") as source, target_path.open("xb") as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
            digest.update(chunk)
        target.flush()
    return digest.hexdigest()


def _document_type_from_suffix(suffix: str) -> str:
    normalized = suffix.lower()
    if normalized in TEXT_DOCUMENT_SUFFIXES:
        return "text"
    if normalized == ".pdf":
        return "pdf"
    if normalized == ".docx":
        return "docx"
    if normalized in {".png", ".jpg", ".jpeg"}:
        # 逻辑类型记录原始材料形态；实际可检索正文仍只来自受控 OCR 解析与版本化分块。
        return "image"
    raise WorkspaceDocumentError("当前不支持该知识库资料格式。")


def _ensure_profile(connection: sqlite3.Connection, profile: KnowledgeIndexProfile) -> None:
    """在调用者事务内写入 profile，并对同 ID 的内容漂移 fail closed。"""

    existing = connection.execute(
        "SELECT profile_json FROM knowledge_index_profiles WHERE profile_id = ?",
        (profile.profile_id,),
    ).fetchone()
    profile_json = profile.model_dump_json()
    if existing is not None:
        if str(existing["profile_json"]) != profile_json:
            raise KnowledgeBaseConflictError(
                f"索引 Profile {profile.profile_id} 已存在但内容不一致，不能静默覆盖。"
            )
        return

    now = _utc_now()
    connection.execute(
        """
        INSERT INTO knowledge_index_profiles (
            profile_id, keyword_backend, keyword_profile_version, splitter_profile_version,
            vector_backend, embedding_provider, embedding_model, embedding_profile_version,
            rerank_mode, profile_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile.profile_id,
            profile.keyword_backend,
            profile.keyword_profile_version,
            profile.splitter_profile_version,
            profile.vector_backend,
            profile.embedding_provider,
            profile.embedding_model,
            profile.embedding_profile_version,
            profile.rerank_mode,
            profile_json,
            now,
            now,
        ),
    )


def _append_audit_event(
    connection: sqlite3.Connection,
    *,
    knowledge_base_id: str,
    event_type: str,
    summary: str,
    details: dict[str, object],
    created_at: str,
) -> None:
    """追加不含原文的审计事件；调用方只能提供稳定标识和短状态事实。"""

    connection.execute(
        """
        INSERT INTO knowledge_audit_events (
            event_id, knowledge_base_id, event_type, summary, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            f"kb_audit_{uuid4().hex[:12]}",
            knowledge_base_id,
            event_type,
            summary,
            json.dumps(details, ensure_ascii=False, separators=(",", ":")),
            created_at,
        ),
    )


def _row_to_knowledge_base(row: sqlite3.Row) -> KnowledgeBaseRecord:
    return KnowledgeBaseRecord(
        knowledge_base_id=str(row["knowledge_base_id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        status=str(row["status"]),
        default_index_profile_id=str(row["default_index_profile_id"]),
        active_index_generation=int(row["active_index_generation"]),
        active_document_version_count=int(row["active_document_version_count"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_document(row: sqlite3.Row) -> KnowledgeDocumentRecord:
    # 导入回执使用 ``knowledge_documents`` 的原始行，材料列表才带有活动版本 LEFT JOIN。
    # 因此可选列必须有零值兜底，不能让列表展示增强影响既有导入事务回读。
    columns = set(row.keys())
    return KnowledgeDocumentRecord(
        document_id=str(row["document_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        display_name=str(row["display_name"]),
        document_type=str(row["document_type"]),
        active_version_id=str(row["active_version_id"] or "") or None,
        active_version_status=(
            str(row["active_version_status"] or "") or None
            if "active_version_status" in columns
            else None
        ),
        active_ocr_page_count=int(row["active_ocr_page_count"]) if "active_ocr_page_count" in columns else 0,
        active_ocr_completed_page_count=(
            int(row["active_ocr_completed_page_count"])
            if "active_ocr_completed_page_count" in columns
            else 0
        ),
        active_ocr_failed_page_count=(
            int(row["active_ocr_failed_page_count"])
            if "active_ocr_failed_page_count" in columns
            else 0
        ),
        active_ocr_retried_page_count=(
            int(row["active_ocr_retried_page_count"])
            if "active_ocr_retried_page_count" in columns
            else 0
        ),
        active_failure_summary=(
            str(row["active_failure_summary"] or "") if "active_failure_summary" in columns else ""
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_document_version(row: sqlite3.Row) -> KnowledgeDocumentVersionRecord:
    return KnowledgeDocumentVersionRecord(
        document_version_id=str(row["document_version_id"]),
        document_id=str(row["document_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        version_number=int(row["version_number"]),
        storage_ref=str(row["storage_ref"]),
        source_sha256=str(row["source_sha256"]),
        document_type=str(row["document_type"]),
        parser_profile_version=str(row["parser_profile_version"]),
        status=str(row["status"]),
        extracted_char_count=int(row["extracted_char_count"]),
        parent_chunk_count=int(row["parent_chunk_count"]),
        child_chunk_count=int(row["child_chunk_count"]),
        ocr_page_count=int(row["ocr_page_count"]),
        ocr_completed_page_count=int(row["ocr_completed_page_count"]),
        ocr_failed_page_count=int(row["ocr_failed_page_count"]),
        ocr_retried_page_count=int(row["ocr_retried_page_count"]),
        failure_summary=str(row["failure_summary"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
