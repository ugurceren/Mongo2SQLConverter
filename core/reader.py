"""
Restartable reading of a collection in `_id` order.

A single cursor held open for a day dies on the first network blip, primary
step-down or idle-session expiry, and pymongo never retries a `getMore`. This
reader asks for bounded chunks instead and remembers the last `_id` it handed
out, so after a transient error it reopens exactly where it stopped.

Positions use the `_id` index bounds (`min` / `max` with a hint) rather than
`{_id: {$gt: last}}`: comparison operators only match values of the same BSON
type, so a `$gt` walk over mixed-type ids stops silently at the end of the
first type. Documents arrive raw and are decoded one by one with lenient
options, so an out-of-range date or broken UTF-8 costs one reject, not the job.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import bson
from bson import CodecOptions, Decimal128
from bson.codec_options import DatetimeConversion
from bson.raw_bson import RawBSONDocument

from core.checkpoint import MISSING
from core.retry import TIMEOUT, TRANSIENT, Retrier, classify_mongo, describe

LENIENT = CodecOptions(
    tz_aware=False,
    datetime_conversion=DatetimeConversion.DATETIME_AUTO,
    unicode_decode_error_handler="replace",
)
RAW = CodecOptions(document_class=RawBSONDocument)
ID_INDEX = [("_id", 1)]


@dataclass
class SourceDoc:
    id: Any
    doc: dict[str, Any] | None  # None when the document could not be decoded
    size: int  # raw BSON bytes
    error: str | None = None


def projection_for(plan: dict[str, Any]) -> dict[str, int] | None:
    """Top-level fields the plan reads; everything else stays on the server."""
    names = {"_id"}
    for column in plan["root"]["columns"]:
        names.add(column["path"].split(".", 1)[0].split("[]", 1)[0])
    for child in plan["children"]:
        names.add(child["source"].split(".", 1)[0].split("[]", 1)[0])
    names.discard("")
    if any(name.startswith("$") for name in names):
        return None
    return {name: 1 for name in sorted(names)}


def same_id(a: Any, b: Any) -> bool:
    """Equal as `_id` index keys: 5, Int64(5) and 5.0 are one key."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    numbers = (int, float, Decimal128)
    if isinstance(a, numbers) and isinstance(b, numbers):
        left = a.to_decimal() if isinstance(a, Decimal128) else a
        right = b.to_decimal() if isinstance(b, Decimal128) else b
        try:
            return left == right
        except (TypeError, ValueError):
            return False
    try:
        return bson.encode({"_id": a}) == bson.encode({"_id": b})
    except Exception:
        return a == b


_FIXED = {0x01: 8, 0x07: 12, 0x08: 1, 0x09: 8, 0x0A: 0, 0x10: 4, 0x11: 8, 0x12: 8, 0x13: 16, 0x7F: 0, 0xFF: 0}


def first_id(data: bytes) -> Any:
    """
    The `_id` of a document that will not decode.

    Drivers write `_id` first, so the first element is read on its own and
    decoded alone; MISSING when that is not the case.
    """
    try:
        kind = data[4]
        end = data.index(b"\x00", 5)
        if data[5:end] != b"_id":
            return MISSING
        start = end + 1
        if kind in _FIXED:
            size = _FIXED[kind]
        elif kind in (0x02, 0x0D, 0x0E):
            size = 4 + struct.unpack_from("<i", data, start)[0]
        elif kind in (0x03, 0x04):
            size = struct.unpack_from("<i", data, start)[0]
        elif kind == 0x05:
            size = 5 + struct.unpack_from("<i", data, start)[0]
        else:
            return MISSING
        element = data[4 : start + size]
        single = struct.pack("<i", 4 + len(element) + 1) + element + b"\x00"
        return bson.decode(single, codec_options=LENIENT)["_id"]
    except Exception:
        return MISSING


class ChunkedReader:
    def __init__(
        self,
        collection,
        *,
        start_after: Any = MISSING,
        query: dict[str, Any] | None = None,
        projection: dict[str, int] | None = None,
        chunk: int = 20_000,
        batch: int = 1_000,
        max_time_ms: int = 300_000,
        step: int = 200_000,
        retrier: Retrier | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._raw = collection.with_options(codec_options=RAW)
        self._ids = collection
        self.query = query or None
        self.projection = projection
        self.chunk = max(1, int(chunk))
        self.batch = batch
        self.max_time_ms = max_time_ms
        self.step = max(1, int(step))  # 0 would bound every range at its own start
        self.retrier = retrier
        self.should_stop = should_stop
        # `pos` is the last `_id` handed out (exclusive), or a range start not
        # yet read (inclusive) once a date-filtered range has been finished.
        self.pos = start_after
        self.inclusive = False
        self.yielded = 0

    def __iter__(self) -> Iterator[SourceDoc]:
        while True:
            if self.should_stop and self.should_stop():
                return
            try:
                if self.query is None:
                    finished = yield from self._chunk()
                else:
                    finished = yield from self._range()
            except Exception as exc:
                kind = classify_mongo(exc)
                if self.retrier is None or kind not in (TRANSIENT, TIMEOUT):
                    raise
                self.retrier.wait(kind, exc, where=repr(self.pos)[:80] if self.pos is not MISSING else "başlangıç")
                continue
            if self.retrier is not None:
                self.retrier.reset()
            if finished:
                return

    def _cursor(self, filter_: dict[str, Any], upper: Any = MISSING):
        cursor = (
            self._raw.find(filter_, self.projection)
            .hint(ID_INDEX)
            .sort("_id", 1)
            .batch_size(self.batch)
            .max_time_ms(self.max_time_ms)
            .comment("mongo2sql")
        )
        if self.pos is not MISSING:
            cursor = cursor.min([("_id", self.pos)])
        if upper is not MISSING:
            cursor = cursor.max([("_id", upper)])
        return cursor

    def _chunk(self):
        # The inclusive `min` bound returns the last handed-out document again;
        # one extra slot keeps every chunk moving forward, even a chunk of 1.
        limit = self.chunk + (1 if self.pos is not MISSING and not self.inclusive else 0)
        cursor = self._cursor({}).limit(limit)
        count = 0
        try:
            for raw in cursor:
                count += 1
                item = self._take(raw)
                if item is not None:
                    yield item
        finally:
            _close(cursor)
        return count < limit

    def _range(self):
        """One stretch of the `_id` index, filtered by the date query."""
        upper = self._boundary()
        cursor = self._cursor(self.query, upper)
        try:
            for raw in cursor:
                item = self._take(raw)
                if item is not None:
                    yield item
        finally:
            _close(cursor)
        if upper is MISSING:
            return True
        self.pos = upper
        self.inclusive = True
        return False

    def _boundary(self) -> Any:
        """The `_id` `step` keys ahead: a covered index walk that bounds each range."""
        cursor = (
            self._ids.find({}, {"_id": 1})
            .hint(ID_INDEX)
            .sort("_id", 1)
            .skip(self.step)
            .limit(1)
            .max_time_ms(self.max_time_ms)
        )
        if self.pos is not MISSING:
            cursor = cursor.min([("_id", self.pos)])
        try:
            for doc in cursor:
                return doc["_id"]
        finally:
            _close(cursor)
        return MISSING

    def _take(self, raw: RawBSONDocument) -> SourceDoc | None:
        item = self._decode(raw)
        if (
            not self.inclusive
            and self.pos is not MISSING
            and item.id is not None
            and same_id(item.id, self.pos)
        ):
            return None  # the bound itself, handed out before
        if item.id is None:
            raise RuntimeError(
                "Bir belgenin _id değeri okunamadı; aktarım bu noktayı geçemez. "
                f"Önceki _id: {self.pos!r}"
            )
        self.pos = item.id
        self.inclusive = False
        self.yielded += 1
        return item

    def _decode(self, raw: RawBSONDocument) -> SourceDoc:
        data = raw.raw
        try:
            doc = bson.decode(data, codec_options=LENIENT)
            return SourceDoc(doc.get("_id"), doc, len(data))
        except Exception as exc:
            mongo_id = first_id(data)
            if mongo_id is MISSING:
                mongo_id = self._next_id()
            return SourceDoc(
                None if mongo_id is MISSING else mongo_id,
                None,
                len(data),
                error=describe(exc),
            )

    def _next_id(self) -> Any:
        """Ask the index for the `_id` after the current position (the undecodable one)."""
        cursor = self._ids.find(self.query or {}, {"_id": 1}).hint(ID_INDEX).sort("_id", 1).limit(2)
        if self.pos is not MISSING:
            cursor = cursor.min([("_id", self.pos)])
        try:
            for doc in cursor:
                if self.pos is not MISSING and not self.inclusive and same_id(doc["_id"], self.pos):
                    continue
                return doc["_id"]
        finally:
            _close(cursor)
        return MISSING


def _close(cursor) -> None:
    """Closing a cursor on a dead connection must not hide the error that killed it."""
    try:
        cursor.close()
    except Exception:
        pass
