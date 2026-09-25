"""Nesting is picked on Şema keşfi, saved, and only shown on SQL aktarımı."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent / "ui_nesting_app.py"


class NestingTests(unittest.TestCase):
    def start(self, page: str, saved_nesting: str | None = None) -> AppTest:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "config.local.yaml"
        if saved_nesting is not None:
            self.config.write_text(
                yaml.safe_dump({"transfer": {"conversations": {"nesting": saved_nesting, "table": "Conv"}}}),
                encoding="utf-8",
            )
        patcher = mock.patch.dict(os.environ, {"M2S_TEST_CONFIG": str(self.config), "M2S_TEST_PAGE": page})
        patcher.start()
        self.addCleanup(patcher.stop)
        at = AppTest.from_file(str(APP), default_timeout=120)
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    def saved(self) -> dict:
        return (yaml.safe_load(self.config.read_text(encoding="utf-8")) or {})["transfer"]["conversations"]

    @staticmethod
    def pick_buttons(at):
        return [b for b in at.button if str(b.key or "").startswith("nest_pick_")]

    @staticmethod
    def text(at) -> str:
        return " ".join(str(item.value) for item in (*at.markdown, *at.caption))

    def test_pick_on_discovery_is_saved_and_shown_read_only_on_transfer(self):
        at = self.start("discovery")
        self.assertEqual(at.session_state["out_nesting"], "hybrid")  # the shape's default
        next(b for b in self.pick_buttons(at) if b.key.endswith("_deep")).click().run()
        self.assertEqual(at.session_state["out_nesting"], "deep")
        self.assertEqual(self.saved()["nesting"], "deep")

        at.session_state["page"] = "transfer"
        at.run()
        self.assertEqual(self.pick_buttons(at), [], "no chooser on the transfer page")
        self.assertEqual(at.session_state["out_nesting"], "deep")
        self.assertIn("**Derin ilişkisel**", self.text(at))
        self.assertNotIn("varsayılan", self.text(at))

    def test_saved_choice_is_used_in_a_new_session(self):
        at = self.start("transfer", saved_nesting="columns")
        self.assertEqual(at.session_state["out_nesting"], "columns")
        self.assertIn("**Tek tablo + JSON**", self.text(at))
        self.assertNotIn("varsayılan", self.text(at))

    def test_without_a_choice_the_default_is_labelled(self):
        at = self.start("transfer")
        self.assertEqual(at.session_state["out_nesting"], "hybrid")
        self.assertIn("varsayılan", self.text(at))

    def test_saving_keeps_the_other_preferences(self):
        at = self.start("discovery", saved_nesting="hybrid")
        next(b for b in self.pick_buttons(at) if b.key.endswith("_columns")).click().run()
        self.assertEqual(self.saved()["nesting"], "columns")
        self.assertEqual(self.saved()["table"], "Conv")

    def test_unknown_saved_value_falls_back_to_the_default(self):
        at = self.start("transfer", saved_nesting="weird")
        self.assertEqual(at.session_state["out_nesting"], "hybrid")


if __name__ == "__main__":
    unittest.main()
