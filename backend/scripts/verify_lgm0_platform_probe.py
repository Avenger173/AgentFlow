"""LGM0：验证可选依赖探针、MCP 契约和 Native 默认路径。

不导入 MCP/LangGraph/LangChain SDK，不建立网络连接，不创建图或 SQLite Checkpointer，
也不读取客户资料。此脚本固定 LGM0 的“依赖已装 != 客户能力已开放”边界。
"""

from __future__ import annotations

import sys
from pathlib import Path
from importlib.metadata import version


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def main() -> None:
    from app.harness.platform_capabilities import probe_runtime_platform, runtime_platform_dependency_status
    from app.mcp.contracts import McpServerReference, McpToolReference

    for module_name in ("mcp", "langgraph", "langchain_core"):
        assert module_name not in sys.modules, f"探针前不应 import {module_name}"

    platform_probe = probe_runtime_platform()
    assert platform_probe.backends[0].backend_id == "native"
    assert platform_probe.backends[0].ready is True
    assert platform_probe.backends[0].enabled is True
    assert all(item.ready is False for item in platform_probe.backends[1:])
    assert all(item.enabled is False for item in platform_probe.backends[1:])
    assert {item.capability_id for item in platform_probe.capabilities} == {
        "mcp_gateway",
        "langchain_adapter",
    }
    capabilities_by_id = {
        item.capability_id: item
        for item in platform_probe.capabilities
    }
    # LGM2 已允许 MCPGateway 作为平台连接总开关启用，但不等于存在通用客户 Tool；
    # LGM0 仍必须保证它没有就绪为任意 MCP 路由，LangChain 也继续默认关闭。
    assert capabilities_by_id["mcp_gateway"].ready is False
    assert capabilities_by_id["langchain_adapter"].ready is False
    assert capabilities_by_id["langchain_adapter"].enabled is False
    assert all(
        module_name not in sys.modules for module_name in ("mcp", "langgraph", "langchain_core")
    ), "版本探针不应 eager import 可选 SDK"

    missing = probe_runtime_platform(version_probe=lambda _package: None)
    assert missing.backends[1].installed is False
    assert all(item.installed is False for item in missing.capabilities)
    dependency_status = runtime_platform_dependency_status()
    assert dependency_status["ready"] is False
    assert "LGM" in str(dependency_status["message"])

    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200, response.text
        health_payload = response.json()
    assert health_payload["capabilities"]["lgm_platform"]["ready"] is False
    assert "LGM" in health_payload["capabilities"]["lgm_platform"]["message"]
    assert all(
        module_name not in sys.modules for module_name in ("mcp", "langgraph", "langchain_core")
    ), "健康检查不应 eager import 可选 SDK"

    server = McpServerReference(
        server_id="project-data",
        display_name="项目数据只读连接",
        transport="stdio",
    )
    tool = McpToolReference(server=server, tool_name="lookup_status")
    assert tool.qualified_name == "mcp.project-data.lookup_status"

    try:
        McpServerReference(server_id="Bad Server", display_name="x", transport="stdio")
    except ValueError:
        pass
    else:
        raise AssertionError("MCP server_id 必须拒绝未规范化名称。")

    # 依赖可在专项验证中真实 import，但仍不创建图、Checkpointer、MCP 会话或网络连接。
    import mcp  # noqa: F401
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import StateGraph

    assert version("mcp") == "2.1.1"
    assert version("langgraph") == "1.2.11"
    assert version("langgraph-checkpoint-sqlite") == "3.1.1"
    assert version("langchain-core") == "1.6.1"
    assert HumanMessage(content="probe").content == "probe"
    assert StateGraph is not None and AsyncSqliteSaver is not None

    print(
        "LGM0 platform probe verification passed: optional SDKs import on demand, "
        "remain inactive, and MCP contracts are stable."
    )


if __name__ == "__main__":
    main()
