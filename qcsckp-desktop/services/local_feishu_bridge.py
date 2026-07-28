# -*- coding: utf-8 -*-
"""本地飞书长连接、绑定码与追投确认任务。

默认模式下，飞书 App Secret 只在当前 Windows 用户下用 DPAPI 加密保存。
飞书卡片点击通过长连接进入本机 SQLite 队列，不经过中心服务器。
"""
from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from config import DATA_DIR, DB_FILE
from utils.log import logger
from utils.sqlite_store import init_sqlite_schema


PROFILE_FILE = os.path.join(DATA_DIR, "feishu_local_profiles.json")
TERMINAL_STATUSES = {"succeeded", "failed", "rejected", "expired", "cancelled"}
ACTIVE_STATUSES = {"pending", "approved_queued", "claimed", "executing"}
VALID_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
PROFILE_LOCK = threading.RLock()
TASK_LOCK = threading.RLock()


def _now() -> datetime:
    return datetime.now()


def _dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return default


def _account_key(username: Any) -> str:
    return str(username or "").strip().casefold()


def _atomic_json_save(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _load_profiles() -> Dict[str, Any]:
    with PROFILE_LOCK:
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                return data
        except Exception:
            pass
        return {"version": 1, "profiles": {}}


def _save_profiles(data: Dict[str, Any]) -> None:
    with PROFILE_LOCK:
        _atomic_json_save(PROFILE_FILE, data)


def _protect_secret(secret: str) -> str:
    if os.name != "nt":
        raise RuntimeError("本地飞书凭据加密当前仅支持 Windows")
    try:
        import win32crypt

        protected = win32crypt.CryptProtectData(
            secret.encode("utf-8"),
            "qcsckp-feishu-app-secret",
            None,
            None,
            None,
            0,
        )
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    except Exception as exc:
        raise RuntimeError(f"无法使用Windows保护飞书凭据：{exc}") from exc


def _unprotect_secret(ciphertext: str) -> str:
    text = str(ciphertext or "")
    if not text.startswith("dpapi:"):
        return ""
    if os.name != "nt":
        return ""
    try:
        import win32crypt

        raw = base64.b64decode(text[6:])
        return win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return ""


def _profile_for(username: str, *, include_secret: bool = False) -> Dict[str, Any]:
    key = _account_key(username)
    raw = (_load_profiles().get("profiles") or {}).get(key)
    profile = dict(raw) if isinstance(raw, dict) else {}
    profile.setdefault("enabled", False)
    profile.setdefault("backend", "local_ws")
    profile.setdefault("app_id", "")
    profile.setdefault("authorized_open_id", "")
    profile.setdefault("send_personal", True)
    profile.setdefault("send_groups", True)
    profile.setdefault("groups", [])
    profile["app_secret_saved"] = bool(profile.get("app_secret_protected"))
    if include_secret:
        profile["app_secret"] = _unprotect_secret(
            str(profile.get("app_secret_protected") or "")
        )
    profile.pop("app_secret_protected", None)
    return profile


def _update_profile(username: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    key = _account_key(username)
    if not key:
        raise RuntimeError("请先登录工具账号")
    with PROFILE_LOCK:
        data = _load_profiles()
        profiles = data.setdefault("profiles", {})
        current = profiles.get(key)
        profile = dict(current) if isinstance(current, dict) else {}
        profile.update(changes)
        profile["updated_at"] = _dt(_now())
        profiles[key] = profile
        _save_profiles(data)
    return _profile_for(username)


def _db() -> sqlite3.Connection:
    init_sqlite_schema(database=DB_FILE)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _task_row(task_uid: str, account_username: str = "") -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        sql = "SELECT * FROM local_retarget_task WHERE task_uid=?"
        params: List[Any] = [str(task_uid)]
        if account_username:
            sql += " AND account_username=?"
            params.append(_account_key(account_username))
        sql += " LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def has_local_task(task_uid: str) -> bool:
    return _task_row(task_uid) is not None


def _task_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = _loads(row.get("payload_json"), {})
    if not isinstance(payload, dict):
        payload = {}
    payload.update(
        {
            "task_uid": str(row.get("task_uid") or ""),
            "status": str(row.get("status") or "pending"),
            "action_nonce": str(row.get("action_nonce") or ""),
            "expires_at": str(row.get("expires_at") or ""),
            "clicker_open_id": str(row.get("approved_by") or ""),
            "claim_token": str(row.get("claim_token") or ""),
            "result_message": str(row.get("result_message") or ""),
            "result_detail": str(row.get("result_detail") or ""),
            "regulate_task_id": str(row.get("regulate_task_id") or ""),
            "result": _loads(row.get("result_json"), {}),
        }
    )
    return payload


def _normalize_materials(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_materials = payload.get("materials")
    if not isinstance(raw_materials, list) or not raw_materials:
        raw_materials = [
            {
                "material_id": payload.get("material_id"),
                "material_name": payload.get("material_name"),
                "product_id": payload.get("product_id"),
                "product_name": payload.get("product_name"),
            }
        ]
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_materials:
        if not isinstance(raw, dict):
            continue
        material_id = str(raw.get("material_id") or "").strip()[:128]
        if not material_id or material_id in seen:
            continue
        seen.add(material_id)
        product_ids: List[str] = []
        for value in raw.get("product_ids") or []:
            product_id = str(value or "").strip()[:128]
            if product_id and product_id not in product_ids:
                product_ids.append(product_id)
        primary_product = str(raw.get("product_id") or "").strip()[:128]
        if primary_product and primary_product not in product_ids:
            product_ids.insert(0, primary_product)
        result.append(
            {
                "material_id": material_id,
                "material_name": str(raw.get("material_name") or "").strip()[:512],
                "product_id": primary_product,
                "product_name": str(raw.get("product_name") or "").strip()[:512],
                "product_ids": product_ids[:20],
            }
        )
        if len(result) >= 20:
            break
    return result


class FeishuApiError(RuntimeError):
    pass


def _connection_error_status(error: Any) -> str:
    text = str(error or "").lower()
    if any(
        marker in text
        for marker in (
            "permission",
            "forbidden",
            "scope",
            "99991672",
            "权限不足",
            "无权限",
        )
    ):
        return "permission_missing"
    if any(
        marker in text
        for marker in (
            "not published",
            "unpublished",
            "app is unavailable",
            "应用未发布",
            "未发布",
        )
    ):
        return "app_unpublished"
    return "error"


def _http_json(
    method: str,
    url: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        message = str(parsed.get("msg") or parsed.get("message") or raw or exc)
        raise FeishuApiError(message) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FeishuApiError(f"无法连接飞书开放平台：{exc}") from exc
    except ValueError as exc:
        raise FeishuApiError("飞书返回内容不是有效JSON") from exc
    if not isinstance(parsed, dict):
        raise FeishuApiError("飞书返回格式异常")
    if int(parsed.get("code") or 0) != 0:
        raise FeishuApiError(str(parsed.get("msg") or "飞书接口调用失败"))
    return parsed


def _tenant_token(app_id: str, app_secret: str) -> Tuple[str, int]:
    response = _http_json(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    token = str(response.get("tenant_access_token") or "")
    if not token:
        raise FeishuApiError("飞书没有返回应用访问凭证，请检查App ID和App Secret")
    return token, int(response.get("expire") or 7200)


def _retarget_method_summary(retargeting: Dict[str, Any]) -> str:
    if str(retargeting.get("method") or "") == "volume":
        volume = retargeting.get("volume") if isinstance(retargeting.get("volume"), dict) else {}
        return (
            f"放量追投｜总预算 {volume.get('total_budget_yuan', '未填写')} 元"
            f"｜时长 {volume.get('duration_hours', '未填写')} 小时"
        )
    cost = (
        retargeting.get("cost_control")
        if isinstance(retargeting.get("cost_control"), dict)
        else {}
    )
    if str(cost.get("optimization_goal") or "") == "live_room":
        live = cost.get("live_room") if isinstance(cost.get("live_room"), dict) else {}
        return (
            f"控成本追投｜日预算 {live.get('daily_budget_yuan', '未填写')} 元"
            f"｜出价 {live.get('bid_per_conversion_yuan', '未填写')} 元"
        )
    roi = cost.get("net_roi") if isinstance(cost.get("net_roi"), dict) else {}
    return (
        f"控成本追投｜日预算 {roi.get('daily_budget_yuan', '未填写')} 元"
        f"｜净成交ROI {roi.get('net_roi_target', '未填写')}"
    )


def _trigger_summary(trigger: Dict[str, Any], *, expanded: bool = False) -> str:
    evaluation = trigger.get("evaluation") if isinstance(trigger.get("evaluation"), dict) else {}
    lines: List[str] = []
    for group_index, group in enumerate(evaluation.get("groups") or []):
        if not isinstance(group, dict):
            continue
        for condition in group.get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            if not expanded and not condition.get("passed"):
                continue
            op = {
                "gt": ">",
                "gte": "≥",
                "lt": "<",
                "lte": "≤",
                "eq": "=",
            }.get(str(condition.get("op") or ""), str(condition.get("op") or ""))
            prefix = f"组{group_index + 1}｜" if expanded else ""
            suffix = f"（{'命中' if condition.get('passed') else '未命中'}）" if expanded else ""
            lines.append(
                f"{prefix}{condition.get('metric', '指标')}："
                f"{condition.get('actual', '未记录')} {op} "
                f"{condition.get('threshold', '未记录')}{suffix}"
            )
            if not expanded and len(lines) >= 5:
                break
        if not expanded and len(lines) >= 5:
            break
    return "\n".join(lines) if expanded else ("；".join(lines) if lines else "规则条件已命中")


def build_task_card(task: Dict[str, Any], *, expanded: bool = False) -> Dict[str, Any]:
    status = str(task.get("status") or "pending")
    status_text = {
        "pending": "等待确认",
        "approved_queued": "已批准，等待工具执行",
        "claimed": "工具已领取",
        "executing": "正在追投",
        "succeeded": "追投成功",
        "failed": "追投失败",
        "rejected": "已暂不追投",
        "expired": "已过期",
        "cancelled": "已取消",
    }.get(status, status)
    template = "green" if status == "succeeded" else (
        "red" if status in {"failed", "expired", "rejected"} else "blue"
    )
    scene_text = "推商品" if str(task.get("promotion_scene") or "live") == "product" else "推直播"
    plan_system_text = {
        "global": "传统全域",
        "chengfang": "千川乘方",
        "unknown": "待确认",
    }.get(str(task.get("plan_system") or "unknown"), "待确认")
    level_text = "商品级" if str(task.get("trigger_level") or "material") == "product" else "素材级"
    materials = _normalize_materials(task)
    material_lines: List[str] = []
    for index, material in enumerate(materials):
        name = str(material.get("material_name") or "未命名素材")[:160]
        line = f"{index + 1}. {name}\n   素材ID：`{material['material_id']}`"
        product_name = str(material.get("product_name") or "")
        product_id = str(material.get("product_id") or "")
        if product_name or product_id:
            line += f"\n   关联商品：{product_name or '未命名商品'}"
            if product_id:
                line += f"（`{product_id}`）"
        material_lines.append(line)
    trigger = task.get("trigger_snapshot") if isinstance(task.get("trigger_snapshot"), dict) else {}
    retargeting = task.get("retargeting") if isinstance(task.get("retargeting"), dict) else {}
    product_line = ""
    if task.get("product_id"):
        product_line = (
            f"\n**商品名称：** {task.get('product_name') or '未命名商品'}"
            f"\n**商品ID：** `{task.get('product_id')}`"
        )
    elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**千川账户：** {task.get('account_name') or '未命名账户'}"
                f"\n**账户ID：** `{task.get('aavid') or ''}`"
                f"\n**计划名称：** {task.get('plan_name') or '未命名计划'}"
                f"\n**计划ID：** `{task.get('ad_id') or ''}`"
                f"\n**推广场景：** {scene_text}"
                f"\n**计划体系：** {plan_system_text}"
                f"\n**触发层级：** {level_text}"
                f"{product_line}"
                f"\n\n**本卡追投素材（{len(material_lines)}条）：**"
                f"\n{chr(10).join(material_lines)}"
                f"\n\n**策略：** {task.get('strategy_name') or trigger.get('strategy_title') or '追投策略命中'}"
            ),
        },
        {
            "tag": "markdown",
            "content": (
                f"**命中原因：** {_trigger_summary(trigger)}"
                f"\n**追投参数：** {_retarget_method_summary(retargeting)}"
                f"\n**有效期至：** {task.get('expires_at') or ''}"
                f"\n**当前状态：** {status_text}"
            ),
        },
    ]
    if task.get("result_message"):
        elements.append(
            {
                "tag": "markdown",
                "content": f"**执行结果：** {str(task.get('result_message'))[:500]}",
            }
        )
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    task_ids = [
        str(value)
        for value in result.get("regulate_task_ids") or []
        if str(value or "").strip()
    ]
    if not task_ids and task.get("regulate_task_id"):
        task_ids = [str(task.get("regulate_task_id"))]
    if task_ids:
        elements.append(
            {
                "tag": "markdown",
                "content": "**千川调控任务ID：**\n"
                + "\n".join(f"{i + 1}. `{value}`" for i, value in enumerate(task_ids)),
            }
        )
    if expanded:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"**完整触发条件**\n{_trigger_summary(trigger, expanded=True)[:2500]}",
                },
                {
                    "tag": "markdown",
                    "content": (
                        "**追投参数快照**\n```json\n"
                        + json.dumps(retargeting, ensure_ascii=False, indent=2)[:2500]
                        + "\n```"
                    ),
                },
            ]
        )
    if status == "pending":
        base = {"task_uid": str(task.get("task_uid") or ""), "nonce": str(task.get("action_nonce") or "")}
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认追投"},
                        "type": "primary",
                        "value": {**base, "action": "approve"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "暂不追投"},
                        "type": "danger",
                        "value": {**base, "action": "reject"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看详情"},
                        "value": {**base, "action": "view"},
                    },
                ],
            }
        )
    return {
        "config": {
            "wide_screen_mode": True,
            "enable_forward": False,
            "update_multi": True,
        },
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": f"千川追投提醒 · {scene_text} · {plan_system_text} · {status_text}",
            },
        },
        "elements": elements,
    }


class LocalFeishuBridge:
    def __init__(self, account_username: str):
        self.account_username = _account_key(account_username)
        self._channel = None
        self._thread: Optional[threading.Thread] = None
        self._status = "not_configured"
        self._last_error = ""
        self._connected_at = ""
        self._token = ""
        self._token_expires_at = 0.0
        self._binding_codes: Dict[str, Dict[str, Any]] = {}
        self._connection_test: Dict[str, Any] = {}
        self._last_card_action_at = ""
        self._seen_messages: List[str] = []
        self._lock = threading.RLock()

    def profile(self, *, include_secret: bool = False) -> Dict[str, Any]:
        return _profile_for(self.account_username, include_secret=include_secret)

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires_at - 120:
                return self._token
        profile = self.profile(include_secret=True)
        app_id = str(profile.get("app_id") or "").strip()
        app_secret = str(profile.get("app_secret") or "")
        if not app_id or not app_secret:
            raise FeishuApiError("请先保存飞书App ID和App Secret")
        token, expires = _tenant_token(app_id, app_secret)
        with self._lock:
            self._token = token
            self._token_expires_at = time.time() + max(300, expires)
        return token

    def test_credentials(self) -> Dict[str, Any]:
        try:
            self._access_token()
            return {"success": True, "message": "飞书应用凭据有效"}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = "https://open.feishu.cn/open-apis" + path
        if query:
            url += "?" + urlencode(query)
        return _http_json(
            method,
            url,
            payload=payload,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )

    def send_text(self, chat_id: str, text: str) -> None:
        if not chat_id:
            return
        self._request(
            "POST",
            "/im/v1/messages",
            query={"receive_id_type": "chat_id"},
            payload={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    def send_private_text(self, open_id: str, text: str) -> None:
        if not open_id:
            return
        self._request(
            "POST",
            "/im/v1/messages",
            query={"receive_id_type": "open_id"},
            payload={
                "receive_id": open_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    def _send_card(self, receive_type: str, receive_id: str, card: Dict[str, Any]) -> str:
        response = self._request(
            "POST",
            "/im/v1/messages",
            query={"receive_id_type": receive_type},
            payload={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        return str((response.get("data") or {}).get("message_id") or "")

    def send_task_cards(self, task: Dict[str, Any]) -> List[Dict[str, str]]:
        profile = self.profile()
        targets: List[Tuple[str, str]] = []
        authorized = str(profile.get("authorized_open_id") or "").strip()
        if profile.get("send_personal", True) and authorized:
            targets.append(("open_id", authorized))
        if profile.get("send_groups", True):
            for group in profile.get("groups") or []:
                if isinstance(group, dict) and str(group.get("chat_id") or "").strip():
                    targets.append(("chat_id", str(group["chat_id"]).strip()))
        if not targets:
            raise FeishuApiError("尚未绑定个人或接收群，请先完成机器人绑定")
        card = build_task_card(task)
        sent: List[Dict[str, str]] = []
        errors: List[str] = []
        for receive_type, receive_id in targets:
            try:
                message_id = self._send_card(receive_type, receive_id, card)
                if message_id:
                    sent.append(
                        {
                            "receive_type": receive_type,
                            "receive_id": receive_id,
                            "message_id": message_id,
                        }
                    )
            except Exception as exc:
                errors.append(f"{receive_type}:{receive_id} {exc}")
        if not sent:
            raise FeishuApiError("飞书卡片发送失败：" + ("；".join(errors) or "未返回消息ID"))
        return sent

    def update_task_cards(self, task_uid: str, *, expanded: bool = False) -> None:
        row = _task_row(task_uid, self.account_username)
        if not row:
            return
        task = _task_payload(row)
        card = build_task_card(task, expanded=expanded)
        content = json.dumps(card, ensure_ascii=False)
        messages = _loads(row.get("card_messages_json"), [])
        for message in messages if isinstance(messages, list) else []:
            message_id = str((message or {}).get("message_id") or "")
            if not message_id:
                continue
            try:
                self._request(
                    "PATCH",
                    "/im/v1/messages/" + quote(message_id, safe=""),
                    payload={"content": content},
                )
            except Exception as exc:
                logger.warning("[飞书长连接] 更新卡片失败 task=%s: %s", task_uid, exc)

    def send_test_card(self) -> Dict[str, Any]:
        try:
            profile = self.profile()
            open_id = str(profile.get("authorized_open_id") or "")
            if not open_id:
                return {"success": False, "message": "请先完成个人绑定"}
            nonce = secrets.token_hex(24)
            with self._lock:
                self._connection_test = {
                    "nonce": nonce,
                    "expires_at": time.time() + 600,
                }
            card = {
                "config": {"wide_screen_mode": True, "enable_forward": False},
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": "飞书本地长连接测试"},
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "**消息发送成功**\n请点击下方按钮，继续验证卡片操作能否回到本机工具。",
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "type": "primary",
                                "text": {"tag": "plain_text", "content": "测试按钮"},
                                "value": {
                                    "action": "connection_test",
                                    "nonce": nonce,
                                },
                            }
                        ],
                    },
                ],
            }
            message_id = self._send_card("open_id", open_id, card)
            return {
                "success": bool(message_id),
                "message": (
                    "测试卡片已发送，请在飞书点击“测试按钮”"
                    if message_id
                    else "飞书没有返回消息ID"
                ),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def _consume_connection_test(self, nonce: str, open_id: str) -> Dict[str, Any]:
        profile = self.profile()
        if open_id != str(profile.get("authorized_open_id") or ""):
            return {"success": False, "message": "只有已绑定授权人可以测试按钮"}
        with self._lock:
            item = dict(self._connection_test)
            if (
                not item
                or float(item.get("expires_at") or 0) < time.time()
                or not secrets.compare_digest(
                    str(item.get("nonce") or ""), str(nonce or "")
                )
            ):
                return {"success": False, "message": "测试按钮已失效，请重新发送测试卡片"}
            self._connection_test.clear()
            self._last_card_action_at = _dt(_now())
        return {"success": True}

    def _finish_connection_test_card(self, message_id: str) -> None:
        if not message_id:
            return
        card = {
            "config": {"wide_screen_mode": True, "enable_forward": False},
            "header": {
                "template": "green",
                "title": {"tag": "plain_text", "content": "飞书本地长连接测试成功"},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "**卡片按钮已回到本机工具**\n"
                        "本地长连接、授权人校验和卡片更新均正常，不会触发千川操作。"
                    ),
                }
            ],
        }
        try:
            self._request(
                "PATCH",
                "/im/v1/messages/" + quote(message_id, safe=""),
                payload={"content": json.dumps(card, ensure_ascii=False)},
            )
        except Exception as exc:
            logger.warning("[飞书长连接] 更新测试卡片失败: %s", exc)

    def issue_binding_code(self, purpose: str) -> Dict[str, Any]:
        purpose = str(purpose or "").strip()
        if purpose not in {"personal", "group"}:
            return {"success": False, "message": "绑定类型无效"}
        if purpose == "group" and not self.profile().get("authorized_open_id"):
            return {"success": False, "message": "请先完成个人绑定"}
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self._binding_codes[purpose] = {
                "code": code,
                "expires_at": time.time() + 600,
            }
        command = f"绑定 {code}" if purpose == "personal" else f"绑定群 {code}"
        return {
            "success": True,
            "purpose": purpose,
            "code": code,
            "command": command,
            "expires_at": _dt(_now() + timedelta(minutes=10)),
        }

    def _consume_binding_code(self, purpose: str, code: str) -> bool:
        with self._lock:
            item = self._binding_codes.get(purpose)
            if not item:
                return False
            if float(item.get("expires_at") or 0) < time.time():
                self._binding_codes.pop(purpose, None)
                return False
            if not secrets.compare_digest(str(item.get("code") or ""), str(code or "")):
                return False
            self._binding_codes.pop(purpose, None)
            return True

    def _remember_message(self, message_id: str) -> bool:
        if not message_id:
            return True
        with self._lock:
            if message_id in self._seen_messages:
                return False
            self._seen_messages.append(message_id)
            if len(self._seen_messages) > 500:
                self._seen_messages = self._seen_messages[-250:]
        return True

    def _on_message(self, message: Any) -> None:
        message_id = str(getattr(message, "message_id", "") or "")
        if not self._remember_message(message_id):
            return
        text = str(getattr(message, "content_text", "") or "").strip()
        sender = getattr(message, "sender", None)
        open_id = str(getattr(sender, "open_id", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        chat_type = str(getattr(message, "chat_type", "") or "")
        personal_match = re.search(r"(?:^|\s)绑定\s+(\d{6})(?:\s|$)", text)
        group_match = re.search(r"绑定群\s+(\d{6})(?:\s|$)", text)
        try:
            if group_match:
                if chat_type not in {"group", "topic"}:
                    self.send_text(chat_id, "请在需要接收提醒的群里@机器人发送群绑定命令。")
                    return
                profile = self.profile()
                if open_id != str(profile.get("authorized_open_id") or ""):
                    self.send_private_text(open_id, "只有已经绑定的授权人可以绑定接收群。")
                    return
                if not self._consume_binding_code("group", group_match.group(1)):
                    self.send_private_text(open_id, "群绑定码无效或已过期，请在工具里重新生成。")
                    return
                groups = [
                    item
                    for item in profile.get("groups") or []
                    if isinstance(item, dict) and str(item.get("chat_id") or "") != chat_id
                ]
                groups.append({"chat_id": chat_id, "bound_at": _dt(_now())})
                _update_profile(self.account_username, {"groups": groups})
                self.send_text(chat_id, "当前群已绑定，后续可以接收千川追投确认卡片。")
                return
            if personal_match:
                if chat_type != "p2p":
                    self.send_text(chat_id, "个人绑定码请私聊机器人发送。")
                    return
                if not self._consume_binding_code("personal", personal_match.group(1)):
                    self.send_private_text(open_id, "个人绑定码无效或已过期，请在工具里重新生成。")
                    return
                profile = self.profile()
                existing = str(profile.get("authorized_open_id") or "")
                if existing and existing != open_id:
                    self.send_private_text(open_id, "该工具账号已经绑定其他授权人。")
                    return
                _update_profile(
                    self.account_username,
                    {
                        "authorized_open_id": open_id,
                        "personal_chat_id": chat_id,
                        "authorized_bound_at": _dt(_now()),
                    },
                )
                self.send_text(chat_id, "个人绑定成功。你是该工具账号唯一可以确认追投的人。")
        except Exception as exc:
            logger.warning("[飞书长连接] 处理绑定消息失败: %s", exc)

    async def _on_card_action(self, event: Any) -> None:
        action = getattr(event, "action", None)
        operator = getattr(event, "operator", None)
        value = getattr(action, "value", None)
        if not isinstance(value, dict):
            value = {}
        open_id = str(getattr(operator, "open_id", "") or "")
        action_name = str(value.get("action") or "")
        if action_name == "connection_test":
            test_result = self._consume_connection_test(
                str(value.get("nonce") or ""), open_id
            )
            if test_result.get("success"):
                threading.Thread(
                    target=self._finish_connection_test_card,
                    args=(str(getattr(event, "message_id", "") or ""),),
                    daemon=True,
                    name="feishu-connection-test-update",
                ).start()
            elif open_id:
                try:
                    self.send_private_text(
                        open_id, str(test_result.get("message") or "测试按钮未生效")
                    )
                except Exception:
                    pass
            return
        result = handle_local_card_action(
            self.account_username,
            task_uid=str(value.get("task_uid") or ""),
            nonce=str(value.get("nonce") or ""),
            action=str(value.get("action") or ""),
            operator_open_id=open_id,
        )
        if result.get("update"):
            threading.Thread(
                target=self.update_task_cards,
                args=(str(value.get("task_uid") or ""),),
                kwargs={"expanded": bool(result.get("expanded"))},
                daemon=True,
                name="feishu-card-update",
            ).start()
        if not result.get("success") and open_id:
            try:
                self.send_private_text(open_id, str(result.get("message") or "本次操作未生效"))
            except Exception:
                pass

    def start(self) -> None:
        profile = self.profile(include_secret=True)
        if not profile.get("enabled") or not profile.get("app_id") or not profile.get("app_secret"):
            with self._lock:
                self._status = "not_configured"
            return
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            self._status = "connecting"
            self._last_error = ""

        def _entry() -> None:
            try:
                from lark_oapi import LogLevel
                from lark_oapi.channel import Events, FeishuChannel

                channel = FeishuChannel(
                    app_id=str(profile["app_id"]),
                    app_secret=str(profile["app_secret"]),
                    transport="ws",
                    # INFO 会输出带短期连接票据的 WS URL，不允许进入用户日志。
                    log_level=LogLevel.CRITICAL,
                )
                channel.on(Events.MESSAGE, self._on_message)
                channel.on(Events.CARD_ACTION, self._on_card_action)
                channel.on(
                    Events.RECONNECTING,
                    lambda *_: self._set_connection_state("reconnecting"),
                )
                channel.on(
                    Events.RECONNECTED,
                    lambda *_: self._set_connection_state("connected"),
                )
                channel.on(
                    Events.ERROR,
                    lambda error: self._set_connection_state("error", str(error)),
                )
                with self._lock:
                    self._channel = channel
                channel.start()
            except Exception as exc:
                self._set_connection_state(_connection_error_status(exc), str(exc))
                logger.warning("[飞书长连接] 启动失败: %s", exc)
            finally:
                # lark-oapi 1.7.1 的 WS 客户端使用模块级事件循环；stop 后需把
                # ping/receive/cache 协程收尾，否则 Python 退出时会报告悬空任务。
                try:
                    from lark_oapi.ws import client as ws_client_module

                    ws_loop = getattr(ws_client_module, "loop", None)
                    if ws_loop is not None and not ws_loop.is_running() and not ws_loop.is_closed():
                        pending = [
                            task
                            for task in asyncio.all_tasks(ws_loop)
                            if not task.done()
                        ]
                        for task in pending:
                            task.cancel()
                        if pending:
                            ws_loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=_entry,
            daemon=True,
            name=f"feishu-local-ws-{self.account_username[:24]}",
        )
        self._thread.start()

    def _set_connection_state(self, status: str, error: str = "") -> None:
        with self._lock:
            self._status = status
            self._last_error = error[:500]
            if status == "connected":
                self._connected_at = _dt(_now())

    def stop(self) -> None:
        channel = None
        worker_thread = None
        with self._lock:
            channel = self._channel
            self._channel = None
            worker_thread = self._thread
            self._thread = None
            self._status = "stopped"
            self._token = ""
            self._token_expires_at = 0.0
            self._binding_codes.clear()
        if channel is not None:
            try:
                channel.stop(join_timeout=5.0)
            except Exception:
                pass
        if (
            worker_thread is not None
            and worker_thread is not threading.current_thread()
            and worker_thread.is_alive()
        ):
            worker_thread.join(timeout=5.0)

    def status(self) -> Dict[str, Any]:
        profile = self.profile()
        connected = False
        with self._lock:
            channel = self._channel
            status = self._status
            error = self._last_error
            connected_at = self._connected_at
        try:
            ws_client = getattr(channel, "_ws_client", None)
            connected = bool(ws_client is not None and getattr(ws_client, "_conn", None) is not None)
        except Exception:
            connected = False
        if connected and status in {"connecting", "reconnecting"}:
            status = "connected"
            self._set_connection_state("connected")
        return {
            "success": True,
            "account_username": self.account_username,
            "status": status,
            "connected": connected,
            "connected_at": connected_at,
            "last_error": error,
            "last_card_action_at": self._last_card_action_at,
            "profile": {
                "enabled": bool(profile.get("enabled")),
                "backend": str(profile.get("backend") or "local_ws"),
                "app_id": str(profile.get("app_id") or ""),
                "app_secret_saved": bool(profile.get("app_secret_saved")),
                "authorized_open_id": str(profile.get("authorized_open_id") or ""),
                "send_personal": bool(profile.get("send_personal", True)),
                "send_groups": bool(profile.get("send_groups", True)),
                "groups": profile.get("groups") if isinstance(profile.get("groups"), list) else [],
            },
        }


class LocalFeishuManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._account = ""
        self._bridge: Optional[LocalFeishuBridge] = None

    @property
    def account(self) -> str:
        with self._lock:
            return self._account

    def activate(self, username: str) -> None:
        key = _account_key(username)
        with self._lock:
            if key == self._account and self._bridge is not None:
                self._bridge.start()
                return
            old = self._bridge
            self._bridge = None
            self._account = key
        if old:
            old.stop()
        if key:
            bridge = LocalFeishuBridge(key)
            with self._lock:
                self._bridge = bridge
            bridge.start()

    def deactivate(self) -> None:
        with self._lock:
            bridge = self._bridge
            self._bridge = None
            self._account = ""
        if bridge:
            bridge.stop()

    def bridge(self) -> Optional[LocalFeishuBridge]:
        with self._lock:
            return self._bridge

    def restart(self) -> None:
        with self._lock:
            account = self._account
            old = self._bridge
            self._bridge = None
        if old:
            old.stop()
        if account:
            bridge = LocalFeishuBridge(account)
            with self._lock:
                self._bridge = bridge
            bridge.start()


_MANAGER = LocalFeishuManager()


def activate_local_feishu_account(username: str) -> None:
    _MANAGER.activate(username)


def deactivate_local_feishu_account() -> None:
    _MANAGER.deactivate()


def selected_task_backend() -> str:
    override = str(os.getenv("QCSCKP_RETARGET_TASK_BACKEND") or "").strip().lower()
    if override in {"local_ws", "cloud_http"}:
        return override
    account = _MANAGER.account
    if account:
        return str(_profile_for(account).get("backend") or "local_ws")
    return "local_ws"


def get_local_feishu_status() -> Dict[str, Any]:
    bridge = _MANAGER.bridge()
    if bridge is None:
        return {
            "success": True,
            "account_username": "",
            "status": "logged_out",
            "connected": False,
            "last_error": "",
            "profile": {},
        }
    return bridge.status()


def save_local_feishu_config(config: Dict[str, Any]) -> Dict[str, Any]:
    account = _MANAGER.account
    if not account:
        return {"success": False, "message": "请先登录工具账号"}
    app_id = str((config or {}).get("app_id") or "").strip()
    app_secret = str((config or {}).get("app_secret") or "")
    existing = _profile_for(account)
    if not app_id:
        return {"success": False, "message": "请输入飞书App ID"}
    changes: Dict[str, Any] = {
        "app_id": app_id,
        "enabled": bool((config or {}).get("enabled", True)),
        "backend": "local_ws",
        "send_personal": bool((config or {}).get("send_personal", True)),
        "send_groups": bool((config or {}).get("send_groups", True)),
    }
    if app_secret:
        changes["app_secret_protected"] = _protect_secret(app_secret)
    elif not existing.get("app_secret_saved"):
        return {"success": False, "message": "请输入飞书App Secret"}
    _update_profile(account, changes)
    _MANAGER.restart()
    return {"success": True, "message": "飞书应用配置已加密保存到本机"}


def test_local_feishu_credentials() -> Dict[str, Any]:
    bridge = _MANAGER.bridge()
    if bridge is None:
        return {"success": False, "message": "请先登录工具账号"}
    return bridge.test_credentials()


def issue_local_feishu_binding_code(purpose: str) -> Dict[str, Any]:
    bridge = _MANAGER.bridge()
    if bridge is None:
        return {"success": False, "message": "请先登录工具账号"}
    return bridge.issue_binding_code(purpose)


def remove_local_feishu_group(chat_id: str) -> Dict[str, Any]:
    account = _MANAGER.account
    if not account:
        return {"success": False, "message": "请先登录工具账号"}
    profile = _profile_for(account)
    groups = [
        group
        for group in profile.get("groups") or []
        if isinstance(group, dict) and str(group.get("chat_id") or "") != str(chat_id or "")
    ]
    _update_profile(account, {"groups": groups})
    return {"success": True}


def clear_local_feishu_binding() -> Dict[str, Any]:
    account = _MANAGER.account
    if not account:
        return {"success": False, "message": "请先登录工具账号"}
    _update_profile(
        account,
        {
            "authorized_open_id": "",
            "personal_chat_id": "",
            "groups": [],
        },
    )
    return {"success": True}


def send_local_feishu_test_card() -> Dict[str, Any]:
    bridge = _MANAGER.bridge()
    if bridge is None:
        return {"success": False, "message": "请先登录工具账号"}
    return bridge.send_test_card()


def _expire_local_tasks(account_username: str) -> List[str]:
    account = _account_key(account_username)
    if not account:
        return []
    now_text = _dt(_now())
    expired: List[str] = []
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT task_uid FROM local_retarget_task "
            "WHERE account_username=? AND active_dedupe_key IS NOT NULL AND ("
            "(status IN ('pending','approved_queued') AND expires_at<=?) OR "
            "(status IN ('claimed','executing') AND claim_expires_at IS NOT NULL AND claim_expires_at<=?))",
            (account, now_text, now_text),
        ).fetchall()
        expired = [str(row["task_uid"]) for row in rows]
        if expired:
            placeholders = ",".join("?" for _ in expired)
            conn.execute(
                f"UPDATE local_retarget_task SET status='expired',active_dedupe_key=NULL,"
                f"claim_token=NULL,claim_expires_at=NULL,finished_at=?,"
                f"result_message='追投卡片已过期或本机执行中断',updated_at=? "
                f"WHERE task_uid IN ({placeholders})",
                [now_text, now_text, *expired],
            )
        conn.commit()
    finally:
        conn.close()
    return expired


def create_local_retarget_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    account = _MANAGER.account
    bridge = _MANAGER.bridge()
    if not account or bridge is None:
        return {"success": False, "message": "请先登录工具账号并配置飞书长连接"}
    status = bridge.status()
    if not status.get("connected"):
        return {
            "success": False,
            "message": "飞书长连接尚未就绪，请在“飞书绑定”页面确认状态为已连接",
        }
    profile = bridge.profile()
    if not profile.get("authorized_open_id") and not profile.get("groups"):
        return {"success": False, "message": "请先完成飞书个人或群绑定"}
    payload = dict(payload or {})
    raw_materials = payload.get("materials")
    if isinstance(raw_materials, list) and len(raw_materials) > 20:
        return {"success": False, "message": "一张追投卡片最多支持20条素材"}
    materials = _normalize_materials(payload)
    required = {
        "aavid": str(payload.get("aavid") or "").strip(),
        "ad_id": str(payload.get("ad_id") or "").strip(),
        "target_uid": str(payload.get("target_uid") or "").strip(),
        "strategy_id": str(payload.get("strategy_id") or "").strip(),
        "strategy_hash": str(payload.get("strategy_hash") or "").strip(),
    }
    if (
        any(not value for value in required.values())
        or not materials
        or len(materials) > 20
        or not re.fullmatch(r"[a-f0-9]{64}", required["strategy_hash"])
    ):
        return {"success": False, "message": "账户、计划、素材或策略快照不完整"}
    payload["materials"] = materials
    payload["material_id"] = materials[0]["material_id"]
    payload["material_name"] = materials[0]["material_name"]
    payload.setdefault("promotion_scene", "live")
    payload.setdefault("plan_system", "unknown")
    payload.setdefault("trigger_level", "material")
    for task_uid in _expire_local_tasks(account):
        threading.Thread(
            target=bridge.update_task_cards,
            args=(task_uid,),
            daemon=True,
        ).start()
    dedupe = hashlib.sha256(
        f"{account}|{required['target_uid']}|{required['strategy_id']}".encode("utf-8")
    ).hexdigest()
    task_uid = str(uuid.uuid4())
    nonce = secrets.token_hex(32)
    created_at = _dt(_now())
    expires_at = _dt(_now() + timedelta(minutes=30))
    conn = _db()
    try:
        try:
            conn.execute(
                "INSERT INTO local_retarget_task("
                "task_uid,account_username,active_dedupe_key,status,action_nonce,"
                "payload_json,expires_at,created_at,updated_at"
                ") VALUES(?,?,?,'pending',?,?,?,?,?)",
                (
                    task_uid,
                    account,
                    dedupe,
                    nonce,
                    _json(payload),
                    expires_at,
                    created_at,
                    created_at,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT task_uid,status,expires_at FROM local_retarget_task "
                "WHERE account_username=? AND active_dedupe_key=? LIMIT 1",
                (account, dedupe),
            ).fetchone()
            if row:
                return {
                    "success": True,
                    "duplicate": True,
                    "data": {
                        "task_uid": row["task_uid"],
                        "status": row["status"],
                        "expires_at": row["expires_at"],
                    },
                }
            raise
    finally:
        conn.close()
    row = _task_row(task_uid, account)
    task = _task_payload(row or {})
    try:
        messages = bridge.send_task_cards(task)
    except Exception as exc:
        conn = _db()
        try:
            conn.execute(
                "UPDATE local_retarget_task SET status='failed',active_dedupe_key=NULL,"
                "result_message=?,finished_at=?,updated_at=? WHERE task_uid=?",
                (f"飞书卡片发送失败：{exc}", _dt(_now()), _dt(_now()), task_uid),
            )
            conn.commit()
        finally:
            conn.close()
        return {"success": False, "message": str(exc), "task_uid": task_uid}
    conn = _db()
    try:
        conn.execute(
            "UPDATE local_retarget_task SET card_messages_json=?,updated_at=? WHERE task_uid=?",
            (_json(messages), _dt(_now()), task_uid),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "success": True,
        "duplicate": False,
        "data": {
            "task_uid": task_uid,
            "status": "pending",
            "expires_at": expires_at,
            "sent_count": len(messages),
        },
    }


def handle_local_card_action(
    account_username: str,
    *,
    task_uid: str,
    nonce: str,
    action: str,
    operator_open_id: str,
) -> Dict[str, Any]:
    account = _account_key(account_username)
    profile = _profile_for(account)
    if operator_open_id != str(profile.get("authorized_open_id") or ""):
        return {"success": False, "message": "你不是该工具账号绑定的追投授权人"}
    row = _task_row(task_uid, account)
    if not row:
        return {"success": False, "message": "追投任务不存在或不属于当前账号"}
    if not secrets.compare_digest(str(row.get("action_nonce") or ""), str(nonce or "")):
        return {"success": False, "message": "卡片任务校验失败"}
    expires = _parse_dt(row.get("expires_at"))
    if expires and expires <= _now() and str(row.get("status")) in ACTIVE_STATUSES:
        conn = _db()
        try:
            conn.execute(
                "UPDATE local_retarget_task SET status='expired',active_dedupe_key=NULL,"
                "result_message='追投卡片已超过30分钟有效期',finished_at=?,updated_at=? "
                "WHERE task_uid=?",
                (_dt(_now()), _dt(_now()), task_uid),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "success": False,
            "message": "卡片已超过30分钟有效期",
            "update": True,
        }
    if action == "view":
        return {"success": True, "message": "已展开详情", "update": True, "expanded": True}
    if action not in {"approve", "reject"}:
        return {"success": False, "message": "未知的卡片操作"}
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status FROM local_retarget_task WHERE task_uid=? AND account_username=?",
            (task_uid, account),
        ).fetchone()
        if not current:
            conn.rollback()
            return {"success": False, "message": "任务不存在"}
        current_status = str(current["status"])
        if current_status != "pending":
            conn.rollback()
            return {
                "success": current_status in {"approved_queued", "claimed", "executing", "succeeded", "rejected"},
                "message": "该卡片已经处理，不会重复追投",
                "update": True,
            }
        now_text = _dt(_now())
        if action == "approve":
            conn.execute(
                "UPDATE local_retarget_task SET status='approved_queued',approved_by=?,"
                "approved_at=?,updated_at=? WHERE task_uid=? AND status='pending'",
                (operator_open_id, now_text, now_text, task_uid),
            )
            message = "已确认，工具将重新复核后执行追投"
        else:
            conn.execute(
                "UPDATE local_retarget_task SET status='rejected',approved_by=?,"
                "active_dedupe_key=NULL,finished_at=?,result_message='授权人暂不追投',"
                "updated_at=? WHERE task_uid=? AND status='pending'",
                (operator_open_id, now_text, now_text, task_uid),
            )
            message = "本次提醒已结束，不会追投"
        conn.commit()
        return {"success": True, "message": message, "update": True}
    finally:
        conn.close()


def pull_local_retarget_task() -> Dict[str, Any]:
    account = _MANAGER.account
    bridge = _MANAGER.bridge()
    if not account or bridge is None:
        return {"success": False, "message": "missing_local_account", "silent": True}
    for task_uid in _expire_local_tasks(account):
        threading.Thread(
            target=bridge.update_task_cards,
            args=(task_uid,),
            daemon=True,
        ).start()
    claim_token = secrets.token_hex(32)
    now_text = _dt(_now())
    claim_expires = _dt(_now() + timedelta(minutes=10))
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM local_retarget_task WHERE account_username=? "
            "AND status='approved_queued' AND expires_at>? ORDER BY id ASC LIMIT 1",
            (account, now_text),
        ).fetchone()
        if not row:
            conn.commit()
            return {"success": True, "data": None}
        updated = conn.execute(
            "UPDATE local_retarget_task SET status='claimed',claim_token=?,"
            "claim_expires_at=?,claimed_at=?,updated_at=? "
            "WHERE task_uid=? AND status='approved_queued'",
            (
                claim_token,
                claim_expires,
                now_text,
                now_text,
                row["task_uid"],
            ),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return {"success": True, "data": None}
        conn.commit()
    finally:
        conn.close()
    claimed = _task_row(str(row["task_uid"]), account)
    task = _task_payload(claimed or {})
    task["claim_token"] = claim_token
    return {"success": True, "data": task}


def report_local_retarget_task(
    task_uid: str,
    claim_token: str,
    status: str,
    *,
    message: str = "",
    detail: str = "",
    regulate_task_id: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if status not in {"executing", "succeeded", "failed"}:
        return {"success": False, "message": "本地任务状态无效"}
    row = _task_row(task_uid)
    if not row:
        return {"success": False, "message": "本地任务不存在"}
    if not secrets.compare_digest(str(row.get("claim_token") or ""), str(claim_token or "")):
        return {"success": False, "message": "本地任务领取令牌无效"}
    current_status = str(row.get("status") or "")
    if current_status in TERMINAL_STATUSES:
        return {"success": current_status == status, "message": "任务已经结束"}
    now_text = _dt(_now())
    conn = _db()
    try:
        if status == "executing":
            updated = conn.execute(
                "UPDATE local_retarget_task SET status='executing',claim_expires_at=?,"
                "result_message=?,updated_at=? WHERE task_uid=? AND claim_token=? "
                "AND status IN ('claimed','executing')",
                (
                    _dt(_now() + timedelta(minutes=10)),
                    str(message or "")[:1000],
                    now_text,
                    task_uid,
                    claim_token,
                ),
            )
        else:
            updated = conn.execute(
                "UPDATE local_retarget_task SET status=?,active_dedupe_key=NULL,"
                "claim_expires_at=NULL,result_message=?,result_detail=?,regulate_task_id=?,"
                "result_json=?,finished_at=?,updated_at=? "
                "WHERE task_uid=? AND claim_token=? AND status IN ('claimed','executing')",
                (
                    status,
                    str(message or "")[:1000],
                    str(detail or "")[:4000],
                    str(regulate_task_id or "")[:128],
                    _json(result or {}),
                    now_text,
                    now_text,
                    task_uid,
                    claim_token,
                ),
            )
        conn.commit()
        if updated.rowcount != 1:
            return {"success": False, "message": "本地任务状态已经变化"}
    finally:
        conn.close()
    bridge = _MANAGER.bridge()
    if bridge:
        threading.Thread(
            target=bridge.update_task_cards,
            args=(task_uid,),
            daemon=True,
            name="feishu-result-card-update",
        ).start()
    return {"success": True}
