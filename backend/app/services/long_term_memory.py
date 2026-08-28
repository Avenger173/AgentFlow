from __future__ import annotations

import re
from collections.abc import Iterable

from app.schemas.memory import LongTermMemoryRecord


class LongTermMemorySafetyError(ValueError):
    """长期记忆内容越过隐私或范围边界时抛出。"""


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|ark)-[a-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|secret[_ -]?key)\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._-]{12,}"),
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
)
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?i)(?:\b[a-z]:[\\/]|\\\\[^\\/]+[\\/]|(?:^|\s)/(?:home|users|var|etc|tmp)/)")
_SCOPE_PATTERN = re.compile(r"^(?:global|project:[a-z0-9][a-z0-9_-]{0,63})$")
_TASK_ID_PATTERN = re.compile(r"^task_[a-z0-9_]{4,156}$", re.IGNORECASE)


def normalize_memory_scope(value: str) -> str:
    """规范全局或项目级范围，拒绝把文件路径伪装成项目标识。"""

    scope = " ".join(value.strip().lower().split())
    if not _SCOPE_PATTERN.fullmatch(scope):
        raise LongTermMemorySafetyError("记忆范围只能是 global 或 project:项目标识（英文、数字、_、-）。")
    return scope


def sanitize_memory_text(value: str, *, field_name: str, maximum: int) -> str:
    """压缩用户确认文本，并阻止敏感凭据、私钥与绝对路径进入长期存储。"""

    normalized = " ".join(value.strip().split())
    if not normalized:
        raise LongTermMemorySafetyError(f"{field_name} 不能为空。")
    if len(normalized) < 2:
        raise LongTermMemorySafetyError(f"{field_name} 至少需要 2 个字符。")
    if len(normalized) > maximum:
        raise LongTermMemorySafetyError(f"{field_name} 超过允许长度，建议先压缩成稳定事实。")
    if _ABSOLUTE_PATH_PATTERN.search(normalized):
        raise LongTermMemorySafetyError(f"{field_name} 不能包含绝对路径，请改写为项目内的抽象约束。")
    if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        raise LongTermMemorySafetyError(f"{field_name} 不能包含密钥、令牌或私钥。")
    return normalized


def normalize_memory_source_task_id(value: str | None) -> str | None:
    """来源只能是已有任务标识，不接受任意备注或路径替代审计来源。"""

    task_id = (value or "").strip()
    if not task_id:
        return None
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise LongTermMemorySafetyError("记忆来源必须是有效的 task_id，不能填写文件路径或自由文本。")
    return task_id


def normalize_memory_tags(values: Iterable[str]) -> list[str]:
    """保留小而稳定的标签集合，供后续精确检索而不是存放另一份正文。"""

    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = " ".join(str(value).strip().split()).lower()
        if not tag:
            continue
        if len(tag) > 32:
            raise LongTermMemorySafetyError("记忆标签不能超过 32 个字符。")
        if _ABSOLUTE_PATH_PATTERN.search(tag) or any(pattern.search(tag) for pattern in _SECRET_PATTERNS):
            raise LongTermMemorySafetyError("记忆标签不能包含路径或敏感凭据。")
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    if len(tags) > 8:
        raise LongTermMemorySafetyError("每条记忆最多保留 8 个标签。")
    return tags


def build_memory_context_summary(records: Iterable[LongTermMemoryRecord]) -> list[str]:
    """生成给计划审计与模型提示的最小上下文，不回传整段历史或源文件。"""

    items: list[str] = []
    for item in records:
        items.append(f"{item.title}：{item.summary}")
        if len(items) >= 3:
            break
    return items
