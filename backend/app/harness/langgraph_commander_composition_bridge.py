"""LGM5.3 组合任务正式 bridge 的映射与同步边界。

此模块尚不把 LangGraph 注册为客户 Runtime。它只为将来的受审计试点建立三项事实：稳定
调用键、AgentFlow 主任务到图 checkpoint 的一对一映射，以及图受限结果到主库终态的同步。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256

from app.database.langgraph_bridge_repository import transition_langgraph_composition_bridge
from app.harness.langgraph_commander_composition_shadow import (
    CommanderCompositionShadowResult,
    build_composition_invocations,
    composition_graph_identity,
    composition_thread_id,
)
from app.schemas.chat import WorkflowPlan
from app.schemas.langgraph_bridge import LangGraphCompositionBridgeRecord


def build_composition_bridge_record(
    *,
    runtime_task_id: str,
    plan: WorkflowPlan,
) -> LangGraphCompositionBridgeRecord:
    """为一个已创建的 Runtime 任务生成稳定 bridge 身份，不携带客户正文。"""

    invocations, plan_digest = build_composition_invocations(plan)
    graph_id, graph_version = composition_graph_identity()
    invocation_ids = tuple(sorted(item.invocation_id for item in invocations))
    bridge_invocation_key = _stable_digest(
        {
            "runtime_task_id": runtime_task_id,
            "graph_id": graph_id,
            "graph_version": graph_version,
            "plan_digest": plan_digest,
            "invocation_ids": invocation_ids,
        }
    )
    now = _now()
    return LangGraphCompositionBridgeRecord(
        runtime_task_id=runtime_task_id,
        bridge_invocation_key=bridge_invocation_key,
        graph_id=graph_id,
        graph_version=graph_version,
        thread_id=composition_thread_id(runtime_task_id),
        plan_digest=plan_digest,
        created_at=now,
        updated_at=now,
    )


def mark_composition_bridge_running(
    *,
    runtime_task_id: str,
) -> LangGraphCompositionBridgeRecord:
    """在图首次/恢复调用前登记运行态；它不会派发任何专业动作。"""

    return transition_langgraph_composition_bridge(
        runtime_task_id=runtime_task_id,
        status="running",
        delivery_state="pending",
    )


def sync_composition_bridge_result(
    *,
    runtime_task_id: str,
    result: CommanderCompositionShadowResult,
) -> LangGraphCompositionBridgeRecord:
    """把 Graph 受限结果投影为主任务 bridge 状态。

    子 Agent 的完整交付物仍应保留在 AgentFlow 既有 task/artifact 表；这里仅记录哪些稳定
    invocation 已完成或待恢复，防止 checkpoint 双方对客户结论拥有第二份事实来源。
    """

    if result.task_id != runtime_task_id:
        raise ValueError("LGM5 组合 bridge 结果与 Runtime 任务标识不匹配。")
    delivery_state = str(result.delivery.get("status", "failed"))
    if delivery_state not in {"completed", "partial", "blocked", "failed"}:
        raise ValueError("LGM5 组合图返回了未声明的交付状态。")
    return transition_langgraph_composition_bridge(
        runtime_task_id=runtime_task_id,
        status=result.status,
        delivery_state=delivery_state,
        completed_invocation_ids=result.completed_invocation_ids,
        failed_invocation_ids=result.failed_invocation_ids,
    )


def _stable_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
