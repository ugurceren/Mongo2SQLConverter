"""MSSQL connection helpers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

import pyodbc

# How the connection proves who it is. Kept as a mode rather than a
# trusted/untrusted flag because the cases need different connection string
# parts, and one of them needs work before pyodbc is even called.
AUTH_WINDOWS = "windows"
AUTH_SQL = "sql"
AUTH_WINDOWS_USER = "windows_user"

AUTH_MODES = (AUTH_WINDOWS, AUTH_SQL, AUTH_WINDOWS_USER)
# Modes that cannot connect without a password in hand.
AUTH_NEEDS_PASSWORD = (AUTH_SQL, AUTH_WINDOWS_USER)
# Modes where the account is typed in rather than taken from the process.
AUTH_NEEDS_USERNAME = (AUTH_SQL, AUTH_WINDOWS_USER)


def auth_mode(cfg: dict[str, Any]) -> str:
    """Read the mode from a config dict, falling back to the old boolean."""
    raw = str(cfg.get("auth") or "").strip()
    if raw in AUTH_MODES:
        return raw
    return AUTH_WINDOWS if cfg.get("trusted_connection", True) else AUTH_SQL


def split_windows_user(raw: str | None) -> tuple[str, str]:
    """Split `DOMAIN\\user` or `user@domain` into (user, domain)."""
    text = (raw or "").strip()
    if "\\" in text:
        domain, _, user = text.partition("\\")
        return user.strip(), domain.strip()
    if "@" in text:
        user, _, domain = text.partition("@")
        return user.strip(), domain.strip()
    return text, ""


def _escape_odbc_value(value: str) -> str:
    """A braced ODBC value ends at the first `}`, so a literal one is doubled."""
    return value.replace("}", "}}")


@dataclass
class SqlAccess:
    """What the connected login is and whether it can do what a transfer needs."""

    login: str
    database: str
    user: str
    is_owner: bool
    roles: tuple[str, ...]
    can_create_table: bool | None
    can_create_schema: bool | None
    schema: str
    schema_exists: bool
    can_insert: bool | None
    can_select: bool | None

    @property
    def can_write(self) -> bool:
        if self.is_owner:
            return True
        if self.can_insert is not None:
            return self.can_insert
        return "db_datawriter" in self.roles

    @property
    def can_create(self) -> bool:
        if self.is_owner:
            return True
        needed = self.can_create_table
        if not self.schema_exists:
            needed = bool(needed) and bool(self.can_create_schema)
        return bool(needed)


class MssqlConnection:
    def __init__(
        self,
        server: str,
        database: str,
        schema: str = "dbo",
        driver: str = "ODBC Driver 17 for SQL Server",
        trusted_connection: bool = True,
        username: str | None = None,
        password: str | None = None,
        auth: str | None = None,
        encrypt: bool | None = None,
        trust_certificate: bool = False,
    ):
        self.server = server
        self.database = database
        self.schema = schema
        self.driver = driver
        self.trusted_connection = trusted_connection
        self.username = username
        self.password = password
        self.auth = auth if auth in AUTH_MODES else (
            AUTH_WINDOWS if trusted_connection else AUTH_SQL
        )
        self.encrypt = encrypt
        self.trust_certificate = trust_certificate
        self._conn: pyodbc.Connection | None = None

    # ----------------------------------------------------------------------
    # authentication
    # ----------------------------------------------------------------------

    def connection_string(self, redact: bool = False) -> str:
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
        ]
        if self.auth in (AUTH_WINDOWS, AUTH_WINDOWS_USER):
            # The impersonation in `_as_windows_user` decides *whose* token this is.
            parts.append("Trusted_Connection=yes")
        else:
            secret = "***" if redact else _escape_odbc_value(self.password or "")
            parts.append(f"UID={self.username or ''}")
            parts.append(f"PWD={{{secret}}}")

        # Left to the user because it matters for driver 18, which encrypts
        # and verifies the certificate by default.
        if self.encrypt is not None:
            parts.append(f"Encrypt={'yes' if self.encrypt else 'no'}")
        if self.trust_certificate:
            parts.append("TrustServerCertificate=yes")
        return ";".join(parts)

    @contextmanager
    def _as_windows_user(self) -> Iterator[None]:
        """
        Borrow another Windows account for the duration of the connect call.

        ODBC has no way to pass domain credentials: `Trusted_Connection=yes`
        always uses the process token. Logging the account on with
        NEW_CREDENTIALS swaps only the identity used for network access, which
        is exactly what reaching SQL Server over the wire needs.
        """
        if self.auth != AUTH_WINDOWS_USER:
            yield
            return

        try:
            import win32con
            import win32security
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise RuntimeError(
                "Windows (başka hesap) modu için pywin32 gerekir: pip install pywin32"
            ) from exc

        user, domain = split_windows_user(self.username)
        if not user:
            raise RuntimeError("Kullanıcı adı boş. Örnek: DOMAIN\\servis_hesabi")
        if not self.password:
            raise RuntimeError(
                "Bu mod şifre ister. Bağlantılar sayfasından hesabın şifresini girin."
            )

        try:
            token = win32security.LogonUser(
                user,
                domain or None,
                self.password,
                win32con.LOGON32_LOGON_NEW_CREDENTIALS,
                win32con.LOGON32_PROVIDER_WINNT50,
            )
        except Exception as exc:
            raise RuntimeError(f"Windows oturumu açılamadı ({user}): {exc}") from exc

        win32security.ImpersonateLoggedOnUser(token)
        try:
            yield
        finally:
            # The connection is authenticated by now, so dropping the identity
            # here does not affect queries that run later.
            win32security.RevertToSelf()
            token.Close()

    def connect(self) -> pyodbc.Connection:
        if self.auth in AUTH_NEEDS_PASSWORD and not self.password:
            raise RuntimeError(
                "Seçili kimlik doğrulama modu şifre ister; Bağlantılar sayfasından girin."
            )
        with self._as_windows_user():
            self._conn = pyodbc.connect(self.connection_string(), autocommit=False)
        return self._conn

    @property
    def conn(self) -> pyodbc.Connection:
        if self._conn is None:
            return self.connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def execute_script(self, sql: str) -> None:
        cur = self.conn.cursor()
        batch: list[str] = []
        for line in sql.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            batch.append(line)
            if stripped.endswith(";"):
                cur.execute("\n".join(batch).rstrip(";"))
                batch.clear()
        if batch:
            cur.execute("\n".join(batch))
        self.conn.commit()

    def test(self) -> SqlAccess:
        """
        Who am I and may I write here?

        A transfer creates the schema and tables, then inserts and deletes, so
        a connection that merely succeeds is not enough: reporting the missing
        right here beats failing halfway through a load.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT SUSER_SNAME(), DB_NAME(), USER_NAME(), "
            "ISNULL(IS_ROLEMEMBER('db_owner'), 0), "
            "ISNULL(IS_ROLEMEMBER('db_ddladmin'), 0), "
            "ISNULL(IS_ROLEMEMBER('db_datawriter'), 0), "
            "ISNULL(IS_ROLEMEMBER('db_datareader'), 0), "
            "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE'), "
            "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE SCHEMA'), "
            "CASE WHEN SCHEMA_ID(?) IS NULL THEN 0 ELSE 1 END",
            self.schema,
        )
        row = cur.fetchone()

        roles = [
            name
            for name, flag in (
                ("db_owner", row[3]),
                ("db_ddladmin", row[4]),
                ("db_datawriter", row[5]),
                ("db_datareader", row[6]),
            )
            if flag
        ]
        schema_exists = bool(row[9])

        insert_ok: bool | None = None
        select_ok: bool | None = None
        if schema_exists:
            # Asked separately: on a schema that does not exist yet the
            # permission call has nothing to answer about.
            cur.execute(
                "SELECT HAS_PERMS_BY_NAME(QUOTENAME(?), 'SCHEMA', 'INSERT'), "
                "HAS_PERMS_BY_NAME(QUOTENAME(?), 'SCHEMA', 'SELECT')",
                self.schema,
                self.schema,
            )
            perms = cur.fetchone()
            insert_ok = None if perms[0] is None else bool(perms[0])
            select_ok = None if perms[1] is None else bool(perms[1])

        return SqlAccess(
            login=str(row[0]),
            database=str(row[1]),
            user=str(row[2]),
            is_owner=bool(row[3]),
            roles=tuple(roles),
            can_create_table=None if row[7] is None else bool(row[7]),
            can_create_schema=None if row[8] is None else bool(row[8]),
            schema=self.schema,
            schema_exists=schema_exists,
            can_insert=insert_ok,
            can_select=select_ok,
        )

    # ----------------------------------------------------------------------
    # schema / table management
    # ----------------------------------------------------------------------

    def ensure_schema(self, schema: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = ?) "
            "EXEC(N'CREATE SCHEMA [' + ? + N']')",
            schema,
            schema,
        )
        self.conn.commit()

    def column_char_widths(self, schema: str, table: str) -> dict[str, int | None]:
        """NVARCHAR/CHAR declared length. None means MAX (no clip). Missing tables: {}."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT c.name, t.name, c.max_length "
            "FROM sys.columns c "
            "JOIN sys.tables tb ON tb.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = tb.schema_id "
            "JOIN sys.types t ON t.user_type_id = c.user_type_id "
            "WHERE s.name = ? AND tb.name = ?",
            schema,
            table,
        )
        out: dict[str, int | None] = {}
        for name, type_name, max_length in cur.fetchall():
            kind = str(type_name).lower()
            length = int(max_length)
            if kind in {"nvarchar", "nchar"}:
                out[str(name)] = None if length < 0 else length // 2
            elif kind in {"varchar", "char"}:
                out[str(name)] = None if length < 0 else length
        return out

    def table_exists(self, schema: str, table: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = ? AND t.name = ?",
            schema,
            table,
        )
        return cur.fetchone() is not None

    def max_key(
        self, schema: str, table: str, column: str = "mongo_id"
    ) -> tuple[bool, Any]:
        """Return (table_exists, MAX(column)). Value is None when empty or column missing."""
        if not self.table_exists(schema, table):
            return False, None
        cur = self.conn.cursor()
        try:
            cur.execute(f"SELECT MAX([{column}]) FROM [{schema}].[{table}]")
        except Exception:
            self.rollback()
            return True, None
        row = cur.fetchone()
        if not row or row[0] is None:
            return True, None
        return True, row[0]

    def drop_table(self, schema: str, table: str) -> None:
        cur = self.conn.cursor()
        cur.execute(f"IF OBJECT_ID(N'[{schema}].[{table}]', N'U') IS NOT NULL DROP TABLE [{schema}].[{table}]")
        self.conn.commit()

    def execute(self, sql: str) -> None:
        cur = self.conn.cursor()
        cur.execute(sql)
        self.conn.commit()

    # ----------------------------------------------------------------------
    # data movement
    # ----------------------------------------------------------------------

    def clear_table(self, schema: str, table: str) -> None:
        cur = self.conn.cursor()
        cur.execute(f"DELETE FROM [{schema}].[{table}]")
        self.conn.commit()

    def delete_keys(
        self, schema: str, table: str, key_column: str, keys: Sequence[Any], chunk: int = 500
    ) -> None:
        """Remove rows by key so a re-run replaces them (child rows cascade)."""
        if not keys:
            return
        cur = self.conn.cursor()
        for start in range(0, len(keys), chunk):
            part = keys[start : start + chunk]
            marks = ", ".join("?" for _ in part)
            cur.execute(
                f"DELETE FROM [{schema}].[{table}] WHERE [{key_column}] IN ({marks})",
                *part,
            )

    def insert_rows(
        self, schema: str, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
    ) -> int:
        batch = list(rows)
        if not batch:
            return 0
        cols = ", ".join(f"[{name}]" for name in columns)
        marks = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO [{schema}].[{table}] ({cols}) VALUES ({marks})"

        cur = self.conn.cursor()
        try:
            cur.fast_executemany = True
            cur.executemany(sql, batch)
        except Exception:
            # fast_executemany rejects some MAX / mixed-width parameter sets;
            # a plain executemany still gets the batch in.
            cur = self.conn.cursor()
            cur.executemany(sql, batch)
        return len(batch)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        if self._conn is not None:
            self._conn.rollback()


KNOWN_SQL_DRIVERS = (
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 18 for SQL Server",
    "SQL Server",
)


def available_drivers() -> list[str]:
    found: list[str] = []
    try:
        found = [d for d in pyodbc.drivers() if "SQL Server" in d]
    except Exception:
        found = []
    ordered: list[str] = []
    for name in (*KNOWN_SQL_DRIVERS, *found):
        if name not in ordered:
            ordered.append(name)
    return ordered
