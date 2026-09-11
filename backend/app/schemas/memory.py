from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# 会话状态已经由任务、计划和事件表保存；本模块只处理跨任务仍有价值、且用户明确确认过的
# 长期事实。把二者分开可以避免把完整聊天记录误当成“记忆”无限累积。
LongTermMemoryKind = Literal["user_preference", "project_constraint", "experience"]
LongTermMemoryProposalStatus = Literal["pending", "confirmed", "rejected", "expired", "superseded"]
LongTermMemoryProposalSourceType = Literal[
    "explicit_user",
    "verified_project_constraint",
    "successful_task_experience",
]


class LongTermMemoryCreateRequest(BaseModel):
    """由用户明确确认后创建的一条长期记忆。

    summary 是经过压缩的事实，不允许提交整份文档、原始表格或完整对话。敏感内容和绝对路径
    还会由服务层二次筛查，Pydantic 的长度限制只负责保护 API 与数据库的基础边界。
    """

    kind: LongTermMemoryKind
    scope: str = Field(default="global", min_length=1, max_length=80)
    title: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=2, max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=8)
    source_task_id: str | None = Field(default=None, max_length=160)
    user_confirmed: bool = True


class LongTermMemoryUpdateRequest(BaseModel):
    """长期记忆的显式编辑请求。

    不提供的字段保持原值；enabled 只影响后续是否允许被检索，不会删除其审计来源。
    """

    title: str | None = Field(default=None, min_length=2, max_length=120)
    summary: str | None = Field(default=None, min_length=2, max_length=1000)
    tags: list[str] | None = Field(default=None, max_length=8)
    enabled: bool | None = None


class LongTermMemoryRecord(BaseModel):
    """返回给设置页、计划审计和未来记忆管理页的脱敏记录。"""

    memory_id: str
    kind: LongTermMemoryKind
    scope: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    source_task_id: str | None = None
    user_confirmed: bool = True
    enabled: bool = True
    created_at: str
    updated_at: str
    last_used_at: str = ""


class LongTermMemoryListResponse(BaseModel):
    items: list[LongTermMemoryRecord] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)


class LongTermMemoryClearResponse(BaseModel):
    scope: str
    deleted_count: int = Field(default=0, ge=0)


class LongTermMemoryProposal(BaseModel):
    """供客户复核的持久化长期记忆候选。

    候选不是记忆记录，也不意味着系统已经保存任何内容。它只保留经脱敏压缩的短事实、来源
    标识和状态关系；最终仍要由客户明确确认后才能写入长期表。
    """

    proposal_id: str
    task_id: str
    kind: LongTermMemoryKind
    title: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=2, max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=8)
    suggested_scope: str = Field(default="global", min_length=1, max_length=80)
    reason: str = Field(default="", max_length=500)
    requires_user_confirmation: bool = True
    status: LongTermMemoryProposalStatus = "pending"
    source_type: LongTermMemoryProposalSourceType = "explicit_user"
    source_id: str = Field(default="", max_length=180)
    source_conversation_id: str | None = Field(default=None, max_length=64)
    replaces_proposal_id: str | None = Field(default=None, max_length=120)
    replaced_by_proposal_id: str | None = Field(default=None, max_length=120)
    replaces_memory_id: str | None = Field(default=None, max_length=120)
    confirmed_memory_id: str | None = Field(default=None, max_length=120)
    created_at: str = ""
    updated_at: str = ""


class LongTermMemoryProposalListResponse(BaseModel):
    task_id: str | None = None
    scope: str | None = None
    items: list[LongTermMemoryProposal] = Field(default_factory=list, max_length=200)
    note: str = Field(default="", max_length=500)


class LongTermMemoryProposalConfirmRequest(BaseModel):
    """客户在确认候选时可以精简、改名、换范围，但不能伪造来源任务。"""

    proposal_id: str = Field(min_length=8, max_length=120)
    kind: LongTermMemoryKind
    scope: str = Field(default="global", min_length=1, max_length=80)
    title: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=2, max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=8)
    user_confirmed: bool = False


class LongTermMemoryProposalRejectRequest(BaseModel):
    """拒绝待确认候选需要显式确认；拒绝操作可安全重试。"""

    user_rejected: bool = False


class MemoryObservationRecord(BaseModel):
    """MEM-6 无正文观测记录，供本地诊断与验收使用。"""

    observation_id: str
    event_type: Literal["context", "retrieval", "lifecycle"]
    observed_at: str
    context_budget_tokens: int = Field(default=0, ge=0)
    context_estimated_tokens: int = Field(default=0, ge=0)
    compaction_count: int = Field(default=0, ge=0)
    summary_message_count: int = Field(default=0, ge=0)
    retrieval_mode: str = Field(default="", max_length=80)
    candidate_count: int = Field(default=0, ge=0)
    recalled_memory_ids: list[str] = Field(default_factory=list, max_length=8)
    retrieval_latency_ms: int = Field(default=0, ge=0)
    fallback_reason: str = Field(default="", max_length=200)
    lifecycle_action: str = Field(default="", max_length=80)
    affected_conversation_count: int = Field(default=0, ge=0)
    affected_proposal_count: int = Field(default=0, ge=0)


class MemoryObservationListResponse(BaseModel):
    items: list[MemoryObservationRecord] = Field(default_factory=list, max_length=200)
