"""Shared state and connection helpers for the UI pages."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import streamlit as st

from . import theme

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
    AUTH_NEEDS_USERNAME,
    AUTH_SQL,
    AUTH_WINDOWS,
    AUTH_WINDOWS_USER,
    MssqlConnection,
    auth_mode,
    available_drivers,
)
from core.settings import load_connection_overrides, load_settings

COLLECTIONS_KEY = "collections"
COLLECTIONS_ERROR_KEY = "collections_error"
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


def mongo_client(mongo_cfg: dict[str, Any], long_running: bool = False) -> MongoClientWrapper:
    return MongoClientWrapper(
        uri=mongo_cfg.get("uri") or "",
        database=mongo_cfg.get("database") or "",
        username=mongo_cfg.get("username") or None,
        password=mongo_cfg.get("password") or None,
        long_running=long_running,
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
SQL_CHECKPOINT_KEY = "sql_checkpoints"


def invalidate_sql_watermarks() -> None:
    st.session_state.pop(SQL_WATERMARK_KEY, None)
    st.session_state.pop(SQL_CHECKPOINT_KEY, None)


def sql_checkpoint(settings: Settings, schema: str, table: str):
    """
    This table's checkpoint row, or None (no row, no table, or no connection).
    Cached per session like the watermark; a finished job clears both.
    """
    if not settings.sql_ready or not schema or not table:
        return None
    cache = st.session_state.setdefault(SQL_CHECKPOINT_KEY, {})
    key = f"{settings.mssql.get('server')}|{settings.mssql.get('database')}|{schema}|{table}"
    if key in cache:
        return cache[key]
    from core import checkpoint

    target = sql_target(settings.mssql, settings.mssql_password, schema)
    row = None
    try:
        with st.spinner("Kontrol noktası okunuyor..."):
            # A running batch holds the row briefly; a stuck one must not hang the page.
            target.connect(login_timeout=15, query_timeout=15)
            if target.table_exists(schema, checkpoint.TABLE):
                row = checkpoint.read(target, schema, table)
    except Exception:
        row = None
    finally:
        target.close()
    cache[key] = row
    return row


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


@dataclass
class ConnError:
    """A connection failure: a short line for people and the driver's own text."""

    summary: str
    detail: str = ""


def mongo_error(exc: Exception) -> ConnError:
    from pymongo import errors

    detail = str(exc)
    if isinstance(exc, errors.ConfigurationError):
        summary = "Bağlantı URI'si geçersiz. `mongodb://sunucu:27017` biçiminde yazın."
    elif isinstance(exc, errors.OperationFailure) and exc.code == 18:
        summary = "Kimlik doğrulama başarısız. Kullanıcı adını, şifreyi ve authSource değerini kontrol edin."
    elif isinstance(exc, errors.OperationFailure) and exc.code == 13:
        summary = "Bu hesabın veritabanını okuma yetkisi yok."
    elif isinstance(exc, errors.ConnectionFailure):
        summary = "Sunucuya ulaşılamadı. Adresi, portu ve ağ erişimini kontrol edin."
    else:
        summary = "Mongo bağlantısı kurulamadı."
    return ConnError(summary, detail)


def sql_error(exc: Exception) -> ConnError:
    # MssqlConnection raises RuntimeError with a message that is already readable.
    if isinstance(exc, RuntimeError):
        return ConnError(str(exc))
    detail = str(exc)
    state = str(exc.args[0]) if exc.args else ""
    lowered = detail.lower()
    if "certificate" in lowered:
        summary = (
            "Sunucu sertifikası doğrulanamadı. \"Sunucu sertifikasına doğrulamadan güven\" "
            "kutusunu işaretleyin ya da Driver 17 seçin."
        )
    elif state == "28000":
        summary = "Giriş başarısız. Kullanıcı adını ve şifreyi kontrol edin."
    elif state == "IM002":
        summary = "ODBC sürücüsü bulunamadı. Kurulu bir sürücü seçin."
    elif state in ("08001", "08S01", "HYT00"):
        summary = "Sunucuya ulaşılamadı. Sunucu adını, instance'ı ve ağ erişimini kontrol edin."
    elif state == "42000" and "database" in lowered:
        summary = "Veritabanı açılamadı. Adını ve bu hesabın erişimini kontrol edin."
    else:
        summary = "SQL Server bağlantısı kurulamadı."
    return ConnError(summary, detail)


MONGO_HEALTH_KEY = "m2s_mongo_health"
SQL_HEALTH_KEY = "m2s_sql_health"


def _mongo_signature(cfg: dict[str, Any]) -> str:
    return "|".join(
        str(cfg.get(name) or "").strip() for name in ("uri", "database", "username")
    )


def _sql_signature(cfg: dict[str, Any]) -> str:
    auth = auth_mode(cfg)
    user = str(cfg.get("username") or "").strip() if auth in AUTH_NEEDS_USERNAME else ""
    return "|".join(
        (
            str(cfg.get("server") or "").strip(),
            str(cfg.get("database") or "").strip(),
            auth,
            user,
            str(cfg.get("driver") or ""),
            str(cfg.get("encrypt") or "default"),
            str(bool(cfg.get("trust_certificate", False))),
        )
    )


def _signature(kind: str, cfg: dict[str, Any]) -> str:
    return _mongo_signature(cfg) if kind == "mongo" else _sql_signature(cfg)


def record_health(kind: str, cfg: dict[str, Any], ok: bool) -> None:
    """Remember whether a connection with exactly this config worked, for this session."""
    key = MONGO_HEALTH_KEY if kind == "mongo" else SQL_HEALTH_KEY
    st.session_state[key] = {"signature": _signature(kind, cfg), "ok": bool(ok)}


def connection_health(kind: str, cfg: dict[str, Any]) -> bool | None:
    """True/False from this session's last check of this config; None when never checked."""
    seen = st.session_state.get(MONGO_HEALTH_KEY if kind == "mongo" else SQL_HEALTH_KEY)
    if not seen or seen["signature"] != _signature(kind, cfg):
        return None
    return seen["ok"]


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


def collection_list(settings: Settings) -> tuple[list[str], ConnError | None, str | None]:
    """
    Collection names for the saved connection, cached per session.

    Returns (names, error, warning). The error is cached with the names, so it
    stays on screen until `invalidate_collections` forces a fresh read.
    """
    if COLLECTIONS_KEY in st.session_state:
        return (
            st.session_state[COLLECTIONS_KEY],
            st.session_state.get(COLLECTIONS_ERROR_KEY),
            _empty_warning(settings, st.session_state[COLLECTIONS_KEY]),
        )

    if not settings.mongo_ready:
        st.session_state[COLLECTIONS_KEY] = []
        return [], None, None

    try:
        with st.spinner("Koleksiyon listesi alınıyor..."):
            names = fetch_collections(settings.mongo)
    except Exception as exc:
        error = mongo_error(exc)
        st.session_state[COLLECTIONS_KEY] = []
        st.session_state[COLLECTIONS_ERROR_KEY] = error
        record_health("mongo", settings.mongo, False)
        return [], error, None

    st.session_state[COLLECTIONS_KEY] = names
    st.session_state.pop(COLLECTIONS_ERROR_KEY, None)
    record_health("mongo", settings.mongo, True)
    return names, None, _empty_warning(settings, names)


def _empty_warning(settings: Settings, names: list[str]) -> str | None:
    if names or not settings.mongo_ready or st.session_state.get(COLLECTIONS_ERROR_KEY):
        return None
    return (
        f"`{settings.mongo.get('database')}` içinde koleksiyon yok. "
        "Veritabanı adı yanlış olabilir."
    )


def invalidate_collections() -> None:
    st.session_state.pop(COLLECTIONS_KEY, None)
    st.session_state.pop(COLLECTIONS_ERROR_KEY, None)
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


def nesting_widget_key(collection: str | None) -> str:
    return f"{NESTING_KEY}_{collection or 'none'}"


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
    state_key = nesting_widget_key(collection)
    if st.session_state.get(state_key) not in allowed:
        st.session_state[state_key] = default
    root = root_table or sql_table_ident(collection)

    with theme.collapsible_card(
        f"nesting_{collection}",
        "Bu koleksiyon nasıl kırılsın?",
        shape_caption(shape)
        if shape
        else "Seçenekler bu koleksiyondaki dizi ve nesne derinliğine göre gelir.",
    ):

        cols = st.columns(2)
        picked: str | None = None
        for i, key in enumerate(allowed):
            selected = st.session_state[state_key] == key
            wrap = f"nest_on_{collection}_{key}" if selected else f"nest_off_{collection}_{key}"
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
                        key=f"nest_pick_{collection}_{key}",
                    ):
                        picked = key
        if picked and picked != st.session_state[state_key]:
            st.session_state[state_key] = picked
            st.rerun()
    return st.session_state[state_key]


nesting_card = nesting_choice


def _editor_cell_text(entry: dict[str, Any], *keys: str, fallback: str) -> str:
    """Read a data-editor cell, treating blank / NaN as the generated name."""
    raw: Any = None
    for key in keys:
        if key in entry:
            raw = entry.get(key)
            break
    if raw is None:
        return fallback
    if isinstance(raw, float) and raw != raw:
        return fallback
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return fallback
    return text


def table_names_editor(
    plan: dict[str, Any],
    overrides: dict[str, str],
    *,
    editor_key: str,
) -> tuple[str, dict[str, str], list[str]]:
    """
    Let the user rename generated SQL tables.

    Returns (root_table, child_overrides, duplicate_names). Child overrides are
    keyed by Mongo source path and only kept when they differ from the generated
    name, so a later kök-tablo rename still updates untouched children.
    """
    generated_root = plan["root"]["table"]
    items: list[tuple[str, str, str, str]] = [
        ("", "(kök tablo)", generated_root, generated_root)
    ]
    for child in plan["children"]:
        generated = child["table"]
        custom = str(overrides.get(child["source"]) or "").strip()
        items.append(
            (
                child["source"],
                child["source"],
                generated,
                sql_table_ident(custom) if custom else generated,
            )
        )

    head = st.columns([2, 2, 2.4])
    head[0].caption("kaynak")
    head[1].caption("üretilen")
    head[2].caption("SQL adı")

    typed: list[tuple[str, str, str]] = []
    for i, (path, source_label, generated, sql_name) in enumerate(items):
        cols = st.columns([2, 2, 2.4], vertical_alignment="center")
        cols[0].markdown(f"`{source_label}`")
        cols[1].markdown(f"`{generated}`")
        widget_key = f"{editor_key}_{i}"
        if widget_key not in st.session_state:
            st.session_state[widget_key] = sql_name
        typed.append(
            (
                path,
                generated,
                cols[2].text_input(
                    "SQL adı",
                    key=widget_key,
                    label_visibility="collapsed",
                ),
            )
        )

    new_root = generated_root
    new_overrides: dict[str, str] = {}
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for path, generated, raw in typed:
        name = sql_table_ident(_editor_cell_text({"sql_name": raw}, "sql_name", fallback=generated))
        folded = name.lower()
        if folded in seen:
            duplicates.append(name)
            continue
        seen[folded] = path
        if path == "":
            new_root = name
        elif name != generated:
            new_overrides[path] = name
    return new_root, new_overrides, duplicates


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
            # This plan is what the transfer writes with, so big collections
            # get a larger sample: rare long values then shape the widths.
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
                min_large_sample=50_000,
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
    """Everything that changes the profiled shape. Table names are applied later."""
    return "|".join(
        str(part)
        for part in (
            options.get("collection"),
            options.get("nesting"),
            options.get("sample"),
            options.get("allow_null"),
            repr(query),
        )
    )


def _materialize_plan(plan: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    from core.transfer import relax_nullability, retarget_plan

    plan = retarget_plan(plan, schema=options["schema"], root_table=options["table"])
    if options.get("allow_null"):
        plan = relax_nullability(plan)
    return plan


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
    Renaming the root table retargets the cached profile; it does not scan Mongo
    again.
    """
    if not options.get("collection") or not options.get("table"):
        return None
    cache = st.session_state.setdefault(PLAN_CACHE_KEY, {})
    key = plan_signature(options, query)
    if force or key not in cache:
        cache[key] = profile_one(
            settings,
            options["collection"],
            options["sample"],
            options["schema"],
            nesting=options["nesting"],
            query=query,
        )
    return _materialize_plan(cache[key], options)


def stored_plan(options: dict[str, Any], query: dict[str, Any] | None = None) -> dict | None:
    """Already-profiled plan for these options, or None. Never reads Mongo."""
    cache = st.session_state.get(PLAN_CACHE_KEY) or {}
    raw = cache.get(plan_signature(options, query))
    if raw is None:
        return None
    if not options.get("table"):
        return None
    return _materialize_plan(raw, options)


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
