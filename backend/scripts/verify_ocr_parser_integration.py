"""K7.3 OCR 与受控解析/知识库契约的离线回归。

脚本只在临时 data 目录构造最小图片和 PDF，并把 OCR 工厂替换为内存假实现。它不安装
Paddle、不加载模型、不联网，也不输出客户正文或绝对路径；验证的是 K7.3 的解析边界、来源
定位和 SQLite 契约能否真正承接 K7.2 Adapter 的结果。
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
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_ocr_parser_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT)
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "ocr_parser_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import fitz

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import (
    create_knowledge_base,
    import_workspace_documents_to_knowledge_base,
    parse_knowledge_document_version,
)
from app.services import workspace_documents
from app.services.ocr_adapter import (
    OcrAdapterError,
    OcrDocumentResult,
    OcrPageResult,
    OcrTextRegion,
)


class _FakeOcrAdapter:
    """固定返回页/区域结果，并模拟一次可恢复的临时页面失败。"""

    calls: list[str] = []

    def recognize_path(
        self,
        path: Path,
        *,
        page_numbers: tuple[int, ...] | None = None,
    ) -> OcrDocumentResult:
        self.calls.append(path.suffix.lower())
        if path.suffix.lower() == ".pdf":
            if page_numbers == (2,):
                return OcrDocumentResult(
                    document_type="pdf",
                    pages=(
                        OcrPageResult(
                            page_number=2,
                            status="completed",
                            text="扫描 PDF 第二页",
                            regions=(OcrTextRegion(1, "扫描 PDF 第二页", 0.97, None),),
                            confidence_average=0.97,
                        ),
                    ),
                )
            return OcrDocumentResult(
                document_type="pdf",
                pages=(
                    OcrPageResult(
                        page_number=1,
                        status="completed",
                        text="扫描 PDF 第一页",
                        regions=(
                            OcrTextRegion(1, "扫描 PDF 第一页", 0.98, None),
                        ),
                        confidence_average=0.98,
                    ),
                    # 第二页失败必须只丢弃该页，不撤回第一页已获得的受控文本。
                    OcrPageResult(2, "failed", "", (), None, "ocr_page_failed"),
                ),
            )
        return OcrDocumentResult(
            document_type="image",
            pages=(
                OcrPageResult(
                    page_number=1,
                    status="completed",
                    text="图片材料标题\n图片材料正文",
                    regions=(
                        OcrTextRegion(1, "图片材料标题", 0.99, None),
                        OcrTextRegion(2, "图片材料正文", 0.97, None),
                    ),
                    confidence_average=0.98,
                ),
            ),
        )


class _NotReadyOcrAdapter:
    """模拟 K7.2 的模型未准备状态，确保解析层不会尝试下载或吞掉错误。"""

    def recognize_path(
        self,
        path: Path,
        *,
        page_numbers: tuple[int, ...] | None = None,
    ) -> OcrDocumentResult:
        raise OcrAdapterError("ocr_not_ready", "本地 OCR 模型尚未准备。")


def _tiny_png_base64() -> str:
    """固定 1x1 PNG，足够验证受控二进制导入而无需图像处理额外依赖。"""

    return (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAusB9Y9JTTcAAAAASUVORK5CYII="
    )


def _two_page_image_only_pdf_base64() -> str:
    """生成无文本层两页 PDF，验证解析器只在整份 PDF 无文本时才转入 OCR。"""

    source_path = VERIFY_ROOT / "image_only.pdf"
    document = fitz.open()
    document.new_page(width=80, height=80)
    document.new_page(width=80, height=80)
    document.save(source_path)
    document.close()
    return base64.b64encode(source_path.read_bytes()).decode("ascii")


def _text_pdf_base64() -> str:
    """生成可提取文本 PDF，用于断言其不会调用可选 OCR。"""

    source_path = VERIFY_ROOT / "text_layer.pdf"
    document = fitz.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((24, 48), "Native PDF text")
    document.save(source_path)
    document.close()
    return base64.b64encode(source_path.read_bytes()).decode("ascii")


def _verify_legacy_schema_migration() -> None:
    """用带历史数据的最小旧库验证 K7 约束迁移不会破坏外键或 generation。"""

    database_path = VERIFY_ROOT / "legacy_knowledge_contract.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        # 这组最小表刻意保留 K7 前的 CHECK 枚举，覆盖真正客户已有数据库的升级路径。
        connection.executescript(
            """
            CREATE TABLE knowledge_bases (knowledge_base_id TEXT PRIMARY KEY);
            CREATE TABLE knowledge_index_generations (index_generation_id TEXT PRIMARY KEY);
            CREATE TABLE knowledge_documents (
                document_id TEXT PRIMARY KEY,
                knowledge_base_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                document_type TEXT NOT NULL CHECK (document_type IN ('text', 'pdf', 'docx')),
                active_version_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
            );
            CREATE TABLE knowledge_document_versions (
                document_version_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                knowledge_base_id TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                storage_ref TEXT NOT NULL UNIQUE,
                storage_suffix TEXT NOT NULL DEFAULT '',
                source_sha256 TEXT NOT NULL,
                document_type TEXT NOT NULL CHECK (document_type IN ('text', 'pdf', 'docx')),
                parser_profile_version TEXT NOT NULL,
                status TEXT NOT NULL,
                extracted_char_count INTEGER NOT NULL DEFAULT 0,
                parent_chunk_count INTEGER NOT NULL DEFAULT 0,
                child_chunk_count INTEGER NOT NULL DEFAULT 0,
                failure_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id),
                FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
            );
            CREATE TABLE knowledge_parent_chunks (
                parent_chunk_id TEXT PRIMARY KEY,
                document_version_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                knowledge_base_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                heading_path_json TEXT NOT NULL DEFAULT '[]',
                source_kind TEXT NOT NULL CHECK (source_kind IN ('line', 'page', 'paragraph', 'table', 'mixed')),
                source_locator TEXT NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                splitter_profile_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (document_version_id) REFERENCES knowledge_document_versions(document_version_id),
                FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id),
                FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
            );
            CREATE TABLE knowledge_child_chunks (
                child_chunk_id TEXT PRIMARY KEY,
                parent_chunk_id TEXT NOT NULL,
                document_version_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                knowledge_base_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                previous_child_chunk_id TEXT NOT NULL DEFAULT '',
                next_child_chunk_id TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL CHECK (source_kind IN ('line', 'page', 'paragraph', 'table', 'mixed')),
                source_locator TEXT NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                splitter_profile_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (parent_chunk_id) REFERENCES knowledge_parent_chunks(parent_chunk_id),
                FOREIGN KEY (document_version_id) REFERENCES knowledge_document_versions(document_version_id),
                FOREIGN KEY (document_id) REFERENCES knowledge_documents(document_id),
                FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(knowledge_base_id)
            );
            CREATE TABLE knowledge_generation_documents (
                index_generation_id TEXT NOT NULL,
                document_version_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                PRIMARY KEY (index_generation_id, document_version_id),
                FOREIGN KEY (index_generation_id) REFERENCES knowledge_index_generations(index_generation_id),
                FOREIGN KEY (document_version_id) REFERENCES knowledge_document_versions(document_version_id)
            );
            """
        )
        connection.execute("INSERT INTO knowledge_bases VALUES ('kb_legacy')")
        connection.execute("INSERT INTO knowledge_index_generations VALUES ('gen_legacy')")
        connection.execute(
            "INSERT INTO knowledge_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("doc_legacy", "kb_legacy", "历史材料.md", "text", "ver_legacy", "t0", "t0"),
        )
        connection.execute(
            "INSERT INTO knowledge_document_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ver_legacy", "doc_legacy", "kb_legacy", 1, "store_legacy", ".md", "a" * 64,
                "text", "parser-v1", "ready", 8, 1, 1, "", "t0", "t0",
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_parent_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "parent_legacy", "ver_legacy", "doc_legacy", "kb_legacy", 1, "[]", "line",
                "第 1 行", 0, 8, "历史正文", "b" * 64, "split-v1", "t0",
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_child_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "child_legacy", "parent_legacy", "ver_legacy", "doc_legacy", "kb_legacy", 1,
                "", "", "line", "第 1 行", 0, 8, "历史正文", "b" * 64, "split-v1", "t0",
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_generation_documents VALUES (?, ?, ?)",
            ("gen_legacy", "ver_legacy", 1),
        )
        connection.commit()

        # 这里复刻 migration runner 的受控窗口：关闭外键只为重建 CHECK 表，提交前仍会做全量
        # foreign_key_check。任何历史事实损坏都应使这次迁移拒绝提交。
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        sqlite_service._apply_knowledge_ocr_contract_v1(connection)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")

        assert connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_child_chunks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_generation_documents").fetchone()[0] == 1
        connection.execute(
            "INSERT INTO knowledge_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("doc_image", "kb_legacy", "扫描件.png", "image", "", "t1", "t1"),
        )
        connection.execute(
            "INSERT INTO knowledge_parent_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "parent_region", "ver_legacy", "doc_legacy", "kb_legacy", 2, "[]", "region",
                "第 1 页 · 区域 1", 0, 8, "区域文字", "c" * 64, "split-v1", "t1",
            ),
        )
        connection.commit()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()


def main() -> None:
    original_factory = workspace_documents._create_ocr_adapter
    workspace_documents._create_ocr_adapter = _FakeOcrAdapter
    try:
        _verify_legacy_schema_migration()
        image_info = workspace_documents.import_workspace_document_base64(
            filename="扫描材料.png",
            content_base64=_tiny_png_base64(),
        )
        assert image_info.document_type == "image"
        image_preview = workspace_documents.get_workspace_document_preview(
            relative_path=image_info.relative_path,
        )
        assert image_preview.document_type == "image"
        assert "图片材料标题" in image_preview.preview
        image_parsed = workspace_documents.parse_controlled_document(
            workspace_documents.resolve_workspace_document_path(image_info.relative_path)
        )
        source_kind, source_locator, _start, _end = workspace_documents.source_location_for_range(
            image_parsed,
            0,
            len(image_parsed.text),
        )
        assert source_kind == "region"
        assert "第 1 页 · 区域" in source_locator

        image_pdf_info = workspace_documents.import_workspace_document_base64(
            filename="扫描合同.pdf",
            content_base64=_two_page_image_only_pdf_base64(),
        )
        image_pdf_preview = workspace_documents.get_workspace_document_preview(
            relative_path=image_pdf_info.relative_path,
        )
        assert image_pdf_preview.document_type == "pdf"
        assert "扫描 PDF 第一页" in image_pdf_preview.preview
        assert "扫描 PDF 第二页" in image_pdf_preview.preview
        assert _FakeOcrAdapter.calls.count(".pdf") >= 2

        calls_before_text_pdf = len(_FakeOcrAdapter.calls)
        text_pdf_info = workspace_documents.import_workspace_document_base64(
            filename="可复制文本.pdf",
            content_base64=_text_pdf_base64(),
        )
        text_pdf_preview = workspace_documents.get_workspace_document_preview(
            relative_path=text_pdf_info.relative_path,
        )
        assert "Native PDF text" in text_pdf_preview.preview
        assert len(_FakeOcrAdapter.calls) == calls_before_text_pdf

        # 在知识库受控副本上再次走同一解析器，验证 image 类型与 region 锚点可以穿过版本/分块表。
        knowledge_base = create_knowledge_base(name="K7.3 OCR 解析回归")
        imported = import_workspace_documents_to_knowledge_base(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            workspace_document_names=[image_info.relative_path],
        ).items[0]
        parsed_version = parse_knowledge_document_version(
            imported.document_version.document_version_id
        )
        assert parsed_version.status == "parsed"
        assert parsed_version.document_type == "image"
        connection = sqlite_service.get_connection()
        try:
            source_kinds = {
                str(row["source_kind"])
                for row in connection.execute(
                    "SELECT source_kind FROM knowledge_child_chunks WHERE document_version_id = ?",
                    (parsed_version.document_version_id,),
                ).fetchall()
            }
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
        assert source_kinds == {"region"}
        assert not foreign_key_errors

        # 未准备时停在稳定且可行动的说明；导入后的源文件不被删除或改写。
        workspace_documents._create_ocr_adapter = _NotReadyOcrAdapter
        not_ready_info = workspace_documents.import_workspace_document_base64(
            filename="待识别.jpg",
            content_base64=_tiny_png_base64(),
        )
        try:
            workspace_documents.get_workspace_document_preview(
                relative_path=not_ready_info.relative_path,
            )
        except workspace_documents.WorkspaceDocumentError as exc:
            assert "本地模型尚未准备" in str(exc)
        else:  # pragma: no cover - 该分支代表解析器错误地把未准备模型当作成功。
            raise AssertionError("未准备 OCR 模型没有阻止图片解析。")
        assert workspace_documents.resolve_workspace_document_path(not_ready_info.relative_path).is_file()

        print(
            "K7.3 OCR parser verification passed: image/no-text-pdf/text-pdf-skip/"
            "region-anchor/knowledge-contract/legacy-migration/not-ready."
        )
    finally:
        workspace_documents._create_ocr_adapter = original_factory
        workspace_documents._parse_cache.clear()
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
