from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.chat import WorkflowMaterialBinding


ConversationRole = Literal["user", "assistant"]


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
    content: str = Field(min_length=1, max_length=2200)
    task_id: str = Field(default="", max_length=160)
    created_at: str


class ConversationContext(BaseModel):
    """一次新请求开始前给 Commander 的有限会话快照。"""

    session: ConversationSessionRecord
    recent_messages: list[ConversationMessageRecord] = Field(default_factory=list, max_length=8)


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
