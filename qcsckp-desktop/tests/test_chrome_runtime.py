# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from utils import common


class ChromeRuntimeTests(unittest.TestCase):
    def test_windows_auto_detection_never_falls_back_to_edge(self):
        def exists(path: str) -> bool:
            return "Microsoft/Edge" in path or "Microsoft\\Edge" in path

        with patch.object(common.sys, "platform", "win32"), patch.object(
            common.os.path, "exists", side_effect=exists
        ):
            with self.assertRaisesRegex(FileNotFoundError, "Google Chrome"):
                common.require_executable_path()

    def test_legacy_edge_path_is_ignored_and_chrome_is_still_required(self):
        edge = os.path.abspath("msedge.exe")
        with patch.object(common.os.path, "isfile", return_value=True), patch.object(
            common.os.path, "exists", return_value=False
        ):
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


if __name__ == "__main__":
    unittest.main()
