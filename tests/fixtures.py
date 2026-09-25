"""Seeded documents and plans shared by the transfer tests."""

from __future__ import annotations

import random
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import bson
from bson import Binary, Decimal128, Int64, ObjectId

from core.inspect import Profile, build_plan, detect_map_prefixes, key_column_type
from core.transfer import relax_nullability

NESTINGS = ("deep", "hybrid", "columns")


def _text(rng: random.Random, n: int) -> str:
    return "".join(rng.choices(string.ascii_letters + " ", k=n))


def conversation(rng: random.Random, index: int) -> dict[str, Any]:
    """One document shaped like a support conversation, with optional parts."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index * 37)
    doc: dict[str, Any] = {
        "_id": ObjectId.from_datetime(base.replace(tzinfo=None)) if index % 2 else ObjectId(),
        "createdAt": base,
        "updatedAt": base.replace(tzinfo=None),
        "status": rng.choice(["open", "closed", "pending"]),
        "channel": rng.choice(["web", "mail", "phone"]),
        "customerId": f"c{rng.randint(1, 10**6)}",
        "subject": _text(rng, rng.randint(5, 90)),
        "priority": rng.randint(1, 5),
        "views": Int64(rng.randint(0, 2**40)),
        "score": rng.random() * 100,
        "resolved": rng.random() > 0.5,
        "amount": Decimal128(Decimal(f"{rng.randint(0, 10**6)}.{rng.randint(0, 999999):06d}")),
        "meta": {"browser": "Chrome", "os": rng.choice(["Windows", "macOS"]), "ref": _text(rng, 20)},
        "tags": [rng.choice(["a", "b", "c", "d"]) for _ in range(rng.randint(0, 4))],
        "messages": [
            {
                "sender": rng.choice(["agent", "customer"]),
                "text": _text(rng, rng.randint(10, 300)),
                "at": base + timedelta(minutes=m),
                "read": rng.random() > 0.3,
                "attachments": [
                    {"name": f"f{a}.png", "size": rng.randint(1, 10**6), "tags": ["x", "y"]}
                    for a in range(rng.randint(0, 2))
                ],
            }
            for m in range(rng.randint(0, 5))
        ],
        # Many rarely-filled keys: the profiler turns this into a map child table.
        "scores": {f"k{rng.randint(0, 250)}": rng.random() for _ in range(rng.randint(1, 6))},
    }
    if rng.random() > 0.7:
        doc["blob"] = Binary(bytes(rng.getrandbits(8) for _ in range(8)))
    if rng.random() > 0.8:
        doc["refs"] = [ObjectId(), ObjectId()]
    if rng.random() > 0.9:
        del doc["meta"]
    return doc


def documents(count: int, seed: int = 7) -> list[dict[str, Any]]:
    """Documents as they come back from Mongo: encoded and decoded once."""
    rng = random.Random(seed)
    return [bson.decode(bson.encode(conversation(rng, i))) for i in range(count)]


def plan_for(docs: list[dict[str, Any]], nesting: str, profile_docs: int = 2000) -> tuple[dict, str]:
    """Build a plan the way the app does, from a profile of the first documents."""
    profile = Profile()
    for doc in docs[:profile_docs]:
        profile.add_document(doc)
    maps = detect_map_prefixes(profile, 30, 0.2)
    plan = relax_nullability(build_plan(profile, "conversations", "dbo", maps, 1.5, nesting=nesting))
    return plan, key_column_type(plan)


def canon(value: Any) -> Any:
    """Type-strict comparison form: True and 1, or 1.5 and 1.50, stay different."""
    if isinstance(value, list):
        return [canon(item) for item in value]
    if isinstance(value, dict):
        return {key: canon(item) for key, item in value.items()}
    return (type(value).__name__, repr(value))
