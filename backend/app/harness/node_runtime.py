"""DeepSeek Harness Node Runtime 的项目内发现与无密钥探针。

H0 阶段只确认项目锁定的 Node 依赖可被安全发现。这里刻意不启动 `dsh web`、
不提交 prompt，也不把模型密钥传给子进程。真正的 session、流式事件和 Tool
装配必须经过后续 Node Bridge 与 RuntimeRouter，不能绕过 AgentFlow 的权限审计。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.schemas.harness import HarnessRuntimeStatus


_CACHE_TTL_SECONDS = 5.0
_SECRET_ENVIRONMENT_KEYS = {
    "AGENTFLOW_LLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
}


@dataclass(frozen=True)
class _CachedProbe:
    """短时缓存，避免设置页轮询时反复创建 Node 子进程。"""

    created_at: float
    status: HarnessRuntimeStatus


_PROBE_LOCK = threading.Lock()
_CACHED_PROBE: _CachedProbe | None = None


def get_node_harness_runtime_status(*, refresh: bool = False) -> HarnessRuntimeStatus:
    """返回项目内 Node Harness 的可用性，不触发模型或 Agent 执行。"""

    global _CACHED_PROBE
    now = time.monotonic()
    with _PROBE_LOCK:
        if not refresh and _CACHED_PROBE and now - _CACHED_PROBE.created_at < _CACHE_TTL_SECONDS:
            return _CACHED_PROBE.status

        status = _probe_node_harness_runtime()
        _CACHED_PROBE = _CachedProbe(created_at=now, status=status)
        return status


def clear_node_harness_probe_cache() -> None:
    """供安装后验证和后续运行时更新主动刷新短时状态缓存。"""

    global _CACHED_PROBE
    with _PROBE_LOCK:
        _CACHED_PROBE = None


def _probe_node_harness_runtime() -> HarnessRuntimeStatus:
    runtime_root = settings.node_harness_runtime_dir
    cli_path = _node_harness_cli_path(runtime_root)
    package_manifest = runtime_root / "package.json"

    if not package_manifest.is_file():
        return _status(
            installed=False,
            ready=False,
            message="未找到项目内 DeepSeek Harness Runtime 清单。",
        )

    if not cli_path.is_file():
        return _status(
            installed=False,
            ready=False,
            message="DeepSeek Harness 依赖尚未安装或安装不完整。",
        )

    try:
        node_version = _run_probe_command(("node", "--version"), runtime_root)
        harness_version = _run_probe_command((str(cli_path), "--version"), runtime_root)
    except NodeHarnessProbeError as error:
        return _status(
            installed=True,
            ready=False,
            message=f"DeepSeek Harness 探针失败：{error}",
        )

    return _status(
        installed=True,
        ready=True,
        node_version=node_version,
        harness_version=harness_version,
        message=(
            "项目内 Node Harness 已就绪。当前仅开放无密钥健康探针；"
            "Agent、Shell、MCP、文件写入和联网工具仍未启用。"
        ),
    )


def _status(
    *,
    installed: bool,
    ready: bool,
    message: str,
    node_version: str | None = None,
    harness_version: str | None = None,
) -> HarnessRuntimeStatus:
    return HarnessRuntimeStatus(
        enabled=settings.node_harness_enabled,
        installed=installed,
        ready=ready,
        node_version=node_version,
        harness_version=harness_version,
        message=message,
        capabilities=["project_local", "version_probe", "feature_flag"],
    )


def _node_harness_cli_path(runtime_root: Path) -> Path:
    """解析 npm 本地 bin，不依赖全局 PATH 或全局 npm 安装。"""

    command_name = "dsh.cmd" if os.name == "nt" else "dsh"
    return runtime_root / "node_modules" / ".bin" / command_name


def _run_probe_command(command: tuple[str, ...], runtime_root: Path) -> str:
    """运行只读版本命令，并把环境、输出和超时限制在受控范围内。"""

    try:
        completed = subprocess.run(
            command,
            cwd=runtime_root,
            env=_probe_environment(),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.node_harness_probe_timeout_seconds,
            shell=False,
        )
    except FileNotFoundError as error:
        raise NodeHarnessProbeError("未找到 Node.js 可执行文件。") from error
    except subprocess.TimeoutExpired as error:
        raise NodeHarnessProbeError("版本探针超时。") from error
    except OSError as error:
        raise NodeHarnessProbeError("无法启动项目内 Node Runtime。") from error

    output = _first_output_line(completed.stdout)
    if completed.returncode != 0 or not output:
        # stderr 可能包含本机路径或环境细节，只保留一行长度受限的非敏感诊断。
        detail = _first_output_line(completed.stderr)
        suffix = f"（{detail[:160]}）" if detail else ""
        raise NodeHarnessProbeError(f"命令退出码 {completed.returncode}{suffix}")
    return output


def _probe_environment() -> Mapping[str, str]:
    """为无密钥探针构造子进程环境。

    `settings` 会读取本地 .env；若直接继承环境，dsh 即使只执行 `--version` 也可能
    看见用户模型 Key。首期探针主动移除已知模型 Key，并关闭遥测。后续真实调用改为
    从 ModelGateway 临时注入最小 Key 集合，绝不复用这个探针环境。
    """

    environment = dict(os.environ)
    for key in _SECRET_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["DSH_TELEMETRY_DISABLED"] = "1"
    environment["DSH_HOME"] = str(settings.node_harness_state_dir)
    return environment


def _first_output_line(value: str) -> str:
    for line in value.splitlines():
        if line.strip():
            return line.strip()
    return ""


class NodeHarnessProbeError(RuntimeError):
    """Node Runtime 的本地启动或版本探针未通过。"""

