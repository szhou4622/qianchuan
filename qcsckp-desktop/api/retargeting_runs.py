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
)
from services.promotion_browser_lock import exclusive_browser_operation
from services.promotion_capability import (
    MANUAL_RETARGET_PROBE_VERSION,
    record_target_capability,
)
from services.plan_system import normalize_plan_system
from services.retargeting_service import (
    QianChuanRetargetingService,
    RetargetingRunResult,
    RetargetingSessionOptions,
    retargeting_block_from_full_config,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore



def _record_manual_retarget_capability_if_verified(
    db: SQLiteStore,
    result: RetargetingRunResult,
    *,
    target_uid: str,
    promotion_scene: str,
    plan_system: str,
    verified_at: str,
) -> bool:
    """仅在真实提交成功时写能力证据；返回是否发生写入。"""
    if not result.success or result.step != "done":
        return False
    record_target_capability(
        db,
        target_uid=target_uid,
        action="retarget",
        promotion_scene=promotion_scene,
        plan_system=plan_system,
        probe_version=MANUAL_RETARGET_PROBE_VERSION,
        verified_at=verified_at,
    )
    return True


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
        "SELECT aadvid, video_name FROM pmc_promotion_material_latest WHERE material_id = ? "
        "ORDER BY collected_at DESC LIMIT 1",
        (mid,),
        fetch=True,
    )
    return rows[0] if rows else None


def _lookup_latest_material_full(
    db: SQLiteStore,
    material_id: str,
    target_uid: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """素材最新状态（与采集入库字段一致），用于即刻追投 query_snapshot。"""
    mid = str(material_id).strip()
    if not mid:
        return None
    uid = str(target_uid or "").strip()
    if uid:
        rows = db.execute(
            "SELECT * FROM pmc_promotion_material_latest WHERE target_uid = ? AND material_id = ? "
            "ORDER BY collected_at DESC LIMIT 1",
            (uid, mid),
            fetch=True,
        )
    else:
        rows = db.execute(
            "SELECT * FROM pmc_promotion_material_latest WHERE material_id = ? ORDER BY collected_at DESC LIMIT 1",
            (mid,),
            fetch=True,
        )
    return rows[0] if rows else None


def _pmc_record_to_dashboard_material_row(
    r: Dict[str, Any], material_name_fallback: str
) -> Dict[str, Any]:
    """
    将本地库单条素材记录映射为与 dashboard get_table_data 输出一致的字段名，供「素材信息」弹窗渲染。
    即刻追投仅取该素材最新一条；单点无周期基线，时段流速为未知。
    """
    mid = str(r.get("material_id") or "").strip()
    title_raw = r.get("video_name")
    if title_raw is None or str(title_raw).strip() == "":
        title = (material_name_fallback or "未命名").strip() or "未命名"
    else:
        title = str(title_raw).strip()[:512]

    def _f(v: Any) -> Optional[float]:
        try:
            if v is None or str(v).strip() == "":
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    def _i(v: Any) -> Optional[int]:
        try:
            if v is None or str(v).strip() == "":
                return None
            return int(v)
        except (TypeError, ValueError):
            return None

    cost = _f(r.get("stat_cost"))
    ct = r.get("video_create_time") or r.get("upload_time")
    ca = r.get("collected_at") or r.get("created_at")
    return {
        "id": mid,
        "aadvid": r.get("aadvid"),
        "targetUid": r.get("target_uid"),
        "adId": r.get("ad_id"),
        "promotionScene": r.get("promotion_scene"),
        "productIds": r.get("product_ids_json"),
        "title": title,
        "createTime": ct,
        "currentCost": cost,
        "costDiff": None,
        "velocity": None,
        "estimatedEcpm": None,
        "maxCostTime": ca,
        "minCostTime": ca,
        "netRoi": _f(r.get("prepay_pay_settle_1h")),
        "netAmount": _f(r.get("order_settle_amount_1h")),
        "hourRefundRate": _f(r.get("refund_rate_1h")),
        "netSettleRate": _f(r.get("order_settle_rate_1h")),
        "netOrderCount": _i(r.get("order_settle_count_1h")),
        "overallPayRoi": _f(r.get("prepay_pay_order_count")),
        "overallAmount": _f(r.get("pay_gmv_include_coupon")),
        "overallOrderCount": _i(r.get("overall_order_count")),
        "overallShowCount": _i(r.get("overall_show_count")),
        "overallClickCount": _i(r.get("overall_click_count")),
        "overallCtr": _f(r.get("overall_ctr")),
        "overallConversionRate": _f(r.get("overall_conversion_rate")),
        "periodStartTime": r.get("stat_date"),
        "periodEndTime": ca,
    }


def _manual_query_snapshot_json(
    db: SQLiteStore,
    material_id: str,
    material_name: str,
    target_uid: Optional[str] = None,
) -> str:
    """即刻追投：query 快照以本地库该素材最新一条为准（对齐规则追投 material_row 结构）。"""
    now = _beijing_now_str()
    mid = str(material_id).strip()
    full = _lookup_latest_material_full(db, mid, target_uid)
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
    target_uid: Optional[str] = None,
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
    requested_target_uid = str(target_uid or "").strip()
    if not requested_target_uid:
        return {
            "success": False,
            "message": "即刻追投必须指定监控计划，请先在规则中选择计划",
        }
    mat_row = _lookup_latest_material_full(db, mid, requested_target_uid or None)
    if not mat_row:
        return {
            "success": False,
            "message": "本地库中未找到该素材 ID，请先由采集服务写入素材数据",
        }

    aavid_raw = mat_row.get("aadvid")
    aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
    resolved_target_uid = str(
        requested_target_uid or mat_row.get("target_uid") or ""
    ).strip()
    if not resolved_target_uid or resolved_target_uid == "legacy_unscoped":
        return {
            "success": False,
            "message": "该素材未归属明确的监控计划，请先在“监控计划”中打开并识别计划",
        }
    target = db.select_one(
        "promotion_target", where={"target_uid": resolved_target_uid}
    )
    if not target or not int(target.get("enabled") or 0):
        return {
            "success": False,
            "message": "素材所属监控计划不存在或已停用，已阻止追投",
        }
    promotion_scene = str(target.get("promotion_scene") or "live").strip().lower()
    plan_system = normalize_plan_system(target.get("plan_system") or "unknown")
    if plan_system == "unknown":
        return {
            "success": False,
            "message": "计划体系尚未识别，已阻止追投；请重新打开计划详情完成识别",
        }
    # “即刻追投”使用可见浏览器，由用户核对表单并亲自点击提交，且仍会
    # 复核账户、计划、场景和素材。它是建立 scoped 能力证据的受控验证
    # 入口，因此不要求目标预先已有能力证据；规则发卡和后台执行仍严格
    # 经过 services.promotion_capability 的目标级 gate。
    source_url = str(target.get("sanitized_page_url") or "").strip() or None
    target_ad_id = str(target.get("ad_id") or "").strip()
    if (
        str(target.get("aadvid") or "").strip() != aavid
        or str(mat_row.get("ad_id") or "").strip() != target_ad_id
    ):
        return {
            "success": False,
            "message": "素材与监控计划不匹配，已阻止追投",
        }
    material_name = ""
    vn = mat_row.get("video_name")
    if vn is not None:
        material_name = str(vn).strip()[:512]

    q_snap = _manual_query_snapshot_json(
        db, mid, material_name, resolved_target_uid
    )

    ad_id = target_ad_id
    if not ad_id:
        now = _beijing_now_str()
        _insert_run(
            db,
            aavid=aavid or "",
            ad_id="",
            material_id=mid,
            target_uid=resolved_target_uid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
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
            target_uid=resolved_target_uid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
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
                async with exclusive_browser_operation(
                    f"即刻追投:{resolved_target_uid}:{mid}"
                ):
                    r = await svc.run_prepare_for_manual_submit(
                        aavid=aavid_int,
                        ad_id=ad_id_int,
                        material_id=mid,
                        retargeting=rt,
                        target_uid=resolved_target_uid,
                        promotion_scene=promotion_scene,
                        plan_system=plan_system,
                        source_url=source_url,
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
        target_uid=resolved_target_uid,
        promotion_scene=promotion_scene,
        plan_system=plan_system,
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

    # 只有千川创建调控任务接口返回成功才提升目标能力。填表失败、用户未
    # 提交、接口失败或超时均不会写入证据。
    try:
        _record_manual_retarget_capability_if_verified(
            db,
            r,
            target_uid=resolved_target_uid,
            promotion_scene=promotion_scene,
            plan_system=plan_system,
            verified_at=ended_at,
        )
    except Exception:
        logger.exception(
            "[即刻追投] 写入目标级追投能力证据失败 target=%s",
            resolved_target_uid,
        )

    if r.success:
        per_str = bool(cfg.get("per_strategy_rate_limit"))
        if per_str:
            strats = cfg.get("strategies")
            st0 = strats[0] if isinstance(strats, list) and strats else {}
            sid = str((st0 or {}).get("id") or "").strip() or "__legacy__"
            rate_limit_increment_manual_only_strategy(
                db, mid, sid, ws, mc, resolved_target_uid
            )
            ws_g, mc_g = _interval_from_root_cfg(cfg)
            rate_limit_record_success(
                db, mid, ws_g, mc_g, resolved_target_uid
            )
        else:
            rate_limit_increment_manual_only(
                db, mid, ws, mc, resolved_target_uid
            )

    return {
        "success": bool(r.success),
        "message": r.message,
        "step": r.step,
        "detail": detail,
        "material_id": str(r.material_id or mid),
        "aavid": str(r.aavid or aavid_int),
        "ad_id": str(r.ad_id or ad_id_int),
        "target_uid": resolved_target_uid,
        "promotion_scene": promotion_scene,
        "plan_system": plan_system,
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
    # video_type：取最新状态表中与流水同素材+同广告主的一条（与大盘口径一致）
    fields = (
        "id, aavid, ad_id, material_id, material_name, strategy_name, started_at, ended_at, duration_ms, status, step, message, "
        "retargeting_method, optimization_goal, regulate_task_id, trigger_source, created_at, "
        "(SELECT m.video_type FROM pmc_promotion_material_latest m "
        "WHERE m.material_id = pmc_retargeting_run.material_id AND m.aadvid = pmc_retargeting_run.aavid "
        "ORDER BY m.collected_at DESC LIMIT 1) AS video_type"
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
        "(SELECT m.video_type FROM pmc_promotion_material_latest m "
        "WHERE m.material_id = r.material_id AND m.aadvid = r.aavid "
        "ORDER BY m.collected_at DESC LIMIT 1) AS video_type "
        "FROM pmc_retargeting_run r WHERE r.id = ?",
        (rid,),
        fetch=True,
    )
    return rows[0] if rows else None
