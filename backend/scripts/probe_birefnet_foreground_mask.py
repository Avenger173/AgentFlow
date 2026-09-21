"""受控执行 BiRefNet Tiny ONNX 的本机前景蒙版探针。

默认只说明不会执行。传入 ``--execute`` 后，脚本只使用程序生成的诊断图片，不读取、上传、
复制或保存用户图片；产生的本地证据写入忽略目录 ``data/media_evaluations``。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw

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
    parser.add_argument("--execute", action="store_true", help="执行本机 ONNX 推理并写入忽略的探针证据")
    args = parser.parse_args()
    readiness = inspect_birefnet_foreground_mask_readiness()
    if not args.execute:
        print(json.dumps({"executed": False, "ready": readiness.ready, "reason": readiness.reason}, ensure_ascii=False))
        return
    if not readiness.ready:
        raise SystemExit(readiness.reason)

    source = _diagnostic_image()
    session = create_birefnet_foreground_mask_session()
    result = create_birefnet_foreground_alpha_mask(source, session=session)
    evidence_dir = PROJECT_ROOT / "data" / "media_evaluations" / (
        "birefnet_foreground_mask_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    evidence_dir.mkdir(parents=True, exist_ok=False)
    source.save(evidence_dir / "generated_input.png")
    result.alpha_mask.save(evidence_dir / "output_alpha_mask.png")
    manifest = {
        "executed": True,
        "fixture": "program_generated_diagnostic_only_not_a_quality_fixture",
        "model_id": result.model_id,
        "device": readiness.device,
        "source_size": list(result.source_size),
        "alpha_size": list(result.alpha_mask.size),
        "alpha_mode": result.alpha_mask.mode,
        "elapsed_ms": result.elapsed_ms,
        "output_readback": (evidence_dir / "output_alpha_mask.png").is_file(),
        "quality_claim": "none; real licensed fixtures are required for matting or segmentation quality acceptance",
    }
    (evidence_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"evidence_dir": str(evidence_dir), **manifest}, ensure_ascii=False))


def _diagnostic_image() -> Image.Image:
    image = Image.new("RGB", (960, 640), color=(236, 239, 242))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((270, 150, 690, 530), radius=84, fill=(51, 111, 176))
    draw.ellipse((395, 215, 565, 385), fill=(238, 173, 73))
    draw.rectangle((435, 385, 525, 490), fill=(238, 173, 73))
    return image


if __name__ == "__main__":
    main()
