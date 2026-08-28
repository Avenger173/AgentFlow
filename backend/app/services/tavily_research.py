"""ResearchGateway 使用的 Tavily 搜索适配器。

Tavily 负责在服务端检索并返回来源的清洗正文片段，可以避开本机逐页抓取动态站点时常见的
超时和页面体积问题。本模块不解释事实、不生成数值，也不直接参与 PPT 渲染；其返回仍必须由
``presentation_research_gateway`` 做来源、对象、时间、单位和证据原文校验后才可交付。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Sequence

import httpx

from app.core.config import settings
from app.services.presentation_research_network import research_httpx_options


_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_QUERIES = 6
_MAX_RESULTS_PER_QUERY = 2
_MAX_RESPONSE_BYTES = 600_000
_MAX_RAW_CONTENT_CHARS = 7_000
_MAX_RELEVANT_CHARS = 3_000
_MAX_PAGE_TEXT_CHARS = 4_000
_REQUEST_TIMEOUT_SECONDS = 20.0
_MAX_CONCURRENT_QUERIES = 3


@dataclass(frozen=True)
class TavilyResearchCandidate:
    """一次受限查询返回的候选来源。

    ``raw_content`` 仅保存 Tavily 返回的已清洗页面正文；普通搜索摘要不具备数据核验资格，
    因此不会写入该字段。上层会在正文缺失时决定是否允许受控直连回读来源页面。
    """

    title: str
    url: str
    raw_content: str = ""
    source_query: str = ""


@dataclass(frozen=True)
class TavilyResearchResolution:
    """适配器层的脱敏结果，不包含 API Key、完整响应或服务端答案。"""

    candidates: tuple[TavilyResearchCandidate, ...]
    query_count: int
    retrieved_at: str
    warnings: tuple[str, ...]


def fetch_tavily_research_sources(
    queries: Sequence[str],
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> TavilyResearchResolution:
    """执行至多六条研究查询，并保留每个来源可审计的正文片段。

    请求使用 Tavily 官方 Bearer 鉴权和 ``include_raw_content=text``。查询串来自已经确认的
    研究蓝图，调用方不能传入任意 URL；这里也不会采用供应商生成的 ``answer`` 字段，以免把
    搜索服务的自然语言总结误当成数据事实。
    """

    normalized_queries = _normalize_queries(queries)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    effective_key = (api_key if api_key is not None else settings.tavily_api_key).strip()
    if not effective_key:
        return TavilyResearchResolution(
            candidates=(),
            query_count=0,
            retrieved_at=now,
            warnings=("未配置 Tavily Research Key，无法使用稳定搜索来源。",),
        )
    if not normalized_queries:
        return TavilyResearchResolution(candidates=(), query_count=0, retrieved_at=now, warnings=())

    owns_client = client is None
    active_client = client or httpx.Client(
        timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=8.0),
        follow_redirects=False,
        **research_httpx_options(),
        headers={
            "Authorization": f"Bearer {effective_key}",
            "Content-Type": "application/json",
            "User-Agent": "AgentFlow-ResearchGateway/0.1",
        },
    )
    candidates: list[TavilyResearchCandidate] = []
    warnings: list[str] = []
    seen_urls: set[str] = set()
    request_headers = {
        "Authorization": f"Bearer {effective_key}",
        "Content-Type": "application/json",
        "User-Agent": "AgentFlow-ResearchGateway/0.1",
    }
    try:
        # 官方建议把复杂研究拆成若干聚焦查询后并发聚合。这里仍保留总查询预算和单查询超时，
        # 但不再让 A、B 两个对象的资料串行等待，降低 PPT 导出的无意义耗时。
        with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT_QUERIES, len(normalized_queries))) as executor:
            futures = [
                executor.submit(_post_search, active_client, query=query, headers=request_headers)
                for query in normalized_queries
            ]
            responses: list[tuple[str, dict[str, Any] | Exception]] = []
            for query, future in zip(normalized_queries, futures):
                try:
                    responses.append((query, future.result()))
                except (httpx.HTTPError, ValueError) as exc:
                    responses.append((query, exc))

        # 按查询原顺序汇总，让相同输入的候选优先级保持稳定，便于复盘和离线测试。
        for query, response_payload in responses:
            if isinstance(response_payload, Exception):
                warnings.append(f"Tavily 查询未完成：{_safe_error(response_payload)}")
                continue
            for item in _result_candidates(response_payload, source_query=query):
                url_key = item.url.casefold()
                if url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                candidates.append(item)
    finally:
        if owns_client:
            active_client.close()

    if not candidates and not warnings:
        warnings.append("Tavily 没有返回可用于研究的公开来源。")
    return TavilyResearchResolution(
        candidates=tuple(candidates),
        query_count=len(normalized_queries),
        retrieved_at=now,
        warnings=tuple(warnings[:3]),
    )


def _post_search(
    client: httpx.Client,
    *,
    query: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    """读取有限大小的 JSON 响应，防止第三方异常正文侵占任务内存。"""

    payload = {
        "query": query,
        "search_depth": "advanced",
        "chunks_per_source": 3,
        "max_results": _MAX_RESULTS_PER_QUERY,
        "include_answer": False,
        "include_raw_content": "text",
        "include_images": False,
        "auto_parameters": False,
    }
    with client.stream("POST", _TAVILY_SEARCH_URL, json=payload, headers=headers) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise ValueError("Tavily 响应超过读取上限")
            chunks.append(chunk)
    try:
        parsed = json.loads(b"".join(chunks))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Tavily 返回内容不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Tavily 返回格式无效")
    return parsed


def _result_candidates(
    payload: dict[str, Any],
    *,
    source_query: str = "",
) -> tuple[TavilyResearchCandidate, ...]:
    """保留与查询相关的来源原文片段，并补充有限正文上下文。

    Advanced Search 的 ``content`` 是从来源页面按查询重排的原文 chunks，不是 ``answer``
    生成结论。把它放在整页清洗正文前面，能避免关键数字位于长页面后部而被 5000 字上限截掉。
    """

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return ()
    candidates: list[TavilyResearchCandidate] = []
    for item in raw_results[:_MAX_RESULTS_PER_QUERY]:
        if not isinstance(item, dict):
            continue
        title = _normalize_text(str(item.get("title") or ""))[:180]
        url = _normalize_text(str(item.get("url") or ""))[:1_800]
        raw_content = item.get("raw_content")
        relevant_chunks = item.get("content")
        relevant_text = _normalize_text(relevant_chunks) if isinstance(relevant_chunks, str) else ""
        page_text = _normalize_text(raw_content) if isinstance(raw_content, str) else ""
        # 搜索相关片段有时从表格中段开始，若让它独占上限，页面开头的列名会被截掉，模型只能
        # 看到一串无法解释的数字。两段各有固定预算，既保留命中行，也保留表头和页面语境。
        if relevant_text and page_text:
            content = _normalize_text(
                f"{relevant_text[:_MAX_RELEVANT_CHARS]} [...] PAGE TEXT: "
                f"{page_text[:_MAX_PAGE_TEXT_CHARS]}"
            )[:_MAX_RAW_CONTENT_CHARS]
        else:
            content = (relevant_text or page_text)[:_MAX_RAW_CONTENT_CHARS]
        if title and url:
            candidates.append(
                TavilyResearchCandidate(
                    title=title,
                    url=url,
                    raw_content=content,
                    source_query=_normalize_text(source_query)[:400],
                )
            )
    return tuple(candidates)


def _normalize_queries(queries: Sequence[str]) -> tuple[str, ...]:
    """按既有研究预算去重，不能让外部 Provider 的调用数随意扩大。"""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in queries:
        query = _normalize_text(str(value))[:400]
        key = query.casefold()
        if not query or key in seen:
            continue
        seen.add(key)
        normalized.append(query)
        if len(normalized) >= _MAX_QUERIES:
            break
    return tuple(normalized)


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"服务返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "网络服务暂不可用"
    return _normalize_text(str(exc))[:120] or "未知原因"
