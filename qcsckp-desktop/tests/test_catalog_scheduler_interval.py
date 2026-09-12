"""Verify the actual catalog scheduler cadence without threads or network."""
import unittest
from unittest.mock import Mock, patch

from services import official_api_catalog as catalog


class CatalogSchedulerIntervalTests(unittest.TestCase):
    def test_default_start_passes_five_minutes_to_worker(self):
        worker = Mock()
        with patch.object(catalog, "_SCHEDULER_THREAD", None), patch.object(
            catalog, "_SCHEDULER_STOP"
        ) as stop, patch.object(catalog.threading, "Thread", return_value=worker) as factory:
            self.assertIs(worker, catalog.start_official_api_catalog_scheduler())
            self.assertEqual((300,), factory.call_args.kwargs["args"])
            self.assertIs(catalog._catalog_scheduler_loop, factory.call_args.kwargs["target"])
            worker.start.assert_called_once_with()
            stop.clear.assert_called_once_with()

    def test_first_refresh_is_immediate_and_failure_retries_after_five_minutes(self):
        events = []
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        stop.wait.side_effect = lambda seconds: events.append(("wait", seconds))

        def refresh():
            events.append(("refresh",))
            if len(events) == 1:
                raise RuntimeError("temporary catalog failure")

        with patch.object(catalog, "_SCHEDULER_STOP", stop), patch.object(
            catalog, "start_official_api_catalog_sync", side_effect=refresh
        ):
            catalog._catalog_scheduler_loop(300)
        self.assertEqual([("refresh",), ("wait", 300), ("refresh",), ("wait", 300)], events)

    def test_repeated_start_does_not_create_another_scheduler(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with patch.object(catalog, "_SCHEDULER_THREAD", worker), patch.object(
            catalog.threading, "Thread"
        ) as factory:
            self.assertIs(worker, catalog.start_official_api_catalog_scheduler())
            factory.assert_not_called()

    def test_busy_sync_coalesces_refresh_without_starting_parallel_worker(self):
        worker = Mock()
        worker.is_alive.return_value = True
        with patch.object(catalog, "_THREAD", worker), patch.object(
            catalog, "_PENDING_ALL", False
        ), patch.object(catalog, "_PENDING_ACCOUNT_UIDS", set()), patch.object(
            catalog.threading, "Thread"
        ) as factory:
            for _ in range(3):
                self.assertTrue(catalog.start_official_api_catalog_sync()["queued"])
            self.assertTrue(catalog._PENDING_ALL)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
