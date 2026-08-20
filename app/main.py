"""Mongo2SQLConverter — Streamlit application shell."""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui import connections, discovery, theme, transfer  # noqa: E402
from app.ui.services import Settings, load_state  # noqa: E402
from core.settings import LOCAL_CONFIG_PATH  # noqa: E402

st.set_page_config(
    page_title="Mongo2SQL Dönüştürücü",
    page_icon="◧",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.inject_css()

SETTINGS: Settings = load_state()


def _sidebar_status(settings: Settings) -> None:
    if settings.mongo_ready:
        mongo = theme.status_row("MongoDB", settings.mongo.get("database") or "—", "ok")
    else:
        mongo = theme.status_row("MongoDB", "yapılandırılmadı", "warn")
    if settings.sql_ready:
        sql = theme.status_row(
            "SQL Server", f"{settings.mssql.get('database')} · {settings.schema}", "ok"
        )
    else:
        sql = theme.status_row("SQL Server", "yalnızca aktarım için", "off")

    theme.sidebar_panel(
        [
            theme.sidebar_block("Bağlantılar", [mongo, sql]),
            theme.sidebar_foot(f"Ayarlar · {LOCAL_CONFIG_PATH.name}"),
        ]
    )


def page_discovery() -> None:
    discovery.render(SETTINGS)


def page_transfer() -> None:
    transfer.render(SETTINGS)


def page_connections() -> None:
    connections.render(SETTINGS)


page_schema = st.Page(
    page_discovery,
    title="Şema keşfi",
    icon=":material/schema:",
    url_path="discovery",
)
page_sql = st.Page(
    page_transfer,
    title="SQL aktarımı",
    icon=":material/moving:",
    url_path="transfer",
)
page_links = st.Page(
    page_connections,
    title="Bağlantılar",
    icon=":material/settings_ethernet:",
    url_path="connections",
    default=True,
)
theme.register_pages(
    {
        "discovery": page_schema,
        "transfer": page_sql,
        "connections": page_links,
    }
)

navigation = st.navigation(
    [page_links, page_schema, page_sql],
    position="hidden",
)

with st.sidebar:
    theme.brand()
    theme.nav_menu(
        [
            ("nav_conn", page_links, ":material/settings_ethernet:", "Bağlantılar"),
            ("nav_schema", page_schema, ":material/schema:", "Şema keşfi"),
            ("nav_sql", page_sql, ":material/moving:", "SQL aktarımı"),
        ]
    )
    theme.theme_toggle()
    _sidebar_status(SETTINGS)

navigation.run()
