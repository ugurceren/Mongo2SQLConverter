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
    collection_count,
    collection_count_caption,
    collection_list,
    count_matching,
    date_field_bounds,
    date_field_options,
    format_int,
    invalidate_sql_watermarks,
    mongo_client,
    nesting_summary,
    record_health,
    remember_collection,
    sql_checkpoint,
    sql_table_watermark,
    sql_target,
    stored_plan,
    table_names_editor,
)
from core.inspect import LARGE_COLLECTION, nesting_labels, render_database_ddl, sql_table_ident
from core.logutil import log_path_display
from core.mongo import date_range_filter
from core.preflight import PROBE_DOCS, format_duration, run_preflight
from core.rejects import read_lines
from core.settings import (
    ROOT,
    default_transfer_prefs,
    load_sync_watermark,
    load_transfer_prefs,
    save_transfer_prefs,
)
from core.transfer import (
    apply_column_selection,
    apply_table_names,
    plan_column_rows,
    plan_tables,
    resolve_table_names,
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
PREFLIGHT_KEY = "tr_preflight"
PREFLIGHT_REQUEST_KEY = "tr_preflight_request"


def _defaults(settings: Settings) -> dict:
    return {
        "collection": None,
        "schema": settings.schema,
        "table": "",
        "sample": 0,
        "batch": 2000,
        "recreate": False,
        "clear_first": False,
        "allow_null": True,
        "mode": "full",
        "schedule_mode": "auto",
        "watermark": None,
        "nesting": "hybrid",
        "date_filter": default_transfer_prefs()["date_filter"],
        "columns": default_transfer_prefs()["columns"],
        "table_names": {},
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
        st.session_state[f"{SAVED_PREFS_KEY}:{collection}"] = repr(prefs)
        st.session_state.pop(EXCLUDE_KEY, None)
        _apply_prefs_widgets(prefs, collection)
    return st.session_state[PREFS_KEY]


def _pending_table_key(collection: str) -> str:
    return f"tr_pending_table_{collection}"


def _table_widget_key(collection: str) -> str:
    return f"tr_table_{collection}"


def _sync_table_widget(collection: str, prefs: dict) -> None:
    """Apply pending / PascalCase names before the Kök tablo input is created."""
    table_key = _table_widget_key(collection)
    pending_key = _pending_table_key(collection)
    if pending_key in st.session_state:
        st.session_state[table_key] = st.session_state.pop(pending_key)
    elif table_key not in st.session_state:
        st.session_state[table_key] = prefs.get("table") or sql_table_ident(collection)
    current = st.session_state.get(table_key)
    if isinstance(current, str) and current.strip():
        normalized = sql_table_ident(current)
        if current != normalized:
            st.session_state[table_key] = normalized


def _apply_prefs_widgets(prefs: dict, collection: str) -> None:
    """Seed widgets when the selected collection changes."""
    st.session_state[_table_widget_key(collection)] = prefs.get("table") or sql_table_ident(
        collection
    )
    st.session_state[f"tr_sample_{collection}"] = int(prefs.get("sample") or 5000)
    st.session_state[f"tr_batch_{collection}"] = int(prefs.get("batch") or 2000)
    st.session_state[f"tr_null_{collection}"] = bool(prefs.get("allow_null", True))


def _job_prefs(options: dict) -> dict:
    return {
        "date_filter": options["date_filter"],
        "columns": options["columns"],
        "nesting": options.get("nesting") or "hybrid",
        "table": options.get("table") or "",
        "schedule_mode": options.get("schedule_mode") or "auto",
        "batch": int(options.get("batch") or 2000),
        "sample": int(options["sample"]) if options.get("sample") is not None else 5000,
        "allow_null": bool(options.get("allow_null", True)),
        "table_names": dict(options.get("table_names") or {}),
    }


def _remember_prefs(collection: str | None, options: dict) -> None:
    """Write the current choices to config.local.yaml, but only when they change."""
    if not collection or st.session_state.get(PREFS_STAMP) != collection:
        return
    prefs = _job_prefs(options)
    snapshot = repr(prefs)
    saved_key = f"{SAVED_PREFS_KEY}:{collection}"
    if st.session_state.get(saved_key) == snapshot:
        return
    try:
        save_transfer_prefs(collection, prefs)
    except OSError as exc:
        st.caption(f"Tercihler kaydedilemedi: {exc}")
        return
    st.session_state[saved_key] = snapshot
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

    with theme.collapsible_card(
        f"tr_date_{collection}",
        "Tarih aralığı",
        "Yalnız belirli bir dönemin kayıtları aktarılsın.",
    ):
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
    if plan is not None:
        plan = apply_table_names(plan, options.get("table_names") or {})

    with theme.collapsible_card(
        f"tr_cols_{collection}",
        "Kolonlar",
        "Aktarılacak kolonları ve alt tabloları seçin.",
    ):
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
        shape_id = abs(hash(tuple((row["path"], row["table"]) for row in rows))) % 10**8
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
    with theme.collapsible_card(
        "tr_target",
        "Hedef",
        "Önce koleksiyon ve kök tabloyu seçin. Kırılım sonra sorulur.",
    ):
        row = st.columns([2.6, 1.8], vertical_alignment="bottom")
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
        if collection:
            _sync_table_widget(collection, prefs)
        with row[1]:
            table = st.text_input(
                "Kök tablo",
                key=_table_widget_key(collection) if collection else "tr_table_none",
                disabled=not collection,
                help=(
                    "PascalCase yazılır. Alt tablolar bu addan türer; her tabloyu **Tablo adları** "
                    f"kartından ayrıca adlandırabilirsiniz. Hedef şema `{settings.schema}`; "
                    "şema yalnız **Bağlantılar** sayfasında değişir."
                ),
            )
        if collection:
            collection_count_caption(settings, collection)

    options["collection"] = collection
    options["schema"] = settings.schema
    # Normalised here as well as in the plan, so the watermark and table-exists
    # lookups ask about the name the transfer will actually create.
    options["table"] = sql_table_ident(table) if (table or "").strip() else ""
    if not collection:
        return options

    st.write("")
    # Chosen on Şema keşfi; shown here only, so the two pages cannot disagree.
    options["nesting"] = nesting_summary(
        settings, collection, options["table"] or sql_table_ident(collection)
    )

    st.write("")
    options["date_filter"] = _date_card(settings, collection, prefs)

    st.write("")
    with theme.collapsible_card("tr_sync", "Senkron", "Yazma şekli ve profil ayarları."):
        mode = st.radio(
            "Senkron",
            ("Tam senkron", "Artımlı"),
            horizontal=True,
            key=f"tr_sync_{collection}",
            help="Tam: tüm belgeler. Artımlı: SQL tablosundaki son `_id` sonrası yeni kayıtlar.",
        )
        incremental = mode == "Artımlı"
        watermark = None
        watermark_source = None
        restart = False
        point = sql_checkpoint(settings, options["schema"], options["table"])
        unfinished = bool(
            point is not None
            and point.mode == "full"
            and point.status != "completed"
            and point.last_id_json
        )
        if unfinished:
            st.info(
                f"Yarım kalan tam yükleme var: **{format_int(point.docs_done)}** belge işlendi "
                f"({format_int(point.docs_written)} yazıldı, {format_int(point.rejected)} reddedildi), "
                f"durum `{point.status}`. Aynı kırılım ve tarih aralığıyla çalıştırınca kaldığı "
                "yerden devam eder."
            )
            restart = st.checkbox(
                "Baştan başla",
                key=f"tr_restart_{collection}",
                help=(
                    "Kontrol noktasını yok sayar. Tabloları da boşaltın ya da yeniden oluşturun; "
                    "yoksa her parti yazmadan önce siler ve yükleme yavaşlar."
                ),
            )
        if incremental:
            if point is not None and point.last_id_json:
                watermark = {"last_id": _checkpoint_id(point), "updated": str(point.updated_at or "")[:19]}
                watermark_source = "checkpoint"
            else:
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
                where = {"checkpoint": "kontrol noktası", "sql": "SQL tablosu"}.get(
                    watermark_source or "", "kayıtlı işaret"
                )
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
                step=1000,
                key=f"tr_sample_{collection}",
                help=(
                    "Şema için kaç belge taransın. 5000 önerilir. "
                    "0 = koleksiyonun tamamı. Aktarılacak belge sayısını değiştirmez."
                ),
            )
        with row2[1]:
            batch = st.number_input(
                "Yazma partisi",
                min_value=100,
                step=100,
                key=f"tr_batch_{collection}",
                help=(
                    "SQL'e bir seferde kaç belgelik paket yazılsın. "
                    "Hızı ve belleği etkiler; aktarılacak belge sayısını değiştirmez."
                ),
            )
        with row2[2]:
            recreate = st.checkbox(
                "Tabloları yeniden oluştur (DROP + CREATE)",
                key=f"tr_recreate_{collection}",
                disabled=incremental,
                help="Artımlı modda şema durur; yalnızca yeni satırlar yazılır.",
            )
            clear_first = st.checkbox(
                "Yazmadan önce tabloları boşalt",
                key=f"tr_clear_{collection}",
                disabled=incremental,
                help="Hedef tablolardaki mevcut satırlar silinir, sonra aktarım başlar.",
            )
            allow_null = st.checkbox(
                "Anahtar dışındaki kolonlar NULL kabul etsin",
                value=True,
                key=f"tr_null_{collection}",
                help="Örnekle profillenen alanlar başka belgelerde eksik olabilir.",
            )

    options.update(
        {
            "sample": int(sample),
            "batch": int(batch),
            "recreate": False if incremental else recreate,
            "clear_first": False if incremental else clear_first,
            "allow_null": allow_null,
            "mode": "incremental" if incremental else "full",
            "restart": bool(restart),
            "unfinished": unfinished,
            "schedule_mode": prefs.get("schedule_mode") or "auto",
            "watermark": watermark,
            "table_names": dict(prefs.get("table_names") or {}),
        }
    )
    return options


def _checkpoint_id(point) -> str:
    """The checkpoint's `_id` as people read it (ObjectId hex, number, text)."""
    try:
        value = point.last_id
    except Exception:
        return str(point.last_id_json)
    return str(value)


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
            options.get("restart"),
            options.get("allow_null"),
            repr(options.get("date_filter")),
            repr(sorted(columns.get("exclude") or [])),
            repr(sorted(columns.get("exclude_tables") or [])),
            repr(sorted((options.get("table_names") or {}).items())),
        )
    )


def _request_run() -> None:
    """Claim the next rerun for a write, so the button renders disabled while it runs."""
    st.session_state[RUN_REQUEST_KEY] = True
    st.session_state.pop(RUN_DONE_KEY, None)


def _request_stop() -> None:
    transfer_job.request_stop()


def _request_preflight() -> None:
    st.session_state[PREFLIGHT_REQUEST_KEY] = True


def _preflight_if_requested(settings: Settings, options: dict, selected: dict | None) -> None:
    """Run the pre-flight check in this rerun; the report stays until a setting changes."""
    if not st.session_state.pop(PREFLIGHT_REQUEST_KEY, None) or selected is None:
        return
    date_filter = options["date_filter"]
    signature = _run_signature(options)
    source = mongo_client(_mongo_cfg(settings), long_running=True)
    try:
        with st.spinner(f"Ön kontrol: sürücü, yetki, log ayarları ve {format_int(PROBE_DOCS)} belgelik hız ölçümü..."):
            source.connect()
            report = run_preflight(
                source,
                lambda: sql_target(dict(settings.mssql), settings.mssql_password, options["schema"]),
                selected,
                options["collection"],
                date_query=_date_query(date_filter),
                date_field=str(date_filter["field"]) if date_filter.get("enabled") and date_filter.get("field") else None,
            )
        st.session_state[PREFLIGHT_KEY] = {"signature": signature, "report": report}
    except Exception as exc:
        st.session_state[PREFLIGHT_KEY] = {"signature": signature, "error": str(exc)}
    finally:
        source.close()


_LEVEL_ICONS = {"ok": ":material/check_circle:", "info": ":material/info:", "warn": ":material/warning:"}
_RATE_LABELS = (("Okuma", "okuma"), ("Düzleştirme", "düzleştirme"), ("SQL yazma", "SQL yazma"))


def _preflight_view(options: dict) -> None:
    saved = st.session_state.get(PREFLIGHT_KEY)
    if not saved or saved.get("signature") != _run_signature(options):
        return
    if saved.get("error"):
        theme.error_with_detail("Ön kontrol tamamlanamadı.", saved["error"])
        return
    report = saved["report"]
    warnings = sum(1 for finding in report.findings if finding.level == "warn")
    title = "Ön kontrol raporu" + (f" · {warnings} uyarı" if warnings else "")
    with st.expander(title, expanded=True):
        cols = st.columns(4)
        for col, (label, key) in zip(cols, _RATE_LABELS):
            rate = report.rates.get(key)
            col.metric(label, "—" if rate is None else f"{format_int(round(rate))}/sn", border=True)
        cols[3].metric("Tahmini süre", format_duration(report.projection.get("ön okumalı")), border=True)
        facts = []
        if report.rows_per_doc:
            facts.append(f"belge başına {report.rows_per_doc:.1f} satır")
        if report.estimated_docs:
            facts.append(f"~{format_int(report.estimated_docs)} belge")
        serial = report.projection.get("sıralı")
        if serial:
            facts.append(f"ön okuma kapalıyken {format_duration(serial)}")
        if facts:
            st.caption(
                " · ".join(facts)
                + ". Hızlar belge/sn; tahmin kesintisiz çalışma içindir ve gerçek tablolarda biraz uzun sürebilir."
            )
        st.markdown(
            "\n".join(
                f"- {_LEVEL_ICONS.get(finding.level, '')} **{finding.topic}** · {finding.text}"
                for finding in report.findings
            )
        )


def _range_note(date_filter: dict[str, Any]) -> str:
    if not date_filter.get("enabled") or not date_filter.get("field"):
        return ""
    start, end = date_filter.get("start"), date_filter.get("end")
    if not start or not end:
        return f" · {date_filter['field']}"
    return f" · {date_filter['field']} {start:%d.%m.%Y}–{end:%d.%m.%Y}"


def _quote_cmd(arg: str) -> str:
    """Quote a Windows cmd.exe argument when it is not a plain token."""
    if arg and not any(ch.isspace() or ch in '&()[]{}^=;!\'+,`~%' for ch in arg):
        return arg
    return '"' + arg.replace('"', '""') + '"'


def _quote_ps(arg: str) -> str:
    return "'" + arg.replace("'", "''") + "'"


def _scheduler_args(
    collection: str, mode: str, table: str, table_names: dict[str, str]
) -> list[str]:
    args = ["--collection", collection, "--mode", mode]
    if table:
        args += ["--table", table]
    for source, name in sorted((table_names or {}).items()):
        args += ["--rename", f"{source}={name}"]
    return args


def _scheduler_command(
    collection: str,
    mode: str,
    table: str,
    table_names: dict[str, str],
) -> tuple[str, str, str]:
    """Return (one-line command, PowerShell script, batch script)."""
    python = sys.executable
    script = ROOT / "tools" / "run_transfer.py"
    extra = _scheduler_args(collection, mode, table, table_names)
    cmd_args = " ".join(_quote_cmd(part) for part in extra)
    ps_args = " ".join(_quote_ps(part) for part in extra)
    cmdline = f'"{python}" "{script}" {cmd_args}'
    ps1 = (
        "$ErrorActionPreference = 'Stop'\n"
        f"Set-Location -LiteralPath '{ROOT}'\n"
        f"& '{python}' '{script}' {ps_args}\n"
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    )
    bat = (
        "@echo off\n"
        f"cd /d \"{ROOT}\"\n"
        f"\"{python}\" \"{script}\" {cmd_args}\n"
        "exit /b %ERRORLEVEL%\n"
    )
    return cmdline, ps1, bat


def _scheduler_card(options: dict) -> None:
    collection = options.get("collection")
    if not collection:
        return
    mode = options.get("schedule_mode") or "auto"
    schema = options.get("schema") or ""
    table = options.get("table") or ""
    table_names = dict(options.get("table_names") or {})
    cmdline, ps1, bat = _scheduler_command(collection, mode, table, table_names)
    with theme.collapsible_card(
        f"tr_sched_{collection}",
        "Zamanla",
        "Windows Görev Zamanlayıcı bu komutu çalıştırır. "
        "Komut bu koleksiyonun kök tablosuna kilitlenir; başka koleksiyonun adları karışmaz.",
    ):
        st.code(cmdline, language="text")
        targets = table or sql_table_ident(collection)
        extra = ", ".join(f"{path}→{name}" for path, name in sorted(table_names.items()))
        st.caption(
            f"Hedef `{schema}.{targets}`"
            + (f" · {extra}" if extra else "")
            + f". Şema Bağlantılar sayfasındaki `mssql.schema` değeridir. "
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
                key=f"tr_sched_ps1_{collection}",
                width="stretch",
            )
        with row[1]:
            st.download_button(
                ".bat indir",
                bat,
                file_name=f"mongo2sql_{collection}.bat",
                mime="text/plain",
                key=f"tr_sched_bat_{collection}",
                width="stretch",
            )


def _tables_card(settings: Settings, options: dict, plan: dict) -> dict:
    """Rename generated SQL tables. Returns the plan with those names applied."""
    collection = options["collection"]
    overrides = {
        source: name
        for source, name in dict(options.get("table_names") or {}).items()
        if source
    }
    nonce_key = f"tr_tables_nonce_{collection}"
    nonce = int(st.session_state.get(nonce_key) or 0)
    sources = ",".join(child["source"] for child in plan["children"])
    table_count = 1 + len(plan["children"])
    with theme.collapsible_card(
        f"tr_tables_{collection}",
        "Tablo adları",
        f"`{settings.mssql.get('database')}` · `{plan['schema']}` — {table_count} tablo",
    ):
        new_root, new_overrides, problems, changed = table_names_editor(
            plan,
            overrides,
            editor_key=f"tr_tables_grid_{collection}_{plan['root']['table']}_{sources}_{nonce}",
        )
        st.caption(
            "SQL adı kolonunu düzenleyin, ardından **Tablo adlarını kaydet** ile yazın. "
            "Boş bırakılan alt tablolar kök addan türemeye devam eder. "
            "Adlar PascalCase'e çevrilir; büyük/küçük harf farkı ayrı ad sayılmaz. "
            "Ad değiştirmek SQL'deki tabloyu yeniden adlandırmaz: yeni adla yeni bir tablo oluşur, "
            "eski tablo ve satırları olduğu gibi kalır."
        )
        if problems:
            st.error("Bu adlarla kaydedilemez:\n\n" + "\n".join(f"- {problem}" for problem in problems))
        elif changed:
            st.info(
                "Kaydedilmemiş değişiklik var. Aktarım ve **Zamanla** komutu kaydedilmiş adları kullanır."
            )
            if options.get("unfinished"):
                st.warning(
                    "Bu tablolara yarım kalmış bir tam yükleme yazıyor. Alt tablo adını şimdi "
                    "değiştirirseniz kalan belgeler yeni tabloya yazılır, yazılmış satırlar eski "
                    "tabloda kalır. Kök tablonun adı değişirse yükleme yeni tablolarda baştan başlar."
                )
        saved = st.button(
            "Tablo adlarını kaydet",
            type="primary",
            key=f"tr_tables_save_{collection}_{nonce}",
            disabled=bool(problems) or not changed,
            width="stretch",
        )
    if saved:
        # Names for tables this plan does not show (excluded, or an array this
        # sample missed) stay saved.
        _, elsewhere = resolve_table_names(plan, overrides)
        options["table_names"] = {**{key: overrides[key] for key in elsewhere}, **new_overrides}
        if new_root != options["table"]:
            st.session_state[_pending_table_key(collection)] = new_root
            options["table"] = new_root
        st.session_state[nonce_key] = nonce + 1
        _bump_editor()
        _remember_prefs(collection, options)
        st.toast("Tablo adları kaydedildi", icon=":material/check_circle:")
        st.rerun()
    return apply_table_names(plan, overrides)


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
        for kind, cfg in job.connections.items():
            record_health(kind, cfg, True)
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
    # The mode the run really had: a requested incremental pass finishes an
    # unfinished full load first.
    mode_label = "Artımlı" if job.mode == "incremental" else "Tam senkron"
    if job.created:
        st.success("Oluşturulan tablolar: " + ", ".join(job.created))
    if job.existing:
        st.caption("Zaten mevcut: " + ", ".join(job.existing))
    if job.status == "cancelled":
        st.warning(
            "Aktarım durduruldu. Yazılmış partiler SQL'de kaldı; yeniden başlatınca kontrol "
            "noktasından devam eder."
        )
    if job.status == "error":
        theme.error_with_detail(
            "Aktarım yarıda kaldı. Ayrıntı günlükte de var.", job.error or job.message
        )
        return
    if stats is None:
        return
    with theme.collapsible_card(
        "tr_result",
        "Sonuç",
        f"`{options['collection']}` → `{options['schema']}`",
    ):
        cols = st.columns(5)
        cols[0].metric("Mod", mode_label, border=True)
        cols[1].metric("Belge", format_int(stats.written), border=True)
        cols[2].metric("Satır", format_int(stats.total_rows), border=True)
        cols[3].metric("Reddedilen", format_int(stats.rejected), border=True)
        cols[4].metric("Kırpılan", format_int(stats.truncated), border=True)
        if job.note:
            st.caption(f"Başlangıç · {job.note}")
        if stats.first_load:
            st.caption("İlk yükleme: hedef tablolar boştu, parti silmeleri atlandı.")
        st.caption(f"Günlük · `{log_path_display()}`")
        if stats.read_seconds or stats.sql_seconds:
            megabytes = stats.read_bytes / 1_000_000
            speed = f", {megabytes / stats.read_seconds:.2f} MB/sn" if stats.read_seconds else ""
            st.caption(
                f"Süre dağılımı · Mongo okuma {stats.read_seconds:,.0f} sn ({megabytes:,.1f} MB{speed}) · "
                f"düzleştirme {stats.flatten_seconds:,.0f} sn · SQL ekleme {stats.insert_seconds:,.0f} sn · "
                f"commit {stats.commit_seconds:,.0f} sn. Okuma ile yazma aynı anda sürer."
            )
        if stats.last_id:
            st.caption(f"İşaret `_id` = `{stats.last_id}`")
        if stats.widened:
            st.caption("Genişletilen kolonlar · " + ", ".join(f"`{item}`" for item in stats.widened))
        if stats.retries:
            st.caption(f"{format_int(stats.retries)} geçici hata beklenip aşıldı.")
        if stats.slow_tables:
            st.caption(
                "Hızlı yazma yolu çalışmadığı için yavaş yolda yazılan tablolar · "
                + ", ".join(f"`{table}`" for table in stats.slow_tables)
            )
        if stats.rows:
            st.dataframe(
                [{"tablo": table, "satır": count} for table, count in stats.rows.items()],
                hide_index=True,
                width="stretch",
            )
        if stats.rejects_path:
            st.caption(f"Red dosyası · `{stats.rejects_path}`")
            with st.expander("İlk kayıtlar"):
                st.dataframe(read_lines(stats.rejects_path, 20), hide_index=True, width="stretch")
    if stats.documents == 0 and job.mode == "incremental" and job.status == "done":
        st.info("Yeni belge yok; işaret zaten güncel.")
    if stats.documents == 0 and options["date_filter"].get("enabled") and job.status == "done":
        st.info("Seçilen tarih aralığında belge bulunamadı.")
    if stats.rejected:
        st.warning(
            f"{format_int(stats.rejected)} belge SQL'e yazılamadı ve atlandı; iş durmadı. "
            "Her birinin `_id`'si ve nedeni red dosyasında."
        )
    if stats.truncated:
        st.warning(
            f"{format_int(stats.truncated)} değer, kolon genişletilemediği için kırpıldı "
            "(anahtar kolonu ya da ALTER yetkisi yok); red dosyasında listeleniyor."
        )
    if stats.nulled:
        st.caption(
            f"{format_int(stats.nulled)} değer SQL Server'ın kabul etmeyeceği biçimdeydi "
            "(NaN, aralık dışı sayı ya da tarih) ve NULL yazıldı; red dosyasında."
        )


def _run_summary(settings: Settings, options: dict, selected: dict | None) -> str:
    """One line saying what the run button will do."""
    collection = options["collection"]
    if selected is None:
        return f"`{collection}` · plan hazır olunca aktarım açılır."
    target = ".".join(
        part
        for part in (settings.mssql.get("database"), selected["schema"], selected["root"]["table"])
        if part
    )
    nesting = selected.get("nesting") or ""
    parts = [
        f"`{collection}` → `{target}`",
        f"{1 + len(selected['children'])} tablo",
        "Artımlı" if options["mode"] == "incremental" else "Tam senkron",
        nesting_labels().get(nesting, nesting),
    ]
    dropped = []
    if options["columns"]["exclude"]:
        dropped.append(f"{len(options['columns']['exclude'])} kolon")
    if options["columns"]["exclude_tables"]:
        dropped.append(f"{len(options['columns']['exclude_tables'])} alt tablo")
    if dropped:
        parts.append(", ".join(dropped) + " hariç")
    date_note = _range_note(options["date_filter"]).lstrip(" ·")
    if date_note:
        parts.append(date_note)
    return " · ".join(part for part in parts if part)


def _run_card(settings: Settings, options: dict, selected: dict | None) -> None:
    """Summary, run/stop buttons and live progress in one place."""
    job = transfer_job.snapshot()
    ready = bool(settings.sql_ready and selected is not None)
    busy = transfer_job.is_busy()
    finished = st.session_state.get(RUN_DONE_KEY) == _run_signature(options)
    write_label = "Artımlı senkron" if options["mode"] == "incremental" else "Tam senkron"

    with theme.collapsible_card(
        "tr_run", "Çalıştır", _run_summary(settings, options, selected), foldable=False
    ):
        # A load that runs for days belongs in Task Scheduler: here it ends with the UI.
        needs_confirm = False
        estimate = collection_count(settings, options["collection"])
        if estimate and estimate > LARGE_COLLECTION and not busy:
            st.warning(
                f"Bu koleksiyon büyük (~{format_int(estimate)} belge). Uzun işleri **Zamanla** "
                "kartındaki komutla Görev Zamanlayıcı'dan çalıştırın: arayüz kapanırsa buradaki "
                "iş durur. Durursa kontrol noktasından devam eder."
            )
            needs_confirm = not st.checkbox(
                "Yine de burada başlat", key=f"tr_big_ok_{options['collection']}"
            )
        actions = st.columns([1.6, 1.6, 1.4, 2.4], vertical_alignment="center")
        with actions[0]:
            st.button(
                write_label,
                key="tr_do_write",
                type="primary",
                icon=":material/play_arrow:",
                disabled=not ready or needs_confirm or busy or finished,
                on_click=_request_run,
                width="stretch",
                help="Eksik tabloları oluşturur, sonra belgeleri yazar. Sayfa değiştirmek durdurmaz.",
            )
        with actions[1]:
            if busy:
                st.button(
                    "Aktarımı durdur",
                    key="tr_do_stop",
                    icon=":material/stop:",
                    on_click=_request_stop,
                    disabled=job.status != "running",
                    width="stretch",
                    help="Açık yazma partisi bitince durur.",
                )
            elif finished:
                st.button(
                    "Yeniden çalıştır",
                    key="tr_do_again",
                    icon=":material/replay:",
                    on_click=_request_run,
                    disabled=needs_confirm,
                    width="stretch",
                )
        with actions[2]:
            st.button(
                "Ön kontrol",
                key="tr_do_check",
                icon=":material/fact_check:",
                on_click=_request_preflight,
                disabled=not ready or busy,
                width="stretch",
                help=f"Hiçbir şey yazmadan sürücüyü, yetkileri ve log ayarlarını denetler; "
                f"{format_int(PROBE_DOCS)} belgeyle hızı ölçüp toplam süreyi tahmin eder.",
            )
        with actions[3]:
            if busy:
                st.caption("Aktarım arka planda sürüyor. Sayfa değiştirmek durdurmaz.")
            elif finished:
                st.caption("Bu ayarlarla aktarım tamamlandı. Bir ayarı değiştirin ya da yeniden çalıştırın.")
            elif not ready:
                st.caption("Koleksiyon, kök tablo ve SQL bağlantısı tamamlanınca aktarım açılır.")
            elif needs_confirm:
                st.caption("Burada başlatmak için yukarıdaki kutuyu işaretleyin.")

        if not busy:
            _preflight_if_requested(settings, options, selected)
            _preflight_view(options)

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
        st.caption(
            f"Günlük `{log_path_display()}` — başlangıç, ilerleme ve bitiş bu dosyaya yazılır."
        )


def render(settings: Settings) -> None:
    theme.page_header(
        "SQL aktarımı",
        "Tam senkron tüm koleksiyonu yazar. Artımlı, SQL tablosundaki son `_id` "
        "sonrası yeni belgeleri ekler; eski kayıtlardaki güncelleme için tam senkron gerekir.",
        step="transfer",
    )

    collections, error, warning = collection_list(settings)
    if error:
        theme.error_with_detail(error.summary, error.detail)
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
            theme.error_with_detail("Koleksiyon profillenemedi.", str(exc))

    if options["collection"]:
        st.write("")
        _, options["columns"] = _columns_card(
            settings, options, _date_query(options["date_filter"])
        )

    selected = None
    if plan is not None:
        selected = apply_column_selection(
            plan, options["columns"]["exclude"], options["columns"]["exclude_tables"]
        )
        st.write("")
        selected = _tables_card(settings, options, selected)
        st.session_state[PLAN_KEY] = selected
    else:
        st.session_state.pop(PLAN_KEY, None)

    if options["collection"]:
        _remember_prefs(options["collection"], options)

    job = transfer_job.snapshot()
    _consume_finished_job(job)

    if options["collection"]:
        st.write("")
        _run_card(settings, options, selected)

    _launch_if_requested(settings, options, selected)

    if not transfer_job.is_busy() and job.collection == (options.get("collection") or "") and (
        job.status in {"done", "error", "cancelled"}
    ):
        st.write("")
        _render_result(options, job)

    if options.get("collection"):
        st.write("")
        _scheduler_card(options)

    if PLAN_KEY in st.session_state:
        with st.expander("Üretilen DDL", expanded=False):
            st.code(render_database_ddl([st.session_state[PLAN_KEY]]), language="sql")
