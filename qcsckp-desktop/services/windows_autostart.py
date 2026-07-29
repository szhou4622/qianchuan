# -*- coding: utf-8 -*-
"""当前Windows用户的可选开机自启。"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict

from config import API_BASE_URL, DATA_DIR, PROJECT_ROOT, TEST_MODE


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "QCSCKPDesktop"


def _command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    executable = pythonw if os.path.isfile(pythonw) else sys.executable
    entry = os.path.join(PROJECT_ROOT, "autostart_launcher.py")
    args = [
        os.path.abspath(executable),
        os.path.abspath(entry),
        "--data-dir",
        os.path.abspath(DATA_DIR),
        "--api-base-url",
        str(API_BASE_URL),
    ]
    if TEST_MODE:
        args.append("--test-mode")
    return subprocess.list2cmdline(args)


def get_windows_autostart_status() -> Dict[str, object]:
    if os.name != "nt":
        return {
            "success": True,
            "supported": False,
            "enabled": False,
            "message": "仅Windows支持开机自启",
        }
    import winreg

    enabled = False
    value = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_READ,
        ) as key:
            value = str(winreg.QueryValueEx(key, VALUE_NAME)[0] or "")
            enabled = bool(value)
    except FileNotFoundError:
        pass
    return {
        "success": True,
        "supported": True,
        "enabled": enabled,
        "command": value if enabled else _command(),
    }


def set_windows_autostart(enabled: bool) -> Dict[str, object]:
    if os.name != "nt":
        return {
            "success": False,
            "supported": False,
            "enabled": False,
            "message": "仅Windows支持开机自启",
        }
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                VALUE_NAME,
                0,
                winreg.REG_SZ,
                _command(),
            )
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    result = get_windows_autostart_status()
    result["message"] = "开机自启已开启" if enabled else "开机自启已关闭"
    return result
