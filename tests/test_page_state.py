"""
SQL aktarımı keeps its inputs across a visit to another page.

Streamlit deletes a widget's key after a run that did not draw it; without
putting the saved value back, the inputs came back at their minimum
(sample 0, batch 100), the collection box came back empty, and the minimum
was then saved as the collection's preference.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import yaml
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent / "ui_target_app.py"


class PageStateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "config.local.yaml"
        patcher = mock.patch.dict(os.environ, {"M2S_TEST_CONFIG": str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, saved: dict | None = None) -> AppTest:
        if saved is not None:
            self.config.write_text(yaml.safe_dump({"transfer": {"conversations": saved}}), encoding="utf-8")
        at = AppTest.from_file(str(APP), default_timeout=120)
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    @staticmethod
    def round_trip(at: AppTest, times: int = 2) -> None:
        for _ in range(times):
            at.session_state["page"] = "other"
            at.run()
            at.session_state["page"] = "transfer"
            at.run()

    def saved(self) -> dict:
        return yaml.safe_load(self.config.read_text(encoding="utf-8"))["transfer"]["conversations"]

    def test_defaults_and_steps(self):
        at = self.open()
        sample = at.number_input(key="tr_sample_conversations")
        batch = at.number_input(key="tr_batch_conversations")
        self.assertEqual((sample.value, sample.step), (5000, 1000))
        self.assertEqual((batch.value, batch.step), (1000, 500))

    def test_choices_survive_another_page(self):
        at = self.open()
        at.number_input(key="tr_sample_conversations").set_value(20000)
        at.number_input(key="tr_batch_conversations").set_value(3000)
        at.checkbox(key="tr_null_conversations").uncheck()
        at.radio(key="tr_sync_conversations").set_value("Artımlı")
        at.checkbox(key="tr_recreate_conversations").check()
        at.run()
        self.round_trip(at)
        out = at.session_state["out"]
        self.assertEqual(out["collection"], "conversations")
        self.assertEqual((out["sample"], out["batch"], out["allow_null"], out["mode"]), (20000, 3000, False, "incremental"))
        saved = self.saved()
        self.assertEqual((saved["sample"], saved["batch"], saved["allow_null"]), (20000, 3000, False))
        # Destructive options start unticked again, on purpose.
        self.assertFalse(at.checkbox(key="tr_recreate_conversations").value)

    def test_saved_full_scan_stays_zero(self):
        at = self.open({"table": "Conversations", "sample": 0, "batch": 1500})
        self.assertEqual(at.number_input(key="tr_sample_conversations").value, 0)
        self.assertEqual(at.number_input(key="tr_batch_conversations").value, 1500)
        self.round_trip(at)
        self.assertEqual(self.saved()["sample"], 0)

    def test_last_picked_collection_follows_between_pages(self):
        at = self.open()
        at.selectbox(key="tr_collection").select("other").run()
        at.session_state["page"] = "discovery"
        at.run()
        self.assertEqual(at.session_state["disc_out"], "other")
        at.selectbox(key="disc_collection").select("conversations").run()
        for page in ("transfer", "other", "transfer", "discovery"):
            at.session_state["page"] = page
            at.run()
            self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(at.session_state["disc_out"], "conversations")
        at.session_state["page"] = "transfer"
        at.run()
        self.assertEqual(at.session_state["out"]["collection"], "conversations")

    def test_date_range_survives_and_reopens(self):
        at = self.open()
        at.checkbox(key="tr_date_on_conversations").check().run()
        at.date_input(key="tr_date_start_conversations").set_value(date(2025, 2, 1))
        at.date_input(key="tr_date_end_conversations").set_value(date(2025, 2, 28))
        at.run()
        self.round_trip(at)
        dates = at.session_state["out"]["date_filter"]
        self.assertEqual((dates["enabled"], dates["start"], dates["end"]), (True, date(2025, 2, 1), date(2025, 2, 28)))

        reopened = self.open()  # a new session: from config.local.yaml
        dates = reopened.session_state["out"]["date_filter"]
        self.assertEqual((dates["start"], dates["end"]), (date(2025, 2, 1), date(2025, 2, 28)))


if __name__ == "__main__":
    unittest.main()
