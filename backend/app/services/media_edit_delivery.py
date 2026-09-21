"""确定性图片编辑的统一 Runtime 审计与恢复层。

图片修订由 ``media_workspace`` 原子保存、哈希并回读；本模块只把一次编辑命令接入既有
WorkflowRun、事件和工具调用历史。这样调色、裁剪和蒙版不会绕过任务可见性，也能在服务
重启恰好发生在“文件已提交、任务未落终态”时按 task_id 对账，而不是盲目重跑。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from threading import RLock
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
    MediaImageEditTaskResultResponse,
    MediaImageOperationRequest,
    MediaImageRevisionInfo,
)
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    TaskControlResponse,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.media_workspace import (
    MediaWorkspaceConflictError,
    MediaWorkspaceError,
    create_media_image_revision,
    find_media_image_revision_for_task,
)
from app.services.task_event_stream import publish_live_task_event


MEDIA_EDIT_STEP_ID = "media_image_edit"
MEDIA_EDIT_TOOL_NAME = "media.edit_image"
MEDIA_AGENT_ID = "media_agent"
_TASK_TIMEOUT_MS = 60_000
_TOOL_TIMEOUT_MS = 45_000
_TASK_LOCK = RLock()


def create_media_edit_queued_run(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
) -> WorkflowRun:
    """登记编辑意图，任务历史在 PNG 写入前即可追踪。"""

    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="图片编辑已受理，等待校验当前版本。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EDIT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EDIT_TOOL_NAME,
                status="pending",
                message="已受理图片编辑，尚未创建新的修订版本。",
                output=_base_output(project_id=project_id, asset_id=asset_id, request=request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=_now(), step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "图片编辑已受理，等待校验当前版本。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_media_edit_task(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
) -> MediaImageEditTaskResultResponse:
    """创建并回读一份不可变修订；编辑开始后不接受取消以避免半提交语义。"""

    with _TASK_LOCK:
        current = load_workflow_run(task_id)
        if _is_cancelled_run(current):
            return _result_from_run(current)
        _save_running_run(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            started_at=_now(),
        )

    started_clock = perf_counter()
    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDIT_STEP_ID,
        message="正在校验当前图片版本和编辑参数。",
    )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDIT_STEP_ID,
        message="正在生成并回读新的 PNG 修订版本。",
    )
    try:
        revision = await asyncio.to_thread(
            create_media_image_revision,
            project_id=project_id,
            asset_id=asset_id,
            base_revision_id=request.base_revision_id,
            operation=request.operation,
            parameters=request.operation_parameters(),
            layer_source_asset_id=request.layer_source_asset_id(),
            task_id=task_id,
        )
    except MediaWorkspaceConflictError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            conflict=True,
        )
    except MediaWorkspaceError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
        )
    except Exception:  # pragma: no cover - 兜底仍必须写出可见终态。
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message="图片编辑发生未预期错误，未确认新的修订版本。",
            duration_ms=_duration_ms(started_clock),
        )

    duration_ms = _duration_ms(started_clock)
    message = "图片编辑已生成新的 PNG 修订版本，并通过文件回读验证。"
    with _TASK_LOCK:
        completed = _completed_run(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            revision=revision,
            duration_ms=duration_ms,
            message=message,
        )
        save_workflow_run(
            run=completed,
            events=[
                _event(task_id, 1, "task_queued", "图片编辑已受理，等待校验当前版本。"),
                _event(task_id, 2, "task_started", "正在校验当前图片版本和编辑参数。", step_id=MEDIA_EDIT_STEP_ID),
                _event(task_id, 3, "tool_started", "正在生成并回读新的 PNG 修订版本。", step_id=MEDIA_EDIT_STEP_ID),
                _event(task_id, 4, "task_completed", "图片编辑完成，已登记新的可回读修订版本。", step_id=MEDIA_EDIT_STEP_ID),
            ],
            plan=None,
            artifacts=[],
            tool_calls=[
                _tool_call(
                    task_id=task_id,
                    project_id=project_id,
                    asset_id=asset_id,
                    request=request,
                    status="completed",
                    duration_ms=duration_ms,
                    revision=revision,
                )
            ],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_completed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDIT_STEP_ID,
        message="图片编辑完成，已登记新的可回读修订版本。",
    )
    return MediaImageEditTaskResultResponse(
        task_id=task_id,
        status="completed",
        summary=completed.summary,
        message=message,
        revision=revision,
    )


def get_media_edit_task_result(task_id: str) -> MediaImageEditTaskResultResponse | None:
    run = load_workflow_run(task_id)
    if not _is_media_edit_run(run):
        return None
    assert run is not None
    return _result_from_run(run)


async def cancel_media_edit_task(task_id: str) -> TaskControlResponse | None:
    """只允许在工具开始前取消编辑，避免取消结果和已写入修订互相矛盾。"""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_media_edit_run(run):
            return None
        assert run is not None
        if run.status != "pending":
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="图片编辑已经开始或结束；为保持修订历史一致，不能中途取消。",
                workflow_run=run,
            )
        cancelled = _cancelled_run(run)
        events = list(load_task_log_events(task_id) or [])
        save_workflow_run(
            run=cancelled,
            events=[
                *events,
                _event(
                    task_id,
                    len(events) + 1,
                    "task_cancelled",
                    "图片编辑已取消，未创建新的修订版本。",
                    step_id=MEDIA_EDIT_STEP_ID,
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
        step_id=MEDIA_EDIT_STEP_ID,
        level="warning",
        message="图片编辑已取消，未创建新的修订版本。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="图片编辑已取消，源图和既有修订未被修改。",
        workflow_run=cancelled,
    )


def recover_interrupted_media_edit_tasks() -> list[str]:
    """按已写入修订的 task_id 对账中断编辑，绝不自动重放旧命令。"""

    recovered: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        with _TASK_LOCK:
            run = load_workflow_run(task_id)
            if not _is_media_edit_run(run):
                continue
            assert run is not None
            step = _edit_step(run)
            output = step.output
            project_id = output.get("project_id")
            asset_id = output.get("asset_id")
            if not isinstance(project_id, str) or not isinstance(asset_id, str):
                _persist_restart_failure(run, "服务重启时图片编辑检查点不完整，未自动重试。")
                recovered.append(task_id)
                continue
            try:
                revision = find_media_image_revision_for_task(project_id=project_id, task_id=task_id)
            except MediaWorkspaceError:
                _persist_restart_failure(
                    run,
                    "服务重启时未找到通过回读验证的图片修订；为避免重复编辑，任务未自动重试。",
                )
            else:
                _persist_reconciled_completion(run, revision)
            recovered.append(task_id)
    return recovered


def _save_running_run(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
    started_at: str,
) -> None:
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="正在生成可回读的图片修订版本。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EDIT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EDIT_TOOL_NAME,
                status="running",
                message="正在生成并回读新的 PNG 修订版本。",
                output=_base_output(project_id=project_id, asset_id=asset_id, request=request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, step_total=1, tool_call_total=1),
    )
    save_workflow_run(
        run=run,
        events=[
            _event(task_id, 1, "task_queued", "图片编辑已受理，等待校验当前版本。"),
            _event(task_id, 2, "task_started", "正在校验当前图片版本和编辑参数。", step_id=MEDIA_EDIT_STEP_ID),
            _event(task_id, 3, "tool_started", "正在生成并回读新的 PNG 修订版本。", step_id=MEDIA_EDIT_STEP_ID),
        ],
        plan=None,
        artifacts=[],
        tool_calls=[
            _tool_call(
                task_id=task_id,
                project_id=project_id,
                asset_id=asset_id,
                request=request,
                status="running",
            )
        ],
    )


async def _persist_failed_task(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
    message: str,
    duration_ms: int,
    conflict: bool = False,
) -> MediaImageEditTaskResultResponse:
    with _TASK_LOCK:
        failed = _failed_run(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=message,
            duration_ms=duration_ms,
            conflict=conflict,
        )
        save_workflow_run(
            run=failed,
            events=[
                _event(task_id, 1, "task_queued", "图片编辑已受理，等待校验当前版本。"),
                _event(task_id, 2, "task_started", "正在校验当前图片版本和编辑参数。", step_id=MEDIA_EDIT_STEP_ID),
                _event(task_id, 3, "tool_started", "正在生成并回读新的 PNG 修订版本。", step_id=MEDIA_EDIT_STEP_ID),
                _event(task_id, 4, "task_failed", message, step_id=MEDIA_EDIT_STEP_ID, level="error"),
            ],
            plan=None,
            artifacts=[],
            tool_calls=[
                _tool_call(
                    task_id=task_id,
                    project_id=project_id,
                    asset_id=asset_id,
                    request=request,
                    status="failed",
                    duration_ms=duration_ms,
                    error=message,
                    conflict=conflict,
                )
            ],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDIT_STEP_ID,
        level="error",
        message=message,
    )
    return MediaImageEditTaskResultResponse(
        task_id=task_id,
        status="failed",
        summary=failed.summary,
        message=message,
        conflict=conflict,
    )


def _completed_run(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
    revision: MediaImageRevisionInfo,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    output = _base_output(project_id=project_id, asset_id=asset_id, request=request)
    output.update({"message": message, "revision": revision.model_dump(), "verification_passed": True})
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary="图片编辑已完成并通过 PNG 文件回读验证。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EDIT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EDIT_TOOL_NAME,
                status="completed",
                message=message,
                output=output,
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(
            started_at=_started_at(task_id),
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
    asset_id: str,
    request: MediaImageOperationRequest,
    message: str,
    duration_ms: int,
    conflict: bool,
) -> WorkflowRun:
    output = _base_output(project_id=project_id, asset_id=asset_id, request=request)
    output.update({"message": message, "conflict": conflict})
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="图片编辑未完成，未登记新的修订版本。",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EDIT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EDIT_TOOL_NAME,
                status="failed",
                message=message,
                output=output,
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(
            started_at=_started_at(task_id),
            finished_at=_now(),
            duration_ms=duration_ms,
            step_total=1,
            step_failed=1,
            tool_call_total=1,
            tool_call_failed=1,
        ),
    )


def _persist_reconciled_completion(run: WorkflowRun, revision: MediaImageRevisionInfo) -> None:
    message = "服务重启后已对账到通过文件回读验证的图片修订，并补齐任务终态。"
    steps = [
        step.model_copy(
            update={
                "status": "completed",
                "message": message,
                "output": {
                    **step.output,
                    "message": message,
                    "revision": revision.model_dump(),
                    "verification_passed": True,
                    "reconciled_after_service_restart": True,
                },
            }
        )
        if step.step_id == MEDIA_EDIT_STEP_ID
        else step
        for step in run.steps
    ]
    completed = run.model_copy(
        update={
            "status": "completed",
            "summary": "服务重启后已对账并恢复已验证的图片修订。",
            "steps": steps,
            "metrics": run.metrics.model_copy(
                update={
                    "finished_at": _now(),
                    "step_total": max(1, run.metrics.step_total),
                    "step_completed": max(1, run.metrics.step_completed),
                    "tool_call_total": max(1, run.metrics.tool_call_total),
                }
            ),
        }
    )
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=completed,
        events=[
            *events,
            _event(
                run.task_id,
                len(events) + 1,
                "task_reconciled_after_restart",
                message,
                step_id=MEDIA_EDIT_STEP_ID,
                level="warning",
            ),
        ],
        plan=None,
        artifacts=[],
        tool_calls=[
            _tool_call(
                task_id=run.task_id,
                project_id=str(_edit_step(run).output.get("project_id", "")),
                asset_id=str(_edit_step(run).output.get("asset_id", "")),
                request=_request_from_output(_edit_step(run).output),
                status="completed",
                revision=revision,
            )
        ],
    )


def _persist_restart_failure(run: WorkflowRun, message: str) -> None:
    failed_steps = [
        step.model_copy(
            update={
                "status": "failed",
                "message": message,
                "output": {
                    **step.output,
                    "message": message,
                    "interrupted_by_service_restart": True,
                    "restart_reconciliation": "no_verified_revision",
                },
            }
        )
        if step.step_id == MEDIA_EDIT_STEP_ID
        else step
        for step in run.steps
    ]
    failed = run.model_copy(
        update={
            "status": "failed",
            "summary": "服务重启中断图片编辑，未发现可验证的新修订版本。",
            "steps": failed_steps,
            "metrics": run.metrics.model_copy(
                update={
                    "finished_at": _now(),
                    "step_total": max(1, run.metrics.step_total),
                    "step_failed": max(1, run.metrics.step_failed),
                    "tool_call_total": max(1, run.metrics.tool_call_total),
                    "tool_call_failed": max(1, run.metrics.tool_call_failed),
                }
            ),
        }
    )
    events = list(load_task_log_events(run.task_id) or [])
    output = _edit_step(run).output
    save_workflow_run(
        run=failed,
        events=[
            *events,
            _event(
                run.task_id,
                len(events) + 1,
                "task_interrupted_by_restart",
                message,
                step_id=MEDIA_EDIT_STEP_ID,
                level="warning",
            ),
        ],
        plan=None,
        artifacts=[],
        tool_calls=[
            _tool_call(
                task_id=run.task_id,
                project_id=str(output.get("project_id", "")),
                asset_id=str(output.get("asset_id", "")),
                request=_request_from_output(output),
                status="failed",
                error=message,
            )
        ],
    )


def _cancelled_run(run: WorkflowRun) -> WorkflowRun:
    now = _now()
    return run.model_copy(
        update={
            "status": "cancelled",
            "summary": "图片编辑已取消，未创建新的修订版本。",
            "steps": [
                step.model_copy(
                    update={
                        "status": "cancelled",
                        "message": "图片编辑已在执行前取消，未创建新的修订版本。",
                        "output": {**step.output, "cancelled": True, "message": "用户取消了图片编辑。"},
                    }
                )
                if step.status == "pending"
                else step
                for step in run.steps
            ],
            "metrics": run.metrics.model_copy(update={"finished_at": now}),
        }
    )


def _result_from_run(run: WorkflowRun) -> MediaImageEditTaskResultResponse:
    step = _edit_step(run)
    output = step.output
    revision = None
    if run.status == "completed":
        payload = output.get("revision")
        if isinstance(payload, dict):
            try:
                revision = MediaImageRevisionInfo.model_validate(payload)
            except ValueError:
                revision = None
    return MediaImageEditTaskResultResponse(
        task_id=run.task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", step.message)),
        conflict=bool(output.get("conflict", False)),
        revision=revision,
    )


def _tool_call(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
    status: str,
    duration_ms: int = 0,
    revision: MediaImageRevisionInfo | None = None,
    error: str = "",
    conflict: bool = False,
) -> WorkflowToolCall:
    result: dict[str, Any] = {"verification_passed": bool(revision)}
    if revision is not None:
        result.update(
            {
                "revision_id": revision.revision_id,
                "parent_revision_id": revision.parent_revision_id,
                "sha256": revision.sha256,
                "width": revision.width,
                "height": revision.height,
                "size_bytes": revision.size_bytes,
            }
        )
    if conflict:
        result["conflict"] = True
    return WorkflowToolCall(
        call_id=f"call_media_edit_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=MEDIA_EDIT_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_EDIT_TOOL_NAME,
        status=status,
        risk_level="low",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=duration_ms,
        request={
            "project_id": project_id,
            "asset_id": asset_id,
            "base_revision_id": request.base_revision_id,
            "operation": request.operation,
            "parameters": request.operation_parameters(),
            "layer_source_asset_id": request.layer_source_asset_id(),
            "model_used": False,
            "network_used": False,
        },
        result=result,
        error=error,
        finished_at=_now() if status in {"completed", "failed", "skipped"} else "",
    )


def _base_output(*, project_id: str, asset_id: str, request: MediaImageOperationRequest) -> dict[str, object]:
    return {
        "project_id": project_id,
        "asset_id": asset_id,
        "base_revision_id": request.base_revision_id,
        "operation": request.operation,
        "parameters": request.operation_parameters(),
        "layer_source_asset_id": request.layer_source_asset_id(),
        "model_used": False,
        "network_used": False,
    }


def _request_from_output(output: dict[str, object]) -> MediaImageOperationRequest:
    payload: dict[str, object] = {
        "operation": output.get("operation"),
        "base_revision_id": output.get("base_revision_id"),
    }
    parameters = output.get("parameters")
    if isinstance(parameters, dict):
        operation = output.get("operation")
        if operation == "adjust_color":
            payload.update(parameters)
        elif operation == "crop":
            payload.update({f"crop_{key}": value for key, value in parameters.items()})
        elif operation == "resize":
            payload.update({f"resize_{key}": value for key, value in parameters.items()})
        elif operation == "apply_rect_mask":
            payload.update({f"mask_{key}": value for key, value in parameters.items()})
        elif operation == "composite_raster_layer":
            payload.update({f"layer_{key}": value for key, value in parameters.items()})
        elif operation == "recompose_raster_layers":
            payload["layer_stack"] = parameters.get("layer_stack")
    layer_source_asset_id = output.get("layer_source_asset_id")
    if isinstance(layer_source_asset_id, str):
        payload["overlay_asset_id"] = layer_source_asset_id
    return MediaImageOperationRequest.model_validate(payload)


def _is_media_edit_run(run: WorkflowRun | None) -> bool:
    return bool(
        run
        and any(step.step_id == MEDIA_EDIT_STEP_ID and step.action == MEDIA_EDIT_TOOL_NAME for step in run.steps)
    )


def _is_cancelled_run(run: WorkflowRun | None) -> bool:
    return bool(run and _is_media_edit_run(run) and run.status == "cancelled")


def _edit_step(run: WorkflowRun) -> WorkflowStepRun:
    return next(step for step in run.steps if step.step_id == MEDIA_EDIT_STEP_ID)


def _started_at(task_id: str) -> str:
    run = load_workflow_run(task_id)
    return run.metrics.started_at if run is not None and run.metrics.started_at else _now()


def _limits() -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(
        max_steps=1,
        max_tool_calls=1,
        max_retries_per_tool=0,
        tool_timeout_ms=_TOOL_TIMEOUT_MS,
        task_timeout_ms=_TASK_TIMEOUT_MS,
    )


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
