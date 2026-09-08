"""Check real post-license entry with an isolated empty business profile.

Copies only three existing same-machine DPAPI ciphertext files. Does not read
their cleartext, activate a code, copy business data, or enable a strategy.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time


AUTH_FILES = ("license_credentials.dpapi", "license_machine_code.dpapi", "license_device_code.dpapi")
EMPTY_TABLES = ("qianchuan_account", "promotion_target", "qianchuan_api_audit", "local_retarget_task",
                "pmc_retargeting_run", "pmc_regulation_run", "feishu_outbox")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(args):
    app = args.app.resolve()
    assert app.parent.name.startswith(".verify-"), "Use a separately extracted final ZIP"
    identity = args.license_identity_dir.resolve()
    assert identity.is_dir() and all((identity / name).is_file() for name in AUTH_FILES)
    assert not any((identity / name).is_symlink() for name in AUTH_FILES)
    original = {name: digest(identity / name) for name in AUTH_FILES}
    manifest = json.loads((app / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8-sig"))
    helper_spec = importlib.util.spec_from_file_location("isolated_smoke", args.smoke_helper.resolve())
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    with tempfile.TemporaryDirectory(prefix="smoke-licensed-", dir=app.parent.parent) as directory:
        home = Path(directory).resolve()
        assert not home.is_relative_to(identity) and not identity.is_relative_to(home)
        private = home / "shared-v1/identity"
        private.mkdir(parents=True)
        for name in AUTH_FILES:
            shutil.copyfile(identity / name, private / name)
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("QCSCKP_", "QIANCHUAN_", "FEISHU_"))}
        data = home / "channels" / manifest["channel"] / "data"
        env.update(QCSCKP_HOME=str(home), QCSCKP_DATA_DIR=str(data))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        proc = subprocess.Popen([str(app / "QCSCKP.exe")], cwd=app, env=env,
                                startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            profile = home / "channels" / manifest["channel"]
            state_file = profile / "startup-state" / f"{proc.pid}.json"
            log_file = profile / "logs/startup.log"
            entered_at = None
            deadline = time.monotonic() + 90 + max(60, args.observe_seconds)
            while time.monotonic() < deadline:
                assert proc.poll() is None, "Post-license process exited"
                state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
                logs = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
                assert state.get("phase") not in {"failed", "window_timeout"}, "Startup failed"
                assert "ModuleNotFoundError" not in logs and "启动失败，已回滚" not in logs, "Runtime failed after authorization"
                if state.get("phase") == "ready" and "page_loaded=index.html" in logs and "后台服务已统一启动" in logs:
                    assert state["software_contract_sha256"] == manifest["software_contract_sha256"]
                    if entered_at is None:
                        entered_at = time.monotonic()
                        print(json.dumps({"stage": "licensed_main_page_entered", "pid": proc.pid}), flush=True)
                    if time.monotonic() - entered_at >= max(60, args.observe_seconds):
                        break
                time.sleep(0.5)
            else:
                raise AssertionError("Did not complete real authorized main-page entry and observation")
            windows = helper.windows_for_pid(proc.pid)
            assert any(f"r{manifest['build_revision']}" in w["title"] and not w["hung"] for w in windows)
            counts = {}
            with closing(sqlite3.connect((data / "qianchuan.db").as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                for table in EMPTY_TABLES:
                    counts[table] = db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    assert counts[table] == 0, "Unexpected business data or platform request"
            network_path = profile / "logs/license-network.log"
            events = [json.loads(line) for line in network_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            requests = [row for row in events if row.get("event") == "request"]
            assert requests and all(row.get("method") == "GET" for row in requests), "License check must not activate or unbind"
            assert any(row.get("path") == "/device/status" and row.get("http_status") == 200 for row in requests)
            assert original == {name: digest(identity / name) for name in AUTH_FILES}, "Source credentials changed during validation"
            result = {"success": True, "version": manifest["version"], "revision": manifest["build_revision"],
                      "channel": manifest["channel"], "pid": proc.pid, "main_page": "index.html",
                      "real_license_http_200": True, "license_writes": 0, "counts": counts,
                      "source_credentials_unchanged": True, "observed_seconds": max(60, args.observe_seconds),
                      "software_contract_sha256": manifest["software_contract_sha256"]}
        except Exception as error:
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                evidence = [line[:500] for line in (locals().get("logs") or "").splitlines()
                            if "ModuleNotFoundError" in line or "page_loaded=" in line or "后台服务已统一启动" in line]
                args.report.write_text(json.dumps({"success": False, "error_type": type(error).__name__,
                    "diagnostic_lines": evidence[-8:]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=15)
            helper.stop_isolated_webview(home)
    result["isolated_profile_removed"] = not home.exists()
    assert result["isolated_profile_removed"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--license-identity-dir", required=True, type=Path)
    parser.add_argument("--smoke-helper", required=True, type=Path)
    parser.add_argument("--observe-seconds", type=int, default=65)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = run(args)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
