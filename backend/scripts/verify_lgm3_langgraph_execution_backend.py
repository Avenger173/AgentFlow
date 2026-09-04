"""LGM3：验证隔离 LangGraph 图的路由、事件、SQLite checkpoint 和恢复语义。

不启动 FastAPI、不读取客户材料、不调用模型或 MCP。全部状态位于临时目录；夹具失败由
后端注入，确保“重启后恢复”可以验证已完成节点不会因为模型或网络抖动而重复执行。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


async def main() -> None:
    from app.harness.event_projection import project_runtime_event
    from app.harness.langgraph_execution_backend import LangGraphExecutionBackend
    from app.harness.runtime_router import (
        RuntimeRouter,
        RuntimeRoutingRequest,
        lgm3_graph_identity,
    )

    graph_id, graph_version = lgm3_graph_identity()
    _verify_router(
        RuntimeRouter(),
        graph_id=graph_id,
        graph_version=graph_version,
    )
    _verify_event_projection(project_runtime_event)

    root = Path(tempfile.mkdtemp(prefix="agentflow_lgm3_"))
    events: list[str] = []

    async def sink(event) -> None:
        events.append(event.kind)

    try:
        await _verify_success(
            root=root,
            event_sink=sink,
            events=events,
        )
        await _verify_interrupt_resume(
            root=root,
            event_sink=sink,
            events=events,
        )
        await _verify_restart_recovery(
            root=root,
            event_sink=sink,
            events=events,
        )
        await _verify_cancellation(
            root=root,
            event_sink=sink,
            events=events,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert not root.exists(), "关闭后必须释放临时 SQLite checkpoint 文件。"

    print(
        "LGM3 LangGraph ExecutionBackend verification passed: deterministic routing, "
        "parallel branches, interrupt/resume, restart recovery, cancellation, and "
        "SQLite checkpoints remain isolated from customer runtime."
    )


def _verify_router(router, *, graph_id: str, graph_version: str) -> None:
    from app.harness.runtime_router import RuntimeRoutingRequest

    native = router.select(
        RuntimeRoutingRequest(
            task_id="task_customer",
            requested_backend="langgraph",
            graph_id=graph_id,
            graph_version=graph_version,
            internal_test=False,
            feature_enabled=True,
            graph_admitted=True,
        )
    )
    assert native.backend_id == "native" and native.accepted is False
    assert "客户任务" in native.reason

    accepted = router.select(
        RuntimeRoutingRequest(
            task_id="task_fixture",
            requested_backend="langgraph",
            graph_id=graph_id,
            graph_version=graph_version,
            internal_test=True,
            feature_enabled=True,
            graph_admitted=True,
            read_only=True,
        )
    )
    assert accepted.backend_id == "langgraph" and accepted.accepted is True

    refused_side_effect = router.select(
        RuntimeRoutingRequest(
            task_id="task_fixture_write",
            requested_backend="langgraph",
            graph_id=graph_id,
            graph_version=graph_version,
            internal_test=True,
            feature_enabled=True,
            graph_admitted=True,
            read_only=False,
        )
    )
    assert refused_side_effect.backend_id == "native" and refused_side_effect.accepted is False


def _verify_event_projection(project_runtime_event) -> None:
    from app.harness.contracts import HarnessRuntimeEvent

    progress = project_runtime_event(
        task_id="task_lgm3_projection",
        sequence=1,
        event=HarnessRuntimeEvent(
            kind="runtime_heartbeat",
            message="LangGraph 测试节点已完成：fixture_tool。",
        ),
    )
    assert progress.event == "runtime_backend_progress"
    assert progress.level == "info"
    assert "fixture_tool" not in progress.message

    waiting = project_runtime_event(
        task_id="task_lgm3_projection",
        sequence=2,
        event=HarnessRuntimeEvent(kind="permission_required", message="internal"),
    )
    assert waiting.event == "runtime_permission_required"
    assert waiting.level == "warning"


async def _verify_success(*, root: Path, event_sink, events: list[str]) -> None:
    from app.harness.langgraph_execution_backend import LangGraphExecutionBackend

    backend = LangGraphExecutionBackend(checkpoint_path=root / "success.db")
    try:
        result = await backend.execute_task(_request(root, "task_lgm3_success", "success"), event_sink)
        assert result.status == "completed", result
        assert result.metadata["backend"] == "langgraph"
        snapshot = await backend.inspect_task("task_lgm3_success")
        assert snapshot is not None
        assert set(snapshot.branch_results) == {"alpha", "beta"}
        assert snapshot.tool_calls == ("fixture.lookup",)
        assert snapshot.completed_nodes.count("prepare") == 1
        assert snapshot.completed_nodes.count("finalize") == 1
        assert not snapshot.next_nodes
        assert "runtime_started" in events and "assistant_final" in events
    finally:
        await backend.close()


async def _verify_interrupt_resume(*, root: Path, event_sink, events: list[str]) -> None:
    from app.harness.langgraph_execution_backend import LangGraphExecutionBackend

    checkpoint_path = root / "approval.db"
    backend = LangGraphExecutionBackend(checkpoint_path=checkpoint_path)
    try:
        waiting = await backend.execute_task(_request(root, "task_lgm3_approval", "approval"), event_sink)
        assert waiting.status == "waiting_permission", waiting
        before = await backend.inspect_task("task_lgm3_approval")
        assert before is not None
        assert set(before.branch_results) == {"alpha", "beta"}
        assert "approval_gate" in before.next_nodes
        assert "permission_required" in events
    finally:
        await backend.close()

    # 新实例模拟进程重启：不依赖内存中的 pending task，仍在同一 task/thread 上恢复。
    resumed_backend = LangGraphExecutionBackend(checkpoint_path=checkpoint_path)
    try:
        resumed = await resumed_backend.resume_task(
            "task_lgm3_approval",
            {"approved": True},
            event_sink,
        )
        assert resumed.status == "completed", resumed
        after = await resumed_backend.inspect_task("task_lgm3_approval")
        assert after is not None
        for node in ("prepare", "branch_alpha", "branch_beta", "approval_gate", "fixture_tool", "finalize"):
            assert after.completed_nodes.count(node) == 1, (node, after.completed_nodes)
    finally:
        await resumed_backend.close()


async def _verify_restart_recovery(*, root: Path, event_sink, events: list[str]) -> None:
    from app.harness.langgraph_execution_backend import LangGraphExecutionBackend

    checkpoint_path = root / "failure.db"
    failing_backend = LangGraphExecutionBackend(
        checkpoint_path=checkpoint_path,
        inject_tool_failure=True,
    )
    try:
        failed = await failing_backend.execute_task(
            _request(root, "task_lgm3_recovery", "failure_resume"),
            event_sink,
        )
        assert failed.status == "failed" and failed.failure_code == "fixture_tool_failed", failed
        before = await failing_backend.inspect_task("task_lgm3_recovery")
        assert before is not None
        assert before.completed_nodes.count("prepare") == 1
        assert before.completed_nodes.count("branch_alpha") == 1
        assert before.completed_nodes.count("branch_beta") == 1
        assert "fixture_tool" in before.next_nodes
    finally:
        await failing_backend.close()

    recovered_backend = LangGraphExecutionBackend(checkpoint_path=checkpoint_path)
    try:
        recovered = await recovered_backend.resume_task("task_lgm3_recovery", {}, event_sink)
        assert recovered.status == "completed", recovered
        after = await recovered_backend.inspect_task("task_lgm3_recovery")
        assert after is not None
        for node in ("prepare", "branch_alpha", "branch_beta", "fixture_tool", "finalize"):
            assert after.completed_nodes.count(node) == 1, (node, after.completed_nodes)
    finally:
        await recovered_backend.close()


async def _verify_cancellation(*, root: Path, event_sink, events: list[str]) -> None:
    from app.harness.langgraph_execution_backend import LangGraphExecutionBackend

    backend = LangGraphExecutionBackend(checkpoint_path=root / "cancel.db")
    try:
        execution = asyncio.create_task(
            backend.execute_task(_request(root, "task_lgm3_cancel", "cancellable"), event_sink)
        )
        control = None
        for _ in range(30):
            await asyncio.sleep(0.01)
            control = await backend.cancel_task("task_lgm3_cancel")
            if control.status == "accepted":
                break
        assert control is not None and control.status == "accepted", control
        cancelled = await execution
        assert cancelled.status == "cancelled", cancelled
        assert "runtime_cancelled" in events
    finally:
        await backend.close()


def _request(root: Path, task_id: str, scenario: str):
    from app.harness.contracts import HarnessExecutionRequest

    return HarnessExecutionRequest(
        task_id=task_id,
        task_text=f"LGM3 fixture: {scenario}",
        workspace_dir=root.resolve(),
        provider_id="fixture",
        model_id="fixture",
    )


if __name__ == "__main__":
    asyncio.run(main())
