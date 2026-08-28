"""PPT 制作使用的 Pexels 授权图片 Provider。

该模块只接受 Runtime 已确认的短检索词，并在导出线程内执行。它不接触模型 Key、不把图片
写回 workspace，也不把二进制内容放进 SQLite；PPTX 会内嵌图片，任务历史只保留可追溯的
摄影师、照片页和许可证说明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlparse

import httpx

from app.core.config import settings


_PEXELS_API_ROOT = "https://api.pexels.com/v1"
_PEXELS_IMAGE_HOST = "images.pexels.com"
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png"}
_MAX_IMAGES_PER_EXPORT = 6


@dataclass(frozen=True)
class PexelsImageAsset:
    """一张可安全嵌入 PPTX 的已下载图片，以及必要的署名信息。"""

    photo_id: int
    query: str
    image_bytes: bytes
    photo_url: str
    photographer: str
    photographer_url: str

    @property
    def credit_text(self) -> str:
        photographer = self.photographer or "Pexels 摄影师"
        return f"图片：{photographer} · Pexels"

    def audit_metadata(self) -> dict[str, object]:
        """仅返回可审计元数据，绝不把原始图片字节写入任务数据库。"""

        return {
            "provider": "pexels",
            "photo_id": self.photo_id,
            "query": self.query,
            "photo_url": self.photo_url,
            "photographer": self.photographer,
            "photographer_url": self.photographer_url,
            "license": "Pexels License",
        }


@dataclass(frozen=True)
class PexelsAssetResolution:
    """一次导出中的图库结果。warnings 可直接交给交付层展示与审计。"""

    images: tuple[PexelsImageAsset, ...]
    warnings: tuple[str, ...]

    @property
    def provider(self) -> str:
        return "pexels"

    @property
    def label(self) -> str:
        return "Pexels 授权图片"


def fetch_pexels_images(queries: Sequence[str], *, limit: int = _MAX_IMAGES_PER_EXPORT) -> PexelsAssetResolution:
    """按逐页短检索词下载最多六张横版图片，所有异常均以可降级 warning 返回。

    Pexels 默认额度有限，因此一份 PPT 最多进行 ``limit`` 次检索，每个检索只取一张结果。
    下载 URL 必须来自 Pexels API 返回的 ``images.pexels.com`` HTTPS 地址，避免图库 Provider
    被误用为任意网络下载通道。
    """

    api_key = settings.pexels_api_key.strip()
    if not api_key:
        return PexelsAssetResolution(
            images=(),
            warnings=("未配置 Pexels 授权图库 Key，本次已自动使用无图设计版式。",),
        )

    clean_queries = _normalize_queries(queries, limit=limit)
    if not clean_queries:
        return PexelsAssetResolution(
            images=(),
            warnings=("当前创作计划没有可用的授权图片检索词，本次已自动使用无图设计版式。",),
        )

    images: list[PexelsImageAsset] = []
    warnings: list[str] = []
    seen_photo_ids: set[int] = set()
    headers = {
        "Authorization": api_key,
        "User-Agent": "AgentFlow-PresentationStudio/0.1",
    }
    try:
        # 导出请求本身在后台线程执行。给跨境图库留出有限的读取时间，但单张失败会降级，
        # 不允许图片 Provider 无限占住一次 PPT 导出。
        with httpx.Client(headers=headers, timeout=httpx.Timeout(14.0, connect=7.0), follow_redirects=False) as client:
            for query in clean_queries:
                try:
                    image = _fetch_one_image(client, query=query, seen_photo_ids=seen_photo_ids)
                except (httpx.HTTPError, ValueError) as exc:
                    warnings.append(f"授权图片“{query}”未能获取：{_safe_error_message(exc)}")
                    continue
                if image is not None:
                    images.append(image)
                    seen_photo_ids.add(image.photo_id)
    except httpx.HTTPError as exc:
        warnings.append(f"授权图库连接失败，本次已自动使用无图设计版式：{_safe_error_message(exc)}")

    if not images and not warnings:
        warnings.append("授权图库没有返回适合当前主题的横版图片，本次已自动使用无图设计版式。")
    return PexelsAssetResolution(images=tuple(images), warnings=tuple(warnings[:4]))


def _fetch_one_image(
    client: httpx.Client,
    *,
    query: str,
    seen_photo_ids: set[int],
) -> PexelsImageAsset | None:
    response = client.get(
        f"{_PEXELS_API_ROOT}/search",
        # 取一小组候选并优先选择 alt 文本与检索词重合的图片。它不是多模态语义判定，
        # 但能避免“查询匹配却直接拿 API 第一张”的明显错配，并保持 Provider 的调用可控。
        params={"query": query, "orientation": "landscape", "per_page": 12, "locale": "zh-CN"},
    )
    response.raise_for_status()
    photos = response.json().get("photos", [])
    if not isinstance(photos, list):
        raise ValueError("图库响应格式无效")

    candidates = sorted(
        (photo for photo in photos if isinstance(photo, dict)),
        key=lambda photo: _photo_relevance_score(photo, query),
        reverse=True,
    )
    for photo in candidates:
        photo_id = photo.get("id")
        if not isinstance(photo_id, int) or photo_id in seen_photo_ids:
            continue
        image_url = ((photo.get("src") or {}).get("landscape") or "").strip()
        if not _is_safe_pexels_image_url(image_url):
            continue
        image_response = client.get(image_url)
        image_response.raise_for_status()
        content_type = image_response.headers.get("content-type", "").split(";", 1)[0].lower()
        image_bytes = image_response.content
        if content_type not in _SUPPORTED_IMAGE_TYPES:
            raise ValueError("图库返回了不支持的图片类型")
        if not image_bytes or len(image_bytes) > _MAX_IMAGE_BYTES:
            raise ValueError("图库图片体积不符合导出限制")
        return PexelsImageAsset(
            photo_id=photo_id,
            query=query,
            image_bytes=image_bytes,
            photo_url=str(photo.get("url") or ""),
            photographer=str(photo.get("photographer") or ""),
            photographer_url=str(photo.get("photographer_url") or ""),
        )
    return None


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


def _photo_relevance_score(photo: dict[str, object], query: str) -> tuple[int, int]:
    """以 Provider 元数据做轻量重排，绝不把未下载图片或 Key 送入额外模型调用。"""

    description = str(photo.get("alt") or "").casefold()
    terms = [term for term in query.casefold().split() if len(term) >= 3]
    matched = sum(1 for term in terms if term in description)
    # 相同重合度时保持较早结果优先，避免引入无法解释的随机排序。
    photo_id = photo.get("id")
    stable_id = photo_id if isinstance(photo_id, int) else 0
    return matched, -stable_id


def _is_safe_pexels_image_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname == _PEXELS_IMAGE_HOST


def _safe_error_message(exc: Exception) -> str:
    """不返回请求头、URL 参数或 Provider 原始正文，避免把 Key/内部细节带进任务历史。"""

    if isinstance(exc, httpx.HTTPStatusError):
        return f"Pexels 返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    return "服务暂不可用"
