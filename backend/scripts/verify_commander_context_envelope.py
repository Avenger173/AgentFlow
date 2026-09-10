"""Verify MEM-3 ContextEnvelope selection without a provider call or customer data."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import ValidationError

from app.database.conversation_repository import estimate_conversation_tokens
from app.schemas.agent import AgentDescriptor
from app.schemas.chat import ChatRequest, WorkflowPlanPreferences
from app.schemas.conversation import (
    ConversationActiveTask,
    ConversationContext,
    ConversationMessageRecord,
    ConversationOpenItem,
    ConversationSessionRecord,
    ConversationWorkingState,
    ConversationWorkingStateValue,
)
from app.schemas.memory import LongTermMemoryRecord
from app.services.commander import create_commander_plan
from app.services.commander_intent import resolve_commander_intent_candidate
from app.services.conversation_context_envelope import (
    ContextEnvelopeBudgetError,
    build_context_envelope,
)
from app.services.conversation_working_state import build_working_state_prompt_summary
from app.services.llm_chat import _system_prompt_for_agent
from app.services.model_gateway import ModelRuntime


def _runtime(*, provider: str, model: str, max_tokens: int = 2_048) -> ModelRuntime:
    return ModelRuntime(
        provider=provider,
        label="verification runtime",
        transport="openai_compatible",
        base_url="https://example.invalid/v1",
        model=model,
        api_key="verification-key-not-sent",
        thinking="disabled",
        max_tokens=max_tokens,
        temperature=0.0,
        timeout_seconds=30.0,
    )


def _context(*, oversized_latest_turn: bool = False) -> ConversationContext:
    conversation_id = "conv_contextenvelope01"
    state = ConversationWorkingState(
        conversation_id=conversation_id,
        project_scope="project:context-envelope",
        revision=7,
        current_goal=ConversationWorkingStateValue(
            value="Create a verified player career presentation.",
            source="user_message",
            source_id="task_context_goal",
        ),
        constraints={
            "budget": ConversationWorkingStateValue(
                value="3000",
                source="user_message",
                source_id="task_context_budget",
            ),
            "delivery_format": ConversationWorkingStateValue(
                value="PPTX",
                source="user_message",
                source_id="task_context_format",
            ),
        },
        open_items=[
            ConversationOpenItem(
                item_id="task:task_context_active",
                title="Verify the data table before export.",
                status="open",
                task_id="task_context_active",
                source="workflow_run",
                source_id="task_context_active",
            )
        ],
        active_task=ConversationActiveTask(
            task_id="task_context_active",
            plan_id="plan_context_active",
            status="running",
            current_step="verify_data",
            next_action="review_sources",
        ),
    )
    latest_user = "latest user requirement: keep the chart editable."
    latest_assistant = "latest assistant response: data validation is still pending."
    if oversized_latest_turn:
        latest_user = "u" * 7_900 + " [latest-user-marker]"
        latest_assistant = "a" * 7_900 + " [latest-assistant-marker]"
    messages = [
        ConversationMessageRecord(
            message_id="msg_context_user_001",
            conversation_id=conversation_id,
            role="user",
            content="earlier user request",
            task_id="task_context_old",
            created_at="2026-09-10T00:00:01Z",
        ),
        ConversationMessageRecord(
            message_id="msg_context_assistant_001",
            conversation_id=conversation_id,
            role="assistant",
            content="earlier assistant response",
            task_id="task_context_old",
            created_at="2026-09-10T00:00:02Z",
        ),
        ConversationMessageRecord(
            message_id="msg_context_delivery_001",
            conversation_id=conversation_id,
            role="assistant",
            content="delivery-only message must not become a half turn",
            task_id="task_context_delivery",
            created_at="2026-09-10T00:00:03Z",
        ),
        ConversationMessageRecord(
            message_id="msg_context_user_002",
            conversation_id=conversation_id,
            role="user",
            content=latest_user,
            task_id="task_context_active",
            created_at="2026-09-10T00:00:04Z",
        ),
        ConversationMessageRecord(
            message_id="msg_context_assistant_002",
            conversation_id=conversation_id,
            role="assistant",
            content=latest_assistant,
            task_id="task_context_active",
            created_at="2026-09-10T00:00:05Z",
        ),
    ]
    return ConversationContext(
        session=ConversationSessionRecord(
            conversation_id=conversation_id,
            project_scope="project:context-envelope",
            summary=(
                "[Goal] career presentation\n[Constraint] verified values only\n"
                "[Todo] confirm chart data\n[Result] none"
            ),
            last_task_id="task_context_active",
            last_plan_id="plan_context_active",
            archived_message_count=5,
            created_at="2026-09-10T00:00:00Z",
            updated_at="2026-09-10T00:00:05Z",
        ),
        recent_messages=messages,
        summarized_message_count=0,
        estimated_memory_tokens=0,
        working_state=state,
    )


def _memory() -> list[LongTermMemoryRecord]:
    return [
        LongTermMemoryRecord(
            memory_id="mem_context_001",
            kind="project_constraint",
            scope="project:context-envelope",
            title="Data delivery rule",
            summary="Only use verified numeric data in customer-facing charts.",
            tags=["data"],
            created_at="2026-09-10T00:00:00Z",
            updated_at="2026-09-10T00:00:00Z",
        )
    ]


class _CapturingIntentRuntime:
    def __init__(self) -> None:
        self.payload: dict[str, object] = {}

    async def chat_json(self, *, system_prompt: str, user_message: str, maximum_tokens: int) -> str:
        del system_prompt, maximum_tokens
        self.payload = json.loads(user_message)
        return (
            '{"version":"agentflow.commander_intent.v1","intent":"presentation",'
            '"is_follow_up":true,"delivery":"presentation","preferred_agents":[],'
            '"required_material_kinds":[],"confidence":0.9,"clarifying_question":""}'
        )


def main() -> None:
    context = _context()
    current_message = "Continue with the latest requirement."
    envelope = build_context_envelope(
        message=current_message,
        context=context,
        long_term_memories=_memory(),
        runtime=_runtime(provider="deepseek", model="deepseek-v4-flash"),
    )
    state_summary = build_working_state_prompt_summary(context.working_state)
    assert envelope.audit.model_context_window_source == "verified_model"
    assert envelope.audit.model_context_window_tokens == 1_048_576
    assert envelope.audit.memory_token_budget == 20_000
    assert envelope.audit.estimated_memory_tokens <= envelope.audit.memory_token_budget
    assert envelope.audit.estimate_note.endswith("not provider-reported usage.")
    assert "Create a verified player career presentation." in envelope.rendered_context
    assert "budget=3000" in envelope.rendered_context
    assert "task_context_active" in envelope.rendered_context
    assert "plan_context_active" in envelope.rendered_context
    assert "latest user requirement" in envelope.rendered_context
    assert "latest assistant response" in envelope.rendered_context
    assert all(item.message_id != "msg_context_delivery_001" for item in envelope.selected_recent_messages)
    assert [item.role for item in envelope.selected_recent_messages] == ["user", "assistant", "user", "assistant"]
    assert envelope.planning_context_summary[-1] == state_summary

    intent_runtime = _CapturingIntentRuntime()
    asyncio.run(
        resolve_commander_intent_candidate(
            runtime=intent_runtime,  # type: ignore[arg-type]
            message=envelope.current_message,
            conversation_context=envelope.rendered_context,
            agents=[],
            materials=[],
            agent_hints=[],
        )
    )
    assert intent_runtime.payload["current_message"] == envelope.current_message
    assert intent_runtime.payload["conversation_context"] == envelope.rendered_context

    plan = create_commander_plan(
        envelope.current_message,
        available_agents=[],
        preferences=WorkflowPlanPreferences(),
        memory_context=_memory(),
        memory_context_summary_override=envelope.memory_context_summary,
        project_scope="project:context-envelope",
        conversation_id=context.session.conversation_id,
        conversation_context_summary=envelope.planning_context_summary,
        context_envelope_audit=envelope.audit,
        has_conversation_context=envelope.has_conversation_context,
    )
    assert plan.context_envelope_audit == envelope.audit
    assert plan.memory_context_summary == envelope.memory_context_summary
    assert state_summary in plan.conversation_context_summary
    assert plan.user_goal == envelope.current_message

    reply_prompt = _system_prompt_for_agent(
        AgentDescriptor(
            id="commander_agent",
            name="Commander",
            description="Coordinates bounded plans.",
            category="system",
        ),
        planning_context="validated plan facts",
        context_envelope=envelope.rendered_context,
    )
    assert envelope.rendered_context in reply_prompt
    assert "validated plan facts" in reply_prompt

    fallback_envelope = build_context_envelope(
        message=current_message,
        context=context,
        long_term_memories=_memory(),
        runtime=_runtime(provider="fixture", model="unknown-model"),
    )
    assert fallback_envelope.audit.model_context_window_source == "conservative_fallback"
    assert fallback_envelope.audit.estimated_memory_tokens <= fallback_envelope.audit.memory_token_budget

    clipped_envelope = build_context_envelope(
        message=current_message,
        context=_context(oversized_latest_turn=True),
        long_term_memories=[],
        runtime=_runtime(provider="fixture", model="unknown-model", max_tokens=10_000),
    )
    assert clipped_envelope.audit.estimated_memory_tokens <= clipped_envelope.audit.memory_token_budget
    assert clipped_envelope.audit.latest_complete_turn_preserved is True
    assert [item.role for item in clipped_envelope.selected_recent_messages[-2:]] == ["user", "assistant"]
    assert "[latest-user-marker]" in clipped_envelope.selected_recent_messages[-2].content
    assert "[latest-assistant-marker]" in clipped_envelope.selected_recent_messages[-1].content
    assert estimate_conversation_tokens(clipped_envelope.rendered_context) == clipped_envelope.audit.estimated_memory_tokens

    try:
        build_context_envelope(
            message="x" * 4_000,
            context=context,
            long_term_memories=[],
            runtime=_runtime(provider="fixture", model="unknown-model", max_tokens=15_000),
        )
    except ContextEnvelopeBudgetError:
        pass
    else:
        raise AssertionError("required working state must not be silently dropped when no safe budget remains")

    try:
        ChatRequest(message="x" * 4_001)
    except ValidationError:
        pass
    else:
        raise AssertionError("chat input must have a bounded model-context contract")

    print("Commander MEM-3 ContextEnvelope verification passed.")


if __name__ == "__main__":
    main()
