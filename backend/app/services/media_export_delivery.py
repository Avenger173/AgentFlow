"""图片 PNG 导出的统一任务交付层。

图片工作区本身保存不可变 source/revision/manifest；本模块只负责把一次客户确认的导出
接入既有 ``WorkflowRun``、事件、工具调用和 Artifact 历史。这样图片工作台不会出现
"文件已经生成，但任务历史没有任何交付记录" 的断层。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from threading import Event, RLock
from time import perf_counter
from typing import Any

from app.database.task_repository import (
    list_interrupted_runtime_task_ids,
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.events import TaskLogEvent
from app.schemas.media_workspace import (
    MediaImageExportInfo,
    MediaImageExportRequest,
    MediaImageExportTaskResultResponse,
)
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    TaskControlResponse,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.media_workspace import (
    MediaWorkspaceError,
    discard_media_image_export,
    export_media_image_revision,
    find_media_export_for_task,
    resolve_media_export_download_path,
)
from app.services.task_event_stream import publish_live_task_event
from app.workflow.state_machine import can_cancel


MEDIA_EXPORT_STEP_ID = "media_image_export"
MEDIA_EXPORT_TOOL_NAME = "media.export_png"
MEDIA_AGENT_ID = "media_agent"
_TASK_TIMEOUT_MS = 60_000
_TOOL_TIMEOUT_MS = 45_000

# Pillow/文件复制在工作线程里执行，不能安全强杀。取消先持久化，然后在文件已经写入但
# Artifact 尚未登记的边界清理受控副本，避免历史页展示半完成交付物。
_TASK_LOCK = RLock()
_TASK_CANCEL_EVENTS: dict[str, Event] = {}


def create_media_export_queued_run(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
) -> WorkflowRun:
    """登记待执行导出，使任务历史在后台复制开始前就可见。"""

    now = _now()
    with _TASK_LOCK:
        _TASK_CANCEL_EVENTS[task_id] = Event()
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="图片 PNG 导出已受理，尚未写入交付文件。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EXPORT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EXPORT_TOOL_NAME,
                status="pending",
                message="已受理图片 PNG 导出，等待校验当前版本。",
                output=_base_output(project_id=project_id, revision_id=revision_id, request=request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "图片 PNG 导出已受理，将只写入新的受控交付文件。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_media_export_task(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
) -> MediaImageExportTaskResultResponse:
    """复制、回读校验并登记一张 PNG；未验证产物绝不写入 Artifact。"""

    if _is_cancel_requested(task_id):
        try:
            return _cancelled_task_result(task_id)
        finally:
            _forget_cancel_state(task_id)

    started_at = _now()
    started_clock = perf_counter()
    running_events = [
        _event(task_id, 1, "task_queued", "图片 PNG 导出已受理，将只写入新的受控交付文件。"),
        _event(task_id, 2, "task_started", "正在校验图片版本和源文件完整性。", step_id=MEDIA_EXPORT_STEP_ID),
        _event(task_id, 3, "tool_started", "正在复制 PNG 并回读验证像素文件。", step_id=MEDIA_EXPORT_STEP_ID),
    ]
    export: MediaImageExportInfo | None = None
    try:
        with _TASK_LOCK:
            if _is_cancel_requested(task_id):
                return _cancelled_task_result(task_id)
            _save_running_run(
                task_id=task_id,
                project_id=project_id,
                revision_id=revision_id,
                request=request,
                started_at=started_at,
                events=running_events,
            )

        await publish_live_task_event(
            task_id=task_id,
            event="task_started",
            agent_id=MEDIA_AGENT_ID,
            step_id=MEDIA_EXPORT_STEP_ID,
            message="正在校验图片版本和源文件完整性。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="tool_started",
            agent_id=MEDIA_AGENT_ID,
            step_id=MEDIA_EXPORT_STEP_ID,
            message="正在复制 PNG 并回读验证像素文件。",
        )

        export = await asyncio.to_thread(
            export_media_image_revision,
            project_id=project_id,
            revision_id=revision_id,
            filename=request.filename,
            task_id=task_id,
        )
        with _TASK_LOCK:
            if _is_cancel_requested(task_id):
                _discard_export_quietly(project_id=project_id, export_id=export.export_id)
                return _cancelled_task_result(task_id)

            duration_ms = _duration_ms(started_clock)
            artifact = _artifact_for_export(task_id=task_id, export=export)
            message = "图片 PNG 已导出并通过文件回读验证。"
            run = _completed_run(
                task_id=task_id,
                project_id=project_id,
                revision_id=revision_id,
                request=request,
                export=export,
                started_at=started_at,
                duration_ms=duration_ms,
                message=message,
            )
            save_workflow_run(
                run=run,
                events=[
                    *running_events,
                    _event(task_id, 4, "artifact_saved", "PNG 已回读验证并登记为受控交付物。", step_id=MEDIA_EXPORT_STEP_ID),
                    _event(task_id, 5, "task_completed", "图片 PNG 导出完成，源图片和版本均未修改。", step_id=MEDIA_EXPORT_STEP_ID),
                ],
                plan=None,
                artifacts=[artifact],
                tool_calls=[
                    _tool_call(
                        task_id=task_id,
                        project_id=project_id,
                        revision_id=revision_id,
                        request=request,
                        status="completed",
                        duration_ms=duration_ms,
                        export=export,
                    )
                ],
            )
        await publish_live_task_event(
            task_id=task_id,
            event="artifact_saved",
            agent_id=MEDIA_AGENT_ID,
            step_id=MEDIA_EXPORT_STEP_ID,
            message="PNG 已回读验证并登记为受控交付物。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id=MEDIA_AGENT_ID,
            step_id=MEDIA_EXPORT_STEP_ID,
            message="图片 PNG 导出完成，源图片和版本均未修改。",
        )
        return MediaImageExportTaskResultResponse(
            task_id=task_id,
            status="completed",
            summary=run.summary,
            message=message,
            export=export,
        )
    except MediaWorkspaceError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            revision_id=revision_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message=str(exc),
            events=running_events,
            export=export,
        )
    except Exception:  # pragma: no cover - 兜底仍须留下可见终态并清理未登记导出。
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            revision_id=revision_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message="图片 PNG 导出发生未预期错误，未保留不完整交付文件。",
            events=running_events,
            export=export,
        )
    finally:
        _forget_cancel_state(task_id)


async def cancel_media_export_task(task_id: str) -> TaskControlResponse | None:
    """协作式取消图片导出；只处理本模块创建的统一 Runtime 任务。"""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_media_export_run(run):
            return None
        assert run is not None
        if not can_cancel(run.status):
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="图片导出任务已经结束，不能取消已有终态。",
                workflow_run=run,
            )
        _TASK_CANCEL_EVENTS.setdefault(task_id, Event()).set()
        cancelled = _cancelled_run(run)
        existing_events = load_task_log_events(task_id)
        save_workflow_run(
            run=cancelled,
            events=[
                *existing_events,
                _event(
                    task_id,
                    len(existing_events) + 1,
                    "task_cancelled",
                    "图片 PNG 导出已取消，未登记新的交付文件。",
                    step_id=MEDIA_EXPORT_STEP_ID,
                    level="warning",
                ),
            ],
            plan=None,
            artifacts=list_workflow_artifacts(task_id),
            tool_calls=list_workflow_tool_calls(task_id),
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EXPORT_STEP_ID,
        level="warning",
        message="图片 PNG 导出已取消，未登记新的交付文件。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="图片 PNG 导出已取消；源图片和版本未被修改。",
        workflow_run=cancelled,
    )


def get_media_export_task_result(task_id: str) -> MediaImageExportTaskResultResponse | None:
    """从 SQLite 重建导出任务结果；服务重启后仍能看到已验证的 PNG。"""

    run = load_workflow_run(task_id)
    if not _is_media_export_run(run):
        return None
    assert run is not None
    step = next(item for item in run.steps if item.step_id == MEDIA_EXPORT_STEP_ID)
    output = step.output
    export = _load_export(output)
    return MediaImageExportTaskResultResponse(
        task_id=task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", step.message)),
        export=export if run.status == "completed" else None,
    )


def recover_interrupted_media_export_tasks() -> list[str]:
    """在启动时对账中断的 PNG 导出，不自动重新复制或创建第二个导出。

    通用 Runtime 恢复器只处理有 ``WorkflowPlan`` 的编排任务；图片导出是单步骤本地交付，
    因此在这里用 manifest 中冻结的 task_id 反查已提交的 PNG。文件存在、哈希匹配且能以
    PNG 回读时才补写 Artifact 并完成任务；其他情况收束为失败，保留原版本供用户显式重试。
    """

    recovered_task_ids: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        with _TASK_LOCK:
            run = load_workflow_run(task_id)
            if not _is_media_export_run(run):
                continue
            assert run is not None
            step = next(item for item in run.steps if item.step_id == MEDIA_EXPORT_STEP_ID)
            project_id = step.output.get("project_id")
            revision_id = step.output.get("revision_id")
            requested_filename = step.output.get("requested_filename")
            if not all(isinstance(item, str) and item for item in (project_id, revision_id, requested_filename)):
                _persist_restart_failure(
                    run=run,
                    message="服务重启时发现图片导出检查点缺少可验证的项目或版本信息，未自动重试。",
                    project_id="",
                    revision_id="",
                    request=None,
                )
                recovered_task_ids.append(task_id)
                continue

            request = MediaImageExportRequest(filename=requested_filename)
            try:
                export = find_media_export_for_task(project_id=project_id, task_id=task_id)
            except MediaWorkspaceError:
                _persist_restart_failure(
                    run=run,
                    message="服务重启时未找到通过回读验证的 PNG 导出文件；为避免重复交付，任务未自动重试。",
                    project_id=project_id,
                    revision_id=revision_id,
                    request=request,
                )
            else:
                _persist_reconciled_completion(
                    run=run,
                    project_id=project_id,
                    revision_id=revision_id,
                    request=request,
                    export=export,
                )
            recovered_task_ids.append(task_id)
    return recovered_task_ids


def _persist_reconciled_completion(
    *,
    run: WorkflowRun,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
    export: MediaImageExportInfo,
) -> None:
    message = "服务重启后已对账到通过文件回读验证的 PNG，并补登记统一交付记录。"
    now = _now()
    steps = [
        step.model_copy(
            update={
                "status": "completed",
                "message": message,
                "output": {
                    **step.output,
                    "message": message,
                    "export": export.model_dump(),
                    "artifact_count": 1,
                    "reconciled_after_service_restart": True,
                },
            }
        )
        if step.step_id == MEDIA_EXPORT_STEP_ID
        else step
        for step in run.steps
    ]
    reconciled_run = run.model_copy(
        update={
            "status": "completed",
            "summary": "服务重启后已对账并恢复已验证的图片 PNG 交付。",
            "steps": steps,
            "metrics": run.metrics.model_copy(
                update={
                    "finished_at": now,
                    "step_total": max(1, run.metrics.step_total),
                    "step_completed": max(1, run.metrics.step_completed),
                    "tool_call_total": max(1, run.metrics.tool_call_total),
                }
            ),
        }
    )
    tool_calls = list_workflow_tool_calls(run.task_id)
    if tool_calls:
        reconciled_tools = [
            call.model_copy(
                update={
                    "status": "completed",
                    "duration_ms": max(0, call.duration_ms),
                    "result": {
                        **call.result,
                        "export_id": export.export_id,
                        "filename": export.filename,
                        "sha256": export.sha256,
                        "width": export.width,
                        "height": export.height,
                        "size_bytes": export.size_bytes,
                        "verification_passed": True,
                        "reconciled_after_service_restart": True,
                    },
                    "error": "",
                    "finished_at": now,
                }
            )
            if call.step_id == MEDIA_EXPORT_STEP_ID
            else call
            for call in tool_calls
        ]
    else:
        reconciled_tools = [
            _tool_call(
                task_id=run.task_id,
                project_id=project_id,
                revision_id=revision_id,
                request=request,
                status="completed",
                export=export,
            )
        ]
    events = list(load_task_log_events(run.task_id) or [])
    events.append(
        _event(
            run.task_id,
            len(events) + 1,
            "task_reconciled_after_restart",
            message,
            step_id=MEDIA_EXPORT_STEP_ID,
            level="warning",
        )
    )
    save_workflow_run(
        run=reconciled_run,
        events=events,
        plan=None,
        artifacts=[_artifact_for_export(task_id=run.task_id, export=export)],
        tool_calls=reconciled_tools,
    )


def _persist_restart_failure(
    *,
    run: WorkflowRun,
    message: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest | None,
) -> None:
    now = _now()
    failed_steps = [
        step.model_copy(
            update={
                "status": "failed",
                "message": message,
                "output": {
                    **step.output,
                    "message": message,
                    "interrupted_by_service_restart": True,
                    "restart_reconciliation": "no_verified_export",
                },
            }
        )
        if step.step_id == MEDIA_EXPORT_STEP_ID
        else step
        for step in run.steps
    ]
    failed_run = run.model_copy(
        update={
            "status": "failed",
            "summary": "服务重启中断图片 PNG 导出，未发现可验证交付物。",
            "steps": failed_steps,
            "metrics": run.metrics.model_copy(
                update={
                    "finished_at": now,
                    "step_total": max(1, run.metrics.step_total),
                    "step_failed": max(1, run.metrics.step_failed),
                    "tool_call_total": max(1, run.metrics.tool_call_total),
                    "tool_call_failed": max(1, run.metrics.tool_call_failed),
                }
            ),
        }
    )
    tool_calls = list_workflow_tool_calls(run.task_id)
    if tool_calls:
        failed_tools = [
            call.model_copy(
                update={
                    "status": "failed",
                    "error": message,
                    "result": {**call.result, "interrupted_by_service_restart": True},
                    "finished_at": now,
                }
            )
            if call.step_id == MEDIA_EXPORT_STEP_ID
            else call
            for call in tool_calls
        ]
    elif request is not None:
        failed_tools = [
            _tool_call(
                task_id=run.task_id,
                project_id=project_id,
                revision_id=revision_id,
                request=request,
                status="failed",
                error=message,
            )
        ]
    else:
        failed_tools = []
    events = list(load_task_log_events(run.task_id) or [])
    events.append(
        _event(
            run.task_id,
            len(events) + 1,
            "task_interrupted_by_restart",
            message,
            step_id=MEDIA_EXPORT_STEP_ID,
            level="warning",
        )
    )
    save_workflow_run(
        run=failed_run,
        events=events,
        plan=None,
        artifacts=[],
        tool_calls=failed_tools,
    )


async def _persist_failed_task(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
    started_at: str,
    duration_ms: int,
    message: str,
    events: list[TaskLogEvent],
    export: MediaImageExportInfo | None,
) -> MediaImageExportTaskResultResponse:
    with _TASK_LOCK:
        if export is not None:
            _discard_export_quietly(project_id=project_id, export_id=export.export_id)
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        failed = _failed_run(
            task_id=task_id,
            project_id=project_id,
            revision_id=revision_id,
            request=request,
            started_at=started_at,
            duration_ms=duration_ms,
            message=message,
        )
        save_workflow_run(
            run=failed,
            events=[*events, _event(task_id, 4, "task_failed", message, step_id=MEDIA_EXPORT_STEP_ID, level="error")],
            plan=None,
            artifacts=[],
            tool_calls=[
                _tool_call(
                    task_id=task_id,
                    project_id=project_id,
                    revision_id=revision_id,
                    request=request,
                    status="failed",
                    duration_ms=duration_ms,
                    error=message,
                )
            ],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EXPORT_STEP_ID,
        level="error",
        message=message,
    )
    return MediaImageExportTaskResultResponse(
        task_id=task_id,
        status="failed",
        summary=failed.summary,
        message=message,
    )


def _save_running_run(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
    started_at: str,
    events: list[TaskLogEvent],
) -> None:
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="正在导出可复核的 PNG 图片。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EXPORT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EXPORT_TOOL_NAME,
                status="running",
                message="正在复制 PNG 并回读验证像素文件。",
                output=_base_output(project_id=project_id, revision_id=revision_id, request=request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, step_total=1, tool_call_total=1),
    )
    save_workflow_run(
        run=run,
        events=events,
        plan=None,
        artifacts=[],
        tool_calls=[
            _tool_call(
                task_id=task_id,
                project_id=project_id,
                revision_id=revision_id,
                request=request,
                status="running",
            )
        ],
    )


def _completed_run(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
    export: MediaImageExportInfo,
    started_at: str,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    output = _base_output(project_id=project_id, revision_id=revision_id, request=request)
    output.update({"message": message, "export": export.model_dump(), "artifact_count": 1})
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary="图片 PNG 已导出并通过文件回读验证。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EXPORT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EXPORT_TOOL_NAME,
                status="completed",
                message=message,
                output=output,
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            step_total=1,
            step_completed=1,
            tool_call_total=1,
        ),
    )


def _failed_run(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
    started_at: str,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    output = _base_output(project_id=project_id, revision_id=revision_id, request=request)
    output["message"] = message
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="图片 PNG 未完成，未保留不完整交付文件。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EXPORT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EXPORT_TOOL_NAME,
                status="failed",
                message=message,
                output=output,
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            step_total=1,
            step_failed=1,
            tool_call_total=1,
            tool_call_failed=1,
        ),
    )


def _artifact_for_export(*, task_id: str, export: MediaImageExportInfo) -> WorkflowArtifact:
    output_path, _ = resolve_media_export_download_path(
        project_id=export.project_id,
        export_id=export.export_id,
    )
    return WorkflowArtifact(
        artifact_id=f"artifact_media_export_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=MEDIA_EXPORT_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        kind="file",
        name=export.filename,
        summary=f"PNG · {export.width}x{export.height} · 已回读验证",
        uri=f"agentflow-output://media_exports/{export.project_id}/{export.export_id}/{export.filename}",
        mime_type="image/png",
        metadata={
            "runtime": True,
            "output_scope": "media_exports",
            "output_path": str(output_path),
            "output_size_bytes": export.size_bytes,
            "project_id": export.project_id,
            "asset_id": export.asset_id,
            "revision_id": export.revision_id,
            "export_id": export.export_id,
            "sha256": export.sha256,
            "width": export.width,
            "height": export.height,
            "verification": {"passed": True, "format": "PNG", "width": export.width, "height": export.height},
            "source_version_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        created_at=export.created_at,
    )


def _tool_call(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
    status: str,
    duration_ms: int = 0,
    export: MediaImageExportInfo | None = None,
    error: str = "",
) -> WorkflowToolCall:
    result: dict[str, Any] = {}
    if export is not None:
        result = {
            "export_id": export.export_id,
            "filename": export.filename,
            "sha256": export.sha256,
            "width": export.width,
            "height": export.height,
            "size_bytes": export.size_bytes,
            "verification_passed": True,
        }
    return WorkflowToolCall(
        call_id=f"call_media_export_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=MEDIA_EXPORT_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_EXPORT_TOOL_NAME,
        status=status,
        risk_level="low",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=duration_ms,
        request={
            "project_id": project_id,
            "revision_id": revision_id,
            "requested_filename": request.filename,
            "write_scope": "output/media_exports",
            "source_version_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        result=result,
        error=error,
        finished_at=_now() if status in {"completed", "failed", "skipped"} else "",
    )


def _base_output(*, project_id: str, revision_id: str, request: MediaImageExportRequest) -> dict[str, object]:
    return {
        "project_id": project_id,
        "revision_id": revision_id,
        "requested_filename": request.filename,
        "write_scope": "output/media_exports",
        "source_version_unchanged": True,
        "model_used": False,
        "network_used": False,
    }


def _limits() -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(
        max_steps=1,
        max_tool_calls=1,
        max_retries_per_tool=0,
        tool_timeout_ms=_TOOL_TIMEOUT_MS,
        task_timeout_ms=_TASK_TIMEOUT_MS,
    )


def _is_media_export_run(run: WorkflowRun | None) -> bool:
    return bool(
        run
        and any(
            step.step_id == MEDIA_EXPORT_STEP_ID and step.action == MEDIA_EXPORT_TOOL_NAME
            for step in run.steps
        )
    )


def _cancelled_run(run: WorkflowRun) -> WorkflowRun:
    now = _now()
    return run.model_copy(
        update={
            "status": "cancelled",
            "summary": "图片 PNG 导出已取消，未保留新的交付文件。",
            "steps": [
                step.model_copy(
                    update={
                        "status": "cancelled",
                        "message": "图片 PNG 导出已被用户取消，未登记新的交付文件。",
                        "output": {**step.output, "cancelled": True, "message": "用户取消了图片 PNG 导出。"},
                    }
                )
                if step.status in {"pending", "running", "waiting_permission"}
                else step
                for step in run.steps
            ],
            "metrics": run.metrics.model_copy(update={"finished_at": now}),
        }
    )


def _is_cancel_requested(task_id: str) -> bool:
    with _TASK_LOCK:
        event = _TASK_CANCEL_EVENTS.get(task_id)
        return bool(event and event.is_set())


def _cancelled_task_result(task_id: str) -> MediaImageExportTaskResultResponse:
    result = get_media_export_task_result(task_id)
    if result is not None:
        return result
    return MediaImageExportTaskResultResponse(
        task_id=task_id,
        status="cancelled",
        summary="图片 PNG 导出已取消。",
        message="已取消图片 PNG 导出，未登记新的交付文件。",
    )


def _forget_cancel_state(task_id: str) -> None:
    with _TASK_LOCK:
        _TASK_CANCEL_EVENTS.pop(task_id, None)


def _load_export(output: dict[str, object]) -> MediaImageExportInfo | None:
    payload = output.get("export")
    if not isinstance(payload, dict):
        return None
    try:
        return MediaImageExportInfo.model_validate(payload)
    except ValueError:
        return None


def _discard_export_quietly(*, project_id: str, export_id: str) -> None:
    try:
        discard_media_image_export(project_id=project_id, export_id=export_id)
    except MediaWorkspaceError:
        # 清理失败不把底层路径或权限信息覆盖到客户可见任务错误；原始失败仍将落到历史。
        pass


def _event(
    task_id: str,
    sequence: int,
    event: str,
    message: str,
    *,
    step_id: str | None = None,
    level: str = "info",
) -> TaskLogEvent:
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event,
        agent_id=MEDIA_AGENT_ID,
        step_id=step_id,
        level=level,
        message=message,
    )


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))


def _now() -> str:
    return datetime.now(UTC).isoformat()
