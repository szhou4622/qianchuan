import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gui_app import JSApi


class JSApiBridgeTests(unittest.TestCase):
    def setUp(self):
        self.core = Mock()
        self.bridge = JSApi.__new__(JSApi)
        self.bridge.api = self.core

    def test_promotion_target_methods_are_exposed_and_forwarded(self):
        self.core.listPromotionTargets.return_value = {"success": True, "data": []}
        self.core.getPromotionTarget.return_value = {"success": True}
        self.core.savePromotionTarget.return_value = {"success": True}
        self.core.discoverPromotionTarget.return_value = {"success": True}
        self.core.setPromotionTargetEnabled.return_value = {"success": True}
        self.core.clearPromotionTargetWriteBlock.return_value = {"success": True}
        self.core.listPromotionTargetProducts.return_value = {"success": True, "data": []}
        self.core.probePromotionTargetRetargetCapability.return_value = {
            "success": True
        }
        self.core.startPromotionTargetDiscovery.return_value = {"success": True}
        self.core.startQianchuanAccountSelection.return_value = {"success": True}
        self.core.startQianchuanRelogin.return_value = {"success": True}
        self.core.getPromotionTargetDiscoveryStatus.return_value = {"running": False}

        self.assertEqual(
            self.bridge.listPromotionTargets(True),
            {"success": True, "data": []},
        )
        self.assertEqual(
            self.bridge.getPromotionTarget("target-1"),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.savePromotionTarget({"target_uid": "target-1"}),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.discoverPromotionTarget("url", "text", "plan"),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.setPromotionTargetEnabled("target-1", False),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.clearPromotionTargetWriteBlock("target-1"),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.listPromotionTargetProducts("target-1"),
            {"success": True, "data": []},
        )
        self.assertEqual(
            self.bridge.probePromotionTargetRetargetCapability("target-1"),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.startPromotionTargetDiscovery(),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.startQianchuanAccountSelection(),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.startQianchuanRelogin(),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.getPromotionTargetDiscoveryStatus(),
            {"running": False},
        )

        self.core.listPromotionTargets.assert_called_once_with(True)
        self.core.getPromotionTarget.assert_called_once_with("target-1")
        self.core.savePromotionTarget.assert_called_once_with(
            {"target_uid": "target-1"}
        )
        self.core.discoverPromotionTarget.assert_called_once_with(
            "url", "text", "plan"
        )
        self.core.setPromotionTargetEnabled.assert_called_once_with(
            "target-1", False
        )
        self.core.clearPromotionTargetWriteBlock.assert_called_once_with(
            "target-1"
        )
        self.core.listPromotionTargetProducts.assert_called_once_with("target-1")
        self.core.probePromotionTargetRetargetCapability.assert_called_once_with(
            "target-1"
        )
        self.core.startPromotionTargetDiscovery.assert_called_once_with()
        self.core.startQianchuanAccountSelection.assert_called_once_with()
        self.core.startQianchuanRelogin.assert_called_once_with()
        self.core.getPromotionTargetDiscoveryStatus.assert_called_once_with()

    def test_operation_daily_report_methods_are_exposed_and_forwarded(self):
        config = {"enabled": True, "aavids": ["1001"]}
        self.core.getOperationDailyReportConfig.return_value = {
            "success": True,
            "accounts": [{"aavid": "1001"}],
        }
        self.core.saveOperationDailyReportConfig.return_value = {"success": True}
        self.core.sendYesterdayOperationDailyReportNow.return_value = {
            "success": True
        }

        self.assertTrue(
            self.bridge.getOperationDailyReportConfig()["success"]
        )
        self.assertEqual(
            self.bridge.saveOperationDailyReportConfig(config),
            {"success": True},
        )
        self.assertEqual(
            self.bridge.sendYesterdayOperationDailyReportNow(),
            {"success": True},
        )

        self.core.getOperationDailyReportConfig.assert_called_once_with()
        self.core.saveOperationDailyReportConfig.assert_called_once_with(config)
        self.core.sendYesterdayOperationDailyReportNow.assert_called_once_with()

    def test_rule_retargeting_reload_refreshes_promotion_targets(self):
        page = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "rule_retargeting.html"
        ).read_text(encoding="utf-8")
        start = page.index(
            "document.getElementById('btnReload').addEventListener"
        )
        end = page.index(
            "document.getElementById('btnRefreshLivePreflight')",
            start,
        )
        reload_handler = page[start:end]
        self.assertIn(
            "await loadPromotionTargetOptions(api);",
            reload_handler,
        )
        self.assertIn('id="strategyAccountUid"', page)
        self.assertIn('id="strategyTargetUid"', page)
        self.assertNotIn('id="sectionStrategyTriggerLevel"', page)
        self.assertNotIn('id="strategyTriggerLevel"', page)
        self.assertIn("grid grid-cols-1 md:grid-cols-2 gap-3 max-w-4xl", page)
        self.assertIn("renderStrategyTargetOptions(accountUid, '')", page)
        self.assertIn("account_uid: s.account_uid || ''", page)
        self.assertLess(
            reload_handler.index("await loadPromotionTargetOptions(api);"),
            reload_handler.index("await api.getRuleRetargetingConfig();"),
        )

    def test_monitor_page_exposes_plan_system_and_safe_capability_probe(self):
        page = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "promotion_targets.html"
        ).read_text(encoding="utf-8")
        self.assertIn("<title>计划高级设置</title>", page)
        self.assertIn("返回千川账户管理", page)
        self.assertIn("target_uid", page)
        self.assertIn('data-field="plan_system"', page)
        self.assertIn("验证追投能力", page)
        self.assertIn("probePromotionTargetRetargetCapability", page)
        self.assertIn("不会点击提交", page)

    def test_primary_navigation_hides_internal_control_and_advanced_plan_pages(self):
        page = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "index.html"
        ).read_text(encoding="utf-8")
        nav = page[page.index('<nav class="menu-list">'):page.index("</nav>")]
        self.assertNotIn('data-page="control"', nav)
        self.assertNotIn('data-page="promotion-targets"', nav)
        self.assertIn("'control': 'control.html'", page)
        self.assertIn("'promotion-targets': 'promotion_targets.html'", page)

    def test_operation_page_syncs_exports_and_renders_account_names(self):
        page = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "operation_events.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn('id="recordBtn"', page)
        self.assertNotIn("startOperationRecordBrowser", page)
        self.assertIn('id="syncBtn"', page)
        self.assertIn("syncOperationLogsNow", page)
        self.assertIn("导出当前结果", page)
        self.assertIn("saveOperationEventsCsv", page)
        self.assertIn("item.account_name", page)
        self.assertIn("`${name}（${id}）`", page)

    def test_operation_csv_uses_native_save_dialog(self):
        self.core.exportOperationEventsCsv.return_value = {
            "success": True,
            "filename": "千川账户_1001_操作流水.csv",
            "content": "\ufeff账户ID,操作\n1001,修改预算\n",
        }
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "流水.csv"
            window = Mock()
            window.create_file_dialog.return_value = [str(destination)]
            with patch("gui_app.webview.windows", [window]):
                result = self.bridge.saveOperationEventsCsv(
                    "1001",
                    "2026-08-01",
                    "2026-08-11",
                    "budget_update",
                    "platform_log",
                    "success",
                    "王斌",
                    "预算",
                )
            self.assertTrue(result["success"])
            self.assertFalse(result["cancelled"])
            self.assertEqual(
                "\ufeff账户ID,操作\n1001,修改预算\n",
                destination.read_text(encoding="utf-8"),
            )
            window.create_file_dialog.assert_called_once()
        self.core.exportOperationEventsCsv.assert_called_once_with(
            aavid="1001",
            date_from="2026-08-01",
            date_to="2026-08-11",
            action_type="budget_update",
            source="platform_log",
            status="success",
            operator="王斌",
            q="预算",
        )

    def test_operation_log_sync_is_exposed_and_forwarded(self):
        self.core.syncOperationLogsNow.return_value = {
            "success": True,
            "running": True,
        }
        self.assertEqual(
            self.bridge.syncOperationLogsNow("1001"),
            {"success": True, "running": True},
        )
        self.core.syncOperationLogsNow.assert_called_once_with("1001")

    def test_operation_query_uses_named_arguments_without_target_shift(self):
        self.core.listOperationEvents.return_value = {
            "success": True,
            "total": 1,
        }
        result = self.bridge.listOperationEvents(
            "1001",
            "2026-08-05",
            "2026-08-11",
            "stop",
            "platform_log",
            "success",
            "operator",
            "keyword",
            2,
            50,
        )
        self.assertEqual(1, result["total"])
        self.core.listOperationEvents.assert_called_once_with(
            aavid="1001",
            date_from="2026-08-05",
            date_to="2026-08-11",
            action_type="stop",
            source="platform_log",
            status="success",
            operator="operator",
            q="keyword",
            page=2,
            page_size=50,
        )

    def test_diagnostics_page_has_direct_account_management_return(self):
        page = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "control.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="btnBackAccounts"', page)
        self.assertIn("返回千川账户管理", page)
        self.assertIn("window.location.href = 'qianchuan_accounts.html'", page)


if __name__ == "__main__":
    unittest.main()
