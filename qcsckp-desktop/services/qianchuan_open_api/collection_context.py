"""One bounded collection generation, shared across retries and pagination."""
from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Mapping, Optional

from .errors import CollectionCancelledError, CollectionDeadlineExceeded, PageAttemptBudgetExceeded

_CURRENT: contextvars.ContextVar[Optional["CollectionContext"]] = contextvars.ContextVar("qcsckp_collection_context", default=None)
_CANCEL_GATES: contextvars.ContextVar[tuple[threading.Event, ...]] = contextvars.ContextVar("qcsckp_managed_cancel_gates", default=())


def request_fingerprint(endpoint: str, query: Any, *, advertiser_id: Any = "") -> str:
    encoded = json.dumps([str(endpoint), str(advertiser_id), query or {}],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CollectionContext:
    def __init__(self, timeout_seconds: Optional[float] = 300, *, generation: str = "",
                 is_current: Optional[Callable[[], bool]] = None,
                 progress_callback: Optional[Callable[[dict[str, Any]], None]] = None):
        self.deadline = float("inf") if timeout_seconds is None else time.monotonic() + max(0.0, float(timeout_seconds))
        self.generation = str(generation)
        self.is_current = is_current
        self.progress_callback = progress_callback
        self.max_request_attempts = 4
        self._cancel = threading.Event()
        self._reason = "采集已取消"
        self._parent: Optional[CollectionContext] = None
        # Shared by child contexts, cancellation, page budgets and the final
        # snapshot commit gate. RLock permits final checks on the same context.
        self._lock = threading.RLock()
        self._page_attempts: dict[str, int] = {}
        self._rescans: set[str] = set()
        self._scope_slots: dict[str, threading.BoundedSemaphore] = {}

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check_active(self, phase: str = "") -> None:
        if any(gate.is_set() for gate in _CANCEL_GATES.get()):
            raise CollectionCancelledError("受管任务已取消，迟到操作作废", code="client_cancelled", pagination={"phase": phase})
        if self._parent is not None:
            self._parent.check_active(phase)
        if self._cancel.is_set() or (self.is_current is not None and not self.is_current()):
            raise CollectionCancelledError(self._reason, code="client_cancelled", pagination={"phase": phase})
        if self.remaining_seconds() <= 0:
            raise CollectionDeadlineExceeded("本轮采集已超过截止时间，迟到结果不入库", code="client_deadline",
                                             pagination={"phase": phase})

    def cancel(self, reason: str = "采集已取消") -> None:
        with self._lock:
            self._reason = str(reason or "采集已取消")
            self._cancel.set()

    def wait(self, seconds: float, phase: str = "backoff") -> None:
        until = time.monotonic() + max(0.0, float(seconds))
        while True:
            self.check_active(phase)
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            self._cancel.wait(min(0.1, remaining, self.remaining_seconds()))

    def child(self) -> "CollectionContext":
        child = CollectionContext(0, generation=self.generation, progress_callback=self.progress_callback)
        child.deadline = self.deadline
        child._parent = self
        child._lock = self._lock
        child._page_attempts = self._page_attempts
        child._rescans = self._rescans
        child._scope_slots = self._scope_slots
        child.max_request_attempts = self.max_request_attempts
        # A child may reach a commit helper from a bounded worker. Losing
        # these frozen capabilities would silently skip auth/scope/lease gates.
        for name in ("authorization_identity", "target_identity", "job_claim", "database"):
            if hasattr(self, name):
                value = getattr(self, name)
                setattr(child, name, dict(value) if isinstance(value, Mapping) else value)
        return child

    def reserve_page_attempt(self, key: str, maximum: int = 4) -> int:
        self.check_active("before_http_attempt")
        with self._lock:
            used = self._page_attempts.get(key, 0)
            if used >= min(4, max(1, int(maximum))):
                raise PageAttemptBudgetExceeded("该逻辑页已耗尽最多4次HTTP发送预算", code="client_page_budget")
            self._page_attempts[key] = used + 1
            return used + 1

    def page_attempts(self, key: str) -> int:
        with self._lock:
            return self._page_attempts.get(key, 0)

    @contextmanager
    def io_slot(self, scope_key: str):
        if not scope_key:
            yield
            return
        with self._lock:
            gate = self._scope_slots.setdefault(scope_key, threading.BoundedSemaphore(3))
        while not gate.acquire(timeout=0.05):
            self.check_active("scope_io_capacity")
        try:
            self.check_active("scope_io_ready")
            yield
        finally:
            gate.release()

    def reserve_rescan(self, scope_key: str) -> bool:
        self.check_active("before_full_rescan")
        with self._lock:
            if self._rescans:
                return False
            self._rescans.add(scope_key)
            return True

    def progress(self, phase: str, **metadata: Any) -> None:
        self.check_active(phase)
        if self.progress_callback is not None:
            try:
                self.progress_callback({"phase": phase, "generation": self.generation, **metadata})
            except Exception:
                # A diagnostic sink must not turn a completed read into a retry.
                pass


def current_collection_context() -> Optional[CollectionContext]:
    return _CURRENT.get()


@contextmanager
def managed_cancellation_gate(event: threading.Event):
    token = _CANCEL_GATES.set((*_CANCEL_GATES.get(), event))
    try:
        yield
    finally:
        _CANCEL_GATES.reset(token)


@contextmanager
def use_collection_context(context: Optional[CollectionContext]):
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)
