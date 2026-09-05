"""Lossless target-scoped wake-up batching, independent of periodic scans."""
from __future__ import annotations

import threading
import time
from typing import Iterable, Optional


class TargetWakeBatch:
    def __init__(self, merge_seconds: float = 1.0):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._targets: set[str] = set()
        self._full = False
        self._first_at: Optional[float] = None
        self.merge_seconds = merge_seconds

    def request(self, target_uids: Optional[Iterable[str]] = None) -> bool:
        targets = None if target_uids is None else {str(x).strip() for x in target_uids if str(x).strip()}
        if targets == set():
            return False
        with self._lock:
            if targets is None:
                self._full = True
            else:
                self._targets.update(targets)
            if self._first_at is None:
                self._first_at = time.monotonic()
            self.event.set()
        return True

    def remaining(self) -> float:
        with self._lock:
            return (max(0.0, self.merge_seconds - (time.monotonic() - self._first_at))
                    if self._first_at is not None else 0.0)

    def take(self, *, full_scan: bool = False) -> tuple[bool, Optional[set[str]]]:
        with self._lock:
            if not full_scan and self._first_at is None:
                return False, set()
            scope = None if full_scan or self._full else set(self._targets)
            self._targets.clear()
            self._full = False
            self._first_at = None
            self.event.clear()
            return True, scope
