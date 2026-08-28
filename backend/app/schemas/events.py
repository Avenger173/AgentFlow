from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


TaskLogLevel = Literal["info", "warning", "error"]


class TaskLogEvent(BaseModel):
    """任务日志事件。

    level 只保留最小的一组语义，方便前端用颜色区分普通信息、权限提醒和错误。
    """

    task_id: str
    sequence: int
    event: str
    agent_id: str
    step_id: str | None = None
    level: TaskLogLevel = "info"
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskLogListResponse(BaseModel):
    task_id: str
    total: int
    events: list[TaskLogEvent] = Field(default_factory=list)
