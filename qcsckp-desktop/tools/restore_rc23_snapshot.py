#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关闭千川工具后，将本地数据恢复到指定的rc23升级快照。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DESKTOP_ROOT))

from config import DATA_DIR, DB_FILE  # noqa: E402


def _latest_snapshot() -> Path:
    marker = Path(DATA_DIR) / "rollback" / "rc23_snapshot.json"
    if marker.is_file():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            candidate = Path(str(value.get("snapshot_dir") or ""))
            if candidate.is_dir():
                return candidate
        except Exception:
            pass
    candidates = sorted(
        (Path(DATA_DIR) / "rollback").glob("测试1版-rc23-*"),
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("没有找到rc23回滚快照")
    return candidates[0]


def _tool_is_running() -> bool:
    try:
        import psutil
    except Exception:
        return False
    current_pid = os.getpid()
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            if int(process.info["pid"]) == current_pid:
                continue
            command = " ".join(process.info.get("cmdline") or []).lower()
            if "qcsckp-desktop" in command and "gui_app.py" in command:
                return True
        except Exception:
            continue
    return False


def restore(snapshot: Path, *, force: bool = False) -> None:
    snapshot = snapshot.resolve()
    rollback_root = (Path(DATA_DIR) / "rollback").resolve()
    if rollback_root not in snapshot.parents or not snapshot.is_dir():
        raise RuntimeError("快照必须位于当前数据目录的rollback文件夹内")
    database = snapshot / "qianchuan.db"
    if not database.is_file():
        raise RuntimeError("快照缺少qianchuan.db")
    if _tool_is_running() and not force:
        raise RuntimeError("千川工具仍在运行，请先从托盘退出后再执行回滚")

    data_dir = Path(DATA_DIR).resolve()
    temp_db = Path(str(DB_FILE) + ".rc23-restore.tmp")
    shutil.copy2(database, temp_db)
    os.replace(temp_db, Path(DB_FILE))
    for suffix in ("-wal", "-shm"):
        path = Path(str(DB_FILE) + suffix)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for source in snapshot.iterdir():
        if source.name in {"qianchuan.db", "manifest.json"} or not source.is_file():
            continue
        destination = data_dir / source.name
        temp = data_dir / f".{source.name}.rc23-restore.tmp"
        shutil.copy2(source, temp)
        os.replace(temp, destination)
    print(f"已恢复rc23数据快照：{snapshot}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="关闭工具后恢复测试1版-rc23本地数据快照",
    )
    parser.add_argument(
        "snapshot",
        nargs="?",
        help="快照目录；不填则使用自动记录的最新rc23快照",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="跳过运行进程检查（仅在确认工具已完全退出时使用）",
    )
    args = parser.parse_args()
    try:
        restore(
            Path(args.snapshot) if args.snapshot else _latest_snapshot(),
            force=args.force,
        )
        return 0
    except Exception as exc:
        print(f"回滚失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
