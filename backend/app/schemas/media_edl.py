"""受控短视频 EDL 渲染契约。

EDL 是媒体 Agent 的确定性 Tool 输入，不携带本机路径、FFmpeg 参数或任意滤镜。首版只允许
从同一受控媒体源按时间顺序挑选片段，模型后续只能提出候选片段，仍由 Harness 校验后执行。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


MAX_EDL_CLIPS = 8
MAX_EDL_OUTPUT_DURATION_MS = 180_000


class MediaEdlClip(BaseModel):
    """源媒体上的一个精确时间范围，单位固定为毫秒。"""

    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> "MediaEdlClip":
        if self.end_ms <= self.begin_ms:
            raise ValueError("EDL 片段结束时间必须晚于开始时间。")
        return self


class MediaEditDecisionList(BaseModel):
    """单源、顺序剪辑的受限 EDL；边界会在执行前再次对照媒体时长。"""

    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    clips: list[MediaEdlClip] = Field(min_length=1, max_length=MAX_EDL_CLIPS)

    @model_validator(mode="after")
    def validate_clip_order_and_budget(self) -> "MediaEditDecisionList":
        total_duration_ms = 0
        previous_end_ms = -1
        for clip in self.clips:
            if clip.begin_ms < previous_end_ms:
                raise ValueError("EDL 首版只支持按源时间顺序且不重叠的片段。")
            previous_end_ms = clip.end_ms
            total_duration_ms += clip.end_ms - clip.begin_ms
        if total_duration_ms > MAX_EDL_OUTPUT_DURATION_MS:
            raise ValueError("EDL 首版输出总时长不能超过 3 分钟。")
        return self

    @property
    def requested_duration_ms(self) -> int:
        return sum(clip.end_ms - clip.begin_ms for clip in self.clips)


class MediaEdlRenderInfo(BaseModel):
    """已回读视频交付的有限事实；磁盘路径仅保留在受控 Artifact 元数据中。"""

    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clip_count: int = Field(ge=1, le=MAX_EDL_CLIPS)
    requested_duration_ms: int = Field(ge=1, le=MAX_EDL_OUTPUT_DURATION_MS)
    rendered_duration_ms: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=256 * 1024 * 1024)
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)
    video_codec: str = Field(min_length=1, max_length=64)
    audio_codec: str = Field(min_length=1, max_length=64)
    created_at: str


class MediaEdlRenderStartResponse(BaseModel):
    task_id: str = Field(pattern=r"^task_media_edl_[0-9a-f]{12}$")
    status: Literal["queued"] = "queued"


class MediaEdlRenderTaskResultResponse(BaseModel):
    """查询一次 EDL 渲染任务的可恢复终态。"""

    task_id: str = Field(pattern=r"^task_media_edl_[0-9a-f]{12}$")
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    failure_reason: Literal[
        "validation_failed",
        "tool_execution_failed",
        "delivery_verification_failed",
        "cancelled",
        "unexpected",
    ] | None = None
    render: MediaEdlRenderInfo | None = None
