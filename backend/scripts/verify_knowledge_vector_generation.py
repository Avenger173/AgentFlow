"""知识库 K1 语义 generation 分支离线回归。

用确定性假向量替代 FastEmbed 权重，仍然写入并回读真实 Chroma PersistentClient。这样可验证
K1 的 generation 状态、关键词降级和目录清理边界，而不会下载模型或将任何客户材料送出本机。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_vector_generation_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_vector_generation_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.services import knowledge_keyword_index as keyword_index
from app.services.knowledge_vector_index import ChromaGenerationIndex
from app.services.workspace_documents import import_workspace_document, resolve_workspace_document_path


def _build_base(name: str, filename: str) -> str:
    knowledge_base = create_knowledge_base(name=name)
    import_workspace_document(
        filename=filename,
        content="# Local semantic check\n\nA generation must keep keyword and vector indexes isolated.\n",
    )
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=knowledge_base.knowledge_base_id,
        workspace_document_names=[filename],
    )
    return knowledge_base.knowledge_base_id


def _generation_modes(knowledge_base_id: str) -> tuple[str, str]:
    connection = sqlite_service.get_connection()
    try:
        row = connection.execute(
            """
            SELECT generation.vector_index_mode, job.status
            FROM knowledge_index_generations AS generation
            INNER JOIN knowledge_index_jobs AS job ON job.index_generation_id = generation.index_generation_id
            WHERE generation.knowledge_base_id = ?
            ORDER BY generation.generation_number DESC
            LIMIT 1
            """,
            (knowledge_base_id,),
        ).fetchone()
        assert row is not None
        return str(row["vector_index_mode"]), str(row["status"])
    finally:
        connection.close()


def main() -> None:
    original_capability = keyword_index.vector_index_capability
    original_embed = keyword_index.embed_local_texts
    try:
        keyword_index.vector_index_capability = lambda: SimpleNamespace(
            model_initialized=True,
            chroma_available=True,
            fastembed_available=True,
        )
        calls: list[tuple[bool, int]] = []

        def fake_embed(texts: list[str], *, allow_download: bool) -> list[list[float]]:
            # Index Job 必须显式禁止下载；假向量维度固定，便于 Chroma 真正持久化和回读。
            calls.append((allow_download, len(texts)))
            return [[float(index + 1), 0.5, 0.25] for index, _ in enumerate(texts)]

        keyword_index.embed_local_texts = fake_embed
        ready_base_id = _build_base("K1 vector ready", "vector_ready.md")
        ready_job = keyword_index.create_knowledge_index_job(ready_base_id)
        completed = keyword_index.run_knowledge_index_job(ready_job.index_job_id)
        assert completed.status == "completed"
        assert _generation_modes(ready_base_id) == ("ready", "completed")
        assert calls == [(False, 1)]
        assert (
            completed.vector_indexed_child_count,
            completed.reused_vector_child_count,
            completed.embedded_child_count,
        ) == (1, 0, 1)

        # 第二代同时包含旧的 ready 文档与新增文档。K5.6 只允许从同 Profile 的活动
        # generation 回读相同 child ID + 内容哈希的向量，因此旧材料可复用，新材料仍须嵌入。
        import_workspace_document(
            filename="vector_incremental.md",
            content="# Incremental semantic check\n\nReady versions must remain in the next vector generation.\n",
        )
        import_workspace_documents_to_knowledge_base(
            knowledge_base_id=ready_base_id,
            workspace_document_names=["vector_incremental.md"],
        )
        incremental_job = keyword_index.create_knowledge_index_job(ready_base_id)
        incremental = keyword_index.run_knowledge_index_job(incremental_job.index_job_id)
        assert incremental.status == "completed"
        assert _generation_modes(ready_base_id) == ("ready", "completed")
        assert calls == [(False, 1), (False, 1)]
        assert (
            incremental.vector_indexed_child_count,
            incremental.reused_vector_child_count,
            incremental.embedded_child_count,
        ) == (2, 1, 1)

        # 修改旧材料会产生新的受控文档版本和新的 child ID。即使资料库仍有两份材料，也只能
        # 复用未改的增量材料，更新版本必须重新嵌入，避免旧向量被误当成新正文的语义表示。
        resolve_workspace_document_path("vector_ready.md").write_text(
            "# Local semantic check\n\nThis revision must receive a fresh embedding.\n",
            encoding="utf-8",
        )
        import_workspace_documents_to_knowledge_base(
            knowledge_base_id=ready_base_id,
            workspace_document_names=["vector_ready.md"],
        )
        updated_job = keyword_index.create_knowledge_index_job(ready_base_id)
        updated = keyword_index.run_knowledge_index_job(updated_job.index_job_id)
        assert updated.status == "completed"
        assert calls == [(False, 1), (False, 1), (False, 1)]
        assert (
            updated.vector_indexed_child_count,
            updated.reused_vector_child_count,
            updated.embedded_child_count,
        ) == (2, 1, 1)

        # 旧活动目录被清理或损坏时，读取 Adapter 不得重建它、更不能让新 generation 读取空
        # collection 后误报命中。新增资料会让第 4 代把全部 3 个当前子块重新嵌入并自行验证。
        ChromaGenerationIndex(
            knowledge_base_id=ready_base_id,
            generation_number=3,
        ).remove_generation_directory()
        import_workspace_document(
            filename="vector_recovery.md",
            content="# Recovery check\n\nMissing source vectors must fall back to a complete fresh embedding.\n",
        )
        import_workspace_documents_to_knowledge_base(
            knowledge_base_id=ready_base_id,
            workspace_document_names=["vector_recovery.md"],
        )
        recovery_job = keyword_index.create_knowledge_index_job(ready_base_id)
        recovered = keyword_index.run_knowledge_index_job(recovery_job.index_job_id)
        assert recovered.status == "completed"
        assert calls == [(False, 1), (False, 1), (False, 1), (False, 3)]
        assert (
            recovered.vector_indexed_child_count,
            recovered.reused_vector_child_count,
            recovered.embedded_child_count,
        ) == (3, 0, 3)

        def fake_embedding_failure(texts: list[str], *, allow_download: bool) -> list[list[float]]:
            assert allow_download is False
            raise RuntimeError("offline fixture failure")

        keyword_index.embed_local_texts = fake_embedding_failure
        failed_base_id = _build_base("K1 vector fallback", "vector_fallback.md")
        failed_job = keyword_index.create_knowledge_index_job(failed_base_id)
        partial = keyword_index.run_knowledge_index_job(failed_job.index_job_id)
        assert partial.status == "partial_failure"
        assert _generation_modes(failed_base_id) == ("failed", "partial_failure")

        print("Knowledge K1 vector generation verification passed: Chroma ready path and keyword-only fallback.")
    finally:
        keyword_index.vector_index_capability = original_capability
        keyword_index.embed_local_texts = original_embed
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
