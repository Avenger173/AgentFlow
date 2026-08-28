"""知识库 K2 只读检索 API 离线回归。"""

from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_retrieval_api_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_retrieval_api_verify.db")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.database import sqlite as sqlite_service
from main import app


def _wait_for_index(client: TestClient, index_job_id: str) -> dict[str, object]:
    """只轮询真实 job 终态，不猜测后台任务进度。"""

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        payload = client.get(f"/api/knowledge/index-jobs/{index_job_id}").json()
        if payload["status"] in {"completed", "partial_failure", "failed", "cancelled"}:
            return payload
        time.sleep(0.03)
    raise AssertionError("知识库索引任务没有在离线夹具时间内结束。")


def main() -> None:
    try:
        with TestClient(app) as client:
            workspace = client.post(
                "/api/workspace/documents",
                json={
                    "filename": "retrieval_api.md",
                    "content": "# 交付要求\n\n编号 AF-204 的验收必须保留来源定位。\n",
                },
            )
            assert workspace.status_code == 200
            base = client.post("/api/knowledge/bases", json={"name": "K2 API 回归"})
            assert base.status_code == 201
            base_id = base.json()["knowledge_base_id"]
            imported = client.post(
                "/api/knowledge/documents/import",
                json={"knowledge_base_id": base_id, "workspace_document_names": ["retrieval_api.md"]},
            )
            assert imported.status_code == 201
            queued = client.post(f"/api/knowledge/bases/{base_id}/index/start")
            assert queued.status_code == 202
            finished = _wait_for_index(client, queued.json()["index_job_id"])
            assert finished["status"] == "completed"

            retrieved = client.post(
                "/api/knowledge/retrieve",
                json={"knowledge_base_id": base_id, "query": "AF-204 来源定位", "top_k": 3},
            )
            assert retrieved.status_code == 200
            payload = retrieved.json()
            assert payload["diagnostics"]["mode"] == "keyword"
            assert payload["evidences"]
            assert payload["evidences"][0]["source"]["document_version_id"].startswith("kb_ver_")

            pending = client.post("/api/knowledge/bases", json={"name": "K2 未索引回归"})
            assert pending.status_code == 201
            unavailable = client.post(
                "/api/knowledge/retrieve",
                json={"knowledge_base_id": pending.json()["knowledge_base_id"], "query": "任意问题"},
            )
            assert unavailable.status_code == 409
            missing = client.post(
                "/api/knowledge/retrieve",
                json={"knowledge_base_id": "kb_deadbeef", "query": "任意问题"},
            )
            assert missing.status_code == 404
        print("Knowledge K2 retrieval API verification passed: read-only evidence, unavailable and missing boundaries.")
    finally:
        # SQLite WAL 句柄在 Windows 上可能在 TestClient 退出后的极短时间内才释放；夹具重试
        # 清理，但不把清理失败掩盖成产品逻辑通过。
        sqlite_service._INITIALIZED_PATHS.clear()
        for _ in range(20):
            gc.collect()
            try:
                shutil.rmtree(VERIFY_ROOT, ignore_errors=False)
                break
            except PermissionError:
                time.sleep(0.05)
        else:
            shutil.rmtree(VERIFY_ROOT, ignore_errors=False)


if __name__ == "__main__":
    main()
