"""受控 PDF 整理 Tool 的执行、验证与任务审计服务。

该模块刻意不调用 LLM：合并、提取、旋转和删除页面都是可确定完成的本地文件操作。文档
助手拥有用户入口和任务表达，Tool 只接受已导入 workspace 的 PDF，并只在固定输出目录
生成新文件；原文件从不被覆盖或删除。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from app.core.config import settings
from app.database.task_repository import (
    list_workflow_artifacts,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.events import TaskLogEvent
from app.schemas.pdf_processing import (
    PdfProcessingOperation,
    PdfProcessingStartRequest,
    PdfProcessingTaskResultResponse,
    PdfProcessingVerification,
)
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.task_event_stream import publish_live_task_event
from app.services.workspace_documents import WorkspaceDocumentError, resolve_workspace_document_path

try:
    import fitz
except ImportError:  # pragma: no cover - requirements 环境始终安装 PyMuPDF。
    fitz = None


PDF_PROCESSING_AGENT_ID = "document_agent"
PDF_PROCESSING_STEP_ID = "pdf_processing"
PDF_PROCESSING_TOOL_NAME = "document.pdf_process"
PDF_PROCESSING_ALLOWED_SUFFIXES = {".pdf"}
PDF_PROCESSING_MAX_INPUT_BYTES = 50 * 1024 * 1024
PDF_PROCESSING_MAX_TOTAL_PAGES = 1_000
PDF_PROCESSING_MAX_OUTPUT_BYTES = 100 * 1024 * 1024


class PdfProcessingError(ValueError):
    """可直接展示给客户的 PDF 处理失败原因。"""


@dataclass(frozen=True)
class _PdfProcessingOutput:
    output_path: Path
    output_name: str
    expected_page_count: int
    actual_page_count: int
    output_size_bytes: int
    source_names: tuple[str, ...]


def create_pdf_processing_queued_run(
    *,
    task_id: str,
    request: PdfProcessingStartRequest,
) -> WorkflowRun:
    """在后台开始前持久化任务，让历史页立即看到真实的待执行任务。"""

    now = _now()
    operation_name = _operation_label(request.operation)
    step = WorkflowStepRun(
        step_id=PDF_PROCESSING_STEP_ID,
        agent=PDF_PROCESSING_AGENT_ID,
        action=PDF_PROCESSING_TOOL_NAME,
        status="pending",
        message=f"已受理 PDF {operation_name}任务，等待本地处理。",
        output={
            "operation": request.operation,
            "document_refs": request.document_refs,
            "page_range": request.page_range,
            "rotation_degrees": request.rotation_degrees,
            "write_scope": "output/document_processing",
            "original_files_unchanged": True,
        },
    )
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary=f"PDF {operation_name}任务已受理，尚未写入输出文件。",
        steps=[step],
        limits=RuntimeExecutionLimits(
            max_steps=1,
            max_tool_calls=1,
            max_retries_per_tool=0,
            tool_timeout_ms=30_000,
            task_timeout_ms=60_000,
        ),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[
            _event(
                task_id,
                1,
                "task_queued",
                f"PDF {operation_name}任务已受理，将只生成新的受控副本。",
            )
        ],
        plan=None,
        artifacts=[],
        # 现有 Tool 审计只接受“已开始/已结束”等事实状态；排队期间由 WorkflowStepRun 表达，
        # 等 Tool 真正完成或失败后再写一条完整记录，避免把尚未发生的调用伪装成审计事实。
        tool_calls=[],
    )
    return run


async def run_pdf_processing_task(
    *,
    task_id: str,
    request: PdfProcessingStartRequest,
) -> PdfProcessingTaskResultResponse:
    """在异步 API 生命周期中执行一个确定性 PDF Tool，并写回标准任务记录。"""

    started_at = _now()
    started_clock = perf_counter()
    operation_name = _operation_label(request.operation)
    events = [
        _event(task_id, 1, "task_queued", f"PDF {operation_name}任务已受理，将只生成新的受控副本。"),
        _event(task_id, 2, "task_started", "正在校验已选择的 PDF 和页码范围。", step_id=PDF_PROCESSING_STEP_ID),
        _event(task_id, 3, "tool_started", f"正在执行 PDF {operation_name}，原文件不会被修改。", step_id=PDF_PROCESSING_STEP_ID),
    ]
    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=PDF_PROCESSING_AGENT_ID,
        step_id=PDF_PROCESSING_STEP_ID,
        message="正在校验已选择的 PDF 和页码范围。",
    )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=PDF_PROCESSING_AGENT_ID,
        step_id=PDF_PROCESSING_STEP_ID,
        message=f"正在执行 PDF {operation_name}，原文件不会被修改。",
    )

    try:
        output = await asyncio.to_thread(_process_pdf, request, task_id)
        verification = PdfProcessingVerification(
            output_opened=True,
            expected_page_count=output.expected_page_count,
            actual_page_count=output.actual_page_count,
            output_size_bytes=output.output_size_bytes,
        )
        artifact = _artifact_for_output(task_id, request.operation, output, verification)
        duration_ms = _duration_ms(started_clock)
        events.extend(
            [
                _event(
                    task_id,
                    4,
                    "artifact_saved",
                    f"已生成 {output.output_name}，正在验证页数与可打开性。",
                    step_id=PDF_PROCESSING_STEP_ID,
                ),
                _event(
                    task_id,
                    5,
                    "task_completed",
                    f"PDF {operation_name}完成：{output.actual_page_count} 页，已通过文件验证。",
                    step_id=PDF_PROCESSING_STEP_ID,
                ),
            ]
        )
        run = _final_run(
            task_id=task_id,
            request=request,
            status="completed",
            summary=f"PDF {operation_name}完成，已生成并验证新文件。",
            message=f"已生成 {output.output_name}，原文件未被修改。",
            started_at=started_at,
            duration_ms=duration_ms,
            output=output,
            verification=verification,
        )
        save_workflow_run(
            run=run,
            events=events,
            plan=None,
            artifacts=[artifact],
            tool_calls=[_tool_call(task_id, request, status="completed", duration_ms=duration_ms, output=output, verification=verification)],
        )
        await publish_live_task_event(
            task_id=task_id,
            event="artifact_saved",
            agent_id=PDF_PROCESSING_AGENT_ID,
            step_id=PDF_PROCESSING_STEP_ID,
            message=f"已生成 {output.output_name}，页数与可打开性已验证。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id=PDF_PROCESSING_AGENT_ID,
            step_id=PDF_PROCESSING_STEP_ID,
            message=f"PDF {operation_name}完成，原文件未被修改。",
        )
        return PdfProcessingTaskResultResponse(
            task_id=task_id,
            status="completed",
            operation=request.operation,
            summary=run.summary,
            message=f"已生成 {output.output_name}，原文件未被修改。",
            artifact=artifact,
            verification=verification,
        )
    except (PdfProcessingError, WorkspaceDocumentError) as exc:
        duration_ms = _duration_ms(started_clock)
        message = str(exc)
        events.append(
            _event(task_id, 4, "task_failed", message, step_id=PDF_PROCESSING_STEP_ID, level="error")
        )
        run = _failed_run(task_id, request, started_at, duration_ms, message)
        save_workflow_run(
            run=run,
            events=events,
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(task_id, request, status="failed", duration_ms=duration_ms, error=message)],
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id=PDF_PROCESSING_AGENT_ID,
            step_id=PDF_PROCESSING_STEP_ID,
            level="error",
            message=message,
        )
        return PdfProcessingTaskResultResponse(
            task_id=task_id,
            status="failed",
            operation=request.operation,
            summary="PDF 整理未完成，未保留不完整输出文件。",
            message=message,
        )
    except Exception:  # pragma: no cover - 防止后台任务静默退出，细节写日志而不回显内部路径。
        duration_ms = _duration_ms(started_clock)
        message = "PDF 处理发生未预期错误，未保留不完整输出文件。"
        events.append(
            _event(task_id, 4, "task_failed", message, step_id=PDF_PROCESSING_STEP_ID, level="error")
        )
        run = _failed_run(task_id, request, started_at, duration_ms, message)
        save_workflow_run(
            run=run,
            events=events,
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(task_id, request, status="failed", duration_ms=duration_ms, error=message)],
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_failed",
            agent_id=PDF_PROCESSING_AGENT_ID,
            step_id=PDF_PROCESSING_STEP_ID,
            level="error",
            message=message,
        )
        return PdfProcessingTaskResultResponse(
            task_id=task_id,
            status="failed",
            operation=request.operation,
            summary="PDF 整理未完成，未保留不完整输出文件。",
            message=message,
        )


def get_pdf_processing_task_result(task_id: str) -> PdfProcessingTaskResultResponse | None:
    """从标准任务与 artifact 记录恢复 PDF Tool 的终态，不依赖进程内缓存。"""

    run = load_workflow_run(task_id)
    if run is None:
        return None
    step = next((item for item in run.steps if item.step_id == PDF_PROCESSING_STEP_ID), None)
    if step is None or step.action != PDF_PROCESSING_TOOL_NAME:
        return None
    output = step.output
    operation = output.get("operation")
    artifact = next(iter(list_workflow_artifacts(task_id)), None)
    verification_payload = output.get("verification")
    verification = None
    if isinstance(verification_payload, dict):
        verification = PdfProcessingVerification.model_validate(verification_payload)
    return PdfProcessingTaskResultResponse(
        task_id=task_id,
        status=run.status,
        operation=operation if operation in {"merge", "extract", "rotate", "delete"} else None,
        summary=run.summary,
        message=str(output.get("message", step.message)),
        artifact=artifact,
        verification=verification,
    )


def _process_pdf(request: PdfProcessingStartRequest, task_id: str) -> _PdfProcessingOutput:
    if fitz is None:
        raise PdfProcessingError("PDF 处理组件未安装，请联系管理员修复本地运行环境。")

    source_paths = [
        resolve_workspace_document_path(item, allowed_suffixes=PDF_PROCESSING_ALLOWED_SUFFIXES)
        for item in request.document_refs
    ]
    total_input_bytes = sum(path.stat().st_size for path in source_paths)
    if total_input_bytes > PDF_PROCESSING_MAX_INPUT_BYTES:
        raise PdfProcessingError("本次选择的 PDF 总大小超过 50 MB，请分批处理。")

    output_path = _new_output_path(request.operation, task_id)
    try:
        if request.operation == "merge":
            expected_page_count = _merge_pdfs(source_paths, output_path)
        else:
            source_path = source_paths[0]
            expected_page_count = _transform_single_pdf(
                source_path,
                output_path,
                operation=request.operation,
                page_range=request.page_range,
                rotation_degrees=request.rotation_degrees,
            )
        actual_page_count, output_size = _verify_output(output_path, expected_page_count)
    except PdfProcessingError:
        _remove_partial_output(output_path)
        raise
    except Exception as exc:
        _remove_partial_output(output_path)
        raise PdfProcessingError("无法处理所选 PDF；请确认文件未损坏、未加密且页码范围有效。") from exc

    return _PdfProcessingOutput(
        output_path=output_path,
        output_name=output_path.name,
        expected_page_count=expected_page_count,
        actual_page_count=actual_page_count,
        output_size_bytes=output_size,
        source_names=tuple(path.name for path in source_paths),
    )


def _merge_pdfs(source_paths: list[Path], output_path: Path) -> int:
    result = fitz.open()
    expected_page_count = 0
    try:
        for source_path in source_paths:
            source = fitz.open(str(source_path))
            try:
                _ensure_unencrypted_pdf(source, source_path.name)
                expected_page_count += source.page_count
                _ensure_page_budget(expected_page_count)
                result.insert_pdf(source)
            finally:
                source.close()
        if expected_page_count < 1:
            raise PdfProcessingError("选择的 PDF 没有可处理的页面。")
        result.save(str(output_path), garbage=4, deflate=True)
    finally:
        result.close()
    return expected_page_count


def _transform_single_pdf(
    source_path: Path,
    output_path: Path,
    *,
    operation: PdfProcessingOperation,
    page_range: str,
    rotation_degrees: int,
) -> int:
    source = fitz.open(str(source_path))
    try:
        _ensure_unencrypted_pdf(source, source_path.name)
        _ensure_page_budget(source.page_count)
        selected_pages = _parse_page_range(page_range, source.page_count)
        if operation == "extract":
            result = fitz.open()
            try:
                for page_index in selected_pages:
                    result.insert_pdf(source, from_page=page_index, to_page=page_index)
                result.save(str(output_path), garbage=4, deflate=True)
            finally:
                result.close()
            return len(selected_pages)
        if operation == "rotate":
            for page_index in selected_pages:
                page = source.load_page(page_index)
                page.set_rotation((page.rotation + rotation_degrees) % 360)
            source.save(str(output_path), garbage=4, deflate=True)
            return source.page_count
        if operation == "delete":
            if len(selected_pages) >= source.page_count:
                raise PdfProcessingError("不能删除 PDF 的全部页面。")
            for page_index in reversed(selected_pages):
                source.delete_page(page_index)
            source.save(str(output_path), garbage=4, deflate=True)
            return source.page_count
    finally:
        source.close()
    raise PdfProcessingError("不支持的 PDF 操作。")


def _parse_page_range(raw_value: str, page_count: int) -> list[int]:
    """把 1-based 的客户输入转为稳定、去重且保持顺序的页索引。"""

    normalized = raw_value.replace("，", ",").replace("－", "-").strip()
    if not normalized:
        raise PdfProcessingError("请填写页码范围，例如 1-3,5。")
    selected: list[int] = []
    for token in normalized.split(","):
        part = token.strip()
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if match is None:
            raise PdfProcessingError("页码范围格式不正确，请使用 1-3,5 这样的写法。")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > page_count:
            raise PdfProcessingError(f"页码范围超出文件页数（共 {page_count} 页）。")
        for one_based_page in range(start, end + 1):
            page_index = one_based_page - 1
            if page_index not in selected:
                selected.append(page_index)
    if not selected:
        raise PdfProcessingError("没有可处理的页面。")
    return selected


def _ensure_unencrypted_pdf(document: Any, source_name: str) -> None:
    if document.needs_pass:
        raise PdfProcessingError(f"{source_name} 已加密，当前版本不处理受密码保护的 PDF。")


def _ensure_page_budget(page_count: int) -> None:
    if page_count < 1:
        raise PdfProcessingError("选择的 PDF 没有可处理的页面。")
    if page_count > PDF_PROCESSING_MAX_TOTAL_PAGES:
        raise PdfProcessingError("本次 PDF 页数超过 1000 页，请分批处理。")


def _verify_output(output_path: Path, expected_page_count: int) -> tuple[int, int]:
    if not output_path.is_file() or output_path.stat().st_size < 1:
        raise PdfProcessingError("PDF 输出文件为空，未保留该结果。")
    output_size = output_path.stat().st_size
    if output_size > PDF_PROCESSING_MAX_OUTPUT_BYTES:
        raise PdfProcessingError("生成的 PDF 超过 100 MB，未保留该结果。")
    output = fitz.open(str(output_path))
    try:
        if output.needs_pass:
            raise PdfProcessingError("生成的 PDF 无法直接打开，未保留该结果。")
        actual_page_count = output.page_count
        if actual_page_count != expected_page_count:
            raise PdfProcessingError("生成 PDF 的页数与预期不一致，未保留该结果。")
    finally:
        output.close()
    return actual_page_count, output_size


def _new_output_path(operation: PdfProcessingOperation, task_id: str) -> Path:
    output_dir = settings.document_processing_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = task_id.rsplit("_", maxsplit=1)[-1]
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return output_dir / f"pdf_{operation}_{timestamp}_{suffix}.pdf"


def _remove_partial_output(output_path: Path) -> None:
    """只清理当前任务刚创建的已知输出，绝不触碰输入或输出目录中的其他文件。"""

    if output_path.is_file():
        output_path.unlink(missing_ok=True)


def _artifact_for_output(
    task_id: str,
    operation: PdfProcessingOperation,
    output: _PdfProcessingOutput,
    verification: PdfProcessingVerification,
) -> WorkflowArtifact:
    return WorkflowArtifact(
        artifact_id=f"artifact_pdf_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=PDF_PROCESSING_STEP_ID,
        agent_id=PDF_PROCESSING_AGENT_ID,
        kind="file",
        name=output.output_name,
        summary=f"PDF {_operation_label(operation)}完成，{output.actual_page_count} 页，已验证可打开。",
        uri=f"agentflow-output://document_processing/{output.output_name}",
        mime_type="application/pdf",
        metadata={
            "runtime": True,
            "output_scope": "document_processing",
            "output_path": str(output.output_path),
            "operation": operation,
            "source_documents": list(output.source_names),
            "page_count": output.actual_page_count,
            "verification": verification.model_dump(),
            "original_files_unchanged": True,
        },
        created_at=_now(),
    )


def _final_run(
    *,
    task_id: str,
    request: PdfProcessingStartRequest,
    status: str,
    summary: str,
    message: str,
    started_at: str,
    duration_ms: int,
    output: _PdfProcessingOutput,
    verification: PdfProcessingVerification,
) -> WorkflowRun:
    step = WorkflowStepRun(
        step_id=PDF_PROCESSING_STEP_ID,
        agent=PDF_PROCESSING_AGENT_ID,
        action=PDF_PROCESSING_TOOL_NAME,
        status="completed",
        message=message,
        output={
            "operation": request.operation,
            "document_refs": request.document_refs,
            "page_range": request.page_range,
            "rotation_degrees": request.rotation_degrees,
            "output_name": output.output_name,
            "page_count": output.actual_page_count,
            "verification": verification.model_dump(),
            "write_scope": "output/document_processing",
            "original_files_unchanged": True,
            "message": message,
        },
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,
        summary=summary,
        steps=[step],
        limits=RuntimeExecutionLimits(
            max_steps=1,
            max_tool_calls=1,
            max_retries_per_tool=0,
            tool_timeout_ms=30_000,
            task_timeout_ms=60_000,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            step_total=1,
            step_completed=1,
            tool_call_total=1,
        ),
    )


def _failed_run(
    task_id: str,
    request: PdfProcessingStartRequest,
    started_at: str,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    step = WorkflowStepRun(
        step_id=PDF_PROCESSING_STEP_ID,
        agent=PDF_PROCESSING_AGENT_ID,
        action=PDF_PROCESSING_TOOL_NAME,
        status="failed",
        message=message,
        output={
            "operation": request.operation,
            "document_refs": request.document_refs,
            "page_range": request.page_range,
            "rotation_degrees": request.rotation_degrees,
            "write_scope": "output/document_processing",
            "original_files_unchanged": True,
            "message": message,
        },
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="PDF 整理未完成，原文件未被修改。",
        steps=[step],
        limits=RuntimeExecutionLimits(
            max_steps=1,
            max_tool_calls=1,
            max_retries_per_tool=0,
            tool_timeout_ms=30_000,
            task_timeout_ms=60_000,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            step_total=1,
            step_failed=1,
            tool_call_total=1,
            tool_call_failed=1,
        ),
    )


def _tool_call(
    task_id: str,
    request: PdfProcessingStartRequest,
    *,
    status: str,
    duration_ms: int = 0,
    output: _PdfProcessingOutput | None = None,
    verification: PdfProcessingVerification | None = None,
    error: str = "",
) -> WorkflowToolCall:
    result: dict[str, Any] = {}
    if output is not None and verification is not None:
        result = {
            "output_name": output.output_name,
            "page_count": output.actual_page_count,
            "output_size_bytes": output.output_size_bytes,
            "verification": verification.model_dump(),
        }
    return WorkflowToolCall(
        call_id=f"call_pdf_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=PDF_PROCESSING_STEP_ID,
        agent_id=PDF_PROCESSING_AGENT_ID,
        tool_name=PDF_PROCESSING_TOOL_NAME,
        status=status,
        risk_level="low",
        # 用户在聚焦工作区明确选择输入、操作并点击生成；写入范围固定在 output，不覆盖原件。
        permission_required=False,
        max_attempts=1,
        timeout_ms=30_000,
        duration_ms=duration_ms,
        request={
            "operation": request.operation,
            "document_refs": request.document_refs,
            "page_range": request.page_range,
            "rotation_degrees": request.rotation_degrees,
            "write_scope": "output/document_processing",
            "user_initiated": True,
            "original_files_unchanged": True,
        },
        result=result,
        error=error,
        started_at="",
        finished_at=_now() if status in {"completed", "failed"} else "",
    )


def _event(
    task_id: str,
    sequence: int,
    event: str,
    message: str,
    *,
    step_id: str | None = None,
    level: str = "info",
) -> TaskLogEvent:
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event,
        agent_id=PDF_PROCESSING_AGENT_ID,
        step_id=step_id,
        level=level,
        message=message,
    )


def _operation_label(operation: PdfProcessingOperation) -> str:
    return {
        "merge": "合并",
        "extract": "提取页面",
        "rotate": "旋转页面",
        "delete": "删除页面",
    }[operation]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))
