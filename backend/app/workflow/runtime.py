from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from app.core.config import settings
from app.database.task_repository import (
    list_workflow_artifacts,
    list_workflow_tool_calls,
    append_workflow_event,
    get_runtime_execution_control,
    list_runtime_permission_requests,
    load_task_log_events,
    load_workflow_plan,
    load_workflow_run,
    record_runtime_permission_decision,
    save_workflow_runtime_checkpoint,
    set_runtime_execution_control,
)
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.data_agent import DataAnalysisPreviewRequest
from app.schemas.document_agent import DocumentAgentRunRequest
from app.schemas.knowledge import KnowledgeAnswerRequest, KnowledgeDeepTaskRequest
from app.schemas.events import TaskLogEvent, TaskLogLevel
from app.schemas.workflow import (
    RuntimeExecutionMetrics,
    RuntimeExecutionLimits,
    RuntimePermissionDecisionInput,
    RuntimePermissionItem,
    RuntimePermissionRequest,
    WorkflowArtifact,
    WorkflowExecutionResponse,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.workspace_documents import (
    WorkspaceDocumentError,
    read_workspace_document_preview,
    search_workspace_documents,
)
from app.services.document_agent import run_document_agent
from app.services.data_analysis_delegate import (
    create_data_analysis_preview_queued_run,
    run_data_analysis_preview_task,
)
from app.services.knowledge_answer import (
    create_knowledge_answer_queued_run,
    get_knowledge_answer_task_result,
    run_knowledge_answer_task,
)
from app.services.conversation_memory import persist_async_assistant_delivery
from app.services.knowledge_deep_dispatch import (
    KnowledgeDeepTaskDispatchError,
    start_knowledge_deep_task_in_background,
)
from app.workflow.dry_run import clear_dry_run_memory_cache
from app.workflow.node_contracts import tool_name_for_step
from app.workflow.permission_policy import PermissionPolicyDecision, evaluate_permission_policy
from app.workflow.state_machine import is_terminal_status


_PERMISSION_LABELS: dict[str, str] = {
    "file_read": "文件读取",
    "file_write": "文件写入",
    "network": "网络访问",
    "shell": "Shell 命令",
    "database": "数据库访问",
    "plugin_install": "插件安装",
    "knowledge_deep_analysis": "全库深度分析预算",
}
_TEXT_FILE_SUFFIXES = {".txt", ".md", ".markdown"}
_MAX_TEXT_FILE_BYTES = 1_000_000
_MAX_TOOL_ATTEMPTS = 3
_TOOL_TIMEOUT_MS = 30_000
# C6.4 只为彼此独立、只读且已有正式子任务入口的专业动作开放小规模并行。这里不是
# “所有步骤都可以并发”的开关：深度分析、导出、OCR、联网和任何写入型动作仍必须走各自的
# 权限、检查点与恢复链路。
_COMPOSITION_MAX_PARALLELISM = 2
_COMPOSITION_MAX_SPECIALIST_STEPS = 3
_COMPOSITION_MAX_PARENT_TOOL_CALLS = 12
_COMPOSITION_SAFE_ACTIONS = {
    ("document_agent", "analyze_document"),
    ("document_agent", "search_text"),
    ("data_agent", "analyze_dataset"),
    ("knowledge_agent", "answer_question"),
}
_NON_RETRYABLE_ERROR_CODES = {
    "artifact_verification_failed",
    "empty_query",
    "invalid_parameters",
    "missing_document_context",
    "path_outside_workspace",
    "unsupported_file_type",
    "file_not_found",
    "file_too_large",
}

RuntimeEventReporter = Callable[[TaskLogEvent], None]


def execute_workflow_runtime(task_id: str) -> WorkflowExecutionResponse | None:
    """执行或恢复一个真实 Runtime 任务。

    这是阶段 4B 的最小执行器：从 dry-run 任务创建新的 runtime task；如果传入的已经是
    runtime task，则在权限批准后继续执行同一个 task_id。当前只执行安全内置工具，
    不执行用户代码、不运行 Shell、不联网、不加载插件。
    """

    prepared = prepare_workflow_runtime(task_id)
    if prepared is None or not prepared.accepted:
        return prepared

    # 兼容旧的 /execute：仍同步返回最终状态。新的 /start 会复用同一准备与执行函数，
    # 但交给后台 Job 管理器运行，桌面端不需要为过渡期维护两套 Runtime 规则。
    run = run_prepared_workflow_runtime(
        runtime_task_id=prepared.runtime_task_id,
        source_task_id=prepared.source_task_id,
    )
    return prepared.model_copy(
        update={
            "status": run.status,
            "message": _execution_message(run),
            "workflow_run": run,
        }
    )


def prepare_workflow_runtime(task_id: str) -> WorkflowExecutionResponse | None:
    """创建或恢复一个可后台执行的 Runtime 检查点。

    此函数只做 SQLite 短写入，不触发 Tool 或模型调用。调用方拿到 ``runtime_task_id`` 后即可
    建立 WebSocket，再把耗时执行交给后台线程；这避免“请求长时间卡住，客户端却不知道任务 ID”
    的黑盒体验。
    """

    source_run = load_workflow_run(task_id)
    plan = load_workflow_plan(task_id)
    if source_run is None or plan is None:
        return None

    # 旧的 C6.3 计划可能仍标记为 requires_composition_runtime。C6.4 仅兼容其中严格受限的
    # 只读组合；其它组合继续拒绝，不能因为新调度器存在就把深度分析、导出或写入型步骤抢跑。
    if (
        plan.execution_readiness == "requires_composition_runtime"
        and not _supports_native_composition_runtime(plan)
    ):
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=False,
            status=source_run.status,
            message=(
                "当前是多材料组合计划，已展示依赖与并行边界；"
                "组合 Runtime 与最终汇总尚未启用，不能提前执行。"
            ),
            workflow_run=source_run,
        )

    if source_run.mode == "dry_run":
        runtime_task_id = f"{task_id}_runtime_{uuid4().hex[:8]}"
        permission_requests = _build_permission_requests(runtime_task_id, plan)
        initial_run = _new_runtime_run(runtime_task_id=runtime_task_id, plan=plan)
        save_workflow_runtime_checkpoint(
            run=initial_run,
            plan=plan,
            permission_requests=permission_requests,
            artifacts=[],
            tool_calls=[],
        )
        set_runtime_execution_control(
            task_id=runtime_task_id,
            pause_requested=False,
            cancel_requested=False,
        )
        _append_runtime_event(
            runtime_task_id,
            [],
            "connected",
            "system",
            "已连接 AgentFlow Runtime 日志通道。",
        )
        _append_runtime_event(
            runtime_task_id,
            [],
            "task_queued",
            "workflow_engine",
            "Runtime 已进入执行队列，正在等待安全执行槽位。",
        )
        clear_dry_run_memory_cache()
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=runtime_task_id,
            accepted=True,
            status="pending",
            message="Runtime 已受理，正在准备执行。",
            workflow_run=initial_run,
        )

    if is_terminal_status(source_run.status):
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=False,
            status=source_run.status,
            message="该 runtime 任务已经结束，请使用 retry 创建新的执行记录。",
            workflow_run=source_run,
        )

    control = get_runtime_execution_control(task_id)
    if control is None:
        return None
    if control.cancel_requested:
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=False,
            status=source_run.status,
            message="该 Runtime 已收到取消请求，不能继续执行。",
            workflow_run=source_run,
        )
    if source_run.status == "waiting_permission":
        decisions = _permission_decisions_by_step(task_id)
        if not any(value in {"approved", "denied"} for value in decisions.values()):
            return WorkflowExecutionResponse(
                source_task_id=task_id,
                runtime_task_id=task_id,
                accepted=False,
                status="waiting_permission",
                message="任务仍在等待权限确认，确认或拒绝后才能继续。",
                workflow_run=source_run,
            )
    if source_run.status == "blocked":
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=False,
            status="blocked",
            message="任务已被策略或材料问题阻塞；请按提示补充后重新规划或 retry。",
            workflow_run=source_run,
        )

    # paused / waiting_permission 的继续必须复用原 task、步骤和产物；不能新建一条“看起来
    # 成功”的任务，也不能把用户授权或已完成步骤重置。
    queued_run = source_run.model_copy(
        update={
            "status": "pending",
            "summary": "Runtime 已恢复到执行队列，将从上一个安全检查点继续。",
        }
    )
    permission_requests = _build_permission_requests(task_id, plan)
    save_workflow_runtime_checkpoint(
        run=queued_run,
        plan=plan,
        permission_requests=permission_requests,
        artifacts=list_workflow_artifacts(task_id),
        tool_calls=list_workflow_tool_calls(task_id),
    )
    set_runtime_execution_control(task_id=task_id, pause_requested=False)
    _append_runtime_event(
        task_id,
        [],
        "task_resume_queued",
        "workflow_engine",
        "已收到继续请求，Runtime 将从已完成步骤之后恢复。",
    )
    clear_dry_run_memory_cache()
    return WorkflowExecutionResponse(
        source_task_id=task_id,
        runtime_task_id=task_id,
        accepted=True,
        status="pending",
        message="Runtime 已重新进入执行队列。",
        workflow_run=queued_run,
    )


def run_prepared_workflow_runtime(
    *,
    runtime_task_id: str,
    source_task_id: str,
    event_reporter: RuntimeEventReporter | None = None,
) -> WorkflowRun:
    """运行已受理的 Runtime；供同步兼容入口与后台 Job 共用。"""

    plan = load_workflow_plan(runtime_task_id)
    if plan is None:
        raise RuntimeError(f"Runtime task '{runtime_task_id}' lost its workflow plan.")
    run = _run_plan(
        runtime_task_id=runtime_task_id,
        source_task_id=source_task_id,
        plan=plan,
        event_reporter=event_reporter,
    )
    _persist_direct_knowledge_answer_delivery(plan=plan, run=run)
    return run


def _run_plan(
    *,
    runtime_task_id: str,
    source_task_id: str,
    plan: WorkflowPlan,
    event_reporter: RuntimeEventReporter | None = None,
) -> WorkflowRun:
    """按计划类型选择受控调度器。

    常规计划继续使用已经稳定的顺序 Runtime。只有 C6.4 明确列白名单的组合计划才进入
    并行调度器，避免一个新字段把旧计划的执行语义整体改变。
    """

    if _supports_native_composition_runtime(plan):
        return _run_composition_plan(
            runtime_task_id=runtime_task_id,
            source_task_id=source_task_id,
            plan=plan,
            event_reporter=event_reporter,
        )
    return _run_sequential_plan(
        runtime_task_id=runtime_task_id,
        source_task_id=source_task_id,
        plan=plan,
        event_reporter=event_reporter,
    )


def _is_direct_knowledge_answer_plan(plan: WorkflowPlan) -> bool:
    """识别唯一可自动交付到会话的单资料库只读问答。

    这是服务端最终准入，不能信任 Qt 的展示判断。任何写入、外部服务、确认步骤或多专业
    Agent 组合都不走这条路径，避免把复杂任务的局部子结果误写成客户的最终对话答案。
    """

    if (
        plan.execution_readiness != "ready"
        or plan.requires_confirmation
        or plan.workspace_scope.write_paths
        or plan.workspace_scope.external_services
    ):
        return False
    specialists = [step for step in plan.steps if step.agent != "commander_agent"]
    if len(specialists) != 1:
        return False
    step = specialists[0]
    return (
        not step.requires_confirmation
        and step.agent == "knowledge_agent"
        and step.action == "answer_question"
        and step.execution_mode == "execute"
        and bool(str(step.input.get("knowledge_base_id", "")).strip())
    )


def _persist_direct_knowledge_answer_delivery(*, plan: WorkflowPlan, run: WorkflowRun) -> None:
    """把已通过 K3 Gate 的最终正文追加到同一会话，而不是把父任务短摘要当作回答。"""

    if (
        run.status != "completed"
        or not plan.conversation_id
        or not _is_direct_knowledge_answer_plan(plan)
    ):
        return
    knowledge_step = next(step for step in plan.steps if step.agent == "knowledge_agent")
    step_run = next((item for item in run.steps if item.step_id == knowledge_step.id), None)
    result = step_run.output.get("result") if step_run is not None else None
    delegated_task_id = str(result.get("delegated_task_id", "")) if isinstance(result, dict) else ""
    if not delegated_task_id:
        return

    try:
        delegated = get_knowledge_answer_task_result(delegated_task_id)
        answer = delegated.result.answer if delegated is not None and delegated.result is not None else None
        if delegated is None or delegated.status != "completed" or answer is None:
            return
        source_lines: list[str] = []
        for source in delegated.result.evidence_gate.sources[:4]:
            anchor = source.source
            label = (
                f"第 {anchor.source_locator} 页"
                if anchor.source_kind == "page"
                else f"第 {anchor.source_locator} 行"
                if anchor.source_kind == "line"
                else f"第 {anchor.source_locator} 段"
                if anchor.source_kind == "paragraph"
                else anchor.source_locator
            )
            source_lines.append(f"- {source.document_name} · {label or '可定位片段'}")
        delivery = "## 基于已选资料库的回答\n\n" + answer.answer_markdown
        if source_lines:
            delivery += "\n\n### 参考来源\n" + "\n".join(source_lines)
        # 会话归档不可用不应撤销已经完成并写入任务历史的专业问答；下一次恢复或新问题仍可从
        # 任务历史访问该子任务，稳定 message_id 也会防止恢复后产生重复交付。
        persist_async_assistant_delivery(
            conversation_id=plan.conversation_id,
            task_id=delegated_task_id,
            assistant_message=delivery,
        )
    except Exception:
        return


def _composition_specialist_steps(plan: WorkflowPlan) -> list[WorkflowStep]:
    """返回 C6.4 明确允许并行的专业步骤，顺序仍以计划顺序为准。"""

    return [
        step
        for step in plan.steps
        if step.parallel_group == "specialist_read_only"
    ]


def _composition_synthesis_step(plan: WorkflowPlan) -> WorkflowStep | None:
    return next(
        (
            step
            for step in plan.steps
            if step.agent == "commander_agent" and step.action == "synthesize_results"
        ),
        None,
    )


def _supports_native_composition_runtime(plan: WorkflowPlan) -> bool:
    """判断计划是否属于当前 Native 组合 Runtime 的窄白名单。

    这层检查同时服务于旧计划兼容与服务端准入。它不信任 UI 的 readiness 字段，而是重新
    核验 DAG、步骤数量、动作类型和汇总依赖，避免保存过的计划或手工请求扩大并行权限。
    """

    specialists = _composition_specialist_steps(plan)
    synthesis = _composition_synthesis_step(plan)
    if not (2 <= len(specialists) <= _COMPOSITION_MAX_SPECIALIST_STEPS):
        return False
    if synthesis is None:
        return False
    specialist_ids = {step.id for step in specialists}
    if set(synthesis.depends_on) != specialist_ids:
        return False
    if any((step.agent, step.action) not in _COMPOSITION_SAFE_ACTIONS for step in specialists):
        return False
    if any(step.depends_on != ["step_1"] for step in specialists):
        return False

    non_specialist_steps = [
        step
        for step in plan.steps
        if step.id not in specialist_ids and step.id != synthesis.id
    ]
    return (
        len(non_specialist_steps) == 1
        and non_specialist_steps[0].id == "step_1"
        and non_specialist_steps[0].agent == "commander_agent"
        and non_specialist_steps[0].action == "analyze_task"
        and not synthesis.required_permissions
    )


def _run_composition_plan(
    *,
    runtime_task_id: str,
    source_task_id: str,
    plan: WorkflowPlan,
    event_reporter: RuntimeEventReporter | None = None,
) -> WorkflowRun:
    """执行 C6.4 的受控组合计划。

    父任务只调度白名单中的只读子任务。每个子任务仍沿用自己的任务历史、来源和验证器；
    父任务仅保存短摘要、关联 task ID 和状态。即使一个分支失败，另一个独立分支也会继续，
    最终汇总绝不读取失败分支的正文或假装它已完成。
    """

    # ``source_task_id`` 是后台 Job 的来源审计参数。组合 Runtime 复用现有 parent task，
    # 不需要把它写进模型上下文或子任务输入。
    _ = source_task_id
    previous_run = load_workflow_run(runtime_task_id)
    resuming = previous_run is not None and previous_run.mode == "runtime"
    previous_steps = {
        step_run.step_id: step_run
        for step_run in (previous_run.steps if resuming else [])
        if step_run.status == "completed"
    }
    started_at = _runtime_started_at(previous_run) if resuming else datetime.now(UTC)
    permission_requests = _build_permission_requests(runtime_task_id, plan)
    decisions = _permission_decisions_by_step(runtime_task_id)
    policy_decisions = {
        step.id: evaluate_permission_policy(
            permission_policy=plan.preference_applied.permission_policy,
            step=step,
        )
        for step in plan.steps
    }
    events = load_task_log_events(runtime_task_id) or []
    tool_calls = [
        call
        for call in (list_workflow_tool_calls(runtime_task_id) if resuming else [])
        if call.step_id in previous_steps
    ]
    artifacts = [
        artifact
        for artifact in (list_workflow_artifacts(runtime_task_id) if resuming else [])
        if artifact.step_id in previous_steps
    ]
    step_states: dict[str, WorkflowStepRun] = dict(previous_steps)
    runtime_context: dict[str, dict[str, object]] = {}
    plan_steps_by_id = {step.id: step for step in plan.steps}
    for step_id, step_run in previous_steps.items():
        plan_step = plan_steps_by_id.get(step_id)
        if plan_step is not None:
            _remember_step_result(runtime_context, plan_step, step_run)

    specialists = _composition_specialist_steps(plan)
    synthesis = _composition_synthesis_step(plan)
    assert synthesis is not None  # 已由 _supports_native_composition_runtime 进行守卫。
    root_step = plan_steps_by_id["step_1"]
    status = "running"
    summary = "组合 Runtime 正在执行受控专业任务。"

    def ordered_steps() -> list[WorkflowStepRun]:
        return [step_states.get(step.id, _pending_step(step)) for step in plan.steps]

    def save_progress() -> None:
        _save_runtime_progress_checkpoint(
            runtime_task_id=runtime_task_id,
            plan=plan,
            status=status,
            summary=summary,
            steps=ordered_steps(),
            tool_calls=tool_calls,
            artifacts=artifacts,
            permission_requests=permission_requests,
            started_at=started_at,
        )

    def finish(final_status: str, final_summary: str, *, event: str, level: TaskLogLevel = "info") -> WorkflowRun:
        final_steps = ordered_steps()
        run = WorkflowRun(
            task_id=runtime_task_id,
            mode="runtime",
            status=final_status,  # type: ignore[arg-type]
            summary=final_summary,
            max_risk_level=plan.max_risk_level,
            requires_confirmation=plan.requires_confirmation,
            validation_errors=[],
            steps=final_steps,
            limits=_runtime_execution_limits(plan),
            metrics=_build_runtime_metrics(
                steps=final_steps,
                tool_calls=tool_calls,
                permission_requests=permission_requests,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            ),
        )
        save_workflow_runtime_checkpoint(
            run=run,
            plan=plan,
            permission_requests=permission_requests,
            artifacts=artifacts,
            tool_calls=tool_calls,
        )
        _record_policy_permission_decisions(
            task_id=runtime_task_id,
            permission_requests=permission_requests,
            existing_decisions=decisions,
            policy_decisions=policy_decisions,
        )
        _append_runtime_event(
            runtime_task_id,
            events,
            event,
            "workflow_engine",
            final_summary,
            level=level,
            reporter=event_reporter,
        )
        clear_dry_run_memory_cache()
        return run

    def control_requested() -> str | None:
        control = get_runtime_execution_control(runtime_task_id)
        if control is None:
            return "cancelled"
        if control.cancel_requested:
            return "cancelled"
        if control.pause_requested:
            return "paused"
        return None

    def stop_for_control() -> WorkflowRun | None:
        requested = control_requested()
        if requested == "cancelled":
            return finish(
                "cancelled",
                "组合任务已按用户请求取消；已完成子任务不会被重复执行。",
                event="task_cancelled",
                level="warning",
            )
        if requested == "paused":
            return finish(
                "paused",
                "组合任务已在安全检查点暂停；已完成子任务和审计记录已保留。",
                event="task_paused",
                level="warning",
            )
        if _elapsed_ms(started_at, datetime.now(UTC)) >= _runtime_execution_limits(plan).task_timeout_ms:
            return finish(
                "blocked",
                "组合任务已达到共享时间预算；已完成子任务会保留，未完成分支可在复核后重试。",
                event="task_waiting",
                level="warning",
            )
        return None

    def preflight_step(step: WorkflowStep) -> WorkflowRun | None:
        """在启动 Tool 前执行同一套权限检查，避免并发分支绕过治理层。"""

        nonlocal status, summary
        policy_decision = policy_decisions[step.id]
        decision = decisions.get(step.id)
        if policy_decision.action == "block":
            blocked_step, blocked_call = _blocked_step(
                runtime_task_id=runtime_task_id,
                step=step,
                reason=policy_decision.reason,
            )
            step_states[step.id] = blocked_step
            tool_calls.append(blocked_call)
            status = "blocked"
            summary = f"组合任务已阻塞：{step.title} 未通过平台权限策略。"
            save_progress()
            return finish("blocked", summary, event="task_waiting", level="warning")
        if step.requires_confirmation or policy_decision.action == "confirm":
            if decision == "denied":
                blocked_step, blocked_call = _blocked_step(
                    runtime_task_id=runtime_task_id,
                    step=step,
                    reason="用户拒绝了该步骤所需权限。",
                )
                step_states[step.id] = blocked_step
                tool_calls.append(blocked_call)
                status = "blocked"
                summary = f"组合任务已阻塞：{step.title} 的权限被拒绝。"
                save_progress()
                return finish("blocked", summary, event="task_waiting", level="warning")
            if policy_decision.action == "confirm" and decision != "approved":
                waiting_step, waiting_call = _waiting_permission_step(runtime_task_id, step)
                step_states[step.id] = waiting_step
                tool_calls.append(waiting_call)
                status = "waiting_permission"
                summary = f"组合任务正在等待权限确认：{step.title}。"
                save_progress()
                return finish("waiting_permission", summary, event="task_waiting", level="warning")
            if policy_decision.action == "allow" and decision != "approved":
                _append_runtime_event(
                    runtime_task_id,
                    events,
                    "permission_auto_approved",
                    "governance",
                    f"步骤 {step.title} 已按“{policy_decision.policy}”策略自动批准，并保留审计。",
                    step.id,
                    reporter=event_reporter,
                )
        return None

    _append_runtime_event(
        runtime_task_id,
        events,
        "task_resumed" if previous_steps else "task_started",
        "workflow_engine",
        "组合 Runtime 已开始；只读专业步骤会在有限并发槽位内执行。",
        reporter=event_reporter,
    )

    stopped_run = stop_for_control()
    if stopped_run is not None:
        return stopped_run

    # 任务分析节点已经在 dry-run 中形成计划；Runtime 仍记录该无副作用步骤已被消费，保持
    # DAG 事实完整，并让恢复逻辑能以同一检查点判断专业分支是否可以启动。
    if root_step.id not in step_states:
        preflight_result = preflight_step(root_step)
        if preflight_result is not None:
            return preflight_result
        step_states[root_step.id] = _running_step(root_step)
        save_progress()
        _append_runtime_event(
            runtime_task_id,
            events,
            "step_started",
            root_step.agent,
            f"开始执行安全工具：{root_step.title}",
            root_step.id,
            reporter=event_reporter,
        )
        root_run, root_call, root_artifacts = _execute_safe_step_with_retries(
            runtime_task_id=runtime_task_id,
            step=root_step,
            plan=plan,
            output_dir=settings.data_dir / "outputs" / runtime_task_id,
            runtime_context=runtime_context,
        )
        step_states[root_step.id] = root_run
        tool_calls.append(root_call)
        artifacts.extend(root_artifacts)
        if root_run.status != "completed":
            status = "blocked" if root_run.status == "blocked" else "failed"
            summary = f"组合任务无法启动：{root_run.message}"
            save_progress()
            return finish(
                status,
                summary,
                event="task_waiting" if status == "blocked" else "task_failed",
                level="warning" if status == "blocked" else "error",
            )
        _remember_step_result(runtime_context, root_step, root_run)
        _append_runtime_event(
            runtime_task_id,
            events,
            "step_completed",
            root_step.agent,
            root_run.message,
            root_step.id,
            reporter=event_reporter,
        )

    stopped_run = stop_for_control()
    if stopped_run is not None:
        return stopped_run

    pending_specialists = [step for step in specialists if step.id not in step_states]
    if pending_specialists:
        # 在提交线程池前一次性检查本父任务的并行数量与 parent Tool 配额。该预留发生在任何
        # 子任务创建之前，避免部分子任务已消耗额度后才发现本次组合无预算。
        reserved_tool_calls = len(pending_specialists) + 1  # 每个子任务 + 一次最终汇总。
        if len(tool_calls) + reserved_tool_calls > _COMPOSITION_MAX_PARENT_TOOL_CALLS:
            status = "blocked"
            summary = "组合任务的共享 Tool 预算不足，未启动新的专业子任务。"
            save_progress()
            return finish("blocked", summary, event="task_waiting", level="warning")

        for step in pending_specialists:
            preflight_result = preflight_step(step)
            if preflight_result is not None:
                return preflight_result

        for step in pending_specialists:
            step_states[step.id] = _running_step(step)
        summary = (
            f"正在并行执行 {len(pending_specialists)} 个只读专业步骤；"
            f"本轮最多使用 {_COMPOSITION_MAX_PARALLELISM} 个执行槽位。"
        )
        save_progress()
        _append_runtime_event(
            runtime_task_id,
            events,
            "composition_group_started",
            "workflow_engine",
            summary,
            reporter=event_reporter,
        )
        for step in pending_specialists:
            _append_runtime_event(
                runtime_task_id,
                events,
                "step_started",
                step.agent,
                f"开始执行组合子任务：{step.title}",
                step.id,
                reporter=event_reporter,
            )

        def execute_specialist(step: WorkflowStep) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
            # 每个子任务在独立线程内创建自己的 async loop；父任务的 SQLite 检查点仍只由当前
            # 调度线程写入，避免多个分支并发改写同一 parent 快照。
            return _execute_safe_step_with_retries(
                runtime_task_id=runtime_task_id,
                step=step,
                plan=plan,
                output_dir=settings.data_dir / "outputs" / runtime_task_id,
                runtime_context={},
            )

        with ThreadPoolExecutor(
            max_workers=min(_COMPOSITION_MAX_PARALLELISM, len(pending_specialists)),
            thread_name_prefix="agentflow-composition",
        ) as executor:
            futures = {executor.submit(execute_specialist, step): step for step in pending_specialists}
            for future in as_completed(futures):
                step = futures[future]
                try:
                    step_run, tool_call, step_artifacts = future.result()
                except Exception as exc:  # pragma: no cover - 仅兜住线程边界的未预期异常。
                    step_run, tool_call, step_artifacts = _failed_safe_step(
                        runtime_task_id=runtime_task_id,
                        step=step,
                        started_at=datetime.now(UTC),
                        error_code="agent_delegate_failed",
                        message="组合子任务在线程中异常结束；其它独立子任务会继续完成。",
                        details={"exception_type": type(exc).__name__},
                    )
                step_states[step.id] = step_run
                tool_calls.append(tool_call)
                artifacts.extend(step_artifacts)
                if step_run.status == "completed":
                    _remember_step_result(runtime_context, step, step_run)
                    event_name, level = "step_completed", "info"
                elif step_run.status == "blocked":
                    event_name, level = "step_blocked", "warning"
                else:
                    event_name, level = "step_failed", "error"
                _append_runtime_event(
                    runtime_task_id,
                    events,
                    event_name,
                    step.agent,
                    step_run.message,
                    step.id,
                    level=level,
                    reporter=event_reporter,
                )
                save_progress()

        stopped_run = stop_for_control()
        if stopped_run is not None:
            return stopped_run

    completed_specialists = [
        step for step in specialists if step_states.get(step.id) is not None and step_states[step.id].status == "completed"
    ]
    unavailable_specialists = [step for step in specialists if step not in completed_specialists]
    if not completed_specialists:
        blocked_count = sum(
            1
            for step in unavailable_specialists
            if step_states.get(step.id) is not None and step_states[step.id].status == "blocked"
        )
        status = "blocked" if blocked_count else "failed"
        summary = "组合任务没有可用于汇总的已完成专业结果；请从失败或阻塞的子任务继续处理。"
        step_states[synthesis.id] = _pending_step(synthesis)
        save_progress()
        return finish(
            status,
            summary,
            event="task_waiting" if status == "blocked" else "task_failed",
            level="warning" if status == "blocked" else "error",
        )

    if synthesis.id not in step_states:
        step_states[synthesis.id] = _running_step(synthesis)
        save_progress()
        _append_runtime_event(
            runtime_task_id,
            events,
            "step_started",
            synthesis.agent,
            "正在基于已完成子任务的脱敏摘要汇总交付。",
            synthesis.id,
            reporter=event_reporter,
        )
        synthesis_run, synthesis_call = _execute_composition_synthesis(
            runtime_task_id=runtime_task_id,
            step=synthesis,
            completed_steps=[(step, step_states[step.id]) for step in completed_specialists],
            unavailable_steps=[(step, step_states.get(step.id)) for step in unavailable_specialists],
        )
        step_states[synthesis.id] = synthesis_run
        tool_calls.append(synthesis_call)
        if synthesis_run.status == "completed":
            _remember_step_result(runtime_context, synthesis, synthesis_run)
            _append_runtime_event(
                runtime_task_id,
                events,
                "step_completed",
                synthesis.agent,
                synthesis_run.message,
                synthesis.id,
                reporter=event_reporter,
            )
        else:
            status = "failed"
            summary = f"组合任务汇总失败：{synthesis_run.message}"
            save_progress()
            return finish("failed", summary, event="task_failed", level="error")

    status = "completed"
    summary = _composition_parent_summary(completed_specialists, unavailable_specialists)
    save_progress()
    return finish("completed", summary, event="task_completed")


def _execute_composition_synthesis(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    completed_steps: list[tuple[WorkflowStep, WorkflowStepRun]],
    unavailable_steps: list[tuple[WorkflowStep, WorkflowStepRun | None]],
) -> tuple[WorkflowStepRun, WorkflowToolCall]:
    """把子任务的受控摘要收束成父任务结果，不再读取客户原文或重新调用模型。"""

    started_at = datetime.now(UTC)
    completed_results = [
        _composition_child_summary(plan_step, step_run)
        for plan_step, step_run in completed_steps
    ]
    unavailable_results = [
        _composition_unavailable_summary(plan_step, step_run)
        for plan_step, step_run in unavailable_steps
    ]
    completion_state = "complete" if not unavailable_results else "partial"
    result = {
        "completion_state": completion_state,
        "result_scope": "仅基于已完成子任务的脱敏摘要；未完成分支没有进入本次结论。",
        "completed_children": completed_results,
        "unavailable_children": unavailable_results,
    }
    message = (
        f"已汇总 {len(completed_results)} 项已完成专业结果。"
        if completion_state == "complete"
        else f"已汇总 {len(completed_results)} 项已完成专业结果；另有 {len(unavailable_results)} 项未完成。"
    )
    finished_at = datetime.now(UTC)
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message=message,
        requires_confirmation=False,
        risk_level=step.risk_level,
        output={"runtime": True, "tool_name": _tool_name_for_step(step), "result": result},
    )
    tool_call = _completed_tool_call(
        runtime_task_id=runtime_task_id,
        step=step,
        attempt=1,
        timeout_ms=step.timeout_ms or _TOOL_TIMEOUT_MS,
        started_at=started_at,
        finished_at=finished_at,
        request={"action": step.action, "child_step_ids": step.input.get("child_step_ids", [])},
        result=result,
    )
    return step_run, tool_call


def _composition_child_summary(step: WorkflowStep, step_run: WorkflowStepRun) -> dict[str, object]:
    """压缩父任务可保存的子任务事实，不复制文档、数据行、证据片段或模型 Prompt。"""

    raw_result = step_run.output.get("result")
    result = raw_result if isinstance(raw_result, dict) else {}
    reply = result.get("reply")
    return {
        "step_id": step.id,
        "agent": step.agent,
        "action": step.action,
        "delegated_task_id": str(result.get("delegated_task_id", "")),
        "status": "completed",
        "summary": _compact_text(str(reply or step_run.message), limit=600),
        "source_count": result.get("source_count"),
        "chart_count": result.get("chart_count"),
        "table_count": result.get("table_count"),
    }


def _composition_unavailable_summary(
    step: WorkflowStep,
    step_run: WorkflowStepRun | None,
) -> dict[str, object]:
    result = step_run.output.get("result") if step_run is not None else {}
    result = result if isinstance(result, dict) else {}
    return {
        "step_id": step.id,
        "agent": step.agent,
        "action": step.action,
        "delegated_task_id": str(result.get("delegated_task_id", "")),
        "status": step_run.status if step_run is not None else "pending",
        "recovery_hint": step.recovery_hint or "可在关联子任务或原计划中查看失败原因后重试。",
    }


def _composition_parent_summary(
    completed_steps: list[WorkflowStep],
    unavailable_steps: list[WorkflowStep],
) -> str:
    if not unavailable_steps:
        return f"组合任务已完成：已汇总 {len(completed_steps)} 项受控专业结果。"
    return (
        f"组合任务部分完成：已汇总 {len(completed_steps)} 项已完成专业结果；"
        f"{len(unavailable_steps)} 项未完成，未完成分支未被写入最终结论。"
    )


def fail_prepared_workflow_runtime(
    *,
    runtime_task_id: str,
    error: Exception,
    event_reporter: RuntimeEventReporter | None = None,
) -> WorkflowRun | None:
    """将后台线程未预期抛出的异常收束为可见、可重试的失败状态。"""

    previous_run = load_workflow_run(runtime_task_id)
    plan = load_workflow_plan(runtime_task_id)
    if previous_run is None or plan is None:
        return None

    failure_message = f"Runtime 后台执行异常：{type(error).__name__}。"
    failed_steps = [
        step.model_copy(
            update={
                "status": "failed",
                "message": failure_message,
                "output": {**step.output, "runtime_failure": type(error).__name__},
            }
        )
        if step.status == "running"
        else step
        for step in previous_run.steps
    ]
    now = datetime.now(UTC)
    run = previous_run.model_copy(
        update={
            "status": "failed",
            "summary": failure_message,
            "steps": failed_steps,
            "metrics": _build_runtime_metrics(
                steps=failed_steps,
                tool_calls=list_workflow_tool_calls(runtime_task_id),
                permission_requests=_build_permission_requests(runtime_task_id, plan),
                started_at=_runtime_started_at(previous_run),
                finished_at=now,
            ),
        }
    )
    save_workflow_runtime_checkpoint(
        run=run,
        plan=plan,
        permission_requests=_build_permission_requests(runtime_task_id, plan),
        artifacts=list_workflow_artifacts(runtime_task_id),
        tool_calls=list_workflow_tool_calls(runtime_task_id),
    )
    _append_runtime_event(
        runtime_task_id,
        [],
        "task_failed",
        "workflow_engine",
        failure_message,
        level="error",
        reporter=event_reporter,
    )
    clear_dry_run_memory_cache()
    return run


def request_runtime_pause(task_id: str) -> WorkflowExecutionResponse | None:
    """请求在下一个安全边界暂停 Runtime；不强杀正在运行的 Tool。"""

    run = load_workflow_run(task_id)
    if run is None or run.mode != "runtime":
        return None
    if is_terminal_status(run.status) or run.status == "blocked":
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=False,
            status=run.status,
            message="当前任务没有可暂停的执行链路。",
            workflow_run=run,
        )

    set_runtime_execution_control(task_id=task_id, pause_requested=True)
    if run.status == "waiting_permission":
        # 等待权限时没有后台 Tool 正在执行，暂停可以立即成为事实状态；用户之后仍可先查看或
        # 调整权限，再显式继续，避免“点了暂停但页面永远显示等待确认”。
        plan = load_workflow_plan(task_id)
        if plan is None:
            return None
        now = datetime.now(UTC)
        paused_run = run.model_copy(
            update={
                "status": "paused",
                "summary": "任务已在权限确认点暂停，可继续或取消。",
                "metrics": _build_runtime_metrics(
                    steps=run.steps,
                    tool_calls=list_workflow_tool_calls(task_id),
                    permission_requests=_build_permission_requests(task_id, plan),
                    started_at=_runtime_started_at(run),
                    finished_at=now,
                ),
            }
        )
        save_workflow_runtime_checkpoint(
            run=paused_run,
            plan=plan,
            permission_requests=_build_permission_requests(task_id, plan),
            artifacts=list_workflow_artifacts(task_id),
            tool_calls=list_workflow_tool_calls(task_id),
        )
        _append_runtime_event(
            task_id,
            [],
            "task_paused",
            "workflow_engine",
            "任务已在权限确认点暂停，等待你主动继续。",
            level="warning",
        )
        clear_dry_run_memory_cache()
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=True,
            status="paused",
            message="任务已暂停。",
            workflow_run=paused_run,
        )
    _append_runtime_event(
        task_id,
        [],
        "task_pause_requested",
        "workflow_engine",
        "已收到暂停请求；正在运行的安全工具完成后会停止后续步骤。",
        level="warning",
    )
    clear_dry_run_memory_cache()
    return WorkflowExecutionResponse(
        source_task_id=task_id,
        runtime_task_id=task_id,
        accepted=True,
        status=run.status,
        message="暂停请求已记录，将在当前安全步骤完成后生效。",
        workflow_run=run,
    )


def request_runtime_cancel(task_id: str) -> WorkflowExecutionResponse | None:
    """请求取消 Runtime；空闲等待态立即落终态，运行态等待当前 Tool 安全返回。"""

    run = load_workflow_run(task_id)
    if run is None or run.mode != "runtime":
        return None
    if is_terminal_status(run.status):
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=False,
            status=run.status,
            message="任务已经结束，不能再次取消。",
            workflow_run=run,
        )

    set_runtime_execution_control(
        task_id=task_id,
        pause_requested=False,
        cancel_requested=True,
    )
    if run.status == "running":
        _append_runtime_event(
            task_id,
            [],
            "task_cancel_requested",
            "workflow_engine",
            "已收到取消请求；正在运行的安全工具完成后不会继续后续步骤。",
            level="warning",
        )
        clear_dry_run_memory_cache()
        return WorkflowExecutionResponse(
            source_task_id=task_id,
            runtime_task_id=task_id,
            accepted=True,
            status="running",
            message="取消请求已记录，将在当前安全步骤完成后生效。",
            workflow_run=run,
        )

    plan = load_workflow_plan(task_id)
    if plan is None:
        return None
    cancelled_steps = [
        step.model_copy(
            update={
                "status": "cancelled",
                "message": "任务已被用户取消，该步骤不会继续执行。",
                "output": {**step.output, "cancelled": True},
            }
        )
        if step.status in {"pending", "running", "waiting_permission"}
        else step
        for step in run.steps
    ]
    now = datetime.now(UTC)
    cancelled_run = run.model_copy(
        update={
            "status": "cancelled",
            "summary": "任务已取消，未继续执行后续步骤。",
            "steps": cancelled_steps,
            "metrics": _build_runtime_metrics(
                steps=cancelled_steps,
                tool_calls=list_workflow_tool_calls(task_id),
                permission_requests=_build_permission_requests(task_id, plan),
                started_at=_runtime_started_at(run),
                finished_at=now,
            ),
        }
    )
    save_workflow_runtime_checkpoint(
        run=cancelled_run,
        plan=plan,
        permission_requests=_build_permission_requests(task_id, plan),
        artifacts=list_workflow_artifacts(task_id),
        tool_calls=list_workflow_tool_calls(task_id),
    )
    _append_runtime_event(
        task_id,
        [],
        "task_cancelled",
        "workflow_engine",
        "任务在等待状态被取消，后续工具不会触发。",
        level="warning",
    )
    clear_dry_run_memory_cache()
    return WorkflowExecutionResponse(
        source_task_id=task_id,
        runtime_task_id=task_id,
        accepted=True,
        status="cancelled",
        message="任务已取消。",
        workflow_run=cancelled_run,
    )


def _run_sequential_plan(
    *,
    runtime_task_id: str,
    source_task_id: str,
    plan: WorkflowPlan,
    event_reporter: RuntimeEventReporter | None = None,
) -> WorkflowRun:
    # Runtime 因权限确认或用户补充信息再次进入这里时，必须保留先前已完成的步骤、产物和
    # 事件。重跑整份计划会重复调用有副作用的工具，也会让审计时间线看起来像被改写。
    previous_run = load_workflow_run(runtime_task_id)
    resuming = previous_run is not None and previous_run.mode == "runtime"
    previous_steps_by_id = {
        step_run.step_id: step_run
        for step_run in (previous_run.steps if resuming else [])
    }
    completed_step_ids = {
        step_id
        for step_id, step_run in previous_steps_by_id.items()
        if step_run.status == "completed"
    }
    started_at = _runtime_started_at(previous_run) if resuming else datetime.now(UTC)
    permission_requests = _build_permission_requests(runtime_task_id, plan)
    decisions = _permission_decisions_by_step(runtime_task_id)
    policy_decisions = {
        step.id: evaluate_permission_policy(
            permission_policy=plan.preference_applied.permission_policy,
            step=step,
        )
        for step in plan.steps
    }
    output_dir = settings.data_dir / "outputs" / runtime_task_id

    events = load_task_log_events(runtime_task_id) or []
    sequence = max((event.sequence for event in events), default=0) + 1
    was_interrupted = bool(
        resuming
        and (
            previous_run.status in {"paused", "waiting_permission"}
            or completed_step_ids
        )
    )
    _append_runtime_event(
        runtime_task_id,
        events,
        "task_resumed" if was_interrupted else "task_started",
        "workflow_engine",
        "已从上一个安全检查点继续执行；已完成步骤不会重复运行。"
        if was_interrupted
        else "Runtime 已开始执行安全内置工具。",
        reporter=event_reporter,
    )
    sequence += 1
    steps: list[WorkflowStepRun] = []
    # 只保留已完成步骤的输出。上次 waiting/blocked/pending 的临时记录会被当前恢复尝试
    # 替换，避免同一 call_id 或 artifact 在保存时形成重复记录。
    tool_calls = [
        call
        for call in (list_workflow_tool_calls(runtime_task_id) if resuming else [])
        if call.step_id in completed_step_ids
    ]
    artifacts = [
        artifact
        for artifact in (list_workflow_artifacts(runtime_task_id) if resuming else [])
        if artifact.step_id in completed_step_ids
    ]
    runtime_context: dict[str, dict[str, object]] = {}
    plan_steps_by_id = {step.id: step for step in plan.steps}
    for completed_step_id in completed_step_ids:
        completed_step = previous_steps_by_id[completed_step_id]
        plan_step = plan_steps_by_id.get(completed_step_id)
        if plan_step is not None:
            _remember_step_result(runtime_context, plan_step, completed_step)
    stopped = False
    status = "running"
    summary = "Runtime 正在执行。"

    # Runtime 只能在步骤边界处理控制信号。这样一次正在进行的模型调用或受控写入不会被线程
    # 粗暴打断；用户仍会马上看到“已请求暂停/取消”，下一安全点会将其落为正式状态。
    def apply_control_request() -> bool:
        nonlocal sequence, status, summary, stopped
        control = get_runtime_execution_control(runtime_task_id)
        if control is None:
            return False
        if control.cancel_requested:
            status = "cancelled"
            summary = "任务已按用户请求取消，未继续执行后续步骤。"
            stopped = True
            _append_runtime_event(
                runtime_task_id,
                events,
                "task_cancelled",
                "workflow_engine",
                "已在安全检查点停止任务，后续工具不会继续触发。",
                level="warning",
                reporter=event_reporter,
            )
            sequence += 1
            return True
        if control.pause_requested:
            status = "paused"
            summary = "任务已在安全检查点暂停，可继续或取消。"
            stopped = True
            _append_runtime_event(
                runtime_task_id,
                events,
                "task_paused",
                "workflow_engine",
                "已完成当前安全步骤并暂停，已完成步骤和审计记录将被保留。",
                level="warning",
                reporter=event_reporter,
            )
            sequence += 1
            return True
        return False

    for step in plan.steps:
        previous_step = previous_steps_by_id.get(step.id)
        if previous_step is not None and previous_step.status == "completed":
            # 断点续办不重新触发已完成步骤。其结构化 output 已在上方恢复为本次短期上下文。
            steps.append(previous_step)
            continue

        apply_control_request()
        if stopped:
            steps.append(_pending_step(step))
            tool_calls.append(_skipped_tool_call(runtime_task_id, step))
            continue

        if step.execution_mode == "guided_handoff":
            # D5.4 前数据工作台尚未被标记为可由 Commander 自动委派。此处只产生可审计的
            # 用户行动点，既不会读取数据，也不会伪造一个数据 Agent 子任务。
            step_run, tool_call = _guided_handoff_step(runtime_task_id, step)
            steps.append(step_run)
            tool_calls.append(tool_call)
            _append_runtime_event(
                runtime_task_id,
                events,
                "user_action_required",
                step.agent,
                step_run.message,
                step.id,
                level="warning",
                reporter=event_reporter,
            )
            sequence += 1
            status = "blocked"
            summary = f"Runtime 已暂停：{step.title} 需要你在专业工作台继续。"
            stopped = True
            continue

        policy_decision = policy_decisions[step.id]
        decision = decisions.get(step.id)
        if policy_decision.action == "block":
            step_run, tool_call = _blocked_step(
                runtime_task_id=runtime_task_id,
                step=step,
                reason=policy_decision.reason,
            )
            steps.append(step_run)
            tool_calls.append(tool_call)
            _append_runtime_event(
                runtime_task_id,
                events,
                "permission_denied",
                "governance",
                f"步骤 {step.title} 被平台权限策略阻止：{policy_decision.reason}",
                step.id,
                level="warning",
                reporter=event_reporter,
            )
            sequence += 1
            status = "blocked"
            summary = f"Runtime 已阻塞：{step.title} 未通过平台权限策略。"
            stopped = True
            continue

        if step.requires_confirmation or policy_decision.action == "confirm":
            if decision == "denied":
                step_run, tool_call = _blocked_step(
                    runtime_task_id=runtime_task_id,
                    step=step,
                    reason="用户拒绝了该步骤所需权限。",
                )
                steps.append(step_run)
                tool_calls.append(tool_call)
                _append_runtime_event(
                    runtime_task_id,
                    events,
                    "permission_denied",
                    "workflow_engine",
                    f"步骤 {step.title} 的权限被拒绝，Runtime 已阻塞。",
                    step.id,
                    level="warning",
                    reporter=event_reporter,
                )
                sequence += 1
                status = "blocked"
                summary = f"Runtime 已阻塞：{step.title} 的权限被拒绝。"
                stopped = True
                continue

            if policy_decision.action == "confirm" and decision != "approved":
                step_run, tool_call = _waiting_permission_step(runtime_task_id, step)
                steps.append(step_run)
                tool_calls.append(tool_call)
                _append_runtime_event(
                    runtime_task_id,
                    events,
                    "permission_required",
                    "workflow_engine",
                    _build_confirmation_message(step),
                    step.id,
                    level="warning",
                    reporter=event_reporter,
                )
                sequence += 1
                status = "waiting_permission"
                summary = f"Runtime 正在等待权限确认：{step.title}。"
                stopped = True
                continue

            if policy_decision.action == "allow" and decision != "approved":
                _append_runtime_event(
                    runtime_task_id,
                    events,
                    "permission_auto_approved",
                    "governance",
                    f"步骤 {step.title} 已按“{policy_decision.policy}”策略自动批准，并保留审计。",
                    step.id,
                    reporter=event_reporter,
                )
                sequence += 1

        # 先把“running”检查点写入 SQLite，再进入可能耗时的 Tool。客户端即使此刻重连，也能
        # 看见当前卡在哪个步骤，而不是等整个 HTTP/模型调用结束后才突然跳到终态。
        steps.append(_running_step(step))
        _save_runtime_progress_checkpoint(
            runtime_task_id=runtime_task_id,
            plan=plan,
            status=status,
            summary=summary,
            steps=steps,
            tool_calls=tool_calls,
            artifacts=artifacts,
            permission_requests=permission_requests,
            started_at=started_at,
        )
        _append_runtime_event(
            runtime_task_id,
            events,
            "step_started",
            step.agent,
            f"开始执行安全工具：{step.title}",
            step.id,
            reporter=event_reporter,
        )
        sequence += 1
        step_run, tool_call, step_artifacts = _execute_safe_step_with_retries(
            runtime_task_id=runtime_task_id,
            step=step,
            plan=plan,
            output_dir=output_dir,
            runtime_context=runtime_context,
        )
        steps[-1] = step_run
        tool_calls.append(tool_call)
        artifacts.extend(step_artifacts)
        if step_run.status == "failed":
            if tool_call.attempt > 1:
                _append_runtime_event(
                    runtime_task_id,
                    events,
                    "step_retried",
                    "workflow_engine",
                    f"工具失败后已自动重试 {tool_call.attempt - 1} 次。",
                    step.id,
                    level="warning",
                    reporter=event_reporter,
                )
                sequence += 1
            _append_runtime_event(
                runtime_task_id,
                events,
                "step_failed",
                step.agent,
                step_run.message,
                step.id,
                level="error",
                reporter=event_reporter,
            )
            sequence += 1
            status = "failed"
            summary = f"Runtime 执行失败：{step.title}。{step_run.message}"
            stopped = True
            continue

        if step_run.status == "blocked":
            _append_runtime_event(
                runtime_task_id,
                events,
                "step_blocked",
                step.agent,
                step_run.message,
                step.id,
                level="warning",
                reporter=event_reporter,
            )
            sequence += 1
            status = "blocked"
            summary = f"Runtime 已暂停：{step.title} 需要补充材料或调整任务。"
            stopped = True
            continue

        _remember_step_result(runtime_context, step, step_run)
        _append_runtime_event(
            runtime_task_id,
            events,
            "step_completed",
            step.agent,
            step_run.message,
            step.id,
            reporter=event_reporter,
        )
        sequence += 1
        # 如果用户恰好在 Tool 执行期间点击暂停/取消，先如实记录这个已经完成的步骤，再在
        # 同一个安全边界消费控制信号。这样最后一个步骤完成时也不会忽略用户的暂停请求。
        apply_control_request()

    if status == "running":
        status = "completed"
        summary = _completed_parent_summary(steps)
        _append_runtime_event(
            runtime_task_id,
            events,
            "task_completed",
            "workflow_engine",
            summary,
            reporter=event_reporter,
        )
    elif status == "failed":
        _append_runtime_event(
            runtime_task_id,
            events,
            "task_failed",
            "workflow_engine",
            summary,
            level="error",
            reporter=event_reporter,
        )
    elif status in {"waiting_permission", "blocked"}:
        _append_runtime_event(
            runtime_task_id,
            events,
            "task_waiting",
            "workflow_engine",
            summary,
            level="warning",
            reporter=event_reporter,
        )

    finished_at = datetime.now(UTC)
    run = WorkflowRun(
        task_id=runtime_task_id,
        mode="runtime",
        status=status,
        summary=summary,
        max_risk_level=plan.max_risk_level,
        requires_confirmation=plan.requires_confirmation,
        validation_errors=[],
        steps=steps,
        limits=_runtime_execution_limits(plan),
        metrics=_build_runtime_metrics(
            steps=steps,
            tool_calls=tool_calls,
            permission_requests=permission_requests,
            started_at=started_at,
            finished_at=finished_at,
        ),
    )
    save_workflow_runtime_checkpoint(
        run=run,
        plan=plan,
        permission_requests=permission_requests,
        artifacts=artifacts,
        tool_calls=tool_calls,
    )
    _record_policy_permission_decisions(
        task_id=runtime_task_id,
        permission_requests=permission_requests,
        existing_decisions=decisions,
        policy_decisions=policy_decisions,
    )
    # 现有查询层带有开发期内存缓存。真实 Runtime 保存后清空缓存，确保后续 GET 读到新状态。
    clear_dry_run_memory_cache()
    return run


def _execute_safe_step_with_retries(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    plan: WorkflowPlan,
    output_dir: Path,
    runtime_context: dict[str, dict[str, object]],
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """执行安全工具，并按 retryable 标记决定是否自动重试。

    确定性错误不会重试，避免参数错误或越权路径导致 Agent 空转；临时性错误最多执行
    `_MAX_TOOL_ATTEMPTS` 次，也就是初试一次、重试两次。
    """

    attempt = 1
    while True:
        step_run, tool_call, artifacts = _execute_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            plan=plan,
            output_dir=output_dir,
            runtime_context=runtime_context,
            attempt=attempt,
        )
        if step_run.status != "failed":
            return step_run, tool_call, artifacts

        error_payload = step_run.output.get("error", {})
        retryable = bool(error_payload.get("retryable"))
        if not retryable or attempt >= _MAX_TOOL_ATTEMPTS:
            return step_run, tool_call, artifacts

        attempt += 1


def _execute_safe_step(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    plan: WorkflowPlan,
    output_dir: Path,
    runtime_context: dict[str, dict[str, object]],
    attempt: int = 1,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    if step.agent == "document_agent" and step.action == "read_text":
        return _execute_document_read_text(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=datetime.now(UTC),
            attempt=attempt,
        )
    if step.agent == "document_agent" and step.action == "search_text":
        return _execute_document_search_text(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=datetime.now(UTC),
            attempt=attempt,
        )
    if step.agent == "document_agent" and step.action == "analyze_document":
        return _execute_document_agent_handoff(
            runtime_task_id=runtime_task_id,
            step=step,
            attempt=attempt,
        )
    if step.agent == "knowledge_agent" and step.action == "answer_question":
        return _execute_knowledge_agent_handoff(
            runtime_task_id=runtime_task_id,
            step=step,
            attempt=attempt,
        )
    if step.agent == "knowledge_agent" and step.action == "deep_summary":
        return _execute_knowledge_deep_summary_handoff(
            runtime_task_id=runtime_task_id,
            step=step,
            attempt=attempt,
        )
    if step.agent == "data_agent" and step.action == "analyze_dataset":
        return _execute_data_analysis_handoff(
            runtime_task_id=runtime_task_id,
            step=step,
            attempt=attempt,
        )
    if step.agent == "document_agent" and step.action in {"extract_requirements", "summarize_document"}:
        return _execute_document_memory_tool(
            runtime_task_id=runtime_task_id,
            step=step,
            plan=plan,
            runtime_context=runtime_context,
            started_at=datetime.now(UTC),
            attempt=attempt,
        )
    if step.agent == "code_agent" and step.action == "generate_code":
        return _execute_code_generate_code(
            runtime_task_id=runtime_task_id,
            step=step,
            plan=plan,
            output_dir=output_dir,
            runtime_context=runtime_context,
            attempt=attempt,
        )
    if step.agent == "report_agent" and step.action in {"generate_report", "generate_markdown_report"}:
        return _execute_report_compose_markdown(
            runtime_task_id=runtime_task_id,
            step=step,
            plan=plan,
            output_dir=output_dir,
            runtime_context=runtime_context,
            attempt=attempt,
        )

    return _execute_memory_summary_tool(
        runtime_task_id=runtime_task_id,
        step=step,
        plan=plan,
        attempt=attempt,
    )


def _execute_document_agent_handoff(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """把 Commander 的文档步骤委派给同一条正式 Document Agent 运行入口。

    父 Runtime 只保存脱敏结果和子任务 ID；模型调用、Tool trace、来源映射与停止条件
    继续由 `run_document_agent` 独立负责。这样直接页面和 Commander 不会各自维护一套
    文档分析循环，同时历史页仍能沿着 `delegated_task_id` 追溯完整子任务。
    """

    started_at = datetime.now(UTC)
    timeout_ms = step.timeout_ms or 120_000
    try:
        request = DocumentAgentRunRequest.model_validate(
            {
                "task_goal": step.input.get("task_goal", ""),
                "document_refs": step.input.get("document_refs", []),
                "query": step.input.get("query", ""),
                "output_mode": step.input.get("output_mode", "auto"),
            }
        )
    except Exception as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="invalid_parameters",
            message="文档助手委派参数不符合受控输入契约。",
            details={"reason": str(exc)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    async def _run_with_timeout():
        return await asyncio.wait_for(
            run_document_agent(request),
            timeout=timeout_ms / 1000,
        )

    try:
        response = asyncio.run(_run_with_timeout())
    except TimeoutError:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="tool_timeout",
            message="文档助手在允许时间内没有完成受控分析。",
            details={"timeout_ms": timeout_ms},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )
    except Exception as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="agent_delegate_failed",
            message="文档助手委派过程发生未预期错误。",
            details={"reason": str(exc)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    result = {
        "delegated_task_id": response.task_id,
        "agent_mode": response.mode,
        "agent_status": response.status,
        "stop_reason": response.stop_reason,
        "reply": response.reply,
        "document_context": response.document_context.model_dump(mode="json"),
    }
    finished_at = datetime.now(UTC)
    if response.status == "completed":
        step_run = WorkflowStepRun(
            step_id=step.id,
            agent=step.agent,
            action=step.action,
            status="completed",
            message="文档助手已完成受控分析；结果和来源可从关联子任务复盘。",
            requires_confirmation=step.requires_confirmation,
            risk_level=step.risk_level,
            output={"runtime": True, "tool_name": _tool_name_for_step(step), "result": result},
        )
        tool_call = _completed_tool_call(
            runtime_task_id=runtime_task_id,
            step=step,
            attempt=attempt,
            timeout_ms=timeout_ms,
            started_at=started_at,
            finished_at=finished_at,
            request={"action": step.action, "input": step.input},
            result=result,
        )
        return step_run, tool_call, [_delegated_agent_artifact(runtime_task_id, step, result)]

    blocked = response.status in {"needs_clarification", "insufficient_context"}
    status = "blocked" if blocked else "failed"
    message = response.reply or "文档助手没有返回可用结果。"
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status=status,
        message=message,
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={
            "runtime": True,
            "tool_name": _tool_name_for_step(step),
            "result": result,
            "error": {} if blocked else {"code": response.stop_reason, "message": message, "retryable": False},
        },
    )
    tool_call = WorkflowToolCall(
        call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        tool_name=_tool_name_for_step(step),
        status="blocked" if blocked else "failed",
        risk_level=step.risk_level,
        permission_required=step.requires_confirmation,
        attempt=attempt,
        max_attempts=1,
        timeout_ms=timeout_ms,
        duration_ms=_elapsed_ms(started_at, finished_at),
        failure_count=0 if blocked else 1,
        request={"action": step.action, "input": step.input},
        result=result,
        error="" if blocked else message,
        started_at=_format_dt(started_at),
        finished_at=_format_dt(finished_at),
    )
    return step_run, tool_call, [_delegated_agent_artifact(runtime_task_id, step, result)]


def _execute_data_analysis_handoff(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """把 D5.4 的单份数据预览委派到数据工作台的确定性 D2 链路。

    Commander 不直接读取 CSV/XLSX，也不会获得原始行。它只持有稳定数据引用并等待子任务
    返回脱敏结论、源哈希与聚合数量；写文件的 D3/D5.2/D5.3 操作始终留在数据工作台。
    """

    started_at = datetime.now(UTC)
    timeout_ms = step.timeout_ms or 120_000
    raw_refs = step.input.get("dataset_refs")
    dataset_refs = [str(item).strip() for item in raw_refs] if isinstance(raw_refs, list) else []
    dataset_name = str(step.input.get("dataset_name", "")).strip()
    if len(dataset_refs) != 1 or not dataset_name or dataset_refs[0] != dataset_name:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="invalid_parameters",
            message="数据工作台委派必须绑定一份与步骤一致的已导入数据文件。",
            details={"dataset_ref_count": len(dataset_refs)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    try:
        request = DataAnalysisPreviewRequest.model_validate(
            {
                "dataset_name": dataset_name,
                "goal": step.input.get("task_goal", ""),
                "cleaning_policy": step.input.get("cleaning_policy", "safe"),
                "max_chart_count": step.input.get("max_chart_count", 4),
            }
        )
    except Exception as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="invalid_parameters",
            message="数据工作台委派参数不符合只读分析输入契约。",
            details={"reason": str(exc)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    delegated_task_id = f"task_data_preview_{uuid4().hex[:12]}"
    create_data_analysis_preview_queued_run(task_id=delegated_task_id, request=request)

    async def _run_with_timeout():
        return await asyncio.wait_for(
            run_data_analysis_preview_task(task_id=delegated_task_id, request=request),
            timeout=timeout_ms / 1000,
        )

    try:
        response = asyncio.run(_run_with_timeout())
    except TimeoutError:
        return _data_delegate_failure(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            timeout_ms=timeout_ms,
            attempt=attempt,
            delegated_task_id=delegated_task_id,
            error_code="tool_timeout",
            message="数据工作台在允许时间内没有完成只读分析预览；未写入任何数据文件。",
        )
    except Exception:
        return _data_delegate_failure(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            timeout_ms=timeout_ms,
            attempt=attempt,
            delegated_task_id=delegated_task_id,
            error_code="agent_delegate_failed",
            message="数据工作台委派过程发生未预期错误；可从关联子任务查看当前状态。",
        )

    result = {
        "delegated_task_id": response.task_id,
        "agent_status": response.status,
        "stop_reason": "completed" if response.status == "completed" else "data_analysis_failed",
        "reply": response.message,
        "source_sha256": response.source_sha256,
        "insight_mode": response.insight_mode,
        "chart_count": response.chart_count,
        "table_count": response.table_count,
        "read_only": True,
    }
    if response.status != "completed":
        return _data_delegate_failure(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            timeout_ms=timeout_ms,
            attempt=attempt,
            delegated_task_id=response.task_id,
            error_code="data_analysis_failed",
            message=response.message,
            result=result,
        )

    finished_at = datetime.now(UTC)
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message="数据工作台已完成只读分析；结论可在关联子任务复盘，正式交付仍需客户确认。",
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={"runtime": True, "tool_name": _tool_name_for_step(step), "result": result},
    )
    tool_call = _completed_tool_call(
        runtime_task_id=runtime_task_id,
        step=step,
        attempt=attempt,
        timeout_ms=timeout_ms,
        started_at=started_at,
        finished_at=finished_at,
        request={"action": step.action, "dataset_name": dataset_name, "goal_length": len(request.goal.strip())},
        result=result,
    )
    return step_run, tool_call, [_delegated_agent_artifact(runtime_task_id, step, result)]


def _data_delegate_failure(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    started_at: datetime,
    timeout_ms: int,
    attempt: int,
    delegated_task_id: str,
    error_code: str,
    message: str,
    result: dict[str, object] | None = None,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """同时保留父步骤失败与关联子任务，避免客户失去可复盘入口。"""

    delegated_result = result or {
        "delegated_task_id": delegated_task_id,
        "agent_status": "failed",
        "stop_reason": error_code,
        "reply": message,
        "read_only": True,
    }
    step_run, tool_call, _ = _failed_safe_step(
        runtime_task_id=runtime_task_id,
        step=step,
        started_at=started_at,
        error_code=error_code,
        message=message,
        details={"delegated_task_id": delegated_task_id},
        timeout_ms=timeout_ms,
        attempt=attempt,
    )
    step_run = step_run.model_copy(
        update={
            "output": {
                **step_run.output,
                "result": delegated_result,
            }
        }
    )
    tool_call = tool_call.model_copy(update={"result": delegated_result})
    return step_run, tool_call, [_delegated_agent_artifact(runtime_task_id, step, delegated_result)]


def _execute_knowledge_agent_handoff(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """把 C4 的 Commander 步骤委派到既有 K3 问答任务。

    Knowledge Agent 的索引、检索、Evidence Gate 与来源闭合均已在独立服务中实现；父
    Runtime 只创建关联任务并保存脱敏摘要，不能为“方便汇总”重新读取父块或绕过 Gate。
    """

    started_at = datetime.now(UTC)
    timeout_ms = step.timeout_ms or 120_000
    try:
        request = KnowledgeAnswerRequest.model_validate(
            {
                "knowledge_base_id": step.input.get("knowledge_base_id", ""),
                "query": step.input.get("query", ""),
            }
        )
    except Exception as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="invalid_parameters",
            message="知识库委派参数不符合受控输入契约。",
            details={"reason": str(exc)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    delegated_task_id = f"task_kb_{uuid4().hex[:12]}"
    create_knowledge_answer_queued_run(task_id=delegated_task_id, request=request)

    async def _run_with_timeout():
        return await asyncio.wait_for(
            run_knowledge_answer_task(task_id=delegated_task_id, request=request),
            timeout=timeout_ms / 1000,
        )

    try:
        response = asyncio.run(_run_with_timeout())
    except TimeoutError:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="tool_timeout",
            message="知识库助手在允许时间内没有完成可信问答。",
            details={"timeout_ms": timeout_ms, "delegated_task_id": delegated_task_id},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )
    except Exception as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="agent_delegate_failed",
            message="知识库助手委派过程发生未预期错误。",
            details={"reason": str(exc), "delegated_task_id": delegated_task_id},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    answer = response.result.answer if response.result is not None else None
    result = {
        "delegated_task_id": response.task_id,
        "agent_status": response.status,
        "stop_reason": response.result.stop_reason if response.result is not None else "knowledge_answer_not_completed",
        "reply": response.message,
        "source_count": len(answer.source_ids) if answer is not None else 0,
        "retrieval_mode": response.result.retrieval_diagnostics.mode if response.result is not None else "unavailable",
    }
    finished_at = datetime.now(UTC)
    if response.status == "completed":
        step_run = WorkflowStepRun(
            step_id=step.id,
            agent=step.agent,
            action=step.action,
            status="completed",
            message="知识库助手已完成可信问答；完整结论与来源可从关联子任务查看。",
            requires_confirmation=step.requires_confirmation,
            risk_level=step.risk_level,
            output={"runtime": True, "tool_name": _tool_name_for_step(step), "result": result},
        )
        tool_call = _completed_tool_call(
            runtime_task_id=runtime_task_id,
            step=step,
            attempt=attempt,
            timeout_ms=timeout_ms,
            started_at=started_at,
            finished_at=finished_at,
            request={"action": step.action, "input": step.input},
            result=result,
        )
        return step_run, tool_call, [_delegated_agent_artifact(runtime_task_id, step, result)]

    blocked = response.status == "blocked"
    message = response.message or "知识库助手没有返回可用结论。"
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="blocked" if blocked else "failed",
        message=message,
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={
            "runtime": True,
            "tool_name": _tool_name_for_step(step),
            "result": result,
            "error": {} if blocked else {"code": result["stop_reason"], "message": message, "retryable": False},
        },
    )
    tool_call = WorkflowToolCall(
        call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        tool_name=_tool_name_for_step(step),
        status="blocked" if blocked else "failed",
        risk_level=step.risk_level,
        permission_required=step.requires_confirmation,
        attempt=attempt,
        max_attempts=1,
        timeout_ms=timeout_ms,
        duration_ms=_elapsed_ms(started_at, finished_at),
        failure_count=0 if blocked else 1,
        request={"action": step.action, "input": step.input},
        result=result,
        error="" if blocked else message,
        started_at=_format_dt(started_at),
        finished_at=_format_dt(finished_at),
    )
    return step_run, tool_call, [_delegated_agent_artifact(runtime_task_id, step, result)]


def _execute_knowledge_deep_summary_handoff(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """把 C5 的客户确认委派成独立 K4 后台子任务，而不是阻塞父 Runtime。

    父步骤成功只代表“范围已冻结且后台子任务已受理”，绝不把它写成深度分析已经完成。
    K4 继续独占 Map/Reduce checkpoint、模型失败恢复、暂停/继续/取消和报告资格判断。
    """

    started_at = datetime.now(UTC)
    timeout_ms = step.timeout_ms or 30_000
    try:
        request = KnowledgeDeepTaskRequest.model_validate(
            {
                "knowledge_base_id": step.input.get("knowledge_base_id", ""),
                "task_goal": step.input.get("task_goal", ""),
                "task_kind": "summary",
            }
        )
    except Exception as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="invalid_parameters",
            message="知识库深度委派参数不符合受控输入契约。",
            details={"reason": str(exc)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    try:
        receipt = start_knowledge_deep_task_in_background(request)
    except KnowledgeDeepTaskDispatchError as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="knowledge_deep_scope_failed",
            message=str(exc),
            details={},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )
    except Exception as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="agent_delegate_failed",
            message="知识库深度子任务受理时发生未预期错误。",
            details={"reason": str(exc)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    result = {
        "delegated_task_id": receipt.task_id,
        "agent_status": "queued",
        "handoff_state": "accepted",
        "scope_map_count": receipt.map_unit_count,
        "stop_reason": "background_deep_task_accepted",
        "reply": (
            f"已受理知识库全库深度总结子任务，当前活动版本的 {receipt.map_unit_count} 个章节已冻结。"
            "父任务的委派已完成；深度分析仍在关联子任务后台执行，请在任务历史查看阶段、暂停、继续或取消。"
        ),
    }
    finished_at = datetime.now(UTC)
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message="知识库深度子任务已受理；父任务不会等待整库分析完成。",
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={"runtime": True, "tool_name": _tool_name_for_step(step), "result": result},
    )
    tool_call = _completed_tool_call(
        runtime_task_id=runtime_task_id,
        step=step,
        attempt=attempt,
        timeout_ms=timeout_ms,
        started_at=started_at,
        finished_at=finished_at,
        request={"action": step.action, "input": step.input},
        result=result,
    )
    return step_run, tool_call, [_delegated_agent_artifact(runtime_task_id, step, result)]


def _execute_document_memory_tool(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    plan: WorkflowPlan,
    runtime_context: dict[str, dict[str, object]],
    started_at: datetime,
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    document_context = _document_context_from_runtime_context(runtime_context)
    if _has_prior_document_source(runtime_context) and not _has_document_context(document_context):
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="missing_document_context",
            message="前置文档工具没有产生可用上下文，无法提取文档要求。",
            details={
                "source_steps": document_context.get("source_steps", []),
                "search_match_total": document_context.get("search_match_total", 0),
                "read_preview_total": document_context.get("read_preview_total", 0),
            },
            attempt=attempt,
        )
    result = {
        "summary": step.expected_output,
        "workflow_summary": plan.summary,
        "input_excerpt": _compact_text(str(step.input), limit=800),
        "context": document_context,
        "runtime_note": "该文档工具当前生成结构化摘要，不读取工作区外文件。",
    }
    return _completed_memory_step(
        runtime_task_id=runtime_task_id,
        step=step,
        message=f"已完成文档工具：{step.title}。",
        result=result,
        attempt=attempt,
    )


def _remember_step_result(
    runtime_context: dict[str, dict[str, object]],
    step: WorkflowStep,
    step_run: WorkflowStepRun,
) -> None:
    """把已完成步骤的结构化输出放入本次 Runtime 的短期上下文。

    这是阶段 4B 的最小 memory：只在当前任务内生效，不做长期记忆，也不把原始大文本反复
    传给后续步骤。后续接 LangGraph/checkpoint 时可以把这里替换成显式 State。
    """

    if step_run.status != "completed":
        return
    result = step_run.output.get("result")
    if not isinstance(result, dict):
        return
    runtime_context[step.id] = {
        "agent": step.agent,
        "action": step.action,
        "tool_name": step_run.output.get("tool_name"),
        "result": result,
    }


def _document_context_from_runtime_context(
    runtime_context: dict[str, dict[str, object]],
) -> dict[str, object]:
    """从前置文档工具结果中提炼后续步骤可用的短上下文。"""

    source_steps: list[str] = []
    search_matches: list[dict[str, object]] = []
    read_previews: list[dict[str, object]] = []

    for step_id, item in runtime_context.items():
        tool_name = item.get("tool_name")
        result = item.get("result")
        if not isinstance(result, dict):
            continue

        if tool_name == "document.search_text":
            source_steps.append(step_id)
            for match in result.get("matches", [])[:5]:
                if not isinstance(match, dict):
                    continue
                search_matches.append(
                    {
                        "document_name": match.get("document_name", ""),
                        "line_number": match.get("line_number", 0),
                        "preview": _compact_text(str(match.get("preview", "")), limit=300),
                    }
                )
            auto_read = result.get("auto_read")
            if isinstance(auto_read, dict):
                read_previews.append(
                    {
                        "relative_path": auto_read.get("relative_path", ""),
                        "bytes": auto_read.get("bytes", 0),
                        "preview": _compact_text(str(auto_read.get("preview", "")), limit=600),
                        "source": "search_auto_read",
                    }
                )
        elif tool_name == "document.read_text":
            source_steps.append(step_id)
            read_previews.append(
                {
                    "relative_path": result.get("relative_path", ""),
                    "bytes": result.get("bytes", 0),
                    "preview": _compact_text(str(result.get("preview", "")), limit=600),
                }
            )
        elif tool_name == "agent.document_agent.analyze":
            source_steps.append(step_id)
            document_context = result.get("document_context")
            if not isinstance(document_context, dict):
                continue
            sources = document_context.get("sources")
            if not isinstance(sources, list):
                continue
            for source in sources[:5]:
                if not isinstance(source, dict):
                    continue
                read_previews.append(
                    {
                        "relative_path": source.get("relative_path", ""),
                        "bytes": 0,
                        "preview": _compact_text(str(source.get("excerpt", "")), limit=600),
                        "source": "document_agent",
                    }
                )

    return {
        "source_steps": source_steps,
        "search_match_total": len(search_matches),
        "read_preview_total": len(read_previews),
        "search_matches": search_matches,
        "read_previews": read_previews,
    }


def _has_prior_document_source(runtime_context: dict[str, dict[str, object]]) -> bool:
    """判断本次任务中是否已经跑过会产生文档上下文的工具。

    如果用户只是用一句话描述需求，Document 步骤可以从目标文本生成结构化摘要；但如果
    前面已经安排了 read/search，却没有任何命中或预览，再继续生成会让用户误以为文档被
    成功分析过，所以应当以 missing_document_context 明确失败。
    """

    return any(
        item.get("tool_name") in {"document.read_text", "document.search_text", "agent.document_agent.analyze"}
        for item in runtime_context.values()
    )


def _execute_memory_summary_tool(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    plan: WorkflowPlan,
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    result = {
        "summary": step.expected_output,
        "workflow_summary": plan.summary,
        "runtime_note": "该步骤当前由安全内置工具生成结构化摘要，不触发外部副作用。",
    }
    return _completed_memory_step(
        runtime_task_id=runtime_task_id,
        step=step,
        message=f"已完成安全内置步骤：{step.title}。",
        result=result,
        attempt=attempt,
    )


def _completed_memory_step(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    message: str,
    result: dict[str, object],
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    started_at = datetime.now(UTC)
    finished_at = datetime.now(UTC)
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message=message,
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={
            "runtime": True,
            "tool_name": _tool_name_for_step(step),
            "result": result,
        },
    )
    tool_call = _completed_tool_call(
        runtime_task_id=runtime_task_id,
        step=step,
        attempt=attempt,
        timeout_ms=_TOOL_TIMEOUT_MS,
        started_at=started_at,
        finished_at=finished_at,
        request={"action": step.action, "input": step.input},
        result=result,
    )
    return step_run, tool_call, [_memory_artifact(runtime_task_id, step)]


def _execute_code_generate_code(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    plan: WorkflowPlan,
    output_dir: Path,
    runtime_context: dict[str, dict[str, object]],
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    started_at = datetime.now(UTC)
    path = output_dir / "code_draft.py"
    # Code Agent 第一版不直接读文件，只消费 Document Agent 已经审计过的短上下文。
    document_context = _document_context_from_runtime_context(runtime_context)
    text = _code_draft(plan, document_context=document_context)
    write_result = _write_controlled_output(path, text)
    if write_result is None:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="io_error",
            message="code.generate_code 写入受控代码草稿失败。",
            details={"path": str(path)},
            attempt=attempt,
        )
    verification = _verify_text_artifact(
        path,
        required_snippets=["AgentFlow 代码草稿", "DOCUMENT_CONTEXT", plan.summary],
    )
    if not verification["ok"]:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="artifact_verification_failed",
            message="code.generate_code 产物回读验证失败。",
            details={"path": str(path), "verification": verification},
            attempt=attempt,
        )

    output = {
        "output_file": str(path),
        "relative_path": f"outputs/{runtime_task_id}/{path.name}",
        "bytes": write_result,
        "document_context": document_context,
        "verification": verification,
    }
    finished_at = datetime.now(UTC)
    return _completed_file_step(
        runtime_task_id=runtime_task_id,
        step=step,
        message="已在受控 outputs 目录生成代码草稿。",
        output=output,
        artifact=_file_artifact(runtime_task_id, step, path, "code", "代码草稿"),
        started_at=started_at,
        finished_at=finished_at,
        attempt=attempt,
    )


def _execute_report_compose_markdown(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    plan: WorkflowPlan,
    output_dir: Path,
    runtime_context: dict[str, dict[str, object]],
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    started_at = datetime.now(UTC)
    path = output_dir / "README.md"
    # Report Agent 汇总同一任务内的结构化上下文，避免报告只复述 workflow summary。
    document_context = _document_context_from_runtime_context(runtime_context)
    text = _report_draft(plan, document_context=document_context)
    write_result = _write_controlled_output(path, text)
    if write_result is None:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="io_error",
            message="report.compose_markdown 写入受控 Markdown 报告失败。",
            details={"path": str(path)},
            attempt=attempt,
        )
    required_snippets = ["AgentFlow Runtime 报告草稿", "## 步骤", plan.summary]
    if _has_document_context(document_context):
        required_snippets.append("## 文档上下文")
    verification = _verify_text_artifact(path, required_snippets=required_snippets)
    if not verification["ok"]:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="artifact_verification_failed",
            message="report.compose_markdown 产物回读验证失败。",
            details={"path": str(path), "verification": verification},
            attempt=attempt,
        )

    output = {
        "output_file": str(path),
        "relative_path": f"outputs/{runtime_task_id}/{path.name}",
        "bytes": write_result,
        "document_context": document_context,
        "verification": verification,
    }
    finished_at = datetime.now(UTC)
    return _completed_file_step(
        runtime_task_id=runtime_task_id,
        step=step,
        message="已在受控 outputs 目录生成 Markdown 报告草稿。",
        output=output,
        artifact=_file_artifact(runtime_task_id, step, path, "report", "报告草稿"),
        started_at=started_at,
        finished_at=finished_at,
        attempt=attempt,
    )


def _completed_file_step(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    message: str,
    output: dict[str, object],
    artifact: WorkflowArtifact,
    started_at: datetime,
    finished_at: datetime,
    attempt: int,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message=message,
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={
            "runtime": True,
            "tool_name": _tool_name_for_step(step),
            "result": output,
        },
    )
    tool_call = _completed_tool_call(
        runtime_task_id=runtime_task_id,
        step=step,
        attempt=attempt,
        timeout_ms=_TOOL_TIMEOUT_MS,
        started_at=started_at,
        finished_at=finished_at,
        request={
            "action": step.action,
            "input": step.input,
            "required_permissions": step.required_permissions,
        },
        result=output,
    )
    return step_run, tool_call, [artifact]


def _completed_tool_call(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    attempt: int,
    timeout_ms: int,
    started_at: datetime,
    finished_at: datetime,
    request: dict[str, object],
    result: dict[str, object],
) -> WorkflowToolCall:
    return WorkflowToolCall(
        call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        tool_name=_tool_name_for_step(step),
        status="completed",
        risk_level=step.risk_level,
        permission_required=step.requires_confirmation,
        attempt=attempt,
        max_attempts=_MAX_TOOL_ATTEMPTS,
        timeout_ms=timeout_ms,
        duration_ms=_elapsed_ms(started_at, finished_at),
        failure_count=max(0, attempt - 1),
        request=request,
        result=result,
        started_at=_format_dt(started_at),
        finished_at=_format_dt(finished_at),
    )


def _execute_document_read_text(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    started_at: datetime,
    attempt: int = 1,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """读取受控工作区内的文本文件。

    这里刻意只允许 data/workspaces 或 data/workspace 下的 txt/markdown，避免 Runtime
    第一版把任意本机文件读取能力裸露给模型。
    """

    timeout_ms = _tool_timeout_ms(step)
    if _tool_timed_out(started_at, timeout_ms):
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="tool_timeout",
            message="document.read_text 执行超过工具超时限制。",
            details={"timeout_ms": timeout_ms},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    path_value = step.input.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="invalid_parameters",
            message="document.read_text 需要 input.path 指向受控工作区内的文本文件。",
            details={"required": "input.path"},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    candidate = Path(path_value.strip())
    if not candidate.is_absolute():
        candidate = settings.data_dir / "workspaces" / candidate
    resolved = candidate.resolve()
    workspace_roots = [
        (settings.data_dir / "workspaces").resolve(),
        (settings.data_dir / "workspace").resolve(),
    ]
    if not any(resolved.is_relative_to(root) for root in workspace_roots):
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="path_outside_workspace",
            message="document.read_text 只能读取受控工作区内的文件。",
            details={"path": str(resolved)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )
    if resolved.suffix.lower() not in _TEXT_FILE_SUFFIXES:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="unsupported_file_type",
            message="document.read_text 当前只支持 txt、md 和 markdown 文件。",
            details={"suffix": resolved.suffix.lower()},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )
    if not resolved.exists() or not resolved.is_file():
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="file_not_found",
            message="document.read_text 未找到指定文件。",
            details={"path": str(resolved)},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )
    if resolved.stat().st_size > _MAX_TEXT_FILE_BYTES:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="file_too_large",
            message="document.read_text 当前只读取 1MB 以内的文本文件。",
            details={"path": str(resolved), "max_bytes": _MAX_TEXT_FILE_BYTES},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    text = resolved.read_text(encoding="utf-8")
    output = {
        "path": str(resolved),
        "relative_path": _workspace_relative_path(resolved),
        "bytes": len(text.encode("utf-8")),
        "preview": text[:1200],
    }
    finished_at = datetime.now(UTC)
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message="已读取受控工作区内的文本文件。",
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={"runtime": True, "tool_name": _tool_name_for_step(step), "result": output},
    )
    tool_call = WorkflowToolCall(
        call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        tool_name=_tool_name_for_step(step),
        status="completed",
        risk_level=step.risk_level,
        permission_required=step.requires_confirmation,
        attempt=attempt,
        max_attempts=_MAX_TOOL_ATTEMPTS,
        timeout_ms=timeout_ms,
        duration_ms=_elapsed_ms(started_at, finished_at),
        failure_count=max(0, attempt - 1),
        request={"action": step.action, "input": step.input},
        result=output,
        started_at=_format_dt(started_at),
        finished_at=_format_dt(finished_at),
    )
    return step_run, tool_call, [_memory_artifact(runtime_task_id, step)]


def _execute_document_search_text(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    started_at: datetime,
    attempt: int = 1,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """执行受控 workspace 文档精确搜索。

    Runtime 只调用 workspace 服务暴露的安全搜索入口，不直接接收任意路径，也不递归扫描
    用户项目目录。这样后续把实现替换成 ripgrep 或索引时，权限边界仍保持在服务层。
    """

    timeout_ms = _tool_timeout_ms(step)
    if _tool_timed_out(started_at, timeout_ms):
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="tool_timeout",
            message="document.search_text 执行超过工具超时限制。",
            details={"timeout_ms": timeout_ms},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    query_value = step.input.get("query")
    if not isinstance(query_value, str) or not query_value.strip():
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="empty_query",
            message="document.search_text 需要 input.query 作为搜索词。",
            details={"required": "input.query"},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    try:
        selected_refs = step.input.get("document_refs")
        allowed_relative_paths = (
            [item.strip() for item in selected_refs if isinstance(item, str) and item.strip()]
            if isinstance(selected_refs, list)
            else None
        )
        response = search_workspace_documents(
            query=query_value,
            limit=_bounded_int(step.input.get("limit"), default=20, minimum=1, maximum=50),
            case_sensitive=bool(step.input.get("case_sensitive", False)),
            context_chars=_bounded_int(
                step.input.get("context_chars"),
                default=80,
                minimum=0,
                maximum=240,
            ),
            allowed_relative_paths=allowed_relative_paths,
        )
    except WorkspaceDocumentError as exc:
        return _failed_safe_step(
            runtime_task_id=runtime_task_id,
            step=step,
            started_at=started_at,
            error_code="invalid_parameters",
            message=str(exc),
            details={"query": query_value},
            timeout_ms=timeout_ms,
            attempt=attempt,
        )

    output = response.model_dump()
    if step.input.get("auto_read_if_unique") is True and response.suggested_read_path:
        try:
            output["auto_read"] = read_workspace_document_preview(
                relative_path=response.suggested_read_path,
                preview_chars=_bounded_int(
                    step.input.get("auto_read_preview_chars"),
                    default=2_400,
                    minimum=400,
                    maximum=8_000,
                ),
            )
        except WorkspaceDocumentError as exc:
            # 自动读取只是搜索后的增强上下文；搜索本身已经成功，不因为预览失败整步失败。
            output["auto_read_error"] = str(exc)
    finished_at = datetime.now(UTC)
    auto_read_note = "，并已读取唯一命中文档预览" if "auto_read" in output else ""
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message=f"已搜索受控 workspace 文档，命中 {response.total} 处{auto_read_note}。",
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={"runtime": True, "tool_name": _tool_name_for_step(step), "result": output},
    )
    tool_call = _completed_tool_call(
        runtime_task_id=runtime_task_id,
        step=step,
        attempt=attempt,
        timeout_ms=timeout_ms,
        started_at=started_at,
        finished_at=finished_at,
        request={"action": step.action, "input": step.input},
        result=output,
    )
    return step_run, tool_call, [_memory_artifact(runtime_task_id, step)]


def _workspace_relative_path(path: Path) -> str:
    """把受控 workspace 绝对路径压成展示用相对路径。

    工具审计可以保存绝对路径用于排障，但传给后续 Code/Report 的上下文应优先使用
    workspace 相对文件名，避免产物和 UI 无意义暴露本地 data 目录。
    """

    resolved = path.resolve()
    for root in (
        (settings.data_dir / "workspaces").resolve(),
        (settings.data_dir / "workspace").resolve(),
    ):
        if resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
    return path.name


def _failed_safe_step(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    started_at: datetime,
    error_code: str,
    message: str,
    details: dict[str, object] | None = None,
    timeout_ms: int = _TOOL_TIMEOUT_MS,
    attempt: int = 1,
) -> tuple[WorkflowStepRun, WorkflowToolCall, list[WorkflowArtifact]]:
    """生成统一的安全工具失败记录。

    当前先记录单次确定性失败；后续引入真实重试循环时，仍复用 error/result 这套结构。
    """

    finished_at = datetime.now(UTC)
    error_payload = {
        "code": error_code,
        "message": message,
        "details": details or {},
        "retryable": _is_retryable_error_code(error_code),
        "max_attempts": _MAX_TOOL_ATTEMPTS,
        "attempt": attempt,
    }
    step_run = WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="failed",
        message=message,
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={
            "runtime": True,
            "tool_name": _tool_name_for_step(step),
            "error": error_payload,
        },
    )
    tool_call = WorkflowToolCall(
        call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        tool_name=_tool_name_for_step(step),
        status="failed",
        risk_level=step.risk_level,
        permission_required=step.requires_confirmation,
        attempt=attempt,
        max_attempts=_MAX_TOOL_ATTEMPTS,
        timeout_ms=timeout_ms,
        duration_ms=_elapsed_ms(started_at, finished_at),
        failure_count=attempt,
        request={"action": step.action, "input": step.input},
        result={"error": error_payload},
        error=message,
        started_at=_format_dt(started_at),
        finished_at=_format_dt(finished_at),
    )
    return step_run, tool_call, []


def _is_retryable_error_code(error_code: str) -> bool:
    """判断工具错误是否值得自动重试。

    参数错误、越权路径、文件不存在这类确定性失败不重试，避免 Agent 在同一个错误上空转。
    后续接入网络或临时 IO 错误时，可以把对应错误码标成 retryable。
    """

    return error_code not in _NON_RETRYABLE_ERROR_CODES


def _tool_timeout_ms(step: WorkflowStep) -> int:
    """读取单步工具超时。

    当前只有内部测试和后续 Runtime 策略会传入 timeout_ms；用户输入不能借此获得额外权限。
    """

    raw_timeout = step.input.get("timeout_ms")
    if isinstance(raw_timeout, int) and raw_timeout >= 0:
        return raw_timeout
    return _TOOL_TIMEOUT_MS


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """把工具输入里的数字参数收敛到安全范围。"""

    if isinstance(value, int):
        return max(minimum, min(value, maximum))
    return default


def _tool_timed_out(started_at: datetime, timeout_ms: int) -> bool:
    """判断工具是否已经超过超时预算。

    `timeout_ms=0` 表示立即超时，方便离线验证覆盖超时路径而不实际等待。
    """

    return timeout_ms <= 0 or _elapsed_ms(started_at, datetime.now(UTC)) >= timeout_ms


def _waiting_permission_step(
    runtime_task_id: str,
    step: WorkflowStep,
) -> tuple[WorkflowStepRun, WorkflowToolCall]:
    message = f"等待用户确认权限：{_describe_permissions(step.required_permissions)}。"
    return (
        WorkflowStepRun(
            step_id=step.id,
            agent=step.agent,
            action=step.action,
            status="waiting_permission",
            message=message,
            requires_confirmation=True,
            risk_level=step.risk_level,
            output={
                "runtime": True,
                "permission_summary": _describe_permissions(step.required_permissions),
            },
        ),
        WorkflowToolCall(
            call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
            task_id=runtime_task_id,
            step_id=step.id,
            agent_id=step.agent,
            tool_name=_tool_name_for_step(step),
            status="pending_permission",
            risk_level=step.risk_level,
            permission_required=True,
            request={"required_permissions": step.required_permissions},
            result={},
            started_at=_now_iso(),
        ),
    )


def _guided_handoff_step(
    runtime_task_id: str,
    step: WorkflowStep,
) -> tuple[WorkflowStepRun, WorkflowToolCall]:
    """记录非执行型专业工作台交接，保留后续恢复语义。"""

    message = step.recovery_hint or f"请在 {step.title} 对应的专业工作台继续。"
    handoff = {
        "runtime": True,
        "guided_handoff": True,
        "next_action": "open_data_workspace",
        "task_goal": step.input.get("task_goal", ""),
        "dataset_refs": step.input.get("dataset_refs", []),
        "recovery_hint": step.recovery_hint,
    }
    return (
        WorkflowStepRun(
            step_id=step.id,
            agent=step.agent,
            action=step.action,
            status="blocked",
            message=message,
            requires_confirmation=False,
            risk_level=step.risk_level,
            output=handoff,
        ),
        WorkflowToolCall(
            call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
            task_id=runtime_task_id,
            step_id=step.id,
            agent_id=step.agent,
            tool_name=_tool_name_for_step(step),
            status="blocked",
            risk_level=step.risk_level,
            permission_required=False,
            error=message,
            request={"action": step.action, "input": step.input},
            result=handoff,
            started_at=_now_iso(),
            finished_at=_now_iso(),
        ),
    )


def _blocked_step(
    *,
    runtime_task_id: str,
    step: WorkflowStep,
    reason: str,
) -> tuple[WorkflowStepRun, WorkflowToolCall]:
    return (
        WorkflowStepRun(
            step_id=step.id,
            agent=step.agent,
            action=step.action,
            status="blocked",
            message=reason,
            requires_confirmation=step.requires_confirmation,
            risk_level=step.risk_level,
            output={"runtime": True, "blocked_reason": reason},
        ),
        WorkflowToolCall(
            call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
            task_id=runtime_task_id,
            step_id=step.id,
            agent_id=step.agent,
            tool_name=_tool_name_for_step(step),
            status="blocked",
            risk_level=step.risk_level,
            permission_required=step.requires_confirmation,
            error=reason,
            request={"required_permissions": step.required_permissions},
            started_at=_now_iso(),
            finished_at=_now_iso(),
        ),
    )


def _pending_step(step: WorkflowStep) -> WorkflowStepRun:
    return WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="pending",
        message="前置步骤尚未完成，当前步骤等待 Runtime 继续调度。",
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={"runtime": True},
    )


def _running_step(step: WorkflowStep) -> WorkflowStepRun:
    """构造已经持久化但尚未完成的步骤快照。"""

    return WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="running",
        message="安全工具正在执行；暂停或取消会在当前工具返回后的安全点生效。",
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={"runtime": True, "in_progress": True},
    )


def _new_runtime_run(*, runtime_task_id: str, plan: WorkflowPlan) -> WorkflowRun:
    """在后台任务真正开始前写入可查询的 Runtime 骨架。"""

    now = datetime.now(UTC)
    steps = [_pending_step(step) for step in plan.steps]
    return WorkflowRun(
        task_id=runtime_task_id,
        mode="runtime",
        status="pending",
        summary="Runtime 已进入执行队列。",
        max_risk_level=plan.max_risk_level,
        requires_confirmation=plan.requires_confirmation,
        validation_errors=[],
        steps=steps,
        metrics=_build_runtime_metrics(
            steps=steps,
            tool_calls=[],
            permission_requests=_build_permission_requests(runtime_task_id, plan),
            started_at=now,
            finished_at=now,
        ),
    )


def _save_runtime_progress_checkpoint(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
    status: str,
    summary: str,
    steps: list[WorkflowStepRun],
    tool_calls: list[WorkflowToolCall],
    artifacts: list[WorkflowArtifact],
    permission_requests: list[RuntimePermissionRequest],
    started_at: datetime,
) -> None:
    """把当前安全步骤及其余待执行步骤一起保存为可恢复快照。"""

    known_steps = {step.step_id: step for step in steps}
    snapshot_steps = [
        known_steps.get(plan_step.id, _pending_step(plan_step))
        for plan_step in plan.steps
    ]
    now = datetime.now(UTC)
    run = WorkflowRun(
        task_id=runtime_task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        max_risk_level=plan.max_risk_level,
        requires_confirmation=plan.requires_confirmation,
        validation_errors=[],
        steps=snapshot_steps,
        limits=_runtime_execution_limits(plan),
        metrics=_build_runtime_metrics(
            steps=snapshot_steps,
            tool_calls=tool_calls,
            permission_requests=permission_requests,
            started_at=started_at,
            finished_at=now,
        ),
    )
    save_workflow_runtime_checkpoint(
        run=run,
        plan=plan,
        permission_requests=permission_requests,
        artifacts=artifacts,
        tool_calls=tool_calls,
    )


def _append_runtime_event(
    task_id: str,
    events: list[TaskLogEvent],
    event_name: str,
    agent_id: str,
    message: str,
    step_id: str | None = None,
    *,
    level: TaskLogLevel = "info",
    reporter: RuntimeEventReporter | None = None,
) -> TaskLogEvent:
    """持久化 append-only 事件，并可选地转发给当前 WebSocket 实时流。"""

    event = append_workflow_event(
        task_id=task_id,
        event_name=event_name,
        agent_id=agent_id,
        message=message,
        step_id=step_id,
        level=level,
    )
    events.append(event)
    if reporter is not None:
        reporter(event)
    return event


def _runtime_started_at(previous_run: WorkflowRun | None) -> datetime:
    """恢复任务沿用首次启动时间，避免每次 pause/resume 把总耗时归零。"""

    if previous_run is None or not previous_run.metrics.started_at:
        return datetime.now(UTC)
    try:
        value = datetime.fromisoformat(previous_run.metrics.started_at.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _skipped_tool_call(runtime_task_id: str, step: WorkflowStep) -> WorkflowToolCall:
    return WorkflowToolCall(
        call_id=f"{runtime_task_id}:{step.id}:runtime-tool",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        tool_name=_tool_name_for_step(step),
        status="skipped",
        risk_level=step.risk_level,
        permission_required=step.requires_confirmation,
        result={"reason": "前置步骤尚未完成。"},
    )


def _build_permission_requests(
    task_id: str,
    plan: WorkflowPlan,
) -> list[RuntimePermissionRequest]:
    requests: list[RuntimePermissionRequest] = []
    for step in plan.steps:
        if not step.requires_confirmation:
            continue
        policy_decision = evaluate_permission_policy(
            permission_policy=plan.preference_applied.permission_policy,
            step=step,
        )
        requests.append(
            RuntimePermissionRequest(
                request_id=f"{task_id}:{step.id}:permission",
                task_id=task_id,
                step_id=step.id,
                agent_id=step.agent,
                permissions=step.required_permissions,
                risk_level=step.risk_level,
                summary=_build_confirmation_message(step),
                details={
                    "runtime": True,
                    "action": step.action,
                    "title": step.title,
                    "expected_output": step.expected_output,
                    "permission_summary": _describe_permissions(step.required_permissions),
                    "permission_policy": policy_decision.policy,
                    "policy_action": policy_decision.action,
                    "policy_reason": policy_decision.reason,
                },
            )
        )
    return requests


def _record_policy_permission_decisions(
    *,
    task_id: str,
    permission_requests: list[RuntimePermissionRequest],
    existing_decisions: dict[str, str],
    policy_decisions: dict[str, PermissionPolicyDecision],
) -> None:
    """在请求落库后记录自动裁决；已经存在的用户批准/拒绝永远优先保留。"""

    for request in permission_requests:
        if existing_decisions.get(request.step_id) in {"approved", "denied"}:
            continue
        policy_decision = policy_decisions[request.step_id]
        if policy_decision.action not in {"allow", "block"}:
            continue
        record_runtime_permission_decision(
            task_id=task_id,
            request_id=request.request_id,
            decision_input=RuntimePermissionDecisionInput(
                decision="approved" if policy_decision.action == "allow" else "denied",
                decided_by=f"platform_policy:{policy_decision.policy}",
                note=policy_decision.reason,
            ),
        )


def _permission_decisions_by_step(task_id: str) -> dict[str, str]:
    items: list[RuntimePermissionItem] = list_runtime_permission_requests(task_id=task_id)
    return {
        item.request.step_id: item.decision.decision
        for item in items
    }


def _runtime_execution_limits(plan: WorkflowPlan) -> RuntimeExecutionLimits:
    """返回任务实际采用的执行预算，而不是只在计划页展示估算值。"""

    if _supports_native_composition_runtime(plan):
        # 三个子任务在两个槽位中最多形成两轮；每个分支仍有自己的 timeout/retry 上限。
        # Parent 预算只约束调度器自身的调用数，不能擅自覆盖子 Agent 的专用预算。
        return RuntimeExecutionLimits(
            max_steps=1 + _COMPOSITION_MAX_SPECIALIST_STEPS + 1,
            max_tool_calls=_COMPOSITION_MAX_PARENT_TOOL_CALLS,
            max_retries_per_tool=_MAX_TOOL_ATTEMPTS - 1,
            tool_timeout_ms=120_000,
            task_timeout_ms=240_000,
            token_budget=12_288,
        )
    return RuntimeExecutionLimits()


def _build_runtime_metrics(
    *,
    steps: list[WorkflowStepRun],
    tool_calls: list[WorkflowToolCall],
    permission_requests: list[RuntimePermissionRequest],
    started_at: datetime,
    finished_at: datetime,
) -> RuntimeExecutionMetrics:
    return RuntimeExecutionMetrics(
        started_at=_format_dt(started_at),
        finished_at=_format_dt(finished_at),
        duration_ms=_elapsed_ms(started_at, finished_at),
        step_total=len(steps),
        step_completed=sum(1 for step in steps if step.status == "completed"),
        step_failed=sum(1 for step in steps if step.status == "failed"),
        tool_call_total=len(tool_calls),
        tool_call_simulated=0,
        tool_call_failed=sum(1 for call in tool_calls if call.status == "failed"),
        retry_total=sum(max(0, call.attempt - 1) for call in tool_calls),
        permission_request_total=len(permission_requests),
        estimated_input_tokens=sum(_rough_token_estimate(str(call.request)) for call in tool_calls),
        estimated_output_tokens=sum(_rough_token_estimate(str(call.result)) for call in tool_calls),
    )


def _file_artifact(
    runtime_task_id: str,
    step: WorkflowStep,
    path: Path,
    kind: str,
    name: str,
) -> WorkflowArtifact:
    return WorkflowArtifact(
        artifact_id=f"{runtime_task_id}:{step.id}:artifact",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        kind=kind,
        name=name,
        summary=step.expected_output,
        uri=f"agentflow-output://{runtime_task_id}/{path.name}",
        mime_type="text/markdown" if path.suffix.lower() == ".md" else "text/plain",
        metadata={
            "runtime": True,
            "relative_path": f"outputs/{runtime_task_id}/{path.name}",
            "output_path": str(path),
        },
        created_at=_now_iso(),
    )


def _memory_artifact(runtime_task_id: str, step: WorkflowStep) -> WorkflowArtifact:
    return WorkflowArtifact(
        artifact_id=f"{runtime_task_id}:{step.id}:artifact",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        kind="text",
        name=f"{step.title} 摘要",
        summary=step.expected_output,
        uri=f"memory://runtime/{runtime_task_id}/{step.id}",
        mime_type="text/plain",
        metadata={"runtime": True, "action": step.action},
        created_at=_now_iso(),
    )


def _delegated_agent_artifact(
    runtime_task_id: str,
    step: WorkflowStep,
    result: dict[str, object],
) -> WorkflowArtifact:
    """记录父任务到专业 Agent 子任务的可审计关联。

    这里不复制原文、模型上下文或来源片段，避免父子任务的审计数据重复膨胀；历史页只需
    通过 `delegated_task_id` 跳转/查询子任务，就能看到完整 Tool trace 和结构化结论。
    """

    delegated_task_id = str(result.get("delegated_task_id", ""))
    return WorkflowArtifact(
        artifact_id=f"{runtime_task_id}:{step.id}:delegation",
        task_id=runtime_task_id,
        step_id=step.id,
        agent_id=step.agent,
        kind="data",
        name=(
            "知识库深度总结任务" if step.action == "deep_summary"
            else "知识库问答结果" if step.agent == "knowledge_agent"
            else "数据工作台分析结果" if step.agent == "data_agent"
            else "文档助手运行结果"
        ),
        summary=_compact_text(str(result.get("reply", "")), limit=240),
        uri=f"agentflow-task://{delegated_task_id}" if delegated_task_id else "agentflow-task://unknown",
        mime_type="application/json",
        metadata={
            "runtime": True,
            "action": step.action,
            "delegated_task_id": delegated_task_id,
            "agent_status": result.get("agent_status", ""),
            "stop_reason": result.get("stop_reason", ""),
        },
        created_at=_now_iso(),
    )


def _completed_parent_summary(steps: list[WorkflowStepRun]) -> str:
    """把已完成的专业委派收束为 Commander 的客户可读终态。

    子任务仍拥有模型正文、来源和 Tool trace；父任务只引用已经落在 step output 的脱敏
    身份与状态，避免为了写一句汇总而把材料内容再复制一份进 SQLite 或任务时间线。
    """

    delegations: list[dict[str, object]] = []
    for step in steps:
        result = step.output.get("result") if isinstance(step.output, dict) else None
        if not isinstance(result, dict) or not result.get("delegated_task_id"):
            continue
        delegations.append(result)

    if not delegations:
        return f"Runtime 执行完成；共完成 {len(steps)} 个步骤。"

    background = [item for item in delegations if item.get("handoff_state") == "accepted"]
    if background:
        return (
            f"总指挥已完成 {len(background)} 个后台任务委派；深度分析尚未在父任务内完成，"
            "请从关联子任务查看实时阶段、检查点和最终报告。"
        )

    completed = sum(1 for item in delegations if item.get("agent_status") == "completed")
    if completed == len(delegations):
        return (
            f"总指挥已完成本次任务：{completed} 个专业 Agent 委派已返回结果；"
            "可在关联子任务查看完整来源和执行轨迹。"
        )
    return (
        f"总指挥已结束本次任务：{completed}/{len(delegations)} 个专业 Agent 委派完成；"
        "请在关联子任务查看未完成原因和下一步。"
    )


def _has_document_context(document_context: dict[str, object]) -> bool:
    return bool(
        document_context.get("search_match_total")
        or document_context.get("read_preview_total")
    )


def _code_draft(plan: WorkflowPlan, *, document_context: dict[str, object]) -> str:
    context_json = json.dumps(document_context, ensure_ascii=False, indent=2)
    context_note = ""
    if _has_document_context(document_context):
        context_note = (
            "# 下面的 DOCUMENT_CONTEXT 来自前置 Document Agent 步骤，"
            "只包含受控工作区的短摘要和命中片段。\n"
        )
    return (
        "# AgentFlow 代码草稿\n"
        "# 该文件由阶段 4B 的安全 Runtime 生成，不会自动执行。\n\n"
        "import json\n\n"
        f"{context_note}"
        f"DOCUMENT_CONTEXT = json.loads({context_json!r})\n\n"
        "def main():\n"
        f"    print({plan.summary!r})\n\n"
        "    if DOCUMENT_CONTEXT.get(\"read_previews\"):\n"
        "        source = DOCUMENT_CONTEXT[\"read_previews\"][0].get(\"relative_path\") or DOCUMENT_CONTEXT[\"read_previews\"][0].get(\"path\")\n"
        "        print(\"来源文档:\", source)\n\n"
        "if __name__ == \"__main__\":\n"
        "    main()\n"
    )


def _report_draft(plan: WorkflowPlan, *, document_context: dict[str, object]) -> str:
    lines = [
        "# AgentFlow Runtime 报告草稿",
        "",
        f"- 工作流：{plan.workflow_name}",
        f"- 摘要：{plan.summary}",
    ]
    if _has_document_context(document_context):
        lines.extend(
            [
                "",
                "## 文档上下文",
                "",
                f"- 来源步骤：{', '.join(str(step) for step in document_context.get('source_steps', [])) or '无'}",
                f"- 搜索命中：{document_context.get('search_match_total', 0)}",
                f"- 读取预览：{document_context.get('read_preview_total', 0)}",
            ]
        )
        search_matches = document_context.get("search_matches", [])
        if isinstance(search_matches, list) and search_matches:
            lines.extend(["", "### 搜索命中片段"])
            for match in search_matches[:5]:
                if not isinstance(match, dict):
                    continue
                preview = _compact_text(str(match.get("preview", "")), limit=160).replace("\n", " ")
                lines.append(
                    f"- {match.get('document_name', '')}:{match.get('line_number', 0)} - {preview}"
                )
        read_previews = document_context.get("read_previews", [])
        if isinstance(read_previews, list) and read_previews:
            lines.extend(["", "### 读取预览"])
            for item in read_previews[:3]:
                if not isinstance(item, dict):
                    continue
                source = item.get("relative_path") or item.get("path") or "未知来源"
                preview = _compact_text(str(item.get("preview", "")), limit=220).replace("\n", " ")
                lines.append(f"- {source}：{preview}")

    lines.extend(["", "## 步骤"])
    for step in plan.steps:
        lines.extend(
            [
                "",
                f"### {step.title}",
                f"- Agent：{step.agent}",
                f"- Action：{step.action}",
                f"- 预期产物：{step.expected_output}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _write_controlled_output(path: Path, text: str) -> int | None:
    """把产物写入受控目录。

    这里集中处理目录创建和编码写入，避免 code/report 两处各写一套易漏的异常路径。
    写入失败返回 `None`，上层统一转成结构化失败。
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return len(text.encode("utf-8"))
    except OSError:
        return None


def _verify_text_artifact(path: Path, *, required_snippets: list[str]) -> dict[str, object]:
    """回读受控文本产物并验证关键片段。

    第一版 Verifier 只做确定性检查：文件必须能按 UTF-8 读回，且包含调用方声明的关键片段。
    这比单纯相信 `write_text` 成功更稳，也不会引入额外模型调用或高成本质量评审。
    """

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "reason": "read_failed",
            "error": str(exc),
            "checked_snippets": len(required_snippets),
            "missing_snippets": required_snippets,
        }

    missing_snippets = [snippet for snippet in required_snippets if snippet not in content]
    return {
        "ok": not missing_snippets,
        "reason": "ok" if not missing_snippets else "missing_snippets",
        "checked_snippets": len(required_snippets),
        "missing_snippets": missing_snippets,
        "read_back_bytes": len(content.encode("utf-8")),
    }


def _execution_message(run: WorkflowRun) -> str:
    if run.status == "completed":
        return "Runtime 已完成安全内置工具执行。"
    if run.status == "waiting_permission":
        return "Runtime 已暂停，等待用户权限确认。"
    if run.status == "blocked":
        return "Runtime 已阻塞，请查看权限或工具调用记录。"
    return run.summary


def _event(
    task_id: str,
    sequence: int,
    event: str,
    agent_id: str,
    message: str,
    step_id: str | None = None,
    level: TaskLogLevel = "info",
) -> TaskLogEvent:
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event,
        agent_id=agent_id,
        step_id=step_id,
        level=level,
        message=message,
    )


def _build_confirmation_message(step: WorkflowStep) -> str:
    return f"步骤 {step.title} 涉及 {_describe_permissions(step.required_permissions)}，需要确认后才能继续。"


def _describe_permissions(required_permissions: list[str]) -> str:
    if not required_permissions:
        return "无额外权限"
    return "、".join(
        _PERMISSION_LABELS.get(permission, permission) for permission in required_permissions
    )


def _tool_name_for_step(step: WorkflowStep) -> str:
    return tool_name_for_step(step)


def _compact_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _now_iso() -> str:
    return _format_dt(datetime.now(UTC))


def _format_dt(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _elapsed_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _rough_token_estimate(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
