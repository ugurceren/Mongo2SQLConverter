"""Mongo2SQLConverter — Streamlit application shell."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui import connections, discovery, theme, transfer, transfer_job  # noqa: E402
from app.ui.services import (  # noqa: E402
    PLAN_CACHE_KEY,
    SQL_AUTH_LABELS,
    SQL_SESSION_PASSWORD,
    Settings,
    connection_health,
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
    page_icon=":material/swap_horiz:",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.inject_css()
theme.theme_toggle()  # top bar: brand, breadcrumb and the light/dark switch

SETTINGS: Settings = load_state()


ENCRYPT_SHORT = {"default": "varsayılan şifreleme", "yes": "şifreli", "no": "şifresiz"}


def _driver_short(driver: str | None) -> str:
    """`ODBC Driver 17 for SQL Server` -> `ODBC 17` so it fits the rail."""
    match = re.search(r"ODBC Driver (\d+)", driver or "")
    return f"ODBC {match.group(1)}" if match else (driver or "—")


def _security_short(mssql: dict) -> str:
    encrypt = ENCRYPT_SHORT.get(str(mssql.get("encrypt") or "default"), ENCRYPT_SHORT["default"])
    cert = "sertifikaya güvenilir" if mssql.get("trust_certificate") else "sertifika doğrulanır"
    return f"{encrypt} · {cert}"


def _password_place(saved: str | None, session: bool) -> str:
    """Where a password lives, never the password itself."""
    if not saved:
        return "girilmedi"
    if session:
        return "yalnızca bu oturumda"
    return "bu makinede kayıtlı"


def _health_row(label: str, value: str, ok: bool | None) -> str:
    """Green only after this session reached the server; saved but untested is neutral."""
    if ok is True:
        return theme.status_row(label, value, "ok")
    if ok is False:
        return theme.status_row(label, f"{value} · bağlanamadı", "err")
    return theme.status_row(label, f"{value} · test edilmedi", "saved")


def _mongo_rows(settings: Settings) -> list[str]:
    mongo = settings.mongo
    if settings.mongo_ready:
        head = _health_row(
            "MongoDB", mongo.get("database") or "—", connection_health("mongo", mongo)
        )
    else:
        head = theme.status_row("MongoDB", "yapılandırılmadı", "warn")
    if not (mongo.get("uri") or mongo.get("database")):
        return [head]

    uri = mongo.get("uri")
    rows: list[tuple[str, str]] = [("Sunucu", mongo_endpoint(uri))]
    if not settings.mongo_ready:
        rows.append(("Veritabanı", mongo.get("database") or "—"))
    rows += [
        ("Kullanıcı", mongo_identity(mongo)),
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
        head = _health_row(
            "SQL Server",
            f"{mssql.get('database')} / {settings.schema}",
            connection_health("sql", mssql),
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
    rows: list[tuple[str, str]] = [("Sunucu", mssql.get("server") or "—")]
    if not settings.sql_ready:
        rows.append(("Veritabanı", f"{mssql.get('database') or '—'} / {settings.schema}"))
    rows.append(("Yöntem", SQL_AUTH_LABELS.get(auth, auth)))
    if auth in AUTH_NEEDS_USERNAME:
        rows.append(("Kullanıcı", mssql.get("username") or "—"))
    rows += [
        ("Sürücü", _driver_short(mssql.get("driver"))),
        ("Güvenlik", _security_short(mssql)),
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


def _sidebar_blocks(settings: Settings) -> list[str]:
    return [
        theme.sidebar_block("Kaynak", _mongo_rows(settings)),
        theme.sidebar_block("Hedef", _sql_rows(settings)),
        theme.sidebar_foot(f"Ayarlar · {LOCAL_CONFIG_PATH.name}"),
    ]


def _steps_done(settings: Settings) -> set[str]:
    """Steps this session really finished, not just the ones left of the current page."""
    done: set[str] = set()
    if settings.mongo_ready and connection_health("mongo", settings.mongo) is True:
        done.add("connections")
    if "drdl" in st.session_state or st.session_state.get(PLAN_CACHE_KEY):
        done.add("discovery")
    if transfer_job.snapshot().status == "done":
        done.add("transfer")
    return done


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


def _current_page(page: object) -> str:
    path = (getattr(page, "url_path", None) or "").strip("/")
    if path in ("connections", "discovery", "transfer"):
        return path
    title = getattr(page, "title", "") or ""
    if title == "Şema keşfi":
        return "discovery"
    if title == "SQL aktarımı":
        return "transfer"
    return "connections"


navigation = st.navigation(
    [page_links, page_schema, page_sql],
    position="hidden",
)
current = _current_page(navigation)

with st.sidebar:
    theme.nav_menu(
        [
            ("connections", page_links, ":material/settings_ethernet:", "Bağlantılar"),
            ("discovery", page_schema, ":material/schema:", "Şema keşfi"),
            ("transfer", page_sql, ":material/moving:", "SQL aktarımı"),
        ],
        current=current,
    )
    # The transfer page shows its own progress next to the run button.
    if current != "transfer":
        transfer.render_sidebar_job()
    status_slot = st.empty()
    blocks = _sidebar_blocks(SETTINGS)
    theme.sidebar_panel(blocks, slot=status_slot)

st.session_state[theme.STEPS_DONE_KEY] = _steps_done(SETTINGS)
theme.forget_stepper()
navigation.run()

# The page may have just reached a server or finished a step; redraw what shows that.
st.session_state[theme.STEPS_DONE_KEY] = _steps_done(SETTINGS)
theme.refresh_stepper()
fresh = _sidebar_blocks(SETTINGS)
if fresh != blocks:
    theme.sidebar_panel(fresh, slot=status_slot)
