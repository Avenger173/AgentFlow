"""LGM5.4 组合任务业务 Adapter 的主库回读边界。

LangGraph invocation 只携带哈希摘要，不能作为业务输入。每次专业调用都必须以 Runtime 任务 ID
从 AgentFlow 主库重新取得已经批准的计划，复核图身份、计划摘要与 invocation 后才交给受限
执行器。该模块不注册 RuntimeRouter；客户任务尚不会经过这里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.database.langgraph_bridge_repository import load_langgraph_composition_bridge
from app.database.task_repository import load_workflow_plan
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionInvocation,
    CommanderCompositionOutcome,
    build_composition_invocations,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep


CompositionBusinessExecutor = Callable[
    [str, WorkflowStep, WorkflowPlan],
    Awaitable[CommanderCompositionOutcome],
]


class AgentFlowCompositionBusinessAdapter:
    """从主任务恢复受控步骤的单 invocation Adapter。

    执行器将来会复用现有 Runtime 专业步骤委派；它的完整子任务、artifact、事件和客户结论
    必须继续写入 AgentFlow 现有存储。这里返回给图的只有状态、关联子任务 ID、数量事实和固定
    摘要，避免 Graph checkpoint 成为第二份业务记录。
    """

    def __init__(
        self,
        *,
        runtime_task_id: str,
        execute_approved_step: CompositionBusinessExecutor,
    ) -> None:
        self._runtime_task_id = runtime_task_id
        self._execute_approved_step = execute_approved_step

    async def __call__(
        self,
        invocation: CommanderCompositionInvocation,
    ) -> CommanderCompositionOutcome:
        plan = load_workflow_plan(self._runtime_task_id)
        bridge = load_langgraph_composition_bridge(self._runtime_task_id)
        if plan is None or bridge is None:
            return _failed(invocation, "主任务或组合 bridge 不存在，不能派发专业步骤。")
        if bridge.status != "running":
            return _failed(invocation, "组合 bridge 当前未处于可执行状态。")

        try:
            expected_invocations, plan_digest = build_composition_invocations(plan)
        except Exception:
            return _failed(invocation, "主任务计划没有通过当前组合 Runtime 准入。")
        if bridge.plan_digest != plan_digest:
            return _failed(invocation, "主任务计划摘要与已保存组合 bridge 不一致。")

        expected = next(
            (item for item in expected_invocations if item.invocation_id == invocation.invocation_id),
            None,
        )
        if expected != invocation:
            return _failed(invocation, "组合 invocation 与主任务已批准步骤不一致。")
        step = next((item for item in plan.steps if item.id == invocation.step_id), None)
        if step is None or (step.agent, step.action) != (invocation.agent_id, invocation.action):
            return _failed(invocation, "组合 invocation 找不到对应的已批准专业步骤。")

        try:
            receipt = await self._execute_approved_step(self._runtime_task_id, step, plan)
        except Exception:
            return _failed(invocation, "专业步骤在受控业务执行器中没有返回可用结果。")
        if receipt.invocation_id != invocation.invocation_id:
            return _failed(invocation, "专业步骤返回了不匹配的 invocation 标识。")
        return _normalize_receipt(invocation=invocation, receipt=receipt)


def _normalize_receipt(
    *,
    invocation: CommanderCompositionInvocation,
    receipt: CommanderCompositionOutcome,
) -> CommanderCompositionOutcome:
    """将业务回执收束为 Graph 可保存的最小状态，不复制专业结论。"""

    if receipt.status == "completed":
        summary = "一项已批准的专业步骤已完成；完整结果保留在关联任务交付中。"
        recovery_hint = ""
    elif receipt.status == "blocked":
        summary = "一项专业步骤正在等待处理；其它独立步骤可继续。"
        recovery_hint = "完成所需处理后可从已保存检查点恢复未完成专业步骤。"
    else:
        summary = "一项专业步骤未完成；其它独立步骤可继续。"
        recovery_hint = "可从已保存检查点恢复未完成专业步骤。"
    return CommanderCompositionOutcome(
        invocation_id=invocation.invocation_id,
        status=receipt.status,
        summary=summary,
        delegated_task_id=receipt.delegated_task_id[:100],
        source_count=_nonnegative_or_none(receipt.source_count),
        chart_count=_nonnegative_or_none(receipt.chart_count),
        table_count=_nonnegative_or_none(receipt.table_count),
        recovery_hint=recovery_hint,
    )


def _failed(
    invocation: CommanderCompositionInvocation,
    reason: str,
) -> CommanderCompositionOutcome:
    return CommanderCompositionOutcome(
        invocation_id=invocation.invocation_id,
        status="failed",
        summary="当前专业步骤未能通过组合业务 Adapter 的主库复核。",
        recovery_hint=reason,
    )


def _nonnegative_or_none(value: int | None) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
