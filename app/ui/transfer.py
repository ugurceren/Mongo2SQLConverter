"""Transfer page: load a selected collection into SQL Server tables."""

from __future__ import annotations

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    collection_list,
    mongo_client,
    profile_one,
    sql_target,
)
from core.inspect import render_database_ddl, sql_ident
from core.transfer import (
    ensure_tables,
    plan_tables,
    relax_nullability,
    retarget_plan,
    transfer_collection,
)

PLAN_KEY = "transfer_plan"


def _target_card(settings: Settings, collections: list[str]) -> dict:
    with st.container(border=True):
        theme.card_title(
            "Hedef",
            "Tablolar secilen collection'in profilinden uretilir; sabit sema yoktur.",
        )
        row = st.columns([2.2, 1.6, 1.6], vertical_alignment="bottom")
        with row[0]:
            if collections:
                collection = st.selectbox(
                    "Kaynak collection", options=collections, key="tr_collection"
                )
            else:
                collection = None
                st.selectbox("Kaynak collection", options=["Collection yok"], disabled=True)
        with row[1]:
            schema = st.text_input("Hedef sema", value=settings.schema, key="tr_schema")
        with row[2]:
            table = st.text_input(
                "Kok tablo",
                value=sql_ident(collection) if collection else "",
                key=f"tr_table_{collection or 'none'}",
                help="Child tablolar bu addan turer: <ad>_<alan>.",
            )

        row2 = st.columns([1.2, 1.2, 2.6], vertical_alignment="bottom")
        with row2[0]:
            sample = st.number_input(
                "Ornek", min_value=0, value=0, step=500, key="tr_sample",
                help="Profilleme icin. 0 = tam tarama; yazma her zaman tum dokumanlari kapsar.",
            )
        with row2[1]:
            batch = st.number_input("Batch", min_value=50, value=500, step=50, key="tr_batch")
        with row2[2]:
            recreate = st.checkbox("Tablolari yeniden olustur (DROP + CREATE)", key="tr_recreate")
            clear_first = st.checkbox("Yazmadan once tablolari bosalt", key="tr_clear")
            allow_null = st.checkbox(
                "Anahtar disindaki kolonlar NULL kabul etsin",
                value=True,
                key="tr_null",
                help="Ornekle profillenen alanlar baska dokumanlarda eksik olabilir.",
            )

    return {
        "collection": collection,
        "schema": schema.strip() or settings.schema,
        "table": table.strip(),
        "sample": int(sample),
        "batch": int(batch),
        "recreate": recreate,
        "clear_first": clear_first,
        "allow_null": allow_null,
    }


def _build_plan(settings: Settings, options: dict) -> dict:
    plan = profile_one(settings, options["collection"], options["sample"], options["schema"])
    plan = retarget_plan(plan, schema=options["schema"], root_table=options["table"])
    return relax_nullability(plan) if options["allow_null"] else plan


def _run(settings: Settings, options: dict, create: bool, write: bool) -> None:
    plan = _build_plan(settings, options)
    st.session_state[PLAN_KEY] = plan
    tables = plan_tables(plan)

    with st.container(border=True):
        theme.card_title(
            "Plan",
            f"<code>{settings.mssql.get('database')}</code> · "
            f"<code>{options['schema']}</code> — {len(tables)} tablo",
        )
        st.code("\n".join(tables), language="text")

    if not (create or write):
        return

    target = sql_target(settings.mssql, settings.mssql_password, options["schema"])
    try:
        target.connect()
        created, existing = ensure_tables(target, plan, recreate=options["recreate"])
        if created:
            st.success("Olusturulan tablolar: " + ", ".join(created))
        if existing:
            st.caption("Zaten mevcut: " + ", ".join(existing))
        if not write:
            return

        status = st.empty()
        bar = st.progress(0)
        expected = options["sample"]

        def on_progress(done: int, total: int) -> None:
            status.caption(f"{done} dokuman islendi")
            if expected:
                bar.progress(min(done / expected, 1.0))

        source = mongo_client(settings.mongo)
        try:
            source.connect()
            stats = transfer_collection(
                source,
                target,
                plan,
                options["collection"],
                sample=expected,
                batch_size=options["batch"],
                clear_first=options["clear_first"],
                progress=on_progress,
            )
        finally:
            source.close()
        status.empty()
        bar.empty()

        with st.container(border=True):
            theme.card_title("Sonuc", f"{options['collection']} → {options['schema']}")
            cols = st.columns(4)
            cols[0].metric("Dokuman", stats.documents)
            cols[1].metric("Satir", stats.total_rows)
            cols[2].metric("Atlanan", stats.skipped_no_id)
            cols[3].metric("Kirpilan", stats.truncated)
            st.dataframe(
                [{"tablo": table, "satir": count} for table, count in stats.rows.items()],
                hide_index=True,
                width="stretch",
            )
        if stats.skipped_no_id:
            st.caption(f"{stats.skipped_no_id} dokumanda `_id` yok, birincil anahtar uretilemedi.")
        if stats.truncated:
            st.warning(
                f"{stats.truncated} deger kolon genisligine kirpildi. Tam tarama ile "
                "profilleyip tablolari yeniden olusturmak bunu giderir."
            )
    finally:
        target.close()


def render(settings: Settings) -> None:
    theme.page_header(
        "Adim 2",
        "SQL aktarimi",
        "Secilen collection profillenir, tablolar plandan uretilir ve dokumanlar "
        "batch batch yazilir. Ayni collection tekrar yazildiginda satirlar cogalmaz.",
    )

    collections, error, warning = collection_list(settings)
    if error:
        st.error(error)
    elif warning:
        st.warning(warning)

    blockers = []
    if not settings.mongo_ready:
        blockers.append("Mongo baglantisi")
    if not settings.sql_ready:
        blockers.append("SQL baglantisi")
    if blockers:
        st.warning(
            " ve ".join(blockers) + " eksik. **Baglantilar** sayfasindan kaydedin."
        )

    options = _target_card(settings, collections)
    ready = bool(
        settings.mongo_ready and settings.sql_ready and options["collection"] and options["table"]
    )

    st.write("")
    actions = st.columns([1.3, 1.3, 1.3, 2.1])
    with actions[0]:
        do_plan = st.button(
            "Plani hazirla", disabled=not options["collection"], width="stretch"
        )
    with actions[1]:
        do_create = st.button("Tablolari olustur", disabled=not ready, width="stretch")
    with actions[2]:
        do_write = st.button(
            "Veriyi yaz", type="primary", disabled=not ready, width="stretch"
        )

    if do_plan or do_create or do_write:
        st.write("")
        try:
            _run(settings, options, create=do_create or do_write, write=do_write)
        except Exception as exc:
            st.error(str(exc))

    if PLAN_KEY in st.session_state:
        with st.expander("Uretilen DDL", expanded=False):
            st.code(render_database_ddl([st.session_state[PLAN_KEY]]), language="sql")
