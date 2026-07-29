# -*- coding: utf-8 -*-
"""当前工具账号的单一千川登录会话（Windows DPAPI 加密）。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from config import DATA_DIR


SESSION_FILE = os.path.join(DATA_DIR, "qianchuan_sessions.json")
LEGACY_COOKIE_FILE = os.path.join(DATA_DIR, "qcookie.json")
LEGACY_ROLLBACK_FILE = os.path.join(DATA_DIR, "qcookie.legacy.rc23.json")
SESSION_LOCK = threading.RLock()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _session_epoch(profile: Dict[str, Any]) -> int:
    try:
        return max(1, int(profile.get("session_epoch") or 1))
    except (TypeError, ValueError):
        return 1


def current_session_owner() -> str:
    override = str(os.getenv("QCSCKP_SESSION_OWNER") or "").strip().casefold()
    if override:
        return override
    try:
        from services.cloud_retarget_client import load_device_session

        return str(
            (load_device_session() or {}).get("username") or ""
        ).strip().casefold()
    except Exception:
        return ""


def _atomic_save(payload: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = SESSION_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, SESSION_FILE)


def _load_file() -> Dict[str, Any]:
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "profiles": {}}


def _protect_json(state: Dict[str, Any]) -> str:
    if os.name != "nt":
        raise RuntimeError("千川登录状态加密当前仅支持Windows")
    try:
        import win32crypt

        raw = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        protected = win32crypt.CryptProtectData(
            raw,
            "qcsckp-qianchuan-storage-state",
            None,
            None,
            None,
            0,
        )
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    except Exception as exc:
        raise RuntimeError(f"无法使用Windows保护千川登录状态：{exc}") from exc


def _unprotect_json(ciphertext: str) -> Optional[Dict[str, Any]]:
    text = str(ciphertext or "")
    if not text.startswith("dpapi:") or os.name != "nt":
        return None
    try:
        import win32crypt

        raw = base64.b64decode(text[6:])
        clear = win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1]
        state = json.loads(clear.decode("utf-8"))
        if isinstance(state, dict) and isinstance(state.get("cookies", []), list):
            return state
    except Exception:
        return None
    return None


def _read_legacy_cookie() -> Optional[Dict[str, Any]]:
    for path in (LEGACY_COOKIE_FILE, LEGACY_ROLLBACK_FILE):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            if isinstance(state, dict) and isinstance(state.get("cookies", []), list):
                return state
        except Exception:
            continue
    return None


def load_qianchuan_storage_state(
    owner_username: Any = None,
) -> Optional[Dict[str, Any]]:
    owner = str(
        owner_username or current_session_owner() or ""
    ).strip().casefold()
    with SESSION_LOCK:
        if owner:
            profile = (_load_file().get("profiles") or {}).get(owner) or {}
            state = _unprotect_json(str(profile.get("storage_state_protected") or ""))
            if state is not None:
                return state
            # 已登录工具账号时绝不借用其他账号或rc23回滚副本的会话。
            return None
        # 尚未登录工具账号时仅供升级识别；保存仍必须绑定工具账号。
        return _read_legacy_cookie()


def save_qianchuan_storage_state(
    state: Dict[str, Any],
    *,
    owner_username: Any = None,
) -> Dict[str, Any]:
    if not isinstance(state, dict) or not isinstance(state.get("cookies", []), list):
        raise ValueError("千川登录状态格式无效")
    owner = str(owner_username or current_session_owner() or "").strip().casefold()
    if not owner:
        raise RuntimeError("请先登录工具账号，再保存千川登录状态")
    protected = _protect_json(state)
    with SESSION_LOCK:
        data = _load_file()
        profiles = data.setdefault("profiles", {})
        current = profiles.get(owner)
        profile = dict(current) if isinstance(current, dict) else {}
        profile.update(
            {
                "storage_state_protected": protected,
                "status": "available",
                "last_error": "",
                "session_epoch": _session_epoch(profile),
                "updated_at": _now_text(),
            }
        )
        profiles[owner] = profile
        _atomic_save(data)
    return session_status(owner)


async def save_context_storage_state(
    context: Any,
    *,
    owner_username: Any = None,
) -> Dict[str, Any]:
    if context is None:
        raise RuntimeError("浏览器上下文不存在")
    state = await context.storage_state()
    return save_qianchuan_storage_state(
        state,
        owner_username=owner_username,
    )


def migrate_legacy_qcookie() -> Dict[str, Any]:
    owner = current_session_owner()
    if not owner:
        return {
            "success": False,
            "migrated": False,
            "message": "工具账号尚未登录，暂不迁移千川登录状态",
        }
    with SESSION_LOCK:
        existing = (_load_file().get("profiles") or {}).get(owner) or {}
        if _unprotect_json(str(existing.get("storage_state_protected") or "")) is not None:
            return {"success": True, "migrated": False, "message": "千川登录状态已加密"}
        state = _read_legacy_cookie()
        if state is None:
            return {"success": True, "migrated": False, "message": "尚无千川登录状态"}
        if os.path.isfile(LEGACY_COOKIE_FILE) and not os.path.isfile(LEGACY_ROLLBACK_FILE):
            shutil.copy2(LEGACY_COOKIE_FILE, LEGACY_ROLLBACK_FILE)
        save_qianchuan_storage_state(state, owner_username=owner)
        try:
            os.remove(LEGACY_COOKIE_FILE)
        except FileNotFoundError:
            pass
        return {
            "success": True,
            "migrated": True,
            "message": "千川登录状态已迁入Windows加密存储",
        }


def mark_qianchuan_session_invalid(
    message: Any,
    *,
    owner_username: Any = None,
) -> None:
    owner = str(
        owner_username or current_session_owner() or ""
    ).strip().casefold()
    if not owner:
        return
    with SESSION_LOCK:
        data = _load_file()
        profiles = data.setdefault("profiles", {})
        current = profiles.get(owner)
        profile = dict(current) if isinstance(current, dict) else {}
        epoch = _session_epoch(profile) + 1
        profile.update(
            {
                "status": "login_required",
                "last_error": str(message or "千川登录状态已失效")[:1000],
                "session_epoch": epoch,
                "updated_at": _now_text(),
            }
        )
        profiles[owner] = profile
        _atomic_save(data)
    # 登录失效前生成或批准的任务不能在重新登录后补执行。
    try:
        from services.local_feishu_bridge import cancel_active_local_retarget_tasks

        cancel_active_local_retarget_tasks(
            owner,
            str(message or "千川登录状态已失效，请重新命中规则并再次确认"),
        )
    except Exception:
        # 会话总闸已经关闭；卡片状态同步失败也不能重新放行写操作。
        pass


def mark_qianchuan_session_available(*, owner_username: Any = None) -> None:
    owner = str(
        owner_username or current_session_owner() or ""
    ).strip().casefold()
    if not owner:
        return
    with SESSION_LOCK:
        data = _load_file()
        profiles = data.setdefault("profiles", {})
        current = profiles.get(owner)
        profile = dict(current) if isinstance(current, dict) else {}
        if not profile:
            return
        profile.update(
            {
                "status": "available",
                "last_error": "",
                "session_epoch": _session_epoch(profile),
                "updated_at": _now_text(),
            }
        )
        profiles[owner] = profile
        _atomic_save(data)


def has_qianchuan_session() -> bool:
    return load_qianchuan_storage_state() is not None


def automation_session_ready(owner_username: Any = None) -> Dict[str, Any]:
    """写操作总闸：已知登录失效时，即使旧Cookie仍可解密也必须暂停。"""
    status = session_status(owner_username)
    ready = bool(status.get("available")) and str(
        status.get("status") or ""
    ) != "login_required"
    return {
        **status,
        "ready": ready,
        "message": (
            ""
            if ready
            else str(status.get("last_error") or "千川登录状态不存在或已失效")
        ),
    }


def session_status(owner_username: Any = None) -> Dict[str, Any]:
    owner = str(owner_username or current_session_owner() or "").strip().casefold()
    with SESSION_LOCK:
        profile = (
            ((_load_file().get("profiles") or {}).get(owner) or {})
            if owner
            else {}
        )
        encrypted = bool(str(profile.get("storage_state_protected") or "").startswith("dpapi:"))
        available = _unprotect_json(
            str(profile.get("storage_state_protected") or "")
        ) is not None
        if not available and not owner:
            available = _read_legacy_cookie() is not None
    return {
        "success": True,
        "owner_username": owner,
        "available": available,
        "encrypted": encrypted,
        "status": str(
            profile.get("status")
            or ("available" if available else "not_configured")
        ),
        "updated_at": str(profile.get("updated_at") or ""),
        "last_error": str(profile.get("last_error") or ""),
        "session_epoch": _session_epoch(profile),
        "legacy_cookie_present": os.path.isfile(LEGACY_COOKIE_FILE),
        "rollback_cookie_present": os.path.isfile(LEGACY_ROLLBACK_FILE),
    }


def restore_rc23_cookie() -> Dict[str, Any]:
    """显式回滚时恢复 rc23 读取的 qcookie.json。"""
    state = load_qianchuan_storage_state()
    if state is None:
        return {"success": False, "message": "没有可恢复的千川登录状态"}
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = LEGACY_COOKIE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(temp, LEGACY_COOKIE_FILE)
    return {"success": True, "message": "已恢复rc23兼容的千川登录状态"}
