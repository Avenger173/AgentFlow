"""知识库 K0.3 的纯状态守卫。

它不访问 SQLite、Chroma、文件或模型。K1 的 Repository/Indexer 需要在真正写入前调用这些
规则，让“候选索引构建”和“活动索引切换”始终遵循同一套契约，而不是由各层各自猜测。
"""

from __future__ import annotations

from app.schemas.knowledge import (
    KnowledgeIndexGenerationRecord,
    KnowledgeIndexJobStatus,
)


class KnowledgeContractError(ValueError):
    """调用方尝试违反知识库版本或任务状态契约。"""


# 索引任务是单向运行记录：重新索引应新建 job/generation，不能把已经终止的 job 倒回运行，
# 否则历史任务和失败证据会失去可复盘性。
_INDEX_JOB_TRANSITIONS: dict[KnowledgeIndexJobStatus, frozenset[KnowledgeIndexJobStatus]] = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"completed", "partial_failure", "failed", "cancelled"}),
    "completed": frozenset(),
    "partial_failure": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def assert_index_job_transition(
    current_status: KnowledgeIndexJobStatus,
    next_status: KnowledgeIndexJobStatus,
) -> None:
    """验证一次状态迁移；同状态持久化允许用于幂等的阶段事件补写。"""

    if current_status == next_status:
        return
    if next_status not in _INDEX_JOB_TRANSITIONS[current_status]:
        raise KnowledgeContractError(
            f"知识库索引任务不允许从 {current_status} 迁移到 {next_status}。"
        )


def assert_generation_can_activate(generation: KnowledgeIndexGenerationRecord) -> None:
    """只允许完整验证后的 immutable generation 成为资料库活动快照。"""

    if generation.status != "ready":
        raise KnowledgeContractError("未就绪的索引代次不能切换为活动版本。")
    if not generation.document_version_ids or not generation.activated_at:
        raise KnowledgeContractError("活动索引代次缺少文档版本快照或激活时间。")

