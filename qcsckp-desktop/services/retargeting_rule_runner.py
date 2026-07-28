# -*- coding: utf-8 -*-
"""
规则化追投调度：按固定间隔拉取大屏同期素材，按每条「追投策略」的 trigger 分别筛选后执行 Playwright 追投，并写入 pmc_retargeting_run。
多策略之间 asyncio 并行，默认最多 5 路（环境变量 RETARGET_STRATEGY_PARALLEL）。
根级 interval 对应全局限频表 pmc_retargeting_rate_limit（每素材一行）。
未开启分策略限频：仅按该表做「是否跳过」判断，成功后只更新该表。
开启分策略限频：「是否跳过」只按各策略的 pmc_retargeting_rate_limit_strategy 判断；成功后策略表 +1，且对全局表按根级 interval 调用 rate_limit_record_success（窗口未过期则 use_count+1，过期则重置 limit_started_at 并记为 1）。
触发限频时仅跳过，不写流水。
同一素材在多策略并行时，用「每素材一把 asyncio 锁」串行化：检查限频 → 执行追投 → 成功后记次，避免两策略同时通过检查导致超次数。
同一策略本轮内多条命中素材：同一浏览器进程内每条素材均重新 goto 投放详情 URL 并切 Tab；该策略全部处理完后再 close。

运行（项目根目录）:
    python -m services.retargeting_rule_runner
GUI：由 gui_app 调用 start_retargeting_rule_runner_background_thread() 启动同逻辑后台线程。
可选环境变量 / 常量见下方 DEFAULT_INTERVAL_SEC。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

RATE_LIMIT_TABLE = "pmc_retargeting_rate_limit"
RATE_LIMIT_STRATEGY_TABLE = "pmc_retargeting_rate_limit_strategy"

from api.dashboard import DashboardApi
from api.rule_retargeting_config import (
    build_trigger_evaluation_snapshot,
    evaluate_trigger,
    load_rule_retargeting_config,
)
from config import CURRENT_VERSION, TEST_MODE
from services.cloud_retarget_client import create_retarget_task
from services.local_test_guard import row_is_in_test_scope
from services.product_rule_engine import evaluate_product_strategy
from services.plan_system import normalize_plan_system
from services.promotion_browser_lock import exclusive_browser_operation
from services.retargeting_service import (
    QianChuanRetargetingService,
    retarget_log_tag,
    retargeting_block_from_full_config,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


DEFAULT_INTERVAL_SEC = 180

# 多策略并行上限（同一轮内各策略各起一个浏览器任务）
MAX_STRATEGY_PARALLEL = 5

_DASHBOARD_PAGE_SIZE = 500_000

# 多策略并行时，同一 material_id 的限频检查与记次必须互斥，避免竞态超次数
_material_retouch_locks: Dict[str, asyncio.Lock] = {}
_material_retouch_locks_guard = asyncio.Lock()


def auto_execute_allowed_in_current_environment() -> bool:
    """正式环境保留自动追投；本地测试环境只允许走飞书确认任务。"""
    return not TEST_MODE


async def _lock_for_material_retouch(
    material_id: str,
    target_uid: Optional[str] = None,
) -> asyncio.Lock:
    mid = str(material_id).strip()
    if not mid:
        mid = "__empty_material__"
    lock_key = f"{_rate_target_uid(target_uid)}:{mid}"
    async with _material_retouch_locks_guard:
        if lock_key not in _material_retouch_locks:
            _material_retouch_locks[lock_key] = asyncio.Lock()
        return _material_retouch_locks[lock_key]


def _beijing_now_str() -> str:
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _interval_from_root_cfg(cfg: Dict[str, Any]) -> Tuple[int, int]:
    """全策略共用限频：优先根级 interval，与 rule_retargeting.json 一致。"""
    inv = cfg.get("interval")
    if isinstance(inv, dict):
        return _interval_window_and_max({"interval": inv})
    strats = cfg.get("strategies")
    if isinstance(strats, list) and strats:
        r0 = strats[0].get("retargeting")
        if isinstance(r0, dict):
            return _interval_window_and_max(r0)
    rb = retargeting_block_from_full_config(cfg)
    return _interval_window_and_max(rb)


def _interval_window_and_max(retargeting: Dict[str, Any]) -> Tuple[int, int]:
    inv = retargeting.get("interval") or {}
    if not isinstance(inv, dict):
        return 86400, 1
    try:
        ws = int(float(inv.get("window_seconds", 86400)))
    except (TypeError, ValueError):
        ws = 86400
    try:
        mc = int(inv.get("max_count", 1))
    except (TypeError, ValueError):
        mc = 1
    return max(0, ws), max(0, mc)


def _parse_beijing_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


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


def resolve_target(
    db: SQLiteStore,
    target_uid: str,
) -> Optional[Dict[str, Any]]:
    uid = str(target_uid or "").strip()
    if not uid:
        return None
    return db.select_one("promotion_target", where={"target_uid": uid})


def _rate_target_uid(target_uid: Optional[str]) -> str:
    return str(target_uid or "legacy_unscoped").strip() or "legacy_unscoped"


def _optimization_goal_str(retargeting: Dict[str, Any]) -> Optional[str]:
    m = str(retargeting.get("method") or "").strip().lower()
    if m != "cost_control":
        return None
    cc = retargeting.get("cost_control") or {}
    if not isinstance(cc, dict):
        return None
    og = str(cc.get("optimization_goal") or "net_roi").strip().lower()
    return og or None


def _material_name_from_dashboard_row(row: Dict[str, Any]) -> str:
    """大屏 get_table_data 行：title 来自 video_name；兼容原始键名。"""
    if not isinstance(row, dict):
        return ""
    for k in ("title", "video_name", "videoName"):
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s[:512]
    return ""


def _account_name_for_target(
    db: SQLiteStore,
    target: Dict[str, Any],
    fallback: str = "",
) -> str:
    aavid = str(target.get("aadvid") or "").strip()
    ad_id = str(target.get("ad_id") or "").strip()
    if aavid:
        account = db.select_one(
            "pmc_ad_detail_basic",
            fields="user_info_name",
            where=(
                {"aadvid": aavid, "ad_id": ad_id}
                if ad_id
                else {"aadvid": aavid}
            ),
            order_by="created_at DESC",
        )
        name = str((account or {}).get("user_info_name") or "").strip()
        if name:
            return name[:200]
    return str(fallback or "").strip()[:200]


def rate_limit_should_skip(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> bool:
    """
    只读判断：当前窗口内「已成功次数」是否已达上限；达上限则跳过本次追投。
    不写入限频表；计数仅在追投成功后由 rate_limit_record_success 更新。

    window_seconds / max_count 任一为 0 或负则视为不限频。
    """
    if window_seconds <= 0 or max_count <= 0:
        return False
    if not str(material_id).strip():
        return False

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return False

    rows = db.select(
        table=RATE_LIMIT_TABLE,
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
        limit=1,
    )

    if not rows:
        return False

    row = rows[0]
    start_s = row.get("limit_started_at") or ""
    start_dt = _parse_beijing_dt(str(start_s))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        return False

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        return use_count >= max_count
    return False


def rate_limit_record_success(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    """
    追投成功（Playwright 返回 success）后调用：按窗口累加成功次数，或过期后新开窗口记为 1。
    不限频时不写表。
    """
    if window_seconds <= 0 or max_count <= 0:
        return
    if not str(material_id).strip():
        return

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return

    rows = db.select(
        table=RATE_LIMIT_TABLE,
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": material_id,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    start_dt = _parse_beijing_dt(str(row.get("limit_started_at") or ""))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        db.update(
            table=RATE_LIMIT_TABLE,
            data={
                "limit_started_at": now_str,
                "use_count": 1,
                "updated_at": now_str,
            },
            where="target_uid = ? AND material_id = ?",
            params=(_rate_target_uid(target_uid), material_id),
        )
        return

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        db.update(
            table=RATE_LIMIT_TABLE,
            data={"use_count": use_count + 1, "updated_at": now_str},
            where="target_uid = ? AND material_id = ?",
            params=(_rate_target_uid(target_uid), material_id),
        )
        return

    db.update(
        table=RATE_LIMIT_TABLE,
        data={
            "limit_started_at": now_str,
            "use_count": 1,
            "updated_at": now_str,
        },
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
    )


def rate_limit_strategy_should_skip(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> bool:
    """分策略限频：同一素材在不同策略下独立计数。"""
    if window_seconds <= 0 or max_count <= 0:
        return False
    mid = str(material_id).strip()
    sid = str(strategy_id).strip()
    if not mid or not sid:
        return False

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return False

    rows = db.select(
        table=RATE_LIMIT_STRATEGY_TABLE,
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
        limit=1,
    )

    if not rows:
        return False

    row = rows[0]
    start_s = row.get("limit_started_at") or ""
    start_dt = _parse_beijing_dt(str(start_s))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        return False

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        return use_count >= max_count
    return False


def rate_limit_strategy_record_success(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    if window_seconds <= 0 or max_count <= 0:
        return
    mid = str(material_id).strip()
    sid = str(strategy_id).strip()
    if not mid or not sid:
        return

    now_str = _beijing_now_str()
    now_dt = _parse_beijing_dt(now_str)
    if now_dt is None:
        return

    rows = db.select(
        table=RATE_LIMIT_STRATEGY_TABLE,
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": mid,
                "strategy_id": sid,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    start_dt = _parse_beijing_dt(str(row.get("limit_started_at") or ""))
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    if start_dt is None:
        db.update(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
                "limit_started_at": now_str,
                "use_count": 1,
                "updated_at": now_str,
            },
            where="target_uid = ? AND material_id = ? AND strategy_id = ?",
            params=(_rate_target_uid(target_uid), mid, sid),
        )
        return

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        db.update(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={"use_count": use_count + 1, "updated_at": now_str},
            where="target_uid = ? AND material_id = ? AND strategy_id = ?",
            params=(_rate_target_uid(target_uid), mid, sid),
        )
        return

    db.update(
        table=RATE_LIMIT_STRATEGY_TABLE,
        data={
            "limit_started_at": now_str,
            "use_count": 1,
            "updated_at": now_str,
        },
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
    )


def rate_limit_increment_manual_only(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    """
    即刻追投「表单已就绪」成功时：仅对 use_count +1，不修改 limit_started_at（不重置窗口起点）。
    与自动追投的 rate_limit_record_success 区分，避免与调度器的窗口轮转逻辑打架。
    不限频时不写表。
    """
    if window_seconds <= 0 or max_count <= 0:
        return
    if not str(material_id).strip():
        return

    now_str = _beijing_now_str()
    rows = db.select(
        table=RATE_LIMIT_TABLE,
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": material_id,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    db.update(
        table=RATE_LIMIT_TABLE,
        data={"use_count": use_count + 1, "updated_at": now_str},
        where="target_uid = ? AND material_id = ?",
        params=(_rate_target_uid(target_uid), material_id),
    )


def rate_limit_increment_manual_only_strategy(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
    target_uid: Optional[str] = None,
) -> None:
    """即刻追投成功：分策略限频表仅 use_count+1，不重置窗口起点。"""
    if window_seconds <= 0 or max_count <= 0:
        return
    mid = str(material_id).strip()
    sid = str(strategy_id).strip()
    if not mid or not sid:
        return

    now_str = _beijing_now_str()
    rows = db.select(
        table=RATE_LIMIT_STRATEGY_TABLE,
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
                "target_uid": _rate_target_uid(target_uid),
                "material_id": mid,
                "strategy_id": sid,
                "limit_started_at": now_str,
                "use_count": 1,
            },
        )
        return

    row = rows[0]
    try:
        use_count = int(row.get("use_count") or 0)
    except (TypeError, ValueError):
        use_count = 0

    db.update(
        table=RATE_LIMIT_STRATEGY_TABLE,
        data={"use_count": use_count + 1, "updated_at": now_str},
        where="target_uid = ? AND material_id = ? AND strategy_id = ?",
        params=(_rate_target_uid(target_uid), mid, sid),
    )


def _insert_run(
    db: SQLiteStore,
    *,
    aavid: str,
    ad_id: str,
    material_id: str,
    target_uid: str = "legacy_unscoped",
    promotion_scene: str = "live",
    plan_system: str = "unknown",
    trigger_level: str = "material",
    product_id: str = "",
    product_name: str = "",
    material_name: str = "",
    strategy_name: str = "",
    regulate_task_id: str = "",
    started_at: str,
    ended_at: str,
    duration_ms: int,
    status: int,
    step: str,
    message: str,
    detail: str,
    retargeting: Dict[str, Any],
    rule_full_json: str,
    trigger_snapshot_json: str,
    query_snapshot_json: str,
    headless: bool,
    browser_headless_rule: bool,
    trigger_source: str = "scheduler",
    cloud_task_id: str = "",
    operator_id: str = "",
    materials: Optional[List[Dict[str, Any]]] = None,
) -> None:
    _rid = str(regulate_task_id or "").strip()
    _mn = str(material_name or "").strip()
    _sn = str(strategy_name or "").strip()[:128]
    if not _sn or _sn == "?":
        _sn = None
    data: Dict[str, Any] = {
        "aavid": aavid,
        "ad_id": ad_id,
        "target_uid": _rate_target_uid(target_uid),
        "promotion_scene": str(promotion_scene or "live"),
        "plan_system": normalize_plan_system(plan_system or "unknown"),
        "trigger_level": (
            "product" if str(trigger_level or "material") == "product" else "material"
        ),
        "product_id": str(product_id or "").strip() or None,
        "product_name": str(product_name or "").strip() or None,
        "material_id": material_id,
        "material_name": _mn if _mn else None,
        "materials_json": _json_dumps(materials or []),
        "strategy_name": _sn,
        "regulate_task_id": _rid if _rid else None,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "status": status,
        "step": step,
        "message": message[:2000] if message else "",
        "detail": detail[:8000] if detail else "",
        "retargeting_method": str(retargeting.get("method") or "")[:64],
        "optimization_goal": (_optimization_goal_str(retargeting) or "")[:64] or None,
        "retargeting_json": _json_dumps(retargeting),
        "rule_full_json": rule_full_json,
        "trigger_snapshot_json": trigger_snapshot_json,
        "query_snapshot_json": query_snapshot_json,
        "headless": 1 if headless else 0,
        "browser_headless_rule": 1 if browser_headless_rule else 0,
        "trigger_source": (str(trigger_source or "scheduler").strip()[:64] or "scheduler"),
        "app_version": CURRENT_VERSION,
    }
    run_id = db.insert(table="pmc_retargeting_run", data=data)
    try:
        from api.operation_events import upsert_operation_event

        upsert_operation_event(
            {
                "event_uid": f"retarget_run:{run_id}",
                "aavid": aavid,
                "ad_id": ad_id,
                "target_uid": _rate_target_uid(target_uid),
                "promotion_scene": str(promotion_scene or "live"),
                "plan_system": normalize_plan_system(plan_system or "unknown"),
                "source": "tool_direct",
                "action_type": "retarget",
                "object_type": "assist_task" if len(materials or []) > 1 else "material",
                "object_id": _rid or material_id,
                "object_name": (
                    f"{len(materials or [])}条素材追投"
                    if len(materials or []) > 1
                    else _mn
                ),
                "plan_id": ad_id,
                "material_id": material_id,
                "material_name": _mn,
                "product_id": str(product_id or "").strip(),
                "product_name": str(product_name or "").strip(),
                "regulate_task_id": _rid,
                "status": "success" if status == 1 else "failed",
                "summary": message or "追投",
                "detail": detail,
                "after": {
                    "regulate_task_id": _rid,
                    "materials": materials or [],
                },
                "trigger_json": trigger_snapshot_json,
                "request_json": data["retargeting_json"],
                "response": {"step": step},
                "raw": {"materials": materials or []},
                "cloud_task_id": cloud_task_id,
                "operator_id": operator_id,
                "operator_name": "飞书确认用户" if operator_id else "工具",
                "occurred_at": ended_at,
            },
            db,
        )
    except Exception:
        logger.exception("%s 统一操作流水写入失败 run_id=%s", retarget_log_tag(scheduler=True), run_id)


async def run_one_cycle(db: SQLiteStore) -> None:
    _log_sched = retarget_log_tag(scheduler=True)
    cfg = load_rule_retargeting_config()
    if not cfg.get("enabled"):
        logger.info("%s 未启用 enabled，跳过本轮", _log_sched)
        return

    period = str(cfg.get("trigger_query_period") or "1h").strip() or "1h"
    strategies = cfg.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        trig = cfg.get("trigger")
        ret = cfg.get("retargeting") if isinstance(cfg.get("retargeting"), dict) else {}
        if not isinstance(trig, dict):
            logger.warning("%s trigger 非法，跳过", _log_sched)
            return
        strategies = [
            {"id": "", "title": "策略 1", "trigger": trig, "retargeting": ret},
        ]

    rule_full_json = _json_dumps(cfg)
    ws, mc = _interval_from_root_cfg(cfg)
    per_strategy_rl = bool(cfg.get("per_strategy_rate_limit"))

    dash = DashboardApi()
    resp = dash.get_table_data(
        period=period,
        sort_by="costDiff",
        sort_order="desc",
        page=1,
        page_size=_DASHBOARD_PAGE_SIZE,
    )
    if not resp.get("success"):
        logger.warning("%s get_table_data 失败: %s", _log_sched, resp.get("message"))
        return

    rows: List[Dict[str, Any]] = resp.get("data") or []
    account_name = str((dash.get_dashboard_account_label() or {}).get("label") or "").strip()
    query_at = _beijing_now_str()
    period_label = resp.get("period") or ""

    logger.info(
        "%s 周期=%s 素材总数=%s 策略数=%s 并行上限=%s",
        _log_sched,
        period,
        len(rows),
        len(strategies),
        min(MAX_STRATEGY_PARALLEL, len(strategies)),
    )

    sem = asyncio.Semaphore(MAX_STRATEGY_PARALLEL)
    browser_rule = bool(cfg.get("browser_headless", True))
    enabled_targets = db.select(
        "promotion_target",
        where={"enabled": 1},
        order_by="updated_at DESC, id DESC",
    )

    async def process_strategy(st: Dict[str, Any]) -> None:
        async with sem:
            trigger = st.get("trigger") or {}
            if not isinstance(trigger, dict):
                logger.warning("%s 策略 %s trigger 非法，跳过", _log_sched, st.get("id"))
                return
            retargeting = st.get("retargeting") or {}
            if not isinstance(retargeting, dict):
                retargeting = {}
            target_uid = str(st.get("target_uid") or "").strip()
            if not target_uid:
                # 旧版规则只在恰好一条启用计划时自动绑定；多计划时拒绝猜测。
                if len(enabled_targets) == 1:
                    target_uid = str(enabled_targets[0].get("target_uid") or "").strip()
                else:
                    logger.warning(
                        "%s 策略 %s 未选择监控计划，当前启用计划数=%s，已安全跳过",
                        _log_sched,
                        st.get("id"),
                        len(enabled_targets),
                    )
                    return
            target = next(
                (
                    item
                    for item in enabled_targets
                    if str(item.get("target_uid") or "") == target_uid
                ),
                None,
            )
            if not target:
                logger.warning(
                    "%s 策略 %s 对应监控计划不存在或已停用 target=%s",
                    _log_sched,
                    st.get("id"),
                    target_uid,
                )
                return
            target_status = str(target.get("last_status") or "").strip().lower()
            if target_status != "ok":
                logger.warning(
                    "%s 策略 %s 对应计划当前不可追投 "
                    "target=%s status=%s，已跳过历史素材",
                    _log_sched,
                    st.get("id"),
                    target_uid,
                    target_status or "unknown",
                )
                return
            promotion_scene = str(target.get("promotion_scene") or "live").strip()
            plan_system = normalize_plan_system(
                target.get("plan_system") or "unknown"
            )
            target_account_name = _account_name_for_target(
                db,
                target,
                account_name,
            )
            if plan_system == "unknown":
                logger.warning(
                    "%s 策略 %s 对应计划尚未确认是传统全域还是千川乘方，"
                    "本轮不发送卡片、不执行追投",
                    _log_sched,
                    st.get("id"),
                )
                return
            if plan_system == "chengfang":
                logger.warning(
                    "%s 策略 %s 对应千川乘方计划；乘方适配器尚未通过真实页面验证，"
                    "本轮不发送卡片、不执行追投",
                    _log_sched,
                    st.get("id"),
                )
                return
            if promotion_scene == "product":
                try:
                    capability = json.loads(target.get("capability_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    capability = {}
                if not isinstance(capability, dict) or not bool(
                    capability.get("retarget_execute")
                ):
                    logger.warning(
                        "%s 策略 %s 对应商品计划的追投表单能力尚未通过本机验证，"
                        "本轮不发送追投卡片",
                        _log_sched,
                        st.get("id"),
                    )
                    return
            target_rows = [
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("targetUid") or "") == target_uid
            ]

            st_label = str(st.get("title") or st.get("id") or "?")[:64]
            action_mode = str(st.get("action_mode") or "card_confirm").strip().lower()
            if action_mode not in ("card_confirm", "auto_execute"):
                action_mode = "card_confirm"
            _tag = retarget_log_tag(strategy_title=st_label)
            strategy_id_for_rl = str(st.get("id") or "").strip() or "__legacy__"
            ws_s, mc_s = _interval_window_and_max(retargeting)

            scoped_rows = [
                row
                for row in target_rows
                if row_is_in_test_scope(row)
                and str(row.get("id") or "").strip() not in ("", "-2")
            ]
            hit_rows: List[Dict[str, Any]] = []
            trigger_level = str(st.get("trigger_level") or "material").strip().lower()
            if trigger_level == "product":
                if promotion_scene != "product":
                    logger.warning("%s 商品级策略不能用于直播计划，已跳过", _tag)
                    return
                relation_rows = db.select(
                    "promotion_material_product",
                    fields="material_id, product_id",
                    where={"target_uid": target_uid},
                )
                relation_map: Dict[str, List[str]] = {}
                for relation in relation_rows:
                    relation_map.setdefault(
                        str(relation.get("material_id") or ""),
                        [],
                    ).append(str(relation.get("product_id") or ""))
                product_rows = db.select(
                    "promotion_product",
                    fields="product_id, product_name",
                    where={"target_uid": target_uid},
                )
                product_names = {
                    str(item.get("product_id") or ""): str(item.get("product_name") or "")
                    for item in product_rows
                }
                allowed_products = st.get("product_filter")
                if not allowed_products and str(target.get("product_filter_mode") or "all") == "selected":
                    try:
                        allowed_products = json.loads(target.get("product_ids_json") or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        allowed_products = []
                product_hits = evaluate_product_strategy(
                    scoped_rows,
                    st,
                    relation_map=relation_map,
                    product_names=product_names,
                    allowed_product_ids=allowed_products,
                )
                for product_hit in product_hits:
                    for candidate in product_hit.get("candidates") or []:
                        candidate_row = dict(candidate)
                        candidate_row["_trigger_level"] = "product"
                        candidate_row["_product_id"] = str(product_hit.get("productId") or "")
                        candidate_row["_product_name"] = str(product_hit.get("productName") or "")
                        candidate_row["_product_metrics"] = {
                            key: value
                            for key, value in product_hit.items()
                            if key not in ("materials", "candidates")
                        }
                        hit_rows.append(candidate_row)
            else:
                for row in scoped_rows:
                    if evaluate_trigger(trigger, row):
                        candidate_row = dict(row)
                        candidate_row["_trigger_level"] = "material"
                        hit_rows.append(candidate_row)

            logger.info(
                "%s target=%s scene=%s level=%s 命中候选素材数=%s",
                _tag,
                target_uid,
                promotion_scene,
                trigger_level,
                len(hit_rows),
            )
            if not hit_rows:
                return

            if action_mode == "card_confirm":
                aavid_raw = target.get("aadvid")
                aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
                ad_id = str(target.get("ad_id") or "").strip()
                try:
                    aavid_int = int(aavid)
                    ad_id_int = int(ad_id)
                except (TypeError, ValueError):
                    logger.warning(
                        "%s 监控计划缺少有效账户或计划ID，无法创建批量追投卡片",
                        _tag,
                    )
                    return

                batch_materials: List[Dict[str, Any]] = []
                material_index: Dict[str, Dict[str, Any]] = {}
                evaluation_snapshots: List[Dict[str, Any]] = []
                query_material_rows: List[Dict[str, Any]] = []
                for row in hit_rows:
                    material_id = str(row.get("id") or "").strip()
                    if not material_id:
                        continue
                    if per_strategy_rl:
                        limited = rate_limit_strategy_should_skip(
                            db,
                            material_id,
                            strategy_id_for_rl,
                            ws_s,
                            mc_s,
                            target_uid,
                        )
                    else:
                        limited = rate_limit_should_skip(
                            db,
                            material_id,
                            ws,
                            mc,
                            target_uid,
                        )
                    if limited:
                        logger.info(
                            "%s 批量卡片跳过已达限频素材 material_id=%s",
                            _tag,
                            material_id,
                        )
                        continue

                    product_id = str(row.get("_product_id") or "").strip()
                    product_name = str(row.get("_product_name") or "").strip()
                    evaluation_row = (
                        row.get("_product_metrics")
                        if trigger_level == "product"
                        and isinstance(row.get("_product_metrics"), dict)
                        else row
                    )
                    if material_id in material_index:
                        existing = material_index[material_id]
                        if product_id and product_id not in existing["product_ids"]:
                            existing["product_ids"].append(product_id)
                        continue
                    if len(batch_materials) >= 20:
                        logger.warning(
                            "%s 单个追投计划最多20条素材，其余命中素材留待本任务结束后再次提醒",
                            _tag,
                        )
                        break

                    material = {
                        "material_id": material_id,
                        "material_name": _material_name_from_dashboard_row(row),
                        "product_id": product_id,
                        "product_name": product_name,
                        "product_ids": [product_id] if product_id else [],
                    }
                    batch_materials.append(material)
                    material_index[material_id] = material
                    evaluation_snapshots.append(
                        {
                            "material_id": material_id,
                            "product_id": product_id,
                            "product_name": product_name,
                            "evaluation": build_trigger_evaluation_snapshot(
                                trigger,
                                evaluation_row,
                            ),
                        }
                    )
                    query_material_rows.append(
                        {
                            "material_id": material_id,
                            "material_name": material["material_name"],
                            "product_id": product_id,
                            "product_name": product_name,
                            "material_row": row,
                        }
                    )

                if not batch_materials:
                    return
                strategy_snapshot = {
                    "id": str(st.get("id") or ""),
                    "title": st_label,
                    "target_uid": target_uid,
                    "trigger_level": trigger_level,
                    "product_filter": st.get("product_filter") or [],
                    "candidate_trigger": st.get("candidate_trigger") or {},
                    "candidate_sort": st.get("candidate_sort") or "net_roi_desc",
                    "candidate_limit": st.get("candidate_limit") or 1,
                    "action_mode": action_mode,
                    "trigger": trigger,
                    "retargeting": retargeting,
                }
                strategy_json = json.dumps(
                    strategy_snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                strategy_hash = hashlib.sha256(
                    strategy_json.encode("utf-8")
                ).hexdigest()
                product_ids = {
                    pid
                    for material in batch_materials
                    for pid in material.get("product_ids") or []
                    if pid
                }
                first_material = batch_materials[0]
                trigger_snapshot = {
                    "strategy_id": st.get("id"),
                    "strategy_title": st.get("title"),
                    "target_uid": target_uid,
                    "promotion_scene": promotion_scene,
                    "plan_system": plan_system,
                    "trigger_level": trigger_level,
                    "trigger_config": trigger,
                    "material_count": len(batch_materials),
                    "materials": evaluation_snapshots,
                }
                query_snapshot = {
                    "query_period": period,
                    "period_label": period_label,
                    "query_at": query_at,
                    "dashboard_total": resp.get("total"),
                    "materials": query_material_rows,
                    "target": {
                        "target_uid": target_uid,
                        "aavid": aavid,
                        "ad_id": ad_id,
                        "plan_name": target.get("plan_name"),
                        "promotion_scene": promotion_scene,
                        "plan_system": plan_system,
                    },
                }
                card_payload = {
                    "aavid": str(aavid_int),
                    "account_name": target_account_name,
                    "ad_id": str(ad_id_int),
                    "target_uid": target_uid,
                    "plan_name": str(target.get("plan_name") or ""),
                    "promotion_scene": promotion_scene,
                    "plan_system": plan_system,
                    "trigger_level": trigger_level,
                    "product_id": (
                        str(first_material.get("product_id") or "")
                        if len(product_ids) == 1
                        else ""
                    ),
                    "product_name": (
                        str(first_material.get("product_name") or "")
                        if len(product_ids) == 1
                        else ""
                    ),
                    "material_id": str(first_material["material_id"]),
                    "material_name": str(first_material["material_name"]),
                    "materials": batch_materials,
                    "strategy_id": str(st.get("id") or "__legacy__"),
                    "strategy_name": st_label,
                    "strategy_hash": strategy_hash,
                    "trigger_snapshot": trigger_snapshot,
                    "query_snapshot": query_snapshot,
                    "retargeting": retargeting,
                    "rule_snapshot": strategy_snapshot,
                }
                card_result = await asyncio.to_thread(
                    create_retarget_task,
                    card_payload,
                )
                if card_result.get("success"):
                    logger.info(
                        "%s 已创建单张批量飞书确认卡片 materials=%s task_uid=%s duplicate=%s",
                        _tag,
                        len(batch_materials),
                        (card_result.get("data") or {}).get("task_uid"),
                        bool(card_result.get("duplicate")),
                    )
                else:
                    logger.warning(
                        "%s 批量飞书确认任务创建失败: %s",
                        _tag,
                        card_result.get("message") or "未知错误",
                    )
                return

            svc = QianChuanRetargetingService.from_rule_file_dict(cfg)
            headless_cfg = browser_rule
            try:
                for row in hit_rows:
                    material_id = str(row.get("id") or "").strip()
                    material_name = _material_name_from_dashboard_row(row)
                    product_id = str(row.get("_product_id") or "").strip()
                    product_name = str(row.get("_product_name") or "").strip()
                    aavid_raw = target.get("aadvid")
                    aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
                    ad_id = str(target.get("ad_id") or "").strip()

                    evaluation_row = (
                        row.get("_product_metrics")
                        if trigger_level == "product"
                        and isinstance(row.get("_product_metrics"), dict)
                        else row
                    )
                    eval_snap = build_trigger_evaluation_snapshot(trigger, evaluation_row)
                    trigger_snap = _json_dumps(
                        {
                            "strategy_id": st.get("id"),
                            "strategy_title": st.get("title"),
                            "target_uid": target_uid,
                            "promotion_scene": promotion_scene,
                            "plan_system": plan_system,
                            "trigger_level": trigger_level,
                            "product_id": product_id,
                            "product_name": product_name,
                            "trigger_config": trigger,
                            "evaluation": eval_snap,
                        }
                    )
                    query_snap = _json_dumps(
                        {
                            "query_period": period,
                            "period_label": period_label,
                            "query_at": query_at,
                            "dashboard_total": resp.get("total"),
                            "material_row": row,
                            "target": {
                                "target_uid": target_uid,
                                "aavid": aavid,
                                "ad_id": ad_id,
                                "plan_name": target.get("plan_name"),
                                "promotion_scene": promotion_scene,
                                "plan_system": plan_system,
                            },
                        }
                    )

                    # 同一素材多策略并行时，必须串行化「限频判断 → 执行 → 记成功次数」，避免竞态导致超次数
                    mat_lock = await _lock_for_material_retouch(material_id, target_uid)
                    async with mat_lock:
                        if per_strategy_rl:
                            if rate_limit_strategy_should_skip(
                                db,
                                material_id,
                                strategy_id_for_rl,
                                ws_s,
                                mc_s,
                                target_uid,
                            ):
                                logger.info(
                                    "%s 分策略限频跳过 material_id=%s strategy_id=%s（%ss 内已达 %s 次）",
                                    _tag,
                                    material_id,
                                    strategy_id_for_rl,
                                    ws_s,
                                    mc_s,
                                )
                                continue
                        else:
                            if rate_limit_should_skip(
                                db,
                                material_id,
                                ws,
                                mc,
                                target_uid,
                            ):
                                logger.info(
                                    "%s 全局限频跳过 material_id=%s（%ss 窗口内已达上限 %s 次）",
                                    _tag,
                                    material_id,
                                    ws,
                                    mc,
                                )
                                continue

                        if not ad_id:
                            now = _beijing_now_str()
                            _insert_run(
                                db,
                                aavid=aavid or "",
                                ad_id="",
                                material_id=material_id,
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                trigger_level=trigger_level,
                                product_id=product_id,
                                product_name=product_name,
                                material_name=material_name,
                                strategy_name=st_label,
                                regulate_task_id="",
                                started_at=now,
                                ended_at=now,
                                duration_ms=0,
                                status=-1,
                                step="resolve_ad_id",
                                message="监控计划中缺少 ad_id",
                                detail="",
                                retargeting=retargeting,
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            logger.warning(
                                "%s 无 ad_id aavid=%s material_id=%s",
                                _tag,
                                aavid,
                                material_id,
                            )
                            continue

                        try:
                            aavid_int = int(str(aavid_raw).strip())
                            ad_id_int = int(str(ad_id).strip())
                        except (TypeError, ValueError):
                            now = _beijing_now_str()
                            _insert_run(
                                db,
                                aavid=aavid or "",
                                ad_id=str(ad_id),
                                material_id=material_id,
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                trigger_level=trigger_level,
                                product_id=product_id,
                                product_name=product_name,
                                material_name=material_name,
                                strategy_name=st_label,
                                regulate_task_id="",
                                started_at=now,
                                ended_at=now,
                                duration_ms=0,
                                status=-1,
                                step="validate",
                                message="aavid 或 ad_id 无法转为整数",
                                detail="",
                                retargeting=retargeting,
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            continue

                        if not auto_execute_allowed_in_current_environment():
                            logger.warning(
                                "%s 本地测试模式禁止自动追投；请改为“飞书确认后追投”完成受控验收 material_id=%s",
                                _tag,
                                material_id,
                            )
                            continue

                        started_at = _beijing_now_str()
                        t0 = time.time()
                        try:
                            async with exclusive_browser_operation(
                                f"追投:{target_uid}:{material_id}"
                            ):
                                result = await svc.run(
                                    aavid=aavid_int,
                                    ad_id=ad_id_int,
                                    material_id=material_id,
                                    retargeting=retargeting,
                                    strategy_title=st_label,
                                    target_uid=target_uid,
                                    promotion_scene=promotion_scene,
                                    source_url=target.get("sanitized_page_url") or None,
                                    reuse_session=False,
                                    close_session=False,
                                )
                        except Exception:
                            ended_at = _beijing_now_str()
                            dur = int((time.time() - t0) * 1000)
                            _insert_run(
                                db,
                                aavid=str(aavid_int),
                                ad_id=str(ad_id_int),
                                material_id=material_id,
                                target_uid=target_uid,
                                promotion_scene=promotion_scene,
                                plan_system=plan_system,
                                trigger_level=trigger_level,
                                product_id=product_id,
                                product_name=product_name,
                                material_name=material_name,
                                strategy_name=st_label,
                                regulate_task_id="",
                                started_at=started_at,
                                ended_at=ended_at,
                                duration_ms=dur,
                                status=-1,
                                step="exception",
                                message="run 异常",
                                detail=traceback.format_exc()[:8000],
                                retargeting=retargeting,
                                rule_full_json=rule_full_json,
                                trigger_snapshot_json=trigger_snap,
                                query_snapshot_json=query_snap,
                                headless=headless_cfg,
                                browser_headless_rule=browser_rule,
                            )
                            logger.exception("%s run 异常 material_id=%s", _tag, material_id)
                            continue

                        ended_at = _beijing_now_str()
                        dur = int((time.time() - t0) * 1000)
                        st_ok = 1 if result.success else -1
                        detail = "" if result.success else (result.detail or "")
                        _insert_run(
                            db,
                            aavid=str(result.aavid or aavid_int),
                            ad_id=str(result.ad_id or ad_id_int),
                            material_id=str(result.material_id or material_id),
                            target_uid=target_uid,
                            promotion_scene=promotion_scene,
                            plan_system=plan_system,
                            trigger_level=trigger_level,
                            product_id=product_id,
                            product_name=product_name,
                            material_name=material_name,
                            strategy_name=st_label,
                            regulate_task_id=str(result.regulate_task_id or ""),
                            started_at=started_at,
                            ended_at=ended_at,
                            duration_ms=dur,
                            status=st_ok,
                            step=result.step,
                            message=result.message,
                            detail=detail,
                            retargeting=retargeting,
                            rule_full_json=rule_full_json,
                            trigger_snapshot_json=trigger_snap,
                            query_snapshot_json=query_snap,
                            headless=bool(result.headless),
                            browser_headless_rule=browser_rule,
                        )
                        if result.success:
                            if per_strategy_rl:
                                rate_limit_strategy_record_success(
                                    db,
                                    material_id,
                                    strategy_id_for_rl,
                                    ws_s,
                                    mc_s,
                                    target_uid,
                                )
                            # 全局表始终用根级 interval；record_success 内：若 limit_started_at+全局窗口已过期则重置窗口并记 1，否则 use_count+1
                            rate_limit_record_success(
                                db,
                                material_id,
                                ws,
                                mc,
                                target_uid,
                            )
                        logger.info(
                            "%s material_id=%s success=%s step=%s",
                            _tag,
                            material_id,
                            result.success,
                            result.step,
                        )
            finally:
                await svc.close()

    await asyncio.gather(*(process_strategy(st) for st in strategies))


async def main_loop(interval_sec: int = DEFAULT_INTERVAL_SEC) -> None:
    init_sqlite_schema()
    db = SQLiteStore()
    logger.info(
        "%s 启动，间隔 %ss，版本 %s",
        retarget_log_tag(scheduler=True),
        interval_sec,
        CURRENT_VERSION,
    )
    while True:
        try:
            await run_one_cycle(db)
        except Exception:
            logger.exception("%s 本轮未捕获异常", retarget_log_tag(scheduler=True))
        await asyncio.sleep(max(1, int(interval_sec)))


def _gui_background_target() -> None:
    """GUI 内嵌：在独立线程中跑 asyncio 事件循环（与命令行 `main()` 等价）。"""
    try:
        asyncio.run(main_loop())
    except Exception:
        logger.exception("%s 后台线程异常退出", retarget_log_tag(scheduler=True))


def start_retargeting_rule_runner_background_thread() -> threading.Thread:
    """
    供 gui_app 等调用：启动守护线程，逻辑与 `python -m services.retargeting_rule_runner` 一致。
    `rule_retargeting.json` 未启用 enabled 时每轮仅快速跳过。
    """
    t = threading.Thread(
        target=_gui_background_target,
        name="retargeting-rule-runner",
        daemon=True,
    )
    t.start()
    logger.info(
        "%s 后台线程已启动（GUI，间隔 %ss，可用 RETARGET_RULE_INTERVAL_SEC 覆盖）",
        retarget_log_tag(scheduler=True),
        DEFAULT_INTERVAL_SEC,
    )
    return t
