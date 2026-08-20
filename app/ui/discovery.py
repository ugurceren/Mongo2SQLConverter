"""Schema discovery page: profile Mongo and export DRDL / DDL. No SQL needed."""

from __future__ import annotations

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    apply_remembered_collection,
    collection_count_caption,
    collection_list,
    invalidate_collections,
    nesting_card,
    profile_many,
    remember_collection,
    NESTING_KEY,
)
from core.inspect import (
    NESTING_HYBRID,
    render_database_ddl,
    render_database_drdl,
    sql_ident,
)

RESULT_KEYS = ("drdl", "ddl", "plan", "result_name", "result_scope")


def _clear_results() -> None:
    for key in RESULT_KEYS:
        st.session_state.pop(key, None)


def _overview(settings: Settings, collections: list[str]) -> bool:
    with st.container(border=True):
        head = st.columns([2.2, 1.45], vertical_alignment="center")
        with head[0]:
            theme.card_title(
                "Kaynak veritabanı",
                "Kayıtlı Mongo bağlantısından okunur.",
            )
        with head[1]:
            run_database = st.button(
                "Veritabanı DRDL",
                type="primary",
                icon=":material/database:",
                disabled=not collections,
                width="stretch",
                key="disc_database_drdl",
                help=(
                    "Tüm koleksiyonları tarar. Aşağıda kırılım seçtiyseniz onu kullanır; "
                    "yoksa hibrit."
                ),
            )
        cols = st.columns([1.4, 1, 1, 1.1], vertical_alignment="bottom")
        cols[0].metric("Veritabanı", settings.mongo.get("database") or "—")
        cols[1].metric("Koleksiyon", len(collections))
        cols[2].metric("Şema hedefi", settings.schema)
        with cols[3]:
            st.button(
                "Listeyi yenile",
                key="disc_refresh",
                on_click=invalidate_collections,
                width="stretch",
            )
    return run_database


def _pick_collection(settings: Settings, collections: list[str]) -> tuple[str | None, int]:
    with st.container(border=True):
        theme.card_title(
            "Koleksiyon seç",
            "Önce kaynağı seçin. Kırılım seçenekleri bu koleksiyona göre sonra sorulur.",
        )
        row = st.columns([2.6, 1.1], vertical_alignment="bottom")
        with row[0]:
            if collections:
                apply_remembered_collection("disc_collection", collections)
                empty = "disc_collection" not in st.session_state
                collection = st.selectbox(
                    "Koleksiyon",
                    options=collections,
                    placeholder="Koleksiyon seçin...",
                    key="disc_collection",
                    **({"index": None} if empty else {}),
                )
            else:
                collection = None
                st.selectbox("Koleksiyon", options=["Koleksiyon yok"], disabled=True)
        with row[1]:
            sample = st.number_input(
                "Örnek",
                min_value=0,
                value=5000,
                step=500,
                key="disc_sample",
                help=(
                    "Şema için kaç belge taransın. 5000 önerilir: hızlı ve tipik şekil için yeterli. "
                    "0 = koleksiyonun tamamı; uzunluklar kesin olur, büyük koleksiyonda yavaştır."
                ),
            )
        st.caption(
            "Örnek yalnız şema ölçümü içindir; **5000 önerilir**. "
            "**0 = tam tarama** (kesin genişlik, yavaş). "
            "Örneklemede max uzunluklar alt sınırdır — görünmeyen daha uzun değerler kesilebilir."
        )
        collection_count_caption(settings, collection, int(sample))
        if not collection:
            st.caption("Koleksiyon seçildikten sonra iç içe yapı sorulur.")
    remember_collection(collection)
    return collection, int(sample)


def render(settings: Settings) -> None:
    theme.page_header(
        "Keşif",
        "Şema keşfi",
        "Koleksiyonları ölçerek DRDL ve MSSQL şema önerisi üretir. Belge okur, "
        "hiçbir yere yazmaz; SQL bağlantısı gerekmez.",
        step="discovery",
    )

    if not settings.mongo_ready:
        theme.need_connections(["Mongo bağlantısı"])

    collections, error, warning = collection_list(settings)
    run_database = _overview(settings, collections)

    if error:
        st.error(error)
    elif warning:
        st.warning(warning)

    st.write("")
    collection, sample = _pick_collection(settings, collections)

    nesting = None
    run_collection = False
    if collection:
        st.write("")
        nesting = nesting_card(settings, collection, sql_ident(collection))
        st.write("")
        run_collection = st.button("Şemayı çıkar", type="primary", width="stretch")

    nest = nesting or st.session_state.get(NESTING_KEY) or NESTING_HYBRID
    if (run_collection or run_database) and nest:
        whole_db = bool(run_database)
        targets = collections if whole_db else [collection]
        st.write("")
        plans, skipped, errors, total_docs = profile_many(
            settings, targets, sample, settings.schema, nesting=nest
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
            st.success(f"{len(plans)} koleksiyon · {total_docs} belge profillendi")
        else:
            _clear_results()
            st.warning("Profillenecek belge bulunamadı.")
        if skipped:
            st.caption("Boş olduğu için atlandı: " + ", ".join(skipped))
        for item in errors:
            st.error(item)

    if "drdl" not in st.session_state:
        return

    name = st.session_state.get("result_name") or "schema"
    st.write("")
    with st.container(border=True):
        scope = st.session_state.get("result_scope")
        theme.card_title(
            "Çıktı",
            f"<code>{name}</code> · "
            f"{'tüm veritabanı (DRDL)' if scope == 'database' else 'tek koleksiyon'}",
        )
        theme.page_cta(
            "transfer",
            "SQL aktarımına geç",
            ":material/moving:",
            "cta_to_transfer",
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
