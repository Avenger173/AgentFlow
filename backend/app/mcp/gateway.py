"""LGM1 的测试专用 MCPGateway 外观。"""

from __future__ import annotations

from typing import Any

from app.mcp.client_manager import McpClientManager, create_deterministic_test_server_config
from app.mcp.contracts import McpGatewayAuditEvent, McpToolDescriptor, McpToolResult


class McpGateway:
    """封装确定性 MCP Server 的发现、调用和无正文审计。

    此类没有 FastAPI route、配置持久化或 Agent action 注册。只有专项回归显式构造它，避免
    LGM1 的基础设施被误当成可以由 Commander 或模型自动使用的客户能力。
    """

    def __init__(self, manager: McpClientManager) -> None:
        self._manager = manager

    @classmethod
    def for_deterministic_test(cls) -> "McpGateway":
        return cls(McpClientManager(create_deterministic_test_server_config()))

    async def discover_tools(self) -> tuple[McpToolDescriptor, ...]:
        return await self._manager.discover_tools()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> McpToolResult:
        return await self._manager.call_tool(tool_name, arguments)

    def audit_snapshot(self) -> tuple[McpGatewayAuditEvent, ...]:
        return self._manager.audit_snapshot()

    async def close(self) -> None:
        await self._manager.close()
