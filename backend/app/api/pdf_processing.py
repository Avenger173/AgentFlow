"""文档助手 PDF 整理 Tool 的异步 API。"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.schemas.pdf_processing import (
    PdfProcessingStartRequest,
    PdfProcessingTaskResultResponse,
    PdfProcessingTaskStartResponse,
)
from app.services.pdf_processing import (
    create_pdf_processing_queued_run,
    get_pdf_processing_task_result,
    run_pdf_processing_task,
)
from app.services.task_event_stream import (
    finish_live_task_event_stream,
    has_live_task_event_stream,
    live_task_event_stream_finished,
    open_live_task_event_stream,
    publish_live_task_event,
)
from fastapi import APIRouter, HTTPException, status


router = APIRouter(prefix="/api/agents/document_agent/pdf-tools", tags=["document-agent-pdf-tools"])
logger = logging.getLogger(__name__)
_BACKGROUND_PDF_TASKS: set[asyncio.Task[None]] = set()


@router.post("/start", response_model=PdfProcessingTaskStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_pdf_processing(request: PdfProcessingStartRequest) -> PdfProcessingTaskStartResponse:
    """立即受理 PDF 任务，通过 WebSocket 推送真实阶段，避免阻塞 Qt 主线程。"""

    task_id = f"task_pdf_{uuid4().hex[:12]}"
    create_pdf_processing_queued_run(task_id=task_id, request=request)
    open_live_task_event_stream(task_id)
    await publish_live_task_event(
        task_id=task_id,
        event="task_queued",
        agent_id="document_agent",
        message="PDF 整理任务已受理，将在受控目录生成新副本。",
    )
    task = asyncio.create_task(_run_pdf_processing_background(task_id, request))
    _BACKGROUND_PDF_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_PDF_TASKS.discard)
    return PdfProcessingTaskStartResponse(task_id=task_id)


@router.get("/{task_id}/result", response_model=PdfProcessingTaskResultResponse)
async def get_pdf_processing_result(task_id: str) -> PdfProcessingTaskResultResponse:
    """读取 PDF Tool 终态；服务重启后仍可由 SQLite 恢复完成结果。"""

    result = get_pdf_processing_task_result(task_id)
    if result is not None:
        return result
    if has_live_task_event_stream(task_id) and not live_task_event_stream_finished(task_id):
        return PdfProcessingTaskResultResponse(task_id=task_id, status="running", message="PDF 整理任务正在执行。")
    raise HTTPException(status_code=404, detail=f"PDF task '{task_id}' was not found.")


async def _run_pdf_processing_background(task_id: str, request: PdfProcessingStartRequest) -> None:
    """后台任务必须无论成功或失败都结束实时事件流，避免 Qt 无限等待。"""

    try:
        await run_pdf_processing_task(task_id=task_id, request=request)
    except Exception:  # pragma: no cover - 服务层已尽量把错误转为任务终态。
        logger.exception("PDF processing task ended unexpectedly: %s", task_id)
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id="document_agent",
            level="error",
            message="PDF 整理任务异常结束，请在历史任务中查看记录。",
        )
    finally:
        await finish_live_task_event_stream(task_id)
