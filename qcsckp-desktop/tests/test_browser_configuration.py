"""Chrome configuration and failed-start recovery; no real browsers or API calls."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import control_panel_config as settings
from utils import common


class BrowserConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="chrome-config-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.public = self._chrome("public")
        self.override = self._chrome("override")
        patcher = patch.object(settings, "DATA_DIR", str(self.root / "data"))
        patcher.start()
        self.addCleanup(patcher.stop)
        settings.save_scrape_service_config(browser_executable_path=self.public)

    def _chrome(self, name):
        path = self.root / name / "chrome.exe"
        path.parent.mkdir()
        path.touch()
        return str(path)

    def test_empty_override_inherits_public_path_and_follows_changes(self):
        self.assertEqual(self.public, settings.effective_browser_executable_path())
        settings.save_scrape_service_config(browser_executable_path=self.override)
        self.assertEqual(self.override, settings.effective_browser_executable_path(""))

    def test_valid_override_wins_and_quotes_are_normalized(self):
        self.assertEqual(self.override, settings.effective_browser_executable_path('"' + self.override + '"'))

    def test_missing_override_and_legacy_edge_inherit_public_path(self):
        for value in (str(self.root / "missing" / "chrome.exe"), str(self.root / "msedge.exe")):
            with self.subTest(value=value):
                self.assertEqual(self.public, settings.effective_browser_executable_path(value))

    def test_missing_public_path_uses_auto_detection(self):
        settings.save_scrape_service_config(browser_executable_path=str(self.root / "missing/chrome.exe"))
        self.assertIsNone(settings.effective_browser_executable_path())
        with patch.object(common, "_chrome_candidates", return_value=[self.override]):
            self.assertEqual(self.override, common.require_executable_path(settings.effective_browser_executable_path()))

    def test_unrelated_manual_program_is_rejected_instead_of_ignored(self):
        other = self.root / "other.exe"
        other.touch()
        with self.assertRaisesRegex(FileNotFoundError, "重新选择chrome.exe"):
            common.require_executable_path(settings.effective_browser_executable_path(str(other)))

    def test_round_trip_preserves_other_control_settings(self):
        settings.save_scrape_service_config(interval_seconds=37, headless_poll=False, fetch_assist_tasks=True)
        settings.save_scrape_service_config(browser_executable_path=self.override)
        actual = settings.load_scrape_service_config()
        self.assertEqual(self.override, actual["browser_executable_path"])
        self.assertEqual(37, actual["interval_seconds"])
        self.assertFalse(actual["headless_poll"])
        self.assertTrue(actual["fetch_assist_tasks"])

    def test_failed_save_propagates_error_and_preserves_existing_file(self):
        before = Path(settings.unified_config_path()).read_bytes()
        with patch.object(settings.os, "replace", side_effect=PermissionError("test: file locked")):
            with self.assertRaises(PermissionError):
                settings.save_scrape_service_config(browser_executable_path=self.override)
        self.assertEqual(before, Path(settings.unified_config_path()).read_bytes())

    def test_rule_getters_preserve_inheritance_and_display_effective_path(self):
        from api.views import Api
        api = Api.__new__(Api)
        for method, loader in (
            (api.getRuleRetargetingConfig, "api.rule_retargeting_config.load_rule_retargeting_config"),
            (api.getRuleRegulationConfig, "api.rule_regulation_config.load_rule_regulation_config"),
        ):
            with self.subTest(method=method.__name__), patch(loader, return_value={"strategies": [], "browser_executable_path": ""}):
                result = method()
                self.assertEqual("", result["browser_executable_path"])
                self.assertEqual(self.public, result["browser_effective_path"])
            with patch(loader, return_value={"strategies": [], "browser_executable_path": self.override}):
                result = method()
                self.assertEqual(self.override, result["browser_executable_path"])
                self.assertEqual(self.override, result["browser_effective_path"])

    def test_official_status_reports_chrome_without_gating_api_collection(self):
        from services import official_api_controller as module
        controller = module.OfficialApiController()
        controller._running = True
        with patch.object(module, "official_api_session_status", return_value={"available": True}), patch.object(
            module, "official_api_catalog_status", return_value={}
        ), patch.object(module, "get_collection_queue_health", return_value={}):
            status = controller.status()
            self.assertEqual(self.public, status["browser"]["path"])
            self.assertTrue(status["browser"]["available"])
            self.assertIn("账户授权需要 Chrome", status["browser"]["phase"])
            self.assertTrue(status["running"])
            settings.save_scrape_service_config(browser_executable_path="")
            with patch.object(common, "_chrome_candidates", return_value=[]):
                status = controller.status()
            self.assertFalse(status["browser"]["available"])
            self.assertIn("chrome://version", status["browser"]["message"])
            self.assertTrue(status["running"])


class BrowserStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, kind):
        if kind == "collection":
            from services import fetcher as module
            instance = module.QianChuanFetcher(storage_state={"cookies": [], "origins": []})
            return module, instance, instance._init_browser, "playwright"
        if kind == "retargeting":
            from services import retargeting_service as module
            instance = module.QianChuanRetargetingService()
        else:
            from services import regulation_service as module
            instance = module.QianChuanRegulationStopService()
        instance._storage_state = {"cookies": [], "origins": []}
        return module, instance, instance._ensure_browser, "_playwright"

    def _driver(self):
        page = object()
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), add_init_script=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        driver = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
        starter = SimpleNamespace(start=AsyncMock(return_value=driver))
        return starter, driver, browser, context, page

    async def test_missing_executable_does_not_start_driver(self):
        for kind in ("collection", "retargeting", "regulation"):
            module, instance, initialize, attribute = self._session(kind)
            with self.subTest(kind=kind), patch.object(module, "async_playwright", create=True) as start, patch.object(
                module, "effective_browser_executable_path", return_value=None
            ), patch.object(module, "require_executable_path", side_effect=FileNotFoundError("missing chrome")):
                with self.assertRaises(FileNotFoundError):
                    await initialize()
                start.assert_not_called()
                self.assertIsNone(getattr(instance, attribute))

    async def test_failed_start_cleans_resources_and_can_retry(self):
        for kind in ("collection", "retargeting", "regulation"):
            for stage in ("driver", "launch", "context", "page", "cancel"):
                module, instance, initialize, attribute = self._session(kind)
                starter, driver, browser, context, page = self._driver()
                operation = {
                    "driver": starter.start, "launch": driver.chromium.launch,
                    "context": browser.new_context, "page": context.new_page,
                    "cancel": driver.chromium.launch,
                }[stage]
                error = asyncio.CancelledError() if stage == "cancel" else RuntimeError("test: " + stage)
                operation.side_effect = error
                with self.subTest(kind=kind, stage=stage), patch.object(
                    module, "async_playwright", return_value=starter, create=True
                ), patch.object(module, "effective_browser_executable_path", return_value="public/chrome.exe"), patch.object(
                    module, "require_executable_path", return_value="public/chrome.exe"
                ):
                    with self.assertRaises(type(error)):
                        await initialize()
                    if stage != "driver":
                        driver.stop.assert_awaited_once()
                    if stage in {"context", "page"}:
                        browser.close.assert_awaited_once()
                    self.assertIsNone(getattr(instance, attribute))
                    self.assertIsNone(instance.browser)
                    self.assertIsNone(instance.context)
                    self.assertIsNone(instance.page)
                    operation.side_effect = None
                    await initialize()
                    self.assertIs(instance.page, page)
                    await instance.close()
                    stops = driver.stop.await_count
                    await instance.close()
                    self.assertEqual(stops, driver.stop.await_count)

    async def test_browser_close_failure_still_stops_driver_and_keeps_start_error(self):
        for kind in ("collection", "retargeting", "regulation"):
            module, instance, initialize, attribute = self._session(kind)
            starter, driver, browser, context, page = self._driver()
            browser.new_context.side_effect = RuntimeError("original start error")
            browser.close.side_effect = RuntimeError("cleanup error")
            with self.subTest(kind=kind), patch.object(module, "async_playwright", return_value=starter, create=True), patch.object(
                module, "effective_browser_executable_path", return_value=None
            ), patch.object(module, "require_executable_path", return_value="chrome.exe"):
                with self.assertRaisesRegex(RuntimeError, "original start error"):
                    await initialize()
                driver.stop.assert_awaited_once()
                self.assertIsNone(getattr(instance, attribute))

    async def test_three_browser_flows_inherit_public_path_and_allow_overrides(self):
        with tempfile.TemporaryDirectory(prefix="chrome-flow-") as directory:
            root = Path(directory)
            public = root / "public" / "chrome.exe"
            override = root / "override" / "chrome.exe"
            for path in (public, override):
                path.parent.mkdir()
                path.touch()
            with patch.object(settings, "DATA_DIR", str(root / "data")):
                settings.save_scrape_service_config(browser_executable_path=str(public))
                for kind in ("collection", "retargeting", "regulation"):
                    values = (None,) if kind == "collection" else (None, str(override), str(root / "missing/chrome.exe"))
                    for value in values:
                        module, instance, initialize, attribute = self._session(kind)
                        if kind != "collection":
                            instance._opt.browser_executable_path = value
                        starter, driver, browser, context, page = self._driver()
                        with self.subTest(kind=kind, override=value), patch.object(
                            module, "async_playwright", return_value=starter, create=True
                        ):
                            await initialize()
                            expected = str(override) if value == str(override) else str(public)
                            self.assertEqual(expected, driver.chromium.launch.call_args.kwargs["executable_path"])
                            await instance.close()


if __name__ == "__main__":
    unittest.main()
