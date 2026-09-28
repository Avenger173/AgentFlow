"""离线评估 MM-4 已完成转写 Artifact 的 CER/WER 与时间戳偏差。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from media_transcription_quality import QualityContractError, create_self_test_bundle, evaluate_run


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", type=Path, help="冻结的一次 media_transcription 质量运行清单")
    source.add_argument("--self-test", action="store_true", help="仅以临时合成 Artifact 验证计分器")
    parser.add_argument("--suite", type=Path, help="与 --run 配对的冻结 quality suite.json")
    parser.add_argument(
        "--verify-media-files",
        action="store_true",
        help="额外回读每个源视频并验证 SHA-256；计分器本身不调用网络、ffmpeg 或模型。",
    )
    args = parser.parse_args()
    if not args.self_test and args.suite is None:
        parser.error("--run requires --suite")
    try:
        if args.self_test:
            report = _run_self_test()
        else:
            assert args.suite is not None and args.run is not None
            report = evaluate_run(args.suite, args.run, verify_files=args.verify_media_files)
    except (OSError, QualityContractError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))
    if not args.self_test and report["quality_gate_passed"] is not True:
        raise SystemExit(1)


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_mm4_asr_score_") as temporary:
        root = Path(temporary)
        suite_path, run_path = create_self_test_bundle(root)
        report = evaluate_run(suite_path, run_path, verify_files=True)
        if report["quality_gate_passed"] is not True:
            raise AssertionError("valid synthetic scoring contract did not pass its own fixed thresholds")
        published_suite = json.loads(suite_path.read_text(encoding="utf-8"))
        for fixture in published_suite["fixtures"]:
            fixture["reference_transcript_reviewed"] = False
            fixture["time_annotations_reviewed"] = False
            fixture["reference_provenance"] = "published_benchmark"
            fixture["reference_provenance_url"] = "https://example.invalid/published-asr-benchmark"
        published_suite_path = root / "published_text_suite.json"
        published_suite_path.write_text(json.dumps(published_suite, ensure_ascii=False), encoding="utf-8")
        published_run = json.loads(run_path.read_text(encoding="utf-8"))
        published_run["suite_sha256"] = hashlib.sha256(
            json.dumps(published_suite, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        published_run_path = root / "published_text_run.json"
        published_run_path.write_text(json.dumps(published_run, ensure_ascii=False), encoding="utf-8")
        published_report = evaluate_run(published_suite_path, published_run_path, verify_files=True)
        if (
            published_report["quality_gate"] != "G4-ASR-TEXT-DEV"
            or published_report["quality_gate_passed"] is not True
            or published_report["full_asr_gate_passed"] is not False
            or published_report["timestamp_gate_status"] != "not_evaluated_without_independent_time_annotations"
        ):
            raise AssertionError("published text-only suite was allowed to claim timestamp quality")
        timed_published_suite = json.loads(published_suite_path.read_text(encoding="utf-8"))
        for fixture in timed_published_suite["fixtures"]:
            fixture["time_annotations_reviewed"] = True
            fixture["independent_time_annotation_review"] = {
                "provenance": "independent_human_review",
                "annotation_scope": "source_speech_envelope",
                "source_suite_sha256": published_run["suite_sha256"],
                "review_packet_manifest_sha256": "b" * 64,
                "review_csv_sha256": "c" * 64,
                "reviewer_id_hashes": ["d" * 64],
                "owner_approval_id_hash": "e" * 64,
            }
        timed_published_suite["reviewed_time_annotation_summary"] = {
            "source_suite_sha256": published_run["suite_sha256"],
        }
        timed_published_suite_path = root / "published_timed_suite.json"
        timed_published_suite_path.write_text(json.dumps(timed_published_suite, ensure_ascii=False), encoding="utf-8")
        timed_published_report = evaluate_run(timed_published_suite_path, published_run_path, verify_files=True)
        if (
            timed_published_report["quality_gate"] != "G4-ASR-DEV"
            or timed_published_report["full_asr_gate_passed"] is not True
            or timed_published_report["timestamp_gate_status"] != "scored"
            or timed_published_report["run"]["suite_binding"] != "pre_annotation_source_suite"
        ):
            raise AssertionError("reviewed timing suite did not safely reuse its blind pre-annotation ASR run")
        mismatched_timing_suite = json.loads(json.dumps(timed_published_suite))
        mismatched_timing_suite["reviewed_time_annotation_summary"]["source_suite_sha256"] = "f" * 64
        mismatched_timing_suite_path = root / "published_timed_suite_mismatch.json"
        mismatched_timing_suite_path.write_text(
            json.dumps(mismatched_timing_suite, ensure_ascii=False), encoding="utf-8"
        )
        try:
            evaluate_run(mismatched_timing_suite_path, published_run_path, verify_files=False)
        except QualityContractError as exc:
            if "does not match" not in str(exc):
                raise
        else:
            raise AssertionError("quality evaluator accepted mismatched reviewed-time source binding")
        invalid = json.loads(run_path.read_text(encoding="utf-8"))
        invalid["cases"][0]["provider_call_count"] = 2
        invalid_path = root / "invalid_run.json"
        invalid_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
        try:
            evaluate_run(suite_path, invalid_path, verify_files=False)
        except QualityContractError as exc:
            if "zero or one Provider call" not in str(exc):
                raise
        else:
            raise AssertionError("quality evaluator accepted a replayed paid request")
        low_quality = json.loads(run_path.read_text(encoding="utf-8"))
        first_case = low_quality["cases"][0]
        artifact_path = root / str(first_case["artifact_file"])
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["transcript"]["text"] = "unrelated output"
        artifact["transcript"]["segments"][0]["begin_ms"] = 8_000
        artifact["transcript"]["segments"][0]["end_ms"] = 70_000
        low_quality_artifact = root / "artifacts" / "low_quality.json"
        encoded_artifact = json.dumps(artifact, ensure_ascii=False).encode("utf-8")
        low_quality_artifact.write_bytes(encoded_artifact)
        first_case["artifact_file"] = "artifacts/low_quality.json"
        first_case["artifact_sha256"] = hashlib.sha256(encoded_artifact).hexdigest()
        low_quality_path = root / "low_quality_run.json"
        low_quality_path.write_text(json.dumps(low_quality, ensure_ascii=False), encoding="utf-8")
        low_quality_report = evaluate_run(suite_path, low_quality_path, verify_files=False)
        if low_quality_report["quality_gate_passed"] is not False:
            raise AssertionError("quality evaluator accepted an unrelated transcript and late timestamp as passing")
        incomplete = json.loads(run_path.read_text(encoding="utf-8"))
        incomplete["cases"][0] = {
            "fixture_id": incomplete["cases"][0]["fixture_id"],
            "status": "outcome_unknown",
            "failure_category": "provider_outcome_unknown",
            "provider_call_count": 1,
        }
        incomplete_path = root / "incomplete_run.json"
        incomplete_path.write_text(json.dumps(incomplete, ensure_ascii=False), encoding="utf-8")
        incomplete_report = evaluate_run(suite_path, incomplete_path, verify_files=False)
        if incomplete_report["quality_gate_passed"] is not False:
            raise AssertionError("quality evaluator accepted an unknown Provider outcome as passing")
        if incomplete_report["incomplete_case_statuses"] != [
            {
                "fixture_id": incomplete["cases"][0]["fixture_id"],
                "status": "outcome_unknown",
                "failure_category": "provider_outcome_unknown",
            }
        ]:
            raise AssertionError("quality evaluator did not report the incomplete Provider outcome")
        inconsistent = json.loads(run_path.read_text(encoding="utf-8"))
        inconsistent["cases"][0] = {
            "fixture_id": inconsistent["cases"][0]["fixture_id"],
            "status": "failed",
            "failure_category": "provider_rejected",
            "provider_call_count": 0,
        }
        inconsistent_path = root / "inconsistent_run.json"
        inconsistent_path.write_text(json.dumps(inconsistent, ensure_ascii=False), encoding="utf-8")
        try:
            evaluate_run(suite_path, inconsistent_path, verify_files=False)
        except QualityContractError as exc:
            if "inconsistent Provider call count" not in str(exc):
                raise
        else:
            raise AssertionError("quality evaluator accepted an underreported rejected Provider call")
    report["self_test"] = True
    report["negative_contract_check"] = "published_text_without_review_cannot_claim_timestamp_quality_reviewed_time_requires_matching_source_binding_provider_replay_low_quality_unknown_underreported_failure"
    return report


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
