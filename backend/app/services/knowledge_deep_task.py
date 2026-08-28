"""知识库 K4 的深度任务范围、Map 执行和检查点服务。

K3 问答只读取有限检索证据；K4 则必须明确记录“本次要处理活动 generation 中的哪些章节”。
范围快照不保存正文，Map 节点只能通过稳定 parent ID 受控回读一章内容；每章完成后立即把
结构化小结写入 SQLite。Reduce、最终报告与 UI 仍留在后续小步，不能把 Map 阶段误称为完整交付。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from threading import Lock
from time import monotonic
from typing import Awaitable, Callable, Iterable

from app.database.sqlite import get_connection
from app.database.knowledge_repository import KnowledgeBaseNotFoundError, KnowledgeBaseUnavailableError
from app.database.task_repository import (
    append_workflow_event,
    get_runtime_execution_control,
    load_workflow_run,
    save_workflow_run,
    save_workflow_runtime_checkpoint,
    set_runtime_execution_control,
)
from app.agents.runner import (
    AgentDefinition,
    AgentModelUsageSummary,
    AgentRunResult,
    AgentRunner,
    ToolCallingModel,
)
from app.schemas.events import TaskLogEvent, TaskLogLevel
from app.schemas.knowledge import (
    KnowledgeContextRouteDecision,
    KnowledgeDeepMapDraft,
    KnowledgeDeepMapFinding,
    KnowledgeDeepMapResult,
    KnowledgeDeepComparisonRow,
    KnowledgeDeepReduceConflict,
    KnowledgeDeepReduceDraft,
    KnowledgeDeepReduceFinding,
    KnowledgeDeepReduceResult,
    KnowledgeDeepTaskCoverage,
    KnowledgeDeepTaskMapRunResponse,
    KnowledgeDeepTaskControlResponse,
    KnowledgeDeepTaskReportReadiness,
    KnowledgeDeepTaskReduceRunResponse,
    KnowledgeDeepTaskResultResponse,
    KnowledgeDeepTaskMapUnit,
    KnowledgeDeepTaskRequest,
    KnowledgeDeepTaskScope,
    KnowledgeSourceAnchor,
)
from app.schemas.workflow import RuntimeExecutionLimits, RuntimeExecutionMetrics, WorkflowRun, WorkflowStepRun
from app.services.knowledge_context_router import (
    KnowledgeContextBudgetError,
    enforce_knowledge_context_budget,
    plan_knowledge_context_route,
)
from app.services.model_gateway import (
    ModelGatewayError,
    ModelRuntime,
    model_route_audit_snapshot_for_stage,
    resolve_model_runtime_for_route,
)


MAX_DEEP_TASK_MAP_CONTEXT_CHARS = 8_000
MAX_DEEP_TASK_MAP_OUTPUT_TOKENS = 1_600
# 每个 Reduce 节点只消费有限、已经验证的小结。章节再多也通过多层 checkpoint 向上折叠，
# 最终节点永远不会把整库的所有摘要直接塞进一次模型调用。
MAX_DEEP_TASK_REDUCE_BATCH_UNITS = 6
MAX_DEEP_TASK_REDUCE_SUMMARY_CHARS = 900
MAX_DEEP_TASK_REDUCE_FINDING_CHARS = 360
MAX_DEEP_TASK_REDUCE_OUTPUT_TOKENS = 2_000
# 深度任务的单节点通常会花费数秒，客户不应因一次瞬态网络/限流错误反复手点“继续”。
# 这里只允许一次短退避重试；第二次仍失败即停驻并保留 checkpoint，避免把恢复变成无界扣费循环。
MAX_DEEP_TASK_MODEL_ATTEMPTS = 2
DEEP_TASK_TRANSIENT_MODEL_STOP_REASONS = frozenset({"model_timeout", "model_connection_failed"})
# K4 会连续处理多个章节。模型供应商若明确返回 RPM 上限，后续请求必须按同一账户的窗口排队，
# 不能让客户靠连续点击“继续”去撞限流。仅保留进程内脱敏哈希键和单调时钟，不写 API Key，也不把
# 限流状态伪装成持久业务数据；重启后会重新根据供应商回执学习限制。
_DEEP_TASK_RATE_LOCK = Lock()
_DEEP_TASK_RATE_LIMITS_RPM: dict[str, int] = {}
_DEEP_TASK_REQUEST_TIMES: dict[str, deque[float]] = {}
KNOWLEDGE_DEEP_TASK_AGENT_ID = "knowledge_agent"
KNOWLEDGE_DEEP_TASK_MAP_ACTION = "knowledge.deep_map"
KNOWLEDGE_DEEP_TASK_REDUCE_BATCH_ACTION = "knowledge.deep_reduce_batch"
KNOWLEDGE_DEEP_TASK_REDUCE_FINAL_ACTION = "knowledge.deep_reduce_final"
KnowledgeDeepTaskProgressCallback = Callable[[str, str, str | None, TaskLogLevel], Awaitable[None]]


class KnowledgeDeepTaskScopeError(ValueError):
    """K4 不能安全冻结整库范围时的客户可解释失败。"""


class KnowledgeDeepTaskScopeStaleError(KnowledgeDeepTaskScopeError):
    """资料库活动 generation 已变更，旧 Map checkpoint 不能继续执行。"""


class KnowledgeDeepTaskMapExecutionError(KnowledgeDeepTaskScopeError):
    """Map 阶段无法安全启动或恢复时的稳定错误。"""


def build_knowledge_deep_task_scope(request: KnowledgeDeepTaskRequest) -> KnowledgeDeepTaskScope:
    """从当前活动 generation 构造无正文的受控 Map 单元清单。

    查询只读取版本、标题路径、来源锚点和字符范围等结构元数据。全库总结冻结全部活动章节；
    资料对照只冻结客户明确选中的资料。两种情况都不因章节数量静默裁剪，也不读取正文。
    """

    with get_connection() as connection:
        base = connection.execute(
            "SELECT status, active_index_generation FROM knowledge_bases WHERE knowledge_base_id = ?",
            (request.knowledge_base_id,),
        ).fetchone()
        if base is None:
            raise KnowledgeBaseNotFoundError("未找到指定资料库。")
        if str(base["status"]) in {"deleting", "deleted"}:
            raise KnowledgeBaseUnavailableError("资料库正在删除或已删除，不能创建深度任务。")
        active_generation_number = int(base["active_index_generation"])
        if active_generation_number < 1:
            raise KnowledgeDeepTaskScopeError("资料库尚未完成活动索引，不能开始整库深度任务。")
        generation = connection.execute(
            """
            SELECT index_generation_id, generation_number
            FROM knowledge_index_generations
            WHERE knowledge_base_id = ?
                AND generation_number = ?
                AND status = 'ready'
            """,
            (request.knowledge_base_id, active_generation_number),
        ).fetchone()
        if generation is None:
            raise KnowledgeDeepTaskScopeError("当前活动索引尚未就绪，不能创建深度任务。")

        member_rows = connection.execute(
            """
            SELECT member.document_version_id, version.document_id
            FROM knowledge_generation_documents AS member
            INNER JOIN knowledge_document_versions AS version
                ON version.document_version_id = member.document_version_id
                AND version.knowledge_base_id = ?
            WHERE index_generation_id = ?
            ORDER BY ordinal ASC
            """,
            (request.knowledge_base_id, str(generation["index_generation_id"])),
        ).fetchall()
        parent_rows = connection.execute(
            """
            SELECT
                parent.parent_chunk_id,
                parent.document_id,
                parent.document_version_id,
                parent.ordinal AS parent_ordinal,
                parent.heading_path_json,
                parent.source_kind,
                parent.source_locator,
                parent.start_char,
                parent.end_char,
                document.display_name
            FROM knowledge_generation_documents AS member
            INNER JOIN knowledge_parent_chunks AS parent
                ON parent.document_version_id = member.document_version_id
                AND parent.knowledge_base_id = ?
            INNER JOIN knowledge_documents AS document
                ON document.document_id = parent.document_id
                AND document.knowledge_base_id = ?
            WHERE member.index_generation_id = ?
            ORDER BY member.ordinal ASC, parent.ordinal ASC
            """,
            (
                request.knowledge_base_id,
                request.knowledge_base_id,
                str(generation["index_generation_id"]),
            ),
        ).fetchall()

    member_versions = {str(row["document_version_id"]) for row in member_rows}
    member_document_ids = {str(row["document_id"]) for row in member_rows}
    selected_document_ids = list(request.document_ids)
    if selected_document_ids:
        unknown_document_ids = set(selected_document_ids).difference(member_document_ids)
        if unknown_document_ids:
            raise KnowledgeDeepTaskScopeError("选中的资料不属于当前活动索引，请刷新资料列表后重试。")
        parent_rows = [row for row in parent_rows if str(row["document_id"]) in set(selected_document_ids)]
        member_versions = {
            str(row["document_version_id"])
            for row in member_rows
            if str(row["document_id"]) in set(selected_document_ids)
        }
    parent_versions = {str(row["document_version_id"]) for row in parent_rows}
    missing_versions = member_versions.difference(parent_versions)
    if not member_versions or missing_versions:
        raise KnowledgeDeepTaskScopeError(
            "当前活动资料存在尚未形成可处理章节的文档，未创建不完整的整库任务。"
        )
    selected_parent_rows = parent_rows
    scope_mode = "selected_documents" if selected_document_ids else "complete"
    scope_notice = (
        f"本次资料对照已冻结客户选择的 {len(selected_document_ids)} 份资料，共 {len(selected_parent_rows)} 个章节。"
        if selected_document_ids
        else f"本次深度总结已冻结当前活动索引的全部 {len(selected_parent_rows)} 个章节。"
    )
    map_units: list[KnowledgeDeepTaskMapUnit] = []
    for row in selected_parent_rows:
        try:
            heading_path = json.loads(str(row["heading_path_json"]))
        except json.JSONDecodeError as exc:
            raise KnowledgeDeepTaskScopeError("活动资料的章节结构无效，未创建深度任务。") from exc
        if not isinstance(heading_path, list) or not all(isinstance(item, str) for item in heading_path):
            raise KnowledgeDeepTaskScopeError("活动资料的章节结构无效，未创建深度任务。")
        parent_chunk_id = str(row["parent_chunk_id"])
        map_unit_id = "kb_map_" + sha256(
            f"{generation['index_generation_id']}:{parent_chunk_id}".encode("utf-8")
        ).hexdigest()[:16]
        start_char = int(row["start_char"])
        end_char = int(row["end_char"])
        map_units.append(
            KnowledgeDeepTaskMapUnit(
                map_unit_id=map_unit_id,
                parent_chunk_id=parent_chunk_id,
                document_id=str(row["document_id"]),
                document_version_id=str(row["document_version_id"]),
                document_name=str(row["display_name"]),
                parent_ordinal=int(row["parent_ordinal"]),
                source=KnowledgeSourceAnchor(
                    document_id=str(row["document_id"]),
                    document_version_id=str(row["document_version_id"]),
                    source_kind=str(row["source_kind"]),
                    source_locator=str(row["source_locator"]),
                    start_char=start_char,
                    end_char=end_char,
                    heading_path=heading_path,
                ),
                heading_path=heading_path,
                character_count=end_char - start_char,
            )
        )
    return KnowledgeDeepTaskScope(
        knowledge_base_id=request.knowledge_base_id,
        task_kind=request.task_kind,
        task_goal=request.task_goal,
        index_generation_id=str(generation["index_generation_id"]),
        active_index_generation=int(generation["generation_number"]),
        selected_document_ids=selected_document_ids,
        covered_document_count=len({str(row["document_version_id"]) for row in selected_parent_rows}),
        available_document_count=len(member_versions),
        available_map_count=len(parent_rows),
        scope_mode=scope_mode,
        scope_notice=scope_notice,
        map_units=map_units,
    )


def verify_knowledge_deep_task_scope(scope: KnowledgeDeepTaskScope) -> KnowledgeDeepTaskScope:
    """确认恢复前的活动 generation 与章节集合没有漂移。

    K4 以后会在每一个 Map/Reduce 安全边界调用此函数。资料更新后不允许把旧 checkpoint 接到
    新 generation 上继续跑，必须重新计划；这比“自动补进新章节”更诚实，也避免报告的来源
    覆盖范围无法复盘。
    """

    current_scope = build_knowledge_deep_task_scope(
        KnowledgeDeepTaskRequest(
            knowledge_base_id=scope.knowledge_base_id,
            task_kind=scope.task_kind,
            task_goal=scope.task_goal,
            document_ids=scope.selected_document_ids,
        )
    )
    expected_units = [unit.map_unit_id for unit in scope.map_units]
    current_units = [unit.map_unit_id for unit in current_scope.map_units]
    if (
        current_scope.index_generation_id != scope.index_generation_id
        or current_scope.active_index_generation != scope.active_index_generation
        or current_units != expected_units
    ):
        raise KnowledgeDeepTaskScopeStaleError(
            "资料库活动版本已更新，原深度任务范围已失效；请基于当前资料重新创建任务。"
        )
    return current_scope


def create_knowledge_deep_task_map_queued_run(*, task_id: str, scope: KnowledgeDeepTaskScope) -> WorkflowRun:
    """创建仅代表 K4 Map 阶段的后台任务，并立即留下可恢复的初始快照。"""

    _validate_map_execution_scope(scope)
    started_at = _now()
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="知识库深度任务已受理，正在等待逐章节 Map 分析。",
        steps=[_new_map_step(scope, unit) for unit in scope.map_units],
        limits=_map_execution_limits(len(scope.map_units)),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            step_total=len(scope.map_units),
        ),
    )
    save_workflow_run(
        run=run,
        events=[
            TaskLogEvent(
                task_id=task_id,
                sequence=1,
                event="knowledge_deep_map_queued",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                message=scope.scope_notice or f"已冻结 {len(scope.map_units)} 个章节，等待受控 Map 分析。",
            )
        ],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


def get_knowledge_deep_task_scope(task_id: str) -> KnowledgeDeepTaskScope | None:
    """从首个 Map checkpoint 恢复冻结 scope，不回扫资料库或依赖进程内缓存。"""

    run = load_workflow_run(task_id)
    if run is None:
        return None
    scope_step = next(
        (
            step
            for step in run.steps
            if step.agent == KNOWLEDGE_DEEP_TASK_AGENT_ID
            and step.action == KNOWLEDGE_DEEP_TASK_MAP_ACTION
            and isinstance(step.output.get("deep_task_scope"), dict)
        ),
        None,
    )
    if scope_step is None:
        return None
    try:
        return KnowledgeDeepTaskScope.model_validate(scope_step.output["deep_task_scope"])
    except ValueError:
        # 历史任务若不含首期 K4 scope 契约，不能假装可以安全恢复；调用方会返回明确的不存在/不支持。
        return None


def get_knowledge_deep_task_result(task_id: str) -> KnowledgeDeepTaskResultResponse | None:
    """从统一任务历史恢复 K4 状态、部分 Map 结果或最终 Reduce checkpoint。"""

    run = load_workflow_run(task_id)
    scope = get_knowledge_deep_task_scope(task_id)
    if run is None or scope is None:
        return None
    final_step = next(
        (
            step
            for step in run.steps
            if step.action == KNOWLEDGE_DEEP_TASK_REDUCE_FINAL_ACTION
            and step.agent == KNOWLEDGE_DEEP_TASK_AGENT_ID
        ),
        None,
    )
    raw_result = final_step.output.get("reduce_result") if final_step is not None else None
    result: KnowledgeDeepReduceResult | None = None
    result_validation_warning = ""
    if isinstance(raw_result, dict):
        try:
            result = KnowledgeDeepReduceResult.model_validate(raw_result)
        except ValueError:
            # 结果接口也要能读取早期或异常中断的任务快照。最终 Reduce 契约损坏时不把原始
            # 校验细节暴露给客户，而是降级为不可正式导出的部分结果状态。
            result_validation_warning = "最终汇总检查点无法通过当前输出契约校验，暂不能导出正式报告。"
    coverage = _deep_task_coverage(run, scope)
    if result_validation_warning:
        coverage.warnings = [*coverage.warnings, result_validation_warning][:8]
    return KnowledgeDeepTaskResultResponse(
        task_id=task_id,
        status=run.status,
        summary=run.summary,
        scope=scope,
        result=result,
        coverage=coverage,
        report_readiness=_deep_task_report_readiness(run, coverage, result),
    )


def _deep_task_coverage(run: WorkflowRun, scope: KnowledgeDeepTaskScope) -> KnowledgeDeepTaskCoverage:
    """从 checkpoint 构造客户可读的完成范围，不重新读取父块正文或资料库。"""

    map_steps = {step.step_id: step for step in _map_steps_for_scope(run, scope)}
    completed_map_unit_ids: list[str] = []
    failed_map_unit_ids: list[str] = []
    cancelled_map_unit_ids: list[str] = []
    pending_map_unit_ids: list[str] = []
    completed_map_results: list[KnowledgeDeepMapResult] = []
    warnings: list[str] = []

    for unit in scope.map_units:
        step = map_steps.get(unit.map_unit_id)
        step_status = step.status if step is not None else "pending"
        if step_status == "completed":
            completed_map_unit_ids.append(unit.map_unit_id)
            raw_map_result = step.output.get("map_result") if step is not None else None
            if isinstance(raw_map_result, dict):
                try:
                    completed_map_results.append(KnowledgeDeepMapResult.model_validate(raw_map_result))
                except ValueError:
                    # 历史快照可能来自旧版本或被异常中断；覆盖计数仍要如实保留，但不把无法
                    # 通过当前契约校验的内容送到客户预览或后续报告。
                    warnings.append(f"章节 {unit.parent_ordinal} 的历史小结无法验证，未纳入部分结果预览。")
            else:
                warnings.append(f"章节 {unit.parent_ordinal} 已标记完成但缺少可验证小结，未纳入部分结果预览。")
        elif step_status == "failed":
            failed_map_unit_ids.append(unit.map_unit_id)
            failure_message = str(step.output.get("failure_message", "")).strip()
            if failure_message:
                warnings.append(f"章节 {unit.parent_ordinal}：{failure_message}")
        elif step_status == "cancelled":
            cancelled_map_unit_ids.append(unit.map_unit_id)
        else:
            pending_map_unit_ids.append(unit.map_unit_id)

    # 分层 Reduce 的节点数取决于树高，不能继续沿用“最多四批加一次最终合并”的旧公式。
    total_reduce_count = len(_reduce_plan(scope))
    reduce_steps = {step.step_id: step for step in _reduce_steps(run)}
    expected_reduce_step_ids = [node.step_id for node in _reduce_plan(scope)]
    reduce_counts = {"completed": 0, "failed": 0, "cancelled": 0, "pending": 0}
    for step_id in expected_reduce_step_ids:
        step = reduce_steps.get(step_id)
        step_status = step.status if step is not None else "pending"
        if step_status in {"completed", "failed", "cancelled"}:
            reduce_counts[step_status] += 1
            if step_status == "failed" and step is not None:
                failure_message = str(step.output.get("failure_message", "")).strip()
                if failure_message:
                    warnings.append(f"Reduce 节点 {step_id}：{failure_message}")
        else:
            reduce_counts["pending"] += 1

    if run.status == "completed" and len(completed_map_unit_ids) == len(scope.map_units) and (
        reduce_counts["completed"] == total_reduce_count
    ):
        state = "complete"
    elif completed_map_unit_ids:
        state = "partial"
    elif run.status in {"failed", "cancelled"}:
        state = "unavailable"
    else:
        state = "in_progress"

    if failed_map_unit_ids:
        warnings.append(f"{len(failed_map_unit_ids)} 个章节未完成，未纳入当前结果范围。")
    if cancelled_map_unit_ids:
        warnings.append(f"{len(cancelled_map_unit_ids)} 个章节因用户取消未继续分析。")
    if pending_map_unit_ids and run.status in {"paused", "blocked"}:
        warnings.append(f"{len(pending_map_unit_ids)} 个章节仍等待客户继续或重新创建任务。")
    if reduce_counts["failed"]:
        warnings.append(f"{reduce_counts['failed']} 个 Reduce 节点未完成，已保留此前汇总检查点。")

    return KnowledgeDeepTaskCoverage(
        state=state,
        total_map_count=len(scope.map_units),
        completed_map_unit_ids=completed_map_unit_ids,
        failed_map_unit_ids=failed_map_unit_ids,
        cancelled_map_unit_ids=cancelled_map_unit_ids,
        pending_map_unit_ids=pending_map_unit_ids,
        completed_map_results=completed_map_results,
        total_reduce_count=total_reduce_count,
        completed_reduce_count=reduce_counts["completed"],
        failed_reduce_count=reduce_counts["failed"],
        cancelled_reduce_count=reduce_counts["cancelled"],
        pending_reduce_count=reduce_counts["pending"],
        warnings=warnings[:8],
    )


def _deep_task_report_readiness(
    run: WorkflowRun,
    coverage: KnowledgeDeepTaskCoverage,
    result: KnowledgeDeepReduceResult | None,
) -> KnowledgeDeepTaskReportReadiness:
    """给后续报告 UI/导出使用的单一资格判断；部分结果不伪装成完整正式报告。"""

    readiness_warnings = list(coverage.warnings)
    missing_map_unit_ids = [
        *coverage.failed_map_unit_ids,
        *coverage.cancelled_map_unit_ids,
        *coverage.pending_map_unit_ids,
    ]
    expected_map_unit_ids = set(coverage.completed_map_unit_ids)
    completed_map_results_are_complete = {
        item.map_unit_id for item in coverage.completed_map_results
    } == expected_map_unit_ids
    reduce_coverage_is_complete = result is not None and set(result.covered_map_unit_ids) == expected_map_unit_ids
    if run.status == "completed" and coverage.state == "complete" and completed_map_results_are_complete and reduce_coverage_is_complete:
        return KnowledgeDeepTaskReportReadiness(
            state="ready_for_export",
            can_export=True,
            message="所有冻结章节与 Reduce 节点均已完成；后续客户确认后才可导出正式报告。",
            missing_map_unit_ids=[],
            warnings=readiness_warnings,
        )
    if coverage.state == "complete" and not completed_map_results_are_complete:
        readiness_warnings.append("部分已完成章节的小结无法通过当前契约校验，暂不能导出正式报告。")
    if coverage.state == "complete" and result is not None and not reduce_coverage_is_complete:
        readiness_warnings.append("最终汇总的章节覆盖与冻结范围不一致，暂不能导出正式报告。")
    if coverage.completed_map_unit_ids:
        return KnowledgeDeepTaskReportReadiness(
            state="partial_preview",
            can_export=False,
            message="当前可查看已完成章节的小结，但范围尚不完整，不能导出为正式深度报告。",
            missing_map_unit_ids=missing_map_unit_ids,
            warnings=readiness_warnings[:8],
        )
    return KnowledgeDeepTaskReportReadiness(
        state="not_ready",
        can_export=False,
        message="当前没有可验证的章节小结，暂不能生成部分结果或正式报告。",
        missing_map_unit_ids=missing_map_unit_ids,
        warnings=readiness_warnings[:8],
    )


def request_knowledge_deep_task_pause(task_id: str) -> KnowledgeDeepTaskControlResponse | None:
    """登记 K4 暂停意图，并只在没有模型回合运行时立即写入暂停检查点。"""

    run = load_workflow_run(task_id)
    scope = get_knowledge_deep_task_scope(task_id)
    if run is None or scope is None:
        return None
    if run.status in {"completed", "failed", "cancelled", "blocked"}:
        return _deep_task_control_response(
            task_id,
            "pause",
            accepted=False,
            status=run.status,
            message="当前深度任务没有可暂停的执行链路。",
        )
    if run.status == "paused":
        return _deep_task_control_response(
            task_id,
            "pause",
            accepted=False,
            status="paused",
            message="深度任务已在安全检查点暂停；可继续或取消。",
        )

    set_runtime_execution_control(task_id=task_id, pause_requested=True)
    if run.status == "running":
        append_workflow_event(
            task_id=task_id,
            event_name="task_pause_requested",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            message="已收到暂停请求；当前模型回合结束并写入检查点后将暂停。",
            level="warning",
        )
        return _deep_task_control_response(
            task_id,
            "pause",
            accepted=True,
            status="running",
            message="暂停请求已记录，将在当前模型回合结束后生效。",
        )

    paused_run = _with_map_run_state(
        run,
        status="paused",
        summary="深度任务已在启动前暂停，尚未读取新的章节正文。",
        finished=False,
    )
    _save_map_checkpoint(paused_run)
    append_workflow_event(
        task_id=task_id,
        event_name="task_paused",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        message="深度任务已在安全检查点暂停；冻结范围和既有检查点均已保留。",
        level="warning",
    )
    return _deep_task_control_response(
        task_id,
        "pause",
        accepted=True,
        status="paused",
        message="深度任务已暂停，尚未开始新的模型回合。",
    )


def request_knowledge_deep_task_cancel(task_id: str) -> KnowledgeDeepTaskControlResponse | None:
    """登记 K4 取消意图；运行中任务只在当前模型回合安全返回后停止。"""

    run = load_workflow_run(task_id)
    scope = get_knowledge_deep_task_scope(task_id)
    if run is None or scope is None:
        return None
    if run.status in {"completed", "failed", "cancelled"}:
        return _deep_task_control_response(
            task_id,
            "cancel",
            accepted=False,
            status=run.status,
            message="深度任务已经结束，不能再次取消。",
        )

    set_runtime_execution_control(task_id=task_id, pause_requested=False, cancel_requested=True)
    if run.status == "running":
        append_workflow_event(
            task_id=task_id,
            event_name="task_cancel_requested",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            message="已收到取消请求；当前模型回合结束并保存检查点后不会继续后续章节。",
            level="warning",
        )
        return _deep_task_control_response(
            task_id,
            "cancel",
            accepted=True,
            status="running",
            message="取消请求已记录，将在当前模型回合结束后生效。",
        )

    cancelled_run = _cancel_deep_task_run(run)
    _save_map_checkpoint(cancelled_run)
    append_workflow_event(
        task_id=task_id,
        event_name="task_cancelled",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        message="深度任务已取消；不会读取新的章节、调用模型或创建正式报告。",
        level="warning",
    )
    return _deep_task_control_response(
        task_id,
        "cancel",
        accepted=True,
        status="cancelled",
        message="深度任务已取消，已有检查点仅保留用于历史复盘。",
    )


def resume_knowledge_deep_task(task_id: str) -> tuple[KnowledgeDeepTaskControlResponse, KnowledgeDeepTaskScope] | None:
    """恢复已暂停或已阻塞的 K4 任务，保持原 task_id、scope 和已完成检查点。"""

    run = load_workflow_run(task_id)
    scope = get_knowledge_deep_task_scope(task_id)
    if run is None or scope is None:
        return None
    if run.status not in {"paused", "blocked"}:
        return (
            _deep_task_control_response(
                task_id,
                "resume",
                accepted=False,
                status=run.status,
                message="只有已暂停或已阻塞的深度任务可以继续。",
            ),
            scope,
        )

    # 恢复不验证或扩展 scope；Runtime 在读取正文前仍会检查活动 generation。这样资料更新后
    # 会回到可解释 blocked，而不是把旧检查点静默接到新索引。
    set_runtime_execution_control(task_id=task_id, pause_requested=False, cancel_requested=False)
    resumed_run = _with_map_run_state(
        run,
        status="pending",
        summary="深度任务已收到继续请求，正在从已保存检查点恢复。",
        finished=False,
    )
    _save_map_checkpoint(resumed_run)
    append_workflow_event(
        task_id=task_id,
        event_name="task_resumed",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        message="深度任务将从已保存检查点继续；已完成 Map/Reduce 节点不会重复调用模型。",
    )
    return (
        _deep_task_control_response(
            task_id,
            "resume",
            accepted=True,
            status="pending",
            message="继续请求已受理，正在从已有检查点恢复。",
        ),
        scope,
    )


def mark_knowledge_deep_task_unexpected_failure(task_id: str) -> KnowledgeDeepTaskResultResponse | None:
    """把后台边界以外的异常收束为可恢复的失败任务，不泄漏堆栈或原始材料。"""

    run = load_workflow_run(task_id)
    if run is None or run.status in {"completed", "cancelled"}:
        return get_knowledge_deep_task_result(task_id)
    active_step = next((step for step in run.steps if step.status == "running"), None)
    if active_step is not None:
        failed_step = active_step.model_copy(
            update={
                "status": "failed",
                "message": "深度任务在运行时异常结束，已保留此前检查点；请稍后重试。",
                "output": {**active_step.output, "stop_reason": "unexpected_runtime_error"},
            }
        )
        run = _replace_map_step(
            run,
            failed_step,
            status="failed",
            summary="知识库深度任务异常结束，已保留已有检查点供任务历史复盘。",
        )
    else:
        run = _with_map_run_state(
            run,
            status="failed",
            summary="知识库深度任务异常结束，未读取新的资料正文。",
            finished=True,
        )
    _save_map_checkpoint(run)
    append_workflow_event(
        task_id=task_id,
        event_name="knowledge_deep_task_failed",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        step_id=active_step.step_id if active_step is not None else None,
        message="深度任务异常结束，已保存可见失败状态；资料和原始文件未被修改。",
        level="error",
    )
    return get_knowledge_deep_task_result(task_id)


async def _apply_deep_task_control_at_safe_boundary(
    *,
    run: WorkflowRun,
    task_id: str,
    progress_callback: KnowledgeDeepTaskProgressCallback | None,
) -> WorkflowRun | None:
    """只在模型回合之间消费协作控制信号，避免粗暴取消正在执行的 Provider 请求。"""

    control = get_runtime_execution_control(task_id)
    if control is None:
        return None
    if control.cancel_requested:
        cancelled_run = _cancel_deep_task_run(run)
        _save_map_checkpoint(cancelled_run)
        append_workflow_event(
            task_id=task_id,
            event_name="task_cancelled",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            message="已在模型回合安全返回后取消深度任务；后续节点不会继续执行。",
            level="warning",
        )
        await _notify_progress(
            progress_callback,
            "task_cancelled",
            "深度任务已在安全检查点取消，后续章节不会继续分析。",
            None,
            "warning",
        )
        return cancelled_run
    if control.pause_requested:
        paused_run = _with_map_run_state(
            run,
            status="paused",
            summary="深度任务已在安全检查点暂停，可继续或取消。",
            finished=False,
        )
        _save_map_checkpoint(paused_run)
        append_workflow_event(
            task_id=task_id,
            event_name="task_paused",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            message="已在模型回合安全返回后暂停深度任务；完成节点不会重复执行。",
            level="warning",
        )
        await _notify_progress(
            progress_callback,
            "task_paused",
            "深度任务已在安全检查点暂停；可继续或取消。",
            None,
            "warning",
        )
        return paused_run
    return None


def _cancel_deep_task_run(run: WorkflowRun) -> WorkflowRun:
    """保留已完成/失败节点，把尚未安全提交的节点明确标记为取消。"""

    cancelled_steps = [
        step.model_copy(
            update={
                "status": "cancelled",
                "message": "深度任务已被用户取消，该节点不会继续执行。",
                "output": {**step.output, "cancelled": True},
            }
        )
        if step.status in {"pending", "running"}
        else step
        for step in run.steps
    ]
    cancelled = run.model_copy(update={"steps": cancelled_steps})
    return _with_map_run_state(
        cancelled,
        status="cancelled",
        summary="深度任务已按用户请求取消，未继续执行后续 Map/Reduce 节点。",
        finished=True,
    )


def _deep_task_control_response(
    task_id: str,
    action: str,
    *,
    accepted: bool,
    status: str,
    message: str,
) -> KnowledgeDeepTaskControlResponse:
    """集中构造控制回执，避免 API 把 WorkflowRun 的大步骤输出返回给客户。"""

    return KnowledgeDeepTaskControlResponse(
        task_id=task_id,
        action=action,  # type: ignore[arg-type]
        accepted=accepted,
        status=status,  # type: ignore[arg-type]
        message=message,
    )


async def run_knowledge_deep_task(
    *,
    task_id: str,
    scope: KnowledgeDeepTaskScope,
    model: ToolCallingModel | None = None,
    progress_callback: KnowledgeDeepTaskProgressCallback | None = None,
) -> KnowledgeDeepTaskResultResponse:
    """执行 K4 的 Map 后 Reduce 主链；任何非完成状态都保留同一 task 的 checkpoint。"""

    if model is None:
        try:
            # 同一深度任务的 Map/Reduce 必须使用同一份已解析 Runtime。若客户在 Map 期间修改模型
            # 配置，不能让后半段悄悄换到另一个 Provider 或输出预算；没有可用配置时仍交给 Map
            # 入口写入可解释 blocked checkpoint。
            model = _knowledge_deep_reduce_model_with_output_budget(
                resolve_model_runtime_for_route("knowledge_deep_analysis").runtime
            )
        except ModelGatewayError:
            model = None

    map_response = await run_knowledge_deep_task_map(
        task_id=task_id,
        scope=scope,
        model=model,
        progress_callback=progress_callback,
    )
    if map_response.status != "completed":
        result = get_knowledge_deep_task_result(task_id)
        if result is not None:
            return result
        raise KnowledgeDeepTaskMapExecutionError("深度任务 Map 未完成且检查点不可恢复。")

    run_after_map = load_workflow_run(task_id)
    if run_after_map is None:
        raise KnowledgeDeepTaskMapExecutionError("深度任务 Map 已完成但检查点不可恢复。")
    controlled_run = await _apply_deep_task_control_at_safe_boundary(
        run=run_after_map,
        task_id=task_id,
        progress_callback=progress_callback,
    )
    if controlled_run is not None:
        result = get_knowledge_deep_task_result(task_id)
        if result is not None:
            return result
        raise KnowledgeDeepTaskMapExecutionError("深度任务控制状态无法从检查点恢复。")

    await run_knowledge_deep_task_reduce(
        task_id=task_id,
        scope=scope,
        model=model,
        progress_callback=progress_callback,
    )
    result = get_knowledge_deep_task_result(task_id)
    if result is None:
        raise KnowledgeDeepTaskMapExecutionError("深度任务 Reduce 已结束但结果检查点不可恢复。")
    return result


async def run_knowledge_deep_task_map(
    *,
    task_id: str,
    scope: KnowledgeDeepTaskScope,
    model: ToolCallingModel | None = None,
    progress_callback: KnowledgeDeepTaskProgressCallback | None = None,
) -> KnowledgeDeepTaskMapRunResponse:
    """执行或恢复 K4 的有限 Map 阶段。

    每个安全边界都先确认活动 generation 未变化；已完成章节直接从 SQLite checkpoint 复用，
    不再次调用模型。模型失败会保留之前已完成的章节并停止，避免在同一故障下继续消耗额度。
    """

    _validate_map_execution_scope(scope)
    run = load_workflow_run(task_id)
    if run is None:
        run = create_knowledge_deep_task_map_queued_run(task_id=task_id, scope=scope)
    _validate_map_run_shape(run, scope)
    if not _scope_is_current(scope):
        blocked = _with_map_run_state(
            run,
            status="blocked",
            summary="资料库活动索引已变化，旧深度任务范围不能继续；请重新创建任务。",
            finished=False,
        )
        _save_map_checkpoint(blocked)
        append_workflow_event(
            task_id=task_id,
            event_name="knowledge_deep_map_scope_stale",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            step_id=scope.map_units[0].map_unit_id,
            message="活动资料已更新，未读取旧范围内的章节正文。",
            level="warning",
        )
        return _map_run_response(blocked)

    if run.status in {"paused", "cancelled", "failed"}:
        # pause/cancel/resume 都必须由显式控制 API 驱动。这里不因为后台协程再次进入就擅自
        # 恢复用户已暂停的任务，也不把终态重新变成可执行状态。
        return _map_run_response(run)

    controlled_run = await _apply_deep_task_control_at_safe_boundary(
        run=run,
        task_id=task_id,
        progress_callback=progress_callback,
    )
    if controlled_run is not None:
        return _map_run_response(controlled_run)

    # 已经完整 Map 的任务不会重跑章节，但整体任务仍可能正从 Reduce 的失败检查点恢复。
    # resume API 会先把 WorkflowRun 写为 pending；若这里把 pending 原样回传，外层会误判
    # “Map 尚未完成”并直接退出，导致客户看到 24/24 后永远停在等待执行。
    if all(step.status == "completed" for step in _map_steps_for_scope(run, scope)):
        if run.status != "running":
            run = _with_map_run_state(
                run,
                status="running",
                summary="Map 章节检查点已完整，正在从已保存状态进入 Reduce 汇总。",
                finished=False,
            )
            _save_map_checkpoint(run)
            append_workflow_event(
                task_id=task_id,
                event_name="knowledge_deep_map_reused_for_reduce",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                message="所有 Map 章节已完成，本次恢复直接进入未完成的 Reduce 节点。",
            )
            await _notify_progress(
                progress_callback,
                "knowledge_deep_map_reused_for_reduce",
                "章节分析已完成，正在恢复后续汇总，不会重新读取章节。",
                None,
                "info",
            )
        return _map_run_response(run, phase_status="completed")

    # 对同一 task 的再次调用就是显式恢复动作。只把上次失败的单个节点退回 pending，已经完成
    # 的节点保持不可变，既避免重复扣费，也不给失败节点偷偷无限重试的机会。
    if any(step.status == "failed" for step in run.steps):
        run = _reset_failed_map_steps(run)
        _save_map_checkpoint(run)
        append_workflow_event(
            task_id=task_id,
            event_name="knowledge_deep_map_resumed",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            message="已按客户显式恢复请求重试上次失败章节；已完成章节不会重新调用模型。",
        )

    if model is None:
        try:
            model = _knowledge_deep_map_model_with_output_budget(
                resolve_model_runtime_for_route("knowledge_deep_analysis").runtime
            )
        except ModelGatewayError:
            blocked = _with_map_run_state(
                run,
                status="blocked",
                summary="知识库深度任务等待可用模型配置，已完成的章节检查点将被保留。",
                finished=False,
            )
            _save_map_checkpoint(blocked)
            append_workflow_event(
                task_id=task_id,
                event_name="knowledge_deep_map_blocked",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                message="当前没有可用的深度分析模型配置；未读取新的章节正文。",
                level="warning",
            )
            await _notify_progress(
                progress_callback,
                "knowledge_deep_map_blocked",
                "当前没有可用模型配置，已保留完成章节，可在配置后继续。",
                None,
                "warning",
            )
            return _map_run_response(blocked)

    # Map 与 Reduce 都会复用同一任务快照，但属于不同实际模型阶段。这里在首次实际解析到
    # Runtime 后立即写入脱敏阶段事实；恢复时若快照已存在则保持不可变，不重新猜当前配置。
    run = _with_model_route_audit(run, model=model, stage="knowledge_deep_map")
    run = _with_map_run_state(
        run,
        status="running",
        summary="知识库深度任务正在逐章节分析，已完成章节会立即保存。",
        finished=False,
    )
    _save_map_checkpoint(run)
    await _notify_progress(
        progress_callback,
        "knowledge_deep_map_started",
        "正在按冻结的章节范围执行 Map 分析。",
        None,
        "info",
    )

    for unit in scope.map_units:
        current_step = _find_map_step(run, unit.map_unit_id)
        if current_step.status == "completed":
            continue
        controlled_run = await _apply_deep_task_control_at_safe_boundary(
            run=run,
            task_id=task_id,
            progress_callback=progress_callback,
        )
        if controlled_run is not None:
            return _map_run_response(controlled_run)
        try:
            parent_content = _load_active_map_parent_content(scope, unit)
        except KnowledgeDeepTaskScopeError:
            stale_run = _with_map_run_state(
                run,
                status="blocked",
                summary="资料库活动索引已变化，旧 Map 范围不能继续；请重新创建深度任务。",
                finished=False,
            )
            _save_map_checkpoint(stale_run)
            append_workflow_event(
                task_id=task_id,
                event_name="knowledge_deep_map_scope_stale",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                step_id=unit.map_unit_id,
                message="活动资料已更新，未把旧章节检查点接到新索引。",
                level="warning",
            )
            return _map_run_response(stale_run)

        definition = AgentDefinition(
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            system_prompt=_map_system_prompt(scope, unit),
            tools=(),
            output_model=KnowledgeDeepMapDraft,
            max_turns=2,
            max_tool_calls=0,
            max_same_tool_failure=0,
            max_output_repair_attempts=1,
        )
        user_message = _map_user_message(scope, unit, parent_content)
        context_route = plan_knowledge_context_route(
            stage="deep_map",
            system_prompt=definition.system_prompt,
            user_message=user_message,
            model=model,
        )
        try:
            enforce_knowledge_context_budget(context_route)
        except KnowledgeContextBudgetError as exc:
            # Map 的单章读取上限是 K4 正确性和成本边界的一部分。即使当前 Provider 已声明长窗口，
            # 也不能把超预算章节改为整库直读；停驻后由客户缩小材料或后续方案明确处理。
            failure_message = str(exc)
            failed_step = current_step.model_copy(
                update={
                    "status": "failed",
                    "message": failure_message,
                    "output": _map_step_output(
                        unit,
                        task_scope=_task_scope_for_map_unit(scope, unit),
                        context_route=context_route,
                        stop_reason="context_budget_exceeded",
                        failure_message=failure_message,
                    ),
                }
            )
            run = _replace_map_step(
                run,
                failed_step,
                status="blocked",
                summary="知识库深度任务的单章上下文超过预算，未改走整库长上下文。",
            )
            _save_map_checkpoint(run)
            append_workflow_event(
                task_id=task_id,
                event_name="knowledge_deep_map_context_budget_exceeded",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                step_id=unit.map_unit_id,
                message="当前章节超过受控上下文预算，未向模型发送正文。",
                level="warning",
            )
            return _map_run_response(run)

        running_step = current_step.model_copy(
            update={
                "status": "running",
                "message": "正在仅分析当前章节，完成后立即保存检查点。",
                "output": _map_step_output(
                    unit,
                    task_scope=_task_scope_for_map_unit(scope, unit),
                    context_route=context_route,
                ),
            }
        )
        run = _replace_map_step(run, running_step, status="running", summary="知识库深度任务正在逐章节分析。")
        _save_map_checkpoint(run)
        await _notify_progress(
            progress_callback,
            "knowledge_deep_map_unit_started",
            f"正在分析章节 {unit.parent_ordinal}：{unit.document_name}。",
            unit.map_unit_id,
            "info",
        )

        result, model_usage = await _run_deep_task_model_turn(
            definition=definition,
            model=model,
            user_message=user_message,
            task_id=task_id,
            progress_callback=progress_callback,
            step_id=unit.map_unit_id,
            stage_label="Map 章节分析",
        )
        run = _with_provider_usage_metrics(run, model=model, usage=model_usage)
        if result.status != "completed" or not isinstance(result.output, KnowledgeDeepMapDraft):
            failure_message = _deep_task_model_failure_message(result.stop_reason, result.message)
            failed_step = running_step.model_copy(
                update={
                    "status": "failed",
                    "message": failure_message,
                    "output": _map_step_output(
                        unit,
                        task_scope=_task_scope_for_map_unit(scope, unit),
                        context_route=context_route,
                        stop_reason=result.stop_reason,
                        failure_message=failure_message,
                    ),
                }
            )
            run = _replace_map_step(
                run,
                failed_step,
                status="blocked",
                summary="知识库深度任务因一个章节的模型输出失败而暂停，未继续消耗额度。",
            )
            _save_map_checkpoint(run)
            append_workflow_event(
                task_id=task_id,
                event_name="knowledge_deep_map_unit_failed",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                step_id=unit.map_unit_id,
                message=f"当前章节未完成：{failure_message}；之前完成的章节检查点已保留。",
                level="error",
            )
            await _notify_progress(
                progress_callback,
                "knowledge_deep_map_unit_failed",
                f"一个章节未完成：{failure_message}；已保留进度并停止自动继续。",
                unit.map_unit_id,
                "error",
            )
            return _map_run_response(run)

        map_result = _map_result_from_draft(unit, result.output)

        # 模型返回后再次只读核对当前章节仍属于冻结 generation，避免把旧正文的结论写入新索引。
        try:
            _load_active_map_parent_content(scope, unit, include_content=False)
        except KnowledgeDeepTaskScopeError:
            stale_run = _with_map_run_state(
                run,
                status="blocked",
                summary="资料库在章节分析期间更新，模型结果未写入新索引对应的任务记录。",
                finished=False,
            )
            _save_map_checkpoint(stale_run)
            return _map_run_response(stale_run)

        completed_step = current_step.model_copy(
            update={
                "status": "completed",
                "message": "章节分析完成，结构化小结已写入可恢复检查点。",
                "output": _map_step_output(
                    unit,
                    task_scope=_task_scope_for_map_unit(scope, unit),
                    context_route=context_route,
                    map_result=map_result,
                ),
            }
        )
        run = _replace_map_step(
            run,
            completed_step,
            status="running",
            summary="知识库深度任务正在逐章节分析，已完成章节已保存。",
        )
        _save_map_checkpoint(run)
        append_workflow_event(
            task_id=task_id,
            event_name="knowledge_deep_map_unit_completed",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            step_id=unit.map_unit_id,
            message=f"章节 {unit.parent_ordinal} 的结构化小结已保存。",
        )
        await _notify_progress(
            progress_callback,
            "knowledge_deep_map_unit_completed",
            f"章节 {unit.parent_ordinal} 已完成并保存检查点。",
            unit.map_unit_id,
            "info",
        )
        controlled_run = await _apply_deep_task_control_at_safe_boundary(
            run=run,
            task_id=task_id,
            progress_callback=progress_callback,
        )
        if controlled_run is not None:
            return _map_run_response(controlled_run)

    completed_run = _with_map_run_state(
        run,
        status="running",
        summary="知识库深度任务的 Map 阶段已完成，正在进入 Reduce 汇总。",
        finished=False,
    )
    _save_map_checkpoint(completed_run)
    append_workflow_event(
        task_id=task_id,
        event_name="knowledge_deep_map_completed",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        message="所有冻结章节的 Map 检查点已完成，等待后续 Reduce 阶段。",
    )
    await _notify_progress(
        progress_callback,
        "knowledge_deep_map_completed",
        "全部章节已完成 Map 分析，下一阶段才会做受控汇总。",
        None,
        "info",
    )
    # 这里的 `completed` 仅表示 Map 阶段完成；WorkflowRun 本身仍为 running，紧接着会进入
    # Reduce。这样进程在两个阶段之间退出时，启动恢复也不会留下一个无法继续的“整体完成”任务。
    return _map_run_response(completed_run, phase_status="completed")


async def run_knowledge_deep_task_reduce(
    *,
    task_id: str,
    scope: KnowledgeDeepTaskScope,
    model: ToolCallingModel | None = None,
    progress_callback: KnowledgeDeepTaskProgressCallback | None = None,
) -> KnowledgeDeepTaskReduceRunResponse:
    """在已完成 Map checkpoint 之上执行两级 Reduce。

    Reduce 不读取父块正文，也不扫描资料库；输入只由已验证的 Map 小结、发现项和稳定来源 ID
    构成。批次和最终合并均落 SQLite checkpoint，因此模型格式失败后可以显式恢复且不会重跑
    已完成批次。
    """

    _validate_map_execution_scope(scope)
    run = load_workflow_run(task_id)
    if run is None:
        raise KnowledgeDeepTaskMapExecutionError("未找到对应的 Map 任务，不能在不存在的检查点上执行 Reduce。")
    _validate_map_run_shape(run, scope)
    if not _scope_is_current(scope):
        return _block_reduce_for_stale_scope(run, task_id, scope.map_units[0].map_unit_id)
    if run.status in {"paused", "cancelled", "failed"}:
        return _reduce_run_response(run)
    controlled_run = await _apply_deep_task_control_at_safe_boundary(
        run=run,
        task_id=task_id,
        progress_callback=progress_callback,
    )
    if controlled_run is not None:
        return _reduce_run_response(controlled_run)
    map_steps = _map_steps_for_scope(run, scope)
    if any(step.status != "completed" for step in map_steps):
        blocked = _with_map_run_state(
            run,
            status="blocked",
            summary="Map 阶段尚未全部完成，不能基于不完整章节生成 Reduce 结论。",
            finished=False,
        )
        _save_map_checkpoint(blocked)
        return _reduce_run_response(blocked)

    run = _ensure_reduce_steps(run, scope)
    _validate_reduce_run_shape(run, scope)
    final_step = _find_reduce_final_step(run)
    if final_step.status == "completed":
        return _reduce_run_response(run)

    # 与 Map 相同，只有调用方再次进入同一任务才会重试上次失败的 Reduce 节点；已完成批次不再
    # 请求模型，避免模型异常期间反复扣费。
    if any(step.status == "failed" for step in _reduce_steps(run)):
        run = _reset_failed_reduce_steps(run)
        _save_map_checkpoint(run)
        append_workflow_event(
            task_id=task_id,
            event_name="knowledge_deep_reduce_resumed",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            message="已按客户显式恢复请求重试失败的 Reduce 节点；已完成 Map 和批次不会重跑。",
        )

    if model is None:
        try:
            model = _knowledge_deep_reduce_model_with_output_budget(
                resolve_model_runtime_for_route("knowledge_deep_analysis").runtime
            )
        except ModelGatewayError:
            blocked = _with_map_run_state(
                run,
                status="blocked",
                summary="深度任务等待可用 Reduce 模型配置，已完成的 Map 检查点将保留。",
                finished=False,
            )
            _save_map_checkpoint(blocked)
            append_workflow_event(
                task_id=task_id,
                event_name="knowledge_deep_reduce_blocked",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                message="当前没有可用的深度汇总模型配置；未读取父块正文或创建报告。",
                level="warning",
            )
            return _reduce_run_response(blocked)

    run = _with_model_route_audit(run, model=model, stage="knowledge_deep_reduce")
    run = _with_map_run_state(
        run,
        status="running",
        summary="正在合并已完成章节的小结，来源覆盖范围保持可追溯。",
        finished=False,
    )
    _save_map_checkpoint(run)
    await _notify_progress(
        progress_callback,
        "knowledge_deep_reduce_started",
        "正在分批合并已完成章节的小结。",
        None,
        "info",
    )

    reduce_plan = _reduce_plan(scope)
    for node_index, node in enumerate((item for item in reduce_plan if not item.is_final), start=1):
        reduce_step = _find_reduce_step(run, node.step_id)
        if reduce_step.status == "completed":
            continue
        controlled_run = await _apply_deep_task_control_at_safe_boundary(
            run=run,
            task_id=task_id,
            progress_callback=progress_callback,
        )
        if controlled_run is not None:
            return _reduce_run_response(controlled_run)
        if not _scope_is_current(scope):
            return _block_reduce_for_stale_scope(run, task_id, node.step_id)

        input_label = "章节小结" if not node.input_step_ids else "上一层汇总小结"
        definition = _reduce_definition(
            stage="batch",
            task_kind=scope.task_kind,
        )
        user_message = (
            _reduce_batch_user_message(
                scope,
                [unit for unit in scope.map_units if unit.map_unit_id in set(node.map_unit_ids)],
                run,
            )
            if not node.input_step_ids
            else _reduce_rollup_user_message(scope, node, run)
        )
        context_route = plan_knowledge_context_route(
            stage="deep_reduce",
            system_prompt=definition.system_prompt,
            user_message=user_message,
            model=model,
        )
        try:
            enforce_knowledge_context_budget(context_route)
        except KnowledgeContextBudgetError as exc:
            stopped_step = reduce_step.model_copy(
                update={"output": {**reduce_step.output, "context_route": context_route.model_dump(mode="json")}}
            )
            return await _stop_reduce_for_model_failure(
                run=run,
                task_id=task_id,
                step=stopped_step,
                stop_reason="context_budget_exceeded",
                failure_message=str(exc),
                progress_callback=progress_callback,
            )
        running_step = reduce_step.model_copy(
            update={
                "status": "running",
                "message": f"正在合并当前节点的{input_label}。",
                "output": {**reduce_step.output, "context_route": context_route.model_dump(mode="json")},
            }
        )
        run = _replace_map_step(run, running_step, status="running", summary="正在分批执行深度任务 Reduce。")
        _save_map_checkpoint(run)
        result, model_usage = await _run_deep_task_model_turn(
            definition=definition,
            model=model,
            user_message=user_message,
            task_id=task_id,
            progress_callback=progress_callback,
            step_id=node.step_id,
            stage_label="Reduce 批次汇总",
        )
        run = _with_provider_usage_metrics(run, model=model, usage=model_usage)
        if result.status != "completed" or not isinstance(result.output, KnowledgeDeepReduceDraft):
            return await _stop_reduce_for_model_failure(
                run=run,
                task_id=task_id,
                step=running_step,
                stop_reason=result.stop_reason,
                failure_message=_deep_task_model_failure_message(result.stop_reason, result.message),
                progress_callback=progress_callback,
            )
        if not _scope_is_current(scope):
            return _block_reduce_for_stale_scope(run, task_id, node.step_id)

        completed_step = reduce_step.model_copy(
            update={
                "status": "completed",
                "message": "批次小结完成，已保存可恢复 Reduce 检查点。",
                "output": _reduce_batch_step_output(node, result.output, context_route=context_route),
            }
        )
        run = _replace_map_step(run, completed_step, status="running", summary="已完成一个 Reduce 批次，正在继续合并。")
        _save_map_checkpoint(run)
        append_workflow_event(
            task_id=task_id,
            event_name="knowledge_deep_reduce_batch_completed",
            agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            step_id=node.step_id,
            message=f"第 {node_index} 个分层汇总节点已保存。",
        )
        await _notify_progress(
            progress_callback,
            "knowledge_deep_reduce_batch_completed",
            f"第 {node_index} 个分层汇总节点已完成。",
            node.step_id,
            "info",
        )
        controlled_run = await _apply_deep_task_control_at_safe_boundary(
            run=run,
            task_id=task_id,
            progress_callback=progress_callback,
        )
        if controlled_run is not None:
            return _reduce_run_response(controlled_run)

    final_node = next(node for node in reduce_plan if node.is_final)
    final_step = _find_reduce_step(run, final_node.step_id)
    controlled_run = await _apply_deep_task_control_at_safe_boundary(
        run=run,
        task_id=task_id,
        progress_callback=progress_callback,
    )
    if controlled_run is not None:
        return _reduce_run_response(controlled_run)
    if not _scope_is_current(scope):
        return _block_reduce_for_stale_scope(run, task_id, final_step.step_id)
    all_map_ids = [unit.map_unit_id for unit in scope.map_units]
    final_definition = _reduce_definition(
        stage="final",
        task_kind=scope.task_kind,
    )
    final_user_message = _reduce_final_user_message(scope, run, final_node)
    final_context_route = plan_knowledge_context_route(
        stage="deep_reduce",
        system_prompt=final_definition.system_prompt,
        user_message=final_user_message,
        model=model,
    )
    try:
        enforce_knowledge_context_budget(final_context_route)
    except KnowledgeContextBudgetError as exc:
        stopped_step = final_step.model_copy(
            update={"output": {**final_step.output, "context_route": final_context_route.model_dump(mode="json")}}
        )
        return await _stop_reduce_for_model_failure(
            run=run,
            task_id=task_id,
            step=stopped_step,
            stop_reason="context_budget_exceeded",
            failure_message=str(exc),
            progress_callback=progress_callback,
        )
    final_running = final_step.model_copy(
        update={
            "status": "running",
            "message": "正在合并批次结论并保留可定位冲突。",
            "output": {**final_step.output, "context_route": final_context_route.model_dump(mode="json")},
        }
    )
    run = _replace_map_step(run, final_running, status="running", summary="正在完成深度任务的最终 Reduce 合并。")
    _save_map_checkpoint(run)
    final_model_result, model_usage = await _run_deep_task_model_turn(
        definition=final_definition,
        model=model,
        user_message=final_user_message,
        task_id=task_id,
        progress_callback=progress_callback,
        step_id=final_step.step_id,
        stage_label="Reduce 最终汇总",
    )
    run = _with_provider_usage_metrics(run, model=model, usage=model_usage)
    if final_model_result.status != "completed" or not isinstance(final_model_result.output, KnowledgeDeepReduceDraft):
        return await _stop_reduce_for_model_failure(
            run=run,
            task_id=task_id,
            step=final_running,
            stop_reason=final_model_result.stop_reason,
            failure_message=_deep_task_model_failure_message(
                final_model_result.stop_reason,
                final_model_result.message,
            ),
            progress_callback=progress_callback,
        )
    if not _scope_is_current(scope):
        return _block_reduce_for_stale_scope(run, task_id, final_step.step_id)

    final_result = _reduce_result_from_draft(
        task_kind=scope.task_kind,
        draft=final_model_result.output,
        covered_map_unit_ids=all_map_ids,
        comparison_document_ids=scope.selected_document_ids,
        comparison_fallback_values=_comparison_fallback_values(scope, run),
    )
    final_completed = final_step.model_copy(
        update={
            "status": "completed",
            "message": "最终 Reduce 小结已完成，等待后续报告渲染与客户确认。",
            "output": {
                "knowledge_base_id": scope.knowledge_base_id,
                "read_scope": "map_checkpoints_only",
                "map_unit_ids": list(final_node.map_unit_ids),
                "input_step_ids": list(final_node.input_step_ids),
                "reduce_level": final_node.level,
                "original_files_unchanged": True,
                "context_route": final_context_route.model_dump(mode="json"),
                "reduce_result": final_result.model_dump(mode="json"),
            },
        }
    )
    run = _replace_map_step(
        run,
        final_completed,
        status="completed",
        summary="深度任务的 Map 与 Reduce 检查点均已完成，尚未生成正式报告文件。",
    )
    run = _with_map_run_state(run, status="completed", summary=run.summary, finished=True)
    _save_map_checkpoint(run)
    append_workflow_event(
        task_id=task_id,
        event_name="knowledge_deep_reduce_completed",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        step_id=final_step.step_id,
        message="深度任务 Reduce 检查点已完成，等待后续报告渲染和客户入口。",
    )
    await _notify_progress(
        progress_callback,
        "knowledge_deep_reduce_completed",
        "深度任务已完成可追溯 Reduce，后续阶段才生成正式报告。",
        final_step.step_id,
        "info",
    )
    controlled_run = await _apply_deep_task_control_at_safe_boundary(
        run=run,
        task_id=task_id,
        progress_callback=progress_callback,
    )
    if controlled_run is not None:
        return _reduce_run_response(controlled_run)
    return _reduce_run_response(run)


async def _run_deep_task_model_turn(
    *,
    definition: AgentDefinition,
    model: ToolCallingModel,
    user_message: str,
    task_id: str,
    progress_callback: KnowledgeDeepTaskProgressCallback | None,
    step_id: str,
    stage_label: str,
) -> tuple[AgentRunResult, AgentModelUsageSummary]:
    """执行一个 K4 模型节点，并只对瞬态请求失败做一次受控退避重试。

    Map/Reduce checkpoint 的“继续”是客户显式动作，不能变成无限自动重跑；但网络抖动、临时限流
    或供应商短暂 5xx 不该迫使客户连续点击。这里最多两次调用，第二次仍失败就原样返回给上层
    停驻。结构化输出不合格不会走这条重试，以免格式问题重复消耗额度。
    """

    await _wait_for_deep_task_model_slot(
        model=model,
        task_id=task_id,
        progress_callback=progress_callback,
        step_id=step_id,
        stage_label=stage_label,
    )
    result = await AgentRunner().run(
        definition=definition,
        model=model,
        user_message=user_message,
    )
    usage = result.model_usage_summary
    if (
        result.status == "completed"
        or not _is_transient_deep_task_model_failure(result)
        or MAX_DEEP_TASK_MODEL_ATTEMPTS < 2
    ):
        return result, usage

    retry_delay = _remember_deep_task_rate_limit(model, result)
    append_workflow_event(
        task_id=task_id,
        event_name="knowledge_deep_model_transient_retry",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        step_id=step_id,
        message=f"{stage_label}遇到临时模型请求问题，系统将等待安全窗口后进行一次受控重试。",
        level="warning",
    )
    await _notify_progress(
        progress_callback,
        "knowledge_deep_model_transient_retry",
        f"{stage_label}遇到临时模型请求问题，正在等待安全窗口后进行一次受控重试。",
        step_id,
        "warning",
    )
    # 连接复位/5xx 只需短退避；429 则优先采用供应商明确的 retry-after，随后还会进入同一
    # Provider/API Key 的 RPM 队列。这样不会把“自动恢复”变成短时间内多次无效扣费。
    await asyncio.sleep(retry_delay)
    await _wait_for_deep_task_model_slot(
        model=model,
        task_id=task_id,
        progress_callback=progress_callback,
        step_id=step_id,
        stage_label=stage_label,
    )
    retry_result = await AgentRunner().run(
        definition=definition,
        model=model,
        user_message=user_message,
    )
    return retry_result, _merge_model_usage_summaries(usage, retry_result.model_usage_summary)


def _merge_model_usage_summaries(
    *summaries: AgentModelUsageSummary,
) -> AgentModelUsageSummary:
    """合并一次节点内的首次调用与受控重试，不把缺失 usage 伪造成 0。"""

    return AgentModelUsageSummary(
        request_total=sum(item.request_total for item in summaries),
        usage_reported_request_total=sum(item.usage_reported_request_total for item in summaries),
        cache_observed_request_total=sum(item.cache_observed_request_total for item in summaries),
        input_tokens=_sum_optional_usage(item.input_tokens for item in summaries),
        output_tokens=_sum_optional_usage(item.output_tokens for item in summaries),
        total_tokens=_sum_optional_usage(item.total_tokens for item in summaries),
        cache_read_input_tokens=_sum_optional_usage(item.cache_read_input_tokens for item in summaries),
        cache_creation_input_tokens=_sum_optional_usage(
            item.cache_creation_input_tokens for item in summaries
        ),
        cache_miss_input_tokens=_sum_optional_usage(item.cache_miss_input_tokens for item in summaries),
    )


def _with_provider_usage_metrics(
    run: WorkflowRun,
    *,
    model: ToolCallingModel,
    usage: AgentModelUsageSummary,
) -> WorkflowRun:
    """把真实 Provider 的无正文 usage 累加到同一可恢复任务快照。

    只有 ``ModelRuntime`` 代表真实供应商请求。离线 mock 也会复用 AgentRunner，但绝不能在
    客户任务历史里伪造 Provider 调用或 token；无法取得 usage 的真实响应仍记录请求数，令
    客户和后续成本层知道当前统计是部分可观测而非 0 消耗。
    """

    if not isinstance(model, ModelRuntime) or usage.request_total < 1:
        return run

    metrics = run.metrics
    updated_metrics = metrics.model_copy(
        update={
            "provider_model_request_total": metrics.provider_model_request_total + usage.request_total,
            "provider_usage_reported_request_total": (
                metrics.provider_usage_reported_request_total + usage.usage_reported_request_total
            ),
            "provider_cache_observed_request_total": (
                metrics.provider_cache_observed_request_total + usage.cache_observed_request_total
            ),
            "provider_input_tokens": _add_optional_usage(metrics.provider_input_tokens, usage.input_tokens),
            "provider_output_tokens": _add_optional_usage(metrics.provider_output_tokens, usage.output_tokens),
            "provider_total_tokens": _add_optional_usage(metrics.provider_total_tokens, usage.total_tokens),
            "provider_cache_read_input_tokens": _add_optional_usage(
                metrics.provider_cache_read_input_tokens,
                usage.cache_read_input_tokens,
            ),
            "provider_cache_creation_input_tokens": _add_optional_usage(
                metrics.provider_cache_creation_input_tokens,
                usage.cache_creation_input_tokens,
            ),
            "provider_cache_miss_input_tokens": _add_optional_usage(
                metrics.provider_cache_miss_input_tokens,
                usage.cache_miss_input_tokens,
            ),
        }
    )
    return run.model_copy(update={"metrics": updated_metrics})


def _with_model_route_audit(
    run: WorkflowRun,
    *,
    model: ToolCallingModel,
    stage: str,
) -> WorkflowRun:
    """把深度任务已解析的真实路由追加到同一 checkpoint。

    任务可能跨 Map/Reduce、暂停和恢复运行数小时。只要同阶段的 Route/provider/model/thinking
    已被保存，就不再改写它；测试替身或未通过 Route 解析的模型也不会被当前配置虚构成审计项。
    """

    snapshot = model_route_audit_snapshot_for_stage(model, stage=stage)
    if snapshot is None:
        return run
    if any(
        item.stage == snapshot.stage
        and item.route_id == snapshot.route_id
        and item.profile_id == snapshot.profile_id
        and item.provider == snapshot.provider
        and item.model == snapshot.model
        and item.thinking == snapshot.thinking
        for item in run.model_routes
    ):
        return run
    return run.model_copy(update={"model_routes": [*run.model_routes, snapshot]})


def _sum_optional_usage(values: Iterable[int | None]) -> int | None:
    observed = [value for value in values if isinstance(value, int)]
    return sum(observed) if observed else None


def _add_optional_usage(current: int | None, addition: int | None) -> int | None:
    if current is None and addition is None:
        return None
    return (current or 0) + (addition or 0)


def _is_transient_deep_task_model_failure(result: AgentRunResult) -> bool:
    """只重试连接、超时、限流和短暂服务端错误，参数/契约错误不会被重复发送。"""

    if result.stop_reason in DEEP_TASK_TRANSIENT_MODEL_STOP_REASONS:
        return True
    if result.stop_reason != "model_request_failed":
        return False
    # Gateway 只保留脱敏的 HTTP 状态和短原因；4xx 中只有超时/冲突/限流具备短时恢复价值。
    return bool(re.search(r"HTTP (?:408|409|425|429|5\d\d)\b", result.message))


def _deep_task_rate_limit_key(model: ToolCallingModel) -> str | None:
    """为真实 Runtime 生成进程内限流桶键，绝不把 API Key 原文写入日志或 SQLite。"""

    if not isinstance(model, ModelRuntime):
        return None
    api_key = model.api_key.strip()
    if not api_key:
        return None
    key_digest = sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return f"{model.provider}:{model.model}:{model.base_url.rstrip('/').lower()}:{key_digest}"


def _rpm_limit_from_model_failure(result: AgentRunResult) -> int | None:
    """仅接受供应商明确声明的正整数 RPM，避免从任意错误文本臆测限额。"""

    if result.stop_reason != "model_request_failed" or "HTTP 429" not in result.message:
        return None
    match = re.search(r"\bmax\s+rpm\s*:\s*(\d{1,6})\b", result.message, flags=re.IGNORECASE)
    if match is None:
        return None
    return max(1, int(match.group(1)))


def _retry_delay_seconds(result: AgentRunResult) -> float:
    """读取供应商可公开的 retry-after 提示；未知时保持很短的网络退避。"""

    match = re.search(r"(?:try\s+again\s+)?after\s+(\d+(?:\.\d+)?)\s+seconds?", result.message, flags=re.IGNORECASE)
    if match is None:
        return 0.8
    return min(30.0, max(0.8, float(match.group(1)) + 0.2))


def _remember_deep_task_rate_limit(model: ToolCallingModel, result: AgentRunResult) -> float:
    """记录本进程观察到的 RPM，并返回适合该次临时错误的最短退避。"""

    rpm = _rpm_limit_from_model_failure(result)
    rate_key = _deep_task_rate_limit_key(model)
    if rpm is not None and rate_key is not None:
        with _DEEP_TASK_RATE_LOCK:
            previous = _DEEP_TASK_RATE_LIMITS_RPM.get(rate_key)
            # 数字越小代表限制越严格；同一账户多个回执不应因一次较宽松的提示放宽已有保护。
            _DEEP_TASK_RATE_LIMITS_RPM[rate_key] = min(previous, rpm) if previous is not None else rpm
    return _retry_delay_seconds(result)


async def _wait_for_deep_task_model_slot(
    *,
    model: ToolCallingModel,
    task_id: str,
    progress_callback: KnowledgeDeepTaskProgressCallback | None,
    step_id: str,
    stage_label: str,
) -> None:
    """在已知 RPM 窗口中预约一次模型请求，避免同一账户的 K4 节点互相触发 429。"""

    rate_key = _deep_task_rate_limit_key(model)
    if rate_key is None:
        return

    announced_wait = False
    while True:
        now = monotonic()
        wait_seconds = 0.0
        with _DEEP_TASK_RATE_LOCK:
            request_times = _DEEP_TASK_REQUEST_TIMES.setdefault(rate_key, deque())
            while request_times and request_times[0] <= now - 60.0:
                request_times.popleft()

            rpm = _DEEP_TASK_RATE_LIMITS_RPM.get(rate_key)
            if rpm is not None and len(request_times) >= rpm:
                # 比滚动一分钟边界多留一点余量，防止时钟粒度或供应商窗口边界导致刚恢复又 429。
                wait_seconds = max(0.0, request_times[0] + 60.25 - now)
            else:
                request_times.append(now)
                return

        if not announced_wait:
            displayed_seconds = max(1, int(wait_seconds + 0.999))
            message = (
                f"{stage_label}正在遵守模型供应商的请求速率限制，约 {displayed_seconds} 秒后自动继续；"
                "已完成章节检查点不会重复执行。"
            )
            append_workflow_event(
                task_id=task_id,
                event_name="knowledge_deep_model_rate_limit_wait",
                agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
                step_id=step_id,
                message=message,
                level="info",
            )
            await _notify_progress(
                progress_callback,
                "knowledge_deep_model_rate_limit_wait",
                message,
                step_id,
                "info",
            )
            announced_wait = True
        await asyncio.sleep(wait_seconds)


def _deep_task_model_failure_message(stop_reason: str, runner_message: str) -> str:
    """将 Runner 的受控失败分类转换为客户能判断下一步的短说明。

    ``runner_message`` 来自 ModelGateway 的脱敏异常或固定 Runner 文案，不包含请求正文；仍在这里
    截断和压缩，防止未来 Provider 的异常文本意外撑大 SQLite 任务历史或 Qt 状态区。
    """

    normalized = " ".join(runner_message.split())[:260]
    if stop_reason == "model_timeout":
        return "模型本次等待超时；系统已自动重试一次，仍未完成。"
    if stop_reason == "model_connection_failed":
        return "模型服务暂时无法连接；系统已自动重试一次，仍未完成。"
    if stop_reason == "model_output_invalid":
        return "模型返回内容未通过当前节点的结构化校验；系统未继续重复调用。"
    if stop_reason == "max_turns_exceeded":
        return "模型没有在当前受控轮次内完成结论；系统未继续扩展模型预算。"
    if normalized:
        return normalized
    return "模型请求未完成；系统已保留检查点，等待客户继续或检查模型配置。"


@dataclass(frozen=True)
class _ReducePlanNode:
    """一个可恢复 Reduce 节点的冻结输入边界。

    首层节点只读 Map checkpoint；后续 rollup 节点只读前一层的 Reduce 草稿。每层最大扇出固定为
    六，目的是控制单次模型上下文，而非限制客户可分析的章节数量。
    """

    step_id: str
    map_unit_ids: tuple[str, ...]
    input_step_ids: tuple[str, ...]
    level: int
    is_final: bool = False


def _reduce_plan(scope: KnowledgeDeepTaskScope) -> list[_ReducePlanNode]:
    """为任意章节数构建分层 Reduce 树，最终节点永远只消费最多六个上层小结。"""

    primary_nodes: list[_ReducePlanNode] = []
    for index, start in enumerate(range(0, len(scope.map_units), MAX_DEEP_TASK_REDUCE_BATCH_UNITS), start=1):
        units = scope.map_units[start : start + MAX_DEEP_TASK_REDUCE_BATCH_UNITS]
        primary_nodes.append(
            _ReducePlanNode(
                step_id=_reduce_batch_step_id(index),
                map_unit_ids=tuple(unit.map_unit_id for unit in units),
                input_step_ids=(),
                level=1,
            )
        )

    nodes = list(primary_nodes)
    current_level = primary_nodes
    level = 2
    while len(current_level) > MAX_DEEP_TASK_REDUCE_BATCH_UNITS:
        next_level: list[_ReducePlanNode] = []
        for index, start in enumerate(
            range(0, len(current_level), MAX_DEEP_TASK_REDUCE_BATCH_UNITS),
            start=1,
        ):
            children = current_level[start : start + MAX_DEEP_TASK_REDUCE_BATCH_UNITS]
            next_level.append(
                _ReducePlanNode(
                    step_id=f"knowledge_reduce_rollup_{level}_{index}",
                    map_unit_ids=tuple(
                        map_unit_id
                        for child in children
                        for map_unit_id in child.map_unit_ids
                    ),
                    input_step_ids=tuple(child.step_id for child in children),
                    level=level,
                )
            )
        nodes.extend(next_level)
        current_level = next_level
        level += 1

    nodes.append(
        _ReducePlanNode(
            step_id=_reduce_final_step_id(),
            map_unit_ids=tuple(unit.map_unit_id for unit in scope.map_units),
            input_step_ids=tuple(node.step_id for node in current_level),
            level=level,
            is_final=True,
        )
    )
    return nodes


def _ensure_reduce_steps(run: WorkflowRun, scope: KnowledgeDeepTaskScope) -> WorkflowRun:
    """为已完成 Map 任务追加可恢复 Reduce 节点；Map 步骤顺序永不改写。"""

    map_count = len(scope.map_units)
    plan = _reduce_plan(scope)
    expected_reduce_ids = [node.step_id for node in plan]
    existing_reduce = run.steps[map_count:]
    if existing_reduce:
        if [step.step_id for step in existing_reduce] != expected_reduce_ids:
            raise KnowledgeDeepTaskMapExecutionError("已有 Reduce 检查点与当前任务范围不匹配，不能错误恢复。")
        return run

    reduce_steps = [
        WorkflowStepRun(
            step_id=node.step_id,
            agent=KNOWLEDGE_DEEP_TASK_AGENT_ID,
            action=KNOWLEDGE_DEEP_TASK_REDUCE_FINAL_ACTION if node.is_final else KNOWLEDGE_DEEP_TASK_REDUCE_BATCH_ACTION,
            status="pending",
            message=("等待合并最后一层小结。" if node.is_final else "等待合并受控章节或上层小结。"),
            output={
                "knowledge_base_id": scope.knowledge_base_id,
                "read_scope": "reduce_checkpoints_only" if node.input_step_ids else "map_checkpoints_only",
                "map_unit_ids": list(node.map_unit_ids),
                "input_step_ids": list(node.input_step_ids),
                "reduce_level": node.level,
                "original_files_unchanged": True,
            },
        )
        for node in plan
    ]
    steps = [*run.steps, *reduce_steps]
    expanded = run.model_copy(
        update={
            "status": "pending",
            "summary": "Map 检查点已完成，等待执行受控 Reduce 合并。",
            "steps": steps,
            "limits": _deep_task_limits(len(scope.map_units), len(reduce_steps)),
            "metrics": run.metrics.model_copy(
                update={
                    "step_total": len(steps),
                    "step_completed": sum(step.status == "completed" for step in steps),
                    "step_failed": 0,
                    "finished_at": "",
                }
            ),
        }
    )
    _save_map_checkpoint(expanded)
    append_workflow_event(
        task_id=run.task_id,
        event_name="knowledge_deep_reduce_queued",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        message=f"已建立 {len(reduce_steps) - 1} 个分层 Reduce 节点和一个最终合并节点。",
    )
    return expanded


def _validate_reduce_run_shape(run: WorkflowRun, scope: KnowledgeDeepTaskScope) -> None:
    plan = _reduce_plan(scope)
    expected_ids = [node.step_id for node in plan]
    plan_by_id = {node.step_id: node for node in plan}
    reduce_steps = _reduce_steps(run)
    if [step.step_id for step in reduce_steps] != expected_ids:
        raise KnowledgeDeepTaskMapExecutionError("Reduce 检查点缺少节点或顺序异常，不能继续恢复。")
    for step in reduce_steps:
        node = plan_by_id[step.step_id]
        expected_action = KNOWLEDGE_DEEP_TASK_REDUCE_FINAL_ACTION if node.is_final else KNOWLEDGE_DEEP_TASK_REDUCE_BATCH_ACTION
        if step.agent != KNOWLEDGE_DEEP_TASK_AGENT_ID or step.action != expected_action:
            raise KnowledgeDeepTaskMapExecutionError("Reduce 检查点的 Agent 或动作不匹配。")
        if step.status == "completed":
            key = "reduce_result" if step.step_id == _reduce_final_step_id() else "reduce_draft"
            raw_output = step.output.get(key)
            if not isinstance(raw_output, dict):
                raise KnowledgeDeepTaskMapExecutionError("已完成 Reduce 节点缺少结构化检查点，不能继续恢复。")
            if key == "reduce_result":
                KnowledgeDeepReduceResult.model_validate(raw_output)
            else:
                KnowledgeDeepReduceDraft.model_validate(raw_output)
        # K4.15 前保存的历史任务没有 rollup 元数据。只要它们已有的节点顺序与完成 checkpoint
        # 仍可验证，就允许继续读取和导出；新任务则必须把冻结依赖完整写回。
        if "map_unit_ids" in step.output and step.output.get("map_unit_ids", []) != list(node.map_unit_ids):
            raise KnowledgeDeepTaskMapExecutionError("Reduce 检查点的章节覆盖范围不匹配，不能继续恢复。")
        if "input_step_ids" in step.output and step.output.get("input_step_ids", []) != list(node.input_step_ids):
            raise KnowledgeDeepTaskMapExecutionError("Reduce 检查点的上游依赖不匹配，不能继续恢复。")


def _reduce_steps(run: WorkflowRun) -> list[WorkflowStepRun]:
    return [step for step in run.steps if step.action in {
        KNOWLEDGE_DEEP_TASK_REDUCE_BATCH_ACTION,
        KNOWLEDGE_DEEP_TASK_REDUCE_FINAL_ACTION,
    }]


def _reduce_batch_step_id(batch_index: int) -> str:
    return f"knowledge_reduce_batch_{batch_index}"


def _reduce_final_step_id() -> str:
    return "knowledge_reduce_final"


def _find_reduce_step(run: WorkflowRun, step_id: str) -> WorkflowStepRun:
    for step in run.steps:
        if step.step_id == step_id:
            return step
    raise KnowledgeDeepTaskMapExecutionError("任务检查点缺少当前 Reduce 节点，不能继续恢复。")


def _find_reduce_final_step(run: WorkflowRun) -> WorkflowStepRun:
    return _find_reduce_step(run, _reduce_final_step_id())


def _reduce_definition(
    *,
    stage: str,
    task_kind: str,
) -> AgentDefinition:
    """Reduce 只提交语义草稿；来源闭合与动态编号由 Runtime 单向投影。"""

    return AgentDefinition(
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        system_prompt=(
            "你是 AgentFlow 知识库的受控 Reduce 分析器。输入只包含已经完成的章节级小结，"
            "不包含原始文档正文。只能基于这些小结归纳，不能补写外部常识或无来源事实。"
            f"当前阶段={stage}；任务类型={task_kind}。"
            "只返回一个 JSON object，且只包含 overview、findings、conflicts、comparison_rows、warnings 五个字段。"
            "overview 是总体归纳；findings 是最多 6 条短字符串；conflicts 是最多 4 个"
            "{topic,description} 对象，只在材料确有不一致或待确认边界时填写；warnings 是最多 4 条"
            "短字符串。若任务类型为 comparison，最终阶段必须尽量返回 3 到 8 个 comparison_rows；"
            "每行是 {dimension,values,conclusion}，values 的顺序严格对应输入 comparison_documents，"
            "不得猜测没有出现在小结中的内容。非 comparison 可返回空数组。不要输出 map_unit_id、"
            "source_id、任何内部编号、Markdown、代码围栏或解释文字。"
            "来源范围、覆盖范围和编号由系统根据冻结检查点写入，不需要也不得自行编造。"
        ),
        tools=(),
        output_model=KnowledgeDeepReduceDraft,
        max_turns=2,
        max_tool_calls=0,
        max_same_tool_failure=0,
        max_output_repair_attempts=1,
    )


def _reduce_batch_user_message(
    scope: KnowledgeDeepTaskScope,
    unit_batch: list[KnowledgeDeepTaskMapUnit],
    run: WorkflowRun,
) -> str:
    return json.dumps(
        {
            "task_goal": scope.task_goal,
            "task_kind": scope.task_kind,
            "comparison_documents": _comparison_documents(scope),
            "input_scope": {"chapter_count": len(unit_batch), "stage": "batch_reduce"},
            "map_summaries": [_compact_map_checkpoint(run, unit) for unit in unit_batch],
        },
        ensure_ascii=False,
    )


def _reduce_rollup_user_message(
    scope: KnowledgeDeepTaskScope,
    node: _ReducePlanNode,
    run: WorkflowRun,
) -> str:
    """将上一层有限小结送入下一层；不会重新读取父块或扫描资料库。"""

    return json.dumps(
        {
            "task_goal": scope.task_goal,
            "task_kind": scope.task_kind,
            "comparison_documents": _comparison_documents(scope),
            "input_scope": {
                "chapter_count": len(node.map_unit_ids),
                "stage": f"reduce_rollup_level_{node.level}",
            },
            "reduce_summaries": [
                _compact_reduce_checkpoint(_find_reduce_step(run, step_id))
                for step_id in node.input_step_ids
            ],
        },
        ensure_ascii=False,
    )


def _reduce_final_user_message(scope: KnowledgeDeepTaskScope, run: WorkflowRun, node: _ReducePlanNode) -> str:
    """最终节点只读取最末层的有限 checkpoint，完整来源范围由 Runtime 另行投影。"""

    return json.dumps(
        {
            "task_goal": scope.task_goal,
            "task_kind": scope.task_kind,
            "comparison_documents": _comparison_documents(scope),
            "input_scope": {"chapter_count": len(scope.map_units), "stage": "final_reduce"},
            "reduce_summaries": [
                _compact_reduce_checkpoint(_find_reduce_step(run, step_id))
                for step_id in node.input_step_ids
            ],
        },
        ensure_ascii=False,
    )


def _comparison_documents(scope: KnowledgeDeepTaskScope) -> list[dict[str, str]]:
    """只把对照列的稳定顺序和可读资料名给模型，不提供路径或正文。"""

    if scope.task_kind != "comparison":
        return []
    names_by_document_id: dict[str, str] = {}
    for unit in scope.map_units:
        names_by_document_id.setdefault(unit.document_id, unit.document_name)
    return [
        {"document_id": document_id, "document_name": names_by_document_id.get(document_id, "已选资料")}
        for document_id in scope.selected_document_ids
    ]


def _comparison_fallback_values(scope: KnowledgeDeepTaskScope, run: WorkflowRun) -> list[str]:
    """在模型漏掉表格字段时，从已完成 Map 小结组成一行不增添事实的资料摘要。"""

    if scope.task_kind != "comparison":
        return []
    values: list[str] = []
    for document_id in scope.selected_document_ids:
        summaries: list[str] = []
        for unit in scope.map_units:
            if unit.document_id != document_id:
                continue
            raw_result = _find_map_step(run, unit.map_unit_id).output.get("map_result")
            if not isinstance(raw_result, dict):
                continue
            result = KnowledgeDeepMapResult.model_validate(raw_result)
            summaries.append(_compact_text(result.summary, 240))
            if len(summaries) >= 3:
                break
        values.append("；".join(summaries) if summaries else "当前汇总未明确说明")
    return values


def _compact_map_checkpoint(run: WorkflowRun, unit: KnowledgeDeepTaskMapUnit) -> dict[str, object]:
    step = _find_map_step(run, unit.map_unit_id)
    raw_result = step.output.get("map_result")
    if not isinstance(raw_result, dict):
        raise KnowledgeDeepTaskMapExecutionError("Map 检查点缺少结构化小结，不能进入 Reduce。")
    result = KnowledgeDeepMapResult.model_validate(raw_result)
    return {
        "map_unit_id": unit.map_unit_id,
        "document_name": unit.document_name,
        "heading_path": unit.heading_path,
        "source": unit.source.model_dump(mode="json"),
        "summary": _compact_text(result.summary, MAX_DEEP_TASK_REDUCE_SUMMARY_CHARS),
        "findings": [
            {
                "finding_id": finding.finding_id,
                "statement": _compact_text(finding.statement, MAX_DEEP_TASK_REDUCE_FINDING_CHARS),
                "source_ids": finding.source_ids,
            }
            for finding in result.findings[:3]
        ],
        "warnings": [_compact_text(item, 180) for item in result.warnings[:2]],
    }


def _compact_reduce_checkpoint(step: WorkflowStepRun) -> dict[str, object]:
    raw_draft = step.output.get("reduce_draft")
    if not isinstance(raw_draft, dict):
        raise KnowledgeDeepTaskMapExecutionError("Reduce 批次检查点缺少结构化小结，不能继续最终合并。")
    draft = KnowledgeDeepReduceDraft.model_validate(raw_draft)
    return {
        "chapter_count": len(step.output.get("map_unit_ids", [])),
        "overview": _compact_text(draft.overview, 1_200),
        "findings": [_compact_text(finding, 480) for finding in draft.findings[:6]],
        "conflicts": [
            {
                "topic": conflict.topic,
                "description": _compact_text(conflict.description, 480),
            }
            for conflict in draft.conflicts[:4]
        ],
        "warnings": [_compact_text(item, 180) for item in draft.warnings[:3]],
        "comparison_rows": [
            {
                "dimension": _compact_text(row.dimension, 120),
                "values": [_compact_text(value, 260) for value in row.values[:12]],
                "conclusion": _compact_text(row.conclusion, 240),
            }
            for row in draft.comparison_rows[:8]
        ],
    }


def _compact_text(value: str, limit: int) -> str:
    # Map 小结可能来自不同 Provider；压缩空白并截断是 Reduce 的上下文预算保护，不修改 SQLite
    # 中完整 checkpoint，后续报告渲染仍可以按需读取完整受验证小结。
    compacted = " ".join(value.split())
    return compacted[:limit]


def _reduce_result_from_draft(
    *,
    task_kind: str,
    draft: KnowledgeDeepReduceDraft,
    covered_map_unit_ids: list[str],
    comparison_document_ids: list[str] | None = None,
    comparison_fallback_values: list[str] | None = None,
) -> KnowledgeDeepReduceResult:
    """将模型的语义草稿投影为可审计的正式 Reduce 结果。

    Map 与 Reduce 的输入范围均由冻结 scope 决定。模型只表达从这些小结中归纳出的内容，Runtime
    才把当前输入章节写入每条结果的来源范围。这样既不允许模型伪造 ID，也不会因复制 24 个动态
    字符串而让真实长任务停驻。这里的来源表示“本次归纳的已读范围”，不是模型声称的逐句引文。
    """

    source_ids = list(dict.fromkeys(covered_map_unit_ids))
    if not source_ids:
        raise KnowledgeDeepTaskMapExecutionError("Reduce 结果缺少冻结的 Map 覆盖范围。")
    findings = [item.strip()[:1_200] for item in draft.findings if item.strip()][:12]
    warnings = [item.strip()[:480] for item in draft.warnings if item.strip()][:8]
    conflicts = list(draft.conflicts)[:8] if len(source_ids) >= 2 else []
    comparison_rows: list[KnowledgeDeepComparisonRow] = []
    if task_kind == "comparison":
        expected_column_count = len(comparison_document_ids or [])
        for row in draft.comparison_rows[:8]:
            values = [item.strip()[:520] for item in row.values if item.strip()][:expected_column_count]
            while len(values) < expected_column_count:
                values.append("当前汇总未明确说明")
            if expected_column_count >= 2:
                comparison_rows.append(
                    KnowledgeDeepComparisonRow(
                        dimension=row.dimension.strip()[:120],
                        values=values,
                        conclusion=row.conclusion.strip()[:360],
                        source_ids=source_ids,
                    )
                )
        # 即使 Provider 忽略了表格字段，也必须交付一个诚实的资料摘要行，不能让客户点击“资料
        # 对照”后得到一份没有任何表格的普通摘要。该回退仅复用已完成 Map 小结，不补造事实。
        if not comparison_rows and expected_column_count >= 2:
            fallback_values = list(comparison_fallback_values or [])[:expected_column_count]
            while len(fallback_values) < expected_column_count:
                fallback_values.append("当前汇总未明确说明")
            comparison_rows.append(
                KnowledgeDeepComparisonRow(
                    dimension="资料摘要",
                    values=fallback_values,
                    conclusion="模型未给出更细对照维度；此行仅汇集各资料的已完成章节小结。",
                    source_ids=source_ids,
                )
            )
    return KnowledgeDeepReduceResult(
        task_kind=task_kind,
        overview=draft.overview.strip()[:2_000],
        findings=[
            KnowledgeDeepReduceFinding(
                finding_id=f"kb_reduce_finding_{index}",
                statement=statement,
                source_ids=source_ids,
            )
            for index, statement in enumerate(findings, start=1)
        ],
        conflicts=[
            KnowledgeDeepReduceConflict(
                conflict_id=f"kb_reduce_conflict_{index}",
                topic=conflict.topic.strip()[:180],
                description=conflict.description.strip()[:1_000],
                source_ids=source_ids,
            )
            for index, conflict in enumerate(conflicts, start=1)
        ],
        comparison_rows=comparison_rows,
        warnings=warnings,
        covered_map_unit_ids=source_ids,
        failed_map_unit_ids=[],
    )


def _reduce_batch_step_output(
    node: _ReducePlanNode,
    draft: KnowledgeDeepReduceDraft,
    *,
    context_route: KnowledgeContextRouteDecision | None = None,
) -> dict[str, object]:
    output: dict[str, object] = {
        "read_scope": "reduce_checkpoints_only" if node.input_step_ids else "map_checkpoints_only",
        "map_unit_ids": list(node.map_unit_ids),
        "input_step_ids": list(node.input_step_ids),
        "reduce_level": node.level,
        # 这份草稿不包含模型复制的 ID；当前批次范围由同一 checkpoint 的 map_unit_ids 固定。
        "reduce_draft": draft.model_dump(mode="json"),
        "original_files_unchanged": True,
    }
    if context_route is not None:
        output["context_route"] = context_route.model_dump(mode="json")
    return output


async def _stop_reduce_for_model_failure(
    *,
    run: WorkflowRun,
    task_id: str,
    step: WorkflowStepRun,
    stop_reason: str,
    failure_message: str,
    progress_callback: KnowledgeDeepTaskProgressCallback | None,
) -> KnowledgeDeepTaskReduceRunResponse:
    failed_step = step.model_copy(
        update={
            "status": "failed",
            "message": failure_message,
            "output": {**step.output, "stop_reason": stop_reason, "failure_message": failure_message},
        }
    )
    failed_run = _replace_map_step(
        run,
        failed_step,
        status="blocked",
        summary="深度任务 Reduce 因一个节点的模型输出失败而暂停，已完成检查点将保留。",
    )
    _save_map_checkpoint(failed_run)
    append_workflow_event(
        task_id=task_id,
        event_name="knowledge_deep_reduce_failed",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        step_id=step.step_id,
        message=f"当前 Reduce 节点未完成：{failure_message}。",
        level="error",
    )
    await _notify_progress(
        progress_callback,
        "knowledge_deep_reduce_failed",
        f"一个 Reduce 节点未完成：{failure_message}；已保留进度并停止自动继续。",
        step.step_id,
        "error",
    )
    return _reduce_run_response(failed_run)


def _block_reduce_for_stale_scope(run: WorkflowRun, task_id: str, step_id: str) -> KnowledgeDeepTaskReduceRunResponse:
    blocked = _with_map_run_state(
        run,
        status="blocked",
        summary="资料库活动索引已变化，深度任务的旧 Reduce 范围不能继续；请重新创建任务。",
        finished=False,
    )
    _save_map_checkpoint(blocked)
    append_workflow_event(
        task_id=task_id,
        event_name="knowledge_deep_reduce_scope_stale",
        agent_id=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        step_id=step_id,
        message="活动资料已更新，未将旧 Map 小结合并到新索引。",
        level="warning",
    )
    return _reduce_run_response(blocked)


def _scope_is_current(scope: KnowledgeDeepTaskScope) -> bool:
    try:
        verify_knowledge_deep_task_scope(scope)
    except KnowledgeDeepTaskScopeError:
        return False
    return True


def _reset_failed_reduce_steps(run: WorkflowRun) -> WorkflowRun:
    steps = [
        step.model_copy(
            update={
                "status": "pending",
                "message": "等待客户显式恢复后的 Reduce 重试。",
                "output": {
                    key: value
                    for key, value in step.output.items()
                    if key not in {"stop_reason", "failure_message"}
                },
            }
        )
        if step.status == "failed" and step.action in {
            KNOWLEDGE_DEEP_TASK_REDUCE_BATCH_ACTION,
            KNOWLEDGE_DEEP_TASK_REDUCE_FINAL_ACTION,
        }
        else step
        for step in run.steps
    ]
    return run.model_copy(
        update={
            "status": "pending",
            "summary": "深度任务等待恢复失败的 Reduce 节点，完成的 Map 与批次将跳过。",
            "steps": steps,
            "metrics": run.metrics.model_copy(
                update={
                    "step_total": len(steps),
                    "step_completed": sum(step.status == "completed" for step in steps),
                    "step_failed": 0,
                    "finished_at": "",
                }
            ),
        }
    )


def _deep_task_limits(map_unit_count: int, reduce_step_count: int) -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(
        max_steps=map_unit_count + reduce_step_count,
        max_tool_calls=0,
        max_retries_per_tool=0,
        tool_timeout_ms=60_000,
        # 预算字段也要反映完整资料库的真实执行规模。它只描述本次可恢复任务，不会因为跨越
        # 多个供应商限流窗口就强杀已经安全落盘的 checkpoint。
        task_timeout_ms=max(1_800_000, (map_unit_count + reduce_step_count) * 120_000),
        token_budget=(map_unit_count * MAX_DEEP_TASK_MAP_OUTPUT_TOKENS)
        + (reduce_step_count * MAX_DEEP_TASK_REDUCE_OUTPUT_TOKENS),
    )


def _reduce_run_response(run: WorkflowRun) -> KnowledgeDeepTaskReduceRunResponse:
    final_step = next((step for step in _reduce_steps(run) if step.step_id == _reduce_final_step_id()), None)
    raw_result = final_step.output.get("reduce_result") if final_step is not None else None
    result = KnowledgeDeepReduceResult.model_validate(raw_result) if isinstance(raw_result, dict) else None
    return KnowledgeDeepTaskReduceRunResponse(
        task_id=run.task_id,
        status=run.status,
        completed_reduce_batch_count=sum(
            step.status == "completed" and step.action == KNOWLEDGE_DEEP_TASK_REDUCE_BATCH_ACTION
            for step in _reduce_steps(run)
        ),
        summary=run.summary,
        result=result,
    )


def _validate_map_execution_scope(scope: KnowledgeDeepTaskScope) -> None:
    # 完整范围的成本由逐章节 checkpoint、供应商限流队列和分层 Reduce 控制，不再把固定章节数
    # 当作客户可见的上限。这里仍保留独立函数，方便未来接入磁盘队列或分布式 worker 时复用同一
    # 启动前校验边界。
    if not scope.map_units:
        raise KnowledgeDeepTaskMapExecutionError("当前深度任务没有可执行的冻结章节。")


def _validate_map_run_shape(run: WorkflowRun, scope: KnowledgeDeepTaskScope) -> None:
    expected_ids = [unit.map_unit_id for unit in scope.map_units]
    map_steps = run.steps[: len(expected_ids)]
    actual_ids = [step.step_id for step in map_steps]
    if run.mode != "runtime" or actual_ids != expected_ids or any(
        step.agent != KNOWLEDGE_DEEP_TASK_AGENT_ID or step.action != KNOWLEDGE_DEEP_TASK_MAP_ACTION
        for step in map_steps
    ):
        raise KnowledgeDeepTaskMapExecutionError("已有任务与当前深度任务范围不匹配，不能错误恢复。")
    for step in map_steps:
        if step.status == "completed":
            raw_result = step.output.get("map_result")
            if not isinstance(raw_result, dict):
                raise KnowledgeDeepTaskMapExecutionError("已完成章节缺少结构化检查点，不能继续恢复。")
            result = KnowledgeDeepMapResult.model_validate(raw_result)
            if result.map_unit_id != step.step_id:
                raise KnowledgeDeepTaskMapExecutionError("章节检查点与当前 Map 单元不匹配，不能继续恢复。")


def _map_steps_for_scope(run: WorkflowRun, scope: KnowledgeDeepTaskScope) -> list[WorkflowStepRun]:
    """返回固定在任务前缀的 Map 步骤；后续 Reduce 步骤不改变其恢复身份。"""

    return run.steps[: len(scope.map_units)]


def _new_map_step(scope: KnowledgeDeepTaskScope, unit: KnowledgeDeepTaskMapUnit) -> WorkflowStepRun:
    return WorkflowStepRun(
        step_id=unit.map_unit_id,
        agent=KNOWLEDGE_DEEP_TASK_AGENT_ID,
        action=KNOWLEDGE_DEEP_TASK_MAP_ACTION,
        status="pending",
        message="等待受控章节分析。",
        output=_map_step_output(unit, task_scope=_task_scope_for_map_unit(scope, unit)),
    )


def _map_step_output(
    unit: KnowledgeDeepTaskMapUnit,
    *,
    task_scope: KnowledgeDeepTaskScope | None = None,
    context_route: KnowledgeContextRouteDecision | None = None,
    map_result: KnowledgeDeepMapResult | None = None,
    stop_reason: str = "",
    failure_message: str = "",
) -> dict[str, object]:
    """投影可审计元数据与小结，不让父块正文进入统一任务历史。"""

    output: dict[str, object] = {
        "map_unit_id": unit.map_unit_id,
        "parent_chunk_id": unit.parent_chunk_id,
        "document_id": unit.document_id,
        "document_version_id": unit.document_version_id,
        "document_name": unit.document_name,
        "parent_ordinal": unit.parent_ordinal,
        "source": unit.source.model_dump(mode="json"),
        "character_count": unit.character_count,
        "original_files_unchanged": True,
    }
    if task_scope is not None:
        # scope 只含 stable ID、来源锚点和任务目标。首个 Map 步骤承担这个 checkpoint，服务重启后
        # 能恢复深度任务；正文仍只在一次模型调用的内存消息中存在。
        output["deep_task_scope"] = task_scope.model_dump(mode="json")
    if context_route is not None:
        # 仅记录字符预算、路由和 Provider 能力状态；模型可见章节正文永不进入任务历史。
        output["context_route"] = context_route.model_dump(mode="json")
    if map_result is not None:
        output["map_result"] = map_result.model_dump(mode="json")
    if stop_reason:
        output["stop_reason"] = stop_reason
    if failure_message:
        output["failure_message"] = failure_message
    return output


def _task_scope_for_map_unit(
    scope: KnowledgeDeepTaskScope,
    unit: KnowledgeDeepTaskMapUnit,
) -> KnowledgeDeepTaskScope | None:
    """只在第一个 Map step 保存一次 scope，避免把同一无正文清单重复写入每章 checkpoint。"""

    return scope if unit.map_unit_id == scope.map_units[0].map_unit_id else None


def _load_active_map_parent_content(
    scope: KnowledgeDeepTaskScope,
    unit: KnowledgeDeepTaskMapUnit,
    *,
    include_content: bool = True,
) -> str:
    """仅在活动 generation 与冻结范围一致时读取一个父章节。

    这不是通用“按 ID 读取数据库”的接口：SQL 同时绑定资料库、活动 generation、文档版本和
    parent ID。即使恢复任务持有一个旧 ID，也拿不到新 generation 或其它资料库的正文。
    """

    if unit not in scope.map_units:
        raise KnowledgeDeepTaskScopeError("当前章节不属于该深度任务冻结范围。")
    select_content = ", parent.content" if include_content else ""
    with get_connection() as connection:
        row = connection.execute(
            f"""
            SELECT parent.parent_chunk_id, parent.document_id, parent.document_version_id{select_content}
            FROM knowledge_bases AS base
            INNER JOIN knowledge_index_generations AS generation
                ON generation.knowledge_base_id = base.knowledge_base_id
                AND generation.generation_number = base.active_index_generation
                AND generation.status = 'ready'
            INNER JOIN knowledge_generation_documents AS member
                ON member.index_generation_id = generation.index_generation_id
            INNER JOIN knowledge_parent_chunks AS parent
                ON parent.document_version_id = member.document_version_id
                AND parent.knowledge_base_id = base.knowledge_base_id
            WHERE base.knowledge_base_id = ?
                AND generation.index_generation_id = ?
                AND parent.parent_chunk_id = ?
                AND parent.document_id = ?
                AND parent.document_version_id = ?
            """,
            (
                scope.knowledge_base_id,
                scope.index_generation_id,
                unit.parent_chunk_id,
                unit.document_id,
                unit.document_version_id,
            ),
        ).fetchone()
    if row is None:
        raise KnowledgeDeepTaskScopeStaleError(
            "资料库活动索引或当前章节已变化，旧深度任务不能继续读取正文。"
        )
    if not include_content:
        return ""
    content = str(row["content"])
    if not content:
        raise KnowledgeDeepTaskScopeError("当前章节正文为空，未向模型发送不完整材料。")
    return content[:MAX_DEEP_TASK_MAP_CONTEXT_CHARS]


def _map_system_prompt(scope: KnowledgeDeepTaskScope, unit: KnowledgeDeepTaskMapUnit) -> str:
    return (
        "你是 AgentFlow 知识库的章节级分析器。只能分析当前一次提供的单个章节，"
        "不能把常识、其它章节或未提供资料写成事实。请只输出一个 JSON object，且只包含 summary、"
        "findings、warnings 三个字段。summary 是不超过 450 个中文字符的章节小结；findings 是最多"
        "4 条短字符串，分别写对最终摘要、比较或审查有价值的事实、约束、风险或缺口；warnings 是"
        "最多 2 条短字符串。没有可靠发现时返回空数组。不要输出 map_unit_id、source_id、编号、"
        "Markdown、代码围栏或解释文字。"
        f"task_kind={scope.task_kind}。"
    )


def _map_user_message(scope: KnowledgeDeepTaskScope, unit: KnowledgeDeepTaskMapUnit, parent_content: str) -> str:
    """构造仅含单章节正文的模型输入；正文不进入 checkpoint 或事件。"""

    return json.dumps(
        {
            "task_goal": scope.task_goal,
            "task_kind": scope.task_kind,
            "map_unit": {
                "map_unit_id": unit.map_unit_id,
                "document_name": unit.document_name,
                "heading_path": unit.heading_path,
                "source": unit.source.model_dump(mode="json"),
            },
            "chapter_content": parent_content,
            "output_contract": {
                "summary": "章节小结字符串，最多 450 个中文字符",
                "findings": ["最多 4 条短字符串"],
                "warnings": ["最多 2 条短字符串"],
            },
        },
        ensure_ascii=False,
    )


def _map_result_from_draft(unit: KnowledgeDeepTaskMapUnit, draft: KnowledgeDeepMapDraft) -> KnowledgeDeepMapResult:
    """把模型的内容草稿投影为带冻结来源的正式 Map checkpoint。

    来源 ID 与发现编号由 Runtime 单向写入，模型无法跨章节扩展来源，也不再会因复制动态 ID 失败。
    这里同时收紧长度和数量，保证后续 Reduce 的输入预算稳定。
    """

    source_ids = [unit.map_unit_id]
    findings = [item.strip()[:360] for item in draft.findings if item.strip()][:4]
    warnings = [item.strip()[:240] for item in draft.warnings if item.strip()][:4]
    return KnowledgeDeepMapResult(
        map_unit_id=unit.map_unit_id,
        summary=draft.summary.strip()[:1_200],
        findings=[
            KnowledgeDeepMapFinding(
                finding_id=f"kb_map_finding_{index}",
                statement=statement,
                source_ids=source_ids,
            )
            for index, statement in enumerate(findings, start=1)
        ],
        source_ids=source_ids,
        warnings=warnings,
    )


def _find_map_step(run: WorkflowRun, map_unit_id: str) -> WorkflowStepRun:
    for step in run.steps:
        if step.step_id == map_unit_id:
            return step
    raise KnowledgeDeepTaskMapExecutionError("任务检查点缺少当前章节步骤，不能继续恢复。")


def _replace_map_step(
    run: WorkflowRun,
    replacement: WorkflowStepRun,
    *,
    status: str,
    summary: str,
) -> WorkflowRun:
    steps = [replacement if step.step_id == replacement.step_id else step for step in run.steps]
    completed = sum(step.status == "completed" for step in steps)
    failed = sum(step.status == "failed" for step in steps)
    return run.model_copy(
        update={
            "status": status,
            "summary": summary,
            "steps": steps,
            "metrics": run.metrics.model_copy(
                update={"step_total": len(steps), "step_completed": completed, "step_failed": failed}
            ),
        }
    )


def _with_map_run_state(
    run: WorkflowRun,
    *,
    status: str,
    summary: str,
    finished: bool,
) -> WorkflowRun:
    completed = sum(step.status == "completed" for step in run.steps)
    failed = sum(step.status == "failed" for step in run.steps)
    metric_updates: dict[str, object] = {
        "step_total": len(run.steps),
        "step_completed": completed,
        "step_failed": failed,
        # K5.8 只保存任务从受理到当前 checkpoint 的真实墙钟耗时。它可能包含排队或暂停，
        # 因此性能面板会把它标为端到端任务事实，不会伪装成单次 Provider 调用耗时。
        "duration_ms": _runtime_duration_ms(run.metrics.started_at),
    }
    if finished:
        metric_updates["finished_at"] = _now()
    elif status in {"pending", "running", "paused", "blocked"}:
        # Map 结束后会进入 Reduce，或客户会从暂停/阻塞显式继续；这些都不是最终完成时间。
        # 清空旧值可避免任务详情显示“已结束”却仍在运行或等待用户操作。
        metric_updates["finished_at"] = ""
    return run.model_copy(
        update={
            "status": status,
            "summary": summary,
            "metrics": run.metrics.model_copy(update=metric_updates),
        }
    )


def _reset_failed_map_steps(run: WorkflowRun) -> WorkflowRun:
    """仅在调用方再次进入同一 task 时允许失败章节重试。"""

    steps = [
        step.model_copy(
            update={
                "status": "pending",
                "message": "等待客户显式恢复后的章节重试。",
                "output": {
                    key: value
                    for key, value in step.output.items()
                    if key not in {"stop_reason", "failure_message"}
                },
            }
        )
        if step.status == "failed"
        else step
        for step in run.steps
    ]
    return run.model_copy(
        update={
            "status": "pending",
            "summary": "知识库深度任务等待恢复失败章节，已完成章节将跳过。",
            "steps": steps,
            "metrics": run.metrics.model_copy(
                update={
                    "step_total": len(steps),
                    "step_completed": sum(step.status == "completed" for step in steps),
                    "step_failed": 0,
                    "finished_at": "",
                }
            ),
        }
    )


def _map_execution_limits(map_unit_count: int) -> RuntimeExecutionLimits:
    return RuntimeExecutionLimits(
        max_steps=map_unit_count,
        max_tool_calls=0,
        max_retries_per_tool=0,
        tool_timeout_ms=60_000,
        task_timeout_ms=max(1_800_000, map_unit_count * 120_000),
        token_budget=map_unit_count * MAX_DEEP_TASK_MAP_OUTPUT_TOKENS,
    )


def _save_map_checkpoint(run: WorkflowRun) -> None:
    # 该写入是短 SQLite 事务，发生在模型调用前后；长调用期间不持有数据库连接或锁。
    save_workflow_runtime_checkpoint(
        run=run,
        plan=None,
        permission_requests=[],
        artifacts=[],
        tool_calls=[],
    )


def _map_run_response(
    run: WorkflowRun,
    *,
    phase_status: str | None = None,
) -> KnowledgeDeepTaskMapRunResponse:
    """构造 Map 阶段回执；阶段完成不等同于整个 Map/Reduce 工作流终态。"""

    return KnowledgeDeepTaskMapRunResponse(
        task_id=run.task_id,
        status=phase_status or run.status,
        completed_map_count=sum(
            step.status == "completed" and step.action == KNOWLEDGE_DEEP_TASK_MAP_ACTION
            for step in run.steps
        ),
        failed_map_unit_ids=[
            step.step_id
            for step in run.steps
            if step.status == "failed" and step.action == KNOWLEDGE_DEEP_TASK_MAP_ACTION
        ],
        summary=run.summary,
    )


async def _notify_progress(
    callback: KnowledgeDeepTaskProgressCallback | None,
    event: str,
    message: str,
    step_id: str | None,
    level: TaskLogLevel,
) -> None:
    if callback is None:
        return
    try:
        await callback(event, message, step_id, level)
    except Exception:
        # 观察面异常不能让已落盘的 Map checkpoint 回滚或中断后续恢复。
        return


def _knowledge_deep_map_model_with_output_budget(runtime: ModelRuntime) -> ModelRuntime:
    """Map 只提升足以容纳章节小结的输出预算，不修改客户保存的 provider 偏好。"""

    return replace(runtime, max_tokens=max(runtime.max_tokens, MAX_DEEP_TASK_MAP_OUTPUT_TOKENS))


def _knowledge_deep_reduce_model_with_output_budget(runtime: ModelRuntime) -> ModelRuntime:
    """Reduce 只提升汇总 JSON 所需输出预算，保持客户保存的 provider 配置不变。"""

    return replace(runtime, max_tokens=max(runtime.max_tokens, MAX_DEEP_TASK_REDUCE_OUTPUT_TOKENS))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _runtime_duration_ms(started_at: str) -> int:
    """将持久化的 ISO 开始时间换算为任务端到端毫秒数，异常历史记录安全降级为零。"""

    if not started_at:
        return 0
    try:
        parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, round((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds() * 1_000))
