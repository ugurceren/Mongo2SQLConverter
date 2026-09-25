"""Profiling a date range samples through the index instead of reading the whole range."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from core.mongo import MongoClientWrapper
from tests import fakes

START = datetime(2025, 1, 1)


class FakeWrapper(MongoClientWrapper):
    def __init__(self, store):
        super().__init__("mongodb://fake", "app")
        self._collection = fakes.FakeCollection(store)

    def collection(self, name):
        return self._collection


def store_with(count: int, *, indexed: bool) -> fakes.MongoStore:
    store = fakes.MongoStore()
    for i in range(count):
        store.add({"_id": ObjectId.from_datetime(START + timedelta(minutes=i)), "createdAt": START + timedelta(minutes=i * 10)})
    store.indexes = ("createdAt",) if indexed else ()
    return store


class SpreadSampleTests(unittest.TestCase):
    def test_indexed_range_reads_about_the_sample_and_spans_the_period(self):
        store = store_with(5000, indexed=True)
        lower = datetime(2025, 1, 10, tzinfo=timezone.utc)
        upper = datetime(2025, 1, 30, tzinfo=timezone.utc)
        docs = list(FakeWrapper(store).iter_documents("c", sample=300, query={"createdAt": {"$gte": lower, "$lt": upper}}))
        self.assertEqual(len(docs), 300)
        self.assertEqual(len({doc["_id"] for doc in docs}), 300)
        dates = sorted(doc["createdAt"] for doc in docs)
        self.assertTrue(all(lower.replace(tzinfo=None) <= d < upper.replace(tzinfo=None) for d in dates))
        self.assertLess(dates[0] - lower.replace(tzinfo=None), timedelta(days=1))
        self.assertLess(upper.replace(tzinfo=None) - dates[-1], timedelta(days=1))
        self.assertLessEqual(store.returned, 64 * 5)  # sample-sized reads, not the ~2880 in range
        self.assertFalse(getattr(store, "aggregations", None))

    def test_open_ended_range_uses_the_index_bounds(self):
        store = store_with(2000, indexed=True)
        docs = list(FakeWrapper(store).iter_documents("c", sample=100, query={"createdAt": {"$gte": datetime(2025, 1, 5)}}))
        self.assertEqual(len(docs), 100)
        self.assertFalse(getattr(store, "aggregations", None))

    def test_small_range_returns_what_is_there(self):
        store = store_with(2000, indexed=True)
        query = {"createdAt": {"$gte": START, "$lt": START + timedelta(minutes=95)}}
        docs = list(FakeWrapper(store).iter_documents("c", sample=500, query=query))
        self.assertEqual(len(docs), 10)

    def test_without_an_index_the_old_sample_is_kept(self):
        store = store_with(100, indexed=False)
        list(FakeWrapper(store).iter_documents("c", sample=50, query={"createdAt": {"$gte": START}}))
        self.assertEqual(store.aggregations[0][-1], {"$sample": {"size": 50}})

    def test_other_queries_and_no_query_use_sample(self):
        store = store_with(100, indexed=True)
        wrapper = FakeWrapper(store)
        list(wrapper.iter_documents("c", sample=50))
        list(wrapper.iter_documents("c", sample=50, query={"createdAt": {"$gte": START}, "x": 1}))
        self.assertEqual(len(store.aggregations), 2)


if __name__ == "__main__":
    unittest.main()
