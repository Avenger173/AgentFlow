"""LGM5.7 默认关闭的 Commander 组合图开发者试点闸门。

此模块故意没有注册 RuntimeRouter、FastAPI 或 Qt。它只在开发者显式调用时检查授权证据、
资源基线、撤销状态和当前环境开关；任何不满足条件的请求都留在 Native 路线。
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from app.database.langgraph_trial_repository import (
    load_langgraph_composition_trial_admission,
    load_langgraph_composition_trial_authorization,
    revoke_langgraph_composition_trial_admission,
    revoke_langgraph_composition_trial_authorization,
    save_langgraph_composition_trial_admission,
    save_langgraph_composition_trial_authorization,
)
from app.harness.langgraph_commander_composition_parent_coordinator import (
    LangGraphCompositionCoordinatorResult,
    LangGraphCompositionParentCoordinator,
)
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionComparisonReport,
    CommanderCompositionShadowError,
    CommanderCompositionShadowResult,
    build_composition_invocations,
    compare_native_composition_execution,
)
from app.schemas.chat import WorkflowPlan
from app.schemas.events import TaskLogEvent
from app.schemas.langgraph_trial import (
    LangGraphCompositionTrialAdmissionRecord,
    LangGraphCompositionTrialAuthorization,
    LangGraphCompositionTrialAuthorizationRecord,
    LangGraphCompositionTrialEvidence,
)
from app.schemas.workflow import WorkflowArtifact, WorkflowRun, WorkflowToolCall
from app.workflow.runtime import supports_native_read_only_composition_runtime


_TRIAL_SWITCH_VALUE = "developer-approved"
_MAX_BASELINE_REGRESSION_RATIO = 1.10


@dataclass(frozen=True)
class LangGraphCompositionTrialDecision:
    """一次准入判断的受限结果，供内部脚本/测试而非客户 UI 使用。"""

    admitted: bool
    blockers: tuple[str, ...]
    plan_digest: str


@dataclass(frozen=True)
class LangGraphCompositionTrialExecutionResult:
    """试点执行的内部终态；故障只指向 Native 重试，绝不静默换后端。"""

    status: str
    runtime_task_id: str
    coordinator_result: LangGraphCompositionCoordinatorResult | None = None
    native_retry_required: bool = False
    message: str = ""


@dataclass(frozen=True)
class LangGraphCompositionTrialResourceMeasurement:
    """同机同进程采样的启动/常驻内存事实，不使用推测或模型 token 替代。"""

    startup_ms: int
    resident_memory_mib: int

    def __post_init__(self) -> None:
        if self.startup_ms < 1 or self.resident_memory_mib < 1:
            raise ValueError("试点资源基线必须是正整数。")


@dataclass(frozen=True)
class LangGraphCompositionTrialRecoveryObservation:
    """一次失败后恢复的调用集合，证明已完成分支没有被重复派发。"""

    initial_completed_invocation_ids: tuple[str, ...]
    initial_failed_invocation_ids: tuple[str, ...]
    resumed_completed_invocation_ids: tuple[str, ...]
    replayed_invocation_ids: tuple[str, ...]

    def passed(self) -> bool:
        initial_completed = set(self.initial_completed_invocation_ids)
        initial_failed = set(self.initial_failed_invocation_ids)
        resumed_completed = set(self.resumed_completed_invocation_ids)
        replayed = set(self.replayed_invocation_ids)
        return bool(
            initial_failed
            and not initial_completed.intersection(initial_failed)
            and initial_failed.issubset(resumed_completed)
            and replayed.issubset(initial_failed)
            and not replayed.intersection(initial_completed)
        )


@dataclass(frozen=True)
class LangGraphCompositionTrialRetryObservation:
    """故障注入后的停止与 Native 重试观察。"""

    graph_or_bridge_failure_observed: bool
    trial_stopped: bool
    native_retry_required: bool

    def passed(self) -> bool:
        return (
            self.graph_or_bridge_failure_observed
            and self.trial_stopped
            and self.native_retry_required
        )


@dataclass(frozen=True)
class LangGraphCompositionTrialObservation:
    """由两条 Runtime 的受限审计投影生成的试点证据。"""

    evidence: LangGraphCompositionTrialEvidence
    composition_report: CommanderCompositionComparisonReport
    tool_calls_match: bool
    artifacts_match: bool
    event_projection_match: bool
    delivery_projection_match: bool


def developer_trial_switch_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """读取每次启动前都可撤回的双重开发者开关。

    仅 ``AGENTFLOW_LANGGRAPH_ENABLED=true`` 不足以进入试点；第二项必须是精确值，避免
    部署方把通用依赖开关误当作客户路由许可。
    """

    values = environment if environment is not None else os.environ
    return (
        str(values.get("AGENTFLOW_LANGGRAPH_ENABLED", "")).strip().lower()
        in {"1", "true", "yes", "on"}
        and str(values.get("AGENTFLOW_LANGGRAPH_COMPOSITION_TRIAL", "")).strip().lower()
        == _TRIAL_SWITCH_VALUE
    )


def evaluate_composition_developer_trial_authorization(
    *,
    plan: WorkflowPlan,
    authorization: LangGraphCompositionTrialAuthorization,
    environment: Mapping[str, str] | None = None,
) -> LangGraphCompositionTrialDecision:
    """验证真实候选运行前的最小边界，但不把运行后结论提前写成准入。"""

    blockers: list[str] = []
    try:
        _invocations, plan_digest = build_composition_invocations(plan)
    except CommanderCompositionShadowError:
        plan_digest = ""
        blockers.append("任务没有通过 C6.4 受控只读组合计划准入。")
    if not supports_native_read_only_composition_runtime(plan):
        blockers.append("当前计划超出开发者试点允许的只读 Agent/action 边界。")
    if not developer_trial_switch_enabled(environment):
        blockers.append("LangGraph 组合试点开关未由开发者显式开启。")
    if not authorization.real_materials_authorized:
        blockers.append("真实材料尚未获得本次开发者试点授权。")
    if not authorization.real_model_authorized:
        blockers.append("真实模型尚未获得本次开发者试点授权。")
    return LangGraphCompositionTrialDecision(
        admitted=not blockers,
        blockers=tuple(blockers),
        plan_digest=plan_digest,
    )


def authorize_composition_developer_trial(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
    authorization: LangGraphCompositionTrialAuthorization,
    environment: Mapping[str, str] | None = None,
) -> LangGraphCompositionTrialAuthorizationRecord:
    """登记候选运行前的授权判断；它不会替代运行后的最终准入。"""

    decision = evaluate_composition_developer_trial_authorization(
        plan=plan,
        authorization=authorization,
        environment=environment,
    )
    now = _now()
    record = LangGraphCompositionTrialAuthorizationRecord(
        runtime_task_id=runtime_task_id,
        plan_digest=decision.plan_digest,
        status="authorized" if decision.admitted else "rejected",
        authorization=authorization,
        blockers=decision.blockers,
        created_at=now,
        updated_at=now,
    )
    return save_langgraph_composition_trial_authorization(record)


def revoke_composition_developer_trial_authorization(
    runtime_task_id: str,
) -> LangGraphCompositionTrialAuthorizationRecord:
    """撤销尚未开始的候选运行授权，不影响既有 Native Runtime。"""

    return revoke_langgraph_composition_trial_authorization(runtime_task_id)


def require_composition_developer_trial_authorization(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
    environment: Mapping[str, str] | None = None,
) -> LangGraphCompositionTrialAuthorizationRecord:
    """候选 Graph 创建前重新核验计划、开关和双重真实资源授权。"""

    record = load_langgraph_composition_trial_authorization(runtime_task_id)
    if record is None:
        raise CommanderCompositionShadowError("当前 Runtime 没有开发者预授权；请继续使用 Native Runtime。")
    if record.status != "authorized":
        raise CommanderCompositionShadowError("当前开发者预授权未通过或已撤销；请继续使用 Native Runtime。")
    decision = evaluate_composition_developer_trial_authorization(
        plan=plan,
        authorization=record.authorization,
        environment=environment,
    )
    if not decision.admitted or decision.plan_digest != record.plan_digest:
        raise CommanderCompositionShadowError("开发者预授权已失效；请停止试点并按 Native 路线重试。")
    return record


def observe_composition_developer_trial(
    *,
    plan: WorkflowPlan,
    native_run: WorkflowRun,
    graph_result: CommanderCompositionShadowResult,
    graph_run: WorkflowRun,
    native_artifacts: list[WorkflowArtifact],
    graph_artifacts: list[WorkflowArtifact],
    native_tool_calls: list[WorkflowToolCall],
    graph_tool_calls: list[WorkflowToolCall],
    native_events: list[TaskLogEvent],
    graph_events: list[TaskLogEvent],
    authorization: LangGraphCompositionTrialAuthorization,
    native_resources: LangGraphCompositionTrialResourceMeasurement,
    graph_resources: LangGraphCompositionTrialResourceMeasurement,
    recovery: LangGraphCompositionTrialRecoveryObservation,
    native_retry: LangGraphCompositionTrialRetryObservation,
) -> LangGraphCompositionTrialObservation:
    """把已完成的真实试点审计投影转换为不可含正文的准入证据。

    该函数不运行模型、不读取文件，也不写 SQLite。它只接受已经保存的 Runtime 结果与精简
    审计对象，因此真实试点不能靠调用方手填“对照已通过”的布尔值。
    """

    _invocations, plan_digest = build_composition_invocations(plan)
    report = compare_native_composition_execution(
        plan=plan,
        native_run=native_run,
        shadow_execution=graph_result,
    )
    if authorization.material_scope_digest == plan_digest:
        raise ValueError("材料范围摘要不能复用计划摘要。")
    scoped_steps = _composition_scope_step_ids(plan)
    tool_calls_match = _tool_call_signature(native_tool_calls, scoped_steps) == _tool_call_signature(
        graph_tool_calls,
        scoped_steps,
    )
    artifacts_match = _artifact_signature(native_artifacts, scoped_steps) == _artifact_signature(
        graph_artifacts,
        scoped_steps,
    )
    event_projection_match = _event_projection_signature(
        native_events,
        scoped_steps,
        native_run.status,
    ) == _event_projection_signature(graph_events, scoped_steps, graph_run.status)
    delivery_projection_match = _delivery_projection_signature(
        native_run,
        native_artifacts,
        scoped_steps,
    ) == _delivery_projection_signature(
        graph_run,
        graph_artifacts,
        scoped_steps,
    )
    evidence = LangGraphCompositionTrialEvidence(
        evidence_origin="developer_authorized_live",
        approval_reference=authorization.approval_reference,
        comparison_reference=_comparison_reference(report),
        plan_digest=plan_digest,
        material_scope_digest=authorization.material_scope_digest,
        model_profile_digest=authorization.model_profile_digest,
        native_reference_id=authorization.native_reference_id,
        graph_reference_id=authorization.graph_reference_id,
        composition_comparison_passed=report.outcome == "passed" and tool_calls_match,
        event_delivery_comparison_passed=event_projection_match and delivery_projection_match,
        source_artifact_comparison_passed=artifacts_match
        and _result_fact_signature(native_run, scoped_steps)
        == _result_fact_signature(graph_run, scoped_steps),
        recovery_semantics_passed=recovery.passed(),
        native_retry_route_verified=native_retry.passed(),
        real_materials_authorized=authorization.real_materials_authorized,
        real_model_authorized=authorization.real_model_authorized,
        native_startup_ms=native_resources.startup_ms,
        graph_startup_ms=graph_resources.startup_ms,
        native_resident_memory_mib=native_resources.resident_memory_mib,
        graph_resident_memory_mib=graph_resources.resident_memory_mib,
    )
    return LangGraphCompositionTrialObservation(
        evidence=evidence,
        composition_report=report,
        tool_calls_match=tool_calls_match,
        artifacts_match=artifacts_match,
        event_projection_match=event_projection_match,
        delivery_projection_match=delivery_projection_match,
    )


def _composition_scope_step_ids(plan: WorkflowPlan) -> frozenset[str]:
    specialists = {
        step.id for step in plan.steps if step.parallel_group == "specialist_read_only"
    }
    synthesis = next(
        (
            step.id
            for step in plan.steps
            if step.agent == "commander_agent" and step.action == "synthesize_results"
        ),
        "",
    )
    return frozenset((*specialists, synthesis) if synthesis else specialists)


def _tool_call_signature(
    tool_calls: list[WorkflowToolCall],
    scoped_steps: frozenset[str],
) -> tuple[tuple[str, str, str, str, int, bool], ...]:
    """比较 Tool 的可审计结构，不读取请求参数、结果正文或错误文本。"""

    return tuple(
        sorted(
            (
                call.step_id,
                call.agent_id,
                call.tool_name,
                call.status,
                call.failure_count,
                call.permission_required,
            )
            for call in tool_calls
            if call.step_id in scoped_steps
        )
    )


def _artifact_signature(
    artifacts: list[WorkflowArtifact],
    scoped_steps: frozenset[str],
) -> tuple[tuple[str, str, str, str], ...]:
    """比较交付物类型与来源步骤，刻意忽略名称、URI、路径和正文。"""

    return tuple(
        sorted(
            (artifact.step_id, artifact.agent_id, artifact.kind, artifact.mime_type.lower())
            for artifact in artifacts
            if artifact.step_id in scoped_steps
        )
    )


def _event_projection_signature(
    events: list[TaskLogEvent],
    scoped_steps: frozenset[str],
    terminal_status: str,
) -> tuple[str, tuple[tuple[str, str, str, str], ...]]:
    """只保留客户状态投影相关事件，不把顺序号、消息或内部日志文本纳入对照。"""

    step_events = tuple(
        sorted(
            (event.event, event.agent_id, event.step_id or "", event.level)
            for event in events
            if event.step_id in scoped_steps
            and event.event in {"step_completed", "step_blocked", "step_failed"}
        )
    )
    return terminal_status, step_events


def _result_fact_signature(
    run: WorkflowRun,
    scoped_steps: frozenset[str],
) -> tuple[tuple[str, int, int, int], ...]:
    """比较来源、图表与表格数量事实，不带专业结论或来源文本。"""

    values: list[tuple[str, int, int, int]] = []
    for step in run.steps:
        if step.step_id not in scoped_steps:
            continue
        result = step.output.get("result") if isinstance(step.output, dict) else None
        result = result if isinstance(result, dict) else {}
        verification = result.get("verification")
        verification = verification if isinstance(verification, dict) else {}
        values.append(
            (
                step.step_id,
                _nonnegative_int(result.get("source_count")),
                max(
                    _nonnegative_int(result.get("chart_count")),
                    _nonnegative_int(verification.get("chart_count")),
                ),
                max(
                    _nonnegative_int(result.get("table_count")),
                    _nonnegative_int(verification.get("table_count")),
                ),
            )
        )
    return tuple(sorted(values))


def _delivery_projection_signature(
    run: WorkflowRun,
    artifacts: list[WorkflowArtifact],
    scoped_steps: frozenset[str],
) -> tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, str, str, str], ...], tuple[tuple[str, int, int, int], ...]]:
    """用状态、步骤、产物形态和数量事实代表客户交付投影。"""

    step_states = tuple(
        sorted((step.step_id, step.status) for step in run.steps if step.step_id in scoped_steps)
    )
    return (
        run.status,
        step_states,
        _artifact_signature(artifacts, scoped_steps),
        _result_fact_signature(run, scoped_steps),
    )


def _comparison_reference(report: CommanderCompositionComparisonReport) -> str:
    """由对照身份生成不透明引用，避免把 task 标识扩散进准入证据正文。"""

    source = "|".join(
        (
            report.native_task_id,
            report.shadow_task_id,
            report.outcome,
            ",".join(report.native_completed_step_ids),
            ",".join(report.shadow_completed_step_ids),
        )
    )
    return f"comparison-{sha256(source.encode('utf-8')).hexdigest()[:24]}"


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def evaluate_composition_trial_admission(
    *,
    plan: WorkflowPlan,
    evidence: LangGraphCompositionTrialEvidence,
    environment: Mapping[str, str] | None = None,
) -> LangGraphCompositionTrialDecision:
    """验证 LGM5.7 的所有试点前置条件，不写数据库、不派发图。"""

    blockers: list[str] = []
    try:
        _invocations, plan_digest = build_composition_invocations(plan)
    except CommanderCompositionShadowError:
        plan_digest = ""
        blockers.append("任务没有通过 C6.4 受控只读组合计划准入。")
    if plan_digest and evidence.plan_digest != plan_digest:
        blockers.append("试点证据与当前已批准计划摘要不一致。")
    if not supports_native_read_only_composition_runtime(plan):
        blockers.append("当前计划超出开发者试点允许的只读 Agent/action 边界。")
    if not developer_trial_switch_enabled(environment):
        blockers.append("LangGraph 组合试点开关未由开发者显式开启。")
    if evidence.evidence_origin != "developer_authorized_live":
        blockers.append("确定性夹具不能替代真实材料与模型的开发者验收。")
    if not (evidence.real_materials_authorized and evidence.real_model_authorized):
        blockers.append("真实材料或模型尚未获得本次开发者试点授权。")
    if not evidence.composition_comparison_passed:
        blockers.append("Native/Graph 专业调用集合与汇总对照尚未通过。")
    if not evidence.event_delivery_comparison_passed:
        blockers.append("事件与客户交付投影对照尚未通过。")
    if not evidence.source_artifact_comparison_passed:
        blockers.append("来源与受控产物对照尚未通过。")
    if not evidence.recovery_semantics_passed:
        blockers.append("中断恢复语义对照尚未通过。")
    if not evidence.native_retry_route_verified:
        blockers.append("bridge/Graph/父 checkpoint 故障后的 Native 重试路线尚未验证。")
    if evidence.graph_startup_ms > _within_budget(evidence.native_startup_ms):
        blockers.append("LangGraph 启动基线超过 Native 参考值 10%。")
    if evidence.graph_resident_memory_mib > _within_budget(evidence.native_resident_memory_mib):
        blockers.append("LangGraph 常驻内存基线超过 Native 参考值 10%。")
    return LangGraphCompositionTrialDecision(
        admitted=not blockers,
        blockers=tuple(blockers),
        plan_digest=plan_digest,
    )


def register_composition_developer_trial(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
    evidence: LangGraphCompositionTrialEvidence,
    environment: Mapping[str, str] | None = None,
) -> LangGraphCompositionTrialAdmissionRecord:
    """登记一次试点准入判断；被拒绝的记录同样可追溯。"""

    decision = evaluate_composition_trial_admission(
        plan=plan,
        evidence=evidence,
        environment=environment,
    )
    now = _now()
    record = LangGraphCompositionTrialAdmissionRecord(
        runtime_task_id=runtime_task_id,
        plan_digest=decision.plan_digest or evidence.plan_digest,
        status="admitted" if decision.admitted else "rejected",
        evidence=evidence,
        blockers=decision.blockers,
        created_at=now,
        updated_at=now,
    )
    return save_langgraph_composition_trial_admission(record)


def revoke_composition_developer_trial(
    runtime_task_id: str,
) -> LangGraphCompositionTrialAdmissionRecord:
    """暴露单一、可审计的开始前撤销入口。"""

    return revoke_langgraph_composition_trial_admission(runtime_task_id)


def require_composition_developer_trial(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
    environment: Mapping[str, str] | None = None,
) -> LangGraphCompositionTrialAdmissionRecord:
    """在 Graph 创建前重新校验记录、计划和可撤回环境开关。"""

    record = load_langgraph_composition_trial_admission(runtime_task_id)
    if record is None:
        raise CommanderCompositionShadowError("当前 Runtime 没有开发者试点准入记录；请继续使用 Native Runtime。")
    if record.status != "admitted":
        raise CommanderCompositionShadowError("当前开发者试点未获准或已撤销；请继续使用 Native Runtime。")
    decision = evaluate_composition_trial_admission(
        plan=plan,
        evidence=record.evidence,
        environment=environment,
    )
    if not decision.admitted or decision.plan_digest != record.plan_digest:
        raise CommanderCompositionShadowError("开发者试点准入已失效；请停止试点并按 Native 路线重试。")
    return record


class LangGraphCompositionDeveloperTrialRunner:
    """仅供开发者脚本调用的试点执行守卫。

    它不会自动接管客户任务：调用方必须先显式创建准入记录。协调器报错时不偷偷改走
    Native，而是返回明确的 ``native_retry_required``，让原 Runtime 路线重新执行。
    """

    def __init__(
        self,
        *,
        runtime_task_id: str,
        plan: WorkflowPlan,
        coordinator_factory: Callable[[], LangGraphCompositionParentCoordinator],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runtime_task_id = runtime_task_id
        self._plan = plan
        self._coordinator_factory = coordinator_factory
        self._environment = environment

    async def execute(self, *, resume: bool = False) -> LangGraphCompositionTrialExecutionResult:
        require_composition_developer_trial(
            runtime_task_id=self._runtime_task_id,
            plan=self._plan,
            environment=self._environment,
        )
        try:
            result = await self._coordinator_factory().execute(resume=resume)
        except Exception:
            return LangGraphCompositionTrialExecutionResult(
                status="stopped",
                runtime_task_id=self._runtime_task_id,
                native_retry_required=True,
                message="LangGraph 试点已停止；请从当前 Runtime 任务按 Native 路线重试。",
            )
        return LangGraphCompositionTrialExecutionResult(
            status="completed",
            runtime_task_id=self._runtime_task_id,
            coordinator_result=result,
            message="开发者试点已完成；客户默认 Runtime 路线未改变。",
        )


class LangGraphCompositionDeveloperTrialCandidateRunner:
    """仅用于收集对照证据的候选 Graph 运行守卫。

    候选运行只要求预授权，不会把它误报为“最终准入”。协调器故障后仍必须停止，并由
    原 Runtime 显式按 Native 路线重试。没有 Router、API 或 Qt 会构造这个对象。
    """

    def __init__(
        self,
        *,
        runtime_task_id: str,
        plan: WorkflowPlan,
        coordinator_factory: Callable[[], LangGraphCompositionParentCoordinator],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runtime_task_id = runtime_task_id
        self._plan = plan
        self._coordinator_factory = coordinator_factory
        self._environment = environment

    async def execute(self, *, resume: bool = False) -> LangGraphCompositionTrialExecutionResult:
        require_composition_developer_trial_authorization(
            runtime_task_id=self._runtime_task_id,
            plan=self._plan,
            environment=self._environment,
        )
        try:
            result = await self._coordinator_factory().execute(resume=resume)
        except Exception:
            return LangGraphCompositionTrialExecutionResult(
                status="stopped",
                runtime_task_id=self._runtime_task_id,
                native_retry_required=True,
                message="LangGraph 候选运行已停止；请从当前 Runtime 任务按 Native 路线重试。",
            )
        return LangGraphCompositionTrialExecutionResult(
            status="completed",
            runtime_task_id=self._runtime_task_id,
            coordinator_result=result,
            message="LangGraph 候选运行已完成；尚待对照审计决定是否最终准入。",
        )


def _within_budget(native_value: int) -> int:
    return math.floor(native_value * _MAX_BASELINE_REGRESSION_RATIO)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
