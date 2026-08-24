"""MongoDB access for schema profiling and document iteration."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from bson import ObjectId
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

logger = logging.getLogger("mongo2sql")


class MongoClientWrapper:
    def __init__(
        self,
        uri: str,
        database: str,
        username: str | None = None,
        password: str | None = None,
    ):
        self.uri = uri
        self.database_name = database
        self.username = username
        self.password = password
        self._client: MongoClient | None = None

    def connect(self) -> Database:
        kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": 15000}
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

    def estimated_count(self, collection: str, query: dict[str, Any] | None = None) -> int:
        col = self.collection(collection)
        if query:
            return int(col.count_documents(query))
        try:
            return int(col.estimated_document_count())
        except Exception:
            return int(col.count_documents({}))

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
                if count % 1000 == 0:
                    logger.info("read %s documents from %s", count, collection)
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
