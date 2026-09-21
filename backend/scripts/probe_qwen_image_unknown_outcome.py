"""Capture one controlled real Qwen Image timeout or caller-cancellation outcome for MM-0.

The probe submits only an in-memory synthetic image. A deliberately short client timeout
can occur after the provider has accepted the request, so the only passing result is the
adapter's explicit ``unknown`` outcome. It never retries, downloads no result URL, and
does not store the input image.
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
from time import perf_counter

import httpx
from PIL import Image, ImageDraw


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.model_gateway import ModelGatewayError, resolve_visual_model_runtime_for_route  # noqa: E402
from app.services.qwen_image_edit import (  # noqa: E402
    QwenImageEditInput,
    QwenImageEditOutcomeUnknownError,
    edit_qwen_image,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1.0,
        help="超时模式的客户端等待、取消模式的取消延迟；必须为 0.1 到 5 秒",
    )
    parser.add_argument(
        "--mode",
        choices=("timeout", "cancel"),
        default="timeout",
        help="timeout 捕获未知结果；cancel 验证调用方取消在途等待",
    )
    parser.add_argument("--execute", action="store_true", help="明确提交一次合成图 Qwen Image 探针")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to submit one synthetic Qwen Image outcome probe.")
        return
    if not 0.1 <= args.timeout_seconds <= 5.0:
        raise SystemExit("timeout-seconds must be between 0.1 and 5")

    output_dir = PROJECT_ROOT / "data" / "media_evaluations" / (
        "qwen_image_unknown_outcome_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        manifest = asyncio.run(_run(timeout_seconds=args.timeout_seconds, mode=args.mode))
    except RuntimeError as exc:
        manifest = {"passed": False, "error": _safe_error(exc)}
        _write_manifest(output_dir, manifest)
        print(json.dumps({"output_dir": str(output_dir), **manifest}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc
    _write_manifest(output_dir, manifest)
    print(json.dumps({"output_dir": str(output_dir), **manifest}, ensure_ascii=False))
    if manifest.get("passed") is not True:
        raise SystemExit(1)


async def _run(*, timeout_seconds: float, mode: str) -> dict[str, object]:
    resolved = resolve_visual_model_runtime_for_route("media_image_edit", validate=True)
    runtime = resolved.runtime
    if runtime.model != "qwen-image-3.0-pro":
        # 本探针固定当前经公开夹具筛出的候选，避免用户路由切换后在别的模型上留下混淆证据。
        runtime = replace(runtime, model="qwen-image-3.0-pro")
    image_bytes = _synthetic_input()
    started = perf_counter()
    timeout = httpx.Timeout(timeout_seconds if mode == "timeout" else 30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        request = edit_qwen_image(
            images=[QwenImageEditInput(image_bytes=image_bytes, mime_type="image/png")],
            prompt="将蓝色方块改成绿色，其他区域保持不变。",
            output_count=1,
            output_size="512*512",
            prompt_extend=False,
            watermark=False,
            seed=2026092001,
            runtime=runtime,
            client=client,
        )
        if mode == "cancel":
            return await _cancel_in_flight_request(
                request=request,
                runtime=runtime,
                cancel_after_seconds=timeout_seconds,
                started=started,
            )
        try:
            await request
        except QwenImageEditOutcomeUnknownError as exc:
            elapsed_ms = round((perf_counter() - started) * 1000, 3)
            if exc.reason != "request_timeout":
                raise RuntimeError(f"预期 request_timeout，实际为 {exc.reason}") from exc
            return {
                "passed": True,
                "provider": runtime.provider,
                "model": runtime.model,
                "request_count": 1,
                "client_timeout_seconds": timeout_seconds,
                "elapsed_ms": elapsed_ms,
                "outcome": exc.outcome,
                "reason": exc.reason,
                "safe_to_retry_automatically": exc.safe_to_retry_automatically,
                "input": "synthetic_in_memory_512_png_not_persisted",
                "result_download_attempted": False,
                "cost_state": "unknown; provider may have accepted the request after client timeout",
            }
        except ModelGatewayError as exc:
            raise RuntimeError(f"探针没有进入未知结果路径：{_safe_error(exc)}") from exc
    raise RuntimeError("Provider 在短超时内返回成功；本次没有验证未知结果路径，且不会重发。")


async def _cancel_in_flight_request(
    *,
    request: object,
    runtime: object,
    cancel_after_seconds: float,
    started: float,
) -> dict[str, object]:
    if not isinstance(request, asyncio.Future) and not asyncio.iscoroutine(request):
        raise RuntimeError("取消探针未创建可等待的图片编辑请求。")
    task = asyncio.ensure_future(request)
    await asyncio.sleep(cancel_after_seconds)
    if task.done():
        try:
            await task
        except ModelGatewayError as exc:
            raise RuntimeError(f"取消前 Provider 已返回明确错误：{_safe_error(exc)}") from exc
        raise RuntimeError("取消前 Provider 已返回成功；本次不会重发。")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        elapsed_ms = round((perf_counter() - started) * 1000, 3)
        provider = getattr(runtime, "provider", "")
        model = getattr(runtime, "model", "")
        return {
            "passed": True,
            "mode": "cancel",
            "provider": provider,
            "model": model,
            "request_count": 1,
            "cancel_after_seconds": cancel_after_seconds,
            "elapsed_ms": elapsed_ms,
            "caller_status": "cancelled",
            "remote_outcome": "unknown; request may have reached provider before local cancellation",
            "safe_to_retry_automatically": False,
            "input": "synthetic_in_memory_512_png_not_persisted",
            "result_download_attempted": False,
            "cost_state": "unknown; provider may have accepted the request before local cancellation",
        }
    raise RuntimeError("取消请求没有传播 asyncio.CancelledError。")


def _synthetic_input() -> bytes:
    image = Image.new("RGB", (512, 512), color=(236, 240, 246))
    draw = ImageDraw.Draw(image)
    draw.rectangle((128, 128, 384, 384), fill=(45, 101, 191))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_manifest(output_dir: Path, manifest: dict[str, object]) -> None:
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
