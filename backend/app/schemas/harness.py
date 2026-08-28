"""外部 Harness Runtime 的稳定状态协议。

这一层只描述 AgentFlow 是否能安全发现并管理某个外部 Runtime，不能据此推断
模型、Shell、MCP 或写文件能力已经开放。这样 Qt、任务历史和后续 Adapter 都能
使用同一份状态，而不会直接依赖 Node/Python 的内部对象。
"""

from typing import Literal

from pydantic import BaseModel, Field


class HarnessRuntimeStatus(BaseModel):
    """给管理端与后续 Qt 设置页使用的脱敏 Runtime 健康状态。"""

    backend: Literal["node_harness"] = "node_harness"
    enabled: bool = False
    installed: bool = False
    ready: bool = False
    node_version: str | None = None
    harness_version: str | None = None
    message: str
    capabilities: list[str] = Field(default_factory=list)


class HarnessProfilePreflight(BaseModel):
    """项目专属 Node Harness profile 的无密钥组合检查结果。"""

    profile_name: str
    ready: bool = False
    message: str
    disabled_entries: list[str] = Field(default_factory=list)
    permission_mode: Literal["read-only"] = "read-only"
    launch_isolated: bool = True
