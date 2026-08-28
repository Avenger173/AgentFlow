from __future__ import annotations

from app.database.memory_repository import (
    mark_long_term_memories_used,
    search_long_term_memories,
)
from app.schemas.chat import WorkflowPlanPreferences
from app.schemas.memory import LongTermMemoryRecord


def retrieve_commander_memory_context(
    *,
    user_goal: str,
    preferences: WorkflowPlanPreferences,
    project_scope: str | None = None,
) -> list[LongTermMemoryRecord]:
    """按用户开关读取总指挥的最小长期记忆上下文。

    C2 只允许 global 与一个已明确传入的项目范围。没有开启开关时甚至不访问记忆表，保证
    “关闭”不仅是不注入 prompt，也是不读取客户长期记录。当前 Qt 尚未选择项目范围，
    因而稳定使用 global；未来项目页接入前不得猜测项目身份或扫描 workspace。
    """

    if not preferences.memory_enabled:
        return []
    scopes = {"global"}
    if project_scope:
        scopes.add(project_scope)
    records = search_long_term_memories(query=user_goal, scopes=scopes, limit=3)
    # 只在实际创建计划的路径标记“已使用”，避免设置页浏览、健康检查等操作污染使用时间。
    mark_long_term_memories_used([item.memory_id for item in records])
    return records
