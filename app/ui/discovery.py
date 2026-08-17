"""Schema discovery page: profile Mongo and export DRDL / DDL. No SQL needed."""

from __future__ import annotations

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    collection_list,
    invalidate_collections,
    profile_many,
)
from core.inspect import render_database_ddl, render_database_drdl

RESULT_KEYS = ("drdl", "ddl", "plan", "result_name", "result_scope")


def _clear_results() -> None:
    for key in RESULT_KEYS:
        st.session_state.pop(key, None)


def _overview(settings: Settings, collections: list[str]) -> None:
    with st.container(border=True):
        theme.card_title("Kaynak database", "Kayitli Mongo baglantisindan okunur.")
        cols = st.columns([1.4, 1, 1, 1.1], vertical_alignment="bottom")
        cols[0].metric("Database", settings.mongo.get("database") or "—")
        cols[1].metric("Collection", len(collections))
        cols[2].metric("Sema hedefi", settings.schema)
        with cols[3]:
            st.button(
                "Listeyi yenile",
                key="disc_refresh",
                on_click=invalidate_collections,
                width="stretch",
            )
        st.caption(settings.mongo.get("uri") or "Baglanti kayitli degil.")


def render(settings: Settings) -> None:
    theme.page_header(
        "Adim 1",
        "Sema kesfi",
        "Collection'lari olcerek DRDL ve MSSQL sema onerisi uretir. Dokuman okur, "
        "hicbir yere yazmaz; SQL baglantisi gerekmez.",
    )

    if not settings.mongo_ready:
        st.warning("Mongo baglantisi kayitli degil. **Baglantilar** sayfasindan kaydedin.")

    collections, error, warning = collection_list(settings)
    _overview(settings, collections)

    if error:
        st.error(error)
    elif warning:
        st.warning(warning)

    st.write("")
    with st.container(border=True):
        theme.card_title("Cikti uret", "Tek collection tam sema verir; database geneli yalnizca DRDL.")
        row = st.columns([2.6, 1.1, 1.3, 1.3], vertical_alignment="bottom")
        with row[0]:
            if collections:
                default = "conversations" if "conversations" in collections else collections[0]
                collection = st.selectbox(
                    "Collection",
                    options=collections,
                    index=collections.index(default),
                    key="disc_collection",
                )
            else:
                collection = None
                st.selectbox("Collection", options=["Collection yok"], disabled=True)
        with row[1]:
            sample = st.number_input(
                "Ornek",
                min_value=0,
                value=0,
                step=500,
                key="disc_sample",
                help="0 = tam tarama. Ornekleme hizlidir ama uzunluklar alt sinir olur.",
            )
        with row[2]:
            run_collection = st.button(
                "Semayi cikar", type="primary", disabled=not collection, width="stretch"
            )
        with row[3]:
            run_database = st.button(
                "Database DRDL", disabled=not collections, width="stretch"
            )

    if run_collection or run_database:
        whole_db = bool(run_database)
        targets = collections if whole_db else [collection]
        st.write("")
        plans, skipped, errors, total_docs = profile_many(
            settings, targets, int(sample), settings.schema
        )
        if plans:
            database = settings.mongo.get("database") or "database"
            st.session_state["drdl"] = render_database_drdl(plans, database)
            st.session_state["result_name"] = database if whole_db else collection
            st.session_state["result_scope"] = "database" if whole_db else "collection"
            if whole_db:
                st.session_state.pop("ddl", None)
                st.session_state.pop("plan", None)
            else:
                st.session_state["ddl"] = render_database_ddl(plans)
                st.session_state["plan"] = plans[0]
            st.success(f"{len(plans)} collection · {total_docs} dokuman profillendi")
        else:
            _clear_results()
            st.warning("Profillenecek dokuman bulunamadi.")
        if skipped:
            st.caption("Bos oldugu icin atlandi: " + ", ".join(skipped))
        for item in errors:
            st.error(item)

    if "drdl" not in st.session_state:
        return

    name = st.session_state.get("result_name") or "schema"
    st.write("")
    with st.container(border=True):
        scope = st.session_state.get("result_scope")
        theme.card_title(
            "Cikti",
            f"<code>{name}</code> · "
            f"{'tum database (DRDL)' if scope == 'database' else 'tek collection'}",
        )
        if scope == "collection" and "ddl" in st.session_state and "plan" in st.session_state:
            drdl_tab, ddl_tab, plan_tab = st.tabs(["DRDL", "MSSQL DDL", "Plan"])
            with drdl_tab:
                st.download_button("DRDL indir", st.session_state["drdl"], f"{name}.drdl")
                st.code(st.session_state["drdl"], language="yaml")
            with ddl_tab:
                st.download_button("DDL indir", st.session_state["ddl"], f"{name}.sql")
                st.code(st.session_state["ddl"], language="sql")
            with plan_tab:
                st.json(st.session_state["plan"], expanded=False)
        else:
            st.download_button("DRDL indir", st.session_state["drdl"], f"{name}.drdl")
            st.code(st.session_state["drdl"], language="yaml")
