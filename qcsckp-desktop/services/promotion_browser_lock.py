"""千川采集、追投、停投和日志同步共享的优先级浏览器锁。"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Tuple


PRIORITY_RETARGET = 10
PRIORITY_REGULATION = 20
PRIORITY_COLLECTION = 30
PRIORITY_OPERATION_LOG = 40
PRIORITY_BACKFILL = 50

_CONDITION = threading.Condition()
_WAITERS: List[Tuple[int, int, object]] = []
_SEQUENCE = itertools.count()
_ACTIVE = False


def _acquire(priority: int, timeout_seconds: float) -> bool:
    global _ACTIVE
    token = object()
    ticket = (int(priority), next(_SEQUENCE), token)
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    with _CONDITION:
        heapq.heappush(_WAITERS, ticket)
        while True:
            if not _ACTIVE and _WAITERS and _WAITERS[0][2] is token:
                heapq.heappop(_WAITERS)
                _ACTIVE = True
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    _WAITERS.remove(ticket)
                    heapq.heapify(_WAITERS)
                except ValueError:
                    pass
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
    acquired = await asyncio.to_thread(
        _acquire,
        int(priority),
        max(0.1, float(timeout_seconds)),
    )
    if not acquired:
        raise TimeoutError(f"等待浏览器操作锁超时：{label}")
    try:
        yield
    finally:
        _release()


def browser_queue_snapshot() -> dict:
    with _CONDITION:
        return {
            "active": bool(_ACTIVE),
            "waiting_count": len(_WAITERS),
            "waiting_priorities": [int(item[0]) for item in sorted(_WAITERS)],
        }
