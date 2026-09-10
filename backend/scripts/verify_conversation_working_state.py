"""验证会话工作状态的前向迁移、恢复快照和幂等写入。

脚本只创建临时 SQLite，并在其中构造一份已应用旧 migration 的会话数据；不读取开发数据库、
客户材料、模型配置或网络。它覆盖 MEM-2 最容易被忽略的升级路径，而不仅是新库首启。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_working_state_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.database import sqlite as sqlite_database  # noqa: E402
from app.database.conversation_repository import (  # noqa: E402
    create_conversation,
    get_conversation_context,
    get_conversation_working_state,
)
from app.database.sqlite import get_connection  # noqa: E402
from app.database.task_repository import save_workflow_run  # noqa: E402
from app.schemas.chat import WorkflowPlan, WorkflowStep  # noqa: E402
from app.schemas.workflow import WorkflowArtifact, WorkflowRun, WorkflowStepRun  # noqa: E402
from app.services.conversation_working_state import record_successful_user_message  # noqa: E402


CONVERSATION_ID = "conv_legacy_eval_01"
PROJECT_SCOPE = "project:legacy_eval"


def main() -> None:
    try:
        _prepare_old_database_shape()
        _run_forward_migration()
        _assert_legacy_data_and_state_contract()
        _assert_runtime_checkpoint_projection()
        print("Conversation working-state migration verification passed.")
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)


def _prepare_old_database_shape() -> None:
    # 首先让当前版本创建完整的历史表集合；随后仅回退这一次新增 migration，精确模拟旧客户端
    # 已经写入数据但尚未升级到 MEM-2 的数据库状态。
    with get_connection():
        pass
    database_path = settings.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP INDEX idx_commander_conversation_working_states_scope_updated")
        connection.execute("DROP TABLE commander_conversation_working_states")
        connection.execute(
            "DELETE FROM schema_migrations WHERE migration_id = ?",
            ("20260910_commander_conversation_working_state_v1",),
        )
        connection.execute(
            """
            INSERT INTO commander_conversations (
                conversation_id, project_scope, title, summary, summary_message_count,
                material_bindings_json, last_task_id, last_plan_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                CONVERSATION_ID,
                PROJECT_SCOPE,
                "旧会话标题",
                "[目标] 保留旧会话摘要。",
                1,
                "[]",
                "task_legacy_eval_01",
                "plan_legacy_eval_01",
                "2026-09-09T00:00:00Z",
                "2026-09-09T01:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO commander_conversation_messages (
                message_id, conversation_id, role, content, task_id, created_at
            ) VALUES (?, ?, 'user', ?, ?, ?)
            """,
            (
                "turn_legacy_eval_01",
                CONVERSATION_ID,
                "旧会话消息仍必须保留。",
                "task_legacy_eval_01",
                "2026-09-09T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO long_term_memories (
                memory_id, kind, scope, title, summary, tags_json, source_task_id,
                user_confirmed, enabled, created_at, updated_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, '')
            """,
            (
                "memory_legacy_eval_01",
                "project_constraint",
                PROJECT_SCOPE,
                "旧长期记忆",
                "长期记忆记录不能因会话状态迁移丢失。",
                "[]",
                "task_legacy_eval_01",
                "2026-09-09T00:00:00Z",
                "2026-09-09T00:00:00Z",
            ),
        )
        connection.commit()
    sqlite_database._INITIALIZED_PATHS.discard(database_path)


def _run_forward_migration() -> None:
    with get_connection() as connection:
        migration = connection.execute(
            "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
            ("20260910_commander_conversation_working_state_v1",),
        ).fetchone()
        assert migration is not None, "未应用 ConversationWorkingState 前向 migration。"


def _assert_legacy_data_and_state_contract() -> None:
    context = get_conversation_context(CONVERSATION_ID)
    assert context.session.summary == "[目标] 保留旧会话摘要。"
    assert context.session.last_task_id == "task_legacy_eval_01"
    assert context.session.last_plan_id == "plan_legacy_eval_01"
    assert [message.content for message in context.recent_messages] == ["旧会话消息仍必须保留。"]
    assert context.working_state is not None and context.working_state.revision == 0

    with get_connection() as connection:
        memory_count = connection.execute("SELECT COUNT(*) FROM long_term_memories").fetchone()[0]
        state_count = connection.execute(
            "SELECT COUNT(*) FROM commander_conversation_working_states WHERE conversation_id = ?",
            (CONVERSATION_ID,),
        ).fetchone()[0]
    assert memory_count == 1, "迁移不应影响长期记忆。"
    assert state_count == 0, "旧会话应在首次可信状态写入前保持惰性空快照。"

    first = record_successful_user_message(
        conversation_id=CONVERSATION_ID,
        project_scope=PROJECT_SCOPE,
        message="预算改为 3000",
        task_id="task_legacy_eval_02",
    )
    repeated = record_successful_user_message(
        conversation_id=CONVERSATION_ID,
        project_scope=PROJECT_SCOPE,
        message="预算改为 3000",
        task_id="task_legacy_eval_02",
    )
    restored = get_conversation_working_state(
        conversation_id=CONVERSATION_ID,
        project_scope=PROJECT_SCOPE,
    )
    assert first.revision == 1
    assert repeated.revision == 1, "重复事件不应制造新的工作状态 revision。"
    assert restored == first
    assert restored.constraints["budget"].value == "3000"


def _assert_runtime_checkpoint_projection() -> None:
    session = create_conversation(project_scope="project:runtime_eval")
    task_id = "task_runtime_eval_01"
    plan = WorkflowPlan(
        workflow_name="runtime_eval",
        description="合成 Runtime 投影验证。",
        user_goal="生成验证报告",
        project_scope=session.project_scope,
        conversation_id=session.conversation_id,
        steps=[
            WorkflowStep(
                id="step_export",
                agent="commander_agent",
                action="export",
                title="导出验证报告",
            )
        ],
    )
    running = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="running",
        summary="正在导出验证报告。",
        steps=[
            WorkflowStepRun(
                step_id="step_export",
                agent="commander_agent",
                action="export",
                status="running",
                message="正在写入受控产物。",
            )
        ],
    )
    save_workflow_run(run=running, events=[], plan=plan, artifacts=[])
    running_state = get_conversation_working_state(
        conversation_id=session.conversation_id,
        project_scope=session.project_scope,
    )
    assert running_state.active_task is not None
    assert running_state.active_task.status == "running"
    assert running_state.active_task.current_step == "export"
    assert running_state.latest_verified_result is None

    artifact = WorkflowArtifact(
        artifact_id="artifact_runtime_eval_01",
        task_id=task_id,
        step_id="step_export",
        agent_id="commander_agent",
        kind="report",
        name="验证报告",
        summary="已通过合成回读。",
        uri="artifact://runtime-eval/report",
    )
    completed = running.model_copy(
        update={
            "status": "completed",
            "summary": "验证报告已通过回读。",
            "steps": [
                WorkflowStepRun(
                    step_id="step_export",
                    agent="commander_agent",
                    action="export",
                    status="completed",
                    message="产物已验证。",
                    output={"artifact_id": artifact.artifact_id},
                )
            ],
        }
    )
    save_workflow_run(run=completed, events=[], plan=plan, artifacts=[artifact])
    completed_state = get_conversation_working_state(
        conversation_id=session.conversation_id,
        project_scope=session.project_scope,
    )
    assert completed_state.active_task is not None
    assert completed_state.active_task.status == "completed"
    assert completed_state.latest_verified_result is not None
    assert completed_state.latest_verified_result.artifact_id == artifact.artifact_id
    assert any(item.task_id == task_id and item.status == "completed" for item in completed_state.open_items)

    save_workflow_run(run=completed, events=[], plan=plan, artifacts=[artifact])
    repeated_state = get_conversation_working_state(
        conversation_id=session.conversation_id,
        project_scope=session.project_scope,
    )
    assert repeated_state.revision == completed_state.revision
    assert repeated_state.latest_verified_result == completed_state.latest_verified_result
    with get_connection() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


if __name__ == "__main__":
    main()
