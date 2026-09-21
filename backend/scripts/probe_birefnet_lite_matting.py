"""Run the official BiRefNet Lite Matting PyTorch candidate on approved MM-0 fixtures.

This is an isolated model-selection probe. It must run with the dedicated
``backend/.mm0_birefnet_matting_probe`` virtual environment and never becomes
an import-time dependency of the FastAPI service.
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

from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
DEFAULT_MODEL_PATH = (
    PROJECT_ROOT
    / "data"
    / "media_model_cache"
    / "birefnet_lite_matting"
    / "BiRefNet_lite-matting-epoch_110.pth"
)
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "media_model_cache" / "birefnet_v1_source"
MODEL_ID = "BiRefNet_lite-matting-epoch_110"


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
        print("Dry run only. Pass --execute to run the isolated Lite Matting probe.")
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
    torch, transforms, model = _load_model(model_path, source_dir)
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
        else PROJECT_ROOT / "data" / "media_evaluations" / ("birefnet_lite_matting_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    normalize = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    cases: list[dict[str, object]] = []
    for fixture in fixtures:
        case_id = str(fixture["case_id"])
        source_path = fixture_dir / str(fixture["file"])
        source_hash = _validate_fixture_source(source_path, fixture, fixture_dir)
        with Image.open(source_path) as opened:
            source = opened.convert("RGB")
        predictions = [_predict_alpha(torch, model, normalize, source) for _ in range(args.repeat_count)]
        alpha, elapsed_ms = predictions[-1]
        repeat_elapsed_ms = [elapsed for _, elapsed in predictions]
        if args.test_pause_before_commit_seconds:
            _write_test_stage(
                args.test_ready_file,
                stage="result_ready_before_commit",
                case_id=case_id,
                repeat_elapsed_ms=repeat_elapsed_ms,
            )
            time.sleep(args.test_pause_before_commit_seconds)
        alpha_name = f"{case_id.lower()}_alpha.png"
        cutout_name = f"{case_id.lower()}_cutout.png"
        alpha.save(output_dir / alpha_name)
        cutout = source.convert("RGBA")
        cutout.putalpha(alpha)
        cutout.save(output_dir / cutout_name)
        _verify_png(output_dir / alpha_name, "L", source.size)
        _verify_png(output_dir / cutout_name, "RGBA", source.size)
        cases.append(
            {
                "case_id": case_id,
                "source_sha256": source_hash,
                "source_size": list(source.size),
                "alpha_file": alpha_name,
                "cutout_file": cutout_name,
                "elapsed_ms": elapsed_ms,
                "repeat_elapsed_ms": repeat_elapsed_ms,
                "quality_status": "pending_human_review",
            }
        )

    manifest = {
        "executed": True,
        "model_id": MODEL_ID,
        "model_sha256": _sha256(model_path),
        "device": "cpu",
        "input_size": [1024, 1024],
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
    if not (source_dir / "models" / "birefnet.py").is_file():
        raise SystemExit(f"BiRefNet v1 source is not ready: {source_dir}")


def _load_model(model_path: Path, source_dir: Path) -> tuple[Any, Any, Any]:
    try:
        import torch
        from torchvision import transforms
    except ImportError as exc:
        raise SystemExit(
            "Install the isolated MM-0 probe environment before executing this script."
        ) from exc

    os.environ["HOME"] = str(source_dir)
    os.chdir(source_dir)
    if str(source_dir) not in sys.path:
        sys.path.insert(0, str(source_dir))
    import config

    original_init = config.Config.__init__

    def lite_matting_init(instance: Any) -> None:
        original_init(instance)
        instance.task = "P3M-10k"
        instance.bb = "swin_v1_t"
        instance.lateral_channels_in_collection = [768, 384, 192, 96]
        if instance.mul_scl_ipt == "cat":
            instance.lateral_channels_in_collection = [channel * 2 for channel in instance.lateral_channels_in_collection]
        instance.cxt = (
            instance.lateral_channels_in_collection[1:][::-1][-instance.cxt_num:]
            if instance.cxt_num
            else []
        )
        instance.device = "cpu"

    config.Config.__init__ = lite_matting_init
    from models.birefnet import BiRefNet

    model = BiRefNet(bb_pretrained=False)
    raw_state = torch.load(model_path, map_location="cpu", weights_only=True)
    if not isinstance(raw_state, dict):
        raise SystemExit("Lite Matting weight must be a state dictionary")
    state = {
        key.removeprefix("module._orig_mod.").removeprefix("_orig_mod.").removeprefix("module."): value
        for key, value in raw_state.items()
    }
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing or unexpected:
        raise SystemExit(f"Lite Matting weight mismatch: missing={len(missing)}, unexpected={len(unexpected)}")
    model.eval()
    return torch, transforms, model


def _predict_alpha(torch: Any, model: Any, normalize: Any, source: Image.Image) -> tuple[Image.Image, int]:
    resized = source.resize((1024, 1024), Image.Resampling.BILINEAR)
    tensor = normalize(torch.from_numpy(__import__("numpy").asarray(resized).copy()).permute(2, 0, 1).float() / 255.0)
    started = time.perf_counter()
    with torch.no_grad():
        prediction = model(tensor.unsqueeze(0))[-1].sigmoid()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    alpha = prediction.squeeze(0).squeeze(0).clamp(0, 1).mul(255).byte().cpu().numpy()
    return Image.fromarray(alpha, mode="L").resize(source.size, Image.Resampling.BILINEAR), elapsed_ms


def _validate_fixture_source(source_path: Path, fixture: dict[str, Any], fixture_dir: Path) -> str:
    if source_path.parent != fixture_dir or not source_path.is_file():
        raise SystemExit(f"invalid fixture source path: {source_path}")
    expected_hash = fixture.get("sha256")
    actual_hash = _sha256(source_path)
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
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
