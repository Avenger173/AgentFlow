"""MEM-4 长期记忆候选生命周期离线验收。

所有夹具使用临时 SQLite 与 mock Commander，不调用真实模型、网络或客户材料。它覆盖持久候选
账本、确认幂等、同键替代、拒绝、压缩前提取及秘密/路径/长段落边界。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


backend_root = Path(__file__).resolve().parents[1]
work_dir = Path(tempfile.mkdtemp(prefix="agentflow_memory_lifecycle_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(work_dir)
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
sys.path.insert(0, str(backend_root))


def _save_completed_runtime(
    *,
    task_id: str,
    user_goal: str,
    project_scope: str,
    artifacts: list[object] | None = None,
) -> None:
    from app.database.task_repository import save_workflow_run
    from app.services.agent_catalog import list_agents
    from app.services.commander import create_commander_plan
    from app.workflow.dry_run import run_workflow_dry_run

    agents = list_agents()
    plan = create_commander_plan(
        user_goal,
        available_agents=agents,
        project_scope=project_scope,
    )
    dry_run = run_workflow_dry_run(task_id=task_id, plan=plan, available_agents=agents)
    completed_runtime = dry_run.model_copy(
        update={
            "mode": "runtime",
            "status": "completed",
            "summary": "离线夹具中的已完成总指挥任务。",
        }
    )
    save_workflow_run(
        run=completed_runtime,
        events=[],
        plan=plan,
        permission_requests=[],
        artifacts=artifacts or [],
        tool_calls=[],
    )


def _confirmation_payload(proposal: dict[str, object]) -> dict[str, object]:
    return {
        "proposal_id": proposal["proposal_id"],
        "kind": proposal["kind"],
        "scope": proposal["suggested_scope"],
        "title": proposal["title"],
        "summary": proposal["summary"],
        "tags": proposal["tags"],
        "user_confirmed": True,
    }


def main() -> None:
    try:
        from fastapi.testclient import TestClient

        from app.database.memory_repository import (
            get_long_term_memory_proposal,
            list_long_term_memory_proposals,
            search_long_term_memories,
        )
        from app.database.sqlite import _apply_long_term_memory_candidate_lifecycle_v1, get_connection
        from app.schemas.workflow import WorkflowArtifact
        from app.services.commander_memory_proposals import prepare_pre_compaction_memory_proposals
        from app.services.conversation_memory import (
            persist_successful_conversation_turn,
            prepare_conversation,
        )
        from main import app

        client = TestClient(app)

        # 从 MEM-4 之前已含正式记忆的旧表前向升级，不能只验证空库首次建表。
        legacy_path = work_dir / "legacy_memory_before_mem4.db"
        with sqlite3.connect(legacy_path) as legacy_connection:
            legacy_connection.row_factory = sqlite3.Row
            legacy_connection.execute(
                """
                CREATE TABLE long_term_memories (
                    memory_id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    title TEXT NOT NULL, summary TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                    source_task_id TEXT NOT NULL DEFAULT '', user_confirmed INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, last_used_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            legacy_connection.execute(
                """
                INSERT INTO long_term_memories (
                    memory_id, kind, scope, title, summary, created_at, updated_at
                ) VALUES ('memory_legacy', 'user_preference', 'global', '旧偏好', '旧库记录保留。', '2026-09-10Z', '2026-09-10Z')
                """
            )
            _apply_long_term_memory_candidate_lifecycle_v1(legacy_connection)
            legacy_columns = {
                str(row["name"])
                for row in legacy_connection.execute("PRAGMA table_info(long_term_memories)").fetchall()
            }
            assert {"memory_key", "replaced_by_memory_id"}.issubset(legacy_columns), legacy_columns
            assert legacy_connection.execute(
                "SELECT summary FROM long_term_memories WHERE memory_id = 'memory_legacy'"
            ).fetchone()[0] == "旧库记录保留。"
            assert legacy_connection.execute("PRAGMA foreign_key_check").fetchall() == []

        # 已完成任务立即持久化候选，但候选未确认前正式长期表必须保持为空。
        _save_completed_runtime(
            task_id="task_memory_lifecycle_markdown",
            user_goal="以后每次统一输出 Markdown 格式。",
            project_scope="project:lifecycle",
        )
        first_list = client.get("/api/tasks/task_memory_lifecycle_markdown/memory-proposals")
        assert first_list.status_code == 200, first_list.text
        first_payload = first_list.json()
        assert len(first_payload["items"]) == 1, first_payload
        first = first_payload["items"][0]
        assert first["status"] == "pending", first
        assert first["source_type"] == "verified_project_constraint", first
        assert client.get("/api/memories?scope=project:lifecycle").json()["total"] == 0

        confirmed = client.post(
            "/api/tasks/task_memory_lifecycle_markdown/memory-proposals/confirm",
            json=_confirmation_payload(first),
        )
        assert confirmed.status_code == 200, confirmed.text
        first_memory = confirmed.json()
        repeated = client.post(
            "/api/tasks/task_memory_lifecycle_markdown/memory-proposals/confirm",
            json=_confirmation_payload(first),
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["memory_id"] == first_memory["memory_id"], repeated.text
        assert get_long_term_memory_proposal(first["proposal_id"]).status == "confirmed"

        # 同键的新偏好先作为候选，确认后停用旧值；检索不能返回冲突旧值。
        _save_completed_runtime(
            task_id="task_memory_lifecycle_pdf",
            user_goal="以后每次统一输出 PDF 格式。",
            project_scope="project:lifecycle",
        )
        second_list = client.get("/api/tasks/task_memory_lifecycle_pdf/memory-proposals")
        assert second_list.status_code == 200, second_list.text
        second = second_list.json()["items"][0]
        assert second["replaces_memory_id"] == first_memory["memory_id"], second
        second_confirmed = client.post(
            "/api/tasks/task_memory_lifecycle_pdf/memory-proposals/confirm",
            json=_confirmation_payload(second),
        )
        assert second_confirmed.status_code == 200, second_confirmed.text
        second_memory = second_confirmed.json()
        active = client.get("/api/memories?scope=project:lifecycle&include_disabled=false")
        assert active.status_code == 200, active.text
        assert [item["memory_id"] for item in active.json()["items"]] == [second_memory["memory_id"]]
        recalled = search_long_term_memories(
            query="以后每次输出格式",
            scopes={"project:lifecycle"},
        )
        assert [item.memory_id for item in recalled] == [second_memory["memory_id"]], recalled

        # 一次性任务没有候选，明确拒绝的候选不进入正式表，也不会因 Runtime 恢复再次出现。
        _save_completed_runtime(
            task_id="task_memory_lifecycle_one_off",
            user_goal="请整理这份材料，并生成一份本次会议纪要。",
            project_scope="project:lifecycle",
        )
        one_off = client.get("/api/tasks/task_memory_lifecycle_one_off/memory-proposals")
        assert one_off.status_code == 200 and one_off.json()["items"] == [], one_off.text

        experience_artifact = WorkflowArtifact(
            artifact_id="artifact_memory_experience",
            task_id="task_memory_lifecycle_experience",
            step_id="step_memory_experience",
            agent_id="commander_agent",
            kind="report",
            name="验证产物",
            summary="已回读验证。",
            uri="artifact://memory-experience",
        )
        _save_completed_runtime(
            task_id="task_memory_lifecycle_experience",
            user_goal="以后每次都把这次成功的流程沉淀为可复用经验。",
            project_scope="project:experience",
            artifacts=[experience_artifact],
        )
        experience = client.get("/api/tasks/task_memory_lifecycle_experience/memory-proposals")
        assert experience.status_code == 200, experience.text
        assert experience.json()["items"][0]["source_type"] == "successful_task_experience", experience.text

        _save_completed_runtime(
            task_id="task_memory_lifecycle_reject",
            user_goal="以后每次都使用中文回复。",
            project_scope="project:reject",
        )
        rejection_list = client.get("/api/tasks/task_memory_lifecycle_reject/memory-proposals")
        assert rejection_list.status_code == 200, rejection_list.text
        rejection = rejection_list.json()["items"][0]
        rejected = client.post(
            f"/api/tasks/task_memory_lifecycle_reject/memory-proposals/{rejection['proposal_id']}/reject",
            json={"user_rejected": True},
        )
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected", rejected.text
        rejected_retry = client.post(
            f"/api/tasks/task_memory_lifecycle_reject/memory-proposals/{rejection['proposal_id']}/reject",
            json={"user_rejected": True},
        )
        assert rejected_retry.status_code == 200 and rejected_retry.json()["status"] == "rejected", rejected_retry.text
        _save_completed_runtime(
            task_id="task_memory_lifecycle_reject",
            user_goal="以后每次都使用中文回复。",
            project_scope="project:reject",
        )
        assert len(
            list_long_term_memory_proposals(
                task_id="task_memory_lifecycle_reject",
                statuses={"pending", "rejected", "confirmed", "superseded", "expired"},
            )
        ) == 1

        # 普通聊天先提取、后归档；最后一条明确偏好即使触发 compaction，也只产生一个候选。
        prepared = prepare_conversation(
            conversation_id=None,
            project_scope="project:compaction",
            message="开始一段连续会话。",
            supplied_materials=[],
        )
        for index in range(12):
            persist_successful_conversation_turn(
                prepared=prepared,
                user_message=f"本轮只处理一次性说明 {index}，不保存为长期偏好。" + "内容" * 100,
                assistant_message="已处理本轮一次性说明。",
                material_bindings=[],
                task_id=f"task_memory_compact_{index:02d}",
                plan_id=f"plan_memory_compact_{index:02d}",
            )
        compacted = persist_successful_conversation_turn(
            prepared=prepared,
            user_message="以后每次都用简洁风格说明结果。",
            assistant_message="已记录本轮可确认的候选。",
            material_bindings=[],
            task_id="task_memory_compact_durable",
            plan_id="plan_memory_compact_durable",
        )
        assert compacted.session.summary, compacted.session
        persist_successful_conversation_turn(
            prepared=prepared,
            user_message="以后每次都用简洁风格说明结果。",
            assistant_message="重复表达不会生成第二条候选。",
            material_bindings=[],
            task_id="task_memory_compact_retry",
            plan_id="plan_memory_compact_retry",
        )
        compact_candidates = list_long_term_memory_proposals(
            scope="project:compaction",
            statuses={"pending"},
        )
        assert len(compact_candidates) == 1, compact_candidates
        assert compact_candidates[0].source_type == "explicit_user", compact_candidates[0]
        generic_list = client.get("/api/memories/proposals?scope=project:compaction")
        assert generic_list.status_code == 200 and len(generic_list.json()["items"]) == 1, generic_list.text
        generic_proposal = generic_list.json()["items"][0]
        generic_confirmed = client.post(
            f"/api/memories/proposals/{generic_proposal['proposal_id']}/confirm",
            json=_confirmation_payload(generic_proposal),
        )
        assert generic_confirmed.status_code == 200, generic_confirmed.text
        persist_successful_conversation_turn(
            prepared=prepared,
            user_message="以后每次都使用中文回复。",
            assistant_message="已生成另一条待确认候选。",
            material_bindings=[],
            task_id="task_memory_compact_reject",
            plan_id="plan_memory_compact_reject",
        )
        generic_rejection_list = client.get("/api/memories/proposals?scope=project:compaction")
        assert generic_rejection_list.status_code == 200 and len(generic_rejection_list.json()["items"]) == 1
        generic_rejection = generic_rejection_list.json()["items"][0]
        generic_rejected = client.post(
            f"/api/memories/proposals/{generic_rejection['proposal_id']}/reject",
            json={"user_rejected": True},
        )
        assert generic_rejected.status_code == 200 and generic_rejected.json()["status"] == "rejected", generic_rejected.text

        # 密钥、绝对路径和过长原文不能进入候选账本，即使句子包含长期信号。
        forbidden_inputs = (
            "以后每次使用 sk-abcdefghijklmnopqrstuvwxyz123456 作为密钥。",
            "以后每次读取 D:\\private\\secret.txt。",
            "以后每次" + "请保留这一整段原文。" * 80,
        )
        for text in forbidden_inputs:
            drafts = prepare_pre_compaction_memory_proposals(
                task_id="task_memory_forbidden",
                project_scope="project:compaction",
                conversation_id=prepared.context.session.conversation_id,
                user_message=text,
            )
            assert drafts == [], (text, drafts)

        with get_connection() as connection:
            migration = connection.execute(
                "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
                ("20260910_long_term_memory_candidate_lifecycle_v1",),
            ).fetchone()
            assert migration is not None
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        print("Commander MEM-4 memory lifecycle verification passed.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
