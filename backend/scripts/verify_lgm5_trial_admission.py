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

from app.database.langgraph_trial_repository import (
    load_langgraph_composition_trial_admission,
    load_langgraph_composition_trial_authorization,
)
from app.database.task_repository import save_workflow_run
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionShadowError,
    CommanderCompositionShadowResult,
    build_composition_invocations,
)
from app.harness.langgraph_composition_trial import (
    LangGraphCompositionTrialAuthorization,
    LangGraphCompositionDeveloperTrialCandidateRunner,
    LangGraphCompositionDeveloperTrialRunner,
    LangGraphCompositionTrialRecoveryObservation,
    LangGraphCompositionTrialResourceMeasurement,
    LangGraphCompositionTrialRetryObservation,
    developer_trial_switch_enabled,
    authorize_composition_developer_trial,
    observe_composition_developer_trial,
    register_composition_developer_trial,
    require_composition_developer_trial,
    require_composition_developer_trial_authorization,
    revoke_composition_developer_trial,
    revoke_composition_developer_trial_authorization,
)
from app.harness import langgraph_composition_trial as trial_module
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent
from app.schemas.langgraph_trial import LangGraphCompositionTrialEvidence
from app.schemas.workflow import WorkflowArtifact, WorkflowRun, WorkflowStepRun, WorkflowToolCall


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


def _authorization(**overrides: object) -> LangGraphCompositionTrialAuthorization:
    values: dict[str, object] = {
        "approval_reference": "approval-preauthorization-0001",
        "material_scope_digest": "c" * 64,
        "model_profile_digest": "d" * 64,
        "native_reference_id": "native-preauthorization-0001",
        "graph_reference_id": "graph-preauthorization-0001",
        "real_materials_authorized": True,
        "real_model_authorized": True,
    }
    values.update(overrides)
    return LangGraphCompositionTrialAuthorization.model_validate(values)


class _RaisingCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *, resume: bool = False):
        del resume
        self.calls += 1
        raise RuntimeError("fixture：bridge checkpoint 异常。")


def _completed_run(task_id: str, plan: WorkflowPlan) -> WorkflowRun:
    steps: list[WorkflowStepRun] = []
    for step in plan.steps:
        if step.id in {"step_2", "step_3"}:
            result = {"source_count": 2}
        elif step.id == "step_4":
            # Native Runtime 的既有客户交付契约使用 complete；Graph bridge 使用 completed。
            result = {"completion_state": "complete"}
        else:
            result = {}
        steps.append(
            WorkflowStepRun(
                step_id=step.id,
                agent=step.agent,
                action=step.action,
                status="completed",
                message="fixture completed",
                output={"result": result},
            )
        )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary="fixture composition completed",
        steps=steps,
    )


def _artifacts(task_id: str) -> list[WorkflowArtifact]:
    return [
        WorkflowArtifact(
            artifact_id=f"{task_id}:step_2",
            task_id=task_id,
            step_id="step_2",
            agent_id="document_agent",
            kind="text",
            name="fixture document",
            mime_type="text/plain",
        ),
        WorkflowArtifact(
            artifact_id=f"{task_id}:step_3",
            task_id=task_id,
            step_id="step_3",
            agent_id="data_agent",
            kind="data",
            name="fixture data",
            mime_type="application/json",
        ),
    ]


def _tool_calls(task_id: str) -> list[WorkflowToolCall]:
    return [
        WorkflowToolCall(
            call_id=f"{task_id}:step_2",
            task_id=task_id,
            step_id="step_2",
            agent_id="document_agent",
            tool_name="agent.document_agent.analyze",
            status="completed",
        ),
        WorkflowToolCall(
            call_id=f"{task_id}:step_3",
            task_id=task_id,
            step_id="step_3",
            agent_id="data_agent",
            tool_name="agent.data_agent.analyze",
            status="completed",
        ),
        WorkflowToolCall(
            call_id=f"{task_id}:step_4",
            task_id=task_id,
            step_id="step_4",
            agent_id="commander_agent",
            tool_name="planner.synthesize",
            status="completed",
        ),
    ]


def _events(task_id: str) -> list[TaskLogEvent]:
    return [
        TaskLogEvent(
            task_id=task_id,
            sequence=index,
            event="step_completed",
            agent_id=agent_id,
            step_id=step_id,
            message="fixture",
        )
        for index, (step_id, agent_id) in enumerate(
            (
                ("step_2", "document_agent"),
                ("step_3", "data_agent"),
                ("step_4", "commander_agent"),
            ),
            start=1,
        )
    ]


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

    preauthorization_disabled_task = "task_lgm57_preauthorization_disabled"
    _save_runtime(preauthorization_disabled_task, plan)
    preauthorization_disabled = authorize_composition_developer_trial(
        runtime_task_id=preauthorization_disabled_task,
        plan=plan,
        authorization=_authorization(),
        environment={"AGENTFLOW_LANGGRAPH_ENABLED": "true"},
    )
    assert preauthorization_disabled.status == "rejected"
    assert any("显式开启" in item for item in preauthorization_disabled.blockers)

    candidate_task = "task_lgm57_preauthorization_candidate"
    _save_runtime(candidate_task, plan)
    candidate_authorization = authorize_composition_developer_trial(
        runtime_task_id=candidate_task,
        plan=plan,
        authorization=_authorization(),
        environment=_OPEN_TRIAL_ENV,
    )
    assert candidate_authorization.status == "authorized"
    assert load_langgraph_composition_trial_admission(candidate_task) is None
    assert require_composition_developer_trial_authorization(
        runtime_task_id=candidate_task,
        plan=plan,
        environment=_OPEN_TRIAL_ENV,
    ).status == "authorized"
    candidate_coordinator = _RaisingCoordinator()
    candidate_result = asyncio.run(
        LangGraphCompositionDeveloperTrialCandidateRunner(
            runtime_task_id=candidate_task,
            plan=plan,
            coordinator_factory=lambda: candidate_coordinator,  # type: ignore[arg-type]
            environment=_OPEN_TRIAL_ENV,
        ).execute()
    )
    assert candidate_result.status == "stopped" and candidate_result.native_retry_required
    assert candidate_coordinator.calls == 1
    candidate_revoked = revoke_composition_developer_trial_authorization(candidate_task)
    assert candidate_revoked.status == "revoked"
    assert load_langgraph_composition_trial_authorization(candidate_task) == candidate_revoked
    try:
        require_composition_developer_trial_authorization(
            runtime_task_id=candidate_task,
            plan=plan,
            environment=_OPEN_TRIAL_ENV,
        )
    except CommanderCompositionShadowError:
        pass
    else:
        raise AssertionError("已撤销的候选预授权不能再次进入 Graph 执行。")

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

    native_run = _completed_run("task_lgm57_observation_native", plan)
    graph_run = _completed_run("task_lgm57_observation_graph", plan)
    invocations, _ = build_composition_invocations(plan)
    graph_result = CommanderCompositionShadowResult(
        task_id=graph_run.task_id,
        status="completed",
        plan_digest=plan_digest,
        completed_invocation_ids=tuple(item.invocation_id for item in invocations),
        failed_invocation_ids=(),
        delivery={
            "status": "completed",
            "result_scope": "仅汇总已完成的受控专业调用；未完成分支不进入本次结论。",
        },
    )
    observation = observe_composition_developer_trial(
        plan=plan,
        native_run=native_run,
        graph_result=graph_result,
        graph_run=graph_run,
        native_artifacts=_artifacts(native_run.task_id),
        graph_artifacts=_artifacts(graph_run.task_id),
        native_tool_calls=_tool_calls(native_run.task_id),
        graph_tool_calls=_tool_calls(graph_run.task_id),
        native_events=_events(native_run.task_id),
        graph_events=_events(graph_run.task_id),
        authorization=LangGraphCompositionTrialAuthorization(
            approval_reference="approval-observation-0001",
            material_scope_digest="c" * 64,
            model_profile_digest="d" * 64,
            native_reference_id="native-observation-0001",
            graph_reference_id="graph-observation-0001",
            real_materials_authorized=True,
            real_model_authorized=True,
        ),
        native_resources=LangGraphCompositionTrialResourceMeasurement(
            startup_ms=1_000,
            resident_memory_mib=500,
        ),
        graph_resources=LangGraphCompositionTrialResourceMeasurement(
            startup_ms=1_050,
            resident_memory_mib=520,
        ),
        recovery=LangGraphCompositionTrialRecoveryObservation(
            initial_completed_invocation_ids=(invocations[0].invocation_id,),
            initial_failed_invocation_ids=(invocations[1].invocation_id,),
            resumed_completed_invocation_ids=tuple(item.invocation_id for item in invocations),
            replayed_invocation_ids=(invocations[1].invocation_id,),
        ),
        native_retry=LangGraphCompositionTrialRetryObservation(
            graph_or_bridge_failure_observed=True,
            trial_stopped=True,
            native_retry_required=True,
        ),
    )
    assert observation.composition_report.outcome == "passed"
    assert observation.evidence.composition_comparison_passed
    assert observation.evidence.event_delivery_comparison_passed
    assert observation.evidence.source_artifact_comparison_passed
    assert observation.evidence.recovery_semantics_passed

    # 已保存失败事件在恢复成功后仍需留给审计，但最终客户状态应以最后一次步骤终态为准。
    recovered_events = [
        *_events(graph_run.task_id),
        TaskLogEvent(
            task_id=graph_run.task_id,
            sequence=10,
            event="step_failed",
            agent_id="document_agent",
            step_id="step_2",
            message="fixture failure",
            level="error",
        ),
        TaskLogEvent(
            task_id=graph_run.task_id,
            sequence=11,
            event="step_completed",
            agent_id="document_agent",
            step_id="step_2",
            message="fixture recovered",
            level="info",
        ),
    ]
    scoped_steps = trial_module._composition_scope_step_ids(plan)
    assert trial_module._event_projection_signature(
        _events(native_run.task_id), scoped_steps, native_run.status
    ) == trial_module._event_projection_signature(
        recovered_events, scoped_steps, graph_run.status
    )

    # 两条独立检索都拿到可定位来源即可；图表和表格数量仍由交付签名逐项比较。
    source_count_variant = _completed_run("task_lgm57_source_variant", plan)
    source_step = next(item for item in source_count_variant.steps if item.step_id == "step_2")
    source_step.output["result"]["source_count"] = 3
    assert trial_module._result_fact_signature(
        native_run, scoped_steps
    ) == trial_module._result_fact_signature(source_count_variant, scoped_steps)
    source_step.output["result"]["source_count"] = 0
    assert trial_module._result_fact_signature(
        native_run, scoped_steps
    ) != trial_module._result_fact_signature(source_count_variant, scoped_steps)

    mismatched_artifacts = _artifacts(graph_run.task_id)
    mismatched_artifacts[1] = mismatched_artifacts[1].model_copy(update={"kind": "file"})
    mismatch = observe_composition_developer_trial(
        plan=plan,
        native_run=native_run,
        graph_result=graph_result,
        graph_run=graph_run,
        native_artifacts=_artifacts(native_run.task_id),
        graph_artifacts=mismatched_artifacts,
        native_tool_calls=_tool_calls(native_run.task_id),
        graph_tool_calls=_tool_calls(graph_run.task_id),
        native_events=_events(native_run.task_id),
        graph_events=_events(graph_run.task_id),
        authorization=LangGraphCompositionTrialAuthorization(
            approval_reference="approval-observation-0002",
            material_scope_digest="e" * 64,
            model_profile_digest="f" * 64,
            native_reference_id="native-observation-0002",
            graph_reference_id="graph-observation-0002",
            real_materials_authorized=True,
            real_model_authorized=True,
        ),
        native_resources=LangGraphCompositionTrialResourceMeasurement(
            startup_ms=1_000,
            resident_memory_mib=500,
        ),
        graph_resources=LangGraphCompositionTrialResourceMeasurement(
            startup_ms=1_050,
            resident_memory_mib=520,
        ),
        recovery=LangGraphCompositionTrialRecoveryObservation(
            initial_completed_invocation_ids=(invocations[0].invocation_id,),
            initial_failed_invocation_ids=(invocations[1].invocation_id,),
            resumed_completed_invocation_ids=tuple(item.invocation_id for item in invocations),
            replayed_invocation_ids=(invocations[1].invocation_id,),
        ),
        native_retry=LangGraphCompositionTrialRetryObservation(
            graph_or_bridge_failure_observed=True,
            trial_stopped=True,
            native_retry_required=True,
        ),
    )
    assert not mismatch.artifacts_match
    assert not mismatch.evidence.source_artifact_comparison_passed
    observed_rejected_task = "task_lgm57_trial_observed_rejected"
    _save_runtime(observed_rejected_task, plan)
    observed_rejected = register_composition_developer_trial(
        runtime_task_id=observed_rejected_task,
        plan=plan,
        evidence=mismatch.evidence,
        environment=_OPEN_TRIAL_ENV,
    )
    assert observed_rejected.status == "rejected"
    assert any("来源与受控产物" in item for item in observed_rejected.blockers)
    print("LGM5.7 developer trial admission verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
