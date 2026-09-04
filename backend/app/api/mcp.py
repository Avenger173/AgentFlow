"""LGM2 内置 MCP 连接管理入口。

首期没有客户可填的 URL、命令或密钥。页面只管理随版本发布的 Wikimedia 只读连接，并把
工具发现限制为固定目录；未来新连接必须先经过独立产品方案与 Gateway 审查。
"""

from __future__ import annotations

from app.core.config import settings
from app.mcp.connection_store import (
    PUBLIC_REFERENCE_CONNECTION_ID,
    McpConnectionStoreError,
    load_public_reference_connection,
    record_public_reference_check,
    set_public_reference_enabled,
)
from app.mcp.gateway import McpGateway
from app.schemas.mcp import (
    McpConnectionInfo,
    McpConnectionListResponse,
    McpConnectionMutationResponse,
    McpConnectionToolInfo,
)
from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get("/connections", response_model=McpConnectionListResponse)
async def list_connections() -> McpConnectionListResponse:
    """返回内置连接状态，不启动子进程、不触发网络。"""

    return McpConnectionListResponse(total=1, connections=[_public_reference_info()])


@router.post(
    f"/connections/{PUBLIC_REFERENCE_CONNECTION_ID}/enable",
    response_model=McpConnectionMutationResponse,
)
async def enable_public_reference_connection() -> McpConnectionMutationResponse:
    """显式启用固定连接；此操作本身不联网、不读取公开页面。"""

    if not settings.mcp_enabled:
        raise HTTPException(status_code=409, detail="MCP 平台连接已被部署配置关闭。")
    try:
        set_public_reference_enabled(True)
    except McpConnectionStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return McpConnectionMutationResponse(
        connection=_public_reference_info(),
        message="已启用 Wikimedia 公开资料连接。实际检索仍会在任务执行前按联网权限策略确认。",
    )


@router.post(
    f"/connections/{PUBLIC_REFERENCE_CONNECTION_ID}/disable",
    response_model=McpConnectionMutationResponse,
)
async def disable_public_reference_connection() -> McpConnectionMutationResponse:
    """停用后 Commander 不再计划或调用此 Tool。"""

    try:
        set_public_reference_enabled(False)
    except McpConnectionStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return McpConnectionMutationResponse(
        connection=_public_reference_info(),
        message="已停用 Wikimedia 公开资料连接；后续对话不会再承诺该能力。",
    )


@router.post(
    f"/connections/{PUBLIC_REFERENCE_CONNECTION_ID}/test",
    response_model=McpConnectionMutationResponse,
)
async def test_public_reference_connection() -> McpConnectionMutationResponse:
    """检测 MCP 协议和固定 Tool 目录，不检索任何公开页面。"""

    if not settings.mcp_enabled:
        raise HTTPException(status_code=409, detail="MCP 平台连接已被部署配置关闭。")
    try:
        state = load_public_reference_connection()
    except McpConnectionStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not state.enabled:
        raise HTTPException(status_code=409, detail="请先启用 Wikimedia 公开资料连接，再检测 Tool。")

    gateway = McpGateway.for_public_reference()
    try:
        tools = await gateway.discover_tools()
        approved = {"search_wikimedia"}
        discovered = {item.reference.tool_name for item in tools}
        if discovered != approved:
            raise RuntimeError("固定公开资料连接返回了未批准的 Tool 目录。")
        record_public_reference_check(tool_count=len(tools))
    except Exception as exc:
        error_code = getattr(exc, "code", "mcp_connection_failed")
        try:
            record_public_reference_check(error_code=str(error_code))
        except McpConnectionStoreError:
            pass
        raise HTTPException(status_code=502, detail="MCP 连接检测失败；没有执行公开资料检索。") from exc
    finally:
        await gateway.close()

    return McpConnectionMutationResponse(
        connection=_public_reference_info(),
        message="连接检测通过：已发现 1 个受控只读 Tool，尚未读取任何公开页面。",
    )


def _public_reference_info() -> McpConnectionInfo:
    try:
        state = load_public_reference_connection()
    except McpConnectionStoreError:
        state = None
    if not settings.mcp_enabled:
        status = "platform_disabled"
    elif state is None or not state.enabled:
        status = "disabled"
    elif state.last_check_status == "failed":
        status = "degraded"
    else:
        status = "ready"
    return McpConnectionInfo(
        connection_id=PUBLIC_REFERENCE_CONNECTION_ID,
        display_name="Wikimedia 公开资料参考",
        description="只读检索固定的中文维基百科公开页面，返回可追溯参考线索；不执行任意网页抓取。",
        transport="stdio",
        status=status,
        enabled=bool(state.enabled) if state is not None else False,
        requires_network=True,
        requires_command_confirmation=True,
        origin_summary="AgentFlow 内置 MCP 服务 -> 固定 zh.wikipedia.org Action API",
        last_checked_at=state.last_checked_at if state is not None else "",
        last_tool_count=state.last_tool_count if state is not None else 0,
        last_error_code=state.last_error_code if state is not None else "",
        tools=[
            McpConnectionToolInfo(
                qualified_name="mcp.public-reference.search_wikimedia",
                title="检索 Wikimedia 公开资料",
                description="返回最多三条标题、链接、摘要和抓取时间；只作为公开资料参考线索。",
                required_permissions=["network", "shell"],
                commander_selectable=bool(state and state.enabled and settings.mcp_enabled),
            )
        ],
    )
