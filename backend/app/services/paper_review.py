"""论文审查 V1：基于可解释规则的结构、引用与格式检查。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from app.database.task_repository import load_workflow_run, save_workflow_run
from app.schemas.events import TaskLogEvent
from app.schemas.paper_review import (
    PaperReviewCheck,
    PaperReviewFinding,
    PaperReviewReport,
    PaperReviewRequest,
    PaperReviewRunResponse,
)
from app.schemas.workflow import RuntimeExecutionLimits, RuntimeExecutionMetrics, WorkflowRun, WorkflowStepRun, WorkflowToolCall
from app.services.document_review_support import (
    ReviewEvidenceLine,
    collect_evidence_lines,
    document_anchor,
    find_keyword_evidence,
    load_review_chunks,
    unique_sources,
)
from app.workflow.dry_run import clear_dry_run_memory_cache


_PAPER_REVIEW_AGENT_ID = "document_agent"
_PAPER_REVIEW_STEP_ID = "paper_review"
_PAPER_RULES = (
    (
        "paper_review.structure",
        "structure",
        "论文结构",
        ("摘要", "abstract", "引言", "绪论", "方法", "结果", "讨论", "结论"),
        "未识别到足够清晰的论文结构线索。",
        "至少明确摘要、研究背景/引言、方法、结果、讨论或结论等主要部分，并在最终格式中使用稳定标题层级。",
        "medium",
    ),
    (
        "paper_review.references",
        "citation",
        "参考文献区",
        ("参考文献", "references", "bibliography"),
        "未识别到参考文献区或等价文献列表。",
        "补充统一格式的参考文献区，并与正文引用编号或作者年份形式对应。",
        "high",
    ),
    (
        "paper_review.figure_table",
        "figure_table",
        "图表提及",
        # 不能直接搜索单个“图”或“表”，否则“图书”“表述”等普通词会被误判为图表编号。
        ("图1", "图 1", "表1", "表 1", "figure", "table"),
        "未识别到图表编号或图表提及。",
        "若论文使用图表，请为每个图表提供编号、标题，并在正文中解释其作用；若确实没有图表，可忽略本项。",
        "low",
    ),
    (
        "paper_review.heading_format",
        "format",
        "标题层级与格式线索",
        ("# ", "## ", "1.", "1、", "一、", "（一）"),
        "未识别到稳定的标题层级或章节编号线索。",
        "使用一种统一的标题编号体系，并确保同级标题格式、缩进和标点保持一致。",
        "medium",
    ),
)


class PaperReviewServiceError(RuntimeError):
    """论文审查的可预期业务错误。"""


def run_paper_review(*, request: PaperReviewRequest, task_id: str | None = None) -> PaperReviewRunResponse:
    """全文读取一篇受控材料，执行论文规范规则并写入标准任务历史。"""

    started_at = datetime.now(UTC)
    stable_task_id = task_id or f"task_paper_review_{uuid4().hex[:12]}"
    try:
        chunks = load_review_chunks(document_ref=request.document_ref)
    except ValueError as exc:
        raise PaperReviewServiceError(str(exc)) from exc
    document_ref = str(chunks[0].get("relative_path") or request.document_ref)
    lines = collect_evidence_lines(chunks, source_prefix="paper_review_src")
    anchor = document_anchor(document_ref=document_ref, chunks=chunks, source_id="paper_review_document_scope")

    checks: list[PaperReviewCheck] = []
    findings: list[PaperReviewFinding] = []
    for rule_id, category, label, keywords, detail, suggestion, severity in _PAPER_RULES:
        matched = find_keyword_evidence(lines, keywords)
        if matched:
            checks.append(PaperReviewCheck(
                rule_id=rule_id,
                category=category,  # type: ignore[arg-type]
                label=label,
                status="passed",
                message=f"识别到与“{label}”相关的文档线索，仍建议人工检查是否完整且格式统一。",
                source_refs=unique_sources(item.source_ref for item in matched),
            ))
        else:
            checks.append(PaperReviewCheck(
                rule_id=rule_id,
                category=category,  # type: ignore[arg-type]
                label=label,
                status="attention",
                message=detail,
                source_refs=[anchor],
            ))
            findings.append(PaperReviewFinding(
                id=f"finding_{len(findings) + 1:02d}",
                rule_id=rule_id,
                severity=severity,  # type: ignore[arg-type]
                category=category,  # type: ignore[arg-type]
                title=f"{label}需要复核",
                detail=detail,
                suggestion=suggestion,
                evidence="这是对整份解析材料的规则提示，不表示系统对论文内容作出了学术结论。",
                source_refs=[anchor],
            ))

    citation_lines = _find_citations(lines)
    references_check = next(item for item in checks if item.rule_id == "paper_review.references")
    if citation_lines and references_check.status == "attention":
        findings.append(PaperReviewFinding(
            id=f"finding_{len(findings) + 1:02d}",
            rule_id="paper_review.citation_mapping",
            severity="high",
            category="citation",
            title="正文引用缺少可识别的参考文献区",
            detail="正文中出现引用编号或作者年份形式，但没有识别到参考文献区，读者无法核验引文来源。",
            suggestion="补充参考文献区，并逐项核对正文引用编号、作者年份与条目是否一一对应。",
            evidence="已检测到正文引用格式；缺失提示针对文献列表结构。",
            source_refs=unique_sources(item.source_ref for item in citation_lines),
        ))

    long_lines = [line for line in lines if len(line.text) >= 220][:2]
    if long_lines:
        findings.append(PaperReviewFinding(
            id=f"finding_{len(findings) + 1:02d}",
            rule_id="paper_review.long_sentence",
            severity="low",
            category="language",
            title="存在较长段落或句子",
            detail="检测到单行文本较长，阅读时可能难以识别论点、证据和结论的关系。",
            suggestion="拆分为更短的句子或段落；每段优先表达一个论点，并用过渡句连接证据与结论。",
            evidence="该提示只依据文本长度，不对语言质量或学术价值作自动判断。",
            source_refs=unique_sources(item.source_ref for item in long_lines),
        ))

    findings = _sort_findings(findings)
    paper_type = _resolve_paper_type(request.paper_type, chunks)
    summary = _build_summary(document_ref, findings, checks)
    report = PaperReviewReport(
        document_ref=document_ref,
        paper_type=paper_type,  # type: ignore[arg-type]
        summary=summary,
        findings=findings,
        checks=checks,
        warnings=[
            "本审查不做查重，不验证引文真实性，不判断学术创新性，也不提供投稿、学位或法律结论。",
            "图表项只检查文档是否存在明确图表线索；没有图表的研究不必为了通过检查而新增图表。",
        ],
    )
    response = _persist_paper_review(task_id=stable_task_id, report=report, chunks=chunks, started_at=started_at)
    clear_dry_run_memory_cache()
    return response


def get_paper_review_result(task_id: str) -> PaperReviewRunResponse | None:
    """从历史任务恢复经 schema 校验的论文审查报告。"""

    run = load_workflow_run(task_id)
    if run is None:
        return None
    step = next((item for item in reversed(run.steps) if item.step_id == _PAPER_REVIEW_STEP_ID), None)
    if step is None:
        return None
    payload = step.output.get("paper_review_report")
    if not isinstance(payload, dict):
        return None
    try:
        report = PaperReviewReport.model_validate(payload)
    except Exception:
        return None
    return PaperReviewRunResponse(task_id=task_id, status="completed" if run.status == "completed" else "failed", report=report, workflow_run=run)


def _find_citations(lines: list[ReviewEvidenceLine]) -> list[ReviewEvidenceLine]:
    """仅识别常见的引用形式，不把普通数字或括号泛化成论文引用。"""

    matched: list[ReviewEvidenceLine] = []
    pattern = re.compile(r"\[\s*\d+(?:\s*[,，-]\s*\d+)*\s*\]|\([A-Z][A-Za-z .&-]+,?\s*(?:19|20)\d{2}[a-z]?\)")
    for line in lines:
        if pattern.search(line.text):
            matched.append(line)
            if len(matched) == 2:
                break
    return matched


def _resolve_paper_type(requested: str, chunks: list[dict[str, object]]) -> str:
    if requested != "auto":
        return requested
    preview = " ".join(str(chunk.get("text") or "")[:1_500] for chunk in chunks[:2]).casefold()
    if any(token in preview for token in ("学位论文", "硕士", "博士", "thesis")):
        return "thesis"
    if any(token in preview for token in ("研究报告", "technical report")):
        return "report"
    return "article"


def _sort_findings(findings: list[PaperReviewFinding]) -> list[PaperReviewFinding]:
    priority = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda item: (priority[item.severity], item.rule_id))


def _build_summary(document_ref: str, findings: list[PaperReviewFinding], checks: list[PaperReviewCheck]) -> str:
    high_count = sum(item.severity == "high" for item in findings)
    passed_count = sum(item.status == "passed" for item in checks)
    if not findings:
        return f"“{document_ref}”已通过本轮 {len(checks)} 项论文形式规则检查；仍需作者人工复核引用内容与研究结论。"
    return f"“{document_ref}”完成 {len(checks)} 项论文形式规则检查，识别 {len(findings)} 项待复核问题，其中 {high_count} 项高优先级；已有 {passed_count} 项找到文档线索。"


def _persist_paper_review(*, task_id: str, report: PaperReviewReport, chunks: list[dict[str, object]], started_at: datetime) -> PaperReviewRunResponse:
    """把论文审查写入现有 Workflow 历史，复用统一的工具与阶段可观测性。"""

    finished_at = datetime.now(UTC)
    duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
    char_count = sum(len(str(chunk.get("text") or "")) for chunk in chunks)
    tool_call = WorkflowToolCall(
        call_id=f"{task_id}:document.read_text:1",
        task_id=task_id,
        step_id="paper_review_read",
        agent_id=_PAPER_REVIEW_AGENT_ID,
        tool_name="document.read_text",
        status="completed",
        risk_level="low",
        permission_required=False,
        max_attempts=1,
        timeout_ms=30_000,
        duration_ms=duration_ms,
        request={"document_ref": report.document_ref, "coverage": "full_document"},
        result={"chunk_count": len(chunks), "characters_scanned": char_count},
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
    )
    steps = [
        WorkflowStepRun(
            step_id="paper_review_read",
            agent=_PAPER_REVIEW_AGENT_ID,
            action="read_paper_document",
            status="completed",
            message=f"已读取 {len(chunks)} 个受控文本分块，覆盖选定论文材料。",
            output={"document_ref": report.document_ref, "chunk_count": len(chunks)},
        ),
        WorkflowStepRun(
            step_id=_PAPER_REVIEW_STEP_ID,
            agent=_PAPER_REVIEW_AGENT_ID,
            action="review_paper_document",
            status="completed",
            message=report.summary,
            output={"runtime": True, "paper_review_report": report.model_dump(mode="json"), "review_strategy": report.review_strategy},
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
        limits=RuntimeExecutionLimits(max_steps=2, max_tool_calls=1, max_retries_per_tool=0, tool_timeout_ms=30_000, task_timeout_ms=60_000),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
            duration_ms=duration_ms,
            step_total=2,
            step_completed=2,
            tool_call_total=1,
            estimated_input_tokens=max(1, char_count // 4),
        ),
    )
    events = [
        TaskLogEvent(task_id=task_id, sequence=1, event="paper_review_started", agent_id=_PAPER_REVIEW_AGENT_ID, step_id="paper_review_read", message="论文审查已开始，正在读取受控材料。"),
        TaskLogEvent(task_id=task_id, sequence=2, event="paper_review_rules_completed", agent_id=_PAPER_REVIEW_AGENT_ID, step_id=_PAPER_REVIEW_STEP_ID, level="warning" if report.findings else "info", message=report.summary),
    ]
    save_workflow_run(run=run, events=events, plan=None, tool_calls=[tool_call])
    return PaperReviewRunResponse(task_id=task_id, status="completed", report=report, workflow_run=run)
