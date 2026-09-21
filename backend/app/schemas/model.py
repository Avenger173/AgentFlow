from typing import Literal

from pydantic import BaseModel, Field


ModelTransport = Literal["openai_compatible", "anthropic", "ark_image", "dashscope_multimodal"]
ModelKind = Literal["chat", "image"]
ContextCacheMode = Literal[
    "automatic_observable",
    "explicit_request",
    "observable_if_returned",
    "unknown",
]
ModelRouteScope = Literal[
    "commander_planning",
    "commander_synthesis",
    "document_analysis",
    "document_presentation",
    "data_insight",
    "knowledge_answer",
    "knowledge_deep_analysis",
    "visual_generation",
    "media_image_edit",
]
ModelRouteMode = Literal["inherit_global", "configured"]
ModelRouteAvailability = Literal["ready", "unavailable", "reserved"]


class ModelGenerationParameters(BaseModel):
    """供应商或任务作用域可覆盖的生成参数。

    ``None`` 表示不在该层强制发送：路由层会继续采用任务推荐值或 Provider 配置，
    Provider 配置层则采用部署默认值。最终是否进入请求还受供应商能力声明约束。
    """

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)


class ModelProviderInfo(BaseModel):
    """前端可展示的模型供应商静态信息。

    这里只放供应商能力和默认入口，不放 Key。这样 Qt 后续做模型设置页时，
    只要拉一次这个接口，就能知道当前仓库已经支持哪些 provider。
    """

    provider: str
    label: str
    transport: ModelTransport
    model_kind: ModelKind = "chat"
    default_base_url: str
    default_model: str | None = None
    recommended_models: list[str] = Field(default_factory=list)
    supports_thinking: bool = False
    supports_json_output: bool = True
    supports_tool_calls: bool = True
    supports_temperature: bool = True
    supports_top_p: bool = True
    supports_max_tokens: bool = True
    supports_presence_penalty: bool = False
    supports_frequency_penalty: bool = False
    supports_visual_generation: bool = False
    supports_image_edit: bool = False
    # 只说明已核验的接入方式；实际命中只依赖每次模型响应的 usage 计量。
    context_cache_mode: ContextCacheMode = "unknown"
    context_cache_note: str = ""
    # 仅说明该 provider 是否已有本地加密 Key；响应永远不包含 Key 明文或密文。
    api_key_configured: bool = False
    configured_base_url: str | None = None
    configured_model: str | None = None
    configured_thinking: Literal["enabled", "disabled"] = "disabled"
    configured_parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
    notes: str = ""


class ModelProviderStatus(BaseModel):
    """当前解析出来的模型运行时状态。"""

    provider: str
    label: str = ""
    transport: ModelTransport | None = None
    model_kind: ModelKind = "chat"
    base_url: str | None = None
    model: str | None = None
    # 供应商列表页也要返回当前运行时的思考偏好，否则 Qt 保存后刷新会把下拉框误置为关闭。
    thinking: Literal["enabled", "disabled"] = "disabled"
    api_key_configured: bool = False
    api_key_source: Literal["local_config", "environment", "none"] = "none"
    configuration_source: Literal["local_config", "environment", "default", "error"] = "default"
    secure_storage_available: bool = False
    secure_storage: str = ""
    supports_thinking: bool = False
    parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
    context_cache_mode: ContextCacheMode = "unknown"
    context_cache_note: str = ""
    notes: str = ""
    configuration_error: str | None = None


class ModelProviderListResponse(BaseModel):
    current: ModelProviderStatus
    providers: list[ModelProviderInfo]


class ModelConfigUpdateRequest(BaseModel):
    """模型配置写入请求。

    `api_key` 只允许出现在请求体中，响应模型没有这个字段。前端如果不想修改 Key，
    就不要传 `api_key`；如果想清空本地 Key，传 `clear_api_key=true`。
    """

    provider: str = Field(min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    thinking: Literal["enabled", "disabled"] = "disabled"
    parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
    # 图像 Provider 保存时不能替换全局聊天模型；文本 Provider 默认保持旧行为。
    set_as_default: bool = True
    api_key: str | None = Field(default=None, max_length=20000)
    clear_api_key: bool = False


class ModelConfigResponse(BaseModel):
    """模型配置读取响应，永远不返回 API Key 明文。"""

    provider: str
    label: str = ""
    transport: ModelTransport | None = None
    model_kind: ModelKind = "chat"
    base_url: str | None = None
    model: str | None = None
    thinking: Literal["enabled", "disabled"] = "disabled"
    parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
    api_key_configured: bool = False
    api_key_source: Literal["local_config", "environment", "none"] = "none"
    configuration_source: Literal["local_config", "environment", "default", "error"] = "default"
    secure_storage_available: bool = False
    secure_storage: str = ""
    updated_at: str | None = None
    context_cache_mode: ContextCacheMode = "unknown"
    context_cache_note: str = ""
    configuration_error: str | None = None


class ModelConnectionTestRequest(BaseModel):
    """模型连接测试请求。

    这个请求用于测试“用户正在编辑的表单内容”，不代表保存配置。
    `api_key` 只允许出现在请求体，响应里只返回来源和测试结果。
    """

    provider: str = Field(min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    thinking: Literal["enabled", "disabled"] = "disabled"
    api_key: str | None = Field(default=None, max_length=20000)


class ModelCatalogRequest(BaseModel):
    """读取供应商模型目录；临时 Key 只存在于本次请求内。"""

    provider: str = Field(min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=20000)


class ModelCatalogResponse(BaseModel):
    provider: str
    models: list[str] = Field(default_factory=list)
    source: Literal["remote", "recommended"] = "recommended"
    message: str = ""


class ModelConnectionTestResponse(BaseModel):
    """模型连接测试响应，永远不返回 API Key 明文。"""

    ok: bool
    provider: str
    label: str = ""
    transport: ModelTransport | None = None
    base_url: str | None = None
    model: str | None = None
    api_key_source: Literal["request", "local_config", "environment", "none"] = "none"
    elapsed_ms: int = 0
    message: str
    response_preview: str = ""


class ModelRouteSettings(BaseModel):
    """一个客户可见的模型作用域配置，不保存 API Key。

    作用域 Profile 只决定某类任务该解析哪个 Provider、模型和思考模式。密钥仍由既有
    ``ModelConfigStore`` 按 Provider 独立加密保存，避免出现第二份密钥或把 Key 回显给 UI。
    """

    route_id: ModelRouteScope
    mode: ModelRouteMode = "inherit_global"
    provider: str = ""
    base_url: str = ""
    model: str = ""
    thinking: Literal["enabled", "disabled"] = "disabled"
    parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
    updated_at: str = ""


class ModelRouteAuditSnapshot(BaseModel):
    """一次任务实际解析到的脱敏模型路由快照。"""

    # 同一任务可在不同阶段使用不同模型。阶段必须由 Runtime 的固定代码声明，不能接收客户
    # 自由文本，避免历史审计混入提示词、文件名或其他敏感上下文。
    stage: str = Field(default="", max_length=80)
    route_id: ModelRouteScope
    profile_id: str
    mode: ModelRouteMode
    provider: str = ""
    label: str = ""
    model: str = ""
    thinking: Literal["enabled", "disabled"] = "disabled"
    parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
    compatibility: ModelRouteAvailability = "unavailable"
    note: str = ""


class ModelRouteStatus(BaseModel):
    """模型页展示一条路由 Profile 所需的完整脱敏状态。"""

    route_id: ModelRouteScope
    label: str
    description: str
    required_capabilities: list[str] = Field(default_factory=list)
    settings: ModelRouteSettings
    recommended_parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
    availability: ModelRouteAvailability
    availability_message: str = ""
    resolved: ModelRouteAuditSnapshot | None = None


class ModelRouteListResponse(BaseModel):
    routes: list[ModelRouteStatus]


class ModelRouteUpdateRequest(BaseModel):
    """更新一个作用域的显式模型选择。

    ``inherit_global`` 时 Provider 字段会被清空；``configured`` 时 API 会验证供应商、
    Key 与能力，拒绝通过静默后备掩盖错误配置。
    """

    mode: ModelRouteMode = "inherit_global"
    provider: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    thinking: Literal["enabled", "disabled"] = "disabled"
    parameters: ModelGenerationParameters = Field(default_factory=ModelGenerationParameters)
