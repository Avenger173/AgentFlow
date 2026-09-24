"""音视频源文件、探测结果、ASR 规范化音频与转写交付契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class MediaSourceImportRequest(BaseModel):
    """客户端只能上传媒体名称与 Base64 内容，不能夹带本机路径。"""

    filename: str = Field(min_length=1, max_length=180)
    # 256 MiB 原始文件的严格 Base64 上限，再加上极小的编码余量。
    content_base64: str = Field(min_length=4, max_length=357_913_950)


class MediaTranscriptionPreparationResponse(BaseModel):
    source: MediaSourceInfo
    audio: MediaTranscriptionAudioInfo


class MediaTranscriptionRequest(BaseModel):
    """一次已经由用户提交的短媒体转写请求。"""

    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    audio_id: str = Field(pattern=r"^mda_[0-9a-f]{16}$")
    language_hints: list[str] = Field(default_factory=list, max_length=4)
    speaker_diarization: bool = False

    @model_validator(mode="after")
    def normalize_language_hints(self) -> "MediaTranscriptionRequest":
        normalized = list(dict.fromkeys(" ".join(item.split()).lower() for item in self.language_hints if item.strip()))
        if any(len(item) > 12 for item in normalized):
            raise ValueError("语音转写语言提示格式不正确。")
        self.language_hints = normalized
        return self


class MediaTranscriptionStartResponse(BaseModel):
    task_id: str = Field(pattern=r"^task_media_transcription_[0-9a-f]{12}$")
    status: Literal["queued"] = "queued"


class MediaTranscriptionWordInfo(BaseModel):
    text: str = Field(min_length=1, max_length=160)
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    punctuation: str = Field(default="", max_length=8)

    @model_validator(mode="after")
    def validate_range(self) -> "MediaTranscriptionWordInfo":
        if self.end_ms < self.begin_ms:
            raise ValueError("词级时间戳结束时间不能早于开始时间。")
        return self


class MediaTranscriptionSegmentInfo(BaseModel):
    sentence_id: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4_000)
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker_id: int | None = Field(default=None, ge=0)
    words: list[MediaTranscriptionWordInfo] = Field(default_factory=list, max_length=2_000)

    @model_validator(mode="after")
    def validate_range(self) -> "MediaTranscriptionSegmentInfo":
        if self.end_ms < self.begin_ms:
            raise ValueError("句级时间戳结束时间不能早于开始时间。")
        return self


class MediaTranscriptInfo(BaseModel):
    text: str = Field(min_length=1, max_length=120_000)
    segments: list[MediaTranscriptionSegmentInfo] = Field(min_length=1, max_length=10_000)


class MediaTranscriptionArtifactPayload(BaseModel):
    """磁盘转写 JSON 的内部回读契约，不保存音频正文或 Provider 原始响应。"""

    schema_version: Literal[1] = 1
    kind: Literal["media_transcription"] = "media_transcription"
    task_id: str = Field(pattern=r"^task_media_transcription_[0-9a-f]{12}$")
    project_id: str = Field(pattern=r"^mp_[0-9a-f]{16}$")
    request: MediaTranscriptionRequest
    audio: MediaTranscriptionAudioInfo
    transcript: MediaTranscriptInfo
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)
    provider_request_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_usage: dict[str, int | bool | None] = Field(default_factory=dict)
    created_at: str


class MediaTranscriptionTaskResultResponse(BaseModel):
    """转写任务的可恢复查询结果。"""

    task_id: str = Field(pattern=r"^task_media_transcription_[0-9a-f]{12}$")
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    failure_reason: Literal[
        "provider_rejected",
        "provider_outcome_unknown",
        "validation_failed",
        "delivery_verification_failed",
        "cancelled",
        "unexpected",
    ] | None = None
    transcript: MediaTranscriptInfo | None = None
    artifact_id: str | None = Field(default=None, pattern=r"^artifact_media_transcription_[0-9a-f]{12}$")
