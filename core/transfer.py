"""
Move MongoDB documents into MSSQL using a profiled plan.

The plan produced by `core.inspect.build_plan` already describes every table,
column and Mongo path, so loading is generic: nothing here is aware of a
specific collection. Changing the selected collection changes the tables.

Re-running is idempotent: a batch first deletes the root rows it is about to
write, and child rows follow through the plan's ON DELETE CASCADE keys.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterator, Sequence

from bson import Binary, Decimal128, ObjectId, json_util

from core.inspect import ddl_statements, iter_from_mongo, key_column_type, sql_ident
from core.mongo import MongoClientWrapper
from core.mssql import MssqlConnection

ProgressCallback = Callable[[int, int], None]


# --------------------------------------------------------------------------
# plan retargeting
# --------------------------------------------------------------------------


def retarget_plan(
    plan: dict[str, Any], schema: str | None = None, root_table: str | None = None
) -> dict[str, Any]:
    """
    Point a plan at a different schema / root table name.

    Child table names are derived from the root name, so they are renamed with
    it and the DDL and the loader stay in agreement.
    """
    out = dict(plan)
    if schema:
        out["schema"] = schema

    old_root = plan["root"]["table"]
    new_root = sql_ident(root_table) if root_table else old_root
    out["root"] = dict(plan["root"])
    out["root"]["table"] = new_root

    children = []
    for child in plan["children"]:
        renamed = dict(child)
        if new_root != old_root and child["table"].startswith(old_root + "_"):
            renamed["table"] = new_root + child["table"][len(old_root) :]
        children.append(renamed)
    out["children"] = children
    return out


def plan_tables(plan: dict[str, Any]) -> list[str]:
    return [plan["root"]["table"], *(child["table"] for child in plan["children"])]


def relax_nullability(plan: dict[str, Any]) -> dict[str, Any]:
    """
    Allow NULL everywhere except the keys.

    A profile only knows the documents it read, so a field that happened to be
    present in every sampled document still gets NOT NULL. That turns one
    missing field in an unseen document into a failed insert.
    """
    out = dict(plan)
    root = dict(plan["root"])
    root["columns"] = [
        {**column, "nullable": column["name"] != "mongo_id"}
        for column in plan["root"]["columns"]
    ]
    out["root"] = root
    out["children"] = [
        {**child, "columns": [{**column, "nullable": True} for column in child["columns"]]}
        for child in plan["children"]
    ]
    return out


# --------------------------------------------------------------------------
# value conversion
# --------------------------------------------------------------------------


def _json_text(value: Any) -> str:
    try:
        return json_util.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(value, default=str, ensure_ascii=False)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return _json_text(value)
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
    if width is not None and len(text) > width:
        return text[:width], True
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


@dataclass
class TransferStats:
    documents: int = 0
    skipped_no_id: int = 0
    truncated: int = 0
    rows: dict[str, int] = field(default_factory=dict)

    def add_rows(self, table: str, count: int) -> None:
        if count:
            self.rows[table] = self.rows.get(table, 0) + count

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


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


def child_columns(child: dict[str, Any]) -> list[str]:
    if child["kind"] == "map":
        return [child["parent_key"], child["key_column"]["name"], child["value_column"]["name"]]
    return [
        child["parent_key"],
        *child["idx_columns"],
        *(column["name"] for column in child["columns"]),
    ]


# --------------------------------------------------------------------------
# table creation
# --------------------------------------------------------------------------


def ensure_tables(
    target: MssqlConnection, plan: dict[str, Any], recreate: bool = False
) -> tuple[list[str], list[str]]:
    """Create the plan's tables. Returns (created, already existing)."""
    schema = plan["schema"]
    target.ensure_schema(schema)
    statements = ddl_statements(plan)

    if recreate:
        # Children first: they hold the foreign keys into the root table.
        for table, _ in reversed(statements):
            target.drop_table(schema, table)

    created: list[str] = []
    existing: list[str] = []
    for table, sql in statements:
        if target.table_exists(schema, table):
            existing.append(table)
            continue
        target.execute(sql)
        created.append(table)
    return created, existing


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------


def transfer_collection(
    mongo: MongoClientWrapper,
    target: MssqlConnection,
    plan: dict[str, Any],
    collection: str,
    sample: int = 0,
    batch_size: int = 500,
    clear_first: bool = False,
    progress: ProgressCallback | None = None,
) -> TransferStats:
    """Stream a collection into the plan's tables, batch by batch."""
    schema = plan["schema"]
    root_table = plan["root"]["table"]
    key_type = key_column_type(plan)
    root_columns = [column["name"] for column in plan["root"]["columns"]]
    child_specs = [(child["table"], child_columns(child)) for child in plan["children"]]

    if clear_first:
        # Children first: pre-existing tables may lack the cascade the plan asks for.
        for child in reversed(plan["children"]):
            target.clear_table(schema, child["table"])
        target.clear_table(schema, root_table)

    stats = TransferStats()
    expected = sample or None

    keys: list[Any] = []
    root_rows: list[list[Any]] = []
    child_rows: dict[str, list[list[Any]]] = {table: [] for table, _ in child_specs}

    def flush() -> None:
        if not root_rows:
            return
        try:
            target.delete_keys(schema, root_table, "mongo_id", keys)
            stats.add_rows(root_table, target.insert_rows(schema, root_table, root_columns, root_rows))
            for table, columns in child_specs:
                rows = child_rows[table]
                if rows:
                    stats.add_rows(table, target.insert_rows(schema, table, columns, rows))
            target.commit()
        except Exception:
            target.rollback()
            raise
        keys.clear()
        root_rows.clear()
        for rows in child_rows.values():
            rows.clear()

    for doc in iter_from_mongo(mongo, collection, sample):
        stats.documents += 1
        key, row, children, truncated = flatten_document(doc, plan, key_type)
        stats.truncated += truncated
        if key is None:
            stats.skipped_no_id += 1
        else:
            keys.append(key)
            root_rows.append(row)
            for table, rows in children.items():
                child_rows[table].extend(rows)

        if len(root_rows) >= batch_size:
            flush()
            if progress:
                progress(stats.documents, expected or 0)

    flush()
    if progress:
        progress(stats.documents, expected or stats.documents)
    return stats
