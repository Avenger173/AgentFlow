"""对 MM-4 的真实受控媒体转写交付链路执行一次探针。

默认不联网。``--live`` 只允许一次 Windows SAPI 生成英文短句的模型调用：生成 MP4、受控导入、
真实 ffprobe/ffmpeg 音轨提取、Qwen 转写、JSON 回读与 Artifact 登记。所有数据写入忽略的
评测目录；manifest 不含 Key、绝对路径、音频/视频正文、转写正文或 Provider 原始响应。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from uuid import uuid4
import wave


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
_FIXTURE_TEXT = "AgentFlow validates the media transcription delivery."


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one generated-fixture live media transcription delivery probe.")
    parser.add_argument("--live", action="store_true", help="Allow exactly one real Qwen Audio request.")
    parser.add_argument("--ffmpeg-path", type=Path, help="Absolute path to the approved ffmpeg executable.")
    parser.add_argument("--ffprobe-path", type=Path, help="Absolute path to the approved ffprobe executable.")
    return parser.parse_args()


def _require_executable(path: Path | None, *, name: str) -> Path:
    if path is None:
        raise SystemExit(f"Missing --{name}-path. This probe does not guess a development tool path.")
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".exe":
        raise SystemExit(f"Configured {name} executable is unavailable.")
    return resolved


def _run(command: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Generated-fixture media command could not start or finish.") from exc
    if completed.returncode != 0:
        raise RuntimeError("Generated-fixture media command failed.")
    return completed


def _synthesize_fixture(path: Path) -> dict[str, object]:
    escaped_path = str(path.resolve()).replace("'", "''")
    escaped_text = _FIXTURE_TEXT.replace("'", "''")
    command = (
        "Add-Type -AssemblyName System.Speech; "
        "$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new(); "
        "$culture = [System.Globalization.CultureInfo]::GetCultureInfo('en-US'); "
        "$synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet, "
        "[System.Speech.Synthesis.VoiceAge]::NotSet, 0, $culture); "
        f"$synth.SetOutputToWaveFile('{escaped_path}'); "
        f"$synth.Speak('{escaped_text}'); "
        "$synth.Dispose();"
    )
    completed = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], timeout_seconds=45.0)
    if not path.is_file():
        raise RuntimeError("Windows SAPI did not create the generated fixture.")
    with wave.open(str(path), "rb") as source:
        return {
            "channels": source.getnchannels(),
            "sample_rate": source.getframerate(),
            "duration_seconds": round(source.getnframes() / float(source.getframerate()), 3),
        }


def _write_manifest(directory: Path, payload: dict[str, object]) -> None:
    (directory / "run_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _normalized_text(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


async def _execute(*, evidence_dir: Path, ffmpeg_path: Path, ffprobe_path: Path) -> dict[str, object]:
    # 在切换到临时数据目录前解析现有的受控模型路由。解密后的 Key 仅停留在内存 runtime，
    # 后续临时 SQLite 和 evidence 目录不会复制 model_config 或 Key。
    sys.path.insert(0, str(BACKEND_ROOT))
    from app.services.model_gateway import resolve_audio_model_runtime_for_route

    resolution = resolve_audio_model_runtime_for_route("media_transcription", validate=True)
    runtime = resolution.runtime
    route_audit = resolution.audit_snapshot(stage="media_transcription")

    os.environ["AGENTFLOW_DATA_DIR"] = str(evidence_dir / "isolated_data")
    os.environ["AGENTFLOW_OUTPUT_DIR"] = str(evidence_dir / "isolated_output")
    os.environ["AGENTFLOW_DATABASE_PATH"] = str(evidence_dir / "isolated_data" / "probe.db")

    from app.database.task_repository import list_workflow_artifacts, load_workflow_run
    from app.schemas.media_source import MediaTranscriptionRequest
    from app.services.media_source_preparation import (
        extract_primary_audio_for_transcription,
        import_media_source_bytes,
        probe_media_source,
    )
    from app.services.media_transcription_delivery import (
        create_media_transcription_queued_run,
        run_media_transcription_task,
    )
    from app.services.media_workspace import create_media_project

    spoken_wav = evidence_dir / "generated_sapi.wav"
    fixture_metadata = _synthesize_fixture(spoken_wav)
    mp4_path = evidence_dir / "generated_fixture.mp4"
    _run(
        [
            str(ffmpeg_path),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=25",
            "-i",
            str(spoken_wav),
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(mp4_path),
        ],
        timeout_seconds=90.0,
    )
    project = create_media_project(title="Generated media transcription probe")
    source_bytes = mp4_path.read_bytes()
    source = import_media_source_bytes(
        project_scope=project.project_id,
        filename="generated-fixture.mp4",
        content=source_bytes,
    )
    probe = probe_media_source(
        source_id=source.source_id,
        expected_project_scope=project.project_id,
        ffprobe_executable=ffprobe_path,
    )
    audio = extract_primary_audio_for_transcription(
        source_id=source.source_id,
        expected_project_scope=project.project_id,
        ffprobe_executable=ffprobe_path,
        ffmpeg_executable=ffmpeg_path,
    )
    request = MediaTranscriptionRequest(source_id=source.source_id, audio_id=audio.audio_id, language_hints=["en"])
    task_id = f"task_media_transcription_{uuid4().hex[:12]}"
    create_media_transcription_queued_run(task_id=task_id, project_id=project.project_id, request=request)
    started = perf_counter()
    result = await run_media_transcription_task(
        task_id=task_id,
        project_id=project.project_id,
        request=request,
        runtime=runtime,
        route_audit=route_audit,
    )
    elapsed_ms = round((perf_counter() - started) * 1000, 3)
    if result.status != "completed" or result.transcript is None or result.artifact_id is None:
        raise RuntimeError(f"Live transcription task ended as {result.status}: {result.message}")
    if _normalized_text(result.transcript.text) != _normalized_text(_FIXTURE_TEXT):
        raise RuntimeError("Live transcription text does not match the generated fixture after normalization.")
    run = load_workflow_run(task_id)
    artifacts = list_workflow_artifacts(task_id)
    if run is None or len(artifacts) != 1:
        raise RuntimeError("Live transcription task did not persist a single verified Artifact.")
    artifact = artifacts[0]
    if artifact.metadata.get("verification", {}).get("passed") is not True:
        raise RuntimeError("Live transcription Artifact did not retain JSON verification.")
    return {
        "probe": "live_media_transcription_delivery_v1",
        "route": "media_transcription",
        "fixture": "windows_sapi_generated_english_speech_muxed_with_black_video",
        "fixture_metadata": fixture_metadata,
        "model_call_limit": 1,
        "model_call_count": 1,
        "provider": runtime.provider,
        "model": runtime.model,
        "elapsed_ms": elapsed_ms,
        "source": source.model_dump(mode="json"),
        "probe_result": probe.model_dump(mode="json"),
        "derived_audio": audio.model_dump(mode="json"),
        "task": {
            "status": result.status,
            "artifact_id": result.artifact_id,
            "segment_count": len(result.transcript.segments),
            "word_timestamp_count": sum(len(segment.words) for segment in result.transcript.segments),
            "transcript_text_sha256": sha256(result.transcript.text.encode("utf-8")).hexdigest(),
            "transcript_char_count": len(result.transcript.text),
            "provider_usage": run.metrics.model_dump(mode="json"),
            "artifact_sha256": artifact.metadata.get("sha256"),
            "artifact_size_bytes": artifact.metadata.get("output_size_bytes"),
        },
        "billing_amount": "unknown",
        "result": "passed",
    }


def main() -> None:
    args = _parse_arguments()
    if not args.live:
        print("Dry run only. Pass --live to allow one generated-fixture Qwen Audio request.")
        return
    ffmpeg_path = _require_executable(args.ffmpeg_path, name="ffmpeg")
    ffprobe_path = _require_executable(args.ffprobe_path, name="ffprobe")
    started_at = datetime.now(UTC)
    evidence_dir = PROJECT_ROOT / "data" / "media_evaluations" / f"live_media_transcription_delivery_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    try:
        summary = asyncio.run(_execute(evidence_dir=evidence_dir, ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path))
    except Exception as exc:
        summary = {
            "probe": "live_media_transcription_delivery_v1",
            "route": "media_transcription",
            "fixture": "windows_sapi_generated_english_speech_muxed_with_black_video",
            "model_call_limit": 1,
            "result": "failed",
            "failure_type": type(exc).__name__,
            "failure_message": str(exc)[:240],
            "billing_amount": "unknown",
        }
        exit_code = 1
    else:
        exit_code = 0
    summary["started_at"] = started_at.isoformat()
    summary["completed_at"] = datetime.now(UTC).isoformat()
    _write_manifest(evidence_dir, summary)
    print(json.dumps({"evidence_dir": str(evidence_dir), **summary}, ensure_ascii=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
