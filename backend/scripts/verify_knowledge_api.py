"""知识库 K1.6 FastAPI 受控入口离线回归。

该脚本只覆盖本机 SQLite、受控 workspace、文档复制和 FTS generation；不会请求模型、
下载 Embedding 权重、读取客户资料或访问网络。它必须通过实际 HTTP 协议取得 workspace
返回的规范化文件名，避免客户端误把原始本机文件名当作资料库导入引用。
"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_api_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_api_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.database import sqlite as sqlite_service
from main import app


def _wait_for_terminal_job(client: TestClient, index_job_id: str) -> dict[str, object]:
    """轮询已受理的后台任务，直到真实终态或明确的离线超时。

    这里不以固定 sleep 假定 FTS 已完成；真实 Qt 也必须读取 job 状态而非在客户端猜测进度。
    """

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        response = client.get(f"/api/knowledge/index-jobs/{index_job_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "partial_failure", "failed", "cancelled"}:
            return payload
        time.sleep(0.03)
    raise AssertionError("知识库后台索引任务在离线回归时未进入终态。")


def main() -> None:
    try:
        with TestClient(app) as client:
            # 只经由已有 workspace API 导入；资料库 API 不接受路径或正文，避免绕过文件边界。
            workspace_response = client.post(
                "/api/workspace/documents",
                json={
                    "filename": "knowledge_api_source.md",
                    "content": "# K1 API check\n\nThe index job must be observable and recoverable.\n",
                },
            )
            assert workspace_response.status_code == 200, workspace_response.text
            workspace_document_name = workspace_response.json()["name"]

            created = client.post(
                "/api/knowledge/bases",
                json={"name": "K1 API verification", "description": "temporary local verification"},
            )
            assert created.status_code == 201, created.text
            knowledge_base = created.json()
            knowledge_base_id = knowledge_base["knowledge_base_id"]
            assert knowledge_base["status"] == "empty"

            # 同名资料库必须返回可理解的冲突，不得以数据库异常或静默重复掩盖客户操作。
            conflict = client.post("/api/knowledge/bases", json={"name": "K1 API verification"})
            assert conflict.status_code == 409, conflict.text

            imported = client.post(
                "/api/knowledge/documents/import",
                json={
                    "knowledge_base_id": knowledge_base_id,
                    "workspace_document_names": [workspace_document_name],
                },
            )
            assert imported.status_code == 201, imported.text
            import_payload = imported.json()
            assert import_payload["status"] == "queued"
            assert import_payload["items"][0]["workspace_document_name"] == workspace_document_name
            assert import_payload["items"][0]["document_version"]["status"] == "queued"
            bases_before_index = client.get("/api/knowledge/bases")
            assert bases_before_index.status_code == 200, bases_before_index.text
            assert bases_before_index.json()["knowledge_bases"][0]["status"] == "empty"

            started = client.post(f"/api/knowledge/bases/{knowledge_base_id}/index/start")
            assert started.status_code == 202, started.text
            job_payload = started.json()
            terminal_job = _wait_for_terminal_job(client, job_payload["index_job_id"])
            assert terminal_job["status"] == "completed", terminal_job
            assert terminal_job["stage"] == "completed"
            assert terminal_job["indexed_document_count"] == 1

            documents = client.get(f"/api/knowledge/bases/{knowledge_base_id}/documents")
            assert documents.status_code == 200, documents.text
            assert len(documents.json()["documents"]) == 1
            assert documents.json()["documents"][0]["active_version_id"]

            jobs = client.get(f"/api/knowledge/bases/{knowledge_base_id}/index-jobs")
            assert jobs.status_code == 200, jobs.text
            assert jobs.json()["jobs"][0]["index_job_id"] == job_payload["index_job_id"]

            bases = client.get("/api/knowledge/bases")
            assert bases.status_code == 200, bases.text
            assert bases.json()["knowledge_bases"][0]["status"] == "ready"

            # 诊断接口只能报告依赖/缓存状态，不能隐式触发模型权重下载。
            capability = client.get("/api/knowledge/vector-capability")
            assert capability.status_code == 200, capability.text
            capability_payload = capability.json()
            assert capability_payload["chroma_available"] is True
            assert capability_payload["fastembed_available"] is True
            assert capability_payload["model_initialized"] is False

            deletion = client.delete(f"/api/knowledge/bases/{knowledge_base_id}")
            assert deletion.status_code == 202, deletion.text
            assert deletion.json()["status"] == "deleting"

            # 删除是后台清理，不能假设 202 返回时磁盘和 SQLite 已完成；以客户实际列表状态
            # 轮询终态。源 workspace 文件必须依然可打开，证明删除没有越过受控副本边界。
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                current_bases = client.get("/api/knowledge/bases")
                assert current_bases.status_code == 200, current_bases.text
                if not any(
                    item["knowledge_base_id"] == knowledge_base_id
                    for item in current_bases.json()["knowledge_bases"]
                ):
                    break
                time.sleep(0.03)
            else:
                raise AssertionError("知识库删除任务未在离线回归中完成。")
            source_preview = client.get(f"/api/workspace/documents/{workspace_document_name}")
            assert source_preview.status_code == 200, source_preview.text
            removed_documents = client.get(f"/api/knowledge/bases/{knowledge_base_id}/documents")
            assert removed_documents.status_code == 404, removed_documents.text

        print("Knowledge K1.6 API verification passed: controlled import, background FTS job, status and vector guard.")
    finally:
        gc.collect()
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
