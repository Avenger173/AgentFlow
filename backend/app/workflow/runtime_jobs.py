"""同进程后台 Runtime Job 管理器。

它不是新的编排引擎，只负责把已经持久化的 Runtime 检查点放到后台线程执行，并把 Runtime
追加的事实事件桥接到现有 WebSocket 缓冲。SQLite 仍是任务、步骤、权限和审计的权威来源；
进程重启后未完成任务不会伪造“仍在运行”，由后续恢复入口按持久化检查点重新排队。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

from app.database.task_repository import (
    append_workflow_event,
    list_interrupted_runtime_task_ids,
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_workflow_plan,
    load_workflow_run,
    save_workflow_runtime_checkpoint,
)
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import WorkflowExecutionResponse
from app.services.task_event_stream import (
    finish_live_task_event_stream,
    open_live_task_event_stream,
    publish_live_task_event,
)
from app.workflow.runtime import (
    fail_prepared_workflow_runtime,
    prepare_workflow_runtime,
    run_prepared_workflow_runtime,
)
from app.workflow.dry_run import clear_dry_run_memory_cache


_START_LOCK = asyncio.Lock()
_ACTIVE_JOBS: dict[str, asyncio.Task[None]] = {}
_ACTIVE_SOURCE_TASKS: dict[str, str] = {}


def recover_interrupted_runtime_jobs() -> list[str]:
    """将进程退出前的瞬时 Runtime 状态收束为可解释的 ``blocked`` 检查点。

    进程内 worker 退出时无法可靠知道某次模型、文件或外部调用是否已经产生副作用。启动阶段
    因此绝不自动重跑；只保留已完成步骤与已有产物，把正在执行的步骤/Tool 标出中断原因，
    让客户在历史中复核后显式 retry 创建一条新的执行记录。
    """

    recovered_task_ids: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        run = load_workflow_run(task_id)
        plan = load_workflow_plan(task_id)
        if run is None or plan is None or run.mode != "runtime":
            # 正常 Runtime 必有计划快照。遇到遗留坏记录时不臆造状态，保留给后续数据库诊断。
            continue

        interruption_message = (
            "服务重启时发现该 Runtime 尚未结束。为避免重复执行或未知副作用，"
            "已保留检查点并停止后续步骤；请复核历史后 retry 创建新的执行记录。"
        )
        interrupted_steps = [
            step.model_copy(
                update={
                    "status": "blocked",
                    "message": interruption_message,
                    "output": {**step.output, "interrupted_by_service_restart": True},
                }
            )
            if step.status in {"running", "waiting_permission"}
            else step
            for step in run.steps
        ]
        interrupted_tools = [
            tool_call.model_copy(
                update={
                    "status": "blocked",
                    "error": "服务重启中断：不会自动重试该工具。",
                    "result": {
                        **tool_call.result,
                        "interrupted_by_service_restart": True,
                    },
                }
            )
            if tool_call.status in {"running", "pending_permission"}
            else tool_call
            for tool_call in list_workflow_tool_calls(task_id)
        ]
        recovered_run = run.model_copy(
            update={
                "status": "blocked",
                "summary": interruption_message,
                "steps": interrupted_steps,
            }
        )
        # ``permission_requests=[]`` 配合 Runtime checkpoint 的增量写入语义，会保留已有审批
        # 决策；恢复扫描不修改客户已经作出的权限选择。
        save_workflow_runtime_checkpoint(
            run=recovered_run,
            plan=plan,
            permission_requests=[],
            artifacts=list_workflow_artifacts(task_id),
            tool_calls=interrupted_tools,
        )
        append_workflow_event(
            task_id=task_id,
            event_name="task_interrupted_by_restart",
            agent_id="workflow_engine",
            message=interruption_message,
            level="warning",
        )
        recovered_task_ids.append(task_id)

    if recovered_task_ids:
        # 查询层的短期缓存只能做加速，不能让历史页在服务重启后继续读到旧 running 快照。
        clear_dry_run_memory_cache()
    return recovered_task_ids


async def start_runtime_job(task_id: str) -> WorkflowExecutionResponse | None:
    """受理任务并立即返回 runtime ID；实际 Tool 执行在后台继续。"""

    async with _START_LOCK:
        existing_runtime_task_id = _ACTIVE_SOURCE_TASKS.get(task_id)
        if existing_runtime_task_id is not None:
            run = await asyncio.to_thread(load_workflow_run, existing_runtime_task_id)
            if run is not None:
                return WorkflowExecutionResponse(
                    source_task_id=task_id,
                    runtime_task_id=existing_runtime_task_id,
                    accepted=False,
                    status=run.status,
                    message="该任务已经在后台执行；请查看同一任务的实时进度。",
                    workflow_run=run,
                )

        prepared = await asyncio.to_thread(prepare_workflow_runtime, task_id)
        if prepared is None or not prepared.accepted:
            return prepared

        runtime_task_id = prepared.runtime_task_id
        # 先创建缓冲，再启动线程。这样即使第一步很快完成，随后连接的 WebSocket 仍能回放它。
        open_live_task_event_stream(runtime_task_id)
        loop = asyncio.get_running_loop()
        background_task = asyncio.create_task(
            _run_runtime_job(
                source_task_id=prepared.source_task_id,
                runtime_task_id=runtime_task_id,
                loop=loop,
            ),
            name=f"agentflow-runtime-{runtime_task_id}",
        )
        _ACTIVE_JOBS[runtime_task_id] = background_task
        _ACTIVE_SOURCE_TASKS[task_id] = runtime_task_id
        # resume 入口的 task_id 就是 runtime_task_id；记住它能避免用户连续点击“继续”创建
        # 两个并发恢复线程。
        _ACTIVE_SOURCE_TASKS[runtime_task_id] = runtime_task_id
        return prepared


async def _run_runtime_job(
    *,
    source_task_id: str,
    runtime_task_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """在线程中调用同步 Runtime，并将每条已落库事件转发给实时缓冲。"""

    published_events: list[Future[None]] = []

    def report(event: TaskLogEvent) -> None:
        # Runtime 线程不能直接操作 asyncio.Condition；使用线程安全调度，把已持久化事实
        # 投递回拥有 WebSocket 的事件循环。
        future = asyncio.run_coroutine_threadsafe(
            publish_live_task_event(
                task_id=event.task_id,
                event=event.event,
                agent_id=event.agent_id,
                message=event.message,
                step_id=event.step_id,
                level=event.level,
            ),
            loop,
        )
        published_events.append(future)

    try:
        await asyncio.to_thread(
            run_prepared_workflow_runtime,
            runtime_task_id=runtime_task_id,
            source_task_id=source_task_id,
            event_reporter=report,
        )
    except Exception as exc:  # pragma: no cover - 仅兜住真正的后台线程异常。
        await asyncio.to_thread(
            fail_prepared_workflow_runtime,
            runtime_task_id=runtime_task_id,
            error=exc,
            event_reporter=report,
        )
    finally:
        # 先冲刷已经从 Runtime 线程投递到事件循环的事实事件，再关闭流。否则非常快的末步可能
        # 在 finish 之后才抵达，客户端会错过“步骤完成/任务完成”而只能等历史页刷新。
        for future in published_events:
            try:
                await asyncio.wrap_future(future)
            except Exception:
                # 实时缓冲失败不影响 SQLite 中已经保存的正式审计记录。
                continue
        await finish_live_task_event_stream(runtime_task_id)
        _ACTIVE_JOBS.pop(runtime_task_id, None)
        _ACTIVE_SOURCE_TASKS.pop(source_task_id, None)
        _ACTIVE_SOURCE_TASKS.pop(runtime_task_id, None)
