"""D5.3 字段加工副本的 Runtime 交付链路。

计算和数据副本回读位于 :mod:`data_transformations`；本模块只负责统一任务状态、实时事件、
取消、工具审计与 artifact。字段加工与分析工作簿/PNG 使用不同输出根，互不清理对方文件。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from threading import Event, RLock
from time import perf_counter
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
    DataTransformPlan,
    DataTransformPreviewRequest,
    DataTransformationArtifact,
    DataTransformationExportRequest,
    DataTransformationExportResponse,
    DataTransformationTaskResultResponse,
    DataTransformationVerification,
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
from app.services.data_transformations import (
    DataTransformationError,
    export_data_transformation_workbook,
    remove_data_transformation_output,
)
from app.services.task_event_stream import publish_live_task_event
from app.workflow.state_machine import can_cancel


DATA_TRANSFORMATION_STEP_ID = "data_transformation_export"
DATA_TRANSFORMATION_TOOL_NAME = "data.transform_fields"
DATA_ANALYSIS_AGENT_ID = "data_agent"
_TASK_TIMEOUT_MS = 150_000
_TOOL_TIMEOUT_MS = 120_000

# openpyxl 写入无法安全强杀。取消先持久化，线程回到安全提交点后删除尚未登记的新副本。
_TASK_LOCK = RLock()
_TASK_CANCEL_EVENTS: dict[str, Event] = {}


def create_data_transformation_queued_run(
    *, task_id: str, request: DataTransformationExportRequest
) -> WorkflowRun:
    """在后台写入前立即登记任务，历史页不会出现黑盒等待。"""

    now = _now()
    with _TASK_LOCK:
        _TASK_CANCEL_EVENTS[task_id] = Event()
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="字段加工副本已受理，尚未写入新文件。",
        steps=[
            WorkflowStepRun(
                step_id=DATA_TRANSFORMATION_STEP_ID,
                agent=DATA_ANALYSIS_AGENT_ID,
                action=DATA_TRANSFORMATION_TOOL_NAME,
                status="pending",
                message="已受理字段加工，等待复核计划与写入新副本。",
                output=_base_output(request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "字段加工已受理，将只生成新的受控数据副本。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_data_transformation_task(
    *, task_id: str, request: DataTransformationExportRequest
) -> DataTransformationTaskResultResponse:
    """后台生成字段加工副本，验证通过后才登记 artifact。"""

    if _is_cancel_requested(task_id):
        try:
            return _cancelled_task_result(task_id)
        finally:
            _forget_cancel_state(task_id)

    started_at = _now()
    started_clock = perf_counter()
    running_events = [
        _event(task_id, 1, "task_queued", "字段加工已受理，将只生成新的受控数据副本。"),
        _event(task_id, 2, "task_started", "正在复核数据版本和字段加工计划。", step_id=DATA_TRANSFORMATION_STEP_ID),
        _event(task_id, 3, "tool_started", "正在本地加工字段并重新打开验证新副本；源文件不会被修改。", step_id=DATA_TRANSFORMATION_STEP_ID),
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
        step_id=DATA_TRANSFORMATION_STEP_ID,
        message="正在复核数据版本和字段加工计划。",
    )
    await publish_live_task_event(
        task_id=task_id,
        event="tool_started",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_TRANSFORMATION_STEP_ID,
        message="正在本地加工字段并重新打开验证新副本；源文件不会被修改。",
    )
    try:
        result = await asyncio.to_thread(export_data_transformation_workbook, request)
        with _TASK_LOCK:
            if _is_cancel_requested(task_id):
                remove_data_transformation_output(result)
                return _cancelled_task_result(task_id)
            duration_ms = _duration_ms(started_clock)
            message = f"已新增字段“{result.plan.result_column}”，并通过新副本回读验证。"
            run = _completed_run(
                task_id=task_id,
                request=request,
                result=result,
                started_at=started_at,
                duration_ms=duration_ms,
                message=message,
            )
            events = [
                *running_events,
                _event(task_id, 4, "artifact_saved", "字段加工副本已通过回读，正在登记交付物。", step_id=DATA_TRANSFORMATION_STEP_ID),
                _event(task_id, 5, "task_completed", "字段加工副本已完成并写入任务历史，源文件未被修改。", step_id=DATA_TRANSFORMATION_STEP_ID),
            ]
            save_workflow_run(
                run=run,
                events=events,
                plan=None,
                artifacts=[_artifact_for_result(task_id, request, result)],
                tool_calls=[_tool_call(task_id, request, status="completed", duration_ms=duration_ms, result=result)],
            )
        await publish_live_task_event(
            task_id=task_id,
            event="artifact_saved",
            agent_id=DATA_ANALYSIS_AGENT_ID,
            step_id=DATA_TRANSFORMATION_STEP_ID,
            message=f"已生成并验证字段“{result.plan.result_column}”的新副本。",
        )
        await publish_live_task_event(
            task_id=task_id,
            event="task_completed",
            agent_id=DATA_ANALYSIS_AGENT_ID,
            step_id=DATA_TRANSFORMATION_STEP_ID,
            message="字段加工副本已完成，源文件未被修改。",
        )
        return _result_from_export(task_id, run, message, result)
    except DataTransformationError as exc:
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
    except Exception:  # pragma: no cover - 后台异常也必须落到客户可见终态。
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        return await _persist_failed_task(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message="字段加工发生未预期错误，未保留不完整输出文件。",
            events=running_events,
        )
    finally:
        _forget_cancel_state(task_id)


async def cancel_data_transformation_task(task_id: str) -> TaskControlResponse | None:
    """协作式取消字段加工，不删除此前已完成的交付物。"""

    with _TASK_LOCK:
        run = load_workflow_run(task_id)
        if not _is_data_transformation_run(run):
            return None
        assert run is not None
        if not can_cancel(run.status):
            return TaskControlResponse(
                task_id=task_id,
                action="cancel",
                accepted=False,
                status=run.status,
                message="字段加工任务已结束，不能取消已完成或已失败的记录。",
                workflow_run=run,
            )
        _TASK_CANCEL_EVENTS.setdefault(task_id, Event()).set()
        cancelled = _cancelled_run(run)
        old_events = load_task_log_events(task_id)
        save_workflow_run(
            run=cancelled,
            events=[*old_events, _event(task_id, len(old_events) + 1, "task_cancelled", "字段加工已取消，未登记新的数据副本。", step_id=DATA_TRANSFORMATION_STEP_ID, level="warning")],
            plan=None,
            artifacts=list_workflow_artifacts(task_id),
            tool_calls=list_workflow_tool_calls(task_id),
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_cancelled",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_TRANSFORMATION_STEP_ID,
        level="warning",
        message="字段加工已取消，未登记新的数据副本。",
    )
    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=True,
        status="cancelled",
        message="字段加工已取消；源文件未被修改，可回到数据工作台重新确认。",
        workflow_run=cancelled,
    )


def get_data_transformation_task_result(task_id: str) -> DataTransformationTaskResultResponse | None:
    """从 SQLite 恢复字段加工任务；不回传客户原始表格或绝对路径。"""

    run = load_workflow_run(task_id)
    if not _is_data_transformation_run(run):
        return None
    assert run is not None
    step = next(item for item in run.steps if item.step_id == DATA_TRANSFORMATION_STEP_ID)
    output = step.output
    artifact = next(
        (item for candidate in list_workflow_artifacts(task_id) if (item := _to_transformation_artifact(candidate)) is not None),
        None,
    )
    return DataTransformationTaskResultResponse(
        task_id=task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", step.message)),
        artifact=artifact,
        plan=_load_plan(output),
        plans=_load_plans(output),
        verification=_load_verification(output),
        affected_count=_safe_int(output.get("affected_count")),
        empty_result_count=_safe_int(output.get("empty_result_count")),
        warnings=_string_list(output.get("warnings")),
    )


async def _persist_failed_task(
    *,
    task_id: str,
    request: DataTransformationExportRequest,
    started_at: str,
    duration_ms: int,
    message: str,
    events: list[TaskLogEvent],
) -> DataTransformationTaskResultResponse:
    with _TASK_LOCK:
        if _is_cancel_requested(task_id):
            return _cancelled_task_result(task_id)
        failed = _failed_run(task_id=task_id, request=request, started_at=started_at, duration_ms=duration_ms, message=message)
        save_workflow_run(
            run=failed,
            events=[*events, _event(task_id, 4, "task_failed", message, step_id=DATA_TRANSFORMATION_STEP_ID, level="error")],
            plan=None,
            artifacts=[],
            tool_calls=[_tool_call(task_id, request, status="failed", duration_ms=duration_ms, error=message)],
        )
    await publish_live_task_event(
        task_id=task_id,
        event="task_failed",
        agent_id=DATA_ANALYSIS_AGENT_ID,
        step_id=DATA_TRANSFORMATION_STEP_ID,
        level="error",
        message=message,
    )
    return DataTransformationTaskResultResponse(
        task_id=task_id,
        status="failed",
        summary=failed.summary,
        message=message,
    )


def _save_running_run(*, task_id: str, request: DataTransformationExportRequest, started_at: str, events: list[TaskLogEvent]) -> None:
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="正在生成可复核的字段加工副本。",
        steps=[
            WorkflowStepRun(
                step_id=DATA_TRANSFORMATION_STEP_ID,
                agent=DATA_ANALYSIS_AGENT_ID,
                action=DATA_TRANSFORMATION_TOOL_NAME,
                status="running",
                message="正在本地加工字段并重新打开验证新副本。",
                output=_base_output(request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, step_total=1, tool_call_total=1),
    )
    save_workflow_run(run=run, events=events, plan=None, artifacts=[], tool_calls=[_tool_call(task_id, request, status="running")])


def _completed_run(*, task_id: str, request: DataTransformationExportRequest, result: DataTransformationExportResponse, started_at: str, duration_ms: int, message: str) -> WorkflowRun:
    output = _base_output(request)
    output.update(
        {
            "message": message,
            "plan": result.plan.model_dump(),
            "plans": [plan.model_dump() for plan in result.plans],
            "affected_count": result.affected_count,
            "empty_result_count": result.empty_result_count,
            "verification": result.verification.model_dump(),
            "warnings": result.warnings,
        }
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary=f"已新增 {max(1, len(result.plans))} 个字段，并通过本地数据副本回读验证。",
        steps=[WorkflowStepRun(step_id=DATA_TRANSFORMATION_STEP_ID, agent=DATA_ANALYSIS_AGENT_ID, action=DATA_TRANSFORMATION_TOOL_NAME, status="completed", message=message, output=output)],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, finished_at=_now(), duration_ms=duration_ms, step_total=1, step_completed=1, tool_call_total=1),
    )


def _failed_run(*, task_id: str, request: DataTransformationExportRequest, started_at: str, duration_ms: int, message: str) -> WorkflowRun:
    output = _base_output(request)
    output["message"] = message
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="字段加工未完成，未保留不完整输出文件。",
        steps=[WorkflowStepRun(step_id=DATA_TRANSFORMATION_STEP_ID, agent=DATA_ANALYSIS_AGENT_ID, action=DATA_TRANSFORMATION_TOOL_NAME, status="failed", message=message, output=output)],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, finished_at=_now(), duration_ms=duration_ms, step_total=1, step_failed=1, tool_call_total=1, tool_call_failed=1),
    )


def _artifact_for_result(task_id: str, request: DataTransformationExportRequest, result: DataTransformationExportResponse) -> WorkflowArtifact:
    output_path = (settings.data_transformation_output_dir / result.artifact.name).resolve()
    return WorkflowArtifact(
        artifact_id=f"artifact_transform_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_TRANSFORMATION_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        kind="data",
        name=result.artifact.name,
        summary=f"新增 {max(1, len(result.plans))} 个字段 · 有效结果 {result.affected_count} 个单元格",
        uri=result.artifact.uri,
        mime_type=(
            "text/csv"
            if result.artifact.name.casefold().endswith(".csv")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        metadata={
            "runtime": True,
            "output_scope": "data_transformations",
            "output_path": str(output_path),
            "output_size_bytes": result.artifact.size_bytes,
            "dataset_name": request.dataset_name,
            "source_sha256": request.source_sha256,
            "operation_type": result.plan.operation_type,
            "result_column": result.plan.result_column,
            "result_columns": [plan.result_column for plan in result.plans],
            "operation_count": max(1, len(result.plans)),
            "affected_count": result.affected_count,
            "empty_result_count": result.empty_result_count,
            "verification": result.verification.model_dump(),
            "original_file_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        created_at=result.artifact.created_at,
    )


def _tool_call(*args: Any, **kwargs: Any) -> WorkflowToolCall:
    task_id: str = args[0]
    request: DataTransformationExportRequest = args[1]
    status: str = kwargs["status"]
    duration_ms: int = kwargs.get("duration_ms", 0)
    result: DataTransformationExportResponse | None = kwargs.get("result")
    error: str = kwargs.get("error", "")
    payload: dict[str, Any] = {}
    if result is not None:
        payload = {
            "result_column": result.plan.result_column,
            "result_columns": [plan.result_column for plan in result.plans],
            "affected_count": result.affected_count,
            "empty_result_count": result.empty_result_count,
            "verification_passed": result.verification.passed,
        }
    return WorkflowToolCall(
        call_id=f"call_transform_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_TRANSFORMATION_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        tool_name=DATA_TRANSFORMATION_TOOL_NAME,
        status=status,
        risk_level="low",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=duration_ms,
        request={
            "dataset_name": request.dataset_name,
            "source_sha256": request.source_sha256,
            "operation_type": request.operation_type,
            "operation_count": max(1, len(request.operations)),
            "write_scope": "output/data_transformations",
            "user_confirmed": True,
            "original_file_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        result=payload,
        error=error,
        finished_at=_now() if status in {"completed", "failed", "skipped"} else "",
    )


def _base_output(request: DataTransformationExportRequest) -> dict[str, Any]:
    return {
        "dataset_name": request.dataset_name,
        "source_sha256": request.source_sha256,
        "operation_type": request.operation_type,
        "primary_column": request.primary_column,
        "secondary_column": request.secondary_column,
        "result_column_requested": request.result_column or "",
        "operation_count": max(1, len(request.operations)),
        "write_scope": "output/data_transformations",
        "original_file_unchanged": True,
        "model_used": False,
        "network_used": False,
    }


def _limits() -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(max_steps=1, max_tool_calls=1, max_retries_per_tool=0, tool_timeout_ms=_TOOL_TIMEOUT_MS, task_timeout_ms=_TASK_TIMEOUT_MS)


def _is_data_transformation_run(run: WorkflowRun | None) -> bool:
    return bool(run and any(step.step_id == DATA_TRANSFORMATION_STEP_ID and step.action == DATA_TRANSFORMATION_TOOL_NAME for step in run.steps))


def _cancelled_run(run: WorkflowRun) -> WorkflowRun:
    now = _now()
    return run.model_copy(
        update={
            "status": "cancelled",
            "summary": "字段加工已取消，未保留新的数据副本。",
            "steps": [
                step.model_copy(update={"status": "cancelled", "message": "字段加工已被用户取消，未登记新的数据副本。", "output": {**step.output, "cancelled": True, "message": "用户取消了字段加工。"}})
                if step.status in {"pending", "running", "waiting_permission"}
                else step
                for step in run.steps
            ],
            "metrics": run.metrics.model_copy(update={"finished_at": now}),
        }
    )


def _is_cancel_requested(task_id: str) -> bool:
    with _TASK_LOCK:
        event = _TASK_CANCEL_EVENTS.get(task_id)
        return bool(event and event.is_set())


def _cancelled_task_result(task_id: str) -> DataTransformationTaskResultResponse:
    result = get_data_transformation_task_result(task_id)
    if result is not None:
        return result
    return DataTransformationTaskResultResponse(task_id=task_id, status="cancelled", summary="字段加工已取消。", message="已取消字段加工，未登记新的数据副本。")


def _forget_cancel_state(task_id: str) -> None:
    with _TASK_LOCK:
        _TASK_CANCEL_EVENTS.pop(task_id, None)


def _load_plan(output: dict[str, Any]) -> DataTransformPlan | None:
    payload = output.get("plan")
    if not isinstance(payload, dict):
        return None
    try:
        return DataTransformPlan.model_validate(payload)
    except ValueError:
        return None


def _load_plans(output: dict[str, Any]) -> list[DataTransformPlan]:
    payload = output.get("plans")
    if not isinstance(payload, list):
        plan = _load_plan(output)
        return [plan] if plan is not None else []
    plans: list[DataTransformPlan] = []
    # 队列合同允许最多十二项；任务恢复必须保留全部已验证计划，不能沿用早期四项演示上限。
    for item in payload[:12]:
        if not isinstance(item, dict):
            continue
        try:
            plans.append(DataTransformPlan.model_validate(item))
        except ValueError:
            continue
    return plans


def _load_verification(output: dict[str, Any]) -> DataTransformationVerification | None:
    payload = output.get("verification")
    if not isinstance(payload, dict):
        return None
    try:
        return DataTransformationVerification.model_validate(payload)
    except ValueError:
        return None


def _to_transformation_artifact(artifact: WorkflowArtifact) -> DataTransformationArtifact | None:
    if artifact.metadata.get("output_scope") != "data_transformations":
        return None
    try:
        return DataTransformationArtifact(name=artifact.name, uri=artifact.uri, size_bytes=int(artifact.metadata["output_size_bytes"]), created_at=artifact.created_at)
    except (KeyError, TypeError, ValueError):
        return None


def _result_from_export(task_id: str, run: WorkflowRun, message: str, result: DataTransformationExportResponse) -> DataTransformationTaskResultResponse:
    return DataTransformationTaskResultResponse(task_id=task_id, status="completed", summary=run.summary, message=message, artifact=result.artifact, plan=result.plan, plans=result.plans, verification=result.verification, affected_count=result.affected_count, empty_result_count=result.empty_result_count, warnings=result.warnings)


def _event(task_id: str, sequence: int, event: str, message: str, *, step_id: str | None = None, level: str = "info") -> TaskLogEvent:
    return TaskLogEvent(task_id=task_id, sequence=sequence, event=event, agent_id=DATA_ANALYSIS_AGENT_ID, step_id=step_id, level=level, message=message)


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))


def _now() -> str:
    return datetime.now(UTC).isoformat()
