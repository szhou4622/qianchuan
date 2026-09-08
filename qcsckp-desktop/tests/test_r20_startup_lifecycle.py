"""Startup failures, readiness races and tightly scoped dependency repair."""
import ast
from contextlib import ExitStack
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import startup_bootstrap as bootstrap


class ManualThread:
    created = []

    def __init__(self, target, **kwargs):
        self.target = target
        self.created.append(self)

    def start(self):
        pass


class StartupLifecycleTests(unittest.TestCase):
    def setUp(self):
        bootstrap.begin_startup_attempt()
        ManualThread.created = []
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(bootstrap, "_write_startup_phase"))
        self.stack.enter_context(patch.object(bootstrap, "startup_log"))
        self.stack.enter_context(patch.object(bootstrap, "install_exception_hooks"))
        self.stack.enter_context(patch.object(bootstrap, "validate_package_integrity", return_value=[]))
        self.stack.enter_context(patch.object(bootstrap, "ensure_managed_dependencies"))
        self.stack.enter_context(patch.object(bootstrap, "ensure_webview2"))
        self.stack.enter_context(patch.object(bootstrap, "_record_diagnostic_event"))
        self.native = self.stack.enter_context(patch.object(bootstrap, "native_message"))

    def tearDown(self):
        self.stack.close()
        bootstrap.begin_startup_attempt()

    def test_loader_failure_cancels_watchdog_before_primary_dialog(self):
        window = SimpleNamespace(destroy=Mock())
        def main():
            bootstrap.start_window_watchdog(window)
            raise RuntimeError("Python.Runtime.Loader.Initialize failed")
        def message(*args, **kwargs):
            self.assertEqual("failed", bootstrap.startup_state()["terminal"])
            ManualThread.created[0].target()
        self.native.side_effect = message
        with patch.object(bootstrap.threading, "Thread", ManualThread), patch.dict(
            sys.modules, {"gui_app": SimpleNamespace(main=main)}
        ):
            self.assertEqual(1, bootstrap._main_impl())
        self.native.assert_called_once()
        window.destroy.assert_not_called()
        self.assertFalse(bootstrap.window_ready_was_reached())

    def test_timeout_clean_window_return_is_nonzero_and_late_loaded_cannot_override(self):
        close = Mock()
        def main():
            bootstrap.start_window_watchdog(SimpleNamespace(destroy=Mock()), close_window=close)
            with patch.object(bootstrap._WATCHDOG_DONE, "wait", return_value=False):
                ManualThread.created[0].target()
            bootstrap.mark_window_ready()
            bootstrap.mark_normal_exit()
        with patch.object(bootstrap.threading, "Thread", ManualThread), patch.dict(
            sys.modules, {"gui_app": SimpleNamespace(main=main)}
        ):
            self.assertEqual(1, bootstrap._main_impl())
        self.assertEqual("window_timeout", bootstrap.startup_state()["terminal"])
        self.assertFalse(bootstrap.window_ready_was_reached())
        close.assert_called_once()
        self.native.assert_called_once()

    def test_concurrent_ready_and_failure_always_preserves_failure_terminal(self):
        for _ in range(20):
            generation = bootstrap.begin_startup_attempt()
            barrier = threading.Barrier(2)
            def ready():
                barrier.wait()
                bootstrap.mark_window_ready(generation)
            def failed():
                barrier.wait()
                bootstrap.mark_startup_phase("failed", "original failure")
            threads = [threading.Thread(target=ready), threading.Thread(target=failed)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual("failed", bootstrap.startup_state()["terminal"])
            bootstrap.mark_normal_exit()
            self.assertEqual("failed", bootstrap.startup_state()["phase"])

    def test_previous_generation_loaded_is_ignored(self):
        old = bootstrap.begin_startup_attempt()
        bootstrap.begin_startup_attempt()
        bootstrap.mark_window_ready(old)
        self.assertFalse(bootstrap.window_ready_was_reached())

    def test_main_entry_alias_reuses_module_state_for_gui_import(self):
        path = Path(bootstrap.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Do not execute the entrypoint. All other top-level code only defines
        # standard-library state and functions, without GUI/business startup.
        tree.body = [node for node in tree.body if not (isinstance(node, ast.If)
                     and any(isinstance(child, ast.Raise) for child in node.body))]
        module = ModuleType("__main__")
        module.__file__ = str(path)
        with patch.dict(sys.modules, {"__main__": module, "startup_bootstrap": bootstrap}):
            exec(compile(tree, str(path), "exec"), module.__dict__)
            imported = importlib.import_module("startup_bootstrap")
            self.assertIs(module, imported)
            self.assertIs(module._WATCHDOG_DONE, imported._WATCHDOG_DONE)

    def test_error_codes_are_not_conflated(self):
        blocked = bootstrap.describe_startup_exception(RuntimeError("0x80131515"))
        com = bootstrap.describe_startup_exception(RuntimeError("0x8001010d"))
        self.assertIn("阻止", blocked)
        self.assertIn("COM", com)
        self.assertNotIn("下载标记", com)

    def test_startup_state_exports_only_public_configuration_fingerprint(self):
        contract = {"software_contract_sha256": "a" * 64, "policy_version": 3,
                    "packaged_policy_enforced": True, "defaults": {"secret": "never-export"},
                    "unexpected_private_value": "also-never-export"}
        with patch("release_configuration.public_runtime_contract", return_value=contract):
            state = bootstrap._state_payload("ready")
        self.assertEqual("a" * 64, state["software_contract_sha256"])
        self.assertEqual(3, state["configuration_policy_version"])
        self.assertTrue(state["packaged_policy_enforced"])
        encoded = json.dumps(state)
        self.assertNotIn("never-export", encoded)
        self.assertNotIn("defaults", state)

    def test_winforms_actions_are_deferred_through_native_ui_queue(self):
        path = Path(bootstrap.__file__).parent / "gui_app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "dispatch_window_action")
        namespace = {"sys": SimpleNamespace(platform="win32"), "threading": threading,
                     "startup_log": Mock(), "describe_startup_exception": str}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
        window = SimpleNamespace(native=SimpleNamespace(BeginInvoke=Mock()))
        action = Mock()
        with patch.dict(sys.modules, {"System": SimpleNamespace(Action=lambda callback: callback)}):
            self.assertTrue(namespace["dispatch_window_action"](window, action))
        action.assert_not_called()
        window.native.BeginInvoke.call_args.args[0]()
        action.assert_called_once()

    def test_tray_failure_does_not_prevent_timeout_close_dispatch(self):
        path = Path(bootstrap.__file__).parent / "gui_app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                    and node.name == "close_failed_startup")
        tray = SimpleNamespace(force_close=False, icon=SimpleNamespace(stop=Mock(side_effect=RuntimeError("tray failed"))))
        window = SimpleNamespace(destroy=Mock())
        dispatch = Mock(return_value=True)
        namespace = {"tray_app": tray, "window": window, "dispatch_window_action": dispatch, "startup_log": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
        namespace["close_failed_startup"]()
        self.assertTrue(tray.force_close)
        dispatch.assert_called_once_with(window, window.destroy)


class DependencyRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qcsckp-r20-deps-")
        self.root = Path(self.temp.name)
        self.entries = []
        for name in bootstrap.REQUIRED_MANAGED_DEPENDENCIES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = ("synthetic dependency " + name).encode()
            target.write_bytes(payload)
            self.entries.append({"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        (self.root / "PACKAGE-MANIFEST.json").write_text(json.dumps({"app_name": "QCSCKP", "critical_files": self.entries}), encoding="utf-8")
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(bootstrap, "_app_root", return_value=self.root))
        self.stack.enter_context(patch.object(bootstrap.sys, "platform", "win32"))
        self.stack.enter_context(patch.object(bootstrap.sys, "frozen", True, create=True))
        self.stack.enter_context(patch.object(bootstrap, "startup_log"))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_confirmed_repair_only_removes_named_verified_dependency_stream(self):
        blocked = self.root / self.entries[0]["path"]
        zones = {str(blocked): 3}
        def unlink(stream):
            self.assertEqual(str(blocked) + ":Zone.Identifier", str(stream))
            zones[str(blocked)] = None
        with patch.object(bootstrap, "_download_zone", side_effect=lambda path: zones.get(str(path))), patch.object(
            bootstrap, "native_confirm", return_value=True
        ) as confirm, patch.object(Path, "unlink", autospec=True, side_effect=unlink) as remove:
            bootstrap.ensure_managed_dependencies()
        confirm.assert_called_once()
        remove.assert_called_once()
        self.assertTrue(blocked.is_file())

    def test_hash_mismatch_blocks_before_user_prompt_or_repair(self):
        (self.root / self.entries[0]["path"]).write_bytes(b"changed")
        with patch.object(bootstrap, "native_confirm") as confirm, patch.object(Path, "unlink") as remove:
            with self.assertRaises(bootstrap.StartupAbort):
                bootstrap.ensure_managed_dependencies()
        confirm.assert_not_called()
        remove.assert_not_called()

    def test_change_during_user_confirmation_cannot_be_unblocked(self):
        target = self.root / self.entries[0]["path"]
        def confirm(*args, **kwargs):
            target.write_bytes(b"replacement")
            return True
        with patch.object(bootstrap, "_download_zone", return_value=3), patch.object(
            bootstrap, "native_confirm", side_effect=confirm
        ), patch.object(Path, "unlink") as remove:
            with self.assertRaises(bootstrap.StartupAbort):
                bootstrap.ensure_managed_dependencies()
        remove.assert_not_called()

    def test_declined_confirmation_never_unblocks(self):
        with patch.object(bootstrap, "_download_zone", return_value=3), patch.object(
            bootstrap, "native_confirm", return_value=False
        ), patch.object(Path, "unlink") as remove:
            with self.assertRaises(bootstrap.StartupAbort):
                bootstrap.ensure_managed_dependencies()
        remove.assert_not_called()

    def test_missing_required_dependency_manifest_entry_fails_closed(self):
        (self.root / "PACKAGE-MANIFEST.json").write_text(json.dumps({"critical_files": self.entries[1:]}), encoding="utf-8")
        with self.assertRaises(bootstrap.StartupAbort):
            bootstrap.ensure_managed_dependencies()

    def test_manifest_root_bin_duplicate_is_repaired_but_unlisted_copy_is_untouched(self):
        duplicate = self.root / "bin/Python.Runtime.dll"
        payload = b"root-bin dependency"
        duplicate.write_bytes(payload)
        self.entries.append({"path": "bin/Python.Runtime.dll", "size": len(payload),
                             "sha256": hashlib.sha256(payload).hexdigest()})
        (self.root / "PACKAGE-MANIFEST.json").write_text(json.dumps({"critical_files": self.entries}), encoding="utf-8")
        unlisted = self.root / "bin/unlisted/Python.Runtime.dll"
        unlisted.parent.mkdir()
        unlisted.write_bytes(b"not in the manifest")
        zones = {str(duplicate): 3, str(unlisted): 3}
        def unlink(stream):
            self.assertEqual(str(duplicate) + ":Zone.Identifier", str(stream))
            zones[str(duplicate)] = None
        with patch.object(bootstrap, "_download_zone", side_effect=lambda path: zones.get(str(path))) as inspect, patch.object(
            bootstrap, "native_confirm", return_value=True
        ), patch.object(Path, "unlink", autospec=True, side_effect=unlink) as remove:
            bootstrap.ensure_managed_dependencies()
        remove.assert_called_once()
        self.assertNotIn(unlisted, [call.args[0] for call in inspect.call_args_list])
        self.assertEqual(3, zones[str(unlisted)])
