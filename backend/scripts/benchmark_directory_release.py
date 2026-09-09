"""生成 Windows 目录发行的可复跑启动、内存与关闭基线。

每轮启动独立的候选 Qt 客户端，使用临时用户数据目录与 mock 模式；不读取 .env、客户资料、
模型配置、输出目录或网络。它只聚合客户端到后端健康、运行时树 RSS、关闭和端口释放耗时。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = BACKEND_ROOT / "scripts" / "verify_directory_client_smoke.py"
METRIC_KEYS = (
    "client_ready_ms",
    "runtime_rss_bytes",
    "client_close_ms",
    "runtime_process_count",
    "readonly_request_count",
    "readonly_max_ms",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 AgentFlow 目录发行性能基线。")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3, choices=range(3, 6))
    return parser.parse_args()


def _measure_once(*, release_root: Path, metrics_path: Path) -> dict[str, int]:
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(SMOKE_SCRIPT),
            "--release-root",
            str(release_root),
            "--metrics-output",
            str(metrics_path),
            "--read-only-probe-concurrency",
            "12",
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=75,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "目录发行单轮基准失败：\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), int) for key in METRIC_KEYS):
        raise RuntimeError("目录发行单轮基准没有返回完整的数值指标。")
    return {key: int(payload[key]) for key in METRIC_KEYS}


def main() -> None:
    arguments = _parse_args()
    release_root = arguments.release_root.resolve()
    if not (release_root / "AgentFlow.exe").is_file():
        raise SystemExit("目录发行基准已停止：未找到根级 AgentFlow.exe。")

    scratch = Path(tempfile.mkdtemp(prefix="agentflow_directory_release_benchmark_"))
    try:
        samples = [
            _measure_once(release_root=release_root, metrics_path=scratch / f"sample_{index}.json")
            for index in range(arguments.runs)
        ]
        summary = {key: round(median([item[key] for item in samples])) for key in METRIC_KEYS}
        print(
            "AgentFlow directory release benchmark: "
            f"runs={arguments.runs} "
            f"ready_ms={summary['client_ready_ms']} "
            f"runtime_rss_mib={summary['runtime_rss_bytes'] / (1024 * 1024):.1f} "
            f"close_ms={summary['client_close_ms']} "
            f"process_count={summary['runtime_process_count']} "
            f"readonly_requests={summary['readonly_request_count']} "
            f"readonly_max_ms={summary['readonly_max_ms']}"
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise SystemExit(f"目录发行基准已停止：{error}") from error
