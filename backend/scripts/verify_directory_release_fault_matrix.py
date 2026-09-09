"""验证目录发行在可选组件缺失或本地状态损坏时仍可安全降级。

只启动命令行显式给出的候选后端，数据根、输出根和 MCP 状态都位于系统临时目录。该验证
不读取 .env、客户资料、既有 SQLite、模型缓存，也不下载模型、启动 Node 或连接网络。
"""

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
    parser = argparse.ArgumentParser(description="验证 AgentFlow 目录发行离线故障矩阵。")
    parser.add_argument("--release-root", type=Path, required=True)
    return parser.parse_args()


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _release_environment(*, root: Path, scratch: Path, port: int) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "AGENTFLOW_ENVIRONMENT": "production",
            "AGENTFLOW_RELEASE_MODE": "directory",
            "AGENTFLOW_PROJECT_ROOT": str(root),
            "AGENTFLOW_DATA_DIR": str(scratch / "data"),
            "AGENTFLOW_OUTPUT_DIR": str(scratch / "output"),
            "AGENTFLOW_USER_AGENTS_DIR": str(scratch / "agents"),
            "AGENTFLOW_HOST": "127.0.0.1",
            "AGENTFLOW_PORT": str(port),
            "AGENTFLOW_CHAT_MODE": "mock",
            "AGENTFLOW_NODE_HARNESS_ENABLED": "false",
            "AGENTFLOW_MCP_ENABLED": "true",
        }
    )
    return environment


def _wait_for_health(*, port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 45
    endpoint = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("目录后端在离线故障矩阵健康检查前提前退出。")
        try:
            response = httpx.get(endpoint, timeout=1.0)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("目录后端未能在 45 秒内进入离线健康状态。")


def _get_json(endpoint: str) -> dict[str, object]:
    response = httpx.get(endpoint, timeout=5.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise AssertionError("离线故障矩阵接口没有返回 JSON object。")
    return payload


def _verify_optional_runtime_states(*, port: int, scratch: Path) -> None:
    base_url = f"http://127.0.0.1:{port}"
    health = _get_json(f"{base_url}/health")
    assert health.get("status") == "ok", health
    capabilities = health.get("capabilities")
    assert isinstance(capabilities, dict), health
    platform = capabilities.get("lgm_platform")
    assert isinstance(platform, dict), capabilities
    assert platform.get("ready") is False, platform
    assert "Native Runtime" in str(platform.get("message", "")), platform

    harness = _get_json(f"{base_url}/api/harness/runtime?refresh=true")
    assert harness.get("enabled") is False, harness
    assert harness.get("ready") is False, harness
    # 后端规格会保留 Harness profile/package 清单，但默认不携带 node_modules；因此正确
    # 的目录发行诊断是“依赖未安装”，而不是把这一可选能力错误说成 Native 后端缺失。
    assert "依赖尚未安装或安装不完整" in str(harness.get("message", "")), harness

    connections = _get_json(f"{base_url}/api/mcp/connections")
    rows = connections.get("connections")
    assert isinstance(rows, list) and len(rows) == 1, connections
    connection = rows[0]
    assert isinstance(connection, dict), connections
    assert connection.get("status") == "disabled", connection
    assert connection.get("enabled") is False, connection
    tools = connection.get("tools")
    assert isinstance(tools, list) and tools and tools[0].get("commander_selectable") is False, connection
    assert not (scratch / "data" / "mcp_connections.json").exists(), connection

    vector = _get_json(f"{base_url}/api/knowledge/vector-capability")
    assert vector.get("fastembed_available") is True, vector
    assert vector.get("model_initialized") is False, vector
    assert "客户确认下载" in str(vector.get("message", "")), vector

    ocr = _get_json(f"{base_url}/api/knowledge/ocr-capability")
    assert ocr.get("paddleocr_available") is False, ocr
    assert ocr.get("model_initialized") is False, ocr
    assert "未安装" in str(ocr.get("message", "")), ocr

    # 写入刻意损坏的可选 MCP 状态，只验证它被安全隔离并给出恢复提示；不触发网络或子进程。
    connection_state = scratch / "data" / "mcp_connections.json"
    connection_state.parent.mkdir(parents=True, exist_ok=True)
    connection_state.write_text("{broken", encoding="utf-8")
    corrupted = _get_json(f"{base_url}/api/mcp/connections")
    corrupted_rows = corrupted.get("connections")
    assert isinstance(corrupted_rows, list) and len(corrupted_rows) == 1, corrupted
    corrupted_connection = corrupted_rows[0]
    assert isinstance(corrupted_connection, dict), corrupted
    assert corrupted_connection.get("status") == "degraded", corrupted_connection
    assert corrupted_connection.get("enabled") is False, corrupted_connection
    assert corrupted_connection.get("last_error_code") == "connection_state_invalid", corrupted_connection
    assert "连接已保持停用" in str(corrupted_connection.get("recovery_message", "")), corrupted_connection

    # 客户点击“停用”应当实际覆写损坏状态，而不是继续返回无法操作的错误。
    reset = httpx.post(
        f"{base_url}/api/mcp/connections/public-reference/disable",
        timeout=5.0,
    )
    reset.raise_for_status()
    reset_connection = reset.json().get("connection")
    assert isinstance(reset_connection, dict), reset.json()
    assert reset_connection.get("status") == "disabled", reset_connection
    assert reset_connection.get("enabled") is False, reset_connection
    assert not reset_connection.get("recovery_message"), reset_connection
    persisted = connection_state.read_text(encoding="utf-8")
    assert '"enabled": false' in persisted, persisted


def _verify_invalid_configuration(*, executable: Path, root: Path, scratch: Path) -> None:
    environment = _release_environment(root=root, scratch=scratch, port=_available_loopback_port())
    environment["AGENTFLOW_PORT"] = "not-a-number"
    completed = subprocess.run(
        [str(executable)],
        cwd=str(executable.parent),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    diagnostic = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 2, diagnostic
    assert "AGENTFLOW_STARTUP_CONFIG_INVALID" in diagnostic, diagnostic
    assert str(scratch) not in diagnostic, diagnostic


def main() -> None:
    root = _parse_args().release_root.resolve()
    executable = root / "backend" / "AgentFlowBackend.exe"
    if not executable.is_file():
        raise SystemExit("离线故障矩阵已停止：未找到 backend/AgentFlowBackend.exe。")

    scratch = Path(tempfile.mkdtemp(prefix="agentflow_directory_fault_matrix_"))
    port = _available_loopback_port()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=_release_environment(root=root, scratch=scratch, port=port),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_health(port=port, process=process)
        _verify_optional_runtime_states(port=port, scratch=scratch)
        if process.poll() is not None:
            raise RuntimeError("可选组件降级检查意外终止了 Native 后端。")
        process.terminate()
        process.wait(timeout=5)
        _verify_invalid_configuration(executable=executable, root=root, scratch=scratch / "invalid_config")
        print(
            "AgentFlow directory fault matrix passed: "
            "native=healthy node=disabled mcp=isolated vector=confirmation ocr=optional config=actionable"
        )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"离线故障矩阵已停止：{error}") from error
