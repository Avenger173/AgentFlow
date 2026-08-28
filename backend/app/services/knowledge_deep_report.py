"""知识库 K4 正式 Markdown 报告交付服务。

深度任务的 Map/Reduce 已经把模型工作收束为可恢复 checkpoint。本模块只把一条**完整且已验证**
的任务快照渲染为新 Markdown 文件：不重新检索、不读取父块正文、不调用模型，也不接受客户
传入输出路径。这样“确认导出”是一个可审计的文件交付动作，而不是又一次不透明的 Agent 运行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import re
from uuid import uuid4

from app.core.config import settings
from app.database.task_repository import append_workflow_artifact
from app.schemas.knowledge import (
    KnowledgeDeepReduceConflict,
    KnowledgeDeepReduceFinding,
    KnowledgeDeepTaskMapUnit,
    KnowledgeDeepTaskReportExportRequest,
    KnowledgeDeepTaskReportExportResponse,
    KnowledgeDeepTaskResultResponse,
)
from app.schemas.workflow import WorkflowArtifact
from app.services.knowledge_deep_task import get_knowledge_deep_task_result


_KNOWLEDGE_AGENT_ID = "knowledge_agent"
_MARKDOWN_MIME_TYPE = "text/markdown; charset=utf-8"
_REPORT_TITLES = {
    "summary": "知识库深度摘要报告",
    "comparison": "知识库资料对照表",
    "audit": "知识库深度审查报告",
}


class KnowledgeDeepTaskReportExportError(ValueError):
    """正式深度报告无法安全交付时返回的客户可解释错误。"""


class KnowledgeDeepTaskReportNotFoundError(KnowledgeDeepTaskReportExportError):
    """任务或其冻结 scope 已无法从统一历史恢复。"""


class KnowledgeDeepTaskReportConfirmationError(KnowledgeDeepTaskReportExportError):
    """客户没有明确确认导出时拒绝写入文件。"""


class KnowledgeDeepTaskReportNotReadyError(KnowledgeDeepTaskReportExportError):
    """部分结果、损坏 checkpoint 或未完成 Reduce 不得生成正式报告。"""


class KnowledgeDeepTaskReportConflictError(KnowledgeDeepTaskReportExportError):
    """目标名称冲突或输出目录边界异常。"""


def export_knowledge_deep_task_report(
    *,
    task_id: str,
    request: KnowledgeDeepTaskReportExportRequest,
) -> KnowledgeDeepTaskReportExportResponse:
    """确认后创建一份只基于冻结任务快照的 Markdown 正式报告。

    该服务始终新建文件，绝不覆盖已有报告。写入后回读文件，再追加 artifact；任何一步失败都
    仅撤回本次 ``x`` 模式创建的文件，避免在历史里留下不可定位的“幽灵交付物”。
    """

    if not request.confirmed:
        raise KnowledgeDeepTaskReportConfirmationError("导出知识库深度报告前需要客户确认。")

    result = get_knowledge_deep_task_result(task_id)
    if result is None or result.scope is None:
        raise KnowledgeDeepTaskReportNotFoundError("未找到可恢复的知识库深度任务。")
    if result.result is None or result.report_readiness is None or not result.report_readiness.can_export:
        raise KnowledgeDeepTaskReportNotReadyError("当前任务范围尚未完整验证，不能导出正式深度报告。")

    filename = _safe_report_filename(
        request.filename,
        fallback_title=_REPORT_TITLES[result.scope.task_kind],
        task_id=task_id,
    )
    output_root = settings.knowledge_report_output_dir
    output_root.mkdir(parents=True, exist_ok=True)
    target = (output_root / filename).resolve()
    try:
        target.relative_to(output_root)
    except ValueError as exc:
        raise KnowledgeDeepTaskReportConflictError("报告文件名未通过受控输出目录校验。") from exc

    markdown = _render_knowledge_deep_report(result)
    try:
        # 文件系统的 x 模式是最终的不覆盖保证；不能只依赖应用层的 exists 判断。
        with target.open("x", encoding="utf-8", newline="\n") as file:
            file.write(markdown)
    except FileExistsError as exc:
        raise KnowledgeDeepTaskReportConflictError(
            f"output/knowledge_reports 中已存在同名文件“{filename}”，请改名后再次确认导出。"
        ) from exc
    except OSError as exc:
        raise KnowledgeDeepTaskReportExportError("无法写入知识库深度报告。") from exc

    try:
        _verify_rendered_report(target, expected_markdown=markdown, task_id=task_id)
    except Exception as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, KnowledgeDeepTaskReportExportError):
            raise
        raise KnowledgeDeepTaskReportExportError("知识库深度报告回读验证失败，已撤回本次文件。") from exc

    artifact_id = f"{task_id}:knowledge_deep_report:{uuid4().hex[:10]}"
    relative_path = f"output/knowledge_reports/{filename}"
    artifact = WorkflowArtifact(
        artifact_id=artifact_id,
        task_id=task_id,
        step_id="knowledge_deep_report_export",
        agent_id=_KNOWLEDGE_AGENT_ID,
        kind="markdown",
        name=filename,
        summary=(
            f"客户确认导出的知识库深度报告，覆盖 {result.coverage.total_map_count if result.coverage else 0} 个章节，"
            f"基于冻结索引 generation {result.scope.active_index_generation}。"
        ),
        uri=f"agentflow-output://knowledge_reports/{filename}",
        mime_type=_MARKDOWN_MIME_TYPE,
        metadata={
            "runtime": True,
            "output_scope": "knowledge_reports",
            "output_path": str(target),
            "relative_output_path": relative_path,
            "confirmed_by": "local_user",
            "knowledge_base_id": result.scope.knowledge_base_id,
            "index_generation_id": result.scope.index_generation_id,
            "active_index_generation": result.scope.active_index_generation,
            "task_kind": result.scope.task_kind,
            "map_unit_count": result.coverage.total_map_count if result.coverage else 0,
            "report_verification": "utf8_roundtrip",
        },
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    try:
        append_workflow_artifact(
            artifact=artifact,
            event_name="artifact_saved",
            message=f"客户已确认导出知识库深度 Markdown 报告：{relative_path}",
        )
    except Exception as exc:
        # artifact 是客户在历史页重新打开报告的唯一入口。审计失败时撤回本次创建文件，而不是
        # 留下客户看得见却无法追踪的副本。
        target.unlink(missing_ok=True)
        raise KnowledgeDeepTaskReportExportError("知识库深度报告审计失败，已撤回本次新建文件。") from exc

    return KnowledgeDeepTaskReportExportResponse(
        task_id=task_id,
        artifact_id=artifact_id,
        filename=filename,
        relative_path=relative_path,
        artifact_uri=artifact.uri,
        character_count=len(markdown),
        message="知识库深度 Markdown 报告已导出，可在任务历史中预览或打开。",
    )


def _safe_report_filename(raw_filename: str, *, fallback_title: str, task_id: str) -> str:
    """把客户命名收束为一个 Markdown 文件名，不允许目录、扩展名欺骗或 Windows 非法字符。"""

    candidate = raw_filename.strip()
    if not candidate:
        candidate = f"{fallback_title}-{task_id[-6:]}.md"
    if "/" in candidate or "\\" in candidate or Path(candidate).name != candidate:
        raise KnowledgeDeepTaskReportExportError("报告名称只能是文件名，不能包含目录或路径分隔符。")
    if not candidate.lower().endswith(".md"):
        raise KnowledgeDeepTaskReportExportError("报告名称必须以 .md 结尾。")

    stem = candidate[:-3].strip()
    sanitized_stem = re.sub(r'[<>:"|?*\x00-\x1f]+', "-", stem)
    sanitized_stem = re.sub(r"\s+", " ", sanitized_stem).strip(" .-")
    if not sanitized_stem:
        sanitized_stem = f"知识库深度报告-{task_id[-6:]}"
    return f"{sanitized_stem[:96]}.md"


def _render_knowledge_deep_report(result: KnowledgeDeepTaskResultResponse) -> str:
    """从已验证 checkpoint 生成可阅读报告，不接触原始父块内容。"""

    if result.scope is None or result.result is None or result.coverage is None:
        raise KnowledgeDeepTaskReportExportError("当前任务缺少完整的报告快照。")

    scope = result.scope
    reduce_result = result.result
    source_labels = {unit.map_unit_id: _map_unit_source_label(unit) for unit in scope.map_units}
    title = _REPORT_TITLES[scope.task_kind]
    lines = [
        f"# {title}",
        "",
        "## 任务说明",
        "",
        f"- 任务 ID：`{result.task_id}`",
        f"- 任务目标：{scope.task_goal}",
        f"- 资料库：`{scope.knowledge_base_id}`",
        f"- 冻结索引版本：generation {scope.active_index_generation}（`{scope.index_generation_id}`）",
        f"- 覆盖资料：{scope.covered_document_count} 份；覆盖章节：{result.coverage.total_map_count} 个",
        "",
    ]
    if scope.scope_mode == "goal_focused":
        lines.extend(
            (
                "## 覆盖边界",
                "",
                scope.scope_notice or "本次只分析资料库内与目标相关的冻结章节，不代表整库穷举审计。",
                "",
            )
        )
    lines.extend(
        [
            "## 总体结论",
            "",
            reduce_result.overview.strip(),
            "",
            "## 主要发现",
            "",
        ]
    )
    if scope.task_kind == "comparison":
        lines.extend(("", "## 资料对照表", ""))
        _append_comparison_table(lines, result)
    _append_findings(lines, reduce_result.findings, source_labels)
    lines.extend(("", "## 保留的差异与待确认项", ""))
    _append_conflicts(lines, reduce_result.conflicts, source_labels)

    if reduce_result.warnings:
        lines.extend(("", "## 注意事项", ""))
        lines.extend(f"- {warning}" for warning in reduce_result.warnings)

    lines.extend(("", "## 来源范围", ""))
    for unit in scope.map_units:
        lines.append(f"- `{unit.map_unit_id}`：{source_labels[unit.map_unit_id]}")

    lines.extend(
        (
            "",
            "---",
            "",
            "本报告基于导出时指定的冻结索引快照和已验证 Map/Reduce 检查点生成。"
            "之后资料库即使更新，本报告仍代表该次任务的历史范围；需要反映新材料时，应重新发起深度任务。",
            f"<!-- AgentFlow 知识库深度报告 · 任务 {result.task_id} · generation {scope.active_index_generation} -->",
            "",
        )
    )
    return "\n".join(lines)


def _append_findings(
    lines: list[str],
    findings: list[KnowledgeDeepReduceFinding],
    source_labels: dict[str, str],
) -> None:
    """按稳定来源映射输出发现；模型输出中不应出现 scope 外 ID，仍保留防御性回退。"""

    if not findings:
        lines.append("- 本次没有可写入正式报告的聚合发现。")
        return
    for index, finding in enumerate(findings, start=1):
        lines.extend((f"### 发现 {index}", "", finding.statement.strip(), ""))
        labels = "；".join(_source_label(source_id, source_labels) for source_id in finding.source_ids)
        lines.extend((f"> 来源范围：{labels}", ""))


def _append_comparison_table(lines: list[str], result: KnowledgeDeepTaskResultResponse) -> None:
    """以冻结的客户选择顺序渲染 Markdown 表格，而不是把跨资料对照藏进普通段落。"""

    assert result.scope is not None and result.result is not None
    names_by_document_id: dict[str, str] = {}
    for unit in result.scope.map_units:
        names_by_document_id.setdefault(unit.document_id, unit.document_name)
    headers = ["对照维度"] + [
        names_by_document_id.get(document_id, "已选资料")
        for document_id in result.scope.selected_document_ids
    ] + ["结论"]
    lines.append("| " + " | ".join(_markdown_table_cell(item) for item in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    if not result.result.comparison_rows:
        lines.append("| 当前未形成可对照行 | - | - |")
        return
    for row in result.result.comparison_rows:
        values = list(row.values)
        while len(values) < len(result.scope.selected_document_ids):
            values.append("当前汇总未明确说明")
        cells = [row.dimension, *values[: len(result.scope.selected_document_ids)], row.conclusion or "-"]
        lines.append("| " + " | ".join(_markdown_table_cell(item) for item in cells) + " |")


def _markdown_table_cell(value: str) -> str:
    """限制模型文本在 Markdown 表格中的结构影响，不修改其已验证语义。"""

    return " ".join(value.replace("|", "\\|").splitlines()).strip() or "-"


def _append_conflicts(
    lines: list[str],
    conflicts: list[KnowledgeDeepReduceConflict],
    source_labels: dict[str, str],
) -> None:
    """冲突必须保留而非裁决；没有冲突时给出明确而克制的说明。"""

    if not conflicts:
        lines.append("- 当前已完成范围内未记录需要保留的跨章节差异。")
        return
    for index, conflict in enumerate(conflicts, start=1):
        lines.extend((f"### 待确认项 {index}：{conflict.topic.strip()}", "", conflict.description.strip(), ""))
        labels = "；".join(_source_label(source_id, source_labels) for source_id in conflict.source_ids)
        lines.extend((f"> 对照范围：{labels}", ""))


def _map_unit_source_label(unit: KnowledgeDeepTaskMapUnit) -> str:
    """将 scope 中的稳定来源元数据转为报告可读标签，不暴露本机路径或父块正文。"""

    heading = " > ".join(part.strip() for part in unit.heading_path if part.strip())
    location = unit.source.source_locator.replace("\n", " ").strip()
    parts = [unit.document_name]
    if heading:
        parts.append(heading)
    if location:
        parts.append(location)
    else:
        parts.append(f"章节 {unit.parent_ordinal}")
    return " · ".join(parts)


def _source_label(source_id: str, source_labels: dict[str, str]) -> str:
    """未知来源仅作为防御性标记，不允许其伪装成已定位章节。"""

    return source_labels.get(source_id, f"`{source_id}`（未能映射到冻结章节）")


def _verify_rendered_report(target: Path, *, expected_markdown: str, task_id: str) -> None:
    """对 UTF-8 新文件做最小回读验证，确保 artifact 不会指向空文件或错误任务。"""

    try:
        rendered = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KnowledgeDeepTaskReportExportError("知识库深度报告无法按 UTF-8 回读。") from exc
    required_markers = ("# ", "## 总体结论", f"任务 ID：`{task_id}`")
    if rendered != expected_markdown or any(marker not in rendered for marker in required_markers):
        raise KnowledgeDeepTaskReportExportError("知识库深度报告回读内容与冻结任务不一致。")
