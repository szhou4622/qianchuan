"""Persistent final-state reconciliation for official Qianchuan writes.

POST success only means that the platform accepted the request.  This worker
owns the later read-only verification so a process restart cannot turn a
submitted task into a false success or cause the POST to be repeated.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from services.qianchuan_open_api.audit import OfficialApiAuditStore
from services.qianchuan_open_api.runtime import get_official_api_service
from services.qianchuan_session import current_session_owner
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


_STOP = threading.Event()
_WAKE = threading.Event()
_LOCK = threading.Lock()
_THREAD: Optional[threading.Thread] = None
_LEASE_OWNER = f"reconcile-{uuid.uuid4().hex}"
_LEASE_SECONDS = 90
_MAX_ATTEMPTS = 8


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), default=str)


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _notify_reconciled_auto_stop(
    store: SQLiteStore,
    row: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    status: str,
    error: str = "",
    verified: Optional[Mapping[str, Any]] = None,
    submitted_at: str = "",
    confirmed_at: str = "",
    verification_duration_seconds: Optional[float] = None,
) -> None:
    """Send the terminal notification for an auto-stop reconciliation.

    Confirmation-mode stops already own a local card. Auto stops have no
    ``local_retarget_task`` row, so their submitted result previously became
    silent as soon as it entered background verification.
    """

    aavid = str(row.get("aavid") or "").strip()
    ad_id = str(row.get("ad_id") or "").strip()
    task_id = str(row.get("control_task_id") or data.get("task_id") or "").strip()
    owner = str(row.get("account_username") or "").strip().casefold()
    target_rows = store.execute(
        "SELECT * FROM promotion_target WHERE aadvid=? AND ad_id=? "
        "ORDER BY updated_at DESC,id DESC LIMIT 1",
        (aavid, ad_id),
        fetch=True,
    ) or []
    target = dict(target_rows[0]) if target_rows else {}
    account = store.select_one(
        "qianchuan_account",
        where={"owner_username": owner, "aavid": aavid},
    ) or {}
    assist = (
        store.select_one(
            "pmc_roi2_assist_task",
            where={
                "target_uid": str(target.get("target_uid") or ""),
                "assist_task_id": task_id,
            },
        )
        if target
        else None
    ) or {}
    succeeded = status == "confirmed_succeeded"
    platform_status = str((verified or {}).get("status") or "").strip().upper()
    if succeeded and platform_status == "OFFLINE_TIME":
        message = "调控任务已自然到期"
    elif succeeded:
        message = "官方 API 停投已核验成功"
    elif status == "confirmed_failed":
        message = "官方 API 停投核验失败"
    else:
        message = "官方 API 停投结果无法确认，请人工检查"
    if error:
        message += f"：{str(error)[:500]}"
    expected = str(data.get("expected_status") or "PAUSE").strip().upper()
    from services.regulation_rule_runner import _send_auto_stop_result_notification

    _send_auto_stop_result_notification(
        store,
        owner=owner,
        aavid=aavid,
        account_name=str(account.get("account_name") or ""),
        plan_name=str(target.get("plan_name") or ""),
        ad_id=ad_id,
        promotion_scene=str(
            data.get("promotion_scene")
            or target.get("promotion_scene")
            or "live"
        ),
        plan_system=str(target.get("plan_system") or "unknown"),
        task_name=str(assist.get("task_name") or ""),
        assist_task_id=task_id,
        stop_action="pause" if expected == "PAUSE" else "delete",
        success=succeeded,
        message=message,
        platform_status=platform_status,
        request_id=str(row.get("request_id") or ""),
        submitted_at=submitted_at,
        confirmed_at=confirmed_at,
        verification_duration_seconds=verification_duration_seconds,
        delivery_task_uid=str(row.get("task_uid") or ""),
    )


def enqueue_execution_reconciliation(
    *,
    task_uid: str,
    action_type: str,
    aavid: Any,
    ad_id: Any,
    control_task_id: Any,
    request_id: str,
    request_uid: str,
    idempotency_key: str,
    verify_payload: Mapping[str, Any],
    db: Optional[SQLiteStore] = None,
) -> dict[str, Any]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner = str(current_session_owner() or "").strip().casefold()
    if not owner:
        raise RuntimeError("工具账号已经退出，不能登记官方 API 对账任务")
    stable_key = str(idempotency_key or task_uid or request_uid or control_task_id).strip()
    existing = store.select_one(
        "execution_reconciliation",
        where={"account_username": owner, "idempotency_key": stable_key},
    )
    if isinstance(existing, dict):
        if str(existing.get("status") or "") == "submitting":
            store.execute(
                "UPDATE execution_reconciliation SET task_uid=?,action_type=?,aavid=?,ad_id=?,"
                "control_task_id=?,request_id=?,status='submitted',next_attempt_at=?,"
                "payload_json=?,last_error='',updated_at=? WHERE account_username=? "
                "AND idempotency_key=? AND status='submitting'",
                (
                    str(task_uid or ""),
                    str(action_type or "retarget"),
                    str(aavid or ""),
                    str(ad_id or ""),
                    str(control_task_id or ""),
                    str(request_id or ""),
                    (datetime.now() + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S"),
                    _json({**dict(verify_payload), "request_uid": str(request_uid or "")}),
                    _now(),
                    owner,
                    stable_key,
                ),
            )
            existing = store.select_one(
                "execution_reconciliation",
                where={"account_username": owner, "idempotency_key": stable_key},
            ) or existing
        _WAKE.set()
        return existing
    row = {
        "reconciliation_uid": uuid.uuid4().hex,
        "account_username": owner,
        "task_uid": str(task_uid or ""),
        "action_type": str(action_type or "retarget"),
        "aavid": str(aavid or ""),
        "ad_id": str(ad_id or ""),
        "control_task_id": str(control_task_id or ""),
        "request_id": str(request_id or ""),
        "idempotency_key": stable_key,
        "status": "submitted",
        "next_attempt_at": (datetime.now() + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S"),
        "payload_json": _json(
            {
                **dict(verify_payload),
                "request_uid": str(request_uid or ""),
            }
        ),
    }
    store.insert("execution_reconciliation", row)
    _WAKE.set()
    return row


def reserve_execution_intent(
    *,
    task_uid: str,
    action_type: str,
    aavid: Any,
    ad_id: Any,
    idempotency_key: str,
    verify_payload: Mapping[str, Any],
    control_task_id: Any = "",
    db: Optional[SQLiteStore] = None,
) -> tuple[dict[str, Any], bool]:
    """Persist an immutable write intent immediately before the POST.

    A crash after this reservation is intentionally fail-closed: on restart
    the same idempotency key is not submitted again until an operator or the
    reconciliation flow has resolved the previous attempt.
    """
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner = str(current_session_owner() or "").strip().casefold()
    if not owner:
        raise RuntimeError("工具账号已经退出，不能登记官方 API 写入意图")
    stable_key = str(idempotency_key or task_uid).strip()
    if not stable_key:
        raise RuntimeError("写入任务缺少不可变幂等键")
    existing = store.select_one(
        "execution_reconciliation",
        where={"account_username": owner, "idempotency_key": stable_key},
    )
    if isinstance(existing, dict):
        return existing, False
    row = {
        "reconciliation_uid": uuid.uuid4().hex,
        "account_username": owner,
        "task_uid": str(task_uid or ""),
        "action_type": str(action_type or "retarget"),
        "aavid": str(aavid or ""),
        "ad_id": str(ad_id or ""),
        "control_task_id": str(control_task_id or ""),
        "idempotency_key": stable_key,
        "status": "submitting",
        "next_attempt_at": _now(),
        "payload_json": _json(dict(verify_payload)),
    }
    try:
        store.insert("execution_reconciliation", row)
    except Exception:
        existing = store.select_one(
            "execution_reconciliation",
            where={"account_username": owner, "idempotency_key": stable_key},
        )
        if isinstance(existing, dict):
            return existing, False
        raise
    return row, True


def resolve_stop_idempotency_key(
    base_key: str,
    *,
    allow_confirmed_failed_retry: bool = False,
    db: Optional[SQLiteStore] = None,
) -> str:
    """Return the shared cycle key or a user-authorized failed-attempt suffix."""
    stable = str(base_key or "").strip()
    if not stable:
        raise RuntimeError("停投周期缺少幂等键")
    store = db or SQLiteStore()
    owner = str(current_session_owner() or "").strip().casefold()
    if not owner:
        return stable
    rows = store.execute(
        "SELECT idempotency_key,status FROM execution_reconciliation "
        "WHERE account_username=? AND action_type='stop' "
        "AND (idempotency_key=? OR idempotency_key LIKE ?) ORDER BY id",
        (owner, stable, stable + ":attempt:%"),
        fetch=True,
    ) or []
    if not rows:
        return stable
    blocking = {
        "submitting",
        "submitted",
        "verifying",
        "unknown_requires_review",
        "confirmed_succeeded",
    }
    if any(str(row.get("status") or "") in blocking for row in rows):
        return stable
    if not allow_confirmed_failed_retry or any(
        str(row.get("status") or "") != "confirmed_failed" for row in rows
    ):
        return stable
    next_attempt = 2
    for row in rows:
        key = str(row.get("idempotency_key") or "")
        matched = re.fullmatch(re.escape(stable) + r":attempt:(\d+)", key)
        if matched:
            next_attempt = max(next_attempt, int(matched.group(1)) + 1)
    return f"{stable}:attempt:{next_attempt}"


def finish_execution_intent(
    idempotency_key: str,
    *,
    status: str,
    error: str = "",
    db: Optional[SQLiteStore] = None,
) -> None:
    store = db or SQLiteStore()
    owner = str(current_session_owner() or "").strip().casefold()
    if not owner or not str(idempotency_key or "").strip():
        return
    store.execute(
        "UPDATE execution_reconciliation SET status=?,last_error=?,confirmed_at=?,updated_at=? "
        "WHERE account_username=? AND idempotency_key=? AND status='submitting'",
        (
            str(status or "unknown_requires_review"),
            str(error or "")[:1000],
            _now() if str(status or "").startswith("confirmed_") else None,
            _now(),
            owner,
            str(idempotency_key),
        ),
    )


def _claim_one(store: SQLiteStore, owner: str) -> Optional[dict[str, Any]]:
    now = _now()
    lease_until = (datetime.now() + timedelta(seconds=_LEASE_SECONDS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with store.transaction() as conn:
        store.execute(
            "UPDATE execution_reconciliation SET status='submitted',lease_owner=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE account_username=? "
            "AND status='verifying' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
            (now, owner, now),
            connection=conn,
        )
        rows = store.execute(
            "SELECT * FROM execution_reconciliation WHERE account_username=? "
            "AND status='submitted' AND next_attempt_at<=? ORDER BY next_attempt_at,id LIMIT 1",
            (owner, now),
            fetch=True,
            connection=conn,
        ) or []
        if not rows:
            return None
        row = dict(rows[0])
        token = int(row.get("fencing_token") or 0) + 1
        changed = store.execute(
            "UPDATE execution_reconciliation SET status='verifying',attempt_count=attempt_count+1,"
            "lease_owner=?,lease_expires_at=?,fencing_token=?,updated_at=? "
            "WHERE reconciliation_uid=? AND status='submitted' AND fencing_token=?",
            (
                _LEASE_OWNER,
                lease_until,
                token,
                now,
                str(row.get("reconciliation_uid") or ""),
                int(row.get("fencing_token") or 0),
            ),
            connection=conn,
        )
        if int(changed or 0) != 1:
            return None
        row["fencing_token"] = token
        row["lease_owner"] = _LEASE_OWNER
        row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
        return row


def _finish(
    store: SQLiteStore,
    row: Mapping[str, Any],
    *,
    status: str,
    error: str = "",
    verified: Optional[Mapping[str, Any]] = None,
) -> bool:
    now = _now()
    submitted_at = str(row.get("created_at") or "")
    verification_duration_seconds: Optional[float] = None
    try:
        verification_duration_seconds = max(
            0.0,
            round(
                (
                    datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
                    - datetime.strptime(submitted_at, "%Y-%m-%d %H:%M:%S")
                ).total_seconds(),
                3,
            ),
        )
    except (TypeError, ValueError):
        verification_duration_seconds = None
    verified_status = str((verified or {}).get("status") or "").strip().upper()
    naturally_expired = (
        status == "confirmed_succeeded" and verified_status == "OFFLINE_TIME"
    )
    changed = store.execute(
        "UPDATE execution_reconciliation SET status=?,lease_owner=NULL,lease_expires_at=NULL,"
        "last_error=?,confirmed_at=?,card_update_state='pending',updated_at=? "
        "WHERE reconciliation_uid=? AND status='verifying' AND lease_owner=? AND fencing_token=?",
        (
            status,
            str(error or "")[:1000],
            now if status.startswith("confirmed_") else None,
            now,
            str(row.get("reconciliation_uid") or ""),
            str(row.get("lease_owner") or ""),
            int(row.get("fencing_token") or 0),
        ),
    )
    if int(changed or 0) != 1:
        return False
    data = _payload(row.get("payload_json"))
    is_stop = str(row.get("action_type") or "retarget") == "stop"
    request_uid = str(data.get("request_uid") or "")
    if request_uid:
        OfficialApiAuditStore(store).mark_reconciled(
            request_uid,
            status=("confirmed" if status == "confirmed_succeeded" else "unresolved"),
            task_id=row.get("control_task_id"),
            response=verified or {"verification_error": error},
        )
    task_uid = str(row.get("task_uid") or "")
    run_task_uid = str(data.get("execution_uid") or task_uid)
    if run_task_uid:
        succeeded = status == "confirmed_succeeded"
        if is_stop:
            stop_step = "terminal_natural" if naturally_expired else (
                "confirmed_succeeded" if succeeded else status
            )
            stop_message = (
                "调控任务已自然到期"
                if naturally_expired
                else "官方 API 停投已核验成功"
                if succeeded
                else "官方 API 停投核验失败，请人工检查"
            )
            store.execute(
                "UPDATE pmc_regulation_run SET status=?,execution_state=?,step=?,message=?,detail=?,ended_at=? "
                "WHERE execution_uid=? AND execution_state='submitted_verifying'",
                (
                    2 if naturally_expired else 1 if succeeded else -1,
                    "confirmed_succeeded" if succeeded else status,
                    stop_step,
                    stop_message,
                    str(error or "")[:4000],
                    now,
                    run_task_uid,
                ),
            )
            if succeeded:
                actual_status = verified_status or "DISABLE"
                if actual_status not in {
                    "DISABLE",
                    "DISABLED",
                    "FINISHED",
                    "ENDED",
                    "OFFLINE_TIME",
                }:
                    actual_status = "DISABLE"
                store.execute(
                    "UPDATE pmc_roi2_assist_task SET ad_delivery_type=1,"
                    "ad_delivery_name=?,task_status_source='api',"
                    "task_status_observed_at=?,reconciliation_status='confirmed',updated_at=? "
                    "WHERE aadvid=? AND ad_id=? AND assist_task_id=?",
                    (
                        actual_status,
                        now,
                        now,
                        str(row.get("aavid") or ""),
                        str(row.get("ad_id") or ""),
                        str(row.get("control_task_id") or ""),
                    ),
                )
        else:
            run_update = store.execute(
                "UPDATE pmc_retargeting_run SET status=?,execution_state=?,step=?,message=?,detail=?,ended_at=? "
                "WHERE execution_uid=? AND execution_state='submitted_verifying'",
                (
                    1 if succeeded else -1,
                    "confirmed_succeeded" if succeeded else status,
                    "confirmed_succeeded" if succeeded else status,
                    "官方 API 追投已核验成功" if succeeded else "官方 API 追投核验失败，请人工检查",
                    str(error or "")[:4000],
                    now,
                    run_task_uid,
                ),
            )
            if int(run_update or 0) != 1:
                # r12 and older grouped runs persisted the parent group UID
                # while reconciliation used its concrete attempt UID. Repair
                # those rows without resubmitting the platform write.
                legacy_run_uid = re.sub(r":attempt:\d+$", "", run_task_uid)
                if legacy_run_uid and legacy_run_uid != run_task_uid:
                    store.execute(
                        "UPDATE pmc_retargeting_run SET status=?,execution_state=?,"
                        "step=?,message=?,detail=?,ended_at=? WHERE execution_uid=? "
                        "AND execution_state='submitted_verifying'",
                        (
                            1 if succeeded else -1,
                            "confirmed_succeeded" if succeeded else status,
                            "confirmed_succeeded" if succeeded else status,
                            (
                                "官方 API 追投已核验成功"
                                if succeeded
                                else "官方 API 追投核验失败，请人工检查"
                            ),
                            str(error or "")[:4000],
                            now,
                            legacy_run_uid,
                        ),
                    )
        store.execute(
            "UPDATE account_operation_event SET status=?,summary=?,detail=?,updated_at=? "
            "WHERE cloud_task_id=? AND status='pending'",
            (
                "success" if succeeded else "failed",
                (
                    "官方 API 停投已核验成功"
                    if is_stop and succeeded
                    else "官方 API 追投已核验成功"
                    if succeeded
                    else "官方 API 写入核验失败，请人工检查"
                ),
                str(error or "")[:4000],
                now,
                run_task_uid,
            ),
        )
    if task_uid:
        siblings = store.execute(
            "SELECT status FROM execution_reconciliation WHERE account_username=? AND task_uid=?",
            (str(row.get("account_username") or ""), task_uid),
            fetch=True,
        ) or []
        sibling_statuses = {str(item.get("status") or "") for item in siblings}
        if sibling_statuses.intersection({"submitted", "verifying"}):
            return True
        succeeded = bool(sibling_statuses) and sibling_statuses == {"confirmed_succeeded"}
        aggregate_status = "confirmed_succeeded" if succeeded else "unknown_requires_review"
        label = "停投" if is_stop else "追投"
        terminal_message = (
            "调控任务已自然到期"
            if is_stop and naturally_expired
            else f"官方 API {label}已核验成功"
            if succeeded
            else f"官方 API {label}核验失败，请人工检查"
        )
        final_result = {
            "success": succeeded,
            "step": (
                "terminal_natural"
                if is_stop and naturally_expired
                else aggregate_status
            ),
            "regulate_task_id": str(row.get("control_task_id") or ""),
            "request_id": str(row.get("request_id") or ""),
            "submitted_at": submitted_at,
            "confirmed_at": now,
            "verification_duration_seconds": verification_duration_seconds,
            "platform_status": verified_status,
            "control_cycle_key": str(data.get("control_cycle_key") or ""),
            "verification": dict(verified or {}),
        }
        stop_observer_count = 0
        if is_stop and final_result["control_cycle_key"]:
            try:
                from services.local_feishu_bridge import (
                    finalize_reconciled_local_stop_cycle,
                )

                stop_observer_count = finalize_reconciled_local_stop_cycle(
                    str(row.get("account_username") or ""),
                    final_result["control_cycle_key"],
                    succeeded=succeeded,
                    message=terminal_message,
                    detail=str(error or ""),
                    regulate_task_id=str(row.get("control_task_id") or ""),
                    result=final_result,
                )
            except Exception:
                logger.exception(
                    "官方 API 对账完成后更新停投周期卡片失败 cycle=%s",
                    final_result["control_cycle_key"],
                )
        local_task = store.select_one(
            "local_retarget_task",
            fields="task_uid,action_type",
            where={
                "task_uid": task_uid,
                "account_username": str(row.get("account_username") or ""),
            },
        )
        if local_task and not (is_stop and stop_observer_count):
            try:
                from services.local_feishu_bridge import finalize_reconciled_local_task

                finalize_reconciled_local_task(
                    task_uid,
                    succeeded=succeeded,
                    message=terminal_message,
                    detail=str(error or ""),
                    regulate_task_id=str(row.get("control_task_id") or ""),
                    result=final_result,
                )
            except Exception:
                logger.exception("官方 API 对账完成后更新飞书任务失败 task=%s", task_uid)
        elif is_stop and not stop_observer_count:
            try:
                _notify_reconciled_auto_stop(
                    store,
                    row,
                    data,
                    status=status,
                    error=error,
                    verified=verified,
                    submitted_at=submitted_at,
                    confirmed_at=now,
                    verification_duration_seconds=verification_duration_seconds,
                )
                pending_delivery = store.execute(
                    "SELECT id FROM feishu_outbox WHERE account_username=? "
                    "AND task_uid=? AND status IN ('queued','sending') LIMIT 1",
                    (
                        str(row.get("account_username") or ""),
                        str(row.get("task_uid") or ""),
                    ),
                    fetch=True,
                ) or []
                store.execute(
                    "UPDATE execution_reconciliation SET card_update_state=?,updated_at=? "
                    "WHERE reconciliation_uid=?",
                    (
                        "queued" if pending_delivery else "sent",
                        now,
                        str(row.get("reconciliation_uid") or ""),
                    ),
                )
            except Exception:
                queued_rows = store.execute(
                    "SELECT id FROM feishu_outbox WHERE account_username=? "
                    "AND task_uid=? AND status IN ('queued','sending') LIMIT 1",
                    (
                        str(row.get("account_username") or ""),
                        str(row.get("task_uid") or ""),
                    ),
                    fetch=True,
                ) or []
                store.execute(
                    "UPDATE execution_reconciliation SET card_update_state=?,updated_at=? "
                    "WHERE reconciliation_uid=?",
                    (
                        "queued" if queued_rows else "failed",
                        now,
                        str(row.get("reconciliation_uid") or ""),
                    ),
                )
                logger.exception(
                    "官方 API 自动停投对账完成后发送通知失败 task=%s",
                    task_uid,
                )
    return True


def _retry(store: SQLiteStore, row: Mapping[str, Any], error: str) -> None:
    attempts = int(row.get("attempt_count") or 0)
    if attempts >= _MAX_ATTEMPTS:
        _finish(store, row, status="unknown_requires_review", error=error)
        return
    delay = min(300, 2 ** min(attempts, 8))
    next_at = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
    store.execute(
        "UPDATE execution_reconciliation SET status='submitted',next_attempt_at=?,"
        "lease_owner=NULL,lease_expires_at=NULL,last_error=?,updated_at=? "
        "WHERE reconciliation_uid=? AND status='verifying' AND lease_owner=? AND fencing_token=?",
        (
            next_at,
            str(error or "")[:1000],
            _now(),
            str(row.get("reconciliation_uid") or ""),
            str(row.get("lease_owner") or ""),
            int(row.get("fencing_token") or 0),
        ),
    )


def _verify_one(store: SQLiteStore, row: Mapping[str, Any]) -> None:
    row = dict(row)
    data = _payload(row.get("payload_json"))
    try:
        action_type = str(row.get("action_type") or "retarget")
        if action_type == "retarget":
            from services.official_api_execution import _verify_control_task

            service = get_official_api_service()
            task_id = str(row.get("control_task_id") or "").strip()
            if not task_id:
                # A transient POST response can omit the created task ID even
                # though the downstream service completed the write.  Search
                # only by the immutable execution name, frozen material group
                # and parameters; never resubmit the POST.
                found = service.find_duplicate_control_task(
                    row.get("aavid"),
                    ad_id=row.get("ad_id"),
                    marketing_goal=(
                        "LIVE_PROM_GOODS"
                        if str(data.get("promotion_scene") or "").lower() == "live"
                        else "VIDEO_PROM_GOODS"
                    ),
                    task_name=str(data.get("task_name") or ""),
                    budget=data.get("budget"),
                    duration=data.get("duration") or None,
                    material_ids=data.get("material_ids") or [],
                )
                task_id = str((found or {}).get("task_id") or "").strip()
                if not task_id:
                    raise RuntimeError("平台暂未查询到本次追投任务，稍后继续核验")
                changed = store.execute(
                    "UPDATE execution_reconciliation SET control_task_id=?,updated_at=? "
                    "WHERE reconciliation_uid=? AND status='verifying' "
                    "AND lease_owner=? AND fencing_token=?",
                    (
                        task_id,
                        _now(),
                        str(row.get("reconciliation_uid") or ""),
                        str(row.get("lease_owner") or ""),
                        int(row.get("fencing_token") or 0),
                    ),
                )
                if int(changed or 0) != 1:
                    raise RuntimeError("对账任务租约已经变化，本轮结果已丢弃")
                row["control_task_id"] = task_id
            verified = _verify_control_task(
                service,
                aavid=row.get("aavid"),
                ad_id=row.get("ad_id"),
                promotion_scene=data.get("promotion_scene"),
                task_id=task_id,
                material_ids=data.get("material_ids") or [],
                budget=data.get("budget"),
                duration=data.get("duration"),
            )
        elif action_type == "stop":
            from services.official_api_execution import (
                _find_control_task,
                classify_stop_status,
            )

            verified = _find_control_task(
                get_official_api_service(),
                aavid=row.get("aavid"),
                ad_id=row.get("ad_id"),
                promotion_scene=data.get("promotion_scene"),
                task_id=row.get("control_task_id"),
            )
            if not verified:
                raise RuntimeError("官方 API 暂未返回待核验的调控任务")
            expected = str(data.get("expected_status") or "PAUSE").upper()
            actual = str(verified.get("status") or "").upper()
            stop_outcome, stop_message = classify_stop_status(expected, actual)
            if stop_outcome == "pending":
                raise RuntimeError(stop_message)
            verified = dict(verified)
            verified["verification_outcome"] = stop_outcome
            verified["verification_message"] = stop_message
            if stop_outcome == "terminal_natural":
                data["verification_note"] = stop_message
        else:
            raise RuntimeError("暂不支持的对账动作")
    except Exception as exc:
        _retry(store, row, str(exc))
        return
    _finish(
        store,
        row,
        status="confirmed_succeeded",
        error=str(data.get("verification_note") or ""),
        verified=verified,
    )


def _loop() -> None:
    init_sqlite_schema()
    store = SQLiteStore()
    while not _STOP.is_set():
        owner = str(current_session_owner() or "").strip().casefold()
        if not owner:
            _WAKE.wait(2)
            _WAKE.clear()
            continue
        row = _claim_one(store, owner)
        if row:
            _verify_one(store, row)
            continue
        _WAKE.wait(2)
        _WAKE.clear()


def start_official_api_reconciliation_background_thread() -> threading.Thread:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_loop,
            name="official-api-reconciliation",
            daemon=True,
        )
        _THREAD.start()
        return _THREAD


def stop_official_api_reconciliation_background_thread(timeout: float = 8.0) -> None:
    _STOP.set()
    _WAKE.set()
    thread = _THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.1, float(timeout)))


def get_execution_reconciliation_health(db: Optional[SQLiteStore] = None) -> dict[str, Any]:
    store = db or SQLiteStore()
    owner = str(current_session_owner() or "").strip().casefold()
    rows = store.execute(
        "SELECT status,COUNT(*) AS count FROM execution_reconciliation "
        "WHERE account_username=? GROUP BY status",
        (owner,),
        fetch=True,
    ) or []
    return {
        "running": bool(_THREAD and _THREAD.is_alive()),
        "counts": {str(row.get("status")): int(row.get("count") or 0) for row in rows},
    }
