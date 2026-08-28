"""总指挥 C0 动作准入与 C1 初始交接的隔离回归。

该脚本刻意强制 mock 模式，不调用任何真实模型或外部服务。它验证的是产品边界：总指挥
只能使用明确绑定的材料，文档可进入真实委派计划，数据目前只能诚实地交给数据工作台。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


# 必须在导入 app 前注入配置，确保本脚本不会污染开发时的 SQLite、workspace 或模型设置。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_c0_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.schemas.chat import WorkflowMaterialBinding
from app.services.agent_catalog import list_agents
from app.services.commander import create_commander_plan
from app.workflow.dry_run import run_workflow_dry_run
from main import app


def _document_material(ref: str) -> WorkflowMaterialBinding:
    return WorkflowMaterialBinding(
        binding_id="verify_document",
        kind="document",
        ref=ref,
        display_name=ref,
        origin="client_selected",
        usage="隔离回归明确选择的文档。",
    )


def _dataset_material(ref: str) -> WorkflowMaterialBinding:
    return WorkflowMaterialBinding(
        binding_id="verify_dataset",
        kind="dataset",
        ref=ref,
        display_name=ref,
        origin="client_selected",
        usage="隔离回归明确选择的数据文件。",
    )


def main() -> None:
    client = TestClient(app)
    agents = list_agents()

    admissions = client.get("/api/agents/action-admissions")
    assert admissions.status_code == 200, admissions.text
    admission_items = {
        (item["agent_id"], item["action"]): item
        for item in admissions.json()["actions"]
    }
    assert admission_items[("document_agent", "analyze_document")]["execution_mode"] == "execute"
    assert admission_items[("data_agent", "open_workspace")]["execution_mode"] == "guided_handoff"
    assert admission_items[("data_agent", "open_workspace")]["requires_runtime_ready"] is False

    # 只有自然语言里的“这个文档”时，总指挥不能扫描本机 workspace 进行猜测。
    ambiguous = create_commander_plan("帮我整理这个文档", agents)
    assert ambiguous.validation_errors == []
    assert all(step.agent != "document_agent" for step in ambiguous.steps)
    assert ambiguous.next_action == "ask_clarifying_questions"
    assert ambiguous.clarifying_questions

    document_plan = create_commander_plan(
        "请在已选文档中搜索《验收要求》。",
        agents,
        materials=[_document_material("project brief.md")],
    )
    assert document_plan.validation_errors == []
    assert document_plan.material_bindings[0].ref == "project brief.md"
    document_step = next(step for step in document_plan.steps if step.agent == "document_agent")
    assert document_step.action == "search_text"
    assert document_step.execution_mode == "execute"
    assert document_step.admission_status == "ready"
    assert document_step.input["document_refs"] == ["project brief.md"]
    assert document_plan.workspace_scope.read_paths == ["data/workspaces/project brief.md"], (
        document_plan.workspace_scope.read_paths
    )
    assert "data/workspaces/*" not in document_plan.workspace_scope.read_paths

    # 文档委派完成后，父 Runtime 必须给客户可读的总指挥汇总；完整材料和来源仍留在子任务。
    document_upload = client.post(
        "/api/workspace/documents",
        json={
            "filename": "commander_parent_summary.md",
            "content": "# 验收材料\n\n交付前必须说明范围、验收标准和待确认风险。\n",
        },
    )
    assert document_upload.status_code == 200, document_upload.text
    delegated_plan = create_commander_plan(
        "请梳理已选材料中的验收要求和待确认风险。",
        agents,
        materials=[_document_material("commander_parent_summary.md")],
    )
    delegated_step = next(step for step in delegated_plan.steps if step.agent == "document_agent")
    assert delegated_step.action == "analyze_document"
    delegated_source_task_id = "verify_commander_document_parent"
    delegated_dry_run = run_workflow_dry_run(
        task_id=delegated_source_task_id,
        plan=delegated_plan,
        available_agents=agents,
    )
    assert delegated_dry_run.status == "completed", delegated_dry_run.validation_errors
    delegated_execution = client.post(f"/api/tasks/{delegated_source_task_id}/execute")
    assert delegated_execution.status_code == 200, delegated_execution.text
    delegated_payload = delegated_execution.json()
    assert delegated_payload["status"] == "completed"
    assert "总指挥已完成本次任务" in delegated_payload["workflow_run"]["summary"]
    delegated_result = next(
        step["output"]["result"]
        for step in delegated_payload["workflow_run"]["steps"]
        if step["agent"] == "document_agent"
    )
    delegated_updates = client.get(f"/api/tasks/{delegated_payload['runtime_task_id']}/updates")
    assert delegated_updates.status_code == 200, delegated_updates.text
    retrospective = next(
        item["payload"]["task_retrospective"]
        for item in delegated_updates.json()["updates"]
        if item["event"] == "task_state_snapshot"
    )
    delegation_summary = retrospective["delegations"]
    assert delegation_summary[0]["task_id"] == delegated_result["delegated_task_id"]
    assert delegation_summary[0]["status"] == "completed"
    assert "完整来源" in delegation_summary[0]["next_action"]

    # 目录跳转不属于用户可绑定的材料，计划必须退回澄清而不是把路径交给后端工具。
    unsafe_plan = create_commander_plan(
        "请分析数据文件。",
        agents,
        materials=[_dataset_material("../private.csv")],
    )
    assert not unsafe_plan.material_bindings
    assert all(step.agent != "data_agent" for step in unsafe_plan.steps)
    assert unsafe_plan.next_action == "ask_clarifying_questions"

    data_plan = create_commander_plan(
        "分析这份销售趋势数据并生成图表建议。",
        agents,
        materials=[_dataset_material("sales trend.csv")],
    )
    assert data_plan.validation_errors == []
    data_step = next(step for step in data_plan.steps if step.agent == "data_agent")
    # D5.4 已开放一份已绑定数据集的只读分析；写入、导出和字段加工仍不属于 Commander。
    # 真实父子任务和零 output 写入由更聚焦的 verify_commander_data_delegate.py 覆盖，
    # C0/C1 这里只固定规划器不会回退到已淘汰的 open_workspace 历史分支。
    assert data_step.action == "analyze_dataset"
    assert data_step.execution_mode == "execute"
    assert data_step.admission_status == "ready"
    assert data_step.required_permissions == ["file_read"]
    assert data_plan.requires_confirmation is False
    assert data_plan.next_action == "execute_after_confirm", data_plan.clarifying_questions

    print("Commander C0/C1 verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
