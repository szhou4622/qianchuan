"""No real background services, GUI, network or production DB are started."""
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from services import runtime_supervisor as runtime


class Child:
    def __init__(self):
        self.alive = True

    def is_alive(self):
        return self.alive


class OwnThread(Child):
    def __init__(self, *args, **kwargs):
        super().__init__()

    def start(self):
        pass

    def join(self, timeout=None):
        self.alive = False


class RuntimeSupervisorTests(unittest.TestCase):
    def test_failed_starter_rolls_back_itself_and_previous_services_then_allows_retry(self):
        supervisor = runtime.BackgroundRuntimeSupervisor()
        child, calls = Child(), []
        def first_start():
            calls.append("first_start")
            return child
        def first_stop():
            calls.append("first_stop")
            child.alive = False
        def fail():
            calls.append("second_start")
            raise RuntimeError("synthetic halfway failure")
        specs = [runtime.ServiceSpec("one", first_start, first_stop),
                 runtime.ServiceSpec("two", fail, lambda: calls.append("second_stop"))]
        with patch.object(supervisor, "_build_service_specs", return_value=specs):
            with self.assertRaises(RuntimeError):
                supervisor.start(Mock())
        self.assertEqual(["first_start", "second_start", "second_stop", "first_stop"], calls)
        self.assertFalse(supervisor._started)
        self.assertEqual("start_failed", supervisor.health_snapshot()["state"])
        with patch.object(supervisor, "_build_service_specs", return_value=[]), patch.object(runtime.threading, "Thread", OwnThread):
            supervisor.start(Mock())
            self.assertTrue(supervisor._started)
            supervisor.stop()
        self.assertEqual("stopped", supervisor.health_snapshot()["state"])

    def test_failed_rollback_is_observable_and_does_not_start_duplicates(self):
        supervisor = runtime.BackgroundRuntimeSupervisor()
        start = Mock(side_effect=RuntimeError("half started"))
        stop = Mock(side_effect=RuntimeError("still running"))
        with patch.object(supervisor, "_build_service_specs", return_value=[runtime.ServiceSpec("child", start, stop)]):
            with self.assertRaises(RuntimeError):
                supervisor.start(Mock())
            with self.assertRaises(RuntimeError):
                supervisor.start(Mock())
        self.assertEqual(1, start.call_count)
        self.assertEqual("rollback_incomplete", supervisor.health_snapshot()["state"])
        self.assertIn("child", supervisor.health_snapshot()["rollback_pending"])

    def test_alive_worker_is_not_restarted_just_by_polling(self):
        supervisor = runtime.BackgroundRuntimeSupervisor()
        starter, child = Mock(), Child()
        supervisor._active = [runtime.ServiceSpec("rules", starter, Mock())]
        supervisor._handles["rules"] = child
        supervisor._watchdog_once()
        starter.assert_not_called()

    def test_dead_critical_start_result_is_rolled_back(self):
        supervisor = runtime.BackgroundRuntimeSupervisor()
        dead = Child()
        dead.alive = False
        stopper = Mock()
        with patch.object(supervisor, "_build_service_specs", return_value=[runtime.ServiceSpec("dead", Mock(return_value=dead), stopper)]):
            with self.assertRaises(RuntimeError):
                supervisor.start(Mock())
        stopper.assert_called_once()
        self.assertFalse(supervisor._started)

    def test_void_starter_uses_observed_handle_and_dead_flag_is_reset_before_restart(self):
        supervisor = runtime.BackgroundRuntimeSupervisor()
        state = {"thread": Child()}
        calls = []
        def stop():
            calls.append("stop")
            state["thread"].alive = False
        def start():
            calls.append("start")
            state["thread"] = Child()
        spec = runtime.ServiceSpec("daily_report", start, stop, True, lambda: state["thread"])
        supervisor._active = [spec]
        supervisor._handles[spec.name] = state["thread"]
        supervisor._watchdog_once()
        self.assertEqual([], calls)
        state["thread"].alive = False
        supervisor._watchdog_once()
        self.assertEqual(["stop", "start"], calls)
        self.assertTrue(supervisor.health_snapshot()["services"][spec.name]["alive"])

    def test_live_but_stalled_collection_uses_generation_recovery_not_starter(self):
        supervisor = runtime.BackgroundRuntimeSupervisor()
        starter = Mock()
        supervisor._started = True
        supervisor._active = [runtime.ServiceSpec("collection", starter, Mock())]
        supervisor._handles["collection"] = Child()
        state = Mock(return_value={"generation": "g1", "thread_alive": True, "status": "running",
                                   "last_heartbeat_monotonic": 100, "stalled_after_seconds": 330})
        recover = Mock(return_value={"status": "restarted", "generation": "g2"})
        with patch.object(runtime, "QIANCHUAN_BACKEND", "official_api"), patch.object(runtime.time, "monotonic", return_value=1000), patch.dict(
            sys.modules, {"services.official_api_collection": SimpleNamespace(
                get_official_api_collection_watchdog_state=state,
                request_official_api_collection_recovery=recover)}
        ):
            supervisor._watchdog_once()
        recover.assert_called_once_with(expected_generation="g1", reason="heartbeat_stalled")
        starter.assert_not_called()
        self.assertEqual("restarted", supervisor.health_snapshot()["collector"]["recovery"]["status"])

    def test_resource_pressure_stays_observable_and_recovery_is_rate_limited(self):
        supervisor = runtime.BackgroundRuntimeSupervisor()
        supervisor._started = True
        state = Mock(return_value={"generation": "g1", "thread_alive": True, "status": "resource_pressure",
                                   "last_heartbeat_monotonic": 999, "retry_after_seconds": 60})
        recover = Mock(return_value={"status": "deferred", "generation": "g1", "retry_after_seconds": 60})
        with patch.object(runtime, "QIANCHUAN_BACKEND", "official_api"), patch.object(runtime.time, "monotonic", return_value=1000), patch.dict(
            sys.modules, {"services.official_api_collection": SimpleNamespace(
                get_official_api_collection_watchdog_state=state,
                request_official_api_collection_recovery=recover)}
        ):
            supervisor._check_collection_progress()
            supervisor._check_collection_progress()
            self.assertEqual("degraded", supervisor.health_snapshot()["state"])
            self.assertEqual("resource_pressure", supervisor.health_snapshot()["collector"]["status"])
            state.return_value = {"generation": "g1", "thread_alive": True, "status": "running",
                                  "last_heartbeat_monotonic": 1000}
            supervisor._check_collection_progress()
        recover.assert_called_once()
        self.assertEqual("running", supervisor.health_snapshot()["state"])
