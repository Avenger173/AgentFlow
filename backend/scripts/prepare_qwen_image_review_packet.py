"""Prepare a low-friction independent review packet for frozen Qwen Image evidence.

The packet is an offline evidence aid. It does not call a model, alter images, or determine
quality. A reviewer sees the original, locally composed result, and fixed edit scope together,
then records a simple decision in a blank CSV.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
EXPECTED_FIXTURE_SET = "agentflow-mm0-public-image-fixtures-v2"
EXPECTED_MODEL = "qwen-image-3.0-pro"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, action="append", required=True, help="Qwen 质量探针结果目录；可重复指定")
    parser.add_argument("--output-dir", type=Path, help="审阅包输出目录；默认写入忽略的评测目录")
    parser.add_argument("--execute", action="store_true", help="明确生成离线审阅包")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run only. Pass --execute to prepare the offline Qwen review packet.")
        return

    cases = _load_cases([path.resolve() for path in args.run_dir])
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else PROJECT_ROOT / "data" / "media_evaluations" / ("qwen_image_review_packet_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    cards_dir = output_dir / "cards"
    cards_dir.mkdir()
    rows: list[dict[str, str]] = []
    card_records: list[dict[str, str]] = []
    for item in cases:
        card_name = f"{item['case_id'].lower()}_review.png"
        card_path = cards_dir / card_name
        _render_review_card(item, card_path)
        card_records.append(
            {
                "case_id": item["case_id"],
                "card_file": f"cards/{card_name}",
                "card_sha256": _sha256(card_path),
                "input_sha256": item["input_sha256"],
            }
        )
        rows.append(
            {
                "case_id": item["case_id"],
                "category": _category_label(item["mode"]),
                "goal": _goal_label(item["case_id"], item["mode"]),
                "review_focus": _review_focus(item["mode"]),
                "card_file": f"cards/{card_name}",
                "reviewer_id": "",
                "decision": "",
                "note": "",
            }
        )
    _write_csv(output_dir / "review.csv", rows)
    _write_readme(output_dir, rows)
    manifest = {
        "packet": "qwen_image_independent_review_v2",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fixture_set": EXPECTED_FIXTURE_SET,
        "model": EXPECTED_MODEL,
        "case_count": len(rows),
        "source_runs": sorted({item["run_dir"].name for item in cases}),
        "cards": card_records,
        "quality_claim": "none; decisions must be supplied by a non-implementer in review.csv",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output_dir": str(output_dir), "case_count": len(rows)}))


def _load_cases(run_dirs: list[Path]) -> list[dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        manifest_path = run_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"review source manifest is unreadable: {run_dir}") from exc
        if not isinstance(manifest, dict) or manifest.get("fixture_set") != EXPECTED_FIXTURE_SET:
            raise SystemExit(f"review source is not the frozen v2 fixture run: {run_dir.name}")
        if manifest.get("requested_model") != EXPECTED_MODEL:
            raise SystemExit(f"review source did not use {EXPECTED_MODEL}: {run_dir.name}")
        for raw_case in manifest.get("cases", []):
            if not isinstance(raw_case, dict) or raw_case.get("status") != "completed_pending_review":
                continue
            case_id = str(raw_case.get("case_id") or "")
            if not case_id or case_id in cases:
                raise SystemExit(f"duplicate or invalid review case: {case_id or run_dir.name}")
            required = ("input_file", "input_sha256", "mask_file", "mask_composed_file", "mode")
            if any(not raw_case.get(key) for key in required):
                raise SystemExit(f"review case lacks required evidence fields: {case_id}")
            item = {key: raw_case[key] for key in required}
            item["case_id"] = case_id
            item["run_dir"] = run_dir
            _validate_case_files(item)
            cases[case_id] = item
    expected = {f"QWEN-REAL-{index:02d}" for index in range(1, 10)}
    if set(cases) != expected:
        missing = sorted(expected - set(cases))
        unexpected = sorted(set(cases) - expected)
        raise SystemExit(f"review packet requires exactly nine frozen cases; missing={missing}, unexpected={unexpected}")
    return [cases[case_id] for case_id in sorted(cases)]


def _validate_case_files(item: dict[str, Any]) -> None:
    run_dir = item["run_dir"]
    input_path = _safe_child(run_dir, str(item["input_file"]))
    _safe_child(run_dir, str(item["mask_file"]))
    _safe_child(run_dir, str(item["mask_composed_file"]))
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    if digest != item["input_sha256"]:
        raise SystemExit(f"review input hash mismatch: {item['case_id']}")


def _render_review_card(item: dict[str, Any], output_path: Path) -> None:
    run_dir = item["run_dir"]
    source = _open_rgb(_safe_child(run_dir, str(item["input_file"])))
    result = _open_rgb(_safe_child(run_dir, str(item["mask_composed_file"])))
    mask = _open_rgb(_safe_child(run_dir, str(item["mask_file"])))
    if source.size != result.size or source.size != mask.size:
        raise SystemExit(f"review case image sizes do not match: {item['case_id']}")
    panels = [("Input", source), ("Result", result), ("Edit scope", _mask_panel(mask))]
    panel_size = (420, 330)
    title_height = 52
    card = Image.new("RGB", (panel_size[0] * 3, panel_size[1] + title_height), color=(250, 251, 252))
    draw = ImageDraw.Draw(card)
    font = _font(22)
    for index, (label, image) in enumerate(panels):
        panel = ImageOps.contain(image, panel_size, method=Image.Resampling.LANCZOS)
        left = index * panel_size[0] + (panel_size[0] - panel.width) // 2
        top = title_height + (panel_size[1] - panel.height) // 2
        card.paste(panel, (left, top))
        text_box = draw.textbbox((0, 0), label, font=font)
        text_left = index * panel_size[0] + (panel_size[0] - (text_box[2] - text_box[0])) // 2
        draw.text((text_left, 14), label, fill=(33, 50, 75), font=font)
    card.save(output_path, format="PNG")
    with Image.open(output_path) as readback:
        if readback.format != "PNG" or readback.mode != "RGB":
            raise SystemExit(f"review card readback failed: {output_path.name}")


def _mask_panel(mask: Image.Image) -> Image.Image:
    grayscale = mask.convert("L")
    panel = Image.new("RGB", grayscale.size, color=(242, 245, 248))
    overlay = Image.new("RGB", grayscale.size, color=(200, 70, 70))
    panel.paste(overlay, mask=grayscale)
    return panel


def _open_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_child(parent: Path, name: str) -> Path:
    path = (parent / name).resolve()
    if not name or path.parent != parent or not path.is_file():
        raise SystemExit(f"review evidence file is missing or unsafe: {name or '<empty>'}")
    return path


def _category_label(mode: str) -> str:
    return {
        "background_matting_local": "人物换背景",
        "remove_glass_local": "局部移除",
        "remove_synthetic_sticker_local": "局部移除",
        "edit_chinese_label_local": "中文改字",
    }.get(mode, "未知")


def _goal_label(case_id: str, mode: str) -> str:
    goals = {
        "QWEN-REAL-04": "移除右上角玻璃杯并自然补全台面",
        "QWEN-REAL-05": "移除左上角红色贴纸并补全白色背景",
        "QWEN-REAL-06": "移除右下角红色贴纸并补全桌面",
        "QWEN-REAL-07": "价格数字 99 改为 129",
        "QWEN-REAL-08": "价格数字 2999 改为 3199",
        "QWEN-REAL-09": "价格数字 39 改为 49",
    }
    if case_id in goals:
        return goals[case_id]
    if mode == "background_matting_local":
        return "保留人物，替换为自然浅灰蓝背景"
    return "按固定编辑目标完成局部修改"


def _review_focus(mode: str) -> str:
    return {
        "background_matting_local": "人物五官、发丝和衣物是否被改坏；边缘与背景是否自然",
        "remove_glass_local": "目标是否消失；台面纹理、光影和接缝是否自然",
        "remove_synthetic_sticker_local": "贴纸与红边是否完全消失；周围背景是否自然",
        "edit_chinese_label_local": "目标数字是否正确；标签字体和画面其它区域是否自然",
    }.get(mode, "目标是否完成；未编辑区域和边缘是否可接受")


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_readme(output_dir: Path, rows: list[dict[str, str]]) -> None:
    text = """# Qwen Image 独立审阅包

请由未参与本实现的审阅者查看 `cards/` 中每一张三栏图：原图、局部合成结果、固定编辑范围。

在 `review.csv` 中为每个案例填写：

- `reviewer_id`：匿名标识即可。
- `decision`：`accept`、`reject` 或 `needs_revision`。
- `note`：只写发现的问题，例如“发丝边缘发灰”“贴纸残留红边”“数字错误”。

审阅只判断当前图是否可接受，不推断模型能力，也不修改任何原始证据。`needs_revision` 不能按通过计入验收。

本包包含 %d 个冻结公开样本，不含客户图片、API Key 或临时结果 URL。
""" % len(rows)
    (output_dir / "README.md").write_text(text, encoding="utf-8")


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in (Path(r"C:\Windows\Fonts\segoeui.ttf"), Path(r"C:\Windows\Fonts\arial.ttf")):
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


if __name__ == "__main__":
    main()
