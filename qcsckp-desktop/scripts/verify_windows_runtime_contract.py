"""Run the established isolated release smoke and check its public fingerprint.

The smoke helper path is explicit so this QA wrapper does not depend on a
developer's hidden environment or launch the production executable.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--smoke-helper", required=True, type=Path)
    parser.add_argument("--host-overrides", action="store_true")
    args = parser.parse_args()
    app = args.app.resolve()
    manifest = json.loads((app / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8-sig"))
    defaults = json.loads((app / "bin" / "DEFAULT-CONFIG.json").read_text(encoding="utf-8"))
    assert manifest["software_contract_sha256"] == defaults["software_contract_sha256"]
    spec = importlib.util.spec_from_file_location("release_smoke", args.smoke_helper.resolve())
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    original_windows = smoke.windows_for_pid
    original_popen = smoke.subprocess.Popen
    verified = []

    def windows(pid):
        rows = original_windows(pid)
        for path in app.parent.parent.glob(f"smoke-*/channels/{manifest['channel']}/startup-state/{pid}.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if state.get("phase") == "ready":
                assert state.get("software_contract_sha256") == defaults["software_contract_sha256"], "Packaged runtime differs from public defaults"
                assert state.get("configuration_policy_version") == defaults["policy_version"]
                assert state.get("packaged_policy_enforced") is True
                verified.append(pid)
        return rows

    def popen(*positional, **kwargs):
        if args.host_overrides:
            env = dict(kwargs["env"])
            # Fake values only; no credentials are copied out of the host.
            env.update(PYTHONNET_RUNTIME="coreclr", PYWEBVIEW_GUI="qt",
                       QCSCKP_TEST_MODE="1", QCSCKP_QIANCHUAN_BACKEND="legacy_browser",
                       QCSCKP_OE_APP_ID="synthetic-not-an-account", QCSCKP_OE_APP_SECRET="synthetic-not-a-secret",
                       QCSCKP_OE_ACCESS_TOKEN="synthetic-invalid-token", QCSCKP_ALLOW_LIVE_API_WRITES="1",
                       QCSCKP_REGULATION_SHADOW_MODE="1")
            kwargs["env"] = env
        return original_popen(*positional, **kwargs)

    smoke.windows_for_pid = windows
    smoke.subprocess.Popen = popen
    old_argv = sys.argv
    sys.argv = [str(args.smoke_helper), str(app)]
    try:
        smoke.main()
    finally:
        sys.argv = old_argv
        smoke.subprocess.Popen = original_popen
    assert verified, "No ready process fingerprint observed"
    print(json.dumps({"runtime_contract_verified": True,
                      "host_overrides_injected": args.host_overrides,
                      "software_contract_sha256": defaults["software_contract_sha256"]}))


if __name__ == "__main__":
    main()
