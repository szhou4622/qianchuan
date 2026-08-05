"""V1A 后台服务启动、元数据发布和已有实例唤醒。"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .api_server import V1AHttpServer
from .runtime import RuntimeContext
from .runtime_paths import RuntimePaths


@dataclass
class RunningService:
    runtime: RuntimeContext
    server: V1AHttpServer
    thread: threading.Thread
    launch_token: str
    wake_token: str
    base_url: str

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.runtime.close()
        try:
            self.runtime.paths.service_state.unlink(missing_ok=True)
        except OSError:
            pass


def start_service(
    *,
    paths: RuntimePaths | None = None,
    frontend_dist: Path | None = None,
) -> RunningService:
    resolved_paths = (paths or RuntimePaths.default()).ensure()
    apply_pending_restore(resolved_paths)
    runtime = RuntimeContext(resolved_paths)
    launch_token = secrets.token_urlsafe(32)
    wake_token = secrets.token_urlsafe(32)
    dist = frontend_dist or (
        Path(__file__).resolve().parents[1] / "production_v1a_frontend" / "dist"
    )
    server = V1AHttpServer(runtime, launch_token, wake_token, dist)
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="qcsckp-v1a-local-api",
    )
    thread.start()
    metadata = {
        "pid": os.getpid(),
        "base_url": base_url,
        "wake_token": wake_token,
        "started_at_epoch": time.time(),
    }
    runtime.paths.service_state.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        os.chmod(runtime.paths.service_state, 0o600)
    except OSError:
        pass
    return RunningService(runtime, server, thread, launch_token, wake_token, base_url)


def apply_pending_restore(paths: RuntimePaths) -> bool:
    request_path = paths.root / "restore-request.json"
    if not request_path.is_file():
        return False
    request = json.loads(request_path.read_text(encoding="utf-8"))
    snapshot = Path(str(request.get("snapshot") or "")).resolve()
    snapshots_root = paths.snapshots_dir.resolve()
    if snapshots_root not in snapshot.parents or not snapshot.is_file():
        raise RuntimeError("恢复请求中的快照路径无效")
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    if digest != str(request.get("snapshot_sha256") or ""):
        raise RuntimeError("恢复快照校验失败")
    for suffix in ("-wal", "-shm"):
        Path(str(paths.runtime_db) + suffix).unlink(missing_ok=True)
    shutil.copy2(snapshot, paths.runtime_db)
    request_path.rename(paths.root / "restore-request.applied.json")
    return True


def wake_existing(paths: RuntimePaths | None = None, *, retries: int = 20) -> bool:
    metadata_path = (paths or RuntimePaths.default()).ensure().service_state
    for _ in range(retries):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            body = json.dumps({"wake_token": metadata["wake_token"]}).encode("utf-8")
            request = urllib.request.Request(
                str(metadata["base_url"]).rstrip("/") + "/api/v1/runtime/wake",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status == 200
        except Exception:
            time.sleep(0.25)
    return False
