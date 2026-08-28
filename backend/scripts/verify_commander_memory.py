"""总指挥 C2 长期记忆初版的隔离回归。

脚本只使用 FastAPI TestClient 和 mock 聊天模式，不调用真实模型、不读取开发数据库。它验证
用户确认、敏感内容拒绝、总开关、最小计划注入和删除边界，避免“功能看似存在但关闭后仍读取”
这类隐私回归。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_memory_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from main import app


def main() -> None:
    client = TestClient(app)

    # 创建必须显式确认；服务端同时把秘密和绝对路径挡在数据库外，而不是只依赖 Qt 表单。
    rejected_confirmation = client.post(
        "/api/memories",
        json={
            "kind": "user_preference",
            "title": "未确认偏好",
            "summary": "不应保存。",
            "user_confirmed": False,
        },
    )
    assert rejected_confirmation.status_code == 400, rejected_confirmation.text

    rejected_secret = client.post(
        "/api/memories",
        json={
            "kind": "project_constraint",
            "title": "含秘密的记录",
            "summary": "api_key=test-api-key-placeholder",
            "user_confirmed": True,
        },
    )
    assert rejected_secret.status_code == 400, rejected_secret.text

    rejected_path = client.post(
        "/api/memories",
        json={
            "kind": "project_constraint",
            "title": "含路径的记录",
            "summary": "读取 D:\\private\\customer.docx 的原文。",
            "user_confirmed": True,
        },
    )
    assert rejected_path.status_code == 400, rejected_path.text

    create_response = client.post(
        "/api/memories",
        json={
            "kind": "project_constraint",
            "scope": "global",
            "title": "项目交付约束",
            "summary": "项目方案优先给出可追溯的范围、验收标准和待确认事项。",
            "tags": ["项目", "验收", "交付"],
            "source_task_id": "task_verify_memory",
            "user_confirmed": True,
        },
    )
    assert create_response.status_code == 201, create_response.text
    memory = create_response.json()
    memory_id = memory["memory_id"]
    assert memory["source_task_id"] == "task_verify_memory"
    assert memory["enabled"] is True

    listed = client.get("/api/memories?scope=global")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1

    # 默认关闭时，计划连记忆表都不读取，响应中不能出现本次记录。
    disabled_plan = client.post("/api/chat", json={"message": "请帮我整理这个项目的任务。"})
    assert disabled_plan.status_code == 200, disabled_plan.text
    assert disabled_plan.json()["workflow_plan"]["memory_context_summary"] == []

    saved_preferences = client.put(
        "/api/settings/runtime-preferences",
        json={
            "permission_policy": "smart_confirm",
            "personality": "professional",
            "memory_enabled": True,
        },
    )
    assert saved_preferences.status_code == 200, saved_preferences.text
    assert saved_preferences.json()["memory_enabled"] is True

    enabled_plan = client.post("/api/chat", json={"message": "请帮我整理这个项目的任务。"})
    assert enabled_plan.status_code == 200, enabled_plan.text
    plan = enabled_plan.json()["workflow_plan"]
    assert any("项目交付约束" in item for item in plan["memory_context_summary"]), plan
    assert any("已参考 1 条用户确认的长期记忆" in item for item in plan["assumptions"]), plan

    used_memory = client.get(f"/api/memories/{memory_id}")
    assert used_memory.status_code == 200, used_memory.text
    assert used_memory.json()["last_used_at"]

    disabled_record = client.put(f"/api/memories/{memory_id}", json={"enabled": False})
    assert disabled_record.status_code == 200, disabled_record.text
    no_record_plan = client.post("/api/chat", json={"message": "请帮我整理这个项目的任务。"})
    assert no_record_plan.status_code == 200, no_record_plan.text
    assert no_record_plan.json()["workflow_plan"]["memory_context_summary"] == []

    clear_without_confirmation = client.delete("/api/memories?scope=global")
    assert clear_without_confirmation.status_code == 400, clear_without_confirmation.text
    deleted = client.delete(f"/api/memories/{memory_id}")
    assert deleted.status_code == 204, deleted.text
    assert client.get("/api/memories?scope=global").json()["total"] == 0

    print("Commander C2 memory verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
