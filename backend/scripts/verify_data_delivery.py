"""数据工作台 D4 工作簿交付任务的离线端到端回归。

本脚本验证“确认导出”不再是无历史的同步旁路：任务应先受理、再完成受控 Excel 交付，并在
SQLite 中留下阶段日志、工具审计和脱敏 artifact。所有数据与输出均位于临时目录。
"""

from __future__ import annotations

import atexit
import base64
from io import BytesIO
import os
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event
from time import sleep
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_data_delivery_verify_"))
VERIFY_DATA_DIR = VERIFY_ROOT / "data"
VERIFY_OUTPUT_DIR = VERIFY_ROOT / "output" / "data_analysis"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
os.environ["AGENTFLOW_DATA_ANALYSIS_OUTPUT_DIR"] = str(VERIFY_OUTPUT_DIR)
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
atexit.register(lambda: shutil.rmtree(VERIFY_ROOT, ignore_errors=True))
sys.path.insert(0, str(BACKEND_ROOT))

from main import app  # noqa: E402
from app.services import data_analysis_delivery  # noqa: E402


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sample_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "销售明细"
    sheet.append(["2026 年模拟销售明细"])
    sheet.append(["日期", "区域", "产品", "金额", "数量"])
    sheet.append(["2026-01-05", "华东", "A 产品", 1200, 12])
    sheet.append(["2026-01-22", "华南", "A 产品", 800, 8])
    sheet.append(["2026-02-10", "华东", "B 产品", 1500, 15])
    sheet.append(["2026-02-28", "华北", "B 产品", 900, 9])
    sheet.append(["2026-03-08", "华东", "A 产品", 1800, 18])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _wait_for_result(client: TestClient, task_id: str) -> dict[str, object]:
    """导出在后台线程运行；短轮询仅用于离线脚本，不代表 Qt 的实时事件展示。"""

    endpoint = f"/api/agents/data_agent/analysis/export/{task_id}/result"
    last_payload: dict[str, object] | None = None
    for _ in range(160):
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        last_payload = response.json()
        if last_payload["status"] in {"completed", "failed", "cancelled"}:
            return last_payload
        sleep(0.05)
    raise AssertionError(f"数据工作簿任务未在预期时间结束：{last_payload}")


def main() -> None:
    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "销售历史样本.xlsx", "content_base64": _base64(_sample_xlsx())},
        )
        assert imported.status_code == 200, imported.text

        preview = client.post(
            "/api/agents/data_agent/analysis/preview",
            json={
                "dataset_name": "销售历史样本.xlsx",
                "goal": "分析月度销售趋势、区域表现和产品结构",
                "max_chart_count": 3,
            },
        )
        assert preview.status_code == 200, preview.text
        source_sha256 = preview.json()["dataset_profile"]["source_sha256"]

        start = client.post(
            "/api/agents/data_agent/analysis/export/start",
            json={
                "dataset_name": "销售历史样本.xlsx",
                "source_sha256": source_sha256,
                "goal": "分析月度销售趋势、区域表现和产品结构",
                "max_chart_count": 3,
                "confirmed": True,
            },
        )
        assert start.status_code == 202, start.text
        task_id = start.json()["task_id"]
        assert task_id.startswith("task_data_")
        result = _wait_for_result(client, task_id)
        assert result["status"] == "completed", result
        assert result["verification"]["passed"] is True
        assert result["artifact"]["uri"].startswith("agentflow-output://data_analysis/")
        assert str(VERIFY_ROOT) not in str(result)

        history = client.get(f"/api/tasks/{task_id}")
        assert history.status_code == 200, history.text
        assert history.json()["status"] == "completed"
        assert history.json()["steps"][0]["action"] == "data.render_workbook"
        assert history.json()["steps"][0]["output"]["model_used"] is False
        assert history.json()["steps"][0]["output"]["network_used"] is False

        artifacts = client.get(f"/api/tasks/{task_id}/artifacts")
        assert artifacts.status_code == 200, artifacts.text
        artifact = artifacts.json()["artifacts"][0]
        assert artifact["kind"] == "data"
        assert artifact["metadata"]["output_path"] == "<hidden>"
        assert str(VERIFY_ROOT) not in str(artifacts.json())

        tool_calls = client.get(f"/api/tasks/{task_id}/tool-calls")
        assert tool_calls.status_code == 200, tool_calls.text
        tool_call = tool_calls.json()["tool_calls"][0]
        assert tool_call["status"] == "completed"
        assert tool_call["tool_name"] == "data.render_workbook"
        assert tool_call["request"]["original_file_unchanged"] is True
        assert tool_call["request"]["model_used"] is False
        assert tool_call["request"]["network_used"] is False

        logs = client.get(f"/api/tasks/{task_id}/logs")
        assert logs.status_code == 200, logs.text
        events = [item["event"] for item in logs.json()["events"]]
        assert events == ["task_queued", "task_started", "tool_started", "artifact_saved", "task_completed"]

        artifact_preview = client.get(f"/api/tasks/{task_id}/artifacts/{artifact['artifact_id']}/preview")
        assert artifact_preview.status_code == 200, artifact_preview.text
        assert artifact_preview.json()["available"] is False
        assert "文本" in artifact_preview.json()["reason"]

        # 历史列表继续脱敏绝对路径，但客户点击“打开”时后端应只对已登记且仍在受控根内的
        # runtime artifact 调用系统默认程序。这里 mock 掉 Windows 副作用，验证边界与回执。
        with patch("app.api.tasks.os.startfile") as start_file:
            opened = client.post(f"/api/tasks/{task_id}/artifacts/{artifact['artifact_id']}/open")
        assert opened.status_code == 200, opened.text
        assert opened.json()["opened"] is True
        start_file.assert_called_once()
        assert str(VERIFY_ROOT) not in str(opened.json())

        # 模拟用户在 openpyxl 后台线程已经开始写入时点击取消。这里不强杀线程，而是验证服务会
        # 先持久化 cancelled，并在线程返回后删除尚未登记的输出，不让 artifact 漏进任务历史。
        output_before_cancel = set(VERIFY_OUTPUT_DIR.glob("*.xlsx"))
        renderer_entered = Event()
        release_renderer = Event()
        real_export = data_analysis_delivery.export_data_analysis_workbook

        def delayed_export(request: object):
            renderer_entered.set()
            assert release_renderer.wait(timeout=5), "取消回归未释放后台 Excel 渲染线程"
            return real_export(request)

        with patch(
            "app.services.data_analysis_delivery.export_data_analysis_workbook",
            side_effect=delayed_export,
        ):
            cancelled_start = client.post(
                "/api/agents/data_agent/analysis/export/start",
                json={
                    "dataset_name": "销售历史样本.xlsx",
                    "source_sha256": source_sha256,
                    "goal": "生成可取消的区域销售工作簿",
                    "max_chart_count": 3,
                    "confirmed": True,
                },
            )
            assert cancelled_start.status_code == 202, cancelled_start.text
            cancelled_task_id = cancelled_start.json()["task_id"]
            assert renderer_entered.wait(timeout=2), "后台 Excel 渲染没有进入预期阶段"

            cancellation = client.post(f"/api/tasks/{cancelled_task_id}/cancel")
            assert cancellation.status_code == 200, cancellation.text
            assert cancellation.json()["accepted"] is True
            assert cancellation.json()["status"] == "cancelled"

            cancelled = _wait_for_result(client, cancelled_task_id)
            assert cancelled["status"] == "cancelled", cancelled
            assert cancelled["artifact"] is None

            # 数据导出不保存客户全文目标，因此不能在历史页伪造一键重试；UI 必须引导客户回到
            # 当前工作台预览重新确认。
            retry = client.post(f"/api/tasks/{cancelled_task_id}/retry")
            assert retry.status_code == 200, retry.text
            assert retry.json()["accepted"] is False
            assert retry.json()["status"] == "cancelled"
            assert "数据工作台" in retry.json()["message"]

            release_renderer.set()

        # 等待协作式清理完成。取消终态会先可见，文件清理稍后随同步渲染线程返回执行。
        for _ in range(100):
            if set(VERIFY_OUTPUT_DIR.glob("*.xlsx")) == output_before_cancel:
                break
            sleep(0.05)
        else:
            raise AssertionError("已取消任务仍留下未登记的 Excel 输出文件")

        cancelled_history = client.get(f"/api/tasks/{cancelled_task_id}")
        assert cancelled_history.status_code == 200, cancelled_history.text
        assert cancelled_history.json()["status"] == "cancelled"
        cancelled_artifacts = client.get(f"/api/tasks/{cancelled_task_id}/artifacts")
        assert cancelled_artifacts.status_code == 200, cancelled_artifacts.text
        assert cancelled_artifacts.json()["total"] == 0
        cancelled_calls = client.get(f"/api/tasks/{cancelled_task_id}/tool-calls")
        assert cancelled_calls.status_code == 200, cancelled_calls.text
        assert cancelled_calls.json()["tool_calls"][0]["status"] == "skipped"
        cancelled_logs = client.get(f"/api/tasks/{cancelled_task_id}/logs")
        assert cancelled_logs.status_code == 200, cancelled_logs.text
        assert cancelled_logs.json()["events"][-1]["event"] == "task_cancelled"

        failure_start = client.post(
            "/api/agents/data_agent/analysis/export/start",
            json={
                "dataset_name": "销售历史样本.xlsx",
                "source_sha256": "0" * 64,
                "goal": "分析月度销售趋势",
                "confirmed": True,
            },
        )
        assert failure_start.status_code == 202, failure_start.text
        failure = _wait_for_result(client, failure_start.json()["task_id"])
        assert failure["status"] == "failed"
        assert failure["artifact"] is None
        assert not list(VERIFY_OUTPUT_DIR.glob("*.partial.xlsx"))

    print(
        "Data delivery verification passed: "
        "queued_runtime=true history=true events=true artifact=true paths_hidden=true controlled_open=true "
        "cancelled_task=true retry_guard=true failed_task=true"
    )


if __name__ == "__main__":
    main()
