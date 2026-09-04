from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


CommanderIntentKind = Literal[
    "direct_answer",
    "document",
    "data",
    "knowledge",
    "presentation",
    "public_reference",
    "fresh_external_information",
    "clarify",
]
CommanderIntentDelivery = Literal[
    "answer",
    "analysis",
    "chart_png",
    "analysis_workbook",
    "presentation",
    "public_reference_search",
    "none",
]
CommanderIntentMaterialKind = Literal["document", "dataset", "knowledge_base"]
CommanderIntentAgentId = Literal[
    "document_agent",
    "data_agent",
    "knowledge_agent",
]


class CommanderIntentCandidate(BaseModel):
    """模型给出的受限语义候选，不是可执行计划或权限凭据。"""

    version: Literal["agentflow.commander_intent.v1"] = "agentflow.commander_intent.v1"
    intent: CommanderIntentKind = "direct_answer"
    is_follow_up: bool = False
    delivery: CommanderIntentDelivery = "answer"
    preferred_agents: list[CommanderIntentAgentId] = Field(default_factory=list, max_length=3)
    required_material_kinds: list[CommanderIntentMaterialKind] = Field(default_factory=list, max_length=3)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    clarifying_question: str = Field(default="", max_length=180)


class CommanderIntentResolution(BaseModel):
    """由 Harness 记录的候选来源与最终裁决，不保存模型原始思考。"""

    source: Literal["model", "deterministic"] = "deterministic"
    candidate: CommanderIntentCandidate | None = None
    final_intent: str = Field(default="direct_answer", max_length=80)
    applied: bool = False
    note: str = Field(default="", max_length=240)
