"""验证 LGM5.7 的 Native/Graph 候选成对准备，不调用模型或专业 Agent。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm57_prepare_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import (
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
from app.harness.langgraph_commander_composition_shadow import CommanderCompositionShadowError
from app.harness.langgraph_composition_trial_preparation import (
    prepare_composition_developer_trial_pair,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun


def _plan() -> WorkflowPlan:
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
            agent="knowledge_agent",
            action="answer_question",
            title="知识库可信问答",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
    ]
    return WorkflowPlan(
        workflow_name="verify_lgm57_trial_preparation",
        description="LGM5.7 成对准备夹具。",
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
                id="step_4",
                agent="commander_agent",
                action="synthesize_results",
                title="汇总已完成结果",
                depends_on=[step.id for step in specialist_steps],
                input={
                    "child_step_ids": [step.id for step in specialist_steps],
                    "composition_mode": "native_read_only_c6_4",
                },
            ),
        ],
    )


def _save_source(task_id: str, plan: WorkflowPlan, *, status: str = "completed") -> None:
    save_workflow_run(
        run=WorkflowRun(
            task_id=task_id,
            mode="dry_run",
            status=status,  # type: ignore[arg-type]
            summary="LGM5.7 成对准备临时源计划。",
        ),
        events=[
            TaskLogEvent(
                task_id=task_id,
                sequence=1,
                event="dry_run_completed",
                agent_id="workflow_engine",
                message="fixture",
            )
        ],
        plan=plan,
        artifacts=[],
        tool_calls=[],
    )


def main() -> None:
    try:
        plan = _plan()
        _save_source("task_lgm57_prepare_source", plan)
        pair = prepare_composition_developer_trial_pair("task_lgm57_prepare_source")
        assert pair.native_runtime_task_id != pair.graph_candidate_runtime_task_id
        assert len(pair.plan_digest) == 64

        native_run = load_workflow_run(pair.native_runtime_task_id)
        candidate_run = load_workflow_run(pair.graph_candidate_runtime_task_id)
        assert native_run is not None and candidate_run is not None
        assert all(step.status == "pending" for step in native_run.steps)
        assert candidate_run.status == "pending"
        assert candidate_run.steps[0].status == "completed"
        assert all(step.status == "pending" for step in candidate_run.steps[1:])
        assert len(list_workflow_tool_calls(pair.graph_candidate_runtime_task_id)) == 1
        assert len(list_workflow_artifacts(pair.graph_candidate_runtime_task_id)) == 1
        assert any(
            event.step_id == "step_1" and event.event == "step_completed"
            for event in load_task_log_events(pair.graph_candidate_runtime_task_id)
        )

        _save_source("task_lgm57_prepare_incomplete", plan, status="pending")
        try:
            prepare_composition_developer_trial_pair("task_lgm57_prepare_incomplete")
        except CommanderCompositionShadowError:
            pass
        else:
            raise AssertionError("未完成 dry-run 计划不能生成试点 Runtime 对。")
        print("LGM5.7 Native/Graph trial preparation verification passed.")
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
