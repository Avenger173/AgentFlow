"""总指挥 C3 计划版本的隔离回归。

脚本只走 mock Chat 和 dry-run：它验证客户无法直接编辑步骤/权限，旧计划能回看，且已经
派生 Runtime 的来源任务会拒绝再修订，避免计划和实际执行脱节。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_plan_versions_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from main import app


def _create_document_plan(client: TestClient) -> tuple[str, dict]:
    response = client.post(
        "/api/chat",
        json={
            "message": "请整理已选项目材料中的验收要求。",
            "materials": [
                {
                    "binding_id": "revision_document",
                    "kind": "document",
                    "ref": "project_brief.md",
                    "display_name": "项目说明",
                    "origin": "client_selected",
                    "usage": "客户明确选择的修订材料。",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return payload["task_id"], payload["workflow_plan"]


def main() -> None:
    client = TestClient(app)
    task_id, initial_plan = _create_document_plan(client)
    assert initial_plan["plan_version"] == 1

    # 客户不能把 UI 表单伪装成步骤/权限编辑器；只有目标和变更说明是受支持输入。
    forbidden_field = client.post(
        f"/api/tasks/{task_id}/plan-revisions",
        json={
            "user_goal": "请提取已选材料中的交付范围和验收要求。",
            "change_summary": "改为明确提取交付范围。",
            "confirmed": True,
            "steps": [],
        },
    )
    assert forbidden_field.status_code == 422, forbidden_field.text

    unconfirmed = client.post(
        f"/api/tasks/{task_id}/plan-revisions",
        json={
            "user_goal": "请提取已选材料中的交付范围和验收要求。",
            "change_summary": "改为明确提取交付范围。",
            "confirmed": False,
        },
    )
    assert unconfirmed.status_code == 422, unconfirmed.text

    revised = client.post(
        f"/api/tasks/{task_id}/plan-revisions",
        json={
            "user_goal": "请提取已选材料中的交付范围、验收要求和待确认风险。",
            "change_summary": "补充范围和风险输出。",
            "confirmed": True,
        },
    )
    assert revised.status_code == 200, revised.text
    revised_payload = revised.json()
    revised_plan = revised_payload["workflow_plan"]
    assert revised_plan["plan_version"] == 2
    assert revised_plan["parent_plan_id"] == initial_plan["plan_id"]
    assert revised_plan["plan_id"] != initial_plan["plan_id"]
    assert revised_plan["material_bindings"] == initial_plan["material_bindings"]
    assert "验收要求和待确认风险" in revised_plan["user_goal"]
    assert "补充范围和风险输出" in revised_plan["change_summary"]
    assert revised_payload["workflow_run"]["status"] == "completed"

    # 当前计划已换到 v2，但 v1 仍能按版本精确回看，旧目标不会被覆盖。
    versions = client.get(f"/api/tasks/{task_id}/plan-versions")
    assert versions.status_code == 200, versions.text
    version_payload = versions.json()
    assert version_payload["total"] == 2
    assert version_payload["versions"][0]["plan_version"] == 2
    assert version_payload["versions"][0]["is_current"] is True
    assert version_payload["versions"][1]["plan_version"] == 1
    assert version_payload["versions"][1]["is_current"] is False
    previous = client.get(f"/api/tasks/{task_id}/plan-versions/1")
    assert previous.status_code == 200, previous.text
    assert previous.json()["workflow_plan"]["user_goal"] == initial_plan["user_goal"]
    assert client.get(f"/api/tasks/{task_id}/plan").json()["workflow_plan"]["plan_version"] == 2
    logs = client.get(f"/api/tasks/{task_id}/logs")
    assert logs.status_code == 200, logs.text
    assert any(event["event"] == "plan_revised" for event in logs.json()["events"])

    # 一旦已经派生真实 Runtime，新版本不会悄悄改变那条执行链；客户应创建新任务。
    executed_task, _ = _create_document_plan(client)
    executed = client.post(f"/api/tasks/{executed_task}/execute")
    assert executed.status_code == 200, executed.text
    locked = client.post(
        f"/api/tasks/{executed_task}/plan-revisions",
        json={
            "user_goal": "请重新安排已选材料的分析范围。",
            "change_summary": "尝试修改已执行任务。",
            "confirmed": True,
        },
    )
    assert locked.status_code == 409, locked.text
    assert "已经派生真实执行记录" in locked.json()["detail"]

    print("Commander C3 plan revision verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
