"""离线验证 Qwen Image 编辑 Provider 的请求/响应契约。

脚本使用 httpx MockTransport，不读取本机模型配置、不连接百炼，也不消耗用户额度。
真实模型探针由后续 MM-0 专用脚本在用户明确执行时完成。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.model_gateway import VisualModelRuntime
from app.services.model_gateway import ModelGatewayError
from app.services.qwen_image_edit import (
    QwenImageEditInput,
    QwenImageEditOutcomeUnknownError,
    QwenImageEditProviderError,
    QwenImageEditRateLimitError,
    download_qwen_image_result,
    edit_qwen_image,
)


def _fixture_png() -> bytes:
    image = Image.new("RGB", (640, 512), color=(230, 235, 245))
    image.paste((35, 99, 188), (128, 128, 512, 384))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _verify_request_and_response() -> None:
    fixture = _fixture_png()
    result_url = "https://dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com/output.png"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert str(request.url) == "https://dashscope.example.test/api/v1/services/aigc/multimodal-generation/generation"
            assert request.headers["authorization"] == "Bearer fixture-qwen-key"
            payload = json.loads(request.content)
            assert payload["model"] == "qwen-image-2.0-pro"
            content = payload["input"]["messages"][0]["content"]
            assert content[0]["image"].startswith("data:image/png;base64,")
            assert base64.b64decode(content[0]["image"].split(",", 1)[1]) == fixture
            assert content[1]["text"] == "把蓝色矩形改成绿色，其他区域保持不变。"
            assert payload["parameters"] == {
                "n": 1,
                "watermark": False,
                "prompt_extend": True,
                "size": "1024*1024",
            }
            return httpx.Response(
                200,
                json={
                    "request_id": "fixture-request-id",
                    "usage": {"image_count": 1, "width": 1024, "height": 1024},
                    "output": {
                        "choices": [{"message": {"content": [{"image": result_url}]}}]
                    },
                },
            )
        assert request.method == "GET"
        assert str(request.url) == result_url
        assert request.headers["accept"] == "image/png,image/*;q=0.8"
        return httpx.Response(200, content=fixture, headers={"content-type": "image/png"})

    runtime = VisualModelRuntime(
        provider="qwen_image",
        label="Qwen Image / DashScope",
        transport="dashscope_multimodal",
        base_url="https://dashscope.example.test/api/v1",
        model="qwen-image-2.0-pro",
        api_key="fixture-qwen-key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await edit_qwen_image(
            images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
            prompt="把蓝色矩形改成绿色，其他区域保持不变。",
            output_size="1024*1024",
            runtime=runtime,
            client=client,
        )
    assert result.provider == "qwen_image"
    assert result.request_id == "fixture-request-id"
    assert result.output_urls == (result_url,)
    assert (result.image_count, result.width, result.height) == (1, 1024, 1024)
    assert result.usage_reported is True
    assert result.output_image_count == 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloaded = await download_qwen_image_result(result_url=result_url, client=client)
    assert downloaded.image_bytes == fixture
    assert (downloaded.mime_type, downloaded.image_format, downloaded.width, downloaded.height) == (
        "image/png",
        "PNG",
        640,
        512,
    )
    try:
        await download_qwen_image_result(result_url="https://example.com/not-qwen.png")
    except ModelGatewayError as exc:
        assert "可信" in str(exc)
    else:  # pragma: no cover - 防止不可信 URL 静默进入下载器。
        raise AssertionError("untrusted result host must be rejected")


async def _verify_qwen3_usage_and_request_header() -> None:
    fixture = _fixture_png()
    result_url = "https://dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com/qwen3-output.png"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "fixture-qwen3-header-request-id"},
            json={
                "usage": {
                    "input_image_count": 1,
                    "input_image_type": "qima_input_1k",
                    "output_image_count": 1,
                    "output_image_type": "qima_output_1k",
                    "output_width": 1024,
                    "output_height": 1024,
                },
                "output": {"choices": [{"message": {"content": [{"image": result_url}]}}]},
            },
        )

    runtime = VisualModelRuntime(
        provider="qwen_image",
        label="Qwen Image / DashScope",
        transport="dashscope_multimodal",
        base_url="https://dashscope.example.test/api/v1",
        model="qwen-image-3.0-pro",
        api_key="fixture-qwen-key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await edit_qwen_image(
            images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
            prompt="Verify Qwen 3.0 usage recording.",
            runtime=runtime,
            client=client,
        )
    assert result.request_id == "fixture-qwen3-header-request-id"
    assert result.usage_reported is True
    assert result.input_image_count == 1
    assert result.input_image_type == "qima_input_1k"
    assert result.output_image_count == 1
    assert result.output_image_type == "qima_output_1k"
    assert (result.image_count, result.width, result.height) == (1, 1024, 1024)


async def _verify_qwen3_size_contract() -> None:
    fixture = _fixture_png()
    result_url = "https://dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com/qwen3-size.png"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["parameters"]["size"] == "384*1024"
        return httpx.Response(
            200,
            json={
                "output": {"choices": [{"message": {"content": [{"image": result_url}]}}]},
            },
        )

    runtime = VisualModelRuntime(
        provider="qwen_image",
        label="Qwen Image / DashScope",
        transport="dashscope_multimodal",
        base_url="https://dashscope.example.test/api/v1",
        model="qwen-image-3.0-pro",
        api_key="fixture-qwen-key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await edit_qwen_image(
            images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
            prompt="验证 Qwen 3.0 的竖图输出尺寸。",
            output_size="384*1024",
            runtime=runtime,
            client=client,
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        try:
            await edit_qwen_image(
                images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
                prompt="验证像素面积下限。",
                output_size="384*512",
                runtime=runtime,
                client=client,
            )
        except ModelGatewayError as exc:
            assert "总像素" in str(exc)
        else:  # pragma: no cover - 防止无效面积仍然发往 Provider。
            raise AssertionError("Qwen 3.0 output below the documented pixel-area floor must be rejected")


async def _verify_submission_failure_contracts() -> None:
    fixture = _fixture_png()
    runtime = VisualModelRuntime(
        provider="qwen_image",
        label="Qwen Image / DashScope",
        transport="dashscope_multimodal",
        base_url="https://dashscope.example.test/api/v1",
        model="qwen-image-3.0-pro",
        api_key="fixture-qwen-key",
    )

    def rate_limited(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"code": "Throttling.RateQuota", "message": "retry later"},
            headers={"Retry-After": "35"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(rate_limited)) as client:
        try:
            await edit_qwen_image(
                images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
                prompt="测试限流分类。",
                runtime=runtime,
                client=client,
            )
        except QwenImageEditRateLimitError as exc:
            assert exc.status_code == 429
            assert exc.outcome == "rejected"
            assert exc.error_code == "Throttling.RateQuota"
            assert exc.retry_after_seconds == 35.0
        else:  # pragma: no cover - 防止 429 被误当作未知或成功。
            raise AssertionError("429 must preserve a retryable rejected outcome")

    def invalid_request(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "InvalidParameter", "message": "invalid model"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_request)) as client:
        try:
            await edit_qwen_image(
                images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
                prompt="测试明确拒绝分类。",
                runtime=runtime,
                client=client,
            )
        except QwenImageEditProviderError as exc:
            assert type(exc) is QwenImageEditProviderError
            assert exc.status_code == 400
            assert exc.outcome == "rejected"
        else:  # pragma: no cover - 防止 4xx 被误判成可收费的未知提交。
            raise AssertionError("4xx must preserve a rejected submission outcome")

    def server_error(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": "ServiceUnavailable", "message": "temporary outage"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server_error)) as client:
        try:
            await edit_qwen_image(
                images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
                prompt="测试服务端未知结果分类。",
                runtime=runtime,
                client=client,
            )
        except QwenImageEditOutcomeUnknownError as exc:
            assert exc.reason == "provider_server_error"
            assert exc.status_code == 503
            assert exc.outcome == "unknown"
            assert exc.safe_to_retry_automatically is False
        else:  # pragma: no cover - 防止 5xx 被静默重试或伪装为明确失败。
            raise AssertionError("5xx must preserve an unknown submission outcome")

    class TimeoutClient:
        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            raise httpx.ReadTimeout("fixture timeout")

    try:
        await edit_qwen_image(
            images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
            prompt="测试超时未知结果分类。",
            runtime=runtime,
            client=TimeoutClient(),  # type: ignore[arg-type]
        )
    except QwenImageEditOutcomeUnknownError as exc:
        assert exc.reason == "request_timeout"
        assert exc.status_code is None
        assert exc.safe_to_retry_automatically is False
    else:  # pragma: no cover - 防止请求超时被误判成可安全重试。
        raise AssertionError("timeout must preserve an unknown submission outcome")

    class ConnectionErrorClient:
        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("fixture connection lost")

    try:
        await edit_qwen_image(
            images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
            prompt="测试连接中断未知结果分类。",
            runtime=runtime,
            client=ConnectionErrorClient(),  # type: ignore[arg-type]
        )
    except QwenImageEditOutcomeUnknownError as exc:
        assert exc.reason == "request_connection"
        assert exc.status_code is None
        assert exc.safe_to_retry_automatically is False
    else:  # pragma: no cover - 防止连接中断被误判成安全重试。
        raise AssertionError("connection interruption must preserve an unknown submission outcome")

    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def waiting_handler(_: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_request.wait()
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(waiting_handler)) as client:
        task = asyncio.create_task(
            edit_qwen_image(
                images=[QwenImageEditInput(image_bytes=fixture, mime_type="image/png")],
                prompt="测试取消传播。",
                runtime=runtime,
                client=client,
            )
        )
        await request_started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - 防止取消被适配器变成自动重试或普通错误。
            raise AssertionError("caller cancellation must propagate unchanged")


def main() -> None:
    asyncio.run(_verify_request_and_response())
    asyncio.run(_verify_qwen3_usage_and_request_header())
    asyncio.run(_verify_qwen3_size_contract())
    asyncio.run(_verify_submission_failure_contracts())
    print("Qwen Image edit provider verification passed.")


if __name__ == "__main__":
    main()
