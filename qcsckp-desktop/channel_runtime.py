"""Channel profiles and shared identity. No business service imports at boot."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path

from release_identity import CHANNEL, CHANNELS

AUTH_FILES = ("license_credentials.dpapi", "license_machine_code.dpapi",
              "license_device_code.dpapi", "license_metadata.json")
SKIP_DIRS = {"logs", "temp", "storage", "cache", "startup-state", "diagnostics", "__pycache__"}


@dataclass(frozen=True)
class Layout:
    home: Path
    profile: Path
    data: Path
    shared: Path
    legacy: Path


def layout(channel=None, home=None):
    channel = channel or CHANNEL
    if channel not in CHANNELS:
        raise ValueError("unknown channel")
    base = Path(home or os.getenv("QCSCKP_HOME") or
                Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData/Local") / "QCSCKP").resolve()
    profile = base / "channels" / channel
    override = os.getenv("QCSCKP_DATA_DIR", "").strip()
    data = Path(override).expanduser().resolve() if override else profile / "data"
    return Layout(base, profile, data, base / "shared-v1", base / "official-api-v1" / "data")


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def atomic_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def profile_files(source):
    source = Path(source)
    if not source.exists():
        return []
    result = []
    for p in source.rglob("*"):
        rel = p.relative_to(source)
        if p.is_symlink() or getattr(p, "is_junction", lambda: False)():
            raise RuntimeError("数据目录包含链接，无法安全复制")
        if not p.is_file() or any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.name.endswith(("-wal", "-shm", ".lock")) or p.name == "command.json":
            continue
        result.append((p, rel))
    return result


def snapshot(source, destination, *, include_identity=False):
    """Never copy WAL files; stage all data before publishing the directory."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError("目标副本已经存在，不会覆盖")
    files = profile_files(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    needed = sum(p.stat().st_size for p, _ in files)
    if shutil.disk_usage(destination.parent).free < needed * 2 + 64 * 1024 * 1024:
        raise RuntimeError("剩余磁盘空间不足，原数据未改动")
    stage = Path(tempfile.mkdtemp(prefix=".profile-copy-", dir=destination.parent))
    try:
        for p, rel in files:
            if not include_identity and p.name in AUTH_FILES:
                continue
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                with closing(sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)) as src:
                    with closing(sqlite3.connect(target)) as dst:
                        src.backup(dst)
                        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise RuntimeError("数据库副本完整性检查失败")
            else:
                shutil.copy2(p, target)
        os.rename(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def pause_profile(data):
    for name in ("rule_retargeting.json", "rule_regulation.json"):
        for p in data.rglob(name):
            value = read_json(p)
            value["enabled"] = False
            atomic_json(p, value)
    settings = data / "qianchuan_runtime_settings.json"
    if settings.exists():
        value = read_json(settings)
        value["allow_live_api_writes"] = False
        for profile in value.get("profiles", {}).values():
            if isinstance(profile, dict):
                profile["allow_live_api_writes"] = False
        atomic_json(settings, value)
    # Cancel confirmations, never replay an instruction from a stale copy.
    db = data / "qianchuan.db"
    if db.exists():
        with closing(sqlite3.connect(db)) as c, c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "local_retarget_task" in tables:
                c.execute("UPDATE local_retarget_task SET status='cancelled',active_dedupe_key=NULL "
                          "WHERE status IN ('pending','approved_queued','claimed','executing')")
            for table in ("feishu_inbox", "feishu_outbox"):
                if table in tables:
                    c.execute(f"UPDATE {table} SET status='cancelled' WHERE status IN ('pending','received')")


def prepare_profile(confirm_copy, channel=CHANNEL, home=None):
    paths = layout(channel, home)
    paths.shared.mkdir(parents=True, exist_ok=True)
    state_path = paths.shared / "active-channel.json"
    previous = read_json(state_path)
    prior = previous.get("channel")
    source = (paths.home / "channels" / prior / "data") if prior in CHANNELS else paths.legacy
    source_exists = source.exists() and bool(profile_files(source))
    ready = paths.profile / "profile-ready.json"
    first = not ready.exists()
    switched = prior != channel
    # Identity has one authoritative store. Do not rotate on channel changes.
    for name in AUTH_FILES:
        target = paths.shared / "identity" / name
        candidate = source / name
        if not target.exists() and candidate.is_file():
            atomic_bytes(target, candidate.read_bytes())
    if switched and source_exists:
        backup = paths.home / "channel-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        snapshot(source, backup / "source", include_identity=True)
        if paths.data.exists() and paths.data != source and profile_files(paths.data):
            snapshot(paths.data, backup / "target", include_identity=True)
    if first:
        # A diagnostics-only directory does not constitute an existing profile.
        if not paths.data.exists() and source_exists and confirm_copy(source, channel):
            snapshot(source, paths.data)
        paths.data.mkdir(parents=True, exist_ok=True)
    if switched or first:
        pause_profile(paths.data)
        atomic_json(paths.profile / "switch-state.json", {
            "pending": True, "from_channel": prior or "legacy", "channel": channel,
            "changed_at": time.time(), "reason": "切版后请核验平台任务，再恢复自动投放"})
    atomic_json(ready, {"channel": channel, "initialized_at": time.time()})
    atomic_json(state_path, {"channel": channel, "updated_at": time.time()})
    return paths


def switch_state():
    return read_json(layout().profile / "switch-state.json")


def require_writes_resumed():
    if switch_state().get("pending"):
        raise RuntimeError("切版保护中：请先在版本与诊断页核验平台状态，再恢复自动投放")


class InstanceLease:
    """Machine-wide Windows mutex plus the legacy data-directory lock."""
    def __init__(self, home=None):
        self.paths = layout(home=home)
        self.handles = []
        self.mutex = None

    def acquire(self):
        if os.name == "nt":
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            k.CreateMutexW.restype = wintypes.HANDLE
            # Tests can isolate the mutex by setting a separate QCSCKP_HOME.
            import hashlib
            suffix = hashlib.sha256(str(self.paths.home).casefold().encode()).hexdigest()[:16]
            name = "Global\\QCSCKP-Desktop-Channels" + ("-" + suffix if os.getenv("QCSCKP_HOME") else "")
            self.mutex = k.CreateMutexW(None, False, name)
            if not self.mutex or ctypes.get_last_error() == 183:
                self.close()
                return False
        try:
            for directory in (self.paths.shared, self.paths.legacy):
                if directory == self.paths.legacy and not directory.exists():
                    continue
                directory.mkdir(parents=True, exist_ok=True)
                f = (directory / "qcsckp.instance.lock").open("a+b")
                if f.tell() == 0:
                    f.write(b"1"); f.flush()
                f.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except Exception:
                    f.close()
                    raise
                self.handles.append(f)
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        for f in self.handles:
            f.close()
        self.handles.clear()
        if self.mutex:
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.CloseHandle(self.mutex)
            self.mutex = None
