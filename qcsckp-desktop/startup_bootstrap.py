"""Minimal Windows bootstrap and startup diagnostics for QCSCKP.

This module intentionally imports only Python's standard library.  The
packaged executable enters here before importing pywebview, database drivers,
or any business service so import/DLL failures can be written to disk and
shown with a native Windows dialog instead of disappearing silently.
"""

from __future__ import annotations

import faulthandler
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from utils.log_redaction import redact_text

# PyInstaller executes this entrypoint as __main__. GUI imports must reuse
# this module rather than allocating a second ready event and watchdog state.
if __name__ == "__main__":
    sys.modules["startup_bootstrap"] = sys.modules[__name__]

APP_NAME = "QCSCKP"
APP_TITLE = "千川素材看盘工具"
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_BOOTSTRAPPER_NAME = "MicrosoftEdgeWebview2Setup.exe"
WINDOW_READY_TIMEOUT_SECONDS = 20
REQUIRED_MANAGED_DEPENDENCIES = (
    "bin/pythonnet/runtime/Python.Runtime.dll",
    "bin/clr_loader/ffi/dlls/amd64/ClrLoader.dll",
    "bin/webview/lib/Microsoft.Web.WebView2.Core.dll",
    "bin/webview/lib/Microsoft.Web.WebView2.WinForms.dll",
    "bin/webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
)

_LOG_LOCK = threading.RLock()
_FAULT_HANDLE: Optional[Any] = None
_WINDOW_READY = threading.Event()
_STATE_FILE: Optional[Path] = None
_HOOKS_INSTALLED = False
_STATE_LOCK = threading.RLock()
_WATCHDOG_DONE = threading.Event()
_WATCHDOG_ARMED = False
_STARTUP_GENERATION = 0
_STARTUP_PHASE = ""
_STARTUP_TERMINAL = ""
_FATAL_DIALOG_SHOWN = False
_TERMINAL_PHASES = {"failed", "window_timeout", "cancelled", "stopped"}


class StartupAbort(RuntimeError):
    """A startup preflight failure that has already been explained to the user."""


def _runtime_root() -> Path:
    from channel_runtime import layout
    return layout().profile


def _logs_dir() -> Path:
    return _runtime_root() / "logs"


def _diagnostics_dir() -> Path:
    return _runtime_root() / "diagnostics"


def startup_log_path() -> Path:
    return _logs_dir() / "startup.log"


def _state_dir() -> Path:
    return _runtime_root() / "startup-state"


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _redact(text: Any) -> str:
    value = redact_text(text)
    patterns = (
        r"(?i)(app[_ -]?secret|access[_ -]?token|refresh[_ -]?token|activation[_ -]?code|device[_ -]?credential|device[_ -]?session)\s*[:=]\s*[^\s,;]+",
        r"(?i)(authorization\s*:\s*bearer)\s+[^\s]+",
        r"(?i)(cookie|set-cookie)\s*:\s*[^\r\n]+",
        r"(?i)(sessionid|session_id|sid|ticket|code)\s*=\s*[^\s,;]+",
    )
    for pattern in patterns:
        value = re.sub(pattern, r"\1=<redacted>", value)
    value = re.sub(
        r"(?i)https?://[^\s?#]+(?:\?[^\s#]*)?(?:#[^\s]*)?",
        "<url>",
        value,
    )
    value = re.sub(
        r"(?i)[A-Z]:\\(?:[^\s\\/:*?\"<>|]+\\)*[^\s\\/:*?\"<>|]*",
        "<local-path>",
        value,
    )
    value = re.sub(
        r"(?i)\b[A-Z]:\\Users\\[^\\\r\n]+",
        "<local-user>",
        value,
    )
    value = re.sub(r"(?<!\d)\d{12,}(?!\d)", "<business-id>", value)
    return value


def _rotate_startup_log() -> None:
    path = startup_log_path()
    try:
        if path.is_file() and path.stat().st_size > 2 * 1024 * 1024:
            backup = path.with_suffix(".log.1")
            if backup.exists():
                backup.unlink()
            path.replace(backup)
    except OSError:
        pass


def startup_log(message: Any) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {_redact(message)}\n"
    with _LOG_LOCK:
        try:
            path = startup_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
        except OSError:
            pass


def native_message(message: str, *, title: str = APP_TITLE, error: bool = True) -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        flags = 0x00000010 if error else 0x00000040
        return int(ctypes.windll.user32.MessageBoxW(None, str(message), str(title), flags))
    except Exception:
        return 0


def native_confirm(message: str, *, title: str = APP_TITLE) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        result = ctypes.windll.user32.MessageBoxW(
            None,
            str(message),
            str(title),
            0x00000004 | 0x00000030 | 0x00000100,
        )
        return int(result) == 6
    except Exception:
        return False


def _state_payload(phase: str, detail: str = "") -> dict[str, Any]:
    payload = {
        "pid": os.getpid(),
        "executable": str(Path(sys.executable).resolve()),
        "phase": str(phase or "unknown"),
        "detail": _redact(detail)[:1000],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_unix": time.time(),
    }
    try:
        from release_configuration import public_runtime_contract
        contract = public_runtime_contract()
        digest = str(contract.get("software_contract_sha256") or "").lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            payload["software_contract_sha256"] = digest
            payload["configuration_policy_version"] = int(contract["policy_version"])
            payload["packaged_policy_enforced"] = bool(contract.get("packaged_policy_enforced"))
        else:
            payload["configuration_policy_error"] = "invalid_public_fingerprint"
    except Exception as exc:
        payload["configuration_policy_error"] = type(exc).__name__
    for identity_path in (
        _app_root() / "bin" / "release.json",
        _app_root() / "release.json",
    ):
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8-sig"))
            payload.update(
                {
                    "version": str(identity.get("version") or ""),
                    "channel": str(identity.get("channel") or ""),
                    "build_revision": int(identity.get("build_revision") or 0),
                }
            )
            break
        except (OSError, ValueError, TypeError):
            continue
    return payload


def begin_startup_attempt() -> int:
    global _WATCHDOG_DONE, _WATCHDOG_ARMED, _STARTUP_GENERATION
    global _STARTUP_PHASE, _STARTUP_TERMINAL, _FATAL_DIALOG_SHOWN, _STATE_FILE
    with _STATE_LOCK:
        _WATCHDOG_DONE.set()
        _WATCHDOG_DONE = threading.Event()
        _WATCHDOG_ARMED = False
        _WINDOW_READY.clear()
        _STARTUP_GENERATION += 1
        _STARTUP_PHASE = _STARTUP_TERMINAL = ""
        _FATAL_DIALOG_SHOWN = False
        _STATE_FILE = None
        return _STARTUP_GENERATION


def startup_state() -> dict[str, Any]:
    with _STATE_LOCK:
        return {"generation": _STARTUP_GENERATION, "phase": _STARTUP_PHASE,
                "terminal": _STARTUP_TERMINAL, "ready": _WINDOW_READY.is_set(),
                "watchdog_armed": _WATCHDOG_ARMED}


def cancel_window_watchdog() -> None:
    # Cancellation is not readiness. Never set _WINDOW_READY to stop a timer.
    with _STATE_LOCK:
        _WATCHDOG_DONE.set()


def mark_startup_phase(phase: str, detail: str = "", *, generation: Optional[int] = None) -> bool:
    global _STARTUP_PHASE, _STARTUP_TERMINAL
    with _STATE_LOCK:
        if generation is not None and generation != _STARTUP_GENERATION:
            return False
        if _STARTUP_TERMINAL:
            return False
        if phase in _TERMINAL_PHASES:
            _STARTUP_TERMINAL = phase
            _WATCHDOG_DONE.set()
        if phase == "ready":
            _WINDOW_READY.set()
            _WATCHDOG_DONE.set()
        _STARTUP_PHASE = phase
        _write_startup_phase(phase, detail)
    return True


def _write_startup_phase(phase: str, detail: str = "") -> None:
    global _STATE_FILE
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        if _STATE_FILE is None:
            _STATE_FILE = directory / f"{os.getpid()}.json"
        payload = _state_payload(phase, detail)
        temp = _STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(_STATE_FILE)
        # Keep only a small diagnostic history.  Active process files are not
        # removed here; stale records are harmless and bounded.
        records = sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for old in records[10:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass
    startup_log(f"phase={phase}" + (f" detail={detail}" if detail else ""))
    if phase == "failed":
        _record_diagnostic_event("failed", "startup_failure")


def _record_diagnostic_event(phase: str, event: str, **kwargs: Any) -> None:
    # Diagnostics is optional during import/DLL failures. It must never prevent
    # the standard-library startup log and native error dialog from running.
    try:
        from services.diagnostics import record_event
        record_event(phase, event, **kwargs)
    except Exception as exc:
        startup_log(f"diagnostic_event_failed type={type(exc).__name__}")


def mark_window_ready(generation: Optional[int] = None) -> None:
    mark_startup_phase("ready", generation=generation)


def window_ready_was_reached() -> bool:
    return _WINDOW_READY.is_set()


def mark_normal_exit() -> None:
    mark_startup_phase("stopped")


def _exception_text(exc_type: Any, exc: BaseException, tb: Any) -> str:
    return "".join(traceback.format_exception(exc_type, exc, tb))


def _show_fatal_error(summary: str, detail: str = "") -> None:
    global _FATAL_DIALOG_SHOWN
    with _STATE_LOCK:
        if _FATAL_DIALOG_SHOWN:
            return
        _FATAL_DIALOG_SHOWN = True
    path = startup_log_path()
    message = (
        f"{summary}\n\n"
        f"启动日志：{path}\n"
        "请将该日志发送给开发者排查。"
    )
    if detail:
        message += f"\n\n错误摘要：{_redact(detail)[:500]}"
    native_message(message)


def install_exception_hooks() -> None:
    global _FAULT_HANDLE, _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return
    _HOOKS_INSTALLED = True
    _rotate_startup_log()
    try:
        path = startup_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _FAULT_HANDLE = path.open("a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_FAULT_HANDLE, all_threads=True)
        if getattr(sys, "stdout", None) is None:
            sys.stdout = _FAULT_HANDLE
        if getattr(sys, "stderr", None) is None:
            sys.stderr = _FAULT_HANDLE
    except (OSError, RuntimeError):
        _FAULT_HANDLE = None

    def main_hook(exc_type: Any, exc: BaseException, tb: Any) -> None:
        cancel_window_watchdog()
        mark_startup_phase("failed", str(exc))
        _record_diagnostic_event("runtime", "runtime_failure", exception=exc)
        detail = _exception_text(exc_type, exc, tb)
        startup_log("unhandled_exception\n" + detail)
        _show_fatal_error("软件启动失败。", describe_startup_exception(exc))

    def thread_hook(args: Any) -> None:
        _record_diagnostic_event("runtime", "runtime_failure", exception=args.exc_value)
        detail = _exception_text(args.exc_type, args.exc_value, args.exc_traceback)
        startup_log(f"thread_exception name={getattr(args.thread, 'name', '')}\n{detail}")

    sys.excepthook = main_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = thread_hook


def _version_ok(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and text != "0.0.0.0" and re.match(r"^\d+(?:\.\d+){1,3}$", text))


def detect_webview2_version() -> str:
    if sys.platform != "win32":
        return "not-required"
    try:
        import winreg

        access_modes = [winreg.KEY_READ]
        for name in ("KEY_WOW64_32KEY", "KEY_WOW64_64KEY"):
            value = getattr(winreg, name, 0)
            if value:
                access_modes.append(winreg.KEY_READ | value)
        locations = (
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
            (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        )
        for root, subkey in locations:
            for access in access_modes:
                try:
                    with winreg.OpenKey(root, subkey, 0, access) as key:
                        value, _ = winreg.QueryValueEx(key, "pv")
                    if _version_ok(value):
                        return str(value).strip()
                except OSError:
                    continue
    except Exception as exc:
        startup_log(f"webview2_registry_check_failed={exc}")

    roots = (
        Path(os.getenv("ProgramFiles(x86)") or "") / "Microsoft" / "EdgeWebView" / "Application",
        Path(os.getenv("LOCALAPPDATA") or "") / "Microsoft" / "EdgeWebView" / "Application",
    )
    for root in roots:
        try:
            if not root.is_dir():
                continue
            versions = sorted(
                (item.name for item in root.iterdir() if item.is_dir() and _version_ok(item.name)),
                reverse=True,
            )
            for version in versions:
                if (root / version / "msedgewebview2.exe").is_file():
                    return version
        except OSError:
            continue
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package_integrity() -> list[str]:
    if not (getattr(sys, "frozen", False) and sys.platform == "win32"):
        return []
    root = _app_root()
    manifest_path = root / "PACKAGE-MANIFEST.json"
    if not manifest_path.is_file():
        return ["PACKAGE-MANIFEST.json 缺失"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        return [f"PACKAGE-MANIFEST.json 无法读取：{exc}"]
    issues: list[str] = []
    for item in manifest.get("critical_files") or []:
        if not isinstance(item, dict):
            issues.append("安装包清单包含无效条目")
            continue
        relative = str(item.get("path") or "").replace("/", os.sep)
        path = (root / relative).resolve()
        if (not relative or Path(relative).is_absolute() or ".." in Path(relative).parts
                or ":" in relative or not path.is_relative_to(root.resolve())):
            issues.append("安装包清单包含无效路径")
            continue
        if not path.is_file():
            issues.append(f"缺少文件：{relative}")
            continue
        try:
            expected_size = int(item.get("size") or 0)
            if expected_size <= 0 or path.stat().st_size != expected_size:
                issues.append(f"文件大小不一致：{relative}")
                continue
            expected_hash = str(item.get("sha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or _sha256(path) != expected_hash:
                issues.append(f"文件校验失败：{relative}")
        except (OSError, TypeError, ValueError) as exc:
            issues.append(f"文件无法读取：{relative}（{exc}）")
    return issues


def _verified_managed_dependencies() -> list[tuple[Path, dict[str, Any]]]:
    root = _app_root().resolve()
    try:
        manifest = json.loads((root / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise StartupAbort("启动依赖清单无法读取，未修改任何下载标记") from exc
    entries = {}
    for entry in manifest.get("critical_files") or []:
        if isinstance(entry, dict):
            entries[str(entry.get("path") or "").replace("\\", "/").casefold()] = entry
    missing = [name for name in REQUIRED_MANAGED_DEPENDENCIES if name.casefold() not in entries]
    if missing:
        raise StartupAbort("安装包缺少关键运行依赖校验项：" + "、".join(missing))
    selected = set(name.casefold() for name in REQUIRED_MANAGED_DEPENDENCIES)
    dependency_names = {Path(name).name.casefold() for name in REQUIRED_MANAGED_DEPENDENCIES}
    dependency_names.add("webbrowserinterop.x64.dll")
    # PyInstaller may also load a root-bin copy. Check every matching copy
    # declared by the package manifest, but never scan/unblock arbitrary files.
    selected.update(name for name in entries if name.startswith("bin/")
                    and (Path(name).name in dependency_names
                         or Path(name).name.startswith("microsoft.web.webview2.")) and name.endswith(".dll"))
    verified = []
    for name in sorted(selected):
        entry = entries[name]
        relative = str(entry.get("path") or "").replace("\\", "/")
        path = (root / relative).resolve()
        digest = str(entry.get("sha256") or "").lower()
        if (Path(relative).is_absolute() or ".." in Path(relative).parts or ":" in relative
                or not path.is_relative_to(root) or not path.is_file()
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise StartupAbort(f"关键依赖路径或校验项无效：{relative}")
        if path.stat().st_size != int(entry.get("size") or 0) or _sha256(path) != digest:
            raise StartupAbort(f"关键依赖内容校验失败，未解除下载标记：{relative}")
        verified.append((path, entry))
    return verified


def _download_zone(path: Path) -> Optional[int]:
    try:
        with Path(str(path) + ":Zone.Identifier").open("rb") as stream:
            value = stream.read(4096).decode("utf-8", "replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StartupAbort("无法检查关键依赖的下载标记：" + path.name) from exc
    match = re.search(r"(?im)^ZoneId\s*=\s*(\d+)\s*$", value)
    return int(match.group(1)) if match else None


def ensure_managed_dependencies() -> None:
    """Only a hash-matching, named dependency may have its own MOTW removed."""
    if not (getattr(sys, "frozen", False) and sys.platform == "win32"):
        return
    verified = _verified_managed_dependencies()
    blocked = [(path, entry) for path, entry in verified if (_download_zone(path) or 0) >= 3]
    if not blocked:
        return
    names = "\n".join("• " + str(entry["path"]) for _, entry in blocked)
    if not native_confirm(
        "以下运行依赖已匹配本安装包的大小与 SHA256 清单，但仍带有互联网下载标记，"
        "可能导致 Python/.NET 加载失败（0x80131515）。\n\n" + names
        + "\n\n仅在确认安装包来源可信时继续。是否只解除以上具体文件的下载标记？"
        "\n不会修改系统安全策略、其他文件或安装新的 .NET。"
    ):
        raise StartupAbort("已保留关键依赖下载标记，启动已取消；请使用来源可信的本地安装包")
    # Recheck after the user dialog; never act on a file changed while waiting.
    current = {path: entry for path, entry in _verified_managed_dependencies()}
    for path, entry in blocked:
        if current.get(path) != entry:
            raise StartupAbort("确认期间依赖清单发生变化，未继续解除下载标记")
        if (_download_zone(path) or 0) >= 3:
            try:
                Path(str(path) + ":Zone.Identifier").unlink()
            except OSError as exc:
                raise StartupAbort("该依赖下载标记无法解除：" + path.name) from exc
            if (_download_zone(path) or 0) >= 3:
                raise StartupAbort("该依赖仍被下载标记阻止：" + path.name)
            startup_log("unblocked_verified_dependency=" + str(entry["path"]))


def describe_startup_exception(exc: BaseException) -> str:
    fragments, seen = [], set()
    current = exc
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        fragments.append(str(current))
        try:
            hresult = getattr(current, "HResult", None)
            if hresult is not None:
                fragments.append(hex(int(hresult) & 0xFFFFFFFF))
            current = getattr(current, "InnerException", None) or current.__cause__ or current.__context__
        except Exception:
            break
    text = "\n".join(fragments)
    if "0x80131515" in text.lower() or "-2146233067" in text:
        return ("Windows 阻止了运行依赖加载（0x80131515）。请检查启动前的下载标记提示；"
                "若安装位置是网络盘，请改用可信的本地安装包。不要全局关闭 .NET 安全限制。\n" + text)
    if "0x8001010d" in text.lower():
        return "窗口 COM 调用遇到同步输入回调限制（0x8001010d），并非已确认的 .NET 缺失。\n" + text
    if "python.runtime.loader.initialize" in text.lower():
        return "Python/.NET 桥接初始化失败，请查看启动日志中的最内层异常；不能仅凭此提示认定 WebView2 缺失。\n" + text
    return text


def _install_webview2() -> bool:
    installer = _app_root() / "runtime" / WEBVIEW2_BOOTSTRAPPER_NAME
    if not installer.is_file():
        raise StartupAbort("安装包缺少 Microsoft WebView2 安装组件")
    agreed = native_confirm(
        "当前电脑未检测到 Microsoft Edge WebView2 Runtime。\n\n"
        "千川素材看盘工具需要该微软组件显示界面。是否立即安装？"
    )
    if not agreed:
        raise StartupAbort("用户取消安装 Microsoft Edge WebView2 Runtime")
    mark_startup_phase("installing_webview2")
    try:
        completed = subprocess.run(
            [str(installer), "/silent", "/install"],
            cwd=str(installer.parent),
            timeout=300,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise StartupAbort("WebView2 安装超过5分钟仍未完成") from exc
    except OSError as exc:
        raise StartupAbort(f"无法启动 WebView2 安装程序：{exc}") from exc
    if int(completed.returncode or 0) != 0:
        raise StartupAbort(f"WebView2 安装失败，退出码 {completed.returncode}")
    for _ in range(20):
        version = detect_webview2_version()
        if version:
            startup_log(f"webview2_install_success version={version}")
            return True
        time.sleep(1)
    raise StartupAbort("WebView2 安装程序已结束，但仍未检测到运行时")


def ensure_webview2() -> str:
    if not (getattr(sys, "frozen", False) and sys.platform == "win32"):
        return detect_webview2_version()
    version = detect_webview2_version()
    if version:
        startup_log(f"webview2_version={version}")
        return version
    _install_webview2()
    return detect_webview2_version()


def _recent_startup_states() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for path in sorted(_state_dir().glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:10]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    rows.append(payload)
            except (OSError, ValueError, TypeError):
                continue
    except OSError:
        pass
    return rows


def recover_stuck_instance_if_requested(executable: str) -> bool:
    """Terminate only a user-confirmed same-executable process stuck before ready."""
    if sys.platform != "win32":
        return False
    wanted = os.path.realpath(os.path.abspath(executable)).casefold()
    now = time.time()
    candidate: Optional[dict[str, Any]] = None
    for state in _recent_startup_states():
        try:
            pid = int(state.get("pid") or 0)
            state_exe = os.path.realpath(os.path.abspath(str(state.get("executable") or ""))).casefold()
            age = now - float(state.get("updated_unix") or 0)
        except (TypeError, ValueError, OSError):
            continue
        if pid and pid != os.getpid() and state_exe == wanted and state.get("phase") != "ready" and age >= 30:
            candidate = state
            break
    if not candidate:
        return False
    pid = int(candidate["pid"])
    try:
        import psutil

        proc = psutil.Process(pid)
        if os.path.realpath(proc.exe()).casefold() != wanted:
            return False
    except Exception:
        return False
    if not native_confirm(
        "检测到同一程序上次启动超过30秒仍未显示窗口。\n\n"
        "是否结束该异常实例并重新启动？"
    ):
        return False
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            return False
    startup_log(f"recovered_stuck_instance pid={pid}")
    return True


def _recent_windows_application_errors() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        completed = subprocess.run(
            [
                "wevtutil.exe",
                "qe",
                "Application",
                "/q:*[System[(Level=2)]]",
                "/c:40",
                "/rd:true",
                "/f:text",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    raw = str(completed.stdout or "")
    blocks = re.split(r"(?m)^Event\[\d+\]:", raw)
    return [
        _redact(block.strip())[:4000]
        for block in blocks
        if "qcsckp.exe" in block.casefold()
    ][:5]


def start_window_watchdog(window: Any, timeout: int = WINDOW_READY_TIMEOUT_SECONDS,
                          *, close_window: Optional[Any] = None) -> threading.Thread:
    global _WATCHDOG_ARMED
    with _STATE_LOCK:
        generation = _STARTUP_GENERATION
        done = _WATCHDOG_DONE
        _WATCHDOG_ARMED = True

    def worker() -> None:
        if done.wait(max(5, int(timeout))):
            return
        with _STATE_LOCK:
            if done.is_set() or generation != _STARTUP_GENERATION or _WINDOW_READY.is_set():
                return
            if not mark_startup_phase("window_timeout", f"timeout_seconds={timeout}", generation=generation):
                return
        _show_fatal_error(
            "软件窗口启动超时。",
            "WebView2没有在规定时间内完成页面加载",
        )
        try:
            (close_window or window.destroy)()
        except Exception as exc:
            startup_log(f"window_destroy_after_timeout_failed={exc}")

    thread = threading.Thread(target=worker, daemon=True, name="qcsckp-startup-watchdog")
    thread.start()
    return thread


def generate_diagnostic_report() -> Path:
    target = _diagnostics_dir() / f"startup-diagnostic-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    issues = validate_package_integrity()
    lines = [
        f"Application: {APP_NAME}",
        f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Windows: {platform.platform()}",
        f"Architecture: {platform.machine()}",
        f"Executable: {Path(sys.executable).name}",
        f"Frozen: {bool(getattr(sys, 'frozen', False))}",
        f"WebView2 version: {detect_webview2_version() or 'NOT_FOUND'}",
        f"Package integrity: {'OK' if not issues else 'FAILED'}",
    ]
    lines.extend(f"- {item}" for item in issues)
    states = _recent_startup_states()[:5]
    lines.append("Recent startup states:")
    for state in states:
        lines.append(
            f"- pid={state.get('pid')} phase={state.get('phase')} updated_at={state.get('updated_at')} "
            f"detail={_redact(state.get('detail'))}"
        )
    try:
        tail = startup_log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-120:]
    except OSError:
        tail = []
    lines.append("Startup log tail:")
    lines.extend(_redact(line) for line in tail)
    windows_errors = _recent_windows_application_errors()
    lines.append("Recent Windows application errors for QCSCKP.exe:")
    if windows_errors:
        lines.extend(windows_errors)
    else:
        lines.append("- none found or event log unavailable")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _run_diagnostics() -> int:
    try:
        report = generate_diagnostic_report()
    except Exception as exc:
        startup_log("diagnostic_generation_failed\n" + traceback.format_exc())
        _show_fatal_error("启动诊断生成失败。", str(exc))
        return 2
    native_message(
        f"启动诊断已生成：\n{report}\n\n请将该文件发送给开发者。",
        error=False,
    )
    try:
        subprocess.Popen(["explorer.exe", "/select,", str(report)])
    except OSError:
        pass
    return 0


def _repair_license_connection() -> int:
    """Standalone entry: no WebView, credential store or business runtime."""
    try:
        from services.license_transport import LicenseTransport

        result = LicenseTransport().diagnose_and_repair()
        detail = "\n".join(step["message"] for step in result.get("steps", []))
        native_message(
            result["message"] + "\n\n" + detail
            + "\n\n脱敏日志：" + str(result.get("log_path") or "")
            + "\n\n检测不代表激活成功。请重新打开软件，使用原有授权在线验证。",
            error=not result["success"],
        )
        return 0 if result["success"] else 2
    except Exception as exc:
        from services.license_transport import describe_network_error

        detail = describe_network_error(exc)
        startup_log("license_repair_failed=" + detail["kind"])
        native_message("授权连接修复未完成：" + detail["message"] + "。原激活凭证未改动。")
        return 2


def _main_impl() -> int:
    begin_startup_attempt()
    from release_configuration import enforce_packaged_configuration
    enforce_packaged_configuration()
    install_exception_hooks()
    mark_startup_phase("bootstrap", f"frozen={bool(getattr(sys, 'frozen', False))}")
    if "--diagnose-startup" in sys.argv[1:]:
        return _run_diagnostics()
    if "--repair-license-connection" in sys.argv[1:]:
        return _repair_license_connection()
    try:
        mark_startup_phase("package_integrity")
        issues = validate_package_integrity()
        if issues:
            raise StartupAbort("；".join(issues[:5]))
        mark_startup_phase("managed_dependency_check")
        ensure_managed_dependencies()
        mark_startup_phase("webview2_check")
        ensure_webview2()
        mark_startup_phase("app_import")
        import gui_app

        mark_startup_phase("app_main")
        gui_code = gui_app.main()
        state = startup_state()
        if state["terminal"] in {"failed", "window_timeout"}:
            return 1
        if isinstance(gui_code, int) and gui_code:
            mark_startup_phase("cancelled", "窗口启动被用户中断")
            return gui_code
        if state["watchdog_armed"] and not state["ready"]:
            raise StartupAbort("窗口在就绪前已退出")
        return 0
    except StartupAbort as exc:
        cancel_window_watchdog()
        startup_log(f"startup_aborted={exc}")
        mark_startup_phase("failed", str(exc))
        _show_fatal_error("软件无法启动。", str(exc))
        return 2
    except Exception as exc:
        cancel_window_watchdog()
        _record_diagnostic_event("bootstrap", "startup_failure", exception=exc)
        detail = traceback.format_exc()
        startup_log("startup_failed\n" + detail)
        mark_startup_phase("failed", str(exc))
        _show_fatal_error("软件启动失败。", describe_startup_exception(exc))
        return 1
    finally:
        cancel_window_watchdog()
        if not _WINDOW_READY.is_set():
            startup_log("bootstrap_exit_before_window_ready")


def main() -> int:
    from channel_runtime import InstanceLease, prepare_profile
    from release_identity import CHANNEL, CHANNELS, DISPLAY_VERSION
    from services.diagnostics import start_worker, stop_worker
    lease = InstanceLease()
    if not lease.acquire():
        native_message("QCSCKP 另一版本仍在运行，请先从托盘完全退出。不会强制结束进程。")
        return 2
    try:
        prepare_profile(lambda source, channel: native_confirm(
            f"即将首次运行{CHANNELS[channel]}。\n是否复制当前版本的配置与历史数据？\n"
            "原数据会先备份并保留；新副本独立使用，不自动合并。\n选择否将建立空白业务配置。"))
        start_worker()
        startup_log(DISPLAY_VERSION)
        return _main_impl()
    except Exception as exc:
        _record_diagnostic_event("switch", "switch_failure", exception=exc)
        native_message("安全切版未完成，原数据已保留。\n" + str(exc))
        return 2
    finally:
        stop_worker()
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
