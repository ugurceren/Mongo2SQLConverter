"""Transfer page: full or incremental load of a collection into SQL Server."""

from __future__ import annotations

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    collection_list,
    mongo_client,
    nesting_choice,
    profile_one,
    sql_target,
)
from core.inspect import nesting_labels, render_database_ddl, sql_ident
from core.mongo import decode_mongo_id, id_after_filter
from core.settings import load_sync_watermark, save_sync_watermark
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
            "Kirilim secenekleri secilen collection'un nested yapisina gore sunulur.",
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

        nesting = nesting_choice(settings, collection)

        mode = st.radio(
            "Senkron",
            ("Full sync", "Incremental"),
            horizontal=True,
            key="tr_mode",
            help="Full: tum dokumanlar. Incremental: son kaydedilen `_id` sonrasi yeni kayitlar.",
        )
        incremental = mode == "Incremental"
        watermark = load_sync_watermark(collection) if collection else None

        if incremental:
            if watermark:
                st.caption(
                    f"Son watermark · `_id` > `{watermark['last_id']}`"
                    + (f" · {watermark['updated']}" if watermark.get("updated") else "")
                )
            else:
                st.warning(
                    "Bu collection icin watermark yok. Once **Full sync** calistirin, "
                    "ya da incremental ilk seferde tum dokumanlari okur."
                )
        else:
            st.caption(
                "Full sync tum dokumanlari yazar. Silinen Mongo kayitlari SQL'den de dussun "
                "istiyorsaniz tablolari bosaltin veya yeniden olusturun."
            )

        row2 = st.columns([1.2, 1.2, 2.6], vertical_alignment="bottom")
        with row2[0]:
            sample = st.number_input(
                "Profil ornegi",
                min_value=0,
                value=0,
                step=500,
                key="tr_sample",
                help="Yalnizca sema profili. 0 = tam tarama. Yazma bu degeri kullanmaz.",
            )
        with row2[1]:
            batch = st.number_input("Batch", min_value=50, value=500, step=50, key="tr_batch")
        with row2[2]:
            recreate = st.checkbox(
                "Tablolari yeniden olustur (DROP + CREATE)",
                key="tr_recreate",
                disabled=incremental,
                help="Incremental modda sema durur; yalnizca yeni satirlar yazilir.",
            )
            clear_first = st.checkbox(
                "Yazmadan once tablolari bosalt",
                key="tr_clear",
                disabled=incremental,
            )
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
        "recreate": False if incremental else recreate,
        "clear_first": False if incremental else clear_first,
        "allow_null": allow_null,
        "mode": "incremental" if incremental else "full",
        "watermark": watermark,
        "nesting": nesting,
    }


def _build_plan(settings: Settings, options: dict) -> dict:
    plan = profile_one(
        settings,
        options["collection"],
        options["sample"],
        options["schema"],
        nesting=options["nesting"],
    )
    plan = retarget_plan(plan, schema=options["schema"], root_table=options["table"])
    return relax_nullability(plan) if options["allow_null"] else plan


def _write_query(options: dict) -> dict | None:
    if options["mode"] != "incremental":
        return None
    mark = options.get("watermark")
    if not mark:
        return None
    try:
        last_id = decode_mongo_id(mark["last_id"], mark["last_id_type"])
    except Exception:
        return None
    return id_after_filter(last_id)


def _run(settings: Settings, options: dict, create: bool, write: bool) -> None:
    plan = _build_plan(settings, options)
    st.session_state[PLAN_KEY] = plan
    tables = plan_tables(plan)
    mode_label = "Incremental" if options["mode"] == "incremental" else "Full sync"

    nesting_title = nesting_labels().get(plan.get("nesting") or "", plan.get("nesting") or "")
    with st.container(border=True):
        theme.card_title(
            "Plan",
            f"<code>{settings.mssql.get('database')}</code> · "
            f"<code>{options['schema']}</code> — {len(tables)} tablo · {mode_label} · {nesting_title}",
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

        query = _write_query(options)
        status = st.empty()
        bar = st.progress(0)

        def on_progress(done: int, total: int) -> None:
            status.caption(f"{done} dokuman islendi")
            if total:
                bar.progress(min(done / total, 1.0))

        source = mongo_client(settings.mongo)
        try:
            source.connect()
            expected = source.estimated_count(options["collection"], query)
            stats = transfer_collection(
                source,
                target,
                plan,
                options["collection"],
                sample=0,
                batch_size=options["batch"],
                clear_first=options["clear_first"],
                query=query,
                mode=options["mode"],
                progress=lambda done, _: on_progress(done, expected),
            )
        finally:
            source.close()
        status.empty()
        bar.empty()

        if stats.last_id and stats.last_id_type:
            save_sync_watermark(options["collection"], stats.last_id, stats.last_id_type)

        with st.container(border=True):
            theme.card_title("Sonuc", f"{options['collection']} → {options['schema']}")
            cols = st.columns(5)
            cols[0].metric("Mod", mode_label)
            cols[1].metric("Dokuman", stats.documents)
            cols[2].metric("Satir", stats.total_rows)
            cols[3].metric("Atlanan", stats.skipped_no_id)
            cols[4].metric("Kirpilan", stats.truncated)
            if stats.last_id:
                st.caption(f"Watermark `_id` = `{stats.last_id}`")
            st.dataframe(
                [{"tablo": table, "satir": count} for table, count in stats.rows.items()],
                hide_index=True,
                width="stretch",
            )
        if stats.documents == 0 and options["mode"] == "incremental":
            st.info("Yeni dokuman yok; watermark zaten guncel.")
        if stats.skipped_no_id:
            st.caption(f"{stats.skipped_no_id} dokumanda `_id` yok, birincil anahtar uretilemedi.")
        if stats.truncated:
            st.warning(
                f"{stats.truncated} deger kolon genisligine kirpildi. Full sync ile "
                "tam tarama yapip tablolari yeniden olusturmak bunu giderir."
            )
    finally:
        target.close()


def render(settings: Settings) -> None:
    theme.page_header(
        "Adim 2",
        "SQL aktarimi",
        "Full sync tum collection'i yazar. Incremental, son `_id` watermark'indan "
        "sonraki yeni dokumanlari ekler; eski kayitlardaki guncelleme icin full gerekir.",
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
    write_label = "Artimli senkron" if options["mode"] == "incremental" else "Tam senkron"

    st.write("")
    actions = st.columns([1.3, 1.3, 1.3, 2.1])
    with actions[0]:
        do_plan = st.button(
            "Plani hazirla", disabled=not options["collection"], width="stretch"
        )
    with actions[1]:
        do_create = st.button(
            "Tablolari olustur",
            disabled=not ready or options["mode"] == "incremental",
            width="stretch",
        )
    with actions[2]:
        do_write = st.button(
            write_label, type="primary", disabled=not ready, width="stretch"
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
