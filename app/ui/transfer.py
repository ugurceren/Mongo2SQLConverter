"""Transfer page: full or incremental load of a collection into SQL Server."""

from __future__ import annotations

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    apply_remembered_collection,
    collection_count_caption,
    collection_list,
    invalidate_sql_watermarks,
    mongo_client,
    nesting_card,
    profile_one,
    remember_collection,
    sql_table_watermark,
    sql_target,
)
from core.inspect import nesting_labels, render_database_ddl, sql_ident
from core.mongo import decode_mongo_id, id_after_filter
from core.settings import load_sync_watermark, save_sync_watermark
from core.transfer import (
    ensure_tables,
    plan_tables,
    read_root_watermark,
    relax_nullability,
    retarget_plan,
    transfer_collection,
)

PLAN_KEY = "transfer_plan"


def _defaults(settings: Settings) -> dict:
    return {
        "collection": None,
        "schema": settings.schema,
        "table": "",
        "sample": 0,
        "batch": 500,
        "recreate": False,
        "clear_first": False,
        "allow_null": True,
        "mode": "full",
        "watermark": None,
        "nesting": "hybrid",
    }


def _target_card(settings: Settings, collections: list[str]) -> dict:
    options = _defaults(settings)
    with st.container(border=True):
        theme.card_title(
            "Hedef",
            "Önce koleksiyon ve kök tabloyu seçin. Kırılım sonra sorulur.",
        )
        row = st.columns([2.2, 1.6, 1.6], vertical_alignment="bottom")
        with row[0]:
            if collections:
                apply_remembered_collection("tr_collection", collections)
                empty = "tr_collection" not in st.session_state
                collection = st.selectbox(
                    "Kaynak koleksiyon",
                    options=collections,
                    placeholder="Koleksiyon seçin...",
                    key="tr_collection",
                    **({"index": None} if empty else {}),
                )
            else:
                collection = None
                st.selectbox("Kaynak koleksiyon", options=["Koleksiyon yok"], disabled=True)
        remember_collection(collection)
        with row[1]:
            schema = st.text_input("Hedef şema", value=settings.schema, key="tr_schema")
        with row[2]:
            table = st.text_input(
                "Kök tablo",
                value=sql_ident(collection) if collection else "",
                key=f"tr_table_{collection or 'none'}",
                disabled=not collection,
                help="Alt tablolar bu addan türer: <ad>_<alan>.",
            )
        if not collection:
            st.caption("Koleksiyon seçildikten sonra iç içe yapı sorulur.")
        else:
            collection_count_caption(settings, collection)

    options["collection"] = collection
    options["schema"] = (schema or "").strip() or settings.schema
    options["table"] = (table or "").strip()
    if not collection:
        return options

    st.write("")
    options["nesting"] = nesting_card(
        settings, collection, options["table"] or sql_ident(collection)
    )

    st.write("")
    with st.container(border=True):
        theme.card_title("Senkron", "Yazma şekli ve profil ayarları.")
        mode = st.radio(
            "Senkron",
            ("Tam senkron", "Artımlı"),
            horizontal=True,
            key="tr_sync_kind",
            help="Tam: tüm belgeler. Artımlı: SQL tablosundaki son `_id` sonrası yeni kayıtlar.",
        )
        incremental = mode == "Artımlı"
        watermark = None
        watermark_source = None
        if incremental:
            sql_status, sql_mark = sql_table_watermark(
                settings, options["schema"], options["table"]
            )
            if sql_status == "sql":
                watermark = sql_mark
                watermark_source = "sql" if sql_mark else None
            else:
                watermark = load_sync_watermark(collection)
                watermark_source = "file" if watermark else None

            if watermark:
                where = "SQL tablosu" if watermark_source == "sql" else "kayıtlı işaret"
                st.caption(
                    f"Son işaret · {where} · `_id` > `{watermark['last_id']}`"
                    + (f" · {watermark['updated']}" if watermark.get("updated") else "")
                )
            else:
                st.warning(
                    "Bu koleksiyon için SQL tablosunda kayıt yok. Önce **Tam senkron** çalıştırın, "
                    "ya da artımlı ilk seferde tüm belgeleri okur."
                )
        else:
            st.caption(
                "Tam senkron tüm belgeleri yazar. Silinen Mongo kayıtları SQL'den de düşsün "
                "istiyorsanız tabloları boşaltın veya yeniden oluşturun."
            )

        row2 = st.columns([1.2, 1.2, 2.6], vertical_alignment="bottom")
        with row2[0]:
            sample = st.number_input(
                "Profil örneği",
                min_value=0,
                value=5000,
                step=500,
                key="tr_sample",
                help=(
                    "Şema için kaç belge taransın. 5000 önerilir. "
                    "0 = koleksiyonun tamamı. Aktarılacak belge sayısını değiştirmez."
                ),
            )
        with row2[1]:
            batch = st.number_input(
                "Yazma partisi",
                min_value=50,
                value=500,
                step=50,
                key="tr_batch",
                help=(
                    "SQL'e bir seferde kaç belgelik paket yazılsın. "
                    "Hızı ve belleği etkiler; aktarılacak belge sayısını değiştirmez."
                ),
            )
        with row2[2]:
            recreate = st.checkbox(
                "Tabloları yeniden oluştur (DROP + CREATE)",
                key="tr_recreate",
                disabled=incremental,
                help="Artımlı modda şema durur; yalnızca yeni satırlar yazılır.",
            )
            clear_first = st.checkbox(
                "Yazmadan önce tabloları boşalt",
                key="tr_clear",
                disabled=incremental,
                help="Hedef tablolardaki mevcut satırlar silinir, sonra aktarım başlar.",
            )
            allow_null = st.checkbox(
                "Anahtar dışındaki kolonlar NULL kabul etsin",
                value=True,
                key="tr_null",
                help="Örnekle profillenen alanlar başka belgelerde eksik olabilir.",
            )
        st.caption(
            "Profil örneği yalnız kolon tipi ve genişliği içindir; **5000 önerilir**. "
            "**0 = tam tarama** (kesin genişlik). "
            "Yazma partisi SQL'e kaç belgelik paket halinde yazılacağını ayarlar. "
            "İkisi de kaç belgenin aktarılacağını değiştirmez; onu senkron seçimi belirler."
        )

    options.update(
        {
            "sample": int(sample),
            "batch": int(batch),
            "recreate": False if incremental else recreate,
            "clear_first": False if incremental else clear_first,
            "allow_null": allow_null,
            "mode": "incremental" if incremental else "full",
            "watermark": watermark,
        }
    )
    return options


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
    mode_label = "Artımlı" if options["mode"] == "incremental" else "Tam senkron"

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
        invalidate_sql_watermarks()
        if created:
            st.success("Oluşturulan tablolar: " + ", ".join(created))
        if existing:
            st.caption("Zaten mevcut: " + ", ".join(existing))
        if not write:
            return

        if options["mode"] == "incremental":
            exists, sql_mark = read_root_watermark(
                target, options["schema"], plan["root"]["table"]
            )
            if exists:
                options["watermark"] = sql_mark

        query = _write_query(options)
        status = st.empty()
        bar = st.progress(0)

        def on_progress(done: int, total: int) -> None:
            status.caption(f"{done} belge işlendi")
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
            invalidate_sql_watermarks()

        with st.container(border=True):
            theme.card_title("Sonuç", f"{options['collection']} → {options['schema']}")
            cols = st.columns(5)
            cols[0].metric("Mod", mode_label)
            cols[1].metric("Belge", stats.documents)
            cols[2].metric("Satır", stats.total_rows)
            cols[3].metric("Atlanan", stats.skipped_no_id)
            cols[4].metric("Kırpılan", stats.truncated)
            if stats.last_id:
                st.caption(f"İşaret `_id` = `{stats.last_id}`")
            st.dataframe(
                [{"tablo": table, "satır": count} for table, count in stats.rows.items()],
                hide_index=True,
                width="stretch",
            )
        if stats.documents == 0 and options["mode"] == "incremental":
            st.info("Yeni belge yok; işaret zaten güncel.")
        if stats.skipped_no_id:
            st.caption(f"{stats.skipped_no_id} belgede `_id` yok, birincil anahtar üretilemedi.")
        if stats.truncated:
            st.warning(
                f"{stats.truncated} değer kolon genişliğine kırpıldı. Tam senkron ile "
                "tam tarama yapıp tabloları yeniden oluşturmak bunu giderir."
            )
    finally:
        target.close()


def render(settings: Settings) -> None:
    theme.page_header(
        "Aktarım",
        "SQL aktarımı",
        "Tam senkron tüm koleksiyonu yazar. Artımlı, SQL tablosundaki son `_id` "
        "sonrası yeni belgeleri ekler; eski kayıtlardaki güncelleme için tam senkron gerekir.",
        step="transfer",
    )

    collections, error, warning = collection_list(settings)
    if error:
        st.error(error)
    elif warning:
        st.warning(warning)

    blockers = []
    if not settings.mongo_ready:
        blockers.append("Mongo bağlantısı")
    if not settings.sql_ready:
        blockers.append("SQL bağlantısı")
    if blockers:
        theme.need_connections(blockers)

    options = _target_card(settings, collections)
    ready = bool(
        settings.mongo_ready and settings.sql_ready and options["collection"] and options["table"]
    )
    write_label = "Artımlı senkron" if options["mode"] == "incremental" else "Tam senkron"

    if options["collection"]:
        st.write("")
        actions = st.columns([1.3, 1.3, 1.3, 2.1])
        with actions[0]:
            do_plan = st.button("Planı hazırla", width="stretch")
        with actions[1]:
            do_create = st.button(
                "Tabloları oluştur",
                disabled=not ready or options["mode"] == "incremental",
                width="stretch",
            )
        with actions[2]:
            do_write = st.button(
                write_label, type="primary", disabled=not ready, width="stretch"
            )
    else:
        do_plan = do_create = do_write = False

    if do_plan or do_create or do_write:
        st.write("")
        try:
            _run(settings, options, create=do_create or do_write, write=do_write)
        except Exception as exc:
            st.error(str(exc))

    if PLAN_KEY in st.session_state:
        with st.expander("Üretilen DDL", expanded=False):
            st.code(render_database_ddl([st.session_state[PLAN_KEY]]), language="sql")
