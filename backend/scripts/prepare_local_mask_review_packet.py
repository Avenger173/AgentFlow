"""Prepare an offline independent-review packet from frozen local mask evidence.

The packet makes alpha or binary-mask results inspectable without asserting they are
visually correct. It never loads model weights and never sends a network request.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
FIXTURE_SET = "agentflow-mm0-public-image-fixtures-v2"
_CANDIDATES: dict[str, tuple[str, str, str]] = {
    "lite_matting": ("BiRefNet_lite-matting-epoch_110", "alpha_file", "alpha"),
    "sam2_selection": ("sam2.1_hiera_tiny", "mask_file", "binary_mask"),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate", choices=sorted(_CANDIDATES), required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to build an offline local-mask review packet.")
        return

    fixture_dir = args.fixture_dir.resolve()
    run_dir = args.run_dir.resolve()
    candidate = str(args.candidate)
    output_dir = args.output_dir.resolve() if args.output_dir else (
        PROJECT_ROOT / "data" / "media_evaluations" / (
            f"{candidate}_review_packet_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        )
    )
    cases = _collect_cases(fixture_dir=fixture_dir, run_dir=run_dir, candidate=candidate)
    output_dir.mkdir(parents=True, exist_ok=False)
    cards_dir = output_dir / "cards"
    cards_dir.mkdir()
    rows: list[dict[str, str]] = []
    card_records: list[dict[str, str]] = []
    for item in cases:
        card_path = cards_dir / f"{item['case_id'].lower()}_review.png"
        _render_card(item, card_path)
        card_records.append(
            {
                "case_id": str(item["case_id"]),
                "card_file": f"cards/{card_path.name}",
                "card_sha256": _sha256(card_path),
                "source_sha256": str(item["source_sha256"]),
                "mask_sha256": str(item["mask_sha256"]),
            }
        )
        rows.append(
            {
                "case_id": str(item["case_id"]),
                "category": str(item["category"]),
                "review_focus": _review_focus(candidate, str(item["category"])),
                "card_file": f"cards/{card_path.name}",
                "reviewer_id": "",
                "decision": "",
                "note": "",
            }
        )
    _write_csv(output_dir / "review.csv", rows)
    manifest = {
        "packet": "agentflow_local_mask_independent_review_v2",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "candidate": candidate,
        "model": _CANDIDATES[candidate][0],
        "fixture_set": FIXTURE_SET,
        "source_run": run_dir.name,
        "case_count": len(cases),
        "cards": card_records,
        "quality_claim": "none; decisions must be supplied by a non-implementer in review.csv",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "README.md").write_text(_readme(candidate), encoding="utf-8")
    print(json.dumps({"ok": True, "output_dir": str(output_dir), "case_count": len(cases)}, ensure_ascii=False))


def _collect_cases(*, fixture_dir: Path, run_dir: Path, candidate: str) -> list[dict[str, Any]]:
    fixture_manifest = _load_manifest(fixture_dir / "manifest.json", "fixture")
    if fixture_manifest.get("fixture_set") != FIXTURE_SET:
        raise SystemExit("fixture-dir is not the frozen MM-0 public fixture set v2")
    run_manifest = _load_manifest(run_dir / "manifest.json", "candidate run")
    expected_model, artifact_field, _ = _CANDIDATES[candidate]
    if run_manifest.get("fixture_set") != FIXTURE_SET or run_manifest.get("model_id") != expected_model:
        raise SystemExit("candidate run does not match the frozen fixture set or selected model")
    fixtures = _index_cases(fixture_manifest.get("fixtures"), "fixture")
    results = _index_cases(run_manifest.get("cases"), "candidate run")
    if set(fixtures) != set(results) or len(fixtures) != 9:
        raise SystemExit("review packet requires exactly the same nine fixture and candidate cases")
    collected: list[dict[str, Any]] = []
    for case_id in sorted(fixtures):
        fixture = fixtures[case_id]
        result = results[case_id]
        source_path = _safe_file(fixture_dir, fixture.get("file"), "source image")
        if _sha256(source_path) != fixture.get("sha256") or result.get("source_sha256") != fixture.get("sha256"):
            raise SystemExit(f"source hash mismatch: {case_id}")
        mask_path = _safe_file(run_dir, result.get(artifact_field), "mask result")
        with Image.open(source_path) as opened:
            source = opened.convert("RGB")
        with Image.open(mask_path) as opened:
            mask = opened.convert("L")
        if mask.size != source.size:
            raise SystemExit(f"mask size mismatch: {case_id}")
        collected.append(
            {
                "case_id": case_id,
                "category": _category(case_id),
                "source_sha256": fixture["sha256"],
                "mask_sha256": _sha256(mask_path),
                "source": source,
                "mask": mask,
            }
        )
    return collected


def _load_manifest(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{label} manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} manifest must be a JSON object")
    return payload


def _index_cases(value: object, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise SystemExit(f"{label} manifest has no cases list")
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise SystemExit(f"{label} manifest has an invalid case")
        case_id = str(item.get("case_id") or "")
        if not case_id or case_id in result:
            raise SystemExit(f"{label} manifest has duplicate or missing case IDs")
        result[case_id] = item
    return result


def _safe_file(parent: Path, name: object, label: str) -> Path:
    value = str(name or "").strip()
    path = (parent / value).resolve()
    if not value or path.parent != parent or not path.is_file():
        raise SystemExit(f"{label} is missing or unsafe")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _category(case_id: str) -> str:
    if "PERSON" in case_id:
        return "person"
    if "PRODUCT" in case_id:
        return "product"
    if "TRANSPARENT" in case_id:
        return "transparent_object"
    raise SystemExit(f"unknown frozen category: {case_id}")


def _render_card(item: dict[str, Any], output_path: Path) -> None:
    source: Image.Image = item["source"]
    mask: Image.Image = item["mask"]
    maximum = (420, 420)
    source_panel = _fit(source, maximum)
    mask_panel = _fit(mask.convert("RGB"), maximum)
    preview_panel = _fit(_checkerboard_preview(source, mask), maximum)
    cell_width, cell_height = maximum
    header_height = 42
    canvas = Image.new("RGB", (cell_width * 3 + 16, cell_height + header_height + 8), color=(248, 250, 252))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for index, (label, panel) in enumerate((("Input", source_panel), ("Alpha / mask", mask_panel), ("Edge preview", preview_panel))):
        left = 4 + index * (cell_width + 4)
        draw.text((left + cell_width // 2, 14), label, fill=(23, 49, 86), font=font, anchor="mm")
        _paste_centered(canvas, panel, left, header_height, cell_width, cell_height)
    canvas.save(output_path)
    with Image.open(output_path) as opened:
        if opened.format != "PNG" or opened.mode != "RGB":
            raise SystemExit(f"review card readback failed: {output_path.name}")


def _fit(image: Image.Image, maximum: tuple[int, int]) -> Image.Image:
    output = image.copy()
    output.thumbnail(maximum, Image.Resampling.LANCZOS)
    return output


def _checkerboard_preview(source: Image.Image, alpha: Image.Image) -> Image.Image:
    width, height = source.size
    background = Image.new("RGB", (width, height), color=(222, 228, 236))
    draw = ImageDraw.Draw(background)
    tile = 32
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            if (x // tile + y // tile) % 2:
                draw.rectangle((x, y, x + tile - 1, y + tile - 1), fill=(244, 247, 250))
    background.paste(source, mask=alpha)
    return background


def _paste_centered(canvas: Image.Image, panel: Image.Image, left: int, top: int, width: int, height: int) -> None:
    x = left + (width - panel.width) // 2
    y = top + (height - panel.height) // 2
    canvas.paste(panel, (x, y))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _review_focus(candidate: str, category: str) -> str:
    if candidate == "lite_matting":
        focuses = {
            "person": "发丝、胡须和衣物边缘是否自然，背景是否明显漏入。",
            "product": "目标与相邻物体是否混入，杯把、轮廓和阴影是否可接受。",
            "transparent_object": "玻璃、液体和高光是否保留连续透明边缘，是否有孔洞或大片误选。",
        }
    else:
        focuses = {
            "person": "主体覆盖是否合理，背景泄漏和发丝缺失是否会妨碍后续局部编辑。",
            "product": "被选商品是否符合框选意图，邻近物体和背景是否被明显误选。",
            "transparent_object": "二值选区是否足以用于后续局部操作；透明边缘缺失应明确记为问题。",
        }
    return focuses[category]


def _readme(candidate: str) -> str:
    kind = "Lite Matting alpha" if candidate == "lite_matting" else "SAM 二值选区"
    return f"""# 本地模型独立评审包

候选：{kind}

每张卡从左到右为原图、模型 alpha/二值 mask、在棋盘背景上的边缘预览。请由非实现者在 `review.csv` 中填写匿名标识、`accept` / `reject` / `needs_revision` 及原因。

本包不包含任何自动质量结论；模型分数、运行成功和卡片生成成功均不等于质量通过。
"""


if __name__ == "__main__":
    main()
