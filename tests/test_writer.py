"""Batch writer: all-or-nothing batches, bisection, retries and lost commit replies."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import bson
import pyodbc

from core import checkpoint as cp
from core.convert import Flattener
from core.reader import SourceDoc
from core.rejects import RejectLog
from core.retry import Retrier, RetryPolicy, Stopped
from core.transfer import _unit
from core.writer import BatchWriter, TooManyRejects
from tests import fakes
from tests.fixtures import documents, plan_for

DOCS = documents(240, seed=11)
PLAN, KEY_TYPE = plan_for(DOCS, "deep")
ROOT = PLAN["root"]["table"]
MESSAGES = "ConversationsMessages"


def child_rows(units) -> dict[str, int]:
    out: dict[str, int] = {}
    for unit in units:
        if unit.reject is None:
            for table, rows in unit.children.items():
                out[table] = out.get(table, 0) + len(rows)
    return out


class WriterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = fakes.FakeServer(PLAN)
        self.server.begin_run("run-1")
        self.ops = fakes.FakeOps(self.server)
        self.ops.open()
        self.ops.run_id = "run-1"
        self.flattener = Flattener(PLAN, KEY_TYPE)
        self.rejects = RejectLog("conversations", ROOT, "run-1", directory=Path(self.tmp.name))
        self.addCleanup(self.rejects.close)

    def units(self, docs=DOCS, start=0):
        return [
            _unit(SourceDoc(doc.get("_id"), doc, len(bson.encode(doc))), start + seq, self.flattener)
            for seq, doc in enumerate(docs, 1)
        ]

    def writer(self, *, first_load=True, max_rejects=1000, burst_limit=50, should_stop=None, ops=None):
        retrier = Retrier("sql", RetryPolicy(), sleep=lambda seconds: None, should_stop=should_stop)
        return BatchWriter(
            ops or self.ops,
            PLAN,
            self.flattener.columns,
            first_load=first_load,
            rejects=self.rejects,
            retrier=retrier,
            max_rejects=max_rejects,
            burst_limit=burst_limit,
        )

    def write(self, writer, units, size=60):
        for start in range(0, len(units), size):
            writer.write(units[start : start + size])

    def reject_lines(self):
        self.rejects.close()
        if not self.rejects.path.exists():
            return []
        return [json.loads(line) for line in self.rejects.path.read_text(encoding="utf-8").splitlines()]

    def assert_all_written(self, units, missing=()):
        live = [unit for unit in units if unit.reject is None and unit.id not in missing]
        self.assertEqual(sorted(self.server.root_keys()), sorted(unit.key for unit in live))
        expected = child_rows(live)
        for table, count in expected.items():
            self.assertEqual(len(self.server.tables[table]), count, table)
        point = self.server.checkpoint
        self.assertEqual(point["docs_done"], units[-1].seq)
        self.assertEqual(point["docs_written"], len(live))


class CleanWriteTests(WriterCase):
    def test_batches_land_with_their_checkpoint(self):
        units = self.units()
        writer = self.writer()
        self.write(writer, units)
        self.assert_all_written(units)
        self.assertEqual(writer.stats.documents, len(units))
        self.assertEqual(self.server.checkpoint["last_id"], units[-1].id)
        self.assertEqual(self.reject_lines(), [])

    def test_resync_replaces_rows_and_their_children(self):
        units = self.units()
        self.write(self.writer(), units)
        changed = [dict(doc) for doc in DOCS]
        for doc in changed:
            doc["messages"] = doc["messages"][:1]
        again = self.units(changed, start=len(units))
        self.write(self.writer(first_load=False), again)
        self.assertEqual(len(self.server.root_keys()), len(units))
        self.assertEqual(len(self.server.tables[MESSAGES]), sum(1 for doc in changed if doc["messages"]))
        self.assertEqual(self.reject_lines(), [])


class PoisonTests(WriterCase):
    def poison_root(self, unit):
        column = self.flattener.columns[(ROOT, "customerId")]
        self.server.poison.add(unit.root[column.position])

    def test_bad_root_value_rejects_one_document(self):
        units = self.units()
        bad = units[37]
        self.poison_root(bad)
        writer = self.writer()
        self.write(writer, units, size=240)
        self.assert_all_written(units, missing={bad.id})
        lines = self.reject_lines()
        self.assertEqual([(line["kind"], line["stage"]) for line in lines], [("reject", "sql")])
        self.assertEqual(lines[0]["_id"], cp.encode_id(bad.id)[0])
        self.assertEqual(lines[0]["sqlstate"], "22003")
        self.assertEqual(self.server.checkpoint["rejected"], 1)

    def test_bad_child_value_rejects_the_whole_document(self):
        units = self.units()
        bad = next(unit for unit in units[100:] if unit.children.get(MESSAGES))
        text_at = self.server.columns[MESSAGES].index("text")
        self.server.poison.add(bad.children[MESSAGES][0][text_at])
        self.write(self.writer(), units)
        self.assert_all_written(units, missing={bad.id})
        self.assertNotIn(bad.key, {row[0] for row in self.server.tables[MESSAGES].values()})
        self.assertEqual(len(self.reject_lines()), 1)

    def test_flatten_and_decode_rejects_move_the_checkpoint(self):
        docs = [dict(doc) for doc in DOCS[:50]]
        del docs[10]["_id"]
        units = self.units(docs)
        units[20] = _unit(SourceDoc(units[20].id, None, 10, error="InvalidBSON: bad"), units[20].seq, self.flattener)
        self.write(self.writer(), units, size=25)
        self.assertEqual(len(self.server.root_keys()), 48)
        self.assertEqual(self.server.checkpoint["docs_done"], 50)
        self.assertEqual(self.server.checkpoint["rejected"], 2)
        kinds = sorted((line["stage"], line["detail"]) for line in self.reject_lines())
        self.assertEqual(kinds, [("decode", "decode_error"), ("flatten", "key_missing")])

    def test_reject_limit_stops_the_job(self):
        units = self.units()
        for unit in units[:5]:
            self.poison_root(unit)
        with self.assertRaises(TooManyRejects):
            self.write(self.writer(max_rejects=3), units)

    def test_systematic_errors_stop_at_the_burst_limit(self):
        units = self.units()
        for unit in units[:30]:
            self.poison_root(unit)
        with self.assertRaises(TooManyRejects):
            self.write(self.writer(burst_limit=5), units, size=240)


class RetryTests(WriterCase):
    def test_lost_commit_reply_is_not_written_twice(self):
        self.server.commit_faults = {2: "after", 4: "after"}
        units = self.units()
        self.write(self.writer(), units)
        self.assert_all_written(units)
        self.assertEqual(self.reject_lines(), [])  # a second insert would have been a duplicate key
        self.assertGreaterEqual(self.ops.reconnects, 2)

    def test_connection_lost_before_commit_writes_again(self):
        self.server.commit_faults = {1: "before", 3: "before"}
        units = self.units()
        self.write(self.writer(), units)
        self.assert_all_written(units)
        self.assertEqual(self.reject_lines(), [])

    def test_deadlock_and_full_log_are_waited_out(self):
        self.server.insert_faults = {2: fakes.deadlock(), 9: fakes.log_full(), 15: fakes.link_failure()}
        units = self.units()
        writer = self.writer()
        self.write(writer, units)
        self.assert_all_written(units)
        self.assertEqual(writer.retrier.total_retries, 3)

    def test_configuration_errors_stop_at_once(self):
        self.server.insert_faults = {1: fakes.missing_table()}
        with self.assertRaises(pyodbc.ProgrammingError):
            self.writer().write(self.units()[:10])
        self.assertEqual(self.server.root_keys(), [])
        self.assertEqual(self.server.checkpoint["docs_done"], 0)

    def test_bind_errors_use_the_slow_path(self):
        self.server.bind_fails = {MESSAGES}
        units = self.units()
        writer = self.writer()
        self.write(writer, units, size=40)
        self.assert_all_written(units)
        self.assertIn(MESSAGES, writer.stats.slow_tables)
        self.assertIn(MESSAGES, self.ops.slow_inserts)

    def test_taken_over_checkpoint_stops_writing(self):
        units = self.units()
        writer = self.writer()
        writer.write(units[:50])
        self.server.checkpoint["run_id"] = "someone-else"
        with self.assertRaises(cp.TakenOver):
            writer.write(units[50:100])
        self.assertEqual(len(self.server.root_keys()), 50)

    def test_stop_during_a_retry_wait(self):
        self.server.insert_faults = {1: fakes.link_failure()}
        writer = self.writer(should_stop=lambda: True)
        with self.assertRaises(Stopped):
            writer.write(self.units()[:10])
        self.assertEqual(self.server.root_keys(), [])


if __name__ == "__main__":
    unittest.main()
