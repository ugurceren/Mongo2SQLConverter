"""Shared state and connection helpers for the UI pages."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import streamlit as st

from core.inspect import (
    NESTING_OPTIONS,
    Profile,
    describe_shape,
    nesting_keys_for,
    preview_table_count,
    preview_tables,
    profile_collection,
    shape_caption,
    sql_table_ident,
)
from core.logutil import JobLog, get_logger
from core.mongo import MongoClientWrapper
from core.mssql import (
    AUTH_NEEDS_PASSWORD,
    AUTH_SQL,
    AUTH_WINDOWS,
    AUTH_WINDOWS_USER,
    MssqlConnection,
    auth_mode,
    available_drivers,
)
from core.settings import load_connection_overrides, load_settings

COLLECTIONS_KEY = "collections"
# Password typed for this session only, when the user asked not to store it.
SQL_SESSION_PASSWORD = "sql_session_password"

ENCRYPT_CHOICES = ("default", "yes", "no")

# Shared so the Baglantilar form and the sidebar name a mode the same way.
SQL_AUTH_LABELS = {
    AUTH_WINDOWS: "Windows — bu oturum",
    AUTH_SQL: "SQL Server hesabı",
    AUTH_WINDOWS_USER: "Windows — başka hesap",
}


@dataclass
class Settings:
    mongo: dict[str, Any]
    mssql: dict[str, Any]
    profiler: dict[str, Any]
    mongo_password: str | None
    mssql_password: str | None

    @property
    def mongo_ready(self) -> bool:
        return bool(self.mongo.get("uri") and self.mongo.get("database"))

    @property
    def sql_auth(self) -> str:
        return auth_mode(self.mssql)

    @property
    def sql_needs_password(self) -> bool:
        """Target is filled in but the chosen mode has no password to use yet."""
        if not (self.mssql.get("server") and self.mssql.get("database")):
            return False
        return self.sql_auth in AUTH_NEEDS_PASSWORD and not self.mssql_password

    @property
    def sql_ready(self) -> bool:
        # A password mode without a password would offer the transfer page a
        # write that cannot connect, so it does not count as ready.
        if self.sql_needs_password:
            return False
        return bool(self.mssql.get("server") and self.mssql.get("database"))

    @property
    def map_min_keys(self) -> int:
        return int(self.profiler.get("map_min_keys", 30))

    @property
    def map_max_fill(self) -> float:
        return float(self.profiler.get("map_max_fill", 0.2))

    @property
    def headroom(self) -> float:
        return float(self.profiler.get("headroom", 1.5))

    @property
    def schema(self) -> str:
        return self.mssql.get("schema") or "dbo"


def load_state() -> Settings:
    cfg = load_settings()
    stored = load_connection_overrides()
    mongo = cfg.get("mongodb") or {}
    mssql = cfg.get("mssql") or {}
    saved_sql_password = (stored.get("mssql") or {}).get("password") or mssql.get("password")
    return Settings(
        mongo=mongo,
        mssql=mssql,
        profiler=cfg.get("profiler") or {},
        mongo_password=(stored.get("mongodb") or {}).get("password") or mongo.get("password"),
        # A session password wins: it is the one the user just typed and chose
        # not to write to disk.
        mssql_password=st.session_state.get(SQL_SESSION_PASSWORD) or saved_sql_password,
    )


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------


def mongo_client(mongo_cfg: dict[str, Any]) -> MongoClientWrapper:
    return MongoClientWrapper(
        uri=mongo_cfg.get("uri") or "",
        database=mongo_cfg.get("database") or "",
        username=mongo_cfg.get("username") or None,
        password=mongo_cfg.get("password") or None,
    )


def _uri_authority(uri: str | None) -> str:
    """The `user:pass@host` part of a Mongo URI, without scheme, path or options."""
    text = (uri or "").strip()
    if not text:
        return ""
    authority = text.partition("://")[2] or text
    return authority.split("/", 1)[0].split("?", 1)[0]


def mongo_endpoint(uri: str | None) -> str:
    """Host(s) a URI points at. Credentials are dropped: this goes on screen."""
    return _uri_authority(uri).rpartition("@")[2] or "—"


def mongo_identity(mongo_cfg: dict[str, Any]) -> str:
    """Who the connection logs in as, whether that came from the form or the URI."""
    typed = (mongo_cfg.get("username") or "").strip()
    if typed:
        return typed
    embedded = _uri_authority(mongo_cfg.get("uri")).rpartition("@")[0]
    if embedded:
        return embedded.split(":", 1)[0] + " (URI içinde)"
    return "kimlik yok"


def mongo_uri_option(uri: str | None, name: str) -> str | None:
    """One query option from a Mongo URI (`authSource`, `replicaSet`, …)."""
    query = (uri or "").partition("?")[2]
    if not query:
        return None
    wanted = name.lower()
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key.lower() == wanted and value:
            return value
    return None


def encrypt_flag(raw: Any) -> bool | None:
    """Config's encrypt choice as a tristate; None leaves it to the driver."""
    text = str(raw or "default").strip().lower()
    if text in ("yes", "true", "1"):
        return True
    if text in ("no", "false", "0"):
        return False
    return None


def sql_target(
    mssql_cfg: dict[str, Any], password: str | None, schema: str | None = None
) -> MssqlConnection:
    return MssqlConnection(
        server=mssql_cfg.get("server") or "",
        database=mssql_cfg.get("database") or "",
        schema=schema or mssql_cfg.get("schema") or "dbo",
        driver=mssql_cfg.get("driver") or available_drivers()[0],
        trusted_connection=bool(mssql_cfg.get("trusted_connection", True)),
        auth=auth_mode(mssql_cfg),
        username=mssql_cfg.get("username") or None,
        password=password,
        encrypt=encrypt_flag(mssql_cfg.get("encrypt")),
        trust_certificate=bool(mssql_cfg.get("trust_certificate", False)),
    )


SQL_WATERMARK_KEY = "sql_watermarks"


def invalidate_sql_watermarks() -> None:
    st.session_state.pop(SQL_WATERMARK_KEY, None)


def sql_table_watermark(
    settings: Settings, schema: str, table: str
) -> tuple[str, dict[str, str] | None]:
    """
    Last `mongo_id` in the SQL root table.

    Returns (status, watermark):
    - "sql": table exists (watermark is None when the table is empty)
    - "missing": table or SQL connection is not available
    """
    if not settings.sql_ready or not schema or not table:
        return "missing", None
    cache = st.session_state.setdefault(SQL_WATERMARK_KEY, {})
    key = (
        f"{settings.mssql.get('server')}|{settings.mssql.get('database')}|"
        f"{schema}|{table}"
    )
    if key in cache:
        return cache[key]
    from core.transfer import read_root_watermark

    target = sql_target(settings.mssql, settings.mssql_password, schema)
    try:
        with st.spinner("SQL tablosundaki son id okunuyor..."):
            target.connect()
            exists, watermark = read_root_watermark(target, schema, table)
    except Exception:
        return "missing", None
    finally:
        target.close()
    status = "sql" if exists else "missing"
    cache[key] = (status, watermark)
    return status, watermark


def mongo_cfg_from_form(uri: str, database: str, user: str, password: str | None) -> dict[str, Any]:
    return {
        "uri": uri,
        "database": database,
        "username": user or None,
        "password": password,
    }


def fetch_collections(mongo_cfg: dict[str, Any]) -> list[str]:
    mongo = mongo_client(mongo_cfg)
    try:
        mongo.connect()
        return mongo.list_collections()
    finally:
        mongo.close()


def collection_list(settings: Settings) -> tuple[list[str], str | None, str | None]:
    """
    Collection names for the saved connection, cached per session.

    Returns (names, error, warning). Call `invalidate_collections` to force a
    fresh read on the next run.
    """
    if COLLECTIONS_KEY in st.session_state:
        return st.session_state[COLLECTIONS_KEY], None, None

    if not settings.mongo_ready:
        st.session_state[COLLECTIONS_KEY] = []
        return [], None, None

    try:
        with st.spinner("Koleksiyon listesi alınıyor..."):
            names = fetch_collections(settings.mongo)
    except Exception as exc:
        st.session_state[COLLECTIONS_KEY] = []
        return [], f"Mongo bağlantısı başarısız: {exc}", None

    st.session_state[COLLECTIONS_KEY] = names
    warning = None
    if not names:
        warning = (
            f"`{settings.mongo.get('database')}` içinde koleksiyon yok. "
            "Veritabanı adı yanlış olabilir."
        )
    return names, None, warning


def invalidate_collections() -> None:
    st.session_state.pop(COLLECTIONS_KEY, None)
    st.session_state.pop(COUNTS_KEY, None)
    st.session_state.pop(PLAN_CACHE_KEY, None)
    st.session_state.pop("field_indexes", None)
    st.session_state.pop(DATE_BOUNDS_KEY, None)
    st.session_state.pop(RANGE_INDEX_KEY, None)
    st.session_state.pop(RANGE_INDEX_MISS, None)


COUNTS_KEY = "collection_counts"


def format_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def collection_count(settings: Settings, name: str) -> int | None:
    """Estimated document count, cached per collection for this session."""
    if not settings.mongo_ready or not name:
        return None
    cache = st.session_state.setdefault(COUNTS_KEY, {})
    if name in cache:
        return cache[name]
    mongo = mongo_client(settings.mongo)
    try:
        mongo.connect()
        cache[name] = mongo.estimated_count(name)
    except Exception:
        cache[name] = None
    finally:
        mongo.close()
    return cache[name]


def collection_count_caption(
    settings: Settings, name: str | None, sample: int | None = None
) -> None:
    if not name:
        return
    total = collection_count(settings, name)
    if total is None:
        return
    text = f"Bu koleksiyonda yaklaşık **{format_int(total)}** kayıt var."
    if sample is not None and total > 0:
        if sample <= 0:
            text += " Örnek 0 = tam tarama."
        elif sample >= total:
            text += f" Örnek ({format_int(sample)}) koleksiyonun tamamını kapsar."
        else:
            pct = 100.0 * sample / total
            shown = f"{pct:.2f}" if pct < 0.1 else f"{pct:.1f}"
            shown = shown.replace(".", ",")
            text += f" Örnek {format_int(sample)}, kayıtların yaklaşık %{shown}'ü."
    st.caption(text)


SELECTED_COLLECTION_KEY = "selected_collection"


def remember_collection(name: str | None) -> None:
    """Keep the discovery collection so transfer can open on the same one."""
    if name:
        st.session_state[SELECTED_COLLECTION_KEY] = name


def apply_remembered_collection(widget_key: str, collections: list[str]) -> None:
    """Pre-fill a collection selectbox from discovery, unless the user already changed it."""
    source = st.session_state.get(SELECTED_COLLECTION_KEY)
    if source not in collections:
        return
    stamp = f"{widget_key}_from_discovery"
    if st.session_state.get(stamp) != source:
        st.session_state[widget_key] = source
        st.session_state[stamp] = source


NESTING_KEY = "nesting_mode"
SHAPE_KEY = "shape_peek"
PEEK_SAMPLE = 5000


def peek_shape(settings: Settings, collection: str) -> dict[str, Any] | None:
    """Cheap sample profile so nesting options match this collection."""
    cache = st.session_state.setdefault(SHAPE_KEY, {})
    cache_key = f"{collection}:{PEEK_SAMPLE}"
    if cache_key in cache:
        return cache[cache_key]
    if not settings.mongo_ready or not collection:
        return None
    mongo = mongo_client(settings.mongo)
    try:
        mongo.connect()
        with st.spinner(f"{collection} yapısı okunuyor..."):
            profile = Profile()
            for doc in mongo.iter_documents(collection, sample=PEEK_SAMPLE):
                profile.add_document(doc)
    except Exception:
        return None
    finally:
        mongo.close()
    if not profile.documents:
        cache[cache_key] = {
            "documents": 0,
            "top_arrays": [],
            "nested_arrays": [],
            "top_objects": [],
            "nested_objects": [],
        }
    else:
        cache[cache_key] = describe_shape(profile)
    return cache[cache_key]


def nesting_choice(
    settings: Settings, collection: str, root_table: str | None = None
) -> str:
    """Ask how to split nested fields — only after a collection is chosen."""
    shape = peek_shape(settings, collection)
    allowed, default = nesting_keys_for(shape)
    titles = {item[0]: item[1] for item in NESTING_OPTIONS}
    hints = {item[0]: item[2] for item in NESTING_OPTIONS}
    if st.session_state.get(NESTING_KEY) not in allowed:
        st.session_state[NESTING_KEY] = default
    root = root_table or sql_table_ident(collection)

    with st.container(border=True):
        st.markdown(
            '<div class="m2s-nest-title">Bu koleksiyon nasıl kırılsın?</div>',
            unsafe_allow_html=True,
        )
        if shape:
            st.caption(shape_caption(shape))
        else:
            st.caption("Seçenekler bu koleksiyondaki dizi ve nesne derinliğine göre gelir.")

        cols = st.columns(2)
        picked: str | None = None
        for i, key in enumerate(allowed):
            selected = st.session_state[NESTING_KEY] == key
            wrap = f"nest_on_{key}" if selected else f"nest_off_{key}"
            tables, note = preview_tables(key, root, shape)
            shown = tables[:8]
            extra = f"\n+{len(tables) - 8} daha" if len(tables) > 8 else ""
            preview = html.escape("\n".join(shown) + extra)
            n_tables = preview_table_count(key, shape)
            with cols[i % 2]:
                st.markdown(
                    f'<div class="m2s-nest-count">{n_tables} tablo</div>',
                    unsafe_allow_html=True,
                )
                with st.container(border=True, key=wrap):
                    st.markdown(f"**{titles[key]}**")
                    st.caption(hints[key])
                    st.markdown(
                        f'<div class="m2s-table-preview">{preview}</div>',
                        unsafe_allow_html=True,
                    )
                    st.caption(note)
                    if st.button(
                        "Seçildi" if selected else "Bunu kullan",
                        type="primary" if selected else "secondary",
                        width="stretch",
                        key=f"nest_pick_{key}",
                    ):
                        picked = key
        if picked and picked != st.session_state[NESTING_KEY]:
            st.session_state[NESTING_KEY] = picked
            st.rerun()
    return st.session_state[NESTING_KEY]


nesting_card = nesting_choice


# --------------------------------------------------------------------------
# profiling
# --------------------------------------------------------------------------


def profile_many(
    settings: Settings, names: list[str], sample: int, schema: str, nesting: str = "deep"
) -> tuple[list[dict], list[str], list[str], int]:
    """Profile several collections, reporting progress. Errors do not stop the run."""
    plans: list[dict] = []
    skipped: list[str] = []
    errors: list[str] = []
    total_docs = 0

    mongo = mongo_client(settings.mongo)
    status = st.empty()
    bar = st.progress(0)
    try:
        mongo.connect()
        for i, name in enumerate(names, 1):
            status.caption(f"Profilleniyor: {name} ({i}/{len(names)})")
            try:
                profile, plan = profile_collection(
                    mongo,
                    name,
                    sample,
                    schema,
                    settings.map_min_keys,
                    settings.map_max_fill,
                    settings.headroom,
                    nesting=nesting,
                )
                if profile.documents == 0:
                    skipped.append(name)
                else:
                    plans.append(plan)
                    total_docs += profile.documents
            except Exception as exc:
                errors.append(f"{name}: {exc}")
            bar.progress(i / len(names))
    finally:
        mongo.close()
        status.empty()
        bar.empty()
    return plans, skipped, errors, total_docs


def profile_one(
    settings: Settings,
    name: str,
    sample: int,
    schema: str,
    nesting: str = "deep",
    query: dict[str, Any] | None = None,
) -> dict:
    job = JobLog("profil", collection=name, örnek=sample, sema=schema, nesting=nesting)
    job.start()
    mongo = mongo_client(settings.mongo)
    try:
        mongo.connect()
        with st.spinner(f"{name} profilleniyor..."):
            _, plan = profile_collection(
                mongo,
                name,
                sample,
                schema,
                settings.map_min_keys,
                settings.map_max_fill,
                settings.headroom,
                nesting=nesting,
                query=query,
            )
        job.done(belgeler=plan.get("documents"))
        return plan
    except Exception as exc:
        get_logger().exception("profil başarısız collection=%s error=%s", name, exc)
        raise
    finally:
        mongo.close()


PLAN_CACHE_KEY = "plan_cache"


def plan_signature(options: dict[str, Any], query: dict[str, Any] | None) -> str:
    """Everything that changes the profiled plan; the column picker is not part of it."""
    return "|".join(
        str(part)
        for part in (
            options.get("collection"),
            options.get("schema"),
            options.get("table"),
            options.get("nesting"),
            options.get("sample"),
            options.get("allow_null"),
            repr(query),
        )
    )


def cached_plan(
    settings: Settings,
    options: dict[str, Any],
    query: dict[str, Any] | None = None,
    force: bool = False,
) -> dict | None:
    """
    Profiled plan for the current options, kept for this session.

    The column picker and the write must agree on one plan, and repeated button
    presses should not re-profile the collection. Column exclusions are applied
    on top of this plan by the caller, so they never invalidate the cache.
    """
    from core.transfer import relax_nullability, retarget_plan

    if not options.get("collection") or not options.get("table"):
        return None
    cache = st.session_state.setdefault(PLAN_CACHE_KEY, {})
    key = plan_signature(options, query)
    if not force and key in cache:
        return cache[key]

    plan = profile_one(
        settings,
        options["collection"],
        options["sample"],
        options["schema"],
        nesting=options["nesting"],
        query=query,
    )
    plan = retarget_plan(plan, schema=options["schema"], root_table=options["table"])
    if options.get("allow_null"):
        plan = relax_nullability(plan)
    cache[key] = plan
    return plan


def stored_plan(options: dict[str, Any], query: dict[str, Any] | None = None) -> dict | None:
    """Already-profiled plan for these options, or None. Never reads Mongo."""
    cache = st.session_state.get(PLAN_CACHE_KEY) or {}
    return cache.get(plan_signature(options, query))


def invalidate_plans() -> None:
    st.session_state.pop(PLAN_CACHE_KEY, None)


# --------------------------------------------------------------------------
# date range helpers
# --------------------------------------------------------------------------


DATE_BOUNDS_KEY = "date_field_bounds"
RANGE_INDEX_KEY = "range_index_fields"
RANGE_INDEX_MISS = "range_index_fields_miss"


def range_index_fields(settings: Settings, collection: str) -> set[str] | None:
    """Leading keys of range indexes on this collection, or None if Mongo did not answer."""
    if not settings.mongo_ready or not collection:
        return None
    cache = st.session_state.setdefault(RANGE_INDEX_KEY, {})
    if collection in cache:
        return cache[collection]
    missed = st.session_state.setdefault(RANGE_INDEX_MISS, set())
    if collection in missed:
        return None
    mongo = mongo_client(settings.mongo)
    try:
        mongo.connect()
        cache[collection] = mongo.leading_range_index_fields(collection)
        return cache[collection]
    except Exception:
        missed.add(collection)
        return None
    finally:
        mongo.close()


def date_field_options(settings: Settings, collection: str) -> list[dict[str, Any]]:
    """Date-typed fields from the shape peek, tagged and sorted by range-index usability."""
    shape = peek_shape(settings, collection)
    if not shape:
        return []
    indexed = range_index_fields(settings, collection)
    fields: list[dict[str, Any]] = []
    for raw in shape.get("date_fields") or []:
        item = dict(raw)
        path = item.get("path") or ""
        if indexed is None:
            item["indexed"] = None
        else:
            item["indexed"] = bool(path) and path in indexed
        fields.append(item)

    def sort_key(item: dict[str, Any]) -> tuple:
        flag = item.get("indexed")
        rank = 0 if flag is True else 1 if flag is False else 2
        return (rank, -(item.get("present") or 0), item.get("path") or "")

    fields.sort(key=sort_key)
    return fields


def date_field_bounds(
    settings: Settings, collection: str, field: str
) -> tuple[datetime | None, datetime | None]:
    """Min/max BSON dates for one field, cached per collection+field this session.

    Only safe when `field` leads a range index; the caller must skip unindexed fields.
    """
    if not settings.mongo_ready or not collection or not field:
        return None, None
    cache = st.session_state.setdefault(DATE_BOUNDS_KEY, {})
    key = f"{collection}|{field}"
    if key in cache:
        return cache[key]
    mongo = mongo_client(settings.mongo)
    try:
        mongo.connect()
        with st.spinner("Tarih aralığı okunuyor..."):
            bounds = mongo.date_bounds(collection, field)
        cache[key] = bounds
        return bounds
    except Exception:
        return None, None
    finally:
        mongo.close()


def count_matching(
    settings: Settings, collection: str, query: dict[str, Any] | None
) -> int | None:
    if not settings.mongo_ready or not collection:
        return None
    mongo = mongo_client(settings.mongo)
    try:
        mongo.connect()
        with st.spinner("Kayıtlar sayılıyor..."):
            return mongo.estimated_count(collection, query)
    except Exception:
        return None
    finally:
        mongo.close()
