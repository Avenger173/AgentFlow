"""阶段 5 共用的受控 AgentRunner。

Runner 管理模型工具循环、参数校验、上限和停止原因；具体 Agent 只提供角色定义、工具白名单
和最终输出 schema。它不接触 API Key、绝对工作区路径或数据库对象，这些仍保留在 Runtime
本地上下文中。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol

from pydantic import BaseModel, ValidationError

from app.services.model_gateway import (
    ModelConversationMessage,
    ModelGatewayConnectionError,
    ModelGatewayTimeoutError,
    ModelToolDefinition,
    ModelToolTurn,
    ModelUsageMetrics,
)


class AgentRunnerError(RuntimeError):
    """AgentRunner 内部的结构化失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolCallingModel(Protocol):
    """让真实 ModelRuntime 与离线 mock 共用 Runner 的最小协议。"""

    async def tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn: ...


ToolHandler = Callable[[BaseModel], dict[str, Any] | Awaitable[dict[str, Any]]]
# 有些 Agent 的证据边界取决于 Runtime 状态，例如“所有用户选择的材料已经读取完”。
# Predicate 只负责关闭后续 Tool 暴露，不携带模型上下文，也不替模型生成结果。
ToolPhaseClosePredicate = Callable[[], bool]


@dataclass(frozen=True)
class AgentRunProgress:
    """Runner 已确认发生的阶段事件，不包含未经校验的模型正文。"""

    stage: str
    turn_index: int
    message: str
    tool_name: str = ""
    level: str = "info"


AgentProgressCallback = Callable[[AgentRunProgress], Awaitable[None] | None]


@dataclass(frozen=True)
class AgentTool:
    """一个受控 Tool 的 schema 与执行入口。

    ``name`` 是平台内部的稳定审计名称，可以使用 ``domain.action`` 这类层级写法；
    ``model_name`` 是发给模型供应商的函数名。部分 OpenAI-compatible 服务只接受
    字母、数字、下划线和连字符，因此二者必须允许分离，避免协议限制污染历史记录。
    """

    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    model_name: str = ""
    closes_tool_phase: bool = False

    @property
    def model_tool_name(self) -> str:
        """返回模型协议使用的函数名，未声明别名时兼容既有 Tool。"""

        return self.model_name or self.name

    def model_definition(self) -> ModelToolDefinition:
        return ModelToolDefinition(
            name=self.model_tool_name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise AgentRunnerError("invalid_tool_arguments", "工具参数不符合声明的结构。") from exc

        try:
            result = self.handler(validated)
            if inspect.isawaitable(result):
                result = await result
        except AgentRunnerError:
            raise
        except Exception as exc:
            # Tool 的底层异常不能直接冲出 Agent loop；返回结构化失败让模型可决定是否调整参数，
            # Runner 也能按同类失败阈值收敛，避免接口 500 或无限重试。
            raise AgentRunnerError("tool_execution_failed", str(exc)) from exc
        if not isinstance(result, dict):
            raise AgentRunnerError("invalid_tool_result", "工具没有返回 JSON object 结果。")
        return result


@dataclass(frozen=True)
class AgentDefinition:
    """一个正式内置 Agent 的静态边界定义。"""

    agent_id: str
    system_prompt: str
    tools: tuple[AgentTool, ...]
    output_model: type[BaseModel]
    max_turns: int = 4
    max_tool_calls: int = 8
    max_same_tool_failure: int = 2
    # 供应商即便声明 JSON mode，也可能偶发输出自然语言、截断 JSON 或漏掉必填字段。这里
    # 只允许一次“格式修复”回合，且修复时不再开放 Tool，避免把协议容错变成无界重试。
    max_output_repair_attempts: int = 1
    close_tool_phase_when: ToolPhaseClosePredicate | None = None


@dataclass(frozen=True)
class AgentToolTrace:
    call_id: str
    turn_index: int
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class AgentTurnTrace:
    turn_index: int
    tool_call_count: int
    finished: bool
    output_repair_requested: bool = False
    # 每轮只保留供应商实际回传的聚合计量，不记录 prompt、模型正文、缓存键或任何凭据。
    usage: ModelUsageMetrics = ModelUsageMetrics()


@dataclass(frozen=True)
class AgentModelUsageSummary:
    """一次 AgentRunner 运行内、按实际模型回合聚合的用量摘要。

    token 字段在至少一轮响应实际提供对应 usage 时才有值；因此数值旁必须同时参考
    ``usage_reported_request_total/request_total``，不能把部分可观测合计当作完整账单。
    """

    request_total: int = 0
    usage_reported_request_total: int = 0
    cache_observed_request_total: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_miss_input_tokens: int | None = None


@dataclass(frozen=True)
class AgentRunResult:
    """Runner 的结果不直接绑定某个业务 Agent。"""

    status: str
    stop_reason: str
    output: BaseModel | None
    tool_traces: tuple[AgentToolTrace, ...]
    turn_traces: tuple[AgentTurnTrace, ...]
    message: str = ""

    @property
    def model_usage_summary(self) -> AgentModelUsageSummary:
        """从已落下的回合 trace 计算只读摘要，避免调用方遗漏失败/修复轮。"""

        return summarize_model_usage(trace.usage for trace in self.turn_traces)


class AgentRunner:
    """执行“模型 -> Tool -> 观察 -> 最终结构化输出”的有限循环。"""

    async def run(
        self,
        *,
        definition: AgentDefinition,
        model: ToolCallingModel,
        user_message: str,
        progress_callback: AgentProgressCallback | None = None,
    ) -> AgentRunResult:
        # 模型返回的是 provider 函数名，trace / 数据库则始终记录内部稳定名称。
        # 这样可同时满足供应商函数命名规则和跨版本审计可读性。
        tool_map = {tool.model_tool_name: tool for tool in definition.tools}
        if len(tool_map) != len(definition.tools):
            return AgentRunResult(
                status="failed",
                stop_reason="duplicate_model_tool_name",
                output=None,
                tool_traces=(),
                turn_traces=(),
                message="Agent 定义中存在重复的模型 Tool 函数名。",
            )
        model_tools = [tool.model_definition() for tool in definition.tools]
        messages = [ModelConversationMessage(role="user", content=user_message)]
        traces: list[AgentToolTrace] = []
        turns: list[AgentTurnTrace] = []
        same_failure_counts: dict[tuple[str, str], int] = {}
        tool_phase_closed = False
        output_repair_pending = False
        output_repair_attempts = 0

        for turn_index in range(1, definition.max_turns + 1):
            # 动态收束用于“多份材料均已读到”这类 Runtime 可确定判断。只在新模型轮生效，
            # 让同一批已经接受的并行 Tool 调用完整落审计，下一轮再强制输出最终结构化结果。
            if not tool_phase_closed and definition.close_tool_phase_when is not None:
                try:
                    tool_phase_closed = bool(definition.close_tool_phase_when())
                except Exception as exc:
                    return AgentRunResult(
                        status="failed",
                        stop_reason="tool_phase_guard_failed",
                        output=None,
                        tool_traces=tuple(traces),
                        turn_traces=tuple(turns),
                        message=f"工具阶段收束条件执行失败：{exc}",
                    )
            # 某些 Agent 的“读取材料”是完整的上下文获取边界。读取成功后不再暴露 Tool，
            # 让模型必须基于已拿到的事实收束为结构化结果，避免无收益地反复检索。
            # 一旦进入输出格式修复，模型只能重写最后的 JSON，不能借此重新搜索、读取或执行
            # 工具。修复只改善表达，不应改变已获取的事实范围和权限边界。
            available_model_tools = [] if tool_phase_closed or output_repair_pending else model_tools
            await _emit_progress(
                progress_callback,
                AgentRunProgress(
                    stage="model_turn_started",
                    turn_index=turn_index,
                    message="正在请求模型决定下一步。",
                ),
            )
            try:
                turn = await model.tool_turn(
                    system_prompt=definition.system_prompt,
                    messages=messages,
                    tools=available_model_tools,
                )
            except (asyncio.TimeoutError, ModelGatewayTimeoutError):
                # 超时是用户可理解、通常可重试的终态，不能与权限、协议或供应商 4xx 混成
                # 同一个 model_request_failed。已完成的 Tool trace 仍会交给上层任务历史。
                return AgentRunResult(
                    status="failed",
                    stop_reason="model_timeout",
                    output=None,
                    tool_traces=tuple(traces),
                    turn_traces=tuple(turns),
                    message="模型在当前等待时间内没有返回。",
                )
            except ModelGatewayConnectionError:
                # 连接失败通常可以通过检查网络、Base URL 或稍后重试恢复。不要把它和协议、
                # 权限或供应商 HTTP 错误混在一起，任务历史也能据此给出正确的下一步。
                return AgentRunResult(
                    status="failed",
                    stop_reason="model_connection_failed",
                    output=None,
                    tool_traces=tuple(traces),
                    turn_traces=tuple(turns),
                    message="模型服务当前无法连接。",
                )
            except Exception as exc:
                return AgentRunResult(
                    status="failed",
                    stop_reason="model_request_failed",
                    output=None,
                    tool_traces=tuple(traces),
                    turn_traces=tuple(turns),
                    message=f"模型调用失败：{exc}",
                )

            messages.append(
                ModelConversationMessage(
                    role="assistant",
                    content=turn.content,
                    tool_calls=turn.tool_calls,
                    reasoning_content=turn.reasoning_content,
                )
            )
            if not turn.tool_calls:
                await _emit_progress(
                    progress_callback,
                    AgentRunProgress(
                        stage="output_validation_started",
                        turn_index=turn_index,
                        message="正在校验结构化结论和来源。",
                    ),
                )
                try:
                    output = _parse_model_output(turn.content, definition.output_model)
                except AgentRunnerError as exc:
                    validation_message = _output_validation_message(
                        str(exc),
                        turn.finish_reason,
                    )
                    can_repair = (
                        output_repair_attempts < definition.max_output_repair_attempts
                        and turn_index < definition.max_turns
                    )
                    if can_repair:
                        # 这条 assistant 消息刚刚被证明不是合法最终结果，而且没有 Tool Call。
                        # 将它留在 repair 请求的会话里会污染上下文；对 DeepSeek JSON mode 的空
                        # content 而言，还会被接口判为无效 assistant message 并返回 HTTP 400。
                        # 已完成的 Tool 调用及其结果都在此前消息中保留，故移除它不会扩大或丢失
                        # 已确认的证据范围。格式修复只应消费“已验证事实 + 修复指令”。
                        messages.pop()
                        output_repair_attempts += 1
                        output_repair_pending = True
                        turns.append(
                            AgentTurnTrace(
                                turn_index,
                                0,
                                False,
                                output_repair_requested=True,
                                usage=turn.usage,
                            )
                        )
                        messages.append(
                            ModelConversationMessage(
                                role="user",
                                content=_output_repair_instruction(validation_message),
                            )
                        )
                        await _emit_progress(
                            progress_callback,
                            AgentRunProgress(
                                stage="output_format_repair_started",
                                turn_index=turn_index,
                                message="首次结构化输出未通过校验，正在请求一次无工具格式修复。",
                            ),
                        )
                        continue

                    turns.append(AgentTurnTrace(turn_index, 0, True, usage=turn.usage))
                    repair_suffix = (
                        f"已进行 {output_repair_attempts} 次格式修复后仍未通过。"
                        if output_repair_attempts
                        else "本次没有可用的格式修复轮次。"
                    )
                    return AgentRunResult(
                        status="failed",
                        stop_reason=exc.code,
                        output=None,
                        tool_traces=tuple(traces),
                        turn_traces=tuple(turns),
                        message=f"{validation_message} {repair_suffix}",
                    )
                turns.append(AgentTurnTrace(turn_index, 0, True, usage=turn.usage))
                return AgentRunResult(
                    status="completed",
                    stop_reason="completed",
                    output=output,
                    tool_traces=tuple(traces),
                    turn_traces=tuple(turns),
                )

            if len(traces) + len(turn.tool_calls) > definition.max_tool_calls:
                turns.append(AgentTurnTrace(turn_index, len(turn.tool_calls), True, usage=turn.usage))
                return AgentRunResult(
                    status="budget_exhausted",
                    stop_reason="max_tool_calls_exceeded",
                    output=None,
                    tool_traces=tuple(traces),
                    turn_traces=tuple(turns),
                    message=f"工具调用超过本次任务上限 {definition.max_tool_calls} 次。",
                )

            turns.append(AgentTurnTrace(turn_index, len(turn.tool_calls), False, usage=turn.usage))
            for call in turn.tool_calls:
                if tool_phase_closed or output_repair_pending:
                    return AgentRunResult(
                        status="failed",
                        stop_reason=(
                            "output_repair_requested_tools"
                            if output_repair_pending
                            else "tool_phase_closed"
                        ),
                        output=None,
                        tool_traces=tuple(traces),
                        turn_traces=tuple(turns),
                        message=(
                            "模型在输出格式修复阶段仍请求调用工具，已停止本次运行。"
                            if output_repair_pending
                            else "模型在材料获取阶段结束后仍请求调用工具，已停止本次运行。"
                        ),
                    )
                tool = tool_map.get(call.name)
                if tool is None:
                    return AgentRunResult(
                        status="failed",
                        stop_reason="tool_not_allowed",
                        output=None,
                        tool_traces=tuple(traces),
                        turn_traces=tuple(turns),
                        message=f"模型请求了未获授权的工具：{call.name}。",
                    )

                try:
                    await _emit_progress(
                        progress_callback,
                        AgentRunProgress(
                            stage="tool_execution_started",
                            turn_index=turn_index,
                            tool_name=tool.name,
                            message=f"正在执行 {tool.name}。",
                        ),
                    )
                    result = await tool.execute(call.arguments)
                    trace = AgentToolTrace(
                        call_id=call.call_id,
                        turn_index=turn_index,
                        tool_name=tool.name,
                        arguments=call.arguments,
                        result=result,
                    )
                    tool_message = {"ok": True, "result": result}
                except AgentRunnerError as exc:
                    key = (tool.name, exc.code)
                    same_failure_counts[key] = same_failure_counts.get(key, 0) + 1
                    trace = AgentToolTrace(
                        call_id=call.call_id,
                        turn_index=turn_index,
                        tool_name=tool.name,
                        arguments=call.arguments,
                        error_code=exc.code,
                        error_message=str(exc),
                    )
                    tool_message = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
                    traces.append(trace)
                    messages.append(
                        ModelConversationMessage(
                            role="tool",
                            content=json.dumps(tool_message, ensure_ascii=False),
                            tool_call_id=call.call_id,
                            tool_name=tool.model_tool_name,
                        )
                    )
                    if same_failure_counts[key] >= definition.max_same_tool_failure:
                        return AgentRunResult(
                            status="failed",
                            stop_reason="repeated_tool_failure",
                            output=None,
                            tool_traces=tuple(traces),
                            turn_traces=tuple(turns),
                            message=f"工具 {tool.name} 连续 {same_failure_counts[key]} 次失败，已停止自动重试。",
                        )
                    continue

                traces.append(trace)
                await _emit_progress(
                    progress_callback,
                    AgentRunProgress(
                        stage="tool_execution_completed",
                        turn_index=turn_index,
                        tool_name=tool.name,
                        message=f"已完成 {tool.name}。",
                    ),
                )
                if tool.closes_tool_phase:
                    tool_phase_closed = True
                messages.append(
                    ModelConversationMessage(
                        role="tool",
                        content=json.dumps(tool_message, ensure_ascii=False),
                        tool_call_id=call.call_id,
                        tool_name=tool.model_tool_name,
                    )
                )

        return AgentRunResult(
            status="max_turns_exceeded",
            stop_reason="max_turns_exceeded",
            output=None,
            tool_traces=tuple(traces),
            turn_traces=tuple(turns),
            message=f"模型超过本次任务上限 {definition.max_turns} 轮，已停止。",
        )


def summarize_model_usage(metrics: Iterable[ModelUsageMetrics]) -> AgentModelUsageSummary:
    """聚合多轮真实 usage，同时保留每个字段可能仅部分回合可观测的事实。"""

    collected = tuple(metrics)
    reported = tuple(item for item in collected if item.usage_observation == "reported")
    cache_observed = tuple(item for item in collected if item.cache_observation == "reported")
    return AgentModelUsageSummary(
        request_total=len(collected),
        usage_reported_request_total=len(reported),
        cache_observed_request_total=len(cache_observed),
        input_tokens=_sum_reported_usage(reported, "input_tokens"),
        output_tokens=_sum_reported_usage(reported, "output_tokens"),
        total_tokens=_sum_reported_usage(reported, "total_tokens"),
        cache_read_input_tokens=_sum_reported_usage(cache_observed, "cache_read_input_tokens"),
        cache_creation_input_tokens=_sum_reported_usage(cache_observed, "cache_creation_input_tokens"),
        cache_miss_input_tokens=_sum_reported_usage(cache_observed, "cache_miss_input_tokens"),
    )


def _sum_reported_usage(metrics: tuple[ModelUsageMetrics, ...], field_name: str) -> int | None:
    values = [getattr(item, field_name) for item in metrics]
    observed = [value for value in values if isinstance(value, int)]
    return sum(observed) if observed else None


async def _emit_progress(
    callback: AgentProgressCallback | None,
    event: AgentRunProgress,
) -> None:
    """进度观察不能反向影响 Agent 主链路，UI 断开也不能让任务失败。"""

    if callback is None:
        return
    try:
        result = callback(event)
        if inspect.isawaitable(result):
            await result
    except Exception:
        # 运行审计和最终结果仍由主链路持久化；这里刻意不把观察面异常升级为任务异常。
        return


def _parse_model_output(content: str, output_model: type[BaseModel]) -> BaseModel:
    """解析最终 JSON，兼容模型偶尔附带解释、代码围栏或前置 JSON 示例。

    模型的自然语言前缀不属于业务结果。某些模型会先重复一个不完整 schema 示例、再输出
    正式结果，因此不能只尝试第一个 ``{``；这里会逐个尝试完整 JSON object，并只接受通过
    Pydantic Output Guardrail 的那一个。这里不修复字段语义，也不接受 array 作为最终结果。
    """

    raw = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(raw)
    saw_json_object = False

    for candidate in candidates:
        for object_index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                payload, _remaining = json.JSONDecoder().raw_decode(candidate[object_index:])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            saw_json_object = True
            try:
                return output_model.model_validate(payload)
            except ValidationError:
                # 一个 JSON object 可能只是模型复述的 schema 或不完整草稿；继续查找后续
                # object，直到找到可验证的最终结果，而不是在第一个示例处误判失败。
                continue

    if saw_json_object:
        raise AgentRunnerError("model_output_invalid", "模型最终结果没有通过输出契约校验。")
    raise AgentRunnerError("model_output_invalid", "模型没有返回合法的 JSON 结构化结果。")


def _output_repair_instruction(validation_message: str) -> str:
    """构造一次无工具格式修复请求，不暴露 Runtime 内部实现或原始校验细节。"""

    return (
        "你刚才的最终回复未通过结构化输出校验（"
        f"{validation_message}）。请仅重写最终结果：只返回一个符合系统提示中 schema 的 JSON object，"
        "不要使用 Markdown 代码围栏、解释文字或工具调用。只能复用已出现的 source_id，"
        "不要添加新事实、文件名、路径或行号。"
    )


def _output_validation_message(validation_message: str, finish_reason: str) -> str:
    """把供应商的长度终态转成可行动的、脱敏的任务说明。

    输出契约失败本身仍由 ``_parse_model_output`` 判定；这里只补充“为什么一次格式修复也可能
    无法完成”的原因。不能拼接原始响应或供应商错误正文，那里可能含有用户材料。
    """

    if finish_reason.strip().lower() in {"length", "max_tokens"}:
        return f"{validation_message} 本次模型输出达到长度上限，结果可能在 JSON 结束前被截断。"
    return validation_message
