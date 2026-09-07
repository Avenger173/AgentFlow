"""验证 LGM5.4 组合业务 Adapter 只从主库复核 approved invocation。

不调用真实专业 Agent、模型、网络、MCP 或客户文件。夹具执行器只记录被批准的步骤，并故意
返回一段不应进入 Graph 的文本，确认 Adapter 输出会被压缩成固定客户安全摘要。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm5_business_adapter_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.langgraph_bridge_repository import ensure_langgraph_composition_bridge
from app.database.task_repository import save_workflow_run
from app.harness.langgraph_commander_composition_bridge import (
    build_composition_bridge_record,
    mark_composition_bridge_running,
)
from app.harness.langgraph_commander_composition_business_adapter import (
    AgentFlowCompositionBusinessAdapter,
    composition_delegation_call_id,
)
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionInvocation,
    CommanderCompositionOutcome,
    build_composition_invocations,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun


_TASK_ID = "task_lgm5_business_adapter"
_SECRET_GOAL = "机密用户目标：不能通过 invocation 直接进入业务执行器。"
_SECRET_REF = "客户隐藏资料.md"


def _plan() -> WorkflowPlan:
    return WorkflowPlan(
        workflow_name="verify_lgm5_business_adapter",
        description="LGM5.4 Adapter 夹具。",
        user_goal=_SECRET_GOAL,
        material_bindings=[
            {
                "binding_id": "document_fixture",
                "kind": "document",
                "ref": _SECRET_REF,
                "display_name": "展示名.md",
                "origin": "client_selected",
                "usage": "fixture",
            },
            {
                "binding_id": "dataset_fixture",
                "kind": "dataset",
                "ref": "客户隐藏数据.csv",
                "display_name": "数据.csv",
                "origin": "client_selected",
                "usage": "fixture",
            },
        ],
        steps=[
            WorkflowStep(
                id="step_1",
                agent="commander_agent",
                action="analyze_task",
                title="分析用户任务",
                input={"message": _SECRET_GOAL},
                execution_mode="planning_only",
            ),
            WorkflowStep(
                id="step_2",
                agent="document_agent",
                action="analyze_document",
                title="文档受控分析",
                depends_on=["step_1"],
                parallel_group="specialist_read_only",
                input={"document_refs": [_SECRET_REF]},
            ),
            WorkflowStep(
                id="step_3",
                agent="data_agent",
                action="analyze_dataset",
                title="数据只读预览",
                depends_on=["step_1"],
                parallel_group="specialist_read_only",
                input={"dataset_refs": ["客户隐藏数据.csv"]},
            ),
            WorkflowStep(
                id="step_4",
                agent="commander_agent",
                action="synthesize_results",
                title="汇总已完成结果",
                depends_on=["step_2", "step_3"],
                input={
                    "child_step_ids": ["step_2", "step_3"],
                    "composition_mode": "native_read_only_c6_4",
                },
            ),
        ],
    )


async def _verify() -> None:
    plan = _plan()
    save_workflow_run(
        run=WorkflowRun(task_id=_TASK_ID, mode="runtime", status="pending", summary="fixture"),
        events=[TaskLogEvent(task_id=_TASK_ID, sequence=1, event="runtime_accepted", agent_id="fixture", message="fixture")],
        plan=plan,
        artifacts=[],
        tool_calls=[],
    )
    ensure_langgraph_composition_bridge(build_composition_bridge_record(runtime_task_id=_TASK_ID, plan=plan))
    mark_composition_bridge_running(runtime_task_id=_TASK_ID)
    invocations, _digest = build_composition_invocations(plan)
    calls: list[tuple[str, str, str, str]] = []

    async def executor(
        runtime_task_id: str,
        step: WorkflowStep,
        approved_plan: WorkflowPlan,
    ) -> CommanderCompositionOutcome:
        calls.append(
            (
                runtime_task_id,
                step.id,
                approved_plan.plan_id,
                str(step.input.get("_agentflow_delegation_call_id", "")),
            )
        )
        invocation = next(item for item in invocations if item.step_id == step.id)
        return CommanderCompositionOutcome(
            invocation_id=invocation.invocation_id,
            status="completed",
            summary="机密专业结论：这段内容不应进入 Graph checkpoint。",
            delegated_task_id=f"fixture_child_{step.id}",
            source_count=2,
        )

    adapter = AgentFlowCompositionBusinessAdapter(
        runtime_task_id=_TASK_ID,
        execute_approved_step=executor,
    )
    result = await adapter(invocations[0])
    assert result.status == "completed"
    assert result.summary == "一项已批准的专业步骤已完成；完整结果保留在关联任务交付中。"
    assert result.delegated_task_id == "fixture_child_step_2"
    assert result.source_count == 2
    expected_call_id = composition_delegation_call_id(runtime_task_id=_TASK_ID, invocation=invocations[0])
    assert expected_call_id.startswith("lgm5call_")
    assert calls == [(_TASK_ID, "step_2", plan.plan_id, expected_call_id)]
    assert "_agentflow_delegation_call_id" not in plan.steps[1].input

    forged = CommanderCompositionInvocation(
        invocation_id=invocations[1].invocation_id,
        step_id=invocations[1].step_id,
        agent_id=invocations[1].agent_id,
        action=invocations[1].action,
        material_digest="0" * 64,
        input_digest=invocations[1].input_digest,
    )
    rejected = await adapter(forged)
    assert rejected.status == "failed"
    assert calls == [(_TASK_ID, "step_2", plan.plan_id, expected_call_id)]


def main() -> None:
    asyncio.run(_verify())
    print("LGM5 composition business adapter verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
