from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.model import ModelRouteAuditSnapshot
from app.schemas.workflow import WorkflowRun

RiskLevel = Literal["low", "medium", "high"]
CommandRiskLevel = Literal["none", "read_only", "diagnostic", "modifying", "network", "high_risk"]
WorkflowExecutionMode = Literal["execute", "guided_handoff", "planning_only"]
WorkflowAdmissionStatus = Literal["ready", "guided", "blocked"]
WorkflowMaterialKind = Literal["document", "dataset", "knowledge_base", "artifact"]
WorkflowMaterialOrigin = Literal["client_selected", "user_named", "task_artifact"]
# 组合计划在 C6.3 先表达依赖图，不允许旧 Runtime 把它误当成已经具备结果汇总能力的
# 多 Agent 执行任务。等 C6.4 具备父任务汇总与有限并发后，才会解除该保护。
WorkflowExecutionReadiness = Literal["ready", "requires_composition_runtime"]


class WorkflowCommandPolicy(BaseModel):
    """单个步骤的命令治理摘要。

    这不是让 Agent 直接执行 Shell，而是让 Commander 在计划层先说明：
    这个步骤是否可能需要命令、风险等级是什么、是否必须等待用户确认。
    Runtime 后续仍要按平台权限策略重新校验，不能只相信计划字段。
    """

    may_run_command: bool = False
    risk_level: CommandRiskLevel = "none"
    requires_confirmation: bool = False
    allowed: bool = True
    reason: str = ""


class WorkflowRetryPolicy(BaseModel):
    """计划层的重试建议。

    真实重试由 Runtime 执行；这里先给 UI、审计和未来 LangGraph 编排层一个稳定入口。
    """

    max_attempts: int = Field(default=1, ge=1)
    retryable: bool = False
    stop_condition: str = "stop_on_first_failure"


class WorkflowPlanPreferences(BaseModel):
    """本次计划实际应用的用户偏好快照。"""

    permission_policy: str = "smart_confirm"
    personality: str = "professional"
    cost_mode: str = "balanced"
    execution_style: str = "plan_then_confirm"
    detail_level: str = "standard"
    memory_enabled: bool = False


class WorkflowBudgetEstimate(BaseModel):
    """计划开始前给用户看的粗粒度预算。"""

    step_count: int = Field(default=0, ge=0)
    time_level: Literal["low", "medium", "high"] = "low"
    model_cost_level: Literal["low", "medium", "high"] = "low"
    requires_network: bool = False
    requires_command: bool = False


class WorkflowWorkspaceScope(BaseModel):
    """计划可能触碰的工作区范围。

    这里是预估范围，不代表授权；真实读写仍由 Runtime 的 workspace/outputs 边界校验。
    """

    read_paths: list[str] = Field(default_factory=list)
    write_paths: list[str] = Field(default_factory=list)
    external_services: list[str] = Field(default_factory=list)
    notes: str = ""


class WorkflowMaterialBinding(BaseModel):
    """总指挥计划中允许使用的一份受控材料引用。

    计划只保存后端可复核的相对引用和用途，绝不保存绝对路径、正文或表格原始行。这样
    “用户选了什么材料”可以跨计划版本和子任务追溯，而模型内容仍由具体 Agent 的受控
    Tool 按需读取。
    """

    binding_id: str = Field(min_length=1, max_length=160)
    kind: WorkflowMaterialKind
    ref: str = Field(min_length=1, max_length=280)
    display_name: str = Field(default="", max_length=180)
    origin: WorkflowMaterialOrigin = "client_selected"
    usage: str = Field(default="", max_length=240)
    model_visible: bool = False


CommanderAgentHintId = Literal["document_agent", "data_agent", "knowledge_agent"]


class CommanderAgentHint(BaseModel):
    """客户在调度台显式点名的一项已实现专业能力。

    它是本轮计划的路由偏好和材料提醒，不是 ``agent_id`` 替换，更不能提升权限、
    打开网络或绕过 action 准入。枚举保持很小，避免客户端把任意插件或历史占位 Agent
    伪装成已经可执行的能力。
    """

    agent_id: CommanderAgentHintId
    source: Literal["mention"] = "mention"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    agent_id: str | None = None
    task_id: str | None = None
    # 同一调度会话的稳定、无业务含义 ID。缺省时由后端创建；它只关联有限短期上下文，
    # 不能替代用户确认的跨会话长期记忆，也不能携带路径、账号或任意客户正文。
    conversation_id: str | None = Field(default=None, max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)
    # 当前 Qt 仍可只发 message；以后材料选择器会把后端确认过的相对引用放在这里。
    # 客户端不能用该字段传递绝对路径或正文，服务层与具体 Agent 仍会二次校验引用范围。
    materials: list[WorkflowMaterialBinding] = Field(default_factory=list, max_length=8)
    # `@文档助手` 一类标签只在本轮缩小 Commander 的候选路由；后端仍会从 message 解析
    # 同一份有限别名，以兼容 API 客户端和避免 Qt/UI 状态成为唯一可信来源。
    agent_hints: list[CommanderAgentHint] = Field(default_factory=list, max_length=3)
    # 项目范围只是一枚稳定标识，不是工作区路径。它决定本次计划可读取哪个 project:* 的
    # 已确认短记忆，并会写入计划快照供后续任务结束时的记忆候选继续复用。
    project_scope: str = Field(default="global", min_length=1, max_length=80)


class WorkflowStep(BaseModel):
    """工作流步骤的后端协议模型。

    Qt 当前只展示基础字段；权限和风险字段先进入协议，给后续 Workflow Engine、
    用户确认弹窗和审计日志预留稳定结构。
    """

    id: str
    agent: str
    action: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    # 相同非空组代表这些只读专业步骤可在未来 C6.4 Runtime 中申请同一受控并发槽位。
    # 它只是计划语义，当前 Runtime 不会因为这个字段自行并发执行。
    parallel_group: str = Field(default="", max_length=80)
    input: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    expected_output: str = ""
    required_permissions: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    requires_confirmation: bool = False
    tool_name: str | None = None
    command_policy: WorkflowCommandPolicy = Field(default_factory=WorkflowCommandPolicy)
    success_criteria: list[str] = Field(default_factory=list)
    timeout_ms: int | None = Field(default=None, ge=1)
    retry_policy: WorkflowRetryPolicy = Field(default_factory=WorkflowRetryPolicy)
    # execute 表示 Runtime 可以执行已准入的 Agent action；guided_handoff 只提示用户
    # 转到对应工作台，不会伪造子任务；planning_only 是总指挥本身的无副作用步骤。
    execution_mode: WorkflowExecutionMode = "execute"
    admission_status: WorkflowAdmissionStatus = "ready"
    admission_reason: str = ""
    verification_scope: str = ""
    recovery_hint: str = ""


class WorkflowPlan(BaseModel):
    """Commander 输出给前端和 Workflow Engine 的结构化计划。

    `validation_errors` 只表达计划结构问题，不代表任务执行失败。当前阶段先返回空列表；
    后续如果 LLM JSON 规划出错，可以把错误展示给用户，而不是直接执行。
    """

    # `version/workflow_name` 是早期协议字段，继续保留给 Qt 和旧任务兼容；
    # 新增的 schema_version/plan_id/plan_version 用于后续更清晰地区分协议和计划修订。
    version: str = "1.0"
    schema_version: str = "agentflow.workflow_plan.v1"
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:12]}")
    plan_version: int = Field(default=1, ge=1)
    parent_plan_id: str | None = None
    change_summary: str = ""
    intent: str = "general"
    user_goal: str = ""
    workflow_name: str
    description: str
    summary: str = ""
    clarifying_questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    definition_of_done: list[str] = Field(default_factory=list)
    preference_applied: WorkflowPlanPreferences = Field(default_factory=WorkflowPlanPreferences)
    budget_estimate: WorkflowBudgetEstimate = Field(default_factory=WorkflowBudgetEstimate)
    workspace_scope: WorkflowWorkspaceScope = Field(default_factory=WorkflowWorkspaceScope)
    material_bindings: list[WorkflowMaterialBinding] = Field(default_factory=list)
    # 记录规范化后的显式路由偏好，方便计划版本、任务历史与模型表达共享同一份事实。
    agent_hints: list[CommanderAgentHint] = Field(default_factory=list, max_length=3)
    # 计划的项目身份只用于记忆隔离与审计，绝不代表 Runtime 获得了一个新的文件系统范围。
    project_scope: str = "global"
    # C6.2 会话身份只用于关联有限的自动上下文和任务审计；模型不会取得稳定 ID，Qt 也不把
    # 它解释成材料或权限。旧计划缺失该字段时保持空字符串以兼容历史记录。
    conversation_id: str = ""
    # 仅保留本次真正注入的短摘要，便于计划/历史页审计；完整记录仍由记忆管理 API 管理。
    memory_context_summary: list[str] = Field(default_factory=list, max_length=3)
    conversation_context_summary: list[str] = Field(default_factory=list, max_length=2)
    steps: list[WorkflowStep]
    max_risk_level: RiskLevel = "low"
    requires_confirmation: bool = False
    # 计划是否能进入当前 Native Runtime。组合预览在 C6.3 阶段必须停在计划审阅，
    # 不能让旧执行器顺序跑完几个子任务后伪装为“已经汇总”。
    execution_readiness: WorkflowExecutionReadiness = "ready"
    validation_errors: list[str] = Field(default_factory=list)
    next_action: str = "execute_after_confirm"
    conflict_summary: dict[str, Any] = Field(default_factory=dict)
    task_retrospective: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    task_id: str
    agent_id: str
    reply: str
    # 回传服务端确认的会话 ID。客户端只缓存该 ID，不持久化聊天正文或材料正文。
    conversation_id: str = ""
    mode: str = "mock"
    model: str | None = None
    model_route: ModelRouteAuditSnapshot | None = None
    workflow_plan: WorkflowPlan | None = None
    workflow_run: WorkflowRun | None = None
    artifacts: list[str] = Field(default_factory=list)
