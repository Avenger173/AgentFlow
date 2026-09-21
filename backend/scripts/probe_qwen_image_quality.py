"""在冻结的公开图片夹具上执行 Qwen Image 首轮质量筛选。

默认不联网。``--execute`` 会向已配置的 ``media_image_edit`` 路由提交九次公开样本编辑：
换背景、移除物体和中文数字改字各三次。结果写入忽略目录并保留原始模型输出与本地蒙版合成输出，
但不自动给出内容质量通过结论。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from time import perf_counter

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.media_image_composition import (  # noqa: E402
    compose_local_generated_edit,
    verify_hard_protected_pixels,
)
from app.services.model_gateway import (  # noqa: E402
    ModelGatewayError,
    VisualModelRuntime,
    resolve_visual_model_runtime_for_route,
)
from app.services.qwen_image_edit import (  # noqa: E402
    QwenImageEditInput,
    download_qwen_image_result,
    edit_qwen_image,
)


@dataclass(frozen=True)
class _CaseDefinition:
    case_id: str
    source_case_id: str
    prompt: str
    mode: str
    seed: int
    target_bounds: tuple[int, int, int, int] | None = None
    label_before: str | None = None
    label_after: str | None = None
    label_font_size: int = 54
    mask_expand_px: int = 0
    mask_feather_radius_px: int = 0


_CASES = (
    _CaseDefinition(
        case_id="QWEN-REAL-01",
        source_case_id="MM0-PERSON-01",
        prompt=(
            "将人物身后的灰色背景替换为干净、自然的浅灰蓝摄影棚背景。"
            "人物的脸、头发、胡须、衣物、姿势、构图和光照保持不变，不要改变人物。"
        ),
        mode="background_matting_local",
        seed=202609181,
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-02",
        source_case_id="MM0-PERSON-02",
        prompt=(
            "将人物身后的室内背景替换为干净、自然的浅灰蓝摄影棚背景。"
            "人物的脸、头发、衣物、姿势、构图和光照保持不变，不要改变人物。"
        ),
        mode="background_matting_local",
        seed=202609182,
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-03",
        source_case_id="MM0-PERSON-03",
        prompt=(
            "将人物身后的门口和砖墙背景替换为干净、自然的浅灰蓝摄影棚背景。"
            "人物的脸、头发、胡须、衣物、姿势、构图和光照保持不变，不要改变人物。"
        ),
        mode="background_matting_local",
        seed=202609183,
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-04",
        source_case_id="MM0-PRODUCT-01",
        prompt=(
            "移除画面右上角装有可乐和柠檬的玻璃杯，并以自然的大理石台面和环境光补全该位置。"
            "保留面碗、餐具、编织包和其他区域。"
        ),
        mode="remove_glass_local",
        seed=202609184,
        target_bounds=(790, 0, 1140, 430),
        mask_feather_radius_px=14,
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-05",
        source_case_id="MM0-PRODUCT-02",
        prompt=(
            "移除画面左上角的红色圆形贴纸，并以自然的纯白背景补全该位置。"
            "保留相机、镜头、阴影和其他区域。"
        ),
        mode="remove_synthetic_sticker_local",
        seed=202609185,
        target_bounds=(72, 72, 224, 224),
        mask_expand_px=24,
        mask_feather_radius_px=10,
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-06",
        source_case_id="MM0-PRODUCT-03",
        prompt=(
            "移除画面右下角的红色圆形贴纸，并以自然的浅色桌面背景补全该位置。"
            "保留茶杯、杯碟、茶水和其他区域。"
        ),
        mode="remove_synthetic_sticker_local",
        seed=202609186,
        target_bounds=(1030, 650, 1182, 802),
        mask_expand_px=24,
        mask_feather_radius_px=10,
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-07",
        source_case_id="MM0-PRODUCT-01",
        prompt=(
            "将画面左下白色价格标签中的数字“99”改为“129”，保留“新品”和“元”字、"
            "标签位置、深蓝字体、照片构图及其他区域不变。"
        ),
        mode="edit_chinese_label_local",
        seed=202609187,
        target_bounds=(42, 696, 466, 812),
        label_before="新品 99 元",
        label_after="新品 129 元",
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-08",
        source_case_id="MM0-PRODUCT-02",
        prompt=(
            "将画面左上白色价格标签中的数字“2999”改为“3199”，保留“新品”和“元”字、"
            "标签位置、深蓝字体、相机构图及其他区域不变。"
        ),
        mode="edit_chinese_label_local",
        seed=202609188,
        target_bounds=(36, 36, 564, 164),
        label_before="新品 2999 元",
        label_after="新品 3199 元",
        label_font_size=46,
    ),
    _CaseDefinition(
        case_id="QWEN-REAL-09",
        source_case_id="MM0-PRODUCT-03",
        prompt=(
            "将画面右下白色价格标签中的数字“39”改为“49”，保留“新品”和“元”字、"
            "标签位置、深蓝字体、茶杯构图及其他区域不变。"
        ),
        mode="edit_chinese_label_local",
        seed=202609189,
        target_bounds=(854, 680, 1230, 812),
        label_before="新品 39 元",
        label_after="新品 49 元",
        label_font_size=44,
    ),
)

_STRICT_REMOVAL_CORRECTION = (
    " 必须完全清除贴纸本体、红色外圈、文字、圆形边界和阴影，不得留下任何圆形或红色痕迹；"
    "只使用周围连续的原有背景自然补全。"
)


async def _run(
    *,
    fixture_dir: Path,
    matting_evidence_dir: Path,
    output_dir: Path,
    definitions: tuple[_CaseDefinition, ...],
    minimum_interval_seconds: float,
    model_override: str,
    prompt_variant: str,
) -> dict[str, object]:
    fixture_manifest = _load_fixture_manifest(fixture_dir)
    matting_alpha_paths = _load_matting_alpha_paths(
        matting_evidence_dir=matting_evidence_dir,
        fixture_manifest=fixture_manifest,
    )
    resolved_runtime = resolve_visual_model_runtime_for_route("media_image_edit", validate=True).runtime
    if not isinstance(resolved_runtime, VisualModelRuntime):
        raise RuntimeError("media_image_edit 路由没有解析为 Qwen Image 运行时。")
    runtime = replace(resolved_runtime, model=model_override) if model_override else resolved_runtime
    output_dir.mkdir(parents=True, exist_ok=False)
    cases: list[dict[str, object]] = []
    previous_request_started: float | None = None
    for definition in definitions:
        if previous_request_started is not None:
            remaining_delay = minimum_interval_seconds - (perf_counter() - previous_request_started)
            if remaining_delay > 0:
                await asyncio.sleep(remaining_delay)
        previous_request_started = perf_counter()
        try:
            cases.append(
                await _run_case(
                    definition=definition,
                    fixture_manifest=fixture_manifest,
                    fixture_dir=fixture_dir,
                    matting_alpha_paths=matting_alpha_paths,
                    runtime=runtime,
                    output_dir=output_dir,
                )
            )
        except (ModelGatewayError, RuntimeError, OSError, ValueError) as exc:
            cases.append(
                {
                    "case_id": definition.case_id,
                    "mode": definition.mode,
                    "status": "failed",
                    "error": _safe_error(exc),
                }
            )
    manifest = {
        "probe": "qwen_image_public_fixture_quality_v2",
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fixture_set": fixture_manifest["fixture_set"],
        "matting_evidence_dir": matting_evidence_dir.name,
        "provider_route": "media_image_edit",
        "requested_model": runtime.model,
        "prompt_variant": prompt_variant,
        "minimum_interval_seconds": minimum_interval_seconds,
        "requested_case_ids": [definition.case_id for definition in definitions],
        "cases": cases,
        "quality_claim": "none; inspect raw and locally-composed output before MODEL-02 is scored",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_review_template(output_dir=output_dir, cases=cases)
    return manifest


async def _run_case(
    *,
    definition: _CaseDefinition,
    fixture_manifest: dict[str, object],
    fixture_dir: Path,
    matting_alpha_paths: dict[str, Path],
    runtime: VisualModelRuntime,
    output_dir: Path,
) -> dict[str, object]:
    source = _load_source_image(
        fixture_manifest=fixture_manifest,
        fixture_dir=fixture_dir,
        source_case_id=definition.source_case_id,
    )
    input_image, edit_mask = _prepare_case_image(
        source=source,
        definition=definition,
        matting_alpha_path=matting_alpha_paths.get(definition.source_case_id),
    )
    if edit_mask is not None and definition.mask_feather_radius_px:
        edit_mask = edit_mask.filter(ImageFilter.GaussianBlur(radius=definition.mask_feather_radius_px))
    input_bytes = _png_bytes(input_image)
    input_path = output_dir / f"{definition.case_id.lower()}_input.png"
    input_path.write_bytes(input_bytes)
    if edit_mask is not None:
        edit_mask.save(output_dir / f"{definition.case_id.lower()}_mask.png")
    width, height = input_image.size
    started = perf_counter()
    provider_result = await edit_qwen_image(
        images=[QwenImageEditInput(image_bytes=input_bytes, mime_type="image/png")],
        prompt=definition.prompt,
        output_count=1,
        output_size=f"{width}*{height}",
        prompt_extend=False,
        watermark=False,
        seed=definition.seed,
        runtime=runtime,
    )
    downloaded = await download_qwen_image_result(result_url=provider_result.output_urls[0])
    elapsed_ms = max(0, round((perf_counter() - started) * 1000))
    raw_path = output_dir / f"{definition.case_id.lower()}_provider_raw.png"
    raw_path.write_bytes(downloaded.image_bytes)
    record: dict[str, object] = {
        "case_id": definition.case_id,
        "source_case_id": definition.source_case_id,
        "mode": definition.mode,
        "status": "completed_pending_review",
        "prompt": definition.prompt,
        "seed": definition.seed,
        "input_file": input_path.name,
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "provider_raw_file": raw_path.name,
        "provider_raw_sha256": hashlib.sha256(downloaded.image_bytes).hexdigest(),
        "provider_model": provider_result.model,
        "provider_request_id": provider_result.request_id,
        "provider_image_count": provider_result.image_count,
        "provider_size": [downloaded.width, downloaded.height],
        "provider_usage": {
            "reported": provider_result.usage_reported,
            "input_image_count": provider_result.input_image_count,
            "output_image_count": provider_result.output_image_count,
            "input_image_type": provider_result.input_image_type,
            "output_image_type": provider_result.output_image_type,
            "output_width": provider_result.width,
            "output_height": provider_result.height,
        },
        "elapsed_ms": elapsed_ms,
        "mask_expand_px": definition.mask_expand_px,
        "mask_feather_radius_px": definition.mask_feather_radius_px,
        "result_url_host_sha256": hashlib.sha256(provider_result.output_urls[0].split("?", 1)[0].encode("utf-8")).hexdigest(),
    }
    if downloaded.image_format != "PNG":
        record["status"] = "failed"
        record["error"] = "Qwen Image 返回格式不是预期 PNG。"
        return record
    with Image.open(BytesIO(downloaded.image_bytes)) as raw_image:
        raw = raw_image.convert("RGBA")
    if raw.size != input_image.size:
        record["status"] = "failed"
        record["error"] = "Qwen Image 返回尺寸与固定输入尺寸不一致，局部结果不会被静默缩放或合成。"
        return record
    if edit_mask is None:
        raise RuntimeError("冻结的 Qwen 质量探针必须有可验证的局部编辑蒙版。")
    composition_fields = _compose_local_result(
        case_id=definition.case_id,
        input_image=input_image,
        raw_image=raw,
        edit_mask=edit_mask,
        output_dir=output_dir,
    )
    record["mask_file"] = f"{definition.case_id.lower()}_mask.png"
    record.update(composition_fields)
    protection = composition_fields["composed_hard_protection"]
    if not isinstance(protection, dict) or protection.get("passed") is not True:  # pragma: no cover
        record["status"] = "failed"
        record["error"] = "本地蒙版合成未保持保护区像素。"
    return record


def _compose_local_result(
    *,
    case_id: str,
    input_image: Image.Image,
    raw_image: Image.Image,
    edit_mask: Image.Image,
    output_dir: Path,
) -> dict[str, object]:
    raw_protection = verify_hard_protected_pixels(
        source_image=input_image,
        result_image=raw_image,
        edit_mask=edit_mask,
    )
    composition = compose_local_generated_edit(
        source_image=input_image,
        generated_image=raw_image,
        edit_mask=edit_mask,
    )
    composed_path = output_dir / f"{case_id.lower()}_mask_composed.png"
    composition.image.save(composed_path)
    composed_protection = verify_hard_protected_pixels(
        source_image=input_image,
        result_image=composition.image,
        edit_mask=edit_mask,
    )
    return {
        "mask_composed_file": composed_path.name,
        "raw_unmasked_change": {
            "protected_pixel_count": raw_protection.protected_pixel_count,
            "changed_pixel_count": raw_protection.changed_pixel_count,
            "max_channel_delta": raw_protection.max_channel_delta,
        },
        "composed_hard_protection": {
            "passed": composed_protection.passed,
            "protected_pixel_count": composed_protection.protected_pixel_count,
            "changed_pixel_count": composed_protection.changed_pixel_count,
            "max_channel_delta": composed_protection.max_channel_delta,
        },
    }


def _recompose_provider_run(
    *,
    fixture_dir: Path,
    matting_evidence_dir: Path,
    provider_run_dir: Path,
    output_dir: Path,
    definitions: tuple[_CaseDefinition, ...],
) -> dict[str, object]:
    fixture_manifest = _load_fixture_manifest(fixture_dir)
    matting_alpha_paths = _load_matting_alpha_paths(
        matting_evidence_dir=matting_evidence_dir,
        fixture_manifest=fixture_manifest,
    )
    provider_manifest_path = provider_run_dir / "manifest.json"
    if not provider_manifest_path.is_file():
        raise RuntimeError("待重合成的 Provider 运行目录缺少 manifest.json。")
    try:
        provider_manifest = json.loads(provider_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("待重合成的 Provider manifest 无法读取。") from exc
    if not isinstance(provider_manifest, dict) or provider_manifest.get("fixture_set") != fixture_manifest.get("fixture_set"):
        raise RuntimeError("待重合成的 Provider 运行记录与当前公开夹具版本不一致。")
    provider_cases = {
        str(item.get("case_id")): item
        for item in provider_manifest.get("cases", [])
        if isinstance(item, dict) and item.get("status") == "completed_pending_review"
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    cases: list[dict[str, object]] = []
    for definition in definitions:
        provider_case = provider_cases.get(definition.case_id)
        if not isinstance(provider_case, dict):
            raise RuntimeError(f"待重合成的 Provider 运行记录缺少已完成用例 {definition.case_id}。")
        source = _load_source_image(
            fixture_manifest=fixture_manifest,
            fixture_dir=fixture_dir,
            source_case_id=definition.source_case_id,
        )
        input_image, edit_mask = _prepare_case_image(
            source=source,
            definition=definition,
            matting_alpha_path=matting_alpha_paths.get(definition.source_case_id),
        )
        if edit_mask is None:
            raise RuntimeError(f"{definition.case_id} 未生成可验证蒙版。")
        if definition.mask_feather_radius_px:
            edit_mask = edit_mask.filter(ImageFilter.GaussianBlur(radius=definition.mask_feather_radius_px))
        input_bytes = _png_bytes(input_image)
        if provider_case.get("input_sha256") != hashlib.sha256(input_bytes).hexdigest():
            raise RuntimeError(f"{definition.case_id} 的重合成输入与 Provider 原始请求不一致。")
        raw_name = str(provider_case.get("provider_raw_file") or "")
        raw_path = (provider_run_dir / raw_name).resolve()
        if not raw_name or raw_path.parent != provider_run_dir or not raw_path.is_file():
            raise RuntimeError(f"{definition.case_id} 的 Provider 原始结果引用无效。")
        raw_bytes = raw_path.read_bytes()
        if provider_case.get("provider_raw_sha256") != hashlib.sha256(raw_bytes).hexdigest():
            raise RuntimeError(f"{definition.case_id} 的 Provider 原始结果哈希不匹配。")
        with Image.open(BytesIO(raw_bytes)) as opened:
            if str(opened.format or "").upper() != "PNG":
                raise RuntimeError(f"{definition.case_id} 的 Provider 原始结果不是 PNG。")
            raw = opened.convert("RGBA")
        if raw.size != input_image.size:
            raise RuntimeError(f"{definition.case_id} 的 Provider 原始结果尺寸不匹配。")
        input_name = f"{definition.case_id.lower()}_input.png"
        mask_name = f"{definition.case_id.lower()}_mask.png"
        raw_output_name = f"{definition.case_id.lower()}_provider_raw.png"
        (output_dir / input_name).write_bytes(input_bytes)
        edit_mask.save(output_dir / mask_name)
        (output_dir / raw_output_name).write_bytes(raw_bytes)
        record: dict[str, object] = {
            "case_id": definition.case_id,
            "source_case_id": definition.source_case_id,
            "mode": definition.mode,
            "status": "completed_pending_review",
            "prompt": provider_case.get("prompt"),
            "seed": definition.seed,
            "input_file": input_name,
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "provider_raw_file": raw_output_name,
            "provider_raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "provider_model": provider_case.get("provider_model"),
            "provider_request_id": provider_case.get("provider_request_id"),
            "provider_usage": provider_case.get("provider_usage"),
            "provider_size": list(raw.size),
            "mask_expand_px": definition.mask_expand_px,
            "mask_feather_radius_px": definition.mask_feather_radius_px,
            "recomposed_from_provider_run": provider_run_dir.name,
        }
        record["mask_file"] = mask_name
        record.update(
            _compose_local_result(
                case_id=definition.case_id,
                input_image=input_image,
                raw_image=raw,
                edit_mask=edit_mask,
                output_dir=output_dir,
            )
        )
        protection = record["composed_hard_protection"]
        if not isinstance(protection, dict) or protection.get("passed") is not True:  # pragma: no cover
            record["status"] = "failed"
            record["error"] = "离线重合成未保持保护区像素。"
        cases.append(record)
    manifest = {
        "probe": "qwen_image_local_recomposition_v1",
        "executed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fixture_set": fixture_manifest["fixture_set"],
        "matting_evidence_dir": matting_evidence_dir.name,
        "source_provider_run": provider_run_dir.name,
        "requested_case_ids": [definition.case_id for definition in definitions],
        "network_calls": 0,
        "cases": cases,
        "quality_claim": "none; this run only replays local composition over verified prior Provider output",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_review_template(output_dir=output_dir, cases=cases)
    return manifest


def _load_fixture_manifest(fixture_dir: Path) -> dict[str, object]:
    manifest_path = fixture_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("fixture-dir 缺少公开夹具 manifest.json。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("公开夹具 manifest 无法读取。") from exc
    if not isinstance(manifest, dict) or manifest.get("fixture_set") != "agentflow-mm0-public-image-fixtures-v2":
        raise RuntimeError("fixture-dir 不是已冻结的 MM-0 公开夹具集。")
    return manifest


def _load_matting_alpha_paths(*, matting_evidence_dir: Path, fixture_manifest: dict[str, object]) -> dict[str, Path]:
    manifest_path = matting_evidence_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("matting-evidence-dir 缺少 manifest.json。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Lite Matting evidence manifest 无法读取。") from exc
    if not isinstance(manifest, dict) or manifest.get("fixture_set") != fixture_manifest.get("fixture_set"):
        raise RuntimeError("Lite Matting evidence 与当前公开夹具版本不一致。")
    if manifest.get("model_id") != "BiRefNet_lite-matting-epoch_110":
        raise RuntimeError("背景编辑只能引用已冻结的 Lite Matting evidence。")
    source_hashes = {
        str(item.get("case_id")): str(item.get("sha256"))
        for item in fixture_manifest.get("fixtures", [])
        if isinstance(item, dict)
    }
    paths: dict[str, Path] = {}
    for item in manifest.get("cases", []):
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or "")
        alpha_name = str(item.get("alpha_file") or "")
        alpha_path = (matting_evidence_dir / alpha_name).resolve()
        if (
            case_id not in source_hashes
            or item.get("source_sha256") != source_hashes[case_id]
            or not alpha_name
            or alpha_path.parent != matting_evidence_dir
            or not alpha_path.is_file()
        ):
            raise RuntimeError(f"Lite Matting evidence 的 {case_id or 'unknown'} alpha 引用无效。")
        paths[case_id] = alpha_path
    required = {definition.source_case_id for definition in _CASES if definition.mode == "background_matting_local"}
    if not required.issubset(paths):
        raise RuntimeError("Lite Matting evidence 缺少换背景样本的 alpha。")
    return paths


def _load_source_image(*, fixture_manifest: dict[str, object], fixture_dir: Path, source_case_id: str) -> Image.Image:
    fixtures = fixture_manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise RuntimeError("公开夹具 manifest 缺少 fixtures。")
    fixture = next(
        (item for item in fixtures if isinstance(item, dict) and item.get("case_id") == source_case_id),
        None,
    )
    if not isinstance(fixture, dict):
        raise RuntimeError(f"公开夹具集中找不到 {source_case_id}。")
    filename = str(fixture.get("file") or "").strip()
    source_path = (fixture_dir / filename).resolve()
    if not filename or source_path.parent != fixture_dir or not source_path.is_file():
        raise RuntimeError(f"{source_case_id} 的夹具文件引用无效。")
    content = source_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != fixture.get("sha256"):
        raise RuntimeError(f"{source_case_id} 的夹具哈希不匹配。")
    with Image.open(BytesIO(content)) as image:
        return image.convert("RGB")


def _prepare_case_image(
    *,
    source: Image.Image,
    definition: _CaseDefinition,
    matting_alpha_path: Path | None,
) -> tuple[Image.Image, Image.Image | None]:
    base = _resize_to_multiple_of_16(source)
    if definition.mode == "background_matting_local":
        if matting_alpha_path is None:
            raise RuntimeError("换背景探针缺少匹配的 Lite Matting alpha。")
        with Image.open(matting_alpha_path) as opened:
            alpha = opened.convert("L")
        if alpha.size != base.size:
            alpha = alpha.resize(base.size, Image.Resampling.LANCZOS)
        return base, ImageOps.invert(alpha)
    if definition.mode == "remove_glass_local":
        bounds = _require_target_bounds(definition, base.size)
        mask = Image.new("L", base.size, color=0)
        # 公开商品夹具右上方的玻璃杯；这是显式固定的人工选区，不冒充语义分割结果。
        ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=28, fill=255)
        return base, mask
    if definition.mode == "remove_synthetic_sticker_local":
        bounds = _require_target_bounds(definition, base.size)
        mask_bounds = _expand_bounds(bounds, base.size, definition.mask_expand_px)
        labelled = base.copy()
        draw = ImageDraw.Draw(labelled)
        draw.ellipse(bounds, fill=(216, 48, 45), outline=(152, 27, 27), width=4)
        center_x = (bounds[0] + bounds[2]) // 2
        center_y = (bounds[1] + bounds[3]) // 2
        font = _load_chinese_font(max(18, min(bounds[2] - bounds[0], bounds[3] - bounds[1]) // 4))
        draw.text((center_x, center_y), "贴纸", font=font, fill=(255, 255, 255), anchor="mm")
        mask = Image.new("L", base.size, color=0)
        ImageDraw.Draw(mask).ellipse(mask_bounds, fill=255)
        return labelled, mask
    if definition.mode == "edit_chinese_label_local":
        bounds = _require_target_bounds(definition, base.size)
        before = str(definition.label_before or "").strip()
        after = str(definition.label_after or "").strip()
        if not before or not after:
            raise RuntimeError("中文改字探针缺少冻结的原文字和目标文字。")
        labelled = base.copy()
        draw = ImageDraw.Draw(labelled)
        draw.rounded_rectangle(bounds, radius=18, fill=(250, 250, 248), outline=(205, 210, 214), width=3)
        font = _load_chinese_font(definition.label_font_size)
        text_x = bounds[0] + 24
        text_y = bounds[1] + (bounds[3] - bounds[1]) // 2
        draw.text((text_x, text_y), before, font=font, fill=(22, 66, 128), anchor="lm")
        mask = Image.new("L", base.size, color=0)
        ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=18, fill=255)
        return labelled, mask
    raise RuntimeError(f"未登记的 Qwen 图片质量探针模式：{definition.mode}")


def _require_target_bounds(definition: _CaseDefinition, size: tuple[int, int]) -> tuple[int, int, int, int]:
    bounds = definition.target_bounds
    if bounds is None:
        raise RuntimeError(f"{definition.case_id} 缺少固定目标区域。")
    left, top, right, bottom = bounds
    width, height = size
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise RuntimeError(f"{definition.case_id} 的固定目标区域超出图片尺寸。")
    return bounds


def _expand_bounds(
    bounds: tuple[int, int, int, int],
    size: tuple[int, int],
    padding: int,
) -> tuple[int, int, int, int]:
    if padding < 0:
        raise RuntimeError("局部编辑蒙版扩张像素不能为负数。")
    left, top, right, bottom = bounds
    width, height = size
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def _resize_to_multiple_of_16(image: Image.Image) -> Image.Image:
    width, height = image.size
    target_width = max(512, width - width % 16)
    target_height = max(512, height - height % 16)
    if (target_width, target_height) == image.size:
        return image
    return image.resize((target_width, target_height), Image.Resampling.LANCZOS)


def _load_chinese_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    raise RuntimeError("当前 Windows 环境缺少微软雅黑字体，无法构造中文数字编辑夹具。")


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_review_template(*, output_dir: Path, cases: list[dict[str, object]]) -> None:
    lines = [
        "# Qwen Image 首轮公开夹具复核",
        "",
        "本文件只给出复核项；`completed_pending_review` 不代表 MODEL-02 通过。",
        "",
    ]
    for case in cases:
        case_id = str(case.get("case_id") or "unknown")
        lines.extend(
            [
                f"## {case_id}",
                f"- 状态：{case.get('status')}",
                f"- 输入：{case.get('input_file', 'n/a')}",
                f"- 模型原始结果：{case.get('provider_raw_file', 'n/a')}",
                f"- 蒙版合成结果：{case.get('mask_composed_file', 'n/a')}",
                "- 人工复核：目标是否完成；主体/物体是否自然；是否出现结构、文字或边缘伪影；是否可接受一次修正。",
                "",
            ]
        )
    (output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240]


def _validate_frozen_inputs(*, fixture_dir: Path, matting_evidence_dir: Path) -> int:
    fixture_manifest = _load_fixture_manifest(fixture_dir)
    matting_alpha_paths = _load_matting_alpha_paths(
        matting_evidence_dir=matting_evidence_dir,
        fixture_manifest=fixture_manifest,
    )
    for definition in _CASES:
        source = _load_source_image(
            fixture_manifest=fixture_manifest,
            fixture_dir=fixture_dir,
            source_case_id=definition.source_case_id,
        )
        input_image, mask = _prepare_case_image(
            source=source,
            definition=definition,
            matting_alpha_path=matting_alpha_paths.get(definition.source_case_id),
        )
        if mask is None or mask.size != input_image.size:
            raise RuntimeError(f"{definition.case_id} 未生成同尺寸可验证蒙版。")
    return len(_CASES)


def _select_case_definitions(case_ids: list[str] | None) -> tuple[_CaseDefinition, ...]:
    if not case_ids:
        return _CASES
    by_id = {definition.case_id: definition for definition in _CASES}
    selected: list[_CaseDefinition] = []
    for case_id in case_ids:
        definition = by_id.get(case_id)
        if definition is None:
            raise RuntimeError(f"未登记的 Qwen 图片质量探针用例：{case_id}")
        if definition not in selected:
            selected.append(definition)
    return tuple(selected)


def _apply_strict_removal_correction(
    definitions: tuple[_CaseDefinition, ...],
) -> tuple[_CaseDefinition, ...]:
    return tuple(
        replace(definition, prompt=definition.prompt + _STRICT_REMOVAL_CORRECTION)
        if definition.mode == "remove_synthetic_sticker_local"
        else definition
        for definition in definitions
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True, help="已准备的 MM-0 公开夹具目录")
    parser.add_argument(
        "--matting-evidence-dir",
        type=Path,
        required=True,
        help="与夹具版本一致的 Lite Matting 探针证据目录，用于换背景的本地保护蒙版",
    )
    parser.add_argument("--validate-inputs", action="store_true", help="仅验证九个冻结用例、来源哈希和本地蒙版，不调用 Qwen")
    parser.add_argument("--execute", action="store_true", help="明确执行九次公开图片 Qwen 编辑请求")
    parser.add_argument(
        "--case-id",
        action="append",
        choices=[definition.case_id for definition in _CASES],
        help="只重跑指定冻结用例；可重复指定，避免限流后重发已完成的请求",
    )
    parser.add_argument(
        "--minimum-interval-seconds",
        type=float,
        default=35.0,
        help="相邻 Provider 请求起点的最小间隔；默认 35 秒，避免 DashScope 图片编辑限流",
    )
    parser.add_argument(
        "--model",
        default="",
        help="仅覆盖本次探针的 Qwen 图片模型 ID，不修改保存的 Provider 或任务路由配置",
    )
    parser.add_argument(
        "--strict-removal-correction",
        action="store_true",
        help="仅对合成贴纸移除追加一次显式残影约束；用于记录一次受限修正，不覆盖首轮结果",
    )
    parser.add_argument(
        "--recompose-provider-run",
        type=Path,
        help="从已验证的 Provider 原始结果离线重放本地蒙版合成；不发出模型或网络请求",
    )
    parser.add_argument("--output-dir", type=Path, help="默认写入忽略的 data/media_evaluations 目录")
    args = parser.parse_args()
    fixture_dir = args.fixture_dir.resolve()
    matting_evidence_dir = args.matting_evidence_dir.resolve()
    if args.minimum_interval_seconds < 0 or args.minimum_interval_seconds > 300:
        parser.error("--minimum-interval-seconds 必须在 0 到 300 之间。")
    model_override = str(args.model or "").strip()
    if model_override and not model_override.casefold().startswith("qwen-image"):
        parser.error("--model 只能是已接入的 qwen-image* 图片编辑模型 ID。")
    if args.validate_inputs:
        try:
            case_count = _validate_frozen_inputs(
                fixture_dir=fixture_dir,
                matting_evidence_dir=matting_evidence_dir,
            )
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"Qwen Image quality input validation failed: {_safe_error(exc)}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(json.dumps({"ok": True, "case_count": case_count, "network_calls": 0}, ensure_ascii=False))
        return
    if args.recompose_provider_run is not None:
        if args.execute or args.strict_removal_correction or model_override:
            parser.error("--recompose-provider-run 不能与 --execute、--model 或 --strict-removal-correction 同用。")
        try:
            definitions = _select_case_definitions(args.case_id)
        except RuntimeError as exc:
            parser.error(str(exc))
        output_dir = args.output_dir or (
            PROJECT_ROOT / "data" / "media_evaluations" / ("qwen_image_recompose_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        )
        try:
            manifest = _recompose_provider_run(
                fixture_dir=fixture_dir,
                matting_evidence_dir=matting_evidence_dir,
                provider_run_dir=args.recompose_provider_run.resolve(),
                output_dir=output_dir.resolve(),
                definitions=definitions,
            )
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"Qwen Image local recomposition failed: {_safe_error(exc)}", file=sys.stderr)
            raise SystemExit(1) from exc
        failed = sum(1 for case in manifest["cases"] if case.get("status") == "failed")
        print(json.dumps({"ok": failed == 0, "output_dir": str(output_dir), "failed_case_count": failed, "network_calls": 0}, ensure_ascii=False))
        if failed:
            raise SystemExit(1)
        return
    if not args.execute:
        print("Dry run only. Pass --validate-inputs or --execute to continue.")
        return
    output_dir = args.output_dir or (
        PROJECT_ROOT / "data" / "media_evaluations" / ("qwen_image_quality_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    )
    try:
        definitions = _select_case_definitions(args.case_id)
    except RuntimeError as exc:
        parser.error(str(exc))
    if args.strict_removal_correction:
        if not any(definition.mode == "remove_synthetic_sticker_local" for definition in definitions):
            parser.error("--strict-removal-correction 只适用于合成贴纸移除用例。")
        definitions = _apply_strict_removal_correction(definitions)
    try:
        manifest = asyncio.run(
            _run(
                fixture_dir=fixture_dir,
                matting_evidence_dir=matting_evidence_dir,
                output_dir=output_dir.resolve(),
                definitions=definitions,
                minimum_interval_seconds=float(args.minimum_interval_seconds),
                model_override=model_override,
                prompt_variant="strict_removal_correction" if args.strict_removal_correction else "initial",
            )
        )
    except (ModelGatewayError, RuntimeError, OSError, ValueError) as exc:
        print(f"Qwen Image quality probe setup failed: {_safe_error(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc
    failed = sum(1 for case in manifest["cases"] if case.get("status") == "failed")
    print(json.dumps({"ok": failed == 0, "output_dir": str(output_dir), "failed_case_count": failed}, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
