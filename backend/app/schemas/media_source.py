"""音视频源文件、探测结果与 ASR 规范化音频的内部契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MediaSourceMimeType = Literal[
    "audio/wav",
    "audio/mpeg",
    "audio/mp4",
    "video/mp4",
    "video/quicktime",
    "video/x-matroska",
    "video/webm",
]


class MediaSourceInfo(BaseModel):
    """一个受控原始媒体副本；响应中不暴露本机路径或媒体正文。"""

    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    project_scope: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=180)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: MediaSourceMimeType
    size_bytes: int = Field(ge=1)
    created_at: str


class MediaAudioStreamInfo(BaseModel):
    stream_index: int = Field(ge=0)
    codec_name: str = Field(min_length=1, max_length=64)
    sample_rate: int | None = Field(default=None, ge=1, le=384_000)
    channels: int | None = Field(default=None, ge=1, le=64)
    language: str | None = Field(default=None, max_length=24)


class MediaVideoStreamInfo(BaseModel):
    stream_index: int = Field(ge=0)
    codec_name: str = Field(min_length=1, max_length=64)
    width: int | None = Field(default=None, ge=1, le=16_384)
    height: int | None = Field(default=None, ge=1, le=16_384)
    average_frame_rate: str | None = Field(default=None, max_length=32)


class MediaProbeInfo(BaseModel):
    """从 ffprobe 提取的有限事实，供后续转写与 EDL 校验使用。"""

    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_format: str = Field(min_length=1, max_length=160)
    duration_seconds: float | None = Field(default=None, ge=0)
    audio_streams: list[MediaAudioStreamInfo] = Field(default_factory=list)
    video_streams: list[MediaVideoStreamInfo] = Field(default_factory=list)
    probed_at: str


class MediaTranscriptionAudioInfo(BaseModel):
    """为 ASR 固定为单声道 16 kHz PCM WAV 的受控派生文件。"""

    audio_id: str = Field(pattern=r"^mda_[0-9a-f]{16}$")
    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_stream_index: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=7 * 1024 * 1024)
    duration_seconds: float = Field(ge=0)
    sample_rate: Literal[16_000] = 16_000
    channels: Literal[1] = 1
    created_at: str
