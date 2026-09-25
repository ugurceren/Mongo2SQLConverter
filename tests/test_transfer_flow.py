"""
End to end through `transfer_collection`: a fake Mongo and a fake SQL Server
with transactions, faults injected on both sides, stops and resumes.

The checkpoint handling mirrors `core.run_job.execute_transfer`: decide with
`plan_run`, write the row for the run, load, label the row at the end.
"""

from __future__ import annotations

import copy
import json
import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from bson import ObjectId
from pymongo import errors

from core import checkpoint as cp
from core.checkpoint import MISSING, Checkpoint, plan_run
from core.convert import Flattener
from core.rejects import RejectLog
from core.retry import RetryPolicy
from core.transfer import transfer_collection
from tests import fakes
from tests.fixtures import documents, plan_for

QUIET = logging.getLogger("mongo2sql.tests")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False

FAST = RetryPolicy(base_seconds=0.001, cap_seconds=0.001)
COLLECTION = "conversations"


class Flow:
    """One collection and one SQL database, driven through several runs."""

    def __init__(self, docs, plan, key_type, directory: Path):
        self.docs = docs
        self.plan = plan
        self.key_type = key_type
        self.root = plan["root"]["table"]
        self.directory = directory
        self.store = fakes.MongoStore()
        for doc in docs:
            self.store.add(doc)
        self.mongo = fakes.FakeMongo({COLLECTION: fakes.FakeCollection(self.store)})
        self.server = fakes.FakeServer(plan)
        self.runs = 0

    def checkpoint_row(self) -> Checkpoint | None:
        point = self.server.checkpoint
        if point is None:
            return None
        last = point["last_id"]
        return Checkpoint(
            target_table=self.root,
            collection=COLLECTION,
            mongo_database="app",
            run_id=point["run_id"],
            mode=point["mode"],
            status=point["status"],
            first_load=point["first_load"],
            plan_hash=point["plan_hash"],
            filter_hash=point["filter_hash"],
            last_id_json=None if last is MISSING else cp.encode_id(last)[0],
            last_id_type=None,
            docs_done=point["docs_done"],
            docs_written=point["docs_written"],
            rows_written=point["rows_written"],
            rejected=point["rejected"],
            clipped=point["clipped"],
            resume_count=point["resume_count"],
        )

    def run(self, requested="auto", *, restart=False, should_stop=None, prefetch=True, plan=None, batch_docs=120):
        plan = plan or self.plan
        row = self.checkpoint_row()
        root_empty = not self.server.tables[self.root]
        tables_empty = root_empty and not any(self.server.tables.values())
        run = plan_run(
            requested,
            checkpoint=row,
            restart=restart,
            plan_hash_value=cp.plan_hash(plan),
            filter_hash_value=cp.filter_hash(None),
            tables_empty=tables_empty,
            root_empty=root_empty,
            key_type=self.key_type,
            overlap_minutes=15,
        )
        self.runs += 1
        run_id = f"run-{self.runs}"
        keep_mark = row is not None and bool(row.last_id_json) and (run.resume or run.mode == "incremental")
        self.server.begin_run(
            run_id,
            mode=run.mode,
            status="running",
            first_load=run.first_load,
            plan_hash=cp.plan_hash(plan),
            filter_hash=cp.filter_hash(None),
            last_id=row.last_id if keep_mark else MISSING,
            docs_done=run.docs_done,
            docs_written=run.carried.get("docs_written", 0),
            rows_written=run.carried.get("rows_written", 0),
            rejected=run.carried.get("rejected", 0),
            clipped=run.carried.get("clipped", 0),
            resume_count=run.resume_count,
        )
        ops = fakes.FakeOps(self.server)
        ops.open()
        ops.run_id = run_id
        rejects = RejectLog(COLLECTION, self.root, run_id, directory=self.directory)
        try:
            stats = transfer_collection(
                self.mongo,
                ops,
                plan,
                COLLECTION,
                run=run,
                rejects=rejects,
                batch_docs=batch_docs,
                expected_count=len(self.store.entries),
                should_stop=should_stop,
                prefetch=prefetch,
                reader_options={"chunk": 300, "batch": 50},
                retry_policy=FAST,
            )
        finally:
            rejects.close()
        self.server.checkpoint["status"] = "stopped" if stats.stopped else "completed"
        return run, stats, rejects

    def expected(self, skip=()) -> dict[str, int]:
        flattener = Flattener(self.plan, self.key_type)
        totals = {table: 0 for table in self.server.tables}
        keys = []
        for entry in self.store.entries:
            if entry.doc is None or entry.id in skip:
                continue
            flat = flattener.flatten(entry.doc)
            keys.append(flat.key)
            totals[self.root] += 1
            for table, rows in flat.children.items():
                totals[table] += len(rows)
        return {"keys": sorted(keys), "rows": totals}

    def state(self) -> dict:
        return {
            "keys": sorted(self.server.root_keys()),
            "rows": {table: len(rows) for table, rows in self.server.tables.items()},
        }

    def reject_ids(self) -> list[str]:
        ids = []
        for path in sorted(self.directory.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["kind"] == "reject":
                    ids.append(entry["_id"])
        return ids


class FlowCase(unittest.TestCase):
    DOCS = documents(1500, seed=5)
    PLAN, KEY_TYPE = plan_for(DOCS, "deep")

    def setUp(self):
        for target in ("core.transfer.get_logger", "core.logutil.get_logger"):
            patcher = mock.patch(target, return_value=QUIET)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.flow = Flow(self.DOCS, self.PLAN, self.KEY_TYPE, Path(tmp.name))

    def stop_after(self, documents_done: int):
        server = self.flow.server
        return lambda: server.checkpoint is not None and server.checkpoint["docs_done"] >= documents_done


class LoadTests(FlowCase):
    def test_first_load(self):
        run, stats, _ = self.flow.run()
        self.assertEqual((run.mode, run.first_load), ("full", True))
        self.assertEqual(self.flow.state(), self.flow.expected())
        self.assertEqual(stats.written, len(self.DOCS))
        self.assertTrue(stats.clean)
        point = self.flow.server.checkpoint
        self.assertEqual(point["docs_done"], len(self.DOCS))
        self.assertEqual(point["last_id"], self.flow.store.entries[-1].id)

    def test_stop_then_resume_writes_every_document_once(self):
        for prefetch in (True, False):
            with self.subTest(prefetch=prefetch):
                self.setUp()
                _, first, _ = self.flow.run(should_stop=self.stop_after(500), prefetch=prefetch)
                self.assertTrue(first.stopped)
                self.assertLess(len(self.flow.server.root_keys()), len(self.DOCS))
                run, second, _ = self.flow.run(prefetch=prefetch)
                self.assertTrue(run.resume)
                self.assertTrue(run.first_load)
                self.assertEqual(self.flow.state(), self.flow.expected())
                self.assertEqual(self.flow.reject_ids(), [])  # a repeat would be a duplicate key
                self.assertEqual(first.written + second.written, len(self.DOCS))
                self.assertEqual(self.flow.server.checkpoint["docs_written"], len(self.DOCS))

    def test_faults_on_both_sides_and_poison_documents(self):
        flow = self.flow
        entries = flow.store.entries
        bad_value = entries[700].doc["customerId"]
        flow.server.poison.add(bad_value)
        # Sorts right after entry 300 (those ids are a timestamp and zeros).
        broken = ObjectId.from_datetime(entries[300].id.generation_time + timedelta(seconds=1))
        flow.store.add_raw(broken, fakes.undecodable(broken))
        flow.store.faults = {
            100: errors.AutoReconnect("connection reset"),
            401: errors.CursorNotFound("cursor id 9 not found", 43),
            950: errors.NotPrimaryError("stepdown"),
        }
        flow.server.commit_faults = {3: "after", 6: "before", 9: "after"}
        flow.server.insert_faults = {20: fakes.deadlock(), 41: fakes.link_failure()}
        _, stats, _ = flow.run()
        poisoned = {entry.id for entry in entries if entry.doc is not None and entry.doc["customerId"] == bad_value}
        self.assertEqual(flow.state(), flow.expected(skip=poisoned))
        self.assertEqual(sorted(flow.reject_ids()), sorted(cp.encode_id(v)[0] for v in {*poisoned, broken}))
        self.assertEqual(stats.rejected, len(poisoned) + 1)
        self.assertFalse(stats.clean)
        self.assertGreaterEqual(stats.retries, 8)
        self.assertEqual(flow.store.faults, {})
        self.assertEqual(flow.server.commit_faults, {})
        self.assertEqual(flow.server.insert_faults, {})

    def test_resume_survives_a_fresh_sample(self):
        # The next run profiles another random sample: other widths, same load.
        self.flow.run(should_stop=self.stop_after(500))
        drifted = copy.deepcopy(self.PLAN)
        for column in drifted["root"]["columns"]:
            if column["sql_type"].startswith("NVARCHAR(") and column["name"] != "mongo_id":
                column["sql_type"] = "NVARCHAR(64)"
        run, stats, _ = self.flow.run(plan=drifted)
        self.assertTrue(run.resume)
        self.assertEqual(self.flow.state(), self.flow.expected())
        self.assertEqual(self.flow.reject_ids(), [])

    def test_restart_replaces_what_is_there(self):
        self.flow.run(should_stop=self.stop_after(400))
        run, _, _ = self.flow.run(restart=True)
        self.assertEqual((run.mode, run.resume, run.first_load), ("full", False, False))
        self.assertEqual(self.flow.state(), self.flow.expected())
        self.assertEqual(self.flow.reject_ids(), [])


class IncrementalTests(FlowCase):
    def test_new_and_late_documents_are_picked_up(self):
        flow = self.flow
        flow.run()
        last = flow.store.entries[-1].id
        # A client whose clock is five minutes behind, and fresh inserts.
        late = ObjectId.from_datetime(last.generation_time - timedelta(minutes=5))
        new_docs = documents(40, seed=99)
        for index, doc in enumerate(new_docs):
            doc["_id"] = late if index == 0 else ObjectId()
            flow.store.add(doc)
        run, stats, _ = flow.run("auto")
        self.assertEqual(run.mode, "incremental")
        self.assertEqual(flow.state(), flow.expected())
        self.assertEqual(flow.reject_ids(), [])
        self.assertGreaterEqual(stats.written, 40)

    def test_nothing_new_rewrites_only_the_overlap(self):
        flow = self.flow
        flow.run()
        before = flow.state()
        _, stats, _ = flow.run("incremental")
        self.assertEqual(flow.state(), before)
        self.assertLess(stats.written, len(self.DOCS))


class MixedIdTests(FlowCase):
    def setUp(self):
        super().setUp()
        docs = documents(600, seed=21)
        for index, doc in enumerate(docs):
            family = index % 3
            doc["_id"] = index * 7 if family == 0 else (f"id-{index:05d}" if family == 1 else doc["_id"])
        plan, key_type = plan_for(docs, "deep")
        self.flow = Flow(docs, plan, key_type, self.flow.directory)

    def test_resume_across_type_boundaries(self):
        flow = self.flow
        self.assertTrue(flow.key_type.upper().startswith("NVARCHAR"))
        # Stops land in the numbers, then in the strings.
        flow.run(should_stop=self.stop_after(150), batch_docs=50)
        flow.run(should_stop=self.stop_after(330), batch_docs=50)
        run, _, _ = flow.run(batch_docs=50)
        self.assertTrue(run.resume)
        self.assertEqual(flow.state(), flow.expected())
        self.assertEqual(flow.reject_ids(), [])


if __name__ == "__main__":
    unittest.main()
