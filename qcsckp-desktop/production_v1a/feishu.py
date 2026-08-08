"""飞书长连接、持久化 Inbox/Outbox、绑定码和 V1A 模拟卡。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Callable

from .candidates import CandidateService
from .security import (
    protect_for_current_windows_user,
    sanitize_exception_text,
    stable_json_hash,
    unprotect_for_current_windows_user,
)
from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .timeutils import utc_iso, utc_now


class FeishuError(RuntimeError):
    pass


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    data = None
    request_headers = {"Content-Type": "application/json; charset=utf-8"}
    request_headers.update(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise FeishuError(f"HTTP {exc.code}: {body[:500]}") from exc
    parsed = json.loads(body or "{}")
    if int(parsed.get("code") or 0) != 0:
        raise FeishuError(
            f"飞书错误 {parsed.get('code')}: {parsed.get('msg') or parsed.get('message') or 'unknown'}"
        )
    return parsed


class FeishuApiClient:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 120:
                return self._token
        response = _http_json(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            payload={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        token = str(response.get("tenant_access_token") or "")
        if not token:
            raise FeishuError("飞书未返回 tenant_access_token")
        with self._lock:
            self._token = token
            self._expires_at = time.time() + max(300, int(response.get("expire") or 7200))
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = "https://open.feishu.cn/open-apis" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return _http_json(
            method,
            url,
            payload=payload,
            headers={"Authorization": f"Bearer {self.token()}"},
        )

    def send_card(self, receive_type: str, receive_id: str, card: dict[str, Any]) -> str:
        response = self.request(
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

    def send_text(self, receive_type: str, receive_id: str, text: str) -> str:
        response = self.request(
            "POST",
            "/im/v1/messages",
            query={"receive_id_type": receive_type},
            payload={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        return str((response.get("data") or {}).get("message_id") or "")

    def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        self.request(
            "PATCH",
            "/im/v1/messages/" + urllib.parse.quote(message_id, safe=""),
            payload={"content": json.dumps(card, ensure_ascii=False)},
        )


def build_candidate_preview_card(
    batch: dict[str, Any],
    *,
    account_name: str,
    plan_name: str,
    plan_system: str,
    promotion_scene: str,
    groups: list[dict[str, Any]] | None = None,
    page: int = 1,
    page_size: int = 5,
) -> dict[str, Any]:
    materials = json.loads(str(batch["material_snapshot_json"]))
    frozen_groups = list(groups or [])
    total_pages = max(1, (len(frozen_groups) + page_size - 1) // page_size)
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    visible_groups = frozen_groups[start : start + page_size]
    materials_by_id = {
        str(material["material_id"]): material for material in materials
    }
    system_label = {"global": "全域", "chengfang": "乘方"}.get(plan_system, "待确认")
    scene_label = {"product": "推商品", "live": "推直播"}.get(
        promotion_scene, "待确认"
    )
    lines = [
        f"**账户：** {account_name}",
        f"**账户 ID：** {batch['aavid']}",
        f"**计划：** {plan_name}",
        f"**计划类型：** {system_label} · {scene_label}",
        f"**候选素材：** {len(materials)} 条",
        f"**冻结分组：** {len(frozen_groups)} 组，当前第 {page}/{total_pages} 页",
        f"**有效期：** {batch['expires_at']}",
    ]
    group_lines: list[str] = []
    for group in visible_groups:
        material_ids = [
            str(value) for value in json.loads(str(group.get("material_ids_json") or "[]"))
        ]
        material_lines = []
        for material_id in material_ids:
            material = materials_by_id.get(material_id) or {}
            material_lines.append(
                f"- {material.get('material_name') or material_id}（素材 ID：{material_id}）"
            )
        group_lines.append(
            f"**第 {group.get('sequence')} 组 · {len(material_ids)} 条**\n"
            + "\n".join(material_lines)
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "V1A 追投候选模拟预览"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "**V1A 模拟，不执行任何千川操作**\n\n" + "\n".join(lines),
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": "\n\n".join(group_lines) or "尚未在桌面任务中心保存冻结分组",
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "确认后只生成 dry-run 结果和模拟审计，不会提交千川。分组只能在桌面任务中心修改。",
                    }
                ],
            },
            *(
                [
                    {
                        "tag": "action",
                        "actions": [
                            *(
                                [
                                    {
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": "上一页分组"},
                                        "value": {
                                            "action": "v1a_previous_page",
                                            "candidate_batch_id": batch["candidate_batch_id"],
                                            "page": page,
                                        },
                                    }
                                ]
                                if page > 1
                                else []
                            ),
                            *(
                                [
                                    {
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": "下一页分组"},
                                        "value": {
                                            "action": "v1a_next_page",
                                            "candidate_batch_id": batch["candidate_batch_id"],
                                            "page": page,
                                        },
                                    }
                                ]
                                if page < total_pages
                                else []
                            ),
                        ],
                    }
                ]
                if total_pages > 1
                else []
            ),
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认模拟"},
                        "type": "primary",
                        "value": {
                            "action": "v1a_confirm_groups",
                            "candidate_batch_id": batch["candidate_batch_id"],
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝模拟"},
                        "value": {
                            "action": "v1a_reject_groups",
                            "candidate_batch_id": batch["candidate_batch_id"],
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看任务中心"},
                        "value": {
                            "action": "v1a_view_task_center",
                            "candidate_batch_id": batch["candidate_batch_id"],
                        },
                    }
                ],
            },
        ],
    }


def build_adjustment_preview_card(
    candidate: dict[str, Any],
    *,
    account_name: str,
    plan_name: str,
    task_name: str,
    control_task_id: str,
    plan_system: str,
    promotion_scene: str,
) -> dict[str, Any]:
    system_label = {"global": "全域", "chengfang": "乘方"}.get(plan_system, "待确认")
    scene_label = {"product": "推商品", "live": "推直播"}.get(promotion_scene, "待确认")
    action_label = {
        "retarget_pause": "暂停追投任务模拟",
        "retarget_adjust": "预算/时长调整模拟",
    }.get(str(candidate["action_type"]), str(candidate["action_type"]))
    metrics = json.loads(str(candidate.get("metrics_snapshot_json") or "{}"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"V1A {action_label}"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "**V1A模拟，不执行任何千川操作**\n\n"
                    f"**账户：** {account_name}\n**账户 ID：** {candidate['aavid']}\n"
                    f"**计划：** {plan_name}\n**计划类型：** {system_label} · {scene_label}\n"
                    f"**Scene 2 调控任务：** {task_name}\n**调控任务 ID：** {control_task_id}\n"
                    f"**今日指标：** 消耗 {metrics.get('spend_cent', 0)} 分 · 订单 {metrics.get('order_count', 0)} · "
                    f"成交 {metrics.get('gmv_cent', 0)} 分 · ROI {metrics.get('roi_decimal') or '—'}\n"
                    f"**有效期：** {candidate['expires_at']}"
                ),
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "此卡只用于验证规则、证据和通知链路，不包含确认执行按钮。",
                    }
                ],
            },
        ],
    }


def build_candidate_result_card(
    batch: dict[str, Any],
    *,
    account_name: str,
    plan_name: str,
    plan_system: str,
    promotion_scene: str,
    group_count: int,
    result: str,
) -> dict[str, Any]:
    system_label = {"global": "全域", "chengfang": "乘方"}.get(plan_system, "待确认")
    scene_label = {"product": "推商品", "live": "推直播"}.get(
        promotion_scene, "待确认"
    )
    succeeded = result == "confirmed"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if succeeded else "grey",
            "title": {
                "tag": "plain_text",
                "content": "V1A 模拟确认完成" if succeeded else "V1A 模拟已拒绝",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "**未执行任何千川操作**\n\n"
                    f"**账户：** {account_name}\n**账户 ID：** {batch['aavid']}\n"
                    f"**计划：** {plan_name}\n**计划类型：** {system_label} · {scene_label}\n"
                    f"**冻结分组：** {group_count} 组\n"
                    f"**处理结果：** {'已生成 dry-run 结果与模拟审计' if succeeded else '已拒绝，本批次不会生成模拟执行结果'}"
                ),
            }
        ],
    }


class FeishuService:
    def __init__(
        self,
        database: RuntimeDatabase,
        writer: StorageWriter,
        candidates: CandidateService,
    ):
        self.database = database
        self.writer = writer
        self.candidates = candidates
        self._connection: FeishuLongConnection | None = None

    def save_credentials(self, tool_user_id: str, app_id: str, app_secret: str) -> None:
        app_id = app_id.strip()
        if not app_id.startswith("cli_") or not app_secret:
            raise ValueError("App ID 或 App Secret 格式不正确")
        protected_id = protect_for_current_windows_user(app_id)
        protected_secret = protect_for_current_windows_user(app_secret)
        now = utc_iso()
        self.writer.execute(
            """
            UPDATE feishu_profile
            SET app_id=NULL, encrypted_app_id=?, encrypted_app_secret=?, credential_status='untested',
                transport_status='disconnected', event_status='not_received',
                send_status='unavailable', last_error_code=NULL,
                last_error_message=NULL, updated_at=?
            WHERE tool_user_id=?
            """,
            (protected_id, protected_secret, now, tool_user_id),
        )

    def _profile_with_secret(self, tool_user_id: str) -> tuple[dict[str, Any], str]:
        profile = self.database.query_one(
            "SELECT * FROM feishu_profile WHERE tool_user_id=?", (tool_user_id,)
        )
        if not profile or not profile.get("encrypted_app_secret"):
            raise FeishuError("飞书凭据未配置")
        protected_id = str(profile.get("encrypted_app_id") or "")
        if protected_id:
            app_id = unprotect_for_current_windows_user(protected_id)
        else:
            # 兼容早期V1A明文App ID；首次重新保存凭据后即清空旧列。
            app_id = str(profile.get("app_id") or "")
        if not app_id.startswith("cli_"):
            raise FeishuError("飞书App ID无法解密或格式无效")
        secret = unprotect_for_current_windows_user(str(profile["encrypted_app_secret"]))
        profile = dict(profile)
        profile["app_id"] = app_id
        return profile, secret

    def test_credentials(self, tool_user_id: str) -> dict[str, Any]:
        try:
            profile, secret = self._profile_with_secret(tool_user_id)
            FeishuApiClient(str(profile["app_id"]), secret).token()
            self.writer.execute(
                "UPDATE feishu_profile SET credential_status='valid', send_status=?, last_error_code=NULL, last_error_message=NULL, updated_at=? WHERE tool_user_id=?",
                (
                    "ready" if profile.get("authorized_open_id") else "unavailable",
                    utc_iso(),
                    tool_user_id,
                ),
            )
            return {"valid": True}
        except Exception as exc:
            self.writer.execute(
                "UPDATE feishu_profile SET credential_status='invalid', send_status='unavailable', last_error_code=?, last_error_message=?, updated_at=? WHERE tool_user_id=?",
                (type(exc).__name__, sanitize_exception_text(exc, 500), utc_iso(), tool_user_id),
            )
            return {"valid": False, "error": sanitize_exception_text(exc, 500)}

    def issue_binding_code(self, tool_user_id: str, purpose: str) -> str:
        if purpose not in {"personal", "group"}:
            raise ValueError("purpose must be personal or group")
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        self.writer.execute(
            """
            INSERT INTO feishu_binding_code(
                binding_code_uid, tool_user_id, code_hash, purpose,
                expires_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                f"bind_{uuid.uuid4().hex}",
                tool_user_id,
                code_hash,
                purpose,
                utc_iso(utc_now() + timedelta(minutes=10)),
                utc_iso(),
            ),
        )
        return code

    def ingest_event(
        self,
        *,
        tool_user_id: str,
        event_id: str,
        event_type: str,
        sender_open_id: str,
        message_id: str | None,
        payload: dict[str, Any],
    ) -> bool:
        """持久化事件；重复 event_id 返回 False 且绝不再次处理。"""

        now = utc_iso()
        serialized_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        protected_payload = protect_for_current_windows_user(serialized_payload)

        def op(conn):
            with short_transaction(conn):
                try:
                    conn.execute(
                        """
                        INSERT INTO feishu_inbox(
                            event_id, tool_user_id, event_type, sender_open_id,
                            message_id, received_at, status, payload_hash,
                            payload_json
                        ) VALUES(?, ?, ?, ?, ?, ?, 'received', ?, ?)
                        """,
                        (
                            event_id,
                            tool_user_id,
                            event_type,
                            sender_open_id,
                            message_id,
                            now,
                            stable_json_hash(payload),
                            protected_payload,
                        ),
                    )
                except __import__("sqlite3").IntegrityError:
                    return False
                conn.execute(
                    """
                    UPDATE feishu_profile
                    SET event_status='receiving', last_event_at=?, updated_at=?
                    WHERE tool_user_id=?
                    """,
                    (now, now, tool_user_id),
                )
                return True

        return bool(self.writer.submit(op))

    def process_message(
        self,
        tool_user_id: str,
        *,
        event_id: str,
        sender_open_id: str,
        chat_id: str,
        chat_type: str,
        text: str,
        message_id: str = "",
    ) -> dict[str, Any]:
        payload = {
            "sender_open_id": sender_open_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "text": text,
            "message_id": message_id,
        }
        if not self.ingest_event(
            tool_user_id=tool_user_id,
            event_id=event_id,
            event_type="im.message.receive_v1",
            sender_open_id=sender_open_id,
            message_id=message_id,
            payload=payload,
        ):
            return {"processed": False, "reason": "duplicate_event"}
        return self._process_message_received(tool_user_id, event_id, payload)

    def _process_message_received(
        self, tool_user_id: str, event_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        sender_open_id = str(payload.get("sender_open_id") or "")
        chat_id = str(payload.get("chat_id") or "")
        chat_type = str(payload.get("chat_type") or "")
        text = str(payload.get("text") or "")
        personal = re.search(r"(?:^|\s)绑定\s+(\d{6})(?:\s|$)", text)
        group = re.search(r"绑定群\s+(\d{6})(?:\s|$)", text)
        try:
            if group:
                result = self._bind_group(
                    tool_user_id, sender_open_id, chat_id, chat_type, group.group(1)
                )
            elif personal:
                result = self._bind_personal(
                    tool_user_id, sender_open_id, chat_id, chat_type, personal.group(1)
                )
            else:
                result = {"processed": True, "action": "ignored"}
            self._mark_inbox(tool_user_id, event_id, "processed")
            return result
        except Exception:
            self._mark_inbox(tool_user_id, event_id, "failed")
            raise

    def _consume_code(self, conn, tool_user_id: str, purpose: str, code: str) -> bool:
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        row = conn.execute(
            """
            SELECT binding_code_uid FROM feishu_binding_code
            WHERE tool_user_id=? AND purpose=? AND code_hash=?
              AND used_at IS NULL AND expires_at>?
            """,
            (tool_user_id, purpose, code_hash, utc_iso()),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE feishu_binding_code SET used_at=? WHERE binding_code_uid=? AND used_at IS NULL",
            (utc_iso(), row["binding_code_uid"]),
        )
        return True

    def _ensure_admin_route(self, conn, tool_user_id: str) -> str:
        row = conn.execute(
            "SELECT route_id FROM feishu_route WHERE tool_user_id=? AND route_name='管理员默认位置'",
            (tool_user_id,),
        ).fetchone()
        if row:
            return str(row["route_id"])
        route_id = f"route_{uuid.uuid4().hex}"
        now = utc_iso()
        conn.execute(
            "INSERT INTO feishu_route(route_id, tool_user_id, route_name, created_at, updated_at) VALUES(?, ?, '管理员默认位置', ?, ?)",
            (route_id, tool_user_id, now, now),
        )
        return route_id

    def _bind_personal(
        self,
        tool_user_id: str,
        sender_open_id: str,
        chat_id: str,
        chat_type: str,
        code: str,
    ) -> dict[str, Any]:
        if chat_type != "p2p":
            raise FeishuError("个人绑定码必须私聊机器人发送")

        def op(conn):
            with short_transaction(conn):
                if not self._consume_code(conn, tool_user_id, "personal", code):
                    raise FeishuError("个人绑定码无效、已使用或已过期")
                profile = conn.execute(
                    "SELECT authorized_open_id FROM feishu_profile WHERE tool_user_id=?",
                    (tool_user_id,),
                ).fetchone()
                existing = str(profile["authorized_open_id"] or "") if profile else ""
                if existing and existing != sender_open_id:
                    raise FeishuError("该工具账号已绑定其他授权人")
                route_id = self._ensure_admin_route(conn, tool_user_id)
                conn.execute(
                    "UPDATE feishu_profile SET authorized_open_id=?, binding_status='bound', send_status='ready', updated_at=? WHERE tool_user_id=?",
                    (sender_open_id, utc_iso(), tool_user_id),
                )
                conn.execute(
                    "UPDATE feishu_route SET personal_open_id=?, updated_at=? WHERE route_id=?",
                    (sender_open_id, utc_iso(), route_id),
                )
                return route_id

        route_id = self.writer.submit(op)
        return {"processed": True, "action": "personal_bound", "route_id": route_id}

    def _bind_group(
        self,
        tool_user_id: str,
        sender_open_id: str,
        chat_id: str,
        chat_type: str,
        code: str,
    ) -> dict[str, Any]:
        if chat_type not in {"group", "topic"}:
            raise FeishuError("群绑定码必须在群里@机器人发送")

        def op(conn):
            with short_transaction(conn):
                profile = conn.execute(
                    "SELECT authorized_open_id FROM feishu_profile WHERE tool_user_id=?",
                    (tool_user_id,),
                ).fetchone()
                if not profile or str(profile["authorized_open_id"] or "") != sender_open_id:
                    raise FeishuError("只有已绑定授权人可以绑定接收群")
                if not self._consume_code(conn, tool_user_id, "group", code):
                    raise FeishuError("群绑定码无效、已使用或已过期")
                route_id = self._ensure_admin_route(conn, tool_user_id)
                route = conn.execute(
                    "SELECT group_chat_ids_json FROM feishu_route WHERE route_id=?",
                    (route_id,),
                ).fetchone()
                groups = json.loads(str(route["group_chat_ids_json"] or "[]"))
                if chat_id not in groups:
                    groups.append(chat_id)
                conn.execute(
                    "UPDATE feishu_route SET group_chat_ids_json=?, updated_at=? WHERE route_id=?",
                    (json.dumps(sorted(groups)), utc_iso(), route_id),
                )
                return route_id

        route_id = self.writer.submit(op)
        return {"processed": True, "action": "group_bound", "route_id": route_id}

    def _mark_inbox(self, tool_user_id: str, event_id: str, status: str) -> None:
        self.writer.execute(
            "UPDATE feishu_inbox SET status=?, processed_at=? WHERE tool_user_id=? AND event_id=?",
            (status, utc_iso(), tool_user_id, event_id),
        )

    def process_card_action(
        self,
        tool_user_id: str,
        *,
        event_id: str,
        operator_open_id: str,
        message_id: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "operator_open_id": operator_open_id,
            "message_id": message_id,
            "value": value,
        }
        if not self.ingest_event(
            tool_user_id=tool_user_id,
            event_id=event_id,
            event_type="card.action.trigger",
            sender_open_id=operator_open_id,
            message_id=message_id,
            payload=payload,
        ):
            return {"processed": False, "reason": "duplicate_event"}
        return self._process_card_received(tool_user_id, event_id, payload)

    def _process_card_received(
        self, tool_user_id: str, event_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        operator_open_id = str(payload.get("operator_open_id") or "")
        message_id = str(payload.get("message_id") or "")
        value = payload.get("value")
        if not isinstance(value, dict):
            value = {}
        profile = self.database.query_one(
            "SELECT authorized_open_id FROM feishu_profile WHERE tool_user_id=?",
            (tool_user_id,),
        )
        if not profile or str(profile["authorized_open_id"] or "") != operator_open_id:
            self._mark_inbox(tool_user_id, event_id, "rejected_unauthorized")
            raise FeishuError("只有已绑定授权人可以操作模拟卡")
        action = str(value.get("action") or "")
        batch_id = str(value.get("candidate_batch_id") or "")
        page = max(1, int(value.get("page") or 1))
        page_data = self.candidates.page(tool_user_id, batch_id, page)
        frozen_groups = self.database.query_all(
            "SELECT * FROM retarget_group WHERE candidate_batch_id=? ORDER BY sequence",
            (batch_id,),
        )
        updated_card = None
        target = self.database.query_one(
            "SELECT * FROM source_plan WHERE tool_user_id=? AND target_uid=?",
            (tool_user_id, page_data["batch"]["target_uid"]),
        )
        account = self.database.query_one(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=?",
            (tool_user_id, page_data["batch"]["aavid"]),
        )
        if not target or not account:
            raise FeishuError("候选关联的账户或计划不存在")
        if action in {"v1a_previous_page", "v1a_next_page"}:
            destination = page - 1 if action == "v1a_previous_page" else page + 1
            group_pages = max(1, math.ceil(len(frozen_groups) / 5))
            destination = min(max(destination, 1), group_pages)
            updated_card = build_candidate_preview_card(
                page_data["batch"],
                account_name=str(account["account_name"]),
                plan_name=str(target["plan_name"]),
                plan_system=str(target["plan_system"]),
                promotion_scene=str(target["promotion_scene"]),
                groups=frozen_groups,
                page=destination,
            )
            groups = []
        elif action == "v1a_confirm_groups":
            groups = self.candidates.confirm_groups(
                tool_user_id,
                batch_id,
                authorized_by_open_id=operator_open_id,
            )
            updated_card = build_candidate_result_card(
                page_data["batch"],
                account_name=str(account["account_name"]),
                plan_name=str(target["plan_name"]),
                plan_system=str(target["plan_system"]),
                promotion_scene=str(target["promotion_scene"]),
                group_count=len(groups),
                result="confirmed",
            )
        elif action == "v1a_reject_groups":
            self.candidates.reject_groups(
                tool_user_id, batch_id, rejected_by_open_id=operator_open_id
            )
            groups = []
            updated_card = build_candidate_result_card(
                page_data["batch"],
                account_name=str(account["account_name"]),
                plan_name=str(target["plan_name"]),
                plan_system=str(target["plan_system"]),
                promotion_scene=str(target["promotion_scene"]),
                group_count=len(frozen_groups),
                result="rejected",
            )
        elif action == "v1a_view_task_center":
            groups = []
        else:
            raise FeishuError("未知的 V1A 模拟动作")
        if updated_card:
            if action in {"v1a_confirm_groups", "v1a_reject_groups"}:
                self._enqueue_task_card_updates(
                    tool_user_id,
                    event_id=event_id,
                    task_uid=batch_id,
                    card=updated_card,
                )
            elif message_id:
                self._enqueue_card_update(
                    tool_user_id,
                    event_id=event_id,
                    message_id=message_id,
                    card=updated_card,
                )
        self._mark_inbox(tool_user_id, event_id, "processed")
        return {
            "processed": True,
            "action": action,
            "group_uids": groups,
            "dry_run": True,
            "updated_card": updated_card,
        }

    def _enqueue_card_update(
        self,
        tool_user_id: str,
        *,
        event_id: str,
        message_id: str,
        card: dict[str, Any],
    ) -> str:
        outbox_id = "outbox_update_" + stable_json_hash(
            [tool_user_id, event_id, message_id]
        )[:40]
        now = utc_iso()
        self.writer.execute(
            """
            INSERT INTO feishu_outbox(
                outbox_id, tool_user_id, route_id, task_uid,
                card_version, payload_json, status, attempt_count,
                next_attempt_at, updated_at, created_at
            ) VALUES(?, ?, ?, ?, 1, ?, 'queued', 0, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                outbox_id,
                tool_user_id,
                f"message:{message_id}",
                f"card_event:{event_id}",
                json.dumps(
                    {
                        "operation": "update_card",
                        "message_id": message_id,
                        "card": card,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
                now,
                now,
            ),
        )
        return outbox_id

    def _enqueue_task_card_updates(
        self,
        tool_user_id: str,
        *,
        event_id: str,
        task_uid: str,
        card: dict[str, Any],
    ) -> list[str]:
        message_ids = {
            str(row["message_id"])
            for row in self.database.query_all(
                "SELECT message_id FROM feishu_outbox WHERE tool_user_id=? AND task_uid=? AND status='sent' AND message_id IS NOT NULL",
                (tool_user_id, task_uid),
            )
        }
        return [
            self._enqueue_card_update(
                tool_user_id,
                event_id=event_id,
                message_id=message_id,
                card=card,
            )
            for message_id in sorted(message_ids)
        ]

    def recover_inbox(self, tool_user_id: str, limit: int = 100) -> dict[str, int]:
        """重放已落库但尚未完成的事件；所有业务写入仍保持幂等。"""

        rows = self.database.query_all(
            """
            SELECT * FROM feishu_inbox
            WHERE tool_user_id=? AND status='received'
            ORDER BY received_at ASC LIMIT ?
            """,
            (tool_user_id, max(1, min(int(limit), 500))),
        )
        processed = failed = unrecoverable = 0
        for row in rows:
            event_id = str(row["event_id"])
            try:
                protected = str(row.get("payload_json") or "")
                if not protected:
                    raise FeishuError("历史Inbox事件不含可恢复载荷")
                payload = json.loads(unprotect_for_current_windows_user(protected))
                if row["event_type"] == "im.message.receive_v1":
                    self._process_message_received(tool_user_id, event_id, payload)
                elif row["event_type"] == "card.action.trigger":
                    self._process_card_received(tool_user_id, event_id, payload)
                else:
                    raise FeishuError("不支持的Inbox事件类型")
                processed += 1
            except Exception:
                current = self.database.query_one(
                    "SELECT status FROM feishu_inbox WHERE tool_user_id=? AND event_id=?",
                    (tool_user_id, event_id),
                )
                if current and current.get("status") == "received":
                    self._mark_inbox(tool_user_id, event_id, "failed_unrecoverable")
                    unrecoverable += 1
                else:
                    failed += 1
        return {
            "processed": processed,
            "failed": failed,
            "unrecoverable": unrecoverable,
        }

    def enqueue_candidate_preview(self, tool_user_id: str, batch_id: str) -> list[str]:
        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                batch = conn.execute(
                    "SELECT * FROM candidate_batch WHERE tool_user_id=? AND candidate_batch_id=?",
                    (tool_user_id, batch_id),
                ).fetchone()
                if not batch:
                    raise KeyError(batch_id)
                if batch["status"] == "pending_approval":
                    return [
                        str(row["outbox_id"])
                        for row in conn.execute(
                            "SELECT outbox_id FROM feishu_outbox WHERE tool_user_id=? AND task_uid=? AND card_version=1 ORDER BY created_at",
                            (tool_user_id, batch_id),
                        ).fetchall()
                    ]
                if batch["status"] != "grouped":
                    raise FeishuError("请先在桌面任务中心保存至少一个冻结分组")
                target = conn.execute(
                    "SELECT * FROM source_plan WHERE tool_user_id=? AND target_uid=?",
                    (tool_user_id, batch["target_uid"]),
                ).fetchone()
                account = conn.execute(
                    "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=?",
                    (tool_user_id, batch["aavid"]),
                ).fetchone()
                if not target or not account:
                    raise FeishuError("候选账户或计划不存在")
                route_id = account["feishu_route_id"]
                if route_id:
                    route = conn.execute(
                        "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_id=? AND enabled=1",
                        (tool_user_id, route_id),
                    ).fetchone()
                else:
                    route = conn.execute(
                        "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_name='管理员默认位置' AND enabled=1",
                        (tool_user_id,),
                    ).fetchone()
                if not route:
                    raise FeishuError("该账户尚未配置飞书接收位置")
                groups = conn.execute(
                    "SELECT * FROM retarget_group WHERE candidate_batch_id=? AND status='frozen' ORDER BY sequence",
                    (batch_id,),
                ).fetchall()
                if not groups:
                    raise FeishuError("请先在桌面任务中心保存至少一个冻结分组")
                card = build_candidate_preview_card(
                    dict(batch),
                    account_name=str(account["account_name"]),
                    plan_name=str(target["plan_name"]),
                    plan_system=str(target["plan_system"]),
                    promotion_scene=str(target["promotion_scene"]),
                    groups=[dict(group) for group in groups],
                )
                targets: list[tuple[str, str]] = []
                if route["personal_open_id"]:
                    targets.append(("open_id", str(route["personal_open_id"])))
                for chat_id in json.loads(str(route["group_chat_ids_json"] or "[]")):
                    targets.append(("chat_id", str(chat_id)))
                if not targets:
                    raise FeishuError("飞书接收位置为空")
                outbox_ids: list[str] = []
                for receive_type, receive_id in targets:
                    outbox_id = f"outbox_{uuid.uuid4().hex}"
                    delivery_route = f"{receive_type}:{receive_id}"
                    conn.execute(
                        """
                        INSERT INTO feishu_outbox(
                            outbox_id, tool_user_id, route_id, task_uid,
                            card_version, payload_json, status, attempt_count,
                            next_attempt_at, updated_at, created_at
                        ) VALUES(?, ?, ?, ?, 1, ?, 'queued', 0, ?, ?, ?)
                        """,
                        (
                            outbox_id, tool_user_id, delivery_route, batch_id,
                            json.dumps(
                                {"receive_type": receive_type, "receive_id": receive_id, "card": card},
                                ensure_ascii=False, sort_keys=True,
                            ),
                            now, now, now,
                        ),
                    )
                    outbox_ids.append(outbox_id)
                updated = conn.execute(
                    "UPDATE candidate_batch SET status='pending_approval', updated_at=? WHERE tool_user_id=? AND candidate_batch_id=? AND status='grouped'",
                    (now, tool_user_id, batch_id),
                ).rowcount
                if updated != 1:
                    raise FeishuError("候选分组状态已变化，请刷新后重试")
                return outbox_ids

        return list(self.writer.submit(op))

    def enqueue_adjustment_preview(
        self, tool_user_id: str, adjustment_candidate_id: str
    ) -> list[str]:
        candidate = self.database.query_one(
            "SELECT * FROM adjustment_candidate WHERE tool_user_id=? AND adjustment_candidate_id=?",
            (tool_user_id, adjustment_candidate_id),
        )
        if not candidate:
            raise KeyError(adjustment_candidate_id)
        target = self.database.query_one(
            "SELECT * FROM source_plan WHERE tool_user_id=? AND target_uid=?",
            (tool_user_id, candidate["target_uid"]),
        )
        account = self.database.query_one(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=?",
            (tool_user_id, candidate["aavid"]),
        )
        task = self.database.query_one(
            "SELECT * FROM platform_control_task WHERE tool_user_id=? AND control_task_uid=?",
            (tool_user_id, candidate["control_task_uid"]),
        )
        if not target or not account or not task:
            raise FeishuError("暂停或调整候选关联对象不存在")
        route_id = account.get("feishu_route_id")
        if route_id:
            route = self.database.query_one(
                "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_id=? AND enabled=1",
                (tool_user_id, route_id),
            )
        else:
            route = self.database.query_one(
                "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_name='管理员默认位置' AND enabled=1",
                (tool_user_id,),
            )
        if not route:
            raise FeishuError("该账户尚未配置飞书接收位置")
        card = build_adjustment_preview_card(
            candidate,
            account_name=str(account["account_name"]),
            plan_name=str(target["plan_name"]),
            task_name=str(task.get("task_name") or task["control_task_id"]),
            control_task_id=str(task["control_task_id"]),
            plan_system=str(target["plan_system"]),
            promotion_scene=str(target["promotion_scene"]),
        )
        targets: list[tuple[str, str]] = []
        if route.get("personal_open_id"):
            targets.append(("open_id", str(route["personal_open_id"])))
        for chat_id in json.loads(str(route.get("group_chat_ids_json") or "[]")):
            targets.append(("chat_id", str(chat_id)))
        if not targets:
            raise FeishuError("飞书接收位置为空")
        now = utc_iso()
        outbox_ids: list[str] = []
        for receive_type, receive_id in targets:
            outbox_id = f"outbox_{uuid.uuid4().hex}"
            delivery_route = f"{receive_type}:{receive_id}"
            inserted = self.writer.execute(
                """
                INSERT INTO feishu_outbox(
                    outbox_id, tool_user_id, route_id, task_uid,
                    card_version, payload_json, status, attempt_count,
                    next_attempt_at, updated_at, created_at
                ) VALUES(?, ?, ?, ?, 1, ?, 'queued', 0, ?, ?, ?)
                ON CONFLICT(tool_user_id, route_id, task_uid, card_version) DO NOTHING
                """,
                (
                    outbox_id,
                    tool_user_id,
                    delivery_route,
                    adjustment_candidate_id,
                    json.dumps(
                        {"receive_type": receive_type, "receive_id": receive_id, "card": card},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                    now,
                ),
            )
            if inserted:
                outbox_ids.append(outbox_id)
            else:
                existing = self.database.query_one(
                    "SELECT outbox_id FROM feishu_outbox WHERE tool_user_id=? AND route_id=? AND task_uid=? AND card_version=1",
                    (tool_user_id, delivery_route, adjustment_candidate_id),
                )
                if existing:
                    outbox_ids.append(str(existing["outbox_id"]))
        self.writer.execute(
            "UPDATE adjustment_candidate SET status='preview_queued', updated_at=? WHERE adjustment_candidate_id=? AND status='frozen'",
            (now, adjustment_candidate_id),
        )
        return outbox_ids

    def enqueue_daily_report(
        self,
        tool_user_id: str,
        report_uid: str,
        summary: dict[str, Any],
        route: dict[str, Any],
        *,
        account_name: str | None = None,
    ) -> list[str]:
        real = summary["real_platform_operations"]
        simulation = summary["simulation_candidates"]
        completeness = summary.get("platform_log_completeness") or {}
        real_actions = "、".join(
            f"{name} {count}" for name, count in sorted(real.get("actions", {}).items())
        ) or "无真实平台操作"
        simulation_actions = "、".join(
            f"{name} {count}" for name, count in sorted(simulation.get("actions", {}).items())
        ) or "无模拟候选"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": f"{summary['business_date']} 千川昨日操作日报",
                },
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**范围：** {account_name or '全部已勾选账户'}\n"
                        f"**真实平台操作：** {real['total']} 条，失败 {real['failures']} 条\n"
                        f"{real_actions}"
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        f"**V1A 模拟候选（不执行千川操作）：** {simulation['total']} 条\n"
                        f"{simulation_actions}"
                    ),
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "平台日志完整性："
                            + (json.dumps(completeness, ensure_ascii=False) if completeness else "尚无完整同步证据"),
                        }
                    ],
                },
            ],
        }
        targets: list[tuple[str, str]] = []
        if route.get("personal_open_id"):
            targets.append(("open_id", str(route["personal_open_id"])))
        for chat_id in json.loads(str(route.get("group_chat_ids_json") or "[]")):
            targets.append(("chat_id", str(chat_id)))
        now = utc_iso()
        outbox_ids: list[str] = []
        for receive_type, receive_id in targets:
            outbox_id = f"outbox_{uuid.uuid4().hex}"
            delivery_route = f"{receive_type}:{receive_id}"
            inserted = self.writer.execute(
                """
                INSERT INTO feishu_outbox(
                    outbox_id, tool_user_id, route_id, task_uid,
                    card_version, payload_json, status, attempt_count,
                    next_attempt_at, updated_at, created_at
                ) VALUES(?, ?, ?, ?, 1, ?, 'queued', 0, ?, ?, ?)
                ON CONFLICT(tool_user_id, route_id, task_uid, card_version) DO NOTHING
                """,
                (
                    outbox_id,
                    tool_user_id,
                    delivery_route,
                    report_uid,
                    json.dumps(
                        {"receive_type": receive_type, "receive_id": receive_id, "card": card},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                    now,
                ),
            )
            if inserted:
                outbox_ids.append(outbox_id)
            else:
                existing = self.database.query_one(
                    "SELECT outbox_id FROM feishu_outbox WHERE tool_user_id=? AND route_id=? AND task_uid=? AND card_version=1",
                    (tool_user_id, delivery_route, report_uid),
                )
                if existing:
                    outbox_ids.append(str(existing["outbox_id"]))
        return outbox_ids

    def deliver_outbox_once(self, tool_user_id: str, limit: int = 20) -> int:
        now = utc_iso()
        claim_owner = f"outbox-worker-{uuid.uuid4().hex}"
        claim_expires_at = utc_iso(utc_now() + timedelta(minutes=2))

        def claim(conn):
            with short_transaction(conn):
                candidates = conn.execute(
                    """
                    SELECT outbox_id FROM feishu_outbox
                    WHERE tool_user_id=?
                      AND (
                        (status IN ('queued','retry') AND (next_attempt_at IS NULL OR next_attempt_at<=?))
                        OR (status='sending' AND claim_expires_at<=?)
                      )
                    ORDER BY created_at ASC LIMIT ?
                    """,
                    (tool_user_id, now, now, limit),
                ).fetchall()
                ids = [str(row["outbox_id"]) for row in candidates]
                if not ids:
                    return []
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE feishu_outbox SET status='sending', claim_owner=?, claim_expires_at=?, updated_at=? WHERE outbox_id IN ({placeholders})",
                    (claim_owner, claim_expires_at, now, *ids),
                )
                return [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM feishu_outbox WHERE claim_owner=? AND outbox_id IN ({placeholders}) ORDER BY created_at",
                        (claim_owner, *ids),
                    ).fetchall()
                ]

        rows = list(self.writer.submit(claim))
        if not rows:
            return 0
        profile, secret = self._profile_with_secret(tool_user_id)
        client = FeishuApiClient(str(profile["app_id"]), secret)
        sent = 0
        failures = 0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            try:
                if payload.get("operation") == "update_card":
                    message_id = str(payload["message_id"])
                    client.update_card(message_id, payload["card"])
                else:
                    message_id = client.send_card(
                        payload["receive_type"], payload["receive_id"], payload["card"]
                    )
                self.writer.execute(
                    "UPDATE feishu_outbox SET status='sent', message_id=?, attempt_count=attempt_count+1, sent_at=?, updated_at=?, claim_owner=NULL, claim_expires_at=NULL WHERE outbox_id=? AND status='sending' AND claim_owner=?",
                    (message_id, utc_iso(), utc_iso(), row["outbox_id"], claim_owner),
                )
                sent += 1
            except Exception as exc:
                failures += 1
                attempts = int(row["attempt_count"] or 0) + 1
                status = "failed" if attempts >= 6 else "retry"
                delay = min(300, 2**attempts)
                self.writer.execute(
                    "UPDATE feishu_outbox SET status=?, attempt_count=?, next_attempt_at=?, updated_at=?, claim_owner=NULL, claim_expires_at=NULL WHERE outbox_id=? AND status='sending' AND claim_owner=?",
                    (
                        status,
                        attempts,
                        utc_iso(utc_now() + timedelta(seconds=delay)),
                        utc_iso(),
                        row["outbox_id"],
                        claim_owner,
                    ),
                )
                self.writer.execute(
                    "UPDATE feishu_profile SET send_status='error', last_error_code=?, last_error_message=?, updated_at=? WHERE tool_user_id=?",
                    (type(exc).__name__, sanitize_exception_text(exc, 500), utc_iso(), tool_user_id),
                )
        if sent and not failures:
            self.writer.execute(
                "UPDATE feishu_profile SET send_status='ready', last_error_code=NULL, last_error_message=NULL, updated_at=? WHERE tool_user_id=?",
                (utc_iso(), tool_user_id),
            )
        for task_uid in {
            str(row.get("task_uid") or "")
            for row in rows
            if str(row.get("task_uid") or "").startswith("report_")
        }:
            states = self.database.query_all(
                "SELECT status FROM feishu_outbox WHERE tool_user_id=? AND task_uid=?",
                (tool_user_id, task_uid),
            )
            statuses = {str(item["status"]) for item in states}
            if statuses and statuses <= {"sent"}:
                self.writer.execute(
                    "UPDATE daily_report_delivery SET status='sent', sent_at=?, updated_at=? WHERE report_uid=?",
                    (utc_iso(), utc_iso(), task_uid),
                )
            elif "failed" in statuses and not statuses.intersection({"queued", "retry"}):
                self.writer.execute(
                    "UPDATE daily_report_delivery SET status='failed', updated_at=? WHERE report_uid=?",
                    (utc_iso(), task_uid),
                )
        return sent

    def status(self, tool_user_id: str) -> dict[str, Any]:
        profile = self.database.query_one(
            "SELECT * FROM feishu_profile WHERE tool_user_id=?", (tool_user_id,)
        )
        if not profile:
            return {
                "credential": "not_configured",
                "transport": "disconnected",
                "events": "not_received",
                "binding": "unbound",
                "sending": "unavailable",
            }
        return {
            "credential": profile["credential_status"],
            "transport": profile["transport_status"],
            "events": profile["event_status"],
            "binding": profile["binding_status"],
            "sending": profile["send_status"],
            "last_event_at": profile["last_event_at"],
            "last_error_code": profile["last_error_code"],
            "last_error_message": profile["last_error_message"],
        }

    def start_long_connection(self, tool_user_id: str) -> None:
        self.stop_long_connection()
        profile, secret = self._profile_with_secret(tool_user_id)
        self.recover_inbox(tool_user_id)
        self._connection = FeishuLongConnection(
            service=self,
            tool_user_id=tool_user_id,
            app_id=str(profile["app_id"]),
            app_secret=secret,
        )
        self._connection.start()

    def stop_long_connection(self) -> None:
        if self._connection:
            self._connection.stop()
            self._connection = None


class FeishuLongConnection:
    """lark-oapi 1.7.1 长连接；回调先落 Inbox，再异步处理。"""

    def __init__(
        self,
        *,
        service: FeishuService,
        tool_user_id: str,
        app_id: str,
        app_secret: str,
    ):
        self.service = service
        self.tool_user_id = tool_user_id
        self.app_id = app_id
        self.app_secret = app_secret
        self._client = None
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.service.writer.execute(
            "UPDATE feishu_profile SET transport_status='connecting', updated_at=? WHERE tool_user_id=?",
            (utc_iso(), self.tool_user_id),
        )

        def entry() -> None:
            try:
                import lark_oapi as lark
                from lark_oapi import LogLevel
                from lark_oapi.event.callback.model.p2_card_action_trigger import (
                    P2CardActionTriggerResponse,
                )
                from lark_oapi.ws import Client as WsClient

                def message_handler(data: Any) -> None:
                    header = getattr(data, "header", None)
                    event = getattr(data, "event", None)
                    message = getattr(event, "message", None)
                    sender = getattr(event, "sender", None)
                    sender_id = getattr(sender, "sender_id", None)
                    content = json.loads(str(getattr(message, "content", "") or "{}"))
                    text = re.sub(r"@_user_\d+\s*", "", str(content.get("text") or "")).strip()
                    event_id = str(getattr(header, "event_id", "") or uuid.uuid4().hex)
                    threading.Thread(
                        target=self._process_message_safely,
                        kwargs={
                            "event_id": event_id,
                            "sender_open_id": str(getattr(sender_id, "open_id", "") or ""),
                            "chat_id": str(getattr(message, "chat_id", "") or ""),
                            "chat_type": str(getattr(message, "chat_type", "") or ""),
                            "text": text,
                            "message_id": str(getattr(message, "message_id", "") or ""),
                        },
                        daemon=True,
                        name="qcsckp-v1a-feishu-message",
                    ).start()

                def card_handler(data: Any) -> Any:
                    header = getattr(data, "header", None)
                    event = getattr(data, "event", None)
                    context = getattr(event, "context", None)
                    operator = getattr(event, "operator", None)
                    action = getattr(event, "action", None)
                    value = getattr(action, "value", None)
                    if not isinstance(value, dict):
                        value = {}
                    else:
                        value = dict(value)
                    form_value = getattr(action, "form_value", None)
                    if isinstance(form_value, dict):
                        value["form_value"] = form_value
                    event_id = str(getattr(header, "event_id", "") or uuid.uuid4().hex)
                    threading.Thread(
                        target=self._process_card_safely,
                        kwargs={
                            "event_id": event_id,
                            "operator_open_id": str(getattr(operator, "open_id", "") or ""),
                            "message_id": str(getattr(context, "open_message_id", "") or ""),
                            "value": value,
                        },
                        daemon=True,
                        name="qcsckp-v1a-feishu-card",
                    ).start()
                    return P2CardActionTriggerResponse(
                        {"toast": {"type": "info", "content": "V1A模拟请求已收到"}}
                    )

                dispatcher = (
                    lark.EventDispatcherHandler.builder("", "", LogLevel.CRITICAL)
                    .register_p2_im_message_receive_v1(message_handler)
                    .register_p2_card_action_trigger(card_handler)
                    .build()
                )
                self._client = WsClient(
                    self.app_id,
                    self.app_secret,
                    log_level=LogLevel.CRITICAL,
                    event_handler=dispatcher,
                )
                self._client.on_reconnecting = lambda: self._set_transport("reconnecting")
                self._client.on_reconnected = lambda: self._set_transport("connected")
                # SDK 的 start() 在连接后阻塞；由监控线程检查真实 _conn，禁止
                # 在握手前把“连接中”误报为“已连接”。
                self._start_connection_monitor()
                self._client.start()
            except Exception as exc:
                self.service.writer.execute(
                    "UPDATE feishu_profile SET transport_status='error', last_error_code=?, last_error_message=?, updated_at=? WHERE tool_user_id=?",
                    (type(exc).__name__, sanitize_exception_text(exc, 500), utc_iso(), self.tool_user_id),
                )

        self._thread = threading.Thread(
            target=entry,
            daemon=True,
            name="qcsckp-v1a-feishu-ws",
        )
        self._thread.start()

    def _start_connection_monitor(self) -> None:
        def monitor() -> None:
            last_state = "connecting"
            while not self._stop.wait(0.5):
                client = self._client
                if client is None:
                    return
                state = "connected" if getattr(client, "_conn", None) is not None else "connecting"
                if state != last_state:
                    self._set_transport(state)
                    last_state = state

        self._monitor_thread = threading.Thread(
            target=monitor,
            daemon=True,
            name="qcsckp-v1a-feishu-ws-monitor",
        )
        self._monitor_thread.start()

    def _set_transport(self, state: str) -> None:
        self.service.writer.execute(
            "UPDATE feishu_profile SET transport_status=?, updated_at=? WHERE tool_user_id=?",
            (state, utc_iso(), self.tool_user_id),
        )

    def _process_message_safely(self, **kwargs) -> None:
        try:
            result = self.service.process_message(self.tool_user_id, **kwargs)
            action = result.get("action")
            if action in {"personal_bound", "group_bound"}:
                profile, secret = self.service._profile_with_secret(self.tool_user_id)
                client = FeishuApiClient(str(profile["app_id"]), secret)
                client.send_text(
                    "chat_id",
                    kwargs["chat_id"],
                    "绑定成功。V1A只发送模拟预览，不执行任何千川操作。",
                )
        except Exception:
            pass

    def _process_card_safely(self, **kwargs) -> None:
        try:
            result = self.service.process_card_action(self.tool_user_id, **kwargs)
            if result.get("updated_card"):
                self.service.deliver_outbox_once(self.tool_user_id)
        except Exception:
            event_id = str(kwargs.get("event_id") or "")
            row = self.service.database.query_one(
                "SELECT status FROM feishu_inbox WHERE tool_user_id=? AND event_id=?",
                (self.tool_user_id, event_id),
            )
            if row and row.get("status") == "received":
                self.service._mark_inbox(self.tool_user_id, event_id, "failed")

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        self._client = None
        if client is not None:
            try:
                loop_module = __import__("lark_oapi.ws.client", fromlist=["loop"])
                loop = getattr(loop_module, "loop", None)
                disconnect = getattr(client, "_disconnect", None)
                if callable(disconnect) and loop is not None:
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(disconnect(), loop).result(timeout=2)
                    elif not loop.is_closed():
                        loop.run_until_complete(disconnect())
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)
        self._monitor_thread = None
        self.service.writer.execute(
            "UPDATE feishu_profile SET transport_status='disconnected', updated_at=? WHERE tool_user_id=?",
            (utc_iso(), self.tool_user_id),
        )
