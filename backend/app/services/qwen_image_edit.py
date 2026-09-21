"""DashScope Qwen Image 的受控图片编辑适配器。

该模块只解决 ``原图 + 指令 -> Provider 结果`` 的 MM-0 可行性验证，不创建媒体工程、
不落库客户图片、也不把供应商返回 URL 伪装成正式 Artifact。后续修图工作区接入时，
必须将结果下载、回读、原子提交到受控资产区后才能交付给用户。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Literal, Sequence
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from app.core.config import settings
from app.services.model_gateway import (
    ModelGatewayConnectionError,
    ModelGatewayError,
    ModelGatewayTimeoutError,
    VisualModelRuntime,
    resolve_visual_model_runtime_for_route,
)


_MULTIMODAL_GENERATION_PATH = "/services/aigc/multimodal-generation/generation"
_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/bmp", "image/tiff", "image/webp", "image/gif"}
_MAX_INPUT_IMAGES = 3
_MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_OUTPUT_IMAGE_BYTES = 30 * 1024 * 1024
_MAX_OUTPUT_IMAGE_PIXELS = 40_000_000
_MAX_PROMPT_CHARS = 800
_MAX_NEGATIVE_PROMPT_CHARS = 500
_SAFE_SIZE_RE = re.compile(r"^(?:[5-9]\d{2}|1\d{3}|20(?:0[0-4][0-8]|[0-3]\d{2}))\*(?:[5-9]\d{2}|1\d{3}|20(?:0[0-4][0-8]|[0-3]\d{2}))$")
_SECRET_PATTERN = re.compile(r"<?\b(?:sk|ak)-[A-Za-z0-9_-]{12,}\b>?", re.IGNORECASE)
_DASHSCOPE_RESULT_HOST_RE = re.compile(
    r"^dashscope(?:-[a-z0-9]+)*\.oss(?:-[a-z0-9]+)*\.aliyuncs\.com$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QwenImageEditInput:
    """一张已在调用边界内验证的图片，绝不以客户端路径作为模型输入。"""

    image_bytes: bytes
    mime_type: str


@dataclass(frozen=True)
class QwenImageEditResult:
    """一次 Provider 调用的最小脱敏结果。

    ``output_urls`` 是短期 Provider 结果，不是可交付文件；调用方需要自行下载并回读验证。
    """

    provider: str
    model: str
    request_id: str
    output_urls: tuple[str, ...]
    image_count: int | None = None
    width: int | None = None
    height: int | None = None
    input_image_count: int | None = None
    output_image_count: int | None = None
    input_image_type: str | None = None
    output_image_type: str | None = None
    usage_reported: bool = False


@dataclass(frozen=True)
class QwenDownloadedImage:
    """已在内存中回读的 Provider 结果，尚未成为工程 artifact。"""

    image_bytes: bytes
    mime_type: str
    image_format: str
    width: int
    height: int


class QwenImageEditProviderError(ModelGatewayError):
    """保留安全的 HTTP/错误码事实，供上层判定是否可以显式重试。"""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str = "",
        message: str = "",
        outcome: Literal["rejected", "unknown"] = "rejected",
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code[:80]
        self.outcome = outcome
        detail = " · ".join(part for part in (self.error_code, message[:180]) if part)
        suffix = f"（{detail}）" if detail else ""
        super().__init__(f"Qwen Image 编辑接口返回 HTTP {status_code}{suffix}。")


class QwenImageEditRateLimitError(QwenImageEditProviderError):
    """Provider 已明确拒绝的限流，调用方可在预算内等待后重试。"""

    def __init__(self, *, error_code: str = "", message: str = "", retry_after_seconds: float | None = None) -> None:
        super().__init__(
            status_code=429,
            error_code=error_code,
            message=message,
            outcome="rejected",
        )
        self.retry_after_seconds = retry_after_seconds


class QwenImageEditOutcomeUnknownError(ModelGatewayError):
    """提交可能已到达 Provider，却没有获得可回读结果。

    同步图片接口没有可供本项目查询的提交 ID。超时、连接在写入后中断及 5xx 都不能安全地
    自动重发，否则一次用户操作可能重复计费或生成多份不可审计结果。
    """

    def __init__(
        self,
        *,
        reason: Literal["request_timeout", "request_connection", "provider_server_error"],
        status_code: int | None = None,
        error_code: str = "",
        message: str = "",
    ) -> None:
        self.reason = reason
        self.status_code = status_code
        self.error_code = error_code[:80]
        self.outcome = "unknown"
        self.safe_to_retry_automatically = False
        detail = " · ".join(part for part in (self.error_code, message[:180]) if part)
        suffix = f"（{detail}）" if detail else ""
        status = f" HTTP {status_code}" if status_code is not None else ""
        super().__init__(f"Qwen Image 编辑提交结果未知{status}{suffix}；不会自动重试。")


async def edit_qwen_image(
    *,
    images: Sequence[QwenImageEditInput],
    prompt: str,
    output_count: int = 1,
    output_size: str | None = None,
    negative_prompt: str | None = None,
    prompt_extend: bool = True,
    watermark: bool = False,
    seed: int | None = None,
    runtime: VisualModelRuntime | None = None,
    client: httpx.AsyncClient | None = None,
) -> QwenImageEditResult:
    """提交一次 Qwen Image 编辑请求。

    这不是通用图片下载器：输入始终由调用方在内存中提供，且输出仅限官方返回的 HTTPS URL。
    当没有传入 runtime 时，固定解析 ``media_image_edit`` 路由，因此不会误用 PPT 的 Seedream
    文生图配置或全局文本模型。
    """

    active_runtime = runtime or resolve_visual_model_runtime_for_route("media_image_edit", validate=True).runtime
    if not isinstance(active_runtime, VisualModelRuntime):
        raise ModelGatewayError("图片编辑路由未解析到图像模型运行时。")
    if active_runtime.provider != "qwen_image" or active_runtime.transport != "dashscope_multimodal":
        raise ModelGatewayError("当前图片编辑路由不是已接入的 Qwen Image Provider。")

    prepared_images = _prepare_images(images)
    clean_prompt = _normalize_prompt(prompt, field_name="编辑指令", maximum=_MAX_PROMPT_CHARS, required=True)
    clean_negative_prompt = _normalize_prompt(
        negative_prompt or "",
        field_name="反向提示词",
        maximum=_MAX_NEGATIVE_PROMPT_CHARS,
        required=False,
    )
    count = int(output_count)
    if count < 1 or count > 6:
        raise ModelGatewayError("Qwen Image 输出数量必须在 1 到 6 之间。")
    size = _normalize_size(output_size)
    if seed is not None and not 0 <= int(seed) <= 2_147_483_647:
        raise ModelGatewayError("Qwen Image seed 必须在 0 到 2147483647 之间。")

    content: list[dict[str, str]] = [
        {"image": _as_data_url(item)}
        for item in prepared_images
    ]
    content.append({"text": clean_prompt})
    parameters: dict[str, object] = {
        "n": count,
        "watermark": bool(watermark),
        "prompt_extend": bool(prompt_extend),
    }
    if clean_negative_prompt:
        parameters["negative_prompt"] = clean_negative_prompt
    if size:
        parameters["size"] = size
    if seed is not None:
        parameters["seed"] = int(seed)
    payload: dict[str, object] = {
        "model": active_runtime.model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": parameters,
    }
    url = f"{active_runtime.base_url.rstrip('/')}{_MULTIMODAL_GENERATION_PATH}"
    headers = {
        "Authorization": f"Bearer {active_runtime.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(max(30.0, min(float(settings.llm_timeout_seconds), 180.0)), connect=10.0)
    )
    try:
        response = await active_client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise QwenImageEditOutcomeUnknownError(
            reason="request_timeout",
            message="等待 Provider 响应超时",
        ) from exc
    except httpx.RequestError as exc:
        raise QwenImageEditOutcomeUnknownError(
            reason="request_connection",
            message="提交连接中断",
        ) from exc
    finally:
        if owns_client:
            await active_client.aclose()

    if response.status_code >= 400:
        error_code, error_message = _provider_error(response)
        if response.status_code == 429:
            raise QwenImageEditRateLimitError(
                error_code=error_code,
                message=error_message,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code >= 500 or response.status_code == 408:
            raise QwenImageEditOutcomeUnknownError(
                reason="provider_server_error",
                status_code=response.status_code,
                error_code=error_code,
                message=error_message,
            )
        raise QwenImageEditProviderError(
            status_code=response.status_code,
            error_code=error_code,
            message=error_message,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ModelGatewayError("Qwen Image 编辑接口没有返回合法 JSON。") from exc
    if not isinstance(body, dict):
        raise ModelGatewayError("Qwen Image 编辑接口响应顶层不是 JSON object。")
    return _parse_result(body, runtime=active_runtime, response_headers=response.headers)


async def download_qwen_image_result(
    *,
    result_url: str,
    client: httpx.AsyncClient | None = None,
) -> QwenDownloadedImage:
    """及时下载并回读 Qwen Image 的短期结果 URL。

    只接收 DashScope 官方结果域名，禁止调用方把任意远程 URL 变成后端下载请求。结果始终
    停留在内存中；媒体工程后续需要经过自己的 hash、文件回读和原子提交才能持久化。
    """

    _validate_result_url(result_url)
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(max(30.0, min(float(settings.llm_timeout_seconds), 180.0)), connect=10.0),
        follow_redirects=False,
    )
    try:
        async with active_client.stream(
            "GET",
            result_url,
            headers={"Accept": "image/png,image/*;q=0.8"},
        ) as response:
            if response.status_code >= 400:
                raise QwenImageEditProviderError(status_code=response.status_code, message="结果图片下载失败")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if not content_type.startswith("image/"):
                raise ModelGatewayError("Qwen Image 结果下载返回的不是图片内容。")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > _MAX_OUTPUT_IMAGE_BYTES:
                    raise ModelGatewayError("Qwen Image 结果图片超过 30 MB 安全上限。")
                chunks.append(chunk)
    except httpx.TimeoutException as exc:
        raise ModelGatewayTimeoutError("Qwen Image 结果图片下载超时。") from exc
    except httpx.RequestError as exc:
        raise ModelGatewayConnectionError("Qwen Image 结果图片当前无法下载。") from exc
    finally:
        if owns_client:
            await active_client.aclose()
    return _decode_downloaded_image(b"".join(chunks))


def _prepare_images(images: Sequence[QwenImageEditInput]) -> tuple[QwenImageEditInput, ...]:
    values = tuple(images)
    if not values:
        raise ModelGatewayError("Qwen Image 编辑至少需要一张源图。")
    if len(values) > _MAX_INPUT_IMAGES:
        raise ModelGatewayError(f"Qwen Image 编辑一次最多接收 {_MAX_INPUT_IMAGES} 张源图。")
    for item in values:
        mime_type = item.mime_type.strip().lower()
        if mime_type not in _ALLOWED_MIME_TYPES:
            raise ModelGatewayError("源图格式仅支持 JPEG、PNG、BMP、TIFF、WEBP 或 GIF。")
        if not item.image_bytes or len(item.image_bytes) > _MAX_INPUT_IMAGE_BYTES:
            raise ModelGatewayError("单张源图必须大于 0 且不超过 20 MB。")
        try:
            with Image.open(BytesIO(item.image_bytes)) as image:
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ModelGatewayError("源图内容无法被安全识别为有效图片。") from exc
    return values


def _normalize_prompt(value: str, *, field_name: str, maximum: int, required: bool) -> str:
    normalized = " ".join(str(value).split()).strip()
    if required and not normalized:
        raise ModelGatewayError(f"{field_name}不能为空。")
    if len(normalized) > maximum:
        raise ModelGatewayError(f"{field_name}不能超过 {maximum} 个字符。")
    return normalized


def _normalize_size(value: str | None) -> str:
    size = (value or "").strip().lower().replace("x", "*")
    if not size:
        return ""
    if not _SAFE_SIZE_RE.fullmatch(size):
        raise ModelGatewayError("输出尺寸必须是 512 到 2048 范围内的“宽*高”格式。")
    return size


def _as_data_url(item: QwenImageEditInput) -> str:
    encoded = base64.b64encode(item.image_bytes).decode("ascii")
    return f"data:{item.mime_type.strip().lower()};base64,{encoded}"


def _parse_result(
    body: dict[str, object],
    *,
    runtime: VisualModelRuntime,
    response_headers: httpx.Headers | None = None,
) -> QwenImageEditResult:
    output = body.get("output")
    choices = output.get("choices") if isinstance(output, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelGatewayError("Qwen Image 编辑响应没有返回 choices。")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        raise ModelGatewayError("Qwen Image 编辑响应没有返回图片内容。")
    urls: list[str] = []
    for item in content:
        value = item.get("image") if isinstance(item, dict) else None
        url = str(value or "").strip()
        try:
            _validate_result_url(url)
        except ModelGatewayError:
            continue
        urls.append(url)
    if not urls:
        raise ModelGatewayError("Qwen Image 编辑响应没有返回可用 HTTPS 图片地址。")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    body_request_id = _optional_text(body.get("request_id") or body.get("requestId"), maximum=160)
    header_request_id = _optional_text(
        response_headers.get("x-request-id") if response_headers is not None else None,
        maximum=160,
    )
    return QwenImageEditResult(
        provider=runtime.provider,
        model=runtime.model,
        request_id=body_request_id or header_request_id,
        output_urls=tuple(urls[:6]),
        image_count=_first_optional_int(usage.get("image_count"), usage.get("output_image_count")),
        width=_first_optional_int(usage.get("width"), usage.get("output_width")),
        height=_first_optional_int(usage.get("height"), usage.get("output_height")),
        input_image_count=_optional_int(usage.get("input_image_count")),
        output_image_count=_first_optional_int(usage.get("output_image_count"), usage.get("image_count")),
        input_image_type=_optional_text(usage.get("input_image_type"), maximum=80) or None,
        output_image_type=_optional_text(usage.get("output_image_type"), maximum=80) or None,
        usage_reported=bool(usage),
    )


def _provider_error(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    nested = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(body.get("code") or nested.get("code") or nested.get("type") or "").strip()
    message = str(body.get("message") or nested.get("message") or "").strip()
    message = _SECRET_PATTERN.sub("[REDACTED]", re.sub(r"\s+", " ", message))[:180]
    return code[:80], message


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """只解析简单的秒数 Retry-After；日期格式不猜测本地时钟。"""

    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0.0 <= seconds <= 3600.0 else None


def _validate_result_url(value: str) -> None:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or not _DASHSCOPE_RESULT_HOST_RE.fullmatch(hostname)
    ):
        raise ModelGatewayError("Qwen Image 返回的不是可信的官方 HTTPS 结果地址。")


def _decode_downloaded_image(image_bytes: bytes) -> QwenDownloadedImage:
    if not image_bytes:
        raise ModelGatewayError("Qwen Image 结果图片为空。")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.verify()
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
            image_format = str(image.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ModelGatewayError("Qwen Image 结果无法回读为有效图片。") from exc
    if width < 1 or height < 1 or width * height > _MAX_OUTPUT_IMAGE_PIXELS:
        raise ModelGatewayError("Qwen Image 结果图片尺寸无效或超过 4000 万像素上限。")
    mime_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image_format)
    if not mime_type:
        raise ModelGatewayError("Qwen Image 结果格式不在 PNG、JPEG、WEBP 支持范围内。")
    return QwenDownloadedImage(
        image_bytes=image_bytes,
        mime_type=mime_type,
        image_format=image_format,
        width=width,
        height=height,
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_optional_int(*values: object) -> int | None:
    for value in values:
        parsed = _optional_int(value)
        if parsed is not None:
            return parsed
    return None


def _optional_text(value: object, *, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]
