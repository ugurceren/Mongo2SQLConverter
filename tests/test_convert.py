"""The compiled flattener gives the reference rows, except where it must not."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bson import Binary, Code, Decimal128, Int64, MinKey, ObjectId, Regex, Timestamp
from bson.datetime_ms import DatetimeMS

from core.convert import Column, Flattener, _Sink, compile_converter
from core.textutil import clip_utf16, utf16_len
from tests import legacy_reference as legacy
from tests.fixtures import NESTINGS, canon, documents, plan_for

SQL_TYPES = (
    "BIT",
    "INT",
    "BIGINT",
    "FLOAT",
    "DECIMAL(38, 6)",
    "DATETIME2(3)",
    "VARBINARY(MAX)",
    "CHAR(24)",
    "NVARCHAR(16)",
    "NVARCHAR(450)",
    "NVARCHAR(MAX)",
)

VALUES = [
    None,
    True,
    False,
    0,
    1,
    -7,
    2**31 - 1,
    -(2**31),
    Int64(2**40),
    Int64(-(2**63)),
    0.0,
    -0.0,
    2.75,
    -1e-300,
    1e20,
    float("nan"),
    float("inf"),
    Decimal128("1234.5"),
    Decimal128("-0.000001"),
    Decimal128("123456789012345678901234567890.1234"),
    "",
    "abc",
    "Türkçe ğüşıöç",
    "😀" * 3,
    "1",
    "yes",
    " TRUE ",
    "12.5",
    "not a number",
    datetime(2025, 5, 1, 10, 30, 0, 123000),
    datetime(2025, 5, 1, 10, 30, tzinfo=timezone.utc),
    datetime(2025, 5, 1, 13, 30, tzinfo=timezone(timedelta(hours=3))),
    datetime(1, 1, 1),
    datetime(9999, 12, 31, 23, 59, 59),
    ObjectId("65f0c2a1b2c3d4e5f6a7b8c9"),
    Binary(b"\x00\x01", 0),
    Binary(b"0123456789abcdef", 4),
    b"bytes",
    Regex("^a", "i"),
    Code("return 1"),
    Timestamp(1700000000, 1),
    MinKey(),
    {"nested": [1, {"x": ObjectId("65f0c2a1b2c3d4e5f6a7b8c9")}]},
    [1, "two", None],
]


def _new(sql_type: str, value):
    sink = _Sink()
    column = Column("T", "c", 0, sql_type)
    return compile_converter(sql_type, column, sink)(value), sink, column


class ConverterMatrixTests(unittest.TestCase):
    """Every value × type pair converts like `coerce`, unless it would fail in SQL."""

    def test_matches_reference(self) -> None:
        for sql_type in SQL_TYPES:
            for value in VALUES:
                with self.subTest(sql_type=sql_type, value=repr(value)[:40]):
                    try:
                        expected, clipped = legacy.coerce(value, sql_type)
                    except (OverflowError, ValueError):
                        # The reference crashes the whole batch here; the new
                        # converter must turn it into NULL with a note.
                        got, sink, _ = _new(sql_type, value)
                        self.assertIsNone(got)
                        self.assertTrue(sink.notes)
                        continue
                    got, sink, column = _new(sql_type, value)
                    if clipped:
                        # Overflow is reported, not clipped.
                        self.assertIn(column, sink.overflow or {})
                        self.assertEqual(canon(clip_utf16(got, column.units)), canon(expected))
                        continue
                    if _sql_would_reject(expected, sql_type):
                        self.assertIsNone(got)
                        self.assertTrue(sink.notes, "the NULL must be reported")
                        continue
                    if isinstance(expected, Decimal) and expected.as_tuple().exponent < -6:
                        self.assertEqual(got, expected.quantize(Decimal("0.000001")))
                        continue
                    self.assertEqual(canon(got), canon(expected))
                    self.assertFalse(sink.overflow)


def _sql_would_reject(value, sql_type: str) -> bool:
    base = sql_type.split("(")[0]
    if base == "INT" and isinstance(value, int) and not isinstance(value, bool):
        return not (-(2**31) <= value <= 2**31 - 1)
    if base == "BIGINT" and isinstance(value, int) and not isinstance(value, bool):
        return not (-(2**63) <= value <= 2**63 - 1)
    if base == "FLOAT" and isinstance(value, float):
        return not math.isfinite(value)
    if base == "DECIMAL" and isinstance(value, Decimal):
        return not value.is_finite() or abs(value) >= Decimal(10) ** 32
    return False


class PoisonValueTests(unittest.TestCase):
    def test_values_sql_server_rejects_become_null_with_a_note(self) -> None:
        cases = [
            ("FLOAT", Decimal128("NaN")),
            ("FLOAT", Decimal128("Infinity")),
            ("FLOAT", 10**400),
            ("DECIMAL(38, 6)", Decimal128("NaN")),
            ("DECIMAL(38, 6)", Decimal128("1E+40")),
            ("DECIMAL(38, 6)", float("nan")),
            ("INT", 2**31),
            ("INT", 1e12),
            ("BIGINT", 2**63),
            ("DATETIME2(3)", DatetimeMS(2**62)),
            ("DATETIME", datetime(1700, 1, 1)),
        ]
        for sql_type, value in cases:
            with self.subTest(sql_type=sql_type, value=repr(value)[:40]):
                got, sink, _ = _new(sql_type, value)
                self.assertIsNone(got)
                self.assertEqual(len(sink.notes or []), 1)
                self.assertIn(sink.notes[0].kind, {"out_of_range", "bad_date"})

    def test_decimal_keeps_scale_six(self) -> None:
        got, _, _ = _new("DECIMAL(38, 6)", Decimal128("1.1234565"))
        self.assertEqual(got, Decimal("1.123457"))
        got, _, _ = _new("DECIMAL(38, 6)", Decimal128("-2.5"))
        self.assertEqual(got, Decimal("-2.5"))


class TextWidthTests(unittest.TestCase):
    def test_astral_boundaries(self) -> None:
        # NVARCHAR(4): "😀😀" is exactly 4 UTF-16 units, one more overflows.
        for text, fits in (("😀😀", True), ("😀😀a", False), ("abcd", True), ("abcde", False), ("😀ab", True)):
            with self.subTest(text=text):
                got, sink, column = _new("NVARCHAR(4)", text)
                self.assertEqual(got, text)
                self.assertEqual(column in (sink.overflow or {}), not fits)
                if not fits:
                    self.assertEqual(sink.overflow[column], utf16_len(text))

    def test_max_never_overflows(self) -> None:
        got, sink, _ = _new("NVARCHAR(MAX)", "x" * 100_000)
        self.assertEqual(len(got), 100_000)
        self.assertFalse(sink.overflow)

    def test_widened_column_stops_reporting(self) -> None:
        sink = _Sink()
        column = Column("T", "c", 0, "NVARCHAR(4)")
        convert = compile_converter("NVARCHAR(4)", column, sink)
        convert("abcdef")
        self.assertIn(column, sink.overflow)
        column.units = 16
        sink.overflow = None
        convert("abcdef")
        self.assertIsNone(sink.overflow)


class FlattenEquivalenceTests(unittest.TestCase):
    """Real plans from the profiler, all three nesting modes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = documents(3000)

    def test_rows_match_reference(self) -> None:
        for nesting in NESTINGS:
            plan, key_type = plan_for(self.docs, nesting, profile_docs=500)
            flattener = Flattener(plan, key_type)
            compared = 0
            for index, doc in enumerate(self.docs):
                key, root, children, truncated = legacy.flatten_document(doc, plan, key_type)
                flat = flattener.flatten(doc)
                with self.subTest(nesting=nesting, doc=index):
                    self.assertIsNone(flat.reject)
                    self.assertFalse(flat.notes)
                    if truncated:
                        # Profiled on 500 documents: later ones may overflow.
                        _clip(flat, flattener.root_table)
                    else:
                        self.assertFalse(flat.overflow)
                    self.assertEqual(canon(flat.key), canon(key))
                    self.assertEqual(canon(flat.root), canon(root))
                    self.assertEqual(canon(flat.children), canon(children))
                    compared += 1
            self.assertEqual(compared, len(self.docs))
            if nesting != "columns":
                self.assertTrue(plan["children"], f"{nesting} should produce child tables")

    def test_overflow_clips_to_reference(self) -> None:
        """Values longer than the profile saw: reported, and clipping gives the old rows."""
        import copy

        for nesting in NESTINGS:
            plan, key_type = plan_for(self.docs, nesting, profile_docs=500)
            flattener = Flattener(plan, key_type)
            overflowed = 0
            for index, original in enumerate(self.docs[:300]):
                doc = copy.deepcopy(original)
                doc["subject"] = "S" * 5000 + "😀"
                doc["channel"] = "x" * 300
                for message in doc.get("messages", []):
                    message["text"] = "m" * 3000
                    message["sender"] = "s" * 200
                key, root, children, truncated = legacy.flatten_document(doc, plan, key_type)
                flat = flattener.flatten(doc)
                with self.subTest(nesting=nesting, doc=index):
                    self.assertTrue(truncated)
                    self.assertTrue(flat.overflow)
                    self.assertIsNone(flat.reject)
                    _clip(flat, flattener.root_table)
                    self.assertEqual(canon(flat.root), canon(root))
                    self.assertEqual(canon(flat.children), canon(children))
                    overflowed += 1
            self.assertEqual(overflowed, 300)


def _clip(flat, root_table: str) -> None:
    """What the writer does when a column cannot be widened."""
    for column in flat.overflow:
        rows = [flat.root] if column.table == root_table else flat.children.get(column.table, [])
        for row in rows:
            value = row[column.position]
            if isinstance(value, str) and utf16_len(value) > column.units:
                row[column.position] = clip_utf16(value, column.units)


class KeyTests(unittest.TestCase):
    def _plan(self, key_type: str) -> dict:
        return {
            "schema": "dbo",
            "root": {
                "table": "Things",
                "columns": [
                    {"name": "mongo_id", "path": "_id", "sql_type": key_type, "nullable": False},
                    {"name": "name", "path": "name", "sql_type": "NVARCHAR(8)", "nullable": True},
                ],
            },
            "children": [
                {
                    "kind": "map",
                    "table": "ThingsAttrs",
                    "source": "attrs",
                    "parent_key": "things_id",
                    "key_column": {"name": "key", "sql_type": "NVARCHAR(4)"},
                    "value_column": {"name": "value", "sql_type": "NVARCHAR(8)"},
                    "idx_columns": [],
                    "columns": [],
                }
            ],
        }

    def test_missing_and_mismatched_keys_are_rejects(self) -> None:
        flattener = Flattener(self._plan("BIGINT"), "BIGINT")
        self.assertEqual(flattener.flatten({"name": "x"}).reject, "key_missing")
        self.assertEqual(flattener.flatten({"_id": ObjectId(), "name": "x"}).reject, "key_type_mismatch")
        self.assertIsNone(flattener.flatten({"_id": 5, "name": "x"}).reject)

    def test_too_long_key_is_a_reject_not_a_clip(self) -> None:
        flattener = Flattener(self._plan("NVARCHAR(6)"), "NVARCHAR(6)")
        self.assertEqual(flattener.flatten({"_id": "abcdefgh"}).reject, "key_too_long")
        self.assertIsNone(flattener.flatten({"_id": "abc"}).reject)

    def test_map_keys_are_clipped_and_reported(self) -> None:
        plan = self._plan("NVARCHAR(6)")
        doc = {"_id": "k1", "attrs": {"long-key-1": "a", "long-key-2": "b", "ok": "c"}}
        flat = Flattener(plan, "NVARCHAR(6)").flatten(doc)
        _, _, children, truncated = legacy.flatten_document(doc, plan, "NVARCHAR(6)")
        # Both long keys clip to "long"; the second is a duplicate and drops out, as before.
        self.assertEqual(canon(flat.children), canon(children))
        self.assertEqual([note.kind for note in flat.notes], ["clip"])
        self.assertEqual(truncated, 1)


if __name__ == "__main__":
    unittest.main()
