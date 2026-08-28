"""D5.2 图表 PNG 的异步任务交付层。

渲染算法位于 :mod:`data_charts`；这里仅处理客户确认后的任务状态、协作式取消、事件流、
工具审计和 artifact 历史。D3 Excel 与 D5.2 PNG 各自有固定输出目录，避免一个交付物的
失败、取消或清理影响另一个交付物。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from threading import Event, RLock
from typing import Any

from app.database.task_repository import (
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_task_log_events,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.data_agent import (
    DataChartArtifact,
    DataChartExportRequest,
    DataChartExportResponse,
    DataChartTaskResultResponse,
    DataChartVerification,
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
from app.services.data_charts import (
    DataChartError,
    export_data_chart_pngs,
    remove_data_chart_output,
)
from app.services.task_event_stream import publish_live_task_event
from app.workflow.state_machine import can_cancel


DATA_CHART_EXPORT_STEP_ID = "data_chart_export"
DATA_CHART_EXPORT_TOOL_NAME = "data.render_chart_png"
DATA_ANALYSIS_AGENT_ID = "data_agent"
_TASK_TIMEOUT_MS = 150_000
_TOOL_TIMEOUT_MS = 120_000

# matplotlib/Pillow 渲染发生在工作线程，不能安全地强杀。取消请求先稳定落库；线程返回后在
# artifact 登记前检查该标记，确保已取消任务不会把半成品显示给客户。
_TASK_LOCK = RLock()
_TASK_CANCEL_EVENTS: dict[str, Event] = {}


def create_data_chart_queued_run(*, task_id: str, request: DataChartExportRequest) -> WorkflowRun:
    """在后台开始前登记待处理图表任务，历史页能立即看到真实状态。"""

    now = _now()
    with _TASK_LOCK:
        _TASK_CANCEL_EVENTS[task_id] = Event()
    step = WorkflowStepRun(
        step_id=DATA_CHART_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_CHART_EXPORT_TOOL_NAME,
        status="pending",
        message="已受理图表看板生成，等待本地渲染。",
        output=_base_output(request),
    )
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="图表看板生成已受理，尚未写入 PNG 文件。",
        steps=[step],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "图表看板生成已受理，将只写入新的受控 PNG。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_data_chart_export_task(
    *,
    task_id: str,
    request: DataChartExportRequest,
) -> DataChartTaskResultResponse:
    """后台生成图表 PNG，并只登记所有像素回读通过后的真实 artifact。"""

    if _is_cancel_requested(task_id):
        try:
            return _cancelled_task_result(task_id)
        finally:
            _forget_cancel_state(task_id)

    started_at = _now()
    started_clock = perf_counter()
    running_events = [
        _event(task_id, 1, "task_queued", "图表看板生成已受理，将只写入新的受控 PNG。"),
        _event(task_id, 2, "task_started", "正在复核数据版本并读取已验证的聚合结果。", step_id=DATA_CHART_EXPORT_STEP_ID),
        _event(task_id, 3, "tool_started", "正在本地绘制并回读验证 PNG 图表；源文件不会被修改。", step_id=DATA_CHART_EXPORT_STEP_ID),
    ]
    with _TASK_LOCK:
        if _is_cancel_requested(task_id):
            try:
                return _cancelled_task_result(task_id)
            finally:
                _forget_cancel_state(task_id)
        _save_running_run(task_id=task_id, request=request, started_at=started_at, events=running_events)

    await publish_live_task_event(
        task_id=task_id,
        event="task_started",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_CHART_EXPORT_STEP_ID,
        message="正在复核数据版本并读取已验证的聚合结果。",
    )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_CHART_EXPORT_STEP_ID,
        message="正在本地绘制并回读验证 PNG 图表；源文件不会被修改。",
    )

    try:
        result = await asyncio.to_thread(export_data_chart_pngs, request, task_id=task_id)
        with _TASK_LOCK:
            if _is_cancel_requested(task_id):
                remove_data_chart_output(task_id)
                return _cancelled_task_result(task_id)

            duration_ms = _duration_ms(started_clock)
            artifacts = [_artifact_for_result(task_id, request, item, result) for item in result.artifacts]
            message = f"已生成 {len(artifacts)} 张图表 PNG，并通过像素回读验证。"
            completed_events = [
                *running_events,
                _event(task_id, 4, "artifact_saved", "已生成图表 PNG，正在登记经过验证的交付物。", step_id=DATA_CHART_EXPORT_STEP_ID),
                _event(task_id, 5, "task_completed", "图表看板已完成并写入任务历史，源文件未被修改。", step_id=DATA_CHART_EXPORT_STEP_ID),
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
                artifacts=artifacts,
                tool_calls=[_tool_call(task_id, request, status="completed", duration_ms=duration_ms, result=result)],
            )
        await publish_live_task_event(
            task_id=task_id,
            event="artifact_saved",
            agent_id=DATA_ANALYSIS_AGENT_ID,
            step_id=DATA_CHART_EXPORT_STEP_ID,
            message=f"已生成 {len(artifacts)} 张已验证图表 PNG。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id=DATA_ANALYSIS_AGENT_ID,
            step_id=DATA_CHART_EXPORT_STEP_ID,
            message="图表看板已完成，源文件未被修改。",
        )
        return _result_from_export(task_id, run, message, result)
    except DataChartError as exc:
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
    except Exception:  # pragma: no cover - 后台任务必须留下客户可见终态。
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        return await _persist_failed_task(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message="图表 PNG 生成发生未预期错误，未保留不完整输出文件。",
            events=running_events,
        )
    finally:
        _forget_cancel_state(task_id)


async def cancel_data_chart_export_task(task_id: str) -> TaskControlResponse | None:
    """协作式取消 D5.2 任务，不影响已完成的既有交付物。"""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_data_chart_run(run):
            return None
        if not can_cancel(run.status):
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="图表任务已结束，不能取消已完成或已失败的记录。",
                workflow_run=run,
            )
        cancel_event = _TASK_CANCEL_EVENTS.setdefault(task_id, Event())
        cancel_event.set()
        cancelled_run = _cancelled_run(run)
        events = [*load_task_log_events(task_id), _event(task_id, len(load_task_log_events(task_id)) + 1, "task_cancelled", "图表看板生成已取消，未登记新的 PNG。", step_id=DATA_CHART_EXPORT_STEP_ID, level="warning")]
        save_workflow_run(
            run=cancelled_run,
            events=events,
            plan=None,
            artifacts=list_workflow_artifacts(task_id),
            tool_calls=list_workflow_tool_calls(task_id),
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_CHART_EXPORT_STEP_ID,
        level="warning",
        message="图表看板生成已取消，未登记新的 PNG。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="图表看板生成已取消；源文件未被修改，可回到数据工作台重新确认保存。",
        workflow_run=cancelled_run,
    )


def get_data_chart_export_task_result(task_id: str) -> DataChartTaskResultResponse | None:
    """从 SQLite 重建图表任务终态，服务重启后仍可回看 PNG artifact。"""

    run = load_workflow_run(task_id)
    if not _is_data_chart_run(run):
        return None
    assert run is not None
    step = next(item for item in run.steps if item.step_id == DATA_CHART_EXPORT_STEP_ID)
    output = step.output
    return DataChartTaskResultResponse(
        task_id=task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", step.message)),
        artifacts=[item for artifact in list_workflow_artifacts(task_id) if (item := _to_data_chart_artifact(artifact)) is not None],
        verification=_load_verification(output),
        warnings=_string_list(output.get("warnings")),
        skipped_items=_string_list(output.get("skipped_items")),
    )


async def _persist_failed_task(
    *,
    task_id: str,
    request: DataChartExportRequest,
    started_at: str,
    duration_ms: int,
    message: str,
    events: list[TaskLogEvent],
) -> DataChartTaskResultResponse:
    with _TASK_LOCK:
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        remove_data_chart_output(task_id)
        failed_run = _failed_run(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=duration_ms,
            message=message,
        )
        failed_events = [*events, _event(task_id, 4, "task_failed", message, step_id=DATA_CHART_EXPORT_STEP_ID, level="error")]
        save_workflow_run(
            run=failed_run,
            events=failed_events,
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(task_id, request, status="failed", duration_ms=duration_ms, error=message)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_CHART_EXPORT_STEP_ID,
        level="error",
        message=message,
    )
    return DataChartTaskResultResponse(
        task_id=task_id,
        status="failed",
        summary=failed_run.summary,
        message=message,
    )


def _save_running_run(*, task_id: str, request: DataChartExportRequest, started_at: str, events: list[TaskLogEvent]) -> None:
    step = WorkflowStepRun(
        step_id=DATA_CHART_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_CHART_EXPORT_TOOL_NAME,
        status="running",
        message="正在本地绘制并回读验证 PNG 图表。",
        output=_base_output(request),
    )
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="正在生成可保存的图表看板。",
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
    request: DataChartExportRequest,
    result: DataChartExportResponse,
    started_at: str,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    output = _base_output(request)
    output.update(
        {
            "message": message,
            "artifact_count": len(result.artifacts),
            "verification": result.verification.model_dump(),
            "warnings": result.warnings,
            "skipped_items": result.skipped_items,
        }
    )
    step = WorkflowStepRun(
        step_id=DATA_CHART_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_CHART_EXPORT_TOOL_NAME,
        status="completed",
        message=message,
        output=output,
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary="图表看板已完成并通过本地 PNG 回读验证。",
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


def _failed_run(*, task_id: str, request: DataChartExportRequest, started_at: str, duration_ms: int, message: str) -> WorkflowRun:
    output = _base_output(request)
    output["message"] = message
    step = WorkflowStepRun(
        step_id=DATA_CHART_EXPORT_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_CHART_EXPORT_TOOL_NAME,
        status="failed",
        message=message,
        output=output,
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="图表看板未完成，未保留不完整输出文件。",
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


def _artifact_for_result(task_id: str, request: DataChartExportRequest, item: DataChartArtifact, result: DataChartExportResponse) -> WorkflowArtifact:
    # ``output_path`` 仅供后端的受控 resolver 使用；通用 artifact 列表会自动脱敏该字段。
    from app.services.data_charts import resolve_data_chart_artifact_path

    output_path = resolve_data_chart_artifact_path(task_id=task_id, filename=item.name)
    return WorkflowArtifact(
        artifact_id=item.artifact_id,
        task_id=task_id,
        step_id=DATA_CHART_EXPORT_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        kind="data",
        name=item.name,
        summary=f"{item.title} · {item.chart_type} · {item.width}x{item.height}",
        uri=item.uri,
        mime_type="image/png",
        metadata={
            "runtime": True,
            "output_scope": "data_charts",
            "output_path": str(output_path),
            "output_size_bytes": item.size_bytes,
            "chart_id": item.chart_id,
            "chart_type": item.chart_type,
            "chart_title": item.title,
            "width": item.width,
            "height": item.height,
            "dataset_name": request.dataset_name,
            "source_sha256": request.source_sha256,
            "verification": result.verification.model_dump(),
            "original_file_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        created_at=item.created_at,
    )


def _tool_call(task_id: str, request: DataChartExportRequest, *, status: str, duration_ms: int = 0, result: DataChartExportResponse | None = None, error: str = "") -> WorkflowToolCall:
    response: dict[str, Any] = {}
    if result is not None:
        response = {
            "chart_count": result.verification.chart_count,
            "chart_ids": result.verification.chart_ids,
            "image_sizes": result.verification.image_sizes,
        }
    return WorkflowToolCall(
        call_id=f"call_chart_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_CHART_EXPORT_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        tool_name=DATA_CHART_EXPORT_TOOL_NAME,
        status=status,
        risk_level="low",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=duration_ms,
        request={
            "dataset_name": request.dataset_name,
            "source_sha256": request.source_sha256,
            "max_chart_count": request.max_chart_count,
            "write_scope": "output/data_charts",
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


def _base_output(request: DataChartExportRequest) -> dict[str, Any]:
    return {
        "dataset_name": request.dataset_name,
        "source_sha256": request.source_sha256,
        "analysis_goal_provided": bool(request.goal.strip()),
        "cleaning_policy": request.cleaning_policy,
        "max_chart_count": request.max_chart_count,
        "write_scope": "output/data_charts",
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


def _is_data_chart_run(run: WorkflowRun | None) -> bool:
    return bool(run and any(step.step_id == DATA_CHART_EXPORT_STEP_ID and step.action == DATA_CHART_EXPORT_TOOL_NAME for step in run.steps))


def _cancelled_run(run: WorkflowRun) -> WorkflowRun:
    now = _now()
    steps = [
        step.model_copy(
            update={
                "status": "cancelled",
                "message": "图表看板生成已被用户取消，未登记新的 PNG。",
                "output": {**step.output, "cancelled": True, "message": "用户取消了图表看板生成。"},
            }
        )
        if step.status in {"pending", "running", "waiting_permission"}
        else step
        for step in run.steps
    ]
    return run.model_copy(
        update={
            "status": "cancelled",
            "summary": "图表看板生成已取消，未保留新的 PNG 文件。",
            "steps": steps,
            "metrics": run.metrics.model_copy(update={"finished_at": now}),
        }
    )


def _is_cancel_requested(task_id: str) -> bool:
    with _TASK_LOCK:
        event = _TASK_CANCEL_EVENTS.get(task_id)
        return bool(event and event.is_set())


def _cancelled_task_result(task_id: str) -> DataChartTaskResultResponse:
    result = get_data_chart_export_task_result(task_id)
    if result is not None:
        return result
    return DataChartTaskResultResponse(
        task_id=task_id,
        status="cancelled",
        summary="图表看板生成已取消。",
        message="已取消图表看板生成，未登记新的 PNG。",
    )


def _forget_cancel_state(task_id: str) -> None:
    with _TASK_LOCK:
        _TASK_CANCEL_EVENTS.pop(task_id, None)


def _load_verification(output: dict[str, Any]) -> DataChartVerification | None:
    payload = output.get("verification")
    if not isinstance(payload, dict):
        return None
    try:
        return DataChartVerification.model_validate(payload)
    except ValueError:
        return None


def _to_data_chart_artifact(artifact: WorkflowArtifact) -> DataChartArtifact | None:
    metadata = artifact.metadata
    try:
        return DataChartArtifact(
            artifact_id=artifact.artifact_id,
            chart_id=str(metadata["chart_id"]),
            chart_type=str(metadata["chart_type"]),
            title=str(metadata["chart_title"]),
            name=artifact.name,
            uri=artifact.uri,
            size_bytes=int(metadata["output_size_bytes"]),
            width=int(metadata["width"]),
            height=int(metadata["height"]),
            created_at=artifact.created_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _result_from_export(task_id: str, run: WorkflowRun, message: str, result: DataChartExportResponse) -> DataChartTaskResultResponse:
    return DataChartTaskResultResponse(
        task_id=task_id,
        status="completed",
        summary=run.summary,
        message=message,
        artifacts=result.artifacts,
        verification=result.verification,
        warnings=result.warnings,
        skipped_items=result.skipped_items,
    )


def _event(task_id: str, sequence: int, event: str, message: str, *, step_id: str | None = None, level: str = "info") -> TaskLogEvent:
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
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))


def _now() -> str:
    return datetime.now(UTC).isoformat()
