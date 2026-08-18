# -*- coding: utf-8 -*-
"""
钉钉群机器人 Webhook：推送大屏前 15 条（与 DashboardApi.get_table_data 一致）。
配置位于 data/control_panel.json → robot.dingtalk；每次推送前重新读取。

调度说明：每整点推送一次（整点后 45 秒内触发，同一小时不重复）。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from hook.dingtalk_bot import DingtalkWebhook, build_dingtalk_notification_title, format_dingtalk_push_body
from services.control_panel_config import load_robot_push_config, save_robot_push_section
from utils.common import WEBHOOK_PUSH_BASE_TITLE, WEBHOOK_PUSH_TABLE_HEADERS, material_rows_for_webhook_push

_last_fired_hour_key: Optional[Tuple[int, int, int, int]] = None


def load_dingtalk_webhook_push_config() -> Dict[str, Any]:
    """从 control_panel.json 读取 robot.dingtalk。"""
    full = load_robot_push_config()
    return dict(full["dingtalk"])


def save_dingtalk_webhook_push_config(
    enabled: Optional[bool] = None,
    webhook: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """合并写入 control_panel.json → robot.dingtalk。"""
    save_robot_push_section("dingtalk", enabled=enabled, webhook=webhook, keyword=keyword)
    return load_dingtalk_webhook_push_config()


def run_dingtalk_webhook_push_once(dashboard_api: Any, *, ignore_enabled: bool = False) -> Dict[str, Any]:
    """
    读取最新配置并推送；用于整点任务与手动测试。
    dashboard_api: api.dashboard.DashboardApi 实例
    ignore_enabled: 为 True 时仅校验 Webhook（用于「测试推送」按钮）
    """
    cfg = load_dingtalk_webhook_push_config()
    if not ignore_enabled and not cfg.get("enabled"):
        return {"success": False, "skipped": True, "message": "未启用整点推送"}
    hook = str(cfg.get("webhook") or "").strip()
    if not hook:
        return {"success": False, "skipped": True, "message": "Webhook 为空"}

    result = dashboard_api.get_table_data(
        "1h", "costDiff", "desc", 1, 15
    )
    if not result.get("success"):
        return {
            "success": False,
            "message": result.get("message") or "get_table_data 失败",
        }
    data = result.get("data") or []
    rows = material_rows_for_webhook_push(data)
    if ignore_enabled and not rows:
        return {
            "success": False,
            "message": "查询无数据：本地库在最近 1 小时（与大屏「时段流速」一致）内没有可推送的素材，请先完成抓取入库。",
        }

    from services.webhook_push_runtime import WEBHOOK_ROBOT_HTTP_RETRIES

    title = build_dingtalk_notification_title(str(cfg.get("keyword") or ""))
    data_time = f"数据时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    body = format_dingtalk_push_body(
        WEBHOOK_PUSH_TABLE_HEADERS,
        rows,
        headline=WEBHOOK_PUSH_BASE_TITLE,
        data_time_line=data_time,
        max_rows=20,
    )
    bot = DingtalkWebhook(hook, retries=WEBHOOK_ROBOT_HTTP_RETRIES)
    send = bot.try_send_markdown(title, body)
    if send.ok:
        return {"success": True, "message": "已发送", "row_count": len(rows)}
    return {
        "success": False,
        "message": send.error or "发送失败",
        "code": send.code,
    }


def _hour_key_now() -> Tuple[int, int, int, int]:
    t = datetime.now()
    return (t.year, t.month, t.day, t.hour)


_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _hourly_push_loop() -> None:
    global _last_fired_hour_key
    from api.dashboard import DashboardApi

    dash = DashboardApi()
    if _STOP.wait(5):
        return
    while not _STOP.is_set():
        try:
            now = datetime.now()
            if now.minute == 0 and now.second < 45:
                key = _hour_key_now()
                if key != _last_fired_hour_key:
                    _last_fired_hour_key = key
                    run_dingtalk_webhook_push_once(dash)
        except Exception as e:
            print(f"[钉钉整点推送] 异常: {e}")
        _STOP.wait(10)


def start_dingtalk_webhook_push_background_thread() -> threading.Thread | None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(target=_hourly_push_loop, name="dingtalk-webhook-hourly", daemon=True)
    _THREAD.start()
    print("[钉钉整点推送] 后台线程已启动（每整点推送，配置：data/control_panel.json）")
    return _THREAD


def stop_dingtalk_webhook_push_background_thread(timeout: float = 3.0) -> None:
    global _THREAD
    _STOP.set()
    thread = _THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(0.1, float(timeout)))
    if thread is None or not thread.is_alive():
        _THREAD = None
