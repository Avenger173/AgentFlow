"""验证 H1 执行后端契约，不启动 Node 或真实模型。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.harness.contracts import (  # noqa: E402
    HarnessExecutionRequest,
    HarnessRuntimeEvent,
)
from app.harness.fake_backend import FakeNodeHarnessBackend  # noqa: E402
from app.harness.node_bridge import NodeHarnessBridge  # noqa: E402
from app.services.model_gateway import ModelRuntime  # noqa: E402


async def _verify_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="agentflow_harness_contract_") as temp_dir:
        request = HarnessExecutionRequest(
            task_id="task_harness_contract",
            task_text="只验证外部 Runtime 事件契约。",
            workspace_dir=Path(temp_dir).resolve(),
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
        )
        events: list[HarnessRuntimeEvent] = []

        async def collect(event: HarnessRuntimeEvent) -> None:
            events.append(event)

        result = await FakeNodeHarnessBackend().execute_task(request, collect)

    assert result.status == "completed"
    assert result.session_id == "fake-task_harness_contract"
    assert [event.kind for event in events] == [
        "runtime_started",
        "runtime_heartbeat",
        "assistant_final",
    ]
    assert all("API Key" not in event.message for event in events)

    # 默认关闭时真实 Bridge 必须在解密 Key 或创建子进程前直接拒绝，防止测试或错误路由
    # 意外消耗模型额度。这里的占位 key 只用于证明它不会进入结果文本。
    os.environ["AGENTFLOW_NODE_HARNESS_ENABLED"] = "false"
    disabled_result = await NodeHarnessBridge().execute_task(
        request,
        collect,
        runtime=ModelRuntime(
            provider="deepseek",
            label="DeepSeek",
            transport="openai_compatible",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            api_key="test-key-not-sent",
            thinking="disabled",
            max_tokens=32,
            temperature=0.0,
            timeout_seconds=10.0,
        ),
    )
    assert disabled_result.status == "failed"
    assert disabled_result.failure_code == "runtime_disabled"
    assert "test-key-not-sent" not in str(disabled_result.metadata)
    print("Node Harness adapter contract verification passed: 3 normalized events")


if __name__ == "__main__":
    asyncio.run(_verify_contract())
