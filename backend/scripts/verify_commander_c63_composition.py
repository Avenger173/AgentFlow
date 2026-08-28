"""验证 C6.3 的多材料组合计划结构。

本脚本只验证确定性规划与 Runtime 准入，不读取客户文件、不调用模型、不联网。重点是：
多份已显式选择的材料会形成可审阅的依赖图和并行组。实际并发、失败隔离和结果汇总由
``verify_commander_c64_runtime.py`` 单独验证，避免计划结构回归偷偷变成真实模型或文件测试。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_c63_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.schemas.chat import WorkflowMaterialBinding
from app.services.agent_catalog import list_agents
from app.services.commander import create_commander_plan
from app.workflow.dry_run import run_workflow_dry_run
from main import app


def _material(kind: str, ref: str, name: str) -> WorkflowMaterialBinding:
    """构造客户已显式选择的受控引用，不包含正文或绝对路径。"""

    return WorkflowMaterialBinding(
        binding_id=f"verify_{kind}",
        kind=kind,
        ref=ref,
        display_name=name,
        origin="client_selected",
        usage="C6.3 组合计划隔离回归的明确材料。",
    )


def main() -> None:
    agents = list_agents()
    materials = [
        _material("document", "project_brief.md", "项目说明.md"),
        _material("dataset", "sales_2026.csv", "销售数据.csv"),
        _material("knowledge_base", "kb_c63verify", "产品资料库"),
    ]
    goal = "请结合已选文档、数据和资料库，梳理项目目标、数据趋势与资料依据。"
    plan = create_commander_plan(goal, available_agents=agents, materials=materials)

    assert plan.validation_errors == [], plan.validation_errors
    assert plan.execution_readiness == "ready"
    assert plan.next_action == "execute_after_confirm"

    specialist_steps = [
        step
        for step in plan.steps
        if step.agent in {"document_agent", "data_agent", "knowledge_agent"}
    ]
    assert {step.agent for step in specialist_steps} == {
        "document_agent",
        "data_agent",
        "knowledge_agent",
    }
    assert all(step.depends_on == ["step_1"] for step in specialist_steps)
    assert all(step.parallel_group == "specialist_read_only" for step in specialist_steps)

    synthesis = next(step for step in plan.steps if step.action == "synthesize_results")
    assert synthesis.execution_mode == "execute"
    assert set(synthesis.depends_on) == {step.id for step in specialist_steps}
    assert synthesis.input["composition_mode"] == "native_read_only_c6_4"

    dry_run = run_workflow_dry_run(
        task_id="verify_commander_c63_plan",
        plan=plan,
        available_agents=agents,
    )
    assert dry_run.status == "completed", dry_run.validation_errors

    # API 层只核对新计划的可执行性协议；真实 Runtime 由 C6.4 的脱敏 fixture 覆盖，
    # 这里不创建客户材料、不调用模型，也不触发真正的子任务。
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "message": goal,
                "materials": [item.model_dump() for item in materials],
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["workflow_plan"]["execution_readiness"] == "ready"
        assert payload["workflow_plan"]["next_action"] == "execute_after_confirm"

    print("Commander C6.3 composition-plan structure verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
