from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.chat import WorkflowMaterialBinding


ConversationRole = Literal["user", "assistant"]
ConversationWorkingStateSource = Literal["user_message", "workflow_plan", "workflow_run"]
ConversationOpenItemStatus = Literal[
    "open",
    "pending_confirmation",
    "completed",
    "failed",
    "blocked",
    "cancelled",
]


class ConversationWorkingStateValue(BaseModel):
    """一个已解析字段的受控值及其可信来源。

    会话摘要仍然服务于自然语言连续性；这里专门保存目标、约束和决策等可被后续流程稳定
    读取的状态。来源不接受助手自然语言，避免“已经完成”之类的话术直接改变任务事实。
    """

    value: Any
    source: ConversationWorkingStateSource
    source_id: str = Field(default="", max_length=160)
    updated_at: str = ""


class ConversationOpenItem(BaseModel):
    """会话中尚需处理或已由 Runtime 终结的工作项。"""

    item_id: str = Field(min_length=1, max_length=180)
    title: str = Field(min_length=1, max_length=240)
    status: ConversationOpenItemStatus = "open"
    task_id: str = Field(default="", max_length=160)
    source: ConversationWorkingStateSource = "user_message"
    source_id: str = Field(default="", max_length=160)
    updated_at: str = ""


class ConversationActiveTask(BaseModel):
    """当前会话可恢复的任务指针；它不是 WorkflowRun 的副本。"""

    task_id: str = Field(min_length=1, max_length=160)
    plan_id: str = Field(default="", max_length=160)
    status: str = Field(default="queued", max_length=48)
    current_step: str = Field(default="", max_length=180)
    step_index: int | None = Field(default=None, ge=1)
    next_action: str = Field(default="", max_length=180)
    resume_checkpoint: str = Field(default="", max_length=160)
    updated_at: str = ""


class ConversationVerifiedResult(BaseModel):
    """只记录已由 Runtime 验证的最新交付索引，不保存交付正文。"""

    task_id: str = Field(min_length=1, max_length=160)
    artifact_id: str = Field(default="", max_length=160)
    artifact_ids: list[str] = Field(default_factory=list, max_length=16)
    summary: str = Field(default="", max_length=600)
    verified_at: str = ""


class ConversationWorkingState(BaseModel):
    """会话的结构化工作状态快照。

    ``revision`` 只在语义状态发生变化时增加，``applied_event_ids`` 保持有界，用来让 Runtime
    checkpoint 的重复写入可幂等。该状态严格限定在一个 project scope 内，不能替代长期记忆。
    """

    conversation_id: str = Field(min_length=8, max_length=64)
    project_scope: str = Field(min_length=1, max_length=80)
    revision: int = Field(default=0, ge=0)
    current_goal: ConversationWorkingStateValue | None = None
    constraints: dict[str, ConversationWorkingStateValue] = Field(default_factory=dict)
    decisions: dict[str, ConversationWorkingStateValue] = Field(default_factory=dict)
    open_items: list[ConversationOpenItem] = Field(default_factory=list, max_length=32)
    active_task: ConversationActiveTask | None = None
    latest_verified_result: ConversationVerifiedResult | None = None
    pending_confirmation: list[str] = Field(default_factory=list, max_length=16)
    applied_event_ids: list[str] = Field(default_factory=list, max_length=64)
    updated_at: str = ""


class ConversationSessionRecord(BaseModel):
    """一段调度会话的可持久化、非正文状态。

    会话用于自动维持同一客户对话的有限上下文；它不是跨会话长期记忆，也不是任务历史的
    替代品。材料只保存经 Commander 规范化的相对引用或资料库 ID，不能保存绝对路径、文件
    正文、表格行、API Key 或模型隐藏推理。
    """

    conversation_id: str = Field(min_length=8, max_length=64)
    project_scope: str = Field(min_length=1, max_length=80)
    title: str = Field(default="", max_length=96)
    summary: str = Field(default="", max_length=1400)
    material_bindings: list[WorkflowMaterialBinding] = Field(default_factory=list, max_length=8)
    last_task_id: str = Field(default="", max_length=160)
    last_plan_id: str = Field(default="", max_length=160)
    # 这是客户可回看的脱敏消息总数，不代表会被模型完整读取。
    archived_message_count: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str


class ConversationMessageRecord(BaseModel):
    """会话归档中的一条脱敏消息。

    入库前由会话服务去除明显凭据与本机绝对路径并截断；表结构保留 role 与 task_id，方便
    UI 按页恢复客户实际看过的聊天记录，不把任务事件正文复制进会话档案。
    """

    message_id: str = Field(min_length=8, max_length=80)
    conversation_id: str = Field(min_length=8, max_length=64)
    role: ConversationRole
    content: str = Field(min_length=1, max_length=8000)
    task_id: str = Field(default="", max_length=160)
    created_at: str


class ConversationContext(BaseModel):
    """一次新请求开始前给 Commander 的有限会话快照与压缩水位。"""

    session: ConversationSessionRecord
    recent_messages: list[ConversationMessageRecord] = Field(default_factory=list, max_length=20)
    summarized_message_count: int = Field(default=0, ge=0)
    estimated_memory_tokens: int = Field(default=0, ge=0)
    # 所有消费者（恢复 API、Prompt、计划审计）复用同一份状态快照，避免各自猜测任务进度。
    working_state: ConversationWorkingState | None = None


class ConversationSessionList(BaseModel):
    """同一项目范围内可切换的会话元数据，不携带聊天正文。"""

    project_scope: str = Field(min_length=1, max_length=80)
    conversations: list[ConversationSessionRecord] = Field(default_factory=list, max_length=80)


class ConversationTranscriptPage(BaseModel):
    """客户按页读取的完整会话归档。"""

    session: ConversationSessionRecord
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    messages: list[ConversationMessageRecord] = Field(default_factory=list, max_length=100)
