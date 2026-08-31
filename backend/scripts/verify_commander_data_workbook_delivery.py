"""验证 Commander 委派分析 Excel 的 Runtime 与会话交付闭环。

脚本使用临时 CSV、mock 对话与本地 pandas/openpyxl：覆盖“明确数据目标 -> 受控计划 ->
客户确认后的 Runtime -> 新工作簿回读 -> 同一会话交付”。不读取客户数据、不调用网络或真实模型。
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
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_commander_workbook_delivery_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"] = str(VERIFY_ROOT / "workspace")
os.environ["AGENTFLOW_DATA_ANALYSIS_OUTPUT_DIR"] = str(VERIFY_ROOT / "output" / "data_analysis")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.schemas.chat import WorkflowMaterialBinding, WorkflowPlanPreferences  # noqa: E402
from app.services.agent_catalog import list_agents  # noqa: E402
from app.services.commander import create_commander_plan  # noqa: E402
from app.services.conversation_memory import prepare_conversation  # noqa: E402
from app.workflow.dry_run import run_workflow_dry_run  # noqa: E402
from main import app  # noqa: E402


def _sample_csv() -> bytes:
    """提供日期、分类和数值，确保工作簿能写入分析表与原生图表。"""

    return (
        "month,region,sales\n"
        "2026-01-01,east,120\n"
        "2026-01-01,south,90\n"
        "2026-02-01,east,155\n"
        "2026-02-01,south,112\n"
        "2026-03-01,east,183\n"
        "2026-03-01,south,146\n"
    ).encode("utf-8")


def main() -> None:
    """运行一条确认写入型 Commander 工作簿任务，并验证父子任务与会话交付。"""

    agents = list_agents()
    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={
                "filename": "commander_workbook_delivery.csv",
                "content_base64": base64.b64encode(_sample_csv()).decode("ascii"),
            },
        )
        assert imported.status_code == 200, imported.text
        dataset_ref = imported.json()["relative_path"]
        source_path = Path(os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"]) / dataset_ref
        source_hash_before = sha256(source_path.read_bytes()).hexdigest()
        material = WorkflowMaterialBinding(
            binding_id="verify_workbook_dataset",
            kind="dataset",
            ref=dataset_ref,
            display_name="总指挥工作簿验收.csv",
            origin="client_selected",
            usage="客户明确要求导出分析 Excel。",
        )
        prepared_conversation = prepare_conversation(
            conversation_id="",
            project_scope="global",
            message="请分析当前 CSV 的月度趋势和区域差异，并生成分析 Excel 工作簿。",
            supplied_materials=[material],
        )

        # `auto_approve` 仅模拟客户已经在会话中回复“开始执行”；正式 smart_confirm 仍保留
        # file_write 的自然语言确认边界，不能因本离线脚本而自动放开生产写入。
        plan = create_commander_plan(
            "请分析当前 CSV 的月度趋势和区域差异，并生成分析 Excel 工作簿。",
            available_agents=agents,
            materials=[material],
            preferences=WorkflowPlanPreferences(permission_policy="auto_approve"),
            conversation_id=prepared_conversation.context.session.conversation_id,
        )
        assert plan.validation_errors == [], plan.validation_errors
        analysis_step = next(step for step in plan.steps if step.action == "analyze_dataset")
        workbook_step = next(step for step in plan.steps if step.action == "export_analysis_workbook")
        assert workbook_step.depends_on == [analysis_step.id]
        assert workbook_step.required_permissions == ["file_read", "file_write"]

        source_task_id = "verify_commander_workbook_parent"
        dry_run = run_workflow_dry_run(
            task_id=source_task_id,
            plan=plan,
            available_agents=agents,
        )
        assert dry_run.status == "completed", dry_run.validation_errors
        executed = client.post(f"/api/tasks/{source_task_id}/execute")
        assert executed.status_code == 200, executed.text
        payload = executed.json()
        assert payload["status"] == "completed", payload

        workbook_runtime_step = next(
            item
            for item in payload["workflow_run"]["steps"]
            if item["action"] == "export_analysis_workbook"
        )
        result = workbook_runtime_step["output"]["result"]
        assert result["agent_status"] == "completed", result
        assert result["read_only"] is False
        assert result["artifact"] is not None
        assert result["verification"]["passed"] is True

        delegated_task_id = result["delegated_task_id"]
        child_artifacts = client.get(f"/api/tasks/{delegated_task_id}/artifacts")
        assert child_artifacts.status_code == 200, child_artifacts.text
        artifacts = child_artifacts.json()["artifacts"]
        assert len(artifacts) == 1
        assert artifacts[0]["mime_type"].endswith("spreadsheetml.sheet")
        assert artifacts[0]["metadata"].get("output_path") == "<hidden>"

        assert sha256(source_path.read_bytes()).hexdigest() == source_hash_before
        conversation_id = prepared_conversation.context.session.conversation_id
        messages = client.get(f"/api/chat/conversations/{conversation_id}/messages").json()["messages"]
        deliveries = [
            item["content"]
            for item in messages
            if item["role"] == "assistant" and "分析 Excel 已交付" in item["content"]
        ]
        assert len(deliveries) == 1, deliveries
        assert "通过工作簿回读验证" in deliveries[0]
        assert "源 CSV/XLSX 没有被修改" in deliveries[0]

    print("Commander workbook delivery verification passed: runtime=completed xlsx=audited chat_delivery=once source_unchanged=true")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
