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
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from api.dashboard import DashboardApi
from api.operation_events import prune_operation_events
from api.rule_retargeting_config import evaluate_trigger, load_rule_retargeting_config
from config import DATA_DIR
from services.cloud_retarget_client import pull_retarget_task, report_retarget_task
from services.local_test_guard import (
    assert_test_scope,
    consume_live_retarget_batch_once,
)
from services.retargeting_rule_runner import (
    _insert_run,
    _interval_from_root_cfg,
    _interval_window_and_max,
    rate_limit_record_success,
    rate_limit_should_skip,
    rate_limit_strategy_record_success,
    rate_limit_strategy_should_skip,
    resolve_ad_id_for_aavid,
)
from services.retargeting_service import QianChuanRetargetingService
from services.product_rule_engine import evaluate_product_strategy
from services.plan_system import normalize_plan_system
from services.promotion_browser_lock import exclusive_browser_operation
from utils.log import logger
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


POLL_SECONDS = 5


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        "action_mode": str(strategy.get("action_mode") or "card_confirm"),
        "trigger": strategy.get("trigger") if isinstance(strategy.get("trigger"), dict) else {},
        "retargeting": strategy.get("retargeting") if isinstance(strategy.get("retargeting"), dict) else {},
    }


def _strategy_hash(strategy: Dict[str, Any]) -> str:
    raw = json.dumps(_strategy_snapshot(strategy), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_hash(snapshot: Dict[str, Any]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    if status in ("succeeded", "failed"):
        data["finished_at"] = _now()
    db.insert_or_update("cloud_retarget_task_local", data, unique_fields=["cloud_task_id"])


def _cached_report(task_uid: str, claim_token: str, local: Dict[str, Any]) -> None:
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


def _validate_task(
    task: Dict[str, Any],
    db: SQLiteStore,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    cfg = load_rule_retargeting_config()
    if not cfg.get("enabled"):
        raise RuntimeError("规则化追投已关闭")
    strategy_id = str(task.get("strategy_id") or "")
    strategy = _find_strategy(cfg, strategy_id)
    if not strategy:
        raise RuntimeError("追投策略已删除")
    if str(strategy.get("action_mode") or "card_confirm") != "card_confirm":
        raise RuntimeError("追投策略的执行方式已经变更")
    expected_hash = str(task.get("strategy_hash") or "")
    task_snapshot = task.get("rule_snapshot") if isinstance(task.get("rule_snapshot"), dict) else {}
    if not task_snapshot or _snapshot_hash(task_snapshot) != expected_hash:
        raise RuntimeError("云端追投策略快照校验失败")
    if _strategy_hash(strategy) != expected_hash:
        raise RuntimeError("追投策略参数已经变更，请等待新提醒")

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
    for material in materials:
        assert_test_scope(aavid, material["material_id"])
    target: Dict[str, Any] = {}
    if target_uid:
        target = db.select_one("promotion_target", where={"target_uid": target_uid}) or {}
        if not target or not bool(target.get("enabled")):
            raise RuntimeError("监控计划已删除或停用")
        if str(target.get("last_status") or "").strip().lower() != "ok":
            raise RuntimeError(
                "监控计划当前不是投放中状态，已阻止追投"
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
            raise RuntimeError("计划体系尚未确认是传统全域还是千川乘方")
        if plan_system == "chengfang":
            raise RuntimeError("千川乘方计划尚未通过本机追投适配验证")
        if promotion_scene == "product":
            try:
                capability = json.loads(target.get("capability_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                capability = {}
            if not isinstance(capability, dict) or not bool(
                capability.get("retarget_execute")
            ):
                raise RuntimeError(
                    "商品全域计划的追投表单能力尚未通过本机验证，已安全停止"
                )
        strategy_target_uid = str(strategy.get("target_uid") or "")
        if strategy_target_uid and strategy_target_uid != target_uid:
            raise RuntimeError("追投策略已改为其他监控计划")
    else:
        # 兼容升级前已经发出的卡片；新任务均必须带 target_uid。
        current_ad_id = str(resolve_ad_id_for_aavid(db, aavid) or "")
        if not current_ad_id or current_ad_id != ad_id:
            raise RuntimeError("当前账户或广告ID与提醒不一致")
    if not os.path.isfile(os.path.join(DATA_DIR, "qcookie.json")):
        raise RuntimeError("千川登录状态不存在，请在服务控制中重新登录")

    period = str(cfg.get("trigger_query_period") or "1h")
    if target_uid:
        target_rows = _latest_target_rows(target_uid, period)
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


async def _execute_task(task: Dict[str, Any], db: SQLiteStore) -> Dict[str, Any]:
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
        cfg, strategy, rows = await asyncio.to_thread(_validate_task, task, db)
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
        consume_live_retarget_batch_once(task_uid, aavid, material_ids)
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
    try:
        target = db.select_one("promotion_target", where={"target_uid": target_uid}) or {}
        async with exclusive_browser_operation(
            f"飞书确认追投:{target_uid}:{','.join(material_ids)}"
        ):
            if promotion_scene == "product":
                results.append(
                    await svc.run(
                        aavid=int(aavid),
                        ad_id=int(ad_id),
                        material_id=material_id,
                        material_ids=material_ids,
                        retargeting=retargeting,
                        strategy_title=strategy_name,
                        target_uid=target_uid,
                        promotion_scene=promotion_scene,
                        source_url=target.get("sanitized_page_url") or None,
                        reuse_session=False,
                        close_session=False,
                    )
                )
            else:
                # 直播详情页一次只能从具体素材行创建调控任务；仍然只发
                # 一张飞书卡、只确认一次，但在同一浏览器锁内逐条完成。
                for current_material_id in material_ids:
                    results.append(
                        await svc.run(
                            aavid=int(aavid),
                            ad_id=int(ad_id),
                            material_id=current_material_id,
                            retargeting=retargeting,
                            strategy_title=strategy_name,
                            target_uid=target_uid,
                            promotion_scene=promotion_scene,
                            source_url=target.get("sanitized_page_url") or None,
                            reuse_session=False,
                            close_session=False,
                        )
                    )
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
        successful_material_ids = (
            material_ids
            if promotion_scene == "product" and bool(results[0].success)
            else [
                current_material_id
                for current_material_id, current_result in zip(material_ids, results)
                if bool(current_result.success)
            ]
        )
        all_succeeded = (
            bool(results[0].success)
            if promotion_scene == "product"
            else len(successful_material_ids) == len(material_ids)
        )
        first_result = results[0]
        payload = first_result.asdict()
        payload["success"] = all_succeeded
        payload["regulate_task_ids"] = regulate_task_ids
        payload["successful_material_ids"] = successful_material_ids
        payload["results"] = result_payloads
        if all_succeeded:
            payload["message"] = f"追投成功（{len(materials)}条素材）"
        elif successful_material_ids:
            payload["message"] = (
                f"部分追投成功（{len(successful_material_ids)}/{len(materials)}条素材）"
            )
            payload["detail"] = _json(result_payloads)
            payload["step"] = "partial_failure"
        regulate_task_id = regulate_task_ids[0] if regulate_task_ids else ""
    payload["materials"] = materials
    payload["material_count"] = len(materials)
    trigger_snapshot = task.get("trigger_snapshot") or {}
    query_snapshot = dict(task.get("query_snapshot") or {})
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
        status=1 if payload.get("success") else -1,
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
        cloud_task_id=task_uid,
        operator_id=str(task.get("clicker_open_id") or ""),
        materials=materials,
    )
    successful_ids = set(payload.get("successful_material_ids") or [])
    if payload.get("success") and not successful_ids:
        successful_ids = set(material_ids)
    if successful_ids:
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


async def _heartbeat_lease(task_uid: str, claim_token: str) -> None:
    while True:
        await asyncio.sleep(240)
        response = await asyncio.to_thread(
            report_retarget_task,
            task_uid,
            claim_token,
            "executing",
            message="桌面工具正在执行追投",
        )
        if not response.get("success"):
            logger.warning("[飞书确认追投] 任务租约续期失败 %s: %s", task_uid, response.get("message"))
            return


async def run_worker_loop() -> None:
    init_sqlite_schema()
    db = SQLiteStore()
    last_prune = 0.0
    logger.info("[飞书确认追投] 任务轮询已启动")
    while True:
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
            cached = _local_task(db, task_uid)
            if cached and str(cached.get("status")) in ("succeeded", "failed"):
                await asyncio.to_thread(_cached_report, task_uid, claim_token, cached)
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
            heartbeat = asyncio.create_task(_heartbeat_lease(task_uid, claim_token))
            try:
                result = await _execute_task(task, db)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            final_status = "succeeded" if result.get("success") else "failed"
            _save_local_task(db, task_uid, final_status, result, task)
            await asyncio.to_thread(
                report_retarget_task,
                task_uid,
                claim_token,
                final_status,
                message=str(result.get("message") or ("追投成功" if result.get("success") else "追投失败")),
                detail=str(result.get("detail") or ""),
                regulate_task_id=str(result.get("regulate_task_id") or ""),
                result=result,
            )
        except Exception:
            logger.exception("[飞书确认追投] 任务轮询异常")
            await asyncio.sleep(15)


def start_retarget_task_worker_background_thread() -> threading.Thread:
    def _entry() -> None:
        asyncio.run(run_worker_loop())

    thread = threading.Thread(target=_entry, name="retarget-card-worker", daemon=True)
    thread.start()
    return thread
