#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""受控真实追投启动前的只读硬检查。"""
from __future__ import annotations

import os
import sys


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    desktop_root = os.path.join(repo_root, "qcsckp-desktop")
    if desktop_root not in sys.path:
        sys.path.insert(0, desktop_root)

    from services.local_test_guard import build_live_retarget_preflight

    result = build_live_retarget_preflight()
    if not result.get("success"):
        print("验收检查失败：" + str(result.get("message") or "未知错误"))
        return 2
    for item in result.get("checks") or []:
        if item.get("key") == "live_gate":
            continue
        mark = "通过" if item.get("ok") else "未通过"
        print(f"[{mark}] {item.get('label')}: {item.get('detail')}")
    if not result.get("ready_to_arm"):
        print("准备项未全部通过，已拒绝开启真实追投。")
        return 2
    print("基础准备项全部通过，可以等待用户最终确认并开启一次性授权。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
