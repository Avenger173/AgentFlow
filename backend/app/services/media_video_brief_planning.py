"""模型辅助的视频讲解计划。

模型只在已经验证的转写中选择句段并给出简短概括。时间码、关键帧和 HTML 不属于模型
输出，由下面的 Harness 统一绑定，避免模型将网页代码或本机路径带入交付链。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import ValidationError

from app.schemas.media_source import MediaTranscriptionArtifactPayload, MediaTranscriptionSegmentInfo
from app.schemas.media_video_brief import (
    MAX_VIDEO_BRIEF_CHAPTERS,
    MediaVideoBriefChapterInfo,
    MediaVideoBriefFactInfo,
    MediaVideoBriefModelChapter,
    MediaVideoBriefModelPlan,
    MediaVideoBriefPlanInfo,
    MediaVideoBriefRequest,
)
from app.services.media_source_preparation import MediaSourcePreparationError, get_media_source
from app.services.media_transcript_timing import normalize_cumulative_transcript_spans
from app.services.media_transcription_delivery import load_verified_media_transcription_payload
from app.services.model_gateway import ModelRuntime


MAX_VIDEO_BRIEF_PLANNING_SEGMENTS = 320
MAX_VIDEO_BRIEF_PLANNING_CHARACTERS = 32_000

# 只把用户明确写出的章节数量转成硬约束，避免从普通描述中臆测篇幅。
_CHINESE_CHAPTER_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_EXPLICIT_CHAPTER_RANGE = re.compile(
    r"(?:整理(?:为|成)?|制作(?:为|成)?|生成(?:为|成)?|输出(?:为|成)?|概括(?:为|成)?|分(?:为|成)?|按)\s*"
    r"([1-6一二三四五六])\s*(?:[-~～至到]\s*([1-6一二三四五六]))?\s*(?:个)?(?:章节|章)"
)

Planner = Callable[..., Awaitable[MediaVideoBriefModelPlan]]
TranscriptLoader = Callable[..., MediaTranscriptionArtifactPayload]


class MediaVideoBriefPlanningError(ValueError):
    """可以安全展示给用户的讲解规划错误。"""


@dataclass(frozen=True)
class MediaVideoBriefPlanningContext:
    project_id: str
    request: MediaVideoBriefRequest
    source_id: str
    source_sha256: str
    segments: tuple[MediaTranscriptionSegmentInfo, ...]
    requested_chapter_min: int = 1
    requested_chapter_max: int = 6


def load_media_video_brief_planning_context(
    *,
    project_id: str,
    request: MediaVideoBriefRequest,
    transcript_loader: TranscriptLoader = load_verified_media_transcription_payload,
) -> MediaVideoBriefPlanningContext:
    """将一份完成转写严格绑定回同项目的受控视频素材。"""

    payload = transcript_loader(
        task_id=request.transcription_task_id,
        expected_project_id=project_id,
    )
    if payload.project_id != project_id:
        raise MediaVideoBriefPlanningError("已验证转写不属于当前视频项目。")
    try:
        source = get_media_source(payload.request.source_id, expected_project_scope=project_id)
    except MediaSourcePreparationError as exc:
        raise MediaVideoBriefPlanningError(str(exc)) from exc
    if payload.audio.source_sha256 != source.source_sha256:
        raise MediaVideoBriefPlanningError("已验证转写与当前受控视频哈希不匹配。")

    ordered = tuple(sorted(payload.transcript.segments, key=lambda item: (item.begin_ms, item.end_ms, item.sentence_id)))
    normalized = tuple(
        ordered[span.source_index].model_copy(
            update={"text": span.text, "begin_ms": span.begin_ms, "end_ms": span.end_ms, "words": []}
        )
        for span in normalize_cumulative_transcript_spans(ordered)
        if span.end_ms > span.begin_ms and span.text.strip()
    )
    if not normalized:
        raise MediaVideoBriefPlanningError("已验证转写没有可用于视频讲解的有效句段。")
    if len(normalized) > MAX_VIDEO_BRIEF_PLANNING_SEGMENTS:
        raise MediaVideoBriefPlanningError("当前视频讲解最多支持 320 个转写句段，请先截取更短的素材。")
    if sum(len(item.text) for item in normalized) > MAX_VIDEO_BRIEF_PLANNING_CHARACTERS:
        raise MediaVideoBriefPlanningError("当前转写上下文超过视频讲解上限，请先截取更短的素材。")
    if len({item.sentence_id for item in normalized}) != len(normalized):
        raise MediaVideoBriefPlanningError("已验证转写存在重复句段标识，无法安全生成视频讲解。")
    requested_chapter_min, requested_chapter_max = _resolve_requested_chapter_range(request.goal)
    return MediaVideoBriefPlanningContext(
        project_id=project_id,
        request=request,
        source_id=source.source_id,
        source_sha256=source.source_sha256,
        segments=normalized,
        requested_chapter_min=requested_chapter_min,
        requested_chapter_max=requested_chapter_max,
    )


async def generate_media_video_brief_model_plan(
    *, runtime: ModelRuntime, context: MediaVideoBriefPlanningContext
) -> MediaVideoBriefModelPlan:
    """只发起一次受限 JSON 规划调用；调用方控制重试和交付。"""

    payload = {
        "goal": context.request.goal,
        "chapter_count_range": {
            "min": context.requested_chapter_min,
            "max": context.requested_chapter_max,
        },
        "segments": [
            {"sentence_id": item.sentence_id, "text": item.text}
            for item in context.segments
        ],
    }
    content = await runtime.chat_json(
        system_prompt=build_media_video_brief_planning_system_prompt(),
        user_message=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        maximum_tokens=1_000,
    )
    return parse_media_video_brief_model_plan(content)


def parse_media_video_brief_model_plan(content: str) -> MediaVideoBriefModelPlan:
    """解析完整计划，兼容少数 Provider 只返回受限章节结构的 JSON mode 输出。"""

    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character not in "{[":
            continue
        try:
            raw, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(raw, list):
            return _normalize_chapter_only_output(raw)
        if not isinstance(raw, dict):
            raise MediaVideoBriefPlanningError("模型讲解计划必须返回 JSON object 或受限章节数组。")
        try:
            return MediaVideoBriefModelPlan.model_validate(raw)
        except ValidationError as exc:
            try:
                chapter = MediaVideoBriefModelChapter.model_validate(raw)
            except ValidationError:
                raise MediaVideoBriefPlanningError("模型讲解计划未通过固定契约校验。") from exc
            return _plan_from_chapters([chapter])
    raise MediaVideoBriefPlanningError("模型没有返回合法的视频讲解 JSON。")


def _normalize_chapter_only_output(raw: list[object]) -> MediaVideoBriefModelPlan:
    """仅兼容严格章节数组，不能把任意 JSON list 降级为可交付计划。"""

    if not raw:
        raise MediaVideoBriefPlanningError("模型返回的章节列表不能为空。")
    try:
        chapters = [MediaVideoBriefModelChapter.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise MediaVideoBriefPlanningError("模型讲解章节列表未通过固定契约校验。") from exc
    return _plan_from_chapters(chapters)


def _plan_from_chapters(chapters: list[MediaVideoBriefModelChapter]) -> MediaVideoBriefModelPlan:
    """Provider 遗漏外层壳时，复用首章标题而不引入另一轮模型总结。"""

    if not chapters:
        raise MediaVideoBriefPlanningError("模型讲解章节列表不能为空。")
    return MediaVideoBriefModelPlan(
        action="brief",
        title=chapters[0].title,
        chapters=chapters,
        clarification_question="",
    )


def build_media_video_brief_plan(
    *, context: MediaVideoBriefPlanningContext, model_plan: MediaVideoBriefModelPlan
) -> tuple[MediaVideoBriefPlanInfo | None, str | None]:
    """将句段引用映射为唯一的来源时间码和关键帧位置。"""

    if model_plan.action == "clarify":
        return None, model_plan.clarification_question
    chapter_count = len(model_plan.chapters)
    if not context.requested_chapter_min <= chapter_count <= context.requested_chapter_max:
        raise MediaVideoBriefPlanningError(
            f"模型返回 {chapter_count} 章，不符合用户要求的 "
            f"{context.requested_chapter_min}-{context.requested_chapter_max} 章。"
        )
    by_sentence_id = {item.sentence_id: item for item in context.segments}
    chapters: list[MediaVideoBriefChapterInfo] = []
    for raw_chapter in model_plan.chapters:
        source_segments = _resolve_segments(raw_chapter.sentence_ids, by_sentence_id)
        facts: list[MediaVideoBriefFactInfo] = []
        for raw_fact in raw_chapter.facts:
            fact_segments = _resolve_segments(raw_fact.sentence_ids, by_sentence_id)
            facts.append(
                MediaVideoBriefFactInfo(
                    text=raw_fact.text,
                    sentence_ids=[item.sentence_id for item in fact_segments],
                    begin_ms=fact_segments[0].begin_ms,
                    end_ms=fact_segments[-1].end_ms,
                )
            )
        keyframe_segment = source_segments[len(source_segments) // 2]
        chapters.append(
            MediaVideoBriefChapterInfo(
                chapter_id="chapter_1",
                title=raw_chapter.title,
                sentence_ids=[item.sentence_id for item in source_segments],
                begin_ms=source_segments[0].begin_ms,
                end_ms=source_segments[-1].end_ms,
                keyframe_sentence_id=keyframe_segment.sentence_id,
                keyframe_timestamp_ms=keyframe_segment.begin_ms
                + (keyframe_segment.end_ms - keyframe_segment.begin_ms) // 2,
                facts=facts,
                layout=raw_chapter.layout,
                animation=raw_chapter.animation,
            )
        )
    # 时间顺序由 Harness 决定，模型的叙事顺序不能造成虚假的视频时间线。
    chapters.sort(key=lambda item: (item.begin_ms, item.end_ms, item.title))
    normalized_chapters = [
        chapter.model_copy(update={"chapter_id": f"chapter_{index}"})
        for index, chapter in enumerate(chapters, start=1)
    ]
    try:
        return (
            MediaVideoBriefPlanInfo(
                source_id=context.source_id,
                source_sha256=context.source_sha256,
                transcription_task_id=context.request.transcription_task_id,
                goal=context.request.goal,
                title=model_plan.title,
                chapters=normalized_chapters,
            ),
            None,
        )
    except ValueError as exc:
        raise MediaVideoBriefPlanningError("视频讲解计划未通过时间证据校验。") from exc


def _resolve_segments(
    sentence_ids: list[int],
    by_sentence_id: dict[int, MediaTranscriptionSegmentInfo],
) -> list[MediaTranscriptionSegmentInfo]:
    resolved: list[MediaTranscriptionSegmentInfo] = []
    for sentence_id in sentence_ids:
        segment = by_sentence_id.get(sentence_id)
        if segment is None:
            raise MediaVideoBriefPlanningError("模型引用了当前转写中不存在的句段。")
        resolved.append(segment)
    resolved.sort(key=lambda item: (item.begin_ms, item.end_ms, item.sentence_id))
    if not resolved or resolved[-1].end_ms <= resolved[0].begin_ms:
        raise MediaVideoBriefPlanningError("模型引用的句段无法形成有效讲解来源时间。")
    return resolved


def _resolve_requested_chapter_range(goal: str) -> tuple[int, int]:
    """仅接受用户明确给出的“整理为三到五章”式范围。"""

    match = _EXPLICIT_CHAPTER_RANGE.search(goal)
    if match is None:
        return 1, MAX_VIDEO_BRIEF_CHAPTERS
    minimum = _chapter_number_value(match.group(1))
    maximum = _chapter_number_value(match.group(2)) if match.group(2) else minimum
    if minimum > maximum:
        raise MediaVideoBriefPlanningError("视频讲解目标中的章节范围无效。")
    return minimum, maximum


def _chapter_number_value(raw: str) -> int:
    return _CHINESE_CHAPTER_NUMBERS.get(raw, int(raw) if raw.isascii() else 0)


def build_media_video_brief_planning_system_prompt() -> str:
    return (
        "你是 AgentFlow 的受限视频讲解规划器。只返回一个 JSON 对象，不要 Markdown、解释、推理过程或额外字段。"
        "返回内容的首字符必须是 {，根对象必须同时包含 action、title、chapters、clarification_question 四个字段。"
        "你只能根据用户目标和给定的转写句段概括视频内容；不能读取视频、不能假设画面细节、不能调用工具。"
        "只能引用给定的 sentence_id，不能输出毫秒时间、文件路径、图片、HTML、CSS、JavaScript、模型名、费用或新素材。"
        "每个 fact 都必须是其 sentence_ids 中原话的保守概括，不能加入未提供的事实；章节最多 6 个、每章最多 3 个事实。"
        "用户消息中的 chapter_count_range 是硬约束；当它给出最小和最大章节数时，brief 必须返回该闭区间内的章节数。"
        "目标不清晰或转写不够支持时只请求澄清。交付会由系统从句段自动生成关键帧和离线网页。\n"
        "当 action 为 brief 时，使用这个完整形状："
        '{"action":"brief","title":"简短总标题","chapters":[{"title":"章节标题","sentence_ids":[0],"facts":[{"text":"保守概括","sentence_ids":[0]}],"layout":"chapter","animation":"appear"}],"clarification_question":""}'
        "。当 action 为 clarify 时，title 必须为空、chapters 必须为 []，并只填写 clarification_question。"
    )
