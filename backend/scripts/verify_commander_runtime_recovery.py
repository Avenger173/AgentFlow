"""验证服务重启后 Runtime 只会安全停驻，不会被自动续跑。

测试刻意构造一个已进入 ``running`` 的 Runtime 检查点，再调用启动恢复扫描。验收重点是：
状态进入 blocked、正在执行的步骤和工具带中断标记、append-only 事件存在，并且客户只能 retry
生成新的 dry-run。全程使用 SQLite 和 mock，不触发模型、网络或文件写入。
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile


os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
_VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_runtime_recovery_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(_VERIFY_DATA_DIR)
atexit.register(lambda: shutil.rmtree(_VERIFY_DATA_DIR, ignore_errors=True))

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.database.task_repository import (
    append_workflow_event,
    list_workflow_artifacts,
    load_workflow_plan,
    load_workflow_run,
    save_workflow_runtime_checkpoint,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.workflow import WorkflowToolCall
from app.services.agent_catalog import list_agents
from app.workflow.dry_run import run_workflow_dry_run
from app.workflow.runtime import prepare_workflow_runtime
from main import app


def main() -> None:
    """构造中断检查点，断言恢复扫描只落库安全事实。"""

    source_task_id = "verify_commander_runtime_recovery"
    plan = WorkflowPlan(
        workflow_name="commander_runtime_recovery_verify",
        description="验证 Runtime 服务重启恢复语义。",
        summary="不会调用真实模型、网络或 Shell。",
        steps=[
            WorkflowStep(
                id="step_plan",
                agent="commander_agent",
                action="analyze_task",
                title="建立执行上下文",
                input={"message": "验证服务重启时的安全停驻。"},
            )
        ],
    )
    agents = [item.model_copy(update={"runtime_ready": True}) for item in list_agents()]
    run_workflow_dry_run(task_id=source_task_id, plan=plan, available_agents=agents)
    prepared = prepare_workflow_runtime(source_task_id)
    assert prepared is not None and prepared.accepted is True
    runtime_task_id = prepared.runtime_task_id
    runtime_run = load_workflow_run(runtime_task_id)
    runtime_plan = load_workflow_plan(runtime_task_id)
    assert runtime_run is not None and runtime_plan is not None

    running_step = runtime_run.steps[0].model_copy(
        update={"status": "running", "message": "模拟进程退出前正在执行。"}
    )
    running_tool = WorkflowToolCall(
        call_id=f"{runtime_task_id}:step_plan:runtime-tool",
        task_id=runtime_task_id,
        step_id="step_plan",
        agent_id="commander_agent",
        tool_name="commander.analyze_task",
        status="running",
    )
    interrupted_seed = runtime_run.model_copy(
        update={
            "status": "running",
            "summary": "模拟服务退出前的执行状态。",
            "steps": [running_step],
        }
    )
    save_workflow_runtime_checkpoint(
        run=interrupted_seed,
        plan=runtime_plan,
        permission_requests=[],
        artifacts=list_workflow_artifacts(runtime_task_id),
        tool_calls=[running_tool],
    )
    append_workflow_event(
        task_id=runtime_task_id,
        event_name="step_started",
        agent_id="commander_agent",
        step_id="step_plan",
        message="模拟 Runtime 已进入工具边界。",
    )

    with TestClient(app) as client:
        # 进入 TestClient context 会执行与真实 Uvicorn 相同的 FastAPI lifespan；这里不手工
        # 调用恢复函数，确保后端实际启动路径也会扫描并安全停驻遗留 Runtime。
        assert app.state.recovered_runtime_task_count == 1
        recovered = load_workflow_run(runtime_task_id)
        assert recovered is not None
        assert recovered.status == "blocked"
        assert recovered.steps[0].status == "blocked"
        assert recovered.steps[0].output["interrupted_by_service_restart"] is True
        state = client.get(f"/api/tasks/{runtime_task_id}/runtime-state")
        assert state.status_code == 200, state.text
        assert state.json()["status"] == "blocked"
        assert state.json()["allowed_actions"] == ["retry"]
        logs = client.get(f"/api/tasks/{runtime_task_id}/logs")
        assert logs.status_code == 200, logs.text
        assert any(event["event"] == "task_interrupted_by_restart" for event in logs.json()["events"])
        retry = client.post(f"/api/tasks/{runtime_task_id}/retry")
        assert retry.status_code == 200, retry.text
        assert retry.json()["accepted"] is True
        assert retry.json()["new_task_id"] != runtime_task_id

    print("Commander C3 runtime recovery verification passed.")


if __name__ == "__main__":
    main()
