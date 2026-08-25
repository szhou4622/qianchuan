# -*- coding: utf-8 -*-
"""
规则化停投配置持久化：data/rule_regulation.json。
结构与追投类似：运行项（浏览器）与多策略；每条策略含「监测指标」与停投执行方式（暂停/结束），不含执行次数/限频字段，亦无追投任务块。
旧配置键 ``delete`` 仅表示官方 API 的 ``DISABLE``（结束调控），绝不调用删除接口。
"""
from __future__ import annotations

import copy
import math
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR

from .rule_retargeting_config import (
    ALLOWED_GROUP_COMBINE,
    ALLOWED_OPS,
    MAX_STRATEGIES,
    _STRATEGY_TITLE_MAX_LEN,
    _atomic_write,
    _coerce_float,
    _new_strategy_id,
    _normalize_json_whole_floats,
    _owner_config_path,
    _read_json,
    target_allows_advance_strategy_configuration,
)

FILENAME = "rule_regulation.json"
_lock = threading.RLock()

# 与 dashboard.html ASSIST_METRIC_COLUMNS、schema_pmc_roi2_assist_task 指标列一致（snake_case）
ALLOWED_METRICS_ROI2_ASSIST = frozenset(
    {
        "show_cnt_for_roi2_assist",
        "click_cnt_for_roi2_assist",
        "ctr_for_roi2_assist",
        "convert_rate_for_roi2_assist",
        "stat_cost_for_roi2_assist",
        "total_pay_order_count_for_roi2_assist",
        "total_pay_order_gmv_include_coupon_for_roi2_assist",
        "total_prepay_and_pay_order_roi2_assist",
        "total_cost_per_pay_order_for_roi2_assist",
        "pay_convert_cost_for_roi2_assist",
        "pay_convert_cnt_for_roi2_assist",
        "total_order_settle_amount_for_roi2_1h_assist",
        "total_refund_order_gmv_for_roi2_1h_rate_assist",
        "total_prepay_and_pay_settle_roi2_1h_assist",
        "total_pay_order_gmv_for_roi2_assist",
        "total_pay_order_coupon_amount_for_roi2_assist",
    }
)

# 与 dashboard formatAssistMetricCell 中 format=percent 一致；规则配置里存 0~100 的百分数（如 11.11），旧版曾存 0~1 小数
_RATE_METRICS_ROI2 = frozenset(
    {
        "ctr_for_roi2_assist",
        "convert_rate_for_roi2_assist",
        "total_refund_order_gmv_for_roi2_1h_rate_assist",
    }
)

# 停投执行方式（与前端单选一致，执行侧读取）
ALLOWED_REGULATION_STOP_ACTION = frozenset({"pause", "delete"})
ALLOWED_ACTION_MODES = frozenset({"card_confirm", "auto_execute"})


def _normalize_regulation_stop_action(raw: Any) -> str:
    s = str(raw or "pause").strip().lower()
    return s if s in ALLOWED_REGULATION_STOP_ACTION else "pause"


def _normalize_action_mode(raw: Any, *, legacy: bool = False) -> str:
    if raw in ALLOWED_ACTION_MODES:
        return str(raw)
    return "auto_execute" if legacy else "card_confirm"


def _default_condition_roi2() -> Dict[str, Any]:
    return {"metric": "stat_cost_for_roi2_assist", "op": "gt", "value": 0.0}


def _default_group_roi2() -> Dict[str, Any]:
    return {"join": "and", "conditions": [_default_condition_roi2()]}


def _default_trigger_roi2() -> Dict[str, Any]:
    return {"group_combine": "or", "groups": [_default_group_roi2()]}


def _normalize_condition_roi2(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_condition_roi2()
    m = str(raw.get("metric") or "stat_cost_for_roi2_assist").strip()
    if m not in ALLOWED_METRICS_ROI2_ASSIST:
        m = "stat_cost_for_roi2_assist"
    op = str(raw.get("op") or "gt").strip().lower()
    if op not in ALLOWED_OPS:
        op = "gt"
    val = _coerce_float(raw.get("value"), 0.0)
    return {"metric": m, "op": op, "value": val}


def _normalize_group_roi2(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_group_roi2()
    join = str(raw.get("join") or "and").strip().lower()
    if join != "and":
        join = "and"
    conds = raw.get("conditions")
    out_conds: List[Dict[str, Any]] = []
    if isinstance(conds, list) and conds:
        for c in conds:
            out_conds.append(_normalize_condition_roi2(c))
    else:
        out_conds = [_default_condition_roi2()]
    return {"join": join, "conditions": out_conds}


def _normalize_trigger_roi2(raw: Any) -> Dict[str, Any]:
    base = copy.deepcopy(_default_trigger_roi2())
    if not isinstance(raw, dict):
        return base
    gc = str(raw.get("group_combine") or "or").strip().lower()
    if gc not in ALLOWED_GROUP_COMBINE:
        gc = "or"
    base["group_combine"] = gc
    groups = raw.get("groups")
    out_groups: List[Dict[str, Any]] = []
    if isinstance(groups, list) and groups:
        for g in groups:
            out_groups.append(_normalize_group_roi2(g))
    else:
        out_groups = [_default_group_roi2()]
    base["groups"] = out_groups
    return base


def config_path() -> str:
    return _owner_config_path(FILENAME)


def _default_strategy(index: int = 0) -> Dict[str, Any]:
    return {
        "id": _new_strategy_id(),
        "title": f"策略 {index + 1}",
        "account_uid": "",
        "aavid": "",
        "target_uid": "",
        "trigger": _normalize_trigger_roi2(None),
        "regulation_stop_action": "pause",
        "action_mode": "card_confirm",
    }


def _default_full() -> Dict[str, Any]:
    return {
        "enabled": False,
        "browser_headless": True,
        "browser_executable_path": "",
        "trigger_query_period": "1h",
        "whitelist_assist_ids": [],
        "strategies": [_default_strategy(0)],
    }


def _normalize_strategy_entry(
    raw: Any,
    index: int,
    legacy_stop: Optional[str] = None,
    *,
    legacy_existing: bool = False,
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    sid = str(raw.get("id") or "").strip()
    if not sid:
        sid = _new_strategy_id()
    title = str(raw.get("title") or "").strip()
    if not title:
        title = f"策略 {index + 1}"
    if len(title) > _STRATEGY_TITLE_MAX_LEN:
        title = title[:_STRATEGY_TITLE_MAX_LEN]
    trig = _normalize_trigger_roi2(raw.get("trigger"))
    if raw.get("regulation_stop_action") is not None:
        rsa_src = raw.get("regulation_stop_action")
    elif legacy_stop is not None:
        rsa_src = legacy_stop
    else:
        rsa_src = "pause"
    rsa = _normalize_regulation_stop_action(rsa_src)
    return {
        "id": sid,
        "title": title,
        "account_uid": str(raw.get("account_uid") or "").strip(),
        "aavid": str(raw.get("aavid") or "").strip(),
        "target_uid": str(raw.get("target_uid") or "").strip(),
        "trigger": trig,
        "regulation_stop_action": rsa,
        "action_mode": _normalize_action_mode(
            raw.get("action_mode"),
            legacy=legacy_existing and "action_mode" not in raw,
        ),
    }


def _normalize_full(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = copy.deepcopy(_default_full())
    if not raw or not isinstance(raw, dict):
        return _normalize_json_whole_floats(base)

    base["enabled"] = bool(raw.get("enabled", False))
    base["browser_headless"] = bool(raw.get("browser_headless", True))
    base["browser_executable_path"] = str(raw.get("browser_executable_path") or "").strip()
    tqp = str(raw.get("trigger_query_period") or "1h").strip().lower()
    base["trigger_query_period"] = tqp if tqp else "1h"

    # 追投白名单：assist_task_id 列表，去重且过滤空字符串
    _wl = raw.get("whitelist_assist_ids")
    if isinstance(_wl, list):
        _seen: set = set()
        _out: List[str] = []
        for _it in _wl:
            _s = str(_it).strip()
            if _s and _s not in _seen:
                _seen.add(_s)
                _out.append(_s)
        base["whitelist_assist_ids"] = _out
    else:
        base["whitelist_assist_ids"] = []

    legacy_root_stop: Optional[str] = None
    if isinstance(raw, dict) and raw.get("regulation_stop_action") is not None:
        legacy_root_stop = _normalize_regulation_stop_action(raw.get("regulation_stop_action"))

    strategies_in: List[Any] = []
    if isinstance(raw.get("strategies"), list) and raw["strategies"]:
        strategies_in = list(raw["strategies"])[:MAX_STRATEGIES]
    elif isinstance(raw.get("trigger"), dict):
        strategies_in = [
            {
                "id": raw.get("legacy_strategy_id") or _new_strategy_id(),
                "title": "策略 1",
                "trigger": raw.get("trigger"),
            }
        ]
    else:
        strategies_in = [_default_strategy(0)]

    base["strategies"] = [
        _normalize_strategy_entry(
            s,
            i,
            legacy_root_stop
            if isinstance(s, dict) and s.get("regulation_stop_action") is None
            else None,
            legacy_existing=bool(raw),
        )
        for i, s in enumerate(strategies_in)
    ]
    if not base["strategies"]:
        base["strategies"] = [_default_strategy(0)]

    return _normalize_json_whole_floats(base)


def load_rule_regulation_config() -> Dict[str, Any]:
    with _lock:
        disk = _read_json(config_path())
        normalized = _normalize_full(disk if isinstance(disk, dict) else None)
        # rc27: existing stop strategies historically executed automatically.
        # Persist the compatibility decision once so later edits cannot silently
        # reinterpret an old strategy as a new card-confirm strategy.
        if isinstance(disk, dict):
            old_strategies = disk.get("strategies")
            needs_action_mode_migration = (
                isinstance(old_strategies, list)
                and any(
                    isinstance(item, dict)
                    and "action_mode" not in item
                    for item in old_strategies
                )
            )
            if needs_action_mode_migration:
                _atomic_write(config_path(), normalized)
        return normalized


def _cond_value_ok(metric: str, val: float) -> bool:
    if not math.isfinite(val):
        return False
    m = str(metric or "").strip()
    if m in _RATE_METRICS_ROI2:
        if 0.0 <= val <= 100.0:
            return True
        return 0.0 < val <= 1.0
    return True


def validate_rule_regulation_config(data: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "配置须为对象"
    strats = data.get("strategies")
    if isinstance(strats, list) and len(strats) > MAX_STRATEGIES:
        return False, f"策略最多 {MAX_STRATEGIES} 条"

    enabled = bool(data.get("enabled", False))
    if enabled:
        if not isinstance(strats, list) or not strats:
            return False, "启用时至少保留一条策略"

    # 有策略则始终校验触发条件（保存与追投一致，不区分是否启用）
    if isinstance(strats, list) and strats:
        for i, st in enumerate(strats):
            if not isinstance(st, dict):
                return False, f"策略{i + 1} 项须为对象"
            if enabled and not str(st.get("target_uid") or "").strip():
                return False, f"策略{i + 1}：启用停投前请选择监控计划"
            rsa = _normalize_regulation_stop_action(st.get("regulation_stop_action"))
            if rsa not in ALLOWED_REGULATION_STOP_ACTION:
                return False, f"策略{i + 1} 停投执行方式无效"
            if str(st.get("action_mode") or "") not in ALLOWED_ACTION_MODES:
                return False, f"策略{i + 1} 飞书确认方式无效"
            trig = st.get("trigger")
            if not isinstance(trig, dict):
                return False, f"策略{i + 1} 缺少监测指标"
            groups = trig.get("groups") or []
            if not groups:
                return False, f"策略{i + 1} 须至少有一个条件组"
            for g in groups:
                if not isinstance(g, dict):
                    continue
                for c in g.get("conditions") or []:
                    if not isinstance(c, dict):
                        continue
                    m = str(c.get("metric") or "")
                    op = str(c.get("op") or "gt").strip().lower()
                    v = _coerce_float(c.get("value"), 0.0)
                    if not _cond_value_ok(m, v):
                        return False, f"策略{i + 1}：触发条件须为有效数字（率类为 0～100 的百分比，旧版 0～1 小数仍兼容）"
                    if op in ("gt", "gte") and v <= 0:
                        return False, f"策略{i + 1}：「大于/大于等于」的阈值须大于 0"

    return True, ""


def bind_and_validate_strategy_targets(
    data: Dict[str, Any],
    targets_by_uid: Dict[str, Dict[str, Any]],
    accounts_by_uid: Dict[str, Dict[str, Any]],
) -> Tuple[bool, str]:
    """Bind every stop strategy to one explicit account and monitored plan.

    ``target_uid`` is already globally scoped, but persisting the account
    snapshot makes the user's selection explicit and lets the scheduler reject
    a stale or tampered cross-account configuration before it can create a
    stop candidate.  Legacy strategies are hydrated from their target once.
    """
    if not isinstance(data, dict):
        return False, "配置须为对象"
    strategies = data.get("strategies")
    if not isinstance(strategies, list):
        return True, ""
    enabled = bool(data.get("enabled", False))
    for index, strategy in enumerate(strategies):
        if not isinstance(strategy, dict):
            continue
        title = str(strategy.get("title") or f"策略{index + 1}").strip()
        target_uid = str(strategy.get("target_uid") or "").strip()
        if not target_uid:
            if enabled:
                return False, f"“{title}”必须选择监控计划"
            continue
        target = targets_by_uid.get(target_uid)
        if not isinstance(target, dict):
            if enabled:
                return False, f"“{title}”选择的监控计划不存在，请重新选择"
            continue
        target_account_uid = str(target.get("account_uid") or "").strip()
        target_aavid = str(target.get("aadvid") or "").strip()
        selected_account_uid = str(strategy.get("account_uid") or "").strip()
        selected_aavid = str(strategy.get("aavid") or "").strip()
        if enabled and selected_account_uid and selected_account_uid != target_account_uid:
            return False, f"“{title}”选择的账户与监控计划不一致"
        if enabled and selected_aavid and selected_aavid != target_aavid:
            return False, f"“{title}”选择的千川账户ID与监控计划不一致"
        account = accounts_by_uid.get(target_account_uid)
        if not isinstance(account, dict):
            if enabled:
                return False, f"“{title}”所属千川账户不存在，请重新选择"
            continue
        strategy["account_uid"] = target_account_uid
        strategy["aavid"] = target_aavid
        if enabled:
            if not bool(account.get("enabled")):
                return False, f"“{title}”所属千川账户尚未启用"
            if not bool(target.get("enabled")):
                return False, f"“{title}”选择的监控计划尚未勾选监控"
            if not bool(target.get("monitor_eligible")):
                return False, f"“{title}”选择的计划当前不可监控"
            if not target_allows_advance_strategy_configuration(
                target, "stop_eligible"
            ):
                return False, f"“{title}”选择的计划尚未取得停投资格"
    return True, ""


def preview_merge(partial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _lock:
        disk = _read_json(config_path())
        cur: Dict[str, Any] = dict(disk) if isinstance(disk, dict) else {}
        if partial and isinstance(partial, dict):
            if "enabled" in partial:
                cur["enabled"] = bool(partial["enabled"])
            if "browser_headless" in partial:
                cur["browser_headless"] = bool(partial["browser_headless"])
            if "browser_executable_path" in partial:
                cur["browser_executable_path"] = str(
                    partial.get("browser_executable_path") or ""
                ).strip()
            if "trigger_query_period" in partial:
                tqp = str(partial.get("trigger_query_period") or "").strip()
                if tqp:
                    cur["trigger_query_period"] = tqp
            if "whitelist_assist_ids" in partial and isinstance(partial["whitelist_assist_ids"], list):
                cur["whitelist_assist_ids"] = partial["whitelist_assist_ids"]
            if "strategies" in partial and isinstance(partial["strategies"], list):
                cur["strategies"] = partial["strategies"]
            if "trigger" in partial and isinstance(partial["trigger"], dict):
                cur["trigger"] = partial["trigger"]
        return _normalize_full(cur)


def merge_and_save(partial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _lock:
        cur = preview_merge(partial)
        _atomic_write(config_path(), cur)
        return cur


# ---------- 调控任务行与触发条件求值（指标 snake_case，与 pmc_roi2_assist_task / get_roi2_assist_table_data 列一致）----------


def _compare_op_roi2(op: str, left: float, right: float) -> bool:
    o = (op or "gt").strip().lower()
    if o == "gt":
        return left > right
    if o == "gte":
        return left >= right
    if o == "lt":
        return left < right
    if o == "lte":
        return left <= right
    if o == "eq":
        return left == right
    return False


def metric_value_roi2_assist_from_dashboard_row(metric: str, row: Dict[str, Any]) -> Optional[float]:
    m = str(metric or "").strip()
    if m not in ALLOWED_METRICS_ROI2_ASSIST:
        return None
    v = row.get(m)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rate_compare_round(x: float) -> float:
    """率类比较前统一保留 6 位小数，减轻浮点误差。"""
    if not math.isfinite(x):
        return x
    return round(x, 6)


def evaluate_condition_roi2_assist(cond: Dict[str, Any], row: Dict[str, Any]) -> bool:
    metric = str(cond.get("metric") or "").strip()
    op = str(cond.get("op") or "gt").strip().lower()
    if op not in ALLOWED_OPS:
        op = "gt"
    try:
        threshold = float(cond.get("value", 0))
    except (TypeError, ValueError):
        threshold = 0.0
    actual = metric_value_roi2_assist_from_dashboard_row(metric, row)
    if actual is None:
        return False
    if metric in _RATE_METRICS_ROI2:
        # 实际值与阈值均按行数据 / 配置原始数值比较（与大屏原始展示一致）
        left = _rate_compare_round(float(actual))
        right = _rate_compare_round(float(threshold))
        return _compare_op_roi2(op, left, right)
    return _compare_op_roi2(op, float(actual), threshold)


def evaluate_group_roi2_assist(group: Dict[str, Any], row: Dict[str, Any]) -> bool:
    conditions = group.get("conditions") or []
    if not conditions:
        return False
    join = str(group.get("join") or "and").strip().lower()
    results = [
        evaluate_condition_roi2_assist(c, row) if isinstance(c, dict) else False for c in conditions
    ]
    if join == "or":
        return any(results)
    return all(results)


def evaluate_trigger_roi2_assist(trigger: Dict[str, Any], row: Dict[str, Any]) -> bool:
    gc = str(trigger.get("group_combine") or "or").strip().lower()
    if gc not in ALLOWED_GROUP_COMBINE:
        gc = "or"
    groups = trigger.get("groups") or []
    if not groups:
        return False
    gr = [evaluate_group_roi2_assist(g, row) if isinstance(g, dict) else False for g in groups]
    if gc == "and":
        return all(gr)
    return any(gr)


def build_trigger_evaluation_snapshot_roi2_assist(
    trigger: Dict[str, Any], row: Dict[str, Any]
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "group_combine": trigger.get("group_combine"),
        "groups": [],
        "passed": evaluate_trigger_roi2_assist(trigger, row),
    }
    for g in trigger.get("groups") or []:
        if not isinstance(g, dict):
            continue
        gd: Dict[str, Any] = {
            "join": g.get("join"),
            "conditions": [],
            "passed": evaluate_group_roi2_assist(g, row),
        }
        for c in g.get("conditions") or []:
            if not isinstance(c, dict):
                continue
            m = str(c.get("metric") or "").strip()
            act = metric_value_roi2_assist_from_dashboard_row(m, row)
            gd["conditions"].append(
                {
                    "metric": m,
                    "op": c.get("op"),
                    "threshold": c.get("value"),
                    "actual": act,
                    "passed": evaluate_condition_roi2_assist(c, row),
                }
            )
        out["groups"].append(gd)
    return out
