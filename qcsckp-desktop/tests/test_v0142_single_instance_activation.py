# -*- coding: utf-8 -*-
"""v0.1.42 single-instance window activation regression tests."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import gui_app
from gui_app import SingleInstanceChecker


class SingleInstanceActivationV0142Tests(unittest.TestCase):
    def test_existing_same_executable_is_activated(self):
        checker = SingleInstanceChecker("unused")
        process = Mock()
        process.info = {"pid": 9527, "exe": r"C:\Tool\QCSCKP.exe"}
        with (
            patch.object(gui_app, "PSUTIL_AVAILABLE", True),
            patch.object(gui_app.os, "getpid", return_value=100),
            patch.object(gui_app.sys, "executable", r"C:\Tool\QCSCKP.exe"),
            patch.object(
                gui_app.psutil,
                "process_iter",
                return_value=[process],
            ),
            patch.object(
                checker,
                "_activate_windows_process",
                return_value=True,
            ) as activated,
        ):
            result = checker.activate_existing_instance()

        self.assertTrue(result)
        activated.assert_called_once_with(9527)

    def test_lock_contention_writes_command_and_activates_existing_window(self):
        checker = SingleInstanceChecker("unused")
        handle = Mock()
        handle.read.return_value = b"1"
        handle.fileno.return_value = 123
        with (
            patch("gui_app.open", return_value=handle),
            patch("msvcrt.locking", side_effect=PermissionError("busy")),
            patch.object(checker, "_write_show_window_command") as command,
            patch.object(checker, "activate_existing_instance") as activated,
        ):
            result = checker.acquire_runtime_lease()

        self.assertFalse(result)
        handle.close.assert_called_once()
        command.assert_called_once()
        activated.assert_called_once()


if __name__ == "__main__":
    unittest.main()
