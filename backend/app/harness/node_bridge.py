"""AgentFlow 对官方 `dsh --profile ...` 的受控单任务 Bridge。"""

from __future__ import annotations

import asyncio
import os
import subprocess

from app.core.config import settings
from app.harness.contracts import (
    HarnessControlResult,
    HarnessEventSink,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    HarnessRuntimeEvent,
)
from app.harness.node_profile import (
    _PROFILE_NAME,
    _profile_environment,
    preflight_node_harness_profile,
)
from app.harness.node_runtime import _first_output_line, _node_harness_cli_path
from app.services.model_gateway import ModelRuntime


class NodeHarnessBridge:
    """以最低权限启动官方 headless CLI。

    当前官方 headless 只会在结束时写出最终文本，因此这里刻意只发开始、心跳、最终结果或
    失败事件。它不伪造 token delta，也不允许通过这个 Bridge 接入 Shell、文件、网络或 MCP。
    """

    backend_id = "node_harness"

    async def execute_task(
        self,
        request: HarnessExecutionRequest,
        event_sink: HarnessEventSink,
        *,
        runtime: ModelRuntime,
    ) -> HarnessExecutionResult:
        """运行一个启用后才可执行的只读 DeepSeek Harness 子任务。"""

        if not settings.node_harness_enabled:
            return _failure("runtime_disabled", "Node Harness 当前未启用。")
        if request.permission_mode != "read-only":
            return _failure("permission_not_supported", "Node Harness 当前只支持 read-only。")
        if runtime.provider != "deepseek":
            return _failure("provider_not_supported", "Node Harness 首期只允许 DeepSeek Provider。")

        profile = await asyncio.to_thread(preflight_node_harness_profile)
        if not profile.ready:
            return _failure("profile_preflight_failed", profile.message)

        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_started",
                message="Node Harness 已在只读隔离 profile 中开始执行。",
            )
        )
        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_heartbeat",
                message="Node Harness 正在执行受控单任务；当前版本仅在结束时返回最终文本。",
            )
        )

        try:
            final_text = await asyncio.to_thread(_run_headless_task, request, runtime)
        except NodeHarnessBridgeError as error:
            message = f"Node Harness 执行失败：{error}"
            await event_sink(HarnessRuntimeEvent(kind="runtime_failed", message=message))
            return _failure("headless_failed", message)

        await event_sink(
            HarnessRuntimeEvent(
                kind="assistant_final",
                message="Node Harness 已返回最终文本。",
            )
        )
        return HarnessExecutionResult(
            status="completed",
            final_text=final_text,
            metadata={"backend": "node_harness", "profile": _PROFILE_NAME},
        )

    async def resume_task(
        self,
        task_id: str,
        resume_input: dict[str, object],
        event_sink: HarnessEventSink,
    ) -> HarnessExecutionResult:
        """官方 headless CLI 当前不暴露可恢复 session，明确拒绝而非伪造恢复。"""

        await event_sink(
            HarnessRuntimeEvent(
                kind="runtime_failed",
                message="Node Harness 当前 headless 模式不支持从检查点恢复。",
            )
        )
        return _failure("resume_not_supported", "Node Harness 当前 headless 模式不支持恢复。")

    async def cancel_task(self, task_id: str) -> HarnessControlResult:
        return HarnessControlResult(
            status="unsupported",
            message="Node Harness 当前只支持受控单任务批处理，尚无安全取消协议。",
        )

    async def close(self) -> None:
        return None


class NodeHarnessBridgeError(RuntimeError):
    """官方 CLI 子进程不能形成可信终态时抛出。"""


def _run_headless_task(request: HarnessExecutionRequest, runtime: ModelRuntime) -> str:
    """在隔离 cwd 与一次性环境中运行官方 headless CLI。"""

    cli_path = _node_harness_cli_path(settings.node_harness_runtime_dir)
    if not cli_path.is_file():
        raise NodeHarnessBridgeError("未找到项目内 dsh CLI。")

    launch_dir = settings.node_harness_launch_dir
    environment = _task_environment(launch_dir, request, runtime)
    try:
        completed = subprocess.run(
            (str(cli_path), "--profile", _PROFILE_NAME, request.task_text),
            cwd=launch_dir,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(request.timeout_seconds, settings.node_harness_task_timeout_seconds),
            shell=False,
        )
    except FileNotFoundError as error:
        raise NodeHarnessBridgeError("未找到 Node.js 可执行文件。") from error
    except subprocess.TimeoutExpired as error:
        raise NodeHarnessBridgeError("受控 headless 任务超时。") from error
    except OSError as error:
        raise NodeHarnessBridgeError("无法启动项目内 dsh CLI。") from error

    final_text = completed.stdout.strip()
    if completed.returncode != 0 or not final_text:
        # stderr 只作为内部短诊断来源，严格限长并剔除可能的 Key 形式，避免进入任务事件。
        detail = _sanitize_diagnostic(_first_output_line(completed.stderr))
        suffix = f"（{detail}）" if detail else ""
        raise NodeHarnessBridgeError(f"CLI 退出码 {completed.returncode}{suffix}")
    return final_text


def _task_environment(
    launch_dir, request: HarnessExecutionRequest, runtime: ModelRuntime
) -> dict[str, str]:
    """只在子进程内临时注入本次获准的 DeepSeek 连接信息。"""

    environment = _profile_environment(launch_dir)
    # `dsh-credentials-local` 把继承环境排在其它来源之前；这里不创建 credentials 文件，
    # 子进程结束后明文 Key 仅随该进程环境销毁。不要把 environment 写入日志或返回对象。
    environment["DEEPSEEK_API_KEY"] = runtime.api_key
    environment["DEEPSEEK_BASE_URL"] = runtime.base_url
    environment["AGENTFLOW_HARNESS_MODEL"] = runtime.model
    environment["AGENTFLOW_HARNESS_THINKING"] = runtime.thinking
    environment["AGENTFLOW_HARNESS_MAX_TOKENS"] = str(request.max_output_tokens)
    environment["AGENTFLOW_HARNESS_IDLE_TIMEOUT_MS"] = str(
        max(5_000, int(request.timeout_seconds * 1_000))
    )
    # 允许系统 PATH 支撑 `dsh.cmd` 寻找 Node，但不把当前工作目录更换为客户 workspace。
    return environment


def _sanitize_diagnostic(value: str) -> str:
    """返回适合任务事件的有限错误摘要，不透传 API Key 或大段 Provider 内容。"""

    sanitized = value.replace("\r", " ").replace("\n", " ").strip()
    for prefix in ("sk-", "ark-"):
        if prefix in sanitized:
            sanitized = sanitized.split(prefix, 1)[0].rstrip()
    return sanitized[:160]


def _failure(code: str, message: str) -> HarnessExecutionResult:
    return HarnessExecutionResult(status="failed", failure_code=code, metadata={"message": message})
