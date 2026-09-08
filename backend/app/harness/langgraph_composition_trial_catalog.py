"""LGM5.7 开发者试点源计划的脱敏目录。"""

from __future__ import annotations

from dataclasses import dataclass

from app.database.sqlite import get_connection
from app.database.task_repository import load_workflow_plan
from app.harness.langgraph_commander_composition_shadow import build_composition_invocations
from app.workflow.runtime import supports_native_read_only_composition_runtime


@dataclass(frozen=True)
class LangGraphCompositionTrialSource:
    """可用于开发者试点的完成 dry-run 最小标识，不含客户目标或材料信息。"""

    task_id: str
    plan_id: str
    plan_digest: str
    specialist_actions: tuple[str, ...]


def list_composition_developer_trial_sources(
    *,
    limit: int = 20,
) -> tuple[LangGraphCompositionTrialSource, ...]:
    """列出最近的 C6.4 完成计划，只输出试点选择所需的脱敏身份。"""

    if not 1 <= limit <= 100:
        raise ValueError("试点计划列表数量必须在 1 至 100 之间。")
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT task_id
            FROM workflow_runs
            WHERE mode = 'dry_run' AND status = 'completed' AND plan_json IS NOT NULL
            ORDER BY updated_at DESC, task_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    sources: list[LangGraphCompositionTrialSource] = []
    for row in rows:
        task_id = str(row["task_id"])
        plan = load_workflow_plan(task_id)
        if plan is None or not supports_native_read_only_composition_runtime(plan):
            continue
        invocations, plan_digest = build_composition_invocations(plan)
        sources.append(
            LangGraphCompositionTrialSource(
                task_id=task_id,
                plan_id=plan.plan_id,
                plan_digest=plan_digest,
                specialist_actions=tuple(
                    f"{item.agent_id}.{item.action}" for item in invocations
                ),
            )
        )
    return tuple(sources)
