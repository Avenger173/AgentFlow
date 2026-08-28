"""外部 Harness Runtime 的只读管理接口。"""

import asyncio

from app.harness.node_profile import preflight_node_harness_profile
from app.harness.node_runtime import get_node_harness_runtime_status
from app.schemas.harness import HarnessProfilePreflight, HarnessRuntimeStatus
from fastapi import APIRouter, Query


router = APIRouter(prefix="/api/harness", tags=["harness"])


@router.get("/runtime", response_model=HarnessRuntimeStatus)
async def get_harness_runtime_status(
    refresh: bool = Query(default=False, description="是否绕过短时本地探针缓存。"),
) -> HarnessRuntimeStatus:
    """查询项目内 Node Harness 状态，不启动模型、Agent 或工具。"""

    # 版本探针会创建极短 Node 子进程，不能占用 FastAPI 事件循环。
    return await asyncio.to_thread(get_node_harness_runtime_status, refresh=refresh)


@router.post("/profile/preflight", response_model=HarnessProfilePreflight)
async def preflight_harness_profile() -> HarnessProfilePreflight:
    """生成并回读 AgentFlow 专属只读 profile，不启动模型或工具。"""

    # 该动作会在项目数据目录初始化 profile 文件并运行一次 dsh 配置组合，因此不做成 GET。
    return await asyncio.to_thread(preflight_node_harness_profile)
