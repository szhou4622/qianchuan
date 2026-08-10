# -*- coding: utf-8 -*-
"""
规则化追投配置持久化（供 Api 使用）：存于 data/rule_retargeting.json（独立于 control_panel.json）。
多策略：`strategies[]` 每项含 `id`、`title`、`trigger`、`retargeting`；根级 `interval` 为全策略共用的限频。
旧版仅含根级 `trigger`/`retargeting` 的配置会在加载时规范化为单条策略并写入 `strategies`；
若磁盘上仍带根级冗余字段，首次读取时会去掉并写回文件。
实际执行见 services/retargeting_rule_runner.py、services/retargeting_service.py。
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from config import DATA_DIR

FILENAME = "rule_retargeting.json"
_lock = threading.RLock()

DEFAULT_TASK_SUFFIX = "素材看盘自动追投"
TASK_NAME_SUFFIX_MAX_LEN = 15

ALLOWED_METRICS = frozenset(
    {
        "costDiff",
        "currentCost",
        "estimatedEcpm",
        "netRoi",
        "netAmount",
        "overallPayRoi",
        "overallAmount",
        "hourRefundRate",
        "netSettleRate",
        "netOrderCount",
        "overallOrderCount",
        "overallShowCount",
        "overallClickCount",
        "overallCtr",
        "overallConversionRate",
        # Existing-control-task budget rules.  These two metrics are evaluated
        # against pmc_roi2_assist_task rows, never against individual material
        # rows.
        "assistCost",
        "assistRoi",
    }
)
ALLOWED_OPS = frozenset({"gt", "gte", "lt", "lte", "eq"})
ALLOWED_GROUP_COMBINE = frozenset({"or", "and"})
# 与 static/rule_retargeting.html RATE_METRICS 一致；触发阈值新版存百分数，旧版为小数 0~1
_RATE_TRIGGER_METRICS = frozenset(
    {
        "hourRefundRate",
        "netSettleRate",
        "overallCtr",
        "overallConversionRate",
    }
)
ALLOWED_METHOD = frozenset({"volume", "cost_control"})
ALLOWED_GOAL = frozenset({"net_roi", "live_room"})
ALLOWED_ACTION_MODES = frozenset({"card_confirm", "auto_execute"})
ALLOWED_TASK_ACTIONS = frozenset({"create_retarget", "increase_budget"})
ALLOWED_BUDGET_INCREASE_MODES = frozenset({"fixed", "spend_percentage"})
ASSIST_TASK_METRICS = frozenset({"assistCost", "assistRoi"})
ALLOWED_TRIGGER_LEVELS = frozenset({"material", "product"})
ALLOWED_CANDIDATE_SORTS = frozenset({"net_roi_desc"})
MAX_CANDIDATE_LIMIT = 20

# 追投间隔·时间范围（秒）：clamp 上下限与默认值（默认 24 小时窗口内最多 1 次）
_MAX_INTERVAL_WINDOW_SECONDS = 720.0 * 3600.0
_MIN_INTERVAL_WINDOW_SECONDS = 60.0
_DEFAULT_INTERVAL_WINDOW_SECONDS = 24.0 * 3600.0

# 多策略：最多条数（与前端一致）
MAX_STRATEGIES = 10
_STRATEGY_TITLE_MAX_LEN = 32


def config_path() -> str:
    return os.path.join(DATA_DIR, FILENAME)


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    _ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _normalize_json_whole_floats(obj: Any) -> Any:
    """写出 JSON 前：浮点若实为整数（如 1.0、122.0）则改为 int，避免 .0；真小数保留。"""
    if isinstance(obj, dict):
        return {k: _normalize_json_whole_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_json_whole_floats(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if math.isfinite(obj) and abs(obj - round(obj)) < 1e-9:
            return int(round(obj))
        return obj
    return obj


def _default_condition() -> Dict[str, Any]:
    return {"metric": "costDiff", "op": "gt", "value": 0.0}


def _default_group() -> Dict[str, Any]:
    return {"join": "and", "conditions": [_default_condition()]}


def _default_trigger() -> Dict[str, Any]:
    return {"group_combine": "or", "groups": [_default_group()]}


def _default_retargeting() -> Dict[str, Any]:
    return {
        "method": "volume",
        "volume": {
            "total_budget_yuan": None,
            "duration_hours": 1.0,
        },
        "cost_control": {
            "optimization_goal": "net_roi",
            "net_roi": {"daily_budget_yuan": None, "net_roi_target": None},
            "live_room": {"daily_budget_yuan": None, "bid_per_conversion_yuan": None},
        },
        "budget_increase": {
            "mode": "fixed",
            "fixed_amount_yuan": None,
            "spend_percentage": None,
            "volume_extend_hours": 1.0,
        },
        # 滑动窗口（秒）内允许的最大追投次数；默认 86400s = 24 小时 1 次（执行侧接入后用于限频）
        "interval": {"window_seconds": _DEFAULT_INTERVAL_WINDOW_SECONDS, "max_count": 1},
        "task_name_suffix": DEFAULT_TASK_SUFFIX,
    }


def _new_strategy_id() -> str:
    return str(uuid.uuid4())


def _default_strategy(index: int = 0) -> Dict[str, Any]:
    return {
        "id": _new_strategy_id(),
        "title": f"策略 {index + 1}",
        # 旧配置允许暂时为空。运行时仅在“唯一启用目标”时兼容绑定；
        # 新版前端保存时会显式选择监控计划。
        "account_uid": "",
        "target_uid": "",
        "trigger_level": "material",
        "product_filter": [],
        "candidate_trigger": _default_trigger(),
        "candidate_sort": "net_roi_desc",
        "candidate_limit": 1,
        "action_mode": "card_confirm",
        "task_action": "create_retarget",
        "trigger": _default_trigger(),
        "retargeting": _default_retargeting(),
    }


def _default_full() -> Dict[str, Any]:
    inv = {
        "window_seconds": int(_DEFAULT_INTERVAL_WINDOW_SECONDS),
        "max_count": 1,
    }
    s0 = _default_strategy(0)
    s0["retargeting"]["interval"] = copy.deepcopy(inv)
    return {
        "enabled": False,
        "browser_headless": True,
        "browser_executable_path": "",
        "trigger_query_period": "1h",
        "per_strategy_rate_limit": False,
        "interval": inv,
        "strategies": [s0],
    }


def _coerce_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _normalize_condition(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_condition()
    m = str(raw.get("metric") or "costDiff").strip()
    if m not in ALLOWED_METRICS:
        m = "costDiff"
    op = str(raw.get("op") or "gt").strip().lower()
    if op not in ALLOWED_OPS:
        op = "gt"
    val = _coerce_float(raw.get("value"), 0.0)
    return {"metric": m, "op": op, "value": val}


def _normalize_group(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_group()
    join = str(raw.get("join") or "and").strip().lower()
    if join != "and":
        join = "and"
    conds = raw.get("conditions")
    out_conds: List[Dict[str, Any]] = []
    if isinstance(conds, list) and conds:
        for c in conds:
            out_conds.append(_normalize_condition(c))
    else:
        out_conds = [_default_condition()]
    return {"join": join, "conditions": out_conds}


def _normalize_trigger(raw: Any) -> Dict[str, Any]:
    base = copy.deepcopy(_default_trigger())
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
            out_groups.append(_normalize_group(g))
    else:
        out_groups = [_default_group()]
    base["groups"] = out_groups
    return base


def _clamp_duration(h: Any) -> float:
    v = _coerce_float(h, 1.0)
    # 0.5 step, range [0.5, 24]
    if v < 0.5:
        v = 0.5
    if v > 24.0:
        v = 24.0
    # snap to 0.5
    steps = round((v - 0.5) / 0.5)
    return round(0.5 + steps * 0.5, 2)


# 与 static/rule_retargeting.html 中预算 / ROI / 出价校验对齐
_MIN_BUDGET_YUAN = 100.0
_MAX_BID_YUAN = 10000.0
_MIN_LIVE_BID_YUAN = 0.1
_RE_LEADING_ZERO_BAD = re.compile(r"^0\d")


def _float_at_most_two_decimal_places(n: float) -> bool:
    if not math.isfinite(n):
        return False
    return abs(n * 100 - round(n * 100)) < 1e-6


def _str_has_leading_zero_bad(s: str) -> bool:
    s = str(s).strip()
    return bool(s and _RE_LEADING_ZERO_BAD.match(s))


def _str_more_than_two_decimal_places(s: str) -> bool:
    s = str(s).strip()
    if "." not in s:
        return False
    frac = s.split(".", 1)[1]
    digits = "".join(c for c in frac if c.isdigit())
    if len(digits) == 0:
        return True  # 如 100.、12. 仅尾随小数点
    return len(digits) > 2


def _budget_yuan_error_msg(raw: Any) -> Optional[str]:
    """调控总预算 / 调控日预算，与前端 budgetYuanErrorMsg 一致。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "请输入有效数字"
    if isinstance(raw, (int, float)):
        n = float(raw)
        if not math.isfinite(n):
            return "请输入有效数字"
        if n <= 0:
            return "预算需大于 0 元"
        if n < _MIN_BUDGET_YUAN:
            return "预算不能低于100元"
        if not _float_at_most_two_decimal_places(n):
            return "仅支持最多2位小数"
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _str_has_leading_zero_bad(s):
        return "不能以0开头，请正确输入"
    if _str_more_than_two_decimal_places(s):
        return "仅支持最多2位小数"
    try:
        n = float(s)
    except Exception:
        return "请输入有效数字"
    if not math.isfinite(n):
        return "请输入有效数字"
    if n <= 0:
        return "预算需大于 0 元"
    if n < _MIN_BUDGET_YUAN:
        return "预算不能低于100元"
    return None


def _budget_increment_yuan_error_msg(raw: Any) -> Optional[str]:
    """追加预算的固定增量：必须大于0，最多两位小数。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "请填写每次新增金额"
    if isinstance(raw, bool):
        return "请输入有效数字"
    s = str(raw).strip()
    if _str_has_leading_zero_bad(s):
        return "不能以0开头，请正确输入"
    if _str_more_than_two_decimal_places(s):
        return "仅支持最多2位小数"
    try:
        value = float(s)
    except Exception:
        return "请输入有效数字"
    if not math.isfinite(value) or value <= 0:
        return "每次新增金额需大于0元"
    return None


def _spend_percentage_error_msg(raw: Any) -> Optional[str]:
    """按最新调控消耗计算的预算增量百分比。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "请填写消耗金额百分比"
    if isinstance(raw, bool):
        return "请输入有效百分比"
    s = str(raw).strip()
    if _str_has_leading_zero_bad(s):
        return "不能以0开头，请正确输入"
    if _str_more_than_two_decimal_places(s):
        return "百分比最多保留2位小数"
    try:
        value = float(s)
    except Exception:
        return "请输入有效百分比"
    if not math.isfinite(value) or value <= 0 or value > 1000:
        return "消耗金额百分比须在0到1000之间"
    return None


def _net_roi_target_error_msg(raw: Any) -> Optional[str]:
    """净成交 ROI 目标，与前端 netRoiTargetErrorMsg 一致。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "请输入有效数字"
    if isinstance(raw, (int, float)):
        n = float(raw)
        if not math.isfinite(n):
            return "请输入有效数字"
        if n < 0.01 or n > 100:
            return "支持范围: 0.01-100，最多两位小数"
        if not _float_at_most_two_decimal_places(n):
            return "仅支持最多2位小数"
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _str_has_leading_zero_bad(s):
        return "不能以0开头，请正确输入"
    if _str_more_than_two_decimal_places(s):
        return "仅支持最多2位小数"
    try:
        n = float(s)
    except Exception:
        return "请输入有效数字"
    if not math.isfinite(n):
        return "请输入有效数字"
    if n < 0.01 or n > 100:
        return "支持范围: 0.01-100，最多两位小数"
    return None


def _duration_hours_error_msg(raw: Any) -> Optional[str]:
    """调控时长，与前端 validateDurationText 一致。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "调控时长须为数字"
    if isinstance(raw, (int, float)):
        dv = float(raw)
    else:
        s = str(raw).strip()
        if not s:
            return None
        try:
            dv = float(s)
        except Exception:
            return "调控时长须为数字"
    if not math.isfinite(dv):
        return "调控时长须为数字"
    if dv < 0.5 or dv > 24:
        return "调控时长范围 0.5～24 小时，请正确填写"
    steps = round((dv - 0.5) / 0.5)
    expected = 0.5 + steps * 0.5
    if abs(dv - expected) > 1e-4:
        return "调控时长需为0.5的整数倍，请正确填写"
    return None


def _bid_per_conversion_error_msg(raw: Any, daily_budget_raw: Any) -> Optional[str]:
    """直播间「我的出价」，与前端 ccBid 一致：最多两位小数等。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "请输入有效数字"
    if isinstance(raw, (int, float)):
        bid = float(raw)
        if not math.isfinite(bid):
            return "请输入有效数字"
        if not _float_at_most_two_decimal_places(bid):
            return "仅支持最多2位小数"
    else:
        s = str(raw).strip()
        if not s:
            return None
        if _str_has_leading_zero_bad(s):
            return "不能以0开头，请正确输入"
        if _str_more_than_two_decimal_places(s):
            return "仅支持最多2位小数"
        try:
            bid = float(s)
        except Exception:
            return "请输入有效数字"
        if not math.isfinite(bid):
            return "请输入有效数字"
    if bid < _MIN_LIVE_BID_YUAN:
        return "出价不能低于0.1元"
    if bid > _MAX_BID_YUAN:
        return "出价不能高于10,000元"
    if isinstance(daily_budget_raw, (int, float)) and not isinstance(daily_budget_raw, bool):
        db = float(daily_budget_raw)
        if math.isfinite(db) and bid > db:
            return "出价不能高于预算"
    elif daily_budget_raw is not None and str(daily_budget_raw).strip():
        try:
            db = float(str(daily_budget_raw).strip())
        except Exception:
            pass
        else:
            if math.isfinite(db) and bid > db:
                return "出价不能高于预算"
    return None


def _interval_window_seconds_error_msg(raw: Any) -> Optional[str]:
    """时间范围秒数：须为正整数，且 ≥60，与前端 intervalWindowSecondsErrorMsg 一致。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "时间范围须为整数秒"
    if isinstance(raw, (int, float)):
        wv = float(raw)
        if not math.isfinite(wv):
            return "时间范围须为数字"
        if abs(wv - round(wv)) > 1e-9:
            return "时间范围须为整数秒"
        iv = int(round(wv))
        if iv < int(_MIN_INTERVAL_WINDOW_SECONDS):
            return "时间范围不能低于 60 秒"
        if iv > _MAX_INTERVAL_WINDOW_SECONDS:
            return f"时间范围须在 60～{int(_MAX_INTERVAL_WINDOW_SECONDS)} 秒之间"
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _str_has_leading_zero_bad(s):
        return "不能以0开头，请正确输入"
    if not s.isdigit():
        return "时间范围须为整数秒"
    iv = int(s, 10)
    if iv < int(_MIN_INTERVAL_WINDOW_SECONDS):
        return "时间范围不能低于 60 秒"
    if iv > _MAX_INTERVAL_WINDOW_SECONDS:
        return f"时间范围须在 60～{int(_MAX_INTERVAL_WINDOW_SECONDS)} 秒之间"
    return None


def _interval_max_count_error_msg(raw: Any) -> Optional[str]:
    """追投次数上限：须为正整数，且 ≥1，与前端 intervalMaxCountErrorMsg 一致。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "追投次数须为整数"
    if isinstance(raw, (int, float)):
        mv = float(raw)
        if not math.isfinite(mv):
            return "追投次数须为数字"
        if abs(mv - round(mv)) > 1e-9:
            return "追投次数须为整数"
        iv = int(round(mv))
        if iv < 1:
            return "追投次数须大于等于 1"
        if iv > 9999:
            return "追投次数须在 1～9999 之间"
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _str_has_leading_zero_bad(s):
        return "不能以0开头，请正确输入"
    if not s.isdigit():
        return "追投次数须为整数"
    iv = int(s, 10)
    if iv < 1:
        return "追投次数须大于等于 1"
    if iv > 9999:
        return "追投次数须在 1～9999 之间"
    return None


def _clamp_interval_window_seconds(s: Any) -> float:
    v = _coerce_float(s, _DEFAULT_INTERVAL_WINDOW_SECONDS)
    if v < _MIN_INTERVAL_WINDOW_SECONDS:
        v = _MIN_INTERVAL_WINDOW_SECONDS
    if v > _MAX_INTERVAL_WINDOW_SECONDS:
        v = _MAX_INTERVAL_WINDOW_SECONDS
    return float(int(round(v)))


def _interval_window_seconds_from_inv(inv: Dict[str, Any]) -> float:
    """优先 window_seconds；兼容旧字段 window_hours（×3600）。"""
    if inv.get("window_seconds") is not None:
        return _clamp_interval_window_seconds(inv.get("window_seconds"))
    if inv.get("window_hours") is not None:
        try:
            h = float(inv.get("window_hours"))
        except Exception:
            h = 24.0
        return _clamp_interval_window_seconds(h * 3600.0)
    return float(_DEFAULT_INTERVAL_WINDOW_SECONDS)


def _clamp_interval_max_count(n: Any) -> int:
    try:
        v = int(round(float(n)))
    except Exception:
        v = 1
    if v < 1:
        v = 1
    if v > 9999:
        v = 9999
    return v


def _normalize_retargeting(raw: Any) -> Dict[str, Any]:
    base = copy.deepcopy(_default_retargeting())
    if not isinstance(raw, dict):
        return base
    r = raw
    method = str(r.get("method") or "volume").strip().lower()
    if method not in ALLOWED_METHOD:
        method = "volume"
    base["method"] = method

    vol = r.get("volume")
    if isinstance(vol, dict):
        tb = vol.get("total_budget_yuan")
        if tb is None or (isinstance(tb, str) and not str(tb).strip()):
            base["volume"]["total_budget_yuan"] = None
        else:
            try:
                base["volume"]["total_budget_yuan"] = float(tb)
            except Exception:
                base["volume"]["total_budget_yuan"] = None
        base["volume"]["duration_hours"] = _clamp_duration(vol.get("duration_hours"))

    cc = r.get("cost_control")
    if isinstance(cc, dict):
        og = str(cc.get("optimization_goal") or "net_roi").strip().lower()
        if og not in ALLOWED_GOAL:
            og = "net_roi"
        base["cost_control"]["optimization_goal"] = og
        nr = cc.get("net_roi")
        if isinstance(nr, dict):
            for k, key in (("daily_budget_yuan", "daily_budget_yuan"), ("net_roi_target", "net_roi_target")):
                x = nr.get(k)
                if x is None or (isinstance(x, str) and not str(x).strip()):
                    base["cost_control"]["net_roi"][key] = None
                else:
                    try:
                        base["cost_control"]["net_roi"][key] = float(x)
                    except Exception:
                        base["cost_control"]["net_roi"][key] = None
        lr = cc.get("live_room")
        if isinstance(lr, dict):
            for k, key in (("daily_budget_yuan", "daily_budget_yuan"), ("bid_per_conversion_yuan", "bid_per_conversion_yuan")):
                x = lr.get(k)
                if x is None or (isinstance(x, str) and not str(x).strip()):
                    base["cost_control"]["live_room"][key] = None
                else:
                    try:
                        base["cost_control"]["live_room"][key] = float(x)
                    except Exception:
                        base["cost_control"]["live_room"][key] = None

    increase = r.get("budget_increase")
    if isinstance(increase, dict):
        mode = str(increase.get("mode") or "fixed").strip().lower()
        if mode not in ALLOWED_BUDGET_INCREASE_MODES:
            mode = "fixed"
        base["budget_increase"]["mode"] = mode
        for key in ("fixed_amount_yuan", "spend_percentage"):
            value = increase.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                base["budget_increase"][key] = None
            else:
                try:
                    base["budget_increase"][key] = float(value)
                except Exception:
                    base["budget_increase"][key] = None
        base["budget_increase"]["volume_extend_hours"] = _clamp_duration(
            increase.get("volume_extend_hours")
        )

    inv = r.get("interval")
    if isinstance(inv, dict):
        base["interval"]["window_seconds"] = _interval_window_seconds_from_inv(inv)
        base["interval"]["max_count"] = _clamp_interval_max_count(inv.get("max_count"))
    else:
        base["interval"] = {"window_seconds": _DEFAULT_INTERVAL_WINDOW_SECONDS, "max_count": 1}

    suf = r.get("task_name_suffix")
    s = str(suf).strip() if suf is not None else ""
    if len(s) > TASK_NAME_SUFFIX_MAX_LEN:
        s = s[:TASK_NAME_SUFFIX_MAX_LEN]
    base["task_name_suffix"] = s if s else DEFAULT_TASK_SUFFIX

    return base


def _normalize_global_interval_dict(inv_raw: Any) -> Dict[str, Any]:
    """根级 interval：全策略共用限频（与每条 retargeting.interval 写入相同值）。"""
    if not isinstance(inv_raw, dict):
        return {
            "window_seconds": int(_DEFAULT_INTERVAL_WINDOW_SECONDS),
            "max_count": 1,
        }
    return {
        "window_seconds": int(_interval_window_seconds_from_inv(inv_raw)),
        "max_count": _clamp_interval_max_count(inv_raw.get("max_count")),
    }


def _normalize_strategy_entry(
    raw: Any,
    index: int,
    global_inv: Dict[str, Any],
    *,
    per_strategy_interval: bool,
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
    trig = _normalize_trigger(raw.get("trigger"))
    ret = _normalize_retargeting(raw.get("retargeting"))
    action_mode = str(raw.get("action_mode") or "card_confirm").strip().lower()
    if action_mode not in ALLOWED_ACTION_MODES:
        action_mode = "card_confirm"
    task_action = str(raw.get("task_action") or "create_retarget").strip().lower()
    if task_action not in ALLOWED_TASK_ACTIONS:
        task_action = "create_retarget"
    account_uid = str(raw.get("account_uid") or "").strip()
    target_uid = str(raw.get("target_uid") or "").strip()
    trigger_level = str(raw.get("trigger_level") or "material").strip().lower()
    if trigger_level not in ALLOWED_TRIGGER_LEVELS:
        trigger_level = "material"
    product_filter_raw = raw.get("product_filter")
    product_filter: List[str] = []
    if isinstance(product_filter_raw, list):
        seen_product_ids = set()
        for value in product_filter_raw:
            product_id = str(value or "").strip()
            if product_id and product_id not in seen_product_ids:
                seen_product_ids.add(product_id)
                product_filter.append(product_id)
    candidate_trigger = _normalize_trigger(raw.get("candidate_trigger"))
    candidate_sort = str(raw.get("candidate_sort") or "net_roi_desc").strip().lower()
    if candidate_sort not in ALLOWED_CANDIDATE_SORTS:
        candidate_sort = "net_roi_desc"
    try:
        candidate_limit = int(raw.get("candidate_limit") or 1)
    except (TypeError, ValueError):
        candidate_limit = 1
    candidate_limit = max(1, min(MAX_CANDIDATE_LIMIT, candidate_limit))
    if per_strategy_interval:
        rraw = raw.get("retargeting")
        if not isinstance(rraw, dict) or not isinstance(rraw.get("interval"), dict):
            ret["interval"] = copy.deepcopy(global_inv)
    else:
        ret["interval"] = copy.deepcopy(global_inv)
    return {
        "id": sid,
        "title": title,
        "account_uid": account_uid,
        "target_uid": target_uid,
        "trigger_level": trigger_level,
        "product_filter": product_filter,
        "candidate_trigger": candidate_trigger,
        "candidate_sort": candidate_sort,
        "candidate_limit": candidate_limit,
        "action_mode": action_mode,
        "task_action": task_action,
        "trigger": trig,
        "retargeting": ret,
    }


def _disk_needs_strategy_migration_rewrite(raw: Optional[Dict[str, Any]]) -> bool:
    """
    磁盘上是否需要迁移并写回：
    - 无有效 strategies，但根级同时存在 trigger、retargeting（旧版单规则）→ 写入 strategies；
    - 已有 strategies，但仍带根级 trigger/retargeting 冗余 → 删除根级字段。
    """
    if not raw or not isinstance(raw, dict):
        return False
    strats = raw.get("strategies")
    has_nonempty_strategies = isinstance(strats, list) and len(strats) > 0
    trig_ok = isinstance(raw.get("trigger"), dict)
    ret_ok = isinstance(raw.get("retargeting"), dict)
    if not has_nonempty_strategies and trig_ok and ret_ok:
        return True
    if has_nonempty_strategies and ("trigger" in raw or "retargeting" in raw):
        return True
    if has_nonempty_strategies:
        for strategy in strats:
            if not isinstance(strategy, dict):
                return True
            mode = str(strategy.get("action_mode") or "").strip().lower()
            if mode not in ALLOWED_ACTION_MODES:
                return True
            if any(
                key not in strategy
                for key in (
                    "account_uid",
                    "target_uid",
                    "trigger_level",
                    "product_filter",
                    "candidate_trigger",
                    "candidate_sort",
                    "candidate_limit",
                    "task_action",
                )
            ):
                return True
    return False


def _normalize_full(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = copy.deepcopy(_default_full())
    if not raw or not isinstance(raw, dict):
        return _normalize_json_whole_floats(base)

    base["enabled"] = bool(raw.get("enabled", False))
    base["browser_headless"] = bool(raw.get("browser_headless", True))
    base["browser_executable_path"] = str(raw.get("browser_executable_path") or "").strip()
    tqp = str(raw.get("trigger_query_period") or "1h").strip().lower()
    base["trigger_query_period"] = tqp if tqp else "1h"

    strategies_in: List[Any] = []
    if isinstance(raw.get("strategies"), list) and raw["strategies"]:
        strategies_in = list(raw["strategies"])[:MAX_STRATEGIES]
    else:
        # 旧版：根级 trigger + retargeting → 单条策略
        strategies_in = [
            {
                "id": raw.get("legacy_strategy_id") or _new_strategy_id(),
                "title": "策略 1",
                "trigger": raw.get("trigger"),
                "retargeting": raw.get("retargeting"),
            }
        ]

    # 先解析全局 interval：优先根级 interval；否则从首条 retargeting.interval 推断
    g_inv: Dict[str, Any]
    if isinstance(raw.get("interval"), dict):
        g_inv = _normalize_global_interval_dict(raw["interval"])
    else:
        first_ret = None
        if strategies_in and isinstance(strategies_in[0], dict):
            first_ret = strategies_in[0].get("retargeting")
        if isinstance(first_ret, dict) and isinstance(first_ret.get("interval"), dict):
            g_inv = _normalize_global_interval_dict(first_ret["interval"])
        else:
            g_inv = _normalize_global_interval_dict(None)

    base["interval"] = copy.deepcopy(g_inv)
    per_str = bool(raw.get("per_strategy_rate_limit"))
    base["per_strategy_rate_limit"] = per_str
    out_strategies: List[Dict[str, Any]] = []
    for i, s in enumerate(strategies_in):
        out_strategies.append(
            _normalize_strategy_entry(s, i, g_inv, per_strategy_interval=per_str)
        )
    if not out_strategies:
        out_strategies = [_normalize_strategy_entry({}, 0, g_inv, per_strategy_interval=per_str)]

    base["strategies"] = out_strategies
    if out_strategies:
        base.pop("trigger", None)
        base.pop("retargeting", None)
    return _normalize_json_whole_floats(base)


def load_rule_retargeting_config() -> Dict[str, Any]:
    with _lock:
        path = config_path()
        disk = _read_json(path)
        out = _normalize_full(disk)
        if _disk_needs_strategy_migration_rewrite(disk):
            _atomic_write(path, out)
        return out


def _validate_retargeting_block(r: Dict[str, Any], *, validate_interval: bool) -> Tuple[bool, str]:
    """校验单条调控任务（与旧版单规则 retargeting 字段一致）。"""
    method = str(r.get("method") or "volume").lower()
    if method == "volume":
        vol = r.get("volume")
        if isinstance(vol, dict):
            tb = vol.get("total_budget_yuan")
            if tb is not None and str(tb).strip() != "":
                err = _budget_yuan_error_msg(tb)
                if err:
                    return False, err
            d = vol.get("duration_hours")
            if d is not None:
                err = _duration_hours_error_msg(d)
                if err:
                    return False, err
    if method == "cost_control":
        cc = r.get("cost_control")
        if isinstance(cc, dict):
            og = str(cc.get("optimization_goal") or "").lower()
            if og == "net_roi":
                nr = cc.get("net_roi")
                if isinstance(nr, dict):
                    x = nr.get("daily_budget_yuan")
                    if x is not None and str(x).strip() != "":
                        err = _budget_yuan_error_msg(x)
                        if err:
                            return False, err
                    y = nr.get("net_roi_target")
                    if y is not None and str(y).strip() != "":
                        err = _net_roi_target_error_msg(y)
                        if err:
                            return False, err
            if og == "live_room":
                lr = cc.get("live_room")
                if isinstance(lr, dict):
                    x = lr.get("daily_budget_yuan")
                    if x is not None and str(x).strip() != "":
                        err = _budget_yuan_error_msg(x)
                        if err:
                            return False, err
                    b = lr.get("bid_per_conversion_yuan")
                    if b is not None and str(b).strip() != "":
                        err = _bid_per_conversion_error_msg(b, lr.get("daily_budget_yuan"))
                        if err:
                            return False, err
    if validate_interval:
        inv = r.get("interval")
        if inv is not None:
            if not isinstance(inv, dict):
                return False, "追投间隔须为对象"
            ws = inv.get("window_seconds")
            wh_legacy = inv.get("window_hours")
            if ws is not None and str(ws).strip() != "":
                err = _interval_window_seconds_error_msg(ws)
                if err:
                    return False, err
            elif wh_legacy is not None and str(wh_legacy).strip() != "":
                try:
                    hv = float(wh_legacy)
                except Exception:
                    return False, "追投间隔·时间范围（小时，已废弃）须为数字"
                if hv < 1 or hv > 720:
                    return False, "追投间隔·时间范围（小时）须在 1～720 之间"
            mc = inv.get("max_count")
            if mc is not None and str(mc).strip() != "":
                err = _interval_max_count_error_msg(mc)
                if err:
                    return False, err
    return True, ""


def _validate_budget_increase_block(r: Dict[str, Any]) -> Tuple[bool, str]:
    increase = r.get("budget_increase")
    if not isinstance(increase, dict):
        return False, "缺少追加预算设置"
    mode = str(increase.get("mode") or "fixed").strip().lower()
    if mode not in ALLOWED_BUDGET_INCREASE_MODES:
        return False, "追加预算计算方式无效"
    if mode == "fixed":
        error = _budget_increment_yuan_error_msg(increase.get("fixed_amount_yuan"))
    else:
        error = _spend_percentage_error_msg(increase.get("spend_percentage"))
    if error:
        return False, error
    duration_error = _duration_hours_error_msg(increase.get("volume_extend_hours"))
    if duration_error:
        return False, duration_error
    return True, ""


def _trigger_metrics(trigger: Any) -> List[str]:
    metrics: List[str] = []
    if not isinstance(trigger, dict):
        return metrics
    for group in trigger.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for condition in group.get("conditions") or []:
            if isinstance(condition, dict):
                metric = str(condition.get("metric") or "").strip()
                if metric:
                    metrics.append(metric)
    return metrics


def validate_rule_retargeting_config(data: Dict[str, Any]) -> Tuple[bool, str]:
    """提交前校验（normalize 前可调用；多策略时每条均校验）。"""
    if not isinstance(data, dict):
        return False, "配置须为对象"
    strats = data.get("strategies")
    if isinstance(strats, list) and len(strats) > MAX_STRATEGIES:
        return False, f"追投策略最多 {MAX_STRATEGIES} 条"

    per_str = bool(data.get("per_strategy_rate_limit"))

    # 全局 interval（与运行配置里「追投次数限制」一致）；分策略限频时由每条策略的 interval 生效，根级仅作草稿
    if not per_str:
        g_inv = data.get("interval")
        if isinstance(g_inv, dict):
            ws = g_inv.get("window_seconds")
            wh_legacy = g_inv.get("window_hours")
            if ws is not None and str(ws).strip() != "":
                err = _interval_window_seconds_error_msg(ws)
                if err:
                    return False, err
            elif wh_legacy is not None and str(wh_legacy).strip() != "":
                try:
                    hv = float(wh_legacy)
                except Exception:
                    return False, "追投间隔·时间范围（小时，已废弃）须为数字"
                if hv < 1 or hv > 720:
                    return False, "追投间隔·时间范围（小时）须在 1～720 之间"
            mc = g_inv.get("max_count")
            if mc is not None and str(mc).strip() != "":
                err = _interval_max_count_error_msg(mc)
                if err:
                    return False, err

    # 未启用时：不校验各策略调控参数（允许先关掉再改表单；避免「关不掉」）
    if not bool(data.get("enabled", False)):
        return True, ""

    if isinstance(strats, list) and strats:
        for i, st in enumerate(strats):
            if not isinstance(st, dict):
                return False, "追投策略项须为对象"
            if not str(st.get("target_uid") or "").strip():
                return False, f"策略{i + 1} 必须选择监控计划"
            trigger_level = str(st.get("trigger_level") or "material").strip().lower()
            if trigger_level not in ALLOWED_TRIGGER_LEVELS:
                return False, f"策略{i + 1} 的触发层级无效"
            candidate_sort = str(st.get("candidate_sort") or "net_roi_desc").strip().lower()
            if candidate_sort not in ALLOWED_CANDIDATE_SORTS:
                return False, f"策略{i + 1} 的候选素材排序方式无效"
            try:
                candidate_limit = int(st.get("candidate_limit", 1))
            except (TypeError, ValueError):
                return False, f"策略{i + 1} 的候选素材数量必须为整数"
            if candidate_limit < 1 or candidate_limit > MAX_CANDIDATE_LIMIT:
                return False, f"策略{i + 1} 的候选素材数量必须在 1 到 {MAX_CANDIDATE_LIMIT} 之间"
            if trigger_level == "product" and not isinstance(st.get("candidate_trigger"), dict):
                return False, f"策略{i + 1} 缺少候选素材条件"
            product_filter = st.get("product_filter")
            if product_filter is not None and not isinstance(product_filter, list):
                return False, f"策略{i + 1} 的商品筛选必须为列表"
            mode = str(st.get("action_mode") or "card_confirm").strip().lower()
            if mode not in ALLOWED_ACTION_MODES:
                return False, f"策略{i + 1} 的执行方式无效"
            task_action = str(st.get("task_action") or "create_retarget").strip().lower()
            if task_action not in ALLOWED_TASK_ACTIONS:
                return False, f"策略{i + 1} 的调控动作无效"
            metrics = _trigger_metrics(st.get("trigger"))
            if task_action == "increase_budget":
                if not metrics or any(metric not in ASSIST_TASK_METRICS for metric in metrics):
                    return False, f"策略{i + 1} 追加预算时只能使用调控消耗和调控ROI"
            elif any(metric in ASSIST_TASK_METRICS for metric in metrics):
                return False, f"策略{i + 1} 新建追投时不能使用调控任务指标"
            r = st.get("retargeting")
            if not isinstance(r, dict):
                return False, f"策略{i + 1} 缺少调控任务"
            ok, msg = _validate_retargeting_block(r, validate_interval=per_str)
            if not ok:
                return False, msg
            if task_action == "increase_budget":
                ok, msg = _validate_budget_increase_block(r)
                if not ok:
                    return False, msg
        return True, ""

    r = data.get("retargeting")
    if r is not None and isinstance(r, dict):
        ok, msg = _validate_retargeting_block(r, validate_interval=not per_str)
        if not ok:
            return False, msg
    return True, ""


def validate_strategy_target_compatibility(
    data: Dict[str, Any],
    targets_by_uid: Dict[str, Dict[str, Any]],
) -> Tuple[bool, str]:
    """校验依赖监控目标场景的追投方式，避免保存必然失败的配置。"""
    if not isinstance(data, dict) or not bool(data.get("enabled", False)):
        return True, ""
    strategies = data.get("strategies")
    if not isinstance(strategies, list):
        return True, ""
    for index, strategy in enumerate(strategies):
        if not isinstance(strategy, dict):
            continue
        target_uid = str(strategy.get("target_uid") or "").strip()
        target = targets_by_uid.get(target_uid)
        if not isinstance(target, dict):
            title = str(strategy.get("title") or f"策略{index + 1}").strip()
            return False, f"“{title}”选择的监控计划不存在，请重新选择"
        account_uid = str(strategy.get("account_uid") or "").strip()
        target_account_uid = str(target.get("account_uid") or "").strip()
        if account_uid and account_uid != target_account_uid:
            title = str(strategy.get("title") or f"策略{index + 1}").strip()
            return False, f"“{title}”选择的监控账户与计划不一致，请重新选择"
        if "account_enabled" in target and not bool(target.get("account_enabled")):
            title = str(strategy.get("title") or f"策略{index + 1}").strip()
            return False, f"“{title}”选择的千川账户已停用，请先启用账户"
        if not bool(target.get("enabled")):
            title = str(strategy.get("title") or f"策略{index + 1}").strip()
            return False, f"“{title}”选择的监控计划已停用，请先启用计划"
        if "retarget_eligible" in target and not bool(target.get("retarget_eligible")):
            title = str(strategy.get("title") or f"策略{index + 1}").strip()
            reason = str(target.get("ineligible_reason") or "计划尚未取得追投资格").strip()
            return False, f"“{title}”当前不可用于追投：{reason}"
        scene = str(target.get("promotion_scene") or "live").strip().lower()
        retargeting = strategy.get("retargeting")
        if not isinstance(retargeting, dict):
            continue
        if str(strategy.get("task_action") or "create_retarget").strip().lower() == "increase_budget":
            # Existing-task adjustment derives volume/ROI/conversion type from
            # the matched control-task row; the source-plan scene does not use
            # the create-retarget method restriction below.
            continue
        method = str(retargeting.get("method") or "volume").strip().lower()
        if scene == "product" and method == "cost_control":
            title = str(strategy.get("title") or f"策略{index + 1}").strip()
            return (
                False,
                f"“{title}”选择的是推商品计划；推商品当前仅支持放量追投，"
                "不能保存控成本追投。推直播仍可使用控成本追投。",
            )
    return True, ""


# ---------- 大屏行字典与触发条件求值（与 DashboardApi.get_table_data 返回的 data 项字段名一致，camelCase）----------

def metric_value_from_dashboard_row(metric: str, row: Dict[str, Any]) -> Optional[float]:
    """从大屏表格行取指标数值；无键或不可转 float 则返回 None。"""
    if metric not in ALLOWED_METRICS:
        return None
    assist_field = {
        "assistCost": "stat_cost_for_roi2_assist",
        "assistRoi": "total_prepay_and_pay_order_roi2_assist",
    }.get(metric)
    v = row.get(assist_field or metric)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _compare_op(op: str, left: float, right: float) -> bool:
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


def _rate_trigger_compare_round(x: float) -> float:
    if not math.isfinite(x):
        return x
    return round(x, 6)


def evaluate_condition(cond: Dict[str, Any], row: Dict[str, Any]) -> bool:
    metric = str(cond.get("metric") or "").strip()
    op = str(cond.get("op") or "gt").strip().lower()
    if op not in ALLOWED_OPS:
        op = "gt"
    try:
        threshold = float(cond.get("value", 0))
    except (TypeError, ValueError):
        threshold = 0.0
    actual = metric_value_from_dashboard_row(metric, row)
    if actual is None:
        return False
    if metric in _RATE_TRIGGER_METRICS:
        # 实际值与阈值均按行数据 / 配置原始数值比较（与大屏「原始值+%」一致），不做 |x|<=1 换算
        left = _rate_trigger_compare_round(float(actual))
        right = _rate_trigger_compare_round(float(threshold))
        return _compare_op(op, left, right)
    return _compare_op(op, float(actual), threshold)


def evaluate_group(group: Dict[str, Any], row: Dict[str, Any]) -> bool:
    conditions = group.get("conditions") or []
    if not conditions:
        return False
    join = str(group.get("join") or "and").strip().lower()
    results = [evaluate_condition(c, row) if isinstance(c, dict) else False for c in conditions]
    if join == "or":
        return any(results)
    return all(results)


def evaluate_trigger(trigger: Dict[str, Any], row: Dict[str, Any]) -> bool:
    gc = str(trigger.get("group_combine") or "or").strip().lower()
    if gc not in ALLOWED_GROUP_COMBINE:
        gc = "or"
    groups = trigger.get("groups") or []
    if not groups:
        return False
    gr = [evaluate_group(g, row) if isinstance(g, dict) else False for g in groups]
    if gc == "and":
        return all(gr)
    return any(gr)


def build_trigger_evaluation_snapshot(trigger: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """每条素材一条：记录各条件阈值与实际值、组内/组间是否通过。"""
    out: Dict[str, Any] = {
        "group_combine": trigger.get("group_combine"),
        "groups": [],
        "passed": evaluate_trigger(trigger, row),
    }
    for g in trigger.get("groups") or []:
        if not isinstance(g, dict):
            continue
        gd: Dict[str, Any] = {
            "join": g.get("join"),
            "conditions": [],
            "passed": evaluate_group(g, row),
        }
        for c in g.get("conditions") or []:
            if not isinstance(c, dict):
                continue
            m = str(c.get("metric") or "").strip()
            act = metric_value_from_dashboard_row(m, row)
            gd["conditions"].append(
                {
                    "metric": m,
                    "op": c.get("op"),
                    "threshold": c.get("value"),
                    "actual": act,
                    "passed": evaluate_condition(c, row),
                }
            )
        out["groups"].append(gd)
    return out


def preview_merge(partial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """与当前磁盘配置合并并规范化，不写入。"""
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
            if "per_strategy_rate_limit" in partial:
                cur["per_strategy_rate_limit"] = bool(partial["per_strategy_rate_limit"])
            if "interval" in partial and isinstance(partial["interval"], dict):
                cur["interval"] = partial["interval"]
            if "strategies" in partial and isinstance(partial["strategies"], list):
                cur["strategies"] = partial["strategies"]
            if "trigger" in partial and isinstance(partial["trigger"], dict):
                cur["trigger"] = partial["trigger"]
            if "retargeting" in partial and isinstance(partial["retargeting"], dict):
                cur["retargeting"] = partial["retargeting"]
        return _normalize_full(cur)


def merge_and_save(partial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """与当前磁盘配置合并后规范化并保存。trigger / retargeting 若传入则整体替换再规范化。"""
    with _lock:
        cur = preview_merge(partial)
        _atomic_write(config_path(), cur)
        return cur


def save_rule_retargeting_config(partial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """别名：合并保存。"""
    return merge_and_save(partial)
