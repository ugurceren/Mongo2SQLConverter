"""Connections page: MongoDB source and, optionally, the SQL Server target."""

from __future__ import annotations

from importlib.util import find_spec

import streamlit as st

from app.ui import theme
from app.ui.services import (
    SQL_AUTH_LABELS,
    SQL_SESSION_PASSWORD,
    Settings,
    invalidate_collections,
    mongo_cfg_from_form,
    mongo_client,
    sql_target,
)
from core.mssql import (
    AUTH_NEEDS_PASSWORD,
    AUTH_NEEDS_USERNAME,
    AUTH_SQL,
    AUTH_WINDOWS,
    AUTH_WINDOWS_USER,
    MssqlConnection,
    available_drivers,
)
from core.settings import save_connection_overrides


SAVE_FLASH = "conn_saved"

# Suggestions only — never written to config.yaml.
HINT_MONGO_URI = "mongodb://localhost:27017"
HINT_MONGO_DB = "mydb"
HINT_MONGO_USER = "kullanıcı"
HINT_SQL_SERVER = "localhost"
HINT_SQL_DB = "MyWarehouse"
HINT_SQL_SCHEMA = "dbo"
HINT_SQL_USER = "sa"
HINT_WIN_USER = "DOMAIN\\servis_hesabi"

SQL_AUTH_ORDER = (AUTH_WINDOWS, AUTH_SQL, AUTH_WINDOWS_USER)

ENCRYPT_LABELS = {
    "default": "Sürücü varsayılanı",
    "yes": "Açık (Encrypt=yes)",
    "no": "Kapalı (Encrypt=no)",
}
ENCRYPT_ORDER = ("default", "yes", "no")

# Example-file values treated as hints, not filled-in data.
_EXAMPLE_MONGO_URI = (HINT_MONGO_URI, "mongodb://host:27017/?authSource=admin", "mongodb://host:27017")
_EXAMPLE_MONGO_DB = (HINT_MONGO_DB,)
_EXAMPLE_MONGO_USER = (HINT_MONGO_USER, "user")
_EXAMPLE_SQL_SERVER = (HINT_SQL_SERVER, r"srv\INSTANCE")
_EXAMPLE_SQL_DB = (HINT_SQL_DB,)
_EXAMPLE_SQL_SCHEMA = (HINT_SQL_SCHEMA,)
_EXAMPLE_SQL_USER = (HINT_SQL_USER, "user", HINT_WIN_USER)


def _hint_field(saved: str | None, hint: str, examples: tuple[str, ...]) -> tuple[str, str]:
    """Show saved text only when it is a real value; examples stay as placeholders."""
    raw = (saved or "").strip()
    if not raw or raw in examples:
        return "", hint
    return raw, hint


def _or_saved(typed: str, saved: str | None) -> str:
    return (typed or "").strip() or (saved or "")


def _clear_example_widget(key: str, examples: tuple[str, ...]) -> None:
    current = st.session_state.get(key)
    if isinstance(current, str) and current.strip() in examples:
        st.session_state[key] = ""


def _hint_input(
    label: str,
    key: str,
    saved: str | None,
    hint: str,
    examples: tuple[str, ...],
    **kwargs,
) -> str:
    shown, placeholder = _hint_field(saved, hint, examples)
    if key not in st.session_state:
        st.session_state[key] = shown
    else:
        _clear_example_widget(key, examples)
    return st.text_input(label, placeholder=placeholder, key=key, **kwargs)


def _mark_saved(label: str) -> None:
    st.session_state[SAVE_FLASH] = label
    st.toast(f"{label} kaydedildi", icon="✅")


def _show_saved() -> None:
    label = st.session_state.pop(SAVE_FLASH, None)
    if not label:
        return
    st.success(f"**{label}** ayarları kaydedildi.")


def _test_mongo(mongo_cfg: dict) -> None:
    mongo = mongo_client(mongo_cfg)
    try:
        info = mongo.test()
        collections = info["collections"]
        st.success(
            f"Bağlantı kuruldu — `{info['database']}` ({len(collections)} koleksiyon)"
        )
        if collections:
            preview = ", ".join(collections[:12])
            extra = f" … +{len(collections) - 12}" if len(collections) > 12 else ""
            st.caption(f"Koleksiyonlar: {preview}{extra}")
        else:
            st.warning(
                f"`{info['database']}` içinde koleksiyon yok. Veritabanı adı yanlış olabilir."
            )
        if info["databases"]:
            st.caption("Erişilebilir veritabanları: " + ", ".join(info["databases"]))
    except Exception as exc:
        st.error(f"Bağlantı kurulamadı: {exc}")
    finally:
        mongo.close()


def _user_help(auth: str) -> str:
    if auth == AUTH_SQL:
        return "SQL Server'da tanımlı login adı. Domain öneki ya da köşeli parantez yazmayın."
    if auth == AUTH_WINDOWS_USER:
        return "DOMAIN\\hesap ya da hesap@domain. Bağlantı anında bu hesap taklit edilir."
    return "Windows oturumu kullanıldığı için kullanıcı adı gerekmez."


def _password_placeholder(auth: str, has_password: bool) -> str:
    if auth not in AUTH_NEEDS_PASSWORD:
        return "Windows oturumu"
    if has_password:
        return "kayıtlı — değiştirmek için yazın"
    return "hesabın şifresi"


def _store_password_default(auth: str, settings: Settings) -> bool:
    # Already stored on disk: keep storing, the user chose that before.
    if settings.mssql.get("password"):
        return True
    return auth == AUTH_SQL


def _auth_note(auth: str) -> str:
    if auth == AUTH_WINDOWS:
        return (
            "Uygulamayı çalıştıran Windows hesabıyla bağlanır. Başka bir hesap "
            "istiyorsanız yöntemi değiştirin ya da uygulamayı o hesapla başlatın."
        )
    if auth == AUTH_SQL:
        return "SQL Server kimlik doğrulaması: login adı ve şifre sunucuya gönderilir."
    return (
        "Girilen domain hesabı yalnız bağlantı kurulurken taklit edilir; "
        "şifre SQL Server'a gönderilmez, Windows'a doğrulatılır."
    )


def _test_sql(target: MssqlConnection) -> None:
    try:
        target.connect()
        access = target.test()
    except Exception as exc:
        st.error(f"Bağlantı kurulamadı: {exc}")
        return
    finally:
        target.close()

    st.success(f"Bağlantı kuruldu — {access.database} / oturum: {access.login}")
    roles = ", ".join(access.roles) if access.roles else "rol üyeliği yok"
    schema_note = (
        f"`{access.schema}` şeması var" if access.schema_exists else f"`{access.schema}` şeması yok"
    )
    st.caption(f"Veritabanı kullanıcısı: `{access.user}` · {roles} · {schema_note}")

    if not access.can_write:
        st.warning(
            f"Bu hesap `{access.schema}` şemasına yazamıyor. Aktarım INSERT ve DELETE "
            f"çalıştırır; `db_datawriter` (ya da şema üzerinde INSERT/DELETE) gerekir."
        )
    if not access.can_create:
        missing = "CREATE TABLE" if access.schema_exists else "CREATE SCHEMA + CREATE TABLE"
        st.warning(
            f"Bu hesap tablo oluşturamıyor ({missing} yok). Aktarım eksik tabloları kendi "
            f"oluşturduğu için başarısız olur; `db_ddladmin` verin ya da tabloları elle oluşturun."
        )
    if access.can_write and access.can_create:
        st.caption("Yetki yeterli: şema ve tablo oluşturabilir, veri yazabilir.")


def _mongo_card(settings: Settings) -> None:
    with st.container(border=True):
        theme.section_heading(
            "Kaynak",
            "MongoDB",
            "Şema keşfi ve aktarım için okunan veritabanı. Zorunlu. "
            "Gri yazı örnektir; Kaydet'e basınca config.local.yaml yazar.",
        )
        with st.form("mongo_conn"):
            left, right = st.columns(2)
            with left:
                uri = _hint_input(
                    "Bağlantı URI",
                    "mongo_uri",
                    settings.mongo.get("uri"),
                    HINT_MONGO_URI,
                    _EXAMPLE_MONGO_URI,
                )
                database = _hint_input(
                    "Veritabanı",
                    "mongo_db",
                    settings.mongo.get("database"),
                    HINT_MONGO_DB,
                    _EXAMPLE_MONGO_DB,
                )
            with right:
                user = _hint_input(
                    "Kullanıcı",
                    "mongo_user",
                    settings.mongo.get("username"),
                    HINT_MONGO_USER,
                    _EXAMPLE_MONGO_USER,
                )
                password = st.text_input(
                    "Şifre",
                    type="password",
                    placeholder="kayıtlı — değiştirmek için yazın"
                    if settings.mongo_password
                    else "boş bırakın veya yazın",
                    help="Boş bırakılırsa kayıtlı şifre korunur. Öneri gösterilmez.",
                )
            actions = st.columns([1, 1, 2])
            with actions[0]:
                do_test = st.form_submit_button(
                    "Bağlantıyı dene", key="mongo_test", width="stretch"
                )
            with actions[1]:
                do_save = st.form_submit_button(
                    "Kaydet", type="primary", width="stretch"
                )

        if do_test:
            _test_mongo(
                mongo_cfg_from_form(
                    _or_saved(uri, settings.mongo.get("uri")),
                    _or_saved(database, settings.mongo.get("database")),
                    _or_saved(user, settings.mongo.get("username")),
                    password or settings.mongo_password,
                )
            )
        if do_save:
            save_connection_overrides(
                mongodb={
                    "uri": _or_saved(uri, settings.mongo.get("uri")),
                    "database": _or_saved(database, settings.mongo.get("database")),
                    "username": _or_saved(user, settings.mongo.get("username")),
                    "password": password or None,
                },
                mssql={},
            )
            _mark_saved("MongoDB")
            invalidate_collections()
            st.rerun()


def _sql_card(settings: Settings) -> None:
    with st.container(border=True):
        theme.section_heading(
            "Hedef",
            "SQL Server",
            "Yalnızca veri aktarırken gerekir. Şema keşfi bu bağlantıyı kullanmaz. "
            "Gri yazı örnektir; gerçek değerleri siz yazın.",
        )
        if "sql_auth_mode" not in st.session_state:
            st.session_state["sql_auth_mode"] = settings.sql_auth

        left, right = st.columns(2)
        with left:
            st.markdown("**Yazılacak yer**")
            server = _hint_input(
                "Sunucu",
                "sql_server",
                settings.mssql.get("server"),
                HINT_SQL_SERVER,
                _EXAMPLE_SQL_SERVER,
            )
            database = _hint_input(
                "Veritabanı",
                "sql_db",
                settings.mssql.get("database"),
                HINT_SQL_DB,
                _EXAMPLE_SQL_DB,
            )
            schema = _hint_input(
                "Şema",
                "sql_schema",
                settings.mssql.get("schema"),
                HINT_SQL_SCHEMA,
                _EXAMPLE_SQL_SCHEMA,
            )
        with right:
            st.markdown("**Kimlik doğrulama**")
            auth = st.selectbox(
                "Yöntem",
                SQL_AUTH_ORDER,
                format_func=lambda mode: SQL_AUTH_LABELS[mode],
                key="sql_auth_mode",
                help=(
                    "Yazma yetkisi olan hesabı burada seçersiniz. Windows modları "
                    "Trusted_Connection, diğerleri kullanıcı adı + şifre kullanır."
                ),
            )
            drivers = available_drivers()
            current = settings.mssql.get("driver") or "ODBC Driver 17 for SQL Server"
            if current not in drivers:
                drivers = [current, *drivers]
            driver = st.selectbox(
                "ODBC sürücü",
                drivers,
                index=drivers.index(current),
                help="Listede kurulu olmayan sürücüler de görünür; kurulu olanı seçin.",
            )

            needs_user = auth in AUTH_NEEDS_USERNAME
            needs_password = auth in AUTH_NEEDS_PASSWORD
            user_hint = HINT_WIN_USER if auth == AUTH_WINDOWS_USER else HINT_SQL_USER
            user = _hint_input(
                "Kullanıcı",
                "sql_user",
                settings.mssql.get("username") if needs_user else None,
                user_hint,
                _EXAMPLE_SQL_USER,
                disabled=not needs_user,
                help=_user_help(auth),
            )
            password = st.text_input(
                "Şifre",
                type="password",
                disabled=not needs_password,
                placeholder=_password_placeholder(auth, bool(settings.mssql_password)),
            )
            store_password = st.checkbox(
                "Şifreyi bu makinede sakla",
                value=_store_password_default(auth, settings),
                key=f"sql_store_pw_{auth}",
                disabled=not needs_password,
                help=(
                    "Kapalıyken şifre config.local.yaml'a yazılmaz, yalnız bu oturumda "
                    "tutulur. Domain hesapları için kapalı tutmanız önerilir."
                ),
            )
            st.caption(_auth_note(auth))
            if auth == AUTH_WINDOWS_USER and find_spec("win32security") is None:
                st.warning("Bu mod `pywin32` ister: `pip install pywin32`", icon=":material/download:")

        st.write("")
        transport = st.columns([1.4, 1.6, 2.0], vertical_alignment="bottom")
        with transport[0]:
            encrypt_default = str(settings.mssql.get("encrypt") or "default")
            if encrypt_default not in ENCRYPT_ORDER:
                encrypt_default = "default"
            encrypt_choice = st.selectbox(
                "Şifreleme",
                ENCRYPT_ORDER,
                index=ENCRYPT_ORDER.index(encrypt_default),
                format_func=lambda mode: ENCRYPT_LABELS[mode],
                help="Driver 18 varsayılan olarak şifreler ve sertifikayı doğrular.",
            )
        with transport[1]:
            trust_certificate = st.checkbox(
                "Sunucu sertifikasına doğrulamadan güven",
                value=bool(settings.mssql.get("trust_certificate", False)),
                key="sql_trust_cert",
                help="TrustServerCertificate=yes — kurum CA'sı olmayan iç sunucular için.",
            )
        with transport[2]:
            if "18" in driver and encrypt_choice != "no" and not trust_certificate:
                st.caption(
                    "Driver 18 + self-signed sertifika, sertifika zinciri hatası verir. "
                    "Sertifikaya güvenin ya da Driver 17 seçin."
                )

        actions = st.columns([1, 1, 2])
        with actions[0]:
            do_test = st.button("Bağlantıyı dene", key="sql_test", width="stretch")
        with actions[1]:
            do_save = st.button("Kaydet", type="primary", key="sql_save", width="stretch")

        payload: dict = {
            "server": _or_saved(server, settings.mssql.get("server")),
            "database": _or_saved(database, settings.mssql.get("database")),
            "schema": _or_saved(schema, settings.mssql.get("schema")),
            "driver": driver,
            "auth": auth,
            # Kept in step with the mode so an older config stays readable.
            "trusted_connection": auth in (AUTH_WINDOWS, AUTH_WINDOWS_USER),
            "username": _or_saved(user, settings.mssql.get("username")) if needs_user else "",
            "encrypt": encrypt_choice,
            "trust_certificate": trust_certificate,
        }
        effective_password = (password or settings.mssql_password) if needs_password else None

        if do_test:
            _test_sql(sql_target(payload, effective_password))
        if do_save:
            saved = dict(payload)
            if needs_password and store_password:
                # None means "leave what is on disk", so an empty box keeps it.
                saved["password"] = effective_password or None
                st.session_state.pop(SQL_SESSION_PASSWORD, None)
            elif needs_password:
                saved["password"] = ""
                if effective_password:
                    st.session_state[SQL_SESSION_PASSWORD] = effective_password
                else:
                    st.session_state.pop(SQL_SESSION_PASSWORD, None)
            else:
                saved["password"] = ""
                st.session_state.pop(SQL_SESSION_PASSWORD, None)
            save_connection_overrides(mongodb={}, mssql=saved)
            _mark_saved("SQL Server")
            st.rerun()


def render(settings: Settings) -> None:
    theme.page_header(
        "Yapılandırma",
        "Bağlantılar",
        "Kaynak ve hedef ayrı tutulur: Mongo olmadan hiçbir şey çalışmaz, SQL yalnızca "
        "veri yazarken devreye girer. Kaydet, değerleri bu makinede tutar; git'e yazılmaz.",
        step="connections",
    )
    _show_saved()
    _mongo_card(settings)
    st.write("")
    _sql_card(settings)
    if settings.mongo_ready:
        theme.next_step(
            "discovery",
            "Koleksiyonları ölçüp DRDL ve MSSQL şeması önerisi çıkarın. "
            "Belge okur, hiçbir yere yazmaz; SQL bağlantısı gerekmez.",
        )
