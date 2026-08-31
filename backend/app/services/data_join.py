"""R5.4C 两份数据的受控关联与新副本交付。

本模块只处理记录型 CSV/XLSX，刻意不提供 SQL、任意 pandas 表达式或多表自由编排。
Commander 先从两份数据画像中编译连接意图，服务层再重新读取当前受控副本、校验源哈希、
拒绝重复键并完成内存预览。确认后才写出一个全新的同类型文件，并重新读取检查列和行数。
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.schemas.data_agent import (
    DataJoinArtifact,
    DataJoinExportRequest,
    DataJoinExportResponse,
    DataJoinIntent,
    DataJoinOperationInput,
    DataJoinPlan,
    DataJoinPreviewRequest,
    DataJoinPreviewResponse,
    DataJoinVerification,
)
from app.services.data_workspace import (
    DataWorkspaceError,
    MAX_DATASET_COLUMNS,
    MAX_DATASET_ROWS,
    get_data_dataset_profile,
    load_data_dataset_for_analysis,
)

try:
    import pandas as pd
except ImportError:  # pragma: no cover - 由健康检查向客户报告依赖缺失。
    pd = None


class DataJoinError(ValueError):
    """连接计划、计算或副本验证无法安全完成时抛出。"""


@dataclass(frozen=True)
class DataJoinComputation:
    """预览与正式导出共享的一次确定性连接结果。"""

    preview: DataJoinPreviewResponse
    frame: Any


def build_data_join_intent(dataset_names: list[str], goal: str) -> DataJoinIntent:
    """根据两份当前画像生成受限连接意图。

    只有一个共同字段，或用户在目标中明确点出唯一字段对时才自动采用；存在多个可能键时
    必须澄清，避免把同名的日期、名称或编号误当成业务关联键。
    """

    if len(dataset_names) != 2 or dataset_names[0] == dataset_names[1]:
        raise DataJoinError("多数据集交付首版需要选择两份不同的数据文件。")
    left_name, right_name = (item.strip() for item in dataset_names)
    try:
        left_profile = get_data_dataset_profile(left_name)
        right_profile = get_data_dataset_profile(right_name)
    except DataWorkspaceError as exc:
        raise DataJoinError(str(exc)) from exc

    left_columns = [item.name for item in left_profile.columns]
    right_columns = [item.name for item in right_profile.columns]
    pairs = _candidate_key_pairs(left_columns, right_columns, goal)
    if not pairs:
        raise DataJoinError("两份数据没有明确的共同关联键，请在问题中写出‘左表字段=右表字段’。")
    if len(pairs) > 1:
        labels = "、".join(f"{left}={right}" for left, right in pairs[:6])
        raise DataJoinError(f"检测到多个候选关联键（{labels}），请明确指定要按哪一列关联。")

    left_key, right_key = pairs[0]
    left_type = _column_type(left_profile, left_key)
    right_type = _column_type(right_profile, right_key)
    if not _types_compatible(left_type, right_type):
        raise DataJoinError(f"关联键类型不兼容：{left_key} 为 {left_type}，{right_key} 为 {right_type}。")

    join_type = _join_type_from_goal(goal)
    renames = _right_column_renames(left_columns, right_columns, right_key, right_name)
    output_columns = [*left_columns, *renames.values()]
    if len(output_columns) > MAX_DATASET_COLUMNS:
        raise DataJoinError("合并后的字段超过 100 列限制，请先减少右侧数据字段。")
    return DataJoinIntent(
        operation=DataJoinOperationInput(
            left_dataset=left_name,
            right_dataset=right_name,
            left_key=left_key,
            right_key=right_key,
            join_type=join_type,
        ),
        source_hashes={left_name: left_profile.source_sha256, right_name: right_profile.source_sha256},
        output_columns=output_columns,
        right_column_renames=renames,
        summary=(
            f"按 {left_name}.{left_key} = {right_name}.{right_key} 执行"
            f"{'左连接' if join_type == 'left' else '内连接'}；重复关联键将停止，不自动扩张行数。"
        ),
    )


def preview_data_join(request: DataJoinPreviewRequest) -> DataJoinPreviewResponse:
    """只在内存中执行连接并返回有限统计和样例。"""

    return _compute_data_join(request).preview


def export_data_join_copy(request: DataJoinExportRequest) -> DataJoinExportResponse:
    """确认后写出新副本，并重新读取验证输出结构与源版本。"""

    computation = _compute_data_join(request)
    left_profile, _left_frame = _load_and_check_source(request.left_dataset, request.source_hashes)
    right_profile, _right_frame = _load_and_check_source(request.right_dataset, request.source_hashes)
    output_dir = settings.data_join_output_dir
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataJoinError("无法创建多数据集合并输出目录，请检查磁盘权限和可用空间。") from exc

    # 交付格式跟随左表，保持用户可预期；画像类型位于 dataset 元数据中。
    extension = ".xlsx" if left_profile.dataset.dataset_type == "xlsx" else ".csv"
    destination = output_dir / _next_output_name(output_dir, left_profile.dataset.name, right_profile.dataset.name, extension)
    temporary = output_dir / f".{destination.stem}.{uuid.uuid4().hex}.partial{extension}"
    try:
        _write_frame(temporary, computation.frame, extension)
        verification = _verify_output(
            temporary,
            extension=extension,
            expected_columns=computation.preview.plan.output_columns,
            expected_rows=computation.preview.output_row_count,
            source_hashes=request.source_hashes,
            left_name=request.left_dataset,
            right_name=request.right_dataset,
        )
        if not verification.passed:
            raise DataJoinError("合并副本回读验证未通过，未生成正式交付文件。")
        os.rename(temporary, destination)
    except DataJoinError:
        raise
    except FileExistsError as exc:
        raise DataJoinError("合并副本命名冲突，请重新确认本次交付。") from exc
    except OSError as exc:
        raise DataJoinError("无法写入多数据集合并副本，请检查输出目录权限和可用空间。") from exc
    except Exception as exc:
        raise DataJoinError("多数据集合并或副本回读失败，未生成正式交付文件。") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    stat = destination.stat()
    created_at = datetime.now(UTC).isoformat()
    artifact = DataJoinArtifact(
        name=destination.name,
        uri=f"agentflow-output://data_joins/{destination.name}",
        size_bytes=stat.st_size,
        created_at=created_at,
    )
    plan = computation.preview.plan
    return DataJoinExportResponse(
        artifact=artifact,
        plan=plan,
        verification=verification,
        output_row_count=computation.preview.output_row_count,
        matched_row_count=computation.preview.matched_row_count,
        left_only_row_count=computation.preview.left_only_row_count,
        right_only_row_count=computation.preview.right_only_row_count,
        warnings=computation.preview.warnings,
    )


def _compute_data_join(request: DataJoinPreviewRequest) -> DataJoinComputation:
    """读取两份当前文件，校验连接合同并在内存中生成结果。"""

    if request.left_dataset == request.right_dataset:
        raise DataJoinError("左右数据文件必须不同，不能把同一份文件关联到自身。")
    left_profile, left_frame = _load_and_check_source(request.left_dataset, request.source_hashes)
    right_profile, right_frame = _load_and_check_source(request.right_dataset, request.source_hashes)
    _ensure_key_exists(left_profile.columns, request.left_key, "左表")
    _ensure_key_exists(right_profile.columns, request.right_key, "右表")
    if not _types_compatible(_column_type(left_profile, request.left_key), _column_type(right_profile, request.right_key)):
        raise DataJoinError("左右关联键类型不兼容，请先统一字段类型后再关联。")

    left_key_values = left_frame[request.left_key].map(_normalise_key)
    right_key_values = right_frame[request.right_key].map(_normalise_key)
    duplicate_left = _duplicate_key_count(left_key_values)
    duplicate_right = _duplicate_key_count(right_key_values)
    if duplicate_left or duplicate_right:
        raise DataJoinError(
            f"发现重复关联键：左表 {duplicate_left} 个、右表 {duplicate_right} 个；"
            "首版为避免结果行数被隐式扩张，暂不自动合并。"
        )

    renames = _right_column_renames(list(left_frame.columns), list(right_frame.columns), request.right_key, request.right_dataset)
    left_work = left_frame.copy(deep=True)
    right_work = right_frame.drop(columns=[request.right_key]).copy(deep=True)
    left_work["__agentflow_join_key"] = left_key_values
    right_work["__agentflow_join_key"] = right_key_values
    right_work = right_work.rename(columns=renames)
    try:
        merged = left_work.merge(
            right_work,
            how=request.join_type,
            on="__agentflow_join_key",
            sort=False,
            validate="one_to_one",
        )
    except Exception as exc:
        raise DataJoinError("关联键无法建立稳定的一对一连接，请检查字段格式和空值。") from exc
    merged = merged.drop(columns=["__agentflow_join_key"])
    if len(merged.index) > MAX_DATASET_ROWS or len(merged.columns) > MAX_DATASET_COLUMNS:
        raise DataJoinError("合并结果超过 100,000 行或 100 列限制，请缩小数据范围。")

    left_key_set = {value for value in left_key_values if value}
    right_key_set = {value for value in right_key_values if value}
    matched = sum(1 for value in left_key_values if value and value in right_key_set)
    left_only = len(left_frame.index) - matched if request.join_type == "left" else 0
    right_only = sum(1 for value in right_key_values if value and value not in left_key_set)
    output_columns = list(merged.columns)
    preview_rows = [
        [_display_value(value) for value in row]
        for row in merged.head(8).itertuples(index=False, name=None)
    ]
    plan = DataJoinPlan(
        left_dataset=request.left_dataset,
        right_dataset=request.right_dataset,
        left_key=request.left_key,
        right_key=request.right_key,
        join_type=request.join_type,
        output_columns=output_columns,
        right_column_renames=renames,
        summary=(
            f"按 {request.left_dataset}.{request.left_key} = {request.right_dataset}.{request.right_key} 执行"
            f"{'左连接' if request.join_type == 'left' else '内连接'}；重复关联键将停止，不自动扩张行数。"
        ),
    )
    preview = DataJoinPreviewResponse(
        plan=plan,
        left_row_count=len(left_frame.index),
        right_row_count=len(right_frame.index),
        output_row_count=len(merged.index),
        matched_row_count=matched,
        left_only_row_count=left_only,
        right_only_row_count=right_only,
        duplicate_left_key_count=duplicate_left,
        duplicate_right_key_count=duplicate_right,
        preview_rows=preview_rows,
        warnings=(
            [f"右表有 {right_only} 个关联键未在左表中匹配。"] if right_only else []
        ),
    )
    return DataJoinComputation(preview=preview, frame=merged)


def _load_and_check_source(dataset_name: str, source_hashes: dict[str, str]) -> tuple[Any, Any]:
    """重新读取并锁定源版本，拒绝用旧预览写入新版本。"""

    try:
        profile, frame = load_data_dataset_for_analysis(dataset_name)
    except DataWorkspaceError as exc:
        raise DataJoinError(str(exc)) from exc
    expected = str(source_hashes.get(dataset_name, "")).strip().casefold()
    if not expected or expected != profile.source_sha256.casefold():
        raise DataJoinError(f"数据文件“{dataset_name}”在预览后发生变化，请重新生成合并预览。")
    return profile, frame


def _candidate_key_pairs(left_columns: list[str], right_columns: list[str], goal: str) -> list[tuple[str, str]]:
    shared = [(left, right) for left in left_columns for right in right_columns if left.casefold() == right.casefold()]
    if len(shared) == 1:
        return shared
    mentioned_shared = [pair for pair in shared if _goal_mentions_column(goal, pair[0])]
    if len(mentioned_shared) == 1:
        return mentioned_shared
    if len(shared) > 1:
        return mentioned_shared
    explicit_pairs: list[tuple[str, str]] = []
    for left in left_columns:
        for right in right_columns:
            if left.casefold() == right.casefold():
                continue
            if _goal_mentions_column(goal, left) and _goal_mentions_column(goal, right):
                explicit_pairs.append((left, right))
    return explicit_pairs


def _goal_mentions_column(goal: str, column: str) -> bool:
    normalized_goal = goal.casefold()
    normalized_column = column.strip().casefold()
    return bool(normalized_column and normalized_column in normalized_goal)


def _join_type_from_goal(goal: str) -> str:
    lowered = goal.casefold()
    if any(marker in lowered for marker in ("内连接", "内关联", "inner", "仅保留匹配")):
        return "inner"
    return "left"


def _column_type(profile: Any, column: str) -> str:
    item = next((candidate for candidate in profile.columns if candidate.name == column), None)
    return str(item.inferred_type) if item is not None else "unknown"


def _types_compatible(left_type: str, right_type: str) -> bool:
    if left_type == right_type or "mixed" in {left_type, right_type}:
        return True
    return {left_type, right_type} <= {"number", "date"}


def _ensure_key_exists(columns: Any, key: str, label: str) -> None:
    if not any(item.name == key for item in columns):
        raise DataJoinError(f"{label}不存在关联字段“{key}”，请重新生成计划。")


def _duplicate_key_count(values: Any) -> int:
    non_empty = values[values.map(bool)]
    return int(non_empty.duplicated().sum())


def _normalise_key(value: Any) -> str:
    if pd is not None and pd.isna(value):
        return ""
    return " ".join(str(value).strip().split()).casefold()


def _right_column_renames(left_columns: list[str], right_columns: list[str], right_key: str, right_dataset: str) -> dict[str, str]:
    """为右表同名字段生成稳定列名；右侧关联键不重复写入结果。"""

    left_set = set(left_columns)
    used = set(left_columns)
    stem = _safe_stem(Path(right_dataset).stem) or "右表"
    mapping: dict[str, str] = {}
    for column in right_columns:
        if column == right_key:
            continue
        candidate = column if column not in left_set else f"{stem}__{column}"
        suffix = 2
        while candidate in used:
            candidate = f"{stem}__{column}__{suffix}"
            suffix += 1
        mapping[column] = candidate
        used.add(candidate)
    return mapping


def _write_frame(path: Path, frame: Any, extension: str) -> None:
    if extension == ".csv":
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        return
    frame.to_excel(path, index=False, sheet_name="数据合并副本", engine="openpyxl")


def _verify_output(
    path: Path,
    *,
    extension: str,
    expected_columns: list[str],
    expected_rows: int,
    source_hashes: dict[str, str],
    left_name: str,
    right_name: str,
) -> DataJoinVerification:
    try:
        if extension == ".csv":
            frame = pd.read_csv(path, encoding="utf-8-sig", dtype=object)
            dataset_type = "csv"
            sheets = ["CSV 数据"]
        else:
            frame = pd.read_excel(path, sheet_name="数据合并副本", dtype=object, engine="openpyxl")
            dataset_type = "xlsx"
            sheets = ["数据合并副本"]
    except Exception as exc:
        raise DataJoinError("合并副本无法重新读取，未登记交付物。") from exc
    left_profile = get_data_dataset_profile(left_name)
    right_profile = get_data_dataset_profile(right_name)
    hashes_unchanged = (
        source_hashes.get(left_name, "").casefold() == left_profile.source_sha256.casefold()
        and source_hashes.get(right_name, "").casefold() == right_profile.source_sha256.casefold()
    )
    passed = list(frame.columns) == expected_columns and len(frame.index) == expected_rows and hashes_unchanged
    return DataJoinVerification(
        passed=passed,
        dataset_type=dataset_type,
        row_count=len(frame.index),
        column_count=len(frame.columns),
        output_columns=[str(item) for item in frame.columns],
        source_hashes_unchanged=hashes_unchanged,
        warnings=[] if passed else ["输出列、行数或源文件版本与预览不一致。"],
    )


def _next_output_name(directory: Path, left_name: str, right_name: str, extension: str) -> str:
    left_stem = _safe_stem(Path(left_name).stem) or "左表"
    right_stem = _safe_stem(Path(right_name).stem) or "右表"
    base = f"合并副本_{left_stem}_{right_stem}"
    candidate = f"{base}{extension}"
    index = 2
    while (directory / candidate).exists():
        candidate = f"{base}_{index}{extension}"
        index += 1
    return candidate


def _safe_stem(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", value).strip("._ ")[:60]


def _display_value(value: Any) -> str:
    if pd is not None and pd.isna(value):
        return ""
    return str(value)
