"""Run the fixed MM-2 G2 image-quality suite with one Qwen call per task.

This is an evaluation harness, not a product feature. It writes public-fixture inputs,
raw Provider outputs, redacted request facts, and a blind review packet to an ignored
directory. It never retries a task automatically after a submitted or unknown request.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any

from PIL import Image, ImageDraw, ImageFont


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.model_gateway import (  # noqa: E402
    ModelGatewayError,
    VisualModelRuntime,
    resolve_visual_model_runtime_for_route,
)
from app.services.qwen_image_edit import (  # noqa: E402
    QwenImageEditInput,
    QwenImageEditOutcomeUnknownError,
    QwenImageEditProviderError,
    QwenImageEditRateLimitError,
    download_qwen_image_result,
    edit_qwen_image,
)
from verify_media_g2_quality_suite import _verify_suite  # noqa: E402


_FONT_PATHS = (Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\msyhbd.ttc"))
_TERMINAL_STATUSES = {"completed_pending_review", "rejected", "unknown", "failed_pre_submission"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--validate-inputs", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume-dir", type=Path)
    parser.add_argument("--model", default="qwen-image-3.0-pro")
    parser.add_argument("--minimum-interval-seconds", type=float, default=35.0)
    args = parser.parse_args()
    if args.validate_inputs == args.execute:
        parser.error("choose exactly one of --validate-inputs or --execute")
    if args.output_dir and args.resume_dir:
        parser.error("--output-dir and --resume-dir cannot be used together")
    if args.minimum_interval_seconds < 0 or args.minimum_interval_seconds > 300:
        parser.error("--minimum-interval-seconds must be between 0 and 300")

    suite_path = args.suite.resolve()
    try:
        suite_report = _verify_suite(suite_path, verify_files=True)
        suite = _read_json(suite_path)
        if args.validate_inputs:
            report = _validate_inputs(suite_path.parent, suite, suite_report)
        else:
            report = asyncio.run(
                _execute(
                    suite_dir=suite_path.parent,
                    suite=suite,
                    suite_report=suite_report,
                    output_dir=args.output_dir.resolve() if args.output_dir else None,
                    resume_dir=args.resume_dir.resolve() if args.resume_dir else None,
                    requested_model=str(args.model).strip(),
                    minimum_interval_seconds=float(args.minimum_interval_seconds),
                )
            )
    except (ModelGatewayError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(report, ensure_ascii=False))
    if report.get("terminal_failures", 0):
        raise SystemExit(1)


def _validate_inputs(suite_dir: Path, suite: dict[str, Any], suite_report: dict[str, object]) -> dict[str, object]:
    fixtures = _index_fixtures(suite_dir, suite)
    tasks = _tasks(suite)
    with tempfile.TemporaryDirectory(prefix="agentflow_g2_input_") as temporary:
        output_dir = Path(temporary)
        records = [_prepare_task_input(task, fixtures[task["fixture_id"]], output_dir) for task in tasks]
        if len({record["input_sha256"] for record in records}) != len(records):
            raise RuntimeError("G2 input recipes produced duplicate task images")
    return {
        "ok": True,
        "suite_sha256": suite_report["suite_sha256"],
        "input_count": len(records),
        "network_calls": 0,
        "provider_calls": 0,
        "quality_claim": "none; only frozen input construction and readback passed",
    }


async def _execute(
    *,
    suite_dir: Path,
    suite: dict[str, Any],
    suite_report: dict[str, object],
    output_dir: Path | None,
    resume_dir: Path | None,
    requested_model: str,
    minimum_interval_seconds: float,
) -> dict[str, object]:
    if not requested_model.startswith("qwen-image"):
        raise ValueError("G2 only permits a configured qwen-image model")
    fixtures = _index_fixtures(suite_dir, suite)
    tasks = _tasks(suite)
    runtime = _resolve_runtime(requested_model)
    run_dir, manifest = _open_run(
        output_dir=output_dir,
        resume_dir=resume_dir,
        suite=suite,
        suite_report=suite_report,
        runtime=runtime,
        minimum_interval_seconds=minimum_interval_seconds,
        task_count=len(tasks),
    )
    cases = manifest["cases"]
    assert isinstance(cases, dict)
    previous_request_started: float | None = None
    for task in tasks:
        task_id = task["task_id"]
        if task_id in cases and str(cases[task_id].get("status")) in _TERMINAL_STATUSES:
            continue
        record = _prepare_task_input(task, fixtures[task["fixture_id"]], run_dir)
        if previous_request_started is not None:
            delay = minimum_interval_seconds - (perf_counter() - previous_request_started)
            if delay > 0:
                await asyncio.sleep(delay)
        previous_request_started = perf_counter()
        record["request_started_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            provider_result = await edit_qwen_image(
                images=[QwenImageEditInput(image_bytes=(run_dir / record["input_file"]).read_bytes(), mime_type="image/png")],
                prompt=task["instruction"],
                output_count=1,
                output_size=record["output_size"],
                prompt_extend=False,
                watermark=False,
                seed=_seed_for_task(task_id),
                runtime=runtime,
            )
            record["provider_request_id_sha256"] = _sha256_text(provider_result.request_id)
            record["provider_model"] = provider_result.model
            record["provider_usage"] = {
                "reported": provider_result.usage_reported,
                "input_image_count": provider_result.input_image_count,
                "output_image_count": provider_result.output_image_count,
                "input_image_type": provider_result.input_image_type,
                "output_image_type": provider_result.output_image_type,
                "output_width": provider_result.width,
                "output_height": provider_result.height,
                "billing_amount": "unknown",
            }
            record["result_url_sha256"] = _sha256_text(provider_result.output_urls[0].split("?", 1)[0])
            downloaded = await download_qwen_image_result(result_url=provider_result.output_urls[0])
            result_path = run_dir / f"{task_id.lower()}_provider_raw.png"
            result_path.write_bytes(downloaded.image_bytes)
            record["provider_raw_file"] = result_path.name
            record["provider_raw_sha256"] = hashlib.sha256(downloaded.image_bytes).hexdigest()
            record["provider_result_format"] = downloaded.image_format
            record["provider_result_size"] = [downloaded.width, downloaded.height]
            if downloaded.image_format != "PNG":
                record["status"] = "rejected"
                record["error_category"] = "unexpected_result_format"
            elif [downloaded.width, downloaded.height] != record["input_size"]:
                record["status"] = "rejected"
                record["error_category"] = "unexpected_result_size"
            else:
                _read_png(downloaded.image_bytes)
                record["status"] = "completed_pending_review"
        except QwenImageEditOutcomeUnknownError as exc:
            record["status"] = "unknown"
            record["error_category"] = str(exc.reason)
        except QwenImageEditRateLimitError as exc:
            record["status"] = "rejected"
            record["error_category"] = "rate_limited"
            record["retry_after_seconds"] = exc.retry_after_seconds
        except QwenImageEditProviderError as exc:
            record["status"] = "rejected"
            record["error_category"] = "provider_rejected"
            record["provider_http_status"] = exc.status_code
            record["provider_error_code"] = exc.error_code
        except (ModelGatewayError, OSError, RuntimeError, ValueError) as exc:
            submitted = "provider_request_id_sha256" in record
            record["status"] = "unknown" if submitted else "failed_pre_submission"
            record["error_category"] = "post_submission_download_error" if submitted else "local_or_gateway_error"
            record["error"] = _safe_error(exc)
        finally:
            record["elapsed_ms"] = max(0, round((perf_counter() - previous_request_started) * 1000))
            cases[task_id] = record
            _write_json(run_dir / "manifest.json", manifest)

    manifest["completed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    manifest["run_state"] = "complete"
    _write_json(run_dir / "manifest.json", manifest)
    packet = _create_review_packet(run_dir, manifest)
    terminal_failures = sum(1 for record in cases.values() if record.get("status") != "completed_pending_review")
    return {
        "ok": terminal_failures == 0,
        "run_dir": str(run_dir),
        "provider_route": "media_image_edit",
        "provider": runtime.provider,
        "model": runtime.model,
        "task_count": len(tasks),
        "completed_pending_review": len(tasks) - terminal_failures,
        "terminal_failures": terminal_failures,
        "review_packet_dir": str(packet),
        "quality_claim": "none; a completed run requires independent blind review before G2 can pass",
    }


def _resolve_runtime(model: str) -> VisualModelRuntime:
    resolved = resolve_visual_model_runtime_for_route("media_image_edit", validate=True).runtime
    if not isinstance(resolved, VisualModelRuntime) or resolved.provider != "qwen_image":
        raise RuntimeError("media_image_edit route is not configured for Qwen Image")
    return replace(resolved, model=model)


def _open_run(
    *,
    output_dir: Path | None,
    resume_dir: Path | None,
    suite: dict[str, Any],
    suite_report: dict[str, object],
    runtime: VisualModelRuntime,
    minimum_interval_seconds: float,
    task_count: int,
) -> tuple[Path, dict[str, Any]]:
    if resume_dir is not None:
        manifest = _read_json(resume_dir / "manifest.json")
        if manifest.get("suite_sha256") != suite_report["suite_sha256"] or manifest.get("model") != runtime.model:
            raise RuntimeError("resume run does not match the frozen suite or selected model")
        if not isinstance(manifest.get("cases"), dict):
            raise RuntimeError("resume run manifest is missing cases")
        return resume_dir, manifest
    run_dir = output_dir or (
        PROJECT_ROOT / "data" / "media_evaluations" / datetime.now(UTC).strftime("g2_qwen_image_%Y%m%dT%H%M%SZ")
    )
    if run_dir.exists():
        raise RuntimeError("G2 output directory already exists")
    run_dir.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "run_type": "agentflow-mm2-g2-qwen-image-v1",
        "run_state": "running",
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "suite_sha256": suite_report["suite_sha256"],
        "fixture_set": suite.get("fixture_set"),
        "provider_route": "media_image_edit",
        "provider": runtime.provider,
        "model": runtime.model,
        "minimum_interval_seconds": minimum_interval_seconds,
        "max_provider_calls_per_task": 1,
        "task_count": task_count,
        "cases": {},
        "quality_claim": "none; output must be independently blind-reviewed",
    }
    _write_json(run_dir / "manifest.json", manifest)
    return run_dir, manifest


def _index_fixtures(suite_dir: Path, suite: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_fixtures = suite.get("fixtures")
    if not isinstance(raw_fixtures, list):
        raise RuntimeError("suite fixtures are missing")
    indexed: dict[str, dict[str, Any]] = {}
    for raw in raw_fixtures:
        if not isinstance(raw, dict):
            raise RuntimeError("suite fixture is invalid")
        fixture_id = str(raw.get("fixture_id") or "")
        path = (suite_dir / str(raw.get("file") or "")).resolve()
        if not fixture_id or path.parent != suite_dir or not path.is_file():
            raise RuntimeError("suite fixture path is invalid")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != raw.get("sha256"):
            raise RuntimeError(f"fixture hash mismatch: {fixture_id}")
        indexed[fixture_id] = {"record": raw, "path": path}
    return indexed


def _tasks(suite: dict[str, Any]) -> list[dict[str, str]]:
    raw_tasks = suite.get("tasks")
    if not isinstance(raw_tasks, list):
        raise RuntimeError("suite tasks are missing")
    tasks: list[dict[str, str]] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise RuntimeError("suite task is invalid")
        task = {key: str(raw.get(key) or "") for key in ("task_id", "fixture_id", "category", "instruction", "input_recipe")}
        if not all(task.values()):
            raise RuntimeError("suite task lacks required execution fields")
        tasks.append(task)
    return tasks


def _prepare_task_input(task: dict[str, str], fixture: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    with Image.open(fixture["path"]) as source:
        base = _resize_for_model(source.convert("RGB"))
    image, target_bounds = _apply_recipe(base, task["input_recipe"])
    content = _png_bytes(image)
    _read_png(content)
    input_path = output_dir / f"{task['task_id'].lower()}_input.png"
    input_path.write_bytes(content)
    return {
        "task_id": task["task_id"],
        "fixture_id": task["fixture_id"],
        "category": task["category"],
        "split": fixture["record"]["split"],
        "instruction": task["instruction"],
        "input_recipe": task["input_recipe"],
        "target_bounds": list(target_bounds) if target_bounds else None,
        "source_sha256": fixture["record"]["sha256"],
        "input_file": input_path.name,
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "input_size": [image.width, image.height],
        "output_size": f"{image.width}*{image.height}",
    }


def _resize_for_model(image: Image.Image) -> Image.Image:
    width, height = image.size
    scale = min(1.0, 1280.0 / max(width, height))
    target_width = max(512, int(width * scale) // 16 * 16)
    target_height = max(512, int(height * scale) // 16 * 16)
    if (target_width, target_height) == image.size:
        return image
    return image.resize((target_width, target_height), Image.Resampling.LANCZOS)


def _apply_recipe(image: Image.Image, recipe: str) -> tuple[Image.Image, tuple[int, int, int, int] | None]:
    if recipe == "source_only":
        return image, None
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    margin = max(24, min(width, height) // 32)
    if recipe == "synthetic_sticker_bottom_right":
        size = max(96, min(width, height) // 6)
        bounds = (width - margin - size, height - margin - size, width - margin, height - margin)
        shadow = tuple(value + 7 for value in bounds)
        draw.ellipse(shadow, fill=(112, 28, 28))
        draw.ellipse(bounds, fill=(214, 49, 45), outline=(143, 23, 23), width=4)
        font = _load_font(max(24, size // 4))
        draw.text(((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2), "\u6d4b\u8bd5", font=font, fill=(255, 255, 255), anchor="mm")
        return canvas, bounds
    if recipe == "synthetic_chinese_price_label_top_left":
        label_width = min(max(260, width // 3), width - 2 * margin)
        label_height = max(84, min(132, height // 6))
        bounds = (margin, margin, margin + label_width, margin + label_height)
        draw.rounded_rectangle(bounds, radius=18, fill=(250, 250, 248), outline=(204, 210, 216), width=3)
        font = _load_font(max(28, label_height // 3))
        draw.text((bounds[0] + 20, (bounds[1] + bounds[3]) // 2), "\u65b0\u54c1 99 \u5143", font=font, fill=(22, 66, 128), anchor="lm")
        return canvas, bounds
    raise RuntimeError(f"unsupported input recipe: {recipe}")


def _create_review_packet(run_dir: Path, manifest: dict[str, Any]) -> Path:
    packet_dir = run_dir / "review_packet"
    if packet_dir.exists():
        return packet_dir
    cards_dir = packet_dir / "cards"
    cards_dir.mkdir(parents=True)
    cases = manifest.get("cases")
    assert isinstance(cases, dict)
    cards: list[dict[str, str]] = []
    for task_id, record in sorted(cases.items()):
        if record.get("status") != "completed_pending_review":
            continue
        input_path = run_dir / str(record["input_file"])
        result_path = run_dir / str(record["provider_raw_file"])
        with Image.open(input_path) as input_image, Image.open(result_path) as result_image:
            if input_image.size != result_image.size:
                raise RuntimeError("review card source and result dimensions do not match")
            card = Image.new("RGB", (input_image.width * 2, input_image.height), color=(255, 255, 255))
            card.paste(input_image.convert("RGB"), (0, 0))
            card.paste(result_image.convert("RGB"), (input_image.width, 0))
        card_name = f"{task_id.lower()}_card.png"
        card_path = cards_dir / card_name
        card.save(card_path, format="PNG")
        cards.append({"case_id": task_id, "card_file": f"cards/{card_name}", "card_sha256": _sha256_file(card_path)})
    packet_manifest = {
        "packet": "agentflow-mm2-g2-independent-review-v1",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_run": run_dir.name,
        "model_hidden_from_reviewers": True,
        "left_panel": "evaluation input",
        "right_panel": "model result",
        "case_count": len(cards),
        "cards": cards,
        "quality_claim": "none; blank reviewer forms are not a quality result",
    }
    _write_json(packet_dir / "manifest.json", packet_manifest)
    _write_review_csv(packet_dir / "reviewer_a.csv", cards)
    _write_review_csv(packet_dir / "reviewer_b.csv", cards)
    (packet_dir / "README.md").write_text(
        "# MM-2 G2 Image Review\n\n"
        "Each card shows the evaluation input on the left and the model result on the right. "
        "Do not use model name, provider facts, or implementation details while scoring. "
        "Score instruction adherence, target protection, and edge naturalness from 1 to 5. "
        "A score gap of two or more between reviewers requires recheck.\n",
        encoding="utf-8",
    )
    return packet_dir


def _write_review_csv(path: Path, cards: list[dict[str, str]]) -> None:
    columns = [
        "case_id",
        "card_file",
        "reviewer_id",
        "instruction_adherence",
        "target_protection",
        "edge_naturalness",
        "decision",
        "note",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for card in cards:
            writer.writerow({"case_id": card["case_id"], "card_file": card["card_file"]})


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    raise RuntimeError("a Chinese-capable Windows font is required to construct G2 text fixtures")


def _read_png(content: bytes) -> None:
    with Image.open(BytesIO(content)) as image:
        if str(image.format or "").upper() != "PNG":
            raise RuntimeError("G2 image artifact is not a PNG")
        image.verify()


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _seed_for_task(task_id: str) -> int:
    return int(hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:8], 16) % 2_147_483_647


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("JSON artifact cannot be read") from exc
    if not isinstance(value, dict):
        raise RuntimeError("JSON artifact must be an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
