"""PDF 整理 Tool 的离线端到端回归。

脚本只在临时 data/output 目录中创建小型 PDF，不读取项目内客户文件、不调用 LLM，也不会
使用任何 API Key。它覆盖首发的合并、提取、旋转、删除和输出文件验证边界。
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

import fitz


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_pdf_processing_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(TEMP_ROOT / "data")
os.environ["AGENTFLOW_DOCUMENT_PROCESSING_OUTPUT_DIR"] = str(TEMP_ROOT / "outputs")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def _pdf_bytes(*pages: str) -> bytes:
    document = fitz.open()
    try:
        for text in pages:
            page = document.new_page()
            page.insert_text((72, 72), text)
        stream = BytesIO()
        document.save(stream)
        return stream.getvalue()
    finally:
        document.close()


def _import_pdf(client: TestClient, filename: str, content: bytes) -> str:
    response = client.post(
        "/api/workspace/documents",
        json={
            "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    )
    response.raise_for_status()
    return response.json()["relative_path"]


def _start_and_wait(client: TestClient, payload: dict[str, object]) -> dict[str, object]:
    response = client.post("/api/agents/document_agent/pdf-tools/start", json=payload)
    response.raise_for_status()
    task_id = response.json()["task_id"]
    for _attempt in range(80):
        result = client.get(f"/api/agents/document_agent/pdf-tools/{task_id}/result")
        result.raise_for_status()
        body = result.json()
        if body["status"] != "running" and body["status"] != "pending":
            assert body["status"] == "completed", body
            return body
        time.sleep(0.025)
    raise AssertionError(f"PDF task did not finish: {task_id}")


def _assert_pdf_artifact(result: dict[str, object], expected_pages: int) -> None:
    artifact = result["artifact"]
    assert artifact is not None, result
    metadata = artifact["metadata"]
    output_path = Path(metadata["output_path"])
    assert output_path.is_file(), output_path
    verification = result["verification"]
    assert verification["output_opened"] is True, verification
    assert verification["expected_page_count"] == expected_pages, verification
    assert verification["actual_page_count"] == expected_pages, verification
    # 回归不仅信任 API 元数据，还重新打开真实产物，防止以后误把空文件登记成成功。
    output = fitz.open(output_path)
    try:
        assert output.page_count == expected_pages
    finally:
        output.close()


def main() -> None:
    try:
        with TestClient(app) as client:
            first = _import_pdf(client, "first.pdf", _pdf_bytes("A1", "A2"))
            second = _import_pdf(client, "second.pdf", _pdf_bytes("B1"))
            source = _import_pdf(client, "source.pdf", _pdf_bytes("P1", "P2", "P3", "P4"))

            merged = _start_and_wait(
                client,
                {"operation": "merge", "document_refs": [first, second]},
            )
            _assert_pdf_artifact(merged, 3)

            extracted = _start_and_wait(
                client,
                {"operation": "extract", "document_refs": [source], "page_range": "1-2,4"},
            )
            _assert_pdf_artifact(extracted, 3)

            rotated = _start_and_wait(
                client,
                {
                    "operation": "rotate",
                    "document_refs": [source],
                    "page_range": "2-3",
                    "rotation_degrees": 90,
                },
            )
            _assert_pdf_artifact(rotated, 4)
            rotated_path = Path(rotated["artifact"]["metadata"]["output_path"])
            rotated_document = fitz.open(rotated_path)
            try:
                assert rotated_document.load_page(1).rotation == 90
                assert rotated_document.load_page(2).rotation == 90
            finally:
                rotated_document.close()

            deleted = _start_and_wait(
                client,
                {"operation": "delete", "document_refs": [source], "page_range": "2,4"},
            )
            _assert_pdf_artifact(deleted, 2)

            invalid = client.post(
                "/api/agents/document_agent/pdf-tools/start",
                json={"operation": "extract", "document_refs": [source], "page_range": "8"},
            )
            invalid.raise_for_status()
            invalid_task_id = invalid.json()["task_id"]
            for _attempt in range(80):
                invalid_result = client.get(
                    f"/api/agents/document_agent/pdf-tools/{invalid_task_id}/result"
                )
                invalid_result.raise_for_status()
                invalid_body = invalid_result.json()
                if invalid_body["status"] != "running" and invalid_body["status"] != "pending":
                    assert invalid_body["status"] == "failed", invalid_body
                    assert "页码范围" in invalid_body["message"], invalid_body
                    break
                time.sleep(0.025)
            else:
                raise AssertionError("invalid PDF task did not finish")

        print("PDF processing verification: merge/extract/rotate/delete/error paths passed")
    finally:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
