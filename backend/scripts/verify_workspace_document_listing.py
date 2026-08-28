"""验证 workspace 列表不会在页面初始化时解析 PDF/DOCX/OCR。"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_workspace_listing_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from app.services import workspace_documents


def main() -> None:
    try:
        text_document = workspace_documents.import_workspace_document(
            filename="quick-note.md",
            content="# 轻量材料\n\n列表不应解析正文。",
        )
        # 文本导入仍为客户的即时反馈保留短预览，不影响列表的无解析边界。
        assert "轻量材料" in text_document.preview

        binary_document = workspace_documents.import_workspace_document_base64(
            filename="slow-scan.pdf",
            content_base64=base64.b64encode(b"not-a-real-pdf").decode("ascii"),
        )
        assert binary_document.preview == ""

        original_reader = workspace_documents._read_workspace_document

        def fail_if_parsed(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("列表请求不应调用完整文档解析")

        workspace_documents._read_workspace_document = fail_if_parsed  # type: ignore[assignment]
        try:
            listed = workspace_documents.list_workspace_documents()
        finally:
            workspace_documents._read_workspace_document = original_reader

        by_name = {item.name: item for item in listed}
        assert set(by_name) == {"quick-note.md", "slow-scan.pdf"}
        assert by_name["quick-note.md"].preview == ""
        assert by_name["slow-scan.pdf"].preview == ""
        print("Workspace document listing performance boundary verification passed.")
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
