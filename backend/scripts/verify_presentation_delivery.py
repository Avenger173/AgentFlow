"""离线验证项目方案 PPT 交付链路。

该脚本强制 mock 模式与临时数据目录，不读取真实模型密钥，也不会向项目 output/ 写入文件。
它覆盖“已核验草稿 -> 自动材料预检 -> 只读计划 -> 明确确认 -> PPTX 回读 -> 历史审计”这一条
正式交付主线。
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_verify_presentation_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(_VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DOCUMENT_PRESENTATION_OUTPUT_DIR"] = str(_VERIFY_ROOT / "presentations")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
atexit.register(lambda: shutil.rmtree(_VERIFY_ROOT, ignore_errors=True))
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient
from pptx import Presentation

from main import app


def main() -> None:
    """运行端到端断言，并确保文件与审计记录都来自受控路径。"""

    client = TestClient(app)
    imported = client.post(
        "/api/workspace/documents",
        json={
            "filename": "presentation_source.md",
            "content": (
                "# 星河平台升级方案\n\n"
                "## 项目背景\n"
                "当前系统需要升级权限管理与文档检索能力。\n\n"
                "## 项目目标\n"
                "必须提供可追溯的文档交付和审核记录。\n\n"
                "## 实施计划\n"
                "第一阶段完成文档助手，第二阶段完成审查工作流。\n\n"
                "## 交付与验收\n"
                "交付可编辑 PPTX，并保留来源追溯页。\n\n"
                "## 风险与依赖\n"
                "模型输出必须经过结构校验，不能直接写入客户原始文件。\n"
            ),
        },
    )
    assert imported.status_code == 200, imported.text

    draft = client.post(
        "/api/agents/document_agent/run",
        json={
            "task_goal": "为这份项目材料生成可审阅的 Markdown 草稿预览。",
            "document_refs": ["presentation_source.md"],
            "output_mode": "draft",
        },
    )
    assert draft.status_code == 200, draft.text
    draft_payload = draft.json()
    assert draft_payload["status"] == "completed"
    assert draft_payload["document_context"]["draft_verification_state"] == "verified"
    task_id = draft_payload["task_id"]

    preview = client.post(
        f"/api/agents/document_agent/{task_id}/presentation-preview",
        json={"presentation_type": "project_proposal"},
    )
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["slides"][0]["role"] == "cover"
    assert preview_payload["slides"][-1]["role"] == "sources"
    assert len(preview_payload["slides"]) >= 5
    # 预检必须在计划建立时自动完成，而不是让 Qt 额外点击“项目审查”才出现。样例缺少
    # 责任与术语等材料表述，因此允许出现 attention，但每条问题都要可追溯。
    preflight = preview_payload["preflight"]
    assert preflight["strategy"] == "project_delivery_preflight_v1"
    assert preflight["check_total"] == 6
    assert preflight["checked_documents"] == ["presentation_source.md"]
    assert preflight["attention_check_total"] > 0
    assert preflight["findings"]
    assert all(item["source_refs"] for item in preflight["findings"])

    # 文件写入需要本次显式确认；只看到计划或刷新页面不能创建任何 PPTX。
    missing_confirmation = client.post(
        f"/api/agents/document_agent/{task_id}/presentations/export",
        json={
            "presentation_type": "project_proposal",
            "plan_id": preview_payload["plan_id"],
            "filename": "未确认方案.pptx",
            "confirmed": False,
        },
    )
    assert missing_confirmation.status_code == 409, missing_confirmation.text

    stale_plan = client.post(
        f"/api/agents/document_agent/{task_id}/presentations/export",
        json={
            "presentation_type": "project_proposal",
            "plan_id": "0" * 48,
            "filename": "过期方案.pptx",
            "confirmed": True,
        },
    )
    assert stale_plan.status_code == 409, stale_plan.text

    exported = client.post(
        f"/api/agents/document_agent/{task_id}/presentations/export",
        json={
            "presentation_type": "project_proposal",
            "plan_id": preview_payload["plan_id"],
            "filename": "星河平台项目方案.pptx",
            "confirmed": True,
        },
    )
    assert exported.status_code == 200, exported.text
    exported_payload = exported.json()
    assert exported_payload["verification"]["passed"] is True
    assert exported_payload["slide_count"] == len(preview_payload["slides"])

    output_path = _VERIFY_ROOT / "presentations" / "星河平台项目方案.pptx"
    assert output_path.exists() and output_path.stat().st_size > 0
    generated = Presentation(output_path)
    assert len(generated.slides) == exported_payload["slide_count"]

    artifacts = client.get(f"/api/tasks/{task_id}/artifacts")
    assert artifacts.status_code == 200, artifacts.text
    artifact = next(
        item
        for item in artifacts.json()["artifacts"]
        if item["artifact_id"] == exported_payload["artifact_id"]
    )
    assert artifact["metadata"]["output_scope"] == "document_presentations"
    assert artifact["uri"].startswith("agentflow-output://document_presentations/")

    duplicate_name = client.post(
        f"/api/agents/document_agent/{task_id}/presentations/export",
        json={
            "presentation_type": "project_proposal",
            "plan_id": preview_payload["plan_id"],
            "filename": "星河平台项目方案.pptx",
            "confirmed": True,
        },
    )
    assert duplicate_name.status_code == 409, duplicate_name.text
    print("Presentation delivery verification passed.")


if __name__ == "__main__":
    main()
