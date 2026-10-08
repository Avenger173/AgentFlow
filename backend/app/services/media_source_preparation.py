"""为音视频转写准备可审计的私有源文件与规范化音频。

图片工作区已有不可变 revision 语义，不能把视频和音频硬塞进同一份图片 manifest。本模块只
提供 MM-4 所需的最小受控源文件层：导入方传入内存字节，文件写入私有目录；ffprobe/ffmpeg
只能通过 source_id 取得内部路径。它不暴露 API、不会调用模型、不会创建任务或字幕产物。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from shutil import which
import subprocess
from threading import Lock, RLock
from typing import Any
from uuid import uuid4
import wave

from PIL import Image, UnidentifiedImageError

from app.core.config import settings
from app.schemas.media_source import (
    MediaAudioStreamInfo,
    MediaProbeInfo,
    MediaSourceInfo,
    MediaSourceMimeType,
    MediaTranscriptionAudioInfo,
    MediaVideoStreamInfo,
)
from app.schemas.media_edl import MediaEditDecisionList, MediaEdlRenderInfo


MAX_MEDIA_SOURCE_BYTES = 256 * 1024 * 1024
MAX_TRANSCRIPTION_AUDIO_BYTES = 7 * 1024 * 1024
# 16 kHz / 单声道 / s16le WAV 每秒 32,000 bytes。210 秒留出文件头与实现余量，
# 既满足当前 Qwen 单请求 7 MiB 限制，也避免把分段策略暴露给桌面端。
MAX_TRANSCRIPTION_CHUNK_SECONDS = 210
MAX_TRANSCRIPTION_CHUNKS = 8
MAX_TRANSCRIPTION_TOTAL_AUDIO_BYTES = MAX_TRANSCRIPTION_AUDIO_BYTES * MAX_TRANSCRIPTION_CHUNKS
MAX_EDL_RENDER_BYTES = 256 * 1024 * 1024
EDL_RENDER_DURATION_TOLERANCE_MS = 500
MAX_VIDEO_KEYFRAME_BYTES = 12 * 1024 * 1024
_SOURCE_ID_PATTERN = re.compile(r"^ms_[0-9a-f]{16}$")
_DERIVED_AUDIO_ID_PATTERN = re.compile(r"^mda_[0-9a-f]{16}$")
_SAFE_SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,180}$")
_SOURCE_TYPE_BY_SUFFIX: dict[str, MediaSourceMimeType] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}
_SOURCE_LOCKS: dict[str, RLock] = {}
_SOURCE_LOCKS_GUARD = Lock()


class MediaSourcePreparationError(ValueError):
    """可安全展示的媒体准备错误；永不拼接内部绝对路径或命令输出。"""


class MediaToolUnavailableError(MediaSourcePreparationError):
    """本机尚未配置受控 FFmpeg/ffprobe 可执行文件。"""


class MediaToolExecutionError(MediaSourcePreparationError):
    """工具无法验证或生成受控媒体文件。"""


@dataclass(frozen=True)
class MediaProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class MediaTranscriptionAudioChunk:
    """一个已回读且可单独提交给 ASR 的受控 WAV 分段。"""

    chunk_index: int
    begin_ms: int
    end_ms: int
    audio_bytes: bytes


@dataclass(frozen=True)
class MediaVideoKeyframeRenderInfo:
    """服务端从受控视频读出的单张 JPEG 关键帧元数据。"""

    timestamp_ms: int
    sha256: str
    size_bytes: int
    width: int
    height: int


@dataclass(frozen=True)
class _TranscriptionWavMetadata:
    sha256: str
    size_bytes: int
    frame_count: int

    @property
    def duration_seconds(self) -> float:
        return round(self.frame_count / 16_000.0, 3)


MediaCommandRunner = Callable[[Sequence[str], float], MediaProcessResult]


def media_transcription_preparation_status() -> dict[str, object]:
    """轻量报告 ffprobe/ffmpeg 是否可用，不启动进程或读取任何媒体文件。"""

    missing: list[str] = []
    for tool_name in ("ffprobe", "ffmpeg"):
        try:
            _resolve_media_tool(tool_name, explicit=None, allow_fixture=False)
        except MediaToolUnavailableError:
            missing.append(tool_name)
    if not missing:
        return {
            "ready": True,
            "message": "媒体转写预处理依赖已就绪。",
        }
    return {
        "ready": False,
        "message": f"媒体转写预处理缺少：{'、'.join(missing)}。请安装 FFmpeg 或设置对应 AGENTFLOW_*_PATH。",
    }


def media_source_root(*, root_dir: Path | None = None) -> Path:
    return (root_dir if root_dir is not None else settings.media_source_dir).resolve()


def import_media_source_bytes(
    *,
    project_scope: str,
    filename: str,
    content: bytes,
    root_dir: Path | None = None,
) -> MediaSourceInfo:
    """将已经由上层读入的字节写入私有目录；调用方不能提供本机路径。"""

    safe_scope = _validate_project_scope(project_scope)
    safe_filename, suffix, mime_type = _validate_source_filename(filename)
    if not content:
        raise MediaSourcePreparationError("音视频素材不能为空。")
    if len(content) > MAX_MEDIA_SOURCE_BYTES:
        raise MediaSourcePreparationError("音视频素材超过当前受控导入上限 256 MB。")

    root = media_source_root(root_dir=root_dir)
    sources_root = root / "sources"
    sources_root.mkdir(parents=True, exist_ok=True)
    source_id = _new_id("ms")
    source_dir = (sources_root / source_id).resolve()
    source_dir.relative_to(sources_root.resolve())
    source_dir.mkdir(parents=False, exist_ok=False)
    relative_file = f"source{suffix}"
    source_path = _resolve_source_file(source_dir, relative_file)
    now = _utc_now()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source_id": source_id,
        "project_scope": safe_scope,
        "filename": safe_filename,
        "source_file": relative_file,
        "source_sha256": _sha256_bytes(content),
        "mime_type": mime_type,
        "size_bytes": len(content),
        "created_at": now,
        "probe": None,
        "derived_audio": [],
    }
    try:
        _atomic_write_bytes(source_path, content)
        _write_manifest(source_dir, manifest)
    except Exception:
        _remove_source_directory(source_dir)
        raise
    return _source_info(manifest)


def import_media_source_staged_file(
    *,
    project_scope: str,
    filename: str,
    staged_path: Path,
    root_dir: Path | None = None,
) -> MediaSourceInfo:
    """Atomically import a server-staged upload without loading the media into memory.

    ``staged_path`` is created by the API in a private directory and is never derived
    from a client-supplied path. The caller still receives only the source metadata.
    """

    safe_scope = _validate_project_scope(project_scope)
    safe_filename, suffix, mime_type = _validate_source_filename(filename)
    try:
        source_size = staged_path.stat().st_size
    except OSError as exc:
        raise MediaSourcePreparationError("待导入的音视频素材不存在。") from exc
    if source_size <= 0:
        raise MediaSourcePreparationError("音视频素材不能为空。")
    if source_size > MAX_MEDIA_SOURCE_BYTES:
        raise MediaSourcePreparationError("音视频素材超过当前受控导入上限 256 MB。")

    root = media_source_root(root_dir=root_dir)
    sources_root = root / "sources"
    sources_root.mkdir(parents=True, exist_ok=True)
    source_id = _new_id("ms")
    source_dir = (sources_root / source_id).resolve()
    source_dir.relative_to(sources_root.resolve())
    source_dir.mkdir(parents=False, exist_ok=False)
    relative_file = f"source{suffix}"
    source_path = _resolve_source_file(source_dir, relative_file)
    now = _utc_now()
    try:
        source_sha256, copied_size = _atomic_copy_file_and_hash(staged_path, source_path)
        if copied_size != source_size:
            raise MediaSourcePreparationError("音视频素材在导入期间发生变化。")
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "source_id": source_id,
            "project_scope": safe_scope,
            "filename": safe_filename,
            "source_file": relative_file,
            "source_sha256": source_sha256,
            "mime_type": mime_type,
            "size_bytes": copied_size,
            "created_at": now,
            "probe": None,
            "derived_audio": [],
        }
        _write_manifest(source_dir, manifest)
    except Exception:
        _remove_source_directory(source_dir)
        raise
    return _source_info(manifest)


def get_media_source(
    source_id: str,
    *,
    expected_project_scope: str | None = None,
    root_dir: Path | None = None,
) -> MediaSourceInfo:
    """读取脱敏元数据；给定 scope 时必须先匹配，防止跨项目引用。"""

    _, manifest = _load_source_manifest(source_id, root_dir=root_dir)
    _require_project_scope(manifest, expected_project_scope)
    return _source_info(manifest)


def probe_media_source(
    *,
    source_id: str,
    expected_project_scope: str | None = None,
    root_dir: Path | None = None,
    ffprobe_executable: str | Path | None = None,
    command_runner: MediaCommandRunner | None = None,
) -> MediaProbeInfo:
    """用固定 ffprobe 参数提取媒体事实；不返回路径、标签正文或完整命令输出。"""

    with _source_write_lock(source_id):
        source_dir, manifest = _load_source_manifest(source_id, root_dir=root_dir)
        _require_project_scope(manifest, expected_project_scope)
        source_path = _verified_source_path(source_dir, manifest)
        executable = _resolve_media_tool(
            "ffprobe",
            explicit=ffprobe_executable,
            allow_fixture=command_runner is not None,
        )
        result = (command_runner or _run_media_command)(
            (
                executable,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(source_path),
            ),
            30.0,
        )
        if result.returncode != 0:
            raise MediaToolExecutionError("ffprobe 无法读取当前受控媒体文件。")
        probe = _parse_probe_result(source_id=source_id, source_sha256=manifest["source_sha256"], stdout=result.stdout)
        manifest["probe"] = probe.model_dump(mode="json")
        _write_manifest(source_dir, manifest)
        return probe


def extract_primary_audio_for_transcription(
    *,
    source_id: str,
    expected_project_scope: str | None = None,
    root_dir: Path | None = None,
    ffprobe_executable: str | Path | None = None,
    ffmpeg_executable: str | Path | None = None,
    command_runner: MediaCommandRunner | None = None,
) -> MediaTranscriptionAudioInfo:
    """只提取首个已验证音轨为 16 kHz 单声道 WAV，并在回读通过后才登记。"""

    with _source_write_lock(source_id):
        source_dir, manifest = _load_source_manifest(source_id, root_dir=root_dir)
        _require_project_scope(manifest, expected_project_scope)
        source_path = _verified_source_path(source_dir, manifest)
        probe = _load_or_probe_locked(
            source_id=source_id,
            source_dir=source_dir,
            manifest=manifest,
            ffprobe_executable=ffprobe_executable,
            command_runner=command_runner,
        )
        if not probe.audio_streams:
            raise MediaSourcePreparationError("当前媒体没有可用于转写的音轨。")
        audio_stream = probe.audio_streams[0]
        existing = _find_verified_derived_audio(manifest, source_dir, source_sha256=manifest["source_sha256"], stream_index=audio_stream.stream_index)
        if existing is not None:
            return existing

        executable = _resolve_media_tool(
            "ffmpeg",
            explicit=ffmpeg_executable,
            allow_fixture=command_runner is not None,
        )
        if probe.duration_seconds is not None and probe.duration_seconds > MAX_TRANSCRIPTION_CHUNK_SECONDS * MAX_TRANSCRIPTION_CHUNKS:
            raise MediaSourcePreparationError(
                f"当前短视频最长支持 {MAX_TRANSCRIPTION_CHUNK_SECONDS * MAX_TRANSCRIPTION_CHUNKS // 60} 分钟转写，请先截取需要的片段。"
            )

        audio_id = _new_id("mda")
        derived_dir = source_dir / "derived"
        derived_dir.mkdir(exist_ok=True)
        temporary_path = _resolve_source_file(derived_dir, f"{audio_id}.source.tmp.wav")
        result = (command_runner or _run_media_command)(
            (
                executable,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-map",
                f"0:{audio_stream.stream_index}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(temporary_path),
            ),
            90.0,
        )
        if result.returncode != 0:
            temporary_path.unlink(missing_ok=True)
            raise MediaToolExecutionError("ffmpeg 无法从当前媒体生成转写音轨。")
        pending_paths: list[tuple[Path, Path]] = []
        try:
            audio, record, pending_paths = _prepare_transcription_audio_bundle(
                source_dir=source_dir,
                derived_dir=derived_dir,
                source_path=temporary_path,
                audio_id=audio_id,
                source_id=source_id,
                source_sha256=manifest["source_sha256"],
                source_stream_index=audio_stream.stream_index,
            )
            for pending_path, final_path in pending_paths:
                os.replace(pending_path, final_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            for pending_path, final_path in pending_paths:
                pending_path.unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
            raise
        manifest["derived_audio"].append(record)
        _write_manifest(source_dir, manifest)
        return audio


def read_transcription_audio_bytes(
    *,
    source_id: str,
    audio_id: str,
    expected_project_scope: str | None = None,
    root_dir: Path | None = None,
) -> tuple[MediaTranscriptionAudioInfo, bytes]:
    """兼容旧的单段读取方；分段媒体必须使用 ``read_transcription_audio_chunks``。"""

    audio, chunks = read_transcription_audio_chunks(
        source_id=source_id,
        audio_id=audio_id,
        expected_project_scope=expected_project_scope,
        root_dir=root_dir,
    )
    if len(chunks) != 1:
        raise MediaSourcePreparationError("该受控音频包含多个转写分段，必须按顺序提交并合并时间轴。")
    return audio, chunks[0].audio_bytes


def read_transcription_audio_chunks(
    *,
    source_id: str,
    audio_id: str,
    expected_project_scope: str | None = None,
    root_dir: Path | None = None,
) -> tuple[MediaTranscriptionAudioInfo, tuple[MediaTranscriptionAudioChunk, ...]]:
    """读取并回读每个受控 ASR 分段，绝不暴露内部存储路径。"""

    source_dir, manifest = _load_source_manifest(source_id, root_dir=root_dir)
    _require_project_scope(manifest, expected_project_scope)
    _validate_id(audio_id, _DERIVED_AUDIO_ID_PATTERN, "转写音频")
    for record in manifest["derived_audio"]:
        if record.get("audio_id") != audio_id:
            continue
        audio = _derived_audio_info(record)
        return audio, _read_verified_transcription_audio_chunks(source_dir=source_dir, record=record, audio=audio)
    raise MediaSourcePreparationError("未找到指定的受控转写音频。")


def render_media_edl(
    *,
    edl: MediaEditDecisionList,
    expected_project_scope: str,
    output_path: Path,
    root_dir: Path | None = None,
    ffprobe_executable: str | Path | None = None,
    ffmpeg_executable: str | Path | None = None,
    command_runner: MediaCommandRunner | None = None,
) -> MediaEdlRenderInfo:
    """Render a constrained single-source EDL into a verified MP4 artifact."""

    final_path = _validate_edl_output_path(output_path)
    temporary_path = final_path.with_name(f".{final_path.stem}.{uuid4().hex}.tmp.mp4")
    with _source_write_lock(edl.source_id):
        source_dir, manifest = _load_source_manifest(edl.source_id, root_dir=root_dir)
        _require_project_scope(manifest, expected_project_scope)
        source_path = _verified_source_path(source_dir, manifest)
        probe = _load_or_probe_locked(
            source_id=edl.source_id,
            source_dir=source_dir,
            manifest=manifest,
            ffprobe_executable=ffprobe_executable,
            command_runner=command_runner,
        )
        _validate_edl_against_source(edl=edl, probe=probe)
        video_stream = probe.video_streams[0]
        audio_stream = probe.audio_streams[0]
        executable = _resolve_media_tool(
            "ffmpeg",
            explicit=ffmpeg_executable,
            allow_fixture=command_runner is not None,
        )
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        command = _build_edl_render_command(
            executable=executable,
            source_path=source_path,
            video_stream_index=video_stream.stream_index,
            audio_stream_index=audio_stream.stream_index,
            edl=edl,
            output_path=temporary_path,
        )
        try:
            result = (command_runner or _run_media_command)(command, 180.0)
            if result.returncode != 0:
                raise MediaToolExecutionError("FFmpeg 无法渲染指定的受限剪辑片段。")
            info = _verify_edl_render_output(
                path=temporary_path,
                edl=edl,
                source_sha256=manifest["source_sha256"],
                ffprobe_executable=ffprobe_executable,
                command_runner=command_runner,
            )
            os.replace(temporary_path, final_path)
            return info
        except Exception:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise


def verify_media_edl_render(
    *,
    edl: MediaEditDecisionList,
    expected_project_scope: str,
    output_path: Path,
    root_dir: Path | None = None,
    ffprobe_executable: str | Path | None = None,
    command_runner: MediaCommandRunner | None = None,
) -> MediaEdlRenderInfo:
    """Re-read a completed MP4 without re-running FFmpeg."""

    final_path = _validate_edl_output_path(output_path)
    with _source_write_lock(edl.source_id):
        source_dir, manifest = _load_source_manifest(edl.source_id, root_dir=root_dir)
        _require_project_scope(manifest, expected_project_scope)
        _verified_source_path(source_dir, manifest)
        probe = _load_or_probe_locked(
            source_id=edl.source_id,
            source_dir=source_dir,
            manifest=manifest,
            ffprobe_executable=ffprobe_executable,
            command_runner=command_runner,
        )
        _validate_edl_against_source(edl=edl, probe=probe)
        return _verify_edl_render_output(
            path=final_path,
            edl=edl,
            source_sha256=manifest["source_sha256"],
            ffprobe_executable=ffprobe_executable,
            command_runner=command_runner,
        )


def extract_media_video_keyframe(
    *,
    source_id: str,
    expected_project_scope: str,
    timestamp_ms: int,
    output_path: Path,
    root_dir: Path | None = None,
    ffprobe_executable: str | Path | None = None,
    ffmpeg_executable: str | Path | None = None,
    command_runner: MediaCommandRunner | None = None,
) -> MediaVideoKeyframeRenderInfo:
    """从受控视频提取一帧 JPEG，时间点只能由上层已验证计划提供。

    这个函数不接收滤镜、尺寸、输入路径或编码器选项。它只为讲解 HTML 的受控图片资源提供
    单帧提取与回读，不复用为任意截图接口。
    """

    if timestamp_ms < 0:
        raise MediaSourcePreparationError("关键帧时间不能为负数。")
    final_path = output_path.resolve()
    if final_path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise MediaSourcePreparationError("关键帧交付只能使用 JPEG 格式。")
    temporary_path = final_path.with_name(f".{final_path.stem}.{uuid4().hex}.tmp.jpg")
    with _source_write_lock(source_id):
        source_dir, manifest = _load_source_manifest(source_id, root_dir=root_dir)
        _require_project_scope(manifest, expected_project_scope)
        source_path = _verified_source_path(source_dir, manifest)
        probe = _load_or_probe_locked(
            source_id=source_id,
            source_dir=source_dir,
            manifest=manifest,
            ffprobe_executable=ffprobe_executable,
            command_runner=command_runner,
        )
        if not probe.video_streams:
            raise MediaSourcePreparationError("当前受控素材不含视频轨，无法提取讲解关键帧。")
        if probe.duration_seconds is None:
            raise MediaSourcePreparationError("FFprobe 未返回可用时长，无法提取讲解关键帧。")
        duration_ms = int(round(probe.duration_seconds * 1_000))
        if timestamp_ms > duration_ms:
            raise MediaSourcePreparationError("关键帧时间超出已探测的视频时长。")
        executable = _resolve_media_tool(
            "ffmpeg",
            explicit=ffmpeg_executable,
            allow_fixture=command_runner is not None,
        )
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        command = (
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-ss",
            f"{timestamp_ms / 1_000:.3f}",
            "-map",
            f"0:{probe.video_streams[0].stream_index}",
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:-2:force_original_aspect_ratio=decrease",
            "-q:v",
            "2",
            "-an",
            "-y",
            str(temporary_path),
        )
        try:
            result = (command_runner or _run_media_command)(command, 45.0)
            if result.returncode != 0:
                raise MediaToolExecutionError("FFmpeg 无法提取讲解关键帧。")
            info = _verify_media_video_keyframe(path=temporary_path, timestamp_ms=timestamp_ms)
            os.replace(temporary_path, final_path)
            return info
        except Exception:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise


def _verify_media_video_keyframe(*, path: Path, timestamp_ms: int) -> MediaVideoKeyframeRenderInfo:
    if not path.is_file():
        raise MediaToolExecutionError("FFmpeg 未生成可回读的讲解关键帧。")
    size_bytes = path.stat().st_size
    if size_bytes < 1 or size_bytes > MAX_VIDEO_KEYFRAME_BYTES:
        raise MediaSourcePreparationError("讲解关键帧大小不在允许范围内。")
    try:
        with Image.open(path) as image:
            if image.format != "JPEG":
                raise MediaToolExecutionError("讲解关键帧不是 JPEG 格式。")
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaToolExecutionError("讲解关键帧无法通过图片回读校验。") from exc
    if width < 1 or height < 1 or width * height > 40_000_000:
        raise MediaToolExecutionError("讲解关键帧尺寸无效。")
    return MediaVideoKeyframeRenderInfo(
        timestamp_ms=timestamp_ms,
        sha256=_sha256_file(path),
        size_bytes=size_bytes,
        width=width,
        height=height,
    )


def _load_or_probe_locked(
    *,
    source_id: str,
    source_dir: Path,
    manifest: dict[str, Any],
    ffprobe_executable: str | Path | None,
    command_runner: MediaCommandRunner | None,
) -> MediaProbeInfo:
    stored = manifest.get("probe")
    if isinstance(stored, dict):
        probe = _probe_info(stored)
        if probe.source_sha256 == manifest["source_sha256"]:
            return probe
    source_path = _verified_source_path(source_dir, manifest)
    executable = _resolve_media_tool("ffprobe", explicit=ffprobe_executable, allow_fixture=command_runner is not None)
    result = (command_runner or _run_media_command)(
        (executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source_path)),
        30.0,
    )
    if result.returncode != 0:
        raise MediaToolExecutionError("ffprobe 无法读取当前受控媒体文件。")
    probe = _parse_probe_result(source_id=source_id, source_sha256=manifest["source_sha256"], stdout=result.stdout)
    manifest["probe"] = probe.model_dump(mode="json")
    _write_manifest(source_dir, manifest)
    return probe


def _parse_probe_result(*, source_id: str, source_sha256: str, stdout: str) -> MediaProbeInfo:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MediaToolExecutionError("ffprobe 返回了无效的媒体描述。") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("format"), dict):
        raise MediaToolExecutionError("ffprobe 未返回媒体容器信息。")
    raw_format = payload["format"]
    container_format = str(raw_format.get("format_name") or "").strip()
    if not container_format:
        raise MediaToolExecutionError("ffprobe 未识别媒体容器格式。")
    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, list):
        raise MediaToolExecutionError("ffprobe 未返回媒体流信息。")
    audio_streams: list[MediaAudioStreamInfo] = []
    video_streams: list[MediaVideoStreamInfo] = []
    for stream in raw_streams:
        if not isinstance(stream, dict):
            continue
        index = _positive_int(stream.get("index"), allow_zero=True)
        codec_type = str(stream.get("codec_type") or "")
        codec_name = _safe_codec_name(stream.get("codec_name"))
        if index is None or not codec_name:
            continue
        if codec_type == "audio":
            tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
            audio_streams.append(
                MediaAudioStreamInfo(
                    stream_index=index,
                    codec_name=codec_name,
                    sample_rate=_positive_int(stream.get("sample_rate")),
                    channels=_positive_int(stream.get("channels")),
                    language=_safe_language(tags.get("language")),
                )
            )
        elif codec_type == "video":
            video_streams.append(
                MediaVideoStreamInfo(
                    stream_index=index,
                    codec_name=codec_name,
                    width=_positive_int(stream.get("width")),
                    height=_positive_int(stream.get("height")),
                    average_frame_rate=_safe_frame_rate(stream.get("avg_frame_rate")),
                )
            )
    return MediaProbeInfo(
        source_id=source_id,
        source_sha256=source_sha256,
        container_format=container_format[:160],
        duration_seconds=_nonnegative_float(raw_format.get("duration")),
        audio_streams=audio_streams,
        video_streams=video_streams,
        probed_at=_utc_now(),
    )


def _validate_edl_output_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.suffix.lower() != ".mp4":
        raise MediaSourcePreparationError("EDL 渲染交付物只能是 MP4 文件。")
    return resolved


def _validate_edl_against_source(*, edl: MediaEditDecisionList, probe: MediaProbeInfo) -> None:
    if probe.duration_seconds is None:
        raise MediaSourcePreparationError("FFprobe 未返回可用时长，无法安全渲染剪辑片段。")
    if not probe.video_streams or not probe.audio_streams:
        raise MediaSourcePreparationError("EDL 首版只支持同时包含视频和音频轨的素材。")
    source_duration_ms = int(round(probe.duration_seconds * 1000))
    if any(clip.end_ms > source_duration_ms for clip in edl.clips):
        raise MediaSourcePreparationError("EDL 片段超出了已探测的源素材时长。")


def _build_edl_render_command(
    *,
    executable: str,
    source_path: Path,
    video_stream_index: int,
    audio_stream_index: int,
    edl: MediaEditDecisionList,
    output_path: Path,
) -> tuple[str, ...]:
    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, clip in enumerate(edl.clips):
        begin = _edl_seconds(clip.begin_ms)
        end = _edl_seconds(clip.end_ms)
        video_label = f"v{index}"
        audio_label = f"a{index}"
        filters.append(
            f"[0:{video_stream_index}]trim=start={begin}:end={end},setpts=PTS-STARTPTS[{video_label}]"
        )
        filters.append(
            f"[0:{audio_stream_index}]atrim=start={begin}:end={end},asetpts=PTS-STARTPTS[{audio_label}]"
        )
        concat_inputs.extend((f"[{video_label}]", f"[{audio_label}]"))
    filters.append(f"{''.join(concat_inputs)}concat=n={len(edl.clips)}:v=1:a=1[vout][aout]")
    return (
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output_path),
    )


def _verify_edl_render_output(
    *,
    path: Path,
    edl: MediaEditDecisionList,
    source_sha256: str,
    ffprobe_executable: str | Path | None,
    command_runner: MediaCommandRunner | None,
) -> MediaEdlRenderInfo:
    if not path.is_file() or path.stat().st_size < 1 or path.stat().st_size > MAX_EDL_RENDER_BYTES:
        raise MediaSourcePreparationError("EDL 渲染结果不存在、为空或超过交付上限。")
    executable = _resolve_media_tool("ffprobe", explicit=ffprobe_executable, allow_fixture=command_runner is not None)
    result = (command_runner or _run_media_command)(
        (executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)),
        30.0,
    )
    if result.returncode != 0:
        raise MediaToolExecutionError("FFprobe 无法回读 EDL 渲染交付物。")
    probe = _parse_probe_result(source_id=edl.source_id, source_sha256=source_sha256, stdout=result.stdout)
    if probe.duration_seconds is None or not probe.video_streams or not probe.audio_streams:
        raise MediaSourcePreparationError("EDL 渲染结果缺少可验证的时长、视频或音频轨。")
    video = probe.video_streams[0]
    if video.width is None or video.height is None:
        raise MediaSourcePreparationError("EDL 渲染结果缺少视频尺寸。")
    rendered_duration_ms = int(round(probe.duration_seconds * 1000))
    if abs(rendered_duration_ms - edl.requested_duration_ms) > EDL_RENDER_DURATION_TOLERANCE_MS:
        raise MediaSourcePreparationError("EDL 渲染结果与剪辑单时长不一致，已拒绝交付。")
    return MediaEdlRenderInfo(
        source_id=edl.source_id,
        source_sha256=source_sha256,
        clip_count=len(edl.clips),
        requested_duration_ms=edl.requested_duration_ms,
        rendered_duration_ms=rendered_duration_ms,
        sha256=_sha256_file(path),
        size_bytes=path.stat().st_size,
        width=video.width,
        height=video.height,
        video_codec=video.codec_name,
        audio_codec=probe.audio_streams[0].codec_name,
        created_at=_utc_now(),
    )


def _edl_seconds(value_ms: int) -> str:
    return f"{value_ms // 1000}.{value_ms % 1000:03d}"


def _find_verified_derived_audio(
    manifest: dict[str, Any],
    source_dir: Path,
    *,
    source_sha256: str,
    stream_index: int,
) -> MediaTranscriptionAudioInfo | None:
    for record in manifest.get("derived_audio", []):
        if not isinstance(record, dict):
            continue
        try:
            audio = _derived_audio_info(record)
        except MediaSourcePreparationError:
            continue
        if audio.source_sha256 != source_sha256 or audio.source_stream_index != stream_index:
            continue
        try:
            _read_verified_transcription_audio_chunks(source_dir=source_dir, record=record, audio=audio)
        except MediaSourcePreparationError:
            continue
        return audio
    return None


def _prepare_transcription_audio_bundle(
    *,
    source_dir: Path,
    derived_dir: Path,
    source_path: Path,
    audio_id: str,
    source_id: str,
    source_sha256: str,
    source_stream_index: int,
) -> tuple[MediaTranscriptionAudioInfo, dict[str, Any], list[tuple[Path, Path]]]:
    """将完整 WAV 保持在受控目录内，并仅在超过 Provider 上限时按固定边界分段。"""

    source_metadata = _inspect_transcription_wav(path=source_path, enforce_size_limit=False)
    if source_metadata.size_bytes <= MAX_TRANSCRIPTION_AUDIO_BYTES:
        audio = _audio_info_from_wav(
            metadata=_inspect_transcription_wav(path=source_path, enforce_size_limit=True),
            audio_id=audio_id,
            source_id=source_id,
            source_sha256=source_sha256,
            source_stream_index=source_stream_index,
            chunk_count=1,
        )
        final_relative = f"derived/{audio_id}.wav"
        final_path = _resolve_source_file(source_dir, final_relative)
        return audio, audio.model_dump(mode="json") | {"file": final_relative}, [(source_path, final_path)]

    required_chunk_count = (source_metadata.frame_count + MAX_TRANSCRIPTION_CHUNK_SECONDS * 16_000 - 1) // (
        MAX_TRANSCRIPTION_CHUNK_SECONDS * 16_000
    )
    if required_chunk_count > MAX_TRANSCRIPTION_CHUNKS or source_metadata.size_bytes > MAX_TRANSCRIPTION_TOTAL_AUDIO_BYTES:
        raise MediaSourcePreparationError(
            f"当前短视频最多支持 {MAX_TRANSCRIPTION_CHUNK_SECONDS * MAX_TRANSCRIPTION_CHUNKS // 60} 分钟转写，请先截取需要的片段。"
        )

    pending_paths: list[tuple[Path, Path]] = []
    chunks: list[dict[str, object]] = []
    try:
        with wave.open(str(source_path), "rb") as source:
            frame_offset = 0
            chunk_index = 0
            while frame_offset < source_metadata.frame_count:
                frame_count = min(MAX_TRANSCRIPTION_CHUNK_SECONDS * 16_000, source_metadata.frame_count - frame_offset)
                pending_path = _resolve_source_file(derived_dir, f"{audio_id}.part{chunk_index + 1:03d}.tmp.wav")
                final_relative = f"derived/{audio_id}.part{chunk_index + 1:03d}.wav"
                final_path = _resolve_source_file(source_dir, final_relative)
                with wave.open(str(pending_path), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(16_000)
                    target.writeframes(source.readframes(frame_count))
                metadata = _inspect_transcription_wav(path=pending_path, enforce_size_limit=True)
                begin_ms = int(round(frame_offset * 1000 / 16_000))
                frame_offset += frame_count
                end_ms = int(round(frame_offset * 1000 / 16_000))
                chunks.append(
                    {
                        "chunk_index": chunk_index,
                        "file": final_relative,
                        "sha256": metadata.sha256,
                        "size_bytes": metadata.size_bytes,
                        "duration_seconds": metadata.duration_seconds,
                        "begin_ms": begin_ms,
                        "end_ms": end_ms,
                    }
                )
                pending_paths.append((pending_path, final_path))
                chunk_index += 1
    except (OSError, wave.Error) as exc:
        for pending_path, _ in pending_paths:
            pending_path.unlink(missing_ok=True)
        raise MediaToolExecutionError("无法将规范化转写音频切分为可验证的 WAV 分段。") from exc
    finally:
        source_path.unlink(missing_ok=True)

    if len(chunks) != required_chunk_count:
        for pending_path, _ in pending_paths:
            pending_path.unlink(missing_ok=True)
        raise MediaToolExecutionError("规范化转写音频分段数量与受控时长不一致。")

    audio = MediaTranscriptionAudioInfo(
        audio_id=audio_id,
        source_id=source_id,
        source_sha256=source_sha256,
        source_stream_index=source_stream_index,
        sha256=_transcription_bundle_sha256(chunks),
        size_bytes=sum(int(item["size_bytes"]) for item in chunks),
        duration_seconds=source_metadata.duration_seconds,
        chunk_count=len(chunks),
        created_at=_utc_now(),
    )
    return audio, audio.model_dump(mode="json") | {"chunks": chunks}, pending_paths


def _read_verified_transcription_audio_chunks(
    *, source_dir: Path, record: dict[str, Any], audio: MediaTranscriptionAudioInfo
) -> tuple[MediaTranscriptionAudioChunk, ...]:
    raw_chunks = record.get("chunks")
    if raw_chunks is None:
        path = _resolve_source_file(source_dir, str(record.get("file", "")))
        metadata = _inspect_transcription_wav(path=path, enforce_size_limit=True)
        if audio.chunk_count != 1 or metadata.sha256 != audio.sha256 or metadata.size_bytes != audio.size_bytes:
            raise MediaSourcePreparationError("转写音频清单与已回读的 WAV 不一致。")
        if abs(metadata.duration_seconds - audio.duration_seconds) > 0.01:
            raise MediaSourcePreparationError("转写音频时长与清单不一致。")
        return (
            MediaTranscriptionAudioChunk(
                chunk_index=0,
                begin_ms=0,
                end_ms=int(round(metadata.duration_seconds * 1000)),
                audio_bytes=path.read_bytes(),
            ),
        )

    if not isinstance(raw_chunks, list) or len(raw_chunks) != audio.chunk_count or not raw_chunks:
        raise MediaSourcePreparationError("转写音频分段清单无效。")
    chunks: list[MediaTranscriptionAudioChunk] = []
    verified_records: list[dict[str, object]] = []
    previous_end_ms = 0
    for expected_index, raw in enumerate(raw_chunks):
        if not isinstance(raw, dict):
            raise MediaSourcePreparationError("转写音频分段清单无效。")
        try:
            chunk_index = int(raw["chunk_index"])
            begin_ms = int(raw["begin_ms"])
            end_ms = int(raw["end_ms"])
            expected_size = int(raw["size_bytes"])
            expected_sha256 = str(raw["sha256"])
            expected_duration = float(raw["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaSourcePreparationError("转写音频分段清单字段无效。") from exc
        if chunk_index != expected_index or begin_ms != previous_end_ms or end_ms <= begin_ms:
            raise MediaSourcePreparationError("转写音频分段时间范围无效。")
        path = _resolve_source_file(source_dir, str(raw.get("file", "")))
        metadata = _inspect_transcription_wav(path=path, enforce_size_limit=True)
        if (
            metadata.sha256 != expected_sha256
            or metadata.size_bytes != expected_size
            or abs(metadata.duration_seconds - expected_duration) > 0.01
            or abs(metadata.duration_seconds * 1000 - (end_ms - begin_ms)) > 1.0
        ):
            raise MediaSourcePreparationError("转写音频分段已被修改或与清单不一致。")
        verified_records.append(
            {
                "chunk_index": chunk_index,
                "sha256": metadata.sha256,
                "size_bytes": metadata.size_bytes,
                "duration_seconds": metadata.duration_seconds,
                "begin_ms": begin_ms,
                "end_ms": end_ms,
            }
        )
        chunks.append(
            MediaTranscriptionAudioChunk(
                chunk_index=chunk_index,
                begin_ms=begin_ms,
                end_ms=end_ms,
                audio_bytes=path.read_bytes(),
            )
        )
        previous_end_ms = end_ms
    if (
        _transcription_bundle_sha256(verified_records) != audio.sha256
        or sum(len(chunk.audio_bytes) for chunk in chunks) != audio.size_bytes
        or abs(previous_end_ms / 1000.0 - audio.duration_seconds) > 0.01
    ):
        raise MediaSourcePreparationError("转写音频包与分段回读结果不一致。")
    return tuple(chunks)


def _transcription_bundle_sha256(chunks: Sequence[dict[str, object]]) -> str:
    canonical = "\n".join(
        f"{int(item['chunk_index'])}:{int(item['begin_ms'])}:{int(item['end_ms'])}:{str(item['sha256'])}"
        for item in chunks
    )
    return _sha256_bytes(canonical.encode("ascii"))


def _verify_transcription_wav(
    *,
    path: Path,
    audio_id: str,
    source_id: str,
    source_sha256: str,
    source_stream_index: int,
) -> MediaTranscriptionAudioInfo:
    metadata = _inspect_transcription_wav(path=path, enforce_size_limit=True)
    return _audio_info_from_wav(
        metadata=metadata,
        audio_id=audio_id,
        source_id=source_id,
        source_sha256=source_sha256,
        source_stream_index=source_stream_index,
        chunk_count=1,
    )


def _audio_info_from_wav(
    *,
    metadata: _TranscriptionWavMetadata,
    audio_id: str,
    source_id: str,
    source_sha256: str,
    source_stream_index: int,
    chunk_count: int,
) -> MediaTranscriptionAudioInfo:
    return MediaTranscriptionAudioInfo(
        audio_id=audio_id,
        source_id=source_id,
        source_sha256=source_sha256,
        source_stream_index=source_stream_index,
        sha256=metadata.sha256,
        size_bytes=metadata.size_bytes,
        duration_seconds=metadata.duration_seconds,
        chunk_count=chunk_count,
        created_at=_utc_now(),
    )


def _inspect_transcription_wav(*, path: Path, enforce_size_limit: bool) -> _TranscriptionWavMetadata:
    if not path.is_file():
        raise MediaToolExecutionError("ffmpeg 未生成可回读的转写音频。")
    size_bytes = path.stat().st_size
    if size_bytes < 1 or (enforce_size_limit and size_bytes > MAX_TRANSCRIPTION_AUDIO_BYTES):
        raise MediaSourcePreparationError("规范化转写音频分段超过 7 MB 上限。")
    try:
        with wave.open(str(path), "rb") as source:
            sample_rate = source.getframerate()
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            compression = source.getcomptype()
            frames = source.getnframes()
    except (wave.Error, OSError) as exc:
        raise MediaToolExecutionError("ffmpeg 输出不是可读取的 WAV 音频。") from exc
    if sample_rate != 16_000 or channels != 1 or sample_width != 2 or compression != "NONE":
        raise MediaToolExecutionError("ffmpeg 输出未满足 16 kHz 单声道 PCM WAV 契约。")
    return _TranscriptionWavMetadata(sha256=_sha256_file(path), size_bytes=size_bytes, frame_count=frames)


def _load_source_manifest(source_id: str, *, root_dir: Path | None) -> tuple[Path, dict[str, Any]]:
    _validate_id(source_id, _SOURCE_ID_PATTERN, "媒体源")
    root = media_source_root(root_dir=root_dir)
    source_dir = (root / "sources" / source_id).resolve()
    try:
        source_dir.relative_to((root / "sources").resolve())
    except ValueError as exc:
        raise MediaSourcePreparationError("媒体源路径无效。") from exc
    manifest_path = source_dir / "manifest.json"
    if not source_dir.is_dir() or not manifest_path.is_file():
        raise MediaSourcePreparationError("未找到指定受控媒体源。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaSourcePreparationError("受控媒体源元数据无法读取。") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("source_id") != source_id:
        raise MediaSourcePreparationError("受控媒体源元数据结构无效。")
    _source_info(manifest)
    if not isinstance(manifest.get("derived_audio"), list):
        raise MediaSourcePreparationError("受控媒体源派生记录无效。")
    return source_dir, manifest


def _verified_source_path(source_dir: Path, manifest: dict[str, Any]) -> Path:
    path = _resolve_source_file(source_dir, str(manifest.get("source_file", "")))
    if not path.is_file() or path.stat().st_size != int(manifest["size_bytes"]):
        raise MediaSourcePreparationError("受控媒体源不存在或已被修改。")
    if _sha256_file(path) != manifest["source_sha256"]:
        raise MediaSourcePreparationError("受控媒体源哈希不匹配，已拒绝继续处理。")
    return path


def _resolve_media_tool(
    tool_name: str,
    *,
    explicit: str | Path | None,
    allow_fixture: bool,
) -> str:
    if tool_name not in {"ffprobe", "ffmpeg"}:
        raise ValueError("未知媒体工具。")
    if explicit is not None:
        candidate = str(explicit).strip()
        if not candidate:
            raise MediaToolUnavailableError(f"未配置 {tool_name}。")
        if allow_fixture:
            return candidate
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            return str(path)
        raise MediaToolUnavailableError(f"配置的 {tool_name} 不可用。")
    configured = os.getenv(f"AGENTFLOW_{tool_name.upper()}_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            return str(path)
        raise MediaToolUnavailableError(f"配置的 {tool_name} 不可用。")
    discovered = which(tool_name)
    if discovered:
        return discovered
    raise MediaToolUnavailableError(
        f"未检测到 {tool_name}；请安装 FFmpeg 或设置 AGENTFLOW_{tool_name.upper()}_PATH。"
    )


def _run_media_command(command: Sequence[str], timeout_seconds: float) -> MediaProcessResult:
    """不经 Shell 执行固定参数列表；错误正文不回传，避免泄露内部文件名。"""

    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaToolExecutionError("媒体工具执行超时，未生成可交付音频。") from exc
    except OSError as exc:
        raise MediaToolUnavailableError("媒体工具无法启动。") from exc
    return MediaProcessResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _source_info(manifest: dict[str, Any]) -> MediaSourceInfo:
    try:
        return MediaSourceInfo(
            source_id=str(manifest["source_id"]),
            project_scope=str(manifest["project_scope"]),
            filename=str(manifest["filename"]),
            source_sha256=str(manifest["source_sha256"]),
            mime_type=str(manifest["mime_type"]),
            size_bytes=int(manifest["size_bytes"]),
            created_at=str(manifest["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaSourcePreparationError("受控媒体源元数据缺少必要字段。") from exc


def _probe_info(record: dict[str, Any]) -> MediaProbeInfo:
    try:
        return MediaProbeInfo.model_validate(record)
    except (TypeError, ValueError) as exc:
        raise MediaSourcePreparationError("受控媒体源探测记录无效。") from exc


def _derived_audio_info(record: dict[str, Any]) -> MediaTranscriptionAudioInfo:
    try:
        return MediaTranscriptionAudioInfo.model_validate(record)
    except (TypeError, ValueError) as exc:
        raise MediaSourcePreparationError("受控转写音频记录无效。") from exc


def _validate_project_scope(value: str) -> str:
    normalized = value.strip()
    if not _SAFE_SCOPE_PATTERN.fullmatch(normalized):
        raise MediaSourcePreparationError("媒体项目范围格式不正确。")
    return normalized


def _require_project_scope(manifest: dict[str, Any], expected_project_scope: str | None) -> None:
    if expected_project_scope is None:
        return
    if manifest.get("project_scope") != _validate_project_scope(expected_project_scope):
        raise MediaSourcePreparationError("当前媒体源不属于指定项目范围。")


def _validate_source_filename(value: str) -> tuple[str, str, MediaSourceMimeType]:
    name = Path(value).name.strip()
    if name != value.strip() or not _SAFE_FILENAME_PATTERN.fullmatch(name):
        raise MediaSourcePreparationError("媒体文件名不合法。")
    suffix = Path(name).suffix.lower()
    mime_type = _SOURCE_TYPE_BY_SUFFIX.get(suffix)
    if mime_type is None:
        raise MediaSourcePreparationError("当前仅支持 WAV、MP3、M4A、MP4、MOV、MKV 或 WebM 素材。")
    return name, suffix, mime_type


def _resolve_source_file(base_dir: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise MediaSourcePreparationError("受控媒体文件引用无效。")
    candidate = (base_dir / relative_path).resolve()
    try:
        candidate.relative_to(base_dir.resolve())
    except ValueError as exc:
        raise MediaSourcePreparationError("受控媒体文件引用越界。") from exc
    return candidate


def _write_manifest(source_dir: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    target = source_dir / "manifest.json"
    temporary = source_dir / f".manifest-{uuid4().hex}.tmp"
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_copy_file_and_hash(source_path: Path, target_path: Path) -> tuple[str, int]:
    """Copy a private staged file to its immutable source directory and hash that copy."""

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_name(f".{target_path.name}.{uuid4().hex}.tmp")
    digest = sha256()
    copied_size = 0
    try:
        with source_path.open("rb") as source, temporary.open("wb") as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                copied_size += len(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, target_path)
        return digest.hexdigest(), copied_size
    finally:
        temporary.unlink(missing_ok=True)


def _source_write_lock(source_id: str) -> RLock:
    with _SOURCE_LOCKS_GUARD:
        return _SOURCE_LOCKS.setdefault(source_id, RLock())


def _validate_id(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not pattern.fullmatch(value):
        raise MediaSourcePreparationError(f"{label}标识格式不正确。")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _positive_int(value: object, *, allow_zero: bool = False) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if allow_zero:
        return parsed if parsed >= 0 else None
    return parsed if parsed > 0 else None


def _nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _safe_codec_name(value: object) -> str:
    return str(value or "").strip()[:64]


def _safe_language(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized[:24] if normalized else None


def _safe_frame_rate(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized[:32] if normalized and re.fullmatch(r"[0-9]+(?:/[0-9]+)?", normalized) else None


def _remove_source_directory(source_dir: Path) -> None:
    for child in sorted(source_dir.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            child.rmdir()
    source_dir.rmdir()
