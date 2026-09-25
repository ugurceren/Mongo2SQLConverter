"""Schema discovery page: profile Mongo and export DRDL / DDL. No SQL needed."""

from __future__ import annotations

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    apply_remembered_collection,
    collection_count_caption,
    collection_list,
    format_int,
    invalidate_collections,
    nesting_card,
    profile_many,
    remember_collection,
    nesting_widget_key,
)
from core.inspect import (
    NESTING_HYBRID,
    render_database_ddl,
    render_database_drdl,
    sql_table_ident,
)
from core.settings import load_transfer_prefs
from core.transfer import apply_table_names, retarget_plan

RESULT_KEYS = (
    "drdl",
    "ddl",
    "plan",
    "result_name",
    "result_scope",
    "result_summary",
    "result_skipped",
)


def _clear_results() -> None:
    for key in RESULT_KEYS:
        st.session_state.pop(key, None)


def _named_plan(settings: Settings, plan: dict, collection: str) -> dict:
    prefs = load_transfer_prefs(collection)
    if prefs.get("table"):
        plan = retarget_plan(plan, schema=settings.schema, root_table=prefs["table"])
    return apply_table_names(plan, prefs.get("table_names") or {})


def _overview(settings: Settings, collections: list[str]) -> bool:
    with theme.collapsible_card(
        "disc_db",
        "Kaynak veritabanı",
        "Kayıtlı Mongo bağlantısından okunur.",
    ):
        cols = st.columns([1.4, 1, 1])
        cols[0].metric("Veritabanı", settings.mongo.get("database") or "—")
        cols[1].metric("Koleksiyon", len(collections))
        cols[2].metric(
            "Şema hedefi",
            settings.schema,
            help="Şema yalnız **Bağlantılar** sayfasında değişir.",
        )
        actions = st.columns([1.3, 1, 1.7])
        with actions[0]:
            run_database = st.button(
                "Tüm veritabanı için DRDL",
                icon=":material/database:",
                disabled=not collections,
                width="stretch",
                key="disc_database_drdl",
                help=(
                    "Tüm koleksiyonları tarar. Aşağıda kırılım seçtiyseniz onu kullanır; "
                    "yoksa hibrit."
                ),
            )
        with actions[1]:
            st.button(
                "Listeyi yenile",
                icon=":material/refresh:",
                key="disc_refresh",
                on_click=invalidate_collections,
                width="stretch",
            )
    return run_database


def _pick_collection(settings: Settings, collections: list[str]) -> tuple[str | None, int]:
    with theme.collapsible_card(
        "disc_pick",
        "Koleksiyon seç",
        "Önce kaynağı seçin. Kırılım seçenekleri bu koleksiyona göre sonra sorulur.",
    ):
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
                step=1000,
                key="disc_sample",
                help=(
                    "Şema için kaç belge taransın. 5000 önerilir: hızlı ve tipik şekil için yeterli. "
                    "0 = koleksiyonun tamamı; uzunluklar kesin olur, büyük koleksiyonda yavaştır. "
                    "Örneklemede max uzunluklar alt sınırdır; görünmeyen daha uzun değerler kesilebilir."
                ),
            )
        collection_count_caption(settings, collection, int(sample))
    remember_collection(collection)
    return collection, int(sample)


def render(settings: Settings) -> None:
    theme.page_header(
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
        theme.error_with_detail(error.summary, error.detail)
    elif warning:
        st.warning(warning)

    st.write("")
    collection, sample = _pick_collection(settings, collections)

    nesting = None
    run_collection = False
    if collection:
        st.write("")
        # The pick is saved; SQL aktarımı shows it read-only.
        root = load_transfer_prefs(collection).get("table") or sql_table_ident(collection)
        nesting = nesting_card(settings, collection, root)
        st.write("")
        run_collection = st.button("Şemayı çıkar", type="primary", width="stretch")

    nest = nesting or (
        st.session_state.get(nesting_widget_key(collection)) if collection else None
    ) or NESTING_HYBRID
    if (run_collection or run_database) and nest:
        whole_db = bool(run_database)
        targets = collections if whole_db else [collection]
        st.write("")
        plans, skipped, errors, total_docs = profile_many(
            settings, targets, sample, settings.schema, nesting=nest
        )
        if plans:
            database = settings.mongo.get("database") or "database"
            st.session_state["result_name"] = database if whole_db else collection
            st.session_state["result_scope"] = "database" if whole_db else "collection"
            if whole_db:
                st.session_state.pop("ddl", None)
                st.session_state.pop("plan", None)
                st.session_state["drdl"] = render_database_drdl(plans, database)
            else:
                plan = plans[0]
                named = _named_plan(settings, plan, collection)
                st.session_state["plan"] = plan
                st.session_state["ddl"] = render_database_ddl([named])
                st.session_state["drdl"] = render_database_drdl([named], database)
            # Kept for later reruns, shown under the next-step bar below.
            st.session_state["result_summary"] = (
                f"{len(plans)} koleksiyon · {format_int(total_docs)} belge profillendi"
            )
            st.session_state["result_skipped"] = list(skipped)
        else:
            _clear_results()
            st.warning("Profillenecek belge bulunamadı.")
            if skipped:
                st.caption("Boş olduğu için atlandı: " + ", ".join(skipped))
        for item in errors:
            st.error(item)

    if "drdl" not in st.session_state:
        return

    # The next step first, right under the button: no scrolling past the output to find it.
    st.write("")
    theme.next_step(
        "transfer",
        "Çıkarılan şemayı SQL Server'a yazın: tablolar plana göre oluşur, "
        "belgeler tam ya da artımlı olarak aktarılır.",
    )
    summary = st.session_state.get("result_summary")
    if summary:
        st.info(summary, icon=":material/info:")
    if st.session_state.get("result_skipped"):
        st.caption("Boş olduğu için atlandı: " + ", ".join(st.session_state["result_skipped"]))

    name = st.session_state.get("result_name") or "schema"
    st.write("")
    scope = st.session_state.get("result_scope")
    with theme.collapsible_card(
        "disc_out",
        "Çıktı",
        f"`{name}` · {'tüm veritabanı (DRDL)' if scope == 'database' else 'tek koleksiyon'}",
    ):
        if scope == "collection" and "plan" in st.session_state:
            named = _named_plan(settings, st.session_state["plan"], name)
            ddl = render_database_ddl([named])
            database = settings.mongo.get("database") or "database"
            drdl = render_database_drdl([named], database)
            st.caption(
                "Tablo adlarını **SQL aktarımı** sayfasındaki **Tablo adları** kartından "
                "değiştirip kaydedin; bu çıktı kaydedilen adları kullanır."
            )
            ddl_tab, drdl_tab, plan_tab = st.tabs(["MSSQL DDL", "DRDL", "Plan (JSON)"])
            with ddl_tab:
                st.download_button(
                    "DDL indir",
                    ddl,
                    f"{name}.sql",
                    key="disc_ddl_download",
                )
                st.code(ddl, language="sql")
            with drdl_tab:
                st.download_button(
                    "DRDL indir",
                    drdl,
                    f"{name}.drdl",
                    key="disc_drdl_download",
                )
                st.code(drdl, language="yaml")
            with plan_tab:
                st.json(named, expanded=False)
        else:
            st.download_button("DRDL indir", st.session_state["drdl"], f"{name}.drdl")
            st.code(st.session_state["drdl"], language="yaml")
