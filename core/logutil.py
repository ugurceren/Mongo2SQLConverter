"""File logging for long-running jobs (transfer, profiling)."""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from core.settings import ROOT

LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "mongo2sql.log"
LOGGER_NAME = "mongo2sql"

# Progress: first batch always, then at least one of these.
_PROGRESS_EVERY = 25_000
_PROGRESS_SECONDS = 60.0


def configure_logging() -> Path:
    """Attach a rotating UTF-8 file handler. Safe to call on every Streamlit rerun."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    already = any(
        isinstance(handler, RotatingFileHandler)
        and Path(getattr(handler, "baseFilename", "")) == LOG_FILE.resolve()
        for handler in logger.handlers
    )
    if not already:
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
    return LOG_FILE


def get_logger() -> logging.Logger:
    configure_logging()
    return logging.getLogger(LOGGER_NAME)


def log_path_display() -> str:
    """Path shown in the UI, relative to the project when possible."""
    try:
        return str(LOG_FILE.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(LOG_FILE)


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "evet" if value else "hayır"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _join(fields: dict[str, Any]) -> str:
    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={_fmt(value)}")
    return " ".join(parts)


class JobLog:
    """One job: start, throttled progress, done. Failures are logged by the caller."""

    def __init__(self, kind: str, **fields: Any):
        self.kind = kind
        self.fields = dict(fields)
        self.log = get_logger()
        self._t0 = time.monotonic()
        self._last_t = 0.0
        self._last_n = 0

    def _kv(self, extra: dict[str, Any] | None = None) -> str:
        return _join({**self.fields, **(extra or {})})

    def elapsed(self) -> float:
        return max(time.monotonic() - self._t0, 0.0)

    def start(self, **extra: Any) -> None:
        self._t0 = time.monotonic()
        self._last_t = 0.0
        self._last_n = 0
        self.log.info("%s başladı %s", self.kind, self._kv(extra))

    def progress(self, done: int, total: int = 0, **extra: Any) -> None:
        now = time.monotonic()
        first = self._last_n == 0
        due_count = done - self._last_n >= _PROGRESS_EVERY
        due_time = (now - self._last_t) >= _PROGRESS_SECONDS if self._last_t else True
        if not first and not due_count and not due_time:
            return
        elapsed = max(now - self._t0, 0.001)
        extra = dict(extra)
        extra["belgeler"] = f"{done}/{total}" if total else done
        if total:
            extra["pct"] = f"{min(100.0 * done / total, 100.0):.1f}"
        extra["süre_sn"] = f"{elapsed:.0f}"
        extra["hız"] = f"{done / elapsed:.0f}/sn"
        self.log.info("%s ilerliyor %s", self.kind, self._kv(extra))
        self._last_t = now
        self._last_n = done

    def done(self, **extra: Any) -> None:
        elapsed = max(self.elapsed(), 0.001)
        extra = dict(extra)
        extra["süre_sn"] = f"{elapsed:.1f}"
        documents = extra.get("belgeler")
        if isinstance(documents, int) and documents > 0:
            extra["hız"] = f"{documents / elapsed:.0f}/sn"
        self.log.info("%s bitti %s", self.kind, self._kv(extra))
