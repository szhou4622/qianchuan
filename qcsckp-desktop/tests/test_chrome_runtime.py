# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from utils import common


class ChromeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.registry = SimpleNamespace(
            HKEY_CURRENT_USER="user", HKEY_LOCAL_MACHINE="machine",
            KEY_READ=1, KEY_WOW64_64KEY=256, KEY_WOW64_32KEY=512,
            OpenKey=MagicMock(side_effect=FileNotFoundError),
            QueryValueEx=MagicMock(),
        )
        for patcher in (
            patch.dict(common.sys.modules, {"winreg": self.registry}),
            patch.object(common.sys, "platform", "win32"),
            patch.object(common.shutil, "which", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_windows_auto_detection_never_falls_back_to_edge(self):
        def exists(path: str) -> bool:
            return "Microsoft/Edge" in path or "Microsoft\\Edge" in path

        with patch.object(common.os.path, "isfile", side_effect=exists):
            with self.assertRaisesRegex(FileNotFoundError, "Google Chrome"):
                common.require_executable_path()

    def test_legacy_edge_path_is_ignored_and_chrome_is_still_required(self):
        edge = os.path.abspath("msedge.exe")
        with patch.object(common.os.path, "isfile", side_effect=lambda path: path == edge):
            with self.assertRaisesRegex(FileNotFoundError, "Google Chrome"):
                common.require_executable_path(edge)

    def test_runtime_info_reports_the_actual_chrome_path(self):
        chrome = os.path.abspath("chrome.exe")
        with patch.object(common.os.path, "isfile", return_value=True):
            info = common.browser_runtime_info(chrome)
        self.assertTrue(info["available"])
        self.assertTrue(info["is_chrome"])
        self.assertEqual("Google Chrome", info["name"])
        self.assertEqual(os.path.normpath(chrome), info["path"])

    def test_custom_install_from_each_registry_hive_and_view(self):
        chrome = os.path.abspath("custom-install/Chrome/Application/chrome.exe")
        for hive in ("user", "machine"):
            for view in (0, 256, 512):
                with self.subTest(hive=hive, view=view):
                    def open_key(actual_hive, key, reserved, access):
                        if actual_hive != hive or access != 1 | view:
                            raise FileNotFoundError
                        self.assertEqual(
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", key
                        )
                        handle = MagicMock()
                        handle.__enter__.return_value = "chrome-key"
                        return handle

                    self.registry.OpenKey.side_effect = open_key
                    self.registry.QueryValueEx.return_value = ('"' + chrome + '"', 1)
                    with patch.object(common.os.path, "isfile", side_effect=lambda path: path == chrome):
                        self.assertEqual(chrome, common.require_executable_path())
                        self.assertEqual(chrome, common.default_browser_executable_hint())

    def test_redirected_installation_directories(self):
        for variable in ("LOCALAPPDATA", "ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            with self.subTest(variable=variable):
                root = os.path.abspath("redirected-" + variable)
                chrome = os.path.normpath(os.path.join(root, "Google/Chrome/Application/chrome.exe"))
                with patch.dict(os.environ, {variable: root}), patch.object(
                    common.os.path, "isfile", side_effect=lambda path: path == chrome
                ):
                    self.assertEqual(chrome, common.require_executable_path())

    def test_portable_chrome_on_path(self):
        chrome = os.path.abspath("portable/chrome.exe")
        with patch.object(common.shutil, "which", side_effect=lambda name: chrome if name == "chrome.exe" else None), patch.object(
            common.os.path, "isfile", side_effect=lambda path: path == chrome
        ):
            self.assertEqual(chrome, common.require_executable_path())

    def test_stale_config_falls_back_to_installed_chrome(self):
        chrome = os.path.abspath("installed/chrome.exe")
        missing = os.path.abspath("old-install/chrome.exe")
        with patch.object(common.shutil, "which", return_value=chrome), patch.object(
            common.os.path, "isfile", side_effect=lambda path: path == chrome
        ):
            self.assertEqual(chrome, common.require_executable_path(missing))
            self.assertEqual("", common.configured_chrome_path_or_empty(missing))
            self.assertEqual(chrome, common.default_browser_executable_hint())

    def test_manual_path_accepts_quotes_and_environment_variables(self):
        root = os.path.abspath("manual-install")
        chrome = os.path.join(root, "chrome.exe")
        with patch.dict(os.environ, {"QCSCKP_TEST_CHROME_DIR": root}), patch.object(
            common.os.path, "isfile", side_effect=lambda path: path == chrome
        ):
            value = '"${QCSCKP_TEST_CHROME_DIR}/chrome.exe"'
            self.assertEqual(chrome, common.require_executable_path(value))
            self.assertEqual(chrome, common.configured_chrome_path_or_empty(value))

    def test_valid_manual_path_takes_priority(self):
        chrome = os.path.abspath("manual/chrome.exe")
        with patch.object(common.os.path, "isfile", side_effect=lambda path: path == chrome):
            self.assertEqual(chrome, common.require_executable_path(chrome))
        self.registry.OpenKey.assert_not_called()

    def test_non_chrome_manual_path_is_rejected(self):
        with patch.object(common.os.path, "isfile", return_value=True):
            with self.assertRaisesRegex(FileNotFoundError, "重新选择chrome.exe"):
                common.require_executable_path(os.path.abspath("other.exe"))

    def test_missing_chrome_does_not_prefill_nonexistent_path(self):
        with patch.object(common.os.path, "isfile", return_value=False):
            self.assertEqual("", common.default_browser_executable_hint())
            info = common.browser_runtime_info()
            self.assertFalse(info["available"])
            self.assertIn("chrome://version", info["message"])

    def test_registry_entries_must_point_to_an_existing_chrome_file(self):
        directory = os.path.abspath("directory/chrome.exe")
        other = os.path.abspath("other.exe")
        with patch.object(common, "_windows_chrome_registry_paths", return_value=[directory, other]), patch.object(
            common.os.path, "isfile", side_effect=lambda path: path == other
        ):
            with self.assertRaises(FileNotFoundError):
                common.require_executable_path()

    def test_macos_detection_is_preserved(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with patch.object(common.sys, "platform", "darwin"), patch.object(
            common.os.path, "abspath", side_effect=lambda path: path
        ), patch.object(common.os.path, "isfile", side_effect=lambda path: path == chrome):
            self.assertEqual(chrome, common.require_executable_path())
            self.assertEqual(chrome, common.default_browser_executable_hint())
        self.registry.OpenKey.assert_not_called()


class OAuthChromeConfigurationTests(unittest.TestCase):
    def test_authorization_browser_uses_saved_path_or_auto_detection(self):
        from services.qianchuan_open_api import token_provider

        for value, expected in ((" D:/portable/chrome.exe ", "D:/portable/chrome.exe"), ("", None)):
            with self.subTest(configured_path=value), patch(
                "services.control_panel_config.load_scrape_service_config",
                return_value={"browser_executable_path": value},
            ), patch.object(common.os.path, "isfile", return_value=True), patch.object(common, "require_executable_path", return_value="resolved/chrome.exe") as resolve, patch(
                "playwright.sync_api.sync_playwright"
            ) as playwright, patch.object(
                token_provider.threading, "Thread"
            ) as thread, patch.object(token_provider, "_remember_oauth_browser_error") as remember_error:
                # Run the worker synchronously and stop before any real browser or network activity.
                thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
                browser = playwright.return_value.__enter__.return_value.chromium
                browser.launch.side_effect = RuntimeError("test: no real browser")
                self.assertTrue(token_provider.open_oauth_authorization_browser(
                    "https://example.invalid/authorize", "test-chrome-config"
                ))
                resolve.assert_called_once_with(common._normalize_browser_path(expected) if expected else None)
                browser.launch.assert_called_once_with(
                    executable_path="resolved/chrome.exe", headless=False, args=["--start-maximized"]
                )
                remember_error.assert_called_once_with("test-chrome-config", "test: no real browser")
                self.assertNotIn("test-chrome-config", token_provider._oauth_browser_threads)


if __name__ == "__main__":
    unittest.main()
