"""验证 D5.4 Commander 对数据工作台的只读真实委派。

本脚本只导入临时合成 CSV，不调用真实模型或网络。重点固定以下边界：总指挥一次只接受
一份显式数据材料；子任务复用确定性 D2 计算；父任务只保存短结论和源哈希；原始行不进入
父/子任务审计；不创建 Excel、PNG 或字段副本。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_data_d54_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.api.chat import _automatic_read_only_activity
from app.schemas.chat import WorkflowMaterialBinding
from app.services.agent_catalog import list_agents
from app.services.commander import create_commander_plan
from app.workflow.dry_run import run_workflow_dry_run
from main import app


def _dataset_material(ref: str) -> WorkflowMaterialBinding:
    """构造数据工作台已明确选中的相对引用，不传原始内容或本机路径。"""

    return WorkflowMaterialBinding(
        binding_id="verify_dataset",
        kind="dataset",
        ref=ref,
        display_name="D5.4 临时销售数据.csv",
        origin="client_selected",
        usage="客户在数据工作台完成画像后，明确交给总指挥分析。",
    )


def _encoded_sample() -> str:
    """准备带日期、类别和数值的最小 CSV，覆盖趋势与对比的本地计划。"""

    content = (
        "日期,区域,产品,销售额\n"
        "2026-01-05,华东,A,120\n"
        "2026-01-20,华南,A,90\n"
        "2026-02-10,华东,B,180\n"
        "2026-02-28,华北,B,110\n"
        "2026-03-08,华东,A,210\n"
    ).encode("utf-8")
    return base64.b64encode(content).decode("ascii")


def main() -> None:
    """覆盖 D5.4 的准入、Runtime、最小化与父子审计链。"""

    agents = list_agents()
    data_agent = next(item for item in agents if item.id == "data_agent")
    assert data_agent.runtime_ready is True
    assert data_agent.maturity == "mvp"

    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "D5.4 临时销售数据.csv", "content_base64": _encoded_sample()},
        )
        assert imported.status_code == 200, imported.text
        dataset_ref = imported.json()["relative_path"]
        material = _dataset_material(dataset_ref)

        # 无绑定时不能扫描磁盘；多份绑定也不能为了“智能”而猜测该读哪一份。
        unbound_plan = create_commander_plan("请分析销售趋势并给出图表建议。", agents)
        assert all(step.action != "analyze_dataset" for step in unbound_plan.steps)
        assert unbound_plan.clarifying_questions
        multi_plan = create_commander_plan(
            "请分析两份数据。",
            agents,
            materials=[material, _dataset_material("另一份.csv")],
        )
        assert all(step.action != "analyze_dataset" for step in multi_plan.steps)
        assert any("一次只分析一份" in item for item in multi_plan.clarifying_questions)

        plan = create_commander_plan(
            "请分析这份销售数据的月度趋势和区域差异，并说明可以生成哪些图表。",
            agents,
            materials=[material],
        )
        assert plan.validation_errors == []
        data_step = next(step for step in plan.steps if step.action == "analyze_dataset")
        assert data_step.execution_mode == "execute"
        assert data_step.required_permissions == ["file_read"]
        assert data_step.input["dataset_name"] == dataset_ref
        assert data_step.input["dataset_refs"] == [dataset_ref]
        assert plan.next_action == "execute_after_confirm"
        assert plan.workspace_scope.read_paths == [f"data/datasets/{dataset_ref}"]
        assert plan.workspace_scope.write_paths == []
        # 计划协议保持 `execute_after_confirm` 兼容，但桌面端和聊天 API 只会把这个极窄的
        # 单数据集只读 action 识别为可自动受理；写入、联网和组合任务不能复用该判断。
        assert _automatic_read_only_activity(plan) == "data"

        admissions = client.get("/api/agents/action-admissions")
        assert admissions.status_code == 200, admissions.text
        admission = next(
            item for item in admissions.json()["actions"]
            if item["agent_id"] == "data_agent" and item["action"] == "analyze_dataset"
        )
        assert admission["execution_mode"] == "execute"
        assert admission["material_kind"] == "dataset"

        contracts = client.get(
            "/api/workflow/node-contracts",
            params={"agent_id": "data_agent", "action": "analyze_dataset"},
        )
        assert contracts.status_code == 200, contracts.text
        assert contracts.json()["contracts"][0]["tool_name"] == "agent.data_agent.analyze_preview"

        source_task_id = "verify_commander_data_d54_parent"
        dry_run = run_workflow_dry_run(task_id=source_task_id, plan=plan, available_agents=agents)
        assert dry_run.status == "completed", dry_run.validation_errors
        execution = client.post(f"/api/tasks/{source_task_id}/execute")
        assert execution.status_code == 200, execution.text
        runtime_payload = execution.json()
        assert runtime_payload["status"] == "completed"
        runtime_step = next(item for item in runtime_payload["workflow_run"]["steps"] if item["action"] == "analyze_dataset")
        assert runtime_step["status"] == "completed"
        result = runtime_step["output"]["result"]
        assert result["agent_status"] == "completed"
        assert result["source_sha256"]
        assert result["chart_count"] >= 2
        assert result["table_count"] >= 2
        assert result["read_only"] is True

        child_task_id = result["delegated_task_id"]
        child_steps = client.get(f"/api/tasks/{child_task_id}/steps")
        assert child_steps.status_code == 200, child_steps.text
        child_output = child_steps.json()["steps"][0]["output"]
        assert child_output["read_only"] is True
        assert child_output["original_file_unchanged"] is True
        assert child_output["output_created"] is False
        assert child_output["raw_rows_visible"] is False
        assert all("rows" not in table for table in child_output["analysis"]["tables"])
        assert "华东,A,120" not in str(child_output)

        artifacts = client.get(f"/api/tasks/{runtime_payload['runtime_task_id']}/artifacts")
        assert artifacts.status_code == 200, artifacts.text
        delegation = next(item for item in artifacts.json()["artifacts"] if item["step_id"] == data_step.id)
        assert delegation["name"] == "数据工作台分析结果"
        assert delegation["uri"] == f"agentflow-task://{child_task_id}"
        assert delegation["metadata"]["delegated_task_id"] == child_task_id

        # 聊天 API 对同一条安全白名单计划应先保存简短真实状态；Runtime 完成后把已经脱敏的
        # 数据结论追加回同一会话。这里不读取原始 CSV、预览行或模型输出。
        chat = client.post(
            "/api/chat",
            json={
                "message": "请分析当前数据文件的主要趋势和可生成图表。",
                "materials": [material.model_dump(mode="json")],
            },
        )
        assert chat.status_code == 200, chat.text
        chat_payload = chat.json()
        assert "正在分析已选数据" in chat_payload["reply"]
        chat_execution = client.post(f"/api/tasks/{chat_payload['task_id']}/execute")
        assert chat_execution.status_code == 200, chat_execution.text
        conversation_id = chat_payload["conversation_id"]
        transcript = client.get(f"/api/chat/conversations/{conversation_id}/messages", params={"limit": 20})
        assert transcript.status_code == 200, transcript.text
        assistant_messages = [
            item["content"] for item in transcript.json()["messages"] if item["role"] == "assistant"
        ]
        assert any("数据分析结果" in item for item in assistant_messages)

    assert not (VERIFY_DATA_DIR / "outputs").exists()
    print("Commander D5.4 data delegation verification passed: one-dataset read-only child task audited.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
