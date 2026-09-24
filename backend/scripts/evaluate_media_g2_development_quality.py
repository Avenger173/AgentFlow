"""Produce reproducible, offline development evidence for an MM-2 G2 image run.

This script deliberately separates mechanical evidence from human visual judgement.
It validates every persisted input/output pair, verifies that each task produced the
minimum expected local effect, and uses the already prepared local OCR model to check
the synthetic Chinese price edit.  It never calls a Provider, retries a generation,
or claims that objective image deltas prove naturalness or subject preservation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.ocr_adapter import OcrAdapter, OcrAdapterError  # noqa: E402


_EXPECTED_STATUS = "completed_pending_review"
_CATEGORIES = {"background_replace", "object_removal", "text_edit"}
_SPLITS = {"development", "holdout"}
_FONT_PATHS = (Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\msyhbd.ttc"))


@dataclass(frozen=True)
class _CaseImages:
    task_id: str
    category: str
    split: str
    input_path: Path
    output_path: Path
    input_image: Image.Image
    output_image: Image.Image
    target_bounds: tuple[int, int, int, int] | None


def main() -> None:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path, help="Directory created by probe_qwen_image_g2_quality.py.")
    source.add_argument("--self-test", action="store_true", help="Run an offline positive and negative contract test.")
    parser.add_argument(
        "--write-artifacts",
        action="store_true",
        help="Write a JSON report and three category contact sheets into the ignored run directory.",
    )
    parser.add_argument(
        "--skip-ocr",
        action="store_true",
        help="Do not run local OCR. Text-edit cases then remain objectively inconclusive and cannot pass.",
    )
    args = parser.parse_args()

    try:
        if args.self_test:
            report = _run_self_test()
        else:
            assert args.run_dir is not None
            report, sheets = _evaluate_run(args.run_dir.resolve(), use_ocr=not args.skip_ocr)
            if args.write_artifacts:
                _write_json(args.run_dir.resolve() / "developer_quality_report.json", report)
                for category, image in sheets.items():
                    image.save(args.run_dir.resolve() / f"developer_quality_{category}.png", format="PNG")
    except (OSError, ValueError, OcrAdapterError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc

    print(json.dumps(report, ensure_ascii=False))
    if not args.self_test and not report["objective_gate_passed"]:
        raise SystemExit(1)


def _evaluate_run(run_dir: Path, *, use_ocr: bool) -> tuple[dict[str, object], dict[str, Image.Image]]:
    run_dir = run_dir.resolve()
    manifest = _read_json(run_dir / "manifest.json")
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, dict) or not raw_cases:
        raise ValueError("run manifest has no cases")

    ocr = OcrAdapter() if use_ocr else None
    records: list[dict[str, object]] = []
    images: list[_CaseImages] = []
    with tempfile.TemporaryDirectory(prefix="agentflow_g2_ocr_crop_") as ocr_temp:
        ocr_work_dir = Path(ocr_temp) if ocr is not None else None
        for task_id, raw_case in sorted(raw_cases.items()):
            if not isinstance(raw_case, dict):
                raise ValueError(f"case is not an object: {task_id}")
            case_images = _load_case_images(run_dir, str(task_id), raw_case)
            record = _evaluate_case(case_images, raw_case, ocr=ocr, ocr_work_dir=ocr_work_dir)
            records.append(record)
            images.append(case_images)

    category_summary = _summarize(records, key="category", expected=_CATEGORIES)
    split_summary = _summarize(records, key="split", expected=_SPLITS)
    failures = [str(record["task_id"]) for record in records if not record["objective_effect_passed"]]
    integrity_failures = [str(record["task_id"]) for record in records if not record["integrity_passed"]]
    gate_passed = not failures and not integrity_failures
    report: dict[str, object] = {
        "ok": True,
        "assessment": "agentflow-mm2-g2-development-objective-v1",
        "source_run": run_dir.name,
        "task_count": len(records),
        "objective_gate_passed": gate_passed,
        "objective_gate_meaning": (
            "All persisted task pairs passed integrity plus category-specific minimum-effect checks. "
            "This is a development result, not an independent content-quality or release approval."
            if gate_passed
            else "At least one persisted task lacks integrity or its required minimum effect; inspect the listed case IDs."
        ),
        "integrity_passed": len(integrity_failures) == 0,
        "integrity_failure_case_ids": integrity_failures,
        "objective_effect_failure_case_ids": failures,
        "category_summary": category_summary,
        "split_summary": split_summary,
        "visual_review_state": "not_scored_by_this_offline_evaluator",
        "visual_review_next_step": (
            "Use the generated category contact sheets for creator screening. Formal independent review is a separate "
            "human decision and must not be inferred from this report."
        ),
        "cases": records,
        "provider_calls": 0,
        "network_calls": 0,
    }
    sheets = {category: _contact_sheet(category, images) for category in sorted(_CATEGORIES)}
    return report, sheets


def _load_case_images(run_dir: Path, task_id: str, raw_case: dict[str, object]) -> _CaseImages:
    category = str(raw_case.get("category") or "")
    split = str(raw_case.get("split") or "")
    if category not in _CATEGORIES or split not in _SPLITS:
        raise ValueError(f"case {task_id} has unsupported category or split")
    input_path = _safe_run_file(run_dir, raw_case.get("input_file"), task_id, "input")
    output_path = _safe_run_file(run_dir, raw_case.get("provider_raw_file"), task_id, "output")
    if str(raw_case.get("status") or "") != _EXPECTED_STATUS:
        raise ValueError(f"case {task_id} has no completed Provider output")
    _verify_sha256(input_path, str(raw_case.get("input_sha256") or ""), task_id, "input")
    _verify_sha256(output_path, str(raw_case.get("provider_raw_sha256") or ""), task_id, "output")
    target_bounds = _target_bounds(raw_case.get("target_bounds"), task_id, required=category != "background_replace")
    with Image.open(input_path) as input_source, Image.open(output_path) as output_source:
        if input_source.format != "PNG" or output_source.format != "PNG":
            raise ValueError(f"case {task_id} is not persisted as PNG")
        input_image = input_source.convert("RGB")
        output_image = output_source.convert("RGB")
    if input_image.size != output_image.size:
        raise ValueError(f"case {task_id} output dimensions do not match input")
    if target_bounds is not None and not _within_bounds(target_bounds, input_image.size):
        raise ValueError(f"case {task_id} has invalid target bounds")
    return _CaseImages(
        task_id=task_id,
        category=category,
        split=split,
        input_path=input_path,
        output_path=output_path,
        input_image=input_image,
        output_image=output_image,
        target_bounds=target_bounds,
    )


def _evaluate_case(
    case: _CaseImages,
    raw_case: dict[str, object],
    *,
    ocr: OcrAdapter | None,
    ocr_work_dir: Path | None,
) -> dict[str, object]:
    source = np.asarray(case.input_image, dtype=np.int16)
    result = np.asarray(case.output_image, dtype=np.int16)
    overall_delta = _mean_abs_delta(source, result)
    metrics: dict[str, object] = {"overall_mean_abs_delta": round(overall_delta, 5)}
    checks: dict[str, bool] = {}
    if case.category == "background_replace":
        border_delta = _border_mean_abs_delta(source, result)
        metrics["border_mean_abs_delta"] = round(border_delta, 5)
        checks = {
            "image_changed": overall_delta >= 0.01,
            "border_changed": border_delta >= 0.01,
        }
    elif case.category == "object_removal":
        assert case.target_bounds is not None
        target_source = _crop_array(source, case.target_bounds)
        target_result = _crop_array(result, case.target_bounds)
        source_red = _red_marker_ratio(target_source)
        result_red = _red_marker_ratio(target_result)
        target_delta = _mean_abs_delta(target_source, target_result)
        metrics.update(
            {
                "target_mean_abs_delta": round(target_delta, 5),
                "input_red_marker_ratio": round(source_red, 5),
                "output_red_marker_ratio": round(result_red, 5),
            }
        )
        checks = {
            "synthetic_marker_present_in_input": source_red >= 0.08,
            "target_region_changed": target_delta >= 0.02,
            # The original scene can legitimately contain red objects or reflections.  The synthetic sticker
            # fills roughly two thirds of this box, so allow small natural red detail but reject a retained marker.
            "red_marker_removed": result_red <= max(0.10, source_red * 0.15),
        }
    else:
        assert case.target_bounds is not None
        target_source = _crop_array(source, case.target_bounds)
        target_result = _crop_array(result, case.target_bounds)
        target_delta = _mean_abs_delta(target_source, target_result)
        metrics.update(
            {
                "target_mean_abs_delta": round(target_delta, 5),
                "output_blue_ink_ratio": round(_blue_ink_ratio(target_result), 5),
            }
        )
        checks = {
            "target_region_changed": target_delta >= 0.008,
            "blue_label_ink_retained": _blue_ink_ratio(target_result) >= 0.001,
        }
        if ocr is None:
            checks["ocr_price_exact"] = False
            metrics["ocr_state"] = "skipped"
        else:
            assert ocr_work_dir is not None
            before = _ocr_text(ocr, case.input_image, case.target_bounds, ocr_work_dir, case.task_id, "input")
            after = _ocr_text(ocr, case.output_image, case.target_bounds, ocr_work_dir, case.task_id, "output")
            metrics["ocr_input"] = before
            metrics["ocr_output"] = after
            checks["ocr_price_exact"] = _contains_expected_price(before, "99") and _contains_expected_price(after, "129")

    integrity = _integrity_from_case(case, raw_case)
    effect_passed = integrity and all(checks.values())
    return {
        "task_id": case.task_id,
        "category": case.category,
        "split": case.split,
        "integrity_passed": integrity,
        "objective_effect_passed": effect_passed,
        "checks": checks,
        "metrics": metrics,
    }


def _integrity_from_case(case: _CaseImages, raw_case: dict[str, object]) -> bool:
    declared_size = raw_case.get("input_size")
    return (
        case.input_image.size == case.output_image.size
        and isinstance(declared_size, list)
        and declared_size == [case.input_image.width, case.input_image.height]
    )


def _ocr_text(
    ocr: OcrAdapter,
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    work_dir: Path,
    task_id: str,
    variant: str,
) -> str:
    crop_path = work_dir / f"{task_id.lower()}_{variant}_label.png"
    image.crop(bounds).save(crop_path, format="PNG")
    document = ocr.recognize_path(crop_path)
    text = "".join(page.text for page in document.pages)
    return "".join(text.split())


def _contains_expected_price(text: str, amount: str) -> bool:
    """Accept harmless repeated OCR glyphs while still requiring all semantic price tokens."""
    return all(token in text for token in ("新", "品", amount, "元"))


def _summarize(records: list[dict[str, object]], *, key: str, expected: set[str]) -> dict[str, dict[str, object]]:
    buckets: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        buckets[str(record[key])].append(record)
    if set(buckets) != expected:
        raise ValueError(f"run does not contain the expected {key} coverage")
    summary: dict[str, dict[str, object]] = {}
    for value in sorted(expected):
        bucket = buckets[value]
        passed = sum(1 for record in bucket if record["objective_effect_passed"])
        summary[value] = {
            "total": len(bucket),
            "objective_effect_passed": passed,
            "objective_effect_pass_rate": round(passed / len(bucket), 4),
            "failed_case_ids": [str(record["task_id"]) for record in bucket if not record["objective_effect_passed"]],
        }
    return summary


def _contact_sheet(category: str, cases: list[_CaseImages]) -> Image.Image:
    selected = [case for case in cases if case.category == category]
    if len(selected) != 12:
        raise ValueError(f"contact sheet requires 12 {category} cases")
    cell_width, cell_height, title_height = 600, 390, 30
    canvas = Image.new("RGB", (cell_width * 3, cell_height * 4), "white")
    draw = ImageDraw.Draw(canvas)
    font = _load_font(18)
    for index, case in enumerate(selected):
        left = (index % 3) * cell_width
        top = (index // 3) * cell_height
        draw.rectangle((left, top, left + cell_width - 1, top + cell_height - 1), outline=(180, 180, 180), width=1)
        draw.text((left + 8, top + 5), f"{case.task_id}  [{case.split}]", fill=(20, 20, 20), font=font)
        panel_width = (cell_width - 18) // 2
        panel_height = cell_height - title_height - 14
        _paste_fit(canvas, case.input_image, (left + 6, top + title_height), (panel_width, panel_height))
        _paste_fit(canvas, case.output_image, (left + 10 + panel_width, top + title_height), (panel_width, panel_height))
    return canvas


def _paste_fit(canvas: Image.Image, image: Image.Image, origin: tuple[int, int], bounds: tuple[int, int]) -> None:
    max_width, max_height = bounds
    scale = min(max_width / image.width, max_height / image.height)
    target = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    x = origin[0] + (max_width - target.width) // 2
    y = origin[1] + (max_height - target.height) // 2
    canvas.paste(target, (x, y))


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _safe_run_file(run_dir: Path, value: object, task_id: str, label: str) -> Path:
    relative = Path(str(value or ""))
    if not relative.name or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"case {task_id} has invalid {label} path")
    path = (run_dir / relative).resolve()
    if path.parent != run_dir or not path.is_file():
        raise ValueError(f"case {task_id} {label} file is missing")
    return path


def _verify_sha256(path: Path, expected: str, task_id: str, label: str) -> None:
    if len(expected) != 64 or _sha256_file(path) != expected.lower():
        raise ValueError(f"case {task_id} {label} hash mismatch")


def _target_bounds(value: object, task_id: str, *, required: bool) -> tuple[int, int, int, int] | None:
    if value is None and not required:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"case {task_id} has invalid target bounds")
    try:
        bounds = tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"case {task_id} has invalid target bounds") from exc
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError(f"case {task_id} has empty target bounds")
    return bounds


def _within_bounds(bounds: tuple[int, int, int, int], size: tuple[int, int]) -> bool:
    left, top, right, bottom = bounds
    return 0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]


def _crop_array(image: np.ndarray, bounds: tuple[int, int, int, int]) -> np.ndarray:
    left, top, right, bottom = bounds
    return image[top:bottom, left:right]


def _mean_abs_delta(input_image: np.ndarray, output_image: np.ndarray) -> float:
    return float(np.abs(input_image - output_image).mean() / 255.0)


def _border_mean_abs_delta(input_image: np.ndarray, output_image: np.ndarray) -> float:
    height, width = input_image.shape[:2]
    border = max(8, min(height, width) // 12)
    mask = np.zeros((height, width), dtype=bool)
    mask[:border, :] = True
    mask[-border:, :] = True
    mask[:, :border] = True
    mask[:, -border:] = True
    return float(np.abs(input_image - output_image)[mask].mean() / 255.0)


def _red_marker_ratio(image: np.ndarray) -> float:
    red, green, blue = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    marker = (red >= 150) & (green <= 125) & (blue <= 125) & ((red - green) >= 55) & ((red - blue) >= 55)
    return float(marker.mean())


def _blue_ink_ratio(image: np.ndarray) -> float:
    red, green, blue = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    ink = (blue >= 80) & ((blue - red) >= 35) & ((blue - green) >= 20) & (red <= 100)
    return float(ink.mean())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("run manifest is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("run manifest must be an object")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agentflow_g2_development_eval_") as temporary:
        root = Path(temporary)
        cases: dict[str, dict[str, object]] = {}
        for number, category in enumerate(sorted(_CATEGORIES), start=1):
            task_id = f"SELF-{category}"
            source, result, bounds = _self_test_images(category)
            input_path = root / f"{task_id}_input.png"
            output_path = root / f"{task_id}_output.png"
            source.save(input_path, format="PNG")
            result.save(output_path, format="PNG")
            cases[task_id] = {
                "status": _EXPECTED_STATUS,
                "category": category,
                "split": "development" if number < 3 else "holdout",
                "input_file": input_path.name,
                "provider_raw_file": output_path.name,
                "input_sha256": _sha256_file(input_path),
                "provider_raw_sha256": _sha256_file(output_path),
                "input_size": [source.width, source.height],
                "target_bounds": list(bounds) if bounds is not None else None,
            }
        # Add enough cases to exercise the fixed suite summary shape without calling OCR.
        for number in range(2, 13):
            for category in sorted(_CATEGORIES):
                task_id = f"SELF-{number:02d}-{category}"
                source, result, bounds = _self_test_images(category)
                input_path = root / f"{task_id}_input.png"
                output_path = root / f"{task_id}_output.png"
                source.save(input_path, format="PNG")
                result.save(output_path, format="PNG")
                cases[task_id] = {
                    "status": _EXPECTED_STATUS,
                    "category": category,
                    "split": "development" if number <= 8 else "holdout",
                    "input_file": input_path.name,
                    "provider_raw_file": output_path.name,
                    "input_sha256": _sha256_file(input_path),
                    "provider_raw_sha256": _sha256_file(output_path),
                    "input_size": [source.width, source.height],
                    "target_bounds": list(bounds) if bounds is not None else None,
                }
        _write_json(root / "manifest.json", {"cases": cases})
        report, _ = _evaluate_run(root, use_ocr=False)
        # Text tasks intentionally cannot pass when OCR is skipped; a red residual also must not pass.
        if report["objective_gate_passed"]:
            raise AssertionError("development evaluator accepted text edits without OCR")
        broken = cases["SELF-02-object_removal"]
        broken["provider_raw_file"] = broken["input_file"]
        broken["provider_raw_sha256"] = broken["input_sha256"]
        _write_json(root / "manifest.json", {"cases": cases})
        negative, _ = _evaluate_run(root, use_ocr=False)
        if "SELF-02-object_removal" not in negative["objective_effect_failure_case_ids"]:
            raise AssertionError("development evaluator accepted a retained synthetic marker")
    return {
        "ok": True,
        "assessment": "agentflow-mm2-g2-development-objective-v1",
        "self_test": True,
        "provider_calls": 0,
        "network_calls": 0,
        "negative_contract_check": "retained_marker_rejected_and_ocr_required_for_text",
    }


def _self_test_images(category: str) -> tuple[Image.Image, Image.Image, tuple[int, int, int, int] | None]:
    source = Image.new("RGB", (160, 120), (235, 235, 235))
    result = source.copy()
    draw = ImageDraw.Draw(source)
    result_draw = ImageDraw.Draw(result)
    if category == "background_replace":
        result_draw.rectangle((0, 0, 159, 119), fill=(180, 205, 220))
        return source, result, None
    if category == "object_removal":
        bounds = (100, 60, 150, 110)
        draw.ellipse(bounds, fill=(214, 49, 45))
        return source, result, bounds
    bounds = (10, 10, 110, 55)
    draw.rectangle(bounds, fill=(250, 250, 248))
    draw.text((15, 20), "99", fill=(22, 66, 128))
    result_draw.rectangle(bounds, fill=(250, 250, 248))
    result_draw.text((15, 20), "129", fill=(22, 66, 128))
    return source, result, bounds


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
