"""LGM5 LangGraph 组合 bridge 的主库映射仓储。

LangGraph 自己的 SQLite checkpoint 仅负责图状态。本仓储让 AgentFlow 继续持有客户 Runtime
任务、稳定调用键和交付终态，且只落脱敏摘要协议，绝不把客户输入复制到第二套数据库。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.database.sqlite import get_connection
from app.schemas.langgraph_bridge import LangGraphCompositionBridgeRecord


class LangGraphBridgeConflictError(ValueError):
    """同一 Runtime 任务试图以不同图版本或计划摘要重复接入。"""


def ensure_langgraph_composition_bridge(
    record: LangGraphCompositionBridgeRecord,
) -> LangGraphCompositionBridgeRecord:
    """创建或读取同一 Runtime 的稳定桥接记录。

    ``runtime_task_id`` 是 AgentFlow 层唯一键；相同任务只能对应同一图身份、线程、调用键和
    计划摘要。重复提交完全相同的记录会返回当前状态，防止重试创建第二份子任务链。
    """

    with get_connection() as connection:
        existing = connection.execute(
            "SELECT bridge_json FROM langgraph_runtime_bridges WHERE runtime_task_id = ?",
            (record.runtime_task_id,),
        ).fetchone()
        if existing is not None:
            current = LangGraphCompositionBridgeRecord.model_validate_json(existing["bridge_json"])
            _ensure_same_bridge(current=current, requested=record)
            return current

        connection.execute(
            """
            INSERT INTO langgraph_runtime_bridges (
                runtime_task_id, backend_id, graph_id, graph_version, thread_id,
                bridge_invocation_key, plan_digest, status, delivery_state, bridge_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _record_row(record),
        )
    return record


def load_langgraph_composition_bridge(
    runtime_task_id: str,
) -> LangGraphCompositionBridgeRecord | None:
    """读取一条已存在的组合 bridge，不扫描其他客户任务。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT bridge_json FROM langgraph_runtime_bridges WHERE runtime_task_id = ?",
            (runtime_task_id,),
        ).fetchone()
    if row is None:
        return None
    return LangGraphCompositionBridgeRecord.model_validate_json(row["bridge_json"])


def update_langgraph_composition_bridge(
    record: LangGraphCompositionBridgeRecord,
) -> LangGraphCompositionBridgeRecord:
    """更新同一 bridge 的受控状态，不允许改写调用身份或退回终态。"""

    with get_connection() as connection:
        row = connection.execute(
            "SELECT bridge_json FROM langgraph_runtime_bridges WHERE runtime_task_id = ?",
            (record.runtime_task_id,),
        ).fetchone()
        if row is None:
            raise LangGraphBridgeConflictError("LGM5 组合 bridge 尚未准备，不能更新执行状态。")
        current = LangGraphCompositionBridgeRecord.model_validate_json(row["bridge_json"])
        _ensure_same_bridge(current=current, requested=record)
        _ensure_allowed_transition(current=current, updated=record)
        connection.execute(
            """
            UPDATE langgraph_runtime_bridges
            SET status = ?, delivery_state = ?, bridge_json = ?, updated_at = ?
            WHERE runtime_task_id = ?
            """,
            (
                record.status,
                record.delivery_state,
                record.model_dump_json(),
                record.updated_at,
                record.runtime_task_id,
            ),
        )
    return record


def transition_langgraph_composition_bridge(
    *,
    runtime_task_id: str,
    status: str,
    delivery_state: str | None = None,
    completed_invocation_ids: tuple[str, ...] | None = None,
    failed_invocation_ids: tuple[str, ...] | None = None,
) -> LangGraphCompositionBridgeRecord:
    """用现有身份推进 bridge 状态，给正式 Runtime 接入保留一个小而明确的入口。"""

    current = load_langgraph_composition_bridge(runtime_task_id)
    if current is None:
        raise LangGraphBridgeConflictError("LGM5 组合 bridge 尚未准备，不能推进执行状态。")
    updated = LangGraphCompositionBridgeRecord.model_validate(
        {
            **current.model_dump(),
            "status": status,
            "delivery_state": delivery_state if delivery_state is not None else current.delivery_state,
            "completed_invocation_ids": (
                completed_invocation_ids
                if completed_invocation_ids is not None
                else current.completed_invocation_ids
            ),
            "failed_invocation_ids": (
                failed_invocation_ids if failed_invocation_ids is not None else current.failed_invocation_ids
            ),
            "updated_at": _now(),
        }
    )
    return update_langgraph_composition_bridge(updated)


def _record_row(record: LangGraphCompositionBridgeRecord) -> tuple[object, ...]:
    return (
        record.runtime_task_id,
        record.backend_id,
        record.graph_id,
        record.graph_version,
        record.thread_id,
        record.bridge_invocation_key,
        record.plan_digest,
        record.status,
        record.delivery_state,
        record.model_dump_json(),
        record.created_at,
        record.updated_at,
    )


def _ensure_same_bridge(
    *,
    current: LangGraphCompositionBridgeRecord,
    requested: LangGraphCompositionBridgeRecord,
) -> None:
    identity = (
        "backend_id",
        "graph_id",
        "graph_version",
        "thread_id",
        "bridge_invocation_key",
        "plan_digest",
    )
    if any(getattr(current, name) != getattr(requested, name) for name in identity):
        raise LangGraphBridgeConflictError("同一 Runtime 任务不能绑定不同的 LGM5 组合图或计划摘要。")


def _ensure_allowed_transition(
    *,
    current: LangGraphCompositionBridgeRecord,
    updated: LangGraphCompositionBridgeRecord,
) -> None:
    allowed = {
        "prepared": {"prepared", "running", "cancelled", "failed"},
        "running": {"running", "partial", "completed", "blocked", "failed", "cancelled"},
        "partial": {"partial", "running", "completed", "blocked", "failed", "cancelled"},
        "blocked": {"blocked", "running", "failed", "cancelled"},
        "completed": {"completed"},
        "failed": {"failed"},
        "cancelled": {"cancelled"},
    }
    if updated.status not in allowed[current.status]:
        raise LangGraphBridgeConflictError("LGM5 组合 bridge 不允许从当前终态回退或跨越恢复边界。")
    if current.status in {"completed", "failed", "cancelled"} and (
        current.delivery_state != updated.delivery_state
        or current.completed_invocation_ids != updated.completed_invocation_ids
        or current.failed_invocation_ids != updated.failed_invocation_ids
    ):
        raise LangGraphBridgeConflictError("LGM5 组合 bridge 已进入终态，不能改写其交付集合。")
    if set(updated.completed_invocation_ids).difference(current.completed_invocation_ids):
        return
    if current.completed_invocation_ids != updated.completed_invocation_ids:
        raise LangGraphBridgeConflictError("已经完成的 LGM5 专业调用不能从 bridge 检查点回退。")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
