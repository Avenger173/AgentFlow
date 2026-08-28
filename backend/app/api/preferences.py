from __future__ import annotations

from app.schemas.settings import (
    RuntimePreferenceOption,
    RuntimePreferencesResponse,
    RuntimePreferencesUpdateRequest,
)
from app.services.runtime_preferences_store import (
    RuntimePreferencesStoreError,
    load_runtime_preferences,
    save_runtime_preferences,
)
from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/runtime-preferences", response_model=RuntimePreferencesResponse)
def get_runtime_preferences() -> RuntimePreferencesResponse:
    """读取平台运行偏好。

    这个接口不会读取任务内容或密钥，只返回权限确认策略和 Agent 表达风格。
    """

    try:
        preferences = load_runtime_preferences()
    except RuntimePreferencesStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _build_response(preferences)


@router.put("/runtime-preferences", response_model=RuntimePreferencesResponse)
def update_runtime_preferences(
    request: RuntimePreferencesUpdateRequest,
) -> RuntimePreferencesResponse:
    """保存平台运行偏好。

    即使用户选择 `full_access`，真实 Runtime 仍要保留风险提示和审计记录；这里保存的是用户体验
    和审批默认值，不是模型自我授权。
    """

    try:
        preferences = save_runtime_preferences(
            permission_policy=request.permission_policy,
            personality=request.personality,
            memory_enabled=request.memory_enabled,
        )
    except RuntimePreferencesStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _build_response(preferences)


def _build_response(preferences) -> RuntimePreferencesResponse:
    return RuntimePreferencesResponse(
        permission_policy=preferences.permission_policy,
        personality=preferences.personality,
        memory_enabled=preferences.memory_enabled,
        updated_at=preferences.updated_at,
        notes=(
            "运行偏好只影响默认确认策略、Agent 表达风格和总指挥是否读取已确认的长期记忆；"
            "真实文件写入、联网、Shell、插件等高权限动作仍由 Runtime 权限边界和审计记录控制。"
        ),
        permission_policy_options=_permission_policy_options(),
        personality_options=_personality_options(),
    )


def _permission_policy_options() -> list[RuntimePreferenceOption]:
    return [
        RuntimePreferenceOption(
            value="always_ask",
            label="请求批准",
            description="编辑外部文件、联网、命令和高权限动作前都请求确认。",
        ),
        RuntimePreferenceOption(
            value="auto_approve",
            label="替我审批",
            description="低风险动作自动批准；修改、联网和高危动作仍记录审计并可提示。",
        ),
        RuntimePreferenceOption(
            value="smart_confirm",
            label="风险操作确认",
            description="默认推荐：只对检测到的风险操作请求批准。",
        ),
        RuntimePreferenceOption(
            value="full_access",
            label="完全访问",
            description="尽量减少打断，但高危动作仍保留风险提示和审计入口。",
        ),
    ]


def _personality_options() -> list[RuntimePreferenceOption]:
    return [
        RuntimePreferenceOption(
            value="professional",
            label="专业稳重",
            description="表达清晰、克制，优先给出可执行结论。",
        ),
        RuntimePreferenceOption(
            value="concise",
            label="简洁直接",
            description="减少解释，优先给步骤和结果。",
        ),
        RuntimePreferenceOption(
            value="warm",
            label="温和陪伴",
            description="语气更有耐心，适合学习、写作和长任务。",
        ),
        RuntimePreferenceOption(
            value="creative",
            label="创意活泼",
            description="适合头脑风暴、产品构思和探索性任务。",
        ),
    ]
