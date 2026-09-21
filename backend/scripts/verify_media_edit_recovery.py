"""验证图片编辑在服务重启后的定向对账。

夹具模拟 PNG 修订和项目元数据已经原子提交，但 Runtime 终态尚未写入的窗口。恢复器只能按
修订内冻结的 task_id、SHA-256 与 PNG 回读补齐完成态；找不到有效修订的 pending task 必须
失败，不能自动重放已经可能过期的编辑命令。
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_edit_recovery_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-edit-recovery-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import list_workflow_artifacts, load_task_log_events, list_workflow_tool_calls
from app.schemas.media_workspace import MediaImageOperationRequest
from app.services.media_edit_delivery import create_media_edit_queued_run, get_media_edit_task_result
from app.services.media_workspace import (
    create_media_image_revision,
    create_media_project,
    import_media_image_base64,
    resolve_media_revision_preview_path,
)
from main import create_app


def _fixture_png() -> bytes:
    image = Image.new("RGBA", (96, 64), (39, 111, 190, 255))
    image.paste((241, 177, 52, 255), (20, 16, 76, 48))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> None:
    try:
        project = create_media_project(title="编辑恢复夹具")
        asset = import_media_image_base64(
            project_id=project.project_id,
            filename="source.png",
            content_base64=base64.b64encode(_fixture_png()).decode("ascii"),
        )

        recovered_task_id = "task_media_edit_0123456789ab"
        recovered_request = MediaImageOperationRequest(
            operation="rotate_left",
            base_revision_id=asset.current_revision_id,
        )
        create_media_edit_queued_run(
            task_id=recovered_task_id,
            project_id=project.project_id,
            asset_id=asset.asset_id,
            request=recovered_request,
        )
        # 模拟修订 PNG 和 SQLite manifest 已提交，但进程在 Runtime 完成态写入之前退出。
        submitted_revision = create_media_image_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=asset.current_revision_id,
            operation=recovered_request.operation,
            parameters=recovered_request.operation_parameters(),
            task_id=recovered_task_id,
        )

        missing_task_id = "task_media_edit_abcdef123456"
        create_media_edit_queued_run(
            task_id=missing_task_id,
            project_id=project.project_id,
            asset_id=asset.asset_id,
            request=MediaImageOperationRequest(
                operation="grayscale",
                base_revision_id=submitted_revision.revision_id,
            ),
        )

        # 用真实 FastAPI lifespan 验证应用启动钩子，而不是只调用恢复 helper。
        with TestClient(create_app()) as client:
            assert client.app.state.recovered_media_edit_task_count == 2

        recovered_result = get_media_edit_task_result(recovered_task_id)
        assert recovered_result is not None and recovered_result.status == "completed"
        assert recovered_result.revision is not None
        assert recovered_result.revision.revision_id == submitted_revision.revision_id
        assert recovered_result.revision.sha256 == submitted_revision.sha256
        assert list_workflow_artifacts(recovered_task_id) == []
        tool_calls = list_workflow_tool_calls(recovered_task_id)
        assert len(tool_calls) == 1 and tool_calls[0].result["verification_passed"] is True
        assert resolve_media_revision_preview_path(
            project_id=project.project_id,
            revision_id=submitted_revision.revision_id,
        ).is_file()
        recovered_events = load_task_log_events(recovered_task_id) or []
        assert recovered_events[-1].event == "task_reconciled_after_restart"

        missing_result = get_media_edit_task_result(missing_task_id)
        assert missing_result is not None and missing_result.status == "failed"
        assert missing_result.revision is None
        missing_events = load_task_log_events(missing_task_id) or []
        assert missing_events[-1].event == "task_interrupted_by_restart"
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
    print("media edit recovery verification passed: committed-revision reconciliation / no-output no-retry")


if __name__ == "__main__":
    main()
