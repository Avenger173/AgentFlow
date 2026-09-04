"""H1 阶段的无副作用 Harness 后端，用于固定 Bridge 契约。"""

from __future__ import annotations

from app.harness.contracts import (
    HarnessControlResult,
    HarnessEventSink,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    HarnessRuntimeEvent,
)


class FakeNodeHarnessBackend:
    """不启动 Node、不读取文件、不调用模型的契约验证实现。

    它只模拟未来 Bridge 必须提供的生命周期顺序。这样可以先让 Python 的任务事件映射
    具有稳定回归覆盖，而不是拿真实 Provider 额度测试尚未完成的安全边界。
    """

    backend_id = "node_harness_fake"

    async def execute_task(
        self,
        request: HarnessExecutionRequest,
        event_sink: HarnessEventSink,
    ) -> HarnessExecutionResult:
        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_started",
                message="Node Harness Bridge 已受理任务（fake 验证）。",
            )
        )
        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_heartbeat",
                message="Node Harness Bridge 正在等待受控 Runtime 返回（fake 验证）。",
            )
        )
        final_text = f"Fake Harness 已完成任务 {request.task_id} 的协议验证。"
        await event_sink(
            HarnessRuntimeEvent(
                kind="assistant_final",
                message="Node Harness Bridge 已收到最终结果（fake 验证）。",
            )
        )
        return HarnessExecutionResult(
            status="completed",
            final_text=final_text,
            session_id=f"fake-{request.task_id}",
            metadata={"backend": self.backend_id, "provider": request.provider_id},
        )

    async def resume_task(
        self,
        task_id: str,
        resume_input: dict[str, object],
        event_sink: HarnessEventSink,
    ) -> HarnessExecutionResult:
        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_failed",
                message="Fake Node Harness 未保存检查点，不能恢复任务。",
            )
        )
        return HarnessExecutionResult(
            status="failed",
            failure_code="resume_not_supported",
            metadata={"backend": self.backend_id, "task_id": task_id},
        )

    async def cancel_task(self, task_id: str) -> HarnessControlResult:
        return HarnessControlResult(
            status="unsupported",
            message="Fake Node Harness 不维护后台任务，不能取消。",
        )

    async def close(self) -> None:
        return None
