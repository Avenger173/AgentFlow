"""将一次显式确认的 Qwen Image 编辑纳入受控图片版本与任务历史。

模型只负责生成候选像素；本模块负责受理、版本前置条件、Provider 失败分类、结果下载、
PNG 回读、SQLite revision 提交和重启对账。它不自动重放任何已发送到 Provider 的请求，
避免未知结果导致重复计费或覆盖用户后来创建的版本。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from threading import RLock
from time import perf_counter
from typing import Awaitable, Callable

from app.database.task_repository import (
    list_interrupted_runtime_task_ids,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.events import TaskLogEvent
from app.schemas.media_workspace import (
    MediaImageAiEditRequest,
    MediaImageAiEditTaskResultResponse,
    MediaImageRevisionInfo,
)
from app.schemas.model import ModelRouteAuditSnapshot
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
    create_media_image_ai_revision,
    find_media_image_revision_for_task,
    read_media_image_revision_for_ai_edit,
)
from app.services.model_gateway import (
    ModelGatewayError,
    VisualModelRuntime,
    resolve_visual_model_runtime_for_route,
)
from app.services.qwen_image_edit import (
    QwenDownloadedImage,
    QwenImageEditInput,
    QwenImageEditOutcomeUnknownError,
    QwenImageEditProviderError,
    QwenImageEditRateLimitError,
    QwenImageEditResult,
    download_qwen_image_result,
    edit_qwen_image,
)
from app.services.task_event_stream import publish_live_task_event


MEDIA_AI_EDIT_STEP_ID = "media_ai_image_edit"
MEDIA_AI_EDIT_TOOL_NAME = "media.ai_edit_image"
MEDIA_AGENT_ID = "media_agent"
_TASK_TIMEOUT_MS = 240_000
_TOOL_TIMEOUT_MS = 210_000
_TASK_LOCK = RLock()

ImageEditor = Callable[..., Awaitable[QwenImageEditResult]]
ImageDownloader = Callable[..., Awaitable[QwenDownloadedImage]]


def create_media_ai_edit_queued_run(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageAiEditRequest,
) -> WorkflowRun:
    """在网络请求前登记用户已确认的 AI 修图意图。"""

    run = _build_run(
        task_id=task_id,
        project_id=project_id,
        asset_id=asset_id,
        request=request,
        status="pending",
        summary="AI 修图已受理，等待校验当前版本和模型配置。",
        message="已受理 AI 修图，尚未向模型 Provider 发送图片。",
        started_at=_now(),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "AI 修图已受理，尚未向模型 Provider 发送图片。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_media_ai_edit_task(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageAiEditRequest,
    runtime: VisualModelRuntime | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    image_editor: ImageEditor = edit_qwen_image,
    image_downloader: ImageDownloader = download_qwen_image_result,
) -> MediaImageAiEditTaskResultResponse:
    """执行一次、仅一次模型提交，再把验证后的结果写成新 revision。"""

    started_at = _now()
    started_clock = perf_counter()
    with _TASK_LOCK:
        current = load_workflow_run(task_id)
        if _is_cancelled_run(current):
            assert current is not None
            return _result_from_run(current)
        _save_running_run(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            started_at=started_at,
            route_audit=route_audit,
            runtime=runtime,
        )

    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_AI_EDIT_STEP_ID,
        message="正在校验当前图片版本和 AI 修图配置。",
    )
    try:
        source_revision, source_bytes = await asyncio.to_thread(
            read_media_image_revision_for_ai_edit,
            project_id=project_id,
            asset_id=asset_id,
            base_revision_id=request.base_revision_id,
        )
        _validate_source_size(source_revision)
        active_runtime, active_audit = _resolve_runtime(runtime=runtime, route_audit=route_audit)
    except MediaWorkspaceConflictError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            failure_reason="workspace_conflict",
            conflict=True,
        )
    except (MediaWorkspaceError, ModelGatewayError) as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
        )

    with _TASK_LOCK:
        _save_running_run(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            started_at=started_at,
            route_audit=active_audit,
            runtime=active_runtime,
        )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_AI_EDIT_STEP_ID,
        message="正在向已配置的图像模型提交一张当前版本图片。",
    )
    try:
        provider_result = await image_editor(
            images=[QwenImageEditInput(image_bytes=source_bytes, mime_type="image/png")],
            prompt=request.instruction,
            output_count=1,
            output_size=f"{source_revision.width}*{source_revision.height}",
            runtime=active_runtime,
        )
    except QwenImageEditRateLimitError as exc:
        retry_note = ""
        if exc.retry_after_seconds is not None:
            retry_note = f" Provider 建议至少等待 {exc.retry_after_seconds:g} 秒后由用户重新发起。"
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=f"{exc}{retry_note}",
            duration_ms=_duration_ms(started_clock),
            failure_reason="provider_rate_limited",
            retry_after_seconds=exc.retry_after_seconds,
            route_audit=active_audit,
            runtime=active_runtime,
        )
    except QwenImageEditOutcomeUnknownError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=f"{exc} 为避免重复计费或重复编辑，任务不会自动重试。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="provider_outcome_unknown",
            route_audit=active_audit,
            runtime=active_runtime,
        )
    except QwenImageEditProviderError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            failure_reason="provider_rejected",
            route_audit=active_audit,
            runtime=active_runtime,
        )
    except ModelGatewayError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            route_audit=active_audit,
            runtime=active_runtime,
        )
    except Exception:  # pragma: no cover - 适配器替换或 Provider 解析异常也必须留下终态。
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message="AI 修图模型调用发生未预期错误，未创建新的图片版本。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="unexpected",
            route_audit=active_audit,
            runtime=active_runtime,
        )

    if not provider_result.output_urls:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message="图像模型没有返回可下载的结果地址，未创建新的图片版本。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            route_audit=active_audit,
            runtime=active_runtime,
        )
    try:
        downloaded = await image_downloader(result_url=provider_result.output_urls[0])
    except ModelGatewayError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=f"模型已返回结果地址，但本地无法安全下载并验证图片：{exc}。未创建新版本，也不会自动重放请求。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="result_download_failed",
            route_audit=active_audit,
            runtime=active_runtime,
            provider_result=provider_result,
        )
    except Exception:  # pragma: no cover - 下载替身或底层库异常同样不能留下 running 状态。
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message="模型已返回结果地址，但本地下载处理发生未预期错误；未创建新版本，也不会自动重放请求。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="result_download_failed",
            route_audit=active_audit,
            runtime=active_runtime,
            provider_result=provider_result,
        )

    try:
        revision = await asyncio.to_thread(
            create_media_image_ai_revision,
            project_id=project_id,
            asset_id=asset_id,
            base_revision_id=request.base_revision_id,
            image_bytes=downloaded.image_bytes,
            parameters=_revision_parameters(request=request, result=provider_result),
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
            failure_reason="workspace_conflict",
            conflict=True,
            route_audit=active_audit,
            runtime=active_runtime,
            provider_result=provider_result,
        )
    except MediaWorkspaceError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            route_audit=active_audit,
            runtime=active_runtime,
            provider_result=provider_result,
        )
    except Exception:  # pragma: no cover - 兜底仍必须显式记录未提交成功。
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
            message="AI 修图结果提交发生未预期错误，未确认新的图片版本。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="unexpected",
            route_audit=active_audit,
            runtime=active_runtime,
            provider_result=provider_result,
        )

    duration_ms = _duration_ms(started_clock)
    message = "AI 修图结果已下载、回读验证并登记为新的 PNG 修订版本。"
    completed = _build_run(
        task_id=task_id,
        project_id=project_id,
        asset_id=asset_id,
        request=request,
        status="completed",
        summary="AI 修图已完成并通过 PNG 文件回读验证。",
        message=message,
        started_at=started_at,
        duration_ms=duration_ms,
        revision=revision,
        route_audit=active_audit,
        runtime=active_runtime,
        provider_result=provider_result,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=completed,
            events=_events_for_terminal(task_id=task_id, message=message, event="task_completed"),
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(completed)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_completed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_AI_EDIT_STEP_ID,
        message=message,
    )
    return _result_from_run(completed)


def get_media_ai_edit_task_result(task_id: str) -> MediaImageAiEditTaskResultResponse | None:
    run = load_workflow_run(task_id)
    if not _is_media_ai_edit_run(run):
        return None
    assert run is not None
    return _result_from_run(run)


async def cancel_media_ai_edit_task(task_id: str) -> TaskControlResponse | None:
    """仅在网络提交之前接受取消，避免模型侧不可中断调用造成伪取消。"""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_media_ai_edit_run(run):
            return None
        assert run is not None
        if run.status != "pending":
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="AI 修图已经开始或结束；模型提交后不能安全中途取消。",
                workflow_run=run,
            )
        cancelled = _build_run(
            task_id=task_id,
            project_id=str(_step(run).output.get("project_id", "")),
            asset_id=str(_step(run).output.get("asset_id", "")),
            request=_request_from_output(_step(run).output),
            status="cancelled",
            summary="AI 修图已取消，未向模型 Provider 发送图片。",
            message="AI 修图已在执行前取消，未创建新的修订版本。",
            started_at=run.metrics.started_at or _now(),
            failure_reason="cancelled",
        )
        existing_events = list(load_task_log_events(task_id) or [])
        save_workflow_run(
            run=cancelled,
            events=[
                *existing_events,
                _event(task_id, len(existing_events) + 1, "task_cancelled", "AI 修图已取消，未发送图片。", level="warning"),
            ],
            plan=None,
            artifacts=[],
            tool_calls=[],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_AI_EDIT_STEP_ID,
        level="warning",
        message="AI 修图已取消，未向模型 Provider 发送图片。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="AI 修图已取消；源图片和既有版本未被修改。",
        workflow_run=cancelled,
    )


def recover_interrupted_media_ai_edit_tasks() -> list[str]:
    """启动时仅对账已经原子提交的 revision，绝不重放外部模型请求。"""

    recovered: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        with _TASK_LOCK:
            run = load_workflow_run(task_id)
            if not _is_media_ai_edit_run(run):
                continue
            assert run is not None
            output = _step(run).output
            project_id = output.get("project_id")
            asset_id = output.get("asset_id")
            if not isinstance(project_id, str) or not isinstance(asset_id, str):
                _persist_restart_failure(run, "服务重启时 AI 修图检查点不完整，未自动重试模型请求。")
                recovered.append(task_id)
                continue
            try:
                revision = find_media_image_revision_for_task(project_id=project_id, task_id=task_id)
            except MediaWorkspaceError:
                _persist_restart_failure(
                    run,
                    "服务重启时未找到通过回读验证的 AI 修图版本；模型请求不会自动重放。",
                )
            else:
                _persist_reconciled_completion(run, revision)
            recovered.append(task_id)
    return recovered


def _resolve_runtime(
    *, runtime: VisualModelRuntime | None, route_audit: ModelRouteAuditSnapshot | None
) -> tuple[VisualModelRuntime, ModelRouteAuditSnapshot | None]:
    if runtime is not None:
        return runtime, route_audit
    resolution = resolve_visual_model_runtime_for_route("media_image_edit", validate=True)
    if not isinstance(resolution.runtime, VisualModelRuntime):  # pragma: no cover - 路由实现的防御边界。
        raise ModelGatewayError("AI 修图路由未解析到图像模型运行时。")
    return resolution.runtime, resolution.audit_snapshot(stage=MEDIA_AI_EDIT_STEP_ID)


def _validate_source_size(revision: MediaImageRevisionInfo) -> None:
    if not 512 <= revision.width <= 2048 or not 512 <= revision.height <= 2048:
        raise MediaWorkspaceError("AI 修图首版仅支持宽高均在 512 到 2048 像素之间的当前版本，请先用本地缩放生成兼容版本。")


async def _persist_failed_task(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageAiEditRequest,
    message: str,
    duration_ms: int,
    failure_reason: str,
    conflict: bool = False,
    retry_after_seconds: float | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    runtime: VisualModelRuntime | None = None,
    provider_result: QwenImageEditResult | None = None,
) -> MediaImageAiEditTaskResultResponse:
    failed = _build_run(
        task_id=task_id,
        project_id=project_id,
        asset_id=asset_id,
        request=request,
        status="failed",
        summary="AI 修图未完成，未登记新的图片修订版本。",
        message=message,
        started_at=_started_at(task_id),
        duration_ms=duration_ms,
        conflict=conflict,
        failure_reason=failure_reason,
        retry_after_seconds=retry_after_seconds,
        route_audit=route_audit,
        runtime=runtime,
        provider_result=provider_result,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=failed,
            events=_events_for_terminal(task_id=task_id, message=message, event="task_failed", level="error"),
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(failed)] if runtime is not None or provider_result is not None else [],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_AI_EDIT_STEP_ID,
        level="error",
        message=message,
    )
    return _result_from_run(failed)


def _save_running_run(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageAiEditRequest,
    started_at: str,
    route_audit: ModelRouteAuditSnapshot | None,
    runtime: VisualModelRuntime | None,
) -> None:
    running = _build_run(
        task_id=task_id,
        project_id=project_id,
        asset_id=asset_id,
        request=request,
        status="running",
        summary="正在执行一次 AI 修图并验证结果版本。",
        message="正在校验图片并等待图像模型返回结果。",
        started_at=started_at,
        route_audit=route_audit,
        runtime=runtime,
    )
    save_workflow_run(
        run=running,
        events=_running_events(task_id),
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(running)] if runtime is not None else [],
    )


def _build_run(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageAiEditRequest,
    status: str,
    summary: str,
    message: str,
    started_at: str,
    duration_ms: int = 0,
    conflict: bool = False,
    failure_reason: str | None = None,
    retry_after_seconds: float | None = None,
    revision: MediaImageRevisionInfo | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    runtime: VisualModelRuntime | None = None,
    provider_result: QwenImageEditResult | None = None,
) -> WorkflowRun:
    output = _base_output(project_id=project_id, asset_id=asset_id, request=request)
    output.update(
        {
            "message": message,
            "conflict": conflict,
            "failure_reason": failure_reason,
            "retry_after_seconds": retry_after_seconds,
            "verification_passed": bool(revision),
        }
    )
    if runtime is not None:
        output.update({"provider": runtime.provider, "model": runtime.model})
    if provider_result is not None:
        output.update(_provider_result_output(provider_result))
    if revision is not None:
        output["revision"] = revision.model_dump()
    step_status = status if status in {"pending", "running", "completed", "failed", "cancelled"} else "failed"
    metrics = RuntimeExecutionMetrics(
        started_at=started_at,
        finished_at=_now() if status in {"completed", "failed", "cancelled"} else "",
        duration_ms=duration_ms,
        step_total=1,
        step_completed=1 if status == "completed" else 0,
        step_failed=1 if status == "failed" else 0,
        tool_call_total=1 if status in {"running", "completed", "failed"} and runtime is not None else 0,
        tool_call_failed=1 if status == "failed" and runtime is not None else 0,
        provider_model_request_total=1 if provider_result is not None else 0,
        provider_usage_reported_request_total=1 if provider_result is not None and provider_result.usage_reported else 0,
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level="medium",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_AI_EDIT_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_AI_EDIT_TOOL_NAME,
                status=step_status,  # type: ignore[arg-type]
                message=message,
                risk_level="medium",
                output=output,
            )
        ],
        model_routes=[route_audit] if route_audit is not None else [],
        limits=_limits(),
        metrics=metrics,
    )


def _persist_reconciled_completion(run: WorkflowRun, revision: MediaImageRevisionInfo) -> None:
    output = _step(run).output
    message = "服务重启后已对账到通过回读验证的 AI 修图版本，并补齐任务终态。"
    completed = _build_run(
        task_id=run.task_id,
        project_id=str(output.get("project_id", "")),
        asset_id=str(output.get("asset_id", "")),
        request=_request_from_output(output),
        status="completed",
        summary="服务重启后已对账并恢复已验证的 AI 修图版本。",
        message=message,
        started_at=run.metrics.started_at or _now(),
        duration_ms=run.metrics.duration_ms,
        revision=revision,
        route_audit=run.model_routes[0] if run.model_routes else None,
        runtime=None,
        provider_result=None,
    )
    # 运行时 Provider/模型已经是脱敏历史事实，恢复时不能重新解析当前配置覆盖它。
    completed = completed.model_copy(
        update={
            "model_routes": list(run.model_routes),
            "steps": [
                completed.steps[0].model_copy(
                    update={
                        "output": {
                            **completed.steps[0].output,
                            "provider": output.get("provider", ""),
                            "model": output.get("model", ""),
                            "provider_usage": output.get("provider_usage"),
                            "reconciled_after_service_restart": True,
                        }
                    }
                )
            ],
        }
    )
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=completed,
        events=[
            *events,
            _event(run.task_id, len(events) + 1, "task_reconciled_after_restart", message, level="warning"),
        ],
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(completed)],
    )


def _persist_restart_failure(run: WorkflowRun, message: str) -> None:
    output = _step(run).output
    failed = _build_run(
        task_id=run.task_id,
        project_id=str(output.get("project_id", "")),
        asset_id=str(output.get("asset_id", "")),
        request=_request_from_output(output),
        status="failed",
        summary="服务重启中断 AI 修图，未发现可验证的新版本。",
        message=message,
        started_at=run.metrics.started_at or _now(),
        duration_ms=run.metrics.duration_ms,
        failure_reason="provider_outcome_unknown",
        route_audit=run.model_routes[0] if run.model_routes else None,
    ).model_copy(update={"model_routes": list(run.model_routes)})
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=failed,
        events=[
            *events,
            _event(run.task_id, len(events) + 1, "task_interrupted_by_restart", message, level="warning"),
        ],
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(failed)] if run.model_routes else [],
    )


def _result_from_run(run: WorkflowRun) -> MediaImageAiEditTaskResultResponse:
    output = _step(run).output
    revision = None
    if run.status == "completed" and isinstance(output.get("revision"), dict):
        try:
            revision = MediaImageRevisionInfo.model_validate(output["revision"])
        except ValueError:
            revision = None
    retry_after = output.get("retry_after_seconds")
    return MediaImageAiEditTaskResultResponse(
        task_id=run.task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", _step(run).message)),
        conflict=bool(output.get("conflict", False)),
        failure_reason=output.get("failure_reason"),
        retry_after_seconds=float(retry_after) if isinstance(retry_after, (int, float)) else None,
        revision=revision,
    )


def _tool_call(run: WorkflowRun) -> WorkflowToolCall:
    step = _step(run)
    output = step.output
    status = "completed" if run.status == "completed" else "failed" if run.status == "failed" else "running"
    result: dict[str, object] = {"verification_passed": bool(output.get("verification_passed", False))}
    if isinstance(output.get("revision"), dict):
        revision = output["revision"]
        result.update(
            {
                "revision_id": revision.get("revision_id"),
                "parent_revision_id": revision.get("parent_revision_id"),
                "sha256": revision.get("sha256"),
                "width": revision.get("width"),
                "height": revision.get("height"),
                "size_bytes": revision.get("size_bytes"),
            }
        )
    if output.get("failure_reason"):
        result["failure_reason"] = output["failure_reason"]
    if output.get("provider_usage") is not None:
        result["provider_usage"] = output["provider_usage"]
    return WorkflowToolCall(
        call_id=f"call_media_ai_edit_{run.task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=run.task_id,
        step_id=MEDIA_AI_EDIT_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_AI_EDIT_TOOL_NAME,
        status=status,  # type: ignore[arg-type]
        risk_level="medium",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=run.metrics.duration_ms,
        request={
            "project_id": output.get("project_id"),
            "asset_id": output.get("asset_id"),
            "base_revision_id": output.get("base_revision_id"),
            "instruction": output.get("instruction"),
            "output_size": output.get("output_size"),
            "provider": output.get("provider", ""),
            "model": output.get("model", ""),
            "model_used": True,
            "network_used": True,
        },
        result=result,
        error="" if run.status != "failed" else str(output.get("message", "")),
        finished_at=_now() if run.status in {"completed", "failed"} else "",
    )


def _base_output(*, project_id: str, asset_id: str, request: MediaImageAiEditRequest) -> dict[str, object]:
    return {
        "project_id": project_id,
        "asset_id": asset_id,
        "base_revision_id": request.base_revision_id,
        "instruction": request.instruction,
        "model_used": True,
        "network_used": True,
    }


def _provider_result_output(result: QwenImageEditResult) -> dict[str, object]:
    return {
        "provider": result.provider,
        "model": result.model,
        "provider_request_id": result.request_id,
        "provider_usage": {
            "usage_reported": result.usage_reported,
            "input_image_count": result.input_image_count,
            "output_image_count": result.output_image_count,
            "input_image_type": result.input_image_type,
            "output_image_type": result.output_image_type,
            "width": result.width,
            "height": result.height,
        },
    }


def _revision_parameters(*, request: MediaImageAiEditRequest, result: QwenImageEditResult) -> dict[str, object]:
    output = _provider_result_output(result)
    return {
        "instruction": request.instruction,
        "provider": result.provider,
        "model": result.model,
        "request_id": result.request_id,
        "usage": output["provider_usage"],
    }


def _request_from_output(output: dict[str, object]) -> MediaImageAiEditRequest:
    return MediaImageAiEditRequest.model_validate(
        {"base_revision_id": output.get("base_revision_id"), "instruction": output.get("instruction")}
    )


def _is_media_ai_edit_run(run: WorkflowRun | None) -> bool:
    return bool(
        run
        and any(
            step.step_id == MEDIA_AI_EDIT_STEP_ID and step.action == MEDIA_AI_EDIT_TOOL_NAME
            for step in run.steps
        )
    )


def _is_cancelled_run(run: WorkflowRun | None) -> bool:
    return bool(run and _is_media_ai_edit_run(run) and run.status == "cancelled")


def _step(run: WorkflowRun) -> WorkflowStepRun:
    return next(step for step in run.steps if step.step_id == MEDIA_AI_EDIT_STEP_ID)


def _running_events(task_id: str) -> list[TaskLogEvent]:
    return [
        _event(task_id, 1, "task_queued", "AI 修图已受理，尚未向模型 Provider 发送图片。"),
        _event(task_id, 2, "task_started", "正在校验当前图片版本和 AI 修图配置。"),
        _event(task_id, 3, "tool_started", "正在向已配置的图像模型提交一张当前版本图片。"),
    ]


def _events_for_terminal(*, task_id: str, message: str, event: str, level: str = "info") -> list[TaskLogEvent]:
    return [
        *_running_events(task_id),
        _event(task_id, 4, event, message, level=level),
    ]


def _limits() -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(
        max_steps=1,
        max_tool_calls=1,
        max_retries_per_tool=0,
        tool_timeout_ms=_TOOL_TIMEOUT_MS,
        task_timeout_ms=_TASK_TIMEOUT_MS,
    )


def _event(task_id: str, sequence: int, event: str, message: str, *, level: str = "info") -> TaskLogEvent:
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event,
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_AI_EDIT_STEP_ID,
        level=level,  # type: ignore[arg-type]
        message=message,
    )


def _started_at(task_id: str) -> str:
    run = load_workflow_run(task_id)
    return run.metrics.started_at if run is not None and run.metrics.started_at else _now()


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))


def _now() -> str:
    return datetime.now(UTC).isoformat()
