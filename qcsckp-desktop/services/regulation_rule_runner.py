# -*- coding: utf-8 -*-
"""
规则化停投调度：按固定间隔（默认 10 分钟）从 pmc_roi2_assist_task（与 DashboardApi.get_roi2_assist_table_data 一致）
拉取「ad_delivery_type=0 调控中、updated_at 近 N 分钟（默认 30，见 REGULATION_ASSIST_UPDATED_WITHIN_MINUTES；传 0 则仍按近 1 天）」任务，
按 stat_cost_for_roi2_assist 降序；再按每条策略的 trigger（ROI2 调控指标）筛选，
对任务执行暂停或删除（见 regulation_service）。写入 pmc_regulation_run（停投不做素材级限频）。
多策略并行默认最多 3 路（环境变量 REGULATION_STRATEGY_PARALLEL）。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from api.dashboard import DashboardApi
from api.rule_regulation_config import (
    build_trigger_evaluation_snapshot_roi2_assist,
    evaluate_trigger_roi2_assist,
    load_rule_regulation_config,
)
from config import CURRENT_VERSION
from services.regulation_service import (
    QianChuanRegulationStopService,
    regulation_log_tag,
)
from services.promotion_browser_lock import exclusive_browser_operation
from services.qianchuan_accounts import schedulable_promotion_targets
from services.plan_system import normalize_plan_system
from services.promotion_capability import (
    check_target_capability,
    parse_target_capability,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema

DEFAULT_INTERVAL_SEC = 600
MAX_STRATEGY_PARALLEL = 3
_DASHBOARD_PAGE_SIZE = 500_000
# 停投调度拉取调控任务时：仅处理「入库更新时间」近 N 分钟内有变动的行，减少对已停滞数据的重复跳过；0=沿用大屏默认（近 1 天）
_DEFAULT_ASSIST_UPDATED_WITHIN_MIN = 30


def _target_assist_sync_ready(
    target: Dict[str, Any],
    *,
    max_age_minutes: int = _DEFAULT_ASSIST_UPDATED_WITHIN_MIN,
) -> Tuple[bool, str]:
    capability = parse_target_capability(target)
    if bool(capability.get("assist_sync_in_progress")):
        return False, "调控任务正在同步中"
    if not bool(capability.get("assist_sync_enabled")):
        return False, "调控任务采集未启用"
    if not bool(capability.get("assist_sync_ok")):
        return False, "最近一轮调控任务未完整同步"
    raw = str(capability.get("assist_synced_at") or "").strip()
    try:
        synced_at = datetime.fromisoformat(raw)
    except ValueError:
        return False, "调控任务同步时间无效"
    age = datetime.now() - synced_at
    if age < timedelta(minutes=-5) or age > timedelta(
        minutes=max(1, int(max_age_minutes))
    ):
        return False, "调控任务同步结果已过期"
    return True, ""


def _revalidate_stop_candidate(
    db: SQLiteStore,
    *,
    target_uid: str,
    assist_task_id: str,
    aavid: str,
    ad_id: str,
    promotion_scene: str,
    trigger: Dict[str, Any],
    max_age_minutes: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str, str]:
    """在取得浏览器独占锁后重读目标和指标，关闭检查后等待锁的竞态窗口。"""
    target = db.select_one(
        "promotion_target",
        where={"target_uid": target_uid},
    ) or {}
    row = db.select_one(
        "pmc_roi2_assist_task",
        where={
            "target_uid": target_uid,
            "assist_task_id": assist_task_id,
        },
    ) or {}
    target_system = normalize_plan_system(
        target.get("plan_system") or "unknown"
    )
    account = (
        db.select_one(
            "qianchuan_account",
            where={"account_uid": str(target.get("account_uid") or "")},
        )
        if target
        else None
    )
    row_system = normalize_plan_system(row.get("plan_system") or "unknown")
    from services.qianchuan_session import current_session_owner

    current_owner = str(current_session_owner() or "").strip().casefold()
    account_owner = str((account or {}).get("owner_username") or "").strip().casefold()
    row_account_uid = str(row.get("account_uid") or "").strip()
    if (
        not target
        or not bool(target.get("enabled"))
        or str(target.get("capacity_state") or "") != "active"
        or not account
        or not bool(account.get("enabled"))
        or str(target.get("last_status") or "").strip().lower() != "ok"
        or str(target.get("aadvid") or "") != aavid
        or str(target.get("ad_id") or "") != ad_id
        or str(target.get("promotion_scene") or "") != promotion_scene
        or (current_owner and account_owner != current_owner)
    ):
        return None, None, target_system, "监控计划已停用、状态异常或目标身份变化"
    if (
        not row
        or str(row.get("aadvid") or "") != aavid
        or str(row.get("ad_id") or "") != ad_id
        or str(row.get("promotion_scene") or "") != promotion_scene
        or (row_system != "unknown" and row_system != target_system)
        or (
            row_account_uid
            and row_account_uid != str(target.get("account_uid") or "")
        )
    ):
        return None, None, target_system, "调控任务已删除或归属发生变化"
    if target_system == "unknown":
        return None, None, target_system, "计划体系尚未确认"
    assist_ready, assist_error = _target_assist_sync_ready(
        target,
        max_age_minutes=max_age_minutes,
    )
    if not assist_ready:
        return None, None, target_system, assist_error
    capability_ok, capability_error = check_target_capability(
        target,
        action="regulation",
        promotion_scene=promotion_scene,
        plan_system=target_system,
    )
    if not capability_ok:
        return None, None, target_system, capability_error
    delivery_type = row.get("ad_delivery_type")
    if str(delivery_type if delivery_type is not None else "0").strip() not in {
        "",
        "0",
    }:
        return None, None, target_system, "调控任务已不在执行中"
    if not evaluate_trigger_roi2_assist(trigger, row):
        return None, None, target_system, "最新调控指标已不满足停投策略"
    return target, row, target_system, ""


def _assist_updated_within_minutes_from_env() -> Optional[int]:
    e = os.environ.get("REGULATION_ASSIST_UPDATED_WITHIN_MINUTES", "").strip()
    if e == "":
        return _DEFAULT_ASSIST_UPDATED_WITHIN_MIN
    try:
        n = int(e)
        if n <= 0:
            return None
        return min(n, 24 * 60)
    except ValueError:
        return _DEFAULT_ASSIST_UPDATED_WITHIN_MIN

_assist_task_locks: Dict[str, asyncio.Lock] = {}
_assist_task_locks_guard = asyncio.Lock()


async def _lock_for_assist_task(
    aid: str,
    target_uid: Optional[str] = None,
) -> asyncio.Lock:
    k = (
        f"{str(target_uid or 'legacy_unscoped').strip()}:"
        f"{str(aid).strip() or '__empty__'}"
    )
    async with _assist_task_locks_guard:
        if k not in _assist_task_locks:
            _assist_task_locks[k] = asyncio.Lock()
        return _assist_task_locks[k]


def _beijing_now_str() -> str:
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def resolve_ad_id_for_aavid(db: SQLiteStore, aavid: str) -> Optional[str]:
    rows = db.select(
        table="pmc_ad_detail_basic",
        fields="ad_id",
        where="aadvid = ?",
        params=(str(aavid).strip(),),
        limit=1,
    )
    if not rows:
        return None
    v = rows[0].get("ad_id")
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _task_name_from_assist_row(row: Dict[str, Any]) -> str:
    """与 pmc_roi2_assist_task.task_name 一致。"""
    if not isinstance(row, dict):
        return ""
    v = row.get("task_name")
    if v is None:
        return ""
    s = str(v).strip()
    return s[:512] if s else ""


def _product_fields_from_assist_row(
    db: SQLiteStore,
    row: Dict[str, Any],
    target_uid: str,
) -> tuple[str, str]:
    raw = row.get("product_ids_json")
    try:
        product_ids = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        product_ids = []
    if not isinstance(product_ids, list):
        product_ids = []
    ids = list(dict.fromkeys(str(x or "").strip() for x in product_ids if str(x or "").strip()))
    if not ids:
        return "", ""
    names: List[str] = []
    for product_id in ids:
        product = db.select_one(
            "promotion_product",
            fields="product_name",
            where={"target_uid": target_uid, "product_id": product_id},
        )
        name = str((product or {}).get("product_name") or "").strip()
        if name:
            names.append(name)
    return ",".join(ids), "、".join(names)


def has_completed_stop(
    db: SQLiteStore,
    target_uid: str,
    assist_task_id: str,
) -> bool:
    rows = db.execute(
        "SELECT id FROM pmc_regulation_run "
        "WHERE target_uid=? AND assist_task_id=? AND status IN (1,2) "
        "ORDER BY id DESC LIMIT 1",
        (str(target_uid or "legacy_unscoped"), str(assist_task_id or "")),
        fetch=True,
    ) or []
    return bool(rows)


def _insert_regulation_run(
    db: SQLiteStore,
    *,
    aavid: str,
    ad_id: str,
    target_uid: str = "legacy_unscoped",
    promotion_scene: str = "live",
    plan_system: str = "unknown",
    product_id: str = "",
    product_name: str = "",
    task_name: str = "",
    strategy_name: str = "",
    assist_task_id: str = "",
    stop_action: str = "",
    started_at: str,
    ended_at: str,
    duration_ms: int,
    status: int,
    step: str,
    message: str,
    detail: str,
    rule_full_json: str,
    trigger_snapshot_json: str,
    query_snapshot_json: str,
    headless: bool,
    browser_headless_rule: bool,
    trigger_source: str = "scheduler",
) -> None:
    _sn = str(strategy_name or "").strip()[:128]
    if not _sn or _sn == "?":
        _sn = None
    _tn = str(task_name or "").strip()
    try:
        target = db.select_one(
            "promotion_target",
            fields="account_uid",
            where={"target_uid": str(target_uid or "legacy_unscoped")},
        ) or {}
        account_uid = str(target.get("account_uid") or "")
    except Exception:
        account_uid = ""
    data: Dict[str, Any] = {
        "aavid": aavid,
        "account_uid": account_uid,
        "ad_id": ad_id,
        "target_uid": str(target_uid or "legacy_unscoped"),
        "promotion_scene": str(promotion_scene or "live"),
        "plan_system": normalize_plan_system(plan_system or "unknown"),
        "product_id": str(product_id or "").strip() or None,
        "product_name": str(product_name or "").strip() or None,
        "assist_task_id": str(assist_task_id or "").strip() or None,
        "task_name": _tn if _tn else None,
        "strategy_name": _sn,
        "stop_action": str(stop_action or "").strip()[:32] or None,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "status": status,
        "step": step,
        "message": message[:2000] if message else "",
        "detail": detail[:8000] if detail else "",
        "rule_full_json": rule_full_json,
        "trigger_snapshot_json": trigger_snapshot_json,
        "query_snapshot_json": query_snapshot_json,
        "headless": 1 if headless else 0,
        "browser_headless_rule": 1 if browser_headless_rule else 0,
        "trigger_source": (str(trigger_source or "scheduler").strip()[:64] or "scheduler"),
        "app_version": CURRENT_VERSION,
    }
    run_id = db.insert(table="pmc_regulation_run", data=data)
    try:
        from api.operation_events import upsert_operation_event

        upsert_operation_event(
            {
                "event_uid": f"regulation_run:{run_id}",
                "aavid": aavid,
                "account_uid": account_uid,
                "ad_id": ad_id,
                "target_uid": str(target_uid or "legacy_unscoped"),
                "promotion_scene": str(promotion_scene or "live"),
                "plan_system": normalize_plan_system(plan_system or "unknown"),
                "product_id": str(product_id or "").strip(),
                "product_name": str(product_name or "").strip(),
                "source": "tool_direct",
                "action_type": "stop",
                "object_type": "assist_task",
                "object_id": str(assist_task_id or ""),
                "object_name": _tn,
                "plan_id": ad_id,
                "regulate_task_id": str(assist_task_id or ""),
                "status": "success" if status in (1, 2) else "failed",
                "summary": message or "停投",
                "detail": detail,
                "request": {"stop_action": stop_action},
                "trigger_json": trigger_snapshot_json,
                "response": {"step": step},
                "occurred_at": ended_at,
            },
            db,
        )
    except Exception:
        logger.exception("%s 统一操作流水写入失败 run_id=%s", regulation_log_tag(scheduler=True), run_id)


async def run_one_cycle(db: SQLiteStore) -> None:
    _log_sched = regulation_log_tag(scheduler=True)
    cfg = load_rule_regulation_config()
    if not cfg.get("enabled"):
        logger.info("%s 未启用 enabled，跳过本轮", _log_sched)
        return

    period = str(cfg.get("trigger_query_period") or "1h").strip() or "1h"
    strategies = cfg.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        logger.warning("%s strategies 为空", _log_sched)
        return

    rule_full_json = _json_dumps(cfg)

    assist_uw_min = _assist_updated_within_minutes_from_env()
    dash = DashboardApi()
    resp = dash.get_roi2_assist_table_data(
        sort_by="stat_cost_for_roi2_assist",
        sort_order="desc",
        page=1,
        page_size=_DASHBOARD_PAGE_SIZE,
        ad_delivery_type=0,
        regulation_full_scan=True,
        assist_updated_within_minutes=assist_uw_min,
    )
    if not resp.get("success"):
        logger.warning("%s get_roi2_assist_table_data 失败: %s", _log_sched, resp.get("message"))
        return

    rows: List[Dict[str, Any]] = resp.get("data") or []

    # 追投白名单：白名单中的调控任务自动跳过停投
    whitelist = cfg.get("whitelist_assist_ids") or []
    if whitelist:
        whitelist_set = set(str(x).strip() for x in whitelist if str(x).strip())
        filtered_rows: List[Dict[str, Any]] = []
        skipped_ids: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            _aid = str(row.get("assist_task_id") or "").strip()
            if _aid in whitelist_set:
                skipped_ids.append(_aid)
                continue
            filtered_rows.append(row)
        if skipped_ids:
            logger.info(
                "%s 追投白名单过滤：跳过 %s 个调控任务 (%s)",
                _log_sched,
                len(skipped_ids),
                ",".join(skipped_ids),
            )
        rows = filtered_rows

    query_at = _beijing_now_str()
    assist_sort_by = resp.get("sortBy") or "stat_cost_for_roi2_assist"

    max_p = MAX_STRATEGY_PARALLEL
    try:
        e = os.environ.get("REGULATION_STRATEGY_PARALLEL", "").strip()
        if e.isdigit():
            max_p = max(1, min(10, int(e)))
    except Exception:
        pass

    _uw_log = f"updated_at近{assist_uw_min}分钟" if assist_uw_min else "updated_at近1天"
    logger.info(
        "%s 配置周期=%s 调控任务(ad_delivery_type=0,%s)=%s 策略数=%s 并行=%s",
        _log_sched,
        period,
        _uw_log,
        len(rows),
        len(strategies),
        min(max_p, len(strategies)),
    )

    sem = asyncio.Semaphore(max_p)
    browser_rule = bool(cfg.get("browser_headless", True))
    enabled_targets = (
        schedulable_promotion_targets(db=db)
        if hasattr(db, "config")
        else db.select(
            "promotion_target",
            where="enabled=1 AND capacity_state='active'",
        )
    )

    async def process_strategy(st: Dict[str, Any]) -> None:
        async with sem:
            trigger = st.get("trigger") or {}
            if not isinstance(trigger, dict):
                logger.warning("%s 策略 trigger 非法，跳过", _log_sched)
                return

            st_label = str(st.get("title") or st.get("id") or "?")[:64]
            _tag = regulation_log_tag(strategy_title=st_label)
            stop_action = str(st.get("regulation_stop_action") or "pause").strip().lower()
            if stop_action not in ("pause", "delete"):
                stop_action = "pause"
            strategy_target_uid = str(st.get("target_uid") or "").strip()
            if not strategy_target_uid:
                if len(enabled_targets) == 1:
                    strategy_target_uid = str(
                        enabled_targets[0].get("target_uid") or ""
                    ).strip()
                elif len(enabled_targets) > 1:
                    logger.warning(
                        "%s 未选择监控计划，当前有多条启用计划，已安全跳过",
                        _tag,
                    )
                    return

            hit_rows: List[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if (
                    strategy_target_uid
                    and str(row.get("target_uid") or "") != strategy_target_uid
                ):
                    continue
                if evaluate_trigger_roi2_assist(trigger, row):
                    hit_rows.append(row)

            logger.info("%s 命中调控任务数=%s", _tag, len(hit_rows))
            if not hit_rows:
                return

            svc = QianChuanRegulationStopService.from_rule_file_dict(cfg)
            headless_cfg = browser_rule
            try:
                for row in hit_rows:
                    assist_raw = row.get("assist_task_id")
                    assist_task_id = str(assist_raw).strip() if assist_raw is not None else ""
                    task_name = _task_name_from_assist_row(row)
                    aavid_raw = row.get("aadvid")
                    aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
                    target_uid = str(row.get("target_uid") or "legacy_unscoped").strip()
                    if not target_uid or target_uid == "legacy_unscoped":
                        logger.warning(
                            "%s 旧版未归属调控任务仅供查看，不参与自动停投 "
                            "assist_task_id=%s",
                            _tag,
                            assist_task_id,
                        )
                        continue
                    promotion_scene = str(row.get("promotion_scene") or "live").strip()
                    ad_id = str(row.get("ad_id") or "").strip()
                    target = db.select_one(
                        "promotion_target",
                        where={"target_uid": target_uid},
                    ) or {}
                    target_plan_system = normalize_plan_system(
                        target.get("plan_system") or "unknown"
                    )
                    row_plan_system = normalize_plan_system(
                        row.get("plan_system") or "unknown"
                    )
                    plan_system = (
                        target_plan_system
                        if target_plan_system != "unknown"
                        else row_plan_system
                    )
                    product_id, product_name = _product_fields_from_assist_row(
                        db, row, target_uid
                    )

                    eval_snap = build_trigger_evaluation_snapshot_roi2_assist(trigger, row)
                    trigger_snap = _json_dumps(
                        {
                            "strategy_id": st.get("id"),
                            "strategy_title": st.get("title"),
                            "trigger_config": trigger,
                            "evaluation": eval_snap,
                        }
                    )
                    query_snap = _json_dumps(
                        {
                            "data_source": "pmc_roi2_assist_task",
                            "trigger_query_period": period,
                            "assist_updated_within_minutes": assist_uw_min,
                            "sort_by": assist_sort_by,
                            "query_at": query_at,
                            "assist_task_total": resp.get("total"),
                            "assist_row": row,
                        }
                    )

                    if not assist_task_id:
                        now = _beijing_now_str()
                        _insert_regulation_run(
                            db,
                            aavid=aavid or "",
                            ad_id="",
                            target_uid=target_uid,
                            promotion_scene=promotion_scene,
                            plan_system=plan_system,
                            product_id=product_id,
                            product_name=product_name,
                            task_name=task_name,
                            strategy_name=st_label,
                            assist_task_id="",
                            stop_action=stop_action,
                            started_at=now,
                            ended_at=now,
                            duration_ms=0,
                            status=-1,
                            step="validate",
                            message="调控任务行缺少 assist_task_id，无法停投",
                            detail="",
                            rule_full_json=rule_full_json,
                            trigger_snapshot_json=trigger_snap,
                            query_snapshot_json=query_snap,
                            headless=headless_cfg,
                            browser_headless_rule=browser_rule,
                        )
                        logger.info("%s 跳过：无 assist_task_id", _tag)
                        continue

                    task_lock = await _lock_for_assist_task(assist_task_id, target_uid)
                    async with task_lock:
                        if has_completed_stop(db, target_uid, assist_task_id):
                            logger.info(
                                "%s 已有成功停投流水，幂等跳过 target=%s assist_task_id=%s",
                                _tag,
                                target_uid,
                                assist_task_id,
                            )
                            continue
                        if target_uid != "legacy_unscoped":
                            target_matches = (
                                bool(target)
                                and bool(target.get("enabled"))
                                and str(target.get("aadvid") or "") == aavid
                                and str(target.get("ad_id") or "") == ad_id
                                and str(target.get("promotion_scene") or "live")
                                == promotion_scene
                                and (
                                    row_plan_system == "unknown"
                                    or row_plan_system == target_plan_system
                                )
                            )
                            if not target_matches:
                                now = _beijing_now_str()
                                _insert_regulation_run(
                                    db,
                                    aavid=aavid or "",
                                    ad_id=ad_id,
                                    target_uid=target_uid,
                                    promotion_scene=promotion_scene,
                                    plan_system=plan_system,
                                    product_id=product_id,
                                    product_name=product_name,
                                    task_name=task_name,
                                    strategy_name=st_label,
                                    assist_task_id=assist_task_id,
                                    stop_action=stop_action,
                                    started_at=now,
                                    ended_at=now,
                                    duration_ms=0,
                                    status=-1,
                                    step="target_mismatch",
                                    message="调控任务与监控计划不匹配或计划已停用，已阻止停投",
                                    detail="",
                                    rule_full_json=rule_full_json,
                                    trigger_snapshot_json=trigger_snap,
                                    query_snapshot_json=query_snap,
                                    headless=headless_cfg,
                                    browser_headless_rule=browser_rule,
                                )
                                continue
                            if plan_system == "unknown":
                                logger.warning(
                                    "%s 计划体系尚未识别，本轮不执行自动停投 "
                                    "target=%s assist_task_id=%s",
                                    _tag,
                                    target_uid,
                                    assist_task_id,
                                )
                                continue
                            assist_ready, assist_error = (
                                _target_assist_sync_ready(
                                    target,
                                    max_age_minutes=(
                                        assist_uw_min
                                        or _DEFAULT_ASSIST_UPDATED_WITHIN_MIN
                                    ),
                                )
                            )
                            if not assist_ready:
                                logger.warning(
                                    "%s 当前计划的调控任务同步状态不可用，本轮不执行：%s "
                                    "target=%s assist_task_id=%s",
                                    _tag,
                                    assist_error,
                                    target_uid,
                                    assist_task_id,
                                )
                                continue
                            capability_ok, capability_error = (
                                check_target_capability(
                                    target,
                                    action="regulation",
                                    promotion_scene=promotion_scene,
                                    plan_system=plan_system,
                                )
                            )
                            if not capability_ok:
                                logger.warning(
                                    "%s 当前计划的停投能力证据无效，本轮不执行：%s "
                                    "target=%s assist_task_id=%s",
                                    _tag,
                                    capability_error,
                                    target_uid,
                                    assist_task_id,
                                )
                                continue
                        if not ad_id:
                            now = _beijing_now_str()
                            _insert_regulation_run(
                                db,
                                aavid=aavid or "",
                                ad_id="",
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                product_id=product_id,
                                product_name=product_name,
                                task_name=task_name,
                                strategy_name=st_label,
                                assist_task_id=assist_task_id,
                                stop_action=stop_action,
                                started_at=now,
                                ended_at=now,
                                duration_ms=0,
                                status=-1,
                                step="resolve_ad_id",
                                message="pmc_ad_detail_basic 中无该 aadvid 对应的 ad_id",
                                detail="",
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            continue

                        try:
                            aavid_int = int(str(aavid_raw).strip())
                            ad_id_int = int(str(ad_id).strip())
                        except (TypeError, ValueError):
                            now = _beijing_now_str()
                            _insert_regulation_run(
                                db,
                                aavid=aavid or "",
                                ad_id=str(ad_id),
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                product_id=product_id,
                                product_name=product_name,
                                task_name=task_name,
                                strategy_name=st_label,
                                assist_task_id=assist_task_id,
                                stop_action=stop_action,
                                started_at=now,
                                ended_at=now,
                                duration_ms=0,
                                status=-1,
                                step="validate",
                                message="aavid 或 ad_id 无法转为整数",
                                detail="",
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            continue

                        started_at = _beijing_now_str()
                        t0 = time.time()
                        try:
                            async with exclusive_browser_operation(
                                f"停投:{target_uid}:{assist_task_id}",
                                priority=20,
                            ):
                                (
                                    latest_target,
                                    latest_row,
                                    latest_plan_system,
                                    revalidate_error,
                                ) = _revalidate_stop_candidate(
                                    db,
                                    target_uid=target_uid,
                                    assist_task_id=assist_task_id,
                                    aavid=aavid,
                                    ad_id=ad_id,
                                    promotion_scene=promotion_scene,
                                    trigger=trigger,
                                    max_age_minutes=(
                                        assist_uw_min
                                        or _DEFAULT_ASSIST_UPDATED_WITHIN_MIN
                                    ),
                                )
                                if revalidate_error:
                                    logger.warning(
                                        "%s 取得浏览器锁后复核失败，已取消停投：%s "
                                        "target=%s assist_task_id=%s",
                                        _tag,
                                        revalidate_error,
                                        target_uid,
                                        assist_task_id,
                                    )
                                    continue
                                if has_completed_stop(
                                    db,
                                    target_uid,
                                    assist_task_id,
                                ):
                                    continue
                                from services.qianchuan_session import (
                                    automation_session_ready,
                                )

                                session_gate = automation_session_ready()
                                if not session_gate.get("ready"):
                                    logger.warning(
                                        "%s 千川主登录会话不可用，全部账户自动停投已暂停：%s",
                                        _tag,
                                        session_gate.get("message") or "请重新登录",
                                    )
                                    continue
                                target = latest_target or {}
                                row = latest_row or {}
                                plan_system = latest_plan_system
                                task_name = _task_name_from_assist_row(row)
                                product_id, product_name = (
                                    _product_fields_from_assist_row(
                                        db,
                                        row,
                                        target_uid,
                                    )
                                )
                                eval_snap = (
                                    build_trigger_evaluation_snapshot_roi2_assist(
                                        trigger,
                                        row,
                                    )
                                )
                                trigger_snap = _json_dumps(
                                    {
                                        "strategy_id": st.get("id"),
                                        "strategy_title": st.get("title"),
                                        "trigger_config": trigger,
                                        "evaluation": eval_snap,
                                    }
                                )
                                result = await svc.run(
                                    aavid=aavid_int,
                                    ad_id=ad_id_int,
                                    assist_task_id=assist_task_id,
                                    stop_action=stop_action,
                                    strategy_title=st_label,
                                    target_uid=target_uid,
                                    promotion_scene=promotion_scene,
                                    plan_system=plan_system,
                                    source_url=target.get("sanitized_page_url") or None,
                                    reuse_session=False,
                                    close_session=False,
                                )
                        except Exception:
                            ended_at = _beijing_now_str()
                            dur = int((time.time() - t0) * 1000)
                            _insert_regulation_run(
                                db,
                                aavid=str(aavid_int),
                                ad_id=str(ad_id_int),
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                product_id=product_id,
                                product_name=product_name,
                                task_name=task_name,
                                strategy_name=st_label,
                                assist_task_id=assist_task_id,
                                stop_action=stop_action,
                                started_at=started_at,
                                ended_at=ended_at,
                                duration_ms=dur,
                                status=-1,
                                step="exception",
                                message="run 异常",
                                detail=traceback.format_exc()[:8000],
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            logger.exception("%s run 异常 assist_task_id=%s", _tag, assist_task_id)
                            continue

                        ended_at = _beijing_now_str()
                        dur = int((time.time() - t0) * 1000)
                        if result.step == "done_already_paused":
                            st_ok = 2
                        elif result.success:
                            st_ok = 1
                        else:
                            st_ok = -1
                        if result.step == "done_already_paused" or not result.success:
                            detail = (result.detail or "")[:8000]
                        else:
                            detail = ""
                        _insert_regulation_run(
                            db,
                            aavid=str(result.aavid or aavid_int),
                            ad_id=str(result.ad_id or ad_id_int),
                            target_uid=target_uid,
                            promotion_scene=promotion_scene,
                            plan_system=plan_system,
                            product_id=product_id,
                            product_name=product_name,
                            task_name=task_name,
                            strategy_name=st_label,
                            assist_task_id=str(result.assist_task_id or assist_task_id),
                            stop_action=str(result.stop_action or stop_action),
                            started_at=started_at,
                            ended_at=ended_at,
                            duration_ms=dur,
                            status=st_ok,
                            step=result.step,
                            message=result.message,
                            detail=detail,
                            rule_full_json=rule_full_json,
                            trigger_snapshot_json=trigger_snap,
                            query_snapshot_json=query_snap,
                            headless=bool(result.headless),
                            browser_headless_rule=browser_rule,
                        )
                        logger.info(
                            "%s assist_task_id=%s success=%s step=%s",
                            _tag,
                            assist_task_id,
                            result.success,
                            result.step,
                        )
            finally:
                await svc.close()

    await asyncio.gather(*(process_strategy(st) for st in strategies))


def _interval_sec() -> int:
    try:
        e = os.environ.get("REGULATION_RULE_INTERVAL_SEC", "").strip()
        if e.isdigit():
            return max(60, int(e))
    except Exception:
        pass
    return DEFAULT_INTERVAL_SEC


async def main_loop(interval_sec: Optional[int] = None) -> None:
    sec = int(interval_sec) if interval_sec is not None else _interval_sec()
    init_sqlite_schema()
    db = SQLiteStore()
    logger.info(
        "%s 启动，间隔 %ss，版本 %s",
        regulation_log_tag(scheduler=True),
        sec,
        CURRENT_VERSION,
    )
    while True:
        try:
            await run_one_cycle(db)
        except Exception:
            logger.exception("%s 本轮未捕获异常", regulation_log_tag(scheduler=True))
        await asyncio.sleep(max(60, int(sec)))


def _gui_background_target() -> None:
    try:
        asyncio.run(main_loop())
    except Exception:
        logger.exception("%s 后台线程异常退出", regulation_log_tag(scheduler=True))


def start_regulation_rule_runner_background_thread() -> threading.Thread:
    t = threading.Thread(
        target=_gui_background_target,
        name="regulation-rule-runner",
        daemon=True,
    )
    t.start()
    logger.info(
        "%s 后台线程已启动（默认间隔 %ss，环境变量 REGULATION_RULE_INTERVAL_SEC 可覆盖，最低 60s）",
        regulation_log_tag(scheduler=True),
        _interval_sec(),
    )
    return t


def main() -> None:
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
