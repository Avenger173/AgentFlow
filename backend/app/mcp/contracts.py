"""MCPGateway 的稳定协议与 LGM1 测试连接边界。

LGM1 只允许项目内确定性 ``stdio`` 服务。配置对象不会进入 API、数据库、模型上下文或
客户任务；真实连接的持久化配置、密钥引用和权限 UI 留给 LGM2 以后。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


McpTransport = Literal["stdio", "streamable_http"]
McpGatewayErrorCode = Literal[
    "mcp_disabled",
    "mcp_sdk_unavailable",
    "mcp_server_not_found",
    "mcp_transport_not_supported",
    "mcp_connection_failed",
    "mcp_server_exited",
    "mcp_protocol_error",
    "mcp_tool_not_found",
    "mcp_tool_schema_invalid",
    "mcp_tool_timeout",
    "mcp_tool_cancelled",
    "mcp_tool_arguments_invalid",
    "mcp_tool_result_rejected",
    "mcp_schema_too_large",
    "mcp_result_too_large",
    "mcp_launch_config_invalid",
    "mcp_manager_closed",
    "mcp_permission_denied",
    "mcp_network_not_approved",
]
McpGatewayAuditEventType = Literal[
    "connection_started",
    "connection_closed",
    "tools_discovered",
    "tool_call_started",
    "tool_call_completed",
    "tool_call_failed",
    "tool_call_cancelled",
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


class McpToolDescriptor(BaseModel):
    """经 Gateway 裁剪后的 MCP Tool 目录项。

    ``description`` 和 schema 仍是不可信的服务端输入，当前只用于确定性测试和后续受控
    Tool Registry；不能直接拼进系统提示或赋予权限。
    """

    reference: McpToolReference
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(max_length=1_200)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None


class McpToolResult(BaseModel):
    """裁剪、脱敏后的 Tool 结果，供未来 ToolGateway 继续校验。"""

    reference: McpToolReference
    text: str = Field(max_length=8_000)
    structured_content: dict[str, Any] = Field(default_factory=dict)
    content_block_types: list[str] = Field(default_factory=list, max_length=12)
    is_error: bool = False
    result_truncated: bool = False


class McpGatewayAuditEvent(BaseModel):
    """LGM1 的进程内、无敏感内容审计投影。"""

    event_type: McpGatewayAuditEventType
    server_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    tool_name: str = Field(default="", max_length=64)
    status: str = Field(default="", max_length=24)
    duration_ms: int = Field(default=0, ge=0)
    error_code: str = Field(default="", max_length=64)
    request_bytes: int = Field(default=0, ge=0)
    result_bytes: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class McpStdioTestServerConfig:
    """启动项目内测试 MCP Server 所需的最小配置。

    这不是客户 MCP 配置格式：只允许 ``agentflow-test``，只允许绝对 Python 可执行文件、
    受控 backend cwd 以及非常小的环境白名单。它刻意不能持有 URL、认证或客户路径。
    """

    server: McpServerReference
    command: Path
    args: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    connect_timeout_seconds: float = 5.0
    tool_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.server.server_id != "agentflow-test" or self.server.transport != "stdio":
            raise ValueError("LGM1 只允许启动项目内 agentflow-test stdio 服务。")

        command = self.command.resolve()
        cwd = self.cwd.resolve()
        if not command.is_file() or not cwd.is_dir():
            raise ValueError("MCP 测试服务的 command 与 cwd 必须是存在的受控绝对路径。")
        if not 1 <= len(self.args) <= 12:
            raise ValueError("MCP 测试服务参数数量必须在 1 到 12 之间。")
        if any(not item or len(item) > 256 or any(ord(char) < 32 for char in item) for item in self.args):
            raise ValueError("MCP 测试服务参数包含不允许的内容。")
        if not 0.1 <= self.connect_timeout_seconds <= 30 or not 0.1 <= self.tool_timeout_seconds <= 30:
            raise ValueError("MCP 测试服务超时必须位于 0.1 到 30 秒之间。")

        allowed_environment = {"PYTHONIOENCODING", "PYTHONUTF8"}
        environment = dict(self.environment)
        if set(environment) - allowed_environment:
            raise ValueError("MCP 测试服务环境变量不在白名单内。")
        if any(not value or len(value) > 80 or any(ord(char) < 32 for char in value) for value in environment.values()):
            raise ValueError("MCP 测试服务环境变量值不合法。")

        object.__setattr__(self, "command", command)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "environment", environment)


class McpGatewayError(RuntimeError):
    """Gateway 内部错误，只携带稳定错误码与可脱敏短消息。"""

    def __init__(self, code: McpGatewayErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
