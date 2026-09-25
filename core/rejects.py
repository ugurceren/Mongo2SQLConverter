"""
Where documents that could not be written, and values that were changed, go.

One JSON line per event under `logs/rejects/`. Document bodies are never
written, only the `_id`, the table / column and the reason, so the file is safe
to share and small enough to read. The writer syncs the file to disk before
the commit that moves the checkpoint past its documents: a crash can repeat a
line, never lose one.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import json_util

from core.settings import ROOT

REJECTS_DIR = ROOT / "logs" / "rejects"
# The first ones are always worth a log line; after that, every hundredth.
LOG_FIRST = 20
LOG_EVERY = 100

_UNSAFE = re.compile(r"[^0-9A-Za-z_.-]+")


def id_text(value: Any) -> str | None:
    """Canonical extended JSON of an `_id`, the same form the checkpoint stores."""
    if value is None:
        return None
    try:
        return json_util.dumps({"_id": value}, json_options=json_util.CANONICAL_JSON_OPTIONS)
    except (TypeError, ValueError):
        return json.dumps({"_id": str(value)})


class RejectLog:
    def __init__(
        self,
        collection: str,
        table: str,
        run_id: str,
        *,
        directory: Path | None = None,
        log=None,
    ) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{_UNSAFE.sub('_', collection)}__{_UNSAFE.sub('_', table)}__{stamp}_{run_id[:8]}.jsonl"
        self.path = (directory or REJECTS_DIR) / name
        self.collection = collection
        self.run_id = run_id
        self.log = log
        self.rejected = 0
        self.clipped = 0
        self.nulled = 0
        self._handle = None
        self._written = 0
        self._dirty = False

    @property
    def used(self) -> bool:
        return self._written > 0

    def record(
        self,
        kind: str,
        *,
        stage: str,
        mongo_id: Any = None,
        table: str | None = None,
        column: str | None = None,
        error: str | None = None,
        sqlstate: str | None = None,
        native: list[int] | None = None,
        length: int | None = None,
        width: int | None = None,
        detail: str | None = None,
    ) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "collection": self.collection,
            "kind": kind,
            "stage": stage,
            "_id": id_text(mongo_id),
            "table": table,
            "column": column,
            "sqlstate": sqlstate,
            "native": native or None,
            "error": (error or "")[:2000] or None,
            "length": length,
            "width": width,
            "detail": detail,
        }
        self._write(entry)
        if kind == "reject":
            self.rejected += 1
        elif kind == "clip":
            self.clipped += 1
        else:
            self.nulled += 1
        if self.log is not None and (self._written <= LOG_FIRST or self._written % LOG_EVERY == 0):
            self.log.warning(
                "aktarım kayıt tür=%s aşama=%s _id=%s tablo=%s kolon=%s hata=%s dosya=%s",
                kind,
                stage,
                entry["_id"],
                table or "-",
                column or "-",
                (error or detail or "-")[:200],
                self.path,
            )

    def _write(self, entry: dict[str, Any]) -> None:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        self._handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._handle.flush()
        self._written += 1
        self._dirty = True

    def sync(self) -> None:
        """Force what was written to disk; called once per batch, before its commit."""
        if self._dirty and self._handle is not None:
            os.fsync(self._handle.fileno())
            self._dirty = False

    def close(self) -> None:
        if self._handle is not None:
            self.sync()
            self._handle.close()
            self._handle = None


def read_lines(path: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    """The first `limit` records of a rejects file, repeated `_id`/kind pairs once."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                marker = (entry.get("_id"), entry.get("kind"), entry.get("column"))
                if marker in seen:
                    continue
                seen.add(marker)
                out.append(entry)
                if len(out) >= limit:
                    break
    except OSError:
        return out
    return out
