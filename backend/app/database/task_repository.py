from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.database.sqlite import get_connection
from app.schemas.chat import WorkflowPlan
from app.schemas.events import TaskLogEvent
from app.schemas.plan_revisions import WorkflowPlanVersionSummary
from app.schemas.workflow import (
    RiskLevel,
    RuntimePermissionDecision,
    RuntimePermissionDecisionInput,
    RuntimePermissionDecisionRecord,
    RuntimePermissionItem,
    RuntimePermissionRequest,
    RuntimeExecutionControlState,
    WorkflowArtifact,
    WorkflowStepRun,
    WorkflowToolCall,
    WorkflowRun,
    WorkflowRunListItem,
    WorkflowRunMode,
    WorkflowRunStatus,
)


@dataclass(frozen=True)
class WorkflowTaskProgressSnapshot:
    """供父任务轮询关联子任务的轻量进度快照。

    这不是 ``WorkflowRun`` 的替代品：它刻意不读取 ``run_json`` 或 ``step_json``，因此不会把
    K4 数百个章节的模型小结、材料定位或中间输出复制到父任务的状态刷新路径。调用方只得到
    已索引的任务状态、客户摘要，以及按 action/status 聚合后的确定性计数。
    """

    task_id: str
    status: str
    action_status_counts: dict[tuple[str, str], int]
    # `summary` 目前只存于完整 run_json。父任务轮询不应为了展示一句文案解析数百个 Map
    # checkpoint，因此这里保留兼容字段但始终为空，UI 使用稳定的状态提示代替。
    summary: str = ""


def save_workflow_runtime_checkpoint(
    *,
    run: WorkflowRun,
    plan: WorkflowPlan | None,
    permission_requests: list[RuntimePermissionRequest],
    artifacts: list[WorkflowArtifact],
    tool_calls: list[WorkflowToolCall],
) -> None:
    """保存 Runtime 的可恢复检查点，但绝不重写既有事件时间线。

    ``save_workflow_run`` 适合 dry-run 的整份快照，且会替换 events。后台 Runtime 需要在每个
    安全边界更新 run/step/tool/artifact，同时允许 API 线程追加暂停、审批等事件，因此这里把
    两类写入明确分开。所有更新仍是一次短 SQLite 事务，避免长模型调用占用数据库锁。
    """

    _save_workflow_snapshot(
        run=run,
        plan=plan,
        permission_requests=permission_requests,
        replace_permission_requests=False,
        artifacts=artifacts,
        tool_calls=tool_calls,
        replace_events=None,
    )


def save_workflow_run(
    *,
    run: WorkflowRun,
    events: list[TaskLogEvent],
    plan: WorkflowPlan | None,
    permission_requests: list[RuntimePermissionRequest] | None = None,
    replace_permission_requests: bool = True,
    artifacts: list[WorkflowArtifact] | None = None,
    tool_calls: list[WorkflowToolCall] | None = None,
) -> None:
    """保存 dry-run 任务状态、计划和日志。

    当前是同步短事务。dry-run 写入量很小，不会阻塞长时间计算；等真实执行器接入后，
    大量日志写入会改成后台队列或批量刷盘。
    """

    _save_workflow_snapshot(
        run=run,
        plan=plan,
        permission_requests=permission_requests,
        replace_permission_requests=replace_permission_requests,
        artifacts=artifacts,
        tool_calls=tool_calls,
        replace_events=events,
    )


def _save_workflow_snapshot(
    *,
    run: WorkflowRun,
    plan: WorkflowPlan | None,
    permission_requests: list[RuntimePermissionRequest] | None,
    replace_permission_requests: bool,
    artifacts: list[WorkflowArtifact] | None,
    tool_calls: list[WorkflowToolCall] | None,
    replace_events: list[TaskLogEvent] | None,
) -> None:
    """写入 Run 附属快照；``replace_events=None`` 时保留 append-only 时间线。"""

    run_json = run.model_dump_json()
    plan_json = plan.model_dump_json() if plan is not None else None
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO workflow_runs (
                task_id, mode, status, max_risk_level, requires_confirmation,
                run_json, plan_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(task_id) DO UPDATE SET
                mode = excluded.mode,
                status = excluded.status,
                max_risk_level = excluded.max_risk_level,
                requires_confirmation = excluded.requires_confirmation,
                run_json = excluded.run_json,
                plan_json = excluded.plan_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                run.task_id,
                run.mode,
                run.status,
                run.max_risk_level,
                1 if run.requires_confirmation else 0,
                run_json,
                plan_json,
            ),
        )
        connection.execute("DELETE FROM workflow_steps WHERE task_id = ?", (run.task_id,))
        connection.executemany(
            """
            INSERT INTO workflow_steps (
                task_id, step_index, step_id, agent_id, action, status,
                risk_level, requires_confirmation, step_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                (
                    run.task_id,
                    index,
                    step.step_id,
                    step.agent,
                    step.action,
                    step.status,
                    step.risk_level,
                    1 if step.requires_confirmation else 0,
                    step.model_dump_json(),
                )
                for index, step in enumerate(run.steps, start=1)
            ],
        )
        if plan is not None:
            # 每次保存当前计划时顺带留下只增不改的版本快照。遇到同一 task/version 的重复
            # 落盘（例如 Runtime 更新运行态）保持首份原样，不让历史版本被后续状态覆盖。
            connection.execute(
                """
                INSERT OR IGNORE INTO workflow_plan_versions (
                    task_id, plan_version, plan_id, parent_plan_id, user_goal,
                    change_summary, plan_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.task_id,
                    plan.plan_version,
                    plan.plan_id,
                    plan.parent_plan_id or "",
                    plan.user_goal,
                    plan.change_summary,
                    plan.model_dump_json(),
                ),
            )
        if artifacts is not None:
            # artifact 记录是 Runtime 的可观测产物目录；dry-run 只保存虚拟 URI，不写真实文件。
            connection.execute("DELETE FROM workflow_artifacts WHERE task_id = ?", (run.task_id,))
            connection.executemany(
                """
                INSERT INTO workflow_artifacts (
                    task_id, artifact_id, step_id, agent_id, kind, name,
                    summary, uri, mime_type, artifact_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    (
                        artifact.task_id,
                        artifact.artifact_id,
                        artifact.step_id,
                        artifact.agent_id,
                        artifact.kind,
                        artifact.name,
                        artifact.summary,
                        artifact.uri,
                        artifact.mime_type,
                        artifact.model_dump_json(),
                    )
                    for artifact in artifacts
                ],
            )
        if tool_calls is not None:
            # tool_call 记录只描述 Runtime 看到的工具边界，真实执行结果以后由执行器补全。
            connection.execute("DELETE FROM workflow_tool_calls WHERE task_id = ?", (run.task_id,))
            connection.executemany(
                """
                INSERT INTO workflow_tool_calls (
                    task_id, call_id, step_id, agent_id, tool_name, status,
                    risk_level, permission_required, tool_call_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    (
                        tool_call.task_id,
                        tool_call.call_id,
                        tool_call.step_id,
                        tool_call.agent_id,
                        tool_call.tool_name,
                        tool_call.status,
                        tool_call.risk_level,
                        1 if tool_call.permission_required else 0,
                        tool_call.model_dump_json(),
                    )
                    for tool_call in tool_calls
                ],
            )
        if replace_events is not None:
            connection.execute("DELETE FROM workflow_events WHERE task_id = ?", (run.task_id,))
            connection.executemany(
                """
                INSERT INTO workflow_events (
                    task_id, sequence, event, agent_id, step_id, level, event_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.task_id,
                        event.sequence,
                        event.event,
                        event.agent_id,
                        event.step_id,
                        event.level,
                        event.model_dump_json(),
                    )
                    for event in replace_events
                ],
            )
        if permission_requests is not None:
            if replace_permission_requests:
                # dry-run 重新生成或 retry 时，用当前计划的权限请求替换旧记录，避免前端看到过期步骤。
                connection.execute(
                    "DELETE FROM runtime_permission_requests WHERE task_id = ?",
                    (run.task_id,),
                )
                connection.executemany(
                    """
                    INSERT INTO runtime_permission_requests (
                        request_id, task_id, step_id, agent_id, permissions_json,
                        risk_level, summary, details_json, request_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    [
                        _permission_request_row(request)
                        for request in permission_requests
                    ],
                )
            else:
                # 真实 Runtime 可能多次保存同一个任务；这里更新请求描述但保留用户决策，
                # 避免 approved/denied 被新的运行快照重置成 pending。
                connection.executemany(
                    """
                    INSERT INTO runtime_permission_requests (
                        request_id, task_id, step_id, agent_id, permissions_json,
                        risk_level, summary, details_json, request_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(request_id) DO UPDATE SET
                        step_id = excluded.step_id,
                        agent_id = excluded.agent_id,
                        permissions_json = excluded.permissions_json,
                        risk_level = excluded.risk_level,
                        summary = excluded.summary,
                        details_json = excluded.details_json,
                        request_json = excluded.request_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    [
                        _permission_request_row(request)
                        for request in permission_requests
                    ],
                )


def load_workflow_run(task_id: str) -> WorkflowRun | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT run_json FROM workflow_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    if row is None:
        return None
    return WorkflowRun.model_validate_json(row["run_json"])


def load_workflow_task_progress_snapshot(task_id: str) -> WorkflowTaskProgressSnapshot | None:
    """读取关联任务的状态与聚合进度，不反序列化完整运行快照。

    Commander 的历史页会周期性查看少量已委派任务。K4 的 ``run_json`` 可能包含大量 Map
    checkpoint；这里直接使用任务与步骤表中的索引列做聚合，避免每次刷新都解析模型输出或
    复制客户资料到父任务的复盘数据中。
    """

    with get_connection() as connection:
        task_row = connection.execute(
            """
            SELECT task_id, status
            FROM workflow_runs
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if task_row is None:
            return None
        step_rows = connection.execute(
            """
            SELECT action, status, COUNT(*) AS item_count
            FROM workflow_steps
            WHERE task_id = ?
            GROUP BY action, status
            """,
            (task_id,),
        ).fetchall()

    action_status_counts = {
        (str(row["action"]), str(row["status"])): int(row["item_count"])
        for row in step_rows
    }
    return WorkflowTaskProgressSnapshot(
        task_id=str(task_row["task_id"]),
        status=str(task_row["status"]),
        action_status_counts=action_status_counts,
    )


def list_interrupted_runtime_task_ids() -> list[str]:
    """列出服务重启时可能失去执行线程的 Runtime 任务。

    只查询尚未进入终态的三种瞬时状态。``paused`` 是客户已经明确暂停的检查点，``blocked``
    则已经拥有可解释的停止原因，因此两者不应被启动恢复扫描重复改写。
    """

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT task_id
            FROM workflow_runs
            WHERE mode = 'runtime' AND status IN ('pending', 'running', 'waiting_permission')
            ORDER BY updated_at ASC, created_at ASC, task_id ASC
            """
        ).fetchall()
    return [str(row["task_id"]) for row in rows]


def load_workflow_step_runs(task_id: str) -> list[WorkflowStepRun] | None:
    """读取任务的 step 级结果。

    新表用于真实 Runtime 逐步落盘和失败恢复；对旧数据保留 run_json fallback，避免已有
    dry-run 记录因为没有 workflow_steps 行而无法查看详情。
    """

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT step_json
            FROM workflow_steps
            WHERE task_id = ?
            ORDER BY step_index ASC
            """,
            (task_id,),
        ).fetchall()

    if rows:
        return [WorkflowStepRun.model_validate_json(row["step_json"]) for row in rows]

    run = load_workflow_run(task_id)
    if run is None:
        return None
    return list(run.steps)


def list_workflow_artifacts(task_id: str) -> list[WorkflowArtifact]:
    """读取任务产物目录。

    artifact_json 保留完整协议，单独列出的 kind/name/step_id 主要给后续筛选和索引用；
    目前按 task_id 精确查询，历史任务增长时也不会解析无关任务。
    """

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT artifact_json
            FROM workflow_artifacts
            WHERE task_id = ?
            ORDER BY created_at ASC, artifact_id ASC
            """,
            (task_id,),
        ).fetchall()

    return [WorkflowArtifact.model_validate_json(row["artifact_json"]) for row in rows]


def append_workflow_artifact(
    *,
    artifact: WorkflowArtifact,
    event_name: str,
    message: str,
) -> TaskLogEvent:
    """向已经结束的任务追加一个真实产物和对应审计事件。

    文档草稿的保存发生在分析任务完成之后，不能再次调用 ``save_workflow_run``，否则会把
    已有步骤、工具调用和历史产物整体替换。这里使用一个短 SQLite 写事务：先确认任务存在，
    再写产物、分配下一个稳定事件序号并更新时间戳，保证历史页能把“保存”与原任务串起来。
    """

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT 1 FROM workflow_runs WHERE task_id = ?",
            (artifact.task_id,),
        ).fetchone()
        if existing is None:
            raise KeyError(f"Task '{artifact.task_id}' was not found.")

        connection.execute(
            """
            INSERT INTO workflow_artifacts (
                task_id, artifact_id, step_id, agent_id, kind, name,
                summary, uri, mime_type, artifact_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                artifact.task_id,
                artifact.artifact_id,
                artifact.step_id,
                artifact.agent_id,
                artifact.kind,
                artifact.name,
                artifact.summary,
                artifact.uri,
                artifact.mime_type,
                artifact.model_dump_json(),
            ),
        )
        next_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM workflow_events WHERE task_id = ?",
                (artifact.task_id,),
            ).fetchone()["next_sequence"]
        )
        event = TaskLogEvent(
            task_id=artifact.task_id,
            sequence=next_sequence,
            event=event_name,
            agent_id=artifact.agent_id,
            step_id=artifact.step_id,
            message=message,
        )
        connection.execute(
            """
            INSERT INTO workflow_events (
                task_id, sequence, event, agent_id, step_id, level, event_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.task_id,
                event.sequence,
                event.event,
                event.agent_id,
                event.step_id,
                event.level,
                event.model_dump_json(),
            ),
        )
        connection.execute(
            "UPDATE workflow_runs SET updated_at = CURRENT_TIMESTAMP WHERE task_id = ?",
            (artifact.task_id,),
        )
    return event


def append_workflow_event(
    *,
    task_id: str,
    event_name: str,
    agent_id: str,
    message: str,
    step_id: str | None = None,
    level: str = "info",
) -> TaskLogEvent:
    """向已完成计划任务追加一条真实执行阶段。

    PPT 的确认导出发生在创作计划落库之后；它仍属于同一个客户任务，不能重新保存完整
    WorkflowRun，否则会覆盖原来的计划、步骤和事件。这里以短事务分配下一个事件序号，
    让任务历史和实时状态看到同一条可审计事实。
    """

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT 1 FROM workflow_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing is None:
            raise KeyError(f"Task '{task_id}' was not found.")
        next_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM workflow_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()["next_sequence"]
        )
        task_event = TaskLogEvent(
            task_id=task_id,
            sequence=next_sequence,
            event=event_name,
            agent_id=agent_id,
            step_id=step_id,
            level=level,  # type: ignore[arg-type]
            message=message,
        )
        connection.execute(
            """
            INSERT INTO workflow_events (
                task_id, sequence, event, agent_id, step_id, level, event_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_event.task_id,
                task_event.sequence,
                task_event.event,
                task_event.agent_id,
                task_event.step_id,
                task_event.level,
                task_event.model_dump_json(),
            ),
        )
        connection.execute(
            "UPDATE workflow_runs SET updated_at = CURRENT_TIMESTAMP WHERE task_id = ?",
            (task_id,),
        )
    return task_event


def get_runtime_execution_control(task_id: str) -> RuntimeExecutionControlState | None:
    """读取后台 Runtime 的协作式控制状态。

    没有控制行的既有任务仍等价于“未暂停、未取消”，这样旧任务和新任务可平滑共存；任务本体
    不存在时才返回 ``None``，供 API 区分 404。
    """

    with get_connection() as connection:
        run_exists = connection.execute(
            "SELECT 1 FROM workflow_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if run_exists is None:
            return None
        row = connection.execute(
            """
            SELECT pause_requested, cancel_requested, updated_at
            FROM runtime_execution_controls
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()

    if row is None:
        return RuntimeExecutionControlState(task_id=task_id)
    return RuntimeExecutionControlState(
        task_id=task_id,
        pause_requested=bool(row["pause_requested"]),
        cancel_requested=bool(row["cancel_requested"]),
        updated_at=row["updated_at"],
    )


def set_runtime_execution_control(
    *,
    task_id: str,
    pause_requested: bool | None = None,
    cancel_requested: bool | None = None,
) -> RuntimeExecutionControlState | None:
    """原子更新控制信号，保留调用方没有指定的另一项意图。"""

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run_exists = connection.execute(
            "SELECT 1 FROM workflow_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if run_exists is None:
            return None

        row = connection.execute(
            """
            SELECT pause_requested, cancel_requested
            FROM runtime_execution_controls
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        current_pause = bool(row["pause_requested"]) if row is not None else False
        current_cancel = bool(row["cancel_requested"]) if row is not None else False
        next_pause = current_pause if pause_requested is None else pause_requested
        next_cancel = current_cancel if cancel_requested is None else cancel_requested
        connection.execute(
            """
            INSERT INTO runtime_execution_controls (
                task_id, pause_requested, cancel_requested, updated_at
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(task_id) DO UPDATE SET
                pause_requested = excluded.pause_requested,
                cancel_requested = excluded.cancel_requested,
                updated_at = CURRENT_TIMESTAMP
            """,
            (task_id, 1 if next_pause else 0, 1 if next_cancel else 0),
        )
        updated = connection.execute(
            """
            SELECT pause_requested, cancel_requested, updated_at
            FROM runtime_execution_controls
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()

    return RuntimeExecutionControlState(
        task_id=task_id,
        pause_requested=bool(updated["pause_requested"]),
        cancel_requested=bool(updated["cancel_requested"]),
        updated_at=updated["updated_at"],
    )


def list_workflow_tool_calls(task_id: str) -> list[WorkflowToolCall]:
    """读取任务工具调用审计记录。

    这里返回的是 Runtime 级“会/已调用什么工具”的事实记录，不让前端从 step.output 里猜。
    后续真实执行器可以逐条更新 status/result/error，而无需改变前端读取入口。
    """

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT tool_call_json
            FROM workflow_tool_calls
            WHERE task_id = ?
            ORDER BY created_at ASC, call_id ASC
            """,
            (task_id,),
        ).fetchall()

    return [WorkflowToolCall.model_validate_json(row["tool_call_json"]) for row in rows]


def load_workflow_plan(task_id: str) -> WorkflowPlan | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT plan_json FROM workflow_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    if row is None or not row["plan_json"]:
        return None
    return WorkflowPlan.model_validate_json(row["plan_json"])


def ensure_workflow_plan_version(*, task_id: str, plan: WorkflowPlan) -> None:
    """为历史任务补齐当前计划的首个版本快照。

    C3 上线前已经保存的任务没有 ``workflow_plan_versions`` 行。计划修订前显式调用本函数，
    只补一条不可变快照，不改写 ``workflow_runs`` 的当前计划或任何运行状态。
    """

    with get_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO workflow_plan_versions (
                task_id, plan_version, plan_id, parent_plan_id, user_goal,
                change_summary, plan_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                plan.plan_version,
                plan.plan_id,
                plan.parent_plan_id or "",
                plan.user_goal,
                plan.change_summary,
                plan.model_dump_json(),
            ),
        )


def list_workflow_plan_versions(task_id: str) -> list[WorkflowPlanVersionSummary] | None:
    """按版本号读取一个任务的计划历史。

    不从 ``workflow_runs`` 扫描其它任务；计划数通常很少，按 task_id 索引读取即可满足详情页。
    对旧任务保留当前 plan 的只读 fallback，让升级前历史仍然可以被正常查看。
    """

    current_plan = load_workflow_plan(task_id)
    if current_plan is None:
        return None
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT plan_id, plan_version, parent_plan_id, user_goal, change_summary, created_at
            FROM workflow_plan_versions
            WHERE task_id = ?
            ORDER BY plan_version DESC
            """,
            (task_id,),
        ).fetchall()

    if not rows:
        return [
            WorkflowPlanVersionSummary(
                task_id=task_id,
                plan_id=current_plan.plan_id,
                plan_version=current_plan.plan_version,
                parent_plan_id=current_plan.parent_plan_id,
                user_goal=current_plan.user_goal,
                change_summary=current_plan.change_summary,
                created_at="",
                is_current=True,
            )
        ]
    return [
        WorkflowPlanVersionSummary(
            task_id=task_id,
            plan_id=str(row["plan_id"]),
            plan_version=int(row["plan_version"]),
            parent_plan_id=str(row["parent_plan_id"] or "") or None,
            user_goal=str(row["user_goal"]),
            change_summary=str(row["change_summary"]),
            created_at=str(row["created_at"]),
            is_current=str(row["plan_id"]) == current_plan.plan_id,
        )
        for row in rows
    ]


def load_workflow_plan_version(task_id: str, plan_version: int) -> tuple[WorkflowPlanVersionSummary, WorkflowPlan] | None:
    """读取一个不可变计划快照；不会把它设为当前执行计划。"""

    current_plan = load_workflow_plan(task_id)
    if current_plan is None:
        return None
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT plan_id, plan_version, parent_plan_id, user_goal, change_summary, plan_json, created_at
            FROM workflow_plan_versions
            WHERE task_id = ? AND plan_version = ?
            """,
            (task_id, plan_version),
        ).fetchone()
    if row is None:
        if plan_version != current_plan.plan_version:
            return None
        summary = WorkflowPlanVersionSummary(
            task_id=task_id,
            plan_id=current_plan.plan_id,
            plan_version=current_plan.plan_version,
            parent_plan_id=current_plan.parent_plan_id,
            user_goal=current_plan.user_goal,
            change_summary=current_plan.change_summary,
            created_at="",
            is_current=True,
        )
        return summary, current_plan

    plan = WorkflowPlan.model_validate_json(row["plan_json"])
    summary = WorkflowPlanVersionSummary(
        task_id=task_id,
        plan_id=str(row["plan_id"]),
        plan_version=int(row["plan_version"]),
        parent_plan_id=str(row["parent_plan_id"] or "") or None,
        user_goal=str(row["user_goal"]),
        change_summary=str(row["change_summary"]),
        created_at=str(row["created_at"]),
        is_current=str(row["plan_id"]) == current_plan.plan_id,
    )
    return summary, plan


def has_runtime_descendant(task_id: str) -> bool:
    """判断 dry-run 是否已经派生 Runtime，防止修订“当前计划”却不影响旧执行。"""

    # 使用 ! 作为 SQL LIKE 转义符，任务 ID 即使包含通配符也不会扩大查询范围。
    escaped_task_id = task_id.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    pattern = f"{escaped_task_id}!_runtime!_%"
    with get_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM workflow_runs WHERE task_id LIKE ? ESCAPE '!' LIMIT 1",
            (pattern,),
        ).fetchone()
    return row is not None


def load_task_log_events(task_id: str) -> list[TaskLogEvent] | None:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT event_json
            FROM workflow_events
            WHERE task_id = ?
            ORDER BY sequence ASC
            """,
            (task_id,),
        ).fetchall()

    if not rows:
        return None
    return [TaskLogEvent.model_validate_json(row["event_json"]) for row in rows]


def list_workflow_runs(
    *,
    limit: int,
    offset: int,
    status: WorkflowRunStatus | None = None,
    mode: WorkflowRunMode | None = None,
    max_risk_level: RiskLevel | None = None,
    requires_confirmation: bool | None = None,
) -> tuple[int, list[WorkflowRunListItem]]:
    """分页读取任务摘要列表。

    当前 summary/step_count 从 run_json 解析，避免为了早期 UI 列表反复迁移表结构。
    等任务列表字段稳定后，可以把 summary、step_count 单独落列提升查询性能。
    """

    where_sql, filter_params = _build_workflow_run_filter_clause(
        status=status,
        mode=mode,
        max_risk_level=max_risk_level,
        requires_confirmation=requires_confirmation,
    )
    with get_connection() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) AS total FROM workflow_runs{where_sql}",
            filter_params,
        ).fetchone()["total"]
        rows = connection.execute(
            f"""
            SELECT
                task_id, mode, status, max_risk_level, requires_confirmation,
                run_json, created_at, updated_at
            FROM workflow_runs
            {where_sql}
            ORDER BY updated_at DESC, created_at DESC, task_id DESC
            LIMIT ? OFFSET ?
            """,
            (*filter_params, limit, offset),
        ).fetchall()

    tasks: list[WorkflowRunListItem] = []
    for row in rows:
        run = WorkflowRun.model_validate_json(row["run_json"])
        tasks.append(
            WorkflowRunListItem(
                task_id=row["task_id"],
                mode=row["mode"],
                status=row["status"],
                summary=run.summary,
                max_risk_level=row["max_risk_level"],
                requires_confirmation=bool(row["requires_confirmation"]),
                step_count=len(run.steps),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )

    return total, tasks


def list_runtime_permission_requests(
    *,
    task_id: str,
    decision: RuntimePermissionDecision | None = None,
) -> list[RuntimePermissionItem]:
    """读取某个任务的权限请求。

    权限请求数量通常等于敏感步骤数量，规模很小；这里按 task_id 精确读取，不会扫描历史任务。
    """

    clauses = ["task_id = ?"]
    params: list[object] = [task_id]
    if decision is not None:
        clauses.append("decision = ?")
        params.append(decision)

    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM runtime_permission_requests
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at ASC, request_id ASC
            """,
            tuple(params),
        ).fetchall()

    return [_runtime_permission_item_from_row(row) for row in rows]


def record_runtime_permission_decision(
    *,
    task_id: str,
    request_id: str,
    decision_input: RuntimePermissionDecisionInput,
) -> RuntimePermissionItem | None:
    """写入权限决策并返回更新后的审计记录。

    只有已经存在的 request_id 才能被批准或拒绝；这样真实 Runtime 不能绕过“先创建请求、
    再等待用户决策”的顺序。
    """

    decided_at = datetime.now(UTC).isoformat(timespec="seconds")
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM runtime_permission_requests
            WHERE task_id = ? AND request_id = ?
            """,
            (task_id, request_id),
        ).fetchone()
        if row is None:
            return None

        connection.execute(
            """
            UPDATE runtime_permission_requests
            SET decision = ?,
                decided_by = ?,
                decided_at = ?,
                note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND request_id = ?
            """,
            (
                decision_input.decision,
                decision_input.decided_by,
                decided_at,
                decision_input.note,
                task_id,
                request_id,
            ),
        )
        updated = connection.execute(
            """
            SELECT *
            FROM runtime_permission_requests
            WHERE task_id = ? AND request_id = ?
            """,
            (task_id, request_id),
        ).fetchone()

    return _runtime_permission_item_from_row(updated) if updated is not None else None


def _build_workflow_run_filter_clause(
    *,
    status: WorkflowRunStatus | None,
    mode: WorkflowRunMode | None,
    max_risk_level: RiskLevel | None,
    requires_confirmation: bool | None,
) -> tuple[str, tuple[object, ...]]:
    """构造任务列表过滤条件。

    SQL 片段只来自代码内固定字段名，用户传入值全部走参数绑定；这样既能组合多个筛选，
    也避免把查询参数直接拼进 SQL。
    """

    clauses: list[str] = []
    params: list[object] = []

    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if mode is not None:
        clauses.append("mode = ?")
        params.append(mode)
    if max_risk_level is not None:
        clauses.append("max_risk_level = ?")
        params.append(max_risk_level)
    if requires_confirmation is not None:
        clauses.append("requires_confirmation = ?")
        params.append(1 if requires_confirmation else 0)

    if not clauses:
        return "", tuple()

    return " WHERE " + " AND ".join(clauses), tuple(params)


def _runtime_permission_item_from_row(row) -> RuntimePermissionItem:
    request = RuntimePermissionRequest.model_validate_json(row["request_json"])
    decision = RuntimePermissionDecisionRecord(
        request_id=row["request_id"],
        task_id=row["task_id"],
        step_id=row["step_id"],
        decision=row["decision"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        note=row["note"],
    )
    return RuntimePermissionItem(
        request=request,
        decision=decision,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _permission_request_row(request: RuntimePermissionRequest) -> tuple[str, ...]:
    return (
        request.request_id,
        request.task_id,
        request.step_id,
        request.agent_id,
        json.dumps(request.permissions, ensure_ascii=False),
        request.risk_level,
        request.summary,
        json.dumps(request.details, ensure_ascii=False),
        request.model_dump_json(),
    )
