"""LGM1 的测试专用 MCPGateway 外观。"""

from __future__ import annotations

from typing import Any

from app.mcp.client_manager import (
    McpClientManager,
    create_deterministic_test_server_config,
    create_public_reference_server_config,
)
from app.mcp.contracts import McpGatewayAuditEvent, McpToolDescriptor, McpToolResult


class McpGateway:
    """封装确定性 MCP Server 的发现、调用和无正文审计。

    LGM1 只暴露确定性测试服务；LGM2 新增一个产品登记的公开资料连接。两者都必须由
    上层 Action Admission、连接状态和权限策略显式准入，Gateway 本身不提供动态 Server 配置。
    """

    def __init__(self, manager: McpClientManager) -> None:
        self._manager = manager

    @classmethod
    def for_deterministic_test(cls) -> "McpGateway":
        return cls(McpClientManager(create_deterministic_test_server_config()))

    @classmethod
    def for_public_reference(cls) -> "McpGateway":
        return cls(McpClientManager(create_public_reference_server_config()))

    async def discover_tools(self) -> tuple[McpToolDescriptor, ...]:
        return await self._manager.discover_tools()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> McpToolResult:
        return await self._manager.call_tool(tool_name, arguments)

    def audit_snapshot(self) -> tuple[McpGatewayAuditEvent, ...]:
        return self._manager.audit_snapshot()

    async def close(self) -> None:
        await self._manager.close()
