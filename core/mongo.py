"""MongoDB access for schema profiling (no sync/watermark logic)."""

from __future__ import annotations

import logging
from typing import Any, Iterator

from pymongo import MongoClient
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
        return sorted(self.db.list_collection_names())

    def collection(self, name: str) -> Collection:
        return self.db[name]

    def iter_documents(
        self,
        collection: str,
        *,
        sample: int = 0,
        batch_size: int = 500,
    ) -> Iterator[dict[str, Any]]:
        col = self.collection(collection)
        if sample:
            cursor = col.aggregate([{"$sample": {"size": sample}}], allowDiskUse=True)
            yield from cursor
            return

        cursor = col.find({}, no_cursor_timeout=True).batch_size(batch_size)
        count = 0
        try:
            for doc in cursor:
                count += 1
                if count % 1000 == 0:
                    logger.info("profiled %s documents", count)
                yield doc
        finally:
            cursor.close()
