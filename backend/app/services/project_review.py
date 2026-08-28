"""项目文档审查 V1 的确定性质量门与任务审计实现。

这不是一个新的模型 Agent 循环。文档助手已经拥有受控 workspace、来源定位、任务历史和
实时事件；本服务只把这些底座组合为可解释的项目审查工作流。它先提供稳定的规则基线，
后续的模型辅助项也只能作为新增证据来源，不能替代或掩盖规则结果。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Iterable
from uuid import uuid4

from app.database.task_repository import load_workflow_run, save_workflow_run
from app.schemas.document_agent import DocumentSourceRef
from app.schemas.events import TaskLogEvent
from app.schemas.project_review import (
    ProjectDocumentType,
    ProjectReviewCheck,
    ProjectReviewFinding,
    ProjectReviewReport,
    ProjectReviewRequest,
    ProjectReviewRunResponse,
)
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.workspace_documents import WorkspaceDocumentError, read_workspace_document_chunks
from app.workflow.dry_run import clear_dry_run_memory_cache


_PROJECT_REVIEW_AGENT_ID = "document_agent"
_PROJECT_REVIEW_STEP_ID = "project_document_review"
_REVIEW_RULES: tuple[tuple[str, str, str, tuple[str, ...], str, str, str], ...] = (
    (
        "project_review.scope_boundary",
        "scope",
        "范围与边界",
        ("范围", "边界", "不包括", "不包含", "范围外", "out of scope", "scope"),
        "未识别到明确的范围或边界说明。",
        "补充“包含什么 / 不包含什么 / 由谁负责”的范围段落，并让范围与交付物对应。",
        "medium",
    ),
    (
        "project_review.acceptance_criteria",
        "acceptance",
        "验收与完成标准",
        ("验收", "完成标准", "通过标准", "测试标准", "acceptance"),
        "未识别到可验证的验收或完成标准。",
        "为核心交付物写明可观察的通过条件、验证人和验证方式，避免只写“完成后验收”。",
        "high",
    ),
    (
        "project_review.ownership",
        "ownership",
        "责任与协作边界",
        ("负责人", "责任人", "职责", "负责", "owner", "stakeholder"),
        "未识别到明确的责任人、职责或协作角色说明。",
        "为关键交付物和决策点补充负责角色、协作角色及确认责任，避免任务只写“相关人员”。",
        "medium",
    ),
    (
        "project_review.schedule",
        "schedule",
        "节点与计划",
        ("里程碑", "截止", "时间表", "计划", "阶段", "日期", "timeline", "schedule"),
        "未识别到可追踪的时间节点、阶段或计划说明。",
        "补充关键里程碑、目标日期、前置条件和变更时的更新规则。",
        "medium",
    ),
    (
        "project_review.risk_dependency",
        "risk_dependency",
        "风险与依赖",
        ("风险", "依赖", "前置", "阻塞", "假设", "risk", "dependency"),
        "未识别到风险、依赖、前置条件或假设说明。",
        "列出主要风险/依赖、触发信号、责任人和应对方式，并标出会阻塞哪些交付物。",
        "medium",
    ),
    (
        "project_review.terminology",
        "terminology",
        "术语与口径",
        ("术语", "定义", "名词", "缩写", "glossary"),
        "未识别到术语、缩写或关键口径的统一说明。",
        "对项目名、角色名、核心对象和缩写建立简短术语表，减少跨团队理解偏差。",
        "low",
    ),
)


class ProjectReviewServiceError(RuntimeError):
    """项目审查可预期的业务错误，不应被 API 包装为不透明的 500。"""


def evaluate_project_document_material(
    *,
    requested_document_type: ProjectDocumentType,
    chunks: list[dict[str, object]],
) -> ProjectReviewReport:
    """对已读取的受控材料执行项目质量规则，但不创建任务或写入数据库。

    这个纯评估入口让“主动生成项目审查报告”和“制作 PPT 时的自动交付预检”使用完全相同的
    规则基线。调用方决定是否把结果写进历史，避免用户每次预览 PPT 都额外产生一条任务记录。
    """

    if not chunks:
        raise ProjectReviewServiceError("项目审查没有读取到可分析的材料。")

    document_ref = str(chunks[0].get("relative_path") or "")
    if not document_ref:
        raise ProjectReviewServiceError("项目审查材料缺少受控相对路径。")
    document_type = _resolve_document_type(requested_document_type, chunks)
    evidence_lines = _collect_evidence_lines(chunks)
    document_anchor = _document_anchor(document_ref=document_ref, chunks=chunks)

    findings: list[ProjectReviewFinding] = []
    checks: list[ProjectReviewCheck] = []
    for rule_id, category, label, keywords, missing_detail, suggestion, severity in _REVIEW_RULES:
        matched = _find_keyword_evidence(evidence_lines, keywords)
        if matched:
            checks.append(
                ProjectReviewCheck(
                    rule_id=rule_id,
                    category=category,  # type: ignore[arg-type]
                    label=label,
                    status="passed",
                    message=f"识别到与“{label}”相关的材料表述，建议在最终提交前人工确认其具体性。",
                    source_refs=_unique_sources(item.source_ref for item in matched),
                )
            )
            continue
        checks.append(
            ProjectReviewCheck(
                rule_id=rule_id,
                category=category,  # type: ignore[arg-type]
                label=label,
                status="attention",
                message=missing_detail,
                source_refs=[document_anchor],
            )
        )
        findings.append(
            ProjectReviewFinding(
                id=f"finding_{len(findings) + 1:02d}",
                rule_id=rule_id,
                severity=severity,  # type: ignore[arg-type]
                category=category,  # type: ignore[arg-type]
                title=f"{label}需要补充",
                detail=missing_detail,
                suggestion=suggestion,
                evidence="这是基于整份材料的规则检查：未识别到对应章节或明确表述，不代表它在业务上一定不存在。",
                source_refs=[document_anchor],
            )
        )

    requirement_evidence = _find_keyword_evidence(
        evidence_lines,
        ("必须", "不得", "应当", "需", "must", "shall", "required"),
    )
    acceptance_check = next(
        item for item in checks if item.rule_id == "project_review.acceptance_criteria"
    )
    if requirement_evidence and acceptance_check.status == "attention":
        # 强约束缺少验收口径时，直接指向被识别的需求原文，而不是把“缺失”伪装成一条事实。
        findings.append(
            ProjectReviewFinding(
                id=f"finding_{len(findings) + 1:02d}",
                rule_id="project_review.requirement_testability",
                severity="high",
                category="acceptance",
                title="强约束缺少可验证验收口径",
                detail="材料中出现了必须、不得或应当等强约束，但未识别到相应验收标准。",
                suggestion="为每条关键约束补充可验证的结果、判定条件和验收责任人。",
                evidence="已识别到强约束表述；验收项仍需人工补齐。",
                source_refs=_unique_sources(item.source_ref for item in requirement_evidence),
            )
        )

    findings = _sort_findings(findings)
    return ProjectReviewReport(
        document_ref=document_ref,
        document_type=document_type,
        summary=_build_summary(document_ref=document_ref, findings=findings, checks=checks),
        findings=findings,
        checks=checks,
        warnings=[
            "本报告使用可解释的项目质量规则，不判断项目可行性、法律合规或行业认证。",
            "“未识别到”表示当前解析文本中没有匹配到明确表述；请结合原文和实际项目情况复核。",
        ],
    )


def run_project_document_review(
    *,
    request: ProjectReviewRequest,
    task_id: str | None = None,
) -> ProjectReviewRunResponse:
    """读取一份受控项目文档，执行规则审查并写入既有 Workflow 历史。

    规则判断只基于已解析的文本和本地模式；不调用模型、不联网、不写入原文或输出文件，因此
    没有额外权限请求。长文档按 workspace 既有分块读取，既保证全文覆盖，也不重复解析文件。
    """

    started_at = datetime.now(UTC)
    stable_task_id = task_id or f"task_project_review_{uuid4().hex[:12]}"
    try:
        chunks = read_workspace_document_chunks(relative_path=request.document_ref)
    except WorkspaceDocumentError as exc:
        raise ProjectReviewServiceError(str(exc)) from exc
    report = evaluate_project_document_material(
        requested_document_type=request.document_type,
        chunks=chunks,
    )
    response = _persist_project_review(
        task_id=stable_task_id,
        started_at=started_at,
        report=report,
        chunks=chunks,
    )
    clear_dry_run_memory_cache()
    return response


def get_project_document_review_result(task_id: str) -> ProjectReviewRunResponse | None:
    """从历史任务恢复已完成审查，供异步端点和 Qt 轮询共同使用。"""

    run = load_workflow_run(task_id)
    if run is None:
        return None
    step = next((item for item in reversed(run.steps) if item.step_id == _PROJECT_REVIEW_STEP_ID), None)
    if step is None:
        return None
    report_payload = step.output.get("project_review_report")
    if not isinstance(report_payload, dict):
        return None
    try:
        report = ProjectReviewReport.model_validate(report_payload)
    except Exception:
        return None
    return ProjectReviewRunResponse(
        task_id=task_id,
        status="completed" if run.status == "completed" else "failed",
        report=report,
        workflow_run=run,
    )


class _EvidenceLine:
    """规则扫描的最小文本单元，只在本次函数内存在，不进入任务日志。"""

    def __init__(self, text: str, source_ref: DocumentSourceRef) -> None:
        self.text = text
        self.source_ref = source_ref


def _collect_evidence_lines(chunks: list[dict[str, object]]) -> list[_EvidenceLine]:
    """把受控分块转成带定位的扫描行，同时限制审计中不会保存整篇正文。"""

    lines: list[_EvidenceLine] = []
    source_index = 0
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        relative_path = str(chunk.get("relative_path") or "")
        source_kind = str(chunk.get("source_kind") or "line")
        source_locator = str(chunk.get("source_locator") or "")
        start_line = int(chunk.get("start_line") or 1)
        for offset, raw_line in enumerate(text.splitlines() or [text]):
            normalized = " ".join(raw_line.split())
            if not normalized:
                continue
            source_index += 1
            line_number = start_line + offset if source_kind == "line" else start_line
            end_line = line_number
            locator = f"第 {line_number} 行" if source_kind == "line" else source_locator
            lines.append(
                _EvidenceLine(
                    normalized,
                    DocumentSourceRef(
                        source_id=f"project_review_src_{source_index:04d}",
                        relative_path=relative_path,
                        start_line=max(1, line_number),
                        end_line=max(1, end_line),
                        source_kind=source_kind if source_kind in {"line", "page", "paragraph", "table", "mixed"} else "mixed",  # type: ignore[arg-type]
                        source_locator=locator,
                        excerpt=normalized[:360],
                    ),
                )
            )
    return lines


def _document_anchor(*, document_ref: str, chunks: list[dict[str, object]]) -> DocumentSourceRef:
    """为“缺失类”规则提供材料范围锚点，明确它不是虚构的命中行。"""

    first = chunks[0]
    source_kind = str(first.get("source_kind") or "line")
    start_line = max(1, int(first.get("start_line") or 1))
    end_line = max(start_line, int(first.get("end_line") or start_line))
    locator = str(first.get("source_locator") or "")
    if not locator and source_kind == "line":
        locator = f"第 {start_line}-{end_line} 行（审查范围起点）"
    return DocumentSourceRef(
        source_id="project_review_document_scope",
        relative_path=document_ref,
        start_line=start_line,
        end_line=end_line,
        source_kind=source_kind if source_kind in {"line", "page", "paragraph", "table", "mixed"} else "mixed",  # type: ignore[arg-type]
        source_locator=locator or "审查范围起点",
        excerpt="项目文档质量规则覆盖整份已解析材料；本项为缺失提示，不对应单一命中句。",
    )


def _find_keyword_evidence(lines: Iterable[_EvidenceLine], keywords: Iterable[str]) -> list[_EvidenceLine]:
    """做大小写无关的明确关键词定位，最多保留两条，避免报告被重复词淹没。"""

    normalized_keywords = tuple(item.casefold() for item in keywords)
    matched: list[_EvidenceLine] = []
    for line in lines:
        text = line.text.casefold()
        if any(keyword in text for keyword in normalized_keywords):
            matched.append(line)
            if len(matched) == 2:
                break
    return matched


def _unique_sources(sources: Iterable[DocumentSourceRef]) -> list[DocumentSourceRef]:
    """按真实定位去重，而不相信每次派生任务都从 source_001 开始的内部 ID。"""

    result: list[DocumentSourceRef] = []
    seen: set[tuple[str, int, int, str]] = set()
    for source in sources:
        key = (source.relative_path, source.start_line, source.end_line, source.source_locator)
        if key not in seen:
            seen.add(key)
            result.append(source)
        if len(result) == 2:
            break
    return result


def _resolve_document_type(
    requested: ProjectDocumentType,
    chunks: list[dict[str, object]],
) -> ProjectDocumentType:
    if requested != "auto":
        return requested
    preview = " ".join(str(chunk.get("text") or "")[:2_000] for chunk in chunks[:2]).casefold()
    if any(token in preview for token in ("prd", "产品需求", "用户故事", "需求说明")):
        return "prd"
    if any(token in preview for token in ("项目计划", "实施计划", "里程碑", "甘特")):
        return "project_plan"
    return "project_proposal"


def _sort_findings(findings: list[ProjectReviewFinding]) -> list[ProjectReviewFinding]:
    priority = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda item: (priority[item.severity], item.rule_id))


def _build_summary(
    *,
    document_ref: str,
    findings: list[ProjectReviewFinding],
    checks: list[ProjectReviewCheck],
) -> str:
    high_count = sum(item.severity == "high" for item in findings)
    passed_count = sum(item.status == "passed" for item in checks)
    if not findings:
        return (
            f"“{document_ref}”已通过本轮 {len(checks)} 项项目质量规则检查；"
            "仍建议由项目负责人复核每项表述是否足够具体、可执行。"
        )
    return (
        f"“{document_ref}”完成 {len(checks)} 项项目质量规则检查，"
        f"识别 {len(findings)} 项需要关注的问题，其中 {high_count} 项高优先级；"
        f"已有 {passed_count} 项找到明确材料表述。"
    )


def _persist_project_review(
    *,
    task_id: str,
    started_at: datetime,
    report: ProjectReviewReport,
    chunks: list[dict[str, object]],
) -> ProjectReviewRunResponse:
    """把确定性审查写入统一任务历史，保留工具、步骤和结果的可观察性。"""

    finished_at = datetime.now(UTC)
    duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
    read_characters = sum(len(str(chunk.get("text") or "")) for chunk in chunks)
    tool_call = WorkflowToolCall(
        call_id=f"{task_id}:document.read_text:1",
        task_id=task_id,
        step_id="project_review_read",
        agent_id=_PROJECT_REVIEW_AGENT_ID,
        tool_name="document.read_text",
        status="completed",
        risk_level="low",
        permission_required=False,
        timeout_ms=30_000,
        duration_ms=duration_ms,
        request={"document_ref": report.document_ref, "coverage": "full_document"},
        result={"chunk_count": len(chunks), "characters_scanned": read_characters},
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
    )
    steps = [
        WorkflowStepRun(
            step_id="project_review_read",
            agent=_PROJECT_REVIEW_AGENT_ID,
            action="read_project_document",
            status="completed",
            message=f"已读取 {len(chunks)} 个受控文本分块，覆盖选定项目材料。",
            output={"document_ref": report.document_ref, "chunk_count": len(chunks)},
        ),
        WorkflowStepRun(
            step_id=_PROJECT_REVIEW_STEP_ID,
            agent=_PROJECT_REVIEW_AGENT_ID,
            action="review_project_document",
            status="completed",
            message=report.summary,
            output={
                "runtime": True,
                "project_review_report": report.model_dump(mode="json"),
                "review_strategy": report.review_strategy,
            },
        ),
    ]
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="completed",
        summary=report.summary,
        max_risk_level="low",
        requires_confirmation=False,
        steps=steps,
        limits=RuntimeExecutionLimits(
            max_steps=2,
            max_tool_calls=1,
            max_retries_per_tool=0,
            tool_timeout_ms=30_000,
            task_timeout_ms=60_000,
        ),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
            duration_ms=duration_ms,
            step_total=2,
            step_completed=2,
            tool_call_total=1,
            estimated_input_tokens=max(1, read_characters // 4),
        ),
    )
    events = [
        TaskLogEvent(
            task_id=task_id,
            sequence=1,
            event="project_review_started",
            agent_id=_PROJECT_REVIEW_AGENT_ID,
            step_id="project_review_read",
            message="项目文档审查已开始，正在读取受控材料。",
        ),
        TaskLogEvent(
            task_id=task_id,
            sequence=2,
            event="project_review_rules_completed",
            agent_id=_PROJECT_REVIEW_AGENT_ID,
            step_id=_PROJECT_REVIEW_STEP_ID,
            level="warning" if report.findings else "info",
            message=report.summary,
        ),
    ]
    save_workflow_run(run=run, events=events, plan=None, tool_calls=[tool_call])
    return ProjectReviewRunResponse(task_id=task_id, status="completed", report=report, workflow_run=run)
