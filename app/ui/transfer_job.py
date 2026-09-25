"""Background transfer so navigating away from the SQL page does not abort it."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from app.ui.services import mongo_client, sql_target
from core.logutil import get_logger
from core.run_job import TransferRequest, execute_transfer, loader_settings
from core.transfer import TransferStats
from core.writer import LockBusy

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
    note: str = ""  # how the run started (new load, resumed, incremental)
    # Connection settings the job ran with, passwords left out; a finished job
    # proves they work.
    connections: dict[str, dict[str, Any]] = field(default_factory=dict)


_VIEW = JobView()


def _without_password(cfg: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in cfg.items() if key != "password"}


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


def _run(payload: dict[str, Any]) -> None:
    options: dict[str, Any] = payload["options"]
    collection = options["collection"]
    source = mongo_client(payload["mongo"], long_running=True)
    knobs = loader_settings()
    request = TransferRequest(
        collection=collection,
        plan=payload["plan"],
        mongo_database=payload["mongo"].get("database") or "",
        mode=options["mode"],
        date_query=payload["date_query"],
        batch=int(options["batch"]),
        recreate=bool(options.get("recreate")),
        clear_first=bool(options.get("clear_first")),
        restart=bool(options.get("restart")),
        max_rejects=knobs["max_rejects"],
        overlap_minutes=knobs["overlap_minutes"],
        commit_rows=knobs["commit_rows"],
        commit_mb=knobs["commit_mb"],
        prefetch=knobs["prefetch"],
    )

    def target():
        return sql_target(payload["mssql"], payload["mssql_password"], options["schema"])

    def progress(done: int, total: int) -> None:
        _patch(done=done, total=total, message="Belgeler yazılıyor...")

    try:
        if _STOP.is_set():
            _patch(status="cancelled", message="Aktarım durduruldu", stats=TransferStats(stopped=True))
            return
        _patch(message="Mongo bağlantısı açılıyor...")
        source.connect()
        result = execute_transfer(
            source,
            target,
            request,
            progress=progress,
            should_stop=_STOP.is_set,
            on_status=lambda message: _patch(message=message),
            log_extra=payload.get("log_extra"),
        )
        stats = result.stats
        _patch(
            status="cancelled" if stats.stopped else "done",
            stats=stats,
            done=stats.documents,
            created=list(result.created),
            existing=list(result.existing),
            mode=result.mode,
            note=result.note,
            message="Aktarım durduruldu" if stats.stopped else "Aktarım tamamlandı",
        )
    except LockBusy as exc:
        _patch(status="error", error=str(exc), message=str(exc))
    except Exception as exc:
        get_logger().exception(
            "aktarım başarısız collection=%s error=%s",
            collection,
            exc,
        )
        _patch(status="error", error=str(exc), message=str(exc))
    finally:
        source.close()


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
        _VIEW.note = ""
        _VIEW.connections = {
            "mongo": _without_password(mongo_cfg),
            "sql": _without_password(mssql_cfg),
        }
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
