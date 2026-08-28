"""数据工作台 D2 的离线分析计划与确定性计算回归。

只用临时合成数据验证操作白名单、聚合结果结构、图表合同和源文件不变性。日志不打印表格
单元格、指标数值或任何真实客户数据。
"""

from __future__ import annotations

import atexit
import asyncio
import base64
import json
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import shutil
import sys
import tempfile

from fastapi.testclient import TestClient
from openpyxl import Workbook


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_data_analysis_verify_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
atexit.register(lambda: shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True))
sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.data_agent import DataAnalysisOperation, DataAnalysisPlan, DataAnalysisPreviewResponse  # noqa: E402
from app.services.data_analysis import DataPlanValidationError, validate_data_analysis_plan  # noqa: E402
from app.services.data_insights import enrich_data_analysis_insight  # noqa: E402
from app.services.data_workspace import get_data_dataset_profile  # noqa: E402
from main import app  # noqa: E402


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sample_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "销售明细"
    sheet.append(["2026 年模拟销售明细"])
    # 四个数值字段同时验证 D2 数值概览的字段上限与稳定指标 ID，而不是只覆盖单金额列。
    sheet.append(["日期", "区域", "产品", "金额", "数量", "成本", "利润", "订单号"])
    sheet.append(["2026-01-05", "华东", "A 产品", 1200, 12, 700, 500, "A-001"])
    sheet.append(["2026-01-22", "华南", "A 产品", 800, 8, 450, 350, "A-002"])
    sheet.append(["2026-02-10", "华东", "B 产品", 1500, 15, 900, 600, "A-003"])
    sheet.append(["2026-02-28", "华北", "B 产品", 900, 9, 510, 390, "A-004"])
    sheet.append(["2026-03-08", "华东", "A 产品", 1800, 18, 1050, 750, "A-005"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


class _FactAwareRuntime:
    """不调用外部 Provider，验证模型只会收到本地已验证事实并可通过引用合同。"""

    async def chat(self, *, system_prompt: str, user_message: str) -> str:
        context = json.loads(user_message)
        facts = context["analysis_facts"]
        trend = next(item for item in facts if item["kind"] == "trend")
        comparison = next(item for item in facts if item["kind"] == "comparison")
        assert "已生成几张图" in system_prompt
        return json.dumps(
            {
                "headline": "月度走势和区域差异已定位",
                "conclusion": f"{trend['text']}。{comparison['text']}。",
                "highlights": [trend["text"], comparison["text"]],
                "next_actions": ["先复核峰值月份与领先区域的原始记录，再决定是否拆分产品维度。"],
                "evidence_metric_ids": [],
                "evidence_table_ids": [trend["evidence_table_id"], comparison["evidence_table_id"]],
                "evidence_chart_ids": [],
            },
            ensure_ascii=False,
        )


def main() -> None:
    source = _sample_xlsx()
    source_path = VERIFY_DATA_DIR / "source.xlsx"
    source_path.write_bytes(source)
    before_hash = sha256(source_path.read_bytes()).hexdigest()

    with TestClient(app) as client:
        imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "销售分析样本.xlsx", "content_base64": _base64(source)},
        )
        assert imported.status_code == 200, imported.text
        response = client.post(
            "/api/agents/data_agent/analysis/preview",
            json={
                "dataset_name": "销售分析样本.xlsx",
                "goal": "分析月度销售趋势、区域表现和产品结构",
                "cleaning_policy": "safe",
                "max_chart_count": 3,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["analysis_plan"]["planning_mode"] == "deterministic_profile"
        operation_types = {item["operation_type"] for item in payload["analysis_plan"]["operations"]}
        assert {"overview", "numeric_summary", "time_series", "group_aggregate"}.issubset(operation_types)
        assert {item["chart_type"] for item in payload["charts"]} >= {"line", "bar"}
        assert payload["analysis_tables"]
        assert payload["insight"]["mode"] == "local"
        assert payload["insight"]["headline"]
        assert payload["insight"]["evidence_metric_ids"]
        # 降级结论也必须回答真实数据的趋势与横向差异，不能只报告“生成了几张图/几份表”。
        assert "2026-02" in payload["insight"]["conclusion"]
        assert "华东" in payload["insight"]["conclusion"]
        assert "已生成" not in payload["insight"]["conclusion"]
        assert payload["insight"]["next_actions"]
        # 模型解释路径只能消费本地有限聚合事实。这里用假 Runtime 覆盖 JSON 契约、事实上下文
        # 与证据 ID 校验，不需要为了协议测试消耗真实模型额度。
        model_preview = asyncio.run(
            enrich_data_analysis_insight(
                DataAnalysisPreviewResponse.model_validate(payload),
                goal="分析月度销售趋势、区域表现和产品结构",
                runtime=_FactAwareRuntime(),
            )
        )
        assert model_preview.insight is not None
        assert model_preview.insight.mode == "model"
        assert "华东" in model_preview.insight.conclusion
        assert "2026-02" in model_preview.insight.conclusion
        assert all(len(item["rows"]) <= 50 for item in payload["analysis_tables"])
        numeric_table = next(item for item in payload["analysis_tables"] if item["table_id"] == "numeric_summary_table")
        assert len(numeric_table["source_columns"]) == 4
        assert {item["stage"] for item in payload["trace"]} == {"profile", "plan", "validate", "execute"}
        assert not (VERIFY_DATA_DIR / "outputs").exists()

        # 设备/实验类 CSV 往往没有日期列，却天然包含“位置/序号 -> 评分/响应”的连续曲线。
        # 这类数据不能被旧版“只有日期才有折线图”的规则误降级为无意义类别柱图。
        curve_csv = (
            "阶段,焦点位置,有效清晰度,原始清晰度\n"
            "粗调,1200,88,81\n"
            "粗调,1300,91,85\n"
            "精调,1400,96,90\n"
            "精调,1500,94,89\n"
        ).encode("utf-8")
        curve_imported = client.post(
            "/api/agents/data_agent/datasets",
            json={"filename": "自动对焦曲线.csv", "content_base64": _base64(curve_csv)},
        )
        assert curve_imported.status_code == 200, curve_imported.text
        curve_preview = client.post(
            "/api/agents/data_agent/analysis/preview",
            json={
                "dataset_name": "自动对焦曲线.csv",
                "goal": "生成焦点位置与有效清晰度的折线图",
                "max_chart_count": 3,
            },
        )
        assert curve_preview.status_code == 200, curve_preview.text
        curve_payload = curve_preview.json()
        curve_operation = next(
            item
            for item in curve_payload["analysis_plan"]["operations"]
            if item["operation_type"] == "numeric_series"
        )
        assert curve_operation["source_columns"] == ["焦点位置", "有效清晰度"]
        assert any(
            item["chart_id"] == "numeric_series_chart" and item["chart_type"] == "line"
            for item in curve_payload["charts"]
        )
        assert [item["chart_type"] for item in curve_payload["charts"]] == ["line"], curve_payload["charts"]

        profile = get_data_dataset_profile("销售分析样本.xlsx")
        invalid_plan = DataAnalysisPlan(
            dataset_name="销售分析样本.xlsx",
            source_sha256=profile.source_sha256,
            goal="test",
            operations=[
                DataAnalysisOperation(
                    operation_id="bad_group",
                    operation_type="group_aggregate",
                    title="错误列验证",
                    source_columns=["不存在的列"],
                    aggregation="count",
                    rationale="验证未知列拒绝。",
                )
            ],
        )
        try:
            validate_data_analysis_plan(invalid_plan, profile.columns, 1)
        except DataPlanValidationError:
            pass
        else:
            raise AssertionError("未知列计划没有被拒绝")

        missing = client.post(
            "/api/agents/data_agent/analysis/preview",
            json={"dataset_name": "missing.xlsx", "goal": "概览"},
        )
        assert missing.status_code == 404

    assert sha256(source_path.read_bytes()).hexdigest() == before_hash
    print("Data analysis verification passed: plan=whitelist metrics=deterministic charts=line+bar+numeric-curve source_unchanged=true")


if __name__ == "__main__":
    main()
