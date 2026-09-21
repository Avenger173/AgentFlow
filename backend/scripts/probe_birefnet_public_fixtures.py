"""在准备好的公开 MM-0 图片夹具上运行 BiRefNet Tiny 前景蒙版候选。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.birefnet_foreground_mask import (  # noqa: E402
    create_birefnet_foreground_alpha_mask,
    create_birefnet_foreground_mask_session,
    inspect_birefnet_foreground_mask_readiness,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True, help="prepare_media_evaluation_fixtures.py 生成的目录")
    parser.add_argument("--execute", action="store_true", help="执行本机 CPU 前景蒙版推理")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to run local foreground-mask inference.")
        return
    fixture_dir = args.fixture_dir.resolve()
    manifest_path = fixture_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("fixture-dir 缺少 manifest.json。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixtures = manifest.get("fixtures") if isinstance(manifest, dict) else None
    if not isinstance(fixtures, list) or not fixtures:
        raise SystemExit("夹具 manifest 没有可执行的 fixtures。")
    readiness = inspect_birefnet_foreground_mask_readiness()
    if not readiness.ready:
        raise SystemExit(readiness.reason)
    output_dir = PROJECT_ROOT / "data" / "media_evaluations" / (
        "birefnet_public_fixtures_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    session = create_birefnet_foreground_mask_session()
    cases: list[dict[str, object]] = []
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise SystemExit("夹具 manifest 包含非法条目。")
        case_id = str(fixture.get("case_id") or "").strip()
        source_name = str(fixture.get("file") or "").strip()
        source_path = (fixture_dir / source_name).resolve()
        if not case_id or not source_path.is_file() or source_path.parent != fixture_dir:
            raise SystemExit("夹具文件引用无效或越出固定目录。")
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != fixture.get("sha256"):
            raise SystemExit(f"{case_id} 的夹具哈希不匹配。")
        with Image.open(source_path) as source:
            result = create_birefnet_foreground_alpha_mask(source, session=session)
        alpha_name = f"{case_id.lower()}_alpha.png"
        result.alpha_mask.save(output_dir / alpha_name)
        cases.append(
            {
                "case_id": case_id,
                "source_sha256": actual_hash,
                "source_size": list(result.source_size),
                "alpha_file": alpha_name,
                "alpha_mode": result.alpha_mask.mode,
                "alpha_size": list(result.alpha_mask.size),
                "elapsed_ms": result.elapsed_ms,
                "quality_status": "pending_human_review",
            }
        )
    evaluation = {
        "executed": True,
        "model_id": readiness.model_id,
        "device": readiness.device,
        "fixture_set": manifest.get("fixture_set"),
        "cases": cases,
        "quality_claim": "none; inspect each alpha mask against source before MODEL-03 is scored",
    }
    (output_dir / "manifest.json").write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output_dir": str(output_dir), "case_count": len(cases)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
