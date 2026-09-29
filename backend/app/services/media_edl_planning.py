"""Model-assisted, confirmation-only EDL candidate planning.

The model receives a bounded transcript view and can reference only supplied sentence
IDs.  This module never receives local paths, FFmpeg arguments, or permission to
render media; the Harness maps valid IDs back to a constrained EDL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import ValidationError

from app.schemas.media_edl import (
    MediaEdlCandidateInfo,
    MediaEdlCandidateRequest,
    MediaEdlCandidateSelection,
    MediaEdlModelCandidate,
    MediaEditDecisionList,
    MediaEdlClip,
)
from app.schemas.media_source import MediaTranscriptionArtifactPayload, MediaTranscriptionSegmentInfo
from app.services.media_source_preparation import MediaSourcePreparationError, get_media_source
from app.services.media_transcription_delivery import load_verified_media_transcription_payload
from app.services.model_gateway import ModelRuntime


MAX_EDL_PLANNING_SEGMENTS = 320
MAX_EDL_PLANNING_CHARACTERS = 32_000
Planner = Callable[..., Awaitable[MediaEdlModelCandidate]]
TranscriptLoader = Callable[..., MediaTranscriptionArtifactPayload]


class MediaEdlPlanningError(ValueError):
    """A safe candidate-planning failure which never exposes transcript text or paths."""


@dataclass(frozen=True)
class MediaEdlPlanningContext:
    project_id: str
    request: MediaEdlCandidateRequest
    source_id: str
    source_sha256: str
    segments: tuple[MediaTranscriptionSegmentInfo, ...]


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
    segments = tuple(sorted(payload.transcript.segments, key=lambda item: (item.begin_ms, item.end_ms, item.sentence_id)))
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
    )


async def generate_media_edl_model_candidate(
    *, runtime: ModelRuntime, context: MediaEdlPlanningContext
) -> MediaEdlModelCandidate:
    """Make exactly one structured planning request; callers decide retry policy."""

    payload = {
        "goal": context.request.goal,
        "source_id": context.source_id,
        "segments": [
            {
                "sentence_id": segment.sentence_id,
                "begin_ms": segment.begin_ms,
                "end_ms": segment.end_ms,
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
    normalized_clips = [
        MediaEdlClip(begin_ms=selection.begin_ms, end_ms=selection.end_ms)
        for selection in normalized_selections
    ]
    try:
        edl = MediaEditDecisionList(source_id=context.source_id, clips=normalized_clips)
    except ValueError as exc:
        raise MediaEdlPlanningError("模型候选的时间顺序或总时长不满足受限 EDL 规则。") from exc
    return (
        MediaEdlCandidateInfo(
            source_id=context.source_id,
            transcription_task_id=context.request.transcription_task_id,
            goal=context.request.goal,
            selections=normalized_selections,
            edl=edl,
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


def build_media_edl_planning_system_prompt() -> str:
    return (
        "你是 AgentFlow 的受限视频剪辑候选规划器。只返回一个 JSON 对象，不要 Markdown、解释、推理过程或额外字段。"
        "你只根据用户目标和提供的转写句段选择片段；不能读取文件、不能调用工具、不能渲染视频、不能假设未提供的画面内容。"
        "只能引用给定的 sentence_id，不能编造毫秒时间、文件路径、模型名、字幕、费用或新素材。"
        "候选必须按给定 begin_ms 的升序列出，不能按相关性排序、重复或重叠；最多 8 段，总时长不超过 180000 ms。目标不明确时请求澄清。"
        "候选会等待用户单独确认，不会自动执行。\n"
        "JSON 契约："
        '{"action":"candidate|clarify","selections":[{"start_sentence_id":0,"end_sentence_id":0,"reason":""}],'
        '"clarification_question":""}'
    )
