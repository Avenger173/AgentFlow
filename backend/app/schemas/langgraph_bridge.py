"""LGM5 正式业务 bridge 的最小持久化协议。

该协议只关联 AgentFlow Runtime 任务与 LangGraph 的受控 checkpoint。客户目标、材料名称、
正文、文件路径、模型上下文和凭据仍只保留在既有任务/专业 Agent 边界，不能写入 bridge。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


LangGraphBridgeStatus = Literal[
    "prepared",
    "running",
    "partial",
    "completed",
    "blocked",
    "failed",
    "cancelled",
]
LangGraphBridgeDeliveryState = Literal["pending", "partial", "completed", "blocked", "failed"]

_HEX64_PATTERN = r"^[a-f0-9]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:-]+$"
_INVOCATION_PATTERN = r"^[a-f0-9]{12,64}$"


class LangGraphCompositionBridgeRecord(BaseModel):
    """一条可恢复 LGM5 组合调用的脱敏关联记录。"""

    model_config = ConfigDict(extra="forbid")

    runtime_task_id: str = Field(min_length=1, max_length=160, pattern=_IDENTIFIER_PATTERN)
    bridge_invocation_key: str = Field(pattern=_HEX64_PATTERN)
    backend_id: Literal["langgraph"] = "langgraph"
    graph_id: str = Field(min_length=1, max_length=96, pattern=_IDENTIFIER_PATTERN)
    graph_version: str = Field(min_length=1, max_length=48, pattern=_IDENTIFIER_PATTERN)
    thread_id: str = Field(min_length=1, max_length=192, pattern=_IDENTIFIER_PATTERN)
    plan_digest: str = Field(pattern=_HEX64_PATTERN)
    status: LangGraphBridgeStatus = "prepared"
    delivery_state: LangGraphBridgeDeliveryState = "pending"
    completed_invocation_ids: tuple[str, ...] = ()
    failed_invocation_ids: tuple[str, ...] = ()
    created_at: str = Field(min_length=1, max_length=40)
    updated_at: str = Field(min_length=1, max_length=40)

    @field_validator("completed_invocation_ids", "failed_invocation_ids")
    @classmethod
    def _validate_invocation_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 16:
            raise ValueError("LGM5 组合 bridge 最多记录 16 个专业调用。")
        if len(set(values)) != len(values):
            raise ValueError("LGM5 组合 bridge 不允许重复 invocation 标识。")
        for value in values:
            if not value or len(value) > 64:
                raise ValueError("LGM5 组合 bridge invocation 标识无效。")
            if re.fullmatch(_INVOCATION_PATTERN, value) is None:
                raise ValueError("LGM5 组合 bridge invocation 标识格式无效。")
        return tuple(sorted(values))

    @field_validator("failed_invocation_ids")
    @classmethod
    def _validate_disjoint_outcomes(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        completed = set(info.data.get("completed_invocation_ids", ()))
        if completed.intersection(values):
            raise ValueError("一项专业调用不能同时处于完成和失败集合。")
        return values
