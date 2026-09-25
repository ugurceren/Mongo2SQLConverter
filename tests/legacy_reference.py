"""Transfer value conversion and flattening exactly as of commit cc4fcf8.

The optimised code in `core.convert` is checked against this copy, so it must
stay verbatim: do not "fix" or speed up anything here.
"""
# ruff: noqa
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Sequence

from bson import Binary, Decimal128, ObjectId, json_util

def utf16_len(text: str) -> int:
    """NVARCHAR(n) counts UTF-16 code units; astral chars occupy two."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def json_text(value: Any) -> str:
    """Serialize a nested value the same way transfer writes JSON columns."""
    try:
        return json_util.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(value, default=str, ensure_ascii=False)


def clip_utf16(text: str, max_units: int) -> str:
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > max_units:
            return text[:i]
    return text


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json_text(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return _as_naive_utc(value).isoformat(sep=" ")
    if isinstance(value, (ObjectId, Decimal128)):
        return str(value)
    if isinstance(value, (bytes, Binary)):
        return bytes(value).hex()
    return str(value)


def _width_of(sql_type: str) -> int | None:
    if "(" not in sql_type:
        return None
    inside = sql_type.split("(", 1)[1].rstrip(")").split(",")[0].strip()
    if inside.upper() == "MAX":
        return None
    try:
        return int(inside)
    except ValueError:
        return None


def coerce(value: Any, sql_type: str) -> tuple[Any, bool]:
    """Return (parameter value, truncated?) for a column's declared SQL type."""
    if value is None:
        return None, False

    base = sql_type.split("(", 1)[0].upper()

    if base == "BIT":
        if isinstance(value, bool):
            return value, False
        if isinstance(value, (int, float)):
            return bool(value), False
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}, False
        return None, False

    if base in {"INT", "BIGINT"}:
        if isinstance(value, bool):
            return int(value), False
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                return None, False
            return int(value), False
        if isinstance(value, Decimal128):
            try:
                return int(value.to_decimal()), False
            except (ValueError, ArithmeticError, InvalidOperation):
                return None, False
        return None, False

    if base == "FLOAT":
        if isinstance(value, bool):
            return float(value), False
        if isinstance(value, (int, float)):
            numeric = float(value)
            return (numeric if math.isfinite(numeric) else None), False
        if isinstance(value, Decimal128):
            try:
                return float(value.to_decimal()), False
            except (ValueError, ArithmeticError, InvalidOperation):
                return None, False
        return None, False

    if base == "DECIMAL":
        try:
            if isinstance(value, Decimal128):
                return value.to_decimal(), False
            if isinstance(value, (int, float, str)):
                return Decimal(str(value)), False
        except (ValueError, ArithmeticError, InvalidOperation):
            return None, False
        return None, False

    if base.startswith("DATETIME"):
        if isinstance(value, datetime):
            return _as_naive_utc(value), False
        return None, False

    if base == "VARBINARY":
        if isinstance(value, (bytes, bytearray, Binary)):
            return bytes(value), False
        return None, False

    text = _text_of(value)
    width = _width_of(sql_type)
    if width is not None and utf16_len(text) > width:
        return clip_utf16(text, width), True
    return text, False


# --------------------------------------------------------------------------
# document flattening
# --------------------------------------------------------------------------


def _resolve(container: Any, dotted: str) -> Any:
    """Follow a dotted Mongo path; missing or wrongly-shaped links give None."""
    current = container
    if not dotted:
        return current
    for part in dotted.split("."):
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _iter_elements(
    doc: dict[str, Any], source: str
) -> Iterator[tuple[list[int], Any]]:
    """
    Yield (index path, element) for an array source path.

    `source` may cross several array levels ("items[].tags"), which is why the
    index path is a list: one ordinal per level, matching plan idx_columns.
    """
    segments = source.split("[]")

    def walk(container: Any, parts: list[str], indices: list[int]) -> Iterator[tuple[list[int], Any]]:
        values = _resolve(container, parts[0].lstrip("."))
        if not isinstance(values, (list, tuple)):
            return
        for index, item in enumerate(values):
            if len(parts) == 1:
                yield indices + [index], item
            elif isinstance(item, dict):
                yield from walk(item, parts[1:], indices + [index])

    yield from walk(doc, segments, [])


def _iter_map_containers(doc: dict[str, Any], source: str) -> Iterator[dict[str, Any]]:
    """Yield the key/value objects behind a map path, arrays included."""
    if "[]" not in source:
        container = _resolve(doc, source)
        if isinstance(container, dict):
            yield container
        return

    array_source, _, tail = source.rpartition("[]")
    for _, element in _iter_elements(doc, array_source):
        container = _resolve(element, tail.lstrip(".")) if tail else element
        if isinstance(container, dict):
            yield container


def _column_value(element: Any, column_path: str, element_prefix: str) -> Any:
    if column_path == element_prefix:
        return element
    relative = column_path[len(element_prefix) :].lstrip(".")
    return _resolve(element, relative)


def _root_row(
    doc: dict[str, Any], columns: Sequence[dict[str, Any]], key_type: str
) -> tuple[list[Any], Any, int]:
    values: list[Any] = []
    truncated = 0
    key: Any = None
    for column in columns:
        raw = _resolve(doc, column["path"])
        sql_type = key_type if column["name"] == "mongo_id" else column["sql_type"]
        value, cut = coerce(raw, sql_type)
        truncated += int(cut)
        if column["name"] == "mongo_id":
            key = value
        values.append(value)
    return values, key, truncated


def flatten_document(
    doc: dict[str, Any], plan: dict[str, Any], key_type: str
) -> tuple[Any, list[Any], dict[str, list[list[Any]]], int]:
    """
    Turn one document into (key, root row, {child table: rows}, truncations).

    Returns key None when the document has no usable `_id`; the caller skips it
    because that column is the primary key.
    """
    root_values, key, truncated = _root_row(doc, plan["root"]["columns"], key_type)
    children: dict[str, list[list[Any]]] = {}
    if key is None:
        return None, root_values, children, truncated

    for child in plan["children"]:
        rows: list[list[Any]] = []
        if child["kind"] == "map":
            # (parent, key) is the primary key, so a map reached through an
            # array keeps only the first value it sees for a given key.
            seen: set[Any] = set()
            for container in _iter_map_containers(doc, child["source"]):
                for map_key, map_value in container.items():
                    key_value, cut_k = coerce(map_key, child["key_column"]["sql_type"])
                    if key_value is None or key_value in seen:
                        continue
                    seen.add(key_value)
                    value, cut_v = coerce(map_value, child["value_column"]["sql_type"])
                    truncated += int(cut_k) + int(cut_v)
                    rows.append([key, key_value, value])
        else:
            element_prefix = child["source"] + "[]"
            levels = len(child["idx_columns"])
            for indices, element in _iter_elements(doc, child["source"]):
                if len(indices) != levels:
                    continue
                row: list[Any] = [key, *indices]
                for column in child["columns"]:
                    raw = _column_value(element, column["path"], element_prefix)
                    value, cut = coerce(raw, column["sql_type"])
                    truncated += int(cut)
                    row.append(value)
                rows.append(row)
        if rows:
            children[child["table"]] = rows

    return key, root_values, children, truncated
