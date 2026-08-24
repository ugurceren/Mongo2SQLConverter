"""MSSQL connection helpers."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import pyodbc


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
    ):
        self.server = server
        self.database = database
        self.schema = schema
        self.driver = driver
        self.trusted_connection = trusted_connection
        self.username = username
        self.password = password
        self._conn: pyodbc.Connection | None = None

    def connect(self) -> pyodbc.Connection:
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
        ]
        if self.trusted_connection:
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={self.username or ''}")
            parts.append(f"PWD={{{self.password or ''}}}")
        self._conn = pyodbc.connect(";".join(parts), autocommit=False)
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

    def test(self) -> tuple[str, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT SUSER_SNAME(), DB_NAME()")
        row = cur.fetchone()
        return str(row[0]), str(row[1])

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
