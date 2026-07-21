# -*- coding: utf-8 -*-
"""
追投执行流水（pmc_retargeting_run）列表与详情查询。
筛选、分页 SQL 放在此模块，SQLiteStore 仅负责连接与通用 execute。
即刻追投（打开浏览器填表、用户手动提交）入口：run_immediate_retarget_prepare。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from api.rule_retargeting_config import load_rule_retargeting_config
from config import CURRENT_VERSION
from services.retargeting_rule_runner import (
    _insert_run,
    _interval_from_root_cfg,
    _interval_window_and_max,
    rate_limit_increment_manual_only,
    rate_limit_increment_manual_only_strategy,
    rate_limit_record_success,
    resolve_ad_id_for_aavid,
)
from services.retargeting_service import (
    QianChuanRetargetingService,
    RetargetingRunResult,
    RetargetingSessionOptions,
    retargeting_block_from_full_config,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore


def _store() -> SQLiteStore:
    return SQLiteStore()


def _beijing_now_str() -> str:
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _default_strategy_display_name(cfg: Dict[str, Any]) -> str:
    """即刻追投等与「首条策略」对齐的展示名；无配置时与规则页默认一致为「策略 1」。"""
    strategies = cfg.get("strategies")
    if isinstance(strategies, list) and strategies:
        first = strategies[0]
        if isinstance(first, dict):
            t = str(first.get("title") or "").strip()
            if t:
                return t[:128]
    return "策略 1"


_IMMEDIATE_THREAD_LOCK = threading.Lock()
_IMMEDIATE_THREAD: Optional[threading.Thread] = None


def _lookup_latest_material(db: SQLiteStore, material_id: str) -> Optional[Dict[str, Any]]:
    mid = str(material_id).strip()
    if not mid:
        return None
    rows = db.execute(
        "SELECT aadvid, video_name FROM pmc_promotion_material WHERE material_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (mid,),
        fetch=True,
    )
    return rows[0] if rows else None


def _lookup_latest_material_full(db: SQLiteStore, material_id: str) -> Optional[Dict[str, Any]]:
    """pmc_promotion_material 该素材最新一条（与采集入库字段一致），用于即刻追投 query_snapshot。"""
    mid = str(material_id).strip()
    if not mid:
        return None
    rows = db.execute(
        "SELECT * FROM pmc_promotion_material WHERE material_id = ? ORDER BY created_at DESC LIMIT 1",
        (mid,),
        fetch=True,
    )
    return rows[0] if rows else None


def _pmc_record_to_dashboard_material_row(
    r: Dict[str, Any], material_name_fallback: str
) -> Dict[str, Any]:
    """
    将本地库单条素材记录映射为与 dashboard get_table_data 输出一致的字段名，供「素材信息」弹窗渲染。
    即刻追投仅取该素材 created_at 最新一条；单点无周期首尾差，时段流速相关为 0。
    """
    mid = str(r.get("material_id") or "").strip()
    title_raw = r.get("video_name")
    if title_raw is None or str(title_raw).strip() == "":
        title = (material_name_fallback or "未命名").strip() or "未命名"
    else:
        title = str(title_raw).strip()[:512]

    def _f(v: Any, default: float = 0.0) -> float:
        try:
            if v is None or str(v).strip() == "":
                return default
            return float(v)
        except (TypeError, ValueError):
            return default

    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    sc = r.get("stat_cost")
    try:
        cost = float(sc) if sc is not None and str(sc).strip() != "" else 0.0
    except (TypeError, ValueError):
        cost = 0.0
    ct = r.get("video_create_time") or r.get("upload_time")
    ca = r.get("created_at")
    return {
        "id": mid,
        "aadvid": r.get("aadvid"),
        "title": title,
        "createTime": ct,
        "currentCost": cost,
        "costDiff": 0.0,
        "velocity": 0.0,
        "estimatedEcpm": None,
        "maxCostTime": ca,
        "minCostTime": ca,
        "netRoi": _f(r.get("prepay_pay_settle_1h"), 0.0),
        "netAmount": _f(r.get("order_settle_amount_1h"), 0.0),
        "hourRefundRate": _f(r.get("refund_rate_1h"), 0.0),
        "netSettleRate": _f(r.get("order_settle_rate_1h"), 0.0),
        "netOrderCount": _i(r.get("order_settle_count_1h")),
        "overallPayRoi": _f(r.get("prepay_pay_order_count"), 0.0),
        "overallAmount": _f(r.get("pay_gmv_include_coupon"), 0.0),
        "overallOrderCount": _i(r.get("overall_order_count")),
        "overallShowCount": _i(r.get("overall_show_count")),
        "overallClickCount": _i(r.get("overall_click_count")),
        "overallCtr": _f(r.get("overall_ctr"), 0.0),
        "overallConversionRate": _f(r.get("overall_conversion_rate"), 0.0),
        "periodStartTime": r.get("stat_date"),
        "periodEndTime": ca,
    }


def _manual_query_snapshot_json(db: SQLiteStore, material_id: str, material_name: str) -> str:
    """即刻追投：query 快照以本地库该素材最新一条为准（对齐规则追投 material_row 结构）。"""
    now = _beijing_now_str()
    mid = str(material_id).strip()
    full = _lookup_latest_material_full(db, mid)
    if not full:
        return _json_dumps(
            {
                "note": "即刻追投",
                "material_id": mid,
                "query_at": now,
                "period_label": "即刻追投",
                "material_row": None,
            }
        )
    mr = _pmc_record_to_dashboard_material_row(full, material_name)
    return _json_dumps(
        {
            "note": "即刻追投",
            "material_id": mid,
            "query_at": now,
            "period_label": "即刻追投 · 本地库该素材最新一条",
            "material_row": mr,
        }
    )


def run_immediate_retarget_prepare(
    *,
    material_id: str,
    retargeting: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    即刻追投：有头浏览器打开投放页、填表，不自动点击「提交」；成功后写 pmc_retargeting_run，
    并对限频表仅做 use_count +1（不重置窗口起点）。需在 GUI 进程内调用（Playwright）。
    """
    global _IMMEDIATE_THREAD

    mid = str(material_id or "").strip()
    if not mid:
        return {"success": False, "message": "请填写素材 ID"}

    with _IMMEDIATE_THREAD_LOCK:
        if _IMMEDIATE_THREAD is not None and _IMMEDIATE_THREAD.is_alive():
            return {
                "success": False,
                "message": "已有即刻追投会话进行中，请先关闭浏览器窗口后再试",
            }

    cfg = load_rule_retargeting_config()
    _be_path = str(cfg.get("browser_executable_path") or "").strip()
    _browser_executable_for_session = _be_path if _be_path else None
    _strategy_name_immediate = _default_strategy_display_name(cfg)
    rule_full_json = _json_dumps(cfg)
    base_rt = retargeting_block_from_full_config(cfg)
    rt_in = retargeting if isinstance(retargeting, dict) else {}
    if not rt_in:
        rt = base_rt
    else:
        rt = {**base_rt, **rt_in}
        if not isinstance(rt.get("interval"), dict):
            rt["interval"] = base_rt.get("interval") or {}

    ws, mc = _interval_window_and_max(rt)

    db = _store()
    mat_row = _lookup_latest_material_full(db, mid)
    if not mat_row:
        return {
            "success": False,
            "message": "本地库中未找到该素材 ID，请先由采集服务写入素材数据",
        }

    aavid_raw = mat_row.get("aadvid")
    aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
    material_name = ""
    vn = mat_row.get("video_name")
    if vn is not None:
        material_name = str(vn).strip()[:512]

    q_snap = _manual_query_snapshot_json(db, mid, material_name)

    ad_id = resolve_ad_id_for_aavid(db, aavid) if aavid else None
    if not ad_id:
        now = _beijing_now_str()
        _insert_run(
            db,
            aavid=aavid or "",
            ad_id="",
            material_id=mid,
            material_name=material_name,
            strategy_name=_strategy_name_immediate,
            regulate_task_id="",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            status=-1,
            step="resolve_ad_id",
            message="pmc_ad_detail_basic 中无该 aadvid 对应的 ad_id",
            detail="",
            retargeting=rt,
            rule_full_json=rule_full_json,
            trigger_snapshot_json=_json_dumps({"source": "manual", "material_id": mid}),
            query_snapshot_json=q_snap,
            headless=False,
            browser_headless_rule=bool(cfg.get("browser_headless", True)),
            trigger_source="manual",
        )
        return {"success": False, "message": "无法解析计划 ad_id，请检查 pmc_ad_detail_basic 数据"}

    try:
        aavid_int = int(str(aavid_raw).strip())
        ad_id_int = int(str(ad_id).strip())
    except (TypeError, ValueError):
        now = _beijing_now_str()
        _insert_run(
            db,
            aavid=aavid or "",
            ad_id=str(ad_id),
            material_id=mid,
            material_name=material_name,
            strategy_name=_strategy_name_immediate,
            regulate_task_id="",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            status=-1,
            step="validate",
            message="aavid 或 ad_id 无法转为整数",
            detail="",
            retargeting=rt,
            rule_full_json=rule_full_json,
            trigger_snapshot_json=_json_dumps({"source": "manual", "material_id": mid}),
            query_snapshot_json=q_snap,
            headless=False,
            browser_headless_rule=bool(cfg.get("browser_headless", True)),
            trigger_source="manual",
        )
        return {"success": False, "message": "aavid 或 ad_id 无效"}

    result_holder: List[Optional[RetargetingRunResult]] = [None]

    def _thread_target() -> None:
        async def main() -> None:
            svc: Optional[QianChuanRetargetingService] = None
            try:
                svc = QianChuanRetargetingService(
                    RetargetingSessionOptions(
                        headless=False,
                        storage_state=None,
                        browser_executable_path=_browser_executable_for_session,
                    )
                )
                r = await svc.run_prepare_for_manual_submit(
                    aavid=aavid_int,
                    ad_id=ad_id_int,
                    material_id=mid,
                    retargeting=rt,
                )
                result_holder[0] = r
            except Exception:
                logger.exception("[即刻追投] 线程内异常")
                result_holder[0] = RetargetingRunResult(
                    success=False,
                    message="即刻追投线程异常",
                    step="exception",
                    detail=traceback.format_exc()[:8000],
                    aavid=str(aavid_int),
                    ad_id=str(ad_id_int),
                    material_id=mid,
                    finished_at=_beijing_now_str(),
                    headless=False,
                )
                if svc is not None:
                    try:
                        await svc.close()
                    except Exception:
                        pass

        asyncio.run(main())

    started_at = _beijing_now_str()
    t0 = time.time()
    th = threading.Thread(target=_thread_target, name="immediate-retarget", daemon=True)
    with _IMMEDIATE_THREAD_LOCK:
        _IMMEDIATE_THREAD = th
    th.start()

    deadline = time.time() + 600.0
    while time.time() < deadline and result_holder[0] is None:
        time.sleep(0.05)

    r = result_holder[0]
    ended_at = _beijing_now_str()
    dur_ms = int((time.time() - t0) * 1000)

    if r is None:
        return {"success": False, "message": "即刻追投超时（600s）或线程无响应"}

    st = 1 if r.success else -1
    detail = "" if r.success else (r.detail or "")
    _insert_run(
        db,
        aavid=str(r.aavid or aavid_int),
        ad_id=str(r.ad_id or ad_id_int),
        material_id=str(r.material_id or mid),
        material_name=material_name,
        strategy_name=_strategy_name_immediate,
        regulate_task_id=str(r.regulate_task_id or ""),
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=dur_ms,
        status=st,
        step=r.step,
        message=r.message,
        detail=detail,
        retargeting=rt,
        rule_full_json=rule_full_json,
        trigger_snapshot_json=_json_dumps({"source": "manual", "material_id": mid}),
        query_snapshot_json=q_snap,
        headless=bool(r.headless),
        browser_headless_rule=bool(cfg.get("browser_headless", True)),
        trigger_source="manual",
    )

    if r.success:
        per_str = bool(cfg.get("per_strategy_rate_limit"))
        if per_str:
            strats = cfg.get("strategies")
            st0 = strats[0] if isinstance(strats, list) and strats else {}
            sid = str((st0 or {}).get("id") or "").strip() or "__legacy__"
            rate_limit_increment_manual_only_strategy(db, mid, sid, ws, mc)
            ws_g, mc_g = _interval_from_root_cfg(cfg)
            rate_limit_record_success(db, mid, ws_g, mc_g)
        else:
            rate_limit_increment_manual_only(db, mid, ws, mc)

    return {
        "success": bool(r.success),
        "message": r.message,
        "step": r.step,
        "detail": detail,
        "material_id": str(r.material_id or mid),
        "aavid": str(r.aavid or aavid_int),
        "ad_id": str(r.ad_id or ad_id_int),
        "app_version": CURRENT_VERSION,
    }


def query_pmc_retargeting_runs_page(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    retargeting_method: Optional[str] = None,
    status: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[int, List[Dict[str, Any]]]:
    """
    追投执行流水分页查询（不含大 JSON 列，供列表展示）。
    started_at 为北京时间字符串，字典序比较有效。
    q：模糊匹配 material_id、material_name；retargeting_method：精确匹配。
    """
    tbl = "pmc_retargeting_run"
    # video_type：取 pmc_promotion_material 中与流水同素材+同广告主最新一条（与大盘素材类型口径一致）
    fields = (
        "id, aavid, ad_id, material_id, material_name, strategy_name, started_at, ended_at, duration_ms, status, step, message, "
        "retargeting_method, optimization_goal, regulate_task_id, trigger_source, created_at, "
        "(SELECT m.video_type FROM pmc_promotion_material m "
        "WHERE m.material_id = pmc_retargeting_run.material_id AND m.aadvid = pmc_retargeting_run.aavid "
        "ORDER BY m.created_at DESC LIMIT 1) AS video_type"
    )
    where_parts: List[str] = []
    params: List[Any] = []

    if date_from and str(date_from).strip():
        where_parts.append("started_at >= ?")
        params.append(str(date_from).strip())
    if date_to and str(date_to).strip():
        where_parts.append("started_at <= ?")
        params.append(str(date_to).strip())

    if status is not None:
        try:
            st = int(status)
            if st in (-1, 1):
                where_parts.append("status = ?")
                params.append(st)
        except (TypeError, ValueError):
            pass

    rm = (retargeting_method or "").strip()
    if rm:
        where_parts.append("retargeting_method = ?")
        params.append(rm)

    qv = (q or "").strip()
    if qv:
        like = f"%{qv}%"
        where_parts.append("(CAST(material_id AS TEXT) LIKE ? OR IFNULL(material_name,'') LIKE ?)")
        params.extend([like, like])

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        ps = int(page_size)
    except (TypeError, ValueError):
        ps = 20
    ps = max(1, min(ps, 100))
    offset = (page - 1) * ps

    count_sql = f"SELECT COUNT(*) AS c FROM {tbl}{where_sql}"
    data_sql = f"SELECT {fields} FROM {tbl}{where_sql} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?"

    db = _store()
    count_rows = db.execute(count_sql, tuple(params), fetch=True)
    total = int(count_rows[0]["c"]) if count_rows else 0
    items = db.execute(data_sql, tuple(params + [ps, offset]), fetch=True) or []
    return total, items


def get_pmc_retargeting_run_by_id(run_id: Any) -> Optional[Dict[str, Any]]:
    """单条追投流水（含 JSON 快照列），供详情弹窗。"""
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        return None
    if rid < 1:
        return None
    db = _store()
    rows = db.execute(
        "SELECT r.*, "
        "(SELECT m.video_type FROM pmc_promotion_material m "
        "WHERE m.material_id = r.material_id AND m.aadvid = r.aavid "
        "ORDER BY m.created_at DESC LIMIT 1) AS video_type "
        "FROM pmc_retargeting_run r WHERE r.id = ?",
        (rid,),
        fetch=True,
    )
    return rows[0] if rows else None
