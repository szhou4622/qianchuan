import os
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from api.account_auth import AccountAuthApi
from services import cloud_retarget_client
from services.fetcher import QianChuanFetcher
from services.run_services import ServiceController


class LocalStandaloneAuthTests(unittest.TestCase):
    def test_bundled_local_account_logs_in_without_network(self):
        test_salt = bytes.fromhex("00112233445566778899aabbccddeeff")
        test_hash = hashlib.pbkdf2_hmac(
            "sha256", b"test-password", test_salt, 240_000
        ).hex()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cloud_retarget_client,
            "SESSION_FILE",
            os.path.join(tmp, "device_session.json"),
        ), patch("api.account_auth.LOCAL_AUTH_PASSWORD_SALT", test_salt.hex()), patch(
            "api.account_auth.LOCAL_AUTH_PASSWORD_HASH", test_hash
        ), patch("api.account_auth.urlopen") as remote:
            result = AccountAuthApi().verify_login(
                "qcsckp_local",
                "test-password",
            )

            self.assertTrue(result["success"])
            self.assertEqual("local", result["data"]["auth_mode"])
            self.assertEqual("qcsckp_local", result["data"]["username"])
            self.assertEqual(
                "qcsckp_local",
                cloud_retarget_client.load_device_session()["username"],
            )
            remote.assert_not_called()

    def test_wrong_local_password_is_rejected_without_network(self):
        with patch("api.account_auth.urlopen") as remote:
            result = AccountAuthApi().verify_login("qcsckp_local", "wrong")

        self.assertFalse(result["success"])
        self.assertIn("账号或密码错误", result["message"])
        remote.assert_not_called()

    def test_version_check_is_local_and_never_opens_url(self):
        with patch("api.account_auth.urlopen") as remote:
            result = AccountAuthApi().check_version_update("0.1.40")

        self.assertTrue(result["success"])
        self.assertFalse(result["data"]["has_update"])
        self.assertEqual("0.1.40", result["data"]["latest_version"])
        remote.assert_not_called()

    def test_cloud_client_blocks_all_central_requests(self):
        with patch("services.cloud_retarget_client.urlopen") as remote:
            result = cloud_retarget_client._request("/api/account.php")

        self.assertFalse(result["success"])
        self.assertIn("禁用中心服务器", result["message"])
        remote.assert_not_called()

    def test_service_controller_does_not_keep_cloud_backup_password(self):
        controller = ServiceController.__new__(ServiceController)
        controller._cloud_backup_username = "old"
        controller._cloud_backup_password = "old-password"

        controller.set_cloud_backup_credentials("qcsckp_local", "secret")

        self.assertIsNone(controller._cloud_backup_username)
        self.assertEqual("", controller._cloud_backup_password)

    def test_fetcher_cloud_upload_guards_run_before_network(self):
        fetcher = QianChuanFetcher.__new__(QianChuanFetcher)
        with patch("services.fetcher.urlopen") as remote:
            fetcher._upload_cloud_backup_batches("user", "password", [{"id": 1}])
            fetcher._upload_ad_detail_basic_batches("user", "password", [{"id": 1}])

        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
