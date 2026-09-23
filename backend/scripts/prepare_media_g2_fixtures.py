"""Freeze the public-source MM-2 G2 fixture set without calling an image model."""

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
from urllib.parse import quote

import httpx
from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


_COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
_USER_AGENT = "AgentFlow-Media-G2-Evaluation/0.1 (https://github.com/Avenger173/AgentFlow)"
_MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
_MAX_IMAGE_PIXELS = 12_000_000
_SUITE_TYPE = "agentflow-mm2-image-quality-suite-v1"


@dataclass(frozen=True)
class _ExtraSeed:
    fixture_id: str
    title: str


_EXTRA_SEEDS = (
    _ExtraSeed("G2-IMAGE-10", "File:Product photo.jpg"),
    _ExtraSeed("G2-IMAGE-11", "File:Product photography.jpg"),
    _ExtraSeed("G2-IMAGE-12", "File:Portrait-of-a-woman.jpg"),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-fixture-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Fetch the three additional Commons fixtures.")
    parser.add_argument(
        "--confirm-public-fixture-rights",
        action="store_true",
        help="Record that these public-source fixtures are limited to the internal G2 evaluation.",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute and --confirm-public-fixture-rights to freeze G2 fixtures.")
        return
    if not args.confirm_public_fixture_rights:
        parser.error("--confirm-public-fixture-rights is required for a frozen G2 source set")

    output_dir = args.output_dir or (
        PROJECT_ROOT / "data" / "media_evaluation_fixtures" / datetime.now(UTC).strftime("mm2_g2_public_v1_%Y%m%dT%H%M%SZ")
    )
    try:
        manifest = asyncio.run(
            _prepare(
                source_fixture_dir=args.source_fixture_dir.resolve(),
                output_dir=output_dir.resolve(),
            )
        )
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(
        json.dumps(
            {
                "ok": True,
                "output_dir": str(output_dir),
                "fixture_count": len(manifest["fixtures"]),
                "task_count": len(manifest["tasks"]),
                "commons_metadata_requests": len(_EXTRA_SEEDS),
                "commons_download_requests": len(_EXTRA_SEEDS),
            },
            ensure_ascii=False,
        )
    )


async def _prepare(*, source_fixture_dir: Path, output_dir: Path) -> dict[str, object]:
    source_manifest = _read_source_manifest(source_fixture_dir)
    if output_dir.exists():
        raise RuntimeError("output directory already exists")
    output_dir.mkdir(parents=True)
    fixtures: list[dict[str, object]] = []
    try:
        fixtures.extend(_copy_existing_fixtures(source_fixture_dir, source_manifest, output_dir))
        timeout = httpx.Timeout(45.0, connect=10.0)
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json,image/*;q=0.8"}
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            for seed in _EXTRA_SEEDS:
                fixtures.append(await _fetch_extra_fixture(client, seed, output_dir))
        if len(fixtures) != 12:
            raise RuntimeError("G2 fixture assembly did not produce 12 sources")
        tasks = _build_tasks(fixtures)
        payload = {
            "suite_type": _SUITE_TYPE,
            "fixture_set": "agentflow-mm2-g2-public-image-fixtures-v1",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source_fixture_set": source_manifest["fixture_set"],
            "source_policy": "public Commons assets; internal G2 evaluation only",
            "fixtures": fixtures,
            "tasks": tasks,
            "review_protocol": {
                "reviewer_count": 2,
                "blind": True,
                "non_implementer_required": True,
                "dimensions": ["instruction_adherence", "target_protection", "edge_naturalness"],
                "score_min": 1,
                "score_max": 5,
                "pass_score": 4,
                "disagreement_recheck_gap": 2,
            },
        }
        (output_dir / "suite.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    except BaseException:
        for path in output_dir.glob("*"):
            path.unlink(missing_ok=True)
        output_dir.rmdir()
        raise


def _read_source_manifest(path: Path) -> dict[str, object]:
    manifest_path = path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("source fixture manifest cannot be read") from exc
    if not isinstance(manifest, dict) or manifest.get("fixture_set") != "agentflow-mm0-public-image-fixtures-v2":
        raise RuntimeError("source fixture directory must be the frozen MM-0 public v2 set")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) != 9:
        raise RuntimeError("source fixture directory must contain exactly 9 records")
    return manifest


def _copy_existing_fixtures(source_dir: Path, manifest: dict[str, object], output_dir: Path) -> list[dict[str, object]]:
    copied: list[dict[str, object]] = []
    raw_fixtures = manifest["fixtures"]
    assert isinstance(raw_fixtures, list)
    for index, raw in enumerate(raw_fixtures, start=1):
        if not isinstance(raw, dict):
            raise RuntimeError("source fixture record is invalid")
        filename = str(raw.get("file") or "").strip()
        source_path = (source_dir / filename).resolve()
        if not filename or source_path.parent != source_dir or not source_path.is_file():
            raise RuntimeError("source fixture file reference is invalid")
        content = source_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != str(raw.get("sha256") or ""):
            raise RuntimeError("source fixture hash mismatch")
        suffix = source_path.suffix.lower()
        target_name = f"g2-image-{index:02d}{suffix}"
        (output_dir / target_name).write_bytes(content)
        copied.append(
            _fixture_record(
                fixture_id=f"G2-IMAGE-{index:02d}",
                split="development" if index <= 8 else "holdout",
                file_name=target_name,
                content=content,
                source_page=str(raw.get("source_page") or ""),
                license_name=str(raw.get("license") or ""),
                license_url=str(raw.get("license_url") or ""),
                artist=str(raw.get("artist") or ""),
                source_origin_id=str(raw.get("case_id") or ""),
            )
        )
    return copied


async def _fetch_extra_fixture(client: httpx.AsyncClient, seed: _ExtraSeed, output_dir: Path) -> dict[str, object]:
    params = {
        "action": "query",
        "titles": seed.title,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": "1280",
        "format": "json",
        "formatversion": "2",
    }
    response = await client.get(_COMMONS_API_URL, params=params)
    response.raise_for_status()
    image_info = _single_image_info(response.json())
    source_url = str(image_info.get("thumburl") or "").strip()
    if not source_url.startswith("https://"):
        raise RuntimeError(f"{seed.fixture_id} has no HTTPS thumbnail URL")
    content = await _download_image(client, source_url)
    image_format, _, _ = _validate_image(content)
    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image_format)
    if suffix is None:
        raise RuntimeError(f"{seed.fixture_id} returned unsupported image format")
    file_name = f"{seed.fixture_id.lower()}{suffix}"
    (output_dir / file_name).write_bytes(content)
    metadata = image_info.get("extmetadata") if isinstance(image_info.get("extmetadata"), dict) else {}
    return _fixture_record(
        fixture_id=seed.fixture_id,
        split="holdout",
        file_name=file_name,
        content=content,
        source_page=f"https://commons.wikimedia.org/wiki/{quote(seed.title.replace(' ', '_'))}",
        license_name=_metadata_value(metadata, "LicenseShortName"),
        license_url=_metadata_value(metadata, "LicenseUrl"),
        artist=_metadata_value(metadata, "Artist"),
        source_origin_id=seed.title,
    )


def _fixture_record(
    *,
    fixture_id: str,
    split: str,
    file_name: str,
    content: bytes,
    source_page: str,
    license_name: str,
    license_url: str,
    artist: str,
    source_origin_id: str,
) -> dict[str, object]:
    source_page = source_page.strip()
    license_name = license_name.strip()
    license_url = license_url.strip()
    if license_url.startswith("http://"):
        license_url = "https://" + license_url.removeprefix("http://")
    if not license_url and license_name.casefold() == "public domain":
        license_url = source_page
    if not source_page.startswith("https://") or not license_name or not license_url.startswith("https://"):
        raise RuntimeError(f"{fixture_id} lacks public-source provenance metadata")
    return {
        "fixture_id": fixture_id,
        "split": split,
        "file": file_name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "source_page": source_page,
        "license": license_name,
        "license_url": license_url,
        "artist": artist,
        "source_origin_id": source_origin_id,
        "rights_reviewed": True,
    }


def _build_tasks(fixtures: list[dict[str, object]]) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for fixture in fixtures:
        fixture_id = str(fixture["fixture_id"])
        tasks.extend(
            (
                {
                    "task_id": f"{fixture_id}-background",
                    "fixture_id": fixture_id,
                    "category": "background_replace",
                    "instruction": "\u5c06\u4e3b\u4f53\u4e4b\u5916\u7684\u80cc\u666f\u66ff\u6362\u4e3a\u5e72\u51c0\u3001\u81ea\u7136\u7684\u6d45\u7070\u84dd\u6444\u5f71\u68da\u80cc\u666f\u3002\u4fdd\u7559\u4e3b\u4f53\u3001\u8fb9\u7f18\u3001\u6750\u8d28\u3001\u6784\u56fe\u548c\u5149\u7167\uff0c\u4e0d\u8981\u6539\u53d8\u4e3b\u4f53\u3002",
                    "target": "background outside the principal subject",
                    "preserve": "principal subject, composition, material detail, and lighting",
                    "expected_result": "natural studio-style background with subject retained",
                    "input_recipe": "source_only",
                    "max_provider_calls": 1,
                },
                {
                    "task_id": f"{fixture_id}-removal",
                    "fixture_id": fixture_id,
                    "category": "object_removal",
                    "instruction": "\u79fb\u9664\u753b\u9762\u53f3\u4e0b\u89d2\u7ea2\u8272\u5706\u5f62\u201c\u6d4b\u8bd5\u8d34\u7eb8\u201d\uff0c\u53ea\u79fb\u9664\u8d34\u7eb8\u53ca\u5176\u9634\u5f71\uff0c\u7528\u5468\u56f4\u573a\u666f\u81ea\u7136\u8865\u5168\uff0c\u4fdd\u7559\u4e3b\u4f53\u548c\u5176\u4ed6\u533a\u57df\u3002",
                    "target": "synthetic red circular test sticker at the lower right",
                    "preserve": "principal subject and all non-sticker image regions",
                    "expected_result": "sticker and shadow removed without a residual red ring",
                    "input_recipe": "synthetic_sticker_bottom_right",
                    "max_provider_calls": 1,
                },
                {
                    "task_id": f"{fixture_id}-text",
                    "fixture_id": fixture_id,
                    "category": "text_edit",
                    "instruction": "\u5c06\u753b\u9762\u5de6\u4e0a\u89d2\u767d\u8272\u4ef7\u683c\u6807\u7b7e\u4e2d\u7684\u201c99\u201d\u6539\u4e3a\u201c129\u201d\uff0c\u4fdd\u7559\u201c\u65b0\u54c1\u201d\u548c\u201c\u5143\u201d\u3001\u6807\u7b7e\u4f4d\u7f6e\u3001\u5b57\u4f53\u989c\u8272\u3001\u4e3b\u4f53\u4e0e\u5176\u4ed6\u533a\u57df\u4e0d\u53d8\u3002",
                    "target": "99 in the synthetic top-left price label",
                    "preserve": "label layout, non-target label text, subject, and all other regions",
                    "expected_result": "the label reads the requested Chinese price with only the number changed",
                    "input_recipe": "synthetic_chinese_price_label_top_left",
                    "max_provider_calls": 1,
                },
            )
        )
    return tasks


def _single_image_info(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RuntimeError("Commons metadata response is invalid")
    query = payload.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
        raise RuntimeError("Commons metadata did not return one page")
    image_info = pages[0].get("imageinfo")
    if not isinstance(image_info, list) or len(image_info) != 1 or not isinstance(image_info[0], dict):
        raise RuntimeError("Commons metadata did not return one image record")
    return image_info[0]


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            raise RuntimeError("Commons fixture download is not an image")
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError("Commons fixture exceeds 12 MB")
            chunks.append(chunk)
    return b"".join(chunks)


def _validate_image(content: bytes) -> tuple[str, int, int]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
    except (OSError, ValueError) as exc:
        raise RuntimeError("Commons fixture cannot be decoded") from exc
    if width < 512 or height < 512 or width * height > _MAX_IMAGE_PIXELS:
        raise RuntimeError("Commons fixture dimensions are outside the G2 bounds")
    return image_format, width, height


def _metadata_value(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    return str(value.get("value") or "").strip() if isinstance(value, dict) else ""


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


if __name__ == "__main__":
    main()
