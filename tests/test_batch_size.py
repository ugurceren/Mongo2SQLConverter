"""The write batch follows the document count unless a number was chosen."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from core import settings
from core.run_job import _write_batch
from core.settings import BATCH_AUTO, DEFAULT_BATCH, suggest_batch


class SuggestBatchTests(unittest.TestCase):
    def test_steps(self):
        cases = [
            (None, DEFAULT_BATCH),
            (0, 1_000),
            (99_999, 1_000),
            (100_000, 5_000),
            (999_999, 5_000),
            (2_740_547, 10_000),
            (6_000_000, 10_000),
            (10_000_000, 20_000),
            (350_000_000, 20_000),
        ]
        for documents, batch in cases:
            with self.subTest(documents=documents):
                self.assertEqual(suggest_batch(documents), batch)


class SavedBatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.local.yaml"
        patcher = mock.patch.object(settings, "LOCAL_CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def store(self, entry: dict) -> None:
        self.path.write_text(yaml.safe_dump({"transfer": {"orders": entry}}), encoding="utf-8")

    def test_new_collection_is_automatic(self):
        self.assertTrue(settings.load_transfer_prefs("orders")["batch_auto"])

    def test_untouched_old_default_turns_automatic(self):
        self.store({"batch": 1000})
        self.assertTrue(settings.load_transfer_prefs("orders")["batch_auto"])

    def test_old_chosen_number_stays(self):
        self.store({"batch": 3000})
        prefs = settings.load_transfer_prefs("orders")
        self.assertFalse(prefs["batch_auto"])
        self.assertEqual(prefs["batch"], 3000)

    def test_choice_round_trips(self):
        prefs = settings.load_transfer_prefs("orders")
        prefs.update(batch=1000, batch_auto=False)
        settings.save_transfer_prefs("orders", prefs)
        again = settings.load_transfer_prefs("orders")
        self.assertEqual((again["batch"], again["batch_auto"]), (1000, False))


class ScheduledJobBatchTests(unittest.TestCase):
    def test_cli_number_wins(self):
        self.assertEqual(_write_batch(2500, {"batch": 1000, "batch_auto": True}), 2500)

    def test_automatic_prefs_leave_it_to_the_run(self):
        self.assertEqual(_write_batch(None, {"batch": 5000, "batch_auto": True}), BATCH_AUTO)

    def test_chosen_prefs_number(self):
        self.assertEqual(_write_batch(None, {"batch": 3000, "batch_auto": False}), 3000)


if __name__ == "__main__":
    unittest.main()
