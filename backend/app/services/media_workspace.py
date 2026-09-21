"""图片工作区的受控副本、版本记录和可回读导出。

这个模块不调用模型，也不接受客户端提供的绝对路径。图片原件、修订版本和导出
文件分别保存在三个受控位置，避免编辑链路覆盖用户原始素材。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from hashlib import sha256
import io
import json
from pathlib import Path
import re
from shutil import copyfile, rmtree
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from PIL import Image, ImageChops, ImageEnhance, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.database.media_workspace_repository import (
    MediaWorkspaceProjectNotFoundError,
    load_media_workspace_manifest,
    save_media_workspace_manifest,
)
from app.schemas.media_workspace import (
    MediaAssetInfo,
    MediaAssetRevisionListResponse,
    MediaImageExportInfo,
    MediaImageRevisionInfo,
    MediaLayerStackItem,
    MediaLayerStackResponse,
    MediaProjectDetailResponse,
    MediaProjectInfo,
)


MAX_IMAGE_BYTES = 20_000_000
MAX_IMAGE_PIXELS = 40_000_000
_PROJECT_ID_PATTERN = re.compile(r"^mp_[0-9a-f]{16}$")
_ASSET_ID_PATTERN = re.compile(r"^ma_[0-9a-f]{16}$")
_REVISION_ID_PATTERN = re.compile(r"^mr_[0-9a-f]{16}$")
_MASK_ID_PATTERN = re.compile(r"^mm_[0-9a-f]{16}$")
_LAYER_ID_PATTERN = re.compile(r"^ml_[0-9a-f]{16}$")
_EXPORT_ID_PATTERN = re.compile(r"^me_[0-9a-f]{16}$")
_MEDIA_EXPORT_TASK_ID_PATTERN = re.compile(r"^task_media_export_[0-9a-f]{12}$")
_MEDIA_EDIT_TASK_ID_PATTERN = re.compile(r"^task_media_edit_[0-9a-f]{12}$")
_SUPPORTED_IMAGE_TYPES = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}
_OPERATIONS = {
    "rotate_left",
    "rotate_right",
    "flip_horizontal",
    "grayscale",
    "adjust_color",
    "crop",
    "resize",
    "apply_rect_mask",
    "composite_raster_layer",
    "recompose_raster_layers",
}
_PROJECT_LOCKS: dict[str, RLock] = {}
_PROJECT_LOCKS_GUARD = Lock()


class MediaWorkspaceError(ValueError):
    """多媒体工作区的可展示业务错误。"""


class MediaWorkspaceConflictError(MediaWorkspaceError):
    """The client acted on a revision that has since been superseded."""


def media_workspace_root(*, root_dir: Path | None = None) -> Path:
    return (root_dir if root_dir is not None else settings.media_workspace_dir).resolve()


def media_export_root(*, export_root: Path | None = None) -> Path:
    return (export_root if export_root is not None else settings.media_export_output_dir).resolve()


def create_media_project(*, title: str, root_dir: Path | None = None) -> MediaProjectInfo:
    safe_title = _safe_title(title)
    root = media_workspace_root(root_dir=root_dir)
    projects_root = root / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    project_id = _new_id("mp")
    project_dir = projects_root / project_id
    project_dir.mkdir(parents=False, exist_ok=False)
    (project_dir / "sources").mkdir()
    (project_dir / "revisions").mkdir()
    (project_dir / "masks").mkdir()
    now = _utc_now()
    manifest = {
        "schema_version": 7,
        "project_id": project_id,
        "title": safe_title,
        "created_at": now,
        "updated_at": now,
        "assets": [],
        "revisions": [],
        "exports": [],
        "history_events": [],
        "masks": [],
        "layers": [],
    }
    try:
        _write_manifest(project_dir, manifest)
    except Exception:
        rmtree(project_dir, ignore_errors=True)
        raise
    return _project_info(manifest)


def list_media_projects(*, root_dir: Path | None = None) -> list[MediaProjectInfo]:
    workspace_root = media_workspace_root(root_dir=root_dir)
    projects_root = workspace_root / "projects"
    if not projects_root.exists():
        return []
    projects: list[MediaProjectInfo] = []
    for candidate in projects_root.iterdir():
        if not candidate.is_dir() or not _PROJECT_ID_PATTERN.fullmatch(candidate.name):
            continue
        try:
            _, manifest = _project_manifest(candidate.name, root_dir=workspace_root)
            projects.append(_project_info(manifest))
        except MediaWorkspaceError:
            continue
    return sorted(projects, key=lambda item: item.updated_at, reverse=True)


def get_media_project(
    project_id: str,
    *,
    root_dir: Path | None = None,
) -> MediaProjectDetailResponse:
    project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)
    _ = project_dir
    return MediaProjectDetailResponse(
        project=_project_info(manifest),
        assets=[_asset_info(asset, manifest) for asset in manifest["assets"]],
    )


def import_media_image_base64(
    *,
    project_id: str,
    filename: str,
    content_base64: str,
    root_dir: Path | None = None,
) -> MediaAssetInfo:
    with _project_write_lock(project_id):
        return _import_media_image_base64_locked(
            project_id=project_id,
            filename=filename,
            content_base64=content_base64,
            root_dir=root_dir,
        )


def _import_media_image_base64_locked(
    *,
    project_id: str,
    filename: str,
    content_base64: str,
    root_dir: Path | None,
) -> MediaAssetInfo:
    safe_name = _safe_filename(filename)
    raw_bytes = _decode_base64(content_base64)
    source_image, source_suffix, mime_type = _decode_supported_image(raw_bytes)
    normalized = _normalize_image(source_image)
    project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)

    asset_id = _new_id("ma")
    revision_id = _new_id("mr")
    source_relative = f"sources/{asset_id}{source_suffix}"
    revision_relative = f"revisions/{revision_id}.png"
    source_path = _resolve_project_file(project_dir, source_relative)
    revision_path = _resolve_project_file(project_dir, revision_relative)
    _atomic_write_bytes(source_path, raw_bytes)
    try:
        _atomic_save_png(normalized, revision_path)
        _verify_png(revision_path)
    except Exception:
        source_path.unlink(missing_ok=True)
        revision_path.unlink(missing_ok=True)
        raise

    now = _utc_now()
    asset = {
        "asset_id": asset_id,
        "name": safe_name,
        "source_file": source_relative,
        "source_sha256": _sha256_bytes(raw_bytes),
        "mime_type": mime_type,
        "width": normalized.width,
        "height": normalized.height,
        "size_bytes": len(raw_bytes),
        "created_at": now,
        "current_revision_id": revision_id,
        "undo_revision_ids": [],
        "redo_revision_ids": [],
    }
    revision = _revision_record(
        revision_id=revision_id,
        asset_id=asset_id,
        parent_revision_id=None,
        operation="import",
        parameters={},
        relative_file=revision_relative,
        path=revision_path,
        created_at=now,
    )
    manifest["assets"].append(asset)
    manifest["revisions"].append(revision)
    _touch_manifest(manifest)
    try:
        _write_manifest(project_dir, manifest)
    except Exception:
        source_path.unlink(missing_ok=True)
        revision_path.unlink(missing_ok=True)
        raise
    return _asset_info(asset, manifest)


def list_media_asset_revisions(
    *,
    project_id: str,
    asset_id: str,
    root_dir: Path | None = None,
) -> MediaAssetRevisionListResponse:
    _, manifest = _project_manifest(project_id, root_dir=root_dir)
    asset = _find_asset(manifest, asset_id)
    return _asset_revision_list(asset, manifest)


def get_media_image_layer_stack(
    *,
    project_id: str,
    asset_id: str,
    revision_id: str,
    root_dir: Path | None = None,
) -> MediaLayerStackResponse:
    _, manifest = _project_manifest(project_id, root_dir=root_dir)
    asset = _find_asset(manifest, asset_id)
    revision = _find_revision_for_asset(manifest, revision_id, asset_id)
    composition_root_revision_id = revision.get("composition_root_revision_id")
    layer_stack = revision.get("layer_stack") or []
    if composition_root_revision_id is None or not layer_stack:
        return MediaLayerStackResponse(
            asset_id=asset_id,
            revision_id=revision_id,
            editable=False,
            layers=[],
        )
    layer_by_id = {item["layer_id"]: item for item in manifest["layers"]}
    items: list[MediaLayerStackItem] = []
    for state in layer_stack:
        layer = layer_by_id.get(state["layer_id"])
        if layer is None or layer["asset_id"] != asset_id:
            raise MediaWorkspaceError("当前图片图层状态不可读取。")
        source_asset = _find_asset(manifest, layer["source_asset_id"])
        parameters = layer["parameters"]
        items.append(
            MediaLayerStackItem(
                layer_id=layer["layer_id"],
                source_asset_id=source_asset["asset_id"],
                source_name=source_asset["name"],
                visible=state["visible"],
                x=parameters["x"],
                y=parameters["y"],
                opacity=parameters["opacity"],
            )
        )
    return MediaLayerStackResponse(
        asset_id=asset_id,
        revision_id=revision_id,
        composition_root_revision_id=composition_root_revision_id,
        editable=asset["current_revision_id"] == revision_id,
        layers=items,
    )


def create_media_image_revision(
    *,
    project_id: str,
    asset_id: str,
    base_revision_id: str,
    operation: str,
    parameters: dict[str, Any] | None = None,
    layer_source_asset_id: str | None = None,
    task_id: str | None = None,
    root_dir: Path | None = None,
) -> MediaImageRevisionInfo:
    with _project_write_lock(project_id):
        return _create_media_image_revision_locked(
            project_id=project_id,
            asset_id=asset_id,
            base_revision_id=base_revision_id,
            operation=operation,
            parameters=parameters,
            layer_source_asset_id=layer_source_asset_id,
            task_id=task_id,
            root_dir=root_dir,
        )


def _create_media_image_revision_locked(
    *,
    project_id: str,
    asset_id: str,
    base_revision_id: str,
    operation: str,
    parameters: dict[str, Any] | None,
    layer_source_asset_id: str | None,
    task_id: str | None,
    root_dir: Path | None,
) -> MediaImageRevisionInfo:
    if operation not in _OPERATIONS:
        raise MediaWorkspaceError("当前图片工作区不支持该编辑操作。")
    if operation == "composite_raster_layer" and not layer_source_asset_id:
        raise MediaWorkspaceError("栅格图层缺少要叠加的项目素材。")
    if operation != "composite_raster_layer" and layer_source_asset_id is not None:
        raise MediaWorkspaceError("当前图片操作不接受图层素材。")
    if task_id is not None and not _MEDIA_EDIT_TASK_ID_PATTERN.fullmatch(task_id):
        raise MediaWorkspaceError("图片编辑任务标识无效。")
    normalized_parameters = _normalize_operation_parameters(operation, parameters)
    project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)
    asset = _find_asset(manifest, asset_id)
    _assert_current_revision(asset, base_revision_id)
    parent = _find_revision(manifest, asset["current_revision_id"])
    parent_path = _resolve_project_file(project_dir, parent["file"])
    image = _load_revision_image(parent_path)
    revision_id = _new_id("mr")
    revision_relative = f"revisions/{revision_id}.png"
    revision_path = _resolve_project_file(project_dir, revision_relative)
    mask_id: str | None = None
    mask_path: Path | None = None
    mask_record: dict | None = None
    layer_id: str | None = None
    layer_record: dict | None = None
    composition_root_revision_id: str | None = None
    layer_stack: list[dict[str, Any]] = []
    try:
        if operation == "apply_rect_mask":
            mask_id = _new_id("mm")
            mask_relative = f"masks/{mask_id}.png"
            mask_path = _resolve_project_file(project_dir, mask_relative)
            mask_image = _create_rectangular_mask(image.size, normalized_parameters)
            result = _apply_rectangular_mask(image, mask_image)
            mask_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_save_png(mask_image, mask_path)
            _verify_mask_png(mask_path, expected_size=image.size)
        elif operation == "composite_raster_layer":
            composition_root_revision_id, layer_stack = _composition_state(parent)
            source_asset = _find_asset(manifest, layer_source_asset_id)
            if source_asset["asset_id"] == asset_id:
                raise MediaWorkspaceError("不能将当前图片作为它自己的栅格图层。")
            source_revision = _find_revision_for_asset(
                manifest,
                source_asset["current_revision_id"],
                source_asset["asset_id"],
            )
            layer_id = _new_id("ml")
            layer_record = {
                "layer_id": layer_id,
                "asset_id": asset_id,
                "base_revision_id": parent["revision_id"],
                "source_asset_id": source_asset["asset_id"],
                "source_revision_id": source_revision["revision_id"],
                "type": "raster_overlay",
                "parameters": normalized_parameters,
                "created_at": _utc_now(),
            }
            layer_stack = [*layer_stack, {"layer_id": layer_id, "visible": True}]
            result = _render_layer_stack(
                project_dir=project_dir,
                manifest=manifest,
                asset_id=asset_id,
                composition_root_revision_id=composition_root_revision_id,
                layer_stack=layer_stack,
                extra_layers=[layer_record],
            )
        elif operation == "recompose_raster_layers":
            composition_root_revision_id, existing_layer_stack = _composition_state(parent)
            if composition_root_revision_id is None or not existing_layer_stack:
                raise MediaWorkspaceError("当前版本没有可调整的栅格图层。")
            layer_stack = _validate_recomposed_layer_stack(
                requested=normalized_parameters["layer_stack"],
                existing=existing_layer_stack,
            )
            result = _render_layer_stack(
                project_dir=project_dir,
                manifest=manifest,
                asset_id=asset_id,
                composition_root_revision_id=composition_root_revision_id,
                layer_stack=layer_stack,
            )
        else:
            result = _apply_operation(image, operation, normalized_parameters)
        _atomic_save_png(result, revision_path)
        _verify_png(revision_path)
    except Exception:
        revision_path.unlink(missing_ok=True)
        if mask_path is not None:
            mask_path.unlink(missing_ok=True)
        raise
    now = _utc_now()
    try:
        if mask_id is not None and mask_path is not None:
            mask_record = _mask_record(
                mask_id=mask_id,
                asset_id=asset_id,
                base_revision_id=parent["revision_id"],
                parameters=normalized_parameters,
                relative_file=f"masks/{mask_id}.png",
                path=mask_path,
                created_at=now,
            )
        revision = _revision_record(
            revision_id=revision_id,
            asset_id=asset_id,
            parent_revision_id=parent["revision_id"],
            operation=operation,
            parameters=normalized_parameters,
            relative_file=revision_relative,
            path=revision_path,
            created_at=now,
            mask_id=mask_id,
            layer_id=layer_id,
            task_id=task_id,
            composition_root_revision_id=composition_root_revision_id,
            layer_stack=layer_stack,
        )
    except Exception:
        revision_path.unlink(missing_ok=True)
        if mask_path is not None:
            mask_path.unlink(missing_ok=True)
        raise
    manifest["revisions"].append(revision)
    if mask_record is not None:
        manifest["masks"].append(mask_record)
    if layer_record is not None:
        manifest["layers"].append(layer_record)
    asset["undo_revision_ids"].append(parent["revision_id"])
    asset["redo_revision_ids"].clear()
    asset["current_revision_id"] = revision_id
    _touch_manifest(manifest)
    try:
        _write_manifest(project_dir, manifest)
    except Exception:
        revision_path.unlink(missing_ok=True)
        if mask_path is not None:
            mask_path.unlink(missing_ok=True)
        raise
    return _revision_info(revision)


def navigate_media_image_history(
    *,
    project_id: str,
    asset_id: str,
    action: str,
    base_revision_id: str,
    root_dir: Path | None = None,
) -> MediaAssetRevisionListResponse:
    """Move the persisted current-version pointer without rewriting any image bytes."""
    if action not in {"undo", "redo"}:
        raise MediaWorkspaceError("当前图片工作区不支持该历史操作。")
    with _project_write_lock(project_id):
        project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)
        asset = _find_asset(manifest, asset_id)
        _assert_current_revision(asset, base_revision_id)
        stack_key = "undo_revision_ids" if action == "undo" else "redo_revision_ids"
        opposite_stack_key = "redo_revision_ids" if action == "undo" else "undo_revision_ids"
        stack = asset[stack_key]
        if not stack:
            action_name = "撤销" if action == "undo" else "重做"
            raise MediaWorkspaceError(f"当前图片没有可{action_name}的编辑。")

        from_revision_id = asset["current_revision_id"]
        target_revision_id = stack.pop()
        current_revision = _find_revision_for_asset(manifest, from_revision_id, asset_id)
        target_revision = _find_revision_for_asset(manifest, target_revision_id, asset_id)
        _verify_revision_integrity(_resolve_project_file(project_dir, current_revision["file"]), current_revision)
        _verify_revision_integrity(_resolve_project_file(project_dir, target_revision["file"]), target_revision)
        asset[opposite_stack_key].append(from_revision_id)
        asset["current_revision_id"] = target_revision_id
        _append_history_event(
            manifest,
            asset_id=asset_id,
            action=action,
            from_revision_id=from_revision_id,
            to_revision_id=target_revision_id,
        )
        _touch_manifest(manifest)
        _write_manifest(project_dir, manifest)
        return _asset_revision_list(asset, manifest)


def resolve_media_revision_preview_path(
    *,
    project_id: str,
    revision_id: str,
    root_dir: Path | None = None,
) -> Path:
    project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)
    revision = _find_revision(manifest, revision_id)
    path = _resolve_project_file(project_dir, revision["file"])
    _verify_revision_integrity(path, revision)
    return path


def export_media_image_revision(
    *,
    project_id: str,
    revision_id: str,
    filename: str,
    task_id: str | None = None,
    root_dir: Path | None = None,
    export_root: Path | None = None,
) -> MediaImageExportInfo:
    with _project_write_lock(project_id):
        return _export_media_image_revision_locked(
            project_id=project_id,
            revision_id=revision_id,
            filename=filename,
            task_id=task_id,
            root_dir=root_dir,
            export_root=export_root,
        )


def discard_media_image_export(
    *,
    project_id: str,
    export_id: str,
    root_dir: Path | None = None,
    export_root: Path | None = None,
) -> None:
    """撤销一个尚未交付的受控导出记录。

    异步任务在文件复制完成、Artifact 登记前可能收到取消信号。先从项目元数据中删除该
    export，再尽力移除对应文件，确保它不会以可下载或可审计产物的身份残留。文件清理失败
    时元数据仍已失效，后续不会通过任何受控下载路径暴露该孤立文件。
    """

    with _project_write_lock(project_id):
        _discard_media_image_export_locked(
            project_id=project_id,
            export_id=export_id,
            root_dir=root_dir,
            export_root=export_root,
        )


def _discard_media_image_export_locked(
    *,
    project_id: str,
    export_id: str,
    root_dir: Path | None,
    export_root: Path | None,
) -> None:
    _validate_id(export_id, _EXPORT_ID_PATTERN, "导出记录")
    project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)
    export_index = next(
        (index for index, item in enumerate(manifest["exports"]) if item["export_id"] == export_id),
        None,
    )
    if export_index is None:
        raise MediaWorkspaceError("未找到指定导出文件。")
    export = manifest["exports"][export_index]
    export_path = _resolve_export_file(media_export_root(export_root=export_root), export["file"])
    manifest["exports"].pop(export_index)
    _touch_manifest(manifest)
    _write_manifest(project_dir, manifest)
    try:
        export_path.unlink(missing_ok=True)
    except OSError:
        # 元数据已经删除，文件不再能经由 download resolver 访问。此处不将清理异常包装成
        # 取消失败，避免任务历史把一个不可见的遗留文件误判为仍然可交付。
        pass


def _export_media_image_revision_locked(
    *,
    project_id: str,
    revision_id: str,
    filename: str,
    task_id: str | None,
    root_dir: Path | None,
    export_root: Path | None,
) -> MediaImageExportInfo:
    safe_filename = _safe_export_filename(filename)
    if task_id is not None and not _MEDIA_EXPORT_TASK_ID_PATTERN.fullmatch(task_id):
        raise MediaWorkspaceError("图片导出任务标识无效。")
    project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)
    revision = _find_revision(manifest, revision_id)
    source_path = _resolve_project_file(project_dir, revision["file"])
    _verify_revision_integrity(source_path, revision)
    asset = _find_asset(manifest, revision["asset_id"])
    export_id = _new_id("me")
    export_relative = f"{project_id}/{export_id}_{safe_filename}"
    export_path = _resolve_export_file(media_export_root(export_root=export_root), export_relative)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_copy(source_path, export_path)
    width, height = _verify_png(export_path)
    now = _utc_now()
    export = {
        "export_id": export_id,
        "asset_id": asset["asset_id"],
        "revision_id": revision_id,
        "filename": safe_filename,
        "file": export_relative,
        "sha256": _sha256_file(export_path),
        "width": width,
        "height": height,
        "size_bytes": export_path.stat().st_size,
        "created_at": now,
        "task_id": task_id,
    }
    manifest["exports"].append(export)
    _touch_manifest(manifest)
    try:
        _write_manifest(project_dir, manifest)
    except Exception:
        export_path.unlink(missing_ok=True)
        raise
    return _export_info(project_id, export)


def find_media_export_for_task(
    *,
    project_id: str,
    task_id: str,
    root_dir: Path | None = None,
    export_root: Path | None = None,
) -> MediaImageExportInfo:
    """定位某个异步导出任务已经提交且可回读的唯一 PNG。

    启动恢复只认 manifest 中冻结的 task_id、文件哈希和 PNG 回读三项同时成立的结果。没有
    对应记录或出现歧义均交由调用方收束为失败，绝不猜测、重跑或登记错误 Artifact。
    """

    if not _MEDIA_EXPORT_TASK_ID_PATTERN.fullmatch(task_id):
        raise MediaWorkspaceError("图片导出任务标识无效。")
    with _project_write_lock(project_id):
        _, manifest = _project_manifest(project_id, root_dir=root_dir)
        matches = [item for item in manifest["exports"] if item.get("task_id") == task_id]
        if len(matches) != 1:
            raise MediaWorkspaceError("未找到唯一且可恢复的图片导出记录。")
        export = matches[0]
        path = _resolve_export_file(media_export_root(export_root=export_root), export["file"])
        if not path.is_file() or _sha256_file(path) != export["sha256"]:
            raise MediaWorkspaceError("图片导出文件不存在或已被修改。")
        _verify_png(path)
        return _export_info(project_id, export)


def find_media_image_revision_for_task(
    *,
    project_id: str,
    task_id: str,
    root_dir: Path | None = None,
) -> MediaImageRevisionInfo:
    """定位某次图片编辑已经原子提交且回读有效的唯一修订版本。

    重启恢复只对账 manifest 内冻结的编辑 task_id；不会根据当前版本猜测，也不会重放编辑，
    避免重复生成一个新分支或把迟到任务覆盖到用户后来选择的版本。
    """

    if not _MEDIA_EDIT_TASK_ID_PATTERN.fullmatch(task_id):
        raise MediaWorkspaceError("图片编辑任务标识无效。")
    with _project_write_lock(project_id):
        project_dir, manifest = _project_manifest(project_id, root_dir=root_dir)
        matches = [item for item in manifest["revisions"] if item.get("task_id") == task_id]
        if len(matches) != 1:
            raise MediaWorkspaceError("未找到唯一且可恢复的图片编辑版本。")
        revision = matches[0]
        path = _resolve_project_file(project_dir, revision["file"])
        _verify_revision_integrity(path, revision)
        return _revision_info(revision)


def resolve_media_export_download_path(
    *,
    project_id: str,
    export_id: str,
    root_dir: Path | None = None,
    export_root: Path | None = None,
) -> tuple[Path, str]:
    _, manifest = _project_manifest(project_id, root_dir=root_dir)
    _validate_id(export_id, _EXPORT_ID_PATTERN, "导出记录")
    export = next((item for item in manifest["exports"] if item["export_id"] == export_id), None)
    if export is None:
        raise MediaWorkspaceError("未找到指定导出文件。")
    path = _resolve_export_file(media_export_root(export_root=export_root), export["file"])
    if not path.is_file() or _sha256_file(path) != export["sha256"]:
        raise MediaWorkspaceError("导出文件不存在或已被修改，请重新导出。")
    _verify_png(path)
    return path, str(export["filename"])


def _project_manifest(project_id: str, *, root_dir: Path | None) -> tuple[Path, dict]:
    _validate_id(project_id, _PROJECT_ID_PATTERN, "项目")
    root = media_workspace_root(root_dir=root_dir)
    project_dir = (root / "projects" / project_id).resolve()
    try:
        project_dir.relative_to((root / "projects").resolve())
    except ValueError as exc:  # pragma: no cover - 正则已阻止，只保留路径防线。
        raise MediaWorkspaceError("项目路径不在受控工作区内。") from exc
    if not project_dir.is_dir():
        raise MediaWorkspaceError("未找到指定图片项目。")
    try:
        stored_manifest = load_media_workspace_manifest(project_id)
        manifest = _validate_manifest(stored_manifest)
        if stored_manifest.get("schema_version") != manifest["schema_version"]:
            save_media_workspace_manifest(manifest)
    except MediaWorkspaceProjectNotFoundError:
        manifest = _migrate_legacy_manifest_to_sqlite(project_dir)
    except (RuntimeError, ValueError) as exc:
        raise MediaWorkspaceError("图片工程 SQLite 元数据无法读取。") from exc
    if manifest["project_id"] != project_id:
        raise MediaWorkspaceError("图片项目元数据与目录不匹配。")
    return project_dir, manifest


def _migrate_legacy_manifest_to_sqlite(project_dir: Path) -> dict:
    manifest = _read_legacy_manifest(project_dir)
    try:
        save_media_workspace_manifest(manifest)
    except (RuntimeError, ValueError) as exc:
        raise MediaWorkspaceError("图片工程元数据迁移到 SQLite 失败。") from exc
    _archive_legacy_manifest(project_dir)
    return manifest


def _read_legacy_manifest(project_dir: Path) -> dict:
    manifest_path = project_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaWorkspaceError("图片项目元数据不可读取，请新建项目后重新导入。") from exc
    return _validate_manifest(manifest)


def _validate_manifest(manifest: dict) -> dict:
    if manifest.get("schema_version") == 1:
        manifest = _migrate_manifest_v1(manifest)
    if manifest.get("schema_version") == 2:
        manifest = _migrate_manifest_v2(manifest)
    if manifest.get("schema_version") == 3:
        manifest = _migrate_manifest_v3(manifest)
    if manifest.get("schema_version") == 4:
        manifest = _migrate_manifest_v4(manifest)
    if manifest.get("schema_version") == 5:
        manifest = _migrate_manifest_v5(manifest)
    if manifest.get("schema_version") == 6:
        manifest = _migrate_manifest_v6(manifest)
    required = {
        "schema_version",
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "assets",
        "revisions",
        "exports",
        "history_events",
        "masks",
        "layers",
    }
    if set(manifest) != required or manifest["schema_version"] != 7:
        raise MediaWorkspaceError("图片项目元数据版本不受支持。")
    _validate_id(str(manifest["project_id"]), _PROJECT_ID_PATTERN, "项目")
    if (
        not isinstance(manifest["assets"], list)
        or not isinstance(manifest["revisions"], list)
        or not isinstance(manifest["exports"], list)
        or not isinstance(manifest["history_events"], list)
        or not isinstance(manifest["masks"], list)
        or not isinstance(manifest["layers"], list)
    ):
        raise MediaWorkspaceError("图片项目元数据结构无效。")
    for asset in manifest["assets"]:
        if (
            not isinstance(asset, dict)
            or not isinstance(asset.get("undo_revision_ids"), list)
            or not isinstance(asset.get("redo_revision_ids"), list)
            or not all(isinstance(item, str) for item in asset["undo_revision_ids"])
            or not all(isinstance(item, str) for item in asset["redo_revision_ids"])
        ):
            raise MediaWorkspaceError("图片项目历史记录无效。")
    mask_ids: set[str] = set()
    for mask in manifest["masks"]:
        required_mask = {
            "mask_id",
            "asset_id",
            "base_revision_id",
            "type",
            "parameters",
            "file",
            "sha256",
            "width",
            "height",
            "size_bytes",
            "created_at",
        }
        if not isinstance(mask, dict) or set(mask) != required_mask:
            raise MediaWorkspaceError("图片蒙版元数据无效。")
        _validate_id(str(mask["mask_id"]), _MASK_ID_PATTERN, "蒙版")
        _validate_id(str(mask["asset_id"]), _ASSET_ID_PATTERN, "蒙版素材")
        _validate_id(str(mask["base_revision_id"]), _REVISION_ID_PATTERN, "蒙版基础版本")
        if (
            mask["mask_id"] in mask_ids
            or mask["type"] != "rectangular_selection"
            or not isinstance(mask["parameters"], dict)
            or not isinstance(mask["file"], str)
            or not isinstance(mask["sha256"], str)
            or not isinstance(mask["width"], int)
            or not isinstance(mask["height"], int)
            or not isinstance(mask["size_bytes"], int)
        ):
            raise MediaWorkspaceError("图片蒙版元数据无效。")
        mask_ids.add(mask["mask_id"])
    layer_ids: set[str] = set()
    for layer in manifest["layers"]:
        required_layer = {
            "layer_id",
            "asset_id",
            "base_revision_id",
            "source_asset_id",
            "source_revision_id",
            "type",
            "parameters",
            "created_at",
        }
        if not isinstance(layer, dict) or set(layer) != required_layer:
            raise MediaWorkspaceError("图片图层元数据无效。")
        _validate_id(str(layer["layer_id"]), _LAYER_ID_PATTERN, "图层")
        _validate_id(str(layer["asset_id"]), _ASSET_ID_PATTERN, "图层素材")
        _validate_id(str(layer["base_revision_id"]), _REVISION_ID_PATTERN, "图层基础版本")
        _validate_id(str(layer["source_asset_id"]), _ASSET_ID_PATTERN, "图层来源素材")
        _validate_id(str(layer["source_revision_id"]), _REVISION_ID_PATTERN, "图层来源版本")
        if (
            layer["layer_id"] in layer_ids
            or layer["asset_id"] == layer["source_asset_id"]
            or layer["type"] != "raster_overlay"
            or not isinstance(layer["parameters"], dict)
        ):
            raise MediaWorkspaceError("图片图层元数据无效。")
        layer_ids.add(layer["layer_id"])
    for revision in manifest["revisions"]:
        if (
            not isinstance(revision, dict)
            or "mask_id" not in revision
            or "layer_id" not in revision
            or "task_id" not in revision
            or "composition_root_revision_id" not in revision
            or "layer_stack" not in revision
        ):
            raise MediaWorkspaceError("图片修订版本元数据无效。")
        mask_id = revision["mask_id"]
        if mask_id is not None and (not isinstance(mask_id, str) or mask_id not in mask_ids):
            raise MediaWorkspaceError("图片修订版本关联的蒙版无效。")
        layer_id = revision["layer_id"]
        if layer_id is not None and (not isinstance(layer_id, str) or layer_id not in layer_ids):
            raise MediaWorkspaceError("图片修订版本关联的图层无效。")
        task_id = revision["task_id"]
        if task_id is not None and (
            not isinstance(task_id, str) or not _MEDIA_EDIT_TASK_ID_PATTERN.fullmatch(task_id)
        ):
            raise MediaWorkspaceError("图片修订版本关联的任务无效。")
        composition_root_revision_id = revision["composition_root_revision_id"]
        layer_stack = revision["layer_stack"]
        if composition_root_revision_id is None:
            if layer_stack != []:
                raise MediaWorkspaceError("图片修订版本图层栈无效。")
            if revision["operation"] == "recompose_raster_layers":
                raise MediaWorkspaceError("图片修订版本图层栈无效。")
            continue
        if (
            not isinstance(composition_root_revision_id, str)
            or not _REVISION_ID_PATTERN.fullmatch(composition_root_revision_id)
            or not isinstance(layer_stack, list)
            or not layer_stack
        ):
            raise MediaWorkspaceError("图片修订版本图层栈无效。")
        stack_layer_ids: set[str] = set()
        for state in layer_stack:
            if (
                not isinstance(state, dict)
                or set(state) != {"layer_id", "visible"}
                or not isinstance(state["layer_id"], str)
                or state["layer_id"] in stack_layer_ids
                or state["layer_id"] not in layer_ids
                or not isinstance(state["visible"], bool)
            ):
                raise MediaWorkspaceError("图片修订版本图层栈无效。")
            layer = next(item for item in manifest["layers"] if item["layer_id"] == state["layer_id"])
            if layer["asset_id"] != revision["asset_id"]:
                raise MediaWorkspaceError("图片修订版本图层栈无效。")
            stack_layer_ids.add(state["layer_id"])
        if revision["operation"] == "composite_raster_layer" and revision["layer_id"] not in stack_layer_ids:
            raise MediaWorkspaceError("图片修订版本图层栈无效。")
    export_ids: set[str] = set()
    for export in manifest["exports"]:
        required_export = {
            "export_id",
            "asset_id",
            "revision_id",
            "filename",
            "file",
            "sha256",
            "width",
            "height",
            "size_bytes",
            "created_at",
            "task_id",
        }
        if not isinstance(export, dict) or set(export) != required_export:
            raise MediaWorkspaceError("图片导出元数据无效。")
        task_id = export["task_id"]
        if (
            not isinstance(export["export_id"], str)
            or export["export_id"] in export_ids
            or not _EXPORT_ID_PATTERN.fullmatch(export["export_id"])
            or not isinstance(export["asset_id"], str)
            or not _ASSET_ID_PATTERN.fullmatch(export["asset_id"])
            or not isinstance(export["revision_id"], str)
            or not _REVISION_ID_PATTERN.fullmatch(export["revision_id"])
            or not isinstance(export["filename"], str)
            or not isinstance(export["file"], str)
            or not isinstance(export["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", export["sha256"])
            or not isinstance(export["width"], int)
            or not isinstance(export["height"], int)
            or not isinstance(export["size_bytes"], int)
            or export["width"] < 1
            or export["height"] < 1
            or export["size_bytes"] < 1
            or (task_id is not None and (not isinstance(task_id, str) or not _MEDIA_EXPORT_TASK_ID_PATTERN.fullmatch(task_id)))
        ):
            raise MediaWorkspaceError("图片导出元数据无效。")
        export_ids.add(export["export_id"])
    return manifest


def _migrate_manifest_v1(manifest: dict) -> dict:
    required = {"schema_version", "project_id", "title", "created_at", "updated_at", "assets", "revisions", "exports"}
    if set(manifest) != required:
        raise MediaWorkspaceError("图片项目元数据版本不受支持。")
    if not isinstance(manifest["assets"], list) or not isinstance(manifest["revisions"], list):
        raise MediaWorkspaceError("图片项目元数据结构无效。")

    migrated = dict(manifest)
    migrated["schema_version"] = 2
    migrated["history_events"] = []
    for asset in migrated["assets"]:
        if not isinstance(asset, dict):
            raise MediaWorkspaceError("图片项目元数据结构无效。")
        asset["undo_revision_ids"] = _infer_undo_revision_ids(migrated["revisions"], asset)
        asset["redo_revision_ids"] = []
    return migrated


def _migrate_manifest_v2(manifest: dict) -> dict:
    required = {
        "schema_version",
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "assets",
        "revisions",
        "exports",
        "history_events",
    }
    if set(manifest) != required or not isinstance(manifest["revisions"], list):
        raise MediaWorkspaceError("图片项目元数据版本不受支持。")
    migrated = dict(manifest)
    migrated["schema_version"] = 3
    migrated["masks"] = []
    migrated["revisions"] = [
        {**revision, "mask_id": None} if isinstance(revision, dict) else revision
        for revision in manifest["revisions"]
    ]
    return migrated


def _migrate_manifest_v3(manifest: dict) -> dict:
    required = {
        "schema_version",
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "assets",
        "revisions",
        "exports",
        "history_events",
        "masks",
    }
    if set(manifest) != required or not isinstance(manifest["revisions"], list):
        raise MediaWorkspaceError("图片项目元数据版本不受支持。")
    migrated = dict(manifest)
    migrated["schema_version"] = 4
    migrated["layers"] = []
    migrated["revisions"] = [
        {**revision, "layer_id": None} if isinstance(revision, dict) else revision
        for revision in manifest["revisions"]
    ]
    return migrated


def _migrate_manifest_v4(manifest: dict) -> dict:
    required = {
        "schema_version",
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "assets",
        "revisions",
        "exports",
        "history_events",
        "masks",
        "layers",
    }
    if set(manifest) != required or not isinstance(manifest["exports"], list):
        raise MediaWorkspaceError("图片项目元数据版本不受支持。")
    migrated = dict(manifest)
    migrated["schema_version"] = 5
    migrated["exports"] = [
        {**export, "task_id": None} if isinstance(export, dict) else export
        for export in manifest["exports"]
    ]
    return migrated


def _migrate_manifest_v5(manifest: dict) -> dict:
    required = {
        "schema_version",
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "assets",
        "revisions",
        "exports",
        "history_events",
        "masks",
        "layers",
    }
    if set(manifest) != required or not isinstance(manifest["revisions"], list):
        raise MediaWorkspaceError("图片项目元数据版本不受支持。")
    migrated = dict(manifest)
    migrated["schema_version"] = 6
    migrated["revisions"] = [
        {**revision, "task_id": None} if isinstance(revision, dict) else revision
        for revision in manifest["revisions"]
    ]
    return migrated


def _migrate_manifest_v6(manifest: dict) -> dict:
    """Give legacy flattened layer revisions an immutable, renderable stack snapshot."""

    required = {
        "schema_version",
        "project_id",
        "title",
        "created_at",
        "updated_at",
        "assets",
        "revisions",
        "exports",
        "history_events",
        "masks",
        "layers",
    }
    if set(manifest) != required or not isinstance(manifest["revisions"], list):
        raise MediaWorkspaceError("图片项目元数据版本不受支持。")
    migrated = dict(manifest)
    migrated["schema_version"] = 7
    upgraded_by_id: dict[str, dict] = {}
    upgraded_revisions: list[dict] = []
    for item in manifest["revisions"]:
        if not isinstance(item, dict):
            raise MediaWorkspaceError("图片项目元数据结构无效。")
        upgraded = dict(item)
        parent = upgraded_by_id.get(upgraded.get("parent_revision_id"))
        if upgraded.get("operation") == "composite_raster_layer" and upgraded.get("layer_id"):
            root_revision_id = parent.get("composition_root_revision_id") if parent else None
            if root_revision_id is None:
                root_revision_id = parent.get("revision_id") if parent else None
            inherited_stack = list(parent.get("layer_stack") or []) if parent else []
            if not isinstance(root_revision_id, str):
                raise MediaWorkspaceError("图片项目图层历史不可迁移。")
            upgraded["composition_root_revision_id"] = root_revision_id
            upgraded["layer_stack"] = [
                *[dict(state) for state in inherited_stack],
                {"layer_id": upgraded["layer_id"], "visible": True},
            ]
        else:
            upgraded["composition_root_revision_id"] = None
            upgraded["layer_stack"] = []
        revision_id = upgraded.get("revision_id")
        if not isinstance(revision_id, str):
            raise MediaWorkspaceError("图片项目图层历史不可迁移。")
        upgraded_by_id[revision_id] = upgraded
        upgraded_revisions.append(upgraded)
    migrated["revisions"] = upgraded_revisions
    return migrated


def _infer_undo_revision_ids(revisions: list, asset: dict) -> list[str]:
    current_revision_id = asset.get("current_revision_id")
    if not isinstance(current_revision_id, str):
        raise MediaWorkspaceError("图片项目历史记录无效。")
    by_id = {
        revision.get("revision_id"): revision
        for revision in revisions
        if isinstance(revision, dict) and revision.get("asset_id") == asset.get("asset_id")
    }
    current = by_id.get(current_revision_id)
    if current is None:
        raise MediaWorkspaceError("图片项目历史记录无效。")
    undo_ids: list[str] = []
    visited: set[str] = set()
    while current.get("parent_revision_id") is not None:
        revision_id = current.get("revision_id")
        parent_id = current.get("parent_revision_id")
        if not isinstance(revision_id, str) or not isinstance(parent_id, str) or revision_id in visited:
            raise MediaWorkspaceError("图片项目历史记录无效。")
        visited.add(revision_id)
        parent = by_id.get(parent_id)
        if parent is None:
            raise MediaWorkspaceError("图片项目历史记录无效。")
        undo_ids.append(parent_id)
        current = parent
    undo_ids.reverse()
    return undo_ids


def _write_manifest(project_dir: Path, manifest: dict) -> None:
    _ = project_dir
    try:
        save_media_workspace_manifest(_validate_manifest(manifest))
    except (RuntimeError, ValueError) as exc:
        raise MediaWorkspaceError("图片工程 SQLite 元数据写入失败。") from exc


def _archive_legacy_manifest(project_dir: Path) -> None:
    """Keep one inactive migration copy while making SQLite the only read path."""

    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        return
    archive_path = project_dir / "manifest.sqlite-migrated.json"
    try:
        if archive_path.exists():
            manifest_path.unlink()
        else:
            manifest_path.replace(archive_path)
    except OSError as exc:
        raise MediaWorkspaceError("图片工程 SQLite 已写入，但旧元数据留档失败。") from exc


def _project_info(manifest: dict) -> MediaProjectInfo:
    return MediaProjectInfo(
        project_id=manifest["project_id"],
        title=manifest["title"],
        created_at=manifest["created_at"],
        updated_at=manifest["updated_at"],
        asset_count=len(manifest["assets"]),
    )


def _asset_info(asset: dict, manifest: dict) -> MediaAssetInfo:
    revision_count = sum(1 for revision in manifest["revisions"] if revision["asset_id"] == asset["asset_id"])
    return MediaAssetInfo(
        asset_id=asset["asset_id"],
        name=asset["name"],
        source_sha256=asset["source_sha256"],
        mime_type=asset["mime_type"],
        width=int(asset["width"]),
        height=int(asset["height"]),
        size_bytes=int(asset["size_bytes"]),
        created_at=asset["created_at"],
        current_revision_id=asset["current_revision_id"],
        revision_count=revision_count,
        undo_available=bool(asset["undo_revision_ids"]),
        redo_available=bool(asset["redo_revision_ids"]),
    )


def _asset_revision_list(asset: dict, manifest: dict) -> MediaAssetRevisionListResponse:
    revisions = [
        _revision_info(revision)
        for revision in manifest["revisions"]
        if revision["asset_id"] == asset["asset_id"]
    ]
    return MediaAssetRevisionListResponse(asset=_asset_info(asset, manifest), revisions=revisions)


def _revision_info(revision: dict) -> MediaImageRevisionInfo:
    return MediaImageRevisionInfo(
        revision_id=revision["revision_id"],
        asset_id=revision["asset_id"],
        parent_revision_id=revision["parent_revision_id"],
        operation=revision["operation"],
        parameters=dict(revision.get("parameters") or {}),
        mask_id=revision.get("mask_id"),
        layer_id=revision.get("layer_id"),
        sha256=revision["sha256"],
        width=int(revision["width"]),
        height=int(revision["height"]),
        size_bytes=int(revision["size_bytes"]),
        created_at=revision["created_at"],
    )


def _export_info(project_id: str, export: dict) -> MediaImageExportInfo:
    return MediaImageExportInfo(
        project_id=project_id,
        **{key: value for key, value in export.items() if key not in {"file", "task_id"}},
    )


def _find_asset(manifest: dict, asset_id: str) -> dict:
    _validate_id(asset_id, _ASSET_ID_PATTERN, "素材")
    asset = next((item for item in manifest["assets"] if item["asset_id"] == asset_id), None)
    if asset is None:
        raise MediaWorkspaceError("未找到指定图片素材。")
    return asset


def _assert_current_revision(asset: dict, base_revision_id: str) -> None:
    _validate_id(base_revision_id, _REVISION_ID_PATTERN, "基础修订版本")
    if asset["current_revision_id"] != base_revision_id:
        raise MediaWorkspaceConflictError("图片版本已更新，请刷新后再操作。")


def _find_revision(manifest: dict, revision_id: str) -> dict:
    _validate_id(revision_id, _REVISION_ID_PATTERN, "修订版本")
    revision = next((item for item in manifest["revisions"] if item["revision_id"] == revision_id), None)
    if revision is None:
        raise MediaWorkspaceError("未找到指定图片修订版本。")
    return revision


def _find_revision_for_asset(manifest: dict, revision_id: str, asset_id: str) -> dict:
    revision = _find_revision(manifest, revision_id)
    if revision["asset_id"] != asset_id:
        raise MediaWorkspaceError("图片项目历史记录无效。")
    return revision


def _append_history_event(
    manifest: dict,
    *,
    asset_id: str,
    action: str,
    from_revision_id: str,
    to_revision_id: str,
) -> None:
    manifest["history_events"].append(
        {
            "event_id": _new_id("mh"),
            "asset_id": asset_id,
            "action": action,
            "from_revision_id": from_revision_id,
            "to_revision_id": to_revision_id,
            "created_at": _utc_now(),
        }
    )


def _decode_base64(content_base64: str) -> bytes:
    try:
        raw = base64.b64decode(content_base64.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise MediaWorkspaceError("图片内容不是有效的 Base64 数据。") from exc
    if not raw:
        raise MediaWorkspaceError("图片文件为空，无法导入。")
    if len(raw) > MAX_IMAGE_BYTES:
        raise MediaWorkspaceError("单张图片最大支持 20MB，请压缩后再导入。")
    return raw


def _decode_supported_image(raw_bytes: bytes) -> tuple[Image.Image, str, str]:
    try:
        with Image.open(io.BytesIO(raw_bytes)) as probe:
            image_format = str(probe.format or "").upper()
            if image_format not in _SUPPORTED_IMAGE_TYPES:
                raise MediaWorkspaceError("图片工作区仅支持 JPEG、PNG 或 WebP 图片。")
            width, height = probe.size
            if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                raise MediaWorkspaceError("图片尺寸无效或超过 4000 万像素上限。")
            probe.verify()
        with Image.open(io.BytesIO(raw_bytes)) as source:
            source.load()
            image = source.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaWorkspaceError("图片无法解码，可能已损坏或格式不受支持。") from exc
    suffix, mime_type = _SUPPORTED_IMAGE_TYPES[image_format]
    return image, suffix, mime_type


def _normalize_image(image: Image.Image) -> Image.Image:
    normalized = ImageOps.exif_transpose(image)
    width, height = normalized.size
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise MediaWorkspaceError("图片尺寸无效或超过 4000 万像素上限。")
    if "A" in normalized.getbands():
        return normalized.convert("RGBA")
    return normalized.convert("RGB")


def _load_revision_image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as source:
            source.load()
            return _normalize_image(source.copy())
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaWorkspaceError("当前修订版本无法读取，无法继续编辑。") from exc


def _apply_operation(image: Image.Image, operation: str, parameters: dict[str, int]) -> Image.Image:
    if operation == "rotate_left":
        return image.transpose(Image.Transpose.ROTATE_90)
    if operation == "rotate_right":
        return image.transpose(Image.Transpose.ROTATE_270)
    if operation == "flip_horizontal":
        return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if operation == "grayscale":
        alpha = image.getchannel("A") if "A" in image.getbands() else None
        result = ImageOps.grayscale(image.convert("RGB"))
        if alpha is None:
            return result
        rgba = result.convert("RGBA")
        rgba.putalpha(alpha)
        return rgba
    if operation == "adjust_color":
        return _adjust_color(image, parameters)
    if operation == "crop":
        return _crop_image(image, parameters)
    if operation == "resize":
        return _resize_image(image, parameters)
    raise MediaWorkspaceError("当前图片工作区不支持该编辑操作。")  # pragma: no cover


def _normalize_operation_parameters(operation: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(parameters or {})
    if operation == "recompose_raster_layers":
        if set(raw) != {"layer_stack"} or not isinstance(raw["layer_stack"], list) or not raw["layer_stack"]:
            raise MediaWorkspaceError("图层重组需要完整的图层栈状态。")
        normalized_stack: list[dict[str, Any]] = []
        layer_ids: set[str] = set()
        for state in raw["layer_stack"]:
            if not isinstance(state, dict) or set(state) != {"layer_id", "visible"}:
                raise MediaWorkspaceError("图层重组状态无效。")
            layer_id = state["layer_id"]
            visible = state["visible"]
            if (
                not isinstance(layer_id, str)
                or not _LAYER_ID_PATTERN.fullmatch(layer_id)
                or layer_id in layer_ids
                or not isinstance(visible, bool)
            ):
                raise MediaWorkspaceError("图层重组状态无效。")
            layer_ids.add(layer_id)
            normalized_stack.append({"layer_id": layer_id, "visible": visible})
        return {"layer_stack": normalized_stack}
    if any(not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) for key, value in raw.items()):
        raise MediaWorkspaceError("图片编辑参数格式无效。")
    if operation in {"rotate_left", "rotate_right", "flip_horizontal", "grayscale"}:
        if raw:
            raise MediaWorkspaceError("当前图片操作不接受额外参数。")
        return {}
    if operation == "adjust_color":
        allowed = {"brightness", "contrast", "saturation"}
        if set(raw) - allowed:
            raise MediaWorkspaceError("色彩调整包含不支持的参数。")
        normalized = {key: int(raw.get(key, 0)) for key in sorted(allowed)}
        if any(value < -100 or value > 100 for value in normalized.values()):
            raise MediaWorkspaceError("色彩调整范围必须在 -100 到 100 之间。")
        if not any(normalized.values()):
            raise MediaWorkspaceError("请至少调整一个色彩参数。")
        return normalized
    if operation == "crop":
        required = {"x", "y", "width", "height"}
        if set(raw) != required:
            raise MediaWorkspaceError("裁剪需要 x、y、宽度和高度。")
        normalized = {key: int(raw[key]) for key in sorted(required)}
        if normalized["x"] < 0 or normalized["y"] < 0 or normalized["width"] < 1 or normalized["height"] < 1:
            raise MediaWorkspaceError("裁剪区域参数无效。")
        return normalized
    if operation == "resize":
        required = {"width", "height"}
        if set(raw) != required:
            raise MediaWorkspaceError("缩放需要目标宽度和高度。")
        normalized = {key: int(raw[key]) for key in sorted(required)}
        if normalized["width"] < 1 or normalized["height"] < 1 or normalized["width"] > 10_000 or normalized["height"] > 10_000:
            raise MediaWorkspaceError("目标尺寸必须在 1 到 10000 像素之间。")
        if normalized["width"] * normalized["height"] > MAX_IMAGE_PIXELS:
            raise MediaWorkspaceError("目标尺寸超过 4000 万像素上限。")
        return normalized
    if operation == "apply_rect_mask":
        required = {"x", "y", "width", "height"}
        if set(raw) != required:
            raise MediaWorkspaceError("矩形蒙版需要 x、y、宽度和高度。")
        normalized = {key: int(raw[key]) for key in sorted(required)}
        if normalized["x"] < 0 or normalized["y"] < 0 or normalized["width"] < 1 or normalized["height"] < 1:
            raise MediaWorkspaceError("矩形蒙版区域参数无效。")
        return normalized
    if operation == "composite_raster_layer":
        required = {"x", "y", "opacity"}
        if set(raw) != required:
            raise MediaWorkspaceError("栅格图层需要 x、y 和不透明度。")
        normalized = {key: int(raw[key]) for key in sorted(required)}
        if normalized["x"] < 0 or normalized["y"] < 0 or not 1 <= normalized["opacity"] <= 100:
            raise MediaWorkspaceError("栅格图层参数无效。")
        return normalized
    raise MediaWorkspaceError("当前图片工作区不支持该编辑操作。")  # pragma: no cover


def _adjust_color(image: Image.Image, parameters: dict[str, int]) -> Image.Image:
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    result = image.convert("RGB")
    # 固定亮度、对比度、饱和度顺序，使同一参数集跨版本有唯一结果。
    result = ImageEnhance.Brightness(result).enhance(1.0 + parameters["brightness"] / 100.0)
    result = ImageEnhance.Contrast(result).enhance(1.0 + parameters["contrast"] / 100.0)
    result = ImageEnhance.Color(result).enhance(1.0 + parameters["saturation"] / 100.0)
    if alpha is None:
        return result
    rgba = result.convert("RGBA")
    rgba.putalpha(alpha)
    return rgba


def _crop_image(image: Image.Image, parameters: dict[str, int]) -> Image.Image:
    x, y = parameters["x"], parameters["y"]
    width, height = parameters["width"], parameters["height"]
    if x + width > image.width or y + height > image.height:
        raise MediaWorkspaceError("裁剪区域超出当前图片边界。")
    return image.crop((x, y, x + width, y + height))


def _resize_image(image: Image.Image, parameters: dict[str, int]) -> Image.Image:
    return image.resize((parameters["width"], parameters["height"]), Image.Resampling.LANCZOS)


def _create_rectangular_mask(size: tuple[int, int], parameters: dict[str, int]) -> Image.Image:
    width, height = size
    x, y = parameters["x"], parameters["y"]
    mask_width, mask_height = parameters["width"], parameters["height"]
    if x + mask_width > width or y + mask_height > height:
        raise MediaWorkspaceError("矩形蒙版区域超出当前图片边界。")
    mask = Image.new("L", size, 0)
    mask.paste(255, (x, y, x + mask_width, y + mask_height))
    return mask


def _apply_rectangular_mask(image: Image.Image, mask: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = ImageChops.multiply(rgba.getchannel("A"), mask)
    rgba.putalpha(alpha)
    return rgba


def _composite_raster_layer(
    image: Image.Image,
    overlay: Image.Image,
    parameters: dict[str, int],
) -> Image.Image:
    x, y = parameters["x"], parameters["y"]
    if x >= image.width or y >= image.height:
        raise MediaWorkspaceError("栅格图层位置超出当前图片边界。")
    result = image.convert("RGBA")
    layer = overlay.convert("RGBA")
    layer = layer.crop((0, 0, min(layer.width, image.width - x), min(layer.height, image.height - y)))
    if parameters["opacity"] < 100:
        alpha = layer.getchannel("A").point(lambda value: value * parameters["opacity"] // 100)
        layer.putalpha(alpha)
    result.alpha_composite(layer, (x, y))
    return result


def _composition_state(revision: dict) -> tuple[str, list[dict[str, Any]]]:
    """Return the immutable stack snapshot inherited by the next composition revision."""

    root_revision_id = revision.get("composition_root_revision_id")
    layer_stack = revision.get("layer_stack")
    if root_revision_id is None:
        return revision["revision_id"], []
    if not isinstance(root_revision_id, str) or not isinstance(layer_stack, list) or not layer_stack:
        raise MediaWorkspaceError("当前图片图层状态不可读取。")
    return root_revision_id, [dict(item) for item in layer_stack]


def _validate_recomposed_layer_stack(
    *,
    requested: Any,
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(requested, list) or not requested:
        raise MediaWorkspaceError("图层重组需要完整的图层栈状态。")
    existing_ids = [state.get("layer_id") for state in existing]
    requested_ids = [state.get("layer_id") for state in requested if isinstance(state, dict)]
    if (
        len(requested_ids) != len(requested)
        or len(requested_ids) != len(existing_ids)
        or len(set(requested_ids)) != len(requested_ids)
        or set(requested_ids) != set(existing_ids)
    ):
        raise MediaWorkspaceError("图层重组必须保留当前版本的全部图层。")
    normalized: list[dict[str, Any]] = []
    for state in requested:
        if (
            not isinstance(state, dict)
            or set(state) != {"layer_id", "visible"}
            or not isinstance(state["layer_id"], str)
            or not isinstance(state["visible"], bool)
        ):
            raise MediaWorkspaceError("图层重组状态无效。")
        normalized.append({"layer_id": state["layer_id"], "visible": state["visible"]})
    return normalized


def _render_layer_stack(
    *,
    project_dir: Path,
    manifest: dict,
    asset_id: str,
    composition_root_revision_id: str,
    layer_stack: list[dict[str, Any]],
    extra_layers: list[dict] | None = None,
) -> Image.Image:
    root_revision = _find_revision_for_asset(manifest, composition_root_revision_id, asset_id)
    root_path = _resolve_project_file(project_dir, root_revision["file"])
    _verify_revision_integrity(root_path, root_revision)
    result = _load_revision_image(root_path)
    layer_by_id = {item["layer_id"]: item for item in [*manifest["layers"], *(extra_layers or [])]}
    for state in layer_stack:
        layer_id = state["layer_id"]
        layer = layer_by_id.get(layer_id)
        if layer is None or layer["asset_id"] != asset_id:
            raise MediaWorkspaceError("图层栈引用了不属于当前图片的图层。")
        if not state["visible"]:
            continue
        source_revision = _find_revision_for_asset(
            manifest,
            layer["source_revision_id"],
            layer["source_asset_id"],
        )
        source_path = _resolve_project_file(project_dir, source_revision["file"])
        _verify_revision_integrity(source_path, source_revision)
        result = _composite_raster_layer(result, _load_revision_image(source_path), layer["parameters"])
    return result


def _revision_record(
    *,
    revision_id: str,
    asset_id: str,
    parent_revision_id: str | None,
    operation: str,
    parameters: dict[str, Any],
    relative_file: str,
    path: Path,
    created_at: str,
    mask_id: str | None = None,
    layer_id: str | None = None,
    task_id: str | None = None,
    composition_root_revision_id: str | None = None,
    layer_stack: list[dict[str, Any]] | None = None,
) -> dict:
    width, height = _verify_png(path)
    return {
        "revision_id": revision_id,
        "asset_id": asset_id,
        "parent_revision_id": parent_revision_id,
        "operation": operation,
        "parameters": parameters,
        "mask_id": mask_id,
        "layer_id": layer_id,
        "task_id": task_id,
        "composition_root_revision_id": composition_root_revision_id,
        "layer_stack": list(layer_stack or []),
        "file": relative_file,
        "sha256": _sha256_file(path),
        "width": width,
        "height": height,
        "size_bytes": path.stat().st_size,
        "created_at": created_at,
    }


def _mask_record(
    *,
    mask_id: str,
    asset_id: str,
    base_revision_id: str,
    parameters: dict[str, int],
    relative_file: str,
    path: Path,
    created_at: str,
) -> dict:
    width, height = _verify_mask_png(path)
    return {
        "mask_id": mask_id,
        "asset_id": asset_id,
        "base_revision_id": base_revision_id,
        "type": "rectangular_selection",
        "parameters": parameters,
        "file": relative_file,
        "sha256": _sha256_file(path),
        "width": width,
        "height": height,
        "size_bytes": path.stat().st_size,
        "created_at": created_at,
    }


def _verify_revision_integrity(path: Path, revision: dict) -> None:
    if not path.is_file() or _sha256_file(path) != revision["sha256"]:
        raise MediaWorkspaceError("图片修订版本不存在或已被修改，请从原始素材重新开始。")
    width, height = _verify_png(path)
    if width != revision["width"] or height != revision["height"] or path.stat().st_size != revision["size_bytes"]:
        raise MediaWorkspaceError("图片修订版本元数据不一致，请从原始素材重新开始。")


def _verify_png(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise MediaWorkspaceError("图片修订版本格式无效。")
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaWorkspaceError("图片文件回读验证失败。") from exc
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise MediaWorkspaceError("图片文件尺寸超出允许范围。")
    return width, height


def _verify_mask_png(path: Path, *, expected_size: tuple[int, int] | None = None) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.mode != "L":
                raise MediaWorkspaceError("图片蒙版格式无效。")
            image.verify()
        with Image.open(path) as image:
            image.load()
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaWorkspaceError("图片蒙版回读验证失败。") from exc
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise MediaWorkspaceError("图片蒙版尺寸超出允许范围。")
    if expected_size is not None and (width, height) != expected_size:
        raise MediaWorkspaceError("图片蒙版尺寸与当前修订版本不一致。")
    return width, height


def _safe_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise MediaWorkspaceError("项目名称不能为空。")
    return title


def _safe_filename(value: str) -> str:
    candidate = value.strip()
    if not candidate or Path(candidate).name != candidate or candidate in {".", ".."}:
        raise MediaWorkspaceError("图片文件名无效，请重新选择本地图片。")
    if not Path(candidate).stem:
        raise MediaWorkspaceError("图片文件名缺少有效名称。")
    return candidate


def _safe_export_filename(value: str) -> str:
    safe_name = _safe_filename(value)
    stem = Path(safe_name).stem
    return f"{stem}.png"


def _resolve_project_file(project_dir: Path, relative_file: str) -> Path:
    candidate = (project_dir / relative_file).resolve()
    try:
        candidate.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise MediaWorkspaceError("图片文件路径不在受控项目内。") from exc
    return candidate


def _resolve_export_file(export_root: Path, relative_file: str) -> Path:
    candidate = (export_root / relative_file).resolve()
    try:
        candidate.relative_to(export_root.resolve())
    except ValueError as exc:
        raise MediaWorkspaceError("导出文件路径不在受控目录内。") from exc
    return candidate


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MediaWorkspaceError("无法写入图片工作区，请检查目录权限和可用空间。") from exc


def _atomic_save_png(image: Image.Image, path: Path) -> None:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    _atomic_write_bytes(path, buffer.getvalue())


def _atomic_copy(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        copyfile(source, temporary)
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MediaWorkspaceError("无法写入图片导出文件，请检查目录权限和可用空间。") from exc


def _touch_manifest(manifest: dict) -> None:
    manifest["updated_at"] = _utc_now()


def _validate_id(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not pattern.fullmatch(value):
        raise MediaWorkspaceError(f"{label}标识无效。")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _project_write_lock(project_id: str) -> RLock:
    """串行化同一项目的 manifest 修改，避免并发请求覆盖彼此的版本登记。"""

    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(project_id, RLock())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()
