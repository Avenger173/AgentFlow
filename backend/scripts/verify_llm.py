import sys
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from main import app


def main() -> None:
    """执行一次真实模型连通性检查。

    这个脚本会读取 backend/.env 或系统环境变量中的模型配置，但不会打印 API Key。
    运行前请确认 `AGENTFLOW_CHAT_MODE=llm`，否则它会明确失败，避免把 mock 当作真实模型。
    """

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    client = TestClient(app)
    response = client.post(
        "/api/chat",
        json={"message": "请用一句中文回答：AgentFlow LLM 已连接。"},
    )
    payload = response.json()

    assert response.status_code == 200, payload.get("detail", response.text)
    assert payload["mode"] == "llm", f"当前不是真实模型模式：{payload.get('mode')}"
    assert payload["reply"], "模型返回了空回复。"
    assert payload["workflow_plan"]["version"] == "1.0"
    assert payload["workflow_plan"]["workflow_name"] == "commander_initial_plan"
    assert payload["workflow_plan"]["summary"]
    assert payload["workflow_plan"]["max_risk_level"] in {"low", "medium", "high"}
    assert payload["workflow_plan"]["validation_errors"] == []
    assert payload["workflow_run"]["task_id"] == payload["task_id"]
    assert payload["workflow_run"]["mode"] == "dry_run"
    assert payload["workflow_run"]["status"] == "completed"
    assert payload["workflow_run"]["validation_errors"] == []

    task_status = client.get(f"/api/tasks/{payload['task_id']}")
    assert task_status.status_code == 200, task_status.text
    assert task_status.json() == payload["workflow_run"]

    task_list = client.get("/api/tasks?limit=20")
    assert task_list.status_code == 200, task_list.text
    assert any(task["task_id"] == payload["task_id"] for task in task_list.json()["tasks"])

    filtered_task_list = client.get(
        "/api/tasks",
        params={"limit": 20, "status": "completed", "mode": "dry_run"},
    )
    assert filtered_task_list.status_code == 200, filtered_task_list.text
    assert any(task["task_id"] == payload["task_id"] for task in filtered_task_list.json()["tasks"])

    retry = client.post(f"/api/tasks/{payload['task_id']}/retry")
    assert retry.status_code == 200, retry.text
    retry_payload = retry.json()
    assert retry_payload["accepted"] is True
    assert retry_payload["workflow_run"]["mode"] == "dry_run"

    print(
        "AgentFlow LLM verification passed: "
        f"model={payload.get('model')} reply_length={len(payload['reply'])}"
    )


if __name__ == "__main__":
    main()
