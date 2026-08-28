"""D5.2 的受控数据图表 PNG 渲染。

本模块故意不接模型、联网或原始行级计划。它只消费 D2 已经白名单校验并实际算出的聚合
DataFrame，再把有限的 ``DataChartContract`` 渲染为独立 PNG。这样图表可复算、可追溯，
也不会因为模型幻觉或二次解析在图片里出现一套不同的数据。
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from textwrap import fill
from typing import Any

from app.core.config import settings
from app.schemas.data_agent import (
    DataAnalysisPreviewRequest,
    DataChartArtifact,
    DataChartExportRequest,
    DataChartExportResponse,
    DataChartVerification,
)
from app.services.data_analysis import DataAnalysisError, compute_data_analysis

try:  # 服务器端固定使用 Agg，绝不要求桌面绘图环境或阻塞 Qt。
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
except ImportError:  # pragma: no cover - requirements 安装后不会进入。
    plt = None


try:
    from PIL import Image
except ImportError:  # pragma: no cover - requirements 安装后不会进入。
    Image = None


class DataChartError(RuntimeError):
    """客户可理解的图表导出错误。"""


_TASK_ID_PATTERN = re.compile(r"^task_data_chart_[a-f0-9]{12}$")
_SAFE_FILE_STEM = re.compile(r"[^a-zA-Z0-9_-]+")
_PALETTE = ("#2563EB", "#0F766E", "#F59E0B", "#8B5CF6", "#E11D48", "#14B8A6", "#64748B", "#84CC16")


def _configure_cjk_font() -> None:
    """优先固定 Windows 中文字体，避免首次绘图扫描与中文缺字警告。

    发行环境可能没有这几个字体文件，因此找不到时仍保留 matplotlib 默认字体；此时导出不
    会失败，只可能由部署包的字体策略决定英文回退。Windows 桌面开发环境通常命中微软雅黑。
    """

    if plt is None:
        return
    for candidate in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ):
        if not candidate.is_file():
            continue
        font_manager.fontManager.addfont(str(candidate))
        family = font_manager.FontProperties(fname=str(candidate)).get_name()
        matplotlib.rcParams["font.family"] = family
        matplotlib.rcParams["axes.unicode_minus"] = False
        return


_configure_cjk_font()


def export_data_chart_pngs(
    request: DataChartExportRequest,
    *,
    task_id: str,
) -> DataChartExportResponse:
    """基于客户已确认版本生成并回读 PNG 图表。

    每个任务写入自己的新目录；任何异常或取消前清理由 Delivery 层处理。这里若有任一图表
    因为没有有限数值而跳过，会保留其它已验证图表，并把原因写入受控摘要。
    """

    if plt is None or Image is None:
        raise DataChartError("图表渲染依赖未安装，请在 backend 目录安装 requirements.txt。")
    _validate_task_id(task_id)
    try:
        computation = compute_data_analysis(
            DataAnalysisPreviewRequest(
                dataset_name=request.dataset_name,
                goal=request.goal,
                cleaning_policy=request.cleaning_policy,
                max_chart_count=request.max_chart_count,
            )
        )
    except DataAnalysisError as exc:
        raise DataChartError(str(exc)) from exc

    preview = computation.preview
    if preview.dataset_profile.source_sha256.lower() != request.source_sha256.lower():
        raise DataChartError("数据源在预览后已变化，请重新生成分析预览再保存图表。")
    if not preview.charts:
        raise DataChartError("当前分析没有可绘制图表，请先选择包含趋势、分组或构成的分析目标。")

    output_dir = _task_output_dir(task_id)
    if output_dir.exists():
        # task_id 由服务端一次性生成；重复目录说明状态或文件系统异常，不能复用或覆盖。
        raise DataChartError("图表任务输出目录已存在，已拒绝覆盖旧交付物。")
    output_dir.mkdir(parents=True, exist_ok=False)

    artifacts: list[DataChartArtifact] = []
    skipped_items = list(preview.skipped_items)
    warnings = list(preview.warnings)
    try:
        for index, chart in enumerate(preview.charts, start=1):
            table_frame = computation.table_frames.get(chart.table_id)
            if table_frame is None:
                skipped_items.append(f"{chart.title}：缺少已验证的聚合表，未生成图表。")
                continue
            try:
                artifact = _render_chart(
                    task_id=task_id,
                    index=index,
                    chart=chart,
                    table_frame=table_frame,
                    output_dir=output_dir,
                )
                artifacts.append(artifact)
            except DataChartError as exc:
                skipped_items.append(f"{chart.title}：{exc}")

        if not artifacts:
            raise DataChartError("没有形成可交付的图表 PNG；请调整分析目标或检查有效数值字段。")
        if skipped_items:
            warnings.append("部分图表未形成有限数值数据，已跳过；其余图表仍可使用。")
        verification = DataChartVerification(
            passed=True,
            chart_count=len(artifacts),
            chart_ids=[artifact.chart_id for artifact in artifacts],
            image_sizes=[f"{artifact.width}x{artifact.height}" for artifact in artifacts],
            warnings=warnings[:12],
        )
        return DataChartExportResponse(
            artifacts=artifacts,
            verification=verification,
            warnings=warnings[:12],
            skipped_items=skipped_items[:12],
        )
    except Exception:
        # 尚未登记 artifact 的目录不应在失败时留给用户误认为正式交付。
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def resolve_data_chart_artifact_path(*, task_id: str, filename: str) -> Path:
    """由服务端 task 与文件名反查 PNG，禁止把 URI 当作系统路径。"""

    _validate_task_id(task_id)
    if filename != Path(filename).name or not filename.lower().endswith(".png"):
        raise DataChartError("图表产物文件名无效。")
    output_dir = _task_output_dir(task_id)
    output_path = (output_dir / filename).resolve()
    try:
        output_path.relative_to(output_dir)
    except ValueError as exc:  # pragma: no cover - filename 已检查，保留目录边界。
        raise DataChartError("图表产物超出受控输出目录。") from exc
    if not output_path.is_file():
        raise DataChartError("图表产物不存在或已被清理。")
    return output_path


def remove_data_chart_output(task_id: str) -> None:
    """仅清理尚未登记 artifact 的当前任务目录，供协作式取消使用。"""

    try:
        shutil.rmtree(_task_output_dir(task_id), ignore_errors=True)
    except DataChartError:
        return


def _render_chart(*, task_id: str, index: int, chart: Any, table_frame: Any, output_dir: Path) -> DataChartArtifact:
    categories, values = _chart_series(table_frame, chart.category_column, chart.value_column)
    if len(categories) < 2:
        raise DataChartError("有效数据点不足 2 个。")
    if chart.chart_type in {"pie", "doughnut"} and sum(values) <= 0:
        raise DataChartError("构成图需要正数总量，当前数据不适用。")

    figure, axis = plt.subplots(figsize=(10.4, 6.1), dpi=140)
    figure.patch.set_facecolor("#F8FAFC")
    axis.set_facecolor("#FFFFFF")
    labels = [_display_label(item) for item in categories]
    colors = [_PALETTE[item % len(_PALETTE)] for item in range(len(values))]
    try:
        if chart.chart_type == "line":
            axis.plot(range(len(values)), values, color=_PALETTE[0], linewidth=2.8, marker="o", markersize=6)
            axis.fill_between(range(len(values)), values, color=_PALETTE[0], alpha=0.12)
            axis.set_xticks(range(len(labels)), labels, rotation=25, ha="right")
            axis.grid(axis="y", color="#E2E8F0", linewidth=0.8)
        elif chart.chart_type == "bar":
            bars = axis.bar(labels, values, color=colors, width=0.66)
            axis.tick_params(axis="x", rotation=25)
            axis.grid(axis="y", color="#E2E8F0", linewidth=0.8)
            _annotate_bars(axis, bars, values)
        else:
            wedge_width = 0.38 if chart.chart_type == "doughnut" else 1.0
            axis.pie(
                values,
                labels=labels,
                autopct="%1.1f%%",
                startangle=90,
                counterclock=False,
                colors=colors,
                wedgeprops={"width": wedge_width, "edgecolor": "#FFFFFF", "linewidth": 1.3},
                textprops={"fontsize": 9, "color": "#172554"},
            )
            axis.axis("equal")
        axis.set_title(fill(str(chart.title), width=34), loc="left", fontsize=16, fontweight="bold", color="#0F172A", pad=18)
        if chart.chart_type in {"bar", "line"}:
            axis.set_ylabel(_display_label(chart.value_column), color="#475569")
            axis.spines[["top", "right"]].set_visible(False)
            axis.spines[["left", "bottom"]].set_color("#CBD5E1")
            axis.tick_params(colors="#475569")
        figure.text(
            0.125,
            0.02,
            f"基于本地聚合：{_display_label(chart.category_column)} / {_display_label(chart.value_column)}",
            fontsize=8.5,
            color="#64748B",
        )
        figure.tight_layout(rect=(0, 0.055, 1, 1))

        stem = _SAFE_FILE_STEM.sub("_", chart.chart_id).strip("_") or f"chart_{index}"
        filename = f"{index:02d}_{stem}.png"
        output_path = output_dir / filename
        temporary_path = output_dir / f".{filename}.tmp"
        figure.savefig(temporary_path, format="png", facecolor=figure.get_facecolor())
        width, height = _verify_png(temporary_path)
        os.replace(temporary_path, output_path)
    finally:
        plt.close(figure)

    created_at = _now()
    return DataChartArtifact(
        artifact_id=f"artifact_chart_{task_id.rsplit('_', maxsplit=1)[-1]}_{index}",
        chart_id=chart.chart_id,
        chart_type=chart.chart_type,
        title=chart.title,
        name=filename,
        uri=f"agentflow-output://data_charts/{task_id}/{filename}",
        size_bytes=output_path.stat().st_size,
        width=width,
        height=height,
        created_at=created_at,
    )


def _chart_series(table_frame: Any, category_column: str, value_column: str) -> tuple[list[str], list[float]]:
    """只从 D2 形成的有限聚合表提取绘图数据，不回到原始 DataFrame。"""

    if category_column not in table_frame.columns or value_column not in table_frame.columns:
        raise DataChartError("聚合表字段不完整。")
    categories: list[str] = []
    values: list[float] = []
    for category, raw_value in zip(table_frame[category_column].tolist(), table_frame[value_column].tolist(), strict=True):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if value != value or value in {float("inf"), float("-inf")}:
            continue
        label = str(category).strip()
        if not label:
            continue
        categories.append(label)
        values.append(value)
        if len(categories) >= 50:
            break
    return categories, values


def _annotate_bars(axis: Any, bars: Any, values: list[float]) -> None:
    for bar, value in zip(bars, values, strict=True):
        axis.annotate(
            _format_value(value),
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 4),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#334155",
        )


def _verify_png(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            if image.format != "PNG" or width < 800 or height < 450:
                raise DataChartError("PNG 回读尺寸或格式不符合交付要求。")
    except (OSError, ValueError) as exc:
        raise DataChartError("PNG 回读验证失败。") from exc
    if path.stat().st_size < 2_048:
        raise DataChartError("PNG 文件异常小，未登记为交付物。")
    return width, height


def _task_output_dir(task_id: str) -> Path:
    return (settings.data_chart_output_dir / task_id).resolve()


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise DataChartError("图表任务标识无效。")


def _display_label(value: object) -> str:
    text = str(value).strip().replace("\n", " ")
    return text if len(text) <= 28 else f"{text[:27]}…"


def _format_value(value: float) -> str:
    if abs(value) >= 100_000:
        return f"{value:,.0f}"
    if abs(value) >= 1_000:
        return f"{value:,.1f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _now() -> str:
    return datetime.now(UTC).isoformat()
