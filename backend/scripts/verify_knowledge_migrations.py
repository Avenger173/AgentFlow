"""知识库 K1.1-K1.4 SQLite migration 与事实仓储离线回归。

脚本在独立临时数据库中运行，不读取 workspace、客户资料或 .env 中的模型配置，不初始化
Chroma/FastEmbed，也不调用网络。它验证迁移幂等、checksum fail-closed 和资料库元数据
写入的事务边界，是 K1 后续接入文件副本前的基础回归。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_migration_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT)
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "knowledge_verify.db")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import (
    KnowledgeBaseConflictError,
    create_knowledge_base,
    ensure_default_knowledge_index_profile,
    get_knowledge_base,
    list_knowledge_bases,
)


def _close_connection(connection: sqlite3.Connection) -> None:
    """显式关闭验证连接，避免 Windows 在清理临时目录时留下 SQLite 句柄。"""

    connection.close()


def _expect_conflict(action, label: str) -> None:
    try:
        action()
    except KnowledgeBaseConflictError:
        return
    raise AssertionError(f"{label}: 预期资料库冲突，但意外通过。")


def _expect_checksum_failure(connection: sqlite3.Connection) -> None:
    migration_id = "20260821_knowledge_foundation_v1"
    connection.execute(
        "UPDATE schema_migrations SET checksum = ? WHERE migration_id = ?",
        ("tampered", migration_id),
    )
    connection.commit()
    try:
        sqlite_service._apply_schema_migrations(connection)
    except RuntimeError as exc:
        assert migration_id in str(exc)
        return
    raise AssertionError("tampered_checksum: 预期 migration checksum 校验拒绝，但意外通过。")


def main() -> None:
    try:
        # 首次连接触发完整 legacy 初始化和知识库 migration；第二次连接确认已应用 migration
        # 不会重复执行 DDL 或插入第二条 migration 记录。
        first_connection = sqlite_service.get_connection()
        try:
            table_names = {
                str(row["name"])
                for row in first_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            required_tables = {
                "schema_migrations",
                "knowledge_index_profiles",
                "knowledge_bases",
                "knowledge_audit_events",
                "knowledge_documents",
                "knowledge_document_versions",
                "knowledge_parent_chunks",
                "knowledge_child_chunks",
                "knowledge_index_generations",
                "knowledge_generation_documents",
                "knowledge_index_jobs",
                "knowledge_child_chunks_fts",
            }
            assert required_tables <= table_names
        finally:
            _close_connection(first_connection)

        # 模拟后端进程重新启动：清掉进程内“已初始化”缓存后必须再次执行 migration checksum
        # 校验，而不是只验证同一进程内的快捷返回。
        sqlite_service._INITIALIZED_PATHS.clear()
        second_connection = sqlite_service.get_connection()
        try:
            migration_rows = second_connection.execute(
                "SELECT migration_id, checksum FROM schema_migrations"
            ).fetchall()
            migration_ids = {str(row["migration_id"]) for row in migration_rows}
            assert migration_ids == {
                "20260821_knowledge_foundation_v1",
                "20260821_knowledge_document_versions_v1",
                "20260821_knowledge_chunks_v1",
                "20260821_knowledge_keyword_jobs_v1",
                "20260825_knowledge_index_job_metrics_v1",
                "20260825_knowledge_index_vector_reuse_metrics_v1",
                "20260825_knowledge_ocr_contract_v1",
                "20260825_knowledge_ocr_page_metrics_v1",
                "20260825_knowledge_ocr_job_stage_v1",
                "20260826_knowledge_import_state_repair_v1",
            }
        finally:
            _close_connection(second_connection)

        profile = ensure_default_knowledge_index_profile()
        created = create_knowledge_base(name="课程资料", description="仅用于迁移回归。")
        loaded = get_knowledge_base(created.knowledge_base_id)
        listed = list_knowledge_bases()
        assert loaded == created
        assert [item.knowledge_base_id for item in listed] == [created.knowledge_base_id]
        assert loaded.status == "empty"
        assert loaded.default_index_profile_id == profile.profile_id

        _expect_conflict(
            lambda: create_knowledge_base(name="课程资料", description="不应写入。"),
            "duplicate_name",
        )
        audit_connection = sqlite_service.get_connection()
        try:
            audit_rows = audit_connection.execute(
                "SELECT event_type, summary, details_json FROM knowledge_audit_events"
            ).fetchall()
            assert len(audit_rows) == 1
            assert str(audit_rows[0]["event_type"]) == "knowledge_base_created"
            assert "客户原文" not in str(audit_rows[0]["details_json"])
        finally:
            _close_connection(audit_connection)

        checksum_connection = sqlite_service.get_connection()
        try:
            _expect_checksum_failure(checksum_connection)
        finally:
            _close_connection(checksum_connection)

        print("Knowledge K1.1-K1.4 migration verification passed: migration, repository, audit, checksum.")
    finally:
        # 先释放 Repository 的短连接引用，再清理 WAL/SHM，避免验证自身在 Windows 留残留。
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
