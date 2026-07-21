# -*- coding: utf-8 -*-
"""
规则化停投执行流水（pmc_regulation_run）列表与详情查询。
手动停投（打开投放页定位任务、用户手动点暂停/删除）入口：run_immediate_regulation_stop_prepare。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from api.retargeting_runs import _json_dumps
from api.rule_regulation_config import load_rule_regulation_config
from config import CURRENT_VERSION
from services.regulation_rule_runner import _insert_regulation_run
from services.regulation_service import (
    QianChuanRegulationStopService,
    RegulationRunResult,
    RegulationSessionOptions,
)
from utils.log import logger
from utils.sqlite_store import SQLiteStore


def _store() -> SQLiteStore:
    return SQLiteStore()


def _beijing_now_str() -> str:
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def _default_strategy_display_name_reg(cfg: Dict[str, Any]) -> str:
    strategies = cfg.get("strategies")
    if isinstance(strategies, list) and strategies:
        first = strategies[0]
        if isinstance(first, dict):
            t = str(first.get("title") or "").strip()
            if t:
                return t[:128]
    return "策略 1"


def _lookup_roi2_assist_task_row(db: SQLiteStore, assist_task_id: str) -> Optional[Dict[str, Any]]:
    """拉取 pmc_roi2_assist_task 整行（含全部指标列），供解析 aadvid/ad_id 与 query_snapshot 展示。"""
    aid = str(assist_task_id or "").strip()
    if not aid:
        return None
    rows = db.execute(
        "SELECT * FROM pmc_roi2_assist_task WHERE assist_task_id = ? LIMIT 1",
        (aid,),
        fetch=True,
    )
    return dict(rows[0]) if rows else None


def _manual_regulation_query_snapshot_from_roi2_row(
    assist_task_id: str, row: Dict[str, Any]
) -> str:
    """手动停投：assist_row 与调度器流水一致，为整表行（指标列齐全），任务信息弹窗可展示指标明细。"""
    now = _beijing_now_str()
    ar = dict(row)
    ar["assist_task_id"] = str(assist_task_id).strip()

    amj = ar.get("assist_materials_json")
    mats: Any = []
    if isinstance(amj, str) and amj.strip():
        try:
            parsed = json.loads(amj)
            mats = parsed if isinstance(parsed, list) else []
        except Exception:
            mats = []
    elif isinstance(amj, list):
        mats = amj
    if not mats:
        mats = [{"material_id": "", "title": ""}]
    m0 = mats[0] if isinstance(mats[0], dict) else {}
    mid = str(m0.get("material_id") or m0.get("materialId") or "").strip()

    return _json_dumps(
        {
            "note": "手动停投",
            "data_source": "pmc_roi2_assist_task",
            "query_at": now,
            "material_id": mid,
            "assist_row": ar,
        }
    )


_MANUAL_REG_STOP_LOCK = threading.Lock()
_MANUAL_REG_STOP_THREAD: Optional[threading.Thread] = None


def _parse_material_from_query_snapshot(raw: Optional[str]) -> Tuple[str, str]:
    """从 query_snapshot_json 解析首条素材 material_id / 展示名（与 assist_materials_json 结构一致）。"""
    mid = ""
    title = ""
    if not raw or not str(raw).strip():
        return mid, title
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return mid, title
        ar = obj.get("assist_row")
        if not isinstance(ar, dict):
            return mid, title
        amj = ar.get("assist_materials_json")
        mats: Any = []
        if isinstance(amj, str) and amj.strip():
            mats = json.loads(amj)
        elif isinstance(amj, list):
            mats = amj
        if isinstance(mats, list) and mats:
            m0 = mats[0] if isinstance(mats[0], dict) else {}
            mid = str(m0.get("material_id") or m0.get("materialId") or "").strip()
            t = m0.get("title")
            title = str(t).strip() if t is not None else ""
    except Exception:
        pass
    return mid, title


def _list_row_public_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """列表行：去掉大 JSON 列，补充素材字段。"""
    d = dict(row)
    qsnap = d.get("query_snapshot_json")
    mid, mtitle = _parse_material_from_query_snapshot(
        str(qsnap) if qsnap is not None else None
    )
    d["material_id"] = mid
    d["material_name"] = mtitle
    for k in ("query_snapshot_json", "trigger_snapshot_json", "rule_full_json"):
        d.pop(k, None)
    return d


def query_pmc_regulation_runs_page(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    stop_action: Optional[str] = None,
    status: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[int, List[Dict[str, Any]]]:
    """
    分页查询 pmc_regulation_run（列表不含 rule_full_json / 完整快照，仅用于解析素材字段临时读取 query_snapshot_json）。
    """
    tbl = "pmc_regulation_run"
    fields = (
        "id, aavid, ad_id, assist_task_id, task_name, strategy_name, stop_action, "
        "started_at, ended_at, duration_ms, status, step, message, trigger_source, created_at, "
        "query_snapshot_json"
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
            if st in (-1, 1, 2):
                where_parts.append("status = ?")
                params.append(st)
        except (TypeError, ValueError):
            pass

    sa = (stop_action or "").strip().lower()
    if sa in ("pause", "delete"):
        where_parts.append("LOWER(IFNULL(stop_action,'')) = ?")
        params.append(sa)

    qv = (q or "").strip()
    if qv:
        like = f"%{qv}%"
        where_parts.append(
            "("
            "CAST(IFNULL(assist_task_id,'') AS TEXT) LIKE ? OR "
            "IFNULL(task_name,'') LIKE ? OR "
            "IFNULL(strategy_name,'') LIKE ? OR "
            "CAST(IFNULL(aavid,'') AS TEXT) LIKE ? OR "
            "CAST(IFNULL(ad_id,'') AS TEXT) LIKE ? OR "
            "IFNULL(message,'') LIKE ? OR "
            "IFNULL(query_snapshot_json,'') LIKE ?"
            ")"
        )
        params.extend([like, like, like, like, like, like, like])

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
    items_raw = db.execute(data_sql, tuple(params + [ps, offset]), fetch=True) or []
    items = [_list_row_public_fields(dict(r)) for r in items_raw]
    return total, items


def get_pmc_regulation_run_by_id(run_id: Any) -> Optional[Dict[str, Any]]:
    """单条停投流水（含 JSON 列），供详情弹窗。"""
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        return None
    if rid < 1:
        return None
    db = _store()
    rows = db.execute(
        "SELECT * FROM pmc_regulation_run WHERE id = ?",
        (rid,),
        fetch=True,
    )
    if not rows:
        return None
    r = dict(rows[0])
    mid, mtitle = _parse_material_from_query_snapshot(
        str(r.get("query_snapshot_json") or "")
    )
    r["material_id"] = mid
    r["material_name"] = mtitle
    return r


def run_immediate_regulation_stop_prepare(
    *,
    assist_task_id: str,
    stop_action: Optional[str] = None,
) -> Dict[str, Any]:
    """
    手动停投：仅填调控任务 ID，从 pmc_roi2_assist_task 解析广告主与计划；
    有头浏览器打开投放页并筛选该任务，程序代为点击暂停/删除以弹出确认层，用户自行点「确定」；
    等待 batch 接口响应后写 pmc_regulation_run。需在 GUI 进程内调用（Playwright）。
    """
    global _MANUAL_REG_STOP_THREAD

    aid_in = str(assist_task_id or "").strip()
    if not aid_in:
        return {"success": False, "message": "请填写调控任务 ID"}

    act = str(stop_action or "pause").strip().lower()
    if act not in ("pause", "delete"):
        return {"success": False, "message": "停投方式须为 pause 或 delete"}

    with _MANUAL_REG_STOP_LOCK:
        if _MANUAL_REG_STOP_THREAD is not None and _MANUAL_REG_STOP_THREAD.is_alive():
            return {
                "success": False,
                "message": "已有手动停投会话进行中，请先关闭浏览器或等待当前流程结束后再试",
            }

    cfg = load_rule_regulation_config()
    _be_path = str(cfg.get("browser_executable_path") or "").strip()
    _browser_executable_for_session = _be_path if _be_path else None
    strategy_name = _default_strategy_display_name_reg(cfg)
    rule_full_json = _json_dumps(cfg)
    browser_rule = bool(cfg.get("browser_headless", True))

    db = _store()
    roi_row = _lookup_roi2_assist_task_row(db, aid_in)
    if not roi_row:
        return {
            "success": False,
            "message": "本地库中未找到该调控任务 ID，请先由采集同步全域调控任务（表 pmc_roi2_assist_task）",
        }

    aavid_raw = roi_row.get("aadvid")
    ad_id_raw = roi_row.get("ad_id")
    aavid = str(aavid_raw).strip() if aavid_raw is not None else ""
    ad_id = str(ad_id_raw).strip() if ad_id_raw is not None else ""
    task_name_row = str(roi_row.get("task_name") or "").strip()[:512]

    q_snap = _manual_regulation_query_snapshot_from_roi2_row(aid_in, roi_row)
    trig_snap = _json_dumps(
        {
            "source": "manual",
            "assist_task_id": aid_in,
            "stop_action": act,
        }
    )

    if not aavid or not ad_id:
        now = _beijing_now_str()
        _insert_regulation_run(
            db,
            aavid=aavid or "",
            ad_id=ad_id or "",
            task_name=task_name_row or "",
            strategy_name=strategy_name,
            assist_task_id=aid_in,
            stop_action=act,
            started_at=now,
            ended_at=now,
            duration_ms=0,
            status=-1,
            step="validate",
            message="调控任务记录缺少 aadvid 或 ad_id",
            detail="",
            rule_full_json=rule_full_json,
            trigger_snapshot_json=trig_snap,
            query_snapshot_json=q_snap,
            headless=False,
            browser_headless_rule=browser_rule,
            trigger_source="manual",
        )
        return {"success": False, "message": "调控任务记录缺少广告主或计划 ID，请检查采集数据"}

    try:
        aavid_int = int(str(aavid_raw).strip())
        ad_id_int = int(str(ad_id_raw).strip())
    except (TypeError, ValueError):
        now = _beijing_now_str()
        _insert_regulation_run(
            db,
            aavid=aavid or "",
            ad_id=ad_id,
            task_name=task_name_row or "",
            strategy_name=strategy_name,
            assist_task_id=aid_in,
            stop_action=act,
            started_at=now,
            ended_at=now,
            duration_ms=0,
            status=-1,
            step="validate",
            message="aavid 或 ad_id 无法转为整数",
            detail="",
            rule_full_json=rule_full_json,
            trigger_snapshot_json=trig_snap,
            query_snapshot_json=q_snap,
            headless=False,
            browser_headless_rule=browser_rule,
            trigger_source="manual",
        )
        return {"success": False, "message": "aavid 或 ad_id 无效"}

    result_holder: List[Optional[RegulationRunResult]] = [None]

    def _thread_target() -> None:
        async def main() -> None:
            svc: Optional[QianChuanRegulationStopService] = None
            try:
                svc = QianChuanRegulationStopService(
                    RegulationSessionOptions(
                        headless=False,
                        storage_state=None,
                        browser_executable_path=_browser_executable_for_session,
                    )
                )
                r = await svc.run_prepare_for_manual_stop(
                    aavid=aavid_int,
                    ad_id=ad_id_int,
                    assist_task_id=aid_in,
                    stop_action=act,
                    strategy_title=strategy_name,
                )
                result_holder[0] = r
            except Exception:
                logger.exception("[手动停投] 线程内异常")
                result_holder[0] = RegulationRunResult(
                    success=False,
                    message="手动停投线程异常",
                    step="exception",
                    detail=traceback.format_exc()[:8000],
                    aavid=str(aavid_int),
                    ad_id=str(ad_id_int),
                    assist_task_id=aid_in,
                    stop_action=act,
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
    th = threading.Thread(target=_thread_target, name="immediate-regulation-stop", daemon=True)
    with _MANUAL_REG_STOP_LOCK:
        _MANUAL_REG_STOP_THREAD = th
    th.start()

    deadline = time.time() + 600.0
    while time.time() < deadline and result_holder[0] is None:
        time.sleep(0.05)

    r = result_holder[0]
    ended_at = _beijing_now_str()
    dur_ms = int((time.time() - t0) * 1000)

    if r is None:
        return {"success": False, "message": "手动停投超时（600s）或线程无响应"}

    if r.step == "done_already_paused":
        st = 2
    elif r.success:
        st = 1
    else:
        st = -1
    if r.step == "done_already_paused" or not r.success:
        detail = (r.detail or "")[:8000]
    else:
        detail = ""

    _insert_regulation_run(
        db,
        aavid=str(r.aavid or aavid_int),
        ad_id=str(r.ad_id or ad_id_int),
        task_name=task_name_row or "",
        strategy_name=strategy_name,
        assist_task_id=str(r.assist_task_id or aid_in),
        stop_action=str(r.stop_action or act),
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=dur_ms,
        status=st,
        step=r.step,
        message=r.message,
        detail=detail,
        rule_full_json=rule_full_json,
        trigger_snapshot_json=trig_snap,
        query_snapshot_json=q_snap,
        headless=bool(r.headless),
        browser_headless_rule=browser_rule,
        trigger_source="manual",
    )

    return {
        "success": bool(r.success),
        "message": r.message,
        "step": r.step,
        "detail": detail if not r.success else "",
        "assist_task_id": str(r.assist_task_id or aid_in),
        "aavid": str(r.aavid or aavid_int),
        "ad_id": str(r.ad_id or ad_id_int),
        "app_version": CURRENT_VERSION,
    }
