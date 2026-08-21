# -*- coding: utf-8 -*-
"""单一千川登录身份下的多账户目录、路由和监控容量管理。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import DATA_DIR, DB_FILE
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


DEFAULT_OWNER = "local_default"
# A target marked as actively monitored must fit the same five-minute SLA as
# the hot collector.  Overflow targets remain saved but are explicitly placed
# in capacity_waiting instead of silently delivering 6-9 minute-old metrics.
CAPACITY_WINDOW_SECONDS = 5 * 60
CAPACITY_STALE_SECONDS = 10 * 60
CAPACITY_PARALLEL_WORKERS = 6
CAPACITY_ACCOUNT_PARALLEL_WORKERS = 2
# New targets must be admitted optimistically so their real API duration can
# be measured.  A pessimistic 45-second seed caused capacity_waiting targets
# to starve forever without ever receiving a first collection.
DEFAULT_TARGET_DURATION_MS = 5_000
MIN_TARGET_DURATION_MS = 5_000
MAX_TARGET_DURATION_MS = 30 * 60_000
CAPACITY_PROBE_TARGETS_PER_CYCLE = 2
DAILY_CONFIG_FILE = os.path.join(DATA_DIR, "operation_daily_report.json")
_ACCOUNT_DIRECTORY_LOCK = threading.RLock()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _owner_key(value: Any = None) -> str:
    text = str(value or "").strip().casefold()
    if text:
        return text
    try:
        from services.qianchuan_session import current_session_owner

        text = str(current_session_owner() or "").strip().casefold()
    except Exception:
        text = ""
    return text or DEFAULT_OWNER


def make_account_uid(aavid: Any, owner_username: Any = None) -> str:
    aid = str(aavid or "").strip()
    if not aid.isdigit():
        raise ValueError("千川账户ID必须为数字")
    owner = _owner_key(owner_username)
    digest = hashlib.sha256(f"{owner}:{aid}".encode("utf-8")).hexdigest()[:24]
    return f"account_{digest}"


def _json_list(value: Any) -> List[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = [item.strip() for item in value.split(",")]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: List[str] = []
    for one in value:
        item = str(one or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _daily_selected_aavids(owner_username: str) -> set[str]:
    try:
        with open(DAILY_CONFIG_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        profile = ((raw or {}).get("profiles") or {}).get(_owner_key(owner_username)) or {}
        return {
            str(item).strip()
            for item in profile.get("aavids") or []
            if str(item or "").strip()
        }
    except Exception:
        return set()


def _sync_daily_selected_aavids(
    owner_username: Any,
    *,
    db: SQLiteStore,
) -> None:
    """让旧日报配置与账户目录保持一致，兼容仍读取 aavids 的 rc23 页面。"""
    if os.path.abspath(str(db.config.get("database") or "")) != os.path.abspath(DB_FILE):
        return
    owner = _owner_key(owner_username)
    rows = db.select(
        "qianchuan_account",
        fields="aavid",
        where={
            "owner_username": owner,
            "directory_selected": 1,
            "enabled": 1,
            "report_enabled": 1,
        },
        order_by="aavid ASC",
    )
    try:
        with open(DAILY_CONFIG_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        payload = {"version": 1, "profiles": {}}
    if not isinstance(payload, dict):
        payload = {"version": 1, "profiles": {}}
    profiles = payload.setdefault("profiles", {})
    profile = profiles.get(owner)
    profile = dict(profile) if isinstance(profile, dict) else {}
    selected_aavids = [
        str(item.get("aavid") or "")
        for item in rows
        if str(item.get("aavid") or "")
    ]
    profile["aavids"] = selected_aavids
    # “纳入 09:00 日报”本身就是用户对自动日报的明确授权。账户页保存后
    # 应立即生效，不应再要求用户转到飞书页重复开启一个全局开关。
    # 当最后一个账户取消勾选时同步关闭，避免发送用户已经取消的日报。
    profile["enabled"] = bool(selected_aavids)
    profile.setdefault("send_time", "09:00")
    profile.setdefault("send_empty", False)
    profile["updated_at"] = _now_text()
    profiles[owner] = profile
    os.makedirs(os.path.dirname(DAILY_CONFIG_FILE), exist_ok=True)
    temp = DAILY_CONFIG_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, DAILY_CONFIG_FILE)


def ensure_qianchuan_account(
    aavid: Any,
    *,
    account_name: Any = "",
    owner_username: Any = None,
    directory_selected: Optional[bool] = True,
    enabled: Optional[bool] = None,
    report_enabled: Optional[bool] = None,
    seen: bool = False,
    allow_reactivate_removed: bool = False,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    with _ACCOUNT_DIRECTORY_LOCK:
        return _ensure_qianchuan_account_unlocked(
            aavid,
            account_name=account_name,
            owner_username=owner_username,
            directory_selected=directory_selected,
            enabled=enabled,
            report_enabled=report_enabled,
            seen=seen,
            allow_reactivate_removed=allow_reactivate_removed,
            db=db,
        )


def _ensure_qianchuan_account_unlocked(
    aavid: Any,
    *,
    account_name: Any = "",
    owner_username: Any = None,
    directory_selected: Optional[bool] = True,
    enabled: Optional[bool] = None,
    report_enabled: Optional[bool] = None,
    seen: bool = False,
    allow_reactivate_removed: bool = False,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    _initialize_directory_selection(store)
    owner = _owner_key(owner_username)
    aid = str(aavid or "").strip()
    uid = make_account_uid(aid, owner)
    existing = store.select_one(
        "qianchuan_account",
        where={"owner_username": owner, "aavid": aid},
    )
    removed_tombstone = bool(existing) and (
        str((existing or {}).get("last_status") or "").strip().lower()
        == "removed"
        and not bool((existing or {}).get("directory_selected"))
    )
    reactivating = bool(allow_reactivate_removed) and bool(directory_selected)
    values: Dict[str, Any] = {
        "account_uid": uid,
        "owner_username": owner,
        "aavid": aid,
        "account_name": str(
            account_name
            or (existing or {}).get("account_name")
            or f"千川账户 {aid}"
        ).strip()[:256],
        "directory_selected": (
            1
            if (
                bool((existing or {}).get("directory_selected"))
                or (
                    bool(directory_selected)
                    and (not removed_tombstone or reactivating)
                )
            )
            else 0
        ),
        "enabled": (
            1
            if (
                enabled
                if enabled is not None
                else bool((existing or {}).get("enabled", 0))
            )
            else 0
        ),
        "report_enabled": (
            1
            if (
                report_enabled
                if report_enabled is not None
                else bool((existing or {}).get("report_enabled", 0))
            )
            else 0
        ),
        "route_mode": str((existing or {}).get("route_mode") or "default"),
        "route_send_personal": int(
            bool((existing or {}).get("route_send_personal", 1))
        ),
        "route_group_ids_json": str(
            (existing or {}).get("route_group_ids_json") or "[]"
        ),
        "last_status": str((existing or {}).get("last_status") or "pending"),
        "last_error": str((existing or {}).get("last_error") or ""),
    }
    if removed_tombstone and not reactivating:
        values["directory_selected"] = 0
        values["enabled"] = 0
        values["report_enabled"] = 0
        values["last_status"] = "removed"
        values["last_error"] = ""
    if seen and (not removed_tombstone or reactivating):
        values["last_seen_at"] = _now_text()
        values["last_status"] = "available"
        values["last_error"] = ""
    store.insert_or_update(
        "qianchuan_account",
        values,
        unique_fields=["owner_username", "aavid"],
    )
    saved = store.select_one("qianchuan_account", where={"account_uid": uid})
    assert saved is not None
    return _account_row(saved, store)


def _account_row(row: Dict[str, Any], store: SQLiteStore) -> Dict[str, Any]:
    out = dict(row)
    out["directory_selected"] = bool(out.get("directory_selected"))
    out["enabled"] = bool(out.get("enabled"))
    out["report_enabled"] = bool(out.get("report_enabled"))
    out["route_send_personal"] = bool(out.get("route_send_personal"))
    out["route_group_ids"] = _json_list(out.pop("route_group_ids_json", "[]"))
    try:
        out["catalog_counts"] = json.loads(
            str(out.pop("catalog_counts_json", "{}") or "{}")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        out["catalog_counts"] = {}
    aid = str(out.get("aavid") or "")
    counts = store.execute(
        "SELECT SUM(CASE WHEN verification_state!='missing' THEN 1 ELSE 0 END) AS total,"
        "SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_count,"
        "SUM(CASE WHEN enabled=1 AND capacity_state='active' THEN 1 ELSE 0 END) AS active_count,"
        "SUM(CASE WHEN enabled=1 AND capacity_state='capacity_waiting' THEN 1 ELSE 0 END) AS waiting_count,"
        "SUM(CASE WHEN monitor_eligible=1 THEN 1 ELSE 0 END) AS eligible_count,"
        "SUM(CASE WHEN verification_state!='missing' AND promotion_scene='live' AND plan_system='global' THEN 1 ELSE 0 END) AS global_live_count,"
        "SUM(CASE WHEN verification_state!='missing' AND promotion_scene='product' AND plan_system='global' THEN 1 ELSE 0 END) AS global_product_count,"
        "SUM(CASE WHEN verification_state!='missing' AND promotion_scene='live' AND plan_system='chengfang' THEN 1 ELSE 0 END) AS chengfang_live_count,"
        "SUM(CASE WHEN verification_state!='missing' AND promotion_scene='product' AND plan_system='chengfang' THEN 1 ELSE 0 END) AS chengfang_product_count,"
        "SUM(CASE WHEN verification_state!='missing' AND (plan_system='unknown' OR verification_state!='verified') THEN 1 ELSE 0 END) AS unverified_count,"
        "SUM(CASE WHEN verification_state='missing' THEN 1 ELSE 0 END) AS historical_count "
        "FROM promotion_target WHERE account_uid=?",
        (str(out.get("account_uid") or ""),),
        fetch=True,
    ) or [{}]
    out.update(
        {
            "plan_count": int(counts[0].get("total") or 0),
            "enabled_plan_count": int(counts[0].get("enabled_count") or 0),
            "active_plan_count": int(counts[0].get("active_count") or 0),
            "waiting_plan_count": int(counts[0].get("waiting_count") or 0),
            "eligible_plan_count": int(counts[0].get("eligible_count") or 0),
            "four_class_counts": {
                "global_live": int(counts[0].get("global_live_count") or 0),
                "global_product": int(counts[0].get("global_product_count") or 0),
                "chengfang_live": int(counts[0].get("chengfang_live_count") or 0),
                "chengfang_product": int(counts[0].get("chengfang_product_count") or 0),
            },
            "unverified_plan_count": int(counts[0].get("unverified_count") or 0),
            "historical_plan_count": int(counts[0].get("historical_count") or 0),
        }
    )
    sync = store.select_one(
        "platform_log_sync_state",
        where={
            "account_uid": str(out.get("account_uid") or ""),
            "aavid": aid,
        },
    ) or {}
    out["log_sync"] = {
        "last_sync_at": str(sync.get("last_sync_at") or ""),
        "last_status": str(sync.get("last_status") or "not_configured"),
        "last_error": str(sync.get("last_error") or ""),
        "coverage_from": str(sync.get("coverage_from") or ""),
        "coverage_to": str(sync.get("coverage_to") or ""),
    }
    return out


def list_qianchuan_accounts(
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
    ensure_schema: bool = True,
    perform_repairs: bool = True,
) -> List[Dict[str, Any]]:
    store = db or SQLiteStore()
    if ensure_schema:
        init_sqlite_schema(database=store.config.get("database"))
    if perform_repairs:
        _initialize_directory_selection(store)
    owner = _owner_key(owner_username)
    rows = store.select(
        "qianchuan_account",
        where={"owner_username": owner, "directory_selected": 1},
        order_by="enabled DESC, account_name ASC, aavid ASC",
    )
    return [_account_row(row, store) for row in rows]


def get_qianchuan_account(
    value: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Optional[Dict[str, Any]]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    _initialize_directory_selection(store)
    owner = _owner_key(owner_username)
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("account_"):
        row = store.select_one(
            "qianchuan_account",
            where={"account_uid": text, "owner_username": owner},
        )
    else:
        row = store.select_one(
            "qianchuan_account",
            where={"aavid": text, "owner_username": owner},
        )
    return _account_row(row, store) if row else None


def _initialize_directory_selection(store: SQLiteStore) -> None:
    """把 rc28.2 以前真正使用过的账户保留为已选择，其余自动发现账户隐藏。"""
    store.execute(
        "UPDATE qianchuan_account SET directory_selected=1 "
        "WHERE directory_selected IS NULL AND ("
        "enabled=1 OR report_enabled=1 OR EXISTS("
        "SELECT 1 FROM promotion_target t "
        "WHERE t.account_uid=qianchuan_account.account_uid"
        "))"
    )
    store.execute(
        "UPDATE qianchuan_account SET directory_selected=0 "
        "WHERE directory_selected IS NULL"
    )
    # Repair rows resurrected by an older in-flight catalog scan. A removed
    # account is a tombstone until the user explicitly selects it again.
    store.execute(
        "UPDATE qianchuan_account SET directory_selected=0, enabled=0, "
        "report_enabled=0 WHERE lower(COALESCE(last_status,''))='removed'"
    )
    _repair_conflicting_catalog_classes(store)
    _repair_suspicious_empty_catalog_targets(store)


def _repair_conflicting_catalog_classes(store: SQLiteStore) -> None:
    """Repair rows written by the old cross-class replay bug.

    Some Qianchuan page versions ignore an unsupported dataset field and
    return the previous catalog class with HTTP 200.  Older builds trusted the
    requested class name, so a later Chengfang scan could overwrite a verified
    Global plan.  The persisted per-class ``seen_ids`` let us detect this
    narrowly: only the same ad id claimed by multiple classes is repaired.
    """
    from api.promotion_targets import target_eligibility

    class_order = (
        ("global_product", "global", "product"),
        ("global_live", "global", "live"),
        ("chengfang_product", "chengfang", "product"),
        ("chengfang_live", "chengfang", "live"),
    )
    accounts = store.select(
        "qianchuan_account",
        fields=(
            "account_uid,catalog_status,catalog_error,catalog_counts_json"
        ),
        where={"directory_selected": 1},
    )
    for account in accounts:
        try:
            counts = json.loads(str(account.get("catalog_counts_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        states = counts.get("class_status") if isinstance(counts, dict) else None
        if not isinstance(states, dict):
            continue
        first_claim: Dict[str, tuple[str, str]] = {}
        losing_classes = set()
        collisions = set()
        for class_key, system, scene in class_order:
            state = states.get(class_key)
            if not isinstance(state, dict) or not bool(state.get("complete")):
                continue
            for value in state.get("seen_ids") or []:
                ad_id = str(value or "").strip()
                if not ad_id.isdigit():
                    continue
                claimed = first_claim.get(ad_id)
                if claimed is None:
                    first_claim[ad_id] = (system, scene)
                elif claimed != (system, scene):
                    collisions.add(ad_id)
                    losing_classes.add((system, scene))
        if not collisions:
            continue

        account_uid = str(account.get("account_uid") or "")
        for ad_id in collisions:
            target = store.select_one(
                "promotion_target",
                where={
                    "account_uid": account_uid,
                    "ad_id": ad_id,
                },
            )
            if not target or not str(target.get("last_verified_at") or "").strip():
                continue
            system, scene = first_claim[ad_id]
            try:
                capability = json.loads(str(target.get("capability_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                capability = {}
            eligibility = target_eligibility(
                promotion_scene=scene,
                plan_system=system,
                platform_status=target.get("platform_status"),
                verification_state="verified",
                capability=capability if isinstance(capability, dict) else {},
            )
            store.update(
                "promotion_target",
                {
                    "plan_system": system,
                    "promotion_scene": scene,
                    "verification_state": "verified",
                    "monitor_eligible": 1 if eligibility["monitor_eligible"] else 0,
                    "retarget_eligible": 1 if eligibility["retarget_eligible"] else 0,
                    "stop_eligible": 1 if eligibility["stop_eligible"] else 0,
                    "ineligible_reason": str(
                        eligibility.get("ineligible_reason") or ""
                    )[:1000],
                },
                where={"target_uid": target["target_uid"]},
            )

        # A losing class did not provide trustworthy disappearance evidence.
        # Restore only rows that still carry an earlier successful exact-detail
        # verification and no verification error.
        for system, scene in losing_classes:
            targets = store.select(
                "promotion_target",
                where={
                    "account_uid": account_uid,
                    "plan_system": system,
                    "promotion_scene": scene,
                    "verification_state": "missing",
                },
            )
            for target in targets:
                if (
                    not str(target.get("last_verified_at") or "").strip()
                    or str(target.get("last_verification_error") or "").strip()
                ):
                    continue
                try:
                    capability = json.loads(
                        str(target.get("capability_json") or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    capability = {}
                eligibility = target_eligibility(
                    promotion_scene=scene,
                    plan_system=system,
                    platform_status=target.get("platform_status"),
                    verification_state="verified",
                    capability=capability if isinstance(capability, dict) else {},
                )
                store.update(
                    "promotion_target",
                    {
                        "verification_state": "verified",
                        "monitor_eligible": (
                            1 if eligibility["monitor_eligible"] else 0
                        ),
                        "retarget_eligible": (
                            1 if eligibility["retarget_eligible"] else 0
                        ),
                        "stop_eligible": 1 if eligibility["stop_eligible"] else 0,
                        "ineligible_reason": str(
                            eligibility.get("ineligible_reason") or ""
                        )[:1000],
                    },
                    where={"target_uid": target["target_uid"]},
                )

        old_error = str(account.get("catalog_error") or "").strip()
        collision_error = "目录接口返回跨分类重复计划，已保留先取得的可信分类"
        store.update(
            "qianchuan_account",
            {
                "catalog_status": "partial",
                "catalog_error": (
                    old_error
                    if collision_error in old_error
                    else "；".join(value for value in (old_error, collision_error) if value)[
                        :2000
                    ]
                ),
            },
            where={"account_uid": account_uid},
        )


def _repair_suspicious_empty_catalog_targets(store: SQLiteStore) -> None:
    """Restore plans hidden by the old cross-account empty-catalog bug.

    The repair is deliberately narrow: the account sync must be partial, the
    stored class result must claim a complete zero-row response, and the plan
    must still carry earlier successful detail-verification evidence.  The
    account remains partial, so this repair restores visibility without
    allowing stale catalog data to produce candidates.
    """
    from api.promotion_targets import target_eligibility

    accounts = store.select(
        "qianchuan_account",
        fields=(
            "account_uid,catalog_status,catalog_counts_json"
        ),
        where={"directory_selected": 1, "catalog_status": "partial"},
    )
    class_map = {
        "global_live": ("global", "live"),
        "global_product": ("global", "product"),
        "chengfang_live": ("chengfang", "live"),
        "chengfang_product": ("chengfang", "product"),
    }
    for account in accounts:
        try:
            counts = json.loads(str(account.get("catalog_counts_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        states = counts.get("class_status") if isinstance(counts, dict) else None
        if not isinstance(states, dict):
            continue
        for class_key, (system, scene) in class_map.items():
            state = states.get(class_key)
            if not isinstance(state, dict) or not bool(state.get("complete")):
                continue
            try:
                seen = int(state.get("seen") or 0)
            except (TypeError, ValueError):
                continue
            if seen != 0:
                continue
            targets = store.select(
                "promotion_target",
                where={
                    "account_uid": str(account.get("account_uid") or ""),
                    "plan_system": system,
                    "promotion_scene": scene,
                    "verification_state": "missing",
                },
            )
            for target in targets:
                if (
                    not str(target.get("last_verified_at") or "").strip()
                    or str(target.get("last_verification_error") or "").strip()
                ):
                    continue
                try:
                    capability = json.loads(
                        str(target.get("capability_json") or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    capability = {}
                eligibility = target_eligibility(
                    promotion_scene=scene,
                    plan_system=system,
                    platform_status=target.get("platform_status"),
                    verification_state="verified",
                    capability=(capability if isinstance(capability, dict) else {}),
                )
                store.update(
                    "promotion_target",
                    {
                        "verification_state": "verified",
                        "monitor_eligible": (
                            1 if eligibility["monitor_eligible"] else 0
                        ),
                        "retarget_eligible": (
                            1 if eligibility["retarget_eligible"] else 0
                        ),
                        "stop_eligible": (
                            1 if eligibility["stop_eligible"] else 0
                        ),
                        "ineligible_reason": str(
                            eligibility.get("ineligible_reason") or ""
                        )[:1000],
                    },
                    where={"target_uid": target["target_uid"]},
                )


def remove_qianchuan_account(
    value: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    with _ACCOUNT_DIRECTORY_LOCK:
        return _remove_qianchuan_account_unlocked(
            value,
            owner_username=owner_username,
            db=db,
        )


def _remove_qianchuan_account_unlocked(
    value: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """从用户目录移除账户，同时关闭该账户全部自动化但保留历史流水。"""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    account = get_qianchuan_account(
        value,
        owner_username=owner_username,
        db=store,
    )
    if not account or not account.get("directory_selected"):
        raise ValueError("千川账户不存在或已经移除")
    now_text = _now_text()
    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        store.update(
            "qianchuan_account",
            {
                "directory_selected": 0,
                "enabled": 0,
                "report_enabled": 0,
                "last_status": "removed",
                "last_error": "",
            },
            where={
                "account_uid": account["account_uid"],
                "owner_username": account["owner_username"],
            },
            connection=conn,
        )
        store.update(
            "promotion_target",
            {"enabled": 0, "capacity_state": "disabled"},
            where={"account_uid": account["account_uid"]},
            connection=conn,
        )
        store.update(
            "operation_log_sync_window",
            {
                "status": "cancelled",
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": "千川账户已从工具中移除",
            },
            where="account_uid=? AND status IN ('queued','running','backoff')",
            params=(account["account_uid"],),
            connection=conn,
        )
        store.execute(
            "UPDATE local_retarget_task SET status='cancelled',"
            "active_dedupe_key=NULL,result_message=?,finished_at=?,updated_at=? "
            "WHERE qianchuan_account_uid=? "
            "AND status IN ('pending','approved_queued','claimed')",
            (
                "千川账户已从工具中移除",
                now_text,
                now_text,
                account["account_uid"],
            ),
            connection=conn,
        )
    refresh_monitor_capacity(
        owner_username=account.get("owner_username"),
        db=store,
    )
    _sync_daily_selected_aavids(
        account.get("owner_username"),
        db=store,
    )
    return {
        "account_uid": account["account_uid"],
        "aavid": account["aavid"],
        "account_name": account.get("account_name") or "",
        "removed": True,
    }


def save_qianchuan_account_settings(
    value: Any,
    settings: Dict[str, Any],
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    store = db or SQLiteStore()
    account = get_qianchuan_account(
        value,
        owner_username=owner_username,
        db=store,
    )
    if not account:
        raise ValueError("千川账户不存在")
    route_mode = str(settings.get("route_mode", account.get("route_mode") or "default")).strip()
    if route_mode not in {"default", "custom"}:
        raise ValueError("飞书路由模式无效")
    groups = _json_list(
        settings.get("route_group_ids", account.get("route_group_ids") or [])
    )
    route_send_personal = bool(
        settings.get(
            "route_send_personal",
            account.get("route_send_personal"),
        )
    )
    if route_mode == "custom" and not route_send_personal and not groups:
        raise ValueError("账户单独路由至少选择个人或一个已绑定群")
    account_enabled = bool(settings.get("enabled", account.get("enabled")))
    values = {
        "enabled": 1 if account_enabled else 0,
        "report_enabled": (
            1
            if account_enabled
            and bool(settings.get("report_enabled", account.get("report_enabled")))
            else 0
        ),
        "route_mode": route_mode,
        "route_send_personal": (
            1
            if route_send_personal
            else 0
        ),
        "route_group_ids_json": json.dumps(groups, ensure_ascii=False),
    }
    store.update(
        "qianchuan_account",
        values,
        where={"account_uid": account["account_uid"]},
    )
    if not values["enabled"]:
        store.update(
            "operation_log_sync_window",
            {
                "status": "cancelled",
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": "千川账户已停用",
            },
            where="account_uid=? AND request_kind<>'manual' "
            "AND status IN ('queued','running','backoff')",
            params=(account["account_uid"],),
        )
    if not values["enabled"]:
        store.update(
            "promotion_target",
            {"capacity_state": "disabled"},
            where={"account_uid": account["account_uid"]},
        )
    refresh_monitor_capacity(db=store)
    _sync_daily_selected_aavids(
        account.get("owner_username") or owner_username,
        db=store,
    )
    saved = get_qianchuan_account(
        account["account_uid"],
        owner_username=owner_username,
        db=store,
    )
    assert saved is not None
    return saved


def save_qianchuan_account_automation_setup(
    value: Any,
    settings: Dict[str, Any],
    plan_states: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """Atomically persist account route/report settings and every plan intent."""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    account = get_qianchuan_account(
        value,
        owner_username=owner_username,
        db=store,
    )
    if not account:
        raise ValueError("千川账户不存在")
    raw_settings = dict(settings or {})
    normalized_settings = {
        "enabled": bool(raw_settings.get("enabled", account.get("enabled"))),
        "report_enabled": bool(
            raw_settings.get("report_enabled", account.get("report_enabled"))
        ),
        "route_mode": str(
            raw_settings.get("route_mode", account.get("route_mode") or "default")
        ).strip(),
        "route_send_personal": bool(
            raw_settings.get(
                "route_send_personal",
                account.get("route_send_personal"),
            )
        ),
        "route_group_ids": _json_list(
            raw_settings.get(
                "route_group_ids",
                account.get("route_group_ids") or [],
            )
        ),
    }
    if not normalized_settings["enabled"]:
        normalized_settings["report_enabled"] = False
    if normalized_settings["route_mode"] not in {"default", "custom"}:
        raise ValueError("飞书路由模式无效")
    # 账户自动化和飞书通知是两项独立能力。未绑定飞书时仍可启用账户、
    # 选择监控计划并运行本地自动化；只有需要发卡或通知的动作才检查飞书。

    if isinstance(plan_states, dict):
        plan_items = [
            {"target_uid": key, "enabled": value}
            for key, value in plan_states.items()
        ]
    elif isinstance(plan_states, list):
        plan_items = plan_states
    else:
        raise ValueError("计划设置必须为列表或对象")

    targets = store.select(
        "promotion_target",
        where={"account_uid": account["account_uid"]},
    )
    target_by_uid = {
        str(item.get("target_uid") or ""): item for item in targets
    }
    requested: Dict[str, bool] = {}
    for item in plan_items:
        if not isinstance(item, dict):
            raise ValueError("计划设置格式无效")
        uid = str(item.get("target_uid") or "").strip()
        if uid not in target_by_uid:
            raise ValueError("计划不属于当前千川账户，已取消全部保存")
        enabled = bool(item.get("enabled"))
        target = target_by_uid[uid]
        if (
            enabled
            and not bool(target.get("monitor_eligible"))
            and not bool(target.get("enabled"))
        ):
            raise ValueError(
                str(
                    target.get("ineligible_reason")
                    or f"计划 {target.get('plan_name') or uid} 尚不可监控"
                )
            )
        requested[uid] = enabled

    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        store.update(
            "qianchuan_account",
            {
                "enabled": 1 if normalized_settings["enabled"] else 0,
                "report_enabled": (
                    1 if normalized_settings["report_enabled"] else 0
                ),
                "route_mode": normalized_settings["route_mode"],
                "route_send_personal": (
                    1 if normalized_settings["route_send_personal"] else 0
                ),
                "route_group_ids_json": json.dumps(
                    normalized_settings["route_group_ids"],
                    ensure_ascii=False,
                ),
            },
            where={"account_uid": account["account_uid"]},
            connection=conn,
        )
        for uid, enabled in requested.items():
            store.update(
                "promotion_target",
                {
                    "enabled": 1 if enabled else 0,
                    "capacity_state": (
                        "active"
                        if enabled and normalized_settings["enabled"]
                        else "disabled"
                    ),
                },
                where={
                    "target_uid": uid,
                    "account_uid": account["account_uid"],
                },
                connection=conn,
            )
        if not normalized_settings["enabled"]:
            store.update(
                "operation_log_sync_window",
                {
                    "status": "cancelled",
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error": "千川账户已停用",
                },
                where="account_uid=? AND request_kind<>'manual' "
                "AND status IN ('queued','running','backoff')",
                params=(account["account_uid"],),
                connection=conn,
            )
        store.execute(
            "UPDATE operation_log_sync_window SET status='cancelled',"
            "lease_owner=NULL,lease_expires_at=NULL,last_error=?,updated_at=? "
            "WHERE account_uid=? AND object_type='AD' AND request_kind<>'manual' "
            "AND status IN ('queued','running','backoff') "
            "AND NOT EXISTS (SELECT 1 FROM promotion_target t "
            "WHERE t.account_uid=operation_log_sync_window.account_uid "
            "AND t.ad_id=operation_log_sync_window.object_id AND t.enabled=1)",
            (
                "计划已取消监控",
                _now_text(),
                account["account_uid"],
            ),
            connection=conn,
        )

    refresh_monitor_capacity(owner_username=owner_username, db=store)
    _sync_daily_selected_aavids(
        account.get("owner_username") or owner_username,
        db=store,
    )
    saved = get_qianchuan_account(
        account["account_uid"],
        owner_username=owner_username,
        db=store,
    )
    assert saved is not None
    # A save starts first collection only for newly selected, failed or never
    # collected targets. Healthy targets keep their normal periodic schedule
    # and must not flash back to a queued state on every account save.
    saved["immediate_collection_target_uids"] = [
        uid
        for uid, enabled in requested.items()
        if enabled
        and (
            not bool(target_by_uid[uid].get("enabled"))
            or not str(target_by_uid[uid].get("last_sync_at") or "").strip()
            or str(target_by_uid[uid].get("last_status") or "").lower()
            in {"error", "failed", "queued"}
        )
    ]
    return saved


def bind_target_account_scope(
    target_uid: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """为迁移前的计划补齐账户归属；已有归属绝不改绑。"""
    store = db or SQLiteStore()
    uid = str(target_uid or "").strip()
    target = store.select_one("promotion_target", where={"target_uid": uid})
    if not target:
        return None, None
    account_uid = str(target.get("account_uid") or "").strip()
    account = (
        store.select_one("qianchuan_account", where={"account_uid": account_uid})
        if account_uid
        else None
    )
    owner = _owner_key(owner_username)
    if account and str(account.get("owner_username") or "").casefold() != owner:
        raise ValueError("监控计划不属于当前工具账号")
    if not account:
        account_view = ensure_qianchuan_account(
            target.get("aadvid"),
            owner_username=owner_username,
            directory_selected=False,
            db=store,
        )
        account_uid = str(account_view["account_uid"])
        store.update(
            "promotion_target",
            {"account_uid": account_uid},
            where={"target_uid": uid},
        )
        account = store.select_one(
            "qianchuan_account",
            where={"account_uid": account_uid},
        )
    refresh_monitor_capacity(owner_username=owner, db=store)
    return (
        store.select_one("promotion_target", where={"target_uid": uid}),
        account,
    )


def _estimate_duration_ms(row: Dict[str, Any]) -> int:
    try:
        value = int(row.get("last_duration_ms") or DEFAULT_TARGET_DURATION_MS)
    except (TypeError, ValueError):
        value = DEFAULT_TARGET_DURATION_MS
    return max(MIN_TARGET_DURATION_MS, min(MAX_TARGET_DURATION_MS, value))


def _parallel_cycle_ms(
    durations: Iterable[int], *, workers: int = CAPACITY_PARALLEL_WORKERS
) -> int:
    """Estimate wall-clock cycle time using the same bounded worker count.

    Assigning each target to the currently least-loaded lane mirrors the
    scheduler closely enough for admission without pretending three concurrent
    API requests take the sum of all their durations.
    """
    lanes = [0] * max(1, int(workers))
    for duration in durations:
        lane = min(range(len(lanes)), key=lanes.__getitem__)
        lanes[lane] += max(0, int(duration))
    return max(lanes, default=0)


def refresh_monitor_capacity(
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """按最近耗时准入计划；超出五分钟预算的目标进入等待容量。"""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner = _owner_key(owner_username)
    rows = store.execute(
        "SELECT t.target_uid,t.account_uid,t.enabled,t.monitor_eligible,t.capacity_state,t.last_duration_ms,"
        "a.enabled AS account_enabled "
        "FROM promotion_target t "
        "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
        "WHERE a.owner_username=? "
        "ORDER BY CASE WHEN t.capacity_state='active' THEN 0 ELSE 1 END,"
        "t.created_at ASC,t.id ASC",
        (owner,),
        fetch=True,
    ) or []
    budget_ms = CAPACITY_WINDOW_SECONDS * 1000
    account_durations: Dict[str, List[int]] = {}
    active = 0
    waiting = 0
    disabled = 0
    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        for row in rows:
            desired = (
                bool(row.get("enabled"))
                and bool(row.get("account_enabled"))
                and bool(row.get("monitor_eligible"))
            )
            if not desired:
                state = "disabled"
                disabled += 1
            else:
                estimate = _estimate_duration_ms(row)
                account_uid = str(row.get("account_uid") or "unknown")
                candidate_durations = {
                    key: list(values)
                    for key, values in account_durations.items()
                }
                candidate_durations.setdefault(account_uid, []).append(estimate)
                # The runtime has six global plan lanes, but at most two lanes
                # may belong to one advertiser. Model each advertiser's
                # two-lane makespan before packing advertisers globally.
                account_cycle_loads = [
                    _parallel_cycle_ms(
                        values,
                        workers=CAPACITY_ACCOUNT_PARALLEL_WORKERS,
                    )
                    for values in candidate_durations.values()
                ]
                if _parallel_cycle_ms(account_cycle_loads) <= budget_ms:
                    state = "active"
                    account_durations = candidate_durations
                    active += 1
                else:
                    state = "capacity_waiting"
                    waiting += 1
            store.update(
                "promotion_target",
                {
                    "capacity_state": state,
                    **({"last_lag_seconds": 0} if state == "disabled" else {}),
                },
                where={"target_uid": row["target_uid"]},
                connection=conn,
            )
    return {
        "active_count": active,
        "waiting_count": waiting,
        "disabled_count": disabled,
        "estimated_cycle_seconds": int(
            round(
                _parallel_cycle_ms(
                    _parallel_cycle_ms(
                        values,
                        workers=CAPACITY_ACCOUNT_PARALLEL_WORKERS,
                    )
                    for values in account_durations.values()
                )
                / 1000
            )
        ),
        "parallel_workers": CAPACITY_PARALLEL_WORKERS,
        "account_parallel_workers": CAPACITY_ACCOUNT_PARALLEL_WORKERS,
        "capacity_window_seconds": CAPACITY_WINDOW_SECONDS,
        "stale_after_seconds": CAPACITY_STALE_SECONDS,
        "healthy": waiting == 0,
    }


def record_target_duration(
    target_uid: Any,
    duration_ms: Any,
    *,
    interval_seconds: int = 5 * 60,
    cycle_started_at: Optional[datetime] = None,
    refresh_capacity: bool = True,
    db: Optional[SQLiteStore] = None,
) -> None:
    store = db or SQLiteStore()
    target = store.select_one(
        "promotion_target",
        fields="last_duration_ms",
        where={"target_uid": str(target_uid or "").strip()},
    )
    if not target:
        return
    try:
        measured = max(
            MIN_TARGET_DURATION_MS,
            min(MAX_TARGET_DURATION_MS, int(duration_ms)),
        )
    except (TypeError, ValueError):
        return
    old = int(target.get("last_duration_ms") or measured)
    # Slow samples tighten admission immediately. Fast samples recover through
    # EWMA instead of being permanently pinned to an old slow/429 sample.
    interval_ms = max(30, int(interval_seconds)) * 1000
    if old > interval_ms >= measured:
        # A target that was waiting under the former all-history collector
        # must re-enter immediately once the active-only collector proves it
        # can meet the SLA. Keeping a stale multi-minute EWMA would otherwise
        # starve it for several additional cycles.
        smoothed = measured
    else:
        smoothed = (
            measured
            if measured >= old
            else max(
                MIN_TARGET_DURATION_MS,
                int(round(old * 0.7 + measured * 0.3)),
            )
        )
    # The five-minute SLA is start-to-start.  Scheduling from the finish time
    # silently turns a 164-second collection into a 7m44s cycle.  Anchor the
    # next run to the cycle start; when a cycle itself overruns, retry promptly
    # without spinning in a zero-delay loop.
    now = datetime.now()
    cadence_start = cycle_started_at or now
    next_due = cadence_start + timedelta(seconds=max(30, int(interval_seconds)))
    if next_due <= now:
        next_due = now + timedelta(seconds=5)
    store.update(
        "promotion_target",
        {
            "last_duration_ms": smoothed,
            "next_due_at": next_due.strftime("%Y-%m-%d %H:%M:%S"),
            "last_lag_seconds": 0,
        },
        where={"target_uid": str(target_uid or "").strip()},
    )
    if refresh_capacity:
        refresh_monitor_capacity(db=store)


def capacity_snapshot(
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    store = db or SQLiteStore()
    summary = refresh_monitor_capacity(owner_username=owner_username, db=store)
    rows = store.execute(
        "SELECT t.target_uid,t.last_sync_at,t.capacity_state FROM promotion_target t "
        "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
        "WHERE t.enabled=1 AND a.owner_username=?",
        (_owner_key(owner_username),),
        fetch=True,
    ) or []
    now = datetime.now()
    delayed: List[str] = []
    max_lag = 0
    for row in rows:
        if str(row.get("capacity_state") or "") != "active":
            continue
        text = str(row.get("last_sync_at") or "").strip()
        try:
            lag = int((now - datetime.strptime(text, "%Y-%m-%d %H:%M:%S")).total_seconds())
        except Exception:
            lag = CAPACITY_STALE_SECONDS + 1
        lag = max(0, lag)
        max_lag = max(max_lag, lag)
        store.update(
            "promotion_target",
            {"last_lag_seconds": lag},
            where={"target_uid": row["target_uid"]},
        )
        if lag > CAPACITY_STALE_SECONDS:
            delayed.append(str(row["target_uid"]))
    summary.update(
        {
            "delayed_count": len(delayed),
            "delayed_target_uids": delayed,
            "max_lag_seconds": max_lag,
            "healthy": bool(summary.get("healthy")) and not delayed,
        }
    )
    return summary


def capacity_snapshot_readonly(
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> Dict[str, Any]:
    """Read the persisted capacity state without repairing or writing rows.

    Capacity admission is recalculated when account settings change, target
    evidence changes and before the scheduler chooses work.  A page view must
    not repeat that write-heavy calculation for every target.
    """
    store = db or SQLiteStore()
    rows = store.execute(
        "SELECT t.target_uid,t.account_uid,t.enabled,t.monitor_eligible,t.capacity_state,"
        "t.last_duration_ms,t.last_sync_at,a.enabled AS account_enabled "
        "FROM promotion_target t "
        "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
        "WHERE a.owner_username=? AND a.directory_selected=1",
        (_owner_key(owner_username),),
        fetch=True,
    ) or []
    active = 0
    waiting = 0
    disabled = 0
    active_account_durations: Dict[str, List[int]] = {}
    delayed: List[str] = []
    max_lag = 0
    now = datetime.now()
    for row in rows:
        desired = (
            bool(row.get("enabled"))
            and bool(row.get("account_enabled"))
            and bool(row.get("monitor_eligible"))
        )
        state = str(row.get("capacity_state") or "disabled")
        if not desired:
            disabled += 1
            continue
        if state == "capacity_waiting":
            waiting += 1
            continue
        if state != "active":
            disabled += 1
            continue
        active += 1
        account_uid = str(row.get("account_uid") or "unknown")
        active_account_durations.setdefault(account_uid, []).append(
            _estimate_duration_ms(row)
        )
        text = str(row.get("last_sync_at") or "").strip()
        try:
            lag = int(
                (now - datetime.strptime(text, "%Y-%m-%d %H:%M:%S"))
                .total_seconds()
            )
        except Exception:
            lag = CAPACITY_STALE_SECONDS + 1
        lag = max(0, lag)
        max_lag = max(max_lag, lag)
        if lag > CAPACITY_STALE_SECONDS:
            delayed.append(str(row.get("target_uid") or ""))
    return {
        "active_count": active,
        "waiting_count": waiting,
        "disabled_count": disabled,
        "estimated_cycle_seconds": int(
            round(
                _parallel_cycle_ms(
                    _parallel_cycle_ms(
                        values,
                        workers=CAPACITY_ACCOUNT_PARALLEL_WORKERS,
                    )
                    for values in active_account_durations.values()
                )
                / 1000
            )
        ),
        "parallel_workers": CAPACITY_PARALLEL_WORKERS,
        "account_parallel_workers": CAPACITY_ACCOUNT_PARALLEL_WORKERS,
        "capacity_window_seconds": CAPACITY_WINDOW_SECONDS,
        "stale_after_seconds": CAPACITY_STALE_SECONDS,
        "delayed_count": len(delayed),
        "delayed_target_uids": delayed,
        "max_lag_seconds": max_lag,
        "healthy": waiting == 0 and not delayed,
    }


def schedulable_promotion_targets(
    *,
    owner_username: Any = None,
    refresh_capacity: bool = True,
    db: Optional[SQLiteStore] = None,
) -> List[Dict[str, Any]]:
    from api.promotion_targets import _target_row

    store = db or SQLiteStore()
    owner = _owner_key(owner_username)
    if refresh_capacity:
        refresh_monitor_capacity(owner_username=owner, db=store)
    rows = store.execute(
        "SELECT t.* FROM promotion_target t "
        "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
        "WHERE t.enabled=1 AND t.monitor_eligible=1 "
        "AND t.capacity_state='active' AND a.enabled=1 "
        "AND a.owner_username=? "
        "ORDER BY COALESCE(t.last_sync_at,'') ASC,t.created_at ASC,t.id ASC",
        (owner,),
        fetch=True,
    ) or []
    result = [_target_row(row) for row in rows]

    # Always reserve a tiny probe quota for the oldest capacity-waiting
    # targets. Otherwise a target that started with a pessimistic estimate (or
    # suffered one slow sample) can never be measured again and can never
    # recover. Probes still obey due_at, per-account serialization and API
    # backoff in the collector.
    waiting_rows = store.execute(
        "SELECT t.* FROM promotion_target t "
        "JOIN qianchuan_account a ON a.account_uid=t.account_uid "
        "WHERE t.enabled=1 AND t.monitor_eligible=1 "
        "AND t.capacity_state='capacity_waiting' AND a.enabled=1 "
        "AND a.owner_username=? "
        "ORDER BY CASE WHEN COALESCE(t.last_sync_at,'')='' THEN 0 ELSE 1 END,"
        "COALESCE(t.last_sync_at,t.created_at) ASC,t.created_at ASC,t.id ASC",
        (owner,),
        fetch=True,
    ) or []
    probe_accounts: set[str] = set()
    for row in waiting_rows:
        account_uid = str(row.get("account_uid") or "")
        if account_uid in probe_accounts:
            continue
        probe = _target_row(row)
        probe["capacity_probe"] = True
        result.append(probe)
        probe_accounts.add(account_uid)
        if len(probe_accounts) >= CAPACITY_PROBE_TARGETS_PER_CYCLE:
            break
    return result


def resolve_account_feishu_targets(
    aavid: Any,
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
    default_targets: Optional[List[Tuple[str, str]]] = None,
) -> List[Tuple[str, str]]:
    from services.local_feishu_bridge import (
        get_local_feishu_status,
        list_local_feishu_bound_targets,
    )

    account = get_qianchuan_account(
        aavid,
        owner_username=owner_username,
        db=db,
    )
    defaults = (
        list(default_targets)
        if default_targets is not None
        else list_local_feishu_bound_targets()
    )
    if not account or account.get("route_mode") != "custom":
        return defaults
    status = get_local_feishu_status()
    profile = status.get("profile") or {}
    result: List[Tuple[str, str]] = []
    authorized = str(profile.get("authorized_open_id") or "").strip()
    if account.get("route_send_personal") and authorized:
        result.append(("open_id", authorized))
    bound_groups = {
        str(item.get("chat_id") or "").strip()
        for item in profile.get("groups") or []
        if isinstance(item, dict)
    }
    for chat_id in account.get("route_group_ids") or []:
        if chat_id in bound_groups:
            result.append(("chat_id", chat_id))
    return result


def migrate_existing_qianchuan_accounts(
    *,
    owner_username: Any = None,
    authorized_aavids: Optional[Iterable[Any]] = None,
    db: Optional[SQLiteStore] = None,
) -> int:
    """从旧数据创建账户目录；无归属数据只按当前会话已授权账户迁移。"""
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner = _owner_key(owner_username)
    # 尚未登录工具账号时不抢占旧数据；登录成功后由明确账号完成一次性归属迁移。
    if owner == DEFAULT_OWNER:
        return 0
    authorized = (
        {
            str(item or "").strip()
            for item in authorized_aavids
            if str(item or "").strip().isdigit()
        }
        if authorized_aavids is not None
        else None
    )
    selected = _daily_selected_aavids(owner)
    scope_clause = (
        "(COALESCE(account_uid,'')='' OR account_uid IN "
        "(SELECT account_uid FROM qianchuan_account WHERE owner_username=?))"
        if authorized is not None
        else "account_uid IN "
        "(SELECT account_uid FROM qianchuan_account WHERE owner_username=?)"
    )
    rows = store.execute(
        # user_info_name 只用于旧数据首次建档的可读名称兜底，后续不能覆盖
        # 已经从 advName/accountName 得到的权威广告主账户名。
        "SELECT aadvid AS aavid,user_info_name AS account_name,updated_at FROM pmc_ad_detail_basic "
        f"WHERE COALESCE(aadvid,'')<>'' AND {scope_clause} "
        "UNION ALL SELECT aadvid AS aavid,'' AS account_name,updated_at FROM promotion_target "
        f"WHERE COALESCE(aadvid,'')<>'' AND {scope_clause} "
        "UNION ALL SELECT aavid,'' AS account_name,occurred_at AS updated_at FROM account_operation_event "
        f"WHERE COALESCE(aavid,'')<>'' AND {scope_clause} "
        "ORDER BY updated_at DESC",
        (owner, owner, owner),
        fetch=True,
    ) or []
    by_aavid: Dict[str, str] = {}
    for row in rows:
        aid = str(row.get("aavid") or "").strip()
        if not aid or not aid.isdigit():
            continue
        if authorized is not None and aid not in authorized:
            continue
        name = str(row.get("account_name") or "").strip()
        if aid not in by_aavid or (name and not by_aavid[aid]):
            by_aavid[aid] = name
    for aid, name in by_aavid.items():
        existing = store.select_one(
            "qianchuan_account",
            where={"owner_username": owner, "aavid": aid},
        )
        existing_name = str((existing or {}).get("account_name") or "").strip()
        weak_legacy_name = (
            name
            if (
                not existing
                or not existing_name
                or existing_name == f"千川账户 {aid}"
            )
            else ""
        )
        ensure_qianchuan_account(
            aid,
            account_name=weak_legacy_name,
            owner_username=owner,
            directory_selected=(
                bool((existing or {}).get("directory_selected"))
                if existing
                else True
            ),
            # 旧日报配置只用于首次建目录；后续读取页面不能覆盖用户刚保存的选择。
            report_enabled=(aid in selected) if not existing else None,
            db=store,
        )
    with store.transaction() as conn:
        store.execute("BEGIN IMMEDIATE", connection=conn)
        for aid in by_aavid:
            uid = make_account_uid(aid, owner)
            # 无归属旧计划的迁移只能恢复“可查看的历史记录”，不能继承旧工具
            # 账号的启用意图、目录核验或写能力证据。用户必须重新精确核验并启用。
            store.update(
                "promotion_target",
                {
                    "account_uid": uid,
                    "enabled": 0,
                    "platform_status": "unknown",
                    "verification_state": "legacy_unverified",
                    "catalog_seen_at": None,
                    "last_verified_at": None,
                    "last_verification_error": "旧计划已安全迁移，等待重新核验",
                    "monitor_eligible": 0,
                    "retarget_eligible": 0,
                    "stop_eligible": 0,
                    "ineligible_reason": "旧计划等待当前工具账号重新核验",
                    "capability_json": "{}",
                    "last_status": "pending",
                    "last_error": "",
                    "capacity_state": "disabled",
                },
                where="aadvid=? AND COALESCE(account_uid,'')=''",
                params=(aid,),
                connection=conn,
            )
            for table, column in (
                ("pmc_ad_detail_basic", "aadvid"),
                ("pmc_retargeting_run", "aavid"),
                ("pmc_regulation_run", "aavid"),
                ("pmc_roi2_assist_task", "aadvid"),
                ("account_operation_event", "aavid"),
                ("platform_log_sync_state", "aavid"),
            ):
                store.update(
                    table,
                    {"account_uid": uid},
                    where=f"{column}=? AND COALESCE(account_uid,'')=''",
                    params=(aid,),
                    connection=conn,
                )
            store.update(
                "operation_daily_report_delivery",
                {"qianchuan_account_uid": uid},
                where="aavid=? AND COALESCE(qianchuan_account_uid,'')=''",
                params=(aid,),
                connection=conn,
            )
    refresh_monitor_capacity(owner_username=owner, db=store)
    return len(by_aavid)


def upsert_authorized_accounts(
    accounts: Iterable[Dict[str, Any]],
    *,
    owner_username: Any = None,
    db: Optional[SQLiteStore] = None,
) -> List[Dict[str, Any]]:
    store = db or SQLiteStore()
    saved: List[Dict[str, Any]] = []
    for item in accounts or []:
        if not isinstance(item, dict):
            continue
        aid = str(
            item.get("aavid")
            or item.get("advertiser_id")
            or item.get("advertiserId")
            or ""
        ).strip()
        if not aid.isdigit():
            continue
        saved.append(
            ensure_qianchuan_account(
                aid,
                account_name=(
                    item.get("account_name")
                    or item.get("advertiser_name")
                    or item.get("advertiserName")
                    or item.get("name")
                    or ""
                ),
                owner_username=owner_username,
                directory_selected=False,
                seen=True,
                db=store,
            )
        )
    return saved
