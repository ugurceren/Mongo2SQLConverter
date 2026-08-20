"""
Profile a MongoDB collection and propose a relational schema for it.

Walks documents and records per-field-path statistics (BSON types, how often a
field is present, max UTF-16 length, array sizes), then derives SQL Server
column types from those measurements. This is deliberately measurement-based:
mongodrdl and JSON Schema exports both stop at "varchar" / "type: string", so
neither can tell you that a key field reaches 240 characters. That number cost
this project a production truncation error.

    python tools/infer_schema.py --collection conversations
    python tools/infer_schema.py --collection conversations --sample 5000 --out-drdl out.drdl
    python tools/infer_schema.py --from-file conv_sample.json --collection conversations

Outputs: console report always, plus optional --out-json / --out-drdl / --out-ddl.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from bson import Binary, Decimal128, Int64, ObjectId, json_util

from core.mongo import MongoClientWrapper
from core.settings import load_settings
from core.textutil import safe_console, utf16_len

# NVARCHAR widths we are willing to emit. Anything past the last bucket becomes
# NVARCHAR(MAX). 450 is here because it is the widest NVARCHAR that still fits
# in a SQL Server index key (900 bytes / 2).
BUCKETS = (16, 32, 64, 128, 256, 450, 1000, 4000)
INDEX_KEY_LIMIT = 450

# int32 range; anything outside needs BIGINT.
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def type_name(value: Any) -> str:
    # bool before int: bool is a subclass of int in Python.
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Int64):
        return "int64"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, Decimal128):
        return "decimal"
    if isinstance(value, str):
        return "string"
    if isinstance(value, datetime):
        return "date"
    if isinstance(value, ObjectId):
        return "objectId"
    if isinstance(value, Binary):
        return "binary"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


@dataclass
class FieldStats:
    path: str
    # Container prefix this field lives in, used for the fill ratio. None means
    # the path is an array element pseudo-path, where "present" counts elements.
    parent: str | None
    present: int = 0
    nulls: int = 0
    types: Counter = field(default_factory=Counter)
    max_utf16: int = 0
    longest_sample: str = ""
    num_min: float | None = None
    num_max: float | None = None
    non_integral: bool = False
    non_finite: bool = False
    array_max_len: int = 0
    array_total: int = 0
    distinct: set[str] = field(default_factory=set)
    distinct_overflow: bool = False

    def record(self, value: Any, preview_len: int, distinct_limit: int) -> None:
        self.present += 1
        self.types[type_name(value)] += 1

        if value is None:
            self.nulls += 1
            return

        if isinstance(value, str):
            units = utf16_len(value)
            if units > self.max_utf16:
                self.max_utf16 = units
                self.longest_sample = value[:preview_len]
            # Long values are never a useful enum, and holding them all would
            # dominate memory on a big scan.
            if not self.distinct_overflow and len(value) <= 200:
                self.distinct.add(value)
                if len(self.distinct) > distinct_limit:
                    self.distinct_overflow = True
                    self.distinct.clear()
            return

        if isinstance(value, bool):
            return

        if isinstance(value, (int, float, Decimal128)):
            try:
                numeric = float(value.to_decimal()) if isinstance(value, Decimal128) else float(value)
            except (ValueError, ArithmeticError):
                self.non_finite = True
                return
            if not math.isfinite(numeric):
                self.non_finite = True
                return
            self.num_min = numeric if self.num_min is None else min(self.num_min, numeric)
            self.num_max = numeric if self.num_max is None else max(self.num_max, numeric)
            if isinstance(value, float) and numeric != int(numeric):
                self.non_integral = True
            return

        if isinstance(value, (list, tuple)):
            self.array_max_len = max(self.array_max_len, len(value))
            self.array_total += len(value)

    @property
    def concrete_types(self) -> dict[str, int]:
        return {name: n for name, n in self.types.items() if name != "null"}

    @property
    def dominant_type(self) -> str:
        concrete = self.concrete_types
        if not concrete:
            return "null"
        return max(concrete.items(), key=lambda kv: kv[1])[0]


class Profile:
    """Field-path statistics accumulated over a document stream."""

    def __init__(
        self,
        preview_len: int = 80,
        distinct_limit: int = 50,
        max_paths: int = 4000,
    ):
        self.preview_len = preview_len
        self.distinct_limit = distinct_limit
        self.max_paths = max_paths
        self.stats: dict[str, FieldStats] = {}
        # How many times each container prefix was visited, so a child's
        # presence can be expressed as a fill ratio against its own parent
        # rather than against the document count.
        self.containers: Counter = Counter()
        self.documents = 0
        self.paths_truncated = False

    def add_document(self, doc: dict[str, Any]) -> None:
        self.documents += 1
        self._walk(doc, "")

    def _stat(self, path: str, parent: str | None) -> FieldStats | None:
        existing = self.stats.get(path)
        if existing is not None:
            return existing
        if len(self.stats) >= self.max_paths:
            self.paths_truncated = True
            return None
        created = FieldStats(path=path, parent=parent)
        self.stats[path] = created
        return created

    def _walk(self, obj: dict[str, Any], prefix: str) -> None:
        self.containers[prefix] += 1
        for key, value in obj.items():
            path = prefix + str(key)
            stat = self._stat(path, prefix)
            if stat is None:
                continue
            stat.record(value, self.preview_len, self.distinct_limit)

            if isinstance(value, dict):
                self._walk(value, path + ".")
            elif isinstance(value, (list, tuple)):
                element_path = path + "[]"
                element = self._stat(element_path, None)
                if element is None:
                    continue
                for item in value:
                    element.record(item, self.preview_len, self.distinct_limit)
                    if isinstance(item, dict):
                        self._walk(item, element_path + ".")

    def fill_ratio(self, stat: FieldStats) -> float | None:
        """Share of the parent containers in which this field appears."""
        if stat.parent is None:
            return None
        total = self.containers.get(stat.parent, 0)
        if not total:
            return None
        return stat.present / total

    def children_of(self, prefix: str) -> list[FieldStats]:
        depth_prefix = prefix + "." if prefix else ""
        out = []
        for path, stat in self.stats.items():
            if not depth_prefix or not path.startswith(depth_prefix):
                continue
            rest = path[len(depth_prefix):]
            if "." in rest or "[]" in rest:
                continue
            out.append(stat)
        return out


# --------------------------------------------------------------------------
# dictionary-as-object detection
# --------------------------------------------------------------------------


def detect_map_prefixes(
    profile: Profile, min_keys: int, max_fill: float
) -> dict[str, dict[str, Any]]:
    """
    Find objects whose *keys are data* rather than schema.

    `tracker.slots` is the motivating case: every slot name becomes a key, so a
    naive profiler explodes it into hundreds of sparse columns. Such an object
    belongs in a key/value child table instead.
    """
    maps: dict[str, dict[str, Any]] = {}
    for path, stat in profile.stats.items():
        if stat.dominant_type != "object":
            continue
        children = profile.children_of(path)
        if len(children) < min_keys:
            continue
        container_total = profile.containers.get(path + ".", 0)
        if not container_total:
            continue
        fills = [child.present / container_total for child in children]
        average_fill = sum(fills) / len(fills)
        if average_fill > max_fill:
            continue
        maps[path] = {
            "keys": len(children),
            "avg_fill": average_fill,
            "key_max_utf16": max((utf16_len(c.path.rsplit(".", 1)[-1]) for c in children), default=0),
            "value_types": _merge_types(children),
            "value_max_utf16": max((c.max_utf16 for c in children), default=0),
        }
    return maps


def _merge_types(stats: list[FieldStats]) -> dict[str, int]:
    merged: Counter = Counter()
    for stat in stats:
        merged.update(stat.concrete_types)
    return dict(merged)


# --------------------------------------------------------------------------
# BSON -> SQL type inference
# --------------------------------------------------------------------------


def nvarchar_width(max_utf16: int, headroom: float) -> str:
    target = max(1, math.ceil(max_utf16 * headroom))
    for bucket in BUCKETS:
        if target <= bucket:
            return f"NVARCHAR({bucket})"
    return "NVARCHAR(MAX)"


def _type_breakdown(concrete: dict[str, int]) -> str:
    total = sum(concrete.values()) or 1
    return ", ".join(
        f"{name} %{100 * n / total:.1f}"
        for name, n in sorted(concrete.items(), key=lambda kv: -kv[1])
    )


def sql_type_for(
    stat: FieldStats,
    headroom: float,
    shape_conflict: bool = False,
    as_json: bool = False,
) -> tuple[str, list[str]]:
    """Proposed column type plus any notes worth surfacing in the report."""
    notes: list[str] = []
    concrete = stat.concrete_types
    if not concrete:
        return "NVARCHAR(16)", ["yalnizca null gorunmus"]

    if as_json or shape_conflict:
        reason = (
            "JSON kolon"
            if as_json
            else f"sekil cakismasi ({_type_breakdown(concrete)})"
        )
        return "NVARCHAR(MAX)", [f"{reason}: alt alanlar ayri kolon yapilmadi"]

    kinds = set(concrete)

    if len(kinds) > 1:
        # Numeric widenings are not really conflicts, just pick the wider type.
        if kinds <= {"int", "int64"}:
            return "BIGINT", notes
        if kinds <= {"int", "int64", "double", "decimal"}:
            return "FLOAT", ["tamsayi ve ondalik karisik"]
        notes.append(f"tip cakismasi: {_type_breakdown(concrete)}")
        return nvarchar_width(max(stat.max_utf16, 64), headroom), notes

    kind = next(iter(kinds))

    if kind == "string":
        if stat.max_utf16 > INDEX_KEY_LIMIT:
            notes.append(f"max {stat.max_utf16} > {INDEX_KEY_LIMIT}: index anahtari olamaz")
        return nvarchar_width(stat.max_utf16, headroom), notes
    if kind == "bool":
        return "BIT", notes
    if kind == "int":
        low = stat.num_min if stat.num_min is not None else 0
        high = stat.num_max if stat.num_max is not None else 0
        if low < INT32_MIN or high > INT32_MAX:
            return "BIGINT", notes
        return "INT", notes
    if kind == "int64":
        return "BIGINT", notes
    if kind == "double":
        if stat.non_finite:
            notes.append("NaN / Infinity gorulmus, yazim sirasinda NULL'a cevrilmeli")
        return "FLOAT", notes
    if kind == "decimal":
        return "DECIMAL(38, 6)", notes
    if kind == "date":
        return "DATETIME2(3)", notes
    if kind == "objectId":
        return "CHAR(24)", notes
    if kind == "binary":
        return "VARBINARY(MAX)", notes
    if kind == "object":
        return "NVARCHAR(MAX)", ["JSON olarak saklanir"]
    if kind == "array":
        return "NVARCHAR(MAX)", ["JSON olarak saklanir"]
    return nvarchar_width(max(stat.max_utf16, 64), headroom), [f"bilinmeyen tip: {kind}"]


DRDL_TYPES = {
    "NVARCHAR": "varchar",
    "CHAR": "varchar",
    "BIT": "boolean",
    "INT": "int",
    "BIGINT": "int64",
    "FLOAT": "float64",
    "DECIMAL": "decimal128",
    "DATETIME2": "date",
    "VARBINARY": "bindata",
}


def drdl_type(sql: str) -> str:
    base = sql.split("(")[0].strip().upper()
    return DRDL_TYPES.get(base, "varchar")


NESTING_DEEP = "deep"
NESTING_HYBRID = "hybrid"
NESTING_COLUMNS = "columns"
NESTING_DOCUMENT = "document"

NESTING_OPTIONS = (
    (
        NESTING_DEEP,
        "Derin iliskisel",
        "Diziler child tablo, nesneler kolon. En ince kirilim; en cok tablo.",
    ),
    (
        NESTING_HYBRID,
        "Hibrit",
        "Kok diziler child tablo; daha derin ic ice yapilar JSON kolon.",
    ),
    (
        NESTING_COLUMNS,
        "Tek tablo + JSON",
        "Bir dokuman = bir satir. Ust seviye skalerler kolon, nesne ve diziler JSON.",
    ),
    (
        NESTING_DOCUMENT,
        "Birebir dokuman",
        "Tek tablo: mongo_id + document JSON. Sema yok, birebir kopya.",
    ),
)


def nesting_labels() -> dict[str, str]:
    return {key: title for key, title, _ in NESTING_OPTIONS}


def is_top_level(path: str) -> bool:
    return "." not in path and "[]" not in path


def describe_shape(profile: Profile) -> dict[str, Any]:
    """What nesting this collection actually has (from a profile)."""
    top_arrays: list[str] = []
    nested_arrays: list[str] = []
    top_objects: list[str] = []
    nested_objects: list[str] = []
    for stat in profile.stats.values():
        if stat.dominant_type == "array":
            (top_arrays if is_top_level(stat.path) else nested_arrays).append(stat.path)
        elif stat.dominant_type == "object" and profile.children_of(stat.path):
            (top_objects if is_top_level(stat.path) else nested_objects).append(stat.path)
    return {
        "documents": profile.documents,
        "top_arrays": sorted(top_arrays),
        "nested_arrays": sorted(nested_arrays),
        "top_objects": sorted(top_objects),
        "nested_objects": sorted(nested_objects),
    }


def nesting_keys_for(shape: dict[str, Any] | None) -> tuple[list[str], str]:
    """
    Which strategies are meaningful for this shape, plus a default.

    Flat collections do not need child-table modes. Hybrid only differs from
    deep when there are both top-level arrays and deeper nesting.
    """
    if not shape:
        keys = [item[0] for item in NESTING_OPTIONS]
        return keys, NESTING_HYBRID
    has_top_array = bool(shape.get("top_arrays"))
    has_deep = bool(shape.get("nested_arrays") or shape.get("nested_objects"))
    has_object = bool(shape.get("top_objects") or shape.get("nested_objects"))
    if has_top_array and has_deep:
        return [NESTING_DEEP, NESTING_HYBRID, NESTING_COLUMNS, NESTING_DOCUMENT], NESTING_HYBRID
    if has_top_array:
        return [NESTING_DEEP, NESTING_COLUMNS, NESTING_DOCUMENT], NESTING_DEEP
    if has_object:
        return [NESTING_DEEP, NESTING_COLUMNS, NESTING_DOCUMENT], NESTING_COLUMNS
    return [NESTING_COLUMNS, NESTING_DOCUMENT], NESTING_COLUMNS


def shape_caption(shape: dict[str, Any]) -> str:
    parts: list[str] = []
    if shape.get("top_arrays"):
        names = ", ".join(f"`{name}`" for name in shape["top_arrays"][:6])
        extra = f" +{len(shape['top_arrays']) - 6}" if len(shape["top_arrays"]) > 6 else ""
        parts.append(f"kok dizi: {names}{extra}")
    if shape.get("nested_arrays"):
        parts.append(f"{len(shape['nested_arrays'])} ic ice dizi")
    if shape.get("top_objects"):
        names = ", ".join(f"`{name}`" for name in shape["top_objects"][:4])
        parts.append(f"kok nesne: {names}")
    if shape.get("nested_objects"):
        parts.append(f"{len(shape['nested_objects'])} ic ice nesne")
    if not parts:
        return "Bu collection duz gorunuyor: skaler alanlar + istege bagli JSON kopya."
    return "Bu collection'da " + "; ".join(parts) + "."


def _child_table_name(root: str, array_path: str) -> str:
    return f"{root}_{sql_ident(array_path.replace('[]', '').replace('.', '_'))}"


def preview_tables(
    nesting: str, root_table: str, shape: dict[str, Any] | None
) -> tuple[list[str], str]:
    """Approximate SQL tables from a shape peek — not a full plan."""
    root = sql_ident(root_table) if root_table else "tablo"
    if nesting == NESTING_DOCUMENT:
        return [root], "Tek tablo: mongo_id + document JSON."
    if nesting == NESTING_COLUMNS:
        return [root], "Ust seviye skalerler kolon; nesne ve diziler JSON."
    if not shape:
        return [root], "Yapi henuz okunamadi; child tablolar profilde netlesir."
    if nesting == NESTING_HYBRID:
        tables = [root] + [
            _child_table_name(root, path) for path in shape.get("top_arrays") or []
        ]
        nested = list(shape.get("nested_arrays") or [])
        for path in nested[:6]:
            tables.append(f"{path} -> JSON")
        leftover = len(nested) - 6
        if leftover > 0:
            tables.append(f"+{leftover} ic ice dizi -> JSON")
        note = "Kok diziler child tablo."
        if nested or shape.get("nested_objects"):
            note += " Daha derin yapilar JSON kolon."
        return tables, note
    arrays = list(shape.get("top_arrays") or []) + list(shape.get("nested_arrays") or [])
    tables = [root] + [_child_table_name(root, path) for path in arrays]
    return tables, "Diziler child tablo, nesneler kolon."


def sql_ident(raw: str) -> str:
    # Leading underscores are kept on purpose: stripping them turns Mongo's
    # "__v" into "v", which no longer points back at the source field.
    ident = re.sub(r"[^0-9A-Za-z_]+", "_", raw).rstrip("_")
    if not ident.strip("_"):
        ident = ident + "col" if ident else "col"
    if ident[0].isdigit():
        ident = "c_" + ident
    return ident[:120]


def owner_prefix(path: str, map_prefixes: set[str]) -> str:
    """Which table a path's value belongs to: '' for root, else array/map prefix."""
    for prefix in sorted(map_prefixes, key=len, reverse=True):
        if path.startswith(prefix + "."):
            return prefix
    last = path.rfind("[]")
    if last == -1:
        return ""
    return path[: last + 2]


def idx_columns(element_prefix: str) -> list[str]:
    """One ordinal column per array level, so nested arrays keep a composite key."""
    names: list[str] = []
    for match in re.finditer(r"\[\]", element_prefix):
        segment = element_prefix[: match.start()]
        leaf = segment.split(".")[-1].replace("[]", "")
        candidate = sql_ident(leaf) + "_idx"
        while candidate in names:
            candidate = candidate[:-4] + "_x_idx"
        names.append(candidate)
    return names


def column_name(path: str, owner: str) -> str:
    relative = path[len(owner):] if owner and path.startswith(owner) else path
    relative = relative.lstrip(".").replace("[]", "")
    if not relative:
        return "value"
    if relative == "_id" and not owner:
        return "mongo_id"
    return sql_ident(relative.replace(".", "_"))


def parent_key_name(collection: str) -> str:
    stem = collection[:-1] if collection.endswith("s") else collection
    return sql_ident(stem) + "_id"


def build_plan(
    profile: Profile,
    collection: str,
    schema: str,
    maps: dict[str, dict[str, Any]],
    headroom: float,
    table_prefix: str | None = None,
    nesting: str = NESTING_DEEP,
) -> dict[str, Any]:
    if nesting not in {item[0] for item in NESTING_OPTIONS}:
        nesting = NESTING_DEEP
    root_table = sql_ident(collection)
    child_prefix = sql_ident(table_prefix) if table_prefix else root_table
    parent_key = parent_key_name(collection)

    shape_conflicts = {
        stat.path
        for stat in profile.stats.values()
        if profile.children_of(stat.path)
        and stat.concrete_types
        and set(stat.concrete_types) - {"object"}
    }
    json_parents = set(shape_conflicts)

    if nesting == NESTING_DOCUMENT:
        id_stat = profile.stats.get("_id")
        id_sql = "NVARCHAR(450)"
        id_notes: list[str] = []
        if id_stat:
            id_sql, id_notes = sql_type_for(id_stat, headroom)
            if id_sql == "NVARCHAR(MAX)":
                id_sql = f"NVARCHAR({INDEX_KEY_LIMIT})"
        columns = [
            {
                "path": "_id",
                "name": "mongo_id",
                "sql_type": id_sql,
                "nullable": False,
                "fill": 1.0,
                "present": profile.documents,
                "types": id_stat.concrete_types if id_stat else {"objectId": profile.documents},
                "max_utf16": id_stat.max_utf16 if id_stat else 24,
                "notes": id_notes,
                "longest_sample": id_stat.longest_sample if id_stat else "",
                "distinct": None,
            },
            {
                "path": "",
                "name": "document",
                "sql_type": "NVARCHAR(MAX)",
                "nullable": True,
                "fill": 1.0,
                "present": profile.documents,
                "types": {"object": profile.documents},
                "max_utf16": 0,
                "notes": ["tum dokuman JSON olarak saklanir"],
                "longest_sample": "",
                "distinct": None,
            },
        ]
        return {
            "schema": schema,
            "collection": collection,
            "documents": profile.documents,
            "nesting": nesting,
            "root": {"table": root_table, "key_column": "mongo_id", "columns": columns},
            "children": [],
        }

    if nesting == NESTING_COLUMNS:
        for stat in profile.stats.values():
            if is_top_level(stat.path) and stat.dominant_type in {"object", "array"}:
                json_parents.add(stat.path)
        maps = {}
    elif nesting == NESTING_HYBRID:
        for stat in profile.stats.values():
            if is_top_level(stat.path) and stat.dominant_type == "object" and profile.children_of(stat.path):
                json_parents.add(stat.path)
            if stat.dominant_type == "array" and not is_top_level(stat.path):
                json_parents.add(stat.path)
            owner = owner_prefix(stat.path, set())
            if owner.endswith("[]") and stat.dominant_type == "object" and profile.children_of(stat.path):
                json_parents.add(stat.path)
        maps = {}

    map_prefixes = set(maps)

    def suppressed(path: str) -> bool:
        for parent in json_parents:
            if path.startswith(parent + ".") or path.startswith(parent + "[]"):
                return True
        return False

    map_prefixes = {prefix for prefix in map_prefixes if not suppressed(prefix)}

    array_paths: list[str] = []
    if nesting == NESTING_DEEP:
        array_paths = [
            stat.path
            for stat in profile.stats.values()
            if stat.dominant_type == "array"
            and owner_prefix(stat.path, map_prefixes) not in map_prefixes
            and not suppressed(stat.path)
            and stat.path not in json_parents
        ]
    elif nesting == NESTING_HYBRID:
        array_paths = [
            stat.path
            for stat in profile.stats.values()
            if stat.dominant_type == "array"
            and is_top_level(stat.path)
            and not suppressed(stat.path)
            and stat.path not in json_parents
        ]

    groups: dict[str, list[FieldStats]] = {"": []}
    for array_path in array_paths:
        groups[array_path + "[]"] = []

    for stat in profile.stats.values():
        if suppressed(stat.path):
            continue
        owner = owner_prefix(stat.path, map_prefixes)
        if owner in map_prefixes:
            continue
        if stat.path in json_parents:
            groups.setdefault(owner, []).append(stat)
            continue
        if stat.dominant_type == "object" and profile.children_of(stat.path):
            continue
        if stat.dominant_type == "array":
            continue
        groups.setdefault(owner, []).append(stat)

    def columns_for(owner: str) -> list[dict[str, Any]]:
        out = []
        used: set[str] = set()
        for stat in groups.get(owner, []):
            as_json = stat.path in json_parents and stat.path not in shape_conflicts
            sql, notes = sql_type_for(
                stat,
                headroom,
                stat.path in shape_conflicts,
                as_json=as_json,
            )
            name = column_name(stat.path, owner)
            while name in used:
                name = name + "_x"
            used.add(name)
            fill = profile.fill_ratio(stat)
            out.append(
                {
                    "path": stat.path,
                    "name": name,
                    "sql_type": sql,
                    "nullable": stat.nulls > 0 or (fill is not None and fill < 1.0),
                    "fill": fill,
                    "present": stat.present,
                    "types": stat.concrete_types,
                    "max_utf16": stat.max_utf16,
                    "notes": notes,
                    "longest_sample": stat.longest_sample,
                    "distinct": None if stat.distinct_overflow else sorted(stat.distinct),
                }
            )
        return out

    children: list[dict[str, Any]] = []
    for array_path in sorted(array_paths):
        element_prefix = array_path + "[]"
        array_stat = profile.stats[array_path]
        children.append(
            {
                "table": f"{child_prefix}_{sql_ident(array_path.replace('[]', '').replace('.', '_'))}",
                "kind": "array",
                "source": array_path,
                "parent_key": parent_key,
                "idx_columns": idx_columns(element_prefix),
                "max_array_len": array_stat.array_max_len,
                "total_elements": array_stat.array_total,
                "columns": columns_for(element_prefix),
            }
        )

    if nesting == NESTING_DEEP:
        for map_path, info in sorted(maps.items()):
            if map_path not in map_prefixes:
                continue
            base = sql_ident(map_path.replace("[]", "").replace(".", "_"))
            value_kinds = set(info["value_types"])
            value_type = (
                "NVARCHAR(MAX)"
                if value_kinds - {"string"}
                else nvarchar_width(max(info["value_max_utf16"], 170), headroom)
            )
            children.append(
                {
                    "table": f"{child_prefix}_{base}",
                    "kind": "map",
                    "source": map_path,
                    "parent_key": parent_key,
                    "key_column": {
                        "name": f"{base}_key",
                        "sql_type": nvarchar_width(max(info["key_max_utf16"], 85), headroom),
                    },
                    "value_column": {"name": f"{base}_value", "sql_type": value_type},
                    "keys_seen": info["keys"],
                    "avg_fill": info["avg_fill"],
                    "value_types": info["value_types"],
                    "columns": [],
                }
            )

    return {
        "schema": schema,
        "collection": collection,
        "documents": profile.documents,
        "nesting": nesting,
        "root": {"table": root_table, "key_column": "mongo_id", "columns": columns_for("")},
        "children": children,
    }


# --------------------------------------------------------------------------
# console report
# --------------------------------------------------------------------------


def _fill_text(column: dict[str, Any]) -> str:
    fill = column.get("fill")
    if fill is None:
        return f"{column['present']} adet"
    return f"%{100 * fill:.1f}"


def _types_text(types: dict[str, int]) -> str:
    return "+".join(sorted(types, key=lambda name: -types[name]))


def print_report(plan: dict[str, Any], profile: Profile, sampled: int | None) -> None:
    print(f"collection={plan['collection']} documents={plan['documents']} paths={len(profile.stats)}")
    if sampled:
        print(
            f"\n[UYARI] --sample {sampled} kullanildi. Buradaki maksimum uzunluklar ALT SINIRDIR;"
            "\n        ornekte gorunmeyen daha uzun degerler olabilir. Kesin genislik icin tam tarama sart."
        )
    if profile.paths_truncated:
        print("[UYARI] --max-paths sinirina ulasildi, bazi alanlar profillenmedi.")

    conflicts = [
        column
        for group in [plan["root"]["columns"]] + [child["columns"] for child in plan["children"]]
        for column in group
        if any("cakismasi" in note for note in column["notes"])
    ]
    if conflicts:
        print("\n[!] TIP CAKISMALARI")
        for column in conflicts:
            print(f"  {column['path']}")
            for note in column["notes"]:
                print(f"      {note}")

    maps = [child for child in plan["children"] if child["kind"] == "map"]
    if maps:
        print("\n[MAP] anahtarlari veri olan nesneler -> anahtar/deger child tablosu")
        for child in maps:
            print(
                f"  {child['source']}: {child['keys_seen']} anahtar, "
                f"ortalama doluluk %{100 * child['avg_fill']:.1f} "
                f"({_types_text(child['value_types'])})"
            )
            print(
                f"      {child['table']}({child['parent_key']}, "
                f"{child['key_column']['name']}, {child['value_column']['name']})"
            )

    def print_columns(title: str, columns: list[dict[str, Any]]) -> None:
        print(f"\n{title}")
        if not columns:
            print("  (kolon yok)")
            return
        print(f"  {'path':<64} {'doluluk':>9} {'tip':<16} {'max':>6}  SQL")
        for column in columns:
            print(
                f"  {column['path'][:64]:<64} {_fill_text(column):>9} "
                f"{_types_text(column['types'])[:16]:<16} {column['max_utf16']:>6}  {column['sql_type']}"
                f"{'' if column['nullable'] else ' NOT NULL'}"
            )
            for note in column["notes"]:
                if "cakismasi" in note:
                    continue
                print(f"      -> {note}")
            if column["max_utf16"] > INDEX_KEY_LIMIT and column["longest_sample"]:
                print(f"      en uzun: {safe_console(column['longest_sample'])}")

    print_columns(f"[KOK TABLO] {plan['root']['table']}", plan["root"]["columns"])
    for child in plan["children"]:
        if child["kind"] == "map":
            continue
        header = (
            f"[CHILD] {child['table']}  <- {child['source']} "
            f"(max {child['max_array_len']} eleman, toplam {child['total_elements']})"
        )
        print_columns(header, child["columns"])

    print(
        f"\nOzet: 1 kok tablo + {len(plan['children'])} child tablo "
        f"({len(maps)} tanesi anahtar/deger)"
    )


# --------------------------------------------------------------------------
# file outputs
# --------------------------------------------------------------------------


def key_column_type(plan: dict[str, Any]) -> str:
    """Type used for the root primary key and every child foreign key."""
    root = plan["root"]
    key_column = next((c for c in root["columns"] if c["name"] == "mongo_id"), None)
    key_type = key_column["sql_type"] if key_column else "NVARCHAR(450)"
    if key_type == "NVARCHAR(MAX)":
        key_type = f"NVARCHAR({INDEX_KEY_LIMIT})"
    return key_type


def ddl_statements(plan: dict[str, Any]) -> list[tuple[str, str]]:
    """(table, CREATE TABLE ...) per table, without the trailing semicolon."""
    schema = plan["schema"]
    root = plan["root"]
    key_type = key_column_type(plan)
    out: list[tuple[str, str]] = []

    body = []
    for column in root["columns"]:
        sql = key_type if column["name"] == "mongo_id" else column["sql_type"]
        null = "NULL" if column["nullable"] and column["name"] != "mongo_id" else "NOT NULL"
        body.append(f"    [{column['name']}] {sql} {null}")
    body.append(f"    CONSTRAINT [PK_{root['table']}] PRIMARY KEY ([mongo_id])")
    out.append(
        (
            root["table"],
            f"CREATE TABLE [{schema}].[{root['table']}] (\n" + ",\n".join(body) + "\n)",
        )
    )

    for child in plan["children"]:
        body = [f"    [{child['parent_key']}] {key_type} NOT NULL"]
        if child["kind"] == "map":
            body.append(f"    [{child['key_column']['name']}] {child['key_column']['sql_type']} NOT NULL")
            body.append(f"    [{child['value_column']['name']}] {child['value_column']['sql_type']} NULL")
            key_parts = [child["parent_key"], child["key_column"]["name"]]
        else:
            for name in child["idx_columns"]:
                body.append(f"    [{name}] INT NOT NULL")
            for column in child["columns"]:
                null = "NULL" if column["nullable"] else "NOT NULL"
                body.append(f"    [{column['name']}] {column['sql_type']} {null}")
            key_parts = [child["parent_key"], *child["idx_columns"]]
        joined = ", ".join(f"[{part}]" for part in key_parts)
        body.append(f"    CONSTRAINT [PK_{child['table']}] PRIMARY KEY ({joined})")
        body.append(
            f"    CONSTRAINT [FK_{child['table']}] FOREIGN KEY ([{child['parent_key']}]) "
            f"REFERENCES [{schema}].[{root['table']}] ([mongo_id]) ON DELETE CASCADE"
        )
        out.append(
            (
                child["table"],
                f"CREATE TABLE [{schema}].[{child['table']}] (\n" + ",\n".join(body) + "\n)",
            )
        )

    return out


def render_ddl(plan: dict[str, Any]) -> str:
    lines = [
        f"-- {plan['collection']} icin taslak sema ({plan['documents']} dokuman profillendi)",
        "-- Uretilmis oneri; uygulamadan once gozden gecirin.",
        "",
    ]
    for _, statement in ddl_statements(plan):
        lines.append(statement + ";")
        lines.append("")
    return "\n".join(lines)


def _drdl_tables(plan: dict[str, Any]) -> list[str]:
    collection = plan["collection"]
    lines: list[str] = []

    def emit_columns(columns: list[dict[str, Any]], indent: str) -> None:
        lines.append(f"{indent}columns:")
        for column in columns:
            mongo_path = column["path"].replace("[]", "")
            lines.append(f"{indent}- Name: {mongo_path}")
            lines.append(f"{indent}  MongoType: {_types_text(column['types']) or 'string'}")
            lines.append(f"{indent}  SqlName: {column['name']}")
            lines.append(f"{indent}  SqlType: {drdl_type(column['sql_type'])}")

    lines.append(f"  - table: {plan['root']['table']}")
    lines.append(f"    collection: {collection}")
    lines.append("    pipeline: []")
    emit_columns(plan["root"]["columns"], "    ")

    for child in plan["children"]:
        lines.append(f"  - table: {child['table']}")
        lines.append(f"    collection: {collection}")
        if child["kind"] == "map":
            lines.append("    pipeline:")
            lines.append(f"    - $addFields:")
            lines.append(f"        _kv: {{$objectToArray: ${child['source']}}}")
            lines.append("    - $unwind:")
            lines.append("        path: $_kv")
            columns = [
                {"path": "_id", "types": {"string": 1}, "name": child["parent_key"], "sql_type": "NVARCHAR(450)"},
                {"path": "_kv.k", "types": {"string": 1}, "name": child["key_column"]["name"], "sql_type": child["key_column"]["sql_type"]},
                {"path": "_kv.v", "types": child["value_types"], "name": child["value_column"]["name"], "sql_type": child["value_column"]["sql_type"]},
            ]
            emit_columns(columns, "    ")
            continue

        # One stage per array level: MongoDB cannot $unwind a path that traverses
        # an array it has not unwound yet, so nested arrays need a stage each.
        lines.append("    pipeline:")
        segments = child["source"].split("[]")
        for level, idx_name in enumerate(child["idx_columns"]):
            lines.append("    - $unwind:")
            lines.append(f"        path: ${''.join(segments[: level + 1])}")
            lines.append(f"        includeArrayIndex: {idx_name}")
        columns = [
            {"path": "_id", "types": {"string": 1}, "name": child["parent_key"], "sql_type": "NVARCHAR(450)"},
            *child["columns"],
        ]
        emit_columns(columns, "    ")

    return lines


def render_database_drdl(plans: list[dict[str, Any]], database: str) -> str:
    """One DRDL schema with tables from every collection plan."""
    lines = [
        "# mongodrdl uyumlu sekilde uretildi; BI Connector 2026-09 sonrasi EOL.",
        "schema:",
        f"- db: {database}",
        "  tables:",
    ]
    for plan in plans:
        lines.extend(_drdl_tables(plan))
    return "\n".join(lines) + "\n"


def render_drdl(plan: dict[str, Any], database: str) -> str:
    """
    DRDL-shaped YAML, for familiarity and for feeding BI tooling.

    Emitted by hand rather than via a yaml dependency for the pipeline blocks,
    which need MongoDB's `$unwind` spelling preserved exactly.
    """
    return render_database_drdl([plan], database)


def render_database_ddl(plans: list[dict[str, Any]]) -> str:
    return "\n".join(render_ddl(plan) for plan in plans)


# --------------------------------------------------------------------------
# document sources
# --------------------------------------------------------------------------


def iter_from_file(path: Path) -> Iterator[dict[str, Any]]:
    """Read mongoexport output: one extended-JSON document per line, or a JSON array."""
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        for doc in json_util.loads(stripped):
            yield doc
        return
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json_util.loads(line)


def iter_from_mongo(
    source: MongoClientWrapper, collection: str, sample: int
) -> Iterator[dict[str, Any]]:
    yield from source.iter_documents(collection, sample=sample)


def profile_collection(
    source: MongoClientWrapper,
    collection: str,
    sample: int,
    schema: str,
    map_min_keys: int,
    map_max_fill: float,
    headroom: float,
    nesting: str = NESTING_DEEP,
) -> tuple[Profile, dict[str, Any]]:
    profile = Profile()
    for doc in iter_from_mongo(source, collection, sample):
        profile.add_document(doc)
    maps = detect_map_prefixes(profile, map_min_keys, map_max_fill)
    return profile, build_plan(
        profile, collection, schema, maps, headroom, nesting=nesting
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Profile a MongoDB collection and propose a relational schema."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--collection",
        type=str,
        default=None,
        help="MongoDB collection to profile (required unless --from-file).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Profile a random $sample of N documents instead of a full scan. "
        "Faster, but reported max lengths become lower bounds.",
    )
    parser.add_argument(
        "--from-file",
        type=str,
        default=None,
        help="Read documents from a mongoexport JSON file instead of connecting.",
    )
    parser.add_argument("--schema", default=None, help="Target MSSQL schema (default from config)")
    parser.add_argument(
        "--table-prefix",
        default=None,
        help="Prefix for child table names (default: the root table name). "
        "Use e.g. --table-prefix conv to match this project's existing naming.",
    )
    parser.add_argument("--out-json", type=str, default=None)
    parser.add_argument("--out-drdl", type=str, default=None)
    parser.add_argument("--out-ddl", type=str, default=None)
    parser.add_argument("--headroom", type=float, default=1.5, help="Width safety factor")
    parser.add_argument("--distinct-limit", type=int, default=50)
    parser.add_argument("--preview-len", type=int, default=80)
    parser.add_argument("--max-paths", type=int, default=4000)
    parser.add_argument(
        "--map-min-keys",
        type=int,
        default=30,
        help="Minimum distinct keys before an object is treated as a map.",
    )
    parser.add_argument(
        "--map-max-fill",
        type=float,
        default=0.2,
        help="Maximum average key fill ratio for the map heuristic.",
    )
    parser.add_argument(
        "--nesting",
        choices=[item[0] for item in NESTING_OPTIONS],
        default=NESTING_DEEP,
        help="How nested objects/arrays become tables: "
        "deep (child tables), hybrid (top arrays + JSON), "
        "columns (one table, nested JSON), document (mongo_id + JSON).",
    )
    parser.add_argument("--progress-every", type=int, default=1000)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    profile = Profile(
        preview_len=args.preview_len,
        distinct_limit=args.distinct_limit,
        max_paths=args.max_paths,
    )

    source: MongoClientWrapper | None = None
    if args.from_file:
        file_path = Path(args.from_file)
        if not file_path.exists():
            print(f"Dosya bulunamadi: {file_path}", file=sys.stderr)
            return 2
        collection = args.collection or file_path.stem
        schema = args.schema or "dbo"
        database = "mongo"
        documents = iter_from_file(file_path)
    else:
        if not args.collection:
            print("--collection gerekli (veya --from-file kullanin).", file=sys.stderr)
            return 2
        cfg = load_settings(args.config)
        mcfg = cfg.get("mongodb") or {}
        scfg = cfg.get("mssql") or {}
        collection = args.collection
        schema = args.schema or scfg.get("schema", "dbo")
        database = mcfg.get("database", "")
        if not mcfg.get("uri") or not mcfg.get("database"):
            print("config.yaml / config.local.yaml icinde mongodb.uri ve mongodb.database gerekli.", file=sys.stderr)
            return 2
        source = MongoClientWrapper(
            uri=mcfg["uri"],
            database=mcfg["database"],
            username=mcfg.get("username") or None,
            password=mcfg.get("password") or None,
        )
        source.connect()
        documents = iter_from_mongo(source, collection, args.sample)

    try:
        for doc in documents:
            profile.add_document(doc)
            if args.progress_every and profile.documents % args.progress_every == 0:
                print(f"... {profile.documents} dokuman", file=sys.stderr)
    finally:
        if source is not None:
            source.close()

    if not profile.documents:
        print("Hic dokuman okunamadi.", file=sys.stderr)
        return 1

    maps = detect_map_prefixes(profile, args.map_min_keys, args.map_max_fill)
    plan = build_plan(
        profile,
        collection,
        schema,
        maps,
        args.headroom,
        args.table_prefix,
        nesting=args.nesting,
    )

    print_report(plan, profile, args.sample or None)

    if args.out_json:
        Path(args.out_json).write_text(
            json.dumps(plan, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        print(f"\nJSON mapping -> {args.out_json}")
    if args.out_drdl:
        Path(args.out_drdl).write_text(render_drdl(plan, database), encoding="utf-8")
        print(f"DRDL -> {args.out_drdl}")
    if args.out_ddl:
        Path(args.out_ddl).write_text(render_ddl(plan), encoding="utf-8")
        print(f"DDL -> {args.out_ddl}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
