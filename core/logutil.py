"""File logging for long-running jobs (transfer, profiling)."""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from core.settings import ROOT

LOG_DIR = ROOT / "logs"
LOG_PREFIX = "mongo2sql"
LOGGER_NAME = "mongo2sql"

# Progress: first batch always, then at least one of these.
_PROGRESS_EVERY = 25_000
_PROGRESS_SECONDS = 60.0


def log_file_for(day: date) -> Path:
    """`logs/mongo2sql_2026-09-26.log`: one file per (local) day."""
    return LOG_DIR / f"{LOG_PREFIX}_{day:%Y-%m-%d}.log"


def current_log_file() -> Path:
    return log_file_for(date.today())


class DailyFileHandler(logging.FileHandler):
    """
    Writes each record to the file of the day it was made.

    A job running past midnight moves on to the next day's file. The UI and
    scheduled jobs append to the same day's file; unlike size rotation there is
    no renaming, so one process cannot pull a file from under another.
    """

    def __init__(self) -> None:
        self.day = date.today()
        super().__init__(log_file_for(self.day), encoding="utf-8", delay=True)

    def emit(self, record: logging.LogRecord) -> None:
        day = date.fromtimestamp(record.created)
        if day != self.day:  # the handler lock is held here
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.day = day
            self.baseFilename = os.path.abspath(log_file_for(day))
        super().emit(record)


def configure_logging() -> Path:
    """Attach the daily UTF-8 file handler. Safe to call on every Streamlit rerun."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        # The size-rotated `mongo2sql.log` of earlier versions, in a long-lived process.
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    if not any(isinstance(handler, DailyFileHandler) for handler in logger.handlers):
        handler = DailyFileHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
    return current_log_file()


def get_logger() -> logging.Logger:
    configure_logging()
    return logging.getLogger(LOGGER_NAME)


def log_path_display() -> str:
    """Today's log file as shown in the UI, relative to the project when possible."""
    path = current_log_file()
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


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

    def stopped(self, **extra: Any) -> None:
        elapsed = max(self.elapsed(), 0.001)
        extra = dict(extra)
        extra["süre_sn"] = f"{elapsed:.1f}"
        documents = extra.get("belgeler")
        if isinstance(documents, int) and documents > 0:
            extra["hız"] = f"{documents / elapsed:.0f}/sn"
        self.log.info("%s durduruldu %s", self.kind, self._kv(extra))
