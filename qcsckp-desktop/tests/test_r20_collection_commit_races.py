"""Real SQLite in-memory commits against deterministic revoke/cancel races."""
from contextlib import contextmanager, ExitStack, nullcontext
from datetime import datetime, timedelta
import sqlite3
import threading
import unittest
from unittest.mock import patch

from services import collection_lifecycle as lifecycle
from services.qianchuan_open_api.collection_context import CollectionContext, managed_cancellation_gate, use_collection_context
from services.qianchuan_open_api.errors import CollectionCancelledError
from services.qianchuan_open_api.managed_workers import ManagedTask


class CommitConnection(sqlite3.Connection):
    hook = None
    committed_hook = None

    def commit(self):
        active = self.in_transaction
        if active and self.hook:
            self.hook()
        result = super().commit()
        if active and self.committed_hook:
            self.committed_hook()
        return result


class MemoryStore:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False, factory=CommitConnection)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE promotion_target(target_uid TEXT,account_uid TEXT,aadvid TEXT,ad_id TEXT,promotion_scene TEXT,plan_system TEXT,enabled INTEGER);
            CREATE TABLE qianchuan_account(account_uid TEXT,owner_username TEXT,aavid TEXT,enabled INTEGER);
            CREATE TABLE collection_job(id INTEGER,status TEXT,lease_owner TEXT,fencing_token INTEGER,lease_expires_at TEXT,target_uid TEXT,owner_username TEXT,account_uid TEXT);
            CREATE TABLE observations(value INTEGER);
            INSERT INTO promotion_target VALUES('target','account','1001','2001','live','global',1);
            INSERT INTO qianchuan_account VALUES('account','owner','1001',1);
        """)

    @contextmanager
    def transaction(self):
        # Same yield/commit/rollback semantics as SQLiteStore.transaction.
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def select_one(self, table, *, where, connection=None):
        result = (connection or self.connection).execute(
            "SELECT * FROM " + table + " WHERE " + " AND ".join(key + "=?" for key in where),
            tuple(where.values()),
        ).fetchone()
        return dict(result) if result else None

    def count(self):
        return self.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


class CollectionCommitRaceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(lifecycle, "_GENERATION", "epoch-one"))
        self.stack.enter_context(patch.object(lifecycle, "_CONTEXTS", {}))
        self.store = MemoryStore()
        self.target = self.store.select_one("promotion_target", where={"target_uid": "target"})
        self.context = CollectionContext(300, generation="epoch-one", is_current=lambda: lifecycle.generation() == "epoch-one")
        self.context.target_identity = {key: str(self.target[key]) for key in (
            "target_uid", "account_uid", "aadvid", "ad_id", "promotion_scene", "plan_system")}
        self.context.target_identity["owner_username"] = "owner"
        self.context.authorization_identity = {"owner_username": "owner", "app_id": "app", "auth_generation": "auth"}
        self.auth_held = False
        @contextmanager
        def auth_guard(expected):
            self.auth_held = True
            try:
                yield
            finally:
                self.auth_held = False
        self.stack.enter_context(patch("services.qianchuan_open_api.token_provider.authorization_identity_guard", side_effect=auth_guard))
        lifecycle.register(self.context)

    def tearDown(self):
        self.store.connection.close()
        self.stack.close()

    def test_revoke_or_cancel_cannot_cross_the_final_commit(self):
        for action in ("revoke", "context_cancel", "managed_cancel"):
            with self.subTest(action=action):
                # New context per subcase, sharing the actual cancellation lock.
                self.context._cancel.clear()
                lifecycle._GENERATION = "epoch-one"
                self.store.connection.execute("DELETE FROM observations")
                self.store.connection.commit()
                task = ManagedTask(self.context)
                commit_entered, release_commit = threading.Event(), threading.Event()
                cancellation_attempted, cancellation_finished = threading.Event(), threading.Event()
                order, errors = [], []
                def before_commit():
                    self.assertTrue(self.auth_held)
                    commit_entered.set()
                    if not release_commit.wait(2):
                        raise AssertionError("test did not release commit")
                self.store.connection.hook = before_commit
                self.store.connection.committed_hook = lambda: order.append("committed")
                def writer():
                    try:
                        gate = managed_cancellation_gate(task.cancelled) if action == "managed_cancel" else nullcontext()
                        with use_collection_context(self.context), gate:
                            with lifecycle.owned_transaction(self.store, self.target) as connection:
                                connection.execute("INSERT INTO observations VALUES(1)")
                    except Exception as exc:
                        errors.append(exc)
                def cancel():
                    cancellation_attempted.set()
                    if action == "revoke":
                        lifecycle.revoke("epoch-one", "race")
                    elif action == "context_cancel":
                        self.context.cancel("race")
                    else:
                        task.cancel()
                    order.append("cancelled")
                    cancellation_finished.set()
                writing = threading.Thread(target=writer)
                writing.start()
                self.assertTrue(commit_entered.wait(2))
                cancelling = threading.Thread(target=cancel)
                cancelling.start()
                self.assertTrue(cancellation_attempted.wait(2))
                self.assertFalse(cancellation_finished.wait(.05))
                release_commit.set()
                writing.join(2)
                cancelling.join(2)
                self.assertFalse(writing.is_alive())
                self.assertFalse(cancelling.is_alive())
                self.assertEqual([], errors)
                self.assertEqual(["committed", "cancelled"], order)
                self.assertEqual(1, self.store.count())
                self.store.connection.hook = self.store.connection.committed_hook = None

    def test_revoke_while_db_is_held_does_not_wait_for_db_and_forces_rollback(self):
        body_entered, finish_body, revoked = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def writer():
            try:
                with use_collection_context(self.context), lifecycle.owned_transaction(self.store, self.target) as connection:
                    connection.execute("INSERT INTO observations VALUES(1)")
                    body_entered.set()
                    finish_body.wait(2)
            except Exception as exc:
                errors.append(exc)
        writing = threading.Thread(target=writer)
        writing.start()
        self.assertTrue(body_entered.wait(2))
        cancelling = threading.Thread(target=lambda: (lifecycle.revoke("epoch-one", "race"), revoked.set()))
        cancelling.start()
        self.assertTrue(revoked.wait(1), "revoke must not acquire a DB lock while holding lifecycle lock")
        finish_body.set()
        writing.join(2)
        cancelling.join(2)
        self.assertFalse(writing.is_alive())
        self.assertIsInstance(errors[0], CollectionCancelledError)
        self.assertEqual(0, self.store.count())

    def test_scope_and_account_changes_in_transaction_are_rechecked_at_commit(self):
        with use_collection_context(self.context), self.assertRaises(CollectionCancelledError):
            with lifecycle.owned_transaction(self.store, self.target) as connection:
                connection.execute("INSERT INTO observations VALUES(1)")
                connection.execute("UPDATE qianchuan_account SET aavid='9999'")
        self.assertEqual(0, self.store.count())
        self.assertEqual("1001", self.store.select_one("qianchuan_account", where={"account_uid": "account"})["aavid"])

    def test_supplied_target_cannot_borrow_another_context_scope(self):
        with use_collection_context(self.context), self.assertRaises(CollectionCancelledError):
            with lifecycle.owned_transaction(self.store, {**self.target, "target_uid": "other"}):
                self.fail("mismatched scope was accepted")

    def test_lease_fence_is_rechecked_before_final_commit(self):
        expiry = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.connection.execute("INSERT INTO collection_job VALUES(1,'leased','worker',4,?,'target','owner','account')", (expiry,))
        self.store.connection.commit()
        self.context.job_claim = {"id": 1, "lease_owner": "worker", "fencing_token": 4}
        with use_collection_context(self.context), self.assertRaises(CollectionCancelledError):
            with lifecycle.owned_transaction(self.store, self.target) as connection:
                connection.execute("INSERT INTO observations VALUES(1)")
                connection.execute("UPDATE collection_job SET fencing_token=5 WHERE id=1")
        self.assertEqual(0, self.store.count())

    def test_child_inherits_auth_scope_and_shared_commit_lock(self):
        child = self.context.child().child()
        self.assertEqual(self.context.authorization_identity, child.authorization_identity)
        self.assertIsNot(self.context.authorization_identity, child.authorization_identity)
        self.assertEqual(self.context.target_identity, child.target_identity)
        self.assertIs(self.context._lock, child._lock)
        observed = []
        self.store.connection.hook = lambda: observed.append(self.auth_held)
        with use_collection_context(child), lifecycle.owned_transaction(self.store, self.target) as connection:
            connection.execute("INSERT INTO observations VALUES(1)")
        self.assertEqual([True], observed)
        with use_collection_context(child), self.assertRaises(CollectionCancelledError):
            with lifecycle.owned_transaction(self.store, {**self.target, "target_uid": "other"}):
                self.fail("child lost target ownership")

    def test_child_cannot_drop_parent_job_lease_guard(self):
        self.context.job_claim = {"id": 99, "lease_owner": "worker", "fencing_token": 4}
        child = self.context.child()
        self.assertEqual(self.context.job_claim, child.job_claim)
        self.assertIsNot(self.context.job_claim, child.job_claim)
        with use_collection_context(child), self.assertRaises(CollectionCancelledError):
            with lifecycle.owned_transaction(self.store, self.target):
                self.fail("missing parent lease was ignored by child")
