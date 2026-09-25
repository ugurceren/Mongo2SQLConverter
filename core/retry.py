"""
Which errors a long transfer can wait out, and how long it waits.

A 300-million-document load meets dropped connections, failovers, deadlocks
and full logs. Those are waited out and resumed from the checkpoint; data
errors go to bisection (`core.writer`); everything else stops the job.
Classification works on SQLSTATE and native error numbers because pyodbc
raises the same class for very different problems (a deadlock arrives as a
bare `pyodbc.Error`).
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Callable

TRANSIENT = "transient"  # reconnect and try again
RESOURCE = "resource"  # the server needs a person (log / disk full): wait longer
TIMEOUT = "timeout"  # a Mongo query hit maxTimeMS: a few tries, then stop
DATA = "data"  # this batch holds a value SQL Server refuses: bisect
BIND = "bind"  # pyodbc could not bind the batch: retry without fast_executemany
FATAL = "fatal"  # configuration or permission: stop the job
UNKNOWN = "unknown"  # treated like data; the reject burst limit stops systematic cases

# pymongo's own retryable codes (helpers_shared._RETRYABLE_ERROR_CODES is
# private, so it is copied) plus interrupted / shutdown codes.
MONGO_RETRYABLE_CODES = frozenset(
    {6, 7, 89, 91, 134, 175, 189, 237, 262, 9001, 10058, 10107, 11600, 11601, 11602, 13435, 13436}
)

SQL_TRANSIENT_STATES = frozenset({"08001", "08003", "08004", "08007", "08S01", "HYT00", "HYT01", "40001"})
SQL_TRANSIENT_NATIVE = frozenset(
    {
        1205,  # deadlock victim
        1222,  # lock request timeout (SET LOCK_TIMEOUT)
        701,  # insufficient memory
        8645,  # memory grant timeout
        1204,  # out of locks
        3960,  # snapshot update conflict
        4060,  # database unavailable (failover)
        233, 64, 121,  # connection dropped
        10053, 10054, 10060, 10061,  # TCP reset / timeout / refused
        40613, 40197, 40501, 49918, 49919, 49920, 10928, 10929,  # Azure SQL throttling / failover
    }
)
SQL_RESOURCE_NATIVE = frozenset({9002, 1105, 1101})  # log full, filegroup full, no space
SQL_FATAL_NATIVE = frozenset({208, 207, 229, 262, 297, 916, 1088, 4712, 18456})
SQL_DATA_NATIVE = frozenset({8152, 2628, 8115, 8114, 245, 241, 242, 2627, 2601, 515, 547, 1946, 8023})
SQL_BIND_STATES = frozenset({"HY104", "HY105", "HY090", "07006", "07009", "HYC00"})
DEADLOCK_NATIVE = 1205

_NATIVE_RE = re.compile(r"\((-?\d+)\)")


def sql_codes(exc: BaseException) -> tuple[str, frozenset[int]]:
    """SQLSTATE and the native error numbers pyodbc puts in its message."""
    args = getattr(exc, "args", ()) or ()
    state = str(args[0]) if args and isinstance(args[0], str) else ""
    message = " ".join(str(part) for part in args[1:]) if len(args) > 1 else str(exc)
    return state.upper(), frozenset(int(number) for number in _NATIVE_RE.findall(message))


def classify_sql(exc: BaseException) -> str:
    if isinstance(exc, (MemoryError, OverflowError, TypeError)):
        return BIND
    message = str(exc)
    # pyodbc's own buffer check, not SQL Server's 22001.
    if "String data, right truncation: length" in message:
        return BIND
    state, natives = sql_codes(exc)
    if natives & SQL_RESOURCE_NATIVE:
        return RESOURCE
    if state in SQL_TRANSIENT_STATES or natives & SQL_TRANSIENT_NATIVE:
        return TRANSIENT
    if natives & SQL_FATAL_NATIVE or state == "28000":
        return FATAL
    if state[:2] in {"22", "23"} or natives & SQL_DATA_NATIVE:
        return DATA
    if state in SQL_BIND_STATES:
        return BIND
    return UNKNOWN


def classify_mongo(exc: BaseException) -> str:
    from pymongo import errors

    if isinstance(exc, errors.ConnectionFailure):
        # AutoReconnect, NetworkTimeout, NotPrimaryError, ServerSelectionTimeoutError,
        # WaitQueueTimeoutError: the driver reconnects on the next command.
        return TRANSIENT
    if isinstance(exc, errors.CursorNotFound):
        return TRANSIENT
    if isinstance(exc, errors.ExecutionTimeout):
        return TIMEOUT
    if isinstance(exc, errors.OperationFailure):
        if exc.code in MONGO_RETRYABLE_CODES:
            return TRANSIENT
        if exc.has_error_label("RetryableError") or exc.has_error_label("SystemOverloadedError"):
            return TRANSIENT
    return FATAL


def describe(exc: BaseException) -> str:
    """One log-friendly line: class, SQLSTATE / code and the first part of the message."""
    state, natives = sql_codes(exc)
    code = getattr(exc, "code", None)
    parts = [type(exc).__name__]
    if state:
        parts.append(f"sqlstate={state}")
    if natives:
        parts.append("native=" + ",".join(str(n) for n in sorted(natives)))
    if code is not None:
        parts.append(f"code={code}")
    text = " ".join(str(exc).split())
    parts.append(text[:300])
    return " ".join(parts)


@dataclass
class RetryPolicy:
    attempts: int = 12  # per incident
    incident_seconds: float = 1800.0  # give up an incident after this long
    base_seconds: float = 2.0
    cap_seconds: float = 120.0
    resource_seconds: float = 300.0  # log full / disk full: long, fixed waits
    resource_total_seconds: float = 7200.0
    timeout_attempts: int = 3  # Mongo maxTimeMS
    deadlock_budget: int = 200  # per run


class GiveUp(RuntimeError):
    """An incident outlasted its retry budget."""


class Retrier:
    """
    Backoff with jitter for one side (Mongo or SQL) of a job.

    `wait(kind, exc)` sleeps before the next attempt, or raises `GiveUp` when
    the incident is over budget. `reset()` after a success starts a new
    incident. Sleeps run in one-second slices so a stop request is honoured.
    """

    def __init__(
        self,
        side: str,
        policy: RetryPolicy | None = None,
        *,
        log=None,
        should_stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.side = side
        self.policy = policy or RetryPolicy()
        self.log = log
        self.should_stop = should_stop
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self.total_retries = 0
        self.deadlocks = 0
        self._attempt = 0
        self._started: float | None = None

    def reset(self) -> None:
        self._attempt = 0
        self._started = None

    def wait(self, kind: str, exc: BaseException, where: str = "") -> None:
        policy = self.policy
        now = self._clock()
        if self._started is None:
            self._started = now
        self._attempt += 1
        self.total_retries += 1
        elapsed = now - self._started

        if kind == RESOURCE:
            limit_attempts = int(policy.resource_total_seconds // policy.resource_seconds) + 1
            delay = policy.resource_seconds
            over = elapsed >= policy.resource_total_seconds or self._attempt > limit_attempts
        elif kind == TIMEOUT:
            limit_attempts = policy.timeout_attempts
            delay = self._jitter(self._attempt)
            over = self._attempt > limit_attempts
        else:
            limit_attempts = policy.attempts
            _, natives = sql_codes(exc)
            if DEADLOCK_NATIVE in natives:
                self.deadlocks += 1
                if self.deadlocks > policy.deadlock_budget:
                    raise GiveUp(f"çok fazla deadlock ({self.deadlocks})") from exc
                delay = 0.5 + 1.5 * self._rng()
            else:
                delay = self._jitter(self._attempt)
            over = self._attempt > limit_attempts or elapsed >= policy.incident_seconds

        if over:
            raise GiveUp(
                f"{self.side} tarafında hata sürdü ({self._attempt - 1} deneme, "
                f"{elapsed:.0f} sn): {describe(exc)}"
            ) from exc
        if self.log is not None:
            level = self.log.error if kind == RESOURCE else self.log.warning
            advice = ""
            if kind == RESOURCE:
                advice = (
                    " DBA: transaction log ya da disk dolu; log yedeği alın veya dosyayı büyütün."
                )
            level(
                "aktarım yeniden deneme taraf=%s deneme=%s/%s bekle_sn=%.1f konum=%s hata=%s%s",
                self.side,
                self._attempt,
                limit_attempts,
                delay,
                where or "-",
                describe(exc),
                advice,
            )
        self._pause(delay)

    def _jitter(self, attempt: int) -> float:
        policy = self.policy
        return min(policy.cap_seconds, policy.base_seconds * 2 ** (attempt - 1)) * (0.5 + 0.5 * self._rng())

    def _pause(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            if self.should_stop and self.should_stop():
                raise Stopped()
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step


class Stopped(Exception):
    """A stop was requested while waiting to retry."""
