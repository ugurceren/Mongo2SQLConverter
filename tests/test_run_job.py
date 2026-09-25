"""`execute_transfer`: lock, checkpoint lifecycle and ordering, on fake SQL and Mongo."""

from __future__ import annotations

import functools
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyodbc

from core import checkpoint as cp
from core.checkpoint import MISSING
from core.rejects import RejectLog
from core.run_job import TransferRequest, execute_transfer
from core.writer import LockBusy
from tests import fakes
from tests.fixtures import documents, plan_for

QUIET = logging.getLogger("mongo2sql.tests")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False

DOCS = documents(900, seed=8)
PLAN, KEY_TYPE = plan_for(DOCS, "deep")
ROOT = PLAN["root"]["table"]


class RunDb(fakes.FakeDb):
    def __init__(self, server, calls):
        super().__init__(server)
        self.calls = calls
        self.conn = fakes.RecordingConnection()

    def truncate_tables(self, schema, root, children):
        self.calls.append("truncate")
        for rows in self.server.tables.values():
            rows.clear()
        return "truncate"

    def has_rows(self, schema, table):
        return bool(self.server.tables.get(table))

    def max_key(self, schema, table, column="mongo_id"):
        keys = self.server.root_keys()
        return True, (max(keys) if keys else None)


class RunOps(fakes.FakeOps):
    def __init__(self, server, calls, busy=False):
        super().__init__(server)
        self.calls = calls
        self.busy = busy
        self.closed = False

    def open(self, lock_wait_ms=0):
        if self.busy:
            raise LockBusy("busy")
        super().open(lock_wait_ms)
        self.db = RunDb(self.server, self.calls)

    def close(self):
        self.closed = True
        super().close()


class ExecuteTransferTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.calls: list[str] = []
        self.store = fakes.MongoStore()
        for doc in DOCS:
            self.store.add(doc)
        self.mongo = fakes.FakeMongo({"conversations": fakes.FakeCollection(self.store)})
        self.server = fakes.FakeServer(PLAN)
        self.saved = mock.Mock()
        server, calls = self.server, self.calls

        def read(db, schema, table):
            point = server.checkpoint
            if point is None:
                return None
            last = point["last_id"]
            return cp.Checkpoint(
                target_table=table, collection="conversations", mongo_database="app",
                run_id=point["run_id"], mode=point["mode"], status=point["status"],
                first_load=point["first_load"], plan_hash=point["plan_hash"], filter_hash=point["filter_hash"],
                last_id_json=None if last is MISSING else cp.encode_id(last)[0], last_id_type=None,
                docs_done=point["docs_done"], docs_written=point["docs_written"],
                rows_written=point["rows_written"], rejected=point["rejected"], clipped=point["clipped"],
                resume_count=point["resume_count"],
            )

        def begin(db, schema, point):
            calls.append("checkpoint.begin")
            server.begin_run(
                point.run_id, mode=point.mode, status="running", first_load=point.first_load,
                plan_hash=point.plan_hash, filter_hash=point.filter_hash,
                last_id=cp.decode_id(point.last_id_json) if point.last_id_json else MISSING,
                docs_done=point.docs_done, docs_written=point.docs_written, rows_written=point.rows_written,
                rejected=point.rejected, clipped=point.clipped, resume_count=point.resume_count,
            )

        def finish(db, schema, table, run_id, status, error=None):
            calls.append(f"checkpoint.finish:{status}")
            if server.checkpoint and server.checkpoint["run_id"] == run_id:
                server.checkpoint["status"] = status
                server.checkpoint["error"] = error

        def delete(cursor, schema, table):
            calls.append("checkpoint.delete")
            server.checkpoint = None

        def ensure(db, plan, recreate=False):
            if recreate:
                calls.append("recreate")
                for rows in server.tables.values():
                    rows.clear()
            return [], list(server.tables)

        patches = [
            mock.patch.object(cp, "ensure_table", return_value=True),
            mock.patch.object(cp, "read", side_effect=read),
            mock.patch.object(cp, "begin", side_effect=begin),
            mock.patch.object(cp, "finish", side_effect=finish),
            mock.patch.object(cp, "delete", side_effect=delete),
            mock.patch("core.run_job.ensure_tables", side_effect=ensure),
            mock.patch("core.run_job.save_sync_watermark", self.saved),
            mock.patch("core.run_job.load_sync_watermark", return_value=None),
            mock.patch("core.run_job.RejectLog", functools.partial(RejectLog, directory=self.directory)),
            mock.patch("core.run_job.get_logger", return_value=QUIET),
            mock.patch("core.transfer.get_logger", return_value=QUIET),
            mock.patch("core.logutil.get_logger", return_value=QUIET),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, *, busy=False, should_stop=None, **request):
        ops = RunOps(self.server, self.calls, busy=busy)
        with mock.patch("core.run_job.LiveSql", return_value=ops):
            result = execute_transfer(
                self.mongo,
                lambda: None,
                TransferRequest(collection="conversations", plan=PLAN, mongo_database="app", batch=100, **request),
                should_stop=should_stop,
            )
        self.assertTrue(ops.closed)
        return result

    def stop_after(self, count):
        server = self.server
        return lambda: server.checkpoint is not None and server.checkpoint["docs_done"] >= count

    def test_stopped_load_resumes_and_finishes(self):
        first = self.execute(should_stop=self.stop_after(300))
        self.assertTrue(first.stats.stopped)
        self.assertEqual(self.server.checkpoint["status"], "stopped")
        second = self.execute()
        self.assertIn("yarım kalan", second.note)
        self.assertEqual(self.server.checkpoint["status"], "completed")
        self.assertEqual(len(self.server.root_keys()), len(DOCS))
        self.assertEqual(self.server.checkpoint["docs_written"], len(DOCS))
        self.assertEqual(first.stats.written + second.stats.written, len(DOCS))
        self.assertIsNone(second.stats.rejects_path)  # nothing was rejected: no file to point at
        self.saved.assert_called()

    def test_clearing_forgets_the_checkpoint_first(self):
        self.execute()
        self.calls.clear()
        result = self.execute(mode="full", clear_first=True)
        self.assertLess(self.calls.index("checkpoint.delete"), self.calls.index("truncate"))
        self.assertLess(self.calls.index("truncate"), self.calls.index("checkpoint.begin"))
        self.assertTrue(result.stats.first_load)
        self.assertEqual(len(self.server.root_keys()), len(DOCS))

    def test_recreate_forgets_the_checkpoint_first(self):
        self.execute(should_stop=self.stop_after(200))
        self.calls.clear()
        result = self.execute(mode="full", recreate=True)
        self.assertLess(self.calls.index("checkpoint.delete"), self.calls.index("recreate"))
        self.assertFalse(result.stats.resumed)
        self.assertEqual(len(self.server.root_keys()), len(DOCS))

    def test_failure_labels_the_checkpoint(self):
        self.server.insert_faults = {3: fakes.missing_table()}
        with self.assertRaises(pyodbc.ProgrammingError):
            self.execute()
        self.assertEqual(self.server.checkpoint["status"], "failed")
        self.assertIn("208", self.server.checkpoint["error"])

    def test_busy_lock_touches_nothing(self):
        with self.assertRaises(LockBusy):
            self.execute(busy=True)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.server.checkpoint)


if __name__ == "__main__":
    unittest.main()
