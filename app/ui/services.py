"""Shared state and connection helpers for the UI pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st

from core.inspect import (
    NESTING_OPTIONS,
    Profile,
    describe_shape,
    nesting_keys_for,
    profile_collection,
    shape_caption,
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
        with st.spinner("Collection listesi aliniyor..."):
            names = fetch_collections(settings.mongo)
    except Exception as exc:
        st.session_state[COLLECTIONS_KEY] = []
        return [], f"Mongo baglantisi basarisiz: {exc}", None

    st.session_state[COLLECTIONS_KEY] = names
    warning = None
    if not names:
        warning = (
            f"`{settings.mongo.get('database')}` icinde collection yok. "
            "Database adi yanlis olabilir."
        )
    return names, None, warning


def invalidate_collections() -> None:
    st.session_state.pop(COLLECTIONS_KEY, None)


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
        with st.spinner(f"{collection} yapisi okunuyor..."):
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


def nesting_choice(settings: Settings | None = None, collection: str | None = None) -> str:
    """Nesting strategies that make sense for the selected collection's shape."""
    shape = peek_shape(settings, collection) if settings and collection else None
    allowed, default = nesting_keys_for(shape)
    titles = {item[0]: item[1] for item in NESTING_OPTIONS}
    hints = {item[0]: item[2] for item in NESTING_OPTIONS}
    current = st.session_state.get(NESTING_KEY)
    if current not in allowed:
        st.session_state[NESTING_KEY] = default
    choice = st.radio(
        "Ic ice yapi",
        options=allowed,
        format_func=lambda key: titles[key],
        key=NESTING_KEY,
        help="Secenekler, secilen collection'daki dizi ve nesne derinligine gore süzülür.",
    )
    if shape:
        st.caption(shape_caption(shape))
    st.caption(hints[choice])
    return choice


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
            status.caption(f"Profileniyor: {name} ({i}/{len(names)})")
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
