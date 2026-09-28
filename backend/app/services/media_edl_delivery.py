"""Runtime delivery for a constrained, deterministic MP4 EDL render."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import re
from threading import RLock
from time import perf_counter

from app.core.config import settings
from app.database.task_repository import (
    list_interrupted_runtime_task_ids,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.events import TaskLogEvent
from app.schemas.media_edl import MediaEditDecisionList, MediaEdlRenderInfo, MediaEdlRenderTaskResultResponse
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    TaskControlResponse,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.media_source_preparation import (
    MediaSourcePreparationError,
    MediaToolExecutionError,
    render_media_edl,
    verify_media_edl_render,
)
from app.services.task_event_stream import publish_live_task_event


MEDIA_EDL_STEP_ID = "media_edl_render"
MEDIA_EDL_TOOL_NAME = "media.render_edl"
MEDIA_AGENT_ID = "media_agent"
_TASK_TIMEOUT_MS = 210_000
_TOOL_TIMEOUT_MS = 190_000
_TASK_ID_PATTERN = re.compile(r"^task_media_edl_[0-9a-f]{12}$")
_TASK_LOCK = RLock()

Renderer = Callable[..., MediaEdlRenderInfo]
Verifier = Callable[..., MediaEdlRenderInfo]


def create_media_edl_queued_run(
    *, task_id: str, project_id: str, edl: MediaEditDecisionList
) -> WorkflowRun:
    """Persist the intent before background FFmpeg work begins."""

    run = _build_run(
        task_id=task_id,
        project_id=project_id,
        edl=edl,
        status="pending",
        summary="EDL MP4 render accepted; no output file has been created yet.",
        message="The EDL render is queued and will use only the controlled imported source.",
        started_at=_now(),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "EDL render accepted; FFmpeg has not started.")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_media_edl_task(
    *,
    task_id: str,
    project_id: str,
    edl: MediaEditDecisionList,
    renderer: Renderer = render_media_edl,
) -> MediaEdlRenderTaskResultResponse:
    """Run once, verify the rendered file, then create one Runtime artifact."""

    started_at = _now()
    started_clock = perf_counter()
    with _TASK_LOCK:
        current = load_workflow_run(task_id)
        if _is_cancelled_run(current):
            assert current is not None
            return _result_from_run(current)
        _save_running_run(task_id=task_id, project_id=project_id, edl=edl, started_at=started_at)

    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_STEP_ID,
        message="Validating controlled source and constrained EDL ranges.",
    )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_STEP_ID,
        message="Rendering the selected source ranges with fixed FFmpeg parameters.",
    )
    output_path = _render_path(task_id)
    try:
        render = await asyncio.to_thread(
            renderer,
            edl=edl,
            expected_project_scope=project_id,
            output_path=output_path,
        )
        _validate_render_result(render=render, edl=edl, output_path=output_path)
    except MediaToolExecutionError as exc:
        _remove_render_quietly(output_path)
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            edl=edl,
            duration_ms=_duration_ms(started_clock),
            failure_reason="tool_execution_failed",
            message=str(exc),
        )
    except MediaSourcePreparationError as exc:
        _remove_render_quietly(output_path)
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            edl=edl,
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            message=str(exc),
        )
    except (OSError, ValueError) as exc:
        _remove_render_quietly(output_path)
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            edl=edl,
            duration_ms=_duration_ms(started_clock),
            failure_reason="delivery_verification_failed",
            message=f"The rendered MP4 could not be verified: {exc}",
        )
    except Exception:
        _remove_render_quietly(output_path)
        return await _persist_failed(
            task_id=task_id,
            project_id=project_id,
            edl=edl,
            duration_ms=_duration_ms(started_clock),
            failure_reason="unexpected",
            message="The EDL render ended unexpectedly; no verified MP4 was retained.",
        )

    duration_ms = _duration_ms(started_clock)
    artifact = _artifact_for_render(task_id=task_id, project_id=project_id, render=render, output_path=output_path)
    message = "The MP4 was rendered, probed again, hash-checked, and registered as an artifact."
    completed = _build_run(
        task_id=task_id,
        project_id=project_id,
        edl=edl,
        status="completed",
        summary="Constrained EDL MP4 render completed and passed read-back verification.",
        message=message,
        started_at=started_at,
        duration_ms=duration_ms,
        render=render,
        artifact=artifact,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=completed,
            events=[*_running_events(task_id), _event(task_id, 4, "artifact_saved", "Verified MP4 artifact saved."), _event(task_id, 5, "task_completed", message)],
            plan=None,
            artifacts=[artifact],
            tool_calls=[_tool_call(completed)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="artifact_saved",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_STEP_ID,
        message="The rendered MP4 passed read-back verification and was registered.",
    )
    await publish_live_task_event(
        task_id=task_id,
        event="task_completed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_STEP_ID,
        message=message,
    )
    return _result_from_run(completed)


def get_media_edl_task_result(task_id: str) -> MediaEdlRenderTaskResultResponse | None:
    run = load_workflow_run(task_id)
    if not _is_media_edl_run(run):
        return None
    assert run is not None
    return _result_from_run(run)


async def cancel_media_edl_task(task_id: str) -> TaskControlResponse | None:
    """Only a queued render can be cancelled; an active FFmpeg process is not killed."""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_media_edl_run(run):
            return None
        assert run is not None
        if run.status != "pending":
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="The EDL render has started or finished and cannot be safely cancelled mid-render.",
                workflow_run=run,
            )
        output = _step(run).output
        edl = _edl_from_output(output)
        cancelled = _build_run(
            task_id=task_id,
            project_id=str(output["project_id"]),
            edl=edl,
            status="cancelled",
            summary="EDL render cancelled before FFmpeg started.",
            message="The queued EDL render was cancelled; no MP4 was created.",
            started_at=run.metrics.started_at or _now(),
            failure_reason="cancelled",
        )
        events = list(load_task_log_events(task_id) or [])
        save_workflow_run(
            run=cancelled,
            events=[*events, _event(task_id, len(events) + 1, "task_cancelled", "Queued EDL render cancelled.", level="warning")],
            plan=None,
            artifacts=[],
            tool_calls=[],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_STEP_ID,
        level="warning",
        message="Queued EDL render cancelled; the controlled source was not modified.",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="The queued EDL render was cancelled; no output was registered.",
        workflow_run=cancelled,
    )


def recover_interrupted_media_edl_tasks(*, verifier: Verifier = verify_media_edl_render) -> list[str]:
    """Reconcile only an already-rendered file; never rerun FFmpeg after a restart."""

    recovered: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        with _TASK_LOCK:
            run = load_workflow_run(task_id)
            if not _is_media_edl_run(run):
                continue
            assert run is not None
            output = _step(run).output
            try:
                project_id = str(output["project_id"])
                edl = _edl_from_output(output)
                output_path = _render_path(task_id)
                render = verifier(edl=edl, expected_project_scope=project_id, output_path=output_path)
                _validate_render_result(render=render, edl=edl, output_path=output_path)
            except (KeyError, OSError, ValueError, MediaSourcePreparationError):
                _persist_restart_failure(run)
            else:
                _persist_reconciled_completion(run=run, project_id=project_id, edl=edl, render=render, output_path=output_path)
            recovered.append(task_id)
    return recovered


def resolve_media_edl_download_path(*, project_id: str, task_id: str) -> tuple[Path, str]:
    run = load_workflow_run(task_id)
    if not _is_media_edl_run(run) or run is None or run.status != "completed":
        raise MediaSourcePreparationError("The requested verified EDL render was not found.")
    output = _step(run).output
    if output.get("project_id") != project_id:
        raise MediaSourcePreparationError("The requested EDL render is outside this project scope.")
    path = _render_path(task_id)
    render = _render_from_output(output)
    if render is None:
        raise MediaSourcePreparationError("The EDL render record is incomplete.")
    _validate_render_result(render=render, edl=_edl_from_output(output), output_path=path)
    return path, "edited.mp4"


def _persist_reconciled_completion(
    *, run: WorkflowRun, project_id: str, edl: MediaEditDecisionList, render: MediaEdlRenderInfo, output_path: Path
) -> None:
    message = "After restart, the existing MP4 passed read-back verification; FFmpeg was not run again."
    artifact = _artifact_for_render(task_id=run.task_id, project_id=project_id, render=render, output_path=output_path)
    completed = _build_run(
        task_id=run.task_id,
        project_id=project_id,
        edl=edl,
        status="completed",
        summary="Existing EDL MP4 reconciled after restart and passed read-back verification.",
        message=message,
        started_at=run.metrics.started_at or _now(),
        duration_ms=run.metrics.duration_ms,
        render=render,
        artifact=artifact,
    )
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=completed,
        events=[*events, _event(run.task_id, len(events) + 1, "task_reconciled_after_restart", message, level="warning")],
        plan=None,
        artifacts=[artifact],
        tool_calls=[_tool_call(completed)],
    )


def _persist_restart_failure(run: WorkflowRun) -> None:
    output = _step(run).output
    project_id = str(output.get("project_id", ""))
    edl = _edl_from_output(output)
    message = "Service restart interrupted the EDL render and no verified MP4 was found; FFmpeg was not rerun."
    failed = _build_run(
        task_id=run.task_id,
        project_id=project_id,
        edl=edl,
        status="failed",
        summary="EDL render was interrupted by restart without a verified delivery file.",
        message=message,
        started_at=run.metrics.started_at or _now(),
        duration_ms=run.metrics.duration_ms,
        failure_reason="delivery_verification_failed",
    )
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=failed,
        events=[*events, _event(run.task_id, len(events) + 1, "task_interrupted_by_restart", message, level="warning")],
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(failed)],
    )


async def _persist_failed(
    *,
    task_id: str,
    project_id: str,
    edl: MediaEditDecisionList,
    duration_ms: int,
    failure_reason: str,
    message: str,
) -> MediaEdlRenderTaskResultResponse:
    failed = _build_run(
        task_id=task_id,
        project_id=project_id,
        edl=edl,
        status="failed",
        summary="EDL render failed; no unverified MP4 was registered.",
        message=message,
        started_at=_started_at(task_id),
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=failed,
            events=[*_running_events(task_id), _event(task_id, 4, "task_failed", message, level="error")],
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(failed)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_EDL_STEP_ID,
        level="error",
        message=message,
    )
    return _result_from_run(failed)


def _save_running_run(*, task_id: str, project_id: str, edl: MediaEditDecisionList, started_at: str) -> None:
    running = _build_run(
        task_id=task_id,
        project_id=project_id,
        edl=edl,
        status="running",
        summary="Rendering constrained EDL ranges into a new MP4.",
        message="Validating source ranges and rendering with fixed FFmpeg parameters.",
        started_at=started_at,
    )
    save_workflow_run(
        run=running,
        events=_running_events(task_id),
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(running)],
    )


def _build_run(
    *,
    task_id: str,
    project_id: str,
    edl: MediaEditDecisionList,
    status: str,
    summary: str,
    message: str,
    started_at: str,
    duration_ms: int = 0,
    failure_reason: str | None = None,
    render: MediaEdlRenderInfo | None = None,
    artifact: WorkflowArtifact | None = None,
) -> WorkflowRun:
    output = _base_output(project_id=project_id, edl=edl)
    output.update({"message": message, "failure_reason": failure_reason, "verification_passed": artifact is not None})
    if render is not None:
        output["render"] = render.model_dump(mode="json")
    if artifact is not None:
        output["artifact_id"] = artifact.artifact_id
    step_status = status if status in {"pending", "running", "completed", "failed", "cancelled"} else "failed"
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level="medium",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_EDL_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_EDL_TOOL_NAME,
                status=step_status,  # type: ignore[arg-type]
                message=message,
                risk_level="medium",
                output=output,
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now() if status in {"completed", "failed", "cancelled"} else "",
            duration_ms=duration_ms,
            step_total=1,
            step_completed=1 if status == "completed" else 0,
            step_failed=1 if status == "failed" else 0,
            tool_call_total=1 if status in {"running", "completed", "failed"} else 0,
            tool_call_failed=1 if status == "failed" else 0,
        ),
    )


def _artifact_for_render(
    *, task_id: str, project_id: str, render: MediaEdlRenderInfo, output_path: Path
) -> WorkflowArtifact:
    suffix = task_id.rsplit("_", maxsplit=1)[-1]
    return WorkflowArtifact(
        artifact_id=f"artifact_media_edl_{suffix}",
        task_id=task_id,
        step_id=MEDIA_EDL_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        kind="file",
        name="edited.mp4",
        summary=f"MP4 | {render.width}x{render.height} | {render.rendered_duration_ms} ms | verified",
        uri=f"agentflow-output://media_edl/{project_id}/{task_id}/edited.mp4",
        mime_type="video/mp4",
        metadata={
            "runtime": True,
            "output_scope": "media_edl",
            "output_path": str(output_path),
            "output_size_bytes": render.size_bytes,
            "project_id": project_id,
            "source_id": render.source_id,
            "source_sha256": render.source_sha256,
            "sha256": render.sha256,
            "clip_count": render.clip_count,
            "requested_duration_ms": render.requested_duration_ms,
            "rendered_duration_ms": render.rendered_duration_ms,
            "verification": {"passed": True, "format": "MP4", "video_codec": render.video_codec, "audio_codec": render.audio_codec},
            "model_used": False,
            "network_used": False,
        },
        created_at=render.created_at,
    )


def _tool_call(run: WorkflowRun) -> WorkflowToolCall:
    step = _step(run)
    output = step.output
    render = _render_from_output(output)
    status = "completed" if run.status == "completed" else "failed" if run.status == "failed" else "running"
    result: dict[str, object] = {"verification_passed": bool(output.get("verification_passed", False))}
    if render is not None:
        result.update({"sha256": render.sha256, "rendered_duration_ms": render.rendered_duration_ms, "clip_count": render.clip_count})
    if output.get("failure_reason"):
        result["failure_reason"] = output["failure_reason"]
    return WorkflowToolCall(
        call_id=f"call_media_edl_{run.task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=run.task_id,
        step_id=MEDIA_EDL_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_EDL_TOOL_NAME,
        status=status,
        risk_level="medium",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=run.metrics.duration_ms,
        request={
            "project_id": output.get("project_id"),
            "source_id": output.get("source_id"),
            "clip_count": output.get("clip_count"),
            "requested_duration_ms": output.get("requested_duration_ms"),
            "write_scope": "output/media_edl",
            "model_used": False,
            "network_used": False,
        },
        result=result,
        error="" if run.status != "failed" else str(output.get("message", "")),
        finished_at=_now() if run.status in {"completed", "failed"} else "",
    )


def _base_output(*, project_id: str, edl: MediaEditDecisionList) -> dict[str, object]:
    return {
        "project_id": project_id,
        "source_id": edl.source_id,
        "edl": edl.model_dump(mode="json"),
        "clip_count": len(edl.clips),
        "requested_duration_ms": edl.requested_duration_ms,
        "write_scope": "output/media_edl",
        "model_used": False,
        "network_used": False,
    }


def _result_from_run(run: WorkflowRun) -> MediaEdlRenderTaskResultResponse:
    output = _step(run).output
    return MediaEdlRenderTaskResultResponse(
        task_id=run.task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", _step(run).message)),
        failure_reason=output.get("failure_reason"),
        render=_render_from_output(output) if run.status == "completed" else None,
    )


def _render_from_output(output: dict[str, object]) -> MediaEdlRenderInfo | None:
    raw = output.get("render")
    if not isinstance(raw, dict):
        return None
    try:
        return MediaEdlRenderInfo.model_validate(raw)
    except ValueError:
        return None


def _edl_from_output(output: dict[str, object]) -> MediaEditDecisionList:
    raw = output.get("edl")
    if not isinstance(raw, dict):
        raise ValueError("The persisted EDL payload is missing.")
    return MediaEditDecisionList.model_validate(raw)


def _validate_render_result(*, render: MediaEdlRenderInfo, edl: MediaEditDecisionList, output_path: Path) -> None:
    if render.source_id != edl.source_id or render.clip_count != len(edl.clips):
        raise ValueError("The rendered MP4 does not match its EDL source or clip count.")
    if render.requested_duration_ms != edl.requested_duration_ms:
        raise ValueError("The rendered MP4 does not match the requested EDL duration.")
    if not output_path.is_file() or output_path.stat().st_size != render.size_bytes:
        raise ValueError("The rendered MP4 file is missing or its size changed.")
    if _sha256_file(output_path) != render.sha256:
        raise ValueError("The rendered MP4 hash does not match the verified result.")


def _render_path(task_id: str) -> Path:
    if _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError("Invalid EDL render task identifier.")
    root = settings.media_edl_output_dir
    path = (root / f"{task_id}.mp4").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:  # pragma: no cover - fixed task-id naming is the primary guard
        raise ValueError("Invalid EDL render output path.") from exc
    return path


def _remove_render_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_media_edl_run(run: WorkflowRun | None) -> bool:
    return bool(run and any(step.step_id == MEDIA_EDL_STEP_ID and step.action == MEDIA_EDL_TOOL_NAME for step in run.steps))


def _is_cancelled_run(run: WorkflowRun | None) -> bool:
    return bool(run and _is_media_edl_run(run) and run.status == "cancelled")


def _step(run: WorkflowRun) -> WorkflowStepRun:
    return next(step for step in run.steps if step.step_id == MEDIA_EDL_STEP_ID)


def _running_events(task_id: str) -> list[TaskLogEvent]:
    return [
        _event(task_id, 1, "task_queued", "EDL render accepted; FFmpeg has not started."),
        _event(task_id, 2, "task_started", "Validating controlled source and constrained EDL ranges."),
        _event(task_id, 3, "tool_started", "Rendering with fixed FFmpeg parameters and preparing MP4 read-back verification."),
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
        step_id=MEDIA_EDL_STEP_ID,
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
