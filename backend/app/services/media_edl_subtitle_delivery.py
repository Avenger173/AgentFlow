"""Deterministic SRT delivery for a verified transcript and an EDL candidate.

This module deliberately has no model dependency.  A full subtitle keeps source
timestamps; a cut subtitle maps only the candidate's constrained clips onto the
new MP4 timeline.  Both files are UTF-8 read back before becoming artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import re
from threading import RLock
from typing import Literal

from pydantic import ValidationError

from app.core.config import settings
from app.database.task_repository import append_workflow_artifact, list_workflow_artifacts, load_workflow_run
from app.schemas.media_edl import MediaEdlCandidateInfo
from app.schemas.media_source import MediaTranscriptionSegmentInfo
from app.schemas.workflow import WorkflowArtifact
from app.services.media_source_preparation import MediaSourcePreparationError, get_media_source
from app.services.media_transcript_timing import normalize_cumulative_transcript_spans
from app.services.media_transcription_delivery import load_verified_media_transcription_payload


MEDIA_EDL_SUBTITLE_STEP_ID = "media_edl_candidate"
MEDIA_AGENT_ID = "media_agent"
MAX_SRT_BYTES = 2 * 1024 * 1024
_TASK_ID_PATTERN = re.compile(r"^task_media_edl_plan_[0-9a-f]{12}$")
_SUBTITLE_KINDS = {"full", "cut"}
_DELIVERY_LOCK = RLock()

SubtitleKind = Literal["full", "cut"]


class MediaEdlSubtitleDeliveryError(MediaSourcePreparationError):
    """A safe failure while exporting a deterministic subtitle artifact."""


@dataclass(frozen=True)
class _SubtitleEntry:
    begin_ms: int
    end_ms: int
    text: str


def resolve_media_edl_subtitle_download(
    *,
    project_id: str,
    candidate_task_id: str,
    subtitle_kind: SubtitleKind,
) -> tuple[Path, str]:
    """Return one verified subtitle artifact without repeating ASR or planning.

    The candidate task is the immutable version identity.  Repeated downloads only
    verify and reuse its existing file; a missing or changed registered artifact is
    reported instead of silently regenerating a different delivery.
    """

    if subtitle_kind not in _SUBTITLE_KINDS:
        raise MediaEdlSubtitleDeliveryError("未知字幕类型；只支持完整字幕或成片字幕。")
    with _DELIVERY_LOCK:
        candidate, entries = _load_candidate_and_entries(
            project_id=project_id,
            candidate_task_id=candidate_task_id,
        )
        expected_entries = _entries_for_kind(
            subtitle_kind=subtitle_kind,
            entries=entries,
            candidate=candidate,
        )
        if not expected_entries:
            raise MediaEdlSubtitleDeliveryError("当前候选没有可导出的有效字幕句段。")
        encoded = _encode_srt(expected_entries)
        artifact_id = _artifact_id(candidate_task_id=candidate_task_id, subtitle_kind=subtitle_kind)
        path = _subtitle_path(candidate_task_id=candidate_task_id, subtitle_kind=subtitle_kind)
        existing = next(
            (item for item in list_workflow_artifacts(candidate_task_id) if item.artifact_id == artifact_id),
            None,
        )
        if existing is not None:
            _verify_existing_artifact(
                artifact=existing,
                path=path,
                expected=encoded,
                expected_entries=expected_entries,
            )
            return path, existing.name

        _write_and_verify(path=path, encoded=encoded, expected_entries=expected_entries)
        artifact = _build_artifact(
            candidate_task_id=candidate_task_id,
            project_id=project_id,
            candidate=candidate,
            subtitle_kind=subtitle_kind,
            path=path,
            encoded=encoded,
            entry_count=len(expected_entries),
        )
        try:
            append_workflow_artifact(
                artifact=artifact,
                event_name="artifact_saved",
                message=f"已导出并回读验证{_subtitle_label(subtitle_kind)}。",
            )
        except KeyError as exc:  # pragma: no cover - candidate was loaded above.
            raise MediaEdlSubtitleDeliveryError("候选剪辑任务不存在，无法登记字幕交付。") from exc
        return path, artifact.name


def _load_candidate_and_entries(
    *,
    project_id: str,
    candidate_task_id: str,
) -> tuple[MediaEdlCandidateInfo, tuple[_SubtitleEntry, ...]]:
    if _TASK_ID_PATTERN.fullmatch(candidate_task_id) is None:
        raise MediaEdlSubtitleDeliveryError("候选剪辑任务标识无效。")
    run = load_workflow_run(candidate_task_id)
    if run is None or run.status != "completed":
        raise MediaEdlSubtitleDeliveryError("候选剪辑尚未完成，暂时不能导出字幕。")
    step = next(
        (
            item
            for item in run.steps
            if item.step_id == MEDIA_EDL_SUBTITLE_STEP_ID and item.action == "media.plan_edl_candidate"
        ),
        None,
    )
    if step is None or step.output.get("project_id") != project_id:
        raise MediaEdlSubtitleDeliveryError("候选剪辑不属于当前视频项目。")
    raw_candidate = step.output.get("candidate")
    if not isinstance(raw_candidate, dict):
        raise MediaEdlSubtitleDeliveryError("候选剪辑没有可导出的已验证片段。")
    try:
        candidate = MediaEdlCandidateInfo.model_validate(raw_candidate)
    except ValidationError as exc:
        raise MediaEdlSubtitleDeliveryError("候选剪辑记录不完整，无法导出字幕。") from exc

    try:
        transcript = load_verified_media_transcription_payload(
            task_id=candidate.transcription_task_id,
            expected_project_id=project_id,
        )
        source = get_media_source(candidate.source_id, expected_project_scope=project_id)
    except MediaSourcePreparationError as exc:
        raise MediaEdlSubtitleDeliveryError(str(exc)) from exc
    if transcript.request.source_id != candidate.source_id or transcript.audio.source_sha256 != source.source_sha256:
        raise MediaEdlSubtitleDeliveryError("候选剪辑与已验证转写或受控视频不匹配。")

    normalized = normalize_cumulative_transcript_spans(transcript.transcript.segments)
    entries = _normalize_subtitle_entries(
        tuple(
            MediaTranscriptionSegmentInfo(
                sentence_id=transcript.transcript.segments[item.source_index].sentence_id,
                text=item.text,
                begin_ms=item.begin_ms,
                end_ms=item.end_ms,
            )
            for item in normalized
        )
    )
    if not entries:
        raise MediaEdlSubtitleDeliveryError("已验证转写没有可导出的有效句段。")
    return candidate, entries


def _normalize_subtitle_entries(
    segments: tuple[MediaTranscriptionSegmentInfo, ...],
) -> tuple[_SubtitleEntry, ...]:
    """Make SRT entries monotonic while preserving provider text and source timing."""

    entries: list[_SubtitleEntry] = []
    previous_end = 0
    for segment in sorted(segments, key=lambda item: (item.begin_ms, item.end_ms, item.sentence_id)):
        text = " ".join(segment.text.split())
        begin_ms = max(previous_end, segment.begin_ms)
        end_ms = segment.end_ms
        if not text or end_ms <= begin_ms:
            continue
        entries.append(_SubtitleEntry(begin_ms=begin_ms, end_ms=end_ms, text=text))
        previous_end = end_ms
    return tuple(entries)


def _entries_for_kind(
    *,
    subtitle_kind: SubtitleKind,
    entries: tuple[_SubtitleEntry, ...],
    candidate: MediaEdlCandidateInfo,
) -> tuple[_SubtitleEntry, ...]:
    if subtitle_kind == "full":
        return entries

    remapped: list[_SubtitleEntry] = []
    output_offset_ms = 0
    for clip in candidate.edl.clips:
        for entry in entries:
            begin_ms = max(entry.begin_ms, clip.begin_ms)
            end_ms = min(entry.end_ms, clip.end_ms)
            if end_ms <= begin_ms:
                continue
            remapped.append(
                _SubtitleEntry(
                    begin_ms=output_offset_ms + begin_ms - clip.begin_ms,
                    end_ms=output_offset_ms + end_ms - clip.begin_ms,
                    text=entry.text,
                )
            )
        output_offset_ms += clip.end_ms - clip.begin_ms
    return tuple(remapped)


def _encode_srt(entries: tuple[_SubtitleEntry, ...]) -> bytes:
    blocks = [
        f"{index}\n{_format_srt_time(entry.begin_ms)} --> {_format_srt_time(entry.end_ms)}\n{entry.text}"
        for index, entry in enumerate(entries, start=1)
    ]
    encoded = ("\n\n".join(blocks) + "\n").encode("utf-8")
    if len(encoded) > MAX_SRT_BYTES:
        raise MediaEdlSubtitleDeliveryError("字幕文件超过当前受控交付上限。")
    return encoded


def _write_and_verify(*, path: Path, encoded: bytes, expected_entries: tuple[_SubtitleEntry, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _verify_srt(path=path, expected=encoded, expected_entries=expected_entries)


def _verify_existing_artifact(
    *,
    artifact: WorkflowArtifact,
    path: Path,
    expected: bytes,
    expected_entries: tuple[_SubtitleEntry, ...],
) -> None:
    _verify_srt(path=path, expected=expected, expected_entries=expected_entries)
    expected_sha256 = sha256(expected).hexdigest()
    if artifact.metadata.get("sha256") != expected_sha256:
        raise MediaEdlSubtitleDeliveryError("已登记字幕的哈希与当前候选不一致，已停止下载。")


def _verify_srt(*, path: Path, expected: bytes, expected_entries: tuple[_SubtitleEntry, ...]) -> None:
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_SRT_BYTES:
        raise MediaEdlSubtitleDeliveryError("已验证字幕文件不存在或超过上限。")
    actual = path.read_bytes()
    if actual != expected:
        raise MediaEdlSubtitleDeliveryError("字幕文件回读内容与当前候选不一致。")
    if _parse_srt(actual) != expected_entries:
        raise MediaEdlSubtitleDeliveryError("字幕文件未通过编号或时间轴回读校验。")


def _parse_srt(encoded: bytes) -> tuple[_SubtitleEntry, ...]:
    try:
        content = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MediaEdlSubtitleDeliveryError("字幕文件不是 UTF-8 编码。") from exc
    blocks = [block for block in content.replace("\r\n", "\n").strip().split("\n\n") if block.strip()]
    entries: list[_SubtitleEntry] = []
    previous_end = 0
    for expected_index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise MediaEdlSubtitleDeliveryError("字幕文件缺少编号、时间轴或文本。")
        try:
            index = int(lines[0])
        except ValueError as exc:
            raise MediaEdlSubtitleDeliveryError("字幕编号无效。") from exc
        if index != expected_index or " --> " not in lines[1]:
            raise MediaEdlSubtitleDeliveryError("字幕编号或时间轴顺序无效。")
        begin_text, end_text = lines[1].split(" --> ", maxsplit=1)
        begin_ms = _parse_srt_time(begin_text)
        end_ms = _parse_srt_time(end_text)
        text = " ".join("\n".join(lines[2:]).split())
        if not text or end_ms <= begin_ms or begin_ms < previous_end:
            raise MediaEdlSubtitleDeliveryError("字幕时间轴存在重叠、倒序或空文本。")
        entries.append(_SubtitleEntry(begin_ms=begin_ms, end_ms=end_ms, text=text))
        previous_end = end_ms
    return tuple(entries)


def _build_artifact(
    *,
    candidate_task_id: str,
    project_id: str,
    candidate: MediaEdlCandidateInfo,
    subtitle_kind: SubtitleKind,
    path: Path,
    encoded: bytes,
    entry_count: int,
) -> WorkflowArtifact:
    filename = _subtitle_filename(subtitle_kind)
    return WorkflowArtifact(
        artifact_id=_artifact_id(candidate_task_id=candidate_task_id, subtitle_kind=subtitle_kind),
        task_id=candidate_task_id,
        step_id=MEDIA_EDL_SUBTITLE_STEP_ID,
        agent_id=MEDIA_AGENT_ID,
        kind="file",
        name=filename,
        summary=f"{_subtitle_label(subtitle_kind)} · {entry_count} 条 · UTF-8 · 已回读验证",
        uri=f"agentflow-output://media_subtitles/{project_id}/{candidate_task_id}/{filename}",
        mime_type="application/x-subrip",
        metadata={
            "runtime": True,
            "output_scope": "media_subtitles",
            "output_path": str(path),
            "output_size_bytes": len(encoded),
            "sha256": sha256(encoded).hexdigest(),
            "project_id": project_id,
            "source_id": candidate.source_id,
            "transcription_task_id": candidate.transcription_task_id,
            "parent_candidate_task_id": candidate.parent_candidate_task_id,
            "subtitle_kind": subtitle_kind,
            "entry_count": entry_count,
            "verification": {"passed": True, "format": "SRT", "encoding": "UTF-8"},
            "model_used": False,
            "network_used": False,
        },
        created_at=datetime.now(UTC).isoformat(),
    )


def _subtitle_path(*, candidate_task_id: str, subtitle_kind: SubtitleKind) -> Path:
    root = settings.media_edl_subtitle_output_dir
    path = (root / f"{candidate_task_id}_{subtitle_kind}.srt").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:  # pragma: no cover - fixed task IDs make this defensive.
        raise MediaEdlSubtitleDeliveryError("字幕交付路径无效。") from exc
    return path


def _artifact_id(*, candidate_task_id: str, subtitle_kind: SubtitleKind) -> str:
    return f"artifact_media_edl_subtitle_{subtitle_kind}_{candidate_task_id.rsplit('_', maxsplit=1)[-1]}"


def _subtitle_filename(subtitle_kind: SubtitleKind) -> str:
    return "full_transcript.srt" if subtitle_kind == "full" else "edited_subtitles.srt"


def _subtitle_label(subtitle_kind: SubtitleKind) -> str:
    return "完整 SRT" if subtitle_kind == "full" else "成片 SRT"


def _format_srt_time(milliseconds: int) -> str:
    total = max(0, int(milliseconds))
    hours, remaining = divmod(total, 3_600_000)
    minutes, remaining = divmod(remaining, 60_000)
    seconds, millis = divmod(remaining, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _parse_srt_time(value: str) -> int:
    matched = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if matched is None:
        raise MediaEdlSubtitleDeliveryError("字幕时间格式无效。")
    hours, minutes, seconds, millis = (int(part) for part in matched.groups())
    if minutes >= 60 or seconds >= 60:
        raise MediaEdlSubtitleDeliveryError("字幕时间值超出范围。")
    return ((hours * 60 + minutes) * 60 + seconds) * 1_000 + millis
