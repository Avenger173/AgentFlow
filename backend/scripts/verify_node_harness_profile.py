"""验证项目专属 Harness profile 的 H1 无密钥预检。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def main() -> None:
    data_dir = Path(tempfile.mkdtemp(prefix="agentflow_harness_profile_"))
    os.environ["AGENTFLOW_DATA_DIR"] = str(data_dir)
    os.environ["AGENTFLOW_NODE_HARNESS_ENABLED"] = "false"

    try:
        from fastapi.testclient import TestClient

        from app.harness.node_runtime import clear_node_harness_probe_cache
        from main import app

        clear_node_harness_probe_cache()
        response = TestClient(app).post("/api/harness/profile/preflight")
        response.raise_for_status()
        payload = response.json()

        assert payload["profile_name"] == "agentflow-readonly", payload
        assert payload["ready"] is True, payload
        assert payload["permission_mode"] == "read-only", payload
        assert payload["launch_isolated"] is True, payload
        assert "tool-pwsh" in payload["disabled_entries"], payload
        assert "tool-web" in payload["disabled_entries"], payload
        assert "tool-fs" in payload["disabled_entries"], payload
        assert not (data_dir / "deepseek_harness" / ".credentials.yaml").exists()
        print(
            "Node Harness profile verification passed: "
            f"profile={payload['profile_name']} disabled={len(payload['disabled_entries'])}"
        )
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
