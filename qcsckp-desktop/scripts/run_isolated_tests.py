"""Run unittest with a disposable application home, never the live profile."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="qcsckp-isolated-tests-") as test_home:
        env = dict(os.environ)
        for name in tuple(env):
            if name.startswith("QCSCKP_") or name in {"REGULATION_RULE_INTERVAL_SEC", "REGULATION_STRATEGY_PARALLEL", "REGULATION_ASSIST_UPDATED_WITHIN_MINUTES"}:
                env.pop(name, None)
        env.update(QCSCKP_HOME=test_home, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        arguments = sys.argv[1:] or ["discover", "-s", "tests", "-v"]
        report_path = None
        if "--report" in arguments:
            position = arguments.index("--report")
            report_path = Path(arguments[position + 1]).resolve()
            del arguments[position:position + 2]
        entry = "from scripts.test_network_guard import install; install(); import sys,unittest; sys.argv=['unittest',*sys.argv[1:]]; unittest.main(module=None)"
        result = subprocess.run([sys.executable, "-B", "-c", entry, *arguments], cwd=root, env=env,
                                capture_output=report_path is not None, text=report_path is not None,
                                encoding="utf-8" if report_path is not None else None,
                                errors="replace" if report_path is not None else None)
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
            for line in (result.stdout + "\n" + result.stderr).splitlines():
                if line.startswith(("ERROR:", "FAIL:", "Ran ", "FAILED", "OK")):
                    print(line[:2000])
            print(f"Test output: {report_path}")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
