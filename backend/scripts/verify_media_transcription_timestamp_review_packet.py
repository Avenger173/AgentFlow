"""校验 G4-ASR-DEV 人工时间标注审核包的完整性与冻结来源一致性。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import tempfile
from pathlib import Path
from media_transcription_quality import FixtureRecord, QualityContractError, create_self_test_bundle, validate_suite
from prepare_media_transcription_timestamp_review_packet import (
    REVIEW_HEADERS,
    REVIEW_PACKET_TYPE,
    _read_json,
    _write_json,
    _write_review_csv,
    build_review_packet,
)


_INTEGER_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")
_REVIEWER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 G4-ASR-DEV 人工时间标注审核包")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--packet-dir", type=Path, help="data/ 下的时间标注审核包")
    source.add_argument("--self-test", action="store_true", help="仅以临时合成夹具验证审核包校验器")
    parser.add_argument("--source-suite", type=Path, help="生成审核包时使用的原始冻结 quality suite.json")
    parser.add_argument("--require-complete", action="store_true", help="缺少已接受的人工时间标注时返回非零")
    args = parser.parse_args()
    if not args.self_test and args.source_suite is None:
        parser.error("--packet-dir requires --source-suite so copied media is anchored to the frozen source")
    try:
        if args.self_test:
            report = _run_self_test()
        else:
            assert args.packet_dir is not None and args.source_suite is not None
            report = verify_review_packet(args.packet_dir, args.source_suite)
    except (OSError, QualityContractError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))
    if not args.self_test and args.require_complete and report["review_state"] != "complete":
        raise SystemExit(1)


def verify_review_packet(packet_dir: Path, source_suite: Path) -> dict[str, object]:
    """以原始冻结质量集为锚点，验证审核包副本和 CSV 的不可变字段。"""

    packet_dir = packet_dir.resolve()
    source_suite = source_suite.resolve()
    if not packet_dir.is_dir():
        raise QualityContractError("timestamp review packet directory is missing")
    source_payload = _read_json(source_suite, label="source quality suite")
    source_report, fixtures = validate_suite(source_suite, verify_files=True)
    _require_published_text_only_source(source_payload, fixtures)
    manifest = _read_json(packet_dir / "packet.json", label="timestamp review packet manifest")
    if manifest.get("packet_type") != REVIEW_PACKET_TYPE:
        raise QualityContractError("timestamp review packet has an unsupported packet_type")
    review_contract = manifest.get("review_contract")
    if not isinstance(review_contract, dict) or review_contract.get("annotation_scope") != "source_speech_envelope":
        raise QualityContractError("timestamp review packet must use the source speech envelope review scope")
    if manifest.get("source_suite_sha256") != source_report["suite_sha256"]:
        raise QualityContractError("timestamp review packet does not match the frozen source suite")
    if manifest.get("fixture_count") != len(fixtures):
        raise QualityContractError("timestamp review packet fixture count does not match source suite")
    packet_fixtures = manifest.get("fixtures")
    if not isinstance(packet_fixtures, list) or len(packet_fixtures) != len(fixtures):
        raise QualityContractError("timestamp review packet fixture manifest is incomplete")
    expected_rows = _verify_packet_fixtures(packet_dir, packet_fixtures, source_payload, fixtures)
    rows = _read_and_validate_csv(packet_dir / "review.csv", expected_rows, fixtures)
    completed_rows = [row for row in rows if row["completed"]]
    review_state = "complete" if len(completed_rows) == len(rows) else "incomplete"
    return {
        "ok": True,
        "packet_dir": str(packet_dir),
        "source_suite_sha256": source_report["suite_sha256"],
        "fixture_count": len(fixtures),
        "segment_count": len(rows),
        "completed_segment_count": len(completed_rows),
        "review_state": review_state,
        "model_call_count": 0,
        "network_call_count": 0,
        "independence_status": "requires project-owner confirmation; file validation cannot prove reviewer identity or process independence",
        "quality_gate_effect": "none; a complete packet is only eligible input for a separately frozen reviewed-time suite and fixed ASR run",
    }


def _require_published_text_only_source(source_payload: dict[str, object], fixtures: dict[str, FixtureRecord]) -> None:
    raw_fixtures = source_payload.get("fixtures")
    if not isinstance(raw_fixtures, list):
        raise QualityContractError("source quality suite fixtures must be a list")
    raw_by_id = {
        str(item.get("fixture_id")): item for item in raw_fixtures if isinstance(item, dict) and item.get("fixture_id")
    }
    if set(raw_by_id) != set(fixtures):
        raise QualityContractError("source quality suite fixture identifiers are inconsistent")
    for fixture_id, fixture in fixtures.items():
        raw = raw_by_id[fixture_id]
        if fixture.reference_provenance != "published_benchmark":
            raise QualityContractError("timestamp review packet source must use published benchmark text")
        if raw.get("reference_transcript_reviewed") is not False or raw.get("time_annotations_reviewed") is not False:
            raise QualityContractError(f"{fixture_id} source is not an unreviewed published-text-only fixture")


def _verify_packet_fixtures(
    packet_dir: Path,
    packet_fixtures: list[object],
    source_payload: dict[str, object],
    fixtures: dict[str, FixtureRecord],
) -> dict[tuple[str, int], dict[str, object]]:
    manifest_by_id: dict[str, dict[str, object]] = {}
    for raw in packet_fixtures:
        if not isinstance(raw, dict):
            raise QualityContractError("timestamp review packet fixture must be an object")
        fixture_id = _required_string(raw, "fixture_id", "packet fixture")
        if fixture_id in manifest_by_id or fixture_id not in fixtures:
            raise QualityContractError("timestamp review packet has unknown or duplicate fixture_id")
        manifest_by_id[fixture_id] = raw
    if set(manifest_by_id) != set(fixtures):
        raise QualityContractError("timestamp review packet does not cover every frozen fixture")
    expected_rows: dict[tuple[str, int], dict[str, object]] = {}
    for fixture_id, fixture in fixtures.items():
        raw = manifest_by_id[fixture_id]
        for field, expected in (
            ("split", fixture.split),
            ("language", fixture.language),
            ("source_ref", fixture.source_ref),
            ("duration_ms", fixture.duration_ms),
            ("media_sha256", fixture.media_sha256),
        ):
            if raw.get(field) != expected:
                raise QualityContractError(f"{fixture_id} packet {field} does not match frozen source")
        source_text_sha256 = _source_fixture_value(source_payload, fixture_id, "reference_text_sha256")
        source_segments_sha256 = _source_fixture_value(source_payload, fixture_id, "reference_segments_sha256")
        if raw.get("reference_text_sha256") != source_text_sha256 or raw.get("source_reference_segments_sha256") != source_segments_sha256:
            raise QualityContractError(f"{fixture_id} packet reference hashes do not match frozen source")
        media_path = _safe_packet_file(packet_dir, _required_string(raw, "media_file", fixture_id), fixture_id, "copied media")
        text_path = _safe_packet_file(
            packet_dir, _required_string(raw, "reference_text_file", fixture_id), fixture_id, "copied reference text"
        )
        _verify_file_sha256(media_path, fixture.media_sha256, fixture_id, "copied media")
        _verify_file_sha256(text_path, source_text_sha256, fixture_id, "copied reference text")
        segments = raw.get("expected_segments")
        if not isinstance(segments, list) or len(segments) != 1:
            raise QualityContractError(f"{fixture_id} packet must contain exactly one source speech envelope")
        segment = segments[0]
        text_sha256 = _sha256_text(fixture.reference_text)
        if not isinstance(segment, dict) or segment.get("segment_index") != 1:
            raise QualityContractError(f"{fixture_id} packet source envelope index is invalid")
        if segment.get("reference_text_sha256") != text_sha256:
            raise QualityContractError(f"{fixture_id} packet source envelope text hash does not match frozen source")
        expected_rows[(fixture_id, 1)] = {
            "fixture": fixture,
            "text": fixture.reference_text,
            "reference_text_sha256": text_sha256,
        }
    return expected_rows


def _read_and_validate_csv(
    path: Path,
    expected_rows: dict[tuple[str, int], dict[str, object]],
    fixtures: dict[str, FixtureRecord],
) -> list[dict[str, object]]:
    if not path.is_file():
        raise QualityContractError("timestamp review CSV is missing")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != REVIEW_HEADERS:
                raise QualityContractError("timestamp review CSV headers were modified")
            raw_rows = list(reader)
    except UnicodeDecodeError as exc:
        raise QualityContractError("timestamp review CSV must remain UTF-8") from exc
    if len(raw_rows) != len(expected_rows):
        raise QualityContractError("timestamp review CSV row count does not match packet manifest")
    seen: set[tuple[str, int]] = set()
    parsed_rows: list[dict[str, object]] = []
    per_fixture_last_end: dict[str, int] = {}
    for row_number, row in enumerate(raw_rows, start=2):
        if set(row) != set(REVIEW_HEADERS) or any(value is None for value in row.values()):
            raise QualityContractError(f"timestamp review CSV row {row_number} has malformed columns")
        fixture_id = str(row["fixture_id"] or "")
        index = _strict_nonnegative_int(str(row["segment_index"] or ""), f"CSV row {row_number} segment_index")
        key = (fixture_id, index)
        expected = expected_rows.get(key)
        if expected is None or key in seen:
            raise QualityContractError(f"timestamp review CSV row {row_number} has unknown or duplicate segment")
        seen.add(key)
        fixture = expected["fixture"]
        assert isinstance(fixture, FixtureRecord)
        if row["language"] != fixture.language:
            raise QualityContractError(f"timestamp review CSV row {row_number} language was modified")
        if row["reference_text"] != expected["text"] or row["reference_text_sha256"] != expected["reference_text_sha256"]:
            raise QualityContractError(f"timestamp review CSV row {row_number} reference text was modified")
        begin_value = str(row["begin_ms"] or "")
        end_value = str(row["end_ms"] or "")
        reviewer_id = str(row["reviewer_id"] or "")
        review_status = str(row["review_status"] or "")
        if bool(begin_value) != bool(end_value):
            raise QualityContractError(f"timestamp review CSV row {row_number} has only one time boundary")
        if not begin_value:
            if reviewer_id or review_status != "pending":
                raise QualityContractError(f"timestamp review CSV row {row_number} incomplete row must remain pending without reviewer")
            completed = False
        else:
            begin_ms = _strict_nonnegative_int(begin_value, f"CSV row {row_number} begin_ms")
            end_ms = _strict_nonnegative_int(end_value, f"CSV row {row_number} end_ms")
            if begin_ms >= end_ms or end_ms > fixture.duration_ms:
                raise QualityContractError(f"timestamp review CSV row {row_number} has out-of-range time boundaries")
            if not _REVIEWER_PATTERN.fullmatch(reviewer_id) or review_status != "accepted":
                raise QualityContractError(f"timestamp review CSV row {row_number} completed row requires accepted status and reviewer id")
            previous_end = per_fixture_last_end.get(fixture_id)
            if previous_end is not None and begin_ms < previous_end:
                raise QualityContractError(f"timestamp review CSV row {row_number} overlaps its prior segment")
            per_fixture_last_end[fixture_id] = end_ms
            completed = True
        parsed_rows.append({"fixture_id": fixture_id, "segment_index": index, "completed": completed})
    if seen != set(expected_rows):
        raise QualityContractError("timestamp review CSV does not cover every packet segment")
    return parsed_rows


def _source_fixture_value(source_payload: dict[str, object], fixture_id: str, field: str) -> object:
    raw_fixtures = source_payload.get("fixtures")
    assert isinstance(raw_fixtures, list)
    for raw in raw_fixtures:
        if isinstance(raw, dict) and raw.get("fixture_id") == fixture_id:
            if field in raw:
                return raw[field]
    raise QualityContractError(f"{fixture_id} source suite is missing {field}")


def _required_string(record: dict[str, object], field: str, location: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise QualityContractError(f"{location} is missing {field}")
    return value.strip()


def _safe_packet_file(packet_dir: Path, relative: str, fixture_id: str, label: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise QualityContractError(f"{fixture_id} {label} path escapes review packet")
    resolved = (packet_dir / path).resolve()
    try:
        resolved.relative_to(packet_dir)
    except ValueError as exc:
        raise QualityContractError(f"{fixture_id} {label} path escapes review packet") from exc
    return resolved


def _verify_file_sha256(path: Path, expected: str, fixture_id: str, label: str) -> None:
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise QualityContractError(f"{fixture_id} {label} hash mismatch")


def _strict_nonnegative_int(value: str, label: str) -> int:
    if not _INTEGER_PATTERN.fullmatch(value):
        raise QualityContractError(f"{label} must be a non-negative integer")
    return int(value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_mm4_asr_timestamp_verify_") as temporary:
        root = Path(temporary)
        suite_path, _ = create_self_test_bundle(root / "source")
        source_payload = _read_json(suite_path, label="self-test source suite")
        for fixture in source_payload["fixtures"]:
            assert isinstance(fixture, dict)
            fixture["reference_provenance"] = "published_benchmark"
            fixture["reference_provenance_url"] = "https://example.invalid/published-benchmark"
            fixture["reference_transcript_reviewed"] = False
            fixture["time_annotations_reviewed"] = False
        published_suite = suite_path.parent / "published_suite.json"
        _write_json(published_suite, source_payload)
        packet_dir = root / "packet"
        build_review_packet(published_suite, packet_dir, enforce_data_directory=False)
        incomplete = verify_review_packet(packet_dir, published_suite)
        if incomplete["review_state"] != "incomplete" or incomplete["completed_segment_count"] != 0:
            raise AssertionError("blank review packet must not claim completed time annotations")
        review_csv = packet_dir / "review.csv"
        with review_csv.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        duration_by_id = {str(item["fixture_id"]): int(item["duration_ms"]) for item in source_payload["fixtures"]}
        for row in rows:
            duration = duration_by_id[str(row["fixture_id"])]
            row["begin_ms"] = "100"
            row["end_ms"] = str(duration - 100)
            row["reviewer_id"] = "reviewer_a"
            row["review_status"] = "accepted"
        _write_review_csv(review_csv, rows)
        complete = verify_review_packet(packet_dir, published_suite)
        if complete["review_state"] != "complete" or complete["completed_segment_count"] != complete["segment_count"]:
            raise AssertionError("valid completed review packet was rejected")
        packet_manifest = _read_json(packet_dir / "packet.json", label="self-test packet manifest")
        copied_media = packet_dir / str(packet_manifest["fixtures"][0]["media_file"])
        copied_media.write_bytes(b"tampered-media")
        try:
            verify_review_packet(packet_dir, published_suite)
        except QualityContractError as exc:
            if "copied media hash mismatch" not in str(exc):
                raise
        else:
            raise AssertionError("review validator accepted altered copied media")
        source_fixture = packet_manifest["fixtures"][0]
        fixture_id = str(source_fixture["fixture_id"])
        source_media = suite_path.parent / "fixtures" / f"{fixture_id.lower()}.mp4"
        copied_media.write_bytes(source_media.read_bytes())
        rows[0]["reference_text"] = "tampered"
        _write_review_csv(review_csv, rows)
        try:
            verify_review_packet(packet_dir, published_suite)
        except QualityContractError as exc:
            if "reference text was modified" not in str(exc):
                raise
        else:
            raise AssertionError("review validator accepted altered reference text")
    return {
        "ok": True,
        "self_test": True,
        "model_call_count": 0,
        "network_call_count": 0,
        "negative_contract_check": "one_source_envelope_per_media_incomplete_review_altered_reference_and_out_of-band_time_reference_rejected",
    }


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
