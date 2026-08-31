"""Pre-login connection diagnostics. Never repairs or grants a license.

Only a credential-free GET is used to select a working, verified HTTPS
transport. POSTs are sent exactly once by the selected transport. Windows
curl uses Schannel; secrets travel through stdin, never command arguments,
temporary files, curl configuration files or diagnostic output.
"""

from __future__ import annotations

import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, getproxies

from config import CURRENT_VERSION, DATA_DIR, LICENSE_SERVICE_BASE_URL, LOGS_DIR


_MODES = {"default", "bundled_ca", "windows_https"}
_LIMIT = 1024 * 1024
_LOG_LOCK = threading.Lock()
_MESSAGES = {
    "dns": "授权域名解析失败，请检查DNS或网络连接",
    "timeout": "授权连接超时，服务器尚未返回结果",
    "certificate_chain": "HTTPS证书信任链校验失败",
    "certificate_time": "HTTPS证书已过期或尚未生效，请核对电脑日期和时间",
    "certificate_identity": "HTTPS证书与授权服务器域名不匹配，已阻止连接",
    "certificate_invalid": "HTTPS证书校验失败，已阻止不可信连接",
    "tls": "HTTPS握手失败",
    "proxy": "代理连接失败，请检查代理设置",
    "connection_refused": "授权连接被拒绝",
    "connection_reset": "授权连接被中断",
    "network": "授权网络连接失败",
    "transport_unavailable": "安全连接组件不可用",
    "response_invalid": "授权地址未返回预期的JSON响应",
}


class TransportFailure(OSError):
    def __init__(self, kind: str, native_code: int = 0):
        self.kind = kind if kind in _MESSAGES else "network"
        self.native_code = int(native_code)
        super().__init__(_MESSAGES[self.kind])


def describe_network_error(exc: BaseException) -> dict:
    """Allowlisted facts only: exception text may contain proxy passwords."""
    reason = exc.reason if isinstance(exc, URLError) else exc
    kind = "network"
    verify_code = getattr(reason, "verify_code", None)
    if isinstance(reason, TransportFailure):
        kind = reason.kind
    elif isinstance(reason, ssl.SSLCertVerificationError):
        if verify_code in {9, 10}:
            kind = "certificate_time"
        elif verify_code in {62, 64}:
            kind = "certificate_identity"
        elif verify_code in {2, 18, 19, 20, 21}:
            kind = "certificate_chain"
        else:
            kind = "certificate_invalid"
    elif isinstance(reason, ssl.SSLError):
        kind = "tls"
    elif isinstance(reason, socket.gaierror):
        kind = "dns"
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        kind = "timeout"
    elif isinstance(reason, ConnectionRefusedError):
        kind = "connection_refused"
    elif isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
        kind = "connection_reset"
    elif isinstance(reason, str):
        # Only classify known phrases; do not copy any of the supplied text.
        text = reason.lower()
        if "timed out" in text:
            kind = "timeout"
        elif "getaddrinfo failed" in text or "name or service not known" in text:
            kind = "dns"
        elif "tunnel connection failed" in text or "proxy" in text:
            kind = "proxy"
    result = {"kind": kind, "message": _MESSAGES[kind]}
    for field in ("errno", "winerror", "verify_code", "native_code"):
        value = getattr(reason, field, None)
        if isinstance(value, int):
            result[field] = value
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward device credentials to a redirect target.
        return None


def _python_opener(bundled_ca: bool = False):
    context = ssl.create_default_context()
    if bundled_ca:
        import certifi

        # Add the packaged trust roots; retain Windows/enterprise trust roots.
        context.load_verify_locations(cafile=certifi.where())
    return build_opener(_NoRedirect(), HTTPSHandler(context=context)).open


def _proxy_configured() -> bool:
    if any(value for key, value in getproxies().items() if key.lower() in {"https", "http", "all"}):
        return True
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                for name in ("ProxyEnable", "AutoConfigURL", "AutoDetect"):
                    try:
                        if winreg.QueryValueEx(key, name)[0]:
                            return True
                    except OSError:
                        pass
        except OSError:
            pass
    return False


def _system_curl_path() -> Path:
    if sys.platform != "win32":
        raise TransportFailure("transport_unavailable")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not size or size >= len(buffer):
        raise TransportFailure("transport_unavailable")
    path = Path(buffer.value) / "curl.exe"
    if not path.is_file():
        raise TransportFailure("transport_unavailable")
    return path


def _quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t') + '"'


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int):
        super().__init__(body)
        self.status = status


class WindowsHttpsOpener:
    """System curl only, Schannel verified, no shell, redirects or retries."""

    def __init__(self):
        self.path = _system_curl_path()
        try:
            version = subprocess.run(
                [str(self.path), "-q", "--version"], capture_output=True,
                timeout=3, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise TransportFailure("transport_unavailable") from None
        if version.returncode or b"Schannel" not in version.stdout:
            raise TransportFailure("transport_unavailable")

    def __call__(self, request: Request, timeout: float):
        # Do not silently bypass a company's proxy/PAC policy. The app does
        # not change registry, firewall, DNS, proxy environment or system time.
        if _proxy_configured():
            raise TransportFailure("proxy")
        if urlsplit(request.full_url).scheme != "https":
            raise TransportFailure("transport_unavailable")
        lines = [
            "silent", "show-error", 'proto = "=https"', 'retry = "0"',
            'max-redirs = "0"', f'max-filesize = "{_LIMIT}"',
            f"connect-timeout = {_quote(str(min(8.0, timeout)))}",
            f"max-time = {_quote(str(timeout))}",
            f"url = {_quote(request.full_url)}",
            f"request = {_quote(request.get_method())}",
            'write-out = "\\nQCSCKP_HTTP_STATUS:%{http_code}"',
        ]
        for key, value in request.header_items():
            if any(char in key + value for char in ('\r', '\n', '\0')):
                raise TransportFailure("transport_unavailable")
            lines.append(f"header = {_quote(key + ': ' + value)}")
        if request.data is not None:
            lines.append(f"data-raw = {_quote(request.data.decode('utf-8'))}")
        env = dict(os.environ)
        # No TLS key logging or local CA-file overrides. Schannel still
        # performs normal certificate, hostname and revocation verification.
        for key in list(env):
            if key.upper() in {"SSLKEYLOGFILE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"}:
                env.pop(key)
        try:
            result = subprocess.run(
                [str(self.path), "-q", "--config", "-"],
                input=("\n".join(lines) + "\n").encode("utf-8"),
                capture_output=True, timeout=timeout + 2, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise TransportFailure("timeout") from None
        except OSError:
            raise TransportFailure("transport_unavailable") from None
        if result.returncode:
            kind = {5: "proxy", 6: "dns", 7: "connection_refused", 28: "timeout", 35: "tls", 60: "certificate_invalid", 56: "connection_reset", 63: "response_invalid"}.get(result.returncode, "network")
            # stderr may contain private URLs/proxy details: never expose it.
            raise TransportFailure(kind, result.returncode)
        body, marker, status = result.stdout.rpartition(b"\nQCSCKP_HTTP_STATUS:")
        if not marker or not status.strip().isdigit() or len(body) > _LIMIT:
            raise TransportFailure("response_invalid")
        code = int(status.strip())
        if not 200 <= code < 300:
            raise HTTPError(request.full_url, code, "HTTP error", {}, io.BytesIO(body))
        return _Response(body, code)


class LicenseTransport:
    def __init__(self, *, base_url=LICENSE_SERVICE_BASE_URL, settings_file=None, log_file=None):
        self.base_url = str(base_url).rstrip("/")
        self.settings_file = Path(settings_file or Path(DATA_DIR) / "license_transport.json")
        self.log_file = Path(log_file or Path(LOGS_DIR) / "license-network.log")
        self._repair_lock = threading.Lock()
        self._openers = {}
        self.mode = "default"
        try:
            settings = json.loads(self.settings_file.read_text(encoding="utf-8")[:4096])
            if isinstance(settings, dict) and settings.get("mode") in _MODES:
                self.mode = settings["mode"]
        except (OSError, ValueError, TypeError):
            pass

    def _opener(self, mode):
        if mode not in self._openers:
            self._openers[mode] = WindowsHttpsOpener() if mode == "windows_https" else _python_opener(mode == "bundled_ca")
        return self._openers[mode]

    def __call__(self, request, timeout):
        url = urlsplit(request.full_url)
        base = urlsplit(self.base_url)
        allowed = {base.path + "/activate": "POST", base.path + "/device/status": "GET", base.path + "/device/unbind": "POST"}
        if url.scheme != "https" or url.netloc != base.netloc or allowed.get(url.path) != request.get_method():
            raise TransportFailure("transport_unavailable")
        return self._opener(self.mode)(request, timeout=timeout)

    def record(self, **facts):
        """Only call with allowlisted facts, never a response/exception body."""
        allowed = {"event", "request_id", "path", "method", "attempt", "elapsed_ms", "http_status", "mode", "kind", "errno", "winerror", "verify_code", "native_code", "probe_id", "proxy_configured", "saved"}
        data = {key: value for key, value in facts.items() if key in allowed}
        data["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _LOG_LOCK:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                if self.log_file.exists() and self.log_file.stat().st_size > 512 * 1024:
                    os.replace(self.log_file, self.log_file.with_suffix(".log.1"))
                with self.log_file.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(data, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def _save_mode(self, mode):
        name = None
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.settings_file.parent, prefix="license-transport-", suffix=".tmp", delete=False) as stream:
                name = stream.name
                json.dump({"schema": 1, "mode": mode}, stream)
            os.replace(name, self.settings_file)
            return True
        except OSError:
            return False
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def _probe(self, mode, probe_id):
        started = time.monotonic()
        step = {"mode": mode, "http_status": 0, "reachable": False}
        request = Request(self.base_url + "/device/status", headers={"Accept": "application/json", "User-Agent": f"QCSCKP-Desktop/{CURRENT_VERSION}", "X-Client-Request-ID": probe_id}, method="GET")
        try:
            try:
                with self._opener(mode)(request, timeout=8.0) as response:
                    step["http_status"] = int(response.status)
                    raw = response.read(_LIMIT)
            except HTTPError as exc:
                step["http_status"] = int(exc.code)
                raw = exc.read(_LIMIT)
                exc.close()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                payload = None
            # 401 on this credential-FREE probe means the network works. It
            # must never invalidate saved credentials or authorize the user.
            step["reachable"] = step["http_status"] == 401 and isinstance(payload, dict) and (payload.get("ok") is False or payload.get("success") is False)
            step["kind"] = "reachable" if step["reachable"] else "server_response"
            step["message"] = "连接正常（未携带凭证的检测返回401，不代表激活失效）" if step["reachable"] else f"服务器返回HTTP {step['http_status']}，未确认连接恢复，请联系作者"
        except (OSError, URLError) as exc:
            step.update(describe_network_error(exc))
        except Exception:
            # E.g. packaged CA bundle missing; no traceback containing a URL.
            step.update(kind="transport_unavailable", message=_MESSAGES["transport_unavailable"])
        step["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        self.record(event="probe", probe_id=probe_id, **step)
        return step

    def diagnose_and_repair(self):
        if not self._repair_lock.acquire(blocking=False):
            return {"success": False, "message": "连接诊断正在进行，请勿重复点击"}
        try:
            probe_id = uuid.uuid4().hex
            result = {"success": False, "repaired": False, "probe_id": probe_id, "steps": [], "log_path": str(self.log_file)}
            self._openers.clear()  # reload current system trust/proxy settings
            old_mode = self.mode
            modes = list(dict.fromkeys([old_mode, "default", "bundled_ca"] + (["windows_https"] if sys.platform == "win32" else [])))
            for mode in modes:
                if mode == "windows_https" and _proxy_configured():
                    result["steps"].append({"mode": mode, "kind": "proxy", "message": "检测到代理/PAC设置，未擅自切换系统直连；请管理员检查代理", "reachable": False})
                    continue
                step = self._probe(mode, probe_id)
                result["steps"].append(step)
                if step["reachable"]:
                    saved = self._save_mode(mode)
                    self.mode = mode
                    result.update(success=True, repaired=mode != old_mode, mode=mode, saved=saved,
                                  message="授权连接已验证正常；本地激活凭证未改动，正在重新验证授权")
                    if not saved:
                        result["message"] += "（连接设置保存失败，本次有效；下次启动可能需要重新修复）"
                    self.record(event="repair", probe_id=probe_id, mode=mode, saved=saved)
                    return result
                # Do not route around an explicit server rejection, wrong
                # hostname, revoked/expired certificate or bad computer date.
                if step["kind"] in {"server_response", "certificate_time", "certificate_identity", "certificate_invalid"}:
                    break
            result["message"] = "未能自动恢复授权连接；激活凭证保持不变。" + result["steps"][-1]["message"]
            return result
        finally:
            self._repair_lock.release()
