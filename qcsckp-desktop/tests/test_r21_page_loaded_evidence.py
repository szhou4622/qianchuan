"""Read-only page evidence tested without importing or starting a GUI."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class PageLoadedEvidenceTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "gui_app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "handle_window_loaded")
        self.mark = Mock()
        self.log = Mock()
        self.state = {"generation": 4, "terminal": "", "ready": True}
        namespace = {"mark_window_ready": self.mark, "startup_log": self.log, "startup_state": lambda: self.state}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        self.loaded = namespace["handle_window_loaded"]

    def test_index_and_license_emit_only_approved_local_basename_after_ready(self):
        for name in ("license.html", "index.html"):
            with self.subTest(name=name):
                order = []
                self.mark.side_effect = lambda generation: order.append("ready")
                self.log.side_effect = lambda message: order.append(message)
                def location():
                    order.append("read")
                    return "file:///C:/Users/Private%20Name/app/" + name + "?token=secret#license-data"
                self.loaded(SimpleNamespace(get_current_url=location), 4)
                self.assertEqual(["ready", "read", "page_loaded=" + name], order)
                self.assertNotIn("secret", " ".join(order))
                self.assertNotIn("Private", " ".join(order))

    def test_remote_network_and_unapproved_pages_are_not_logged(self):
        for url in ("https://example.test/index.html", "file://network-share/app/index.html", "file:///C:/private/billing.html"):
            with self.subTest(url=url):
                self.log.reset_mock()
                self.loaded(SimpleNamespace(get_current_url=lambda: url), 4)
                self.log.assert_not_called()

    def test_url_failure_does_not_reverse_ready_and_logs_type_only(self):
        window = SimpleNamespace(get_current_url=Mock(side_effect=RuntimeError("secret-token /private/path")))
        self.loaded(window, 4)
        self.mark.assert_called_once_with(4)
        self.log.assert_called_once_with("page_loaded_read_failed_type=RuntimeError")
        self.assertTrue(self.state["ready"])

    def test_terminal_or_stale_loaded_callback_cannot_emit_success_evidence(self):
        window = SimpleNamespace(get_current_url=Mock(return_value="file:///C:/app/index.html"))
        self.state["terminal"] = "failed"
        self.loaded(window, 4)
        self.state["terminal"] = ""
        self.loaded(window, 3)
        window.get_current_url.assert_not_called()
        self.log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
