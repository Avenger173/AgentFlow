"""知识库重负载任务的进程内运行队列。

索引会打开/解析多份本地材料并写入 FTS、向量目录；深度任务会持续调用模型并保存大量
checkpoint。两类工作各自只允许一个活动任务，既保留普通问答的即时性，也不让同类任务在
Windows 上同时争抢 CPU、内存、SQLite 写入和文件句柄。任务的业务状态仍保存到 SQLite，
这里仅维护当前进程的排队顺序。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Awaitable, Callable

from app.schemas.knowledge import (
    KnowledgeRuntimeQueueItem,
    KnowledgeRuntimeQueueSnapshot,
    KnowledgeRuntimeWorkKind,
)
from app.services.knowledge_performance import knowledge_runtime_queue_max_active_work_kinds


KnowledgeQueueWaitingCallback = Callable[[int], Awaitable[None]]


@dataclass(frozen=True)
class _QueueEntry:
    """队列内部身份只使用任务/索引 ID，不保存资料库、路径或材料内容。"""

    work_id: str
    work_kind: KnowledgeRuntimeWorkKind
    queued_at: float


class KnowledgeRuntimeQueueReservation:
    """已经获得一个受控运行槽位的句柄；调用者必须在 finally 中释放。"""

    def __init__(self, queue: "KnowledgeRuntimeQueue", entry: _QueueEntry) -> None:
        self._queue = queue
        self._entry = entry
        self._released = False

    async def release(self) -> None:
        """释放当前 work kind 的槽位，并唤醒同类任务的下一位等待者。"""

        if self._released:
            return
        self._released = True
        await self._queue.release(self._entry)


class KnowledgeRuntimeQueue:
    """按工作类型串行的异步队列。

    不使用线程池阻塞等待：后台协程在等待时会让出事件循环，Qt 对应的 HTTP/WebSocket 请求可
    继续处理。中高配时索引和深度任务分为两条受控通道，避免某个长深度任务把客户的新索引
    无限期饿死；低配时两条通道会退回全局串行。同类通道始终严格 FIFO 串行。
    """

    def __init__(self, *, max_active_work_kinds: int | None = None) -> None:
        self._condition = asyncio.Condition()
        # App 启动时根据粗粒度本机资源固定该限制。运行中不因磁盘临时波动改变队列规则，
        # 避免正在等待的客户任务被不可见地重新排序；重启后会重新评估。
        self._max_active_work_kinds = max(
            1,
            min(2, max_active_work_kinds or knowledge_runtime_queue_max_active_work_kinds()),
        )
        self._active: dict[KnowledgeRuntimeWorkKind, _QueueEntry | None] = {
            "index": None,
            "deep_task": None,
        }
        self._waiting: dict[KnowledgeRuntimeWorkKind, deque[_QueueEntry]] = {
            "index": deque(),
            "deep_task": deque(),
        }

    async def reserve(
        self,
        *,
        work_id: str,
        work_kind: KnowledgeRuntimeWorkKind,
        on_waiting: KnowledgeQueueWaitingCallback | None = None,
    ) -> KnowledgeRuntimeQueueReservation | None:
        """按 FIFO 等待一个槽位；同一 work ID 重复受理时返回 ``None``。

        K4 的“继续”按钮可能在网络较慢时被连续点击。重复后台协程不能再次进入同一 checkpoint
        链，因此 queue 在进程内去重；持久化任务状态仍由原有 Runtime 负责判定。
        """

        entry = _QueueEntry(work_id=work_id, work_kind=work_kind, queued_at=monotonic())
        async with self._condition:
            if self._contains_work_id_locked(entry):
                return None
            # 中高配只等待同类通道；低配使用全局槽位，另一通道的活动任务同样在前方。
            if self._max_active_work_kinds == 1:
                active_ahead_count = self._active_work_kind_count_locked()
            else:
                active_ahead_count = 1 if self._active[work_kind] else 0
            ahead_count = len(self._waiting[work_kind]) + active_ahead_count
            self._waiting[work_kind].append(entry)

        if ahead_count and on_waiting is not None:
            try:
                await on_waiting(ahead_count)
            except Exception:
                # 状态推送是观察面；推送失败不能改变 FIFO 或使重任务永远卡在等待队列里。
                pass

        try:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self._active[work_kind] is None
                    and self._active_work_kind_count_locked() < self._max_active_work_kinds
                    and bool(self._waiting[work_kind])
                    and self._waiting[work_kind][0] == entry
                )
                self._waiting[work_kind].popleft()
                self._active[work_kind] = entry
            return KnowledgeRuntimeQueueReservation(self, entry)
        except BaseException:
            # 协程在等待期间取消时必须移除自身，避免后续任务永远等在一个无主队头后面。
            async with self._condition:
                try:
                    self._waiting[work_kind].remove(entry)
                except ValueError:
                    pass
                self._condition.notify_all()
            raise

    async def release(self, entry: _QueueEntry) -> None:
        """释放已经激活的 entry；错误释放不影响其它运行中的工作。"""

        async with self._condition:
            if self._active.get(entry.work_kind) == entry:
                self._active[entry.work_kind] = None
                self._condition.notify_all()

    async def snapshot(self) -> KnowledgeRuntimeQueueSnapshot:
        """返回当前进程队列事实；不会读取 SQLite 或客户资料。"""

        now = monotonic()
        async with self._condition:
            active_items: list[KnowledgeRuntimeQueueItem] = []
            waiting_items: list[KnowledgeRuntimeQueueItem] = []
            for work_kind in ("index", "deep_task"):
                active = self._active[work_kind]
                if active is not None:
                    active_items.append(
                        self._snapshot_item(active, state="active", position=1, now=now)
                    )
                position_offset = 1 if active is not None else 0
                waiting_items.extend(
                    self._snapshot_item(item, state="waiting", position=position_offset + index, now=now)
                    for index, item in enumerate(self._waiting[work_kind], start=1)
                )
        lane_message = (
            "当前低配策略会把索引和深度任务全局串行执行。"
            if self._max_active_work_kinds == 1
            else "索引与深度任务按各自通道 FIFO 串行执行。"
        )
        message = (
            "当前没有知识库重任务排队。"
            if not active_items and not waiting_items
            else f"{lane_message} 普通检索和问答不进入此队列。"
        )
        return KnowledgeRuntimeQueueSnapshot(
            active_items=active_items,
            waiting_items=waiting_items[:32],
            max_active_work_kinds=self._max_active_work_kinds,
            message=message,
        )

    def _contains_work_id_locked(self, entry: _QueueEntry) -> bool:
        """同类 ID 去重即可；不同种类不会使用同一稳定 ID。"""

        active = self._active[entry.work_kind]
        return (active is not None and active.work_id == entry.work_id) or any(
            item.work_id == entry.work_id for item in self._waiting[entry.work_kind]
        )

    def _active_work_kind_count_locked(self) -> int:
        """统计当前正在占用的通道数，不把同类队列长度误当成并发量。"""

        return sum(entry is not None for entry in self._active.values())

    @staticmethod
    def _snapshot_item(
        entry: _QueueEntry,
        *,
        state: str,
        position: int,
        now: float,
    ) -> KnowledgeRuntimeQueueItem:
        return KnowledgeRuntimeQueueItem(
            work_id=entry.work_id,
            work_kind=entry.work_kind,
            state=state,  # type: ignore[arg-type]
            queue_position=position,
            waited_ms=max(0, round((now - entry.queued_at) * 1_000)),
        )


knowledge_runtime_queue = KnowledgeRuntimeQueue()
