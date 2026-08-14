"""MSSQL connection helpers."""

from __future__ import annotations

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


def available_drivers() -> list[str]:
    try:
        found = [d for d in pyodbc.drivers() if "SQL Server" in d]
        return found or ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]
    except Exception:
        return ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]
