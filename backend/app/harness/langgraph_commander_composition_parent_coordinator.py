"""LGM5.6 组合图的单父任务协调器。

LangGraph 只调度已批准的 invocation；专业子任务仍经既有 Native handoff 创建和持久化。
Graph 分支绝不能并发写入父 Runtime 的 SQLite 快照，因此本模块先收集每条 Native 回执，
再由单一协调器按计划顺序合并步骤、工具调用、产物与 append-only 事件。

该模块没有 RuntimeRouter、API 或 Qt 注册入口，只服务于离线对照和未来默认关闭的开发试点。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from app.database.langgraph_bridge_repository import ensure_langgraph_composition_bridge
from app.database.task_repository import (
    append_workflow_event,
    list_runtime_permission_requests,
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_workflow_plan,
    load_workflow_run,
    save_workflow_runtime_checkpoint,
)
from app.harness.langgraph_commander_composition_bridge import (
    build_composition_bridge_record,
    mark_composition_bridge_running,
    sync_composition_bridge_result,
)
from app.harness.langgraph_commander_composition_business_adapter import AgentFlowCompositionBusinessAdapter
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionInvocation,
    CommanderCompositionOutcome,
    CommanderCompositionShadowError,
    CommanderCompositionShadowResult,
    LangGraphCommanderCompositionShadowBackend,
    build_composition_invocations,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.workflow import WorkflowArtifact, WorkflowRun, WorkflowStepRun, WorkflowToolCall
from app.workflow import runtime as native_runtime


NativeStepExecutor = Callable[
    [str, WorkflowStep, WorkflowPlan, Path],
    tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]],
]
NativeFallback = Callable[[str, WorkflowPlan], Awaitable[WorkflowRun]]


@dataclass(frozen=True)
class NativeCompositionStepReceipt:
    """一条尚未合并到父任务的 Native 专业步骤回执。"""

    invocation_id: str
    step_id: str
    step_run: WorkflowStepRun
    tool_call: WorkflowToolCall
    artifacts: tuple[WorkflowArtifact, ...]


@dataclass(frozen=True)
class LangGraphCompositionCoordinatorResult:
    """协调器的内部结果；客户仍只通过既有 Runtime/DeliveryCard 读取最终交付。"""

    runtime_task_id: str
    graph_result: CommanderCompositionShadowResult
    workflow_run: WorkflowRun
    used_native_fallback: bool = False


class NativeCompositionStepCollector:
    """复用 Native 专业步骤 executor，但延后父任务快照写入。

    真正的文档、数据和知识库子任务照常维护自己的 task/trace/artifact。唯一被延后的
    是父 Runtime 的聚合快照，这样 Graph 的动态并行不会出现多个线程删写同一份父步骤表。
    """

    def __init__(
        self,
        *,
        runtime_task_id: str,
        plan: WorkflowPlan,
        output_dir: Path,
        execute_step: NativeStepExecutor | None = None,
    ) -> None:
        self._runtime_task_id = runtime_task_id
        self._plan = plan
        self._output_dir = output_dir
        self._execute_step = execute_step or _execute_native_step
        self._receipts: dict[str, NativeCompositionStepReceipt] = {}
        self._lock = Lock()

    async def __call__(
        self,
        runtime_task_id: str,
        step: WorkflowStep,
        plan: WorkflowPlan,
    ) -> CommanderCompositionOutcome:
        if runtime_task_id != self._runtime_task_id or plan.plan_id != self._plan.plan_id:
            return CommanderCompositionOutcome(
                invocation_id="",
                status="failed",
                summary="Native 专业步骤执行器收到不匹配的父任务或计划。",
            )
        invocation_id = _invocation_id_for_step(plan=plan, step=step)
        if not invocation_id:
            return CommanderCompositionOutcome(
                invocation_id="",
                status="failed",
                summary="已批准步骤无法映射到组合 invocation。",
            )
        try:
            step_run, tool_call, artifacts = await asyncio.to_thread(
                self._execute_step,
                runtime_task_id,
                step,
                plan,
                self._output_dir,
            )
        except Exception:
            return CommanderCompositionOutcome(
                invocation_id=invocation_id,
                status="failed",
                summary="Native 专业步骤没有返回可合并回执。",
                recovery_hint="可从已保存调用键恢复未完成步骤。",
            )
        receipt = NativeCompositionStepReceipt(
            invocation_id=invocation_id,
            step_id=step.id,
            step_run=step_run,
            tool_call=tool_call,
            artifacts=tuple(artifacts),
        )
        with self._lock:
            self._receipts[invocation_id] = receipt
        return _outcome_from_receipt(invocation_id=invocation_id, receipt=receipt)

    def receipts(self) -> tuple[NativeCompositionStepReceipt, ...]:
        """按已批准计划顺序返回本轮应由父协调器提交的回执。"""

        invocation_order = [item.invocation_id for item in build_composition_invocations(self._plan)[0]]
        with self._lock:
            return tuple(self._receipts[item] for item in invocation_order if item in self._receipts)

    def has_receipts(self) -> bool:
        with self._lock:
            return bool(self._receipts)


class LangGraphCompositionParentCoordinator:
    """以单写入者把 Graph 调度结果投影回 AgentFlow 主任务。"""

    def __init__(
        self,
        *,
        runtime_task_id: str,
        checkpoint_path: Path,
        output_dir: Path,
        execute_step: NativeStepExecutor | None = None,
        native_fallback: NativeFallback | None = None,
    ) -> None:
        self._runtime_task_id = runtime_task_id
        self._checkpoint_path = checkpoint_path
        self._output_dir = output_dir
        self._execute_step = execute_step
        self._native_fallback = native_fallback

    async def execute(self, *, resume: bool = False) -> LangGraphCompositionCoordinatorResult:
        plan, current_run = _load_approved_parent(self._runtime_task_id)
        collector = NativeCompositionStepCollector(
            runtime_task_id=self._runtime_task_id,
            plan=plan,
            output_dir=self._output_dir,
            execute_step=self._execute_step,
        )
        bridge = ensure_langgraph_composition_bridge(
            build_composition_bridge_record(runtime_task_id=self._runtime_task_id, plan=plan)
        )
        if bridge.status not in {"prepared", "partial", "blocked", "running"}:
            raise CommanderCompositionShadowError("当前组合 bridge 已处于不可恢复终态。")
        mark_composition_bridge_running(runtime_task_id=self._runtime_task_id)

        graph = LangGraphCommanderCompositionShadowBackend(
            checkpoint_path=self._checkpoint_path,
            adapters={
                (invocation.agent_id, invocation.action): AgentFlowCompositionBusinessAdapter(
                    runtime_task_id=self._runtime_task_id,
                    execute_approved_step=collector,
                )
                for invocation in build_composition_invocations(plan)[0]
            },
        )
        try:
            graph_result = (
                await graph.resume_task(task_id=self._runtime_task_id, plan=plan)
                if resume
                else await graph.execute_task(task_id=self._runtime_task_id, plan=plan)
            )
        except CommanderCompositionShadowError:
            if self._native_fallback is None or collector.has_receipts():
                _mark_bridge_failed(self._runtime_task_id)
                raise
            fallback_run = await self._native_fallback(self._runtime_task_id, plan)
            _mark_bridge_failed(self._runtime_task_id)
            return LangGraphCompositionCoordinatorResult(
                runtime_task_id=self._runtime_task_id,
                graph_result=_fallback_graph_result(self._runtime_task_id, plan),
                workflow_run=fallback_run,
                used_native_fallback=True,
            )
        finally:
            await graph.close()

        try:
            merged_run = _merge_parent_receipts(
                runtime_task_id=self._runtime_task_id,
                plan=plan,
                current_run=current_run,
                receipts=collector.receipts(),
                graph_result=graph_result,
            )
        except Exception as exc:
            # Graph checkpoint 已经存在，但父任务不能确认交付；明确收束 bridge，避免后续
            # 调度把“图已跑完”错误呈现为客户任务已完成。
            _mark_bridge_failed(self._runtime_task_id)
            raise CommanderCompositionShadowError("组合父任务检查点合并失败，未确认客户交付。") from exc
        sync_composition_bridge_result(runtime_task_id=self._runtime_task_id, result=graph_result)
        return LangGraphCompositionCoordinatorResult(
            runtime_task_id=self._runtime_task_id,
            graph_result=graph_result,
            workflow_run=merged_run,
        )


def _execute_native_step(
    runtime_task_id: str,
    step: WorkflowStep,
    plan: WorkflowPlan,
    output_dir: Path,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    return native_runtime._execute_safe_step_with_retries(
        runtime_task_id=runtime_task_id,
        step=step,
        plan=plan,
        output_dir=output_dir,
        runtime_context={},
    )


def _load_approved_parent(runtime_task_id: str) -> tuple[WorkflowPlan, WorkflowRun]:
    plan = load_workflow_plan(runtime_task_id)
    run = load_workflow_run(runtime_task_id)
    if plan is None or run is None or run.mode != "runtime":
        raise CommanderCompositionShadowError("LGM5.6 只能处理已创建的 AgentFlow Runtime 任务。")
    if not native_runtime.supports_native_read_only_composition_runtime(plan):
        raise CommanderCompositionShadowError("父任务没有通过 C6.4 只读组合准入。")
    root_step = plan.steps[0]
    parent_root = next((item for item in run.steps if item.step_id == root_step.id), None)
    if parent_root is None or parent_root.status != "completed":
        raise CommanderCompositionShadowError("父任务的规划步骤尚未完成，不能提前进入组合图。")
    return plan, run


def _invocation_id_for_step(*, plan: WorkflowPlan, step: WorkflowStep) -> str:
    return next(
        (item.invocation_id for item in build_composition_invocations(plan)[0] if item.step_id == step.id),
        "",
    )


def _outcome_from_receipt(
    *,
    invocation_id: str,
    receipt: NativeCompositionStepReceipt,
) -> CommanderCompositionOutcome:
    result = receipt.step_run.output.get("result")
    result = result if isinstance(result, dict) else {}
    if receipt.step_run.status == "completed":
        status = "completed"
    elif receipt.step_run.status == "blocked":
        status = "blocked"
    else:
        status = "failed"
    return CommanderCompositionOutcome(
        invocation_id=invocation_id,
        status=status,
        summary=receipt.step_run.message,
        delegated_task_id=str(result.get("delegated_task_id", "")),
        source_count=_nonnegative_int_or_none(result.get("source_count")),
        chart_count=_nonnegative_int_or_none(result.get("chart_count")),
        table_count=_nonnegative_int_or_none(result.get("table_count")),
        recovery_hint="可从已保存调用键恢复未完成步骤。" if status != "completed" else "",
    )


def _merge_parent_receipts(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
    current_run: WorkflowRun,
    receipts: tuple[NativeCompositionStepReceipt, ...],
    graph_result: CommanderCompositionShadowResult,
) -> WorkflowRun:
    """在当前协调线程合并本轮回执，Graph 分支永不直接改写父快照。"""

    step_states = {item.step_id: item for item in current_run.steps}
    artifacts = {item.artifact_id: item for item in list_workflow_artifacts(runtime_task_id)}
    tool_calls = {item.call_id: item for item in list_workflow_tool_calls(runtime_task_id)}
    for receipt in receipts:
        step_states[receipt.step_id] = receipt.step_run
        artifacts.update({item.artifact_id: item for item in receipt.artifacts})
        tool_calls[receipt.tool_call.call_id] = receipt.tool_call

    specialists = [item for item in plan.steps if item.parallel_group == "specialist_read_only"]
    completed = [item for item in specialists if step_states.get(item.id) is not None and step_states[item.id].status == "completed"]
    unavailable = [item for item in specialists if item not in completed]
    synthesis = next(
        item for item in plan.steps if item.agent == "commander_agent" and item.action == "synthesize_results"
    )
    if completed:
        synthesis_run, synthesis_call = native_runtime._execute_composition_synthesis(
            runtime_task_id=runtime_task_id,
            step=synthesis,
            completed_steps=[(item, step_states[item.id]) for item in completed],
            unavailable_steps=[(item, step_states.get(item.id)) for item in unavailable],
        )
        step_states[synthesis.id] = synthesis_run
        tool_calls[synthesis_call.call_id] = synthesis_call
    else:
        step_states[synthesis.id] = native_runtime._pending_step(synthesis)

    if graph_result.status == "completed":
        status = "completed"
        summary = native_runtime._composition_parent_summary(completed, unavailable)
    elif completed:
        # Graph 的 partial 必须保持为可恢复状态，不能把父任务提前写成终态 completed。
        status = "blocked"
        summary = (
            f"组合任务部分完成：已汇总 {len(completed)} 项已完成专业结果；"
            f"{len(unavailable)} 项未完成，可从检查点继续。"
        )
    else:
        status = "blocked" if graph_result.status == "blocked" else "failed"
        summary = "组合任务没有可用于汇总的已完成专业结果；可从失败分支继续处理。"

    steps = [step_states.get(item.id, native_runtime._pending_step(item)) for item in plan.steps]
    permission_requests = native_runtime._build_permission_requests(runtime_task_id, plan)
    now = datetime.now(UTC)
    run = WorkflowRun(
        task_id=runtime_task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level=plan.max_risk_level,
        requires_confirmation=plan.requires_confirmation,
        validation_errors=[],
        steps=steps,
        limits=native_runtime._runtime_execution_limits(plan),
        metrics=native_runtime._build_runtime_metrics(
            steps=steps,
            tool_calls=list(tool_calls.values()),
            permission_requests=permission_requests,
            started_at=native_runtime._runtime_started_at(current_run),
            finished_at=now,
        ),
    )
    save_workflow_runtime_checkpoint(
        run=run,
        plan=plan,
        permission_requests=permission_requests,
        artifacts=list(artifacts.values()),
        tool_calls=list(tool_calls.values()),
    )
    for receipt in receipts:
        step = next(item for item in specialists if item.id == receipt.step_id)
        step_run = receipt.step_run
        event_name = "step_completed" if step_run.status == "completed" else "step_blocked" if step_run.status == "blocked" else "step_failed"
        append_workflow_event(
            task_id=runtime_task_id,
            event_name=event_name,
            agent_id=step.agent,
            step_id=step.id,
            message=step_run.message,
            level="info" if step_run.status == "completed" else "warning" if step_run.status == "blocked" else "error",
        )
    append_workflow_event(
        task_id=runtime_task_id,
        event_name="task_completed" if status == "completed" else "task_waiting" if status == "blocked" else "task_failed",
        agent_id="workflow_engine",
        message=summary,
        level="info" if status == "completed" else "warning" if status == "blocked" else "error",
    )
    return run


def _mark_bridge_failed(runtime_task_id: str) -> None:
    from app.database.langgraph_bridge_repository import transition_langgraph_composition_bridge

    transition_langgraph_composition_bridge(
        runtime_task_id=runtime_task_id,
        status="failed",
        delivery_state="failed",
    )


def _fallback_graph_result(runtime_task_id: str, plan: WorkflowPlan) -> CommanderCompositionShadowResult:
    _invocations, plan_digest = build_composition_invocations(plan)
    return CommanderCompositionShadowResult(
        task_id=runtime_task_id,
        status="failed",
        plan_digest=plan_digest,
        completed_invocation_ids=(),
        failed_invocation_ids=(),
        delivery={"status": "failed"},
    )


def _nonnegative_int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
