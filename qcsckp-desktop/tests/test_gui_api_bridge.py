import unittest
from unittest.mock import Mock

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
        self.core.listPromotionTargetProducts.return_value = {"success": True, "data": []}
        self.core.startPromotionTargetDiscovery.return_value = {"success": True}
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
            self.bridge.listPromotionTargetProducts("target-1"),
            {"success": True, "data": []},
        )
        self.assertEqual(
            self.bridge.startPromotionTargetDiscovery(),
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
        self.core.listPromotionTargetProducts.assert_called_once_with("target-1")
        self.core.startPromotionTargetDiscovery.assert_called_once_with()
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


if __name__ == "__main__":
    unittest.main()
