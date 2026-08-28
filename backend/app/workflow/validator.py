from __future__ import annotations

from collections.abc import Iterable

from app.schemas.agent import AgentDescriptor, AgentPermissions
from app.schemas.chat import WorkflowPlan
from app.workflow.action_admission import action_admission_for


# 产品级确认不是 Agent manifest 的底层 Tool 能力。它只用于让 Runtime 在发生较长、较贵但
# 仍是只读的任务前停驻，例如全库深度分析；具体 action 仍须在 Admission 中逐项声明。
PRODUCT_CONFIRMATION_PERMISSIONS = {"knowledge_deep_analysis"}
VALID_PERMISSION_NAMES = set(AgentPermissions.model_fields.keys()) | PRODUCT_CONFIRMATION_PERMISSIONS
SENSITIVE_PERMISSIONS = {"file_write", "network", "shell", "database", *PRODUCT_CONFIRMATION_PERMISSIONS}


def validate_workflow_plan(
    plan: WorkflowPlan,
    *,
    available_agents: Iterable[AgentDescriptor] | None = None,
) -> list[str]:
    """校验 Commander 生成的工作流计划。

    这里暂时只做轻量、确定性的结构校验，不触发任何 Agent 执行，也不访问文件/网络。
    后续 Workflow Engine 会复用这层校验，避免 LLM 或插件生成的计划绕过权限边界。
    """

    errors: list[str] = []
    enabled_agents = _enabled_agent_map(available_agents)

    if not plan.workflow_name.strip():
        errors.append("workflow_name 不能为空。")
    if not plan.schema_version.strip():
        errors.append("schema_version 不能为空。")
    if not plan.plan_id.strip():
        errors.append("plan_id 不能为空。")
    if not plan.summary.strip():
        errors.append("summary 不能为空。")
    if not plan.steps:
        errors.append("workflow_plan 至少需要一个 step。")
        return errors

    step_ids = [step.id for step in plan.steps]
    step_id_set = set(step_ids)

    _validate_step_identity(step_ids, errors)
    _validate_steps(plan, enabled_agents, step_id_set, errors)
    _validate_dag(plan, step_id_set, errors)

    return errors


def _enabled_agent_map(
    agents: Iterable[AgentDescriptor] | None,
) -> dict[str, AgentDescriptor]:
    if agents is None:
        return {}

    return {agent.id: agent for agent in agents if agent.enabled}


def _validate_step_identity(step_ids: list[str], errors: list[str]) -> None:
    seen: set[str] = set()
    for step_id in step_ids:
        if not step_id.strip():
            errors.append("step.id 不能为空。")
            continue
        if step_id in seen:
            errors.append(f"step.id 重复：{step_id}")
        seen.add(step_id)


def _validate_steps(
    plan: WorkflowPlan,
    enabled_agents: dict[str, AgentDescriptor],
    step_id_set: set[str],
    errors: list[str],
) -> None:
    for step in plan.steps:
        if not step.agent.strip():
            errors.append(f"{step.id}: agent 不能为空。")
        elif enabled_agents and step.agent not in enabled_agents:
            errors.append(f"{step.id}: Agent 不存在或未启用：{step.agent}")

        if not step.action.strip():
            errors.append(f"{step.id}: action 不能为空。")
        if not step.title.strip():
            errors.append(f"{step.id}: title 不能为空。")
        if not step.reason.strip():
            errors.append(f"{step.id}: reason 不能为空。")
        if not step.expected_output.strip():
            errors.append(f"{step.id}: expected_output 不能为空。")

        invalid_permissions = [
            permission
            for permission in step.required_permissions
            if permission not in VALID_PERMISSION_NAMES
        ]
        if invalid_permissions:
            errors.append(f"{step.id}: 未知权限字段：{', '.join(invalid_permissions)}")

        if enabled_agents and step.agent in enabled_agents:
            _validate_agent_permission_subset(step, enabled_agents[step.agent], errors)
            _validate_runtime_readiness(step, enabled_agents[step.agent], errors)

        _validate_action_admission(step, plan, errors)

        if (
            SENSITIVE_PERMISSIONS.intersection(step.required_permissions)
            and not step.requires_confirmation
        ):
            errors.append(f"{step.id}: 涉及敏感权限但未标记 requires_confirmation。")

        if (
            step.command_policy.risk_level in {"modifying", "network", "high_risk"}
            and not step.command_policy.requires_confirmation
        ):
            errors.append(f"{step.id}: 命令策略包含风险操作但未要求确认。")

        for dependency in step.depends_on:
            if dependency == step.id:
                errors.append(f"{step.id}: 不能依赖自身。")
            if dependency not in step_id_set:
                errors.append(f"{step.id}: 依赖不存在：{dependency}")


def _validate_agent_permission_subset(
    step,
    agent: AgentDescriptor,
    errors: list[str],
) -> None:
    agent_permissions = {
        name
        for name, enabled in agent.permissions.model_dump().items()
        if enabled
    }
    admission = action_admission_for(step.agent, step.action)
    action_confirmation_permissions = set(admission.additional_permissions) if admission else set()
    missing_permissions = [
        permission
        for permission in step.required_permissions
        if permission not in agent_permissions
        and permission not in action_confirmation_permissions
    ]
    if missing_permissions:
        errors.append(
            f"{step.id}: step 权限超出 Agent manifest 声明：{', '.join(missing_permissions)}"
        )


def _validate_runtime_readiness(step, agent: AgentDescriptor, errors: list[str]) -> None:
    """阻止未就绪 Agent 被伪装成 Runtime 可执行步骤。"""

    if step.execution_mode == "execute" and not agent.runtime_ready:
        errors.append(f"{step.id}: Agent 尚未 runtime_ready，不能作为 execute 步骤。")


def _validate_action_admission(step, plan: WorkflowPlan, errors: list[str]) -> None:
    """校验已登记 action 的执行模式和显式材料范围。

    旧的底层 Runtime 回归可构造未登记的兼容步骤；但一旦使用 C0 已登记 action，就必须
    服从其运行模式。这样插件或模型不能借助一个看似合理的 plan 绕过产品准入。
    """

    admission = action_admission_for(step.agent, step.action)
    if admission is None:
        return
    if step.execution_mode != admission.execution_mode:
        errors.append(f"{step.id}: action 执行模式与准入目录不一致。")
    expected_status = "guided" if admission.execution_mode == "guided_handoff" else "ready"
    if step.admission_status != expected_status:
        errors.append(f"{step.id}: action 准入状态应为 {expected_status}。")
    if admission.material_kind and plan.material_bindings:
        if not any(item.kind == admission.material_kind for item in plan.material_bindings):
            errors.append(f"{step.id}: 缺少 {admission.material_kind} 类型的显式材料绑定。")


def _validate_dag(
    plan: WorkflowPlan,
    step_id_set: set[str],
    errors: list[str],
) -> None:
    """检查依赖图是否成环。

    这里不要求依赖步骤一定出现在数组前面，因为后续 LLM 规划可能先给全量 DAG，
    Workflow Engine 再做拓扑排序；但成环一定不能执行。
    """

    dependencies = {
        step.id: [dependency for dependency in step.depends_on if dependency in step_id_set]
        for step in plan.steps
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str, trail: list[str]) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            cycle = " -> ".join([*trail, step_id])
            errors.append(f"workflow_plan 依赖成环：{cycle}")
            return

        visiting.add(step_id)
        for dependency in dependencies.get(step_id, []):
            visit(dependency, [*trail, step_id])
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in dependencies:
        visit(step_id, [])
