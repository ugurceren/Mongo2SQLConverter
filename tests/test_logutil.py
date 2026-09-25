"""One log file per day, named after it."""

from __future__ import annotations

import logging
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from core import logutil


class DailyLogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        patcher = mock.patch.object(logutil, "LOG_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.handler = logutil.DailyFileHandler()
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(self.handler.close)

    def emit(self, message: str, when: datetime) -> None:
        record = logging.LogRecord("mongo2sql", logging.INFO, __file__, 1, message, None, None)
        record.created = time.mktime(when.timetuple())
        self.handler.handle(record)

    def test_records_go_to_the_file_of_their_day(self):
        self.emit("before midnight", datetime(2026, 9, 25, 23, 59, 58))
        self.emit("after midnight", datetime(2026, 9, 26, 0, 0, 2))
        self.emit("still the 26th", datetime(2026, 9, 26, 9, 0, 0))
        self.handler.close()
        first = self.dir / "mongo2sql_2026-09-25.log"
        second = self.dir / "mongo2sql_2026-09-26.log"
        self.assertEqual(first.read_text(encoding="utf-8").splitlines(), ["before midnight"])
        self.assertEqual(second.read_text(encoding="utf-8").splitlines(), ["after midnight", "still the 26th"])

    def test_today_is_what_the_ui_shows(self):
        self.assertEqual(logutil.current_log_file().name, f"mongo2sql_{date.today():%Y-%m-%d}.log")


if __name__ == "__main__":
    unittest.main()
