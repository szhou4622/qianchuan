# -*- coding: utf-8 -*-
"""
飞书群机器人 Webhook：推送大屏表格前 15 条（与 DashboardApi.get_table_data 一致）。
配置位于 data/control_panel.json → robot.feishu；每次推送前重新读取。

调度说明：每整点推送一次（整点后 45 秒内触发，同一小时不重复）。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from hook.feishu_bot import FeishuWebhook
from services.control_panel_config import load_robot_push_config, save_robot_push_section
from utils.common import (
    WEBHOOK_PUSH_TABLE_HEADERS,
    build_webhook_push_title,
    material_rows_for_webhook_push,
)

_last_fired_hour_key: Optional[Tuple[int, int, int, int]] = None


def load_feishu_webhook_push_config() -> Dict[str, Any]:
    """从 control_panel.json 读取 robot.feishu。"""
    full = load_robot_push_config()
    return dict(full["feishu"])


def save_feishu_webhook_push_config(
    enabled: Optional[bool] = None,
    webhook: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """合并写入 control_panel.json → robot.feishu。"""
    save_robot_push_section("feishu", enabled=enabled, webhook=webhook, keyword=keyword)
    return load_feishu_webhook_push_config()


def run_feishu_webhook_push_once(dashboard_api: Any, *, ignore_enabled: bool = False) -> Dict[str, Any]:
    """
    读取最新配置并推送；用于整点任务与手动测试。
    dashboard_api: api.dashboard.DashboardApi 实例
    ignore_enabled: 为 True 时仅校验 Webhook（用于「测试推送」按钮）
    """
    cfg = load_feishu_webhook_push_config()
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

    title = build_webhook_push_title(str(cfg.get("keyword") or ""))
    intro = f"*数据时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*"
    bot = FeishuWebhook(hook, retries=WEBHOOK_ROBOT_HTTP_RETRIES)
    send = bot.try_send_markdown_table(
        title,
        WEBHOOK_PUSH_TABLE_HEADERS,
        rows,
        subtitle="本地库 · 前15条",
        intro_markdown=intro,
        max_rows=20,
    )
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


def _hourly_push_loop() -> None:
    global _last_fired_hour_key
    from api.dashboard import DashboardApi

    dash = DashboardApi()
    time.sleep(5)
    while True:
        try:
            now = datetime.now()
            if now.minute == 0 and now.second < 45:
                key = _hour_key_now()
                if key != _last_fired_hour_key:
                    _last_fired_hour_key = key
                    run_feishu_webhook_push_once(dash)
        except Exception as e:
            print(f"[飞书整点推送] 异常: {e}")
        time.sleep(10)


def start_feishu_webhook_push_background_thread() -> threading.Thread | None:
    t = threading.Thread(target=_hourly_push_loop, name="feishu-webhook-hourly", daemon=True)
    t.start()
    print("[飞书整点推送] 后台线程已启动（每整点推送，配置：data/control_panel.json）")
    return t
