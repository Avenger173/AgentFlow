"""K7.4.2 OCR 页级状态、有限重试与知识库索引回归。

本脚本完全使用合成的无文本层 PDF 与假 OCR Adapter：不安装 Paddle、不加载模型、不下载权重、
不读取客户资料。它验证扫描件的成功页会进入索引，临时失败页只重试一次，持久失败页仍被如实
标为部分完成，并且索引任务曾进入可见的 ``ocr_recognizing`` 阶段。
"""

from __future__ import annotations

import base64
import gc
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_ocr_index_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT)
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "ocr_index_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import fitz

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import (
    create_knowledge_base,
    import_workspace_documents_to_knowledge_base,
    list_knowledge_documents,
    list_knowledge_document_versions,
)
from app.services import knowledge_keyword_index, workspace_documents
from app.services.ocr_adapter import OcrDocumentResult, OcrPageResult, OcrTextRegion


class _PartialPageOcrAdapter:
    """模拟第 2 页临时失败后恢复，第 3 页持续无文字的本地 OCR 结果。"""

    calls: list[tuple[str, tuple[int, ...] | None]] = []

    def recognize_path(
        self,
        path: Path,
        *,
        page_numbers: tuple[int, ...] | None = None,
    ) -> OcrDocumentResult:
        self.calls.append((path.suffix.lower(), page_numbers))
        if page_numbers == (2,):
            return OcrDocumentResult(
                document_type="pdf",
                pages=(
                    OcrPageResult(
                        page_number=2,
                        status="completed",
                        text="第二区域：巡检要求",
                        regions=(OcrTextRegion(1, "第二区域：巡检要求", 0.96, None),),
                        confidence_average=0.96,
                    ),
                ),
            )
        return OcrDocumentResult(
            document_type="pdf",
            pages=(
                OcrPageResult(
                    page_number=1,
                    status="completed",
                    text="第一区域：扫描材料摘要",
                    regions=(OcrTextRegion(1, "第一区域：扫描材料摘要", 0.98, None),),
                    confidence_average=0.98,
                ),
                OcrPageResult(2, "failed", "", (), None, "ocr_page_failed"),
                OcrPageResult(3, "failed", "", (), None, "ocr_no_text"),
            ),
        )


def _three_page_image_only_pdf_base64() -> str:
    """创建没有文本层的三页 PDF，确保解析路径真正需要 OCR。"""

    source_path = VERIFY_ROOT / "scan_material.pdf"
    document = fitz.open()
    for _ in range(3):
        document.new_page(width=100, height=100)
    document.save(source_path)
    document.close()
    return base64.b64encode(source_path.read_bytes()).decode("ascii")


def _verify_legacy_index_stage_migration() -> None:
    """确认旧任务表可原样升级，不因新增中间阶段丢失 generation 或性能事实。"""

    database_path = VERIFY_ROOT / "legacy_index_stage.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE knowledge_bases (knowledge_base_id TEXT PRIMARY KEY);
            CREATE TABLE knowledge_index_generations (
                index_generation_id TEXT PRIMARY KEY,
                knowledge_base_id TEXT NOT NULL,
                FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
            );
            CREATE TABLE knowledge_index_jobs (
                index_job_id TEXT PRIMARY KEY,
                knowledge_base_id TEXT NOT NULL,
                index_generation_id TEXT NOT NULL,
                target_generation_number INTEGER NOT NULL CHECK (target_generation_number >= 1),
                status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'partial_failure', 'failed', 'cancelled')),
                stage TEXT NOT NULL CHECK (stage IN ('queued', 'parsing', 'chunking', 'keyword_indexing', 'vector_indexing', 'verifying', 'activating', 'completed', 'partial_failure', 'failed', 'cancelled')),
                total_document_count INTEGER NOT NULL CHECK (total_document_count >= 1),
                parsed_document_count INTEGER NOT NULL DEFAULT 0,
                indexed_document_count INTEGER NOT NULL DEFAULT 0,
                failed_document_count INTEGER NOT NULL DEFAULT 0,
                reused_parsed_document_count INTEGER NOT NULL DEFAULT 0,
                parse_and_chunk_elapsed_ms INTEGER NOT NULL DEFAULT 0,
                vector_index_elapsed_ms INTEGER NOT NULL DEFAULT 0,
                keyword_index_elapsed_ms INTEGER NOT NULL DEFAULT 0,
                total_elapsed_ms INTEGER NOT NULL DEFAULT 0,
                vector_indexed_child_count INTEGER NOT NULL DEFAULT 0,
                reused_vector_child_count INTEGER NOT NULL DEFAULT 0,
                embedded_child_count INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                failure_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                UNIQUE (knowledge_base_id, target_generation_number),
                FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id),
                FOREIGN KEY (index_generation_id) REFERENCES knowledge_index_generations(index_generation_id)
            );
            """
        )
        connection.execute("INSERT INTO knowledge_bases VALUES ('kb_legacy')")
        connection.execute("INSERT INTO knowledge_index_generations VALUES ('gen_legacy', 'kb_legacy')")
        connection.execute(
            """
            INSERT INTO knowledge_index_jobs (
                index_job_id, knowledge_base_id, index_generation_id, target_generation_number,
                status, stage, total_document_count, failure_summary, created_at, updated_at
            ) VALUES ('job_legacy', 'kb_legacy', 'gen_legacy', 1, 'running', 'parsing', 2, 'old fact', 't0', 't0')
            """
        )
        connection.commit()

        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        sqlite_service._apply_knowledge_ocr_job_stage_v1(connection)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")

        connection.execute("UPDATE knowledge_index_jobs SET stage = 'ocr_recognizing'")
        upgraded = connection.execute(
            "SELECT stage, failure_summary FROM knowledge_index_jobs WHERE index_job_id = 'job_legacy'"
        ).fetchone()
        assert upgraded == ("ocr_recognizing", "old fact")
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


def main() -> None:
    original_factory = workspace_documents._create_ocr_adapter
    original_update_progress = knowledge_keyword_index._update_job_progress
    observed_stages: list[str] = []

    def track_progress(**kwargs: object) -> None:
        stage = kwargs.get("stage")
        if isinstance(stage, str):
            observed_stages.append(stage)
        original_update_progress(**kwargs)

    workspace_documents._create_ocr_adapter = _PartialPageOcrAdapter
    knowledge_keyword_index._update_job_progress = track_progress
    try:
        _verify_legacy_index_stage_migration()
        workspace_info = workspace_documents.import_workspace_document_base64(
            filename="扫描资料.pdf",
            content_base64=_three_page_image_only_pdf_base64(),
        )
        knowledge_base = create_knowledge_base(name="K7.4.2 OCR 索引回归")
        imported = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=[workspace_info.relative_path],
        ).items[0]

        job = knowledge_keyword_index.create_knowledge_index_job(knowledge_base.knowledge_base_id)
        completed = knowledge_keyword_index.run_knowledge_index_job(job.index_job_id)

        assert "ocr_recognizing" in observed_stages
        assert completed.status == "partial_failure"
        assert completed.stage == "partial_failure"
        assert completed.failed_document_count == 0
        assert any("OCR 有 1 份材料共 1 页未识别" in item for item in completed.failure_summaries)
        assert any(page_numbers == (2,) for _suffix, page_numbers in _PartialPageOcrAdapter.calls)

        version = list_knowledge_document_versions(imported.document.document_id)[0]
        assert version.status == "ready"
        assert version.ocr_page_count == 3
        assert version.ocr_completed_page_count == 2
        assert version.ocr_failed_page_count == 1
        assert version.ocr_retried_page_count == 1
        assert "其它页面已可检索" in version.failure_summary

        material = list_knowledge_documents(knowledge_base.knowledge_base_id)[0]
        assert material.active_version_status == "ready"
        assert material.active_ocr_page_count == 3
        assert material.active_ocr_completed_page_count == 2
        assert material.active_ocr_failed_page_count == 1
        assert "其它页面已可检索" in material.active_failure_summary

        connection = sqlite_service.get_connection()
        try:
            locators = [
                str(row["source_locator"])
                for row in connection.execute(
                    "SELECT source_locator FROM knowledge_child_chunks WHERE document_version_id = ?",
                    (version.document_version_id,),
                ).fetchall()
            ]
            audit_details = [
                str(row["details_json"])
                for row in connection.execute(
                    "SELECT details_json FROM knowledge_audit_events WHERE knowledge_base_id = ?",
                    (knowledge_base.knowledge_base_id,),
                ).fetchall()
            ]
        finally:
            connection.close()
        assert any("第 1 页 · 区域 1" in locator for locator in locators)
        assert any("第 2 页 · 区域 1" in locator for locator in locators)
        assert not any("第 3 页" in locator for locator in locators)
        assert all("扫描材料摘要" not in details for details in audit_details)

        print(
            "K7.4.2 OCR index verification passed: visible OCR stage/one-page retry/"
            "partial-page persistence/source anchors/no-content audit."
        )
    finally:
        knowledge_keyword_index._update_job_progress = original_update_progress
        workspace_documents._create_ocr_adapter = original_factory
        workspace_documents._parse_cache.clear()
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
