"""DeepSeek Harness 项目专属 profile 的物化与无密钥预检。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.core.config import settings
from app.harness.node_runtime import (
    _SECRET_ENVIRONMENT_KEYS,
    _first_output_line,
    _node_harness_cli_path,
    get_node_harness_runtime_status,
)
from app.schemas.harness import HarnessProfilePreflight


_PROFILE_NAME = "agentflow-readonly"
_PROFILE_TEMPLATE_DIRNAME = "agentflow_profile"
_PROFILE_FILES = ("package.json", "cordis.patch.yml")
_DISABLED_ENTRY_IDS = (
    "pwsh-sandbox",
    "tool-pwsh",
    "tool-jobs",
    "tool-fs",
    "tool-fs-search",
    "tool-str-replace-editor",
    "tool-web",
    "web",
    "web-search-deepseek",
    "tool-subagent",
    "tool-subagent-fork",
    "tool-subagent-control",
    "tool-subagent-list-agents",
    "tool-subagent-report",
    "workflow-worker-thread",
    "tool-workflow",
    "tool-ralph",
    "tool-skill",
    "skill",
    "skill-filesystem",
    "tool-goal",
    "tool-todo",
    "commands",
    "command-feedback",
    "command-goal",
    "agent-instructions",
    "session-title-llm",
    "settings",
)


class NodeHarnessProfileError(RuntimeError):
    """项目专属 profile 无法安全准备或组合时抛出。"""


def preflight_node_harness_profile() -> HarnessProfilePreflight:
    """物化只读 profile，并用官方 CLI 回读最终组合配置。

    这不是任务执行接口：不传 API Key、不提交 prompt、不启动 Agent，也不允许调用 Tool。
    它只为后续 Node Bridge 证明 profile 和启动目录的最小权限边界能够被官方 CLI 接受。
    """

    runtime_status = get_node_harness_runtime_status(refresh=True)
    if not runtime_status.ready:
        return _failure(f"Node Harness 未就绪：{runtime_status.message}")

    try:
        _profile_dir, launch_dir = _materialize_profile()
        config_dump = _dump_profile_config(launch_dir)
        _validate_profile_config(config_dump)
    except NodeHarnessProfileError as error:
        return _failure(f"Node Harness profile 预检失败：{error}")

    return HarnessProfilePreflight(
        profile_name=_PROFILE_NAME,
        ready=True,
        message="AgentFlow 只读 Harness profile 已通过组合回读；未启动模型、Agent 或 Tool。",
        disabled_entries=list(_DISABLED_ENTRY_IDS),
    )


def _materialize_profile() -> tuple[Path, Path]:
    """把随包 profile 原子同步到项目专属 DSH_HOME。"""

    runtime_dir = settings.node_harness_runtime_dir
    template_dir = runtime_dir / _PROFILE_TEMPLATE_DIRNAME
    profile_dir = settings.node_harness_state_dir / "profiles" / _PROFILE_NAME
    launch_dir = settings.node_harness_launch_dir
    if not template_dir.is_dir():
        raise NodeHarnessProfileError("缺少随项目发布的只读 profile 模板。")

    # DSH_HOME 与启动目录都由 AgentFlow 管理，避免官方 CLI 从客户 workspace 读取 `.env`。
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_dir.mkdir(parents=True, exist_ok=True)
    if (launch_dir / ".env").exists():
        raise NodeHarnessProfileError("受控 Harness 启动目录中存在 .env，已拒绝预检。")

    for filename in _PROFILE_FILES:
        source = template_dir / filename
        if not source.is_file():
            raise NodeHarnessProfileError(f"profile 模板缺少 {filename}。")
        _write_text_if_changed(profile_dir / filename, source.read_text(encoding="utf-8"))

    return profile_dir, launch_dir


def _write_text_if_changed(target: Path, content: str) -> None:
    """用同目录临时文件替换 profile 配置，避免 CLI 读到半写入 YAML。"""

    if target.is_file() and target.read_text(encoding="utf-8") == content:
        return

    temporary = target.with_name(f".{target.name}.agentflow.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(target)


def _dump_profile_config(launch_dir: Path) -> str:
    runtime_dir = settings.node_harness_runtime_dir
    cli_path = _node_harness_cli_path(runtime_dir)
    if not cli_path.is_file():
        raise NodeHarnessProfileError("未找到项目内 dsh CLI。")

    try:
        completed = subprocess.run(
            (str(cli_path), "--profile", _PROFILE_NAME, "--dump-config"),
            cwd=launch_dir,
            env=_profile_environment(launch_dir),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.node_harness_profile_timeout_seconds,
            shell=False,
        )
    except FileNotFoundError as error:
        raise NodeHarnessProfileError("未找到 Node.js 可执行文件。") from error
    except subprocess.TimeoutExpired as error:
        raise NodeHarnessProfileError("profile 组合检查超时。") from error
    except OSError as error:
        raise NodeHarnessProfileError("无法启动项目内 dsh CLI。") from error

    if completed.returncode != 0 or not completed.stdout.strip():
        detail = _first_output_line(completed.stderr)
        suffix = f"（{detail[:160]}）" if detail else ""
        raise NodeHarnessProfileError(f"dsh 配置检查退出码 {completed.returncode}{suffix}")
    return completed.stdout


def _profile_environment(launch_dir: Path) -> dict[str, str]:
    """为 profile 预检构造隔离环境，不继承任何模型密钥。"""

    environment = dict(os.environ)
    for key in _SECRET_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["DSH_TELEMETRY_DISABLED"] = "1"
    environment["DSH_HOME"] = str(settings.node_harness_state_dir)
    environment["DSH_CWD"] = str(launch_dir)
    # 显式指定只读模式，即使模板被意外修改也不会将 sandbox 升级为可写权限。
    environment["DSH_PERMISSION_MODE"] = "read-only"
    return environment


def _validate_profile_config(config_dump: str) -> None:
    """验证官方 CLI 实际组合后的条目，而不只相信模板文本。"""

    if not _entry_contains(config_dump, "sandbox-policy", "mode: read-only"):
        raise NodeHarnessProfileError("sandbox-policy 未处于 read-only。")
    if not _entry_contains(config_dump, "approval", "policy: ask"):
        raise NodeHarnessProfileError("approval 未处于 ask。")

    enabled_entries = [entry_id for entry_id in _DISABLED_ENTRY_IDS if not _entry_contains(
        config_dump,
        entry_id,
        "disabled: true",
    )]
    if enabled_entries:
        raise NodeHarnessProfileError(
            f"以下默认能力未被禁用：{', '.join(enabled_entries)}。"
        )


def _entry_contains(config_dump: str, entry_id: str, expected_line: str) -> bool:
    """在 dump-config 的单个顶级 entry 中精确查找预期配置行。"""

    marker = f"- id: {entry_id}\n"
    start = config_dump.find(marker)
    if start < 0:
        return False
    end = config_dump.find("\n- id: ", start + len(marker))
    block = config_dump[start:] if end < 0 else config_dump[start:end]
    return any(line.strip() == expected_line for line in block.splitlines())


def _failure(message: str) -> HarnessProfilePreflight:
    return HarnessProfilePreflight(
        profile_name=_PROFILE_NAME,
        ready=False,
        message=message,
        disabled_entries=list(_DISABLED_ENTRY_IDS),
    )
