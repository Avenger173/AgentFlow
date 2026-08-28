"""总指挥计划修订的稳定 HTTP 协议。

计划修订不是让客户端直接编辑步骤、权限或工作区路径。客户只提交新的目标和变更说明，
服务端再复用原有的显式材料与偏好重新生成、校验并保存下一版计划。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import WorkflowPlan
from app.schemas.workflow import WorkflowRun


class WorkflowPlanRevisionRequest(BaseModel):
    """用户确认后的计划修改请求。

    ``extra=forbid`` 是重要的边界：不能借这个接口偷偷提交步骤 JSON、权限开关或本机路径，
    它们都只能由 Commander 准入规则重新推导。
    """

    model_config = ConfigDict(extra="forbid")

    user_goal: str = Field(min_length=2, max_length=1200)
    change_summary: str = Field(min_length=2, max_length=400)
    confirmed: Literal[True]


class WorkflowPlanVersionSummary(BaseModel):
    """计划版本列表的轻量摘要。"""

    task_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    parent_plan_id: str | None = None
    user_goal: str
    change_summary: str = ""
    created_at: str
    is_current: bool = False


class WorkflowPlanVersionListResponse(BaseModel):
    task_id: str
    current_plan_id: str | None = None
    total: int
    versions: list[WorkflowPlanVersionSummary] = Field(default_factory=list)


class WorkflowPlanVersionDetailResponse(BaseModel):
    task_id: str
    version: WorkflowPlanVersionSummary
    workflow_plan: WorkflowPlan


class WorkflowPlanRevisionResponse(BaseModel):
    """重新生成后的当前计划和新的 dry-run 状态。"""

    task_id: str
    workflow_plan: WorkflowPlan
    workflow_run: WorkflowRun
    message: str
