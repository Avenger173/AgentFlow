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

from app.core.config import settings
from app.schemas.media_source import (
    MediaAudioStreamInfo,
    MediaProbeInfo,
    MediaSourceInfo,
    MediaSourceMimeType,
    MediaTranscriptionAudioInfo,
    MediaVideoStreamInfo,
)


MAX_MEDIA_SOURCE_BYTES = 256 * 1024 * 1024
MAX_TRANSCRIPTION_AUDIO_BYTES = 7 * 1024 * 1024
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
        audio_id = _new_id("mda")
        derived_dir = source_dir / "derived"
        derived_dir.mkdir(exist_ok=True)
        temporary_path = _resolve_source_file(derived_dir, f"{audio_id}.tmp.wav")
        final_relative = f"derived/{audio_id}.wav"
        final_path = _resolve_source_file(source_dir, final_relative)
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
        try:
            audio = _verify_transcription_wav(
                path=temporary_path,
                audio_id=audio_id,
                source_id=source_id,
                source_sha256=manifest["source_sha256"],
                source_stream_index=audio_stream.stream_index,
            )
            os.replace(temporary_path, final_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        record = audio.model_dump(mode="json") | {"file": final_relative}
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
    """供后续 ASR Tool 读取已经回读过的派生 WAV，不暴露其存储路径。"""

    source_dir, manifest = _load_source_manifest(source_id, root_dir=root_dir)
    _require_project_scope(manifest, expected_project_scope)
    _validate_id(audio_id, _DERIVED_AUDIO_ID_PATTERN, "转写音频")
    for record in manifest["derived_audio"]:
        if record.get("audio_id") != audio_id:
            continue
        audio = _derived_audio_info(record)
        path = _resolve_source_file(source_dir, str(record.get("file", "")))
        if not path.is_file() or _sha256_file(path) != audio.sha256:
            raise MediaSourcePreparationError("转写音频不存在或已被修改，需要重新提取。")
        _verify_transcription_wav(
            path=path,
            audio_id=audio.audio_id,
            source_id=audio.source_id,
            source_sha256=audio.source_sha256,
            source_stream_index=audio.source_stream_index,
        )
        return audio, path.read_bytes()
    raise MediaSourcePreparationError("未找到指定的受控转写音频。")


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
        path = _resolve_source_file(source_dir, str(record.get("file", "")))
        if not path.is_file() or _sha256_file(path) != audio.sha256:
            continue
        try:
            _verify_transcription_wav(
                path=path,
                audio_id=audio.audio_id,
                source_id=audio.source_id,
                source_sha256=audio.source_sha256,
                source_stream_index=audio.source_stream_index,
            )
        except MediaSourcePreparationError:
            continue
        return audio
    return None


def _verify_transcription_wav(
    *,
    path: Path,
    audio_id: str,
    source_id: str,
    source_sha256: str,
    source_stream_index: int,
) -> MediaTranscriptionAudioInfo:
    if not path.is_file():
        raise MediaToolExecutionError("ffmpeg 未生成可回读的转写音频。")
    size_bytes = path.stat().st_size
    if not 0 < size_bytes <= MAX_TRANSCRIPTION_AUDIO_BYTES:
        raise MediaSourcePreparationError("规范化转写音频超过 7 MB 上限，需要先切分媒体。")
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
    return MediaTranscriptionAudioInfo(
        audio_id=audio_id,
        source_id=source_id,
        source_sha256=source_sha256,
        source_stream_index=source_stream_index,
        sha256=_sha256_file(path),
        size_bytes=size_bytes,
        duration_seconds=round(frames / float(sample_rate), 3),
        created_at=_utc_now(),
    )


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
