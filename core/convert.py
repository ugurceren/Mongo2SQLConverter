"""
Compiled document flattener for the transfer hot path.

`core.transfer.flatten_document` and `coerce` stay the reference behaviour.
This module turns a plan into per-column converters once, so a value costs a
function call instead of re-parsing its SQL type and Mongo path every time.
Rows come out identical to the reference, with three deliberate differences:

- text longer than its column is returned whole and reported as overflow, so
  the writer can widen the column instead of clipping silently (key columns,
  which cannot be widened, are still clipped here and reported);
- values SQL Server would reject (NaN / Infinity decimals, numbers outside the
  column range, dates Python cannot represent) become NULL and are reported;
- a document whose key cannot be stored is reported as a reject instead of
  being dropped without a trace.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Iterator

from bson import Binary, Code, Decimal128, ObjectId, json_util
from bson.datetime_ms import DatetimeMS

from core.textutil import clip_utf16, json_text, utf16_len

Converter = Callable[[Any], Any]

INT32 = (-(2**31), 2**31 - 1)
INT64 = (-(2**63), 2**63 - 1)
# SQL Server date types that start later than Python's year 1.
DATE_FLOOR = {"DATETIME": datetime(1753, 1, 1), "SMALLDATETIME": datetime(1900, 1, 1)}
DATE_CEILING = {"SMALLDATETIME": datetime(2079, 6, 6)}

_RELAXED = json_util.RELAXED_JSON_OPTIONS


def _bson_default(value: Any) -> Any:
    return json_util.default(value, _RELAXED)


def _has_code(value: Any) -> bool:
    """`Code` subclasses str, so `json.dumps` would write it as a plain string."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, Code):
            return True
    return False


def json_text_fast(value: Any) -> str:
    """
    Same text as `json_text`, faster.

    `json_util.dumps` walks every value in Python before handing the result to
    `json.dumps`. In relaxed mode that walk only changes BSON types and
    non-finite floats, so letting `json.dumps` call json_util's own encoder for
    the BSON types gives the same bytes. NaN / Infinity raise here
    (`allow_nan=False`) and, like values holding `Code`, take the original path.
    """
    if _has_code(value):
        return json_text(value)
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, default=_bson_default)
    except (TypeError, ValueError, OverflowError):
        return json_text(value)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def text_of(value: Any) -> str:
    """The reference `_text_of`, with the fast JSON path."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json_text_fast(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return _as_naive_utc(value).isoformat(sep=" ")
    if isinstance(value, (ObjectId, Decimal128)):
        return str(value)
    if isinstance(value, (bytes, Binary)):
        return bytes(value).hex()
    return str(value)


def width_of(sql_type: str) -> int | None:
    if "(" not in sql_type:
        return None
    inside = sql_type.split("(", 1)[1].rstrip(")").split(",")[0].strip()
    if inside.upper() == "MAX":
        return None
    try:
        return int(inside)
    except ValueError:
        return None


def _precision_scale(sql_type: str) -> tuple[int, int]:
    if "(" not in sql_type:
        return 18, 0
    inside = sql_type.split("(", 1)[1].rstrip(")")
    parts = [part.strip() for part in inside.split(",")]
    try:
        precision = int(parts[0])
        scale = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 18, 0
    return precision, scale


def base_type(sql_type: str) -> str:
    return sql_type.split("(", 1)[0].strip().upper()


def kind_of(sql_type: str) -> str:
    """How a value is converted for this column; everything unlisted is text."""
    base = base_type(sql_type)
    if base == "BIT":
        return "bit"
    if base in {"INT", "BIGINT"}:
        return "int"
    if base == "FLOAT":
        return "float"
    if base == "DECIMAL":
        return "decimal"
    if base.startswith("DATETIME") or base == "SMALLDATETIME":
        return "date"
    if base == "VARBINARY":
        return "binary"
    return "text"


class Column:
    """
    One written column, shared by its converter and the writer.

    `units` is the declared text width (None = MAX or not text). The writer
    raises it after widening the live column, and the converter reads it on
    every value, so later documents stop reporting overflow.
    """

    __slots__ = ("table", "name", "position", "sql_type", "units", "is_key")

    def __init__(
        self, table: str, name: str, position: int, sql_type: str, is_key: bool = False
    ) -> None:
        self.table = table
        self.name = name
        self.position = position
        self.sql_type = sql_type
        self.units = width_of(sql_type) if kind_of(sql_type) == "text" else None
        self.is_key = is_key

    def __repr__(self) -> str:
        return f"Column({self.table}.{self.name} {self.sql_type})"


@dataclass
class Note:
    """A value that was changed on its way in; it goes to the rejects file."""

    kind: str  # "clip" | "out_of_range" | "bad_date"
    table: str
    column: str
    length: int | None = None
    width: int | None = None
    detail: str = ""


@dataclass
class Flat:
    key: Any
    root: list[Any]
    children: dict[str, list[list[Any]]]
    notes: list[Note] = field(default_factory=list)
    # Column -> longest UTF-16 length that did not fit its current width.
    overflow: dict[Column, int] = field(default_factory=dict)
    reject: str | None = None

    @property
    def rows(self) -> int:
        return 1 + sum(len(rows) for rows in self.children.values())


class _Sink:
    """Per-document collector the converters write into."""

    __slots__ = ("notes", "overflow")

    def __init__(self) -> None:
        self.notes: list[Note] | None = None
        self.overflow: dict[Column, int] | None = None

    def note(self, item: Note) -> None:
        if self.notes is None:
            self.notes = []
        self.notes.append(item)

    def over(self, column: Column, length: int) -> None:
        if self.overflow is None:
            self.overflow = {}
        if length > self.overflow.get(column, 0):
            self.overflow[column] = length


class _NullSink(_Sink):
    """For converters whose caller does its own reporting."""

    def note(self, item: Note) -> None:
        pass

    def over(self, column: Column, length: int) -> None:
        pass


# --------------------------------------------------------------------------
# converters
# --------------------------------------------------------------------------


def compile_converter(sql_type: str, column: Column, sink: _Sink) -> Converter:
    kind = kind_of(sql_type)
    base = base_type(sql_type)

    if kind == "bit":
        def bit(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "y"}
            return None

        return bit

    if kind == "int":
        low, high = INT32 if base == "INT" else INT64

        def integer(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    return None
                number = int(value)
            elif isinstance(value, Decimal128):
                try:
                    number = int(value.to_decimal())
                except (ValueError, ArithmeticError, InvalidOperation):
                    return None
            else:
                return None
            if low <= number <= high:
                return number
            sink.note(Note("out_of_range", column.table, column.name, detail=str(number)[:64]))
            return None

        return integer

    if kind == "float":
        def floating(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, bool):
                return float(value)
            if isinstance(value, (int, float)):
                try:
                    numeric = float(value)
                except OverflowError:
                    sink.note(Note("out_of_range", column.table, column.name, detail=str(value)[:64]))
                    return None
                return numeric if math.isfinite(numeric) else None
            if isinstance(value, Decimal128):
                try:
                    numeric = float(value.to_decimal())
                except (ValueError, ArithmeticError, InvalidOperation):
                    return None
                if math.isfinite(numeric):
                    return numeric
                sink.note(Note("out_of_range", column.table, column.name, detail=str(value)[:64]))
                return None
            return None

        return floating

    if kind == "decimal":
        precision, scale = _precision_scale(sql_type)
        limit = Decimal(10) ** (precision - scale)
        quantum = Decimal(1).scaleb(-scale)

        def decimal(value: Any) -> Any:
            if value is None:
                return None
            try:
                if isinstance(value, Decimal128):
                    number = value.to_decimal()
                elif isinstance(value, (int, float, str)):
                    number = Decimal(str(value))
                else:
                    return None
                if not number.is_finite() or abs(number) >= limit:
                    sink.note(Note("out_of_range", column.table, column.name, detail=str(value)[:64]))
                    return None
                # More decimals than the column holds cannot always be bound
                # (precision > 38); round like SQL Server would on the way in.
                if number.as_tuple().exponent < -scale:
                    number = number.quantize(quantum, rounding=ROUND_HALF_UP)
                return number
            except (ValueError, ArithmeticError, InvalidOperation):
                return None

        return decimal

    if kind == "date":
        # DATETIME2 / DATETIMEOFFSET hold every Python datetime; the older
        # types start (and SMALLDATETIME ends) inside Python's range.
        floor = DATE_FLOOR.get(base)
        ceiling = DATE_CEILING.get(base)
        checked = floor is not None or ceiling is not None

        def date_value(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, datetime):
                naive = _as_naive_utc(value)
                if checked and (
                    (floor is not None and naive < floor) or (ceiling is not None and naive > ceiling)
                ):
                    sink.note(Note("out_of_range", column.table, column.name, detail=naive.isoformat()))
                    return None
                return naive
            if isinstance(value, DatetimeMS):
                # A BSON date outside Python's range, kept undecoded by the reader.
                sink.note(Note("bad_date", column.table, column.name, detail=repr(value)[:64]))
                return None
            return None

        return date_value

    if kind == "binary":
        def binary(value: Any) -> Any:
            if value is None:
                return None
            if isinstance(value, (bytes, bytearray, Binary)):
                return bytes(value)
            return None

        return binary

    def text(value: Any) -> Any:
        if value is None:
            return None
        out = value if type(value) is str else text_of(value)
        units = column.units
        # 2 × code points bounds the UTF-16 length, so most values skip counting.
        if units is None or len(out) * 2 <= units:
            return out
        length = utf16_len(out)
        if length > units:
            sink.over(column, length)
        return out

    return text


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def split_path(dotted: str) -> tuple[str, ...]:
    return tuple(part for part in dotted.split(".") if part)


def get_path(container: Any, parts: tuple[str, ...]) -> Any:
    """`_resolve` with the path already split."""
    current = container
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _walk(container: Any, segments: list[tuple[str, ...]], depth: int, indices: list[int]) -> Iterator[tuple[list[int], Any]]:
    values = get_path(container, segments[depth])
    if not isinstance(values, (list, tuple)):
        return
    last = depth == len(segments) - 1
    for index, item in enumerate(values):
        if last:
            yield indices + [index], item
        elif isinstance(item, dict):
            yield from _walk(item, segments, depth + 1, indices + [index])


def _segments(source: str) -> list[tuple[str, ...]]:
    return [split_path(segment.lstrip(".")) for segment in source.split("[]")]


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------


class _ArrayChild:
    def __init__(self, child: dict[str, Any], columns: dict, sink: _Sink) -> None:
        self.table = child["table"]
        self.segments = _segments(child["source"])
        self.levels = len(child["idx_columns"])
        prefix = child["source"] + "[]"
        self.cols: list[tuple[tuple[str, ...] | None, Converter]] = []
        first = 1 + self.levels
        for offset, column in enumerate(child["columns"]):
            meta = Column(self.table, column["name"], first + offset, column["sql_type"])
            columns[(self.table, column["name"])] = meta
            relative = None if column["path"] == prefix else split_path(
                column["path"][len(prefix):].lstrip(".")
            )
            self.cols.append((relative, compile_converter(column["sql_type"], meta, sink)))
        self.simple = len(self.segments) == 1 and self.levels == 1

    def rows(self, doc: dict[str, Any], key: Any) -> list[list[Any]]:
        out: list[list[Any]] = []
        cols = self.cols
        if self.simple:
            values = get_path(doc, self.segments[0])
            if not isinstance(values, (list, tuple)):
                return out
            for index, element in enumerate(values):
                row = [key, index]
                for relative, convert in cols:
                    row.append(convert(element if relative is None else get_path(element, relative)))
                out.append(row)
            return out
        for indices, element in _walk(doc, self.segments, 0, []):
            if len(indices) != self.levels:
                continue
            row = [key, *indices]
            for relative, convert in cols:
                row.append(convert(element if relative is None else get_path(element, relative)))
            out.append(row)
        return out


class _MapChild:
    def __init__(self, child: dict[str, Any], columns: dict, sink: _Sink) -> None:
        self.table = child["table"]
        self.sink = sink
        source = child["source"]
        if "[]" in source:
            array_source, _, tail = source.rpartition("[]")
            self.array_segments: list[tuple[str, ...]] | None = _segments(array_source)
            self.tail = split_path(tail.lstrip(".")) if tail else None
            self.path = None
        else:
            self.array_segments = None
            self.tail = None
            self.path = split_path(source)
        key_column = child["key_column"]
        value_column = child["value_column"]
        self.key_meta = Column(self.table, key_column["name"], 1, key_column["sql_type"], is_key=True)
        columns[(self.table, key_column["name"])] = self.key_meta
        # Map keys are part of the primary key, so they cannot be widened:
        # their converter reports nothing and clipping happens in `rows`.
        self.key_convert = compile_converter(key_column["sql_type"], self.key_meta, _NullSink())
        value_meta = Column(self.table, value_column["name"], 2, value_column["sql_type"])
        columns[(self.table, value_column["name"])] = value_meta
        self.value_convert = compile_converter(value_column["sql_type"], value_meta, sink)

    def _containers(self, doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if self.array_segments is None:
            container = get_path(doc, self.path)
            if isinstance(container, dict):
                yield container
            return
        for _, element in _walk(doc, self.array_segments, 0, []):
            container = get_path(element, self.tail) if self.tail is not None else element
            if isinstance(container, dict):
                yield container

    def rows(self, doc: dict[str, Any], key: Any) -> list[list[Any]]:
        out: list[list[Any]] = []
        seen: set[Any] = set()
        units = self.key_meta.units
        for container in self._containers(doc):
            for map_key, map_value in container.items():
                key_value = self.key_convert(map_key)
                if key_value is None:
                    continue
                clipped_from = None
                if units is not None and isinstance(key_value, str) and len(key_value) * 2 > units:
                    length = utf16_len(key_value)
                    if length > units:
                        clipped_from = length
                        key_value = clip_utf16(key_value, units)
                if key_value in seen:
                    continue
                seen.add(key_value)
                if clipped_from is not None:
                    self.sink.note(
                        Note("clip", self.table, self.key_meta.name, clipped_from, units)
                    )
                out.append([key, key_value, self.value_convert(map_value)])
        return out


class Flattener:
    """A plan compiled into converters; `flatten(doc)` gives one document's rows."""

    def __init__(self, plan: dict[str, Any], key_type: str) -> None:
        self.sink = _Sink()
        self.columns: dict[tuple[str, str], Column] = {}
        self.root_table = plan["root"]["table"]
        self._root: list[tuple[tuple[str, ...], Converter]] = []
        self._key_index: int | None = None
        self._key_meta: Column | None = None
        for position, column in enumerate(plan["root"]["columns"]):
            is_key = column["name"] == "mongo_id"
            sql_type = key_type if is_key else column["sql_type"]
            meta = Column(self.root_table, column["name"], position, sql_type, is_key=is_key)
            self.columns[(self.root_table, column["name"])] = meta
            self._root.append((split_path(column["path"]), compile_converter(sql_type, meta, self.sink)))
            if is_key:
                self._key_index = position
                self._key_meta = meta
        self._children: list[_ArrayChild | _MapChild] = []
        for child in plan["children"]:
            if child["kind"] == "map":
                self._children.append(_MapChild(child, self.columns, self.sink))
            else:
                self._children.append(_ArrayChild(child, self.columns, self.sink))

    def flatten(self, doc: dict[str, Any]) -> Flat:
        sink = self.sink
        sink.notes = None
        sink.overflow = None
        root = [convert(get_path(doc, parts)) for parts, convert in self._root]
        key = root[self._key_index] if self._key_index is not None else None
        reject = None
        if key is None:
            reject = "key_missing" if doc.get("_id") is None else "key_type_mismatch"
        elif sink.overflow and self._key_meta in sink.overflow:
            reject = "key_too_long"
        children: dict[str, list[list[Any]]] = {}
        if reject is None:
            for child in self._children:
                rows = child.rows(doc, key)
                if rows:
                    children[child.table] = rows
        return Flat(
            key=key,
            root=root,
            children=children,
            notes=sink.notes or [],
            overflow=sink.overflow or {},
            reject=reject,
        )
