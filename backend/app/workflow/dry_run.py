from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import uuid4

from app.database.task_repository import (
    load_task_log_events,
    load_workflow_plan,
    load_workflow_run,
    save_workflow_run,
)
from app.schemas.agent import AgentDescriptor
from app.schemas.chat import WorkflowPlan, WorkflowStep
from app.schemas.events import TaskLogEvent, TaskLogLevel
from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    RuntimePermissionRequest,
    TaskControlResponse,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.workflow.state_machine import can_cancel, can_retry
from app.workflow.node_contracts import tool_name_for_step
from app.workflow.validator import validate_workflow_plan


MAX_STORED_DRY_RUNS = 200
_DRY_RUNS: OrderedDict[str, WorkflowRun] = OrderedDict()
_DRY_RUN_EVENTS: OrderedDict[str, list[TaskLogEvent]] = OrderedDict()
_DRY_RUN_PLANS: OrderedDict[str, WorkflowPlan] = OrderedDict()


_PERMISSION_LABELS: dict[str, str] = {
    "file_read": "文件读取",
    "file_write": "文件写入",
    "network": "网络访问",
    "shell": "Shell 命令",
    "database": "数据库访问",
    "plugin_install": "插件安装",
}


def run_workflow_dry_run(
    *,
    task_id: str,
    plan: WorkflowPlan,
    available_agents: Iterable[AgentDescriptor] | None = None,
    model_routes: list[ModelRouteAuditSnapshot] | None = None,
) -> WorkflowRun:
    """对工作流计划做一次只读 dry-run。

    dry-run 只校验计划并生成“如果执行会发生什么”的结构化结果，不调用真实 Agent Runtime，
    不读取文件、不写文件、不联网、不执行 Shell。这样可以先把任务状态和 WebSocket 日志链路
    接起来，同时保持安全边界清楚。
    """

    started_at = datetime.now(UTC)
    limits = _build_execution_limits(plan)
    validation_errors = validate_workflow_plan(plan, available_agents=available_agents)
    if validation_errors:
        finished_at = datetime.now(UTC)
        run = WorkflowRun(
            task_id=task_id,
            status="failed",
            summary="Workflow dry-run 未通过校验，真实执行已被阻止。",
            max_risk_level=plan.max_risk_level,
            requires_confirmation=plan.requires_confirmation,
            validation_errors=validation_errors,
            steps=[],
            model_routes=model_routes or [],
            limits=limits,
            metrics=_build_execution_metrics(
                plan=plan,
                step_runs=[],
                limits=limits,
                started_at=started_at,
                finished_at=finished_at,
                validation_error_total=len(validation_errors),
            ),
        )
        _remember_run(run, _build_failed_events(run), plan=plan)
        return run

    step_runs = [_simulate_step(step) for step in plan.steps]
    finished_at = datetime.now(UTC)
    run = WorkflowRun(
        task_id=task_id,
        status="completed",
        summary=_build_run_summary(plan),
        max_risk_level=plan.max_risk_level,
        requires_confirmation=plan.requires_confirmation,
        validation_errors=[],
        steps=step_runs,
        model_routes=model_routes or [],
        limits=limits,
        metrics=_build_execution_metrics(
            plan=plan,
            step_runs=step_runs,
            limits=limits,
            started_at=started_at,
            finished_at=finished_at,
            validation_error_total=0,
        ),
    )
    _remember_run(run, _build_step_events(run, plan.steps), plan=plan)
    return run


def get_workflow_run(task_id: str) -> WorkflowRun | None:
    run = _DRY_RUNS.get(task_id)
    if run is not None:
        return run

    # 内存缓存丢失时从 SQLite 恢复，解决服务重启后任务查询 404 的问题。
    run = load_workflow_run(task_id)
    if run is not None:
        _DRY_RUNS[task_id] = run
        _DRY_RUNS.move_to_end(task_id)
    return run


def get_task_log_events(task_id: str) -> list[TaskLogEvent] | None:
    # Runtime 可在计划落库后继续追加导出、联网研究和产物验证事件。SQLite 因此是任务日志的
    # 权威来源；若优先返回旧内存缓存，历史页会漏掉这些真实阶段，重启前后还会看到两套时间线。
    # 此接口只用于日志/历史拉取，不处于高频执行热路径，使用按 task_id 的短查询换取一致性。
    events = load_task_log_events(task_id)
    if events is not None:
        _DRY_RUN_EVENTS[task_id] = events
        _DRY_RUN_EVENTS.move_to_end(task_id)
        return list(events)

    # 未落库的极短生命周期测试/模拟事件仍允许从内存读取，保持 dry-run 基础能力兼容。
    events = _DRY_RUN_EVENTS.get(task_id)
    if events is not None:
        return list(events)
    return None


def clear_dry_run_memory_cache() -> None:
    """清空 dry-run 内存缓存。

    这个函数只给验证脚本和后续测试使用，用来模拟服务重启后的状态；不会删除 SQLite
    中已经持久化的任务数据。
    """

    _DRY_RUNS.clear()
    _DRY_RUN_EVENTS.clear()
    _DRY_RUN_PLANS.clear()


def request_cancel(task_id: str) -> TaskControlResponse | None:
    """处理任务取消请求。

    当前 dry-run 是同步瞬时完成的，没有后台任务可取消；这里先固定 API 语义，
    真实 Runtime 进入 pending/running/waiting_permission 时会复用这个入口，并把取消结果落库。
    """

    run = get_workflow_run(task_id)
    if run is None:
        return None

    if can_cancel(run.status):
        now = datetime.now(UTC)
        cancelled_steps = [
            _cancel_step(step)
            if step.status in {"pending", "running", "waiting_permission"}
            else step
            for step in run.steps
        ]
        cancelled_metrics = run.metrics.model_copy(
            update={"finished_at": _format_dt(now)}
        )
        cancelled_run = run.model_copy(
            update={
                "status": "cancelled",
                "summary": "任务已取消，未继续执行后续步骤。",
                "steps": cancelled_steps,
                "metrics": cancelled_metrics,
            }
        )
        events = get_task_log_events(task_id) or []
        next_sequence = max((event.sequence for event in events), default=0) + 1
        events.append(
            _event(
                task_id,
                next_sequence,
                "task_cancelled",
                "workflow_engine",
                "用户取消了任务，Runtime 不会继续触发后续工具。",
                level="warning",
            )
        )
        plan = _DRY_RUN_PLANS.get(task_id) or load_workflow_plan(task_id)
        save_workflow_run(run=cancelled_run, events=events, plan=plan)
        # 取消是用户可见状态，必须同步更新开发期缓存，避免 UI 刷新读到旧的 waiting 状态。
        _DRY_RUNS[task_id] = cancelled_run
        _DRY_RUNS.move_to_end(task_id)
        _DRY_RUN_EVENTS[task_id] = events
        _DRY_RUN_EVENTS.move_to_end(task_id)
        if plan is not None:
            _DRY_RUN_PLANS[task_id] = plan
            _DRY_RUN_PLANS.move_to_end(task_id)
        return TaskControlResponse(
            task_id=task_id,
            action="cancel",
            accepted=True,
            status="cancelled",
            message="任务已取消，后续工具不会继续执行。",
            workflow_run=cancelled_run,
        )

    return TaskControlResponse(
        task_id=task_id,
        action="cancel",
        accepted=False,
        status=run.status,
        message="当前 dry-run 已结束，没有正在运行的步骤可取消。",
        workflow_run=run,
    )


def _cancel_step(step: WorkflowStepRun) -> WorkflowStepRun:
    """把尚未完成的步骤标记为取消。

    已 completed/failed/blocked 的步骤保留原状态，避免取消操作篡改已经发生过的执行事实。
    """

    return step.model_copy(
        update={
            "status": "cancelled",
            "message": "任务已被用户取消，该步骤未继续执行。",
            "output": {**step.output, "cancelled": True},
        }
    )


def retry_workflow_dry_run(task_id: str) -> TaskControlResponse | None:
    """基于缓存计划重新生成一次 dry-run。

    retry 不重新调用 Commander 或 LLM，只复用最初的结构化计划，保证成本稳定。
    服务重启或缓存淘汰后计划会丢失，届时返回 None，让 API 层用 404 提醒前端。
    """

    original_run = get_workflow_run(task_id)
    plan = _DRY_RUN_PLANS.get(task_id) or load_workflow_plan(task_id)
    if original_run is None or plan is None:
        return None
    if not can_retry(original_run.status):
        return TaskControlResponse(
            task_id=task_id,
            action="retry",
            accepted=False,
            status=original_run.status,
            message="当前任务尚未进入可重试状态。",
            workflow_run=original_run,
        )

    new_task_id = f"{task_id}_retry_{uuid4().hex[:8]}"
    new_run = run_workflow_dry_run(task_id=new_task_id, plan=plan)
    return TaskControlResponse(
        task_id=task_id,
        action="retry",
        accepted=True,
        status=new_run.status,
        message="已基于缓存 workflow_plan 生成新的 dry-run。",
        new_task_id=new_task_id,
        workflow_run=new_run,
    )


def _simulate_step(step: WorkflowStep) -> WorkflowStepRun:
    confirmation_note = (
        f"真实执行前需要用户确认：{_describe_permissions(step.required_permissions)}。"
        if step.requires_confirmation
        else "无需额外确认即可进入后续模拟步骤。"
    )
    return WorkflowStepRun(
        step_id=step.id,
        agent=step.agent,
        action=step.action,
        status="completed",
        message=f"dry-run 已模拟步骤：{step.title}。{confirmation_note}",
        requires_confirmation=step.requires_confirmation,
        risk_level=step.risk_level,
        output={
            "dry_run": True,
            "would_execute": step.action,
            "expected_output": step.expected_output,
            "required_permissions": step.required_permissions,
            "permission_summary": _describe_permissions(step.required_permissions),
            "confirmation_required": step.requires_confirmation,
        },
    )


def _build_run_summary(plan: WorkflowPlan) -> str:
    confirmation_step_count = sum(1 for step in plan.steps if step.requires_confirmation)
    if confirmation_step_count:
        return (
            "Workflow dry-run 完成；"
            f"{len(plan.steps)} 个步骤中有 {confirmation_step_count} 个需要用户确认敏感权限。"
        )
    return "Workflow dry-run 完成；当前计划未发现需要确认的敏感步骤。"


def _build_execution_limits(plan: WorkflowPlan) -> RuntimeExecutionLimits:
    """生成单次任务预算。

    当前先使用保守固定值，让 Runtime 协议稳定下来；如果后续 Commander 规划出更长任务，
    可以在这里根据任务类型、用户配置或模型成本动态调整预算。
    """

    return RuntimeExecutionLimits(
        max_steps=20,
        max_tool_calls=max(50, len(plan.steps) * 3),
        max_retries_per_tool=2,
        tool_timeout_ms=30_000,
        task_timeout_ms=120_000,
        token_budget=None,
    )


def _build_execution_metrics(
    *,
    plan: WorkflowPlan,
    step_runs: list[WorkflowStepRun],
    limits: RuntimeExecutionLimits,
    started_at: datetime,
    finished_at: datetime,
    validation_error_total: int,
) -> RuntimeExecutionMetrics:
    """生成任务运行指标。

    dry-run 没有真实 token 用量和工具耗时，因此这里只做低成本估算；真实 LLM / Runtime
    接入后会把供应商返回的 token、工具耗时和失败次数写回同一个结构。
    """

    step_total = len(plan.steps)
    tool_call_total = len(step_runs)
    input_estimate = _rough_token_estimate(plan.model_dump_json())
    output_estimate = _rough_token_estimate(
        "\n".join(step.output.get("expected_output", "") for step in step_runs)
    )
    return RuntimeExecutionMetrics(
        started_at=_format_dt(started_at),
        finished_at=_format_dt(finished_at),
        duration_ms=_elapsed_ms(started_at, finished_at),
        step_total=step_total,
        step_completed=sum(1 for step in step_runs if step.status == "completed"),
        step_failed=sum(1 for step in step_runs if step.status == "failed"),
        tool_call_total=tool_call_total,
        tool_call_simulated=tool_call_total,
        tool_call_failed=0,
        retry_total=0,
        permission_request_total=sum(1 for step in plan.steps if step.requires_confirmation),
        validation_error_total=validation_error_total,
        estimated_input_tokens=input_estimate,
        estimated_output_tokens=output_estimate,
        estimated_cost_cny=0.0,
        budget_exceeded=step_total > limits.max_steps or tool_call_total > limits.max_tool_calls,
    )


def _build_failed_events(run: WorkflowRun) -> list[TaskLogEvent]:
    message = "；".join(run.validation_errors) if run.validation_errors else "未知校验错误。"
    return [
        _event(run.task_id, 1, "connected", "system", "已连接 AgentFlow 任务日志通道。"),
        _event(
            run.task_id,
            2,
            "task_failed",
            "workflow_engine",
            f"Workflow dry-run 校验失败：{message}",
            level="error",
        ),
    ]


def _build_step_events(run: WorkflowRun, plan_steps: list[WorkflowStep]) -> list[TaskLogEvent]:
    confirmation_step_count = 0
    events = [
        _event(run.task_id, 1, "connected", "system", "已连接 AgentFlow 任务日志通道。"),
        _event(
            run.task_id,
            2,
            "task_started",
            "workflow_engine",
            "Workflow Engine 已进入 dry-run 模式，只模拟执行，不触发真实工具。",
        ),
    ]
    sequence = 3
    plan_by_step_id = {step.id: step for step in plan_steps}
    for step_run in run.steps:
        plan_step = plan_by_step_id.get(step_run.step_id)
        title = plan_step.title if plan_step else step_run.action
        if plan_step is not None and plan_step.requires_confirmation:
            confirmation_step_count += 1
            events.append(
                _event(
                    run.task_id,
                    sequence,
                    "confirmation_required",
                    "workflow_engine",
                    _build_confirmation_message(plan_step),
                    plan_step.id,
                    level="warning",
                )
            )
            sequence += 1
        events.append(
            _event(
                run.task_id,
                sequence,
                "step_started",
                step_run.agent,
                f"开始 dry-run：{title}",
                step_run.step_id,
            )
        )
        sequence += 1
        events.append(
            _event(
                run.task_id,
                sequence,
                "step_completed",
                step_run.agent,
                step_run.message,
                step_run.step_id,
            )
        )
        sequence += 1

    completed_message = run.summary
    if run.requires_confirmation:
        completed_message += (
            " 请在后续真实执行前确认写入、联网、Shell 或数据库等敏感权限。"
            f" 本次共有 {confirmation_step_count} 个步骤需要确认。"
        )
    events.append(
        _event(run.task_id, sequence, "task_completed", "workflow_engine", completed_message)
    )
    return events


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
    """把技术型权限名翻成更适合审查的提示文案。

    dry-run 阶段不真的等待用户点击确认，但日志要明确告诉前端：
    真实执行时哪些步骤会卡在权限确认点。
    """

    permissions = _describe_permissions(step.required_permissions)
    return f"步骤 {step.title} 涉及 {permissions}，真实执行前需要用户确认。"


def _describe_permissions(required_permissions: list[str]) -> str:
    if not required_permissions:
        return "无额外权限"

    return "、".join(
        _PERMISSION_LABELS.get(permission, permission) for permission in required_permissions
    )


def _remember_run(run: WorkflowRun, events: list[TaskLogEvent], *, plan: WorkflowPlan | None) -> None:
    """保存最近的 dry-run 结果，供 WebSocket 用 task_id 取日志。

    这是开发期内存缓存；SQLite 仍会保存 workflow_runs/workflow_steps/workflow_events，
    服务重启后可以恢复。达到上限时只淘汰内存里的最旧任务，避免长时间运行造成内存增长。
    """

    _DRY_RUNS[run.task_id] = run
    _DRY_RUNS.move_to_end(run.task_id)
    _DRY_RUN_EVENTS[run.task_id] = events
    _DRY_RUN_EVENTS.move_to_end(run.task_id)
    if plan is not None:
        _DRY_RUN_PLANS[run.task_id] = plan
        _DRY_RUN_PLANS.move_to_end(run.task_id)
    permission_requests = _build_permission_requests(run.task_id, plan) if plan else []
    artifacts: list[WorkflowArtifact] = []
    tool_calls: list[WorkflowToolCall] = []
    if run.validation_errors:
        # 计划校验失败时不创建可批准的权限请求，避免用户误以为坏计划可以被放行。
        permission_requests = []
    elif plan is not None:
        artifacts = _build_artifacts(run.task_id, plan)
        tool_calls = _build_tool_calls(run.task_id, plan)

    save_workflow_run(
        run=run,
        events=events,
        plan=plan,
        permission_requests=permission_requests,
        artifacts=artifacts,
        tool_calls=tool_calls,
    )

    while len(_DRY_RUNS) > MAX_STORED_DRY_RUNS:
        old_task_id, _ = _DRY_RUNS.popitem(last=False)
        _DRY_RUN_EVENTS.pop(old_task_id, None)
        _DRY_RUN_PLANS.pop(old_task_id, None)


def _build_permission_requests(
    task_id: str,
    plan: WorkflowPlan,
) -> list[RuntimePermissionRequest]:
    """把 dry-run 计划中的敏感步骤转成待确认权限请求。

    当前请求只用于审查和前端展示；真实 Agent Runtime 接入后，会在执行到对应 step 前读取
    这些决策结果，只有 approved 才能继续触发文件写入、Shell、联网等能力。
    """

    requests: list[RuntimePermissionRequest] = []
    for step in plan.steps:
        if not step.requires_confirmation:
            continue

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
                    "dry_run": True,
                    "action": step.action,
                    "title": step.title,
                    "reason": step.reason,
                    "expected_output": step.expected_output,
                    "permission_summary": _describe_permissions(step.required_permissions),
                },
            )
        )

    return requests


def _build_artifacts(task_id: str, plan: WorkflowPlan) -> list[WorkflowArtifact]:
    """把计划步骤转换成 dry-run 虚拟产物目录。

    这些 artifact 只代表“真实执行成功后可能出现的结果”，不会创建本地文件。后续真实
    Runtime 生成文件时，可以复用 artifact_id/step_id 关系并把 uri 替换成受控文件地址。
    """

    artifacts: list[WorkflowArtifact] = []
    for step in plan.steps:
        kind = _artifact_kind_for_step(step)
        artifact_id = f"{task_id}:{step.id}:artifact"
        artifacts.append(
            WorkflowArtifact(
                artifact_id=artifact_id,
                task_id=task_id,
                step_id=step.id,
                agent_id=step.agent,
                kind=kind,
                name=_artifact_name_for_step(step, kind),
                summary=step.expected_output or f"{step.title} 的 dry-run 预期产物。",
                uri=f"artifact://dry-run/{task_id}/{step.id}",
                mime_type=_mime_type_for_artifact(kind),
                metadata={
                    "dry_run": True,
                    "action": step.action,
                    "title": step.title,
                    "requires_confirmation": step.requires_confirmation,
                    "required_permissions": step.required_permissions,
                },
                created_at=_now_iso(),
            )
        )

    return artifacts


def _build_tool_calls(task_id: str, plan: WorkflowPlan) -> list[WorkflowToolCall]:
    """生成 dry-run 工具调用审计记录。

    ToolCall 是真实 Runtime 的最小可观测单元：它把“模型/Agent 想做什么”和“Runtime 是否
    真的执行”分开。当前 status=simulated，明确表示没有触发文件写入、联网、Shell 等能力。
    """

    tool_calls: list[WorkflowToolCall] = []
    limits = _build_execution_limits(plan)
    for step in plan.steps:
        tool_calls.append(
            WorkflowToolCall(
                call_id=f"{task_id}:{step.id}:tool",
                task_id=task_id,
                step_id=step.id,
                agent_id=step.agent,
                tool_name=_tool_name_for_step(step),
                status="simulated",
                risk_level=step.risk_level,
                permission_required=step.requires_confirmation,
                attempt=1,
                max_attempts=limits.max_retries_per_tool + 1,
                timeout_ms=limits.tool_timeout_ms,
                duration_ms=0,
                failure_count=0,
                request={
                    "dry_run": True,
                    "action": step.action,
                    "input": step.input,
                    "required_permissions": step.required_permissions,
                    "expected_output": step.expected_output,
                },
                result={
                    "simulated": True,
                    "would_execute": step.action,
                    "permission_summary": _describe_permissions(step.required_permissions),
                    "confirmation_required": step.requires_confirmation,
                },
                started_at=_now_iso(),
                finished_at=_now_iso(),
            )
        )

    return tool_calls


def _artifact_kind_for_step(step: WorkflowStep) -> str:
    if step.agent == "code_agent":
        return "code"
    if step.agent == "report_agent":
        return "report"
    if step.agent == "document_agent":
        return "markdown"
    if step.agent == "commander_agent":
        return "text"
    return "other"


def _artifact_name_for_step(step: WorkflowStep, kind: str) -> str:
    if kind == "code":
        return "代码草稿"
    if kind == "report":
        return "报告草稿"
    if kind == "markdown":
        return "文档分析摘要"
    if kind == "text":
        return "任务规划摘要"
    return f"{step.title} 产物草稿"


def _mime_type_for_artifact(kind: str) -> str:
    if kind in {"markdown", "report"}:
        return "text/markdown"
    if kind == "code":
        return "text/plain"
    return "text/plain"


def _tool_name_for_step(step: WorkflowStep) -> str:
    # dry-run 和真实 Runtime 使用同一套 Node Contract，避免审计里同一步出现两种工具名。
    return tool_name_for_step(step)


def _now_iso() -> str:
    return _format_dt(datetime.now(UTC))


def _format_dt(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _elapsed_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _rough_token_estimate(text: str) -> int:
    """用字符数粗略估算 token 数。

    供应商没有返回真实 token 时，先用这个估算值做趋势观察。它不能用于精确计费，
    但足够提醒我们某次规划是否明显膨胀。
    """

    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
