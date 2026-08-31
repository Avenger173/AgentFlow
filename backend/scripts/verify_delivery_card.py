"""验证 R5.4D 统一结果卡只展示交付事实，不泄露内部路径。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile


VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_delivery_card_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.database.task_repository import save_workflow_run  # noqa: E402
from app.schemas.workflow import (  # noqa: E402
    RuntimeExecutionMetrics,
    WorkflowArtifact,
    WorkflowRun,
    WorkflowStepRun,
)
from main import app  # noqa: E402


def main() -> None:
    """写入一份最小已完成任务，验证统一结果卡的客户可读边界。"""

    run = WorkflowRun(
        task_id="verify_delivery_card_task",
        mode="runtime",
        status="completed",
        summary="数据分析已完成并通过回读验证。",
        steps=[
            WorkflowStepRun(
                step_id="step_2",
                agent="data_agent",
                action="data.render_workbook",
                status="completed",
                message="数据工作簿已完成。",
                output={
                    "verification": {
                        "table_count": 2,
                        "chart_count": 3,
                        "metric_count": 4,
                    }
                },
            )
        ],
        metrics=RuntimeExecutionMetrics(
            started_at="2026-08-31T00:00:00+00:00",
            finished_at="2026-08-31T00:00:01+00:00",
            duration_ms=1_000,
            step_total=2,
            step_completed=2,
        ),
    )
    artifact = WorkflowArtifact(
        artifact_id="artifact_delivery_card_1",
        task_id=run.task_id,
        step_id="step_2",
        agent_id="data_agent",
        kind="data",
        name="分析结果.csv",
        summary="已生成数据分析副本。",
        uri="agentflow-output://data_analysis/分析结果.csv",
        mime_type="text/csv",
        metadata={
            "runtime": True,
            "output_path": str(VERIFY_ROOT / "private" / "分析结果.csv"),
        },
    )
    save_workflow_run(run=run, events=[], plan=None, artifacts=[artifact], tool_calls=[])

    with TestClient(app) as client:
        response = client.get(f"/api/tasks/{run.task_id}/delivery")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["schema_version"] == "agentflow.delivery.v1"
        assert payload["headline"] == "任务已完成 · 1 项交付"
        assert payload["terminal"] is True
        assert payload["artifacts"][0]["openable"] is True
        assert payload["artifacts"][0]["previewable"] is False
        assert payload["table_summary"] == {
            "table_count": 2,
            "chart_count": 3,
            "metric_count": 4,
            "description": "2 个表格、3 个图表、4 个指标，均来自已验证交付结果。",
        }
        assert "output_path" not in response.text
        assert str(VERIFY_ROOT) not in response.text
        assert payload["next_actions"] == ["打开交付物", "继续提出下一步要求"]

        missing = client.get("/api/tasks/verify_delivery_card_missing/delivery")
        assert missing.status_code == 404, missing.text

    print("Delivery card verification passed: schema=v1 conclusion_first=true artifact_safe=true path_hidden=true")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
