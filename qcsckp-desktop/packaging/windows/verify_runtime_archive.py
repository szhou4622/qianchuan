"""Read the final executable's PYZ without importing or starting application code.

The expected module list is a literal owned by the runtime supervisor. Reading
its AST does not import config, credentials, database adapters or services.
This is an archive-completeness check, not a substitute for activation smoke tests.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from types import CodeType
from typing import Any, Callable, Sequence

MANIFEST_NAME = "RUNTIME_MODULE_MANIFEST"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "services" / "runtime_supervisor.py"
_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


def expected_runtime_modules(manifest: Path = DEFAULT_MANIFEST) -> tuple[str, ...]:
    """Load exactly one static top-level manifest; never execute its source."""
    tree = ast.parse(Path(manifest).read_text(encoding="utf-8-sig"))
    values = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == MANIFEST_NAME for target in targets):
            values.append(ast.literal_eval(node.value))
    if len(values) != 1:
        raise ValueError("Runtime manifest must have exactly one top-level literal definition")
    modules = values[0]
    if not isinstance(modules, (tuple, list)) or not modules:
        raise ValueError("Runtime manifest must be a non-empty list or tuple")
    if any(not isinstance(name, str) or not _MODULE_NAME.fullmatch(name) for name in modules):
        raise ValueError("Runtime manifest contains an invalid module name")
    if len(set(modules)) != len(modules):
        raise ValueError("Runtime manifest contains duplicate modules")
    return tuple(modules)


def verify_runtime_archive(
    executable: Path,
    expected_modules: Sequence[str],
    *,
    reader_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Open the embedded archives in place; no extraction to disk or imports."""
    expected = tuple(expected_modules)
    if not expected or len(set(expected)) != len(expected) or any(
            not isinstance(name, str) or not _MODULE_NAME.fullmatch(name) for name in expected):
        raise ValueError("Invalid or empty expected module list")
    if reader_factory is None:
        from PyInstaller.archive.readers import CArchiveReader
        reader_factory = CArchiveReader
    archive = reader_factory(str(executable))
    embedded = {name: archive.open_embedded_archive(name)
                for name, entry in archive.toc.items() if entry[-1] == "z"}
    if not embedded:
        raise ValueError("Final executable does not contain an embedded PYZ archive")
    locations: dict[str, list[Any]] = {}
    for reader in embedded.values():
        for name in reader.toc:
            locations.setdefault(name, []).append(reader)
    missing = sorted(set(expected) - locations.keys())
    parents = {name.rsplit(".", 1)[0] for name in expected if "." in name}
    missing_parents = sorted(parents - locations.keys())
    invalid = []
    for name in expected:
        for reader in locations.get(name, []):
            try:
                # marshal decoding produces a code object; it does not execute
                # the module or load any dependency referenced by that code.
                if not isinstance(reader.extract(name), CodeType):
                    raise TypeError("Registry entry is not a Python code module")
            except (Exception, SystemExit) as exc:
                invalid.append({"module": name, "error_type": type(exc).__name__})
    return {
        "success": not (missing or missing_parents or invalid),
        "expected_module_count": len(expected),
        "expected_modules": list(expected),
        "embedded_archives": sorted(embedded),
        "archived_module_count": len(locations),
        "missing_modules": missing,
        "missing_parent_packages": missing_parents,
        "invalid_modules": invalid,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, help="Final staged QCSCKP.exe, never a build-directory PYZ")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="Runtime supervisor source containing the literal module manifest")
    args = parser.parse_args(argv)
    try:
        report = verify_runtime_archive(args.executable, expected_runtime_modules(args.manifest))
    except (Exception, SystemExit) as exc:
        print(json.dumps({"success": False, "verification_error": type(exc).__name__}, ensure_ascii=True))
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

