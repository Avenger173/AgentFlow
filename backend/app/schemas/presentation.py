"""文档助手项目方案演示文稿的稳定协议。

PPT 制作属于文档助手的确定性交付工作流，不是一个独立 Agent。客户端只选择已核验草稿任务
并确认计划；章节正文、来源、模板和输出路径始终由服务端从持久化快照恢复与控制。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.document_agent import DocumentSourceRef


PresentationType = Literal["project_proposal"]
PresentationSlideRole = Literal["cover", "agenda", "content", "summary", "sources"]
PresentationPreflightStatus = Literal["passed", "attention"]
PresentationPreflightSeverity = Literal["high", "medium", "low"]


class PresentationPreviewRequest(BaseModel):
    """读取一份可确认的固定模板预览，不创建文件。"""

    model_config = ConfigDict(extra="forbid")

    presentation_type: PresentationType = "project_proposal"


class PresentationSlidePlan(BaseModel):
    """一张待渲染幻灯片的受控内容计划。

    该结构只包含从已核验草稿提炼的标题、短要点和来源，不接受客户端富文本，也不会让模型
    在导出时再次补写事实。``source_refs`` 既用于渲染页脚，也用于用户确认前的可追溯检查。
    """

    slide_id: str = Field(min_length=1, max_length=80)
    role: PresentationSlideRole
    title: str = Field(min_length=1, max_length=160)
    # 正文页通常不超过五条；来源页允许展示至多十二条去重来源，完整审计仍在原任务历史中。
    bullets: list[str] = Field(default_factory=list, max_length=12)
    source_refs: list[DocumentSourceRef] = Field(default_factory=list, max_length=4)


class PresentationPreflightFinding(BaseModel):
    """自动交付预检发现的材料缺口。

    预检复用项目审查的确定性规则，但不会创建额外任务或阻断客户查看 PPT 计划。它只在
    ``PresentationPreviewResponse`` 中给出制作前应关注的问题；来源仍指回客户已选择的材料。
    """

    severity: PresentationPreflightSeverity
    category: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    suggestion: str = Field(min_length=1, max_length=1_000)
    source_refs: list[DocumentSourceRef] = Field(min_length=1, max_length=2)


class PresentationPreflight(BaseModel):
    """项目方案 PPT 在导出前自动执行的只读材料质量预检。"""

    strategy: Literal["project_delivery_preflight_v1"] = "project_delivery_preflight_v1"
    status: PresentationPreflightStatus
    summary: str = Field(min_length=1, max_length=1_200)
    checked_documents: list[str] = Field(min_length=1, max_length=4)
    check_total: int = Field(ge=1, le=24)
    passed_check_total: int = Field(ge=0, le=24)
    attention_check_total: int = Field(ge=0, le=24)
    high_attention_total: int = Field(ge=0, le=12)
    findings: list[PresentationPreflightFinding] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=4)


class PresentationPreviewResponse(BaseModel):
    """用户确认前看到的演示文稿计划。"""

    source_task_id: str = Field(min_length=1, max_length=120)
    source_version_id: str = Field(min_length=1, max_length=120)
    presentation_type: PresentationType
    plan_id: str = Field(min_length=16, max_length=96)
    title: str = Field(min_length=1, max_length=240)
    slides: list[PresentationSlidePlan] = Field(min_length=3, max_length=12)
    # 预检由服务端自动执行；客户端只展示同一份只读结果，不能伪造“已通过”。
    preflight: PresentationPreflight
    warnings: list[str] = Field(default_factory=list, max_length=6)


class PresentationExportRequest(BaseModel):
    """用户确认把当前预览计划渲染为一个新的 .pptx 文件。"""

    model_config = ConfigDict(extra="forbid")

    presentation_type: PresentationType = "project_proposal"
    plan_id: str = Field(min_length=16, max_length=96)
    filename: str = Field(default="", max_length=120)
    confirmed: bool = False


class PresentationVerification(BaseModel):
    """对刚写入 PPTX 的回读验证摘要。"""

    passed: bool
    slide_count: int = Field(ge=0)
    source_slide_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=6)


class PresentationExportResponse(BaseModel):
    """确认导出后的稳定回执，不暴露服务端绝对路径。"""

    task_id: str = Field(min_length=1, max_length=120)
    artifact_id: str = Field(min_length=1, max_length=180)
    filename: str = Field(min_length=1, max_length=120)
    relative_path: str = Field(min_length=1, max_length=255)
    artifact_uri: str = Field(min_length=1, max_length=255)
    slide_count: int = Field(ge=1)
    verification: PresentationVerification
    message: str
