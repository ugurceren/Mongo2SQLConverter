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
    table_names_editor,
    nesting_widget_key,
)
from core.inspect import (
    NESTING_HYBRID,
    render_database_ddl,
    render_database_drdl,
    sql_table_ident,
)
from core.settings import load_transfer_prefs, save_transfer_prefs
from core.transfer import apply_table_names, retarget_plan

RESULT_KEYS = (
    "drdl",
    "ddl",
    "plan",
    "result_name",
    "result_scope",
    "disc_table_names",
)


def _clear_results() -> None:
    for key in RESULT_KEYS:
        st.session_state.pop(key, None)
    for key in [item for item in st.session_state if str(item).startswith("disc_table_names_")]:
        st.session_state.pop(key, None)
    for key in [item for item in st.session_state if str(item).startswith("disc_tables_nonce_")]:
        st.session_state.pop(key, None)


def _rename_collection_tables(settings: Settings, collection: str) -> None:
    plan = st.session_state.get("plan")
    if not isinstance(plan, dict):
        return
    overrides = {
        source: name
        for source, name in dict(st.session_state.get(f"disc_table_names_{collection}") or {}).items()
        if source
    }
    nonce_key = f"disc_tables_nonce_{collection}"
    nonce = int(st.session_state.get(nonce_key) or 0)
    sources = ",".join(child["source"] for child in plan["children"])
    st.caption("Tablo adları — SQL adı kolonunu düzenleyin. Boş bırakılan alt tablolar kök addan türemeye devam eder.")
    new_root, new_overrides, duplicates, needs_reset = table_names_editor(
        plan,
        overrides,
        editor_key=f"disc_tables_{collection}_{plan['root']['table']}_{sources}_{nonce}",
    )
    st.caption("Adlar PascalCase'e çevrilir. Aynı isim iki tabloda kullanılamaz.")
    if duplicates:
        st.warning(
            "Bu adlar birden fazla tabloda yazıldı, ikincisi yok sayıldı: "
            + ", ".join(duplicates)
        )
    root_changed = new_root != plan["root"]["table"]
    if root_changed:
        plan = retarget_plan(
            plan, schema=settings.schema, root_table=new_root
        )
        st.session_state["plan"] = plan
    st.session_state[f"disc_table_names_{collection}"] = new_overrides
    database = settings.mongo.get("database") or "database"
    named = apply_table_names(plan, new_overrides)
    st.session_state["ddl"] = render_database_ddl([named])
    st.session_state["drdl"] = render_database_drdl([named], database)
    prefs = load_transfer_prefs(collection)
    if prefs.get("table") != new_root or dict(prefs.get("table_names") or {}) != new_overrides:
        prefs["table"] = new_root
        prefs["table_names"] = new_overrides
        try:
            save_transfer_prefs(collection, prefs)
        except OSError as exc:
            st.caption(f"Tablo adları kaydedilemedi: {exc}")
    if root_changed or needs_reset:
        st.session_state[nonce_key] = nonce + 1
        st.rerun()


def _overview(settings: Settings, collections: list[str]) -> bool:
    with st.container(border=True):
        head = st.columns([3.4, 1.1], vertical_alignment="center")
        with head[0]:
            theme.card_title(
                "Kaynak veritabanı",
                "Kayıtlı Mongo bağlantısından okunur.",
            )
        with head[1]:
            run_database = st.button(
                "Veritabanı DRDL",
                type="primary",
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
        st.caption("Şema yalnız **Bağlantılar** sayfasında değişir.")
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
                step=1000,
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
        nesting = nesting_card(settings, collection, sql_table_ident(collection))
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
            st.session_state["drdl"] = render_database_drdl(plans, database)
            st.session_state["result_name"] = database if whole_db else collection
            st.session_state["result_scope"] = "database" if whole_db else "collection"
            if whole_db:
                st.session_state.pop("ddl", None)
                st.session_state.pop("plan", None)
                st.session_state.pop("disc_table_names", None)
                st.session_state.pop(f"disc_table_names_{collection}", None)
            else:
                plan = plans[0]
                prefs = load_transfer_prefs(collection)
                if prefs.get("table"):
                    plan = retarget_plan(
                        plan, schema=settings.schema, root_table=prefs["table"]
                    )
                names = dict(prefs.get("table_names") or {})
                st.session_state["plan"] = plan
                st.session_state[f"disc_table_names_{collection}"] = names
                named = apply_table_names(plan, names)
                st.session_state["ddl"] = render_database_ddl([named])
                st.session_state["drdl"] = render_database_drdl([named], database)
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
        if scope == "collection" and "ddl" in st.session_state and "plan" in st.session_state:
            _rename_collection_tables(settings, name)
            named = apply_table_names(
                st.session_state["plan"],
                st.session_state.get(f"disc_table_names_{name}") or {},
            )
            ddl_tab, drdl_tab, plan_tab = st.tabs(["MSSQL Plan", "DRDL", "Plan"])
            with ddl_tab:
                st.download_button(
                    "DDL indir",
                    st.session_state["ddl"],
                    f"{name}.sql",
                    key="disc_ddl_download",
                )
                st.code(st.session_state["ddl"], language="sql")
            with drdl_tab:
                st.download_button(
                    "DRDL indir",
                    st.session_state["drdl"],
                    f"{name}.drdl",
                    key="disc_drdl_download",
                )
                st.code(st.session_state["drdl"], language="yaml")
            with plan_tab:
                st.json(named, expanded=False)
        else:
            st.download_button("DRDL indir", st.session_state["drdl"], f"{name}.drdl")
            st.code(st.session_state["drdl"], language="yaml")

    theme.next_step(
        "transfer",
        "Çıkarılan şemayı SQL Server'a yazın: tablolar plana göre oluşur, "
        "belgeler tam ya da artımlı olarak aktarılır.",
    )
