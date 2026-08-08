"""持久化后台作业、事件流和可恢复 Worker。"""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .leases import Lease, LeaseManager
from .security import redact_mapping, sanitize_exception_text
from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .timeutils import utc_iso


class EventBus:
    """进程内发布，事实仍由 runtime.db 持久化。"""

    def __init__(self):
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def publish(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("event_uid", f"evt_{uuid.uuid4().hex}")
        payload.setdefault("created_at", utc_iso())
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(payload)
                except (queue.Empty, queue.Full):
                    pass

    def subscribe(self, maxsize: int = 200) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)


@dataclass(frozen=True)
class JobContext:
    job_uid: str
    tool_user_id: str | None
    payload: dict[str, Any]
    lease: Lease
    update_progress: Callable[[int, int, str], None]


JobHandler = Callable[[JobContext], dict[str, Any] | None]


class JobService:
    def __init__(
        self,
        database: RuntimeDatabase,
        writer: StorageWriter,
        leases: LeaseManager,
        events: EventBus,
        owner_instance_id: str,
    ):
        self.database = database
        self.writer = writer
        self.leases = leases
        self.events = events
        self.owner_instance_id = owner_instance_id
        self.handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        if job_type in self.handlers:
            raise ValueError(f"duplicate job handler: {job_type}")
        self.handlers[job_type] = handler

    def create(
        self,
        job_type: str,
        payload: dict[str, Any] | None,
        *,
        tool_user_id: str | None,
        priority: int = 100,
    ) -> str:
        if job_type not in self.handlers:
            raise ValueError(f"unknown job type: {job_type}")
        job_uid = f"job_{uuid.uuid4().hex}"
        now = utc_iso()
        self.writer.execute(
            """
            INSERT INTO background_job(
                job_uid, tool_user_id, job_type, priority, payload_json,
                status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_uid,
                tool_user_id,
                job_type,
                priority,
                json.dumps(redact_mapping(payload or {}), ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        self.events.publish({"type": "job.created", "job_uid": job_uid})
        return job_uid

    def get(self, job_uid: str) -> dict[str, Any] | None:
        return self.database.query_one(
            "SELECT * FROM background_job WHERE job_uid=?", (job_uid,)
        )

    def claim_next(self) -> tuple[dict[str, Any], Lease] | None:
        rows = self.database.query_all(
            """
            SELECT * FROM background_job
            WHERE status='queued'
            ORDER BY priority ASC, created_at ASC
            LIMIT 20
            """
        )
        for row in rows:
            lease = self.leases.acquire(
                "browser-worker",
                self.owner_instance_id,
                str(row["job_uid"]),
                int(row["priority"]),
                ttl_seconds=45,
            )
            if not lease:
                return None

            def op(conn):
                with short_transaction(conn):
                    cursor = conn.execute(
                        """
                        UPDATE background_job
                        SET status='running', lease_owner=?, lease_expires_at=?,
                            fencing_token=?, started_at=COALESCE(started_at, ?), updated_at=?
                        WHERE job_uid=? AND status='queued'
                        """,
                        (
                            lease.owner_instance_id,
                            lease.expires_at,
                            lease.fencing_token,
                            utc_iso(),
                            utc_iso(),
                            row["job_uid"],
                        ),
                    )
                    return cursor.rowcount

            if self.writer.submit(op) == 1:
                claimed = self.get(str(row["job_uid"]))
                assert claimed is not None
                return claimed, lease
            self.leases.release(lease)
        return None

    def recover_expired(self) -> int:
        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                cursor = conn.execute(
                    """
                    UPDATE background_job
                    SET status='queued', lease_owner=NULL, lease_expires_at=NULL,
                        progress_message='Worker租约过期，已安全重新排队', updated_at=?
                    WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                    """,
                    (now, now),
                )
                conn.execute("DELETE FROM task_lease WHERE expires_at <= ?", (now,))
                return cursor.rowcount

        recovered = self.writer.submit(op)
        if recovered:
            self.events.publish({"type": "job.recovered", "count": recovered})
        return int(recovered)

    def _progress(self, job_uid: str, lease: Lease, current: int, total: int, message: str) -> None:
        self.leases.assert_current(lease)
        self.writer.execute(
            """
            UPDATE background_job
            SET progress_current=?, progress_total=?, progress_message=?, updated_at=?
            WHERE job_uid=? AND fencing_token=? AND status='running'
            """,
            (current, total, message, utc_iso(), job_uid, lease.fencing_token),
        )

    def _heartbeat_claim(self, job_uid: str, lease: Lease) -> Lease:
        renewed = self.leases.heartbeat(lease, ttl_seconds=45)
        changed = self.writer.execute(
            """
            UPDATE background_job SET lease_expires_at=?, updated_at=?
            WHERE job_uid=? AND fencing_token=? AND status='running'
            """,
            (renewed.expires_at, utc_iso(), job_uid, lease.fencing_token),
        )
        if changed != 1:
            raise RuntimeError("background_job_lease_lost")
        return renewed
        self.events.publish(
            {
                "type": "job.progress",
                "job_uid": job_uid,
                "current": current,
                "total": total,
                "message": message,
            }
        )

    def execute_claimed(self, job: dict[str, Any], lease: Lease) -> None:
        job_uid = str(job["job_uid"])
        handler = self.handlers[str(job["job_type"])]
        payload = json.loads(str(job["payload_json"] or "{}"))
        lease_ref = [lease]
        heartbeat_stop = threading.Event()
        heartbeat_error: list[BaseException] = []

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(15):
                try:
                    lease_ref[0] = self._heartbeat_claim(job_uid, lease_ref[0])
                except BaseException as exc:
                    heartbeat_error.append(exc)
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            name=f"qcsckp-v1a-job-heartbeat-{job_uid[-8:]}",
            daemon=True,
        )
        context = JobContext(
            job_uid=job_uid,
            tool_user_id=job.get("tool_user_id"),
            payload=payload,
            lease=lease_ref[0],
            update_progress=lambda current, total, message: self._progress(
                job_uid, lease_ref[0], current, total, message
            ),
        )
        heartbeat_thread.start()
        try:
            result = handler(context) or {}
            if heartbeat_error:
                raise heartbeat_error[0]
            self.leases.assert_current(lease_ref[0])
            self.writer.execute(
                """
                UPDATE background_job
                SET status='succeeded', result_json=?, completed_at=?, updated_at=?
                WHERE job_uid=? AND fencing_token=? AND status='running'
                """,
                (
                    json.dumps(redact_mapping(result), ensure_ascii=False, sort_keys=True),
                    utc_iso(),
                    utc_iso(),
                    job_uid,
                    lease.fencing_token,
                ),
            )
            self.events.publish({"type": "job.succeeded", "job_uid": job_uid})
        except Exception as exc:
            blocked = type(exc).__name__ in {"LoginRequired", "UserActionBlocked"}
            terminal_status = "blocked_user_action" if blocked else "failed"
            try:
                self.leases.assert_current(lease)
                self.writer.execute(
                    """
                    UPDATE background_job
                    SET status=?, error_code=?, error_message=?,
                        result_json=?, completed_at=?, updated_at=?
                    WHERE job_uid=? AND fencing_token=? AND status='running'
                    """,
                    (
                        terminal_status,
                        type(exc).__name__,
                        sanitize_exception_text(exc),
                        json.dumps(
                            {"traceback": sanitize_exception_text(traceback.format_exc(limit=8), 4000)},
                            ensure_ascii=False,
                        ),
                        utc_iso(),
                        utc_iso(),
                        job_uid,
                        lease.fencing_token,
                    ),
                )
            finally:
                self.events.publish(
                    {
                        "type": "job.blocked_user_action" if blocked else "job.failed",
                        "job_uid": job_uid,
                        "error_code": type(exc).__name__,
                    }
                )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(2)
            self.leases.release(lease_ref[0])


class JobWorker:
    def __init__(
        self,
        service: JobService,
        idle_seconds: float = 0.25,
        on_stop: Callable[[], None] | None = None,
    ):
        self.service = service
        self.idle_seconds = idle_seconds
        self.on_stop = on_stop
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="qcsckp-v1a-browser-worker",
            daemon=True,
        )

    def start(self) -> "JobWorker":
        self.service.recover_expired()
        self._thread.start()
        return self

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        last_recovery = 0.0
        try:
            while not self._stop.is_set():
                claimed = self.service.claim_next()
                if not claimed:
                    if time.monotonic() - last_recovery >= 10:
                        self.service.recover_expired()
                        last_recovery = time.monotonic()
                    self._stop.wait(self.idle_seconds)
                    continue
                self.service.execute_claimed(*claimed)
        finally:
            # Playwright 对象只能由创建它的 Browser Worker 线程释放。
            if self.on_stop:
                self.on_stop()
