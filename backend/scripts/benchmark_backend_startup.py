"""重复运行启动测量，生成可比较的本地启动基线。

每轮都在独立 Python 进程中运行 ``measure_backend_startup.py``，避免把同一进程内的
模块缓存误当成冷启动优化。该基准不读取客户数据、不调用模型，也不连接 MCP Server。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from statistics import median


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MEASURE_SCRIPT = BACKEND_ROOT / "scripts" / "measure_backend_startup.py"
_TOTAL_READY_PATTERN = re.compile(r"total_ready_ms=(\d+)")


def _measure_once() -> int:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(MEASURE_SCRIPT)],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "启动测量失败：\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    match = _TOTAL_READY_PATTERN.search(result.stdout)
    if match is None:
        raise RuntimeError(f"启动测量未返回 total_ready_ms：{result.stdout}")
    return int(match.group(1))


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 AgentFlow 后端启动基线")
    parser.add_argument("--runs", type=int, default=5, choices=range(3, 11))
    arguments = parser.parse_args()

    measurements = [_measure_once() for _ in range(arguments.runs)]
    print(
        "Backend startup benchmark: "
        f"runs={arguments.runs} values_ms={measurements} "
        f"median_ms={round(median(measurements))} min_ms={min(measurements)} "
        f"max_ms={max(measurements)}"
    )


if __name__ == "__main__":
    main()
