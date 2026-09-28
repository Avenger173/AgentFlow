"""将完整的人工时间审核包冻结为可评分的 G4-ASR-DEV 质量集。

发布基准文本仍保留其公开来源；本脚本只在审核包已完整且与原冻结媒体一致时，
把人工填写的时间边界写入新的不可覆盖质量集。它不调用网络、FFmpeg 或模型，也
不会修改原质量集、审核包或已有 Provider 运行结果。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from media_transcription_quality import QualityContractError, canonical_sha256, create_self_test_bundle, validate_suite
from prepare_media_transcription_timestamp_review_packet import (
    _read_json,
    _require_ignored_output_directory,
    _safe_source_file,
    _write_json,
    _write_review_csv,
    build_review_packet,
)
from verify_media_transcription_timestamp_review_packet import verify_review_packet


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def main() -> None:
    parser = argparse.ArgumentParser(description="冻结完整 G4-ASR-DEV 人工时间标注")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-suite", type=Path, help="原始发布文本质量集 suite.json")
    source.add_argument("--self-test", action="store_true", help="仅以临时合成夹具验证冻结协议")
    parser.add_argument("--review-packet-dir", type=Path, help="完整人工时间标注审核包目录")
    parser.add_argument("--output-dir", type=Path, help="新的 data/ 下已审时间质量集目录")
    parser.add_argument("--approval-id", help="项目负责人确认的非个人、可审计标识")
    args = parser.parse_args()
    if not args.self_test:
        if args.review_packet_dir is None or args.output_dir is None or not args.approval_id:
            parser.error("--source-suite requires --review-packet-dir, --output-dir and --approval-id")
    try:
        if args.self_test:
            report = _run_self_test()
        else:
            assert args.source_suite is not None and args.review_packet_dir is not None and args.output_dir is not None
            assert args.approval_id is not None
            report = freeze_reviewed_time_suite(
                args.source_suite,
                args.review_packet_dir,
                args.output_dir,
                approval_id=args.approval_id,
            )
    except (OSError, QualityContractError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))


def freeze_reviewed_time_suite(
    source_suite: Path,
    review_packet_dir: Path,
    output_dir: Path,
    *,
    approval_id: str,
    enforce_data_directory: bool = True,
) -> dict[str, object]:
    """冻结经审核的时间真值，拒绝空包、篡改包和已有输出目录。"""

    approval_id = _require_approval_id(approval_id)
    source_suite = source_suite.resolve()
    review_packet_dir = review_packet_dir.resolve()
    output_dir = output_dir.expanduser().resolve()
    if enforce_data_directory:
        _require_ignored_output_directory(output_dir)
    if output_dir.exists():
        raise RuntimeError("reviewed time suite output already exists; refuse to overwrite frozen evidence")
    review_report = verify_review_packet(review_packet_dir, source_suite)
    if review_report["review_state"] != "complete":
        raise QualityContractError("timestamp review packet is incomplete and cannot be frozen")
    source_payload = _read_json(source_suite, label="source quality suite")
    source_report, fixtures = validate_suite(source_suite, verify_files=True)
    source_fixtures = _source_fixtures_by_id(source_payload)
    review_rows = _read_complete_review_rows(review_packet_dir / "review.csv")
    review_rows_by_fixture = _review_rows_by_fixture(review_rows, fixtures)
    review_csv_sha256 = _sha256_file(review_packet_dir / "review.csv")
    packet_manifest_sha256 = _sha256_file(review_packet_dir / "packet.json")
    reviewer_hashes = sorted({_hash_identifier(str(row["reviewer_id"])) for row in review_rows})
    review_metadata = {
        "provenance": "independent_human_review",
        "source_suite_sha256": source_report["suite_sha256"],
        "review_packet_manifest_sha256": packet_manifest_sha256,
        "review_csv_sha256": review_csv_sha256,
        "reviewer_id_hashes": reviewer_hashes,
        "owner_approval_id_hash": _hash_identifier(approval_id),
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging_", dir=output_dir.parent))
    try:
        media_dir = staging_dir / "media"
        references_dir = staging_dir / "references"
        media_dir.mkdir()
        references_dir.mkdir()
        frozen_fixtures: list[dict[str, object]] = []
        for fixture_id in sorted(fixtures):
            fixture = fixtures[fixture_id]
            raw_fixture = dict(source_fixtures[fixture_id])
            media_source = _safe_source_file(source_suite.parent, fixture.media_file, fixture_id, "media")
            text_source = _safe_source_file(
                source_suite.parent,
                _required_raw_string(raw_fixture, "reference_text_file", fixture_id),
                fixture_id,
                "reference text",
            )
            media_relative = Path("media") / media_source.name
            text_relative = Path("references") / text_source.name
            segment_relative = Path("references") / f"{Path(text_source.name).stem}.segments.json"
            copied_media = staging_dir / media_relative
            copied_text = staging_dir / text_relative
            shutil.copy2(media_source, copied_media)
            shutil.copy2(text_source, copied_text)
            _verify_sha256(copied_media, fixture.media_sha256, fixture_id, "copied media")
            source_text_sha256 = _required_raw_string(raw_fixture, "reference_text_sha256", fixture_id).lower()
            _verify_sha256(copied_text, source_text_sha256, fixture_id, "copied reference text")
            segments_payload = {
                "segments": [
                    {
                        "text": row["reference_text"],
                        "begin_ms": int(row["begin_ms"]),
                        "end_ms": int(row["end_ms"]),
                    }
                    for row in review_rows_by_fixture[fixture_id]
                ]
            }
            segment_path = staging_dir / segment_relative
            _write_json(segment_path, segments_payload)
            raw_fixture["media_file"] = media_relative.as_posix()
            raw_fixture["reference_text_file"] = text_relative.as_posix()
            raw_fixture["reference_segments_file"] = segment_relative.as_posix()
            raw_fixture["reference_segments_sha256"] = _sha256_file(segment_path)
            raw_fixture["time_annotations_reviewed"] = True
            raw_fixture["independent_time_annotation_review"] = review_metadata
            frozen_fixtures.append(raw_fixture)
        frozen_payload = {
            "suite_type": source_payload["suite_type"],
            "fixtures": frozen_fixtures,
            "reviewed_time_annotation_summary": {
                "source_suite_sha256": source_report["suite_sha256"],
                "review_packet_manifest_sha256": packet_manifest_sha256,
                "review_csv_sha256": review_csv_sha256,
                "reviewer_count": len(reviewer_hashes),
                "approval_id_hash": review_metadata["owner_approval_id_hash"],
            },
        }
        frozen_suite = staging_dir / "suite.json"
        _write_json(frozen_suite, frozen_payload)
        frozen_report, _ = validate_suite(frozen_suite, verify_files=True)
        if frozen_report["timestamp_reference_status"] != "ready":
            raise RuntimeError("frozen reviewed time suite did not become timestamp-scoreable")
        staging_dir.replace(output_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return {
        "ok": True,
        "suite_path": str(output_dir / "suite.json"),
        "suite_sha256": canonical_sha256(frozen_payload),
        "source_suite_sha256": source_report["suite_sha256"],
        "fixture_count": len(frozen_fixtures),
        "segment_count": len(review_rows),
        "timestamp_reference_status": "ready",
        "reviewer_count": len(reviewer_hashes),
        "model_call_count": 0,
        "network_call_count": 0,
        "quality_gate_effect": "none; this freezes reviewed time references but does not reuse or execute a Provider run",
    }


def _source_fixtures_by_id(source_payload: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_fixtures = source_payload.get("fixtures")
    if not isinstance(raw_fixtures, list):
        raise QualityContractError("source quality suite fixtures must be a list")
    result: dict[str, dict[str, object]] = {}
    for raw in raw_fixtures:
        if not isinstance(raw, dict):
            raise QualityContractError("source quality suite fixture must be an object")
        fixture_id = _required_raw_string(raw, "fixture_id", "source fixture")
        if fixture_id in result:
            raise QualityContractError("source quality suite has duplicate fixture identifiers")
        result[fixture_id] = raw
    return result


def _read_complete_review_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError) as exc:
        raise QualityContractError("completed timestamp review CSV is unreadable") from exc
    if not rows:
        raise QualityContractError("completed timestamp review CSV is empty")
    for row in rows:
        if row.get("review_status") != "accepted" or not row.get("reviewer_id"):
            raise QualityContractError("timestamp review CSV has an incomplete row")
    return rows


def _review_rows_by_fixture(
    review_rows: list[dict[str, str]], fixtures: dict[str, object]
) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {fixture_id: [] for fixture_id in fixtures}
    for row in review_rows:
        fixture_id = str(row.get("fixture_id") or "")
        if fixture_id not in grouped:
            raise QualityContractError("timestamp review CSV references an unknown fixture")
        grouped[fixture_id].append(row)
    for fixture_id, rows in grouped.items():
        if not rows:
            raise QualityContractError(f"timestamp review CSV has no rows for {fixture_id}")
        rows.sort(key=lambda item: int(item["segment_index"]))
    return grouped


def _require_approval_id(value: str) -> str:
    cleaned = value.strip()
    if not _IDENTIFIER_PATTERN.fullmatch(cleaned):
        raise QualityContractError("approval_id must be a non-personal identifier using letters, digits, dot, dash or underscore")
    return cleaned


def _required_raw_string(raw: dict[str, object], field: str, location: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise QualityContractError(f"{location} is missing {field}")
    return value.strip()


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(f"agentflow-mm4-review-v1:{value}".encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise QualityContractError(f"required evidence file is missing: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_sha256(path: Path, expected: str, fixture_id: str, label: str) -> None:
    if _sha256_file(path) != expected:
        raise QualityContractError(f"{fixture_id} {label} hash mismatch")


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_mm4_asr_freeze_review_") as temporary:
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
        try:
            freeze_reviewed_time_suite(
                published_suite,
                packet_dir,
                root / "incomplete_output",
                approval_id="owner_test",
                enforce_data_directory=False,
            )
        except QualityContractError as exc:
            if "incomplete" not in str(exc):
                raise
        else:
            raise AssertionError("freezer accepted an incomplete timestamp review packet")
        with (packet_dir / "review.csv").open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        duration_by_fixture = {str(item["fixture_id"]): int(item["duration_ms"]) for item in source_payload["fixtures"]}
        for row in rows:
            row["begin_ms"] = "100"
            row["end_ms"] = str(duration_by_fixture[str(row["fixture_id"])] - 100)
            row["reviewer_id"] = "reviewer_a"
            row["review_status"] = "accepted"
        _write_review_csv(packet_dir / "review.csv", rows)
        output_dir = root / "frozen"
        report = freeze_reviewed_time_suite(
            published_suite,
            packet_dir,
            output_dir,
            approval_id="owner_test",
            enforce_data_directory=False,
        )
        frozen_report, _ = validate_suite(output_dir / "suite.json", verify_files=True)
        if report["timestamp_reference_status"] != "ready" or frozen_report["timestamp_reference_status"] != "ready":
            raise AssertionError("completed timestamp review did not become scoreable")
        frozen_text = (output_dir / "suite.json").read_text(encoding="utf-8")
        if "reviewer_a" in frozen_text or "owner_test" in frozen_text:
            raise AssertionError("frozen suite exposed raw review identifiers")
    return {
        "ok": True,
        "self_test": True,
        "model_call_count": 0,
        "network_call_count": 0,
        "negative_contract_check": "incomplete_review_rejected_and_completed_published_text_time_review_frozen_without_raw_identifiers",
    }


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
