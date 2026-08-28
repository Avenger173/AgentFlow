"""C6.6 `@Agent` 路由偏好的离线回归。

不读取客户文件、不调用模型或网络。它只验证聊天文本和客户端标签最终会收束为同一份
Commander 计划，且显式偏好不能绕过材料选择、动作准入或 Native 组合 Runtime 边界。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def _material(kind: str, ref: str, display_name: str):
    from app.schemas.chat import WorkflowMaterialBinding

    return WorkflowMaterialBinding(
        binding_id=f"verify_{kind}",
        kind=kind,
        ref=ref,
        display_name=display_name,
        origin="client_selected",
        usage="C6.6 离线验证材料。",
    )


def _specialist_agents(plan):
    return [step.agent for step in plan.steps if step.agent != "commander_agent"]


def main() -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="agentflow_c66_hints_"))
    os.environ["AGENTFLOW_DATA_DIR"] = str(temp_dir)
    os.environ["AGENTFLOW_CHAT_MODE"] = "mock"

    try:
        from fastapi.testclient import TestClient

        from app.services.agent_catalog import list_agents
        from app.services.commander import create_commander_plan
        from main import app

        agents = list_agents()
        all_materials = [
            _material("document", "c66_brief.md", "项目说明.md"),
            _material("dataset", "c66_sales.csv", "销售.csv"),
            _material("knowledge_base", "kb_c66verify", "验收资料库"),
        ]

        document_only = create_commander_plan(
            "请 @文档助手 只分析已选说明并给出结论。",
            available_agents=agents,
            materials=all_materials,
        )
        assert [hint.agent_id for hint in document_only.agent_hints] == ["document_agent"]
        assert _specialist_agents(document_only) == ["document_agent"]
        assert document_only.workspace_scope.read_paths == ["data/workspaces/c66_brief.md"]
        assert document_only.validation_errors == [], document_only.validation_errors

        missing_data = create_commander_plan(
            "@数据工作台 请分析趋势。",
            available_agents=agents,
            materials=[all_materials[0]],
        )
        assert [hint.agent_id for hint in missing_data.agent_hints] == ["data_agent"]
        assert "data_agent" not in _specialist_agents(missing_data)
        assert any("已点名 @数据工作台" in item for item in missing_data.clarifying_questions)
        assert missing_data.next_action == "ask_clarifying_questions"

        composed = create_commander_plan(
            "@文档助手 @数据工作台 @知识库 请分别处理当前材料后汇总。",
            available_agents=agents,
            materials=all_materials,
        )
        assert [hint.agent_id for hint in composed.agent_hints] == [
            "document_agent",
            "data_agent",
            "knowledge_agent",
        ]
        specialist_steps = [step for step in composed.steps if step.agent != "commander_agent"]
        assert {step.agent for step in specialist_steps} == {
            "document_agent",
            "data_agent",
            "knowledge_agent",
        }
        assert all(step.parallel_group == "specialist_read_only" for step in specialist_steps)
        assert composed.execution_readiness == "ready"
        assert composed.next_action == "execute_after_confirm"
        assert composed.validation_errors == [], composed.validation_errors

        # API 客户端只能提交枚举中的运行时能力；未知标签不能作为动态 Agent ID 混入后端。
        client = TestClient(app)
        rejected = client.post(
            "/api/chat",
            json={
                "message": "请处理当前材料。",
                "agent_hints": [{"agent_id": "code_agent", "source": "mention"}],
            },
        )
        assert rejected.status_code == 422, rejected.text

        accepted = client.post(
            "/api/chat",
            json={
                "message": "@知识库 请回答问题。",
                "materials": [all_materials[2].model_dump(mode="json")],
            },
        )
        assert accepted.status_code == 200, accepted.text
        payload = accepted.json()
        assert payload["workflow_plan"]["agent_hints"] == [
            {"agent_id": "knowledge_agent", "source": "mention"}
        ]

        print("Commander C6.6 agent hints verification passed.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
