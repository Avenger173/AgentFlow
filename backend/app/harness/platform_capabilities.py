"""LGM0 的依赖发现与后端准备状态。

本模块刻意只使用 ``importlib.metadata`` 检查安装包版本，不 import MCP、LangGraph 或
LangChain。这样 /health 不会创建图、SQLite Checkpointer、网络连接或 MCP 子进程。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from app.core.config import settings
from app.harness.contracts import PlatformCapabilityDescriptor, RuntimeBackendDescriptor


_REQUIRED_PACKAGES: dict[str, str] = {
    "mcp": "2.1.1",
    "langgraph": "1.2.11",
    "langgraph-checkpoint-sqlite": "3.1.1",
    "langchain-core": "1.6.1",
}
PackageVersionProbe = Callable[[str], str | None]


@dataclass(frozen=True)
class RuntimePlatformProbe:
    """LGM0 的后端与非 Runtime 平台能力快照。"""

    backends: tuple[RuntimeBackendDescriptor, ...]
    capabilities: tuple[PlatformCapabilityDescriptor, ...]


def probe_runtime_platform(
    *, version_probe: PackageVersionProbe | None = None
) -> RuntimePlatformProbe:
    """返回 LGM 后端的准备状态，不导入可选 SDK。"""

    probe = version_probe or _installed_distribution_version
    langgraph_packages = ("langgraph", "langgraph-checkpoint-sqlite")
    mcp_packages = ("mcp",)
    langchain_packages = ("langchain-core",)
    langgraph_versions = _package_versions(langgraph_packages, probe)
    mcp_versions = _package_versions(mcp_packages, probe)
    langchain_versions = _package_versions(langchain_packages, probe)

    return RuntimePlatformProbe(
        backends=(
            RuntimeBackendDescriptor(
                backend_id="native",
                label="AgentFlow Native Runtime",
                enabled=True,
                installed=True,
                ready=True,
                message="当前默认执行路径；不依赖 LGM 可选组件。",
                capabilities=("current_default", "task_audit", "native_checkpoint"),
            ),
            RuntimeBackendDescriptor(
                backend_id="langgraph",
                label="LangGraph ExecutionBackend",
                enabled=settings.langgraph_enabled,
                installed=_versions_match(langgraph_versions, langgraph_packages),
                ready=False,
                message=_langgraph_message(langgraph_versions),
                capabilities=("probe_only", "checkpoint_candidate", "no_customer_routing"),
                versions=langgraph_versions,
            ),
            RuntimeBackendDescriptor(
                backend_id="node_harness",
                label="DeepSeek Harness Node Adapter",
                enabled=settings.node_harness_enabled,
                installed=False,
                ready=False,
                message="由既有 Node Harness 独立探针管理；LGM0 不改变其路由或权限边界。",
                capabilities=("separate_probe", "no_lgm_routing_change"),
            ),
        ),
        capabilities=(
            PlatformCapabilityDescriptor(
                capability_id="mcp_gateway",
                label="MCPGateway 依赖准备",
                enabled=settings.mcp_enabled,
                installed=_versions_match(mcp_versions, mcp_packages),
                ready=False,
                message=_mcp_message(mcp_versions),
                capabilities=("probe_only", "tools_not_exposed", "no_connection"),
                versions=mcp_versions,
            ),
            PlatformCapabilityDescriptor(
                capability_id="langchain_adapter",
                label="LangChain 适配层准备",
                enabled=settings.langchain_adapters_enabled,
                installed=_versions_match(langchain_versions, langchain_packages),
                ready=False,
                message=_langchain_message(langchain_versions),
                capabilities=("probe_only", "no_adapter_enabled"),
                versions=langchain_versions,
            ),
        ),
    )


def runtime_platform_dependency_status() -> dict[str, object]:
    """把探针压缩为 `/health` 的轻量状态，不暴露本机目录或包路径。"""

    probe = probe_runtime_platform()
    optional = (*probe.backends[1:], *probe.capabilities)
    installed = all(
        item.installed
        for item in optional
        if item.label != "DeepSeek Harness Node Adapter"
    )
    return {
        "ready": False,
        "message": (
            "LGM 可选依赖已准备；MCPGateway 已提供一条默认停用的 Wikimedia 公开资料连接，"
            "LangGraph 与 LangChain 客户能力仍未开放。"
            if installed
            else "LGM 可选依赖尚未完整安装；当前 Native Runtime 不受影响。"
        ),
    }


def _installed_distribution_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def _package_versions(
    package_names: tuple[str, ...], probe: PackageVersionProbe
) -> dict[str, str]:
    return {
        package_name: detected
        for package_name in package_names
        if (detected := probe(package_name)) is not None
    }


def _versions_match(versions: dict[str, str], required: tuple[str, ...]) -> bool:
    return set(versions) == set(required) and all(
        versions[package_name] == _REQUIRED_PACKAGES[package_name] for package_name in required
    )


def _langgraph_message(versions: dict[str, str]) -> str:
    if _versions_match(versions, ("langgraph", "langgraph-checkpoint-sqlite")):
        return "依赖已准备，默认关闭；LGM3 前不创建图、Checkpoint 或客户路由。"
    return "LangGraph 或 SQLite Checkpointer 未按 LGM0 锁定版本准备；Native Runtime 继续可用。"


def _mcp_message(versions: dict[str, str]) -> str:
    if _versions_match(versions, ("mcp",)):
        return "MCPGateway 已具备 LGM2 受控公开资料连接；每条客户连接仍需单独启用并经权限确认。"
    return "MCP SDK 未按 LGM0 锁定版本准备；当前不存在 MCP 客户能力。"


def _langchain_message(versions: dict[str, str]) -> str:
    if _versions_match(versions, ("langchain-core",)):
        return "LangChain Core 已准备，适配层尚未实现；不会接管模型、记忆或 RAG。"
    return "LangChain Core 未按 LGM0 锁定版本准备；当前不影响 AgentFlow。"
