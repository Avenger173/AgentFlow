"""Run the official SAM 2.1 Tiny box-and-point selection candidate on MM-0 fixtures.

This is model-selection evidence only. It runs in the isolated MM-0 PyTorch
environment and never becomes an import-time dependency of the API service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
DEFAULT_MODEL_PATH = (
    PROJECT_ROOT
    / "data"
    / "media_model_cache"
    / "sam2_hiera_tiny"
    / "sam2.1_hiera_tiny.pt"
)
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "media_model_cache" / "sam2_source"
MODEL_ID = "sam2.1_hiera_tiny"
MODEL_CONFIG = "configs/sam2.1/sam2.1_hiera_t.yaml"
PROMPTS: dict[str, dict[str, list[int]]] = {
    "MM0-PERSON-01": {"box_xyxy": [60, 0, 1200, 1598], "positive_point_xy": [640, 680]},
    "MM0-PERSON-02": {"box_xyxy": [5, 0, 535, 976], "positive_point_xy": [270, 300]},
    "MM0-PERSON-03": {"box_xyxy": [150, 180, 1120, 1918], "positive_point_xy": [650, 650]},
    "MM0-PRODUCT-01": {"box_xyxy": [800, 20, 1120, 440], "positive_point_xy": [940, 170]},
    "MM0-PRODUCT-02": {"box_xyxy": [180, 140, 1150, 900], "positive_point_xy": [700, 500]},
    "MM0-PRODUCT-03": {"box_xyxy": [120, 140, 850, 650], "positive_point_xy": [470, 350]},
    "MM0-TRANSPARENT-01": {"box_xyxy": [70, 430, 1190, 1815], "positive_point_xy": [640, 1100]},
    "MM0-TRANSPARENT-02": {"box_xyxy": [500, 0, 950, 750], "positive_point_xy": [730, 330]},
    "MM0-TRANSPARENT-03": {"box_xyxy": [200, 130, 1050, 1650], "positive_point_xy": [640, 950]},
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--case-id", action="append", help="只执行指定的已冻结夹具；可重复指定")
    parser.add_argument("--repeat-count", type=int, default=1, help="同一已加载 worker 内每个样本的推理次数")
    parser.add_argument("--output-dir", type=Path, help="显式测试输出目录；目录必须不存在")
    parser.add_argument("--test-ready-file", type=Path, help="仅治理探针：模型加载或提交前写入阶段标记")
    parser.add_argument(
        "--test-pause-before-commit-seconds",
        type=float,
        default=0.0,
        help="仅治理探针：结果生成后、任何 artifact 写入前暂停指定秒数",
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    if not args.execute:
        print("Dry run only. Pass --execute to run the isolated SAM 2.1 Tiny selection probe.")
        return

    fixture_dir = args.fixture_dir.resolve()
    model_path = args.model_path.resolve()
    source_dir = args.source_dir.resolve()
    fixtures, fixture_set = _load_fixtures(fixture_dir)
    fixtures = _select_fixtures(fixtures, args.case_id)
    if not 1 <= args.repeat_count <= 3:
        raise SystemExit("repeat-count must be between 1 and 3")
    if not 0.0 <= args.test_pause_before_commit_seconds <= 300.0:
        raise SystemExit("test-pause-before-commit-seconds must be between 0 and 300")
    if args.test_pause_before_commit_seconds and args.test_ready_file is None:
        raise SystemExit("test-pause-before-commit-seconds requires test-ready-file")
    _validate_model_source(model_path, source_dir)
    load_started = time.perf_counter()
    torch, predictor = _load_predictor(model_path, source_dir)
    model_load_ms = round((time.perf_counter() - load_started) * 1000, 3)
    _write_test_stage(
        args.test_ready_file,
        stage="model_loaded",
        model_id=MODEL_ID,
        model_load_ms=model_load_ms,
    )
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else PROJECT_ROOT / "data" / "media_evaluations" / ("sam2_hiera_tiny_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    cases: list[dict[str, object]] = []
    for fixture in fixtures:
        case_id = str(fixture["case_id"])
        prompt = PROMPTS.get(case_id)
        if prompt is None:
            raise SystemExit(f"no frozen selection prompt for {case_id}")
        source_path = fixture_dir / str(fixture["file"])
        source_hash = _validate_fixture_source(source_path, fixture, fixture_dir)
        with Image.open(source_path) as opened:
            source = opened.convert("RGB")
        _validate_prompt(prompt, source.size, case_id)
        predictions = [_predict_mask(torch, predictor, source, prompt) for _ in range(args.repeat_count)]
        mask, score, image_encode_ms, prompt_refine_ms = predictions[-1]
        repeat_elapsed_ms = [round(encoded + refined, 3) for _, _, encoded, refined in predictions]
        if args.test_pause_before_commit_seconds:
            _write_test_stage(
                args.test_ready_file,
                stage="result_ready_before_commit",
                case_id=case_id,
                repeat_elapsed_ms=repeat_elapsed_ms,
            )
            time.sleep(args.test_pause_before_commit_seconds)
        alpha_name = f"{case_id.lower()}_mask.png"
        cutout_name = f"{case_id.lower()}_selected.png"
        mask.save(output_dir / alpha_name)
        cutout = source.convert("RGBA")
        cutout.putalpha(mask)
        cutout.save(output_dir / cutout_name)
        _verify_png(output_dir / alpha_name, "L", source.size)
        _verify_png(output_dir / cutout_name, "RGBA", source.size)
        cases.append(
            {
                "case_id": case_id,
                "source_sha256": source_hash,
                "source_size": list(source.size),
                "prompt_mode": "box_plus_positive_point",
                **prompt,
                "best_iou_score": score,
                "mask_file": alpha_name,
                "selected_file": cutout_name,
                "image_encode_ms": image_encode_ms,
                "prompt_refine_ms": prompt_refine_ms,
                "total_elapsed_ms": round(image_encode_ms + prompt_refine_ms, 3),
                "repeat_elapsed_ms": repeat_elapsed_ms,
                "quality_status": "pending_human_review",
            }
        )

    manifest = {
        "executed": True,
        "model_id": MODEL_ID,
        "model_sha256": _sha256(model_path),
        "source_commit": _git_commit(source_dir),
        "device": "cpu",
        "model_load_ms": model_load_ms,
        "repeat_count": args.repeat_count,
        "fixture_set": fixture_set,
        "cases": cases,
        "quality_claim": "none; outputs require independent visual review before feature acceptance",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"ok": True, "output_dir": str(output_dir), "case_count": len(cases)}))


def _load_fixtures(fixture_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    manifest_path = fixture_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("fixture-dir must contain manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("fixtures"), list):
        raise SystemExit("fixture manifest must contain a fixtures list")
    fixtures = manifest["fixtures"]
    if not fixtures or not all(isinstance(item, dict) for item in fixtures):
        raise SystemExit("fixture manifest contains no valid fixtures")
    return fixtures, manifest.get("fixture_set")


def _select_fixtures(fixtures: list[dict[str, Any]], selected_ids: list[str] | None) -> list[dict[str, Any]]:
    if not selected_ids:
        return fixtures
    by_id = {str(item.get("case_id")): item for item in fixtures}
    selected: list[dict[str, Any]] = []
    for case_id in selected_ids:
        fixture = by_id.get(case_id)
        if fixture is None:
            raise SystemExit(f"unknown fixture case-id: {case_id}")
        if fixture not in selected:
            selected.append(fixture)
    return selected


def _validate_model_source(model_path: Path, source_dir: Path) -> None:
    if not model_path.is_file():
        raise SystemExit(f"model file does not exist: {model_path}")
    if not (source_dir / "sam2" / "sam2_image_predictor.py").is_file():
        raise SystemExit(f"SAM 2 source is not ready: {source_dir}")


def _load_predictor(model_path: Path, source_dir: Path) -> tuple[Any, Any]:
    try:
        import torch
    except ImportError as exc:
        raise SystemExit("Install the isolated MM-0 probe environment before executing this script.") from exc

    os.chdir(source_dir)
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    model = build_sam2(MODEL_CONFIG, str(model_path), device="cpu", apply_postprocessing=False)
    model.eval()
    return torch, SAM2ImagePredictor(model)


def _predict_mask(
    torch: Any,
    predictor: Any,
    source: Image.Image,
    prompt: dict[str, list[int]],
) -> tuple[Image.Image, float, float, float]:
    encode_started = time.perf_counter()
    predictor.set_image(numpy.asarray(source).copy())
    image_encode_ms = round((time.perf_counter() - encode_started) * 1000, 3)
    started = time.perf_counter()
    with torch.inference_mode():
        masks, scores, _ = predictor.predict(
            point_coords=numpy.asarray([prompt["positive_point_xy"]], dtype=numpy.float32),
            point_labels=numpy.asarray([1], dtype=numpy.int32),
            box=numpy.asarray(prompt["box_xyxy"], dtype=numpy.float32),
            multimask_output=True,
        )
    prompt_refine_ms = round((time.perf_counter() - started) * 1000, 3)
    best_index = int(numpy.argmax(scores))
    selected = numpy.asarray(masks[best_index], dtype=bool)
    if selected.shape != (source.height, source.width):
        raise SystemExit("SAM 2 returned a mask with an unexpected size")
    return (
        Image.fromarray(selected.astype(numpy.uint8) * 255, mode="L"),
        float(scores[best_index]),
        image_encode_ms,
        prompt_refine_ms,
    )


def _validate_prompt(prompt: dict[str, list[int]], size: tuple[int, int], case_id: str) -> None:
    width, height = size
    box = prompt["box_xyxy"]
    point = prompt["positive_point_xy"]
    if len(box) != 4 or len(point) != 2:
        raise SystemExit(f"invalid frozen prompt for {case_id}")
    x1, y1, x2, y2 = box
    x, y = point
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height and x1 <= x <= x2 and y1 <= y <= y2):
        raise SystemExit(f"frozen prompt is outside {case_id} source bounds")


def _validate_fixture_source(source_path: Path, fixture: dict[str, Any], fixture_dir: Path) -> str:
    if source_path.parent != fixture_dir or not source_path.is_file():
        raise SystemExit(f"invalid fixture source path: {source_path}")
    actual_hash = _sha256(source_path)
    if actual_hash != fixture.get("sha256"):
        raise SystemExit(f"fixture hash mismatch: {fixture.get('case_id')}")
    return actual_hash


def _verify_png(path: Path, expected_mode: str, expected_size: tuple[int, int]) -> None:
    with Image.open(path) as image:
        if image.format != "PNG" or image.mode != expected_mode or image.size != expected_size:
            raise SystemExit(f"output readback failed: {path.name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(source_dir: Path) -> str:
    head = source_dir / ".git" / "HEAD"
    if not head.is_file():
        return "unknown"
    content = head.read_text(encoding="ascii").strip()
    if content.startswith("ref: "):
        ref = source_dir / ".git" / content.removeprefix("ref: ")
        return ref.read_text(encoding="ascii").strip() if ref.is_file() else "unknown"
    return content


def _write_test_stage(path: Path | None, *, stage: str, **fields: object) -> None:
    """仅供隔离 worker 治理探针同步阶段，不参与用户任务或正式 artifact。"""

    if path is None:
        return
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {"stage": stage, **fields}
    temporary = resolved.with_name(f"{resolved.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(resolved)


if __name__ == "__main__":
    main()
