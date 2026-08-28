"""知识库深度任务的后台受理 Adapter。

Qt 知识库工作台与 Commander 都需要创建同一种 K4 任务。这里把“冻结范围 -> 写入初始
checkpoint -> 在独立线程运行”的执行接缝收拢，避免 Commander 直接依赖 FastAPI 路由或
在父 Runtime 内同步等待整个 Map/Reduce。任务本身仍由 ``knowledge_deep_task`` 服务持久化。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from uuid import uuid4

from app.database.knowledge_repository import KnowledgeBaseNotFoundError, KnowledgeBaseUnavailableError
from app.schemas.knowledge import KnowledgeDeepTaskRequest, KnowledgeDeepTaskScope
from app.services.knowledge_deep_task import (
    KnowledgeDeepTaskMapExecutionError,
    KnowledgeDeepTaskScopeError,
    build_knowledge_deep_task_scope,
    create_knowledge_deep_task_map_queued_run,
    mark_knowledge_deep_task_unexpected_failure,
    run_knowledge_deep_task,
)


class KnowledgeDeepTaskDispatchError(RuntimeError):
    """把可预期的资料库范围/受理失败收束为不泄露内部路径的 C5 错误。"""


@dataclass(frozen=True)
class KnowledgeDeepTaskDispatchReceipt:
    """父 Runtime 需要的最小子任务身份，不复制正文、来源或模型输出。"""

    task_id: str
    map_unit_count: int


def start_knowledge_deep_task_in_background(
    request: KnowledgeDeepTaskRequest,
) -> KnowledgeDeepTaskDispatchReceipt:
    """冻结 K4 scope 并启动守护线程，立即把可追溯子任务 ID 返回给父 Runtime。

    线程只承载长耗时模型循环；所有状态、检查点、恢复和最终结果仍写入统一 SQLite。应用在
    子任务运行期间退出时不会伪报完成，重启后客户可从 K4 的既有继续入口恢复同一任务。
    """

    try:
        scope = build_knowledge_deep_task_scope(request)
        task_id = f"task_k4_{uuid4().hex[:12]}"
        create_knowledge_deep_task_map_queued_run(task_id=task_id, scope=scope)
    except (
        KnowledgeBaseNotFoundError,
        KnowledgeBaseUnavailableError,
        KnowledgeDeepTaskScopeError,
        KnowledgeDeepTaskMapExecutionError,
    ) as exc:
        raise KnowledgeDeepTaskDispatchError(str(exc)) from exc

    worker = threading.Thread(
        target=_run_knowledge_deep_task_worker,
        args=(task_id, scope),
        name=f"agentflow-k4-{task_id[-6:]}",
        daemon=True,
    )
    worker.start()
    return KnowledgeDeepTaskDispatchReceipt(task_id=task_id, map_unit_count=len(scope.map_units))


def _run_knowledge_deep_task_worker(task_id: str, scope: KnowledgeDeepTaskScope) -> None:
    """在线程中建立独立事件循环；异常必须落成可恢复任务状态，不能静默丢失。"""

    async def run() -> None:
        try:
            await run_knowledge_deep_task(task_id=task_id, scope=scope)
        except Exception:
            mark_knowledge_deep_task_unexpected_failure(task_id)

    asyncio.run(run())
