"""多媒体助手在执行前生成的受限图片编辑计划契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


MediaEditScope = Literal["global", "local", "clarify"]
MediaPlanTool = Literal[
    "image.adjust",
    "image.select_region",
    "image.edit_region",
    "image.export",
    "clarify",
]


class MediaEditPlanStep(BaseModel):
    """模型只能提出已登记的工具和短说明，不能携带路径、命令或任意参数。"""

    tool: MediaPlanTool
    instruction: str = Field(min_length=1, max_length=240)


class MediaEditPlanCandidate(BaseModel):
    """仅用于计划评估的候选，不是可直接执行、授权或落库的 MediaEditPlan。"""

    version: Literal["agentflow.media_edit_plan.v1"] = "agentflow.media_edit_plan.v1"
    goal: str = Field(min_length=1, max_length=320)
    scope: MediaEditScope
    target_description: str = Field(default="", max_length=180)
    steps: list[MediaEditPlanStep] = Field(min_length=1, max_length=5)
    clarification_question: str = Field(default="", max_length=180)

    @model_validator(mode="after")
    def _validate_safe_execution_order(self) -> "MediaEditPlanCandidate":
        tools = [step.tool for step in self.steps]
        if self.scope == "clarify":
            if tools != ["clarify"]:
                raise ValueError("澄清计划只能包含 clarify 步骤。")
            if not self.clarification_question.strip():
                raise ValueError("澄清计划必须提供一个明确问题。")
            return self

        if "clarify" in tools:
            raise ValueError("非澄清计划不能混入 clarify 步骤。")
        if self.clarification_question.strip():
            raise ValueError("非澄清计划不能携带澄清问题。")
        if tools[-1] != "image.export":
            raise ValueError("图片编辑计划必须以 image.export 收束。")
        if self.scope == "global":
            if "image.select_region" in tools or "image.edit_region" in tools:
                raise ValueError("全局调整不能包含选区或局部生成编辑。")
            return self

        if "image.select_region" not in tools or "image.edit_region" not in tools:
            raise ValueError("局部编辑必须同时包含选区与局部编辑。")
        if tools.index("image.select_region") > tools.index("image.edit_region"):
            raise ValueError("局部编辑必须先选区，再调用 image.edit_region。")
        if not self.target_description.strip():
            raise ValueError("局部编辑必须说明目标对象或区域。")
        return self
