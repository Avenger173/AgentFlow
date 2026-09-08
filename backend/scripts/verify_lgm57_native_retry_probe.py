"""验证 LGM5.7 Graph 初始化故障会收束为 Native 重试，而不派发专业步骤。

本回归只使用临时 SQLite。checkpoint 故意传入目录，使 LangGraph SQLite 在图初始化阶段
失败；它不调用模型、不读取材料正文、不联网，也不会进入 document/data/knowledge Agent。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm57_native_retry_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.langgraph_bridge_repository import load_langgraph_composition_bridge
from app.database.task_repository import (
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_workflow_plan,
    load_workflow_run,
    save_workflow_run,
)
from app.harness.langgraph_commander_composition_parent_coordinator import (
    LangGraphCompositionParentCoordinator,
)
from app.harness.langgraph_composition_trial import (
    LangGraphCompositionDeveloperTrialCandidateRunner,
    LangGraphCompositionTrialAuthorization,
    authorize_composition_developer_trial,
)
from app.harness.langgraph_composition_trial_preparation import (
    prepare_composition_developer_trial_pair,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowRun


_TRIAL_ENV = {
    "AGENTFLOW_LANGGRAPH_ENABLED": "true",
    "AGENTFLOW_LANGGRAPH_COMPOSITION_TRIAL": "developer-approved",
}


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
            agent="knowledge_agent",
            action="answer_question",
            title="知识库可信问答",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
    ]
    return WorkflowPlan(
        workflow_name="verify_lgm57_native_retry_probe",
        description="LGM5.7 Native 重试离线故障注入夹具。",
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
                depends_on=[item.id for item in specialists],
                input={
                    "child_step_ids": [item.id for item in specialists],
                    "composition_mode": "native_read_only_c6_4",
                },
            ),
        ],
    )


def _save_source(task_id: str, plan: WorkflowPlan) -> None:
    save_workflow_run(
        run=WorkflowRun(
            task_id=task_id,
            mode="dry_run",
            status="completed",
            summary="LGM5.7 Native 重试离线夹具源计划。",
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
    plan = _plan()
    _save_source("task_lgm57_retry_source", plan)
    pair = prepare_composition_developer_trial_pair("task_lgm57_retry_source")
    candidate_plan = load_workflow_plan(pair.graph_candidate_runtime_task_id)
    assert candidate_plan is not None

    authorization = LangGraphCompositionTrialAuthorization(
        approval_reference="offline-fault-probe",
        material_scope_digest="a" * 64,
        model_profile_digest="b" * 64,
        native_reference_id="native-offline-probe",
        graph_reference_id="graph-offline-probe",
        real_materials_authorized=True,
        real_model_authorized=True,
    )
    authorized = authorize_composition_developer_trial(
        runtime_task_id=pair.graph_candidate_runtime_task_id,
        plan=candidate_plan,
        authorization=authorization,
        environment=_TRIAL_ENV,
    )
    assert authorized.status == "authorized"

    # checkpoint 是目录而非 SQLite 文件，保证错误发生在 Graph 初始化而非专业调用期间。
    invalid_checkpoint = VERIFY_ROOT / "invalid-checkpoint"
    invalid_checkpoint.mkdir()
    runner = LangGraphCompositionDeveloperTrialCandidateRunner(
        runtime_task_id=pair.graph_candidate_runtime_task_id,
        plan=candidate_plan,
        coordinator_factory=lambda: LangGraphCompositionParentCoordinator(
            runtime_task_id=pair.graph_candidate_runtime_task_id,
            checkpoint_path=invalid_checkpoint,
            output_dir=VERIFY_ROOT / "outputs",
        ),
        environment=_TRIAL_ENV,
    )
    result = asyncio.run(runner.execute())
    assert result.status == "stopped"
    assert result.native_retry_required

    bridge = load_langgraph_composition_bridge(pair.graph_candidate_runtime_task_id)
    assert bridge is not None
    assert bridge.status == "failed"
    assert bridge.delivery_state == "failed"

    # 成对准备只允许根规划步骤落库；专业 Tool/产物尚未发生。
    assert len(list_workflow_tool_calls(pair.graph_candidate_runtime_task_id)) == 1
    assert len(list_workflow_artifacts(pair.graph_candidate_runtime_task_id)) == 1
    run = load_workflow_run(pair.graph_candidate_runtime_task_id)
    assert run is not None and run.status == "pending"
    print("LGM5.7 native retry fault probe verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
