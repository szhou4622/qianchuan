# -*- coding: utf-8 -*-
"""本地真实追投联调保护；正式模式下完全旁路。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from config import (
    ALLOW_LIVE_RETARGET,
    DATA_DIR,
    LOCAL_TEST_SECRETS_FILE,
    TEST_AAVID,
    TEST_MATERIAL_ID,
    TEST_MODE,
)
from services.plan_system import normalize_plan_system
from services.promotion_capability import check_target_capability


CONSUMED_FILE = os.path.join(DATA_DIR, "live_retarget_consumed.json")


def load_local_test_login_credentials() -> Dict[str, Any]:
    """仅本地测试模式读取随机测试账号；不返回飞书或数据库密钥。"""
    if not TEST_MODE:
        return {"success": False, "message": "正式环境不提供本地测试账号"}
    path = str(LOCAL_TEST_SECRETS_FILE or "").strip()
    if not path or not os.path.isfile(path):
        return {"success": False, "message": "本地测试账号文件不存在"}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception as exc:
        return {"success": False, "message": f"读取本地测试账号失败：{exc}"}
    account = data.get("test_account") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return {"success": False, "message": "本地测试账号配置缺失"}
    username = str(account.get("username") or "").strip()
    password = str(account.get("password") or "")
    if not username or not password:
        return {"success": False, "message": "本地测试账号配置不完整"}
    return {
        "success": True,
        "username": username,
        "password": password,
    }


def _retargeting_summary(retargeting: Dict[str, Any]) -> str:
    method = str(retargeting.get("method") or "volume").strip().lower()
    if method == "volume":
        volume = retargeting.get("volume") if isinstance(retargeting.get("volume"), dict) else {}
        return "放量追投：预算 {} 元，时长 {} 小时".format(
            volume.get("total_budget_yuan") if volume.get("total_budget_yuan") is not None else "未填写",
            volume.get("duration_hours") if volume.get("duration_hours") is not None else "未填写",
        )
    cost_control = (
        retargeting.get("cost_control")
        if isinstance(retargeting.get("cost_control"), dict)
        else {}
    )
    goal = str(cost_control.get("optimization_goal") or "net_roi").strip().lower()
    if goal == "live_room":
        live_room = (
            cost_control.get("live_room")
            if isinstance(cost_control.get("live_room"), dict)
            else {}
        )
        return "控成本追投（直播间成交）：日预算 {} 元，出价 {} 元".format(
            live_room.get("daily_budget_yuan")
            if live_room.get("daily_budget_yuan") is not None
            else "未填写",
            live_room.get("bid_per_conversion_yuan")
            if live_room.get("bid_per_conversion_yuan") is not None
            else "未填写",
        )
    net_roi = (
        cost_control.get("net_roi")
        if isinstance(cost_control.get("net_roi"), dict)
        else {}
    )
    return "控成本追投（净成交ROI）：日预算 {} 元，ROI目标 {}".format(
        net_roi.get("daily_budget_yuan")
        if net_roi.get("daily_budget_yuan") is not None
        else "未填写",
        net_roi.get("net_roi_target")
        if net_roi.get("net_roi_target") is not None
        else "未填写",
    )


def build_live_retarget_preflight() -> Dict[str, Any]:
    """返回给本地测试前端的只读验收清单，不暴露 Cookie、设备令牌或飞书密钥。"""
    if not TEST_MODE:
        return {
            "success": True,
            "test_mode": False,
            "ready_to_arm": False,
            "ready_to_execute": False,
            "checks": [],
            "strategies": [],
        }

    from api.rule_retargeting_config import (
        load_rule_retargeting_config,
        validate_rule_retargeting_config,
    )
    from services.cloud_retarget_client import load_device_session
    from services.retargeting_rule_runner import (
        _interval_from_root_cfg,
        _interval_window_and_max,
        rate_limit_should_skip,
        rate_limit_strategy_should_skip,
        resolve_ad_id_for_aavid,
    )
    from utils.sqlite_store import SQLiteStore, init_sqlite_schema

    checks: List[Dict[str, Any]] = []

    def add_check(key: str, label: str, ok: bool, detail: str, *, arm_required: bool = True) -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "ok": bool(ok),
                "detail": str(detail),
                "arm_required": bool(arm_required),
            }
        )

    target_ready = bool(TEST_AAVID and TEST_MATERIAL_ID)
    add_check(
        "target",
        "验收白名单",
        target_ready,
        (
            f"账户 {TEST_AAVID}；素材 {TEST_MATERIAL_ID}"
            if target_ready
            else "尚未锁定本次验收账户和素材"
        ),
    )

    cfg = load_rule_retargeting_config()
    valid, validation_message = validate_rule_retargeting_config(cfg)
    add_check(
        "rule_config",
        "追投策略配置",
        valid,
        "配置有效" if valid else validation_message,
    )
    add_check(
        "rule_enabled",
        "规则运行开关",
        bool(cfg.get("enabled")),
        "已启用" if cfg.get("enabled") else "尚未启用规则化追投",
    )

    strategy_summaries: List[Dict[str, str]] = []
    card_strategies: List[Dict[str, Any]] = []
    for strategy in cfg.get("strategies") or []:
        if not isinstance(strategy, dict):
            continue
        if str(strategy.get("action_mode") or "card_confirm") != "card_confirm":
            continue
        card_strategies.append(strategy)
        retargeting = (
            strategy.get("retargeting")
            if isinstance(strategy.get("retargeting"), dict)
            else {}
        )
        strategy_summaries.append(
            {
                "id": str(strategy.get("id") or ""),
                "title": str(strategy.get("title") or "未命名策略"),
                "summary": _retargeting_summary(retargeting),
                "card_expiry": "30分钟",
            }
        )
    add_check(
        "card_strategy",
        "飞书确认策略",
        bool(strategy_summaries),
        (
            f"共 {len(strategy_summaries)} 条策略会先发送确认卡片"
            if strategy_summaries
            else "至少需要一条“飞书确认后追投”策略"
        ),
    )

    from services.qianchuan_session import has_qianchuan_session

    cookie_ready = has_qianchuan_session() or os.path.isfile(
        os.path.join(DATA_DIR, "qcookie.json")
    )
    add_check(
        "qianchuan_login",
        "千川登录状态",
        cookie_ready,
        "本机已有登录状态，执行前仍会再次打开页面复核"
        if cookie_ready
        else "请先在服务控制中登录千川",
    )

    device = load_device_session()
    device_ready = bool(str(device.get("token") or "").strip())
    add_check(
        "device_session",
        "本地任务通道",
        device_ready,
        (
            f"设备令牌已就绪（工具账号：{str(device.get('username') or '已登录')}）"
            if device_ready
            else "请重新登录工具账号以签发设备令牌"
        ),
    )

    ad_id = ""
    target_uid = ""
    promotion_scene = ""
    plan_system = "unknown"
    plan_name = ""
    material_name = ""
    material_found = False
    account_name = ""
    monitor_target_found = False
    monitor_target_enabled = False
    monitor_target_account_matches = False
    monitor_target_status = ""
    retarget_capability_ready = False
    retarget_capability_detail = "尚未取得可验证的监控计划"
    rate_limit_ready = False
    rate_limit_detail = "尚未取得可检查限频的计划和素材"
    if target_ready:
        try:
            init_sqlite_schema()
            db = SQLiteStore()
            strategy_target_ids = {
                str(item.get("target_uid") or "").strip()
                for item in (cfg.get("strategies") or [])
                if isinstance(item, dict)
                and str(item.get("action_mode") or "card_confirm") == "card_confirm"
                and str(item.get("target_uid") or "").strip()
            }
            target_row = None
            if len(strategy_target_ids) == 1:
                target_uid = next(iter(strategy_target_ids))
                target_row = db.select_one(
                    "promotion_target",
                    where={"target_uid": target_uid},
                )
            if target_row:
                monitor_target_found = True
                monitor_target_enabled = bool(target_row.get("enabled"))
                monitor_target_account_matches = (
                    str(target_row.get("aadvid") or "").strip() == TEST_AAVID
                )
                monitor_target_status = str(
                    target_row.get("last_status") or ""
                ).strip().lower()
                if monitor_target_account_matches:
                    ad_id = str(target_row.get("ad_id") or "").strip()
                    promotion_scene = str(
                        target_row.get("promotion_scene") or "live"
                    ).strip()
                    plan_system = normalize_plan_system(
                        target_row.get("plan_system") or "unknown"
                    )
                    plan_name = str(target_row.get("plan_name") or "").strip()
                else:
                    ad_id = ""
                retarget_capability_ready, retarget_capability_detail = (
                    check_target_capability(
                        target_row,
                        action="retarget",
                        promotion_scene=promotion_scene,
                        plan_system=plan_system,
                    )
                )
            else:
                # 仅兼容升级前的单计划配置；新配置会由规则校验要求 target_uid。
                ad_id = str(resolve_ad_id_for_aavid(db, TEST_AAVID) or "")
            account_row = db.select_one(
                "pmc_ad_detail_basic",
                fields=["user_info_name"],
                where=(
                    {"aadvid": TEST_AAVID, "ad_id": ad_id}
                    if ad_id
                    else {"aadvid": TEST_AAVID}
                ),
            )
            if account_row:
                account_name = str(account_row.get("user_info_name") or "").strip()
            material_row = db.select_one(
                "pmc_promotion_material",
                fields=["video_name"],
                where=(
                    {
                        "aadvid": TEST_AAVID,
                        "material_id": TEST_MATERIAL_ID,
                        "target_uid": target_uid,
                    }
                    if target_uid
                    else {"aadvid": TEST_AAVID, "material_id": TEST_MATERIAL_ID}
                ),
                order_by="created_at DESC",
            )
            if material_row:
                material_found = True
                material_name = str(material_row.get("video_name") or "").strip()
            if material_found and target_uid:
                relevant_strategies = [
                    item
                    for item in card_strategies
                    if not str(item.get("target_uid") or "").strip()
                    or str(item.get("target_uid") or "").strip() == target_uid
                ]
                if bool(cfg.get("per_strategy_rate_limit")):
                    blocked_strategies: List[str] = []
                    for item in relevant_strategies:
                        retargeting = (
                            item.get("retargeting")
                            if isinstance(item.get("retargeting"), dict)
                            else {}
                        )
                        window_seconds, max_count = _interval_window_and_max(
                            retargeting
                        )
                        if rate_limit_strategy_should_skip(
                            db,
                            TEST_MATERIAL_ID,
                            str(item.get("id") or ""),
                            window_seconds,
                            max_count,
                            target_uid,
                        ):
                            blocked_strategies.append(
                                str(item.get("title") or item.get("id") or "未命名策略")
                            )
                    rate_limit_ready = bool(relevant_strategies) and not blocked_strategies
                    rate_limit_detail = (
                        "所选素材在全部飞书确认策略中均未达到限频"
                        if rate_limit_ready
                        else (
                            "已达到分策略限频：" + "、".join(blocked_strategies)
                            if blocked_strategies
                            else "没有可用于白名单计划的飞书确认策略"
                        )
                    )
                else:
                    window_seconds, max_count = _interval_from_root_cfg(cfg)
                    blocked = rate_limit_should_skip(
                        db,
                        TEST_MATERIAL_ID,
                        window_seconds,
                        max_count,
                        target_uid,
                    )
                    rate_limit_ready = not blocked
                    if window_seconds <= 0 or max_count <= 0:
                        rate_limit_detail = "当前未启用全局追投限频"
                    elif blocked:
                        rate_limit_detail = (
                            f"当前窗口已达到全局限频：{window_seconds}秒内最多"
                            f"{max_count}次，请等待窗口结束"
                        )
                    else:
                        rate_limit_detail = (
                            f"当前未达到全局限频：{window_seconds}秒内最多"
                            f"{max_count}次"
                        )
        except Exception as exc:
            add_check("local_data", "本地账户数据", False, f"读取失败：{exc}")
    add_check(
        "monitor_target",
        "监控计划",
        bool(
            monitor_target_found
            and monitor_target_enabled
            and monitor_target_account_matches
        ),
        (
            f"{plan_name or target_uid} 已启用且属于白名单账户"
            if (
                monitor_target_found
                and monitor_target_enabled
                and monitor_target_account_matches
            )
            else (
                "监控计划已停用"
                if monitor_target_found and not monitor_target_enabled
                else (
                    "监控计划与白名单账户不一致"
                    if monitor_target_found and not monitor_target_account_matches
                    else "策略关联的监控计划不存在"
                )
            )
        ),
    )
    add_check(
        "monitor_target_status",
        "监控计划投放状态",
        monitor_target_status == "ok",
        (
            "最近一次同步确认计划正在投放"
            if monitor_target_status == "ok"
            else (
                f"当前状态为 {monitor_target_status}，请重新同步并确认计划正在投放"
                if monitor_target_status
                else "尚未同步到可执行的投放状态"
            )
        ),
    )
    add_check(
        "ad_mapping",
        "账户与广告ID映射",
        bool(ad_id),
        (
            f"{account_name + '；' if account_name else ''}"
            f"{plan_name + '；' if plan_name else ''}"
            f"{'推商品；' if promotion_scene == 'product' else ('推直播；' if promotion_scene == 'live' else '')}"
            f"{'全域；' if plan_system == 'global' else ('千川乘方；' if plan_system == 'chengfang' else '计划体系待确认；')}"
            f"广告ID {ad_id}"
            if ad_id
            else "尚未采集到该账户的广告ID"
        ),
    )
    add_check(
        "plan_system",
        "计划体系与执行适配器",
        plan_system == "global"
        or (plan_system == "chengfang" and retarget_capability_ready),
        (
            "已识别为全域，可使用当前已验证适配器"
            if plan_system == "global"
            else (
                (
                    "已识别为千川乘方，且当前目标已取得匹配的受控能力证据"
                    if retarget_capability_ready
                    else f"已识别为千川乘方；{retarget_capability_detail}"
                )
                if plan_system == "chengfang"
                else "计划体系尚未识别，请重新打开计划详情"
            )
        ),
    )
    add_check(
        "retarget_capability",
        "追投表单能力",
        retarget_capability_ready,
        (
            "当前计划的追投表单已通过本机只读探测"
            if retarget_capability_ready
            else retarget_capability_detail
        ),
    )
    add_check(
        "material_data",
        "素材最新数据",
        material_found,
        (
            f"{material_name or '已找到素材'}（{TEST_MATERIAL_ID}）"
            if material_found
            else "尚未在该账户的本地数据中找到白名单素材"
        ),
    )
    add_check(
        "retarget_rate_limit",
        "追投限频",
        rate_limit_ready,
        rate_limit_detail,
    )

    consumed = os.path.isfile(CONSUMED_FILE)
    armed = bool(ALLOW_LIVE_RETARGET and not consumed)
    add_check(
        "live_gate",
        "一次性真实追投授权",
        armed,
        (
            "已开启，成功领取一次任务后会自动关闭"
            if armed
            else ("本次授权已使用" if consumed else "保持关闭，待你最终确认后再开启")
        ),
        arm_required=False,
    )

    ready_to_arm = all(
        bool(item["ok"]) for item in checks if bool(item.get("arm_required"))
    )
    return {
        "success": True,
        "test_mode": True,
        "aavid": TEST_AAVID,
        "account_name": account_name,
        "ad_id": ad_id,
        "target_uid": target_uid,
        "promotion_scene": promotion_scene,
        "plan_system": plan_system,
        "plan_name": plan_name,
        "material_id": TEST_MATERIAL_ID,
        "material_name": material_name,
        "ready_to_arm": ready_to_arm,
        "ready_to_execute": bool(ready_to_arm and armed),
        "live_retarget_armed": bool(ALLOW_LIVE_RETARGET),
        "live_retarget_consumed": consumed,
        "checks": checks,
        "strategies": strategy_summaries,
    }


def assert_test_scope(aavid: str, material_id: str) -> None:
    """测试模式只允许显式白名单内的一个账户和素材。"""
    if not TEST_MODE:
        return
    if not TEST_AAVID or not TEST_MATERIAL_ID:
        raise RuntimeError("本地测试模式未配置账户和素材白名单")
    if str(aavid).strip() != TEST_AAVID:
        raise RuntimeError("当前账户不在本地真实追投白名单")
    if str(material_id).strip() != TEST_MATERIAL_ID:
        raise RuntimeError("当前素材不在本地真实追投白名单")


def assert_test_task_scope(
    aavid: str,
    material_ids: List[str],
    candidate_material_ids: List[str],
) -> None:
    """测试模式按账户和卡片候选快照校验用户最终选择的素材。"""
    if not TEST_MODE:
        return
    if not TEST_AAVID:
        raise RuntimeError("本地测试模式未配置账户白名单")
    if str(aavid).strip() != TEST_AAVID:
        raise RuntimeError("当前账户不在本地真实追投白名单")

    selected = {
        str(material_id or "").strip()
        for material_id in material_ids
        if str(material_id or "").strip()
    }
    candidates = {
        str(material_id or "").strip()
        for material_id in candidate_material_ids
        if str(material_id or "").strip()
    }
    if not selected:
        raise RuntimeError("本地真实追投缺少素材")
    if candidates:
        unexpected = sorted(selected - candidates)
        if unexpected:
            raise RuntimeError(
                "所选素材不属于本次飞书提醒候选：" + "、".join(unexpected)
            )
        return

    # 兼容升级前没有 candidate_materials 快照的单素材任务。
    for material_id in selected:
        assert_test_scope(aavid, material_id)


def row_is_in_test_scope(row: Dict[str, Any]) -> bool:
    if not TEST_MODE:
        return True
    if not TEST_AAVID or not TEST_MATERIAL_ID:
        return False
    return (
        str(row.get("aadvid") or "").strip() == TEST_AAVID
        and str(row.get("id") or "").strip() == TEST_MATERIAL_ID
    )


def consume_live_retarget_once(task_uid: str, aavid: str, material_id: str) -> None:
    consume_live_retarget_batch_once(task_uid, aavid, [material_id])


def consume_live_retarget_batch_once(
    task_uid: str,
    aavid: str,
    material_ids: List[str],
    candidate_material_ids: List[str] | None = None,
) -> None:
    """
    在真正调用千川前为整批素材原子消费一次性授权。

    即使执行中断也不允许自动再试，避免形成重复真实追投。
    """
    if not TEST_MODE:
        return
    ids = [str(item or "").strip() for item in material_ids if str(item or "").strip()]
    if not ids:
        raise RuntimeError("本地真实追投缺少素材")
    assert_test_task_scope(
        aavid,
        ids,
        list(candidate_material_ids or []),
    )
    if not ALLOW_LIVE_RETARGET:
        raise RuntimeError("本地真实追投开关未开启，本次只允许模拟验收")
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "task_uid": str(task_uid),
        "aavid": str(aavid),
        "material_id": ids[0],
        "material_ids": ids,
        "consumed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        fd = os.open(CONSUMED_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("本地真实追投一次性授权已被消费") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
