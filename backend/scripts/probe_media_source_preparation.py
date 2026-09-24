"""执行一次 MM-4 受控媒体源的真实 FFmpeg E1 探针。

探针只生成两秒钟的纯色视频与纯音调，不读取用户文件、不调用模型。生成后的 MP4 仍须经
``media_source_preparation`` 的私有导入、ffprobe、受控音轨提取及 WAV 回读路径；报告只
写入模型无关的元数据、哈希和结果，不写入绝对路径、媒体正文或 FFmpeg stderr。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import wave


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings
from app.services.media_source_preparation import (
    extract_primary_audio_for_transcription,
    import_media_source_bytes,
    probe_media_source,
    read_transcription_audio_bytes,
)


_EXPECTED_DURATION_SECONDS = 2.0


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one generated-fixture FFmpeg E1 probe.")
    parser.add_argument("--execute", action="store_true", help="Allow the real local FFmpeg invocation.")
    parser.add_argument("--ffmpeg-path", type=Path, help="Absolute path to the approved ffmpeg executable.")
    parser.add_argument("--ffprobe-path", type=Path, help="Absolute path to the approved ffprobe executable.")
    return parser.parse_args()


def _require_executable(path: Path | None, *, tool_name: str) -> Path:
    if path is None:
        raise SystemExit(f"Missing --{tool_name}-path. This probe does not guess a development tool path.")
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".exe":
        raise SystemExit(f"Configured {tool_name} executable is unavailable.")
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
        raise RuntimeError("Generated-fixture FFmpeg probe could not start or finish.") from exc
    if completed.returncode != 0:
        raise RuntimeError("Generated-fixture FFmpeg command failed.")
    return completed


def _ffmpeg_version(executable: Path) -> str:
    completed = _run([str(executable), "-version"], timeout_seconds=15.0)
    first_line = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
    return first_line[:200]


def _write_manifest(*, evidence_dir: Path, payload: dict[str, object]) -> Path:
    manifest_path = evidence_dir / "manifest.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return manifest_path


def main() -> None:
    args = _parse_arguments()
    if not args.execute:
        raise SystemExit("Refusing real FFmpeg execution without --execute.")
    ffmpeg_path = _require_executable(args.ffmpeg_path, tool_name="ffmpeg")
    ffprobe_path = _require_executable(args.ffprobe_path, tool_name="ffprobe")

    started_at = datetime.now(UTC)
    evidence_dir = (
        settings.data_dir
        / "media_evaluations"
        / f"media_source_preparation_e1_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    evidence_dir.mkdir(parents=True, exist_ok=False)
    fixture_path = evidence_dir / "generated_fixture.mp4"
    source_root = evidence_dir / "controlled_sources"

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
            "color=c=black:s=320x240:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=2",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(fixture_path),
        ],
        timeout_seconds=60.0,
    )
    fixture_bytes = fixture_path.read_bytes()
    source = import_media_source_bytes(
        project_scope="evaluation_mm4_e1",
        filename="generated-fixture.mp4",
        content=fixture_bytes,
        root_dir=source_root,
    )
    probe = probe_media_source(
        source_id=source.source_id,
        expected_project_scope="evaluation_mm4_e1",
        root_dir=source_root,
        ffprobe_executable=ffprobe_path,
    )
    audio = extract_primary_audio_for_transcription(
        source_id=source.source_id,
        expected_project_scope="evaluation_mm4_e1",
        root_dir=source_root,
        ffprobe_executable=ffprobe_path,
        ffmpeg_executable=ffmpeg_path,
    )
    restored, audio_bytes = read_transcription_audio_bytes(
        source_id=source.source_id,
        audio_id=audio.audio_id,
        expected_project_scope="evaluation_mm4_e1",
        root_dir=source_root,
    )
    with wave.open(str(source_root / "sources" / source.source_id / "derived" / f"{audio.audio_id}.wav"), "rb") as wav:
        frame_count = wav.getnframes()
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
    duration_seconds = frame_count / sample_rate
    if sample_rate != 16_000 or channels != 1 or sample_width != 2:
        raise RuntimeError("Extracted WAV does not satisfy the controlled ASR contract.")
    if abs(duration_seconds - _EXPECTED_DURATION_SECONDS) > 0.25:
        raise RuntimeError("Extracted WAV duration does not match the generated fixture.")
    if restored != audio or len(audio_bytes) != audio.size_bytes or sha256(audio_bytes).hexdigest() != audio.sha256:
        raise RuntimeError("Extracted WAV failed metadata or hash readback verification.")

    payload: dict[str, object] = {
        "probe": "media_source_preparation_e1",
        "fixture": "program_generated_black_video_with_880hz_tone",
        "source_kind": "generated_fixture",
        "request_count": 0,
        "model": None,
        "provider": None,
        "ffmpeg_version": _ffmpeg_version(ffmpeg_path),
        "ffprobe_version": _ffmpeg_version(ffprobe_path),
        "source": source.model_dump(mode="json"),
        "probe_result": probe.model_dump(mode="json"),
        "derived_audio": audio.model_dump(mode="json"),
        "wav_readback": {
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width_bytes": sample_width,
            "frame_count": frame_count,
            "duration_seconds": round(duration_seconds, 3),
            "sha256": sha256(audio_bytes).hexdigest(),
        },
        "result": "passed",
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = _write_manifest(evidence_dir=evidence_dir, payload=payload)
    print(f"Media source E1 probe passed. Evidence: {manifest_path}")


if __name__ == "__main__":
    main()
