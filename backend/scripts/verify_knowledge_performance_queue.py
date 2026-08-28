"""离线验证 K5.8 的知识库性能建议与后台运行队列。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEMP_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_knowledge_performance_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(TEMP_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))


async def _verify_queue() -> None:
    """同类任务严格 FIFO，另一个工作通道不会被无关等待队列阻塞。"""

    from app.services.knowledge_runtime_queue import KnowledgeRuntimeQueue

    # 夹具固定在两通道模式，独立验证“同类 FIFO + 异类可并行”的队列规则；低配全局串行
    # 的资源选择由性能 Profile 在应用启动时决定，不依赖测试机器本身的核数。
    queue = KnowledgeRuntimeQueue(max_active_work_kinds=2)
    first = await queue.reserve(work_id="kb_job_first", work_kind="index")
    assert first is not None

    waiting_notified = asyncio.Event()
    observed_ahead_counts: list[int] = []

    async def on_waiting(ahead_count: int) -> None:
        observed_ahead_counts.append(ahead_count)
        waiting_notified.set()

    second_task = asyncio.create_task(
        queue.reserve(work_id="kb_job_second", work_kind="index", on_waiting=on_waiting)
    )
    await asyncio.wait_for(waiting_notified.wait(), timeout=1)
    snapshot = await queue.snapshot()
    assert observed_ahead_counts == [1]
    assert [item.work_id for item in snapshot.active_items] == ["kb_job_first"]
    assert [item.work_id for item in snapshot.waiting_items] == ["kb_job_second"]
    assert snapshot.waiting_items[0].queue_position == 2

    # 同一 job 可能因为重复点击被再次受理；第二条协程不能进入同一个索引链。
    assert await queue.reserve(work_id="kb_job_second", work_kind="index") is None

    # 深度任务属于独立通道，不会被索引等待队列饿死；同类仍由各自 FIFO 保护。
    deep = await queue.reserve(work_id="task_k4_first", work_kind="deep_task")
    assert deep is not None
    cross_snapshot = await queue.snapshot()
    assert {item.work_id for item in cross_snapshot.active_items} == {"kb_job_first", "task_k4_first"}
    await deep.release()

    await first.release()
    second = await asyncio.wait_for(second_task, timeout=1)
    assert second is not None
    after_release = await queue.snapshot()
    assert [item.work_id for item in after_release.active_items] == ["kb_job_second"]
    assert not after_release.waiting_items
    await second.release()
    finished_snapshot = await queue.snapshot()
    assert not finished_snapshot.active_items
    assert not finished_snapshot.waiting_items

    # 低配策略会让不同类型的重任务也共用一个槽位；待索引释放后深度任务才允许开始。
    low_resource_queue = KnowledgeRuntimeQueue(max_active_work_kinds=1)
    low_index = await low_resource_queue.reserve(work_id="kb_job_low", work_kind="index")
    assert low_index is not None
    low_waiting = asyncio.Event()

    async def on_low_waiting(_: int) -> None:
        low_waiting.set()

    low_deep_task = asyncio.create_task(
        low_resource_queue.reserve(
            work_id="task_k4_low",
            work_kind="deep_task",
            on_waiting=on_low_waiting,
        )
    )
    await asyncio.wait_for(low_waiting.wait(), timeout=1)
    low_snapshot = await low_resource_queue.snapshot()
    assert [item.work_id for item in low_snapshot.active_items] == ["kb_job_low"]
    assert [item.work_id for item in low_snapshot.waiting_items] == ["task_k4_low"]
    await low_index.release()
    low_deep = await asyncio.wait_for(low_deep_task, timeout=1)
    assert low_deep is not None
    await low_deep.release()


def _verify_profile_and_api() -> None:
    """性能样本只保留耗时，API 同时返回当前进程队列而不是材料信息。"""

    from fastapi.testclient import TestClient

    from app.services.knowledge_performance import (
        build_knowledge_performance_profile,
        record_knowledge_deep_task_elapsed_ms,
        record_knowledge_retrieval_elapsed_ms,
    )
    from main import app

    record_knowledge_retrieval_elapsed_ms(34)
    record_knowledge_retrieval_elapsed_ms(51)
    record_knowledge_deep_task_elapsed_ms(420)
    profile = build_knowledge_performance_profile()
    assert profile.retrieval_observation.sample_count >= 2
    assert profile.retrieval_observation.median_elapsed_ms == 42
    assert profile.deep_task_observation.sample_count >= 1
    assert profile.runtime_queue.process_local is True
    assert "正文" in profile.privacy_notice

    with TestClient(app) as client:
        response = client.get("/api/knowledge/performance")
        response.raise_for_status()
        payload = response.json()
    assert payload["runtime_queue"]["index_active_limit"] == 1
    assert payload["runtime_queue"]["deep_task_active_limit"] == 1
    assert payload["runtime_queue"]["max_active_work_kinds"] in {1, 2}
    assert payload["retrieval_observation"]["sample_count"] >= 2
    assert "logical_cpu_count" in payload
    assert "privacy_notice" in payload


def main() -> None:
    try:
        asyncio.run(_verify_queue())
        _verify_profile_and_api()
        print("Knowledge performance and runtime queue verification passed.")
    finally:
        shutil.rmtree(TEMP_DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
