"""R5.4C 多数据集合并的任务历史与 artifact 交付。

连接计算本身位于 :mod:`data_join`。这里负责把确认后的单次交付包装成标准子任务，保存
pending/running/completed/failed 状态和脱敏工具调用，让 Commander 能在同一条会话里交付结果。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from app.core.config import settings
from app.database.task_repository import (
    list_workflow_artifacts,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.data_agent import (
    DataJoinArtifact,
    DataJoinExportRequest,
    DataJoinExportResponse,
    DataJoinPlan,
    DataJoinTaskResultResponse,
    DataJoinVerification,
)
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.data_join import DataJoinError, export_data_join_copy


DATA_JOIN_STEP_ID = "data_join_export"
DATA_JOIN_TOOL_NAME = "data.join_datasets"
DATA_ANALYSIS_AGENT_ID = "data_agent"
_TASK_TIMEOUT_MS = 150_000
_TOOL_TIMEOUT_MS = 120_000


def create_data_join_queued_run(*, task_id: str, request: DataJoinExportRequest) -> WorkflowRun:
    """在合并开始前登记子任务，避免父任务出现无状态等待。"""

    now = _now()
    step = WorkflowStepRun(
        step_id=DATA_JOIN_STEP_ID,
        agent=DATA_ANALYSIS_AGENT_ID,
        action=DATA_JOIN_TOOL_NAME,
        status="pending",
        message="已受理多数据集合并，等待生成新的受控副本。",
        output=_base_output(request),
    )
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="多数据集合并已受理，尚未写入输出文件。",
        steps=[step],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "多数据集合并已受理，将只生成新的数据副本。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_data_join_task(*, task_id: str, request: DataJoinExportRequest) -> DataJoinTaskResultResponse:
    """执行确定性连接、回读验证并写入子任务历史。"""

    started_at = _now()
    started_clock = perf_counter()
    running_events = [
        _event(task_id, 1, "task_queued", "多数据集合并已受理，将只生成新的数据副本。"),
        _event(task_id, 2, "task_started", "正在复核两份数据版本和关联键。", step_id=DATA_JOIN_STEP_ID),
        _event(task_id, 3, "tool_started", "正在建立受控关联并回读验证新副本。", step_id=DATA_JOIN_STEP_ID),
    ]
    _save_running_run(task_id=task_id, request=request, started_at=started_at, events=running_events)
    try:
        result = await asyncio.to_thread(export_data_join_copy, request)
        duration_ms = _duration_ms(started_clock)
        message = (
            f"已合并两份数据，生成 {result.artifact.name}；"
            f"输出 {result.output_row_count} 行，关联键和源版本已回读验证。"
        )
        run = _completed_run(task_id=task_id, request=request, result=result, started_at=started_at, duration_ms=duration_ms, message=message)
        save_workflow_run(
            run=run,
            events=[
                *running_events,
                _event(task_id, 4, "artifact_saved", "合并副本已通过回读，正在登记交付物。", step_id=DATA_JOIN_STEP_ID),
                _event(task_id, 5, "task_completed", "多数据集合并已完成，源文件未被修改。", step_id=DATA_JOIN_STEP_ID),
            ],
            plan=None,
            artifacts=[_artifact_for_result(task_id, request, result)],
            tool_calls=[_tool_call(task_id, request, status="completed", duration_ms=duration_ms, result=result)],
        )
        return _result_from_export(task_id, run, message, result)
    except DataJoinError as exc:
        return _persist_failed_task(task_id=task_id, request=request, started_at=started_at, duration_ms=_duration_ms(started_clock), message=str(exc), events=running_events)
    except Exception:
        return _persist_failed_task(task_id=task_id, request=request, started_at=started_at, duration_ms=_duration_ms(started_clock), message="多数据集合并发生未预期错误，未保留不完整输出文件。", events=running_events)


def get_data_join_task_result(task_id: str) -> DataJoinTaskResultResponse | None:
    """从 SQLite 恢复脱敏的合并任务结果。"""

    run = load_workflow_run(task_id)
    if not _is_data_join_run(run):
        return None
    assert run is not None
    step = next(item for item in run.steps if item.step_id == DATA_JOIN_STEP_ID)
    output = step.output
    artifact = next((item for item in list_workflow_artifacts(task_id) if item.metadata.get("output_scope") == "data_joins"), None)
    return DataJoinTaskResultResponse(
        task_id=task_id,
        status=run.status,
        summary=run.summary,
        message=str(output.get("message", step.message)),
        artifact=_to_artifact(artifact),
        plan=_load_plan(output),
        verification=_load_verification(output),
        output_row_count=_safe_int(output.get("output_row_count")),
        matched_row_count=_safe_int(output.get("matched_row_count")),
        left_only_row_count=_safe_int(output.get("left_only_row_count")),
        right_only_row_count=_safe_int(output.get("right_only_row_count")),
        warnings=_string_list(output.get("warnings")),
    )


def _save_running_run(*, task_id: str, request: DataJoinExportRequest, started_at: str, events: list[TaskLogEvent]) -> None:
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="正在建立受控多数据集关联。",
        steps=[WorkflowStepRun(step_id=DATA_JOIN_STEP_ID, agent=DATA_ANALYSIS_AGENT_ID, action=DATA_JOIN_TOOL_NAME, status="running", message="正在建立关联并回读验证新副本。", output=_base_output(request))],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, step_total=1, tool_call_total=1),
    )
    save_workflow_run(run=run, events=events, plan=None, artifacts=[], tool_calls=[_tool_call(task_id, request, status="running")])


def _completed_run(*, task_id: str, request: DataJoinExportRequest, result: DataJoinExportResponse, started_at: str, duration_ms: int, message: str) -> WorkflowRun:
    output = _base_output(request)
    output.update(
        {
            "message": message,
            "plan": result.plan.model_dump(mode="json"),
            "verification": result.verification.model_dump(mode="json"),
            "output_row_count": result.output_row_count,
            "matched_row_count": result.matched_row_count,
            "left_only_row_count": result.left_only_row_count,
            "right_only_row_count": result.right_only_row_count,
            "warnings": result.warnings,
        }
    )
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary="多数据集合并已完成并通过副本回读验证。",
        steps=[WorkflowStepRun(step_id=DATA_JOIN_STEP_ID, agent=DATA_ANALYSIS_AGENT_ID, action=DATA_JOIN_TOOL_NAME, status="completed", message=message, output=output)],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, finished_at=_now(), duration_ms=duration_ms, step_total=1, step_completed=1, tool_call_total=1),
    )


def _persist_failed_task(*, task_id: str, request: DataJoinExportRequest, started_at: str, duration_ms: int, message: str, events: list[TaskLogEvent]) -> DataJoinTaskResultResponse:
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="多数据集合并未完成，未保留不完整输出文件。",
        steps=[WorkflowStepRun(step_id=DATA_JOIN_STEP_ID, agent=DATA_ANALYSIS_AGENT_ID, action=DATA_JOIN_TOOL_NAME, status="failed", message=message, output={**_base_output(request), "message": message})],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, finished_at=_now(), duration_ms=duration_ms, step_total=1, step_failed=1, tool_call_total=1, tool_call_failed=1),
    )
    save_workflow_run(
        run=run,
        events=[*events, _event(task_id, 4, "task_failed", message, step_id=DATA_JOIN_STEP_ID, level="error")],
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(task_id, request, status="failed", duration_ms=duration_ms, error=message)],
    )
    return DataJoinTaskResultResponse(task_id=task_id, status="failed", summary=run.summary, message=message)


def _artifact_for_result(task_id: str, request: DataJoinExportRequest, result: DataJoinExportResponse) -> WorkflowArtifact:
    output_path = (settings.data_join_output_dir / result.artifact.name).resolve()
    return WorkflowArtifact(
        artifact_id=f"artifact_join_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_JOIN_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        kind="data",
        name=result.artifact.name,
        summary=f"两份数据已按 {result.plan.left_key} = {result.plan.right_key} 关联，输出 {result.output_row_count} 行。",
        uri=result.artifact.uri,
        mime_type="text/csv" if result.artifact.name.casefold().endswith(".csv") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        metadata={
            "runtime": True,
            "output_scope": "data_joins",
            "output_path": str(output_path),
            "output_size_bytes": result.artifact.size_bytes,
            "left_dataset": request.left_dataset,
            "right_dataset": request.right_dataset,
            "source_hashes": request.source_hashes,
            "join_type": result.plan.join_type,
            "left_key": result.plan.left_key,
            "right_key": result.plan.right_key,
            "output_row_count": result.output_row_count,
            "matched_row_count": result.matched_row_count,
            "original_files_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        created_at=result.artifact.created_at,
    )


def _tool_call(task_id: str, request: DataJoinExportRequest, *, status: str, duration_ms: int = 0, result: DataJoinExportResponse | None = None, error: str = "") -> WorkflowToolCall:
    payload = {}
    if result is not None:
        payload = {
            "output_row_count": result.output_row_count,
            "matched_row_count": result.matched_row_count,
            "verification_passed": result.verification.passed,
        }
    return WorkflowToolCall(
        call_id=f"call_join_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_JOIN_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        tool_name=DATA_JOIN_TOOL_NAME,
        status=status,
        risk_level="low",
        permission_required=False,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=duration_ms,
        request={
            "left_dataset": request.left_dataset,
            "right_dataset": request.right_dataset,
            "left_key": request.left_key,
            "right_key": request.right_key,
            "join_type": request.join_type,
            "write_scope": "output/data_joins",
            "user_confirmed": True,
            "original_files_unchanged": True,
            "model_used": False,
            "network_used": False,
        },
        result=payload,
        error=error,
        finished_at=_now() if status in {"completed", "failed", "skipped"} else "",
    )


def _base_output(request: DataJoinExportRequest) -> dict[str, Any]:
    return {
        "left_dataset": request.left_dataset,
        "right_dataset": request.right_dataset,
        "left_key": request.left_key,
        "right_key": request.right_key,
        "join_type": request.join_type,
        "source_hashes": request.source_hashes,
        "write_scope": "output/data_joins",
        "original_files_unchanged": True,
        "model_used": False,
        "network_used": False,
    }


def _limits() -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(max_steps=1, max_tool_calls=1, max_retries_per_tool=0, tool_timeout_ms=_TOOL_TIMEOUT_MS, task_timeout_ms=_TASK_TIMEOUT_MS)


def _is_data_join_run(run: WorkflowRun | None) -> bool:
    return bool(run and any(step.step_id == DATA_JOIN_STEP_ID and step.action == DATA_JOIN_TOOL_NAME for step in run.steps))


def _result_from_export(task_id: str, run: WorkflowRun, message: str, result: DataJoinExportResponse) -> DataJoinTaskResultResponse:
    return DataJoinTaskResultResponse(task_id=task_id, status="completed", summary=run.summary, message=message, artifact=result.artifact, plan=result.plan, verification=result.verification, output_row_count=result.output_row_count, matched_row_count=result.matched_row_count, left_only_row_count=result.left_only_row_count, right_only_row_count=result.right_only_row_count, warnings=result.warnings)


def _to_artifact(artifact: WorkflowArtifact | None) -> DataJoinArtifact | None:
    if artifact is None:
        return None
    try:
        return DataJoinArtifact(name=artifact.name, uri=artifact.uri, size_bytes=int(artifact.metadata["output_size_bytes"]), created_at=artifact.created_at)
    except (KeyError, TypeError, ValueError):
        return None


def _load_plan(output: dict[str, Any]) -> DataJoinPlan | None:
    payload = output.get("plan")
    if not isinstance(payload, dict):
        return None
    try:
        return DataJoinPlan.model_validate(payload)
    except ValueError:
        return None


def _load_verification(output: dict[str, Any]) -> DataJoinVerification | None:
    payload = output.get("verification")
    if not isinstance(payload, dict):
        return None
    try:
        return DataJoinVerification.model_validate(payload)
    except ValueError:
        return None


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
