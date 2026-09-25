"""The pre-flight run-time projection."""

from __future__ import annotations

import unittest

from datetime import datetime
from decimal import Decimal

from core.mongo import wire_compressors
from core.preflight import Finding, Report, format_duration, payload_bytes, project


class ProjectionTests(unittest.TestCase):
    def test_prefetch_runs_at_the_slower_side(self):
        out = project(300_000_000, read=20_000, flatten=8_000, write=3_000)
        self.assertAlmostEqual(out["ön okumalı"], 300_000_000 / 3_000)
        self.assertAlmostEqual(out["sıralı"], 300_000_000 * (1 / 20_000 + 1 / 8_000 + 1 / 3_000))
        # Reading and flattening share one thread: 100 and 100 docs/s make 50.
        self.assertAlmostEqual(project(1000, read=100, flatten=100, write=1000)["ön okumalı"], 20)

    def test_missing_rates_give_no_estimate(self):
        self.assertEqual(project(None, 1, 1, 1), {"sıralı": None, "ön okumalı": None})
        self.assertEqual(project(10, 1, None, 1), {"sıralı": None, "ön okumalı": None})

    def test_durations_read_well(self):
        self.assertEqual(format_duration(96_774), "1 gün 2 sa 52 dk")
        self.assertEqual(format_duration(3_600 * 5 + 60), "5 sa 1 dk")
        self.assertEqual(format_duration(59), "0 dk")
        self.assertEqual(format_duration(None), "hesaplanamadı")

    def test_sql_payload_estimate_counts_nulls_and_text(self):
        self.assertEqual(payload_bytes([[None, "abc", 5, Decimal("1.5"), datetime(2025, 1, 1), b"xy"]]), 9 + 18 + 13 + 23 + 13 + 14)

    def test_report_lines(self):
        report = Report(findings=[Finding("ok", "sql", "SQL Server 16")], rates={"SQL yazma": None})
        report.projection = {"ön okumalı": 7200.0}
        self.assertEqual(report.lines(), ["✓ [sql] SQL Server 16", "· [hız] SQL yazma: —", "· [tahmin] ön okumalı: 2 sa 0 dk"])


class CompressionTests(unittest.TestCase):
    def test_default_offers_what_this_pymongo_can_use(self):
        offered = wire_compressors("mongodb://db:27017/app").split(",")
        self.assertEqual(offered[-1], "zlib")  # always available, last resort
        self.assertTrue(set(offered) <= {"zstd", "snappy", "zlib"})

    def test_uri_choice_is_left_alone(self):
        self.assertIsNone(wire_compressors("mongodb://db:27017/app?compressors=snappy"))
        self.assertIsNone(wire_compressors("mongodb://db/?retryWrites=true&Compressors=zlib"))


if __name__ == "__main__":
    unittest.main()
