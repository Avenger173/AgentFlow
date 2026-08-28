"""项目文档审查 V1 的离线端到端验收。

脚本只使用临时 data 目录与 FastAPI TestClient：它验证规则报告、来源定位、任务历史、Tool
审计和异步结果轮询，不触发任何真实模型调用或用户 workspace 写入。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    temp_data_dir = Path(tempfile.mkdtemp(prefix="agentflow_project_review_verify_"))
    os.environ["AGENTFLOW_DATA_DIR"] = str(temp_data_dir)
    os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
    sys.path.insert(0, str(backend_root))
    try:
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        imported = client.post(
            "/api/workspace/documents",
            json={
                "filename": "项目实施方案.md",
                "content": (
                    "# 客户服务平台实施方案\n\n"
                    "## 项目范围\n"
                    "本期包含客户资料导入和工单查询，不包含历史系统迁移。\n\n"
                    "## 实施计划\n"
                    "第一阶段在 2026-08-15 前完成试点部署。\n\n"
                    "## 需求\n"
                    "系统必须支持按客户编号查询工单状态。\n"
                ),
            },
        )
        imported.raise_for_status()
        document_ref = imported.json()["relative_path"]

        response = client.post(
            "/api/agents/document_agent/project-review/run",
            json={"document_ref": document_ref, "document_type": "project_proposal"},
        )
        response.raise_for_status()
        payload = response.json()
        report = payload["report"]
        findings = report["findings"]
        rule_ids = {item["rule_id"] for item in findings}
        assert payload["status"] == "completed"
        assert report["document_ref"] == document_ref
        assert report["review_strategy"] == "deterministic_rules_v1"
        assert "project_review.acceptance_criteria" in rule_ids
        assert "project_review.requirement_testability" in rule_ids
        assert "project_review.ownership" in rule_ids
        assert all(item["source_refs"] for item in findings)
        check_status = {item["rule_id"]: item["status"] for item in report["checks"]}
        assert check_status["project_review.scope_boundary"] == "passed"
        assert check_status["project_review.schedule"] == "passed"

        task_id = payload["task_id"]
        tool_calls = client.get(f"/api/tasks/{task_id}/tool-calls")
        tool_calls.raise_for_status()
        tools = tool_calls.json()["tool_calls"]
        assert len(tools) == 1
        assert tools[0]["tool_name"] == "document.read_text"
        assert tools[0]["request"]["coverage"] == "full_document"

        started = client.post(
            "/api/agents/document_agent/project-review/start",
            json={"document_ref": document_ref},
        )
        assert started.status_code == 202
        async_task_id = started.json()["task_id"]
        final_payload: dict[str, object] | None = None
        for _ in range(30):
            result_response = client.get(
                f"/api/agents/document_agent/project-review/{async_task_id}/result"
            )
            result_response.raise_for_status()
            candidate = result_response.json()
            if candidate["status"] != "running":
                final_payload = candidate
                break
            time.sleep(0.05)
        assert final_payload is not None
        assert final_payload["status"] == "completed"
        assert final_payload["result"]["report"]["document_ref"] == document_ref

        missing = client.post(
            "/api/agents/document_agent/project-review/run",
            json={"document_ref": "不存在的项目方案.md"},
        )
        assert missing.status_code == 400
    finally:
        shutil.rmtree(temp_data_dir, ignore_errors=True)

    print("Project document review verification passed.")


if __name__ == "__main__":
    main()
