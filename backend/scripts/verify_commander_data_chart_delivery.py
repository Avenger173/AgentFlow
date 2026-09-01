"""验证 Commander 委派数据图表 PNG 的真实 Runtime 闭环。

本脚本只使用临时 CSV、mock 对话与本地渲染：覆盖“任务意图 -> 受控计划 -> 权限策略 ->
数据分析子任务 -> 图表 PNG 子任务 -> artifact 审计”，不读取客户数据、不调用网络或真实模型。
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
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_commander_chart_delivery_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"] = str(VERIFY_ROOT / "workspace")
os.environ["AGENTFLOW_DATA_CHART_OUTPUT_DIR"] = str(VERIFY_ROOT / "charts")
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.schemas.chat import WorkflowMaterialBinding, WorkflowPlanPreferences  # noqa: E402
from app.services.agent_catalog import list_agents  # noqa: E402
from app.services.commander import create_commander_plan  # noqa: E402
from app.services.conversation_memory import prepare_conversation  # noqa: E402
from app.workflow.dry_run import run_workflow_dry_run  # noqa: E402
from main import app  # noqa: E402


def _sample_csv() -> bytes:
    """提供日期、分类和数值，确保 D2 可以形成趋势及对比两类图表。"""

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
    """运行一条文件写入型 Commander 任务，并验证父子任务的边界。"""

    agents = list_agents()
    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={
                "filename": "commander_chart_delivery.csv",
                "content_base64": base64.b64encode(_sample_csv()).decode("ascii"),
            },
        )
        assert imported.status_code == 200, imported.text
        dataset_ref = imported.json()["relative_path"]
        source_path = Path(os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"]) / dataset_ref
        source_hash_before = sha256(source_path.read_bytes()).hexdigest()
        material = WorkflowMaterialBinding(
            binding_id="verify_chart_dataset",
            kind="dataset",
            ref=dataset_ref,
            display_name="总指挥图表验收.csv",
            origin="client_selected",
            usage="客户明确要求分析并生成可保存图表。",
        )
        prepared_conversation = prepare_conversation(
            conversation_id="",
            project_scope="global",
            message="请分析当前 CSV 的月度趋势和区域差异，生成 3 张图表并保存为 PNG。",
            supplied_materials=[material],
        )

        # `auto_approve` 仅用于离线验收“客户已经回复开始执行”的后续权限策略；生产环境
        # 默认 smart_confirm 仍会保留文件写入确认。图表节点本身继续把 file_write 写入计划。
        plan = create_commander_plan(
            "请分析当前 CSV 的月度趋势和区域差异，生成 3 张图表并保存为 PNG。",
            available_agents=agents,
            materials=[material],
            preferences=WorkflowPlanPreferences(permission_policy="auto_approve"),
            conversation_id=prepared_conversation.context.session.conversation_id,
        )
        assert plan.validation_errors == [], plan.validation_errors
        analysis_step = next(step for step in plan.steps if step.action == "analyze_dataset")
        chart_step = next(step for step in plan.steps if step.action == "export_chart_dashboard")
        assert chart_step.depends_on == [analysis_step.id]
        assert chart_step.required_permissions == ["file_read", "file_write"]
        assert chart_step.input["max_chart_count"] == 3
        assert chart_step.input["explicit_output_request"] is True
        assert plan.workspace_scope.write_paths == ["data/outputs/<runtime_task_id>/"]

        source_task_id = "verify_commander_chart_parent"
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

        chart_runtime_step = next(
            item
            for item in payload["workflow_run"]["steps"]
            if item["action"] == "export_chart_dashboard"
        )
        result = chart_runtime_step["output"]["result"]
        assert result["agent_status"] == "completed", result
        assert result["read_only"] is False
        assert result["chart_count"] >= 2
        assert result["artifacts"]

        delegated_task_id = result["delegated_task_id"]
        child_artifacts = client.get(f"/api/tasks/{delegated_task_id}/artifacts")
        assert child_artifacts.status_code == 200, child_artifacts.text
        assert len(child_artifacts.json()["artifacts"]) == result["chart_count"]
        assert all(
            item["metadata"].get("output_path") == "<hidden>"
            for item in child_artifacts.json()["artifacts"]
        )

        parent_artifacts = client.get(f"/api/tasks/{payload['runtime_task_id']}/artifacts")
        assert parent_artifacts.status_code == 200, parent_artifacts.text
        assert any(
            item["uri"] == f"agentflow-task://{delegated_task_id}"
            for item in parent_artifacts.json()["artifacts"]
        )
        delivery = client.get(f"/api/tasks/{payload['runtime_task_id']}/delivery")
        assert delivery.status_code == 200, delivery.text
        delivery_artifacts = delivery.json()["artifacts"]
        chart_artifacts = [item for item in delivery_artifacts if item["mime_type"] == "image/png"]
        assert len(chart_artifacts) == result["chart_count"], delivery_artifacts
        assert all(item["openable"] and item["previewable"] for item in chart_artifacts)
        assert all(item["source_task_id"] == delegated_task_id for item in chart_artifacts)
        assert sha256(source_path.read_bytes()).hexdigest() == source_hash_before

        # 图表是客户明确确认后的写入型交付，但完成后也必须回到同一段调度会话，不能只让
        # 客户去数据工作台或任务历史猜测结果。该断言同时覆盖 Runtime 的稳定交付 ID 去重。
        conversation_id = prepared_conversation.context.session.conversation_id
        messages = client.get(f"/api/chat/conversations/{conversation_id}/messages").json()["messages"]
        deliveries = [
            item["content"]
            for item in messages
            if item["role"] == "assistant" and "图表交付已完成" in item["content"]
        ]
        assert len(deliveries) == 1, deliveries
        assert "通过像素回读验证" in deliveries[0]
        assert "源 CSV/XLSX 没有被修改" in deliveries[0]

    print("Commander chart delivery verification passed: plan=two-step runtime=completed png=audited chat_delivery=once source_unchanged=true")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
