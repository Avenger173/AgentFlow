"""知识库 K3 的受控可信回答服务。

本服务刻意不让模型自行搜索、读取文件或接触 SQLite/向量对象。它只接收 K2 检索结果，经
Evidence Gate 放行后提供有限正文；模型返回后还会再次核验活动 generation，防止索引切换
期间把旧版本结论交给客户。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import perf_counter

from app.agents.runner import AgentDefinition, AgentRunner, ToolCallingModel
from app.database.task_repository import load_workflow_run, save_workflow_run
from app.schemas.events import TaskLogEvent, TaskLogLevel
from app.schemas.knowledge import (
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    KnowledgeAnswerSource,
    KnowledgeAnswerTaskResultResponse,
    KnowledgeEvidenceGateResult,
    KnowledgeRetrievalEvidence,
    KnowledgeTrustedAnswer,
)
from app.schemas.workflow import (
    RuntimeExecutionLimits,
    RuntimeExecutionMetrics,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowToolCall,
)
from app.services.knowledge_evidence_gate import gate_knowledge_evidence
from app.services.knowledge_context_router import (
    KnowledgeContextBudgetError,
    enforce_knowledge_context_budget,
    plan_knowledge_context_route,
)
from app.services.knowledge_retrieval import retrieve_knowledge_evidence
from app.services.model_gateway import ModelGatewayError, ModelRuntime, resolve_model_runtime_for_route


_MAX_MODEL_SOURCES = 4
_MAX_EVIDENCE_CHARS_PER_SOURCE = 6_000
_MAX_MODEL_OUTPUT_TOKENS = 2_048
KNOWLEDGE_ANSWER_AGENT_ID = "knowledge_agent"
KNOWLEDGE_RETRIEVAL_STEP_ID = "knowledge_retrieval"
KNOWLEDGE_EVIDENCE_GATE_STEP_ID = "knowledge_evidence_gate"
KNOWLEDGE_ANSWER_STEP_ID = "knowledge_answer"
KNOWLEDGE_RETRIEVAL_TOOL_NAME = "knowledge.retrieve"
KNOWLEDGE_EVIDENCE_GATE_TOOL_NAME = "knowledge.evidence_gate"

# 回调只汇报真实阶段与脱敏摘要；它使 HTTP/WebSocket 层能实时展示任务，而不会把模型正文、
# 父块或数据库对象混进事件流。回调异常不能影响答案自身的可靠性判断。
KnowledgeAnswerProgressCallback = Callable[[str, str, str | None, TaskLogLevel], Awaitable[None]]


class KnowledgeAnswerServiceError(ValueError):
    """K3 回答层的受控失败；具体模型和数据库细节不会透传给客户。"""


@dataclass(frozen=True)
class _ModelEvidence:
    """只在服务内存中存在的模型证据包，不进入 HTTP 响应或任务审计。"""

    source: KnowledgeAnswerSource
    parent_content: str


async def answer_knowledge_question(
    request: KnowledgeAnswerRequest,
    *,
    model: ToolCallingModel | None = None,
    progress_callback: KnowledgeAnswerProgressCallback | None = None,
) -> KnowledgeAnswerResponse:
    """在 Gate 通过后请求一次受约束模型回答，并验证每条 claim 的来源。

    ``model`` 是离线夹具的注入点。生产环境不传时才解析已保存的多供应商 ModelGateway 配置；
    它不会读 API Key、文件路径或数据库对象到模型上下文。
    """

    # 只有真实 Route 解析成功时才保留快照。证据不足、离线夹具或模型配置不可用保持为空，
    # 让历史页诚实区分“本次没调用模型”与“旧版本没有记录”。
    model_routes = []

    await _notify_progress(
        progress_callback,
        event="knowledge_retrieval_started",
        message="正在从当前活动索引检索可用来源。",
        step_id=KNOWLEDGE_RETRIEVAL_STEP_ID,
    )
    retrieval = await asyncio.to_thread(retrieve_knowledge_evidence, request)
    await _notify_progress(
        progress_callback,
        event="knowledge_retrieval_completed",
        message=(
            f"已完成 {retrieval.diagnostics.mode} 检索，定位 {len(retrieval.evidences)} 条候选来源。"
        ),
        step_id=KNOWLEDGE_RETRIEVAL_STEP_ID,
    )
    initial_gate = await asyncio.to_thread(gate_knowledge_evidence, retrieval)
    if initial_gate.evidence_state == "insufficient":
        await _notify_progress(
            progress_callback,
            event="knowledge_evidence_insufficient",
            message="当前证据不足，已停止模型回答，等待补充材料或缩小问题。",
            step_id=KNOWLEDGE_EVIDENCE_GATE_STEP_ID,
            level="warning",
        )
        return KnowledgeAnswerResponse(
            knowledge_base_id=request.knowledge_base_id,
            query=request.query,
            status="insufficient_evidence",
            stop_reason="evidence_insufficient",
            evidence_gate=initial_gate,
            retrieval_diagnostics=retrieval.diagnostics,
            message="当前资料不足以形成可追溯回答，请补充材料、缩小问题范围或重新检索。",
            model_turn_count=0,
        )

    await _notify_progress(
        progress_callback,
        event="knowledge_evidence_verified",
        message=(
            f"已核验 {initial_gate.covered_document_count} 份活动资料，"
            f"证据状态为 {initial_gate.evidence_state}。"
        ),
        step_id=KNOWLEDGE_EVIDENCE_GATE_STEP_ID,
        level="warning" if initial_gate.evidence_state == "partial" else "info",
    )

    model_evidences = _build_model_evidences(retrieval.evidences, initial_gate)
    if not model_evidences:
        # Gate 已验证来源，但若服务内存中的证据无法再一一对应，就宁可停止也不拼接错误正文。
        return KnowledgeAnswerResponse(
            knowledge_base_id=request.knowledge_base_id,
            query=request.query,
            status="failed",
            stop_reason="verified_context_unavailable",
            evidence_gate=initial_gate,
            retrieval_diagnostics=retrieval.diagnostics,
            message="已核验的来源正文当前不可用，未请求模型回答；请重新检索后重试。",
            model_turn_count=0,
        )

    if model is None:
        try:
            resolution = resolve_model_runtime_for_route("knowledge_answer")
            resolved_runtime = resolution.runtime
            model_routes.append(resolution.audit_snapshot(stage="knowledge_answer"))
        except ModelGatewayError:
            return KnowledgeAnswerResponse(
                knowledge_base_id=request.knowledge_base_id,
                query=request.query,
                status="failed",
                stop_reason="model_unavailable",
                evidence_gate=initial_gate,
                retrieval_diagnostics=retrieval.diagnostics,
                message="当前没有可用的回答模型配置；资料和来源未被发送，请先在模型密钥页完成配置。",
                model_turn_count=0,
            )
        # 此处是单次结构化回答，不复用连接测试的极小 token 预算；仍限制为足够容纳有限 claim 的
        # 请求级预算，既避免长输出截断，也不改变客户保存的全局模型配置。
        model = _knowledge_model_with_output_budget(resolved_runtime)

    definition = AgentDefinition(
        agent_id="knowledge_agent",
        system_prompt=_knowledge_answer_system_prompt(initial_gate, model_evidences),
        tools=(),
        output_model=KnowledgeTrustedAnswer,
        max_turns=2,
        max_tool_calls=0,
        max_output_repair_attempts=1,
    )
    user_message = _knowledge_answer_user_message(request, initial_gate, model_evidences)
    context_route = plan_knowledge_context_route(
        stage="knowledge_answer",
        system_prompt=definition.system_prompt,
        user_message=user_message,
        model=model,
    )
    try:
        enforce_knowledge_context_budget(context_route)
    except KnowledgeContextBudgetError as exc:
        # K3 已通过来源数和单条内容上限把输入收束在预算内。这个显式停止只防止未来改动绕过
        # 该边界后，借“长窗口”名义把整库正文直接发送给 Provider。
        return KnowledgeAnswerResponse(
            knowledge_base_id=request.knowledge_base_id,
            query=request.query,
            status="failed",
            stop_reason="context_budget_exceeded",
            evidence_gate=initial_gate,
            retrieval_diagnostics=retrieval.diagnostics,
            context_route=context_route,
            message=str(exc),
            model_turn_count=0,
            model_routes=model_routes,
        )
    await _notify_progress(
        progress_callback,
        event="knowledge_model_started",
        message="正在仅依据已核验来源生成结构化回答。",
        step_id=KNOWLEDGE_ANSWER_STEP_ID,
    )
    result = await AgentRunner().run(
        definition=definition,
        model=model,
        user_message=user_message,
    )
    if result.status != "completed" or not isinstance(result.output, KnowledgeTrustedAnswer):
        await _notify_progress(
            progress_callback,
            event="knowledge_model_failed",
            message="回答模型未返回可核验结果，本次不会展示未经验证的正文。",
            step_id=KNOWLEDGE_ANSWER_STEP_ID,
            level="error",
        )
        return KnowledgeAnswerResponse(
            knowledge_base_id=request.knowledge_base_id,
            query=request.query,
            status="failed",
            stop_reason=result.stop_reason,
            evidence_gate=initial_gate,
            retrieval_diagnostics=retrieval.diagnostics,
            context_route=context_route,
            message=_failure_message(result.message),
            model_turn_count=len(result.turn_traces),
            model_routes=model_routes,
        )

    await _notify_progress(
        progress_callback,
        event="knowledge_model_completed",
        message="模型已返回候选回答，正在重新核验资料版本和引用。",
        step_id=KNOWLEDGE_ANSWER_STEP_ID,
    )

    # 模型等待期间资料库可能完成了新一代索引。最终返回前再次执行 Gate，避免 UI 把已经失效的
    # 证据卡与模型回答并排展示，造成“看起来有引用、实际引用旧版本”的错误信任感。
    final_gate = await asyncio.to_thread(gate_knowledge_evidence, retrieval)
    if final_gate.evidence_state == "insufficient" or (
        final_gate.active_index_generation != initial_gate.active_index_generation
    ):
        return KnowledgeAnswerResponse(
            knowledge_base_id=request.knowledge_base_id,
            query=request.query,
            status="failed",
            stop_reason="evidence_changed",
            evidence_gate=final_gate,
            retrieval_diagnostics=retrieval.diagnostics,
            context_route=context_route,
            message="资料库索引已在回答期间更新，旧来源已失效；未展示本次模型回答，请重新提问。",
            model_turn_count=len(result.turn_traces),
            model_routes=model_routes,
        )

    try:
        answer = _validate_trusted_answer(
            answer=result.output,
            initial_gate=initial_gate,
            final_gate=final_gate,
            model_evidences=model_evidences,
        )
    except KnowledgeAnswerServiceError as exc:
        return KnowledgeAnswerResponse(
            knowledge_base_id=request.knowledge_base_id,
            query=request.query,
            status="failed",
            stop_reason="model_output_invalid",
            evidence_gate=final_gate,
            retrieval_diagnostics=retrieval.diagnostics,
            context_route=context_route,
            message=f"模型回答未通过来源引用校验：{exc}",
            model_turn_count=len(result.turn_traces),
            model_routes=model_routes,
        )

    await _notify_progress(
        progress_callback,
        event="knowledge_answer_verified",
        message="回答与活动来源已重新核验，可安全展示。",
        step_id=KNOWLEDGE_ANSWER_STEP_ID,
    )

    return KnowledgeAnswerResponse(
        knowledge_base_id=request.knowledge_base_id,
        query=request.query,
        status="completed",
        stop_reason="completed",
        evidence_gate=final_gate,
        retrieval_diagnostics=retrieval.diagnostics,
        context_route=context_route,
        answer=answer,
        message=f"已根据 {len(answer.source_ids)} 条可定位来源生成回答。",
        model_turn_count=len(result.turn_traces),
        model_routes=model_routes,
    )


def create_knowledge_answer_queued_run(
    *,
    task_id: str,
    request: KnowledgeAnswerRequest,
) -> WorkflowRun:
    """在模型调用前创建统一历史任务，避免客户面对没有反馈的长等待。"""

    now = _now()
    run = WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="pending",
        summary="知识库问答已受理，正在等待检索当前活动资料版本。",
        steps=_task_steps_for_status(
            request=request,
            retrieval_status="pending",
            evidence_status="pending",
            answer_status="pending",
            message="知识库问答已受理，尚未读取资料或调用模型。",
        ),
        limits=_answer_task_limits(),
        metrics=RuntimeExecutionMetrics(started_at=now, step_total=3),
    )
    save_workflow_run(
        run=run,
        events=[
            _task_event(
                task_id,
                1,
                "task_queued",
                "知识库问答已受理，将只读取当前资料库的活动索引。",
            )
        ],
        plan=None,
        artifacts=[],
        tool_calls=[],
    )
    return run


async def run_knowledge_answer_task(
    *,
    task_id: str,
    request: KnowledgeAnswerRequest,
    model: ToolCallingModel | None = None,
    progress_callback: KnowledgeAnswerProgressCallback | None = None,
) -> KnowledgeAnswerTaskResultResponse:
    """运行 K3 问答并把阶段、来源摘要和终态写入统一任务历史。

    模型仍由 ``answer_knowledge_question`` 的 Gate/Verifier 约束。本函数只负责 Runtime 观察面，
    不能为了任务审计重新读取父块、扩展来源或绕过模型输出契约。
    """

    started_at = _now()
    started_clock = perf_counter()
    events = [
        _task_event(task_id, 1, "task_queued", "知识库问答已受理，将只读取当前资料库的活动索引。"),
    ]

    async def record_stage(
        event: str,
        message: str,
        step_id: str | None,
        level: TaskLogLevel,
    ) -> None:
        staged = _task_event(task_id, len(events) + 1, event, message, step_id=step_id, level=level)
        events.append(staged)
        if progress_callback is not None:
            await progress_callback(event, message, step_id, level)

    await record_stage(
        "task_started",
        "正在读取当前资料库状态，并准备受控检索。",
        KNOWLEDGE_RETRIEVAL_STEP_ID,
        "info",
    )
    try:
        answer_result = await answer_knowledge_question(
            request,
            model=model,
            progress_callback=record_stage,
        )
        run = _final_answer_task_run(
            task_id=task_id,
            request=request,
            answer_result=answer_result,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
        )
        terminal_event, terminal_level = _terminal_event_for_answer(answer_result)
        events.append(
            _task_event(
                task_id,
                len(events) + 1,
                terminal_event,
                run.summary,
                step_id=KNOWLEDGE_ANSWER_STEP_ID,
                level=terminal_level,
            )
        )
        save_workflow_run(
            run=run,
            events=events,
            plan=None,
            artifacts=[],
            tool_calls=_answer_task_tool_calls(task_id, request, answer_result),
        )
        if progress_callback is not None:
            await progress_callback(terminal_event, run.summary, KNOWLEDGE_ANSWER_STEP_ID, terminal_level)
        return KnowledgeAnswerTaskResultResponse(
            task_id=task_id,
            status=run.status,
            summary=run.summary,
            message=_task_message(answer_result),
            result=answer_result,
        )
    except Exception:
        # 未预期异常也要形成可回放失败，而不是让 Qt 只收到 WebSocket 断开。详细堆栈由服务端日志
        # 处理，客户和任务快照只记录稳定、可行动的说明。
        message = "知识库问答在运行时异常结束，资料未被修改；请稍后重试并查看任务历史。"
        run = _unexpected_failed_answer_task_run(
            task_id=task_id,
            request=request,
            started_at=started_at,
            duration_ms=_duration_ms(started_clock),
            message=message,
        )
        events.append(
            _task_event(
                task_id,
                len(events) + 1,
                "task_failed",
                message,
                step_id=KNOWLEDGE_ANSWER_STEP_ID,
                level="error",
            )
        )
        save_workflow_run(run=run, events=events, plan=None, artifacts=[], tool_calls=[])
        if progress_callback is not None:
            await progress_callback("task_failed", message, KNOWLEDGE_ANSWER_STEP_ID, "error")
        return KnowledgeAnswerTaskResultResponse(
            task_id=task_id,
            status="failed",
            summary=run.summary,
            message=message,
        )


def get_knowledge_answer_task_result(task_id: str) -> KnowledgeAnswerTaskResultResponse | None:
    """从统一任务快照恢复 K3 问答结果；不依赖进程内模型或事件缓冲。"""

    run = load_workflow_run(task_id)
    if run is None:
        return None
    answer_step = next((step for step in run.steps if step.step_id == KNOWLEDGE_ANSWER_STEP_ID), None)
    if answer_step is None or answer_step.agent != KNOWLEDGE_ANSWER_AGENT_ID:
        return None
    raw_result = answer_step.output.get("knowledge_answer")
    answer_result = None
    if isinstance(raw_result, dict):
        answer_result = KnowledgeAnswerResponse.model_validate(raw_result)
    return KnowledgeAnswerTaskResultResponse(
        task_id=task_id,
        status=run.status,
        summary=run.summary,
        message=str(answer_step.output.get("message", answer_step.message)),
        result=answer_result,
    )


def _answer_task_limits() -> RuntimeExecutionLimits:
    """固定 K3 的有限预算；问答不开放 Tool loop，也不以增加轮数掩盖引用失败。"""

    return RuntimeExecutionLimits(
        max_steps=3,
        max_tool_calls=2,
        max_retries_per_tool=0,
        tool_timeout_ms=30_000,
        task_timeout_ms=120_000,
        token_budget=4_096,
    )


def _task_steps_for_status(
    *,
    request: KnowledgeAnswerRequest,
    retrieval_status: str,
    evidence_status: str,
    answer_status: str,
    message: str,
    answer_result: KnowledgeAnswerResponse | None = None,
) -> list[WorkflowStepRun]:
    """把固定 K3 阶段投影为历史步骤，避免 Qt 通过字符串猜测真实任务状态。"""

    base_output = {
        "knowledge_base_id": request.knowledge_base_id,
        "query": request.query,
        "read_scope": "knowledge_base_active_generation_only",
        "original_files_unchanged": True,
    }
    retrieval_output = dict(base_output)
    evidence_output = dict(base_output)
    answer_output = dict(base_output)
    if answer_result is not None:
        retrieval_output.update(
            {
                "retrieval_mode": answer_result.retrieval_diagnostics.mode,
                "active_index_generation": answer_result.retrieval_diagnostics.active_index_generation,
                "candidate_count": answer_result.retrieval_diagnostics.parent_deduplicated_count,
                "diagnostic_warnings": answer_result.retrieval_diagnostics.warnings,
            }
        )
        evidence_output.update(
            {
                "evidence_state": answer_result.evidence_gate.evidence_state,
                "required_document_count": answer_result.evidence_gate.required_document_count,
                "covered_document_count": answer_result.evidence_gate.covered_document_count,
                "sources": [item.model_dump(mode="json") for item in answer_result.evidence_gate.sources],
                "warnings": answer_result.evidence_gate.warnings,
            }
        )
        answer_output.update(
            {
                "knowledge_answer": answer_result.model_dump(mode="json"),
                "message": answer_result.message,
                "model_turn_count": answer_result.model_turn_count,
                "stop_reason": answer_result.stop_reason,
            }
        )
        if answer_result.context_route is not None:
            # 任务历史只保留路由计数和状态，不会混入当次模型可见正文或 prompt。
            answer_output["context_route"] = answer_result.context_route.model_dump(mode="json")

    return [
        WorkflowStepRun(
            step_id=KNOWLEDGE_RETRIEVAL_STEP_ID,
            agent=KNOWLEDGE_ANSWER_AGENT_ID,
            action=KNOWLEDGE_RETRIEVAL_TOOL_NAME,
            status=retrieval_status,  # type: ignore[arg-type]
            message=message,
            output=retrieval_output,
        ),
        WorkflowStepRun(
            step_id=KNOWLEDGE_EVIDENCE_GATE_STEP_ID,
            agent=KNOWLEDGE_ANSWER_AGENT_ID,
            action=KNOWLEDGE_EVIDENCE_GATE_TOOL_NAME,
            status=evidence_status,  # type: ignore[arg-type]
            message=message,
            output=evidence_output,
        ),
        WorkflowStepRun(
            step_id=KNOWLEDGE_ANSWER_STEP_ID,
            agent=KNOWLEDGE_ANSWER_AGENT_ID,
            action="knowledge.answer",
            status=answer_status,  # type: ignore[arg-type]
            message=message,
            output=answer_output,
        ),
    ]


def _final_answer_task_run(
    *,
    task_id: str,
    request: KnowledgeAnswerRequest,
    answer_result: KnowledgeAnswerResponse,
    started_at: str,
    duration_ms: int,
) -> WorkflowRun:
    """按可信回答终态生成可回放快照；资料不足是受控停驻，不伪装成模型错误。"""

    if answer_result.status == "completed":
        status = "completed"
        summary = f"知识库问答完成：已依据 {len(answer_result.answer.source_ids) if answer_result.answer else 0} 条可定位来源生成结论。"
        retrieval_status, evidence_status, answer_status = "completed", "completed", "completed"
        step_completed, step_failed = 3, 0
    elif answer_result.status == "insufficient_evidence":
        status = "blocked"
        summary = "知识库问答因资料证据不足而安全停止，未请求模型补写结论。"
        retrieval_status, evidence_status, answer_status = "completed", "blocked", "blocked"
        step_completed, step_failed = 1, 0
    else:
        status = "failed"
        summary = "知识库问答未完成，未展示未经来源核验的模型正文。"
        retrieval_status, evidence_status, answer_status = "completed", "completed", "failed"
        step_completed, step_failed = 2, 1

    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        model_routes=list(answer_result.model_routes),
        steps=_task_steps_for_status(
            request=request,
            retrieval_status=retrieval_status,
            evidence_status=evidence_status,
            answer_status=answer_status,
            message=_task_message(answer_result),
            answer_result=answer_result,
        ),
        limits=_answer_task_limits(),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            step_total=3,
            step_completed=step_completed,
            step_failed=step_failed,
            tool_call_total=2,
            tool_call_failed=0,
            retry_total=max(0, answer_result.model_turn_count - 1),
        ),
    )


def _unexpected_failed_answer_task_run(
    *,
    task_id: str,
    request: KnowledgeAnswerRequest,
    started_at: str,
    duration_ms: int,
    message: str,
) -> WorkflowRun:
    return WorkflowRun(
        task_id=task_id,
        mode="runtime",
        status="failed",
        summary="知识库问答异常结束，资料未被修改。",
        steps=_task_steps_for_status(
            request=request,
            retrieval_status="failed",
            evidence_status="skipped",
            answer_status="failed",
            message=message,
        ),
        limits=_answer_task_limits(),
        metrics=RuntimeExecutionMetrics(
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            step_total=3,
            step_failed=2,
            tool_call_total=0,
            tool_call_failed=0,
        ),
    )


def _answer_task_tool_calls(
    task_id: str,
    request: KnowledgeAnswerRequest,
    answer_result: KnowledgeAnswerResponse,
) -> list[WorkflowToolCall]:
    """把检索与 Gate 作为真实内部能力审计；模型调用不伪装成可执行 Tool。"""

    source_ids = [source.source_id for source in answer_result.evidence_gate.sources]
    retrieval_result = {
        "retrieval_mode": answer_result.retrieval_diagnostics.mode,
        "active_index_generation": answer_result.retrieval_diagnostics.active_index_generation,
        "keyword_candidate_count": answer_result.retrieval_diagnostics.keyword_candidate_count,
        "dense_candidate_count": answer_result.retrieval_diagnostics.dense_candidate_count,
        "parent_deduplicated_count": answer_result.retrieval_diagnostics.parent_deduplicated_count,
        "local_cache_state": answer_result.retrieval_diagnostics.local_cache_state,
        "local_cache_age_ms": answer_result.retrieval_diagnostics.local_cache_age_ms,
        "source_ids": source_ids,
    }
    gate_result = {
        "evidence_state": answer_result.evidence_gate.evidence_state,
        "required_document_count": answer_result.evidence_gate.required_document_count,
        "covered_document_count": answer_result.evidence_gate.covered_document_count,
        "source_ids": source_ids,
        "warnings": answer_result.evidence_gate.warnings,
    }
    request_summary = {
        "knowledge_base_id": request.knowledge_base_id,
        "query_length": len(request.query),
        "read_scope": "knowledge_base_active_generation_only",
        "original_files_unchanged": True,
    }
    return [
        WorkflowToolCall(
            call_id=f"call_kb_retrieve_{task_id.rsplit('_', maxsplit=1)[-1]}",
            task_id=task_id,
            step_id=KNOWLEDGE_RETRIEVAL_STEP_ID,
            agent_id=KNOWLEDGE_ANSWER_AGENT_ID,
            tool_name=KNOWLEDGE_RETRIEVAL_TOOL_NAME,
            status="completed",
            max_attempts=1,
            timeout_ms=30_000,
            request=request_summary,
            result=retrieval_result,
            finished_at=_now(),
        ),
        WorkflowToolCall(
            call_id=f"call_kb_gate_{task_id.rsplit('_', maxsplit=1)[-1]}",
            task_id=task_id,
            step_id=KNOWLEDGE_EVIDENCE_GATE_STEP_ID,
            agent_id=KNOWLEDGE_ANSWER_AGENT_ID,
            tool_name=KNOWLEDGE_EVIDENCE_GATE_TOOL_NAME,
            status="completed",
            max_attempts=1,
            timeout_ms=15_000,
            request=request_summary,
            result=gate_result,
            finished_at=_now(),
        ),
    ]


def _terminal_event_for_answer(answer_result: KnowledgeAnswerResponse) -> tuple[str, TaskLogLevel]:
    if answer_result.status == "completed":
        return "task_completed", "info"
    if answer_result.status == "insufficient_evidence":
        return "task_blocked", "warning"
    return "task_failed", "error"


def _task_message(answer_result: KnowledgeAnswerResponse) -> str:
    """终态消息统一来自已验证响应，不把 Runner 或异常内部文本直接给客户。"""

    return answer_result.message


def _task_event(
    task_id: str,
    sequence: int,
    event: str,
    message: str,
    *,
    step_id: str | None = None,
    level: TaskLogLevel = "info",
) -> TaskLogEvent:
    return TaskLogEvent(
        task_id=task_id,
        sequence=sequence,
        event=event,
        agent_id=KNOWLEDGE_ANSWER_AGENT_ID,
        step_id=step_id,
        level=level,
        message=message,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _duration_ms(started_clock: float) -> int:
    return max(0, int((perf_counter() - started_clock) * 1000))


async def _notify_progress(
    progress_callback: KnowledgeAnswerProgressCallback | None,
    *,
    event: str,
    message: str,
    step_id: str | None,
    level: TaskLogLevel = "info",
) -> None:
    """实时观察面故障不得破坏 Gate、模型输出或最终引用校验。"""

    if progress_callback is None:
        return
    try:
        await progress_callback(event, message, step_id, level)
    except Exception:
        return


def _knowledge_model_with_output_budget(runtime: ModelRuntime) -> ModelRuntime:
    """仅提升本轮 K3 JSON 收束的最小输出预算，不改变客户保存的模型偏好。"""

    return replace(runtime, max_tokens=max(runtime.max_tokens, _MAX_MODEL_OUTPUT_TOKENS))


def _build_model_evidences(
    evidences: list[KnowledgeRetrievalEvidence],
    gate: KnowledgeEvidenceGateResult,
) -> tuple[_ModelEvidence, ...]:
    """按稳定来源锚点把 Gate 来源与 K2 内存证据重新配对，并限制模型上下文总量。

    比较题的 Gate 可能已经覆盖多份资料，但同一文档也可能贡献多个高分父块。先各取一条不同
    文档证据，再按检索顺序补足，避免前四条恰好都来自同一份文档而削弱比较题的事实边界。
    """

    evidence_by_identity = {
        _evidence_identity_from_retrieval(evidence): evidence
        for evidence in evidences
    }
    candidates: list[_ModelEvidence] = []
    for source in gate.sources:
        evidence = evidence_by_identity.get(_evidence_identity_from_source(source))
        if evidence is None:
            continue
        content = _compact_evidence(evidence.parent_content)
        if not content:
            continue
        candidates.append(_ModelEvidence(source=source, parent_content=content))

    selected: list[_ModelEvidence] = []
    selected_document_ids: set[str] = set()
    for candidate in candidates:
        if candidate.source.document_id in selected_document_ids:
            continue
        selected.append(candidate)
        selected_document_ids.add(candidate.source.document_id)
        if len(selected) >= _MAX_MODEL_SOURCES:
            return tuple(selected)
    for candidate in candidates:
        if candidate in selected:
            continue
        selected.append(candidate)
        if len(selected) >= _MAX_MODEL_SOURCES:
            break
    return tuple(selected)


def _evidence_identity_from_retrieval(evidence: KnowledgeRetrievalEvidence) -> tuple[object, ...]:
    """来源范围是父子块之间可稳定配对的最小公开身份，不依赖列表下标。"""

    return (
        evidence.document_id,
        evidence.document_version_id,
        evidence.source.source_kind,
        evidence.source.source_locator,
        evidence.source.start_char,
        evidence.source.end_char,
    )


def _evidence_identity_from_source(source: KnowledgeAnswerSource) -> tuple[object, ...]:
    return (
        source.document_id,
        source.document_version_id,
        source.source.source_kind,
        source.source.source_locator,
        source.source.start_char,
        source.source.end_char,
    )


def _compact_evidence(value: str) -> str:
    """保留自然段边界但限制单条父块占用，避免多来源问题吞掉回答输出预算。"""

    normalized = value.strip()
    if len(normalized) <= _MAX_EVIDENCE_CHARS_PER_SOURCE:
        return normalized
    return normalized[: _MAX_EVIDENCE_CHARS_PER_SOURCE - 3].rstrip() + "..."


def _knowledge_answer_system_prompt(
    gate: KnowledgeEvidenceGateResult,
    model_evidences: tuple[_ModelEvidence, ...],
) -> str:
    """构造不含路径、密钥或数据库对象的回答约束；真实正文只放在用户消息中。"""

    allowed_source_ids = ", ".join(item.source.source_id for item in model_evidences)
    state_rule = (
        "当前证据覆盖充分，可在已提供来源范围内直接回答。"
        if gate.evidence_state == "sufficient"
        else "当前证据只覆盖部分资料；evidence_state 必须输出 partial，并在 warnings 说明覆盖限制。"
    )
    return f"""你是 AgentFlow 知识库的可信回答阶段。只能依据本轮提供的 source_id、来源元数据和正文回答，
不得使用常识、外部知识、历史对话、猜测或未提供的文件内容。不得声称资料不存在，除非本轮正文明确表达。
{state_rule}
允许引用的 source_id 仅为：{allowed_source_ids}。
每条 claim 都必须有 1 至 4 个允许的 source_id；顶层 source_ids 必须恰好等于所有 claim 实际引用的来源并去重。
若 evidence_state 为 sufficient，顶层 source_ids 必须实际覆盖至少 {gate.required_document_count} 份不同文档；不能因为
资料库里还有未引用的其它文档，就把只引用一份文档的比较结论标为充分。
answer_markdown 只组织这些 claim，不要加入无法回指来源的新事实。回答应简洁、分段清晰，最多约 1,500 个汉字。
只输出 JSON object，不要 Markdown 代码围栏或额外解释。JSON 字段固定为：
{{"answer_markdown":"...","claims":[{{"claim_id":"kb_claim_1","statement":"...","source_ids":["kb_src_1"]}}],"source_ids":["kb_src_1"],"evidence_state":"sufficient 或 partial","warnings":["可选的证据范围说明"]}}。"""


def _knowledge_answer_user_message(
    request: KnowledgeAnswerRequest,
    gate: KnowledgeEvidenceGateResult,
    model_evidences: tuple[_ModelEvidence, ...],
) -> str:
    """传递有限正文和来源卡；模型只看见本轮获准回答所需的最小事实。"""

    payload = {
        "question": request.query,
        "evidence_state": gate.evidence_state,
        "required_document_count": gate.required_document_count,
        "covered_document_count": gate.covered_document_count,
        "sources": [
            {
                "source_id": item.source.source_id,
                "document_name": item.source.document_name,
                "heading_path": item.source.heading_path,
                "source": item.source.source.model_dump(mode="json"),
                "content": item.parent_content,
            }
            for item in model_evidences
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _validate_trusted_answer(
    *,
    answer: KnowledgeTrustedAnswer,
    initial_gate: KnowledgeEvidenceGateResult,
    final_gate: KnowledgeEvidenceGateResult,
    model_evidences: tuple[_ModelEvidence, ...],
) -> KnowledgeTrustedAnswer:
    """核验模型只引用本轮实际看到、且最终仍有效的来源。"""

    context_source_ids = {item.source.source_id for item in model_evidences}
    final_source_ids = {item.source_id for item in final_gate.sources}
    answer_source_ids = set(answer.source_ids)
    if not answer_source_ids.issubset(context_source_ids):
        raise KnowledgeAnswerServiceError("回答引用了未提供给模型的来源。")
    if not answer_source_ids.issubset(final_source_ids):
        raise KnowledgeAnswerServiceError("回答引用的来源不再属于当前活动资料版本。")
    if final_gate.evidence_state == "partial" and answer.evidence_state != "partial":
        raise KnowledgeAnswerServiceError("资料覆盖不足时不能把回答标记为充分。")
    if answer.evidence_state == "sufficient" and initial_gate.evidence_state != "sufficient":
        raise KnowledgeAnswerServiceError("初始证据未充分时不能把回答标记为充分。")

    source_by_id = {item.source.source_id: item.source for item in model_evidences}
    cited_document_count = len({source_by_id[source_id].document_id for source_id in answer.source_ids})
    if answer.evidence_state == "sufficient" and cited_document_count < final_gate.required_document_count:
        raise KnowledgeAnswerServiceError("充分回答没有实际覆盖题目所需的独立资料数量。")

    for claim in answer.claims:
        claim_source_ids = set(claim.source_ids)
        if not claim_source_ids.issubset(context_source_ids):
            raise KnowledgeAnswerServiceError("结论引用了未提供给模型的来源。")
        if not claim_source_ids.issubset(final_source_ids):
            raise KnowledgeAnswerServiceError("结论引用的来源已失效。")

    warnings = _unique_warnings([*final_gate.warnings, *answer.warnings])
    return answer.model_copy(update={"warnings": warnings[:8]})


def _unique_warnings(values: list[str]) -> list[str]:
    """合并确定性 Gate 提示与模型的范围提醒，保持顺序且不让重复文案挤占结果。"""

    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _failure_message(message: str) -> str:
    """将 Runner 的稳定失败原因变成客户可行动提示，不回显模型请求或正文。"""

    normalized = " ".join(message.split())
    if "没有返回合法" in normalized or "结构化" in normalized:
        return "模型没有返回可校验的来源引用，本次回答未展示；可稍后重试或切换模型。"
    if "超时" in normalized:
        return "模型在当前等待时间内没有完成回答；资料未被修改，可稍后重试。"
    if "无法连接" in normalized:
        return "模型服务当前无法连接；请检查模型配置或网络后重试。"
    return "模型未能完成本次受约束回答；资料未被修改，可稍后重试或切换模型。"
