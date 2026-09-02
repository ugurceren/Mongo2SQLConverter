"""Transfer page: full or incremental load of a collection into SQL Server."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import streamlit as st

from app.ui import theme
from app.ui.services import (
    Settings,
    apply_remembered_collection,
    cached_plan,
    collection_count_caption,
    collection_list,
    count_matching,
    date_field_options,
    field_has_index,
    format_int,
    invalidate_sql_watermarks,
    mongo_client,
    nesting_card,
    remember_collection,
    sql_table_watermark,
    sql_target,
    stored_plan,
)
from core.inspect import nesting_labels, render_database_ddl, sql_ident
from core.mongo import combine_filters, date_range_filter, decode_mongo_id, id_after_filter
from core.settings import (
    default_transfer_prefs,
    load_sync_watermark,
    load_transfer_prefs,
    save_sync_watermark,
    save_transfer_prefs,
)
from core.transfer import (
    apply_column_selection,
    ensure_tables,
    plan_column_rows,
    plan_tables,
    read_root_watermark,
    transfer_collection,
)

PLAN_KEY = "transfer_plan"
PREFS_KEY = "tr_prefs"
PREFS_STAMP = "tr_prefs_collection"
SAVED_PREFS_KEY = "tr_prefs_saved"
EXCLUDE_KEY = "tr_exclude"
EDITOR_NONCE = "tr_cols_nonce"


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
        "date_filter": default_transfer_prefs()["date_filter"],
        "columns": default_transfer_prefs()["columns"],
    }


# --------------------------------------------------------------------------
# saved preferences
# --------------------------------------------------------------------------


def _prefs(collection: str | None) -> dict:
    """Saved date range and column exclusions, read once per collection."""
    if not collection:
        return default_transfer_prefs()
    if st.session_state.get(PREFS_STAMP) != collection:
        prefs = load_transfer_prefs(collection)
        st.session_state[PREFS_KEY] = prefs
        st.session_state[PREFS_STAMP] = collection
        # Snapshot what is already on disk so simply opening a collection does
        # not rewrite the file.
        st.session_state[SAVED_PREFS_KEY] = repr(prefs)
        st.session_state.pop(EXCLUDE_KEY, None)
    return st.session_state[PREFS_KEY]


def _remember_prefs(collection: str | None, options: dict) -> None:
    """Write the current choices to config.local.yaml, but only when they change."""
    if not collection:
        return
    prefs = {"date_filter": options["date_filter"], "columns": options["columns"]}
    snapshot = repr(prefs)
    if st.session_state.get(SAVED_PREFS_KEY) == snapshot:
        return
    try:
        save_transfer_prefs(collection, prefs)
    except OSError as exc:
        st.caption(f"Tercihler kaydedilemedi: {exc}")
        return
    st.session_state[SAVED_PREFS_KEY] = snapshot
    st.session_state[PREFS_KEY] = prefs


# --------------------------------------------------------------------------
# date range
# --------------------------------------------------------------------------


def _utc_bounds(
    start: date | None, end: date | None, mode: str
) -> tuple[datetime | None, datetime | None]:
    """
    Turn picked days into a half-open datetime range.

    `end` is the last wanted day, so the upper bound is the following midnight.
    Bounds are timezone aware, which lets pymongo store them as UTC.
    """
    zone = timezone.utc if mode == "utc" else datetime.now().astimezone().tzinfo
    lower = datetime.combine(start, time.min, tzinfo=zone) if start else None
    upper = (
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone) if end else None
    )
    return lower, upper


def _date_query(date_filter: dict[str, Any]) -> dict[str, Any] | None:
    if not date_filter.get("enabled") or not date_filter.get("field"):
        return None
    lower, upper = _utc_bounds(
        date_filter.get("start"), date_filter.get("end"), date_filter.get("timezone", "local")
    )
    return date_range_filter(date_filter["field"], lower, upper) or None


def _percent(fill: float | None) -> str:
    if fill is None:
        return "-"
    return "%" + f"{100 * fill:.1f}".replace(".", ",")


def _field_label(field: dict[str, Any]) -> str:
    fill = field.get("fill")
    if fill is None:
        return f"{field['path']} · {format_int(field['present'])} belge"
    return f"{field['path']} · {_percent(fill)}"


def _date_card(settings: Settings, collection: str, prefs: dict) -> dict[str, Any]:
    saved = prefs["date_filter"]
    candidates = date_field_options(settings, collection)
    paths = [field["path"] for field in candidates]
    labels = {field["path"]: _field_label(field) for field in candidates}

    with st.container(border=True):
        theme.card_title(
            "Tarih aralığı",
            "Yalnız belirli bir dönemin kayıtları aktarılsın.",
        )
        if not candidates:
            st.caption(
                "Bu koleksiyonda tarih tipli alan bulunamadı. Tarihler metin olarak "
                "saklanıyorsa aralık filtresi uygulanamaz."
            )
            return dict(default_transfer_prefs()["date_filter"])

        enabled = st.checkbox(
            "Tarih aralığı uygula",
            value=bool(saved["enabled"]),
            key=f"tr_date_on_{collection}",
            help="Kapalıyken koleksiyonun tamamı (ya da artımlı modda yeni kayıtlar) aktarılır.",
        )
        if not enabled:
            st.caption("Kapalı: tarihe göre süzme yapılmaz.")
            return {
                "enabled": False,
                "field": saved["field"],
                "start": saved["start"],
                "end": saved["end"],
                "timezone": saved["timezone"],
            }

        row = st.columns([2.2, 1.4, 1.4, 1.6], vertical_alignment="bottom")
        with row[0]:
            index = paths.index(saved["field"]) if saved["field"] in paths else 0
            field = st.selectbox(
                "Tarih alanı",
                options=paths,
                index=index,
                format_func=lambda path: labels.get(path, path),
                key=f"tr_date_field_{collection}",
                help=(
                    "Yüzde, örneklenen belgelerin ne kadarında bu alanda gerçek bir "
                    "tarih olduğunu gösterir. Alanı boş olan belgeler aralığa girmez."
                ),
            )
        with row[1]:
            start = st.date_input(
                "Başlangıç",
                value=saved["start"] or date.today().replace(month=1, day=1),
                format="DD.MM.YYYY",
                key=f"tr_date_start_{collection}",
            )
        with row[2]:
            end = st.date_input(
                "Bitiş",
                value=saved["end"] or date.today(),
                format="DD.MM.YYYY",
                key=f"tr_date_end_{collection}",
                help="Bitiş günü dahildir.",
            )
        with row[3]:
            zone = st.radio(
                "Saat dilimi",
                options=("local", "utc"),
                index=0 if saved["timezone"] != "utc" else 1,
                format_func=lambda mode: "Yerel saat" if mode == "local" else "UTC",
                horizontal=True,
                key=f"tr_date_tz_{collection}",
                help="Seçilen günler bu saat dilimine göre başlar ve biter; Mongo'ya UTC olarak gider.",
            )

        date_filter = {
            "enabled": True,
            "field": field,
            "start": start,
            "end": end,
            "timezone": zone,
        }
        if start and end and start > end:
            st.error("Başlangıç tarihi bitişten sonra olamaz.")
            date_filter["enabled"] = False
            return date_filter

        lower, upper = _utc_bounds(start, end, zone)
        st.caption(
            f"Filtre · `{field}` >= `{lower:%Y-%m-%d %H:%M %Z}` ve < `{upper:%Y-%m-%d %H:%M %Z}`"
        )

        chosen = next((item for item in candidates if item["path"] == field), None)
        if chosen and chosen["fill"] is not None and chosen["fill"] < 0.99:
            st.warning(
                f"Belgelerin yalnızca {_percent(chosen['fill'])}'inde `{field}` dolu. "
                "Bu alanı taşımayan belgeler hiçbir aralığa girmez."
            )

        if field_has_index(settings, collection, field) is False:
            st.warning(
                f"`{field}` alanında index yok; Mongo tüm koleksiyonu tarar. "
                f"Hızlandırmak için: `db.{collection}.createIndex({{ {field}: 1 }})`"
            )

        if st.button("Kaç kayıt?", key=f"tr_date_count_{collection}"):
            total = count_matching(settings, collection, _date_query(date_filter))
            if total is None:
                st.caption("Sayım yapılamadı.")
            else:
                st.caption(f"Bu aralıkta **{format_int(total)}** kayıt var.")
    return date_filter


# --------------------------------------------------------------------------
# column selection
# --------------------------------------------------------------------------


def _exclusions(collection: str, prefs: dict) -> dict[str, set[str]]:
    """Live exclusion sets for this collection, seeded from the saved prefs."""
    if EXCLUDE_KEY not in st.session_state:
        st.session_state[EXCLUDE_KEY] = {
            "exclude": set(prefs["columns"]["exclude"]),
            "exclude_tables": set(prefs["columns"]["exclude_tables"]),
        }
    return st.session_state[EXCLUDE_KEY]


def _as_columns_pref(state: dict[str, set[str]]) -> dict[str, list[str]]:
    return {
        "exclude": sorted(state["exclude"]),
        "exclude_tables": sorted(state["exclude_tables"]),
    }


def _bump_editor() -> None:
    st.session_state[EDITOR_NONCE] = st.session_state.get(EDITOR_NONCE, 0) + 1


def _row_included(row: dict[str, Any], state: dict[str, set[str]]) -> bool:
    if row["kind"] == "map":
        return row["source"] not in state["exclude_tables"]
    return row["path"] not in state["exclude"]


def _dropped_tables(plan: dict, selected: dict) -> list[str]:
    kept = set(plan_tables(selected))
    return [table for table in plan_tables(plan) if table not in kept]


def _columns_card(
    settings: Settings, options: dict, query: dict[str, Any] | None
) -> tuple[dict | None, dict[str, list[str]]]:
    """
    Pick which columns and child tables to write.

    Returns (profiled plan, exclusions). The plan is None until a profile
    exists, because listing columns requires reading the collection first.
    """
    collection = options["collection"]
    state = _exclusions(collection, _prefs(collection))
    plan = stored_plan(options, query)

    with st.container(border=True):
        theme.card_title("Kolonlar", "Aktarılacak kolonları ve alt tabloları seçin.")
        if plan is None:
            st.caption(
                "Kolon listesi koleksiyonun profilinden gelir. Profil ayarları yukarıda; "
                "listeyi getirmek koleksiyonu okur."
            )
            if st.button("Kolonları getir", key=f"tr_cols_fetch_{collection}"):
                cached_plan(settings, options, query)
                st.rerun()
            if state["exclude"] or state["exclude_tables"]:
                st.caption(
                    f"Kayıtlı seçim: {len(state['exclude'])} kolon, "
                    f"{len(state['exclude_tables'])} tablo hariç tutuluyor."
                )
            return None, _as_columns_pref(state)

        rows = plan_column_rows(plan)
        if not rows:
            st.caption("Bu planda anahtar dışında kolon yok.")
            return plan, _as_columns_pref(state)

        head = st.columns([2.4, 1.2, 1.2], vertical_alignment="bottom")
        with head[0]:
            search = st.text_input(
                "Ara",
                placeholder="kolon ya da path...",
                key=f"tr_cols_search_{collection}",
            )
        with head[1]:
            if st.button("Tümünü seç", width="stretch", key=f"tr_cols_all_{collection}"):
                state["exclude"].clear()
                state["exclude_tables"].clear()
                _bump_editor()
                st.rerun()
        with head[2]:
            if st.button("Tümünü kaldır", width="stretch", key=f"tr_cols_none_{collection}"):
                state["exclude"] = {row["path"] for row in rows if row["kind"] != "map"}
                state["exclude_tables"] = {
                    row["source"] for row in rows if row["kind"] == "map"
                }
                st.session_state[EXCLUDE_KEY] = state
                _bump_editor()
                st.rerun()

        needle = (search or "").strip().lower()
        # A data editor remembers its edits per key. Reusing a key for a
        # different row set would re-apply those edits to the wrong rows, so
        # every change of the search term starts a fresh editor.
        search_state = f"tr_cols_last_search_{collection}"
        if st.session_state.get(search_state) != needle:
            st.session_state[search_state] = needle
            _bump_editor()

        visible = [
            row
            for row in rows
            if not needle or needle in row["path"].lower() or needle in row["name"].lower()
        ]
        if not visible:
            st.caption(f"`{search}` ile eşleşen kolon yok.")
            return plan, _as_columns_pref(state)

        data = [
            {
                "aktar": _row_included(row, state),
                "tablo": row["table"],
                "kolon": row["name"],
                "path": row["path"],
                "tip": row["sql_type"],
                "doluluk": _percent(row["fill"]),
            }
            for row in visible
        ]
        # The plan's own shape is part of the key too: a re-profile can produce
        # a different set of columns.
        shape_id = abs(hash(tuple(row["path"] for row in rows))) % 10**8
        nonce = st.session_state.get(EDITOR_NONCE, 0)
        edited = st.data_editor(
            data,
            key=f"tr_cols_{collection}_{shape_id}_{nonce}",
            hide_index=True,
            width="stretch",
            column_config={
                "aktar": st.column_config.CheckboxColumn("aktar", width="small"),
                "tablo": st.column_config.TextColumn("tablo", width="medium"),
                "kolon": st.column_config.TextColumn("kolon", width="medium"),
                "path": st.column_config.TextColumn("Mongo path", width="large"),
                "tip": st.column_config.TextColumn("SQL tipi", width="small"),
                "doluluk": st.column_config.TextColumn("doluluk", width="small"),
            },
            disabled=("tablo", "kolon", "path", "tip", "doluluk"),
        )

        # Only the visible rows are touched, so a search does not clear the
        # choices made for rows that are currently filtered out.
        for row, entry in zip(visible, edited):
            include = bool(entry.get("aktar"))
            if row["kind"] == "map":
                bucket, value = state["exclude_tables"], row["source"]
            else:
                bucket, value = state["exclude"], row["path"]
            if include:
                bucket.discard(value)
            else:
                bucket.add(value)
        st.session_state[EXCLUDE_KEY] = state

        columns = _as_columns_pref(state)
        selected = apply_column_selection(
            plan, columns["exclude"], columns["exclude_tables"]
        )
        dropped = _dropped_tables(plan, selected)
        kept = len(selected["root"]["columns"])
        st.caption(
            f"Kök tabloda {kept} kolon yazılacak. `mongo_id`, alt tablo anahtarları ve "
            "dizi sıra kolonları her zaman aktarılır."
        )
        if dropped:
            st.info(
                "Tüm kolonları kapatıldığı için şu tablolar oluşturulmayacak: "
                + ", ".join(dropped)
            )
        if (columns["exclude"] or columns["exclude_tables"]) and not options["allow_null"]:
            st.warning(
                "Kolon kapattığınızda var olan tabloda o kolon NULL kalır. "
                "\"Anahtar dışındaki kolonlar NULL kabul etsin\" seçeneğini açın ya da "
                "tabloları yeniden oluşturun."
            )
    return plan, columns


# --------------------------------------------------------------------------
# option cards
# --------------------------------------------------------------------------


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

    prefs = _prefs(collection)

    st.write("")
    options["nesting"] = nesting_card(
        settings, collection, options["table"] or sql_ident(collection)
    )

    st.write("")
    options["date_filter"] = _date_card(settings, collection, prefs)

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
            if options["date_filter"].get("enabled"):
                st.warning(
                    "Artımlı senkron ile tarih aralığı birlikte kullanılıyor. Belgeler `_id` "
                    "sırasıyla okunur ve işaret yalnızca filtreden geçen son belgeye ilerler; "
                    "aralık dışında kalan daha büyük `_id`'ler sonraki koşularda bir daha "
                    "okunmaz. Dönem bazlı yükleme için tam senkron daha güvenlidir."
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


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _build_plan(settings: Settings, options: dict) -> dict:
    plan = cached_plan(settings, options, _date_query(options["date_filter"]))
    if plan is None:
        raise RuntimeError("Plan için koleksiyon ve kök tablo adı gerekir.")
    columns = options["columns"]
    return apply_column_selection(plan, columns["exclude"], columns["exclude_tables"])


def _write_query(options: dict) -> dict | None:
    """Mongo filter for the write pass: date range, `_id` watermark, or both."""
    dates = _date_query(options["date_filter"])
    if options["mode"] != "incremental":
        return dates
    mark = options.get("watermark")
    if not mark:
        return dates
    try:
        last_id = decode_mongo_id(mark["last_id"], mark["last_id_type"])
    except Exception:
        return dates
    return combine_filters(dates, id_after_filter(last_id))


def _range_note(date_filter: dict[str, Any]) -> str:
    if not date_filter.get("enabled") or not date_filter.get("field"):
        return ""
    start, end = date_filter.get("start"), date_filter.get("end")
    if not start or not end:
        return f" · {date_filter['field']}"
    return f" · {date_filter['field']} {start:%d.%m.%Y}–{end:%d.%m.%Y}"


def _plan_summary(settings: Settings, plan: dict, options: dict) -> None:
    tables = plan_tables(plan)
    mode_label = "Artımlı" if options["mode"] == "incremental" else "Tam senkron"
    nesting_title = nesting_labels().get(plan.get("nesting") or "", plan.get("nesting") or "")
    with st.container(border=True):
        theme.card_title(
            "Plan",
            f"<code>{settings.mssql.get('database')}</code> · "
            f"<code>{plan['schema']}</code> — {len(tables)} tablo · {mode_label} · "
            f"{nesting_title}{_range_note(options['date_filter'])}",
        )
        st.code("\n".join(tables), language="text")


def _run(settings: Settings, options: dict, write: bool) -> None:
    plan = _build_plan(settings, options)
    st.session_state[PLAN_KEY] = plan
    _plan_summary(settings, plan, options)
    date_filter = options["date_filter"]
    mode_label = "Artımlı" if options["mode"] == "incremental" else "Tam senkron"

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
        if stats.documents == 0 and date_filter.get("enabled"):
            st.info("Seçilen tarih aralığında belge bulunamadı.")
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
    if options["collection"]:
        st.write("")
        _, options["columns"] = _columns_card(
            settings, options, _date_query(options["date_filter"])
        )
        _remember_prefs(options["collection"], options)

    ready = bool(
        settings.mongo_ready and settings.sql_ready and options["collection"] and options["table"]
    )
    write_label = "Artımlı senkron" if options["mode"] == "incremental" else "Tam senkron"

    if options["collection"]:
        st.write("")
        actions = st.columns([1.3, 1.3, 1.3, 2.1])
        with actions[0]:
            do_plan = st.button("Planı hazırla", key="tr_do_plan", width="stretch")
        with actions[1]:
            do_create = st.button(
                "Tabloları oluştur",
                key="tr_do_create",
                disabled=not ready or options["mode"] == "incremental",
                width="stretch",
            )
        with actions[2]:
            do_write = st.button(
                write_label,
                key="tr_do_write",
                type="primary",
                disabled=not ready,
                width="stretch",
            )
    else:
        do_plan = do_create = do_write = False

    if do_create or do_write:
        st.write("")
        try:
            _run(settings, options, write=do_write)
        except Exception as exc:
            st.error(str(exc))
    elif do_plan:
        # Rerun so the column picker above can list this plan's columns.
        try:
            st.session_state[PLAN_KEY] = _build_plan(settings, options)
        except Exception as exc:
            st.error(str(exc))
        else:
            st.rerun()
    elif PLAN_KEY in st.session_state:
        st.write("")
        _plan_summary(settings, st.session_state[PLAN_KEY], options)

    if PLAN_KEY in st.session_state:
        with st.expander("Üretilen DDL", expanded=False):
            st.code(render_database_ddl([st.session_state[PLAN_KEY]]), language="sql")
