"""LGM2 的受控公开资料 MCP Server。

它只封装既有 Wikimedia Action API Provider，固定中文维基百科域名、固定 GET 查询和有限
来源字段。服务不接受 URL、Header、代理、文件路径或模型指令，不能作为通用网页浏览器。
"""

from __future__ import annotations

from mcp.server import MCPServer

from app.services.wikimedia_research import fetch_wikimedia_references


_MAX_QUERY_LENGTH = 140
_MAX_SOURCE_COUNT = 3

server = MCPServer(
    name="AgentFlow Public Reference MCP",
    version="1.0.0",
    instructions=(
        "只查询固定 Wikimedia 公开页面，返回参考线索和抓取时间。"
        "结果不是自动核验的专业事实或统计结论。"
    ),
)


@server.tool(
    name="search_wikimedia",
    title="检索 Wikimedia 公开资料",
    description="在固定的中文维基百科公开资料中检索主题，返回最多三条可追溯参考线索。",
)
def search_wikimedia(query: str, limit: int = _MAX_SOURCE_COUNT) -> dict[str, object]:
    normalized_query = " ".join(str(query or "").split())[:_MAX_QUERY_LENGTH]
    if len(normalized_query) < 2:
        return {
            "query": normalized_query,
            "sources": [],
            "warnings": ["检索词至少需要两个字符，本次没有请求公开资料。"],
        }

    resolution = fetch_wikimedia_references(
        [normalized_query],
        limit=max(1, min(int(limit or _MAX_SOURCE_COUNT), _MAX_SOURCE_COUNT)),
    )
    return {
        "query": normalized_query,
        "sources": [source.audit_metadata() for source in resolution.sources],
        "warnings": list(resolution.warnings),
    }


if __name__ == "__main__":
    server.run(transport="stdio")
