"""在真实 Provider 调用前验证 MM-4 短媒体 ASR 质量集。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from media_transcription_quality import QualityContractError, create_self_test_bundle, validate_suite


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--suite", type=Path, help="冻结的 agentflow-mm4-asr-quality-suite-v1 suite.json")
    source.add_argument("--self-test", action="store_true", help="仅以临时合成文件验证质量集契约")
    parser.add_argument(
        "--verify-media-files",
        action="store_true",
        help="额外回读每个视频文件并核对其 SHA-256；不会调用 ffmpeg、网络或模型。",
    )
    args = parser.parse_args()
    try:
        if args.self_test:
            report = _run_self_test()
        else:
            assert args.suite is not None
            report, _ = validate_suite(args.suite, verify_files=args.verify_media_files)
    except (OSError, QualityContractError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_mm4_asr_suite_") as temporary:
        root = Path(temporary)
        suite_path, _ = create_self_test_bundle(root)
        report, _ = validate_suite(suite_path, verify_files=True)
        invalid = json.loads(suite_path.read_text(encoding="utf-8"))
        invalid["fixtures"][0]["source_kind"] = "program_generated"
        invalid_path = root / "invalid_suite.json"
        invalid_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
        try:
            validate_suite(invalid_path, verify_files=False)
        except QualityContractError as exc:
            if "public licensed source" not in str(exc):
                raise
        else:
            raise AssertionError("quality suite accepted a program-generated fixture as content-quality evidence")
        published = json.loads(suite_path.read_text(encoding="utf-8"))
        for fixture in published["fixtures"]:
            fixture["reference_transcript_reviewed"] = False
            fixture["time_annotations_reviewed"] = False
            fixture["reference_provenance"] = "published_benchmark"
            fixture["reference_provenance_url"] = "https://example.invalid/published-asr-benchmark"
        published_path = root / "published_benchmark_suite.json"
        published_path.write_text(json.dumps(published, ensure_ascii=False), encoding="utf-8")
        published_report, _ = validate_suite(published_path, verify_files=True)
        if published_report["fixture_reference_provenance_counts"] != {"published_benchmark": 8}:
            raise AssertionError("quality suite did not retain published benchmark reference provenance")
        published_with_unproven_timing = json.loads(published_path.read_text(encoding="utf-8"))
        for fixture in published_with_unproven_timing["fixtures"]:
            fixture["time_annotations_reviewed"] = True
        unproven_timing_path = root / "published_unproven_timing_suite.json"
        unproven_timing_path.write_text(json.dumps(published_with_unproven_timing, ensure_ascii=False), encoding="utf-8")
        try:
            validate_suite(unproven_timing_path, verify_files=False)
        except QualityContractError as exc:
            if "independent review provenance" not in str(exc):
                raise
        else:
            raise AssertionError("published text with unproven timing was accepted as timestamp-ready")
        published_with_timing = json.loads(published_path.read_text(encoding="utf-8"))
        for fixture in published_with_timing["fixtures"]:
            fixture["time_annotations_reviewed"] = True
            fixture["independent_time_annotation_review"] = {
                "provenance": "independent_human_review",
                "annotation_scope": "source_speech_envelope",
                "source_suite_sha256": "a" * 64,
                "review_packet_manifest_sha256": "b" * 64,
                "review_csv_sha256": "c" * 64,
                "reviewer_id_hashes": ["d" * 64],
                "owner_approval_id_hash": "e" * 64,
            }
        timed_path = root / "published_reviewed_timing_suite.json"
        timed_path.write_text(json.dumps(published_with_timing, ensure_ascii=False), encoding="utf-8")
        timed_report, _ = validate_suite(timed_path, verify_files=True)
        if timed_report["timestamp_reference_status"] != "ready":
            raise AssertionError("published benchmark text with independent timing was not timestamp-ready")
    report["self_test"] = True
    report["negative_contract_check"] = "program_generated_fixture_and_unproven_published_timing_rejected_independent_time_review_traceable"
    return report


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
