"""验证 MM-4 受控媒体源、探测与音轨提取契约。

脚本仅使用临时目录和注入式 ffprobe/ffmpeg 替身。替身会写入一个真实可由 wave 回读的
16 kHz 单声道 WAV，用来验证命令白名单、文件原子提交和 hash 回读；不会执行系统工具、
联网、读取用户文件或调用模型。
"""

from __future__ import annotations

import json
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import wave


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.media_source_preparation import (
    MediaProcessResult,
    MediaSourcePreparationError,
    extract_primary_audio_for_transcription,
    get_media_source,
    import_media_source_bytes,
    media_transcription_preparation_status,
    probe_media_source,
    read_transcription_audio_bytes,
)
from app.api.health import health


def _write_pcm_wav(path: Path, *, seconds: int = 2) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * (16_000 * seconds))


def _fixture_probe(*, with_audio: bool) -> str:
    streams: list[dict[str, object]] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
        }
    ]
    if with_audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "tags": {"language": "eng", "title": "must not leak"},
            }
        )
    return json.dumps(
        {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.000"},
            "streams": streams,
        }
    )


def _verify_import_probe_extract_and_reuse() -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...] | list[str], _timeout: float) -> MediaProcessResult:
        normalized = tuple(command)
        commands.append(normalized)
        if normalized[0] == "fixture-ffprobe":
            assert normalized[1:4] == ("-v", "error", "-show_format")
            assert "-show_streams" in normalized
            assert normalized[-1].endswith("source.mp4")
            return MediaProcessResult(returncode=0, stdout=_fixture_probe(with_audio=True), stderr="")
        if normalized[0] == "fixture-ffmpeg":
            assert normalized[1:8] == ("-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", normalized[7])
            assert ("-map", "0:1") == (normalized[8], normalized[9])
            assert ("-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le") == normalized[10:17]
            target = Path(normalized[-1])
            assert target.name.endswith(".tmp.wav")
            _write_pcm_wav(target)
            return MediaProcessResult(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected fixture command: {normalized[0]}")

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        original = b"synthetic-mp4-fixture-not-a-real-video"
        source = import_media_source_bytes(
            project_scope="project_demo",
            filename="demo.mp4",
            content=original,
            root_dir=root,
        )
        assert get_media_source(
            source.source_id,
            expected_project_scope="project_demo",
            root_dir=root,
        ) == source
        try:
            get_media_source(source.source_id, expected_project_scope="project_other", root_dir=root)
        except MediaSourcePreparationError as exc:
            assert "不属于指定项目范围" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected scope isolation")

        probe = probe_media_source(
            source_id=source.source_id,
            expected_project_scope="project_demo",
            root_dir=root,
            ffprobe_executable="fixture-ffprobe",
            command_runner=runner,
        )
        assert probe.container_format.startswith("mov,mp4")
        assert probe.duration_seconds == 2.0
        assert [(item.stream_index, item.codec_name, item.sample_rate, item.channels, item.language) for item in probe.audio_streams] == [
            (1, "aac", 48_000, 2, "eng")
        ]
        assert [(item.stream_index, item.width, item.height) for item in probe.video_streams] == [(0, 1920, 1080)]

        audio = extract_primary_audio_for_transcription(
            source_id=source.source_id,
            expected_project_scope="project_demo",
            root_dir=root,
            ffprobe_executable="fixture-ffprobe",
            ffmpeg_executable="fixture-ffmpeg",
            command_runner=runner,
        )
        assert audio.source_id == source.source_id
        assert audio.source_stream_index == 1
        assert audio.sample_rate == 16_000 and audio.channels == 1
        assert audio.size_bytes <= 7 * 1024 * 1024
        restored, audio_bytes = read_transcription_audio_bytes(
            source_id=source.source_id,
            audio_id=audio.audio_id,
            expected_project_scope="project_demo",
            root_dir=root,
        )
        assert restored == audio
        assert len(audio_bytes) == audio.size_bytes
        assert original == (root / "sources" / source.source_id / "source.mp4").read_bytes()

        reused = extract_primary_audio_for_transcription(
            source_id=source.source_id,
            expected_project_scope="project_demo",
            root_dir=root,
            ffprobe_executable="fixture-ffprobe",
            ffmpeg_executable="fixture-ffmpeg",
            command_runner=runner,
        )
        assert reused == audio
        assert [item[0] for item in commands].count("fixture-ffprobe") == 1
        assert [item[0] for item in commands].count("fixture-ffmpeg") == 1

        manifest = (root / "sources" / source.source_id / "manifest.json").read_text(encoding="utf-8")
        assert "base64" not in manifest.lower()
        assert "must not leak" not in manifest
        assert str(root) not in manifest


def _verify_no_audio_and_tamper_rejection() -> None:
    calls = 0

    def no_audio_runner(command: tuple[str, ...] | list[str], _timeout: float) -> MediaProcessResult:
        nonlocal calls
        calls += 1
        assert tuple(command)[0] == "fixture-ffprobe"
        return MediaProcessResult(returncode=0, stdout=_fixture_probe(with_audio=False), stderr="")

    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = import_media_source_bytes(
            project_scope="project_demo",
            filename="silent.mp4",
            content=b"synthetic-silent-video",
            root_dir=root,
        )
        try:
            extract_primary_audio_for_transcription(
                source_id=source.source_id,
                root_dir=root,
                ffprobe_executable="fixture-ffprobe",
                ffmpeg_executable="fixture-ffmpeg-must-not-run",
                command_runner=no_audio_runner,
            )
        except MediaSourcePreparationError as exc:
            assert "没有可用于转写的音轨" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected no-audio rejection")
        assert calls == 1

        tampered = import_media_source_bytes(
            project_scope="project_demo",
            filename="tampered.wav",
            content=b"original-audio",
            root_dir=root,
        )
        internal_path = root / "sources" / tampered.source_id / "source.wav"
        internal_path.write_bytes(b"tampered-audio")
        try:
            probe_media_source(
                source_id=tampered.source_id,
                root_dir=root,
                ffprobe_executable="fixture-ffprobe",
                command_runner=no_audio_runner,
            )
        except MediaSourcePreparationError as exc:
            assert "哈希不匹配" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected hash rejection")
        assert calls == 1


def main() -> None:
    status = media_transcription_preparation_status()
    assert isinstance(status["ready"], bool)
    assert isinstance(status["message"], str) and status["message"]
    health_response = asyncio.run(health())
    assert health_response.capabilities["media_transcription_preparation"].ready == status["ready"]
    _verify_import_probe_extract_and_reuse()
    _verify_no_audio_and_tamper_rejection()
    print("Media source preparation verification passed.")


if __name__ == "__main__":
    main()
