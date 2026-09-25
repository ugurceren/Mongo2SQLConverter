"""MSSQL connection helpers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

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

    def connect(
        self, *, login_timeout: int | None = None, query_timeout: int | None = None
    ) -> pyodbc.Connection:
        """
        Open the connection. A transfer passes both timeouts so a hung server
        or half-open TCP link turns into an error it can retry, not a job that
        waits forever; interactive checks keep the driver defaults.
        """
        if self.auth in AUTH_NEEDS_PASSWORD and not self.password:
            raise RuntimeError(
                "Seçili kimlik doğrulama modu şifre ister; Bağlantılar sayfasından girin."
            )
        kwargs: dict[str, Any] = {"autocommit": False}
        if login_timeout:
            kwargs["timeout"] = login_timeout
        with self._as_windows_user():
            self._conn = pyodbc.connect(self.connection_string(), **kwargs)
        if query_timeout:
            # Must be set before cursors are created; applies to every statement.
            self._conn.timeout = query_timeout
        return self._conn

    def prepare_session(self, lock_timeout_ms: int = 120_000) -> None:
        """
        Session rules for a long load: any error aborts the whole transaction
        (so a batch is all-or-nothing), and a blocked lock fails after a while
        instead of waiting forever.
        """
        cur = self.conn.cursor()
        cur.execute(f"SET XACT_ABORT ON; SET LOCK_TIMEOUT {int(lock_timeout_ms)};")
        self.conn.commit()

    def scalar(self, sql: str, *params: Any) -> Any:
        """First column of the first row of a batch that may start with non-queries."""
        cur = self.conn.cursor()
        cur.execute(sql, *params)
        while cur.description is None:
            if not cur.nextset():
                return None
        row = cur.fetchone()
        return None if row is None else row[0]

    def get_applock(self, resource: str, timeout_ms: int = 0) -> bool:
        """
        Take a session-level exclusive lock on `resource`.

        One writer per target table: the UI and a scheduled task cannot load
        the same tables at once. The lock dies with the session, so a crashed
        job never leaves it behind.
        """
        result = self.scalar(
            "DECLARE @r int; "
            "EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = ?; "
            "SELECT @r;",
            resource,
            int(timeout_ms),
        )
        self.conn.commit()
        return result is not None and int(result) >= 0

    def release_applock(self, resource: str) -> None:
        try:
            cur = self.conn.cursor()
            cur.execute("EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session';", resource)
            self.conn.commit()
        except Exception:
            self.rollback()

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

    def column_names(self, schema: str, table: str) -> set[str]:
        """Physical column names on an existing table. Missing tables: empty set."""
        if not self.table_exists(schema, table):
            return set()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT c.name "
            "FROM sys.columns c "
            "JOIN sys.tables tb ON tb.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = tb.schema_id "
            "WHERE s.name = ? AND tb.name = ?",
            schema,
            table,
        )
        return {str(row[0]) for row in cur.fetchall()}

    def add_column(
        self,
        schema: str,
        table: str,
        name: str,
        sql_type: str,
        *,
        nullable: bool = True,
    ) -> None:
        null = "NULL" if nullable else "NOT NULL"
        self.execute(
            f"ALTER TABLE [{schema}].[{table}] ADD [{name}] {sql_type} {null}"
        )

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

    def row_count(self, schema: str, table: str) -> int | None:
        """Rows from metadata (no scan). None when the table does not exist."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT SUM(p.rows) FROM sys.partitions p "
            "WHERE p.object_id = OBJECT_ID(?, N'U') AND p.index_id IN (0, 1)",
            f"[{schema}].[{table}]",
        )
        row = cur.fetchone()
        self.conn.commit()
        return None if row is None or row[0] is None else int(row[0])

    def has_rows(self, schema: str, table: str) -> bool:
        if not self.table_exists(schema, table):
            return False
        cur = self.conn.cursor()
        cur.execute(f"SELECT TOP (1) 1 FROM [{schema}].[{table}]")
        found = cur.fetchone() is not None
        self.conn.commit()
        return found

    def key_exists(self, schema: str, table: str, key_column: str, key_type: str, key: Any) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT TOP (1) 1 FROM [{schema}].[{table}] WHERE [{key_column}] = CAST(? AS {key_type})",
            key,
        )
        found = cur.fetchone() is not None
        self.conn.commit()
        return found

    def column_info(self, schema: str, table: str) -> dict[str, dict[str, Any]]:
        """
        Live column facts: SQL type text, nullability, collation, and whether
        the column may be widened (not part of an index, key or computation).
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable, "
            "c.collation_name, c.is_computed, "
            "CASE WHEN EXISTS (SELECT 1 FROM sys.index_columns ic WHERE ic.object_id = c.object_id "
            "AND ic.column_id = c.column_id) THEN 1 ELSE 0 END, "
            "CASE WHEN EXISTS (SELECT 1 FROM sys.foreign_key_columns f WHERE "
            "(f.parent_object_id = c.object_id AND f.parent_column_id = c.column_id) OR "
            "(f.referenced_object_id = c.object_id AND f.referenced_column_id = c.column_id)) "
            "THEN 1 ELSE 0 END "
            "FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id "
            "WHERE c.object_id = OBJECT_ID(?, N'U')",
            f"[{schema}].[{table}]",
        )
        out: dict[str, dict[str, Any]] = {}
        for name, type_name, max_length, precision, scale, nullable, collation, computed, indexed, keyed in cur.fetchall():
            kind = str(type_name).lower()
            length = int(max_length)
            if kind in {"nvarchar", "nchar"}:
                sql_type = f"{kind.upper()}({'MAX' if length < 0 else length // 2})"
            elif kind in {"varchar", "char", "varbinary", "binary"}:
                sql_type = f"{kind.upper()}({'MAX' if length < 0 else length})"
            elif kind in {"decimal", "numeric"}:
                sql_type = f"DECIMAL({int(precision)}, {int(scale)})"
            elif kind in {"datetime2", "datetimeoffset", "time"}:
                sql_type = f"{kind.upper()}({int(scale)})"
            else:
                sql_type = kind.upper()
            out[str(name)] = {
                "sql_type": sql_type,
                "nullable": bool(nullable),
                "collation": collation,
                "widenable": not (computed or indexed or keyed),
            }
        self.conn.commit()
        return out

    def widen_column(
        self,
        schema: str,
        table: str,
        column: str,
        sql_type: str,
        *,
        nullable: bool,
        collation: str | None,
    ) -> None:
        """
        Make a text column wider. Metadata-only for NVARCHAR(n) → (m), but it
        takes a schema-modification lock. Nullability and collation are restated:
        ALTER COLUMN resets both when they are left out.
        """
        collate = f" COLLATE {collation}" if collation else ""
        null = "NULL" if nullable else "NOT NULL"
        cur = self.conn.cursor()
        cur.execute(f"ALTER TABLE [{schema}].[{table}] ALTER COLUMN [{column}] {sql_type}{collate} {null}")
        self.conn.commit()

    def referencing_keys(self, schema: str, table: str) -> list[dict[str, Any]]:
        """Foreign keys that point at `table`, with enough detail to re-create them."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT fk.name, OBJECT_SCHEMA_NAME(fk.parent_object_id), OBJECT_NAME(fk.parent_object_id), "
            "fk.delete_referential_action_desc, fk.update_referential_action_desc, "
            "(SELECT COUNT(*) FROM sys.foreign_key_columns x WHERE x.constraint_object_id = fk.object_id), "
            "COL_NAME(fkc.parent_object_id, fkc.parent_column_id), "
            "COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) "
            "FROM sys.foreign_keys fk "
            "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "WHERE fk.referenced_object_id = OBJECT_ID(?, N'U')",
            f"[{schema}].[{table}]",
        )
        keys = [
            {
                "name": row[0],
                "schema": row[1],
                "table": row[2],
                "on_delete": str(row[3] or "NO_ACTION").replace("_", " "),
                "on_update": str(row[4] or "NO_ACTION").replace("_", " "),
                "columns": int(row[5]),
                "column": row[6],
                "referenced": row[7],
            }
            for row in cur.fetchall()
        ]
        self.conn.commit()
        return keys

    def truncate_tables(self, schema: str, root: str, children: Sequence[str]) -> str:
        """
        Empty the plan's tables fast and in one transaction.

        TRUNCATE is not allowed on a table other tables reference, so the
        children's foreign keys are dropped, every table truncated, and the
        keys re-created with the same names and rules. A crash rolls all of it
        back. Returns "truncate", or "delete" when that is not possible (a key
        from outside the plan, no ALTER permission) and batched deletes ran.
        """
        wanted = {name.lower() for name in children}
        keys = self.referencing_keys(schema, root) if self.table_exists(schema, root) else []
        if all(
            key["schema"] == schema and key["table"].lower() in wanted and key["columns"] == 1
            for key in keys
        ):
            try:
                cur = self.conn.cursor()
                for key in keys:
                    cur.execute(f"ALTER TABLE [{schema}].[{key['table']}] DROP CONSTRAINT [{key['name']}]")
                for name in children:
                    if self.table_exists(schema, name):
                        cur.execute(f"TRUNCATE TABLE [{schema}].[{name}]")
                if self.table_exists(schema, root):
                    cur.execute(f"TRUNCATE TABLE [{schema}].[{root}]")
                for key in keys:
                    cur.execute(
                        f"ALTER TABLE [{schema}].[{key['table']}] WITH CHECK ADD CONSTRAINT [{key['name']}] "
                        f"FOREIGN KEY ([{key['column']}]) REFERENCES [{schema}].[{root}] ([{key['referenced']}]) "
                        f"ON DELETE {key['on_delete']} ON UPDATE {key['on_update']}"
                    )
                self.conn.commit()
                return "truncate"
            except Exception:
                self.rollback()
        for name in [*children, root]:
            self.delete_all(schema, name)
        return "delete"

    def delete_all(self, schema: str, table: str, chunk: int = 50_000) -> None:
        """Empty a table in committed slices so the transaction log stays small."""
        if not self.table_exists(schema, table):
            return
        cur = self.conn.cursor()
        while True:
            cur.execute(f"DELETE TOP ({int(chunk)}) FROM [{schema}].[{table}]")
            removed = cur.rowcount
            self.conn.commit()
            if removed < chunk:
                return

    def non_cascading_children(self, schema: str, root: str, children: Sequence[str]) -> list[str]:
        """Child tables whose rows would not follow a root DELETE on their own."""
        cascades = {
            key["table"].lower()
            for key in (self.referencing_keys(schema, root) if self.table_exists(schema, root) else [])
            if key["on_delete"].upper() == "CASCADE"
        }
        return [name for name in children if name.lower() not in cascades and self.table_exists(schema, name)]

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

    def delete_keys(
        self,
        schema: str,
        table: str,
        key_column: str,
        keys: Sequence[Any],
        chunk: int = 500,
        key_type: str | None = None,
    ) -> None:
        """
        Remove rows by key so a re-run replaces them (child rows cascade).

        With `key_type` each parameter is cast to the column's own type: an
        NVARCHAR parameter against a CHAR(24) key can force a scan under SQL_*
        collations, once per DELETE.
        """
        if not keys:
            return
        cur = self.conn.cursor()
        mark = f"CAST(? AS {key_type})" if key_type else "?"
        for start in range(0, len(keys), chunk):
            part = keys[start : start + chunk]
            marks = ", ".join(mark for _ in part)
            cur.execute(
                f"DELETE FROM [{schema}].[{table}] WHERE [{key_column}] IN ({marks})",
                *part,
            )

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


def installed_drivers() -> list[str]:
    try:
        return [d for d in pyodbc.drivers() if "SQL Server" in d]
    except Exception:
        return []


def available_drivers() -> list[str]:
    """Installed drivers first, so a config without `driver` picks one that exists."""
    found = installed_drivers()
    ordered: list[str] = []
    for name in (*(d for d in KNOWN_SQL_DRIVERS if d in found), *found, *KNOWN_SQL_DRIVERS):
        if name not in ordered:
            ordered.append(name)
    return ordered
