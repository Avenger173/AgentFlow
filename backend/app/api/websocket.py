import asyncio

from app.schemas.events import TaskLogEvent
from app.services.task_event_stream import has_live_task_event_stream, wait_live_task_events
from app.workflow.dry_run import get_task_log_events
from fastapi import APIRouter, WebSocket, WebSocketDisconnect


router = APIRouter(tags=["websocket"])


@router.websocket("/ws/tasks/{task_id}")
async def task_log_stream(websocket: WebSocket, task_id: str) -> None:
    await websocket.accept()

    # 文档助手的异步入口会先创建实时缓冲，再启动模型/Tool loop。这里按实际事件等待，
    # 不能沿用旧的“任务结束后一次性回放”，否则用户仍会在页面上看到长时间无反馈。
    if has_live_task_event_stream(task_id):
        sent_count = 0
        try:
            while True:
                events, finished = await wait_live_task_events(task_id, sent_count)
                for payload in events:
                    await websocket.send_json(payload.model_dump(mode="json"))
                sent_count += len(events)
                if finished:
                    await websocket.close()
                    return
        except WebSocketDisconnect:
            return

    # 如果 /api/chat 已经为这个 task_id 生成 dry-run，优先推送对应的结构化日志。
    # 否则保留固定 fallback 日志，方便开发期单独验证 WebSocket 通道。
    stored_events = get_task_log_events(task_id)
    events = stored_events if stored_events is not None else _fallback_events(task_id)

    try:
        for payload in events:
            await websocket.send_json(payload.model_dump(mode="json"))
            await asyncio.sleep(0.2)
        await websocket.close()
    except WebSocketDisconnect:
        # 用户切换任务或关闭窗口时客户端可能主动断开；这里静默退出即可。
        return


def _fallback_events(task_id: str) -> list[TaskLogEvent]:
    return [
        TaskLogEvent(
            task_id=task_id,
            sequence=1,
            event="connected",
            agent_id="system",
            message="已连接 AgentFlow 任务日志通道。",
        ),
        TaskLogEvent(
            task_id=task_id,
            sequence=2,
            event="task_started",
            agent_id="commander_agent",
            step_id="step_1",
            message="Commander 正在接收任务并准备规划。",
        ),
        TaskLogEvent(
            task_id=task_id,
            sequence=3,
            event="step_started",
            agent_id="document_agent",
            step_id="step_2",
            message="Document Agent 正在模拟读取输入资料。",
        ),
        TaskLogEvent(
            task_id=task_id,
            sequence=4,
            event="step_completed",
            agent_id="code_agent",
            step_id="step_3",
            message="Code Agent 已生成模拟代码结果。",
        ),
        TaskLogEvent(
            task_id=task_id,
            sequence=5,
            event="task_completed",
            agent_id="report_agent",
            step_id="step_4",
            message="Report Agent 已整理模拟报告，任务完成。",
        ),
    ]
