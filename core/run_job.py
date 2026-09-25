"""Streamlit-free transfer of one collection from saved prefs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

from core import checkpoint
from core.checkpoint import MISSING, Checkpoint
from core.inspect import NESTING_OPTIONS, key_column_type, profile_collection, sql_table_ident
from core.logutil import get_logger
from core.mongo import (
    MongoClientWrapper,
    combine_filters,
    date_range_filter,
    decode_mongo_id,
)
from core.mssql import AUTH_NEEDS_PASSWORD, MssqlConnection, auth_mode, available_drivers
from core.preflight import Report, run_preflight
from core.rejects import RejectLog
from core.retry import describe
from core.settings import (
    _as_schedule_mode,
    load_settings,
    load_sync_watermark,
    load_transfer_prefs,
    save_sync_watermark,
)
from core.transfer import (
    TransferStats,
    apply_column_selection,
    apply_table_names,
    ensure_tables,
    plan_table_problems,
    plan_tables,
    relax_nullability,
    resolve_table_names,
    retarget_plan,
    transfer_collection,
)
from core.writer import LiveSql

# Transfers of collections bigger than `core.inspect.LARGE_COLLECTION` profile at least this many.
LARGE_SAMPLE = 50_000
COUNT_TIME_MS = 15_000


@dataclass
class JobResult:
    collection: str
    requested_mode: str
    mode: str
    stats: TransferStats
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class TransferRequest:
    collection: str
    plan: dict[str, Any]
    mongo_database: str
    mode: str = "auto"  # requested: full | incremental | auto
    date_query: dict[str, Any] | None = None
    batch: int = 2000
    recreate: bool = False
    clear_first: bool = False
    restart: bool = False
    max_rejects: int = 1000
    overlap_minutes: int = 15
    commit_rows: int = 20_000
    commit_mb: int = 16
    prefetch: bool = True


def loader_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tunables from the `loader:` section of config.yaml, with safe defaults."""
    raw = (cfg if cfg is not None else load_settings()).get("loader") or {}

    def number(key: str, default: int, low: int = 0) -> int:
        try:
            return max(low, int(raw.get(key, default)))
        except (TypeError, ValueError):
            return default

    return {
        "max_rejects": number("max_rejects", 1000),
        "overlap_minutes": number("incremental_overlap_minutes", 15),
        "commit_rows": number("commit_rows", 20_000, 1000),
        "commit_mb": number("commit_mb", 16, 1),
        "prefetch": bool(raw.get("prefetch", True)),
    }


def _encrypt_flag(raw: Any) -> bool | None:
    text = str(raw or "default").strip().lower()
    if text in ("yes", "true", "1"):
        return True
    if text in ("no", "false", "0"):
        return False
    return None


def _mongo_client(mongo_cfg: dict[str, Any], long_running: bool = False) -> MongoClientWrapper:
    return MongoClientWrapper(
        uri=mongo_cfg.get("uri") or "",
        database=mongo_cfg.get("database") or "",
        username=mongo_cfg.get("username") or None,
        password=mongo_cfg.get("password") or None,
        long_running=long_running,
    )


def _sql_target(
    mssql_cfg: dict[str, Any], password: str | None, schema: str
) -> MssqlConnection:
    drivers = available_drivers()
    driver = mssql_cfg.get("driver") or (drivers[0] if drivers else "ODBC Driver 17 for SQL Server")
    return MssqlConnection(
        server=mssql_cfg.get("server") or "",
        database=mssql_cfg.get("database") or "",
        schema=schema,
        driver=driver,
        trusted_connection=bool(mssql_cfg.get("trusted_connection", True)),
        auth=auth_mode(mssql_cfg),
        username=mssql_cfg.get("username") or None,
        password=password,
        encrypt=_encrypt_flag(mssql_cfg.get("encrypt")),
        trust_certificate=bool(mssql_cfg.get("trust_certificate", False)),
    )


def _filter_tz(mode: str):
    return timezone.utc if mode == "utc" else datetime.now().astimezone().tzinfo


def date_query_from_filter(date_filter: dict[str, Any] | None) -> dict[str, Any] | None:
    if not date_filter or not date_filter.get("enabled") or not date_filter.get("field"):
        return None
    zone = _filter_tz(str(date_filter.get("timezone") or "local"))
    start = date_filter.get("start")
    end = date_filter.get("end")
    if isinstance(start, datetime):
        start = start.date()
    if isinstance(end, datetime):
        end = end.date()
    lower = datetime.combine(start, time.min, tzinfo=zone) if isinstance(start, date) else None
    upper = (
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
        if isinstance(end, date)
        else None
    )
    return date_range_filter(str(date_filter["field"]), lower, upper) or None


def _expected_count(
    source: MongoClientWrapper, collection: str, run: checkpoint.RunPlan, query: dict[str, Any] | None
) -> int | None:
    """
    How many documents this run will read, without a long count: metadata for
    a whole collection, a time-limited count otherwise (None when too slow).
    """
    try:
        if run.mode == "full":
            total = (
                source.estimated_count(collection, query, max_time_ms=COUNT_TIME_MS)
                if query
                else source.estimated_count(collection)
            )
            if total is not None and run.resume:
                return max(total - run.docs_done, 0)
            return total
        after = None if run.start_after is MISSING else {"_id": {"$gt": run.start_after}}
        filters = combine_filters(query, after)
        if not filters:
            return source.estimated_count(collection)
        return source.estimated_count(collection, filters, max_time_ms=COUNT_TIME_MS)
    except Exception:
        return None


def _key_type(db: MssqlConnection, plan: dict[str, Any]) -> str:
    live = db.column_info(plan["schema"], plan["root"]["table"])
    if "mongo_id" in live:
        return live["mongo_id"]["sql_type"]
    return key_column_type(plan)


def execute_transfer(
    source: MongoClientWrapper,
    target_factory: Callable[[], MssqlConnection],
    request: TransferRequest,
    *,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_status: Callable[[str], None] | None = None,
    log_extra: dict[str, Any] | None = None,
) -> JobResult:
    """
    Run one transfer end to end, for the CLI and the UI alike.

    Holds the table lock for the whole run, keeps the checkpoint row, decides
    where to start (`checkpoint.plan_run`) and labels the row completed,
    stopped or failed at the end. The resume point is forgotten *before* any
    table is emptied or dropped, so a crash in between can never leave a
    checkpoint pointing into empty tables.
    """
    plan = request.plan
    # Before any SQL: two tables under one name would mix their rows into one table.
    problems = plan_table_problems(plan)
    if problems:
        raise ValueError("Tablo adları kullanılamaz. " + " ".join(problems))
    schema = plan["schema"]
    root = plan["root"]["table"]
    children = [child["table"] for child in plan["children"]]
    log = get_logger()
    say = on_status or (lambda message: None)

    ops = LiveSql(target_factory, schema, root)
    say("SQL oturumu açılıyor, tablo kilidi alınıyor...")
    ops.open()
    run_id: str | None = None
    has_table = False
    rejects: RejectLog | None = None
    try:
        db = ops.db
        has_table = checkpoint.ensure_table(db, schema)
        if not has_table:
            log.warning(
                "aktarım kontrol noktası tablosu oluşturulamadı (%s.%s); devam noktası "
                "MAX(mongo_id) ile bulunur. DBA oluşturabilir:\n%s",
                schema,
                checkpoint.TABLE,
                checkpoint.table_sql(schema),
            )
        if has_table and (request.recreate or request.clear_first):
            cur = db.conn.cursor()
            checkpoint.delete(cur, schema, root)
            db.conn.commit()

        say("SQL tabloları hazırlanıyor...")
        created, existing = ensure_tables(db, plan, recreate=request.recreate)
        if created:
            log.info("aktarım tablolar oluşturuldu collection=%s tablolar=%s", request.collection, ", ".join(created))
        if request.clear_first and not request.recreate:
            say("Tablolar boşaltılıyor...")
            how = db.truncate_tables(schema, root, children)
            log.info("aktarım tablolar boşaltıldı yöntem=%s tablolar=%s", how, ", ".join([root, *children]))

        row = checkpoint.read(db, schema, root) if has_table else None
        root_empty = not db.has_rows(schema, root)
        tables_empty = root_empty and not any(db.has_rows(schema, table) for table in children)
        legacy_max = None
        yaml_mark: Any = MISSING
        if (row is None or not row.last_id_json) and not root_empty:
            legacy_max = db.max_key(schema, root, "mongo_id")[1]
            mark = load_sync_watermark(request.collection)
            if mark:
                try:
                    yaml_mark = decode_mongo_id(mark["last_id"], mark["last_id_type"])
                except Exception:
                    yaml_mark = MISSING
        request_filter = checkpoint.filter_hash(request.date_query)
        run = checkpoint.plan_run(
            request.mode,
            checkpoint=row,
            restart=request.restart,
            plan_hash_value=checkpoint.plan_hash(plan),
            filter_hash_value=request_filter,
            tables_empty=tables_empty,
            root_empty=root_empty,
            key_type=_key_type(db, plan),
            legacy_max=legacy_max,
            yaml_mark=yaml_mark,
            overlap_minutes=request.overlap_minutes,
        )
        # auto + incremental: a saved date range would skip later `_id` values.
        query = None if (request.mode == "auto" and run.mode == "incremental") else request.date_query
        log.info(
            "aktarım kararı collection=%s istenen=%s mod=%s ilk_yükleme=%s devam=%s karar=%s",
            request.collection,
            request.mode,
            run.mode,
            run.first_load,
            run.resume,
            run.note,
        )

        run_id = checkpoint.new_run_id()
        if has_table:
            keep_mark = row is not None and bool(row.last_id_json) and (run.resume or run.mode == "incremental")
            checkpoint.begin(
                db,
                schema,
                Checkpoint(
                    target_table=root,
                    collection=request.collection,
                    mongo_database=request.mongo_database,
                    run_id=run_id,
                    mode=run.mode,
                    status="running",
                    first_load=run.first_load,
                    plan_hash=checkpoint.plan_hash(plan),
                    filter_hash=request_filter,
                    last_id_json=row.last_id_json if keep_mark else None,
                    last_id_type=row.last_id_type if keep_mark else None,
                    docs_done=run.docs_done,
                    docs_written=run.carried.get("docs_written", 0),
                    rows_written=run.carried.get("rows_written", 0),
                    rejected=run.carried.get("rejected", 0),
                    clipped=run.carried.get("clipped", 0),
                    resume_count=run.resume_count,
                ),
            )
            ops.run_id = run_id

        say("Belge sayısı tahmin ediliyor...")
        expected = _expected_count(source, request.collection, run, query)
        rejects = RejectLog(request.collection, root, run_id, log=log)
        say("Belgeler yazılıyor...")
        stats = transfer_collection(
            source,
            ops,
            plan,
            request.collection,
            run=run,
            rejects=rejects,
            query=query,
            batch_docs=request.batch,
            batch_rows=request.commit_rows,
            batch_bytes=request.commit_mb * 1024 * 1024,
            expected_count=expected,
            progress=progress,
            should_stop=should_stop,
            log_extra=log_extra,
            max_rejects=request.max_rejects,
            prefetch=request.prefetch,
        )
        if has_table:
            checkpoint.finish(ops.db, schema, root, run_id, "stopped" if stats.stopped else "completed")
        if stats.last_id and stats.last_id_type:
            save_sync_watermark(request.collection, stats.last_id, stats.last_id_type)
        if not rejects.used:
            stats.rejects_path = None
        return JobResult(
            collection=request.collection,
            requested_mode=request.mode,
            mode=run.mode,
            stats=stats,
            created=created,
            existing=existing,
            note=run.note,
        )
    except BaseException as exc:
        if has_table and run_id and ops.db is not None:
            ops.rollback()
            checkpoint.finish(ops.db, schema, root, run_id, "failed", error=describe(exc))
        raise
    finally:
        if rejects is not None:
            rejects.close()
        ops.close()


@dataclass
class JobInputs:
    """One scheduled job's settings, resolved from config.local.yaml and the CLI."""

    name: str
    schema: str
    table: str
    pinned: bool
    requested: str
    write_batch: int
    profile_sample: int
    nesting: str
    prefs: dict[str, Any]
    mongo_cfg: dict[str, Any]
    mssql_cfg: dict[str, Any]
    profiler: dict[str, Any]
    knobs: dict[str, Any]

    @property
    def date_filter(self) -> dict[str, Any]:
        return dict(self.prefs["date_filter"])

    @property
    def date_field(self) -> str | None:
        date_filter = self.date_filter
        return str(date_filter["field"]) if date_filter.get("enabled") and date_filter.get("field") else None

    def target(self) -> MssqlConnection:
        return _sql_target(self.mssql_cfg, self.mssql_cfg.get("password"), self.schema)


def _job_inputs(
    collection: str,
    *,
    mode: str | None,
    batch: int | None,
    sample: int | None,
    table: str | None,
    schema: str | None,
    table_names: dict[str, str] | None,
    restart: bool,
) -> JobInputs:
    name = (collection or "").strip()
    if not name:
        raise ValueError("Koleksiyon adı boş.")

    cfg = load_settings()
    mongo_cfg = cfg.get("mongodb") or {}
    mssql_cfg = cfg.get("mssql") or {}
    prefs = load_transfer_prefs(name)
    pinned = bool((table or "").strip())
    if pinned:
        prefs["table"] = str(table).strip()
        prefs["table_names"] = dict(table_names or {})
    elif table_names is not None:
        prefs["table_names"] = dict(table_names)

    if not (mongo_cfg.get("uri") and mongo_cfg.get("database")):
        raise RuntimeError("Mongo bağlantısı config.local.yaml içinde yok.")
    if not (mssql_cfg.get("server") and mssql_cfg.get("database")):
        raise RuntimeError("SQL bağlantısı config.local.yaml içinde yok.")
    if auth_mode(mssql_cfg) in AUTH_NEEDS_PASSWORD and not mssql_cfg.get("password"):
        raise RuntimeError(
            "Bu SQL kimliği şifre ister. Şifreyi config.local.yaml içine yazın "
            "(oturum şifresi CLI'da yok)."
        )

    allowed_nesting = {item[0] for item in NESTING_OPTIONS}
    requested = _as_schedule_mode(mode if mode is not None else prefs["schedule_mode"])
    return JobInputs(
        name=name,
        schema=str(schema or "").strip() or mssql_cfg.get("schema") or "dbo",
        table=sql_table_ident(prefs["table"] or name),
        pinned=pinned,
        requested="full" if restart else requested,
        write_batch=int(batch) if batch is not None else int(prefs["batch"]),
        profile_sample=int(sample) if sample is not None else int(prefs["sample"]),
        nesting=prefs["nesting"] if prefs["nesting"] in allowed_nesting else "hybrid",
        prefs=prefs,
        mongo_cfg=mongo_cfg,
        mssql_cfg=mssql_cfg,
        profiler=cfg.get("profiler") or {},
        knobs=loader_settings(cfg),
    )


def _profile_plan(source: MongoClientWrapper, job: JobInputs, *, full_profile: bool) -> dict[str, Any]:
    """Profile the collection and shape the plan the way the saved prefs say."""
    log = get_logger()
    date_filter = job.date_filter
    # Profiling follows the date range only when this run writes that range.
    probe = job.target()
    probe.connect(login_timeout=30)
    try:
        root_has_rows = probe.has_rows(job.schema, job.table)
    finally:
        probe.close()
    profile_dates = date_filter
    if job.requested == "auto" and root_has_rows:
        profile_dates = {**date_filter, "enabled": False}

    log.info(
        "görev başladı collection=%s istenen=%s tablo=%s.%s parti=%s örnek=%s kilitli=%s",
        job.name,
        job.requested,
        job.schema,
        job.table,
        job.write_batch,
        job.profile_sample,
        job.pinned,
    )
    _, plan = profile_collection(
        source,
        job.name,
        job.profile_sample,
        job.schema,
        int(job.profiler.get("map_min_keys", 30)),
        float(job.profiler.get("map_max_fill", 0.2)),
        float(job.profiler.get("headroom", 1.5)),
        nesting=job.nesting,
        query=date_query_from_filter(profile_dates),
        full_profile=full_profile,
        min_large_sample=LARGE_SAMPLE,
    )
    plan = retarget_plan(plan, schema=job.schema, root_table=job.table)
    if job.prefs["allow_null"]:
        plan = relax_nullability(plan)
    columns = job.prefs["columns"]
    plan = apply_column_selection(plan, columns["exclude"], columns["exclude_tables"])
    names = job.prefs.get("table_names")
    _, unknown = resolve_table_names(plan, names)
    if unknown:
        # Not an error: a rare array can be missing from this run's sample.
        log.warning(
            "tablo adı hiçbir tabloya uymadı, yok sayıldı: %s (kaynak yolları: %s)",
            ", ".join(unknown),
            ", ".join(child["source"] for child in plan["children"]) or "-",
        )
    plan = apply_table_names(plan, names)
    log.info("aktarım hedefleri collection=%s tablolar=%s", job.name, ", ".join(plan_tables(plan)))
    return plan


def run_transfer_job(
    collection: str,
    *,
    mode: str | None = None,
    batch: int | None = None,
    sample: int | None = None,
    table: str | None = None,
    schema: str | None = None,
    table_names: dict[str, str] | None = None,
    restart: bool = False,
    max_rejects: int | None = None,
    full_profile: bool = False,
) -> JobResult:
    """Profile and write one collection using `config.local.yaml` prefs.

    `--table` / `--rename` from a downloaded bat pin the SQL tables for that
    job so another collection's UI edits cannot redirect it. Schema comes from
    `mssql.schema` unless `--schema` is passed.
    """
    job = _job_inputs(
        collection,
        mode=mode,
        batch=batch,
        sample=sample,
        table=table,
        schema=schema,
        table_names=table_names,
        restart=restart,
    )
    knobs = job.knobs
    source = _mongo_client(job.mongo_cfg, long_running=True)
    log = get_logger()
    try:
        source.connect()
        plan = _profile_plan(source, job, full_profile=full_profile)
        request = TransferRequest(
            collection=job.name,
            plan=plan,
            mongo_database=job.mongo_cfg.get("database") or "",
            mode=job.requested,
            date_query=date_query_from_filter(job.date_filter),
            batch=job.write_batch,
            restart=restart,
            max_rejects=knobs["max_rejects"] if max_rejects is None else max(0, int(max_rejects)),
            overlap_minutes=knobs["overlap_minutes"],
            commit_rows=knobs["commit_rows"],
            commit_mb=knobs["commit_mb"],
            prefetch=knobs["prefetch"],
        )
        result = execute_transfer(
            source,
            job.target,
            request,
            log_extra={"aralık": job.date_field} if job.date_field else None,
        )
        log.info(
            "görev bitti collection=%s mod=%s belgeler=%s satır=%s reddedilen=%s",
            job.name,
            result.mode,
            result.stats.documents,
            result.stats.total_rows,
            result.stats.rejected,
        )
        return result
    finally:
        source.close()


def run_preflight_job(
    collection: str,
    *,
    mode: str | None = None,
    sample: int | None = None,
    table: str | None = None,
    schema: str | None = None,
    table_names: dict[str, str] | None = None,
    full_profile: bool = False,
) -> Report:
    """The pre-flight report for a scheduled job, without writing anything."""
    job = _job_inputs(
        collection,
        mode=mode,
        batch=None,
        sample=sample,
        table=table,
        schema=schema,
        table_names=table_names,
        restart=False,
    )
    source = _mongo_client(job.mongo_cfg, long_running=True)
    try:
        source.connect()
        plan = _profile_plan(source, job, full_profile=full_profile)
        return run_preflight(
            source,
            job.target,
            plan,
            job.name,
            date_query=date_query_from_filter(job.date_filter),
            date_field=job.date_field,
        )
    finally:
        source.close()
