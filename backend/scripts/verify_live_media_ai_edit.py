"""执行一次受控的 AI 修图端到端真实模型探针。

默认不联网。传入 ``--live`` 后，脚本只用程序生成的 PNG 提交一次当前
``media_image_edit`` 路由，并验证模型结果已经作为可回读的新 revision 写入
受控图片项目。不会读取客户素材、输出 API Key、Provider 原始正文或临时签名 URL。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from PIL import Image, ImageDraw


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings
from app.schemas.media_workspace import MediaImageAiEditRequest
from app.services.media_ai_edit_delivery import create_media_ai_edit_queued_run, run_media_ai_edit_task
from app.services.media_workspace import (
    create_media_project,
    import_media_image_base64,
    resolve_media_revision_preview_path,
)


def _fixture_png() -> bytes:
    """创建无客户内容的固定几何图，验证指令、尺寸与 revision 提交边界。"""

    image = Image.new("RGB", (1024, 768), color=(244, 247, 251))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((112, 112, 912, 656), radius=36, fill=(32, 96, 186))
    draw.ellipse((348, 224, 676, 552), fill=(228, 83, 76))
    draw.rectangle((148, 148, 324, 246), fill=(236, 194, 50))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _readback_png(path: Path) -> tuple[int, int, str]:
    with Image.open(path) as image:
        image.load()
        return image.width, image.height, image.format or ""


async def _run_live_probe() -> dict[str, object]:
    source = _fixture_png()
    project = create_media_project(title="AI 修图真实探针")
    asset = import_media_image_base64(
        project_id=project.project_id,
        filename="synthetic-live-fixture.png",
        content_base64=base64.b64encode(source).decode("ascii"),
    )
    request = MediaImageAiEditRequest(
        base_revision_id=asset.current_revision_id,
        instruction=(
            "仅将中央的红色圆形改为绿色圆形。保留蓝色圆角矩形、黄色小矩形、背景、位置、边缘和整体构图不变。"
        ),
    )
    task_id = f"task_media_ai_edit_{uuid4().hex[:12]}"
    create_media_ai_edit_queued_run(
        task_id=task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=request,
    )
    started = perf_counter()
    result = await run_media_ai_edit_task(
        task_id=task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=request,
    )
    elapsed_ms = round((perf_counter() - started) * 1000, 3)
    summary: dict[str, object] = {
        "probe": "live_media_ai_edit_revision_v1",
        "route": "media_image_edit",
        "live_call_count": 1,
        "fixture": "programmatic_geometric_png_1024x768",
        "task_status": result.status,
        "failure_reason": result.failure_reason,
        "message": result.message,
        "elapsed_ms": elapsed_ms,
        "billing_amount": "unknown",
    }
    if result.status != "completed" or result.revision is None:
        return summary

    revision = result.revision
    preview_path = resolve_media_revision_preview_path(
        project_id=project.project_id,
        revision_id=revision.revision_id,
    )
    output_width, output_height, output_format = _readback_png(preview_path)
    parameters = revision.parameters
    provider = parameters.get("provider")
    model = parameters.get("model")
    request_id = str(parameters.get("request_id", ""))
    summary.update(
        {
            "revision_created": True,
            "revision_operation": revision.operation,
            "same_dimensions": (output_width, output_height) == (1024, 768),
            "png_readback": output_format.upper() == "PNG",
            "output_width": output_width,
            "output_height": output_height,
            "provider": provider,
            "model": model,
            "provider_usage": parameters.get("usage"),
            "request_id_recorded": bool(request_id),
            "request_id_sha256": hashlib.sha256(request_id.encode("utf-8")).hexdigest() if request_id else None,
        }
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 修图端到端真实模型探针")
    parser.add_argument("--live", action="store_true", help="明确允许提交一次程序生成夹具的真实请求")
    parser.add_argument("--output-dir", type=Path, help="脱敏运行摘要目录；默认写入受控 data 目录")
    args = parser.parse_args()
    if not args.live:
        print("Dry run only. Pass --live to submit one synthetic media_image_edit request.")
        return

    started_at = datetime.now(UTC)
    summary = asyncio.run(_run_live_probe())
    summary["started_at"] = started_at.isoformat(timespec="seconds")
    summary["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    output_dir = args.output_dir or (
        settings.data_dir / "media_evaluations" / f"live_media_ai_edit_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), **summary}, ensure_ascii=False))
    if summary["task_status"] != "completed" or not summary.get("png_readback") or not summary.get("same_dimensions"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
