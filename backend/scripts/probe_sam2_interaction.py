"""Run one frozen SAM 2.1 Tiny positive/negative point refinement probe.

This is MM-0 evidence for reusing one encoded image across an explicit user-like
selection correction. It does not claim that the resulting object boundary is
visually acceptable; that remains an independent-review requirement.
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
    _git_commit,
    _load_predictor,
    _sha256,
    _validate_fixture_source,
)


CASE_ID = "MM0-PRODUCT-02"
BOX_XYXY = [180, 140, 1150, 900]
POSITIVE_POINT_XY = [700, 500]
NEGATIVE_POINT_XY = [1050, 850]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to run the SAM point-refinement probe.")
        return

    fixture_dir = args.fixture_dir.resolve()
    model_path = args.model_path.resolve()
    source_dir = args.source_dir.resolve()
    fixture = _load_frozen_fixture(fixture_dir)
    source_path = fixture_dir / str(fixture["file"])
    source_hash = _validate_fixture_source(source_path, fixture, fixture_dir)
    with Image.open(source_path) as opened:
        source = opened.convert("RGB")
    _validate_points(source.size)

    torch, predictor = _load_predictor(model_path, source_dir)
    encode_started = time.perf_counter()
    predictor.set_image(numpy.asarray(source).copy())
    image_encode_ms = round((time.perf_counter() - encode_started) * 1000, 3)

    initial_started = time.perf_counter()
    with torch.inference_mode():
        initial_masks, initial_scores, initial_logits = predictor.predict(
            point_coords=numpy.asarray([POSITIVE_POINT_XY], dtype=numpy.float32),
            point_labels=numpy.asarray([1], dtype=numpy.int32),
            box=numpy.asarray(BOX_XYXY, dtype=numpy.float32),
            multimask_output=True,
        )
    initial_refine_ms = round((time.perf_counter() - initial_started) * 1000, 3)
    initial_index = int(numpy.argmax(initial_scores))
    initial_mask = numpy.asarray(initial_masks[initial_index], dtype=bool)

    correction_started = time.perf_counter()
    with torch.inference_mode():
        refined_masks, refined_scores, _ = predictor.predict(
            point_coords=numpy.asarray([POSITIVE_POINT_XY, NEGATIVE_POINT_XY], dtype=numpy.float32),
            point_labels=numpy.asarray([1, 0], dtype=numpy.int32),
            box=numpy.asarray(BOX_XYXY, dtype=numpy.float32),
            # 复用第一次的低分辨率 logits；不得重新调用 set_image。
            mask_input=numpy.asarray(initial_logits[initial_index], dtype=numpy.float32)[None, ...],
            multimask_output=False,
        )
    correction_refine_ms = round((time.perf_counter() - correction_started) * 1000, 3)
    refined_mask = numpy.asarray(refined_masks[0], dtype=bool)
    _validate_mask(initial_mask, source.size, "initial")
    _validate_mask(refined_mask, source.size, "refined")

    output_dir = PROJECT_ROOT / "data" / "media_evaluations" / (
        "sam2_interaction_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    initial_mask_file = "initial_mask.png"
    refined_mask_file = "refined_mask.png"
    initial_preview_file = "initial_selected.png"
    refined_preview_file = "refined_selected.png"
    _save_mask(output_dir / initial_mask_file, initial_mask)
    _save_mask(output_dir / refined_mask_file, refined_mask)
    _save_selected(source, initial_mask, output_dir / initial_preview_file)
    _save_selected(source, refined_mask, output_dir / refined_preview_file)
    _verify_png(output_dir / initial_mask_file, "L", source.size)
    _verify_png(output_dir / refined_mask_file, "L", source.size)
    _verify_png(output_dir / initial_preview_file, "RGBA", source.size)
    _verify_png(output_dir / refined_preview_file, "RGBA", source.size)

    negative_x, negative_y = NEGATIVE_POINT_XY
    changed_pixels = int(numpy.logical_xor(initial_mask, refined_mask).sum())
    manifest = {
        "probe": "sam2_hiera_tiny_point_refinement_v1",
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_id": MODEL_ID,
        "model_sha256": _sha256(model_path),
        "source_commit": _git_commit(source_dir),
        "device": "cpu",
        "fixture_set": "agentflow-mm0-public-image-fixtures-v2",
        "case_id": CASE_ID,
        "source_sha256": source_hash,
        "source_size": list(source.size),
        "image_encode_call_count": 1,
        "image_encode_ms": image_encode_ms,
        "initial_prompt": {
            "box_xyxy": BOX_XYXY,
            "positive_point_xy": POSITIVE_POINT_XY,
            "best_iou_score": float(initial_scores[initial_index]),
            "refine_ms": initial_refine_ms,
            "selected_pixel_count": int(initial_mask.sum()),
            "mask_file": initial_mask_file,
            "preview_file": initial_preview_file,
        },
        "negative_point_correction": {
            "negative_point_xy": NEGATIVE_POINT_XY,
            "reused_initial_logits": True,
            "best_iou_score": float(refined_scores[0]),
            "refine_ms": correction_refine_ms,
            "selected_pixel_count": int(refined_mask.sum()),
            "changed_pixel_count": changed_pixels,
            "negative_point_selected_before": bool(initial_mask[negative_y, negative_x]),
            "negative_point_selected_after": bool(refined_mask[negative_y, negative_x]),
            "mask_file": refined_mask_file,
            "preview_file": refined_preview_file,
        },
        "quality_claim": "none; this proves a reusable two-turn prompt path, not object-boundary quality",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output_dir": str(output_dir), "changed_pixel_count": changed_pixels}))


def _load_frozen_fixture(fixture_dir: Path) -> dict[str, Any]:
    manifest_path = fixture_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("fixture-dir must contain a readable manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("fixture_set") != "agentflow-mm0-public-image-fixtures-v2":
        raise SystemExit("fixture-dir is not the frozen MM-0 public fixture set v2")
    fixture = next(
        (item for item in manifest.get("fixtures", []) if isinstance(item, dict) and item.get("case_id") == CASE_ID),
        None,
    )
    if not isinstance(fixture, dict):
        raise SystemExit(f"fixture set does not contain {CASE_ID}")
    return fixture


def _validate_points(size: tuple[int, int]) -> None:
    width, height = size
    x1, y1, x2, y2 = BOX_XYXY
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise SystemExit("frozen interaction box is outside the source image")
    for point in (POSITIVE_POINT_XY, NEGATIVE_POINT_XY):
        x, y = point
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            raise SystemExit("frozen interaction point is outside the selected box")


def _validate_mask(mask: numpy.ndarray, size: tuple[int, int], name: str) -> None:
    width, height = size
    if mask.shape != (height, width):
        raise SystemExit(f"{name} mask does not match the source size")


def _save_mask(path: Path, mask: numpy.ndarray) -> None:
    Image.fromarray(mask.astype(numpy.uint8) * 255, mode="L").save(path)


def _save_selected(source: Image.Image, mask: numpy.ndarray, path: Path) -> None:
    selected = source.convert("RGBA")
    selected.putalpha(Image.fromarray(mask.astype(numpy.uint8) * 255, mode="L"))
    selected.save(path)


def _verify_png(path: Path, expected_mode: str, expected_size: tuple[int, int]) -> None:
    with Image.open(path) as image:
        if image.format != "PNG" or image.mode != expected_mode or image.size != expected_size:
            raise SystemExit(f"output readback failed: {path.name}")


if __name__ == "__main__":
    main()
