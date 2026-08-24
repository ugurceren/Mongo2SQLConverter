"""Shared state and connection helpers for the UI pages."""

from __future__ import annotations

import html
from dataclasses import dataclass
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
    sql_ident,
)
from core.mongo import MongoClientWrapper
from core.mssql import MssqlConnection, available_drivers
from core.settings import load_connection_overrides, load_settings

COLLECTIONS_KEY = "collections"


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
    def sql_ready(self) -> bool:
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
    return Settings(
        mongo=mongo,
        mssql=mssql,
        profiler=cfg.get("profiler") or {},
        mongo_password=(stored.get("mongodb") or {}).get("password") or mongo.get("password"),
        mssql_password=(stored.get("mssql") or {}).get("password") or mssql.get("password"),
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


def sql_target(
    mssql_cfg: dict[str, Any], password: str | None, schema: str | None = None
) -> MssqlConnection:
    return MssqlConnection(
        server=mssql_cfg.get("server") or "",
        database=mssql_cfg.get("database") or "",
        schema=schema or mssql_cfg.get("schema") or "dbo",
        driver=mssql_cfg.get("driver") or available_drivers()[0],
        trusted_connection=bool(mssql_cfg.get("trusted_connection", True)),
        username=mssql_cfg.get("username") or None,
        password=password,
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
    root = root_table or sql_ident(collection)

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
    settings: Settings, name: str, sample: int, schema: str, nesting: str = "deep"
) -> dict:
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
            )
    finally:
        mongo.close()
    return plan
