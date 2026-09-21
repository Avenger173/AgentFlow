"""下载具有可回读授权元数据的 MM-0 图片评测夹具。

默认不联网。``--execute`` 只从固定的 Wikimedia Commons 文件页下载三个公开样本，并将页面
返回的许可证、作者、来源 URL 和 SHA-256 与图片一起写到忽略目录。它不读取用户文件，也不会
调用任何模型或使用 API Key。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


_COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
_USER_AGENT = (
    "AgentFlow-Media-Evaluation/0.1 "
    "(https://github.com/Avenger173/AgentFlow; https://github.com/Avenger173/AgentFlow/issues)"
)
_MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
_MAX_IMAGE_PIXELS = 12_000_000


@dataclass(frozen=True)
class _FixtureSeed:
    case_id: str
    title: str
    purpose: str


_FIXTURES = (
    _FixtureSeed(
        case_id="MM0-PERSON-01",
        title="File:Bearded man with long hair-3052641.jpg",
        purpose="人像头发与轮廓的自动前景蒙版人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-PERSON-02",
        title="File:Long hair-2.jpg",
        purpose="长直发、近景人像与室内复杂背景的自动前景蒙版人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-PERSON-03",
        title='File:"A young Iranian man with long hair" 01.jpg',
        purpose="长卷发、全身人像与室外复杂背景的自动前景蒙版人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-PRODUCT-01",
        title="File:Master Kong Chef's Table Products.jpg",
        purpose="多物体商品边缘与背景分离的人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-PRODUCT-02",
        title="File:Nikon Z 5 w Nikkor 50mm f1.8S.jpg",
        purpose="深色相机机身、镜头环纹与浅色背景的商品边缘人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-PRODUCT-03",
        title="File:Cup of tea isolated on white background - Petr Kratochvil.jpg",
        purpose="杯把、杯碟、柔和阴影与浅色背景的商品边缘人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-TRANSPARENT-01",
        title="File:Orange juice 1 edit1.jpg",
        purpose="透明或半透明玻璃边缘的人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-TRANSPARENT-02",
        title="File:Port wine.jpg",
        purpose="高光、反射与投影下透明酒杯边缘的人工复核",
    ),
    _FixtureSeed(
        case_id="MM0-TRANSPARENT-03",
        title="File:Wine glass with red wine (1).jpg",
        purpose="暗背景、玻璃杯壁与液体边缘的人工复核",
    ),
)


async def _prepare_fixture(client: httpx.AsyncClient, *, seed: _FixtureSeed, output_dir: Path) -> dict[str, object]:
    payload = await _query_image_metadata(client, seed.title)
    image_info = _single_image_info(payload)
    source_url = str(image_info.get("thumburl") or "").strip()
    if not source_url.startswith("https://"):
        raise RuntimeError(f"{seed.case_id} 没有可下载的 HTTPS 缩略图 URL。")
    image_bytes = await _download_image(client, source_url)
    image_format, width, height = _validate_image(image_bytes)
    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image_format)
    if suffix is None:
        raise RuntimeError(f"{seed.case_id} 图片格式 {image_format} 不在夹具允许范围。")
    filename = f"{seed.case_id.lower()}{suffix}"
    (output_dir / filename).write_bytes(image_bytes)
    metadata = image_info.get("extmetadata") if isinstance(image_info.get("extmetadata"), dict) else {}
    return {
        "case_id": seed.case_id,
        "purpose": seed.purpose,
        "file": filename,
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "byte_size": len(image_bytes),
        "format": image_format,
        "width": width,
        "height": height,
        "source_page": f"https://commons.wikimedia.org/wiki/{seed.title.replace(' ', '_')}",
        "download_url": source_url,
        "license": _metadata_value(metadata, "LicenseShortName"),
        "license_url": _metadata_value(metadata, "LicenseUrl"),
        "artist": _metadata_value(metadata, "Artist"),
        "credit": _metadata_value(metadata, "Credit"),
        "quality_status": "pending_human_review",
    }


async def _query_image_metadata(client: httpx.AsyncClient, title: str) -> dict[str, object]:
    params = {
        "action": "query",
        "titles": title,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": "1280",
        "format": "json",
        "formatversion": "2",
    }
    response = await client.get(_COMMONS_API_URL, params=params)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("Wikimedia Commons 元数据响应不是 JSON object。")
    return body


def _single_image_info(payload: dict[str, object]) -> dict[str, object]:
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    pages = query.get("pages") if isinstance(query.get("pages"), list) else []
    if len(pages) != 1 or not isinstance(pages[0], dict):
        raise RuntimeError("Wikimedia Commons 没有返回唯一的文件页。")
    infos = pages[0].get("imageinfo") if isinstance(pages[0].get("imageinfo"), list) else []
    if len(infos) != 1 or not isinstance(infos[0], dict):
        raise RuntimeError("Wikimedia Commons 文件页缺少 imageinfo 元数据。")
    return infos[0]


async def _download_image(client: httpx.AsyncClient, source_url: str) -> bytes:
    async with client.stream("GET", source_url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise RuntimeError("Wikimedia Commons 返回的夹具不是图片内容。")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError("单个公开图片夹具超过 12 MB 限制。")
            chunks.append(chunk)
    return b"".join(chunks)


def _validate_image(image_bytes: bytes) -> tuple[str, int, int]:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.verify()
        with Image.open(BytesIO(image_bytes)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
    except (OSError, ValueError) as exc:
        raise RuntimeError("公开图片夹具无法由 Pillow 回读。") from exc
    if width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS:
        raise RuntimeError("公开图片夹具尺寸无效或超过 1200 万像素限制。")
    return image_format, width, height


def _metadata_value(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, dict):
        return ""
    return str(value.get("value") or "").strip()


async def _run(output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=False)
    timeout = httpx.Timeout(45.0, connect=10.0)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json,image/*;q=0.8"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        fixtures = [await _prepare_fixture(client, seed=seed, output_dir=output_dir) for seed in _FIXTURES]
    manifest = {
        "fixture_set": "agentflow-mm0-public-image-fixtures-v2",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "Wikimedia Commons API imageinfo metadata",
        "fixtures": fixtures,
        "review_note": "仅用于本机评测；每张图片都需要人工复核蒙版边缘，不得将许可证记录替代质量评分。",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="下载固定的公开授权图片夹具")
    parser.add_argument("--output-dir", type=Path, help="忽略的数据目录；默认创建带时间戳的目录")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to download the fixed public MM-0 fixture set.")
        return
    output_dir = args.output_dir or (
        PROJECT_ROOT / "data" / "media_evaluation_fixtures" / datetime.now(UTC).strftime("mm0_public_v2_%Y%m%dT%H%M%SZ")
    )
    try:
        manifest = asyncio.run(_run(output_dir.resolve()))
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"fixture preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps({"ok": True, "output_dir": str(output_dir), "fixture_count": len(manifest["fixtures"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
