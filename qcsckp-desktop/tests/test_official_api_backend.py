import json
import unittest
from datetime import datetime
from decimal import Decimal
from urllib.error import URLError
from unittest.mock import patch

from services.qianchuan_open_api.client import ApiResponse, QianchuanOpenApiClient
from services.qianchuan_open_api.errors import (
    ApiRateLimitError,
    ApiRequestError,
    ApiWriteOutcomeUnknown,
    OfficialApiWriteDisabled,
)
from services.qianchuan_open_api.normalizers import (
    normalize_account,
    normalize_control_task,
    normalize_material,
    normalize_plan,
    stable_material_set,
)
from services.qianchuan_open_api.service import QianchuanOfficialApiService
from services.qianchuan_open_api.token_provider import (
    AccessTokenBundle,
    InjectedTokenProvider,
)
from services.promotion_capability import check_target_capability


class _PagedClient(QianchuanOpenApiClient):
    def __init__(self, pages):
        super().__init__(InjectedTokenProvider(AccessTokenBundle("token")))
        self.pages = list(pages)

    def get(self, endpoint, query=None, *, advertiser_id=""):
        page = int((query or {}).get("page") or 1)
        return self.pages[page - 1]


class _CaptureClient:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.posts = []

    def get_all_pages(self, endpoint, query, **kwargs):
        if "control_task/list" in endpoint:
            return self.tasks, ["req-list"]
        return [], []

    def post(self, endpoint, body, *, advertiser_id=""):
        self.posts.append((endpoint, body, str(advertiser_id)))
        return ApiResponse(
            data={"task_id": 9876543210123456789},
            raw={"code": 0},
            request_id="req-write",
        )


class _PublicInfoClient(_CaptureClient):
    def get(self, endpoint, query=None, *, advertiser_id=""):
        self.last_get = (endpoint, dict(query or {}), str(advertiser_id))
        return ApiResponse(
            data=[
                {
                    "advertiser_id": "1782685702496260",
                    "advertiser_name": "松之选专卖店",
                }
            ],
            raw={"code": 0},
            request_id="req-public-info",
        )


class OfficialApiBackendTests(unittest.TestCase):
    def test_public_info_fills_missing_account_name(self):
        service = QianchuanOfficialApiService(_PublicInfoClient())
        rows = service.list_advertiser_public_info(["1782685702496260"])
        self.assertEqual("松之选专卖店", rows[0]["advertiser_name"])
        self.assertEqual(
            "/open_api/2/advertiser/public_info/",
            service.client.last_get[0],
        )
        self.assertEqual(
            ["1782685702496260"],
            service.client.last_get[1]["advertiser_ids"],
        )

    def test_multi_account_shop_names_are_enriched_from_public_info(self):
        service = QianchuanOfficialApiService(_CaptureClient())
        with patch.object(
            service,
            "list_authorized_accounts",
            return_value=[
                {
                    "advertiser_id": "55192491",
                    "advertiser_name": "店铺主体",
                    "role": "SHOP",
                    "shop_id": "55192491",
                }
            ],
        ), patch.object(
            service,
            "list_shop_advertisers",
            return_value=[
                {"advertiser_id": "10001", "advertiser_name": ""},
                {"advertiser_id": "10002", "advertiser_name": ""},
            ],
        ), patch.object(
            service,
            "list_advertiser_public_info",
            return_value=[
                {"advertiser_id": "10001", "advertiser_name": "账户甲"},
                {"advertiser_id": "10002", "advertiser_name": "账户乙"},
            ],
        ):
            rows, evidence = service.list_business_accounts()
        self.assertEqual(
            {"10001": "账户甲", "10002": "账户乙"},
            {row["advertiser_id"]: row["advertiser_name"] for row in rows},
        )
        self.assertTrue(evidence["account_names_complete"])

    def test_account_refresh_is_queued_while_catalog_worker_is_running(self):
        from services import official_api_catalog as catalog

        worker = unittest.mock.Mock()
        worker.is_alive.return_value = True
        with patch.object(catalog, "_THREAD", worker), patch.object(
            catalog, "_PENDING_ACCOUNT_UIDS", set()
        ), patch.object(catalog, "_PENDING_ALL", False):
            result = catalog.start_official_api_catalog_sync("account-uid-2")
            self.assertTrue(result["queued"])
            self.assertIn("account-uid-2", catalog._PENDING_ACCOUNT_UIDS)

    def test_unknown_detail_system_does_not_replace_list_classification(self):
        from services import official_api_catalog as catalog

        fake_service = unittest.mock.Mock()
        fake_service.list_business_accounts.return_value = (
            [{"advertiser_id": "10001", "advertiser_name": "测试账户"}],
            {"complete": True},
        )
        fake_service.list_all_plans.return_value = (
            [
                {
                    "aavid": "10001",
                    "ad_id": "20001",
                    "plan_name": "直播全域",
                    "promotion_scene": "live",
                    "plan_system": "global",
                    "marketing_goal": "LIVE_PROM_GOODS",
                    "adlab_scene": "UNI_PROJECT",
                    "platform_status": "active",
                }
            ],
            {"complete": True, "classes": {}},
        )
        fake_service.get_plan_detail.return_value = (
            {
                "aavid": "10001",
                "ad_id": "20001",
                "plan_name": "直播全域",
                "promotion_scene": "live",
                "plan_system": "unknown",
                "marketing_goal": "LIVE_PROM_GOODS",
                "adlab_scene": "0",
                "platform_status": "active",
            },
            ApiResponse(data={}, raw={"code": 0}, request_id="req-detail"),
        )
        account = {
            "aavid": "10001",
            "account_uid": "account-1",
            "account_name": "测试账户",
            "owner_username": "owner",
        }
        captured = []
        with patch.object(catalog, "get_official_api_service", return_value=fake_service), patch.object(
            catalog, "ensure_qianchuan_account"
        ), patch.object(catalog, "list_promotion_targets", return_value=[]), patch.object(
            catalog, "upsert_promotion_target", side_effect=lambda payload, **_kwargs: captured.append(payload) or {"target_uid": "target-1"}
        ), patch.object(catalog, "patch_target_sync_state"):
            result = catalog._sync_account(account, unittest.mock.Mock())
        self.assertTrue(result["complete"])
        self.assertEqual("global", captured[0]["plan_system"])
        self.assertEqual("live", captured[0]["promotion_scene"])

    def test_single_shop_account_inherits_official_shop_name(self):
        service = QianchuanOfficialApiService(_CaptureClient())
        with patch.object(
            service,
            "list_authorized_accounts",
            return_value=[
                {
                    "advertiser_id": "55192491",
                    "advertiser_name": "松鲜鲜松之选专卖店",
                    "role": "SHOP",
                    "shop_id": "55192491",
                }
            ],
        ), patch.object(
            service,
            "list_shop_advertisers",
            return_value=[{"advertiser_id": "1782685702496260", "advertiser_name": ""}],
        ):
            rows, evidence = service.list_business_accounts()
        self.assertTrue(evidence["complete"])
        self.assertEqual(rows[0]["advertiser_name"], "松鲜鲜松之选专卖店")

    def test_user_selected_official_account_is_added_and_warmed(self):
        from services.official_api_catalog import add_authorized_account

        fake_service = unittest.mock.Mock()
        fake_service.list_business_accounts.return_value = (
            [
                {
                    "advertiser_id": "1782685702496260",
                    "advertiser_name": "松鲜鲜松之选专卖店",
                }
            ],
            {"complete": True},
        )
        saved = {
            "account_uid": "account-uid-1",
            "aavid": "1782685702496260",
            "account_name": "松鲜鲜松之选专卖店",
        }
        with patch(
            "services.official_api_catalog.get_official_api_service",
            return_value=fake_service,
        ), patch(
            "services.official_api_catalog.ensure_qianchuan_account",
            return_value=saved,
        ) as ensure, patch(
            "services.official_api_catalog.start_official_api_catalog_sync",
            return_value={"success": True, "running": True},
        ) as start_sync:
            result = add_authorized_account("1782685702496260")
        self.assertTrue(result["success"])
        ensure.assert_called_once()
        start_sync.assert_called_once_with("account-uid-1")

    def test_real_oceanengine_collection_keys_are_extracted(self):
        cases = {
            "adv_id_list": [{"adv_id": "123"}],
            "ad_list": [{"ad_id": "456"}],
            "material_list": [{"material_id": "789"}],
            "product_list": [{"product_id": "321"}],
            "task_list": [{"task_id": "654"}],
            "log_list": [{"log_id": "987"}],
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(
                    QianchuanOpenApiClient.extract_items({key: expected}),
                    expected,
                )

    def test_shop_advertiser_object_list_is_not_hidden_by_numeric_list(self):
        data = {
            "adv_id_list": [{"adv_id": "1782685702496260", "extra_permission": []}],
            "list": [1782685702496260],
        }
        self.assertEqual(
            QianchuanOpenApiClient.extract_items(data),
            data["adv_id_list"],
        )

    def test_enterprise_operator_is_not_exposed_as_final_advertiser(self):
        service = QianchuanOfficialApiService(_CaptureClient())
        with patch.object(
            service,
            "list_authorized_accounts",
            return_value=[
                {
                    "advertiser_id": "1858078536393860",
                    "advertiser_name": "企业操作主体",
                    "role": "PLATFORM_ROLE_ENTERPRISE_BP_OPERATOR",
                    "shop_id": "",
                }
            ],
        ):
            rows, evidence = service.list_business_accounts()
        self.assertEqual([], rows)
        self.assertTrue(evidence["complete"])
        self.assertEqual("unsupported_subject", evidence["subjects"][0]["type"])
        self.assertTrue(evidence["subjects"][0]["ignored"])

    def test_api_code_40100_is_treated_as_rate_limit(self):
        client = QianchuanOpenApiClient(
            InjectedTokenProvider(AccessTokenBundle("token")),
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(ApiRateLimitError):
            client._raise_api_error(
                {"code": 40100, "message": "System request frequency exceeded"},
                endpoint="/read/",
                http_status=200,
            )

    def test_shop_advertiser_adv_id_is_normalized_as_long_string(self):
        account = normalize_account({"adv_id": 1782685702496260})
        self.assertEqual(account["advertiser_id"], "1782685702496260")
        self.assertEqual(account["aavid"], "1782685702496260")

    def test_four_plan_classes_are_normalized_from_official_fields(self):
        matrix = {
            ("OVERALL_PROJECT", "LIVE_PROM_GOODS"): ("chengfang", "live"),
            ("OVERALL_PROJECT", "VIDEO_PROM_GOODS"): ("chengfang", "product"),
            ("UNI_PROJECT", "LIVE_PROM_GOODS"): ("global", "live"),
            ("UNI_PROJECT", "VIDEO_PROM_GOODS"): ("global", "product"),
        }
        for (adlab_scene, marketing_goal), expected in matrix.items():
            with self.subTest(adlab_scene=adlab_scene, marketing_goal=marketing_goal):
                plan = normalize_plan(
                    {
                        "ad_id": "9876543210123456789",
                        "adlab_scene": adlab_scene,
                        "marketing_goal": marketing_goal,
                        "status": "ENABLE",
                    },
                    advertiser_id="1234567890123456789",
                )
                self.assertEqual((plan["plan_system"], plan["promotion_scene"]), expected)

    def test_live_plan_list_ad_info_wrapper_is_normalized(self):
        plan = normalize_plan(
            {
                "ad_info": {
                    "id": 1804998056156307,
                    "name": "直播全域计划",
                    "adlab_scene": "UNI_PROJECT",
                    "marketing_goal": "LIVE_PROM_GOODS",
                    "status": "ENABLE",
                },
                "product_info": [{"id": 999}],
                "room_info": [{"id": 888}],
            },
            advertiser_id="1782685702496260",
        )
        self.assertEqual("1804998056156307", plan["ad_id"])
        self.assertEqual("直播全域计划", plan["plan_name"])
        self.assertEqual("global", plan["plan_system"])
        self.assertEqual("live", plan["promotion_scene"])
        self.assertEqual("active", plan["platform_status"])

    def test_official_api_capability_is_valid_for_batch_retarget(self):
        verified_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        capability = {
            "source": "qianchuan_open_api",
            "retarget_execute": True,
            "retarget_scene": "product",
            "retarget_plan_system": "chengfang",
            "retarget_probe_version": "official-open-api-v1",
            "retarget_verified_at": verified_at,
            "retarget_target_uid": "target-1",
            "retarget_aavid": "123",
            "retarget_ad_id": "456",
            "retarget_batch_execute": True,
            "retarget_batch_probe_version": "official-open-api-v1",
            "retarget_batch_verified_at": verified_at,
        }
        ok, reason = check_target_capability(
            {
                "target_uid": "target-1",
                "aadvid": "123",
                "ad_id": "456",
                "capability_json": json.dumps(capability),
            },
            action="retarget",
            promotion_scene="product",
            plan_system="chengfang",
            require_batch=True,
        )
        self.assertTrue(ok, reason)

    def test_long_ids_are_strings_in_normalized_models(self):
        plan = normalize_plan(
            {
                "ad_id": 9876543210123456789,
                "marketing_goal": "LIVE_PROM_GOODS",
                "adlab_scene": "OVERALL_PROJECT",
                "status": "ENABLE",
            },
            advertiser_id=1234567890123456789,
        )
        self.assertEqual(plan["aavid"], "1234567890123456789")
        self.assertEqual(plan["ad_id"], "9876543210123456789")
        self.assertEqual(plan["plan_system"], "chengfang")
        self.assertEqual(plan["promotion_scene"], "live")

    def test_object_arrays_normalize_material_and_product_ids(self):
        material = normalize_material(
            {
                "material_id": "90071992547409931",
                "material_type": "VIDEO",
                "products": [{"product_id": 90071992547409933}],
            }
        )
        task = normalize_control_task(
            {
                "task_id": 90071992547409935,
                "scene": "MATERIAL_ADD_BUDGET",
                "materials": [
                    {"material_id": 90071992547409931},
                    {"material_id": "90071992547409932"},
                ],
            }
        )
        self.assertEqual(material["product_ids"], ["90071992547409933"])
        self.assertEqual(
            task["material_ids"],
            ["90071992547409931", "90071992547409932"],
        )

    def test_pagination_rejects_repeated_pages(self):
        page = ApiResponse(
            data={"list": [{"id": "1"}], "page_info": {"has_more": True}},
            raw={},
            request_id="r1",
        )
        client = _PagedClient([page, page])
        with self.assertRaises(ApiRequestError):
            client.get_all_pages("/open_api/v1.0/test/", {}, page_size=1)

    def test_post_network_failure_is_not_retried(self):
        client = QianchuanOpenApiClient(
            InjectedTokenProvider(AccessTokenBundle("token")),
            max_get_attempts=4,
            sleep=lambda _: None,
        )
        with patch(
            "services.qianchuan_open_api.client.urlopen",
            side_effect=URLError("offline"),
        ) as mocked:
            with self.assertRaises(ApiWriteOutcomeUnknown):
                client.post(
                    "/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/",
                    {"advertiser_id": 123},
                    advertiser_id="123",
                )
        self.assertEqual(mocked.call_count, 1)

    def test_real_writes_are_disabled_by_default(self):
        service = QianchuanOfficialApiService(_CaptureClient(), allow_writes=False)
        with self.assertRaises(OfficialApiWriteDisabled):
            service.update_control_status("123", ["456"], action="PAUSE")

    def test_create_uses_exact_integer_json_ids(self):
        client = _CaptureClient()
        service = QianchuanOfficialApiService(client, allow_writes=True)
        response = service.create_material_control_task(
            "1234567890123456789",
            ad_id="9876543210123456789",
            marketing_goal="VIDEO_PROM_GOODS",
            name="test",
            budget=Decimal("100"),
            duration=Decimal("24"),
            material_ids=["90071992547409931", "90071992547409932"],
        )
        body = client.posts[0][1]
        self.assertIsInstance(body["advertiser_id"], int)
        self.assertEqual(body["advertiser_id"], 1234567890123456789)
        self.assertEqual(body["ad_id"], 9876543210123456789)
        self.assertEqual(body["material_ids"][0], 90071992547409931)
        self.assertNotIn('"1234567890123456789"', json.dumps(body))
        self.assertEqual(str(response.data["task_id"]), "9876543210123456789")

    def test_overlapping_groups_are_not_treated_as_duplicates(self):
        client = _CaptureClient(
            tasks=[
                {
                    "task_id": "88",
                    "material_ids": ["1", "2", "3"],
                    "budget": "100",
                    "duration": "24",
                    "status": "ENABLE",
                }
            ]
        )
        service = QianchuanOfficialApiService(client, allow_writes=True)
        duplicate = service.find_duplicate_control_task(
            "123",
            ad_id="456",
            marketing_goal="VIDEO_PROM_GOODS",
            budget="100",
            duration="24",
            material_ids=["1", "2"],
        )
        self.assertIsNone(duplicate)

    def test_delete_control_action_is_forbidden(self):
        service = QianchuanOfficialApiService(_CaptureClient(), allow_writes=True)
        with self.assertRaises(ValueError):
            service.update_control_status("123", ["456"], action="DELETE")

    def test_stable_material_set_rejects_non_digit_ids(self):
        with self.assertRaises(ValueError):
            stable_material_set(["123", "not-an-id"])


if __name__ == "__main__":
    unittest.main()
