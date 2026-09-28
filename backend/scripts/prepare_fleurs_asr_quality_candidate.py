"""从公开的 Google FLEURS 基准准备可复现的 G4-ASR-DEV 候选质量集。

默认不联网。``--execute`` 才从 Hugging Face datasets-server 获取 CC-BY 4.0 的中英文
公开音频；每个最终夹具由未跨 split 复用的多个原始 utterance 确定性拼接而成。输出仅允许
落到被忽略的 ``data/``，并保留行号、音频哈希、文本哈希和来源页，不保留会过期的签名 URL。
发布基准的参考文本与拼接边界可用于 G4-ASR-DEV；正式 G4 仍另行要求本地人工试听复核。
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlencode

import requests

from prepare_media_transcription_quality_fixtures import PLAN_TYPE, _prepare


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FLEURS_DATASET = "google/fleurs"
FLEURS_PAGE = "https://huggingface.co/datasets/google/fleurs"
FLEURS_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
FLEURS_ROWS_API = "https://datasets-server.huggingface.co/rows"
TARGET_FIXTURE_DURATION_MS = 65_000
MAX_SOURCE_CLIP_BYTES = 8 * 1024 * 1024
_ROW_PAGE_SIZE = 100
_MAX_SELECTION_ATTEMPTS = 12


@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    split: str
    language: str
    config: str
    dataset_split: str


@dataclass(frozen=True)
class SourceRow:
    row_index: int
    source_id: int
    config: str
    dataset_split: str
    duration_ms: int
    transcript: str
    audio_url: str


class SourceClipUnavailable(RuntimeError):
    """公开源的临时下载凭据失效，允许冻结器换用未复用候选。"""

    def __init__(self, row: SourceRow) -> None:
        super().__init__("public FLEURS source clip is unavailable after one metadata refresh")
        self.source_key = _source_key(row)


_FIXTURES = (
    FixtureSpec("ASR-ZH-DEV-01", "development", "zh", "cmn_hans_cn", "validation"),
    FixtureSpec("ASR-EN-DEV-01", "development", "en", "en_us", "validation"),
    FixtureSpec("ASR-ZH-DEV-02", "development", "zh", "cmn_hans_cn", "validation"),
    FixtureSpec("ASR-EN-DEV-02", "development", "en", "en_us", "validation"),
    FixtureSpec("ASR-ZH-DEV-03", "development", "zh", "cmn_hans_cn", "validation"),
    FixtureSpec("ASR-EN-HOLD-01", "holdout", "en", "en_us", "validation"),
    FixtureSpec("ASR-ZH-HOLD-01", "holdout", "zh", "cmn_hans_cn", "validation"),
    FixtureSpec("ASR-EN-HOLD-02", "holdout", "en", "en_us", "validation"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a public FLEURS candidate for G4-ASR-DEV.")
    parser.add_argument("--execute", action="store_true", help="Allow public FLEURS metadata/audio downloads and fixture preparation")
    parser.add_argument("--output-dir", type=Path, help="New ignored data/ directory; required with --execute")
    parser.add_argument("--ffmpeg-path", type=Path, help="Explicit ffmpeg.exe; required with --execute")
    parser.add_argument("--ffprobe-path", type=Path, help="Explicit ffprobe.exe; required with --execute")
    parser.add_argument("--self-test", action="store_true", help="Validate the fixed source split without network, FFmpeg, or models")
    args = parser.parse_args()
    if args.self_test:
        if args.execute or args.output_dir or args.ffmpeg_path or args.ffprobe_path:
            parser.error("--self-test cannot be combined with execution arguments")
        print(json.dumps(_run_self_test(), ensure_ascii=False))
        return
    if not args.execute:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "dry_run",
                    "dataset": FLEURS_DATASET,
                    "fixture_count": len(_FIXTURES),
                    "target_fixture_duration_ms": TARGET_FIXTURE_DURATION_MS,
                    "model_call_count": 0,
                    "network_call_count": 0,
                    "next_step": "pass --execute with a new data/ output directory and explicit FFmpeg paths",
                },
                ensure_ascii=False,
            )
        )
        return
    if args.output_dir is None or args.ffmpeg_path is None or args.ffprobe_path is None:
        parser.error("--execute requires --output-dir, --ffmpeg-path and --ffprobe-path")
    try:
        report = _prepare_fleurs_candidate(
            output_dir=args.output_dir.resolve(),
            ffmpeg_path=_require_executable(args.ffmpeg_path, "ffmpeg"),
            ffprobe_path=_require_executable(args.ffprobe_path, "ffprobe"),
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))


def _prepare_fleurs_candidate(*, output_dir: Path, ffmpeg_path: Path, ffprobe_path: Path) -> dict[str, object]:
    _require_ignored_output_directory(output_dir)
    if output_dir.exists():
        raise RuntimeError("output directory already exists; FLEURS candidate preparation never overwrites evidence")
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="agentflow_fleurs_asr_") as temporary:
        staging = Path(temporary)
        rows_by_key = _fetch_required_rows()
        cached_audio: dict[tuple[str, str, int, int], tuple[bytes, int]] = {}
        unavailable_sources: list[dict[str, object]] = []
        excluded_source_keys: set[tuple[str, str, int, int]] = set()
        plan_path: Path | None = None
        provenance: dict[str, object] | None = None
        for selection_attempt in range(1, _MAX_SELECTION_ATTEMPTS + 1):
            selected = _select_fixture_rows(rows_by_key, excluded_source_keys=excluded_source_keys)
            attempt_staging = staging / f"selection_{selection_attempt:02d}"
            try:
                plan_path, provenance = _build_staged_plan(
                    staging=attempt_staging,
                    selected=selected,
                    ffmpeg_path=ffmpeg_path,
                    ffprobe_path=ffprobe_path,
                    cached_audio=cached_audio,
                )
                break
            except SourceClipUnavailable as exc:
                if exc.source_key in excluded_source_keys:
                    raise RuntimeError("FLEURS source replacement repeated an excluded source row") from exc
                excluded_source_keys.add(exc.source_key)
                unavailable_sources.append(_unavailable_source_record(exc.source_key))
        if plan_path is None or provenance is None:
            raise RuntimeError("FLEURS fixture preparation exhausted the bounded unavailable-source replacement limit")
        prepared = _prepare(
            plan_path=plan_path,
            output_dir=output_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
    provenance["unavailable_sources"] = unavailable_sources
    provenance["unavailable_source_count"] = len(unavailable_sources)
    provenance.update(
        {
            "created_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "prepared_suite_sha256": sha256((output_dir / "suite.json").read_bytes()).hexdigest(),
            "model_call_count": 0,
            "network_call_count": (
                provenance["source_audio_download_count"]
                + provenance["metadata_request_count"]
                + provenance["source_metadata_refresh_count"]
            ),
            "content_claim": "published benchmark candidate prepared; no local human listening review and no model quality result",
        }
    )
    _write_json(output_dir / "fleurs_provenance.json", provenance)
    return {
        "ok": True,
        "output_dir": str(output_dir),
        "fixture_count": prepared["fixture_count"],
        "total_source_duration_ms": provenance["total_source_duration_ms"],
        "quality_contract_ready": prepared["quality_contract_ready"],
        "timestamp_quality_contract_ready": prepared["timestamp_quality_contract_ready"],
        "reference_provenance": "published_benchmark",
        "source_audio_download_count": provenance["source_audio_download_count"],
        "metadata_request_count": provenance["metadata_request_count"],
        "unavailable_source_count": len(unavailable_sources),
        "model_call_count": 0,
        "next_step": "run the fixed quality batch only after reviewing the prepared provenance manifest",
    }


def _fetch_required_rows() -> dict[tuple[str, str], list[SourceRow]]:
    required_keys = {(item.config, item.dataset_split) for item in _FIXTURES}
    return {key: _fetch_rows(config=key[0], dataset_split=key[1]) for key in sorted(required_keys)}


def _fetch_rows(*, config: str, dataset_split: str) -> list[SourceRow]:
    query = urlencode(
        {"dataset": FLEURS_DATASET, "config": config, "split": dataset_split, "offset": 0, "length": _ROW_PAGE_SIZE}
    )
    payload = _read_public_json(f"{FLEURS_ROWS_API}?{query}")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise RuntimeError("FLEURS metadata endpoint did not return rows")
    rows: list[SourceRow] = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("row"), dict):
            continue
        row = raw["row"]
        audio_entries = row.get("audio")
        if not isinstance(audio_entries, list) or not audio_entries or not isinstance(audio_entries[0], dict):
            continue
        audio_url = str(audio_entries[0].get("src") or "")
        transcript = str(row.get("raw_transcription") or row.get("transcription") or "").strip()
        sample_count = row.get("num_samples")
        row_index = raw.get("row_idx")
        source_id = row.get("id")
        if (
            not audio_url.startswith("https://")
            or not transcript
            or not isinstance(sample_count, int)
            or not isinstance(row_index, int)
            or not isinstance(source_id, int)
        ):
            continue
        duration_ms = round(sample_count / 16_000 * 1000)
        if 1_000 <= duration_ms <= 30_000:
            rows.append(
                SourceRow(
                    row_index=row_index,
                    source_id=source_id,
                    config=config,
                    dataset_split=dataset_split,
                    duration_ms=duration_ms,
                    transcript=transcript,
                    audio_url=audio_url,
                )
            )
    if len(rows) < 12:
        raise RuntimeError(f"FLEURS {config}/{dataset_split} returned too few eligible source rows")
    return rows


def _select_fixture_rows(
    rows_by_key: dict[tuple[str, str], list[SourceRow]],
    *,
    excluded_source_keys: set[tuple[str, str, int, int]] | None = None,
) -> dict[str, list[SourceRow]]:
    cursors: Counter[tuple[str, str]] = Counter()
    selected: dict[str, list[SourceRow]] = {}
    selected_source_keys: set[tuple[str, str, int, int]] = set()
    excluded_source_keys = excluded_source_keys or set()
    for fixture in _FIXTURES:
        key = (fixture.config, fixture.dataset_split)
        pool = rows_by_key[key]
        fixture_rows: list[SourceRow] = []
        duration_ms = 0
        while duration_ms < TARGET_FIXTURE_DURATION_MS:
            cursor = cursors[key]
            if cursor >= len(pool):
                raise RuntimeError(f"FLEURS {fixture.config}/{fixture.dataset_split} did not provide enough distinct source audio")
            row = pool[cursor]
            cursors[key] += 1
            source_key = _source_key(row)
            if source_key in excluded_source_keys:
                continue
            if source_key in selected_source_keys:
                raise RuntimeError("FLEURS source row would be reused across quality fixtures")
            selected_source_keys.add(source_key)
            fixture_rows.append(row)
            duration_ms += row.duration_ms
            if len(fixture_rows) > 32:
                raise RuntimeError("FLEURS fixture exceeded the source-row safety limit")
        selected[fixture.fixture_id] = fixture_rows
    return selected


def _build_staged_plan(
    *,
    staging: Path,
    selected: dict[str, list[SourceRow]],
    ffmpeg_path: Path,
    ffprobe_path: Path,
    cached_audio: dict[tuple[str, str, int, int], tuple[bytes, int]],
) -> tuple[Path, dict[str, object]]:
    clips_dir = staging / "source_clips"
    inputs_dir = staging / "inputs"
    references_dir = staging / "references"
    for directory in (clips_dir, inputs_dir, references_dir):
        directory.mkdir(parents=True)
    fixtures: list[dict[str, object]] = []
    provenance_fixtures: list[dict[str, object]] = []
    source_metadata_refresh_count = 0
    for fixture in _FIXTURES:
        source_rows = selected[fixture.fixture_id]
        source_files: list[Path] = []
        lineage: list[dict[str, object]] = []
        for row in source_rows:
            filename = f"{fixture.fixture_id.lower()}_{row.row_index:04d}_{row.source_id}.wav"
            path = clips_dir / filename
            source_key = _source_key(row)
            cached = cached_audio.get(source_key)
            if cached is None:
                cached = _download_public_audio(row)
                cached_audio[source_key] = cached
            encoded, refreshed = cached
            source_metadata_refresh_count += refreshed
            path.write_bytes(encoded)
            actual_duration_ms = _probe_duration_ms(ffprobe_path, path)
            source_files.append(path)
            lineage.append(
                {
                    "config": row.config,
                    "dataset_split": row.dataset_split,
                    "row_index": row.row_index,
                    "source_id": row.source_id,
                    "audio_sha256": sha256(encoded).hexdigest(),
                    "duration_ms": actual_duration_ms,
                    "reference_text_sha256": sha256(row.transcript.encode("utf-8")).hexdigest(),
                }
            )
        audio_path = inputs_dir / f"{fixture.fixture_id.lower()}.wav"
        _concat_audio(ffmpeg_path, source_files, audio_path)
        total_duration_ms = _probe_duration_ms(ffprobe_path, audio_path)
        text_path = references_dir / f"{fixture.fixture_id.lower()}.txt"
        segments_path = references_dir / f"{fixture.fixture_id.lower()}.segments.json"
        text_path.write_text("\n".join(row.transcript for row in source_rows), encoding="utf-8")
        segments = _build_segments(source_rows=source_rows, actual_durations=lineage, total_duration_ms=total_duration_ms)
        _write_json(
            segments_path,
            {
                "segments": segments,
                "annotation_state": "published_benchmark_deterministic_concat",
                "boundary_method": "contiguous_public_source_clip_durations",
            },
        )
        fixtures.append(
            {
                "fixture_id": fixture.fixture_id,
                "split": fixture.split,
                "language": fixture.language,
                "audio_file": str(audio_path.relative_to(staging).as_posix()),
                "reference_text_file": str(text_path.relative_to(staging).as_posix()),
                "reference_segments_file": str(segments_path.relative_to(staging).as_posix()),
                "reference_transcript_reviewed": False,
                "time_annotations_reviewed": False,
                "reference_provenance": "published_benchmark",
                "reference_provenance_url": FLEURS_PAGE,
            }
        )
        provenance_fixtures.append(
            {
                "fixture_id": fixture.fixture_id,
                "split": fixture.split,
                "language": fixture.language,
                "composite_audio_sha256": sha256(audio_path.read_bytes()).hexdigest(),
                "composite_duration_ms": total_duration_ms,
                "source_clip_count": len(lineage),
                "source_lineage": lineage,
            }
        )
    plan = {
        "plan_type": PLAN_TYPE,
        "source_catalog": {
            "dataset_name": "Google FLEURS",
            "dataset_id": "google/fleurs",
            "source_page": FLEURS_PAGE,
            "license": "CC-BY-4.0",
            "license_url": FLEURS_LICENSE_URL,
            "rights_reviewed": True,
        },
        "fixtures": fixtures,
    }
    plan_path = staging / "plan.json"
    _write_json(plan_path, plan)
    provenance = {
        "provenance_type": "agentflow-mm4-fleurs-quality-candidate-v1",
        "dataset": {"id": FLEURS_DATASET, "source_page": FLEURS_PAGE, "license": "CC-BY-4.0", "license_url": FLEURS_LICENSE_URL},
        "reference_provenance": "published_benchmark",
        "reference_provenance_url": FLEURS_PAGE,
        "split_policy": "AgentFlow source-row disjoint split; all selected FLEURS rows originate from validation because the public test metadata endpoint was unavailable during preparation",
        "fixture_source_lineage": provenance_fixtures,
        "source_audio_download_count": len(cached_audio),
        "metadata_request_count": len({(item.config, item.dataset_split) for item in _FIXTURES}),
        "source_metadata_refresh_count": source_metadata_refresh_count,
        "total_source_duration_ms": sum(int(item["composite_duration_ms"]) for item in provenance_fixtures),
    }
    return plan_path, provenance


def _build_segments(
    *, source_rows: list[SourceRow], actual_durations: list[dict[str, object]], total_duration_ms: int
) -> list[dict[str, object]]:
    cursor = 0
    segments: list[dict[str, object]] = []
    for index, (row, record) in enumerate(zip(source_rows, actual_durations, strict=True)):
        duration_ms = int(record["duration_ms"])
        end_ms = cursor + duration_ms
        if index == len(source_rows) - 1:
            end_ms = total_duration_ms
        if end_ms < cursor or end_ms > total_duration_ms:
            raise RuntimeError("FLEURS deterministic source boundaries exceed composite duration")
        segments.append({"text": row.transcript, "begin_ms": cursor, "end_ms": end_ms})
        cursor = end_ms
    if not segments or segments[-1]["end_ms"] != total_duration_ms:
        raise RuntimeError("FLEURS deterministic source boundaries do not cover the composite audio")
    return segments


def _download_public_audio(row: SourceRow) -> tuple[bytes, int]:
    """允许一次公开元数据刷新；仅处理会过期的下载凭据，不重选样本。"""

    urls = [row.audio_url]
    refresh_count = 0
    for attempt, url in enumerate(urls):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "AgentFlow-G4-ASR-Evaluation/1.0"},
                timeout=(15, 90),
            )
            response.raise_for_status()
            content = response.content
        except requests.RequestException:
            if attempt == 0:
                urls.append(_refresh_audio_url(row))
                refresh_count = 1
                continue
            raise SourceClipUnavailable(row) from None
        if not content or len(content) > MAX_SOURCE_CLIP_BYTES:
            raise SourceClipUnavailable(row)
        return content, refresh_count
    raise SourceClipUnavailable(row)


def _source_key(row: SourceRow) -> tuple[str, str, int, int]:
    return (row.config, row.dataset_split, row.row_index, row.source_id)


def _unavailable_source_record(source_key: tuple[str, str, int, int]) -> dict[str, object]:
    config, dataset_split, row_index, source_id = source_key
    return {
        "config": config,
        "dataset_split": dataset_split,
        "row_index": row_index,
        "source_id": source_id,
        "reason": "public_download_unavailable_after_one_metadata_refresh",
    }


def _refresh_audio_url(row: SourceRow) -> str:
    query = urlencode(
        {
            "dataset": FLEURS_DATASET,
            "config": row.config,
            "split": row.dataset_split,
            "offset": row.row_index,
            "length": 1,
        }
    )
    payload = _read_public_json(f"{FLEURS_ROWS_API}?{query}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or not isinstance(rows[0].get("row"), dict):
        raise RuntimeError("public FLEURS metadata refresh did not return the requested source row")
    refreshed = rows[0]
    source = refreshed["row"]
    audio_entries = source.get("audio")
    if refreshed.get("row_idx") != row.row_index or source.get("id") != row.source_id:
        raise RuntimeError("public FLEURS metadata refresh returned a different source row")
    if not isinstance(audio_entries, list) or not audio_entries or not isinstance(audio_entries[0], dict):
        raise RuntimeError("public FLEURS metadata refresh has no audio source")
    url = str(audio_entries[0].get("src") or "")
    if not url.startswith("https://"):
        raise RuntimeError("public FLEURS metadata refresh returned an invalid audio source")
    return url


def _read_public_json(url: str) -> dict[str, Any]:
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "AgentFlow-G4-ASR-Evaluation/1.0"},
                timeout=(15, 30),
            )
            response.raise_for_status()
            value = response.json()
        except (requests.RequestException, json.JSONDecodeError):
            if attempt == 2:
                raise RuntimeError("public FLEURS metadata request failed after bounded retries") from None
            time.sleep(1.5 * (attempt + 1))
            continue
        if not isinstance(value, dict):
            raise RuntimeError("public FLEURS metadata response is not an object")
        return value
    raise AssertionError("bounded FLEURS metadata retry loop fell through")


def _concat_audio(ffmpeg_path: Path, source_files: list[Path], output_path: Path) -> None:
    listing = output_path.with_suffix(".concat.txt")
    listing.write_text("\n".join(f"file '{path.resolve().as_posix()}'" for path in source_files), encoding="utf-8")
    try:
        _run_command(
            [
                str(ffmpeg_path),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ],
            label="FLEURS audio concatenation",
        )
    finally:
        listing.unlink(missing_ok=True)
    if not output_path.is_file() or output_path.stat().st_size < 44:
        raise RuntimeError("FLEURS composite audio was not written")


def _probe_duration_ms(ffprobe_path: Path, path: Path) -> int:
    completed = _run_command(
        [str(ffprobe_path), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        label="FLEURS duration probe",
    )
    try:
        raw = json.loads(completed.stdout)["format"]["duration"]
        duration_ms = round(float(raw) * 1000)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("FLEURS duration probe returned invalid metadata") from exc
    if duration_ms < 1:
        raise RuntimeError("FLEURS source duration must be positive")
    return duration_ms


def _run_command(command: list[str], *, label: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{label} could not start or finish") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed")
    return completed


def _require_executable(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".exe":
        raise RuntimeError(f"{label} executable is unavailable")
    return resolved


def _require_ignored_output_directory(output_dir: Path) -> None:
    data_root = (PROJECT_ROOT / "data").resolve()
    try:
        output_dir.relative_to(data_root)
    except ValueError as exc:
        raise RuntimeError("FLEURS candidate output must remain under ignored project data/") from exc
    if output_dir == data_root:
        raise RuntimeError("FLEURS candidate output must be a new child directory under data/")


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _run_self_test() -> dict[str, object]:
    split_counts = Counter(item.split for item in _FIXTURES)
    language_counts = Counter(item.language for item in _FIXTURES)
    split_languages = Counter((item.split, item.language) for item in _FIXTURES)
    if split_counts != {"development": 5, "holdout": 3} or language_counts["zh"] < 3 or language_counts["en"] < 3:
        raise AssertionError("FLEURS fixture layout no longer matches the G4-ASR-DEV split contract")
    if any(split_languages[(split, language)] < 1 for split in split_counts for language in language_counts):
        raise AssertionError("FLEURS fixture layout lost Chinese or English coverage in a split")
    rows = {
        ("cmn_hans_cn", "validation"): [
            SourceRow(index, index, "cmn_hans_cn", "validation", 22_000, f"中文样本{index}", "https://example.invalid/audio")
            for index in range(24)
        ],
        ("en_us", "validation"): [
            SourceRow(index, index, "en_us", "validation", 22_000, f"English sample {index}", "https://example.invalid/audio")
            for index in range(18)
        ],
    }
    selected = _select_fixture_rows(rows)
    source_keys = {
        (row.config, row.dataset_split, row.row_index, row.source_id)
        for fixture_rows in selected.values()
        for row in fixture_rows
    }
    if len(source_keys) != sum(len(fixture_rows) for fixture_rows in selected.values()):
        raise AssertionError("FLEURS fixture selection reused a source row")
    excluded = _source_key(selected["ASR-ZH-DEV-01"][0])
    replacement_selected = _select_fixture_rows(rows, excluded_source_keys={excluded})
    replacement_keys = {
        _source_key(row) for fixture_rows in replacement_selected.values() for row in fixture_rows
    }
    if excluded in replacement_keys or len(replacement_keys) != sum(len(item) for item in replacement_selected.values()):
        raise AssertionError("FLEURS fixture selection did not safely replace an unavailable source row")
    return {
        "ok": True,
        "self_test": True,
        "fixture_count": len(_FIXTURES),
        "selected_source_row_count": len(source_keys),
        "model_call_count": 0,
        "network_call_count": 0,
        "negative_contract_check": "split_language_coverage_source_uniqueness_and_unavailable_source_replacement_enforced",
    }


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
