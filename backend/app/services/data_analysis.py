"""数据工作台 D2 的本地计划、校验与确定性计算服务。

规划器只依据 D1 的结构画像与客户目标选择少量标准视图；它不会把原始行交给模型，也不会
执行客户提供的代码、公式、SQL 或表达式。执行器只接收已校验的白名单操作，D3 将直接复用
这里返回的聚合表与图表合同渲染工作簿，避免再次计算出另一套数字。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.schemas.data_agent import (
    DataAnalysisOperation,
    DataAnalysisPlan,
    DataAnalysisPreviewRequest,
    DataAnalysisPreviewResponse,
    DataAnalysisTable,
    DataAnalysisTraceStep,
    DataChartContract,
    DataCleaningAction,
    DataColumnProfile,
    DataMetric,
    DataQualityFinding,
)
from app.services.data_workspace import DataWorkspaceError, load_data_dataset_for_analysis

try:  # 与 D1 相同：依赖缺失转为客户可理解错误，而不是接口 500。
    import pandas as pd
except ImportError:  # pragma: no cover - 正常安装 requirements 时不会走到这里。
    pd = None


class DataAnalysisError(ValueError):
    """数据工作台 D2 的可操作错误。"""


class DataPlanValidationError(DataAnalysisError):
    """受控计划不满足列、类型或操作白名单时抛出。"""


_ALLOWED_OPERATIONS = {"overview", "numeric_summary", "group_aggregate", "time_series", "numeric_series"}
_NUMERIC_HINTS = ("金额", "销售", "收入", "数量", "订单", "成本", "利润", "value", "amount", "sales", "revenue", "count")
_CATEGORY_HINTS = ("区域", "地区", "产品", "类别", "渠道", "部门", "region", "area", "product", "category", "channel", "department")
_IDENTIFIER_HINTS = ("编号", "订单号", "id", "code", "编码", "手机号", "电话")
_TREND_WORDS = ("趋势", "trend", "同比", "环比", "月度", "季度", "年度", "时间", "折线", "曲线", "line")
_STRUCTURE_WORDS = ("构成", "占比", "比例", "结构", "份额", "分布")
_COMPARISON_WORDS = ("对比", "比较", "分组", "类别", "区域", "排行", "排名", "top", "构成", "占比")
# 没有日期列的数据同样可能存在自然顺序，例如设备位置与评分、实验浓度与响应、批次序号与耗时。
# 这些名称提示只用于从已识别数值列中选择候选，真实数值、排序和聚合仍由本地确定性 Tool 完成。
_NUMERIC_SEQUENCE_HINTS = ("位置", "序号", "轮次", "阶段", "步", "index", "step", "position", "x")
_NUMERIC_MEASURE_HINTS = ("清晰", "评分", "得分", "质量", "响应", "强度", "耗时", "速度", "rate", "score", "value", "y")


@dataclass
class DataAnalysisComputation:
    """D2/D3 共享的一次本地计算结果。

    API 只返回 ``preview`` 中的受限聚合摘要；D3 则消费这里未格式化的派生 DataFrame，
    以便在 Excel 中写入可编辑数值和原生图表。它们来自同一次受控计算，避免预览和导出
    因重新读取或字符串反解析而出现两套数字。
    """

    preview: DataAnalysisPreviewResponse
    source_frame: Any
    cleaned_frame: Any
    table_frames: dict[str, Any]


def preview_data_analysis(request: DataAnalysisPreviewRequest) -> DataAnalysisPreviewResponse:
    """完成 D2 的只读预览闭环，不写文件、不调用模型或网络。"""

    return compute_data_analysis(request).preview


def compute_data_analysis(request: DataAnalysisPreviewRequest) -> DataAnalysisComputation:
    """执行一次可供 D2 预览与 D3 工作簿复用的受控本地计算。"""

    if pd is None:
        raise DataAnalysisError("数据分析依赖未安装，请在 backend 目录安装 requirements.txt。")
    try:
        profile, frame = load_data_dataset_for_analysis(request.dataset_name)
    except DataWorkspaceError as exc:
        raise DataAnalysisError(str(exc)) from exc

    plan = build_data_analysis_plan(
        dataset_name=request.dataset_name,
        source_sha256=profile.source_sha256,
        columns=profile.columns,
        goal=request.goal,
        max_chart_count=request.max_chart_count,
    )
    validate_data_analysis_plan(plan, profile.columns, request.max_chart_count)
    cleaned_frame, cleaning_actions = _apply_safe_cleaning(frame, profile.columns)
    metrics, tables, charts, skipped_items, execution_warnings, table_frames = _execute_plan(plan, cleaned_frame)
    preview = DataAnalysisPreviewResponse(
        dataset_profile=profile,
        analysis_plan=plan,
        quality_findings=_quality_findings(profile),
        cleaning_actions=cleaning_actions,
        metrics=metrics,
        analysis_tables=tables,
        charts=charts,
        warnings=[*profile.warnings, *execution_warnings],
        skipped_items=skipped_items,
        trace=[
            DataAnalysisTraceStep(stage="profile", status="completed", detail="已复用受控文件画像与稳定列名。"),
            DataAnalysisTraceStep(stage="plan", status="completed", detail="已由本地结构画像规划有限标准视图；未调用模型。"),
            DataAnalysisTraceStep(stage="validate", status="completed", detail="计划列、类型、操作和图表数量均通过白名单校验。"),
            DataAnalysisTraceStep(stage="execute", status="completed", detail="已在本地受控副本完成聚合；尚未生成工作簿。"),
        ],
    )
    return DataAnalysisComputation(
        preview=preview,
        source_frame=frame,
        cleaned_frame=cleaned_frame,
        table_frames=table_frames,
    )


def build_data_analysis_plan(
    *,
    dataset_name: str,
    source_sha256: str,
    columns: list[DataColumnProfile],
    goal: str,
    max_chart_count: int,
) -> DataAnalysisPlan:
    """依据有限画像建立保守合同，而非解释自然语言为任意程序。"""

    normalized_goal = _normalise_goal(goal)
    numeric_columns = [column for column in columns if column.inferred_type == "number"]
    date_columns = [column for column in columns if column.inferred_type == "date"]
    category_columns = [
        column
        for column in columns
        if column.inferred_type in {"text", "boolean"}
        and 2 <= column.unique_count <= 200
        and not _contains_hint(column.name, _IDENTIFIER_HINTS)
    ]
    primary_numeric = _pick_column(numeric_columns, normalized_goal, _NUMERIC_HINTS)
    primary_date = _pick_column(date_columns, normalized_goal, ())
    primary_category = _pick_column(category_columns, normalized_goal, _CATEGORY_HINTS)

    operations: list[DataAnalysisOperation] = [
        DataAnalysisOperation(
            operation_id="overview",
            operation_type="overview",
            title="数据概览与质量提示",
            rationale="所有数据集均先给出范围、缺失与重复提示，方便判断后续统计的可信边界。",
        )
    ]
    warnings: list[str] = []
    if not numeric_columns:
        warnings.append("未发现高置信度数值列；本次只提供记录数量和类别分布，不生成数值汇总。")
    else:
        operations.append(
            DataAnalysisOperation(
                operation_id="numeric_summary",
                operation_type="numeric_summary",
                title="核心数值概览",
                source_columns=[column.name for column in numeric_columns[:4]],
                rationale="用合计、均值、中位数和范围建立基础量级认知。",
            )
        )

    chart_budget = max_chart_count
    wants_trend = _contains_any(normalized_goal, _TREND_WORDS)
    wants_structure = _contains_any(normalized_goal, _STRUCTURE_WORDS)
    wants_comparison = _contains_any(normalized_goal, _COMPARISON_WORDS)

    # “焦点位置 - 有效清晰度”一类任务没有日期，却明确是连续变化关系。旧版只接受日期趋势，
    # 容易把这种表误做成类别柱图。这里优先创建受控的数值序列合同，X 排序后同值取均值。
    sequence_column = _pick_column(numeric_columns, normalized_goal, _NUMERIC_SEQUENCE_HINTS)
    measure_candidates = [item for item in numeric_columns if item.name != getattr(sequence_column, "name", "")]
    measure_column = _pick_column(measure_candidates, normalized_goal, _NUMERIC_MEASURE_HINTS)
    has_semantic_numeric_curve = (
        sequence_column is not None
        and measure_column is not None
        and (_contains_hint(sequence_column.name, _NUMERIC_SEQUENCE_HINTS)
             or _contains_hint(measure_column.name, _NUMERIC_MEASURE_HINTS))
    )
    if chart_budget > 0 and sequence_column is not None and measure_column is not None and has_semantic_numeric_curve:
        operations.append(
            DataAnalysisOperation(
                operation_id="numeric_series",
                operation_type="numeric_series",
                title=f"{measure_column.name}随{sequence_column.name}变化",
                source_columns=[sequence_column.name, measure_column.name],
                aggregation="mean",
                chart_type="line",
                rationale="已识别连续数值字段；按横轴排序并对相同位置取均值，适合观察曲线变化。",
            )
        )
        chart_budget -= 1

    if primary_date is not None and primary_numeric is not None and chart_budget > 0:
        operations.append(
            DataAnalysisOperation(
                operation_id="time_series",
                operation_type="time_series",
                title=f"{primary_numeric.name}月度趋势",
                source_columns=[primary_date.name, primary_numeric.name],
                aggregation="sum",
                chart_type="line",
                rationale="日期与数值字段均可用，按月聚合可观察变化方向。" if wants_trend else "日期与数值字段可形成基础时间趋势视图。",
            )
        )
        chart_budget -= 1

    # 连续测量表（例如焦点位置与有效清晰度）本身已经有明确的曲线语义。旧规则仍会附带
    # “阶段 - 焦点位置”柱图，既占用客户的图表预算，也经常偏离这类文件真正的观察目的。
    # 只有客户明确提到比较/构成，或没有可成立的连续曲线时，才补充分组图。
    should_add_category_chart = not has_semantic_numeric_curve or wants_comparison
    if primary_category is not None and chart_budget > 0 and should_add_category_chart:
        chart_type = "pie" if wants_structure and primary_category.unique_count <= 8 else "bar"
        aggregation = "sum" if primary_numeric is not None else "count"
        source_columns = [primary_category.name]
        if primary_numeric is not None:
            source_columns.append(primary_numeric.name)
        operations.append(
            DataAnalysisOperation(
                operation_id="category_breakdown",
                operation_type="group_aggregate",
                title=f"按{primary_category.name}对比",
                source_columns=source_columns,
                aggregation=aggregation,
                chart_type=chart_type,
                rationale="使用有限类别字段进行分组对比；缺失类别不会被当作有效分类。",
            )
        )
        chart_budget -= 1

    # 客户未点明字段时，也只补一项与主类别不同的互补视图，避免"看起来很全面"但无意义。
    if chart_budget > 0 and primary_numeric is not None and should_add_category_chart:
        secondary_category = next((item for item in category_columns if item.name != getattr(primary_category, "name", "")), None)
        if secondary_category is not None:
            operations.append(
                DataAnalysisOperation(
                    operation_id="secondary_breakdown",
                    operation_type="group_aggregate",
                    title=f"按{secondary_category.name}补充对比",
                    source_columns=[secondary_category.name, primary_numeric.name],
                    aggregation="sum",
                    chart_type="bar",
                    rationale="补充不同分类维度，避免只用单一维度解释数据。",
                )
            )

    return DataAnalysisPlan(
        dataset_name=dataset_name,
        source_sha256=source_sha256,
        goal=goal.strip(),
        operations=operations,
        warnings=warnings,
    )


def validate_data_analysis_plan(
    plan: DataAnalysisPlan,
    columns: list[DataColumnProfile],
    max_chart_count: int,
) -> None:
    """在执行前逐项验证计划，确保 D2 永远不会变成表达式执行入口。"""

    column_by_name = {column.name: column for column in columns}
    chart_count = 0
    seen_operation_ids: set[str] = set()
    for operation in plan.operations:
        if operation.operation_id in seen_operation_ids:
            raise DataPlanValidationError("分析计划包含重复操作 ID，已拒绝执行。")
        seen_operation_ids.add(operation.operation_id)
        if operation.operation_type not in _ALLOWED_OPERATIONS:
            raise DataPlanValidationError("分析计划包含未登记的操作类型。")
        if any(column not in column_by_name for column in operation.source_columns):
            raise DataPlanValidationError("分析计划引用了不存在的字段，已拒绝执行。")
        if operation.chart_type is not None:
            chart_count += 1
        if operation.operation_type == "overview":
            if operation.source_columns or operation.aggregation is not None or operation.chart_type is not None:
                raise DataPlanValidationError("数据概览不能附带列计算或图表操作。")
        elif operation.operation_type == "numeric_summary":
            if not operation.source_columns or any(column_by_name[name].inferred_type != "number" for name in operation.source_columns):
                raise DataPlanValidationError("数值概览只能引用已识别的数值字段。")
        elif operation.operation_type == "time_series":
            if len(operation.source_columns) != 2 or operation.aggregation != "sum" or operation.chart_type != "line":
                raise DataPlanValidationError("时间趋势必须使用一个日期字段、一个数值字段和折线图。")
            date_column, value_column = (column_by_name[name] for name in operation.source_columns)
            if date_column.inferred_type != "date" or value_column.inferred_type != "number":
                raise DataPlanValidationError("时间趋势字段类型不适用。")
        elif operation.operation_type == "numeric_series":
            if len(operation.source_columns) != 2 or operation.aggregation != "mean" or operation.chart_type != "line":
                raise DataPlanValidationError("数值曲线必须使用两个数值字段、均值聚合和折线图。")
            x_column, y_column = (column_by_name[name] for name in operation.source_columns)
            if x_column.inferred_type != "number" or y_column.inferred_type != "number":
                raise DataPlanValidationError("数值曲线只能引用已识别的数值字段。")
        elif operation.operation_type == "group_aggregate":
            if len(operation.source_columns) not in {1, 2} or operation.aggregation not in {"count", "sum", "mean"}:
                raise DataPlanValidationError("分组分析参数不在白名单内。")
            dimension = column_by_name[operation.source_columns[0]]
            if dimension.inferred_type not in {"text", "boolean"}:
                raise DataPlanValidationError("分组维度只能是文本或布尔字段。")
            if operation.aggregation == "count" and len(operation.source_columns) != 1:
                raise DataPlanValidationError("计数分组不能附带额外数值字段。")
            if operation.aggregation in {"sum", "mean"} and (
                len(operation.source_columns) != 2 or column_by_name[operation.source_columns[1]].inferred_type != "number"
            ):
                raise DataPlanValidationError("数值分组必须指定一个高置信度数值字段。")
    if chart_count > max_chart_count:
        raise DataPlanValidationError("分析计划图表数量超过本次允许上限。")


def _apply_safe_cleaning(frame: Any, columns: list[DataColumnProfile]) -> tuple[Any, list[DataCleaningAction]]:
    """只做可逆、可记录的标准化；不删除记录、不填补缺失、不裁剪异常值。"""

    cleaned = frame.copy(deep=True)
    whitespace_count = 0
    for column in columns:
        if column.inferred_type not in {"text", "mixed", "boolean"}:
            continue
        series = cleaned[column.name]
        if series.dtype != object:
            continue
        normalized = series.map(lambda value: value.strip() if isinstance(value, str) else value)
        whitespace_count += int((series != normalized).fillna(False).sum())
        cleaned[column.name] = normalized
    if whitespace_count:
        return cleaned, [DataCleaningAction(action_id="trim_text_whitespace", title="去除文本首尾空白", affected_count=whitespace_count, detail="仅在派生分析副本中规范化字符串；源文件保持不变。")]
    return cleaned, [DataCleaningAction(action_id="safe_copy_only", title="保留源数据副本", affected_count=0, detail="没有需要自动规范化的高置信度安全转换；未删除、填补或裁剪任何记录。")]


def _execute_plan(
    plan: DataAnalysisPlan,
    frame: Any,
) -> tuple[
    list[DataMetric],
    list[DataAnalysisTable],
    list[DataChartContract],
    list[str],
    list[str],
    dict[str, Any],
]:
    """依次执行已验证的标准操作，单项失败只记录跳过而不撤回其它结果。"""

    metrics: list[DataMetric] = []
    tables: list[DataAnalysisTable] = []
    charts: list[DataChartContract] = []
    skipped: list[str] = []
    warnings: list[str] = []
    table_frames: dict[str, Any] = {}
    for operation in plan.operations:
        try:
            if operation.operation_type == "overview":
                metrics.extend(_overview_metrics(frame, operation.operation_id))
            elif operation.operation_type == "numeric_summary":
                table, operation_metrics, table_frame = _numeric_summary(frame, operation)
                tables.append(table)
                metrics.extend(operation_metrics)
                table_frames[table.table_id] = table_frame
            elif operation.operation_type == "group_aggregate":
                table, table_frame = _group_aggregate(frame, operation)
                tables.append(table)
                table_frames[table.table_id] = table_frame
                if operation.chart_type is not None:
                    charts.append(_chart_contract(operation, table))
            elif operation.operation_type == "time_series":
                table, table_frame = _time_series(frame, operation)
                tables.append(table)
                table_frames[table.table_id] = table_frame
                charts.append(_chart_contract(operation, table))
            elif operation.operation_type == "numeric_series":
                table, table_frame = _numeric_curve_series(frame, operation)
                tables.append(table)
                table_frames[table.table_id] = table_frame
                charts.append(_chart_contract(operation, table))
        except DataAnalysisError as exc:
            skipped.append(f"{operation.title}：{exc}")
        except Exception:
            # 不向 API 或任务历史泄露行级异常；D4 会补内部关联 ID，客户只需可行动提示。
            skipped.append(f"{operation.title}：本项计算未完成，已保留其它可用结果。")
    if not tables:
        warnings.append("本次没有形成可用聚合表；请确认数据中是否存在可统计字段。")
    return metrics, tables, charts, skipped, warnings, table_frames


def _overview_metrics(frame: Any, operation_id: str) -> list[DataMetric]:
    return [
        DataMetric(metric_id="row_count", name="记录数", value=float(len(frame)), unit="行", aggregation="count", operation_id=operation_id),
        DataMetric(metric_id="column_count", name="字段数", value=float(len(frame.columns)), unit="列", aggregation="count", operation_id=operation_id),
    ]


def _numeric_summary(frame: Any, operation: DataAnalysisOperation) -> tuple[DataAnalysisTable, list[DataMetric], Any]:
    rows: list[list[str]] = []
    workbook_rows: list[list[float | str]] = []
    metrics: list[DataMetric] = []
    for index, column_name in enumerate(operation.source_columns, start=1):
        series = _numeric_series(frame[column_name])
        valid = series.dropna()
        if valid.empty:
            continue
        total = float(valid.sum())
        mean = float(valid.mean())
        median = float(valid.median())
        rows.append([column_name, _format_number(total), _format_number(mean), _format_number(median), _format_number(float(valid.min())), _format_number(float(valid.max()))])
        workbook_rows.append([column_name, total, mean, median, float(valid.min()), float(valid.max())])
        metric_prefix = f"{operation.operation_id}_{index}"
        metrics.extend(
            [
                DataMetric(metric_id=f"{metric_prefix}_sum", name=f"{column_name}合计", value=total, unit="数值", aggregation="sum", source_columns=[column_name], operation_id=operation.operation_id),
                DataMetric(metric_id=f"{metric_prefix}_mean", name=f"{column_name}均值", value=mean, unit="数值", aggregation="mean", source_columns=[column_name], operation_id=operation.operation_id),
                DataMetric(metric_id=f"{metric_prefix}_median", name=f"{column_name}中位数", value=median, unit="数值", aggregation="median", source_columns=[column_name], operation_id=operation.operation_id),
            ]
        )
    if not rows:
        raise DataAnalysisError("没有可解析的数值记录。")
    columns = ["字段", "合计", "均值", "中位数", "最小值", "最大值"]
    return (
        DataAnalysisTable(
            table_id="numeric_summary_table",
            title=operation.title,
            columns=columns,
            rows=rows,
            source_columns=operation.source_columns,
            operation_id=operation.operation_id,
        ),
        metrics,
        pd.DataFrame(workbook_rows, columns=columns),
    )


def _group_aggregate(frame: Any, operation: DataAnalysisOperation) -> tuple[DataAnalysisTable, Any]:
    dimension_name = operation.source_columns[0]
    dimensions = frame[dimension_name].map(_normalise_category)
    valid_dimension = dimensions.notna() & dimensions.ne("")
    if not bool(valid_dimension.any()):
        raise DataAnalysisError("分组字段没有可用类别。")
    if operation.aggregation == "count":
        result = dimensions[valid_dimension].value_counts(dropna=True).rename("记录数")
        value_name = "记录数"
    else:
        value_name = operation.source_columns[1]
        values = _numeric_series(frame[value_name])
        usable = valid_dimension & values.notna()
        if not bool(usable.any()):
            raise DataAnalysisError("分组字段对应的数值记录不可用。")
        working = pd.DataFrame({dimension_name: dimensions[usable], value_name: values[usable]})
        group = working.groupby(dimension_name, dropna=True)[value_name]
        result = group.sum(min_count=1) if operation.aggregation == "sum" else group.mean()
    result = result.sort_values(ascending=operation.sort_direction == "ascending")
    truncated = len(result) > operation.row_limit
    limited = result.head(operation.row_limit)
    if len(limited) < 2:
        raise DataAnalysisError("可比较类别少于两项，无法生成有效分组视图。")
    columns = [dimension_name, value_name]
    return (
        DataAnalysisTable(
            table_id=f"{operation.operation_id}_table",
            title=operation.title,
            columns=columns,
            rows=[[str(index), _format_number(float(value))] for index, value in limited.items()],
            source_columns=operation.source_columns,
            operation_id=operation.operation_id,
            truncated=truncated,
        ),
        # Excel 的分析表保留完整聚合，图表在 D3 仍根据受控行上限仅使用前若干项，避免
        # 数百个分类标签让交付物不可读。
        result.reset_index().set_axis(columns, axis=1),
    )


def _time_series(frame: Any, operation: DataAnalysisOperation) -> tuple[DataAnalysisTable, Any]:
    date_name, value_name = operation.source_columns
    dates = pd.to_datetime(frame[date_name], errors="coerce")
    values = _numeric_series(frame[value_name])
    usable = dates.notna() & values.notna()
    if not bool(usable.any()):
        raise DataAnalysisError("日期或数值字段没有可用于趋势计算的记录。")
    working = pd.DataFrame({"period": dates[usable].dt.to_period("M").astype(str), value_name: values[usable]})
    result = working.groupby("period", sort=True)[value_name].sum(min_count=1)
    if len(result) < 2:
        raise DataAnalysisError("可用时间点少于两期，暂不生成趋势图。")
    truncated = len(result) > operation.row_limit
    limited = result.head(operation.row_limit)
    columns = ["月份", value_name]
    return (
        DataAnalysisTable(
            table_id=f"{operation.operation_id}_table",
            title=operation.title,
            columns=columns,
            rows=[[str(index), _format_number(float(value))] for index, value in limited.items()],
            source_columns=operation.source_columns,
            operation_id=operation.operation_id,
            truncated=truncated,
        ),
        # 趋势图与 D2 预览保持同一受控点数，避免把超长历史序列直接塞进单张图表。
        limited.rename_axis(columns[0]).reset_index(),
    )


def _numeric_curve_series(frame: Any, operation: DataAnalysisOperation) -> tuple[DataAnalysisTable, Any]:
    """按数值横轴排序生成曲线底稿；相同横轴位置取均值以避免线图重叠误导。"""

    x_name, y_name = operation.source_columns
    x_values = _numeric_series(frame[x_name])
    y_values = _numeric_series(frame[y_name])
    usable = x_values.notna() & y_values.notna()
    if not bool(usable.any()):
        raise DataAnalysisError("两个数值字段没有可用于曲线计算的共同记录。")

    working = pd.DataFrame({x_name: x_values[usable], y_name: y_values[usable]})
    result = working.groupby(x_name, sort=True)[y_name].mean()
    if result.empty:
        raise DataAnalysisError("数值横轴没有可用于曲线计算的记录。")
    limited = result.head(120)
    columns = [x_name, y_name]
    rows = [[_format_number(float(index)), _format_number(float(value))] for index, value in limited.items()]
    return (
        DataAnalysisTable(
            table_id=f"{operation.operation_id}_table",
            title=operation.title,
            columns=columns,
            rows=rows,
            source_columns=operation.source_columns,
            operation_id=operation.operation_id,
            truncated=len(result) > len(limited),
        ),
        limited.rename_axis(x_name).reset_index(),
    )


def _chart_contract(operation: DataAnalysisOperation, table: DataAnalysisTable) -> DataChartContract:
    if operation.chart_type is None:
        raise DataAnalysisError("图表操作缺少图表类型。")
    return DataChartContract(chart_id=f"{operation.operation_id}_chart", chart_type=operation.chart_type, title=operation.title, table_id=table.table_id, category_column=table.columns[0], value_column=table.columns[1], operation_id=operation.operation_id)


def _quality_findings(profile: Any) -> list[DataQualityFinding]:
    summary = profile.quality_summary
    findings: list[DataQualityFinding] = []
    if summary.missing_cell_count:
        findings.append(DataQualityFinding(finding_id="missing_values", severity="warning", title="存在缺失值", impact="缺失记录不会被自动填补；涉及字段的统计会排除无法解析的值。", affected_count=summary.missing_cell_count, handling="仅记录影响范围，保留源值和缺失状态。"))
    if summary.duplicate_row_count:
        findings.append(DataQualityFinding(finding_id="duplicate_rows", severity="warning", title="存在完全重复行", impact="本次统计默认保留重复行，数值可能包含重复贡献。", affected_count=summary.duplicate_row_count, handling="不自动删除；D3 会在质量工作表中保留提示。"))
    if not findings:
        findings.append(DataQualityFinding(finding_id="quality_baseline", severity="info", title="未发现 D1 可识别的缺失或完全重复行", impact="这不等同于业务数据已经核验。", affected_count=0, handling="继续保留字段类型和范围检查结果。"))
    return findings


def _normalise_goal(goal: str) -> str:
    return re.sub(r"[\s_\-]+", "", goal.casefold())


def _pick_column(columns: list[DataColumnProfile], goal: str, hints: tuple[str, ...]) -> DataColumnProfile | None:
    if not columns:
        return None
    explicit = [column for column in columns if _normalise_goal(column.name) in goal]
    if explicit:
        return explicit[0]
    hinted = [column for column in columns if _contains_hint(column.name, hints)]
    return hinted[0] if hinted else columns[0]


def _contains_hint(value: str, hints: tuple[str, ...]) -> bool:
    normalized = _normalise_goal(value)
    return any(_normalise_goal(hint) in normalized for hint in hints)


def _contains_any(value: str, words: tuple[str, ...]) -> bool:
    return any(_normalise_goal(word) in value for word in words)


def _numeric_series(series: Any) -> Any:
    normalized = series.map(lambda value: str(value).replace(",", "").replace("¥", "").replace("￥", "").replace("$", "").strip() if value is not None else None)
    return pd.to_numeric(normalized, errors="coerce")


def _normalise_category(value: Any) -> str | None:
    if value is None or bool(pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _format_number(value: float) -> str:
    if math.isfinite(value) and value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
