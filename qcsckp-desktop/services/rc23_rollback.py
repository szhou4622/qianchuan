# -*- coding: utf-8 -*-
"""rc23 升级前快照；必须在新版 SQLite 建表迁移之前调用。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from typing import Any, Dict

from config import CURRENT_VERSION, DATA_DIR, DB_FILE
from utils.log import logger


SNAPSHOT_MARKER = os.path.join(DATA_DIR, "rollback", "rc23_snapshot.json")
ROLLBACK_FILES = (
    "qcookie.json",
    "control_panel.json",
    "feishu_local_profiles.json",
    "operation_daily_report.json",
    "rule_retargeting.json",
    "rule_regulation.json",
)


def _sqlite_online_backup(source: str, destination: str) -> None:
    source_conn = sqlite3.connect(
        f"file:{os.path.abspath(source)}?mode=ro",
        uri=True,
        timeout=30,
    )
    destination_conn = sqlite3.connect(destination, timeout=30)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def ensure_rc23_upgrade_snapshot() -> Dict[str, Any]:
    """每个数据目录只创建一次升级快照，失败时不阻止旧版启动。"""
    if not os.path.isfile(DB_FILE):
        return {
            "success": True,
            "created": False,
            "message": "新安装无需创建rc23升级快照",
        }
    try:
        with open(SNAPSHOT_MARKER, "r", encoding="utf-8") as handle:
            existing = json.load(handle)
        if isinstance(existing, dict) and existing.get("snapshot_dir"):
            return {
                "success": True,
                "created": False,
                "snapshot_dir": str(existing["snapshot_dir"]),
                "message": "rc23升级快照已存在",
            }
    except Exception:
        pass

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rollback_root = os.path.join(DATA_DIR, "rollback")
    snapshot_dir = os.path.join(
        rollback_root,
        f"测试1版-rc23-auto-{stamp}",
    )
    os.makedirs(snapshot_dir, exist_ok=False)
    try:
        _sqlite_online_backup(
            DB_FILE,
            os.path.join(snapshot_dir, "qianchuan.db"),
        )
        copied = ["qianchuan.db"]
        for name in ROLLBACK_FILES:
            source = os.path.join(DATA_DIR, name)
            if not os.path.isfile(source):
                continue
            shutil.copy2(source, os.path.join(snapshot_dir, name))
            copied.append(name)
        manifest = {
            "format": "qcsckp-rc23-rollback-v1",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_version": "测试1版-rc23",
            "upgrade_target_version": CURRENT_VERSION,
            "snapshot_dir": snapshot_dir,
            "files": copied,
        }
        with open(
            os.path.join(snapshot_dir, "manifest.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        os.makedirs(rollback_root, exist_ok=True)
        temp = SNAPSHOT_MARKER + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
        os.replace(temp, SNAPSHOT_MARKER)
        logger.info("[回滚保护] 已创建rc23升级快照: %s", snapshot_dir)
        return {
            "success": True,
            "created": True,
            "snapshot_dir": snapshot_dir,
            "message": "rc23升级快照已创建",
        }
    except Exception:
        try:
            shutil.rmtree(snapshot_dir)
        except Exception:
            pass
        raise
