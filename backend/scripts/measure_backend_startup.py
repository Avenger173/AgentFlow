"""测量本地 FastAPI 冷启动的导入与 lifespan 就绪耗时，不读取客户运行数据。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
from time import perf_counter


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_startup_measure_"))
os.environ["AGENTFLOW_CHAT_MODE"] = "mock"
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))


def main() -> None:
    import_started = perf_counter()
    from main import app

    imported_at = perf_counter()
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        ready_at = perf_counter()
        response = client.get("/health")
        assert response.status_code == 200, response.text
    health_at = perf_counter()

    print(
        "Backend startup measurement: "
        f"import_ms={round((imported_at - import_started) * 1000)} "
        f"lifespan_ms={round((ready_at - imported_at) * 1000)} "
        f"health_ms={round((health_at - ready_at) * 1000)} "
        f"total_ready_ms={round((ready_at - import_started) * 1000)}"
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)

