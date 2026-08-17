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
        theme.card_title(
            "MongoDB · kaynak",
            "Sema kesfi ve aktarim icin okunan veritabani. Zorunlu.",
        )
        with st.form("mongo_conn"):
            left, right = st.columns(2)
            with left:
                uri = st.text_input("Baglanti URI", value=settings.mongo.get("uri") or "")
                database = st.text_input("Database", value=settings.mongo.get("database") or "")
            with right:
                user = st.text_input("Kullanici", value=settings.mongo.get("username") or "")
                password = st.text_input(
                    "Sifre",
                    type="password",
                    placeholder="kayitli — degistirmek icin yazin"
                    if settings.mongo_password
                    else "bos",
                    help="Bos birakilirsa kayitli sifre korunur.",
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
            st.success(f"Kaydedildi · {LOCAL_CONFIG_PATH.name}")
            invalidate_collections()
            st.rerun()


def _sql_card(settings: Settings) -> None:
    with st.container(border=True):
        theme.card_title(
            "SQL Server · hedef",
            "Yalnizca veri aktarirken gerekir. Sema kesfi bu baglantiyi kullanmaz.",
        )
        with st.form("sql_conn"):
            left, right = st.columns(2)
            with left:
                st.markdown("**Yazilacak yer**")
                server = st.text_input("Sunucu", value=settings.mssql.get("server") or "")
                database = st.text_input("Database", value=settings.mssql.get("database") or "")
                schema = st.text_input("Sema", value=settings.mssql.get("schema") or "dbo")
            with right:
                st.markdown("**Kimlik dogrulama**")
                drivers = available_drivers()
                current = settings.mssql.get("driver") or drivers[0]
                driver = st.selectbox(
                    "ODBC surucu",
                    drivers,
                    index=drivers.index(current) if current in drivers else 0,
                )
                trusted_default = bool(settings.mssql.get("trusted_connection", True))
                auth = st.radio(
                    "Yontem",
                    ("Windows", "SQL Server"),
                    horizontal=True,
                    index=0 if trusted_default else 1,
                )
                user = st.text_input("Kullanici", value=settings.mssql.get("username") or "")
                password = st.text_input(
                    "Sifre",
                    type="password",
                    placeholder="kayitli — degistirmek icin yazin"
                    if settings.mssql_password
                    else "bos",
                )
            actions = st.columns([1, 1, 2])
            with actions[0]:
                do_test = st.form_submit_button(
                    "Test connection", key="sql_test", width="stretch"
                )
            with actions[1]:
                do_save = st.form_submit_button(
                    "Kaydet", type="primary", width="stretch"
                )

        trusted = auth == "Windows"
        if do_test:
            _test_sql(
                sql_target(
                    {
                        "server": server,
                        "database": database,
                        "schema": schema,
                        "driver": driver,
                        "trusted_connection": trusted,
                        "username": user,
                    },
                    password or settings.mssql_password,
                )
            )
        if do_save:
            save_connection_overrides(
                mongodb={},
                mssql={
                    "server": server,
                    "database": database,
                    "schema": schema,
                    "driver": driver,
                    "trusted_connection": trusted,
                    "username": user,
                    "password": password or None,
                },
            )
            st.success(f"Kaydedildi · {LOCAL_CONFIG_PATH.name}")
            st.rerun()


def render(settings: Settings) -> None:
    theme.page_header(
        "Yapilandirma",
        "Baglantilar",
        "Kaynak ve hedef ayri tutulur: Mongo olmadan hicbir sey calismaz, SQL yalnizca "
        "veri yazarken devreye girer. Sifreler config.local.yaml icinde saklanir.",
    )
    _mongo_card(settings)
    st.write("")
    _sql_card(settings)
