"""AgentFlow 与外部执行后端之间的最小稳定契约。

本模块不保存 API Key，也不直接启动 Harness。它只约束控制平面传给执行后端的
非敏感任务描述，以及外部 Runtime 回传给 AgentFlow 事件层的规范化结果。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


HarnessEventKind = Literal[
    "runtime_started",
    "runtime_heartbeat",
    "assistant_final",
    "runtime_failed",
]
HarnessRunStatus = Literal["completed", "failed", "cancelled"]
RuntimeBackendId = Literal[
    "native",
    "node_harness",
    "langgraph",
]
PlatformCapabilityId = Literal["mcp_gateway", "langchain_adapter"]


@dataclass(frozen=True)
class RuntimeBackendDescriptor:
    """控制面可识别的执行后端描述，不携带 SDK 或客户任务对象。

    LGM0 只用它报告 Native / Node Harness / LangGraph 的准备状态。真正的路由、
    checkpoint 和恢复实现留到 LGM3，防止“依赖已安装”被误当成客户任务已经迁移。
    """

    backend_id: RuntimeBackendId
    label: str
    enabled: bool
    installed: bool
    ready: bool
    message: str
    capabilities: tuple[str, ...] = ()
    versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.message.strip():
            raise ValueError("RuntimeBackendDescriptor 必须包含标签和状态说明。")
        if self.ready and (not self.enabled or not self.installed):
            raise ValueError("就绪 Runtime 必须已安装且已启用。")


@dataclass(frozen=True)
class PlatformCapabilityDescriptor:
    """非 Runtime 的可选平台能力描述，例如 MCP Gateway 或 LangChain Adapter。"""

    capability_id: PlatformCapabilityId
    label: str
    enabled: bool
    installed: bool
    ready: bool
    message: str
    capabilities: tuple[str, ...] = ()
    versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.message.strip():
            raise ValueError("PlatformCapabilityDescriptor 必须包含标签和状态说明。")
        if self.ready and (not self.enabled or not self.installed):
            raise ValueError("就绪平台能力必须已安装且已启用。")


@dataclass(frozen=True)
class HarnessExecutionRequest:
    """一次外部 Runtime 委派所需的非敏感上下文。

    Provider、模型和密钥仍由 AgentFlow 的 ModelGateway 与安全存储管理。未来 Bridge
    需要临时注入凭据时，也只能在创建子进程的最后一刻处理，不能写入这个可审计对象。
    """

    task_id: str
    task_text: str
    workspace_dir: Path
    provider_id: str
    model_id: str
    permission_mode: Literal["read-only", "workspace-write"] = "read-only"
    max_output_tokens: int = 2048
    timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空。")
        if not self.task_text.strip():
            raise ValueError("task_text 不能为空。")
        if not self.workspace_dir.is_absolute():
            raise ValueError("workspace_dir 必须是绝对路径。")
        if not self.provider_id.strip() or not self.model_id.strip():
            raise ValueError("provider_id 和 model_id 不能为空。")
        if not 1 <= self.max_output_tokens <= 16_384:
            raise ValueError("max_output_tokens 必须位于 1 到 16384 之间。")
        if not 5.0 <= self.timeout_seconds <= 900.0:
            raise ValueError("timeout_seconds 必须位于 5 到 900 秒之间。")


@dataclass(frozen=True)
class HarnessRuntimeEvent:
    """由外部 Runtime 归一化后的阶段事件。

    `message` 是给当前任务事件流使用的简短摘要，不承载原始 Tool 输出、密钥或绝对路径。
    """

    kind: HarnessEventKind
    message: str
    attempt: int = 1


@dataclass(frozen=True)
class HarnessExecutionResult:
    """一次委派的终态结果，供 RuntimeRouter 映射回现有 Workflow 状态机。"""

    status: HarnessRunStatus
    final_text: str = ""
    failure_code: str | None = None
    session_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


HarnessEventSink = Callable[[HarnessRuntimeEvent], Awaitable[None]]


class ExecutionBackend(Protocol):
    """可插拔执行后端的统一入口。

    Native Runtime 与未来 Node Harness 只通过这一层交换任务和事件；业务 Agent、Qt 页面
    与 API Route 不应依赖某个 SDK 或子进程的私有对象。
    """

    backend_id: str

    async def execute_task(
        self,
        request: HarnessExecutionRequest,
        event_sink: HarnessEventSink,
    ) -> HarnessExecutionResult:
        """执行一次已获准的任务，并按时间顺序发送规范化事件。"""
