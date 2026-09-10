from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Callable

from app.database.conversation_repository import (
    ConversationWorkingStateConflict,
    get_conversation_working_state,
    save_conversation_working_state,
)
from app.schemas.chat import WorkflowPlan
from app.schemas.conversation import (
    ConversationActiveTask,
    ConversationOpenItem,
    ConversationVerifiedResult,
    ConversationWorkingState,
    ConversationWorkingStateValue,
)
from app.schemas.workflow import WorkflowArtifact, WorkflowRun


_BUDGET_PATTERN = re.compile(r"预算(?:\s*(?:改为|调整为|为|是))?\s*(?:人民币|RMB|￥)?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_FORMAT_PATTERN = re.compile(r"(?:交付|格式|改为|换成)[^。；，,]{0,24}\b(PPTX?|DOCX?|PDF|XLSX?|CSV|Markdown)\b", re.IGNORECASE)
_PAGE_COUNT_PATTERN = re.compile(r"(\d{1,3})\s*(?:页|张)")
_TIME_RANGE_PATTERN = re.compile(r"\b(20\d{2})\s*(?:到|至|[-~])\s*(20\d{2})\b")
_MATERIAL_SCOPE_PATTERN = re.compile(r"(?:只使用|仅使用|使用)\s*材料?\s*([^。；，,]+)")
_SOLUTION_CONFIRM_PATTERN = re.compile(r"确认\s*(?:使用|选择)?\s*方案\s*([A-Za-z0-9]+)", re.IGNORECASE)
_GOAL_REPLACE_PATTERN = re.compile(r"^(?:改成|改为|换成)\s*(.+?)\s*$")
_GOAL_DECLARE_PATTERN = re.compile(r"^(?:请|帮我|帮忙)?\s*(?:制作|生成|分析|开始)\s*(.+?)\s*$")
_OPEN_ITEM_PATTERN = re.compile(r"^(?:请|帮我|帮忙)?\s*((?:生成|制作|交付|导出).+?)\s*$")

_TERMINAL_ITEM_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "blocked": "blocked",
    "cancelled": "cancelled",
}
_MAX_EVENT_IDS = 64


def record_successful_user_message(
    *,
    conversation_id: str,
    project_scope: str,
    message: str,
    task_id: str,
) -> ConversationWorkingState:
    """仅在用户消息和助手回复都已成功归档后，提取可确定的状态候选。"""

    return _apply_persisted_reducer(
        conversation_id=conversation_id,
        project_scope=project_scope,
        reducer=lambda state: reduce_user_message(
            state,
            message=message,
            source_id=task_id,
        ),
    )


def synchronize_workflow_run(
    *,
    run: WorkflowRun,
    plan: WorkflowPlan | None,
    artifacts: list[WorkflowArtifact] | None = None,
) -> ConversationWorkingState | None:
    """把持久化成功的 Workflow checkpoint 投影进所属会话的恢复快照。

    没有关联会话的独立工具任务不进入会话记忆。任务仓储在事务提交后调用本函数，因此不会把
    半写入的 run 伪装成进度，也不会让会话状态反向影响 Runtime 的主状态机。
    """

    if plan is None:
        return None
    conversation_id = str(getattr(plan, "conversation_id", "") or "")
    project_scope = str(getattr(plan, "project_scope", "") or "")
    if not conversation_id or not project_scope:
        return None
    return _apply_persisted_reducer(
        conversation_id=conversation_id,
        project_scope=project_scope,
        reducer=lambda state: project_workflow_run(
            state,
            run=run,
            plan=plan,
            artifacts=artifacts or [],
        ),
    )


def reduce_user_message(
    state: ConversationWorkingState,
    *,
    message: str,
    source_id: str,
) -> ConversationWorkingState:
    """用白名单规则把一条已脱敏用户消息归约为工作状态。

    规则刻意窄：不确定的改动只加入待确认列表；没有任何助手文本输入入口，因此助手的
    自述不能关闭待办或登记交付。真实任务状态只能通过 ``project_workflow_event`` 进入。
    """

    text = " ".join(message.strip().split())
    if not text:
        return state
    candidate = state.model_copy(deep=True)
    now = _utc_now()

    budget = _extract_budget(text)
    if budget is not None:
        _set_value(candidate.constraints, "budget", budget, "user_message", source_id, now)
        _remove_pending(candidate, "budget")
    elif "预算" in text and re.search(r"(?:改|调|变|优化)", text):
        _add_pending(candidate, "budget")

    delivery_format = _extract_delivery_format(text)
    if delivery_format:
        _set_value(candidate.constraints, "delivery_format", delivery_format, "user_message", source_id, now)
        _remove_pending(candidate, "delivery_format")

    material_scope = _extract_material_scope(text)
    if material_scope:
        _set_value(candidate.constraints, "material_scope", material_scope, "user_message", source_id, now)
        _remove_pending(candidate, "material_scope")

    page_count = _extract_page_count(text)
    if page_count is not None:
        _set_value(candidate.constraints, "page_count", page_count, "user_message", source_id, now)
        _remove_pending(candidate, "page_count")

    time_range = _extract_time_range(text)
    if time_range:
        _set_value(candidate.constraints, "time_range", time_range, "user_message", source_id, now)
        _remove_pending(candidate, "time_range")

    if "中文" in text:
        _set_value(candidate.constraints, "language", "中文", "user_message", source_id, now)
    elif "英文" in text:
        _set_value(candidate.constraints, "language", "英文", "user_message", source_id, now)
    if re.search(r"(?:必须|需要|包含|含有).{0,12}(?:一张)?表格", text):
        _set_value(candidate.constraints, "table_required", True, "user_message", source_id, now)

    decision = _extract_solution_decision(text)
    if decision:
        _set_value(candidate.decisions, "solution", decision, "user_message", source_id, now)
        _remove_pending(candidate, "solution")

    goal = _extract_goal(
        text,
        has_constraint_signal=bool(
            budget is not None
            or delivery_format
            or material_scope
            or page_count is not None
            or time_range
        ),
    )
    if goal:
        _replace_current_goal(candidate, goal, source_id=source_id, now=now)

    open_item = _extract_open_item(text)
    if open_item:
        _ensure_user_open_item(candidate, title=open_item, source_id=source_id, now=now)

    return _finalize_reduction(state, candidate, now=now)


def project_workflow_run(
    state: ConversationWorkingState,
    *,
    run: WorkflowRun,
    plan: WorkflowPlan | None,
    artifacts: list[WorkflowArtifact],
) -> ConversationWorkingState:
    """从可信 WorkflowRun 构造会话任务投影。"""

    step_index, current_step = _select_current_step(run)
    # dry-run 的步骤输出可能包含用于 UI 演示的虚拟 artifact；它只表示计划预演完成，绝不
    # 能替代 Runtime 回读后的真实交付。最新验证结果因此严格限定为真实运行态产物。
    artifact_ids = _artifact_ids_from_run(run, artifacts) if run.mode == "runtime" else []
    event_id = _workflow_event_id(
        task_id=run.task_id,
        status=run.status,
        current_step=current_step,
        step_index=step_index,
        artifact_ids=artifact_ids,
        plan_id=str(getattr(plan, "plan_id", "") or ""),
        planned_next_action=(
            str(getattr(plan, "next_action", "") or "")
            if run.status in {"pending", "queued"}
            else ""
        ),
    )
    return project_workflow_event(
        state,
        task_id=run.task_id,
        status=run.status,
        current_step=current_step,
        step_index=step_index,
        plan_id=str(getattr(plan, "plan_id", "") or ""),
        task_title=str(getattr(plan, "user_goal", "") or run.summary or "任务"),
        result_summary=run.summary,
        artifact_ids=artifact_ids,
        planned_next_action=(
            str(getattr(plan, "next_action", "") or "")
            if run.status in {"pending", "queued"}
            else ""
        ),
        event_id=event_id,
    )


def project_workflow_event(
    state: ConversationWorkingState,
    *,
    task_id: str,
    status: str,
    current_step: str = "",
    step_index: int | None = None,
    plan_id: str = "",
    task_title: str = "任务",
    result_summary: str = "",
    artifact_ids: list[str] | None = None,
    resume_checkpoint: str = "",
    planned_next_action: str = "",
    event_id: str = "",
) -> ConversationWorkingState:
    """投影单个 Runtime 生命周期事件；供 Runtime 接入和离线验收共用。"""

    normalized_status = (status or "queued").strip().lower()[:48]
    normalized_task_id = task_id.strip()[:160]
    if not normalized_task_id:
        return state
    normalized_event_id = event_id.strip()[:180] or _workflow_event_id(
        task_id=normalized_task_id,
        status=normalized_status,
        current_step=current_step,
        step_index=step_index,
        artifact_ids=artifact_ids or [],
        plan_id=plan_id,
        planned_next_action=planned_next_action,
    )
    if normalized_event_id in state.applied_event_ids:
        return state

    candidate = state.model_copy(deep=True)
    now = _utc_now()
    next_action = planned_next_action[:180] or _next_action_for_status(
        normalized_status,
        current_step=current_step,
    )
    active = ConversationActiveTask(
        task_id=normalized_task_id,
        plan_id=plan_id[:160],
        status=normalized_status,
        current_step=current_step[:180],
        step_index=step_index,
        next_action=next_action,
        resume_checkpoint=resume_checkpoint[:160],
        updated_at=now,
    )
    if candidate.active_task != active:
        candidate.active_task = active

    _upsert_runtime_open_item(
        candidate,
        task_id=normalized_task_id,
        title=task_title or "任务",
        status=normalized_status,
        now=now,
    )

    normalized_artifacts = _dedupe_texts(artifact_ids or [], maximum=16)
    if normalized_status == "completed" and normalized_artifacts:
        verified = ConversationVerifiedResult(
            task_id=normalized_task_id,
            artifact_id=normalized_artifacts[0],
            artifact_ids=normalized_artifacts,
            summary=result_summary[:600],
            verified_at=now,
        )
        if candidate.latest_verified_result != verified:
            candidate.latest_verified_result = verified

    candidate.applied_event_ids = [*candidate.applied_event_ids[-(_MAX_EVENT_IDS - 1) :], normalized_event_id]
    return _finalize_reduction(state, candidate, now=now)


def build_working_state_prompt_summary(state: ConversationWorkingState | None) -> str:
    """将同一快照压成小型 Prompt 片段，不引入任务日志或交付正文。"""

    if state is None or state.revision == 0:
        return ""
    lines: list[str] = [f"结构化工作状态（版本 {state.revision}）："]
    if state.current_goal is not None:
        lines.append("当前目标：" + _display_value(state.current_goal.value))
    if state.constraints:
        constraints = "；".join(
            f"{key}={_display_value(value.value)}"
            for key, value in sorted(state.constraints.items())[:8]
        )
        lines.append("有效约束：" + constraints)
    if state.decisions:
        decisions = "；".join(
            f"{key}={_display_value(value.value)}"
            for key, value in sorted(state.decisions.items())[:6]
        )
        lines.append("已确认决策：" + decisions)
    if state.pending_confirmation:
        lines.append("待确认字段：" + "、".join(state.pending_confirmation[:8]))
    if state.active_task is not None:
        task = state.active_task
        task_text = f"当前任务：{task.task_id}（{task.status}）"
        if task.plan_id:
            task_text += f"，计划：{task.plan_id}"
        if task.current_step:
            task_text += f"，步骤：{task.current_step}"
        if task.next_action:
            task_text += f"，下一步：{task.next_action}"
        lines.append(task_text)
    if state.latest_verified_result is not None:
        result = state.latest_verified_result
        artifact_ids = "、".join(result.artifact_ids[:16])
        result_text = result.artifact_id or artifact_ids or result.task_id
        lines.append(f"已验证交付：{result_text}")
    active_open_items = [
        item
        for item in state.open_items
        if item.status in {"open", "pending_confirmation", "blocked", "failed"}
    ]
    if active_open_items:
        lines.append(
            "未完成事项："
            + "；".join(
                f"{item.item_id}={item.title}（{item.status}）"
                for item in active_open_items
            )
        )
    return "\n".join(lines)


def _apply_persisted_reducer(
    *,
    conversation_id: str,
    project_scope: str,
    reducer: Callable[[ConversationWorkingState], ConversationWorkingState],
) -> ConversationWorkingState:
    for _ in range(3):
        before = get_conversation_working_state(
            conversation_id=conversation_id,
            project_scope=project_scope,
        )
        after = reducer(before)
        if after.revision == before.revision:
            return before
        try:
            return save_conversation_working_state(
                state=after,
                expected_revision=before.revision,
            )
        except ConversationWorkingStateConflict:
            continue
    raise ConversationWorkingStateConflict("会话工作状态连续竞争更新，未能在三次内完成合并。")


def _extract_budget(text: str) -> str | None:
    match = _BUDGET_PATTERN.search(text)
    return match.group(1) if match else None


def _extract_delivery_format(text: str) -> str:
    match = _FORMAT_PATTERN.search(text)
    return match.group(1).upper() if match else ""


def _extract_material_scope(text: str) -> list[str]:
    match = _MATERIAL_SCOPE_PATTERN.search(text)
    if not match:
        return []
    values = re.findall(r"\b[A-Za-z][A-Za-z0-9_-]*\b", match.group(1))
    return _dedupe_texts(values, maximum=8)


def _extract_page_count(text: str) -> int | None:
    if not re.search(r"(?:页数|页|生成|制作|PPT|演示稿)", text, re.IGNORECASE):
        return None
    match = _PAGE_COUNT_PATTERN.search(text)
    return int(match.group(1)) if match else None


def _extract_time_range(text: str) -> str:
    match = _TIME_RANGE_PATTERN.search(text)
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def _extract_solution_decision(text: str) -> str:
    match = _SOLUTION_CONFIRM_PATTERN.search(text)
    return match.group(1).upper() if match else ""


def _extract_goal(text: str, *, has_constraint_signal: bool) -> str:
    if has_constraint_signal or "方案" in text or "预算" in text:
        return ""
    replace = _GOAL_REPLACE_PATTERN.match(text)
    if replace:
        return replace.group(1)[:240]
    declare = _GOAL_DECLARE_PATTERN.match(text)
    if declare:
        prefix = text[: text.find(declare.group(1))]
        return (prefix + declare.group(1)).strip()[:240]
    return ""


def _extract_open_item(text: str) -> str:
    if text.startswith(("改成", "改为", "换成")):
        return ""
    match = _OPEN_ITEM_PATTERN.match(text)
    return match.group(1)[:240] if match else ""


def _replace_current_goal(candidate: ConversationWorkingState, value: str, *, source_id: str, now: str) -> None:
    if candidate.current_goal is not None and candidate.current_goal.value == value:
        return
    candidate.current_goal = ConversationWorkingStateValue(
        value=value,
        source="user_message",
        source_id=source_id[:160],
        updated_at=now,
    )


def _set_value(
    mapping: dict[str, ConversationWorkingStateValue],
    key: str,
    value: object,
    source: str,
    source_id: str,
    now: str,
) -> None:
    existing = mapping.get(key)
    if existing is not None and existing.value == value:
        return
    mapping[key] = ConversationWorkingStateValue(
        value=value,
        source=source,  # type: ignore[arg-type]
        source_id=source_id[:160],
        updated_at=now,
    )


def _ensure_user_open_item(candidate: ConversationWorkingState, *, title: str, source_id: str, now: str) -> None:
    item_id = f"user:{title}"[:180]
    if any(item.item_id == item_id for item in candidate.open_items):
        return
    candidate.open_items.append(
        ConversationOpenItem(
            item_id=item_id,
            title=title,
            status="open",
            source="user_message",
            source_id=source_id[:160],
            updated_at=now,
        )
    )


def _upsert_runtime_open_item(
    candidate: ConversationWorkingState,
    *,
    task_id: str,
    title: str,
    status: str,
    now: str,
) -> None:
    item_id = f"task:{task_id}"
    item_status = _TERMINAL_ITEM_STATUS.get(status, "open")
    replacement = ConversationOpenItem(
        item_id=item_id,
        title=title[:240],
        status=item_status,  # type: ignore[arg-type]
        task_id=task_id,
        source="workflow_run",
        source_id=task_id,
        updated_at=now,
    )
    for index, existing in enumerate(candidate.open_items):
        if existing.item_id == item_id:
            if existing != replacement:
                candidate.open_items[index] = replacement
            return
    candidate.open_items.append(replacement)


def _add_pending(candidate: ConversationWorkingState, key: str) -> None:
    if key not in candidate.pending_confirmation:
        candidate.pending_confirmation.append(key)


def _remove_pending(candidate: ConversationWorkingState, key: str) -> None:
    if key in candidate.pending_confirmation:
        candidate.pending_confirmation.remove(key)


def _select_current_step(run: WorkflowRun) -> tuple[int | None, str]:
    active_statuses = {"running", "waiting_permission", "pending", "blocked", "failed"}
    for index, step in enumerate(run.steps, start=1):
        if step.status in active_statuses:
            return index, step.action
    if run.steps:
        return len(run.steps), run.steps[-1].action
    return None, ""


def _artifact_ids_from_run(run: WorkflowRun, artifacts: list[WorkflowArtifact]) -> list[str]:
    values = [artifact.artifact_id for artifact in artifacts if artifact.task_id == run.task_id]
    for step in run.steps:
        for key in ("artifact_id", "artifact_ids"):
            raw = step.output.get(key)
            if isinstance(raw, str):
                values.append(raw)
            elif isinstance(raw, list):
                values.extend(item for item in raw if isinstance(item, str))
    return _dedupe_texts(values, maximum=16)


def _workflow_event_id(
    *,
    task_id: str,
    status: str,
    current_step: str,
    step_index: int | None,
    artifact_ids: list[str],
    plan_id: str = "",
    planned_next_action: str = "",
) -> str:
    values = ",".join(_dedupe_texts(artifact_ids, maximum=16))
    return (
        f"workflow:{task_id}:{status}:{step_index or 0}:{current_step}:{values}:"
        f"{plan_id}:{planned_next_action}"
    )[:180]


def _next_action_for_status(status: str, *, current_step: str) -> str:
    if status == "completed":
        return "review_result"
    if status == "failed":
        return "retry_or_change_model"
    if status == "paused":
        return "resume"
    if status == "waiting_permission":
        return "await_confirmation"
    if status == "blocked":
        return "resolve_blocker"
    if status == "cancelled":
        return "create_new_task"
    return current_step or "continue_execution"


def _finalize_reduction(
    before: ConversationWorkingState,
    candidate: ConversationWorkingState,
    *,
    now: str,
) -> ConversationWorkingState:
    before_payload = before.model_dump(exclude={"revision", "updated_at"})
    candidate_payload = candidate.model_dump(exclude={"revision", "updated_at"})
    if candidate_payload == before_payload:
        return before
    candidate.revision = before.revision + 1
    candidate.updated_at = now
    return candidate


def _dedupe_texts(values: list[str], *, maximum: int) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized[:160])
        if len(result) >= maximum:
            break
    return result


def _display_value(value: object) -> str:
    if isinstance(value, list):
        return "、".join(str(item)[:80] for item in value[:8])
    return str(value)[:180]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
