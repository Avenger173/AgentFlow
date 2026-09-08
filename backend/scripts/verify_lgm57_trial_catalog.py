"""验证 LGM5.7 试点源计划目录只返回完成的 C6.4 只读组合计划。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm57_catalog_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import save_workflow_run
from app.harness.langgraph_composition_trial_catalog import (
    list_composition_developer_trial_sources,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun


def _composition_plan() -> WorkflowPlan:
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
    ]
    return WorkflowPlan(
        workflow_name="verify_lgm57_trial_catalog",
        description="LGM5.7 目录夹具。",
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
                id="step_4",
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


def _save(task_id: str, *, status: str, plan: WorkflowPlan) -> None:
    save_workflow_run(
        run=WorkflowRun(
            task_id=task_id,
            mode="dry_run",
            status=status,  # type: ignore[arg-type]
            summary="fixture",
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
        plan = _composition_plan()
        _save("task_lgm57_catalog_completed", status="completed", plan=plan)
        _save("task_lgm57_catalog_pending", status="pending", plan=plan)
        sources = list_composition_developer_trial_sources(limit=20)
        assert len(sources) == 1
        source = sources[0]
        assert source.task_id == "task_lgm57_catalog_completed"
        assert len(source.plan_digest) == 64
        assert source.specialist_actions == (
            "document_agent.analyze_document",
            "data_agent.analyze_dataset",
        )
        try:
            list_composition_developer_trial_sources(limit=0)
        except ValueError:
            pass
        else:
            raise AssertionError("越界的候选数量不应被接受。")
        print("LGM5.7 composition trial catalog verification passed.")
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
