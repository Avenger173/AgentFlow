"""PPT 创作可选的 Wikimedia 公开资料参考 Provider。

这不是通用网页浏览器，更不是统计数据源。它只查询固定的中文维基百科 Action API，并把最多三条
公开页面的标题、链接、简短检索摘要和抓取时间交给来源页与任务审计。任何数值、观点或案例都
不能因为出现在这里就自动变成 PPT 正文事实或图表数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Sequence
from urllib.parse import quote, urlparse

import httpx


_WIKIMEDIA_API_URL = "https://zh.wikipedia.org/w/api.php"
_WIKIPEDIA_PAGE_ROOT = "https://zh.wikipedia.org/wiki/"
_MAX_SOURCES_PER_EXPORT = 3
_MAX_QUERY_LENGTH = 140
_MAX_SNIPPET_LENGTH = 220


@dataclass(frozen=True)
class WikimediaResearchSource:
    """一条公开资料参考，字段保持在来源页与审计真正需要的最小范围。"""

    source_id: str
    query: str
    title: str
    page_url: str
    snippet: str
    retrieved_at: str

    def audit_metadata(self) -> dict[str, str]:
        """返回可复盘但不包含 HTTP 原始正文的审计字段。"""

        return {
            "provider": "wikimedia",
            "source_id": self.source_id,
            "query": self.query,
            "title": self.title,
            "page_url": self.page_url,
            "snippet": self.snippet,
            "retrieved_at": self.retrieved_at,
            "scope": "public_reference_only",
        }


@dataclass(frozen=True)
class WikimediaResearchResolution:
    """一次确认导出中的资料检索结果；失败必须保守降级为 warning。"""

    sources: tuple[WikimediaResearchSource, ...]
    warnings: tuple[str, ...]

    @property
    def provider(self) -> str:
        return "wikimedia"


def fetch_wikimedia_references(
    queries: Sequence[str],
    *,
    limit: int = _MAX_SOURCES_PER_EXPORT,
    client: httpx.Client | None = None,
) -> WikimediaResearchResolution:
    """检索固定 Wikimedia 接口并返回最多三条页面参考。

    ``client`` 仅用于离线 MockTransport 验证。生产调用不接受任意 URL，也不跟随重定向，防止
    公开资料功能被扩张成通用联网下载通道。
    """

    clean_queries = _normalize_queries(queries, limit=limit)
    if not clean_queries:
        return WikimediaResearchResolution(
            sources=(),
            warnings=("当前主题没有可用于补充公开资料的检索词，未请求外部资料。",),
        )

    sources: list[WikimediaResearchSource] = []
    warnings: list[str] = []
    seen_titles: set[str] = set()
    owns_client = client is None
    active_client = client or httpx.Client(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        # Wikimedia 要求调用方提供可识别且可追溯的 User-Agent。没有项目地址的泛化
        # 标识会被边缘节点拒绝，导致公开资料能力出现“连接正常但没有结果”的假失败。
        headers={
            "User-Agent": "AgentFlow/0.1 (https://github.com/Avenger173/AgentFlow; public-reference-mcp)",
            "Accept": "application/json",
        },
    )
    try:
        for query in clean_queries:
            try:
                source = _search_one(active_client, query=query, seen_titles=seen_titles)
            except (httpx.HTTPError, ValueError) as exc:
                warnings.append(f"公开资料“{query}”未能读取：{_safe_error_message(exc)}")
                continue
            if source is not None:
                sources.append(source)
                seen_titles.add(source.title.casefold())
    finally:
        if owns_client:
            active_client.close()

    if not sources and not warnings:
        warnings.append("Wikimedia 没有返回与当前主题匹配的公开资料，本次仅保留用户主题作为创作依据。")
    return WikimediaResearchResolution(sources=tuple(sources), warnings=tuple(warnings[:3]))


def _search_one(
    client: httpx.Client,
    *,
    query: str,
    seen_titles: set[str],
) -> WikimediaResearchSource | None:
    response = client.get(
        _WIKIMEDIA_API_URL,
        params={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "search",
            "srsearch": query,
            "srnamespace": "0",
            "srlimit": "4",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("公开资料响应格式无效")
    search = payload.get("query", {})
    results = search.get("search", []) if isinstance(search, dict) else []
    if not isinstance(results, list):
        raise ValueError("公开资料响应缺少搜索结果")

    for item in results:
        if not isinstance(item, dict):
            continue
        title = _normalize_text(str(item.get("title") or ""), limit=160)
        if not title or title.casefold() in seen_titles:
            continue
        snippet = _normalize_text(str(item.get("snippet") or ""), limit=_MAX_SNIPPET_LENGTH)
        source_id = str(item.get("pageid") or title)
        return WikimediaResearchSource(
            source_id=source_id,
            query=query,
            title=title,
            page_url=f"{_WIKIPEDIA_PAGE_ROOT}{quote(title.replace(' ', '_'))}",
            snippet=snippet,
            retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    return None


def _normalize_queries(queries: Sequence[str], *, limit: int) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for value in queries:
        query = _normalize_text(str(value), limit=_MAX_QUERY_LENGTH)
        key = query.casefold()
        if len(query) < 2 or key in seen:
            continue
        seen.add(key)
        values.append(query)
        if len(values) >= max(1, min(limit, _MAX_SOURCES_PER_EXPORT)):
            break
    return values


def _normalize_text(value: str, *, limit: int) -> str:
    text = re.sub(r"<[^>]+>", "", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else f"{text[: max(1, limit - 1)].rstrip()}…"


def _safe_error_message(exc: Exception) -> str:
    """阻止 API 原始正文、参数或内部网络细节进入客户页面和任务历史。"""

    if isinstance(exc, httpx.HTTPStatusError):
        return f"Wikimedia 返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.HTTPError):
        return "服务暂不可用"
    return "返回内容无效"


def is_safe_wikimedia_url(value: str) -> bool:
    """供测试和未来渲染器复用的固定域名检查，拒绝任意外链拼接。"""

    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname == "zh.wikipedia.org"
