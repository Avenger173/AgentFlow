"""验证 LGM5.7 资源探针只在临时目录运行并返回真实测量形状。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_ROOT = Path(tempfile.mkdtemp(prefix="agentflow_lgm57_resource_verify_"))


def main() -> None:
    environment = os.environ.copy()
    environment["AGENTFLOW_CHAT_MODE"] = "mock"
    environment["AGENTFLOW_DATA_DIR"] = str(VERIFY_ROOT / "data")
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(BACKEND_ROOT / "scripts" / "measure_lgm57_composition_resources.py"),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    for kind in ("native", "graph"):
        measurement = payload[kind]
        assert measurement["kind"] == kind
        assert int(measurement["startup_ms"]) >= 1
        assert int(measurement["resident_memory_mib"]) >= 1
    assert isinstance(payload["within_10_percent"], bool)
    assert not (VERIFY_ROOT / "data" / "agentflow.db").exists()
    print("LGM5.7 composition resource probe verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
