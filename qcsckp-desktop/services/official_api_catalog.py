"""使用千川官方 API 刷新现有 v0.1.46 账户与四类计划目录。"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any, Optional

from api.promotion_targets import (
    get_promotion_target,
    list_promotion_targets,
    patch_target_sync_state,
    upsert_promotion_target,
)
from services.qianchuan_accounts import (
    _owner_key,
    ensure_qianchuan_account,
    get_qianchuan_account,
    list_qianchuan_accounts,
)
from services.qianchuan_catalog import (
    catalog_sync_status,
    finalize_catalog_sync,
    mark_catalog_sync_progress,
    mark_catalog_sync_started,
)
from services.qianchuan_open_api.errors import OfficialApiNotConfigured
from services.qianchuan_open_api.runtime import get_official_api_service
from services.promotion_browser_lock import (
    PRIORITY_COLLECTION,
    exclusive_qianchuan_operation,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


_LOCK = threading.Lock()
_THREAD: Optional[threading.Thread] = None
_PENDING_ACCOUNT_UIDS: set[str] = set()
_PENDING_ALL = False
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_THREAD: Optional[threading.Thread] = None
_SCHEDULER_STOP = threading.Event()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def official_api_session_status() -> dict[str, Any]:
    try:
        bundle = get_official_api_service().client.token_provider.get_token()
        return {
            "available": bool(bundle.access_token),
            "status": "available" if bundle.access_token else "not_configured",
            "source": "qianchuan_open_api",
            "last_error": "",
            "message": "千川官方 API 令牌已就绪" if bundle.access_token else "千川官方 API 尚未配置",
        }
    except OfficialApiNotConfigured as exc:
        return {
            "available": False,
            "status": "not_configured",
            "source": "qianchuan_open_api",
            "last_error": str(exc),
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "available": False,
            "status": "token_error",
            "source": "qianchuan_open_api",
            "last_error": str(exc),
            "message": "千川官方 API 授权已失效，请重新授权",
        }


def discover_authorized_accounts() -> dict[str, Any]:
    """只返回候选，不自动把全部授权账户加入用户目录。"""
    rows, evidence = get_official_api_service().list_business_accounts()
    candidates = [
        {
            "aavid": str(row.get("advertiser_id") or ""),
            "account_name": str(row.get("advertiser_name") or ""),
            "role": str(row.get("role") or ""),
            "shop_id": str(row.get("shop_id") or ""),
        }
        for row in rows
        if str(row.get("advertiser_id") or "").strip()
    ]
    complete = bool(evidence.get("complete"))
    if candidates and complete:
        message = "已取得官方授权的千川投放账户，请选择一个账户添加到工具"
    elif candidates:
        message = "已取得部分千川投放账户；未解析的操作主体不会作为账户显示，可先选择已确认账户或稍后重试"
    else:
        message = "尚未解析到可投放的千川账户，请稍后重试；店铺或企业操作主体不会直接作为千川账户添加"
    return {
        "success": True,
        "mode": "official_api",
        "requires_selection": True,
        "complete": complete,
        "evidence": evidence,
        "candidates": candidates,
        "message": message,
    }


def add_authorized_account(aavid: Any) -> dict[str, Any]:
    """Add exactly one user-selected official API account, then warm its catalog."""
    aid = str(aavid or "").strip()
    if not aid.isdigit():
        return {"success": False, "code": "invalid_aavid", "message": "请选择有效的千川账户"}

    rows, evidence = get_official_api_service().list_business_accounts()
    selected = next(
        (row for row in rows if str(row.get("advertiser_id") or "").strip() == aid),
        None,
    )
    if selected is None:
        return {
            "success": False,
            "code": "account_not_authorized",
            "message": "该账户不在当前官方 API 授权范围内，请重新授权后再试",
            "evidence": evidence,
        }

    account = ensure_qianchuan_account(
        aid,
        account_name=str(selected.get("advertiser_name") or ""),
        owner_username=_owner_key(),
        directory_selected=True,
        seen=True,
        allow_reactivate_removed=True,
    )
    sync = start_official_api_catalog_sync(account.get("account_uid") or aid)
    return {
        "success": True,
        "mode": "official_api",
        "account": account,
        "catalog_sync": sync,
        "message": "账户已添加，正在通过千川官方 API 后台读取计划",
    }


def _capability(plan: dict[str, Any], target_uid: str) -> dict[str, Any]:
    common = {
        "source": "qianchuan_open_api",
        "api_contract_version": "official-open-api-v1",
        "api_verified_at": _now(),
        "retarget_supported": True,
        "stop_supported": True,
        "retarget_execute": True,
        "regulation_execute": True,
        "retarget_scene": plan.get("promotion_scene"),
        "retarget_plan_system": plan.get("plan_system"),
        "retarget_probe_version": "official-open-api-v1",
        "retarget_verified_at": _now(),
        "retarget_target_uid": target_uid,
        "retarget_aavid": plan.get("aavid"),
        "retarget_ad_id": plan.get("ad_id"),
        "retarget_batch_execute": True,
        "retarget_batch_probe_version": "official-open-api-v1",
        "retarget_batch_verified_at": _now(),
        "regulation_scene": plan.get("promotion_scene"),
        "regulation_plan_system": plan.get("plan_system"),
        "regulation_probe_version": "official-open-api-v1",
        "regulation_verified_at": _now(),
        "regulation_target_uid": target_uid,
        "regulation_aavid": plan.get("aavid"),
        "regulation_ad_id": plan.get("ad_id"),
    }
    return common


def _sync_account(account: dict[str, Any], db: SQLiteStore) -> dict[str, Any]:
    service = get_official_api_service()
    aavid = str(account.get("aavid") or "")
    business_accounts, account_evidence = service.list_business_accounts()
    authorized = {str(row.get("advertiser_id") or ""): row for row in business_accounts}
    official_account = authorized.get(aavid)
    if not official_account:
        raise RuntimeError("该账户不在当前官方 API 授权链中")
    if official_account:
        ensure_qianchuan_account(
            aavid,
            account_name=official_account.get("advertiser_name") or account.get("account_name") or "",
            owner_username=account.get("owner_username"),
            directory_selected=True,
            seen=True,
            db=db,
        )

    plans, evidence = service.list_all_plans(aavid)
    evidence["account_chain"] = account_evidence
    if not account_evidence.get("complete"):
        evidence["complete"] = False
    existing = {
        str(row.get("ad_id") or ""): row
        for row in list_promotion_targets(owner_username=account.get("owner_username"), db=db)
        if str(row.get("account_uid") or "") == str(account.get("account_uid") or "")
    }
    seen: set[str] = set()
    class_counts = {
        "global_live": 0,
        "global_product": 0,
        "chengfang_live": 0,
        "chengfang_product": 0,
    }
    detail_evidence: dict[str, dict[str, Any]] = {}
    for plan in plans:
        ad_id = str(plan.get("ad_id") or "")
        if not ad_id:
            continue
        try:
            detail, detail_response = service.get_plan_detail(aavid, ad_id)
            if (
                str(detail.get("aavid") or "") != aavid
                or str(detail.get("ad_id") or "") != ad_id
            ):
                raise RuntimeError("计划详情账户或计划 ID 不一致")
            list_plan = plan
            plan = {**list_plan, **detail}
            # The current detail response uses numeric ``adlab_scene=0`` for
            # some full-domain plans, while the list response carries the
            # explicit ``UNI_PROJECT/OVERALL_PROJECT`` contract.  An unknown
            # detail value must not overwrite a confirmed list classification.
            if str(detail.get("plan_system") or "") not in {"global", "chengfang"}:
                plan["plan_system"] = list_plan.get("plan_system") or "unknown"
                plan["adlab_scene"] = list_plan.get("adlab_scene") or ""
            if str(detail.get("promotion_scene") or "") not in {"live", "product"}:
                plan["promotion_scene"] = list_plan.get("promotion_scene") or ""
                plan["marketing_goal"] = list_plan.get("marketing_goal") or ""
            detail_evidence[ad_id] = {
                "complete": True,
                "request_id": detail_response.request_id,
            }
        except Exception as exc:
            detail_evidence[ad_id] = {
                "complete": False,
                "error": str(exc),
            }
        scene = str(plan.get("promotion_scene") or "")
        system = str(plan.get("plan_system") or "unknown")
        seen.add(ad_id)
        if f"{system}_{scene}" in class_counts:
            class_counts[f"{system}_{scene}"] += 1
        prior = existing.get(ad_id) or {}
        # The official paginated list is the catalog source of truth. Detail
        # evidence strengthens execution eligibility, but a transient detail
        # failure must not hide a valid plan or mark the directory partial.
        verified = bool(
            scene in {"live", "product"}
            and system in {"global", "chengfang"}
        )
        saved = upsert_promotion_target(
            {
                "aavid": aavid,
                "ad_id": ad_id,
                "account_name": (official_account or {}).get("advertiser_name") or account.get("account_name") or "",
                "plan_name": plan.get("plan_name") or prior.get("plan_name") or "",
                "promotion_scene": scene or prior.get("promotion_scene") or "product",
                "plan_system": system,
                "platform_status": plan.get("platform_status") or "unknown",
                "verification_state": "verified" if verified else "candidate",
                "verification_evidence_fresh": True,
                "enabled": bool(prior.get("enabled")),
                "last_status": "api_catalog_synced",
                "last_error": "" if verified else "官方 API 返回的计划体系或推广方式不完整",
            },
            owner_username=account.get("owner_username"),
            trusted_catalog=True,
            db=db,
        )
        if verified and detail_evidence[ad_id].get("complete"):
            patch_target_sync_state(
                saved["target_uid"],
                status="api_catalog_synced",
                error="",
                synced=False,
                capability_updates=_capability(plan, saved["target_uid"]),
                db=db,
            )

    # 只有两类 marketing_goal 的全部分页都成功时，才把本轮未见计划标为历史；
    # 部分失败时保留上次完整目录，绝不以异常空结果覆盖。
    if evidence.get("complete"):
        for ad_id, prior in existing.items():
            if ad_id in seen:
                continue
            db.update(
                "promotion_target",
                {
                    "verification_state": "missing",
                    "monitor_eligible": 0,
                    "retarget_eligible": 0,
                    "stop_eligible": 0,
                    "ineligible_reason": "本轮官方 API 完整目录中未发现该计划，保留历史只读记录",
                    "capacity_state": "disabled",
                    "last_status": "missing_from_api_catalog",
                },
                where={"target_uid": prior.get("target_uid")},
            )
    evidence["counts"] = class_counts
    evidence["plan_count"] = len(seen)
    evidence["details"] = detail_evidence
    evidence["error"] = "; ".join(
        str(item.get("error") or "")
        for item in (evidence.get("classes") or {}).values()
        if isinstance(item, dict) and item.get("error")
    )
    return evidence


def run_catalog_sync(account_uid: Any = "", *, db: Optional[SQLiteStore] = None) -> dict[str, Any]:
    store = db or SQLiteStore()
    init_sqlite_schema(database=store.config.get("database"))
    owner = _owner_key()
    requested_uid = str(account_uid or "").strip()
    accounts = list_qianchuan_accounts(owner_username=owner, db=store)
    if requested_uid:
        accounts = [row for row in accounts if str(row.get("account_uid") or "") == requested_uid]
    accounts = [row for row in accounts if row.get("directory_selected")]
    if not accounts:
        return finalize_catalog_sync(
            owner_username=owner,
            refreshed_account_uids=[],
            account_results={},
            error="尚未添加需要同步的千川账户",
            db=store,
        )
    results: dict[str, dict[str, Any]] = {}
    complete: list[str] = []
    for index, account in enumerate(accounts, 1):
        uid = str(account.get("account_uid") or "")
        mark_catalog_sync_progress(
            processed_accounts=index - 1,
            total_accounts=len(accounts),
            current_account=account.get("account_name") or account.get("aavid"),
            message=f"正在通过千川官方 API 同步 {account.get('account_name') or account.get('aavid')}",
        )
        try:
            with exclusive_qianchuan_operation(
                f"官方API目录:{uid}",
                priority=PRIORITY_COLLECTION,
            ):
                result = _sync_account(account, store)
            results[uid] = result
            if result.get("complete"):
                complete.append(uid)
        except Exception as exc:
            results[uid] = {"complete": False, "error": str(exc), "classes": {}}
            store.update(
                "qianchuan_account",
                {"last_status": "api_sync_failed", "last_error": str(exc)[:2000]},
                where={"account_uid": uid},
            )
    return finalize_catalog_sync(
        owner_username=owner,
        complete_account_uids=complete,
        account_results=results,
        refreshed_account_uids=[str(row.get("account_uid") or "") for row in accounts],
        db=store,
    )


def _next_pending_scope() -> tuple[bool, str]:
    global _PENDING_ALL, _THREAD
    with _LOCK:
        if _PENDING_ALL:
            _PENDING_ALL = False
            _PENDING_ACCOUNT_UIDS.clear()
            return True, ""
        if _PENDING_ACCOUNT_UIDS:
            return True, _PENDING_ACCOUNT_UIDS.pop()
        # Clear ownership while holding the same lock used by enqueue callers.
        # This closes the race where a request could be queued just as the old
        # worker was exiting and then never be consumed.
        _THREAD = None
        return False, ""


def _thread_entry(account_uid: str) -> None:
    global _THREAD
    scope = str(account_uid or "").strip()
    try:
        while True:
            try:
                run_catalog_sync(scope)
            except Exception as exc:
                # Never leave the account page in a permanent ``syncing`` state
                # after an unexpected worker failure.
                finalize_catalog_sync(
                    owner_username=_owner_key(),
                    refreshed_account_uids=[],
                    account_results={},
                    error=str(exc),
                )
            has_pending, scope = _next_pending_scope()
            if not has_pending:
                break
            mark_catalog_sync_started(
                owner_username=_owner_key(), account_uid=scope
            )
    finally:
        with _LOCK:
            if _THREAD is threading.current_thread():
                _THREAD = None


def start_official_api_catalog_sync(account_uid: Any = "") -> dict[str, Any]:
    global _THREAD, _PENDING_ALL
    uid = str(account_uid or "").strip()
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            if uid:
                _PENDING_ACCOUNT_UIDS.add(uid)
            else:
                _PENDING_ALL = True
                _PENDING_ACCOUNT_UIDS.clear()
            return {
                "success": True,
                "running": True,
                "queued": True,
                "message": "千川官方 API 目录正在同步，本次刷新已加入队列",
            }
        mark_catalog_sync_started(owner_username=_owner_key(), account_uid=uid)
        _THREAD = threading.Thread(target=_thread_entry, args=(uid,), name="official-api-catalog", daemon=True)
        _THREAD.start()
    return {"success": True, "running": True, "mode": "official_api", "message": "已开始通过千川官方 API 同步账户计划"}


def official_api_catalog_status() -> dict[str, Any]:
    result = catalog_sync_status(owner_username=_owner_key())
    result["backend"] = "official_api"
    return result


def _catalog_scheduler_loop(interval_seconds: int) -> None:
    # 软件启动时先对用户主动添加的账户刷新一次；之后每30分钟增量刷新。
    while not _SCHEDULER_STOP.is_set():
        try:
            start_official_api_catalog_sync()
        except Exception:
            # 单次失败不停止后续调度；详细错误由目录状态记录。
            pass
        _SCHEDULER_STOP.wait(max(60, int(interval_seconds)))


def start_official_api_catalog_scheduler(interval_seconds: int = 1800) -> threading.Thread:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return _SCHEDULER_THREAD
        _SCHEDULER_STOP.clear()
        _SCHEDULER_THREAD = threading.Thread(
            target=_catalog_scheduler_loop,
            args=(interval_seconds,),
            name="qianchuan-official-api-catalog-scheduler",
            daemon=True,
        )
        _SCHEDULER_THREAD.start()
        return _SCHEDULER_THREAD


def stop_official_api_catalog_scheduler() -> None:
    _SCHEDULER_STOP.set()
