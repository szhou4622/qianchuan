"""Progress and commit ownership for the desktop's read-only collection work."""
from __future__ import annotations

import ctypes
import os
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime
from typing import Any, Mapping

from services.qianchuan_open_api.collection_context import CollectionContext, current_collection_context
from services.qianchuan_open_api.errors import CollectionCancelledError

_LOCK = threading.RLock()
_GENERATION = uuid.uuid4().hex
_HEARTBEAT = time.monotonic()
_STATUS = "stopped"
_CONTEXTS: dict[int, CollectionContext] = {}
_RESOURCE_CACHE: tuple[float, dict[str, Any]] = (0.0, {})


def generation() -> str:
    with _LOCK:
        return _GENERATION


def heartbeat(status: str = "running", **details: Any) -> None:
    global _HEARTBEAT, _STATUS
    with _LOCK:
        _HEARTBEAT = time.monotonic()
        _STATUS = str(status)


def register(context: CollectionContext) -> None:
    with _LOCK:
        _CONTEXTS[id(context)] = context


def release(context: CollectionContext) -> None:
    with _LOCK:
        _CONTEXTS.pop(id(context), None)


def active_contexts(epoch: str) -> tuple[CollectionContext, ...]:
    with _LOCK:
        return tuple(ctx for ctx in _CONTEXTS.values() if ctx.generation == epoch)


def revoke(expected_generation: str, reason: str) -> str:
    global _GENERATION
    with _LOCK:
        if _GENERATION != str(expected_generation):
            return _GENERATION
        old = list(_CONTEXTS.values())
        _GENERATION = uuid.uuid4().hex
    for context in old:
        context.cancel(reason)
    return generation()


def resource_pressure() -> dict[str, Any]:
    """Read the Windows commit limit; free physical RAM alone is insufficient."""
    global _RESOURCE_CACHE
    now = time.monotonic()
    if now - _RESOURCE_CACHE[0] < 5:
        return dict(_RESOURCE_CACHE[1])
    result: dict[str, Any] = {"available": False, "critical": False}
    if os.name == "nt":
        class PerformanceInfo(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in (
                "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
                "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize",
            )] + [(name, ctypes.c_ulong) for name in ("HandleCount", "ProcessCount", "ThreadCount")]
        info = PerformanceInfo()
        info.cb = ctypes.sizeof(info)
        try:
            if ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb) and info.CommitLimit:
                headroom = max(0, info.CommitLimit - info.CommitTotal) * info.PageSize
                ratio = info.CommitTotal / info.CommitLimit
                result = {"available": True, "critical": ratio >= .98 or headroom < 512 * 1024 * 1024,
                          "commit_percent": round(ratio * 100, 1), "commit_headroom_mb": round(headroom / 1024**2)}
        except (OSError, AttributeError):
            pass
    _RESOURCE_CACHE = (now, result)
    return dict(result)


def snapshot() -> dict[str, Any]:
    with _LOCK:
        result = {"generation": _GENERATION, "last_heartbeat_monotonic": _HEARTBEAT,
                  "status": _STATUS, "active_batches": len(_CONTEXTS), "stalled_after_seconds": 330}
    result["resource_pressure"] = resource_pressure()
    return result


def check_commit_ownership(store, target, connection) -> None:
    ctx = current_collection_context()
    if ctx is None:
        return
    ctx.check_active("before_commit")
    if ctx.generation and ctx.generation != generation():
        raise CollectionCancelledError("采集代次已撤销，旧数据未提交", code="client_generation_changed")
    expected = getattr(ctx, "target_identity", None)
    if not expected:
        return
    if (not isinstance(target, Mapping)
            or any(str(target.get(key) or "") != str(expected.get(key) or "")
                   for key in ("target_uid", "account_uid", "aadvid", "ad_id", "promotion_scene", "plan_system"))):
        raise CollectionCancelledError("提交目标与本轮采集范围不一致", code="client_scope_changed")
    row = store.select_one("promotion_target", where={"target_uid": expected["target_uid"]}, connection=connection) or {}
    account = store.select_one("qianchuan_account", where={"account_uid": row.get("account_uid")}, connection=connection) or {}
    if (not row.get("enabled") or not account.get("enabled")
            or str(account.get("owner_username") or "").casefold() != expected["owner_username"]
            or str(account.get("aavid") or "") != expected["aadvid"]
            or any(str(row.get(key) or "") != expected[key] for key in ("account_uid", "aadvid", "ad_id", "promotion_scene", "plan_system"))):
        raise CollectionCancelledError("采集期间账户、计划或归属已变化，旧数据未提交", code="client_scope_changed")
    claim = getattr(ctx, "job_claim", None)
    if claim:
        lease = store.select_one("collection_job", where={"id": claim["id"]}, connection=connection) or {}
        if (lease.get("status") != "leased" or lease.get("lease_owner") != claim.get("lease_owner")
                or str(lease.get("target_uid") or "") != expected["target_uid"]
                or str(lease.get("owner_username") or "").casefold() != expected["owner_username"]
                or str(lease.get("account_uid") or "") != expected["account_uid"]
                or int(lease.get("fencing_token") or 0) != int(claim.get("fencing_token") or 0)
                or str(lease.get("lease_expires_at") or "") <= datetime.now().strftime("%Y-%m-%d %H:%M:%S")):
            raise CollectionCancelledError("采集领取资格已失效，旧数据未提交", code="client_lease_changed")


@contextmanager
def owned_transaction(store, target):
    ctx = current_collection_context()
    identity = getattr(ctx, "authorization_identity", None) if ctx else None
    if ctx:
        ctx.check_active("before_write_lock")
    if identity:
        from services.qianchuan_open_api.token_provider import authorization_identity_guard
        auth_guard = authorization_identity_guard(identity)
    else:
        auth_guard = nullcontext()
    with auth_guard:
        with store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            check_commit_ownership(store, target, connection)
            yield connection
            # Lock order is auth -> DB -> lifecycle -> context. revoke never
            # waits for a DB lock while holding _LOCK, and both cancellation
            # events use ctx._lock. The check and the actual commit must share
            # this gate; another check before an unlocked commit is not enough.
            with _LOCK:
                with ctx._lock if ctx is not None else nullcontext():
                    check_commit_ownership(store, target, connection)
                    if ctx:
                        ctx.check_active("commit_complete")
                    connection.commit()
            # SQLiteStore.transaction's following commit has no pending writes.
