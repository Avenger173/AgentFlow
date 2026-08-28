"""文档助手的单 Agent 入口。

它和 `/api/chat` 的 Commander 规划入口并列：客户可以直接在“文档助手”页完成单文档任务，
也可以未来由 Commander 以 manager 模式调度同一套 Agent Definition。
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future

from app.schemas.document_agent import (
    DocumentAgentRunRequest,
    DocumentAgentRunResponse,
    DocumentDraftReviewRequest,
    DocumentDraftRestoreRequest,
    DocumentDraftMergeCandidateListResponse,
    DocumentDraftMergePlanResponse,
    DocumentDraftMergePreviewRequest,
    DocumentDraftVersionDiffResponse,
    DocumentDraftSectionBatchRevisionRequest,
    DocumentDraftSectionManualRevisionRequest,
    DocumentDraftTemplatePreviewRequest,
    DocumentDraftSectionRevisionRequest,
    DocumentDraftSectionReviewRequest,
    DocumentDraftSectionRequest,
    DocumentDraftSaveRequest,
    DocumentDraftSaveResponse,
    DocumentAgentTaskResultResponse,
    DocumentAgentTaskStartResponse,
)
from app.schemas.presentation import (
    PresentationExportRequest,
    PresentationExportResponse,
    PresentationPreviewRequest,
    PresentationPreviewResponse,
)
from app.schemas.presentation_studio import (
    PresentationStudioExportRequest,
    PresentationStudioPlanRequest,
    PresentationStudioPlanResponse,
    PresentationStudioTaskResultResponse,
    PresentationStudioTaskStartResponse,
)
from app.schemas.paper_review import (
    PaperReviewRequest,
    PaperReviewRunResponse,
    PaperReviewTaskResultResponse,
    PaperReviewTaskStartResponse,
)
from app.schemas.project_review import (
    ProjectReviewRequest,
    ProjectReviewRunResponse,
    ProjectReviewTaskResultResponse,
    ProjectReviewTaskStartResponse,
)
from app.services.document_agent import (
    DocumentAgentServiceError,
    DocumentDraftRevisionSuggestionNotFoundError,
    DocumentDraftRestoreNotFoundError,
    DocumentDraftMergeNotFoundError,
    DocumentDraftVersionDiffNotFoundError,
    DocumentDraftSectionNotFoundError,
    DocumentDraftSaveConflictError,
    DocumentDraftSaveConfirmationError,
    DocumentDraftSaveNotFoundError,
    build_document_draft_section_request,
    build_document_draft_section_batch_revision_request,
    build_document_draft_section_manual_revision_request,
    build_document_draft_template_preview_request,
    build_document_draft_merge_preview_request,
    build_document_draft_restore_request,
    build_document_draft_review_request,
    build_document_draft_section_revision_request,
    build_document_draft_section_review_request,
    get_document_agent_result,
    get_document_draft_parent_diff,
    get_document_draft_merge_candidates,
    get_document_draft_merge_plan,
    run_document_agent,
    save_document_draft,
)
from app.services.presentation_delivery import (
    PresentationConflictError,
    PresentationConfirmationError,
    PresentationDeliveryError,
    PresentationNotFoundError,
    build_project_proposal_preview,
    export_project_proposal_presentation,
)
from app.services.presentation_studio import (
    PresentationStudioServiceError,
    build_presentation_studio_plan,
    get_presentation_studio_result,
)
from app.services.presentation_studio_delivery import (
    PresentationStudioConfirmationError,
    PresentationStudioDeliveryError,
    PresentationStudioPlanConflictError,
    PresentationStudioPlanNotFoundError,
    export_presentation_studio_plan,
)
from app.services.project_review import (
    ProjectReviewServiceError,
    get_project_document_review_result,
    run_project_document_review,
)
from app.services.paper_review import (
    PaperReviewServiceError,
    get_paper_review_result,
    run_paper_review,
)
from app.services.task_event_stream import (
    finish_live_task_event_stream,
    has_live_task_event_stream,
    live_task_event_stream_finished,
    open_live_task_event_stream,
    publish_live_task_event,
)
from app.database.task_repository import append_workflow_event
from fastapi import APIRouter, HTTPException, status
from uuid import uuid4


router = APIRouter(prefix="/api/agents/document_agent", tags=["document-agent"])
logger = logging.getLogger(__name__)
_BACKGROUND_DOCUMENT_TASKS: set[asyncio.Task[None]] = set()


@router.post("/run", response_model=DocumentAgentRunResponse)
async def run_document_agent_endpoint(
    request: DocumentAgentRunRequest,
) -> DocumentAgentRunResponse:
    """运行一次只读、可追踪的文档助手任务。"""

    return await run_document_agent(request)


@router.post("/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_agent_endpoint(
    request: DocumentAgentRunRequest,
) -> DocumentAgentTaskStartResponse:
    """受理任务后立即返回，让 Qt 能先连接真实阶段事件流。"""

    task_id = f"task_document_{uuid4().hex[:12]}"
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="document_agent",
        message="文档分析任务已受理，正在建立受控执行上下文。",
    )
    task = asyncio.create_task(_run_document_agent_background(task_id, request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=task_id)


@router.post("/presentation-studio/run", response_model=PresentationStudioPlanResponse)
async def run_presentation_studio_endpoint(
    request: PresentationStudioPlanRequest,
) -> PresentationStudioPlanResponse:
    """同步生成一句需求对应的 PPT 创作计划，便于 API 集成与离线验收。"""

    try:
        return await build_presentation_studio_plan(request=request)
    except PresentationStudioServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/presentation-studio/start",
    response_model=PresentationStudioTaskStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_presentation_studio_endpoint(
    request: PresentationStudioPlanRequest,
) -> PresentationStudioTaskStartResponse:
    """立即受理 PPT 创作计划，让客户端展示真实的简报与计划阶段。"""

    task_id = f"task_presentation_studio_{uuid4().hex[:12]}"
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="document_agent",
        message="PPT 创作任务已受理，正在理解你的主题。",
    )
    task = asyncio.create_task(_run_presentation_studio_background(task_id, request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return PresentationStudioTaskStartResponse(task_id=task_id)


@router.get(
    "/presentation-studio/{task_id}/result",
    response_model=PresentationStudioTaskResultResponse,
)
async def get_presentation_studio_result_endpoint(task_id: str) -> PresentationStudioTaskResultResponse:
    """返回已校验的创作计划；运行中不提前泄露模型半成品。"""

    result = get_presentation_studio_result(task_id)
    if result is not None:
        return PresentationStudioTaskResultResponse(task_id=task_id, status="completed", result=result)
    if has_live_task_event_stream(task_id):
        return PresentationStudioTaskResultResponse(
            task_id=task_id,
            status="failed" if live_task_event_stream_finished(task_id) else "running",
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Presentation Studio task '{task_id}' was not found.")


@router.post(
    "/presentation-studio/{task_id}/export/prepare",
    response_model=PresentationStudioTaskStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def prepare_presentation_studio_export_endpoint(
    task_id: str,
) -> PresentationStudioTaskStartResponse:
    """为一次已经确认的导出预先建立实时阶段通道。

    Qt 必须先连接这个新通道，随后才发送真正的导出请求；否则同一 task_id 的旧“计划已完成”
    事件流可能先关闭 WebSocket，客户就会错过联网研究和文件回读阶段。这个入口不联网、不写
    文件、不校验或消耗计划，只解决实时事件流的订阅时序。
    """

    if get_presentation_studio_result(task_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到可导出的 PPT 创作计划。")
    open_live_task_event_stream(task_id)
    message = "已建立本次导出的实时状态通道，正在等待确认的交付步骤。"
    append_workflow_event(
        task_id=task_id,
        event_name="presentation_export_channel_ready",
        agent_id="document_agent",
        step_id="presentation_studio_export",
        message=message,
    )
    await publish_live_task_event(
        task_id=task_id,
        event="presentation_export_channel_ready",
        agent_id="document_agent",
        step_id="presentation_studio_export",
        message=message,
    )
    return PresentationStudioTaskStartResponse(task_id=task_id)


@router.post(
    "/presentation-studio/{task_id}/export",
    response_model=PresentationExportResponse,
)
async def export_presentation_studio_endpoint(
    task_id: str,
    request: PresentationStudioExportRequest,
) -> PresentationExportResponse:
    """只在客户确认后渲染 PPT 创作计划，并写入受控 artifact。"""

    # 正常 Qt 会先调用 prepare 并订阅新通道；同步 API 调用仍允许直接导出，因此这里兜底
    # 创建通道。两者都只影响内存事件缓冲，不改变计划、权限或文件写入语义。
    if not has_live_task_event_stream(task_id) or live_task_event_stream_finished(task_id):
        open_live_task_event_stream(task_id)
    queued_message = "PPT 导出已受理，正在等待已确认的交付步骤开始。"
    append_workflow_event(
        task_id=task_id,
        event_name="presentation_export_queued",
        agent_id="document_agent",
        step_id="presentation_studio_export",
        message=queued_message,
    )
    await publish_live_task_event(
        task_id=task_id,
        event="presentation_export_queued",
        agent_id="document_agent",
        step_id="presentation_studio_export",
        message=queued_message,
    )
    loop = asyncio.get_running_loop()

    def progress(event: str, message: str, level: str = "info") -> None:
        # 交付服务运行在 worker thread。先把事实追加进 SQLite，再回到 API loop 广播，保证
        # WebSocket 断开或用户稍晚打开历史页时也能看到相同阶段，而不是只有一条最终结果。
        append_workflow_event(
            task_id=task_id,
            event_name=event,
            agent_id="document_agent",
            step_id="presentation_studio_export",
            message=message,
            level=level,
        )
        future: Future[None] = asyncio.run_coroutine_threadsafe(
            publish_live_task_event(
                task_id=task_id,
                event=event,
                agent_id="document_agent",
                step_id="presentation_studio_export",
                message=message,
                level=level,  # type: ignore[arg-type]
            ),
            loop,
        )
        future.result()

    try:
        result = await asyncio.to_thread(
            export_presentation_studio_plan,
            task_id=task_id,
            request=request,
            progress_callback=progress,
        )
        _append_presentation_export_terminal_event(
            task_id=task_id,
            event_name="presentation_export_completed",
            message="PPT 已导出并通过回读验证，可在任务历史查看交付物与研究来源。",
            level="info",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="presentation_export_completed",
            agent_id="document_agent",
            step_id="presentation_studio_export",
            message="PPT 已导出并通过回读验证，可在任务历史查看交付物与研究来源。",
        )
        return result
    except PresentationStudioPlanNotFoundError as exc:
        _append_presentation_export_terminal_event(
            task_id=task_id,
            event_name="presentation_export_failed",
            message="PPT 导出未能开始：未找到可用的创作计划。",
            level="error",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="presentation_export_failed",
            agent_id="document_agent",
            step_id="presentation_studio_export",
            message="PPT 导出未能开始：未找到可用的创作计划。",
            level="error",
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (PresentationStudioPlanConflictError, PresentationStudioConfirmationError) as exc:
        _append_presentation_export_terminal_event(
            task_id=task_id,
            event_name="presentation_export_blocked",
            message="PPT 导出被当前确认或计划状态阻止，请检查提示后重新确认。",
            level="warning",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="presentation_export_blocked",
            agent_id="document_agent",
            step_id="presentation_studio_export",
            message="PPT 导出被当前确认或计划状态阻止，请检查提示后重新确认。",
            level="warning",
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PresentationStudioDeliveryError as exc:
        # 失败事件必须给出用户能行动的脱敏原因。旧文案只有“审查失败”，既无法区分图表回读、
        # 文件占用和格式错误，也迫使客户盲目重试；服务异常本身不包含密钥或模型原始响应。
        failure_message = f"PPT 导出未完成：{str(exc)[:180]}"
        _append_presentation_export_terminal_event(
            task_id=task_id,
            event_name="presentation_export_failed",
            message=failure_message,
            level="error",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="presentation_export_failed",
            agent_id="document_agent",
            step_id="presentation_studio_export",
            message=failure_message,
            level="error",
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    finally:
        await finish_live_task_event_stream(task_id)


def _append_presentation_export_terminal_event(
    *,
    task_id: str,
    event_name: str,
    message: str,
    level: str,
) -> None:
    """尽力记录导出终态；计划不存在时不让审计补写掩盖原始 HTTP 错误。"""

    try:
        append_workflow_event(
            task_id=task_id,
            event_name=event_name,
            agent_id="document_agent",
            step_id="presentation_studio_export",
            message=message,
            level=level,
        )
    except KeyError:
        return


@router.post("/project-review/run", response_model=ProjectReviewRunResponse)
async def run_project_document_review_endpoint(
    request: ProjectReviewRequest,
) -> ProjectReviewRunResponse:
    """同步执行项目文档质量门，便于 API 集成与离线验收。"""

    try:
        # 解析 PDF/DOCX 和大文本扫描属于 CPU/磁盘工作，不能占用 FastAPI 事件循环。
        return await asyncio.to_thread(run_project_document_review, request=request)
    except ProjectReviewServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/paper-review/run", response_model=PaperReviewRunResponse)
async def run_paper_review_endpoint(request: PaperReviewRequest) -> PaperReviewRunResponse:
    """同步执行论文形式审查，专注结构、引用、图表、格式和可读性规则。"""

    try:
        return await asyncio.to_thread(run_paper_review, request=request)
    except PaperReviewServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/paper-review/start",
    response_model=PaperReviewTaskStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_paper_review_endpoint(request: PaperReviewRequest) -> PaperReviewTaskStartResponse:
    """立即受理论文审查，让客户端显示真实读取与规则检查阶段。"""

    task_id = f"task_paper_review_{uuid4().hex[:12]}"
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="document_agent",
        message="论文审查已受理，正在确认受控材料范围。",
    )
    task = asyncio.create_task(_run_paper_review_background(task_id, request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return PaperReviewTaskStartResponse(task_id=task_id)


@router.post(
    "/project-review/start",
    response_model=ProjectReviewTaskStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_project_document_review_endpoint(
    request: ProjectReviewRequest,
) -> ProjectReviewTaskStartResponse:
    """立即受理项目审查，让 Qt 可复用 WebSocket 阶段反馈与结果轮询。"""

    task_id = f"task_project_review_{uuid4().hex[:12]}"
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="document_agent",
        message="项目文档审查已受理，正在确认受控材料范围。",
    )
    task = asyncio.create_task(_run_project_document_review_background(task_id, request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return ProjectReviewTaskStartResponse(task_id=task_id)


@router.get("/project-review/{task_id}/result", response_model=ProjectReviewTaskResultResponse)
async def get_project_document_review_result_endpoint(task_id: str) -> ProjectReviewTaskResultResponse:
    """返回已验证审查报告；运行中只给出阶段状态，不泄露半成品。"""

    result = get_project_document_review_result(task_id)
    if result is not None:
        return ProjectReviewTaskResultResponse(task_id=task_id, status=result.status, result=result)
    if has_live_task_event_stream(task_id):
        if live_task_event_stream_finished(task_id):
            return ProjectReviewTaskResultResponse(task_id=task_id, status="failed")
        return ProjectReviewTaskResultResponse(task_id=task_id, status="running")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project review task '{task_id}' was not found.")


@router.get("/paper-review/{task_id}/result", response_model=PaperReviewTaskResultResponse)
async def get_paper_review_result_endpoint(task_id: str) -> PaperReviewTaskResultResponse:
    """返回已验证的论文审查报告；运行中只返回状态，避免展示不完整结果。"""

    result = get_paper_review_result(task_id)
    if result is not None:
        return PaperReviewTaskResultResponse(task_id=task_id, status=result.status, result=result)
    if has_live_task_event_stream(task_id):
        if live_task_event_stream_finished(task_id):
            return PaperReviewTaskResultResponse(task_id=task_id, status="failed")
        return PaperReviewTaskResultResponse(task_id=task_id, status="running")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Paper review task '{task_id}' was not found.")


@router.get("/{task_id}/result", response_model=DocumentAgentTaskResultResponse)
async def get_document_agent_result_endpoint(task_id: str) -> DocumentAgentTaskResultResponse:
    """获取异步任务终态；运行中只返回状态，不提前暴露未验证的模型文本。"""

    result = get_document_agent_result(task_id)
    if result is not None:
        return DocumentAgentTaskResultResponse(task_id=task_id, status=result.status, result=result)
    if has_live_task_event_stream(task_id):
        if live_task_event_stream_finished(task_id):
            # 后台发生未预期异常时没有可恢复的正式结果，明确结束而不是让客户端无限轮询。
            return DocumentAgentTaskResultResponse(task_id=task_id, status="failed")
        return DocumentAgentTaskResultResponse(task_id=task_id, status="running")
    raise HTTPException(status_code=404, detail=f"Document Agent task '{task_id}' was not found.")


@router.post("/{task_id}/presentation-preview", response_model=PresentationPreviewResponse)
async def get_project_proposal_presentation_preview_endpoint(
    task_id: str,
    request: PresentationPreviewRequest,
) -> PresentationPreviewResponse:
    """返回已核验草稿的项目方案 PPT 计划；不调用模型，也不创建文件。"""

    del request
    try:
        return await asyncio.to_thread(build_project_proposal_preview, task_id=task_id)
    except PresentationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PresentationDeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{task_id}/presentations/export", response_model=PresentationExportResponse)
async def export_project_proposal_presentation_endpoint(
    task_id: str,
    request: PresentationExportRequest,
) -> PresentationExportResponse:
    """确认后在后台线程渲染项目方案 PPT，并追加到原文档任务的审计链。"""

    try:
        return await asyncio.to_thread(
            export_project_proposal_presentation,
            task_id=task_id,
            request=request,
        )
    except PresentationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (PresentationConflictError, PresentationConfirmationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PresentationDeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/{task_id}/version-diff", response_model=DocumentDraftVersionDiffResponse)
async def get_document_draft_parent_diff_endpoint(task_id: str) -> DocumentDraftVersionDiffResponse:
    """返回当前草稿与直接父版本的只读差异，不创建新任务或修改版本链。"""

    try:
        return get_document_draft_parent_diff(task_id=task_id)
    except DocumentDraftVersionDiffNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/{task_id}/merge-candidates", response_model=DocumentDraftMergeCandidateListResponse)
async def get_document_draft_merge_candidates_endpoint(task_id: str) -> DocumentDraftMergeCandidateListResponse:
    """列出当前已核验完整草稿可以选择的同根合并候选，不返回正文。"""

    try:
        return get_document_draft_merge_candidates(task_id=task_id)
    except DocumentDraftMergeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/{task_id}/merge-plan/{other_task_id}", response_model=DocumentDraftMergePlanResponse)
async def get_document_draft_merge_plan_endpoint(
    task_id: str,
    other_task_id: str,
) -> DocumentDraftMergePlanResponse:
    """返回只读三方合并计划，让客户端先展示共同祖先与冲突。"""

    try:
        return get_document_draft_merge_plan(
            primary_task_id=task_id,
            secondary_task_id=other_task_id,
        )
    except DocumentDraftMergeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{task_id}/save-draft", response_model=DocumentDraftSaveResponse)
async def save_document_draft_endpoint(
    task_id: str,
    request: DocumentDraftSaveRequest,
) -> DocumentDraftSaveResponse:
    """保存用户已确认的 Markdown 草稿，不接收任意本机输出路径。"""

    try:
        return save_document_draft(task_id=task_id, request=request)
    except DocumentDraftSaveNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (DocumentDraftSaveConflictError, DocumentDraftSaveConfirmationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{task_id}/restore-preview/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_restore_preview_endpoint(
    task_id: str,
    request: DocumentDraftRestoreRequest,
) -> DocumentAgentTaskStartResponse:
    """把历史草稿恢复为新的独立预览，绝不覆盖旧任务或已保存文件。"""

    # request 保留为空白 Pydantic 契约：调用方只能通过 URL 选择一个历史任务，不能提交正文、
    # 输出路径或覆盖选项。显式接收它可让 FastAPI 对错误 payload 保持一致的 422 反馈。
    del request
    try:
        derived_request = build_document_draft_restore_request(source_task_id=task_id)
    except DocumentDraftRestoreNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_restore_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="旧版本恢复预览已受理，正在校验历史快照身份。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/template-preview/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_template_preview_endpoint(
    task_id: str,
    request: DocumentDraftTemplatePreviewRequest,
) -> DocumentAgentTaskStartResponse:
    """从已核验草稿创建固定模板交付预览，不接收正文、路径或写入选项。"""

    try:
        derived_request = build_document_draft_template_preview_request(
            source_task_id=task_id,
            request=request,
        )
    except DocumentDraftRestoreNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_template_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="模板化交付预览已受理，正在校验已核验草稿快照。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/merge-preview/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_merge_preview_endpoint(
    task_id: str,
    request: DocumentDraftMergePreviewRequest,
) -> DocumentAgentTaskStartResponse:
    """按已确认冲突选择建立同根章节合并预览，不接收正文、路径或写入选项。"""

    try:
        derived_request = build_document_draft_merge_preview_request(
            primary_task_id=task_id,
            request=request,
        )
    except DocumentDraftMergeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_merge_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="章节合并预览已受理，正在校验版本链与冲突选择。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/draft-sections/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_section_endpoint(
    task_id: str,
    request: DocumentDraftSectionRequest,
) -> DocumentAgentTaskStartResponse:
    """从已完成草稿派生一个单章节创作预览，原草稿和文件均不受影响。"""

    try:
        derived_request = build_document_draft_section_request(
            source_task_id=task_id,
            request=request,
        )
    except DocumentDraftSectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_section_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="本章创作任务已受理，正在恢复原草稿的受控材料范围。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/draft-review/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_review_endpoint(
    task_id: str,
    request: DocumentDraftReviewRequest,
) -> DocumentAgentTaskStartResponse:
    """派生只读事实核验任务；不接受草稿正文、文件路径或写入选项。"""

    try:
        derived_request = build_document_draft_review_request(source_task_id=task_id, request=request)
    except DocumentDraftSectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_review_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="草稿事实核验已受理，正在恢复原草稿的受控材料范围。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/draft-sections/review/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_section_review_endpoint(
    task_id: str,
    request: DocumentDraftSectionReviewRequest,
) -> DocumentAgentTaskStartResponse:
    """派生一份只读本章审校建议；不接收正文、路径或写入选项。"""

    try:
        derived_request = build_document_draft_section_review_request(
            source_task_id=task_id,
            request=request,
        )
    except DocumentDraftSectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_section_review_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="本章审校已受理，正在恢复原草稿的受控材料范围。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/draft-sections/manual-revision-preview/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_section_manual_revision_preview_endpoint(
    task_id: str,
    request: DocumentDraftSectionManualRevisionRequest,
) -> DocumentAgentTaskStartResponse:
    """建立用户手动修订的待核验预览；不会改写原稿或创建文件。"""

    try:
        derived_request = build_document_draft_section_manual_revision_request(
            source_task_id=task_id,
            request=request,
        )
    except DocumentDraftSectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_manual_revision_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="手动修订预览已受理，正在校验原草稿快照；完成后仍需重新核验来源。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/draft-sections/revision-preview/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_section_revision_preview_endpoint(
    task_id: str,
    request: DocumentDraftSectionRevisionRequest,
) -> DocumentAgentTaskStartResponse:
    """把一条已审校建议转成独立版本预览，不覆盖草稿或直接写文件。"""

    try:
        derived_request = build_document_draft_section_revision_request(
            source_review_task_id=task_id,
            request=request,
        )
    except (DocumentDraftSectionNotFoundError, DocumentDraftRevisionSuggestionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_section_revision_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="修订预览已受理，正在校验审校建议与原草稿快照。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


@router.post("/{task_id}/draft-sections/revision-batch-preview/start", response_model=DocumentAgentTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_document_draft_section_batch_revision_preview_endpoint(
    task_id: str,
    request: DocumentDraftSectionBatchRevisionRequest,
) -> DocumentAgentTaskStartResponse:
    """把同章且无重叠的多条审校建议合并为一个独立预览版本。"""

    try:
        derived_request = build_document_draft_section_batch_revision_request(
            source_review_task_id=task_id,
            request=request,
        )
    except (DocumentDraftSectionNotFoundError, DocumentDraftRevisionSuggestionNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAgentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    derived_task_id = f"task_document_section_revision_batch_{uuid4().hex[:12]}"
    open_live_task_event_stream(derived_task_id)
    await publish_live_task_event(
        task_id=derived_task_id,
        event="task_queued",
        agent_id="document_agent",
        message="多建议修订预览已受理，正在校验候选片段是否可安全合并。",
    )
    task = asyncio.create_task(_run_document_agent_background(derived_task_id, derived_request))
    _BACKGROUND_DOCUMENT_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_DOCUMENT_TASKS.discard)
    return DocumentAgentTaskStartResponse(task_id=derived_task_id)


async def _run_presentation_studio_background(
    task_id: str,
    request: PresentationStudioPlanRequest,
) -> None:
    """后台建立创作简报与逐页计划，复用既有事件流但不提前写入文件。"""

    async def progress(stage: str, message: str) -> None:
        await publish_live_task_event(
            task_id=task_id,
            event=stage,
            agent_id="document_agent",
            message=message,
        )

    try:
        await publish_live_task_event(
            task_id=task_id,
            event="task_started",
            agent_id="document_agent",
            message="正在将一句主题整理为演示简报，不会创建文件或联网下载素材。",
        )
        result = await build_presentation_studio_plan(
            request=request,
            task_id=task_id,
            progress_callback=progress,
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id="document_agent",
            message=(
                f"已生成 {len(result.slides)} 页 PPT 创作计划；请确认内容和视觉方向后再导出。"
            ),
            level="warning" if result.warnings else "info",
        )
    except Exception:
        logger.exception("Presentation Studio background task failed: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="document_agent",
            message="PPT 创作计划在运行时发生未预期错误，请在任务历史查看详情后重试。",
            level="error",
        )
    finally:
        await finish_live_task_event_stream(task_id)


async def _run_document_agent_background(task_id: str, request: DocumentAgentRunRequest) -> None:
    """把同步 HTTP 结果接口包成可观察的后台任务，不改变原有 /run 兼容入口。"""

    async def progress(stage: str, message: str, level: str = "info") -> None:
        await publish_live_task_event(
            task_id=task_id,
            event=stage,
            agent_id="document_agent",
            message=message,
            level=level,  # type: ignore[arg-type]
        )

    try:
        await publish_live_task_event(
            task_id=task_id,
            event="task_started",
            agent_id="document_agent",
            message=(
                "文档助手开始生成单章节创作预览。"
                if request.output_mode == "section_draft"
                else "文档助手开始生成独立修订预览。"
                if request.output_mode in {"section_revision", "section_revision_batch"}
                else "文档助手开始建立待重新核验的手动修订预览。"
                if request.output_mode == "section_manual_revision"
                else "文档助手开始建立历史草稿恢复预览。"
                if request.output_mode == "draft_restore"
                else "文档助手开始建立模板化交付预览。"
                if request.output_mode == "draft_template"
                else "文档助手开始建立章节合并预览。"
                if request.output_mode == "draft_merge"
                else "文档助手开始核验草稿事实。"
                if request.output_mode == "draft_review"
                else "文档助手开始审校草稿章节。"
                if request.output_mode == "section_review"
                else "文档助手开始只读分析。"
            ),
        )
        result = await run_document_agent(request, task_id=task_id, progress_callback=progress)
        terminal_event = "task_completed" if result.status == "completed" else "task_waiting" if result.status in {"needs_clarification", "insufficient_context"} else "task_failed"
        await publish_live_task_event(
            task_id=task_id,
            event=terminal_event,
            agent_id="document_agent",
            message=(
                "本章创作完成，正在展示可验证预览。"
                if result.status == "completed" and request.output_mode == "section_draft"
                else "修订预览完成，正在展示章节差异。"
                if result.status == "completed" and request.output_mode in {"section_revision", "section_revision_batch"}
                else "手动修订预览已建立；请重新核验来源后再保存。"
                if result.status == "completed" and request.output_mode == "section_manual_revision"
                else "历史草稿已恢复为新的独立预览；旧任务和文件未改动。"
                if result.status == "completed" and request.output_mode == "draft_restore"
                else "模板化交付预览已建立；未匹配章节已明确标记，确认后可另存。"
                if result.status == "completed" and request.output_mode == "draft_template"
                else "章节合并预览已建立；已记录共同祖先与冲突处理，确认后可另存。"
                if result.status == "completed" and request.output_mode == "draft_merge"
                else "草稿事实核验完成，正在展示可追溯结论。"
                if result.status == "completed" and request.output_mode == "draft_review"
                else "本章审校完成，正在展示候选建议。"
                if result.status == "completed" and request.output_mode == "section_review"
                else "分析完成，正在展示可验证结果。"
                if result.status == "completed"
                else result.reply
            ),
            level="info" if result.status == "completed" else "warning" if terminal_event == "task_waiting" else "error",
        )
    except Exception:
        logger.exception("Document Agent background task failed: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="document_agent",
            message="文档助手在运行时发生未预期错误，请在任务历史查看详情后重试。",
            level="error",
        )
    finally:
        await finish_live_task_event_stream(task_id)


async def _run_project_document_review_background(
    task_id: str,
    request: ProjectReviewRequest,
) -> None:
    """在后台执行确定性项目审查，并向既有任务事件流广播真实阶段。"""

    try:
        await publish_live_task_event(
            task_id=task_id,
            event="task_started",
            agent_id="document_agent",
            message="正在读取选定项目材料并建立可追溯定位。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="project_review_rules_started",
            agent_id="document_agent",
            message="正在检查范围、验收、责任、节点、风险依赖与术语口径。",
        )
        result = await asyncio.to_thread(
            run_project_document_review,
            request=request,
            task_id=task_id,
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id="document_agent",
            message=result.report.summary,
            level="warning" if result.report.findings else "info",
        )
    except ProjectReviewServiceError as exc:
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="document_agent",
            message=f"项目文档审查未完成：{exc}",
            level="error",
        )
    except Exception:
        logger.exception("Project document review background task failed: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="document_agent",
            message="项目文档审查在运行时发生未预期错误，请在任务历史查看详情后重试。",
            level="error",
        )
    finally:
        await finish_live_task_event_stream(task_id)


async def _run_paper_review_background(task_id: str, request: PaperReviewRequest) -> None:
    """在后台执行论文形式审查，并复用统一实时事件流和终态恢复方式。"""

    try:
        await publish_live_task_event(
            task_id=task_id,
            event="task_started",
            agent_id="document_agent",
            message="正在读取选定论文材料并建立可追溯定位。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="paper_review_rules_started",
            agent_id="document_agent",
            message="正在检查论文结构、引用、图表、标题格式与可读性。",
        )
        result = await asyncio.to_thread(run_paper_review, request=request, task_id=task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id="document_agent",
            message=result.report.summary,
            level="warning" if result.report.findings else "info",
        )
    except PaperReviewServiceError as exc:
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="document_agent",
            message=f"论文审查未完成：{exc}",
            level="error",
        )
    except Exception:
        logger.exception("Paper review background task failed: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="document_agent",
            message="论文审查在运行时发生未预期错误，请在任务历史查看详情后重试。",
            level="error",
        )
    finally:
        await finish_live_task_event_stream(task_id)
