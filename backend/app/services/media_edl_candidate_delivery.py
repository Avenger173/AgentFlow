"""Runtime delivery for a model-generated, confirmation-only EDL candidate."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import re
from threading import RLock
from time import perf_counter

from app.database.task_repository import list_interrupted_runtime_task_ids, load_task_log_events, load_workflow_run, save_workflow_run
from app.schemas.events import TaskLogEvent
from app.schemas.media_edl import (
    MediaEdlCandidateInfo,
    MediaEdlCandidateRequest,
    MediaEdlCandidateTaskResultResponse,
    MediaEdlModelCandidate,
)
from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import RuntimeExecutionLimits, RuntimeExecutionMetrics, TaskControlResponse, WorkflowRun, WorkflowStepRun, WorkflowToolCall
from app.services.media_edl_planning import (
    MediaEdlPlanningContext,
    MediaEdlPlanningError,
    build_media_edl_candidate,
    generate_media_edl_model_candidate,
    load_media_edl_planning_context,
)
from app.services.model_gateway import ModelGatewayError, ModelRuntime, resolve_model_runtime_for_route
from app.services.task_event_stream import publish_live_task_event


MEDIA_EDL_CANDIDATE_STEP_ID = "media_edl_candidate"
MEDIA_EDL_CANDIDATE_TOOL_NAME = "media.plan_edl_candidate"
MEDIA_AGENT_ID = "media_agent"
_TASK_TIMEOUT_MS = 90_000
_TOOL_TIMEOUT_MS = 75_000
_TASK_ID_PATTERN = re.compile(r"^task_media_edl_plan_[0-9a-f]{12}$")
_TASK_LOCK = RLock()

Planner = Callable[..., Awaitable[MediaEdlModelCandidate]]
ContextLoader = Callable[..., MediaEdlPlanningContext]


def create_media_edl_candidate_queued_run(
    *, task_id: str, project_id: str, request: MediaEdlCandidateRequest
) -> WorkflowRun:
    """Record a candidate-only intent before the planning model receives transcript context."""

    run = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="pending",
        summary="候选剪辑已受理，尚未向模型发送转写上下文。",
        message="正在等待校验已完成的转写交付和受控媒体源。",
        started_at=_now(),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "候选剪辑已受理，尚未调用模型。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_media_edl_candidate_task(
    *,
    task_id: str,
    project_id: str,
    request: MediaEdlCandidateRequest,
    runtime: ModelRuntime | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    planner: Planner = generate_media_edl_model_candidate,
    context_loader: ContextLoader = load_media_edl_planning_context,
) -> MediaEdlCandidateTaskResultResponse:
    """Make one model call, then return a reviewable EDL without rendering it."""

    started_at = _now()
    started_clock = perf_counter()
    with _TASK_LOCK:
        current = load_workflow_run(task_id)
        if _is_cancelled_run(current):
            assert current is not None
            return _result_from_run(current)
        _save_running_run(task_id=task_id, project_id=project_id, request=request, started_at=started_at)

    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
        message="正在校验已完成的转写交付与受控媒体源。",
    )
    try:
        context = await asyncio.to_thread(
            context_loader,
            project_id=project_id,
            request=request,
        )
        active_runtime, active_audit = _resolve_runtime(runtime=runtime, route_audit=route_audit)
    except (MediaEdlPlanningError, ModelGatewayError) as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            message=str(exc),
        )

    with _TASK_LOCK:
        _save_running_run(
            task_id=task_id,
            project_id=project_id,
            request=request,
            started_at=started_at,
            route_audit=active_audit,
            model_requested=True,
        )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
        message="正在让已配置模型从受限转写句段中生成候选片段。",
    )
    try:
        model_candidate = await planner(runtime=active_runtime, context=context)
    except ModelGatewayError as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="provider_outcome_unknown",
            message=f"候选剪辑模型调用未获得可验证结果：{exc}。为避免重复计费，任务不会自动重试。",
            route_audit=active_audit,
            model_requested=True,
        )
    except MediaEdlPlanningError as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="contract_failed",
            message=str(exc),
            route_audit=active_audit,
            model_requested=True,
        )
    except Exception:  # pragma: no cover - provider adapters can fail outside their declared errors.
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="unexpected",
            message="候选剪辑模型调用发生未预期错误，未生成可确认的片段。",
            route_audit=active_audit,
            model_requested=True,
        )

    try:
        candidate, clarification_question = build_media_edl_candidate(
            context=context,
            model_candidate=model_candidate,
        )
    except MediaEdlPlanningError as exc:
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            request=request,
            duration_ms=_duration_ms(started_clock),
            failure_reason="contract_failed",
            message=str(exc),
            route_audit=active_audit,
            model_requested=True,
        )

    if candidate is None:
        summary = "候选剪辑需要补充目标，尚未生成 EDL。"
        message = "模型要求澄清剪辑目标；没有创建文件或调用 FFmpeg。"
    else:
        if candidate.duration_adjusted:
            summary = "候选剪辑已按目标时长收紧，等待用户确认后才可渲染。"
            message = "候选已在转写句段边界按目标时长收紧；没有创建 MP4 或调用 FFmpeg。"
        else:
            summary = "候选剪辑已生成，等待用户确认后才可渲染。"
            message = "候选片段仅来自已验证转写句段；没有创建 MP4 或调用 FFmpeg。"
    completed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="completed",
        summary=summary,
        message=message,
        started_at=started_at,
        duration_ms=_duration_ms(started_clock),
        candidate=candidate,
        clarification_question=clarification_question,
        route_audit=active_audit,
        model_requested=True,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=completed,
            events=[*_running_events(task_id), _event(task_id, 4, "task_completed", message)],
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(completed)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_completed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
        message=message,
    )
    return _result_from_run(completed)


def get_media_edl_candidate_task_result(task_id: str) -> MediaEdlCandidateTaskResultResponse | None:
    run = load_workflow_run(task_id)
    if not _is_media_edl_candidate_run(run):
        return None
    assert run is not None
    return _result_from_run(run)


async def cancel_media_edl_candidate_task(task_id: str) -> TaskControlResponse | None:
    """Only cancel queued work; a started model request is never retried or terminated blindly."""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_media_edl_candidate_run(run):
            return None
        assert run is not None
        if run.status != "pending":
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="候选剪辑已经开始或结束，不能安全取消当前模型请求。",
                workflow_run=run,
            )
        output = _step(run).output
        cancelled = _build_run(
            task_id=task_id,
            project_id=str(output["project_id"]),
            request=_request_from_output(output),
            status="cancelled",
            summary="候选剪辑在模型调用前已取消。",
            message="候选剪辑已取消；没有向模型发送转写，也没有创建文件。",
            started_at=run.metrics.started_at or _now(),
            failure_reason="cancelled",
        )
        events = list(load_task_log_events(task_id) or [])
        save_workflow_run(
            run=cancelled,
            events=[*events, _event(task_id, len(events) + 1, "task_cancelled", "候选剪辑在模型调用前已取消。", level="warning")],
            plan=None,
            artifacts=[],
            tool_calls=[],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
        level="warning",
        message="候选剪辑已取消；没有创建文件或调用 FFmpeg。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="候选剪辑已取消；没有创建文件或注册产物。",
        workflow_run=cancelled,
    )


def recover_interrupted_media_edl_candidate_tasks() -> list[str]:
    """Never replay a model request after restart because its provider outcome is unknown."""

    recovered: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        with _TASK_LOCK:
            run = load_workflow_run(task_id)
            if not _is_media_edl_candidate_run(run):
                continue
            assert run is not None
            output = _step(run).output
            failed = _build_run(
                task_id=run.task_id,
                project_id=str(output.get("project_id", "")),
                request=_request_from_output(output),
                status="failed",
                summary="服务重启中断候选剪辑，未重放模型请求。",
                message="服务重启后无法确认候选剪辑模型请求结果，已停止任务以避免重复计费。",
                started_at=run.metrics.started_at or _now(),
                duration_ms=run.metrics.duration_ms,
                failure_reason="provider_outcome_unknown",
                route_audit=run.model_routes[0] if run.model_routes else None,
                model_requested=bool(_step(run).output.get("model_requested")),
            )
            events = list(load_task_log_events(task_id) or [])
            save_workflow_run(
                run=failed,
                events=[*events, _event(task_id, len(events) + 1, "task_interrupted_by_restart", failed.steps[0].message, level="warning")],
                plan=None,
                artifacts=[],
                tool_calls=[_tool_call(failed)] if failed.metrics.tool_call_total else [],
            )
            recovered.append(task_id)
    return recovered


def _resolve_runtime(
    *, runtime: ModelRuntime | None, route_audit: ModelRouteAuditSnapshot | None
) -> tuple[ModelRuntime, ModelRouteAuditSnapshot | None]:
    if runtime is not None:
        return runtime, route_audit
    resolution = resolve_model_runtime_for_route("media_planning")
    return resolution.runtime, resolution.audit_snapshot(stage=MEDIA_EDL_CANDIDATE_STEP_ID)


async def _persist_failed(
    *,
    task_id: str,
    project_id: str,
    request: MediaEdlCandidateRequest,
    duration_ms: int,
    failure_reason: str,
    message: str,
    route_audit: ModelRouteAuditSnapshot | None = None,
    model_requested: bool = False,
) -> MediaEdlCandidateTaskResultResponse:
    failed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="failed",
        summary="候选剪辑未完成，未生成 EDL 或媒体文件。",
        message=message,
        started_at=_started_at(task_id),
        duration_ms=duration_ms,
        failure_reason=failure_reason,
        route_audit=route_audit,
        model_requested=model_requested,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=failed,
            events=[*_running_events(task_id), _event(task_id, 4, "task_failed", message, level="error")],
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(failed)] if model_requested else [],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
        level="error",
        message=message,
    )
    return _result_from_run(failed)


def _save_running_run(
    *,
    task_id: str,
    project_id: str,
    request: MediaEdlCandidateRequest,
    started_at: str,
    route_audit: ModelRouteAuditSnapshot | None = None,
    model_requested: bool = False,
) -> None:
    running = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="running",
        summary="正在校验转写并生成候选剪辑。",
        message="正在从受控转写句段中生成待确认的候选片段。",
        started_at=started_at,
        route_audit=route_audit,
        model_requested=model_requested,
    )
    save_workflow_run(
        run=running,
        events=_running_events(task_id),
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(running)] if model_requested else [],
    )


def _build_run(
    *,
    task_id: str,
    project_id: str,
    request: MediaEdlCandidateRequest,
    status: str,
    summary: str,
    message: str,
    started_at: str,
    duration_ms: int = 0,
    failure_reason: str | None = None,
    candidate: MediaEdlCandidateInfo | None = None,
    clarification_question: str | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    model_requested: bool = False,
) -> WorkflowRun:
    output: dict[str, object] = {
        "project_id": project_id,
        "transcription_task_id": request.transcription_task_id,
        "parent_candidate_task_id": request.parent_candidate_task_id,
        "goal": request.goal,
        "message": message,
        "failure_reason": failure_reason,
        "model_requested": model_requested,
        "requires_confirmation": True,
    }
    if candidate is not None:
        output["candidate"] = candidate.model_dump(mode="json")
    if clarification_question:
        output["clarification_question"] = clarification_question
    step_status = status if status in {"pending", "running", "completed", "failed", "cancelled"} else "failed"
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level="medium",
        requires_confirmation=candidate is not None,
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EDL_CANDIDATE_TOOL_NAME,
                status=step_status,  # type: ignore[arg-type]
                message=message,
                requires_confirmation=candidate is not None,
                risk_level="medium",
                output=output,
            )
        ],
        model_routes=[route_audit] if route_audit is not None else [],
        limits=RuntimeExecutionLimits(
            max_steps=1,
            max_tool_calls=1,
            max_retries_per_tool=0,
            tool_timeout_ms=_TOOL_TIMEOUT_MS,
            task_timeout_ms=_TASK_TIMEOUT_MS,
            token_budget=720,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now() if status in {"completed", "failed", "cancelled"} else "",
            duration_ms=duration_ms,
            step_total=1,
            step_completed=1 if status == "completed" else 0,
            step_failed=1 if status == "failed" else 0,
            tool_call_total=1 if model_requested else 0,
            tool_call_failed=1 if status == "failed" and model_requested else 0,
            provider_model_request_total=1 if model_requested else 0,
        ),
    )


def _result_from_run(run: WorkflowRun) -> MediaEdlCandidateTaskResultResponse:
    output = _step(run).output
    candidate = None
    if isinstance(output.get("candidate"), dict):
        try:
            candidate = MediaEdlCandidateInfo.model_validate(output["candidate"])
        except ValueError:
            candidate = None
    clarification_question = output.get("clarification_question")
    return MediaEdlCandidateTaskResultResponse(
        task_id=run.task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", _step(run).message)),
        failure_reason=output.get("failure_reason"),
        candidate=candidate,
        clarification_question=clarification_question if isinstance(clarification_question, str) else None,
    )


def _tool_call(run: WorkflowRun) -> WorkflowToolCall:
    output = _step(run).output
    status = "completed" if run.status == "completed" else "failed" if run.status == "failed" else "running"
    result: dict[str, object] = {"requires_confirmation": True}
    if isinstance(output.get("candidate"), dict):
        candidate = output["candidate"]
        selections = candidate.get("selections") if isinstance(candidate, dict) else None
        result["candidate_generated"] = True
        result["clip_count"] = len(selections) if isinstance(selections, list) else 0
    if output.get("clarification_question"):
        result["clarification_requested"] = True
    if output.get("failure_reason"):
        result["failure_reason"] = output["failure_reason"]
    return WorkflowToolCall(
        call_id=f"call_media_edl_plan_{run.task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=run.task_id,
        step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_EDL_CANDIDATE_TOOL_NAME,
        status=status,  # type: ignore[arg-type]
        risk_level="medium",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=run.metrics.duration_ms,
        request={
            "project_id": output.get("project_id"),
            "transcription_task_id": output.get("transcription_task_id"),
            "model_used": True,
            "network_used": True,
        },
        result=result,
        error="" if run.status != "failed" else str(output.get("message", "")),
        finished_at=_now() if run.status in {"completed", "failed"} else "",
    )


def _request_from_output(output: dict[str, object]) -> MediaEdlCandidateRequest:
    return MediaEdlCandidateRequest.model_validate(
        {
            "transcription_task_id": output.get("transcription_task_id"),
            "parent_candidate_task_id": output.get("parent_candidate_task_id"),
            "goal": output.get("goal"),
        }
    )


def _is_media_edl_candidate_run(run: WorkflowRun | None) -> bool:
    return bool(
        run
        and _TASK_ID_PATTERN.fullmatch(run.task_id)
        and any(
            step.step_id == MEDIA_EDL_CANDIDATE_STEP_ID and step.action == MEDIA_EDL_CANDIDATE_TOOL_NAME
            for step in run.steps
        )
    )


def _is_cancelled_run(run: WorkflowRun | None) -> bool:
    return bool(run and _is_media_edl_candidate_run(run) and run.status == "cancelled")


def _step(run: WorkflowRun) -> WorkflowStepRun:
    return next(step for step in run.steps if step.step_id == MEDIA_EDL_CANDIDATE_STEP_ID)


def _running_events(task_id: str) -> list[TaskLogEvent]:
    return [
        _event(task_id, 1, "task_queued", "候选剪辑已受理，尚未调用模型。"),
        _event(task_id, 2, "task_started", "正在校验转写交付与受控媒体源。"),
        _event(task_id, 3, "tool_started", "正在生成仅供确认的候选剪辑片段。"),
    ]


def _event(task_id: str, sequence: int, event: str, message: str, *, level: str = "info") -> TaskLogEvent:
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event,
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_CANDIDATE_STEP_ID,
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
