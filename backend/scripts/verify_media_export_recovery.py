"""验证图片 PNG 导出在服务重启后的定向对账。

夹具刻意不执行正常的 ``run_media_export_task`` 完成路径，而是在受理任务后只提交图片
manifest/export 文件，模拟进程恰好在 Artifact 落库前退出。恢复器必须补登记真实交付物；
没有验证文件的 pending 任务必须失败而不是盲目重跑。
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

from PIL import Image
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_export_recovery_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-recovery-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import list_workflow_artifacts, load_task_log_events
from app.schemas.media_workspace import MediaImageExportRequest
from app.services.media_export_delivery import (
    create_media_export_queued_run,
    get_media_export_task_result,
)
from app.services.media_workspace import (
    create_media_project,
    export_media_image_revision,
    import_media_image_base64,
    resolve_media_export_download_path,
)
from main import create_app


def _fixture_png() -> bytes:
    image = Image.new("RGBA", (96, 64), (32, 104, 185, 255))
    image.paste((244, 181, 63, 255), (20, 16, 76, 48))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> None:
    try:
        project = create_media_project(title="导出恢复夹具")
        asset = import_media_image_base64(
            project_id=project.project_id,
            filename="source.png",
            content_base64=base64.b64encode(_fixture_png()).decode("ascii"),
        )

        recovered_task_id = "task_media_export_0123456789ab"
        recovered_request = MediaImageExportRequest(filename="recovered.png")
        create_media_export_queued_run(
            task_id=recovered_task_id,
            project_id=project.project_id,
            revision_id=asset.current_revision_id,
            request=recovered_request,
        )
        # 模拟文件和项目 manifest 已提交，但后台进程在 WorkflowRun/Artifact 完成前退出。
        submitted_export = export_media_image_revision(
            project_id=project.project_id,
            revision_id=asset.current_revision_id,
            filename=recovered_request.filename,
            task_id=recovered_task_id,
        )
        assert list_workflow_artifacts(recovered_task_id) == []

        missing_task_id = "task_media_export_abcdef123456"
        create_media_export_queued_run(
            task_id=missing_task_id,
            project_id=project.project_id,
            revision_id=asset.current_revision_id,
            request=MediaImageExportRequest(filename="missing.png"),
        )

        # 通过真实 lifespan 调用恢复器，避免只测到 helper 而漏掉应用启动钩子。
        with TestClient(create_app()) as client:
            assert client.app.state.recovered_media_export_task_count == 2

        recovered_result = get_media_export_task_result(recovered_task_id)
        assert recovered_result is not None and recovered_result.status == "completed"
        assert recovered_result.export is not None
        assert recovered_result.export.export_id == submitted_export.export_id
        assert recovered_result.export.sha256 == submitted_export.sha256
        artifacts = list_workflow_artifacts(recovered_task_id)
        assert len(artifacts) == 1
        assert artifacts[0].metadata["export_id"] == submitted_export.export_id
        assert artifacts[0].metadata["verification"]["passed"] is True
        events = load_task_log_events(recovered_task_id) or []
        assert events[-1].event == "task_reconciled_after_restart"
        path, filename = resolve_media_export_download_path(
            project_id=project.project_id,
            export_id=submitted_export.export_id,
        )
        assert path.is_file() and filename == "recovered.png"

        missing_result = get_media_export_task_result(missing_task_id)
        assert missing_result is not None and missing_result.status == "failed"
        assert missing_result.export is None
        assert list_workflow_artifacts(missing_task_id) == []
        missing_events = load_task_log_events(missing_task_id) or []
        assert missing_events[-1].event == "task_interrupted_by_restart"
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
    print("media export recovery verification passed: committed-output reconciliation / no-output no-retry")


if __name__ == "__main__":
    main()
