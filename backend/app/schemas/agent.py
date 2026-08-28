from typing import Literal

from pydantic import BaseModel, Field


class AgentPermissions(BaseModel):
    file_read: bool = False
    file_write: bool = False
    network: bool = False
    shell: bool = False
    database: bool = False


class AgentLlmConfig(BaseModel):
    provider: str = "inherit"
    model: str = "inherit"
    allow_override: bool = True


class AgentUiConfig(BaseModel):
    type: str = "qt_builtin_page"
    config: str | None = None


class AgentDescriptor(BaseModel):
    id: str
    name: str
    description: str
    category: str
    version: str = "0.1.0"
    icon: str | None = None
    sort_order: int = 100
    source: str = "builtin"
    entrypoint: str | None = None
    enabled: bool = True
    # discovered/enabled/runtime_ready 必须分开：页面可见或 manifest 已加载不等于 Agent 已有
    # 可执行 Runtime。Commander 只应路由 runtime_ready 的能力。
    runtime_ready: bool = False
    health: Literal["unknown", "ready", "degraded", "unhealthy"] = "unknown"
    maturity: Literal["placeholder", "experimental", "mvp", "stable"] = "placeholder"
    builtin: bool = True
    llm: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    tools: list[str] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    ui: AgentUiConfig = Field(default_factory=AgentUiConfig)
    capabilities: list[str] = Field(default_factory=list)


class AgentListResponse(BaseModel):
    total: int
    agents: list[AgentDescriptor]


class AgentRegistryStatusResponse(BaseModel):
    loaded_total: int
    builtin_dir: str
    user_dir: str
    errors: list[str] = Field(default_factory=list)


class AgentActionAdmissionDescriptor(BaseModel):
    """面向客户端公开的总指挥动作准入摘要。

    它描述当前产品允许 Commander 计划到什么程度，不泄露 Tool 参数、文件路径或供应商
    运行配置；桌面端据此提示“可直接委派”或“需要转入专业工作台”。
    """

    agent_id: str
    action: str
    execution_mode: Literal["execute", "guided_handoff", "planning_only"]
    requires_runtime_ready: bool
    material_kind: Literal["document", "dataset", "knowledge_base"] | None = None
    expected_output: str
    verification_scope: str
    recovery_hint: str


class AgentActionAdmissionListResponse(BaseModel):
    total: int
    actions: list[AgentActionAdmissionDescriptor]
