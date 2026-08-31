"""Allowlisted diagnostic events; never uploads raw logs or request bodies."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

from channel_runtime import atomic_json, layout, read_json
from release_identity import IDENTITY

ENDPOINT = "https://update.dadaozixun.com/api/qcsckp/diagnostics"
STAGES = {"bootstrap", "package_integrity", "webview2_check", "app_import", "app_main",
          "ready", "stopped", "failed", "license", "update", "runtime", "switch"}
ERRORS = {"dns", "timeout", "certificate_chain", "certificate_time", "certificate_identity",
          "certificate_invalid", "tls", "proxy", "connection_refused", "connection_reset",
          "network", "transport_unavailable", "response_invalid", "invalid_response",
          "business_error", "startup_failure", "runtime_failure", "switch_failure", "http_error"}
_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD = None


@contextmanager
def _db():
    folder = layout().profile / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "events.sqlite3", timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY,created REAL,"
                 "fingerprint TEXT,payload TEXT,sent INTEGER DEFAULT 0)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def diagnostic_id():
    path = layout().shared / "diagnostic-id.json"
    value = read_json(path).get("diagnostic_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", str(value)):
        value = uuid.uuid4().hex
        atomic_json(path, {"diagnostic_id": value})
    return value


def diagnostic_status():
    consent = read_json(layout().profile / "diagnostic-consent.json")
    return {"diagnostic_id": diagnostic_id(), "enabled": consent.get("enabled", False),
            "asked": consent.get("asked", False), "retention_days": 30}


def set_consent(enabled):
    atomic_json(layout().profile / "diagnostic-consent.json",
                {"enabled": bool(enabled), "asked": True, "enabled_at": time.time()})
    return diagnostic_status()


def record_event(stage, error_code, *, exception=None, request_id="", http_status=0, elapsed_ms=0):
    """Best effort. Only structured categories and code locations leave the PC."""
    try:
        event = {"event_id": uuid.uuid4().hex, "diagnostic_id": diagnostic_id(),
                 "app_name": "QCSCKP", "version": IDENTITY["version"], "channel": IDENTITY["channel"],
                 "build_revision": IDENTITY["build_revision"],
                 "source_commit": str(IDENTITY.get("source_commit", ""))[:40],
                 "occurred_at": int(time.time()), "stage": stage if stage in STAGES else "runtime",
                 "error_code": error_code if error_code in ERRORS else "runtime_failure",
                 "http_status": max(0, min(599, int(http_status or 0))),
                 "elapsed_ms": max(0, min(3600000, int(elapsed_ms or 0))),
                 "request_id": request_id if re.fullmatch(r"[a-fA-F0-9-]{16,64}", str(request_id)) else ""}
        if exception is not None:
            name = type(exception).__name__
            event["exception_type"] = name if re.fullmatch(r"[A-Za-z_]{1,80}", name) else "Exception"
            frames = traceback.extract_tb(exception.__traceback__)[-16:]
            event["frames"] = [{"file": Path(f.filename).name, "line": int(f.lineno)} for f in frames
                               if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,75}\.py", Path(f.filename).name)]
        fingerprint = hashlib.sha256(json.dumps({k:v for k,v in event.items()
            if k not in {"event_id", "occurred_at", "request_id", "elapsed_ms"}}, sort_keys=True).encode()).hexdigest()
        with _LOCK, _db() as conn:
            now = time.time()
            if conn.execute("SELECT 1 FROM events WHERE fingerprint=? AND created>?", (fingerprint, now-60)).fetchone():
                return
            conn.execute("DELETE FROM events WHERE created<?", (now-7*86400,))
            conn.execute("INSERT INTO events(event_id,created,fingerprint,payload) VALUES(?,?,?,?)",
                         (event["event_id"], now, fingerprint, json.dumps(event, ensure_ascii=False)))
            conn.execute("DELETE FROM events WHERE event_id NOT IN (SELECT event_id FROM events ORDER BY created DESC LIMIT 1000)")
            # Each event is <4KB; the retained logical queue is bounded below 10MB.
    except Exception:
        pass


def upload_once(opener=urlopen):
    settings = read_json(layout().profile / "diagnostic-consent.json")
    if not settings.get("enabled"):
        return 0
    from services.license_storage import LicenseSecureStore
    store = LicenseSecureStore()
    credentials = store.load_credentials()
    if not credentials:
        return 0
    with _LOCK, _db() as conn:
        rows = conn.execute("SELECT event_id,payload FROM events WHERE sent=0 AND created>=? "
                            "AND created>? ORDER BY created LIMIT 10",
                            (settings.get("enabled_at", time.time()), time.time()-7*86400)).fetchall()
    sent = 0
    for event_id, payload in rows:
        request = Request(ENDPOINT, data=payload.encode("utf-8"), method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + credentials["device_session"],
            "X-Device-Credential": credentials["device_credential"], "X-Client-Request-ID": event_id})
        try:
            with opener(request, timeout=5) as response:
                if response.status != 202:
                    break
            with _LOCK, _db() as conn:
                conn.execute("UPDATE events SET sent=1 WHERE event_id=?", (event_id,))
            sent += 1
        except Exception:
            break
    return sent


def start_worker():
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP.clear()
    def run():
        while not _STOP.wait(60):
            try:
                upload_once()
            except Exception:
                pass
    _THREAD = threading.Thread(target=run, name="qcsckp-safe-diagnostics", daemon=True)
    _THREAD.start()
    logger = logging.getLogger("QianChuanPMCServices")
    if not any(isinstance(h, SafeErrorHandler) for h in logger.handlers):
        logger.addHandler(SafeErrorHandler())


class SafeErrorHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)

    def emit(self, record):
        # Never interpolate record.msg/args: those may contain business data.
        exc = record.exc_info[1] if record.exc_info else None
        record_event("runtime", "runtime_failure", exception=exc)


def stop_worker():
    _STOP.set()


def export_events():
    with _LOCK, _db() as conn:
        events = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM events ORDER BY created")]
    path = layout().profile / "diagnostics" / ("diagnostics-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    atomic_json(path, {"diagnostic_id": diagnostic_id(), "events": events})
    return str(path)
