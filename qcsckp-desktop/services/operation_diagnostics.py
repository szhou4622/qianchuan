"""Bounded local-only evidence. Never sent to a diagnostics server."""
import hashlib
import json
from pathlib import Path
import threading
from datetime import datetime, timedelta
import uuid
from channel_runtime import layout, atomic_json

_LOCK = threading.RLock()
MAX_EVENTS = 2000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
RETENTION_DAYS = 7


def _path():
    return layout().profile / 'diagnostics' / 'operation-evidence.json'


def _bounded(value, depth=0):
    if depth > 10:
        return '<truncated>'
    if isinstance(value, dict):
        return {str(k): _bounded(v, depth+1) for k, v in list(value.items())[:40]
                if not any(s in str(k).lower() for s in ('token','secret','cookie','password','authorization'))}
    if isinstance(value, (list, tuple)):
        return [_bounded(v, depth+1) for v in value[:30]]
    if isinstance(value, set):
        return [_bounded(v, depth+1) for v in sorted(value, key=str)[:30]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:300]


def read_events_with_status(path=None):
    try:
        p = Path(path) if path else _path()
        if not p.is_file():
            return {"events": [], "status": "not_recorded", "error_type": ""}
        if p.stat().st_size > MAX_FILE_BYTES:
            return {"events": [], "status": "read_failed", "error_type": "size_limit"}
        result = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(result, list):
            return {"events": [], "status": "read_failed", "error_type": "invalid_format"}
        return {"events": result[-MAX_EVENTS:], "status": "available", "error_type": ""}
    except (OSError, ValueError) as exc:
        return {"events": [], "status": "read_failed", "error_type": type(exc).__name__}


def read_events(path=None):
    return read_events_with_status(path)["events"]


def _parse_at(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _fit_event(event):
    raw = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) <= MAX_EVENT_BYTES:
        return event
    core = {key: event.get(key) for key in (
        'evidence_id', 'at', 'kind', 'stage', 'reason_code', 'owner_hash',
        'task_uid', 'execution_uid', 'group_uid', 'request_id', 'message_id',
        'target_uid', 'aavid', 'ad_id', 'material_id', 'material_ids',
    ) if key in event}
    core['truncated'] = True
    core['omitted_fields'] = [key for key in event if key not in core]
    return core


def _fit_file(events):
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    kept = [event for event in events if (_parse_at(event.get('at')) or datetime.min) >= cutoff]
    kept = kept[-MAX_EVENTS:]
    while kept and len(json.dumps(kept, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_FILE_BYTES:
        kept.pop(0)
    return kept


def record(kind, **fields):
    try:
        from services.qianchuan_session import current_session_owner
        owner = str(current_session_owner() or '')
        event = {'evidence_id': uuid.uuid4().hex, 'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'kind': str(kind)[:50],
                 'owner_hash': hashlib.sha256(owner.encode()).hexdigest()[:16], **_bounded(fields)}
        event = _fit_event(event)
        with _LOCK:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(p, _fit_file(read_events(p) + [event]))
        return event.get('evidence_id', '')
    except Exception:
        # Diagnostics must never change submission/collection outcomes.
        return ''
