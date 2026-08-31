"""把调度台的字段加工自然语言编译为受限的 D5.3 操作队列。

这里刻意不做通用公式解析，也不把整份数据交给模型。首版只依据当前数据画像中的字段名、
字段类型和几个明确的业务词，生成 ``DataTransformOperationInput``；无法确定字段或操作时
返回可理解的澄清原因。后续接入模型时，这个结果仍必须经过同一套本地字段与类型校验。
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.schemas.data_agent import DataColumnProfile, DataTransformOperationInput
from app.services.data_workspace import DataWorkspaceError, get_data_dataset_profile


class DataTransformIntentError(ValueError):
    """自然语言无法安全映射到有限字段加工动作时抛出。"""


@dataclass(frozen=True)
class DataTransformIntent:
    """供 Commander 写入计划的脱敏字段加工意图。"""

    dataset_name: str
    source_sha256: str
    operations: tuple[DataTransformOperationInput, ...]
    summary: str


_NUMBER_HINTS = (
    "金额", "销售", "收入", "成本", "价格", "数量", "总额", "分数", "得分",
    "score", "amount", "value", "count",
)


def build_data_transform_intent(dataset_name: str, goal: str) -> DataTransformIntent:
    """从当前画像和客户目标构造有限字段操作，不读取或返回原始行。"""

    try:
        profile = get_data_dataset_profile(dataset_name)
    except DataWorkspaceError as exc:
        raise DataTransformIntentError(str(exc)) from exc

    message = goal.strip()
    lowered = message.casefold()
    columns = list(profile.columns)
    numeric = [column for column in columns if column.inferred_type == "number"]
    dates = [column for column in columns if column.inferred_type == "date"]
    text = [column for column in columns if column.inferred_type in {"text", "boolean"}]
    mentioned = _mentioned_columns(message, columns)
    operations: list[DataTransformOperationInput] = []
    result_names: set[str] = {column.name for column in columns}

    if any(word in lowered for word in ("月份", "提取月", "按月", "月度字段")):
        primary = _pick_mentioned_or_first(mentioned, dates)
        if primary is None:
            raise DataTransformIntentError("已识别到“月份”加工，但当前数据没有可用日期字段，请明确选择日期列。")
        operations.append(_operation("date_part", primary, result_names, date_part="month"))

    if any(word in lowered for word in ("排名", "排行", "top")):
        primary = _pick_numeric(mentioned, numeric, lowered)
        if primary is None:
            raise DataTransformIntentError("已识别到“排名”加工，但当前数据没有可用数值字段，请明确选择排名依据。")
        operations.append(_operation("rank", primary, result_names))

    if "累计" in lowered:
        primary = _pick_numeric(mentioned, numeric, lowered)
        date = _pick_mentioned_or_first(mentioned, dates)
        if primary is None or date is None:
            raise DataTransformIntentError("“累计”需要一个数值字段和一个日期字段，请明确写出两列名称。")
        operations.append(_operation("cumulative", primary, result_names, secondary_column=date.name))

    if any(word in lowered for word in ("环比增长率", "环比百分比", "增长率")):
        primary = _pick_numeric(mentioned, numeric, lowered)
        date = _pick_mentioned_or_first(mentioned, dates)
        if primary is None or date is None:
            raise DataTransformIntentError("“环比增长率”需要一个数值字段和一个日期字段，请明确写出两列名称。")
        operations.append(_operation("period_rate", primary, result_names, secondary_column=date.name))
    elif "环比" in lowered:
        primary = _pick_numeric(mentioned, numeric, lowered)
        date = _pick_mentioned_or_first(mentioned, dates)
        if primary is None or date is None:
            raise DataTransformIntentError("“环比”需要一个数值字段和一个日期字段，请明确写出两列名称。")
        operations.append(_operation("period_change", primary, result_names, secondary_column=date.name))

    # “环比百分比”已经在上面的时间序列分支处理，不再误加一列“占比”。
    if not any(word in lowered for word in ("环比", "增长率")) and any(word in lowered for word in ("占比", "份额")):
        primary = _pick_numeric(mentioned, numeric, lowered)
        if primary is None:
            raise DataTransformIntentError("已识别到“占比”加工，但当前数据没有可用数值字段。")
        operations.append(_operation("share", primary, result_names))

    if any(word in lowered for word in ("四舍五入", "保留小数", "小数位", "保留两位", "保留一位")):
        primary = _pick_numeric(mentioned, numeric, lowered)
        if primary is None:
            raise DataTransformIntentError("数值保留位数需要一个数值字段，请明确写出字段名称。")
        operations.append(_operation("round_number", primary, result_names, round_digits=_round_digits(message)))

    if any(word in lowered for word in ("分段", "分档", "等级", "区间")):
        primary = _pick_numeric(mentioned, numeric, lowered)
        if primary is None:
            raise DataTransformIntentError("分段加工需要一个数值字段，请明确写出字段名称。")
        operations.append(_operation("segment", primary, result_names))

    if any(word in lowered for word in ("去空格", "清理空格", "文本清洗", "规范化文本")):
        primary = _pick_mentioned_or_first(mentioned, text)
        if primary is None:
            raise DataTransformIntentError("文本清洗需要一个文本字段，请明确写出字段名称。")
        operations.append(_operation("text_trim", primary, result_names))

    if any(word in lowered for word in ("四则", "相乘", "相加", "相减", "相除", "计算比率")):
        selected = [column for column in mentioned if column.inferred_type == "number"]
        if len(selected) < 2:
            selected = numeric[:2]
        if len(selected) < 2:
            raise DataTransformIntentError("四则计算需要两个数值字段，请明确写出参与计算的两列。")
        operator = "divide" if any(word in lowered for word in ("相除", "比率")) else "add" if "相加" in lowered else "subtract" if "相减" in lowered else "multiply"
        operations.append(_operation("arithmetic", selected[0], result_names, secondary_column=selected[1].name, arithmetic_operator=operator))

    if not operations:
        raise DataTransformIntentError(
            "暂时无法从这句话确定字段加工方式。可直接说：根据金额新增金额排名和累计金额，或根据日期生成月份字段。"
        )
    if len(operations) > 12:
        raise DataTransformIntentError("本次字段加工最多支持 12 个新增字段，请拆成较小的任务。")

    labels = "、".join(operation.result_column or operation.operation_type for operation in operations)
    return DataTransformIntent(
        dataset_name=dataset_name,
        source_sha256=profile.source_sha256,
        operations=tuple(operations),
        summary=f"将基于当前数据新增 {len(operations)} 个字段：{labels}。",
    )


def _mentioned_columns(message: str, columns: list[DataColumnProfile]) -> list[DataColumnProfile]:
    """按字段名长度优先匹配，避免“金额”抢先吞掉“含税金额”。"""

    lowered = message.casefold()
    return [column for column in sorted(columns, key=lambda item: len(item.name), reverse=True) if column.name.casefold() in lowered]


def _pick_mentioned_or_first(
    mentioned: list[DataColumnProfile], candidates: list[DataColumnProfile]
) -> DataColumnProfile | None:
    return next((column for column in mentioned if column in candidates), None) or (candidates[0] if candidates else None)


def _pick_numeric(mentioned: list[DataColumnProfile], candidates: list[DataColumnProfile], goal: str) -> DataColumnProfile | None:
    selected = next((column for column in mentioned if column in candidates), None)
    if selected is not None:
        return selected
    for hint in _NUMBER_HINTS:
        selected = next((column for column in candidates if hint in column.name.casefold() or hint in goal), None)
        if selected is not None:
            return selected
    return candidates[0] if candidates else None


def _operation(
    operation_type: str,
    primary: DataColumnProfile,
    result_names: set[str],
    *,
    secondary_column: str | None = None,
    date_part: str = "month",
    arithmetic_operator: str = "multiply",
    round_digits: int = 2,
) -> DataTransformOperationInput:
    """构造一项带用户可读结果名的操作，并在同轮内消除新列名冲突。"""

    labels = {
        "date_part": {"year": "年份", "month": "月份", "quarter": "季度", "weekday": "星期"}[date_part],
        "rank": "排名",
        "cumulative": "累计",
        "period_change": "环比",
        "period_rate": "环比率",
        "share": "占比",
        "round_number": f"保留{round_digits}位",
        "segment": "分段",
        "text_trim": "清理",
        "arithmetic": "计算",
    }
    base = f"{primary.name}_{labels[operation_type]}"
    result = base
    suffix = 2
    while result in result_names:
        result = f"{base}_{suffix}"
        suffix += 1
    result_names.add(result)
    return DataTransformOperationInput(
        operation_type=operation_type,  # type: ignore[arg-type]
        primary_column=primary.name,
        secondary_column=secondary_column,
        result_column=result,
        date_part=date_part,  # type: ignore[arg-type]
        arithmetic_operator=arithmetic_operator,  # type: ignore[arg-type]
        round_digits=round_digits,
    )


def _round_digits(message: str) -> int:
    match = re.search(r"(?:保留|小数点后|小数位)\s*(\d+)", message)
    if match:
        return min(6, max(0, int(match.group(1))))
    if "保留一位" in message:
        return 1
    return 2
