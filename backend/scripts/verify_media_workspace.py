"""回归验证图片工作区的受控导入、版本链和导出回读。"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATABASE_PATH = Path(tempfile.gettempdir()) / f"agentflow_media_workspace_{uuid4().hex}.db"
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_DATABASE_PATH)
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.media_workspace_repository import (
    delete_media_workspace_manifest,
    load_media_workspace_manifest,
)
import app.services.media_workspace as media_workspace_service
from app.services.media_workspace import (
    MediaWorkspaceConflictError,
    MediaWorkspaceError,
    create_media_image_revision,
    create_media_project,
    export_media_image_revision,
    get_media_image_layer_stack,
    get_media_project,
    import_media_image_base64,
    list_media_asset_revisions,
    list_media_projects,
    navigate_media_image_history,
    resolve_media_export_download_path,
    resolve_media_revision_preview_path,
)


def _fixture_png() -> bytes:
    image = Image.new("RGBA", (320, 180), (28, 91, 169, 255))
    for x in range(80, 240):
        for y in range(35, 145):
            image.putpixel((x, y), (244, 184, 63, 180))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _fixture_webp() -> bytes:
    image = Image.new("RGB", (96, 64), (167, 72, 116))
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def _fixture_overlay_png() -> bytes:
    image = Image.new("RGBA", (24, 18), (213, 61, 74, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _fixture_top_overlay_png() -> bytes:
    image = Image.new("RGBA", (24, 18), (41, 184, 96, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _expect_workspace_error(callback) -> None:
    try:
        callback()
    except MediaWorkspaceError:
        return
    raise AssertionError("expected MediaWorkspaceError")


def _expect_manifest_persistence_failure(callback) -> None:
    original_save = media_workspace_service.save_media_workspace_manifest

    def fail_save(_manifest) -> None:
        raise ValueError("simulated SQLite write failure")

    media_workspace_service.save_media_workspace_manifest = fail_save
    try:
        _expect_workspace_error(callback)
    finally:
        media_workspace_service.save_media_workspace_manifest = original_save


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agentflow_media_workspace_") as temporary:
        root = Path(temporary) / "workspace"
        exports = Path(temporary) / "exports"
        project = create_media_project(title="产品图修改", root_dir=root)
        raw = _fixture_png()
        asset = import_media_image_base64(
            project_id=project.project_id,
            filename="product-source.png",
            content_base64=base64.b64encode(raw).decode("ascii"),
            root_dir=root,
        )

        project_dir = root / "projects" / project.project_id
        manifest = load_media_workspace_manifest(project.project_id)
        source_path = project_dir / manifest["assets"][0]["source_file"]
        assert source_path.read_bytes() == raw
        assert asset.source_sha256 == sha256(raw).hexdigest()
        assert not (project_dir / "manifest.json").exists()
        assert str(root) not in json.dumps(manifest, ensure_ascii=False)
        assert get_media_project(project.project_id, root_dir=root).project.asset_count == 1

        # A v1 JSON workspace must reopen through SQLite and gain an undo chain on its next write.
        legacy_manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
        legacy_manifest["schema_version"] = 1
        legacy_manifest.pop("history_events")
        legacy_manifest.pop("masks")
        legacy_manifest.pop("layers")
        legacy_manifest["assets"][0].pop("undo_revision_ids")
        legacy_manifest["assets"][0].pop("redo_revision_ids")
        for legacy_revision in legacy_manifest["revisions"]:
            legacy_revision.pop("mask_id")
            legacy_revision.pop("layer_id")
        delete_media_workspace_manifest(project.project_id)
        (project_dir / "manifest.json").write_text(
            json.dumps(legacy_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        legacy_reopened = list_media_asset_revisions(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            root_dir=root,
        )
        assert not legacy_reopened.asset.undo_available
        assert not legacy_reopened.asset.redo_available
        assert not (project_dir / "manifest.json").exists()
        assert (project_dir / "manifest.sqlite-migrated.json").is_file()
        assert load_media_workspace_manifest(project.project_id)["schema_version"] == 7

        initial_path = resolve_media_revision_preview_path(
            project_id=project.project_id,
            revision_id=asset.current_revision_id,
            root_dir=root,
        )
        with Image.open(initial_path) as initial:
            assert initial.format == "PNG"
            assert initial.size == (320, 180)

        adjusted = create_media_image_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=asset.current_revision_id,
            operation="adjust_color",
            parameters={"brightness": 20, "contrast": -10, "saturation": -25},
            root_dir=root,
        )
        resized = create_media_image_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=adjusted.revision_id,
            operation="resize",
            parameters={"width": 160, "height": 90},
            root_dir=root,
        )
        cropped = create_media_image_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=resized.revision_id,
            operation="crop",
            parameters={"x": 20, "y": 10, "width": 100, "height": 60},
            root_dir=root,
        )
        rotated = create_media_image_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=cropped.revision_id,
            operation="rotate_right",
            root_dir=root,
        )
        grayscale = create_media_image_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=rotated.revision_id,
            operation="grayscale",
            root_dir=root,
        )
        assert adjusted.parent_revision_id == asset.current_revision_id
        assert adjusted.parameters == {"brightness": 20, "contrast": -10, "saturation": -25}
        assert (adjusted.width, adjusted.height) == (320, 180)
        with Image.open(initial_path) as initial, Image.open(
            resolve_media_revision_preview_path(
                project_id=project.project_id,
                revision_id=adjusted.revision_id,
                root_dir=root,
            )
        ) as adjusted_image:
            assert initial.getpixel((20, 20)) != adjusted_image.getpixel((20, 20))
            assert adjusted_image.getpixel((160, 90))[3] == initial.getpixel((160, 90))[3]
        assert resized.parent_revision_id == adjusted.revision_id
        assert (resized.width, resized.height) == (160, 90)
        assert resized.parameters == {"width": 160, "height": 90}
        assert cropped.parent_revision_id == resized.revision_id
        assert (cropped.width, cropped.height) == (100, 60)
        assert cropped.parameters == {"height": 60, "width": 100, "x": 20, "y": 10}
        assert rotated.parent_revision_id == cropped.revision_id
        assert rotated.width == 60 and rotated.height == 100
        assert grayscale.parent_revision_id == rotated.revision_id
        revisions = list_media_asset_revisions(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            root_dir=root,
        )
        assert [item.operation for item in revisions.revisions] == [
            "import",
            "adjust_color",
            "resize",
            "crop",
            "rotate_right",
            "grayscale",
        ]
        assert revisions.asset.current_revision_id == grayscale.revision_id
        assert revisions.asset.undo_available
        assert not revisions.asset.redo_available
        assert source_path.read_bytes() == raw

        undone = navigate_media_image_history(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            action="undo",
            base_revision_id=grayscale.revision_id,
            root_dir=root,
        )
        assert undone.asset.current_revision_id == rotated.revision_id
        assert undone.asset.undo_available and undone.asset.redo_available
        redone = navigate_media_image_history(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            action="redo",
            base_revision_id=rotated.revision_id,
            root_dir=root,
        )
        assert redone.asset.current_revision_id == grayscale.revision_id
        assert redone.asset.undo_available and not redone.asset.redo_available
        persisted_history = load_media_workspace_manifest(project.project_id)
        assert persisted_history["schema_version"] == 7
        assert [event["action"] for event in persisted_history["history_events"]] == ["undo", "redo"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_futures = [
                executor.submit(
                    create_media_image_revision,
                    project_id=project.project_id,
                    asset_id=asset.asset_id,
                    base_revision_id=grayscale.revision_id,
                    operation=operation,
                    root_dir=root,
                )
                for operation in ("rotate_left", "flip_horizontal")
            ]
        concurrent_revisions = []
        concurrent_conflicts = []
        for future in concurrent_futures:
            try:
                concurrent_revisions.append(future.result())
            except MediaWorkspaceConflictError as exc:
                concurrent_conflicts.append(exc)
        assert len(concurrent_revisions) == 1
        assert len(concurrent_conflicts) == 1
        concurrent_history = list_media_asset_revisions(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            root_dir=root,
        ).revisions
        assert len(concurrent_history) == 7
        assert concurrent_history[-1].parent_revision_id == grayscale.revision_id

        concurrent_undo = navigate_media_image_history(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            action="undo",
            base_revision_id=concurrent_history[-1].revision_id,
            root_dir=root,
        )
        assert concurrent_undo.asset.current_revision_id == grayscale.revision_id
        concurrent_redo = navigate_media_image_history(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            action="redo",
            base_revision_id=grayscale.revision_id,
            root_dir=root,
        )
        assert concurrent_redo.asset.current_revision_id == concurrent_history[-1].revision_id

        exported = export_media_image_revision(
            project_id=project.project_id,
            revision_id=grayscale.revision_id,
            filename="presentation-cover.jpg",
            root_dir=root,
            export_root=exports,
        )
        assert exported.filename == "presentation-cover.png"
        export_path, download_name = resolve_media_export_download_path(
            project_id=project.project_id,
            export_id=exported.export_id,
            root_dir=root,
            export_root=exports,
        )
        assert download_name == "presentation-cover.png"
        assert export_path.is_file()
        assert sha256(export_path.read_bytes()).hexdigest() == exported.sha256
        with Image.open(export_path) as image:
            assert image.format == "PNG"
            assert image.size == (60, 100)

        _expect_workspace_error(
            lambda: create_media_image_revision(
                project_id=project.project_id,
                asset_id=asset.asset_id,
                base_revision_id=concurrent_history[-1].revision_id,
                operation="adjust_color",
                parameters={},
                root_dir=root,
            )
        )
        _expect_workspace_error(
            lambda: create_media_image_revision(
                project_id=project.project_id,
                asset_id=asset.asset_id,
                base_revision_id=concurrent_history[-1].revision_id,
                operation="crop",
                parameters={"x": 90, "y": 50, "width": 20, "height": 20},
                root_dir=root,
            )
        )
        _expect_workspace_error(
            lambda: create_media_image_revision(
                project_id=project.project_id,
                asset_id=asset.asset_id,
                base_revision_id=concurrent_history[-1].revision_id,
                operation="resize",
                parameters={"width": 10_000, "height": 10_000},
                root_dir=root,
            )
        )

        _expect_workspace_error(
            lambda: import_media_image_base64(
                project_id=project.project_id,
                filename="../outside.png",
                content_base64=base64.b64encode(raw).decode("ascii"),
                root_dir=root,
            )
        )
        _expect_workspace_error(
            lambda: export_media_image_revision(
                project_id=project.project_id,
                revision_id=grayscale.revision_id,
                filename="../outside.png",
                root_dir=root,
                export_root=exports,
            )
        )

        masked = create_media_image_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=concurrent_history[-1].revision_id,
            operation="apply_rect_mask",
            parameters={"x": 10, "y": 10, "width": 20, "height": 20},
            root_dir=root,
        )
        assert masked.mask_id is not None
        manifest_with_mask = load_media_workspace_manifest(project.project_id)
        assert len(manifest_with_mask["masks"]) == 1
        mask_record = manifest_with_mask["masks"][0]
        assert mask_record["mask_id"] == masked.mask_id
        assert mask_record["base_revision_id"] == concurrent_history[-1].revision_id
        mask_path = project_dir / mask_record["file"]
        with Image.open(mask_path) as mask_image:
            assert mask_image.format == "PNG"
            assert mask_image.mode == "L"
            assert mask_image.size == (concurrent_history[-1].width, concurrent_history[-1].height)
            assert mask_image.getpixel((0, 0)) == 0
            assert mask_image.getpixel((10, 10)) == 255
        parent_preview = resolve_media_revision_preview_path(
            project_id=project.project_id,
            revision_id=concurrent_history[-1].revision_id,
            root_dir=root,
        )
        masked_preview = resolve_media_revision_preview_path(
            project_id=project.project_id,
            revision_id=masked.revision_id,
            root_dir=root,
        )
        with Image.open(parent_preview) as parent_image, Image.open(masked_preview) as masked_image:
            assert masked_image.mode == "RGBA"
            assert masked_image.getpixel((0, 0))[3] == 0
            assert masked_image.getpixel((15, 15))[3] == parent_image.convert("RGBA").getpixel((15, 15))[3]
        masked_undo = navigate_media_image_history(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            action="undo",
            base_revision_id=masked.revision_id,
            root_dir=root,
        )
        assert masked_undo.asset.current_revision_id == concurrent_history[-1].revision_id
        masked_redo = navigate_media_image_history(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            action="redo",
            base_revision_id=concurrent_history[-1].revision_id,
            root_dir=root,
        )
        assert masked_redo.asset.current_revision_id == masked.revision_id

        assert [item.project_id for item in list_media_projects(root_dir=root)] == [project.project_id]

        layer_project = create_media_project(title="栅格图层验收", root_dir=root)
        layer_base = import_media_image_base64(
            project_id=layer_project.project_id,
            filename="layer-base.png",
            content_base64=base64.b64encode(_fixture_png()).decode("ascii"),
            root_dir=root,
        )
        layer_source = import_media_image_base64(
            project_id=layer_project.project_id,
            filename="layer-source.png",
            content_base64=base64.b64encode(_fixture_overlay_png()).decode("ascii"),
            root_dir=root,
        )
        layered = create_media_image_revision(
            project_id=layer_project.project_id,
            asset_id=layer_base.asset_id,
            base_revision_id=layer_base.current_revision_id,
            operation="composite_raster_layer",
            parameters={"x": 10, "y": 10, "opacity": 100},
            layer_source_asset_id=layer_source.asset_id,
            root_dir=root,
        )
        assert layered.layer_id is not None
        layer_manifest = load_media_workspace_manifest(layer_project.project_id)
        assert len(layer_manifest["layers"]) == 1
        layer_record = layer_manifest["layers"][0]
        assert layer_record["layer_id"] == layered.layer_id
        assert layer_record["base_revision_id"] == layer_base.current_revision_id
        assert layer_record["source_asset_id"] == layer_source.asset_id
        assert layer_record["source_revision_id"] == layer_source.current_revision_id
        layer_base_preview = resolve_media_revision_preview_path(
            project_id=layer_project.project_id,
            revision_id=layer_base.current_revision_id,
            root_dir=root,
        )
        layered_preview = resolve_media_revision_preview_path(
            project_id=layer_project.project_id,
            revision_id=layered.revision_id,
            root_dir=root,
        )
        with Image.open(layer_base_preview) as base_image, Image.open(layered_preview) as layered_image:
            assert layered_image.mode == "RGBA"
            assert layered_image.getpixel((0, 0)) == base_image.convert("RGBA").getpixel((0, 0))
            assert layered_image.getpixel((10, 10)) == (213, 61, 74, 255)
        first_stack = get_media_image_layer_stack(
            project_id=layer_project.project_id,
            asset_id=layer_base.asset_id,
            revision_id=layered.revision_id,
            root_dir=root,
        )
        assert first_stack.editable and len(first_stack.layers) == 1
        assert first_stack.layers[0].visible
        top_source = import_media_image_base64(
            project_id=layer_project.project_id,
            filename="layer-top.png",
            content_base64=base64.b64encode(_fixture_top_overlay_png()).decode("ascii"),
            root_dir=root,
        )
        layered_twice = create_media_image_revision(
            project_id=layer_project.project_id,
            asset_id=layer_base.asset_id,
            base_revision_id=layered.revision_id,
            operation="composite_raster_layer",
            parameters={"x": 10, "y": 10, "opacity": 100},
            layer_source_asset_id=top_source.asset_id,
            root_dir=root,
        )
        two_layer_stack = get_media_image_layer_stack(
            project_id=layer_project.project_id,
            asset_id=layer_base.asset_id,
            revision_id=layered_twice.revision_id,
            root_dir=root,
        )
        assert [item.layer_id for item in two_layer_stack.layers] == [
            layered.layer_id,
            layered_twice.layer_id,
        ]
        twice_preview = resolve_media_revision_preview_path(
            project_id=layer_project.project_id,
            revision_id=layered_twice.revision_id,
            root_dir=root,
        )
        with Image.open(twice_preview) as image:
            assert image.getpixel((10, 10)) == (41, 184, 96, 255)
        reordered = create_media_image_revision(
            project_id=layer_project.project_id,
            asset_id=layer_base.asset_id,
            base_revision_id=layered_twice.revision_id,
            operation="recompose_raster_layers",
            parameters={
                "layer_stack": [
                    {"layer_id": layered_twice.layer_id, "visible": True},
                    {"layer_id": layered.layer_id, "visible": True},
                ]
            },
            root_dir=root,
        )
        reordered_preview = resolve_media_revision_preview_path(
            project_id=layer_project.project_id,
            revision_id=reordered.revision_id,
            root_dir=root,
        )
        with Image.open(reordered_preview) as image:
            assert image.getpixel((10, 10)) == (213, 61, 74, 255)
        hidden_top = create_media_image_revision(
            project_id=layer_project.project_id,
            asset_id=layer_base.asset_id,
            base_revision_id=reordered.revision_id,
            operation="recompose_raster_layers",
            parameters={
                "layer_stack": [
                    {"layer_id": layered_twice.layer_id, "visible": False},
                    {"layer_id": layered.layer_id, "visible": True},
                ]
            },
            root_dir=root,
        )
        hidden_stack = get_media_image_layer_stack(
            project_id=layer_project.project_id,
            asset_id=layer_base.asset_id,
            revision_id=hidden_top.revision_id,
            root_dir=root,
        )
        assert [item.visible for item in hidden_stack.layers] == [False, True]
        hidden_preview = resolve_media_revision_preview_path(
            project_id=layer_project.project_id,
            revision_id=hidden_top.revision_id,
            root_dir=root,
        )
        with Image.open(hidden_preview) as image:
            assert image.getpixel((10, 10)) == (213, 61, 74, 255)
        with Image.open(twice_preview) as image:
            assert image.getpixel((10, 10)) == (41, 184, 96, 255)
        _expect_workspace_error(
            lambda: create_media_image_revision(
                project_id=layer_project.project_id,
                asset_id=layer_base.asset_id,
                base_revision_id=hidden_top.revision_id,
                operation="recompose_raster_layers",
                parameters={"layer_stack": [{"layer_id": layered.layer_id, "visible": True}]},
                root_dir=root,
            )
        )
        changed_layer_source = create_media_image_revision(
            project_id=layer_project.project_id,
            asset_id=layer_source.asset_id,
            base_revision_id=layer_source.current_revision_id,
            operation="grayscale",
            root_dir=root,
        )
        assert changed_layer_source.revision_id != layer_record["source_revision_id"]
        with Image.open(layered_preview) as layered_image:
            assert layered_image.getpixel((10, 10)) == (213, 61, 74, 255)
        _expect_workspace_error(
            lambda: create_media_image_revision(
                project_id=layer_project.project_id,
                asset_id=layer_base.asset_id,
                base_revision_id=layered.revision_id,
                operation="composite_raster_layer",
                parameters={"x": 0, "y": 0, "opacity": 100},
                layer_source_asset_id=layer_base.asset_id,
                root_dir=root,
            )
        )

        webp_project = create_media_project(title="WebP 格式验收", root_dir=root)
        webp_asset = import_media_image_base64(
            project_id=webp_project.project_id,
            filename="reference.webp",
            content_base64=base64.b64encode(_fixture_webp()).decode("ascii"),
            root_dir=root,
        )
        assert webp_asset.mime_type == "image/webp"
        webp_preview = resolve_media_revision_preview_path(
            project_id=webp_project.project_id,
            revision_id=webp_asset.current_revision_id,
            root_dir=root,
        )
        with Image.open(webp_preview) as image:
            assert image.format == "PNG"
            assert image.size == (96, 64)
        _expect_workspace_error(
            lambda: navigate_media_image_history(
                project_id=webp_project.project_id,
                asset_id=webp_asset.asset_id,
                action="undo",
                base_revision_id=webp_asset.current_revision_id,
                root_dir=root,
            )
        )
        webp_rotated = create_media_image_revision(
            project_id=webp_project.project_id,
            asset_id=webp_asset.asset_id,
            base_revision_id=webp_asset.current_revision_id,
            operation="rotate_left",
            root_dir=root,
        )
        assert webp_rotated.parent_revision_id == webp_asset.current_revision_id
        webp_undone = navigate_media_image_history(
            project_id=webp_project.project_id,
            asset_id=webp_asset.asset_id,
            action="undo",
            base_revision_id=webp_rotated.revision_id,
            root_dir=root,
        )
        assert webp_undone.asset.current_revision_id == webp_asset.current_revision_id
        assert webp_undone.asset.redo_available
        webp_branch = create_media_image_revision(
            project_id=webp_project.project_id,
            asset_id=webp_asset.asset_id,
            base_revision_id=webp_asset.current_revision_id,
            operation="grayscale",
            root_dir=root,
        )
        assert webp_branch.parent_revision_id == webp_asset.current_revision_id
        webp_after_branch = list_media_asset_revisions(
            project_id=webp_project.project_id,
            asset_id=webp_asset.asset_id,
            root_dir=root,
        )
        assert webp_after_branch.asset.current_revision_id == webp_branch.revision_id
        assert not webp_after_branch.asset.redo_available
        _expect_workspace_error(
            lambda: navigate_media_image_history(
                project_id=webp_project.project_id,
                asset_id=webp_asset.asset_id,
                action="redo",
                base_revision_id=webp_branch.revision_id,
                root_dir=root,
            )
        )

        # A metadata-write failure must not leave a file tree that SQLite cannot describe.
        projects_before_failure = {item.name for item in (root / "projects").iterdir()}
        _expect_manifest_persistence_failure(
            lambda: create_media_project(title="SQLite failure cleanup", root_dir=root)
        )
        assert {item.name for item in (root / "projects").iterdir()} == projects_before_failure

        failure_project = create_media_project(title="SQLite cleanup fixture", root_dir=root)
        failure_dir = root / "projects" / failure_project.project_id
        _expect_manifest_persistence_failure(
            lambda: import_media_image_base64(
                project_id=failure_project.project_id,
                filename="cleanup.png",
                content_base64=base64.b64encode(raw).decode("ascii"),
                root_dir=root,
            )
        )
        assert not list((failure_dir / "sources").iterdir())
        assert not list((failure_dir / "revisions").iterdir())
        assert get_media_project(failure_project.project_id, root_dir=root).project.asset_count == 0

        failure_asset = import_media_image_base64(
            project_id=failure_project.project_id,
            filename="cleanup.png",
            content_base64=base64.b64encode(raw).decode("ascii"),
            root_dir=root,
        )
        registered_revision_files = {item.name for item in (failure_dir / "revisions").iterdir()}
        registered_mask_files = {item.name for item in (failure_dir / "masks").iterdir()}
        _expect_manifest_persistence_failure(
            lambda: create_media_image_revision(
                project_id=failure_project.project_id,
                asset_id=failure_asset.asset_id,
                base_revision_id=failure_asset.current_revision_id,
                operation="apply_rect_mask",
                parameters={"x": 10, "y": 10, "width": 20, "height": 20},
                root_dir=root,
            )
        )
        assert {item.name for item in (failure_dir / "masks").iterdir()} == registered_mask_files
        assert {item.name for item in (failure_dir / "revisions").iterdir()} == registered_revision_files
        _expect_manifest_persistence_failure(
            lambda: create_media_image_revision(
                project_id=failure_project.project_id,
                asset_id=failure_asset.asset_id,
                base_revision_id=failure_asset.current_revision_id,
                operation="rotate_left",
                root_dir=root,
            )
        )
        assert {item.name for item in (failure_dir / "revisions").iterdir()} == registered_revision_files
        assert (
            list_media_asset_revisions(
                project_id=failure_project.project_id,
                asset_id=failure_asset.asset_id,
                root_dir=root,
            ).asset.current_revision_id
            == failure_asset.current_revision_id
        )

        failure_exports = Path(temporary) / "failed-exports"
        _expect_manifest_persistence_failure(
            lambda: export_media_image_revision(
                project_id=failure_project.project_id,
                revision_id=failure_asset.current_revision_id,
                filename="cleanup.png",
                root_dir=root,
                export_root=failure_exports,
            )
        )
        assert not list(failure_exports.rglob("*.png"))

    print("media workspace verification passed: revision-history/export/readback/path-boundary")


if __name__ == "__main__":
    main()
