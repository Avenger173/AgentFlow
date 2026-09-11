"""运行 AgentFlow 记忆系统的脱敏离线基线评测。

默认 ``baseline`` 模式如实输出当前实现的通过、失败和未支持能力，便于 MEM-0 固定
差距；它不会为了旧实现的已知缺口返回失败码。后续阶段使用 ``--mode gate``，此时任一
required 用例未通过都会返回非零。脚本只使用合成夹具、临时 SQLite 和本地服务函数，
不会读取开发数据库、客户材料或模型配置，也不会调用网络或模型。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = BACKEND_ROOT / "scripts" / "fixtures" / "memory_eval_cases_v1.json"
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_memory_quality_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))

from app.database.conversation_repository import (  # noqa: E402
    get_conversation_context,
    get_conversation_transcript,
    save_conversation_turn,
)
from app.database.memory_repository import (  # noqa: E402
    create_long_term_memory,
    get_long_term_memory,
    search_long_term_memories,
)
from app.database.sqlite import get_connection  # noqa: E402
from app.schemas.chat import ChatRequest, WorkflowPlanPreferences  # noqa: E402
from app.schemas.conversation import ConversationWorkingState  # noqa: E402
from app.services.agent_catalog import get_agent  # noqa: E402
from app.services.commander_memory import retrieve_commander_memory_context  # noqa: E402
from app.services.commander_intent import resolve_commander_intent_candidate  # noqa: E402
from app.services.conversation_memory import (  # noqa: E402
    CONVERSATION_ARCHIVE_MESSAGE_MAX_CHARS,
    prepare_conversation,
    sanitize_conversation_text,
)
from app.services.conversation_working_state import (  # noqa: E402
    project_workflow_event,
    reduce_user_message,
)
from app.services.long_term_memory import LongTermMemorySafetyError, sanitize_memory_text  # noqa: E402
from app.services.llm_chat import LlmChatError, create_llm_chat_response  # noqa: E402
from app.services.model_gateway import ModelGatewayError  # noqa: E402
from app.services.runtime_preferences_store import StoredRuntimePreferences  # noqa: E402


EXPECTED_CATEGORY_COUNTS = {
    "state_update": 12,
    "task_recovery": 8,
    "compaction": 8,
    "long_term_retrieval": 8,
    "scope_isolation": 6,
    "privacy": 6,
}
GATE_PROFILE_CATEGORIES = {
    # 全量门禁保留全部 required，用于 MEM-7 前的最终验收；阶段门禁只选择当前阶段已承诺
    # 修复的能力与既有防回归项，不能把 MEM-2 的未实施状态模型误报成 MEM-1 实现失败。
    "all": None,
    "mem1": {"baseline_probe", "compaction", "long_term_retrieval", "scope_isolation", "privacy"},
    "mem2": {
        "baseline_probe",
        "state_update",
        "task_recovery",
        "compaction",
        "long_term_retrieval",
        "scope_isolation",
        "privacy",
    },
    # MEM-3 adds a separate ContextEnvelope verifier while retaining the complete MEM-2
    # data fixture as its regression floor.
    "mem3": {
        "baseline_probe",
        "state_update",
        "task_recovery",
        "compaction",
        "long_term_retrieval",
        "scope_isolation",
        "privacy",
    },
    # MEM-4 has a dedicated lifecycle verifier and keeps MEM-3's full fixture regression floor.
    "mem4": {
        "baseline_probe",
        "state_update",
        "task_recovery",
        "compaction",
        "long_term_retrieval",
        "scope_isolation",
        "privacy",
    },
    # MEM-5 has a dedicated FTS5/BM25 and Hybrid-admission verifier while retaining the same
    # complete memory, scope, privacy and recovery regression floor as MEM-4.
    "mem5": {
        "baseline_probe",
        "state_update",
        "task_recovery",
        "compaction",
        "long_term_retrieval",
        "scope_isolation",
        "privacy",
    },
}
SYNTHETIC_INPUTS = {
    "synthetic_secret": "模拟凭据 sk-fixtureonly00000000，不代表真实密钥。",
    "synthetic_path": r"请读取 X:\synthetic_fixture\sample.txt，不代表真实本地文件。",
}


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    category: str
    quality_gate: str
    status: str
    duration_ms: float
    details: dict[str, Any]


def _load_cases() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if payload.get("version") != "agentflow.memory_eval.v1":
        raise ValueError("记忆评测夹具版本不匹配。")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("记忆评测夹具缺少 cases 数组。")
    probes = payload.get("baseline_probes")
    if not isinstance(probes, list):
        raise ValueError("记忆评测夹具缺少 baseline_probes 数组。")
    _validate_cases(cases)
    _validate_probes(probes)
    return cases, probes


def _validate_cases(cases: list[dict[str, Any]]) -> None:
    ids = [str(case.get("id") or "") for case in cases]
    if len(cases) != 48 or len(set(ids)) != len(ids) or any(not value for value in ids):
        raise ValueError("记忆评测夹具必须恰好包含 48 条唯一用例。")
    actual_counts = Counter(str(case.get("category") or "") for case in cases)
    if dict(actual_counts) != EXPECTED_CATEGORY_COUNTS:
        raise ValueError(f"记忆评测分类数量不符合 MEM-0 契约：{dict(actual_counts)}")
    invalid_gates = [case["id"] for case in cases if case.get("quality_gate") not in {"required", "diagnostic"}]
    if invalid_gates:
        raise ValueError(f"记忆评测存在无效质量门禁：{invalid_gates}")


def _validate_probes(probes: list[dict[str, Any]]) -> None:
    probe_ids = [str(probe.get("id") or "") for probe in probes]
    expected_ids = {
        "probe_intent_uses_latest_conversation_tail",
        "probe_memory_usage_waits_for_success",
        "probe_model_failure_does_not_mark_memory",
    }
    if set(probe_ids) != expected_ids or len(probe_ids) != len(expected_ids):
        raise ValueError("记忆评测的 MEM-1 确定性探针不完整。")
    if any(probe.get("quality_gate") != "required" for probe in probes):
        raise ValueError("MEM-1 确定性探针必须标记为 required。")


def _result(case: dict[str, Any], status: str, started: float, **details: Any) -> EvaluationResult:
    return EvaluationResult(
        case_id=str(case["id"]),
        category=str(case.get("category") or "baseline_probe"),
        quality_gate=str(case["quality_gate"]),
        status=status,
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
        details=details,
    )


def _run_case(case: dict[str, Any]) -> EvaluationResult:
    started = time.perf_counter()
    try:
        operation = str(case["operation"])
        if operation == "working_state":
            return _run_working_state_case(case, started)
        if operation == "workflow_projection":
            return _run_workflow_projection_case(case, started)
        if operation == "conversation_compaction":
            return _run_compaction_case(case, started)
        if operation == "memory_search":
            return _run_memory_search_case(case, started)
        if operation == "conversation_scope_switch":
            return _run_conversation_scope_switch_case(case, started)
        if operation == "transcript_scope_guard":
            return _run_transcript_scope_guard_case(case, started)
        if operation == "conversation_sanitize":
            return _run_conversation_sanitize_case(case, started)
        if operation == "memory_sanitize_reject":
            return _run_memory_sanitize_case(case, started)
        if operation == "memory_disabled_no_read":
            return _run_memory_disabled_case(case, started)
        if operation == "conversation_archive_bound":
            return _run_archive_bound_case(case, started)
        return _result(case, "failed", started, reason=f"未知评测操作：{operation}")
    except Exception as exc:  # 基线需要保留失败，而不能让单一夹具中断整份报告。
        return _result(case, "failed", started, reason=f"{type(exc).__name__}: {exc}")


def _run_probe(probe: dict[str, Any]) -> EvaluationResult:
    started = time.perf_counter()
    try:
        operation = str(probe["operation"])
        if operation == "intent_context_tail":
            return _run_intent_context_tail_probe(probe, started)
        if operation == "memory_usage_after_success":
            return _run_memory_usage_after_success_probe(probe, started)
        if operation == "model_failure_usage":
            return _run_model_failure_usage_probe(probe, started)
        return _result(probe, "failed", started, reason=f"未知基线探针：{operation}")
    except Exception as exc:
        return _result(probe, "failed", started, reason=f"{type(exc).__name__}: {exc}")


def _run_working_state_case(case: dict[str, Any], started: float) -> EvaluationResult:
    state = _empty_working_state(case_id=str(case["id"]))
    for index, message in enumerate(case.get("messages", []), start=1):
        # 夹具明确标注“助手说”的文本用于验证安全边界：它不是用户事件，不得更新工作事实。
        if str(message).startswith("助手说"):
            continue
        state = reduce_user_message(
            state,
            message=str(message),
            source_id=f"task_eval_state_{index}",
        )
    event = case.get("event")
    if isinstance(event, dict):
        state = _project_fixture_event(state, event)
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise ValueError("working_state 用例缺少 expected 对象。")
    matches, mismatches = _working_state_matches(state, expected)
    return _result(
        case,
        "passed" if matches else "failed",
        started,
        revision=state.revision,
        state=state.model_dump(mode="json"),
        mismatches=mismatches,
    )


def _run_workflow_projection_case(case: dict[str, Any], started: float) -> EvaluationResult:
    event = case.get("event")
    if not isinstance(event, dict):
        raise ValueError("workflow_projection 用例缺少 event 对象。")
    initial = _empty_working_state(case_id=str(case["id"]))
    state = _project_fixture_event(initial, event)
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise ValueError("workflow_projection 用例缺少 expected 对象。")
    matches, mismatches = _working_state_matches(state, expected)

    if "revision_delta" in expected:
        duplicate = _project_fixture_event(state, event)
        delta = duplicate.revision - initial.revision
        if delta != int(expected["revision_delta"]):
            matches = False
            mismatches.append(f"revision_delta={delta!r}")
        state = duplicate
    if expected.get("restart_snapshot_equal") is True:
        restored = ConversationWorkingState.model_validate_json(state.model_dump_json())
        same = restored == state
        if not same:
            matches = False
            mismatches.append("restart_snapshot_equal=false")
    return _result(
        case,
        "passed" if matches else "failed",
        started,
        revision=state.revision,
        state=state.model_dump(mode="json"),
        mismatches=mismatches,
    )


def _empty_working_state(*, case_id: str) -> ConversationWorkingState:
    return ConversationWorkingState(
        conversation_id=f"conv_eval_{case_id}"[:64],
        project_scope="project:eval_working_state",
        updated_at="2026-09-10T00:00:00Z",
    )


def _project_fixture_event(state: ConversationWorkingState, event: dict[str, Any]) -> ConversationWorkingState:
    return project_workflow_event(
        state,
        task_id=str(event["task_id"]),
        status=str(event["status"]),
        current_step=str(event.get("step") or ""),
        step_index=int(event["step_index"]) if event.get("step_index") is not None else None,
        task_title=str(event.get("task_title") or "评测任务"),
        result_summary=str(event.get("summary") or "已通过合成回读验证。"),
        artifact_ids=[str(value) for value in event.get("artifact_ids", [])],
        resume_checkpoint=str(event.get("resumed_from") or ""),
        event_id=str(event.get("event_id") or ""),
    )


def _working_state_matches(state: ConversationWorkingState, expected: dict[str, Any]) -> tuple[bool, list[str]]:
    mismatches: list[str] = []
    if "current_goal" in expected:
        actual = state.current_goal.value if state.current_goal is not None else None
        if actual != expected["current_goal"]:
            mismatches.append(f"current_goal={actual!r}")
    for section_name, values in (("constraints", state.constraints), ("decisions", state.decisions)):
        section_expected = expected.get(section_name)
        if not isinstance(section_expected, dict):
            continue
        for key, expected_value in section_expected.items():
            actual_value = values.get(str(key))
            actual = actual_value.value if actual_value is not None else None
            if actual != expected_value:
                mismatches.append(f"{section_name}.{key}={actual!r}")
    if "pending_confirmation" in expected:
        actual_pending = state.pending_confirmation
        expected_pending = [str(value) for value in expected["pending_confirmation"]]
        if actual_pending != expected_pending:
            mismatches.append(f"pending_confirmation={actual_pending!r}")
    if "open_items" in expected:
        for partial in expected["open_items"]:
            if not isinstance(partial, dict):
                mismatches.append("open_items 包含非对象期望值")
                continue
            if not any(all(getattr(item, key, None) == value for key, value in partial.items()) for item in state.open_items):
                mismatches.append(f"open_item_missing={partial!r}")
    if "active_task" in expected:
        actual_task = state.active_task
        for key, value in expected["active_task"].items():
            if actual_task is None or getattr(actual_task, key, None) != value:
                actual = getattr(actual_task, key, None) if actual_task is not None else None
                mismatches.append(f"active_task.{key}={actual!r}")
    if "latest_verified_result" in expected:
        actual_result = state.latest_verified_result
        for key, value in expected["latest_verified_result"].items():
            if actual_result is None or getattr(actual_result, key, None) != value:
                actual = getattr(actual_result, key, None) if actual_result is not None else None
                mismatches.append(f"latest_verified_result.{key}={actual!r}")
    return not mismatches, mismatches


def _run_intent_context_tail_probe(probe: dict[str, Any], started: float) -> EvaluationResult:
    latest_marker = "[LATEST_EVAL_REQUIREMENT] 最终需求是生成可编辑表格。"

    class CapturingRuntime:
        def __init__(self) -> None:
            self.payload: dict[str, Any] = {}

        async def chat_json(self, *, system_prompt: str, user_message: str, maximum_tokens: int) -> str:
            del system_prompt, maximum_tokens
            self.payload = json.loads(user_message)
            return (
                '{"version":"agentflow.commander_intent.v1","intent":"presentation",'
                '"is_follow_up":true,"delivery":"presentation","preferred_agents":[], '
                '"required_material_kinds":[],"confidence":0.9,"clarifying_question":""}'
            )

    runtime = CapturingRuntime()
    long_context = ("早期合成上下文。" * 320) + latest_marker
    asyncio.run(
        resolve_commander_intent_candidate(
            runtime=runtime,  # type: ignore[arg-type]
            message="按刚才最终要求继续。",
            conversation_context=long_context,
            agents=[],
            materials=[],
            agent_hints=[],
        )
    )
    tail_present = latest_marker in str(runtime.payload.get("conversation_context") or "")
    return _result(
        probe,
        "passed" if tail_present else "failed",
        started,
        latest_marker_in_payload=tail_present,
        payload_context_characters=len(str(runtime.payload.get("conversation_context") or "")),
    )


def _run_memory_usage_after_success_probe(probe: dict[str, Any], started: float) -> EvaluationResult:
    with get_connection() as connection:
        connection.execute("DELETE FROM long_term_memories")
    record = create_long_term_memory(
        kind="project_constraint",
        scope="project:eval_usage",
        title="合成使用时间约束",
        summary="只用于验证成功路径前不能更新使用时间。",
        tags=["使用时间"],
        source_task_id=None,
        user_confirmed=True,
    )
    retrieve_commander_memory_context(
        user_goal="使用时间",
        preferences=WorkflowPlanPreferences(memory_enabled=True),
        project_scope="project:eval_usage",
    )
    used_after_retrieval = get_long_term_memory(record.memory_id).last_used_at
    unchanged = not used_after_retrieval
    return _result(
        probe,
        "passed" if unchanged else "failed",
        started,
        last_used_at_unchanged_before_success=unchanged,
    )


def _run_model_failure_usage_probe(probe: dict[str, Any], started: float) -> EvaluationResult:
    with get_connection() as connection:
        connection.execute("DELETE FROM long_term_memories")
    record = create_long_term_memory(
        kind="project_constraint",
        scope="project:eval_failure",
        title="合成失败路径约束",
        summary="失败路径也不能提前更新使用时间。",
        tags=["失败路径"],
        source_task_id=None,
        user_confirmed=True,
    )
    commander = get_agent("commander_agent")
    if commander is None:
        raise RuntimeError("离线评测未找到 Commander Agent。")

    class FailingRuntime:
        model = "synthetic-failure-model"

        async def chat(self, *, system_prompt: str, user_message: str) -> str:
            del system_prompt, user_message
            raise ModelGatewayError("synthetic model failure")

    failed_as_expected = False
    with (
        patch(
            "app.services.llm_chat.load_runtime_preferences",
            return_value=StoredRuntimePreferences(memory_enabled=True),
        ),
        patch(
            "app.services.llm_chat.resolve_model_runtime_for_route",
            return_value=SimpleNamespace(runtime=FailingRuntime()),
        ),
        patch("app.services.llm_chat.should_resolve_commander_intent", return_value=False),
    ):
        try:
            asyncio.run(
                create_llm_chat_response(
                    request=ChatRequest(
                        message="请按失败路径约束处理。",
                        project_scope="project:eval_failure",
                    ),
                    agent=commander,
                    message="请按失败路径约束处理。",
                )
            )
        except LlmChatError:
            failed_as_expected = True
    unchanged = not get_long_term_memory(record.memory_id).last_used_at
    passed = failed_as_expected and unchanged
    return _result(
        probe,
        "passed" if passed else "failed",
        started,
        model_failure_observed=failed_as_expected,
        last_used_at_unchanged_after_model_failure=unchanged,
    )


def _run_compaction_case(case: dict[str, Any], started: float) -> EvaluationResult:
    prepared = prepare_conversation(
        conversation_id=None,
        project_scope="project:eval_compaction",
        message="建立脱敏评测会话",
        supplied_materials=[],
    )
    conversation_id = prepared.context.session.conversation_id
    signal_index = int(case["signal_index"])
    signal_role = str(case["signal_role"])
    signal_text = str(case["signal_text"])
    expected_last_task_id = str(case.get("expected_last_task_id") or "")

    for index in range(11):
        task_id = expected_last_task_id if index == 10 and expected_last_task_id else f"task_eval_compaction_{index + 1:02d}"
        user_message = f"第 {index + 1} 轮合成用户输入。"
        assistant_message = f"第 {index + 1} 轮合成助手答复。"
        if index == signal_index:
            if signal_role == "user":
                user_message = signal_text
            else:
                assistant_message = signal_text
        save_conversation_turn(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
            material_bindings=[],
            task_id=task_id,
            plan_id=f"plan_eval_compaction_{index + 1:02d}",
        )

    context = get_conversation_context(conversation_id)
    summary = context.session.summary
    recent_text = "\n".join(item.content for item in context.recent_messages)
    missing_summary = [value for value in case.get("expected_summary", []) if value not in summary]
    missing_recent = [value for value in case.get("expected_recent", []) if value not in recent_text]
    pointer_matches = not expected_last_task_id or context.session.last_task_id == expected_last_task_id
    token_reported = not case.get("expect_token_estimate") or context.estimated_memory_tokens > 0
    passed = not missing_summary and not missing_recent and pointer_matches and token_reported
    return _result(
        case,
        "passed" if passed else "failed",
        started,
        summarized_message_count=context.summarized_message_count,
        recent_message_count=len(context.recent_messages),
        estimated_memory_tokens=context.estimated_memory_tokens,
        missing_summary_markers=missing_summary,
        missing_recent_markers=missing_recent,
        last_task_id_matches=pointer_matches,
    )


def _run_memory_search_case(case: dict[str, Any], started: float) -> EvaluationResult:
    # 每个检索用例拥有独立的合成记忆库。否则前一例的 500 条噪声会掩盖后一例，报告无法定位
    # 到真正的回归原因；该表位于本脚本的临时 SQLite，不会影响开发数据库。
    with get_connection() as connection:
        connection.execute("DELETE FROM long_term_memories")
    record_keys = _seed_memory_records(case)
    records = search_long_term_memories(
        query=str(case["query"]),
        scopes={str(scope) for scope in case["scopes"]},
        limit=3,
    )
    by_id = {record_id: key for key, record_id in record_keys.items()}
    returned_keys = [by_id.get(record.memory_id, "external") for record in records]
    expected_keys = {str(value) for value in case.get("expected_keys", [])}
    forbidden_keys = {str(value) for value in case.get("forbidden_keys", [])}
    missing = sorted(expected_keys.difference(returned_keys))
    forbidden = sorted(forbidden_keys.intersection(returned_keys))
    passed = not missing and not forbidden
    return _result(
        case,
        "passed" if passed else "failed",
        started,
        returned_keys=returned_keys,
        expected_keys=sorted(expected_keys),
        missing_expected_keys=missing,
        forbidden_returned_keys=forbidden,
        noise_count=int(case.get("noise_count") or 0),
    )


def _seed_memory_records(case: dict[str, Any]) -> dict[str, str]:
    record_keys: dict[str, str] = {}
    for item in case.get("records", []):
        record = create_long_term_memory(
            kind=str(item["kind"]),
            scope=str(item["scope"]),
            title=str(item["title"]),
            summary=str(item["summary"]),
            tags=[str(tag) for tag in item.get("tags", [])],
            source_task_id=None,
            user_confirmed=bool(item.get("user_confirmed", True)),
        )
        if not bool(item.get("enabled", True)):
            with get_connection() as connection:
                connection.execute("UPDATE long_term_memories SET enabled = 0 WHERE memory_id = ?", (record.memory_id,))
        record_keys[str(item["key"])] = record.memory_id

    noise_count = int(case.get("noise_count") or 0)
    if noise_count:
        _seed_cross_scope_noise(case_id=str(case["id"]), count=noise_count)
        # 固定排序：当前相关记录比 500 条其它项目噪声更旧，稳定复现“先 LIMIT 后筛 scope”的历史缺陷。
        with get_connection() as connection:
            connection.execute(
                "UPDATE long_term_memories SET updated_at = '2001-01-01T00:00:00Z' WHERE memory_id IN ({})".format(
                    ",".join("?" for _ in record_keys)
                ),
                list(record_keys.values()),
            )
    return record_keys


def _seed_cross_scope_noise(*, case_id: str, count: int) -> None:
    rows = [
        (
            f"memory_noise_{case_id}_{index:04d}",
            "project_constraint",
            f"project:noise_{case_id}_{index:04d}",
            "其它项目噪声",
            "与当前评测范围无关的合成记忆。",
            "[]",
            "",
            1,
            1,
            "2099-01-01T00:00:00Z",
            "2099-01-01T00:00:00Z",
            "",
        )
        for index in range(count)
    ]
    with get_connection() as connection:
        connection.executemany(
            """
            INSERT INTO long_term_memories (
                memory_id, kind, scope, title, summary, tags_json, source_task_id,
                user_confirmed, enabled, created_at, updated_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _run_conversation_scope_switch_case(case: dict[str, Any], started: float) -> EvaluationResult:
    initial = prepare_conversation(
        conversation_id=None,
        project_scope="project:eval_alpha",
        message="建立项目 Alpha 会话",
        supplied_materials=[],
    )
    switched = prepare_conversation(
        conversation_id=initial.context.session.conversation_id,
        project_scope="project:eval_beta",
        message="切换项目 Beta",
        supplied_materials=[],
    )
    passed = (
        switched.context.session.conversation_id != initial.context.session.conversation_id
        and switched.context.session.project_scope == "project:eval_beta"
    )
    return _result(case, "passed" if passed else "failed", started, switched_to_new_session=passed)


def _run_transcript_scope_guard_case(case: dict[str, Any], started: float) -> EvaluationResult:
    prepared = prepare_conversation(
        conversation_id=None,
        project_scope="project:eval_alpha",
        message="建立项目 Alpha 会话",
        supplied_materials=[],
    )
    rejected = False
    try:
        get_conversation_transcript(
            conversation_id=prepared.context.session.conversation_id,
            project_scope="project:eval_beta",
        )
    except LookupError:
        rejected = True
    return _result(case, "passed" if rejected else "failed", started, cross_scope_read_rejected=rejected)


def _run_conversation_sanitize_case(case: dict[str, Any], started: float) -> EvaluationResult:
    input_value = SYNTHETIC_INPUTS[str(case["input_kind"])]
    sanitized = sanitize_conversation_text(input_value, maximum=CONVERSATION_ARCHIVE_MESSAGE_MAX_CHARS)
    expected = str(case["expected"]["contains"])
    return _result(
        case,
        "passed" if expected in sanitized else "failed",
        started,
        expected_marker_present=expected in sanitized,
    )


def _run_memory_sanitize_case(case: dict[str, Any], started: float) -> EvaluationResult:
    rejected = False
    try:
        sanitize_memory_text(
            SYNTHETIC_INPUTS[str(case["input_kind"])],
            field_name="合成评测字段",
            maximum=1000,
        )
    except LongTermMemorySafetyError:
        rejected = True
    return _result(case, "passed" if rejected else "failed", started, rejected=rejected)


def _run_memory_disabled_case(case: dict[str, Any], started: float) -> EvaluationResult:
    preferences = WorkflowPlanPreferences(memory_enabled=False)
    # Patch the concrete dependency used by the runtime path. This keeps the
    # privacy gate meaningful when retrieval implementation details evolve.
    with patch(
        "app.services.commander_memory.search_long_term_memory_retrieval",
        side_effect=AssertionError("不应读取长期记忆"),
    ):
        records = retrieve_commander_memory_context(
            user_goal="合成评测请求",
            preferences=preferences,
            project_scope="project:eval_alpha",
        )
    return _result(case, "passed" if records == [] else "failed", started, returned_record_count=len(records))


def _run_archive_bound_case(case: dict[str, Any], started: float) -> EvaluationResult:
    value = sanitize_conversation_text("x" * (CONVERSATION_ARCHIVE_MESSAGE_MAX_CHARS + 32), maximum=CONVERSATION_ARCHIVE_MESSAGE_MAX_CHARS)
    expected_maximum = int(case["expected"]["maximum_characters"])
    return _result(
        case,
        "passed" if len(value) == expected_maximum else "failed",
        started,
        archived_characters=len(value),
    )


def _build_report(results: list[EvaluationResult], probes: list[EvaluationResult]) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    for item in results:
        by_category[item.category][item.status] += 1
    all_results = [*results, *probes]
    required = [item for item in all_results if item.quality_gate == "required"]
    retrieval = [item for item in results if item.category == "long_term_retrieval" and item.details.get("expected_keys")]
    retrieval_hits = [item for item in retrieval if item.status == "passed"]
    durations = [item.duration_ms for item in results]
    report = {
        "version": "agentflow.memory_eval.report.v1",
        "fixture": str(FIXTURE_PATH.relative_to(BACKEND_ROOT)).replace("\\", "/"),
        "case_count": len(results),
        "probe_count": len(probes),
        "result_counts": dict(Counter(item.status for item in all_results)),
        "required_result_counts": dict(Counter(item.status for item in required)),
        "category_result_counts": {category: dict(counts) for category, counts in sorted(by_category.items())},
        "metrics": {
            "state_field_accuracy": _ratio([item for item in results if item.category == "state_update"]),
            "task_recovery_consistency": _ratio([item for item in results if item.category == "task_recovery"]),
            "retrieval_recall_at_3": round(len(retrieval_hits) / len(retrieval), 4) if retrieval else None,
            "cross_scope_leak_count": sum(
                len(item.details.get("forbidden_returned_keys", []))
                for item in results
                if item.category in {"long_term_retrieval", "scope_isolation"}
            ),
            "context_token_estimate_average": round(
                mean(
                    item.details["estimated_memory_tokens"]
                    for item in results
                    if "estimated_memory_tokens" in item.details
                ),
                2,
            ),
            "retrieval_latency_p95_ms": _percentile(
                [item.duration_ms for item in results if item.category in {"long_term_retrieval", "scope_isolation"}],
                0.95,
            ),
            "all_case_latency_p95_ms": _percentile(durations, 0.95),
        },
        "required_failures": [
            {"case_id": item.case_id, "status": item.status, "details": item.details}
            for item in required
            if item.status != "passed"
        ],
        "results": [asdict(item) for item in results],
        "baseline_probes": [asdict(item) for item in probes],
    }
    return report


def _ratio(items: list[EvaluationResult]) -> float | None:
    if not items:
        return None
    return round(sum(item.status == "passed" for item in items) / len(items), 4)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 AgentFlow 记忆系统离线质量评测。")
    parser.add_argument("--mode", choices=("baseline", "gate"), default="baseline")
    parser.add_argument("--gate-profile", choices=tuple(GATE_PROFILE_CATEGORIES), default="all")
    args = parser.parse_args()

    cases, probe_cases = _load_cases()
    results = [_run_case(case) for case in cases]
    probes = [_run_probe(probe) for probe in probe_cases]
    report = _build_report(results, probes)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    gate_categories = GATE_PROFILE_CATEGORIES[args.gate_profile]
    required_unpassed = [
        item
        for item in [*results, *probes]
        if item.quality_gate == "required"
        and item.status != "passed"
        and (gate_categories is None or item.category in gate_categories)
    ]
    if args.mode == "gate" and required_unpassed:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
