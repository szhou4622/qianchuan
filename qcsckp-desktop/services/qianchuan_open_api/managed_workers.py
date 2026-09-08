"""Bounded raw workers with explicit ready/done handshakes.

Unlike Thread.start(), acknowledgement has a deadline. A native bootstrap that
never acknowledges keeps its capacity slot quarantined, so repeated recovery
cannot grow the number of potentially alive threads without bound.
"""
from __future__ import annotations

import _thread
import contextvars
import threading
import time
from typing import Any, Callable, Optional

from .collection_context import CollectionContext, current_collection_context, use_collection_context, managed_cancellation_gate
from .errors import CollectionCancelledError, ManagedWorkerUnavailable


class ManagedTask:
    def __init__(self, context: CollectionContext):
        self.context = context
        self.name = "managed-worker"
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.cancelled = threading.Event()
        self._value: Any = None
        self._error: Optional[BaseException] = None
        self._callback_lock = threading.Lock()
        self._callbacks: list[Callable] = []

    def cancel(self) -> bool:
        # A managed cancellation is an additional gate, not ctx._cancel; both
        # use the context's shared commit lock so neither can cross a commit.
        with self.context._lock:
            self.cancelled.set()
            self._value = None
        return self.finished.is_set()

    def done(self) -> bool:
        return self.finished.is_set()

    def is_alive(self) -> bool:
        return self.ready.is_set() and not self.finished.is_set() and not self.cancelled.is_set()

    @property
    def is_running(self) -> bool:
        return self.ready.is_set() and not self.finished.is_set()

    def join(self, timeout: Optional[float] = None) -> None:
        remaining = self.context.remaining_seconds()
        self.finished.wait((None if remaining == float("inf") else remaining) if timeout is None else max(0.0, timeout))

    def add_done_callback(self, callback: Callable) -> None:
        with self._callback_lock:
            immediate = self.finished.is_set()
            if not immediate:
                self._callbacks.append(callback)
        if immediate:
            callback(self)

    def _finish(self) -> None:
        if self.cancelled.is_set():
            self._value = None
        with self._callback_lock:
            self.finished.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback(self)
            except Exception:
                pass

    def result(self, timeout: Optional[float] = None):
        until = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.finished.is_set():
            self.context.check_active("managed_worker_wait")
            if self.cancelled.is_set():
                raise CollectionCancelledError("后台任务已作废", code="client_cancelled")
            if until is not None and time.monotonic() >= until:
                raise TimeoutError("后台任务尚未完成")
            self.finished.wait(min(0.05, self.context.remaining_seconds()))
        self.context.check_active("managed_worker_result")
        if self.cancelled.is_set():
            raise CollectionCancelledError("后台迟到结果已丢弃", code="client_cancelled")
        if self._error is not None:
            raise self._error
        return self._value


class ManagedWorkers:
    def __init__(self, capacity: int, *, startup_timeout: float = 2.0, capacity_timeout: float = 30.0,
                 starter: Optional[Callable] = None):
        self.capacity = max(1, int(capacity))
        self.startup_timeout = max(0.01, float(startup_timeout))
        self.capacity_timeout = max(0.01, float(capacity_timeout))
        self._slots = threading.BoundedSemaphore(self.capacity)
        self._starter = starter or _thread.start_new_thread
        self._lock = threading.Lock()
        self._tasks: set[ManagedTask] = set()

    def submit(self, fn: Callable[[], Any], *, context: Optional[CollectionContext] = None) -> ManagedTask:
        ctx = context or current_collection_context() or CollectionContext()
        ctx.check_active("before_worker_capacity")
        capacity_deadline = min(ctx.deadline, time.monotonic() + self.capacity_timeout)
        while not self._slots.acquire(timeout=min(0.05, max(0.001, ctx.remaining_seconds()))):
            ctx.check_active("worker_capacity")
            if time.monotonic() >= capacity_deadline:
                raise ManagedWorkerUnavailable("后台容量仍被占用，拒绝无限创建新线程", code="client_worker_capacity")
        try:
            task = ManagedTask(ctx)
            task.name = str(getattr(fn, "__name__", "managed-worker"))
            copied = contextvars.copy_context()
            with self._lock:
                self._tasks.add(task)
        except BaseException:
            # The native start function has not been called yet: this slot is
            # unambiguously unused and can be released even after MemoryError.
            self._slots.release()
            raise

        def body():
            try:
                task.ready.set()
                if task.cancelled.is_set():
                    return
                ctx.check_active("worker_start")
                with use_collection_context(ctx), managed_cancellation_gate(task.cancelled):
                    value = fn()
                ctx.check_active("worker_complete")
                if not task.cancelled.is_set():
                    task._value = value
            except BaseException as exc:
                task._error = exc
            finally:
                with self._lock:
                    self._tasks.discard(task)
                self._slots.release()
                task._finish()

        try:
            self._starter(lambda: copied.run(body), ())
        except BaseException as exc:
            # Conservatively quarantine until body/finally proves exit. A
            # native start can fail while returning its thread identifier after
            # the OS thread was created; releasing here could double-release or
            # permit unbounded late workers under memory pressure.
            task.cancel()
            raise ManagedWorkerUnavailable("原生后台线程启动失败，已隔离未确认容量", code="client_worker_start") from exc
        until = min(ctx.deadline, time.monotonic() + self.startup_timeout)
        while not task.ready.is_set() and not task.finished.is_set():
            try:
                ctx.check_active("worker_ready")
            except BaseException:
                task.cancel()
                raise
            if time.monotonic() >= until:
                task.cancel()
                raise ManagedWorkerUnavailable("后台线程启动未确认，已隔离该容量且不再等待", code="client_worker_start")
            task.ready.wait(min(0.02, max(0.001, until - time.monotonic())))
        return task

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            tasks = tuple(self._tasks)
        return {"capacity": self.capacity, "occupied": len(tasks),
                "unacknowledged": sum(not task.ready.is_set() for task in tasks)}


PAGINATION_WORKERS = ManagedWorkers(18)
IO_WORKERS = ManagedWorkers(12)
TOKEN_WORKERS = ManagedWorkers(8)


def run_bounded(fn: Callable[[], Any], *, context: CollectionContext, lane: str = "io"):
    workers = TOKEN_WORKERS if lane == "token" else IO_WORKERS
    task = workers.submit(fn, context=context)
    try:
        return task.result()
    except BaseException:
        task.cancel()
        raise
