"""总指挥使用的 LGM2 公开资料 MCP 服务。

这里负责连接开关、固定 Tool 名称、MCP 结果契约和来源验证；它不把 MCP 返回正文直接交给
客户或模型。只有通过这层结果验证的数据才进入 Workflow Runtime 的 DeliveryCard/任务审计。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.config import settings
from app.mcp.connection_store import load_public_reference_connection
from app.mcp.contracts import McpGatewayError, McpToolResult
from app.mcp.gateway import McpGateway


PUBLIC_REFERENCE_TOOL_NAME = "search_wikimedia"
_MAX_QUERY_LENGTH = 140
_MAX_SOURCES = 3


class PublicReferenceError(RuntimeError):
    """公开资料连接或其受控结果不满足交付契约。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _PublicReferenceSourcePayload(BaseModel):
    source_id: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=2, max_length=_MAX_QUERY_LENGTH)
    title: str = Field(min_length=1, max_length=160)
    page_url: str = Field(min_length=1, max_length=360)
    snippet: str = Field(default="", max_length=240)
    retrieved_at: str = Field(min_length=10, max_length=40)
    provider: str = Field(default="wikimedia", max_length=40)
    scope: str = Field(default="public_reference_only", max_length=60)

    @field_validator("page_url")
    @classmethod
    def validate_fixed_origin(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "zh.wikipedia.org" or not parsed.path.startswith("/wiki/"):
            raise ValueError("来源 URL 不在允许的 Wikimedia 页面范围内。")
        return value

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        if value != "wikimedia":
            raise ValueError("来源 Provider 不匹配。")
        return value

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        if value != "public_reference_only":
            raise ValueError("来源范围不匹配。")
        return value


class _PublicReferencePayload(BaseModel):
    query: str = Field(min_length=2, max_length=_MAX_QUERY_LENGTH)
    sources: list[_PublicReferenceSourcePayload] = Field(default_factory=list, max_length=_MAX_SOURCES)
    warnings: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("warnings", mode="before")
    @classmethod
    def normalize_warnings(cls, value: object) -> object:
        if isinstance(value, str):
            return [value[:240]]
        if isinstance(value, list):
            return [str(item)[:240] for item in value[:3]]
        return []


@dataclass(frozen=True)
class PublicReferenceSource:
    source_id: str
    title: str
    page_url: str
    snippet: str
    retrieved_at: str

    def to_customer_value(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "page_url": self.page_url,
            "snippet": self.snippet,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True)
class PublicReferenceResolution:
    query: str
    sources: tuple[PublicReferenceSource, ...]
    warnings: tuple[str, ...]
    tool_name: str
    duration_ms: int
    request_bytes: int

    @property
    def completed(self) -> bool:
        return bool(self.sources)

    def runtime_result(self) -> dict[str, object]:
        source_lines = [
            f"- [{item.title}]({item.page_url})：{item.snippet or '可打开页面查看原文'}"
            for item in self.sources
        ]
        reply = (
            "## 公开资料参考\n\n"
            f"已按“{self.query}”检索到 {len(self.sources)} 条可回溯参考线索。\n\n"
            "### 可打开的来源\n"
            + "\n".join(source_lines)
            + "\n\n> 这些内容仅用于公开资料参考与后续人工核对，不自动构成已核验的专业事实或统计结论。"
        )
        return {
            "query": self.query,
            "sources": [item.to_customer_value() for item in self.sources],
            "source_count": len(self.sources),
            "warnings": list(self.warnings),
            "tool_name": self.tool_name,
            "scope": "public_reference_only",
            "fact_boundary": "公开资料仅作为可回溯参考线索，不自动构成已核验的专业事实或统计结论。",
            "reply": reply,
        }


async def search_public_references(
    query: str,
    *,
    gateway_factory: Callable[[], McpGateway] = McpGateway.for_public_reference,
) -> PublicReferenceResolution:
    """执行一次已批准的固定 Tool 调用并验证来源边界。"""

    normalized_query = " ".join(query.split())[:_MAX_QUERY_LENGTH]
    if len(normalized_query) < 2:
        raise PublicReferenceError("invalid_parameters", "公开资料检索词至少需要两个字符。")
    if not settings.mcp_enabled:
        raise PublicReferenceError("mcp_disabled", "MCP 平台连接已被部署配置关闭。")
    connection = load_public_reference_connection()
    if not connection.enabled:
        raise PublicReferenceError(
            "mcp_disabled",
            "Wikimedia 公开资料连接尚未启用；请先在插件管理中启用并确认联网范围。",
        )

    gateway = gateway_factory()
    try:
        tools = await gateway.discover_tools()
        tool_names = {tool.reference.tool_name for tool in tools}
        if tool_names != {PUBLIC_REFERENCE_TOOL_NAME}:
            raise PublicReferenceError("mcp_tool_schema_invalid", "公开资料连接返回了未批准的 Tool 目录。")
        result = await gateway.call_tool(
            PUBLIC_REFERENCE_TOOL_NAME,
            {"query": normalized_query, "limit": _MAX_SOURCES},
        )
        return _verify_tool_result(result, expected_query=normalized_query, audit_events=gateway.audit_snapshot())
    except McpGatewayError as exc:
        raise PublicReferenceError(
            exc.code,
            "公开资料连接暂时不可用；本次没有使用外部结果。",
            retryable=exc.code in {"mcp_connection_failed", "mcp_server_exited", "mcp_tool_timeout"},
        ) from exc
    finally:
        await gateway.close()


def search_public_references_sync(query: str) -> PublicReferenceResolution:
    """供现有同步 Workflow Runtime 线程调用的受控入口。"""

    return asyncio.run(search_public_references(query))


def _verify_tool_result(
    result: McpToolResult,
    *,
    expected_query: str,
    audit_events: tuple[Any, ...],
) -> PublicReferenceResolution:
    if result.reference.server.server_id != "public-reference" or result.reference.tool_name != PUBLIC_REFERENCE_TOOL_NAME:
        raise PublicReferenceError("mcp_tool_result_rejected", "公开资料 Tool 标识不匹配。")
    if result.is_error:
        raise PublicReferenceError("mcp_tool_result_rejected", "公开资料服务返回了受控错误结果。")
    try:
        payload = _PublicReferencePayload.model_validate(result.structured_content)
    except ValidationError as exc:
        raise PublicReferenceError("mcp_tool_result_rejected", "公开资料返回内容未通过来源契约校验。") from exc
    if payload.query != expected_query:
        raise PublicReferenceError("mcp_tool_result_rejected", "公开资料返回的检索词与已批准请求不一致。")
    source_ids = [source.source_id for source in payload.sources]
    if len(source_ids) != len(set(source_ids)):
        raise PublicReferenceError("mcp_tool_result_rejected", "公开资料返回了重复来源。")
    completed = next((event for event in reversed(audit_events) if event.event_type == "tool_call_completed"), None)
    return PublicReferenceResolution(
        query=payload.query,
        sources=tuple(
            PublicReferenceSource(
                source_id=item.source_id,
                title=item.title,
                page_url=item.page_url,
                snippet=item.snippet,
                retrieved_at=item.retrieved_at,
            )
            for item in payload.sources
        ),
        warnings=tuple(payload.warnings),
        tool_name=result.reference.qualified_name,
        duration_ms=completed.duration_ms if completed is not None else 0,
        request_bytes=completed.request_bytes if completed is not None else 0,
    )
