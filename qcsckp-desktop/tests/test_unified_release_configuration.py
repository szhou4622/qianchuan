import os
import sys
import unittest
from unittest.mock import patch

import release_configuration as policy


class UnifiedReleaseConfigurationTests(unittest.TestCase):
    def test_windows_freezes_netfx_and_edgechromium_without_loading_either(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "platform", "win32"), \
             patch.dict(os.environ, {"PYTHONNET_RUNTIME": "coreclr", "PYTHONNET_NETFX_CONFIG_FILE": "private.config",
                                     "PYTHONNET_CORECLR_RUNTIME_CONFIG": "private.json", "PYWEBVIEW_GUI": "qt"}, clear=True):
            policy.enforce_packaged_configuration()
            self.assertEqual("netfx", os.environ["PYTHONNET_RUNTIME"])
            self.assertEqual("edgechromium", os.environ["PYWEBVIEW_GUI"])
            self.assertNotIn("PYTHONNET_NETFX_CONFIG_FILE", os.environ)
            self.assertNotIn("PYTHONNET_CORECLR_RUNTIME_CONFIG", os.environ)
            os.environ["PYTHONNET_RUNTIME"] = "mono"
            self.assertEqual("netfx", policy.environment_value("PYTHONNET_RUNTIME"))
            policy.enforce_packaged_configuration()
            self.assertEqual("netfx", os.environ["PYTHONNET_RUNTIME"])

    def test_owner_and_unknown_future_developer_override_cannot_change_release(self):
        with patch.object(sys, "frozen", True, create=True), patch.dict(os.environ, {
                "QCSCKP_SESSION_OWNER": "teacher-only", "QCSCKP_FUTURE_TEST_BYPASS": "1",
                "QCSCKP_HOME": "isolated-home", "QCSCKP_DATA_DIR": "isolated-data"}, clear=True):
            policy.enforce_packaged_configuration()
            self.assertNotIn("QCSCKP_SESSION_OWNER", os.environ)
            self.assertNotIn("QCSCKP_FUTURE_TEST_BYPASS", os.environ)
            self.assertEqual("isolated-data", os.environ["QCSCKP_DATA_DIR"])

    def test_public_build_config_matches_runtime_contract_and_excludes_private_inputs(self):
        identity = {**policy.IDENTITY, "secret": "must-not-appear", "teacher_owner": "not-public"}
        built = policy.public_default_configuration(identity)
        self.assertEqual(policy.public_runtime_contract()["software_contract_sha256"], built["software_contract_sha256"])
        self.assertNotIn("must-not-appear", str(built))
        self.assertNotIn("teacher_owner", built)
        self.assertNotIn("ignored_development_override_names", built)

    def test_build_policy_does_not_fake_frozen_python_or_require_app_start(self):
        with patch.object(sys, "frozen", False, create=True), patch.object(sys, "platform", "win32"), \
             patch.dict(os.environ, {"QCSCKP_TEST_MODE": "1", "PYWEBVIEW_GUI": "mshtml"}, clear=True):
            policy.enforce_packaged_configuration(for_build=True)
            self.assertFalse(sys.frozen)
            self.assertNotIn("QCSCKP_TEST_MODE", os.environ)
            self.assertEqual("edgechromium", os.environ["PYWEBVIEW_GUI"])

    def test_packaged_build_ignores_developer_function_switches_and_tokens(self):
        injected = {key: "private-injected-value" for key in policy.DEVELOPMENT_ENVIRONMENT_KEYS}
        injected.update(QCSCKP_HOME="isolated-home", QCSCKP_DATA_DIR="isolated-data")
        with patch.dict(os.environ, injected, clear=True), patch.object(sys, "frozen", True, create=True), patch.object(policy, "_ignored_keys", set()):
            policy.enforce_packaged_configuration()
            self.assertEqual("isolated-home", os.environ["QCSCKP_HOME"])
            self.assertEqual("isolated-data", os.environ["QCSCKP_DATA_DIR"])
            for key in injected:
                if key in policy.DEVELOPMENT_ENVIRONMENT_KEYS:
                    self.assertNotIn(key, os.environ)
            public = str(policy.public_runtime_contract())
            self.assertNotIn("private-injected-value", public)

    def test_package_contract_is_not_changed_by_developer_environment(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(policy, "_ignored_keys", set()):
            before = policy.public_runtime_contract()["software_contract_sha256"]
            with patch.dict(os.environ, {"QCSCKP_QIANCHUAN_BACKEND": "browser_legacy", "REGULATION_RULE_INTERVAL_SEC": "1"}):
                policy.enforce_packaged_configuration()
                self.assertEqual(before, policy.public_runtime_contract()["software_contract_sha256"])
                self.assertEqual("official_api", policy.environment_value("QCSCKP_QIANCHUAN_BACKEND", "official_api"))

    def test_source_tests_keep_explicit_injection_without_changing_release_policy(self):
        with patch.object(sys, "frozen", False, create=True), patch.dict(os.environ, {"QCSCKP_TEST_MODE": "1"}):
            policy.enforce_packaged_configuration()
            self.assertEqual("1", policy.environment_value("QCSCKP_TEST_MODE"))


if __name__ == "__main__":
    unittest.main()
