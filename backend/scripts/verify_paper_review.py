"""论文审查 V1 的离线端到端验收，不使用真实模型或用户数据目录。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    temp_data_dir = Path(tempfile.mkdtemp(prefix="agentflow_paper_review_verify_"))
    os.environ["AGENTFLOW_DATA_DIR"] = str(temp_data_dir)
    os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
    sys.path.insert(0, str(backend_root))
    try:
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        imported = client.post(
            "/api/workspace/documents",
            json={
                "filename": "引用不完整的论文.md",
                "content": (
                    "# 面向客户服务的检索方法研究\n\n"
                    "## 摘要\n本研究提出一套可解释的检索流程。\n\n"
                    "## 引言\n已有研究说明结构化检索能够减少人工定位成本[1]。\n\n"
                    "## 方法\n本文按资料导入、规则扫描和人工复核三个阶段开展。\n\n"
                    "## 结果\n如图1所示，规则检查能够定位常见缺失项。\n\n"
                    "## 结论\n该方法适合用于首轮规范检查。\n"
                ),
            },
        )
        imported.raise_for_status()
        document_ref = imported.json()["relative_path"]

        response = client.post(
            "/api/agents/document_agent/paper-review/run",
            json={"document_ref": document_ref, "paper_type": "article"},
        )
        response.raise_for_status()
        payload = response.json()
        report = payload["report"]
        findings = report["findings"]
        rule_ids = {item["rule_id"] for item in findings}
        assert payload["status"] == "completed"
        assert report["review_strategy"] == "deterministic_paper_rules_v1"
        assert report["document_ref"] == document_ref
        assert "paper_review.references" in rule_ids
        assert "paper_review.citation_mapping" in rule_ids
        assert all(item["source_refs"] for item in findings)
        check_status = {item["rule_id"]: item["status"] for item in report["checks"]}
        assert check_status["paper_review.structure"] == "passed"
        assert check_status["paper_review.figure_table"] == "passed"
        assert check_status["paper_review.heading_format"] == "passed"

        task_id = payload["task_id"]
        tools = client.get(f"/api/tasks/{task_id}/tool-calls")
        tools.raise_for_status()
        tool_calls = tools.json()["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool_name"] == "document.read_text"
        assert tool_calls[0]["request"]["coverage"] == "full_document"

        # Qt 客户端使用异步受理与阶段事件，不能只验证同步 /run 路径。即使规则任务很快完成，
        # 事件缓冲也必须能回放 queued、规则检查和终态，随后结果接口才返回完整报告。
        started = client.post(
            "/api/agents/document_agent/paper-review/start",
            json={"document_ref": document_ref, "paper_type": "article"},
        )
        assert started.status_code == 202, started.text
        async_task_id = started.json()["task_id"]
        async_events: list[dict[str, object]] = []
        with client.websocket_connect(f"/ws/tasks/{async_task_id}") as websocket:
            while True:
                try:
                    async_events.append(websocket.receive_json())
                except Exception:
                    break
        assert async_events
        assert async_events[0]["event"] == "task_queued"
        assert any(event["event"] == "paper_review_rules_started" for event in async_events)
        # TestClient 的请求上下文可能在后台任务完成事件写入后立即关闭 WebSocket；客户端的
        # 正式收束依据始终是“事件流关闭后读取结果接口”，因此这里验证可见阶段和终态报告，
        # 不把传输层最后一帧的到达顺序误当成业务契约。

        async_payload: dict[str, object] | None = None
        for _ in range(30):
            result_response = client.get(
                f"/api/agents/document_agent/paper-review/{async_task_id}/result"
            )
            result_response.raise_for_status()
            candidate = result_response.json()
            if candidate["status"] != "running":
                async_payload = candidate
                break
            time.sleep(0.05)
        assert async_payload is not None
        assert async_payload["status"] == "completed"
        assert async_payload["result"]["report"]["document_ref"] == document_ref

        bad = client.post(
            "/api/agents/document_agent/paper-review/run",
            json={"document_ref": "不在工作区的论文.md"},
        )
        assert bad.status_code == 400
    finally:
        shutil.rmtree(temp_data_dir, ignore_errors=True)

    print("Paper review verification passed.")


if __name__ == "__main__":
    main()
