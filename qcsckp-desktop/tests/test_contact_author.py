import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from config import APP_NAME
from services.contact_config import (
    ContactConfigError,
    ContactConfigNotConfigured,
    ContactConfigService,
)
from services.contact_http import ContactLocalHttpServer


ROOT = Path(__file__).resolve().parents[1]


def configured_payload(
    *,
    app_name=APP_NAME,
    enabled=True,
    url="https://cdn.example.com/contact.png",
    updated_at="2026-08-19 20:00:00",
):
    return {
        "data": {
            "app_name": app_name,
            "enabled": enabled,
            "qr_image_url": url,
            "updated_at": updated_at,
        }
    }


class ContactAuthorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-contact-")
        self.root = Path(self.temp.name)
        self.cache_file = self.root / "contact-cache.json"
        self.fallback_file = self.root / "fallback.svg"
        self.fallback_file.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def service(self, loader):
        return ContactConfigService(
            cache_file=str(self.cache_file),
            fallback_image_file=str(self.fallback_file),
            remote_loader=loader,
        )

    def test_remote_config_displays_https_image_and_persists_cache(self):
        service = self.service(lambda _url, _timeout: configured_payload())
        result = service.get_contact_config()
        self.assertEqual("configured", result["status"])
        self.assertEqual("remote", result["source"])
        self.assertEqual("https://cdn.example.com/contact.png", result["qr_image_url"])
        cached = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(APP_NAME, cached["app_name"])

    def test_remote_unconfigured_uses_builtin_image(self):
        def loader(_url, _timeout):
            raise ContactConfigNotConfigured("not configured")

        result = self.service(loader).get_contact_config()
        self.assertEqual("fallback", result["status"])
        self.assertTrue(result["use_builtin_image"])
        self.assertEqual("remote_unconfigured", result["source"])

    def test_network_failure_uses_latest_valid_cache(self):
        self.service(lambda _url, _timeout: configured_payload()).get_contact_config()

        def offline(_url, _timeout):
            raise ContactConfigError("offline")

        result = self.service(offline).get_contact_config()
        self.assertEqual("cache", result["source"])
        self.assertTrue(result["cached"])
        self.assertEqual("https://cdn.example.com/contact.png", result["qr_image_url"])

    def test_disabled_remote_clears_old_image_and_cache(self):
        self.service(lambda _url, _timeout: configured_payload()).get_contact_config()
        disabled = self.service(
            lambda _url, _timeout: configured_payload(
                enabled=False,
                url="https://cdn.example.com/old-contact.png",
            )
        ).get_contact_config()
        self.assertEqual("disabled", disabled["status"])
        self.assertFalse(disabled["enabled"])
        self.assertEqual("", disabled["qr_image_url"])

        def offline(_url, _timeout):
            raise ContactConfigError("offline")

        cached = self.service(offline).get_contact_config()
        self.assertEqual("disabled", cached["status"])
        self.assertEqual("", cached["qr_image_url"])

    def test_non_https_remote_image_is_rejected(self):
        result = self.service(
            lambda _url, _timeout: configured_payload(
                url="http://cdn.example.com/contact.png"
            )
        ).get_contact_config()
        self.assertEqual("builtin", result["source"])
        self.assertTrue(result["use_builtin_image"])
        self.assertNotIn("http://cdn.example.com", result["qr_image_url"])

    def test_mismatched_app_name_is_rejected(self):
        result = self.service(
            lambda _url, _timeout: configured_payload(app_name="another-app")
        ).get_contact_config()
        self.assertEqual(APP_NAME, result["app_name"])
        self.assertEqual("builtin", result["source"])
        self.assertTrue(result["use_builtin_image"])

    def test_enabled_without_image_reports_missing_configuration(self):
        result = self.service(
            lambda _url, _timeout: configured_payload(url="")
        ).get_contact_config()
        self.assertEqual("missing_image", result["status"])
        self.assertEqual("联系方式图片暂未配置", result["message"])
        self.assertFalse(result["use_builtin_image"])

    def test_loopback_get_api_contact_and_preview(self):
        service = self.service(lambda _url, _timeout: configured_payload())
        server = ContactLocalHttpServer(service)
        try:
            server.start()
            self.assertTrue(server.contact_url.startswith("http://127.0.0.1:"))
            with urlopen(server.contact_url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual("*", response.headers["Access-Control-Allow-Origin"])
            self.assertEqual(APP_NAME, payload["app_name"])
            self.assertEqual(
                "https://cdn.example.com/contact.png",
                payload["display_image_url"],
            )
            with urlopen(server.preview_url, timeout=2) as response:
                preview = response.read().decode("utf-8")
                self.assertIn("联系作者", preview)
                self.assertIn("正在读取联系方式…", preview)
                self.assertNotIn("â¦", preview)
        finally:
            server.stop()

    def test_frontend_is_lazy_and_only_discovers_loopback_endpoint(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("sidebarContactSlot.addEventListener('mouseenter'", html)
        self.assertIn("sidebarContactSlot.addEventListener('focusin'", html)
        self.assertIn("sidebarContactBtn.addEventListener('click'", html)
        self.assertIn("api.getContactApiUrl()", html)
        self.assertIn("candidate.onload", html)
        self.assertNotIn("update.dadaozixun.com/api/contact", html)

    def test_user_guide_is_above_contact_and_opens_in_system_browser(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        tutorial = 'id="sidebarTutorialBtn"'
        contact = 'id="sidebarContactSlot"'
        self.assertIn(tutorial, html)
        self.assertLess(html.index(tutorial), html.index(contact))
        self.assertIn("使用教程", html)
        self.assertIn(
            "https://my.feishu.cn/docx/Lu9idLRe8o9cIFx71CGcI0cInox?from=from_copylink",
            html,
        )
        self.assertIn("api.openUrlInBrowser(USER_GUIDE_URL)", html)

    def test_contact_is_available_before_activation_and_large_enough_to_scan(self):
        index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        license_html = (ROOT / "static" / "license.html").read_text(encoding="utf-8")
        gui = (ROOT / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn('id="contactButton"', license_html)
        self.assertIn("getContactApiUrl", license_html)
        self.assertIn('"getContactApiUrl"', gui)
        self.assertIn("max-height: 460px", index_html)

    def test_windows_and_macos_manifests_include_contact_modules_and_image(self):
        windows = (ROOT / "packaging" / "windows" / "build_windows.ps1").read_text(encoding="utf-8")
        macos = (ROOT / "packaging" / "macos" / "build_macos.sh").read_text(encoding="utf-8")
        for manifest in (windows, macos):
            self.assertIn("services.contact_config", manifest)
            self.assertIn("services.contact_http", manifest)
            self.assertIn("contact-author-fallback.svg", manifest)
        self.assertIn('$appName = "QCSCKP"', windows)
        self.assertEqual("QCSCKP", APP_NAME)


if __name__ == "__main__":
    unittest.main()
