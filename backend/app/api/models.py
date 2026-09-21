from __future__ import annotations

from time import perf_counter

from app.core.config import settings
from app.schemas.model import (
    ModelConnectionTestRequest,
    ModelConnectionTestResponse,
    ModelCatalogRequest,
    ModelCatalogResponse,
    ModelConfigResponse,
    ModelConfigUpdateRequest,
    ModelProviderInfo,
    ModelProviderListResponse,
    ModelProviderStatus,
    ModelGenerationParameters,
    ModelRouteAuditSnapshot,
    ModelRouteListResponse,
    ModelRouteScope,
    ModelRouteSettings,
    ModelRouteStatus,
    ModelRouteUpdateRequest,
)
from app.services.model_config_store import (
    ModelConfigStoreError,
    StoredModelConfig,
    load_model_config,
    model_secure_storage_available,
    save_model_config,
)
from app.services.model_gateway import (
    ModelGatewayError,
    discover_model_catalog,
    get_model_provider_profile,
    list_model_provider_profiles,
    model_provider_api_key_source,
    model_provider_connection_preview,
    normalize_model_provider,
    resolve_model_runtime,
    resolve_model_runtime_for_route,
    resolve_model_runtime_for_test,
    resolve_visual_model_runtime,
    resolve_visual_model_runtime_for_route,
    validate_model_profile_model,
)
from app.services.model_route_store import (
    MODEL_ROUTE_DEFINITIONS,
    ModelRouteStoreError,
    list_model_route_ids,
    load_model_route_settings,
    save_model_route_settings,
)
from app.services.secret_store import SecretStoreError, SecretStoreUnavailable
from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/providers", response_model=ModelProviderListResponse)
def list_model_providers() -> ModelProviderListResponse:
    """返回支持的模型供应商和当前运行时摘要。

    这个接口只做本地配置解析，不会触发真实模型请求，也不会返回 API Key 明文。
    """

    try:
        stored_config = load_model_config()
    except ModelConfigStoreError:
        # provider 清单仍可展示；当前配置异常会由 current.configuration_error 单独说明。
        stored_config = StoredModelConfig()

    providers: list[ModelProviderInfo] = []
    for profile in list_model_provider_profiles():
        configured = stored_config.provider_config_for(profile.provider)
        api_key_source = model_provider_api_key_source(profile.provider, stored_config=stored_config)
        resolved_base_url, resolved_model = model_provider_connection_preview(
            profile.provider,
            stored_config=stored_config,
        )
        providers.append(ModelProviderInfo(
            provider=profile.provider,
            label=profile.label,
            transport=profile.transport,
            model_kind=profile.model_kind,
            default_base_url=profile.default_base_url,
            default_model=profile.default_model,
            recommended_models=list(profile.recommended_models),
            supports_thinking=profile.supports_thinking,
            supports_json_output=profile.supports_json_output,
            supports_tool_calls=profile.supports_tool_calls,
            supports_temperature=profile.sends_temperature,
            supports_top_p=profile.sends_top_p,
            supports_max_tokens=profile.model_kind == "chat",
            supports_presence_penalty=profile.sends_presence_penalty,
            supports_frequency_penalty=profile.sends_frequency_penalty,
            supports_visual_generation=profile.supports_visual_generation,
            supports_image_edit=profile.supports_image_edit,
            context_cache_mode=profile.context_cache_mode,
            context_cache_note=profile.context_cache_note,
            api_key_configured=api_key_source != "none",
            configured_base_url=resolved_base_url if configured or resolved_base_url != profile.default_base_url else None,
            configured_model=resolved_model if configured or resolved_model != (profile.default_model or "") else None,
            configured_thinking=(
                configured.thinking if configured and configured.thinking in {"enabled", "disabled"} else "disabled"
            ),
            configured_parameters=configured.parameters if configured else ModelGenerationParameters(),
            notes=profile.notes,
        ))

    config = _build_model_config_response()
    current = ModelProviderStatus(
        provider=config.provider,
        label=config.label,
        transport=config.transport,
        model_kind=config.model_kind,
        base_url=config.base_url,
        model=config.model,
        thinking=config.thinking,
        api_key_configured=config.api_key_configured,
        api_key_source=config.api_key_source,
        configuration_source=config.configuration_source,
        secure_storage_available=config.secure_storage_available,
        secure_storage=config.secure_storage,
        supports_thinking=_supports_thinking(config.provider),
        parameters=config.parameters,
        context_cache_mode=config.context_cache_mode,
        context_cache_note=config.context_cache_note,
        configuration_error=config.configuration_error,
        notes="当前模型运行时状态，不包含 API Key 明文。",
    )

    return ModelProviderListResponse(current=current, providers=providers)


@router.get("/config", response_model=ModelConfigResponse)
def get_model_config() -> ModelConfigResponse:
    """读取当前模型配置。

    返回值是 Qt 设置页使用的脱敏视图，只说明 Key 是否存在以及来源。
    """

    return _build_model_config_response()


@router.get("/routes", response_model=ModelRouteListResponse)
def list_model_routes() -> ModelRouteListResponse:
    """列出产品作用域的显式模型路由，不触发真实模型调用。

    这不是另一套密钥管理页：每条路由只显示当前会解析到的脱敏模型和可用性。客户看到
    ``不可用`` 时必须先配置对应 Provider，系统不会静默切换到其它模型继续执行。
    """

    routes: list[ModelRouteStatus] = []
    for raw_route_id in list_model_route_ids():
        route_id = raw_route_id  # 保留稳定字符串，Pydantic 在 response 边界做 Literal 校验。
        label, description, capabilities, runtime_enabled, recommended_parameters = MODEL_ROUTE_DEFINITIONS[route_id]
        settings_value = load_model_route_settings(route_id)  # type: ignore[arg-type]
        if not runtime_enabled:
            routes.append(
                ModelRouteStatus(
                    route_id=route_id,  # type: ignore[arg-type]
                    label=label,
                    description=description,
                    required_capabilities=list(capabilities),
                    settings=settings_value,
                    recommended_parameters=recommended_parameters,
                    availability="reserved",
                    availability_message="该作用域尚未接入通用模型网关，当前配置只作预留。",
                    resolved=None,
                )
            )
            continue
        try:
            resolution = (
                resolve_visual_model_runtime_for_route(route_id, validate=True)
                if _route_uses_image_runtime(capabilities)
                else resolve_model_runtime_for_route(route_id, validate=True)
            )  # type: ignore[arg-type]
        except (ModelGatewayError, ModelRouteStoreError) as exc:
            routes.append(
                ModelRouteStatus(
                    route_id=route_id,  # type: ignore[arg-type]
                    label=label,
                    description=description,
                    required_capabilities=list(capabilities),
                    settings=settings_value,
                    recommended_parameters=recommended_parameters,
                    availability="unavailable",
                    availability_message=str(exc),
                    resolved=_unavailable_route_snapshot(route_id, settings_value, str(exc)),  # type: ignore[arg-type]
                )
            )
            continue
        routes.append(
            ModelRouteStatus(
                route_id=route_id,  # type: ignore[arg-type]
                label=label,
                description=description,
                required_capabilities=list(capabilities),
                settings=settings_value,
                recommended_parameters=recommended_parameters,
                availability="ready",
                availability_message="可用于已接入该作用域的真实模型调用。",
                resolved=resolution.audit_snapshot(),
            )
        )
    return ModelRouteListResponse(routes=routes)


@router.put("/routes/{route_id}", response_model=ModelRouteStatus)
def update_model_route(route_id: ModelRouteScope, request: ModelRouteUpdateRequest) -> ModelRouteStatus:
    """保存一个作用域的模型 Profile，并在落盘前做能力与 Key 准入。"""

    label, description, capabilities, runtime_enabled, recommended_parameters = MODEL_ROUTE_DEFINITIONS[route_id]
    if not runtime_enabled:
        raise HTTPException(status_code=400, detail=f"{label} 尚未接入通用模型网关，不能保存可执行路由。")

    if request.mode == "inherit_global":
        settings_value = ModelRouteSettings(route_id=route_id, parameters=request.parameters)
    else:
        provider = normalize_model_provider(request.provider)
        try:
            profile = get_model_provider_profile(provider)
        except ModelGatewayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _validate_route_capabilities(
            label=label,
            required_capabilities=capabilities,
            supports_json_output=profile.supports_json_output,
            supports_tool_calls=profile.supports_tool_calls,
            supports_thinking=profile.supports_thinking,
            supports_visual_generation=profile.supports_visual_generation,
            supports_image_edit=profile.supports_image_edit,
            thinking=request.thinking,
        )
        _validate_parameter_capabilities(profile, request.parameters)
        base_url = (request.base_url or profile.default_base_url or "").strip().rstrip("/")
        model = (request.model or profile.default_model or "").strip()
        if not base_url or not model:
            raise HTTPException(status_code=400, detail=f"{label} 的显式 Profile 需要完整 Base URL 和模型名称。")
        try:
            validate_model_profile_model(provider, model)
        except ModelGatewayError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        settings_value = ModelRouteSettings(
            route_id=route_id,
            mode="configured",
            provider=provider,
            base_url=base_url,
            model=model,
            thinking=request.thinking,
            parameters=request.parameters,
        )

    try:
        # 在写入前先证明候选 Profile 能解析到自己的 Key。显式配置不能先落下一个无效路由，
        # 再由后续任务悄悄继承全局默认模型。
        if settings_value.mode == "inherit_global":
            if _route_uses_image_runtime(capabilities):
                inherited_visual = resolve_visual_model_runtime_for_route(route_id, validate=True)
                _validate_parameter_capabilities(
                    get_model_provider_profile(inherited_visual.runtime.provider),
                    settings_value.parameters,
                )
            else:
                inherited_runtime = resolve_model_runtime(validate=True)
                _validate_parameter_capabilities(
                    get_model_provider_profile(inherited_runtime.provider),
                    settings_value.parameters,
                )
        else:
            if _route_uses_image_runtime(capabilities):
                resolve_visual_model_runtime(
                    provider=settings_value.provider,
                    base_url=settings_value.base_url,
                    model=settings_value.model,
                    validate=True,
                )
            else:
                resolve_model_runtime_for_test(
                    provider=settings_value.provider,
                    base_url=settings_value.base_url,
                    model=settings_value.model,
                    thinking=settings_value.thinking,
                )
        save_model_route_settings(settings_value)
        resolution = (
            resolve_visual_model_runtime_for_route(route_id, validate=True)
            if _route_uses_image_runtime(capabilities)
            else resolve_model_runtime_for_route(route_id, validate=True)
        )
    except (ModelGatewayError, ModelRouteStoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    saved = load_model_route_settings(route_id)
    return ModelRouteStatus(
        route_id=route_id,
        label=label,
        description=description,
        required_capabilities=list(capabilities),
        settings=saved,
        recommended_parameters=recommended_parameters,
        availability="ready",
        availability_message="配置已保存；后续已接入该作用域的任务会使用此 Profile。",
        resolved=resolution.audit_snapshot(),
    )


@router.post("/catalog", response_model=ModelCatalogResponse)
async def get_model_catalog(request: ModelCatalogRequest) -> ModelCatalogResponse:
    """读取当前账号可见模型；目录不可用时返回可编辑的内置候选。"""

    try:
        models, source, message = await discover_model_catalog(
            provider=request.provider,
            base_url=request.base_url,
            api_key=request.api_key,
        )
    except ModelGatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ModelCatalogResponse(
        provider=normalize_model_provider(request.provider),
        models=models,
        source=source,
        message=message,
    )


@router.post("/test", response_model=ModelConnectionTestResponse)
async def test_model_connection(request: ModelConnectionTestRequest) -> ModelConnectionTestResponse:
    """用当前表单内容测试一次模型连接。

    这个接口是产品体验上的“先试再保存”：用户可以确认 Base URL、模型名和 Key 可用，
    但后端不会写入本地配置，也不会在响应里返回 Key 明文。
    """

    started = perf_counter()
    try:
        runtime, api_key_source = resolve_model_runtime_for_test(
            provider=request.provider,
            base_url=request.base_url,
            model=request.model,
            thinking=request.thinking,
            api_key=request.api_key,
        )
        reply = await runtime.chat(
            system_prompt="你是 AgentFlow 的模型连接测试。只验证通道是否可用，不要输出多余解释。",
            user_message="请只回复 AgentFlow_OK。",
        )
    except ModelGatewayError as exc:
        return _build_model_connection_error_response(
            request=request,
            message=str(exc),
            elapsed_ms=_elapsed_ms(started),
        )

    return ModelConnectionTestResponse(
        ok=True,
        provider=runtime.provider,
        label=runtime.label,
        transport=runtime.transport,
        base_url=runtime.base_url or None,
        model=runtime.model or None,
        api_key_source=api_key_source,
        elapsed_ms=_elapsed_ms(started),
        message="连接成功，模型返回了有效文本。",
        response_preview=reply[:200],
    )


@router.put("/config", response_model=ModelConfigResponse)
def update_model_config(request: ModelConfigUpdateRequest) -> ModelConfigResponse:
    """写入本地模型配置。

    `api_key` 会立刻交给安全存储层加密；如果当前系统没有安全存储能力，
    就拒绝保存，避免把用户 Key 写进明文 JSON。
    """

    provider = normalize_model_provider(request.provider)
    try:
        profile = get_model_provider_profile(provider)
    except ModelGatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base_url = (request.base_url or profile.default_base_url or "").strip().rstrip("/")
    model = (request.model or profile.default_model or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{profile.label} Base URL 不能为空。")
    if not model:
        raise HTTPException(status_code=400, detail=f"{profile.label} 模型名称不能为空。")
    try:
        validate_model_profile_model(provider, model)
    except ModelGatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _validate_parameter_capabilities(profile, request.parameters)

    api_key = request.api_key.strip() if request.api_key is not None else None
    clear_api_key = request.clear_api_key or api_key == ""
    api_key_to_save = None if clear_api_key else api_key

    try:
        # Key 已按 provider 隔离保存。切换默认模型时保留其它 provider 的密文，
        # 但 Runtime 只会解密当前 provider 对应的 Key，不会跨供应商误用凭据。
        save_model_config(
            provider=provider,
            base_url=base_url,
            model=model,
            thinking=request.thinking,
            parameters=request.parameters,
            set_as_default=request.set_as_default and profile.model_kind == "chat",
            api_key=api_key_to_save,
            clear_api_key=clear_api_key,
        )
    except SecretStoreUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ModelConfigStoreError, SecretStoreError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _build_provider_config_response(provider)


def _validate_route_capabilities(
    *,
    label: str,
    required_capabilities: tuple[str, ...],
    supports_json_output: bool,
    supports_tool_calls: bool,
    supports_thinking: bool,
    supports_visual_generation: bool,
    supports_image_edit: bool,
    thinking: str,
) -> None:
    """拒绝能力不匹配的显式 Profile，不能把它降级为全局模型。"""

    unavailable: list[str] = []
    if "json_output" in required_capabilities and not supports_json_output:
        unavailable.append("JSON Output")
    if "tool_calls" in required_capabilities and not supports_tool_calls:
        unavailable.append("Tool Calls")
    if "visual_generation" in required_capabilities and not supports_visual_generation:
        unavailable.append("视觉生成")
    if "image_edit" in required_capabilities and not supports_image_edit:
        unavailable.append("图片编辑")
    if thinking == "enabled" and not supports_thinking:
        unavailable.append("思考模式")
    if unavailable:
        raise HTTPException(
            status_code=400,
            detail=f"{label} 需要的能力当前 Provider 不满足：{'、'.join(unavailable)}。请更换兼容模型。",
        )


def _validate_parameter_capabilities(profile: object, parameters: ModelGenerationParameters) -> None:
    unsupported: list[str] = []
    checks = (
        ("temperature", "温度", "sends_temperature"),
        ("top_p", "Top P", "sends_top_p"),
        ("presence_penalty", "Presence Penalty", "sends_presence_penalty"),
        ("frequency_penalty", "Frequency Penalty", "sends_frequency_penalty"),
    )
    for field, label, capability in checks:
        if getattr(parameters, field) is not None and not bool(getattr(profile, capability, False)):
            unsupported.append(label)
    if parameters.max_tokens is not None and getattr(profile, "model_kind", "chat") != "chat":
        unsupported.append("最大输出 Token")
    if unsupported:
        provider_label = str(getattr(profile, "label", "当前 Provider"))
        raise HTTPException(
            status_code=400,
            detail=f"{provider_label} 当前适配不发送这些参数：{'、'.join(unsupported)}。请留空使用供应商默认值。",
        )


def _route_uses_image_runtime(required_capabilities: tuple[str, ...]) -> bool:
    """根据显式能力而非 route 名判断是否应解析专用图像运行时。"""

    return "visual_generation" in required_capabilities or "image_edit" in required_capabilities


def _unavailable_route_snapshot(
    route_id: ModelRouteScope,
    settings_value: ModelRouteSettings,
    message: str,
) -> ModelRouteAuditSnapshot:
    """为未就绪的路由保留脱敏说明，帮助客户修复而不是猜测回退模型。"""

    return ModelRouteAuditSnapshot(
        route_id=route_id,
        profile_id=f"route:{route_id}",
        mode=settings_value.mode,
        provider=settings_value.provider,
        model=settings_value.model,
        thinking=settings_value.thinking,
        parameters=settings_value.parameters,
        compatibility="unavailable",
        note=message[:240],
    )


def _build_model_config_response() -> ModelConfigResponse:
    try:
        stored_config = load_model_config()
    except ModelConfigStoreError as exc:
        return ModelConfigResponse(
            provider=settings.llm_provider or "mock",
            configuration_source="error",
            configuration_error=str(exc),
            secure_storage_available=model_secure_storage_available(),
        )

    try:
        runtime = resolve_model_runtime(validate=False)
    except ModelGatewayError as exc:
        provider = normalize_model_provider(stored_config.provider or settings.llm_provider or "mock")
        return ModelConfigResponse(
            provider=provider,
            api_key_configured=_stored_key_matches_provider(stored_config, provider)
            or bool(settings.any_llm_api_key),
            api_key_source=_api_key_source(None, stored_config, provider),
            configuration_source="error",
            secure_storage_available=model_secure_storage_available(),
            secure_storage=stored_config.secure_storage,
            updated_at=stored_config.updated_at or None,
            context_cache_mode=_context_cache_mode(provider),
            context_cache_note=_context_cache_note(provider),
            configuration_error=str(exc),
        )

    return ModelConfigResponse(
        provider=runtime.provider,
        label=runtime.label,
        transport=runtime.transport,
        model_kind="chat",
        base_url=runtime.base_url or None,
        model=runtime.model or None,
        thinking=runtime.thinking if runtime.thinking in {"enabled", "disabled"} else "disabled",
        parameters=ModelGenerationParameters(
            temperature=runtime.temperature,
            top_p=runtime.top_p,
            max_tokens=runtime.max_tokens,
            presence_penalty=runtime.presence_penalty,
            frequency_penalty=runtime.frequency_penalty,
        ),
        api_key_configured=runtime.api_key_configured,
        api_key_source=_api_key_source(runtime.api_key_configured, stored_config, runtime.provider),
        configuration_source=_configuration_source(stored_config),
        secure_storage_available=model_secure_storage_available(),
        secure_storage=stored_config.secure_storage,
        updated_at=stored_config.updated_at or None,
        context_cache_mode=runtime.context_cache_mode,
        context_cache_note=runtime.context_cache_note,
    )


def _build_provider_config_response(provider: str) -> ModelConfigResponse:
    """构造某个 Provider 的脱敏配置，不把图像配置误设为当前聊天 Runtime。"""

    stored_config = load_model_config()
    profile = get_model_provider_profile(provider)
    configured = stored_config.provider_config_for(provider)
    api_key_source = model_provider_api_key_source(provider, stored_config=stored_config)
    resolved_base_url, resolved_model = model_provider_connection_preview(
        provider,
        stored_config=stored_config,
    )
    secret = stored_config.api_key_secret_for(provider)
    if secret is None:
        credential_provider = profile.credential_provider.strip()
        if credential_provider:
            secret = stored_config.api_key_secret_for(credential_provider)
    return ModelConfigResponse(
        provider=provider,
        label=profile.label,
        transport=profile.transport,
        model_kind=profile.model_kind,
        base_url=resolved_base_url or None,
        model=resolved_model or None,
        thinking=(
            configured.thinking
            if configured and configured.thinking in {"enabled", "disabled"}
            else "disabled"
        ),
        parameters=configured.parameters if configured else ModelGenerationParameters(),
        api_key_configured=api_key_source != "none",
        api_key_source=api_key_source,
        configuration_source="local_config" if configured else "environment" if api_key_source == "environment" else "default",
        secure_storage_available=model_secure_storage_available(),
        secure_storage=str(secret.get("storage") or "") if secret else "",
        updated_at=configured.updated_at if configured and configured.updated_at else None,
        context_cache_mode=profile.context_cache_mode,
        context_cache_note=profile.context_cache_note,
    )


def _configuration_source(stored_config: StoredModelConfig) -> str:
    if stored_config.provider:
        return "local_config"
    if settings.llm_provider != "mock" or settings.llm_base_url or settings.llm_model or settings.any_llm_api_key:
        return "environment"
    return "default"


def _api_key_source(
    runtime_key_configured: bool | None,
    stored_config: StoredModelConfig,
    provider: str,
) -> str:
    if _stored_key_matches_provider(stored_config, provider):
        return "local_config"
    if runtime_key_configured:
        return "environment"
    return "none"


def _stored_key_matches_provider(stored_config: StoredModelConfig, provider: str) -> bool:
    return stored_config.api_key_configured_for(provider)


def _supports_thinking(provider: str) -> bool:
    try:
        return get_model_provider_profile(provider).supports_thinking
    except ModelGatewayError:
        return False


def _context_cache_mode(provider: str) -> str:
    try:
        return get_model_provider_profile(provider).context_cache_mode
    except ModelGatewayError:
        return "unknown"


def _context_cache_note(provider: str) -> str:
    try:
        return get_model_provider_profile(provider).context_cache_note
    except ModelGatewayError:
        return ""


def _build_model_connection_error_response(
    *,
    request: ModelConnectionTestRequest,
    message: str,
    elapsed_ms: int,
) -> ModelConnectionTestResponse:
    """把测试失败转换成 200 + ok=false，方便 Qt 直接展示可理解的失败原因。"""

    provider = normalize_model_provider(request.provider)
    label = ""
    transport = None
    try:
        profile = get_model_provider_profile(provider)
        label = profile.label
        transport = profile.transport
    except ModelGatewayError:
        pass

    return ModelConnectionTestResponse(
        ok=False,
        provider=provider,
        label=label,
        transport=transport,
        base_url=(request.base_url or "").strip().rstrip("/") or None,
        model=(request.model or "").strip() or None,
        elapsed_ms=elapsed_ms,
        message=message,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))
