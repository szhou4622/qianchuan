# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from services import operation_daily_report as daily
from services.qianchuan_accounts import (
    ensure_qianchuan_account,
    save_qianchuan_account_settings,
)
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class OperationDailyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "daily.db")
        self.config_path = os.path.join(self.temp.name, "daily.json")
        init_sqlite_schema(database=self.db_path)
        self.store = SQLiteStore(database=self.db_path)
        self.store.execute(
            "INSERT INTO pmc_ad_detail_basic "
            "(aadvid,ad_id,user_info_name,target_uid,plan_name,promotion_scene,plan_system) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "10001",
                "20001",
                "测试千川账户",
                "target-1",
                "测试商品计划",
                "product",
                "global",
            ),
        )
        self._event(
            "event-1",
            "10001",
            "retarget",
            "success",
            "tool_direct",
            "2026-07-27 10:01:02",
            summary="追投成功",
            material_id="70001",
        )
        self._event(
            "event-2",
            "10001",
            "stop",
            "success",
            "platform_log",
            "2026-07-27 11:02:03",
            summary="调控手动关闭",
            regulate_task_id="80001",
        )
        self._event(
            "event-other-account",
            "99999",
            "plan_create",
            "success",
            "platform_log",
            "2026-07-27 12:00:00",
        )
        self._event(
            "event-other-day",
            "10001",
            "plan_delete",
            "success",
            "platform_log",
            "2026-07-26 12:00:00",
        )
        self._event(
            "event-browser-only",
            "10001",
            "other",
            "success",
            "browser_observed",
            "2026-07-27 13:00:00",
            summary="记录模式捕获到千川操作",
        )
        self.store.insert_or_update(
            "platform_log_sync_state",
            {
                "aavid": "10001",
                "coverage_from": "2026-07-27 00:00:00",
                "coverage_to": "2026-07-27 23:59:59",
                "last_sync_at": "2026-07-28 08:55:00",
                "last_status": "ok",
            },
            unique_fields=["aavid"],
        )
        self.sent = []
        self.patches = [
            patch.object(daily, "CONFIG_FILE", self.config_path),
            patch.object(daily, "current_local_feishu_account", return_value="tool-user"),
            patch.object(
                daily,
                "get_local_feishu_status",
                return_value={"connected": True, "status": "connected"},
            ),
            patch.object(
                daily,
                "list_local_feishu_bound_targets",
                return_value=[("open_id", "ou_owner")],
            ),
            patch.object(
                daily,
                "send_local_feishu_bound_card",
                side_effect=self._send,
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _event(
        self,
        uid,
        aavid,
        action,
        status,
        source,
        occurred_at,
        **values,
    ):
        data = {
            "event_uid": uid,
            "aavid": aavid,
            "target_uid": "target-1",
            "source": source,
            "action_type": action,
            "status": status,
            "occurred_at": occurred_at,
            "plan_name": "测试商品计划",
        }
        data.update(values)
        self.store.insert("account_operation_event", data)

    def _send(self, card, *, targets=None):
        self.sent.append({"card": card, "targets": targets})
        receive_type, receive_id = targets[0]
        return [
            {
                "receive_type": receive_type,
                "receive_id": receive_id,
                "message_id": f"om_{len(self.sent)}",
            }
        ]

    def _save_enabled(self):
        account = ensure_qianchuan_account(
            "10001",
            account_name="测试千川账户",
            owner_username="tool-user",
            enabled=True,
            seen=True,
            db=self.store,
        )
        save_qianchuan_account_settings(
            account["account_uid"],
            {"enabled": True, "report_enabled": True},
            owner_username="tool-user",
            db=self.store,
        )
        result = daily.save_operation_daily_report_config(
            {
                "enabled": True,
                "send_time": "09:00",
                "aavids": ["10001"],
                "send_empty": True,
            },
            database=self.db_path,
        )
        self.assertTrue(result["success"])

    def test_report_options_do_not_fall_back_to_historical_accounts(self):
        self.assertEqual([], daily.list_operation_account_options(self.db_path))

    def test_enabled_account_without_monitored_plans_is_report_eligible(self):
        ensure_qianchuan_account(
            "20002",
            account_name="仅日报账户",
            owner_username="tool-user",
            enabled=True,
            seen=True,
            db=self.store,
        )
        options = daily.list_operation_account_options(self.db_path)
        self.assertEqual(["20002"], [item["aavid"] for item in options])

    def test_disabled_account_is_not_report_eligible(self):
        ensure_qianchuan_account(
            "20003",
            account_name="已停用账户",
            owner_username="tool-user",
            enabled=False,
            report_enabled=True,
            seen=True,
            db=self.store,
        )
        self.assertEqual([], daily.list_operation_account_options(self.db_path))

    def test_daily_config_rejects_account_not_enabled_in_directory(self):
        result = daily.save_operation_daily_report_config(
            {
                "enabled": True,
                "send_time": "09:00",
                "aavids": ["10001"],
                "send_empty": True,
            },
            database=self.db_path,
        )
        self.assertFalse(result["success"])
        self.assertIn("添加并启用", result["message"])

    def test_empty_directory_clears_stale_selection_and_blocks_send(self):
        daily._save_profile_config(
            "tool-user",
            {
                "enabled": True,
                "send_time": "09:00",
                "aavids": ["10001"],
                "send_empty": True,
            },
        )
        config = daily.get_operation_daily_report_config(database=self.db_path)
        self.assertFalse(config["config"]["enabled"])
        self.assertEqual([], config["config"]["aavids"])
        result = daily.send_operation_daily_report(
            report_date="2026-07-27",
            mode="manual",
            database=self.db_path,
        )
        self.assertFalse(result["success"])
        self.assertEqual([], self.sent)
        scheduled = daily.run_operation_daily_report_scheduler_once(
            now=datetime(2026, 7, 28, 9, 5),
            database=self.db_path,
        )
        self.assertTrue(scheduled["skipped"])
        self.assertEqual("no_enabled_report_accounts", scheduled["reason"])

    def test_report_is_strictly_isolated_by_account_and_date(self):
        report = daily.build_operation_daily_report(
            "10001",
            "2026-07-27",
            database=self.db_path,
        )
        self.assertEqual(2, report["event_count"])
        self.assertEqual(1, report["action_counts"]["retarget"])
        self.assertEqual(1, report["action_counts"]["stop"])
        self.assertNotIn("browser_observed", report["source_counts"])
        self.assertEqual("测试千川账户", report["account_name"])
        self.assertTrue(report["platform_coverage_complete"])

    def test_platform_verified_browser_event_is_reported_as_platform_log(self):
        self._event(
            "event-browser-verified",
            "10001",
            "budget_update",
            "success",
            "browser_observed",
            "2026-07-27 14:00:00",
            summary="修改计划预算",
            platform_event_id="platform-log-1",
        )
        report = daily.build_operation_daily_report(
            "10001",
            "2026-07-27",
            database=self.db_path,
        )
        self.assertEqual(3, report["event_count"])
        self.assertEqual(1, report["action_counts"]["budget_update"])
        self.assertEqual(2, report["source_counts"]["platform_log"])
        self.assertNotIn("browser_observed", report["source_counts"])

    def test_card_has_account_summary_details_and_no_leading_dot(self):
        report = daily.build_operation_daily_report(
            "10001",
            "2026-07-27",
            database=self.db_path,
        )
        card = daily.build_operation_daily_report_card(report)
        raw = json.dumps(card, ensure_ascii=False)
        self.assertIn("测试千川账户", raw)
        self.assertIn("追投 1", raw)
        self.assertIn("停投 1", raw)
        self.assertIn("追投成功", raw)
        self.assertIn("投放操作日志（时间倒序）", raw)
        self.assertNotIn("浏览器记录", raw)
        self.assertNotIn("记录模式捕获到千川操作", raw)
        self.assertNotIn("·账户ID", raw)

    def test_scheduled_delivery_is_idempotent_after_restart_style_retry(self):
        self._save_enabled()
        first = daily.run_operation_daily_report_scheduler_once(
            now=datetime(2026, 7, 28, 9, 5),
            database=self.db_path,
        )
        second = daily.run_operation_daily_report_scheduler_once(
            now=datetime(2026, 7, 28, 10, 5),
            database=self.db_path,
        )
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(2, len(self.sent))
        self.assertEqual(2, second["skipped_count"])
        row = self.store.select_one(
            "operation_daily_report_delivery",
            where={"delivery_mode": "scheduled"},
        )
        self.assertEqual("success", row["status"])

    def test_scheduled_delivery_claim_blocks_concurrent_sender(self):
        report = daily.build_operation_daily_report(
            "10001",
            "2026-07-27",
            database=self.db_path,
        )
        key = daily._delivery_key(
            "tool-user",
            "10001",
            "2026-07-27",
            "open_id",
            "ou_owner",
        )
        first = daily._claim_scheduled_delivery(
            self.store,
            delivery_key=key,
            report=report,
            account_username="tool-user",
            receive_type="open_id",
            receive_id="ou_owner",
        )
        second = daily._claim_scheduled_delivery(
            self.store,
            delivery_key=key,
            report=report,
            account_username="tool-user",
            receive_type="open_id",
            receive_id="ou_owner",
        )
        self.assertEqual("claimed", first)
        self.assertEqual("in_progress", second)

    def test_manual_send_can_be_repeated_for_testing(self):
        self._save_enabled()
        first = daily.send_operation_daily_report(
            report_date="2026-07-27",
            mode="manual",
            database=self.db_path,
        )
        second = daily.send_operation_daily_report(
            report_date="2026-07-27",
            mode="manual",
            database=self.db_path,
        )
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(4, len(self.sent))

    def test_scheduler_waits_until_configured_time(self):
        self._save_enabled()
        result = daily.run_operation_daily_report_scheduler_once(
            now=datetime(2026, 7, 28, 8, 59),
            database=self.db_path,
        )
        self.assertTrue(result["skipped"])
        self.assertEqual("not_due", result["reason"])
        self.assertEqual([], self.sent)

    def test_personal_and_group_deliveries_are_each_idempotent(self):
        self._save_enabled()
        with patch.object(
            daily,
            "list_local_feishu_bound_targets",
            return_value=[("open_id", "ou_owner"), ("chat_id", "oc_group")],
        ):
            daily.run_operation_daily_report_scheduler_once(
                now=datetime(2026, 7, 28, 9, 5),
                database=self.db_path,
            )
            daily.run_operation_daily_report_scheduler_once(
                now=datetime(2026, 7, 28, 10, 5),
                database=self.db_path,
            )
        self.assertEqual(4, len(self.sent))
        targets = [item["targets"][0] for item in self.sent]
        self.assertEqual(
            [
                ("open_id", "ou_owner"),
                ("chat_id", "oc_group"),
                ("open_id", "ou_owner"),
                ("chat_id", "oc_group"),
            ],
            targets,
        )

    def test_empty_day_has_an_explicit_message(self):
        report = daily.build_operation_daily_report(
            "10001",
            "2026-07-25",
            database=self.db_path,
        )
        card = daily.build_operation_daily_report_card(report)
        self.assertEqual(0, report["event_count"])
        self.assertIn("昨日没有查到操作记录", json.dumps(card, ensure_ascii=False))

    def test_incomplete_platform_coverage_is_visible(self):
        self.store.insert_or_update(
            "platform_log_sync_state",
            {
                "aavid": "10001",
                "coverage_from": "2026-07-27 12:00:00",
                "coverage_to": "2026-07-27 23:59:59",
                "last_sync_at": "2026-07-28 08:55:00",
                "last_status": "ok",
            },
            unique_fields=["aavid"],
        )
        report = daily.build_operation_daily_report(
            "10001",
            "2026-07-27",
            database=self.db_path,
        )
        card = daily.build_operation_daily_report_card(report)
        self.assertFalse(report["platform_coverage_complete"])
        self.assertIn(
            "后台操作日志覆盖不完整",
            json.dumps(card, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
