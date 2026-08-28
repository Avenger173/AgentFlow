"""验证 C6.5 模型路由的保存、拒绝和 Commander 审计闭环。

脚本只写临时数据目录，并把模型网络调用替换为本地协程。它验证显式路由不会借由“连接
失败后回退全局模型”掩盖配置问题，同时确认任务历史只保存脱敏 Provider/模型事实。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_model_routes_c65_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
os.environ["AGENTFLOW_CHAT_MODE"] = "llm"
os.environ["DEEPSEEK_API_KEY"] = "fixture-route-key"
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app.services.model_gateway import ModelRuntime
from main import app


async def _fake_chat(self: ModelRuntime, *, system_prompt: str, user_message: str) -> str:
    """避免真实联网，只让 API 走到 C6.5 的 Runtime 路由边界。"""

    assert self.provider == "deepseek"
    assert self.model == "deepseek-v4-flash"
    return "我会按照已生成的计划执行，并在确认后进入真实 Runtime。"


def main() -> None:
    original_chat = ModelRuntime.chat
    ModelRuntime.chat = _fake_chat
    try:
        with TestClient(app) as client:
            initial = client.get("/api/models/routes")
            initial.raise_for_status()
            routes = {item["route_id"]: item for item in initial.json()["routes"]}
            assert routes["commander_planning"]["availability"] == "ready"
            assert routes["visual_generation"]["availability"] == "reserved"

            saved = client.put(
                "/api/models/routes/commander_planning",
                json={
                    "mode": "configured",
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-flash",
                    "thinking": "enabled",
                },
            )
            saved.raise_for_status()
            payload = saved.json()
            assert payload["settings"]["mode"] == "configured"
            assert payload["resolved"]["provider"] == "deepseek"
            assert payload["resolved"]["thinking"] == "enabled"

            # 不支持 thinking 的 Provider 不能被静默接受，也不能覆写已经保存的有效路由。
            incompatible = client.put(
                "/api/models/routes/commander_planning",
                json={
                    "mode": "configured",
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-fixture",
                    "thinking": "enabled",
                },
            )
            assert incompatible.status_code == 400, incompatible.text
            assert "思考模式" in incompatible.json()["detail"]

            reserved = client.put(
                "/api/models/routes/visual_generation",
                json={"mode": "inherit_global"},
            )
            assert reserved.status_code == 400, reserved.text

            chat = client.post("/api/chat", json={"message": "请整理当前可执行能力。"})
            chat.raise_for_status()
            chat_payload = chat.json()
            assert chat_payload["model_route"]["route_id"] == "commander_planning"
            assert chat_payload["model_route"]["mode"] == "configured"
            assert chat_payload["workflow_run"]["model_routes"][0]["model"] == "deepseek-v4-flash"
            assert "fixture-route-key" not in (VERIFY_DATA_DIR / "model_route_profiles.json").read_text(encoding="utf-8")
    finally:
        ModelRuntime.chat = original_chat

    print("Commander C6.5 model route verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
