"""Only memory, mock transports and bounded local workers; never platform IO."""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from urllib.error import URLError
from unittest.mock import patch

from services.qianchuan_open_api.client import ApiResponse, EndpointRateLimiter, QianchuanOpenApiClient
from services.qianchuan_open_api.collection_context import CollectionContext, current_collection_context, request_fingerprint, use_collection_context
from services.qianchuan_open_api.errors import (
    ApiRequestError, ApiWriteOutcomeUnknown, CollectionCancelledError, CollectionDeadlineExceeded,
    ManagedWorkerUnavailable, PageAttemptBudgetExceeded, PaginationDriftError, PaginationIntegrityError,
)
from services.qianchuan_open_api.managed_workers import ManagedWorkers
from services.qianchuan_open_api.pagination_evidence import help_evidence
from services.qianchuan_open_api.token_provider import AccessTokenBundle, InjectedTokenProvider

MATERIAL = "/open_api/v1.0/qianchuan/uni_promotion/ad/material/get/"
REPORT = "/open_api/v1.0/qianchuan/report/uni_promotion/data/get/"


def payload(page, ids, *, total=2, pages=2, size=1, key="rows"):
    return {key: [{"id": str(value)} for value in ids], "page_info": {
        "page": page, "page_size": size, "total_number": total, "total_page": pages}}


class FakeClient(QianchuanOpenApiClient):
    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def get(self, endpoint, query=None, **kwargs):
        self.calls.append(query["page"])
        data = self.fn(query["page"])
        return ApiResponse(data=data, raw={}, request_id=f"mock-{len(self.calls)}")


class Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")


class NoWait:
    def wait_for_request(self, *args):
        pass


class PaginationRuntimeTests(unittest.TestCase):
    def test_declared_empty_primary_does_not_recurse_into_summary(self):
        data = {"rows": [], "summary": {"items": [{"not_a_row": 1}]},
                "page_info": {"page": 1, "total_page": 0, "total_number": 0, "page_size": 200}}
        client = FakeClient(lambda page: data)
        self.assertEqual([], client.get_all_pages(REPORT, {}, items_key="rows", page_size=200)[0])
        self.assertEqual([], QianchuanOpenApiClient.extract_items(data))
        with self.assertRaises(PaginationIntegrityError):
            client.get_all_pages(REPORT, {}, items_key="missing", page_size=200)

    def test_serial_and_parallel_check_every_page_capacity_and_echo(self):
        for workers in (1, 3):
            for corruption in ("size", "page"):
                with self.subTest(workers=workers, corruption=corruption):
                    def page_data(page):
                        data = payload(page, [page])
                        if page == 2:
                            data["page_info"]["page_size" if corruption == "size" else "page"] = 99
                        return data
                    with self.assertRaises(PaginationIntegrityError):
                        FakeClient(page_data).get_all_pages(REPORT, {}, page_size=1, items_key="rows", parallel_workers=workers)

    def test_false_string_has_more_is_not_treated_as_true(self):
        with self.assertRaises(PaginationIntegrityError):
            QianchuanOpenApiClient._has_more({"has_more": "false"}, page=1, page_size=100, item_count=0)

    def test_auto_primary_never_silently_drops_malformed_rows(self):
        for data in ([{"id": "1"}, "bad"], {"items": [{"id": "1"}, "bad"], "has_more": False}):
            with self.subTest(shape=type(data).__name__), self.assertRaises(PaginationIntegrityError):
                QianchuanOpenApiClient.extract_items(data)
        with self.assertRaises(PaginationIntegrityError):
            FakeClient(lambda page: {"items": [{"id": "1"}, "bad"], "has_more": False}).get_all_pages(REPORT, {})

    def test_contradictory_metadata_never_becomes_success(self):
        for info in ({"total_page": 0}, {"total_number": 1, "total_page": 2},
                     {"total_number": 1, "total_num": 2, "total_page": 1}):
            client = FakeClient(lambda page: {"rows": [{"id": "1"}], "page_info": info})
            with self.subTest(info=info), self.assertRaises(PaginationIntegrityError):
                client.get_all_pages(REPORT, {}, items_key="rows", page_size=100)
            self.assertEqual([1], client.calls)

    def test_missing_verification_total_is_not_claimed_as_number_drift(self):
        first_count = [0]
        def data(page):
            result = payload(page, [page])
            if page == 1:
                first_count[0] += 1
                if first_count[0] > 1:
                    result["page_info"].pop("total_number")
            return result
        with self.assertRaises(PaginationIntegrityError) as caught:
            FakeClient(data).get_all_pages(REPORT, {}, page_size=1, items_key="rows", verify_stability=True)
        self.assertNotIsInstance(caught.exception, PaginationDriftError)
        self.assertEqual("client_page_metadata_missing", caught.exception.code)

    def test_only_one_complete_rescan_across_all_scopes_in_context(self):
        responses = iter([payload(1, [1]), payload(2, [2], total=3, pages=3),
                          payload(1, [1]), payload(2, [2]), payload(1, [1])])
        client = FakeClient(lambda page: next(responses))
        context = CollectionContext(2)
        rows, _ = client.get_all_pages(REPORT, {}, page_size=1, items_key="rows", verify_stability=True, pagination_context=context)
        self.assertEqual(2, len(rows))
        self.assertEqual([1, 2, 1, 2, 1], client.calls)
        self.assertFalse(context.reserve_rescan("another-scope"))

    def test_duplicate_unknown_total_stops_before_requesting_more_pages(self):
        client = FakeClient(lambda page: {"rows": [{"id": "same"}], "has_more": True})
        with self.assertRaisesRegex(PaginationDriftError, "重复页面"):
            client.get_all_pages(REPORT, {}, page_size=1, items_key="rows", max_pages=10)
        self.assertEqual([1, 2, 1, 2], client.calls)

    def test_count_mismatch_can_rescan_once_without_accepting_first_partial(self):
        responses = iter([payload(1, [1], total=2, pages=1, size=200),
                          payload(1, [1, 2], total=2, pages=1, size=200)])
        client = FakeClient(lambda page: next(responses))
        rows, _ = client.get_all_pages(REPORT, {}, page_size=200, items_key="rows")
        self.assertEqual(2, len(rows))
        self.assertEqual([1, 1], client.calls)

    def test_one_first_screen_change_recovers_but_repeated_changes_fail(self):
        for persistent in (False, True):
            first_calls = [0]
            def data(page):
                if page == 1:
                    first_calls[0] += 1
                    ident = str(first_calls[0]) if persistent else ("old" if first_calls[0] == 1 else "new")
                    return payload(page, [ident])
                return payload(page, ["second"])
            client = FakeClient(data)
            if persistent:
                with self.assertRaises(PaginationDriftError):
                    client.get_all_pages(REPORT, {}, page_size=1, items_key="rows", identity_getter=lambda row: row["id"], verify_stability=True)
            else:
                rows, _ = client.get_all_pages(REPORT, {}, page_size=1, items_key="rows", identity_getter=lambda row: row["id"], verify_stability=True)
                self.assertEqual("new", rows[0]["id"])
            self.assertEqual(4, first_calls[0])

    def test_dispatch_is_bounded_and_fatal_failure_stops_new_pages(self):
        gate, exited = threading.Event(), threading.Event()
        active = [0, 0]
        lock = threading.Lock()
        def data(page):
            with lock:
                active[0] += 1
                active[1] = max(active[1], active[0])
            try:
                if page == 2:
                    gate.wait(2)
                    exited.set()
                if page == 3:
                    raise ApiRequestError("invalid parameter", code="40000")
                return payload(page, [page], total=20, pages=20)
            finally:
                with lock:
                    active[0] -= 1
        client = FakeClient(data)
        try:
            with self.assertRaises(ApiRequestError):
                client.get_all_pages(MATERIAL, {}, page_size=1, items_key="rows", parallel_workers=3)
            self.assertLessEqual(active[1], 3)
            self.assertTrue(set(client.calls).issubset({1, 2, 3, 4}))
        finally:
            gate.set()
            exited.wait(2)

    def test_full_dimension_identity_preserves_distinct_rows_for_scope_validation(self):
        rows = [{"dimensions": {"material_id": "m1", "name": name}} for name in ("A", "B")]
        client = FakeClient(lambda page: {"rows": rows, "page_info": {"page": 1, "total_page": 1, "total_number": 2, "page_size": 200}})
        result, _ = client.get_all_pages(REPORT, {}, page_size=200, items_key="rows",
            identity_getter=lambda row: tuple(sorted(row["dimensions"].items())))
        self.assertEqual(2, len(result))


class HttpAttemptTests(unittest.TestCase):
    def client(self, audit):
        return QianchuanOpenApiClient(InjectedTokenProvider(AccessTokenBundle("fake-token")),
            rate_limiter=NoWait(), audit_sink=audit.append, sleep=lambda seconds: None)

    def transport(self, fn, calls):
        def send(request, **kwargs):
            page = int(parse_qs(urlparse(request.full_url).query).get("page", ["0"])[0])
            calls.append(page)
            return fn(page)
        return send

    def success(self, page):
        return Response({"code": 0, "request_id": f"server-{page}", "data": payload(page, [page], key="ad_material_infos")})

    def test_400153_later_material_page_has_only_one_same_page_recheck(self):
        calls, audits = [], []
        second = [0]
        def response(page):
            if page == 2:
                second[0] += 1
                if second[0] == 1:
                    return Response({"code": 400153, "message": "系统开小差，稍后重试"})
            return self.success(page)
        with patch("services.qianchuan_open_api.client.urlopen", side_effect=self.transport(response, calls)):
            result, _ = self.client(audits).get_all_pages(MATERIAL, {}, page_size=1, items_key="ad_material_infos")
        self.assertEqual(2, len(result))
        self.assertEqual([1, 2, 2], calls)
        self.assertEqual(3, len({row["request_uid"] for row in audits}))
        self.assertTrue(all(row["response"]["http_attempt"] for row in audits))
        self.assertEqual(1, audits[-1]["response"]["pagination"]["actual_list_count"])

    def test_400153_parameter_help_prevents_recheck_and_does_not_leak(self):
        calls, audits = [], []
        secret = "private_short_secret"
        def response(page):
            return self.success(page) if page == 1 else Response({"code": 400153, "message": "parameter rejected",
                "help_message": f'invalid field stat_cost_for_roi2; app_secret="{secret}"; Authorization: Bearer bearer-secret'})
        with patch("services.qianchuan_open_api.client.urlopen", side_effect=self.transport(response, calls)):
            with self.assertRaises(ApiRequestError) as caught:
                self.client(audits).get_all_pages(MATERIAL, {}, page_size=1, items_key="ad_material_infos")
        self.assertEqual([1, 2], calls)
        self.assertTrue(caught.exception.help_message["parameter_error"])
        self.assertIn("stat_cost_for_roi2", caught.exception.help_message["field_names"])
        self.assertNotIn(secret, json.dumps(audits))
        self.assertNotIn("bearer-secret", json.dumps(audits))

    def test_network_retries_and_400153_recheck_share_four_physical_sends(self):
        calls, audits = [], []
        second = [0]
        def response(page):
            if page == 1:
                return self.success(page)
            second[0] += 1
            if second[0] <= 3:
                raise URLError("mock timeout")
            return Response({"code": 400153, "message": "ambiguous failure"})
        with patch("services.qianchuan_open_api.client.urlopen", side_effect=self.transport(response, calls)):
            with self.assertRaises(PageAttemptBudgetExceeded):
                self.client(audits).get_all_pages(MATERIAL, {}, page_size=1, items_key="ad_material_infos")
        self.assertEqual(4, calls.count(2))
        self.assertEqual(5, sum(row["response"]["http_attempt"] for row in audits))

    def test_recheck_itself_does_not_gain_four_more_network_attempts(self):
        calls, audits = [], []
        second = [0]
        def response(page):
            if page == 1:
                return self.success(page)
            second[0] += 1
            return Response({"code": 400153, "message": "ambiguous"}) if second[0] == 1 else Response({"code": 50000, "message": "server unavailable"})
        with patch("services.qianchuan_open_api.client.urlopen", side_effect=self.transport(response, calls)):
            with self.assertRaises(ApiRequestError):
                self.client(audits).get_all_pages(MATERIAL, {}, page_size=1, items_key="ad_material_infos")
        self.assertEqual([1, 2, 2], calls)

    def test_first_page_and_report_400153_never_get_material_recheck(self):
        for endpoint in (MATERIAL, REPORT):
            calls, audits = [], []
            with patch("services.qianchuan_open_api.client.urlopen", side_effect=self.transport(
                    lambda page: Response({"code": 400153, "message": "稍后重试"}), calls)):
                with self.assertRaises(ApiRequestError):
                    self.client(audits).get_all_pages(endpoint, {}, page_size=1)
            self.assertEqual([1], calls)

    def test_post_is_not_retried(self):
        audits = []
        with patch("services.qianchuan_open_api.client.urlopen", side_effect=URLError("mock loss")) as send:
            with self.assertRaises(ApiWriteOutcomeUnknown):
                self.client(audits).post("/open_api/mock/write/", {"task_id": "fake-task"})
        send.assert_called_once()

    def test_deadline_is_passed_to_io_and_late_success_is_discarded(self):
        gate, entered, finished = threading.Event(), threading.Event(), threading.Event()
        calls, audits, timeouts = [], [], []
        def send(request, timeout):
            timeouts.append(timeout)
            entered.set()
            gate.wait(1)
            finished.set()
            return self.success(1)
        context = CollectionContext(0.05)
        start = time.monotonic()
        try:
            with patch("services.qianchuan_open_api.client.urlopen", side_effect=send):
                with self.assertRaises(CollectionDeadlineExceeded):
                    self.client(audits).get_all_pages(MATERIAL, {}, page_size=1, items_key="ad_material_infos", pagination_context=context)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertTrue(entered.is_set())
            self.assertLessEqual(timeouts[0], 0.050001)
        finally:
            gate.set()
            finished.wait(1)
        self.assertFalse(any(row["status"] == "success" for row in audits))

    def test_limiter_wait_respects_deadline_before_any_http_send(self):
        audits = []
        client = self.client(audits)
        client.rate_limiter = EndpointRateLimiter()
        client.rate_limiter._next["application"] = time.monotonic() + 5
        with patch("services.qianchuan_open_api.client.urlopen") as send, use_collection_context(CollectionContext(0.03)):
            with self.assertRaises(CollectionDeadlineExceeded):
                client.get(MATERIAL, {"page": 1, "page_size": 100})
        send.assert_not_called()

    def test_blocked_token_acquisition_cannot_send_after_deadline(self):
        gate, entered, finished = threading.Event(), threading.Event(), threading.Event()
        class Provider:
            def get_token(self, **kwargs):
                entered.set()
                gate.wait(1)
                finished.set()
                return AccessTokenBundle("late-fake-token")
        client = QianchuanOpenApiClient(Provider(), rate_limiter=NoWait(), sleep=lambda seconds: None)
        try:
            with patch("services.qianchuan_open_api.client.urlopen") as send, use_collection_context(CollectionContext(0.03)):
                with self.assertRaises(CollectionDeadlineExceeded):
                    client.get(MATERIAL, {"page": 1})
                self.assertTrue(entered.is_set())
                send.assert_not_called()
        finally:
            gate.set()
            finished.wait(1)

    def test_auth_retry_passes_rejected_revision_and_context_change_is_terminal(self):
        calls = []
        class Provider:
            def get_token(self, force_refresh=False, rejected_token_revision=None):
                calls.append((force_refresh, rejected_token_revision))
                return SimpleNamespace(access_token="fake", token_revision=2 if force_refresh else 1)
        responses = iter([Response({"code": 40100, "message": "access_token expired"}, status=401),
                          Response({"code": 0, "data": {}})])
        client = QianchuanOpenApiClient(Provider(), rate_limiter=NoWait(), sleep=lambda seconds: None)
        with patch("services.qianchuan_open_api.client.urlopen", side_effect=lambda *args, **kwargs: next(responses)):
            client.get(MATERIAL, {})
        self.assertEqual([(False, None), (True, 1)], calls)
        from services.qianchuan_open_api.errors import ApiTokenError
        class ChangedProvider:
            def get_token(self, **kwargs):
                raise ApiTokenError("changed", code="authorization_context_changed")
        context = CollectionContext(1)
        with patch("services.qianchuan_open_api.client.urlopen") as send, use_collection_context(context):
            with self.assertRaises(ApiTokenError):
                QianchuanOpenApiClient(ChangedProvider(), rate_limiter=NoWait()).get(MATERIAL, {})
        send.assert_not_called()
        with self.assertRaises(CollectionCancelledError):
            context.check_active()

    def test_unknown_help_keeps_diagnostic_text_without_private_names_or_secrets(self):
        evidence = help_evidence('PrivateStudentName: fields size must be <= 200; trace backend malformed row; app_secret="short_secret"; Authorization: Bearer short-bearer')
        encoded = json.dumps(evidence)
        self.assertIn("200", evidence["text"])
        self.assertIn("malformed row", evidence["text"])
        self.assertNotIn("PrivateStudentName", encoded)
        self.assertNotIn("short_secret", encoded)
        self.assertNotIn("short-bearer", encoded)

    def test_help_redacts_short_header_and_named_credentials(self):
        for secret in ("Cookie: sid=short-cookie", "Set-Cookie: sid=short-set-cookie",
                       "api_key=short-key", "sessionid=short-session",
                       "verification_token=short-verifier", "encrypt_key=short-encrypt"):
            with self.subTest(secret=secret.split(":")[0].split("=")[0]):
                evidence = help_evidence("page_size must be <= 200\n" + secret)
                self.assertIn("page_size must be <= 200", evidence["text"])
                self.assertNotIn("short-", json.dumps(evidence))


class ManagedWorkerTests(unittest.TestCase):
    def test_cancelled_old_io_keeps_scope_capacity_until_actual_exit(self):
        parent = CollectionContext(3)
        old, new = parent.child(), parent.child()
        releases = [threading.Event() for _ in range(3)]
        entered = [threading.Event() for _ in range(3)]
        new_entered = threading.Event()
        workers = ManagedWorkers(4)
        def old_io(index):
            with old.io_slot("one-scope"):
                entered[index].set()
                releases[index].wait(2)
        tasks = [workers.submit(lambda i=i: old_io(i), context=old) for i in range(3)]
        self.assertTrue(all(event.wait(1) for event in entered))
        old.cancel()
        def new_io():
            with new.io_slot("one-scope"):
                new_entered.set()
        fourth = workers.submit(new_io, context=new)
        try:
            self.assertFalse(new_entered.wait(0.03))
            releases[0].set()
            self.assertTrue(new_entered.wait(1))
        finally:
            for event in releases:
                event.set()
            for task in (*tasks, fourth):
                task.join(1)
        self.assertEqual(0, workers.snapshot()["occupied"])
    def test_task_cancel_propagates_to_context_checks_without_cancelling_parent(self):
        ctx = CollectionContext(1)
        entered = threading.Event()
        def work():
            entered.set()
            while True:
                current_collection_context().wait(0.01)
        task = ManagedWorkers(1).submit(work, context=ctx)
        self.assertTrue(entered.wait(1))
        task.cancel()
        task.join(1)
        self.assertTrue(task.done())
        with self.assertRaises(CollectionCancelledError):
            task.result()
        ctx.check_active("parent_still_usable")
    def test_unacknowledged_start_is_bounded_and_its_slot_quarantined(self):
        workers = ManagedWorkers(1, startup_timeout=0.02, capacity_timeout=0.02,
                                 starter=lambda function, args: 123)
        start = time.monotonic()
        with self.assertRaises(ManagedWorkerUnavailable):
            workers.submit(lambda: None, context=CollectionContext(1))
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(1, workers.snapshot()["unacknowledged"])
        with self.assertRaises(ManagedWorkerUnavailable):
            workers.submit(lambda: None, context=CollectionContext(None))
        self.assertEqual(1, workers.snapshot()["occupied"])

    def test_native_start_exception_does_not_reuse_uncertain_capacity(self):
        def fail_start(fn, args):
            raise MemoryError("mock ambiguous native start")
        workers = ManagedWorkers(1, starter=fail_start, capacity_timeout=0.02)
        with self.assertRaises(ManagedWorkerUnavailable):
            workers.submit(lambda: None, context=CollectionContext(1))
        self.assertEqual(1, workers.snapshot()["unacknowledged"])
        with self.assertRaises(ManagedWorkerUnavailable):
            workers.submit(lambda: None, context=CollectionContext(1))

    def test_explicit_context_is_bound_and_runtime_failure_completes_task(self):
        workers = ManagedWorkers(1)
        parent, explicit = CollectionContext(1), CollectionContext(1)
        with use_collection_context(parent):
            task = workers.submit(lambda: current_collection_context(), context=explicit)
        self.assertIs(explicit, task.result())
        def fail():
            raise MemoryError("mock allocation failure")
        task = workers.submit(fail, context=CollectionContext(1))
        with self.assertRaises(MemoryError):
            task.result()
        self.assertTrue(task.done())
        self.assertEqual(0, workers.snapshot()["occupied"])

    def test_generation_change_rejects_late_result_and_callback_runs(self):
        current, gate, entered = [True], threading.Event(), threading.Event()
        ctx = CollectionContext(1, is_current=lambda: current[0])
        def work():
            entered.set()
            gate.wait(1)
            return "must-not-commit"
        task = ManagedWorkers(1).submit(work, context=ctx)
        callback = threading.Event()
        task.add_done_callback(lambda finished: callback.set())
        entered.wait(1)
        current[0] = False
        gate.set()
        task.join(1)
        with self.assertRaises(CollectionCancelledError):
            task.result()
        self.assertTrue(callback.wait(1))


if __name__ == "__main__":
    unittest.main()
