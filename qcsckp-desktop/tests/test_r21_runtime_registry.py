"""Registry resolution is static for packaging, lazy and read-only at runtime."""
import ast
import builtins
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from services import runtime_supervisor as runtime


class RuntimeRegistryTests(unittest.TestCase):
    def test_all_eleven_services_share_runtime_and_public_manifest(self):
        with patch.object(runtime, "QIANCHUAN_BACKEND", "official_api"):
            specs = runtime.BackgroundRuntimeSupervisor()._build_service_specs(None)
        manifest = runtime.runtime_component_manifest()
        self.assertTrue(manifest["success"])
        self.assertEqual(["prune", "webhook", "feishu", "retarget_rules", "retarget_tasks", "operation_logs",
                          "daily_report", "stop_rules", "reconciliation", "catalog", "collection"],
                         [spec.name for spec in specs])
        self.assertEqual([spec.name for spec in specs], [item["name"] for item in manifest["services"]])
        self.assertEqual([False] * 3 + [True] * 8, [spec.watched for spec in specs])
        self.assertEqual(list(runtime.RUNTIME_MODULE_MANIFEST), manifest["modules"])
        for spec, item in zip(specs, manifest["services"]):
            self.assertEqual(spec.start.__name__, item["start"]["function"])
            self.assertEqual(spec.stop.__name__, item["stop"]["function"])
            self.assertTrue(item["start"]["callable"])
            self.assertTrue(item["stop"]["callable"])
            self.assertEqual(spec.observe is not None, item["observe"] is not None)
        self.assertIsNotNone(manifest["services"][6]["observe"])

    def test_manifest_does_not_start_stop_or_construct_workers(self):
        specs = runtime._resolve_service_specs(backend="official_api")
        with ExitStack() as stack:
            callbacks = []
            for spec in specs:
                for callback in (spec.start, spec.stop):
                    spy = Mock(side_effect=AssertionError("registry must never execute a callback"))
                    spy.__module__, spy.__name__ = callback.__module__, callback.__name__
                    stack.enter_context(patch(callback.__module__ + "." + callback.__name__, spy))
                    callbacks.append(spy)
            thread_start = stack.enter_context(patch("threading.Thread.start", side_effect=AssertionError("registry started a thread")))
            manifest = runtime.runtime_component_manifest()
        self.assertEqual(11, len(manifest["services"]))
        for callback in callbacks:
            callback.assert_not_called()
        thread_start.assert_not_called()

    def test_service_builder_delegates_to_single_resolver(self):
        specs, bridge = [], object()
        with patch.object(runtime, "_resolve_service_specs", return_value=specs) as resolve:
            self.assertIs(specs, runtime.BackgroundRuntimeSupervisor()._build_service_specs(bridge))
        resolve.assert_called_once_with(bridge)

    def test_invalid_callback_reports_its_registry_role(self):
        with patch.object(runtime, "_resolve_service_specs", return_value=[runtime.ServiceSpec("broken", None, lambda: None)]):
            with self.assertRaisesRegex(TypeError, "broken.start"):
                runtime.runtime_component_manifest()

    def test_missing_packaged_module_name_is_preserved_exactly(self):
        for missing in (runtime.RUNTIME_MODULE_MANIFEST[0], runtime.RUNTIME_MODULE_MANIFEST[-1]):
            with self.subTest(missing=missing), patch.dict(sys.modules, {missing: None}):
                with self.assertRaises(ModuleNotFoundError) as caught:
                    runtime.runtime_component_manifest()
            self.assertEqual(missing, caught.exception.name)

    def test_missing_transitive_dependency_is_not_misreported_as_top_level_service(self):
        original_import = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == "utils.sqlite_prune_scheduler":
                raise ModuleNotFoundError("No module named 'synthetic_dependency'", name="synthetic_dependency")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=importing):
            with self.assertRaises(ModuleNotFoundError) as caught:
                runtime.runtime_component_manifest()
        self.assertEqual("synthetic_dependency", caught.exception.name)

    def test_literal_manifest_matches_explicit_lazy_imports(self):
        source = Path(runtime.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "RUNTIME_MODULE_MANIFEST" for target in node.targets))
        manifest = ast.literal_eval(assignment.value)
        resolver = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_resolve_service_specs")
        static_imports = {node.module for node in ast.walk(resolver) if isinstance(node, ast.ImportFrom)}
        self.assertTrue(set(manifest).issubset(static_imports))
        top_level_imports = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
        self.assertTrue(set(manifest).isdisjoint(top_level_imports))
        self.assertNotIn("importlib.import_module", source)


if __name__ == "__main__":
    unittest.main()
