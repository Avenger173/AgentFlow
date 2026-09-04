"""LGM1 的短生命周期 MCP stdio ClientManager。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Any, TypeVar

from app.mcp.contracts import (
    McpGatewayAuditEvent,
    McpGatewayError,
    McpServerReference,
    McpStdioTestServerConfig,
    McpToolDescriptor,
    McpToolResult,
)
from app.mcp.result_guard import (
    guard_tool_result,
    normalize_tool_descriptors,
    tool_result_size_bytes,
    validate_tool_arguments,
)


_ResultT = TypeVar("_ResultT")
_TEST_SERVER_ID = "agentflow-test"


class McpClientManager:
    """为项目内测试 Server 管理“启动 -> 调用 -> 关闭”的短会话。

    LGM1 不缓存 session、更不接受客户配置；每次发现或调用都会在 ``async with Client``
    内完成，因此取消、超时和正常返回均会走 SDK 的进程清理路径。
    """

    def __init__(self, config: McpStdioTestServerConfig) -> None:
        self._config = config
        self._tool_catalog: dict[str, McpToolDescriptor] = {}
        self._audit_events: list[McpGatewayAuditEvent] = []
        self._closed = False

    @property
    def server(self) -> McpServerReference:
        return self._config.server

    def audit_snapshot(self) -> tuple[McpGatewayAuditEvent, ...]:
        return tuple(self._audit_events)

    async def discover_tools(self) -> tuple[McpToolDescriptor, ...]:
        """启动测试 Server、规范化目录并立即关闭连接。"""

        started_at = perf_counter()
        self._record("connection_started", status="discover")
        try:
            raw_result = await self._invoke(
                lambda client: client.list_tools(),
                timeout_seconds=self._config.connect_timeout_seconds,
                timeout_code="mcp_connection_failed",
            )
            descriptors = normalize_tool_descriptors(self._config.server, raw_result.tools)
        except asyncio.CancelledError:
            self._record("tool_call_cancelled", status="discover", duration_ms=_elapsed_ms(started_at))
            raise
        except McpGatewayError as error:
            self._record(
                "tool_call_failed",
                status="discover",
                duration_ms=_elapsed_ms(started_at),
                error_code=error.code,
            )
            raise
        else:
            self._tool_catalog = {item.reference.tool_name: item for item in descriptors}
            self._record("tools_discovered", status="completed", duration_ms=_elapsed_ms(started_at))
            return descriptors
        finally:
            self._record("connection_closed", status="discover", duration_ms=_elapsed_ms(started_at))

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> McpToolResult:
        """调用已发现的测试 Tool，并返回经过结果 Guard 的最小对象。"""

        self._ensure_open()
        if not self._tool_catalog:
            await self.discover_tools()
        descriptor = self._tool_catalog.get(tool_name)
        if descriptor is None:
            raise McpGatewayError("mcp_tool_not_found", "请求的 MCP Tool 不在已发现目录中。")
        safe_arguments = validate_tool_arguments(arguments)
        started_at = perf_counter()
        request_bytes = _json_size(safe_arguments)
        self._record("connection_started", tool_name=tool_name, status="call")
        self._record(
            "tool_call_started",
            tool_name=tool_name,
            status="running",
            request_bytes=request_bytes,
        )
        try:
            raw_result = await self._invoke(
                lambda client: client.call_tool(tool_name, safe_arguments),
                timeout_seconds=self._config.tool_timeout_seconds,
                timeout_code="mcp_tool_timeout",
            )
            result = guard_tool_result(descriptor.reference, raw_result)
        except asyncio.CancelledError:
            self._record(
                "tool_call_cancelled",
                tool_name=tool_name,
                status="cancelled",
                duration_ms=_elapsed_ms(started_at),
                request_bytes=request_bytes,
                error_code="mcp_tool_cancelled",
            )
            raise
        except McpGatewayError as error:
            self._record(
                "tool_call_failed",
                tool_name=tool_name,
                status="failed",
                duration_ms=_elapsed_ms(started_at),
                request_bytes=request_bytes,
                error_code=error.code,
            )
            raise
        else:
            self._record(
                "tool_call_completed",
                tool_name=tool_name,
                status="completed_with_error" if result.is_error else "completed",
                duration_ms=_elapsed_ms(started_at),
                request_bytes=request_bytes,
                result_bytes=tool_result_size_bytes(result),
            )
            return result
        finally:
            self._record(
                "connection_closed",
                tool_name=tool_name,
                status="call",
                duration_ms=_elapsed_ms(started_at),
            )

    async def close(self) -> None:
        """阻止后续使用；短会话在每次操作结束时已由 SDK 关闭。"""

        self._closed = True
        self._tool_catalog.clear()

    async def _invoke(
        self,
        operation: Callable[[Any], Awaitable[_ResultT]],
        *,
        timeout_seconds: float,
        timeout_code: str,
    ) -> _ResultT:
        self._ensure_open()
        try:
            from mcp import Client, StdioServerParameters
        except ImportError as error:
            raise McpGatewayError("mcp_sdk_unavailable", "MCP Python SDK 未准备。") from error

        parameters = StdioServerParameters(
            command=str(self._config.command),
            args=list(self._config.args),
            env=dict(self._config.environment),
            cwd=str(self._config.cwd),
            encoding="utf-8",
            encoding_error_handler="replace",
        )

        async def run_operation() -> _ResultT:
            async with Client(parameters, read_timeout_seconds=timeout_seconds) as client:
                return await operation(client)

        try:
            return await asyncio.wait_for(run_operation(), timeout=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as error:
            message = "MCP 测试服务连接超时。" if timeout_code == "mcp_connection_failed" else "MCP Tool 调用超时。"
            raise McpGatewayError(timeout_code, message) from error
        except McpGatewayError:
            raise
        except Exception as error:
            raise McpGatewayError("mcp_server_exited", "MCP 测试服务意外退出或协议中断。") from error

    def _ensure_open(self) -> None:
        if self._closed:
            raise McpGatewayError("mcp_manager_closed", "MCP ClientManager 已关闭。")

    def _record(
        self,
        event_type: str,
        *,
        tool_name: str = "",
        status: str = "",
        duration_ms: int = 0,
        error_code: str = "",
        request_bytes: int = 0,
        result_bytes: int = 0,
    ) -> None:
        self._audit_events.append(
            McpGatewayAuditEvent(
                event_type=event_type,  # type: ignore[arg-type]
                server_id=self._config.server.server_id,
                tool_name=tool_name,
                status=status,
                duration_ms=duration_ms,
                error_code=error_code,
                request_bytes=request_bytes,
                result_bytes=result_bytes,
            )
        )
        if len(self._audit_events) > 128:
            self._audit_events.pop(0)


def create_deterministic_test_server_config() -> McpStdioTestServerConfig:
    """构造唯一可被 LGM1 Gateway 使用的本地测试连接。"""

    backend_root = Path(__file__).resolve().parents[2]
    return McpStdioTestServerConfig(
        server=McpServerReference(
            server_id=_TEST_SERVER_ID,
            display_name="AgentFlow MCP 确定性测试服务",
            transport="stdio",
        ),
        command=Path(sys.executable),
        args=("-X", "utf8", "-m", "app.mcp.deterministic_test_server"),
        cwd=backend_root,
        environment={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1_000))


def _json_size(value: dict[str, Any]) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
