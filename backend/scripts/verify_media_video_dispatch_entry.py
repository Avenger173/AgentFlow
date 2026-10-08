"""验证短视频素材受控上传与调度台交接的最小闭环。

该脚本只用临时目录、FastAPI TestClient 和内存媒体字节。它验证上传响应不泄露客户路径，
并确认总指挥只交接一段已选择素材与目标，不会在这一入口提交转写、候选片段或渲染。
不调用模型、FFmpeg 或网络。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_video_dispatch_entry_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_OUTPUT_DIR"] = str(VERIFY_ROOT / "output")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
sys.path.insert(0, str(BACKEND_ROOT))


def _specialist_steps(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        step
        for step in plan["steps"]  # type: ignore[index]
        if step["agent"] != "commander_agent"  # type: ignore[index]
    ]


def main() -> None:
    from fastapi.testclient import TestClient

    from main import create_app

    try:
        with TestClient(create_app()) as client:
            project_response = client.post(
                "/api/agents/media_agent/projects",
                json={"title": "短视频调度入口验收"},
            )
            assert project_response.status_code == 201, project_response.text
            project_id = project_response.json()["project_id"]

            upload_response = client.post(
                f"/api/agents/media_agent/projects/{project_id}/media-sources/upload",
                files={"file": ("产品演示.mp4", b"synthetic-video-fixture", "video/mp4")},
            )
            assert upload_response.status_code == 201, upload_response.text
            source = upload_response.json()
            assert source["source_id"].startswith("ms_")
            assert source["project_scope"] == project_id
            assert source["filename"] == "产品演示.mp4"
            assert source["size_bytes"] == len(b"synthetic-video-fixture")
            assert "path" not in source
            assert str(VERIFY_ROOT) not in str(source)

            material = {
                "binding_id": "verify_video_source",
                "kind": "media_source",
                "ref": source["source_id"],
                "display_name": source["filename"],
                "origin": "client_selected",
                "usage": "短视频调度入口离线验收素材。",
            }
            response = client.post(
                "/api/chat",
                json={
                    "message": "@多媒体助手 保留介绍产品功能的片段，生成待确认的剪辑候选。",
                    "agent_hints": [{"agent_id": "media_agent", "source": "mention"}],
                    "materials": [material],
                },
            )
            assert response.status_code == 200, response.text
            plan = response.json()["workflow_plan"]
            steps = _specialist_steps(plan)
            assert [(step["agent"], step["action"]) for step in steps] == [
                ("media_agent", "open_video_workspace")
            ]
            handoff = steps[0]
            assert plan["intent"] == "media_video_edit"
            assert plan["next_action"] == "open_video_workspace"
            assert handoff["execution_mode"] == "guided_handoff"
            assert handoff["admission_status"] == "guided"
            assert handoff["input"] == {
                "task_goal": "@多媒体助手 保留介绍产品功能的片段，生成待确认的剪辑候选。",
                "source_id": source["source_id"],
            }
            assert handoff["required_permissions"] == []
            assert plan["workspace_scope"]["read_paths"] == []
            assert plan["workspace_scope"]["write_paths"] == []
            assert plan["workspace_scope"]["external_services"] == []
            assert "转写" in handoff["expected_output"]
            assert "尚未" in handoff["expected_output"]

            followup = client.post(
                "/api/chat",
                json={
                    "message": "@多媒体助手 把刚才那版剪辑改短一些，只保留操作步骤并导出字幕。",
                    "agent_hints": [{"agent_id": "media_agent", "source": "mention"}],
                    "materials": [material],
                },
            )
            assert followup.status_code == 200, followup.text
            followup_plan = followup.json()["workflow_plan"]
            assert followup_plan["intent"] == "media_video_edit"
            assert followup_plan["next_action"] == "open_video_workspace"
            assert [(step["agent"], step["action"]) for step in _specialist_steps(followup_plan)] == [
                ("media_agent", "open_video_workspace")
            ]
            assert followup_plan["workspace_scope"]["external_services"] == []

            brief = client.post(
                "/api/chat",
                json={
                    "message": "@多媒体助手 把这段视频整理成有章节、关键画面和动效的讲解网页。",
                    "agent_hints": [{"agent_id": "media_agent", "source": "mention"}],
                    "materials": [material],
                },
            )
            assert brief.status_code == 200, brief.text
            brief_plan = brief.json()["workflow_plan"]
            assert brief_plan["intent"] == "media_video_edit"
            assert brief_plan["next_action"] == "open_video_workspace"
            brief_steps = _specialist_steps(brief_plan)
            assert [(step["agent"], step["action"]) for step in brief_steps] == [
                ("media_agent", "open_video_workspace")
            ]
            assert "讲解网页" in brief_steps[0]["expected_output"]

            missing_source = client.post(
                "/api/chat",
                json={"message": "@多媒体助手 请从这段视频剪出介绍产品功能的片段。"},
            )
            assert missing_source.status_code == 200, missing_source.text
            missing_plan = missing_source.json()["workflow_plan"]
            assert missing_plan["next_action"] == "ask_clarifying_questions"
            assert not _specialist_steps(missing_plan)
            assert any("导入并选择一段视频素材" in item for item in missing_plan["clarifying_questions"])

            missing_brief = client.post(
                "/api/chat",
                json={"message": "@多媒体助手 把视频整理成动态讲解网页。"},
            )
            assert missing_brief.status_code == 200, missing_brief.text
            missing_brief_plan = missing_brief.json()["workflow_plan"]
            assert missing_brief_plan["next_action"] == "ask_clarifying_questions"
            assert not _specialist_steps(missing_brief_plan)

            presentation = client.post(
                "/api/chat",
                json={
                    "message": "请制作产品介绍 PPT，并在其中引用当前视频。",
                    "materials": [material],
                },
            )
            assert presentation.status_code == 200, presentation.text
            presentation_plan = presentation.json()["workflow_plan"]
            assert presentation_plan["next_action"] == "open_presentation_studio"
            assert not any(step["action"] == "open_video_workspace" for step in presentation_plan["steps"])

            image = client.post(
                "/api/chat",
                json={"message": "@图片助手 把人物身后的杂物去掉。"},
            )
            assert image.status_code == 200, image.text
            assert image.json()["workflow_plan"]["next_action"] == "open_media_workspace"

        print("Media video dispatch entry verification passed.")
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
