"""Where a run starts, and resume points that survive every `_id` type."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import bson
from bson import Binary, Decimal128, Int64, MaxKey, MinKey, ObjectId, Timestamp
from bson.binary import UuidRepresentation

from core import checkpoint as cp
from core.checkpoint import MISSING, Checkpoint, plan_run
from tests.fixtures import documents, plan_for

PLAN = "p" * 64
FILTER = "f" * 64


def point(**changes) -> Checkpoint:
    values = dict(
        target_table="Conversations",
        collection="conversations",
        mongo_database="app",
        run_id="r" * 32,
        mode="full",
        status="running",
        first_load=True,
        plan_hash=PLAN,
        filter_hash=FILTER,
        last_id_json=cp.encode_id(ObjectId("65f000000000000000000abc"))[0],
        last_id_type="objectid",
        docs_done=1234,
        docs_written=1200,
        rows_written=9000,
        rejected=34,
        clipped=2,
        resume_count=1,
    )
    values.update(changes)
    return Checkpoint(**values)


def decide(requested="auto", *, checkpoint=None, restart=False, tables_empty=False, root_empty=False,
           key_type="CHAR(24)", legacy_max=None, yaml_mark=MISSING, overlap=15, plan_hash=PLAN, filter_hash=FILTER):
    return plan_run(
        requested,
        checkpoint=checkpoint,
        restart=restart,
        plan_hash_value=plan_hash,
        filter_hash_value=filter_hash,
        tables_empty=tables_empty,
        root_empty=root_empty,
        key_type=key_type,
        legacy_max=legacy_max,
        yaml_mark=yaml_mark,
        overlap_minutes=overlap,
    )


class PlanRunTests(unittest.TestCase):
    def test_unfinished_full_load_resumes_with_its_counters(self):
        for requested in ("auto", "full", "incremental"):
            with self.subTest(requested=requested):
                run = decide(requested, checkpoint=point())
                self.assertEqual(run.mode, "full")
                self.assertTrue(run.resume)
                self.assertTrue(run.first_load)
                self.assertEqual(run.start_after, ObjectId("65f000000000000000000abc"))
                self.assertEqual(run.docs_done, 1234)
                self.assertEqual(run.resume_count, 2)
                self.assertEqual(run.carried, {"docs_written": 1200, "rows_written": 9000, "rejected": 34, "clipped": 2})

    def test_restart_ignores_the_checkpoint(self):
        run = decide("auto", checkpoint=point(), restart=True, tables_empty=False)
        self.assertEqual((run.mode, run.resume, run.first_load), ("full", False, False))
        self.assertIs(run.start_after, MISSING)
        self.assertTrue(decide("auto", checkpoint=point(), restart=True, tables_empty=True).first_load)

    def test_changed_plan_or_filter_does_not_resume(self):
        for changes in ({"plan_hash": "x" * 64}, {"filter_hash": "y" * 64}, {"status": "completed"}):
            with self.subTest(changes=changes):
                full = decide("full", checkpoint=point(**changes), tables_empty=False)
                self.assertEqual((full.mode, full.resume, full.first_load), ("full", False, False))
                incremental = decide("auto", checkpoint=point(**changes))
                self.assertEqual(incremental.mode, "incremental")
                self.assertFalse(incremental.resume)

    def test_incremental_goes_back_by_the_overlap_for_object_ids(self):
        last = ObjectId.from_datetime(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))
        done = point(status="completed", last_id_json=cp.encode_id(last)[0])
        run = decide("auto", checkpoint=done, overlap=15)
        self.assertEqual(run.mode, "incremental")
        self.assertEqual(run.start_after.generation_time, datetime(2026, 3, 1, 11, 45, tzinfo=timezone.utc))
        self.assertEqual(decide("auto", checkpoint=done, overlap=0).start_after, last)

    def test_other_id_types_continue_exactly(self):
        for value in (41, "000123", 2.5):
            with self.subTest(value=value):
                done = point(status="completed", last_id_json=cp.encode_id(value)[0])
                run = decide("incremental", checkpoint=done)
                self.assertEqual(run.start_after, value)
                self.assertIs(type(run.start_after), type(value))

    def test_empty_root_is_a_first_load(self):
        run = decide("auto", root_empty=True, tables_empty=True)
        self.assertEqual((run.mode, run.first_load), ("full", True))

    def test_legacy_tables_without_checkpoint(self):
        oid = "65f0000000000000000000ab"
        run = decide("auto", legacy_max=oid, key_type="CHAR(24)", overlap=0)
        self.assertEqual((run.mode, run.start_after), ("incremental", ObjectId(oid)))
        run = decide("auto", legacy_max=987, key_type="BIGINT")
        self.assertEqual((run.mode, run.start_after), ("incremental", 987))
        # A text key's MAX is compared under the SQL collation: not trusted.
        run = decide("auto", legacy_max="zzz", key_type="NVARCHAR(450)", yaml_mark="abc")
        self.assertEqual((run.mode, run.start_after), ("incremental", "abc"))
        run = decide("auto", legacy_max="zzz", key_type="NVARCHAR(450)")
        self.assertEqual((run.mode, run.first_load), ("full", False))

    def test_requested_full_without_checkpoint(self):
        run = decide("full", tables_empty=True, root_empty=True)
        self.assertEqual((run.mode, run.first_load, run.resume), ("full", True, False))
        run = decide("full", tables_empty=False)
        self.assertEqual((run.mode, run.first_load), ("full", False))


class IdRoundTripTests(unittest.TestCase):
    VALUES = [
        ObjectId(),
        0,
        -5,
        2**31 - 1,
        Int64(2**53 + 1),
        2.5,
        -0.0,
        Decimal128(Decimal("1.10")),
        "",
        "000123",
        "Türkçe 😀",
        datetime(2024, 2, 29, 23, 59, 59, 999000),
        Binary(b"\x00\x01\x02", 0),
        Binary.from_uuid(uuid.UUID(int=7), UuidRepresentation.STANDARD),
        {"a": 1, "b": [1, "x"]},
        [3, 4],
        True,
        Timestamp(1700000000, 3),
        MinKey(),
        MaxKey(),
    ]

    def test_every_type_comes_back_identical(self):
        # Identical as BSON, which is what the reader's index bound needs:
        # binary subtype 0 comes back as `bytes`, as pymongo decodes it too.
        for value in self.VALUES:
            with self.subTest(value=value):
                text, kind = cp.encode_id(value)
                back = cp.decode_id(text)
                self.assertEqual(bson.encode({"_id": back}), bson.encode({"_id": value}))
                self.assertTrue(kind)

    def test_string_and_number_stay_apart(self):
        self.assertNotEqual(cp.encode_id("123")[0], cp.encode_id(123)[0])
        self.assertIsInstance(cp.decode_id(cp.encode_id("123")[0]), str)


class HashTests(unittest.TestCase):
    def test_plan_hash_follows_the_load_not_the_sample(self):
        docs = documents(400)
        plan, _ = plan_for(docs, "hybrid")
        # Another random sample: other widths, one column fewer, one child table fewer.
        drifted, _ = plan_for(docs[:60], "hybrid")
        drifted["root"]["columns"] = drifted["root"]["columns"][:-1]
        drifted["children"] = drifted["children"][:-1]
        self.assertEqual(cp.plan_hash(plan), cp.plan_hash(drifted))
        other, _ = plan_for(docs, "deep")
        self.assertNotEqual(cp.plan_hash(plan), cp.plan_hash(other))
        renamed = {**plan, "root": {**plan["root"], "table": "Other"}}
        self.assertNotEqual(cp.plan_hash(plan), cp.plan_hash(renamed))

    def test_filter_hash(self):
        start = datetime(2025, 1, 1)
        self.assertEqual(cp.filter_hash(None), cp.filter_hash({}))
        self.assertNotEqual(
            cp.filter_hash({"createdAt": {"$gte": start}}),
            cp.filter_hash({"createdAt": {"$gte": start + timedelta(days=1)}}),
        )


if __name__ == "__main__":
    unittest.main()
