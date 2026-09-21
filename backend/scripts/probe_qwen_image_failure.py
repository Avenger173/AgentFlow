"""验证 Qwen Image 对明确无效模型的真实失败处理。

默认不联网。使用 ``--execute`` 后，只发送程序生成的小型图片和一个显式不存在的模型 ID；
预期 Provider 返回 4xx，且根据官方按成功图像计费规则不产生图片生成费用。不会修改用户模型配置。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.model_gateway import ModelGatewayError, resolve_visual_model_runtime_for_route  # noqa: E402
from app.services.qwen_image_edit import (  # noqa: E402
    QwenImageEditInput,
    QwenImageEditProviderError,
    edit_qwen_image,
)


_INVALID_MODEL = "qwen-image-agentflow-invalid-model-probe"


def _synthetic_input() -> bytes:
    image = Image.new("RGB", (512, 512), color=(90, 130, 170))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _run() -> dict[str, object]:
    resolved = resolve_visual_model_runtime_for_route("media_image_edit", validate=True)
    runtime = replace(resolved.runtime, model=_INVALID_MODEL)
    try:
        await edit_qwen_image(
            images=[QwenImageEditInput(image_bytes=_synthetic_input(), mime_type="image/png")],
            prompt="测试无效模型失败处理。",
            output_count=1,
            output_size="512*512",
            prompt_extend=False,
            watermark=False,
            runtime=runtime,
        )
    except QwenImageEditProviderError as exc:
        if not 400 <= exc.status_code < 500:
            raise RuntimeError(f"无效模型预期返回 4xx，实际为 HTTP {exc.status_code}。") from exc
        return {
            "passed": True,
            "provider": runtime.provider,
            "submitted_model": _INVALID_MODEL,
            "http_status": exc.status_code,
            "error_code": exc.error_code,
            "message": str(exc),
            "cost_expectation": "provider rejected before successful image generation",
        }
    except ModelGatewayError as exc:
        raise RuntimeError("无效模型没有得到可分类的 Provider 4xx。") from exc
    raise RuntimeError("无效模型意外返回成功，失败探针不能通过。")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="明确提交一次无效 Qwen Image 模型失败探针")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to submit one invalid-model Qwen Image request.")
        return
    output_dir = PROJECT_ROOT / "data" / "media_evaluations" / (
        "qwen_image_failure_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        manifest = asyncio.run(_run())
    except RuntimeError as exc:
        manifest = {"passed": False, "error": " ".join(str(exc).split())[:240]}
        (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output_dir": str(output_dir), **manifest}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
