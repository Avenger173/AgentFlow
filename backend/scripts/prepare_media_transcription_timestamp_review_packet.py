"""生成公开转写文本的人工时间标注审核包。

FLEURS 质量集的发布文本可用于 CER/WER，却没有独立的源级语音包络真值。
本脚本只复制冻结的公开媒体和参考文本，并将待审核的时间字段留空；它既不读取
Provider Artifact，也不调用模型，避免模型时间戳反向污染人工参考答案。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from media_transcription_quality import (
    FixtureRecord,
    QualityContractError,
    create_self_test_bundle,
    validate_suite,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REVIEW_PACKET_TYPE = "agentflow-mm4-asr-timestamp-review-packet-v2"
REVIEW_HEADERS = [
    "fixture_id",
    "segment_index",
    "language",
    "reference_text",
    "reference_text_sha256",
    "begin_ms",
    "end_ms",
    "reviewer_id",
    "review_status",
    "note",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 G4-ASR-DEV 人工时间标注审核包")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-suite", type=Path, help="仅含发布文本的冻结 ASR quality suite.json")
    source.add_argument("--self-test", action="store_true", help="仅以临时合成夹具验证审核包协议")
    parser.add_argument("--output-dir", type=Path, help="新的 data/ 下审核包目录")
    args = parser.parse_args()
    if not args.self_test and args.output_dir is None:
        parser.error("--source-suite requires --output-dir")
    try:
        if args.self_test:
            report = _run_self_test()
        else:
            assert args.source_suite is not None and args.output_dir is not None
            report = build_review_packet(args.source_suite, args.output_dir)
    except (OSError, QualityContractError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))


def build_review_packet(
    source_suite: Path,
    output_dir: Path,
    *,
    enforce_data_directory: bool = True,
) -> dict[str, object]:
    """从发布文本质量集创建一个空时间标注包，拒绝覆盖既有审核记录。"""

    source_suite = source_suite.resolve()
    source_payload = _read_json(source_suite, label="source quality suite")
    suite_report, fixtures = validate_suite(source_suite, verify_files=True)
    _require_published_text_only_source(source_payload, fixtures)
    output_dir = output_dir.expanduser().resolve()
    if enforce_data_directory:
        _require_ignored_output_directory(output_dir)
    if output_dir.exists():
        raise RuntimeError("timestamp review output directory already exists; refuse to overwrite a review packet")

    output_dir.mkdir(parents=True)
    media_dir = output_dir / "media"
    references_dir = output_dir / "references"
    media_dir.mkdir()
    references_dir.mkdir()
    rows: list[dict[str, str]] = []
    manifest_fixtures: list[dict[str, object]] = []
    for fixture in sorted(fixtures.values(), key=lambda item: item.fixture_id):
        source_media = _safe_source_file(source_suite.parent, fixture.media_file, fixture.fixture_id, "media")
        source_text = _safe_source_file(
            source_suite.parent,
            _suite_relative_value(source_payload, fixture.fixture_id, "reference_text_file"),
            fixture.fixture_id,
            "reference text",
        )
        media_relative = Path("media") / source_media.name
        text_relative = Path("references") / source_text.name
        copied_media = output_dir / media_relative
        copied_text = output_dir / text_relative
        shutil.copy2(source_media, copied_media)
        shutil.copy2(source_text, copied_text)
        _verify_sha256(copied_media, fixture.media_sha256, fixture.fixture_id, "copied media")
        text_sha256 = _sha256_file(copied_text)
        source_text_sha256 = _suite_sha256_value(source_payload, fixture.fixture_id, "reference_text_sha256")
        if text_sha256 != source_text_sha256:
            raise RuntimeError(f"{fixture.fixture_id} copied reference text hash mismatch")
        # G4 当前只比较每段源媒体的首尾语音包络，不要求人工伪造逐句字幕边界。
        reference_text_sha256 = _sha256_text(fixture.reference_text)
        rows.append(
            {
                "fixture_id": fixture.fixture_id,
                "segment_index": "1",
                "language": fixture.language,
                "reference_text": fixture.reference_text,
                "reference_text_sha256": reference_text_sha256,
                "begin_ms": "",
                "end_ms": "",
                "reviewer_id": "",
                "review_status": "pending",
                "note": "",
            }
        )
        manifest_fixtures.append(
            {
                "fixture_id": fixture.fixture_id,
                "split": fixture.split,
                "language": fixture.language,
                "source_ref": fixture.source_ref,
                "duration_ms": fixture.duration_ms,
                "media_file": media_relative.as_posix(),
                "media_sha256": fixture.media_sha256,
                "reference_text_file": text_relative.as_posix(),
                "reference_text_sha256": source_text_sha256,
                "source_reference_segments_sha256": _suite_sha256_value(
                    source_payload, fixture.fixture_id, "reference_segments_sha256"
                ),
                "expected_segments": [{"segment_index": 1, "reference_text_sha256": reference_text_sha256}],
            }
        )
    review_csv = output_dir / "review.csv"
    _write_review_csv(review_csv, rows)
    instruction_path = output_dir / "REVIEW_INSTRUCTIONS.md"
    instruction_path.write_text(_review_instructions(), encoding="utf-8")
    manifest = {
        "packet_type": REVIEW_PACKET_TYPE,
        "source_suite_sha256": suite_report["suite_sha256"],
        "source_suite_type": source_payload["suite_type"],
        "review_contract": {
            "reference_provenance": "published_benchmark",
            "annotation_scope": "source_speech_envelope",
            "time_values_initial_state": "blank",
            "provider_or_model_output_included": False,
            "model_call_count": 0,
            "network_call_count": 0,
            "completion_rule": "all rows accepted with independently reviewed integer millisecond boundaries",
        },
        "fixture_count": len(manifest_fixtures),
        "segment_count": len(rows),
        "fixtures": manifest_fixtures,
    }
    _write_json(output_dir / "packet.json", manifest)
    return {
        "ok": True,
        "packet_dir": str(output_dir),
        "source_suite_sha256": suite_report["suite_sha256"],
        "fixture_count": len(manifest_fixtures),
        "segment_count": len(rows),
        "review_state": "incomplete",
        "model_call_count": 0,
        "network_call_count": 0,
        "content_handling": "packet stays under ignored data/ and contains no Provider or model output",
    }


def _require_published_text_only_source(source_payload: dict[str, object], fixtures: dict[str, FixtureRecord]) -> None:
    source_fixtures = source_payload.get("fixtures")
    if not isinstance(source_fixtures, list):
        raise QualityContractError("source quality suite fixtures must be a list")
    raw_by_id = {
        str(item.get("fixture_id")): item for item in source_fixtures if isinstance(item, dict) and item.get("fixture_id")
    }
    if set(raw_by_id) != set(fixtures):
        raise QualityContractError("source quality suite fixture identifiers are inconsistent")
    for fixture_id, fixture in fixtures.items():
        raw = raw_by_id[fixture_id]
        if fixture.reference_provenance != "published_benchmark":
            raise QualityContractError("timestamp review packets only accept published benchmark text, never existing reviewed timing")
        if raw.get("reference_transcript_reviewed") is not False or raw.get("time_annotations_reviewed") is not False:
            raise QualityContractError(f"{fixture_id} must explicitly declare unreviewed published text and timestamps")


def _suite_relative_value(source_payload: dict[str, object], fixture_id: str, field: str) -> str:
    fixtures = source_payload.get("fixtures")
    assert isinstance(fixtures, list)
    for raw in fixtures:
        if isinstance(raw, dict) and raw.get("fixture_id") == fixture_id:
            value = raw.get(field)
            if isinstance(value, str) and value.strip():
                return value
    raise QualityContractError(f"{fixture_id} is missing {field}")


def _suite_sha256_value(source_payload: dict[str, object], fixture_id: str, field: str) -> str:
    value = _suite_relative_value(source_payload, fixture_id, field).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise QualityContractError(f"{fixture_id} has an invalid {field}")
    return value


def _safe_source_file(root: Path, relative: str, fixture_id: str, label: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise QualityContractError(f"{fixture_id} {label} path escapes the source suite")
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise QualityContractError(f"{fixture_id} {label} path escapes the source suite") from exc
    if not path.is_file():
        raise QualityContractError(f"{fixture_id} {label} file is missing")
    return path


def _require_ignored_output_directory(output_dir: Path) -> None:
    data_root = (PROJECT_ROOT / "data").resolve()
    try:
        output_dir.relative_to(data_root)
    except ValueError as exc:
        raise RuntimeError("timestamp review output must remain under ignored project data/") from exc
    if output_dir == data_root:
        raise RuntimeError("timestamp review output must be a new child directory under data/")


def _write_review_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_HEADERS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityContractError(f"{label} is not a readable JSON object") from exc
    if not isinstance(value, dict):
        raise QualityContractError(f"{label} must be a JSON object")
    return value


def _verify_sha256(path: Path, expected: str, fixture_id: str, label: str) -> None:
    if _sha256_file(path) != expected:
        raise RuntimeError(f"{fixture_id} {label} hash mismatch")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _review_instructions() -> str:
    return """# G4-ASR-DEV 人工时间标注说明

本审核包只用于为已发布的 FLEURS 参考文本补充独立的源级语音包络真值。当前 G4 指标只比较每段媒体的首个可听词开始和最后一个可听词结束，不是逐句字幕对齐；因此 `review.csv` 每段媒体只有一行。时间字段在创建时为空，包内不含 Qwen 或其他模型的转写、句段或时间戳。

1. 逐行播放 `media/` 内同名媒体，按完整 `reference_text` 对应的语音填写 `begin_ms` 和 `end_ms`。数值为整数毫秒；开始取整段媒体首个可听词，结束取最后一个可听词的结尾。
2. 只编辑 `review.csv` 的 `begin_ms`、`end_ms`、`reviewer_id`、`review_status` 和可选 `note`。不要改动 `fixture_id`、固定为 `1` 的索引、语言、文本或文本哈希。
3. 每一行完成后设置 `review_status=accepted` 并填写审核人标识。每条必须满足 `0 <= begin_ms < end_ms <= 该媒体时长`。
4. 审核人不能参照模型输出、Provider 时间戳或此前的拼接容器边界。独立性由项目负责人在审核记录外确认；脚本只能验证文件、范围、完整性和一致性，不能验证人的真实身份。
5. 完成后使用原始冻结质量集运行校验器。校验通过只说明时间参考可进入后续冻结步骤，不会自动将完整 G4-ASR-DEV 标记为通过，也不会发起新的模型调用。
"""


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_mm4_asr_timestamp_packet_") as temporary:
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
        report = build_review_packet(published_suite, packet_dir, enforce_data_directory=False)
        if report["review_state"] != "incomplete" or report["model_call_count"] != 0:
            raise AssertionError("new timestamp review packet must start empty without model calls")
        review_rows = _read_review_rows(packet_dir / "review.csv")
        if len(review_rows) != 8:
            raise AssertionError("source-envelope review packet must require exactly one row per source media")
        if any(row["begin_ms"] or row["end_ms"] or row["review_status"] != "pending" for row in review_rows):
            raise AssertionError("new timestamp review packet prefilled a reference time boundary")
        if "Provider" in (packet_dir / "review.csv").read_text(encoding="utf-8"):
            raise AssertionError("review CSV must not include Provider output")
    return {
        "ok": True,
        "self_test": True,
        "model_call_count": 0,
        "network_call_count": 0,
        "negative_contract_check": "published_text_only_and_blank_timestamp_fields_enforced",
    }


def _read_review_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
