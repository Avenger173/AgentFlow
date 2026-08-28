"""总指挥 C3 的计划版本服务。

这里不接受客户端提交的步骤 JSON，更不会修改正在运行的 Runtime。它只把用户确认的新目标
重新送入既有 Commander 准入规则，得到下一版 dry-run，保留原计划快照并记录审计事件。
"""

from __future__ import annotations

from app.database.task_repository import (
    append_workflow_event,
    ensure_workflow_plan_version,
    has_runtime_descendant,
    load_workflow_plan,
    load_workflow_run,
)
from app.schemas.chat import WorkflowPlan
from app.schemas.plan_revisions import WorkflowPlanRevisionRequest
from app.schemas.workflow import WorkflowRun
from app.services.agent_catalog import list_agents
from app.services.commander import create_commander_plan
from app.services.commander_memory import retrieve_commander_memory_context
from app.workflow.dry_run import run_workflow_dry_run


class WorkflowPlanRevisionError(RuntimeError):
    """计划无法安全修订时返回给 API 的可理解原因。"""


def revise_workflow_plan(
    *,
    task_id: str,
    request: WorkflowPlanRevisionRequest,
) -> tuple[WorkflowPlan, WorkflowRun]:
    """以新目标替换尚未执行的 dry-run 当前计划，并保存不可变版本历史。"""

    source_run = load_workflow_run(task_id)
    current_plan = load_workflow_plan(task_id)
    if source_run is None or current_plan is None:
        raise LookupError("未找到可修订的任务计划。")
    if source_run.mode != "dry_run":
        raise WorkflowPlanRevisionError("只有尚未执行的 dry-run 计划可以修改；Runtime 任务必须在原执行链中完成或取消。")
    if has_runtime_descendant(task_id):
        raise WorkflowPlanRevisionError("该计划已经派生真实执行记录，不能再修改当前版本；请创建新任务，避免执行与计划不一致。")
    if request.user_goal.strip() == current_plan.user_goal.strip():
        raise WorkflowPlanRevisionError("新目标与当前计划相同；请说明需要改变的范围、材料或预期结果。")

    # 首先补齐升级前任务的版本 1，再写版本 2；旧计划永远不会被新目标覆盖。
    ensure_workflow_plan_version(task_id=task_id, plan=current_plan)
    agents = list_agents()
    memory_context = retrieve_commander_memory_context(
        user_goal=request.user_goal,
        preferences=current_plan.preference_applied,
        project_scope=current_plan.project_scope,
    )
    revised_plan = create_commander_plan(
        request.user_goal,
        available_agents=agents,
        preferences=current_plan.preference_applied,
        materials=current_plan.material_bindings,
        agent_hints=current_plan.agent_hints,
        memory_context=memory_context,
        project_scope=current_plan.project_scope,
    ).model_copy(
        update={
            "plan_version": current_plan.plan_version + 1,
            "parent_plan_id": current_plan.plan_id,
            "change_summary": f"用户确认修改：{request.change_summary.strip()}",
        }
    )
    # 重新 dry-run 只重建审查数据，不触发专业 Agent、文件写入、网络或模型调用。
    revised_run = run_workflow_dry_run(
        task_id=task_id,
        plan=revised_plan,
        available_agents=agents,
    )
    append_workflow_event(
        task_id=task_id,
        event_name="plan_revised",
        agent_id="commander_agent",
        message=(
            f"用户已确认计划修订：v{current_plan.plan_version} -> v{revised_plan.plan_version}。"
            f"变更说明：{request.change_summary.strip()}"
        ),
    )
    return revised_plan, revised_run
