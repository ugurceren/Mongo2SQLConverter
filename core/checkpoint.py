"""
Exactly-once resume point for long transfers.

One row per target root table in `[schema].[Mongo2SqlCheckpoint]`. Every batch
updates it in the same transaction as the rows it writes, so after any crash
the row says precisely which documents are in SQL. The `_id` is stored as
canonical extended JSON: it round-trips every BSON type, where reading
`MAX(mongo_id)` back from SQL cannot tell the string "000123" from the number
123 (and would compare strings under the SQL collation, not Mongo's order).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId, json_util

from core.rejects import id_text

TABLE = "Mongo2SqlCheckpoint"
MISSING = object()  # "start from the first document"


def window(start: Any) -> dict[str, Any]:
    """Resume point of a load read through a date index: the window it was in."""
    return {"window": start}


def is_window(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"window"}


def table_sql(schema: str) -> str:
    return (
        f"CREATE TABLE [{schema}].[{TABLE}] (\n"
        "    [target_table] NVARCHAR(128) NOT NULL CONSTRAINT [PK_Mongo2SqlCheckpoint] PRIMARY KEY,\n"
        "    [collection] NVARCHAR(256) NOT NULL,\n"
        "    [mongo_database] NVARCHAR(256) NOT NULL,\n"
        "    [run_id] CHAR(32) NOT NULL,\n"
        "    [mode] VARCHAR(16) NOT NULL,\n"
        "    [status] VARCHAR(16) NOT NULL,\n"
        "    [first_load] BIT NOT NULL,\n"
        "    [plan_hash] CHAR(64) NULL,\n"
        "    [filter_hash] CHAR(64) NULL,\n"
        "    [last_id_json] NVARCHAR(4000) NULL,\n"
        "    [last_id_type] VARCHAR(16) NULL,\n"
        "    [docs_done] BIGINT NOT NULL,\n"
        "    [docs_written] BIGINT NOT NULL,\n"
        "    [rows_written] BIGINT NOT NULL,\n"
        "    [rejected] BIGINT NOT NULL,\n"
        "    [clipped] BIGINT NOT NULL,\n"
        "    [resume_count] INT NOT NULL,\n"
        "    [started_at] DATETIME2(3) NOT NULL,\n"
        "    [updated_at] DATETIME2(3) NOT NULL,\n"
        "    [finished_at] DATETIME2(3) NULL,\n"
        "    [last_error] NVARCHAR(4000) NULL\n"
        ")"
    )


@dataclass
class Checkpoint:
    target_table: str
    collection: str
    mongo_database: str
    run_id: str
    mode: str
    status: str
    first_load: bool
    plan_hash: str | None
    filter_hash: str | None
    last_id_json: str | None
    last_id_type: str | None
    docs_done: int = 0
    docs_written: int = 0
    rows_written: int = 0
    rejected: int = 0
    clipped: int = 0
    resume_count: int = 0
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None

    @property
    def last_id(self) -> Any:
        return decode_id(self.last_id_json) if self.last_id_json else MISSING


COLUMNS = (
    "target_table, collection, mongo_database, run_id, mode, status, first_load, plan_hash, "
    "filter_hash, last_id_json, last_id_type, docs_done, docs_written, rows_written, rejected, "
    "clipped, resume_count, started_at, updated_at, finished_at, last_error"
)


# --------------------------------------------------------------------------
# ids and hashes
# --------------------------------------------------------------------------


def encode_id(value: Any) -> tuple[str, str]:
    """(canonical extended JSON, BSON type name) of an `_id`."""
    return id_text(value) or "", id_type(value)


def decode_id(text: str) -> Any:
    return json_util.loads(text, json_options=json_util.CANONICAL_JSON_OPTIONS)["_id"]


def id_type(value: Any) -> str:
    if isinstance(value, ObjectId):
        return "objectid"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, datetime):
        return "date"
    return type(value).__name__.lower()[:16]


def plan_hash(plan: dict[str, Any]) -> str:
    """
    Which load this is: the root table and the nesting mode.

    Columns, widths and even rare child tables are left out on purpose: every
    run profiles a fresh random sample, so they drift a little from run to run,
    and the loader reconciles them anyway (missing columns are added, live
    types are used, long text widens its column). Hashing them would restart a
    days-long full load from zero because a sample saw one more rare field.
    """
    shape = {"root": plan["root"]["table"], "nesting": plan.get("nesting")}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()


def filter_hash(query: dict[str, Any] | None) -> str:
    text = json_util.dumps(query or {}, json_options=json_util.CANONICAL_JSON_OPTIONS, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_run_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------


def ensure_table(db, schema: str) -> bool:
    """Create the checkpoint table if needed. False when this login may not."""
    cur = db.conn.cursor()
    try:
        cur.execute(
            f"IF OBJECT_ID(N'[{schema}].[{TABLE}]', N'U') IS NULL EXEC(N'"
            + table_sql(schema).replace("'", "''")
            + "')"
        )
        db.conn.commit()
        return True
    except Exception:
        db.rollback()
        cur = db.conn.cursor()
        cur.execute("SELECT OBJECT_ID(?, N'U')", f"[{schema}].[{TABLE}]")
        row = cur.fetchone()
        return bool(row and row[0] is not None)


def read(db, schema: str, target_table: str) -> Checkpoint | None:
    cur = db.conn.cursor()
    cur.execute(
        f"SELECT {COLUMNS} FROM [{schema}].[{TABLE}] WITH (READCOMMITTEDLOCK) WHERE target_table = ?",
        target_table,
    )
    row = cur.fetchone()
    db.conn.commit()
    if row is None:
        return None
    return Checkpoint(
        target_table=row[0],
        collection=row[1],
        mongo_database=row[2],
        run_id=str(row[3]).strip(),
        mode=row[4],
        status=row[5],
        first_load=bool(row[6]),
        plan_hash=(row[7] or "").strip() or None,
        filter_hash=(row[8] or "").strip() or None,
        last_id_json=row[9],
        last_id_type=row[10],
        docs_done=int(row[11] or 0),
        docs_written=int(row[12] or 0),
        rows_written=int(row[13] or 0),
        rejected=int(row[14] or 0),
        clipped=int(row[15] or 0),
        resume_count=int(row[16] or 0),
        started_at=row[17],
        updated_at=row[18],
        finished_at=row[19],
        last_error=row[20],
    )


def begin(db, schema: str, cp: Checkpoint) -> None:
    """Write the row for a new or resumed run and commit it."""
    cur = db.conn.cursor()
    cur.execute(
        f"UPDATE [{schema}].[{TABLE}] SET collection = ?, mongo_database = ?, run_id = ?, mode = ?, "
        "status = 'running', first_load = ?, plan_hash = ?, filter_hash = ?, last_id_json = ?, "
        "last_id_type = ?, docs_done = ?, docs_written = ?, rows_written = ?, rejected = ?, "
        "clipped = ?, resume_count = ?, started_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME(), "
        "finished_at = NULL, last_error = NULL WHERE target_table = ?",
        cp.collection,
        cp.mongo_database,
        cp.run_id,
        cp.mode,
        cp.first_load,
        cp.plan_hash,
        cp.filter_hash,
        cp.last_id_json,
        cp.last_id_type,
        cp.docs_done,
        cp.docs_written,
        cp.rows_written,
        cp.rejected,
        cp.clipped,
        cp.resume_count,
        cp.target_table,
    )
    if cur.rowcount == 0:
        cur.execute(
            f"INSERT INTO [{schema}].[{TABLE}] ({COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME(), "
            "SYSUTCDATETIME(), NULL, NULL)",
            cp.target_table,
            cp.collection,
            cp.mongo_database,
            cp.run_id,
            cp.mode,
            cp.first_load,
            cp.plan_hash,
            cp.filter_hash,
            cp.last_id_json,
            cp.last_id_type,
            cp.docs_done,
            cp.docs_written,
            cp.rows_written,
            cp.rejected,
            cp.clipped,
            cp.resume_count,
        )
    db.conn.commit()


class TakenOver(RuntimeError):
    """Another job rewrote the checkpoint row: this one must stop writing."""


def advance(
    cursor,
    schema: str,
    target_table: str,
    run_id: str,
    *,
    last_id: Any,
    docs_done: int,
    written: int,
    rows: int,
    rejected: int,
    clipped: int,
) -> None:
    """Move the resume point; runs inside the batch's transaction (no commit)."""
    last_json, last_type = encode_id(last_id)
    cursor.execute(
        f"UPDATE [{schema}].[{TABLE}] SET last_id_json = ?, last_id_type = ?, docs_done = ?, "
        "docs_written = docs_written + ?, rows_written = rows_written + ?, rejected = rejected + ?, "
        "clipped = clipped + ?, updated_at = SYSUTCDATETIME() WHERE target_table = ? AND run_id = ?",
        last_json,
        last_type,
        docs_done,
        written,
        rows,
        rejected,
        clipped,
        target_table,
        run_id,
    )
    if cursor.rowcount != 1:
        raise TakenOver(
            f"{target_table} için kontrol noktası başka bir iş tarafından alındı; bu iş duruyor."
        )


def docs_done(db, schema: str, target_table: str, run_id: str) -> int | None:
    """How far this run's committed batches got (after a lost commit acknowledgement)."""
    cur = db.conn.cursor()
    cur.execute(
        f"SELECT docs_done FROM [{schema}].[{TABLE}] WITH (READCOMMITTEDLOCK) "
        "WHERE target_table = ? AND run_id = ?",
        target_table,
        run_id,
    )
    row = cur.fetchone()
    db.conn.commit()
    return None if row is None else int(row[0])


def finish(db, schema: str, target_table: str, run_id: str, status: str, error: str | None = None) -> None:
    """Best effort: the job is over anyway, this only labels it."""
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"UPDATE [{schema}].[{TABLE}] SET status = ?, finished_at = SYSUTCDATETIME(), "
            "updated_at = SYSUTCDATETIME(), last_error = ? WHERE target_table = ? AND run_id = ?",
            status,
            (error or "")[:4000] or None,
            target_table,
            run_id,
        )
        db.conn.commit()
    except Exception:
        db.rollback()


def delete(cursor, schema: str, target_table: str) -> None:
    """Forget the resume point (tables emptied or recreated); no commit."""
    cursor.execute(
        f"IF OBJECT_ID(N'[{schema}].[{TABLE}]', N'U') IS NOT NULL "
        f"DELETE FROM [{schema}].[{TABLE}] WHERE target_table = ?",
        target_table,
    )


# --------------------------------------------------------------------------
# where a run starts
# --------------------------------------------------------------------------


@dataclass
class RunPlan:
    mode: str  # "full" | "incremental"
    start_after: Any = MISSING
    first_load: bool = False
    resume: bool = False
    docs_done: int = 0
    resume_count: int = 0
    note: str = ""
    carried: dict[str, int] = field(default_factory=dict)  # counters continued from the checkpoint


def _overlap(value: Any, minutes: int) -> Any:
    """
    Start an ObjectId incremental run a little earlier than the last `_id`.

    Clients mint ObjectIds with their own clock; one running behind can insert
    a document "older" than ones already copied. Re-reading a short window
    catches those; incremental batches delete before inserting, so re-reading
    is harmless.
    """
    if minutes <= 0 or not isinstance(value, ObjectId):
        return value
    moment = value.generation_time - timedelta(minutes=minutes)
    return ObjectId.from_datetime(moment.astimezone(timezone.utc))


def plan_run(
    requested: str,
    *,
    checkpoint: Checkpoint | None,
    restart: bool,
    plan_hash_value: str,
    filter_hash_value: str,
    tables_empty: bool,
    root_empty: bool,
    key_type: str,
    legacy_max: Any = None,
    yaml_mark: Any = MISSING,
    overlap_minutes: int = 15,
) -> RunPlan:
    """
    Decide how this run starts.

    - An unfinished full load with the same root table, nesting and filter
      continues where it stopped, keeping its first-load flag (no deletes
      needed after last_id).
    - A requested full run otherwise starts from the first document.
    - Incremental / auto continue after the last written `_id`.
    - Tables filled before checkpoints existed fall back to MAX(mongo_id) for
      ObjectId and integer keys, the saved YAML mark otherwise, and a full
      re-sync with deletes when neither is trustworthy.
    """
    requested = requested if requested in {"full", "incremental", "auto"} else "auto"
    if restart:
        return RunPlan("full", first_load=tables_empty, note="baştan başlatıldı (--restart)")

    if (
        checkpoint is not None
        and checkpoint.mode == "full"
        and checkpoint.status != "completed"
        and checkpoint.plan_hash == plan_hash_value
        and checkpoint.filter_hash == filter_hash_value
        and checkpoint.last_id_json
    ):
        return RunPlan(
            "full",
            start_after=checkpoint.last_id,
            first_load=checkpoint.first_load,
            resume=True,
            docs_done=checkpoint.docs_done,
            resume_count=checkpoint.resume_count + 1,
            carried={
                "docs_written": checkpoint.docs_written,
                "rows_written": checkpoint.rows_written,
                "rejected": checkpoint.rejected,
                "clipped": checkpoint.clipped,
            },
            note=f"yarım kalan tam yükleme sürüyor ({checkpoint.docs_done} belge işlenmişti)",
        )

    if requested == "full":
        return RunPlan("full", first_load=tables_empty, note="yeni tam yükleme")

    # A date-window position says nothing about `_id`s: such tables go the MAX way below.
    if checkpoint is not None and checkpoint.last_id_json and not is_window(checkpoint.last_id):
        start = _overlap(checkpoint.last_id, overlap_minutes)
        return RunPlan("incremental", start_after=start, note="kontrol noktasından artımlı")

    if root_empty:
        return RunPlan("full", first_load=tables_empty, note="tablo boş: tam yükleme")

    # Rows exist but no checkpoint: tables from an older version of the tool.
    key = (key_type or "").upper()
    if legacy_max is not None and key.startswith("CHAR(24)"):
        try:
            return RunPlan(
                "incremental",
                start_after=_overlap(ObjectId(str(legacy_max).strip()), overlap_minutes),
                note="kontrol noktası yok: MAX(mongo_id) ObjectId olarak",
            )
        except Exception:
            pass
    if legacy_max is not None and key.split("(")[0] in {"INT", "BIGINT"}:
        return RunPlan("incremental", start_after=int(legacy_max), note="kontrol noktası yok: MAX(mongo_id) sayı olarak")
    if yaml_mark is not MISSING:
        return RunPlan("incremental", start_after=yaml_mark, note="kontrol noktası yok: kayıtlı işaretten")
    return RunPlan(
        "full",
        first_load=False,
        note="kontrol noktası yok ve anahtar metin: silerek tam senkron (bir kez)",
    )
