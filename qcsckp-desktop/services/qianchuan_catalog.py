# -*- coding: utf-8 -*-
"""Read-only Qianchuan account/plan catalog state and completeness reporting."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from api.promotion_targets import list_promotion_targets
from services.qianchuan_accounts import (
    _owner_key,
    list_qianchuan_accounts,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


CATALOG_INTERVAL_MINUTES = 30
FOUR_CLASSES = (
    ("global", "live", "global_live"),
    ("global", "product", "global_product"),
    ("chengfang", "live", "chengfang_live"),
    ("chengfang", "product", "chengfang_product"),
)
_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {
    "owner_username": "",
    "running": False,
    "started_at": "",
    "finished_at": "",
    "message": "尚未同步完整账户计划目录",
    "status": "not_synced",
    "error": "",
    "failure_kind": "",
    "recovery_action": "",
    "next_due_at": "",
}


def _now() -> datetime:
    return datetime.now()


def _text(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def mark_catalog_sync_started(*, owner_username: Any = None) -> Dict[str, Any]:
    now = _now()
    owner = _owner_key(owner_username)
    with _LOCK:
        _STATE.update(
            {
                "owner_username": owner,
                "running": True,
                "started_at": _text(now),
                "finished_at": "",
                "message": "正在只读同步授权账户和计划目录",
                "status": "syncing",
                "error": "",
                "failure_kind": "",
                "recovery_action": "",
                "next_due_at": _text(now + timedelta(minutes=CATALOG_INTERVAL_MINUTES)),
                "processed_accounts": 0,
                "total_accounts": 0,
                "current_account": "",
            }
        )
        return dict(_STATE)


def mark_catalog_sync_progress(
    *,
    processed_accounts: int,
    total_accounts: int,
    current_account: Any = "",
    message: Any = "",
) -> Dict[str, Any]:
    with _LOCK:
        _STATE.update(
            {
                "running": True,
                "status": "syncing",
                "processed_accounts": max(0, int(processed_accounts)),
                "total_accounts": max(0, int(total_accounts)),
                "current_account": str(current_account or "")[:256],
                "message": str(message or "正在只读同步账户计划目录")[:1000],
            }
        )
        return dict(_STATE)


def _target_counts(targets: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {
        "total": len(targets),
        "verified": 0,
        "eligible": 0,
        "unverified": 0,
        "global_live": 0,
        "global_product": 0,
        "chengfang_live": 0,
        "chengfang_product": 0,
    }
    for target in targets:
        if str(target.get("verification_state") or "") == "verified":
            counts["verified"] += 1
        else:
            counts["unverified"] += 1
        if bool(target.get("monitor_eligible")):
            counts["eligible"] += 1
        for system, scene, key in FOUR_CLASSES:
            if (
                str(target.get("plan_system") or "") == system
                and str(target.get("promotion_scene") or "") == scene
            ):
                counts[key] += 1
                break
    return counts


def finalize_catalog_sync(
    *,
    owner_username: Any = None,
    complete_account_uids: Optional[List[str]] = None,
    account_results: Optional[Dict[str, Dict[str, Any]]] = None,
    error: Any = "",
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """Persist honest completeness. No category is declared complete without evidence."""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner = _owner_key(owner_username)
    accounts = list_qianchuan_accounts(owner_username=owner, db=store)
    targets = list_promotion_targets(owner_username=owner, db=store)
    complete = {str(item or "") for item in complete_account_uids or []}
    result_map = (
        {
            str(key): dict(value)
            for key, value in account_results.items()
            if isinstance(value, dict)
        }
        if isinstance(account_results, dict)
        else {}
    )
    complete.update(
        uid
        for uid, result in result_map.items()
        if bool(result.get("complete"))
    )
    now = _text(_now())
    for account in accounts:
        uid = str(account.get("account_uid") or "")
        scoped = [
            item for item in targets if str(item.get("account_uid") or "") == uid
        ]
        counts = _target_counts(scoped)
        account_result = result_map.get(uid) or {}
        account_error = str(account_result.get("error") or "")
        class_status = account_result.get("classes")
        if isinstance(class_status, dict):
            counts["class_status"] = class_status
        status = (
            "complete"
            if uid in complete and not error and not account_error
            else "partial"
        )
        detail = str(error or account_error or "")
        if status == "partial" and not detail:
            detail = (
                "尚未取得直播、商品、全域、乘方四类页面的全部分页完成证据；"
                "现有计划可查看，未核验计划禁止自动写入"
            )
        store.update(
            "qianchuan_account",
            {
                "catalog_status": status,
                "catalog_last_sync_at": now,
                "catalog_error": detail[:2000],
                "catalog_counts_json": json.dumps(
                    counts,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
            where={"account_uid": uid, "owner_username": owner},
        )
    state_status = (
        "complete"
        if accounts
        and all(str(item.get("account_uid") or "") in complete for item in accounts)
        and not error
        else "partial"
    )
    error_text = str(error or "").strip()
    login_required = bool(
        error_text
        and ("登录" in error_text or "login" in error_text.lower())
    )
    if error_text:
        state_status = "login_required" if login_required else "error"
    with _LOCK:
        _STATE.update(
            {
                "owner_username": owner,
                "running": False,
                "finished_at": now,
                "message": (
                    error_text
                    or (
                        "账户计划目录同步完成"
                        if state_status == "complete"
                        else "目录已更新，但部分分类或分页尚未取得完整证据"
                    )
                ),
                "status": state_status,
                "error": error_text,
                "failure_kind": (
                    "login_required"
                    if login_required
                    else ("sync_error" if error_text else "")
                ),
                "recovery_action": (
                    "open_visible_chrome" if login_required else ""
                ),
                "next_due_at": _text(
                    _now() + timedelta(minutes=CATALOG_INTERVAL_MINUTES)
                ),
                "processed_accounts": len(accounts),
                "total_accounts": len(accounts),
                "current_account": "",
            }
        )
        return catalog_sync_status(owner_username=owner, db=store)


def catalog_sync_status(
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner = _owner_key(owner_username)
    accounts = list_qianchuan_accounts(owner_username=owner, db=store)
    targets = list_promotion_targets(owner_username=owner, db=store)
    with _LOCK:
        state = dict(_STATE)
    if str(state.get("owner_username") or "") not in {"", owner}:
        state = {
            "owner_username": owner,
            "running": False,
            "started_at": "",
            "finished_at": "",
            "message": "尚未同步完整账户计划目录",
            "status": "not_synced",
            "error": "",
            "failure_kind": "",
            "recovery_action": "",
            "next_due_at": "",
        }
    account_states = [
        {
            "account_uid": item.get("account_uid"),
            "aavid": item.get("aavid"),
            "status": item.get("catalog_status") or "not_synced",
            "last_sync_at": item.get("catalog_last_sync_at") or "",
            "error": item.get("catalog_error") or "",
            "counts": item.get("catalog_counts") or {},
        }
        for item in accounts
    ]
    persisted_errors = [
        str(item.get("error") or "").strip()
        for item in account_states
        if str(item.get("error") or "").strip()
    ]
    if (
        not state.get("running")
        and not state.get("error")
        and any(
            "登录" in text or "login" in text.lower()
            for text in persisted_errors
        )
    ):
        state.update(
            {
                "status": "login_required",
                "message": persisted_errors[0],
                "error": persisted_errors[0],
                "failure_kind": "login_required",
                "recovery_action": "open_visible_chrome",
            }
        )
    state_error = str(state.get("error") or "")
    state.update(
        {
            "success": not bool(state_error),
            "account_count": len(accounts),
            "plan_count": len(targets),
            "accounts": account_states,
            "complete": bool(account_states)
            and all(item["status"] == "complete" for item in account_states),
            "interval_minutes": CATALOG_INTERVAL_MINUTES,
        }
    )
    return state


def catalog_sync_due(
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> bool:
    status = catalog_sync_status(owner_username=owner_username, db=db)
    if status.get("running"):
        return False
    latest = max(
        (
            str(item.get("last_sync_at") or "")
            for item in status.get("accounts") or []
        ),
        default="",
    )
    if not latest:
        return True
    try:
        return _now() - datetime.strptime(latest, "%Y-%m-%d %H:%M:%S") >= timedelta(
            minutes=CATALOG_INTERVAL_MINUTES
        )
    except ValueError:
        return True
