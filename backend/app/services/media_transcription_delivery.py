"""将受控 WAV 的一次模型转写接入任务、Artifact 与重启对账。

模型只接收由 ``media_source_preparation`` 回读过的内存 WAV；本模块负责记录用户确认、
Provider 失败语义、JSON 交付回读与恢复。它不读取客户端路径、不自动重放未知模型请求，
也不提前生成字幕、翻译或剪辑时间线。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
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
from app.schemas.media_source import (
    MediaTranscriptInfo,
    MediaTranscriptionAudioInfo,
    MediaTranscriptionArtifactPayload,
    MediaTranscriptionRequest,
    MediaTranscriptionSegmentInfo,
    MediaTranscriptionTaskResultResponse,
    MediaTranscriptionWordInfo,
)
from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    TaskControlResponse,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.media_source_preparation import MediaSourcePreparationError, read_transcription_audio_bytes
from app.services.model_gateway import AudioModelRuntime, ModelGatewayError, resolve_audio_model_runtime_for_route
from app.services.qwen_audio_transcription import (
    QwenAudioTranscriptionInput,
    QwenAudioTranscriptionOutcomeUnknownError,
    QwenAudioTranscriptionProviderError,
    QwenAudioTranscriptionResult,
    transcribe_qwen_audio,
)
from app.services.task_event_stream import publish_live_task_event


MEDIA_TRANSCRIPTION_STEP_ID = "media_transcription"
MEDIA_TRANSCRIPTION_TOOL_NAME = "media.transcribe_audio"
MEDIA_AGENT_ID = "media_agent"
_TASK_TIMEOUT_MS = 240_000
_TOOL_TIMEOUT_MS = 210_000
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_TASK_LOCK = RLock()

Transcriber = Callable[..., Awaitable[QwenAudioTranscriptionResult]]


def create_media_transcription_queued_run(
    *,
    task_id: str,
    project_id: str,
    request: MediaTranscriptionRequest,
) -> WorkflowRun:
    """先持久化客户已经提交的转写意图，后台任务随后才能调用 Provider。"""

    run = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="pending",
        summary="媒体转写已受理，尚未向模型 Provider 发送音频。",
        message="已受理媒体转写，等待校验受控音轨与模型配置。",
        started_at=_now(),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "媒体转写已受理，尚未向模型 Provider 发送音频。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_media_transcription_task(
    *,
    task_id: str,
    project_id: str,
    request: MediaTranscriptionRequest,
    runtime: AudioModelRuntime | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    transcriber: Transcriber = transcribe_qwen_audio,
) -> MediaTranscriptionTaskResultResponse:
    """仅提交一次已验证 WAV，并把回读成功的 JSON 作为唯一交付物。"""

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
            request=request,
            started_at=started_at,
            route_audit=route_audit,
            runtime=runtime,
        )

    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
        message="正在校验受控音轨和语音转写模型配置。",
    )
    try:
        audio_info, audio_bytes = await asyncio.to_thread(
            read_transcription_audio_bytes,
            source_id=request.source_id,
            audio_id=request.audio_id,
            expected_project_scope=project_id,
        )
        active_runtime, active_audit = _resolve_runtime(runtime=runtime, route_audit=route_audit)
    except (MediaSourcePreparationError, ModelGatewayError) as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
        )

    with _TASK_LOCK:
        _save_running_run(
            task_id=task_id,
            project_id=project_id,
            request=request,
            started_at=started_at,
            route_audit=active_audit,
            runtime=active_runtime,
        )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
        message="正在向已配置的语音模型提交一段受控 WAV。",
    )
    try:
        provider_result = await transcriber(
            audio=QwenAudioTranscriptionInput(audio_bytes=audio_bytes, audio_format="wav"),
            language_hints=tuple(request.language_hints),
            speaker_diarization=request.speaker_diarization,
            runtime=active_runtime,
        )
    except QwenAudioTranscriptionOutcomeUnknownError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            request=request,
            message=f"{exc} 为避免重复计费，任务不会自动重试。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="provider_outcome_unknown",
            route_audit=active_audit,
            runtime=active_runtime,
        )
    except QwenAudioTranscriptionProviderError as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
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
            request=request,
            message=str(exc),
            duration_ms=_duration_ms(started_clock),
            failure_reason="validation_failed",
            route_audit=active_audit,
            runtime=active_runtime,
        )
    except Exception:  # pragma: no cover - 外部适配器异常必须收束成可解释终态。
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            request=request,
            message="语音转写模型调用发生未预期错误，未生成可验证的转写交付。",
            duration_ms=_duration_ms(started_clock),
            failure_reason="unexpected",
            route_audit=active_audit,
            runtime=active_runtime,
        )

    try:
        transcript = _transcript_from_provider(provider_result)
        artifact, payload = await asyncio.to_thread(
            _write_verified_transcript_artifact,
            task_id=task_id,
            project_id=project_id,
            request=request,
            audio=audio_info,
            transcript=transcript,
            result=provider_result,
        )
    except (OSError, ValueError, MediaSourcePreparationError) as exc:
        return await _persist_failed_task(
            task_id=task_id,
            project_id=project_id,
            request=request,
            message=f"转写结果无法写入并回读验证：{exc}",
            duration_ms=_duration_ms(started_clock),
            failure_reason="delivery_verification_failed",
            route_audit=active_audit,
            runtime=active_runtime,
            provider_result=provider_result,
        )

    duration_ms = _duration_ms(started_clock)
    message = "语音转写已完成，结构化时间戳 JSON 已回读验证并登记为交付物。"
    completed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="completed",
        summary="媒体转写已完成并通过 JSON 文件回读验证。",
        message=message,
        started_at=started_at,
        duration_ms=duration_ms,
        transcript=transcript,
        artifact=artifact,
        route_audit=active_audit,
        runtime=active_runtime,
        provider_result=provider_result,
    )
    with _TASK_LOCK:
        save_workflow_run(
            run=completed,
            events=_events_for_terminal(task_id=task_id, message=message, event="task_completed"),
            plan=None,
            artifacts=[artifact],
            tool_calls=[_tool_call(completed)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_completed",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
        message=message,
    )
    return _result_from_run(completed)


def get_media_transcription_task_result(task_id: str) -> MediaTranscriptionTaskResultResponse | None:
    run = load_workflow_run(task_id)
    if not _is_media_transcription_run(run):
        return None
    assert run is not None
    return _result_from_run(run)


async def cancel_media_transcription_task(task_id: str) -> TaskControlResponse | None:
    """仅在模型提交前允许取消，不能把不可中断的 Provider 调用伪装为已取消。"""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_media_transcription_run(run):
            return None
        assert run is not None
        if run.status != "pending":
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="媒体转写已经开始或结束；模型提交后不能安全中途取消。",
                workflow_run=run,
            )
        output = _step(run).output
        cancelled = _build_run(
            task_id=task_id,
            project_id=str(output.get("project_id", "")),
            request=_request_from_output(output),
            status="cancelled",
            summary="媒体转写已取消，未向模型 Provider 发送音频。",
            message="媒体转写已在执行前取消，未生成转写交付物。",
            started_at=run.metrics.started_at or _now(),
            failure_reason="cancelled",
        )
        events = list(load_task_log_events(task_id) or [])
        save_workflow_run(
            run=cancelled,
            events=[*events, _event(task_id, len(events) + 1, "task_cancelled", "媒体转写已取消，未发送音频。", level="warning")],
            plan=None,
            artifacts=[],
            tool_calls=[],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=MEDIA_AGENT_ID,
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
        level="warning",
        message="媒体转写已取消，未向模型 Provider 发送音频。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="媒体转写已取消；受控源文件和派生 WAV 未被修改。",
        workflow_run=cancelled,
    )


def recover_interrupted_media_transcription_tasks() -> list[str]:
    """只对账已经写入并回读的 JSON，绝不重放不确定的音频模型请求。"""

    recovered: list[str] = []
    for task_id in list_interrupted_runtime_task_ids():
        with _TASK_LOCK:
            run = load_workflow_run(task_id)
            if not _is_media_transcription_run(run):
                continue
            assert run is not None
            try:
                payload, artifact = _load_verified_transcript_artifact(task_id=task_id, output=_step(run).output)
            except (OSError, ValueError, MediaSourcePreparationError):
                _persist_restart_failure(
                    run,
                    "服务重启时未找到通过回读验证的转写 JSON；为避免重复发送音频，任务未自动重试。",
                )
            else:
                _persist_reconciled_completion(run=run, payload=payload, artifact=artifact)
            recovered.append(task_id)
    return recovered


def _resolve_runtime(
    *, runtime: AudioModelRuntime | None, route_audit: ModelRouteAuditSnapshot | None
) -> tuple[AudioModelRuntime, ModelRouteAuditSnapshot | None]:
    if runtime is not None:
        return runtime, route_audit
    resolution = resolve_audio_model_runtime_for_route("media_transcription", validate=True)
    if not isinstance(resolution.runtime, AudioModelRuntime):  # pragma: no cover - 防御 Route 接线错误。
        raise ModelGatewayError("语音转写路由未解析到音频模型运行时。")
    return resolution.runtime, resolution.audit_snapshot(stage=MEDIA_TRANSCRIPTION_STEP_ID)


def _transcript_from_provider(result: QwenAudioTranscriptionResult) -> MediaTranscriptInfo:
    return MediaTranscriptInfo(
        text=result.text,
        segments=[
            MediaTranscriptionSegmentInfo(
                sentence_id=item.sentence_id,
                text=item.text,
                begin_ms=item.begin_ms,
                end_ms=item.end_ms,
                speaker_id=item.speaker_id,
                words=[
                    MediaTranscriptionWordInfo(
                        text=word.text,
                        begin_ms=word.begin_ms,
                        end_ms=word.end_ms,
                        punctuation=word.punctuation,
                    )
                    for word in item.words
                ],
            )
            for item in result.segments
        ],
    )


def _write_verified_transcript_artifact(
    *,
    task_id: str,
    project_id: str,
    request: MediaTranscriptionRequest,
    audio: MediaTranscriptionAudioInfo,
    transcript: MediaTranscriptInfo,
    result: QwenAudioTranscriptionResult,
) -> tuple[WorkflowArtifact, MediaTranscriptionArtifactPayload]:
    payload = MediaTranscriptionArtifactPayload(
        task_id=task_id,
        project_id=project_id,
        request=request,
        audio=audio,
        transcript=transcript,
        provider=result.provider,
        model=result.model,
        provider_request_id_sha256=_hash_text(result.request_id),
        provider_usage=_provider_usage(result),
        created_at=_now(),
    )
    path = _artifact_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise ValueError("转写 JSON 超过当前受控交付上限。")
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        restored_bytes = path.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    if restored_bytes != encoded:
        raise ValueError("转写 JSON 回读内容不一致。")
    restored = MediaTranscriptionArtifactPayload.model_validate_json(restored_bytes)
    if restored != payload:
        raise ValueError("转写 JSON 回读结构不一致。")
    return _artifact_from_payload(payload=restored, path=path, size_bytes=len(restored_bytes), sha256_value=sha256(restored_bytes).hexdigest()), restored


def _load_verified_transcript_artifact(
    *, task_id: str, output: dict[str, object]
) -> tuple[MediaTranscriptionArtifactPayload, WorkflowArtifact]:
    path = _artifact_path(task_id)
    if not path.is_file() or path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("受控转写 JSON 不存在或超过上限。")
    raw = path.read_bytes()
    payload = MediaTranscriptionArtifactPayload.model_validate_json(raw)
    request = _request_from_output(output)
    if (
        payload.task_id != task_id
        or payload.project_id != str(output.get("project_id", ""))
        or payload.request != request
        or payload.audio.source_id != request.source_id
        or payload.audio.audio_id != request.audio_id
    ):
        raise ValueError("受控转写 JSON 与任务检查点不匹配。")
    # 重启对账前再次确认源和派生 WAV 没有被篡改或跨项目替换。
    verified_audio, _ = read_transcription_audio_bytes(
        source_id=request.source_id,
        audio_id=request.audio_id,
        expected_project_scope=payload.project_id,
    )
    if verified_audio != payload.audio:
        raise ValueError("受控转写 JSON 的派生 WAV 元数据已失效。")
    return payload, _artifact_from_payload(
        payload=payload,
        path=path,
        size_bytes=len(raw),
        sha256_value=sha256(raw).hexdigest(),
    )


def _artifact_path(task_id: str) -> Path:
    if re.fullmatch(r"task_media_transcription_[0-9a-f]{12}", task_id) is None:
        raise ValueError("媒体转写任务标识无效。")
    return (settings.data_dir / "outputs" / "media_transcripts" / f"{task_id}.json").resolve()


def _artifact_from_payload(
    *,
    payload: MediaTranscriptionArtifactPayload,
    path: Path,
    size_bytes: int,
    sha256_value: str,
) -> WorkflowArtifact:
    task_suffix = payload.task_id.rsplit("_", maxsplit=1)[-1]
    return WorkflowArtifact(
        artifact_id=f"artifact_media_transcription_{task_suffix}",
        task_id=payload.task_id,
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        kind="report",
        name="transcript.json",
        summary=f"带句级时间戳的转写 JSON · {len(payload.transcript.segments)} 个句段 · 已回读验证",
        uri=f"agentflow-output://runtime/media_transcripts/{payload.task_id}.json",
        mime_type="application/json",
        metadata={
            "runtime": True,
            "output_scope": "runtime",
            "output_path": str(path),
            "output_size_bytes": size_bytes,
            "sha256": sha256_value,
            "project_id": payload.project_id,
            "source_id": payload.request.source_id,
            "audio_id": payload.request.audio_id,
            "source_sha256": payload.audio.source_sha256,
            "audio_sha256": payload.audio.sha256,
            "segment_count": len(payload.transcript.segments),
            "verification": {"passed": True, "format": "JSON", "schema_version": 1},
            "model_used": True,
            "network_used": True,
        },
        created_at=payload.created_at,
    )


def _build_run(
    *,
    task_id: str,
    project_id: str,
    request: MediaTranscriptionRequest,
    status: str,
    summary: str,
    message: str,
    started_at: str,
    duration_ms: int = 0,
    failure_reason: str | None = None,
    transcript: MediaTranscriptInfo | None = None,
    artifact: WorkflowArtifact | None = None,
    route_audit: ModelRouteAuditSnapshot | None = None,
    runtime: AudioModelRuntime | None = None,
    provider_result: QwenAudioTranscriptionResult | None = None,
) -> WorkflowRun:
    output = _base_output(project_id=project_id, request=request)
    output.update({"message": message, "failure_reason": failure_reason, "verification_passed": artifact is not None})
    if runtime is not None:
        output.update({"provider": runtime.provider, "model": runtime.model})
    if provider_result is not None:
        output.update(_provider_result_output(provider_result))
    if transcript is not None:
        output["transcript"] = transcript.model_dump(mode="json")
    if artifact is not None:
        output["artifact_id"] = artifact.artifact_id
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
        provider_input_tokens=provider_result.input_tokens if provider_result is not None else None,
        provider_output_tokens=provider_result.output_tokens if provider_result is not None else None,
        provider_total_tokens=provider_result.total_tokens if provider_result is not None else None,
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level="medium",
        steps=[
            WorkflowStepRun(
                step_id=MEDIA_TRANSCRIPTION_STEP_ID,
                agent=MEDIA_AGENT_ID,
                action=MEDIA_TRANSCRIPTION_TOOL_NAME,
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


async def _persist_failed_task(
    *,
    task_id: str,
    project_id: str,
    request: MediaTranscriptionRequest,
    message: str,
    duration_ms: int,
    failure_reason: str,
    route_audit: ModelRouteAuditSnapshot | None = None,
    runtime: AudioModelRuntime | None = None,
    provider_result: QwenAudioTranscriptionResult | None = None,
) -> MediaTranscriptionTaskResultResponse:
    failed = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="failed",
        summary="媒体转写未完成，未登记未验证的转写交付物。",
        message=message,
        started_at=_started_at(task_id),
        duration_ms=duration_ms,
        failure_reason=failure_reason,
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
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
        level="error",
        message=message,
    )
    return _result_from_run(failed)


def _save_running_run(
    *,
    task_id: str,
    project_id: str,
    request: MediaTranscriptionRequest,
    started_at: str,
    route_audit: ModelRouteAuditSnapshot | None,
    runtime: AudioModelRuntime | None,
) -> None:
    running = _build_run(
        task_id=task_id,
        project_id=project_id,
        request=request,
        status="running",
        summary="正在执行一次媒体转写并验证 JSON 交付。",
        message="正在校验受控 WAV 并等待语音模型返回稳定时间戳。",
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


def _persist_reconciled_completion(
    *, run: WorkflowRun, payload: MediaTranscriptionArtifactPayload, artifact: WorkflowArtifact
) -> None:
    message = "服务重启后已对账到通过回读验证的转写 JSON，并补齐任务终态。"
    completed = _build_run(
        task_id=run.task_id,
        project_id=payload.project_id,
        request=payload.request,
        status="completed",
        summary="服务重启后已对账并恢复已验证的媒体转写交付。",
        message=message,
        started_at=run.metrics.started_at or _now(),
        duration_ms=run.metrics.duration_ms,
        transcript=payload.transcript,
        artifact=artifact,
    ).model_copy(
        update={
            "model_routes": list(run.model_routes),
            "steps": [_step_from_recovered_payload(base=_step(run), payload=payload, artifact=artifact, message=message)],
            "metrics": run.metrics.model_copy(
                update={
                    "finished_at": _now(),
                    "step_total": max(1, run.metrics.step_total),
                    "step_completed": 1,
                    "step_failed": 0,
                    "tool_call_total": max(1, run.metrics.tool_call_total),
                    "tool_call_failed": 0,
                    "provider_model_request_total": max(1, run.metrics.provider_model_request_total),
                    "provider_usage_reported_request_total": max(
                        1 if bool(payload.provider_usage.get("usage_reported")) else 0,
                        run.metrics.provider_usage_reported_request_total,
                    ),
                    "provider_input_tokens": _usage_int(payload.provider_usage.get("input_tokens")),
                    "provider_output_tokens": _usage_int(payload.provider_usage.get("output_tokens")),
                    "provider_total_tokens": _usage_int(payload.provider_usage.get("total_tokens")),
                }
            ),
        }
    )
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=completed,
        events=[*events, _event(run.task_id, len(events) + 1, "task_reconciled_after_restart", message, level="warning")],
        plan=None,
        artifacts=[artifact],
        tool_calls=[_tool_call(completed)],
    )


def _step_from_recovered_payload(
    *,
    base: WorkflowStepRun,
    payload: MediaTranscriptionArtifactPayload,
    artifact: WorkflowArtifact,
    message: str,
) -> WorkflowStepRun:
    # 保留启动时冻结的 Provider/模型/usage，恢复时不能重新解析当前配置覆盖历史事实。
    output = dict(base.output)
    output.update(
        {
            "message": message,
            "failure_reason": None,
            "verification_passed": True,
            "transcript": payload.transcript.model_dump(mode="json"),
            "artifact_id": artifact.artifact_id,
            "provider": payload.provider,
            "model": payload.model,
            "provider_request_id_sha256": payload.provider_request_id_sha256,
            "provider_usage": payload.provider_usage,
            "reconciled_after_service_restart": True,
        }
    )
    return base.model_copy(update={"status": "completed", "message": message, "output": output})


def _persist_restart_failure(run: WorkflowRun, message: str) -> None:
    output = _step(run).output
    failed = _build_run(
        task_id=run.task_id,
        project_id=str(output.get("project_id", "")),
        request=_request_from_output(output),
        status="failed",
        summary="服务重启中断媒体转写，未发现可验证的 JSON 交付。",
        message=message,
        started_at=run.metrics.started_at or _now(),
        duration_ms=run.metrics.duration_ms,
        failure_reason="provider_outcome_unknown",
    ).model_copy(update={"model_routes": list(run.model_routes)})
    events = list(load_task_log_events(run.task_id) or [])
    save_workflow_run(
        run=failed,
        events=[*events, _event(run.task_id, len(events) + 1, "task_interrupted_by_restart", message, level="warning")],
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(failed)] if run.model_routes else [],
    )


def _result_from_run(run: WorkflowRun) -> MediaTranscriptionTaskResultResponse:
    output = _step(run).output
    transcript = None
    if run.status == "completed" and isinstance(output.get("transcript"), dict):
        try:
            transcript = MediaTranscriptInfo.model_validate(output["transcript"])
        except ValueError:
            transcript = None
    artifact_id = output.get("artifact_id")
    return MediaTranscriptionTaskResultResponse(
        task_id=run.task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", _step(run).message)),
        failure_reason=output.get("failure_reason"),
        transcript=transcript,
        artifact_id=artifact_id if isinstance(artifact_id, str) else None,
    )


def _tool_call(run: WorkflowRun) -> WorkflowToolCall:
    step = _step(run)
    output = step.output
    status = "completed" if run.status == "completed" else "failed" if run.status == "failed" else "running"
    result: dict[str, object] = {"verification_passed": bool(output.get("verification_passed", False))}
    if output.get("artifact_id"):
        result["artifact_id"] = output["artifact_id"]
    if isinstance(output.get("transcript"), dict):
        segments = output["transcript"].get("segments")
        result["segment_count"] = len(segments) if isinstance(segments, list) else 0
    if output.get("failure_reason"):
        result["failure_reason"] = output["failure_reason"]
    if output.get("provider_usage") is not None:
        result["provider_usage"] = output["provider_usage"]
    return WorkflowToolCall(
        call_id=f"call_media_transcription_{run.task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=run.task_id,
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        tool_name=MEDIA_TRANSCRIPTION_TOOL_NAME,
        status=status,  # type: ignore[arg-type]
        risk_level="medium",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=run.metrics.duration_ms,
        request={
            "project_id": output.get("project_id"),
            "source_id": output.get("source_id"),
            "audio_id": output.get("audio_id"),
            "language_hints": output.get("language_hints", []),
            "speaker_diarization": output.get("speaker_diarization", False),
            "provider": output.get("provider", ""),
            "model": output.get("model", ""),
            "model_used": True,
            "network_used": True,
        },
        result=result,
        error="" if run.status != "failed" else str(output.get("message", "")),
        finished_at=_now() if run.status in {"completed", "failed"} else "",
    )


def _base_output(*, project_id: str, request: MediaTranscriptionRequest) -> dict[str, object]:
    return {
        "project_id": project_id,
        "source_id": request.source_id,
        "audio_id": request.audio_id,
        "language_hints": list(request.language_hints),
        "speaker_diarization": request.speaker_diarization,
        "model_used": True,
        "network_used": True,
    }


def _provider_usage(result: QwenAudioTranscriptionResult) -> dict[str, int | bool | None]:
    return {
        "usage_reported": result.usage_reported,
        "duration_seconds": result.duration_seconds,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
    }


def _provider_result_output(result: QwenAudioTranscriptionResult) -> dict[str, object]:
    return {
        "provider": result.provider,
        "model": result.model,
        "provider_request_id_sha256": _hash_text(result.request_id),
        "provider_usage": _provider_usage(result),
    }


def _request_from_output(output: dict[str, object]) -> MediaTranscriptionRequest:
    return MediaTranscriptionRequest.model_validate(
        {
            "source_id": output.get("source_id"),
            "audio_id": output.get("audio_id"),
            "language_hints": output.get("language_hints", []),
            "speaker_diarization": output.get("speaker_diarization", False),
        }
    )


def _is_media_transcription_run(run: WorkflowRun | None) -> bool:
    return bool(
        run
        and any(
            step.step_id == MEDIA_TRANSCRIPTION_STEP_ID and step.action == MEDIA_TRANSCRIPTION_TOOL_NAME
            for step in run.steps
        )
    )


def _is_cancelled_run(run: WorkflowRun | None) -> bool:
    return bool(run and _is_media_transcription_run(run) and run.status == "cancelled")


def _step(run: WorkflowRun) -> WorkflowStepRun:
    return next(step for step in run.steps if step.step_id == MEDIA_TRANSCRIPTION_STEP_ID)


def _running_events(task_id: str) -> list[TaskLogEvent]:
    return [
        _event(task_id, 1, "task_queued", "媒体转写已受理，尚未向模型 Provider 发送音频。"),
        _event(task_id, 2, "task_started", "正在校验受控音轨和语音转写模型配置。"),
        _event(task_id, 3, "tool_started", "正在向已配置的语音模型提交一段受控 WAV。"),
    ]


def _events_for_terminal(*, task_id: str, message: str, event: str, level: str = "info") -> list[TaskLogEvent]:
    return [*_running_events(task_id), _event(task_id, 4, event, message, level=level)]


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
        step_id=MEDIA_TRANSCRIPTION_STEP_ID,
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


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _usage_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
