"""
Pre-flight report for a big transfer, before the first batch is written.

Collects what will slow a long load down or stop it (old ODBC driver, FULL
recovery without log backups, missing permissions, no index on the date
field) and measures real throughput on a small sample: read, flatten, and a
write into temporary tables that is rolled back. From those rates it projects
the run time with and without the reading thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

import pyodbc

from core.convert import Flattener, kind_of
from core.inspect import key_column_type
from core.mongo import MongoClientWrapper
from core.mssql import MssqlConnection, installed_drivers
from core.reader import ChunkedReader, projection_for
from core.retry import describe
from core.transfer import plan_column_specs
from core.writer import rows_per_call

PROBE_DOCS = 5000
SLOW_LINK_MBPS = 5.0  # a LAN moves tens of MB/s; below this the link is the limit


def payload_bytes(rows: list[list[Any]]) -> int:
    """
    Rough bytes SQL Server receives for `rows` sent as RPC parameters: each
    value travels with its type description, NULLs included.
    """
    total = 0
    for row in rows:
        for value in row:
            if value is None:
                total += 9
            elif isinstance(value, str):
                total += 12 + 2 * len(value)
            elif isinstance(value, (bytes, bytearray)):
                total += 12 + len(value)
            elif isinstance(value, Decimal):
                total += 23
            else:  # bit, int, float, dates
                total += 13
    return total


@dataclass
class Finding:
    level: str  # "ok" | "info" | "warn"
    topic: str
    text: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    rates: dict[str, float | None] = field(default_factory=dict)  # docs/s per stage
    rows_per_doc: float | None = None
    bytes_per_doc: float | None = None
    estimated_docs: int | None = None
    projection: dict[str, float | None] = field(default_factory=dict)  # seconds

    def add(self, level: str, topic: str, text: str) -> None:
        self.findings.append(Finding(level, topic, text))

    def lines(self) -> list[str]:
        marks = {"ok": "✓", "info": "·", "warn": "!"}
        out = [f"{marks.get(item.level, '·')} [{item.topic}] {item.text}" for item in self.findings]
        for stage, rate in self.rates.items():
            out.append(f"· [hız] {stage}: {'—' if rate is None else f'{rate:,.0f} belge/sn'}")
        for label, seconds in self.projection.items():
            out.append(f"· [tahmin] {label}: {format_duration(seconds)}")
        return out


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "hesaplanamadı"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} gün {hours} sa {minutes} dk"
    if hours:
        return f"{hours} sa {minutes} dk"
    return f"{minutes} dk"


def project(total: int | None, read: float | None, flatten: float | None, write: float | None) -> dict[str, float | None]:
    """
    Run time from per-stage rates. Serial: the stages take turns. With the
    reading thread, read + flatten run beside the write, so the slower of the
    two sides sets the pace.
    """
    if not total or not read or not flatten or not write:
        return {"sıralı": None, "ön okumalı": None}
    produce = 1.0 / (1.0 / read + 1.0 / flatten)
    return {
        "sıralı": total * (1.0 / read + 1.0 / flatten + 1.0 / write),
        "ön okumalı": total / min(produce, write),
    }


@dataclass
class ProbeTable:
    table: str
    temp: str
    columns: list[str]
    sql_types: list[str]
    create: str


def probe_tables(plan: dict[str, Any]) -> list[ProbeTable]:
    """
    Temporary copies of the plan's tables: same columns, types and primary
    keys, no foreign keys. Constraints stay unnamed, since a named one in
    tempdb clashes with the same name in another session.
    """
    keys: dict[str, list[str]] = {plan["root"]["table"]: ["mongo_id"]}
    for child in plan["children"]:
        if child["kind"] == "map":
            keys[child["table"]] = [child["parent_key"], child["key_column"]["name"]]
        else:
            keys[child["table"]] = [child["parent_key"], *child["idx_columns"]]
    out = []
    for index, (table, specs) in enumerate(plan_column_specs(plan)):
        temp = f"#m2s_probe_{index}"
        pk = keys.get(table, [])
        body = [f"[{name}] {sql_type} {'NOT NULL' if name in pk else 'NULL'}" for name, sql_type in specs]
        if pk:
            body.append(f"PRIMARY KEY ({', '.join(f'[{name}]' for name in pk)})")
        out.append(
            ProbeTable(
                table=table,
                temp=temp,
                columns=[name for name, _ in specs],
                sql_types=[sql_type for _, sql_type in specs],
                create=f"CREATE TABLE {temp} ({', '.join(body)})",
            )
        )
    return out


def run_preflight(
    source: MongoClientWrapper,
    target_factory: Callable[[], MssqlConnection],
    plan: dict[str, Any],
    collection: str,
    *,
    date_query: dict[str, Any] | None = None,
    date_field: str | None = None,
    probe_docs: int = PROBE_DOCS,
) -> Report:
    report = Report()
    schema = plan["schema"]
    root = plan["root"]["table"]

    # -- Mongo ------------------------------------------------------------
    try:
        client = source.db.client
        info = client.server_info()
        topology = client.topology_description.topology_type_name
        report.add("ok", "mongo", f"MongoDB {info.get('version')} · {topology}")
        rtt = []
        for _ in range(10):
            tick = time.perf_counter()
            client.admin.command("ping")
            rtt.append(time.perf_counter() - tick)
        report.add(
            "info",
            "ağ",
            f"Mongo gidiş-dönüş ~{1000 * sorted(rtt)[len(rtt) // 2]:.1f} ms · "
            f"sıkıştırma: {getattr(source, 'compressors', None) or 'yok'}",
        )
    except Exception as exc:
        report.add("warn", "mongo", f"Sunucu bilgisi okunamadı: {describe(exc)}")
    try:
        report.estimated_docs = source.estimated_count(collection)
    except Exception as exc:
        report.add("warn", "mongo", f"Belge sayısı okunamadı: {describe(exc)}")
    if report.estimated_docs is not None:
        report.add("info", "mongo", f"~{report.estimated_docs:,} belge (tahmini)")
    if date_query and date_field:
        indexed = date_field in source.leading_range_index_fields(collection)
        if indexed:
            report.add("ok", "mongo", f"`{date_field}` için index var")
        else:
            report.add(
                "warn",
                "mongo",
                f"`{date_field}` index'siz: aralık filtresi her belgeyi okur. "
                f"db.{collection}.createIndex({{ {date_field}: 1 }})",
            )

    # -- SQL Server -------------------------------------------------------
    db = target_factory()
    try:
        db.connect(login_timeout=30, query_timeout=600)
    except Exception as exc:
        report.add("warn", "sql", f"Bağlanılamadı: {describe(exc)}")
        return report
    try:
        _sql_checks(report, db, schema, root, plan)
        _probe(report, source, db, plan, collection, date_query, probe_docs)
    finally:
        db.rollback()
        db.close()
    report.projection = project(
        report.estimated_docs, report.rates.get("okuma"), report.rates.get("düzleştirme"), report.rates.get("SQL yazma")
    )
    return report


def _sql_checks(report: Report, db: MssqlConnection, schema: str, root: str, plan: dict[str, Any]) -> None:
    try:
        name = db.conn.getinfo(pyodbc.SQL_DRIVER_NAME)
        version = db.conn.getinfo(pyodbc.SQL_DRIVER_VER)
        if "sqlsrv32" in str(name).lower():
            report.add(
                "warn",
                "sürücü",
                f"Eski 'SQL Server' sürücüsü ({name} {version}): DATETIME2 desteklemez ve hızlı "
                "yazma yolu güvenilir değil. ODBC Driver 17 ya da 18 kurun.",
            )
        else:
            report.add("ok", "sürücü", f"{name} {version}")
    except Exception as exc:
        report.add("warn", "sürücü", f"Sürücü bilgisi okunamadı: {describe(exc)}")
    others = [d for d in installed_drivers() if d != db.driver]
    if others:
        report.add("info", "sürücü", "Kurulu diğer sürücüler: " + ", ".join(others))

    db_collation = ""
    try:
        cur = db.conn.cursor()
        cur.execute(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)), "
            "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)), "
            "CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128))"
        )
        version, edition, db_collation = cur.fetchone()
        report.add("ok", "sql", f"SQL Server {version} · {edition} · collation {db_collation}")
    except Exception as exc:
        report.add("warn", "sql", f"Sunucu bilgisi okunamadı: {describe(exc)}")

    try:
        live = db.column_info(schema, root).get("mongo_id")
        key_type = live["sql_type"] if live else key_column_type(plan)
        collation = str((live or {}).get("collation") or db_collation or "")
        # ObjectId text is lowercase hex, so only other text keys can collide.
        text_key = kind_of(key_type) == "text" and key_type.upper() != "CHAR(24)"
        if text_key and "_CI_" in collation.upper():
            report.add(
                "warn",
                "anahtar",
                f"`mongo_id` collation'ı büyük/küçük harf duyarsız ({collation}): yalnız harf "
                "büyüklüğüyle ayrışan metin _id'ler aynı anahtar sayılır ve reddedilir.",
            )
    except Exception:
        pass

    try:
        cur = db.conn.cursor()
        cur.execute(
            "SELECT recovery_model_desc, log_reuse_wait_desc FROM sys.databases WHERE name = DB_NAME()"
        )
        recovery, reuse = cur.fetchone()
        if str(recovery).upper() == "FULL":
            report.add(
                "info",
                "log",
                f"Recovery model FULL (log bekleme: {reuse}). Log yalnız log yedeğiyle boşalır; "
                "uzun yüklemede DBA sık log yedeği planlamalı. Log dolarsa (9002) iş bekler ve "
                "yeniden dener. Log ve veri dosyalarını önceden büyütmek yükü azaltır.",
            )
        else:
            report.add("ok", "log", f"Recovery model {recovery} (log bekleme: {reuse})")
        cur.execute(
            "SELECT type_desc, name, CAST(size AS bigint) * 8 / 1024, max_size, growth, is_percent_growth "
            "FROM sys.database_files"
        )
        for kind, name, size_mb, max_size, growth, percent in cur.fetchall():
            if not growth:
                report.add(
                    "warn",
                    "dosya",
                    f"{kind} {name}: {size_mb:,} MB ve büyümesi kapalı. Dolunca yazma durur "
                    "(iş bekler ve yeniden dener); DBA dosyayı önceden büyütmeli.",
                )
                continue
            grow = f"%{growth}" if percent else f"{int(growth) * 8 // 1024} MB"
            # -1: until the disk is full; 268435456 pages: the 2 TB log ceiling.
            limit = "disk dolana dek" if max_size in (-1, 268435456) else f"{int(max_size) * 8 // 1024:,} MB"
            report.add("info", "dosya", f"{kind} {name}: {size_mb:,} MB, büyüme {grow}, üst sınır {limit}")
    except Exception as exc:
        report.add("info", "log", f"Recovery / dosya bilgisi okunamadı (yetki gerekebilir): {describe(exc)}")

    try:
        access = db.test()
        if access.can_write and access.can_create:
            report.add("ok", "yetki", f"{access.login}: yazabilir ve tablo oluşturabilir")
        else:
            report.add(
                "warn",
                "yetki",
                f"{access.login}: yazma={access.can_write}, tablo oluşturma={access.can_create}",
            )
        if db.table_exists(schema, root) and not db.scalar(
            "SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'ALTER')", f"[{schema}].[{root}]"
        ):
            report.add(
                "warn",
                "yetki",
                "Kök tabloda ALTER yetkisi yok: TRUNCATE ile boşaltma ve kolon genişletme çalışmaz "
                "(yerine parça parça DELETE ve kırpma kullanılır).",
            )
        db.conn.commit()
    except Exception as exc:
        report.add("warn", "yetki", f"Yetkiler okunamadı: {describe(exc)}")


def _probe(
    report: Report,
    source: MongoClientWrapper,
    db: MssqlConnection,
    plan: dict[str, Any],
    collection: str,
    date_query: dict[str, Any] | None,
    probe_docs: int,
) -> None:
    started = time.perf_counter()
    items = []
    try:
        reader = ChunkedReader(
            source.collection(collection),
            query=date_query,
            projection=projection_for(plan),
            chunk=probe_docs,
        )
        for item in reader:
            items.append(item)
            if len(items) >= probe_docs:
                break
    except Exception as exc:
        report.add("warn", "ölçüm", f"Mongo'dan okunamadı: {describe(exc)}")
        return
    read_seconds = time.perf_counter() - started
    if not items:
        report.add("info", "ölçüm", "Ölçüm için belge yok.")
        return
    report.rates["okuma"] = len(items) / read_seconds if read_seconds > 0 else None
    report.bytes_per_doc = sum(item.size for item in items) / len(items)

    key_type = key_column_type(plan)
    flattener = Flattener(plan, key_type)
    started = time.perf_counter()
    flats = [flattener.flatten(item.doc) for item in items if item.doc is not None]
    flat_seconds = time.perf_counter() - started
    report.rates["düzleştirme"] = len(flats) / flat_seconds if flat_seconds > 0 else None
    good = [flat for flat in flats if flat.reject is None and not flat.overflow]
    if not good:
        report.add("info", "ölçüm", "Örnekteki belgeler yazılabilir durumda değil; SQL hızı ölçülmedi.")
        return
    report.rows_per_doc = sum(flat.rows for flat in good) / len(good)

    rtt = []
    for _ in range(20):
        tick = time.perf_counter()
        db.scalar("SELECT 1")
        rtt.append(time.perf_counter() - tick)
    report.add("info", "ağ", f"SQL gidiş-dönüş ~{1000 * sorted(rtt)[len(rtt) // 2]:.1f} ms")

    tables = probe_tables(plan)
    try:
        cur = db.conn.cursor()
        for table in tables:
            cur.execute(table.create)
        payload = 0
        started = time.perf_counter()
        for index, table in enumerate(tables):
            if index == 0:
                rows = [flat.root for flat in good]
            else:
                rows = [row for flat in good for row in flat.children.get(table.table, ())]
            if not rows:
                continue
            payload += payload_bytes(rows)
            names = ", ".join(f"[{name}]" for name in table.columns)
            marks = ", ".join("?" for _ in table.columns)
            writer = db.conn.cursor()
            writer.fast_executemany = True
            step = rows_per_call(table.sql_types)
            for start in range(0, len(rows), step):
                writer.executemany(f"INSERT INTO {table.temp} ({names}) VALUES ({marks})", rows[start : start + step])
        write_seconds = time.perf_counter() - started
        report.rates["SQL yazma"] = len(good) / write_seconds if write_seconds > 0 else None
        report.add(
            "info",
            "ölçüm",
            f"{len(good)} belge ({report.rows_per_doc:.1f} satır/belge, ~{report.bytes_per_doc:,.0f} bayt/belge) "
            "geçici tablolara yazılıp geri alındı. tempdb'ye yazmak genelde daha hızlıdır ve commit "
            "süresi dahil değildir; gerçek hız biraz düşük çıkabilir.",
        )
        read_mbps = sum(item.size for item in items) / 1_000_000 / read_seconds if read_seconds > 0 else None
        sql_mbps = payload / 1_000_000 / write_seconds if write_seconds > 0 else None
        report.add(
            "info",
            "ağ",
            f"Mongo'dan ~{read_mbps or 0:.2f} MB/sn okundu, SQL'e ~{sql_mbps or 0:.2f} MB/sn yazıldı (tahmini).",
        )
        if read_mbps is not None and sql_mbps is not None and min(read_mbps, sql_mbps) < SLOW_LINK_MBPS:
            report.add(
                "warn",
                "ağ",
                "Hız ağ sınırında görünüyor: bu makine ile sunucular arasındaki bağlantı yavaş "
                "(VPN, Wi-Fi ya da uzak ağ). Aktarımı sunuculara yakın bir makinede, örneğin aynı "
                "veri merkezindeki bir sunucuda Zamanla komutuyla çalıştırmak en büyük kazancı sağlar.",
            )
    except Exception as exc:
        report.add("warn", "ölçüm", f"Geçici tablolara yazılamadı: {describe(exc)}")
    finally:
        db.rollback()
        try:
            cur = db.conn.cursor()
            for table in tables:
                cur.execute(f"IF OBJECT_ID('tempdb..{table.temp}') IS NOT NULL DROP TABLE {table.temp}")
            db.conn.commit()
        except Exception:
            db.rollback()
