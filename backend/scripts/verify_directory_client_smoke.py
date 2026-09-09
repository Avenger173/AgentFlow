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
from pathlib import Path

import httpx


WM_CLOSE = 0x0010


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证 AgentFlow 目录发行客户端启动与关闭。")
    parser.add_argument("--release-root", type=Path, required=True)
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


def _wait_for_health(port: int, process: subprocess.Popen[bytes]) -> None:
    endpoint = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("目录发行客户端在随包后端就绪前提前退出。")
        try:
            response = httpx.get(endpoint, timeout=1.0)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("目录发行客户端未能在 30 秒内拉起随包后端。")


def _wait_for_port_release(port: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.25)
    raise RuntimeError("目录发行客户端关闭后，本机后端端口仍未释放。")


def main() -> None:
    root = _parse_args().release_root.resolve()
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
        process = subprocess.Popen([str(executable)], cwd=str(root), env=environment)
        _wait_for_health(port, process)
        if not _request_main_window_close(process.pid):
            raise RuntimeError("目录发行客户端没有可关闭的主窗口。")
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired as error:
            # 只清理本脚本创建的进程树，避免失败夹具持续占用 8765；绝不枚举或终止其他程序。
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True)
            raise RuntimeError("目录发行客户端未在正常关闭窗口内退出。") from error
        _wait_for_port_release(port)
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
