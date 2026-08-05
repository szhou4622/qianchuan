"""首次启动本机管理员、离线恢复码和短期 UI 会话。"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .timeutils import utc_iso, utc_now

PBKDF2_ITERATIONS = 600_000
USERNAME_RE = re.compile(r"^[^\s]{3,32}$")


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
            raise AdminValidationError("账号需为3至32个非空白字符")
        if len(password) < 10:
            raise AdminValidationError("密码至少10位")
        if password.lower() == password or password.upper() == password:
            raise AdminValidationError("密码需同时包含大小写字母")
        if not any(char.isdigit() for char in password):
            raise AdminValidationError("密码需包含数字")


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
