"""验证 K5.7 知识库上下文路由，不调用网络、数据库或真实模型。"""

from __future__ import annotations

from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.knowledge_context_router import (  # noqa: E402
    KNOWLEDGE_CONTEXT_CHAR_BUDGETS,
    KnowledgeContextBudgetError,
    enforce_knowledge_context_budget,
    plan_knowledge_context_route,
)
from app.services.model_gateway import ModelRuntime, get_verified_model_context_window_tokens  # noqa: E402


def _runtime(*, provider: str, model: str) -> ModelRuntime:
    """构造不会发起请求的 Runtime；仅用于检查 Provider 能力与路由边界。"""

    return ModelRuntime(
        provider=provider,
        label="verification runtime",
        transport="openai_compatible",
        base_url="https://example.invalid/v1",
        model=model,
        api_key="verification-key-not-sent",
        thinking="disabled",
        max_tokens=512,
        temperature=0.0,
        timeout_seconds=30.0,
    )


def main() -> None:
    deepseek = _runtime(provider="deepseek", model="deepseek-v4-flash")
    assert get_verified_model_context_window_tokens(deepseek) == 1_048_576
    answer = plan_knowledge_context_route(
        stage="knowledge_answer",
        system_prompt="系统约束",
        user_message="受控来源" * 100,
        model=deepseek,
    )
    assert answer.route == "retrieval_evidence"
    assert answer.route_reason == "interactive_question"
    assert answer.model_input_char_budget == KNOWLEDGE_CONTEXT_CHAR_BUDGETS["knowledge_answer"]
    assert answer.budget_state == "within_budget"
    assert answer.provider_long_context_state == "confirmed_not_enabled"
    assert answer.confirmed_model_context_window_tokens == 1_048_576
    assert answer.long_context_direct_execution is False
    enforce_knowledge_context_budget(answer)

    # 同一个 Provider 的旧/自定义模型不能因默认 profile 支持长窗口就被误判为已确认。
    unknown_model = plan_knowledge_context_route(
        stage="deep_map",
        system_prompt="x",
        user_message="y",
        model=_runtime(provider="deepseek", model="custom-gateway-model"),
    )
    assert unknown_model.route == "map_reduce"
    assert unknown_model.provider_long_context_state == "not_confirmed"
    assert unknown_model.confirmed_model_context_window_tokens is None

    # 离线 mock 不具备 Provider 身份；路由仍可验证，但不会制造供应商能力或 usage 观察。
    mock_route = plan_knowledge_context_route(
        stage="deep_reduce",
        system_prompt="x",
        user_message="y",
        model=object(),
    )
    assert mock_route.route == "map_reduce"
    assert mock_route.provider_long_context_state == "not_checked"
    assert mock_route.cache_usage_policy == "response_usage_only"

    over_budget = plan_knowledge_context_route(
        stage="deep_map",
        system_prompt="x" * KNOWLEDGE_CONTEXT_CHAR_BUDGETS["deep_map"],
        user_message="y",
        model=deepseek,
    )
    assert over_budget.budget_state == "over_budget"
    try:
        enforce_knowledge_context_budget(over_budget)
    except KnowledgeContextBudgetError:
        pass
    else:  # pragma: no cover - 防止未来直接放行超预算消息。
        raise AssertionError("超预算上下文不应改走长窗口后继续发送。")

    print("Knowledge K5.7 context routing verification passed: K3/K4 routes, budget stop and verified capability boundary.")


if __name__ == "__main__":
    main()
