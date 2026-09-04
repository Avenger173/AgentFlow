"""LGM3 的隔离 LangGraph ExecutionBackend。

该模块只执行固定夹具图，用来验证分支、内部 Tool、权限 interrupt、失败恢复、取消及
SQLite Checkpointer。它不注册 API 路由，不读取客户文件，不调用模型或 MCP，且 Router
禁止任何客户任务进入这里；真实业务迁移要等 LGM4 的冻结输入影子对照。
"""

from __future__ import annotations

import asyncio
import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from app.harness.contracts import (
    HarnessControlResult,
    HarnessEventSink,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    HarnessRuntimeEvent,
)
from app.harness.runtime_router import lgm3_graph_identity


FixtureScenario = Literal["success", "approval", "failure_resume", "cancellable"]
_FIXTURE_PREFIX = "LGM3 fixture:"


class _FixtureState(TypedDict, total=False):
    """仅保存 JSON 可序列化测试状态，验证 checkpoint 边界而不搬运客户对象。"""

    task_id: str
    graph_id: str
    graph_version: str
    scenario: FixtureScenario
    completed_nodes: Annotated[list[str], operator.add]
    branch_results: Annotated[list[str], operator.add]
    tool_calls: Annotated[list[str], operator.add]
    approval: str
    final_text: str


@dataclass(frozen=True)
class LangGraphCheckpointSnapshot:
    """给 LGM3 回归读取的最小 checkpoint 视图。"""

    task_id: str
    thread_id: str
    graph_id: str
    graph_version: str
    scenario: FixtureScenario
    completed_nodes: tuple[str, ...]
    branch_results: tuple[str, ...]
    tool_calls: tuple[str, ...]
    next_nodes: tuple[str, ...]


class _DeterministicGraphFailure(RuntimeError):
    """测试夹具中的可控失败，只用于确认恢复不会重跑已完成节点。"""


class _DeterministicGraphCancelled(RuntimeError):
    """测试夹具观察到取消请求后的协作式终止。"""


class LangGraphExecutionBackend:
    """运行 LGM3 确定性图的可关闭后端。

    inject_tool_failure 只服务于隔离回归：第一次实例刻意让内部 Tool 失败，关闭并重建
    后用同一 SQLite checkpoint 恢复，从而验证失败前已经完成的节点不会重复执行。
    """

    backend_id = "langgraph"

    def __init__(self, *, checkpoint_path: Path, inject_tool_failure: bool = False) -> None:
        self._checkpoint_path = checkpoint_path
        self._inject_tool_failure = inject_tool_failure
        self._checkpointer_context: object | None = None
        self._checkpointer: object | None = None
        self._graph: object | None = None
        self._cancelled_task_ids: set[str] = set()
        self._closed = False

    async def execute_task(
        self,
        request: HarnessExecutionRequest,
        event_sink: HarnessEventSink,
    ) -> HarnessExecutionResult:
        """以固定 scenario 启动新测试线程。"""

        scenario = _scenario_from_fixture_text(request.task_text)
        if scenario is None:
            return _failure(
                "fixture_only",
                "LangGraph LGM3 后端只接受项目内确定性测试夹具。",
            )
        if self._closed:
            return _failure("backend_closed", "LangGraph 测试后端已经关闭。")

        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_started",
                message="LangGraph 确定性测试图已开始执行。",
            )
        )
        graph_id, graph_version = lgm3_graph_identity()
        initial_state: _FixtureState = {
            "task_id": request.task_id,
            "graph_id": graph_id,
            "graph_version": graph_version,
            "scenario": scenario,
        }
        return await self._drive(
            task_id=request.task_id,
            graph_input=initial_state,
            event_sink=event_sink,
            resuming=False,
        )

    async def resume_task(
        self,
        task_id: str,
        resume_input: dict[str, object],
        event_sink: HarnessEventSink,
    ) -> HarnessExecutionResult:
        """从同一 task/thread 的 SQLite checkpoint 恢复。"""

        if self._closed:
            return _failure("backend_closed", "LangGraph 测试后端已经关闭。")
        snapshot = await self.inspect_task(task_id)
        if snapshot is None:
            return _failure("checkpoint_not_found", "没有找到可恢复的 LangGraph 测试检查点。")
        graph_id, graph_version = lgm3_graph_identity()
        if snapshot.graph_id != graph_id or snapshot.graph_version != graph_version:
            return _failure("graph_version_mismatch", "检查点不属于当前 LGM3 测试图版本。")

        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_heartbeat",
                message="LangGraph 测试图正在从已保存检查点恢复。",
            )
        )
        if snapshot.scenario == "approval" and "approval_gate" in snapshot.next_nodes:
            from langgraph.types import Command

            graph_input: object = Command(resume=resume_input)
        else:
            graph_input = None
        return await self._drive(
            task_id=task_id,
            graph_input=graph_input,
            event_sink=event_sink,
            resuming=True,
        )

    async def cancel_task(self, task_id: str) -> HarnessControlResult:
        """登记协作式取消；图节点在安全边界检查该标志。"""

        if self._closed:
            return HarnessControlResult(status="closed", message="LangGraph 测试后端已经关闭。")
        snapshot = await self.inspect_task(task_id)
        if snapshot is None:
            return HarnessControlResult(status="not_found", message="未找到可取消的 LangGraph 测试任务。")
        if not snapshot.next_nodes:
            return HarnessControlResult(status="already_terminal", message="测试任务已经到达终态。")
        self._cancelled_task_ids.add(task_id)
        return HarnessControlResult(status="accepted", message="已登记取消请求，当前节点会在安全边界退出。")

    async def inspect_task(self, task_id: str) -> LangGraphCheckpointSnapshot | None:
        """读取受限 checkpoint 元数据，供专项回归验证 task/thread/version 映射。"""

        graph = await self._ensure_graph()
        state = await graph.aget_state(_graph_config(task_id))
        values = state.values if state is not None else {}
        if not values or values.get("task_id") != task_id:
            return None
        return LangGraphCheckpointSnapshot(
            task_id=task_id,
            thread_id=_thread_id(task_id),
            graph_id=str(values.get("graph_id", "")),
            graph_version=str(values.get("graph_version", "")),
            scenario=str(values.get("scenario", "")),
            completed_nodes=tuple(values.get("completed_nodes", [])),
            branch_results=tuple(values.get("branch_results", [])),
            tool_calls=tuple(values.get("tool_calls", [])),
            next_nodes=tuple(state.next),
        )

    async def close(self) -> None:
        """关闭独立 SQLite 连接，不影响 AgentFlow 主数据库。"""

        if self._closed:
            return
        self._closed = True
        context = self._checkpointer_context
        self._graph = None
        self._checkpointer = None
        self._checkpointer_context = None
        if context is not None:
            await context.__aexit__(None, None, None)

    async def _drive(
        self,
        *,
        task_id: str,
        graph_input: object,
        event_sink: HarnessEventSink,
        resuming: bool,
    ) -> HarnessExecutionResult:
        graph = await self._ensure_graph()
        try:
            async for update in graph.astream(
                graph_input,
                _graph_config(task_id),
                stream_mode="updates",
            ):
                if "__interrupt__" in update:
                    await event_sink(
                        HarnessRuntimeEvent(
                            kind="permission_required",
                            message="确定性测试图已在权限节点暂停，等待同一任务恢复。",
                        )
                    )
                    return HarnessExecutionResult(
                        status="waiting_permission",
                        session_id=_thread_id(task_id),
                        metadata=_result_metadata(task_id, resuming=resuming),
                    )
                for node_name in update:
                    await event_sink(
                        HarnessRuntimeEvent(
                            kind="runtime_heartbeat",
                            message=f"LangGraph 测试节点已完成：{node_name}。",
                        )
                    )
        except _DeterministicGraphFailure:
            await event_sink(
                HarnessRuntimeEvent(
                    kind="runtime_failed",
                    message="确定性测试 Tool 按夹具要求失败；可从同一检查点恢复。",
                )
            )
            return _failure(
                "fixture_tool_failed",
                "确定性测试 Tool 已失败，恢复时不应重跑已完成节点。",
                task_id=task_id,
                resuming=resuming,
            )
        except _DeterministicGraphCancelled:
            await event_sink(
                HarnessRuntimeEvent(
                    kind="runtime_cancelled",
                    message="LangGraph 测试图已在节点安全边界响应取消。",
                )
            )
            return HarnessExecutionResult(
                status="cancelled",
                session_id=_thread_id(task_id),
                metadata=_result_metadata(task_id, resuming=resuming),
            )
        except Exception as error:
            await event_sink(
                HarnessRuntimeEvent(
                    kind="runtime_failed",
                    message="LangGraph 测试图发生未分类失败。",
                )
            )
            return _failure(
                "graph_execution_failed",
                f"LangGraph 测试图执行失败：{type(error).__name__}。",
                task_id=task_id,
                resuming=resuming,
            )

        snapshot = await self.inspect_task(task_id)
        if snapshot is None:
            return _failure("checkpoint_missing", "测试图没有写入可读取的检查点。")
        final_text = "LangGraph 确定性测试图已完成分支、Tool 与状态汇总。"
        await event_sink(
            HarnessRuntimeEvent(
                kind="assistant_final",
                message="LangGraph 确定性测试图已完成。",
            )
        )
        return HarnessExecutionResult(
            status="completed",
            final_text=final_text,
            session_id=snapshot.thread_id,
            metadata=_result_metadata(task_id, resuming=resuming),
        )

    async def _ensure_graph(self):
        if self._graph is not None:
            return self._graph
        if self._closed:
            raise RuntimeError("LangGraph 测试后端已关闭。")

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        context = AsyncSqliteSaver.from_conn_string(str(self._checkpoint_path))
        checkpointer = await context.__aenter__()
        try:
            graph = _build_fixture_graph(self)
        except Exception:
            await context.__aexit__(None, None, None)
            raise
        self._checkpointer_context = context
        self._checkpointer = checkpointer
        self._graph = graph.compile(checkpointer=checkpointer, name="AgentFlowLgm3Fixture")
        return self._graph

    async def _fixture_tool(self, state: _FixtureState) -> dict[str, object]:
        """无副作用 Tool 节点；故障和取消只来自明确的测试 scenario。"""

        task_id = state["task_id"]
        if task_id in self._cancelled_task_ids:
            raise _DeterministicGraphCancelled()
        if state["scenario"] == "cancellable":
            await asyncio.sleep(0.12)
            if task_id in self._cancelled_task_ids:
                raise _DeterministicGraphCancelled()
        if state["scenario"] == "failure_resume" and self._inject_tool_failure:
            raise _DeterministicGraphFailure()
        return {
            "completed_nodes": ["fixture_tool"],
            "tool_calls": ["fixture.lookup"],
        }


def _build_fixture_graph(backend: LangGraphExecutionBackend):
    """构造一张固定图：准备 -> 并行分支 -> interrupt -> Tool -> 汇总。"""

    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def prepare(_state: _FixtureState) -> dict[str, object]:
        return {"completed_nodes": ["prepare"]}

    def branch_alpha(_state: _FixtureState) -> dict[str, object]:
        return {
            "completed_nodes": ["branch_alpha"],
            "branch_results": ["alpha"],
        }

    def branch_beta(_state: _FixtureState) -> dict[str, object]:
        return {
            "completed_nodes": ["branch_beta"],
            "branch_results": ["beta"],
        }

    def approval_gate(state: _FixtureState) -> dict[str, object]:
        if state["scenario"] == "approval" and state.get("approval") != "approved":
            response = interrupt(
                {
                    "request_id": f"lgm3-approval-{state['task_id']}",
                    "summary": "LGM3 确定性测试图等待模拟批准。",
                }
            )
            if not isinstance(response, dict) or response.get("approved") is not True:
                raise _DeterministicGraphCancelled()
            return {
                "completed_nodes": ["approval_gate"],
                "approval": "approved",
            }
        return {"completed_nodes": ["approval_gate"]}

    def finalize(_state: _FixtureState) -> dict[str, object]:
        return {
            "completed_nodes": ["finalize"],
            "final_text": "fixture completed",
        }

    graph = StateGraph(_FixtureState)
    graph.add_node("prepare", prepare)
    graph.add_node("branch_alpha", branch_alpha)
    graph.add_node("branch_beta", branch_beta)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("fixture_tool", backend._fixture_tool)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "branch_alpha")
    graph.add_edge("prepare", "branch_beta")
    graph.add_edge(["branch_alpha", "branch_beta"], "approval_gate")
    graph.add_edge("approval_gate", "fixture_tool")
    graph.add_edge("fixture_tool", "finalize")
    graph.add_edge("finalize", END)
    return graph


def _scenario_from_fixture_text(value: str) -> FixtureScenario | None:
    normalized = value.strip().lower()
    if not normalized.startswith(_FIXTURE_PREFIX.lower()):
        return None
    scenario = normalized.removeprefix(_FIXTURE_PREFIX.lower()).strip()
    if scenario in {"success", "approval", "failure_resume", "cancellable"}:
        return scenario
    return None


def _thread_id(task_id: str) -> str:
    return f"lgm3:{task_id}"


def _graph_config(task_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": _thread_id(task_id)}}


def _result_metadata(task_id: str, *, resuming: bool) -> dict[str, str]:
    graph_id, graph_version = lgm3_graph_identity()
    return {
        "backend": "langgraph",
        "graph_id": graph_id,
        "graph_version": graph_version,
        "thread_id": _thread_id(task_id),
        "resumed": str(resuming).lower(),
    }


def _failure(
    code: str,
    message: str,
    *,
    task_id: str | None = None,
    resuming: bool = False,
) -> HarnessExecutionResult:
    metadata = _result_metadata(task_id, resuming=resuming) if task_id else {"backend": "langgraph"}
    return HarnessExecutionResult(
        status="failed",
        failure_code=code,
        metadata=metadata,
    )
