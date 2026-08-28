from __future__ import annotations

from app.schemas.workflow import (
    RuntimePermissionItem,
    WorkflowRun,
    WorkflowTaskEvaluationResponse,
    WorkflowToolCall,
)


def evaluate_workflow_task(
    *,
    run: WorkflowRun,
    tool_calls: list[WorkflowToolCall],
    permissions: list[RuntimePermissionItem],
) -> WorkflowTaskEvaluationResponse:
    """根据已落库的运行事实计算任务效果评估。

    这里故意只使用现有 metrics / tool-calls / permissions，不重新调用模型、不扫描产物目录。
    评估接口会被历史页高频查看，保持纯内存计算能避免把普通浏览操作变成重 IO 路径。
    """

    step_success_rate = _safe_ratio(run.metrics.step_completed, run.metrics.step_total)
    tool_success_rate = _tool_success_rate(tool_calls)
    pending_permissions = sum(1 for item in permissions if item.decision.decision == "pending")
    denied_permissions = sum(1 for item in permissions if item.decision.decision == "denied")
    blocked_tool_calls = sum(1 for call in tool_calls if call.status == "blocked")
    failed_tool_calls = sum(1 for call in tool_calls if call.status == "failed")
    efficiency_score = _efficiency_score(
        run=run,
        pending_permissions=pending_permissions,
        denied_permissions=denied_permissions,
        blocked_tool_calls=blocked_tool_calls,
        failed_tool_calls=failed_tool_calls,
    )
    overall_score = round(
        step_success_rate * 0.50 + tool_success_rate * 0.30 + efficiency_score * 0.20,
        3,
    )
    outcome = _outcome(run)
    warnings = _warnings(
        run=run,
        pending_permissions=pending_permissions,
        denied_permissions=denied_permissions,
        blocked_tool_calls=blocked_tool_calls,
        failed_tool_calls=failed_tool_calls,
    )
    recommendations = _recommendations(
        run=run,
        pending_permissions=pending_permissions,
        denied_permissions=denied_permissions,
        failed_tool_calls=failed_tool_calls,
    )

    return WorkflowTaskEvaluationResponse(
        task_id=run.task_id,
        mode=run.mode,
        status=run.status,
        outcome=outcome,
        summary=_summary(run, outcome),
        step_success_rate=step_success_rate,
        tool_success_rate=tool_success_rate,
        efficiency_score=efficiency_score,
        overall_score=overall_score,
        duration_ms=run.metrics.duration_ms,
        retry_total=run.metrics.retry_total,
        failed_tool_calls=failed_tool_calls,
        blocked_tool_calls=blocked_tool_calls,
        pending_permissions=pending_permissions,
        denied_permissions=denied_permissions,
        warnings=warnings,
        recommendations=recommendations,
    )


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(max(0.0, min(1.0, numerator / denominator)), 3)


def _tool_success_rate(tool_calls: list[WorkflowToolCall]) -> float:
    if not tool_calls:
        return 0.0
    # dry-run 的 simulated 表示工具选择和参数形态已通过预演，但还不代表真实执行成功。
    successful = sum(1 for call in tool_calls if call.status in {"completed", "simulated"})
    return _safe_ratio(successful, len(tool_calls))


def _efficiency_score(
    *,
    run: WorkflowRun,
    pending_permissions: int,
    denied_permissions: int,
    blocked_tool_calls: int,
    failed_tool_calls: int,
) -> float:
    retry_penalty = min(0.30, run.metrics.retry_total * 0.08)
    failure_penalty = min(0.40, (run.metrics.step_failed + failed_tool_calls) * 0.15)
    permission_penalty = min(0.25, pending_permissions * 0.06 + denied_permissions * 0.12)
    blocked_penalty = min(0.25, blocked_tool_calls * 0.12)
    budget_penalty = 0.25 if run.metrics.budget_exceeded else 0.0
    score = 1.0 - retry_penalty - failure_penalty - permission_penalty - blocked_penalty - budget_penalty
    return round(max(0.0, min(1.0, score)), 3)


def _outcome(run: WorkflowRun) -> str:
    if run.status == "waiting_permission":
        return "waiting_permission"
    if run.status == "blocked":
        return "blocked"
    if run.status == "failed":
        return "failed"
    if run.status == "cancelled":
        return "cancelled"
    if run.status in {"pending", "running"}:
        return "running"
    if run.mode == "dry_run" and run.status == "completed":
        return "dry_run_ready"
    if run.status == "completed":
        return "completed"
    return "needs_attention"


def _summary(run: WorkflowRun, outcome: str) -> str:
    if outcome == "dry_run_ready":
        return "预演已通过；还没有产生真实副作用，可由用户确认后转入 runtime。"
    if outcome == "completed":
        return "真实执行已完成，当前指标未发现阻塞性问题。"
    if outcome == "waiting_permission":
        return "任务暂停在权限确认处，用户批准或拒绝后 Runtime 才会继续。"
    if outcome == "blocked":
        return "任务已阻塞，需要处理权限拒绝、上下文缺失或工具错误后再继续。"
    if outcome == "failed":
        return "任务执行失败，请优先查看失败步骤、工具调用和结构化错误。"
    if outcome == "cancelled":
        return "任务已取消，可按原计划 retry 生成新的执行记录。"
    if outcome == "running":
        return "任务仍在执行链路中，评估结果会随运行状态继续变化。"
    return run.summary or "任务需要进一步检查。"


def _warnings(
    *,
    run: WorkflowRun,
    pending_permissions: int,
    denied_permissions: int,
    blocked_tool_calls: int,
    failed_tool_calls: int,
) -> list[str]:
    warnings: list[str] = []
    if run.mode == "dry_run":
        warnings.append("当前只是 dry-run 预演，任务成功率不代表真实文件或工具已经执行。")
    if run.validation_errors:
        warnings.append(f"计划存在 {len(run.validation_errors)} 个校验问题。")
    if pending_permissions:
        warnings.append(f"还有 {pending_permissions} 个权限请求等待确认。")
    if denied_permissions:
        warnings.append(f"已有 {denied_permissions} 个权限请求被拒绝。")
    if failed_tool_calls:
        warnings.append(f"有 {failed_tool_calls} 次工具调用失败。")
    if blocked_tool_calls:
        warnings.append(f"有 {blocked_tool_calls} 次工具调用被阻塞。")
    if run.metrics.retry_total:
        warnings.append(f"已发生 {run.metrics.retry_total} 次自动重试。")
    if run.metrics.budget_exceeded:
        warnings.append("任务已超过当前执行预算。")
    return warnings


def _recommendations(
    *,
    run: WorkflowRun,
    pending_permissions: int,
    denied_permissions: int,
    failed_tool_calls: int,
) -> list[str]:
    recommendations: list[str] = []
    if run.mode == "dry_run" and run.status == "completed":
        recommendations.append("确认计划和权限摘要无误后，再点击“开始执行”。")
    if pending_permissions:
        recommendations.append("先在权限确认区审查敏感步骤，再决定批准或拒绝。")
    if denied_permissions:
        recommendations.append("如需继续执行，请调整任务目标或重新发起 dry-run，避免重复触发被拒绝权限。")
    if failed_tool_calls:
        recommendations.append("查看工具调用记录中的 error/result，优先修正参数、路径或文件类型。")
    if run.status == "blocked":
        recommendations.append("处理阻塞原因后再点击“继续执行”，或使用 retry 生成新的执行记录。")
    if run.status == "failed":
        recommendations.append("失败原因明确后再 retry，避免 Agent 对同一错误重复尝试。")
    if not recommendations:
        recommendations.append("当前无需额外处理，可继续查看产物、日志或发起下一轮任务。")
    return recommendations
