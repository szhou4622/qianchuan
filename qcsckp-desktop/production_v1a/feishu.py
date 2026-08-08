"""飞书长连接、持久化 Inbox/Outbox、绑定码和 V1A 模拟卡。"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    materials = json.loads(str(batch["material_snapshot_json"]))
    total_pages = max(1, (len(materials) + page_size - 1) // page_size)
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    visible = materials[start : start + page_size]
    system_label = {"global": "全域", "chengfang": "乘方"}.get(plan_system, "待确认")
    scene_label = {"product": "推商品", "live": "推直播"}.get(
        promotion_scene, "待确认"
    )
    lines = [
        f"**账户：** {account_name}",
        f"**账户 ID：** {batch['aavid']}",
        f"**计划：** {plan_name}",
        f"**计划类型：** {system_label} · {scene_label}",
        f"**候选素材：** {len(materials)} 条，当前第 {page} 页",
        f"**有效期：** {batch['expires_at']}",
    ]
    material_lines = []
    for material in visible:
        material_id = str(material["material_id"])
        material_lines.append(
            f"{material['sequence']}. {material['material_name']}\n素材 ID：{material_id}"
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
                "content": "\n\n".join(material_lines) or "当前页没有素材",
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "分组保存后只生成 dry-run 结果和模拟审计，不会提交千川。",
                    }
                ],
            },
            {
                "tag": "form",
                "name": "candidate_group_form",
                "elements": [
                    {
                        "tag": "multi_select_static",
                        "name": "material_ids",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "选择本页素材，可重复提交形成多个组",
                        },
                        "options": [
                            {
                                "text": {
                                    "tag": "plain_text",
                                    "content": f"{item['sequence']}. {str(item['material_name'])[:32]}",
                                },
                                "value": str(item["material_id"]),
                            }
                            for item in visible
                        ],
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "所选素材为一组"},
                        "type": "primary",
                        "action_type": "form_submit",
                        "value": {
                            "action": "v1a_selected_group",
                            "candidate_batch_id": batch["candidate_batch_id"],
                            "page": page,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "全部一组（总数≤20）"},
                        "value": {
                            "action": "v1a_all_group",
                            "candidate_batch_id": batch["candidate_batch_id"],
                            "page": page,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "逐条分别模拟"},
                        "value": {
                            "action": "v1a_single_each",
                            "candidate_batch_id": batch["candidate_batch_id"],
                            "page": page,
                        },
                    },
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
                                        "text": {"tag": "plain_text", "content": "上一页"},
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
                                        "text": {"tag": "plain_text", "content": "下一页"},
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
                        "text": {"tag": "plain_text", "content": "查看任务中心 / 继续多组"},
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
                (type(exc).__name__, str(exc)[:500], utc_iso(), tool_user_id),
            )
            return {"valid": False, "error": str(exc)}

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

        def op(conn):
            with short_transaction(conn):
                try:
                    conn.execute(
                        """
                        INSERT INTO feishu_inbox(
                            event_id, tool_user_id, event_type, sender_open_id,
                            message_id, received_at, status, payload_hash
                        ) VALUES(?, ?, ?, ?, ?, ?, 'received', ?)
                        """,
                        (
                            event_id,
                            tool_user_id,
                            event_type,
                            sender_open_id,
                            message_id,
                            now,
                            stable_json_hash(payload),
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
            "chat_id": chat_id,
            "chat_type": chat_type,
            "text_hash": stable_json_hash(text),
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
        if not self.ingest_event(
            tool_user_id=tool_user_id,
            event_id=event_id,
            event_type="card.action.trigger",
            sender_open_id=operator_open_id,
            message_id=message_id,
            payload=value,
        ):
            return {"processed": False, "reason": "duplicate_event"}
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
        visible_ids = [str(row["material_id"]) for row in page_data["materials"]]
        updated_card = None
        if action in {"v1a_previous_page", "v1a_next_page"}:
            target = self.database.query_one(
                "SELECT * FROM source_plan WHERE target_uid=?",
                (page_data["batch"]["target_uid"],),
            )
            account = self.database.query_one(
                "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=?",
                (tool_user_id, page_data["batch"]["aavid"]),
            )
            if not target or not account:
                raise FeishuError("候选关联的账户或计划不存在")
            destination = page - 1 if action == "v1a_previous_page" else page + 1
            destination = min(max(destination, 1), int(page_data["total_pages"]))
            updated_card = build_candidate_preview_card(
                page_data["batch"],
                account_name=str(account["account_name"]),
                plan_name=str(target["plan_name"]),
                plan_system=str(target["plan_system"]),
                promotion_scene=str(target["promotion_scene"]),
                page=destination,
            )
            groups = []
        elif action == "v1a_selected_group":
            form = value.get("form_value") if isinstance(value.get("form_value"), dict) else {}
            selected = value.get("material_ids") or form.get("material_ids") or []
            if isinstance(selected, str):
                selected = [selected]
            selected_ids = [str(item) for item in selected]
            if not selected_ids or len(selected_ids) > 20 or any(
                item not in visible_ids for item in selected_ids
            ):
                raise FeishuError("请选择当前页中的1至20条素材")
            groups = self.candidates.save_groups(
                tool_user_id,
                batch_id,
                [{"mode": "selected_group", "material_ids": selected_ids}],
                created_by_open_id=operator_open_id,
            )
        elif action == "v1a_all_group":
            all_ids = [
                str(row["material_id"])
                for row in json.loads(str(page_data["batch"]["material_snapshot_json"]))
            ]
            if len(all_ids) > 20:
                raise FeishuError("候选超过20条，请在任务中心按页选择分组")
            groups = self.candidates.save_groups(
                tool_user_id,
                batch_id,
                [{"mode": "all_group", "material_ids": all_ids}],
                created_by_open_id=operator_open_id,
            )
        elif action == "v1a_single_each":
            groups = self.candidates.save_groups(
                tool_user_id,
                batch_id,
                [{"mode": "single_each", "material_ids": visible_ids}],
                created_by_open_id=operator_open_id,
            )
        elif action == "v1a_view_task_center":
            groups = []
        else:
            raise FeishuError("未知的 V1A 模拟动作")
        self._mark_inbox(tool_user_id, event_id, "processed")
        return {
            "processed": True,
            "action": action,
            "group_uids": groups,
            "dry_run": True,
            "updated_card": updated_card,
        }

    def enqueue_candidate_preview(self, tool_user_id: str, batch_id: str) -> list[str]:
        batch = self.database.query_one(
            "SELECT * FROM candidate_batch WHERE tool_user_id=? AND candidate_batch_id=?",
            (tool_user_id, batch_id),
        )
        if not batch:
            raise KeyError(batch_id)
        target = self.database.query_one(
            "SELECT * FROM source_plan WHERE target_uid=?", (batch["target_uid"],)
        )
        account = self.database.query_one(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=?",
            (tool_user_id, batch["aavid"]),
        )
        if not target or not account:
            raise FeishuError("候选账户或计划不存在")
        route_id = account.get("feishu_route_id")
        if not route_id:
            route = self.database.query_one(
                "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_name='管理员默认位置' AND enabled=1",
                (tool_user_id,),
            )
        else:
            route = self.database.query_one(
                "SELECT * FROM feishu_route WHERE tool_user_id=? AND route_id=? AND enabled=1",
                (tool_user_id, route_id),
            )
        if not route:
            raise FeishuError("该账户尚未配置飞书接收位置")
        card = build_candidate_preview_card(
            batch,
            account_name=str(account["account_name"]),
            plan_name=str(target["plan_name"]),
            plan_system=str(target["plan_system"]),
            promotion_scene=str(target["promotion_scene"]),
        )
        targets: list[tuple[str, str]] = []
        if route.get("personal_open_id"):
            targets.append(("open_id", str(route["personal_open_id"])))
        for chat_id in json.loads(str(route.get("group_chat_ids_json") or "[]")):
            targets.append(("chat_id", str(chat_id)))
        now = utc_iso()
        outbox_ids = []
        for receive_type, receive_id in targets:
            outbox_id = f"outbox_{uuid.uuid4().hex}"
            delivery_route = f"{receive_type}:{receive_id}"
            self.writer.execute(
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
                    batch_id,
                    json.dumps(
                        {
                            "receive_type": receive_type,
                            "receive_id": receive_id,
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
            outbox_ids.append(outbox_id)
        if not targets:
            raise FeishuError("飞书接收位置为空")
        self.writer.execute(
            "UPDATE candidate_batch SET status='pending_approval', updated_at=? WHERE candidate_batch_id=? AND status='frozen'",
            (now, batch_id),
        )
        return outbox_ids

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
            "SELECT * FROM source_plan WHERE target_uid=?", (candidate["target_uid"],)
        )
        account = self.database.query_one(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=?",
            (tool_user_id, candidate["aavid"]),
        )
        task = self.database.query_one(
            "SELECT * FROM platform_control_task WHERE control_task_uid=?",
            (candidate["control_task_uid"],),
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
            self.writer.execute(
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
                    f"{receive_type}:{receive_id}",
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
            outbox_ids.append(outbox_id)
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
            self.writer.execute(
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
            outbox_ids.append(outbox_id)
        return outbox_ids

    def deliver_outbox_once(self, tool_user_id: str, limit: int = 20) -> int:
        rows = self.database.query_all(
            """
            SELECT * FROM feishu_outbox
            WHERE tool_user_id=? AND status IN ('queued', 'retry')
              AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY created_at ASC LIMIT ?
            """,
            (tool_user_id, utc_iso(), limit),
        )
        if not rows:
            return 0
        profile, secret = self._profile_with_secret(tool_user_id)
        client = FeishuApiClient(str(profile["app_id"]), secret)
        sent = 0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            try:
                message_id = client.send_card(
                    payload["receive_type"], payload["receive_id"], payload["card"]
                )
                self.writer.execute(
                    "UPDATE feishu_outbox SET status='sent', message_id=?, attempt_count=attempt_count+1, sent_at=?, updated_at=? WHERE outbox_id=?",
                    (message_id, utc_iso(), utc_iso(), row["outbox_id"]),
                )
                sent += 1
            except Exception as exc:
                attempts = int(row["attempt_count"] or 0) + 1
                status = "failed" if attempts >= 6 else "retry"
                delay = min(300, 2**attempts)
                self.writer.execute(
                    "UPDATE feishu_outbox SET status=?, attempt_count=?, next_attempt_at=?, updated_at=? WHERE outbox_id=?",
                    (
                        status,
                        attempts,
                        utc_iso(utc_now() + timedelta(seconds=delay)),
                        utc_iso(),
                        row["outbox_id"],
                    ),
                )
                self.writer.execute(
                    "UPDATE feishu_profile SET send_status='error', last_error_code=?, last_error_message=?, updated_at=? WHERE tool_user_id=?",
                    (type(exc).__name__, str(exc)[:500], utc_iso(), tool_user_id),
                )
        if sent:
            self.writer.execute(
                "UPDATE feishu_profile SET send_status='ready', updated_at=? WHERE tool_user_id=?",
                (utc_iso(), tool_user_id),
            )
            for task_uid in {
                str(row.get("task_uid") or "")
                for row in rows
                if str(row.get("task_uid") or "").startswith("report_")
            }:
                pending = self.database.query_one(
                    "SELECT 1 FROM feishu_outbox WHERE tool_user_id=? AND task_uid=? AND status!='sent' LIMIT 1",
                    (tool_user_id, task_uid),
                )
                if not pending:
                    self.writer.execute(
                        "UPDATE daily_report_delivery SET status='sent', sent_at=?, updated_at=? WHERE report_uid=?",
                        (utc_iso(), utc_iso(), task_uid),
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
                    (type(exc).__name__, str(exc)[:500], utc_iso(), self.tool_user_id),
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
            updated_card = result.get("updated_card")
            message_id = str(kwargs.get("message_id") or "")
            if updated_card and message_id:
                profile, secret = self.service._profile_with_secret(self.tool_user_id)
                FeishuApiClient(str(profile["app_id"]), secret).update_card(
                    message_id, updated_card
                )
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
