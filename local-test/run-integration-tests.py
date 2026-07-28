#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 PHP/MariaDB/飞书回调链路的自动化验收。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pymysql


API_BASE = os.environ.get("QCSCKP_INTEGRATION_API_BASE", "http://127.0.0.1:8787")
CALLBACK_URL = os.environ.get(
    "QCSCKP_INTEGRATION_CALLBACK_URL",
    "http://127.0.0.1:8788/api/feishu/card_callback.php",
)


@dataclass
class ApiResponse:
    status: int
    body: Dict[str, Any]


def request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> ApiResponse:
    raw = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    all_headers = {"Content-Type": "application/json; charset=utf-8"}
    all_headers.update(headers or {})
    request = urllib.request.Request(url, data=raw, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
            return ApiResponse(response.status, json.loads(data.decode("utf-8")))
    except urllib.error.HTTPError as error:
        data = error.read()
        try:
            body = json.loads(data.decode("utf-8"))
        except Exception:
            body = {"raw": data.decode("utf-8", "replace")}
        return ApiResponse(error.code, body)


def signed_callback(
    secrets: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    timestamp: Optional[Any] = None,
    signature_override: str = "",
) -> ApiResponse:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts = str(timestamp if timestamp is not None else int(time.time()))
    nonce = uuid.uuid4().hex
    is_new_card_callback = "encrypt" in payload or "schema" in payload
    secret = (
        secrets["feishu_app"]["encrypt_key"]
        if is_new_card_callback
        else secrets["feishu_app"]["verification_token"]
    )
    digest = hashlib.sha256 if is_new_card_callback else hashlib.sha1
    signature = digest(
        ts.encode("utf-8")
        + nonce.encode("utf-8")
        + str(secret).encode("utf-8")
        + raw
    ).hexdigest()
    if signature_override:
        signature = signature_override
    request = urllib.request.Request(
        CALLBACK_URL,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return ApiResponse(
                response.status, json.loads(response.read().decode("utf-8"))
            )
    except urllib.error.HTTPError as error:
        raw_error = error.read()
        return ApiResponse(error.code, json.loads(raw_error.decode("utf-8")))


class LocalIntegrationSuite:
    def __init__(self, runtime_root: str):
        self.runtime_root = os.path.abspath(runtime_root)
        secret_file = os.path.join(self.runtime_root, "secrets.local.json")
        with open(secret_file, "r", encoding="utf-8-sig") as handle:
            self.secrets = json.load(handle)
        db = self.secrets["db"]
        self.connection = pymysql.connect(
            host=db["host"],
            port=int(db["port"]),
            user=db["user"],
            password=db["pass"],
            database=db["name"],
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
        self.token = ""
        self.second_token = ""
        self.checks = 0

    def close(self) -> None:
        self.connection.close()

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)
        self.checks += 1
        print(f"[OK] {message}")

    def db_one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    def db_execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connection.cursor() as cursor:
            return cursor.execute(sql, params)

    def reset(self) -> None:
        for table in (
            "retarget_card_update_jobs",
            "retarget_task_messages",
            "retarget_tasks",
            "desktop_device_sessions",
        ):
            exists = self.db_one("SHOW TABLES LIKE %s", (table,))
            if exists:
                self.db_execute(f"DELETE FROM `{table}`")
        self.db_execute("DELETE FROM accounts WHERE username='local_test_2'")

    def login(self, username: str, password: str, device: str) -> ApiResponse:
        return request_json(
            "POST",
            f"{API_BASE}/api/device/session.php",
            {"username": username, "password": password, "device_name": device},
        )

    def authorized(self, token: Optional[str] = None) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def create_task(
        self,
        suffix: str,
        *,
        token: Optional[str] = None,
        aavid: str = "10001",
        material_id: str = "20001",
        material_ids: Optional[List[str]] = None,
        target_uid: str = "target_local_live_30001",
        promotion_scene: str = "live",
        plan_system: str = "global",
        trigger_level: str = "material",
        product_id: str = "",
        expect_success: bool = True,
    ) -> Dict[str, Any]:
        selected_material_ids = material_ids or [material_id]
        materials = [
            {
                "material_id": current_id,
                "material_name": f"本地素材{current_id}",
                "product_id": product_id,
                "product_name": "本地测试商品" if product_id else "",
                "product_ids": [product_id] if product_id else [],
            }
            for current_id in selected_material_ids
        ]
        snapshot = {
            "id": f"strategy-{suffix}",
            "title": f"本地策略{suffix}",
            "target_uid": target_uid,
            "trigger_level": trigger_level,
            "product_filter": [],
            "candidate_trigger": {"groups": []},
            "candidate_sort": "net_roi_desc",
            "candidate_limit": 20,
            "action_mode": "card_confirm",
            "trigger": {"groups": []},
            "retargeting": {
                "method": "volume",
                "volume": {"total_budget_yuan": 100, "duration_hours": 0.5},
            },
        }
        strategy_hash = hashlib.sha256(
            json.dumps(
                snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        response = request_json(
            "POST",
            f"{API_BASE}/api/retarget_tasks/create.php",
            {
                "aavid": aavid,
                "account_name": f"本地账户{aavid}",
                "ad_id": "30001",
                "target_uid": target_uid,
                "plan_name": "本地测试计划",
                "promotion_scene": promotion_scene,
                "plan_system": plan_system,
                "trigger_level": trigger_level,
                "product_id": product_id,
                "product_name": "本地测试商品" if product_id else "",
                "material_id": material_id,
                "material_name": f"本地素材{material_id}",
                "materials": materials,
                "strategy_id": snapshot["id"],
                "strategy_name": snapshot["title"],
                "strategy_hash": strategy_hash,
                "trigger_snapshot": {"strategy_title": snapshot["title"]},
                "query_snapshot": {"period": "1h"},
                "retargeting": snapshot["retargeting"],
                "rule_snapshot": snapshot,
            },
            self.authorized(token),
        )
        if expect_success:
            self.check(
                response.status == 200 and response.body.get("success"),
                f"create task {suffix}",
            )
        return response.body

    def task_row(self, task_uid: str) -> Dict[str, Any]:
        row = self.db_one("SELECT * FROM retarget_tasks WHERE task_uid=%s", (task_uid,))
        if not row:
            raise AssertionError(f"task not found: {task_uid}")
        return row

    def callback(
        self,
        task_uid: str,
        nonce: str,
        action: str,
        *,
        operator: Optional[str] = None,
    ) -> ApiResponse:
        return signed_callback(
            self.secrets,
            {
                "token": self.secrets["feishu_app"]["verification_token"],
                "event": {
                    "operator": {
                        "operator_id": {
                            "open_id": operator
                            or self.secrets["feishu_app"]["authorized_open_id"]
                        }
                    },
                    "action": {
                        "value": {
                            "task_uid": task_uid,
                            "nonce": nonce,
                            "action": action,
                        }
                    },
                },
            },
        )

    def schema_callback(
        self,
        task_uid: str,
        nonce: str,
        action: str,
        *,
        callback_token: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> ApiResponse:
        payload: Dict[str, Any] = {
            "schema": "2.0",
            "event": {
                "operator": {
                    "operator_id": {
                        "open_id": self.secrets["feishu_app"]["authorized_open_id"]
                        if operator is None
                        else operator
                    }
                },
                "action": {
                    "value": {
                        "task_uid": task_uid,
                        "nonce": nonce,
                        "action": action,
                    }
                },
            },
        }
        if callback_token is not None:
            payload["header"] = {"token": callback_token}
        return signed_callback(self.secrets, payload)

    def approve(self, task_uid: str) -> str:
        row = self.task_row(task_uid)
        response = self.schema_callback(task_uid, row["action_nonce"], "approve")
        self.check(response.body.get("toast", {}).get("type") == "success", "authorized approval")
        self.check(
            response.body.get("card", {}).get("data", {}).get("header", {}).get("template") == "blue",
            "approval response replaces clicked card immediately",
        )
        self.check(self.task_row(task_uid)["status"] == "approved_queued", "approval queued")
        queued = self.db_one(
            "SELECT COUNT(*) AS total FROM retarget_card_update_jobs WHERE task_id=(SELECT id FROM retarget_tasks WHERE task_uid=%s)",
            (task_uid,),
        )
        self.check(queued["total"] == 1, "other card updates are queued outside the callback")
        return row["action_nonce"]

    def pull(self, token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        response = request_json(
            "GET",
            f"{API_BASE}/api/retarget_tasks/pull.php",
            headers=self.authorized(token),
        )
        self.check(response.status == 200 and response.body.get("success"), "pull endpoint")
        return response.body.get("data")

    def result(
        self,
        task: Dict[str, Any],
        status: str,
        *,
        claim_token: Optional[str] = None,
    ) -> ApiResponse:
        return request_json(
            "POST",
            f"{API_BASE}/api/retarget_tasks/result.php",
            {
                "task_uid": task["task_uid"],
                "claim_token": claim_token or task["claim_token"],
                "status": status,
                "message": f"mock {status}",
                "regulate_task_id": "mock-regulate-1" if status == "succeeded" else "",
                "result": {"mock": True, "status": status},
            },
            self.authorized(),
        )

    def run(self) -> None:
        self.reset()
        account = self.secrets["test_account"]
        bad = self.login(account["username"], "wrong-password", "bad-device")
        self.check(bad.status == 401, "invalid local password rejected")
        login = self.login(account["username"], account["password"], "integration-device")
        self.check(login.status == 200 and login.body.get("success"), "device token issued")
        self.token = login.body["data"]["token"]

        challenge = request_json(
            "POST",
            CALLBACK_URL,
            {
                "type": "url_verification",
                "token": self.secrets["feishu_app"]["verification_token"],
                "challenge": "local-challenge",
            },
        )
        self.check(
            challenge.body.get("challenge") == "local-challenge",
            "unsigned callback URL verification",
        )
        bad_challenge = request_json(
            "POST",
            CALLBACK_URL,
            {
                "type": "url_verification",
                "token": "wrong-verification-token",
                "challenge": "must-not-return",
            },
        )
        self.check(
            bad_challenge.status == 403,
            "unsigned callback URL verification token required",
        )
        forged = signed_callback(
            self.secrets,
            {"token": self.secrets["feishu_app"]["verification_token"]},
            signature_override="0" * 64,
        )
        self.check(forged.status == 403, "forged callback signature rejected")
        stale = signed_callback(
            self.secrets,
            {"token": self.secrets["feishu_app"]["verification_token"]},
            timestamp=int(time.time()) - 600,
        )
        self.check(
            stale.status == 200
            and stale.body.get("toast", {}).get("type") == "error",
            "local clock-skew allowance still requires an authorized operator",
        )
        opaque_timestamp = signed_callback(
            self.secrets,
            {"token": self.secrets["feishu_app"]["verification_token"]},
            timestamp="feishu-signed-opaque-timestamp",
        )
        self.check(
            opaque_timestamp.status == 200
            and opaque_timestamp.body.get("toast", {}).get("type") == "error",
            "signed opaque callback timestamp is accepted before operator authorization",
        )
        other_path = request_json("POST", "http://127.0.0.1:8788/api/device/session.php", {})
        self.check(other_path.status == 404, "tunnel proxy exposes callback path only")

        too_many = self.create_task(
            "too-many",
            material_ids=[f"material-{index}" for index in range(21)],
            target_uid="target_local_product_30001",
            promotion_scene="product",
            expect_success=False,
        )
        self.check(
            too_many.get("success") is False
            and "最多支持20条素材" in str(too_many.get("message") or ""),
            "more than twenty materials are rejected",
        )

        created = self.create_task(
            "security",
            material_ids=["20001", "20002", "20003"],
            target_uid="target_local_product_30001",
            promotion_scene="product",
        )
        task_uid = created["data"]["task_uid"]
        row = self.task_row(task_uid)
        message_count = self.db_one(
            "SELECT COUNT(*) AS total FROM retarget_task_messages WHERE task_id=%s",
            (row["id"],),
        )
        self.check(message_count["total"] == 2, "group and personal mock cards share one task")
        duplicate = self.create_task(
            "security",
            material_ids=["29999"],
            target_uid="target_local_product_30001",
            promotion_scene="product",
        )
        self.check(duplicate.get("duplicate") is True, "duplicate trigger is idempotent")
        self.check(duplicate["data"]["task_uid"] == task_uid, "duplicate returns original task")

        unauthorized = self.schema_callback(
            task_uid, row["action_nonce"], "approve", operator="not-authorized"
        )
        self.check(
            unauthorized.body.get("toast", {}).get("type") == "error",
            "unauthorized Feishu user rejected",
        )
        self.check(self.task_row(task_uid)["status"] == "pending", "unauthorized click has no effect")
        tampered = self.schema_callback(task_uid, "f" * 64, "approve")
        self.check(
            tampered.body.get("toast", {}).get("type") == "error",
            "tampered action nonce rejected",
        )
        detail = self.schema_callback(task_uid, row["action_nonce"], "view")
        self.check(
            detail.status == 200
            and detail.body.get("card", {}).get("type") == "raw"
            and isinstance(detail.body.get("card", {}).get("data", {}).get("header"), dict)
            and isinstance(detail.body.get("card", {}).get("data", {}).get("elements"), list),
            "schema callback without token returns wrapped raw detail card",
        )
        card_text = json.dumps(
            detail.body.get("card", {}).get("data", {}),
            ensure_ascii=False,
        )
        self.check(
            all(
                expected in card_text
                for expected in [
                    "本地账户10001",
                    "账户ID",
                    "本地测试计划",
                    "计划ID",
                    "推商品",
                    "全域",
                    "本卡追投素材（3条）",
                    "本地素材20001",
                    "本地素材20002",
                    "本地素材20003",
                    "素材ID",
                ]
            ),
            "one card shows account, plan, scene, system, and all materials",
        )
        wrong_callback_token = self.schema_callback(
            task_uid,
            row["action_nonce"],
            "view",
            callback_token="wrong-verification-token",
        )
        self.check(
            wrong_callback_token.status == 200
            and wrong_callback_token.body.get("card", {}).get("type") == "raw",
            "signed schema callback does not reuse its body token as a signature secret",
        )
        self.check(
            self.task_row(task_uid)["status"] == "pending",
            "viewing task detail has no execution side effect",
        )
        self.approve(task_uid)
        repeated = self.schema_callback(task_uid, row["action_nonce"], "approve")
        self.check(
            repeated.body.get("toast", {}).get("type") == "info",
            "repeated approval does not execute twice",
        )
        claimed = self.pull()
        self.check(claimed and claimed["task_uid"] == task_uid, "approved task claimed")
        self.check(
            [item["material_id"] for item in claimed.get("materials", [])]
            == ["20001", "20002", "20003"],
            "one claimed task carries all card materials",
        )
        wrong_lease = self.result(claimed, "executing", claim_token="0" * 64)
        self.check(wrong_lease.status == 409, "tampered lease token rejected")
        executing = self.result(claimed, "executing")
        self.check(executing.status == 200, "claimed task enters executing")
        succeeded = self.result(claimed, "succeeded")
        self.check(succeeded.status == 200, "mock task succeeds")
        self.check(
            self.task_row(task_uid)["regulate_task_id"] == "mock-regulate-1",
            "regulation task id stored",
        )
        repeat_result = self.result(claimed, "succeeded")
        self.check(repeat_result.body.get("duplicate") is True, "result report is idempotent")

        reject_created = self.create_task("reject")
        reject_uid = reject_created["data"]["task_uid"]
        reject_row = self.task_row(reject_uid)
        rejected = self.schema_callback(reject_uid, reject_row["action_nonce"], "reject")
        self.check(rejected.body.get("toast", {}).get("type") == "success", "task rejected")
        self.check(self.task_row(reject_uid)["status"] == "rejected", "rejected state stored")

        expired_created = self.create_task("expire")
        expired_uid = expired_created["data"]["task_uid"]
        self.db_execute(
            "UPDATE retarget_tasks SET expires_at=DATE_SUB(NOW(),INTERVAL 1 MINUTE) WHERE task_uid=%s",
            (expired_uid,),
        )
        self.pull()
        self.check(self.task_row(expired_uid)["status"] == "expired", "expired task not executed")

        offline_created = self.create_task("offline")
        offline_uid = offline_created["data"]["task_uid"]
        self.approve(offline_uid)
        self.check(
            self.task_row(offline_uid)["status"] == "approved_queued",
            "offline approval remains queued",
        )
        offline_claim = self.pull()
        self.check(offline_claim["task_uid"] == offline_uid, "offline task later claimed")
        self.check(self.result(offline_claim, "failed").status == 200, "offline task finalized")

        recovery_created = self.create_task("claimed-recovery")
        recovery_uid = recovery_created["data"]["task_uid"]
        self.approve(recovery_uid)
        first_claim = self.pull()
        first_token = first_claim["claim_token"]
        self.db_execute(
            "UPDATE retarget_tasks SET lease_expires_at=DATE_SUB(NOW(),INTERVAL 1 MINUTE) WHERE task_uid=%s",
            (recovery_uid,),
        )
        second_claim = self.pull()
        self.check(second_claim["task_uid"] == recovery_uid, "expired claim lease recovered")
        self.check(second_claim["claim_token"] != first_token, "recovered claim uses new lease token")
        self.check(self.result(second_claim, "failed").status == 200, "recovered task finalized")

        interrupted_created = self.create_task("executing-interrupt")
        interrupted_uid = interrupted_created["data"]["task_uid"]
        self.approve(interrupted_uid)
        interrupted_claim = self.pull()
        self.check(self.result(interrupted_claim, "executing").status == 200, "execution lease started")
        self.db_execute(
            "UPDATE retarget_tasks SET lease_expires_at=DATE_SUB(NOW(),INTERVAL 1 MINUTE) WHERE task_uid=%s",
            (interrupted_uid,),
        )
        self.pull()
        self.check(
            self.task_row(interrupted_uid)["status"] == "failed",
            "interrupted execution is not automatically retried",
        )

        password_row = self.db_one(
            "SELECT password_hash FROM accounts WHERE username=%s", (account["username"],)
        )
        self.db_execute(
            "INSERT INTO accounts(username,password_hash,role,valid_from,valid_until,is_disabled) "
            "VALUES('local_test_2',%s,'user',NOW(),DATE_ADD(NOW(),INTERVAL 365 DAY),0)",
            (password_row["password_hash"],),
        )
        second_login = self.login("local_test_2", account["password"], "integration-device-2")
        self.check(second_login.status == 200, "second isolated test account logged in")
        self.second_token = second_login.body["data"]["token"]
        isolated_created = self.create_task(
            "account-isolation",
            token=self.second_token,
            aavid="99999",
            material_id="88888",
        )
        isolated_uid = isolated_created["data"]["task_uid"]
        self.approve(isolated_uid)
        self.check(self.pull() is None, "first account cannot pull second account task")
        second_task = self.pull(self.second_token)
        self.check(second_task["task_uid"] == isolated_uid, "second account pulls its own task")
        second_result = request_json(
            "POST",
            f"{API_BASE}/api/retarget_tasks/result.php",
            {
                "task_uid": second_task["task_uid"],
                "claim_token": second_task["claim_token"],
                "status": "failed",
                "message": "account isolation complete",
            },
            self.authorized(self.second_token),
        )
        self.check(second_result.status == 200, "second account result isolated")


def main() -> None:
    global API_BASE, CALLBACK_URL
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-root",
        default=os.path.join(os.environ["LOCALAPPDATA"], "qcsckp-test-runtime"),
    )
    parser.add_argument("--api-base", default=API_BASE)
    parser.add_argument("--callback-url", default=CALLBACK_URL)
    args = parser.parse_args()
    API_BASE = args.api_base.rstrip("/")
    CALLBACK_URL = args.callback_url
    suite = LocalIntegrationSuite(args.runtime_root)
    try:
        suite.run()
        print(f"\nAll {suite.checks} local integration checks passed.")
    finally:
        suite.close()


if __name__ == "__main__":
    main()
