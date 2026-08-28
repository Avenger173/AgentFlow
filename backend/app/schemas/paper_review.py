"""论文审查 V1 的输入与报告协议。

此协议专注于文档可读性、结构和引用形式，不承担查重、学术创新性判断或学术/法律结论。
论文审查与项目审查共享 Harness 记录方式，但规则分类必须独立，避免客户误把项目交付检查
当作论文规范审查。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.document_agent import DocumentSourceRef
from app.schemas.workflow import WorkflowRun


PaperType = Literal["auto", "article", "thesis", "report"]
PaperReviewSeverity = Literal["high", "medium", "low"]
PaperReviewCategory = Literal["structure", "citation", "figure_table", "format", "language"]
PaperReviewCheckStatus = Literal["passed", "attention"]


class PaperReviewRequest(BaseModel):
    """一次只审查用户明确选择的一篇受控论文或学术报告。"""

    document_ref: str = Field(min_length=1, max_length=260)
    paper_type: PaperType = "auto"


class PaperReviewFinding(BaseModel):
    """一条带来源的论文规范问题，建议必须是可操作的修改方向。"""

    id: str = Field(min_length=1, max_length=80)
    rule_id: str = Field(min_length=1, max_length=120)
    severity: PaperReviewSeverity
    category: PaperReviewCategory
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=1_000)
    suggestion: str = Field(min_length=1, max_length=1_000)
    evidence: str = Field(min_length=1, max_length=600)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=2)


class PaperReviewCheck(BaseModel):
    """一项论文规则的检查结论，保留通过项和待关注项以方便复核。"""

    rule_id: str = Field(min_length=1, max_length=120)
    category: PaperReviewCategory
    label: str = Field(min_length=1, max_length=120)
    status: PaperReviewCheckStatus
    message: str = Field(min_length=1, max_length=600)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=2)


class PaperReviewReport(BaseModel):
    document_ref: str = Field(min_length=1, max_length=260)
    paper_type: PaperType
    review_strategy: Literal["deterministic_paper_rules_v1"] = "deterministic_paper_rules_v1"
    summary: str = Field(min_length=1, max_length=1_200)
    findings: list[PaperReviewFinding] = Field(default_factory=list, max_length=12)
    checks: list[PaperReviewCheck] = Field(min_length=1, max_length=6)
    warnings: list[str] = Field(default_factory=list, max_length=8)


class PaperReviewRunResponse(BaseModel):
    task_id: str
    status: Literal["completed", "failed"]
    report: PaperReviewReport
    workflow_run: WorkflowRun


class PaperReviewTaskStartResponse(BaseModel):
    """异步入口的受理回执；客户端随后订阅阶段事件并按 task_id 获取终态。"""

    task_id: str


class PaperReviewTaskResultResponse(BaseModel):
    """运行中不返回半成品报告，完成后才暴露经过 schema 校验的结果。"""

    task_id: str
    status: Literal["running", "completed", "failed"]
    result: PaperReviewRunResponse | None = None
