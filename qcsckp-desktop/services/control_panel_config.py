# -*- coding: utf-8 -*-
"""
服务管理页配置：统一存于 data/control_panel.json，结构为：

    {
      "crawl": {
        "interval_seconds", "headless_poll", "fetch_assist_tasks",
        "browser_executable_path",
      },
      "feishu_table": { "enabled", "app_token", "personal_base_token", "table_id", "push_mode" },
      "robot": {
        "feishu": { "enabled", "webhook", "keyword" },
        "dingtalk": { "enabled", "webhook", "keyword" }
      }
    }

任意一项保存时整文件重写。唯一旧版兼容：若存在 feishu_webhook_push.json，则读入并入 robot.feishu 后删除该文件。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict, Optional, Tuple

from config import DATA_DIR

UNIFIED_FILENAME = "control_panel.json"

# 仅兼容：更早版本单独存放的飞书 Webhook 整点推送配置
_LEGACY_FEISHU_WH = "feishu_webhook_push.json"

_lock = threading.RLock()


def unified_config_path() -> str:
    return os.path.join(DATA_DIR, UNIFIED_FILENAME)


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    _ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _default_full() -> Dict[str, Any]:
    return {
        "crawl": {
            "interval_seconds": 600,
            "headless_poll": True,
            "fetch_assist_tasks": True,
            "browser_executable_path": "",
        },
        "feishu_table": {
            "enabled": False,
            "app_token": "",
            "personal_base_token": "",
            "table_id": "",
            "push_mode": "each_crawl",
        },
        "robot": {
            "feishu": {"enabled": False, "webhook": "", "keyword": ""},
            "dingtalk": {"enabled": False, "webhook": "", "keyword": ""},
        },
    }


def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _normalize_full(raw: Dict[str, Any]) -> Dict[str, Any]:
    """与默认结构合并，缺键补齐。"""
    base = copy.deepcopy(_default_full())
    if not isinstance(raw, dict):
        return base

    c = raw.get("crawl")
    if isinstance(c, dict):
        if "interval_seconds" in c:
            try:
                base["crawl"]["interval_seconds"] = max(5, int(c["interval_seconds"]))
            except Exception:
                pass
        if "headless_poll" in c:
            base["crawl"]["headless_poll"] = bool(c["headless_poll"])
        if "fetch_assist_tasks" in c:
            base["crawl"]["fetch_assist_tasks"] = bool(c["fetch_assist_tasks"])
        if "browser_executable_path" in c:
            base["crawl"]["browser_executable_path"] = str(
                c.get("browser_executable_path") or ""
            ).strip()

    ft = raw.get("feishu_table")
    if isinstance(ft, dict):
        if "enabled" in ft:
            base["feishu_table"]["enabled"] = bool(ft.get("enabled"))
        for k in ("app_token", "personal_base_token", "table_id"):
            if k in ft:
                base["feishu_table"][k] = str(ft.get(k) or "").strip()
        if "push_mode" in ft:
            pm = str(ft.get("push_mode") or "").strip().lower()
            if pm in ("hourly_latest", "each_crawl"):
                base["feishu_table"]["push_mode"] = pm

    r = raw.get("robot")
    if isinstance(r, dict):
        for sec in ("feishu", "dingtalk"):
            part = r.get(sec)
            if isinstance(part, dict):
                base["robot"][sec]["enabled"] = bool(part.get("enabled", False))
                base["robot"][sec]["webhook"] = str(part.get("webhook") or "").strip()
                base["robot"][sec]["keyword"] = str(part.get("keyword") or "").strip()

    return base


def _consume_legacy_feishu_webhook_json(path: str) -> None:
    """
    若存在 data/feishu_webhook_push.json：读入并写入 control_panel.json → robot.feishu，然后删除旧文件。
    """
    legacy_wh = os.path.join(DATA_DIR, _LEGACY_FEISHU_WH)
    if not os.path.isfile(legacy_wh):
        return

    legacy = _read_json_file(legacy_wh)
    existing = _read_json_file(path)
    if existing is None or not isinstance(existing, dict):
        full = _default_full()
    else:
        full = _normalize_full(existing)

    if legacy and isinstance(legacy, dict):
        full["robot"]["feishu"] = {
            "enabled": bool(legacy.get("enabled", False)),
            "webhook": str(legacy.get("webhook") or "").strip(),
            "keyword": str(legacy.get("keyword") or "").strip(),
        }
    full = _normalize_full(full)
    _atomic_write(path, full)
    try:
        os.remove(legacy_wh)
    except OSError:
        pass


def _load_full_from_disk() -> Dict[str, Any]:
    """须在持有 _lock 下调用。"""
    path = unified_config_path()

    # 仅兼容旧版：feishu_webhook_push.json → robot.feishu，然后删除旧文件
    if os.path.isfile(os.path.join(DATA_DIR, _LEGACY_FEISHU_WH)):
        _consume_legacy_feishu_webhook_json(path)
        raw = _read_json_file(path)
        if raw is not None and raw:
            return _normalize_full(raw)
        merged = _default_full()
        _atomic_write(path, merged)
        return merged

    raw = _read_json_file(path)
    if raw is not None and raw:
        return _normalize_full(raw)

    merged = _default_full()
    _atomic_write(path, merged)
    return merged


def _save_full(full: Dict[str, Any]) -> None:
    data = _normalize_full(full)
    _atomic_write(unified_config_path(), data)


# ---------- 抓取服务 ----------
def load_scrape_service_config() -> Dict[str, Any]:
    with _lock:
        full = _load_full_from_disk()
        c = full["crawl"]
        try:
            iv = max(5, int(c.get("interval_seconds") or 600))
        except Exception:
            iv = 600
        return {
            "interval_seconds": iv,
            "headless_poll": bool(c.get("headless_poll", True)),
            "fetch_assist_tasks": bool(c.get("fetch_assist_tasks", True)),
            "browser_executable_path": str(c.get("browser_executable_path") or "").strip(),
        }


def save_scrape_service_config(
    *,
    interval_seconds: Optional[int] = None,
    headless_poll: Optional[bool] = None,
    fetch_assist_tasks: Optional[bool] = None,
    browser_executable_path: Optional[str] = None,
) -> Dict[str, Any]:
    with _lock:
        full = _load_full_from_disk()
        c = full["crawl"]
        if interval_seconds is not None:
            c["interval_seconds"] = max(5, int(interval_seconds))
        if headless_poll is not None:
            c["headless_poll"] = bool(headless_poll)
        if fetch_assist_tasks is not None:
            c["fetch_assist_tasks"] = bool(fetch_assist_tasks)
        if browser_executable_path is not None:
            c["browser_executable_path"] = str(browser_executable_path or "").strip()
        full["crawl"] = c
        _save_full(full)
        return load_scrape_service_config()


# ---------- 飞书多维表 ----------
def load_feishu_bitable_panel_config() -> Dict[str, Any]:
    with _lock:
        full = _load_full_from_disk()
        ft = full["feishu_table"]
        pm = str(ft.get("push_mode") or "each_crawl").strip().lower()
        if pm not in ("each_crawl", "hourly_latest"):
            pm = "each_crawl"
        return {
            "enabled": bool(ft.get("enabled", False)),
            "app_token": str(ft.get("app_token") or "").strip(),
            "personal_base_token": str(ft.get("personal_base_token") or "").strip(),
            "table_id": str(ft.get("table_id") or "").strip(),
            "push_mode": pm,
        }


def save_feishu_bitable_panel_config(
    *,
    enabled: Optional[bool] = None,
    app_token: Optional[str] = None,
    personal_base_token: Optional[str] = None,
    table_id: Optional[str] = None,
    push_mode: Optional[str] = None,
) -> Dict[str, Any]:
    with _lock:
        full = _load_full_from_disk()
        ft = full["feishu_table"]
        if enabled is not None:
            ft["enabled"] = bool(enabled)
        if app_token is not None:
            ft["app_token"] = str(app_token).strip()
        if personal_base_token is not None:
            ft["personal_base_token"] = str(personal_base_token).strip()
        if table_id is not None:
            ft["table_id"] = str(table_id).strip()
        if push_mode is not None:
            pm = str(push_mode).strip().lower()
            ft["push_mode"] = pm if pm in ("each_crawl", "hourly_latest") else "each_crawl"
        full["feishu_table"] = ft
        _save_full(full)
        return load_feishu_bitable_panel_config()


def snapshot_feishu_bitable_for_fetch() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """仅当 feishu_table.enabled 为 true 且 app_token / personal_base_token / table_id 均非空时，才用于抓取后同步飞书。"""
    c = load_feishu_bitable_panel_config()
    if not c.get("enabled"):
        return None, None, None
    a = c.get("app_token") or None
    p = c.get("personal_base_token") or None
    t = c.get("table_id") or None
    if not a or not p or not t:
        return None, None, None
    return a, p, t


# ---------- 机器人推送 ----------
def load_robot_push_config() -> Dict[str, Any]:
    with _lock:
        full = _load_full_from_disk()
        r = full["robot"]
        fs = r.get("feishu") if isinstance(r.get("feishu"), dict) else {}
        dt = r.get("dingtalk") if isinstance(r.get("dingtalk"), dict) else {}
        return {
            "feishu": {
                "enabled": bool(fs.get("enabled", False)),
                "webhook": str(fs.get("webhook") or "").strip(),
                "keyword": str(fs.get("keyword") or "").strip(),
            },
            "dingtalk": {
                "enabled": bool(dt.get("enabled", False)),
                "webhook": str(dt.get("webhook") or "").strip(),
                "keyword": str(dt.get("keyword") or "").strip(),
            },
        }


def save_robot_push_section(
    section: str,
    *,
    enabled: Optional[bool] = None,
    webhook: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    if section not in ("feishu", "dingtalk"):
        raise ValueError("section must be feishu or dingtalk")
    with _lock:
        full = _load_full_from_disk()
        part = dict(full["robot"][section])
        if enabled is not None:
            part["enabled"] = bool(enabled)
        if webhook is not None:
            part["webhook"] = str(webhook).strip()
        if keyword is not None:
            part["keyword"] = str(keyword).strip()
        full["robot"][section] = part
        _save_full(full)
        return load_robot_push_config()


def ensure_all_control_defaults() -> None:
    """应用启动：保证 data/control_panel.json 存在；若仅有旧 feishu_webhook_push.json 则并入后删除。"""
    _ensure_data_dir()
    with _lock:
        _load_full_from_disk()


def scrape_config_path() -> str:
    return unified_config_path()


def feishu_bitable_config_path() -> str:
    return unified_config_path()


def robot_push_config_path() -> str:
    return unified_config_path()
