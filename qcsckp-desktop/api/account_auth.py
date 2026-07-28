"""
远程账号登录校验（POST /api/account.php）
桌面端版本检测（GET/POST /api/version.php）
文档：dev_files/api文档.md、dev_files/版本更新api文档.md
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import API_BASE_URL


def _decode_http_body(raw: bytes) -> str:
    """HTTP 正文可能是 UTF-8 JSON，也可能是国内服务器返回的 GBK 错误页；避免 strict utf-8 抛错。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "gb2312"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _remote_url(path: str) -> str:
    """path 以 / 开头，如 /api/account.php"""
    p = path if path.startswith("/") else "/" + path
    return API_BASE_URL.rstrip("/") + p


class AccountAuthApi:
    """调用服务端账号校验、版本检测等远程接口。"""

    def _request_json(self, req: Request) -> Dict[str, Any]:
        """执行 HTTP 请求并解析 JSON，错误时返回 success=False。"""
        try:
            with urlopen(req, timeout=30) as resp:
                body = _decode_http_body(resp.read())
                status = getattr(resp, "status", 200)
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    return {
                        "success": False,
                        "message": "服务器返回非 JSON",
                        "http_status": status,
                    }
                if isinstance(parsed, dict) and "http_status" not in parsed:
                    parsed["http_status"] = status
                return parsed if isinstance(parsed, dict) else {"success": False, "message": "响应格式异常"}
        except HTTPError as e:
            raw = _decode_http_body(e.read()) if e.fp else ""
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    if "http_status" not in parsed:
                        parsed["http_status"] = e.code
                    return parsed
            except json.JSONDecodeError:
                pass
            return {
                "success": False,
                "message": raw or str(e),
                "http_status": e.code,
            }
        except URLError as e:
            reason = getattr(e.reason, "strerror", None) or str(e.reason)
            return {"success": False, "message": f"网络错误: {reason}"}

    def verify_login(self, username: str, password: str) -> Dict[str, Any]:
        """
        POST JSON：username、password（首尾空格会从 username 去除，与文档一致）。

        Returns:
            服务端 JSON；网络/解析失败时 success=False，并带 message。
        """
        u = (username or "").strip()
        p = password if password is not None else ""
        if not u or not p:
            return {"success": False, "message": "请提供账号和密码"}

        payload = json.dumps({"username": u, "password": p}, ensure_ascii=False).encode("utf-8")
        req = Request(_remote_url("/api/account.php"), data=payload)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        result = self._request_json(req)
        data = result.get("data") if isinstance(result, dict) else None
        if result.get("success") and isinstance(data, dict):
            try:
                disabled = int(data.get("is_disabled") or 0) == 1
            except Exception:
                disabled = True
            if not disabled and self._is_within_validity(data):
                try:
                    from services.cloud_retarget_client import register_device_session

                    device = register_device_session(u, p)
                    data["device_session_ready"] = bool(device.get("success"))
                    if not device.get("success"):
                        data["device_session_message"] = str(device.get("message") or "设备令牌申请失败")
                except Exception as exc:
                    data["device_session_ready"] = False
                    data["device_session_message"] = str(exc)
        return result

    @staticmethod
    def _parse_server_dt(s: Optional[str]) -> Optional[datetime]:
        if not s or not str(s).strip():
            return None
        try:
            return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _is_within_validity(self, data: Dict[str, Any]) -> bool:
        now = datetime.now()
        vf = self._parse_server_dt(data.get("valid_from"))
        vu = self._parse_server_dt(data.get("valid_until"))
        if vf and now < vf:
            return False
        if vu and now > vu:
            return False
        return True

    def verify_can_start_service(self, username: str, password: str) -> Dict[str, Any]:
        """
        启动采集前必须：远程校验成功、未禁用、在有效期内。
        返回 {"ok": True, "data": ...} 或 {"ok": False, "message": "..."}
        """
        u = (username or "").strip()
        p = password if password is not None else ""
        if not u or not p:
            return {"ok": False, "message": "请提供账号和密码"}
        res = self.verify_login(u, p)
        if not res.get("success"):
            return {"ok": False, "message": res.get("message") or "账号校验失败"}
        data = res.get("data") or {}
        if int(data.get("is_disabled") or 0) == 1:
            return {"ok": False, "message": "账号已禁用或所属代理已禁用"}
        if not self._is_within_validity(data):
            return {"ok": False, "message": "账号不在有效期内"}
        return {"ok": True, "data": data}

    def check_version_update(self, current_version: str = "") -> Dict[str, Any]:
        """
        查询服务器最新桌面版本是否与当前版本不一致（GET + 查询参数）。

        Args:
            current_version: 客户端当前版本号；空字符串时服务端按与 0 比对（见文档）。

        Returns:
            服务端 JSON（success + data.latest_version / has_update / download_url 等）。
        """
        cv = (current_version or "").strip()
        update_url = "/api/version_mac.php" if sys.platform == "darwin" else "/api/version.php"
        url = _remote_url(update_url)
        if cv:
            url = url + "?" + urlencode({"current_version": cv})
        req = Request(url)
        req.add_header("Accept", "application/json")
        return self._request_json(req)
