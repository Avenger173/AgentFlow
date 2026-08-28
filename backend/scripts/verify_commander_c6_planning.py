"""验证 C6.1 的计划/回复一致性，不调用真实模型或客户资料。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_c6_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "llm"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from main import app
from app.schemas.model import ModelRouteAuditSnapshot
from app.services.conversation_memory import persist_async_assistant_delivery


class _ConflictingPlannerRuntime:
    """故意返回客户反馈中的错误话术，验证服务端不会把它直接显示出来。"""

    model = "c6-planning-fixture"
    received_system_prompt = ""

    async def chat(self, *, system_prompt: str, user_message: str) -> str:
        del user_message
        type(self).received_system_prompt = system_prompt
        return "我目前无法直接访问您提到的资料库，因为没有对应检索工具。"


class _FixtureRouteResolution:
    """跟随 C6.5 路由解析协议的最小 fixture，不读取本地模型配置或密钥。"""

    def __init__(self, runtime: _ConflictingPlannerRuntime) -> None:
        self.runtime = runtime

    def audit_snapshot(self, *, stage: str = "") -> ModelRouteAuditSnapshot:
        return ModelRouteAuditSnapshot(
            stage=stage,
            route_id="commander_planning",
            profile_id="route:fixture",
            mode="inherit_global",
            provider="fixture",
            label="C6 planning fixture",
            model="c6-planning-fixture",
            thinking="disabled",
            compatibility="ready",
            note="离线验证 fixture",
        )


def main() -> None:
    client = TestClient(app)
    runtime = _ConflictingPlannerRuntime()
    payload = {
        "message": "请根据资料库“C6 回归资料库”回答 Agent 如何制作。",
        "agent_id": "commander_agent",
        "materials": [
            {
                "binding_id": "verify_c6_knowledge",
                "kind": "knowledge_base",
                "ref": "kb_c6verify01",
                "display_name": "C6 回归资料库",
                "origin": "client_selected",
                "usage": "C6 回归明确选择的资料库。",
            }
        ],
    }
    # C6.5 后聊天代码解析的是带审计快照的 Route Resolution，而不是直接拿 Runtime。
    with patch(
        "app.services.llm_chat.resolve_model_runtime_for_route",
        return_value=_FixtureRouteResolution(runtime),
    ):
        response = client.post("/api/chat", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "llm"
    assert "当前阶段：dry-run" in _ConflictingPlannerRuntime.received_system_prompt
    assert "knowledge_agent.answer_question" in _ConflictingPlannerRuntime.received_system_prompt
    assert "无法直接访问" not in body["reply"]
    assert "正在检索已选资料库" in body["reply"]
    knowledge_step = next(
        step for step in body["workflow_plan"]["steps"] if step["agent"] == "knowledge_agent"
    )
    assert knowledge_step["action"] == "answer_question"
    assert body["workflow_run"]["mode"] == "dry_run"
    context = persist_async_assistant_delivery(
        conversation_id=body["conversation_id"],
        task_id="task_kb_c6delivery",
        assistant_message="## 已完成\n\n这是来源核验后的最终回答。",
    )
    # Runtime 恢复时会再次收束；同一子任务的会话交付只能保留一份。
    context = persist_async_assistant_delivery(
        conversation_id=body["conversation_id"],
        task_id="task_kb_c6delivery",
        assistant_message="## 已完成\n\n这是来源核验后的最终回答。",
    )
    deliveries = [message for message in context.recent_messages if message.task_id == "task_kb_c6delivery"]
    assert len(deliveries) == 1
    assert "最终回答" in deliveries[0].content
    print("Commander C6.1 planning alignment verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
