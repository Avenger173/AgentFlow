"""PPT 制作使用的即梦（Seedream）生成式视觉 Provider。

生成图片和聊天模型是两类不同的外部能力：本模块只在用户确认导出后读取 ``seedream``
专属的安全 Key，向火山方舟图片端点发送由已确认页面槽位派生的短提示词。图片只保存在
本次导出内存中并嵌入 PPTX；任务历史仅保留模型、提示词摘要和来源类型，不保存图片字节、
Key 或 Provider 原始错误正文。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from dataclasses import dataclass
from typing import Sequence

import httpx

from app.core.config import settings
from app.services.model_config_store import ModelConfigStoreError, load_model_config
from app.services.secret_store import SecretStoreError


_SEEDREAM_GENERATIONS_PATH = "/images/generations"
_MAX_IMAGES_PER_EXPORT = 4
_MAX_IMAGE_BYTES = 14 * 1024 * 1024
_MAX_GENERATION_ATTEMPTS = 3


@dataclass(frozen=True)
class SeedreamImageAsset:
    """一张已验证、可内嵌 PPTX 的即梦生成图片及其最小审计信息。"""

    asset_id: str
    query: str
    image_bytes: bytes
    model: str
    prompt_digest: str

    @property
    def credit_text(self) -> str:
        return "AI 生成：Seedream 5.0"

    def audit_metadata(self) -> dict[str, object]:
        """返回供任务历史使用的脱敏元数据，绝不持久化图片或完整提示词。"""

        return {
            "provider": "seedream",
            "source_kind": "ai_generated",
            "asset_id": self.asset_id,
            "query": self.query,
            "model": self.model,
            "prompt_digest": self.prompt_digest,
            # 素材不叠加视觉水印，但仍以来源类型、模型和提示词摘要保持可审计性。
            "watermark": False,
        }


@dataclass(frozen=True)
class SeedreamAssetResolution:
    """一次确认导出中的即梦生成结果；失败允许由本地版式完成降级交付。"""

    images: tuple[SeedreamImageAsset, ...]
    warnings: tuple[str, ...]

    @property
    def provider(self) -> str:
        return "seedream"

    @property
    def label(self) -> str:
        return "Seedream AI 生成图片"


def generate_seedream_images(
    queries: Sequence[str],
    *,
    limit: int = _MAX_IMAGES_PER_EXPORT,
) -> SeedreamAssetResolution:
    """为已确认的页面视觉意图生成最多四张横向图片。

    不接入联网搜索或模型自带工具：生成提示词只由用户确认后的创作计划构成，避免在当前
    阶段把未审计的外部事实、数据或人物形象带进客户交付物。单张失败不阻塞 PPTX 导出，
    对应页面会使用已验证的本地无图版式。
    """

    api_key, key_warning = _seedream_api_key()
    if not api_key:
        return SeedreamAssetResolution(images=(), warnings=(key_warning,))

    clean_queries = _normalize_queries(queries, limit=limit)
    if not clean_queries:
        return SeedreamAssetResolution(
            images=(),
            warnings=("当前创作计划没有可用于生成图片的页面视觉意图，已使用内置版式。",),
        )

    images: list[SeedreamImageAsset] = []
    warnings: list[str] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "AgentFlow-PresentationStudio/0.1",
    }
    try:
        # 生成端点明显慢于图库检索。单图上限与 Qt 总时限按“四张串行 + 渲染余量”共同设计，
        # 避免文件已成功写出、客户端却抢先把长任务误判为超时。
        with httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(75.0, connect=10.0),
            follow_redirects=False,
        ) as client:
            for query in clean_queries:
                image: SeedreamImageAsset | None = None
                last_error: Exception | None = None
                # 方舟生成端点偶尔在账号已开通、Key 有效时短暂返回 429。只对明确的瞬时
                # 拥塞/超时进行有限重试；内容策略、授权和参数错误不重试，避免重复消耗额度。
                for attempt in range(_MAX_GENERATION_ATTEMPTS):
                    try:
                        image = _generate_one_image(client, query=query)
                        break
                    except (httpx.HTTPError, ValueError) as exc:
                        last_error = exc
                        if attempt + 1 < _MAX_GENERATION_ATTEMPTS and _is_retryable_generation_error(exc):
                            time.sleep(_retry_delay_seconds(exc, attempt))
                            continue
                        break
                if image is None:
                    assert last_error is not None
                    warnings.append(f"即梦图片“{query}”未能生成：{_safe_error_message(last_error)}")
                    continue
                images.append(image)
    except httpx.HTTPError as exc:
        warnings.append(f"即梦图像服务连接失败，已使用内置版式：{_safe_error_message(exc)}")

    if not images and not warnings:
        warnings.append("即梦未返回可嵌入的图片，本次已使用内置版式。")
    return SeedreamAssetResolution(images=tuple(images), warnings=tuple(warnings[:4]))


def _seedream_api_key() -> tuple[str, str]:
    """优先读取桌面端 DPAPI 密钥，环境变量只作为无桌面配置时的部署兜底。"""

    try:
        stored_config = load_model_config()
        if stored_config.api_key_configured_for("seedream"):
            return stored_config.decrypt_api_key("seedream"), ""
    except (ModelConfigStoreError, SecretStoreError):
        # 不能把解密层的异常、路径或密文状态暴露给任务历史；仍允许显式环境配置用于部署。
        pass
    if settings.seedream_api_key.strip():
        return settings.seedream_api_key.strip(), ""
    return "", "未配置 Seedream 图像生成 Key，本次已自动使用内置版式。"


def _generate_one_image(client: httpx.Client, *, query: str) -> SeedreamImageAsset:
    prompt = _build_generation_prompt(query)
    response = client.post(
        f"{settings.seedream_base_url}{_SEEDREAM_GENERATIONS_PATH}",
        json={
            "model": settings.seedream_model,
            "prompt": prompt,
            # 使用已探测到可被方舟端点接受的参数组合；横向演示页会在 PPT 渲染层裁切到 16:9。
            "size": "2K",
            # 每个页面槽位只需要一张图。显式关闭组图可避免模型默认进入多图任务，降低导出
            # 期间的排队压力，也与火山方舟 ImageGenerations 的单图调用约定保持一致。
            "sequential_image_generation": "disabled",
            "response_format": "b64_json",
            "output_format": "jpeg",
            # 交付图片不请求可见水印；PPT 来源页与任务 artifact 仍会标明其为 AI 生成素材。
            "watermark": False,
        },
    )
    if response.is_error:
        raise _SeedreamHttpError(response)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("图像服务响应格式无效")
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("图像服务没有返回图片数据")
    encoded_image = data[0].get("b64_json")
    if not isinstance(encoded_image, str) or not encoded_image.strip():
        raise ValueError("图像服务没有返回可用图片")
    try:
        image_bytes = base64.b64decode(encoded_image, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图像服务返回的图片编码无效") from exc
    if not image_bytes.startswith(b"\xff\xd8\xff"):
        raise ValueError("图像服务返回了不支持的图片类型")
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise ValueError("图像服务返回的图片体积超过导出限制")

    asset_id = str(data[0].get("id") or hashlib.sha256(image_bytes).hexdigest()[:20])
    return SeedreamImageAsset(
        asset_id=asset_id[:100],
        query=query,
        image_bytes=image_bytes,
        model=settings.seedream_model,
        prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:20],
    )


def _build_generation_prompt(query: str) -> str:
    """将模型生成的短查询收束为适合演示页的视觉意图，而不是要求模型凭空造事实。"""

    return (
        "为一页专业中文演示文稿生成横向视觉插图。"
        f"主题：{query}。"
        "画面应具有清晰主次、现代构图和适当留白，适合 16:9 页面裁切；"
        "不要生成任何文字、数字、Logo、品牌标识、界面截图、图表或未经证实的事实性数据。"
    )


def _normalize_queries(queries: Sequence[str], *, limit: int) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for value in queries:
        query = " ".join(str(value).split()).strip()
        key = query.casefold()
        if len(query) < 3 or key in seen:
            continue
        seen.add(key)
        values.append(query[:120])
        if len(values) == max(1, min(limit, _MAX_IMAGES_PER_EXPORT)):
            break
    return values


class _SeedreamHttpError(ValueError):
    """保留状态码和错误码给脱敏映射使用，不向调用方泄露响应正文。"""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.error_code = _response_error_code(response)
        self.retry_after_seconds = _retry_after_seconds(response.headers.get("Retry-After", ""))
        super().__init__("Seedream request failed")


def _is_retryable_generation_error(exc: Exception) -> bool:
    """只重试明确的瞬时拥塞/超时，不掩盖配置与内容问题。"""

    if isinstance(exc, httpx.TimeoutException):
        return True
    return isinstance(exc, _SeedreamHttpError) and exc.status_code in {429, 500, 502, 503, 504}


def _retry_after_seconds(value: str) -> float:
    """只接受 Provider 明确给出的秒数，避免解析日期或异常响应导致无限等待。"""

    try:
        return min(12.0, max(0.0, float(value)))
    except ValueError:
        return 0.0


def _retry_delay_seconds(exc: Exception, attempt: int) -> float:
    """优先尊重限流提示；没有提示时做短暂退避，保持 Qt 总超时可控。"""

    if isinstance(exc, _SeedreamHttpError) and exc.retry_after_seconds > 0:
        return exc.retry_after_seconds
    return 2.0 * (attempt + 1)


def _response_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or "")


def _safe_error_message(exc: Exception) -> str:
    """将 Provider 故障转成客户可行动的说明，永不拼接原始正文、URL 或请求头。"""

    if isinstance(exc, _SeedreamHttpError):
        code = exc.error_code.casefold()
        if code == "modelnotopen":
            return "当前账号尚未开通 Seedream 5.0，请在火山方舟模型广场开通后重试"
        if "content" in code or "policy" in code:
            return "生成请求未通过内容安全策略"
        if exc.status_code in {401, 403}:
            return "图像生成 Key 未获授权或已失效"
        if exc.status_code == 429:
            if code == "quotaexceeded":
                return "当前 Seedream 账号的生成队列或并发额度已满，请稍后重试"
            return "图像生成服务繁忙，请稍后重试"
        return f"图像生成服务返回 HTTP {exc.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "单张图片生成超时"
    return "服务暂不可用"
