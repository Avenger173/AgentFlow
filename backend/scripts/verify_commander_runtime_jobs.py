"""验证 Commander C3 的后台 Runtime、暂停与同 task 恢复。

该验证完全离线：只使用受控 workspace、确定性 Runtime 工具和 TestClient。重点不是模型文案，
而是确认后台 ``/start`` 会立即返回、权限等待可以暂停、恢复不重跑完成步骤，且事件会落回
统一任务历史。
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
_VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_runtime_jobs_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(_VERIFY_DATA_DIR)
atexit.register(lambda: shutil.rmtree(_VERIFY_DATA_DIR, ignore_errors=True))

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.schemas.chat import WorkflowPlan, WorkflowPlanPreferences, WorkflowStep
from app.services.agent_catalog import list_agents
from app.workflow.dry_run import run_workflow_dry_run
from main import app


def _wait_for_status(
    client: TestClient,
    task_id: str,
    expected: str,
    *,
    timeout_seconds: float = 4.0,
) -> dict[str, object]:
    """短轮询后台任务；离线安全工具应在数秒内稳定进入指定状态。"""

    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}/runtime-state")
        assert response.status_code == 200, response.text
        last_payload = response.json()
        if last_payload["status"] == expected:
            return last_payload
        time.sleep(0.04)
    raise AssertionError(f"Runtime did not reach {expected}: {last_payload}")


def main() -> None:
    workspace = _VERIFY_DATA_DIR / "workspaces"
    workspace.mkdir(parents=True, exist_ok=True)
    document_name = "runtime_background_material.md"
    (workspace / document_name).write_text(
        "# C3 后台 Runtime 验收材料\n\n该任务必须保持可恢复的审批与步骤审计。\n",
        encoding="utf-8",
    )

    agents = [
        item.model_copy(update={"runtime_ready": True})
        for item in list_agents()
    ]
    plan = WorkflowPlan(
        workflow_name="commander_runtime_jobs_verify",
        description="验证后台 Runtime 的检查点、暂停与权限恢复。",
        summary="不会调用真实模型、网络或 Shell。",
        max_risk_level="medium",
        requires_confirmation=True,
        preference_applied=WorkflowPlanPreferences(permission_policy="smart_confirm"),
        steps=[
            WorkflowStep(
                id="step_plan",
                agent="commander_agent",
                action="analyze_task",
                title="建立受控执行上下文",
                input={"message": "验证后台执行控制。"},
                execution_mode="planning_only",
            ),
            WorkflowStep(
                id="step_read",
                agent="document_agent",
                action="read_text",
                title="读取验收材料",
                depends_on=["step_plan"],
                input={"path": document_name},
                required_permissions=["file_read"],
            ),
            WorkflowStep(
                id="step_code",
                agent="code_agent",
                action="generate_code",
                title="生成受控草稿",
                depends_on=["step_read"],
                required_permissions=["file_read", "file_write"],
                risk_level="medium",
                requires_confirmation=True,
            ),
            WorkflowStep(
                id="step_report",
                agent="report_agent",
                action="generate_report",
                title="生成受控报告",
                depends_on=["step_code"],
                required_permissions=["file_read", "file_write"],
                risk_level="medium",
                requires_confirmation=True,
            ),
        ],
    )
    source_task_id = "verify_commander_runtime_jobs"
    run_workflow_dry_run(
        task_id=source_task_id,
        plan=plan,
        available_agents=agents,
    )

    with TestClient(app) as client:
        start = client.post(f"/api/tasks/{source_task_id}/start")
        assert start.status_code == 200, start.text
        started = start.json()
        assert started["accepted"] is True
        assert started["status"] == "pending"
        runtime_task_id = started["runtime_task_id"]
        assert runtime_task_id != source_task_id

        # /start 返回后再连接也必须收到后台已经发生的阶段事件，而不是只能等最终历史回放。
        with client.websocket_connect(f"/ws/tasks/{runtime_task_id}") as websocket:
            first_live_event = websocket.receive_json()
        assert first_live_event["event"] in {"task_started", "task_resumed", "step_started"}

        waiting_state = _wait_for_status(client, runtime_task_id, "waiting_permission")
        assert waiting_state["allowed_actions"] == ["pause", "cancel"]

        pause = client.post(f"/api/tasks/{runtime_task_id}/pause")
        assert pause.status_code == 200, pause.text
        assert pause.json()["accepted"] is True
        assert pause.json()["status"] == "paused"
        paused_state = _wait_for_status(client, runtime_task_id, "paused")
        assert paused_state["allowed_actions"] == ["resume", "cancel"]

        permissions = client.get(f"/api/tasks/{runtime_task_id}/permissions")
        assert permissions.status_code == 200, permissions.text
        pending_by_step = {
            item["request"]["step_id"]: item["request"]["request_id"]
            for item in permissions.json()["permissions"]
        }
        code_decision = client.post(
            f"/api/tasks/{runtime_task_id}/permissions/{pending_by_step['step_code']}/decision",
            json={"decision": "approved", "decided_by": "verify_runtime_jobs"},
        )
        assert code_decision.status_code == 200, code_decision.text

        resume = client.post(f"/api/tasks/{runtime_task_id}/resume")
        assert resume.status_code == 200, resume.text
        assert resume.json()["accepted"] is True
        _wait_for_status(client, runtime_task_id, "waiting_permission")

        report_decision = client.post(
            f"/api/tasks/{runtime_task_id}/permissions/{pending_by_step['step_report']}/decision",
            json={"decision": "approved", "decided_by": "verify_runtime_jobs"},
        )
        assert report_decision.status_code == 200, report_decision.text
        final_resume = client.post(f"/api/tasks/{runtime_task_id}/resume")
        assert final_resume.status_code == 200, final_resume.text
        _wait_for_status(client, runtime_task_id, "completed")

        logs = client.get(f"/api/tasks/{runtime_task_id}/logs")
        assert logs.status_code == 200, logs.text
        events = logs.json()["events"]
        assert any(event["event"] == "task_paused" for event in events)
        assert sum(
            event["event"] == "step_started" and event["step_id"] == "step_read"
            for event in events
        ) == 1
        assert sum(
            event["event"] == "step_started" and event["step_id"] == "step_code"
            for event in events
        ) == 1
        assert sum(
            event["event"] == "step_started" and event["step_id"] == "step_report"
            for event in events
        ) == 1

    print("Commander C3 runtime job verification passed.")


if __name__ == "__main__":
    main()
