"""MCPGateway 的稳定、无副作用协议。

这里的模型只描述未来 Gateway 可接受的连接和 Tool 标识。它们不保存 command、URL、
API Key 或原始 Tool 描述；这些高权限配置留到 LGM1 的专属配置与权限层。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


McpTransport = Literal["stdio", "streamable_http"]
McpGatewayErrorCode = Literal[
    "mcp_disabled",
    "mcp_sdk_unavailable",
    "mcp_server_not_found",
    "mcp_transport_not_supported",
    "mcp_connection_failed",
    "mcp_protocol_error",
    "mcp_tool_not_found",
    "mcp_tool_schema_invalid",
    "mcp_tool_timeout",
    "mcp_tool_cancelled",
    "mcp_tool_result_rejected",
    "mcp_permission_denied",
    "mcp_network_not_approved",
]


class McpServerReference(BaseModel):
    """给计划、审计和 Tool Registry 使用的脱敏服务器标识。"""

    server_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    display_name: str = Field(min_length=1, max_length=80)
    transport: McpTransport

    @field_validator("display_name")
    @classmethod
    def _reject_control_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("MCP 连接名称不能为空且不能包含控制字符。")
        return normalized


class McpToolReference(BaseModel):
    """未来 MCP Tool 进入 AgentFlow 前的规范化名称。"""

    server: McpServerReference
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")

    @property
    def qualified_name(self) -> str:
        return f"mcp.{self.server.server_id}.{self.tool_name}"


class McpGatewayError(RuntimeError):
    """Gateway 内部错误，只携带稳定错误码与可脱敏短消息。"""

    def __init__(self, code: McpGatewayErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
