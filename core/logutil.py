"""File logging for long-running jobs (transfer, profiling)."""

from __future__ import annotations

import contextvars
import functools
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator

from core.settings import ROOT

LOG_DIR = ROOT / "logs"
LOG_PREFIX = "mongo2sql"
LOGGER_NAME = "mongo2sql"

# Progress: first batch always, then at least one of these.
_PROGRESS_EVERY = 25_000
_PROGRESS_SECONDS = 60.0


ERRORS_PREFIX = f"{LOG_PREFIX}_errors"
JOBS_DIR_NAME = "jobs"

# The job the current thread (or task) is logging for; a job's own file only
# takes records stamped with its id. Threads a job starts must run in a copy
# of its context (`contextvars.copy_context().run`) to be counted in.
_CURRENT_JOB: contextvars.ContextVar[str | None] = contextvars.ContextVar("mongo2sql_job", default=None)


def log_file_for(day: date, prefix: str = LOG_PREFIX) -> Path:
    """`logs/mongo2sql_2026-09-26.log`: one file per (local) day."""
    return LOG_DIR / f"{prefix}_{day:%Y-%m-%d}.log"


def current_log_file() -> Path:
    return log_file_for(date.today())


def current_error_file() -> Path:
    return log_file_for(date.today(), ERRORS_PREFIX)


def jobs_dir() -> Path:
    return LOG_DIR / JOBS_DIR_NAME


def _formatter() -> logging.Formatter:
    return logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


class DailyFileHandler(logging.FileHandler):
    """
    Writes each record to the file of the day it was made.

    A job running past midnight moves on to the next day's file. The UI and
    scheduled jobs append to the same day's file; unlike size rotation there is
    no renaming, so one process cannot pull a file from under another.
    """

    def __init__(self, prefix: str = LOG_PREFIX, level: int = logging.NOTSET) -> None:
        self.prefix = prefix
        self.day = date.today()
        super().__init__(log_file_for(self.day, prefix), encoding="utf-8", delay=True)
        self.setLevel(level)

    def emit(self, record: logging.LogRecord) -> None:
        day = date.fromtimestamp(record.created)
        if day != self.day:  # the handler lock is held here
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.day = day
            self.baseFilename = os.path.abspath(log_file_for(day, self.prefix))
        super().emit(record)


class _StampJob(logging.Filter):
    """Tags every record with the job it was logged for."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.m2s_job = _CURRENT_JOB.get()
        return True


class _OnlyJob(logging.Filter):
    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "m2s_job", None) == self.job_id


def configure_logging() -> Path:
    """
    Attach the daily file handlers: everything, and warnings and errors on
    their own. Safe to call on every Streamlit rerun.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        # The size-rotated `mongo2sql.log` of earlier versions, in a long-lived process.
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    # By class name: Streamlit reloads this module on edits. Old handlers must
    # still count, or every line would be doubled; an old stamp filter reads
    # the old module's job variable, so it is swapped for the current one.
    for old in [f for f in logger.filters if type(f).__name__ == "_StampJob" and type(f) is not _StampJob]:
        logger.removeFilter(old)
    if not any(type(f) is _StampJob for f in logger.filters):
        logger.addFilter(_StampJob())
    daily = [h for h in logger.handlers if type(h).__name__ == "DailyFileHandler"]
    if not any(getattr(h, "prefix", LOG_PREFIX) == LOG_PREFIX for h in daily):
        handler = DailyFileHandler()
        handler.setFormatter(_formatter())
        logger.addHandler(handler)
    if not any(getattr(h, "prefix", None) == ERRORS_PREFIX for h in daily):
        handler = DailyFileHandler(ERRORS_PREFIX, logging.WARNING)
        handler.setFormatter(_formatter())
        logger.addHandler(handler)
    return current_log_file()


def _file_part(text: str) -> str:
    """A collection or job name as a Windows-safe piece of a file name."""
    cleaned = re.sub(r"[^\w.-]+", "-", text, flags=re.UNICODE).strip("-.")
    return cleaned[:80] or "job"


def _new_job_file(kind: str, name: str) -> Path:
    folder = jobs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y-%m-%d_%H%M%S}_{_file_part(kind)}_{_file_part(name)}"
    path = folder / f"{stem}.log"
    n = 2
    while path.exists():
        path = folder / f"{stem}_{n}.log"
        n += 1
    return path


@contextmanager
def job_file(kind: str, name: str) -> Iterator[Path]:
    """
    Also write this job's records to a file of its own, `logs/jobs/
    2026-11-17_164246_aktarim_conversations.log`, next to the daily file.

    Covers everything logged in this thread (and threads started in a copy of
    its context) until the block ends, including a failure the caller logs
    inside the block. Other jobs running at the same time stay out of it.
    """
    logger = get_logger()
    path = _new_job_file(kind, name)
    job_id = str(path)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_formatter())
    handler.addFilter(_OnlyJob(job_id))
    token = _CURRENT_JOB.set(job_id)
    logger.addHandler(handler)
    try:
        logger.info("iş günlüğü dosya=%s", path)
        yield path
    finally:
        logger.removeHandler(handler)
        handler.close()
        _CURRENT_JOB.reset(token)


def in_job_context(target: Callable[..., Any]) -> Callable[..., Any]:
    """`target` for a thread a job starts, so its log lines reach the job's file."""
    return functools.partial(contextvars.copy_context().run, target)


def get_logger() -> logging.Logger:
    configure_logging()
    return logging.getLogger(LOGGER_NAME)


def log_path_display(path: Path | None = None) -> str:
    """A log path as shown in the UI (today's file by default), relative to the project when possible."""
    path = path or current_log_file()
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
