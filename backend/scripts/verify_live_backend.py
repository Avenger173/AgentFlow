import asyncio
import json
import sys

import httpx
import websockets


HTTP_BASE_URL = "http://127.0.0.1:8765"
WS_BASE_URL = "ws://127.0.0.1:8765"


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    message = "帮我根据这个作业要求生成 Python 代码和 README 报告。"

    with httpx.Client(base_url=HTTP_BASE_URL, timeout=5.0) as client:
        health = client.get("/health")
        health.raise_for_status()
        assert health.json()["status"] == "ok"

        agents = client.get("/api/agents")
        agents.raise_for_status()
        agents_payload = agents.json()
        assert agents_payload["agents"][0]["name"] == "总指挥"
        assert agents_payload["agents"][0]["source"] == "builtin"

        registry = client.get("/api/agents/registry/status")
        registry.raise_for_status()
        registry_payload = registry.json()
        assert registry_payload["loaded_total"] >= 4
        assert registry_payload["errors"] == []

        chat = client.post("/api/chat", json={"message": message})
        chat.raise_for_status()
        chat_payload = chat.json()
        assert chat_payload["mode"] in {"mock", "llm"}
        assert chat_payload["reply"]
        task_id = chat_payload["task_id"]
        workflow_plan = chat_payload["workflow_plan"]
        assert workflow_plan["version"] == "1.0"
        assert workflow_plan["workflow_name"] == "commander_initial_plan"
        assert workflow_plan["summary"]
        assert workflow_plan["max_risk_level"] == "medium"
        assert workflow_plan["requires_confirmation"] is True
        assert workflow_plan["validation_errors"] == []
        assert workflow_plan["steps"][0]["input"]["message"] == message
        planned_agents = {step["agent"] for step in workflow_plan["steps"]}
        assert "document_agent" in planned_agents
        assert "code_agent" in planned_agents
        assert "report_agent" in planned_agents
        steps_by_agent = {step["agent"]: step for step in workflow_plan["steps"]}
        for step in workflow_plan["steps"]:
            assert step["reason"]
            assert step["expected_output"]
        assert steps_by_agent["code_agent"]["requires_confirmation"] is True
        assert steps_by_agent["report_agent"]["risk_level"] == "medium"

        workflow_run = chat_payload["workflow_run"]
        assert workflow_run["task_id"] == task_id
        assert workflow_run["mode"] == "dry_run"
        assert workflow_run["status"] == "completed"
        assert workflow_run["requires_confirmation"] is True
        assert workflow_run["max_risk_level"] == "medium"
        assert workflow_run["validation_errors"] == []
        assert len(workflow_run["steps"]) == len(workflow_plan["steps"])
        confirmation_step_ids = {
            step["step_id"] for step in workflow_run["steps"] if step["requires_confirmation"]
        }
        confirmation_step_count = len(confirmation_step_ids)
        assert confirmation_step_count == 2

        task_status = client.get(f"/api/tasks/{task_id}")
        task_status.raise_for_status()
        assert task_status.json() == workflow_run

        task_logs = client.get(f"/api/tasks/{task_id}/logs")
        task_logs.raise_for_status()
        task_logs_payload = task_logs.json()
        assert task_logs_payload["task_id"] == task_id
        assert task_logs_payload["total"] == 2 + len(workflow_run["steps"]) * 2 + 1 + confirmation_step_count
        assert task_logs_payload["events"][1]["agent_id"] == "workflow_engine"
        confirmation_events = [
            event for event in task_logs_payload["events"] if event["event"] == "confirmation_required"
        ]
        assert len(confirmation_events) == confirmation_step_count
        assert {event["step_id"] for event in confirmation_events} == confirmation_step_ids
        assert all(event["level"] == "warning" for event in confirmation_events)

        task_updates = client.get(f"/api/tasks/{task_id}/updates")
        task_updates.raise_for_status()
        task_updates_payload = task_updates.json()
        assert task_updates_payload["task_id"] == task_id
        update_events = {update["event"] for update in task_updates_payload["updates"]}
        assert "confirmation_required" in update_events
        assert "artifact_planned" in update_events
        assert task_updates_payload["updates"][-1]["event"] == "task_state_snapshot"

        task_list = client.get("/api/tasks?limit=10&offset=0")
        task_list.raise_for_status()
        task_list_payload = task_list.json()
        assert task_list_payload["total"] >= 1
        assert any(task["task_id"] == task_id for task in task_list_payload["tasks"])

        filtered_task_list = client.get(
            "/api/tasks",
            params={
                "status": "completed",
                "mode": "dry_run",
                "max_risk_level": "medium",
                "requires_confirmation": "true",
                "limit": 20,
            },
        )
        filtered_task_list.raise_for_status()
        filtered_tasks = filtered_task_list.json()["tasks"]
        assert any(task["task_id"] == task_id for task in filtered_tasks)
        for task in filtered_tasks:
            assert task["status"] == "completed"
            assert task["mode"] == "dry_run"
            assert task["max_risk_level"] == "medium"
            assert task["requires_confirmation"] is True

        missing_task = client.get("/api/tasks/not_exist")
        assert missing_task.status_code == 404

        cancel = client.post(f"/api/tasks/{task_id}/cancel")
        cancel.raise_for_status()
        cancel_payload = cancel.json()
        assert cancel_payload["action"] == "cancel"
        assert cancel_payload["accepted"] is False
        assert cancel_payload["workflow_run"]["task_id"] == task_id

        retry = client.post(f"/api/tasks/{task_id}/retry")
        retry.raise_for_status()
        retry_payload = retry.json()
        assert retry_payload["action"] == "retry"
        assert retry_payload["accepted"] is True
        assert retry_payload["new_task_id"]
        assert retry_payload["workflow_run"]["task_id"] == retry_payload["new_task_id"]

        retry_status = client.get(f"/api/tasks/{retry_payload['new_task_id']}")
        retry_status.raise_for_status()
        assert retry_status.json() == retry_payload["workflow_run"]

    async with websockets.connect(f"{WS_BASE_URL}/ws/tasks/{task_id}") as websocket:
        expected_event_count = 2 + len(workflow_run["steps"]) * 2 + 1 + confirmation_step_count
        dry_run_events = [json.loads(await websocket.recv()) for _ in range(expected_event_count)]
        assert dry_run_events[0]["event"] == "connected"
        assert dry_run_events[1]["agent_id"] == "workflow_engine"
        assert dry_run_events[1]["event"] == "task_started"
        assert any(event["event"] == "confirmation_required" for event in dry_run_events)
        assert dry_run_events[-1]["event"] == "task_completed"
        assert "dry-run" in dry_run_events[-1]["message"]

    async with websockets.connect(f"{WS_BASE_URL}/ws/tasks/demo") as websocket:
        events = [json.loads(await websocket.recv()) for _ in range(5)]
        assert events[0]["event"] == "connected"
        assert events[0]["message"] == "已连接 AgentFlow 任务日志通道。"
        assert events[-1]["event"] == "task_completed"

    print("AgentFlow live backend verification passed with UTF-8 Chinese payloads.")


if __name__ == "__main__":
    asyncio.run(main())
