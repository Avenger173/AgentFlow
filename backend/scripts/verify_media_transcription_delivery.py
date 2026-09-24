"""离线验证 MM-4 受控 WAV 转写任务、JSON 交付与重启对账。

脚本通过 ffprobe/ffmpeg 和 Qwen Audio 的内存替身构造完整链路，不读取用户媒体、不读取
本机 Key、不访问网络，也不消耗 Provider 额度。它验证跨项目拒绝、未知结果不重放、取消、
Artifact 回读和 API 轮询，而不是把 Mock 当成真实 ASR 成绩。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from uuid import uuid4
import wave

from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_transcription_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-transcription-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import list_workflow_artifacts, list_workflow_tool_calls, load_workflow_run
from app.schemas.media_source import MediaTranscriptionRequest
from app.services.media_source_preparation import (
    MediaProcessResult,
    extract_primary_audio_for_transcription,
    import_media_source_bytes,
    probe_media_source,
)
from app.services.media_transcription_delivery import (
    _transcript_from_provider,
    _write_verified_transcript_artifact,
    cancel_media_transcription_task,
    create_media_transcription_queued_run,
    get_media_transcription_task_result,
    recover_interrupted_media_transcription_tasks,
    run_media_transcription_task,
)
from app.services.media_workspace import create_media_project
from app.services.model_gateway import AudioModelRuntime
from app.services.qwen_audio_transcription import (
    QwenAudioTranscriptSegment,
    QwenAudioTranscriptionOutcomeUnknownError,
    QwenAudioTranscriptionProviderError,
    QwenAudioTranscriptionResult,
    QwenAudioWord,
)
from main import create_app


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * 16_000)


def _probe_json() -> str:
    return json.dumps(
        {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.000"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 320, "height": 180},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
            ],
        }
    )


def _media_runner(command: tuple[str, ...] | list[str], _timeout: float) -> MediaProcessResult:
    normalized = tuple(command)
    if normalized[0] == "fixture-ffprobe":
        return MediaProcessResult(returncode=0, stdout=_probe_json(), stderr="")
    if normalized[0] == "fixture-ffmpeg":
        assert ("-map", "0:1") == (normalized[8], normalized[9])
        assert normalized[10:17] == ("-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le")
        _write_wav(Path(normalized[-1]))
        return MediaProcessResult(returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected fixture tool: {normalized[0]}")


def _runtime() -> AudioModelRuntime:
    return AudioModelRuntime(
        provider="qwen_audio",
        label="Qwen Audio / fixture",
        transport="dashscope_multimodal",
        base_url="https://dashscope.fixture.invalid/api/v1",
        model="qwen-audio-3.1-asr-flash",
        api_key="fixture-only-key",
    )


def _provider_result(*, request_id: str = "fixture-provider-request-id") -> QwenAudioTranscriptionResult:
    return QwenAudioTranscriptionResult(
        provider="qwen_audio",
        model="qwen-audio-3.1-asr-flash",
        request_id=request_id,
        text="AgentFlow validates the transcription delivery.",
        segments=(
            QwenAudioTranscriptSegment(
                sentence_id=0,
                text="AgentFlow validates the transcription delivery.",
                begin_ms=0,
                end_ms=1000,
                words=(
                    QwenAudioWord(text="AgentFlow", begin_ms=0, end_ms=300, punctuation=""),
                    QwenAudioWord(text="validates", begin_ms=300, end_ms=600, punctuation=""),
                    QwenAudioWord(text="delivery", begin_ms=600, end_ms=1000, punctuation="."),
                ),
            ),
        ),
        duration_seconds=1,
        input_tokens=24,
        output_tokens=8,
        total_tokens=32,
        usage_reported=True,
    )


async def _success_transcriber(**kwargs: object) -> QwenAudioTranscriptionResult:
    audio = kwargs["audio"]
    assert getattr(audio, "audio_format") == "wav"
    assert len(getattr(audio, "audio_bytes")) > 44
    assert kwargs["language_hints"] == ("en",)
    return _provider_result()


def _prepare_source(project_id: str):  # type: ignore[no-untyped-def]
    source = import_media_source_bytes(
        project_scope=project_id,
        filename="fixture.mp4",
        content=b"synthetic-video-fixture",
    )
    probe_media_source(
        source_id=source.source_id,
        expected_project_scope=project_id,
        ffprobe_executable="fixture-ffprobe",
        command_runner=_media_runner,
    )
    audio = extract_primary_audio_for_transcription(
        source_id=source.source_id,
        expected_project_scope=project_id,
        ffprobe_executable="fixture-ffprobe",
        ffmpeg_executable="fixture-ffmpeg",
        command_runner=_media_runner,
    )
    return source, audio


async def _run() -> None:
    project = create_media_project(title="媒体转写交付夹具")
    source, audio = _prepare_source(project.project_id)
    request = MediaTranscriptionRequest(
        source_id=source.source_id,
        audio_id=audio.audio_id,
        language_hints=[" EN ", "en"],
    )

    success_task_id = "task_media_transcription_0123456789ab"
    create_media_transcription_queued_run(task_id=success_task_id, project_id=project.project_id, request=request)
    success = await run_media_transcription_task(
        task_id=success_task_id,
        project_id=project.project_id,
        request=request,
        runtime=_runtime(),
        transcriber=_success_transcriber,
    )
    assert success.status == "completed", success
    assert success.transcript is not None and len(success.transcript.segments) == 1
    assert success.artifact_id == "artifact_media_transcription_0123456789ab"
    run = load_workflow_run(success_task_id)
    assert run is not None and run.metrics.provider_total_tokens == 32
    assert "fixture-provider-request-id" not in run.model_dump_json()
    artifacts = list_workflow_artifacts(success_task_id)
    assert len(artifacts) == 1 and artifacts[0].metadata["verification"]["passed"] is True
    assert artifacts[0].metadata["output_path"]
    calls = list_workflow_tool_calls(success_task_id)
    assert len(calls) == 1 and calls[0].result["verification_passed"] is True
    assert calls[0].request["source_id"] == source.source_id

    # 跨项目只能在读取受控 WAV 前失败，模型替身不能被触发。
    other_project = create_media_project(title="隔离项目")
    isolation_task_id = "task_media_transcription_123456789abc"
    create_media_transcription_queued_run(task_id=isolation_task_id, project_id=other_project.project_id, request=request)
    called = False

    async def must_not_call(**_: object) -> QwenAudioTranscriptionResult:
        nonlocal called
        called = True
        return _provider_result()

    isolated = await run_media_transcription_task(
        task_id=isolation_task_id,
        project_id=other_project.project_id,
        request=request,
        runtime=_runtime(),
        transcriber=must_not_call,
    )
    assert isolated.status == "failed" and isolated.failure_reason == "validation_failed" and not called

    async def rejected_transcriber(**_: object) -> QwenAudioTranscriptionResult:
        raise QwenAudioTranscriptionProviderError(status_code=400, error_code="InvalidParameter", message="fixture rejected")

    rejected_task_id = "task_media_transcription_23456789abcd"
    create_media_transcription_queued_run(task_id=rejected_task_id, project_id=project.project_id, request=request)
    rejected = await run_media_transcription_task(
        task_id=rejected_task_id,
        project_id=project.project_id,
        request=request,
        runtime=_runtime(),
        transcriber=rejected_transcriber,
    )
    assert rejected.status == "failed" and rejected.failure_reason == "provider_rejected"

    async def unknown_transcriber(**_: object) -> QwenAudioTranscriptionResult:
        raise QwenAudioTranscriptionOutcomeUnknownError(reason="request_timeout", message="fixture timeout")

    unknown_task_id = "task_media_transcription_3456789abcde"
    create_media_transcription_queued_run(task_id=unknown_task_id, project_id=project.project_id, request=request)
    unknown = await run_media_transcription_task(
        task_id=unknown_task_id,
        project_id=project.project_id,
        request=request,
        runtime=_runtime(),
        transcriber=unknown_transcriber,
    )
    assert unknown.status == "failed" and unknown.failure_reason == "provider_outcome_unknown"
    assert "不会自动重试" in unknown.message

    # 执行前取消不允许触发模型替身。
    cancelled_task_id = "task_media_transcription_456789abcdef"
    create_media_transcription_queued_run(task_id=cancelled_task_id, project_id=project.project_id, request=request)
    cancelled = await cancel_media_transcription_task(cancelled_task_id)
    assert cancelled is not None and cancelled.accepted
    cancelled_result = await run_media_transcription_task(
        task_id=cancelled_task_id,
        project_id=project.project_id,
        request=request,
        runtime=_runtime(),
        transcriber=must_not_call,
    )
    assert cancelled_result.status == "cancelled" and not called

    # 模拟 JSON 已原子写入、但 completed Runtime 尚未来得及提交的崩溃窗口。
    recoverable_task_id = "task_media_transcription_56789abcdef0"
    create_media_transcription_queued_run(task_id=recoverable_task_id, project_id=project.project_id, request=request)
    _write_verified_transcript_artifact(
        task_id=recoverable_task_id,
        project_id=project.project_id,
        request=request,
        audio=audio,
        transcript=_transcript_from_provider(_provider_result(request_id="recovery-request-id")),
        result=_provider_result(request_id="recovery-request-id"),
    )
    missing_task_id = "task_media_transcription_6789abcdef01"
    create_media_transcription_queued_run(task_id=missing_task_id, project_id=project.project_id, request=request)

    with TestClient(create_app()) as client:
        assert client.app.state.recovered_media_transcription_task_count == 2
        recovered = get_media_transcription_task_result(recoverable_task_id)
        assert recovered is not None and recovered.status == "completed" and recovered.transcript is not None
        missing = get_media_transcription_task_result(missing_task_id)
        assert missing is not None and missing.status == "failed"
        assert missing.failure_reason == "provider_outcome_unknown"

        preview = client.get(f"/api/tasks/{success_task_id}/artifacts/{success.artifact_id}/preview")
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["available"] is True
        assert "AgentFlow validates" in preview_body["text"]
        assert preview_body["metadata"]["output_path"] == "<hidden>"

        # 媒体 API 只接受 Base64，且后台真正连接到新任务服务。
        invalid_import = client.post(
            f"/api/agents/media_agent/projects/{project.project_id}/media-sources",
            json={"filename": "invalid.mp4", "content_base64": "not base64!"},
        )
        assert invalid_import.status_code == 400, invalid_import.text

        from app.api import media_agent as media_api

        original_runner = media_api.run_media_transcription_task

        async def api_runner(**kwargs: object):  # type: ignore[no-untyped-def]
            return await original_runner(**kwargs, runtime=_runtime(), transcriber=_success_transcriber)

        media_api.run_media_transcription_task = api_runner
        try:
            started = client.post(
                f"/api/agents/media_agent/projects/{project.project_id}/transcriptions/start",
                json=request.model_dump(),
            )
            assert started.status_code == 202, started.text
            api_task_id = started.json()["task_id"]
            api_result = None
            for _ in range(100):
                response = client.get(f"/api/agents/media_agent/transcriptions/{api_task_id}/result")
                assert response.status_code == 200, response.text
                api_result = response.json()
                if api_result["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            assert api_result is not None and api_result["status"] == "completed", api_result
            unified = client.get(f"/api/tasks/{api_task_id}")
            assert unified.status_code == 200 and unified.json()["steps"][0]["action"] == "media.transcribe_audio"
        finally:
            media_api.run_media_transcription_task = original_runner

    # 恢复函数可重复调用，不会为终态任务创建第二份 Artifact 或第二次模型请求。
    assert recover_interrupted_media_transcription_tasks() == []


def main() -> None:
    try:
        asyncio.run(_run())
        print("Media transcription delivery verification passed.")
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
