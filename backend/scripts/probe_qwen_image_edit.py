"""执行一次受控的 Qwen Image 编辑真实探针。

默认不联网。必须传入 ``--execute`` 才会读取本地安全存储中的 Qwen Key，并使用程序生成的
测试图片调用一次 ``media_image_edit`` 路由。探针不读取客户文件、不输出 API Key，也不会把
带签名的临时结果 URL 写入本地记录。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings
from app.services.model_gateway import ModelGatewayError
from app.services.qwen_image_edit import QwenImageEditInput, download_qwen_image_result, edit_qwen_image


def _build_fixture() -> bytes:
    """构造无客户内容的测试图，便于验证“限定区域修改”而非仅验证 HTTP 200。"""

    image = Image.new("RGB", (1024, 768), color=(244, 247, 251))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((112, 112, 912, 656), radius=36, fill=(32, 96, 186))
    draw.ellipse((348, 224, 676, 552), fill=(228, 83, 76))
    draw.rectangle((148, 148, 324, 246), fill=(236, 194, 50))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _synthetic_region_change_metrics(source: bytes, result: bytes) -> dict[str, object]:
    """记录固定夹具的语义变化与圆外漂移，不能把提示词当作蒙版证据。"""

    with Image.open(BytesIO(source)) as source_image, Image.open(BytesIO(result)) as result_image:
        source_rgb = source_image.convert("RGB")
        result_rgb = result_image.convert("RGB")
    if source_rgb.size != result_rgb.size:
        return {"same_dimensions": False}

    center_x, center_y, protected_radius = 512, 388, 180
    source_pixels = source_rgb.load()
    result_pixels = result_rgb.load()
    changed_pixels = 0
    outside_pixels = 0
    total_absolute_difference = 0
    for y in range(source_rgb.height):
        for x in range(source_rgb.width):
            if (x - center_x) ** 2 + (y - center_y) ** 2 <= protected_radius ** 2:
                continue
            before = source_pixels[x, y]
            after = result_pixels[x, y]
            channel_differences = tuple(abs(after[index] - before[index]) for index in range(3))
            total_absolute_difference += sum(channel_differences)
            outside_pixels += 1
            if max(channel_differences) > 16:
                changed_pixels += 1
    return {
        "same_dimensions": True,
        "source_center_rgb": list(source_pixels[center_x, center_y]),
        "output_center_rgb": list(result_pixels[center_x, center_y]),
        "outside_circle_mean_abs_diff": round(total_absolute_difference / (outside_pixels * 3), 3),
        "outside_circle_changed_ratio_delta_gt_16": round(changed_pixels / outside_pixels, 6),
        "protected_circle_radius_px": protected_radius,
    }


async def _run_probe(output_dir: Path) -> dict[str, object]:
    source = _build_fixture()
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / "input.png"
    input_path.write_bytes(source)
    started = datetime.now(UTC)
    result = await edit_qwen_image(
        images=[QwenImageEditInput(image_bytes=source, mime_type="image/png")],
        prompt="仅将中央的红色圆形改为绿色圆形。保留蓝色圆角矩形、黄色小矩形、背景、位置、边缘和整体构图不变。",
        output_count=1,
        output_size="1024*768",
        prompt_extend=False,
        watermark=False,
    )
    downloaded_image = await download_qwen_image_result(result_url=result.output_urls[0])
    image_bytes = downloaded_image.image_bytes
    output_path = output_dir / "output.png"
    output_path.write_bytes(image_bytes)
    region_metrics = _synthetic_region_change_metrics(source, image_bytes)
    finished = datetime.now(UTC)
    return {
        "probe": "qwen_image_edit_synthetic_region_change_v1",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "provider": result.provider,
        "model": result.model,
        "request_id": result.request_id,
        "input_sha256": hashlib.sha256(source).hexdigest(),
        "output_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "output_bytes": len(image_bytes),
        "output_format": downloaded_image.image_format,
        "output_width": downloaded_image.width,
        "output_height": downloaded_image.height,
        "provider_image_count": result.image_count,
        "provider_width": result.width,
        "provider_height": result.height,
        "provider_usage": {
            "reported": result.usage_reported,
            "input_image_count": result.input_image_count,
            "output_image_count": result.output_image_count,
            "input_image_type": result.input_image_type,
            "output_image_type": result.output_image_type,
            "output_width": result.width,
            "output_height": result.height,
        },
        "billing_state": (
            "provider usage is recorded with request_id; invoice amount remains unknown "
            "until delayed provider-side billing/monitoring reconciliation"
        ),
        "synthetic_region_change": region_metrics,
        # 临时 URL 常携带签名，不写入证据包；仅保留域名和不可逆摘要协助排查。
        "result_url_host": urlparse(result.output_urls[0]).netloc,
        "result_url_sha256": hashlib.sha256(result.output_urls[0].encode("utf-8")).hexdigest(),
        "input_file": input_path.name,
        "output_file": output_path.name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen Image 图片编辑最小真实探针")
    parser.add_argument("--execute", action="store_true", help="明确允许一次真实图片编辑请求")
    parser.add_argument("--output-dir", type=Path, help="证据输出目录；默认写入后端数据目录")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to submit one synthetic Qwen Image edit request.")
        return

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or (settings.data_dir / "media_evaluations" / f"qwen_image_probe_{timestamp}")
    try:
        manifest = asyncio.run(_run_probe(output_dir.resolve()))
    except (ModelGatewayError, RuntimeError) as exc:
        print(f"Qwen Image probe failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "output_dir": str(output_dir), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
