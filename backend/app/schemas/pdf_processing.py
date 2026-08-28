"""文档助手 PDF 整理 Tool 的稳定输入输出协议。

PDF 整理是确定性文件处理，不调用模型。它仍通过任务、审计、产物和事件流协议运行，
这样客户能看到操作范围、结果和验证信息，后续也能被 Commander 作为受控 Tool 调度。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.workflow import WorkflowArtifact, WorkflowRunStatus


PdfProcessingOperation = Literal["merge", "extract", "rotate", "delete"]


class PdfProcessingStartRequest(BaseModel):
    """一次明确、不可覆盖原文件的 PDF 整理请求。"""

    model_config = ConfigDict(extra="forbid")

    operation: PdfProcessingOperation
    # 只接受 workspace 内的稳定相对文件名；服务层仍会再次做 containment 校验。
    document_refs: list[str] = Field(min_length=1, max_length=12)
    # 页码使用客户可读的 1-based 表示，例如 ``1-3,5``。空值只允许合并操作。
    page_range: str = Field(default="", max_length=240)
    # 旋转是相对当前页面方向的顺时针角度，不支持任意角度，避免不可预测的渲染结果。
    rotation_degrees: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def validate_operation_scope(self) -> "PdfProcessingStartRequest":
        cleaned_refs = [item.strip() for item in self.document_refs if item and item.strip()]
        if len(cleaned_refs) != len(self.document_refs) or len(set(cleaned_refs)) != len(cleaned_refs):
            raise ValueError("PDF 文件列表不能为空，也不能重复选择同一文件。")
        self.document_refs = cleaned_refs

        if self.operation == "merge":
            if len(self.document_refs) < 2:
                raise ValueError("合并至少需要选择两份 PDF 文件。")
            if self.page_range.strip() or self.rotation_degrees:
                raise ValueError("合并 PDF 不需要填写页码范围或旋转角度。")
            return self

        if len(self.document_refs) != 1:
            raise ValueError("提取、旋转和删除页面时只能选择一份 PDF 文件。")
        if not self.page_range.strip():
            raise ValueError("请填写页码范围，例如 1-3,5。")
        if self.operation == "rotate" and not self.rotation_degrees:
            raise ValueError("旋转页面请选择 90、180 或 270 度。")
        if self.operation != "rotate" and self.rotation_degrees:
            raise ValueError("只有旋转页面操作可以设置旋转角度。")
        return self


class PdfProcessingVerification(BaseModel):
    """确定性产物验证结果，供 UI 和任务历史直接展示。"""

    output_opened: bool
    expected_page_count: int = Field(ge=1)
    actual_page_count: int = Field(ge=1)
    output_size_bytes: int = Field(ge=1)


class PdfProcessingTaskStartResponse(BaseModel):
    task_id: str
    status: Literal["queued"] = "queued"


class PdfProcessingTaskResultResponse(BaseModel):
    task_id: str
    status: WorkflowRunStatus
    operation: PdfProcessingOperation | None = None
    summary: str = ""
    message: str = ""
    artifact: WorkflowArtifact | None = None
    verification: PdfProcessingVerification | None = None
