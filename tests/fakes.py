"""
Stand-ins for MongoDB and SQL Server, enough to drive the loader end to end.

`FakeCollection` keeps documents in BSON `_id` order and honours what the
chunked reader asks for (hint, sort, min / max bounds, skip, limit, a simple
range filter, projection, raw documents), and it can raise driver errors at
chosen documents. `FakeServer` / `FakeOps` play the writer's SQL side with
real transactions: rows and the checkpoint change only on commit, primary
keys are enforced, and errors can be injected before or after a commit.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable

import bson
import pyodbc
from bson import Decimal128, MaxKey, MinKey, ObjectId
from bson.raw_bson import RawBSONDocument

from core import checkpoint as cp
from core.transfer import plan_column_specs

# --------------------------------------------------------------------------
# errors as the drivers raise them
# --------------------------------------------------------------------------


def link_failure() -> pyodbc.Error:
    return pyodbc.OperationalError(
        "08S01",
        "[08S01] [Microsoft][ODBC Driver 17 for SQL Server]TCP Provider: An existing connection "
        "was forcibly closed by the remote host. (10054) (SQLExecDirectW)",
    )


def deadlock() -> pyodbc.Error:
    return pyodbc.Error(
        "40001",
        "[40001] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Transaction (Process ID 71) "
        "was deadlocked on lock resources with another process and has been chosen as the "
        "deadlock victim. Rerun the transaction. (1205) (SQLExecDirectW)",
    )


def log_full() -> pyodbc.Error:
    return pyodbc.ProgrammingError(
        "42000",
        "[42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]The transaction log for "
        "database 'x' is full due to 'LOG_BACKUP'. (9002) (SQLExecDirectW)",
    )


def overflow_error() -> pyodbc.Error:
    return pyodbc.DataError(
        "22003",
        "[22003] [Microsoft][ODBC Driver 17 for SQL Server]Numeric value out of range (0) (SQLExecute)",
    )


def duplicate_key(table: str, key: Any) -> pyodbc.Error:
    return pyodbc.IntegrityError(
        "23000",
        f"[23000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Violation of PRIMARY KEY "
        f"constraint 'PK_{table}'. Cannot insert duplicate key in object 'dbo.{table}'. The "
        f"duplicate key value is ({key}). (2627) (SQLExecute)",
    )


def missing_table() -> pyodbc.Error:
    return pyodbc.ProgrammingError(
        "42S02",
        "[42S02] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Invalid object name "
        "'dbo.Nope'. (208) (SQLExecDirectW)",
    )


def bind_error() -> pyodbc.Error:
    return pyodbc.Error("HY104", "[HY104] [Microsoft][ODBC Driver 17 for SQL Server]Invalid precision value (0) (SQLBindParameter)")


# --------------------------------------------------------------------------
# MongoDB
# --------------------------------------------------------------------------

_RANKS = (
    (bool, 8),
    (int, 2),
    (float, 2),
    (Decimal128, 2),
    (str, 3),
    (dict, 4),
    (list, 5),
    (bytes, 6),
    (ObjectId, 7),
    (datetime, 9),
)


def bson_key(value: Any) -> tuple:
    """Sort key following MongoDB's cross-type order for `_id` values."""
    if isinstance(value, MinKey):
        return (0,)
    if isinstance(value, MaxKey):
        return (100,)
    if value is None:
        return (1,)
    for kind, rank in _RANKS:
        if isinstance(value, kind):
            break
    else:
        raise TypeError(f"no BSON order for {type(value)!r}")
    if rank == 2:
        number = value.to_decimal() if isinstance(value, Decimal128) else Decimal(value)
        return (2, number)
    if rank == 3:
        return (3, value.encode("utf-8"))
    if rank == 7:
        return (7, value.binary)
    if rank == 9:
        moment = value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)
        return (9, moment)
    return (rank, value)


def _naive(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _field(doc: dict[str, Any], path: str) -> Any:
    current: Any = doc
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def matches(doc: dict[str, Any] | None, query: dict[str, Any]) -> bool:
    """`{}`, `$and`, and `$gt/$gte/$lt/$lte` on one field, with type bracketing."""
    if not query:
        return True
    if doc is None:
        return False
    for key, condition in query.items():
        if key == "$and":
            if not all(matches(doc, part) for part in condition):
                return False
            continue
        value = _naive(_field(doc, key))
        if not isinstance(condition, dict):
            if value != _naive(condition):
                return False
            continue
        for op, bound in condition.items():
            bound = _naive(bound)
            if value is None or bson_key(value)[0] != bson_key(bound)[0]:
                return False
            left, right = bson_key(value), bson_key(bound)
            ok = {"$gt": left > right, "$gte": left >= right, "$lt": left < right, "$lte": left <= right}.get(op)
            if ok is None:
                raise NotImplementedError(op)
            if not ok:
                return False
    return True


@dataclass
class _Entry:
    key: tuple
    id: Any
    doc: dict[str, Any] | None  # None for a document that will not decode
    raw: bytes


@dataclass
class MongoStore:
    entries: list[_Entry] = field(default_factory=list)
    # {n: exception}: raised when the n-th document (1-based, all data cursors) is about to arrive
    faults: dict[int, BaseException] = field(default_factory=dict)
    delivered: int = 0
    cursors: int = 0

    def add(self, doc: dict[str, Any]) -> None:
        raw = bson.encode(doc)
        self._insert(_Entry(bson_key(doc["_id"]), doc["_id"], bson.decode(raw), raw))

    def add_raw(self, mongo_id: Any, raw: bytes) -> None:
        self._insert(_Entry(bson_key(mongo_id), mongo_id, None, raw))

    def _insert(self, entry: _Entry) -> None:
        # The `_id` index is unique (and 5 == 5.0 there).
        assert all(other.key != entry.key for other in self.entries), f"duplicate _id {entry.id!r}"
        self.entries.append(entry)
        self.entries.sort(key=lambda item: item.key)


def undecodable(mongo_id: Any) -> bytes:
    """A document whose `_id` is fine and whose next element has an unknown BSON type."""
    head = bson.encode({"_id": mongo_id})[4:-1]
    element = b"\x42bad\x00" + b"\x00" * 4  # type 0x42 does not exist
    body = head + element + b"\x00"
    return (len(body) + 4).to_bytes(4, "little") + body


class FakeCursor:
    def __init__(self, store: MongoStore, query: dict[str, Any], projection: dict[str, int] | None, raw: bool):
        self.store = store
        self.query = query or {}
        self.projection = projection
        self.raw = raw
        self._hint = None
        self._min = None
        self._max = None
        self._skip = 0
        self._limit = 0
        self.closed = False

    def hint(self, spec):
        self._hint = spec
        return self

    def sort(self, key, direction=1):
        assert key == "_id" and direction == 1, "the loader reads in ascending _id order"
        return self

    def batch_size(self, size):
        return self

    def max_time_ms(self, ms):
        return self

    def comment(self, text):
        return self

    def min(self, spec):
        assert self._hint, "min() needs hint()"
        self._min = spec[0][1]
        return self

    def max(self, spec):
        assert self._hint, "max() needs hint()"
        self._max = spec[0][1]
        return self

    def skip(self, count):
        self._skip = count
        return self

    def limit(self, count):
        self._limit = count
        return self

    def close(self):
        self.closed = True

    def _project(self, doc: dict[str, Any]) -> dict[str, Any]:
        if not self.projection:
            return doc
        wanted = {name for name, flag in self.projection.items() if flag}
        return {key: value for key, value in doc.items() if key in wanted or key == "_id"}

    def __iter__(self):
        self.store.cursors += 1
        low = bson_key(self._min) if self._min is not None else None
        high = bson_key(self._max) if self._max is not None else None
        picked = [
            entry
            for entry in self.store.entries
            if (low is None or entry.key >= low)
            and (high is None or entry.key < high)
            and (entry.doc is None and not self.query or matches(entry.doc, self.query))
        ]
        picked = picked[self._skip :]
        if self._limit:
            picked = picked[: self._limit]
        for entry in picked:
            if self.closed:
                return
            if self.raw:
                number = self.store.delivered + 1
                fault = self.store.faults.pop(number, None)
                if fault is not None:
                    raise fault
                self.store.delivered = number
                if entry.doc is None:
                    yield RawBSONDocument(entry.raw)
                else:
                    yield RawBSONDocument(bson.encode(self._project(entry.doc)))
            else:
                doc = {"_id": entry.id} if entry.doc is None else self._project(entry.doc)
                yield copy.deepcopy(doc)


class FakeCollection:
    def __init__(self, store: MongoStore | None = None, *, raw: bool = False):
        self.store = store or MongoStore()
        self.raw = raw

    def with_options(self, codec_options=None, **_):
        raw = codec_options is not None and codec_options.document_class is RawBSONDocument
        return FakeCollection(self.store, raw=raw)

    def find(self, query=None, projection=None):
        return FakeCursor(self.store, query or {}, projection, self.raw)

    def index_information(self):
        return {"_id_": {"key": [("_id", 1)]}}

    def estimated_document_count(self):
        return len(self.store.entries)

    def count_documents(self, query, **_):
        return sum(1 for entry in self.store.entries if matches(entry.doc, query))


class FakeMongo:
    """The part of `MongoClientWrapper` the loader touches."""

    def __init__(self, collections: dict[str, FakeCollection]):
        self.collections = collections

    def collection(self, name: str) -> FakeCollection:
        return self.collections[name]

    def estimated_count(self, name: str, query=None, max_time_ms=None):
        col = self.collections[name]
        return col.count_documents(query) if query else col.estimated_document_count()


# --------------------------------------------------------------------------
# SQL Server
# --------------------------------------------------------------------------


def table_keys(plan: dict[str, Any]) -> dict[str, list[str]]:
    keys = {plan["root"]["table"]: ["mongo_id"]}
    for child in plan["children"]:
        if child["kind"] == "map":
            keys[child["table"]] = [child["parent_key"], child["key_column"]["name"]]
        else:
            keys[child["table"]] = [child["parent_key"], *child["idx_columns"]]
    return keys


class FakeServer:
    """Committed state shared by every session: tables, checkpoint row, lock."""

    def __init__(self, plan: dict[str, Any]):
        self.root = plan["root"]["table"]
        self.columns = {table: [name for name, _ in specs] for table, specs in plan_column_specs(plan)}
        keys = table_keys(plan)
        self.key_index = {table: [self.columns[table].index(name) for name in keys[table]] for table in self.columns}
        self.parent_index = {
            child["table"]: self.columns[child["table"]].index(child["parent_key"]) for child in plan["children"]
        }
        self.tables: dict[str, dict[tuple, tuple]] = {table: {} for table in self.columns}
        self.checkpoint: dict[str, Any] | None = None
        self.commits = 0
        self.inserts = 0
        # {commit number: "before" | "after"}: the link drops before the commit
        # reaches the server, or after it landed (only the reply is lost).
        self.commit_faults: dict[int, str] = {}
        # {insert call number: exception}
        self.insert_faults: dict[int, BaseException] = {}
        # insert calls with fast_executemany raise a bind error for these tables
        self.bind_fails: set[str] = set()
        # any cell equal to one of these makes SQL Server refuse the row
        self.poison: set[Any] = set()
        self.lock_owner: int | None = None
        self.sessions = 0
        self.log: list[tuple[str, str]] = []

    def begin_run(self, run_id: str, **values: Any) -> None:
        self.checkpoint = {
            "run_id": run_id,
            "last_id": cp.MISSING,
            "docs_done": 0,
            "docs_written": 0,
            "rows_written": 0,
            "rejected": 0,
            "clipped": 0,
            **values,
        }

    def root_keys(self) -> list[Any]:
        return [key[0] for key in self.tables[self.root]]

    def row_total(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


class FakeDb:
    """What `transfer_collection` asks the SQL connection directly."""

    def __init__(self, server: FakeServer):
        self.server = server

    def column_info(self, schema, table):
        return {}

    def column_names(self, schema, table):
        return set()

    def non_cascading_children(self, schema, root, children):
        return []


class FakeOps:
    """`core.writer.LiveSql` with in-memory transactions."""

    def __init__(self, server: FakeServer, schema: str = "dbo"):
        self.server = server
        self.schema = schema
        self.root_table = server.root
        self.db: FakeDb | None = None
        self.run_id: str | None = None
        self.key_type = "CHAR(24)"
        self.session = 0
        self.reconnects = 0
        self.slow_inserts: list[str] = []
        self.prepared: dict[str, list[str]] = {}
        self._reset_tx()

    def _reset_tx(self) -> None:
        self._inserted: dict[str, dict[tuple, tuple]] = {table: {} for table in self.server.columns}
        self._deleted: dict[str, set[tuple]] = {table: set() for table in self.server.columns}
        self._advance: dict[str, Any] | None = None

    # -- session -----------------------------------------------------------

    def open(self, lock_wait_ms: int = 0) -> None:
        self.server.sessions += 1
        self.session = self.server.sessions
        self.server.lock_owner = self.session
        self.db = FakeDb(self.server)
        self._reset_tx()

    def reconnect(self) -> None:
        self.reconnects += 1
        self.close()
        self.open()

    def close(self) -> None:
        self._reset_tx()
        self.db = None

    def commit(self) -> None:
        server = self.server
        server.commits += 1
        fault = server.commit_faults.pop(server.commits, None)
        if fault == "before":
            self._reset_tx()
            raise link_failure()
        for table, keys in self._deleted.items():
            for key in keys:
                server.tables[table].pop(key, None)
        for table, rows in self._inserted.items():
            server.tables[table].update(rows)
        if self._advance is not None:
            values = self._advance
            point = server.checkpoint
            point["last_id"] = values["last_id"]
            point["docs_done"] = values["docs_done"]
            for name, total in (
                ("written", "docs_written"),
                ("rows", "rows_written"),
                ("rejected", "rejected"),
                ("clipped", "clipped"),
            ):
                point[total] += values[name]
        self._reset_tx()
        if fault == "after":
            raise link_failure()

    def rollback(self) -> None:
        self._reset_tx()

    # -- statements --------------------------------------------------------

    def prepare_table(self, table, columns, sql_types) -> None:
        assert list(columns) == self.server.columns[table], (table, columns)
        self.prepared[table] = list(columns)

    def forget_cursor(self, table) -> None:
        pass

    def _live(self, table: str, key: tuple) -> bool:
        if key in self._inserted[table]:
            return True
        return key in self.server.tables[table] and key not in self._deleted[table]

    def insert(self, table, rows, slow) -> None:
        server = self.server
        server.inserts += 1
        fault = server.insert_faults.pop(server.inserts, None)
        if fault is not None:
            raise fault
        if table in server.bind_fails and not slow:
            raise bind_error()
        if slow:
            self.slow_inserts.append(table)
        width = len(server.columns[table])
        index = server.key_index[table]
        for row in rows:
            assert len(row) == width, (table, len(row), width)
            if any(_poisoned(value, server.poison) for value in row):
                raise overflow_error()
            key = tuple(row[i] for i in index)
            if self._live(table, key):
                raise duplicate_key(table, key)
            self._inserted[table][key] = tuple(row)

    def delete(self, table, key_column, keys) -> None:
        server = self.server
        wanted = set(keys)
        if table == server.root:
            for key in list(server.tables[table]):
                if key[0] in wanted:
                    self._deleted[table].add(key)
            for key in list(self._inserted[table]):
                if key[0] in wanted:
                    del self._inserted[table][key]
            for child, position in server.parent_index.items():  # ON DELETE CASCADE
                for key, row in server.tables[child].items():
                    if row[position] in wanted:
                        self._deleted[child].add(key)
                for key, row in list(self._inserted[child].items()):
                    if row[position] in wanted:
                        del self._inserted[child][key]
        else:
            position = server.parent_index[table]
            for key, row in server.tables[table].items():
                if row[position] in wanted:
                    self._deleted[table].add(key)

    def advance(self, **values) -> None:
        if self.run_id is None:
            return
        point = self.server.checkpoint
        if point is None or point["run_id"] != self.run_id:
            raise cp.TakenOver("taken over")
        self._advance = values

    def committed_through(self):
        point = self.server.checkpoint
        if self.run_id is None or point is None or point["run_id"] != self.run_id:
            return None
        return point["docs_done"]

    def key_exists(self, key) -> bool:
        return (key,) in self.server.tables[self.root_table]

    def column_meta(self, table):
        return {}

    def widen(self, *args, **kwargs):
        raise AssertionError("no widening in these tests")


def _poisoned(value: Any, poison: set[Any]) -> bool:
    if not poison:
        return False
    try:
        return value in poison
    except TypeError:
        return False


# --------------------------------------------------------------------------
# SQL text
# --------------------------------------------------------------------------


class RecordingCursor:
    """Checks every statement's `?` count against its parameters and replays canned rows."""

    def __init__(self, conn: "RecordingConnection"):
        self.conn = conn
        self.rowcount = 1
        self.description = None
        self.fast_executemany = False
        self._rows: list[tuple] = []

    def execute(self, sql: str, *params: Any):
        if len(params) == 1 and isinstance(params[0], (list, tuple)):
            params = tuple(params[0])
        assert sql.count("?") == len(params), f"{sql.count('?')} marks, {len(params)} params: {sql}"
        assert sql.count("[") == sql.count("]"), sql
        assert sql.count("(") == sql.count(")"), sql
        self.conn.statements.append((sql, params))
        answer = self.conn.answer(sql, params)
        self._rows = list(answer) if answer is not None else []
        self.description = [("c",)] if answer is not None else None
        self.rowcount = self.conn.rowcount
        return self

    def executemany(self, sql: str, rows: Iterable[Any]):
        for row in rows:
            assert sql.count("?") == len(row), sql
        self.conn.statements.append((sql, ("<many>",)))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def nextset(self):
        return False


class RecordingConnection:
    def __init__(self, answer: Callable[[str, tuple], Any] | None = None):
        self.statements: list[tuple[str, tuple]] = []
        self.answer = answer or (lambda sql, params: None)
        self.rowcount = 1
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class RecordingDb:
    """An `MssqlConnection` whose `conn` records SQL instead of sending it."""

    def __init__(self, answer: Callable[[str, tuple], Any] | None = None):
        self.conn = RecordingConnection(answer)

    def rollback(self):
        self.conn.rollback()
