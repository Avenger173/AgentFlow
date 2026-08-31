"""验证总指挥自然语言字段加工的预览、确认写入和会话交付闭环。

脚本使用临时 CSV 与 mock Runtime，不调用真实模型或网络。它覆盖客户只说一句自然语言时，
Commander 是否能从数据画像选出有限加工动作，并在确认后把所有新字段追加到原格式副本。
"""

from __future__ import annotations

import base64
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_commander_transform_delivery_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"] = str(VERIFY_ROOT / "workspace")
os.environ["AGENTFLOW_DATA_TRANSFORMATION_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "data_transformations")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.schemas.chat import WorkflowMaterialBinding, WorkflowPlanPreferences  # noqa: E402
from app.services.agent_catalog import list_agents  # noqa: E402
from app.services.commander import create_commander_plan  # noqa: E402
from app.services.conversation_memory import prepare_conversation  # noqa: E402
from app.workflow.dry_run import run_workflow_dry_run  # noqa: E402
from main import app  # noqa: E402


def _sample_csv() -> bytes:
    """提供日期、金额和数量，覆盖日期拆分、排名与累计三种加工。"""

    return (
        "日期,金额,数量\n"
        "2026-01-01,100,2\n"
        "2026-02-01,150,3\n"
        "2026-03-01,120,4\n"
    ).encode("utf-8")


def main() -> None:
    """运行字段加工的 Commander 端到端离线验收。"""

    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={
                "filename": "commander_transform_delivery.csv",
                "content_base64": base64.b64encode(_sample_csv()).decode("ascii"),
            },
        )
        assert imported.status_code == 200, imported.text
        dataset_ref = imported.json()["relative_path"]
        source_path = Path(os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"]) / dataset_ref
        source_hash_before = sha256(source_path.read_bytes()).hexdigest()
        material = WorkflowMaterialBinding(
            binding_id="verify_transform_dataset",
            kind="dataset",
            ref=dataset_ref,
            display_name="字段加工验收.csv",
            origin="client_selected",
            usage="客户明确要求新增派生字段。",
        )
        conversation = prepare_conversation(
            conversation_id="",
            project_scope="global",
            message="请根据金额新增金额排名、累计金额、月份字段。",
            supplied_materials=[material],
        )
        conversation_id = conversation.context.session.conversation_id
        plan = create_commander_plan(
            "请根据金额新增金额排名、累计金额、月份字段。",
            available_agents=list_agents(),
            preferences=WorkflowPlanPreferences(permission_policy="auto_approve"),
            materials=[material],
            conversation_id=conversation_id,
        )
        assert plan.validation_errors == [], plan.validation_errors
        transform_steps = [
            step for step in plan.steps if step.action in {"plan_field_transform", "export_field_transform"}
        ]
        assert [step.action for step in transform_steps] == ["plan_field_transform", "export_field_transform"]
        assert transform_steps[1].depends_on == [transform_steps[0].id]
        assert transform_steps[1].required_permissions == ["file_read", "file_write"]
        assert len(transform_steps[0].input["operations"]) == 3
        assert plan.intent == "data_transform"

        parent_task_id = "verify_commander_transform_parent"
        dry_run = run_workflow_dry_run(task_id=parent_task_id, plan=plan, available_agents=list_agents())
        assert dry_run.status == "completed", dry_run.validation_errors
        executed = client.post(f"/api/tasks/{parent_task_id}/execute")
        assert executed.status_code == 200, executed.text
        payload = executed.json()
        assert payload["status"] == "completed", payload

        preview_run = next(item for item in payload["workflow_run"]["steps"] if item["action"] == "plan_field_transform")
        preview_result = preview_run["output"]["result"]
        assert preview_result["read_only"] is True
        assert len(preview_result["plans"]) == 3

        export_run = next(item for item in payload["workflow_run"]["steps"] if item["action"] == "export_field_transform")
        export_result = export_run["output"]["result"]
        assert export_result["agent_status"] == "completed", export_result
        assert export_result["read_only"] is False
        assert export_result["artifact"] is not None
        assert export_result["verification"]["passed"] is True
        assert len(export_result["verification"]["result_columns"]) == 3
        assert sha256(source_path.read_bytes()).hexdigest() == source_hash_before

        delegated_task_id = export_result["delegated_task_id"]
        artifacts = client.get(f"/api/tasks/{delegated_task_id}/artifacts")
        assert artifacts.status_code == 200, artifacts.text
        child_artifacts = artifacts.json()["artifacts"]
        assert len(child_artifacts) == 1
        assert child_artifacts[0]["name"].endswith(".csv")
        assert child_artifacts[0]["metadata"].get("output_path") == "<hidden>"

        messages = client.get(f"/api/chat/conversations/{conversation_id}/messages").json()["messages"]
        deliveries = [
            item["content"]
            for item in messages
            if item["role"] == "assistant" and "字段加工副本已交付" in item["content"]
        ]
        assert len(deliveries) == 1, deliveries
        assert "月份" in deliveries[0] and "排名" in deliveries[0]
        assert "源 CSV/XLSX 没有被修改" in deliveries[0]

    print("Commander transformation delivery verification passed: runtime=completed fields=3 csv=verified source_unchanged=true")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
