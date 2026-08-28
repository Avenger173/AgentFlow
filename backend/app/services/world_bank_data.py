"""受控读取 World Bank Indicators API，并把结果收敛为可验证的单张 PPT 图表。

这个 Provider 不是通用搜索工具：它只接收创作计划中已经固定的国家、指标和图表类型，
不会把模型生成的 URL、任意字段或网页正文带入运行时。这样导出的图表可回读、可审计，
也不会把不同年份的数据悄悄放在同一比较图中。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.schemas.presentation_studio import PresentationStudioDataPlan


_WORLD_BANK_API_URL = "https://api.worldbank.org/v2"
_REQUEST_TIMEOUT_SECONDS = 12.0
_MAX_TREND_POINTS = 6


@dataclass(frozen=True)
class WorldBankDataPoint:
    """一条已经通过类型与范围检查的年度指标值。"""

    country_code: str
    country_name: str
    year: int
    value: float


@dataclass(frozen=True)
class WorldBankChartData:
    """渲染层唯一可见的数据契约，不暴露未校验的 API 原始响应。"""

    slide_id: str
    chart_type: str
    indicator_code: str
    indicator_name: str
    title: str
    points: tuple[WorldBankDataPoint, ...]
    source_url: str
    retrieved_at: str

    def audit_metadata(self) -> dict[str, object]:
        return {
            "provider": "world_bank",
            "scope": "structured_indicator_data",
            "chart_type": self.chart_type,
            "slide_id": self.slide_id,
            "indicator_code": self.indicator_code,
            "indicator_name": self.indicator_name,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "points": [
                {
                    "country_code": point.country_code,
                    "country_name": point.country_name,
                    "year": point.year,
                    "value": point.value,
                }
                for point in self.points
            ],
        }


@dataclass(frozen=True)
class WorldBankDataResolution:
    """Provider 的成功/降级结果；降级绝不生成虚构图表。"""

    chart: WorldBankChartData | None
    warnings: tuple[str, ...]


def fetch_world_bank_chart_data(
    plan: PresentationStudioDataPlan,
    *,
    client: httpx.Client | None = None,
) -> WorldBankDataResolution:
    """按固定计划读取 World Bank 指标，并只在数据可比较时返回图表。"""

    # ``planned`` 是旧快照状态，``provider_planned`` 是 V3 起对固定 Provider 的明确命名。
    if plan.state not in {"planned", "provider_planned"} or plan.provider != "world_bank":
        return WorldBankDataResolution(chart=None, warnings=())
    if not _is_valid_plan(plan):
        return WorldBankDataResolution(chart=None, warnings=("结构化数据计划不完整，已跳过本次图表。",))

    country_path = ";".join(plan.country_codes)
    # 最近七个完整年度可兼顾最新性和可用性；不同国家的比较会在解析后再次取共同年份。
    current_year = datetime.now(UTC).year
    start_year = current_year - 7
    end_year = current_year - 1
    params = {
        "format": "json",
        "date": f"{start_year}:{end_year}",
        "per_page": "100",
    }
    source_url = (
        f"{_WORLD_BANK_API_URL}/country/{country_path}/indicator/{plan.indicator_code}"
        f"?format=json&date={start_year}:{end_year}&per_page=100"
    )
    should_close = client is None
    active_client = client or httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        response = active_client.get(
            f"{_WORLD_BANK_API_URL}/country/{country_path}/indicator/{plan.indicator_code}",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return WorldBankDataResolution(
            chart=None,
            warnings=(f"World Bank 指标数据暂时不可用，未生成图表：{exc}",),
        )
    finally:
        if should_close:
            active_client.close()

    points = _parse_points(payload, plan=plan)
    if plan.chart_type == "comparison_bar":
        selected = _select_comparison_points(points, country_codes=tuple(plan.country_codes))
        if len(selected) != len(plan.country_codes):
            return WorldBankDataResolution(
                chart=None,
                warnings=("所选国家没有同一年度的完整可比数据，已跳过图表，未混用不同年份。",),
            )
        year = selected[0].year
        title = f"{plan.indicator_name}对比（{year}）"
    else:
        selected = _select_trend_points(points, country_code=plan.country_codes[0])
        if len(selected) < 3:
            return WorldBankDataResolution(
                chart=None,
                warnings=("该主题可用的连续年度数据不足 3 条，已跳过图表。",),
            )
        title = f"{selected[0].country_name}{plan.indicator_name}趋势"

    return WorldBankDataResolution(
        chart=WorldBankChartData(
            slide_id=plan.slide_id,
            chart_type=plan.chart_type,
            indicator_code=plan.indicator_code,
            indicator_name=plan.indicator_name,
            title=title,
            points=selected,
            source_url=source_url,
            retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"),
        ),
        warnings=(),
    )


def _is_valid_plan(plan: PresentationStudioDataPlan) -> bool:
    if not plan.slide_id or not plan.indicator_code or not plan.indicator_name:
        return False
    if plan.chart_type == "comparison_bar":
        return 2 <= len(plan.country_codes) <= 4
    return plan.chart_type == "trend_line" and len(plan.country_codes) == 1


def _parse_points(payload: object, *, plan: PresentationStudioDataPlan) -> tuple[WorldBankDataPoint, ...]:
    """只接受请求指标与计划国家的有限数值记录，拒绝空值和非有限数。"""

    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        return ()
    allowed_codes = set(plan.country_codes)
    points: list[WorldBankDataPoint] = []
    for item in payload[1]:
        if not isinstance(item, dict):
            continue
        country_code = str(item.get("countryiso3code") or "").upper()
        if country_code not in allowed_codes:
            continue
        indicator = item.get("indicator")
        if not isinstance(indicator, dict) or indicator.get("id") != plan.indicator_code:
            continue
        try:
            year = int(str(item.get("date") or ""))
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if year < 1960 or not math.isfinite(value):
            continue
        country = item.get("country")
        country_name = country.get("value") if isinstance(country, dict) else ""
        points.append(
            WorldBankDataPoint(
                country_code=country_code,
                country_name=str(country_name or country_code),
                year=year,
                value=value,
            )
        )
    return tuple(points)


def _select_comparison_points(
    points: tuple[WorldBankDataPoint, ...],
    *,
    country_codes: tuple[str, ...],
) -> tuple[WorldBankDataPoint, ...]:
    """只选择所有国家都有值的最新共同年度，维持比较口径一致。"""

    by_year: dict[int, dict[str, WorldBankDataPoint]] = {}
    for point in points:
        by_year.setdefault(point.year, {})[point.country_code] = point
    for year in sorted(by_year, reverse=True):
        values = by_year[year]
        if all(code in values for code in country_codes):
            return tuple(values[code] for code in country_codes)
    return ()


def _select_trend_points(
    points: tuple[WorldBankDataPoint, ...],
    *,
    country_code: str,
) -> tuple[WorldBankDataPoint, ...]:
    selected = sorted((point for point in points if point.country_code == country_code), key=lambda point: point.year)
    return tuple(selected[-_MAX_TREND_POINTS:])
