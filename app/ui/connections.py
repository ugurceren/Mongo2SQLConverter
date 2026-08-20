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
from core.settings import LOCAL_CONFIG_PATH, save_connection_overrides


SAVE_FLASH = "conn_saved"

# Suggestions only — never written to config.yaml.
HINT_MONGO_URI = "mongodb://localhost:27017"
HINT_MONGO_DB = "mydb"
HINT_MONGO_USER = "kullanici"
HINT_SQL_SERVER = "localhost"
HINT_SQL_DB = "MyWarehouse"
HINT_SQL_SCHEMA = "dbo"
HINT_SQL_USER = "sa"


def _mark_saved(label: str) -> None:
    st.session_state[SAVE_FLASH] = label
    st.toast(f"{label} kaydedildi", icon="✅")


def _show_saved() -> None:
    label = st.session_state.pop(SAVE_FLASH, None)
    if not label:
        return
    st.success(f"**{label}** ayarlari kaydedildi.")
    st.caption(str(LOCAL_CONFIG_PATH))


def _test_mongo(mongo_cfg: dict) -> None:
    mongo = mongo_client(mongo_cfg)
    try:
        info = mongo.test()
        collections = info["collections"]
        st.success(
            f"Baglanti kuruldu — `{info['database']}` ({len(collections)} collection)"
        )
        if collections:
            preview = ", ".join(collections[:12])
            extra = f" … +{len(collections) - 12}" if len(collections) > 12 else ""
            st.caption(f"Collection'lar: {preview}{extra}")
        else:
            st.warning(
                f"`{info['database']}` icinde collection yok. Database adi yanlis olabilir."
            )
        if info["databases"]:
            st.caption("Erisebilir database'ler: " + ", ".join(info["databases"]))
    except Exception as exc:
        st.error(f"Baglanti kurulamadi: {exc}")
    finally:
        mongo.close()


def _test_sql(target: MssqlConnection) -> None:
    try:
        target.connect()
        login, database = target.test()
        st.success(f"Baglanti kuruldu — {database} / login: {login}")
    except Exception as exc:
        st.error(f"Baglanti kurulamadi: {exc}")
    finally:
        target.close()


def _mongo_card(settings: Settings) -> None:
    with st.container(border=True):
        theme.section_heading(
            "Kaynak",
            "MongoDB",
            "Sema kesfi ve aktarim icin okunan veritabani. Zorunlu. "
            "Gri yazi ornektir; Kaydet'e basinca config.local.yaml yazar.",
        )
        with st.form("mongo_conn"):
            left, right = st.columns(2)
            with left:
                uri = st.text_input(
                    "Baglanti URI",
                    value=settings.mongo.get("uri") or "",
                    placeholder=HINT_MONGO_URI,
                )
                database = st.text_input(
                    "Database",
                    value=settings.mongo.get("database") or "",
                    placeholder=HINT_MONGO_DB,
                )
            with right:
                user = st.text_input(
                    "Kullanici",
                    value=settings.mongo.get("username") or "",
                    placeholder=HINT_MONGO_USER,
                )
                password = st.text_input(
                    "Sifre",
                    type="password",
                    placeholder="kayitli — degistirmek icin yazin"
                    if settings.mongo_password
                    else "bos birakin veya yazin",
                    help="Bos birakilirsa kayitli sifre korunur. Oneri gosterilmez.",
                )
            actions = st.columns([1, 1, 2])
            with actions[0]:
                do_test = st.form_submit_button(
                    "Test connection", key="mongo_test", width="stretch"
                )
            with actions[1]:
                do_save = st.form_submit_button(
                    "Kaydet", type="primary", width="stretch"
                )

        if do_test:
            _test_mongo(
                mongo_cfg_from_form(uri, database, user, password or settings.mongo_password)
            )
        if do_save:
            save_connection_overrides(
                mongodb={
                    "uri": uri,
                    "database": database,
                    "username": user,
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
            "Yalnizca veri aktarirken gerekir. Sema kesfi bu baglantiyi kullanmaz. "
            "Gri yazi ornektir; gercek degerleri siz yazin.",
        )
        trusted_default = bool(settings.mssql.get("trusted_connection", True))
        if "sql_auth" not in st.session_state:
            st.session_state["sql_auth"] = "Windows" if trusted_default else "SQL Server"

        left, right = st.columns(2)
        with left:
            st.markdown("**Yazilacak yer**")
            server = st.text_input(
                "Sunucu",
                value=settings.mssql.get("server") or "",
                placeholder=HINT_SQL_SERVER,
            )
            database = st.text_input(
                "Database",
                value=settings.mssql.get("database") or "",
                placeholder=HINT_SQL_DB,
            )
            schema = st.text_input(
                "Sema",
                value=settings.mssql.get("schema") or "",
                placeholder=HINT_SQL_SCHEMA,
            )
        with right:
            st.markdown("**Kimlik dogrulama**")
            drivers = available_drivers()
            current = settings.mssql.get("driver") or drivers[0]
            driver = st.selectbox(
                "ODBC surucu",
                drivers,
                index=drivers.index(current) if current in drivers else 0,
            )
            auth = st.radio(
                "Yontem",
                ("Windows", "SQL Server"),
                horizontal=True,
                key="sql_auth",
            )
            windows = auth == "Windows"
            user = st.text_input(
                "Kullanici",
                value=settings.mssql.get("username") or "",
                placeholder=HINT_SQL_USER,
                disabled=windows,
            )
            password = st.text_input(
                "Sifre",
                type="password",
                disabled=windows,
                placeholder="Windows oturumu"
                if windows
                else (
                    "kayitli — degistirmek icin yazin"
                    if settings.mssql_password
                    else "bos"
                ),
            )
            if windows:
                st.caption("Windows oturumu kullanilir; kullanici ve sifre gerekmez.")

        actions = st.columns([1, 1, 2])
        with actions[0]:
            do_test = st.button("Test connection", key="sql_test", width="stretch")
        with actions[1]:
            do_save = st.button("Kaydet", type="primary", key="sql_save", width="stretch")

        if do_test:
            _test_sql(
                sql_target(
                    {
                        "server": server,
                        "database": database,
                        "schema": schema,
                        "driver": driver,
                        "trusted_connection": windows,
                        "username": "" if windows else user,
                    },
                    None if windows else (password or settings.mssql_password),
                )
            )
        if do_save:
            payload: dict = {
                "server": server,
                "database": database,
                "schema": schema,
                "driver": driver,
                "trusted_connection": windows,
            }
            if not windows:
                payload["username"] = user
                payload["password"] = password or None
            save_connection_overrides(mongodb={}, mssql=payload)
            _mark_saved("SQL Server")
            st.rerun()


def render(settings: Settings) -> None:
    theme.page_header(
        "Yapilandirma",
        "Baglantilar",
        "Kaynak ve hedef ayri tutulur: Mongo olmadan hicbir sey calismaz, SQL yalnizca "
        "veri yazarken devreye girer. Kaydet, degerleri asagidaki dosyaya yazar.",
        step="connections",
    )
    st.caption(f"Kayit dosyasi · `{LOCAL_CONFIG_PATH}`  ·  git'e eklenmez")
    _show_saved()
    _mongo_card(settings)
    st.write("")
    _sql_card(settings)
    if settings.mongo_ready:
        st.write("")
        theme.page_cta(
            "discovery",
            "Sema kesfine gec",
            ":material/schema:",
            "cta_to_discovery",
        )
