"""本地知识库 K1 的受控 HTTP 入口。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from threading import Lock
from time import monotonic
from uuid import uuid4

from app.database.knowledge_repository import (
    KnowledgeBaseConflictError,
    KnowledgeBaseDeletionPendingError,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseUnavailableError,
    create_knowledge_base,
    finalize_knowledge_base_deletion,
    import_workspace_documents_to_knowledge_base,
    list_knowledge_bases,
    list_knowledge_documents,
    request_knowledge_base_deletion,
)
from app.schemas.knowledge import (
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    KnowledgeAnswerTaskResultResponse,
    KnowledgeAnswerTaskStartResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseListResponse,
    KnowledgeBaseRecord,
    KnowledgeDocumentImportRequest,
    KnowledgeDocumentImportResponse,
    KnowledgeDocumentListResponse,
    KnowledgeDeepTaskControlResponse,
    KnowledgeDeepTaskReportExportRequest,
    KnowledgeDeepTaskReportExportResponse,
    KnowledgeDeepTaskRequest,
    KnowledgeDeepTaskResultResponse,
    KnowledgeDeepTaskScope,
    KnowledgeDeepTaskStartResponse,
    KnowledgeEmbeddingPrepareRequest,
    KnowledgeEmbeddingPrepareResponse,
    KnowledgeIndexJobListResponse,
    KnowledgeIndexJobRecord,
    KnowledgeOcrCapabilityResponse,
    KnowledgeOcrPreparationResponse,
    KnowledgeOcrPrepareRequest,
    KnowledgePerformanceProfileResponse,
    KnowledgeRetrievalRequest,
    KnowledgeRetrievalResult,
)
from app.services.knowledge_keyword_index import (
    KnowledgeIndexJobNotFoundError,
    create_knowledge_index_job,
    get_knowledge_index_job,
    list_knowledge_index_jobs,
    run_knowledge_index_job,
)
from app.services.knowledge_vector_index import (
    EMBEDDING_MODEL_NAME,
    prepare_local_embedding_model,
    vector_index_capability,
)
from app.services.ocr_adapter import (
    OCR_MODEL_PROFILE,
    OcrDependencyInstallError,
    OcrAdapterError,
    install_local_ocr_dependencies,
    ocr_capability,
    prepare_local_ocr_model,
)
from app.services.knowledge_retrieval import (
    KnowledgeRetrievalUnavailableError,
    retrieve_knowledge_evidence,
)
from app.services.knowledge_answer import (
    answer_knowledge_question,
    create_knowledge_answer_queued_run,
    get_knowledge_answer_task_result,
    run_knowledge_answer_task,
)
from app.services.knowledge_deep_task import (
    KnowledgeDeepTaskMapExecutionError,
    KnowledgeDeepTaskScopeError,
    build_knowledge_deep_task_scope,
    create_knowledge_deep_task_map_queued_run,
    get_knowledge_deep_task_result,
    mark_knowledge_deep_task_unexpected_failure,
    request_knowledge_deep_task_cancel,
    request_knowledge_deep_task_pause,
    resume_knowledge_deep_task,
    run_knowledge_deep_task,
)
from app.services.knowledge_deep_report import (
    KnowledgeDeepTaskReportConfirmationError,
    KnowledgeDeepTaskReportConflictError,
    KnowledgeDeepTaskReportExportError,
    KnowledgeDeepTaskReportNotFoundError,
    KnowledgeDeepTaskReportNotReadyError,
    export_knowledge_deep_task_report,
)
from app.services.knowledge_evidence_gate import KnowledgeEvidenceUnavailableError
from app.services.knowledge_performance import (
    build_knowledge_performance_profile,
    record_knowledge_deep_task_elapsed_ms,
)
from app.services.knowledge_runtime_queue import knowledge_runtime_queue
from app.services.task_event_stream import (
    finish_live_task_event_stream,
    has_live_task_event_stream,
    live_task_event_stream_finished,
    open_live_task_event_stream,
    publish_live_task_event,
)
from app.services.workspace_documents import WorkspaceDocumentError
from fastapi import APIRouter, HTTPException, status


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
_BACKGROUND_KNOWLEDGE_JOBS: set[asyncio.Task[None]] = set()
_BACKGROUND_KNOWLEDGE_ANSWER_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_KNOWLEDGE_DEEP_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_KNOWLEDGE_DEEP_TASK_IDS: set[str] = set()
# OCR 模型准备不处理客户文件，也不应伪装成知识库任务。它只保存很小的进程内状态，后端
# 重启后以 ready marker 为准；这样不会把模型目录、下载日志或材料信息写进 SQLite。
_BACKGROUND_OCR_PREPARATION_TASKS: set[asyncio.Task[None]] = set()
_OCR_PREPARATIONS: OrderedDict[str, "_OcrPreparationState"] = OrderedDict()
_OCR_PREPARATIONS_LOCK = Lock()
_OCR_PREPARATION_HISTORY_LIMIT = 8
logger = logging.getLogger(__name__)


@dataclass
class _OcrPreparationState:
    """K7.4 的短生命周期准备状态；消息必须是可展示的脱敏中文说明。"""

    preparation_id: str
    status: str
    model_profile: str
    message: str
    started_at: str
    completed_at: str = ""


def _utc_timestamp() -> str:
    """返回稳定的 UTC 时间戳，避免把本机时区或文件时间混入准备状态。"""

    return datetime.now(timezone.utc).isoformat()


def _ocr_preparation_response(state: _OcrPreparationState) -> KnowledgeOcrPreparationResponse:
    """把私有可变状态收束为稳定 API 契约。"""

    return KnowledgeOcrPreparationResponse(
        preparation_id=state.preparation_id,
        status=state.status,  # type: ignore[arg-type]
        model_profile=state.model_profile,
        message=state.message,
        started_at=state.started_at,
        completed_at=state.completed_at,
    )


def _active_ocr_preparation() -> _OcrPreparationState | None:
    """防止双击或多个页面同时触发重复模型下载。"""

    with _OCR_PREPARATIONS_LOCK:
        for state in reversed(_OCR_PREPARATIONS.values()):
            if state.status in {"queued", "preparing"}:
                return state
    return None


def _get_ocr_preparation(preparation_id: str) -> _OcrPreparationState | None:
    with _OCR_PREPARATIONS_LOCK:
        return _OCR_PREPARATIONS.get(preparation_id)


def _update_ocr_preparation(
    preparation_id: str,
    *,
    status: str,
    message: str,
    completed: bool = False,
) -> None:
    """仅更新受控状态字段，并保留最近有限条记录供前端重连后读取。"""

    with _OCR_PREPARATIONS_LOCK:
        state = _OCR_PREPARATIONS.get(preparation_id)
        if state is None:
            return
        state.status = status
        state.message = message
        if completed:
            state.completed_at = _utc_timestamp()


def _create_ocr_preparation() -> _OcrPreparationState:
    """创建一次准备记录；必须在启动后台协程前完成，避免状态窗口为空。"""

    state = _OcrPreparationState(
        preparation_id=f"ocr_prepare_{uuid4().hex[:16]}",
        status="queued",
        model_profile=OCR_MODEL_PROFILE,
        message="本地 OCR 准备已受理，正在等待本机任务开始。",
        started_at=_utc_timestamp(),
    )
    with _OCR_PREPARATIONS_LOCK:
        _OCR_PREPARATIONS[state.preparation_id] = state
        while len(_OCR_PREPARATIONS) > _OCR_PREPARATION_HISTORY_LIMIT:
            oldest_id, oldest_state = next(iter(_OCR_PREPARATIONS.items()))
            if oldest_state.status in {"queued", "preparing"}:
                break
            _OCR_PREPARATIONS.pop(oldest_id)
    return state


def _ocr_preparation_failure_message(error: BaseException) -> str:
    """将 optional 依赖和模型失败归类为客户可行动提示，不透传异常或路径。"""

    if isinstance(error, OcrDependencyInstallError):
        return str(error)
    if isinstance(error, OcrAdapterError):
        if error.code == "ocr_not_installed":
            return "本机 OCR 可选组件尚未安装；请先安装可选组件后再准备模型。"
        if error.code == "ocr_not_ready":
            return "本地 OCR 模型没有准备完整，请重新确认准备。"
        return "本地 OCR 准备未完成，请检查可选组件后重试。"
    return "本地 OCR 准备失败，请稍后重试或检查可选组件。"


async def _run_ocr_preparation_background(preparation_id: str) -> None:
    """在工作线程安装确认后的可选组件并准备模型；其它路径绝不调用这些函数。"""

    _update_ocr_preparation(
        preparation_id,
        status="preparing",
        message="正在安装本地 OCR 可选组件；不会读取或上传任何材料。",
    )
    try:
        await asyncio.to_thread(install_local_ocr_dependencies)
        _update_ocr_preparation(
            preparation_id,
            status="preparing",
            message="可选组件已安装，正在准备约 29MB 本地 OCR 模型权重；窗口可继续使用。",
        )
        capability = await asyncio.to_thread(prepare_local_ocr_model, allow_download=True)
    except Exception as error:  # 具体底层日志不进入客户状态或 API 响应。
        _update_ocr_preparation(
            preparation_id,
            status="failed",
            message=_ocr_preparation_failure_message(error),
            completed=True,
        )
        return

    if capability.model_initialized:
        _update_ocr_preparation(
            preparation_id,
            status="ready",
            message="本地 OCR 已准备完成；扫描 PDF 和图片将在本机识别，不会上传材料。",
            completed=True,
        )
        return
    _update_ocr_preparation(
        preparation_id,
        status="failed",
        message="本地 OCR 模型没有准备完整，请重新确认准备。",
        completed=True,
    )


def _track_ocr_preparation(task: asyncio.Task[None]) -> None:
    """持有后台任务直到结束，避免事件循环提前回收正在下载的准备协程。"""

    _BACKGROUND_OCR_PREPARATION_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_OCR_PREPARATION_TASKS.discard)


@router.get("/bases", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases_endpoint() -> KnowledgeBaseListResponse:
    """列出资料库元数据，不读取每份文档或索引正文。"""

    return KnowledgeBaseListResponse(knowledge_bases=await asyncio.to_thread(list_knowledge_bases))


@router.post("/bases", response_model=KnowledgeBaseRecord, status_code=201)
async def create_knowledge_base_endpoint(request: KnowledgeBaseCreateRequest) -> KnowledgeBaseRecord:
    """创建空资料库；不会导入文件、下载模型或开始索引。"""

    try:
        return await asyncio.to_thread(create_knowledge_base, name=request.name, description=request.description)
    except KnowledgeBaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/bases/{knowledge_base_id}", response_model=KnowledgeBaseRecord, status_code=202)
async def delete_knowledge_base_endpoint(knowledge_base_id: str) -> KnowledgeBaseRecord:
    """受理资料库删除，并在后台等待运行中索引安全停驻后清理。

    客户请求只会影响知识库私有副本和派生数据；已有 workspace 原材料不会被改名、覆盖或删除。
    """

    try:
        deleting = await asyncio.to_thread(request_knowledge_base_deletion, knowledge_base_id)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    task = asyncio.create_task(_run_knowledge_deletion_background(knowledge_base_id))
    _BACKGROUND_KNOWLEDGE_JOBS.add(task)
    task.add_done_callback(_BACKGROUND_KNOWLEDGE_JOBS.discard)
    return deleting


@router.get("/bases/{knowledge_base_id}/documents", response_model=KnowledgeDocumentListResponse)
async def list_knowledge_documents_endpoint(knowledge_base_id: str) -> KnowledgeDocumentListResponse:
    try:
        documents = await asyncio.to_thread(list_knowledge_documents, knowledge_base_id)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeDocumentListResponse(knowledge_base_id=knowledge_base_id, documents=documents)


@router.post("/documents/import", response_model=KnowledgeDocumentImportResponse, status_code=201)
async def import_knowledge_documents_endpoint(
    request: KnowledgeDocumentImportRequest,
) -> KnowledgeDocumentImportResponse:
    """从既有受控 workspace 复制材料，不接受任意路径或正文直传。"""

    try:
        return await asyncio.to_thread(
            import_workspace_documents_to_knowledge_base,
            knowledge_base_id=request.knowledge_base_id,
            workspace_document_names=request.workspace_document_names,
        )
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KnowledgeBaseUnavailableError, WorkspaceDocumentError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bases/{knowledge_base_id}/index/start", response_model=KnowledgeIndexJobRecord, status_code=202)
async def start_knowledge_index_endpoint(knowledge_base_id: str) -> KnowledgeIndexJobRecord:
    """受理关键词索引任务并转入后台，避免解析/FTS 写入阻塞 Qt 请求。"""

    try:
        job = await asyncio.to_thread(create_knowledge_index_job, knowledge_base_id)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KnowledgeBaseUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job.status == "queued":
        task = asyncio.create_task(_run_index_job_background(job.index_job_id))
        _BACKGROUND_KNOWLEDGE_JOBS.add(task)
        task.add_done_callback(_BACKGROUND_KNOWLEDGE_JOBS.discard)
    return job


@router.get("/index-jobs/{index_job_id}", response_model=KnowledgeIndexJobRecord)
async def get_knowledge_index_job_endpoint(index_job_id: str) -> KnowledgeIndexJobRecord:
    try:
        return await asyncio.to_thread(get_knowledge_index_job, index_job_id)
    except KnowledgeIndexJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/bases/{knowledge_base_id}/index-jobs", response_model=KnowledgeIndexJobListResponse)
async def list_knowledge_index_jobs_endpoint(knowledge_base_id: str) -> KnowledgeIndexJobListResponse:
    try:
        jobs = await asyncio.to_thread(list_knowledge_index_jobs, knowledge_base_id)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeIndexJobListResponse(knowledge_base_id=knowledge_base_id, jobs=jobs)


@router.get("/vector-capability")
async def get_vector_capability_endpoint() -> dict[str, object]:
    """只诊断依赖和受控缓存标记，不加载或下载本地模型。"""

    return vector_index_capability().__dict__


@router.get("/ocr-capability", response_model=KnowledgeOcrCapabilityResponse)
async def get_ocr_capability_endpoint() -> KnowledgeOcrCapabilityResponse:
    """读取 OCR 可选组件与 ready marker；该诊断不导入模型、不读取材料、不联网。"""

    capability = ocr_capability()
    return KnowledgeOcrCapabilityResponse(
        paddleocr_available=capability.paddleocr_available,
        model_initialized=capability.model_initialized,
        profile=capability.profile,
        message=capability.message,
    )


@router.post(
    "/ocr-model/prepare",
    response_model=KnowledgeOcrPreparationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def prepare_ocr_model_endpoint(
    request: KnowledgeOcrPrepareRequest,
) -> KnowledgeOcrPreparationResponse:
    """客户确认后异步准备本地 OCR；导入、解析和索引路径永远不会调用此入口。"""

    # Pydantic Literal 仍保留在接口层：即使未来客户端误传值，也不能把它解释成下载许可。
    del request
    existing = _active_ocr_preparation()
    if existing is not None:
        return _ocr_preparation_response(existing)

    state = _create_ocr_preparation()
    task = asyncio.create_task(_run_ocr_preparation_background(state.preparation_id))
    _track_ocr_preparation(task)
    return _ocr_preparation_response(state)


@router.get(
    "/ocr-preparations/{preparation_id}",
    response_model=KnowledgeOcrPreparationResponse,
)
async def get_ocr_preparation_endpoint(preparation_id: str) -> KnowledgeOcrPreparationResponse:
    """返回同一次显式准备的真实阶段；服务重启后请重新读取 capability 作为最终事实。"""

    state = _get_ocr_preparation(preparation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="OCR preparation was not found. Please refresh capability.")
    return _ocr_preparation_response(state)


@router.get("/performance", response_model=KnowledgePerformanceProfileResponse)
async def get_knowledge_performance_endpoint() -> KnowledgePerformanceProfileResponse:
    """返回 K5.8 的本机性能建议与真实进程队列，不读取任何客户材料。"""

    profile = await asyncio.to_thread(build_knowledge_performance_profile)
    queue_snapshot = await knowledge_runtime_queue.snapshot()
    return profile.model_copy(update={"runtime_queue": queue_snapshot})


@router.post("/vector-model/prepare", response_model=KnowledgeEmbeddingPrepareResponse)
async def prepare_vector_model_endpoint(
    request: KnowledgeEmbeddingPrepareRequest,
) -> KnowledgeEmbeddingPrepareResponse:
    """客户明确确认后才允许下载并初始化本地 Embedding 模型。"""

    dimension = await asyncio.to_thread(
        prepare_local_embedding_model,
        allow_download=request.confirm_download,
    )
    return KnowledgeEmbeddingPrepareResponse(
        status="ready",
        model=EMBEDDING_MODEL_NAME,
        dimension=dimension,
        message="本地 Embedding 模型已准备，可用于后续语义索引。",
    )


@router.post("/retrieve", response_model=KnowledgeRetrievalResult)
async def retrieve_knowledge_endpoint(request: KnowledgeRetrievalRequest) -> KnowledgeRetrievalResult:
    """K2 只读证据检索入口，不生成答案、不写任务历史也不触发模型下载。

    此接口面向后续 Evidence Gate、K3 和受控客户阅读区。返回的是有限来源证据与检索诊断，
    不是模型结论；调用者不得据此伪造“资料库已回答”。
    """

    try:
        return await asyncio.to_thread(retrieve_knowledge_evidence, request)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KnowledgeBaseUnavailableError, KnowledgeRetrievalUnavailableError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/answer", response_model=KnowledgeAnswerResponse)
async def answer_knowledge_endpoint(request: KnowledgeAnswerRequest) -> KnowledgeAnswerResponse:
    """K3 的受约束回答入口。

    服务先检索、再经 Evidence Gate 核验，模型只会拿到有限活动版本正文；模型输出必须回指本轮
    source_id，且提交前再核验一次 generation。当前不写任务历史、不提供流式输出或 Commander 路由。
    """

    try:
        return await answer_knowledge_question(request)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        KnowledgeBaseUnavailableError,
        KnowledgeRetrievalUnavailableError,
        KnowledgeEvidenceUnavailableError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/answer/start", response_model=KnowledgeAnswerTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_knowledge_answer_endpoint(
    request: KnowledgeAnswerRequest,
) -> KnowledgeAnswerTaskStartResponse:
    """受理 K3 可信问答，立即返回任务 ID 并通过既有 WebSocket 推送真实阶段。"""

    task_id = f"task_kb_{uuid4().hex[:12]}"
    create_knowledge_answer_queued_run(task_id=task_id, request=request)
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="knowledge_agent",
        message="知识库问答已受理，将只读取当前活动资料版本。",
    )
    task = asyncio.create_task(_run_knowledge_answer_background(task_id, request))
    _BACKGROUND_KNOWLEDGE_ANSWER_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_KNOWLEDGE_ANSWER_TASKS.discard)
    return KnowledgeAnswerTaskStartResponse(task_id=task_id)


@router.get("/answers/{task_id}/result", response_model=KnowledgeAnswerTaskResultResponse)
async def get_knowledge_answer_result_endpoint(task_id: str) -> KnowledgeAnswerTaskResultResponse:
    """读取 K3 问答状态或终态；完成后从 SQLite 恢复，服务重启不依赖内存缓存。"""

    result = get_knowledge_answer_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return KnowledgeAnswerTaskResultResponse(
            task_id=task_id,
            status="running",
            message="知识库问答正在执行。",
        )
    raise HTTPException(status_code=404, detail=f"Knowledge answer task '{task_id}' was not found.")


@router.post("/deep-tasks/start", response_model=KnowledgeDeepTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_knowledge_deep_task_endpoint(
    request: KnowledgeDeepTaskRequest,
) -> KnowledgeDeepTaskStartResponse:
    """受理 K4 深度任务，先冻结当前 generation 范围再后台执行 Map/Reduce。"""

    try:
        scope = await asyncio.to_thread(build_knowledge_deep_task_scope, request)
    except KnowledgeBaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KnowledgeBaseUnavailableError, KnowledgeDeepTaskScopeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    task_id = f"task_k4_{uuid4().hex[:12]}"
    try:
        await asyncio.to_thread(create_knowledge_deep_task_map_queued_run, task_id=task_id, scope=scope)
    except KnowledgeDeepTaskMapExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="knowledge_agent",
        message=f"知识库深度任务已受理，已冻结 {len(scope.map_units)} 个章节的活动范围。",
    )
    task = asyncio.create_task(_run_knowledge_deep_task_background(task_id, scope))
    _track_knowledge_deep_task(task, task_id=task_id)
    return KnowledgeDeepTaskStartResponse(task_id=task_id)


@router.get("/deep-tasks/{task_id}/result", response_model=KnowledgeDeepTaskResultResponse)
async def get_knowledge_deep_task_result_endpoint(task_id: str) -> KnowledgeDeepTaskResultResponse:
    """读取 K4 的可恢复状态；服务重启后依旧从 SQLite 取回冻结范围与 Reduce checkpoint。"""

    result = await asyncio.to_thread(get_knowledge_deep_task_result, task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return KnowledgeDeepTaskResultResponse(task_id=task_id, status="running", summary="知识库深度任务正在执行。")
    raise HTTPException(status_code=404, detail=f"Knowledge deep task '{task_id}' was not found.")


@router.post(
    "/deep-tasks/{task_id}/report",
    response_model=KnowledgeDeepTaskReportExportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def export_knowledge_deep_task_report_endpoint(
    task_id: str,
    request: KnowledgeDeepTaskReportExportRequest,
) -> KnowledgeDeepTaskReportExportResponse:
    """客户确认后导出完整 K4 任务的 Markdown 报告，不重新运行模型或检索。"""

    try:
        return await asyncio.to_thread(export_knowledge_deep_task_report, task_id=task_id, request=request)
    except KnowledgeDeepTaskReportNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (KnowledgeDeepTaskReportConfirmationError, KnowledgeDeepTaskReportExportError) as exc:
        # 未确认、部分结果、同名冲突和受控写入失败都不会产生文件；客户可按详情提示调整后重试。
        conflict_statuses = (KnowledgeDeepTaskReportNotReadyError, KnowledgeDeepTaskReportConflictError)
        raise HTTPException(status_code=409 if isinstance(exc, conflict_statuses) else 400, detail=str(exc)) from exc


@router.post("/deep-tasks/{task_id}/pause", response_model=KnowledgeDeepTaskControlResponse)
async def pause_knowledge_deep_task_endpoint(task_id: str) -> KnowledgeDeepTaskControlResponse:
    """请求 K4 在当前模型回合完成后暂停，不强杀 Provider 请求或丢失检查点。"""

    response = await asyncio.to_thread(request_knowledge_deep_task_pause, task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Knowledge deep task '{task_id}' was not found.")
    if response.accepted:
        event = "task_paused" if response.status == "paused" else "task_pause_requested"
        await publish_live_task_event(
            task_id=task_id,
            event=event,
            agent_id="knowledge_agent",
            level="warning",
            message=response.message,
        )
    return response


@router.post("/deep-tasks/{task_id}/cancel", response_model=KnowledgeDeepTaskControlResponse)
async def cancel_knowledge_deep_task_endpoint(task_id: str) -> KnowledgeDeepTaskControlResponse:
    """请求 K4 停止后续节点；运行中任务只在模型安全返回后正式取消。"""

    response = await asyncio.to_thread(request_knowledge_deep_task_cancel, task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Knowledge deep task '{task_id}' was not found.")
    if response.accepted:
        event = "task_cancelled" if response.status == "cancelled" else "task_cancel_requested"
        await publish_live_task_event(
            task_id=task_id,
            event=event,
            agent_id="knowledge_agent",
            level="warning",
            message=response.message,
        )
    return response


@router.post(
    "/deep-tasks/{task_id}/resume",
    response_model=KnowledgeDeepTaskControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_knowledge_deep_task_endpoint(task_id: str) -> KnowledgeDeepTaskControlResponse:
    """继续同一条 K4 检查点链，不新建任务也不重跑已完成章节。"""

    resumed = await asyncio.to_thread(resume_knowledge_deep_task, task_id)
    if resumed is None:
        raise HTTPException(status_code=404, detail=f"Knowledge deep task '{task_id}' was not found.")
    response, scope = resumed
    if not response.accepted:
        return response
    if task_id in _BACKGROUND_KNOWLEDGE_DEEP_TASK_IDS:
        # 连续点击“继续”不能启动第二条协程，更不能让重复协程先结束实时事件流。原 task 已持有
        # 同一 checkpoint 和队列槽位，客户只需等待当前运行态刷新。
        return response.model_copy(
            update={"message": "深度任务已在本机运行队列或执行中，完成章节不会重复调用模型。"}
        )

    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_resumed",
        agent_id="knowledge_agent",
        message="深度任务正在从已保存检查点继续；完成章节不会重复调用模型。",
    )
    task = asyncio.create_task(_run_knowledge_deep_task_background(task_id, scope))
    _track_knowledge_deep_task(task, task_id=task_id)
    return response


def _track_knowledge_deep_task(task: asyncio.Task[None], *, task_id: str) -> None:
    """统一持有后台协程，防止未完成 K4 任务因缺少引用被事件循环过早回收。"""

    if task_id in _BACKGROUND_KNOWLEDGE_DEEP_TASK_IDS:
        task.cancel()
        return
    _BACKGROUND_KNOWLEDGE_DEEP_TASKS.add(task)
    _BACKGROUND_KNOWLEDGE_DEEP_TASK_IDS.add(task_id)

    def clear_finished_task(finished_task: asyncio.Task[None]) -> None:
        _BACKGROUND_KNOWLEDGE_DEEP_TASKS.discard(finished_task)
        _BACKGROUND_KNOWLEDGE_DEEP_TASK_IDS.discard(task_id)

    task.add_done_callback(clear_finished_task)


async def _run_index_job_background(index_job_id: str) -> None:
    """索引失败已由服务层持久化；此处只保证后台异常不会遗留未观察协程。"""

    reservation = await knowledge_runtime_queue.reserve(work_id=index_job_id, work_kind="index")
    if reservation is None:
        return
    try:
        await asyncio.to_thread(run_knowledge_index_job, index_job_id)
    except Exception:
        # 任务服务会在可预期异常中写入失败事实；此兜底避免 FastAPI 事件循环因未知异常输出
        # 未处理 task 警告。客户可通过 job 状态读取下一步，而不是从日志猜测。
        return
    finally:
        await reservation.release()


async def _run_knowledge_deletion_background(knowledge_base_id: str) -> None:
    """有限等待取消中的索引任务；未完成时保留 deleting 供启动恢复继续处理。"""

    # 单份受控资料解析最长可达数秒。后台最多等待十秒，既不给 Qt 主线程制造阻塞，也不把
    # 长时间文件占用误判成删除失败；超过后下一次应用启动会继续按相同边界收束。
    for _ in range(100):
        try:
            await asyncio.to_thread(finalize_knowledge_base_deletion, knowledge_base_id)
            return
        except KnowledgeBaseDeletionPendingError:
            await asyncio.sleep(0.1)
        except (KnowledgeBaseNotFoundError, KnowledgeBaseUnavailableError):
            return
        except Exception:
            # 服务层会保留 deleting 和脱敏审计，未知异常不应让事件循环泄露未观察 task。
            return


async def _run_knowledge_answer_background(task_id: str, request: KnowledgeAnswerRequest) -> None:
    """桥接 K3 Runtime 与实时事件流；任务服务已负责把终态写入 SQLite。"""

    async def publish_stage(
        event: str,
        message: str,
        step_id: str | None,
        level: str,
    ) -> None:
        await publish_live_task_event(
            task_id=task_id,
            event=event,
            agent_id="knowledge_agent",
            step_id=step_id,
            level=level,  # type: ignore[arg-type]
            message=message,
        )

    try:
        await run_knowledge_answer_task(
            task_id=task_id,
            request=request,
            progress_callback=publish_stage,
        )
    except Exception:  # pragma: no cover - 服务层已写入受控失败，此处只防止协程泄露。
        logger.exception("Knowledge answer task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="knowledge_agent",
            level="error",
            message="知识库问答异常结束，请在任务历史中查看记录后重试。",
        )
    finally:
        await finish_live_task_event_stream(task_id)


async def _run_knowledge_deep_task_background(task_id: str, scope: KnowledgeDeepTaskScope) -> None:
    """桥接 K4 checkpoint Runtime 与实时阶段流；持久化仍由深度任务服务负责。"""

    async def publish_stage(
        event: str,
        message: str,
        step_id: str | None,
        level: str,
    ) -> None:
        await publish_live_task_event(
            task_id=task_id,
            event=event,
            agent_id="knowledge_agent",
            step_id=step_id,
            level=level,  # type: ignore[arg-type]
            message=message,
        )

    async def publish_queue_waiting(ahead_count: int) -> None:
        await publish_stage(
            "knowledge_runtime_queue_waiting",
            f"正在等待本机深度任务队列，前方还有 {ahead_count} 项同类任务；已保存的检查点不会丢失。",
            None,
            "info",
        )

    reservation = await knowledge_runtime_queue.reserve(
        work_id=task_id,
        work_kind="deep_task",
        on_waiting=publish_queue_waiting,
    )
    if reservation is None:
        return
    execution_started_at = monotonic()
    try:
        await publish_stage(
            "knowledge_runtime_queue_started",
            "已获得本机深度任务运行槽位，正在从当前检查点执行。",
            None,
            "info",
        )
        await run_knowledge_deep_task(task_id=task_id, scope=scope, progress_callback=publish_stage)
    except Exception:  # pragma: no cover - 此处仅处理服务层边界外的未预期异常。
        logger.exception("Knowledge deep task ended unexpectedly: %s", task_id)
        # 深度任务的 Map/Reduce 检查点由服务层落入 SQLite。后台协程若在其边界外异常，仍要
        # 明确收束为失败终态，避免客户刷新任务历史时看到一个永远停在“运行中”的任务。
        await asyncio.to_thread(mark_knowledge_deep_task_unexpected_failure, task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="knowledge_agent",
            level="error",
            message="知识库深度任务异常结束，请在任务历史中查看记录后重试。",
        )
    finally:
        record_knowledge_deep_task_elapsed_ms(round((monotonic() - execution_started_at) * 1_000))
        await reservation.release()
        await finish_live_task_event_stream(task_id)
