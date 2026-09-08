"""LGM5.7 开发者试点的 Native/Graph 成对准备器。

它只从一份已经完成的 C6.4 dry-run 计划创建两条受控 Runtime 骨架，并在 Graph 候选侧完成
无需模型、文件或网络的根规划步骤。真实专业调用仍必须经过独立预授权、候选运行和运行后审计；
本模块没有 API、Qt 或 Router 注册入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.database.task_repository import (
    append_workflow_event,
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_workflow_plan,
    load_workflow_run,
    save_workflow_runtime_checkpoint,
)
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionShadowError,
    build_composition_invocations,
)
from app.schemas.chat import WorkflowPlan
from app.schemas.workflow import WorkflowRun
from app.workflow import runtime as native_runtime


@dataclass(frozen=True)
class LangGraphCompositionTrialPreparedPair:
    """同一冻结组合计划的 Native 基线与 Graph 候选 Runtime 标识。"""

    source_task_id: str
    native_runtime_task_id: str
    graph_candidate_runtime_task_id: str
    plan_digest: str


def prepare_composition_developer_trial_pair(
    source_task_id: str,
) -> LangGraphCompositionTrialPreparedPair:
    """从一份 dry-run 计划创建可对照的 Runtime 对，但不执行专业 Agent。"""

    source_plan, source_run = _load_approved_source(source_task_id)
    _invocations, plan_digest = build_composition_invocations(source_plan)

    native_prepared = native_runtime.prepare_workflow_runtime(source_task_id)
    candidate_prepared = native_runtime.prepare_workflow_runtime(source_task_id)
    if (
        native_prepared is None
        or candidate_prepared is None
        or not native_prepared.accepted
        or not candidate_prepared.accepted
        or native_prepared.runtime_task_id == candidate_prepared.runtime_task_id
    ):
        raise CommanderCompositionShadowError(
            "无法从已批准计划创建独立的 Native/Graph 试点 Runtime。"
        )

    candidate_plan = load_workflow_plan(candidate_prepared.runtime_task_id)
    candidate_run = load_workflow_run(candidate_prepared.runtime_task_id)
    if candidate_plan is None or candidate_run is None:
        raise CommanderCompositionShadowError("Graph 候选 Runtime 未能保存其计划检查点。")
    if candidate_plan.plan_id != source_plan.plan_id:
        raise CommanderCompositionShadowError("Graph 候选 Runtime 的冻结计划身份不一致。")

    _complete_candidate_root_step(
        runtime_task_id=candidate_prepared.runtime_task_id,
        plan=candidate_plan,
        run=candidate_run,
    )
    return LangGraphCompositionTrialPreparedPair(
        source_task_id=source_run.task_id,
        native_runtime_task_id=native_prepared.runtime_task_id,
        graph_candidate_runtime_task_id=candidate_prepared.runtime_task_id,
        plan_digest=plan_digest,
    )


def _load_approved_source(source_task_id: str) -> tuple[WorkflowPlan, WorkflowRun]:
    plan = load_workflow_plan(source_task_id)
    run = load_workflow_run(source_task_id)
    if plan is None or run is None or run.mode != "dry_run" or run.status != "completed":
        raise CommanderCompositionShadowError(
            "开发者试点只能从已完成的 C6.4 dry-run 计划创建。"
        )
    if not native_runtime.supports_native_read_only_composition_runtime(plan):
        raise CommanderCompositionShadowError("源任务不属于获准的 C6.4 只读组合计划。")
    return plan, run


def _complete_candidate_root_step(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
    run: WorkflowRun,
) -> WorkflowRun:
    """只完成根规划步骤，让父协调器可在预授权后接管专业并行步骤。"""

    root_step = plan.steps[0]
    root_run, root_call, root_artifacts = native_runtime._execute_safe_step_with_retries(
        runtime_task_id=runtime_task_id,
        step=root_step,
        plan=plan,
        output_dir=native_runtime.settings.data_dir / "outputs" / runtime_task_id,
        runtime_context={},
    )
    if root_run.status != "completed":
        raise CommanderCompositionShadowError("候选 Runtime 的安全根规划步骤未能完成。")

    steps = [root_run, *(native_runtime._pending_step(step) for step in plan.steps[1:])]
    now = datetime.now(UTC)
    permission_requests = native_runtime._build_permission_requests(runtime_task_id, plan)
    prepared_run = WorkflowRun(
        task_id=runtime_task_id,
        mode="runtime",
        status="pending",
        summary="开发者 Graph 候选已完成安全规划步骤，正在等待预授权。",
        max_risk_level=plan.max_risk_level,
        requires_confirmation=plan.requires_confirmation,
        validation_errors=[],
        steps=steps,
        limits=native_runtime._runtime_execution_limits(plan),
        metrics=native_runtime._build_runtime_metrics(
            steps=steps,
            tool_calls=[root_call],
            permission_requests=permission_requests,
            started_at=native_runtime._runtime_started_at(run),
            finished_at=now,
        ),
    )
    save_workflow_runtime_checkpoint(
        run=prepared_run,
        plan=plan,
        permission_requests=permission_requests,
        artifacts=[*list_workflow_artifacts(runtime_task_id), *root_artifacts],
        tool_calls=[*list_workflow_tool_calls(runtime_task_id), root_call],
    )
    append_workflow_event(
        task_id=runtime_task_id,
        event_name="step_completed",
        agent_id=root_step.agent,
        step_id=root_step.id,
        message="开发者候选已完成安全内置规划步骤；尚未创建 Graph 或执行专业调用。",
    )
    return prepared_run
