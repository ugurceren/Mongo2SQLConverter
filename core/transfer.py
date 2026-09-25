"""
Move MongoDB documents into MSSQL using a profiled plan.

The plan produced by `core.inspect.build_plan` already describes every table,
column and Mongo path, so loading is generic: nothing here is aware of a
specific collection. Changing the selected collection changes the tables.

Re-running is idempotent: a batch first deletes the root rows it is about to
write, and child rows follow through the plan's ON DELETE CASCADE keys.
A first load (empty tables, or `clear_first`) skips that delete.

`coerce` and `flatten_document` below are the reference conversion; the
loader uses the compiled `core.convert.Flattener`, tested against them.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterator, Sequence

from bson import Binary, Decimal128, ObjectId

from core.checkpoint import TABLE as CHECKPOINT_TABLE
from core.checkpoint import RunPlan, is_window
from core.convert import Flattener
from core.inspect import ddl_statements, key_column_type, sql_table_ident
from core.logutil import JobLog, get_logger
from core.mongo import MongoClientWrapper, encode_mongo_id, encode_resume_id
from core.mssql import MssqlConnection
from core.reader import ChunkedReader, SourceDoc, projection_for
from core.rejects import RejectLog
from core.retry import Retrier, RetryPolicy, Stopped
from core.settings import DEFAULT_BATCH
from core.textutil import clip_utf16, json_text, utf16_len
from core.writer import BatchWriter, LiveSql, Unit

ProgressCallback = Callable[[int, int], None]
StopCallback = Callable[[], bool]


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
    new_root = sql_table_ident(root_table) if root_table else old_root
    out["root"] = dict(plan["root"])
    out["root"]["table"] = new_root

    children = []
    for child in plan["children"]:
        renamed = dict(child)
        if new_root != old_root and child["table"].startswith(old_root):
            renamed["table"] = new_root + child["table"][len(old_root) :]
        children.append(renamed)
    out["children"] = children
    return out


def plan_tables(plan: dict[str, Any]) -> list[str]:
    return [plan["root"]["table"], *(child["table"] for child in plan["children"])]


def _loose_source(path: str) -> str:
    return path.replace("[]", "").strip()


def resolve_table_names(
    plan: dict[str, Any], names: dict[str, str] | None
) -> tuple[dict[str, str], list[str]]:
    """
    Match user table names to the plan's tables.

    Keys are Mongo source paths as the Tablo adları card shows them
    (`messages`, `messages[].attachments`); `""` is the root table. A key
    written with array markers (`messages[]`) or without them
    (`messages.attachments`) also matches, when it points at exactly one
    table. Returns ({source: name}, keys that match no table).
    """
    if not names:
        return {}, []
    sources = [child["source"] for child in plan["children"]]
    loose: dict[str, list[str]] = {}
    for source in sources:
        loose.setdefault(_loose_source(source), []).append(source)
    resolved: dict[str, str] = {}
    unknown: list[str] = []
    for raw_key, raw_name in names.items():
        key = str(raw_key).strip()
        name = str(raw_name or "").strip()
        if not name:
            continue
        if key == "" or key in sources:
            resolved[key] = name
            continue
        matches = loose.get(_loose_source(key), [])
        # An exact key for the same table wins over a loosely written one.
        if len(matches) == 1 and matches[0] not in names:
            resolved[matches[0]] = name
        else:
            unknown.append(key)
    return resolved, unknown


def apply_table_names(plan: dict[str, Any], names: dict[str, str] | None) -> dict[str, Any]:
    """
    Override generated SQL table names (see `resolve_table_names` for keys).

    Empty values keep the generated name. Values are passed through
    `sql_table_ident` so they stay valid SQL identifiers.
    """
    resolved, _ = resolve_table_names(plan, names)
    if not resolved:
        return plan
    out = dict(plan)
    root = dict(plan["root"])
    if resolved.get(""):
        root["table"] = sql_table_ident(resolved[""])
    out["root"] = root

    children = []
    for child in plan["children"]:
        renamed = dict(child)
        custom = resolved.get(child["source"])
        if custom:
            renamed["table"] = sql_table_ident(custom)
        children.append(renamed)
    out["children"] = children
    return out


def table_name_problems(tables: Sequence[tuple[str, str]]) -> list[str]:
    """
    Why a set of (label, table name) pairs cannot be written, or [].

    SQL Server compares names without regard to case under the usual
    collations, so `ConvTags` and `convtags` are one table; the checkpoint
    table's name belongs to the loader.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    for label, table in tables:
        groups.setdefault(table.lower(), []).append((label, table))
    problems: list[str] = []
    for folded, entries in groups.items():
        if folded == CHECKPOINT_TABLE.lower():
            problems.append(f"`{entries[0][1]}` aktarımın kontrol noktası tablosunun adı; başka bir ad seçin.")
        if len(entries) > 1:
            spelled = " / ".join(dict.fromkeys(table for _, table in entries))
            labels = ", ".join(label for label, _ in entries)
            problems.append(f"`{spelled}` birden fazla tabloya verilmiş: {labels}.")
    return problems


def plan_table_problems(plan: dict[str, Any]) -> list[str]:
    return table_name_problems(
        [("kök tablo", plan["root"]["table"])]
        + [(child["source"], child["table"]) for child in plan["children"]]
    )


def watermark_from_sql_value(value: Any) -> dict[str, str] | None:
    encoded = encode_resume_id(value)
    if not encoded:
        return None
    last_id, last_id_type = encoded
    return {
        "last_id": last_id,
        "last_id_type": last_id_type,
        "updated": "",
        "source": "sql",
    }


def read_root_watermark(
    target: MssqlConnection, schema: str, table: str
) -> tuple[bool, dict[str, str] | None]:
    """If the root table exists, return (True, last mongo_id watermark or None if empty)."""
    exists, value = target.max_key(schema, table, "mongo_id")
    if not exists:
        return False, None
    if value is None:
        return True, None
    return True, watermark_from_sql_value(value)


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
# column selection
# --------------------------------------------------------------------------


def apply_column_selection(
    plan: dict[str, Any],
    exclude_paths: Sequence[str] = (),
    exclude_sources: Sequence[str] = (),
) -> dict[str, Any]:
    """
    Drop unwanted columns and child tables from a plan.

    The DDL, the flattener and the INSERT column lists all read the same plan,
    so narrowing it here narrows table creation and writing together.

    Selection is stored as exclusions rather than a whitelist: a field that
    only shows up in a later profile stays included by default.

    Keys are never dropped: the root `mongo_id`, each child's parent key and
    the array ordinals carry the relationships the plan depends on. An array
    child whose every column is excluded disappears entirely, and a map child
    is all-or-nothing through `exclude_sources`.
    """
    excluded = set(exclude_paths)
    excluded_sources = set(exclude_sources)
    if not excluded and not excluded_sources:
        return plan

    out = dict(plan)
    root = dict(plan["root"])
    root["columns"] = [
        column
        for column in plan["root"]["columns"]
        if column["name"] == "mongo_id" or column["path"] not in excluded
    ]
    out["root"] = root

    children: list[dict[str, Any]] = []
    for child in plan["children"]:
        if child["source"] in excluded_sources:
            continue
        if child["kind"] == "map":
            children.append(child)
            continue
        kept = [column for column in child["columns"] if column["path"] not in excluded]
        if child["columns"] and not kept:
            continue
        children.append({**child, "columns": kept})
    out["children"] = children
    return out


def plan_column_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """
    One row per selectable column, for the transfer page's picker.

    Key columns are left out because they cannot be deselected. A map child
    contributes a single row standing for its key/value pair.
    """
    rows: list[dict[str, Any]] = []
    for column in plan["root"]["columns"]:
        if column["name"] == "mongo_id":
            continue
        rows.append(
            {
                "table": plan["root"]["table"],
                "kind": "root",
                "source": "",
                "name": column["name"],
                "path": column["path"],
                "sql_type": column["sql_type"],
                "fill": column.get("fill"),
            }
        )

    for child in plan["children"]:
        if child["kind"] == "map":
            rows.append(
                {
                    "table": child["table"],
                    "kind": "map",
                    "source": child["source"],
                    "name": f"{child['key_column']['name']} + {child['value_column']['name']}",
                    "path": child["source"],
                    "sql_type": child["value_column"]["sql_type"],
                    "fill": None,
                }
            )
            continue
        for column in child["columns"]:
            rows.append(
                {
                    "table": child["table"],
                    "kind": "array",
                    "source": child["source"],
                    "name": column["name"],
                    "path": column["path"],
                    "sql_type": column["sql_type"],
                    "fill": column.get("fill"),
                }
            )
    return rows


# --------------------------------------------------------------------------
# value conversion
# --------------------------------------------------------------------------


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


@dataclass
class TransferStats:
    documents: int = 0  # read in this run: written + rejected
    written: int = 0
    truncated: int = 0  # values clipped to their column (recorded in the rejects file)
    rows: dict[str, int] = field(default_factory=dict)
    last_id: str | None = None
    last_id_type: str | None = None
    mode: str = "full"
    first_load: bool = False
    stopped: bool = False
    rejected: int = 0
    nulled: int = 0  # values SQL Server would refuse, written as NULL
    rejects_path: str | None = None
    resumed: bool = False
    retries: int = 0
    widened: list[str] = field(default_factory=list)
    slow_tables: list[str] = field(default_factory=list)
    note: str = ""
    # Where the run's time went; the reading and writing sides overlap.
    read_bytes: int = 0  # raw BSON received from Mongo
    read_seconds: float = 0.0  # waiting for Mongo and decoding
    flatten_seconds: float = 0.0
    sql_seconds: float = 0.0
    insert_seconds: float = 0.0
    commit_seconds: float = 0.0

    def add_rows(self, table: str, count: int) -> None:
        if count:
            self.rows[table] = self.rows.get(table, 0) + count

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    @property
    def clean(self) -> bool:
        return not (self.rejected or self.truncated or self.nulled)


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


def plan_column_specs(plan: dict[str, Any]) -> list[tuple[str, list[tuple[str, str]]]]:
    """(table, [(column, sql_type), ...]) for every table the plan will write."""
    key_type = key_column_type(plan)
    specs: list[tuple[str, list[tuple[str, str]]]] = []
    root_cols = [
        (
            column["name"],
            key_type if column["name"] == "mongo_id" else column["sql_type"],
        )
        for column in plan["root"]["columns"]
    ]
    specs.append((plan["root"]["table"], root_cols))
    for child in plan["children"]:
        cols: list[tuple[str, str]] = [(child["parent_key"], key_type)]
        if child["kind"] == "map":
            cols.append((child["key_column"]["name"], child["key_column"]["sql_type"]))
            cols.append((child["value_column"]["name"], child["value_column"]["sql_type"]))
        else:
            for name in child["idx_columns"]:
                cols.append((name, "INT"))
            for column in child["columns"]:
                cols.append((column["name"], column["sql_type"]))
        specs.append((child["table"], cols))
    return specs


def _add_missing_columns(target: MssqlConnection, plan: dict[str, Any]) -> list[str]:
    """
    A later profile can see fields the first CREATE TABLE did not.

    INSERT uses the current plan, so a new Mongo field such as `values` is 207
    unless this table is widened. New columns are nullable so existing rows stay
    valid. Only the plan's own tables are touched.
    """
    schema = plan["schema"]
    added: list[str] = []
    for table, columns in plan_column_specs(plan):
        live = target.column_names(schema, table)
        if not live:
            continue
        folded = {name.lower() for name in live}
        for name, sql_type in columns:
            if name in live or name.lower() in folded:
                continue
            target.add_column(schema, table, name, sql_type, nullable=True)
            added.append(f"{table}.{name}")
    return added


def _drop_missing_live_columns(target: MssqlConnection, plan: dict[str, Any]) -> dict[str, Any]:
    """Keep only columns that exist on the destination, so INSERT cannot 207."""
    schema = plan["schema"]
    live_root = target.column_names(schema, plan["root"]["table"])
    if not live_root:
        return plan
    folded_root = {name.lower() for name in live_root}
    out = dict(plan)
    root = dict(plan["root"])
    root["columns"] = [
        column
        for column in plan["root"]["columns"]
        if column["name"] in live_root or column["name"].lower() in folded_root
    ]
    out["root"] = root
    children = []
    for child in plan["children"]:
        live = target.column_names(schema, child["table"])
        if not live:
            children.append(child)
            continue
        folded = {name.lower() for name in live}
        renamed = dict(child)
        if child["kind"] != "map":
            renamed["columns"] = [
                column
                for column in child["columns"]
                if column["name"] in live or column["name"].lower() in folded
            ]
        children.append(renamed)
    out["children"] = children
    return out


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
    added = _add_missing_columns(target, plan)
    if added:
        get_logger().info("aktarım kolon eklendi kolonlar=%s", ", ".join(added))
    return created, existing


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------


def adopt_live_types(
    target: MssqlConnection, plan: dict[str, Any], log=None
) -> tuple[dict[str, Any], str]:
    """
    Write with the column types the tables really have.

    Tables created by an earlier profile can differ from this run's plan
    (narrower text, INT where the profile now says BIGINT). Converting to the
    live type turns a value that would not fit into a reported NULL or a
    widening instead of a failed batch. Returns (plan, live key type).
    """
    schema = plan["schema"]
    out = dict(plan)
    key_type = key_column_type(plan)
    changed: list[str] = []

    def adopt(table: str, column: dict[str, Any], live: dict[str, dict[str, Any]]) -> dict[str, Any]:
        info = live.get(column["name"])
        if not info or info["sql_type"].upper() == str(column["sql_type"]).upper():
            return column
        changed.append(f"{table}.{column['name']} {column['sql_type']}→{info['sql_type']}")
        return {**column, "sql_type": info["sql_type"]}

    root_live = target.column_info(schema, plan["root"]["table"])
    root = dict(plan["root"])
    if root_live:
        if "mongo_id" in root_live:
            key_type = root_live["mongo_id"]["sql_type"]
        root["columns"] = [
            column if column["name"] == "mongo_id" else adopt(root["table"], column, root_live)
            for column in plan["root"]["columns"]
        ]
    out["root"] = root

    children = []
    for child in plan["children"]:
        live = target.column_info(schema, child["table"])
        renamed = dict(child)
        if live:
            if child["kind"] == "map":
                renamed["key_column"] = adopt(child["table"], child["key_column"], live)
                renamed["value_column"] = adopt(child["table"], child["value_column"], live)
            else:
                renamed["columns"] = [
                    adopt(child["table"], column, live) for column in child["columns"]
                ]
        children.append(renamed)
    out["children"] = children
    if changed and log is not None:
        log.warning("aktarım canlı kolon tipleri kullanılıyor: %s", "; ".join(changed))
    return out, key_type


class _Failure:
    """An exception raised on the reading thread, handed to the writing one."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_DONE = object()


def _unit(item: SourceDoc, seq: int, flattener: Flattener) -> Unit:
    where = item.position
    if item.doc is None:
        return Unit(seq, item.id, size=item.size, reject="decode_error", stage="decode", error=item.error, position=where)
    flat = flattener.flatten(item.doc)
    if flat.reject is not None:
        return Unit(seq, item.id, size=item.size, reject=flat.reject, stage="flatten", error=flat.reject, position=where)
    return Unit(
        seq,
        item.id,
        key=flat.key,
        root=flat.root,
        children=flat.children,
        size=item.size,
        rows=flat.rows,
        notes=flat.notes,
        overflow=flat.overflow,
        position=where,
    )


def _clock_text(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def transfer_collection(
    mongo: MongoClientWrapper,
    ops: LiveSql,
    plan: dict[str, Any],
    collection: str,
    *,
    run: RunPlan,
    rejects: RejectLog,
    query: dict[str, Any] | None = None,
    batch_docs: int = DEFAULT_BATCH,
    batch_rows: int = 20_000,
    batch_bytes: int = 16 * 1024 * 1024,
    expected_count: int | None = None,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
    log_extra: dict[str, Any] | None = None,
    max_rejects: int = 1000,
    prefetch: bool = True,
    reader_options: dict[str, Any] | None = None,
    retry_policy: RetryPolicy | None = None,
) -> TransferStats:
    """
    Stream a collection into the plan's tables, batch by batch.

    `ops` is a connected `LiveSql` that holds the table lock and owns the
    checkpoint row; `run` says where to start and whether rows may already
    exist. Documents are read in `_id` order, so the checkpoint always names
    the last document that is in SQL. With `prefetch` the next batch is read
    and flattened while the current one is written.
    """
    log = get_logger()
    db = ops.db
    plan, key_type = adopt_live_types(db, plan, log)
    plan = _drop_missing_live_columns(db, plan)
    ops.key_type = key_type
    schema = plan["schema"]
    root_table = plan["root"]["table"]
    for table, specs in plan_column_specs(plan):
        ops.prepare_table(table, [name for name, _ in specs], [sql for _, sql in specs])

    stop = threading.Event()

    def stopping() -> bool:
        return stop.is_set() or bool(should_stop and should_stop())

    flattener = Flattener(plan, key_type)
    child_tables = [child["table"] for child in plan["children"]]
    # A load resumed inside a date window reads that window again: delete first.
    first_load = run.first_load and not (run.resume and is_window(run.start_after))
    writer = BatchWriter(
        ops,
        plan,
        flattener.columns,
        first_load=first_load,
        rejects=rejects,
        retrier=Retrier("sql", retry_policy, log=log, should_stop=stopping),
        max_rejects=max_rejects,
        non_cascading=[] if first_load else db.non_cascading_children(schema, root_table, child_tables),
        log=log,
    )
    mongo_retrier = Retrier("mongo", retry_policy, log=log, should_stop=stopping)
    reader = ChunkedReader(
        mongo.collection(collection),
        start_after=run.start_after,
        query=query,
        projection=projection_for(plan),
        retrier=mongo_retrier,
        should_stop=stopping,
        **(reader_options or {}),
    )

    stats = TransferStats(mode=run.mode, first_load=first_load, resumed=run.resume, note=run.note)
    stats.rejects_path = str(rejects.path)
    job = JobLog(
        "aktarım",
        collection=collection,
        tablo=f"{schema}.{root_table}",
        mod=run.mode,
        parti=batch_docs,
        ilk_yükleme=first_load,
        devam=run.resume,
        **(log_extra or {}),
    )
    job.start(
        beklenen=expected_count,
        başlangıç=run.docs_done,
        karar=run.note,
        sıkıştırma=getattr(mongo, "compressors", None),
    )

    # Where the time goes, so a log line tells a slow link from a slow server:
    # the reading side (Mongo wait + decode, then flattening) and bytes read.
    timing = {"produce": 0.0, "flatten": 0.0, "bytes": 0}

    def batches() -> Iterator[list[Unit]]:
        seq = run.docs_done
        batch: list[Unit] = []
        rows = size = 0
        flatten = 0.0
        started = time.perf_counter()

        def settle() -> None:
            # All three move together, once per batch: the writer reads them
            # from its own thread while this one works on the next batch.
            timing["flatten"] += flatten
            timing["bytes"] += size
            timing["produce"] += time.perf_counter() - started

        for item in reader:
            seq += 1
            tick = time.perf_counter()
            unit = _unit(item, seq, flattener)
            flatten += time.perf_counter() - tick
            batch.append(unit)
            rows += unit.rows
            size += unit.size
            if len(batch) >= batch_docs or rows >= batch_rows or size >= batch_bytes:
                settle()
                yield batch
                batch, rows, size, flatten = [], 0, 0, 0.0
                started = time.perf_counter()
        settle()
        if batch:
            yield batch

    def time_fields() -> dict[str, Any]:
        reading = max(timing["produce"] - timing["flatten"], 0.0)
        megabytes = timing["bytes"] / 1_000_000
        sql = writer.stats
        return {
            "okuma_sn": f"{reading:.0f}",
            "okuma_mb": f"{megabytes:.1f}",
            "okuma_mb_sn": f"{megabytes / reading:.2f}" if reading > 0 else None,
            "düzleştirme_sn": f"{timing['flatten']:.0f}",
            "sql_sn": f"{sql.sql_seconds:.0f}",
            "ekleme_sn": f"{sql.insert_seconds:.0f}",
            "commit_sn": f"{sql.commit_seconds:.0f}",
        }

    def put(q: queue.Queue, item: Any) -> bool:
        while not stop.is_set():
            try:
                q.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def produce(q: queue.Queue) -> None:
        try:
            for batch in batches():
                if not put(q, batch):
                    return
            put(q, _DONE)
        except BaseException as exc:  # handed to the writing thread
            put(q, _Failure(exc))

    samples: deque[tuple[float, int]] = deque()
    done = 0

    def report(batch: list[Unit]) -> None:
        nonlocal done
        done += len(batch)
        now = time.monotonic()
        samples.append((now, done))
        while len(samples) > 2 and now - samples[0][0] > 300:
            samples.popleft()
        rate = 0.0
        if len(samples) > 1 and samples[-1][0] > samples[0][0]:
            rate = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
        remaining = (expected_count - done) if expected_count else None
        eta = _clock_text(remaining / rate) if remaining and remaining > 0 and rate > 0 else None
        if progress:
            progress(done, expected_count or 0)
        job.progress(
            done,
            expected_count or 0,
            satır=sum(writer.stats.rows.values()),
            reddedilen=rejects.rejected,
            **time_fields(),
            kalan=eta,
        )

    worker: threading.Thread | None = None
    q: queue.Queue = queue.Queue(maxsize=2)
    try:
        if prefetch:
            worker = threading.Thread(target=produce, args=(q,), name="mongo2sql-reader", daemon=True)
            worker.start()
            source: Iterator[Any] = iter(q.get, _DONE)
        else:
            source = batches()
        for item in source:
            if isinstance(item, _Failure):
                raise item.exc
            writer.write(item)
            report(item)
            if stopping():
                stats.stopped = True
                break
    except Stopped:
        stats.stopped = True
    finally:
        stop.set()
        if worker is not None:
            while True:  # unblock a producer waiting on a full queue
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            worker.join(timeout=30)
        _fill_stats(stats, writer, rejects, done, mongo_retrier)
        stats.read_bytes = int(timing["bytes"])
        stats.read_seconds = max(timing["produce"] - timing["flatten"], 0.0)
        stats.flatten_seconds = timing["flatten"]

    if not stats.stopped and should_stop and should_stop():
        stats.stopped = True
    finish = job.stopped if stats.stopped else job.done
    finish(
        belgeler=stats.documents,
        yazılan=stats.written,
        satır=stats.total_rows,
        reddedilen=stats.rejected,
        kırpılan=stats.truncated,
        nulllanan=stats.nulled,
        genişletilen=len(stats.widened),
        yeniden_deneme=stats.retries,
        **time_fields(),
        last_id=stats.last_id,
    )
    return stats


def _fill_stats(
    stats: TransferStats, writer: BatchWriter, rejects: RejectLog, done: int, mongo_retrier: Retrier
) -> None:
    written = writer.stats
    stats.documents = done
    stats.written = written.documents
    stats.rows = dict(written.rows)
    stats.rejected = rejects.rejected
    stats.truncated = rejects.clipped
    stats.nulled = rejects.nulled
    stats.widened = list(written.widened)
    stats.slow_tables = sorted(written.slow_tables)
    stats.sql_seconds = written.sql_seconds
    stats.insert_seconds = written.insert_seconds
    stats.commit_seconds = written.commit_seconds
    stats.retries = writer.retrier.total_retries + mongo_retrier.total_retries
    if written.last_id is not None:
        encoded = encode_mongo_id(written.last_id)
        if encoded:
            stats.last_id, stats.last_id_type = encoded
