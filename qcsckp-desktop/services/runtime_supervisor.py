# -*- coding: utf-8 -*-
"""Own the lifecycle of production background workers.

Window hiding does not call ``stop``.  A tray "complete exit" or a normal
WebView shutdown does, so no worker can keep collecting or sending daily
reports after the desktop process has been intentionally closed.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from config import QIANCHUAN_BACKEND
from utils.log import logger


class BackgroundRuntimeSupervisor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started = False
        self._stop = threading.Event()
        self._resume_thread: Optional[threading.Thread] = None

    def start(self, js_api: Any) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()

        from services.local_feishu_bridge import (
            restore_local_feishu_account_from_device_session,
        )
        from services.operation_daily_report import (
            start_operation_daily_report_background_thread,
        )
        from services.operation_log_monitor import (
            start_platform_log_sync_background_thread,
        )
        from services.official_api_reconciliation import (
            start_official_api_reconciliation_background_thread,
        )
        from services.regulation_rule_runner import (
            start_regulation_rule_runner_background_thread,
        )
        from services.retarget_task_worker import (
            start_retarget_task_worker_background_thread,
        )
        from services.retargeting_rule_runner import (
            start_retargeting_rule_runner_background_thread,
        )
        from services.webhook_push_runtime import start_webhook_push_background_threads
        from utils.sqlite_prune_scheduler import start_sqlite_prune_background_thread

        start_sqlite_prune_background_thread()
        start_webhook_push_background_threads()
        try:
            restore_local_feishu_account_from_device_session()
        except Exception as exc:
            logger.warning("[运行主管] 飞书连接恢复失败: %s", exc)
        start_retargeting_rule_runner_background_thread()
        start_retarget_task_worker_background_thread()
        start_platform_log_sync_background_thread()
        start_operation_daily_report_background_thread()
        start_regulation_rule_runner_background_thread()
        start_official_api_reconciliation_background_thread()

        if QIANCHUAN_BACKEND == "official_api":
            from services.official_api_catalog import start_official_api_catalog_scheduler
            from services.official_api_collection import (
                start_official_api_collection_background_thread,
            )

            start_official_api_catalog_scheduler()
            start_official_api_collection_background_thread()
        else:
            js_api.api.service.start_catalog_scheduler()

        def _resume_saved_monitoring() -> None:
            if self._stop.wait(1.0):
                return
            for attempt in range(1, 4):
                if self._stop.is_set():
                    return
                try:
                    result = js_api.api.service.start_from_saved_session()
                    logger.info("[MONITOR] %s", result.get("message") or result.get("phase"))
                    if result.get("phase") != "tool_login_required":
                        return
                except Exception as exc:
                    logger.warning("[MONITOR] 自动恢复后台监控失败（第%s次）: %s", attempt, exc)
                self._stop.wait(2.0)

        self._resume_thread = threading.Thread(
            target=_resume_saved_monitoring,
            name="qianchuan-monitor-resume",
            daemon=True,
        )
        self._resume_thread.start()
        logger.info("[运行主管] 后台服务已统一启动")

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop.set()

        # Stop intake first, then workers that may persist final state, and
        # finally the local Feishu transport.
        stoppers = []
        try:
            from services.official_api_catalog import stop_official_api_catalog_scheduler
            stoppers.append(stop_official_api_catalog_scheduler)
        except Exception:
            pass
        try:
            from services.official_api_collection import stop_official_api_collection_background_thread
            stoppers.append(stop_official_api_collection_background_thread)
        except Exception:
            pass
        try:
            from services.retargeting_rule_runner import stop_retargeting_rule_runner_background_thread
            stoppers.append(stop_retargeting_rule_runner_background_thread)
        except Exception:
            pass
        try:
            from services.regulation_rule_runner import stop_regulation_rule_runner_background_thread
            stoppers.append(stop_regulation_rule_runner_background_thread)
        except Exception:
            pass
        try:
            from services.operation_log_monitor import stop_platform_log_sync_background_thread
            stoppers.append(stop_platform_log_sync_background_thread)
        except Exception:
            pass
        try:
            from services.operation_daily_report import stop_operation_daily_report_background_thread
            stoppers.append(stop_operation_daily_report_background_thread)
        except Exception:
            pass
        try:
            from services.retarget_task_worker import stop_retarget_task_worker_background_thread
            stoppers.append(stop_retarget_task_worker_background_thread)
        except Exception:
            pass
        try:
            from services.official_api_reconciliation import stop_official_api_reconciliation_background_thread
            stoppers.append(stop_official_api_reconciliation_background_thread)
        except Exception:
            pass
        try:
            from services.webhook_push_runtime import stop_webhook_push_background_threads
            stoppers.append(stop_webhook_push_background_threads)
        except Exception:
            pass
        try:
            from utils.sqlite_prune_scheduler import stop_sqlite_prune_background_thread
            stoppers.append(stop_sqlite_prune_background_thread)
        except Exception:
            pass

        for stopper in stoppers:
            try:
                stopper()
            except Exception as exc:
                logger.warning("[运行主管] 停止后台服务失败 %s: %s", stopper.__name__, exc)

        try:
            from services.local_feishu_bridge import deactivate_local_feishu_account
            deactivate_local_feishu_account()
        except Exception as exc:
            logger.warning("[运行主管] 停止飞书连接失败: %s", exc)

        thread = self._resume_thread
        self._resume_thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        logger.info("[运行主管] 后台服务已停止")


RUNTIME_SUPERVISOR = BackgroundRuntimeSupervisor()
