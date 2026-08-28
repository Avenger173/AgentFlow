"""数据工作台 D1 的受控文件与画像入口。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.database.task_repository import list_workflow_artifacts

from app.schemas.data_agent import (
    DataAnalysisPreviewRequest,
    DataAnalysisPreviewResponse,
    DataChartExportRequest,
    DataChartTaskResultResponse,
    DataChartTaskStartResponse,
    DataDatasetCreateRequest,
    DataDatasetInfo,
    DataDatasetListResponse,
    DataDatasetProfileResponse,
    DataRecommendationRequest,
    DataRecommendationResponse,
    DataTransformPreviewRequest,
    DataTransformPreviewResponse,
    DataTransformationExportRequest,
    DataTransformationTaskResultResponse,
    DataTransformationTaskStartResponse,
    DataWorkbookExportRequest,
    DataWorkbookExportResponse,
    DataWorkbookTaskResultResponse,
    DataWorkbookTaskStartResponse,
)
from app.services.data_analysis import DataAnalysisError, preview_data_analysis
from app.services.data_insights import enrich_data_analysis_insight
from app.services.data_recommendations import (
    DataRecommendationError,
    build_data_recommendations,
    refine_data_recommendations_with_model,
)
from app.services.data_analysis_delivery import (
    create_data_workbook_queued_run,
    get_data_workbook_export_task_result,
    run_data_workbook_export_task,
)
from app.services.data_chart_delivery import (
    create_data_chart_queued_run,
    get_data_chart_export_task_result,
    run_data_chart_export_task,
)
from app.services.data_charts import DataChartError, resolve_data_chart_artifact_path
from app.services.data_transformation_delivery import (
    create_data_transformation_queued_run,
    get_data_transformation_task_result,
    run_data_transformation_task,
)
from app.services.data_transformations import DataTransformationError, preview_data_transformation
from app.services.data_workbook import DataWorkbookError, export_data_analysis_workbook
from app.services.data_workspace import (
    DataWorkspaceError,
    get_data_dataset_profile,
    import_data_dataset_base64,
    list_data_datasets,
)
from app.services.task_event_stream import (
    finish_live_task_event_stream,
    has_live_task_event_stream,
    live_task_event_stream_finished,
    open_live_task_event_stream,
    publish_live_task_event,
)
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


router = APIRouter(prefix="/api/agents/data_agent", tags=["data-agent"])
logger = logging.getLogger(__name__)
_BACKGROUND_DATA_WORKBOOK_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_DATA_CHART_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_DATA_TRANSFORMATION_TASKS: set[asyncio.Task[None]] = set()


@router.get("/datasets", response_model=DataDatasetListResponse)
async def list_data_datasets_endpoint() -> DataDatasetListResponse:
    """列出已经导入的受控数据文件，不读取完整表格内容。"""

    datasets = await asyncio.to_thread(list_data_datasets)
    return DataDatasetListResponse(total=len(datasets), datasets=datasets)


@router.post("/datasets", response_model=DataDatasetInfo)
async def import_data_dataset_endpoint(request: DataDatasetCreateRequest) -> DataDatasetInfo:
    """导入一个 Excel/CSV 新副本；请求体不支持任意本机路径。"""

    try:
        return await asyncio.to_thread(
            import_data_dataset_base64,
            filename=request.filename,
            content_base64=request.content_base64,
        )
    except DataWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/datasets/{dataset_name}/profile", response_model=DataDatasetProfileResponse)
async def get_data_dataset_profile_endpoint(dataset_name: str) -> DataDatasetProfileResponse:
    """返回有限预览和聚合画像；不触发模型、联网或任何源文件写入。"""

    try:
        return await asyncio.to_thread(get_data_dataset_profile, dataset_name)
    except DataWorkspaceError as exc:
        status_code = 404 if "未找到" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/recommendations", response_model=DataRecommendationResponse)
async def get_data_recommendations_endpoint(
    request: DataRecommendationRequest,
) -> DataRecommendationResponse:
    """根据本地画像给出下一步建议；模型可选且仅接收 L1 字段画像。"""

    try:
        local_response = await asyncio.to_thread(build_data_recommendations, request)
        # 建议主链始终先完成本地画像映射。真实模型只可在 L1 范围重排候选，任何故障都会
        # 降级为 local_response，不影响用户继续进入 D2 的确定性计算。
        profile = await asyncio.to_thread(get_data_dataset_profile, request.dataset_name)
        return await refine_data_recommendations_with_model(
            local_response,
            profile=profile,
            goal=request.goal,
        )
    except DataRecommendationError as exc:
        status_code = 404 if "未找到" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/analysis/preview", response_model=DataAnalysisPreviewResponse)
async def preview_data_analysis_endpoint(request: DataAnalysisPreviewRequest) -> DataAnalysisPreviewResponse:
    """执行 D2 的只读计划、确定性聚合与受控结论预览。

    本地计算不调用模型、网络或 Shell，不创建工作簿，也不把原始行写入任务日志。完成后结论层
    可选调用 ModelGateway，但只接收已验证的指标和聚合结果；失败时稳定回退为本地结论。
    较大的 DataFrame 计算放入线程，保证 FastAPI 事件循环继续响应 Qt。
    """

    try:
        preview = await asyncio.to_thread(preview_data_analysis, request)
        return await enrich_data_analysis_insight(preview, goal=request.goal)
    except DataAnalysisError as exc:
        status_code = 404 if "未找到" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/analysis/export", response_model=DataWorkbookExportResponse)
async def export_data_analysis_workbook_endpoint(
    request: DataWorkbookExportRequest,
) -> DataWorkbookExportResponse:
    """在用户确认后生成新的原生 Excel 工作簿。

    导出线程会重新执行 D2 的受控计算、比对用户确认过的源哈希，并在临时文件回读通过后才
    返回 artifact 引用。它不覆盖导入文件、不调用模型或网络，也不在 D3 阶段登记任务历史。
    """

    try:
        return await asyncio.to_thread(export_data_analysis_workbook, request)
    except DataWorkbookError as exc:
        status_code = 404 if "未找到" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post(
    "/analysis/export/start",
    response_model=DataWorkbookTaskStartResponse,
    status_code=202,
)
async def start_data_analysis_workbook_export(
    request: DataWorkbookExportRequest,
) -> DataWorkbookTaskStartResponse:
    """受理已确认的 Excel 导出，并立即写入统一任务历史与实时事件流。"""

    task_id = f"task_data_{uuid4().hex[:12]}"
    create_data_workbook_queued_run(task_id=task_id, request=request)
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="data_agent",
        message="数据工作簿导出已受理，将只生成新的受控 Excel 文件。",
    )
    task = asyncio.create_task(_run_data_analysis_workbook_export_background(task_id, request))
    _BACKGROUND_DATA_WORKBOOK_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DATA_WORKBOOK_TASKS.discard)
    return DataWorkbookTaskStartResponse(task_id=task_id)


@router.get(
    "/analysis/export/{task_id}/result",
    response_model=DataWorkbookTaskResultResponse,
)
async def get_data_analysis_workbook_export_result(
    task_id: str,
) -> DataWorkbookTaskResultResponse:
    """查询导出终态；完成任务可在进程重启后从 SQLite 与 artifact 恢复。"""

    result = get_data_workbook_export_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return DataWorkbookTaskResultResponse(
            task_id=task_id,
            status="running",
            summary="数据工作簿正在生成。",
            message="正在写入并回读验证可编辑 Excel。",
        )
    raise HTTPException(status_code=404, detail=f"Data workbook task '{task_id}' was not found.")


async def _run_data_analysis_workbook_export_background(
    task_id: str,
    request: DataWorkbookExportRequest,
) -> None:
    """无论业务异常或进程级异常，均结束事件流，避免桌面端无限等待。"""

    try:
        await run_data_workbook_export_task(task_id=task_id, request=request)
    except Exception:  # pragma: no cover - 服务层会尽量持久化失败任务，这里仅兜底日志与事件流。
        logger.exception("Data workbook export task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="data_agent",
            level="error",
            message="数据工作簿导出异常结束，请在历史任务中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)


@router.post(
    "/charts/export/start",
    response_model=DataChartTaskStartResponse,
    status_code=202,
)
async def start_data_chart_export(request: DataChartExportRequest) -> DataChartTaskStartResponse:
    """受理客户已确认的 PNG 图表看板，不修改源文件或 Excel 工作簿。"""

    task_id = f"task_data_chart_{uuid4().hex[:12]}"
    create_data_chart_queued_run(task_id=task_id, request=request)
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="data_agent",
        message="图表看板生成已受理，将只写入新的受控 PNG。",
    )
    task = asyncio.create_task(_run_data_chart_export_background(task_id, request))
    _BACKGROUND_DATA_CHART_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DATA_CHART_TASKS.discard)
    return DataChartTaskStartResponse(task_id=task_id)


@router.get(
    "/charts/export/{task_id}/result",
    response_model=DataChartTaskResultResponse,
)
async def get_data_chart_export_result(task_id: str) -> DataChartTaskResultResponse:
    """读取 PNG 看板任务终态；进程重启后仍从 SQLite 恢复 artifact 摘要。"""

    result = get_data_chart_export_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return DataChartTaskResultResponse(
            task_id=task_id,
            status="running",
            summary="图表看板正在生成。",
            message="正在本地绘制并回读验证 PNG 图表。",
        )
    raise HTTPException(status_code=404, detail=f"Data chart task '{task_id}' was not found.")


@router.get("/charts/export/{task_id}/artifacts/{artifact_id}/image")
async def get_data_chart_image(task_id: str, artifact_id: str) -> FileResponse:
    """向图表看板返回一个受控 PNG 字节流，不暴露任何本机绝对路径。"""

    if get_data_chart_export_task_result(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Data chart task '{task_id}' was not found.")
    artifact = next(
        (item for item in list_workflow_artifacts(task_id) if item.artifact_id == artifact_id),
        None,
    )
    if artifact is None or artifact.metadata.get("output_scope") != "data_charts":
        raise HTTPException(status_code=404, detail="Data chart artifact was not found.")
    try:
        output_path = resolve_data_chart_artifact_path(task_id=task_id, filename=artifact.name)
    except DataChartError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(output_path, media_type="image/png", filename=artifact.name)


async def _run_data_chart_export_background(task_id: str, request: DataChartExportRequest) -> None:
    """保证图表任务无论如何结束事件流，桌面端会再用结果接口确认最终状态。"""

    try:
        await run_data_chart_export_task(task_id=task_id, request=request)
    except Exception:  # pragma: no cover - 服务层已持久化终态，此处只保留事件流兜底。
        logger.exception("Data chart export task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="data_agent",
            level="error",
            message="图表看板生成异常结束，请在任务历史中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)


@router.post("/transformations/preview", response_model=DataTransformPreviewResponse)
async def preview_data_transformation_endpoint(
    request: DataTransformPreviewRequest,
) -> DataTransformPreviewResponse:
    """计算字段加工预览；只在内存中生成有限样例，不会写出新副本。"""

    try:
        return await asyncio.to_thread(preview_data_transformation, request)
    except DataTransformationError as exc:
        status_code = 404 if "未找到" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post(
    "/transformations/export/start",
    response_model=DataTransformationTaskStartResponse,
    status_code=202,
)
async def start_data_transformation_export(
    request: DataTransformationExportRequest,
) -> DataTransformationTaskStartResponse:
    """受理客户确认过的字段加工新副本；请求不接受任意输出路径。"""

    task_id = f"task_data_transform_{uuid4().hex[:12]}"
    create_data_transformation_queued_run(task_id=task_id, request=request)
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="data_agent",
        message="字段加工已受理，将只生成新的受控 Excel 副本。",
    )
    task = asyncio.create_task(_run_data_transformation_export_background(task_id, request))
    _BACKGROUND_DATA_TRANSFORMATION_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DATA_TRANSFORMATION_TASKS.discard)
    return DataTransformationTaskStartResponse(task_id=task_id)


@router.get(
    "/transformations/export/{task_id}/result",
    response_model=DataTransformationTaskResultResponse,
)
async def get_data_transformation_export_result(
    task_id: str,
) -> DataTransformationTaskResultResponse:
    """读取字段加工终态；完成结果可由 SQLite 和受控 artifact 恢复。"""

    result = get_data_transformation_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return DataTransformationTaskResultResponse(
            task_id=task_id,
            status="running",
            summary="字段加工正在生成。",
            message="正在本地加工字段并重新打开验证新副本。",
        )
    raise HTTPException(status_code=404, detail=f"Data transformation task '{task_id}' was not found.")


async def _run_data_transformation_export_background(
    task_id: str,
    request: DataTransformationExportRequest,
) -> None:
    """无论后台结果如何，均关闭事件流，防止 Qt 因断流无限等待。"""

    try:
        await run_data_transformation_task(task_id=task_id, request=request)
    except Exception:  # pragma: no cover - 服务层已持久化终态，此处仅兜底事件流。
        logger.exception("Data transformation task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="data_agent",
            level="error",
            message="字段加工异常结束，请在任务历史中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)
