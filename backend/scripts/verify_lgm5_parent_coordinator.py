"""验证 LGM5.6 单父协调器的主库合并、恢复与安全回退。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_lgm5_parent_coordinator_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.langgraph_bridge_repository import load_langgraph_composition_bridge
from app.database.task_repository import (
    list_runtime_permission_requests,
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
import app.harness.langgraph_commander_composition_parent_coordinator as coordinator_module
from app.harness.langgraph_commander_composition_parent_coordinator import LangGraphCompositionParentCoordinator
from app.harness.langgraph_commander_composition_shadow import CommanderCompositionShadowError
from app.services.delivery_card import build_delivery_card
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun, WorkflowStepRun
from app.workflow import runtime


def _plan(*, suffix: str) -> WorkflowPlan:
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
        workflow_name=f"verify_lgm5_parent_coordinator_{suffix}",
        description="LGM5.6 临时协调器夹具。",
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
                depends_on=[item.id for item in specialists],
                input={
                    "child_step_ids": [item.id for item in specialists],
                    "composition_mode": "native_read_only_c6_4",
                },
                execution_mode="execute",
            ),
        ],
    )


def _save_parent(*, task_id: str, plan: WorkflowPlan) -> None:
    root = WorkflowStepRun(
        step_id="step_1",
        agent="commander_agent",
        action="analyze_task",
        status="completed",
        message="fixture：规划步骤已完成。",
        output={"runtime": True, "result": {"summary": "fixture"}},
    )
    pending = [runtime._pending_step(step) for step in plan.steps[1:]]
    save_workflow_run(
        run=WorkflowRun(
            task_id=task_id,
            mode="runtime",
            status="running",
            summary="fixture：父 Runtime 已准备好组合步骤。",
            steps=[root, *pending],
        ),
        events=[
            TaskLogEvent(
                task_id=task_id,
                sequence=1,
                event="task_started",
                agent_id="workflow_engine",
                message="fixture：父 Runtime 已开始。",
            )
        ],
        plan=plan,
        artifacts=[],
        tool_calls=[],
    )


def _fixture_executor_factory(calls: Counter[str]):
    def execute(runtime_task_id, step, plan, output_dir):
        del plan, output_dir
        calls[step.id] += 1
        call_id = str(step.input.get("_agentflow_delegation_call_id", ""))
        assert call_id.startswith("lgm5call_")
        started_at = datetime.now(UTC)
        if step.id == "step_3" and calls[step.id] == 1:
            return runtime._failed_safe_step(
                runtime_task_id=runtime_task_id,
                step=step,
                started_at=started_at,
                error_code="agent_delegate_failed",
                message="fixture：数据分支首次失败。",
            )
        result = {
            "delegated_task_id": f"fixture_child_{step.id}",
            "reply": f"fixture：{step.title} 已完成。",
            "source_count": 2 if step.id != "step_3" else 0,
            "chart_count": 2 if step.id == "step_3" else 0,
            "table_count": 1 if step.id == "step_3" else 0,
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
        return step_run, tool_call, [runtime._delegated_agent_artifact(runtime_task_id, step, result)]

    return execute


async def _verify_primary_path() -> None:
    task_id = "task_lgm5_parent_fixture"
    plan = _plan(suffix="primary")
    _save_parent(task_id=task_id, plan=plan)
    calls: Counter[str] = Counter()
    checkpoint = VERIFY_DATA_DIR / "graph" / "primary.sqlite"

    first = await LangGraphCompositionParentCoordinator(
        runtime_task_id=task_id,
        checkpoint_path=checkpoint,
        output_dir=VERIFY_DATA_DIR / "outputs",
        execute_step=_fixture_executor_factory(calls),
    ).execute()
    assert first.workflow_run.status == "blocked", first.workflow_run.summary
    assert first.graph_result.status == "partial"
    assert calls == Counter({"step_2": 1, "step_3": 1, "step_4": 1})
    first_steps = {item.step_id: item for item in first.workflow_run.steps}
    assert first_steps["step_2"].status == "completed"
    assert first_steps["step_3"].status == "failed"
    assert first_steps["step_4"].status == "completed"
    assert first_steps["step_5"].status == "completed"
    first_bridge = load_langgraph_composition_bridge(task_id)
    assert first_bridge is not None and first_bridge.status == "partial"
    assert len(first_bridge.completed_invocation_ids) == 2
    assert len(first_bridge.failed_invocation_ids) == 1
    assert {item.uri for item in list_workflow_artifacts(task_id)} == {
        "agentflow-task://fixture_child_step_2",
        "agentflow-task://fixture_child_step_4",
    }
    partial_card = build_delivery_card(
        run=first.workflow_run,
        artifacts=list_workflow_artifacts(task_id),
        tool_calls=list_workflow_tool_calls(task_id),
        permissions=list_runtime_permission_requests(task_id=task_id),
    )
    assert partial_card.status == "blocked"
    assert partial_card.terminal

    second = await LangGraphCompositionParentCoordinator(
        runtime_task_id=task_id,
        checkpoint_path=checkpoint,
        output_dir=VERIFY_DATA_DIR / "outputs",
        execute_step=_fixture_executor_factory(calls),
    ).execute(resume=True)
    assert second.workflow_run.status == "completed", second.workflow_run.summary
    assert second.graph_result.status == "completed"
    assert calls == Counter({"step_2": 1, "step_3": 2, "step_4": 1})
    current = load_workflow_run(task_id)
    assert current is not None and current.status == "completed"
    current_steps = {item.step_id: item for item in current.steps}
    assert all(current_steps[item].status == "completed" for item in ("step_2", "step_3", "step_4", "step_5"))
    bridge = load_langgraph_composition_bridge(task_id)
    assert bridge is not None and bridge.status == "completed"
    assert len(bridge.completed_invocation_ids) == 3
    assert not bridge.failed_invocation_ids
    assert len(list_workflow_tool_calls(task_id)) == 4
    assert {item.uri for item in list_workflow_artifacts(task_id)} == {
        "agentflow-task://fixture_child_step_2",
        "agentflow-task://fixture_child_step_3",
        "agentflow-task://fixture_child_step_4",
    }
    events = load_task_log_events(task_id) or []
    assert [item.event for item in events].count("step_completed") == 3
    assert [item.event for item in events].count("step_failed") == 1
    assert any(item.event == "task_waiting" for item in events)
    assert events[-1].event == "task_completed"
    delivery = build_delivery_card(
        run=current,
        artifacts=list_workflow_artifacts(task_id),
        tool_calls=list_workflow_tool_calls(task_id),
        permissions=list_runtime_permission_requests(task_id=task_id),
    )
    assert delivery.status == "completed"
    assert delivery.headline == "任务已完成 · 3 项交付"


async def _verify_fallback() -> None:
    task_id = "task_lgm5_fallback_fixture"
    plan = _plan(suffix="fallback")
    _save_parent(task_id=task_id, plan=plan)
    fallback_calls: list[str] = []

    async def fallback(runtime_task_id: str, received_plan: WorkflowPlan) -> WorkflowRun:
        assert runtime_task_id == task_id and received_plan.plan_id == plan.plan_id
        fallback_calls.append(runtime_task_id)
        run = load_workflow_run(runtime_task_id)
        assert run is not None
        return run.model_copy(update={"summary": "fixture：已安全回退 Native。"})

    result = await LangGraphCompositionParentCoordinator(
        runtime_task_id=task_id,
        checkpoint_path=VERIFY_DATA_DIR / "graph" / "missing.sqlite",
        output_dir=VERIFY_DATA_DIR / "outputs",
        execute_step=_fixture_executor_factory(Counter()),
        native_fallback=fallback,
    ).execute(resume=True)
    assert result.used_native_fallback
    assert fallback_calls == [task_id]
    bridge = load_langgraph_composition_bridge(task_id)
    assert bridge is not None and bridge.status == "failed"


async def _verify_parent_checkpoint_failure() -> None:
    task_id = "task_lgm5_checkpoint_failure"
    plan = _plan(suffix="checkpoint_failure")
    _save_parent(task_id=task_id, plan=plan)
    original_save = coordinator_module.save_workflow_runtime_checkpoint

    def reject_parent_checkpoint(**kwargs):
        del kwargs
        raise OSError("fixture parent checkpoint failure")

    coordinator_module.save_workflow_runtime_checkpoint = reject_parent_checkpoint
    try:
        try:
            await LangGraphCompositionParentCoordinator(
                runtime_task_id=task_id,
                checkpoint_path=VERIFY_DATA_DIR / "graph" / "checkpoint_failure.sqlite",
                output_dir=VERIFY_DATA_DIR / "outputs",
                execute_step=_fixture_executor_factory(Counter()),
            ).execute()
        except CommanderCompositionShadowError as exc:
            assert "检查点合并失败" in str(exc)
        else:
            raise AssertionError("父检查点写入失败时不得返回已完成。")
    finally:
        coordinator_module.save_workflow_runtime_checkpoint = original_save
    bridge = load_langgraph_composition_bridge(task_id)
    assert bridge is not None and bridge.status == "failed"


def main() -> None:
    asyncio.run(_verify_primary_path())
    asyncio.run(_verify_fallback())
    asyncio.run(_verify_parent_checkpoint_failure())
    print("LGM5 parent coordinator verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
