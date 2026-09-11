"""受配置驱动的会话保留期维护。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.database.conversation_repository import (
    ConversationRetentionCleanupResult,
    delete_expired_conversations,
)
from app.database.memory_observability_repository import record_memory_observation_safely
from app.services.runtime_preferences_store import load_runtime_preferences


@dataclass(frozen=True)
class ConversationRetentionMaintenanceResult:
    enabled: bool
    retention_days: int
    deleted_conversation_count: int = 0
    deleted_message_count: int = 0
    deleted_working_state_count: int = 0
    deleted_proposal_count: int = 0


def run_configured_conversation_retention(
    *,
    now: datetime | None = None,
) -> ConversationRetentionMaintenanceResult:
    """只有客户明确设置保留期后才删除，使用严格边界避免日期漂移。"""

    preferences = load_runtime_preferences()
    retention_days = preferences.conversation_retention_days
    if retention_days <= 0:
        return ConversationRetentionMaintenanceResult(enabled=False, retention_days=0)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = (reference.astimezone(UTC) - timedelta(days=retention_days)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    deleted = delete_expired_conversations(updated_before=cutoff)
    result = _to_maintenance_result(retention_days=retention_days, deleted=deleted)
    if result.deleted_conversation_count or result.deleted_proposal_count:
        record_memory_observation_safely(
            event_type="lifecycle",
            lifecycle_action="conversation_retention",
            affected_conversation_count=result.deleted_conversation_count,
            affected_proposal_count=result.deleted_proposal_count,
        )
    return result


def _to_maintenance_result(
    *,
    retention_days: int,
    deleted: ConversationRetentionCleanupResult,
) -> ConversationRetentionMaintenanceResult:
    return ConversationRetentionMaintenanceResult(
        enabled=True,
        retention_days=retention_days,
        deleted_conversation_count=deleted.deleted_conversation_count,
        deleted_message_count=deleted.deleted_message_count,
        deleted_working_state_count=deleted.deleted_working_state_count,
        deleted_proposal_count=deleted.deleted_proposal_count,
    )
