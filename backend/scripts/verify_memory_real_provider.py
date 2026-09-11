"""MEM-7 的有限真实 Provider 记忆验收。

此脚本只使用固定脱敏夹具、临时 SQLite 和临时运行偏好。它会读取客户已经配置的模型
Profile 以完成真实调用，但不会读取、打印或保存 API Key，也不会写入生产会话或偏好文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Callable
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded MEM-7 real-provider memory acceptance.")
    parser.add_argument(
        "--max-provider-requests",
        type=int,
        default=17,
        help="Hard cap for all answer and intent-resolution provider requests (default: 17).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=2048,
        help="Per-request provider output-token cap for this isolated process (default: 2048).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="Per-request timeout for this isolated provider acceptance (default: 120).",
    )
    parser.add_argument(
        "--min-request-interval-seconds",
        type=float,
        default=21.0,
        help="Minimum spacing between provider requests to respect the configured account RPM (default: 21).",
    )
    return parser.parse_args()


class ProviderUsageCollector:
    """Collect usage fields only; prompts and model replies never enter the report."""

    def __init__(self, maximum_requests: int, minimum_request_interval_seconds: float) -> None:
        self._maximum_requests = maximum_requests
        self._minimum_request_interval_seconds = minimum_request_interval_seconds
        self._last_request_started_at = 0.0
        self.attempt_total = 0
        self.usages: list[Any] = []

    async def reserve(self) -> None:
        if self.attempt_total >= self._maximum_requests:
            raise AssertionError(
                f"MEM-7 reached the configured provider request cap ({self._maximum_requests})."
            )
        elapsed = time.monotonic() - self._last_request_started_at
        wait_seconds = self._minimum_request_interval_seconds - elapsed
        if self._last_request_started_at and wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        self.attempt_total += 1
        self._last_request_started_at = time.monotonic()

    def record(self, usage: Any) -> None:
        self.usages.append(usage)


def _write_isolated_environment(args: argparse.Namespace) -> Path:
    if not 1 <= args.max_provider_requests <= 32:
        raise ValueError("--max-provider-requests must be between 1 and 32.")
    if not 128 <= args.max_output_tokens <= 2048:
        raise ValueError("--max-output-tokens must be between 128 and 2048.")
    if not 30.0 <= args.timeout_seconds <= 180.0:
        raise ValueError("--timeout-seconds must be between 30 and 180.")
    if not 0.0 <= args.min_request_interval_seconds <= 60.0:
        raise ValueError("--min-request-interval-seconds must be between 0 and 60.")
    work_dir = Path(tempfile.mkdtemp(prefix="agentflow_mem7_provider_"))
    os.environ["AGENTFLOW_DATABASE_PATH"] = str(work_dir / "mem7_provider.db")
    # This process has no write access through runtime preferences to the customer data directory.
    os.environ["AGENTFLOW_LLM_MAX_TOKENS"] = str(args.max_output_tokens)
    os.environ["AGENTFLOW_LLM_TIMEOUT_SECONDS"] = str(args.timeout_seconds)
    return work_dir


def _post_live_chat(
    client: Any,
    *,
    message: str,
    project_scope: str,
    conversation_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, str] = {"message": message, "project_scope": project_scope}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    response = client.post("/api/chat", json=payload)
    data = response.json()
    assert response.status_code == 200, data.get("detail", response.text)
    assert data["mode"] == "llm", f"expected real provider mode, got {data.get('mode')}"
    assert str(data["reply"]).strip(), "provider returned an empty reply"
    assert data.get("conversation_id"), "server did not return a conversation id"
    return data


def _assert_workflow_context_contains(
    payload: dict[str, Any],
    *,
    field: str,
    expected: tuple[str, ...],
) -> None:
    workflow_plan = payload.get("workflow_plan") or {}
    context = str(workflow_plan.get(field, []))
    missing = [item for item in expected if item not in context]
    assert not missing, f"workflow plan {field} omitted required synthetic marker(s)"


def _continuation_message(value: str) -> str:
    """Keep verification-only follow-ups out of the short-turn intent-resolution path."""

    normalized = " ".join(value.split())
    filler = "这是一次仅核对既有会话事实的验收请求，不增加新目标、不要求路由或工具，也不改变已有约束。"
    result = normalized
    while len(result) < 96:
        result += filler
    return result


def _latest_retrieval_contains(observations: list[Any], memory_id: str) -> bool:
    return any(
        item.event_type == "retrieval" and memory_id in item.recalled_memory_ids
        for item in observations
    )


def _make_memory(
    create_memory: Callable[..., Any],
    *,
    kind: str,
    scope: str,
    title: str,
    summary: str,
    tags: list[str],
) -> Any:
    return create_memory(
        kind=kind,
        scope=scope,
        title=title,
        summary=summary,
        tags=tags,
        source_task_id="task_mem7_synthetic_fixture",
        user_confirmed=True,
    )


def main() -> None:
    args = _parse_args()
    work_dir = _write_isolated_environment(args)
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    try:
        from fastapi.testclient import TestClient

        from app.agents.runner import summarize_model_usage
        from app.core.config import settings
        from app.database.conversation_repository import (
            get_conversation_context,
            get_conversation_working_state,
        )
        from app.database.memory_observability_repository import list_memory_observations
        from app.database.memory_repository import (
            create_long_term_memory,
            get_long_term_memory,
            search_long_term_memory_retrieval,
        )
        from app.services.conversation_memory import (
            persist_async_assistant_delivery,
            persist_successful_conversation_turn,
            prepare_conversation,
        )
        from app.services.memory_retrieval import LocalMemoryDenseCandidateProvider
        from app.services.model_gateway import ModelGatewayError, ModelRuntime, resolve_model_runtime_for_route
        from app.services import runtime_preferences_store
        from app.services.runtime_preferences_store import RuntimePreferencesRepository
        from main import app

        assert settings.chat_mode == "llm", "MEM-7 requires AGENTFLOW_CHAT_MODE=llm."
        route = resolve_model_runtime_for_route("commander_planning", validate=True)
        assert route.runtime.api_key_configured, "Commander route has no configured API key."

        original_preferences_repository = runtime_preferences_store._DEFAULT_REPOSITORY
        runtime_preferences_store._DEFAULT_REPOSITORY = RuntimePreferencesRepository(
            work_dir / "runtime_preferences.json"
        )
        runtime_preferences_store.save_runtime_preferences(
            permission_policy="smart_confirm",
            personality="professional",
            memory_enabled=True,
            conversation_retention_days=0,
        )

        collector = ProviderUsageCollector(
            args.max_provider_requests,
            args.min_request_interval_seconds,
        )
        original_chat_with_usage = ModelRuntime.chat_with_usage
        original_tool_turn = ModelRuntime.tool_turn

        async def observed_chat_with_usage(
            runtime: ModelRuntime,
            *,
            system_prompt: str,
            user_message: str,
        ) -> Any:
            await collector.reserve()
            result = await original_chat_with_usage(
                runtime,
                system_prompt=system_prompt,
                user_message=user_message,
            )
            collector.record(result.usage)
            return result

        async def observed_tool_turn(
            runtime: ModelRuntime,
            *,
            system_prompt: str,
            messages: list[Any],
            tools: list[Any],
        ) -> Any:
            await collector.reserve()
            result = await original_tool_turn(
                runtime,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
            )
            collector.record(result.usage)
            return result

        scenario_results: dict[str, str] = {}
        with (
            patch.object(ModelRuntime, "chat_with_usage", observed_chat_with_usage),
            patch.object(ModelRuntime, "tool_turn", observed_tool_turn),
            TestClient(app) as client,
        ):
            # 1. The real answer must see the latest deterministic Working State values.
            latest_scope = "project:mem7_latest"
            first = _post_live_chat(
                client,
                project_scope=latest_scope,
                message="这是一项内部演示准备。预算 5000，交付 PPT。",
            )
            conversation_id = str(first["conversation_id"])
            _post_live_chat(
                client,
                project_scope=latest_scope,
                conversation_id=conversation_id,
                message=_continuation_message("预算改为 2300。其余暂时不变，请保持该约束直到我明确修改它。"),
            )
            _post_live_chat(
                client,
                project_scope=latest_scope,
                conversation_id=conversation_id,
                message=(
                    "改为可编辑 DOCX，只使用材料 B。其余内容维持不变；这段说明只表达"
                    "当前最新约束，不涉及新增执行权限、外部服务或写入动作。"
                ),
            )
            latest = _post_live_chat(
                client,
                project_scope=latest_scope,
                conversation_id=conversation_id,
                message=_continuation_message(
                    "请仅用一行列出目前确认的预算数值、交付格式和材料标记，不要重新规划或新增假设。"
                ),
            )
            latest_state = get_conversation_working_state(
                conversation_id=conversation_id,
                project_scope=latest_scope,
            )
            assert latest_state.constraints["budget"].value == "2300"
            assert latest_state.constraints["delivery_format"].value == "DOCX"
            assert latest_state.constraints["material_scope"].value == ["B"]
            _assert_workflow_context_contains(
                latest,
                field="conversation_context_summary",
                expected=("2300", "DOCX", "B"),
            )
            scenario_results["latest_working_state"] = "passed"

            # 2. Seed only synthetic history, then use one real reply after deterministic compaction.
            compact_scope = "project:mem7_compaction"
            compacted = prepare_conversation(
                conversation_id=None,
                project_scope=compact_scope,
                message="预算 3300。",
                supplied_materials=[],
            )
            persist_successful_conversation_turn(
                prepared=compacted,
                user_message="预算 3300。",
                assistant_message="已确认当前预算。",
                material_bindings=[],
                task_id="task_mem7_compact_budget",
                plan_id="plan_mem7_compact_budget",
            )
            for index in range(12):
                persist_successful_conversation_turn(
                    prepared=compacted,
                    user_message=f"一次性说明 {index}：" + "合成内容" * 100,
                    assistant_message="已记录一次性合成说明。",
                    material_bindings=[],
                    task_id=f"task_mem7_compact_{index:02d}",
                    plan_id=f"plan_mem7_compact_{index:02d}",
                )
            compact_context = get_conversation_context(
                compacted.context.session.conversation_id,
                project_scope=compact_scope,
            )
            assert compact_context.session.summary
            compact_reply = _post_live_chat(
                client,
                project_scope=compact_scope,
                conversation_id=compacted.context.session.conversation_id,
                message=_continuation_message(
                    "请根据同一会话先前已经确认的内容，只回答仍然有效的预算数值，不要解释。"
                ),
            )
            _assert_workflow_context_contains(
                compact_reply,
                field="conversation_context_summary",
                expected=("3300",),
            )
            assert any(
                item.event_type == "context" and item.summary_message_count > 0
                for item in list_memory_observations(limit=80)
            )
            scenario_results["compaction_continuity"] = "passed"

            # 3. An asynchronous delivery is persisted before the real model continues the same session.
            async_scope = "project:mem7_async"
            async_prepared = prepare_conversation(
                conversation_id=None,
                project_scope=async_scope,
                message="请开始一项受控异步检查。",
                supplied_materials=[],
            )
            persist_successful_conversation_turn(
                prepared=async_prepared,
                user_message="请开始一项受控异步检查。",
                assistant_message="任务已进入受控执行队列。",
                material_bindings=[],
                task_id="task_mem7_async_seed",
                plan_id="plan_mem7_async_seed",
            )
            persist_async_assistant_delivery(
                conversation_id=async_prepared.context.session.conversation_id,
                task_id="task_mem7_async_recovered",
                assistant_message="异步任务已恢复并完成，完成标识为 MEM7_ASYNC_RECOVERED_MARKER。",
            )
            async_context = get_conversation_context(
                async_prepared.context.session.conversation_id,
                project_scope=async_scope,
            )
            assert any(
                "MEM7_ASYNC_RECOVERED_MARKER" in item.content
                for item in async_context.recent_messages
            )
            async_reply = _post_live_chat(
                client,
                project_scope=async_scope,
                conversation_id=async_prepared.context.session.conversation_id,
                message=_continuation_message(
                    "请确认刚才异步任务已经恢复完成，不要追加解释或发起新任务。"
                ),
            )
            assert async_reply["workflow_plan"]["conversation_context_summary"]
            scenario_results["async_delivery_continuation"] = "passed"

            # 4. A confirmed global preference is visible in a fresh conversation, not a one-off task fact.
            global_memory = _make_memory(
                create_long_term_memory,
                kind="user_preference",
                scope="global",
                title="MEM7_GLOBAL_PREF_CEDAR",
                summary="固定交付偏好代码为 MEM7_GLOBAL_PREF_CEDAR。",
                tags=["偏好", "MEM7"],
            )
            global_reply = _post_live_chat(
                client,
                project_scope="global",
                message="请只回答我的固定交付偏好代码，不要解释。",
            )
            _assert_workflow_context_contains(
                global_reply,
                field="memory_context_summary",
                expected=("MEM7_GLOBAL_PREF_CEDAR",),
            )
            assert _latest_retrieval_contains(list_memory_observations(limit=80), global_memory.memory_id)
            scenario_results["global_preference_new_session"] = "passed"

            # 5. Similar project facts must never make the other project into model context.
            alpha_memory = _make_memory(
                create_long_term_memory,
                kind="project_constraint",
                scope="project:mem7_alpha",
                title="MEM7_ALPHA_ONLY_NOVA",
                summary="项目 Alpha 的受控约束代码为 MEM7_ALPHA_ONLY_NOVA。",
                tags=["项目", "约束"],
            )
            beta_memory = _make_memory(
                create_long_term_memory,
                kind="project_constraint",
                scope="project:mem7_beta",
                title="MEM7_BETA_FORBIDDEN_ORBIT",
                summary="项目 Beta 的受控约束代码为 MEM7_BETA_FORBIDDEN_ORBIT。",
                tags=["项目", "约束"],
            )
            alpha_reply = _post_live_chat(
                client,
                project_scope="project:mem7_alpha",
                message="请只回答当前项目的受控约束代码，不要解释。",
            )
            _assert_workflow_context_contains(
                alpha_reply,
                field="memory_context_summary",
                expected=("MEM7_ALPHA_ONLY_NOVA",),
            )
            assert "MEM7_BETA_FORBIDDEN_ORBIT" not in str(
                alpha_reply["workflow_plan"].get("memory_context_summary", [])
            )
            observations = list_memory_observations(limit=100)
            assert _latest_retrieval_contains(observations, alpha_memory.memory_id)
            assert not _latest_retrieval_contains(observations, beta_memory.memory_id)
            scenario_results["project_scope_isolation"] = "passed"

            # 6. A wording change still retrieves the same confirmed preference via the default BM25 path.
            paraphrase_memory = _make_memory(
                create_long_term_memory,
                kind="project_constraint",
                scope="project:mem7_paraphrase",
                title="MEM7_PARAPHRASE_STYLE_CEDAR",
                summary="报告使用正式简洁的汇报语气，代码为 MEM7_PARAPHRASE_STYLE_CEDAR。",
                tags=["汇报", "语气", "正式"],
            )
            paraphrase_reply = _post_live_chat(
                client,
                project_scope="project:mem7_paraphrase",
                message="请只回答之前确认的报告表达风格代码，不要解释。",
            )
            _assert_workflow_context_contains(
                paraphrase_reply,
                field="memory_context_summary",
                expected=("MEM7_PARAPHRASE_STYLE_CEDAR",),
            )
            assert _latest_retrieval_contains(list_memory_observations(limit=120), paraphrase_memory.memory_id)
            scenario_results["paraphrase_retrieval"] = "passed"

            # 7. Dense is deliberately unadmitted; its optional probe must fail closed while the real
            # Commander response continues on the admitted BM25 route.
            dense_probe = search_long_term_memory_retrieval(
                query="报告表达风格",
                scopes={"project:mem7_paraphrase"},
                dense_candidate_provider=LocalMemoryDenseCandidateProvider(),
            )
            assert dense_probe.diagnostics.fallback_reason == "dense_unavailable"
            assert dense_probe.diagnostics.mode.endswith("dense_unavailable")
            degraded_reply = _post_live_chat(
                client,
                project_scope="project:mem7_paraphrase",
                message="请只回答之前确认的报告表达风格代码，不要解释。",
            )
            _assert_workflow_context_contains(
                degraded_reply,
                field="memory_context_summary",
                expected=("MEM7_PARAPHRASE_STYLE_CEDAR",),
            )
            scenario_results["dense_unavailable_bm25_continues"] = "passed"

            # 8. Turning memory off disables reads and usage updates for a real subsequent request.
            disabled_memory = _make_memory(
                create_long_term_memory,
                kind="user_preference",
                scope="global",
                title="MEM7_DISABLED_NEVER_INJECT",
                summary="此合成偏好在关闭长期记忆时不得读取或注入。",
                tags=["MEM7", "关闭"],
            )
            observation_count_before = len(list_memory_observations(limit=200))
            runtime_preferences_store.save_runtime_preferences(
                permission_policy="smart_confirm",
                personality="professional",
                memory_enabled=False,
                conversation_retention_days=0,
            )
            disabled_reply = _post_live_chat(
                client,
                project_scope="global",
                message="请用一句中文说明当前系统会如何处理普通问答。",
            )
            assert str(disabled_reply["reply"]).strip()
            assert get_long_term_memory(disabled_memory.memory_id).last_used_at == ""
            assert "MEM7_DISABLED_NEVER_INJECT" not in str(
                disabled_reply["workflow_plan"].get("memory_context_summary", [])
            )
            observations_after = list_memory_observations(limit=200)
            new_observations = observations_after[: max(0, len(observations_after) - observation_count_before)]
            assert not _latest_retrieval_contains(new_observations, disabled_memory.memory_id)
            scenario_results["memory_disabled_no_read"] = "passed"

        usage_summary = summarize_model_usage(collector.usages)
        report = {
            "acceptance": "agentflow.memory.mem7.real_provider.v1",
            "provider": route.runtime.provider,
            "model": route.runtime.model,
            "fixture_scope": "synthetic_only",
            "production_database_modified": False,
            "production_preferences_modified": False,
            "provider_request_cap": args.max_provider_requests,
            "provider_request_attempt_total": collector.attempt_total,
            "provider_response_total": len(collector.usages),
            "max_output_tokens_per_request": args.max_output_tokens,
            "timeout_seconds_per_request": args.timeout_seconds,
            "min_request_interval_seconds": args.min_request_interval_seconds,
            "provider_usage_reported_request_total": usage_summary.usage_reported_request_total,
            "provider_input_tokens": usage_summary.input_tokens,
            "provider_output_tokens": usage_summary.output_tokens,
            "provider_total_tokens": usage_summary.total_tokens,
            "scenarios": scenario_results,
        }
        assert len(scenario_results) == 8 and all(
            value == "passed" for value in scenario_results.values()
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        print("MEM-7 real-provider memory verification passed.")
    except ModelGatewayError as exc:
        raise SystemExit(f"MEM-7 real-provider verification could not start: {type(exc).__name__}") from exc
    finally:
        try:
            runtime_preferences_store._DEFAULT_REPOSITORY = original_preferences_repository
        except UnboundLocalError:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
