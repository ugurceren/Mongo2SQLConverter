"""`tools/run_transfer.py`: flags reach the job, exit codes tell the scheduler what happened."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.preflight import Finding, Report
from core.run_job import JobResult
from core.transfer import TransferStats
from core.writer import LockBusy

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "run_transfer.py"
QUIET = logging.getLogger("mongo2sql.tests")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False


def load_cli():
    spec = importlib.util.spec_from_file_location("run_transfer_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliTests(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()
        for name, value in (("configure_logging", mock.Mock()), ("get_logger", mock.Mock(return_value=QUIET))):
            patcher = mock.patch.object(self.cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv, result=None, error=None):
        job = mock.Mock(return_value=result, side_effect=error)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.cli, "run_transfer_job", job), mock.patch.object(self.cli, "_utf8_output"):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.cli.main(["--collection", "conversations", *argv])
        return code, out.getvalue(), err.getvalue(), job

    def result(self, **stats) -> JobResult:
        return JobResult(
            collection="conversations",
            requested_mode="auto",
            mode="full",
            stats=TransferStats(written=10, rows={"T": 40}, **stats),
            note="yarım kalan tam yükleme sürüyor",
        )

    def test_clean_run_exits_0_and_passes_flags(self):
        code, out, _, job = self.run_cli("--restart", "--max-rejects", "5", "--full-profile", result=self.result())
        self.assertEqual(code, 0)
        kwargs = job.call_args.kwargs
        self.assertEqual((kwargs["restart"], kwargs["max_rejects"], kwargs["full_profile"]), (True, 5, True))
        self.assertIn("10 documents written, 40 rows", out)
        self.assertIn("yarım kalan", out)

    def test_rejects_exit_2_and_show_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.jsonl"
            path.write_text(
                json.dumps({"kind": "reject", "_id": '{"_id": 5}', "table": "T", "error": "Numeric value out of range"}) + "\n",
                encoding="utf-8",
            )
            code, out, _, _ = self.run_cli(result=self.result(rejected=1, rejects_path=str(path)))
        self.assertEqual(code, 2)
        self.assertIn("rejects file", out)
        self.assertIn('reject {"_id": 5} T Numeric value out of range', out)
        for stats in ({"truncated": 3}, {"nulled": 1}):
            with self.subTest(stats=stats):
                self.assertEqual(self.run_cli(result=self.result(**stats))[0], 2)

    def test_failures_exit_1(self):
        for error in (RuntimeError("SQL bağlantısı yok"), LockBusy("başka bir aktarım yazıyor")):
            with self.subTest(error=error):
                code, _, err, _ = self.run_cli(error=error)
                self.assertEqual(code, 1)
                self.assertIn(str(error), err)

    def test_preflight_only_writes_nothing(self):
        report = Report(findings=[Finding("warn", "sürücü", "eski sürücü")], rates={"okuma": 1000.0})
        preflight = mock.Mock(return_value=report)
        out = io.StringIO()
        with mock.patch.object(self.cli, "run_preflight_job", preflight), mock.patch.object(
            self.cli, "run_transfer_job"
        ) as transfer, mock.patch.object(self.cli, "_utf8_output"), contextlib.redirect_stdout(out):
            code = self.cli.main(["--collection", "conversations", "--preflight-only"])
        self.assertEqual(code, 0)
        transfer.assert_not_called()
        self.assertIn("! [sürücü] eski sürücü", out.getvalue())
        self.assertIn("okuma: 1,000 belge/sn", out.getvalue())


if __name__ == "__main__":
    unittest.main()
