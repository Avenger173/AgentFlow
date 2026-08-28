"""验证 Commander C4 对知识库的受控只读委派。

这不是第二套 K3 问答测试：真实检索、Evidence Gate 与 claim/source_id 闭合已经由
``verify_knowledge_answer.py`` 覆盖。本脚本只替换子任务执行函数，验证总指挥不会扫描
资料库、不会把 KB ID 变成文件路径，且会把已确认的只读委派登记为可追溯父子任务。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_knowledge_c4_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.schemas.chat import WorkflowMaterialBinding
from app.schemas.knowledge import KnowledgeAnswerTaskResultResponse
from app.services.agent_catalog import list_agents
from app.services.commander import create_commander_plan
from app.workflow.dry_run import run_workflow_dry_run
from main import app


def _knowledge_base_material(ref: str) -> WorkflowMaterialBinding:
    """构造模拟知识库选择器传给 Commander 的稳定资料库绑定。"""

    return WorkflowMaterialBinding(
        binding_id="verify_knowledge_base",
        kind="knowledge_base",
        ref=ref,
        display_name="C4 回归资料库",
        origin="client_selected",
        usage="客户在知识库页面明确选择的资料库。",
    )


async def _completed_knowledge_task(*, task_id, request, model=None, progress_callback=None):
    """模拟已经由 K3 覆盖过的子任务终态，不调用模型、索引或网络。"""

    del request, model, progress_callback
    return KnowledgeAnswerTaskResultResponse(
        task_id=task_id,
        status="completed",
        summary="模拟可信问答已完成。",
        message="已根据 1 条可定位来源生成回答。",
    )


def main() -> None:
    """验证 C4 的显式范围、Runtime 委派和父子审计边界。"""

    client = TestClient(app)
    agents = list_agents()
    knowledge_agent = next(agent for agent in agents if agent.id == "knowledge_agent")
    assert knowledge_agent.runtime_ready is True
    assert knowledge_agent.permissions.model_dump() == {
        "file_read": False,
        "file_write": False,
        "network": False,
        "shell": False,
        "database": False,
    }

    admissions = client.get("/api/agents/action-admissions")
    assert admissions.status_code == 200, admissions.text
    knowledge_admission = next(
        item
        for item in admissions.json()["actions"]
        if item["agent_id"] == "knowledge_agent" and item["action"] == "answer_question"
    )
    assert knowledge_admission["execution_mode"] == "execute"
    assert knowledge_admission["material_kind"] == "knowledge_base"

    contract = client.get(
        "/api/workflow/node-contracts",
        params={"agent_id": "knowledge_agent", "action": "answer_question"},
    )
    assert contract.status_code == 200, contract.text
    contract_payload = contract.json()["contracts"]
    assert len(contract_payload) == 1
    assert contract_payload[0]["tool_name"] == "agent.knowledge_agent.answer"
    assert contract_payload[0]["required_permissions"] == []

    # 用户只说“问知识库”不能触发本机扫描；必须先在资料库页面选择一个稳定 ID。
    unbound_plan = create_commander_plan("请问知识库里的验收要求。", agents)
    assert all(step.agent != "knowledge_agent" for step in unbound_plan.steps)
    assert unbound_plan.next_action == "ask_clarifying_questions"
    assert unbound_plan.clarifying_questions

    # 不接受路径或目录跳转伪装成资料库 ID，防止聊天请求扩大可读取范围。
    unsafe_plan = create_commander_plan(
        "请问资料库中的验收要求。",
        agents,
        materials=[_knowledge_base_material("../private.sqlite")],
    )
    assert not unsafe_plan.material_bindings
    assert all(step.agent != "knowledge_agent" for step in unsafe_plan.steps)

    knowledge_base_id = "kb_c4verify01"
    plan = create_commander_plan(
        "请根据已选资料库回答验收要求，并给出来源。",
        agents,
        materials=[_knowledge_base_material(knowledge_base_id)],
    )
    assert plan.validation_errors == []
    knowledge_step = next(step for step in plan.steps if step.agent == "knowledge_agent")
    assert knowledge_step.action == "answer_question"
    assert knowledge_step.execution_mode == "execute"
    assert knowledge_step.admission_status == "ready"
    assert knowledge_step.required_permissions == []
    assert knowledge_step.input["knowledge_base_id"] == knowledge_base_id
    assert knowledge_step.input["query"] == "请根据已选资料库回答验收要求，并给出来源。"
    assert plan.workspace_scope.read_paths == [f"knowledge-base://{knowledge_base_id}"]
    assert all("data/" not in path for path in plan.workspace_scope.read_paths)

    source_task_id = "verify_commander_knowledge_parent"
    dry_run = run_workflow_dry_run(task_id=source_task_id, plan=plan, available_agents=agents)
    assert dry_run.status == "completed", dry_run.validation_errors

    # 父 Runtime 只调用既有 K3 子任务入口。这里使用固定终态替身，确保本回归不会消耗模型
    # 额度；K3 的真实 Gate/来源质量由独立脚本验证。
    with patch("app.workflow.runtime.run_knowledge_answer_task", _completed_knowledge_task):
        execution = client.post(f"/api/tasks/{source_task_id}/execute")
    assert execution.status_code == 200, execution.text
    payload = execution.json()
    assert payload["status"] == "completed"
    runtime_step = next(
        item for item in payload["workflow_run"]["steps"] if item["agent"] == "knowledge_agent"
    )
    result = runtime_step["output"]["result"]
    assert runtime_step["status"] == "completed"
    assert result["agent_status"] == "completed"
    assert result["delegated_task_id"].startswith("task_kb_")
    assert result["reply"] == "已根据 1 条可定位来源生成回答。"
    assert "knowledge-base://" not in str(result)

    artifacts = client.get(f"/api/tasks/{payload['runtime_task_id']}/artifacts")
    assert artifacts.status_code == 200, artifacts.text
    delegation = next(
        item for item in artifacts.json()["artifacts"] if item["agent_id"] == "knowledge_agent"
    )
    assert delegation["name"] == "知识库问答结果"
    assert delegation["uri"] == f"agentflow-task://{result['delegated_task_id']}"
    assert delegation["metadata"]["delegated_task_id"] == result["delegated_task_id"]

    print("Commander C4 knowledge route verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
