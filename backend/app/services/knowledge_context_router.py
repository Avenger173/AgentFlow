"""知识库 K5.7 的显式上下文路由与预算边界。

这个模块不读取资料、不会调用模型，也不做 token 估算。它只对“已经构造好的受控消息”做
字符级预算判断，并把 K2/K3/K4 为什么没有改走整库长上下文的事实写成稳定契约。这样模型
窗口再大，也不能绕过来源 Gate、Map checkpoint 或 Provider usage 的可观测性边界。
"""

from __future__ import annotations

from app.schemas.knowledge import KnowledgeContextRouteDecision, KnowledgeContextStage
from app.services.model_gateway import ModelRuntime, get_verified_model_context_window_tokens


# 字符预算是 AgentFlow 的输入防护阈值，不是假装精确的 tokenizer 或 Provider 账单。K3 的 4 条
# 已核验证据、K4 的单章节 Map 与最多 6 个 checkpoint Reduce 都必须在这些上限内收束。
KNOWLEDGE_CONTEXT_CHAR_BUDGETS: dict[KnowledgeContextStage, int] = {
    "knowledge_answer": 32_000,
    "deep_map": 12_000,
    "deep_reduce": 18_000,
}


class KnowledgeContextBudgetError(ValueError):
    """受控消息超出产品预算时阻止 Provider 请求，而不是改为整库直灌。"""


def plan_knowledge_context_route(
    *,
    stage: KnowledgeContextStage,
    system_prompt: str,
    user_message: str,
    model: object | None,
) -> KnowledgeContextRouteDecision:
    """为一次已受控的 K3/K4 模型调用生成可审计路由决策。

    ``model`` 允许离线回归使用假的 ToolCallingModel；只有真实 ``ModelRuntime`` 才查询已核验的
    Provider 能力。即便命中已知长窗口，首期也明确记录为“已确认但未启用”，继续走稳定的
    retrieval evidence 或 Map-Reduce 路线。
    """

    route = "retrieval_evidence" if stage == "knowledge_answer" else "map_reduce"
    route_reason = {
        "knowledge_answer": "interactive_question",
        "deep_map": "chapter_checkpoint",
        "deep_reduce": "summary_checkpoint",
    }[stage]
    runtime = model if isinstance(model, ModelRuntime) else None
    confirmed_window = get_verified_model_context_window_tokens(runtime)
    provider_state = (
        "not_checked"
        if runtime is None
        else "confirmed_not_enabled"
        if confirmed_window is not None
        else "not_confirmed"
    )
    budget = KNOWLEDGE_CONTEXT_CHAR_BUDGETS[stage]
    char_count = len(system_prompt) + len(user_message)
    return KnowledgeContextRouteDecision(
        stage=stage,
        route=route,
        route_reason=route_reason,
        model_input_char_count=char_count,
        model_input_char_budget=budget,
        budget_state="within_budget" if char_count <= budget else "over_budget",
        confirmed_model_context_window_tokens=confirmed_window,
        provider_long_context_state=provider_state,
    )


def enforce_knowledge_context_budget(decision: KnowledgeContextRouteDecision) -> None:
    """阻止超预算请求，并给出不包含模型消息或客户正文的稳定原因。"""

    if decision.budget_state == "within_budget":
        return
    raise KnowledgeContextBudgetError(
        f"{decision.stage} 的受控模型输入超过当前字符预算；未改走整库长上下文，请缩小范围后重试。"
    )
