"""离线验证 AI 修图的受理、失败分类、revision 提交、取消与重启对账。

全部模型与图片下载调用均为内存替身，不读取本机 Qwen Key、不访问网络、不消耗额度。
这个脚本刻意验证最容易出事故的边界：Provider 未知结果不重试、迟到结果不覆盖新版本、
模型临时 URL 不进入项目 metadata，以及重启后只能按已验证 revision 对账。
"""

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
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_media_ai_edit_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / f"media-ai-edit-{uuid4().hex}.db")
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.task_repository import list_workflow_tool_calls, load_workflow_run
from app.schemas.media_workspace import MediaImageAiEditRequest, MediaImageOperationRequest
from app.services.media_ai_edit_delivery import (
    cancel_media_ai_edit_task,
    create_media_ai_edit_queued_run,
    get_media_ai_edit_task_result,
    run_media_ai_edit_task,
)
from app.services.media_edit_delivery import create_media_edit_queued_run, run_media_edit_task
from app.services.media_workspace import (
    MediaWorkspaceError,
    create_media_image_ai_revision,
    create_media_project,
    import_media_image_base64,
    list_media_asset_revisions,
)
from app.services.model_gateway import ModelGatewayError, VisualModelRuntime
from app.services.qwen_image_edit import (
    QwenDownloadedImage,
    QwenImageEditOutcomeUnknownError,
    QwenImageEditProviderError,
    QwenImageEditRateLimitError,
    QwenImageEditResult,
)
from main import create_app


def _fixture_png(*, color: tuple[int, int, int, int], size: tuple[int, int] = (640, 512)) -> bytes:
    image = Image.new("RGBA", size, color)
    left = size[0] // 4
    top = size[1] // 4
    image.paste((245, 183, 61, 255), (left, top, size[0] - left, size[1] - top))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _runtime() -> VisualModelRuntime:
    return VisualModelRuntime(
        provider="qwen_image",
        label="Qwen Image / fixture",
        transport="dashscope_multimodal",
        base_url="https://dashscope.fixture.invalid/api/v1",
        model="qwen-image-3.0-pro",
        api_key="fixture-only-key",
    )


def _provider_result(*, request_id: str = "fixture-request-id") -> QwenImageEditResult:
    return QwenImageEditResult(
        provider="qwen_image",
        model="qwen-image-3.0-pro",
        request_id=request_id,
        output_urls=("https://dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com/fixture.png",),
        input_image_count=1,
        output_image_count=1,
        input_image_type="qima_input_1k",
        output_image_type="qima_output_1k",
        width=640,
        height=512,
        usage_reported=True,
    )


async def _success_editor(**kwargs: object) -> QwenImageEditResult:
    assert kwargs["output_count"] == 1
    assert kwargs["output_size"] == "640*512"
    return _provider_result()


async def _success_downloader(**kwargs: object) -> QwenDownloadedImage:
    assert str(kwargs["result_url"]).startswith("https://dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com/")
    image_bytes = _fixture_png(color=(51, 126, 78, 255))
    return QwenDownloadedImage(
        image_bytes=image_bytes,
        mime_type="image/png",
        image_format="PNG",
        width=640,
        height=512,
    )


async def _run() -> None:
    project = create_media_project(title="AI 修图交付夹具")
    asset = import_media_image_base64(
        project_id=project.project_id,
        filename="source.png",
        content_base64=base64.b64encode(_fixture_png(color=(43, 102, 188, 255))).decode("ascii"),
    )
    request = MediaImageAiEditRequest(
        base_revision_id=asset.current_revision_id,
        instruction="  把黄色区域改成绿色，其余画面保持不变。  ",
    )

    success_task_id = "task_media_ai_edit_0123456789ab"
    create_media_ai_edit_queued_run(
        task_id=success_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=request,
    )
    success = await run_media_ai_edit_task(
        task_id=success_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=request,
        runtime=_runtime(),
        image_editor=_success_editor,
        image_downloader=_success_downloader,
    )
    assert success.status == "completed", success
    assert success.revision is not None and success.revision.operation == "ai_image_edit"
    assert success.revision.parent_revision_id == asset.current_revision_id
    assert success.revision.parameters["instruction"] == request.instruction
    assert success.revision.parameters["provider"] == "qwen_image"
    assert success.revision.parameters["request_id"] == "fixture-request-id"
    assert "oss" not in str(success.revision.parameters)
    revisions = list_media_asset_revisions(project_id=project.project_id, asset_id=asset.asset_id)
    assert revisions.asset.current_revision_id == success.revision.revision_id
    assert revisions.asset.undo_available is True
    calls = list_workflow_tool_calls(success_task_id)
    assert len(calls) == 1
    assert calls[0].tool_name == "media.ai_edit_image"
    assert calls[0].request["model_used"] is True and calls[0].request["network_used"] is True
    assert calls[0].result["verification_passed"] is True

    # Qwen 3.0 的竖图可低于旧的 512px 单边门槛，只要其输入边长建议和输出像素面积均有效。
    portrait_asset = import_media_image_base64(
        project_id=project.project_id,
        filename="portrait-source.png",
        content_base64=base64.b64encode(
            _fixture_png(color=(43, 102, 188, 255), size=(384, 1024))
        ).decode("ascii"),
    )
    portrait_request = MediaImageAiEditRequest(
        base_revision_id=portrait_asset.current_revision_id,
        instruction="把黄色区域改成绿色，其余画面保持不变。",
    )
    portrait_task_id = "task_media_ai_edit_0123456789ac"
    create_media_ai_edit_queued_run(
        task_id=portrait_task_id,
        project_id=project.project_id,
        asset_id=portrait_asset.asset_id,
        request=portrait_request,
    )

    async def portrait_editor(**kwargs: object) -> QwenImageEditResult:
        assert kwargs["output_size"] == "384*1024"
        return QwenImageEditResult(
            provider="qwen_image",
            model="qwen-image-3.0-pro",
            request_id="portrait-fixture-request-id",
            output_urls=("https://dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com/portrait.png",),
        )

    async def portrait_downloader(**_: object) -> QwenDownloadedImage:
        image_bytes = _fixture_png(color=(51, 126, 78, 255), size=(384, 1024))
        return QwenDownloadedImage(
            image_bytes=image_bytes,
            mime_type="image/png",
            image_format="PNG",
            width=384,
            height=1024,
        )

    portrait = await run_media_ai_edit_task(
        task_id=portrait_task_id,
        project_id=project.project_id,
        asset_id=portrait_asset.asset_id,
        request=portrait_request,
        runtime=_runtime(),
        image_editor=portrait_editor,
        image_downloader=portrait_downloader,
    )
    assert portrait.status == "completed", portrait

    # 结果迟到时只能失败，不能覆盖用户在模型等待期间新建的 revision。
    stale_task_id = "task_media_ai_edit_123456789abc"
    stale_request = MediaImageAiEditRequest(
        base_revision_id=success.revision.revision_id,
        instruction="把画面整体调亮一点。",
    )
    create_media_ai_edit_queued_run(
        task_id=stale_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=stale_request,
    )
    local_request = MediaImageOperationRequest(operation="grayscale", base_revision_id=success.revision.revision_id)
    local_task_id = "task_media_edit_123456789abc"
    create_media_edit_queued_run(
        task_id=local_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=local_request,
    )
    local_result = await run_media_edit_task(
        task_id=local_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=local_request,
    )
    assert local_result.status == "completed"
    stale = await run_media_ai_edit_task(
        task_id=stale_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=stale_request,
        runtime=_runtime(),
        image_editor=_success_editor,
        image_downloader=_success_downloader,
    )
    assert stale.status == "failed" and stale.conflict is True
    assert stale.failure_reason == "workspace_conflict"

    current_revision_id = local_result.revision.revision_id if local_result.revision else ""
    assert current_revision_id

    async def rejected_editor(**_: object) -> QwenImageEditResult:
        raise QwenImageEditProviderError(status_code=400, error_code="InvalidParameter", message="fixture rejected")

    rejected_task_id = "task_media_ai_edit_23456789abcd"
    rejected_request = MediaImageAiEditRequest(base_revision_id=current_revision_id, instruction="把背景改成白色。")
    create_media_ai_edit_queued_run(
        task_id=rejected_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=rejected_request,
    )
    rejected = await run_media_ai_edit_task(
        task_id=rejected_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=rejected_request,
        runtime=_runtime(),
        image_editor=rejected_editor,
    )
    assert rejected.status == "failed" and rejected.failure_reason == "provider_rejected"

    async def limited_editor(**_: object) -> QwenImageEditResult:
        raise QwenImageEditRateLimitError(error_code="Throttling", retry_after_seconds=15)

    limited_task_id = "task_media_ai_edit_3456789abcde"
    limited_request = MediaImageAiEditRequest(base_revision_id=current_revision_id, instruction="把天空变得更晴朗。")
    create_media_ai_edit_queued_run(
        task_id=limited_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=limited_request,
    )
    limited = await run_media_ai_edit_task(
        task_id=limited_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=limited_request,
        runtime=_runtime(),
        image_editor=limited_editor,
    )
    assert limited.status == "failed" and limited.failure_reason == "provider_rate_limited"
    assert limited.retry_after_seconds == 15

    async def unknown_editor(**_: object) -> QwenImageEditResult:
        raise QwenImageEditOutcomeUnknownError(reason="request_timeout", message="fixture timeout")

    unknown_task_id = "task_media_ai_edit_456789abcdef"
    unknown_request = MediaImageAiEditRequest(base_revision_id=current_revision_id, instruction="把人物衣服换成深色。")
    create_media_ai_edit_queued_run(
        task_id=unknown_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=unknown_request,
    )
    unknown = await run_media_ai_edit_task(
        task_id=unknown_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=unknown_request,
        runtime=_runtime(),
        image_editor=unknown_editor,
    )
    assert unknown.status == "failed" and unknown.failure_reason == "provider_outcome_unknown"
    assert "不会自动重试" in unknown.message

    async def failed_download(**_: object) -> QwenDownloadedImage:
        raise ModelGatewayError("fixture download failed")

    download_task_id = "task_media_ai_edit_56789abcdef0"
    download_request = MediaImageAiEditRequest(base_revision_id=current_revision_id, instruction="去除左上角的标记。")
    create_media_ai_edit_queued_run(
        task_id=download_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=download_request,
    )
    download_failed = await run_media_ai_edit_task(
        task_id=download_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=download_request,
        runtime=_runtime(),
        image_editor=_success_editor,
        image_downloader=failed_download,
    )
    assert download_failed.status == "failed" and download_failed.failure_reason == "result_download_failed"

    # 用户在网络提交前取消，运行器不能调用替身，也不能写入版本。
    cancel_task_id = "task_media_ai_edit_6789abcdef01"
    cancel_request = MediaImageAiEditRequest(base_revision_id=current_revision_id, instruction="让画面更有电影感。")
    create_media_ai_edit_queued_run(
        task_id=cancel_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=cancel_request,
    )
    cancelled = await cancel_media_ai_edit_task(cancel_task_id)
    assert cancelled is not None and cancelled.accepted
    cancelled_result = await run_media_ai_edit_task(
        task_id=cancel_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=cancel_request,
        runtime=_runtime(),
        image_editor=_success_editor,
        image_downloader=_success_downloader,
    )
    assert cancelled_result.status == "cancelled"

    # revision 元数据是受控内部契约，不能靠隐式字符串转换把异常对象写入项目记录。
    try:
        create_media_image_ai_revision(
            project_id=project.project_id,
            asset_id=asset.asset_id,
            base_revision_id=current_revision_id,
            image_bytes=_fixture_png(color=(80, 80, 80, 255)),
            parameters={
                "instruction": 123,
                "provider": "qwen_image",
                "model": "qwen-image-3.0-pro",
                "request_id": "invalid-metadata",
                "usage": {"usage_reported": False},
            },
            task_id="task_media_ai_edit_789abcdef012",
        )
    except MediaWorkspaceError:
        pass
    else:  # pragma: no cover - 防止异常 metadata 被静默写入 revision。
        raise AssertionError("AI revision metadata must reject non-string instruction")

    # 模拟 PNG 和 manifest 已原子提交，但 Runtime 终态尚未写入的进程崩溃窗口。
    recovered_task_id = "task_media_ai_edit_89abcdef0123"
    recovered_request = MediaImageAiEditRequest(base_revision_id=current_revision_id, instruction="把背景换成浅灰色。")
    create_media_ai_edit_queued_run(
        task_id=recovered_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=recovered_request,
    )
    recovered_revision = create_media_image_ai_revision(
        project_id=project.project_id,
        asset_id=asset.asset_id,
        base_revision_id=current_revision_id,
        image_bytes=_fixture_png(color=(154, 163, 177, 255)),
        parameters={
            "instruction": recovered_request.instruction,
            "provider": "qwen_image",
            "model": "qwen-image-3.0-pro",
            "request_id": "recovery-fixture-request",
            "usage": {"usage_reported": False},
        },
        task_id=recovered_task_id,
    )
    missing_task_id = "task_media_ai_edit_9abcdef01234"
    create_media_ai_edit_queued_run(
        task_id=missing_task_id,
        project_id=project.project_id,
        asset_id=asset.asset_id,
        request=MediaImageAiEditRequest(
            base_revision_id=recovered_revision.revision_id,
            instruction="换成更明亮的配色。",
        ),
    )
    with TestClient(create_app()) as client:
        assert client.app.state.recovered_media_ai_edit_task_count == 2

        # API 只负责受理与后台调度；用同一个离线替身验证它确实接入了新的任务闭环。
        from app.api import media_agent as media_api

        original_runner = media_api.run_media_ai_edit_task

        async def api_runner(**kwargs: object):  # type: ignore[no-untyped-def]
            return await original_runner(
                **kwargs,
                runtime=_runtime(),
                image_editor=_success_editor,
                image_downloader=_success_downloader,
            )

        media_api.run_media_ai_edit_task = api_runner
        try:
            started = client.post(
                f"/api/agents/media_agent/projects/{project.project_id}/images/{asset.asset_id}/ai-edits/start",
                json={
                    "base_revision_id": recovered_revision.revision_id,
                    "instruction": "把黄色区域改成绿色，其余画面保持不变。",
                },
            )
            assert started.status_code == 202, started.text
            api_task_id = started.json()["task_id"]
            api_result = None
            for _ in range(100):
                response = client.get(f"/api/agents/media_agent/ai-edits/{api_task_id}/result")
                assert response.status_code == 200, response.text
                api_result = response.json()
                if api_result["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.01)
            assert api_result is not None and api_result["status"] == "completed", api_result
            unified = client.get(f"/api/tasks/{api_task_id}")
            assert unified.status_code == 200, unified.text
            assert unified.json()["steps"][0]["action"] == "media.ai_edit_image"
        finally:
            media_api.run_media_ai_edit_task = original_runner

    recovered = get_media_ai_edit_task_result(recovered_task_id)
    assert recovered is not None and recovered.status == "completed"
    assert recovered.revision is not None and recovered.revision.revision_id == recovered_revision.revision_id
    missing = get_media_ai_edit_task_result(missing_task_id)
    assert missing is not None and missing.status == "failed"
    assert missing.failure_reason == "provider_outcome_unknown"
    assert load_workflow_run(success_task_id).status == "completed"


def main() -> None:
    try:
        asyncio.run(_run())
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
    print("media AI edit delivery verification passed: success/failure/cancel/conflict/recovery")


if __name__ == "__main__":
    main()
