"""Offline A/V sync regression for the constrained single-source EDL renderer.

The fixture has three known white-flash / audio-pulse pairs.  It is cut into three
one-second clips so the same pairs appear at the beginning, middle, and end of the
rendered MP4.  The check compares each file's decoded video/audio onset offset; it
does not mistake a matching ffprobe duration for A/V synchronization evidence.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_edl_sync_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_MEDIA_EDL_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "media_edl")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-edl-sync-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.media_edl import MediaEditDecisionList  # noqa: E402
from app.services.media_source_preparation import (  # noqa: E402
    import_media_source_bytes,
    probe_media_source,
    render_media_edl,
)
from app.services.media_workspace import create_media_project  # noqa: E402


SAMPLE_RATE = 48_000
FRAME_RATE = 25
FRAME_WIDTH = 320
FRAME_HEIGHT = 180
PULSE_DURATION_MS = 280
SOURCE_PULSE_STARTS_MS = (1_000, 3_000, 5_000)
OUTPUT_PULSE_STARTS_MS = (0, 1_000, 2_000)
MAX_OFFSET_CHANGE_MS = 80


def _tool_paths() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found on PATH")
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    if not Path(ffprobe).is_file():
        raise RuntimeError("ffprobe was not found next to ffmpeg")
    return ffmpeg, ffprobe


def _write_pulse_wav(path: Path, *, duration_ms: int) -> None:
    """Write deterministic PCM pulses without using a model or external media."""

    frame_count = SAMPLE_RATE * duration_ms // 1000
    pulse_frames = SAMPLE_RATE * PULSE_DURATION_MS // 1000
    starts = tuple(SAMPLE_RATE * value // 1000 for value in SOURCE_PULSE_STARTS_MS)
    samples = bytearray()
    for frame_index in range(frame_count):
        active_start = next(
            (start for start in starts if start <= frame_index < start + pulse_frames),
            None,
        )
        if active_start is not None:
            phase = 2.0 * math.pi * 880.0 * (frame_index - active_start) / SAMPLE_RATE
            sample = int(0.55 * 32767 * math.sin(phase))
        else:
            sample = 0
        samples.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(bytes(samples))


def _make_sync_fixture(path: Path, ffmpeg: str) -> None:
    audio_path = path.with_suffix(".wav")
    _write_pulse_wav(audio_path, duration_ms=6_000)
    filters = ",".join(
        (
            "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,1.000,1.280)'",
            "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,3.000,3.280)'",
            "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,5.000,5.280)'",
        )
    )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={FRAME_WIDTH}x{FRAME_HEIGHT}:r={FRAME_RATE}:d=6",
        "-i",
        str(audio_path),
        "-filter:v",
        filters,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    _run_ffmpeg(command, "could not generate the synthetic A/V sync fixture")
    audio_path.unlink(missing_ok=True)


def _run_ffmpeg(command: list[str], failure_message: str) -> bytes:
    completed = subprocess.run(command, check=False, capture_output=True, timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(failure_message)
    return completed.stdout


def _decoded_video_luminance(path: Path, ffmpeg: str) -> list[float]:
    raw = _run_ffmpeg(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"fps={FRAME_RATE}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        "could not decode synthetic video frames",
    )
    frame_size = FRAME_WIDTH * FRAME_HEIGHT
    if len(raw) < frame_size or len(raw) % frame_size != 0:
        raise RuntimeError("decoded video fixture does not contain complete grayscale frames")
    return [sum(raw[offset : offset + frame_size]) / frame_size for offset in range(0, len(raw), frame_size)]


def _decoded_audio_rms(path: Path, ffmpeg: str) -> list[float]:
    raw = _run_ffmpeg(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        "could not decode synthetic audio samples",
    )
    if len(raw) < 4 or len(raw) % 4 != 0:
        raise RuntimeError("decoded audio fixture does not contain float samples")
    samples = struct.unpack(f"<{len(raw) // 4}f", raw)
    window_samples = SAMPLE_RATE // 100
    return [
        math.sqrt(sum(sample * sample for sample in samples[index : index + window_samples]) / window_samples)
        for index in range(0, len(samples) - window_samples + 1, window_samples)
    ]


def _detect_video_onset_ms(luminance: list[float], expected_ms: int) -> int:
    expected_frame = round(expected_ms * FRAME_RATE / 1000)
    first_frame = max(0, expected_frame - 3)
    last_frame = min(len(luminance), expected_frame + 9)
    for frame_index in range(first_frame, last_frame):
        if luminance[frame_index] >= 180.0:
            return round(frame_index * 1000 / FRAME_RATE)
    raise RuntimeError(f"no white flash detected near {expected_ms} ms")


def _detect_audio_onset_ms(rms: list[float], expected_ms: int) -> int:
    expected_window = round(expected_ms / 10)
    first_window = max(0, expected_window - 10)
    last_window = min(len(rms), expected_window + 30)
    for window_index in range(first_window, last_window):
        if rms[window_index] >= 0.08:
            return window_index * 10
    raise RuntimeError(f"no audio pulse detected near {expected_ms} ms")


def _measure_offsets(path: Path, ffmpeg: str, expected_starts_ms: tuple[int, ...]) -> list[dict[str, int]]:
    luminance = _decoded_video_luminance(path, ffmpeg)
    rms = _decoded_audio_rms(path, ffmpeg)
    offsets: list[dict[str, int]] = []
    for expected_ms in expected_starts_ms:
        video_onset_ms = _detect_video_onset_ms(luminance, expected_ms)
        audio_onset_ms = _detect_audio_onset_ms(rms, expected_ms)
        offsets.append(
            {
                "expected_start_ms": expected_ms,
                "video_onset_ms": video_onset_ms,
                "audio_onset_ms": audio_onset_ms,
                "video_minus_audio_ms": video_onset_ms - audio_onset_ms,
            }
        )
    return offsets


def _run() -> dict[str, object]:
    ffmpeg, ffprobe = _tool_paths()
    source_path = VERIFY_ROOT / "sync-fixture.mp4"
    _make_sync_fixture(source_path, ffmpeg)

    project = create_media_project(title="EDL sync fixture")
    source = import_media_source_bytes(
        project_scope=project.project_id,
        filename="sync-fixture.mp4",
        content=source_path.read_bytes(),
    )
    probe = probe_media_source(
        source_id=source.source_id,
        expected_project_scope=project.project_id,
        ffprobe_executable=ffprobe,
    )
    assert probe.duration_seconds and 5.5 <= probe.duration_seconds <= 6.5

    edl = MediaEditDecisionList.model_validate(
        {
            "source_id": source.source_id,
            "clips": [
                {"begin_ms": 1_000, "end_ms": 2_000},
                {"begin_ms": 3_000, "end_ms": 4_000},
                {"begin_ms": 5_000, "end_ms": 6_000},
            ],
        }
    )
    rendered_path = VERIFY_ROOT / "output" / "sync-edl.mp4"
    render = render_media_edl(
        edl=edl,
        expected_project_scope=project.project_id,
        output_path=rendered_path,
        ffprobe_executable=ffprobe,
        ffmpeg_executable=ffmpeg,
    )
    assert abs(render.rendered_duration_ms - 3_000) <= 500

    source_offsets = _measure_offsets(source_path, ffmpeg, SOURCE_PULSE_STARTS_MS)
    rendered_offsets = _measure_offsets(rendered_path, ffmpeg, OUTPUT_PULSE_STARTS_MS)
    measurements: list[dict[str, int | str]] = []
    for position, source_offset, rendered_offset in zip(
        ("start", "middle", "end"), source_offsets, rendered_offsets, strict=True
    ):
        offset_change_ms = abs(
            int(rendered_offset["video_minus_audio_ms"]) - int(source_offset["video_minus_audio_ms"])
        )
        assert offset_change_ms <= MAX_OFFSET_CHANGE_MS, (position, source_offset, rendered_offset)
        measurements.append(
            {
                "position": position,
                "source_offset_ms": int(source_offset["video_minus_audio_ms"]),
                "rendered_offset_ms": int(rendered_offset["video_minus_audio_ms"]),
                "offset_change_ms": offset_change_ms,
            }
        )

    return {
        "ok": True,
        "network_used": False,
        "model_used": False,
        "fixture": "program_generated_flash_audio_pulses",
        "max_allowed_offset_change_ms": MAX_OFFSET_CHANGE_MS,
        "max_observed_offset_change_ms": max(item["offset_change_ms"] for item in measurements),
        "measurements": measurements,
        "rendered_duration_ms": render.rendered_duration_ms,
    }


def main() -> None:
    try:
        print(json.dumps(_run(), ensure_ascii=False, sort_keys=True))
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
