from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from api.dashboard_optimized import _optional_float, _optional_int
from api.rule_retargeting_config import (
    MONITOR_METRIC_OFFICIAL_FIELDS,
    unsupported_strategy_monitor_metrics,
    validate_rule_retargeting_config,
)
from services.official_api_execution import _validate_budget_against_plan
from services.qianchuan_open_api.service import build_material_control_task_body


def _strategy(metric: str = "currentCost") -> dict:
    return {
        "id": "strategy-1",
        "title": "策略1",
        "account_uid": "account-1",
        "target_uid": "target-1",
        "task_action": "create_retarget",
        "action_mode": "card_confirm",
        "trigger": {
            "group_combine": "or",
            "groups": [
                {"join": "and", "conditions": [{"metric": metric, "op": "gt", "value": 1}]}
            ],
        },
        "retargeting": {
            "method": "volume",
            "volume": {"total_budget_yuan": 100, "duration_hours": 24},
            "interval": {"window_seconds": 300, "max_count": 1},
        },
    }


class MonitorMetricContractTests(unittest.TestCase):
    def test_six_metrics_map_to_exact_official_fields(self):
        self.assertEqual(
            {
                "currentCost": "stat_cost_for_roi2",
                "netAmount": "total_order_settle_amount_for_roi2_1h",
                "netRoi": "total_prepay_and_pay_settle_roi2_1h",
                "netOrderCount": "total_order_settle_count_for_roi2_1h",
                "overallAmount": "total_pay_order_gmv_include_coupon_for_roi2",
                "overallPayRoi": "total_prepay_and_pay_order_roi2",
            },
            MONITOR_METRIC_OFFICIAL_FIELDS,
        )

    def test_unsupported_official_metric_is_not_usable_for_target(self):
        target = {
            "capability": {
                "report_metric_units": {
                    "stat_cost_for_roi2": "3",
                    "total_order_settle_amount_for_roi2_1h": "3",
                }
            }
        }
        self.assertEqual(
            ["netRoi"],
            unsupported_strategy_monitor_metrics(_strategy("netRoi"), target),
        )
        self.assertEqual(
            [],
            unsupported_strategy_monitor_metrics(_strategy("netAmount"), target),
        )

    def test_legacy_material_metric_is_rejected_for_new_retarget(self):
        config = {"enabled": True, "strategies": [_strategy("costDiff")]}
        ok, message = validate_rule_retargeting_config(config)
        self.assertFalse(ok)
        self.assertIn("只能使用整体消耗", message)

    def test_missing_values_remain_null(self):
        self.assertIsNone(_optional_float(None))
        self.assertIsNone(_optional_float(""))
        self.assertIsNone(_optional_int(None))
        self.assertEqual(0.0, _optional_float(0))
        self.assertEqual(0, _optional_int(0))

    def test_frontend_uses_dynamic_official_metric_availability(self):
        html = (
            Path(__file__).resolve().parents[1] / "static/rule_retargeting.html"
        ).read_text(encoding="utf-8")
        for field in MONITOR_METRIC_OFFICIAL_FIELDS.values():
            self.assertIn(field, html)
        self.assertIn("当前计划官方接口未提供该指标", html)
        self.assertIn("metricOption.disabled", html)


class ControlTaskRequestContractTests(unittest.TestCase):
    def test_product_volume_contains_only_official_volume_fields(self):
        body = build_material_control_task_body(
            advertiser_id="123",
            ad_id="456",
            marketing_goal="VIDEO_PROM_GOODS",
            name="商品追投",
            budget=Decimal("100"),
            duration=Decimal("24"),
            material_ids=["1001", "1002"],
        )
        self.assertEqual(
            {
                "advertiser_id",
                "ad_id",
                "name",
                "scene",
                "budget",
                "material_type",
                "material_ids",
                "duration",
            },
            set(body),
        )

    def test_live_volume_uses_conservative_smart_bid_only(self):
        body = build_material_control_task_body(
            advertiser_id="123",
            ad_id="456",
            marketing_goal="LIVE_PROM_GOODS",
            name="直播放量",
            budget=100,
            duration=24,
            material_ids=["1001"],
            extra={"smart_bid_type": "SMART_BID_CONSERVATIVE"},
        )
        self.assertEqual("SMART_BID_CONSERVATIVE", body["smart_bid_type"])
        self.assertNotIn("roi2_goal", body)
        self.assertNotIn("bid", body)

    def test_live_roi_cost_control_omits_duration_and_bid(self):
        body = build_material_control_task_body(
            advertiser_id="123",
            ad_id="456",
            marketing_goal="LIVE_PROM_GOODS",
            name="直播控成本",
            budget=100,
            duration=None,
            material_ids=["1001"],
            extra={
                "smart_bid_type": "SMART_BID_CUSTOM",
                "external_action": "AD_CONVERT_TYPE_LIVE_SUCCESSORDER_PAY",
                "deep_external_action": "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
                "roi2_goal": 6,
            },
        )
        self.assertNotIn("duration", body)
        self.assertNotIn("bid", body)
        self.assertEqual(6.0, body["roi2_goal"])

    def test_live_conversion_bid_omits_roi_and_duration(self):
        body = build_material_control_task_body(
            advertiser_id="123",
            ad_id="456",
            marketing_goal="LIVE_PROM_GOODS",
            name="直播间成交",
            budget=100,
            duration=None,
            material_ids=["1001"],
            extra={
                "smart_bid_type": "SMART_BID_CUSTOM",
                "external_action": "AD_CONVERT_TYPE_LIVE_SUCCESSORDER_PAY",
                "bid": 20,
            },
        )
        self.assertEqual(20.0, body["bid"])
        self.assertNotIn("duration", body)
        self.assertNotIn("roi2_goal", body)
        self.assertNotIn("deep_external_action", body)

    def test_budget_cannot_exceed_current_source_plan_budget(self):
        detail = {"raw": {"ad_info": {"budget": "300"}}}
        _validate_budget_against_plan(detail, Decimal("300"))
        with self.assertRaisesRegex(ValueError, "不能高于主计划"):
            _validate_budget_against_plan(detail, Decimal("301"))

    def test_unknown_or_conflicting_fields_fail_before_post(self):
        common = dict(
            advertiser_id="123",
            ad_id="456",
            marketing_goal="LIVE_PROM_GOODS",
            name="错误请求",
            budget=100,
            duration=None,
            material_ids=["1001"],
        )
        with self.assertRaisesRegex(ValueError, "未声明"):
            build_material_control_task_body(**common, extra={"unknown": 1})
        with self.assertRaisesRegex(ValueError, "必须且只能"):
            build_material_control_task_body(
                **common,
                extra={
                    "smart_bid_type": "SMART_BID_CUSTOM",
                    "external_action": "AD_CONVERT_TYPE_LIVE_SUCCESSORDER_PAY",
                    "deep_external_action": "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
                    "roi2_goal": 6,
                    "bid": 10,
                },
            )


if __name__ == "__main__":
    unittest.main()
