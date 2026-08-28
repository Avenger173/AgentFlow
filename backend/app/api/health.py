from app.core.config import settings
from app.schemas.health import HealthResponse
from app.services.data_workspace import data_workspace_dependency_status
from fastapi import APIRouter


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # 健康接口只报告模块级就绪状态。即使可选的数据工作台缺依赖，聊天等其它本地能力仍可启动，
    # 因此不把全局 status 误报为离线；Qt 会把该项降级原因明确展示给客户。
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        capabilities={
            "data_workspace": data_workspace_dependency_status(),
        },
    )
