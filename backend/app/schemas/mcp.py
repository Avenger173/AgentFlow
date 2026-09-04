"""面向桌面端的 LGM2 MCP 连接摘要协议。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


McpConnectionStatus = Literal["disabled", "ready", "degraded", "platform_disabled"]


class McpConnectionToolInfo(BaseModel):
    qualified_name: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1_200)
    required_permissions: list[str] = Field(default_factory=list, max_length=4)
    commander_selectable: bool = False


class McpConnectionInfo(BaseModel):
    connection_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    transport: Literal["stdio", "streamable_http"]
    status: McpConnectionStatus
    enabled: bool
    requires_network: bool
    requires_command_confirmation: bool
    origin_summary: str = Field(min_length=1, max_length=260)
    last_checked_at: str = ""
    last_tool_count: int = Field(default=0, ge=0, le=32)
    last_error_code: str = Field(default="", max_length=80)
    tools: list[McpConnectionToolInfo] = Field(default_factory=list, max_length=8)


class McpConnectionListResponse(BaseModel):
    total: int = Field(ge=0)
    connections: list[McpConnectionInfo] = Field(default_factory=list, max_length=16)


class McpConnectionMutationResponse(BaseModel):
    connection: McpConnectionInfo
    message: str = Field(min_length=1, max_length=300)
