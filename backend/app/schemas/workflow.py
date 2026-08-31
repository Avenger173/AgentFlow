from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.model import ModelRouteAuditSnapshot


RiskLevel = Literal["low", "medium", "high"]
StepRunStatus = Literal[
    "pending",
    "running",
    "waiting_permission",
    "completed",
    "blocked",
    "failed",
    "cancelled",
    "skipped",
]
WorkflowRunStatus = Literal[
    "pending",
    "running",
    "paused",
    "waiting_permission",
    "completed",
    "blocked",
    "failed",
    "cancelled",
]
WorkflowRunMode = Literal["dry_run", "runtime"]
TaskControlAction = Literal["pause", "resume", "cancel", "retry"]
RuntimePermissionDecision = Literal["pending", "approved", "denied"]
RuntimePermissionDecisionInputValue = Literal["approved", "denied"]
WorkflowArtifactKind = Literal["text", "markdown", "code", "report", "data", "file", "other"]
WorkflowToolCallStatus = Literal[
    "simulated",
    "pending_permission",
    "running",
    "completed",
    "blocked",
    "failed",
    "skipped",
]
WorkflowTaskUpdateType = Literal[
    "lifecycle",
    "step",
    "tool_call",
    "permission",
    "artifact",
    "state",
]
WorkflowTaskUpdateLevel = Literal["info", "warning", "error"]
WorkflowCommandRiskLevel = Literal[
    "none",
    "read_only",
    "diagnostic",
    "modifying",
    "network",
    "high_risk",
]
WorkflowCommandPolicyAction = Literal["allow", "confirm", "block"]
WorkflowCommandExecutionScope = Literal[
    "none",
    "read_only",
    "diagnostic",
    "modifying",
    "network",
    "blocked",
]
WorkflowCommandRuntimeRequestStatus = Literal[
    "none",
    "ready",
    "needs_approval",
    "blocked",
]


class WorkflowStepRun(BaseModel):
    """Workflow Engine dry-run 的单步结果。

    当前阶段只模拟状态流转，不执行真实 Agent、工具、文件写入或命令。
    这个结构先固定下来，后续真实执行器可以沿用 status/output/error 字段。
    """

    step_id: str
    agent: str
    action: str
    status: StepRunStatus
    message: str
    requires_confirmation: bool = False
    risk_level: RiskLevel = "low"
    output: dict[str, Any] = Field(default_factory=dict)


class RuntimeExecutionLimits(BaseModel):
    """单次任务执行预算。

    这些值不是 UI 展示用的装饰字段，而是真实 Runtime 的刹车：后续工具执行、局部重试、
    长任务拆分都要先检查预算，避免 Agent 在失败循环里持续消耗时间和 token。
    """

    max_steps: int = 20
    max_tool_calls: int = 50
    max_retries_per_tool: int = 2
    tool_timeout_ms: int = 30_000
    task_timeout_ms: int = 120_000
    token_budget: int | None = None


class RuntimeExecutionMetrics(BaseModel):
    """任务运行指标。

    dry-run 阶段只记录模拟指标；真实 Runtime 接入后会更新耗时、失败数、重试数和 token
    估算。评估 Agent 效果时不要只看回复文本，要看这些可量化指标是否稳定。
    """

    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    step_total: int = 0
    step_completed: int = 0
    step_failed: int = 0
    tool_call_total: int = 0
    tool_call_simulated: int = 0
    tool_call_failed: int = 0
    retry_total: int = 0
    permission_request_total: int = 0
    validation_error_total: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_cost_cny: float = 0.0
    # 以下是 Provider 响应实际返回的 usage 聚合，不是字符数估算或账单金额。字段为 None 表示
    # 对应响应没有提供该项；结合 request_total 可避免把“未观测”误读成 0 token。
    provider_model_request_total: int = 0
    provider_usage_reported_request_total: int = 0
    provider_cache_observed_request_total: int = 0
    provider_input_tokens: int | None = None
    provider_output_tokens: int | None = None
    provider_total_tokens: int | None = None
    provider_cache_read_input_tokens: int | None = None
    provider_cache_creation_input_tokens: int | None = None
    provider_cache_miss_input_tokens: int | None = None
    budget_exceeded: bool = False


class WorkflowRun(BaseModel):
    """一次工作流 dry-run 的结果。

    `mode=dry_run` 明确告诉前端：这些结果只是执行预演，不代表已经产生文件或修改系统。
    真正执行、取消、重试和持久化会在阶段 4 后续小步接入。
    """

    task_id: str
    mode: WorkflowRunMode = "dry_run"
    status: WorkflowRunStatus
    summary: str
    max_risk_level: RiskLevel = "low"
    requires_confirmation: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    steps: list[WorkflowStepRun] = Field(default_factory=list)
    # C6.5：任务只保存 Provider/模型/思考/Profile 等脱敏路由事实。API Key、Base URL 中的
    # 潜在账户片段和模型原始请求不进入任务历史，避免审计反过来扩大敏感信息暴露。
    model_routes: list[ModelRouteAuditSnapshot] = Field(default_factory=list)
    limits: RuntimeExecutionLimits = Field(default_factory=RuntimeExecutionLimits)
    metrics: RuntimeExecutionMetrics = Field(default_factory=RuntimeExecutionMetrics)


class TaskControlResponse(BaseModel):
    """任务控制接口的统一响应。

    dry-run 阶段只支持安全的控制语义：已完成任务不能取消；retry 会基于缓存计划生成新的
    dry-run。后续真实执行器接入后，`accepted` 可表示取消/重试请求是否进入队列。
    """

    task_id: str
    action: TaskControlAction
    accepted: bool
    status: str
    message: str
    new_task_id: str | None = None
    workflow_run: WorkflowRun | None = None


class RuntimeExecutionControlState(BaseModel):
    """Runtime 的协作式控制信号。

    暂停和取消不会从 API 线程强行终止模型或文件解析线程。API 只把用户意图持久化，
    Runtime 在一次 Tool 调用完成、下一步骤开始前等安全边界读取它，从而保证不会留下半写入
    的交付物，也能在服务重启后解释“为什么当时停下”。
    """

    task_id: str
    pause_requested: bool = False
    cancel_requested: bool = False
    updated_at: str = ""


class WorkflowExecutionResponse(BaseModel):
    """真实 Runtime 执行入口的响应。

    `POST /api/tasks/{task_id}/execute` 可以从 dry-run 生成新的 runtime task，也可以在
    runtime task 等待权限后继续执行。source/runtime 分开，避免覆盖用户已经审查过的
    dry-run 记录。
    """

    source_task_id: str
    runtime_task_id: str
    accepted: bool
    status: WorkflowRunStatus
    message: str
    workflow_run: WorkflowRun | None = None


class WorkflowRunListItem(BaseModel):
    """任务列表中的轻量摘要。

    列表页不直接返回 steps/output 这类较大的详情；前端需要详情时再调用
    `GET /api/tasks/{task_id}`，避免历史任务多时列表接口变重。
    """

    task_id: str
    mode: WorkflowRunMode
    status: WorkflowRunStatus
    summary: str
    max_risk_level: RiskLevel
    requires_confirmation: bool
    step_count: int
    created_at: str
    updated_at: str


class WorkflowRunListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    tasks: list[WorkflowRunListItem] = Field(default_factory=list)


class WorkflowStepListResponse(BaseModel):
    """任务详情页可按需读取的 step 级结果。

    WorkflowRun 里仍会保留 steps，方便现有前端兼容；这个独立接口给后续历史页、失败重试
    和真实 Runtime 做局部刷新，避免每次只看步骤时都传完整任务对象。
    """

    task_id: str
    total: int
    steps: list[WorkflowStepRun] = Field(default_factory=list)


class WorkflowRuntimeMetricsResponse(BaseModel):
    task_id: str
    limits: RuntimeExecutionLimits
    metrics: RuntimeExecutionMetrics


class WorkflowModelRouteAuditResponse(BaseModel):
    """历史 Inspector 的实际模型路由审计响应。

    任务运行快照内部还包含步骤、指标等较大对象；历史页只为模型审计按需读取这份很小的
    脱敏列表。它不携带 API Key、Base URL、原始 Prompt、材料名称或 Provider 原始响应。
    """

    task_id: str
    model_routes: list[ModelRouteAuditSnapshot] = Field(default_factory=list)


class WorkflowRuntimeStateResponse(BaseModel):
    """任务 Runtime 状态机快照。

    真实执行器还没接入，但前端和后端需要先共享同一套状态语义：哪些状态是终态、
    哪些控制动作可用、下一步允许转到哪里。这样后续接入真实执行时不会把 cancel/retry
    写成各处临时判断。
    """

    task_id: str
    mode: WorkflowRunMode
    status: WorkflowRunStatus
    terminal: bool
    allowed_actions: list[TaskControlAction] = Field(default_factory=list)
    allowed_next_statuses: list[WorkflowRunStatus] = Field(default_factory=list)
    message: str = ""


class WorkflowArtifact(BaseModel):
    """工作流产物记录。

    dry-run 阶段只登记“如果真实执行会产生什么”，`uri` 使用 artifact:// 这类虚拟地址，
    不指向本地真实文件。真实 Runtime 接入后，同一个模型可以记录实际生成的文件、报告、
    图片或结构化数据，并由权限系统决定前端是否允许打开。
    """

    artifact_id: str
    task_id: str
    step_id: str
    agent_id: str
    kind: WorkflowArtifactKind = "other"
    name: str
    summary: str = ""
    uri: str = ""
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class WorkflowArtifactListResponse(BaseModel):
    task_id: str
    total: int
    artifacts: list[WorkflowArtifact] = Field(default_factory=list)


class WorkflowDeliveryArtifact(BaseModel):
    """结果卡中的安全产物摘要。"""

    artifact_id: str
    name: str
    kind: WorkflowArtifactKind = "other"
    summary: str = ""
    uri: str = ""
    mime_type: str = "text/plain"
    openable: bool = False
    previewable: bool = False


class WorkflowDeliveryFact(BaseModel):
    """结果卡中供客户快速扫读的一条事实。"""

    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=180)


class WorkflowDeliveryTableSummary(BaseModel):
    """结果卡中的数据交付摘要，只保留已回读验证的数量事实。"""

    table_count: int = Field(default=0, ge=0)
    chart_count: int = Field(default=0, ge=0)
    metric_count: int = Field(default=0, ge=0)
    description: str = Field(default="", max_length=220)


class WorkflowDeliveryCard(BaseModel):
    """统一的客户交付卡，隐藏 Runtime 细节但保留可操作结果。"""

    schema_version: Literal["agentflow.delivery.v1"] = "agentflow.delivery.v1"
    delivery_id: str = Field(min_length=8, max_length=180)
    task_id: str = Field(min_length=8, max_length=180)
    mode: WorkflowRunMode
    status: WorkflowRunStatus
    terminal: bool = False
    headline: str = Field(min_length=1, max_length=180)
    summary_markdown: str = Field(min_length=1, max_length=2_200)
    facts: list[WorkflowDeliveryFact] = Field(default_factory=list, max_length=12)
    table_summary: WorkflowDeliveryTableSummary | None = None
    warnings: list[str] = Field(default_factory=list, max_length=8)
    artifacts: list[WorkflowDeliveryArtifact] = Field(default_factory=list, max_length=8)
    next_actions: list[str] = Field(default_factory=list, max_length=4)
    updated_at: str = ""


class WorkflowArtifactPreviewResponse(BaseModel):
    """受控产物预览。

    前端只需要知道“能不能预览、预览了多少、是否截断”，不应该自己拼本地路径读取文件。
    真实 Runtime 产物由后端做目录边界、文本类型和大小限制，dry-run 虚拟产物则返回原因说明。
    """

    task_id: str
    artifact_id: str
    available: bool = False
    reason: str = ""
    kind: WorkflowArtifactKind = "other"
    name: str = ""
    uri: str = ""
    mime_type: str = "text/plain"
    source: str = "unavailable"
    text: str = ""
    encoding: str = "utf-8"
    bytes_read: int = 0
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowToolCall(BaseModel):
    """工作流工具调用审计记录。

    这是 Harness 的 Tool/Runtime 交界面：模型或 Agent 只能提出要调用什么工具，Runtime
    负责执行、权限拦截、结果记录和错误归因。dry-run 中 status=simulated 表示未真实调用。
    """

    call_id: str
    task_id: str
    step_id: str
    agent_id: str
    tool_name: str
    status: WorkflowToolCallStatus = "simulated"
    risk_level: RiskLevel = "low"
    permission_required: bool = False
    attempt: int = 1
    max_attempts: int = 3
    timeout_ms: int = 30_000
    duration_ms: int = 0
    failure_count: int = 0
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""


class WorkflowToolCallListResponse(BaseModel):
    task_id: str
    total: int
    tool_calls: list[WorkflowToolCall] = Field(default_factory=list)


class WorkflowTaskEvaluationResponse(BaseModel):
    """任务效果评估视图。

    这个结构服务 Harness 的 Evaluation 层：它不替代 metrics / tool-calls，
    而是把任务成功率、执行效率、权限阻塞和失败信号整理成用户能理解的摘要。
    当前先用确定性规则计算，后续可以叠加离线用例或 LLM-as-Judge。
    """

    task_id: str
    mode: WorkflowRunMode
    status: WorkflowRunStatus
    outcome: str
    summary: str
    step_success_rate: float = 0.0
    tool_success_rate: float = 0.0
    efficiency_score: float = 0.0
    overall_score: float = 0.0
    duration_ms: int = 0
    retry_total: int = 0
    failed_tool_calls: int = 0
    blocked_tool_calls: int = 0
    pending_permissions: int = 0
    denied_permissions: int = 0
    warnings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class WorkflowTaskUpdate(BaseModel):
    """任务 updates 时间线事件。

    logs 只回答“发生了什么文本事件”，steps/tool-calls/artifacts/permissions 分散在多个接口。
    updates 把这些事实按时间线聚合，给 Qt 和后续事件流面板一个稳定入口，避免前端靠猜测拼装。
    """

    sequence: int
    update_type: WorkflowTaskUpdateType
    event: str
    level: WorkflowTaskUpdateLevel = "info"
    agent_id: str = ""
    step_id: str | None = None
    status: str = ""
    title: str = ""
    message: str
    occurred_at: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowTaskUpdateListResponse(BaseModel):
    task_id: str
    total: int
    updates: list[WorkflowTaskUpdate] = Field(default_factory=list)


class WorkflowNodeContract(BaseModel):
    """内置 Agent 节点契约。

    这是阶段 5 的核心协议之一：它描述某个 Agent/action 会映射到哪个稳定 tool、
    需要什么输入、会写入哪些状态、可能触发哪些权限和失败码。前端、Runtime、验证脚本
    和未来 LangGraph 适配层都应该优先使用这份结构，而不是各自硬编码。
    """

    agent_id: str
    action: str
    tool_name: str
    node_type: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    state_writes: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    failure_codes: list[str] = Field(default_factory=list)
    evaluation_signals: list[str] = Field(default_factory=list)


class WorkflowNodeContractListResponse(BaseModel):
    """节点契约列表响应。

    `total` 与 `contracts` 一起返回，方便 Qt 之后做筛选计数、调试面板或 Agent 能力说明。
    """

    total: int
    contracts: list[WorkflowNodeContract] = Field(default_factory=list)


class WorkflowCommandPolicyCheckRequest(BaseModel):
    """命令安全策略检查请求。

    这个接口只做静态分类，不执行命令。后续代码工坊或 Shell 工具真正上线时，仍必须在
    Runtime 执行前再次校验权限和工作目录。
    """

    command: str = Field(min_length=1, max_length=4000)
    cwd: str = ""
    permission_policy: str = Field(
        default="",
        max_length=32,
        description="可选权限策略；为空时后端使用当前运行偏好。",
    )


class WorkflowCommandPolicyCheckResponse(BaseModel):
    """命令安全策略检查结果。

    风险级别沿用计划层 `command_policy` 的语义：只读命令可以低摩擦放行；诊断命令需要审计；
    修改、联网和高危命令必须进入权限确认或默认拒绝。
    """

    command: str
    normalized_command: str
    risk_level: WorkflowCommandRiskLevel = "none"
    allowed: bool = True
    requires_confirmation: bool = False
    audit_required: bool = True
    concurrency_safe: bool = False
    default_timeout_ms: int = 30_000
    max_output_chars: int = 60_000
    detected_commands: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    destructive_warnings: list[str] = Field(default_factory=list)
    safer_alternatives: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggested_tool: str = ""
    effective_permission_policy: str = "smart_confirm"
    effective_action: WorkflowCommandPolicyAction = "confirm"
    effective_reason: str = ""
    execution_scope: WorkflowCommandExecutionScope = "none"
    execution_route: str = ""
    cwd_policy: str = ""
    sandbox_hint: str = ""
    audit_fields: list[str] = Field(default_factory=list)
    execution_notes: list[str] = Field(default_factory=list)
    runtime_ready: bool = False
    permission_required: bool = False
    runtime_request_status: WorkflowCommandRuntimeRequestStatus = "blocked"
    approval_prompt: str = ""
    block_reason_code: str = ""
    audit_record_preview: dict[str, Any] = Field(default_factory=dict)


class WorkflowCommandPolicyRule(BaseModel):
    """命令治理规则目录项。

    这是给 UI、审计导出和未来 Runtime 解释用的稳定视图；内部正则不暴露，避免外部依赖实现细节。
    """

    rule_id: str
    risk_level: WorkflowCommandRiskLevel = "high_risk"
    category: str
    default_action: WorkflowCommandPolicyAction = "block"
    reason: str
    destructive_warning: str = ""
    examples: list[str] = Field(default_factory=list)
    safer_alternatives: list[str] = Field(default_factory=list)


class WorkflowCommandPolicyRuleListResponse(BaseModel):
    total: int
    rules: list[WorkflowCommandPolicyRule] = Field(default_factory=list)


class RuntimePermissionRequest(BaseModel):
    """真实 Agent Runtime 发起的权限请求。

    dry-run 目前只生成 `confirmation_required` 日志；后续真实执行器准备写文件、联网、
    执行 Shell 或调用插件前，需要先生成这个结构，并交给前端展示和用户确认。
    """

    request_id: str
    task_id: str
    step_id: str
    agent_id: str
    permissions: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimePermissionDecisionRecord(BaseModel):
    """用户或平台 Governance 对权限请求的决策记录。

    这个记录需要持久化到数据库，作为审计依据。模型和 Agent 不能自行创建 approved；
    只有用户决策接口或确定性的 Permission Policy 可以写入结果。
    """

    request_id: str
    task_id: str
    step_id: str
    decision: RuntimePermissionDecision = "pending"
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""


class RuntimePermissionItem(BaseModel):
    """权限请求和当前决策状态的组合视图。

    列表接口返回组合结构，前端不用再分别请求 request 和 decision；真实执行器仍只根据
    `decision.decision` 判断是否允许继续执行敏感步骤。
    """

    request: RuntimePermissionRequest
    decision: RuntimePermissionDecisionRecord
    created_at: str = ""
    updated_at: str = ""


class RuntimePermissionListResponse(BaseModel):
    task_id: str
    total: int
    permissions: list[RuntimePermissionItem] = Field(default_factory=list)


class RuntimePermissionDecisionInput(BaseModel):
    """前端提交的权限决策。

    request_id、task_id、step_id 由路径和数据库记录决定，不允许客户端在 body 里覆盖，
    避免把一个步骤的授权误写到另一个任务上。
    """

    decision: RuntimePermissionDecisionInputValue
    decided_by: str = "local_user"
    note: str = ""
