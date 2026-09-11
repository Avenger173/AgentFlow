"""MEM-6 会话生命周期与无正文观测离线验收。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


backend_root = Path(__file__).resolve().parents[1]
work_dir = Path(tempfile.mkdtemp(prefix="agentflow_memory_mem6_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(work_dir)
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
sys.path.insert(0, str(backend_root))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def main() -> None:
    try:
        from fastapi.testclient import TestClient

        from app.database.conversation_repository import (
            create_conversation,
            get_conversation_context,
            save_conversation_turn,
            save_conversation_working_state,
        )
        from app.database.memory_repository import (
            create_long_term_memory,
            create_or_reuse_long_term_memory_proposal,
        )
        from app.database.sqlite import get_connection
        from app.schemas.conversation import ConversationWorkingState
        from app.services.conversation_lifecycle import run_configured_conversation_retention
        from main import app

        with TestClient(app) as client:
            initial_preferences = client.get("/api/settings/runtime-preferences")
            assert initial_preferences.status_code == 200
            assert initial_preferences.json()["conversation_retention_days"] == 0

            def create_populated_session(scope: str, suffix: str):
                session = create_conversation(project_scope=scope)
                save_conversation_turn(
                    conversation_id=session.conversation_id,
                    user_message=f"测试会话 {suffix}",
                    assistant_message="已保存。",
                    material_bindings=[],
                    task_id="",
                    plan_id="",
                )
                state = ConversationWorkingState(
                    conversation_id=session.conversation_id,
                    project_scope=scope,
                    revision=1,
                    updated_at=_iso(datetime.now(UTC)),
                )
                save_conversation_working_state(state=state, expected_revision=0)
                proposal = create_or_reuse_long_term_memory_proposal(
                    proposal_id=f"proposal_{suffix}",
                    task_id="",
                    kind="user_preference",
                    suggested_scope=scope,
                    title=f"候选 {suffix}",
                    summary="这是一条待确认的短事实。",
                    tags=["mem6"],
                    reason="验收夹具。",
                    source_type="explicit_user",
                    source_id=f"source_{suffix}",
                    source_conversation_id=session.conversation_id,
                    conflict_key=f"key_{suffix}",
                    fingerprint=f"fingerprint_{suffix}",
                )
                return session, proposal

            project_scope = "project:mem6"
            protected_scope = "project:mem6-other"
            session, proposal = create_populated_session(project_scope, "delete")
            other_session, _ = create_populated_session(protected_scope, "other")

            # 恢复、归档与删除都必须带 project scope；错误范围不能读取或删除目标会话。
            assert client.get(
                f"/api/chat/conversations/{session.conversation_id}",
                params={"project_scope": protected_scope},
            ).status_code == 404
            archived = client.post(
                f"/api/chat/conversations/{session.conversation_id}/archive",
                params={"project_scope": project_scope},
            )
            assert archived.status_code == 200
            assert archived.json()["archived_at"]
            assert get_conversation_context(session.conversation_id, project_scope=project_scope).session.archived_at
            rejected_delete = client.delete(
                f"/api/chat/conversations/{session.conversation_id}",
                params={"project_scope": protected_scope},
            )
            assert rejected_delete.status_code == 404
            assert get_conversation_context(session.conversation_id, project_scope=project_scope)

            deleted = client.delete(
                f"/api/chat/conversations/{session.conversation_id}",
                params={"project_scope": project_scope},
            )
            assert deleted.status_code == 200
            deleted_body = deleted.json()
            assert deleted_body["deleted_message_count"] == 2
            assert deleted_body["deleted_working_state_count"] == 1
            assert deleted_body["deleted_proposal_count"] == 1
            with get_connection() as connection:
                assert connection.execute(
                    "SELECT COUNT(*) FROM commander_conversations WHERE conversation_id = ?",
                    (session.conversation_id,),
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT COUNT(*) FROM commander_conversation_messages WHERE conversation_id = ?",
                    (session.conversation_id,),
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT COUNT(*) FROM commander_conversation_working_states WHERE conversation_id = ?",
                    (session.conversation_id,),
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT COUNT(*) FROM long_term_memory_proposals WHERE proposal_id = ?",
                    (proposal.proposal_id,),
                ).fetchone()[0] == 0
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert get_conversation_context(other_session.conversation_id, project_scope=protected_scope)

            retained_session, _ = create_populated_session(project_scope, "retained")
            reference = datetime(2030, 1, 31, tzinfo=UTC)
            with get_connection() as connection:
                connection.execute(
                    "UPDATE commander_conversations SET updated_at = ? WHERE conversation_id = ?",
                    (_iso(reference - timedelta(days=100)), retained_session.conversation_id),
                )
            disabled_maintenance = run_configured_conversation_retention(now=reference)
            assert not disabled_maintenance.enabled
            assert get_conversation_context(retained_session.conversation_id, project_scope=project_scope)

            old_session, _ = create_populated_session(project_scope, "old")
            boundary_session, _ = create_populated_session(project_scope, "boundary")
            cutoff = reference - timedelta(days=10)
            with get_connection() as connection:
                connection.execute(
                    "UPDATE commander_conversations SET updated_at = ? WHERE conversation_id = ?",
                    (_iso(cutoff - timedelta(microseconds=1)), old_session.conversation_id),
                )
                connection.execute(
                    "UPDATE commander_conversations SET updated_at = ? WHERE conversation_id = ?",
                    (_iso(cutoff), boundary_session.conversation_id),
                )
            updated_preferences = client.put(
                "/api/settings/runtime-preferences",
                json={
                    "permission_policy": "smart_confirm",
                    "personality": "professional",
                    "memory_enabled": True,
                    "conversation_retention_days": 10,
                },
            )
            assert updated_preferences.status_code == 200
            enabled_maintenance = run_configured_conversation_retention(now=reference)
            assert enabled_maintenance.enabled
            assert enabled_maintenance.deleted_conversation_count >= 2
            assert client.get(
                f"/api/chat/conversations/{old_session.conversation_id}",
                params={"project_scope": project_scope},
            ).status_code == 404
            assert get_conversation_context(boundary_session.conversation_id, project_scope=project_scope)

            # 观测记录允许 memory ID，但不能复制标题、正文、文件路径、凭据或 embedding。
            forbidden_title = "MEM6_PRIVATE_TITLE_DO_NOT_RECORD"
            create_long_term_memory(
                kind="user_preference",
                scope="global",
                title=forbidden_title,
                summary="测试用稳定短事实。",
                tags=["mem6"],
                source_task_id=None,
                user_confirmed=True,
            )
            chat = client.post("/api/chat", json={"message": "请使用 mem6 偏好继续。"})
            assert chat.status_code == 200, chat.text
            observations = client.get("/api/memories/observations")
            assert observations.status_code == 200
            observation_items = observations.json()["items"]
            event_types = {item["event_type"] for item in observation_items}
            assert {"context", "retrieval", "lifecycle"}.issubset(event_types)
            rendered_observations = json.dumps(observation_items, ensure_ascii=False)
            assert forbidden_title not in rendered_observations
            assert "C:\\\\" not in rendered_observations
            assert "embedding" not in rendered_observations.lower()
            with get_connection() as connection:
                columns = {
                    str(row[1]).lower()
                    for row in connection.execute("PRAGMA table_info(memory_observations)").fetchall()
                }
                assert not columns.intersection({"content", "title", "filename", "path", "credential", "embedding"})
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        print("MEM-6 conversation lifecycle and observability verification passed.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
