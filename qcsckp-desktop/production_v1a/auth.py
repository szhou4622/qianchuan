"""首次启动本机管理员、离线恢复码和短期 UI 会话。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .constants import AUTH_OFFLINE_GRACE_HOURS, PRODUCT_VERSION
from .runtime_paths import RuntimePaths
from .security import (
    protect_for_current_windows_user,
    unprotect_for_current_windows_user,
)
from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .timeutils import utc_iso, utc_now

PBKDF2_ITERATIONS = 600_000
USERNAME_RE = re.compile(r"^[^\s]{2,32}$")


class AdminValidationError(ValueError):
    pass


def _password_hash(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
    ).hex()


def _recovery_hash(code: str) -> str:
    normalized = code.replace("-", "").strip().upper()
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _new_recovery_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(24))
    return "-".join(raw[index : index + 4] for index in range(0, len(raw), 4))


@dataclass(frozen=True)
class CreatedAdmin:
    tool_user_id: str
    username: str
    recovery_code: str


class LocalAdminService:
    def __init__(self, database: RuntimeDatabase, writer: StorageWriter):
        self.database = database
        self.writer = writer

    def admin_exists(self) -> bool:
        row = self.database.query_one("SELECT COUNT(*) AS c FROM tool_user")
        return bool(row and int(row["c"]) > 0)

    def create_initial_admin(self, username: str, password: str) -> CreatedAdmin:
        username = username.strip()
        self._validate(username, password)
        tool_user_id = f"user_{uuid.uuid4().hex}"
        salt = os.urandom(16).hex()
        recovery_code = _new_recovery_code()
        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                existing = conn.execute("SELECT 1 FROM tool_user LIMIT 1").fetchone()
                if existing:
                    raise AdminValidationError("本机管理员已存在")
                conn.execute(
                    """
                    INSERT INTO tool_user(
                        tool_user_id, username, password_salt, password_hash,
                        password_iterations, recovery_code_hash, status,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        tool_user_id,
                        username,
                        salt,
                        _password_hash(password, salt, PBKDF2_ITERATIONS),
                        PBKDF2_ITERATIONS,
                        _recovery_hash(recovery_code),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO qianchuan_identity(
                        login_identity_id, tool_user_id, login_status,
                        profile_path, created_at, updated_at
                    ) VALUES(?, ?, 'not_configured', ?, ?, ?)
                    """,
                    (
                        f"identity_{uuid.uuid4().hex}",
                        tool_user_id,
                        str(self.database.paths.browser_profile_dir),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO feishu_profile(
                        profile_uid, tool_user_id, created_at, updated_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (f"feishu_{uuid.uuid4().hex}", tool_user_id, now, now),
                )

        self.writer.submit(op)
        return CreatedAdmin(tool_user_id, username, recovery_code)

    def verify_password(self, username: str, password: str) -> dict[str, Any] | None:
        row = self.database.query_one(
            "SELECT * FROM tool_user WHERE username=? AND status='active'",
            (username.strip(),),
        )
        if not row:
            # 保持相近计算成本，降低本地旁路枚举差异。
            _password_hash(password, "00" * 16, PBKDF2_ITERATIONS)
            return None
        actual = _password_hash(
            password,
            str(row["password_salt"]),
            int(row["password_iterations"]),
        )
        if not hmac.compare_digest(actual, str(row["password_hash"])):
            return None
        return {
            "tool_user_id": str(row["tool_user_id"]),
            "username": str(row["username"]),
        }

    def reset_password_with_recovery_code(
        self,
        username: str,
        recovery_code: str,
        new_password: str,
    ) -> str:
        self._validate(username.strip(), new_password)
        row = self.database.query_one(
            "SELECT * FROM tool_user WHERE username=? AND status='active'",
            (username.strip(),),
        )
        if not row or not hmac.compare_digest(
            _recovery_hash(recovery_code), str(row["recovery_code_hash"])
        ):
            raise AdminValidationError("恢复码无效")
        salt = os.urandom(16).hex()
        replacement = _new_recovery_code()
        self.writer.execute(
            """
            UPDATE tool_user
            SET password_salt=?, password_hash=?, password_iterations=?,
                recovery_code_hash=?, updated_at=?
            WHERE tool_user_id=?
            """,
            (
                salt,
                _password_hash(new_password, salt, PBKDF2_ITERATIONS),
                PBKDF2_ITERATIONS,
                _recovery_hash(replacement),
                utc_iso(),
                row["tool_user_id"],
            ),
        )
        return replacement

    @staticmethod
    def _validate(username: str, password: str) -> None:
        if not USERNAME_RE.fullmatch(username):
            raise AdminValidationError("账号需为2至32个非空白字符")
        if len(password) < 6:
            raise AdminValidationError("密码至少6个字符")


class AdminSessionStore:
    """会话只驻留后台进程；服务重启后必须重新登录。"""

    def __init__(self, ttl_hours: int = 12):
        self.ttl = timedelta(hours=ttl_hours)
        self._sessions: dict[str, tuple[str, object]] = {}
        self._lock = threading.Lock()

    def issue(self, tool_user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        expires = utc_now() + self.ttl
        with self._lock:
            self._sessions[token] = (tool_user_id, expires)
        return token

    def resolve(self, token: str | None) -> str | None:
        if not token:
            return None
        now = utc_now()
        with self._lock:
            value = self._sessions.get(token)
            if not value:
                return None
            tool_user_id, expires = value
            if expires <= now:
                self._sessions.pop(token, None)
                return None
            return tool_user_id

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()


class CentralAuthError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status: int = 400,
    ):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        super().__init__(message)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class CentralAuthHttpClient:
    """只向中心服务发送工具账号、设备随机ID和认证令牌。"""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 6.0):
        configured = (
            base_url
            or os.getenv("QCSCKP_AUTH_BASE_URL")
            or "https://qcscjk.shanghaijiyue.com"
        ).strip().rstrip("/")
        parsed = urllib.parse.urlparse(configured)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise CentralAuthError(
                "insecure_auth_endpoint",
                "工具认证服务必须使用HTTPS",
            )
        self.base_url = configured
        self.timeout_seconds = timeout_seconds

    def login(self, username: str, password: str, device_id: str) -> dict[str, Any]:
        return self._request(
            "/api/auth/login.php",
            {
                "username": username,
                "password": password,
                "device_id": device_id,
                "client_version": PRODUCT_VERSION,
            },
        )

    def change_password(
        self, change_token: str, new_password: str, device_id: str
    ) -> dict[str, Any]:
        return self._request(
            "/api/auth/change-password.php",
            {"new_password": new_password, "device_id": device_id},
            token=change_token,
        )

    def refresh(self, access_token: str, device_id: str) -> dict[str, Any]:
        return self._request(
            "/api/auth/refresh.php",
            {"device_id": device_id, "client_version": PRODUCT_VERSION},
            token=access_token,
        )

    def logout(self, access_token: str, device_id: str) -> None:
        self._request(
            "/api/auth/logout.php",
            {"device_id": device_id},
            token=access_token,
        )

    def _request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": f"QCSCKP/{PRODUCT_VERSION}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                envelope = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                envelope = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise CentralAuthError(
                    "auth_http_error",
                    f"认证服务返回 HTTP {exc.code}",
                    status=exc.code,
                ) from exc
            error = envelope.get("error") or {}
            raise CentralAuthError(
                str(error.get("code") or "auth_rejected"),
                str(error.get("message") or "认证失败"),
                retryable=bool(error.get("retryable")),
                status=exc.code,
            ) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise CentralAuthError(
                "auth_service_unavailable",
                "暂时无法连接工具认证服务",
                retryable=True,
                status=503,
            ) from exc
        if not isinstance(envelope, dict) or not envelope.get("success"):
            error = envelope.get("error") if isinstance(envelope, dict) else {}
            raise CentralAuthError(
                str((error or {}).get("code") or "invalid_auth_response"),
                str((error or {}).get("message") or "认证服务响应无效"),
            )
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise CentralAuthError("invalid_auth_response", "认证服务未返回有效数据")
        return data


class DeviceIdentity:
    """每个安装运行目录生成一个随机ID；不读取硬盘、主板或MAC地址。"""

    def __init__(self, paths: RuntimePaths):
        self.path = paths.secrets_dir / "device-id.dpapi"

    def get_or_create(self) -> str:
        override = (os.getenv("QCSCKP_TEST_DEVICE_ID") or "").strip()
        if override:
            return override
        if self.path.is_file():
            return unprotect_for_current_windows_user(
                self.path.read_text(encoding="ascii").strip()
            )
        value = f"device_{uuid.uuid4().hex}"
        protected = protect_for_current_windows_user(value)
        self.path.write_text(protected, encoding="ascii")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return value


@dataclass(frozen=True)
class AuthenticatedUser:
    tool_user_id: str
    username: str
    auth_state: str
    valid_until: str


class CentralAuthService:
    """中心账号门禁；千川和飞书业务数据始终只保存在本机。"""

    def __init__(
        self,
        database: RuntimeDatabase,
        writer: StorageWriter,
        paths: RuntimePaths,
        client: CentralAuthHttpClient | Any | None = None,
    ):
        self.database = database
        self.writer = writer
        self.paths = paths
        self.client = client or CentralAuthHttpClient()
        self.device = DeviceIdentity(paths)

    def login(self, username: str, password: str) -> dict[str, Any]:
        username = username.strip()
        if not username or not password:
            raise CentralAuthError("invalid_credentials", "请输入工具账号和密码")
        result = self.client.login(username, password, self.device.get_or_create())
        if bool(result.get("must_change_password")):
            return {
                "must_change_password": True,
                "change_token": str(result.get("change_token") or ""),
                "username": username,
            }
        user = self._persist_online_auth(result)
        return {"must_change_password": False, **user.__dict__}

    def change_initial_password(
        self, change_token: str, new_password: str
    ) -> AuthenticatedUser:
        LocalAdminService._validate("remote_user", new_password)
        result = self.client.change_password(
            change_token, new_password, self.device.get_or_create()
        )
        return self._persist_online_auth(result)

    def restore(self) -> AuthenticatedUser | None:
        row = self.database.query_one(
            """
            SELECT c.*, u.username, u.status AS user_status
            FROM remote_auth_cache c
            JOIN tool_user u ON u.tool_user_id=c.tool_user_id
            WHERE c.auth_status IN ('active','offline_grace')
            ORDER BY c.last_used_at DESC LIMIT 1
            """
        )
        if not row or row["user_status"] != "active":
            return None
        now = utc_now()
        if _parse_utc(str(row["offline_grace_until"])) <= now:
            self._mark_auth(str(row["tool_user_id"]), "expired", "offline_grace_expired")
            return None
        try:
            token = unprotect_for_current_windows_user(
                str(row["encrypted_access_token"])
            )
            refreshed = self.client.refresh(token, self.device.get_or_create())
            return self._persist_online_auth(refreshed)
        except CentralAuthError as exc:
            if not exc.retryable:
                status = (
                    "device_mismatch"
                    if exc.code == "device_mismatch"
                    else "disabled"
                    if exc.code in {"account_disabled", "account_expired"}
                    else "expired"
                )
                self._mark_auth(str(row["tool_user_id"]), status, exc.code, exc.message)
                return None
            self._mark_auth(
                str(row["tool_user_id"]),
                "offline_grace",
                exc.code,
                exc.message,
            )
            return AuthenticatedUser(
                str(row["tool_user_id"]),
                str(row["username"]),
                "offline_grace",
                str(row["offline_grace_until"]),
            )

    def logout(self, tool_user_id: str) -> None:
        row = self.database.query_one(
            "SELECT * FROM remote_auth_cache WHERE tool_user_id=?",
            (tool_user_id,),
        )
        if row and row.get("encrypted_access_token"):
            try:
                token = unprotect_for_current_windows_user(
                    str(row["encrypted_access_token"])
                )
                self.client.logout(token, self.device.get_or_create())
            except Exception:
                pass
        self.writer.execute(
            """
            UPDATE remote_auth_cache
            SET encrypted_access_token='', auth_status='logged_out',
                last_error_code=NULL, last_error_message=NULL, updated_at=?
            WHERE tool_user_id=?
            """,
            (utc_iso(), tool_user_id),
        )

    def is_runtime_authorized(self, tool_user_id: str) -> bool:
        row = self.database.query_one(
            """
            SELECT c.auth_status, c.offline_grace_until, u.status AS user_status
            FROM remote_auth_cache c JOIN tool_user u USING(tool_user_id)
            WHERE c.tool_user_id=?
            """,
            (tool_user_id,),
        )
        return bool(
            row
            and row["user_status"] == "active"
            and row["auth_status"] in {"active", "offline_grace"}
            and _parse_utc(str(row["offline_grace_until"])) > utc_now()
        )

    def status(self, tool_user_id: str | None = None) -> dict[str, Any]:
        if tool_user_id:
            row = self.database.query_one(
                "SELECT c.*, u.username FROM remote_auth_cache c JOIN tool_user u USING(tool_user_id) WHERE c.tool_user_id=?",
                (tool_user_id,),
            )
        else:
            row = self.database.query_one(
                "SELECT c.*, u.username FROM remote_auth_cache c JOIN tool_user u USING(tool_user_id) ORDER BY c.last_used_at DESC LIMIT 1"
            )
        if not row:
            return {"configured": False, "state": "login_required"}
        return {
            "configured": True,
            "state": row["auth_status"],
            "username": row["username"],
            "tool_user_id": row["tool_user_id"],
            "last_online_verified_at": row["last_online_verified_at"],
            "offline_grace_until": row["offline_grace_until"],
        }

    def _persist_online_auth(self, result: dict[str, Any]) -> AuthenticatedUser:
        required = (
            "remote_account_id",
            "tool_user_id",
            "username",
            "access_token",
            "token_expires_at",
            "offline_grace_until",
        )
        if any(not str(result.get(key) or "") for key in required):
            raise CentralAuthError("invalid_auth_response", "认证服务返回字段不完整")
        remote_account_id = str(result["remote_account_id"])
        requested_user_id = str(result["tool_user_id"])
        username = str(result["username"]).strip()
        now = utc_iso()
        encrypted_token = protect_for_current_windows_user(str(result["access_token"]))

        def op(conn):
            with short_transaction(conn):
                mapping = conn.execute(
                    "SELECT tool_user_id FROM remote_auth_cache WHERE remote_account_id=?",
                    (remote_account_id,),
                ).fetchone()
                same_name = conn.execute(
                    "SELECT tool_user_id FROM tool_user WHERE username=?",
                    (username,),
                ).fetchone()
                local_user_id = str(
                    mapping["tool_user_id"]
                    if mapping
                    else same_name["tool_user_id"]
                    if same_name
                    else requested_user_id
                )
                conn.execute(
                    """
                    INSERT INTO tool_user(
                        tool_user_id, username, password_salt, password_hash,
                        password_iterations, recovery_code_hash, status,
                        created_at, updated_at
                    ) VALUES(?, ?, 'remote', 'remote', 1, 'remote', 'active', ?, ?)
                    ON CONFLICT(tool_user_id) DO UPDATE SET
                        username=excluded.username, status='active', updated_at=excluded.updated_at
                    """,
                    (local_user_id, username, now, now),
                )
                profile_path = str(self.paths.browser_profile_dir / local_user_id)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO qianchuan_identity(
                        login_identity_id, tool_user_id, login_status,
                        profile_path, created_at, updated_at
                    ) VALUES(?, ?, 'not_configured', ?, ?, ?)
                    """,
                    (f"identity_{uuid.uuid4().hex}", local_user_id, profile_path, now, now),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO feishu_profile(
                        profile_uid, tool_user_id, created_at, updated_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (f"feishu_{uuid.uuid4().hex}", local_user_id, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO remote_auth_cache(
                        tool_user_id, remote_account_id, encrypted_access_token,
                        token_expires_at, last_online_verified_at,
                        offline_grace_until, auth_status, last_used_at,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    ON CONFLICT(tool_user_id) DO UPDATE SET
                        remote_account_id=excluded.remote_account_id,
                        encrypted_access_token=excluded.encrypted_access_token,
                        token_expires_at=excluded.token_expires_at,
                        last_online_verified_at=excluded.last_online_verified_at,
                        offline_grace_until=excluded.offline_grace_until,
                        auth_status='active', last_error_code=NULL,
                        last_error_message=NULL, last_used_at=excluded.last_used_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        local_user_id,
                        remote_account_id,
                        encrypted_token,
                        str(result["token_expires_at"]),
                        now,
                        str(result["offline_grace_until"]),
                        now,
                        now,
                        now,
                    ),
                )
                return local_user_id

        local_user_id = str(self.writer.submit(op))
        return AuthenticatedUser(
            local_user_id,
            username,
            "active",
            str(result.get("valid_until") or result["offline_grace_until"]),
        )

    def _mark_auth(
        self,
        tool_user_id: str,
        status: str,
        error_code: str,
        error_message: str | None = None,
    ) -> None:
        self.writer.execute(
            """
            UPDATE remote_auth_cache
            SET auth_status=?, last_error_code=?, last_error_message=?,
                last_used_at=?, updated_at=? WHERE tool_user_id=?
            """,
            (
                status,
                error_code,
                (error_message or "")[:500],
                utc_iso(),
                utc_iso(),
                tool_user_id,
            ),
        )
