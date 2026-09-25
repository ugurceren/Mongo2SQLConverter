"""The chunked reader: `_id` order across types, restarts, poison documents."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from bson import Int64, ObjectId
from pymongo import errors

from core.checkpoint import MISSING
from core.reader import ChunkedReader, first_id, projection_for, same_id
from core.retry import GiveUp, Retrier, RetryPolicy
from tests import fakes
from tests.fixtures import documents, plan_for

START = datetime(2025, 1, 1)


def mixed_ids() -> list:
    """Every `_id` family the reader may meet, deliberately inserted out of order."""
    return [
        ObjectId("65f000000000000000000003"),
        "b",
        7,
        datetime(2024, 5, 1),
        2.5,
        "a",
        ObjectId("65f000000000000000000001"),
        Int64(3),
        -1,
        "ab",
        True,
        datetime(2023, 1, 1),
        ObjectId("65f000000000000000000002"),
        "Z",
        10**12,
    ]


def store_with(ids) -> fakes.MongoStore:
    store = fakes.MongoStore()
    for index, value in enumerate(ids):
        store.add({"_id": value, "n": index, "createdAt": START + timedelta(hours=index), "extra": "x" * 10})
    return store


def retrier(**policy) -> Retrier:
    return Retrier("mongo", RetryPolicy(**policy), sleep=lambda seconds: None)


def read_all(reader) -> list:
    return [item for item in reader]


class OrderTests(unittest.TestCase):
    def test_mixed_types_come_back_in_bson_order(self):
        store = store_with(mixed_ids())
        expected = [entry.id for entry in store.entries]
        for chunk in (1, 2, 4, 50):
            with self.subTest(chunk=chunk):
                items = read_all(ChunkedReader(fakes.FakeCollection(store), chunk=chunk))
                self.assertEqual([fakes.bson_key(item.id) for item in items], [fakes.bson_key(v) for v in expected])

    def test_gt_would_stop_at_the_end_of_the_first_type(self):
        # Why the reader uses index bounds: `$gt` only matches the same BSON type.
        store = store_with(mixed_ids())
        col = fakes.FakeCollection(store)
        by_gt = list(col.find({"_id": {"$gt": 2.5}}))
        self.assertTrue(all(isinstance(doc["_id"], (int, float)) for doc in by_gt))
        after = [item.id for item in ChunkedReader(col, start_after=2.5, chunk=3)]
        self.assertIn("a", after)
        self.assertIn(ObjectId("65f000000000000000000001"), after)

    def test_start_after_is_exclusive_for_every_type(self):
        store = store_with(mixed_ids())
        ids = [entry.id for entry in store.entries]
        for position, value in enumerate(ids):
            with self.subTest(value=value):
                items = read_all(ChunkedReader(fakes.FakeCollection(store), start_after=value, chunk=4))
                self.assertEqual(len(items), len(ids) - position - 1)
                if items:
                    self.assertTrue(same_id(items[0].id, ids[position + 1]))

    def test_numbers_equal_across_types_are_one_position(self):
        store = store_with([1, 2, 3])
        items = read_all(ChunkedReader(fakes.FakeCollection(store), start_after=2.0))
        self.assertEqual([item.id for item in items], [3])


class RestartTests(unittest.TestCase):
    def test_transient_errors_resume_exactly(self):
        store = store_with(mixed_ids())
        store.faults = {
            3: errors.AutoReconnect("connection reset"),
            8: errors.CursorNotFound("cursor id 7 not found", 43),
            9: errors.NotPrimaryError("not primary"),
        }
        reader = ChunkedReader(fakes.FakeCollection(store), chunk=4, retrier=retrier())
        items = read_all(reader)
        self.assertEqual([fakes.bson_key(i.id) for i in items], [e.key for e in store.entries])
        self.assertEqual(store.faults, {})
        self.assertEqual(reader.retrier.total_retries, 3)

    def test_without_retrier_the_error_surfaces(self):
        store = store_with(mixed_ids())
        store.faults = {2: errors.AutoReconnect("reset")}
        with self.assertRaises(errors.AutoReconnect):
            read_all(ChunkedReader(fakes.FakeCollection(store), chunk=4))

    def test_permanent_errors_are_not_retried(self):
        store = store_with(mixed_ids())
        store.faults = {2: errors.OperationFailure("not authorized", 13)}
        with self.assertRaises(errors.OperationFailure):
            read_all(ChunkedReader(fakes.FakeCollection(store), chunk=4, retrier=retrier()))

    def test_repeated_timeouts_give_up(self):
        store = store_with(mixed_ids())
        store.faults = {n: errors.ExecutionTimeout("slow", 50) for n in range(2, 7)}
        with self.assertRaises(GiveUp):
            read_all(ChunkedReader(fakes.FakeCollection(store), chunk=4, retrier=retrier(timeout_attempts=2)))

    def test_stop_request(self):
        store = store_with(mixed_ids())
        seen = []
        reader = ChunkedReader(fakes.FakeCollection(store), chunk=2, should_stop=lambda: len(seen) >= 4)
        for item in reader:
            seen.append(item)
        self.assertLess(len(seen), len(store.entries))


class PoisonTests(unittest.TestCase):
    def test_undecodable_document_becomes_one_reject(self):
        store = store_with([1, 2, 4, 5])
        store.add_raw(3, fakes.undecodable(3))
        items = read_all(ChunkedReader(fakes.FakeCollection(store), chunk=2, retrier=retrier()))
        self.assertEqual([item.id for item in items], [1, 2, 3, 4, 5])
        bad = items[2]
        self.assertIsNone(bad.doc)
        self.assertIn("InvalidBSON", bad.error)
        self.assertTrue(all(item.doc is not None for i, item in enumerate(items) if i != 2))

    def test_first_id_reads_every_simple_type(self):
        for value in (ObjectId(), 5, Int64(2**40), 2.5, "text", datetime(2024, 1, 1), True):
            with self.subTest(value=value):
                self.assertTrue(same_id(first_id(fakes.undecodable(value)), value))

    def test_id_not_first_asks_the_index(self):
        store = store_with([1, 2])
        raw = fakes.undecodable(9)
        # Move `_id` behind another element: first_id cannot see it.
        import bson

        body = bson.encode({"a": 1})[4:-1] + fakes.undecodable(9)[4:-1]
        moved = (len(body) + 5).to_bytes(4, "little") + body + b"\x00"
        self.assertIs(first_id(moved), MISSING)
        store.add_raw(9, moved)
        items = read_all(ChunkedReader(fakes.FakeCollection(store), chunk=5))
        self.assertEqual([item.id for item in items], [1, 2, 9])
        self.assertIsNone(items[-1].doc)
        del raw


class RangeTests(unittest.TestCase):
    def test_date_filter_walks_the_id_index_in_ranges(self):
        store = store_with(list(range(40)))
        query = {"createdAt": {"$gte": START + timedelta(hours=10), "$lt": START + timedelta(hours=30)}}
        store.faults = {5: errors.AutoReconnect("reset")}
        reader = ChunkedReader(fakes.FakeCollection(store), query=query, step=7, retrier=retrier())
        ids = [item.id for item in reader]
        self.assertEqual(ids, list(range(10, 30)))

    def test_tiny_settings_still_move_forward(self):
        store = store_with(list(range(12)))
        query = {"createdAt": {"$gte": START}}
        self.assertEqual([i.id for i in ChunkedReader(fakes.FakeCollection(store), query=query, step=0)], list(range(12)))
        self.assertEqual([i.id for i in ChunkedReader(fakes.FakeCollection(store), chunk=0)], list(range(12)))

    def test_range_resume_after_a_position(self):
        store = store_with(list(range(40)))
        query = {"createdAt": {"$gte": START + timedelta(hours=10), "$lt": START + timedelta(hours=30)}}
        reader = ChunkedReader(fakes.FakeCollection(store), query=query, step=6, start_after=17)
        self.assertEqual([item.id for item in reader], list(range(18, 30)))

    def test_projection_keeps_only_planned_fields(self):
        docs = documents(30)
        plan, _ = plan_for(docs, "hybrid")
        projection = projection_for(plan)
        self.assertIn("_id", projection)
        self.assertIn("messages", projection)
        store = fakes.MongoStore()
        for doc in docs:
            store.add({**doc, "unplanned": "x" * 100})
        items = read_all(ChunkedReader(fakes.FakeCollection(store), projection=projection))
        self.assertEqual(len(items), 30)
        self.assertTrue(all("unplanned" not in item.doc for item in items))

    def test_operator_paths_disable_projection(self):
        plan = {"root": {"columns": [{"path": "$weird"}]}, "children": []}
        self.assertIsNone(projection_for(plan))


if __name__ == "__main__":
    unittest.main()


def dated_store(count: int, *, indexed: bool = True, same_day: int = 0) -> fakes.MongoStore:
    """Dates out of `_id` order, so reading by date and by `_id` differ."""
    store = fakes.MongoStore()
    for i in range(count):
        hour = 0 if i < same_day else (i * 37) % count
        store.add({"_id": i, "createdAt": START + timedelta(hours=hour), "n": i})
    store.indexes = ("createdAt",) if indexed else ()
    return store


def in_range(store, lower, upper):
    return sorted(e.id for e in store.entries if lower <= e.doc["createdAt"] < upper)


class DateWindowTests(unittest.TestCase):
    LOWER = START + timedelta(hours=30)
    UPPER = START + timedelta(hours=150)

    def query(self, lower=None, upper=None):
        from datetime import timezone

        bounds = {"$gte": (lower or self.LOWER).replace(tzinfo=timezone.utc)}
        bounds["$lt"] = (upper or self.UPPER).replace(tzinfo=timezone.utc)
        return {"createdAt": bounds}

    def test_reads_the_range_through_the_date_index_once(self):
        store = dated_store(200)
        reader = ChunkedReader(fakes.FakeCollection(store), query=self.query(), window_docs=7)
        items = read_all(reader)
        self.assertEqual(sorted(item.id for item in items), in_range(store, self.LOWER, self.UPPER))
        self.assertTrue(all(item.position and set(item.position) == {"window"} for item in items))
        self.assertIn("createdAt_1", store.hints)
        self.assertEqual(store.delivered, len(items))  # nothing outside the range came back

    def test_transient_errors_mid_window_repeat_nothing(self):
        store = dated_store(200)
        store.faults = {5: errors.AutoReconnect("reset"), 61: errors.CursorNotFound("gone", 43)}
        items = read_all(ChunkedReader(fakes.FakeCollection(store), query=self.query(), window_docs=25, retrier=retrier()))
        ids = [item.id for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids), in_range(store, self.LOWER, self.UPPER))

    def test_one_date_bigger_than_a_window(self):
        store = dated_store(200, same_day=60)
        items = read_all(ChunkedReader(fakes.FakeCollection(store), query=self.query(START, self.UPPER), window_docs=10))
        self.assertEqual(sorted(i.id for i in items), in_range(store, START, self.UPPER))

    def test_resume_starts_at_the_saved_window(self):
        from core.checkpoint import window

        store = dated_store(200)
        restart = START + timedelta(hours=90)
        items = read_all(ChunkedReader(fakes.FakeCollection(store), query=self.query(), start_after=window(restart)))
        self.assertEqual(sorted(i.id for i in items), in_range(store, restart, self.UPPER))

    def test_without_index_or_after_an_id_the_id_walk_stays(self):
        plain = dated_store(200, indexed=False)
        items = read_all(ChunkedReader(fakes.FakeCollection(plain), query=self.query()))
        self.assertTrue(all(item.position is None for item in items))
        indexed = dated_store(200)
        items = read_all(ChunkedReader(fakes.FakeCollection(indexed), query=self.query(), start_after=100))
        self.assertTrue(all(item.position is None and item.id > 100 for item in items))
