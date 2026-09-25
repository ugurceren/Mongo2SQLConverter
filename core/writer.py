"""
Batch writer for long transfers: all-or-nothing batches that survive errors.

Every batch is one transaction: optional deletes, the inserts for every
table, and the checkpoint row moved to the batch's last document. Around that:

- transient errors (dropped connection, deadlock, failover) reconnect and try
  the same batch again; if the commit had landed and only its acknowledgement
  was lost, the checkpoint says so and the batch is not written twice;
- data errors split the batch in halves until the one document SQL Server
  refuses is found; it goes to the rejects file and the job carries on;
- a bind error retries that table without fast_executemany once, and after
  three batches in a row keeps it on the slow path for the run;
- text longer than its column widens the column first, and is clipped (and
  recorded) only when widening is not possible.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from core import checkpoint as cp
from core.convert import Column, Note, base_type, kind_of, width_of
from core.inspect import nvarchar_width
from core.mssql import MssqlConnection
from core.rejects import RejectLog
from core.retry import (
    BIND,
    DATA,
    FATAL,
    RESOURCE,
    TRANSIENT,
    UNKNOWN,
    GiveUp,
    Retrier,
    Stopped,
    classify_sql,
    describe,
    sql_codes,
)
from core.textutil import clip_utf16, utf16_len

BIND_STRIKES = 3  # batches in a row before a table stays on the slow path
WIDEN_ATTEMPTS = 3
MAX_CALL_BYTES = 32 * 1024 * 1024


class TooManyRejects(RuntimeError):
    pass


class LockBusy(RuntimeError):
    pass


@dataclass
class Unit:
    """One source document, ready to write (or already known to be unwritable)."""

    seq: int  # 1-based position of the document in this job
    id: Any  # Mongo `_id`
    key: Any = None  # SQL key value
    root: list[Any] | None = None
    children: dict[str, list[list[Any]]] = field(default_factory=dict)
    size: int = 0  # raw BSON bytes
    rows: int = 0
    reject: str | None = None  # reason, when the document will not be written
    stage: str = ""  # where the reject was decided: decode | flatten | sql
    error: str | None = None
    notes: list[Note] = field(default_factory=list)
    overflow: dict[Column, int] = field(default_factory=dict)
    logged: bool = False  # its reject / notes are already in the rejects file


# --------------------------------------------------------------------------
# the database side
# --------------------------------------------------------------------------


class LiveSql:
    """
    The writer's view of SQL Server: one session holding the table lock,
    persistent insert cursors, and the checkpoint row of this run.
    """

    def __init__(
        self,
        factory: Callable[[], MssqlConnection],
        schema: str,
        root_table: str,
        *,
        login_timeout: int = 30,
        query_timeout: int = 600,
        lock_timeout_ms: int = 120_000,
        reconnect_lock_wait_ms: int = 600_000,
    ) -> None:
        self.factory = factory
        self.schema = schema
        self.root_table = root_table
        self.lock_resource = f"mongo2sql:{schema}.{root_table}"
        self.login_timeout = login_timeout
        self.query_timeout = query_timeout
        self.lock_timeout_ms = lock_timeout_ms
        self.reconnect_lock_wait_ms = reconnect_lock_wait_ms
        self.db: MssqlConnection | None = None
        self.run_id: str | None = None  # set once the checkpoint row is ours
        self.key_type = "NVARCHAR(450)"
        self._cursors: dict[tuple[str, bool], Any] = {}
        self._insert_sql: dict[str, str] = {}
        self._rows_per_call: dict[str, int] = {}
        self._meta: dict[str, dict[str, dict[str, Any]]] = {}

    # -- session -----------------------------------------------------------

    def open(self, lock_wait_ms: int = 0) -> None:
        db = self.factory()
        db.connect(login_timeout=self.login_timeout, query_timeout=self.query_timeout)
        try:
            db.prepare_session(self.lock_timeout_ms)
            if not db.get_applock(self.lock_resource, lock_wait_ms):
                raise LockBusy(
                    f"{self.schema}.{self.root_table} tablosuna şu anda başka bir aktarım yazıyor "
                    "(arayüz ya da Görev Zamanlayıcı). O bitince tekrar deneyin."
                )
        except BaseException:
            db.close()
            raise
        self.db = db
        self._cursors.clear()

    def reconnect(self) -> None:
        """
        Fresh session after a transient error. Taking the lock again (with a
        wait) doubles as a fence: the old session, and its transaction, must be
        gone before this one writes.
        """
        self.close()
        self.open(lock_wait_ms=self.reconnect_lock_wait_ms)

    def close(self) -> None:
        self._cursors.clear()
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                pass
        self.db = None

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        try:
            if self.db is not None:
                self.db.rollback()
        except Exception:
            pass

    # -- statements --------------------------------------------------------

    def prepare_table(self, table: str, columns: Sequence[str], sql_types: Sequence[str]) -> None:
        cols = ", ".join(f"[{name}]" for name in columns)
        marks = ", ".join("?" for _ in columns)
        self._insert_sql[table] = f"INSERT INTO [{self.schema}].[{table}] ({cols}) VALUES ({marks})"
        self._rows_per_call[table] = rows_per_call(sql_types)

    def forget_cursor(self, table: str) -> None:
        """After ALTER COLUMN the prepared statement's parameter sizes are stale."""
        for key in [key for key in self._cursors if key[0] == table]:
            del self._cursors[key]
        self._meta.pop(table, None)

    def _cursor(self, table: str, slow: bool):
        key = (table, slow)
        cursor = self._cursors.get(key)
        if cursor is None:
            cursor = self.db.conn.cursor()
            cursor.fast_executemany = not slow
            self._cursors[key] = cursor
        return cursor

    def insert(self, table: str, rows: list[list[Any]], slow: bool) -> None:
        cursor = self._cursor(table, slow)
        sql = self._insert_sql[table]
        step = self._rows_per_call[table]
        for start in range(0, len(rows), step):
            cursor.executemany(sql, rows[start : start + step])

    def delete(self, table: str, key_column: str, keys: list[Any]) -> None:
        self.db.delete_keys(self.schema, table, key_column, keys, key_type=self.key_type)

    def advance(self, **values: Any) -> None:
        if self.run_id is None:
            return
        cp.advance(self.db.conn.cursor(), self.schema, self.root_table, self.run_id, **values)

    def committed_through(self) -> int | None:
        if self.run_id is None:
            return None
        return cp.docs_done(self.db, self.schema, self.root_table, self.run_id)

    def key_exists(self, key: Any) -> bool:
        return self.db.key_exists(self.schema, self.root_table, "mongo_id", self.key_type, key)

    def column_meta(self, table: str) -> dict[str, dict[str, Any]]:
        if table not in self._meta:
            self._meta[table] = self.db.column_info(self.schema, table)
        return self._meta[table]

    def widen(self, table: str, column: str, sql_type: str, nullable: bool, collation: str | None) -> None:
        self.db.widen_column(self.schema, table, column, sql_type, nullable=nullable, collation=collation)
        self.forget_cursor(table)


def rows_per_call(sql_types: Sequence[str]) -> int:
    """How many rows one executemany call binds, so its buffers stay near 32 MiB."""
    size = 0
    for sql_type in sql_types:
        kind = kind_of(sql_type)
        if kind == "text":
            units = width_of(sql_type)
            size += 8000 if units is None else 2 * units + 2
        elif kind == "binary":
            size += 8000
        elif kind == "decimal":
            size += 24
        elif kind == "date":
            size += 16
        else:
            size += 8
    return max(100, min(10_000, MAX_CALL_BYTES // max(size, 1)))


# --------------------------------------------------------------------------
# the writer
# --------------------------------------------------------------------------


@dataclass
class WriteStats:
    documents: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    widened: list[str] = field(default_factory=list)
    slow_tables: set[str] = field(default_factory=set)
    last_id: Any = None
    sql_seconds: float = 0.0  # insert + commit of the batches that landed
    insert_seconds: float = 0.0  # deletes and inserts: bandwidth and server work
    commit_seconds: float = 0.0  # checkpoint update and commit: round trips and log flushes


class BatchWriter:
    def __init__(
        self,
        ops: LiveSql,
        plan: dict[str, Any],
        columns: dict[tuple[str, str], Column],
        *,
        first_load: bool,
        rejects: RejectLog,
        retrier: Retrier,
        max_rejects: int = 1000,
        burst_limit: int = 50,
        widen: bool = True,
        non_cascading: Sequence[str] = (),
        log=None,
    ) -> None:
        self.ops = ops
        self.first_load = first_load
        self.rejects = rejects
        self.retrier = retrier
        self.max_rejects = max_rejects
        self.burst_limit = burst_limit
        self.widen_enabled = widen
        self.log = log
        self.stats = WriteStats()
        self.root = plan["root"]["table"]
        self.columns = columns
        self.tables: list[str] = [self.root] + [child["table"] for child in plan["children"]]
        self.parent_keys = {child["table"]: child["parent_key"] for child in plan["children"]}
        self.non_cascading = [table for table in non_cascading if table in self.parent_keys]
        self._strikes: dict[str, int] = {}
        self._burst = 0
        self._current: str | None = None

    # -- public ------------------------------------------------------------

    def write(self, units: list[Unit]) -> None:
        if not units:
            return
        self._burst = 0
        self._fit_widths(units)
        self._write(units)

    # -- widths ------------------------------------------------------------

    def _fit_widths(self, units: list[Unit]) -> None:
        needed: dict[Column, int] = {}
        for unit in units:
            for column, length in unit.overflow.items():
                if length > needed.get(column, 0):
                    needed[column] = length
        clip: list[Column] = []
        for column, length in needed.items():
            if column.units is None or length <= column.units:
                continue  # widened earlier in this batch loop
            if not self._widen(column, length):
                clip.append(column)
        if clip:
            self._clip(units, clip)

    def _widen(self, column: Column, length: int) -> bool:
        if not self.widen_enabled or column.is_key:
            return False
        meta = self.ops.column_meta(column.table).get(column.name)
        if not meta or not meta["widenable"]:
            return False
        live = meta["sql_type"]
        base = base_type(live)
        if base not in {"NVARCHAR", "VARCHAR"}:
            return False
        target = nvarchar_width(length, 1.5)
        new_type = target if base == "NVARCHAR" else target.replace("NVARCHAR", "VARCHAR")
        for attempt in range(1, WIDEN_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                self.ops.widen(column.table, column.name, new_type, meta["nullable"], meta["collation"])
            except Exception as exc:
                self.ops.rollback()
                kind = classify_sql(exc)
                if kind in (TRANSIENT, RESOURCE) and attempt < WIDEN_ATTEMPTS:
                    self.retrier.wait(kind, exc, where=f"genişletme {column.table}.{column.name}")
                    try:
                        self.ops.reconnect()
                    except Exception:
                        # `_write` reconnects (and waits) before the next batch.
                        return False
                    continue
                if self.log is not None:
                    self.log.warning(
                        "aktarım kolon genişletilemedi %s.%s %s → %s: %s",
                        column.table,
                        column.name,
                        live,
                        new_type,
                        describe(exc),
                    )
                return False
            column.units = width_of(new_type)
            column.sql_type = new_type
            self.stats.widened.append(f"{column.table}.{column.name} {live}→{new_type}")
            if self.log is not None:
                self.log.info(
                    "aktarım kolon genişletildi %s.%s %s → %s gereken=%s süre_sn=%.2f",
                    column.table,
                    column.name,
                    live,
                    new_type,
                    length,
                    time.monotonic() - started,
                )
            return True
        return False

    def _clip(self, units: list[Unit], columns: list[Column]) -> None:
        for unit in units:
            if unit.reject is not None:
                continue
            for column in columns:
                rows = [unit.root] if column.table == self.root else unit.children.get(column.table, [])
                for row in rows:
                    value = row[column.position]
                    if isinstance(value, str) and column.units is not None:
                        length = utf16_len(value)
                        if length > column.units:
                            row[column.position] = clip_utf16(value, column.units)
                            unit.notes.append(Note("clip", column.table, column.name, length, column.units))

    # -- transactions ------------------------------------------------------

    def _write(self, units: list[Unit]) -> None:
        slow_now: set[str] = set()
        where = f"belge {units[0].seq}-{units[-1].seq}"
        # Set after a transient error: reconnect, then ask whether the last
        # commit landed before writing again.
        recheck = self.ops.db is None
        while True:
            if recheck:
                try:
                    self.ops.reconnect()
                    landed = self._landed(units)
                except (LockBusy, GiveUp, Stopped):
                    raise
                except Exception as exc:
                    kind = classify_sql(exc)
                    if kind not in (TRANSIENT, RESOURCE):
                        raise
                    self.retrier.wait(kind, exc, where=f"yeniden bağlanma, {where}")
                    continue
                recheck = False
                if landed:
                    # The commit reached the server; only its reply was lost.
                    self._after_commit(units, slow_now)
                    self.retrier.reset()
                    return
            try:
                self._once(units, slow_now)
                self.retrier.reset()
                return
            except (cp.TakenOver, GiveUp, Stopped, TooManyRejects):
                self.ops.rollback()
                raise
            except Exception as exc:
                self.ops.rollback()
                kind = classify_sql(exc)
                table = self._current
                if kind in (TRANSIENT, RESOURCE):
                    self.retrier.wait(kind, exc, where=where)
                    recheck = True
                    continue
                if kind == FATAL:
                    raise
                if kind == BIND and table in self.tables and table not in slow_now and table not in self.stats.slow_tables:
                    self._strike(table, exc)
                    slow_now.add(table)
                    continue
                if kind not in (DATA, UNKNOWN, BIND):
                    raise
                if len(units) == 1:
                    self._reject_sql(units[0], exc, table)
                    return
                middle = len(units) // 2
                self._write(units[:middle])
                self._write(units[middle:])
                return

    def _once(self, units: list[Unit], slow_now: set[str]) -> None:
        started = time.perf_counter()
        live = [unit for unit in units if unit.reject is None]
        for unit in units:
            self._log_unit(unit)
        if live and not self.first_load:
            keys = [unit.key for unit in live]
            for table in self.non_cascading:
                self._current = table
                self.ops.delete(table, self.parent_keys[table], keys)
            self._current = self.root
            self.ops.delete(self.root, "mongo_id", keys)
        for table in self.tables:
            if table == self.root:
                rows = [unit.root for unit in live]
            else:
                rows = [row for unit in live for row in unit.children.get(table, ())]
            if rows:
                self._current = table
                self.ops.insert(table, rows, slow=table in slow_now or table in self.stats.slow_tables)
        self._current = None
        inserted = time.perf_counter()
        self.ops.advance(
            last_id=units[-1].id,
            docs_done=units[-1].seq,
            written=len(live),
            rows=sum(unit.rows for unit in live),
            rejected=sum(1 for unit in units if unit.reject is not None),
            clipped=sum(1 for unit in live for note in unit.notes if note.kind == "clip"),
        )
        # The batch's reject lines reach the disk before its checkpoint does.
        self.rejects.sync()
        self.ops.commit()
        finished = time.perf_counter()
        self.stats.sql_seconds += finished - started
        self.stats.insert_seconds += inserted - started
        self.stats.commit_seconds += finished - inserted
        self._after_commit(units, slow_now)

    def _after_commit(self, units: list[Unit], slow_now: set[str]) -> None:
        stats = self.stats
        for unit in units:
            if unit.reject is not None:
                continue
            stats.documents += 1
            stats.rows[self.root] = stats.rows.get(self.root, 0) + 1
            for table, rows in unit.children.items():
                stats.rows[table] = stats.rows.get(table, 0) + len(rows)
        stats.last_id = units[-1].id
        for table in self.tables:
            if table not in slow_now:
                self._strikes[table] = 0

    def _landed(self, units: list[Unit]) -> bool:
        through = self.ops.committed_through()
        if through is not None:
            return through >= units[-1].seq
        if not self.first_load:
            # No checkpoint table: a delete-then-insert batch is safe to repeat.
            return False
        live = [unit for unit in units if unit.reject is None]
        return bool(live) and self.ops.key_exists(live[-1].key)

    # -- rejects -----------------------------------------------------------

    def _log_unit(self, unit: Unit) -> None:
        if unit.logged:
            return
        if unit.reject is not None:
            self.rejects.record(
                "reject",
                stage=unit.stage or "flatten",
                mongo_id=unit.id,
                table=self.root,
                error=unit.error or unit.reject,
                detail=unit.reject,
            )
            self._check_limits()
        else:
            for note in unit.notes:
                self.rejects.record(
                    note.kind,
                    stage="convert" if note.kind != "clip" else "sql",
                    mongo_id=unit.id,
                    table=note.table,
                    column=note.column,
                    length=note.length,
                    width=note.width,
                    detail=note.detail or None,
                )
        unit.logged = True

    def _reject_sql(self, unit: Unit, exc: BaseException, table: str | None) -> None:
        state, natives = sql_codes(exc)
        self.rejects.record(
            "reject",
            stage="sql",
            mongo_id=unit.id,
            table=table,
            error=describe(exc),
            sqlstate=state or None,
            native=sorted(natives),
        )
        unit.reject = "sql"
        unit.stage = "sql"
        unit.logged = True
        self._burst += 1
        self._check_limits()
        if self._burst > self.burst_limit:
            raise TooManyRejects(
                f"Tek partide {self._burst} belge reddedildi; hata sistematik görünüyor, iş durdu. "
                f"Son hata: {describe(exc)}"
            )
        # Move the checkpoint past the rejected document on its own.
        self._write([unit])

    def _check_limits(self) -> None:
        if self.rejects.rejected > self.max_rejects:
            raise TooManyRejects(
                f"Reddedilen belge sayısı sınırı aştı ({self.rejects.rejected} > {self.max_rejects}). "
                f"Ayrıntı: {self.rejects.path}"
            )

    def _strike(self, table: str, exc: BaseException) -> None:
        self._strikes[table] = self._strikes.get(table, 0) + 1
        if self._strikes[table] >= BIND_STRIKES:
            self.stats.slow_tables.add(table)
        if self.log is not None:
            self.log.warning(
                "aktarım bağlama hatası tablo=%s ardışık=%s yavaş_yol=%s hata=%s",
                table,
                self._strikes[table],
                "kalıcı" if table in self.stats.slow_tables else "bu parti",
                describe(exc),
            )


def estimate_seconds(remaining: int | None, rate: float) -> float | None:
    if not remaining or rate <= 0 or math.isnan(rate):
        return None
    return remaining / rate
