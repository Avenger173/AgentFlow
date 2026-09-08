"""测量 LGM5.7 Native/Graph 组合执行壳的同机初始化资源基线。

每个后端都在新 Python 子进程中初始化，避免已导入模块掩盖 LangGraph 的真实冷启动成本。
本脚本只构造最小只读组合计划和临时 Graph checkpoint，不读取任务库、客户材料或凭据，
也不调用模型、网络或专业 Agent。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
_MIB = 1024 * 1024
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LGM5.7 组合执行壳资源基线探针。")
    parser.add_argument("--probe", choices=("native", "graph"))
    parser.add_argument(
        "--checkpoint-path",
        help="仅 Graph 子进程使用的临时 checkpoint 路径。",
    )
    return parser


def _probe_native() -> dict[str, int | str]:
    started_at = time.perf_counter()
    from app.schemas.chat import WorkflowStep
    from app.workflow import runtime as native_runtime

    steps = [
        WorkflowStep(
            id="step_2",
            agent="data_agent",
            action="analyze_dataset",
            title="数据只读预览",
            parallel_group="specialist_read_only",
        ),
        WorkflowStep(
            id="step_3",
            agent="knowledge_agent",
            action="answer_question",
            title="知识库可信问答",
            parallel_group="specialist_read_only",
        ),
    ]
    # 这会走真实的 Provider 槽位解析，但不会创建模型客户端或发送任何请求。
    native_runtime._composition_worker_count(steps)
    return _measurement("native", started_at)


async def _probe_graph_async(checkpoint_path: Path) -> dict[str, int | str]:
    started_at = time.perf_counter()
    from app.harness.langgraph_commander_composition_shadow import (
        LangGraphCommanderCompositionShadowBackend,
    )

    backend = LangGraphCommanderCompositionShadowBackend(
        checkpoint_path=checkpoint_path,
        adapters={},
    )
    try:
        await backend._ensure_graph()
        return _measurement("graph", started_at)
    finally:
        await backend.close()


def _measurement(kind: str, started_at: float) -> dict[str, int | str]:
    return {
        "kind": kind,
        "startup_ms": max(1, math.ceil((time.perf_counter() - started_at) * 1000)),
        "resident_memory_mib": max(1, math.ceil(_process_rss_bytes() / _MIB)),
    }


def _process_rss_bytes() -> int:
    """优先读取当前进程实际 RSS；不引入新的运行时依赖。"""

    try:
        import psutil
    except ModuleNotFoundError:
        psutil = None
    if psutil is not None:
        return int(psutil.Process().memory_info().rss)
    if os.name != "nt":
        raise RuntimeError("当前资源探针需要 psutil 或 Windows RSS API。")

    from ctypes import wintypes

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    success = ctypes.windll.psapi.GetProcessMemoryInfo(
        handle,
        ctypes.byref(counters),
        counters.cb,
    )
    if not success:
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.WorkingSetSize)


def _run_child(kind: str, checkpoint_path: Path | None = None) -> dict[str, int | str]:
    command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--probe", kind]
    if checkpoint_path is not None:
        command.extend(("--checkpoint-path", str(checkpoint_path)))
    environment = os.environ.copy()
    environment["AGENTFLOW_CHAT_MODE"] = "mock"
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        command,
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{kind} resource probe failed: {result.stderr.strip()}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(f"{kind} resource probe returned unexpected output")
    payload = json.loads(lines[0])
    if payload.get("kind") != kind:
        raise RuntimeError(f"{kind} resource probe returned mismatched kind")
    return payload


def _run_measurement() -> dict[str, object]:
    probe_root = Path(tempfile.mkdtemp(prefix="agentflow_lgm57_resource_"))
    try:
        native = _run_child("native")
        graph = _run_child("graph", probe_root / "graph.sqlite")
        native_startup = int(native["startup_ms"])
        native_memory = int(native["resident_memory_mib"])
        graph_startup = int(graph["startup_ms"])
        graph_memory = int(graph["resident_memory_mib"])
        return {
            "native": native,
            "graph": graph,
            "within_10_percent": (
                graph_startup <= math.floor(native_startup * 1.10)
                and graph_memory <= math.floor(native_memory * 1.10)
            ),
        }
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


def main() -> None:
    args = _parser().parse_args()
    if args.probe == "native":
        print(json.dumps(_probe_native(), ensure_ascii=True))
        return
    if not args.checkpoint_path:
        raise SystemExit("Graph probe requires --checkpoint-path.")
    print(json.dumps(asyncio.run(_probe_graph_async(Path(args.checkpoint_path))), ensure_ascii=True))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(json.dumps(_run_measurement(), ensure_ascii=True))
    else:
        main()
