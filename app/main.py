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
    page_title="Mongo2SQL Converter",
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
        mongo = theme.status_row("MongoDB", "yapilandirilmadi", "warn")
    if settings.sql_ready:
        sql = theme.status_row(
            "SQL Server", f"{settings.mssql.get('database')} · {settings.schema}", "ok"
        )
    else:
        sql = theme.status_row("SQL Server", "yalnizca aktarim icin", "off")

    theme.sidebar_panel(
        [
            theme.sidebar_block("Baglantilar", [mongo, sql]),
            theme.sidebar_block(
                "Profil",
                [
                    theme.status_row("Headroom", f"×{settings.headroom:g}"),
                    theme.status_row("Map esigi", f"{settings.map_min_keys} anahtar"),
                ],
                show_in_rail=False,
            ),
            theme.sidebar_foot(f"Ayarlar · {LOCAL_CONFIG_PATH.name}"),
        ]
    )


def page_discovery() -> None:
    discovery.render(SETTINGS)


def page_transfer() -> None:
    transfer.render(SETTINGS)


def page_connections() -> None:
    connections.render(SETTINGS)


with st.sidebar:
    theme.brand()

navigation = st.navigation(
    [
        st.Page(
            page_discovery,
            title="Sema kesfi",
            icon=":material/schema:",
            url_path="discovery",
            default=True,
        ),
        st.Page(
            page_transfer,
            title="SQL aktarimi",
            icon=":material/moving:",
            url_path="transfer",
        ),
        st.Page(
            page_connections,
            title="Baglantilar",
            icon=":material/settings_ethernet:",
            url_path="connections",
        ),
    ]
)

with st.sidebar:
    _sidebar_status(SETTINGS)

navigation.run()
