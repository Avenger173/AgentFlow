"""数据工作台 D5.1 的本地下一步推荐器。

推荐器只读取 D1 已生成的结构画像（L1），不读取完整 DataFrame、不调用模型，也不创建任务或
文件。这样客户可以先看到当前表格真正可完成的事，再把已选建议交给既有 D2 白名单计算链。
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field, ValidationError

from app.schemas.data_agent import (
    DataColumnProfile,
    DataDatasetProfileResponse,
    DataRecommendation,
    DataRecommendationRequest,
    DataRecommendationResponse,
)
from app.services.data_workspace import DataWorkspaceError, get_data_dataset_profile
from app.services.llm_chat import is_llm_enabled
from app.services.model_gateway import ModelGatewayError, ModelRuntime, resolve_model_runtime_for_route


class DataRecommendationError(ValueError):
    """数据推荐阶段可直接反馈给客户的受控错误。"""


_IDENTIFIER_HINTS = ("编号", "订单号", "id", "code", "编码", "手机号", "电话")
_GOAL_HINTS = ("趋势", "对比", "排行", "排名", "构成", "占比", "分布", "质量", "缺失", "重复", "异常")


class _ModelRecommendationAdvice(BaseModel):
    """模型只允许重排本地候选，不能自行编造新的字段、数值或分析动作。"""

    priority_ids: list[str] = Field(default_factory=list, max_length=4)
    guidance: str = Field(min_length=1, max_length=220)


def build_data_recommendations(request: DataRecommendationRequest) -> DataRecommendationResponse:
    """根据当前画像返回最多四个彼此有差异的可执行问题。

    D2 的计划器仍是唯一能决定具体操作和数据计算的地方。这里的 ``question`` 只是客户友好的
    自然语言入口，所有字段选择同时以 ``source_columns`` 显式呈现，便于 Qt 卡片和测试复核。
    """

    try:
        profile = get_data_dataset_profile(request.dataset_name)
    except DataWorkspaceError as exc:
        raise DataRecommendationError(str(exc)) from exc

    recommendations = _build_recommendations(profile, request.goal)
    guidance = _guidance_for(profile, recommendations, request.goal)
    warnings = _recommendation_warnings(profile, recommendations)
    return DataRecommendationResponse(
        dataset_name=profile.dataset.name,
        source_sha256=profile.source_sha256,
        recommendations=recommendations,
        guidance=guidance,
        warnings=warnings,
    )


async def refine_data_recommendations_with_model(
    response: DataRecommendationResponse,
    *,
    profile: DataDatasetProfileResponse,
    goal: str,
    runtime: ModelRuntime | None = None,
) -> DataRecommendationResponse:
    """可选地用 L1 画像改善卡片排序与一句引导，不让模型介入计算合同。

    这里故意不发送预览行、原始单元格、文件路径或质量明细中的可识别内容。模型输出即使格式
    正确，也只能在已有 recommendation_id 集合内排序；所有分析字段和图表候选仍来自本地规则。
    """

    if not response.recommendations:
        return response
    if runtime is None:
        if not is_llm_enabled():
            return response
        try:
            runtime = resolve_model_runtime_for_route("data_insight").runtime
        except ModelGatewayError:
            return _with_local_fallback(response)

    candidate_ids = {item.recommendation_id for item in response.recommendations}
    model_context = {
        "user_goal": " ".join(goal.split())[:400],
        "dataset_shape": {
            "row_count": profile.row_count,
            "column_count": profile.column_count,
        },
        "columns": [
            {
                "name": column.name,
                "type": column.inferred_type,
                "missing_count": column.missing_count,
                "unique_count": column.unique_count,
            }
            for column in profile.columns[:100]
        ],
        "candidates": [
            {
                "id": item.recommendation_id,
                "question": item.question,
                "route": item.route,
                "source_columns": item.source_columns,
                "expected_output": item.expected_output,
            }
            for item in response.recommendations
        ],
    }
    try:
        content = await runtime.chat(
            system_prompt=(
                "你是本地数据工作台的建议排序器。你只能基于已脱敏的字段画像和候选建议，"
                "按用户目标排序，并写一句不超过 80 个汉字的下一步引导。不得新增字段、数值、"
                "图表、公式或分析动作，不得声称读过原始表格。只返回 JSON："
                '{"priority_ids":["候选 id"],"guidance":"一句引导"}。'
            ),
            user_message=json.dumps(model_context, ensure_ascii=False, separators=(",", ":")),
        )
        advice = _parse_model_advice(content, candidate_ids)
    # 建议只是可选入口，不能让 Provider 的连接、超时或不规范回复影响数据画像和本地分析。
    # asyncio.TimeoutError 覆盖部分 Provider 直接透出的网络超时；其余已知模型错误统一回退。
    except (asyncio.TimeoutError, TimeoutError, ModelGatewayError, ValidationError, ValueError, TypeError):
        return _with_local_fallback(response)

    recommendation_by_id = {item.recommendation_id: item for item in response.recommendations}
    ordered_ids = list(advice.priority_ids)
    ordered_ids.extend(item.recommendation_id for item in response.recommendations if item.recommendation_id not in ordered_ids)
    return response.model_copy(
        update={
            "recommendations": [recommendation_by_id[item_id] for item_id in ordered_ids],
            "guidance": advice.guidance.strip(),
            "recommendation_mode": "model_assisted",
        }
    )


def _parse_model_advice(content: str, candidate_ids: set[str]) -> _ModelRecommendationAdvice:
    """从普通文本中定位一个 JSON 对象，并将模型输出严格收束到已有候选集合。"""

    decoder = json.JSONDecoder()
    payload: object | None = None
    for index, char in enumerate(content):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise ValueError("模型没有返回可解析的建议 JSON。")
    advice = _ModelRecommendationAdvice.model_validate(payload)
    seen: set[str] = set()
    valid_ids: list[str] = []
    for item_id in advice.priority_ids:
        if item_id in candidate_ids and item_id not in seen:
            valid_ids.append(item_id)
            seen.add(item_id)
    if not valid_ids:
        raise ValueError("模型建议没有引用有效候选。")
    return advice.model_copy(update={"priority_ids": valid_ids})


def _with_local_fallback(response: DataRecommendationResponse) -> DataRecommendationResponse:
    """模型不可用不能让客户面对空白；本地候选仍是完整且可执行的交付。"""

    warnings = list(response.warnings)
    warnings.append("模型建议暂不可用，已使用本地字段画像整理方向。")
    return response.model_copy(update={"recommendation_mode": "local_fallback", "warnings": warnings[:8]})


def _build_recommendations(
    profile: DataDatasetProfileResponse,
    goal: str,
) -> list[DataRecommendation]:
    """按数据形态添加固定有限的建议，不凭主题词臆测不存在的字段。"""

    columns = profile.columns
    numeric_columns = [column for column in columns if column.inferred_type == "number"]
    date_columns = [column for column in columns if column.inferred_type == "date"]
    category_columns = [
        column
        for column in columns
        if column.inferred_type in {"text", "boolean"}
        and 2 <= column.unique_count <= 24
        and not _is_identifier(column.name)
    ]
    recommendations: list[DataRecommendation] = []

    primary_numeric = _pick_preferred(numeric_columns)
    primary_date = _pick_preferred(date_columns)
    primary_category = _pick_preferred(category_columns)

    if primary_date and primary_numeric:
        recommendations.append(
            DataRecommendation(
                recommendation_id="time_trend",
                question=f"看看{primary_numeric.name}随{primary_date.name}的变化趋势",
                route="trend",
                source_columns=[primary_date.name, primary_numeric.name],
                aggregation="sum",
                chart_candidate="line",
                rationale="已识别日期与数值字段，按时间聚合可以观察变化方向。",
                expected_output="时间趋势表和折线图预览",
            )
        )

    if primary_category and primary_numeric:
        recommendations.append(
            DataRecommendation(
                recommendation_id="category_comparison",
                question=f"比较各{primary_category.name}的{primary_numeric.name}",
                route="comparison",
                source_columns=[primary_category.name, primary_numeric.name],
                aggregation="sum",
                chart_candidate="bar",
                rationale="有限类别与数值字段可以形成清晰的分组对比，避免直接阅读整表。",
                expected_output="分组汇总表和条形图预览",
            )
        )
        if primary_category.unique_count <= 8:
            recommendations.append(
                DataRecommendation(
                    recommendation_id="category_composition",
                    question=f"查看{primary_category.name}的{primary_numeric.name}构成占比",
                    route="composition",
                    source_columns=[primary_category.name, primary_numeric.name],
                    aggregation="sum",
                    chart_candidate="doughnut",
                    rationale="类别数量适合构成视图，可帮助快速判断主要来源。",
                    expected_output="构成汇总表和环形图预览",
                )
            )
    elif primary_category:
        recommendations.append(
            DataRecommendation(
                recommendation_id="category_distribution",
                question=f"统计{primary_category.name}的记录分布",
                route="distribution",
                source_columns=[primary_category.name],
                aggregation="count",
                chart_candidate="bar",
                rationale="当前没有可配对的数值字段，仍可先了解各类别的记录量。",
                expected_output="类别计数表和条形图预览",
            )
        )

    if numeric_columns and len(recommendations) < 4:
        numeric_names = "、".join(column.name for column in numeric_columns[:3])
        recommendations.append(
            DataRecommendation(
                recommendation_id="numeric_overview",
                question=f"汇总{numeric_names}的规模与波动范围",
                route="distribution",
                source_columns=[column.name for column in numeric_columns[:2]],
                rationale="先建立数值字段的合计、均值和范围认知，再决定是否需要深入比较。",
                expected_output="核心指标和数值概览表",
            )
        )

    if _has_quality_signal(profile) or not recommendations:
        recommendations.append(
            DataRecommendation(
                recommendation_id="data_quality",
                question="先检查缺失、重复和异常格式对结果的影响",
                route="quality",
                source_columns=[],
                rationale="先识别质量问题，能避免后续图表把空值、重复记录或格式混乱当成事实。",
                expected_output="数据质量概览和处理提示",
            )
        )

    # 详细目标并不生成未经验证的新路线，但优先把它置顶，让客户仍可从自己的问题开始。
    normalized_goal = " ".join(goal.split())
    if normalized_goal and _contains_goal_hint(normalized_goal):
        recommendations = _prefer_goal_related(recommendations, normalized_goal)
    return recommendations[:4]


def _pick_preferred(columns: list[DataColumnProfile]) -> DataColumnProfile | None:
    """优先选择非空比例高、名称更像业务字段的列；不读取任何单元格值。"""

    if not columns:
        return None
    return max(
        columns,
        key=lambda column: (
            column.non_null_count,
            -column.parse_issue_count,
            -column.missing_count,
            -column.index,
        ),
    )


def _is_identifier(name: str) -> bool:
    normalized = name.strip().lower()
    return any(hint in normalized for hint in _IDENTIFIER_HINTS)


def _has_quality_signal(profile: DataDatasetProfileResponse) -> bool:
    quality = profile.quality_summary
    return any(
        (
            quality.missing_cell_count,
            quality.duplicate_row_count,
            quality.empty_column_count,
            quality.duplicate_header_count,
            quality.parse_issue_column_count,
        )
    )


def _contains_goal_hint(goal: str) -> bool:
    normalized = goal.lower()
    return any(hint in normalized for hint in _GOAL_HINTS)


def _prefer_goal_related(
    recommendations: list[DataRecommendation],
    goal: str,
) -> list[DataRecommendation]:
    """只调整已有建议的展示顺序，绝不让用户文字绕过字段适用条件。"""

    preferred_routes: list[str] = []
    if any(word in goal for word in ("趋势", "同比", "环比")):
        preferred_routes.append("trend")
    if any(word in goal for word in ("构成", "占比", "比例", "份额")):
        preferred_routes.append("composition")
    if any(word in goal for word in ("对比", "排行", "排名")):
        preferred_routes.append("comparison")
    if any(word in goal for word in ("质量", "缺失", "重复", "异常")):
        preferred_routes.append("quality")
    return sorted(
        recommendations,
        key=lambda item: (preferred_routes.index(item.route) if item.route in preferred_routes else len(preferred_routes), item.recommendation_id),
    )


def _guidance_for(
    profile: DataDatasetProfileResponse,
    recommendations: list[DataRecommendation],
    goal: str,
) -> str:
    if goal.strip():
        return "已按当前字段准备可执行方向；也可以直接点击一张建议卡开始本地分析。"
    if recommendations:
        return "不知道从哪里看起？选择一个方向，系统会只用已识别字段生成可复核预览。"
    return "当前数据没有形成可安全执行的分析组合；可先检查字段类型、缺失值或重新选择文件。"


def _recommendation_warnings(
    profile: DataDatasetProfileResponse,
    recommendations: list[DataRecommendation],
) -> list[str]:
    warnings: list[str] = []
    if not any(item.route == "trend" for item in recommendations):
        warnings.append("未识别到可配对的日期与数值字段，暂不推荐趋势图。")
    if not any(item.chart_candidate for item in recommendations):
        warnings.append("当前只能进行质量检查；请确认是否存在可用的类别、日期或数值字段。")
    return warnings
