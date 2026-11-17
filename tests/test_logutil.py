"""One log file per day, named after it."""

from __future__ import annotations

import logging
import re
import tempfile
import threading
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


class JobAndErrorFileTests(unittest.TestCase):
    """Each job also writes its own file; warnings and errors have a daily file of their own."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        for name, value in (("LOG_DIR", self.dir), ("LOGGER_NAME", f"mongo2sql_test_{id(self)}")):
            patcher = mock.patch.object(logutil, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log = logutil.get_logger()
        self.addCleanup(self._close)

    def _close(self):
        for handler in list(self.log.handlers):
            self.log.removeHandler(handler)
            handler.close()

    @staticmethod
    def lines(path: Path) -> list[str]:
        """Messages of the records, without timestamps and traceback lines."""
        return [
            line.split("] ", 1)[1]
            for line in path.read_text(encoding="utf-8").splitlines()
            if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[", line)
        ]

    def test_job_file_holds_the_job_and_its_threads_only(self):
        started = threading.Event()
        release = threading.Event()
        paths = {}

        def other_job():
            with logutil.job_file("profil", "votes") as path:
                paths["other"] = path
                self.log.info("other job line")
                started.set()
                release.wait(5)

        other = threading.Thread(target=other_job)
        other.start()
        started.wait(5)
        self.log.info("before any job of this thread")
        with logutil.job_file("aktarim", "conversations") as path:
            paths["mine"] = path
            self.log.info("mine")
            helper = threading.Thread(target=logutil.in_job_context(lambda: self.log.info("from reader thread")))
            helper.start()
            helper.join()
            try:
                raise ValueError("boom")
            except ValueError:
                self.log.exception("aktarım başarısız")
        release.set()
        other.join()
        self.log.info("after the job")

        mine = self.lines(paths["mine"])
        self.assertEqual(mine[1:4], ["mine", "from reader thread", "aktarım başarısız"])
        self.assertIn("ValueError: boom", paths["mine"].read_text(encoding="utf-8"))
        self.assertTrue(mine[0].startswith("iş günlüğü dosya="))
        self.assertEqual(self.lines(paths["other"])[1:], ["other job line"])
        self.assertEqual(paths["mine"].parent, self.dir / "jobs")
        self.assertRegex(paths["mine"].name, r"^\d{4}-\d\d-\d\d_\d{6}_aktarim_conversations\.log$")

        daily = self.lines(logutil.current_log_file())
        for line in ("before any job of this thread", "mine", "from reader thread", "other job line", "after the job"):
            self.assertIn(line, daily)

    def test_errors_file_takes_warnings_and_errors_only(self):
        self.log.info("info")
        self.log.warning("warn")
        self.log.error("error")
        self.assertEqual(self.lines(logutil.current_error_file()), ["warn", "error"])
        self.assertEqual(logutil.current_error_file().name, f"mongo2sql_errors_{date.today():%Y-%m-%d}.log")

    def test_configure_is_idempotent(self):
        logutil.configure_logging()
        logutil.configure_logging()
        self.assertEqual(len(self.log.handlers), 2)
        self.assertEqual(len(self.log.filters), 1)

    def test_odd_collection_names_make_safe_file_names(self):
        with logutil.job_file("şema keşfi", 'a/b:c*?"<>|') as path:
            pass
        self.assertNotRegex(path.name, r'[\\/:*?"<>| ]')
        with logutil.job_file("aktarim", "x") as first, logutil.job_file("aktarim", "x") as second:
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
