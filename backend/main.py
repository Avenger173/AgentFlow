import asyncio
from contextlib import asynccontextmanager

from app.api import agents, chat, data_agent, document_agent, harness, health, knowledge, memories, models, pdf_processing, preferences, tasks, websocket, workflow, workspace
from app.core.config import settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.workflow.runtime_jobs import recover_interrupted_runtime_jobs
from app.database.knowledge_repository import recover_pending_knowledge_base_deletions
from app.services.knowledge_keyword_index import recover_interrupted_knowledge_index_jobs


@asynccontextmanager
async def _agentflow_lifespan(app: FastAPI):
    """在路由开始接收请求前收束上一进程遗留的 Runtime 检查点。"""

    recovered_task_ids = await asyncio.to_thread(recover_interrupted_runtime_jobs)
    recovered_knowledge_job_ids = await asyncio.to_thread(recover_interrupted_knowledge_index_jobs)
    recovered_knowledge_deletion_ids = await asyncio.to_thread(recover_pending_knowledge_base_deletions)
    # 仅保存数量供本进程诊断；客户仍通过任务历史读取每条真实审计事件。
    app.state.recovered_runtime_task_count = len(recovered_task_ids)
    # 知识库索引同样不能在重启后盲目续跑。K1 先收束为失败并等待显式重试，避免磁盘上半写
    # FTS 或未来 Chroma 目录被误当成已验证 generation。
    app.state.recovered_knowledge_index_job_count = len(recovered_knowledge_job_ids)
    # 删除流程先由索引恢复收束 running job，再继续清理私有资料目录，避免重启后留下不可见
    # 但仍可被磁盘占用的候选副本。
    app.state.recovered_knowledge_deletion_count = len(recovered_knowledge_deletion_ids)
    yield


def create_app() -> FastAPI:
    # 后端入口只负责装配中间件和路由。
    # Agent、Workflow、Tool 的真实逻辑后续放到 app/services 或专门模块里。
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Local backend service for AgentFlow.",
        lifespan=_agentflow_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(knowledge.router)
    app.include_router(harness.router)
    app.include_router(agents.router)
    app.include_router(data_agent.router)
    app.include_router(document_agent.router)
    app.include_router(pdf_processing.router)
    app.include_router(models.router)
    app.include_router(preferences.router)
    app.include_router(memories.router)
    app.include_router(chat.router)
    app.include_router(tasks.router)
    app.include_router(workflow.router)
    app.include_router(workspace.router)
    app.include_router(websocket.router)

    return app


app = create_app()
