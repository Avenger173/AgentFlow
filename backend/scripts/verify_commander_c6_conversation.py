"""验证 C6.2/C6.2.5 会话上下文、完整归档和材料连续性，不调用真实模型。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_c6_conversation_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.database.conversation_repository import get_conversation_context
from main import app


def _knowledge_material() -> dict[str, str]:
    return {
        "binding_id": "c6_conversation_kb",
        "kind": "knowledge_base",
        "ref": "kb_c6conv01",
        "display_name": "会话回归资料库",
        "origin": "client_selected",
        "usage": "客户在第一轮明确交给总指挥的资料库。",
    }


def _knowledge_step(payload: dict) -> dict:
    return next(step for step in payload["workflow_plan"]["steps"] if step["agent"] == "knowledge_agent")


def main() -> None:
    with TestClient(app) as client:
        first = client.post(
            "/api/chat",
            json={
                "message": "请根据资料库回答 Agent 如何制作。",
                "materials": [_knowledge_material()],
            },
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        conversation_id = first_body["conversation_id"]
        assert conversation_id.startswith("conv_")
        assert _knowledge_step(first_body)["action"] == "answer_question"
        restored_after_first = client.get(f"/api/chat/conversations/{conversation_id}")
        assert restored_after_first.status_code == 200, restored_after_first.text
        restored_after_first_body = restored_after_first.json()
        # 会话恢复和会话列表都应显示客户任务的短标题，而不是 task_llm_*、@ 路由标记或
        # 整段原始输入。聊天响应本身只传稳定 conversation_id，不重复会话元数据。
        assert restored_after_first_body["session"]["title"] == "根据资料库回答 Agent 如何制作。"
        restored_messages = restored_after_first_body["recent_messages"]
        assert [item["role"] for item in restored_messages] == ["user", "assistant"]

        # 第二轮故意不再传 materials，只用明确指代。服务应复用同会话已确认资料库，而不是
        # 扫描本机或要求客户再次上传。
        second = client.post(
            "/api/chat",
            json={
                "conversation_id": conversation_id,
                "message": "请按上一步计划继续，回答刚才那份资料库中的 Agent 工具层。",
            },
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["conversation_id"] == conversation_id
        assert _knowledge_step(second_body)["input"]["knowledge_base_id"] == "kb_c6conv01"
        assert "复用了同一会话此前明确选择的材料范围" in second_body["workflow_plan"]["conversation_context_summary"][0]

        # 继续多轮，验证近轮数有硬上限，较早文本会被确定性摘要替代，避免无限长 Prompt。
        # 同时 C6.2.5 必须仍保留完整脱敏归档，不能再为了控制 Prompt 删除客户已看过的消息。
        for index in range(4):
            response = client.post(
                "/api/chat",
                json={
                    "conversation_id": conversation_id,
                    "message": f"继续按刚才资料库说明第 {index + 1} 个工程要点。",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["conversation_id"] == conversation_id

        context = get_conversation_context(conversation_id)
        assert len(context.recent_messages) == 8
        assert context.session.summary
        assert context.session.material_bindings[0].ref == "kb_c6conv01"

        transcript = client.get(
            f"/api/chat/conversations/{conversation_id}/messages",
            params={"project_scope": "global", "limit": 100},
        )
        assert transcript.status_code == 200, transcript.text
        transcript_body = transcript.json()
        assert transcript_body["total"] == 12
        assert len(transcript_body["messages"]) == 12
        assert [item["role"] for item in transcript_body["messages"][:2]] == ["user", "assistant"]
        assert "请根据资料库回答" in transcript_body["messages"][0]["content"]

        session_list = client.get("/api/chat/conversations", params={"project_scope": "global"})
        assert session_list.status_code == 200, session_list.text
        listed = next(
            item for item in session_list.json()["conversations"] if item["conversation_id"] == conversation_id
        )
        assert listed["archived_message_count"] == 12
        assert listed["title"] == "根据资料库回答 Agent 如何制作。"

        # 客户端重启后只通过稳定 ID 恢复有限近轮和确定性摘要；不存在的 ID 不会被读取接口
        # 隐式创建成空会话。
        restored = client.get(f"/api/chat/conversations/{conversation_id}")
        assert restored.status_code == 200, restored.text
        restored_body = restored.json()
        assert restored_body["session"]["conversation_id"] == conversation_id
        assert len(restored_body["recent_messages"]) == 8
        missing = client.get("/api/chat/conversations/conv_missing000000")
        assert missing.status_code == 404, missing.text

        # 空会话不能凭“刚才”跨会话获得上一段资料库的读取范围。
        isolated = client.post(
            "/api/chat",
            json={"message": "请继续回答刚才那份资料库。"},
        )
        assert isolated.status_code == 200, isolated.text
        isolated_body = isolated.json()
        assert isolated_body["conversation_id"] != conversation_id
        assert all(step["agent"] != "knowledge_agent" for step in isolated_body["workflow_plan"]["steps"])

        # project_scope 改变会切出新会话，避免 project:A 的材料进入 project:B。
        scoped = client.post(
            "/api/chat",
            json={
                "conversation_id": conversation_id,
                "project_scope": "project:c6-isolated",
                "message": "请继续回答刚才那份资料库。",
            },
        )
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["conversation_id"] != conversation_id
        assert all(step["agent"] != "knowledge_agent" for step in scoped.json()["workflow_plan"]["steps"])

        # 会话切换列表与正文分页不能跨项目范围泄漏。即使客户知道旧 ID，也不能在 project:B
        # 读取 project:global 的对话归档。
        scoped_list = client.get("/api/chat/conversations", params={"project_scope": "project:c6-isolated"})
        assert scoped_list.status_code == 200, scoped_list.text
        assert all(item["conversation_id"] != conversation_id for item in scoped_list.json()["conversations"])
        cross_scope = client.get(
            f"/api/chat/conversations/{conversation_id}/messages",
            params={"project_scope": "project:c6-isolated"},
        )
        assert cross_scope.status_code == 404, cross_scope.text

    print("Commander C6.2/C6.2.5 conversation archive verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
