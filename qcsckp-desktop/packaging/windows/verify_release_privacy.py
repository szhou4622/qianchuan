"""Fail a Windows release build if it contains local user runtime data.

The executable contains code that knows *how* to configure Feishu and
Qianchuan.  It must never contain a developer's local configuration, DPAPI
ciphertext, bindings, database, cookies, tokens, logs, or history.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


EMPTY_RUNTIME_DIRS = {"data", "logs", "temp"}
PRIVATE_RUNTIME_ROOTS = EMPTY_RUNTIME_DIRS | {
    "storage", "cache", "startup-state", "diagnostics", "channels", "shared-v1", "official-api-v1",
}
PUBLIC_PEM_PATHS = {"bin/certifi/cacert.pem"}
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----", b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
PRIVATE_JSON_FIELDS = {"access_token", "refresh_token", "app_secret", "device_credential",
                       "device_session", "activation_code", "private_key", "password_hash"}

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
    "activation_codes.json",
    "license_codes.json",
    "secrets.json",
    "credentials.json",
    "local_test_secrets.json",
    "session_owner.json",
}

FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".dpapi",
    ".key",
    ".pfx",
    ".p12",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite3-wal",
    ".sqlite3-shm",
}


def private_artifacts(release_root: Path) -> list[tuple[Path, str]]:
    root = release_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"release directory does not exist: {root}")

    artifacts: list[tuple[Path, str]] = []
    for path in root.rglob("*"):
        # Never inspect or sanitize outside files through junctions/symlinks.
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.resolve().is_relative_to(root):
            raise RuntimeError("Release contains a linked path; remove the link before privacy verification.")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        parts = [part.lower() for part in relative.parts]
        name = path.name.lower()

        runtime_parts = parts[1:] if parts and parts[0] == "bin" else parts
        if runtime_parts and runtime_parts[0] in PRIVATE_RUNTIME_ROOTS:
            artifacts.append((path, f"runtime directory is not empty: {relative}"))
            continue
        if any(name == forbidden or name.startswith(forbidden + ".") or name.startswith("." + forbidden + ".")
               for forbidden in FORBIDDEN_NAMES) or name.startswith(".env.") or ".dpapi." in name:
            artifacts.append((path, f"private runtime artifact: {relative}"))
            continue
        if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            artifacts.append((path, f"database or log artifact: {relative}"))
            continue
        if path.suffix.lower() in {".json", ".jsonl", ".csv", ".txt"} and any(
            marker in name for marker in ("activation_codes", "license_codes", "license-keys", "激活码")
        ):
            artifacts.append((path, f"activation code export: {relative}"))
            continue
        if path.suffix.lower() == ".pem":
            # certifi's public trust roots are required for TLS. Never let a
            # private key pass by merely renaming it to that allowed filename.
            content = path.read_bytes()
            if relative.as_posix().lower() not in PUBLIC_PEM_PATHS or any(marker in content for marker in PRIVATE_KEY_MARKERS):
                artifacts.append((path, f"private or unapproved certificate artifact: {relative}"))
            continue
        if path.suffix.lower() in {".json", ".jsonl", ".txt", ".csv"} and path.stat().st_size <= 2 * 1024 * 1024:
            content = path.read_bytes()
            if any(marker in content for marker in PRIVATE_KEY_MARKERS):
                artifacts.append((path, f"private key content: {relative}"))
                continue
            if path.suffix.lower() == ".json":
                try:
                    value = json.loads(content.decode("utf-8-sig"))
                except (ValueError, UnicodeError):
                    value = None
                if isinstance(value, dict) and (any(isinstance(value.get(key), str) and value[key].strip()
                                                    for key in PRIVATE_JSON_FIELDS)
                        or ("qcsckp" in str(value.get("format", "")).lower() and "dpapi" in str(value.get("format", "")).lower())):
                    artifacts.append((path, f"private credential payload: {relative}"))
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
