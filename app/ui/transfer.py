"""Transfer page: full or incremental load of a collection into SQL Server."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import sys
import streamlit as st

from app.ui import theme
from app.ui import transfer_job
from app.ui.services import (
    Settings,
    apply_remembered_collection,
    cached_plan,
    collection_count_caption,
    collection_list,
    count_matching,
    date_field_bounds,
    date_field_options,
    format_int,
    invalidate_sql_watermarks,
    nesting_card,
    remember_collection,
    sql_table_watermark,
    stored_plan,
    NESTING_KEY,
)
from core.inspect import nesting_labels, render_database_ddl, sql_table_ident
from core.logutil import log_path_display
from core.mongo import date_range_filter
from core.settings import (
    ROOT,
    default_transfer_prefs,
    load_sync_watermark,
    load_transfer_prefs,
    save_transfer_prefs,
)
from core.transfer import (
    apply_column_selection,
    plan_column_rows,
    plan_tables,
)

PLAN_KEY = "transfer_plan"
RUN_REQUEST_KEY = "tr_run_request"
RUN_DONE_KEY = "tr_run_done"
JOB_SEQ_KEY = "tr_job_seq"
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
        "schedule_mode": "auto",
        "watermark": None,
        "nesting": "hybrid",
        "date_filter": default_transfer_prefs()["date_filter"],
        "columns": default_transfer_prefs()["columns"],
    }


# --------------------------------------------------------------------------
# saved preferences
# --------------------------------------------------------------------------


def _prefs(collection: str | None) -> dict:
    """Saved job settings, read once per collection."""
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
        _apply_prefs_widgets(prefs, collection)
    return st.session_state[PREFS_KEY]


def _apply_prefs_widgets(prefs: dict, collection: str) -> None:
    """Seed widgets when the selected collection changes."""
    if prefs.get("schema"):
        st.session_state["tr_schema"] = prefs["schema"]
    table_key = f"tr_table_{collection}"
    st.session_state[table_key] = prefs.get("table") or sql_table_ident(collection)
    st.session_state["tr_sample"] = int(prefs.get("sample") or 5000)
    st.session_state["tr_batch"] = int(prefs.get("batch") or 500)
    st.session_state["tr_null"] = bool(prefs.get("allow_null", True))
    if prefs.get("nesting"):
        st.session_state[NESTING_KEY] = prefs["nesting"]


def _job_prefs(options: dict) -> dict:
    return {
        "date_filter": options["date_filter"],
        "columns": options["columns"],
        "nesting": options.get("nesting") or "hybrid",
        "table": options.get("table") or "",
        "schema": options.get("schema") or "",
        "schedule_mode": options.get("schedule_mode") or "auto",
        "batch": int(options.get("batch") or 500),
        "sample": int(options["sample"]) if options.get("sample") is not None else 5000,
        "allow_null": bool(options.get("allow_null", True)),
    }


def _remember_prefs(collection: str | None, options: dict) -> None:
    """Write the current choices to config.local.yaml, but only when they change."""
    if not collection:
        return
    prefs = _job_prefs(options)
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


def _filter_tz(mode: str):
    return timezone.utc if mode == "utc" else datetime.now().astimezone().tzinfo


def _utc_bounds(
    start: date | None, end: date | None, mode: str
) -> tuple[datetime | None, datetime | None]:
    """
    Turn picked days into a half-open datetime range.

    `end` is the last wanted day, so the upper bound is the following midnight.
    Bounds are timezone aware, which lets pymongo store them as UTC.
    """
    zone = _filter_tz(mode)
    lower = datetime.combine(start, time.min, tzinfo=zone) if start else None
    upper = (
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone) if end else None
    )
    return lower, upper


def _as_calendar_date(value: datetime | None, mode: str) -> date | None:
    """Convert a Mongo datetime to the calendar day in the transfer timezone."""
    if value is None or not isinstance(value, datetime):
        return None
    instant = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return instant.astimezone(_filter_tz(mode)).date()


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
        base = f"{field['path']} · {format_int(field['present'])} belge"
    else:
        base = f"{field['path']} · {_percent(fill)}"
    indexed = field.get("indexed")
    if indexed is True:
        return f"{base} · index var"
    if indexed is False:
        return f"{base} · index yok"
    return base


def _preferred_date_path(candidates: list[dict[str, Any]], saved: str | None) -> str:
    """Indexed date field first; keep a saved pick only when it is also indexed."""
    paths = [item["path"] for item in candidates]
    indexed = [item["path"] for item in candidates if item.get("indexed") is True]
    if saved in paths and (saved in indexed or not indexed):
        return saved
    if indexed:
        return indexed[0]
    return paths[0]


def _date_card(settings: Settings, collection: str, prefs: dict) -> dict[str, Any]:
    saved = prefs["date_filter"]
    candidates = date_field_options(settings, collection)
    paths = [field["path"] for field in candidates]
    labels = {field["path"]: _field_label(field) for field in candidates}
    indexed_paths = [item["path"] for item in candidates if item.get("indexed") is True]

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

        if indexed_paths:
            st.caption(
                "Index'li tarih alanları listenin başında. Aralık taraması bunlarda index kullanır."
            )
        elif any(item.get("indexed") is False for item in candidates):
            st.warning(
                "Hiçbir tarih alanında range index yok; seçilen aralık tüm koleksiyonu tarar."
            )

        row = st.columns([2.2, 1.4, 1.4, 1.6], vertical_alignment="bottom")
        with row[0]:
            field_key = f"tr_date_field_{collection}"
            preferred = _preferred_date_path(candidates, saved.get("field"))
            applied = f"tr_date_idx_applied_{collection}"
            current = st.session_state.get(field_key)
            if current not in paths:
                st.session_state[field_key] = preferred
            elif not st.session_state.get(applied):
                if current not in indexed_paths and preferred in indexed_paths:
                    st.session_state[field_key] = preferred
            st.session_state[applied] = True
            field = st.selectbox(
                "Tarih alanı",
                options=paths,
                format_func=lambda path: labels.get(path, path),
                key=field_key,
                help=(
                    "Yüzde, örneklenen belgelerin ne kadarında bu alanda gerçek bir "
                    "tarih olduğunu gösterir. Index'li alanlar önce gelir. "
                    "Alanı boş olan belgeler aralığa girmez."
                ),
            )

        zone_key = f"tr_date_tz_{collection}"
        zone_now = st.session_state.get(zone_key) or saved.get("timezone") or "local"
        if zone_now not in ("local", "utc"):
            zone_now = "local"
        indexed = next(
            (item.get("indexed") for item in candidates if item["path"] == field),
            None,
        )
        if indexed is True:
            lo_dt, hi_dt = date_field_bounds(settings, collection, field)
        else:
            lo_dt, hi_dt = None, None
        lo_date = _as_calendar_date(lo_dt, zone_now)
        hi_date = _as_calendar_date(hi_dt, zone_now)

        start_key = f"tr_date_start_{collection}"
        end_key = f"tr_date_end_{collection}"
        seed_key = f"tr_date_seed_{collection}"
        if st.session_state.get(seed_key) != field:
            if lo_date and hi_date:
                st.session_state[start_key] = lo_date
                st.session_state[end_key] = hi_date
            elif indexed is True:
                st.session_state[start_key] = date.today().replace(month=1, day=1)
                st.session_state[end_key] = date.today()
            else:
                # Unindexed min/max would scan the collection; seed a single day.
                st.session_state[start_key] = date.today()
                st.session_state[end_key] = date.today()
            st.session_state[seed_key] = field

        with row[1]:
            start = st.date_input(
                "Başlangıç",
                format="DD.MM.YYYY",
                key=start_key,
            )
        with row[2]:
            end = st.date_input(
                "Bitiş",
                format="DD.MM.YYYY",
                key=end_key,
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
        if lo_date and hi_date:
            st.caption(f"Verideki aralık · `{field}` {lo_date:%d.%m.%Y}–{hi_date:%d.%m.%Y}")
        elif indexed is True:
            st.caption(
                f"`{field}` için min/max okunamadı; kutular takvim varsayılanıyla doldu."
            )
        elif indexed is False:
            st.caption(
                f"`{field}` index'siz olduğu için min/max okunmadı (koleksiyon taraması). "
                "Kutular bugüne ayarlandı; aralığı kendiniz seçin."
            )
        else:
            st.caption(
                "Index listesi okunamadı; min/max taraması atlandı. "
                "Kutular bugüne ayarlandı."
            )

        chosen = next((item for item in candidates if item["path"] == field), None)
        if chosen and chosen["fill"] is not None and chosen["fill"] < 0.99:
            st.warning(
                f"Belgelerin yalnızca {_percent(chosen['fill'])}'inde `{field}` dolu. "
                "Bu alanı taşımayan belgeler hiçbir aralığa girmez."
            )

        if indexed is False and indexed_paths:
            st.warning(
                f"`{field}` alanında index yok; Mongo tüm koleksiyonu tarar. "
                f"Index'li alternatif: `{indexed_paths[0]}`."
            )
        elif indexed is False:
            st.caption(
                f"Hızlandırmak için: `db.{collection}.createIndex({{ {field}: 1 }})`"
            )

        count_ok = indexed is True
        if st.button(
            "Kaç kayıt?",
            key=f"tr_date_count_{collection}",
            disabled=not count_ok,
            help=(
                "Index'li alanda kaç belgenin aralığa girdiğini sayar."
                if count_ok
                else "Index yokken sayım tüm koleksiyonu tarar; bu yüzden kapalı."
            ),
        ):
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

    Returns (profiled plan, exclusions). The plan is None when the collection
    could not be profiled, because listing columns requires reading it first.
    """
    collection = options["collection"]
    state = _exclusions(collection, _prefs(collection))
    plan = stored_plan(options, query)

    with st.container(border=True):
        theme.card_title("Kolonlar", "Aktarılacak kolonları ve alt tabloları seçin.")
        if plan is None:
            st.caption(
                "Kolon listesi koleksiyonun profilinden gelir. Profil alınamadı: "
                "kök tablo adını ve Mongo bağlantısını kontrol edin."
            )
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
        with st.container(key="tr_cols_grid"):
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
        prefs = _prefs(collection) if collection else default_transfer_prefs()
        with row[1]:
            schema = st.text_input("Hedef şema", value=settings.schema, key="tr_schema")
        with row[2]:
            table = st.text_input(
                "Kök tablo",
                key=f"tr_table_{collection or 'none'}",
                disabled=not collection,
                help="PascalCase yazılır. Alt tablolar bu addan türer: <Ad><Alan>.",
            )
        if not collection:
            st.caption("Koleksiyon seçildikten sonra iç içe yapı sorulur.")
        else:
            collection_count_caption(settings, collection)

    options["collection"] = collection
    options["schema"] = (schema or "").strip() or settings.schema
    # Normalised here as well as in the plan, so the watermark and table-exists
    # lookups ask about the name the transfer will actually create.
    options["table"] = sql_table_ident(table) if (table or "").strip() else ""
    if not collection:
        return options

    st.write("")
    options["nesting"] = nesting_card(
        settings, collection, options["table"] or sql_table_ident(collection)
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
                step=1000,
                key="tr_sample",
                help=(
                    "Şema için kaç belge taransın. 5000 önerilir. "
                    "0 = koleksiyonun tamamı. Aktarılacak belge sayısını değiştirmez."
                ),
            )
        with row2[1]:
            batch = st.number_input(
                "Yazma partisi",
                min_value=100,
                value=500,
                step=100,
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
            "schedule_mode": prefs.get("schedule_mode") or "auto",
            "watermark": watermark,
        }
    )
    return options


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _run_signature(options: dict) -> str:
    """
    Everything a run depends on, so a finished run can stay finished.

    The watermark is left out: an incremental pass advances it, and that alone
    should not turn the button back on.
    """
    columns = options.get("columns") or {}
    return "|".join(
        str(part)
        for part in (
            options.get("collection"),
            options.get("schema"),
            options.get("table"),
            options.get("nesting"),
            options.get("sample"),
            options.get("batch"),
            options.get("mode"),
            options.get("recreate"),
            options.get("clear_first"),
            options.get("allow_null"),
            repr(options.get("date_filter")),
            repr(sorted(columns.get("exclude") or [])),
            repr(sorted(columns.get("exclude_tables") or [])),
        )
    )


def _request_run() -> None:
    """Claim the next rerun for a write, so the button renders disabled while it runs."""
    st.session_state[RUN_REQUEST_KEY] = True
    st.session_state.pop(RUN_DONE_KEY, None)


def _request_stop() -> None:
    transfer_job.request_stop()


def _range_note(date_filter: dict[str, Any]) -> str:
    if not date_filter.get("enabled") or not date_filter.get("field"):
        return ""
    start, end = date_filter.get("start"), date_filter.get("end")
    if not start or not end:
        return f" · {date_filter['field']}"
    return f" · {date_filter['field']} {start:%d.%m.%Y}–{end:%d.%m.%Y}"


def _scheduler_command(collection: str, mode: str) -> tuple[str, str, str]:
    """Return (one-line command, PowerShell script, batch script)."""
    python = sys.executable
    script = ROOT / "tools" / "run_transfer.py"
    cmdline = f'"{python}" "{script}" --collection {collection} --mode {mode}'
    if any(ch.isspace() for ch in collection):
        cmdline = f'"{python}" "{script}" --collection "{collection}" --mode {mode}'
    ps1 = (
        "$ErrorActionPreference = 'Stop'\n"
        f"Set-Location -LiteralPath '{ROOT}'\n"
        f"& '{python}' '{script}' --collection '{collection}' --mode {mode}\n"
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    )
    bat = (
        "@echo off\n"
        f"cd /d \"{ROOT}\"\n"
        f"\"{python}\" \"{script}\" --collection {collection} --mode {mode}\n"
        "exit /b %ERRORLEVEL%\n"
    )
    return cmdline, ps1, bat


def _scheduler_card(options: dict) -> None:
    collection = options.get("collection")
    if not collection:
        return
    mode = options.get("schedule_mode") or "auto"
    cmdline, ps1, bat = _scheduler_command(collection, mode)
    with st.container(border=True):
        theme.card_title(
            "Zamanla",
            "Windows Görev Zamanlayıcı bu komutu çalıştırır. "
            "İlk koşu boş tabloda tam senkron, sonrakiler artımlı (`auto`).",
        )
        st.code(cmdline, language="text")
        st.caption(
            f"Başlangıç dizini `{ROOT}`. Windows kimliği için görevi oturum açmış "
            "kullanıcıyla çalıştırın. SQL şifresi gerekiyorsa `config.local.yaml` içinde olmalı."
        )
        row = st.columns(2)
        with row[0]:
            st.download_button(
                ".ps1 indir",
                ps1,
                file_name=f"mongo2sql_{collection}.ps1",
                mime="text/plain",
                key="tr_sched_ps1",
                width="stretch",
            )
        with row[1]:
            st.download_button(
                ".bat indir",
                bat,
                file_name=f"mongo2sql_{collection}.bat",
                mime="text/plain",
                key="tr_sched_bat",
                width="stretch",
            )


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


def _mongo_cfg(settings: Settings) -> dict[str, Any]:
    cfg = dict(settings.mongo)
    if settings.mongo_password:
        cfg["password"] = settings.mongo_password
    return cfg


def _consume_finished_job(job: transfer_job.JobView) -> bool:
    """Record a finished background job once, then trigger a full rerun."""
    if job.status not in {"done", "error", "cancelled"}:
        return False
    if not job.seq or st.session_state.get(JOB_SEQ_KEY) == job.seq:
        return False
    st.session_state[JOB_SEQ_KEY] = job.seq
    if job.status == "done":
        st.session_state[RUN_DONE_KEY] = job.signature
    invalidate_sql_watermarks()
    return True


def _launch_if_requested(settings: Settings, options: dict, selected: dict | None) -> None:
    if not st.session_state.get(RUN_REQUEST_KEY):
        return
    st.session_state.pop(RUN_REQUEST_KEY, None)
    if selected is None:
        st.warning("Aktarım başlatılamadı: plan hazır değil.")
        return
    if transfer_job.is_busy():
        st.warning("Bir aktarım zaten sürüyor.")
        return
    date_note = _range_note(options["date_filter"]).lstrip(" ·")
    transfer_job.start(
        plan=selected,
        options=options,
        mongo_cfg=_mongo_cfg(settings),
        mssql_cfg=dict(settings.mssql),
        mssql_password=settings.mssql_password,
        date_query=_date_query(options["date_filter"]),
        signature=_run_signature(options),
        log_extra={"aralık": date_note} if date_note else None,
    )
    st.rerun()


def _progress_fraction(job: transfer_job.JobView) -> float:
    if job.total:
        return min(max(job.done / job.total, 0.0), 1.0)
    return 0.0


def _progress_caption(job: transfer_job.JobView) -> str:
    mode = "Artımlı" if job.mode == "incremental" else "Tam senkron"
    if job.total:
        return (
            f"{mode} · `{job.collection}` · "
            f"{format_int(job.done)} / {format_int(job.total)} belge"
        )
    if job.done:
        return f"{mode} · `{job.collection}` · {format_int(job.done)} belge"
    return f"{mode} · `{job.collection}` · {job.message}"


def _draw_running(job: transfer_job.JobView, *, stop_key: str, show_bar: bool) -> None:
    if show_bar:
        st.progress(_progress_fraction(job))
    st.caption(_progress_caption(job))
    if job.message:
        st.caption(job.message)
    st.button(
        "Aktarımı durdur",
        key=stop_key,
        on_click=_request_stop,
        disabled=job.status != "running",
        width="stretch",
        help="Açık yazma partisi bitince durur. Sayfa değiştirmek aktarımı durdurmaz.",
    )


def render_sidebar_job() -> None:
    """Live transfer status in the sidebar on every page."""
    job = transfer_job.snapshot()
    if job.status not in {"running", "stopping"}:
        if _consume_finished_job(job):
            st.rerun()
        return

    with st.container(key="m2s_job"):

        @st.fragment(run_every=1.5)
        def _tick() -> None:
            live = transfer_job.snapshot()
            if live.status in {"running", "stopping"}:
                _draw_running(live, stop_key="tr_stop_side", show_bar=True)
                return
            if _consume_finished_job(live):
                st.rerun()

        _tick()


def _render_result(options: dict, job: transfer_job.JobView) -> None:
    stats = job.stats
    mode_label = "Artımlı" if options["mode"] == "incremental" else "Tam senkron"
    if job.created:
        st.success("Oluşturulan tablolar: " + ", ".join(job.created))
    if job.existing:
        st.caption("Zaten mevcut: " + ", ".join(job.existing))
    if job.status == "cancelled":
        st.warning("Aktarım durduruldu. Yazılmış partiler SQL'de kaldı.")
    if job.status == "error":
        st.error(job.error or job.message)
        return
    if stats is None:
        return
    with st.container(border=True):
        theme.card_title("Sonuç", f"{options['collection']} → {options['schema']}")
        cols = st.columns(5)
        cols[0].metric("Mod", mode_label)
        cols[1].metric("Belge", stats.documents)
        cols[2].metric("Satır", stats.total_rows)
        cols[3].metric("Atlanan", stats.skipped_no_id)
        cols[4].metric("Kırpılan", stats.truncated)
        if stats.first_load:
            st.caption("İlk yükleme: hedef tablo boştu, parti silmeleri atlandı.")
        st.caption(f"Günlük · `{log_path_display()}`")
        if stats.last_id:
            st.caption(f"İşaret `_id` = `{stats.last_id}`")
        if stats.rows:
            st.dataframe(
                [{"tablo": table, "satır": count} for table, count in stats.rows.items()],
                hide_index=True,
                width="stretch",
            )
    if stats.documents == 0 and options["mode"] == "incremental" and job.status == "done":
        st.info("Yeni belge yok; işaret zaten güncel.")
    if stats.documents == 0 and options["date_filter"].get("enabled") and job.status == "done":
        st.info("Seçilen tarih aralığında belge bulunamadı.")
    if stats.skipped_no_id:
        st.caption(f"{stats.skipped_no_id} belgede `_id` yok, birincil anahtar üretilemedi.")
    if stats.truncated:
        st.warning(
            f"{stats.truncated} değer kolon genişliğine kırpıldı. Tam senkron ile "
            "tam tarama yapıp tabloları yeniden oluşturmak bunu giderir."
        )


def render(settings: Settings) -> None:
    theme.page_header(
        "Aktarım",
        "SQL aktarımı",
        "Tam senkron tüm koleksiyonu yazar. Artımlı, SQL tablosundaki son `_id` "
        "sonrası yeni belgeleri ekler; eski kayıtlardaki güncelleme için tam senkron gerekir.",
        step="transfer",
    )
    st.caption(
        f"Aktarım günlüğü `{log_path_display()}` — başlangıç, ilerleme ve bitiş bu dosyaya yazılır. "
        "Başladıktan sonra başka sayfaya geçmek aktarımı durdurmaz; durdurmak için **Aktarımı durdur**."
    )

    collections, error, warning = collection_list(settings)
    if error:
        st.error(error)
    elif warning:
        st.warning(warning)

    blockers = []
    if not settings.mongo_ready:
        blockers.append("Mongo bağlantısı")
    if settings.sql_needs_password:
        blockers.append("SQL şifresi")
    elif not settings.sql_ready:
        blockers.append("SQL bağlantısı")
    if blockers:
        theme.need_connections(blockers)

    options = _target_card(settings, collections)

    # The plan follows the selections above on its own: profiling is cached per
    # option set, so a rerun that changes nothing does not re-read Mongo.
    plan = None
    if options["collection"] and options["table"] and settings.mongo_ready:
        try:
            plan = cached_plan(settings, options, _date_query(options["date_filter"]))
        except Exception as exc:
            st.error(f"Koleksiyon profillenemedi: {exc}")

    if options["collection"]:
        st.write("")
        _, options["columns"] = _columns_card(
            settings, options, _date_query(options["date_filter"])
        )
        _remember_prefs(options["collection"], options)

    selected = None
    if plan is not None:
        selected = apply_column_selection(
            plan, options["columns"]["exclude"], options["columns"]["exclude_tables"]
        )
        st.session_state[PLAN_KEY] = selected
    else:
        st.session_state.pop(PLAN_KEY, None)

    job = transfer_job.snapshot()
    _consume_finished_job(job)
    ready = bool(settings.sql_ready and selected is not None)
    signature = _run_signature(options)
    busy = transfer_job.is_busy()
    finished = st.session_state.get(RUN_DONE_KEY) == signature
    write_label = "Artımlı senkron" if options["mode"] == "incremental" else "Tam senkron"

    if options["collection"]:
        st.write("")
        actions = st.columns([1.6, 1.6, 2.8], vertical_alignment="bottom")
        with actions[0]:
            st.button(
                write_label,
                key="tr_do_write",
                type="primary",
                disabled=not ready or busy or finished,
                on_click=_request_run,
                width="stretch",
                help="Eksik tabloları oluşturur, sonra belgeleri yazar. Sayfa değiştirmek durdurmaz.",
            )
        with actions[1]:
            if busy:
                st.button(
                    "Aktarımı durdur",
                    key="tr_do_stop",
                    on_click=_request_stop,
                    disabled=job.status != "running",
                    width="stretch",
                    help="Açık yazma partisi bitince durur.",
                )
            elif finished:
                st.button(
                    "Yeniden çalıştır",
                    key="tr_do_again",
                    on_click=_request_run,
                    width="stretch",
                )
        with actions[2]:
            if busy:
                st.caption("Aktarım arka planda sürüyor. Sayfa değiştirmek durdurmaz.")
            elif finished:
                st.caption("Bu ayarlarla aktarım tamamlandı. Bir ayarı değiştirin ya da yeniden çalıştırın.")
            elif not ready:
                st.caption("Koleksiyon, kök tablo ve SQL bağlantısı tamamlanınca aktarım açılır.")

    _launch_if_requested(settings, options, selected)

    if selected is not None:
        st.write("")
        _plan_summary(settings, selected, options)

    if options.get("collection"):
        st.write("")
        _scheduler_card(options)

    if busy:

        @st.fragment(run_every=1.5)
        def _live() -> None:
            live = transfer_job.snapshot()
            if live.status in {"running", "stopping"}:
                st.progress(_progress_fraction(live))
                st.caption(_progress_caption(live))
                if live.message:
                    st.caption(live.message)
                return
            if _consume_finished_job(live):
                st.rerun()

        _live()
    elif job.collection == (options.get("collection") or "") and job.status in {
        "done",
        "error",
        "cancelled",
    }:
        st.write("")
        _render_result(options, job)

    if PLAN_KEY in st.session_state:
        with st.expander("Üretilen DDL", expanded=False):
            st.code(render_database_ddl([st.session_state[PLAN_KEY]]), language="sql")
