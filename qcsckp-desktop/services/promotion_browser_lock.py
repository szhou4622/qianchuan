"""千川采集、追投、停投和日志同步共享的优先级浏览器锁。"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional, Tuple


PRIORITY_RETARGET = 10
PRIORITY_REGULATION = 20
PRIORITY_COLLECTION = 30
PRIORITY_OPERATION_LOG = 40
PRIORITY_BACKFILL = 50

_CONDITION = threading.Condition()
MAX_STARVATION_SECONDS = 120.0

_WAITERS: List[Tuple[int, int, object, float]] = []
_SEQUENCE = itertools.count()
_ACTIVE = False


def _next_waiter(now: Optional[float] = None) -> Optional[Tuple[int, int, object, float]]:
    if not _WAITERS:
        return None
    current = time.monotonic() if now is None else float(now)
    starved = [
        item
        for item in _WAITERS
        if current - float(item[3]) >= MAX_STARVATION_SECONDS
    ]
    if starved:
        return min(starved, key=lambda item: item[1])
    return min(_WAITERS, key=lambda item: (item[0], item[1]))


def _remove_waiter(ticket: Tuple[int, int, object, float]) -> None:
    try:
        _WAITERS.remove(ticket)
        heapq.heapify(_WAITERS)
    except ValueError:
        pass


def _acquire(
    priority: int,
    timeout_seconds: float,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    global _ACTIVE
    token = object()
    ticket = (int(priority), next(_SEQUENCE), token, time.monotonic())
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    with _CONDITION:
        heapq.heappush(_WAITERS, ticket)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _remove_waiter(ticket)
                _CONDITION.notify_all()
                return False
            next_waiter = _next_waiter()
            if not _ACTIVE and next_waiter and next_waiter[2] is token:
                _remove_waiter(ticket)
                _ACTIVE = True
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _remove_waiter(ticket)
                _CONDITION.notify_all()
                return False
            _CONDITION.wait(timeout=remaining)


def _release() -> None:
    global _ACTIVE
    with _CONDITION:
        _ACTIVE = False
        _CONDITION.notify_all()


@asynccontextmanager
async def exclusive_browser_operation(
    label: str,
    *,
    timeout_seconds: float = 900.0,
    priority: int = PRIORITY_COLLECTION,
) -> AsyncIterator[None]:
    """跨线程串行千川页面操作；等待任务按数值越小优先级越高。"""
    cancel_event = threading.Event()
    loop = asyncio.get_running_loop()
    acquire_future = loop.run_in_executor(
        None,
        _acquire,
        int(priority),
        max(0.1, float(timeout_seconds)),
        cancel_event,
    )
    try:
        acquired = await asyncio.shield(acquire_future)
    except asyncio.CancelledError:
        # asyncio取消不会停止执行器线程；显式撤销排队票据。若线程已在
        # 竞态中取得锁，则等待其返回后立即释放，绝不遗留“幽灵持锁者”。
        cancel_event.set()
        with _CONDITION:
            _CONDITION.notify_all()
        try:
            acquired_after_cancel = await asyncio.shield(acquire_future)
        except asyncio.CancelledError:
            async def _cleanup_orphan() -> None:
                try:
                    if await asyncio.shield(acquire_future):
                        _release()
                except Exception:
                    pass

            asyncio.create_task(_cleanup_orphan())
        else:
            if acquired_after_cancel:
                _release()
        raise
    if not acquired:
        raise TimeoutError(f"等待浏览器操作锁超时：{label}")
    try:
        yield
    finally:
        _release()


def browser_queue_snapshot() -> dict:
    with _CONDITION:
        next_waiter = _next_waiter()
        return {
            "active": bool(_ACTIVE),
            "waiting_count": len(_WAITERS),
            "waiting_priorities": [
                int(item[0])
                for item in sorted(_WAITERS, key=lambda item: (item[0], item[1]))
            ],
            "next_priority": (
                int(next_waiter[0]) if next_waiter is not None else None
            ),
        }
