from __future__ import annotations

from app.database.conversation_repository import (
    ConversationHistoryRecallItem,
    find_conversation_history_recall_items,
)


_LOOKBACK_SIGNALS = (
    "我叫你",
    "我让你",
    "我之前",
    "我以前",
    "之前我",
    "上次",
    "之前",
    "以前",
    "历史记录",
    "聊天记录",
    "对话记录",
    "会话记录",
)
_RECALL_QUESTION_SIGNALS = (
    "什么",
    "哪些",
    "哪份",
    "哪次",
    "回顾",
    "回忆",
    "查一下",
    "查下",
    "看一下",
    "列一下",
    "列出",
    "总结一下",
    "记录",
)
_TOPIC_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PPT", ("ppt", "pptx", "演示文稿", "幻灯片", "幻灯")),
    ("图表", ("折线图", "柱状图", "饼图", "图表")),
    ("文档", ("word", "docx", "pdf", "文档")),
)
_MAX_RECALLED_ITEMS = 5
_MAX_EXCERPT_LENGTH = 150


def is_conversation_history_recall_request(message: str) -> bool:
    """识别“回顾之前做过什么”而非“现在开始做什么”的显式问题。"""

    normalized = " ".join(message.strip().lower().split())
    if not normalized:
        return False
    return any(signal in normalized for signal in _LOOKBACK_SIGNALS) and any(
        signal in normalized for signal in _RECALL_QUESTION_SIGNALS
    )


def build_conversation_history_recall_reply(*, message: str, project_scope: str) -> str:
    """从同一项目范围的会话归档构建受限的只读历史回顾答复。"""

    topic_label, query_terms = _topic_for_message(message)
    items = find_conversation_history_recall_items(
        project_scope=project_scope,
        query_terms=query_terms,
        excluded_content=message,
        limit=_MAX_RECALLED_ITEMS + 3,
    )
    unique_items = _unique_items(items)
    if not unique_items:
        suffix = f"与 {topic_label} 有关的" if topic_label else "相关的"
        return f"在当前范围内已保存的会话请求中，没有找到{suffix}历史记录。"

    heading = f"我查到你之前提过这些与 {topic_label} 有关的内容：" if topic_label else "我查到你之前提过这些内容："
    lines = [heading]
    for index, item in enumerate(unique_items, start=1):
        lines.append(f"{index}. {_excerpt(item.content)}")
    lines.append("以上来自当前范围内的已保存会话请求；本次已按只读回顾处理。")
    return "\n".join(lines)


def _topic_for_message(message: str) -> tuple[str, tuple[str, ...]]:
    normalized = message.lower()
    for label, aliases in _TOPIC_GROUPS:
        if any(alias in normalized for alias in aliases):
            return label, aliases
    return "", ()


def _unique_items(items: list[ConversationHistoryRecallItem]) -> list[ConversationHistoryRecallItem]:
    unique: list[ConversationHistoryRecallItem] = []
    seen_contents: set[str] = set()
    for item in items:
        normalized = " ".join(item.content.split())
        if (
            not normalized
            or normalized in seen_contents
            or is_conversation_history_recall_request(normalized)
        ):
            continue
        seen_contents.add(normalized)
        unique.append(item)
        if len(unique) >= _MAX_RECALLED_ITEMS:
            break
    return unique


def _excerpt(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= _MAX_EXCERPT_LENGTH:
        return normalized
    return f"{normalized[: _MAX_EXCERPT_LENGTH - 1].rstrip()}..."
