"""Connections page: MongoDB source and, optionally, the SQL Server target."""

from __future__ import annotations

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    invalidate_collections,
    mongo_cfg_from_form,
    mongo_client,
    sql_target,
)
from core.mssql import MssqlConnection, available_drivers
from core.settings import save_connection_overrides


SAVE_FLASH = "conn_saved"

# Suggestions only — never written to config.yaml.
HINT_MONGO_URI = "mongodb://localhost:27017"
HINT_MONGO_DB = "mydb"
HINT_MONGO_USER = "kullanıcı"
HINT_SQL_SERVER = "localhost"
HINT_SQL_DB = "MyWarehouse"
HINT_SQL_SCHEMA = "dbo"
HINT_SQL_USER = "sa"

# Example-file values treated as hints, not filled-in data.
_EXAMPLE_MONGO_URI = (HINT_MONGO_URI, "mongodb://host:27017/?authSource=admin", "mongodb://host:27017")
_EXAMPLE_MONGO_DB = (HINT_MONGO_DB,)
_EXAMPLE_MONGO_USER = (HINT_MONGO_USER, "user")
_EXAMPLE_SQL_SERVER = (HINT_SQL_SERVER, r"srv\INSTANCE")
_EXAMPLE_SQL_DB = (HINT_SQL_DB,)
_EXAMPLE_SQL_SCHEMA = (HINT_SQL_SCHEMA,)
_EXAMPLE_SQL_USER = (HINT_SQL_USER, "user")


def _hint_field(saved: str | None, hint: str, examples: tuple[str, ...]) -> tuple[str, str]:
    """Show saved text only when it is a real value; examples stay as placeholders."""
    raw = (saved or "").strip()
    if not raw or raw in examples:
        return "", hint
    return raw, hint


def _or_saved(typed: str, saved: str | None) -> str:
    return (typed or "").strip() or (saved or "")


def _clear_example_widget(key: str, examples: tuple[str, ...]) -> None:
    current = st.session_state.get(key)
    if isinstance(current, str) and current.strip() in examples:
        st.session_state[key] = ""


def _hint_input(
    label: str,
    key: str,
    saved: str | None,
    hint: str,
    examples: tuple[str, ...],
    **kwargs,
) -> str:
    shown, placeholder = _hint_field(saved, hint, examples)
    if key not in st.session_state:
        st.session_state[key] = shown
    else:
        _clear_example_widget(key, examples)
    return st.text_input(label, placeholder=placeholder, key=key, **kwargs)


def _mark_saved(label: str) -> None:
    st.session_state[SAVE_FLASH] = label
    st.toast(f"{label} kaydedildi", icon="✅")


def _show_saved() -> None:
    label = st.session_state.pop(SAVE_FLASH, None)
    if not label:
        return
    st.success(f"**{label}** ayarları kaydedildi.")


def _test_mongo(mongo_cfg: dict) -> None:
    mongo = mongo_client(mongo_cfg)
    try:
        info = mongo.test()
        collections = info["collections"]
        st.success(
            f"Bağlantı kuruldu — `{info['database']}` ({len(collections)} koleksiyon)"
        )
        if collections:
            preview = ", ".join(collections[:12])
            extra = f" … +{len(collections) - 12}" if len(collections) > 12 else ""
            st.caption(f"Koleksiyonlar: {preview}{extra}")
        else:
            st.warning(
                f"`{info['database']}` içinde koleksiyon yok. Veritabanı adı yanlış olabilir."
            )
        if info["databases"]:
            st.caption("Erişilebilir veritabanları: " + ", ".join(info["databases"]))
    except Exception as exc:
        st.error(f"Bağlantı kurulamadı: {exc}")
    finally:
        mongo.close()


def _test_sql(target: MssqlConnection) -> None:
    try:
        target.connect()
        login, database = target.test()
        st.success(f"Bağlantı kuruldu — {database} / oturum: {login}")
    except Exception as exc:
        st.error(f"Bağlantı kurulamadı: {exc}")
    finally:
        target.close()


def _mongo_card(settings: Settings) -> None:
    with st.container(border=True):
        theme.section_heading(
            "Kaynak",
            "MongoDB",
            "Şema keşfi ve aktarım için okunan veritabanı. Zorunlu. "
            "Gri yazı örnektir; Kaydet'e basınca config.local.yaml yazar.",
        )
        with st.form("mongo_conn"):
            left, right = st.columns(2)
            with left:
                uri = _hint_input(
                    "Bağlantı URI",
                    "mongo_uri",
                    settings.mongo.get("uri"),
                    HINT_MONGO_URI,
                    _EXAMPLE_MONGO_URI,
                )
                database = _hint_input(
                    "Veritabanı",
                    "mongo_db",
                    settings.mongo.get("database"),
                    HINT_MONGO_DB,
                    _EXAMPLE_MONGO_DB,
                )
            with right:
                user = _hint_input(
                    "Kullanıcı",
                    "mongo_user",
                    settings.mongo.get("username"),
                    HINT_MONGO_USER,
                    _EXAMPLE_MONGO_USER,
                )
                password = st.text_input(
                    "Şifre",
                    type="password",
                    placeholder="kayıtlı — değiştirmek için yazın"
                    if settings.mongo_password
                    else "boş bırakın veya yazın",
                    help="Boş bırakılırsa kayıtlı şifre korunur. Öneri gösterilmez.",
                )
            actions = st.columns([1, 1, 2])
            with actions[0]:
                do_test = st.form_submit_button(
                    "Bağlantıyı dene", key="mongo_test", width="stretch"
                )
            with actions[1]:
                do_save = st.form_submit_button(
                    "Kaydet", type="primary", width="stretch"
                )

        if do_test:
            _test_mongo(
                mongo_cfg_from_form(
                    _or_saved(uri, settings.mongo.get("uri")),
                    _or_saved(database, settings.mongo.get("database")),
                    _or_saved(user, settings.mongo.get("username")),
                    password or settings.mongo_password,
                )
            )
        if do_save:
            save_connection_overrides(
                mongodb={
                    "uri": _or_saved(uri, settings.mongo.get("uri")),
                    "database": _or_saved(database, settings.mongo.get("database")),
                    "username": _or_saved(user, settings.mongo.get("username")),
                    "password": password or None,
                },
                mssql={},
            )
            _mark_saved("MongoDB")
            invalidate_collections()
            st.rerun()


def _sql_card(settings: Settings) -> None:
    with st.container(border=True):
        theme.section_heading(
            "Hedef",
            "SQL Server",
            "Yalnızca veri aktarırken gerekir. Şema keşfi bu bağlantıyı kullanmaz. "
            "Gri yazı örnektir; gerçek değerleri siz yazın.",
        )
        trusted_default = bool(settings.mssql.get("trusted_connection", True))
        if "sql_auth" not in st.session_state:
            st.session_state["sql_auth"] = "Windows" if trusted_default else "SQL Server"

        left, right = st.columns(2)
        with left:
            st.markdown("**Yazılacak yer**")
            server = _hint_input(
                "Sunucu",
                "sql_server",
                settings.mssql.get("server"),
                HINT_SQL_SERVER,
                _EXAMPLE_SQL_SERVER,
            )
            database = _hint_input(
                "Veritabanı",
                "sql_db",
                settings.mssql.get("database"),
                HINT_SQL_DB,
                _EXAMPLE_SQL_DB,
            )
            schema = _hint_input(
                "Şema",
                "sql_schema",
                settings.mssql.get("schema"),
                HINT_SQL_SCHEMA,
                _EXAMPLE_SQL_SCHEMA,
            )
        with right:
            st.markdown("**Kimlik doğrulama**")
            drivers = available_drivers()
            current = settings.mssql.get("driver") or "ODBC Driver 17 for SQL Server"
            if current not in drivers:
                drivers = [current, *drivers]
            driver = st.selectbox(
                "ODBC sürücü",
                drivers,
                index=drivers.index(current),
            )
            auth = st.radio(
                "Yöntem",
                ("Windows", "SQL Server"),
                horizontal=True,
                key="sql_auth",
            )
            windows = auth == "Windows"
            user = _hint_input(
                "Kullanıcı",
                "sql_user",
                None if windows else settings.mssql.get("username"),
                HINT_SQL_USER,
                _EXAMPLE_SQL_USER,
                disabled=windows,
            )
            password = st.text_input(
                "Şifre",
                type="password",
                disabled=windows,
                placeholder="Windows oturumu"
                if windows
                else (
                    "kayıtlı — değiştirmek için yazın"
                    if settings.mssql_password
                    else "boş"
                ),
            )
            if windows:
                st.caption("Windows oturumu kullanılır; kullanıcı ve şifre gerekmez.")

        actions = st.columns([1, 1, 2])
        with actions[0]:
            do_test = st.button("Bağlantıyı dene", key="sql_test", width="stretch")
        with actions[1]:
            do_save = st.button("Kaydet", type="primary", key="sql_save", width="stretch")

        if do_test:
            _test_sql(
                sql_target(
                    {
                        "server": _or_saved(server, settings.mssql.get("server")),
                        "database": _or_saved(database, settings.mssql.get("database")),
                        "schema": _or_saved(schema, settings.mssql.get("schema")),
                        "driver": driver,
                        "trusted_connection": windows,
                        "username": "" if windows else _or_saved(user, settings.mssql.get("username")),
                    },
                    None if windows else (password or settings.mssql_password),
                )
            )
        if do_save:
            payload: dict = {
                "server": _or_saved(server, settings.mssql.get("server")),
                "database": _or_saved(database, settings.mssql.get("database")),
                "schema": _or_saved(schema, settings.mssql.get("schema")),
                "driver": driver,
                "trusted_connection": windows,
            }
            if not windows:
                payload["username"] = _or_saved(user, settings.mssql.get("username"))
                payload["password"] = password or None
            save_connection_overrides(mongodb={}, mssql=payload)
            _mark_saved("SQL Server")
            st.rerun()


def render(settings: Settings) -> None:
    theme.page_header(
        "Yapılandırma",
        "Bağlantılar",
        "Kaynak ve hedef ayrı tutulur: Mongo olmadan hiçbir şey çalışmaz, SQL yalnızca "
        "veri yazarken devreye girer. Kaydet, değerleri bu makinede tutar; git'e yazılmaz.",
        step="connections",
    )
    _show_saved()
    _mongo_card(settings)
    st.write("")
    _sql_card(settings)
    if settings.mongo_ready:
        st.write("")
        theme.page_cta(
            "discovery",
            "Şema keşfine geç",
            ":material/schema:",
            "cta_to_discovery",
        )
