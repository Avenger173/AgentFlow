"""MCP Tool 描述、参数与结果的受控裁剪。

MCP Server 的 schema、描述和返回内容都视为外部不可信输入。LGM1 不把它们交给模型或
写入任务历史，但先固定同一份结构、大小和脱敏边界，避免 LGM2 再出现两套协议。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from app.mcp.contracts import (
    McpGatewayError,
    McpServerReference,
    McpToolDescriptor,
    McpToolReference,
    McpToolResult,
)


_MAX_SCHEMA_BYTES = 12_000
_MAX_ARGUMENT_BYTES = 8_000
_MAX_RESULT_BYTES = 12_000
_MAX_JSON_DEPTH = 8
_MAX_COLLECTION_ITEMS = 128
_MAX_TEXT_CHARS = 8_000
_SECRET_VALUE_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "token"}
_SECRET_TOKEN_PATTERN = re.compile(r"(?i)\b(?:sk|ark|ak)-[a-z0-9_-]{8,}\b")
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]{8,}"
)


def normalize_tool_descriptors(
    server: McpServerReference,
    raw_tools: Iterable[Any],
) -> tuple[McpToolDescriptor, ...]:
    """将 SDK 返回的 Tool 定义裁剪为可审计的本地目录。"""

    descriptors: list[McpToolDescriptor] = []
    seen_names: set[str] = set()
    for raw_tool in raw_tools:
        tool_name = _tool_value(raw_tool, "name")
        if not isinstance(tool_name, str):
            raise McpGatewayError("mcp_tool_schema_invalid", "MCP Tool 缺少合法名称。")
        try:
            reference = McpToolReference(server=server, tool_name=tool_name)
        except ValueError as error:
            raise McpGatewayError("mcp_tool_schema_invalid", "MCP Tool 名称不符合 AgentFlow 规范。") from error
        if reference.tool_name in seen_names:
            raise McpGatewayError("mcp_tool_schema_invalid", "MCP Tool 目录存在重名项。")
        seen_names.add(reference.tool_name)

        title = _display_text(_tool_value(raw_tool, "title") or tool_name, limit=160)
        description = _display_text(_tool_value(raw_tool, "description") or "", limit=1_200)
        input_schema = _guard_schema(_tool_value(raw_tool, "input_schema"))
        output_schema = _optional_schema(_tool_value(raw_tool, "output_schema"))
        descriptors.append(
            McpToolDescriptor(
                reference=reference,
                title=title,
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
            )
        )
    if not descriptors:
        raise McpGatewayError("mcp_tool_schema_invalid", "MCP 服务没有返回可用 Tool。")
    return tuple(descriptors)


def validate_tool_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """拒绝不可序列化、超深或超大的 Tool 参数。"""

    if not isinstance(arguments, Mapping):
        raise McpGatewayError("mcp_tool_arguments_invalid", "MCP Tool 参数必须是 JSON object。")
    return _bounded_json_object(
        dict(arguments),
        max_bytes=_MAX_ARGUMENT_BYTES,
        error_code="mcp_tool_arguments_invalid",
        message="MCP Tool 参数超出允许的 JSON 边界。",
    )


def guard_tool_result(reference: McpToolReference, raw_result: Any) -> McpToolResult:
    """仅保留文本与 JSON object 结果，裁剪其它 MCP content block。"""

    raw_content = _tool_value(raw_result, "content") or ()
    block_types: list[str] = []
    text_parts: list[str] = []
    for block in raw_content:
        block_type = str(_tool_value(block, "type") or type(block).__name__).strip().lower()
        if len(block_types) < 12:
            block_types.append(block_type[:48])
        if block_type == "text":
            text = _tool_value(block, "text")
            if isinstance(text, str):
                text_parts.append(_redact_text(text))

    text = "\n".join(part for part in text_parts if part).strip()
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
        truncated = True
    else:
        truncated = False

    raw_structured = _tool_value(raw_result, "structured_content")
    if raw_structured is None:
        structured_content: dict[str, Any] = {}
    elif isinstance(raw_structured, Mapping):
        structured_content = _bounded_json_object(
            _redact_json(dict(raw_structured)),
            max_bytes=_MAX_RESULT_BYTES,
            error_code="mcp_result_too_large",
            message="MCP Tool 结构化结果超出允许边界。",
        )
    else:
        raise McpGatewayError("mcp_tool_result_rejected", "MCP Tool 结构化结果必须是 JSON object。")

    result_bytes = _json_byte_size({"text": text, "structured_content": structured_content})
    if result_bytes > _MAX_RESULT_BYTES:
        raise McpGatewayError("mcp_result_too_large", "MCP Tool 返回内容超过允许大小。")
    return McpToolResult(
        reference=reference,
        text=text,
        structured_content=structured_content,
        content_block_types=block_types,
        is_error=bool(_tool_value(raw_result, "is_error")),
        result_truncated=truncated,
    )


def tool_result_size_bytes(result: McpToolResult) -> int:
    """返回已裁剪结果的 JSON 字节数，供无正文审计使用。"""

    return _json_byte_size(result.model_dump(mode="json"))


def _guard_schema(value: Any) -> dict[str, Any]:
    schema = _bounded_json_object(
        value,
        max_bytes=_MAX_SCHEMA_BYTES,
        error_code="mcp_schema_too_large",
        message="MCP Tool schema 超出允许边界。",
    )
    if schema.get("type") != "object":
        raise McpGatewayError("mcp_tool_schema_invalid", "MCP Tool 输入 schema 根节点必须是 object。")
    return schema


def _optional_schema(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _bounded_json_object(
        value,
        max_bytes=_MAX_SCHEMA_BYTES,
        error_code="mcp_schema_too_large",
        message="MCP Tool 输出 schema 超出允许边界。",
    )


def _bounded_json_object(
    value: Any,
    *,
    max_bytes: int,
    error_code: str,
    message: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise McpGatewayError("mcp_tool_schema_invalid", "MCP JSON schema 必须是 object。")
    _validate_json_shape(value, depth=0)
    try:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise McpGatewayError(error_code, message) from error
    if len(serialized.encode("utf-8")) > max_bytes:
        raise McpGatewayError(error_code, message)
    return json.loads(serialized)


def _validate_json_shape(value: Any, *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise McpGatewayError("mcp_tool_result_rejected", "MCP JSON 层级超过允许深度。")
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise McpGatewayError("mcp_tool_result_rejected", "MCP JSON object 字段过多。")
        for key, nested in value.items():
            if not isinstance(key, str) or len(key) > 160:
                raise McpGatewayError("mcp_tool_result_rejected", "MCP JSON key 不合法。")
            _validate_json_shape(nested, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise McpGatewayError("mcp_tool_result_rejected", "MCP JSON 列表元素过多。")
        for nested in value:
            _validate_json_shape(nested, depth=depth + 1)
        return
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    raise McpGatewayError("mcp_tool_result_rejected", "MCP JSON 包含不支持的数据类型。")


def _tool_value(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _display_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return "未命名 MCP Tool"
    normalized = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return _redact_text(normalized)[:limit] or "未命名 MCP Tool"


def _redact_text(value: str) -> str:
    return _AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", _SECRET_TOKEN_PATTERN.sub("[REDACTED]", value))


def _redact_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SECRET_VALUE_KEYS else _redact_json(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _json_byte_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
