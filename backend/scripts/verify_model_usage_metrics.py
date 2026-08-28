"""验证 ModelGateway 的供应商 usage 归一化，不调用真实模型或网络。"""

from __future__ import annotations

import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.model_gateway import (  # noqa: E402
    _extract_anthropic_tool_turn,
    _extract_openai_tool_turn,
    extract_model_usage_metrics,
    get_model_provider_profile,
)
from app.agents.runner import summarize_model_usage  # noqa: E402


def main() -> None:
    """覆盖 DeepSeek、OpenAI、Anthropic 与未知 usage 的可观测性边界。"""

    deepseek = extract_model_usage_metrics(
        {
            "usage": {
                "prompt_tokens": 120,
                "prompt_cache_hit_tokens": 96,
                "prompt_cache_miss_tokens": 24,
                "completion_tokens": 15,
                "total_tokens": 135,
            }
        }
    )
    assert deepseek.input_tokens == 120
    assert deepseek.output_tokens == 15
    assert deepseek.total_tokens == 135
    assert deepseek.cache_read_input_tokens == 96
    assert deepseek.cache_miss_input_tokens == 24
    assert deepseek.usage_observation == "reported"
    assert deepseek.cache_observation == "reported"

    openai = extract_model_usage_metrics(
        {
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 12,
                "total_tokens": 92,
                "prompt_tokens_details": {"cached_tokens": 64},
            }
        }
    )
    assert openai.input_tokens == 80
    assert openai.cache_read_input_tokens == 64
    assert openai.cache_miss_input_tokens is None
    assert openai.usage_observation == "reported"
    assert openai.cache_observation == "reported"

    anthropic = extract_model_usage_metrics(
        {
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 40,
                "output_tokens": 7,
            }
        }
    )
    # Anthropic 的官方 usage 口径要求把普通输入、cache write 与 cache read 一起计入输入。
    assert anthropic.input_tokens == 70
    assert anthropic.output_tokens == 7
    assert anthropic.total_tokens == 77
    assert anthropic.cache_read_input_tokens == 40
    assert anthropic.cache_creation_input_tokens == 20
    assert anthropic.usage_observation == "reported"
    assert anthropic.cache_observation == "reported"

    absent = extract_model_usage_metrics({"id": "response_without_usage"})
    assert absent.input_tokens is None
    assert absent.cache_read_input_tokens is None
    assert absent.usage_observation == "not_reported"
    assert absent.cache_observation == "not_reported"

    openai_turn = _extract_openai_tool_turn(
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
        }
    )
    assert openai_turn.usage.total_tokens == 8
    assert openai_turn.usage.cache_observation == "not_reported"

    anthropic_turn = _extract_anthropic_tool_turn(
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 4, "output_tokens": 3, "cache_read_input_tokens": 0},
        }
    )
    assert anthropic_turn.usage.input_tokens == 4
    assert anthropic_turn.usage.cache_read_input_tokens == 0
    assert anthropic_turn.usage.cache_observation == "reported"

    summary = summarize_model_usage((deepseek, openai, absent))
    assert summary.request_total == 3
    assert summary.usage_reported_request_total == 2
    assert summary.cache_observed_request_total == 2
    assert summary.input_tokens == 200
    assert summary.output_tokens == 27
    assert summary.total_tokens == 227
    assert summary.cache_read_input_tokens == 160
    assert summary.cache_miss_input_tokens == 24

    assert get_model_provider_profile("deepseek").context_cache_mode == "automatic_observable"
    assert get_model_provider_profile("anthropic").context_cache_mode == "explicit_request"
    assert get_model_provider_profile("openai_compatible").context_cache_mode == "unknown"

    print("Model usage metrics verification passed: DeepSeek/OpenAI/Anthropic/absent usage.")


if __name__ == "__main__":
    main()
