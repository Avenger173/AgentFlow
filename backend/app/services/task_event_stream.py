"""同进程任务的实时事件缓冲。

SQLite 负责历史审计，但一次文档分析尚未结束时，数据库中没有最终快照。这个轻量缓冲只把
已经发生的阶段事件送给 WebSocket；完成后仍由正式 WorkflowRun 写入 SQLite，不把它当成
新的持久化系统或跨进程消息队列。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

from app.schemas.events import TaskLogEvent, TaskLogLevel


@dataclass
class _LiveTaskEventBuffer:
    events: list[TaskLogEvent] = field(default_factory=list)
    finished: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


MAX_LIVE_TASK_EVENT_BUFFERS = 200
_LIVE_TASKS: OrderedDict[str, _LiveTaskEventBuffer] = OrderedDict()


def open_live_task_event_stream(task_id: str) -> None:
    """为刚受理的后台任务创建一个可回放的实时事件缓冲。"""

    _LIVE_TASKS[task_id] = _LiveTaskEventBuffer()
    _LIVE_TASKS.move_to_end(task_id)
    # 仅清理已经结束的最旧缓冲。活动任务绝不能为了节省内存被中途移除，正常桌面单用户
    # 场景下 200 条任务的阶段事件量很小。
    while len(_LIVE_TASKS) > MAX_LIVE_TASK_EVENT_BUFFERS:
        oldest_task_id, oldest = next(iter(_LIVE_TASKS.items()))
        if not oldest.finished:
            break
        del _LIVE_TASKS[oldest_task_id]


def has_live_task_event_stream(task_id: str) -> bool:
    """只判断当前进程是否仍持有该任务的实时通道。"""

    return task_id in _LIVE_TASKS


def live_task_event_stream_finished(task_id: str) -> bool:
    """供结果接口区分“仍在运行”和“异常结束但未持久化结果”。"""

    buffer = _LIVE_TASKS.get(task_id)
    return bool(buffer and buffer.finished)


async def publish_live_task_event(
    *,
    task_id: str,
    event: str,
    agent_id: str,
    message: str,
    step_id: str | None = None,
    level: TaskLogLevel = "info",
) -> None:
    """追加一个已经发生的事实事件，并唤醒所有正在等待的 WebSocket。"""

    buffer = _LIVE_TASKS.get(task_id)
    if buffer is None:
        return

    async with buffer.condition:
        buffer.events.append(
            TaskLogEvent(
                task_id=task_id,
                sequence=len(buffer.events) + 1,
                event=event,
                agent_id=agent_id,
                step_id=step_id,
                level=level,
                message=message,
            )
        )
        buffer.condition.notify_all()


async def wait_live_task_events(
    task_id: str,
    after_sequence: int,
) -> tuple[list[TaskLogEvent], bool]:
    """等待新事件或终态，返回未发送的事件和是否已结束。"""

    buffer = _LIVE_TASKS.get(task_id)
    if buffer is None:
        return [], True

    async with buffer.condition:
        while len(buffer.events) <= after_sequence and not buffer.finished:
            await buffer.condition.wait()
        return list(buffer.events[after_sequence:]), buffer.finished


async def finish_live_task_event_stream(task_id: str) -> None:
    """标记流结束；保留缓冲直到服务退出，允许稍晚连接的客户端回放终态。"""

    buffer = _LIVE_TASKS.get(task_id)
    if buffer is None:
        return

    async with buffer.condition:
        buffer.finished = True
        buffer.condition.notify_all()
