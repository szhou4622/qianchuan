# -*- coding: utf-8 -*-
"""
飞书 / 钉钉 Webhook 整点推送：统一启动入口（供 gui_app 等调用）。
"""
from __future__ import annotations

import threading
from typing import Tuple

# 飞书/钉钉机器人 HTTP 发送：`FeishuWebhook`/`DingtalkWebhook` 的 retries 为失败后额外重试次数，
# 总尝试次数 = 1 + retries。此处为 2 => 最多 3 次请求。
WEBHOOK_ROBOT_HTTP_RETRIES = 2

from services.dingtalk_webhook_push import start_dingtalk_webhook_push_background_thread
from services.feishu_webhook_push import start_feishu_webhook_push_background_thread


def start_webhook_push_background_threads() -> Tuple[threading.Thread | None, threading.Thread | None]:
    """
    启动飞书、钉钉整点推送后台线程（各一条 daemon 线程）。
    返回 (飞书线程, 钉钉线程)，便于调试；一般可忽略返回值。
    """
    t_feishu = start_feishu_webhook_push_background_thread()
    t_dingtalk = start_dingtalk_webhook_push_background_thread()
    return t_feishu, t_dingtalk


def stop_webhook_push_background_threads() -> None:
    from services.dingtalk_webhook_push import stop_dingtalk_webhook_push_background_thread
    from services.feishu_webhook_push import stop_feishu_webhook_push_background_thread

    stop_feishu_webhook_push_background_thread()
    stop_dingtalk_webhook_push_background_thread()
