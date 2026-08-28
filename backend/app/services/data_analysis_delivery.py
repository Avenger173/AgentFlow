"""数据工作台 D4 的工作簿交付任务生命周期。

数据计算与 Excel 渲染仍由 ``data_analysis``、``data_workbook`` 负责；本模块只将用户已经
明确确认的导出操作接入通用 Runtime 任务、实时阶段事件、工具审计和 artifact 历史。这样
不会为了“有任务历史”复制一套数据处理算法，也不会让源表、模型或网络进入这条路径。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from threading import Event, RLock
from typing import Any

from app.core.config import settings
from app.database.task_repository import (
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.data_agent import (
    DataWorkbookArtifact,
    DataWorkbookExportRequest,
    DataWorkbookExportResponse,
    DataWorkbookTaskResultResponse,
    DataWorkbookVerification,
)
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    TaskControlResponse,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.data_workbook import DataWorkbookError, export_data_analysis_workbook
from app.services.task_event_stream import publish_live_task_event
from app.workflow.state_machine import can_cancel


DATA_ANALYSIS_AGENT_ID = "data_agent"
DATA_WORKBOOK_EXPORT_STEP_ID = "data_workbook_export"
DATA_WORKBOOK_EXPORT_TOOL_NAME = "data.render_workbook"
_TASK_TIMEOUT_MS = 150_000
_TOOL_TIMEOUT_MS = 120_000

# 数据工作簿渲染依赖同步的 pandas/openpyxl 调用，不能在线程中安全地强杀。这里仅保存同进程
# 的协作式取消标记：取消请求会先落库，后台线程在安全提交点检查标记、清理新文件且拒绝登记
# artifact。SQLite 仍是客户可见状态的唯一持久来源。
_DATA_TASK_LOCK = RLock()
_DATA_TASK_CANCEL_EVENTS: dict[str, Event] = {}


def create_data_workbook_queued_run(
    *,
    task_id: str,
    request: DataWorkbookExportRequest,
) -> WorkflowRun:
    """在后台开始前写入待处理记录，使任务历史不再是导出后的补记。"""

    now = _now()
    with _DATA_TASK_LOCK:
        _DATA_TASK_CANCEL_EVENTS[task_id] = Event()
    step = WorkflowStepRun(
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_WORKBOOK_EXPORT_TOOL_NAME,
        status="pending",
        message="已受理数据工作簿导出，等待本地生成。",
        output=_base_output(request),
    )
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="数据工作簿导出已受理，尚未写入输出文件。",
        steps=[step],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[
            _event(
                task_id,
                1,
                "task_queued",
                "数据工作簿导出已受理，将只生成新的受控 Excel 文件。",
            )
        ],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_data_workbook_export_task(
    *,
    task_id: str,
    request: DataWorkbookExportRequest,
) -> DataWorkbookTaskResultResponse:
    """在后台执行 D3 导出，并把真实阶段和最终 artifact 写入通用任务历史。"""

    if _is_cancel_requested(task_id):
        # 取消可能发生在 create_task() 调度到本协程之前。此时不应该再把已取消任务推进到
        # running，更不能留下仅进程内使用的协作取消标记。
        try:
            return _cancelled_task_result(task_id)
        finally:
            _forget_data_task_cancel_state(task_id)

    started_at = _now()
    started_clock = perf_counter()
    running_events = [
        _event(task_id, 1, "task_queued", "数据工作簿导出已受理，将只生成新的受控 Excel 文件。"),
        _event(
            task_id,
            2,
            "task_started",
            "正在复核数据版本并准备受控分析副本。",
            step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        ),
        _event(
            task_id,
            3,
            "tool_started",
            "正在写入并回读验证可编辑 Excel；原始数据不会被修改。",
            step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        ),
    ]
    # 启动写库与取消写库必须串行：否则取消刚把任务稳定为 cancelled，后台又可能把它覆写回
    # running，导致 UI 和审计轨迹出现相互矛盾的状态。
    with _DATA_TASK_LOCK:
        if _is_cancel_requested(task_id):
            try:
                return _cancelled_task_result(task_id)
            finally:
                _forget_data_task_cancel_state(task_id)
        _save_running_run(task_id=task_id, request=request, started_at=started_at, events=running_events)
    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        message="正在复核数据版本并准备受控分析副本。",
    )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        message="正在写入并回读验证可编辑 Excel；原始数据不会被修改。",
    )

    try:
        result = await asyncio.to_thread(export_data_analysis_workbook, request)
        with _DATA_TASK_LOCK:
            if _is_cancel_requested(task_id):
                # 写入线程可能在收到取消前刚好通过回读。此时交付尚未进入 artifact 历史，
                # 可以安全删除该新文件，避免用户看到“已取消”却在输出目录多出成品。
                _remove_cancelled_output(result)
                return _cancelled_task_result(task_id)

            output_path = _resolve_created_output_path(result)
            duration_ms = _duration_ms(started_clock)
            artifact = _artifact_for_result(task_id, request, result, output_path)
            message = f"已生成 {result.artifact.name}，原生表格、图表与关键指标已回读验证。"
            completed_events = [
                *running_events,
                _event(
                    task_id,
                    4,
                    "artifact_saved",
                    f"已生成 {result.artifact.name}，正在登记经过验证的交付物。",
                    step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
                ),
                _event(
                    task_id,
                    5,
                    "task_completed",
                    "数据工作簿已完成并通过回读验证，源文件未被修改。",
                    step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
                ),
            ]
            run = _completed_run(
                task_id=task_id,
                request=request,
                result=result,
                started_at=started_at,
                duration_ms=duration_ms,
                message=message,
            )
            save_workflow_run(
                run=run,
                events=completed_events,
                plan=None,
                artifacts=[artifact],
                tool_calls=[
                    _tool_call(
                        task_id,
                        request,
                        status="completed",
                        duration_ms=duration_ms,
                        result=result,
                    )
                ],
            )
        await publish_live_task_event(
            task_id=task_id,
            event="artifact_saved",
            agent_id=DATA_ANALYSIS_AGENT_ID,
            step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
            message=f"已生成 {result.artifact.name}，原生对象与指标已回读验证。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id=DATA_ANALYSIS_AGENT_ID,
            step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
            message="数据工作簿已完成，源文件未被修改。",
        )
        return DataWorkbookTaskResultResponse(
            task_id=task_id,
            status="completed",
            summary=run.summary,
            message=message,
            artifact=result.artifact,
            verification=result.verification,
            warnings=result.warnings,
            skipped_items=result.skipped_items,
        )
    except DataWorkbookError as exc:
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        return await _persist_failed_task(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message=str(exc),
            events=running_events,
        )
    except Exception:  # pragma: no cover - 后台任务绝不能因未知异常而没有终态。
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        return await _persist_failed_task(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message="数据工作簿导出发生未预期错误，未保留不完整输出文件。",
            events=running_events,
        )
    finally:
        _forget_data_task_cancel_state(task_id)


async def cancel_data_workbook_export_task(task_id: str) -> TaskControlResponse | None:
    """取消当前进程中的数据工作簿导出，并立刻写入可恢复的终态审计。

    取消不是对 pandas/openpyxl 线程的非安全中断。若渲染已经进入同步区，任务状态会先变为
    ``cancelled``，线程返回时再清理尚未登记的文件；若已先完成正式登记，则本函数只返回
    ``accepted=false``，不篡改既成事实。
    """

    with _DATA_TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_data_workbook_run(run):
            return None
        if not can_cancel(run.status):
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="当前数据工作簿任务已结束，无法再取消。",
                workflow_run=run,
            )

        cancel_event = _DATA_TASK_CANCEL_EVENTS.setdefault(task_id, Event())
        cancel_event.set()
        cancelled_run = _cancelled_run(run)
        events = list(load_task_log_events(task_id) or [])
        events.append(
            _event(
                task_id,
                max((item.sequence for item in events), default=0) + 1,
                "task_cancelled",
                "已接受取消请求；不会登记新的 Excel 交付物。",
                step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
                level="warning",
            )
        )
        # 数据任务不会持久化客户原始目标全文，故历史页不能在服务重启后安全地一键重试。
        # 工具调用改为 skipped，明确区分“已取消”与“导出失败”。
        tool_calls = [
            call.model_copy(
                update={
                    "status": "skipped",
                    "error": "用户取消了数据工作簿导出。",
                    "finished_at": _now(),
                }
            )
            for call in list_workflow_tool_calls(task_id)
        ]
        save_workflow_run(
            run=cancelled_run,
            events=events,
            plan=None,
            artifacts=None,
            tool_calls=tool_calls,
        )

    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        level="warning",
        message="已接受取消请求；不会登记新的 Excel 交付物。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="数据工作簿导出已取消；源文件未被修改，可回到数据工作台重新确认导出。",
        workflow_run=cancelled_run,
    )


def get_data_workbook_export_task_result(task_id: str) -> DataWorkbookTaskResultResponse | None:
    """从 SQLite 恢复数据工作簿任务，服务重启后仍可查看任务和受控 artifact。"""

    run = load_workflow_run(task_id)
    if run is None:
        return None
    step = next((item for item in run.steps if item.step_id == DATA_WORKBOOK_EXPORT_STEP_ID), None)
    if step is None or step.action != DATA_WORKBOOK_EXPORT_TOOL_NAME:
        return None

    output = step.output
    verification = _load_verification(output)
    artifact = next(iter(list_workflow_artifacts(task_id)), None)
    status = run.status if run.status in {"pending", "running", "completed", "failed", "cancelled"} else "failed"
    return DataWorkbookTaskResultResponse(
        task_id=task_id,
        status=status,
        summary=run.summary,
        message=str(output.get("message", step.message)),
        artifact=_to_data_workbook_artifact(artifact),
        verification=verification,
        warnings=_string_list(output.get("warnings")),
        skipped_items=_string_list(output.get("skipped_items")),
    )


async def _persist_failed_task(
    *,
    task_id: str,
    request: DataWorkbookExportRequest,
    started_at: str,
    duration_ms: int,
    message: str,
    events: list[TaskLogEvent],
) -> DataWorkbookTaskResultResponse:
    with _DATA_TASK_LOCK:
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        # 失败终态与取消终态是互斥的。整个“检查 -> 写入失败”区间持锁，避免取消请求夹在
        # 中间时把用户已经看到的 cancelled 又改成 failed。
        failed_events = [
            *events,
            _event(
                task_id,
                4,
                "task_failed",
                message,
                step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
                level="error",
            ),
        ]
        run = _failed_run(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=duration_ms,
            message=message,
        )
        save_workflow_run(
            run=run,
            events=failed_events,
            plan=None,
            artifacts=[],
            tool_calls=[
                _tool_call(
                    task_id,
                    request,
                    status="failed",
                    duration_ms=duration_ms,
                    error=message,
                )
            ],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        level="error",
        message=message,
    )
    return DataWorkbookTaskResultResponse(
        task_id=task_id,
        status="failed",
        summary=run.summary,
        message=message,
    )


def _save_running_run(
    *,
    task_id: str,
    request: DataWorkbookExportRequest,
    started_at: str,
    events: list[TaskLogEvent],
) -> None:
    step = WorkflowStepRun(
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_WORKBOOK_EXPORT_TOOL_NAME,
        status="running",
        message="正在写入并回读验证可编辑 Excel；原始数据不会被修改。",
        output=_base_output(request),
    )
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="数据工作簿正在生成并回读验证。",
        steps=[step],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, step_total=1, tool_call_total=1),
    )
    save_workflow_run(
        run=run,
        events=events,
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(task_id, request, status="running")],
    )


def _completed_run(
    *,
    task_id: str,
    request: DataWorkbookExportRequest,
    result: DataWorkbookExportResponse,
    started_at: str,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    output = _base_output(request)
    output.update(
        {
            "artifact_name": result.artifact.name,
            "artifact_uri": result.artifact.uri,
            "verification": result.verification.model_dump(),
            "warnings": result.warnings,
            "skipped_items": result.skipped_items,
            "message": message,
        }
    )
    step = WorkflowStepRun(
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_WORKBOOK_EXPORT_TOOL_NAME,
        status="completed",
        message=message,
        output=output,
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary="数据工作簿已完成并通过本地回读验证。",
        steps=[step],
        limits=_limits(),
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
    *,
    task_id: str,
    request: DataWorkbookExportRequest,
    started_at: str,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    output = _base_output(request)
    output["message"] = message
    step = WorkflowStepRun(
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_WORKBOOK_EXPORT_TOOL_NAME,
        status="failed",
        message=message,
        output=output,
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="数据工作簿未完成，未保留不完整输出文件。",
        steps=[step],
        limits=_limits(),
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


def _artifact_for_result(
    task_id: str,
    request: DataWorkbookExportRequest,
    result: DataWorkbookExportResponse,
    output_path: Path,
) -> WorkflowArtifact:
    return WorkflowArtifact(
        artifact_id=f"artifact_data_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        kind="data",
        name=result.artifact.name,
        summary=(
            "已生成可编辑 Excel："
            f"{result.verification.table_count} 个原生表格、{result.verification.chart_count} 个原生图表，"
            "关键指标已回读验证。"
        ),
        uri=result.artifact.uri,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        metadata={
            "runtime": True,
            "output_scope": "data_analysis",
            # 绝对路径仅由后端在受控 artifact resolver 内部使用；展示/预览响应会自动脱敏。
            "output_path": str(output_path),
            "output_size_bytes": result.artifact.size_bytes,
            "dataset_name": request.dataset_name,
            "source_sha256": request.source_sha256,
            "verification": result.verification.model_dump(),
            "warnings": result.warnings,
            "skipped_items": result.skipped_items,
            "original_file_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        created_at=result.artifact.created_at,
    )


def _tool_call(
    task_id: str,
    request: DataWorkbookExportRequest,
    *,
    status: str,
    duration_ms: int = 0,
    result: DataWorkbookExportResponse | None = None,
    error: str = "",
) -> WorkflowToolCall:
    response: dict[str, Any] = {}
    if result is not None:
        response = {
            "artifact_name": result.artifact.name,
            "output_size_bytes": result.artifact.size_bytes,
            "verification": result.verification.model_dump(),
        }
    return WorkflowToolCall(
        call_id=f"call_data_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_WORKBOOK_EXPORT_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        tool_name=DATA_WORKBOOK_EXPORT_TOOL_NAME,
        status=status,
        risk_level="low",
        # 用户点击“确认导出”后才创建文件，且写入范围固定为 output/data_analysis。
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=duration_ms,
        request={
            "dataset_name": request.dataset_name,
            "source_sha256": request.source_sha256,
            "max_chart_count": request.max_chart_count,
            "write_scope": "output/data_analysis",
            "user_confirmed": True,
            "original_file_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        result=response,
        error=error,
        started_at="",
        finished_at=_now() if status in {"completed", "failed", "skipped"} else "",
    )


def _is_data_workbook_run(run: WorkflowRun | None) -> bool:
    """避免通用任务控制把同为 runtime 的其它 Agent 误判为数据导出。"""

    return bool(
        run
        and any(
            step.step_id == DATA_WORKBOOK_EXPORT_STEP_ID
            and step.action == DATA_WORKBOOK_EXPORT_TOOL_NAME
            for step in run.steps
        )
    )


def _cancelled_run(run: WorkflowRun) -> WorkflowRun:
    """保留已发生的运行事实，仅把未完成步骤稳定地转换为取消终态。"""

    now = _now()
    steps = [
        step.model_copy(
            update={
                "status": "cancelled",
                "message": "数据工作簿导出已被用户取消，未登记新的交付文件。",
                "output": {**step.output, "cancelled": True, "message": "用户取消了数据工作簿导出。"},
            }
        )
        if step.status in {"pending", "running", "waiting_permission"}
        else step
        for step in run.steps
    ]
    return run.model_copy(
        update={
            "status": "cancelled",
            "summary": "数据工作簿导出已取消，未保留新的交付文件。",
            "steps": steps,
            "metrics": run.metrics.model_copy(update={"finished_at": now}),
        }
    )


def _is_cancel_requested(task_id: str) -> bool:
    with _DATA_TASK_LOCK:
        event = _DATA_TASK_CANCEL_EVENTS.get(task_id)
        return bool(event and event.is_set())


def _cancelled_task_result(task_id: str) -> DataWorkbookTaskResultResponse:
    """从已经落库的取消终态重建 API 响应，避免后台线程随后覆盖客户看到的状态。"""

    result = get_data_workbook_export_task_result(task_id)
    if result is not None:
        return result
    return DataWorkbookTaskResultResponse(
        task_id=task_id,
        status="cancelled",
        summary="数据工作簿导出已取消。",
        message="已取消数据工作簿导出，未登记新的交付文件。",
    )


def _remove_cancelled_output(result: DataWorkbookExportResponse) -> None:
    """仅删除本任务刚刚生成、尚未登记 artifact 的受控文件。"""

    try:
        _resolve_created_output_path(result).unlink(missing_ok=True)
    except (DataWorkbookError, OSError):
        # 无法清理时不把取消改报成成功；后台没有 artifact，用户仍能从任务记录看见取消事实。
        pass


def _forget_data_task_cancel_state(task_id: str) -> None:
    with _DATA_TASK_LOCK:
        _DATA_TASK_CANCEL_EVENTS.pop(task_id, None)


def _resolve_created_output_path(result: DataWorkbookExportResponse) -> Path:
    """由服务端结果反查固定输出根，不把 artifact URI 当作文件路径解析。"""

    name = result.artifact.name
    if name != Path(name).name or not name.lower().endswith(".xlsx"):
        raise DataWorkbookError("数据工作簿返回了无效文件名，未登记该输出。")
    output_root = settings.data_analysis_output_dir.resolve()
    output_path = (output_root / name).resolve()
    try:
        output_path.relative_to(output_root)
    except ValueError as exc:  # pragma: no cover - 文件名服务端生成，仍保留目录防线。
        raise DataWorkbookError("数据工作簿输出超出受控目录，已拒绝登记。") from exc
    if not output_path.is_file():
        raise DataWorkbookError("数据工作簿回读后未找到正式输出文件，未登记 artifact。")
    return output_path


def _load_verification(output: dict[str, Any]) -> DataWorkbookVerification | None:
    payload = output.get("verification")
    if not isinstance(payload, dict):
        return None
    try:
        return DataWorkbookVerification.model_validate(payload)
    except ValueError:
        return None


def _to_data_workbook_artifact(artifact: WorkflowArtifact | None) -> DataWorkbookArtifact | None:
    if artifact is None:
        return None
    size_bytes = artifact.metadata.get("output_size_bytes")
    if not isinstance(size_bytes, int) or size_bytes < 1:
        return None
    return DataWorkbookArtifact(
        name=artifact.name,
        uri=artifact.uri,
        size_bytes=size_bytes,
        created_at=artifact.created_at,
    )


def _base_output(request: DataWorkbookExportRequest) -> dict[str, Any]:
    """写入任务历史的最小事实；不保存原始行、客户目标全文或本机绝对路径。"""

    return {
        "dataset_name": request.dataset_name,
        "source_sha256": request.source_sha256,
        "analysis_goal_provided": bool(request.goal.strip()),
        "cleaning_policy": request.cleaning_policy,
        "max_chart_count": request.max_chart_count,
        "write_scope": "output/data_analysis",
        "original_file_unchanged": True,
        "model_used": False,
        "network_used": False,
    }


def _limits() -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(
        max_steps=1,
        max_tool_calls=1,
        max_retries_per_tool=0,
        tool_timeout_ms=_TOOL_TIMEOUT_MS,
        task_timeout_ms=_TASK_TIMEOUT_MS,
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
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=step_id,
        level=level,
        message=message,
    )


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))
