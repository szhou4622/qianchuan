"""Fail a release when duplicate SDK sources or long archive paths return."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

MAX_ARCHIVE_PATH = 110
SDK_MODULES = (
    "lark_oapi", "lark_oapi.ws.client",
    "lark_oapi.event.callback.model.p2_card_action_trigger",
    "baseopensdk", "baseopensdk.api.base.v1",
)


def verify_layout(root: Path, *, modules=None, max_archive_path=MAX_ARCHIVE_PATH):
    root = root.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ValueError("Empty release")
    archive_paths = []
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Linked path outside release")
        relative = path.relative_to(root)
        if (len(relative.parts) > 1 and relative.parts[:2] in (("bin", "lark_oapi"), ("bin", "baseopensdk"))
                and path.suffix.lower() in {".py", ".pyc"}):
            raise ValueError("Redundant SDK source: " + relative.as_posix())
        archive_paths.append(root.name + "/" + relative.as_posix())
    longest = max(archive_paths, key=lambda name: len(name.encode("utf-16-le")) // 2)
    longest_length = len(longest.encode("utf-16-le")) // 2
    if longest_length > max_archive_path:
        raise ValueError(f"Archive path exceeds {max_archive_path} characters: {longest}")
    if modules is None:
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(root / "QCSCKP.exe"))
        modules = set()
        for name, entry in archive.toc.items():
            if entry[-1] == "z":
                modules.update(archive.open_embedded_archive(name).toc)
    missing = sorted(set(SDK_MODULES) - set(modules))
    if missing:
        raise ValueError("Required archived SDK modules missing: " + ", ".join(missing))
    return {"success": True, "package_root": root.name, "scanned_files": len(files),
            "longest_archive_path": longest, "longest_archive_path_characters": longest_length,
            "archive_path_budget": max_archive_path, "external_sdk_sources": 0,
            "required_archived_sdk_modules": list(SDK_MODULES)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify_layout(args.release_root)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
