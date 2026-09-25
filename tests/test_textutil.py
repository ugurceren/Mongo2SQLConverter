"""The C-level UTF-16 count and the fast JSON path match the originals."""

from __future__ import annotations

import math
import random
import re
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from bson import Binary, Code, Decimal128, Int64, MaxKey, MinKey, ObjectId, Regex, Timestamp, json_util

from core.convert import json_text_fast
from core.textutil import utf16_len
from tests import legacy_reference as legacy


class Utf16LenTests(unittest.TestCase):
    def test_matches_reference(self) -> None:
        samples = [
            "",
            "ascii",
            "Türkçe ğüşıöç",
            "emoji 😀 and 𝔘𝔫𝔦𝔠𝔬𝔡𝔢",
            "\U0010ffff" * 3,
            "lone \ud800 surrogate",
            "\udfff",
            "x" * 5000,
        ]
        rng = random.Random(3)
        for _ in range(200):
            samples.append("".join(chr(rng.choice([rng.randint(32, 0x7FF), rng.randint(0x10000, 0x1FFFF)])) for _ in range(rng.randint(0, 50))))
        for text in samples:
            with self.subTest(text=text[:20]):
                self.assertEqual(utf16_len(text), legacy.utf16_len(text))


def _random_value(rng: random.Random, depth: int = 0):
    leaves = [
        lambda: rng.choice(["a", "Türkçe", "😀", "", "quote\"back\\slash", "\n\t"]),
        lambda: rng.randint(-(2**40), 2**40),
        lambda: rng.random() * 1e6,
        lambda: rng.choice([True, False, None]),
        lambda: rng.choice([0.0, -0.0, 1e-300, 1e300, float("nan"), float("inf"), float("-inf")]),
        lambda: Int64(rng.randint(-(2**62), 2**62)),
        lambda: ObjectId(),
        lambda: datetime(2020, 1, 1, 12, 30, 5, 123000),
        lambda: datetime(2020, 1, 1, tzinfo=timezone.utc),
        lambda: datetime(1901, 3, 4),
        lambda: Decimal128(Decimal("1234.5678")),
        lambda: Decimal128("NaN"),
        lambda: Decimal128("Infinity"),
        lambda: Binary(b"\x00\x01\x02", 0),
        lambda: Binary(uuid.UUID(int=rng.getrandbits(128)).bytes, 4),
        lambda: b"raw-bytes",
        lambda: Regex("^a.*b$", re.IGNORECASE),
        lambda: Code("function () { return 1; }"),
        lambda: Timestamp(1700000000, 3),
        lambda: MinKey(),
        lambda: MaxKey(),
    ]
    if depth < 3 and rng.random() < 0.4:
        if rng.random() < 0.5:
            return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
        return {f"k{i}": _random_value(rng, depth + 1) for i in range(rng.randint(0, 4))}
    return rng.choice(leaves)()


class FastJsonTests(unittest.TestCase):
    def test_same_bytes_as_json_util(self) -> None:
        rng = random.Random(11)
        for i in range(5000):
            value = _random_value(rng)
            if not isinstance(value, (dict, list)):
                value = {"v": value}
            with self.subTest(i=i):
                self.assertEqual(json_text_fast(value), legacy.json_text(value))

    def test_non_finite_floats_take_the_original_path(self) -> None:
        for number in (math.nan, math.inf, -math.inf):
            self.assertEqual(json_text_fast([number]), json_util.dumps([number], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
