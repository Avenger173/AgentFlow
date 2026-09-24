"""多媒体 Agent 的受控图片与短媒体转写 API。

图片操作仍走不可变 revision；音视频转写必须先转入私有源文件、提取固定 WAV，再通过
一次性任务写入可回读 JSON。两条路径都不能绕过项目范围、任务历史或交付验证。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.schemas.media_workspace import (
    MediaAssetInfo,
    MediaAssetRevisionListResponse,
    MediaHistoryNavigationRequest,
    MediaImageAiEditRequest,
    MediaImageAiEditTaskResultResponse,
    MediaImageAiEditTaskStartResponse,
    MediaImageEditTaskResultResponse,
    MediaImageEditTaskStartResponse,
    MediaImageExportInfo,
    MediaImageExportRequest,
    MediaImageExportTaskResultResponse,
    MediaImageExportTaskStartResponse,
    MediaImageImportRequest,
    MediaImageOperationRequest,
    MediaImageRevisionInfo,
    MediaLayerStackResponse,
    MediaProjectCreateRequest,
    MediaProjectDetailResponse,
    MediaProjectInfo,
    MediaProjectListResponse,
)
from app.schemas.media_source import (
    MediaProbeInfo,
    MediaSourceImportRequest,
    MediaSourceInfo,
    MediaTranscriptionPreparationResponse,
    MediaTranscriptionRequest,
    MediaTranscriptionStartResponse,
    MediaTranscriptionTaskResultResponse,
)
from app.services.media_workspace import (
    MediaWorkspaceConflictError,
    MediaWorkspaceError,
    create_media_image_revision,
    create_media_project,
    export_media_image_revision,
    get_media_project,
    get_media_image_layer_stack,
    import_media_image_base64,
    list_media_asset_revisions,
    list_media_projects,
    navigate_media_image_history,
    resolve_media_export_download_path,
    resolve_media_revision_preview_path,
)
from app.services.media_export_delivery import (
    create_media_export_queued_run,
    get_media_export_task_result,
    run_media_export_task,
)
from app.services.media_edit_delivery import (
    create_media_edit_queued_run,
    get_media_edit_task_result,
    run_media_edit_task,
)
from app.services.media_ai_edit_delivery import (
    create_media_ai_edit_queued_run,
    get_media_ai_edit_task_result,
    run_media_ai_edit_task,
)
from app.services.media_source_preparation import (
    MediaSourcePreparationError,
    extract_primary_audio_for_transcription,
    get_media_source,
    import_media_source_bytes,
    probe_media_source,
)
from app.services.media_transcription_delivery import (
    create_media_transcription_queued_run,
    get_media_transcription_task_result,
    run_media_transcription_task,
)
from app.services.task_event_stream import (
    finish_live_task_event_stream,
    has_live_task_event_stream,
    live_task_event_stream_finished,
    open_live_task_event_stream,
    publish_live_task_event,
)


router = APIRouter(prefix="/api/agents/media_agent", tags=["media-agent"])
logger = logging.getLogger(__name__)
_BACKGROUND_MEDIA_EXPORT_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_MEDIA_EDIT_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_MEDIA_AI_EDIT_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_MEDIA_TRANSCRIPTION_TASKS: set[asyncio.Task[None]] = set()


@router.get("/projects", response_model=MediaProjectListResponse)
async def list_media_projects_endpoint() -> MediaProjectListResponse:
    projects = await asyncio.to_thread(list_media_projects)
    return MediaProjectListResponse(total=len(projects), projects=projects)


@router.post("/projects", response_model=MediaProjectInfo, status_code=201)
async def create_media_project_endpoint(request: MediaProjectCreateRequest) -> MediaProjectInfo:
    try:
        return await asyncio.to_thread(create_media_project, title=request.title)
    except MediaWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}", response_model=MediaProjectDetailResponse)
async def get_media_project_endpoint(project_id: str) -> MediaProjectDetailResponse:
    try:
        return await asyncio.to_thread(get_media_project, project_id)
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.post("/projects/{project_id}/images", response_model=MediaAssetInfo, status_code=201)
async def import_media_image_endpoint(
    project_id: str,
    request: MediaImageImportRequest,
) -> MediaAssetInfo:
    try:
        return await asyncio.to_thread(
            import_media_image_base64,
            project_id=project_id,
            filename=request.filename,
            content_base64=request.content_base64,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.post("/projects/{project_id}/media-sources", response_model=MediaSourceInfo, status_code=201)
async def import_media_source_endpoint(project_id: str, request: MediaSourceImportRequest) -> MediaSourceInfo:
    """导入音视频副本；调用方永远不能提供本机媒体路径。"""

    try:
        await asyncio.to_thread(get_media_project, project_id)
        content = await asyncio.to_thread(_decode_media_source_base64, request.content_base64)
        return await asyncio.to_thread(
            import_media_source_bytes,
            project_scope=project_id,
            filename=request.filename,
            content=content,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    except MediaSourcePreparationError as exc:
        raise _media_source_error_to_http(exc) from exc


@router.get("/projects/{project_id}/media-sources/{source_id}", response_model=MediaSourceInfo)
async def get_media_source_endpoint(project_id: str, source_id: str) -> MediaSourceInfo:
    try:
        await asyncio.to_thread(get_media_project, project_id)
        return await asyncio.to_thread(
            get_media_source,
            source_id,
            expected_project_scope=project_id,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    except MediaSourcePreparationError as exc:
        raise _media_source_error_to_http(exc) from exc


@router.post("/projects/{project_id}/media-sources/{source_id}/probe", response_model=MediaProbeInfo)
async def probe_media_source_endpoint(project_id: str, source_id: str) -> MediaProbeInfo:
    try:
        await asyncio.to_thread(get_media_project, project_id)
        return await asyncio.to_thread(
            probe_media_source,
            source_id=source_id,
            expected_project_scope=project_id,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    except MediaSourcePreparationError as exc:
        raise _media_source_error_to_http(exc) from exc


@router.post(
    "/projects/{project_id}/media-sources/{source_id}/transcription-audio",
    response_model=MediaTranscriptionPreparationResponse,
)
async def prepare_media_source_transcription_audio_endpoint(
    project_id: str,
    source_id: str,
) -> MediaTranscriptionPreparationResponse:
    """仅允许从受控源导出固定 ASR WAV，不能提交任意 ffmpeg 参数。"""

    try:
        await asyncio.to_thread(get_media_project, project_id)
        source, audio = await asyncio.to_thread(_prepare_transcription_audio, project_id, source_id)
        return MediaTranscriptionPreparationResponse(source=source, audio=audio)
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    except MediaSourcePreparationError as exc:
        raise _media_source_error_to_http(exc) from exc


@router.post(
    "/projects/{project_id}/transcriptions/start",
    response_model=MediaTranscriptionStartResponse,
    status_code=202,
)
async def start_media_transcription_endpoint(
    project_id: str,
    request: MediaTranscriptionRequest,
) -> MediaTranscriptionStartResponse:
    """受理一次显式提交的受控 WAV 转写，不在 API 线程中直接等待 Provider。"""

    task_id = f"task_media_transcription_{uuid4().hex[:12]}"
    try:
        await asyncio.to_thread(get_media_project, project_id)
        await asyncio.to_thread(
            create_media_transcription_queued_run,
            task_id=task_id,
            project_id=project_id,
            request=request,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="media_agent",
        message="媒体转写已受理，尚未向模型 Provider 发送音频。",
    )
    task = asyncio.create_task(
        _run_media_transcription_background(task_id=task_id, project_id=project_id, request=request)
    )
    _BACKGROUND_MEDIA_TRANSCRIPTION_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_MEDIA_TRANSCRIPTION_TASKS.discard)
    return MediaTranscriptionStartResponse(task_id=task_id)


@router.get(
    "/transcriptions/{task_id}/result",
    response_model=MediaTranscriptionTaskResultResponse,
)
async def get_media_transcription_result_endpoint(task_id: str) -> MediaTranscriptionTaskResultResponse:
    result = get_media_transcription_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return MediaTranscriptionTaskResultResponse(
            task_id=task_id,
            status="running",
            summary="媒体转写正在执行。",
            message="正在等待语音模型并回读结构化转写交付。",
        )
    raise HTTPException(status_code=404, detail=f"Media transcription task '{task_id}' was not found.")


@router.get(
    "/projects/{project_id}/images/{asset_id}/revisions",
    response_model=MediaAssetRevisionListResponse,
)
async def list_media_asset_revisions_endpoint(
    project_id: str,
    asset_id: str,
) -> MediaAssetRevisionListResponse:
    try:
        return await asyncio.to_thread(
            list_media_asset_revisions,
            project_id=project_id,
            asset_id=asset_id,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.get(
    "/projects/{project_id}/images/{asset_id}/revisions/{revision_id}/layers",
    response_model=MediaLayerStackResponse,
)
async def get_media_image_layer_stack_endpoint(
    project_id: str,
    asset_id: str,
    revision_id: str,
) -> MediaLayerStackResponse:
    try:
        return await asyncio.to_thread(
            get_media_image_layer_stack,
            project_id=project_id,
            asset_id=asset_id,
            revision_id=revision_id,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.post(
    "/projects/{project_id}/images/{asset_id}/revisions",
    response_model=MediaImageRevisionInfo,
    status_code=201,
)
async def create_media_image_revision_endpoint(
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
) -> MediaImageRevisionInfo:
    try:
        return await asyncio.to_thread(
            create_media_image_revision,
            project_id=project_id,
            asset_id=asset_id,
            base_revision_id=request.base_revision_id,
            operation=request.operation,
            parameters=request.operation_parameters(),
            layer_source_asset_id=request.layer_source_asset_id(),
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.post(
    "/projects/{project_id}/images/{asset_id}/revisions/start",
    response_model=MediaImageEditTaskStartResponse,
    status_code=202,
)
async def start_media_image_revision_endpoint(
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
) -> MediaImageEditTaskStartResponse:
    """受理一次确定性图片编辑，并把新修订纳入统一 Runtime 历史。"""

    task_id = f"task_media_edit_{uuid4().hex[:12]}"
    try:
        await asyncio.to_thread(
            create_media_edit_queued_run,
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="media_agent",
        message="图片编辑已受理，等待校验当前版本。",
    )
    task = asyncio.create_task(
        _run_media_edit_background(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
        )
    )
    _BACKGROUND_MEDIA_EDIT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_MEDIA_EDIT_TASKS.discard)
    return MediaImageEditTaskStartResponse(task_id=task_id)


@router.get(
    "/edits/{task_id}/result",
    response_model=MediaImageEditTaskResultResponse,
)
async def get_media_image_revision_result_endpoint(task_id: str) -> MediaImageEditTaskResultResponse:
    result = get_media_edit_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return MediaImageEditTaskResultResponse(
            task_id=task_id,
            status="running",
            summary="图片编辑正在执行。",
            message="正在生成并回读新的 PNG 修订版本。",
        )
    raise HTTPException(status_code=404, detail=f"Media edit task '{task_id}' was not found.")


@router.post(
    "/projects/{project_id}/images/{asset_id}/ai-edits/start",
    response_model=MediaImageAiEditTaskStartResponse,
    status_code=202,
)
async def start_media_image_ai_edit_endpoint(
    project_id: str,
    asset_id: str,
    request: MediaImageAiEditRequest,
) -> MediaImageAiEditTaskStartResponse:
    """受理一次已确认的模型修图；模型输出必须回到受控 revision 链。"""

    task_id = f"task_media_ai_edit_{uuid4().hex[:12]}"
    try:
        await asyncio.to_thread(
            create_media_ai_edit_queued_run,
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="media_agent",
        message="AI 修图已受理，尚未向模型 Provider 发送图片。",
    )
    task = asyncio.create_task(
        _run_media_ai_edit_background(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
        )
    )
    _BACKGROUND_MEDIA_AI_EDIT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_MEDIA_AI_EDIT_TASKS.discard)
    return MediaImageAiEditTaskStartResponse(task_id=task_id)


@router.get(
    "/ai-edits/{task_id}/result",
    response_model=MediaImageAiEditTaskResultResponse,
)
async def get_media_image_ai_edit_result_endpoint(task_id: str) -> MediaImageAiEditTaskResultResponse:
    result = get_media_ai_edit_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return MediaImageAiEditTaskResultResponse(
            task_id=task_id,
            status="running",
            summary="AI 修图正在执行。",
            message="正在等待图像模型并回读验证结果。",
        )
    raise HTTPException(status_code=404, detail=f"Media AI edit task '{task_id}' was not found.")


@router.post(
    "/projects/{project_id}/images/{asset_id}/history/{action}",
    response_model=MediaAssetRevisionListResponse,
)
async def navigate_media_image_history_endpoint(
    project_id: str,
    asset_id: str,
    action: Literal["undo", "redo"],
    request: MediaHistoryNavigationRequest,
) -> MediaAssetRevisionListResponse:
    try:
        return await asyncio.to_thread(
            navigate_media_image_history,
            project_id=project_id,
            asset_id=asset_id,
            action=action,
            base_revision_id=request.base_revision_id,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.get("/projects/{project_id}/revisions/{revision_id}/preview")
async def preview_media_revision_endpoint(project_id: str, revision_id: str) -> FileResponse:
    try:
        path = await asyncio.to_thread(
            resolve_media_revision_preview_path,
            project_id=project_id,
            revision_id=revision_id,
        )
        return FileResponse(path, media_type="image/png")
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.post(
    "/projects/{project_id}/revisions/{revision_id}/export",
    response_model=MediaImageExportInfo,
    status_code=201,
)
async def export_media_revision_endpoint(
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
) -> MediaImageExportInfo:
    try:
        return await asyncio.to_thread(
            export_media_image_revision,
            project_id=project_id,
            revision_id=revision_id,
            filename=request.filename,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


@router.post(
    "/projects/{project_id}/revisions/{revision_id}/export/start",
    response_model=MediaImageExportTaskStartResponse,
    status_code=202,
)
async def start_media_revision_export_endpoint(
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
) -> MediaImageExportTaskStartResponse:
    """受理图片 PNG 导出，并将真实交付写入统一任务历史。"""

    task_id = f"task_media_export_{uuid4().hex[:12]}"
    try:
        await asyncio.to_thread(
            create_media_export_queued_run,
            task_id=task_id,
            project_id=project_id,
            revision_id=revision_id,
            request=request,
        )
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="media_agent",
        message="图片 PNG 导出已受理，将只写入新的受控交付文件。",
    )
    task = asyncio.create_task(
        _run_media_export_background(
            task_id=task_id,
            project_id=project_id,
            revision_id=revision_id,
            request=request,
        )
    )
    _BACKGROUND_MEDIA_EXPORT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_MEDIA_EXPORT_TASKS.discard)
    return MediaImageExportTaskStartResponse(task_id=task_id)


@router.get(
    "/exports/{task_id}/result",
    response_model=MediaImageExportTaskResultResponse,
)
async def get_media_revision_export_result_endpoint(
    task_id: str,
) -> MediaImageExportTaskResultResponse:
    """查询图片导出终态；完成任务可从 SQLite 和受控 Artifact 恢复。"""

    result = get_media_export_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return MediaImageExportTaskResultResponse(
            task_id=task_id,
            status="running",
            summary="图片 PNG 正在导出。",
            message="正在复制并回读验证 PNG 交付文件。",
        )
    raise HTTPException(status_code=404, detail=f"Media export task '{task_id}' was not found.")


@router.get("/projects/{project_id}/exports/{export_id}/download")
async def download_media_export_endpoint(project_id: str, export_id: str) -> FileResponse:
    try:
        path, filename = await asyncio.to_thread(
            resolve_media_export_download_path,
            project_id=project_id,
            export_id=export_id,
        )
        return FileResponse(path, media_type="image/png", filename=filename)
    except MediaWorkspaceError as exc:
        raise _media_error_to_http(exc) from exc


async def _run_media_export_background(
    *,
    task_id: str,
    project_id: str,
    revision_id: str,
    request: MediaImageExportRequest,
) -> None:
    """保证任何异常都会关闭实时流，桌面端不会无限等待导出状态。"""

    try:
        await run_media_export_task(
            task_id=task_id,
            project_id=project_id,
            revision_id=revision_id,
            request=request,
        )
    except Exception:  # pragma: no cover - 服务层已落终态，这里仅为实时事件提供最后兜底。
        logger.exception("Media export task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="media_agent",
            level="error",
            message="图片 PNG 导出异常结束，请在任务历史中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)


async def _run_media_edit_background(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageOperationRequest,
) -> None:
    """让任何未预期异常都关闭实时流，客户端不会无限等待编辑结果。"""

    try:
        await run_media_edit_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
        )
    except Exception:  # pragma: no cover - 服务层已尽量持久化，此处只保证实时流结束。
        logger.exception("Media edit task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="media_agent",
            level="error",
            message="图片编辑异常结束，请在任务历史中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)


async def _run_media_ai_edit_background(
    *,
    task_id: str,
    project_id: str,
    asset_id: str,
    request: MediaImageAiEditRequest,
) -> None:
    """模型请求异常也必须结束实时流，避免客户端无限显示处理中。"""

    try:
        await run_media_ai_edit_task(
            task_id=task_id,
            project_id=project_id,
            asset_id=asset_id,
            request=request,
        )
    except Exception:  # pragma: no cover - 服务层会落终态，此处只守住事件流生命周期。
        logger.exception("Media AI edit task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="media_agent",
            level="error",
            message="AI 修图异常结束，请在任务历史中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)


async def _run_media_transcription_background(
    *,
    task_id: str,
    project_id: str,
    request: MediaTranscriptionRequest,
) -> None:
    """模型或文件回读异常都必须关闭实时流，客户端不会永久等待。"""

    try:
        await run_media_transcription_task(task_id=task_id, project_id=project_id, request=request)
    except Exception:  # pragma: no cover - 服务层应已落终态，这里仅兜住事件流生命周期。
        logger.exception("Media transcription task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="media_agent",
            step_id="media_transcription",
            level="error",
            message="媒体转写异常结束，请在任务历史中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)


def _media_error_to_http(exc: MediaWorkspaceError) -> HTTPException:
    detail = str(exc)
    if isinstance(exc, MediaWorkspaceConflictError):
        status_code = 409
    else:
        status_code = 404 if detail.startswith("未找到") else 400
    return HTTPException(status_code=status_code, detail=detail)


def _media_source_error_to_http(exc: MediaSourcePreparationError) -> HTTPException:
    detail = str(exc)
    # 不以 400 透露另一个项目的 source_id 是否存在。
    status_code = 404 if "未找到" in detail or "不属于指定项目范围" in detail else 400
    return HTTPException(status_code=status_code, detail=detail)


def _decode_media_source_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise MediaSourcePreparationError("媒体内容不是有效的 Base64 编码。") from exc


def _prepare_transcription_audio(project_id: str, source_id: str):  # type: ignore[no-untyped-def]
    source = get_media_source(source_id, expected_project_scope=project_id)
    audio = extract_primary_audio_for_transcription(source_id=source_id, expected_project_scope=project_id)
    return source, audio
