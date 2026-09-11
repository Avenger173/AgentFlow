"""验证跨会话历史回顾的动作边界、范围隔离和正常 PPT 路由。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_history_recall_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))


def _save_request(*, scope: str, user_message: str) -> None:
    from app.database.conversation_repository import create_conversation, save_conversation_turn

    session = create_conversation(project_scope=scope)
    save_conversation_turn(
        conversation_id=session.conversation_id,
        user_message=user_message,
        assistant_message="已记录该请求。",
        material_bindings=[],
        task_id="task_history_fixture",
        plan_id="",
    )


def main() -> None:
    from fastapi.testclient import TestClient

    from app.schemas.chat import ChatRequest
    from app.services.agent_catalog import get_agent
    from app.services.llm_chat import create_llm_chat_response
    from main import app

    alpha_scope = "project:history-alpha"
    beta_scope = "project:history-beta"
    _save_request(
        scope=alpha_scope,
        user_message="帮我生成凯恩的生涯 PPT，要有数据、表格、折线图、柱状图和饼图。",
    )
    _save_request(
        scope=alpha_scope,
        user_message="帮我生成球星莱万多夫斯基的生涯 PPT，要有数据支撑。",
    )
    _save_request(
        scope=beta_scope,
        user_message="帮我生成仅属于 beta 项目的隐私经营复盘 PPT。",
    )

    with TestClient(app) as client:
        recall = client.post(
            "/api/chat",
            json={
                "project_scope": alpha_scope,
                "message": "我叫你生成过什么内容的 PPT？",
            },
        )
        assert recall.status_code == 200, recall.text
        recall_body = recall.json()
        assert recall_body["mode"] == "history_recall"
        assert recall_body["workflow_plan"] is None
        assert recall_body["workflow_run"] is None
        assert "凯恩" in recall_body["reply"]
        assert "莱万多夫斯基" in recall_body["reply"]
        assert "beta 项目" not in recall_body["reply"]

        # 即使直接进入真实模型服务，回顾请求也必须在解析运行时配置前短路。
        commander = get_agent("commander_agent")
        assert commander is not None
        llm_recall = asyncio.run(
            create_llm_chat_response(
                request=ChatRequest(
                    project_scope=alpha_scope,
                    message="我叫你生成过什么内容的 PPT？",
                ),
                agent=commander,
                message="我叫你生成过什么内容的 PPT？",
            )
        )
        assert llm_recall.mode == "history_recall"
        assert llm_recall.model is None
        assert llm_recall.workflow_plan is None
        assert "凯恩" in llm_recall.reply

        repeated_recall = client.post(
            "/api/chat",
            json={
                "project_scope": alpha_scope,
                "message": "我之前让你生成过哪些 PPT？",
            },
        )
        assert repeated_recall.status_code == 200, repeated_recall.text
        repeated_body = repeated_recall.json()
        assert "凯恩" in repeated_body["reply"]
        assert "莱万多夫斯基" in repeated_body["reply"]
        assert "我叫你生成过什么内容" not in repeated_body["reply"]

        isolated_recall = client.post(
            "/api/chat",
            json={
                "project_scope": beta_scope,
                "message": "我叫你生成过什么内容的 PPT？",
            },
        )
        assert isolated_recall.status_code == 200, isolated_recall.text
        isolated_body = isolated_recall.json()
        assert isolated_body["workflow_plan"] is None
        assert "beta 项目" in isolated_body["reply"]
        assert "凯恩" not in isolated_body["reply"]

        creation = client.post(
            "/api/chat",
            json={
                "project_scope": alpha_scope,
                "message": "帮我生成一份球星职业生涯 PPT。",
            },
        )
        assert creation.status_code == 200, creation.text
        creation_body = creation.json()
        assert creation_body["workflow_plan"] is not None
        assert creation_body["workflow_plan"]["next_action"] == "open_presentation_studio"
        assert any(
            step["action"] == "open_presentation_studio"
            for step in creation_body["workflow_plan"]["steps"]
        )

    print("Conversation history recall verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
