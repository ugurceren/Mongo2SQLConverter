"""Streamlit-free transfer of one collection from saved prefs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from core.inspect import NESTING_OPTIONS, profile_collection, sql_table_ident
from core.logutil import get_logger
from core.mongo import (
    MongoClientWrapper,
    combine_filters,
    date_range_filter,
    decode_mongo_id,
    id_after_filter,
)
from core.mssql import AUTH_NEEDS_PASSWORD, MssqlConnection, auth_mode, available_drivers
from core.settings import (
    _as_schedule_mode,
    load_settings,
    load_transfer_prefs,
    save_sync_watermark,
)
from core.transfer import (
    TransferStats,
    apply_column_selection,
    apply_table_names,
    ensure_tables,
    plan_tables,
    read_root_watermark,
    relax_nullability,
    retarget_plan,
    transfer_collection,
)


@dataclass
class JobResult:
    collection: str
    requested_mode: str
    mode: str
    stats: TransferStats
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)


def _encrypt_flag(raw: Any) -> bool | None:
    text = str(raw or "default").strip().lower()
    if text in ("yes", "true", "1"):
        return True
    if text in ("no", "false", "0"):
        return False
    return None


def _mongo_client(mongo_cfg: dict[str, Any]) -> MongoClientWrapper:
    return MongoClientWrapper(
        uri=mongo_cfg.get("uri") or "",
        database=mongo_cfg.get("database") or "",
        username=mongo_cfg.get("username") or None,
        password=mongo_cfg.get("password") or None,
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


def resolve_write_mode(requested: str, target: MssqlConnection, schema: str, table: str) -> str:
    """`auto`: empty or missing root table → full; otherwise incremental."""
    requested = _as_schedule_mode(requested)
    if requested in ("full", "incremental"):
        return requested
    exists, watermark = read_root_watermark(target, schema, table)
    if exists and watermark:
        return "incremental"
    return "full"


def _write_query(
    mode: str, date_query: dict[str, Any] | None, watermark: dict[str, str] | None
) -> dict | None:
    if mode != "incremental":
        return date_query
    if not watermark:
        return date_query
    try:
        last_id = decode_mongo_id(watermark["last_id"], watermark["last_id_type"])
    except Exception:
        return date_query
    return combine_filters(date_query, id_after_filter(last_id))


def run_transfer_job(
    collection: str,
    *,
    mode: str | None = None,
    batch: int | None = None,
    sample: int | None = None,
    table: str | None = None,
    schema: str | None = None,
    table_names: dict[str, str] | None = None,
) -> JobResult:
    """Profile and write one collection using `config.local.yaml` prefs.

    `--table` / `--rename` from a downloaded bat pin the SQL tables for that
    job so another collection's UI edits cannot redirect it.
    """
    name = (collection or "").strip()
    if not name:
        raise ValueError("Koleksiyon adı boş.")

    cfg = load_settings()
    mongo_cfg = cfg.get("mongodb") or {}
    mssql_cfg = cfg.get("mssql") or {}
    profiler = cfg.get("profiler") or {}
    prefs = load_transfer_prefs(name)
    pinned = bool((table or "").strip())
    if pinned:
        prefs["table"] = str(table).strip()
        prefs["table_names"] = dict(table_names or {})
    elif table_names is not None:
        prefs["table_names"] = dict(table_names)
    if (schema or "").strip():
        prefs["schema"] = str(schema).strip()

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
    nesting = prefs["nesting"] if prefs["nesting"] in allowed_nesting else "hybrid"
    schema = prefs["schema"] or mssql_cfg.get("schema") or "dbo"
    table = sql_table_ident(prefs["table"] or name)
    requested = _as_schedule_mode(mode if mode is not None else prefs["schedule_mode"])
    write_batch = int(batch) if batch is not None else int(prefs["batch"])
    profile_sample = int(sample) if sample is not None else int(prefs["sample"])
    columns = prefs["columns"]
    date_filter = dict(prefs["date_filter"])

    source = _mongo_client(mongo_cfg)
    target = _sql_target(mssql_cfg, mssql_cfg.get("password"), schema)
    log = get_logger()
    created: list[str] = []
    existing: list[str] = []
    try:
        source.connect()
        target.connect()
        resolved = resolve_write_mode(requested, target, schema, table)
        # auto + incremental: a saved date range would skip later _id values.
        write_dates = date_filter
        if requested == "auto" and resolved == "incremental":
            write_dates = {**date_filter, "enabled": False}
        date_query = date_query_from_filter(write_dates)

        log.info(
            "görev başladı collection=%s istenen=%s mod=%s tablo=%s.%s parti=%s örnek=%s kilitli=%s",
            name,
            requested,
            resolved,
            schema,
            table,
            write_batch,
            profile_sample,
            pinned,
        )
        _, plan = profile_collection(
            source,
            name,
            profile_sample,
            schema,
            int(profiler.get("map_min_keys", 30)),
            float(profiler.get("map_max_fill", 0.2)),
            float(profiler.get("headroom", 1.5)),
            nesting=nesting,
            query=date_query,
        )
        plan = retarget_plan(plan, schema=schema, root_table=table)
        if prefs["allow_null"]:
            plan = relax_nullability(plan)
        plan = apply_column_selection(
            plan, columns["exclude"], columns["exclude_tables"]
        )
        plan = apply_table_names(plan, prefs.get("table_names"))
        log.info(
            "aktarım hedefleri collection=%s tablolar=%s",
            name,
            ", ".join(plan_tables(plan)),
        )

        created, existing = ensure_tables(target, plan, recreate=False)
        if created:
            log.info(
                "aktarım tablolar oluşturuldu collection=%s tablolar=%s",
                name,
                ", ".join(created),
            )

        watermark = None
        if resolved == "incremental":
            exists, sql_mark = read_root_watermark(target, schema, plan["root"]["table"])
            if exists:
                watermark = sql_mark

        query = _write_query(resolved, date_query, watermark)
        expected = source.estimated_count(name, query)
        date_note = ""
        if write_dates.get("enabled") and write_dates.get("field"):
            date_note = str(write_dates["field"])
        stats = transfer_collection(
            source,
            target,
            plan,
            name,
            sample=0,
            batch_size=write_batch,
            clear_first=False,
            query=query,
            mode=resolved,
            expected_count=expected,
            log_extra={"aralık": date_note} if date_note else None,
        )
        if stats.last_id and stats.last_id_type:
            save_sync_watermark(name, stats.last_id, stats.last_id_type)
        log.info(
            "görev bitti collection=%s mod=%s belgeler=%s satır=%s",
            name,
            resolved,
            stats.documents,
            stats.total_rows,
        )
        return JobResult(
            collection=name,
            requested_mode=requested,
            mode=resolved,
            stats=stats,
            created=created,
            existing=existing,
        )
    finally:
        source.close()
        target.close()
