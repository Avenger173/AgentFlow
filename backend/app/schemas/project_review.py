"""项目文档审查的稳定协议。

项目审查不是把通用聊天回答换一个标题，而是把可复核的质量门、问题位置和处理建议写成独立
协议。第一版使用确定性规则，后续即使增加模型辅助审查，也必须输出同一套 finding/check
结构，不能把无法定位的自由文本混入正式报告。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.document_agent import DocumentSourceRef
from app.schemas.workflow import WorkflowRun


ProjectDocumentType = Literal["auto", "prd", "project_proposal", "project_plan"]
ProjectReviewStatus = Literal["completed", "failed"]
ProjectReviewSeverity = Literal["high", "medium", "low"]
ProjectReviewCategory = Literal[
    "scope",
    "acceptance",
    "ownership",
    "schedule",
    "risk_dependency",
    "terminology",
]
ProjectReviewCheckStatus = Literal["passed", "attention"]


class ProjectReviewRequest(BaseModel):
    """用户明确选择一份项目材料后发起审查的输入。

    V1 不支持让服务端自行猜测“当前项目文件”，也不接收绝对路径、正文或多个互相混杂的
    材料。用户先选择一份 PRD、项目方案或计划，才能让问题位置和建议保持可解释。
    """

    document_ref: str = Field(min_length=1, max_length=260)
    document_type: ProjectDocumentType = "auto"


class ProjectReviewFinding(BaseModel):
    """一条可处理的问题：规则、影响、建议与来源必须同时存在。"""

    id: str = Field(min_length=1, max_length=80)
    rule_id: str = Field(min_length=1, max_length=120)
    severity: ProjectReviewSeverity
    category: ProjectReviewCategory
    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=1_000)
    suggestion: str = Field(min_length=1, max_length=1_000)
    evidence: str = Field(min_length=1, max_length=600)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=2)


class ProjectReviewCheck(BaseModel):
    """一项质量门的检查结果；通过项也保留证据，避免报告只剩负面清单。"""

    rule_id: str = Field(min_length=1, max_length=120)
    category: ProjectReviewCategory
    label: str = Field(min_length=1, max_length=120)
    status: ProjectReviewCheckStatus
    message: str = Field(min_length=1, max_length=600)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=2)


class ProjectReviewReport(BaseModel):
    """项目审查的可展示报告，不包含模型内部思考或本机绝对路径。"""

    document_ref: str = Field(min_length=1, max_length=260)
    document_type: ProjectDocumentType
    review_strategy: Literal["deterministic_rules_v1"] = "deterministic_rules_v1"
    summary: str = Field(min_length=1, max_length=1_200)
    findings: list[ProjectReviewFinding] = Field(default_factory=list, max_length=12)
    checks: list[ProjectReviewCheck] = Field(min_length=1, max_length=6)
    warnings: list[str] = Field(default_factory=list, max_length=8)


class ProjectReviewRunResponse(BaseModel):
    """同步审查入口的完整结果，同时包含现有任务历史可消费的 WorkflowRun。"""

    task_id: str
    status: ProjectReviewStatus
    report: ProjectReviewReport
    workflow_run: WorkflowRun


class ProjectReviewTaskStartResponse(BaseModel):
    """异步入口先返回稳定任务 ID，客户端再复用既有事件流与结果轮询。"""

    task_id: str


class ProjectReviewTaskResultResponse(BaseModel):
    """任务运行时不提前泄露半成品；完成后才返回经 schema 校验的报告。"""

    task_id: str
    status: Literal["running", "completed", "failed"]
    result: ProjectReviewRunResponse | None = None
