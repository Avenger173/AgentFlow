"""验证 Commander C5 对 K4 深度总结的受控后台委派。

本脚本不调用真实模型、不读取真实资料正文。它替换后台受理 Adapter，重点验证：深度意图
必须有显式资料库绑定、预算确认不会被普通权限策略跳过、父 Runtime 只记录“已受理”并以
``agentflow-task://`` 关联 K4 子任务，而不是伪称子任务已完成。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_knowledge_c5_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.database.task_repository import WorkflowTaskProgressSnapshot
from app.schemas.chat import WorkflowMaterialBinding, WorkflowPlanPreferences
from app.services.agent_catalog import list_agents
from app.services.commander import create_commander_plan
from app.services.knowledge_deep_dispatch import KnowledgeDeepTaskDispatchReceipt
from app.workflow.dry_run import run_workflow_dry_run
from app.workflow.permission_policy import evaluate_permission_policy
from main import app


def _knowledge_base_material(ref: str) -> WorkflowMaterialBinding:
    """构造知识库页面已明确选择的稳定 ID，不传路径或正文。"""

    return WorkflowMaterialBinding(
        binding_id="verify_knowledge_base",
        kind="knowledge_base",
        ref=ref,
        display_name="C5 深度委派资料库",
        origin="client_selected",
        usage="客户在知识库页面明确选择，用于全库深度总结。",
    )


def _accepted_deep_task(_request) -> KnowledgeDeepTaskDispatchReceipt:
    """模拟 K4 已冻结范围并进入后台，不触发任何模型或线程。"""

    return KnowledgeDeepTaskDispatchReceipt(task_id="task_k4_c5verify01", map_unit_count=37)


def _running_deep_task_snapshot(_task_id: str) -> WorkflowTaskProgressSnapshot:
    """模拟 K4 checkpoint 的聚合计数，不构造正文、来源或模型输出。"""

    return WorkflowTaskProgressSnapshot(
        task_id="task_k4_c5verify01",
        status="running",
        summary="知识库深度任务正在执行逐章节 Map 分析。",
        action_status_counts={
            ("knowledge.deep_map", "completed"): 11,
            ("knowledge.deep_map", "running"): 1,
            ("knowledge.deep_map", "pending"): 25,
        },
    )


def main() -> None:
    """覆盖 C5 的计划、确认、父子审计和全库范围边界。"""

    client = TestClient(app)
    agents = list_agents()

    # 仅有深度意图也不能触发本机资料库扫描，必须回到选择器绑定范围。
    unbound_plan = create_commander_plan("请对知识库做深度总结。", agents)
    assert all(step.action != "deep_summary" for step in unbound_plan.steps)
    assert unbound_plan.next_action == "ask_clarifying_questions"
    assert unbound_plan.clarifying_questions

    knowledge_base_id = "kb_c5verify01"
    plan = create_commander_plan(
        "请对已选资料库做全库深度总结，并梳理关键结论。",
        agents,
        materials=[_knowledge_base_material(knowledge_base_id)],
    )
    assert plan.validation_errors == []
    deep_step = next(step for step in plan.steps if step.action == "deep_summary")
    assert deep_step.input == {
        "knowledge_base_id": knowledge_base_id,
        "task_goal": "请对已选资料库做全库深度总结，并梳理关键结论。",
        "task_kind": "summary",
        "delegation_mode": "background_child_task",
    }
    assert deep_step.required_permissions == ["knowledge_deep_analysis"]
    assert deep_step.requires_confirmation is True
    assert deep_step.risk_level == "medium"
    assert plan.budget_estimate.time_level == "high"
    assert plan.budget_estimate.model_cost_level == "high"
    assert plan.next_action == "review_plan_and_confirm_permissions"
    assert plan.workspace_scope.read_paths == [f"knowledge-base://{knowledge_base_id}"]

    # 这是客户确认的预算动作，不是“完全访问”可以静默绕过的文件权限。四种策略都必须停在
    # 明确确认点，后续同类长任务可以沿用这一产品级 Permission 类型。
    for permission_policy in ("always_ask", "smart_confirm", "auto_approve", "full_access"):
        policy_plan = create_commander_plan(
            "请对已选资料库做全库深度总结，并梳理关键结论。",
            agents,
            preferences=WorkflowPlanPreferences(permission_policy=permission_policy),
            materials=[_knowledge_base_material(knowledge_base_id)],
        )
        policy_step = next(step for step in policy_plan.steps if step.action == "deep_summary")
        assert evaluate_permission_policy(permission_policy=permission_policy, step=policy_step).action == "confirm"

    admissions = client.get("/api/agents/action-admissions")
    assert admissions.status_code == 200, admissions.text
    deep_admission = next(
        item for item in admissions.json()["actions"]
        if item["agent_id"] == "knowledge_agent" and item["action"] == "deep_summary"
    )
    assert deep_admission["execution_mode"] == "execute"
    assert deep_admission["material_kind"] == "knowledge_base"

    contract = client.get(
        "/api/workflow/node-contracts",
        params={"agent_id": "knowledge_agent", "action": "deep_summary"},
    )
    assert contract.status_code == 200, contract.text
    assert contract.json()["contracts"][0]["tool_name"] == "agent.knowledge_agent.deep_summary"

    source_task_id = "verify_commander_knowledge_deep_parent"
    dry_run = run_workflow_dry_run(task_id=source_task_id, plan=plan, available_agents=agents)
    assert dry_run.status == "completed", dry_run.validation_errors

    # 第一次执行必须停在预算确认，不能因“只读”或全库分析的后台形式被自动启动。
    initial = client.post(f"/api/tasks/{source_task_id}/execute")
    assert initial.status_code == 200, initial.text
    runtime_task_id = initial.json()["runtime_task_id"]
    assert initial.json()["status"] == "waiting_permission"
    permissions = client.get(f"/api/tasks/{runtime_task_id}/permissions")
    assert permissions.status_code == 200, permissions.text
    permission = permissions.json()["permissions"][0]
    assert permission["request"]["permissions"] == ["knowledge_deep_analysis"]

    decision = client.post(
        f"/api/tasks/{runtime_task_id}/permissions/{permission['request']['request_id']}/decision",
        json={"decision": "approved", "decided_by": "verify_c5"},
    )
    assert decision.status_code == 200, decision.text

    with patch("app.workflow.runtime.start_knowledge_deep_task_in_background", _accepted_deep_task):
        execution = client.post(f"/api/tasks/{runtime_task_id}/execute")
    assert execution.status_code == 200, execution.text
    payload = execution.json()
    assert payload["status"] == "completed"
    runtime_step = next(item for item in payload["workflow_run"]["steps"] if item["action"] == "deep_summary")
    result = runtime_step["output"]["result"]
    assert runtime_step["status"] == "completed"
    assert result["handoff_state"] == "accepted"
    assert result["agent_status"] == "queued"
    assert result["delegated_task_id"] == "task_k4_c5verify01"
    assert result["scope_map_count"] == 37
    assert "仍在关联子任务后台执行" in result["reply"]
    assert "尚未在父任务内完成" in payload["workflow_run"]["summary"]

    artifacts = client.get(f"/api/tasks/{runtime_task_id}/artifacts")
    assert artifacts.status_code == 200, artifacts.text
    delegation = next(item for item in artifacts.json()["artifacts"] if item["step_id"] == deep_step.id)
    assert delegation["name"] == "知识库深度总结任务"
    assert delegation["uri"] == "agentflow-task://task_k4_c5verify01"
    assert delegation["metadata"]["delegated_task_id"] == "task_k4_c5verify01"

    # C5.2：父任务不把“子任务受理”误显示为最终结果。updates 只读取 K4 的状态与 SQL
    # 聚合计数；没有读取 scope、章节摘要、来源，也不会再调用模型。
    with patch(
        "app.workflow.updates.load_workflow_task_progress_snapshot",
        _running_deep_task_snapshot,
    ):
        updates = client.get(f"/api/tasks/{runtime_task_id}/updates")
    assert updates.status_code == 200, updates.text
    state_snapshot = next(item for item in updates.json()["updates"] if item["event"] == "task_state_snapshot")
    delegation_snapshot = state_snapshot["payload"]["task_retrospective"]["delegations"][0]
    assert delegation_snapshot["task_id"] == "task_k4_c5verify01"
    assert delegation_snapshot["status"] == "running"
    assert delegation_snapshot["status_source"] == "child_checkpoint"
    assert delegation_snapshot["map_completed"] == 11
    assert delegation_snapshot["map_total"] == 37
    assert delegation_snapshot["reduce_total"] == 0
    assert "仍在执行" in delegation_snapshot["next_action"]

    print("Commander C5 knowledge deep delegation verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
