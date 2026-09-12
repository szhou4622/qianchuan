"""Bounded local-only evidence. Never sent to a diagnostics server."""
import hashlib
import json
from pathlib import Path
import threading
from datetime import datetime
from channel_runtime import layout, atomic_json

_LOCK = threading.RLock()
MAX_EVENTS = 500


def _path():
    return layout().profile / 'diagnostics' / 'operation-evidence.json'


def _bounded(value, depth=0):
    if depth > 6:
        return '<truncated>'
    if isinstance(value, dict):
        return {str(k): _bounded(v, depth+1) for k, v in list(value.items())[:40]
                if not any(s in str(k).lower() for s in ('token','secret','cookie','password','authorization'))}
    if isinstance(value, (list, tuple)):
        return [_bounded(v, depth+1) for v in value[:30]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:300]


def read_events(path=None):
    try:
        p = Path(path) if path else _path()
        if p.stat().st_size > 4 * 1024 * 1024:
            return []
        result = json.loads(p.read_text(encoding='utf-8'))
        return result[-MAX_EVENTS:] if isinstance(result, list) else []
    except (OSError, ValueError):
        return []


def record(kind, **fields):
    try:
        from services.qianchuan_session import current_session_owner
        owner = str(current_session_owner() or '')
        event = {'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'kind': str(kind)[:50],
                 'owner_hash': hashlib.sha256(owner.encode()).hexdigest()[:16], **_bounded(fields)}
        encoded = json.dumps(event, ensure_ascii=False)
        if len(encoded.encode('utf-8')) > 6000:
            event = {k: v for k, v in event.items() if k != 'evaluation'}
            event['truncated'] = True
        with _LOCK:
            p = _path()
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(p, (read_events(p) + [event])[-MAX_EVENTS:])
    except Exception:
        # Diagnostics must never change submission/collection outcomes.
        pass
