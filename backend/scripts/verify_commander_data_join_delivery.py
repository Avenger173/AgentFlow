"""验证总指挥两份数据的关联预览、确认交付和源文件保护。"""

from __future__ import annotations

import base64
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_commander_join_delivery_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"] = str(VERIFY_ROOT / "workspace")
os.environ["AGENTFLOW_DATA_JOIN_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "data_joins")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.schemas.chat import WorkflowMaterialBinding, WorkflowPlanPreferences  # noqa: E402
from app.services.agent_catalog import list_agents  # noqa: E402
from app.services.commander import create_commander_plan  # noqa: E402
from app.services.conversation_memory import prepare_conversation  # noqa: E402
from app.workflow.dry_run import run_workflow_dry_run  # noqa: E402
from main import app  # noqa: E402


def _left_csv() -> bytes:
    """左表保留全部记录，便于验证左连接的未匹配统计。"""

    return "客户ID,客户名,订单金额\nA001,甲公司,120\nA002,乙公司,80\nA003,丙公司,50\n".encode("utf-8")


def _right_csv() -> bytes:
    """右表提供一个额外字段，并保留一个左表不存在的客户。"""

    return "客户ID,客户等级\nA001,重点\nA002,普通\nA004,潜在\n".encode("utf-8")


def _import(client: TestClient, filename: str, content: bytes) -> str:
    response = client.post(
        "/api/agents/data_agent/datasets",
        json={"filename": filename, "content_base64": base64.b64encode(content).decode("ascii")},
    )
    assert response.status_code == 200, response.text
    return response.json()["relative_path"]


def main() -> None:
    """执行 R5.4C 的最小端到端离线验收。"""

    with TestClient(app) as client:
        left_ref = _import(client, "join_left.csv", _left_csv())
        right_ref = _import(client, "join_right.csv", _right_csv())
        left_path = Path(os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"]) / left_ref
        right_path = Path(os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"]) / right_ref
        source_hashes_before = {
            left_ref: sha256(left_path.read_bytes()).hexdigest(),
            right_ref: sha256(right_path.read_bytes()).hexdigest(),
        }
        materials = [
            WorkflowMaterialBinding(binding_id="left", kind="dataset", ref=left_ref, display_name="销售客户.csv"),
            WorkflowMaterialBinding(binding_id="right", kind="dataset", ref=right_ref, display_name="客户等级.csv"),
        ]
        message = "请把两份数据按客户ID合并，保留左表全部记录。"
        conversation = prepare_conversation(conversation_id="", project_scope="global", message=message, supplied_materials=materials)
        plan = create_commander_plan(
            message,
            available_agents=list_agents(),
            preferences=WorkflowPlanPreferences(permission_policy="auto_approve"),
            materials=materials,
            conversation_id=conversation.context.session.conversation_id,
        )
        assert plan.validation_errors == [], plan.validation_errors
        assert plan.intent == "data_join"
        join_steps = [step for step in plan.steps if step.action in {"plan_dataset_join", "export_dataset_join"}]
        assert [step.action for step in join_steps] == ["plan_dataset_join", "export_dataset_join"]
        assert join_steps[1].depends_on == [join_steps[0].id]
        assert join_steps[1].required_permissions == ["file_read", "file_write"]
        assert join_steps[0].input["left_key"] == "客户ID"
        assert join_steps[0].input["right_key"] == "客户ID"
        assert join_steps[0].input["join_type"] == "left"

        parent_task_id = "verify_commander_join_parent"
        dry_run = run_workflow_dry_run(task_id=parent_task_id, plan=plan, available_agents=list_agents())
        assert dry_run.status == "completed", dry_run.validation_errors
        executed = client.post(f"/api/tasks/{parent_task_id}/execute")
        assert executed.status_code == 200, executed.text
        payload = executed.json()
        assert payload["status"] == "completed", payload

        preview_run = next(item for item in payload["workflow_run"]["steps"] if item["action"] == "plan_dataset_join")
        preview_result = preview_run["output"]["result"]
        assert preview_result["read_only"] is True
        assert preview_result["output_row_count"] == 3
        assert preview_result["matched_row_count"] == 2
        assert preview_result["left_only_row_count"] == 1
        assert preview_result["right_only_row_count"] == 1

        export_run = next(item for item in payload["workflow_run"]["steps"] if item["action"] == "export_dataset_join")
        export_result = export_run["output"]["result"]
        assert export_result["agent_status"] == "completed", export_result
        assert export_result["read_only"] is False
        assert export_result["verification"]["passed"] is True
        assert export_result["verification"]["source_hashes_unchanged"] is True
        assert "客户等级" in export_result["verification"]["output_columns"]
        assert all(sha256(path.read_bytes()).hexdigest() == source_hashes_before[ref] for ref, path in ((left_ref, left_path), (right_ref, right_path)))

        delegated_task_id = export_result["delegated_task_id"]
        artifacts = client.get(f"/api/tasks/{delegated_task_id}/artifacts")
        assert artifacts.status_code == 200, artifacts.text
        child_artifacts = artifacts.json()["artifacts"]
        assert len(child_artifacts) == 1
        assert child_artifacts[0]["name"].endswith(".csv")
        assert child_artifacts[0]["metadata"].get("output_path") == "<hidden>"

        messages = client.get(f"/api/chat/conversations/{conversation.context.session.conversation_id}/messages")
        assert messages.status_code == 200, messages.text
        deliveries = [
            item["content"]
            for item in messages.json()["messages"]
            if item["role"] == "assistant" and "多数据集合并副本已交付" in item["content"]
        ]
        assert len(deliveries) == 1, deliveries
        assert "匹配行数：2" in deliveries[0]
        assert "两份源文件均未修改" in deliveries[0]

        automatic_key_plan = create_commander_plan(
            "请把两份数据合并。",
            available_agents=list_agents(),
            preferences=WorkflowPlanPreferences(permission_policy="auto_approve"),
            materials=materials,
            conversation_id=conversation.context.session.conversation_id,
        )
        # 两份数据只有一个同名字段时，系统应自动采用该字段，减少用户额外输入。
        assert automatic_key_plan.validation_errors == [], automatic_key_plan.validation_errors
        assert automatic_key_plan.intent == "data_join"
        automatic_join = next(step for step in automatic_key_plan.steps if step.action == "plan_dataset_join")
        assert automatic_join.input["left_key"] == "客户ID"
        assert automatic_join.input["right_key"] == "客户ID"

    print("Commander join delivery verification passed: two_datasets=true left_join=true preview=true csv=verified source_unchanged=true")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
