"""LGM4 的知识库 K4 LangGraph 影子执行后端。

这个模块不注册 API、不会把客户任务切换到 LangGraph，也不重新实现 K4 的 Map/Reduce。
它只在隔离夹具中把既有 K4 服务包进一张图，用同一份冻结范围和假模型验证 checkpoint
恢复、最终结果和交付资格与 Native 路径一致。Graph State 只保存稳定任务引用和摘要，正文
继续由 K4 服务在每个 Map 节点按既有受控引用读取。
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal, TypedDict

from app.agents.runner import ToolCallingModel
from app.database.task_repository import load_workflow_run
from app.harness.contracts import HarnessEventSink, HarnessRuntimeEvent
from app.schemas.knowledge import KnowledgeDeepTaskScope
from app.services.knowledge_deep_task import (
    KnowledgeDeepTaskScopeError,
    create_knowledge_deep_task_map_queued_run,
    get_knowledge_deep_task_result,
    get_knowledge_deep_task_scope,
    request_knowledge_deep_task_cancel,
    resume_knowledge_deep_task,
    run_knowledge_deep_task_map,
    run_knowledge_deep_task_reduce,
    verify_knowledge_deep_task_scope,
)


_GRAPH_ID = "knowledge_deep_shadow"
_GRAPH_VERSION = "v1"


class _K4ShadowState(TypedDict, total=False):
    """允许进入 SQLite checkpoint 的最小状态，不携带正文或 Runtime 对象。"""

    task_id: str
    graph_id: str
    graph_version: str
    scope_digest: str
    knowledge_base_id: str
    index_generation_id: str
    completed_nodes: Annotated[list[str], operator.add]
    result_digest: str


@dataclass(frozen=True)
class K4ShadowSnapshot:
    """仅供 LGM4 回归读取的 checkpoint 摘要。"""

    task_id: str
    thread_id: str
    graph_id: str
    graph_version: str
    scope_digest: str
    completed_nodes: tuple[str, ...]
    next_nodes: tuple[str, ...]
    result_digest: str


@dataclass(frozen=True)
class K4ShadowExecutionMetrics:
    """影子运行与 Native K4 checkpoint 的无正文对照事实。"""

    graph_elapsed_ms: int = 0
    graph_checkpoint_node_total: int = 0
    native_step_total: int = 0
    native_step_completed: int = 0
    native_step_failed: int = 0
    native_duration_ms: int = 0
    native_provider_request_total: int = 0
    native_retry_total: int = 0


@dataclass(frozen=True)
class K4ShadowExecutionResult:
    """影子图的受限回执，不替代客户可见的 K4 结果协议。"""

    task_id: str
    status: Literal["completed", "blocked", "cancelled", "failed"]
    stage: Literal["scope", "map", "reduce", "verify", "runtime"]
    scope_digest: str
    completed_nodes: tuple[str, ...] = ()
    result_digest: str = ""
    message: str = ""
    resumed: bool = False
    metrics: K4ShadowExecutionMetrics = K4ShadowExecutionMetrics()


class _K4ShadowBlocked(RuntimeError):
    """K4 已保存可恢复 checkpoint，但当前 Graph 节点不能继续。"""

    def __init__(self, stage: Literal["map", "reduce"], message: str) -> None:
        super().__init__(message)
        self.stage = stage


class _K4ShadowCancelled(RuntimeError):
    """K4 在协作式安全边界确认了取消，不允许 Graph 静默恢复。"""


class LangGraphK4ShadowBackend:
    """只用于 LGM4 隔离对照的业务图。

    进程重启时调用方须用同一冻结 ``scope`` 与一个可用 Model fixture 重建本对象；scope 本身
    仍可从 Native K4 task checkpoint 恢复，LangGraph 的 checkpoint 不复制它。
    """

    backend_id = "langgraph"

    def __init__(
        self,
        *,
        checkpoint_path: Path,
        scope: KnowledgeDeepTaskScope,
        model: ToolCallingModel,
    ) -> None:
        self._checkpoint_path = checkpoint_path
        self._scope = scope
        self._scope_digest = _digest(scope.model_dump(mode="json"))
        self._model = model
        self._checkpointer_context: object | None = None
        self._graph: object | None = None
        self._active_event_sink: HarnessEventSink | None = None
        self._closed = False

    async def execute_task(
        self,
        task_id: str,
        event_sink: HarnessEventSink | None = None,
    ) -> K4ShadowExecutionResult:
        """启动一条仅存在于临时夹具数据库的 K4 影子任务。"""

        if self._closed:
            return self._failure(task_id, "runtime", "影子后端已经关闭。")
        initial_state: _K4ShadowState = {
            "task_id": task_id,
            "graph_id": _GRAPH_ID,
            "graph_version": _GRAPH_VERSION,
            "scope_digest": self._scope_digest,
            "knowledge_base_id": self._scope.knowledge_base_id,
            "index_generation_id": self._scope.index_generation_id,
        }
        return await self._drive(
            task_id=task_id,
            graph_input=initial_state,
            resumed=False,
            event_sink=event_sink,
        )

    async def resume_task(
        self,
        task_id: str,
        event_sink: HarnessEventSink | None = None,
    ) -> K4ShadowExecutionResult:
        """从同一 Graph/K4 checkpoint 恢复；已完成章节仍由 Native K4 跳过。"""

        if self._closed:
            return self._failure(task_id, "runtime", "影子后端已经关闭。", resumed=True)
        snapshot = await self.inspect_task(task_id)
        if snapshot is None:
            return self._failure(task_id, "runtime", "没有找到可恢复的影子检查点。", resumed=True)
        if (
            snapshot.graph_id != _GRAPH_ID
            or snapshot.graph_version != _GRAPH_VERSION
            or snapshot.scope_digest != self._scope_digest
        ):
            return self._failure(task_id, "scope", "影子检查点与当前图版本或冻结范围不匹配。", resumed=True)
        native_scope = get_knowledge_deep_task_scope(task_id)
        if native_scope is None or _digest(native_scope.model_dump(mode="json")) != self._scope_digest:
            return self._failure(task_id, "scope", "Native K4 检查点与影子范围不匹配。", resumed=True)
        control = resume_knowledge_deep_task(task_id)
        if control is None or not control[0].accepted:
            return self._failure(task_id, "runtime", "Native K4 未接受本次恢复请求。", resumed=True)
        return await self._drive(
            task_id=task_id,
            graph_input=None,
            resumed=True,
            event_sink=event_sink,
        )

    async def cancel_task(self, task_id: str) -> bool:
        """把取消权交回既有 K4 控制面；Graph 不另建一套控制状态。"""

        if self._closed or await self.inspect_task(task_id) is None:
            return False
        response = request_knowledge_deep_task_cancel(task_id)
        return response is not None and response.accepted

    async def inspect_task(self, task_id: str) -> K4ShadowSnapshot | None:
        """读取无正文 checkpoint 摘要，用于对照恢复边界。"""

        graph = await self._ensure_graph()
        state = await graph.aget_state(_graph_config(task_id))
        values = state.values if state is not None else {}
        if not values or values.get("task_id") != task_id:
            return None
        return K4ShadowSnapshot(
            task_id=task_id,
            thread_id=_thread_id(task_id),
            graph_id=str(values.get("graph_id", "")),
            graph_version=str(values.get("graph_version", "")),
            scope_digest=str(values.get("scope_digest", "")),
            completed_nodes=tuple(values.get("completed_nodes", [])),
            next_nodes=tuple(state.next),
            result_digest=str(values.get("result_digest", "")),
        )

    async def close(self) -> None:
        """关闭影子 SQLite Checkpointer，不触碰 AgentFlow 主任务连接。"""

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
        graph_input: object,
        resumed: bool,
        event_sink: HarnessEventSink | None,
    ) -> K4ShadowExecutionResult:
        graph = await self._ensure_graph()
        started = perf_counter()
        self._active_event_sink = event_sink
        try:
            await self._emit("runtime_started")
            await graph.ainvoke(graph_input, _graph_config(task_id))
        except _K4ShadowBlocked as exc:
            await self._emit("runtime_failed")
            return await self._from_snapshot(
                task_id,
                "blocked",
                exc.stage,
                str(exc),
                resumed=resumed,
                elapsed_ms=_elapsed_ms(started),
            )
        except _K4ShadowCancelled:
            await self._emit("runtime_cancelled")
            return await self._from_snapshot(
                task_id,
                "cancelled",
                "map",
                "Native K4 已在章节安全边界确认取消。",
                resumed=resumed,
                elapsed_ms=_elapsed_ms(started),
            )
        except KnowledgeDeepTaskScopeError as exc:
            await self._emit("runtime_failed")
            return await self._from_snapshot(
                task_id,
                "failed",
                "scope",
                str(exc),
                resumed=resumed,
                elapsed_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # 影子夹具只输出错误类别，避免泄漏模型或材料正文。
            await self._emit("runtime_failed")
            return await self._from_snapshot(
                task_id,
                "failed",
                "runtime",
                f"LangGraph K4 影子图发生未分类失败：{type(exc).__name__}。",
                resumed=resumed,
                elapsed_ms=_elapsed_ms(started),
            )
        finally:
            self._active_event_sink = None
        await self._emit("assistant_final", event_sink=event_sink)
        return await self._from_snapshot(
            task_id,
            "completed",
            "verify",
            "K4 影子对照已完成。",
            resumed=resumed,
            elapsed_ms=_elapsed_ms(started),
        )

    async def _from_snapshot(
        self,
        task_id: str,
        status: Literal["completed", "blocked", "cancelled", "failed"],
        stage: Literal["scope", "map", "reduce", "verify", "runtime"],
        message: str,
        *,
        resumed: bool,
        elapsed_ms: int,
    ) -> K4ShadowExecutionResult:
        snapshot = await self.inspect_task(task_id)
        native_run = load_workflow_run(task_id)
        native_metrics = native_run.metrics if native_run is not None else None
        return K4ShadowExecutionResult(
            task_id=task_id,
            status=status,
            stage=stage,
            scope_digest=self._scope_digest,
            completed_nodes=snapshot.completed_nodes if snapshot is not None else (),
            result_digest=snapshot.result_digest if snapshot is not None else "",
            message=message,
            resumed=resumed,
            metrics=K4ShadowExecutionMetrics(
                graph_elapsed_ms=elapsed_ms,
                graph_checkpoint_node_total=len(snapshot.completed_nodes) if snapshot is not None else 0,
                native_step_total=native_metrics.step_total if native_metrics is not None else 0,
                native_step_completed=native_metrics.step_completed if native_metrics is not None else 0,
                native_step_failed=native_metrics.step_failed if native_metrics is not None else 0,
                native_duration_ms=native_metrics.duration_ms if native_metrics is not None else 0,
                native_provider_request_total=(
                    native_metrics.provider_model_request_total if native_metrics is not None else 0
                ),
                native_retry_total=native_metrics.retry_total if native_metrics is not None else 0,
            ),
        )

    async def _ensure_graph(self):
        if self._graph is not None:
            return self._graph
        if self._closed:
            raise RuntimeError("影子后端已经关闭。")
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        context = AsyncSqliteSaver.from_conn_string(str(self._checkpoint_path))
        checkpointer = await context.__aenter__()
        try:
            graph = _build_graph(self)
        except Exception:
            await context.__aexit__(None, None, None)
            raise
        self._checkpointer_context = context
        self._graph = graph.compile(checkpointer=checkpointer, name="AgentFlowK4Shadow")
        return self._graph

    def _validate_state(self, state: _K4ShadowState) -> None:
        if (
            state.get("task_id", "").strip() == ""
            or state.get("graph_id") != _GRAPH_ID
            or state.get("graph_version") != _GRAPH_VERSION
            or state.get("scope_digest") != self._scope_digest
            or state.get("knowledge_base_id") != self._scope.knowledge_base_id
            or state.get("index_generation_id") != self._scope.index_generation_id
        ):
            raise KnowledgeDeepTaskScopeError("影子图状态与冻结 K4 范围不匹配。")

    def _freeze_scope(self, state: _K4ShadowState) -> dict[str, object]:
        self._validate_state(state)
        # 先验证活动 generation，防止 Graph 在新版本资料上创建旧 scope 的 Native 任务。
        verify_knowledge_deep_task_scope(self._scope)
        task_id = state["task_id"]
        stored_scope = get_knowledge_deep_task_scope(task_id)
        if stored_scope is None:
            create_knowledge_deep_task_map_queued_run(task_id=task_id, scope=self._scope)
        elif _digest(stored_scope.model_dump(mode="json")) != self._scope_digest:
            raise KnowledgeDeepTaskScopeError("同一影子任务 ID 已绑定不同的 K4 范围。")
        return {"completed_nodes": ["scope_frozen"]}

    async def _run_map(self, state: _K4ShadowState) -> dict[str, object]:
        self._validate_state(state)
        response = await run_knowledge_deep_task_map(
            task_id=state["task_id"],
            scope=self._scope,
            model=self._model,
            progress_callback=self._progress,
        )
        if response.status == "cancelled":
            raise _K4ShadowCancelled()
        if response.status != "completed" or response.completed_map_count != len(self._scope.map_units):
            raise _K4ShadowBlocked("map", response.summary)
        return {"completed_nodes": ["map_completed"]}

    async def _run_reduce(self, state: _K4ShadowState) -> dict[str, object]:
        self._validate_state(state)
        response = await run_knowledge_deep_task_reduce(
            task_id=state["task_id"],
            scope=self._scope,
            model=self._model,
            progress_callback=self._progress,
        )
        if response.status == "cancelled":
            raise _K4ShadowCancelled()
        if response.status != "completed" or response.result is None:
            raise _K4ShadowBlocked("reduce", response.summary)
        return {"completed_nodes": ["reduce_completed"]}

    def _verify_delivery(self, state: _K4ShadowState) -> dict[str, object]:
        self._validate_state(state)
        result = get_knowledge_deep_task_result(state["task_id"])
        if (
            result is None
            or result.status != "completed"
            or result.result is None
            or result.coverage is None
            or result.coverage.state != "complete"
            or result.report_readiness is None
            or not result.report_readiness.can_export
        ):
            raise _K4ShadowBlocked("reduce", "Native K4 结果未满足完整覆盖和正式报告资格。")
        return {
            "completed_nodes": ["delivery_verified"],
            "result_digest": _digest(result.result.model_dump(mode="json")),
        }

    def _failure(
        self,
        task_id: str,
        stage: Literal["scope", "map", "reduce", "verify", "runtime"],
        message: str,
        *,
        resumed: bool = False,
    ) -> K4ShadowExecutionResult:
        return K4ShadowExecutionResult(
            task_id=task_id,
            status="failed",
            stage=stage,
            scope_digest=self._scope_digest,
            message=message,
            resumed=resumed,
        )

    async def _progress(self, event: str, _message: str, _step_id: str | None, _level: str) -> None:
        """将 K4 既有阶段回调归一化为无正文 Harness 进度事件。"""

        if event.endswith("_failed") or event.endswith("_blocked"):
            await self._emit("runtime_failed")
        else:
            await self._emit("runtime_heartbeat")

    async def _emit(
        self,
        kind: Literal[
            "runtime_started",
            "runtime_heartbeat",
            "assistant_final",
            "runtime_failed",
            "runtime_cancelled",
        ],
        *,
        event_sink: HarnessEventSink | None = None,
    ) -> None:
        sink = event_sink if event_sink is not None else self._active_event_sink
        if sink is None:
            return
        await sink(HarnessRuntimeEvent(kind=kind, message="K4 影子执行阶段发生变化。"))


def k4_shadow_graph_identity() -> tuple[str, str]:
    """公开影子图身份，避免测试散落魔法字符串。"""

    return _GRAPH_ID, _GRAPH_VERSION


def _build_graph(backend: LangGraphK4ShadowBackend):
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(_K4ShadowState)
    graph.add_node("scope_frozen", backend._freeze_scope)
    graph.add_node("map_completed", backend._run_map)
    graph.add_node("reduce_completed", backend._run_reduce)
    graph.add_node("delivery_verified", backend._verify_delivery)
    graph.add_edge(START, "scope_frozen")
    graph.add_edge("scope_frozen", "map_completed")
    graph.add_edge("map_completed", "reduce_completed")
    graph.add_edge("reduce_completed", "delivery_verified")
    graph.add_edge("delivery_verified", END)
    return graph


def _thread_id(task_id: str) -> str:
    return f"lgm4:{task_id}"


def _graph_config(task_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": _thread_id(task_id)}}


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))
