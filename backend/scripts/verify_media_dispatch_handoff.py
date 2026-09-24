"""验证调度台到图片工作区的安全交接契约。

不读取客户图片、不调用 Provider 或网络。该脚本固定验证图片编辑只会生成工作区引导，
而不是从聊天入口隐式导入文件或创建 AI 修图任务。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def _specialist_steps(plan):
    return [step for step in plan.steps if step.agent != "commander_agent"]


def main() -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="agentflow_media_dispatch_"))
    os.environ["AGENTFLOW_DATA_DIR"] = str(temp_dir)
    os.environ["AGENTFLOW_CHAT_MODE"] = "mock"

    try:
        from fastapi.testclient import TestClient

        from app.services.agent_catalog import list_agents
        from app.services.commander import create_commander_plan
        from main import app

        agents = list_agents()
        media_agent = next(agent for agent in agents if agent.id == "media_agent")
        assert media_agent.enabled is True
        assert media_agent.health == "ready"
        assert media_agent.runtime_ready is False
        assert media_agent.maturity == "experimental"

        plan = create_commander_plan(
            "@图片助手 把人物身后的杂物去掉，其他区域保持不变。",
            available_agents=agents,
        )
        assert [hint.agent_id for hint in plan.agent_hints] == ["media_agent"]
        specialist_steps = _specialist_steps(plan)
        assert [(step.agent, step.action) for step in specialist_steps] == [
            ("media_agent", "open_media_workspace")
        ]
        handoff = specialist_steps[0]
        assert handoff.execution_mode == "guided_handoff"
        assert handoff.required_permissions == []
        assert handoff.requires_confirmation is False
        assert handoff.input["instruction"] == "把人物身后的杂物去掉，其他区域保持不变。"
        assert plan.intent == "media_image_edit"
        assert plan.next_action == "open_media_workspace"
        assert plan.workspace_scope.read_paths == []
        assert plan.workspace_scope.write_paths == []
        assert plan.workspace_scope.external_services == []
        assert plan.validation_errors == [], plan.validation_errors

        natural_plan = create_commander_plan(
            "把产品图的背景换成浅灰色，主体保持不变。",
            available_agents=agents,
        )
        assert natural_plan.next_action == "open_media_workspace"
        assert any(step.agent == "media_agent" for step in natural_plan.steps)

        # PPT 语义优先级高于图片编辑词，避免“PPT 里放一张图片”被错误转入图片工作区。
        presentation_plan = create_commander_plan(
            "请制作产品介绍 PPT，并在其中放一张图片。",
            available_agents=agents,
        )
        assert presentation_plan.next_action == "open_presentation_studio"
        assert not any(step.agent == "media_agent" for step in presentation_plan.steps)

        client = TestClient(app)
        response = client.post(
            "/api/chat",
            json={
                "message": "@图片助手 把背景换成浅灰色，人物保持不变。",
                "agent_hints": [{"agent_id": "media_agent", "source": "mention"}],
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        plan_payload = payload["workflow_plan"]
        assert plan_payload["agent_hints"] == [{"agent_id": "media_agent", "source": "mention"}]
        assert plan_payload["next_action"] == "open_media_workspace"
        media_step = next(
            step for step in plan_payload["steps"] if step["agent"] == "media_agent"
        )
        assert media_step["action"] == "open_media_workspace"
        assert media_step["input"]["instruction"] == "把背景换成浅灰色，人物保持不变。"
        assert media_step["required_permissions"] == []

        print("Media dispatch handoff verification passed.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
