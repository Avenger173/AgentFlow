"""回读目录发行的 AgentFlowBackend：只验证本机 /health 与可写用户数据根。"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证目录发行后端可离线启动。")
    parser.add_argument("--release-root", type=Path, required=True, help="包含 backend/AgentFlowBackend.exe 的候选目录。")
    return parser.parse_args()


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> None:
    args = _parse_args()
    release_root = args.release_root.resolve()
    executable = release_root / "backend" / "AgentFlowBackend.exe"
    if not executable.is_file():
        raise SystemExit("目录发行验证已停止：未找到 backend/AgentFlowBackend.exe。")

    scratch = Path(tempfile.mkdtemp(prefix="agentflow_directory_backend_verify_"))
    port = _available_loopback_port()
    environment = os.environ.copy()
    environment.update(
        {
            "AGENTFLOW_ENVIRONMENT": "production",
            "AGENTFLOW_PROJECT_ROOT": str(release_root),
            "AGENTFLOW_DATA_DIR": str(scratch / "data"),
            "AGENTFLOW_OUTPUT_DIR": str(scratch / "output"),
            "AGENTFLOW_USER_AGENTS_DIR": str(scratch / "agents"),
            "AGENTFLOW_HOST": "127.0.0.1",
            "AGENTFLOW_PORT": str(port),
            "AGENTFLOW_CHAT_MODE": "mock",
            "AGENTFLOW_NODE_HARNESS_ENABLED": "false",
        }
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        # 打包后端是无控制台 GUI 子进程；验证不读取 stdout/stderr，避免日志带入测试报告。
        process = subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        health_url = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("目录后端在健康检查前提前退出。")
            try:
                response = httpx.get(health_url, timeout=1.0)
                if response.status_code == 200 and response.json().get("status") == "ok":
                    print("AgentFlow directory backend verification passed: health=ok user-data=isolated")
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("目录后端未能在 45 秒内通过 /health。")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f"目录发行验证已停止：{error}") from error
