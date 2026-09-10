from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ContextWindowSource = Literal["verified_model", "conservative_fallback"]


class ContextEnvelopeAudit(BaseModel):
    """A content-free audit record for one bounded conversation context."""

    schema_version: str = "agentflow.context_envelope.v1"
    model_context_window_tokens: int = Field(ge=1)
    model_context_window_source: ContextWindowSource
    output_reserve_tokens: int = Field(ge=0)
    system_tool_reserve_tokens: int = Field(ge=0)
    current_message_tokens_estimate: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    memory_token_budget: int = Field(ge=0, le=20_000)
    estimated_memory_tokens: int = Field(ge=0)
    working_state_revision: int = Field(default=0, ge=0)
    long_term_memory_count: int = Field(default=0, ge=0, le=3)
    compaction_summary_included: bool = False
    recent_message_count: int = Field(default=0, ge=0)
    recent_complete_turn_count: int = Field(default=0, ge=0)
    latest_complete_turn_preserved: bool = False
    selected_sections: list[str] = Field(default_factory=list, max_length=4)
    omitted_sections: list[str] = Field(default_factory=list, max_length=4)
    deterministic_compaction: bool = True
    estimate_note: str = (
        "All token values are conservative local estimates, not provider-reported usage."
    )
