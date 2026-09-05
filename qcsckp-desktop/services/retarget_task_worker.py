# -*- coding: utf-8 -*-
"""领取飞书已批准的追投任务，复核后调用现有自动追投服务。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import traceback
from collections import Counter
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from api.dashboard import DashboardApi
from api.operation_events import prune_operation_events
from api.rule_retargeting_config import evaluate_trigger, load_rule_retargeting_config
from config import DATA_DIR
from config import QIANCHUAN_BACKEND
from services.cloud_retarget_client import pull_retarget_task, report_retarget_task
from services.local_test_guard import (
    assert_test_task_scope,
    consume_live_retarget_batch_once,
)
from services.retargeting_rule_runner import (
    _insert_run,
    _interval_from_root_cfg,
    _interval_window_and_max,
    rate_limit_remaining_capacity,
    rate_limit_record_success,
    rate_limit_should_skip,
    rate_limit_strategy_remaining_capacity,
    rate_limit_strategy_record_success,
    rate_limit_strategy_should_skip,
    block_target_after_rate_record_failure,
    resolve_ad_id_for_aavid,
)
from services.retargeting_service import QianChuanRetargetingService
from services.product_rule_engine import evaluate_product_strategy
from services.plan_system import normalize_plan_system
from services.promotion_capability import check_target_capability
from services.promotion_browser_lock import exclusive_browser_operation
from services.retarget_budget_increase import (
    assist_task_sync_ready,
    budget_increase_fingerprint,
    calculate_budget_increase,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


# Local Feishu callbacks and this worker run in the same desktop process.  A
# one-second idle poll keeps click-to-claim latency predictable without network
# traffic; the official API is only called after a task has been claimed.
POLL_SECONDS = 1
MAX_REVALIDATION_AGE_SECONDS = 10 * 60
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: Optional[threading.Thread] = None
_WORKER_STOP = threading.Event()
_LEASE_CANCEL_EVENT = ContextVar("retarget_lease_cancel_event", default=None)


def _assert_execution_not_cancelled() -> str:
    event = _LEASE_CANCEL_EVENT.get()
    if event is not None and (event.is_set() or _WORKER_STOP.is_set()):
        raise RuntimeError("任务领取权已失效，本次未继续提交")
    return ""


class RetargetTaskInvalidated(RuntimeError):
    """The frozen card no longer matches the user's saved strategy."""


def _strategy_invalidated_result(exc: BaseException) -> Dict[str, Any]:
    return {
        "success": False,
        "invalidated": True,
        "message": (
            "策略已更新，本卡失效，未向千川提交；"
            "请使用最新策略生成的新提醒"
        ),
        "detail": str(exc or "追投策略已更新"),
        "step": "strategy_invalidated",
    }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_is_fresh(
    value: Any,
    *,
    max_age_seconds: int = MAX_REVALIDATION_AGE_SECONDS,
) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        observed_at = datetime.strptime(
            text.replace("T", " ")[:19],
            "%Y-%m-%d %H:%M:%S",
        )
    except (TypeError, ValueError):
        return False
    age_seconds = (datetime.now() - observed_at).total_seconds()
    return -300 <= age_seconds <= max(1, int(max_age_seconds))


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return "{}"


def _safe_plan_system(value: Any) -> str:
    try:
        return normalize_plan_system(value or "unknown")
    except ValueError:
        return "unknown"


def _strategy_snapshot(strategy: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(strategy.get("id") or ""),
        "title": str(strategy.get("title") or strategy.get("id") or "?")[:64],
        "account_uid": str(strategy.get("account_uid") or ""),
        "target_uid": str(strategy.get("target_uid") or ""),
        "trigger_level": str(strategy.get("trigger_level") or "material"),
        "product_filter": strategy.get("product_filter") if isinstance(strategy.get("product_filter"), list) else [],
        "candidate_trigger": (
            strategy.get("candidate_trigger")
            if isinstance(strategy.get("candidate_trigger"), dict)
            else {}
        ),
        "candidate_sort": str(strategy.get("candidate_sort") or "net_roi_desc"),
        "candidate_limit": int(strategy.get("candidate_limit") or 1),
        "material_grouping_mode": (
            "merged"
            if str(strategy.get("material_grouping_mode") or "separate").strip().lower()
            == "merged"
            else "separate"
        ),
        "action_mode": str(strategy.get("action_mode") or "card_confirm"),
        "task_action": str(strategy.get("task_action") or "create_retarget"),
        "trigger": strategy.get("trigger") if isinstance(strategy.get("trigger"), dict) else {},
        "retargeting": strategy.get("retargeting") if isinstance(strategy.get("retargeting"), dict) else {},
    }


def _strategy_hash(strategy: Dict[str, Any]) -> str:
    raw = json.dumps(_strategy_snapshot(strategy), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_hash(snapshot: Dict[str, Any]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strategy_matches_task_snapshot(
    strategy: Dict[str, Any],
    task_snapshot: Dict[str, Any],
    expected_hash: str,
) -> bool:
    """Compare a queued card with the current strategy, including rc46 cards.

    Cards created before the canonical-snapshot fix did not persist the
    default ``task_action=create_retarget`` field.  They are safe to accept
    only when every other normalized field is identical and the current
    action is still that default.  Non-default actions never use this bridge.
    """
    current = _strategy_snapshot(strategy)
    if _snapshot_hash(current) == expected_hash:
        return True
    if str(current.get("task_action") or "") != "create_retarget":
        return False
    # Older cards can lack one or both later-added default fields.  Only bridge
    # the default values; a non-default current action/grouping never matches.
    removable_defaults = []
    if str(current.get("task_action") or "") == "create_retarget":
        removable_defaults.append("task_action")
    if str(current.get("material_grouping_mode") or "") == "separate":
        removable_defaults.append("material_grouping_mode")
    for mask in range(1, 1 << len(removable_defaults)):
        legacy = dict(current)
        for index, key in enumerate(removable_defaults):
            if mask & (1 << index):
                legacy.pop(key, None)
        if task_snapshot == legacy and _snapshot_hash(legacy) == expected_hash:
            return True
    return False


def _find_strategy(cfg: Dict[str, Any], strategy_id: str) -> Optional[Dict[str, Any]]:
    for strategy in cfg.get("strategies") or []:
        if isinstance(strategy, dict) and str(strategy.get("id") or "") == strategy_id:
            return strategy
    return None


def _latest_target_rows(target_uid: str, period: str) -> list[Dict[str, Any]]:
    response = DashboardApi().get_table_data(
        period=period,
        sort_by="costDiff",
        sort_order="desc",
        page=1,
        page_size=500_000,
        target_uid=target_uid,
    )
    if not response.get("success"):
        raise RuntimeError(response.get("message") or "读取素材最新数据失败")
    return [row for row in (response.get("data") or []) if isinstance(row, dict)]


def _latest_material_row(aavid: str, material_id: str, period: str) -> Optional[Dict[str, Any]]:
    """旧飞书任务兼容入口；新版任务按 target_uid 查询。"""
    response = DashboardApi().get_table_data(
        period=period,
        sort_by="costDiff",
        sort_order="desc",
        page=1,
        page_size=500_000,
    )
    if not response.get("success"):
        raise RuntimeError(response.get("message") or "读取素材最新数据失败")
    return next(
        (
            row
            for row in (response.get("data") or [])
            if isinstance(row, dict)
            and str(row.get("id") or "") == material_id
            and str(row.get("aadvid") or "") == aavid
        ),
        None,
    )


def _local_task(db: SQLiteStore, task_uid: str) -> Optional[Dict[str, Any]]:
    return db.select_one("cloud_retarget_task_local", where={"cloud_task_id": task_uid})


def _save_local_task(
    db: SQLiteStore,
    task_uid: str,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    task: Optional[Dict[str, Any]] = None,
) -> None:
    data: Dict[str, Any] = {
        "cloud_task_id": task_uid,
        "status": status,
        "result_json": _json(result or {}),
        "target_uid": str((task or {}).get("target_uid") or "legacy_unscoped"),
        "promotion_scene": str((task or {}).get("promotion_scene") or "live"),
        "plan_system": _safe_plan_system((task or {}).get("plan_system")),
    }
    if status == "executing":
        data["claimed_at"] = _now()
    if status in ("succeeded", "failed", "invalidated"):
        data["finished_at"] = _now()
    db.insert_or_update("cloud_retarget_task_local", data, unique_fields=["cloud_task_id"])


def _cached_report(task_uid: str, claim_token: str, local: Dict[str, Any], fencing_token=None) -> None:
    raw = local.get("result_json") or "{}"
    try:
        result = json.loads(raw)
    except Exception:
        result = {}
    report_retarget_task(
        task_uid,
        claim_token,
        str(local.get("status") or "failed"),
        message=str(result.get("message") or "本机已处理该任务"),
        detail=str(result.get("detail") or ""),
        regulate_task_id=str(result.get("regulate_task_id") or ""),
        result=result,
        fencing_token=fencing_token,
    )


def _task_materials(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_materials = task.get("materials")
    if not isinstance(raw_materials, list) or not raw_materials:
        raw_materials = [
            {
                "material_id": task.get("material_id"),
                "material_name": task.get("material_name"),
                "product_id": task.get("product_id"),
                "product_name": task.get("product_name"),
            }
        ]
    materials: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_materials:
        if not isinstance(raw, dict):
            continue
        material_id = str(raw.get("material_id") or "").strip()
        if not material_id or material_id in seen:
            continue
        seen.add(material_id)
        product_id = str(raw.get("product_id") or "").strip()
        product_ids: List[str] = []
        for value in raw.get("product_ids") or []:
            pid = str(value or "").strip()
            if pid and pid not in product_ids:
                product_ids.append(pid)
        if product_id and product_id not in product_ids:
            product_ids.insert(0, product_id)
        materials.append(
            {
                "material_id": material_id,
                "material_name": str(raw.get("material_name") or "").strip()[:512],
                "product_id": product_id,
                "product_name": str(raw.get("product_name") or "").strip()[:512],
                "product_ids": product_ids[:20],
            }
        )
        if len(materials) >= 20:
            break
    return materials


def _task_retarget_groups(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_groups = task.get("retarget_groups")
    if not isinstance(raw_groups, list):
        return []
    groups: List[Dict[str, Any]] = []
    for index, raw_group in enumerate(raw_groups[:10]):
        if not isinstance(raw_group, dict):
            continue
        materials = _task_materials({"materials": raw_group.get("materials") or []})
        if not materials:
            continue
        groups.append(
            {
                "group_uid": str(raw_group.get("group_uid") or f"group-{index + 1}")[:64],
                "materials": materials,
                "material_ids": [item["material_id"] for item in materials],
            }
        )
    return groups


def _task_candidate_material_ids(task: Dict[str, Any]) -> List[str]:
    raw_candidates = task.get("candidate_materials")
    candidates = _task_materials(
        {
            "materials": (
                raw_candidates
                if isinstance(raw_candidates, list) and raw_candidates
                else task.get("materials")
            )
        }
    )
    return [item["material_id"] for item in candidates]


def _validate_group_rate_capacity(
    task: Dict[str, Any],
    cfg: Dict[str, Any],
    strategy: Dict[str, Any],
    groups: List[Dict[str, Any]],
    db: SQLiteStore,
) -> None:
    occurrences = Counter(
        material_id
        for group in groups
        for material_id in group.get("material_ids") or []
    )
    target_uid = str(task.get("target_uid") or "legacy_unscoped")
    retargeting = task.get("retargeting") if isinstance(task.get("retargeting"), dict) else {}
    for material_id, requested_count in occurrences.items():
        if bool(cfg.get("per_strategy_rate_limit")):
            window_seconds, max_count = _interval_window_and_max(retargeting)
            remaining = rate_limit_strategy_remaining_capacity(
                db,
                material_id,
                str(strategy.get("id") or ""),
                window_seconds,
                max_count,
                target_uid,
            )
        else:
            window_seconds, max_count = _interval_from_root_cfg(cfg)
            remaining = rate_limit_remaining_capacity(
                db,
                material_id,
                window_seconds,
                max_count,
                target_uid,
            )
        if remaining is not None and requested_count > remaining:
            raise RuntimeError(
                f"素材 {material_id} 在本批次被安排{requested_count}次，"
                f"但当前限频只剩{remaining}次"
            )


async def _execute_grouped_task(
    task: Dict[str, Any],
    db: SQLiteStore,
    groups: List[Dict[str, Any]],
) -> Dict[str, Any]:
    validations: List[Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]] = []
    group_tasks: List[Dict[str, Any]] = []
    try:
        for index, group in enumerate(groups):
            materials = [dict(item) for item in group["materials"]]
            group_task = {
                **task,
                "task_uid": f"{task.get('task_uid')}:group:{index + 1}",
                "materials": materials,
                "material_id": materials[0]["material_id"],
                "material_name": materials[0].get("material_name") or "",
                "retarget_groups": [],
                "group_index": index + 1,
                "parent_task_uid": str(task.get("task_uid") or ""),
            }
            group_tasks.append(group_task)
            validations.append(await asyncio.to_thread(_validate_task, group_task, db))
        cfg, strategy, _rows = validations[0]
        _validate_group_rate_capacity(task, cfg, strategy, groups, db)
        unique_material_ids = list(
            dict.fromkeys(
                material_id
                for group in groups
                for material_id in group["material_ids"]
            )
        )
        consume_live_retarget_batch_once(
            str(task.get("task_uid") or ""),
            str(task.get("aavid") or ""),
            unique_material_ids,
            _task_candidate_material_ids(task),
        )
    except RetargetTaskInvalidated as exc:
        return _strategy_invalidated_result(exc)
    except Exception as exc:
        return {
            "success": False,
            "message": str(exc),
            "detail": traceback.format_exc(),
            "step": "group_revalidate",
        }

    group_results: List[Dict[str, Any]] = []
    regulate_task_ids: List[str] = []
    for index, group_task in enumerate(group_tasks, start=1):
        result = await _execute_task(
            group_task,
            db,
            _prevalidated=validations[index - 1],
            _skip_live_guard=True,
            _allow_groups=False,
        )
        ids = [
            str(value)
            for value in result.get("regulate_task_ids") or []
            if str(value or "")
        ]
        if not ids and result.get("regulate_task_id"):
            ids = [str(result["regulate_task_id"])]
        regulate_task_ids.extend(ids)
        group_results.append(
            {
                "group_index": index,
                "group_uid": groups[index - 1]["group_uid"],
                "material_ids": groups[index - 1]["material_ids"],
                "success": bool(result.get("success")),
                "message": str(result.get("message") or ""),
                "regulate_task_ids": ids,
                "result": result,
            }
        )
    succeeded_count = sum(1 for result in group_results if result["success"])
    all_succeeded = succeeded_count == len(group_results)
    pending_verification = any(
        str((result.get("result") or {}).get("step") or "") == "submitted_verifying"
        for result in group_results
    )
    return {
        "success": all_succeeded,
        "message": (
            f"{len(group_results)}条追投已提交，正在核验平台最终状态"
            if pending_verification
            else f"{len(group_results)}条追投全部创建成功"
            if all_succeeded
            else f"多组追投完成：成功{succeeded_count}条，失败{len(group_results) - succeeded_count}条"
        ),
        "detail": "" if all_succeeded else _json(group_results),
        "step": "submitted_verifying" if pending_verification else ("done" if all_succeeded else "partial_failure"),
        "pending_verification": pending_verification,
        "regulate_task_id": regulate_task_ids[0] if regulate_task_ids else "",
        "regulate_task_ids": regulate_task_ids,
        "group_results": group_results,
        "group_count": len(group_results),
        "successful_group_count": succeeded_count,
        "material_count": sum(len(group["material_ids"]) for group in groups),
        "unique_material_count": len(
            {
                material_id
                for group in groups
                for material_id in group["material_ids"]
            }
        ),
    }


def _validate_task(
    task: Dict[str, Any],
    db: SQLiteStore,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    cfg = load_rule_retargeting_config()
    if not cfg.get("enabled"):
        raise RetargetTaskInvalidated("规则化追投已关闭")
    strategy_id = str(task.get("strategy_id") or "")
    strategy = _find_strategy(cfg, strategy_id)
    if not strategy:
        raise RetargetTaskInvalidated("追投策略已删除")
    if str(strategy.get("action_mode") or "card_confirm") != "card_confirm":
        raise RetargetTaskInvalidated("追投策略的执行方式已经变更")
    expected_hash = str(task.get("strategy_hash") or "")
    task_snapshot = task.get("rule_snapshot") if isinstance(task.get("rule_snapshot"), dict) else {}
    if not task_snapshot or _snapshot_hash(task_snapshot) != expected_hash:
        raise RuntimeError("云端追投策略快照校验失败")
    if not _strategy_matches_task_snapshot(strategy, task_snapshot, expected_hash):
        raise RetargetTaskInvalidated("追投策略参数已经变更")

    aavid = str(task.get("aavid") or "")
    ad_id = str(task.get("ad_id") or "")
    target_uid = str(task.get("target_uid") or "")
    promotion_scene = str(task.get("promotion_scene") or "live")
    plan_system = normalize_plan_system(task.get("plan_system") or "unknown")
    trigger_level = str(task.get("trigger_level") or "material")
    materials = _task_materials(task)
    if not materials:
        raise RuntimeError("追投任务没有有效素材")
    if len(materials) > 20:
        raise RuntimeError("单个追投计划最多支持20条素材")
    assert_test_task_scope(
        aavid,
        [material["material_id"] for material in materials],
        _task_candidate_material_ids(task),
    )
    if not target_uid or target_uid == "legacy_unscoped":
        raise RuntimeError("旧版未归属任务仅供查看，不能执行追投")
    from services.qianchuan_accounts import bind_target_account_scope

    target: Dict[str, Any] = (
        db.select_one("promotion_target", where={"target_uid": target_uid})
        or {}
    )
    legacy_test_double = bool(target) and "account_uid" not in target
    account = None
    if target and not legacy_test_double:
        target, account = bind_target_account_scope(
            target_uid,
            owner_username=task.get("account_username"),
            db=db,
        )
        target = target or {}
    if (
        not target
        or not bool(target.get("enabled"))
        or (
            not legacy_test_double
            and (
                not bool(target.get("monitor_eligible"))
                or not bool(target.get("retarget_eligible"))
            )
        )
        or (
            not legacy_test_double
            and str(target.get("capacity_state") or "") != "active"
        )
        or (
            not legacy_test_double
            and (not account or not bool(account.get("enabled")))
        )
    ):
        raise RuntimeError(
            str(
                target.get("ineligible_reason")
                or "监控计划已删除、停用、未核验或正在等待监控容量"
            )
        )
    if (
        not legacy_test_double
        and str((account or {}).get("owner_username") or "").strip().casefold()
        != str(task.get("account_username") or "").strip().casefold()
    ):
        raise RuntimeError("追投任务不属于当前工具账号")
    task_account_uid = str(task.get("qianchuan_account_uid") or "").strip()
    if (
        task_account_uid
        and not legacy_test_double
        and task_account_uid != str(target.get("account_uid") or "")
    ):
        raise RuntimeError("追投任务的千川账户归属已被篡改")
    # ``collecting`` is only the transient state of the next read cycle.  The
    # last completed snapshot remains valid while it is fresh, and the
    # official execution adapter performs its own live recheck before POST.
    sync_status = str(target.get("last_status") or "").strip().lower()
    if sync_status not in {"ok", "collecting"}:
        raise RuntimeError("监控计划当前不是投放中状态，已阻止追投")
    if not legacy_test_double and not _timestamp_is_fresh(
        target.get("last_sync_at")
    ):
        raise RuntimeError(
            "监控计划最近一次采集已超过10分钟或时间无效，"
            "必须等待新一轮实时数据后重新确认"
        )
    if bool(target.get("automation_write_blocked")):
        raise RuntimeError(
            "该计划已触发自动写入安全封锁："
            + str(target.get("write_block_reason") or "请人工核对后解除")
        )
    if (
        str(target.get("aadvid") or "") != aavid
        or str(target.get("ad_id") or "") != ad_id
        or str(target.get("promotion_scene") or "") != promotion_scene
        or normalize_plan_system(target.get("plan_system") or "unknown")
        != plan_system
    ):
        raise RuntimeError("当前账户、广告ID或计划体系与提醒不一致")
    if plan_system == "unknown":
        raise RuntimeError("计划体系尚未确认是全域还是千川乘方")
    capability_ok, capability_error = check_target_capability(
        target,
        action="retarget",
        promotion_scene=promotion_scene,
        plan_system=plan_system,
        require_batch=len(materials) > 1,
    )
    if not capability_ok:
        raise RuntimeError(
            f"当前计划的追投能力证据无效：{capability_error}，已安全停止"
        )
    strategy_target_uid = str(strategy.get("target_uid") or "")
    if strategy_target_uid and strategy_target_uid != target_uid:
        raise RuntimeError("追投策略已改为其他监控计划")
    strategy_account_uid = str(strategy.get("account_uid") or "").strip()
    if strategy_account_uid and strategy_account_uid != str(
        target.get("account_uid") or ""
    ).strip():
        raise RuntimeError("追投策略已改为其他千川账户")
    from services.qianchuan_session import (
        automation_session_ready,
        current_session_owner,
        has_qianchuan_session,
    )

    task_owner = str(task.get("account_username") or "").strip().casefold()
    if not legacy_test_double and (
        not task_owner or current_session_owner() != task_owner
    ):
        raise RuntimeError("当前工具账号已经切换，本次追投任务已作废")
    session_gate = automation_session_ready(task.get("account_username"))
    if not legacy_test_double and int(
        task.get("qianchuan_session_epoch") or 0
    ) != int(
        session_gate.get("session_epoch") or 1
    ):
        raise RuntimeError("千川登录会话已经变化，请等待新提醒并重新确认")
    legacy_cookie_ready = os.path.isfile(os.path.join(DATA_DIR, "qcookie.json"))
    if (
        not legacy_test_double
        and str(session_gate.get("status") or "") == "login_required"
    ):
        raise RuntimeError("千川登录状态已失效，请重新登录后等待新提醒")
    if (
        not legacy_test_double
        and not session_gate.get("ready")
        and not legacy_cookie_ready
        and not has_qianchuan_session()
    ):
        raise RuntimeError("千川登录状态不存在，请在服务控制中重新登录")

    period = str(cfg.get("trigger_query_period") or "1h")
    if target_uid:
        raw_target_rows = _latest_target_rows(target_uid, period)
        if not legacy_test_double:
            selected_ids = {
                material["material_id"] for material in materials
            }
            stale_ids = {
                str(item.get("id") or "")
                for item in raw_target_rows
                if str(item.get("id") or "") in selected_ids
                and not _timestamp_is_fresh(
                    item.get("periodEndTime")
                    or item.get("period_end_time")
                    or item.get("createdAt")
                    or item.get("created_at")
                )
            }
            if stale_ids:
                raise RuntimeError(
                    "以下素材的实时数据已超过10分钟或时间无效："
                    + "、".join(sorted(stale_ids))
                )
            target_rows = [
                item
                for item in raw_target_rows
                if _timestamp_is_fresh(
                    item.get("periodEndTime")
                    or item.get("period_end_time")
                    or item.get("createdAt")
                    or item.get("created_at")
                )
            ]
        else:
            target_rows = raw_target_rows
        rows_by_id = {
            str(item.get("id") or ""): item
            for item in target_rows
            if str(item.get("aadvid") or "") == aavid
        }
    else:
        target_rows = []
        rows_by_id = {}
        for material in materials:
            material_id = material["material_id"]
            row = _latest_material_row(aavid, material_id, period)
            if (
                row
                and not legacy_test_double
                and not _timestamp_is_fresh(
                    row.get("periodEndTime")
                    or row.get("period_end_time")
                    or row.get("createdAt")
                    or row.get("created_at")
                )
            ):
                raise RuntimeError(
                    f"素材 {material_id} 的实时数据已超过10分钟或时间无效"
                )
            if row:
                rows_by_id[material_id] = row
    missing_ids = [
        material["material_id"]
        for material in materials
        if material["material_id"] not in rows_by_id
    ]
    if missing_ids:
        raise RuntimeError("最新素材数据中已找不到素材：" + "、".join(missing_ids))

    trigger = strategy.get("trigger") or {}
    trigger_level = str(strategy.get("trigger_level") or "material")
    if trigger_level == "product":
        if promotion_scene != "product":
            raise RuntimeError("商品级提醒不能用于直播计划")
        relation_rows = db.select(
            "promotion_material_product",
            fields="material_id, product_id",
            where={"target_uid": target_uid},
        )
        relation_map: Dict[str, list[str]] = {}
        for item in relation_rows:
            relation_map.setdefault(str(item.get("material_id") or ""), []).append(
                str(item.get("product_id") or "")
            )
        product_rows = db.select(
            "promotion_product",
            fields="product_id, product_name",
            where={"target_uid": target_uid},
        )
        product_names = {
            str(item.get("product_id") or ""): str(item.get("product_name") or "")
            for item in product_rows
        }
        snapshot_product_ids = {
            product_id
            for material in materials
            for product_id in material.get("product_ids") or []
            if product_id
        }
        if not snapshot_product_ids:
            raise RuntimeError("商品级提醒缺少有效商品")
        hits = evaluate_product_strategy(
            target_rows,
            strategy,
            relation_map=relation_map,
            product_names=product_names,
            allowed_product_ids=sorted(snapshot_product_ids),
        )
        current_candidates: Dict[str, set[str]] = {}
        for hit in hits:
            hit_product_id = str(hit.get("productId") or "")
            for candidate in hit.get("candidates") or []:
                candidate_id = str(candidate.get("id") or "")
                if candidate_id:
                    current_candidates.setdefault(candidate_id, set()).add(
                        hit_product_id
                    )
        for material in materials:
            material_id = material["material_id"]
            stored_products = set(material.get("product_ids") or [])
            current_relations = set(relation_map.get(material_id) or [])
            if not stored_products.intersection(current_relations):
                raise RuntimeError(
                    f"素材 {material_id} 已不再关联卡片中的商品"
                )
            if not stored_products.intersection(
                current_candidates.get(material_id) or set()
            ):
                raise RuntimeError(
                    f"素材 {material_id} 的商品汇总或候选条件已不满足追投规则"
                )
    else:
        failed_ids = [
            material["material_id"]
            for material in materials
            if not evaluate_trigger(
                trigger,
                rows_by_id[material["material_id"]],
            )
        ]
        if failed_ids:
            raise RuntimeError(
                "以下素材最新数据已不满足追投规则：" + "、".join(failed_ids)
            )

    retargeting = task.get("retargeting")
    if not isinstance(retargeting, dict):
        raise RuntimeError("追投参数快照无效")
    if json.dumps(retargeting, ensure_ascii=False, sort_keys=True) != json.dumps(
        task_snapshot.get("retargeting") or {}, ensure_ascii=False, sort_keys=True
    ):
        raise RuntimeError("追投参数快照与策略版本不一致")
    for material in materials:
        material_id = material["material_id"]
        if bool(cfg.get("per_strategy_rate_limit")):
            ws, mc = _interval_window_and_max(retargeting)
            if rate_limit_strategy_should_skip(
                db,
                material_id,
                strategy_id,
                ws,
                mc,
                target_uid,
            ):
                raise RuntimeError(f"素材 {material_id} 已达到本策略追投次数上限")
        else:
            ws, mc = _interval_from_root_cfg(cfg)
            if rate_limit_should_skip(db, material_id, ws, mc, target_uid):
                raise RuntimeError(f"素材 {material_id} 已达到全局追投次数上限")
    return cfg, strategy, [rows_by_id[item["material_id"]] for item in materials]


def _validate_budget_increase_task(
    task: Dict[str, Any],
    db: SQLiteStore,
) -> Tuple[
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, Any],
    Dict[str, Any],
]:
    """Re-read and validate a control task before adjusting its budget."""
    cfg = load_rule_retargeting_config()
    if not cfg.get("enabled"):
        raise RetargetTaskInvalidated("规则化追投已关闭")
    strategy_id = str(task.get("strategy_id") or "")
    strategy = _find_strategy(cfg, strategy_id)
    if not strategy:
        raise RetargetTaskInvalidated("追加预算策略已删除")
    if str(strategy.get("task_action") or "create_retarget") != "increase_budget":
        raise RetargetTaskInvalidated("策略已不再执行追加预算")
    if str(strategy.get("action_mode") or "card_confirm") != "card_confirm":
        raise RetargetTaskInvalidated("策略执行方式已经变更")
    expected_hash = str(task.get("strategy_hash") or "")
    task_snapshot = (
        task.get("rule_snapshot")
        if isinstance(task.get("rule_snapshot"), dict)
        else {}
    )
    if not task_snapshot or _snapshot_hash(task_snapshot) != expected_hash:
        raise RuntimeError("追加预算策略快照校验失败")
    if _strategy_hash(strategy) != expected_hash:
        raise RetargetTaskInvalidated("追加预算策略已经修改")

    target_uid = str(task.get("target_uid") or "")
    aavid = str(task.get("aavid") or "")
    ad_id = str(task.get("ad_id") or "")
    assist_task_id = str(task.get("assist_task_id") or "")
    target = db.select_one(
        "promotion_target",
        where={"target_uid": target_uid},
    ) or {}
    account = (
        db.select_one(
            "qianchuan_account",
            where={"account_uid": str(target.get("account_uid") or "")},
        )
        if target
        else None
    )
    if (
        not target
        or not bool(target.get("enabled"))
        or not bool(target.get("monitor_eligible"))
        or str(target.get("capacity_state") or "") != "active"
        or bool(target.get("automation_write_blocked"))
        or str(target.get("last_status") or "").strip().lower() != "ok"
        or str(target.get("aadvid") or "") != aavid
        or str(target.get("ad_id") or "") != ad_id
        or not account
        or not bool(account.get("enabled"))
    ):
        raise RuntimeError("监控账户或计划已停用、异常或归属发生变化")
    if str(strategy.get("target_uid") or "") != target_uid:
        raise RuntimeError("策略已经改为其他监控计划")
    strategy_account_uid = str(strategy.get("account_uid") or "")
    if strategy_account_uid and strategy_account_uid != str(
        target.get("account_uid") or ""
    ):
        raise RuntimeError("策略已经改为其他千川账户")
    if normalize_plan_system(target.get("plan_system") or "unknown") == "unknown":
        raise RuntimeError("计划体系尚未确认")
    sync_ready, sync_error = assist_task_sync_ready(target, max_age_minutes=10)
    if not sync_ready:
        raise RuntimeError(sync_error)

    from services.qianchuan_session import (
        automation_session_ready,
        current_session_owner,
    )

    owner = str(task.get("account_username") or "").strip().casefold()
    if not owner or str(current_session_owner() or "").strip().casefold() != owner:
        raise RuntimeError("当前工具账号已经切换或退出")
    if str((account or {}).get("owner_username") or "").strip().casefold() != owner:
        raise RuntimeError("千川账户不属于当前工具账号")
    session_gate = automation_session_ready(owner)
    if not session_gate.get("ready"):
        raise RuntimeError(
            str(session_gate.get("message") or "千川登录状态已失效")
        )
    if int(task.get("qianchuan_session_epoch") or 0) != int(
        session_gate.get("session_epoch") or 1
    ):
        raise RuntimeError("千川登录会话已经变化，请等待新的提醒")

    row = db.select_one(
        "pmc_roi2_assist_task",
        where={
            "target_uid": target_uid,
            "assist_task_id": assist_task_id,
        },
    ) or {}
    if (
        not row
        or str(row.get("aadvid") or "") != aavid
        or str(row.get("ad_id") or "") != ad_id
    ):
        raise RuntimeError("调控任务已删除或归属发生变化")
    delivery_type = row.get("ad_delivery_type")
    if str(delivery_type if delivery_type is not None else "0").strip() not in {
        "",
        "0",
    }:
        raise RuntimeError("调控任务已不在执行中")
    if not _timestamp_is_fresh(row.get("metrics_observed_at")):
        raise RuntimeError("调控任务最新指标已超过10分钟，请等待重新同步")
    trigger = strategy.get("trigger") or {}
    if not evaluate_trigger(trigger, row):
        raise RuntimeError("调控任务最新消耗或ROI已不满足策略")

    retargeting = (
        strategy.get("retargeting")
        if isinstance(strategy.get("retargeting"), dict)
        else {}
    )
    increase = (
        retargeting.get("budget_increase")
        if isinstance(retargeting.get("budget_increase"), dict)
        else {}
    )
    stored_calculation = task.get("calculation_snapshot")
    stored_fingerprint = str(task.get("calculation_fingerprint") or "")
    if not isinstance(stored_calculation, dict) or (
        budget_increase_fingerprint(
            target_uid=target_uid,
            strategy_id=strategy_id,
            calculation=stored_calculation,
        )
        != stored_fingerprint
    ):
        raise RuntimeError("卡片预算计算快照校验失败")
    latest_calculation = calculate_budget_increase(row, increase)
    return cfg, strategy, target, row, latest_calculation


async def _execute_budget_increase_task(
    task: Dict[str, Any], db: SQLiteStore,
) -> Dict[str, Any]:
    """An existing budget/duration plan shares one fenced, GET-recoverable intent."""
    target_uid = str(task.get("target_uid") or "")
    assist_task_id = str(task.get("assist_task_id") or "")
    gate = None
    calculation = {}
    try:
        async with exclusive_browser_operation(
            f"飞书确认追加预算:{target_uid}:{assist_task_id}", priority=10,
        ):
            _, _, target, row, calculation = await asyncio.to_thread(_validate_budget_increase_task, task, db)
            from config import QIANCHUAN_BACKEND
            if QIANCHUAN_BACKEND != "official_api":
                return {
                    "success": False, "step": "platform_capability_unverified",
                    "message": "追加预算提交能力尚未完成真实页面接口取证，本次未向千川提交",
                    "calculation": calculation,
                }
            from datetime import datetime, timedelta
            from functools import partial
            from services.official_api_execution import prepare_submission_gate, _ExistingExecutionIntent
            from services.qianchuan_open_api.normalizers import text_id
            from services.qianchuan_open_api.runtime import get_official_api_service
            service = get_official_api_service()
            aavid, ad_id = str(target.get("aadvid") or ""), str(target.get("ad_id") or "")
            scene = str(target.get("promotion_scene") or "product")
            now = datetime.now()
            tasks, _ = await asyncio.to_thread(
                service.list_control_tasks, aavid, ad_id=ad_id,
                marketing_goal="LIVE_PROM_GOODS" if scene == "live" else "VIDEO_PROM_GOODS",
                start_time=(now - timedelta(days=179)).strftime("%Y-%m-%d 00:00:00"),
                end_time=(now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"),
            )
            exact = next((item for item in tasks if text_id(item.get("task_id")) == text_id(assist_task_id)), None)
            if not exact or str(exact.get("scene") or "").upper() != "MATERIAL_ADD_BUDGET":
                raise RuntimeError("官方 API 未找到待调整的素材追投调控任务")
            if str(exact.get("status") or "").upper() in {
                "PAUSE", "PAUSED", "DISABLE", "DISABLED", "FINISHED", "ENDED", "OFFLINE_TIME",
            }:
                raise RuntimeError("调控任务已不在可调整状态")
            extend_hours = calculation.get("extend_hours")
            new_duration = None
            if extend_hours:
                if exact.get("duration") is None:
                    raise RuntimeError("官方 API 未返回当前时长，本次预算/时长均未提交")
                new_duration = float(exact["duration"]) + float(extend_hours)
            task_uid = str(task.get("task_uid") or "")
            execution_uid = str(task.get("execution_uid") or task_uid) + ":budget-plan"
            required_steps = ["budget"] + (["duration"] if extend_hours else [])

            def final_check():
                _assert_execution_not_cancelled()
                _validate_budget_increase_task(task, db)
                return ""

            gate = prepare_submission_gate(
                task_uid=task_uid, action_type="budget", aavid=aavid, ad_id=ad_id,
                intent_key=execution_uid, control_task_id=assist_task_id,
                submission_claim=task.get("submission_claim"), pre_submit_check=final_check,
                verify_payload={
                    "aavid": aavid, "ad_id": ad_id, "promotion_scene": scene,
                    "task_id": assist_task_id, "execution_uid": execution_uid,
                    "budget": calculation["new_budget_yuan"], "duration": new_duration,
                    "required_steps": required_steps, "attempted_steps": ["budget"],
                    "completed_steps": [],
                },
            )
            try:
                response = await asyncio.to_thread(
                    service.update_control_budget, aavid, assist_task_id,
                    calculation["new_budget_yuan"], before_send=gate.before_send,
                )
            except _ExistingExecutionIntent as exc:
                status = str(exc.row.get("status") or "unknown_requires_review")
                return {"success": status == "confirmed_succeeded", "step": status,
                        "message": "该预算调整已有提交记录，只允许继续核验，不会重复修改",
                        "calculation": calculation}
            if response is None:
                raise RuntimeError("预算修改未返回确定结果")
            gate.verify_payload["completed_steps"] = ["budget"]
            gate.accept(response)
            if extend_hours:
                response = await asyncio.to_thread(
                    service.update_control_duration, aavid, assist_task_id, new_duration,
                    before_send=partial(gate.before_followup, "duration"),
                )
                if response is None:
                    raise RuntimeError("时长修改未返回确定结果")
                gate.verify_payload["completed_steps"] = ["budget", "duration"]
                gate.accept(response)
            return {
                "success": True, "step": "submitted_verifying",
                "message": "预算/时长修改已提交，正在整体只读核验",
                "detail": _json({"submission_phase": gate.phase, "required_steps": required_steps,
                                 "completed_steps": gate.verify_payload["completed_steps"]}),
                "calculation": calculation,
            }
    except Exception as exc:
        if gate is not None and gate.handle_error(exc):
            return {
                "success": True, "step": "submitted_verifying",
                "message": "预算/时长修改部分已接受或结果未知，已禁止重复提交，等待整体核验",
                "detail": _json({"submission_phase": gate.phase, "error": str(exc),
                                 "completed_steps": gate.verify_payload.get("completed_steps") or []}),
                "calculation": calculation,
            }
        if isinstance(exc, RetargetTaskInvalidated):
            return _strategy_invalidated_result(exc)
        return {"success": False, "message": str(exc),
                "detail": _json({"submission_phase": gate.phase if gate else "not_sent",
                                 "trace": traceback.format_exc()}), "step": "revalidate"}


async def _execute_task(
    task: Dict[str, Any],
    db: SQLiteStore,
    *,
    _prevalidated: Optional[
        Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]
    ] = None,
    _skip_live_guard: bool = False,
    _allow_groups: bool = True,
) -> Dict[str, Any]:
    _assert_execution_not_cancelled()
    if str(task.get("task_operation") or "") == "increase_budget":
        return await _execute_budget_increase_task(task, db)
    groups = _task_retarget_groups(task)
    if _allow_groups and len(groups) > 1:
        return await _execute_grouped_task(task, db, groups)
    task_uid = str(task.get("task_uid") or "")
    aavid = str(task.get("aavid") or "")
    ad_id = str(task.get("ad_id") or "")
    target_uid = str(task.get("target_uid") or "legacy_unscoped")
    promotion_scene = str(task.get("promotion_scene") or "live")
    plan_system = _safe_plan_system(task.get("plan_system"))
    trigger_level = str(task.get("trigger_level") or "material")
    materials = _task_materials(task)
    first_material = materials[0] if materials else {}
    product_id = str(first_material.get("product_id") or task.get("product_id") or "")
    product_name = str(first_material.get("product_name") or task.get("product_name") or "")
    material_id = str(first_material.get("material_id") or task.get("material_id") or "")
    material_ids = [item["material_id"] for item in materials]
    started_at = _now()
    t0 = time.time()
    cfg: Dict[str, Any] = {}
    strategy: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    try:
        if _prevalidated is None:
            cfg, strategy, rows = await asyncio.to_thread(_validate_task, task, db)
        else:
            cfg, strategy, rows = _prevalidated
    except RetargetTaskInvalidated as exc:
        return _strategy_invalidated_result(exc)
    except Exception as exc:
        failure = {"success": False, "message": str(exc), "detail": traceback.format_exc(), "step": "revalidate"}
        ended_at = _now()
        task_retargeting = task.get("retargeting") if isinstance(task.get("retargeting"), dict) else {}
        _insert_run(
            db,
            aavid=aavid,
            ad_id=ad_id,
            material_id=material_id,
            target_uid=target_uid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
            trigger_level=trigger_level,
            product_id=product_id,
            product_name=product_name,
            material_name=str(task.get("material_name") or ""),
            strategy_name=str(task.get("strategy_name") or "飞书确认追投"),
            regulate_task_id="",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=int((time.time() - t0) * 1000),
            status=-1,
            step="revalidate",
            message=failure["message"],
            detail=failure["detail"],
            retargeting=task_retargeting,
            rule_full_json=_json(task.get("rule_snapshot") or {}),
            trigger_snapshot_json=_json(task.get("trigger_snapshot") or {}),
            query_snapshot_json=_json(task.get("query_snapshot") or {}),
            headless=True,
            browser_headless_rule=True,
            trigger_source=f"feishu_card:{task_uid}"[:64],
            cloud_task_id=task_uid,
            operator_id=str(task.get("clicker_open_id") or ""),
            materials=materials,
        )
        return failure

    retargeting = task.get("retargeting") or {}
    strategy_name = str(strategy.get("title") or task.get("strategy_name") or "飞书确认追投")
    try:
        if not _skip_live_guard:
            consume_live_retarget_batch_once(
                task_uid,
                aavid,
                material_ids,
                _task_candidate_material_ids(task),
            )
    except Exception as exc:
        failure = {
            "success": False,
            "message": str(exc),
            "detail": traceback.format_exc(),
            "step": "local_live_guard",
        }
        ended_at = _now()
        _insert_run(
            db,
            aavid=aavid,
            ad_id=ad_id,
            material_id=material_id,
            target_uid=target_uid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
            trigger_level=trigger_level,
            product_id=product_id,
            product_name=product_name,
            material_name=str(task.get("material_name") or ""),
            strategy_name=strategy_name,
            regulate_task_id="",
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=int((time.time() - t0) * 1000),
            status=-1,
            step=failure["step"],
            message=failure["message"],
            detail=failure["detail"],
            retargeting=retargeting,
            rule_full_json=_json(cfg),
            trigger_snapshot_json=_json(task.get("trigger_snapshot") or {}),
            query_snapshot_json=_json(task.get("query_snapshot") or {}),
            headless=bool(cfg.get("browser_headless", True)),
            browser_headless_rule=bool(cfg.get("browser_headless", True)),
            trigger_source=f"feishu_card:{task_uid}"[:64],
            cloud_task_id=task_uid,
            operator_id=str(task.get("clicker_open_id") or ""),
            materials=materials,
        )
        return failure
    svc = QianChuanRetargetingService.from_rule_file_dict(cfg)
    results: List[Any] = []
    attempt_history: List[Dict[str, Any]] = []
    submission_attempts = 0
    rate_recorded_under_lock = False
    last_attempt_execution_uid = task_uid
    try:
        target = db.select_one("promotion_target", where={"target_uid": target_uid}) or {}
        for attempt in range(1, 4):
            submission_attempts = attempt
            async with exclusive_browser_operation(
                f"飞书确认追投:{target_uid}:{','.join(material_ids)}",
                priority=10,
            ):
                # 每一次允许的提交尝试前都重新复核完整业务状态。重试等待
                # 发生在锁外，不能阻塞其他账户的采集和写任务。
                cfg, strategy, rows = await asyncio.to_thread(
                    _validate_task,
                    task,
                    db,
                )
                attempt_execution_uid = f"{task_uid}:attempt:{attempt}"
                last_attempt_execution_uid = attempt_execution_uid
                def final_preflight():
                    _assert_execution_not_cancelled()
                    _validate_task(task, db)
                    return ""
                locked_result = await svc.run(
                    aavid=int(aavid),
                    ad_id=int(ad_id),
                    material_id=material_id,
                    material_ids=material_ids,
                    retargeting=retargeting,
                    strategy_title=strategy_name,
                    execution_uid=attempt_execution_uid,
                    reconciliation_task_uid=str(
                        task.get("parent_task_uid") or task_uid
                    ),
                    target_uid=target_uid,
                    promotion_scene=promotion_scene,
                    plan_system=plan_system,
                    source_url=target.get("sanitized_page_url") or None,
                    reuse_session=False,
                    close_session=False,
                    **({"submission_claim": task.get("submission_claim"),
                        "pre_submit_check": final_preflight}
                       if QIANCHUAN_BACKEND == "official_api" else {}),
                )
                results = [locked_result]
                attempt_history.append(
                    {
                        "attempt": attempt,
                        **locked_result.asdict(),
                    }
                )
                if locked_result.success:
                    try:
                        for current_material_id in material_ids:
                            if bool(cfg.get("per_strategy_rate_limit")):
                                ws, mc = _interval_window_and_max(retargeting)
                                rate_limit_strategy_record_success(
                                    db,
                                    current_material_id,
                                    str(strategy.get("id") or ""),
                                    ws,
                                    mc,
                                    target_uid,
                                )
                            root_ws, root_mc = _interval_from_root_cfg(cfg)
                            rate_limit_record_success(
                                db,
                                current_material_id,
                                root_ws,
                                root_mc,
                                target_uid,
                            )
                    except Exception as rate_exc:
                        logger.exception(
                            "追投成功后记录限频失败，已暂停目标 target=%s",
                            target_uid,
                        )
                        block_target_after_rate_record_failure(
                            db,
                            target_uid,
                            rate_exc,
                        )
                    rate_recorded_under_lock = True
            if (
                locked_result.success
                or not bool(getattr(locked_result, "retryable", False))
                or attempt >= 3
            ):
                break
            default_delay = 60 if attempt == 1 else 120
            retry_delay = max(
                default_delay,
                int(getattr(locked_result, "retry_after_seconds", 0) or 0),
            )
            retry_report = await asyncio.to_thread(
                report_retarget_task,
                str(task.get("parent_task_uid") or task_uid),
                str(task.get("claim_token") or ""),
                "executing",
                fencing_token=task.get("fencing_token"),
                message=(
                    f"第{attempt}次追投已明确失败，未创建任务；"
                    f"将在{retry_delay}秒后进行第{attempt + 1}次尝试"
                ),
                detail=str(locked_result.message or ""),
                result={
                    "success": False,
                    "step": "retry_waiting",
                    "attempt": attempt,
                    "max_attempts": 3,
                    "retry_after_seconds": retry_delay,
                },
            )
            if not retry_report.get("success"):
                event = _LEASE_CANCEL_EVENT.get()
                if event is not None:
                    event.set()
                raise RuntimeError("重试前领取权已失效，禁止继续提交")
            await asyncio.sleep(retry_delay)
    except Exception as exc:
        results = []
        failure = {"success": False, "message": "追投执行异常", "detail": traceback.format_exc(), "step": "exception"}
    finally:
        await svc.close()

    ended_at = _now()
    duration = int((time.time() - t0) * 1000)
    if not results:
        payload = failure
        regulate_task_id = ""
    else:
        result_payloads = [item.asdict() for item in results]
        regulate_task_ids = [
            str(item.regulate_task_id or "")
            for item in results
            if str(item.regulate_task_id or "")
        ]
        successful_material_ids = material_ids if bool(results[0].success) else []
        all_succeeded = bool(results[0].success)
        first_result = results[0]
        payload = first_result.asdict()
        payload["success"] = all_succeeded
        payload["regulate_task_ids"] = regulate_task_ids
        payload["successful_material_ids"] = successful_material_ids
        payload["results"] = result_payloads
        pending_verification = any(
            str(getattr(item, "step", "") or "") == "submitted_verifying"
            for item in results
        )
        if pending_verification:
            payload["message"] = "追投已提交，正在核验平台最终状态"
            payload["step"] = "submitted_verifying"
            payload["pending_verification"] = True
        elif all_succeeded:
            payload["message"] = f"追投成功（{len(materials)}条素材）"
        elif successful_material_ids:
            payload["message"] = (
                f"部分追投成功（{len(successful_material_ids)}/{len(materials)}条素材）"
            )
            payload["detail"] = _json(result_payloads)
            payload["step"] = "partial_failure"
        regulate_task_id = regulate_task_ids[0] if regulate_task_ids else ""
    payload["attempt_count"] = submission_attempts
    payload["max_attempts"] = 3
    payload["attempt_history"] = attempt_history
    if (
        results
        and not payload.get("success")
        and bool(getattr(results[0], "retryable", False))
        and submission_attempts >= 3
    ):
        payload["message"] = (
            f"{str(payload.get('message') or '追投失败')}（已达到3次尝试上限）"
        )
    payload["materials"] = materials
    payload["material_count"] = len(materials)
    trigger_snapshot = task.get("trigger_snapshot") or {}
    query_snapshot = dict(task.get("query_snapshot") or {})
    selection_snapshot = task.get("selection_snapshot")
    if isinstance(selection_snapshot, dict):
        query_snapshot["feishu_material_selection"] = selection_snapshot
    query_snapshot["revalidated_at"] = ended_at
    query_snapshot["revalidated_material_rows"] = rows
    _insert_run(
        db,
        aavid=aavid,
        ad_id=ad_id,
        material_id=material_id,
        target_uid=target_uid,
        promotion_scene=promotion_scene,
        plan_system=plan_system,
        trigger_level=trigger_level,
        product_id=product_id,
        product_name=product_name,
        material_name=str(task.get("material_name") or ""),
        strategy_name=strategy_name,
        regulate_task_id=regulate_task_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration,
        # pmc_retargeting_run.status is a legacy -1/1 field.  Pending/final
        # state is stored separately in execution_state.
        status=(1 if payload.get("success") else -1),
        step=str(payload.get("step") or "done"),
        message=str(payload.get("message") or ""),
        detail=str(payload.get("detail") or ""),
        retargeting=retargeting,
        rule_full_json=_json(cfg),
        trigger_snapshot_json=_json(trigger_snapshot),
        query_snapshot_json=_json(query_snapshot),
        headless=bool(payload.get("headless", cfg.get("browser_headless", True))),
        browser_headless_rule=bool(cfg.get("browser_headless", True)),
        trigger_source=f"feishu_card:{task_uid}"[:64],
        cloud_task_id=last_attempt_execution_uid,
        operator_id=str(task.get("clicker_open_id") or ""),
        materials=materials,
    )
    successful_ids = set(payload.get("successful_material_ids") or [])
    if payload.get("success") and not successful_ids:
        successful_ids = set(material_ids)
    if successful_ids and not rate_recorded_under_lock:
        for current_material_id in material_ids:
            if current_material_id not in successful_ids:
                continue
            if bool(cfg.get("per_strategy_rate_limit")):
                ws, mc = _interval_window_and_max(retargeting)
                rate_limit_strategy_record_success(
                    db,
                    current_material_id,
                    str(strategy.get("id") or ""),
                    ws,
                    mc,
                    target_uid,
                )
            root_ws, root_mc = _interval_from_root_cfg(cfg)
            rate_limit_record_success(
                db,
                current_material_id,
                root_ws,
                root_mc,
                target_uid,
            )
    payload["regulate_task_id"] = regulate_task_id
    return payload


async def _heartbeat_lease(task_uid: str, claim_token: str, *, fencing_token=None, cancelled=None) -> None:
    while not _WORKER_STOP.is_set():
        # Cancellation must not leave a four-minute blocking executor job
        # behind for every completed card.
        await asyncio.sleep(240)
        if _WORKER_STOP.is_set():
            if cancelled is not None:
                cancelled.set()
            return
        try:
            response = await asyncio.to_thread(
                report_retarget_task,
                task_uid,
                claim_token,
                "executing",
                fencing_token=fencing_token,
                message="桌面工具正在执行追投",
            )
        except Exception:
            if cancelled is not None:
                cancelled.set()
            logger.exception("[飞书确认追投] 任务租约续期异常 task=%s", task_uid)
            return
        if not response.get("success"):
            if cancelled is not None:
                cancelled.set()
            logger.warning("[飞书确认追投] 任务租约续期失败 %s: %s", task_uid, response.get("message"))
            return


async def run_worker_loop() -> None:
    init_sqlite_schema()
    db = SQLiteStore()
    last_prune = 0.0
    logger.info("[飞书确认追投] 任务轮询已启动")
    while not _WORKER_STOP.is_set():
        try:
            if time.time() - last_prune > 6 * 3600:
                await asyncio.to_thread(prune_operation_events, 180)
                last_prune = time.time()
            response = await asyncio.to_thread(pull_retarget_task)
            if not response.get("success"):
                if not response.get("silent"):
                    logger.warning("[飞书确认追投] 领取任务失败: %s", response.get("message"))
                await asyncio.sleep(15)
                continue
            task = response.get("data")
            if not isinstance(task, dict):
                await asyncio.sleep(POLL_SECONDS)
                continue
            task_uid = str(task.get("task_uid") or "")
            claim_token = str(task.get("claim_token") or "")
            fence = task.get("fencing_token")
            if fence is not None:
                task["submission_claim"] = {
                    "task_uid": task_uid, "account_username": str(task.get("account_username") or ""),
                    "claim_token": claim_token, "fencing_token": int(fence),
                }
            cached = _local_task(db, task_uid)
            if cached and str(cached.get("status")) in (
                "succeeded",
                "failed",
                "invalidated",
                "unknown_requires_review",
            ):
                await asyncio.to_thread(_cached_report, task_uid, claim_token, cached, fence)
                continue
            if cached and str(cached.get("status")) == "executing":
                unknown = {
                    "success": False,
                    "message": "上次执行在完成前中断，为避免重复追投，本次未再次执行",
                    "detail": "请在千川调控任务和账户操作流水中人工核对",
                    "step": "recovery_guard",
                }
                _save_local_task(db, task_uid, "failed", unknown, task)
                await asyncio.to_thread(
                    report_retarget_task,
                    task_uid,
                    claim_token,
                    "failed",
                    fencing_token=fence,
                    message=unknown["message"],
                    detail=unknown["detail"],
                    result=unknown,
                )
                continue

            _save_local_task(
                db,
                task_uid,
                "executing",
                {"message": "桌面工具正在复核并执行追投"},
                task,
            )
            executing_report = await asyncio.to_thread(
                report_retarget_task,
                task_uid,
                claim_token,
                "executing",
                fencing_token=fence,
                message="桌面工具正在复核并执行追投",
            )
            if not executing_report.get("success"):
                refused = {
                    "success": False,
                    "message": "任务队列未确认本机执行租约，本次未执行追投",
                    "detail": str(executing_report.get("message") or "任务租约无效"),
                    "step": "claim_confirm",
                }
                _save_local_task(db, task_uid, "failed", refused, task)
                logger.warning("[飞书确认追投] 任务队列拒绝执行租约 %s: %s", task_uid, refused["detail"])
                continue
            cancelled = threading.Event()
            context_token = _LEASE_CANCEL_EVENT.set(cancelled)
            heartbeat = asyncio.create_task(_heartbeat_lease(task_uid, claim_token, fencing_token=fence, cancelled=cancelled))
            try:
                result = await _execute_task(task, db)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                _LEASE_CANCEL_EVENT.reset(context_token)
            pending_verification = bool(result.get("pending_verification")) or str(
                result.get("step") or ""
            ) == "submitted_verifying"
            final_status = (
                "verifying"
                if pending_verification
                else "unknown_requires_review"
                if str(result.get("step") or "") in {"unknown_requires_review", "result_unknown"}
                else (
                    "invalidated"
                    if result.get("invalidated")
                    else ("succeeded" if result.get("success") else "failed")
                )
            )
            _save_local_task(db, task_uid, final_status, result, task)
            final_report = await asyncio.to_thread(
                report_retarget_task,
                task_uid,
                claim_token,
                final_status,
                fencing_token=fence,
                message=str(
                    result.get("message")
                    or (
                        "追投已提交，正在核验平台最终状态"
                        if pending_verification
                        else ("追投成功" if result.get("success") else "追投失败")
                    )
                ),
                detail=str(result.get("detail") or ""),
                regulate_task_id=str(result.get("regulate_task_id") or ""),
                result=result,
            )
            if not final_report.get("success"):
                logger.warning("[飞书确认追投] 最终状态回报未落地，保留执行意图待对账 task=%s", task_uid)
        except Exception:
            logger.exception("[飞书确认追投] 任务轮询异常")
            await asyncio.sleep(15)


def start_retarget_task_worker_background_thread() -> threading.Thread:
    global _WORKER_THREAD
    def _entry() -> None:
        asyncio.run(run_worker_loop())

    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return _WORKER_THREAD
        _WORKER_STOP.clear()
        _WORKER_THREAD = threading.Thread(
            target=_entry,
            name="retarget-card-worker",
            daemon=True,
        )
        _WORKER_THREAD.start()
        return _WORKER_THREAD


def stop_retarget_task_worker_background_thread(timeout: float = 18.0) -> None:
    global _WORKER_THREAD
    _WORKER_STOP.set()
    with _WORKER_LOCK:
        thread = _WORKER_THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.1, float(timeout)))
    if thread is None or not thread.is_alive():
        with _WORKER_LOCK:
            if _WORKER_THREAD is thread:
                _WORKER_THREAD = None
