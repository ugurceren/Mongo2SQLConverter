"""Mongo2SQLConverter — Streamlit application shell."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui import connections, discovery, theme, transfer  # noqa: E402
from app.ui.services import (  # noqa: E402
    SQL_AUTH_LABELS,
    SQL_SESSION_PASSWORD,
    Settings,
    load_state,
    mongo_endpoint,
    mongo_identity,
    mongo_uri_option,
)
from core.mssql import AUTH_NEEDS_PASSWORD, AUTH_NEEDS_USERNAME  # noqa: E402
from core.settings import LOCAL_CONFIG_PATH  # noqa: E402
from core.logutil import configure_logging  # noqa: E402

configure_logging()

st.set_page_config(
    page_title="Mongo2SQL Dönüştürücü",
    page_icon="◧",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.inject_css()
theme.theme_toggle()  # top bar: brand + sidebar toggle aligned to sidebar edge

SETTINGS: Settings = load_state()


ENCRYPT_SHORT = {"default": "sürücü varsayılanı", "yes": "açık (Encrypt=yes)", "no": "kapalı"}


def _driver_short(driver: str | None) -> str:
    """`ODBC Driver 17 for SQL Server` -> `ODBC 17` so it fits the rail."""
    match = re.search(r"ODBC Driver (\d+)", driver or "")
    return f"ODBC {match.group(1)}" if match else (driver or "—")


def _password_place(saved: str | None, session: bool) -> str:
    """Where a password lives, never the password itself."""
    if not saved:
        return "girilmedi"
    if session:
        return "yalnızca bu oturumda"
    return "bu makinede kayıtlı"


def _mongo_rows(settings: Settings) -> list[str]:
    if settings.mongo_ready:
        head = theme.status_row("MongoDB", settings.mongo.get("database") or "—", "ok")
    else:
        head = theme.status_row("MongoDB", "yapılandırılmadı", "warn")
    if not (settings.mongo.get("uri") or settings.mongo.get("database")):
        return [head]

    uri = settings.mongo.get("uri")
    rows: list[tuple[str, str]] = [
        ("Sunucu", mongo_endpoint(uri)),
        ("Veritabanı", settings.mongo.get("database") or "—"),
        ("Kullanıcı", mongo_identity(settings.mongo)),
        ("Şifre", _password_place(settings.mongo_password, False)),
    ]
    auth_source = mongo_uri_option(uri, "authSource")
    if auth_source:
        rows.append(("Auth source", auth_source))
    replica = mongo_uri_option(uri, "replicaSet")
    if replica:
        rows.append(("Replica set", replica))
    return [head, theme.status_detail(rows)]


def _sql_rows(settings: Settings) -> list[str]:
    mssql = settings.mssql
    if settings.sql_ready:
        head = theme.status_row(
            "SQL Server", f"{mssql.get('database')} / {settings.schema}", "ok"
        )
    elif settings.sql_needs_password:
        head = theme.status_row("SQL Server", "şifre bekliyor", "warn")
    elif mssql.get("server") or mssql.get("database"):
        head = theme.status_row("SQL Server", "eksik", "warn")
    else:
        head = theme.status_row("SQL Server", "yalnızca aktarım için", "off")
    if not (mssql.get("server") or mssql.get("database")):
        return [head]

    auth = settings.sql_auth
    if auth in AUTH_NEEDS_USERNAME:
        user = mssql.get("username") or "—"
    else:
        user = "bu Windows oturumu"
    rows = [
        ("Sunucu", mssql.get("server") or "—"),
        ("Veritabanı", mssql.get("database") or "—"),
        ("Şema", settings.schema),
        ("Yöntem", SQL_AUTH_LABELS.get(auth, auth)),
        ("Kullanıcı", user),
        ("Sürücü", _driver_short(mssql.get("driver"))),
        (
            "Şifreleme",
            ENCRYPT_SHORT.get(str(mssql.get("encrypt") or "default"), "sürücü varsayılanı"),
        ),
        (
            "Sertifika",
            "doğrulanmıyor" if mssql.get("trust_certificate") else "doğrulanıyor",
        ),
    ]
    if auth in AUTH_NEEDS_PASSWORD:
        rows.append(
            (
                "Şifre",
                _password_place(
                    settings.mssql_password,
                    bool(st.session_state.get(SQL_SESSION_PASSWORD)),
                ),
            )
        )
    return [head, theme.status_detail(rows)]


def _sidebar_status(settings: Settings) -> None:
    theme.sidebar_panel(
        [
            theme.sidebar_block("Kaynak", _mongo_rows(settings)),
            theme.sidebar_block("Hedef", _sql_rows(settings), show_in_rail=False),
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

_NAV_KEY = {
    "connections": "nav_conn",
    "discovery": "nav_schema",
    "transfer": "nav_sql",
}


def _current_nav_key(page: object) -> str:
    path = (getattr(page, "url_path", None) or "").strip("/")
    if path in _NAV_KEY:
        return _NAV_KEY[path]
    title = getattr(page, "title", "") or ""
    if title == "Şema keşfi":
        return "nav_schema"
    if title == "SQL aktarımı":
        return "nav_sql"
    return "nav_conn"


navigation = st.navigation(
    [page_links, page_schema, page_sql],
    position="hidden",
)

with st.sidebar:
    theme.nav_menu(
        [
            ("nav_conn", page_links, ":material/settings_ethernet:", "Bağlantılar"),
            ("nav_schema", page_schema, ":material/schema:", "Şema keşfi"),
            ("nav_sql", page_sql, ":material/moving:", "SQL aktarımı"),
        ],
        current_key=_current_nav_key(navigation),
    )
    _sidebar_status(SETTINGS)

navigation.run()
