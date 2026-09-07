"""验证 LGM5.7 开发者试点准入、撤销与 Native 重试守卫。

只使用临时 SQLite 与伪协调器。证据对象用来覆盖契约分支，并不代表已经进行了真实材料、
真实模型或客户任务试点。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm57_trial_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database.langgraph_trial_repository import load_langgraph_composition_trial_admission
from app.database.task_repository import save_workflow_run
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionShadowError,
    build_composition_invocations,
)
from app.harness.langgraph_composition_trial import (
    LangGraphCompositionDeveloperTrialRunner,
    developer_trial_switch_enabled,
    register_composition_developer_trial,
    require_composition_developer_trial,
    revoke_composition_developer_trial,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.langgraph_trial import LangGraphCompositionTrialEvidence
from app.schemas.workflow import WorkflowRun


_OPEN_TRIAL_ENV = {
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
            agent="data_agent",
            action="analyze_dataset",
            title="数据只读预览",
            depends_on=["step_1"],
            parallel_group="specialist_read_only",
        ),
    ]
    return WorkflowPlan(
        workflow_name="verify_lgm57_trial_admission",
        description="LGM5.7 临时准入夹具。",
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


def _save_runtime(task_id: str, plan: WorkflowPlan) -> None:
    save_workflow_run(
        run=WorkflowRun(
            task_id=task_id,
            mode="runtime",
            status="pending",
            summary="LGM5.7 临时开发者试点任务。",
        ),
        events=[
            TaskLogEvent(
                task_id=task_id,
                sequence=1,
                event="runtime_queued",
                agent_id="workflow_engine",
                message="fixture",
            )
        ],
        plan=plan,
        artifacts=[],
        tool_calls=[],
    )


def _evidence(plan_digest: str, **overrides: object) -> LangGraphCompositionTrialEvidence:
    values: dict[str, object] = {
        "evidence_origin": "developer_authorized_live",
        "approval_reference": "approval-0001",
        "comparison_reference": "comparison-0001",
        "plan_digest": plan_digest,
        "material_scope_digest": "a" * 64,
        "model_profile_digest": "b" * 64,
        "native_reference_id": "native-reference-0001",
        "graph_reference_id": "graph-reference-0001",
        "composition_comparison_passed": True,
        "event_delivery_comparison_passed": True,
        "source_artifact_comparison_passed": True,
        "recovery_semantics_passed": True,
        "native_retry_route_verified": True,
        "real_materials_authorized": True,
        "real_model_authorized": True,
        "native_startup_ms": 1_000,
        "graph_startup_ms": 1_100,
        "native_resident_memory_mib": 500,
        "graph_resident_memory_mib": 550,
    }
    values.update(overrides)
    return LangGraphCompositionTrialEvidence.model_validate(values)


class _RaisingCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *, resume: bool = False):
        del resume
        self.calls += 1
        raise RuntimeError("fixture：bridge checkpoint 异常。")


def main() -> None:
    plan = _plan()
    _invocations, plan_digest = build_composition_invocations(plan)

    assert not developer_trial_switch_enabled({"AGENTFLOW_LANGGRAPH_ENABLED": "true"})
    assert developer_trial_switch_enabled(_OPEN_TRIAL_ENV)

    disabled_task = "task_lgm57_trial_disabled"
    _save_runtime(disabled_task, plan)
    disabled = register_composition_developer_trial(
        runtime_task_id=disabled_task,
        plan=plan,
        evidence=_evidence(plan_digest),
        environment={"AGENTFLOW_LANGGRAPH_ENABLED": "true"},
    )
    assert disabled.status == "rejected"
    assert any("显式开启" in item for item in disabled.blockers)

    fixture_task = "task_lgm57_trial_fixture"
    _save_runtime(fixture_task, plan)
    fixture = register_composition_developer_trial(
        runtime_task_id=fixture_task,
        plan=plan,
        evidence=_evidence(plan_digest, evidence_origin="fixture"),
        environment=_OPEN_TRIAL_ENV,
    )
    assert fixture.status == "rejected"
    assert any("确定性夹具" in item for item in fixture.blockers)

    baseline_task = "task_lgm57_trial_baseline"
    _save_runtime(baseline_task, plan)
    baseline = register_composition_developer_trial(
        runtime_task_id=baseline_task,
        plan=plan,
        evidence=_evidence(plan_digest, graph_resident_memory_mib=551),
        environment=_OPEN_TRIAL_ENV,
    )
    assert baseline.status == "rejected"
    assert any("常驻内存" in item for item in baseline.blockers)

    admitted_task = "task_lgm57_trial_admitted"
    _save_runtime(admitted_task, plan)
    admitted = register_composition_developer_trial(
        runtime_task_id=admitted_task,
        plan=plan,
        evidence=_evidence(plan_digest),
        environment=_OPEN_TRIAL_ENV,
    )
    assert admitted.status == "admitted", admitted
    assert load_langgraph_composition_trial_admission(admitted_task) is not None
    assert register_composition_developer_trial(
        runtime_task_id=admitted_task,
        plan=plan,
        evidence=_evidence(plan_digest),
        environment=_OPEN_TRIAL_ENV,
    ) == admitted
    required = require_composition_developer_trial(
        runtime_task_id=admitted_task,
        plan=plan,
        environment=_OPEN_TRIAL_ENV,
    )
    assert required.status == "admitted"
    revoked = revoke_composition_developer_trial(admitted_task)
    assert revoked.status == "revoked"
    try:
        require_composition_developer_trial(
            runtime_task_id=admitted_task,
            plan=plan,
            environment=_OPEN_TRIAL_ENV,
        )
    except CommanderCompositionShadowError:
        pass
    else:
        raise AssertionError("已撤销的试点不能再次进入 Graph 执行。")

    stopped_task = "task_lgm57_trial_stopped"
    _save_runtime(stopped_task, plan)
    assert register_composition_developer_trial(
        runtime_task_id=stopped_task,
        plan=plan,
        evidence=_evidence(plan_digest),
        environment=_OPEN_TRIAL_ENV,
    ).status == "admitted"
    coordinator = _RaisingCoordinator()
    result = asyncio.run(
        LangGraphCompositionDeveloperTrialRunner(
            runtime_task_id=stopped_task,
            plan=plan,
            coordinator_factory=lambda: coordinator,  # type: ignore[arg-type]
            environment=_OPEN_TRIAL_ENV,
        ).execute()
    )
    assert result.status == "stopped" and result.native_retry_required
    assert coordinator.calls == 1
    print("LGM5.7 developer trial admission verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
