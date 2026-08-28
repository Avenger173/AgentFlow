"""知识库 K1.4 本地关键词 generation 与重启恢复离线回归。"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_keyword_jobs_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT)
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "knowledge_keyword_jobs_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.services import knowledge_keyword_index as keyword_index_service
from app.database.knowledge_repository import (
    create_knowledge_base,
    get_knowledge_base,
    import_workspace_documents_to_knowledge_base,
    list_knowledge_document_versions,
)
from app.services.knowledge_keyword_index import (
    create_knowledge_index_job,
    recover_interrupted_knowledge_index_jobs,
    run_knowledge_index_job,
)
from app.services.workspace_documents import import_workspace_document, resolve_workspace_document_path


def main() -> None:
    try:
        knowledge_base = create_knowledge_base(name="K1.4 索引任务回归")
        import_workspace_document(
            filename="制度.md",
            content="# 制度\n\n审批窗口为每周一上午，编号 AF-204。\n",
        )
        import_workspace_document(
            filename="验收.md",
            content="# 验收\n\n验收条件包括来源定位和失败可追溯。\n",
        )
        import_workspace_document(filename="空白.md", content="\n")
        imported = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=["制度.md", "验收.md", "空白.md"],
        )
        job = create_knowledge_index_job(knowledge_base.knowledge_base_id)
        assert job.status == "queued" and job.total_document_count == 3
        completed = run_knowledge_index_job(job.index_job_id)
        assert completed.status == "partial_failure"
        assert completed.stage == "partial_failure"
        assert completed.indexed_document_count == 2
        assert completed.failed_document_count == 1
        assert completed.failure_summaries
        assert completed.reused_parsed_document_count == 0
        assert completed.parse_and_chunk_elapsed_ms >= 0
        assert completed.vector_index_elapsed_ms >= 0
        assert completed.keyword_index_elapsed_ms >= 0
        assert completed.total_elapsed_ms >= completed.parse_and_chunk_elapsed_ms
        assert completed.vector_indexed_child_count == 0
        assert completed.reused_vector_child_count == 0
        assert completed.embedded_child_count == 0

        base_after_first_run = get_knowledge_base(knowledge_base.knowledge_base_id)
        assert base_after_first_run.status == "partial_failure"
        assert base_after_first_run.active_index_generation == 1
        assert base_after_first_run.active_document_version_count == 2
        connection = sqlite_service.get_connection()
        try:
            generation = connection.execute(
                """
                SELECT status, keyword_index_mode, vector_index_mode FROM knowledge_index_generations
                WHERE knowledge_base_id = ? AND generation_number = 1
                """,
                (knowledge_base.knowledge_base_id,),
            ).fetchone()
            assert str(generation["status"]) == "ready"
            assert str(generation["keyword_index_mode"]) == "fts5_cjk"
            assert str(generation["vector_index_mode"]) == "pending"
            fts_rows = connection.execute(
                """
                SELECT child_chunk_id FROM knowledge_child_chunks_fts
                WHERE knowledge_base_id = ? AND index_generation_id = (
                    SELECT index_generation_id FROM knowledge_index_generations
                    WHERE knowledge_base_id = ? AND generation_number = 1
                )
                """,
                (knowledge_base.knowledge_base_id, knowledge_base.knowledge_base_id),
            ).fetchall()
            assert len(fts_rows) >= 2
            # FTS 的原文和中文影子字段必须能回读，而不是只写了一个空 generation 标记。
            hits = connection.execute(
                """
                SELECT child_chunk_id FROM knowledge_child_chunks_fts
                WHERE knowledge_child_chunks_fts MATCH ? AND knowledge_base_id = ?
                """,
                ('"AF-204"', knowledge_base.knowledge_base_id),
            ).fetchall()
            assert hits
        finally:
            connection.close()

        ready_versions = [
            item
            for imported_item in imported.items
            for item in list_knowledge_document_versions(imported_item.document.document_id)
            if item.status == "ready"
        ]
        assert len(ready_versions) == 2

        # 新增一份独立材料后的成功重建必须复用上一代 ready 版本；此前该路径会把 ready
        # 误算成解析失败，直到 K3 的多资料比较回归才暴露出来。
        incremental_base = create_knowledge_base(name="K1.4 增量重建回归")
        import_workspace_document(filename="增量旧材料.md", content="# 旧材料\n\n编号 AF-301 需要来源定位。\n")
        import_workspace_documents_to_knowledge_base(
            knowledge_base_id=incremental_base.knowledge_base_id,
            workspace_document_names=["增量旧材料.md"],
        )
        first_incremental_job = create_knowledge_index_job(incremental_base.knowledge_base_id)
        assert run_knowledge_index_job(first_incremental_job.index_job_id).status == "completed"
        import_workspace_document(filename="增量新材料.md", content="# 新材料\n\n编号 AF-302 需要负责人确认。\n")
        import_workspace_documents_to_knowledge_base(
            knowledge_base_id=incremental_base.knowledge_base_id,
            workspace_document_names=["增量新材料.md"],
        )
        second_incremental_job = create_knowledge_index_job(incremental_base.knowledge_base_id)
        assert second_incremental_job.total_document_count == 2
        second_incremental_result = run_knowledge_index_job(second_incremental_job.index_job_id)
        assert second_incremental_result.status == "completed"
        assert second_incremental_result.parsed_document_count == 2
        assert second_incremental_result.indexed_document_count == 2
        assert second_incremental_result.failed_document_count == 0
        assert second_incremental_result.reused_parsed_document_count == 1
        assert second_incremental_result.total_elapsed_ms >= max(
            second_incremental_result.parse_and_chunk_elapsed_ms,
            second_incremental_result.vector_index_elapsed_ms,
            second_incremental_result.keyword_index_elapsed_ms,
        )
        assert get_knowledge_base(incremental_base.knowledge_base_id).active_index_generation == 2

        # 资料快照和 Profile 均未变化时，客户再次点击建立索引应明确复用已验证 generation，
        # 不创建第 3 代 FTS/向量目录，也不再次解析或嵌入全部材料。
        reused_index_job = create_knowledge_index_job(incremental_base.knowledge_base_id)
        assert reused_index_job.index_job_id == second_incremental_job.index_job_id
        assert reused_index_job.status == "completed"
        assert reused_index_job.target_generation_number == 2
        connection = sqlite_service.get_connection()
        try:
            generation_count = connection.execute(
                "SELECT COUNT(*) AS total FROM knowledge_index_generations WHERE knowledge_base_id = ?",
                (incremental_base.knowledge_base_id,),
            ).fetchone()
            assert int(generation_count["total"]) == 2
            reused_audits = connection.execute(
                """
                SELECT event_type FROM knowledge_audit_events
                WHERE knowledge_base_id = ? AND event_type = 'knowledge_index_job_reused'
                """,
                (incremental_base.knowledge_base_id,),
            ).fetchall()
            assert len(reused_audits) == 1
        finally:
            connection.close()

        # 旧 generation 的向量仍 pending 时，客户后来明确准备了本地模型，快路径必须失效，
        # 让下一代构建补齐语义索引。这里仅替换 capability 探针，不实例化或下载模型。
        original_vector_capability = keyword_index_service.vector_index_capability
        try:
            keyword_index_service.vector_index_capability = lambda: SimpleNamespace(model_initialized=True)
            vector_rebuild_job = create_knowledge_index_job(incremental_base.knowledge_base_id)
        finally:
            keyword_index_service.vector_index_capability = original_vector_capability
        assert vector_rebuild_job.status == "queued"
        assert vector_rebuild_job.target_generation_number == 3

        # 更新其中一份材料后，第二个 generation 必须仍带上未修改的活动材料；随后人为模拟
        # 进程在 running 阶段退出，恢复逻辑只能安全失败，不能重跑或破坏 generation 1。
        resolve_workspace_document_path("制度.md").write_text(
            "# 制度\n\n审批窗口改为每周二上午，编号 AF-205。\n", encoding="utf-8"
        )
        import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=["制度.md"],
        )
        second_job = create_knowledge_index_job(knowledge_base.knowledge_base_id)
        connection = sqlite_service.get_connection()
        try:
            connection.execute(
                "UPDATE knowledge_index_jobs SET status = 'running', stage = 'keyword_indexing' WHERE index_job_id = ?",
                (second_job.index_job_id,),
            )
            connection.commit()
        finally:
            connection.close()
        assert recover_interrupted_knowledge_index_jobs() == [second_job.index_job_id]
        recovered_base = get_knowledge_base(knowledge_base.knowledge_base_id)
        assert recovered_base.active_index_generation == 1
        connection = sqlite_service.get_connection()
        try:
            failed_job = connection.execute(
                "SELECT status, stage FROM knowledge_index_jobs WHERE index_job_id = ?",
                (second_job.index_job_id,),
            ).fetchone()
            assert (str(failed_job["status"]), str(failed_job["stage"])) == ("failed", "failed")
        finally:
            connection.close()

        print(
            "Knowledge K1.4 keyword job verification passed: generation, FTS, partial failure, "
            "restart recovery and index performance metrics."
        )
    finally:
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
