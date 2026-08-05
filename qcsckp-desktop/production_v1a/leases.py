"""跨崩溃持久化租约、心跳与 fencing token。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .timeutils import utc_iso, utc_now


@dataclass(frozen=True)
class Lease:
    resource_key: str
    owner_instance_id: str
    task_uid: str
    priority: int
    fencing_token: int
    expires_at: str


class LeaseConflict(RuntimeError):
    pass


class StaleFencingToken(RuntimeError):
    pass


class LeaseManager:
    def __init__(self, database: RuntimeDatabase, writer: StorageWriter):
        self.database = database
        self.writer = writer

    def acquire(
        self,
        resource_key: str,
        owner_instance_id: str,
        task_uid: str,
        priority: int,
        ttl_seconds: int = 30,
    ) -> Lease | None:
        now_dt = utc_now()
        now = utc_iso(now_dt)
        expires = utc_iso(now_dt + timedelta(seconds=ttl_seconds))

        def op(conn):
            with short_transaction(conn):
                row = conn.execute(
                    "SELECT * FROM task_lease WHERE resource_key=?", (resource_key,)
                ).fetchone()
                if row and str(row["expires_at"]) > now:
                    if (
                        row["owner_instance_id"] == owner_instance_id
                        and row["task_uid"] == task_uid
                    ):
                        conn.execute(
                            "UPDATE task_lease SET heartbeat_at=?, expires_at=? WHERE resource_key=?",
                            (now, expires, resource_key),
                        )
                        return Lease(
                            resource_key,
                            owner_instance_id,
                            task_uid,
                            priority,
                            int(row["fencing_token"]),
                            expires,
                        )
                    return None
                token = int(row["fencing_token"]) + 1 if row else 1
                conn.execute(
                    """
                    INSERT INTO task_lease(
                        resource_key, owner_instance_id, task_uid, priority,
                        acquired_at, heartbeat_at, expires_at, fencing_token
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(resource_key) DO UPDATE SET
                        owner_instance_id=excluded.owner_instance_id,
                        task_uid=excluded.task_uid,
                        priority=excluded.priority,
                        acquired_at=excluded.acquired_at,
                        heartbeat_at=excluded.heartbeat_at,
                        expires_at=excluded.expires_at,
                        fencing_token=excluded.fencing_token
                    """,
                    (
                        resource_key,
                        owner_instance_id,
                        task_uid,
                        priority,
                        now,
                        now,
                        expires,
                        token,
                    ),
                )
                return Lease(
                    resource_key,
                    owner_instance_id,
                    task_uid,
                    priority,
                    token,
                    expires,
                )

        return self.writer.submit(op)

    def heartbeat(self, lease: Lease, ttl_seconds: int = 30) -> Lease:
        now_dt = utc_now()
        expires = utc_iso(now_dt + timedelta(seconds=ttl_seconds))
        changed = self.writer.execute(
            """
            UPDATE task_lease SET heartbeat_at=?, expires_at=?
            WHERE resource_key=? AND owner_instance_id=? AND task_uid=? AND fencing_token=?
            """,
            (
                utc_iso(now_dt),
                expires,
                lease.resource_key,
                lease.owner_instance_id,
                lease.task_uid,
                lease.fencing_token,
            ),
        )
        if changed != 1:
            raise StaleFencingToken("lease owner or fencing token changed")
        return Lease(
            lease.resource_key,
            lease.owner_instance_id,
            lease.task_uid,
            lease.priority,
            lease.fencing_token,
            expires,
        )

    def release(self, lease: Lease) -> bool:
        return (
            self.writer.execute(
                """
                DELETE FROM task_lease
                WHERE resource_key=? AND owner_instance_id=? AND task_uid=? AND fencing_token=?
                """,
                (
                    lease.resource_key,
                    lease.owner_instance_id,
                    lease.task_uid,
                    lease.fencing_token,
                ),
            )
            == 1
        )

    def assert_current(self, lease: Lease) -> None:
        row = self.database.query_one(
            "SELECT * FROM task_lease WHERE resource_key=?", (lease.resource_key,)
        )
        if (
            not row
            or row["owner_instance_id"] != lease.owner_instance_id
            or row["task_uid"] != lease.task_uid
            or int(row["fencing_token"]) != lease.fencing_token
            or str(row["expires_at"]) <= utc_iso()
        ):
            raise StaleFencingToken("stale or expired lease")
