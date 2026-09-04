"""LGM1 MCPGateway 的本地确定性 stdio 回归。

不读取客户文件、不调用 LLM、不连接网络。唯一子进程是项目内
``app.mcp.deterministic_test_server``，它只有固定数学/回显行为且不继承项目 `.env`。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


async def main() -> None:
    from app.mcp.client_manager import create_deterministic_test_server_config
    from app.mcp.contracts import McpGatewayError, McpServerReference, McpToolReference
    from app.mcp.gateway import McpGateway
    from app.mcp.result_guard import guard_tool_result, normalize_tool_descriptors

    config = create_deterministic_test_server_config()
    assert config.server.server_id == "agentflow-test"
    assert config.command.is_absolute() and config.command.is_file()
    assert config.cwd == BACKEND_ROOT.resolve()
    assert set(config.environment) == {"PYTHONUTF8", "PYTHONIOENCODING"}

    gateway = McpGateway.for_deterministic_test()
    try:
        descriptors = await gateway.discover_tools()
        names = {item.reference.tool_name for item in descriptors}
        assert {
            "echo_text",
            "sum_values",
            "delayed_echo",
            "large_payload",
            "terminate_process",
        } <= names
        assert all(item.input_schema.get("type") == "object" for item in descriptors)

        echo_result = await gateway.call_tool(
            "echo_text", {"text": "hello", "category": "verification"}
        )
        assert echo_result.is_error is False
        assert echo_result.structured_content["text"] == "hello"
        assert echo_result.structured_content["category"] == "verification"

        sum_result = await gateway.call_tool("sum_values", {"values": [3, 5, 8]})
        assert sum_result.is_error is False
        assert sum_result.structured_content["sum"] == 16
        assert sum_result.structured_content["count"] == 3

        from app.mcp.tool_adapter import project_tool_call_audit

        projected_call = project_tool_call_audit(
            task_id="task_lgm1_fixture",
            step_id="fixture_step",
            agent_id="mcp_gateway_fixture",
            call_id="fixture-call",
            result=sum_result,
            duration_ms=9,
            request_bytes=22,
        )
        assert projected_call.tool_name == "mcp.agentflow-test.sum_values"
        assert projected_call.status == "completed"
        assert "values" not in projected_call.request
        assert "sum" not in projected_call.result

        try:
            await gateway.call_tool("not_in_catalog", {})
        except McpGatewayError as error:
            assert error.code == "mcp_tool_not_found"
        else:
            raise AssertionError("不存在的 Tool 必须在本地目录阶段被拒绝。")

        try:
            await gateway.call_tool("large_payload", {"size": 20_000})
        except McpGatewayError as error:
            assert error.code == "mcp_result_too_large"
        else:
            raise AssertionError("超大 MCP 结果不能进入 Gateway。")

        try:
            await gateway.call_tool("terminate_process", {})
        except McpGatewayError as error:
            assert error.code == "mcp_server_exited"
        else:
            raise AssertionError("异常退出的 MCP Server 不能伪装为成功。")
        restarted_tools = await gateway.discover_tools()
        assert {item.reference.tool_name for item in restarted_tools} == names

        pending_call = asyncio.create_task(
            gateway.call_tool("delayed_echo", {"text": "cancel", "delay_ms": 1_000})
        )
        await asyncio.sleep(0.05)
        pending_call.cancel()
        try:
            await pending_call
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("取消的 MCP 调用不应伪装为成功。")

        audit_events = gateway.audit_snapshot()
        assert any(item.event_type == "tools_discovered" for item in audit_events)
        assert any(
            item.event_type == "tool_call_completed" and item.tool_name == "sum_values"
            for item in audit_events
        )
        assert any(
            item.event_type == "tool_call_failed"
            and item.tool_name == "large_payload"
            and item.error_code == "mcp_result_too_large"
            for item in audit_events
        )
        assert any(
            item.event_type == "tool_call_failed"
            and item.tool_name == "terminate_process"
            and item.error_code == "mcp_server_exited"
            for item in audit_events
        )
        assert any(
            item.event_type == "tool_call_cancelled" and item.tool_name == "delayed_echo"
            for item in audit_events
        )
        assert all("hello" not in item.model_dump_json() for item in audit_events)

        reference = McpToolReference(
            server=McpServerReference(
                server_id="agentflow-test",
                display_name="AgentFlow MCP 确定性测试服务",
                transport="stdio",
            ),
            tool_name="echo_text",
        )
        guarded = guard_tool_result(
            reference,
            SimpleNamespace(
                content=(),
                structured_content={"api_key": "sk-should-never-appear", "safe": "value"},
                is_error=False,
            ),
        )
        assert guarded.structured_content["api_key"] == "[REDACTED]"
        assert guarded.structured_content["safe"] == "value"

        try:
            normalize_tool_descriptors(
                reference.server,
                [SimpleNamespace(name="bad_schema", title="bad", description="bad", input_schema={"type": "array"})],
            )
        except McpGatewayError as error:
            assert error.code == "mcp_tool_schema_invalid"
        else:
            raise AssertionError("非 object 的 Tool schema 必须被拒绝。")
    finally:
        await gateway.close()

    try:
        await gateway.discover_tools()
    except McpGatewayError as error:
        assert error.code == "mcp_manager_closed"
    else:
        raise AssertionError("关闭后的 Gateway 不能再次创建 MCP 子进程。")

    print(
        "LGM1 MCP Gateway verification passed: deterministic stdio discovery, "
        "calls, guardrails, cancellation, audit, and cleanup are stable."
    )


if __name__ == "__main__":
    asyncio.run(main())
