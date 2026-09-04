from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.schemas.agent import AgentDescriptor
from app.schemas.model import ModelRouteAuditSnapshot, ModelRouteScope, ModelRouteSettings
from app.services.model_config_store import ModelConfigStoreError, StoredModelConfig, load_model_config
from app.services.model_route_store import load_model_route_settings
from app.services.secret_store import SecretStoreError


ModelTransport = Literal["openai_compatible", "anthropic"]
ContextCacheMode = Literal[
    "automatic_observable",
    "explicit_request",
    "observable_if_returned",
    "unknown",
]


class ModelGatewayError(RuntimeError):
    """模型网关层错误。

    这里统一收口供应商、网络、鉴权和响应格式问题。上层聊天服务只需要把错误转成
    `LlmChatError` / HTTP 502，不需要知道 DeepSeek、OpenAI 或 Claude 的细节。
    """


class ModelGatewayTimeoutError(ModelGatewayError):
    """供应商请求超过已配置等待时间。

    超时与 HTTP 4xx/5xx、响应协议错误的恢复方式不同：前者通常可由用户稍后重试或改用
    更快的模型解决，因此上层 AgentRunner 需要保留稳定的停止原因。
    """


class ModelGatewayConnectionError(ModelGatewayError):
    """模型服务无法建立网络连接。

    这类错误通常表示本机网络、Base URL、代理或供应商服务暂不可用。它和“请求已送达但
    供应商返回 HTTP 错误”不同，Runner 可以据此给用户稳定、可操作的重试提示。
    """


@dataclass(frozen=True)
class ModelRouteResolution:
    """一次作用域模型路由的解析结果。

    ``runtime`` 只在当前调用栈内携带解密后的 Key；历史、API 和 Qt 只使用
    ``audit_snapshot``，因此任务审计能说明实际模型而不会扩大密钥可见范围。
    """

    route: ModelRouteSettings
    runtime: "ModelRuntime"

    def audit_snapshot(self, *, stage: str = "") -> ModelRouteAuditSnapshot:
        return ModelRouteAuditSnapshot(
            stage=stage,
            route_id=self.route.route_id,
            profile_id=f"route:{self.route.route_id}",
            mode=self.route.mode,
            provider=self.runtime.provider,
            label=self.runtime.label,
            model=self.runtime.model,
            thinking=self.runtime.thinking if self.runtime.thinking in {"enabled", "disabled"} else "disabled",
            compatibility="ready",
            note="继承全局配置" if self.route.mode == "inherit_global" else "使用显式模型 Profile",
        )


@dataclass(frozen=True)
class ModelToolDefinition:
    """供应商无关的 Tool 定义。

    AgentRunner 只依赖这个小结构，具体 OpenAI-compatible / Anthropic 请求体在
    ``ModelRuntime`` 内转换，避免每个 Agent 自己拼供应商私有 JSON。
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ModelToolCall:
    """模型提出的一次工具调用，参数已解析成 JSON object。"""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelConversationMessage:
    """Model 可见的短会话记录。

    Runtime 的绝对路径、密钥、数据库对象和权限决策不会放入这个结构；只能通过受控 Tool
    结果向模型提供必要事实。
    """

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    # Kimi 与 DeepSeek 的思考模式 Tool Calling 都要求回传上一轮 reasoning_content；
    # 其它 provider 会忽略此字段。这个兼容字段只能在 Gateway 保留，不能泄漏到 Agent 协议层。
    reasoning_content: str = ""
    tool_call_id: str = ""
    tool_name: str = ""


@dataclass(frozen=True)
class ModelUsageMetrics:
    """供应商无关的单次请求 token 计量。

    ``cache_observation=reported`` 只表示供应商实际返回了缓存相关计量字段，0 仍是一次
    有效的“本次未命中”观测；``not_reported`` 则不能被 UI 或成本层解释成缓存关闭或命中。
    不同供应商对 cache write 的计费方式不同，因此它和 cache read/hit 单独保存。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_miss_input_tokens: int | None = None
    # 与缓存字段分开：多数供应商会回传普通 token usage，但并不回传缓存细目。
    usage_observation: Literal["reported", "not_reported"] = "not_reported"
    cache_observation: Literal["reported", "not_reported"] = "not_reported"


@dataclass(frozen=True)
class ModelToolTurn:
    """一次模型回合的标准化结果。"""

    content: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    reasoning_content: str = ""
    # OpenAI-compatible 服务会在 choices[0].finish_reason 中说明是否因长度停止。该信息只
    # 用于 Runtime 的失败诊断，不能替代业务输出契约，也不会把供应商原始回复暴露给用户。
    finish_reason: str = ""
    # 只保存供应商实际回传的聚合 token 计量。这里不保存 prompt、缓存键、模型原始响应或
    # API Key；上层可以据此做真实成本/缓存可观测性，而不能据此推断“缓存一定命中”。
    usage: ModelUsageMetrics = ModelUsageMetrics()


@dataclass(frozen=True)
class ModelTextTurn:
    """普通文本请求的标准化结果，保留文本与实际用量的同一响应边界。"""

    content: str
    usage: ModelUsageMetrics = ModelUsageMetrics()


@dataclass(frozen=True)
class NativeWebSearchSource:
    """供应商原生联网搜索返回的一条候选来源。

    这个结构刻意只保留标题和 URL。原生 Web Search 返回的正文可能是供应商专用的加密载荷，
    不能被业务层当作可验证数据；后续 ResearchGateway 会重新按安全边界读取页面并建立证据。
    """

    title: str
    url: str


@dataclass(frozen=True)
class NativeWebSearchResult:
    """一次受限原生联网搜索的脱敏结果，供上层 Gateway 继续处理。"""

    sources: tuple[NativeWebSearchSource, ...]
    query_count: int
    retrieved_at: str


@dataclass(frozen=True)
class ModelProviderProfile:
    """一个可用模型供应商的静态描述。

    profile 只描述“能怎么连”，不包含用户密钥。真正的 Key、模型名、Base URL
    由 `resolve_model_runtime()` 从本地 `.env` 或系统环境变量里读取。
    """

    provider: str
    label: str
    transport: ModelTransport
    default_base_url: str
    default_model: str | None = None
    supports_thinking: bool = False
    # 部分兼容接口接受 thinking，但不接受 OpenAI reasoning_effort；两项必须分开声明。
    sends_reasoning_effort: bool = False
    # OpenAI-compatible 服务并不完全一致；Kimi 已弃用 max_tokens，改用 max_completion_tokens。
    completion_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    # Kimi K2.6 对 temperature 取值有限制，省略时采用供应商默认采样策略。
    sends_temperature: bool = True
    supports_json_output: bool = True
    supports_tool_calls: bool = True
    # 这是“当前已核验的协议行为”，不是命中率承诺。实际命中只能读取单次响应 usage。
    context_cache_mode: ContextCacheMode = "unknown"
    context_cache_note: str = "当前未验证可观测的上下文缓存协议。"
    notes: str = ""


@dataclass(frozen=True)
class ModelRuntime:
    """一次模型调用实际使用的运行时配置。

    这个对象是 AgentFlow 的 Model Harness 小壳：它把“哪个供应商、哪个模型、走什么协议、
    请求限制是多少”固定下来，让上层 Agent / Workflow 不直接拼各厂商 API。
    """

    provider: str
    label: str
    transport: ModelTransport
    base_url: str
    model: str
    api_key: str
    thinking: str
    max_tokens: int
    temperature: float
    timeout_seconds: float
    supports_thinking: bool = False
    sends_reasoning_effort: bool = False
    completion_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    sends_temperature: bool = True
    supports_json_output: bool = True
    supports_tool_calls: bool = True
    context_cache_mode: ContextCacheMode = "unknown"
    context_cache_note: str = "当前未验证可观测的上下文缓存协议。"
    # 仅由 ``resolve_model_runtime_for_route`` 写入的脱敏路由事实。运行时仍将 API Key 留在
    # 内存对象中，但历史写入必须改用本字段，不能序列化整个 ModelRuntime。
    route_audit_snapshot: ModelRouteAuditSnapshot | None = None

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key.strip())

    async def chat(self, *, system_prompt: str, user_message: str) -> str:
        """发送一次普通文本聊天请求。

        当前只抽象“系统提示 + 用户消息 -> 文本回复”的最小能力。后续 JSON 输出、
        Tool Calls、流式输出会继续挂在这个网关后面，而不是散落到各个 Agent。
        """

        result = await self.chat_with_usage(
            system_prompt=system_prompt,
            user_message=user_message,
        )
        return result.content

    async def chat_with_usage(
        self,
        *,
        system_prompt: str,
        user_message: str,
    ) -> ModelTextTurn:
        """发送普通文本请求并保留供应商实际 usage。

        旧 ``chat`` 继续只返回文本，避免现有 Agent 改动；需要成本、缓存或性能指标的
        调用方必须显式选择这个入口，防止把未观测到的缓存状态误写入任务历史。
        """

        if self.transport == "anthropic":
            return await self._chat_anthropic(system_prompt=system_prompt, user_message=user_message)

        return await self._chat_openai_compatible(
            system_prompt=system_prompt,
            user_message=user_message,
        )

    async def chat_json(
        self,
        *,
        system_prompt: str,
        user_message: str,
        maximum_tokens: int = 512,
    ) -> str:
        """执行一个有界 JSON 回合，用于意图等小型结构化决策。

        结构化回合仍通过 Gateway 处理协议差异；DeepSeek/Kimi 会在该请求级别关闭思考，
        不改变客户保存的模型偏好。供应商未声明 JSON mode 时保留提示词约束并由调用方
        做 Pydantic 校验，因此不能把不兼容伪装成已通过的结构化输出。
        """

        bounded_tokens = max(128, min(int(maximum_tokens), self.max_tokens, 1024))
        constrained_runtime = replace(self, max_tokens=bounded_tokens, temperature=0)
        turn = await constrained_runtime.tool_turn(
            system_prompt=system_prompt,
            messages=[ModelConversationMessage(role="user", content=user_message)],
            tools=[],
        )
        return turn.content

    async def tool_turn(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        """发送一次可调用工具的模型回合。

        这里不执行工具，也不解释模型结果；它只把不同供应商的响应归一为 ``ModelToolTurn``。
        工具白名单、参数校验、失败上限和审计都由 AgentRunner / Runtime 决定。
        """

        if tools and not self.supports_tool_calls:
            raise ModelGatewayError(f"当前 {self.label} profile 未声明 Tool Calls 支持。")
        if self.transport == "anthropic":
            return await self._tool_turn_anthropic(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
            )
        return await self._tool_turn_openai_compatible(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
        )

    async def native_web_search_sources(
        self,
        *,
        queries: tuple[str, ...],
        max_uses: int = 6,
    ) -> NativeWebSearchResult:
        """通过已验证的供应商原生搜索取得候选 URL，不把私有协议泄漏给 Agent。

        目前只有 DeepSeek 的 Anthropic-compatible 原生 Web Search 经过本项目探针验证。它返回的
        ``encrypted_content`` 仅供供应商会话继续使用，不能直接喂给 PPT 渲染器，因此这里先收敛
        为短标题和 URL；ResearchGateway 仍需在用户已确认联网的前提下读取、截断并验证页面证据。
        """

        if self.provider != "deepseek":
            raise ModelGatewayError(f"当前 {self.label} 尚未配置可用的原生 Web Search 适配器。")
        normalized_queries = tuple(
            " ".join(query.split())[:140]
            for query in queries
            if isinstance(query, str) and len(" ".join(query.split())) >= 4
        )[:6]
        if not normalized_queries:
            raise ModelGatewayError("原生 Web Search 缺少有效查询语句。")
        if max_uses < 1 or max_uses > 6:
            raise ModelGatewayError("原生 Web Search 的查询预算必须在 1 到 6 之间。")

        url = _deepseek_anthropic_messages_url(self.base_url)
        search_prompt = (
            "Search only the approved research queries below. Collect source candidates suitable for "
            "verifying numerical facts. Prefer official organisations, governing bodies, primary data "
            "publishers, and reputable statistics providers. Prefer publicly accessible static HTML, text, or PDF "
            "pages with an explicit date and metric; avoid dynamic comparison pages, paywalls, login pages, and "
            "sources that only repeat another site's claim. Do not invent values, URLs, citations, or additional "
            "queries. A later stage will read and validate the pages.\n\nApproved queries:\n- "
            + "\n- ".join(normalized_queries)
        )
        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": min(max(512, self.max_tokens), 2_400),
            "temperature": 0,
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max_uses,
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": search_prompt}],
                }
            ],
        }
        data = await self._post_json(
            url=url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        return NativeWebSearchResult(
            sources=_extract_native_web_search_sources(data),
            query_count=len(normalized_queries),
            retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    async def _chat_openai_compatible(
        self,
        *,
        system_prompt: str,
        user_message: str,
    ) -> ModelTextTurn:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            self.completion_tokens_field: self.max_tokens,
        }

        _apply_openai_runtime_options(payload, self)

        data = await self._post_json(url=url, headers=headers, payload=payload)
        try:
            content = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelGatewayError("模型接口响应格式不符合 OpenAI-compatible 预期。") from exc

        return ModelTextTurn(
            content=_require_text_content(content),
            usage=extract_model_usage_metrics(data),
        )

    async def _chat_anthropic(
        self,
        *,
        system_prompt: str,
        user_message: str,
    ) -> ModelTextTurn:
        url = _anthropic_messages_url(self.base_url)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self.model,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_message}],
                }
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        data = await self._post_json(url=url, headers=headers, payload=payload)
        return ModelTextTurn(
            content=_extract_anthropic_text(data),
            usage=extract_model_usage_metrics(data),
        )

    async def _tool_turn_openai_compatible(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # DeepSeek V4 的思考模式在“模型调用工具 -> 回传工具结果”时有两项严格要求：
        # 必须回传 reasoning_content，且不能发送 tool_choice。若按普通 OpenAI 兼容层默认
        # 拼装，会在第二轮收到 HTTP 400。这个差异集中在 Gateway，调用方无需感知供应商细节。
        deepseek_thinking_tool_turn = self.provider == "deepseek" and self.thinking == "enabled"
        # 文档/PPT 的最终收束和格式修复属于“无工具 + 强制 JSON”的输出阶段。真实验收显示
        # DeepSeek/Kimi 在该组合下开启长思考会产生空 content、不完整 JSON 或撞上请求超时；
        # 仅在这个请求级别切换为非思考 JSON，不改变用户保存的全局偏好，也不影响有工具任务。
        structured_json_turn = (
            self.provider in {"deepseek", "kimi"}
            and self.thinking == "enabled"
            and not tools
            and self.supports_json_output
        )
        payload: dict[str, object] = {
            "model": self.model,
            "messages": _openai_tool_messages(
                system_prompt=system_prompt,
                messages=messages,
                preserve_reasoning=self.provider in {"kimi", "deepseek"} and self.thinking == "enabled",
                require_tool_call_content=deepseek_thinking_tool_turn,
            ),
            self.completion_tokens_field: self.max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
            # DeepSeek V4 思考模式拒绝 tool_choice；缺省值就是自动决定是否调用工具。
            if not deepseek_thinking_tool_turn:
                payload["tool_choice"] = "auto"
        elif self.supports_json_output:
            # 文档 Agent 在 Tool 阶段结束后必须生成可校验 JSON。仅靠提示词容易被部分模型
            # 忽略；这里利用 provider 已声明的 JSON mode 降低“自然语言长回答 -> 协议失败”。
            payload["response_format"] = {"type": "json_object"}
        _apply_openai_runtime_options(
            payload,
            self,
            thinking_override="disabled" if structured_json_turn else None,
        )
        data = await self._post_json(url=url, headers=headers, payload=payload)
        return _extract_openai_tool_turn(data)

    async def _tool_turn_anthropic(
        self,
        *,
        system_prompt: str,
        messages: list[ModelConversationMessage],
        tools: list[ModelToolDefinition],
    ) -> ModelToolTurn:
        url = _anthropic_messages_url(self.base_url)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": self.model,
            "system": system_prompt,
            "messages": _anthropic_tool_messages(messages),
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]
        data = await self._post_json(url=url, headers=headers, payload=payload)
        return _extract_anthropic_tool_turn(data)

    async def _post_json(self, *, url: str, headers: dict[str, str], payload: dict[str, object]) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            # 不暴露 URL、请求正文或供应商原始错误；其中可能夹带用户材料或敏感配置。
            raise ModelGatewayTimeoutError("模型请求在配置的等待时间内没有返回。") from exc
        except httpx.RequestError as exc:
            # 连接、DNS、TLS 或代理错误都不应把底层网络类名直接展示给用户；它们也和供应商
            # 已收到请求后的 HTTP 4xx/5xx 有不同的排查路径，因此保留单独的稳定错误类型。
            raise ModelGatewayConnectionError("模型服务当前无法连接。") from exc

        if response.status_code >= 400:
            # 只提取供应商结构化 error 的类型与短原因，不透传完整响应体，避免错误页面或
            # 上游回显夹带请求正文。该信息足以区分参数不兼容、限流与内容超限。
            detail = _provider_error_detail(response)
            suffix = f"（{detail}）" if detail else ""
            raise ModelGatewayError(f"模型接口返回 HTTP {response.status_code}{suffix}。")

        try:
            data = response.json()
        except ValueError as exc:
            raise ModelGatewayError("模型接口返回的内容不是合法 JSON。") from exc

        if not isinstance(data, dict):
            raise ModelGatewayError("模型接口响应顶层不是 JSON object。")
        return data


def _provider_error_detail(response: httpx.Response) -> str:
    """从标准错误对象中提取不含请求正文的有限诊断信息。"""

    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    error_type = str(error.get("type") or error.get("code") or "").strip()
    message = re.sub(r"\s+", " ", str(error.get("message") or "")).strip()
    # 供应商 message 理论上只解释参数；仍限制长度，并过滤看起来像密钥的片段。
    # 部分网关会在限流报错中回显 ``<ak-...>`` 形式的账户/访问令牌标识；即使不是完整 API
    # Key，也不该进入任务历史或 UI。这里同样处理 sk/ark/ak 前缀及尖括号包裹的变体。
    message = re.sub(r"<?\b(?:sk|ark|ak)-[A-Za-z0-9_-]{12,}\b>?", "[REDACTED]", message, flags=re.IGNORECASE)[:160]
    return " · ".join(part for part in (error_type[:60], message) if part)


_PROVIDER_PROFILES: dict[str, ModelProviderProfile] = {
    "deepseek": ModelProviderProfile(
        provider="deepseek",
        label="DeepSeek",
        transport="openai_compatible",
        default_base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
        supports_thinking=True,
        sends_reasoning_effort=True,
        context_cache_mode="automatic_observable",
        context_cache_note="DeepSeek 自动构建前缀缓存；仅以响应 usage 的命中/未命中 token 统计为准。",
        notes="DeepSeek OpenAI-compatible Chat Completions。",
    ),
    "kimi": ModelProviderProfile(
        provider="kimi",
        label="Kimi / Moonshot",
        transport="openai_compatible",
        default_base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k2.6",
        supports_thinking=True,
        completion_tokens_field="max_completion_tokens",
        sends_temperature=False,
        context_cache_note="当前没有接入经核验的 Kimi 上下文缓存计量字段。",
        notes="Kimi K2.6 支持文本、图片、视频输入与 OpenAI-compatible Tool Calls。",
    ),
    "openai": ModelProviderProfile(
        provider="openai",
        label="OpenAI",
        transport="openai_compatible",
        default_base_url="https://api.openai.com/v1",
        context_cache_mode="observable_if_returned",
        context_cache_note="Gateway 会读取响应中的 cached_tokens；是否启用及命中由具体模型、请求和响应决定。",
        notes="OpenAI Chat Completions / Responses 兼容入口先走 Chat Completions 最小闭环。",
    ),
    "anthropic": ModelProviderProfile(
        provider="anthropic",
        label="Anthropic Claude",
        transport="anthropic",
        default_base_url="https://api.anthropic.com",
        context_cache_mode="explicit_request",
        context_cache_note="Anthropic 缓存需在请求内容中显式声明 cache_control；当前 Gateway 尚未发送该标记。",
        notes="Claude 走 Anthropic Messages API，和 OpenAI-compatible 分开适配。",
    ),
    "qwen": ModelProviderProfile(
        provider="qwen",
        label="Qwen / DashScope",
        transport="openai_compatible",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        context_cache_note="当前没有接入经核验的 DashScope 上下文缓存计量字段。",
        notes="通义千问可走 DashScope OpenAI-compatible 模式。",
    ),
    "openai_compatible": ModelProviderProfile(
        provider="openai_compatible",
        label="自定义 OpenAI-compatible",
        transport="openai_compatible",
        default_base_url="",
        context_cache_note="自定义兼容网关的缓存协议未知；仅在响应实际提供标准计量字段时记录。",
        notes="用于 Moonshot、智谱、Gemini 兼容网关或私有模型代理等自定义入口。",
    ),
}

_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "dashscope": "qwen",
    "moonshot": "kimi",
    "moonshotai": "kimi",
    "openai-compatible": "openai_compatible",
    "openai_compatible": "openai_compatible",
    "custom": "openai_compatible",
    "custom_openai": "openai_compatible",
}

_PROVIDER_ENV_PREFIXES = {
    "deepseek": ("DEEPSEEK",),
    "openai": ("OPENAI",),
    "anthropic": ("ANTHROPIC", "CLAUDE"),
    "qwen": ("QWEN", "DASHSCOPE"),
    "kimi": ("KIMI", "MOONSHOT"),
    "openai_compatible": ("OPENAI_COMPATIBLE", "CUSTOM_LLM"),
}

_PLACEHOLDER_VALUES = {
    "replace_me",
    "your-openai-model",
    "your-claude-model",
    "your-qwen-model",
    "your-kimi-model",
    "your-model-name",
}


def list_model_provider_profiles() -> list[ModelProviderProfile]:
    """返回 UI / 文档可展示的模型供应商清单。"""

    return list(_PROVIDER_PROFILES.values())


def get_model_provider_profile(provider: str) -> ModelProviderProfile:
    """按标准 provider id 返回 profile，供 API 层做写入校验。"""

    return _profile_for(normalize_model_provider(provider))


def get_verified_model_context_window_tokens(runtime: ModelRuntime | None) -> int | None:
    """返回当前 Runtime 已核验的模型上下文窗口，未知时保守地返回 ``None``。

    这个函数刻意按“供应商 + 实际模型名”判断，不能因为一个 Provider 的默认模型支持长窗口，
    就把客户自定义的旧模型或兼容网关误判成可直读整库。2026-08 已核验的 DeepSeek V4
    Flash/Pro 为 1M；其它模型在加入前需补官方证据、请求契约与离线/真实回归。
    """

    if runtime is None:
        return None
    if runtime.provider == "deepseek" and runtime.model.strip().lower() in {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }:
        return 1_048_576
    return None


def normalize_model_provider(provider: str | None) -> str:
    value = (provider or "").strip().lower()
    if not value:
        return "mock"
    return _PROVIDER_ALIASES.get(value, value)


def any_model_api_key_configured() -> bool:
    """判断本机是否至少存在一个可用 Key。

    `chat_mode=auto` 只需要轻量判断，不需要解密 Key；真实调用时仍由
    `resolve_model_runtime()` 做 provider 精确解析。
    """

    try:
        stored_config = load_model_config()
    except ModelConfigStoreError:
        stored_config = StoredModelConfig()
    return stored_config.any_api_key_configured or bool(settings.any_llm_api_key)


def resolve_model_runtime(
    agent: AgentDescriptor | None = None,
    *,
    validate: bool = True,
) -> ModelRuntime:
    """解析当前 Agent 实际要使用的模型运行时。

    解析顺序：
    1. Agent manifest 里的 `llm.provider/model`，值为 `inherit` 时继承全局配置。
    2. 供应商专属环境变量，如 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`。
    3. 通用环境变量，如 `AGENTFLOW_LLM_API_KEY`。
    4. 供应商 profile 默认值，比如 DeepSeek 的默认 Base URL。

    `validate=False` 用于状态接口预览配置；真实调用必须保持默认 `True`。
    """

    stored_config = _load_stored_model_config()
    provider = _effective_provider(agent, stored_config=stored_config)
    profile = _profile_for(provider)
    base_url = _resolve_runtime_field(
        provider,
        "BASE_URL",
        config_value=_stored_field(stored_config, provider, "base_url"),
        settings_value=settings.llm_base_url,
        default=profile.default_base_url,
    ).rstrip("/")
    model = _effective_model(agent, provider=provider, profile=profile, stored_config=stored_config)
    api_key = _resolve_runtime_field(
        provider,
        "API_KEY",
        config_value=_stored_api_key(stored_config, provider),
        settings_value=settings.llm_api_key,
    )
    thinking = _resolve_runtime_field(
        provider,
        "THINKING",
        config_value=_stored_field(stored_config, provider, "thinking"),
        settings_value=settings.llm_thinking,
        default="disabled",
    ).lower()

    runtime = ModelRuntime(
        provider=profile.provider,
        label=profile.label,
        transport=profile.transport,
        base_url=base_url,
        model=model,
        api_key=api_key,
        thinking=thinking,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout_seconds=settings.llm_timeout_seconds,
        supports_thinking=profile.supports_thinking,
        sends_reasoning_effort=profile.sends_reasoning_effort,
        completion_tokens_field=profile.completion_tokens_field,
        sends_temperature=profile.sends_temperature,
        supports_json_output=profile.supports_json_output,
        supports_tool_calls=profile.supports_tool_calls,
        context_cache_mode=profile.context_cache_mode,
        context_cache_note=profile.context_cache_note,
    )

    if validate:
        _validate_runtime(runtime)
    return runtime


def resolve_model_runtime_for_route(
    route_id: ModelRouteScope,
    *,
    validate: bool = True,
) -> ModelRouteResolution:
    """解析一个客户可见作用域的模型路由。

    路由 Profile 的 ``configured`` 模式必须使用自身明确的 Provider、Base URL、模型和
    思考偏好；它不会在错误时降级成全局默认。``inherit_global`` 才会调用既有默认解析。
    这样客户在审计里看到的选择与实际请求保持同一事实。
    """

    route = load_model_route_settings(route_id)
    if route.mode == "inherit_global":
        runtime = resolve_model_runtime(validate=validate)
        resolution = ModelRouteResolution(route=route, runtime=runtime)
        return ModelRouteResolution(
            route=route,
            runtime=replace(runtime, route_audit_snapshot=resolution.audit_snapshot()),
        )

    if not route.provider or not route.base_url or not route.model:
        raise ModelGatewayError(f"模型路由 {route_id} 的显式 Profile 不完整，请重新保存配置。")
    provider = normalize_model_provider(route.provider)
    profile = _profile_for(provider)
    if route.thinking == "enabled" and not profile.supports_thinking:
        raise ModelGatewayError(f"{profile.label} 当前 Profile 不支持思考模式，无法用于 {route_id}。")

    # 复用请求级解析可确保 Provider Key 仍从同一份 DPAPI/环境变量边界获得；随后恢复正式
    # 任务预算，绝不把连接测试的 64 token / 30 秒限制偷偷带入 Agent 长任务。
    runtime, _ = resolve_model_runtime_for_test(
        provider=provider,
        base_url=route.base_url,
        model=route.model,
        thinking=route.thinking,
    )
    runtime = replace(
        runtime,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    if validate:
        _validate_runtime(runtime)
    resolution = ModelRouteResolution(route=route, runtime=runtime)
    return ModelRouteResolution(
        route=route,
        runtime=replace(runtime, route_audit_snapshot=resolution.audit_snapshot()),
    )


def model_route_audit_snapshot_for_stage(
    model: object,
    *,
    stage: str,
) -> ModelRouteAuditSnapshot | None:
    """从真实 Route Runtime 取得可落库的阶段快照。

    测试替身、确定性 Tool 或未经过显式 Route 解析的模型没有可审计配置，必须返回 ``None``；
    不能用当前全局配置补猜历史事实。阶段由调用方使用稳定内部标识提供。
    """

    if not isinstance(model, ModelRuntime) or model.route_audit_snapshot is None:
        return None
    return model.route_audit_snapshot.model_copy(update={"stage": stage})


def resolve_model_runtime_for_test(
    *,
    provider: str,
    base_url: str | None = None,
    model: str | None = None,
    thinking: str = "disabled",
    api_key: str | None = None,
) -> tuple[ModelRuntime, Literal["request", "local_config", "environment", "none"]]:
    """解析一次“测试连接”专用运行时。

    连接测试要验证用户正在编辑的表单内容，但不能把未确认的配置写进本地仓储。
    因此这里优先使用请求体字段，其次才回退到同 provider 的本地配置和环境变量。
    返回的 api_key_source 只用于 UI 提示来源，不暴露 Key 明文。
    """

    normalized_provider = normalize_model_provider(provider)
    profile = _profile_for(normalized_provider)
    stored_config = _load_stored_model_config()

    base_url_value = (base_url or "").strip().rstrip("/")
    if not base_url_value:
        base_url_value = _resolve_runtime_field(
            normalized_provider,
            "BASE_URL",
            config_value=_stored_field(stored_config, normalized_provider, "base_url"),
            settings_value=settings.llm_base_url,
            default=profile.default_base_url,
        ).rstrip("/")

    model_value = (model or "").strip()
    if not model_value:
        model_value = _resolve_runtime_field(
            normalized_provider,
            "MODEL",
            config_value=_stored_field(stored_config, normalized_provider, "model"),
            settings_value=settings.llm_model,
            default=profile.default_model or "",
        )

    api_key_value = (api_key or "").strip()
    api_key_source: Literal["request", "local_config", "environment", "none"] = "request"
    if not api_key_value:
        api_key_value, resolved_source = _resolve_runtime_field_with_source(
            normalized_provider,
            "API_KEY",
            config_value=_stored_api_key(stored_config, normalized_provider),
            settings_value=settings.llm_api_key,
        )
        if resolved_source == "local_config":
            api_key_source = "local_config"
        elif resolved_source == "environment":
            api_key_source = "environment"
        else:
            api_key_source = "none"

    thinking_value = thinking.strip().lower() if thinking else "disabled"
    if thinking_value not in {"enabled", "disabled"}:
        thinking_value = "disabled"

    runtime = ModelRuntime(
        provider=profile.provider,
        label=profile.label,
        transport=profile.transport,
        base_url=base_url_value,
        model=model_value,
        api_key=api_key_value,
        thinking=thinking_value,
        # 连接测试只需要一个很短的探测回复，限制 token/超时能减少等待和额度消耗。
        max_tokens=max(1, min(settings.llm_max_tokens, 64)),
        temperature=0.0,
        timeout_seconds=max(5.0, min(float(settings.llm_timeout_seconds), 30.0)),
        supports_thinking=profile.supports_thinking,
        sends_reasoning_effort=profile.sends_reasoning_effort,
        completion_tokens_field=profile.completion_tokens_field,
        sends_temperature=profile.sends_temperature,
        supports_json_output=profile.supports_json_output,
        supports_tool_calls=profile.supports_tool_calls,
        context_cache_mode=profile.context_cache_mode,
        context_cache_note=profile.context_cache_note,
    )

    _validate_runtime(runtime)
    return runtime, api_key_source


def resolve_model_runtime_for_provider(
    provider: str,
    *,
    thinking: str = "disabled",
) -> ModelRuntime:
    """为平台内部模型路由解析一个明确 provider，不改变全局默认配置。

    该入口复用每个 provider 已隔离保存的 Key，并使用其 profile 默认模型；适用于同一受控
    任务在供应商内容过滤或协议不兼容时切换一次后备模型。它不会测试连接或写回配置。
    """

    runtime, _ = resolve_model_runtime_for_test(
        provider=provider,
        thinking=thinking,
    )
    # 测试连接构造器会有意把响应压到 64 tokens、30 秒；若直接复用，数据抽取 JSON 会被
    # 截断并被误判为“模型不会返回结构”。内部路由需要沿用同一套 provider/Key 解析，
    # 但必须恢复正式任务预算。请求级 thinking 仍由调用方显式限制，不写回用户配置。
    return replace(
        runtime,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def _effective_provider(
    agent: AgentDescriptor | None,
    *,
    stored_config: StoredModelConfig,
) -> str:
    if agent is not None and agent.llm.provider and agent.llm.provider != "inherit":
        return normalize_model_provider(agent.llm.provider)

    if stored_config.provider:
        return normalize_model_provider(stored_config.provider)

    return normalize_model_provider(settings.llm_provider)


def _effective_model(
    agent: AgentDescriptor | None,
    *,
    provider: str,
    profile: ModelProviderProfile,
    stored_config: StoredModelConfig,
) -> str:
    if agent is not None and agent.llm.model and agent.llm.model != "inherit":
        return agent.llm.model

    return _resolve_runtime_field(
        provider,
        "MODEL",
        config_value=_stored_field(stored_config, provider, "model"),
        settings_value=settings.llm_model,
        default=profile.default_model or "",
    )


def _profile_for(provider: str) -> ModelProviderProfile:
    try:
        return _PROVIDER_PROFILES[provider]
    except KeyError as exc:
        supported = "、".join(profile.provider for profile in list_model_provider_profiles())
        raise ModelGatewayError(f"暂不支持模型供应商：{provider}。当前支持：{supported}") from exc


def _resolve_runtime_field(
    provider: str,
    field: str,
    *,
    config_value: str = "",
    settings_value: str = "",
    default: str = "",
) -> str:
    value, _source = _resolve_runtime_field_with_source(
        provider,
        field,
        config_value=config_value,
        settings_value=settings_value,
        default=default,
    )
    return value


def _resolve_runtime_field_with_source(
    provider: str,
    field: str,
    *,
    config_value: str = "",
    settings_value: str = "",
    default: str = "",
) -> tuple[str, Literal["local_config", "environment", "default"]]:
    if config_value and config_value.strip().lower() not in _PLACEHOLDER_VALUES:
        return config_value, "local_config"

    for prefix in _PROVIDER_ENV_PREFIXES.get(provider, ()):
        value = _usable_env_value(f"{prefix}_{field}")
        if value:
            return value, "environment"

    value = _usable_env_value(f"AGENTFLOW_LLM_{field}")
    if value:
        return value, "environment"

    # settings_value 目前只用于兼容早期 `DEEPSEEK_*` 变量。切到 OpenAI / Claude / Qwen
    # 时不能把残留的 DeepSeek Key 或模型名误当成新供应商配置；通用配置已经在上面的
    # `AGENTFLOW_LLM_*` 分支里处理过了。
    if provider == "deepseek" and settings_value and settings_value.strip().lower() not in _PLACEHOLDER_VALUES:
        return settings_value, "environment"
    return default, "default"


def _load_stored_model_config() -> StoredModelConfig:
    try:
        return load_model_config()
    except ModelConfigStoreError as exc:
        raise ModelGatewayError(str(exc)) from exc


def _stored_field(stored_config: StoredModelConfig, provider: str, field: str) -> str:
    if normalize_model_provider(stored_config.provider) != provider:
        return ""

    value = getattr(stored_config, field, "")
    return value if isinstance(value, str) else ""


def _stored_api_key(stored_config: StoredModelConfig, provider: str) -> str:
    if not stored_config.api_key_configured_for(provider):
        return ""

    try:
        return stored_config.decrypt_api_key(provider)
    except SecretStoreError as exc:
        raise ModelGatewayError(f"本地模型 API Key 解密失败：{exc}") from exc


def _usable_env_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value.lower() in _PLACEHOLDER_VALUES:
        return ""
    return value


def _validate_runtime(runtime: ModelRuntime) -> None:
    if not runtime.base_url:
        raise ModelGatewayError(f"未配置 {runtime.label} Base URL。")
    if not runtime.model:
        raise ModelGatewayError(f"未配置 {runtime.label} 模型名。")
    if not runtime.api_key_configured:
        raise ModelGatewayError(f"未配置 {runtime.label} API Key。")


def _anthropic_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _deepseek_anthropic_messages_url(base_url: str) -> str:
    """只接受 DeepSeek 官方域名，避免本地配置把 Key 发送到未知 Anthropic 代理。"""

    base = base_url.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or parsed.netloc.lower() != "api.deepseek.com":
        raise ModelGatewayError("DeepSeek 原生 Web Search 仅支持官方 https://api.deepseek.com 入口。")
    if not parsed.path or parsed.path == "/":
        base = f"{base}/anthropic"
    elif parsed.path.rstrip("/") != "/anthropic":
        raise ModelGatewayError("DeepSeek 原生 Web Search 需要官方 Anthropic-compatible Base URL。")
    return _anthropic_messages_url(base)


def _apply_openai_runtime_options(
    payload: dict[str, object],
    runtime: ModelRuntime,
    *,
    thinking_override: str | None = None,
) -> None:
    """把兼容协议共有的运行参数集中应用到两类 OpenAI 请求。

    ``thinking_override`` 只服务于已经验证过的单请求兼容策略，不能写回模型配置，也不供
    Agent 直接指定。这样 Provider 差异仍被限制在 ModelGateway 内。
    """

    effective_thinking = thinking_override or runtime.thinking
    if runtime.supports_thinking and effective_thinking in {"enabled", "disabled"}:
        # 仅明确声明支持的 profile 才会收到私有 thinking 字段。
        payload["thinking"] = {"type": effective_thinking}
    if runtime.sends_reasoning_effort and effective_thinking == "enabled":
        payload["reasoning_effort"] = "high"
    elif runtime.sends_temperature:
        payload["temperature"] = runtime.temperature


def _openai_tool_messages(
    *,
    system_prompt: str,
    messages: list[ModelConversationMessage],
    preserve_reasoning: bool = False,
    require_tool_call_content: bool = False,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for message in messages:
        if message.role == "tool":
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
            continue
        if message.role == "assistant":
            # DeepSeek V4 思考模式的 Tool Call 回放不能把 content 写成 null。普通兼容接口
            # 允许 null，所以该限制通过显式参数只作用于对应的 Provider/模式组合。
            assistant_content: str | None = message.content or None
            if require_tool_call_content and message.tool_calls:
                assistant_content = message.content
            assistant: dict[str, Any] = {"role": "assistant", "content": assistant_content}
            if preserve_reasoning and message.reasoning_content:
                assistant["reasoning_content"] = message.reasoning_content
            if message.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            payload.append(assistant)
            continue
        payload.append({"role": "user", "content": message.content})
    return payload


def _anthropic_tool_messages(messages: list[ModelConversationMessage]) -> list[dict[str, Any]]:
    """转换内部会话，并把连续 Tool 结果合并为 Anthropic 要求的 user 消息。"""

    payload: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            payload.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if message.role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            )
            continue

        flush_results()
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            payload.append({"role": "assistant", "content": blocks})
        else:
            payload.append({"role": "user", "content": [{"type": "text", "text": message.content}]})
    flush_results()
    return payload


def _extract_openai_tool_turn(data: dict[str, Any]) -> ModelToolTurn:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelGatewayError("模型接口响应格式不符合 OpenAI-compatible Tool Calls 预期。") from exc
    if not isinstance(choice, dict) or not isinstance(message, dict):
        raise ModelGatewayError("OpenAI-compatible 模型消息不是 JSON object。")

    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise ModelGatewayError("OpenAI-compatible 模型 tool_calls 不是 array。")
    calls: list[ModelToolCall] = []
    for index, raw_call in enumerate(raw_calls, start=1):
        if not isinstance(raw_call, dict):
            raise ModelGatewayError("OpenAI-compatible 模型返回了无效工具调用。")
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise ModelGatewayError("OpenAI-compatible 工具调用缺少 function 字段。")
        name = function.get("name")
        raw_arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not name:
            raise ModelGatewayError("OpenAI-compatible 工具调用缺少函数名。")
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as exc:
            raise ModelGatewayError("OpenAI-compatible 工具调用参数不是合法 JSON。") from exc
        if not isinstance(arguments, dict):
            raise ModelGatewayError("OpenAI-compatible 工具调用参数必须是 JSON object。")
        call_id = raw_call.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"openai_tool_{index}"
        calls.append(ModelToolCall(call_id=call_id, name=name, arguments=arguments))

    content = message.get("content")
    reasoning_content = message.get("reasoning_content")
    finish_reason = choice.get("finish_reason")
    return ModelToolTurn(
        content=content.strip() if isinstance(content, str) else "",
        tool_calls=tuple(calls),
        reasoning_content=reasoning_content.strip() if isinstance(reasoning_content, str) else "",
        finish_reason=finish_reason.strip() if isinstance(finish_reason, str) else "",
        usage=extract_model_usage_metrics(data),
    )


def _extract_anthropic_tool_turn(data: dict[str, Any]) -> ModelToolTurn:
    content = data.get("content")
    if not isinstance(content, list):
        raise ModelGatewayError("模型接口响应格式不符合 Anthropic Tool Use 预期。")
    text_parts: list[str] = []
    calls: list[ModelToolCall] = []
    for index, block in enumerate(content, start=1):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
        elif block_type == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            arguments = block.get("input")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"anthropic_tool_{index}"
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                raise ModelGatewayError("Anthropic 工具调用缺少名称或 JSON object 参数。")
            calls.append(ModelToolCall(call_id=call_id, name=name, arguments=arguments))
    return ModelToolTurn(
        content="\n".join(text_parts),
        tool_calls=tuple(calls),
        usage=extract_model_usage_metrics(data),
    )


def extract_model_usage_metrics(data: dict[str, Any]) -> ModelUsageMetrics:
    """从不同供应商的 ``usage`` object 读取可比较的 token 计量。

    这是一层只读兼容逻辑：没有响应 usage、字段形态未知或值非法时保持 ``None``，绝不根据
    prompt 文本长度估算缓存命中。DeepSeek 的 hit/miss、OpenAI 的 ``cached_tokens`` 与
    Anthropic 的 read/creation 字段会被分别映射，便于后续任务指标和成本层按真实信息处理。
    """

    raw_usage = data.get("usage")
    if not isinstance(raw_usage, dict):
        return ModelUsageMetrics()

    prompt_tokens = _usage_int(raw_usage.get("prompt_tokens"))
    input_tokens = _usage_int(raw_usage.get("input_tokens"))
    output_tokens = _first_usage_int(
        raw_usage.get("completion_tokens"),
        raw_usage.get("output_tokens"),
    )
    total_tokens = _usage_int(raw_usage.get("total_tokens"))

    prompt_details = raw_usage.get("prompt_tokens_details")
    input_details = raw_usage.get("input_tokens_details")
    cached_detail_tokens = _first_usage_int(
        prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None,
        input_details.get("cached_tokens") if isinstance(input_details, dict) else None,
    )
    deepseek_cache_hit = _usage_int(raw_usage.get("prompt_cache_hit_tokens"))
    deepseek_cache_miss = _usage_int(raw_usage.get("prompt_cache_miss_tokens"))
    anthropic_cache_read = _usage_int(raw_usage.get("cache_read_input_tokens"))
    anthropic_cache_creation = _usage_int(raw_usage.get("cache_creation_input_tokens"))

    cache_read = _first_usage_int(
        deepseek_cache_hit,
        cached_detail_tokens,
        anthropic_cache_read,
    )
    cache_observed = any(
        value is not None
        for value in (
            deepseek_cache_hit,
            deepseek_cache_miss,
            cached_detail_tokens,
            anthropic_cache_read,
            anthropic_cache_creation,
        )
    )

    # Anthropic 的 input_tokens 不包含 cache read/creation；官方 usage 口径要求三者相加。
    # 其它 OpenAI-compatible 形态的 prompt/input tokens 已是完整输入计量，不要二次相加。
    if anthropic_cache_read is not None or anthropic_cache_creation is not None:
        normalized_input = sum(
            value
            for value in (input_tokens, anthropic_cache_read, anthropic_cache_creation)
            if value is not None
        )
    else:
        normalized_input = _first_usage_int(prompt_tokens, input_tokens)

    if total_tokens is None and normalized_input is not None and output_tokens is not None:
        total_tokens = normalized_input + output_tokens

    return ModelUsageMetrics(
        input_tokens=normalized_input,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=anthropic_cache_creation,
        cache_miss_input_tokens=deepseek_cache_miss,
        usage_observation="reported",
        cache_observation="reported" if cache_observed else "not_reported",
    )


def _usage_int(value: object) -> int | None:
    """接受供应商 JSON 的非负整数，拒绝 bool、负数和字符串数字。"""

    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _first_usage_int(*values: object) -> int | None:
    for value in values:
        normalized = _usage_int(value)
        if normalized is not None:
            return normalized
    return None


def _extract_native_web_search_sources(data: dict[str, Any]) -> tuple[NativeWebSearchSource, ...]:
    """从 Anthropic-compatible 的 ``web_search_tool_result`` 中提取并去重候选来源。

    原始返回允许携带加密正文，但业务层只需要可信 URL 的候选集合。URL 仍会在
    ResearchGateway 重新检查 HTTPS、公开域名和页面大小，不能因为供应商返回而直接信任。
    """

    content = data.get("content")
    if not isinstance(content, list):
        raise ModelGatewayError("原生 Web Search 响应缺少内容块列表。")
    sources: list[NativeWebSearchSource] = []
    seen: set[str] = set()
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
            continue
        results = block.get("content")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict) or result.get("type") != "web_search_result":
                continue
            title = result.get("title")
            raw_url = result.get("url")
            if not isinstance(title, str) or not isinstance(raw_url, str):
                continue
            url = raw_url.strip()
            parsed = urlparse(url)
            if not title.strip() or parsed.scheme != "https" or not parsed.netloc:
                continue
            key = url.casefold()
            if key in seen:
                continue
            seen.add(key)
            sources.append(NativeWebSearchSource(title=" ".join(title.split())[:180], url=url))
            if len(sources) >= 12:
                return tuple(sources)
    if not sources:
        raise ModelGatewayError("原生 Web Search 没有返回可读取的 HTTPS 来源。")
    return tuple(sources)


def _require_text_content(content: object) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ModelGatewayError("模型返回了空回复。")
    return content.strip()


def _extract_anthropic_text(data: dict) -> str:
    content = data.get("content")
    if isinstance(content, str):
        return _require_text_content(content)

    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
        if text_parts:
            return "\n".join(text_parts)

    raise ModelGatewayError("模型接口响应格式不符合 Anthropic Messages API 预期。")
