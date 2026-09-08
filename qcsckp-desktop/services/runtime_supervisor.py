"""Transactional lifecycle and observable recovery of background workers."""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Optional

from config import QIANCHUAN_BACKEND
from utils.log import logger

# Archive validation reads this literal through AST without importing runtime
# code. A regression test compares it with the resolved registry below.
RUNTIME_MODULE_MANIFEST = (
    "utils.sqlite_prune_scheduler",
    "services.webhook_push_runtime",
    "services.local_feishu_bridge",
    "services.retargeting_rule_runner",
    "services.retarget_task_worker",
    "services.operation_log_monitor",
    "services.operation_daily_report",
    "services.regulation_rule_runner",
    "services.official_api_reconciliation",
    "services.official_api_catalog",
    "services.official_api_collection",
)


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    start: Callable[[], Any]
    stop: Callable[[], Any]
    watched: bool = True
    observe: Optional[Callable[[], Any]] = None


def _resolve_service_specs(js_api: Any = None, *, backend: Optional[str] = None) -> list[ServiceSpec]:
    """Resolve lazily without starting anything; static imports are packager-visible."""
    from utils.sqlite_prune_scheduler import start_sqlite_prune_background_thread, stop_sqlite_prune_background_thread
    from services.webhook_push_runtime import start_webhook_push_background_threads, stop_webhook_push_background_threads
    from services.local_feishu_bridge import restore_local_feishu_account_from_device_session, deactivate_local_feishu_account
    from services.retargeting_rule_runner import start_retargeting_rule_runner_background_thread, stop_retargeting_rule_runner_background_thread
    from services.retarget_task_worker import start_retarget_task_worker_background_thread, stop_retarget_task_worker_background_thread
    from services.operation_log_monitor import start_platform_log_sync_background_thread, stop_platform_log_sync_background_thread
    from services import operation_daily_report
    from services.operation_daily_report import start_operation_daily_report_background_thread, stop_operation_daily_report_background_thread
    from services.regulation_rule_runner import start_regulation_rule_runner_background_thread, stop_regulation_rule_runner_background_thread
    from services.official_api_reconciliation import start_official_api_reconciliation_background_thread, stop_official_api_reconciliation_background_thread

    specs = [
        ServiceSpec("prune", start_sqlite_prune_background_thread, stop_sqlite_prune_background_thread, False),
        ServiceSpec("webhook", start_webhook_push_background_threads, stop_webhook_push_background_threads, False),
        ServiceSpec("feishu", restore_local_feishu_account_from_device_session, deactivate_local_feishu_account, False),
        ServiceSpec("retarget_rules", start_retargeting_rule_runner_background_thread, stop_retargeting_rule_runner_background_thread),
        ServiceSpec("retarget_tasks", start_retarget_task_worker_background_thread, stop_retarget_task_worker_background_thread),
        ServiceSpec("operation_logs", start_platform_log_sync_background_thread, stop_platform_log_sync_background_thread),
        ServiceSpec("daily_report", start_operation_daily_report_background_thread, stop_operation_daily_report_background_thread,
                    observe=lambda: operation_daily_report.SCHEDULER_THREAD),
        ServiceSpec("stop_rules", start_regulation_rule_runner_background_thread, stop_regulation_rule_runner_background_thread),
        ServiceSpec("reconciliation", start_official_api_reconciliation_background_thread, stop_official_api_reconciliation_background_thread),
    ]
    if (QIANCHUAN_BACKEND if backend is None else backend) == "official_api":
        from services.official_api_catalog import start_official_api_catalog_scheduler, stop_official_api_catalog_scheduler
        from services.official_api_collection import start_official_api_collection_background_thread, stop_official_api_collection_background_thread
        specs.extend([
            ServiceSpec("catalog", start_official_api_catalog_scheduler, stop_official_api_catalog_scheduler),
            ServiceSpec("collection", start_official_api_collection_background_thread, stop_official_api_collection_background_thread),
        ])
    else:
        service = js_api.api.service
        specs.append(ServiceSpec("catalog", service.start_catalog_scheduler, service.stop_catalog_scheduler))
    return specs


def runtime_component_manifest() -> dict[str, Any]:
    """Read-only activation dependency check using the exact runtime registry."""
    specs = _resolve_service_specs(backend="official_api")
    def describe(callback: Callable, role: str) -> dict[str, Any]:
        if not callable(callback):
            raise TypeError(f"后台服务注册回调不可调用：{role}")
        return {"module": callback.__module__, "function": callback.__name__, "callable": True}
    services = [{"name": spec.name, "watched": spec.watched,
                 "start": describe(spec.start, spec.name + ".start"),
                 "stop": describe(spec.stop, spec.name + ".stop"),
                 "observe": describe(spec.observe, spec.name + ".observe") if spec.observe is not None else None}
                for spec in specs]
    return {
        "success": True,
        "backend": "official_api",
        "expected_modules": list(RUNTIME_MODULE_MANIFEST),
        "modules": list(dict.fromkeys(item[role]["module"] for item in services for role in ("start", "stop"))),
        "services": services,
    }


runtime_registry_manifest = runtime_component_manifest


class BackgroundRuntimeSupervisor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._started = False
        self._starting = False
        self._stop = threading.Event()
        self._resume_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._active: list[ServiceSpec] = []
        self._handles: dict[str, Any] = {}
        self._health: dict[str, Any] = {"state": "stopped", "services": {}, "collector": {}}
        self._next_recovery: dict[str, float] = {}
        self._observed_since: dict[str, float] = {}

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._health, "started": self._started, "starting": self._starting,
                    "services": {k: dict(v) for k, v in self._health["services"].items()},
                    "collector": dict(self._health.get("collector") or {})}

    @staticmethod
    def _alive(handle: Any) -> Optional[bool]:
        if isinstance(handle, (tuple, list)):
            states = [BackgroundRuntimeSupervisor._alive(item) for item in handle if item is not None]
            return any(states) if states and all(state is not None for state in states) else None
        method = getattr(handle, "is_alive", None)
        return bool(method()) if callable(method) else None

    def _build_service_specs(self, js_api: Any) -> list[ServiceSpec]:
        return _resolve_service_specs(js_api)

    def _stop_children(self) -> list[str]:
        errors = []
        for spec in list(reversed(self._active)):
            try:
                spec.stop()
                handle = self._handles.get(spec.name)
                if self._alive(handle) is True:
                    raise RuntimeError("停止请求返回后线程仍存活；禁止启动重复实例")
                self._active.remove(spec)
                self._handles.pop(spec.name, None)
                with self._lock:
                    self._health["services"][spec.name] = {"status": "stopped"}
            except Exception as exc:
                errors.append(spec.name)
                logger.warning("[运行主管] 停止%s失败: %s", spec.name, exc)
                with self._lock:
                    self._health["services"][spec.name] = {"status": "stop_incomplete", "error": str(exc)[:400]}
        return errors

    def _join_own_threads(self) -> list[str]:
        remaining = []
        for attr in ("_resume_thread", "_watchdog_thread"):
            thread = getattr(self, attr)
            if thread is not None and thread is not threading.current_thread():
                if self._alive(thread):
                    thread.join(timeout=3.0)
                if self._alive(thread):
                    remaining.append(attr)
                    continue
            setattr(self, attr, None)
        return remaining

    def start(self, js_api: Any) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._started:
                    return
                if self._active:
                    raise RuntimeError("上轮后台服务尚未完全停止，不能重复启动")
            if self._join_own_threads():
                raise RuntimeError("上轮恢复/主管线程尚未停止，不能重复启动")
            with self._lock:
                self._starting = True
                self._stop = threading.Event()
                stop_event = self._stop
                self._next_recovery.clear()
                self._observed_since.clear()
                self._health = {"state": "starting", "services": {}, "collector": {}}
            try:
                for spec in self._build_service_specs(js_api):
                    if stop_event.is_set():
                        raise RuntimeError("后台服务启动已取消")
                    # Include a failing starter: it may have spawned a child
                    # before throwing, so its stopper must also be attempted.
                    self._active.append(spec)
                    handle = spec.start()
                    if spec.observe is not None:
                        handle = spec.observe()
                    self._handles[spec.name] = handle
                    if spec.watched and self._alive(handle) is False:
                        raise RuntimeError(f"关键服务 {spec.name} 启动后未存活")
                    with self._lock:
                        self._health["services"][spec.name] = {"status": "running", "alive": self._alive(handle)}
                self._resume_thread = threading.Thread(
                    target=self._resume_saved_monitoring, args=(js_api, stop_event),
                    name="qianchuan-monitor-resume", daemon=True,
                )
                self._resume_thread.start()
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog, args=(stop_event,),
                    name="qcsckp-runtime-watchdog", daemon=True,
                )
                self._watchdog_thread.start()
                if stop_event.is_set():
                    raise RuntimeError("后台服务启动已取消")
                with self._lock:
                    self._started = True
                    self._starting = False
                    self._health["state"] = "running"
                logger.info("[运行主管] 后台服务已统一启动")
            except Exception as exc:
                stop_event.set()
                errors = self._stop_children()
                errors.extend(self._join_own_threads())
                with self._lock:
                    self._started = self._starting = False
                    self._health.update(state="rollback_incomplete" if errors else "start_failed",
                                        last_error=str(exc)[:400], rollback_pending=errors)
                logger.exception("[运行主管] 启动失败，已回滚本轮后台服务")
                raise

    def _resume_saved_monitoring(self, js_api: Any, stop_event: threading.Event) -> None:
        if stop_event.wait(1.0):
            return
        for attempt in range(1, 4):
            if stop_event.is_set():
                return
            try:
                result = js_api.api.service.start_from_saved_session()
                phase = str(result.get("phase") or "")
                with self._lock:
                    self._health["resume"] = {"phase": phase, "message": str(result.get("message") or "")[:400],
                                              "attempt": attempt}
                if phase == "resource_pressure":
                    self._check_collection_progress(force_reason="resource_pressure")
                elif phase != "tool_login_required":
                    return
            except Exception as exc:
                with self._lock:
                    self._health["resume"] = {"phase": "failed", "error": str(exc)[:400], "attempt": attempt}
                logger.warning("[运行主管] 自动恢复监控失败: %s", exc)
            if stop_event.wait(2.0):
                return

    def _check_collection_progress(self, *, force_reason: str = "") -> None:
        if QIANCHUAN_BACKEND != "official_api" or self._stop.is_set():
            return
        try:
            from services.official_api_collection import (
                get_official_api_collection_watchdog_state,
                request_official_api_collection_recovery,
            )
            state = dict(get_official_api_collection_watchdog_state())
            now = time.monotonic()
            heartbeat = float(state.get("last_heartbeat_monotonic") or 0)
            first_seen = self._observed_since.setdefault("collection", now)
            age = max(0.0, now - (heartbeat or first_seen))
            status = str(state.get("status") or "unknown")
            limit = max(30.0, float(state.get("stalled_after_seconds") or 330))
            reason = force_reason or (
                "thread_dead" if not state.get("thread_alive")
                else "resource_pressure" if status == "resource_pressure"
                else "heartbeat_stalled" if age is not None and age > limit else ""
            )
            with self._lock:
                self._health["collector"] = {**state, "heartbeat_age_seconds": age, "watchdog_reason": reason}
                if not reason and self._started:
                    self._health["state"] = "running"
            if not reason or now < self._next_recovery.get("collection", 0):
                return
            generation = str(state.get("generation") or "")
            if not generation:
                outcome = {"status": "unsafe", "message": "采集代次不可观察，未启动重复线程"}
            else:
                # Only the collector can fence the old generation and decide
                # whether recovery is safe. Never call its starter here.
                with self._lifecycle_lock:
                    if self._stop.is_set():
                        return
                    outcome = dict(request_official_api_collection_recovery(
                        expected_generation=generation, reason=reason,
                    ))
            self._next_recovery["collection"] = now + max(30.0, float(outcome.get("retry_after_seconds") or state.get("retry_after_seconds") or 30))
            with self._lock:
                self._health["collector"]["recovery"] = outcome
                self._health["state"] = "running" if outcome.get("status") == "restarted" else "degraded"
        except Exception as exc:
            with self._lock:
                self._health["collector"] = {"status": "health_check_failed", "error": str(exc)[:400]}
                self._health["state"] = "degraded"
            logger.warning("[运行主管] 采集进度检查失败，未盲目重开: %s", exc)

    def _watchdog_once(self) -> None:
        if self._stop.is_set():
            return
        for spec in list(self._active):
            if not spec.watched or self._stop.is_set():
                continue
            if spec.name == "collection":
                self._check_collection_progress()
                continue
            if spec.observe is not None:
                self._handles[spec.name] = spec.observe()
            alive = self._alive(self._handles.get(spec.name))
            with self._lock:
                self._health["services"][spec.name] = {"status": "running" if alive else "unobservable" if alive is None else "stopped", "alive": alive}
            if alive is not False or time.monotonic() < self._next_recovery.get(spec.name, 0):
                continue
            self._next_recovery[spec.name] = time.monotonic() + 60
            try:
                with self._lifecycle_lock:
                    if self._stop.is_set():
                        return
                    spec.stop()
                    self._handles[spec.name] = spec.start()
                    if spec.observe is not None:
                        self._handles[spec.name] = spec.observe()
                with self._lock:
                    self._health["services"][spec.name] = {"status": "restart_requested", "alive": self._alive(self._handles[spec.name])}
            except Exception as exc:
                with self._lock:
                    self._health["services"][spec.name] = {"status": "restart_failed", "error": str(exc)[:400]}
                logger.warning("[运行主管] %s恢复失败: %s", spec.name, exc)

    def _watchdog(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(30.0):
            self._watchdog_once()

    def stop(self) -> None:
        self._stop.set()
        with self._lifecycle_lock:
            errors = self._stop_children()
        # A watchdog can be waiting for the lifecycle lock. Release it before
        # joining so shutdown does not manufacture an unresponsive own thread.
        errors.extend(self._join_own_threads())
        with self._lock:
            self._started = self._starting = False
            self._health.update(state="stop_incomplete" if errors else "stopped", rollback_pending=errors)
        logger.info("[运行主管] 后台服务停止状态: %s", self._health["state"])


RUNTIME_SUPERVISOR = BackgroundRuntimeSupervisor()
