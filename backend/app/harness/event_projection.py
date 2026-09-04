"""把可插拔 Runtime 的规范化事件投影到 AgentFlow 已有任务事件模型。

LGM3 只在测试图中使用该投影，因而不会把任何 LangGraph 内部状态写入客户任务历史。
后续真实试点可把返回的 TaskLogEvent 交给现有 append/WebSocket 路径，而无需让 Qt 认识
第三方框架事件。
"""

from __future__ import annotations

from app.harness.contracts import HarnessRuntimeEvent
from app.schemas.events import TaskLogEvent, TaskLogLevel


def project_runtime_event(
    *,
    task_id: str,
    sequence: int,
    event: HarnessRuntimeEvent,
    agent_id: str = "runtime_backend",
    step_id: str | None = None,
) -> TaskLogEvent:
    """生成客户事件模型，默认不转发框架节点名或原始 SDK 消息。"""

    event_name, level, message = _projection_for(event)
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event_name,
        agent_id=agent_id,
        step_id=step_id,
        level=level,
        message=message,
    )


def _projection_for(event: HarnessRuntimeEvent) -> tuple[str, TaskLogLevel, str]:
    if event.kind == "runtime_started":
        return "runtime_backend_started", "info", "已开始执行受控 Runtime。"
    if event.kind == "runtime_heartbeat":
        return "runtime_backend_progress", "info", "正在执行已获准的任务步骤。"
    if event.kind == "permission_required":
        return "runtime_permission_required", "warning", "任务正在等待所需确认。"
    if event.kind == "assistant_final":
        return "runtime_backend_completed", "info", "受控 Runtime 已完成并返回结果。"
    if event.kind == "runtime_cancelled":
        return "runtime_backend_cancelled", "warning", "任务已在安全边界取消。"
    return "runtime_backend_failed", "error", "受控 Runtime 未能完成任务，已保留可用检查点。"
