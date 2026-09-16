"""Background transfer so navigating away from the SQL page does not abort it."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from app.ui.services import mongo_client, sql_target
from core.logutil import get_logger
from core.mongo import combine_filters, decode_mongo_id, id_after_filter
from core.settings import save_sync_watermark
from core.transfer import (
    TransferStats,
    ensure_tables,
    read_root_watermark,
    transfer_collection,
)

_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_SEQ = 0


@dataclass
class JobView:
    seq: int = 0
    status: str = "idle"
    collection: str = ""
    mode: str = "full"
    signature: str = ""
    message: str = ""
    done: int = 0
    total: int = 0
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    error: str | None = None
    stats: TransferStats | None = None


_VIEW = JobView()


def snapshot() -> JobView:
    with _LOCK:
        return copy.copy(_VIEW)


def is_busy() -> bool:
    return snapshot().status in {"running", "stopping"}


def request_stop() -> None:
    with _LOCK:
        if _VIEW.status == "running":
            _VIEW.status = "stopping"
            _VIEW.message = "Durduruluyor (açık parti yazılınca durur)..."
    _STOP.set()


def _patch(**fields: Any) -> None:
    with _LOCK:
        for key, value in fields.items():
            setattr(_VIEW, key, value)


def _write_query(options: dict[str, Any], date_query: dict[str, Any] | None) -> dict | None:
    if options.get("mode") != "incremental":
        return date_query
    mark = options.get("watermark")
    if not mark:
        return date_query
    try:
        last_id = decode_mongo_id(mark["last_id"], mark["last_id_type"])
    except Exception:
        return date_query
    return combine_filters(date_query, id_after_filter(last_id))


def _run(payload: dict[str, Any]) -> None:
    options: dict[str, Any] = payload["options"]
    plan: dict[str, Any] = payload["plan"]
    date_query = payload["date_query"]
    collection = options["collection"]
    source = mongo_client(payload["mongo"])
    target = sql_target(payload["mssql"], payload["mssql_password"], options["schema"])
    try:
        if _STOP.is_set():
            _patch(status="cancelled", message="Aktarım durduruldu", stats=TransferStats(stopped=True))
            return
        _patch(message="SQL tabloları hazırlanıyor...")
        source.connect()
        target.connect()
        created, existing = ensure_tables(target, plan, recreate=options["recreate"])
        _patch(created=list(created), existing=list(existing))
        if created:
            get_logger().info(
                "aktarım tablolar oluşturuldu collection=%s tablolar=%s",
                collection,
                ", ".join(created),
            )
        if _STOP.is_set():
            _patch(status="cancelled", message="Aktarım durduruldu", stats=TransferStats(stopped=True))
            return

        if options["mode"] == "incremental":
            exists, sql_mark = read_root_watermark(
                target, options["schema"], plan["root"]["table"]
            )
            if exists:
                options["watermark"] = sql_mark

        query = _write_query(options, date_query)
        expected = source.estimated_count(collection, query)
        _patch(total=expected or 0, message="Belgeler yazılıyor...")

        stats = transfer_collection(
            source,
            target,
            plan,
            collection,
            sample=0,
            batch_size=options["batch"],
            clear_first=options["clear_first"],
            query=query,
            mode=options["mode"],
            progress=lambda done, total: _patch(
                done=done,
                total=total or expected or 0,
                message="Belgeler yazılıyor...",
            ),
            expected_count=expected,
            log_extra=payload.get("log_extra"),
            should_stop=_STOP.is_set,
        )
        if stats.last_id and stats.last_id_type:
            save_sync_watermark(collection, stats.last_id, stats.last_id_type)
        if stats.stopped:
            _patch(status="cancelled", stats=stats, done=stats.documents, message="Aktarım durduruldu")
        else:
            _patch(
                status="done",
                stats=stats,
                done=stats.documents,
                message="Aktarım tamamlandı",
            )
    except Exception as exc:
        get_logger().exception(
            "aktarım başarısız collection=%s error=%s",
            collection,
            exc,
        )
        _patch(status="error", error=str(exc), message=str(exc))
    finally:
        source.close()
        target.close()


def start(
    *,
    plan: dict[str, Any],
    options: dict[str, Any],
    mongo_cfg: dict[str, Any],
    mssql_cfg: dict[str, Any],
    mssql_password: str | None,
    date_query: dict[str, Any] | None,
    signature: str,
    log_extra: dict[str, Any] | None = None,
) -> bool:
    global _THREAD, _SEQ
    with _LOCK:
        if _VIEW.status in {"running", "stopping"}:
            return False
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _SEQ += 1
        _STOP.clear()
        _VIEW.seq = _SEQ
        _VIEW.status = "running"
        _VIEW.collection = str(options.get("collection") or "")
        _VIEW.mode = str(options.get("mode") or "full")
        _VIEW.signature = signature
        _VIEW.message = "Aktarım başlıyor..."
        _VIEW.done = 0
        _VIEW.total = 0
        _VIEW.created = []
        _VIEW.existing = []
        _VIEW.error = None
        _VIEW.stats = None
        payload = {
            "plan": copy.deepcopy(plan),
            "options": copy.deepcopy(options),
            "mongo": copy.deepcopy(mongo_cfg),
            "mssql": copy.deepcopy(mssql_cfg),
            "mssql_password": mssql_password,
            "date_query": copy.deepcopy(date_query),
            "log_extra": dict(log_extra) if log_extra else None,
        }
        _THREAD = threading.Thread(
            target=_run,
            args=(payload,),
            name="mongo2sql-transfer",
            daemon=True,
        )
        _THREAD.start()
    return True
