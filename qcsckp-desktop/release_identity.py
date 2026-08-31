"""Immutable build identity; channel is never inferred from a version number."""
from __future__ import annotations

import json
import re
import sys
import subprocess
from pathlib import Path

CHANNELS = {"production": "正式版", "development": "开发版", "stable": "历史稳定版"}


def load_identity(path=None):
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    data = json.loads(Path(path or root / "release.json").read_text(encoding="utf-8-sig"))
    if data.get("app_name") != "QCSCKP" or data.get("channel") not in CHANNELS:
        raise ValueError("发布渠道配置无效，已停止启动")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(data.get("version", ""))):
        raise ValueError("发布版本号无效")
    if type(data.get("build_revision")) is not int or data["build_revision"] < 1:
        raise ValueError("发布构建号无效")
    if path is None and not getattr(sys, "frozen", False):
        try:
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=2,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            if re.fullmatch(r"[a-f0-9]{40}", head):
                data["source_commit"] = head
        except (OSError, subprocess.TimeoutExpired):
            pass
    return data


IDENTITY = load_identity()
CHANNEL = IDENTITY["channel"]
VERSION = IDENTITY["version"]
BUILD_REVISION = IDENTITY["build_revision"]
DISPLAY_VERSION = f"v{VERSION} · {CHANNELS[CHANNEL]} · r{BUILD_REVISION}"
