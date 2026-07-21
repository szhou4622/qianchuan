"""
Webhook 相关工具（飞书 / 钉钉机器人推送）。
"""

from __future__ import annotations

from .dingtalk_bot import (
    DingtalkBotError,
    DingtalkBotHook,
    DingtalkWebhook,
    build_dingtalk_notification_title,
    normalize_dingtalk_webhook_url,
)
from .feishu_bot import (
    DEFAULT_HOOK_BASE_CN,
    FeishuBotError,
    FeishuBotHook,
    FeishuWebhook,
    WEBHOOK_BASE_FEISHU_CN,
    WEBHOOK_BASE_LARK_INTL,
    WebhookSendResult,
    build_interactive_markdown_card,
    build_markdown_table,
    build_markdown_table_card,
    normalize_webhook_url,
    send_text,
    try_send_text,
)

__all__ = [
    "DingtalkBotError",
    "DingtalkBotHook",
    "DingtalkWebhook",
    "build_dingtalk_notification_title",
    "normalize_dingtalk_webhook_url",
    "DEFAULT_HOOK_BASE_CN",
    "WEBHOOK_BASE_FEISHU_CN",
    "WEBHOOK_BASE_LARK_INTL",
    "FeishuBotError",
    "FeishuBotHook",
    "FeishuWebhook",
    "WebhookSendResult",
    "build_interactive_markdown_card",
    "build_markdown_table",
    "build_markdown_table_card",
    "normalize_webhook_url",
    "send_text",
    "try_send_text",
]
