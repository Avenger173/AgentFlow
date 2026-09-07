"""验证 LGM5 Native 与 LangGraph 组合影子的交付事实一致性。

同一份三分支只读组合计划先通过 Native C6.4 fixture 执行，再由 LangGraph shadow 使用
等价的确定性 adapter 执行。整个脚本只使用临时 SQLite、假子任务结果与受控摘要，不读取
客户材料、不调用模型、网络或 MCP。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm5_comparison_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import save_workflow_run
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionInvocation,
    CommanderCompositionOutcome,
    LangGraphCommanderCompositionShadowBackend,
    compare_native_composition_execution,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun, WorkflowStepRun
from app.workflow import runtime


def _plan() -> WorkflowPlan:
    specialists = [
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
        workflow_name="verify_lgm5_composition_comparison",
        description="LGM5 Native/Graph 对照夹具。",
        steps=[
            WorkflowStep(
                id="step_1",
                agent="commander_agent",
                action="analyze_task",
                title="分析用户任务",
                input={"message": "fixture"},
                execution_mode="planning_only",
            ),
            *specialists,
            WorkflowStep(
                id="step_5",
                agent="commander_agent",
                action="synthesize_results",
                title="汇总已完成结果",
                depends_on=[step.id for step in specialists],
                input={
                    "child_step_ids": [step.id for step in specialists],
                    "composition_mode": "native_read_only_c6_4",
                },
            ),
        ],
    )


def _native_fixture_executor(*, runtime_task_id, step, plan, output_dir, runtime_context):
    del plan, output_dir, runtime_context
    started_at = datetime.now(UTC)
    if step.id == "step_3":
        return runtime._failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="agent_delegate_failed",
            message="fixture：数据子任务失败。",
        )
    result = {
        "delegated_task_id": f"fixture_child_{step.id}",
        "reply": f"{step.title} 的脱敏结论。",
        "source_count": 2,
    }
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message=f"fixture：{step.title} 已完成。",
        output={"runtime": True, "tool_name": runtime._tool_name_for_step(step), "result": result},
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


async def _verify_shadow(plan: WorkflowPlan):
    async def adapter(invocation: CommanderCompositionInvocation) -> CommanderCompositionOutcome:
        if invocation.step_id == "step_3":
            return CommanderCompositionOutcome(
                invocation_id=invocation.invocation_id,
                status="failed",
                summary="fixture：数据子任务失败。",
                recovery_hint="修复数据分支后可单独恢复。",
            )
        return CommanderCompositionOutcome(
            invocation_id=invocation.invocation_id,
            status="completed",
            summary=f"fixture：{invocation.agent_id} 已完成。",
            delegated_task_id=f"fixture_child_{invocation.step_id}",
            source_count=2,
        )

    backend = LangGraphCommanderCompositionShadowBackend(
        checkpoint_path=VERIFY_ROOT / "checkpoints" / "comparison.db",
        adapters={
            ("document_agent", "analyze_document"): adapter,
            ("data_agent", "analyze_dataset"): adapter,
            ("knowledge_agent", "answer_question"): adapter,
        },
    )
    try:
        return await backend.execute_task(task_id="task_lgm5_shadow_compare", plan=plan)
    finally:
        await backend.close()


def main() -> None:
    plan = _plan()
    source_task_id = "task_lgm5_native_compare"
    save_workflow_run(
        run=WorkflowRun(task_id=source_task_id, mode="dry_run", status="completed", summary="LGM5 对照 dry-run。"),
        events=[TaskLogEvent(task_id=source_task_id, sequence=1, event="dry_run_completed", agent_id="workflow_engine", message="fixture")],
        plan=plan,
        artifacts=[],
        tool_calls=[],
    )
    original_executor = runtime._execute_safe_step_with_retries
    runtime._execute_safe_step_with_retries = _native_fixture_executor
    try:
        response = runtime.execute_workflow_runtime(source_task_id)
    finally:
        runtime._execute_safe_step_with_retries = original_executor
    assert response is not None and response.workflow_run is not None
    shadow = asyncio.run(_verify_shadow(plan))
    report = compare_native_composition_execution(
        plan=plan,
        native_run=response.workflow_run,
        shadow_execution=shadow,
    )
    assert report.outcome == "passed", report
    assert report.native_completed_step_ids == report.shadow_completed_step_ids == ("step_2", "step_4")
    assert report.native_unavailable_step_ids == report.shadow_unavailable_step_ids == ("step_3",)
    assert report.native_delivery_state == report.shadow_delivery_state == "partial"
    print("LGM5 Native/Graph composition comparison verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
