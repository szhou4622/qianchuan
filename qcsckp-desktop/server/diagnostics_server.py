#!/usr/bin/env python3
"""Dedicated, bounded QCSCKP diagnostics. Never persists authentication data."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FIELDS = {"event_id", "diagnostic_id", "app_name", "version", "channel", "build_revision",
          "source_commit", "occurred_at", "stage", "error_code", "http_status", "elapsed_ms",
          "request_id", "exception_type", "frames"}
STAGES = {"bootstrap", "package_integrity", "webview2_check", "app_import", "app_main",
          "ready", "stopped", "failed", "license", "update", "runtime", "switch"}
ERRORS = {"dns", "timeout", "certificate_chain", "certificate_time", "certificate_identity",
          "certificate_invalid", "tls", "proxy", "connection_refused", "connection_reset",
          "network", "transport_unavailable", "response_invalid", "invalid_response",
          "business_error", "startup_failure", "runtime_failure", "switch_failure", "http_error"}


def validate_event(value):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("unexpected fields")
    for field in ("event_id", "diagnostic_id"):
        if not re.fullmatch(r"[a-f0-9]{32}", str(value.get(field, ""))):
            raise ValueError("invalid identifier")
    if value.get("app_name") != "QCSCKP" or value.get("channel") not in {"production","development","stable"}:
        raise ValueError("invalid application")
    if value.get("stage") not in STAGES or value.get("error_code") not in ERRORS:
        raise ValueError("invalid category")
    patterns = {"version": r"\d+\.\d+\.\d+", "source_commit": r"[a-f0-9]{7,40}",
                "request_id": r"[a-fA-F0-9-]{16,64}|", "exception_type": r"[A-Za-z_]{1,80}|"}
    for key, pattern in patterns.items():
        if not re.fullmatch(pattern, str(value.get(key, ""))):
            raise ValueError("invalid metadata")
    for key, lo, hi in (("build_revision",1,1000000),("http_status",0,599),("elapsed_ms",0,3600000),
                         ("occurred_at",int(time.time())-8*86400,int(time.time())+86400)):
        if type(value.get(key)) is not int or not lo <= value[key] <= hi:
            raise ValueError("invalid numeric field")
    frames = value.get("frames", [])
    if not isinstance(frames, list) or len(frames)>16:
        raise ValueError("invalid frames")
    for frame in frames:
        if not isinstance(frame,dict) or set(frame)!={"file","line"}:
            raise ValueError("invalid frame")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,75}\.py", str(frame["file"])):
            raise ValueError("invalid code location")
        if type(frame["line"]) is not int or not 1 <= frame["line"] <= 1000000:
            raise ValueError("invalid line")
    return value


def initialize(db):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as c, c:
        c.execute("CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,subject TEXT,"
                  "diagnostic_id TEXT,received_at REAL,payload TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS event_subject_time ON events(subject,received_at)")
        c.execute("CREATE INDEX IF NOT EXISTS event_diagnostic ON events(diagnostic_id,received_at)")
        c.execute("DELETE FROM events WHERE received_at<?", (time.time()-30*86400,))


def persist(db, subject, event, now=None):
    now = time.time() if now is None else now
    with closing(sqlite3.connect(db,timeout=5)) as c, c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM events WHERE received_at<?", (now-30*86400,))
        old=c.execute("SELECT subject FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
        if old:
            return 202 if old[0]==subject else 409
        if c.execute("SELECT COUNT(*) FROM events WHERE subject=? AND received_at>?",(subject,now-60)).fetchone()[0]>=10:
            return 429
        if Path(db).stat().st_size > 200*1024*1024:
            return 507
        c.execute("INSERT INTO events VALUES(?,?,?,?,?)",(event["event_id"],subject,event["diagnostic_id"],now,
                                                          json.dumps(event,ensure_ascii=False)))
    return 202


def authenticate(headers, opener=urlopen):
    auth = headers.get("Authorization", "")
    credential = headers.get("X-Device-Credential", "")
    if not auth.startswith("Bearer ") or not credential or len(auth)>8192 or len(credential)>2048:
        return None
    req=Request("http://127.0.0.1:8791/api/license/device/status?"+urlencode({"app_name":"QCSCKP"}),
        headers={"Authorization":auth,"X-Device-Credential":credential},method="GET")
    with opener(req,timeout=3) as response:
        body=json.loads(response.read(1024*1024))
    data=body.get("data",body)
    if body.get("ok") is False or not isinstance(data,dict) or data.get("app_name")!="QCSCKP":
        return None
    code, machine = str(data.get("code_id", "")), str(data.get("machine_code", ""))
    if not code or not machine:
        return None
    return hashlib.sha256(("QCSCKP|"+code+"|"+machine).encode()).hexdigest()


def handler_for(db):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # Nginx records status/timing; neither body nor credentials are logged.
            pass

        def reply(self,status,value):
            body=json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path=="/health" else 404,{"ok":self.path=="/health","service":"qcsckp-diagnostics"})

        def do_POST(self):
            if self.path!="/api/qcsckp/diagnostics":
                self.reply(404,{"ok":False}); return
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=65536:
                    self.close_connection=True; self.reply(413,{"ok":False}); return
                self.connection.settimeout(5)
                event=validate_event(json.loads(self.rfile.read(size)))
                subject=authenticate(self.headers)
                if not subject:
                    self.reply(401,{"ok":False}); return
                status=persist(db,subject,event)
                self.reply(status,{"ok":status==202,"event_id":event["event_id"]})
            except HTTPError as exc:
                exc.close(); self.reply(401 if exc.code in {401,403} else 503,{"ok":False})
            except (URLError,TimeoutError,ConnectionError):
                self.reply(503,{"ok":False})
            except (ValueError,TypeError,KeyError):
                self.reply(400,{"ok":False})
            except Exception:
                self.reply(503,{"ok":False})
    return Handler


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--db",default="/var/lib/qcsckp-diagnostics/events.sqlite3")
    p.add_argument("--port",type=int,default=8797)
    p.add_argument("--query",action="store_true")
    p.add_argument("--diagnostic-id",default="")
    p.add_argument("--hours",type=int,default=24)
    a=p.parse_args()
    if a.query:
        with closing(sqlite3.connect(Path(a.db).resolve().as_uri()+"?mode=ro",uri=True)) as c:
            rows=c.execute("SELECT payload FROM events WHERE received_at>? AND (?='' OR diagnostic_id=?) "
                           "ORDER BY received_at DESC LIMIT 500",(time.time()-max(1,a.hours)*3600,a.diagnostic_id,a.diagnostic_id))
            for row in rows: print(row[0])
        return
    initialize(a.db)
    def cleanup():
        while True:
            time.sleep(3600)
            try:
                initialize(a.db)
            except sqlite3.Error:
                pass
    threading.Thread(target=cleanup,daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1",a.port),handler_for(a.db)).serve_forever()


if __name__=="__main__": main()
