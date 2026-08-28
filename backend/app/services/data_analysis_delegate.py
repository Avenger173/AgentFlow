"""Commander 委派数据工作台只读预览的子任务生命周期。

该服务复用 D2 的确定性画像和聚合，而不是在 Commander 中再实现一份数据算法。子任务
只保存字段级画像、已验证指标、聚合数量和结论；原始行、有限预览行、绝对路径均不会进入
任务历史、父任务输出或模型上下文。任何写入 CSV/XLSX/PNG 的交付仍必须由数据工作台中
客户明确确认后的独立任务完成。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from app.database.task_repository import save_workflow_run
from app.schemas.data_agent import DataAnalysisPreviewRequest, DataAnalysisPreviewResponse
from app.schemas.events import TaskLogEvent
from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.data_analysis import DataAnalysisError, preview_data_analysis
from app.services.data_insights import enrich_data_analysis_insight


DATA_ANALYSIS_AGENT_ID = "data_agent"
DATA_ANALYSIS_PREVIEW_STEP_ID = "data_analysis_preview"
DATA_ANALYSIS_PREVIEW_TOOL_NAME = "data.preview_analysis"
_TASK_TIMEOUT_MS = 120_000
_TOOL_TIMEOUT_MS = 90_000


@dataclass(frozen=True)
class DataAnalysisDelegationResult:
    """给 Commander 返回的最小子任务回执，不携带聚合表的行内容。"""

    task_id: str
    status: str
    summary: str
    message: str
    source_sha256: str = ""
    insight_mode: str = "local"
    chart_count: int = 0
    table_count: int = 0


def create_data_analysis_preview_queued_run(
    *,
    task_id: str,
    request: DataAnalysisPreviewRequest,
) -> WorkflowRun:
    """在实际读取前创建只读子任务，保证失败也可从父任务追溯。"""

    now = _now()
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="数据分析预览已受理，等待在受控本地副本上计算。",
        steps=[
            WorkflowStepRun(
                step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
                agent=DATA_ANALYSIS_AGENT_ID,
                action=DATA_ANALYSIS_PREVIEW_TOOL_NAME,
                status="pending",
                message="已受理只读数据分析预览，尚未读取数据文件。",
                output=_base_output(request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=1),
    )
    save_workflow_run(
        run=run,
        events=[_event(task_id, 1, "task_queued", "数据分析预览已受理；仅读取当前受控数据副本。")],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_data_analysis_preview_task(
    *,
    task_id: str,
    request: DataAnalysisPreviewRequest,
) -> DataAnalysisDelegationResult:
    """执行一次可恢复审计的 D2 只读预览。

    pandas 计算在线程中执行，避免阻塞 FastAPI/Runtime 的事件循环。任务取消或超时时会把
    子任务收束为失败终态；底层计算没有写入副作用，因此即使工作线程稍后返回也不会产生文件。
    """

    started_at = _now()
    started_clock = perf_counter()
    # 数据画像与聚合可完全本地完成；只有结论层实际解析到模型路由时才会填入此列表。
    # 它不会保存原始行、API Key 或当前全局配置的猜测值。
    model_routes: list[ModelRouteAuditSnapshot] = []
    running_events = [
        _event(task_id, 1, "task_queued", "数据分析预览已受理；仅读取当前受控数据副本。"),
        _event(
            task_id,
            2,
            "task_started",
            "正在复用数据画像并执行本地白名单聚合。",
            step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
        ),
        _event(
            task_id,
            3,
            "tool_started",
            "正在生成只读分析预览；不会写入 Excel、CSV 或 PNG。",
            step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
        ),
    ]
    _save_running_run(task_id=task_id, request=request, started_at=started_at, events=running_events)

    try:
        preview = await asyncio.to_thread(preview_data_analysis, request)
        # 结论层有严格 JSON 校验和本地事实回退；它只读取有限聚合结果，绝不会看到原始行。
        preview = await enrich_data_analysis_insight(
            preview,
            goal=request.goal,
            audit_collector=model_routes,
        )
    except asyncio.CancelledError:
        _save_failed_run(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message="数据分析预览超过本次运行时间，未写入任何数据文件。",
            error_code="tool_timeout",
            events=running_events,
            model_routes=model_routes,
        )
        raise
    except DataAnalysisError as exc:
        return _save_failed_run(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message=str(exc),
            error_code="data_file_unavailable" if "未找到" in str(exc) else "data_analysis_failed",
            events=running_events,
            model_routes=model_routes,
        )
    except Exception:
        # 不把底层异常或原始数据结构写入客户可见日志，保留可行动且不泄露实现细节的说明。
        return _save_failed_run(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message="数据分析预览未能完成；源文件没有被修改，请在数据工作台检查当前文件后重试。",
            error_code="data_analysis_failed",
            events=running_events,
            model_routes=model_routes,
        )

    return _save_completed_run(
        task_id=task_id,
        request=request,
        preview=preview,
        started_at=started_at,
        duration_ms=_duration_ms(started_clock),
        events=running_events,
        model_routes=model_routes,
    )


def _save_running_run(
    *,
    task_id: str,
    request: DataAnalysisPreviewRequest,
    started_at: str,
    events: list[TaskLogEvent],
) -> None:
    """保存运行态，父任务与历史页都能区分“已受理”和“已完成”。"""

    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="正在执行只读数据分析预览。",
        steps=[
            WorkflowStepRun(
                step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
                agent=DATA_ANALYSIS_AGENT_ID,
                action=DATA_ANALYSIS_PREVIEW_TOOL_NAME,
                status="running",
                message="正在复用数据画像并执行本地白名单聚合。",
                output=_base_output(request),
            )
        ],
        limits=_limits(),
        metrics=RuntimeExecutionMetrics(started_at=started_at, step_total=1, tool_call_total=1),
    )
    save_workflow_run(run=run, events=events, plan=None, artifacts=[], tool_calls=[])


def _save_completed_run(
    *,
    task_id: str,
    request: DataAnalysisPreviewRequest,
    preview: DataAnalysisPreviewResponse,
    started_at: str,
    duration_ms: int,
    events: list[TaskLogEvent],
    model_routes: list[ModelRouteAuditSnapshot],
) -> DataAnalysisDelegationResult:
    result = _safe_result(task_id=task_id, preview=preview)
    message = str(result["reply"])
    completed_events = [
        *events,
        _event(
            task_id,
            4,
            "tool_completed",
            "本地聚合与结论已完成；未生成或修改任何数据文件。",
            step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
        ),
        _event(
            task_id,
            5,
            "task_completed",
            "数据分析预览已完成；可回到数据工作台确认图表或文件交付。",
            step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
        ),
    ]
    output = {
        **_base_output(request),
        "result": result,
        "analysis": _safe_analysis_details(preview),
    }
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary="数据工作台已完成只读分析预览，尚未生成文件交付。",
        model_routes=model_routes,
        steps=[
            WorkflowStepRun(
                step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
                agent=DATA_ANALYSIS_AGENT_ID,
                action=DATA_ANALYSIS_PREVIEW_TOOL_NAME,
                status="completed",
                message="已完成受控数据分析；原始数据未被修改。",
                output=output,
            )
        ],
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
    save_workflow_run(
        run=run,
        events=completed_events,
        plan=None,
        artifacts=[],
        tool_calls=[_tool_call(task_id=task_id, request=request, status="completed", duration_ms=duration_ms, result=result)],
    )
    return DataAnalysisDelegationResult(
        task_id=task_id,
        status="completed",
        summary=run.summary,
        message=message,
        source_sha256=str(result["source_sha256"]),
        insight_mode=str(result["insight_mode"]),
        chart_count=int(result["chart_count"]),
        table_count=int(result["table_count"]),
    )


def _save_failed_run(
    *,
    task_id: str,
    request: DataAnalysisPreviewRequest,
    started_at: str,
    duration_ms: int,
    message: str,
    error_code: str,
    events: list[TaskLogEvent],
    model_routes: list[ModelRouteAuditSnapshot],
) -> DataAnalysisDelegationResult:
    """把只读任务的失败稳定写回 SQLite，避免父任务只能看到一次瞬时异常。"""

    result = {
        "delegated_task_id": task_id,
        "agent_status": "failed",
        "stop_reason": error_code,
        "reply": message,
        "read_only": True,
    }
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="数据分析预览未完成，未生成或修改任何数据文件。",
        model_routes=model_routes,
        steps=[
            WorkflowStepRun(
                step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
                agent=DATA_ANALYSIS_AGENT_ID,
                action=DATA_ANALYSIS_PREVIEW_TOOL_NAME,
                status="failed",
                message=message,
                output={**_base_output(request), "result": result, "error": {"code": error_code}},
            )
        ],
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
    save_workflow_run(
        run=run,
        events=[
            *events,
            _event(task_id, 4, "task_failed", message, step_id=DATA_ANALYSIS_PREVIEW_STEP_ID, level="error"),
        ],
        plan=None,
        artifacts=[],
        tool_calls=[
            _tool_call(
                task_id=task_id,
                request=request,
                status="failed",
                duration_ms=duration_ms,
                result=result,
                error=message,
            )
        ],
    )
    return DataAnalysisDelegationResult(
        task_id=task_id,
        status="failed",
        summary=run.summary,
        message=message,
    )


def _safe_result(*, task_id: str, preview: DataAnalysisPreviewResponse) -> dict[str, object]:
    """构造父任务可保存的紧凑回执，绝不复制表格行或客户预览。"""

    insight = preview.insight
    reply = insight.conclusion if insight is not None else "已完成本地数据聚合，但未形成可显示结论。"
    return {
        "delegated_task_id": task_id,
        "agent_status": "completed",
        "stop_reason": "completed",
        "reply": reply[:640],
        "source_sha256": preview.dataset_profile.source_sha256,
        "insight_mode": insight.mode if insight is not None else "local",
        "insight_headline": insight.headline if insight is not None else "数据分析预览已完成",
        "chart_count": len(preview.charts),
        "table_count": len(preview.analysis_tables),
        "metric_count": len(preview.metrics),
        "read_only": True,
    }


def _safe_analysis_details(preview: DataAnalysisPreviewResponse) -> dict[str, object]:
    """在子任务内保存可复盘的 L1/L2 摘要，不保存任何原始或预览数据行。"""

    profile = preview.dataset_profile
    return {
        "dataset": {
            "name": profile.dataset.name,
            "source_sha256": profile.source_sha256,
            "row_count": profile.row_count,
            "column_count": profile.column_count,
            "selected_sheet": profile.selected_sheet,
        },
        "operations": [
            {
                "operation_id": item.operation_id,
                "operation_type": item.operation_type,
                "title": item.title,
                "source_columns": item.source_columns,
                "chart_type": item.chart_type,
            }
            for item in preview.analysis_plan.operations
        ],
        "metrics": [
            {
                "metric_id": item.metric_id,
                "name": item.name,
                "value": item.value,
                "unit": item.unit,
                "aggregation": item.aggregation,
            }
            for item in preview.metrics[:16]
        ],
        "tables": [
            {
                "table_id": item.table_id,
                "title": item.title,
                "columns": item.columns,
                "row_count": len(item.rows),
                "truncated": item.truncated,
            }
            for item in preview.analysis_tables[:6]
        ],
        "charts": [
            {"chart_id": item.chart_id, "chart_type": item.chart_type, "title": item.title}
            for item in preview.charts[:4]
        ],
        "quality_finding_count": len(preview.quality_findings),
        "warnings": preview.warnings[:12],
        "skipped_items": preview.skipped_items[:12],
        "insight": preview.insight.model_dump(mode="json") if preview.insight is not None else None,
    }


def _base_output(request: DataAnalysisPreviewRequest) -> dict[str, object]:
    """记录任务边界而不泄露原始数据或绝对位置。"""

    return {
        "dataset_name": request.dataset_name,
        "goal_length": len(request.goal.strip()),
        "cleaning_policy": request.cleaning_policy,
        "max_chart_count": request.max_chart_count,
        "read_only": True,
        "original_file_unchanged": True,
        "output_created": False,
        "raw_rows_visible": False,
        "external_data_access": False,
    }


def _tool_call(
    *,
    task_id: str,
    request: DataAnalysisPreviewRequest,
    status: str,
    duration_ms: int,
    result: dict[str, object],
    error: str = "",
) -> WorkflowToolCall:
    return WorkflowToolCall(
        call_id=f"call_data_preview_{task_id.rsplit('_', maxsplit=1)[-1]}",
        task_id=task_id,
        step_id=DATA_ANALYSIS_PREVIEW_STEP_ID,
        agent_id=DATA_ANALYSIS_AGENT_ID,
        tool_name=DATA_ANALYSIS_PREVIEW_TOOL_NAME,
        status=status,  # type: ignore[arg-type]
        risk_level="low",
        permission_required=False,
        attempt=1,
        max_attempts=1,
        timeout_ms=_TOOL_TIMEOUT_MS,
        duration_ms=duration_ms,
        failure_count=1 if status == "failed" else 0,
        request={
            "dataset_name": request.dataset_name,
            "goal_length": len(request.goal.strip()),
            "cleaning_policy": request.cleaning_policy,
            "max_chart_count": request.max_chart_count,
        },
        result=result,
        error=error,
        started_at="",
        finished_at=_now(),
    )


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
        level=level,  # type: ignore[arg-type]
        message=message,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))
