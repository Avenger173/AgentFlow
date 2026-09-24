"""从已获授权的本地语音样本准备 MM-4 短媒体转写候选质量集。

此脚本不下载数据集、不要求外部数据集 Key，也不调用模型。它只消费调用者已经
取得并复核许可的音频和参考文本，用显式提供的 ffmpeg/ffprobe 生成黑底 MP4，
并把来源、哈希和人工标注状态写进忽略目录。未获人工确认的时间标注始终保持
``false``，不能通过 G4-ASR-DEV 质量集校验。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_transcription_quality import (
    QualityContractError,
    SUITE_TYPE,
    canonical_sha256,
    validate_suite,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAN_TYPE = "agentflow-mm4-asr-fixture-preparation-plan-v1"
_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus"}
_MAX_AUDIO_DURATION_MS = 200_000


@dataclass(frozen=True)
class FixturePlan:
    fixture_id: str
    split: str
    language: str
    audio_path: Path
    reference_text_path: Path
    reference_segments_path: Path | None
    reference_transcript_reviewed: bool
    time_annotations_reviewed: bool


@dataclass(frozen=True)
class SourceCatalog:
    dataset_name: str
    dataset_id: str
    source_page: str
    license_name: str
    license_url: str
    rights_reviewed: bool


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", type=Path, help="本地公开授权音频与参考文本准备计划 JSON")
    source.add_argument("--self-test", action="store_true", help="用临时合成音频验证准备器，不构成质量证据")
    parser.add_argument("--output-dir", type=Path, help="新建的忽略目录；不能覆盖已有目录")
    parser.add_argument("--ffmpeg-path", type=Path, help="显式指定 ffmpeg.exe")
    parser.add_argument("--ffprobe-path", type=Path, help="显式指定 ffprobe.exe")
    args = parser.parse_args()
    if args.self_test:
        if args.output_dir is not None:
            parser.error("--self-test does not accept --output-dir")
        ffmpeg_path = _require_executable(args.ffmpeg_path, "ffmpeg")
        ffprobe_path = _require_executable(args.ffprobe_path, "ffprobe")
        try:
            report = _run_self_test(ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path)
        except (OSError, RuntimeError, QualityContractError) as exc:
            print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
            raise SystemExit(1) from exc
        print(json.dumps(report, ensure_ascii=False))
        return
    if args.output_dir is None:
        parser.error("--plan requires --output-dir")
    ffmpeg_path = _require_executable(args.ffmpeg_path, "ffmpeg")
    ffprobe_path = _require_executable(args.ffprobe_path, "ffprobe")
    try:
        report = _prepare(
            plan_path=args.plan.resolve(),
            output_dir=args.output_dir.resolve(),
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
    except (OSError, RuntimeError, QualityContractError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))


def _prepare(
    *,
    plan_path: Path,
    output_dir: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    allow_temporary_output: bool = False,
) -> dict[str, object]:
    plan_payload = _read_json(plan_path, "fixture preparation plan")
    catalog, fixtures = _read_plan(plan_payload, root=plan_path.parent)
    if not allow_temporary_output:
        _require_ignored_output_directory(output_dir)
    if output_dir.exists():
        raise RuntimeError("output directory already exists; fixture preparation never overwrites a prior evidence set")
    output_dir.mkdir(parents=True)
    try:
        media_dir = output_dir / "media"
        references_dir = output_dir / "references"
        media_dir.mkdir()
        references_dir.mkdir()
        suite_fixtures: list[dict[str, object]] = []
        source_records: list[dict[str, object]] = []
        for fixture in fixtures:
            suite_record, source_record = _prepare_fixture(
                fixture=fixture,
                catalog=catalog,
                root=output_dir,
                media_dir=media_dir,
                references_dir=references_dir,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
            )
            suite_fixtures.append(suite_record)
            source_records.append(source_record)
        suite_payload = {"suite_type": SUITE_TYPE, "fixtures": suite_fixtures}
        suite_path = output_dir / "suite.json"
        _write_json(suite_path, suite_payload)
        annotations_complete = all(
            item["reference_transcript_reviewed"] is True and item["time_annotations_reviewed"] is True
            for item in suite_fixtures
        )
        quality_contract: dict[str, object]
        if annotations_complete:
            quality_contract, _ = validate_suite(suite_path, verify_files=True)
        else:
            quality_contract = {
                "ready": False,
                "reason": "reference_transcript_reviewed and time_annotations_reviewed must both be true for every fixture",
            }
        manifest = {
            "fixture_set": "agentflow-mm4-asr-prepared-candidate-v1",
            "plan_sha256": canonical_sha256(plan_payload),
            "suite_sha256": canonical_sha256(suite_payload),
            "source_catalog": {
                "dataset_name": catalog.dataset_name,
                "dataset_id": catalog.dataset_id,
                "source_page": catalog.source_page,
                "license": catalog.license_name,
                "license_url": catalog.license_url,
                "rights_reviewed": catalog.rights_reviewed,
            },
            "fixtures": source_records,
            "quality_contract": quality_contract,
            "model_calls": 0,
            "network_calls": 0,
            "content_claim": "candidate preparation only; it is not an ASR quality result",
        }
        _write_json(output_dir / "preparation_manifest.json", manifest)
    except BaseException:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    return {
        "ok": True,
        "output_dir": str(output_dir),
        "fixture_count": len(suite_fixtures),
        "annotation_review_complete": annotations_complete,
        "quality_contract_ready": annotations_complete,
        "model_calls": 0,
        "network_calls": 0,
    }


def _prepare_fixture(
    *,
    fixture: FixturePlan,
    catalog: SourceCatalog,
    root: Path,
    media_dir: Path,
    references_dir: Path,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    audio_bytes = fixture.audio_path.read_bytes()
    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
    source_ref = hashlib.sha256(f"{catalog.dataset_id}:{audio_sha256}".encode("utf-8")).hexdigest()[:32]
    source_duration_ms = _probe_duration_ms(ffprobe_path, fixture.audio_path)
    if source_duration_ms > _MAX_AUDIO_DURATION_MS:
        raise RuntimeError(f"fixture {fixture.fixture_id} exceeds the current 200 second normalized-audio limit")
    output_name = f"{fixture.fixture_id.lower()}.mp4"
    output_path = media_dir / output_name
    _mux_black_video(ffmpeg_path, fixture.audio_path, output_path)
    video_duration_ms = _probe_duration_ms(ffprobe_path, output_path)
    _verify_video_streams(ffprobe_path, output_path, fixture.fixture_id)
    if abs(video_duration_ms - source_duration_ms) > 1_500:
        raise RuntimeError(f"fixture {fixture.fixture_id} MP4 duration diverged from its source audio")
    video_bytes = output_path.read_bytes()
    text = fixture.reference_text_path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"fixture {fixture.fixture_id} reference text is empty")
    text_name = f"{fixture.fixture_id.lower()}.txt"
    text_path = references_dir / text_name
    text_path.write_text(text, encoding="utf-8")
    segments_name = f"{fixture.fixture_id.lower()}.segments.json"
    segments_path = references_dir / segments_name
    if fixture.reference_segments_path is not None:
        segments_payload = _read_json(fixture.reference_segments_path, f"reference segments for {fixture.fixture_id}")
    else:
        # 临时单段只让标注者看到要填的最小结构；状态保持待复核，不能用于质量评分。
        segments_payload = {
            "segments": [{"text": text, "begin_ms": 0, "end_ms": video_duration_ms}],
            "annotation_state": "pending_human_review",
        }
    _write_json(segments_path, segments_payload)
    relative_media = f"media/{output_name}"
    relative_text = f"references/{text_name}"
    relative_segments = f"references/{segments_name}"
    suite_record = {
        "fixture_id": fixture.fixture_id,
        "split": fixture.split,
        "language": fixture.language,
        "source_kind": "public_licensed",
        "source_ref": source_ref,
        "source_page": catalog.source_page,
        "license": catalog.license_name,
        "license_url": catalog.license_url,
        "rights_reviewed": catalog.rights_reviewed,
        "media_file": relative_media,
        "media_sha256": hashlib.sha256(video_bytes).hexdigest(),
        "duration_ms": video_duration_ms,
        "normalized_audio_duration_ms": video_duration_ms,
        "reference_text_file": relative_text,
        "reference_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "reference_segments_file": relative_segments,
        "reference_segments_sha256": hashlib.sha256(segments_path.read_bytes()).hexdigest(),
        "reference_transcript_reviewed": fixture.reference_transcript_reviewed,
        "time_annotations_reviewed": fixture.time_annotations_reviewed,
    }
    source_record = {
        "fixture_id": fixture.fixture_id,
        "split": fixture.split,
        "language": fixture.language,
        "source_ref": source_ref,
        "input_audio_sha256": audio_sha256,
        "input_audio_byte_size": len(audio_bytes),
        "source_audio_duration_ms": source_duration_ms,
        "prepared_video_sha256": suite_record["media_sha256"],
        "prepared_video_duration_ms": video_duration_ms,
        "reference_transcript_reviewed": fixture.reference_transcript_reviewed,
        "time_annotations_reviewed": fixture.time_annotations_reviewed,
    }
    return suite_record, source_record


def _read_plan(payload: dict[str, object], *, root: Path) -> tuple[SourceCatalog, list[FixturePlan]]:
    if payload.get("plan_type") != PLAN_TYPE:
        raise RuntimeError(f"plan_type must be {PLAN_TYPE}")
    raw_catalog = payload.get("source_catalog")
    if not isinstance(raw_catalog, dict):
        raise RuntimeError("source_catalog must be an object")
    catalog = SourceCatalog(
        dataset_name=_required_string(raw_catalog, "dataset_name", "source_catalog"),
        dataset_id=_required_string(raw_catalog, "dataset_id", "source_catalog"),
        source_page=_require_https(raw_catalog, "source_page", "source_catalog"),
        license_name=_required_string(raw_catalog, "license", "source_catalog"),
        license_url=_require_https(raw_catalog, "license_url", "source_catalog"),
        rights_reviewed=raw_catalog.get("rights_reviewed") is True,
    )
    if not catalog.rights_reviewed:
        raise RuntimeError("source_catalog.rights_reviewed must be explicitly true before fixture preparation")
    raw_fixtures = payload.get("fixtures")
    if not isinstance(raw_fixtures, list) or len(raw_fixtures) != 8:
        raise RuntimeError("fixture plan must contain exactly eight source audio records")
    fixtures: list[FixturePlan] = []
    fixture_ids: set[str] = set()
    source_hashes: set[str] = set()
    for index, raw in enumerate(raw_fixtures, start=1):
        if not isinstance(raw, dict):
            raise RuntimeError(f"fixture plan record {index} must be an object")
        fixture_id = _required_string(raw, "fixture_id", f"fixture {index}")
        if fixture_id in fixture_ids:
            raise RuntimeError(f"duplicate fixture_id: {fixture_id}")
        fixture_ids.add(fixture_id)
        split = _required_string(raw, "split", fixture_id)
        language = _required_string(raw, "language", fixture_id)
        if split not in {"development", "holdout"} or language not in {"zh", "en"}:
            raise RuntimeError(f"fixture {fixture_id} has unsupported split or language")
        audio_path = _existing_relative_file(root, raw, "audio_file", fixture_id)
        if audio_path.suffix.lower() not in _AUDIO_SUFFIXES:
            raise RuntimeError(f"fixture {fixture_id} source is not a supported audio file")
        audio_sha256 = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        if audio_sha256 in source_hashes:
            raise RuntimeError("the same source audio cannot be reused across fixture splits")
        source_hashes.add(audio_sha256)
        text_path = _existing_relative_file(root, raw, "reference_text_file", fixture_id)
        segment_path: Path | None = None
        if raw.get("reference_segments_file") is not None:
            segment_path = _existing_relative_file(root, raw, "reference_segments_file", fixture_id)
        if raw.get("time_annotations_reviewed") is True and segment_path is None:
            raise RuntimeError(f"fixture {fixture_id} cannot mark time annotations reviewed without a segment file")
        fixtures.append(
            FixturePlan(
                fixture_id=fixture_id,
                split=split,
                language=language,
                audio_path=audio_path,
                reference_text_path=text_path,
                reference_segments_path=segment_path,
                reference_transcript_reviewed=raw.get("reference_transcript_reviewed") is True,
                time_annotations_reviewed=raw.get("time_annotations_reviewed") is True,
            )
        )
    return catalog, fixtures


def _probe_duration_ms(ffprobe_path: Path, path: Path) -> int:
    payload = _run_json_command(
        [
            str(ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        label="ffprobe duration",
    )
    raw_duration = payload.get("format", {}).get("duration") if isinstance(payload.get("format"), dict) else None
    try:
        duration_ms = round(float(raw_duration) * 1000)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ffprobe did not return a valid media duration") from exc
    if duration_ms < 1:
        raise RuntimeError("media duration must be positive")
    return duration_ms


def _verify_video_streams(ffprobe_path: Path, path: Path, fixture_id: str) -> None:
    payload = _run_json_command(
        [str(ffprobe_path), "-v", "error", "-show_streams", "-of", "json", str(path)],
        label="ffprobe streams",
    )
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError(f"fixture {fixture_id} MP4 has no stream metadata")
    codec_types = {str(stream.get("codec_type") or "") for stream in streams if isinstance(stream, dict)}
    if not {"video", "audio"}.issubset(codec_types):
        raise RuntimeError(f"fixture {fixture_id} MP4 must contain one video and one audio stream")


def _mux_black_video(ffmpeg_path: Path, audio_path: Path, output_path: Path) -> None:
    _run_command(
        [
            str(ffmpeg_path),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x360:r=25",
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        label="ffmpeg fixture mux",
    )
    if not output_path.is_file() or output_path.stat().st_size < 1:
        raise RuntimeError("ffmpeg did not write a prepared fixture video")


def _run_json_command(command: list[str], *, label: str) -> dict[str, object]:
    completed = _run_command(command, label=label)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} did not return JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} did not return a JSON object")
    return value


def _run_command(command: list[str], *, label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{label} could not start or finish") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed")
    return completed


def _require_executable(path: Path | None, label: str) -> Path:
    if path is None:
        raise SystemExit(f"missing --{label}-path; this script does not guess development tool locations")
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".exe":
        raise SystemExit(f"configured {label} executable is unavailable")
    return resolved


def _require_ignored_output_directory(output_dir: Path) -> None:
    """公开源音频与文本仍不应落入 Git 工作树的可跟踪位置。"""

    data_root = (PROJECT_ROOT / "data").resolve()
    try:
        output_dir.relative_to(data_root)
    except ValueError as exc:
        raise RuntimeError("output directory must stay under the ignored project data/ directory") from exc


def _existing_relative_file(root: Path, record: dict[str, object], field: str, location: str) -> Path:
    relative = Path(_required_string(record, field, location))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"{location} {field} must stay inside the plan directory")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"{location} {field} escapes the plan directory") from exc
    if not path.is_file():
        raise RuntimeError(f"{location} {field} does not exist")
    return path


def _required_string(record: dict[str, object], field: str, location: str) -> str:
    value = str(record.get(field) or "").strip()
    if not value:
        raise RuntimeError(f"{location} is missing {field}")
    return value


def _require_https(record: dict[str, object], field: str, location: str) -> str:
    value = _required_string(record, field, location)
    if not value.startswith("https://"):
        raise RuntimeError(f"{location} {field} must be HTTPS")
    return value


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not a readable JSON object") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _run_self_test(*, ffmpeg_path: Path, ffprobe_path: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_mm4_fixture_prepare_") as temporary:
        root = Path(temporary)
        plan_path = _write_self_test_plan(root)
        output_dir = root / "prepared"
        report = _prepare(
            plan_path=plan_path,
            output_dir=output_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            allow_temporary_output=True,
        )
        if report["annotation_review_complete"] is not False or report["quality_contract_ready"] is not False:
            raise AssertionError("unreviewed generated timing annotations were treated as quality-ready")
        invalid = _read_json(plan_path, "self-test plan")
        invalid["fixtures"][1]["audio_file"] = invalid["fixtures"][0]["audio_file"]
        invalid_path = root / "invalid_plan.json"
        _write_json(invalid_path, invalid)
        try:
            _read_plan(invalid, root=root)
        except RuntimeError as exc:
            if "same source audio" not in str(exc):
                raise
        else:
            raise AssertionError("fixture preparation accepted a duplicate source audio file")
        try:
            _require_ignored_output_directory(root / "outside_project_data")
        except RuntimeError as exc:
            if "ignored project data" not in str(exc):
                raise
        else:
            raise AssertionError("fixture preparation accepted a trackable output directory")
    report["self_test"] = True
    report["negative_contract_check"] = "duplicate_source_and_trackable_output_rejected"
    return report


def _write_self_test_plan(root: Path) -> Path:
    inputs_dir = root / "inputs"
    inputs_dir.mkdir()
    fixture_rows = (
        ("ASR-ZH-DEV-01", "development", "zh", "这是仅用于验证夹具准备器的合成语音。"),
        ("ASR-EN-DEV-01", "development", "en", "This synthetic speech only verifies fixture preparation."),
        ("ASR-ZH-DEV-02", "development", "zh", "时间标注在人工复核前不能参与质量评分。"),
        ("ASR-EN-DEV-02", "development", "en", "The prepared video keeps a deterministic audio stream."),
        ("ASR-ZH-DEV-03", "development", "zh", "公开来源和本地哈希会被记录到清单中。"),
        ("ASR-EN-HOLD-01", "holdout", "en", "Holdout fixtures remain isolated from development tuning."),
        ("ASR-ZH-HOLD-01", "holdout", "zh", "留出集必须保持独立来源。"),
        ("ASR-EN-HOLD-02", "holdout", "en", "No provider call is made during fixture preparation."),
    )
    fixtures: list[dict[str, object]] = []
    for number, (fixture_id, split, language, text) in enumerate(fixture_rows, start=1):
        audio_name = f"sample_{number}.wav"
        _write_sine_wav(inputs_dir / audio_name, frequency=300 + number * 40)
        text_name = f"sample_{number}.txt"
        (inputs_dir / text_name).write_text(text, encoding="utf-8")
        fixtures.append(
            {
                "fixture_id": fixture_id,
                "split": split,
                "language": language,
                "audio_file": f"inputs/{audio_name}",
                "reference_text_file": f"inputs/{text_name}",
                "reference_transcript_reviewed": False,
                "time_annotations_reviewed": False,
            }
        )
    payload = {
        "plan_type": PLAN_TYPE,
        "source_catalog": {
            "dataset_name": "synthetic self-test only",
            "dataset_id": "self-test-source",
            "source_page": "https://example.invalid/source",
            "license": "self-test record",
            "license_url": "https://example.invalid/license",
            "rights_reviewed": True,
        },
        "fixtures": fixtures,
    }
    path = root / "plan.json"
    _write_json(path, payload)
    return path


def _write_sine_wav(path: Path, *, frequency: int) -> None:
    import math

    sample_rate = 16_000
    frames = bytearray()
    for index in range(sample_rate):
        sample = int(8_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(sample.to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(bytes(frames))


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
