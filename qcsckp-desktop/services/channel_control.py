"""Explicit, read-only platform verification before enabling channel writes."""
from __future__ import annotations

import time

from channel_runtime import atomic_json, layout, switch_state
from release_identity import IDENTITY, DISPLAY_VERSION


def status():
    from services.diagnostics import diagnostic_status
    return {"success": True, "release": IDENTITY, "display_version": DISPLAY_VERSION,
            "switch": switch_state(), "diagnostics": diagnostic_status()}


def verify_and_resume():
    from services.qianchuan_session import current_session_owner
    from services.qianchuan_open_api.runtime import get_official_api_service
    from services.official_api_execution import _control_window
    from utils.sqlite_store import SQLiteStore, init_sqlite_schema
    owner = str(current_session_owner() or "").strip().casefold()
    if not owner:
        return {"success": False, "message": "请先完成本机千川账户授权"}
    init_sqlite_schema()
    db = SQLiteStore()
    unresolved = db.execute(
        "SELECT COUNT(*) AS n FROM execution_reconciliation WHERE "
        "status NOT IN ('confirmed_succeeded','confirmed_failed')", fetch=True)
    if unresolved and unresolved[0]["n"]:
        return {"success": False, "message": "仍有提交结果未核清的任务，请先完成写入对账；不会重复提交"}
    targets = db.execute("SELECT p.* FROM promotion_target p JOIN qianchuan_account a "
                         "ON a.account_uid=p.account_uid WHERE a.owner_username=? AND p.enabled=1",
                         (owner,), fetch=True) or []
    service = get_official_api_service()
    start, end = _control_window()
    for target in targets:
        detail, _ = service.get_plan_detail(target["aadvid"], target["ad_id"])
        if str(detail.get("ad_id")) != str(target["ad_id"]):
            raise RuntimeError("平台计划身份核验不一致，切版保护未解除")
        service.list_control_tasks(target["aadvid"], ad_id=target["ad_id"],
            marketing_goal="LIVE_PROM_GOODS" if target.get("promotion_scene") == "live" else "VIDEO_PROM_GOODS",
            start_time=start, end_time=end)
    state = switch_state()
    state.update(pending=False, verified_at=time.time(), verified_targets=len(targets))
    atomic_json(layout().profile / "switch-state.json", state)
    return {"success": True, "message": "平台状态核验完成。请到策略页手动恢复需要的自动投放；历史确认指令不会重放。"}
