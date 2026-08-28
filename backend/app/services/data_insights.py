"""数据工作台的受控结论生成。

本模块刻意处在“确定性计算之后”。模型只能阅读已验证的指标、聚合表和图表合同，不能看到
原始行、文件路径或任意公式；模型不可用时，本地结论仍确保结果页不会留下空白。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from pydantic import ValidationError

from app.schemas.data_agent import DataAnalysisInsight, DataAnalysisPreviewResponse
from app.schemas.model import ModelRouteAuditSnapshot
from app.services.llm_chat import is_llm_enabled
from app.services.model_gateway import ModelGatewayError, ModelRuntime, resolve_model_runtime_for_route


class DataInsightError(ValueError):
    """模型结论不满足数据引用合同，触发本地结论回退。"""


async def enrich_data_analysis_insight(
    preview: DataAnalysisPreviewResponse,
    *,
    goal: str,
    runtime: ModelRuntime | None = None,
    audit_collector: list[ModelRouteAuditSnapshot] | None = None,
) -> DataAnalysisPreviewResponse:
    """为一次已完成的本地计算补上模型或本地的可追溯结论。"""

    local = _build_local_insight(preview)
    if runtime is None:
        if not is_llm_enabled():
            return preview.model_copy(update={"insight": local})
        try:
            resolution = resolve_model_runtime_for_route("data_insight")
            runtime = resolution.runtime
            if audit_collector is not None:
                # 记录的是已经通过 Route/Profile 解析、即将用于本次结论请求的脱敏事实；不把
                # 失败后的本地结论伪装成模型结果，也不从当前配置反推旧任务。
                audit_collector.append(resolution.audit_snapshot(stage="data_insight"))
        except ModelGatewayError:
            return preview.model_copy(update={"insight": local})

    try:
        content = await asyncio.wait_for(
            runtime.chat(
                system_prompt=(
                    "你是数据工作台的分析解读者。输入的 analysis_facts 是后端已经计算并校验的唯一事实来源；"
                    "必须优先引用它们的字段、类别、期间和数值来回答用户问题。不能补写未给出的数值、字段、"
                    "时间范围、外部业务事实或因果关系。不能把“已生成几张图、几份表、可查看图表”当作结论。"
                    "若同时存在趋势和横向对比，优先各说出一个；只有数据不支持时才说明限制。"
                    "输出必须是一个 JSON 对象，不要 Markdown、代码围栏或解释。JSON 格式："
                    '{"headline":"不超过24字","conclusion":"80到300字、含具体事实的直接结论",'
                    '"highlights":["1到3条带数值或类别/期间的发现"],'
                    '"next_actions":["最多3条、仅基于当前数据的复核或下钻建议"],'
                    '"evidence_metric_ids":["已有ID"],"evidence_table_ids":["已有ID"],'
                    '"evidence_chart_ids":["已有ID"]}。'
                    "至少引用一个已有 evidence ID。若 analysis_facts 中存在数值，conclusion 或 highlights "
                    "必须出现至少一个数值；用户目标未满足时如实说明当前范围。"
                ),
                user_message=json.dumps(_model_context(preview, goal), ensure_ascii=False, separators=(",", ":")),
            ),
            timeout=20,
        )
        insight = _parse_model_insight(content, preview)
    except (asyncio.TimeoutError, TimeoutError, ModelGatewayError, ValidationError, ValueError, TypeError):
        return preview.model_copy(update={"insight": local})
    return preview.model_copy(update={"insight": insight})


def _model_context(preview: DataAnalysisPreviewResponse, goal: str) -> dict[str, Any]:
    """仅构造已经聚合的、有限长度的模型可见上下文。"""

    profile = preview.dataset_profile
    return {
        "user_goal": " ".join(goal.split())[:600],
        "dataset": {
            "row_count": profile.row_count,
            "column_count": profile.column_count,
            "sheet": profile.selected_sheet,
        },
        "metrics": [
            {
                "id": metric.metric_id,
                "name": metric.name,
                "value": metric.value,
                "unit": metric.unit,
                "aggregation": metric.aggregation,
            }
            for metric in preview.metrics[:16]
        ],
        # 给模型一份已验证的自然语言事实，而不是要求它从零散表格中自行猜趋势。表格仍保留，
        # 以便模型核对措辞；两者都只来自本地聚合结果，不含原始行。
        "analysis_facts": _build_evidence_facts(preview),
        "tables": [
            {
                "id": table.table_id,
                "title": table.title,
                "columns": table.columns,
                # 这里只允许已聚合的有限行，绝不把原始表格样本交给模型。
                "rows": table.rows[:12],
            }
            for table in preview.analysis_tables[:4]
        ],
        "charts": [
            {
                "id": chart.chart_id,
                "type": chart.chart_type,
                "title": chart.title,
                "category_column": chart.category_column,
                "value_column": chart.value_column,
            }
            for chart in preview.charts[:4]
        ],
        "quality": [
            {"id": finding.finding_id, "title": finding.title, "affected_count": finding.affected_count}
            for finding in preview.quality_findings[:4]
        ],
    }


def _parse_model_insight(content: str, preview: DataAnalysisPreviewResponse) -> DataAnalysisInsight:
    """定位 JSON 并严格校验模型只引用本次计算已产生的对象。"""

    decoder = json.JSONDecoder()
    payload: object | None = None
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise DataInsightError("模型没有返回结论 JSON。")

    if not isinstance(payload, dict):
        raise DataInsightError("模型结论不是对象。")
    # 后续建议属于辅助内容。旧 Provider 偶尔会漏掉该字段，不能因此丢掉一份已经引用正确、
    # 内容可读的结论；缺失时沿用本地事实推导出的保守下钻建议。
    normalized_payload = {**payload, "mode": "model"}
    if not normalized_payload.get("next_actions"):
        normalized_payload["next_actions"] = _build_follow_up_actions(_build_evidence_facts(preview), preview)
    insight = DataAnalysisInsight.model_validate(normalized_payload)
    metric_ids = {metric.metric_id for metric in preview.metrics}
    table_ids = {table.table_id for table in preview.analysis_tables}
    chart_ids = {chart.chart_id for chart in preview.charts}
    if not set(insight.evidence_metric_ids).issubset(metric_ids):
        raise DataInsightError("模型结论引用了不存在的指标。")
    if not set(insight.evidence_table_ids).issubset(table_ids):
        raise DataInsightError("模型结论引用了不存在的聚合表。")
    if not set(insight.evidence_chart_ids).issubset(chart_ids):
        raise DataInsightError("模型结论引用了不存在的图表。")
    if not (insight.evidence_metric_ids or insight.evidence_table_ids or insight.evidence_chart_ids):
        raise DataInsightError("模型结论没有可追溯依据。")
    if _facts_contain_numbers(_build_evidence_facts(preview)) and not re.search(
        r"\d", " ".join([insight.conclusion, *insight.highlights])
    ):
        raise DataInsightError("模型结论没有引用任何已验证数值。")
    return insight


def _build_local_insight(preview: DataAnalysisPreviewResponse) -> DataAnalysisInsight:
    """模型不可用时，直接从有限聚合结果写出可核对的数据结论。"""

    facts = _build_evidence_facts(preview)
    # 先回答用户最关心的“怎么变、谁更突出”，再补充总量或质量提示；计划执行顺序不能决定
    # 客户阅读顺序，否则数值概览会把趋势与横向差异挤出主结论。
    priority = {"trend": 0, "comparison": 1, "numeric_series": 2, "summary": 3, "quality": 4}
    ordered_facts = sorted(facts, key=lambda item: priority.get(str(item.get("kind", "")), 9))
    factual_texts = [str(item["text"]) for item in ordered_facts if item.get("text")]
    if factual_texts:
        headline = _headline_from_fact(ordered_facts[0])
        # 先保留最有解释力的两到三条事实。这里不让本地降级退回“完成了多少步骤”，使得
        # 模型异常不会降低客户读到的结果质量。
        conclusion = "。".join(text.rstrip("。") for text in factual_texts[:3]) + "。"
        highlights = factual_texts[:3]
    else:
        primary_metrics = [metric for metric in preview.metrics if metric.metric_id not in {"row_count", "column_count"}]
        metric_bits = [
            f"{metric.name}为{_format_number(metric.value)}{metric.unit if metric.unit != '数值' else ''}"
            for metric in primary_metrics[:2]
        ]
        headline = "当前数据可形成基础概览"
        conclusion = (
            f"当前数据尚未形成可比较的趋势或类别视图；已计算{'、'.join(metric_bits)}。"
            if metric_bits
            else "当前数据没有足够的日期、类别或数值组合形成趋势和横向对比；可先核对字段类型与有效记录。"
        )
        highlights = [conclusion]
    return DataAnalysisInsight(
        mode="local",
        headline=headline,
        conclusion=conclusion,
        highlights=highlights,
        next_actions=_build_follow_up_actions(facts, preview),
        evidence_metric_ids=[metric.metric_id for metric in preview.metrics[:4]],
        evidence_table_ids=_unique_ids(str(item["evidence_table_id"]) for item in ordered_facts)[:4],
        evidence_chart_ids=[chart.chart_id for chart in preview.charts[:2]],
    )


def _build_evidence_facts(preview: DataAnalysisPreviewResponse) -> list[dict[str, str]]:
    """从 D2 已验证聚合表抽取趋势、横向差异与质量事实。

    这层是模型解释和本地回退共同使用的“事实底稿”。它刻意只读取有限聚合行，不读取客户
    原始记录；每条事实都带回表 ID，便于模型输出继续通过既有证据合同校验。
    """

    facts: list[dict[str, str]] = []
    for table in preview.analysis_tables:
        rows = _numeric_rows(table.rows)
        if table.operation_id == "time_series" and len(rows) >= 2:
            facts.append(_time_series_fact(table.table_id, table.columns, rows, table.truncated))
        elif table.operation_id == "numeric_series" and len(rows) >= 2:
            facts.append(_numeric_series_fact(table.table_id, table.columns, rows, table.truncated))
        elif table.operation_id in {"category_breakdown", "secondary_breakdown"} and len(rows) >= 2:
            facts.append(_comparison_fact(table.table_id, table.title, table.columns, rows, table.truncated))
        elif table.operation_id == "numeric_summary" and rows:
            facts.extend(_numeric_summary_facts(table.table_id, rows))

    for finding in preview.quality_findings[:2]:
        facts.append(
            {
                "kind": "quality",
                "text": f"数据质量提示：{finding.title}，影响 {finding.affected_count} 个单元格或记录；{finding.handling}",
                "evidence_table_id": "",
            }
        )
    return facts


def _numeric_rows(rows: list[list[str]]) -> list[tuple[str, float]]:
    """过滤不能被稳定解释为数值的有限聚合行，避免结论层把显示文本当作数据。"""

    result: list[tuple[str, float]] = []
    for row in rows:
        if len(row) < 2:
            continue
        value = _parse_number(row[1])
        if value is not None:
            result.append((str(row[0]), value))
    return result


def _time_series_fact(
    table_id: str, columns: list[str], rows: list[tuple[str, float]], truncated: bool
) -> dict[str, str]:
    period_name = columns[0] if columns else "期间"
    value_name = columns[1] if len(columns) > 1 else "数值"
    start_period, start_value = rows[0]
    end_period, end_value = rows[-1]
    peak_period, peak_value = max(rows, key=lambda item: item[1])
    trough_period, trough_value = min(rows, key=lambda item: item[1])
    change = end_value - start_value
    if abs(change) < 1e-9:
        change_text = "首尾基本持平"
    else:
        direction = "增加" if change > 0 else "减少"
        change_text = f"累计{direction}{_format_number(abs(change))}"
        if abs(start_value) > 1e-9:
            change_text += f"（{_format_percent(change / abs(start_value) * 100)}）"
    scope = "已展示的" if truncated else "当前"
    return {
        "kind": "trend",
        "text": (
            f"{value_name}在{scope}{period_name}范围内从 {start_period} 的{_format_number(start_value)}"
            f"变为 {end_period} 的{_format_number(end_value)}，{change_text}；"
            f"最高点为 {peak_period} 的{_format_number(peak_value)}，最低点为 {trough_period} 的{_format_number(trough_value)}"
        ),
        "evidence_table_id": table_id,
    }


def _numeric_series_fact(
    table_id: str, columns: list[str], rows: list[tuple[str, float]], truncated: bool
) -> dict[str, str]:
    x_name = columns[0] if columns else "横轴"
    y_name = columns[1] if len(columns) > 1 else "数值"
    start_x, start_y = rows[0]
    end_x, end_y = rows[-1]
    peak_x, peak_y = max(rows, key=lambda item: item[1])
    scope = "已展示样本" if truncated else "当前样本"
    return {
        "kind": "numeric_series",
        "text": (
            f"在{scope}中，{x_name}从 {start_x} 到 {end_x} 时，{y_name}从{_format_number(start_y)}"
            f"变为{_format_number(end_y)}；最高值为{_format_number(peak_y)}，对应 {x_name}={peak_x}"
        ),
        "evidence_table_id": table_id,
    }


def _comparison_fact(
    table_id: str, title: str, columns: list[str], rows: list[tuple[str, float]], truncated: bool
) -> dict[str, str]:
    value_name = columns[1] if len(columns) > 1 else "数值"
    # 聚合表的可视排序未来可能允许客户改成升序；结论中的“居首”仍必须由数值本身决定，
    # 不能把第一行机械当作最大值。
    ranked_rows = sorted(rows, key=lambda item: item[1], reverse=True)
    top_label, top_value = ranked_rows[0]
    second_label, second_value = ranked_rows[1]
    difference = top_value - second_value
    scope = "已展示类别" if truncated else "当前类别"
    text = (
        f"{title}中，{top_label}的{value_name}为{_format_number(top_value)}，在{scope}中居首；"
        f"比{second_label}高{_format_number(abs(difference))}"
    )
    if abs(second_value) > 1e-9:
        text += f"（{_format_percent(abs(difference) / abs(second_value) * 100)}）"
    if all(value >= 0 for _, value in rows):
        total = sum(value for _, value in rows)
        if total > 0:
            text += f"；前两项占{scope}合计的{_format_percent((top_value + second_value) / total * 100)}"
    return {"kind": "comparison", "text": text, "evidence_table_id": table_id}


def _numeric_summary_facts(table_id: str, rows: list[tuple[str, float]]) -> list[dict[str, str]]:
    """数值概览补充最多两条合计事实，避免回退模式只陈述处理步骤。"""

    return [
        {
            "kind": "summary",
            "text": f"{label}的已计算合计为{_format_number(value)}",
            "evidence_table_id": table_id,
        }
        for label, value in rows[:2]
    ]


def _headline_from_fact(fact: dict[str, str]) -> str:
    """用最强事实生成标题，避免将处理步骤数量伪装成分析结论。"""

    if fact.get("kind") == "trend":
        return "时间走势与波动已定位"
    if fact.get("kind") == "comparison":
        return "横向差异已定位"
    if fact.get("kind") == "numeric_series":
        return "连续数据变化已定位"
    if fact.get("kind") == "quality":
        return "数据质量需要先关注"
    return str(fact.get("text", ""))[:24] or "已形成可核对结论"


def _build_follow_up_actions(
    facts: list[dict[str, str]], preview: DataAnalysisPreviewResponse
) -> list[str]:
    """只提出可由当前数据继续完成的复核建议，不替客户作业务决策。"""

    kinds = {str(item.get("kind", "")) for item in facts}
    actions: list[str] = []
    if "trend" in kinds:
        actions.append("围绕峰值和低谷期间筛选原始记录，核对波动是否来自录入差异、样本量或实际业务变化。")
    if "comparison" in kinds:
        actions.append("对领先类别与次高类别继续按时间或其它已有字段拆分，确认差异是否持续存在。")
    if "numeric_series" in kinds:
        actions.append("在最高值附近补看更多相邻样本，避免只依据首尾点判断连续变化。")
    if preview.quality_findings:
        actions.append("在用于正式汇报或后续计算前，先处理结果页列出的缺失、重复或解析问题。")
    if not actions:
        actions.append("先确认日期、类别和数值字段是否被正确识别，再选择需要比较的维度重新分析。")
    return actions[:3]


def _unique_ids(values: Any) -> list[str]:
    """保持引用顺序去重，并忽略质量事实这类没有表 ID 的项目。"""

    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _facts_contain_numbers(facts: list[dict[str, str]]) -> bool:
    return any(re.search(r"\d", str(item.get("text", ""))) for item in facts)


def _parse_number(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    """保持本地回退文案紧凑，不在结果卡中制造长小数。"""

    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _format_percent(value: float) -> str:
    return f"{value:.2f}%"
