"""通过 FastAPI 验证图片工作区对桌面端暴露的完整接口。"""

from __future__ import annotations

import asyncio
import base64
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_workspace_api_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from main import create_app
from app.database.media_workspace_repository import load_media_workspace_manifest
from app.database.task_repository import list_workflow_artifacts
from app.schemas.media_workspace import MediaImageExportRequest, MediaImageOperationRequest
from app.services.media_edit_delivery import (
    cancel_media_edit_task,
    create_media_edit_queued_run,
    run_media_edit_task,
)
from app.services.media_export_delivery import (
    cancel_media_export_task,
    create_media_export_queued_run,
    run_media_export_task,
)


def _fixture_png_base64() -> str:
    image = Image.new("RGB", (144, 96), (51, 106, 178))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _fixture_overlay_png_base64() -> str:
    image = Image.new("RGBA", (18, 14), (213, 61, 74, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def main() -> None:
    try:
        with TestClient(create_app()) as client:
            created = client.post("/api/agents/media_agent/projects", json={"title": "接口验收项目"})
            assert created.status_code == 201, created.text
            project = created.json()
            project_id = project["project_id"]

            imported = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images",
                json={"filename": "sample.png", "content_base64": _fixture_png_base64()},
            )
            assert imported.status_code == 201, imported.text
            asset = imported.json()

            revised = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={"operation": "rotate_left", "base_revision_id": asset["current_revision_id"]},
            )
            assert revised.status_code == 201, revised.text
            revision = revised.json()
            assert (revision["width"], revision["height"]) == (96, 144)

            adjusted = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={
                    "operation": "adjust_color",
                    "base_revision_id": revision["revision_id"],
                    "brightness": 15,
                    "contrast": -10,
                    "saturation": -20,
                },
            )
            assert adjusted.status_code == 201, adjusted.text
            adjusted_revision = adjusted.json()
            assert adjusted_revision["parameters"] == {"brightness": 15, "contrast": -10, "saturation": -20}

            resized = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={
                    "operation": "resize",
                    "base_revision_id": adjusted_revision["revision_id"],
                    "resize_width": 60,
                    "resize_height": 90,
                },
            )
            assert resized.status_code == 201, resized.text
            resized_revision = resized.json()
            assert (resized_revision["width"], resized_revision["height"]) == (60, 90)
            assert resized_revision["parameters"] == {"width": 60, "height": 90}

            cropped = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={
                    "operation": "crop",
                    "base_revision_id": resized_revision["revision_id"],
                    "crop_x": 5,
                    "crop_y": 6,
                    "crop_width": 40,
                    "crop_height": 50,
                },
            )
            assert cropped.status_code == 201, cropped.text
            revision = cropped.json()
            assert (revision["width"], revision["height"]) == (40, 50)
            assert revision["parameters"] == {"x": 5, "y": 6, "width": 40, "height": 50}

            stale_edit = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={"operation": "flip_horizontal", "base_revision_id": resized_revision["revision_id"]},
            )
            assert stale_edit.status_code == 409, stale_edit.text

            stale_history = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/history/undo",
                json={"base_revision_id": resized_revision["revision_id"]},
            )
            assert stale_history.status_code == 409, stale_history.text

            undone = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/history/undo",
                json={"base_revision_id": revision["revision_id"]},
            )
            assert undone.status_code == 200, undone.text
            undone_asset = undone.json()["asset"]
            assert undone_asset["current_revision_id"] == resized_revision["revision_id"]
            assert undone_asset["undo_available"] and undone_asset["redo_available"]

            redone = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/history/redo",
                json={"base_revision_id": resized_revision["revision_id"]},
            )
            assert redone.status_code == 200, redone.text
            redone_asset = redone.json()["asset"]
            assert redone_asset["current_revision_id"] == revision["revision_id"]
            assert redone_asset["undo_available"] and not redone_asset["redo_available"]

            exhausted_redo = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/history/redo",
                json={"base_revision_id": revision["revision_id"]},
            )
            assert exhausted_redo.status_code == 400, exhausted_redo.text

            masked = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={
                    "operation": "apply_rect_mask",
                    "base_revision_id": revision["revision_id"],
                    "mask_x": 5,
                    "mask_y": 6,
                    "mask_width": 20,
                    "mask_height": 25,
                },
            )
            assert masked.status_code == 201, masked.text
            revision = masked.json()
            assert revision["parameters"] == {"height": 25, "width": 20, "x": 5, "y": 6}
            assert revision["mask_id"].startswith("mm_")

            overlay_imported = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images",
                json={"filename": "overlay.png", "content_base64": _fixture_overlay_png_base64()},
            )
            assert overlay_imported.status_code == 201, overlay_imported.text
            overlay_asset = overlay_imported.json()
            layered = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={
                    "operation": "composite_raster_layer",
                    "base_revision_id": revision["revision_id"],
                    "overlay_asset_id": overlay_asset["asset_id"],
                    "layer_x": 10,
                    "layer_y": 10,
                    "layer_opacity": 100,
                },
            )
            assert layered.status_code == 201, layered.text
            revision = layered.json()
            assert revision["parameters"] == {"opacity": 100, "x": 10, "y": 10}
            assert revision["layer_id"].startswith("ml_")

            preview = client.get(
                f"/api/agents/media_agent/projects/{project_id}/revisions/{revision['revision_id']}/preview"
            )
            assert preview.status_code == 200, preview.text
            assert preview.headers["content-type"].startswith("image/png")
            with Image.open(io.BytesIO(preview.content)) as image:
                assert image.size == (40, 50)
                assert image.mode == "RGBA"
                assert image.getpixel((0, 0))[3] == 0
                assert image.getpixel((10, 10))[3] == 255
                assert image.getpixel((10, 10)) == (213, 61, 74, 255)

            layer_stack = client.get(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}"
                f"/revisions/{revision['revision_id']}/layers"
            )
            assert layer_stack.status_code == 200, layer_stack.text
            assert layer_stack.json()["editable"] is True
            assert len(layer_stack.json()["layers"]) == 1
            layer_id = layer_stack.json()["layers"][0]["layer_id"]
            recompose_started = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions/start",
                json={
                    "operation": "recompose_raster_layers",
                    "base_revision_id": revision["revision_id"],
                    "layer_stack": [{"layer_id": layer_id, "visible": False}],
                },
            )
            assert recompose_started.status_code == 202, recompose_started.text
            recompose_result = None
            for _ in range(100):
                response = client.get(
                    f"/api/agents/media_agent/edits/{recompose_started.json()['task_id']}/result"
                )
                assert response.status_code == 200, response.text
                recompose_result = response.json()
                if recompose_result["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            assert recompose_result is not None and recompose_result["status"] == "completed", recompose_result
            revision = recompose_result["revision"]
            assert revision is not None and revision["operation"] == "recompose_raster_layers"
            hidden_stack = client.get(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}"
                f"/revisions/{revision['revision_id']}/layers"
            )
            assert hidden_stack.status_code == 200, hidden_stack.text
            assert hidden_stack.json()["layers"][0]["visible"] is False

            pre_edit_revision = revision
            edit_started = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions/start",
                json={"operation": "flip_horizontal", "base_revision_id": pre_edit_revision["revision_id"]},
            )
            assert edit_started.status_code == 202, edit_started.text
            edit_task_id = edit_started.json()["task_id"]
            edit_result = None
            for _ in range(100):
                response = client.get(f"/api/agents/media_agent/edits/{edit_task_id}/result")
                assert response.status_code == 200, response.text
                edit_result = response.json()
                if edit_result["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            assert edit_result is not None and edit_result["status"] == "completed", edit_result
            task_revision = edit_result["revision"]
            assert task_revision is not None
            assert task_revision["parent_revision_id"] == pre_edit_revision["revision_id"]

            unified_edit_task = client.get(f"/api/tasks/{edit_task_id}")
            assert unified_edit_task.status_code == 200, unified_edit_task.text
            edit_payload = unified_edit_task.json()
            assert edit_payload["mode"] == "runtime"
            assert edit_payload["status"] == "completed"
            assert edit_payload["steps"][0]["action"] == "media.edit_image"
            assert edit_payload["steps"][0]["output"]["revision"]["revision_id"] == task_revision["revision_id"]

            edit_tool_calls = client.get(f"/api/tasks/{edit_task_id}/tool-calls")
            assert edit_tool_calls.status_code == 200, edit_tool_calls.text
            assert edit_tool_calls.json()["tool_calls"][0]["tool_name"] == "media.edit_image"
            assert edit_tool_calls.json()["tool_calls"][0]["result"]["verification_passed"] is True

            stale_edit_started = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions/start",
                json={"operation": "grayscale", "base_revision_id": pre_edit_revision["revision_id"]},
            )
            assert stale_edit_started.status_code == 202, stale_edit_started.text
            stale_edit_result = None
            for _ in range(100):
                response = client.get(f"/api/agents/media_agent/edits/{stale_edit_started.json()['task_id']}/result")
                assert response.status_code == 200, response.text
                stale_edit_result = response.json()
                if stale_edit_result["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            assert stale_edit_result is not None and stale_edit_result["status"] == "failed", stale_edit_result
            assert stale_edit_result["conflict"] is True
            revision = task_revision

            exported = client.post(
                f"/api/agents/media_agent/projects/{project_id}/revisions/{revision['revision_id']}/export",
                json={"filename": "slide-cover.jpg"},
            )
            assert exported.status_code == 201, exported.text
            export = exported.json()
            assert export["filename"] == "slide-cover.png"
            download = client.get(
                f"/api/agents/media_agent/projects/{project_id}/exports/{export['export_id']}/download"
            )
            assert download.status_code == 200, download.text
            assert download.headers["content-type"].startswith("image/png")

            task_started = client.post(
                f"/api/agents/media_agent/projects/{project_id}/revisions/{revision['revision_id']}/export/start",
                json={"filename": "task-delivery.png"},
            )
            assert task_started.status_code == 202, task_started.text
            media_task_id = task_started.json()["task_id"]
            task_result = None
            for _ in range(100):
                response = client.get(f"/api/agents/media_agent/exports/{media_task_id}/result")
                assert response.status_code == 200, response.text
                task_result = response.json()
                if task_result["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            assert task_result is not None and task_result["status"] == "completed", task_result
            task_export = task_result["export"]
            assert task_export is not None and task_export["filename"] == "task-delivery.png"

            unified_task = client.get(f"/api/tasks/{media_task_id}")
            assert unified_task.status_code == 200, unified_task.text
            task_payload = unified_task.json()
            assert task_payload["mode"] == "runtime"
            assert task_payload["status"] == "completed"
            assert task_payload["steps"][0]["action"] == "media.export_png"
            assert task_payload["steps"][0]["output"]["export"]["export_id"] == task_export["export_id"]

            artifacts = client.get(f"/api/tasks/{media_task_id}/artifacts")
            assert artifacts.status_code == 200, artifacts.text
            artifact_payload = artifacts.json()["artifacts"]
            assert len(artifact_payload) == 1
            assert artifact_payload[0]["metadata"]["output_scope"] == "media_exports"
            assert artifact_payload[0]["metadata"]["output_path"] == "<hidden>"
            assert artifact_payload[0]["uri"].startswith("agentflow-output://media_exports/")

            tool_calls = client.get(f"/api/tasks/{media_task_id}/tool-calls")
            assert tool_calls.status_code == 200, tool_calls.text
            assert tool_calls.json()["tool_calls"][0]["tool_name"] == "media.export_png"
            assert tool_calls.json()["tool_calls"][0]["status"] == "completed"

            task_download = client.get(
                f"/api/agents/media_agent/projects/{project_id}/exports/{task_export['export_id']}/download"
            )
            assert task_download.status_code == 200, task_download.text

            # 在后台启动前取消一个已入队任务：不应生成导出文件，也不应留下 Artifact。
            cancelled_task_id = "task_media_export_0123456789ab"
            exports_before_cancel = len(load_media_workspace_manifest(project_id)["exports"])
            create_media_export_queued_run(
                task_id=cancelled_task_id,
                project_id=project_id,
                revision_id=revision["revision_id"],
                request=MediaImageExportRequest(filename="cancelled.png"),
            )
            cancel_response = asyncio.run(cancel_media_export_task(cancelled_task_id))
            assert cancel_response is not None and cancel_response.accepted
            cancelled_result = asyncio.run(
                run_media_export_task(
                    task_id=cancelled_task_id,
                    project_id=project_id,
                    revision_id=revision["revision_id"],
                    request=MediaImageExportRequest(filename="cancelled.png"),
                )
            )
            assert cancelled_result.status == "cancelled"
            assert list_workflow_artifacts(cancelled_task_id) == []
            assert len(load_media_workspace_manifest(project_id)["exports"]) == exports_before_cancel

            # 编辑任务只允许在写入前取消，取消后不能新增修订或遗留伪终态。
            cancelled_edit_task_id = "task_media_edit_0123456789ab"
            revisions_before_cancel = len(load_media_workspace_manifest(project_id)["revisions"])
            cancelled_edit_request = MediaImageOperationRequest(
                operation="grayscale",
                base_revision_id=revision["revision_id"],
            )
            create_media_edit_queued_run(
                task_id=cancelled_edit_task_id,
                project_id=project_id,
                asset_id=asset["asset_id"],
                request=cancelled_edit_request,
            )
            cancelled_edit_response = asyncio.run(cancel_media_edit_task(cancelled_edit_task_id))
            assert cancelled_edit_response is not None and cancelled_edit_response.accepted
            cancelled_edit_result = asyncio.run(
                run_media_edit_task(
                    task_id=cancelled_edit_task_id,
                    project_id=project_id,
                    asset_id=asset["asset_id"],
                    request=cancelled_edit_request,
                )
            )
            assert cancelled_edit_result.status == "cancelled"
            assert len(load_media_workspace_manifest(project_id)["revisions"]) == revisions_before_cancel

            invalid_adjustment = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={"operation": "adjust_color"},
            )
            assert invalid_adjustment.status_code == 422, invalid_adjustment.text
            invalid_crop = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={"operation": "crop", "crop_x": 1, "crop_y": 1},
            )
            assert invalid_crop.status_code == 422, invalid_crop.text
            invalid_layer = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images/{asset['asset_id']}/revisions",
                json={
                    "operation": "composite_raster_layer",
                    "base_revision_id": revision["revision_id"],
                    "overlay_asset_id": overlay_asset["asset_id"],
                },
            )
            assert invalid_layer.status_code == 422, invalid_layer.text

            rejected = client.post(
                f"/api/agents/media_agent/projects/{project_id}/images",
                json={"filename": "../invalid.png", "content_base64": _fixture_png_base64()},
            )
            assert rejected.status_code == 400, rejected.text
            missing = client.get("/api/agents/media_agent/projects/mp_0000000000000000")
            assert missing.status_code == 404, missing.text
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
    print("media workspace API verification passed: project/import/history/revisions/media-export-task/artifact/cancellation")


if __name__ == "__main__":
    main()
