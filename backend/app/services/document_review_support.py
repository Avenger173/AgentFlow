"""多类文档审查共享的受控读取与来源定位小工具。"""

from __future__ import annotations

from typing import Iterable

from app.schemas.document_agent import DocumentSourceRef
from app.services.workspace_documents import WorkspaceDocumentError, read_workspace_document_chunks


class ReviewEvidenceLine:
    """规则扫描的最小、可定位文本单位，不存入任务历史的正文载荷。"""

    def __init__(self, text: str, source_ref: DocumentSourceRef) -> None:
        self.text = text
        self.source_ref = source_ref


def load_review_chunks(*, document_ref: str) -> list[dict[str, object]]:
    """读取 workspace 中的一份材料，全文覆盖但复用底层解析缓存。"""

    try:
        chunks = read_workspace_document_chunks(relative_path=document_ref)
    except WorkspaceDocumentError as exc:
        raise ValueError(str(exc)) from exc
    if not chunks:
        raise ValueError("没有读取到可用于审查的材料。")
    return chunks


def collect_evidence_lines(chunks: list[dict[str, object]], *, source_prefix: str) -> list[ReviewEvidenceLine]:
    """把解析分块转成定位行；PDF/DOCX 使用页/段落定位而不是伪造文本行号。"""

    lines: list[ReviewEvidenceLine] = []
    source_index = 0
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        relative_path = str(chunk.get("relative_path") or "")
        source_kind = str(chunk.get("source_kind") or "line")
        source_locator = str(chunk.get("source_locator") or "")
        start_line = int(chunk.get("start_line") or 1)
        for offset, raw_line in enumerate(text.splitlines() or [text]):
            normalized = " ".join(raw_line.split())
            if not normalized:
                continue
            source_index += 1
            line_number = start_line + offset if source_kind == "line" else start_line
            locator = f"第 {line_number} 行" if source_kind == "line" else source_locator
            lines.append(
                ReviewEvidenceLine(
                    normalized,
                    DocumentSourceRef(
                        source_id=f"{source_prefix}_{source_index:04d}",
                        relative_path=relative_path,
                        start_line=max(1, line_number),
                        end_line=max(1, line_number),
                        source_kind=source_kind if source_kind in {"line", "page", "paragraph", "table", "mixed"} else "mixed",  # type: ignore[arg-type]
                        source_locator=locator,
                        excerpt=normalized[:360],
                    ),
                )
            )
    return lines


def document_anchor(*, document_ref: str, chunks: list[dict[str, object]], source_id: str) -> DocumentSourceRef:
    """为缺失类规则提供全文审查范围锚点，避免虚构一条“命中行”。"""

    first = chunks[0]
    source_kind = str(first.get("source_kind") or "line")
    start_line = max(1, int(first.get("start_line") or 1))
    end_line = max(start_line, int(first.get("end_line") or start_line))
    locator = str(first.get("source_locator") or "")
    if not locator and source_kind == "line":
        locator = f"第 {start_line}-{end_line} 行（审查范围起点）"
    return DocumentSourceRef(
        source_id=source_id,
        relative_path=document_ref,
        start_line=start_line,
        end_line=end_line,
        source_kind=source_kind if source_kind in {"line", "page", "paragraph", "table", "mixed"} else "mixed",  # type: ignore[arg-type]
        source_locator=locator or "审查范围起点",
        excerpt="该问题来自整份已解析材料的规则检查，不对应单一命中句。",
    )


def find_keyword_evidence(
    lines: Iterable[ReviewEvidenceLine],
    keywords: Iterable[str],
    *,
    limit: int = 2,
) -> list[ReviewEvidenceLine]:
    """进行大小写无关的明确文本定位，报告只保留前两处最有用的来源。"""

    normalized_keywords = tuple(item.casefold() for item in keywords)
    matched: list[ReviewEvidenceLine] = []
    for line in lines:
        if any(keyword in line.text.casefold() for keyword in normalized_keywords):
            matched.append(line)
            if len(matched) >= limit:
                break
    return matched


def unique_sources(sources: Iterable[DocumentSourceRef], *, limit: int = 2) -> list[DocumentSourceRef]:
    """按真实文件定位去重，避免内部 source_id 因派生任务变化造成重复。"""

    result: list[DocumentSourceRef] = []
    seen: set[tuple[str, int, int, str]] = set()
    for source in sources:
        key = (source.relative_path, source.start_line, source.end_line, source.source_locator)
        if key not in seen:
            seen.add(key)
            result.append(source)
        if len(result) >= limit:
            break
    return result
