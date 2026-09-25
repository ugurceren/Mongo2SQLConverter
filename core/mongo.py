"""MongoDB access for schema profiling and document iteration."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from bson import ObjectId
from bson.codec_options import DatetimeConversion
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ExecutionTimeout
from pymongo.collection import Collection
from pymongo.database import Database

logger = logging.getLogger("mongo2sql")


def _value_at(doc: dict[str, Any] | None, path: str) -> Any:
    current: Any = doc
    if current is None:
        return None
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


class MongoClientWrapper:
    def __init__(
        self,
        uri: str,
        database: str,
        username: str | None = None,
        password: str | None = None,
        *,
        long_running: bool = False,
    ):
        self.uri = uri
        self.database_name = database
        self.username = username
        self.password = password
        # A transfer waits longer for a server and sets socket timeouts, so a
        # half-open connection becomes an error it can retry, not a hang.
        self.long_running = long_running
        self._client: MongoClient | None = None

    def connect(self) -> Database:
        kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": 30000 if self.long_running else 15000,
            "appname": "mongo2sql",
            # A date outside Python's range or broken UTF-8 in one document
            # must not fail the whole batch it arrives in.
            "datetime_conversion": DatetimeConversion.DATETIME_AUTO,
            "unicode_decode_error_handler": "replace",
        }
        if self.long_running:
            kwargs["connectTimeoutMS"] = 20000
            kwargs["socketTimeoutMS"] = 360000  # just above the reader's maxTimeMS
        if self.username:
            kwargs["username"] = self.username
            kwargs["password"] = self.password
        self._client = MongoClient(self.uri, **kwargs)
        self._client.admin.command("ping")
        return self._client[self.database_name]

    @property
    def db(self) -> Database:
        if self._client is None:
            return self.connect()
        return self._client[self.database_name]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def list_collections(self) -> list[str]:
        names = self.db.list_collection_names()
        return sorted(n for n in names if not n.startswith("system."))

    def list_databases(self) -> list[str]:
        names = self.db.client.list_database_names()
        skip = {"admin", "local", "config"}
        return sorted(n for n in names if n not in skip)

    def test(self) -> dict[str, Any]:
        self.connect()
        collections = self.list_collections()
        try:
            databases = self.list_databases()
        except Exception:
            databases = []
        return {
            "database": self.database_name,
            "collections": collections,
            "databases": databases,
        }

    def collection(self, name: str) -> Collection:
        return self.db[name]

    def estimated_count(
        self,
        collection: str,
        query: dict[str, Any] | None = None,
        max_time_ms: int | None = None,
    ) -> int | None:
        """
        Document count. Without a query it comes from metadata (instant). With
        `max_time_ms` a filtered count that would scan too long gives None
        instead of holding up the job.
        """
        col = self.collection(collection)
        if query:
            if max_time_ms:
                try:
                    return int(col.count_documents(query, maxTimeMS=max_time_ms))
                except ExecutionTimeout:
                    return None
            return int(col.count_documents(query))
        try:
            return int(col.estimated_document_count())
        except Exception:
            return int(col.count_documents({}))

    def date_bounds(
        self, collection: str, field: str
    ) -> tuple[datetime | None, datetime | None]:
        """Earliest and latest BSON dates stored at `field` (dotted paths allowed).

        Call only when `field` leads a range index: sort + limit 1 then uses it.
        `$type` is omitted so the planner is not forced off that index.
        """
        if not field:
            return None, None
        col = self.collection(collection)
        projection = {field: 1}
        lowest = col.find_one({}, projection, sort=[(field, ASCENDING)])
        highest = col.find_one({}, projection, sort=[(field, DESCENDING)])
        lo = _value_at(lowest, field)
        hi = _value_at(highest, field)
        return (
            lo if isinstance(lo, datetime) else None,
            hi if isinstance(hi, datetime) else None,
        )

    def leading_range_index_fields(self, collection: str) -> set[str]:
        """Field paths that lead an ascending/descending index (usable for a range)."""
        try:
            info = self.collection(collection).index_information()
        except Exception:
            return set()
        out: set[str] = set()
        for spec in info.values():
            keys = spec.get("key") or []
            if not keys:
                continue
            name, direction = keys[0][0], keys[0][1]
            if direction in (ASCENDING, DESCENDING, 1, -1):
                out.add(name)
        return out

    def has_index_on(self, collection: str, field: str) -> bool:
        """True when an index starts with `field`, so a range scan can use it."""
        return field in self.leading_range_index_fields(collection)

    def iter_documents(
        self,
        collection: str,
        *,
        sample: int = 0,
        batch_size: int = 500,
        query: dict[str, Any] | None = None,
        sort_by_id: bool = False,
    ) -> Iterator[dict[str, Any]]:
        col = self.collection(collection)
        match = query or {}
        if sample:
            pipeline: list[dict[str, Any]] = []
            if match:
                pipeline.append({"$match": match})
            pipeline.append({"$sample": {"size": sample}})
            yield from col.aggregate(pipeline, allowDiskUse=True)
            return

        cursor = col.find(match, no_cursor_timeout=True)
        if sort_by_id:
            cursor = cursor.sort("_id", ASCENDING)
        cursor = cursor.batch_size(batch_size)
        count = 0
        try:
            for doc in cursor:
                count += 1
                if count % 25000 == 0:
                    logger.debug("read %s documents from %s", count, collection)
                yield doc
        finally:
            cursor.close()


def encode_mongo_id(value: Any) -> tuple[str, str] | None:
    """Serialize `_id` so an incremental run can resume after it."""
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return str(value), "objectid"
    if isinstance(value, bool):
        return str(value), "str"
    if isinstance(value, int):
        return str(value), "int"
    return str(value), "str"


def encode_resume_id(value: Any) -> tuple[str, str] | None:
    """Serialize a Mongo `_id` or a `mongo_id` value read back from SQL."""
    if value is None:
        return None
    encoded = encode_mongo_id(value)
    if encoded and encoded[1] != "str":
        return encoded
    if isinstance(value, Decimal):
        try:
            as_int = int(value)
            if Decimal(as_int) == value:
                return str(as_int), "int"
        except (ValueError, OverflowError, InvalidOperation):
            pass
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) == 24 and ObjectId.is_valid(raw):
        return raw, "objectid"
    if raw.isdigit() or (raw[0] == "-" and raw[1:].isdigit()):
        return raw, "int"
    return raw, "str"


def decode_mongo_id(raw: str, kind: str) -> Any:
    if kind == "objectid":
        return ObjectId(raw)
    if kind == "int":
        return int(raw)
    return raw


def id_after_filter(last_id: Any) -> dict[str, Any]:
    return {"_id": {"$gt": last_id}}


def date_range_filter(
    field: str, start: datetime | None, end: datetime | None
) -> dict[str, Any]:
    """Range filter on a date field: `start` inclusive, `end` exclusive.

    The caller passes the day after the last wanted day as `end`, so a picked
    range covers whole days without needing an end-of-day timestamp.
    """
    if not field:
        return {}
    bounds: dict[str, Any] = {}
    if start is not None:
        bounds["$gte"] = start
    if end is not None:
        bounds["$lt"] = end
    return {field: bounds} if bounds else {}


def combine_filters(*filters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Join filters with `$and`; empty ones drop out, a single one stays plain."""
    parts = [f for f in filters if f]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}
