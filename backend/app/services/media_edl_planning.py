"""Model-assisted, confirmation-only EDL candidate planning.

The model receives a bounded transcript view and can reference only supplied sentence
IDs.  This module never receives local paths, FFmpeg arguments, or permission to
render media; the Harness maps valid IDs back to a constrained EDL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
import re
from typing import Awaitable, Callable

from pydantic import ValidationError

from app.schemas.media_edl import (
    MediaEdlCandidateInfo,
    MediaEdlCandidateRequest,
    MediaEdlCandidateSelection,
    MediaEdlModelCandidate,
    MediaEditDecisionList,
    MediaEdlClip,
    MAX_EDL_OUTPUT_DURATION_MS,
)
from app.schemas.media_source import MediaTranscriptionArtifactPayload, MediaTranscriptionSegmentInfo
from app.services.media_source_preparation import MediaSourcePreparationError, get_media_source
from app.services.media_transcription_delivery import load_verified_media_transcription_payload
from app.services.media_transcript_timing import normalize_cumulative_transcript_spans
from app.services.model_gateway import ModelRuntime


MAX_EDL_PLANNING_SEGMENTS = 320
MAX_EDL_PLANNING_CHARACTERS = 32_000
Planner = Callable[..., Awaitable[MediaEdlModelCandidate]]
TranscriptLoader = Callable[..., MediaTranscriptionArtifactPayload]
_DURATION_VALUE = r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十两])"
_DURATION_UNIT = r"(?:秒(?:钟)?|s(?:ec(?:onds?)?)?|分钟|分(?:钟)?|min(?:utes?)?)"
_DURATION_RANGE = re.compile(
    rf"(?P<minimum>{_DURATION_VALUE})\s*(?P<minimum_unit>{_DURATION_UNIT})?\s*"
    rf"(?:到|至|[-~～—])\s*(?P<maximum>{_DURATION_VALUE})\s*(?P<maximum_unit>{_DURATION_UNIT})",
    re.IGNORECASE,
)
_DURATION_CEILING = re.compile(
    rf"(?P<maximum>{_DURATION_VALUE})\s*(?P<unit>{_DURATION_UNIT})\s*(?:以内|之内|以内|内|以下)",
    re.IGNORECASE,
)
_DURATION_CEILING_PREFIX = re.compile(
    rf"(?:控制在|限制在|不超过|不多于|至多|最多|小于|少于)\s*"
    rf"(?P<maximum>{_DURATION_VALUE})\s*(?P<unit>{_DURATION_UNIT})",
    re.IGNORECASE,
)
_CHINESE_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


class MediaEdlPlanningError(ValueError):
    """A safe candidate-planning failure which never exposes transcript text or paths."""


@dataclass(frozen=True)
class MediaEdlDurationConstraint:
    """A deterministic duration requirement extracted from the user goal."""

    minimum_duration_ms: int = 1
    maximum_duration_ms: int = MAX_EDL_OUTPUT_DURATION_MS

    @property
    def label(self) -> str:
        if self.minimum_duration_ms <= 1:
            return f"不超过 {_format_duration(self.maximum_duration_ms)}"
        return f"{_format_duration(self.minimum_duration_ms)} 到 {_format_duration(self.maximum_duration_ms)}"


_DEFAULT_DURATION_CONSTRAINT = MediaEdlDurationConstraint()


@dataclass(frozen=True)
class MediaEdlPlanningContext:
    project_id: str
    request: MediaEdlCandidateRequest
    source_id: str
    source_sha256: str
    segments: tuple[MediaTranscriptionSegmentInfo, ...]
    duration_constraint: MediaEdlDurationConstraint = _DEFAULT_DURATION_CONSTRAINT


def load_media_edl_planning_context(
    *,
    project_id: str,
    request: MediaEdlCandidateRequest,
    transcript_loader: TranscriptLoader = load_verified_media_transcription_payload,
) -> MediaEdlPlanningContext:
    """Bind one completed transcript to its original controlled media source."""

    try:
        payload = transcript_loader(task_id=request.transcription_task_id, expected_project_id=project_id)
        source = get_media_source(payload.request.source_id, expected_project_scope=project_id)
    except MediaSourcePreparationError as exc:
        raise MediaEdlPlanningError(str(exc)) from exc
    if source.source_sha256 != payload.audio.source_sha256:
        raise MediaEdlPlanningError("媒体源哈希与已验证转写交付不一致。")
    duration_constraint = parse_media_edl_duration_constraint(request.goal)
    if duration_constraint.minimum_duration_ms > MAX_EDL_OUTPUT_DURATION_MS:
        raise MediaEdlPlanningError(
            f"剪辑目标要求至少 {_format_duration(duration_constraint.minimum_duration_ms)}，"
            "当前短视频剪辑最多支持 3 分钟。"
        )
    raw_segments = tuple(sorted(payload.transcript.segments, key=lambda item: (item.begin_ms, item.end_ms, item.sentence_id)))
    segments = tuple(
        raw_segments[span.source_index].model_copy(
            update={"text": span.text, "begin_ms": span.begin_ms, "end_ms": span.end_ms, "words": []}
        )
        for span in normalize_cumulative_transcript_spans(raw_segments)
    )
    if not segments:
        raise MediaEdlPlanningError("已验证转写没有可用于候选剪辑的句段。")
    if len(segments) > MAX_EDL_PLANNING_SEGMENTS:
        raise MediaEdlPlanningError("当前候选剪辑只支持最多 320 个转写句段，长媒体尚未实现。")
    if len({segment.sentence_id for segment in segments}) != len(segments):
        raise MediaEdlPlanningError("已验证转写存在重复句段标识，无法安全生成候选剪辑。")
    if sum(len(segment.text) for segment in segments) > MAX_EDL_PLANNING_CHARACTERS:
        raise MediaEdlPlanningError("当前转写上下文超过候选剪辑上限，长媒体尚未实现。")
    return MediaEdlPlanningContext(
        project_id=project_id,
        request=request,
        source_id=source.source_id,
        source_sha256=source.source_sha256,
        segments=segments,
        duration_constraint=duration_constraint,
    )


async def generate_media_edl_model_candidate(
    *, runtime: ModelRuntime, context: MediaEdlPlanningContext
) -> MediaEdlModelCandidate:
    """Make exactly one structured planning request; callers decide retry policy."""

    payload = {
        "goal": context.request.goal,
        "source_id": context.source_id,
        "duration_constraint": {
            "minimum_total_duration_ms": context.duration_constraint.minimum_duration_ms,
            "maximum_total_duration_ms": context.duration_constraint.maximum_duration_ms,
            "display": context.duration_constraint.label,
        },
        "segments": [
            {
                "sentence_id": segment.sentence_id,
                "begin_ms": segment.begin_ms,
                "end_ms": segment.end_ms,
                "duration_ms": segment.end_ms - segment.begin_ms,
                "text": segment.text,
            }
            for segment in context.segments
        ],
    }
    content = await runtime.chat_json(
        system_prompt=build_media_edl_planning_system_prompt(),
        user_message=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        maximum_tokens=720,
    )
    return parse_media_edl_model_candidate(content)


def parse_media_edl_model_candidate(content: str) -> MediaEdlModelCandidate:
    """Accept the first JSON object only; model prose never becomes a fallback plan."""

    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            raw, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        try:
            return MediaEdlModelCandidate.model_validate(raw)
        except ValidationError as exc:
            raise MediaEdlPlanningError("模型候选剪辑未通过固定契约校验。") from exc
    raise MediaEdlPlanningError("模型没有返回合法的候选剪辑 JSON。")


def build_media_edl_candidate(
    *, context: MediaEdlPlanningContext, model_candidate: MediaEdlModelCandidate
) -> tuple[MediaEdlCandidateInfo | None, str | None]:
    """Map sentence IDs to exact ranges, normalize harmless overlap, then validate the EDL."""

    if model_candidate.action == "clarify":
        return None, model_candidate.clarification_question
    by_sentence_id = {segment.sentence_id: segment for segment in context.segments}
    selections: list[MediaEdlCandidateSelection] = []
    for selection in model_candidate.selections:
        first = by_sentence_id.get(selection.start_sentence_id)
        last = by_sentence_id.get(selection.end_sentence_id)
        if first is None or last is None:
            raise MediaEdlPlanningError("模型引用了当前转写中不存在的句段。")
        if (first.begin_ms, first.end_ms) > (last.begin_ms, last.end_ms):
            raise MediaEdlPlanningError("模型引用的句段范围不是源时间顺序。")
        try:
            clip = MediaEdlClip(begin_ms=first.begin_ms, end_ms=last.end_ms)
        except ValueError as exc:
            raise MediaEdlPlanningError("模型引用的句段无法形成有效剪辑范围。") from exc
        selections.append(
            MediaEdlCandidateSelection(
                start_sentence_id=selection.start_sentence_id,
                end_sentence_id=selection.end_sentence_id,
                begin_ms=clip.begin_ms,
                end_ms=clip.end_ms,
                reason=selection.reason,
            )
        )
    normalized_selections = _normalize_candidate_selections(selections)
    normalized_selections, duration_adjusted = _fit_candidate_duration_constraint(
        selections=normalized_selections,
        context=context,
    )
    normalized_clips = [
        MediaEdlClip(begin_ms=selection.begin_ms, end_ms=selection.end_ms)
        for selection in normalized_selections
    ]
    try:
        edl = MediaEditDecisionList(source_id=context.source_id, clips=normalized_clips)
    except ValueError as exc:
        raise MediaEdlPlanningError("模型候选的时间顺序或总时长不满足受限 EDL 规则。") from exc
    _validate_candidate_duration(edl=edl, constraint=context.duration_constraint)
    return (
        MediaEdlCandidateInfo(
            source_id=context.source_id,
            transcription_task_id=context.request.transcription_task_id,
            goal=context.request.goal,
            selections=normalized_selections,
            edl=edl,
            target_min_duration_ms=context.duration_constraint.minimum_duration_ms,
            target_max_duration_ms=context.duration_constraint.maximum_duration_ms,
            duration_adjusted=duration_adjusted,
        ),
        None,
    )


def _normalize_candidate_selections(
    selections: list[MediaEdlCandidateSelection],
) -> list[MediaEdlCandidateSelection]:
    """Restore source-time order and remove repeated output from overlapping model ranges.

    The model only chooses transcript sentence IDs. It has no authority to choose a playback
    order, and this first EDL runtime cannot render overlapping clips. Sorting by harness-owned
    millisecond bounds preserves every chosen source interval; overlapping intervals are merged
    into their union so an accidental relevance-ordered list never duplicates source content.
    Disjoint selections and the final three-minute budget remain strictly validated by the EDL.
    """

    ordered = sorted(
        selections,
        key=lambda item: (item.begin_ms, item.end_ms, item.start_sentence_id, item.end_sentence_id),
    )
    normalized: list[MediaEdlCandidateSelection] = []
    for selection in ordered:
        if not normalized or selection.begin_ms >= normalized[-1].end_ms:
            normalized.append(selection)
            continue

        previous = normalized[-1]
        extends_previous = selection.end_ms > previous.end_ms
        normalized[-1] = MediaEdlCandidateSelection(
            start_sentence_id=previous.start_sentence_id,
            end_sentence_id=selection.end_sentence_id if extends_previous else previous.end_sentence_id,
            begin_ms=previous.begin_ms,
            end_ms=max(previous.end_ms, selection.end_ms),
            reason=_merged_candidate_reason(previous.reason, selection.reason),
        )
    return normalized


def _merged_candidate_reason(first: str, second: str) -> str:
    """Keep a compact review reason when two selected ranges become one source interval."""

    parts = [value.strip() for value in (first, second) if value.strip()]
    unique_parts = list(dict.fromkeys(parts))
    return "；".join(unique_parts)[:240]


def _fit_candidate_duration_constraint(
    *,
    selections: list[MediaEdlCandidateSelection],
    context: MediaEdlPlanningContext,
) -> tuple[list[MediaEdlCandidateSelection], bool]:
    """Conservatively shorten an overlong explicit target at transcript boundaries.

    The model has already selected the semantic content.  This helper may only drop
    trailing selected content or shorten the final retained selection to a supplied
    sentence boundary.  It never reorders, expands, invents, or renders a clip.
    """

    constraint = context.duration_constraint
    total_duration_ms = sum(item.end_ms - item.begin_ms for item in selections)
    if total_duration_ms <= constraint.maximum_duration_ms:
        return selections, False
    if constraint.maximum_duration_ms >= MAX_EDL_OUTPUT_DURATION_MS:
        return selections, False

    position_by_sentence_id = {segment.sentence_id: index for index, segment in enumerate(context.segments)}
    fitted: list[MediaEdlCandidateSelection] = []
    fitted_duration_ms = 0
    for selection in selections:
        selection_duration_ms = selection.end_ms - selection.begin_ms
        if fitted_duration_ms + selection_duration_ms <= constraint.maximum_duration_ms:
            fitted.append(selection)
            fitted_duration_ms += selection_duration_ms
            continue

        remaining_duration_ms = constraint.maximum_duration_ms - fitted_duration_ms
        start_index = position_by_sentence_id.get(selection.start_sentence_id)
        end_index = position_by_sentence_id.get(selection.end_sentence_id)
        if start_index is None or end_index is None or end_index < start_index:
            break
        truncated_end: MediaTranscriptionSegmentInfo | None = None
        for segment in context.segments[start_index : end_index + 1]:
            if segment.end_ms - selection.begin_ms <= remaining_duration_ms:
                truncated_end = segment
            else:
                break
        if truncated_end is not None and truncated_end.end_ms > selection.begin_ms:
            fitted.append(
                selection.model_copy(
                    update={
                        "end_sentence_id": truncated_end.sentence_id,
                        "end_ms": truncated_end.end_ms,
                    }
                )
            )
        break

    fitted_duration_ms = sum(item.end_ms - item.begin_ms for item in fitted)
    if constraint.minimum_duration_ms <= fitted_duration_ms <= constraint.maximum_duration_ms:
        return fitted, True
    return selections, False


def parse_media_edl_duration_constraint(goal: str) -> MediaEdlDurationConstraint:
    """Parse common Chinese duration goals without asking a model to interpret policy."""

    compact_goal = " ".join(goal.lower().split())
    range_match = _DURATION_RANGE.search(compact_goal)
    if range_match is not None:
        minimum_unit = range_match.group("minimum_unit") or range_match.group("maximum_unit")
        maximum_unit = range_match.group("maximum_unit")
        minimum_duration_ms = _duration_to_ms(range_match.group("minimum"), minimum_unit)
        maximum_duration_ms = _duration_to_ms(range_match.group("maximum"), maximum_unit)
        if minimum_duration_ms is not None and maximum_duration_ms is not None and minimum_duration_ms <= maximum_duration_ms:
            return MediaEdlDurationConstraint(
                minimum_duration_ms=minimum_duration_ms,
                maximum_duration_ms=min(maximum_duration_ms, MAX_EDL_OUTPUT_DURATION_MS),
            )

    for pattern in (_DURATION_CEILING, _DURATION_CEILING_PREFIX):
        ceiling_match = pattern.search(compact_goal)
        if ceiling_match is None:
            continue
        ceiling_duration_ms = _duration_to_ms(ceiling_match.group("maximum"), ceiling_match.group("unit"))
        if ceiling_duration_ms is not None:
            return MediaEdlDurationConstraint(
                maximum_duration_ms=min(ceiling_duration_ms, MAX_EDL_OUTPUT_DURATION_MS),
            )
    return _DEFAULT_DURATION_CONSTRAINT


def _duration_to_ms(raw_value: str, raw_unit: str | None) -> int | None:
    if not raw_unit:
        return None
    try:
        value = float(raw_value)
    except ValueError:
        value = float(_CHINESE_NUMBERS.get(raw_value, 0))
    if value <= 0:
        return None
    unit = raw_unit.lower()
    multiplier = 60_000 if unit.startswith(("分", "min")) else 1_000
    return max(1, int(value * multiplier))


def _validate_candidate_duration(*, edl: MediaEditDecisionList, constraint: MediaEdlDurationConstraint) -> None:
    duration_ms = edl.requested_duration_ms
    if constraint.minimum_duration_ms <= duration_ms <= constraint.maximum_duration_ms:
        return
    raise MediaEdlPlanningError(
        f"模型候选总时长 {_format_duration(duration_ms)}，不满足用户目标“{constraint.label}”；"
        "已停止，未创建可确认候选或 MP4。"
    )


def _format_duration(duration_ms: int) -> str:
    seconds = max(0, int(round(duration_ms / 1_000)))
    return f"{seconds} 秒"


def build_media_edl_planning_system_prompt() -> str:
    return (
        "你是 AgentFlow 的受限视频剪辑候选规划器。只返回一个 JSON 对象，不要 Markdown、解释、推理过程或额外字段。"
        "你只根据用户目标和提供的转写句段选择片段；不能读取文件、不能调用工具、不能渲染视频、不能假设未提供的画面内容。"
        "只能引用给定的 sentence_id，不能编造毫秒时间、文件路径、模型名、字幕、费用或新素材。"
        "候选必须按给定 begin_ms 的升序列出，不能按相关性排序、重复或重叠；最多 8 段。"
        "duration_constraint 是硬约束：返回前必须依据每段的 begin_ms/end_ms 核算合并后总时长，并严格落入其 minimum_total_duration_ms 到 maximum_total_duration_ms。"
        "宁可少选也不能超时；目标不明确时请求澄清。"
        "候选会等待用户单独确认，不会自动执行。\n"
        "JSON 契约："
        '{"action":"candidate|clarify","selections":[{"start_sentence_id":0,"end_sentence_id":0,"reason":""}],'
        '"clarification_question":""}'
    )
