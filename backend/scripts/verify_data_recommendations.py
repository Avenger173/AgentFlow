"""D5.1 数据工作台推荐器离线回归。

这个脚本只验证结构画像到建议合同的映射，不启动模型、不联网，也不读取客户真实数据。
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEMP_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_data_recommendations_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(TEMP_DATA_DIR)
# 离线回归不可因开发机恰好保存了 API Key 而意外消耗真实额度。
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
sys.path.insert(0, str(BACKEND_ROOT))


def _import_csv(client, filename: str, content: str) -> None:
    response = client.post(
        "/api/agents/data_agent/datasets",
        json={
            "filename": filename,
            "content_base64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        },
    )
    response.raise_for_status()


def main() -> None:
    from fastapi.testclient import TestClient

    from main import app
    from app.schemas.data_agent import DataDatasetProfileResponse, DataRecommendationResponse
    from app.services.data_recommendations import refine_data_recommendations_with_model

    client = TestClient(app)
    try:
        _import_csv(
            client,
            "sales.csv",
            "日期,区域,产品,销售额\n2026-01-01,东区,A,120\n2026-01-10,西区,B,80\n2026-02-03,东区,A,160\n2026-02-11,南区,C,110\n",
        )
        sales = client.post(
            "/api/agents/data_agent/recommendations",
            json={"dataset_name": "sales.csv", "goal": "看看趋势和区域对比"},
        )
        sales.raise_for_status()
        sales_payload = sales.json()
        recommendations = sales_payload["recommendations"]
        routes = [item["route"] for item in recommendations]
        assert sales_payload["recommendation_mode"] == "local_profile"
        assert "trend" in routes
        assert "comparison" in routes
        assert len(recommendations) <= 4
        assert len({item["recommendation_id"] for item in recommendations}) == len(recommendations)
        assert all(
            set(item["source_columns"]).issubset({"日期", "区域", "产品", "销售额"})
            for item in recommendations
        )

        _import_csv(
            client,
            "notes.csv",
            "主题,状态\n需求梳理,进行中\n验收准备,待开始\n需求梳理,进行中\n",
        )
        notes = client.post(
            "/api/agents/data_agent/recommendations",
            json={"dataset_name": "notes.csv"},
        )
        notes.raise_for_status()
        note_routes = [item["route"] for item in notes.json()["recommendations"]]
        assert "trend" not in note_routes
        assert "comparison" not in note_routes
        assert "distribution" in note_routes

        # 模型只允许重排既有候选；即使它返回不存在的 ID，验证器也不能让其进入结果。
        class FakeRuntime:
            async def chat(self, *, system_prompt: str, user_message: str) -> str:
                del system_prompt, user_message
                return '{"priority_ids":["category_comparison","unknown"],"guidance":"先比较已识别类别，再查看趋势。"}'

        profile_response = client.get("/api/agents/data_agent/datasets/sales.csv/profile")
        profile_response.raise_for_status()
        refined = asyncio.run(
            refine_data_recommendations_with_model(
                DataRecommendationResponse.model_validate(sales_payload),
                profile=DataDatasetProfileResponse.model_validate(profile_response.json()),
                goal="趋势和区域对比",
                runtime=FakeRuntime(),
            )
        )
        assert refined.recommendation_mode == "model_assisted"
        assert refined.recommendations[0].recommendation_id == "category_comparison"
        assert all(item.recommendation_id != "unknown" for item in refined.recommendations)

        print("Data recommendation verification passed: local contracts and L1-only model refinement remain bounded.")
    finally:
        shutil.rmtree(TEMP_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
