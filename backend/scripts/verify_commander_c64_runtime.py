"""验证 C6.4 Native 组合 Runtime 的调度边界。

本脚本用受控 fixture 替代三类专业 Agent 的内部模型/文件调用，只验证父 Runtime：最多两个
并发槽位、一个分支失败不取消其它只读分支、最终汇总只包含已完成的脱敏子结果。它不会读取
客户材料、不会调用 Provider，也不会联网。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_c64_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import load_task_log_events, save_workflow_run
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun, WorkflowStepRun
from app.workflow import runtime


def _plan() -> WorkflowPlan:
    """构造最小可执行 DAG，不依赖客户真实材料或 Agent 健康探测。"""

    specialist_steps = [
        WorkflowStep(
            id="step_2",
            agent="document_agent",
            action="analyze_document",
            title="文档受控分析",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
        WorkflowStep(
            id="step_3",
            agent="data_agent",
            action="analyze_dataset",
            title="数据只读预览",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
        WorkflowStep(
            id="step_4",
            agent="knowledge_agent",
            action="answer_question",
            title="知识库可信问答",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
    ]
    return WorkflowPlan(
        workflow_name="verify_commander_c64_runtime",
        description="C6.4 组合 Runtime 离线 fixture。",
        steps=[
            WorkflowStep(
                id="step_1",
                agent="commander_agent",
                action="analyze_task",
                title="分析用户任务",
                input={"message": "fixture"},
                execution_mode="planning_only",
            ),
            *specialist_steps,
            WorkflowStep(
                id="step_5",
                agent="commander_agent",
                action="synthesize_results",
                title="汇总已完成结果",
                depends_on=[step.id for step in specialist_steps],
                input={
                    "child_step_ids": [step.id for step in specialist_steps],
                    "composition_mode": "native_read_only_c6_4",
                },
                execution_mode="execute",
            ),
        ],
    )


def main() -> None:
    plan = _plan()
    assert runtime._supports_native_composition_runtime(plan)
    source_task_id = "verify_commander_c64_runtime"
    save_workflow_run(
        run=WorkflowRun(
            task_id=source_task_id,
            mode="dry_run",
            status="completed",
            summary="C6.4 fixture dry-run。",
        ),
        events=[
            TaskLogEvent(
                task_id=source_task_id,
                sequence=1,
                event="dry_run_completed",
                agent_id="workflow_engine",
                message="C6.4 fixture dry-run 已完成。",
            )
        ],
        plan=plan,
        artifacts=[],
        tool_calls=[],
    )

    original_executor = runtime._execute_safe_step_with_retries
    lock = threading.Lock()
    concurrency = {"active": 0, "peak": 0}

    def fixture_executor(*, runtime_task_id, step, plan, output_dir, runtime_context):
        """模拟三个子任务，其中数据分支失败，另外两支仍可完成。"""

        with lock:
            concurrency["active"] += 1
            concurrency["peak"] = max(concurrency["peak"], concurrency["active"])
        try:
            if step.parallel_group:
                # 留出足够交叠时间，使并发限制可被稳定观测。
                time.sleep(0.12)
            if step.id == "step_3":
                return runtime._failed_safe_step(
                    runtime_task_id=runtime_task_id,
                    step=step,
                    started_at=datetime.now(UTC),
                    error_code="agent_delegate_failed",
                    message="fixture：数据子任务失败。",
                )

            result = {
                "delegated_task_id": f"fixture_child_{step.id}",
                "reply": f"{step.title} 的脱敏结论。",
                "source_count": 2,
            }
            started_at = datetime.now(UTC)
            step_run = WorkflowStepRun(
                step_id=step.id,
                agent=step.agent,
                action=step.action,
                status="completed",
                message=f"fixture：{step.title} 已完成。",
                output={
                    "runtime": True,
                    "tool_name": runtime._tool_name_for_step(step),
                    "result": result,
                },
            )
            tool_call = runtime._completed_tool_call(
                runtime_task_id=runtime_task_id,
                step=step,
                attempt=1,
                timeout_ms=30_000,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                request={"fixture": True},
                result=result,
            )
            return step_run, tool_call, []
        finally:
            with lock:
                concurrency["active"] -= 1

    runtime._execute_safe_step_with_retries = fixture_executor
    try:
        response = runtime.execute_workflow_runtime(source_task_id)
    finally:
        runtime._execute_safe_step_with_retries = original_executor

    assert response is not None and response.workflow_run is not None
    run = response.workflow_run
    step_by_id = {step.step_id: step for step in run.steps}
    assert run.status == "completed", run.summary
    assert "部分完成" in run.summary
    assert concurrency["peak"] == 2, concurrency
    assert step_by_id["step_2"].status == "completed"
    assert step_by_id["step_3"].status == "failed"
    assert step_by_id["step_4"].status == "completed"
    assert step_by_id["step_5"].status == "completed"
    synthesis = step_by_id["step_5"].output["result"]
    assert synthesis["completion_state"] == "partial"
    assert {item["step_id"] for item in synthesis["completed_children"]} == {"step_2", "step_4"}
    assert {item["step_id"] for item in synthesis["unavailable_children"]} == {"step_3"}
    assert run.limits.max_tool_calls == 12
    assert run.limits.task_timeout_ms == 240_000
    events = load_task_log_events(run.task_id) or []
    assert any(event.event == "composition_group_started" for event in events)
    assert any(event.event == "step_failed" and event.step_id == "step_3" for event in events)
    print("Commander C6.4 composition runtime verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
