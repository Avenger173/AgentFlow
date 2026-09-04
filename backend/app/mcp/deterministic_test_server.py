"""LGM1 的项目内确定性 MCP stdio Server。

此模块不读取文件、环境变量、网络或模型。它只向 Gateway 回归提供固定 Tool 行为，不能作为
客户连接配置或生产服务入口。
"""

from __future__ import annotations

import asyncio
import os

from mcp.server import MCPServer


server = MCPServer(
    name="AgentFlow Deterministic MCP Test",
    version="1.0.0",
    instructions="仅用于 AgentFlow LGM1 本地协议回归，不访问客户数据或外部系统。",
)


@server.tool(
    name="echo_text",
    title="回显文本",
    description="返回完全确定的文本和结构化回显结果。",
)
def echo_text(text: str, category: str = "general") -> dict[str, object]:
    return {
        "text": text,
        "category": category,
        "source": "agentflow-deterministic-test",
    }


@server.tool(
    name="sum_values",
    title="求和",
    description="计算一组整数的和，用于验证参数和结构化结果。",
)
def sum_values(values: list[int]) -> dict[str, object]:
    return {"count": len(values), "sum": sum(values), "source": "agentflow-deterministic-test"}


@server.tool(
    name="delayed_echo",
    title="延迟回显",
    description="在限定延迟后回显文本，用于超时和取消回归。",
)
async def delayed_echo(text: str, delay_ms: int = 20) -> dict[str, object]:
    await asyncio.sleep(max(0, min(delay_ms, 1_000)) / 1_000)
    return {"text": text, "delay_ms": delay_ms, "source": "agentflow-deterministic-test"}


@server.tool(
    name="large_payload",
    title="大结果",
    description="返回受控大文本，仅用于验证 Gateway 的结果大小拦截。",
)
def large_payload(size: int) -> dict[str, object]:
    bounded_size = max(0, min(size, 20_000))
    return {"payload": "x" * bounded_size, "source": "agentflow-deterministic-test"}


@server.tool(
    name="terminate_process",
    title="终止测试服务",
    description="立即结束当前测试子进程，仅用于 Gateway 崩溃/重启回归。",
)
def terminate_process() -> None:
    # 这是唯一允许显式结束进程的地方：它只会结束本轮由 ClientManager 创建的测试子进程，
    # 不接收路径、命令或客户输入，也不可能影响 AgentFlow 主后端。
    os._exit(17)


if __name__ == "__main__":
    server.run(transport="stdio")
