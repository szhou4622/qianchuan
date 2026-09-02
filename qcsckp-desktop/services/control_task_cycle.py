"""Resume-aware idempotency for control-task stop operations."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Mapping, Optional


_RESUME_TRANSITIONS = ("已暂停 -> 调控中", "已暂停 → 调控中")
_ACTIVE_STATUSES = {"PROCESSING", "ENABLE", "ENABLED", "ACTIVE", "RUNNING", "DELIVERING"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> Optional[datetime]:
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _cycle_key(target_uid: str, task_id: str, marker: str) -> str:
    return hashlib.sha256(
        f"{target_uid}|{task_id}|{marker}".encode("utf-8")
    ).hexdigest()


def stop_cycle_execution_key(
    owner: Any,
    aavid: Any,
    ad_id: Any,
    assist_task_id: Any,
    cycle_key: Any,
    action: Any,
) -> str:
    """Return one shared idempotency namespace for every actor in a cycle."""
    return hashlib.sha256(
        "|".join(
            (
                _text(owner).casefold(),
                _text(aavid),
                _text(ad_id),
                _text(assist_task_id),
                _text(cycle_key),
                _text(action).lower(),
            )
        ).encode("utf-8")
    ).hexdigest()


def _latest_successful_stop(
    db: Any,
    target_uid: str,
    task_id: str,
    *,
    connection: Any = None,
) -> dict[str, Any]:
    rows = db.execute(
        "SELECT execution_uid,ended_at,created_at FROM pmc_regulation_run "
        "WHERE target_uid=? AND assist_task_id=? AND "
        "(status IN (1,2) OR execution_state='confirmed_succeeded' "
        "OR step='confirmed_succeeded') "
        "ORDER BY COALESCE(NULLIF(ended_at,''),created_at) DESC,id DESC LIMIT 1",
        (target_uid, task_id),
        fetch=True,
        connection=connection,
    ) or []
    run = dict(rows[0]) if rows else {}

    reconciliations = db.execute(
        "SELECT e.reconciliation_uid,e.confirmed_at,e.created_at "
        "FROM execution_reconciliation e JOIN promotion_target p "
        "ON p.ad_id=e.ad_id AND p.aadvid=e.aavid "
        "WHERE p.target_uid=? AND e.control_task_id=? "
        "AND e.action_type='stop' AND e.status='confirmed_succeeded' "
        "ORDER BY COALESCE(NULLIF(e.confirmed_at,''),e.created_at) DESC,e.id DESC LIMIT 1",
        (target_uid, task_id),
        fetch=True,
        connection=connection,
    ) or []
    reconciliation = dict(reconciliations[0]) if reconciliations else {}

    candidates = []
    if run:
        completed_at = _text(run.get("ended_at") or run.get("created_at"))
        candidates.append(
            {
                "completed_at": completed_at,
                "completed_dt": _time(completed_at),
                "marker": _text(run.get("execution_uid")) or completed_at,
            }
        )
    if reconciliation:
        completed_at = _text(
            reconciliation.get("confirmed_at") or reconciliation.get("created_at")
        )
        candidates.append(
            {
                "completed_at": completed_at,
                "completed_dt": _time(completed_at),
                "marker": _text(reconciliation.get("reconciliation_uid")) or completed_at,
            }
        )
    valid = [item for item in candidates if item.get("completed_dt") is not None]
    if valid:
        return max(valid, key=lambda item: item["completed_dt"])
    return candidates[0] if candidates else {}


def _latest_resume_event(
    db: Any,
    target_uid: str,
    task_id: str,
    stop_completed_at: str,
    *,
    connection: Any = None,
) -> dict[str, Any]:
    rows = db.execute(
        "SELECT event_uid,platform_event_id,occurred_at,summary,action_type "
        "FROM account_operation_event WHERE target_uid=? AND regulate_task_id=? "
        "AND source IN ('qianchuan_open_api','platform_log') "
        "AND status='success' AND occurred_at>=? AND (action_type='control_resume' "
        "OR summary LIKE ? OR summary LIKE ?) "
        "ORDER BY occurred_at DESC,id DESC LIMIT 1",
        (
            target_uid,
            task_id,
            stop_completed_at,
            f"%{_RESUME_TRANSITIONS[0]}%",
            f"%{_RESUME_TRANSITIONS[1]}%",
        ),
        fetch=True,
        connection=connection,
    ) or []
    return dict(rows[0]) if rows else {}


def stop_cycle_state(
    db: Any,
    target_uid: Any,
    assist_task_id: Any,
    *,
    assist_row: Optional[Mapping[str, Any]] = None,
    observed_at: Any = "",
    connection: Any = None,
) -> dict[str, Any]:
    """Return the current stop cycle and whether another stop must be blocked.

    A successful stop remains authoritative until an exact official operation
    log proves that the same task was resumed.  The resumed cycle becomes
    actionable only after a fresh official active-task observation made after
    that resume event.
    """

    target = _text(target_uid) or "legacy_unscoped"
    task_id = _text(assist_task_id)
    initial_key = _cycle_key(target, task_id, "initial")
    if not task_id:
        return {
            "cycle_key": initial_key,
            "blocked": True,
            "reason": "missing_task_id",
            "stop_completed_at": "",
            "resume_at": "",
            "resume_event_uid": "",
        }

    completed = _latest_successful_stop(
        db,
        target,
        task_id,
        connection=connection,
    )
    stop_at = _text(completed.get("completed_at"))
    if not stop_at:
        return {
            "cycle_key": initial_key,
            "blocked": False,
            "reason": "initial_cycle",
            "stop_completed_at": "",
            "resume_at": "",
            "resume_event_uid": "",
        }

    resume = _latest_resume_event(
        db,
        target,
        task_id,
        stop_at,
        connection=connection,
    )
    if not resume:
        return {
            "cycle_key": _cycle_key(target, task_id, f"stopped:{completed.get('marker') or stop_at}"),
            "blocked": True,
            "reason": "stop_already_completed",
            "stop_completed_at": stop_at,
            "resume_at": "",
            "resume_event_uid": "",
        }

    resume_at = _text(resume.get("occurred_at"))
    resume_uid = _text(resume.get("platform_event_id") or resume.get("event_uid"))
    resumed_key = _cycle_key(target, task_id, f"resume:{resume_uid or resume_at}")
    row = dict(assist_row or {})
    if not row or any(
        key not in row
        for key in (
            "ad_delivery_type",
            "ad_delivery_name",
            "task_status_source",
            "task_status_observed_at",
            "data_source",
            "updated_at",
        )
    ):
        persisted = db.select_one(
            "pmc_roi2_assist_task",
            where={"target_uid": target, "assist_task_id": task_id},
            connection=connection,
        ) or {}
        row = {**persisted, **row}
    row_observed_at = _text(
        row.get("task_status_observed_at") or observed_at or row.get("updated_at")
    )
    active = _text(row.get("ad_delivery_type") if row.get("ad_delivery_type") is not None else "0") in {"", "0"}
    official = _text(row.get("data_source")) == "qianchuan_open_api"
    explicit_status = _text(row.get("task_status_source")) == "api"
    platform_active = _text(row.get("ad_delivery_name")).upper() in _ACTIVE_STATUSES
    fresh_after_resume = bool(
        _time(row_observed_at)
        and _time(resume_at)
        and _time(row_observed_at) > _time(resume_at)
    )
    if (
        not row
        or not active
        or not official
        or not explicit_status
        or not platform_active
        or not fresh_after_resume
    ):
        return {
            "cycle_key": resumed_key,
            "blocked": True,
            "reason": "resume_waiting_fresh_collection",
            "stop_completed_at": stop_at,
            "resume_at": resume_at,
            "resume_event_uid": resume_uid,
        }
    return {
        "cycle_key": resumed_key,
        "blocked": False,
        "reason": "resumed_cycle_ready",
        "stop_completed_at": stop_at,
        "resume_at": resume_at,
        "resume_event_uid": resume_uid,
    }
