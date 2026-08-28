"""知识库 K4.2 Map 检查点与恢复的离线回归。

本脚本走真实工作区导入、generation、SQLite 任务历史和 AgentRunner，但使用固定假模型，
不读取客户模型配置、不下载模型，也不发送网络请求。验证重点是章节隔离、失败后恢复和索引
变更拒绝，而不是模型语言质量。
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import quote
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_deep_map_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
os.environ["AGENTFLOW_DATABASE_PATH"] = str(VERIFY_ROOT / "data" / "knowledge_deep_map.db")
os.environ["AGENTFLOW_KNOWLEDGE_REPORT_OUTPUT_DIR"] = str(VERIFY_ROOT / "knowledge_reports")
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import sqlite as sqlite_service
from app.database.knowledge_repository import create_knowledge_base, import_workspace_documents_to_knowledge_base
from app.database.task_repository import load_task_log_events, load_workflow_run
from app.agents.runner import AgentModelUsageSummary, AgentRunResult
from app.schemas.knowledge import (
    KnowledgeDeepMapDraft,
    KnowledgeDeepReduceDraft,
    KnowledgeDeepTaskReportExportRequest,
    KnowledgeDeepTaskRequest,
)
from app.services.knowledge_deep_task import (
    _DEEP_TASK_RATE_LIMITS_RPM,
    _DEEP_TASK_REQUEST_TIMES,
    _deep_task_rate_limit_key,
    _remember_deep_task_rate_limit,
    _reduce_plan,
    _with_provider_usage_metrics,
    _wait_for_deep_task_model_slot,
    build_knowledge_deep_task_scope,
    create_knowledge_deep_task_map_queued_run,
    get_knowledge_deep_task_result,
    get_knowledge_deep_task_scope,
    mark_knowledge_deep_task_unexpected_failure,
    request_knowledge_deep_task_cancel,
    request_knowledge_deep_task_pause,
    resume_knowledge_deep_task,
    run_knowledge_deep_task,
    run_knowledge_deep_task_map,
    run_knowledge_deep_task_reduce,
)
from app.services.knowledge_keyword_index import create_knowledge_index_job, run_knowledge_index_job
from app.services.knowledge_deep_report import (
    KnowledgeDeepTaskReportConfirmationError,
    KnowledgeDeepTaskReportNotReadyError,
    export_knowledge_deep_task_report,
)
from app.services.model_gateway import (
    ModelConversationMessage,
    ModelGatewayError,
    ModelRuntime,
    ModelToolDefinition,
    ModelToolTurn,
    ModelUsageMetrics,
)
from app.services.workspace_documents import import_workspace_document


class _MapFixtureModel:
    """从 Runner 输入中读取当前唯一 map_unit，模拟成功或持续非法 JSON。"""

    def __init__(
        self,
        *,
        failing_map_unit_id: str = "",
        fail_reduce: bool = False,
        on_map_turn=None,
        transient_map_request_failures: int = 0,
        transient_reduce_request_failures: int = 0,
    ) -> None:
        self.failing_map_unit_id = failing_map_unit_id
        self.fail_reduce = fail_reduce
        self.on_map_turn = on_map_turn
        self.transient_map_request_failures = transient_map_request_failures
        self.transient_reduce_request_failures = transient_reduce_request_failures
        self.turn_count = 0
        self.reduce_turn_count = 0
        self.received_chapters: list[str] = []

    async def tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        self.turn_count += 1
        assert not tools
        if "Reduce 分析器" in system_prompt:
            self.reduce_turn_count += 1
            if self.transient_reduce_request_failures > 0:
                self.transient_reduce_request_failures -= 1
                raise ModelGatewayError("模型接口返回 HTTP 503（temporary service overload）。")
            payload = _reduce_payload(messages)
            input_scope = payload["input_scope"]
            assert isinstance(input_scope, dict)
            assert int(input_scope["chapter_count"]) >= 1
            assert "chapter_content" not in json.dumps(payload, ensure_ascii=False)
            if self.fail_reduce:
                return ModelToolTurn(content="不是 JSON")
            result = {
                "overview": "已基于受控章节小结完成归纳，未读取原始正文。",
                # 真实模型只需要表达语义。来源范围与稳定编号由 Runtime 从冻结 checkpoint 投影，
                # 不再要求 Provider 复制 map_unit_id 或 source_ids。
                "findings": ["章节材料共同说明交付与风险约束需要纳入后续报告。"],
                "conflicts": (
                    [
                        {
                            "topic": "责任分工",
                            "description": "两份章节对责任边界存在不同表述，保留给后续人工确认。",
                        }
                    ]
                    if int(input_scope["chapter_count"]) >= 2
                    else []
                ),
                "warnings": [],
            }
            return ModelToolTurn(content=json.dumps(result, ensure_ascii=False))
        # Runtime 只把当前冻结章节交给 Map 回合；夹具只检查这个不可跨章节的安全语义。
        assert "单个章节" in system_prompt
        if self.transient_map_request_failures > 0:
            self.transient_map_request_failures -= 1
            raise ModelGatewayError("模型接口返回 HTTP 503（temporary service overload）。")
        if self.on_map_turn is not None:
            self.on_map_turn()
        payload = _map_payload(messages)
        map_unit = payload["map_unit"]
        chapter_content = str(payload["chapter_content"])
        self.received_chapters.append(chapter_content)
        # 当前章节的动态 ID 只放在受控用户输入中；系统提示词不再要求模型复制它。
        assert str(map_unit["map_unit_id"]).startswith("kb_map_")
        if map_unit["map_unit_id"] == self.failing_map_unit_id:
            return ModelToolTurn(content="不是 JSON")
        # Map 模型只需给出语义内容。来源、章节 ID 和发现编号由 Runtime 从冻结范围补齐，避免
        # Provider 因复制动态 ID 或旧对象 schema 失败而卡住真实长任务。
        result = {
            "summary": f"{map_unit['document_name']} 的章节内容已完成受控小结。",
            "findings": ["章节包含可供后续汇总的明确约束。"],
            "warnings": [],
        }
        return ModelToolTurn(content=json.dumps(result, ensure_ascii=False))


def _map_payload(messages: list[ModelConversationMessage]) -> dict[str, object]:
    """忽略 Runner 的格式修复提示，找到本次唯一可读章节。"""

    for message in messages:
        if message.role != "user":
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload.get("map_unit"), dict) and "chapter_content" in payload:
            return payload
    raise AssertionError("Map 假模型没有收到受控章节输入。")


def _verify_full_scope_reduce_plan(scope) -> None:
    """完整范围不能再裁剪为 24 章，分层计划必须保持每个模型节点的有限扇出。"""

    map_units = [scope.map_units[index % len(scope.map_units)] for index in range(389)]
    # 该纯内存 scope 只验证计划树，不会被送入 Map 回读；通过不同稳定 ID 避免触发范围重复校验。
    expanded_scope = scope.model_copy(update={"map_units": map_units})
    # ``model_copy`` 不重跑 Pydantic 校验，这里只关心 Reduce 的确定性分组与最终输入规模。
    plan = _reduce_plan(expanded_scope)
    assert len(plan) > 5
    assert plan[-1].is_final
    assert len(plan[-1].input_step_ids) <= 6
    assert all(len(node.input_step_ids) <= 6 for node in plan)


def _verify_semantic_output_compatibility() -> None:
    """不同 Provider 的旧字段或来源对象不能再让 Map/Reduce 无谓停驻。"""

    map_draft = KnowledgeDeepMapDraft.model_validate(
        {
            "overview": "当前章节给出了交付约束。",
            "points": [{"statement": "验收范围需要在交付前固定。", "source_ids": ["ignored"]}],
            "notes": "章节没有列出责任人。",
        }
    )
    assert map_draft.summary == "当前章节给出了交付约束。"
    assert map_draft.findings == ["验收范围需要在交付前固定。"]
    assert map_draft.warnings == ["章节没有列出责任人。"]

    reduce_draft = KnowledgeDeepReduceDraft.model_validate(
        {
            # 故意不提供 overview：Runtime 只能从模型已提供的发现文本生成概述，不能补写事实。
            "key_findings": [{"statement": "交付与风险复核都应进入最终报告。", "source_ids": ["ignored"]}],
            "open_questions": [
                {"title": "责任边界", "statement": "不同章节尚未统一责任划分。", "source_ids": ["ignored"]}
            ],
        }
    )
    assert reduce_draft.overview == "交付与风险复核都应进入最终报告。"
    assert reduce_draft.findings == ["交付与风险复核都应进入最终报告。"]
    assert reduce_draft.conflicts[0].topic == "责任边界"
    assert reduce_draft.conflicts[0].description == "不同章节尚未统一责任划分。"


def _reduce_payload(messages: list[ModelConversationMessage]) -> dict[str, object]:
    """找到 Reduce 的受控小结输入；格式修复消息不能扩大来源范围。"""

    for message in messages:
        if message.role != "user":
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload.get("input_scope"), dict) and (
            "map_summaries" in payload or "batch_summaries" in payload or "reduce_summaries" in payload
        ):
            return payload
    raise AssertionError("Reduce 假模型没有收到受控小结输入。")


def _index_active_materials(*, knowledge_base_id: str, document_names: list[str]) -> None:
    import_workspace_documents_to_knowledge_base(
        knowledge_base_id=knowledge_base_id,
        workspace_document_names=document_names,
    )
    completed = run_knowledge_index_job(create_knowledge_index_job(knowledge_base_id).index_job_id)
    assert completed.status == "completed", completed.failure_summaries


def _verify_provider_rate_limit_queue(*, task_id: str) -> None:
    """供应商已经声明 RPM 时，K4 应等待安全窗口，而不是让客户手动连续恢复。"""

    runtime = ModelRuntime(
        provider="kimi",
        label="Kimi verification runtime",
        transport="openai_compatible",
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.6",
        api_key="verification-rate-key-only",
        thinking="disabled",
        max_tokens=512,
        temperature=0.0,
        timeout_seconds=30.0,
    )
    rate_failure = AgentRunResult(
        status="failed",
        stop_reason="model_request_failed",
        output=None,
        tool_traces=(),
        turn_traces=(),
        message="模型接口返回 HTTP 429（rate_limit · max RPM: 3, please try again after 1 seconds）。",
    )
    rate_key = _deep_task_rate_limit_key(runtime)
    assert rate_key is not None
    _DEEP_TASK_RATE_LIMITS_RPM.clear()
    _DEEP_TASK_REQUEST_TIMES.clear()
    try:
        assert _remember_deep_task_rate_limit(runtime, rate_failure) == 1.2
        assert _DEEP_TASK_RATE_LIMITS_RPM[rate_key] == 3
        # 模拟同一账号最近一分钟已经发出三次请求；冻结单调时钟让测试不真的等待十秒。
        _DEEP_TASK_REQUEST_TIMES[rate_key] = deque([50.0, 60.0, 70.0])
        clock = [100.0]
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)
            clock[0] += seconds

        with patch("app.services.knowledge_deep_task.monotonic", lambda: clock[0]), patch(
            "app.services.knowledge_deep_task.asyncio.sleep", fake_sleep
        ):
            asyncio.run(
                _wait_for_deep_task_model_slot(
                    model=runtime,
                    task_id=task_id,
                    progress_callback=None,
                    step_id="rate_limit_fixture",
                    stage_label="Map 章节分析",
                )
            )
        assert delays == [10.25]
        assert len(_DEEP_TASK_REQUEST_TIMES[rate_key]) == 3
    finally:
        _DEEP_TASK_RATE_LIMITS_RPM.clear()
        _DEEP_TASK_REQUEST_TIMES.clear()


def _verify_provider_usage_metrics(*, run) -> None:
    """K5.3 只累计真实 ModelRuntime 的 usage，未知字段和 mock 都不能变成虚假 0。"""

    runtime = ModelRuntime(
        provider="deepseek",
        label="DeepSeek verification runtime",
        transport="openai_compatible",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        api_key="verification-usage-key-only",
        thinking="disabled",
        max_tokens=512,
        temperature=0.0,
        timeout_seconds=30.0,
    )
    first = AgentModelUsageSummary(
        request_total=2,
        usage_reported_request_total=1,
        cache_observed_request_total=1,
        input_tokens=120,
        output_tokens=15,
        total_tokens=135,
        cache_read_input_tokens=96,
        cache_miss_input_tokens=24,
    )
    updated = _with_provider_usage_metrics(run, model=runtime, usage=first)
    assert updated.metrics.provider_model_request_total == 2
    assert updated.metrics.provider_usage_reported_request_total == 1
    assert updated.metrics.provider_cache_observed_request_total == 1
    assert updated.metrics.provider_input_tokens == 120
    assert updated.metrics.provider_cache_read_input_tokens == 96
    assert updated.metrics.provider_cache_miss_input_tokens == 24

    # 第二批响应只回传普通 usage：总 token 可累计，缓存细目保持上一次真实观测，不能清零。
    second = AgentModelUsageSummary(
        request_total=1,
        usage_reported_request_total=1,
        output_tokens=8,
        total_tokens=28,
    )
    updated = _with_provider_usage_metrics(updated, model=runtime, usage=second)
    assert updated.metrics.provider_model_request_total == 3
    assert updated.metrics.provider_usage_reported_request_total == 2
    assert updated.metrics.provider_cache_observed_request_total == 1
    assert updated.metrics.provider_input_tokens == 120
    assert updated.metrics.provider_output_tokens == 23
    assert updated.metrics.provider_total_tokens == 163
    assert updated.metrics.provider_cache_read_input_tokens == 96

    # 离线 mock 也可构造 ModelToolTurn，但绝不是 Provider 请求，指标必须原样保持。
    mock_summary = AgentModelUsageSummary(request_total=1, usage_reported_request_total=1, input_tokens=99)
    unchanged = _with_provider_usage_metrics(updated, model=_MapFixtureModel(), usage=mock_summary)
    assert unchanged == updated

    # 保留这个对象级夹具，确保零 cache hit 仍属于“已观测”而不是“未知”。
    zero_cache = ModelUsageMetrics(
        input_tokens=4,
        output_tokens=3,
        total_tokens=7,
        cache_read_input_tokens=0,
        usage_observation="reported",
        cache_observation="reported",
    )
    assert zero_cache.cache_read_input_tokens == 0


def main() -> None:
    try:
        _verify_semantic_output_compatibility()
        # 两份材料各放入一个不会出现在另一个章节中的标记，验证 Runner 每回合不拼接整库正文。
        import_workspace_document(
            filename="deep_map_delivery.md",
            content="# 交付约束\n\nMAP_ONLY_DELIVERY：交付前必须保留验收范围。\n",
        )
        import_workspace_document(
            filename="deep_map_risk.md",
            content="# 风险约束\n\nMAP_ONLY_RISK：发布前必须完成风险复核。\n",
        )
        base = create_knowledge_base(name="K4 Map 检查点回归")
        _index_active_materials(
            knowledge_base_id=base.knowledge_base_id,
            document_names=["deep_map_delivery.md", "deep_map_risk.md"],
        )
        scope = build_knowledge_deep_task_scope(
            KnowledgeDeepTaskRequest(
                knowledge_base_id=base.knowledge_base_id,
                task_kind="audit",
                task_goal="审查当前资料的交付与风险约束。",
            )
        )
        assert len(scope.map_units) == 2
        _verify_full_scope_reduce_plan(scope)
        first_unit, second_unit = scope.map_units

        # 资料对照必须由客户显式选择文档，最终即使模型漏掉细分表格字段，也会从已完成的 Map
        # 小结构成一行诚实的资料摘要表，而不是退化成没有表格的普通跨文档摘要。
        comparison_scope = build_knowledge_deep_task_scope(
            KnowledgeDeepTaskRequest(
                knowledge_base_id=base.knowledge_base_id,
                task_kind="comparison",
                task_goal="逐项对照两份资料的交付与风险约束。",
                document_ids=[first_unit.document_id, second_unit.document_id],
            )
        )
        assert comparison_scope.scope_mode == "selected_documents"
        assert comparison_scope.selected_document_ids == [first_unit.document_id, second_unit.document_id]
        comparison_task_id = "task_k4_compare42"
        create_knowledge_deep_task_map_queued_run(task_id=comparison_task_id, scope=comparison_scope)
        comparison_result = asyncio.run(
            run_knowledge_deep_task(
                task_id=comparison_task_id,
                scope=comparison_scope,
                model=_MapFixtureModel(),
            )
        )
        assert comparison_result.status == "completed"
        assert comparison_result.result is not None
        assert len(comparison_result.result.comparison_rows) >= 1
        assert len(comparison_result.result.comparison_rows[0].values) == 2
        comparison_report = export_knowledge_deep_task_report(
            task_id=comparison_task_id,
            request=KnowledgeDeepTaskReportExportRequest(
                confirmed=True,
                filename="K4 资料对照表离线回归.md",
            ),
        )
        comparison_report_text = (VERIFY_ROOT / "knowledge_reports" / comparison_report.filename).read_text(
            encoding="utf-8"
        )
        assert "## 资料对照表" in comparison_report_text
        assert "| 对照维度 |" in comparison_report_text

        rate_limit_task_id = "task_k4_ratecafe"
        rate_limit_run = create_knowledge_deep_task_map_queued_run(task_id=rate_limit_task_id, scope=scope)
        _verify_provider_rate_limit_queue(task_id=rate_limit_task_id)
        _verify_provider_usage_metrics(run=rate_limit_run)

        # K4.5 的暂停在启动前立即生效，不应读取任何章节或触发模型；显式继续后仍沿用同一
        # task/scope，之前没有完成的节点才开始执行。
        paused_task_id = "task_k4_aabbccdd"
        create_knowledge_deep_task_map_queued_run(task_id=paused_task_id, scope=scope)
        pause_response = request_knowledge_deep_task_pause(paused_task_id)
        assert pause_response is not None and pause_response.accepted
        assert pause_response.status == "paused"
        paused_map = asyncio.run(
            run_knowledge_deep_task_map(
                task_id=paused_task_id,
                scope=scope,
                model=_MapFixtureModel(),
            )
        )
        assert paused_map.status == "paused"
        assert paused_map.completed_map_count == 0
        resumed = resume_knowledge_deep_task(paused_task_id)
        assert resumed is not None and resumed[0].accepted
        assert resumed[0].status == "pending"
        resumed_map = asyncio.run(
            run_knowledge_deep_task_map(
                task_id=paused_task_id,
                scope=scope,
                model=_MapFixtureModel(),
            )
        )
        assert resumed_map.status == "completed"
        assert resumed_map.completed_map_count == 2

        # 瞬态 HTTP 请求失败不应要求客户连续点击“继续并重试”。Runtime 对同一个 Map 节点只做
        # 一次短退避重试，成功后继续后续章节；这里模拟的仍是 AgentRunner 的 model_request_failed。
        transient_task_id = "task_k4_ff00ee11"
        create_knowledge_deep_task_map_queued_run(task_id=transient_task_id, scope=scope)
        transient_model = _MapFixtureModel(transient_map_request_failures=1)
        transient_map = asyncio.run(
            run_knowledge_deep_task_map(
                task_id=transient_task_id,
                scope=scope,
                model=transient_model,
            )
        )
        assert transient_map.status == "completed"
        assert transient_model.turn_count == 3  # 首次失败 + 当前章节重试 + 第二章节。
        transient_events = load_task_log_events(transient_task_id)
        assert transient_events is not None
        assert any(event.event == "knowledge_deep_model_transient_retry" for event in transient_events)

        # Reduce 复用同一个受控重试包装：一次临时 HTTP 请求失败后只重试当前批次，Map checkpoint
        # 不会重新读取或调用模型，最终合并仍可正常完成。
        transient_reduce_task_id = "task_k4_ff00ee22"
        create_knowledge_deep_task_map_queued_run(task_id=transient_reduce_task_id, scope=scope)
        prepared_map = asyncio.run(
            run_knowledge_deep_task_map(
                task_id=transient_reduce_task_id,
                scope=scope,
                model=_MapFixtureModel(),
            )
        )
        assert prepared_map.status == "completed"
        transient_reduce_model = _MapFixtureModel(transient_reduce_request_failures=1)
        transient_reduce = asyncio.run(
            run_knowledge_deep_task_reduce(
                task_id=transient_reduce_task_id,
                scope=scope,
                model=transient_reduce_model,
            )
        )
        assert transient_reduce.status == "completed"
        assert transient_reduce_model.reduce_turn_count == 3  # 首次失败 + 批次重试 + 最终 Reduce。
        transient_reduce_events = load_task_log_events(transient_reduce_task_id)
        assert transient_reduce_events is not None
        assert any(event.event == "knowledge_deep_model_transient_retry" for event in transient_reduce_events)

        # 取消发生在模型回合中时，当前章节先写入 checkpoint，下一章节不会再调用模型。这是
        # 协作式取消的关键：不强杀 Provider 请求，也不丢失已经安全完成的工作。
        cancelling_task_id = "task_k4_ddccbbaa"
        create_knowledge_deep_task_map_queued_run(task_id=cancelling_task_id, scope=scope)
        cancel_responses = []

        def cancel_after_current_turn() -> None:
            response = request_knowledge_deep_task_cancel(cancelling_task_id)
            assert response is not None
            cancel_responses.append(response)

        cancelled_model = _MapFixtureModel(on_map_turn=cancel_after_current_turn)
        cancelled_map = asyncio.run(
            run_knowledge_deep_task_map(
                task_id=cancelling_task_id,
                scope=scope,
                model=cancelled_model,
            )
        )
        assert cancel_responses and cancel_responses[0].accepted
        assert cancel_responses[0].status == "running"
        assert cancelled_map.status == "cancelled"
        assert cancelled_model.turn_count == 1
        cancelled_run = load_workflow_run(cancelling_task_id)
        assert cancelled_run is not None
        assert [step.status for step in cancelled_run.steps] == ["completed", "cancelled"]
        cancelled_events = load_task_log_events(cancelling_task_id)
        assert cancelled_events is not None
        assert any(event.event == "task_cancel_requested" for event in cancelled_events)
        assert cancelled_events[-1].event == "task_cancelled"
        cancelled_result = get_knowledge_deep_task_result(cancelling_task_id)
        assert cancelled_result is not None
        assert cancelled_result.coverage is not None
        assert cancelled_result.coverage.state == "partial"
        assert cancelled_result.coverage.completed_map_unit_ids == [first_unit.map_unit_id]
        assert cancelled_result.coverage.cancelled_map_unit_ids == [second_unit.map_unit_id]
        assert cancelled_result.report_readiness is not None
        assert cancelled_result.report_readiness.state == "partial_preview"
        assert cancelled_result.report_readiness.can_export is False

        # 首次运行在第二章节持续返回非法 JSON。Runner 最多做一次格式修复，随后任务停驻；
        # 第一章节的 checkpoint 必须已落库，且历史快照不能泄漏任何原始正文标记。
        interrupted_model = _MapFixtureModel(failing_map_unit_id=second_unit.map_unit_id)
        interrupted = asyncio.run(
            run_knowledge_deep_task_map(
                task_id="task_k4_a1b2c3d4",
                scope=scope,
                model=interrupted_model,
            )
        )
        assert interrupted.status == "blocked"
        assert interrupted.completed_map_count == 1
        assert interrupted.failed_map_unit_ids == [second_unit.map_unit_id]
        assert interrupted_model.turn_count == 3  # 第一章一次 + 第二章一次 + 格式修复一次。
        assert all(
            not ("MAP_ONLY_DELIVERY" in chapter and "MAP_ONLY_RISK" in chapter)
            for chapter in interrupted_model.received_chapters
        )
        stored = load_workflow_run("task_k4_a1b2c3d4")
        assert stored is not None
        stored_json = stored.model_dump_json()
        assert "MAP_ONLY_DELIVERY" not in stored_json
        assert "MAP_ONLY_RISK" not in stored_json
        assert sum(step.status == "completed" for step in stored.steps) == 1
        partial_result = get_knowledge_deep_task_result("task_k4_a1b2c3d4")
        assert partial_result is not None
        assert partial_result.result is None
        assert partial_result.coverage is not None
        assert partial_result.coverage.state == "partial"
        assert partial_result.coverage.completed_map_unit_ids == [first_unit.map_unit_id]
        assert partial_result.coverage.failed_map_unit_ids == [second_unit.map_unit_id]
        assert partial_result.coverage.pending_map_unit_ids == []
        assert len(partial_result.coverage.completed_map_results) == 1
        assert partial_result.coverage.completed_map_results[0].map_unit_id == first_unit.map_unit_id
        assert partial_result.report_readiness is not None
        assert partial_result.report_readiness.state == "partial_preview"
        assert partial_result.report_readiness.can_export is False
        assert partial_result.report_readiness.missing_map_unit_ids == [second_unit.map_unit_id]
        assert "MAP_ONLY_DELIVERY" not in partial_result.model_dump_json()
        assert "MAP_ONLY_RISK" not in partial_result.model_dump_json()
        try:
            export_knowledge_deep_task_report(
                task_id="task_k4_a1b2c3d4",
                request=KnowledgeDeepTaskReportExportRequest(confirmed=True),
            )
        except KnowledgeDeepTaskReportNotReadyError:
            pass
        else:
            raise AssertionError("部分完成的深度任务不应导出正式报告。")

        # 第二次进入同一 task 是显式恢复。已完成章节不应再次请求模型，只补上一个失败节点。
        recovered_model = _MapFixtureModel()
        recovered = asyncio.run(
            run_knowledge_deep_task_map(
                task_id="task_k4_a1b2c3d4",
                scope=scope,
                model=recovered_model,
            )
        )
        assert recovered.status == "completed"
        assert recovered.completed_map_count == 2
        assert recovered_model.turn_count == 1
        assert len(recovered_model.received_chapters) == 1
        assert not (
            "MAP_ONLY_RISK" in recovered_model.received_chapters[0]
            and "MAP_ONLY_DELIVERY" in recovered_model.received_chapters[0]
        )
        events = load_task_log_events("task_k4_a1b2c3d4")
        assert events is not None
        event_names = [event.event for event in events]
        assert "knowledge_deep_map_unit_failed" in event_names
        assert "knowledge_deep_map_resumed" in event_names
        assert event_names[-1] == "knowledge_deep_map_completed"

        # 完成后的重复调用是纯读取，不会再次调用模型。
        idempotent_model = _MapFixtureModel()
        idempotent = asyncio.run(
            run_knowledge_deep_task_map(
                task_id="task_k4_a1b2c3d4",
                scope=scope,
                model=idempotent_model,
            )
        )
        assert idempotent.status == "completed"
        assert idempotent_model.turn_count == 0

        # Reduce 只消费已完成 Map checkpoint。先注入一次持续非法 JSON，确认失败批次会停驻；
        # 恢复后仅重试该 Reduce 节点，不重新读取章节正文、更不会再次执行 Map。
        failed_reduce_model = _MapFixtureModel(fail_reduce=True)
        failed_reduce = asyncio.run(
            run_knowledge_deep_task_reduce(
                task_id="task_k4_a1b2c3d4",
                scope=scope,
                model=failed_reduce_model,
            )
        )
        assert failed_reduce.status == "blocked"
        assert failed_reduce.completed_reduce_batch_count == 0
        assert failed_reduce_model.reduce_turn_count == 2  # 首次输出 + 一次格式修复。
        assert failed_reduce_model.received_chapters == []

        # 客户从 UI 点击“继续并重试”走的是 resume API + 完整 Runtime，而不是直接调用
        # Reduce。Map 已完整但 WorkflowRun 被恢复为 pending 时，必须跳过 Map 并继续 Reduce。
        resumed_reduce = resume_knowledge_deep_task("task_k4_a1b2c3d4")
        assert resumed_reduce is not None and resumed_reduce[0].accepted
        assert resumed_reduce[0].status == "pending"
        recovered_reduce_model = _MapFixtureModel()
        reduced = asyncio.run(
            run_knowledge_deep_task(
                task_id="task_k4_a1b2c3d4",
                scope=scope,
                model=recovered_reduce_model,
            )
        )
        assert reduced.status == "completed"
        assert reduced.coverage is not None
        assert reduced.coverage.completed_reduce_count == reduced.coverage.total_reduce_count
        assert reduced.result is not None
        assert reduced.result.covered_map_unit_ids == [unit.map_unit_id for unit in scope.map_units]
        assert len(reduced.result.conflicts) == 1
        assert recovered_reduce_model.reduce_turn_count == 2  # 一个批次 + 一个最终合并。
        assert recovered_reduce_model.received_chapters == []
        stored_after_reduce = load_workflow_run("task_k4_a1b2c3d4")
        assert stored_after_reduce is not None
        stored_after_reduce_json = stored_after_reduce.model_dump_json()
        assert "MAP_ONLY_DELIVERY" not in stored_after_reduce_json
        assert "MAP_ONLY_RISK" not in stored_after_reduce_json
        stored_map_steps = stored_after_reduce.steps[: len(scope.map_units)]
        assert all(step.output["context_route"]["route"] == "map_reduce" for step in stored_map_steps)
        assert all(step.output["context_route"]["stage"] == "deep_map" for step in stored_map_steps)
        stored_reduce_steps = stored_after_reduce.steps[len(scope.map_units) :]
        assert all(step.output["context_route"]["route"] == "map_reduce" for step in stored_reduce_steps)
        assert all(step.output["context_route"]["stage"] == "deep_reduce" for step in stored_reduce_steps)
        assert all(step.output["context_route"]["long_context_direct_execution"] is False for step in stored_reduce_steps)
        events = load_task_log_events("task_k4_a1b2c3d4")
        assert events is not None
        event_names = [event.event for event in events]
        assert "knowledge_deep_reduce_failed" in event_names
        assert "knowledge_deep_reduce_resumed" in event_names
        assert event_names[-1] == "knowledge_deep_reduce_completed"

        idempotent_reduce_model = _MapFixtureModel()
        idempotent_reduce = asyncio.run(
            run_knowledge_deep_task_reduce(
                task_id="task_k4_a1b2c3d4",
                scope=scope,
                model=idempotent_reduce_model,
            )
        )
        assert idempotent_reduce.status == "completed"
        assert idempotent_reduce_model.turn_count == 0

        # scope 与最终 Reduce 都必须只靠 SQLite 任务快照恢复，服务重启后不能依赖内存中的请求对象。
        restored_scope = get_knowledge_deep_task_scope("task_k4_a1b2c3d4")
        assert restored_scope is not None and restored_scope == scope
        restored_task = get_knowledge_deep_task_result("task_k4_a1b2c3d4")
        assert restored_task is not None and restored_task.result is not None
        assert restored_task.result.covered_map_unit_ids == [unit.map_unit_id for unit in scope.map_units]
        assert restored_task.coverage is not None
        assert restored_task.coverage.state == "complete"
        assert restored_task.coverage.completed_map_unit_ids == [unit.map_unit_id for unit in scope.map_units]
        assert restored_task.coverage.completed_reduce_count == restored_task.coverage.total_reduce_count
        assert restored_task.report_readiness is not None
        assert restored_task.report_readiness.state == "ready_for_export"
        assert restored_task.report_readiness.can_export is True
        try:
            export_knowledge_deep_task_report(
                task_id="task_k4_a1b2c3d4",
                request=KnowledgeDeepTaskReportExportRequest(confirmed=False),
            )
        except KnowledgeDeepTaskReportConfirmationError:
            pass
        else:
            raise AssertionError("未确认的深度任务报告不应写入文件。")
        exported_report = export_knowledge_deep_task_report(
            task_id="task_k4_a1b2c3d4",
            request=KnowledgeDeepTaskReportExportRequest(
                confirmed=True,
                filename="K4 深度审查离线回归.md",
            ),
        )
        report_path = VERIFY_ROOT / "knowledge_reports" / exported_report.filename
        assert report_path.is_file()
        report_text = report_path.read_text(encoding="utf-8")
        assert "# 知识库深度审查报告" in report_text
        assert "## 总体结论" in report_text
        assert f"任务 ID：`{exported_report.task_id}`" in report_text
        assert "MAP_ONLY_DELIVERY" not in report_text
        assert "MAP_ONLY_RISK" not in report_text

        # HTTP 层只验证立即受理、无正文 scope 的结果补读与后台入口注入点；测试不命中真实模型。
        from fastapi.testclient import TestClient

        from app.api import knowledge as knowledge_api
        from main import app

        async def fake_deep_task_runtime(*, task_id: str, scope, progress_callback=None):
            return await run_knowledge_deep_task(
                task_id=task_id,
                scope=scope,
                model=_MapFixtureModel(),
                progress_callback=progress_callback,
            )

        with patch.object(knowledge_api, "run_knowledge_deep_task", fake_deep_task_runtime):
            client = TestClient(app)
            started = client.post(
                "/api/knowledge/deep-tasks/start",
                json={
                    "knowledge_base_id": base.knowledge_base_id,
                    "task_kind": "audit",
                    "task_goal": "审查交付与风险约束。",
                },
            )
            assert started.status_code == 202, started.text
            api_task_id = started.json()["task_id"]
            accepted = client.get(f"/api/knowledge/deep-tasks/{api_task_id}/result")
            assert accepted.status_code == 200, accepted.text
            accepted_payload = accepted.json()
            assert accepted_payload["status"] in {"pending", "running", "completed"}
            assert "coverage" in accepted_payload
            assert "report_readiness" in accepted_payload
            assert "MAP_ONLY_DELIVERY" not in accepted.text
            assert "MAP_ONLY_RISK" not in accepted.text

            api_export = client.post(
                "/api/knowledge/deep-tasks/task_k4_a1b2c3d4/report",
                json={"confirmed": True, "filename": "K4 API 报告回归.md"},
            )
            assert api_export.status_code == 201, api_export.text
            exported_payload = api_export.json()
            preview = client.get(
                f"/api/tasks/task_k4_a1b2c3d4/artifacts/{exported_payload['artifact_id']}/preview"
            )
            assert preview.status_code == 200, preview.text
            preview_payload = preview.json()
            assert preview_payload["available"] is True
            assert "知识库深度审查报告" in preview_payload["text"]
            assert "MAP_ONLY_DELIVERY" not in preview_payload["text"]
            assert preview_payload["metadata"].get("output_path") == "<hidden>"

            # 兼容旧 Qt：它曾把 artifact ID 预编码后再交给 QUrl，网络层最终会让后端收到
            # 字面量 ``%3A``。不能因为客户端 URL 编码差异让历史报告变成“已生成但打不开”。
            legacy_encoded_artifact_id = quote(quote(exported_payload["artifact_id"], safe=""), safe="")
            legacy_preview = client.get(
                f"/api/tasks/task_k4_a1b2c3d4/artifacts/{legacy_encoded_artifact_id}/preview"
            )
            assert legacy_preview.status_code == 200, legacy_preview.text
            assert legacy_preview.json()["artifact_id"] == exported_payload["artifact_id"]
            with patch("app.api.tasks.os.startfile") as start_file:
                legacy_open = client.post(
                    f"/api/tasks/task_k4_a1b2c3d4/artifacts/{legacy_encoded_artifact_id}/open"
                )
            assert legacy_open.status_code == 200, legacy_open.text
            assert legacy_open.json()["opened"] is True
            start_file.assert_called_once()

            api_pause_task_id = "task_k4_badc0ffe"
            create_knowledge_deep_task_map_queued_run(task_id=api_pause_task_id, scope=scope)
            api_paused = client.post(f"/api/knowledge/deep-tasks/{api_pause_task_id}/pause")
            assert api_paused.status_code == 200, api_paused.text
            assert api_paused.json()["status"] == "paused"
            api_resumed = client.post(f"/api/knowledge/deep-tasks/{api_pause_task_id}/resume")
            assert api_resumed.status_code == 202, api_resumed.text
            assert api_resumed.json()["accepted"] is True

            api_cancel_task_id = "task_k4_f00dbabe"
            create_knowledge_deep_task_map_queued_run(task_id=api_cancel_task_id, scope=scope)
            api_cancelled = client.post(f"/api/knowledge/deep-tasks/{api_cancel_task_id}/cancel")
            assert api_cancelled.status_code == 200, api_cancelled.text
            assert api_cancelled.json()["status"] == "cancelled"

        # API 后台协程之外若发生未知异常，任务也必须写入 SQLite 的失败终态。这样客户端
        # 断开后重新打开任务历史，不会误看到一个永久运行中的深度任务；冻结 scope 仍可补读。
        unexpected_failure_task_id = "task_k4_f1e2d3c4"
        create_knowledge_deep_task_map_queued_run(task_id=unexpected_failure_task_id, scope=scope)
        unexpected_failure = mark_knowledge_deep_task_unexpected_failure(unexpected_failure_task_id)
        assert unexpected_failure is not None
        assert unexpected_failure.status == "failed"
        assert unexpected_failure.scope == scope
        failure_events = load_task_log_events(unexpected_failure_task_id)
        assert failure_events is not None
        assert failure_events[-1].event == "knowledge_deep_task_failed"

        # generation 更新后，旧 scope 在模型调用前就被拒绝，防止旧 checkpoint 混进新资料。
        updated_delivery = import_workspace_document(
            filename="deep_map_delivery.md",
            content="# 交付约束\n\nMAP_ONLY_DELIVERY_V2：新增发布演练。\n",
        )
        # workspace 同名导入会保留原件并生成一个稳定的新相对文件名；必须索引实际返回的
        # 新材料，不能再依赖“即使内容没变也强行新建 generation”的旧行为伪造 scope stale。
        _index_active_materials(
            knowledge_base_id=base.knowledge_base_id,
            document_names=[updated_delivery.relative_path],
        )
        stale_model = _MapFixtureModel()
        stale = asyncio.run(
            run_knowledge_deep_task_map(
                task_id="task_k4_e5f6a7b8",
                scope=scope,
                model=stale_model,
            )
        )
        assert stale.status == "blocked"
        assert stale_model.turn_count == 0

        print("Knowledge K4 deep task verification passed: Map/Reduce isolation, recovery and stale rejection.")
    finally:
        sqlite_service._INITIALIZED_PATHS.clear()
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
