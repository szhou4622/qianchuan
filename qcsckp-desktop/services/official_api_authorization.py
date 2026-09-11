"""Non-secret authorization scopes and quota migration for read-only collection."""
from __future__ import annotations

import hashlib
import json
import math
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Mapping

PREFIX = "v2:"
_AUTH_BACKOFF = {}


def scope_key(owner, app_id, account_id):
    return PREFIX + json.dumps([str(owner).casefold(), str(app_id or ""), str(account_id or "")],
                               ensure_ascii=True, separators=(",", ":"))


def split_scope(value, identity):
    raw = str(value or "")
    if raw.startswith(PREFIX):
        try:
            parts = json.loads(raw[len(PREFIX):])
            if isinstance(parts, list) and len(parts) == 3 and all(isinstance(v, str) for v in parts):
                return tuple(parts)
        except ValueError:
            pass
    return (str(identity.get("owner_username") or "").casefold(),
            str(identity.get("app_id") or ""), raw)


def scope_hash(owner, kind, value):
    return hashlib.sha256(f"{owner}|{kind}|{value}".encode()).hexdigest()


def backoff_reason(value, default="rate_limit"):
    raw = str(value or "")
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        meta = {}
    if isinstance(meta, dict) and meta.get("reason") in {"rate_limit", "token", "permission"}:
        return meta["reason"]
    lower = raw.lower()
    # A real throttle wins even if the accompanying text mentions permissions.
    if any(token in lower for token in ("40110", "429", "rate_limit", "too many requests")):
        return "rate_limit"
    if any(token in lower for token in ("41013", "41001", "41002", "refresh_token", "apitokenerror", "令牌", "重新授权", "token")):
        return "token"
    if any(token in lower for token in ("apipermissionerror", "permission", "权限", "无权")):
        return "permission"
    return default


def metadata(value):
    try:
        result = json.loads(str(value or "{}"))
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _remaining(row, now=None):
    try:
        due = datetime.strptime(str(row.get("backoff_until") or ""), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 0
    return max(0, math.ceil((due - (now or datetime.now())).total_seconds()))


def _upsert_window(store, owner, kind, value, due, meta, *, connection, count=1):
    key = scope_hash(owner, kind, value)
    row = store.select_one("api_quota_state", where={"scope_key": key}, connection=connection) or {}
    old_due = str(row.get("backoff_until") or "")
    if kind == "account_auth" and metadata(row.get("last_error")).get("auth_generation") != meta.get("auth_generation"):
        old_due = ""
    store.insert_or_update("api_quota_state", {
        "scope_key": key, "owner_username": owner, "scope_type": kind, "scope_id": value,
        "backoff_until": max(old_due, due),
        "rate_limit_count": int(row.get("rate_limit_count") or 0) + count,
        "last_error": json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
        "last_request_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, unique_fields=["scope_key"], connection=connection)


def migrate_legacy_windows(store, identity, *, connection=None):
    """Bind previously unscoped rows once; never apply app A's window to app B."""
    owner, app = str(identity.get("owner_username") or ""), str(identity.get("app_id") or "")
    if not store.execute("SELECT 1 FROM api_quota_state WHERE owner_username=? "
                         "AND scope_type IN ('account','application') AND scope_id NOT LIKE 'v2:%' LIMIT 1",
                         (owner,), fetch=True, connection=connection):
        return
    with (nullcontext(connection) if connection is not None else store.transaction()) as conn:
        if connection is None:
            conn.execute("BEGIN IMMEDIATE")
        rows = store.select("api_quota_state", where={"owner_username": owner}, connection=conn) or []
        for row in rows:
            kind, value = str(row.get("scope_type") or ""), str(row.get("scope_id") or "")
            if kind not in {"account", "application"} or value.startswith(PREFIX):
                continue
            reason = backoff_reason(row.get("last_error"))
            destination = "account_auth" if reason != "rate_limit" else kind
            target = scope_key(owner, app, "" if kind == "application" else value)
            _upsert_window(store, owner, destination, target, str(row.get("backoff_until") or ""),
                {"reason": reason, "app_id": app, "auth_generation": identity.get("auth_generation", ""),
                 "legacy_migrated": True}, connection=conn, count=int(row.get("rate_limit_count") or 0))
            store.execute("DELETE FROM api_quota_state WHERE scope_key=?", (row["scope_key"],), connection=conn)


def persist_window(store, identity, kind, value, seconds, *, reason, now=None):
    if not hasattr(store, "config") or not isinstance(store.config, dict):
        return
    due = ((now or datetime.now()) + timedelta(seconds=max(30, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")
    with store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _upsert_window(store, identity["owner_username"], kind, value, due,
            {"reason": reason, "app_id": identity.get("app_id", ""),
             "auth_generation": identity.get("auth_generation", "")}, connection=conn)


def backoff_state(api, account_key, *, db=None):
    identity = api._quota_identity()
    owner, app, account = split_scope(account_key, identity)
    canonical = scope_key(owner, app, account)
    identity = {**identity, "owner_username": owner, "app_id": app}
    with api._ACTIVE_LOCK:
        rate = max(0, math.ceil(float(api._ACCOUNT_BACKOFF_UNTIL.get(canonical, 0)) - time.monotonic()))
        auth_due, auth_reason = _AUTH_BACKOFF.get((canonical, identity.get("auth_generation", "")), (0, "token"))
    auth = max(0, math.ceil(auth_due - time.monotonic()))
    if db is not None and isinstance(db, api.SQLiteStore):
        migrate_legacy_windows(db, identity)
        for kind, value in (("account", canonical), ("application", scope_key(owner, app, "")),
                            ("account_auth", canonical), ("account_auth", scope_key(owner, app, ""))):
            row = db.select_one("api_quota_state", where={"scope_key": scope_hash(owner, kind, value)}) or {}
            seconds = _remaining(row, now=api.datetime.now())
            reason = backoff_reason(row.get("last_error"), "token" if kind == "account_auth" else "rate_limit")
            if reason == "rate_limit":
                rate = max(rate, seconds)
            elif metadata(row.get("last_error")).get("auth_generation", "") == identity.get("auth_generation", ""):
                if seconds > auth:
                    auth, auth_reason = seconds, reason
    if rate:
        with api._ACTIVE_LOCK:
            api._ACCOUNT_RATE_LIMITED.add(account_key)
            api._ACCOUNT_CLEAN_CYCLES[account_key] = 0
    # Rate stays visible while it independently prevents a fresh read.
    return {"seconds": max(rate, auth), "reason": "rate_limit" if rate >= auth and rate else auth_reason if auth else "",
            "rate_limit_seconds": rate, "authorization_seconds": auth}


def set_backoff(api, account_key, seconds, *, db=None, include_application=False, reason="rate_limit"):
    identity = api._quota_identity()
    owner, app, account = split_scope(account_key, identity)
    identity = {**identity, "owner_username": owner, "app_id": app}
    canonical = scope_key(owner, app, account)
    context = api.current_collection_context()
    guard = nullcontext()
    if context and getattr(context, "authorization_identity", None):
        from services.qianchuan_open_api.token_provider import authorization_identity_guard
        guard = authorization_identity_guard(context.authorization_identity)
    with guard:
        if context:
            context.check_active("before_backoff_commit")
        with api._ACTIVE_LOCK:
            if reason == "rate_limit":
                api._ACCOUNT_BACKOFF_UNTIL[canonical] = max(api._ACCOUNT_BACKOFF_UNTIL.get(canonical, 0),
                                                            time.monotonic() + max(30, int(seconds)))
            else:
                key = (canonical, identity.get("auth_generation", ""))
                _AUTH_BACKOFF[key] = (max(_AUTH_BACKOFF.get(key, (0, reason))[0],
                                          time.monotonic() + max(30, int(seconds))), reason)
        if db is not None and isinstance(db, api.SQLiteStore):
            persist_window(db, identity, "account" if reason == "rate_limit" else "account_auth",
                           canonical, seconds, reason=reason, now=api.datetime.now())
            if include_application and reason == "rate_limit":
                persist_window(db, identity, "application", scope_key(owner, app, ""), seconds, reason=reason, now=api.datetime.now())


def _detach_directory_accounts(store, owner, account_uids, *, connection, now, reason):
    """Detach current selections, preserving all metrics and completed business history.

    The caller owns the directory lock, authorization guard and write transaction.
    A claimed/executing card is revoked only when no durable send intent exists;
    uncertain/submitted platform operations retain their original reconciliation.
    """
    ids = sorted({str(uid) for uid in account_uids if uid})
    if not ids:
        return {"accounts_detached": 0, "cards_cancelled": 0}
    marks = ",".join("?" for _ in ids)
    params = [owner, *ids]
    store.execute(
        "UPDATE qianchuan_account SET directory_selected=0,enabled=0,report_enabled=0,"
        "last_status='removed',last_error='',catalog_status='not_synced',catalog_error='',updated_at=? "
        f"WHERE owner_username=? AND account_uid IN ({marks})",
        [now, *params], connection=connection,
    )
    store.execute(
        "UPDATE promotion_target SET enabled=0,capacity_state='disabled',monitor_eligible=0,"
        "retarget_eligible=0,stop_eligible=0,verification_state='candidate',last_verified_at=NULL,"
        "last_status='authorization_required',ineligible_reason=?,updated_at=? "
        f"WHERE account_uid IN (SELECT account_uid FROM qianchuan_account WHERE owner_username=? AND account_uid IN ({marks}))",
        [reason, now, *params], connection=connection,
    )
    store.execute(
        "UPDATE collection_job SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,"
        "fencing_token=fencing_token+1,last_error=?,updated_at=? WHERE owner_username=? "
        f"AND account_uid IN ({marks}) AND status IN ('queued','retry','leased')",
        [reason, now, *params], connection=connection,
    )
    store.execute(
        "UPDATE operation_log_sync_window SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,"
        "fencing_token=fencing_token+1,last_error=?,updated_at=? WHERE owner_username=? "
        f"AND account_uid IN ({marks}) AND status IN ('queued','running','backoff')",
        [reason, now, *params], connection=connection,
    )
    # Old cards without a Qianchuan account UID cannot prove which selection
    # produced them; revoke only unsubmitted cards for this tool owner as well.
    rows = store.execute(
        "SELECT task_uid FROM local_retarget_task t WHERE account_username=? "
        f"AND (qianchuan_account_uid IN ({marks}) OR COALESCE(qianchuan_account_uid,'')='') "
        "AND status IN ('pending','approved_queued','claimed','executing') "
        "AND NOT EXISTS (SELECT 1 FROM execution_reconciliation e WHERE e.account_username=t.account_username "
        "AND (e.task_uid=t.task_uid OR substr(e.task_uid,1,length(t.task_uid)+1)=t.task_uid || ':'))",
        params, fetch=True, connection=connection,
    ) or []
    task_ids = [str(row["task_uid"]) for row in rows]
    for uid in task_ids:
        store.execute(
            "UPDATE local_retarget_task SET status='cancelled',active_dedupe_key=NULL,claim_token=NULL,"
            "claim_expires_at=NULL,claimed_at=NULL,fencing_token=fencing_token+1,"
            "result_message=?,finished_at=?,updated_at=? WHERE task_uid=? AND account_username=?",
            [reason + "；本卡已取消，未向千川提交", now, now, uid, owner], connection=connection,
        )
        # Never start a new delivery for a cancelled card. In-flight sends keep
        # their receipt path and will read the now-terminal task for PATCH.
        store.execute(
            "UPDATE feishu_outbox SET status='cancelled',last_error=?,updated_at=? "
            "WHERE account_username=? AND task_uid=? AND operation='send' AND status='queued'",
            [reason, now, owner, uid], connection=connection,
        )
    return {"accounts_detached": len(ids), "cards_cancelled": len(task_ids)}


def reconcile_selected_authorized_accounts(identity, accounts, evidence, *, db):
    """Repair legacy stale selections only against a complete current grant list.

    Legacy selection time cannot be reconstructed. Still-authorized selections
    remain untouched; this helper never guesses which account was newly added.
    """
    if not evidence.get("complete"):
        return {"status": "incomplete", "accounts_detached": 0, "cards_cancelled": 0}
    from services.qianchuan_accounts import _ACCOUNT_DIRECTORY_LOCK
    from services.qianchuan_open_api.token_provider import authorization_identity_guard
    owner = str(identity.get("owner_username") or "").casefold()
    allowed = set()
    for row in accounts:
        aid = str(row.get("advertiser_id") or row.get("aavid") or "").strip() if isinstance(row, Mapping) else ""
        if not aid.isdigit():
            raise ValueError("完整授权账户列表中存在无法识别的账户，保留原选择")
        allowed.add(aid)
    if not owner or not identity.get("app_id"):
        raise ValueError("缺少当前千川授权身份")
    with authorization_identity_guard(identity), _ACCOUNT_DIRECTORY_LOCK, db.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = db.select("qianchuan_account", where={"owner_username": owner, "directory_selected": 1}, connection=conn)
        detached = [row["account_uid"] for row in existing if str(row.get("aavid") or "") not in allowed]
        counts = _detach_directory_accounts(db, owner, detached, connection=conn,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason="账户不在当前完整千川授权范围内")
    return {"status": "reconciled", **counts}


def handle_change(api, previous, current, *, event, db=None, notify_catalog=None):
    """Invalidate access evidence, not user strategy or historical metrics."""
    from services.qianchuan_open_api.token_provider import authorization_identity_guard, authorization_identity_is_current, AuthorizationContextChanged
    from services.qianchuan_accounts import _ACCOUNT_DIRECTORY_LOCK
    if not authorization_identity_is_current(current):
        return {"status": "superseded", "cancelled_batches": 0}
    store = db or api.SQLiteStore()
    api._ensure_collection_schema(store)
    owner = str(current.get("owner_username") or "").casefold()
    contexts = api.collection_lifecycle.active_contexts(api.collection_lifecycle.generation())
    affected = []
    for context in contexts:
        prior = getattr(context, "authorization_identity", None) or {}
        target = getattr(context, "target_identity", None) or {}
        if str(prior.get("owner_username") or target.get("owner_username") or "").casefold() == owner:
            if not prior or prior == previous:
                context.cancel("千川授权已变化，旧采集批次已撤销")
                affected.append(context)
    api._release_cancelled_collection_leases(affected)
    try:
        with authorization_identity_guard(current), _ACCOUNT_DIRECTORY_LOCK:
            with store.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # Before switching apps, pin legacy real limits to the OLD app.
                legacy_identity = previous if previous.get("app_id") and previous.get("owner_username") == owner else current
                migrate_legacy_windows(store, legacy_identity, connection=conn)
                rows = store.select("api_quota_state", where={"owner_username": owner}, connection=conn) or []
                cleared = 0
                for row in rows:
                    old_generation = metadata(row.get("last_error")).get("auth_generation", "") != current.get("auth_generation", "")
                    if old_generation and (row.get("scope_type") == "account_auth" or backoff_reason(row.get("last_error")) != "rate_limit"):
                        store.execute("DELETE FROM api_quota_state WHERE scope_key=?", (row["scope_key"],), connection=conn)
                        cleared += 1
                targets = store.execute("SELECT t.* FROM promotion_target t JOIN qianchuan_account a "
                    "ON a.account_uid=t.account_uid WHERE a.owner_username=?", (owner,), fetch=True, connection=conn) or []
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                detach = event == "disconnected" or str(previous.get("app_id") or "") != str(current.get("app_id") or "")
                detached_ids = set()
                detached_counts = {"accounts_detached": 0, "cards_cancelled": 0}
                if detach:
                    accounts = store.select("qianchuan_account", where={"owner_username": owner}, connection=conn) or []
                    detached_ids = {row["account_uid"] for row in accounts
                        if event == "disconnected" or metadata(row.get("selection_authorization_json")) != current}
                    detached_counts = _detach_directory_accounts(store, owner, detached_ids,
                        connection=conn, now=now, reason="本机千川 API 配置已清除或应用已切换，请重新添加账户")
                for target in targets:
                    if detach and target.get("account_uid") not in detached_ids:
                        continue
                    capability = metadata(target.get("capability_json"))
                    # Retain collection history, but discard every cached write/access proof.
                    history = {k: v for k, v in capability.items() if k in {
                        "collection_committed_at", "material_backfill", "material_backfill_state",
                        "last_successful_collection_at"}}
                    history.update(authorization_revalidation_required=True,
                                   authorization_identity=dict(current))
                    store.update("promotion_target", {
                        "verification_state": "candidate", "monitor_eligible": 0,
                        "retarget_eligible": 0, "stop_eligible": 0, "last_verified_at": None,
                        "last_status": "authorization_revalidating" if event == "authorization_completed" else "authorization_required",
                        "last_error": "", "last_verification_error": "",
                        "ineligible_reason": "授权已变化，等待重新核验计划访问资格",
                        "capability_json": json.dumps(history, ensure_ascii=False), "next_due_at": now,
                    }, where={"target_uid": target["target_uid"]}, connection=conn)
                # Release persisted old-owner claims, including a queued callback racing a worker.
                store.execute("UPDATE collection_job SET status='queued',lease_owner=NULL,lease_expires_at=NULL,"
                    "fencing_token=fencing_token+1,due_at=?,last_error='',updated_at=? "
                    "WHERE owner_username=? AND status='leased'", (now, now, owner), connection=conn)
                store.execute("UPDATE collection_job SET due_at=?,last_error='' WHERE owner_username=? "
                    "AND status IN ('queued','retry')", (now, owner), connection=conn)
                account_count = store.execute("SELECT COUNT(*) AS n FROM qianchuan_account "
                    "WHERE owner_username=? AND directory_selected=1", (owner,), fetch=True, connection=conn)[0]["n"]
            with api._ACTIVE_LOCK:
                for key in list(_AUTH_BACKOFF):
                    if split_scope(key[0], current)[0] == owner and key[1] != current.get("auth_generation", ""):
                        _AUTH_BACKOFF.pop(key, None)
    except AuthorizationContextChanged:
        return {"status": "superseded", "cancelled_batches": len(affected)}
    api._WAKE.set()
    queued = False
    if event == "authorization_completed" and current.get("app_id") and account_count:
        if notify_catalog is None:
            from services.official_api_catalog import start_official_api_catalog_sync
            notify_catalog = start_official_api_catalog_sync
        # No credential/DB/collector lock is held while scheduling the network reader.
        notify_catalog()
        queued = True
    return {"status": "revalidation_queued", "cancelled_batches": len(affected),
            "auth_backoffs_cleared": cleared, "targets_revalidating": len(targets), "catalog_queued": queued,
            **detached_counts}
