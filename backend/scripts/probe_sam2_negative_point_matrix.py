"""Run frozen, effective negative-point probes across three SAM 2.1 Tiny categories.

Each correction point is deliberately inside the initial selection. The probe only
records whether SAM responds to a user correction on a reused image embedding; it
does not turn its internal score or a changed pixel count into a quality claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy
from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

from probe_sam2_hiera_tiny import (  # noqa: E402
    DEFAULT_MODEL_PATH,
    DEFAULT_SOURCE_DIR,
    MODEL_ID,
    PROMPTS,
    _git_commit,
    _load_predictor,
    _sha256,
    _validate_fixture_source,
)


INTERACTIONS: tuple[dict[str, object], ...] = (
    {
        "case_id": "MM0-PERSON-01",
        "category": "person",
        "negative_point_xy": [640, 1400],
        "correction_intent": "用户明确排除初始选区内的一个部位",
    },
    {
        "case_id": "MM0-PRODUCT-01",
        "category": "product",
        "negative_point_xy": [940, 270],
        "correction_intent": "用户明确排除初始选区内的一个商品区域",
    },
    {
        "case_id": "MM0-TRANSPARENT-01",
        "category": "transparent_object",
        "negative_point_xy": [640, 1500],
        "correction_intent": "用户明确排除初始选区内的透明物体区域",
    },
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to run the frozen negative-point matrix.")
        return

    fixture_dir = args.fixture_dir.resolve()
    fixtures = _load_fixtures(fixture_dir)
    model_path = args.model_path.resolve()
    source_dir = args.source_dir.resolve()
    if not model_path.is_file():
        raise SystemExit(f"model file does not exist: {model_path}")
    if not (source_dir / "sam2" / "sam2_image_predictor.py").is_file():
        raise SystemExit(f"SAM 2 source is not ready: {source_dir}")

    torch, predictor = _load_predictor(model_path, source_dir)
    output_dir = PROJECT_ROOT / "data" / "media_evaluations" / (
        "sam2_negative_point_matrix_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    cases = [
        _run_case(
            torch=torch,
            predictor=predictor,
            fixture=fixtures[str(interaction["case_id"])],
            fixture_dir=fixture_dir,
            interaction=interaction,
            output_dir=output_dir,
        )
        for interaction in INTERACTIONS
    ]
    manifest = {
        "probe": "sam2_hiera_tiny_effective_negative_point_matrix_v1",
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_id": MODEL_ID,
        "model_sha256": _sha256(model_path),
        "source_commit": _git_commit(source_dir),
        "device": "cpu",
        "fixture_set": "agentflow-mm0-public-image-fixtures-v2",
        "image_encode_call_count_per_case": 1,
        "case_count": len(cases),
        "cases": cases,
        "quality_claim": (
            "none; a correction point changing a binary mask does not establish "
            "object-boundary accuracy, alpha quality, or product readiness"
        ),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    effective_count = sum(1 for case in cases if case["negative_point_deselected"])
    print(json.dumps({"ok": True, "output_dir": str(output_dir), "effective_count": effective_count}))


def _load_fixtures(fixture_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("fixture-dir must contain a readable manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("fixture_set") != "agentflow-mm0-public-image-fixtures-v2":
        raise SystemExit("fixture-dir is not the frozen MM-0 public fixture set v2")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise SystemExit("fixture manifest contains no fixtures")
    indexed = {str(item.get("case_id")): item for item in fixtures if isinstance(item, dict)}
    required = {str(item["case_id"]) for item in INTERACTIONS}
    if set(indexed) < required:
        raise SystemExit("fixture set is missing a frozen interaction case")
    return indexed


def _run_case(
    *,
    torch: Any,
    predictor: Any,
    fixture: dict[str, Any],
    fixture_dir: Path,
    interaction: dict[str, object],
    output_dir: Path,
) -> dict[str, object]:
    case_id = str(interaction["case_id"])
    prompt = PROMPTS.get(case_id)
    if prompt is None:
        raise SystemExit(f"no frozen initial prompt for {case_id}")
    source_path = fixture_dir / str(fixture.get("file") or "")
    source_hash = _validate_fixture_source(source_path, fixture, fixture_dir)
    with Image.open(source_path) as opened:
        source = opened.convert("RGB")
    negative_point = _validated_point(interaction["negative_point_xy"], source.size, prompt, case_id)

    encode_started = time.perf_counter()
    predictor.set_image(numpy.asarray(source).copy())
    image_encode_ms = round((time.perf_counter() - encode_started) * 1000, 3)
    positive_point = prompt["positive_point_xy"]
    initial_started = time.perf_counter()
    with torch.inference_mode():
        initial_masks, initial_scores, initial_logits = predictor.predict(
            point_coords=numpy.asarray([positive_point], dtype=numpy.float32),
            point_labels=numpy.asarray([1], dtype=numpy.int32),
            box=numpy.asarray(prompt["box_xyxy"], dtype=numpy.float32),
            multimask_output=True,
        )
    initial_refine_ms = round((time.perf_counter() - initial_started) * 1000, 3)
    initial_index = int(numpy.argmax(initial_scores))
    initial_mask = numpy.asarray(initial_masks[initial_index], dtype=bool)
    _validate_mask(initial_mask, source.size, case_id, "initial")
    negative_x, negative_y = negative_point
    if not bool(initial_mask[negative_y, negative_x]):
        raise SystemExit(f"frozen negative point is not inside the initial selection: {case_id}")

    correction_started = time.perf_counter()
    with torch.inference_mode():
        refined_masks, refined_scores, _ = predictor.predict(
            point_coords=numpy.asarray([positive_point, negative_point], dtype=numpy.float32),
            point_labels=numpy.asarray([1, 0], dtype=numpy.int32),
            box=numpy.asarray(prompt["box_xyxy"], dtype=numpy.float32),
            mask_input=numpy.asarray(initial_logits[initial_index], dtype=numpy.float32)[None, ...],
            multimask_output=False,
        )
    correction_refine_ms = round((time.perf_counter() - correction_started) * 1000, 3)
    refined_mask = numpy.asarray(refined_masks[0], dtype=bool)
    _validate_mask(refined_mask, source.size, case_id, "refined")

    initial_mask_file = f"{case_id.lower()}_initial_mask.png"
    refined_mask_file = f"{case_id.lower()}_refined_mask.png"
    initial_preview_file = f"{case_id.lower()}_initial_selected.png"
    refined_preview_file = f"{case_id.lower()}_refined_selected.png"
    _save_mask(output_dir / initial_mask_file, initial_mask)
    _save_mask(output_dir / refined_mask_file, refined_mask)
    _save_selected(output_dir / initial_preview_file, source, initial_mask)
    _save_selected(output_dir / refined_preview_file, source, refined_mask)
    for name, mode in ((initial_mask_file, "L"), (refined_mask_file, "L"), (initial_preview_file, "RGBA"), (refined_preview_file, "RGBA")):
        _verify_png(output_dir / name, mode, source.size)

    changed_pixels = int(numpy.logical_xor(initial_mask, refined_mask).sum())
    selected_after = bool(refined_mask[negative_y, negative_x])
    return {
        "case_id": case_id,
        "category": interaction["category"],
        "correction_intent": interaction["correction_intent"],
        "source_sha256": source_hash,
        "source_size": list(source.size),
        "image_encode_call_count": 1,
        "image_encode_ms": image_encode_ms,
        "initial_prompt": {
            **prompt,
            "best_iou_score": float(initial_scores[initial_index]),
            "refine_ms": initial_refine_ms,
            "selected_pixel_count": int(initial_mask.sum()),
            "mask_file": initial_mask_file,
            "preview_file": initial_preview_file,
        },
        "negative_point_correction": {
            "negative_point_xy": negative_point,
            "reused_initial_logits": True,
            "best_iou_score": float(refined_scores[0]),
            "refine_ms": correction_refine_ms,
            "selected_pixel_count": int(refined_mask.sum()),
            "changed_pixel_count": changed_pixels,
            "negative_point_selected_before": True,
            "negative_point_selected_after": selected_after,
            "mask_file": refined_mask_file,
            "preview_file": refined_preview_file,
        },
        "negative_point_deselected": not selected_after,
    }


def _validated_point(value: object, size: tuple[int, int], prompt: dict[str, list[int]], case_id: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, int) for item in value):
        raise SystemExit(f"invalid frozen negative point for {case_id}")
    x, y = value
    x1, y1, x2, y2 = prompt["box_xyxy"]
    width, height = size
    if not (0 <= x < width and 0 <= y < height and x1 <= x <= x2 and y1 <= y <= y2):
        raise SystemExit(f"negative point is outside the frozen box: {case_id}")
    return x, y


def _validate_mask(mask: numpy.ndarray, size: tuple[int, int], case_id: str, stage: str) -> None:
    if mask.shape != (size[1], size[0]):
        raise SystemExit(f"{stage} mask has an unexpected size: {case_id}")


def _save_mask(path: Path, mask: numpy.ndarray) -> None:
    Image.fromarray(mask.astype(numpy.uint8) * 255, mode="L").save(path)


def _save_selected(path: Path, source: Image.Image, mask: numpy.ndarray) -> None:
    selected = source.convert("RGBA")
    selected.putalpha(Image.fromarray(mask.astype(numpy.uint8) * 255, mode="L"))
    selected.save(path)


def _verify_png(path: Path, expected_mode: str, expected_size: tuple[int, int]) -> None:
    with Image.open(path) as image:
        if image.format != "PNG" or image.mode != expected_mode or image.size != expected_size:
            raise SystemExit(f"output readback failed: {path.name}")


if __name__ == "__main__":
    main()
