from __future__ import annotations

from app.schemas.workflow import (
    TaskControlAction,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRuntimeStateResponse,
)


TERMINAL_STATUSES: set[WorkflowRunStatus] = {
    "completed",
    "failed",
    "cancelled",
}

_TRANSITIONS: dict[WorkflowRunStatus, set[WorkflowRunStatus]] = {
    "pending": {"running", "paused", "blocked", "cancelled", "failed"},
    "running": {"paused", "waiting_permission", "completed", "blocked", "failed", "cancelled"},
    "paused": {"running", "cancelled", "failed"},
    "waiting_permission": {"paused", "running", "blocked", "failed", "cancelled"},
    "completed": set(),
    "blocked": {"running", "failed", "cancelled"},
    "failed": set(),
    "cancelled": set(),
}


def is_terminal_status(status: WorkflowRunStatus) -> bool:
    """判断任务是否已经进入终态。

    终态任务不能再被取消；是否允许 retry 由控制动作策略决定。把这个判断集中在一处，
    避免后续真实 Runtime 和 API 层对状态理解不一致。
    """

    return status in TERMINAL_STATUSES


def can_transition(from_status: WorkflowRunStatus, to_status: WorkflowRunStatus) -> bool:
    """判断 Runtime 是否允许从一个状态切到另一个状态。"""

    return to_status in _TRANSITIONS.get(from_status, set())


def allowed_next_statuses(status: WorkflowRunStatus) -> list[WorkflowRunStatus]:
    """返回稳定排序的可达状态，方便前端或测试做可预测展示。"""

    order: list[WorkflowRunStatus] = [
        "pending",
        "running",
        "paused",
        "waiting_permission",
        "completed",
        "blocked",
        "failed",
        "cancelled",
    ]
    allowed = _TRANSITIONS.get(status, set())
    return [candidate for candidate in order if candidate in allowed]


def allowed_control_actions(status: WorkflowRunStatus) -> list[TaskControlAction]:
    """根据状态返回当前允许的用户控制动作。

    cancel 只对尚未结束的任务有意义；retry 先允许终态任务重跑。真实 Runtime 接入后，
    可以在这里进一步区分 completed 是否允许“基于结果继续执行”和 failed 是否允许自动重试。
    """

    actions: list[TaskControlAction] = []
    if status in {"pending", "running", "waiting_permission"}:
        actions.append("pause")
        actions.append("cancel")
    if status == "paused":
        actions.extend(["resume", "cancel"])
    if status in TERMINAL_STATUSES or status == "blocked":
        actions.append("retry")
    return actions


def can_cancel(status: WorkflowRunStatus) -> bool:
    return "cancel" in allowed_control_actions(status)


def can_retry(status: WorkflowRunStatus) -> bool:
    return "retry" in allowed_control_actions(status)


def describe_runtime_state(run: WorkflowRun) -> WorkflowRuntimeStateResponse:
    """把 WorkflowRun 转成前端可展示的状态机快照。"""

    actions = allowed_control_actions(run.status)
    next_statuses = allowed_next_statuses(run.status)
    return WorkflowRuntimeStateResponse(
        task_id=run.task_id,
        mode=run.mode,
        status=run.status,
        terminal=is_terminal_status(run.status),
        allowed_actions=actions,
        allowed_next_statuses=next_statuses,
        message=_message_for_state(run.status, actions, next_statuses),
    )


def _message_for_state(
    status: WorkflowRunStatus,
    actions: list[TaskControlAction],
    next_statuses: list[WorkflowRunStatus],
) -> str:
    if status == "completed":
        return "任务已完成，可基于原计划 retry 生成新的执行记录。"
    if status == "failed":
        return "任务已失败，可 retry；失败原因需要从日志、步骤或工具调用记录中查看。"
    if status == "blocked":
        return "任务被阻塞，可在补充权限或上下文后由 Runtime 恢复，也可 retry。"
    if status == "cancelled":
        return "任务已取消，可 retry 重新开始。"
    if status == "waiting_permission":
        return "任务正在等待用户权限确认，确认或拒绝后 Runtime 才能继续转移状态。"
    if status == "paused":
        return "任务已在安全边界暂停；不会重复已完成步骤，可继续或取消。"
    if "cancel" in actions and next_statuses:
        return "任务仍在执行链路中，可取消；Runtime 只能转移到允许的下一状态。"
    return "任务状态已记录。"
