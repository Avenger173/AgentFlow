"""MM-4 短媒体转写质量集与离线评分的共享契约。

这个模块只读取冻结的公开授权夹具、参考标注和已回读的转写 Artifact，
不调用 Provider、不读取客户任务库，也不会把转写正文写入评测报告。程序生成
的 SAPI 夹具适合验证工具与交付链路，但不能进入此处的内容质量样本。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


SUITE_TYPE = "agentflow-mm4-asr-quality-suite-v1"
RUN_TYPE = "agentflow-mm4-asr-quality-run-v1"
SPLITS = {"development", "holdout"}
LANGUAGES = {"zh", "en"}
MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
MAX_NORMALIZED_AUDIO_DURATION_MS = 200_000
MIN_TOTAL_DURATION_MS = 8 * 60 * 1000
_ZH_CER_LIMIT = 0.15
_EN_WER_LIMIT = 0.20
_TIMESTAMP_P95_LIMIT_MS = 500
_TIMESTAMP_MAX_LIMIT_MS = 1_500
_COMPLETED_STATUS = "completed"
_INCOMPLETE_STATUSES = {"failed", "cancelled", "outcome_unknown", "not_started"}
_FAILURE_CATEGORIES = {
    "validation_failed",
    "provider_rejected",
    "provider_outcome_unknown",
    "delivery_verification_failed",
    "cancelled",
    "unexpected",
    "batch_halted",
}


class QualityContractError(ValueError):
    """质量集、参考标注或回读结果不满足冻结契约。"""


@dataclass(frozen=True)
class FixtureRecord:
    fixture_id: str
    split: Literal["development", "holdout"]
    language: Literal["zh", "en"]
    source_ref: str
    media_file: str
    media_sha256: str
    duration_ms: int
    normalized_audio_duration_ms: int
    reference_text: str
    reference_segments: list[dict[str, object]]


def canonical_sha256(value: object) -> str:
    """用稳定 JSON 表达记录夹具或运行清单的身份，不依赖文件格式化。"""

    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_suite(suite_path: Path, *, verify_files: bool) -> tuple[dict[str, object], dict[str, FixtureRecord]]:
    """验证 G4-ASR-DEV 夹具规模、来源、标注和短媒体输入边界。"""

    suite_path = suite_path.resolve()
    payload = _read_json(suite_path, label="quality suite")
    if payload.get("suite_type") != SUITE_TYPE:
        raise QualityContractError(f"suite_type must be {SUITE_TYPE}")
    fixtures = _index_fixtures(payload.get("fixtures"), root=suite_path.parent, verify_files=verify_files)

    split_counts = Counter(record.split for record in fixtures.values())
    if split_counts != Counter({"development": 5, "holdout": 3}):
        raise QualityContractError("suite must contain exactly 5 development and 3 holdout source videos")
    language_counts = Counter(record.language for record in fixtures.values())
    if any(language_counts[language] < 3 for language in LANGUAGES):
        raise QualityContractError("suite must contain at least three Chinese and three English source videos")
    split_languages = Counter((record.split, record.language) for record in fixtures.values())
    if any(split_languages[(split, language)] < 1 for split in SPLITS for language in LANGUAGES):
        raise QualityContractError("both splits must include Chinese and English source videos")
    total_duration_ms = sum(record.duration_ms for record in fixtures.values())
    if total_duration_ms < MIN_TOTAL_DURATION_MS:
        raise QualityContractError("short-media ASR suite must provide at least eight minutes of source video")

    report: dict[str, object] = {
        "ok": True,
        "suite_type": SUITE_TYPE,
        "suite_path": str(suite_path),
        "suite_sha256": canonical_sha256(payload),
        "fixture_count": len(fixtures),
        "fixture_split_counts": dict(sorted(split_counts.items())),
        "fixture_language_counts": dict(sorted(language_counts.items())),
        "total_source_duration_ms": total_duration_ms,
        "short_media_audio_limit_ms": MAX_NORMALIZED_AUDIO_DURATION_MS,
        "quality_claim": "none; this validates G4-ASR-DEV fixture provenance and annotation prerequisites only",
    }
    return report, fixtures


def evaluate_run(
    suite_path: Path,
    run_path: Path,
    *,
    verify_files: bool,
) -> dict[str, object]:
    """离线计算冻结运行的 CER/WER 与句段时间包络偏差。"""

    suite_report, fixtures = validate_suite(suite_path, verify_files=verify_files)
    run_path = run_path.resolve()
    run = _read_json(run_path, label="quality run")
    if run.get("run_type") != RUN_TYPE:
        raise QualityContractError(f"run_type must be {RUN_TYPE}")
    if str(run.get("suite_sha256") or "").lower() != str(suite_report["suite_sha256"]):
        raise QualityContractError("quality run does not match the frozen suite hash")
    route = _required_string(run, "route", "quality run")
    if route != "media_transcription":
        raise QualityContractError("quality run must use the media_transcription route")
    _required_string(run, "provider", "quality run")
    _required_string(run, "model", "quality run")

    raw_cases = run.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) != len(fixtures):
        raise QualityContractError("quality run must contain exactly one result for every frozen fixture")
    case_records = _read_cases(raw_cases, fixtures=fixtures, root=run_path.parent, verify_files=verify_files)

    metric_records: list[dict[str, object]] = []
    incomplete_cases: list[str] = []
    incomplete_case_statuses: list[dict[str, str]] = []
    for case in case_records:
        if case["status"] != _COMPLETED_STATUS:
            incomplete_cases.append(str(case["fixture_id"]))
            incomplete_case_statuses.append(
                {
                    "fixture_id": str(case["fixture_id"]),
                    "status": str(case["status"]),
                    "failure_category": str(case["failure_category"]),
                }
            )
            continue
        fixture = fixtures[str(case["fixture_id"])]
        metric_records.append(_score_completed_case(case, fixture))

    metric_summary = _summarize_metrics(metric_records)
    metric_gate_passed = not incomplete_cases and _metrics_pass(metric_summary)
    return {
        "ok": True,
        "assessment": "agentflow-mm4-g4-asr-development-v1",
        "quality_gate": "G4-ASR-DEV",
        "quality_gate_passed": metric_gate_passed,
        "quality_gate_meaning": (
            "Frozen short-media ASR fixtures passed the text and timestamp development thresholds. "
            "This is not full G4, subtitle, EDL, long-media, sync, UI, or release approval."
            if metric_gate_passed
            else "At least one fixture is incomplete or failed its frozen text/timestamp threshold; inspect case IDs and metrics."
        ),
        "suite": {
            "path": str(suite_path.resolve()),
            "sha256": suite_report["suite_sha256"],
            "fixture_count": suite_report["fixture_count"],
        },
        "run": {
            "path": str(run_path),
            "provider": str(run["provider"]),
            "model": str(run["model"]),
            "route": route,
            "provider_call_count": sum(int(case["provider_call_count"]) for case in case_records),
            "network_calls_by_evaluator": 0,
        },
        "incomplete_case_ids": incomplete_cases,
        "incomplete_case_statuses": incomplete_case_statuses,
        "metrics": metric_summary,
        "cases": metric_records,
        "content_handling": "reports retain hashes and numeric scores only; no transcript text is emitted",
    }


def _index_fixtures(value: object, *, root: Path, verify_files: bool) -> dict[str, FixtureRecord]:
    if not isinstance(value, list) or len(value) != 8:
        raise QualityContractError("fixtures must contain exactly eight source videos")
    records: dict[str, FixtureRecord] = {}
    source_refs: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise QualityContractError(f"fixture {index} must be an object")
        fixture_id = _required_string(raw, "fixture_id", f"fixture {index}")
        if fixture_id in records:
            raise QualityContractError(f"duplicate fixture_id: {fixture_id}")
        split = _required_string(raw, "split", fixture_id)
        if split not in SPLITS:
            raise QualityContractError(f"fixture {fixture_id} has unsupported split")
        language = _required_string(raw, "language", fixture_id)
        if language not in LANGUAGES:
            raise QualityContractError(f"fixture {fixture_id} language must be zh or en")
        if raw.get("source_kind") != "public_licensed" or raw.get("rights_reviewed") is not True:
            raise QualityContractError(f"fixture {fixture_id} must be a rights-reviewed public licensed source")
        source_ref = _required_string(raw, "source_ref", fixture_id)
        if source_ref in source_refs:
            raise QualityContractError("a source recording cannot be reused across fixture splits")
        source_refs.add(source_ref)
        for field in ("source_page", "license_url"):
            if not _required_string(raw, field, fixture_id).startswith("https://"):
                raise QualityContractError(f"fixture {fixture_id} must provide HTTPS {field}")
        _required_string(raw, "license", fixture_id)
        media_file = _relative_path(raw, "media_file", fixture_id)
        if Path(media_file).suffix.lower() not in MEDIA_SUFFIXES:
            raise QualityContractError(f"fixture {fixture_id} must reference a supported video container")
        media_sha256 = _sha256_value(raw, "media_sha256", fixture_id)
        duration_ms = _positive_int(raw, "duration_ms", fixture_id)
        normalized_audio_duration_ms = _positive_int(raw, "normalized_audio_duration_ms", fixture_id)
        if normalized_audio_duration_ms > MAX_NORMALIZED_AUDIO_DURATION_MS or normalized_audio_duration_ms > duration_ms:
            raise QualityContractError(f"fixture {fixture_id} is outside the current short-media ASR input limit")
        text_path = _safe_file(root, _relative_path(raw, "reference_text_file", fixture_id), fixture_id, "reference text")
        segments_path = _safe_file(root, _relative_path(raw, "reference_segments_file", fixture_id), fixture_id, "reference segments")
        text_sha256 = _sha256_value(raw, "reference_text_sha256", fixture_id)
        segments_sha256 = _sha256_value(raw, "reference_segments_sha256", fixture_id)
        if verify_files:
            _verify_file_sha256(_safe_file(root, media_file, fixture_id, "media"), media_sha256, fixture_id, "media")
        # 人工校对文本和时间标注属于质量集本体，不能因跳过媒体二进制回读而缺席。
        _verify_file_sha256(text_path, text_sha256, fixture_id, "reference text")
        _verify_file_sha256(segments_path, segments_sha256, fixture_id, "reference segments")
        if raw.get("reference_transcript_reviewed") is not True or raw.get("time_annotations_reviewed") is not True:
            raise QualityContractError(f"fixture {fixture_id} must have reviewed transcript and time annotations")
        reference_text = _read_text(text_path, fixture_id)
        reference_segments = _read_reference_segments(segments_path, fixture_id, duration_ms)
        if _normalize_characters(reference_text) != _normalize_characters(
            " ".join(str(segment["text"]) for segment in reference_segments)
        ):
            raise QualityContractError(f"fixture {fixture_id} reference transcript does not match its time annotations")
        records[fixture_id] = FixtureRecord(
            fixture_id=fixture_id,
            split=split,  # type: ignore[arg-type]
            language=language,  # type: ignore[arg-type]
            source_ref=source_ref,
            media_file=media_file,
            media_sha256=media_sha256,
            duration_ms=duration_ms,
            normalized_audio_duration_ms=normalized_audio_duration_ms,
            reference_text=reference_text,
            reference_segments=reference_segments,
        )
    return records


def _read_cases(
    raw_cases: list[object],
    *,
    fixtures: dict[str, FixtureRecord],
    root: Path,
    verify_files: bool,
) -> list[dict[str, object]]:
    seen: set[str] = set()
    records: list[dict[str, object]] = []
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise QualityContractError(f"run case {index} must be an object")
        fixture_id = _required_string(raw, "fixture_id", f"run case {index}")
        if fixture_id not in fixtures or fixture_id in seen:
            raise QualityContractError(f"run case has unknown or duplicate fixture_id: {fixture_id}")
        seen.add(fixture_id)
        status = _required_string(raw, "status", fixture_id)
        if status != _COMPLETED_STATUS and status not in _INCOMPLETE_STATUSES:
            raise QualityContractError(f"run case {fixture_id} has unsupported status")
        provider_call_count = raw.get("provider_call_count")
        if not isinstance(provider_call_count, int) or provider_call_count < 0 or provider_call_count > 1:
            raise QualityContractError(f"run case {fixture_id} must record zero or one Provider call")
        if status == _COMPLETED_STATUS:
            if provider_call_count != 1:
                raise QualityContractError(f"completed case {fixture_id} must record exactly one Provider call")
            artifact_file = _safe_file(root, _relative_path(raw, "artifact_file", fixture_id), fixture_id, "artifact")
            artifact_sha256 = _sha256_value(raw, "artifact_sha256", fixture_id)
            # 结果 Artifact 是评分唯一允许读取的模型输出，必须始终回读并哈希校验。
            _verify_file_sha256(artifact_file, artifact_sha256, fixture_id, "artifact")
            transcript = _read_artifact_transcript(artifact_file, fixture=fixtures[fixture_id])
            failure_category: str | None = None
        else:
            failure_category = _required_string(raw, "failure_category", fixture_id)
            if failure_category not in _FAILURE_CATEGORIES:
                raise QualityContractError(f"run case {fixture_id} has unsupported failure_category")
            if "artifact_file" in raw or "artifact_sha256" in raw:
                raise QualityContractError(f"incomplete case {fixture_id} must not claim an Artifact")
            if status == "cancelled" and (failure_category != "cancelled" or provider_call_count != 0):
                raise QualityContractError(f"cancelled case {fixture_id} must not submit a Provider request")
            if status == "outcome_unknown" and (failure_category != "provider_outcome_unknown" or provider_call_count != 1):
                raise QualityContractError(f"outcome_unknown case {fixture_id} must record one unknown Provider request")
            if status == "not_started" and (failure_category != "batch_halted" or provider_call_count != 0):
                raise QualityContractError(f"not_started case {fixture_id} must remain an unsubmitted batch remainder")
            if status == "failed":
                expected_call_count = 0 if failure_category == "validation_failed" else 1
                if failure_category in {"cancelled", "batch_halted", "provider_outcome_unknown"}:
                    raise QualityContractError(f"failed case {fixture_id} has an incompatible failure_category")
                if provider_call_count != expected_call_count:
                    raise QualityContractError(f"failed case {fixture_id} has an inconsistent Provider call count")
            transcript = None
        records.append(
            {
                "fixture_id": fixture_id,
                "status": status,
                "provider_call_count": provider_call_count,
                "failure_category": failure_category,
                "transcript": transcript,
            }
        )
    if seen != set(fixtures):
        raise QualityContractError("quality run does not cover every frozen fixture")
    return records


def _read_artifact_transcript(path: Path, *, fixture: FixtureRecord) -> dict[str, object]:
    if not path.is_file():
        raise QualityContractError(f"completed fixture {fixture.fixture_id} artifact file is missing")
    payload = _read_json(path, label=f"artifact for {fixture.fixture_id}")
    try:
        from app.schemas.media_source import MediaTranscriptionArtifactPayload

        artifact = MediaTranscriptionArtifactPayload.model_validate(payload)
    except (ImportError, ValidationError) as exc:
        raise QualityContractError(f"artifact for {fixture.fixture_id} fails the media transcription contract") from exc
    if artifact.audio.source_sha256 != fixture.media_sha256:
        raise QualityContractError(f"artifact source hash does not match frozen fixture: {fixture.fixture_id}")
    segments = [segment.model_dump(mode="json") for segment in artifact.transcript.segments]
    return {"text": artifact.transcript.text, "segments": segments}


def _score_completed_case(case: dict[str, object], fixture: FixtureRecord) -> dict[str, object]:
    transcript = case["transcript"]
    assert isinstance(transcript, dict)
    actual_text = str(transcript["text"])
    if fixture.language == "zh":
        reference_tokens = list(_normalize_characters(fixture.reference_text))
        actual_tokens = list(_normalize_characters(actual_text))
        metric_name = "cer"
        metric_limit = _ZH_CER_LIMIT
    else:
        reference_tokens = _english_words(fixture.reference_text)
        actual_tokens = _english_words(actual_text)
        metric_name = "wer"
        metric_limit = _EN_WER_LIMIT
    distance = _levenshtein(reference_tokens, actual_tokens)
    substitutions, deletions, insertions = _alignment_errors(reference_tokens, actual_tokens)
    if not reference_tokens:
        raise QualityContractError(f"fixture {fixture.fixture_id} has no scoreable reference tokens")
    error_rate = distance / len(reference_tokens)
    start_error_ms, end_error_ms = _timestamp_envelope_error(
        transcript.get("segments"), fixture.reference_segments, fixture.fixture_id
    )
    return {
        "fixture_id": fixture.fixture_id,
        "split": fixture.split,
        "language": fixture.language,
        "provider_call_count": case["provider_call_count"],
        "text_metric": metric_name,
        "reference_token_count": len(reference_tokens),
        "edit_distance": distance,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "error_rate": round(error_rate, 6),
        "error_rate_limit": metric_limit,
        "text_metric_passed": error_rate <= metric_limit,
        "timestamp_start_abs_error_ms": start_error_ms,
        "timestamp_end_abs_error_ms": end_error_ms,
        "timestamp_case_max_abs_error_ms": max(start_error_ms, end_error_ms),
        "timestamp_case_passed": max(start_error_ms, end_error_ms) <= _TIMESTAMP_MAX_LIMIT_MS,
    }


def _summarize_metrics(records: list[dict[str, object]]) -> dict[str, object]:
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        groups[(str(record["split"]), str(record["language"]))].append(record)
    summary: dict[str, object] = {}
    for split in sorted(SPLITS):
        split_summary: dict[str, object] = {}
        for language in sorted(LANGUAGES):
            records_for_group = groups.get((split, language), [])
            if not records_for_group:
                split_summary[language] = {"status": "missing"}
                continue
            errors = sum(int(record["edit_distance"]) for record in records_for_group)
            references = sum(int(record["reference_token_count"]) for record in records_for_group)
            metric_name = str(records_for_group[0]["text_metric"])
            metric_limit = float(records_for_group[0]["error_rate_limit"])
            timestamp_values = sorted(
                int(value)
                for record in records_for_group
                for value in (record["timestamp_start_abs_error_ms"], record["timestamp_end_abs_error_ms"])
            )
            aggregate_rate = errors / references if references else 1.0
            p95 = _percentile(timestamp_values, 0.95)
            maximum = max(timestamp_values)
            split_summary[language] = {
                "status": "scored",
                "fixture_count": len(records_for_group),
                metric_name: round(aggregate_rate, 6),
                "reference_token_count": references,
                "edit_distance": errors,
                "metric_limit": metric_limit,
                "text_metric_passed": aggregate_rate <= metric_limit,
                "timestamp_p95_abs_error_ms": p95,
                "timestamp_max_abs_error_ms": maximum,
                "timestamp_p95_limit_ms": _TIMESTAMP_P95_LIMIT_MS,
                "timestamp_max_limit_ms": _TIMESTAMP_MAX_LIMIT_MS,
                "timestamp_metric_passed": p95 <= _TIMESTAMP_P95_LIMIT_MS and maximum <= _TIMESTAMP_MAX_LIMIT_MS,
            }
        summary[split] = split_summary
    return summary


def _metrics_pass(summary: dict[str, object]) -> bool:
    for split in SPLITS:
        split_summary = summary.get(split)
        if not isinstance(split_summary, dict):
            return False
        for language in LANGUAGES:
            group = split_summary.get(language)
            if not isinstance(group, dict):
                return False
            if group.get("status") != "scored" or group.get("text_metric_passed") is not True:
                return False
            if group.get("timestamp_metric_passed") is not True:
                return False
    return True


def _timestamp_envelope_error(
    actual_segments: object,
    reference_segments: list[dict[str, object]],
    fixture_id: str,
) -> tuple[int, int]:
    if not isinstance(actual_segments, list) or not actual_segments or not reference_segments:
        raise QualityContractError(f"fixture {fixture_id} has no scoreable timestamp segments")
    try:
        actual_start = min(int(segment["begin_ms"]) for segment in actual_segments if isinstance(segment, dict))
        actual_end = max(int(segment["end_ms"]) for segment in actual_segments if isinstance(segment, dict))
        reference_start = min(int(segment["begin_ms"]) for segment in reference_segments)
        reference_end = max(int(segment["end_ms"]) for segment in reference_segments)
    except (KeyError, TypeError, ValueError) as exc:
        raise QualityContractError(f"fixture {fixture_id} has malformed timestamp segments") from exc
    return abs(actual_start - reference_start), abs(actual_end - reference_end)


def _read_reference_segments(path: Path, fixture_id: str, duration_ms: int) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    value = _read_json(path, label=f"reference segments for {fixture_id}")
    raw_segments = value.get("segments") if isinstance(value, dict) else None
    if not isinstance(raw_segments, list) or not raw_segments:
        raise QualityContractError(f"fixture {fixture_id} reference segments must be a non-empty list")
    segments: list[dict[str, object]] = []
    previous_end = -1
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise QualityContractError(f"fixture {fixture_id} reference segment {index} must be an object")
        text = _required_string(raw, "text", f"fixture {fixture_id} reference segment {index}")
        begin_ms = _positive_or_zero_int(raw, "begin_ms", f"fixture {fixture_id} reference segment {index}")
        end_ms = _positive_or_zero_int(raw, "end_ms", f"fixture {fixture_id} reference segment {index}")
        if end_ms < begin_ms or begin_ms < previous_end or end_ms > duration_ms:
            raise QualityContractError(f"fixture {fixture_id} reference timestamps are not monotonic or exceed media duration")
        previous_end = end_ms
        segments.append({"text": text, "begin_ms": begin_ms, "end_ms": end_ms})
    return segments


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityContractError(f"{label} is not a readable JSON object") from exc
    if not isinstance(value, dict):
        raise QualityContractError(f"{label} must be a JSON object")
    return value


def _read_text(path: Path, fixture_id: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise QualityContractError(f"fixture {fixture_id} reference text is unreadable") from exc
    if not value:
        raise QualityContractError(f"fixture {fixture_id} reference text is empty")
    return value


def _relative_path(record: dict[str, object], field: str, location: str) -> str:
    value = _required_string(record, field, location)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise QualityContractError(f"{location} {field} must remain inside the suite/run directory")
    return value


def _safe_file(root: Path, relative: str, location: str, label: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise QualityContractError(f"{location} {label} path escapes its directory") from exc
    return path


def _verify_file_sha256(path: Path, expected: str, location: str, label: str) -> None:
    if not path.is_file():
        raise QualityContractError(f"{location} {label} file is missing")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise QualityContractError(f"{location} {label} hash mismatch")


def _sha256_value(record: dict[str, object], field: str, location: str) -> str:
    value = _required_string(record, field, location).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise QualityContractError(f"{location} has an invalid {field}")
    return value


def _required_string(record: dict[str, object], field: str, location: str) -> str:
    value = str(record.get(field) or "").strip()
    if not value:
        raise QualityContractError(f"{location} is missing {field}")
    return value


def _positive_int(record: dict[str, object], field: str, location: str) -> int:
    value = _positive_or_zero_int(record, field, location)
    if value < 1:
        raise QualityContractError(f"{location} {field} must be positive")
    return value


def _positive_or_zero_int(record: dict[str, object], field: str, location: str) -> int:
    value = record.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise QualityContractError(f"{location} {field} must be a non-negative integer")
    return value


def _normalize_characters(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _english_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", value.casefold())


def _levenshtein(reference: list[str], actual: list[str]) -> int:
    previous = list(range(len(actual) + 1))
    for row, reference_token in enumerate(reference, start=1):
        current = [row]
        for column, actual_token in enumerate(actual, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (reference_token != actual_token),
                )
            )
        previous = current
    return previous[-1]


def _alignment_errors(reference: list[str], actual: list[str]) -> tuple[int, int, int]:
    """回溯编辑矩阵，分别报告替换、删除与插入，便于定位模型问题。"""

    matrix = [[0] * (len(actual) + 1) for _ in range(len(reference) + 1)]
    for row in range(1, len(reference) + 1):
        matrix[row][0] = row
    for column in range(1, len(actual) + 1):
        matrix[0][column] = column
    for row, reference_token in enumerate(reference, start=1):
        for column, actual_token in enumerate(actual, start=1):
            matrix[row][column] = min(
                matrix[row - 1][column] + 1,
                matrix[row][column - 1] + 1,
                matrix[row - 1][column - 1] + (reference_token != actual_token),
            )
    substitutions = deletions = insertions = 0
    row, column = len(reference), len(actual)
    while row or column:
        if row and column and matrix[row][column] == matrix[row - 1][column - 1] + (reference[row - 1] != actual[column - 1]):
            if reference[row - 1] != actual[column - 1]:
                substitutions += 1
            row -= 1
            column -= 1
        elif row and matrix[row][column] == matrix[row - 1][column] + 1:
            deletions += 1
            row -= 1
        else:
            insertions += 1
            column -= 1
    return substitutions, deletions, insertions


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        raise QualityContractError("timestamp metrics have no values")
    index = min(len(values) - 1, max(0, round((len(values) - 1) * quantile)))
    return values[index]


def create_self_test_bundle(root: Path) -> tuple[Path, Path]:
    """创建仅用于脚本自测的合成字节和合法 Artifact，绝不构成真实质量证据。"""

    fixtures_dir = root / "fixtures"
    references_dir = root / "references"
    artifacts_dir = root / "artifacts"
    for directory in (fixtures_dir, references_dir, artifacts_dir):
        directory.mkdir(parents=True, exist_ok=True)
    rows = (
        ("ASR-ZH-DEV-01", "development", "zh", "今天我们检查媒体转写质量。", 75),
        ("ASR-EN-DEV-01", "development", "en", "Today we verify media transcription quality.", 70),
        ("ASR-ZH-DEV-02", "development", "zh", "请保留每一句的开始和结束时间。", 30),
        ("ASR-EN-DEV-02", "development", "en", "The transcript keeps stable sentence timestamps.", 60),
        ("ASR-ZH-DEV-03", "development", "zh", "公开授权素材需要人工校对标注。", 50),
        ("ASR-EN-HOLD-01", "holdout", "en", "The holdout source is never used during tuning.", 55),
        ("ASR-ZH-HOLD-01", "holdout", "zh", "留出集只在冻结配置后运行一次。", 65),
        ("ASR-EN-HOLD-02", "holdout", "en", "A completed task records exactly one provider call.", 75),
    )
    fixtures: list[dict[str, object]] = []
    run_cases: list[dict[str, object]] = []
    for number, (fixture_id, split, language, text, seconds) in enumerate(rows, start=1):
        media_relative = f"fixtures/{fixture_id.lower()}.mp4"
        media_content = f"synthetic-video-{fixture_id}".encode("ascii")
        (root / media_relative).write_bytes(media_content)
        text_relative = f"references/{fixture_id.lower()}.txt"
        segments_relative = f"references/{fixture_id.lower()}.segments.json"
        (root / text_relative).write_text(text, encoding="utf-8")
        segments_payload = {"segments": [{"text": text, "begin_ms": 200, "end_ms": seconds * 1000 - 200}]}
        (root / segments_relative).write_text(json.dumps(segments_payload, ensure_ascii=False), encoding="utf-8")
        media_sha256 = hashlib.sha256(media_content).hexdigest()
        fixtures.append(
            {
                "fixture_id": fixture_id,
                "split": split,
                "language": language,
                "source_kind": "public_licensed",
                "source_ref": f"self-test-source-{number}",
                "source_page": f"https://example.invalid/source/{number}",
                "license": "CC0-1.0 test record",
                "license_url": "https://example.invalid/license",
                "rights_reviewed": True,
                "media_file": media_relative,
                "media_sha256": media_sha256,
                "duration_ms": seconds * 1000,
                "normalized_audio_duration_ms": seconds * 1000,
                "reference_text_file": text_relative,
                "reference_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "reference_segments_file": segments_relative,
                "reference_segments_sha256": hashlib.sha256(
                    (root / segments_relative).read_bytes()
                ).hexdigest(),
                "reference_transcript_reviewed": True,
                "time_annotations_reviewed": True,
            }
        )
        source_id = f"ms_{number:016x}"
        audio_id = f"mda_{number:016x}"
        task_id = f"task_media_transcription_{number:012x}"
        artifact_payload = {
            "schema_version": 1,
            "kind": "media_transcription",
            "task_id": task_id,
            "project_id": "mp_0000000000000001",
            "request": {"source_id": source_id, "audio_id": audio_id, "language_hints": [language]},
            "audio": {
                "audio_id": audio_id,
                "source_id": source_id,
                "source_sha256": media_sha256,
                "source_stream_index": 0,
                "sha256": "a" * 64,
                "size_bytes": 64_000,
                "duration_seconds": float(seconds),
                "sample_rate": 16_000,
                "channels": 1,
                "created_at": "2026-09-24T00:00:00Z",
            },
            "transcript": {
                "text": text,
                "segments": [
                    {
                        "sentence_id": 0,
                        "text": text,
                        "begin_ms": 280,
                        "end_ms": seconds * 1000 - 260,
                        "words": [],
                    }
                ],
            },
            "provider": "qwen_audio",
            "model": "qwen-audio-3.1-asr-flash",
            "provider_request_id_sha256": "b" * 64,
            "provider_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "created_at": "2026-09-24T00:00:00Z",
        }
        artifact_relative = f"artifacts/{fixture_id.lower()}.json"
        artifact_bytes = json.dumps(artifact_payload, ensure_ascii=False).encode("utf-8")
        (root / artifact_relative).write_bytes(artifact_bytes)
        run_cases.append(
            {
                "fixture_id": fixture_id,
                "status": "completed",
                "provider_call_count": 1,
                "artifact_file": artifact_relative,
                "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            }
        )
    suite_payload = {"suite_type": SUITE_TYPE, "fixtures": fixtures}
    suite_path = root / "suite.json"
    suite_path.write_text(json.dumps(suite_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    run_payload = {
        "run_type": RUN_TYPE,
        "suite_sha256": canonical_sha256(suite_payload),
        "route": "media_transcription",
        "provider": "qwen_audio",
        "model": "qwen-audio-3.1-asr-flash",
        "cases": run_cases,
    }
    run_path = root / "run.json"
    run_path.write_text(json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return suite_path, run_path
