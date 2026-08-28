"""总指挥 C3 任务后记忆候选的离线回归。

该脚本只使用临时 SQLite 和 mock 规划，不读取用户真实记忆、不请求任何模型，也不会在项目
目录生成业务产物。它验证候选绝不自动保存、只接受完成 Runtime、确认后保留项目范围与来源。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


backend_root = Path(__file__).resolve().parents[1]
work_dir = Path(tempfile.mkdtemp(prefix="agentflow_memory_proposals_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(work_dir)
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
sys.path.insert(0, str(backend_root))


def _save_completed_runtime(*, task_id: str, user_goal: str, project_scope: str) -> None:
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
        artifacts=[],
        tool_calls=[],
    )


def main() -> None:
    try:
        from fastapi.testclient import TestClient

        from main import app

        client = TestClient(app)
        _save_completed_runtime(
            task_id="task_memory_proposal_done",
            user_goal="这个项目以后统一输出 Markdown 交付，并保持简洁的说明风格。",
            project_scope="project:demo",
        )

        proposals = client.get("/api/tasks/task_memory_proposal_done/memory-proposals")
        assert proposals.status_code == 200, proposals.text
        payload = proposals.json()
        assert len(payload["items"]) == 1, payload
        proposal = payload["items"][0]
        assert proposal["suggested_scope"] == "project:demo", proposal
        assert proposal["requires_user_confirmation"] is True, proposal
        assert client.get("/api/memories?scope=project:demo").json()["total"] == 0

        rejected_confirm = client.post(
            "/api/tasks/task_memory_proposal_done/memory-proposals/confirm",
            json={
                "proposal_id": proposal["proposal_id"],
                "kind": proposal["kind"],
                "scope": proposal["suggested_scope"],
                "title": proposal["title"],
                "summary": proposal["summary"],
                "tags": proposal["tags"],
                "user_confirmed": False,
            },
        )
        assert rejected_confirm.status_code == 400, rejected_confirm.text

        confirmed = client.post(
            "/api/tasks/task_memory_proposal_done/memory-proposals/confirm",
            json={
                "proposal_id": proposal["proposal_id"],
                "kind": proposal["kind"],
                "scope": proposal["suggested_scope"],
                "title": "项目交付格式",
                "summary": "这个项目以后统一输出 Markdown 交付，并保持简洁的说明风格。",
                "tags": ["project", "markdown"],
                "user_confirmed": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        memory = confirmed.json()
        assert memory["scope"] == "project:demo", memory
        assert memory["source_task_id"] == "task_memory_proposal_done", memory

        # 同一确认请求重试必须返回同一条记录，而不是插入重复记忆。
        repeated = client.post(
            "/api/tasks/task_memory_proposal_done/memory-proposals/confirm",
            json={
                "proposal_id": proposal["proposal_id"],
                "kind": proposal["kind"],
                "scope": proposal["suggested_scope"],
                "title": "项目交付格式",
                "summary": "这个项目以后统一输出 Markdown 交付，并保持简洁的说明风格。",
                "tags": ["project", "markdown"],
                "user_confirmed": True,
            },
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["memory_id"] == memory["memory_id"], repeated.text
        assert client.get("/api/memories?scope=project:demo").json()["total"] == 1

        _save_completed_runtime(
            task_id="task_memory_proposal_one_off",
            user_goal="请整理这份材料，并生成一份本次会议纪要。",
            project_scope="global",
        )
        one_off = client.get("/api/tasks/task_memory_proposal_one_off/memory-proposals")
        assert one_off.status_code == 200, one_off.text
        assert one_off.json()["items"] == [], one_off.text

        # 普通 dry-run 不能把“计划生成完”误认成任务完成。
        dry_run = client.post(
            "/api/chat",
            json={
                "message": "以后请在项目中统一使用简洁格式。",
                "project_scope": "project:demo",
            },
        )
        assert dry_run.status_code == 200, dry_run.text
        not_completed = client.get(f"/api/tasks/{dry_run.json()['task_id']}/memory-proposals")
        assert not_completed.status_code == 409, not_completed.text

        invalid_scope = client.post(
            "/api/chat",
            json={"message": "测试范围", "project_scope": "D:\\private"},
        )
        assert invalid_scope.status_code == 400, invalid_scope.text

        print("Commander C3 memory proposal verification passed.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
