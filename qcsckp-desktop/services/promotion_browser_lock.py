"""采集、追投和停投共享的浏览器写操作互斥锁。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator


_LOCK = threading.Lock()


@asynccontextmanager
async def exclusive_browser_operation(
    label: str,
    *,
    timeout_seconds: float = 900.0,
) -> AsyncIterator[None]:
    """跨后台线程串行化千川页面操作，防止轮询和调控同时切换页面。"""
    acquired = await asyncio.to_thread(
        _LOCK.acquire,
        True,
        max(0.1, float(timeout_seconds)),
    )
    if not acquired:
        raise TimeoutError(f"等待浏览器操作锁超时：{label}")
    try:
        yield
    finally:
        _LOCK.release()

