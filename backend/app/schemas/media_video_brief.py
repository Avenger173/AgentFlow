"""受控视频讲解网页的规划、交付与回读契约。

模型只可以概括已提供的转写句段并引用 ``sentence_id``。时间码、关键帧位置、HTML、
图片字节和输出路径均由服务端固定流程生成，不能由模型或客户端传入。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_VIDEO_BRIEF_CHAPTERS = 6
MAX_VIDEO_BRIEF_FACTS_PER_CHAPTER = 3

VideoBriefLayout = Literal["chapter", "evidence", "summary"]
VideoBriefAnimation = Literal["fade", "appear", "auto_animate"]


class MediaVideoBriefRequest(BaseModel):
    """用户明确请求一次基于既有转写的视频讲解交付。"""

    model_config = ConfigDict(extra="forbid")

    transcription_task_id: str = Field(pattern=r"^task_media_transcription_[0-9a-f]{12}$")
    goal: str = Field(min_length=2, max_length=1_200)

    @model_validator(mode="after")
    def normalize_goal(self) -> "MediaVideoBriefRequest":
        self.goal = " ".join(self.goal.split())
        if len(self.goal) < 2:
            raise ValueError("视频讲解目标不能为空。")
        return self


class MediaVideoBriefModelFact(BaseModel):
    """模型的一条概括，必须附着在当前章节给定的转写句段上。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=2, max_length=220)
    # 技术讲解中的一条结论可能跨越多个连续句段；这里限制的是可审计引用规模，
    # 不是模型输出质量。过低上限会错误拒绝真实长口播的保守概括。
    sentence_ids: list[int] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def normalize_text(self) -> "MediaVideoBriefModelFact":
        self.text = " ".join(self.text.split())
        if len(self.text) < 2:
            raise ValueError("讲解事实不能为空。")
        self.sentence_ids = list(dict.fromkeys(self.sentence_ids))
        return self


class MediaVideoBriefModelChapter(BaseModel):
    """模型只能选择句段、受限版式和受限动效。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=96)
    sentence_ids: list[int] = Field(min_length=1, max_length=36)
    facts: list[MediaVideoBriefModelFact] = Field(min_length=1, max_length=MAX_VIDEO_BRIEF_FACTS_PER_CHAPTER)
    layout: VideoBriefLayout = "chapter"
    animation: VideoBriefAnimation = "appear"

    @model_validator(mode="after")
    def normalize_chapter(self) -> "MediaVideoBriefModelChapter":
        self.title = " ".join(self.title.split())
        if len(self.title) < 2:
            raise ValueError("讲解章节标题不能为空。")
        self.sentence_ids = list(dict.fromkeys(self.sentence_ids))
        allowed_ids = set(self.sentence_ids)
        if any(not set(fact.sentence_ids).issubset(allowed_ids) for fact in self.facts):
            raise ValueError("讲解事实只能引用本章节选择的转写句段。")
        return self


class MediaVideoBriefModelPlan(BaseModel):
    """一次无工具模型调用的严格输出，不含任何时间码、文件或网页代码。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["brief", "clarify"]
    title: str = Field(default="", max_length=120)
    chapters: list[MediaVideoBriefModelChapter] = Field(default_factory=list, max_length=MAX_VIDEO_BRIEF_CHAPTERS)
    clarification_question: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def validate_action_shape(self) -> "MediaVideoBriefModelPlan":
        self.title = " ".join(self.title.split())
        self.clarification_question = " ".join(self.clarification_question.split())
        if self.action == "brief" and (len(self.title) < 2 or not self.chapters or self.clarification_question):
            raise ValueError("讲解计划必须包含标题和章节，且不能同时请求澄清。")
        if self.action == "clarify" and (self.title or self.chapters or not self.clarification_question):
            raise ValueError("澄清结果只能包含一个问题。")
        return self


class MediaVideoBriefFactInfo(BaseModel):
    text: str = Field(min_length=2, max_length=220)
    sentence_ids: list[int] = Field(min_length=1, max_length=12)
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> "MediaVideoBriefFactInfo":
        if self.end_ms <= self.begin_ms:
            raise ValueError("讲解事实的来源时间范围无效。")
        return self


class MediaVideoBriefChapterInfo(BaseModel):
    chapter_id: str = Field(pattern=r"^chapter_[1-6]$")
    title: str = Field(min_length=2, max_length=96)
    sentence_ids: list[int] = Field(min_length=1, max_length=36)
    begin_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)
    keyframe_sentence_id: int = Field(ge=0)
    keyframe_timestamp_ms: int = Field(ge=0)
    facts: list[MediaVideoBriefFactInfo] = Field(min_length=1, max_length=MAX_VIDEO_BRIEF_FACTS_PER_CHAPTER)
    layout: VideoBriefLayout
    animation: VideoBriefAnimation

    @model_validator(mode="after")
    def validate_chapter_range(self) -> "MediaVideoBriefChapterInfo":
        if self.end_ms <= self.begin_ms:
            raise ValueError("讲解章节的来源时间范围无效。")
        if not self.begin_ms <= self.keyframe_timestamp_ms <= self.end_ms:
            raise ValueError("关键帧时间必须位于章节来源范围内。")
        return self


class MediaVideoBriefPlanInfo(BaseModel):
    source_id: str = Field(pattern=r"^ms_[0-9a-f]{16}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcription_task_id: str = Field(pattern=r"^task_media_transcription_[0-9a-f]{12}$")
    goal: str = Field(min_length=2, max_length=1_200)
    title: str = Field(min_length=2, max_length=120)
    chapters: list[MediaVideoBriefChapterInfo] = Field(min_length=1, max_length=MAX_VIDEO_BRIEF_CHAPTERS)


class MediaVideoBriefKeyframeInfo(BaseModel):
    chapter_id: str = Field(pattern=r"^chapter_[1-6]$")
    timestamp_ms: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=12 * 1024 * 1024)
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)
    mime_type: Literal["image/jpeg"] = "image/jpeg"


class MediaVideoBriefDeliveryInfo(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=32 * 1024 * 1024)
    keyframes: list[MediaVideoBriefKeyframeInfo] = Field(min_length=1, max_length=MAX_VIDEO_BRIEF_CHAPTERS)
    reveal_version: Literal["6.0.1"] = "6.0.1"
    created_at: str


class MediaVideoBriefStartResponse(BaseModel):
    task_id: str = Field(pattern=r"^task_media_video_brief_[0-9a-f]{12}$")
    status: Literal["queued"] = "queued"


class MediaVideoBriefTaskResultResponse(BaseModel):
    task_id: str = Field(pattern=r"^task_media_video_brief_[0-9a-f]{12}$")
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    summary: str
    message: str
    failure_reason: Literal[
        "validation_failed",
        "provider_outcome_unknown",
        "contract_failed",
        "tool_execution_failed",
        "delivery_verification_failed",
        "cancelled",
        "unexpected",
    ] | None = None
    plan: MediaVideoBriefPlanInfo | None = None
    delivery: MediaVideoBriefDeliveryInfo | None = None
    artifact_id: str | None = Field(default=None, pattern=r"^artifact_media_video_brief_[0-9a-f]{12}$")
    clarification_question: str | None = None
