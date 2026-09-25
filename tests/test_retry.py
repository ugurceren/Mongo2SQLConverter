"""Error classes decide between waiting, bisecting and stopping."""

from __future__ import annotations

import unittest

import pyodbc
from pymongo import errors

from core.retry import (
    BIND,
    DATA,
    FATAL,
    RESOURCE,
    TIMEOUT,
    TRANSIENT,
    UNKNOWN,
    GiveUp,
    Retrier,
    RetryPolicy,
    Stopped,
    classify_mongo,
    classify_sql,
    describe,
    sql_codes,
)
from tests import fakes


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class SqlClassificationTests(unittest.TestCase):
    def test_codes_are_read_from_state_and_message(self):
        state, natives = sql_codes(fakes.deadlock())
        self.assertEqual(state, "40001")
        self.assertIn(1205, natives)

    def test_classes(self):
        cases = [
            (fakes.link_failure(), TRANSIENT),
            (fakes.deadlock(), TRANSIENT),
            (pyodbc.Error("HYT00", "[HYT00] [Microsoft][ODBC Driver 17 for SQL Server]Query timeout expired (0) (SQLExecDirectW)"), TRANSIENT),
            (pyodbc.OperationalError("HY000", "[HY000] Lock request time out period exceeded. (1222) (SQLExecDirectW)"), TRANSIENT),
            (fakes.log_full(), RESOURCE),
            (fakes.overflow_error(), DATA),
            (fakes.duplicate_key("T", "x"), DATA),
            (pyodbc.IntegrityError("23000", "[23000] Cannot insert the value NULL into column 'a'. (515) (SQLExecute)"), DATA),
            (pyodbc.ProgrammingError("42000", "[42000] String or binary data would be truncated in table 'x'. (2628) (SQLExecute)"), DATA),
            (fakes.missing_table(), FATAL),
            (pyodbc.ProgrammingError("42000", "[42000] The INSERT permission was denied on the object 'T'. (229) (SQLExecute)"), FATAL),
            (pyodbc.InterfaceError("28000", "[28000] Login failed for user 'x'. (18456) (SQLDriverConnect)"), FATAL),
            (fakes.bind_error(), BIND),
            (pyodbc.Error("HY000", "String data, right truncation: length 802 buffer 800"), BIND),
            (MemoryError(), BIND),
            (pyodbc.Error("HY000", "[HY000] Something odd (50000) (SQLExecute)"), UNKNOWN),
        ]
        for exc, expected in cases:
            with self.subTest(exc=describe(exc)):
                self.assertEqual(classify_sql(exc), expected)

    def test_describe_keeps_codes(self):
        text = describe(fakes.deadlock())
        self.assertIn("sqlstate=40001", text)
        self.assertIn("1205", text)


class MongoClassificationTests(unittest.TestCase):
    def test_classes(self):
        cases = [
            (errors.AutoReconnect("primary stepped down"), TRANSIENT),
            (errors.NetworkTimeout("timed out"), TRANSIENT),
            (errors.NotPrimaryError("not primary"), TRANSIENT),
            (errors.ServerSelectionTimeoutError("no servers"), TRANSIENT),
            (errors.CursorNotFound("cursor id 1 not found", 43), TRANSIENT),
            (errors.ExecutionTimeout("operation exceeded time limit", 50), TIMEOUT),
            (errors.OperationFailure("interrupted at shutdown", 11600), TRANSIENT),
            (errors.OperationFailure("not authorized", 13), FATAL),
            (errors.OperationFailure("hint provided does not correspond to an existing index", 2), FATAL),
            (ValueError("bug"), FATAL),
        ]
        for exc, expected in cases:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(classify_mongo(exc), expected)


class RetrierTests(unittest.TestCase):
    def make(self, policy=None, should_stop=None):
        clock = Clock()
        retrier = Retrier(
            "sql",
            policy or RetryPolicy(),
            sleep=clock.sleep,
            clock=clock,
            rng=lambda: 1.0,
            should_stop=should_stop,
        )
        return retrier, clock

    def test_backoff_grows_to_the_cap_then_gives_up(self):
        retrier, clock = self.make(RetryPolicy(attempts=5, base_seconds=2, cap_seconds=10, incident_seconds=10**6))
        waits = []
        for _ in range(5):
            before = clock.now
            retrier.wait(TRANSIENT, fakes.link_failure())
            waits.append(clock.now - before)
        self.assertEqual(waits, [2, 4, 8, 10, 10])
        with self.assertRaises(GiveUp):
            retrier.wait(TRANSIENT, fakes.link_failure())
        self.assertEqual(retrier.total_retries, 6)

    def test_reset_starts_a_new_incident(self):
        retrier, _ = self.make(RetryPolicy(attempts=2, incident_seconds=10**6))
        for _ in range(10):
            retrier.wait(TRANSIENT, fakes.link_failure())
            retrier.reset()

    def test_incident_time_limit(self):
        retrier, clock = self.make(RetryPolicy(attempts=100, base_seconds=100, cap_seconds=100, incident_seconds=250))
        retrier.wait(TRANSIENT, fakes.link_failure())
        retrier.wait(TRANSIENT, fakes.link_failure())
        retrier.wait(TRANSIENT, fakes.link_failure())
        with self.assertRaises(GiveUp):
            retrier.wait(TRANSIENT, fakes.link_failure())

    def test_log_full_waits_long_and_fixed(self):
        retrier, clock = self.make(RetryPolicy(resource_seconds=300, resource_total_seconds=900))
        for _ in range(3):
            before = clock.now
            retrier.wait(RESOURCE, fakes.log_full())
            self.assertEqual(clock.now - before, 300)
        with self.assertRaises(GiveUp):
            retrier.wait(RESOURCE, fakes.log_full())

    def test_deadlocks_have_their_own_budget(self):
        retrier, _ = self.make(RetryPolicy(deadlock_budget=3))
        for _ in range(3):
            retrier.wait(TRANSIENT, fakes.deadlock())
            retrier.reset()
        with self.assertRaises(GiveUp):
            retrier.wait(TRANSIENT, fakes.deadlock())

    def test_mongo_timeouts_get_few_tries(self):
        retrier, _ = self.make(RetryPolicy(timeout_attempts=2))
        exc = errors.ExecutionTimeout("slow", 50)
        retrier.wait(TIMEOUT, exc)
        retrier.wait(TIMEOUT, exc)
        with self.assertRaises(GiveUp):
            retrier.wait(TIMEOUT, exc)

    def test_stop_request_interrupts_a_wait(self):
        calls = []

        def stop():
            calls.append(1)
            return len(calls) > 3

        retrier, clock = self.make(RetryPolicy(base_seconds=60, cap_seconds=60), should_stop=stop)
        with self.assertRaises(Stopped):
            retrier.wait(TRANSIENT, fakes.link_failure())
        self.assertLessEqual(clock.now, 4)


if __name__ == "__main__":
    unittest.main()
