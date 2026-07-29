# -*- coding: utf-8 -*-
import json
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from api.retargeting_runs import (
    MANUAL_RETARGET_PROBE_VERSION,
    _record_manual_retarget_capability_if_verified,
)
from services.promotion_capability import (
    RETARGET_FORM_PROBE_VERSION,
    check_target_capability,
    record_target_capability,
)
from services.retargeting_service import RetargetingRunResult


TEST_CAPABILITY_VERIFIED_AT = datetime.now().isoformat(timespec="seconds")


class PromotionCapabilityTests(unittest.TestCase):
    @staticmethod
    def _bound_target(
        capability,
        *,
        target_uid="target-1",
        aavid="10001",
        ad_id="20001",
    ):
        return {
            "target_uid": target_uid,
            "aadvid": aavid,
            "ad_id": ad_id,
            "capability_json": json.dumps(capability),
        }

    def test_chengfang_retarget_with_complete_scoped_evidence_passes(self):
        ok, reason = check_target_capability(
            self._bound_target(
                {
                    "retarget_execute": True,
                    "retarget_scene": "live",
                    "retarget_plan_system": "chengfang",
                    "retarget_probe_version": RETARGET_FORM_PROBE_VERSION,
                    "retarget_verified_at": TEST_CAPABILITY_VERIFIED_AT,
                    "retarget_target_uid": "target-1",
                    "retarget_aavid": "10001",
                    "retarget_ad_id": "20001",
                }
            ),
            action="retarget",
            promotion_scene="live",
            plan_system="chengfang",
        )
        self.assertTrue(ok, reason)

    def test_stale_scoped_probe_version_is_rejected(self):
        ok, reason = check_target_capability(
            {
                "retarget_execute": True,
                "retarget_scene": "live",
                "retarget_plan_system": "chengfang",
                "retarget_probe_version": "retarget-form-v2",
                "retarget_verified_at": TEST_CAPABILITY_VERIFIED_AT,
                "retarget_target_uid": "target-1",
                "retarget_aavid": "10001",
                "retarget_ad_id": "20001",
            },
            action="retarget",
            promotion_scene="live",
            plan_system="chengfang",
        )
        self.assertFalse(ok)
        self.assertIn("版本", reason)

    def test_chengfang_legacy_unscoped_evidence_is_rejected(self):
        ok, reason = check_target_capability(
            {"capability_json": '{"retarget_execute":true}'},
            action="retarget",
            promotion_scene="product",
            plan_system="chengfang",
        )
        self.assertFalse(ok)
        self.assertIn("缺少", reason)

    def test_chengfang_tampered_plan_system_label_is_rejected(self):
        ok, reason = check_target_capability(
            {
                "regulation_execute": True,
                "regulation_scene": "product",
                "regulation_plan_system": "global",
                "regulation_probe_version": "manual-stop-batch-v1",
                "regulation_verified_at": TEST_CAPABILITY_VERIFIED_AT,
                "regulation_target_uid": "target-1",
                "regulation_aavid": "10001",
                "regulation_ad_id": "20001",
            },
            action="regulation",
            promotion_scene="product",
            plan_system="chengfang",
        )
        self.assertFalse(ok)
        self.assertIn("计划体系", reason)

    def test_global_legacy_product_evidence_is_rejected(self):
        ok, reason = check_target_capability(
            {"retarget_execute": True},
            action="retarget",
            promotion_scene="product",
            plan_system="global",
        )
        self.assertFalse(ok)
        self.assertIn("缺少", reason)

    def test_live_global_without_probe_is_rejected_for_new_target(self):
        ok, reason = check_target_capability(
            {},
            action="regulation",
            promotion_scene="live",
            plan_system="global",
        )
        self.assertFalse(ok)
        self.assertIn("验证", reason)

    def test_live_global_legacy_boolean_evidence_remains_compatible(self):
        ok, reason = check_target_capability(
            {"regulation_execute": True},
            action="regulation",
            promotion_scene="live",
            plan_system="global",
        )
        self.assertTrue(ok, reason)

    def test_live_global_legacy_boolean_never_authorizes_batch(self):
        ok, reason = check_target_capability(
            {"retarget_execute": True},
            action="retarget",
            promotion_scene="live",
            plan_system="global",
            require_batch=True,
        )
        self.assertFalse(ok)
        self.assertIn("多素材", reason)

    def test_record_capability_rechecks_target_scope_and_merges(self):
        class FakeStore:
            def __init__(self):
                self.updated = None

            def select_one(self, table, **_kwargs):
                self.assert_table = table
                return {
                    "target_uid": "target-1",
                    "aadvid": "10001",
                    "ad_id": "20001",
                    "promotion_scene": "live",
                    "plan_system": "chengfang",
                    "capability_json": '{"retarget_execute":true}',
                }

            @contextmanager
            def transaction(self):
                yield object()

            def execute(self, *_args, **_kwargs):
                return []

            def update(self, table, values, *, where, **_kwargs):
                self.updated = (table, values, where)

        store = FakeStore()
        capability = record_target_capability(
            store,
            target_uid="target-1",
            action="regulation",
            promotion_scene="live",
            plan_system="chengfang",
            probe_version="manual-stop-batch-v1",
            verified_at=TEST_CAPABILITY_VERIFIED_AT,
        )
        self.assertTrue(capability["retarget_execute"])
        self.assertTrue(capability["regulation_execute"])
        self.assertEqual("live", capability["regulation_scene"])
        self.assertEqual("chengfang", capability["regulation_plan_system"])
        self.assertEqual(
            ("promotion_target", {"target_uid": "target-1"}),
            (store.updated[0], store.updated[2]),
        )

    def test_record_capability_rejects_scope_tampering(self):
        class FakeStore:
            @contextmanager
            def transaction(self):
                yield object()

            def execute(self, *_args, **_kwargs):
                return []

            def select_one(self, _table, **_kwargs):
                return {
                    "target_uid": "target-1",
                    "aadvid": "10001",
                    "ad_id": "20001",
                    "promotion_scene": "product",
                    "plan_system": "global",
                    "capability_json": "{}",
                }

        with self.assertRaisesRegex(ValueError, "作用域"):
            record_target_capability(
                FakeStore(),
                target_uid="target-1",
                action="regulation",
                promotion_scene="product",
                plan_system="chengfang",
                probe_version="manual-stop-batch-v1",
            )

    def test_failed_manual_retarget_never_writes_capability(self):
        failed = RetargetingRunResult(
            success=False,
            message="接口失败",
            step="submit_api",
        )
        with patch(
            "api.retargeting_runs.record_target_capability"
        ) as record:
            wrote = _record_manual_retarget_capability_if_verified(
                object(),
                failed,
                target_uid="target-1",
                promotion_scene="live",
                plan_system="chengfang",
                verified_at=TEST_CAPABILITY_VERIFIED_AT,
            )
        self.assertFalse(wrote)
        record.assert_not_called()

    def test_successful_manual_retarget_records_scoped_capability(self):
        succeeded = RetargetingRunResult(
            success=True,
            message="追投成功",
            step="done",
        )
        store = object()
        with patch(
            "api.retargeting_runs.record_target_capability"
        ) as record:
            wrote = _record_manual_retarget_capability_if_verified(
                store,
                succeeded,
                target_uid="target-1",
                promotion_scene="product",
                plan_system="chengfang",
                verified_at=TEST_CAPABILITY_VERIFIED_AT,
            )
        self.assertTrue(wrote)
        record.assert_called_once_with(
            store,
            target_uid="target-1",
            action="retarget",
            promotion_scene="product",
            plan_system="chengfang",
            probe_version=MANUAL_RETARGET_PROBE_VERSION,
            verified_at=TEST_CAPABILITY_VERIFIED_AT,
        )

    def test_capability_copied_to_another_target_is_rejected(self):
        capability = {
            "retarget_execute": True,
            "retarget_scene": "product",
            "retarget_plan_system": "global",
            "retarget_probe_version": RETARGET_FORM_PROBE_VERSION,
            "retarget_verified_at": TEST_CAPABILITY_VERIFIED_AT,
            "retarget_target_uid": "target-1",
            "retarget_aavid": "10001",
            "retarget_ad_id": "20001",
        }
        ok, reason = check_target_capability(
            self._bound_target(
                capability,
                target_uid="target-2",
                ad_id="20002",
            ),
            action="retarget",
            promotion_scene="product",
            plan_system="global",
        )
        self.assertFalse(ok)
        self.assertIn("监控目标", reason)

    def test_multi_material_retarget_requires_batch_probe(self):
        capability = {
            "retarget_execute": True,
            "retarget_scene": "product",
            "retarget_plan_system": "global",
            "retarget_probe_version": RETARGET_FORM_PROBE_VERSION,
            "retarget_verified_at": TEST_CAPABILITY_VERIFIED_AT,
            "retarget_target_uid": "target-1",
            "retarget_aavid": "10001",
            "retarget_ad_id": "20001",
        }
        ok, reason = check_target_capability(
            self._bound_target(capability),
            action="retarget",
            promotion_scene="product",
            plan_system="global",
            require_batch=True,
        )
        self.assertFalse(ok)
        self.assertIn("多素材", reason)

    def test_expired_capability_is_rejected(self):
        capability = {
            "retarget_execute": True,
            "retarget_scene": "product",
            "retarget_plan_system": "global",
            "retarget_probe_version": RETARGET_FORM_PROBE_VERSION,
            "retarget_verified_at": "2026-01-01 00:00:00",
            "retarget_target_uid": "target-1",
            "retarget_aavid": "10001",
            "retarget_ad_id": "20001",
        }
        ok, reason = check_target_capability(
            self._bound_target(capability),
            action="retarget",
            promotion_scene="product",
            plan_system="global",
        )
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_all_four_scene_system_scopes_accept_bound_current_evidence(self):
        for scene in ("live", "product"):
            for plan_system in ("global", "chengfang"):
                with self.subTest(scene=scene, plan_system=plan_system):
                    capability = {
                        "retarget_execute": True,
                        "retarget_scene": scene,
                        "retarget_plan_system": plan_system,
                        "retarget_probe_version": RETARGET_FORM_PROBE_VERSION,
                        "retarget_verified_at": TEST_CAPABILITY_VERIFIED_AT,
                        "retarget_target_uid": "target-1",
                        "retarget_aavid": "10001",
                        "retarget_ad_id": "20001",
                    }
                    ok, reason = check_target_capability(
                        self._bound_target(capability),
                        action="retarget",
                        promotion_scene=scene,
                        plan_system=plan_system,
                    )
                    self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
