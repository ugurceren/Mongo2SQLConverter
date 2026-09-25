"""Theme gallery: every widget the app uses, with static data and no database.

Run from the project root so `.streamlit/config.toml` applies:

    python -m streamlit run tools/ui_gallery.py --server.port 8512

It uses the app's page paths, so the theme toggle and breadcrumb behave as in the app.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui import theme  # noqa: E402

st.set_page_config(
    page_title="Mongo2SQL — galeri",
    page_icon=":material/palette:",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.inject_css()
theme.theme_toggle()

COLUMN_ROWS = [
    {"aktar": True, "tablo": "Orders", "kolon": "createdAt", "path": "createdAt", "tip": "DATETIME2", "doluluk": "%100,0"},
    {"aktar": True, "tablo": "Orders", "kolon": "status", "path": "status", "tip": "NVARCHAR(20)", "doluluk": "%98,4"},
    {"aktar": False, "tablo": "OrdersItems", "kolon": "sku", "path": "items[].sku", "tip": "NVARCHAR(40)", "doluluk": "%87,1"},
    {"aktar": True, "tablo": "OrdersItems", "kolon": "qty", "path": "items[].qty", "tip": "INT", "doluluk": "%87,1"},
]
RESULT_ROWS = [{"tablo": "Orders", "satır": 12345}, {"tablo": "OrdersItems", "satır": 40210}]
DDL = (
    "CREATE TABLE [dbo].[Orders] (\n"
    "    [mongo_id] NVARCHAR(24) NOT NULL PRIMARY KEY,\n"
    "    [createdAt] DATETIME2 NULL,\n"
    "    [status] NVARCHAR(20) NULL\n"
    ");"
)
DRDL = "schema:\n- db: shop\n  tables:\n  - table: Orders\n    collection: orders\n    columns:\n    - Name: status\n      MongoType: string\n"


def _inputs_card() -> None:
    with theme.collapsible_card(
        "gal_inputs",
        "Alanlar",
        "Aktif ve pasif alanlar, seçim kutuları ve işaretler. Örnek `kod` ve **kalın** metin.",
        kicker="Form",
    ):
        left, right = st.columns(2)
        with left:
            st.text_input("Sunucu", placeholder="localhost", key="gal_server")
            st.text_input("Pasif alan", placeholder="sa", disabled=True, key="gal_disabled")
            st.number_input("Örnek", value=5000, step=1000, key="gal_sample")
            st.date_input("Başlangıç", value=date(2026, 1, 1), format="DD.MM.YYYY", key="gal_date")
        with right:
            st.selectbox(
                "Yöntem",
                ["Windows — bu oturum", "SQL Server hesabı"],
                key="gal_select",
                help="Yardım metni.",
            )
            st.selectbox("Pasif seçim", ["Koleksiyon yok"], disabled=True, key="gal_select_off")
            st.multiselect("Kolonlar", ["createdAt", "status", "total"], default=["status"], key="gal_multi")
            st.checkbox("Şifreyi bu makinede sakla", value=True, key="gal_check")
            st.radio("Senkron", ["Tam senkron", "Artımlı"], horizontal=True, key="gal_radio")
        buttons = st.columns([1, 1, 2])
        buttons[0].button("Bağlantıyı dene", key="mongo_test", width="stretch")
        buttons[1].button("Kaydet", type="primary", key="gal_save", width="stretch")


def _data_card() -> None:
    with theme.collapsible_card("gal_data", "Tablolar", "Canvas'a çizilen ızgaralar yerel temadan boyanır."):
        with st.container(key="tr_cols_grid"):
            st.data_editor(COLUMN_ROWS, hide_index=True, width="stretch", key="gal_editor")
        st.dataframe(RESULT_ROWS, hide_index=True, width="stretch")
        cols = st.columns(4)
        cols[0].metric("Mod", "Tam senkron", border=True)
        cols[1].metric("Belge", "12.345", border=True)
        cols[2].metric("Satır", "52.555", border=True)
        cols[3].metric("Kırpılan", "0", border=True)


def _code_card() -> None:
    with theme.collapsible_card("gal_code", "Kod ve sekmeler", "SQL, YAML ve JSON çıktıları."):
        ddl_tab, drdl_tab, plan_tab = st.tabs(["MSSQL DDL", "DRDL", "Plan (JSON)"])
        with ddl_tab:
            st.download_button("DDL indir", DDL, "orders.sql", key="gal_ddl")
            st.code(DDL, language="sql")
        with drdl_tab:
            st.code(DRDL, language="yaml")
        with plan_tab:
            st.json({"root": {"table": "Orders", "columns": 3}, "children": []}, expanded=True)
        with st.expander("Üretilen DDL"):
            st.code(DDL, language="sql")


def _feedback_card() -> None:
    with theme.collapsible_card("gal_feedback", "Geri bildirim", "Uyarılar ve ilerleme."):
        st.success("Bağlantı kuruldu — `shop` (4 koleksiyon)")
        st.info("Yeni belge yok; işaret zaten güncel.")
        st.warning("Hiçbir tarih alanında range index yok.")
        theme.error_with_detail(
            "Sunucuya ulaşılamadı. Adresi ve ağ erişimini kontrol edin.",
            "host:27017: [Errno 11001] getaddrinfo failed (configured timeouts: "
            "socketTimeoutMS: 20000.0ms, connectTimeoutMS: 20000.0ms), Timeout: 15.0s",
        )
        st.progress(0.42)
        st.caption("Tam senkron · `orders` · 5.185 / 12.345 belge")


def _run_card() -> None:
    with theme.collapsible_card(
        "gal_nesting",
        "Bu koleksiyon nasıl kırılsın?",
        "Bu koleksiyonda kök dizi: `items`, `tags`; 2 iç içe nesne.",
    ):
        cols = st.columns(2)
        for col, (key, title, selected) in zip(
            cols, (("hybrid", "Hibrit", True), ("deep", "Derin ilişkisel", False))
        ):
            with col:
                st.markdown('<div class="m2s-nest-count">2 tablo</div>', unsafe_allow_html=True)
                with st.container(border=True, key=f"nest_{'on' if selected else 'off'}_gal_{key}"):
                    st.markdown(f"**{title}**")
                    st.markdown(
                        '<div class="m2s-table-preview">Orders\nOrdersItems</div>',
                        unsafe_allow_html=True,
                    )
                    st.button(
                        "Seçildi" if selected else "Bunu kullan",
                        type="primary" if selected else "secondary",
                        key=f"gal_nest_{key}",
                        width="stretch",
                    )
    st.write("")
    with theme.collapsible_card(
        "gal_run",
        "Çalıştır",
        "`orders` → `MyWarehouse.dbo.Orders` · 2 tablo · Tam senkron · Hibrit · 1 kolon hariç",
        foldable=False,
    ):
        actions = st.columns([1.6, 1.6, 2.8], vertical_alignment="center")
        actions[0].button("Tam senkron", type="primary", icon=":material/play_arrow:", key="gal_run_go", width="stretch")
        actions[1].button("Aktarımı durdur", icon=":material/stop:", key="gal_run_stop", width="stretch")
        actions[2].caption("Aktarım arka planda sürüyor. Sayfa değiştirmek durdurmaz.")
        st.progress(0.42)
        st.caption("Tam senkron · `orders` · 5.185 / 12.345 belge")
        st.caption("Günlük `logs\\mongo2sql_2026-09-26.log` — başlangıç, ilerleme ve bitiş bu dosyaya yazılır.")


def _page(step: str, title: str) -> None:
    theme.page_header(title, "Tema galerisi: uygulamadaki bileşenler, veritabanı olmadan. `kod` örneği.", step=step)
    _inputs_card()
    st.write("")
    _data_card()
    st.write("")
    _code_card()
    st.write("")
    _feedback_card()
    st.write("")
    _run_card()
    upcoming = {"connections": "discovery", "discovery": "transfer"}.get(step)
    if upcoming:
        theme.next_step(upcoming, "Sıradaki sayfanın bağlantısı; `kod` içeren ipucu.")


page_connections = st.Page(
    lambda: _page("connections", "Bağlantılar"),
    title="Bağlantılar",
    icon=":material/settings_ethernet:",
    url_path="connections",
    default=True,
)
page_discovery = st.Page(
    lambda: _page("discovery", "Şema keşfi"),
    title="Şema keşfi",
    icon=":material/schema:",
    url_path="discovery",
)
page_transfer = st.Page(
    lambda: _page("transfer", "SQL aktarımı"),
    title="SQL aktarımı",
    icon=":material/moving:",
    url_path="transfer",
)
theme.register_pages(
    {"connections": page_connections, "discovery": page_discovery, "transfer": page_transfer}
)
navigation = st.navigation([page_connections, page_discovery, page_transfer], position="hidden")
current = (getattr(navigation, "url_path", "") or "").strip("/") or "connections"
st.session_state[theme.STEPS_DONE_KEY] = {"connections"} if current != "connections" else set()

with st.sidebar:
    theme.nav_menu(
        [
            ("connections", page_connections, ":material/settings_ethernet:", "Bağlantılar"),
            ("discovery", page_discovery, ":material/schema:", "Şema keşfi"),
            ("transfer", page_transfer, ":material/moving:", "SQL aktarımı"),
        ],
        current=current,
    )
    with st.container(key="m2s_job"):
        st.progress(0.42)
        st.caption("Tam senkron · `orders` · 5.185 / 12.345 belge")
        st.button("Aktarımı durdur", key="gal_stop", width="stretch")
    theme.sidebar_panel(
        [
            theme.sidebar_block(
                "Kaynak",
                [
                    theme.status_row("MongoDB", "shop", "ok"),
                    theme.status_detail([("Sunucu", "db01:27017"), ("Kullanıcı", "reader"), ("Şifre", "bu makinede kayıtlı")]),
                ],
            ),
            theme.sidebar_block(
                "Hedef",
                [
                    theme.status_row("SQL Server", "şifre bekliyor", "warn"),
                    theme.status_detail([("Sunucu", r"sql01\DWH"), ("Yöntem", "SQL Server hesabı")]),
                ],
            ),
            theme.sidebar_block(
                "Diğer durumlar",
                [
                    theme.status_row("Bağlanamadı", "db01:27017", "err"),
                    theme.status_row("Kayıtlı", "test edilmedi", "saved"),
                    theme.status_row("Kapalı", "yalnızca aktarım için", "off"),
                ],
            ),
            theme.sidebar_foot("Galeri · statik veri"),
        ]
    )

theme.forget_stepper()
navigation.run()
theme.refresh_stepper()
