"""Windows 用户范围全局互斥量。"""

from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183


def _current_user_identity() -> str:
    if os.name == "nt":
        try:
            import win32api
            import win32security

            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
            )
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            return win32security.ConvertSidToStringSid(sid)
        except Exception:
            pass
    return f"{os.getenv('USERDOMAIN', '')}\\{os.getenv('USERNAME', '')}"


def mutex_name() -> str:
    digest = hashlib.sha256(_current_user_identity().encode("utf-8")).hexdigest()[:24]
    # Global namespace + user-SID digest: all installation directories for the
    # same Windows user share one instance, while different users stay isolated.
    return f"Global\\QCSCKP-production-v1a-{digest}"


class GlobalUserMutex:
    def __init__(self, name: str | None = None):
        self.name = name or mutex_name()
        self._handle = None
        self.already_running = False

    def acquire(self) -> bool:
        if os.name != "nt":
            # V1A 的正式边界是 Windows；非 Windows 仅供单元测试。
            self.already_running = False
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        return not self.already_running

    def close(self) -> None:
        if self._handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = None

    def __enter__(self) -> "GlobalUserMutex":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
