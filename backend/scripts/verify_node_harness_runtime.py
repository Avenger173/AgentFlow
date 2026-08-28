"""验证项目内 Node Harness 的 H0 安装与 FastAPI 状态接口。

本脚本只执行 `node --version`、`dsh --version` 和本地 TestClient 请求；不读取
API Key、不发送模型 prompt、不启动 `dsh web`，因此可以作为每次升级 npm lockfile
后的低成本回归。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def main() -> None:
    # 显式关闭功能开关，验证“已安装”与“已获准参与任务”是两件不同的事。
    os.environ["AGENTFLOW_NODE_HARNESS_ENABLED"] = "false"

    from fastapi.testclient import TestClient

    from app.harness.node_runtime import clear_node_harness_probe_cache
    from main import app

    clear_node_harness_probe_cache()
    client = TestClient(app)
    response = client.get("/api/harness/runtime", params={"refresh": "true"})
    response.raise_for_status()
    payload = response.json()

    assert payload["backend"] == "node_harness"
    assert payload["enabled"] is False
    assert payload["installed"] is True, payload
    assert payload["ready"] is True, payload
    assert payload["node_version"].startswith("v"), payload
    assert payload["harness_version"].startswith("0.1.0-rc.6"), payload
    assert "Agent" not in payload["message"] or "未启用" in payload["message"], payload

    print(
        "Node Harness runtime verification passed: "
        f"node={payload['node_version']} harness={payload['harness_version']} enabled={payload['enabled']}"
    )


if __name__ == "__main__":
    main()
