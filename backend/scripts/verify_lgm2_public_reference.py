"""LGM2 受控公开资料 MCP 的离线回归。

不读取客户数据、不访问网络、不启动外部网页抓取器。脚本用临时数据目录、固定 MCP 夹具验证
连接启停、Commander 准入、来源契约和 Runtime 投影，防止未来重构把该能力退化成任意 URL Tool。
"""

from __future__ import annotations

import asyncio
import argparse
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEMP_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_lgm2_public_reference_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(TEMP_DATA_DIR)
os.environ["AGENTFLOW_MCP_ENABLED"] = "true"
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.mcp.contracts import (  # noqa: E402
    McpGatewayAuditEvent,
    McpServerReference,
    McpToolDescriptor,
    McpToolReference,
    McpToolResult,
)
from app.services.commander import create_commander_plan  # noqa: E402
from app.services.delivery_card import build_delivery_card  # noqa: E402
from app.services.public_reference_mcp import (  # noqa: E402
    PublicReferenceResolution,
    PublicReferenceSource,
    search_public_references,
)
from app.schemas.workflow import (  # noqa: E402
    RuntimeExecutionMetrics,
    RuntimeExecutionLimits,
    WorkflowRun,
)
from app.workflow import runtime  # noqa: E402
from main import app  # noqa: E402


def _message() -> str:
    return "请联网检索公开资料 AI Agent 的制作方法"


class _FixtureGateway:
    def __init__(self) -> None:
        server = McpServerReference(
            server_id="public-reference",
            display_name="Wikimedia 公开资料参考",
            transport="stdio",
        )
        self.reference = McpToolReference(server=server, tool_name="search_wikimedia")
        self.called_arguments: dict[str, object] | None = None

    async def discover_tools(self) -> tuple[McpToolDescriptor, ...]:
        return (
            McpToolDescriptor(
                reference=self.reference,
                title="检索 Wikimedia 公开资料",
                description="fixture",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> McpToolResult:
        assert tool_name == "search_wikimedia"
        self.called_arguments = arguments
        return McpToolResult(
            reference=self.reference,
            text="fixture",
            structured_content={
                "query": "AI Agent 制作方法",
                "sources": [
                    {
                        "source_id": "wikimedia:fixture",
                        "query": "AI Agent 制作方法",
                        "title": "人工智能",
                        "page_url": "https://zh.wikipedia.org/wiki/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD",
                        "snippet": "fixture source",
                        "retrieved_at": "2026-09-04T00:00:00Z",
                        "provider": "wikimedia",
                        "scope": "public_reference_only",
                    }
                ],
                "warnings": [],
            },
        )

    def audit_snapshot(self) -> tuple[McpGatewayAuditEvent, ...]:
        return (
            McpGatewayAuditEvent(
                event_type="tool_call_completed",
                server_id="public-reference",
                tool_name="search_wikimedia",
                status="completed",
                duration_ms=12,
                request_bytes=52,
                result_bytes=420,
            ),
        )

    async def close(self) -> None:
        return None


def _fixture_resolution() -> PublicReferenceResolution:
    return PublicReferenceResolution(
        query="AI Agent 制作方法",
        sources=(
            PublicReferenceSource(
                source_id="wikimedia:fixture",
                title="人工智能",
                page_url="https://zh.wikipedia.org/wiki/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD",
                snippet="fixture source",
                retrieved_at="2026-09-04T00:00:00Z",
            ),
        ),
        warnings=(),
        tool_name="mcp.public-reference.search_wikimedia",
        duration_ms=12,
        request_bytes=52,
    )


def main(*, live: bool = False) -> None:
    try:
        client = TestClient(app)
        disabled = client.get("/api/mcp/connections")
        disabled.raise_for_status()
        assert disabled.json()["connections"][0]["enabled"] is False

        disabled_plan = create_commander_plan(_message())
        assert not any(step.action == "search_public_references" for step in disabled_plan.steps)
        assert len(disabled_plan.clarifying_questions) == 1

        fresh_news_plan = create_commander_plan("帮我查一下最近有关汽车的新闻资料")
        assert fresh_news_plan.intent == "fresh_external_information"
        assert fresh_news_plan.agent_hints == []
        assert not any(
            step.agent in {"document_agent", "data_agent", "knowledge_agent"}
            for step in fresh_news_plan.steps
        )
        assert "暂不支持最近新闻" in fresh_news_plan.clarifying_questions[0]
        assert "@文档助手" not in fresh_news_plan.clarifying_questions[0]

        plain_document_plan = create_commander_plan("请整理一份文档")
        assert "@文档助手" not in plain_document_plan.clarifying_questions[0]
        hinted_document_plan = create_commander_plan("@文档助手 请整理一份文档")
        assert "已点名 @文档助手" in hinted_document_plan.clarifying_questions[0]

        enabled = client.post("/api/mcp/connections/public-reference/enable")
        enabled.raise_for_status()
        assert enabled.json()["connection"]["enabled"] is True

        connection_test = client.post("/api/mcp/connections/public-reference/test")
        connection_test.raise_for_status()
        assert connection_test.json()["connection"]["last_tool_count"] == 1

        enabled_plan = create_commander_plan(_message())
        public_step = next(step for step in enabled_plan.steps if step.action == "search_public_references")
        assert public_step.required_permissions == ["network", "shell"]
        assert public_step.requires_confirmation is True
        assert public_step.tool_name == "mcp.public-reference.search_wikimedia"

        fixture_gateway = _FixtureGateway()
        resolution = asyncio.run(
            search_public_references(
                "AI Agent 制作方法",
                gateway_factory=lambda: fixture_gateway,  # type: ignore[arg-type]
            )
        )
        assert fixture_gateway.called_arguments == {"query": "AI Agent 制作方法", "limit": 3}
        assert resolution.completed and resolution.duration_ms == 12
        assert resolution.runtime_result()["scope"] == "public_reference_only"

        with patch.object(runtime, "search_public_references_sync", return_value=_fixture_resolution()):
            step_run, tool_call, artifacts = runtime._execute_safe_step(
                runtime_task_id="task_lgm2_fixture",
                step=public_step,
                plan=enabled_plan,
                output_dir=TEMP_DATA_DIR / "outputs",
                runtime_context={},
            )
        assert step_run.status == "completed"
        assert artifacts == []
        assert tool_call.request == {
            "mcp_server_id": "public-reference",
            "mcp_tool_name": "search_wikimedia",
            "request_bytes": 52,
        }
        assert tool_call.result["source_count"] == 1

        run = WorkflowRun(
            task_id="task_lgm2_fixture",
            mode="runtime",
            status="completed",
            summary="fixture",
            max_risk_level="high",
            requires_confirmation=True,
            steps=[step_run],
            limits=RuntimeExecutionLimits(),
            metrics=RuntimeExecutionMetrics(
                started_at=datetime.now(UTC).isoformat(),
                finished_at=datetime.now(UTC).isoformat(),
                step_total=1,
                step_completed=1,
            ),
        )
        card = build_delivery_card(run=run, artifacts=[], tool_calls=[tool_call], permissions=[])
        assert card.headline == "公开资料参考已就绪"
        assert "https://zh.wikipedia.org/wiki/" in card.summary_markdown
        assert any(fact.label == "公开来源" and fact.value == "1 条" for fact in card.facts)
        if live:
            live_resolution = asyncio.run(search_public_references("人工智能"))
            assert live_resolution.completed
            assert all(item.page_url.startswith("https://zh.wikipedia.org/wiki/") for item in live_resolution.sources)
            print(f"LGM2 live Wikimedia verification passed ({len(live_resolution.sources)} sources)")
        print("LGM2 public reference verification passed")
    finally:
        shutil.rmtree(TEMP_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="验证 LGM2 受控公开资料 MCP。")
    parser.add_argument("--live", action="store_true", help="额外读取固定 Wikimedia 公开资料，默认不联网。")
    main(live=parser.parse_args().live)
