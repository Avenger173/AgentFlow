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

from app.database.langgraph_trial_repository import (
    load_langgraph_composition_trial_admission,
    revoke_langgraph_composition_trial_admission,
    save_langgraph_composition_trial_admission,
)
from app.harness.langgraph_commander_composition_parent_coordinator import (
    LangGraphCompositionCoordinatorResult,
    LangGraphCompositionParentCoordinator,
)
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionShadowError,
    build_composition_invocations,
)
from app.schemas.chat import WorkflowPlan
from app.schemas.langgraph_trial import (
    LangGraphCompositionTrialAdmissionRecord,
    LangGraphCompositionTrialEvidence,
)
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


def _within_budget(native_value: int) -> int:
    return math.floor(native_value * _MAX_BASELINE_REGRESSION_RATIO)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
