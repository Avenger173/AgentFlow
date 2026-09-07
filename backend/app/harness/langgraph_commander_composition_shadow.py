"""LGM5 的 Commander 只读组合任务 LangGraph 影子后端。

它只接受已经通过 C6.4 Native Runtime 准入的组合计划，验证 LangGraph 能否以同样的
材料边界表达并行、部分完成与恢复。它不注册 API、不改变 RuntimeRouter、不读取客户正文，
也不直接调用专业 Agent；业务桥接会在影子对照通过后单独接入。

Graph checkpoint 仅保存任务/计划摘要、步骤动作、材料引用摘要哈希与受控子结果摘要。模型
对象、原始提示、材料名、文档正文、DataFrame、绝对路径和 API Key 均不能进入本模块状态。
"""

from __future__ import annotations

import json
import operator
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Awaitable, Callable, Literal, TypedDict

from app.harness.contracts import HarnessEventSink, HarnessRuntimeEvent
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.workflow import WorkflowRun
from app.workflow.runtime import supports_native_read_only_composition_runtime


_GRAPH_ID = "commander_composition_shadow"
_GRAPH_VERSION = "v1"
_MAX_SUMMARY_LENGTH = 600

CompositionOutcomeStatus = Literal["completed", "failed", "blocked"]
CompositionRunStatus = Literal["completed", "partial", "failed", "blocked"]


@dataclass(frozen=True)
class CommanderCompositionInvocation:
    """一项获准专业调用的无正文图输入。"""

    invocation_id: str
    step_id: str
    agent_id: str
    action: str
    material_digest: str
    input_digest: str


@dataclass(frozen=True)
class CommanderCompositionOutcome:
    """专业调用写入图状态的受限回执。"""

    invocation_id: str
    status: CompositionOutcomeStatus
    summary: str
    delegated_task_id: str = ""
    source_count: int | None = None
    chart_count: int | None = None
    table_count: int | None = None
    recovery_hint: str = ""


CompositionSpecialistAdapter = Callable[
    [CommanderCompositionInvocation],
    Awaitable[CommanderCompositionOutcome],
]


class _CompositionState(TypedDict, total=False):
    """可进入 SQLite Checkpointer 的最小 JSON 状态。"""

    task_id: str
    graph_id: str
    graph_version: str
    plan_digest: str
    invocations: list[dict[str, str]]
    active_invocation: dict[str, str]
    outcomes: Annotated[dict[str, dict[str, object]], operator.or_]
    completed_nodes: Annotated[list[str], operator.add]
    delivery: dict[str, object]


@dataclass(frozen=True)
class CommanderCompositionShadowSnapshot:
    """给 LGM5 回归读取的最小检查点视图。"""

    task_id: str
    thread_id: str
    graph_id: str
    graph_version: str
    plan_digest: str
    completed_invocation_ids: tuple[str, ...]
    pending_invocation_ids: tuple[str, ...]
    failed_invocation_ids: tuple[str, ...]
    next_nodes: tuple[str, ...]


@dataclass(frozen=True)
class CommanderCompositionShadowResult:
    """影子图的受限结果，不替代客户最终交付协议。"""

    task_id: str
    status: CompositionRunStatus
    plan_digest: str
    completed_invocation_ids: tuple[str, ...]
    failed_invocation_ids: tuple[str, ...]
    delivery: dict[str, object]
    resumed: bool = False


@dataclass(frozen=True)
class CommanderCompositionComparisonReport:
    """Native 与 LGM5 影子组合执行的无正文一致性报告。"""

    native_task_id: str
    shadow_task_id: str
    outcome: Literal["passed", "failed"]
    native_completed_step_ids: tuple[str, ...]
    shadow_completed_step_ids: tuple[str, ...]
    native_unavailable_step_ids: tuple[str, ...]
    shadow_unavailable_step_ids: tuple[str, ...]
    native_delivery_state: str
    shadow_delivery_state: str
    blockers: tuple[str, ...] = ()


class CommanderCompositionShadowError(ValueError):
    """计划不符合 LGM5 影子图的窄准入边界。"""


class LangGraphCommanderCompositionShadowBackend:
    """以动态并行子节点对照 C6.4 的只读组合任务。

    ``adapters`` 是回归期的显式业务替身。它接收的只有 ``CommanderCompositionInvocation``；
    因此即使某个 adapter 以后接入正式 Agent，也必须从 AgentFlow 主任务存储按 task/step
    重新取得受控输入，不能依赖 Graph checkpoint 携带客户正文。
    """

    def __init__(
        self,
        *,
        checkpoint_path: Path,
        adapters: dict[tuple[str, str], CompositionSpecialistAdapter],
    ) -> None:
        self._checkpoint_path = checkpoint_path
        self._adapters = dict(adapters)
        self._checkpointer_context: object | None = None
        self._graph: object | None = None
        self._event_sink: HarnessEventSink | None = None
        self._closed = False

    async def execute_task(
        self,
        *,
        task_id: str,
        plan: WorkflowPlan,
        event_sink: HarnessEventSink | None = None,
    ) -> CommanderCompositionShadowResult:
        """创建或继续同一任务的影子图，首次执行从完整准入计划生成摘要输入。"""

        invocations, plan_digest = build_composition_invocations(plan)
        existing = await self.inspect_task(task_id)
        if existing is not None:
            if existing.plan_digest != plan_digest:
                raise CommanderCompositionShadowError("同一影子任务不能以不同计划版本恢复。")
            return await self._drive(
                task_id=task_id,
                graph_input={},
                plan_digest=plan_digest,
                event_sink=event_sink,
                resumed=True,
            )
        initial_state: _CompositionState = {
            "task_id": task_id,
            "graph_id": _GRAPH_ID,
            "graph_version": _GRAPH_VERSION,
            "plan_digest": plan_digest,
            "invocations": [asdict(item) for item in invocations],
            "outcomes": {},
        }
        return await self._drive(
            task_id=task_id,
            graph_input=initial_state,
            plan_digest=plan_digest,
            event_sink=event_sink,
            resumed=False,
        )

    async def resume_task(
        self,
        *,
        task_id: str,
        plan: WorkflowPlan,
        event_sink: HarnessEventSink | None = None,
    ) -> CommanderCompositionShadowResult:
        """从相同计划摘要恢复，只重新派发失败或尚未开始的专业调用。"""

        invocations, plan_digest = build_composition_invocations(plan)
        del invocations
        snapshot = await self.inspect_task(task_id)
        if snapshot is None:
            raise CommanderCompositionShadowError("没有找到可恢复的 LGM5 组合任务检查点。")
        if snapshot.graph_id != _GRAPH_ID or snapshot.graph_version != _GRAPH_VERSION:
            raise CommanderCompositionShadowError("检查点不属于当前 LGM5 组合图版本。")
        if snapshot.plan_digest != plan_digest:
            raise CommanderCompositionShadowError("恢复计划与原组合任务的受控摘要不一致。")
        return await self._drive(
            task_id=task_id,
            graph_input={},
            plan_digest=plan_digest,
            event_sink=event_sink,
            resumed=True,
        )

    async def inspect_task(self, task_id: str) -> CommanderCompositionShadowSnapshot | None:
        """读取不含客户正文的 checkpoint 摘要。"""

        graph = await self._ensure_graph()
        state = await graph.aget_state(_graph_config(task_id))
        values = state.values if state is not None else {}
        if not values or values.get("task_id") != task_id:
            return None
        outcomes = values.get("outcomes")
        outcomes = outcomes if isinstance(outcomes, dict) else {}
        completed = sorted(
            key
            for key, item in outcomes.items()
            if isinstance(item, dict) and item.get("status") == "completed"
        )
        failed = sorted(
            key
            for key, item in outcomes.items()
            if isinstance(item, dict) and item.get("status") != "completed"
        )
        invocation_ids = {
            str(item.get("invocation_id", ""))
            for item in values.get("invocations", [])
            if isinstance(item, dict)
        }
        pending = sorted(invocation_ids.difference(completed).difference(failed))
        return CommanderCompositionShadowSnapshot(
            task_id=task_id,
            thread_id=_thread_id(task_id),
            graph_id=str(values.get("graph_id", "")),
            graph_version=str(values.get("graph_version", "")),
            plan_digest=str(values.get("plan_digest", "")),
            completed_invocation_ids=tuple(completed),
            pending_invocation_ids=tuple(pending),
            failed_invocation_ids=tuple(failed),
            next_nodes=tuple(state.next),
        )

    async def close(self) -> None:
        """关闭独立 LangGraph SQLite 连接，不触碰 AgentFlow 主数据库。"""

        if self._closed:
            return
        self._closed = True
        context = self._checkpointer_context
        self._graph = None
        self._checkpointer_context = None
        if context is not None:
            await context.__aexit__(None, None, None)

    async def _drive(
        self,
        *,
        task_id: str,
        graph_input: _CompositionState | dict[str, object],
        plan_digest: str,
        event_sink: HarnessEventSink | None,
        resumed: bool,
    ) -> CommanderCompositionShadowResult:
        if self._closed:
            raise CommanderCompositionShadowError("LGM5 组合影子后端已经关闭。")
        graph = await self._ensure_graph()
        self._event_sink = event_sink
        try:
            await self._emit(
                "runtime_heartbeat" if resumed else "runtime_started",
                "组合任务正在从已批准的只读专业步骤构建执行状态。"
                if not resumed
                else "组合任务正在从已保存检查点恢复未完成专业步骤。",
            )
            await graph.ainvoke(graph_input, _graph_config(task_id))
            snapshot = await self.inspect_task(task_id)
            if snapshot is None or snapshot.plan_digest != plan_digest:
                raise CommanderCompositionShadowError("组合任务影子图没有保存可读取的有效检查点。")
            graph_state = await graph.aget_state(_graph_config(task_id))
            delivery = graph_state.values.get("delivery") if graph_state is not None else {}
            delivery = delivery if isinstance(delivery, dict) else {}
            if snapshot.completed_invocation_ids:
                status: CompositionRunStatus = "completed" if not snapshot.failed_invocation_ids else "partial"
            elif snapshot.failed_invocation_ids:
                status = "blocked" if delivery.get("blocked_count") else "failed"
            else:
                status = "failed"
            await self._emit(
                "assistant_final",
                "组合任务影子图已完成受控汇总。"
                if status == "completed"
                else "组合任务影子图已形成部分完成汇总；未完成分支可单独恢复。",
            )
            return CommanderCompositionShadowResult(
                task_id=task_id,
                status=status,
                plan_digest=plan_digest,
                completed_invocation_ids=snapshot.completed_invocation_ids,
                failed_invocation_ids=snapshot.failed_invocation_ids,
                delivery=delivery,
                resumed=resumed,
            )
        except Exception as exc:
            await self._emit("runtime_failed", "组合任务影子图发生未分类失败。")
            raise CommanderCompositionShadowError(f"LGM5 组合影子图执行失败：{type(exc).__name__}。") from exc
        finally:
            self._event_sink = None

    async def _ensure_graph(self):
        if self._graph is not None:
            return self._graph
        if self._closed:
            raise CommanderCompositionShadowError("LGM5 组合影子后端已经关闭。")
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        context = AsyncSqliteSaver.from_conn_string(str(self._checkpoint_path))
        checkpointer = await context.__aenter__()
        try:
            graph = _build_graph(self).compile(checkpointer=checkpointer, name="AgentFlowLgm5CommanderComposition")
        except Exception:
            await context.__aexit__(None, None, None)
            raise
        self._checkpointer_context = context
        self._graph = graph
        return self._graph

    async def _run_specialist(self, state: _CompositionState) -> dict[str, object]:
        payload = state.get("active_invocation")
        if not isinstance(payload, dict):
            return {}
        invocation = _invocation_from_payload(payload)
        await self._emit(
            "runtime_heartbeat",
            f"正在执行受控专业步骤：{invocation.agent_id}.{invocation.action}。",
        )
        adapter = self._adapters.get((invocation.agent_id, invocation.action))
        if adapter is None:
            outcome = CommanderCompositionOutcome(
                invocation_id=invocation.invocation_id,
                status="failed",
                summary="当前专业调用没有注册影子执行适配器。",
                recovery_hint="请检查该专业动作的影子适配器准入后重试。",
            )
        else:
            try:
                outcome = await adapter(invocation)
            except Exception:
                outcome = CommanderCompositionOutcome(
                    invocation_id=invocation.invocation_id,
                    status="failed",
                    summary="专业调用在影子执行中未返回可用结果。",
                    recovery_hint="该独立分支可在修复后单独恢复；其它已完成分支不会重跑。",
                )
        if outcome.invocation_id != invocation.invocation_id:
            outcome = CommanderCompositionOutcome(
                invocation_id=invocation.invocation_id,
                status="failed",
                summary="专业调用返回了不匹配的 invocation 标识。",
                recovery_hint="请检查适配器的调用关联关系后重试。",
            )
        result = _outcome_payload(outcome)
        if outcome.status == "completed":
            await self._emit("runtime_heartbeat", "一项独立专业步骤已完成，正在等待其它可并行步骤。")
        else:
            await self._emit("runtime_heartbeat", "一项专业步骤未完成，其它独立步骤会继续执行。")
        return {
            "outcomes": {invocation.invocation_id: result},
            "completed_nodes": [f"specialist:{invocation.invocation_id}"],
        }

    async def _emit(self, kind: str, message: str) -> None:
        if self._event_sink is not None:
            await self._event_sink(HarnessRuntimeEvent(kind=kind, message=message))


def build_composition_invocations(
    plan: WorkflowPlan,
) -> tuple[tuple[CommanderCompositionInvocation, ...], str]:
    """从 C6.4 准入计划构造无正文 invocation 集合与稳定摘要。"""

    if not supports_native_read_only_composition_runtime(plan):
        raise CommanderCompositionShadowError("LGM5 只接受已通过 C6.4 Native 只读组合准入的计划。")
    specialists = [step for step in plan.steps if step.parallel_group == "specialist_read_only"]
    material_digest = _stable_digest(
        [
            {"kind": item.kind, "ref": item.ref, "binding_id": item.binding_id}
            for item in sorted(plan.material_bindings, key=lambda item: item.binding_id)
        ]
    )
    invocations = tuple(
        CommanderCompositionInvocation(
            invocation_id=_stable_digest(
                {
                    "plan_id": plan.plan_id,
                    "plan_version": plan.plan_version,
                    "step_id": step.id,
                    "agent_id": step.agent,
                    "action": step.action,
                    "material_digest": material_digest,
                    "input_digest": _step_input_digest(step),
                }
            )[:24],
            step_id=step.id,
            agent_id=step.agent,
            action=step.action,
            material_digest=material_digest,
            input_digest=_step_input_digest(step),
        )
        for step in specialists
    )
    plan_digest = _stable_digest(
        {
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "schema_version": plan.schema_version,
            "material_digest": material_digest,
            "specialists": [asdict(item) for item in invocations],
            "synthesis_step": next(
                step.id
                for step in plan.steps
                if step.agent == "commander_agent" and step.action == "synthesize_results"
            ),
        }
    )
    return invocations, plan_digest


def compare_native_composition_execution(
    *,
    plan: WorkflowPlan,
    native_run: WorkflowRun,
    shadow_execution: CommanderCompositionShadowResult,
) -> CommanderCompositionComparisonReport:
    """比较同一只读组合计划的子任务完成集合与汇总边界。

    比较只使用已存的 step 状态、影子 invocation 摘要与汇总枚举，不读取子任务正文、模型
    回复或材料内容。它是未来业务桥接的准入事实，不会自行选择或切换任何客户 Runtime。
    """

    invocations, _plan_digest = build_composition_invocations(plan)
    invocation_by_id = {item.invocation_id: item for item in invocations}
    native_by_step_id = {step.step_id: step for step in native_run.steps}
    specialist_step_ids = {item.step_id for item in invocations}
    native_completed = tuple(
        sorted(
            step_id
            for step_id in specialist_step_ids
            if native_by_step_id.get(step_id) is not None
            and native_by_step_id[step_id].status == "completed"
        )
    )
    native_unavailable = tuple(sorted(specialist_step_ids.difference(native_completed)))
    shadow_completed = tuple(
        sorted(
            invocation_by_id[item_id].step_id
            for item_id in shadow_execution.completed_invocation_ids
            if item_id in invocation_by_id
        )
    )
    shadow_unavailable = tuple(
        sorted(
            invocation_by_id[item_id].step_id
            for item_id in shadow_execution.failed_invocation_ids
            if item_id in invocation_by_id
        )
    )
    synthesis = next(
        (
            step
            for step in native_run.steps
            if step.agent == "commander_agent" and step.action == "synthesize_results"
        ),
        None,
    )
    native_result = synthesis.output.get("result") if synthesis is not None else {}
    native_result = native_result if isinstance(native_result, dict) else {}
    native_delivery_state = str(native_result.get("completion_state", ""))
    shadow_delivery_state = str(shadow_execution.delivery.get("status", ""))
    blockers: list[str] = []
    if native_run.status != "completed":
        blockers.append("Native 组合父任务没有完成，不能作为影子对照基线。")
    if native_completed != shadow_completed:
        blockers.append("Native 与影子的已完成专业步骤集合不一致。")
    if native_unavailable != shadow_unavailable:
        blockers.append("Native 与影子的未完成专业步骤集合不一致。")
    if native_delivery_state != shadow_delivery_state:
        blockers.append("Native 与影子的部分完成汇总状态不一致。")
    if shadow_execution.delivery.get("result_scope") != "仅汇总已完成的受控专业调用；未完成分支不进入本次结论。":
        blockers.append("影子汇总范围没有保持只使用已完成分支的边界。")
    return CommanderCompositionComparisonReport(
        native_task_id=native_run.task_id,
        shadow_task_id=shadow_execution.task_id,
        outcome="passed" if not blockers else "failed",
        native_completed_step_ids=native_completed,
        shadow_completed_step_ids=shadow_completed,
        native_unavailable_step_ids=native_unavailable,
        shadow_unavailable_step_ids=shadow_unavailable,
        native_delivery_state=native_delivery_state,
        shadow_delivery_state=shadow_delivery_state,
        blockers=tuple(blockers),
    )


def _build_graph(backend: LangGraphCommanderCompositionShadowBackend):
    from langgraph.constants import Send
    from langgraph.graph import END, START, StateGraph

    def prepare(_state: _CompositionState) -> dict[str, object]:
        return {"completed_nodes": ["prepare"]}

    def dispatch(state: _CompositionState):
        outcomes = state.get("outcomes")
        outcomes = outcomes if isinstance(outcomes, dict) else {}
        pending = [
            item
            for item in state.get("invocations", [])
            if isinstance(item, dict)
            and str(outcomes.get(str(item.get("invocation_id", "")), {}).get("status", "")) != "completed"
        ]
        if pending:
            return [Send("specialist", {"active_invocation": item}) for item in pending]
        return [Send("synthesize", {})]

    def synthesize(state: _CompositionState) -> dict[str, object]:
        outcomes = state.get("outcomes")
        outcomes = outcomes if isinstance(outcomes, dict) else {}
        completed = [
            {"invocation_id": key, "summary": _compact(str(value.get("summary", "")))}
            for key, value in sorted(outcomes.items())
            if isinstance(value, dict) and value.get("status") == "completed"
        ]
        incomplete = [
            {
                "invocation_id": key,
                "status": str(value.get("status", "failed")),
                "recovery_hint": _compact(str(value.get("recovery_hint", ""))),
            }
            for key, value in sorted(outcomes.items())
            if isinstance(value, dict) and value.get("status") != "completed"
        ]
        return {
            "completed_nodes": ["synthesize"],
            "delivery": {
                "status": "completed" if not incomplete else "partial",
                "result_scope": "仅汇总已完成的受控专业调用；未完成分支不进入本次结论。",
                "completed_children": completed,
                "unavailable_children": incomplete,
                "blocked_count": sum(1 for item in incomplete if item["status"] == "blocked"),
            },
        }

    graph = StateGraph(_CompositionState)
    graph.add_node("prepare", prepare)
    graph.add_node("specialist", backend._run_specialist)
    graph.add_node("synthesize", synthesize)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", dispatch, ["specialist", "synthesize"])
    graph.add_edge("specialist", "synthesize")
    graph.add_edge("synthesize", END)
    return graph


def _invocation_from_payload(payload: dict[str, str]) -> CommanderCompositionInvocation:
    return CommanderCompositionInvocation(
        invocation_id=str(payload.get("invocation_id", "")),
        step_id=str(payload.get("step_id", "")),
        agent_id=str(payload.get("agent_id", "")),
        action=str(payload.get("action", "")),
        material_digest=str(payload.get("material_digest", "")),
        input_digest=str(payload.get("input_digest", "")),
    )


def _outcome_payload(outcome: CommanderCompositionOutcome) -> dict[str, object]:
    return {
        "status": outcome.status,
        "summary": _compact(outcome.summary),
        "delegated_task_id": _compact(outcome.delegated_task_id, limit=100),
        "source_count": outcome.source_count,
        "chart_count": outcome.chart_count,
        "table_count": outcome.table_count,
        "recovery_hint": _compact(outcome.recovery_hint, limit=240),
    }


def _step_input_digest(step: WorkflowStep) -> str:
    """只对稳定计划输入做摘要，不能把原始 goal/query 写入 checkpoint。"""

    permitted = {
        key: value
        for key, value in step.input.items()
        if key
        in {
            "document_refs",
            "dataset_refs",
            "dataset_name",
            "knowledge_base_id",
            "output_mode",
            "cleaning_policy",
            "max_chart_count",
        }
    }
    return _stable_digest(permitted)


def _stable_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _compact(value: str, *, limit: int = _MAX_SUMMARY_LENGTH) -> str:
    return " ".join(value.split())[:limit]


def _thread_id(task_id: str) -> str:
    return f"lgm5:{task_id}"


def _graph_config(task_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": _thread_id(task_id)}}
