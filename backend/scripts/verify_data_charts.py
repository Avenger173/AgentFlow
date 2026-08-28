"""D5.2 图表看板/PNG 的离线端到端回归。

仅使用临时合成 CSV，覆盖异步受理、聚合图表渲染、PNG 像素回读、受控图像读取、artifact
绝对路径脱敏、协作式取消和导入源不变性；不会调用模型、网络或真实客户文件。
"""

from __future__ import annotations

import atexit
import asyncio
import base64
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from fastapi.testclient import TestClient
from PIL import Image


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_data_chart_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"] = str(VERIFY_ROOT / "workspace")
os.environ["AGENTFLOW_DATA_CHART_OUTPUT_DIR"] = str(VERIFY_ROOT / "charts")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
atexit.register(lambda: shutil.rmtree(VERIFY_ROOT, ignore_errors=True))
sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.data_agent import DataChartExportRequest  # noqa: E402
from app.services.data_chart_delivery import (  # noqa: E402
    cancel_data_chart_export_task,
    create_data_chart_queued_run,
)
from main import app  # noqa: E402


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _wait_for_terminal_result(client: TestClient, task_id: str) -> dict[str, object]:
    for _ in range(300):
        response = client.get(f"/api/agents/data_agent/charts/export/{task_id}/result")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] not in {"queued", "pending", "running"}:
            return payload
        time.sleep(0.1)
    raise AssertionError("图表任务在 30 秒内没有到达终态")


def main() -> None:
    source = (
        "month,region,sales\n"
        "2026-01-01,east,120\n"
        "2026-01-01,south,90\n"
        "2026-02-01,east,150\n"
        "2026-02-01,south,110\n"
        "2026-03-01,east,180\n"
        "2026-03-01,south,135\n"
    ).encode("utf-8")

    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "chart_verify.csv", "content_base64": _base64(source)},
        )
        assert imported.status_code == 200, imported.text
        profile = client.get("/api/agents/data_agent/datasets/chart_verify.csv/profile")
        assert profile.status_code == 200, profile.text
        source_sha256 = profile.json()["source_sha256"]
        source_path = Path(os.environ["AGENTFLOW_DATA_WORKSPACE_DIR"]) / "chart_verify.csv"
        source_hash_before = sha256(source_path.read_bytes()).hexdigest()

        started = client.post(
            "/api/agents/data_agent/charts/export/start",
            json={
                "dataset_name": "chart_verify.csv",
                "source_sha256": source_sha256,
                "goal": "monthly trend and region comparison",
                "cleaning_policy": "safe",
                "max_chart_count": 4,
                "confirmed": True,
            },
        )
        assert started.status_code == 202, started.text
        payload = _wait_for_terminal_result(client, started.json()["task_id"])
        assert payload["status"] == "completed", payload
        assert payload["verification"]["passed"] is True
        assert payload["verification"]["chart_count"] >= 2
        assert len(payload["artifacts"]) >= 2

        first_artifact = payload["artifacts"][0]
        image = client.get(
            "/api/agents/data_agent/charts/export/"
            f"{payload['task_id']}/artifacts/{first_artifact['artifact_id']}/image"
        )
        assert image.status_code == 200, image.text
        image_path = VERIFY_ROOT / "verified.png"
        image_path.write_bytes(image.content)
        with Image.open(image_path) as rendered:
            assert rendered.format == "PNG"
            assert rendered.width >= 800 and rendered.height >= 450

        history = client.get(f"/api/tasks/{payload['task_id']}/artifacts")
        assert history.status_code == 200, history.text
        history_artifacts = history.json()["artifacts"]
        assert len(history_artifacts) == len(payload["artifacts"])
        assert all(item["metadata"].get("output_path") == "<hidden>" for item in history_artifacts)
        assert sha256(source_path.read_bytes()).hexdigest() == source_hash_before

        # 取消尚未启动的任务，验证取消不会生成 artifact，客户可回工作台重新确认而非历史重放。
        cancelled_id = "task_data_chart_abcdef123456"
        request = DataChartExportRequest(
            dataset_name="chart_verify.csv",
            source_sha256=source_sha256,
            goal="monthly trend",
            confirmed=True,
        )
        create_data_chart_queued_run(task_id=cancelled_id, request=request)
        cancelled = asyncio.run(cancel_data_chart_export_task(cancelled_id))
        assert cancelled is not None and cancelled.accepted is True
        cancelled_result = client.get(f"/api/agents/data_agent/charts/export/{cancelled_id}/result")
        assert cancelled_result.status_code == 200
        assert cancelled_result.json()["status"] == "cancelled"
        assert not cancelled_result.json()["artifacts"]
        retry = client.post(f"/api/tasks/{cancelled_id}/retry")
        assert retry.status_code == 200, retry.text
        assert retry.json()["accepted"] is False

    print("Data chart verification passed: async=completed png=validated history=hidden-path cancel=safe source_unchanged=true")


if __name__ == "__main__":
    main()
