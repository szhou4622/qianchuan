# -*- coding: utf-8 -*-
import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from api.operation_events import (
    ingest_platform_log_rows,
    operation_event_account_summary,
)
from services.operation_log_monitor import (
    _enabled_log_account_ids,
    _extract_platform_rows,
    _fetch_platform_log_payload,
    _prepare_platform_log_page,
    _sync_account_platform_logs_unlocked,
)
from services.qianchuan_accounts import ensure_qianchuan_account
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class _FakePage:
    def __init__(self):
        self.url = "https://qianchuan.jinritemai.com/"

    async def goto(self, url, **_kwargs):
        self.url = url

    def get_by_text(self, *_args, **_kwargs):
        return self

    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None

    async def wait_for_timeout(self, _milliseconds):
        return None


class _FakeFetcher:
    def __init__(self, **_kwargs):
        self.page = None
        self.closed = False

    async def _init_browser(self):
        self.page = _FakePage()

    async def close(self):
        self.closed = True


class OperationLogSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = SQLiteStore(
            database=os.path.join(self.temp.name, "operation-log.db")
        )
        init_sqlite_schema(database=self.db.config["database"])

    def tearDown(self):
        self.temp.cleanup()

    def _account(self, aavid="1001", *, enabled=True, report_enabled=False):
        account = ensure_qianchuan_account(
            aavid,
            account_name=f"账户{aavid}",
            owner_username="tool-owner",
            enabled=enabled,
            seen=True,
            db=self.db,
        )
        self.db.update(
            "qianchuan_account",
            {
                "enabled": 1 if enabled else 0,
                "report_enabled": 1 if report_enabled else 0,
                "directory_selected": 1,
            },
            where={"account_uid": account["account_uid"]},
        )
        return account

    def _target(self, account, ad_id="2001"):
        self.db.insert(
            "promotion_target",
            {
                "target_uid": f"target-{ad_id}",
                "account_uid": account["account_uid"],
                "aadvid": account["aavid"],
                "ad_id": ad_id,
                "plan_name": f"计划{ad_id}",
                "promotion_scene": "live",
                "plan_system": "chengfang",
                "enabled": 1,
                "verification_state": "verified",
            },
        )

    def test_enabled_accounts_sync_even_when_daily_report_is_disabled(self):
        self._account("1001", enabled=True, report_enabled=False)
        self._account("1002", enabled=False, report_enabled=True)
        self.assertEqual(
            ["1001"],
            _enabled_log_account_ids(self.db, "tool-owner"),
        )

    def test_camel_case_platform_rows_are_detected(self):
        rows = [
            {
                "logId": "log-1",
                "operateTime": "2026-08-11 09:00:00",
                "optContent": "修改预算",
                "operatorName": "测试用户",
            }
        ]
        self.assertEqual(rows, _extract_platform_rows({"data": {"list": rows}}))

    def test_transient_platform_timeout_is_retried(self):
        page = _FakePage()
        page.evaluate = AsyncMock(
            side_effect=[
                {
                    "ok": True,
                    "status": 200,
                    "contentType": "application/json",
                    "text": '{"status_code": 50001, "message": "网络超时，请稍后重试"}',
                },
                {
                    "ok": True,
                    "status": 200,
                    "contentType": "application/json",
                    "text": '{"status_code": 0, "data": {"logs": []}}',
                },
            ]
        )
        with patch(
            "services.operation_log_monitor.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            payload = asyncio.run(
                _fetch_platform_log_payload(page, "/readonly-operation-log")
            )
        self.assertEqual(0, payload["status_code"])
        self.assertEqual(2, page.evaluate.await_count)
        sleep.assert_awaited_once()

    def test_log_page_forces_detail_route_and_uses_readonly_probe(self):
        page = _FakePage()
        page.evaluate = AsyncMock(
            return_value={
                "ok": True,
                "status": 200,
                "contentType": "application/json",
                "text": '{"status_code": 0, "data": {"list": []}}',
            }
        )
        target = {
            "ad_id": "2001",
            "promotion_scene": "product",
            "sanitized_page_url": (
                "https://qianchuan.jinritemai.com/uni-prom?"
                "aavid=1001&ct=1"
            ),
        }
        asyncio.run(
            _prepare_platform_log_page(
                page,
                aavid="1001",
                target=target,
                start=datetime(2026, 8, 1),
                end=datetime(2026, 8, 11, 12),
            )
        )
        self.assertIn("/uni-prom/detail?", page.url)
        requested_url = page.evaluate.await_args.args[1]["url"]
        self.assertIn("objectID=2001", requested_url)
        self.assertIn("pageSize=1", requested_url)

    def test_paused_retarget_log_is_classified_as_stop(self):
        self._account()
        ingest_platform_log_rows(
            "1001",
            [
                {
                    "id": "platform-stop-1",
                    "aavid": "1001",
                    "ad_id": "2001",
                    "contentTitle": "修改",
                    "contentLog": [
                        "操作内容：素材追投，ID：987654321",
                        "调控状态：调控中 -> 已暂停",
                    ],
                    "objectType": "单元",
                    "objectName": "测试计划",
                    "createTime": "2026-08-11 12:00:00",
                    "status": 1,
                }
            ],
            owner_username="tool-owner",
            db=self.db,
            update_sync_state=False,
        )
        event = self.db.select_one(
            "account_operation_event",
            where={"platform_event_id": "platform-stop-1"},
        )
        self.assertEqual("stop", event["action_type"])
        self.assertEqual("987654321", event["regulate_task_id"])
        self.assertEqual("", event["material_id"])

    def test_account_summary_explains_filters_that_hide_existing_rows(self):
        self._account()
        ingest_platform_log_rows(
            "1001",
            [
                {
                    "logId": "summary-1",
                    "operateTime": "2026-08-01 09:00:00",
                    "optContent": "修改预算",
                    "operatorName": "王斌",
                }
            ],
            owner_username="tool-owner",
            db=self.db,
            update_sync_state=False,
        )
        summary = operation_event_account_summary(
            "1001",
            owner_username="tool-owner",
            db=self.db,
        )
        self.assertEqual(1, summary["total"])
        self.assertEqual("2026-08-01 09:00:00", summary["available_from"])
        self.assertEqual(["王斌"], summary["operators"])

    def test_direct_readonly_endpoint_backfills_and_persists_rows(self):
        account = self._account()
        self._target(account)
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "logId": "log-1",
                        "operateTime": "2026-08-11 09:00:00",
                        "optContent": "修改预算",
                        "operatorName": "测试用户",
                    }
                ],
                "total": 1,
                "hasMore": False,
            },
        }
        with patch(
            "services.operation_log_monitor.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.operation_log_monitor.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.operation_log_monitor.load_qianchuan_storage_state",
            return_value={"cookies": [], "origins": []},
        ), patch(
            "services.operation_log_monitor.QianChuanFetcher",
            _FakeFetcher,
        ), patch(
            "services.operation_log_monitor._fetch_platform_log_payload",
            new=AsyncMock(return_value=payload),
        ) as fetch_payload, patch(
            "api.operation_events.init_sqlite_schema",
        ):
            result = asyncio.run(
                _sync_account_platform_logs_unlocked(
                    "1001",
                    "tool-owner",
                    db=self.db,
                )
            )
        self.assertTrue(result["success"])
        self.assertTrue(result["first_backfill"])
        self.assertEqual(1, result["row_count"])
        requested_url = fetch_payload.await_args.args[1]
        self.assertTrue(requested_url.startswith("/ad/api/pmc/v1/ad/get_opt_log"))
        self.assertIn("objectID=2001", requested_url)
        self.assertIn("currentPage=1", requested_url)
        self.assertIn("pageSize=100", requested_url)
        event = self.db.select_one(
            "account_operation_event",
            where={"platform_event_id": "log-1"},
        )
        self.assertIsNotNone(event)
        self.assertEqual("platform_log", event["source"])
        self.assertEqual("budget_update", event["action_type"])
        self.assertEqual("测试用户", event["operator_name"])
        state = self.db.select_one(
            "platform_log_sync_state",
            where={"aavid": "1001"},
        )
        self.assertEqual("ok", state["last_status"])
        self.assertTrue(state["coverage_from"])
        self.assertTrue(state["coverage_to"])

    def test_log_page_preparation_falls_back_to_another_verified_plan(self):
        account = self._account()
        self._target(account, "2001")
        self._target(account, "2002")
        payload = {"code": 0, "data": {"list": [], "total": 0}}
        with patch(
            "services.operation_log_monitor.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.operation_log_monitor.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.operation_log_monitor.load_qianchuan_storage_state",
            return_value={"cookies": [], "origins": []},
        ), patch(
            "services.operation_log_monitor.QianChuanFetcher",
            _FakeFetcher,
        ), patch(
            "services.operation_log_monitor._prepare_platform_log_page",
            new=AsyncMock(side_effect=[RuntimeError("首条不可用"), None]),
        ) as prepare, patch(
            "services.operation_log_monitor._fetch_platform_log_payload",
            new=AsyncMock(return_value=payload),
        ), patch("api.operation_events.init_sqlite_schema"):
            result = asyncio.run(
                _sync_account_platform_logs_unlocked(
                    "1001",
                    "tool-owner",
                    db=self.db,
                )
            )
        self.assertTrue(result["success"])
        self.assertEqual("empty", result["status"])
        self.assertEqual(2, prepare.await_count)

    def test_incremental_sync_without_new_rows_keeps_existing_account_ok(self):
        account = self._account()
        self._target(account, "2001")
        ingest_platform_log_rows(
            "1001",
            [
                {
                    "logId": "existing-1",
                    "operateTime": "2026-08-01 09:00:00",
                    "optContent": "修改预算",
                    "operatorName": "王斌",
                }
            ],
            owner_username="tool-owner",
            db=self.db,
            update_sync_state=False,
        )
        payload = {"code": 0, "data": {"list": [], "total": 0}}
        with patch(
            "services.operation_log_monitor.current_session_owner",
            return_value="tool-owner",
        ), patch(
            "services.operation_log_monitor.automation_session_ready",
            return_value={"ready": True},
        ), patch(
            "services.operation_log_monitor.load_qianchuan_storage_state",
            return_value={"cookies": [], "origins": []},
        ), patch(
            "services.operation_log_monitor.QianChuanFetcher",
            _FakeFetcher,
        ), patch(
            "services.operation_log_monitor._prepare_platform_log_page",
            new=AsyncMock(),
        ), patch(
            "services.operation_log_monitor._fetch_platform_log_payload",
            new=AsyncMock(return_value=payload),
        ), patch("api.operation_events.init_sqlite_schema"):
            result = asyncio.run(
                _sync_account_platform_logs_unlocked(
                    "1001",
                    "tool-owner",
                    db=self.db,
                )
            )
        self.assertTrue(result["success"])
        self.assertEqual("ok", result["status"])
        self.assertEqual(0, result["row_count"])
        self.assertEqual(1, result["stored_row_count"])


if __name__ == "__main__":
    unittest.main()
