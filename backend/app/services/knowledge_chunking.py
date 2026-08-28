"""知识库 K1.3 的确定性父子分块器。

这里不调用模型，也不决定检索排序。它只把受控解析文本拆成可回读的父块和精确匹配子块，
并让每一块保留原始字符范围、标题路径和既有解析器给出的页/段落/行定位。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Literal

from app.services.workspace_documents import (
    ParsedControlledDocument,
    SourceKind,
    source_location_for_range,
)


SPLITTER_PROFILE_VERSION = "parent_child_v1"
PARENT_TARGET_CHARS = 6_000
PARENT_MAX_CHARS = 9_000
CHILD_TARGET_CHARS = 1_200
CHILD_MIN_CHARS = 320
CHILD_OVERLAP_CHARS = 160


@dataclass(frozen=True)
class ChunkDraft:
    """尚未写入数据库的一段可追溯文本；不携带文件路径或客户身份信息。"""

    ordinal: int
    start_char: int
    end_char: int
    content: str
    content_sha256: str
    source_kind: SourceKind
    source_locator: str
    heading_path: tuple[str, ...]


@dataclass(frozen=True)
class ParentChildChunkDrafts:
    """一个文档版本的父块与子块草案，所有范围均相对于解析后的完整文本。"""

    extracted_char_count: int
    parents: tuple[ChunkDraft, ...]
    children: tuple[ChunkDraft, ...]


def build_parent_child_chunks(parsed: ParsedControlledDocument) -> ParentChildChunkDrafts:
    """按结构优先、长度兜底的规则建立父子块。

    Markdown 优先按一级/二级标题组织父块；无标题文本、PDF 与 DOCX 则优先使用自然段或解析
    段/页边界。子块只在各父块内部滑动，避免精确命中跨越不相关章节。
    """

    text = parsed.text
    if not text.strip():
        return ParentChildChunkDrafts(extracted_char_count=0, parents=(), children=())

    parent_ranges = _parent_ranges(parsed)
    parents: list[ChunkDraft] = []
    children: list[ChunkDraft] = []
    child_ordinal = 1
    for parent_ordinal, (start_char, end_char, heading_path) in enumerate(parent_ranges, start=1):
        parent = _draft(
            parsed=parsed,
            ordinal=parent_ordinal,
            start_char=start_char,
            end_char=end_char,
            heading_path=heading_path,
        )
        parents.append(parent)
        for child_start, child_end in _child_ranges(text, start_char, end_char):
            children.append(
                _draft(
                    parsed=parsed,
                    ordinal=child_ordinal,
                    start_char=child_start,
                    end_char=child_end,
                    heading_path=heading_path,
                )
            )
            child_ordinal += 1
    return ParentChildChunkDrafts(
        extracted_char_count=len(text),
        parents=tuple(parents),
        children=tuple(children),
    )


def _parent_ranges(parsed: ParsedControlledDocument) -> list[tuple[int, int, tuple[str, ...]]]:
    text = parsed.text
    if parsed.document_type == "text":
        markdown_ranges = _markdown_section_ranges(text)
        if markdown_ranges:
            return _expand_ranges(text, markdown_ranges)
        return _expand_ranges(text, [(0, len(text), ())])

    if parsed.segments:
        grouped: list[tuple[int, int, tuple[str, ...]]] = []
        start = parsed.segments[0].start_char
        end = start
        for segment in parsed.segments:
            if end > start and segment.end_char - start > PARENT_TARGET_CHARS:
                grouped.append((start, end, ()))
                start = segment.start_char
            end = segment.end_char
            if end - start >= PARENT_MAX_CHARS:
                grouped.append((start, end, ()))
                start = end
        if end > start:
            grouped.append((start, end, ()))
        return _expand_ranges(text, grouped)
    return _expand_ranges(text, [(0, len(text), ())])


def _markdown_section_ranges(text: str) -> list[tuple[int, int, tuple[str, ...]]]:
    """用一级/二级标题形成稳定父块，三级以下标题保留在所属父块文本中。"""

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
    stack: list[str] = []
    sections: list[tuple[int, tuple[str, ...]]] = []
    for match in heading_pattern.finditer(text):
        level = len(match.group(1))
        title = match.group(2).strip().rstrip("#").strip()
        if not title:
            continue
        stack = stack[: level - 1]
        stack.append(title)
        if level <= 2:
            sections.append((match.start(), tuple(stack)))
    if not sections:
        return []
    if sections[0][0] > 0 and text[: sections[0][0]].strip():
        sections.insert(0, (0, ("未命名开头",)))
    return [
        (start, sections[index + 1][0] if index + 1 < len(sections) else len(text), heading_path)
        for index, (start, heading_path) in enumerate(sections)
        if text[start : sections[index + 1][0] if index + 1 < len(sections) else len(text)].strip()
    ]


def _expand_ranges(
    text: str,
    ranges: list[tuple[int, int, tuple[str, ...]]],
) -> list[tuple[int, int, tuple[str, ...]]]:
    """对过长父块再按自然换行切开，保证任何单个章节不会放大后续上下文。"""

    expanded: list[tuple[int, int, tuple[str, ...]]] = []
    for start_char, end_char, heading_path in ranges:
        cursor = start_char
        while cursor < end_char:
            preferred_end = min(end_char, cursor + PARENT_MAX_CHARS)
            if preferred_end < end_char:
                split_end = _preferred_boundary(text, cursor, preferred_end, minimum=PARENT_TARGET_CHARS // 2)
            else:
                split_end = preferred_end
            if split_end <= cursor:
                split_end = preferred_end
            if text[cursor:split_end].strip():
                expanded.append((cursor, split_end, heading_path))
            cursor = split_end
    return expanded


def _child_ranges(text: str, parent_start: int, parent_end: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = parent_start
    while cursor < parent_end:
        preferred_end = min(parent_end, cursor + CHILD_TARGET_CHARS)
        end_char = (
            _preferred_boundary(text, cursor, preferred_end, minimum=CHILD_MIN_CHARS)
            if preferred_end < parent_end
            else preferred_end
        )
        if end_char <= cursor:
            end_char = preferred_end
        if text[cursor:end_char].strip():
            ranges.append((cursor, end_char))
        if end_char >= parent_end:
            break
        # 重叠只保留在同一父块内，并从上一段的自然结束点回退，避免断字和无限循环。
        next_cursor = max(cursor + 1, end_char - CHILD_OVERLAP_CHARS)
        if next_cursor < end_char:
            next_cursor = _next_boundary(text, next_cursor, end_char)
        cursor = min(next_cursor, parent_end)
    return ranges


def _preferred_boundary(text: str, start: int, preferred_end: int, *, minimum: int) -> int:
    floor = min(preferred_end, start + minimum)
    for marker in ("\n\n", "\n", "。", "；", ". ", "; "):
        position = text.rfind(marker, floor, preferred_end)
        if position >= floor:
            return position + len(marker)
    return preferred_end


def _next_boundary(text: str, candidate: int, ceiling: int) -> int:
    newline = text.find("\n", candidate, ceiling)
    if newline >= 0:
        return newline + 1
    return candidate


def _draft(
    *,
    parsed: ParsedControlledDocument,
    ordinal: int,
    start_char: int,
    end_char: int,
    heading_path: tuple[str, ...],
) -> ChunkDraft:
    content = parsed.text[start_char:end_char]
    source_kind, source_locator, _source_start, _source_end = source_location_for_range(
        parsed,
        start_char,
        end_char,
    )
    return ChunkDraft(
        ordinal=ordinal,
        start_char=start_char,
        end_char=end_char,
        content=content,
        content_sha256=sha256(content.encode("utf-8")).hexdigest(),
        source_kind=source_kind,
        source_locator=source_locator,
        heading_path=heading_path,
    )
