"""Scope errors must never attach another plan's money or manufacture zeros."""
from copy import deepcopy
import unittest

from services.chengfang_material_metrics import (
    DATA_PERIOD, chengfang_live_context, merge_chengfang_material_metrics,
    report_metric_value,
)
from services.qianchuan_open_api.client import ApiResponse, QianchuanOpenApiClient
from services.qianchuan_open_api.errors import ApiRequestError, ApiTokenError, PaginationIntegrityError
from services.qianchuan_open_api.normalizers import normalize_plan
from services.qianchuan_open_api.service import CHENGFANG_OPTIONAL_TRAFFIC_METRICS, QianchuanOfficialApiService

CORE_FIELDS = (
    "stat_cost_for_roi2", "total_order_settle_count_for_roi2_1h", "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_order_roi2", "total_pay_order_gmv_include_coupon_for_roi2",
    "total_prepay_and_pay_settle_roi2_1h", "total_refund_order_gmv_for_roi2_1h_rate", "total_pay_order_count_for_roi2",
)


def plan(pid="2001", anchor="8001", *, state="DISABLE", **extra):
    return {"ad_id": pid, "aweme_id": anchor, "marketing_goal": "LIVE_PROM_GOODS",
            "adlab_scene": "OVERALL_PROJECT", "status": state,
            "delivery_setting": {"smart_bid_type": "SMART_BID_CUSTOM"}, **extra}


class ReadClient:
    def __init__(self):
        self.detail = plan()
        self.plans = [plan(), plan("2002", "8002")]
        self.report = [{"dimensions": {"material_id": {"Value": 3001, "ValueStr": "3001"},
                                       "roi2_material_video_name": {"ValueStr": "video"}},
                        "metrics": {"stat_cost_for_roi2": {"Value": "12.50"},
                                    "total_prepay_and_pay_order_roi2": {"Value": 4},
                                    "total_pay_order_gmv_include_coupon_for_roi2": {"Value": 50}}}]
        self.calls = []
        self.catalog_error = None
        self.bid_catalogs = {}
        self.peer_details = {}
        self.global_plans = []
        self.optional_error = None
        self.core_error = None
        self.optional_report = None

    def get(self, endpoint, query, **kwargs):
        self.calls.append((endpoint, deepcopy(query), kwargs))
        if endpoint == QianchuanOfficialApiService.REPORT_CONFIG:
            return ApiResponse({'custom_config_datas':[{'data_topic':'OVERALL_ROI_LIVE_MATERIAL_VIDEO','metrics':[{'field':'stat_cost_for_roi2','unit':3}]}]}, {}, 'config-request')
        value = self.detail if query["ad_id"] == "2001" else self.peer_details.get(query["ad_id"])
        if value is None:
            value = next(p for p in self.plans if p["ad_id"] == query["ad_id"])
        return ApiResponse(deepcopy(value), {}, "detail-request")

    def get_all_pages(self, endpoint, query, **kwargs):
        self.calls.append((endpoint, deepcopy(query), kwargs))
        if endpoint == QianchuanOfficialApiService.PLAN_LIST:
            if self.catalog_error:
                raise self.catalog_error
            if query.get("adlab_scene") == "UNI_PROJECT":
                return [{"ad_info":deepcopy(p)} for p in self.global_plans], ["global-catalog-request"]
            plans = self.bid_catalogs.get(query.get("filtering", {}).get("smart_bid_type"), self.plans)
            return [{"ad_info": deepcopy(p)} for p in plans], ["catalog-request"]
        if endpoint == QianchuanOfficialApiService.REPORT_DATA:
            optional = set(query.get("metrics") or ()) <= CHENGFANG_OPTIONAL_TRAFFIC_METRICS
            error = self.optional_error if optional else self.core_error
            if error:
                raise error
            if optional and self.optional_report is not None:
                return deepcopy(self.optional_report), ["optional-request"]
            return deepcopy(self.report), ["report-request"]
        raise AssertionError(endpoint)

    def post(self, *args, **kwargs):
        raise AssertionError("report reads must not write")


class ChengfangMetricTests(unittest.TestCase):
    def strict_read(self, client, **kwargs):
        metrics = kwargs.pop("metrics", ["stat_cost_for_roi2", "total_prepay_and_pay_order_roi2", "total_pay_order_gmv_include_coupon_for_roi2"])
        return QianchuanOfficialApiService(client, allow_writes=False).list_chengfang_live_material_report(
            "1001", "2001", start_date="2026-09-10", end_date="2026-09-10",
            metrics=metrics, **kwargs)

    def test_strict_route_uses_fresh_complete_scope_all_statuses_and_chengfang_period(self):
        client = ReadClient()
        rows, ids, scope = self.strict_read(client)
        self.assertEqual("unique_plan_in_complete_catalog", scope["attribution"])
        self.assertEqual("8001", scope["anchor_id"])
        self.assertEqual(["detail-request", "catalog-request", "catalog-request", "global-catalog-request", "global-catalog-request", "report-request"], ids)
        catalogs = [c for c in client.calls if c[0] == QianchuanOfficialApiService.PLAN_LIST and c[1]['adlab_scene']=='OVERALL_PROJECT']
        self.assertEqual(["SMART_BID_CUSTOM", "SMART_BID_CONSERVATIVE"],
                         [c[1]["filtering"]["smart_bid_type"] for c in catalogs])
        for catalog in catalogs:
            self.assertNotIn("status", catalog[1]["filtering"])
            self.assertEqual("OVERALL_PROJECT", catalog[1]["adlab_scene"])
            self.assertTrue(catalog[2]["verify_stability"])
            self.assertEqual("2026-09-10 00:00:00", catalog[1]["start_time"])
        report = next(c for c in client.calls if c[0] == QianchuanOfficialApiService.REPORT_DATA)
        self.assertEqual(DATA_PERIOD, report[1]["data_period"])
        self.assertEqual({"roi2_material_type_v3": ["3"], "anchor_id": ["8001"], "ecp_app_id": ["1"]},
                         {row["field"]: row["values"] for row in report[1]["filters"]})
        self.assertNotIn("ad_id", report[1]["dimensions"])
        self.assertEqual("12.50", rows[0]["raw"]["metrics"]["stat_cost_for_roi2"]["Value"])

    def test_same_anchor_other_paused_plan_blocks_before_report(self):
        client = ReadClient()
        client.plans[1]["aweme_id"] = "8001"
        with self.assertRaisesRegex(ApiRequestError, "多个乘方直播计划"):
            self.strict_read(client)
        self.assertFalse(any(c[0] == QianchuanOfficialApiService.REPORT_DATA for c in client.calls))

    def test_competing_plan_only_in_volume_catalog_still_blocks(self):
        client = ReadClient()
        client.bid_catalogs = {"SMART_BID_CUSTOM": [plan()],
                               "SMART_BID_CONSERVATIVE": [plan("2003", "8001")]}
        with self.assertRaisesRegex(ApiRequestError, "多个乘方直播计划"):
            self.strict_read(client)
        self.assertFalse(any(c[0] == QianchuanOfficialApiService.REPORT_DATA for c in client.calls))

    def test_explicit_wrong_peer_identity_is_not_hidden_by_detail_fallback(self):
        client = ReadClient()
        client.plans[1]["advertiser_id"] = "9999"
        with self.assertRaisesRegex(ApiRequestError, "千川账户"):
            self.strict_read(client)
        self.assertEqual(1, sum(c[0] == QianchuanOfficialApiService.PLAN_DETAIL for c in client.calls))

    def test_missing_peer_anchor_blocks_even_after_detail_fallback(self):
        client = ReadClient()
        client.plans[1].pop("aweme_id")
        with self.assertRaisesRegex(ApiRequestError, "抖音号ID缺少"):
            self.strict_read(client)
        self.assertEqual(2, sum(c[0] == QianchuanOfficialApiService.PLAN_DETAIL for c in client.calls))
        self.assertFalse(any(c[0] == QianchuanOfficialApiService.REPORT_DATA for c in client.calls))

    def test_missing_list_fields_can_be_proved_by_fresh_peer_detail(self):
        client = ReadClient()
        client.peer_details["2002"] = deepcopy(client.plans[1])
        client.plans[1] = {"ad_id": "2002"}
        rows, _, scope = self.strict_read(client)
        self.assertEqual(1, len(rows))
        self.assertEqual(2, scope["catalog_plan_count"])

    def test_wrong_detail_scope_and_changed_hint_fail(self):
        for changes in ({"ad_id": "9999"}, {"advertiser_id": "9999"},
                        {"adlab_scene": "UNI_PROJECT"}, {"marketing_goal": "VIDEO_PROM_GOODS"},
                        {"aweme_id": 8001.0}, {"ecp_app_id": "2"}):
            with self.subTest(changes=changes):
                client = ReadClient()
                client.detail.update(changes)
                with self.assertRaises(ApiRequestError):
                    self.strict_read(client)
                self.assertFalse(any(c[0] == QianchuanOfficialApiService.REPORT_DATA for c in client.calls))
        with self.assertRaisesRegex(ApiRequestError, "归属在采集期间变化"):
            self.strict_read(ReadClient(), plan_detail=normalize_plan(plan(anchor="9999"), advertiser_id="1001"))

    def test_incomplete_or_missing_target_catalog_never_queries_report(self):
        for kind in ("missing", "duplicate", "paging"):
            client = ReadClient()
            if kind == "missing":
                client.plans = client.plans[1:]
            elif kind == "duplicate":
                client.plans.append(plan())
            else:
                client.catalog_error = PaginationIntegrityError("incomplete")
            with self.subTest(kind=kind), self.assertRaises(ApiRequestError):
                self.strict_read(client)
            self.assertFalse(any(c[0] == QianchuanOfficialApiService.REPORT_DATA for c in client.calls))

    def test_merge_intersects_membership_uses_report_and_keeps_missing_unknown(self):
        client = ReadClient()
        client.report.append({"dimensions": {"material_id": {"ValueStr": "9999"}},
                              "metrics": {"stat_cost_for_roi2": {"Value": 9000}}})
        rows, _, scope = self.strict_read(client)
        members = [{"material_id": "3001", "stats_info": {"stat_cost_for_roi2": 0},
                    "raw": {"stats_info": {"stat_cost_for_roi2": 0}, "video_info": {"title": "keep"}}},
                   {"material_id": "3002", "stats_info": {"stat_cost_for_roi2": 0},
                    "raw": {"stats_info": {"stat_cost_for_roi2": 0}}}]
        merged = merge_chengfang_material_metrics(members, rows, scope=scope)
        self.assertEqual(["3001", "3002"], [r["material_id"] for r in merged])
        self.assertEqual((12.5, 4, 50), tuple(merged[0]["stats_info"].values()))
        self.assertEqual({}, merged[1]["stats_info"])
        self.assertEqual({}, merged[1]["raw"]["stats_info"])
        self.assertFalse(merged[1]["report_row_present"])
        self.assertEqual("keep", merged[0]["raw"]["video_info"]["title"])
        self.assertEqual("12.50", merged[0]["raw"]["stats_info"]["stat_cost_for_roi2"]["Value"])
        self.assertEqual(0, members[0]["stats_info"]["stat_cost_for_roi2"])

    def test_zero_null_invalid_and_declared_units_preserve_meaning(self):
        self.assertEqual(0.005, report_metric_value({"Value":0.5,"ValueStr":"0.50%"}))
        self.assertEqual(0.0625, report_metric_value({"Value":6.25,"ValueStr":"6.25%"}))
        self.assertEqual(0, report_metric_value({"Value": 0, "ValueStr": "--"}))
        self.assertIsNone(report_metric_value({"Value": None, "ValueStr": "0"}))
        self.assertIsNone(report_metric_value({"Value": "--"}))
        self.assertIsNone(report_metric_value({"Value": float("nan")}))
        self.assertEqual(12.5, report_metric_value({"Value": 1250, "unit": 2}))
        self.assertEqual(12.5, report_metric_value({"Value": 1250000}, "1"))
        self.assertEqual(0.1, report_metric_value({"Value": 10, "Unit": 10}))
        with self.assertRaisesRegex(ApiRequestError, "未验证"):
            report_metric_value({"Value": 5, "unit": "unknown"})
        with self.assertRaisesRegex(ApiRequestError, "不一致"):
            report_metric_value({"Value": 5, "unit": 2}, "3")

    def test_unproven_scope_and_duplicate_report_do_not_merge(self):
        with self.assertRaises(ApiRequestError):
            merge_chengfang_material_metrics([], [], scope={})
        rows, _, scope = self.strict_read(ReadClient())
        with self.assertRaisesRegex(ApiRequestError, "多个报表行"):
            merge_chengfang_material_metrics([], rows + rows, scope=scope)

    def test_long_anchor_and_material_ids_stay_exact_strings(self):
        sample = plan(anchor="369875085961575")
        context = chengfang_live_context(normalize_plan(sample, advertiser_id="1001"), advertiser_id="1001", ad_id="2001")
        self.assertEqual("369875085961575", context["anchor_id"])
        client = ReadClient()
        client.report[0]["dimensions"]["material_id"] = {"Value": float(7677262821705547802), "ValueStr": "7677262821705547802"}
        rows, _, _ = self.strict_read(client)
        self.assertEqual("7677262821705547802", rows[0]["material_id"])

    def test_sibling_room_info_is_known_anchor_evidence(self):
        raw = plan()
        raw.pop("aweme_id")
        normalized = normalize_plan({"ad_info": raw, "room_info": {"anchor_id": "8001"}}, advertiser_id="1001")
        self.assertEqual("8001", chengfang_live_context(normalized, advertiser_id="1001", ad_id="2001")["anchor_id"])

    def test_absent_report_does_not_fall_back_to_native_zero_in_real_snapshot(self):
        from services.official_api_collection import _material_snapshot
        rows, _, scope = self.strict_read(ReadClient())
        merged = merge_chengfang_material_metrics(
            [{"material_id": "3002", "stats_info": {"stat_cost_for_roi2": 0},
              "raw": {"stats_info": {"stat_cost_for_roi2": 0}}}], rows, scope=scope)
        snapshot = _material_snapshot(merged[0], target={"plan_system": "chengfang", "promotion_scene": "live"}, units={}, request_id="report-request")
        self.assertIsNone(snapshot["stat_cost"])

    def test_summary_sentinel_counts_for_paging_but_never_becomes_material(self):
        summary = {"dimensions": {"material_id": {"Value": "-2", "ValueStr": "-"},
                                   "roi2_material_video_name": {"ValueStr": "-"}},
                   "metrics": {"stat_cost_for_roi2": {"Value": 99999}}}
        ordinary = ReadClient().report[0]

        class PagedClient(QianchuanOpenApiClient):
            def __init__(self, declared):
                self.calls = []
                self.declared = declared

            def get(self, endpoint, query=None, **kwargs):
                self.calls.append(query["page"])
                return ApiResponse({"rows": [summary, ordinary], "page_info": {
                    "page": 1, "page_size": 200, "total_number": self.declared, "total_page": 1}}, {}, "summary-request")

        client = PagedClient(2)
        rows, ids = QianchuanOfficialApiService(client, allow_writes=False).list_material_report(
            "1001", plan_system="chengfang", promotion_scene="live",
            start_date="2026-09-10", end_date="2026-09-10", metrics=["stat_cost_for_roi2"])
        self.assertEqual([1], client.calls)
        self.assertEqual(["3001"], [row["material_id"] for row in rows])
        self.assertEqual(1, len(ids))
        with self.assertRaises(PaginationIntegrityError):
            QianchuanOfficialApiService(PagedClient(1), allow_writes=False).list_material_report(
                "1001", plan_system="chengfang", promotion_scene="live",
                start_date="2026-09-10", end_date="2026-09-10", metrics=["stat_cost_for_roi2"])

    def test_other_negative_or_inexact_ids_are_not_summary_exceptions(self):
        for block in ({"Value": "-1", "ValueStr": "-"}, {"Value": -2.0, "ValueStr": "-"},
                      {"Value": "-2", "ValueStr": "-3"}, {"Value": "-2"},
                      {"Value": True, "ValueStr": "-"}, {"Value": 7677262821705547802.0}):
            with self.subTest(block=block):
                client = ReadClient()
                client.report[0]["dimensions"]["material_id"] = block
                with self.assertRaises(ApiRequestError):
                    self.strict_read(client)

    def test_all_metrics_share_one_supported_period_and_one_complete_response(self):
        client = ReadClient()
        metrics = [*CORE_FIELDS, *sorted(CHENGFANG_OPTIONAL_TRAFFIC_METRICS)]
        client.report[0]["metrics"] = {field: {"Value": index + 1} for index, field in enumerate(CORE_FIELDS)}
        client.report[0]["metrics"]["live_show_count_for_roi2_v2"] = {"Value": 0}
        client.report.append({'dimensions':{'material_id':{'ValueStr':'3002'}},'metrics':{
            field:{'Value':42 if field=='live_show_count_for_roi2_v2' else 0} for field in metrics}})
        rows, requests, scope = self.strict_read(client, metrics=metrics)
        queries = [c[1] for c in client.calls if c[0] == QianchuanOfficialApiService.REPORT_DATA]
        self.assertEqual([metrics], [q["metrics"] for q in queries])
        self.assertEqual('ALL_DATA', queries[0]["data_period"])
        self.assertEqual(set(metrics),set(rows[1]['stats_info']))
        self.assertEqual(0,rows[1]['stats_info']['stat_cost_for_roi2'])
        self.assertEqual(42,rows[1]['stats_info']['live_show_count_for_roi2_v2'])
        self.assertEqual([],scope['optional_metric_errors'])
        self.assertEqual(["report-request"], scope["core_metric_request_ids"])
        merged = merge_chengfang_material_metrics([{"material_id": "3001"}], rows, scope=scope)
        from services.official_api_collection import _material_snapshot
        snapshot = _material_snapshot(merged[0], target={"plan_system": "chengfang", "promotion_scene": "live"}, units={}, request_id="report-request")
        self.assertEqual(1, snapshot["stat_cost"])
        self.assertEqual(0,snapshot["overall_show_count"])
        self.assertIsNone(snapshot["overall_click_count"])

    def test_complete_response_keeps_traffic_only_materials_for_membership_intersection(self):
        client = ReadClient()
        client.report = [
            {"dimensions": {"material_id": {"ValueStr": "3001"}}, "metrics": {
                "live_show_count_for_roi2_v2": {"Value": 42}, "stat_cost_for_roi2": {"Value": 12.5}}},
            {"dimensions": {"material_id": {"ValueStr": "9999"}}, "metrics": {"live_show_count_for_roi2_v2": {"Value": 99999}}},
        ]
        rows, _, scope = self.strict_read(client, metrics=["stat_cost_for_roi2", "live_show_count_for_roi2_v2"])
        self.assertEqual(["3001","9999"], [row["material_id"] for row in rows])
        merged=merge_chengfang_material_metrics([{'material_id':'3001'}],rows,scope=scope)
        self.assertEqual(["3001"],[r['material_id'] for r in merged])
        self.assertEqual({"stat_cost_for_roi2": 12.5, "live_show_count_for_roi2_v2": 42}, rows[0]["stats_info"])
        self.assertEqual([], scope["optional_metric_errors"])
        self.assertEqual([], scope["optional_metric_request_ids"])

    def test_core_failure_and_optional_auth_scope_or_pagination_error_fail_whole_read(self):
        from services.qianchuan_open_api.token_provider import AuthorizationContextChanged
        client = ReadClient()
        client.core_error = ApiRequestError("core unavailable", code="40000")
        with self.assertRaisesRegex(ApiRequestError, "core unavailable"):
            self.strict_read(client, metrics=["stat_cost_for_roi2", "live_show_count_for_roi2_v2"])
        self.assertEqual(1, sum(c[0] == QianchuanOfficialApiService.REPORT_DATA for c in client.calls))
        for error in (AuthorizationContextChanged(), ApiTokenError("bad token", code="40000"),
                      PaginationIntegrityError("incomplete", code="40000"), ApiRequestError("bad params", code="400153")):
            with self.subTest(error=type(error).__name__):
                client = ReadClient()
                client.core_error = error
                with self.assertRaises(type(error)):
                    self.strict_read(client, metrics=["stat_cost_for_roi2", "live_show_count_for_roi2_v2"])

    def test_global_peer_with_same_anchor_blocks_whole_period_attribution(self):
        client=ReadClient()
        client.global_plans=[plan('2003','8001',adlab_scene='UNI_PROJECT')]
        with self.assertRaisesRegex(ApiRequestError,'其他全域计划'):
            self.strict_read(client)

    def test_config_and_report_periods_are_identical(self):
        client=ReadClient()
        QianchuanOfficialApiService(client,allow_writes=False).get_report_config('1001',plan_system='chengfang',promotion_scene='live')
        self.strict_read(client)
        config=next(q for endpoint,q,_ in client.calls if endpoint==QianchuanOfficialApiService.REPORT_CONFIG)
        report=next(q for endpoint,q,_ in client.calls if endpoint==QianchuanOfficialApiService.REPORT_DATA)
        self.assertEqual('ALL_DATA',config['data_period'])
        self.assertEqual(config['data_period'],report['data_period'])


if __name__ == "__main__":
    unittest.main()
