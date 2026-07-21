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
from config import CURRENT_VERSION
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


async def _lock_for_material_retouch(material_id: str) -> asyncio.Lock:
    mid = str(material_id).strip()
    if not mid:
        mid = "__empty_material__"
    async with _material_retouch_locks_guard:
        if mid not in _material_retouch_locks:
            _material_retouch_locks[mid] = asyncio.Lock()
        return _material_retouch_locks[mid]


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


def rate_limit_should_skip(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
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
        where="material_id = ?",
        params=(material_id,),
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
        where="material_id = ?",
        params=(material_id,),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_TABLE,
            data={
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
            where="material_id = ?",
            params=(material_id,),
        )
        return

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        db.update(
            table=RATE_LIMIT_TABLE,
            data={"use_count": use_count + 1, "updated_at": now_str},
            where="material_id = ?",
            params=(material_id,),
        )
        return

    db.update(
        table=RATE_LIMIT_TABLE,
        data={
            "limit_started_at": now_str,
            "use_count": 1,
            "updated_at": now_str,
        },
        where="material_id = ?",
        params=(material_id,),
    )


def rate_limit_strategy_should_skip(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
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
        where="material_id = ? AND strategy_id = ?",
        params=(mid, sid),
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
        where="material_id = ? AND strategy_id = ?",
        params=(mid, sid),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
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
            where="material_id = ? AND strategy_id = ?",
            params=(mid, sid),
        )
        return

    window_end = start_dt + timedelta(seconds=window_seconds)
    if window_end > now_dt:
        db.update(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={"use_count": use_count + 1, "updated_at": now_str},
            where="material_id = ? AND strategy_id = ?",
            params=(mid, sid),
        )
        return

    db.update(
        table=RATE_LIMIT_STRATEGY_TABLE,
        data={
            "limit_started_at": now_str,
            "use_count": 1,
            "updated_at": now_str,
        },
        where="material_id = ? AND strategy_id = ?",
        params=(mid, sid),
    )


def rate_limit_increment_manual_only(
    db: SQLiteStore,
    material_id: str,
    window_seconds: int,
    max_count: int,
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
        where="material_id = ?",
        params=(material_id,),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_TABLE,
            data={
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
        where="material_id = ?",
        params=(material_id,),
    )


def rate_limit_increment_manual_only_strategy(
    db: SQLiteStore,
    material_id: str,
    strategy_id: str,
    window_seconds: int,
    max_count: int,
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
        where="material_id = ? AND strategy_id = ?",
        params=(mid, sid),
        limit=1,
    )

    if not rows:
        db.insert(
            table=RATE_LIMIT_STRATEGY_TABLE,
            data={
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
        where="material_id = ? AND strategy_id = ?",
        params=(mid, sid),
    )


def _insert_run(
    db: SQLiteStore,
    *,
    aavid: str,
    ad_id: str,
    material_id: str,
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
) -> None:
    _rid = str(regulate_task_id or "").strip()
    _mn = str(material_name or "").strip()
    _sn = str(strategy_name or "").strip()[:128]
    if not _sn or _sn == "?":
        _sn = None
    data: Dict[str, Any] = {
        "aavid": aavid,
        "ad_id": ad_id,
        "material_id": material_id,
        "material_name": _mn if _mn else None,
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
    db.insert(table="pmc_retargeting_run", data=data)


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

    async def process_strategy(st: Dict[str, Any]) -> None:
        async with sem:
            trigger = st.get("trigger") or {}
            if not isinstance(trigger, dict):
                logger.warning("%s 策略 %s trigger 非法，跳过", _log_sched, st.get("id"))
                return
            retargeting = st.get("retargeting") or {}
            if not isinstance(retargeting, dict):
                retargeting = {}

            st_label = str(st.get("title") or st.get("id") or "?")[:64]
            _tag = retarget_log_tag(strategy_title=st_label)
            strategy_id_for_rl = str(st.get("id") or "").strip() or "__legacy__"
            ws_s, mc_s = _interval_window_and_max(retargeting)

            hit_rows: List[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                _mid = str(row.get("id") or "").strip()
                if _mid == "-2":
                    continue
                if evaluate_trigger(trigger, row):
                    hit_rows.append(row)

            logger.info("%s 命中素材数=%s", _tag, len(hit_rows))
            if not hit_rows:
                return

            svc = QianChuanRetargetingService.from_rule_file_dict(cfg)
            headless_cfg = browser_rule
            try:
                for row in hit_rows:
                    material_id = str(row.get("id") or "").strip()
                    material_name = _material_name_from_dashboard_row(row)
                    aavid_raw = row.get("aadvid")
                    aavid = str(aavid_raw).strip() if aavid_raw is not None else ""

                    eval_snap = build_trigger_evaluation_snapshot(trigger, row)
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
                            "query_period": period,
                            "period_label": period_label,
                            "query_at": query_at,
                            "dashboard_total": resp.get("total"),
                            "material_row": row,
                        }
                    )

                    # 同一素材多策略并行时，必须串行化「限频判断 → 执行 → 记成功次数」，避免竞态导致超次数
                    mat_lock = await _lock_for_material_retouch(material_id)
                    async with mat_lock:
                        if per_strategy_rl:
                            if rate_limit_strategy_should_skip(
                                db, material_id, strategy_id_for_rl, ws_s, mc_s
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
                            if rate_limit_should_skip(db, material_id, ws, mc):
                                logger.info(
                                    "%s 全局限频跳过 material_id=%s（%ss 窗口内已达上限 %s 次）",
                                    _tag,
                                    material_id,
                                    ws,
                                    mc,
                                )
                                continue

                        ad_id = resolve_ad_id_for_aavid(db, aavid) if aavid else None
                        if not ad_id:
                            now = _beijing_now_str()
                            _insert_run(
                                db,
                                aavid=aavid or "",
                                ad_id="",
                                material_id=material_id,
                                material_name=material_name,
                                strategy_name=st_label,
                                regulate_task_id="",
                                started_at=now,
                                ended_at=now,
                                duration_ms=0,
                                status=-1,
                                step="resolve_ad_id",
                                message="pmc_ad_detail_basic 中无该 aadvid 对应的 ad_id",
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

                        started_at = _beijing_now_str()
                        t0 = time.time()
                        try:
                            result = await svc.run(
                                aavid=aavid_int,
                                ad_id=ad_id_int,
                                material_id=material_id,
                                retargeting=retargeting,
                                strategy_title=st_label,
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
                                    db, material_id, strategy_id_for_rl, ws_s, mc_s
                                )
                            # 全局表始终用根级 interval；record_success 内：若 limit_started_at+全局窗口已过期则重置窗口并记 1，否则 use_count+1
                            rate_limit_record_success(db, material_id, ws, mc)
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


