"""验证完整目录发行的 Qt 客户端会自动拉起随包后端。

该脚本只针对命令行显式给出的候选目录启动一个自己创建的 AgentFlow.exe 进程。运行数据
写入系统临时目录，后端保持 mock 模式；不会读取 .env、客户材料、模型配置或联网调用模型。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
import psutil


WM_CLOSE = 0x0010


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证 AgentFlow 目录发行客户端启动与关闭。")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument(
        "--metrics-output",
        type=Path,
        help="可选：将本次无正文启动/关闭指标写为 JSON，供目录发行基准脚本聚合。",
    )
    parser.add_argument(
        "--read-only-probe-concurrency",
        type=int,
        default=0,
        choices=range(0, 17),
        help="可选：后端就绪后并发访问受控只读状态端点的请求数；0 表示跳过。",
    )
    return parser.parse_args()


def _assert_port_available(port: int) -> None:
    """防止候选客户端误把其他 AgentFlow 实例当成自己的后端。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"端口 {port} 已被占用，拒绝执行客户端冒烟。") from error


def _request_main_window_close(process_id: int) -> bool:
    """向本脚本启动的 Qt 顶层窗口发送 WM_CLOSE，保留 BackendManager 正常清理机会。"""

    user32 = ctypes.windll.user32
    target_window = ctypes.c_void_p()

    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def find_window(handle: int, _context: int) -> bool:
        nonlocal target_window
        owner_process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner_process_id))
        if owner_process_id.value == process_id and user32.IsWindowVisible(handle):
            target_window = ctypes.c_void_p(handle)
            return False
        return True

    user32.EnumWindows(find_window, 0)
    if not target_window.value:
        return False
    return bool(user32.PostMessageW(target_window, WM_CLOSE, 0, 0))


def _wait_for_health(port: int, process: subprocess.Popen[bytes]) -> float:
    endpoint = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("目录发行客户端在随包后端就绪前提前退出。")
        try:
            response = httpx.get(endpoint, timeout=1.0)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return time.monotonic()
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("目录发行客户端未能在 30 秒内拉起随包后端。")


def _wait_for_port_release(port: int) -> float:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return time.monotonic()
        time.sleep(0.25)
    raise RuntimeError("目录发行客户端关闭后，本机后端端口仍未释放。")


def _snapshot_runtime_processes(process_id: int) -> tuple[int, list[str], list[int]]:
    """返回主客户端及其直属运行时树的 RSS/名称/PID，不读取命令行或客户数据。"""

    root = psutil.Process(process_id)
    processes = [root, *root.children(recursive=True)]
    names: list[str] = []
    process_ids: list[int] = []
    resident_bytes = 0
    for item in processes:
        try:
            names.append(item.name())
            process_ids.append(item.pid)
            resident_bytes += item.memory_info().rss
        except (psutil.Error, OSError):
            continue
    return resident_bytes, names, process_ids


def _probe_read_only_endpoints(*, port: int, concurrency: int) -> dict[str, int]:
    """验证候选包在并发状态查询下仍保持 Native Runtime，不读取材料或启动可选组件。"""

    if concurrency <= 0:
        return {"readonly_request_count": 0, "readonly_max_ms": 0}

    base_url = f"http://127.0.0.1:{port}"
    endpoints = (
        "/health",
        "/api/mcp/connections",
        "/api/knowledge/vector-capability",
        "/api/knowledge/ocr-capability",
    )

    def request(endpoint: str) -> int:
        started_at = time.monotonic()
        response = httpx.get(f"{base_url}{endpoint}", timeout=5.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("并发只读状态请求没有返回 JSON object。")
        return round((time.monotonic() - started_at) * 1000)

    elapsed_ms: list[int] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(request, endpoints[index % len(endpoints)]) for index in range(concurrency)]
        for future in as_completed(futures):
            try:
                elapsed_ms.append(future.result())
            except (httpx.HTTPError, RuntimeError) as error:
                raise RuntimeError(f"并发只读状态请求失败：{error}") from error
    return {"readonly_request_count": len(elapsed_ms), "readonly_max_ms": max(elapsed_ms, default=0)}


def _write_metrics(path: Path, metrics: dict[str, Any]) -> None:
    """只写入聚合性能事实，拒绝把候选绝对路径、日志或客户内容写进基准文件。"""

    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    arguments = _parse_args()
    root = arguments.release_root.resolve()
    executable = root / "AgentFlow.exe"
    backend = root / "backend" / "AgentFlowBackend.exe"
    if not executable.is_file() or not backend.is_file():
        raise SystemExit("目录发行客户端验证已停止：候选包缺少根级入口或随包后端。")

    port = 8765
    _assert_port_available(port)
    scratch = Path(tempfile.mkdtemp(prefix="agentflow_directory_client_verify_"))
    environment = os.environ.copy()
    # 空值使 Qt 走“同级 AgentFlowBackend.exe”自动判定，而不是依赖测试专用发布环境变量。
    environment["AGENTFLOW_RELEASE_MODE"] = ""
    environment.update(
        {
            "AGENTFLOW_DATA_DIR": str(scratch / "data"),
            "AGENTFLOW_OUTPUT_DIR": str(scratch / "output"),
            "AGENTFLOW_USER_AGENTS_DIR": str(scratch / "agents"),
            "AGENTFLOW_CHAT_MODE": "mock",
            "AGENTFLOW_PORT": str(port),
        }
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        started_at = time.monotonic()
        process = subprocess.Popen([str(executable)], cwd=str(root), env=environment)
        ready_at = _wait_for_health(port, process)
        resident_bytes, process_names, process_ids = _snapshot_runtime_processes(process.pid)
        expected_processes = {"agentflow.exe", "agentflowbackend.exe"}
        if any(name.lower() not in expected_processes for name in process_names):
            raise RuntimeError("目录发行默认运行时出现未批准的子进程。")
        if "agentflowbackend.exe" not in {name.lower() for name in process_names}:
            raise RuntimeError("目录发行客户端未持有随包后端进程。")
        read_only_metrics = _probe_read_only_endpoints(
            port=port,
            concurrency=arguments.read_only_probe_concurrency,
        )
        if not _request_main_window_close(process.pid):
            raise RuntimeError("目录发行客户端没有可关闭的主窗口。")
        close_requested_at = time.monotonic()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired as error:
            # 只清理本脚本创建的进程树，避免失败夹具持续占用 8765；绝不枚举或终止其他程序。
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True)
            raise RuntimeError("目录发行客户端未在正常关闭窗口内退出。") from error
        port_released_at = _wait_for_port_release(port)
        still_running = [process_id for process_id in process_ids if psutil.pid_exists(process_id)]
        if still_running:
            raise RuntimeError("目录发行客户端关闭后仍保留自身运行时进程。")
        if arguments.metrics_output is not None:
            _write_metrics(
                arguments.metrics_output,
                {
                    "client_ready_ms": round((ready_at - started_at) * 1000),
                    "runtime_rss_bytes": resident_bytes,
                    "client_close_ms": round((port_released_at - close_requested_at) * 1000),
                    "runtime_process_count": len(process_ids),
                    **read_only_metrics,
                },
            )
        print("AgentFlow directory client smoke passed: bundled-backend=healthy close=clean")
    finally:
        if process is not None and process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f"目录发行客户端验证已停止：{error}") from error
