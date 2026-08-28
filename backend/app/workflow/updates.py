from __future__ import annotations

from app.database.task_repository import load_workflow_task_progress_snapshot
from app.schemas.events import TaskLogEvent
from app.schemas.workflow import (
    RuntimePermissionItem,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowTaskEvaluationResponse,
    WorkflowTaskUpdate,
    WorkflowTaskUpdateListResponse,
    WorkflowToolCall,
)
from app.workflow.evaluation import evaluate_workflow_task


# C5.2 只把 K4 的状态镜像回 Commander。其他专业 Agent 后续要接入时，应同样提供
# “状态 + 聚合计数”的轻量读取器，不能让父任务刷新去搬运子任务的正文、来源或模型轨迹。
_KNOWLEDGE_DEEP_TASK_PREFIX = "task_k4_"
_KNOWLEDGE_DEEP_MAP_ACTION = "knowledge.deep_map"
_KNOWLEDGE_DEEP_REDUCE_ACTIONS = frozenset(
    {
        "knowledge.deep_reduce_batch",
        "knowledge.deep_reduce_final",
    }
)
_ACTIVE_DELEGATION_STATUSES = frozenset({"queued", "pending", "running", "waiting_permission"})


def build_task_updates(
    *,
    run: WorkflowRun,
    events: list[TaskLogEvent],
    tool_calls: list[WorkflowToolCall],
    artifacts: list[WorkflowArtifact],
    permissions: list[RuntimePermissionItem],
) -> WorkflowTaskUpdateListResponse:
    """把分散的运行事实整理成给 UI 消费的 updates 时间线。

    后端仍保留 logs/steps/tool-calls/artifacts/permissions 这些精确接口；updates 是一层
    只读聚合视图，目的是让前端不用重复拉取多份数据再自己猜顺序和关联关系。
    """

    step_by_id = {step.step_id: step for step in run.steps}
    tools_by_step = _group_by_step(tool_calls)
    artifacts_by_step = _group_by_step(artifacts)
    permissions_by_step = _group_permissions_by_step(permissions)
    evaluation = evaluate_workflow_task(
        run=run,
        tool_calls=tool_calls,
        permissions=permissions,
    )

    updates: list[WorkflowTaskUpdate] = []
    emitted_artifact_ids: set[str] = set()
    for event in events:
        step = step_by_id.get(event.step_id or "")
        related_tools = tools_by_step.get(event.step_id or "", [])
        related_artifacts = artifacts_by_step.get(event.step_id or "", [])
        related_permissions = permissions_by_step.get(event.step_id or "", [])
        payload = {
            "log": event.model_dump(mode="json"),
        }
        if step is not None:
            payload["step"] = step.model_dump(mode="json")
        if related_tools:
            payload["tool_calls"] = [tool.model_dump(mode="json") for tool in related_tools]
        if related_permissions:
            payload["permissions"] = [
                permission.model_dump(mode="json")
                for permission in related_permissions
            ]

        updates.append(
            WorkflowTaskUpdate(
                sequence=event.sequence * 10,
                update_type=_update_type_for_event(event.event),
                event=event.event,
                level=event.level,
                agent_id=event.agent_id,
                step_id=event.step_id,
                status=_status_for_event(run=run, event=event, step_status=step.status if step else ""),
                title=_title_for_event(event.event),
                message=event.message,
                occurred_at=event.created_at.isoformat(),
                payload=payload,
            )
        )

        # 产物是用户最关心的结果之一；在对应步骤完成后补一条专门 update，方便 Qt 后续
        # 直接做“产物已计划/已生成”的时间线，不必从 step.output 里猜。
        if event.event == "step_completed" and related_artifacts:
            for artifact_index, artifact in enumerate(related_artifacts, start=1):
                if artifact.artifact_id in emitted_artifact_ids:
                    continue
                emitted_artifact_ids.add(artifact.artifact_id)
                updates.append(
                    _artifact_update(
                        artifact=artifact,
                        base_sequence=event.sequence * 10 + artifact_index,
                        occurred_at=artifact.created_at or event.created_at.isoformat(),
                        step=step,
                        tool_calls=related_tools,
                    )
                )

    # 某些失败/等待场景可能没有 step_completed，但仍有已登记产物；最后补齐未发出的产物。
    next_sequence = (max((event.sequence for event in events), default=0) + 1) * 10
    for artifact in artifacts:
        if artifact.artifact_id in emitted_artifact_ids:
            continue
        step = step_by_id.get(artifact.step_id or "")
        updates.append(
            _artifact_update(
                artifact=artifact,
                base_sequence=next_sequence,
                occurred_at=artifact.created_at,
                step=step,
                tool_calls=tools_by_step.get(artifact.step_id or "", []),
            )
        )
        next_sequence += 1

    updates.append(
        WorkflowTaskUpdate(
            sequence=next_sequence,
            update_type="state",
            event="task_state_snapshot",
            level="info" if run.status == "completed" else _level_for_status(run.status),
            agent_id="workflow_engine",
            status=run.status,
            title="任务状态",
            message=run.summary,
            occurred_at=run.metrics.finished_at or run.metrics.started_at,
            payload={
                "mode": run.mode,
                "status": run.status,
                "metrics": run.metrics.model_dump(mode="json"),
                "limits": run.limits.model_dump(mode="json"),
                "step_total": len(run.steps),
                "tool_call_total": len(tool_calls),
                "artifact_total": len(artifacts),
                "permission_total": len(permissions),
                # 状态快照是 UI 最稳定的终点事件；把轻量复盘放在这里，前端不用再额外
                # 请求 evaluation 才能告诉用户“结果怎么样、下一步做什么”。
                "evaluation": evaluation.model_dump(mode="json"),
                "task_retrospective": _retrospective_payload(
                    run=run,
                    evaluation=evaluation,
                    artifacts=artifacts,
                    tool_calls=tool_calls,
                    permissions=permissions,
                ),
            },
        )
    )

    updates.sort(key=lambda item: item.sequence)
    return WorkflowTaskUpdateListResponse(
        task_id=run.task_id,
        total=len(updates),
        updates=updates,
    )


def _retrospective_payload(
    *,
    run: WorkflowRun,
    evaluation: WorkflowTaskEvaluationResponse,
    artifacts: list[WorkflowArtifact],
    tool_calls: list[WorkflowToolCall],
    permissions: list[RuntimePermissionItem],
) -> dict:
    """生成给 UI 直接展示的轻量任务复盘。

    evaluation 偏指标，retrospective 偏产品表达：它只复用已经传入 updates 的事实，
    不重新读文件、不扫描目录、不调用模型，避免一个状态刷新接口变成高成本路径。
    """

    return {
        "outcome": evaluation.outcome,
        "summary": evaluation.summary,
        "mode": run.mode,
        "status": run.status,
        "score": {
            "overall": evaluation.overall_score,
            "step_success": evaluation.step_success_rate,
            "tool_success": evaluation.tool_success_rate,
            "efficiency": evaluation.efficiency_score,
        },
        "facts": {
            "step_total": len(run.steps),
            "step_completed": run.metrics.step_completed,
            "tool_call_total": len(tool_calls),
            "artifact_total": len(artifacts),
            "permission_total": len(permissions),
            "retry_total": evaluation.retry_total,
            "duration_ms": evaluation.duration_ms,
        },
        "warnings": evaluation.warnings[:5],
        "recommendations": evaluation.recommendations[:5],
        # 父任务只呈现已落库的委派身份与短摘要。完整来源、模型输出和 Tool trace 保持在
        # 专业 Agent 子任务中，既可追溯，也避免一次状态轮询复制大段客户材料。
        "delegations": _delegation_retrospective_items(tool_calls),
    }


def _delegation_retrospective_items(tool_calls: list[WorkflowToolCall]) -> list[dict]:
    """提取父任务可安全展示的专业 Agent 委派状态。

    Tool 回执只能说明“当时已受理”，不能代表后台子任务之后是否完成、暂停或失败。对于已支持
    的 K4 任务，这里再读取一份聚合状态快照，保持父子任务的职责边界：父任务不复制结果正文，
    只镜像客户判断下一步所需的状态和 Map/Reduce 进度。
    """

    items: list[dict] = []
    seen_task_ids: set[str] = set()
    for call in tool_calls:
        result = call.result if isinstance(call.result, dict) else {}
        delegated_task_id = str(result.get("delegated_task_id", "")).strip()
        if not delegated_task_id or delegated_task_id in seen_task_ids:
            continue
        seen_task_ids.add(delegated_task_id)
        reply = str(result.get("reply", "")).strip()
        item = {
            "agent_id": call.agent_id,
            "task_id": delegated_task_id,
            "status": str(result.get("agent_status", call.status)),
            "stop_reason": str(result.get("stop_reason", "")),
            "summary": reply[:240],
            "next_action": _delegation_next_action(str(result.get("agent_status", call.status))),
        }
        item.update(_knowledge_deep_delegation_snapshot(delegated_task_id))
        items.append(item)
        if len(items) >= 4:
            break
    return items


def _knowledge_deep_delegation_snapshot(task_id: str) -> dict:
    """返回 K4 子任务的最小状态镜像；非 K4 任务保持旧回执兼容。"""

    if not task_id.startswith(_KNOWLEDGE_DEEP_TASK_PREFIX):
        return {}
    snapshot = load_workflow_task_progress_snapshot(task_id)
    if snapshot is None:
        # 理论上 Dispatcher 成功回执前已创建 checkpoint；仍保留降级路径，避免一次历史清理或
        # 旧版本任务让父任务详情读取失败。这里不把“未找到”伪装成成功，只保留原受理回执。
        return {"status_source": "handoff_receipt"}

    counts = snapshot.action_status_counts
    map_total = _action_total(counts, {_KNOWLEDGE_DEEP_MAP_ACTION})
    map_completed = _action_status_total(counts, {_KNOWLEDGE_DEEP_MAP_ACTION}, "completed")
    map_failed = _action_status_total(counts, {_KNOWLEDGE_DEEP_MAP_ACTION}, "failed")
    map_cancelled = _action_status_total(counts, {_KNOWLEDGE_DEEP_MAP_ACTION}, "cancelled")
    reduce_total = _action_total(counts, _KNOWLEDGE_DEEP_REDUCE_ACTIONS)
    reduce_completed = _action_status_total(counts, _KNOWLEDGE_DEEP_REDUCE_ACTIONS, "completed")
    reduce_failed = _action_status_total(counts, _KNOWLEDGE_DEEP_REDUCE_ACTIONS, "failed")
    reduce_cancelled = _action_status_total(counts, _KNOWLEDGE_DEEP_REDUCE_ACTIONS, "cancelled")

    return {
        "status": snapshot.status,
        "summary": snapshot.summary[:240] or "关联知识库深度任务已保存状态快照。",
        "next_action": _delegation_next_action(snapshot.status),
        "status_source": "child_checkpoint",
        "delegation_kind": "knowledge_deep",
        "map_completed": map_completed,
        "map_total": map_total,
        "map_failed": map_failed,
        "map_cancelled": map_cancelled,
        "reduce_completed": reduce_completed,
        "reduce_total": reduce_total,
        "reduce_failed": reduce_failed,
        "reduce_cancelled": reduce_cancelled,
    }


def _action_total(counts: dict[tuple[str, str], int], actions: set[str] | frozenset[str]) -> int:
    """汇总限定 action 的步骤数；SQL 已按状态聚合，循环规模固定且很小。"""

    return sum(count for (action, _status), count in counts.items() if action in actions)


def _action_status_total(
    counts: dict[tuple[str, str], int],
    actions: set[str] | frozenset[str],
    target_status: str,
) -> int:
    """读取某类 K4 节点在一个终态中的数量。"""

    return sum(
        count
        for (action, status), count in counts.items()
        if action in actions and status == target_status
    )


def _delegation_next_action(status: str) -> str:
    """把子任务真实状态转换为父任务可读的下一步，不泄漏内部检查点。"""

    if status in _ACTIVE_DELEGATION_STATUSES:
        return "关联子任务仍在执行；可留在本页查看自动更新，或打开关联任务查看完整进度。"
    if status in {"paused", "blocked"}:
        return "关联子任务已暂停或需要处理；请打开关联任务后继续、重试或结束。"
    if status == "completed":
        return "关联子任务已完成；可打开关联任务查看完整来源、结论与正式报告资格。"
    if status == "cancelled":
        return "关联子任务已结束；可打开关联任务查看已保留的范围与检查点。"
    if status == "failed":
        return "关联子任务未完成；请打开关联任务查看失败原因和可恢复状态。"
    return "可在关联子任务查看完整来源、工具调用与结果详情。"


def _group_by_step(items):
    grouped: dict[str, list] = {}
    for item in items:
        grouped.setdefault(item.step_id, []).append(item)
    return grouped


def _group_permissions_by_step(
    permissions: list[RuntimePermissionItem],
) -> dict[str, list[RuntimePermissionItem]]:
    grouped: dict[str, list[RuntimePermissionItem]] = {}
    for item in permissions:
        grouped.setdefault(item.request.step_id, []).append(item)
    return grouped


def _update_type_for_event(event: str) -> str:
    if event == "artifact_saved":
        return "artifact"
    if event in {
        "confirmation_required",
        "permission_required",
        "permission_auto_approved",
        "permission_denied",
    }:
        return "permission"
    if event in {"step_started", "step_completed", "step_failed", "step_retried"}:
        return "step"
    if event in {"tool_started", "tool_completed", "tool_failed"}:
        return "tool_call"
    return "lifecycle"


def _status_for_event(*, run: WorkflowRun, event: TaskLogEvent, step_status: str) -> str:
    if event.event in {
        "task_queued",
        "task_started",
        "task_resumed",
        "task_resume_queued",
        "task_pause_requested",
        "task_paused",
        "task_completed",
        "task_failed",
        "task_waiting",
        "task_cancel_requested",
        "task_cancelled",
    }:
        return run.status
    if event.event == "step_started":
        return "running"
    if step_status:
        return step_status
    if event.event in {"confirmation_required", "permission_required"}:
        return "waiting_permission"
    if event.event == "permission_denied":
        return "blocked"
    return ""


def _title_for_event(event: str) -> str:
    titles = {
        "connected": "连接日志",
        "task_queued": "任务已受理",
        "task_started": "任务开始",
        "task_resumed": "任务继续",
        "task_resume_queued": "继续已受理",
        "task_pause_requested": "暂停已请求",
        "task_paused": "任务已暂停",
        "task_completed": "任务完成",
        "task_failed": "任务失败",
        "task_waiting": "任务等待",
        "task_cancel_requested": "取消已请求",
        "task_cancelled": "任务取消",
        "confirmation_required": "需要确认",
        "permission_required": "等待权限",
        "permission_auto_approved": "策略已批准",
        "permission_denied": "权限拒绝",
        "step_started": "步骤开始",
        "step_completed": "步骤完成",
        "step_failed": "步骤失败",
        "step_retried": "步骤重试",
        "artifact_saved": "草稿已保存",
    }
    return titles.get(event, event)


def _artifact_update(
    *,
    artifact: WorkflowArtifact,
    base_sequence: int,
    occurred_at: str,
    step=None,
    tool_calls: list[WorkflowToolCall] | None = None,
) -> WorkflowTaskUpdate:
    dry_run = bool(artifact.metadata.get("dry_run"))
    event = "artifact_planned" if dry_run else "artifact_created"
    message = (
        f"预期产物：{artifact.name}。"
        if dry_run
        else f"产物已生成：{artifact.name}。"
    )
    if artifact.summary:
        message += f" {artifact.summary}"

    return WorkflowTaskUpdate(
        sequence=base_sequence,
        update_type="artifact",
        event=event,
        level="info",
        agent_id=artifact.agent_id,
        step_id=artifact.step_id,
        status="planned" if dry_run else "created",
        title="产物",
        message=message,
        occurred_at=occurred_at,
        payload=_artifact_payload(
            artifact=artifact,
            step=step,
            tool_calls=tool_calls or [],
        ),
    )


def _artifact_payload(
    *,
    artifact: WorkflowArtifact,
    step,
    tool_calls: list[WorkflowToolCall],
) -> dict:
    """产物事件除了 artifact 元数据，也带上同 step 的输出和工具审计。

    这样 Qt 的事件流能直接显示 document.context / verification，不必再额外调用 steps
    或 tool-calls 接口来猜这份产物到底基于哪些前置文档生成。
    """

    payload = {"artifact": artifact.model_dump(mode="json")}
    if step is not None:
        payload["step"] = step.model_dump(mode="json")
    if tool_calls:
        payload["tool_calls"] = [
            tool_call.model_dump(mode="json")
            for tool_call in tool_calls
        ]
    return payload


def _level_for_status(status: str) -> str:
    if status in {"failed", "blocked", "cancelled"}:
        return "error" if status == "failed" else "warning"
    if status in {"paused", "waiting_permission"}:
        return "warning"
    return "info"
