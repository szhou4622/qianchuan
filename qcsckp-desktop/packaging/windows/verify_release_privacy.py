"""Fail a Windows release build if it contains local user runtime data.

The executable contains code that knows *how* to configure Feishu and
Qianchuan.  It must never contain a developer's local configuration, DPAPI
ciphertext, bindings, database, cookies, tokens, logs, or history.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


EMPTY_RUNTIME_DIRS = {"data", "logs", "temp"}

# Known runtime artifacts.  Keep this list explicit so a dependency module
# named e.g. ``token_store.py`` is not mistaken for user data.
FORBIDDEN_NAMES = {
    ".env",
    "control_panel.json",
    "dashboard_config.json",
    "device_session.json",
    "feishu_local_profiles.json",
    "feishu_webhook_push.json",
    "last_crawl_target.json",
    "license_credentials.dpapi",
    "license_device_code.dpapi",
    "license_machine_code.dpapi",
    "license_metadata.json",
    "license_transport.json",
    "live_retarget_consumed.json",
    "operation_daily_report.json",
    "promotion_readonly_probe.json",
    "qcookie.json",
    "qcookie.legacy.rc23.json",
    "qianchuan.db",
    "qianchuan_open_api_token.json",
    "qianchuan_runtime_settings.json",
    "qianchuan_sessions.json",
}

FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite3",
    ".log",
}


def private_artifacts(release_root: Path) -> list[tuple[Path, str]]:
    root = release_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"release directory does not exist: {root}")

    artifacts: list[tuple[Path, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        parts = [part.lower() for part in relative.parts]
        name = path.name.lower()

        if parts and parts[0] in EMPTY_RUNTIME_DIRS:
            artifacts.append((path, f"runtime directory is not empty: {relative}"))
            continue
        if name in FORBIDDEN_NAMES or name.startswith(".env."):
            artifacts.append((path, f"private runtime artifact: {relative}"))
            continue
        if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            artifacts.append((path, f"database or log artifact: {relative}"))
            continue
        if name.endswith(".json") and "feishu" in name and any(
            marker in name
            for marker in ("profile", "config", "binding", "credential", "secret", "target")
        ):
            artifacts.append((path, f"possible Feishu credential artifact: {relative}"))

    return sorted(artifacts, key=lambda item: str(item[0]).lower())


def privacy_violations(release_root: Path) -> list[str]:
    return sorted({reason for _, reason in private_artifacts(release_root)})


def sanitize_release(release_root: Path) -> list[str]:
    """Remove user-specific artifacts from a staged release before zipping."""
    removed: list[str] = []
    root = release_root.resolve()
    for path, _ in private_artifacts(root):
        relative = str(path.relative_to(root))
        path.unlink()
        removed.append(relative)
    return removed


def verify_release(release_root: Path) -> None:
    violations = privacy_violations(release_root)
    if violations:
        details = os.linesep.join(f"- {item}" for item in violations)
        raise RuntimeError(
            "Release privacy verification failed. Remove all local user data before packaging:"
            f"{os.linesep}{details}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_root", type=Path)
    parser.add_argument(
        "--sanitize",
        action="store_true",
        help="remove private runtime artifacts before the final verification",
    )
    args = parser.parse_args()
    try:
        if args.sanitize:
            removed = sanitize_release(args.release_root)
            for item in removed:
                print(f"Excluded private runtime artifact: {item}")
        verify_release(args.release_root)
    except (OSError, RuntimeError) as exc:
        print(str(exc))
        return 1
    print(f"Release privacy verification passed: {args.release_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
