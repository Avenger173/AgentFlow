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


class MediaEdlCandidateRequest(BaseModel):
    """Request a candidate EDL from one already-verified transcription task."""

    transcription_task_id: str = Field(pattern=r"^task_media_transcription_[0-9a-f]{12}$")
    # 继续修改只引用同一受控转写产生的旧候选；模型仍只能从本轮受限句段中重新选择。
    parent_candidate_task_id: str | None = Field(
        default=None,
        pattern=r"^task_media_edl_plan_[0-9a-f]{12}$",
    )
    goal: str = Field(min_length=2, max_length=1_200)

    @model_validator(mode="after")
    def normalize_goal(self) -> "MediaEdlCandidateRequest":
        self.goal = " ".join(self.goal.split())
        if len(self.goal) < 2:
            raise ValueError("剪辑目标不能为空。")
        return self


class MediaEdlModelSelection(BaseModel):
    """The planning model may reference only sentence IDs from the supplied transcript."""

    start_sentence_id: int = Field(ge=0)
    end_sentence_id: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_sentence_order(self) -> "MediaEdlModelSelection":
        if self.end_sentence_id < self.start_sentence_id:
            raise ValueError("候选片段结束句段不能早于开始句段。")
        self.reason = " ".join(self.reason.split())
        if not self.reason:
            raise ValueError("候选片段需要简短理由。")
        return self


class MediaEdlModelCandidate(BaseModel):
    """Strict model-only output; source IDs and millisecond ranges are Harness-owned."""

    action: Literal["candidate", "clarify"]
    selections: list[MediaEdlModelSelection] = Field(default_factory=list, max_length=MAX_EDL_CLIPS)
    clarification_question: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def validate_action_shape(self) -> "MediaEdlModelCandidate":
        self.clarification_question = " ".join(self.clarification_question.split())
        if self.action == "candidate" and not self.selections:
            raise ValueError("候选剪辑至少需要一个句段范围。")
        if self.action == "candidate" and self.clarification_question:
            raise ValueError("候选剪辑不能同时要求澄清。")
        if self.action == "clarify" and (self.selections or not self.clarification_question):
            raise ValueError("澄清结果只能包含问题，不能包含片段。")
        return self


class MediaEdlCandidateSelection(BaseModel):
    start_sentence_id: int = Field(ge=0)
    end_sentence_id: int = Field(ge=0)
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_time_range(self) -> "MediaEdlCandidateSelection":
        if self.end_ms <= self.begin_ms:
            raise ValueError("候选片段结束时间必须晚于开始时间。")
        return self


class MediaEdlCandidateInfo(BaseModel):
    """A reviewable candidate. Rendering always needs a separate explicit request."""

    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    transcription_task_id: str = Field(pattern=r"^task_media_transcription_[0-9a-f]{12}$")
    parent_candidate_task_id: str | None = Field(
        default=None,
        pattern=r"^task_media_edl_plan_[0-9a-f]{12}$",
    )
    goal: str = Field(min_length=2, max_length=1_200)
    selections: list[MediaEdlCandidateSelection] = Field(min_length=1, max_length=MAX_EDL_CLIPS)
    edl: MediaEditDecisionList
    target_min_duration_ms: int = Field(default=1, ge=1, le=MAX_EDL_OUTPUT_DURATION_MS)
    target_max_duration_ms: int = Field(default=MAX_EDL_OUTPUT_DURATION_MS, ge=1, le=MAX_EDL_OUTPUT_DURATION_MS)
    duration_adjusted: bool = False
    requires_confirmation: Literal[True] = True

    @model_validator(mode="after")
    def validate_duration_target(self) -> "MediaEdlCandidateInfo":
        if self.target_min_duration_ms > self.target_max_duration_ms:
            raise ValueError("候选剪辑的目标时长范围无效。")
        if not self.target_min_duration_ms <= self.edl.requested_duration_ms <= self.target_max_duration_ms:
            raise ValueError("候选剪辑时长不满足其目标范围。")
        return self


class MediaEdlCandidateStartResponse(BaseModel):
    task_id: str = Field(pattern=r"^task_media_edl_plan_[0-9a-f]{12}$")
    status: Literal["queued"] = "queued"


class MediaEdlCandidateTaskResultResponse(BaseModel):
    task_id: str = Field(pattern=r"^task_media_edl_plan_[0-9a-f]{12}$")
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    failure_reason: Literal[
        "validation_failed",
        "provider_rejected",
        "provider_outcome_unknown",
        "contract_failed",
        "cancelled",
        "unexpected",
    ] | None = None
    candidate: MediaEdlCandidateInfo | None = None
    clarification_question: str | None = None
