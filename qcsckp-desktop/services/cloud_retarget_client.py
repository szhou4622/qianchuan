# -*- coding: utf-8 -*-
"""追投卡片云端任务客户端；只保存可撤销设备令牌，不保存账号密码。"""
from __future__ import annotations

import json
import os
import platform
import socket
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import API_BASE_URL, DATA_DIR


SESSION_FILE = os.path.join(DATA_DIR, "device_session.json")


def _url(path: str) -> str:
    return API_BASE_URL.rstrip("/") + (path if path.startswith("/") else "/" + path)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace") if raw else ""


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    token: str = "",
    timeout: int = 30,
) -> Dict[str, Any]:
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(_url(path), data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-Device-Name", device_name())
    try:
        with urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(_decode(resp.read()))
            return parsed if isinstance(parsed, dict) else {"success": False, "message": "响应格式异常"}
    except HTTPError as exc:
        raw = _decode(exc.read()) if exc.fp else ""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed.setdefault("http_status", exc.code)
                return parsed
        except Exception:
            pass
        return {"success": False, "message": raw or str(exc), "http_status": exc.code}
    except (URLError, TimeoutError, OSError) as exc:
        return {"success": False, "message": f"云端连接失败: {exc}"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def device_name() -> str:
    host = socket.gethostname() or "desktop"
    return f"{host}-{platform.system()}"[:120]


def _atomic_save(data: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSION_FILE)


def load_device_session() -> Dict[str, Any]:
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clear_device_session() -> Dict[str, Any]:
    token = str(load_device_session().get("token") or "").strip()
    result = _request("/api/device/session.php", method="DELETE", token=token) if token else {"success": True}
    try:
        os.remove(SESSION_FILE)
    except FileNotFoundError:
        pass
    return result


def register_device_session(username: str, password: str) -> Dict[str, Any]:
    res = _request(
        "/api/device/session.php",
        method="POST",
        payload={"username": username, "password": password, "device_name": device_name()},
    )
    data = res.get("data") if isinstance(res, dict) else None
    if res.get("success") and isinstance(data, dict) and data.get("token"):
        _atomic_save(
            {
                "username": str(data.get("username") or username),
                "token": str(data["token"]),
                "device_name": str(data.get("device_name") or device_name()),
            }
        )
    return res


def _token() -> str:
    return str(load_device_session().get("token") or "").strip()


def create_retarget_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    token = _token()
    if not token:
        return {"success": False, "message": "桌面端尚未取得设备令牌，请重新登录工具账号"}
    return _request("/api/retarget_tasks/create.php", method="POST", payload=payload, token=token)


def pull_retarget_task() -> Dict[str, Any]:
    token = _token()
    if not token:
        return {"success": False, "message": "missing_device_session", "silent": True}
    return _request("/api/retarget_tasks/pull.php", token=token, timeout=35)


def report_retarget_task(
    task_uid: str,
    claim_token: str,
    status: str,
    *,
    message: str = "",
    detail: str = "",
    regulate_task_id: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    token = _token()
    if not token:
        return {"success": False, "message": "missing_device_session"}
    return _request(
        "/api/retarget_tasks/result.php",
        method="POST",
        token=token,
        payload={
            "task_uid": task_uid,
            "claim_token": claim_token,
            "status": status,
            "message": message,
            "detail": detail,
            "regulate_task_id": regulate_task_id,
            "result": result or {},
        },
    )
