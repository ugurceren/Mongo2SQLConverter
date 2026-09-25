"""Manual table names: key matching, clashes, and the "Tablo adları" card."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from streamlit.testing.v1 import AppTest

from core.run_job import TransferRequest, execute_transfer
from core.settings import parse_table_renames
from core.transfer import (
    apply_table_names,
    plan_table_problems,
    plan_tables,
    resolve_table_names,
    retarget_plan,
)
from tests.fixtures import documents, plan_for

PLAN, _ = plan_for(documents(200), "deep")
APP = Path(__file__).resolve().parent / "ui_table_names_app.py"


class ResolveTests(unittest.TestCase):
    def test_sources_as_the_card_shows_them(self):
        tables = plan_tables(apply_table_names(PLAN, {"messages": "ConvMessages", "messages[].attachments": "files 2024"}))
        self.assertEqual(tables[1:3], ["ConvMessages", "Files2024"])

    def test_array_markers_may_be_written_or_left_out(self):
        for key in ("messages[]", " messages[] "):
            with self.subTest(key=key):
                self.assertEqual(plan_tables(apply_table_names(PLAN, {key: "ConvMessages"}))[1], "ConvMessages")
        tables = plan_tables(apply_table_names(PLAN, {"messages.attachments": "Files"}))
        self.assertEqual(tables[2], "Files")

    def test_cli_flags_reach_the_table(self):
        names = parse_table_renames(["messages[]=ConvMessages"])
        self.assertEqual(plan_tables(apply_table_names(PLAN, names))[1], "ConvMessages")

    def test_exact_key_wins_and_unknown_keys_are_reported(self):
        resolved, unknown = resolve_table_names(PLAN, {"messages": "Exact", "messages[]": "Loose", "mesages": "Typo"})
        self.assertEqual(resolved, {"messages": "Exact"})
        self.assertEqual(unknown, ["messages[]", "mesages"])

    def test_root_follows_a_rename_and_keeps_child_overrides(self):
        plan = apply_table_names(retarget_plan(PLAN, root_table="Archive"), {"messages": "ConvMessages"})
        self.assertEqual(plan_tables(plan)[:3], ["Archive", "ConvMessages", "ArchiveMessagesAttachments"])


class ProblemTests(unittest.TestCase):
    def test_clean_plan(self):
        self.assertEqual(plan_table_problems(PLAN), [])

    def test_same_name_twice_even_in_another_case(self):
        for name in ("ConversationsTags", "conversationstags"):
            with self.subTest(name=name):
                problems = plan_table_problems(apply_table_names(PLAN, {"messages": name}))
                self.assertEqual(len(problems), 1)
                self.assertIn("messages, tags", problems[0])

    def test_checkpoint_table_name_is_reserved(self):
        problems = plan_table_problems(apply_table_names(PLAN, {"tags": "Mongo2SqlCheckpoint"}))
        self.assertIn("kontrol noktası", problems[0])

    def test_transfer_refuses_before_touching_sql(self):
        clashing = apply_table_names(PLAN, {"messages": "ConversationsTags"})
        with mock.patch("core.run_job.LiveSql") as live:
            with self.assertRaises(ValueError) as caught:
                execute_transfer(None, lambda: None, TransferRequest("conversations", clashing, "app"))
        live.assert_not_called()
        self.assertIn("ConversationsTags", str(caught.exception))


class CardTests(unittest.TestCase):
    """The card as a user drives it (Streamlit AppTest, no browser)."""

    def start(self, saved: dict | None = None, *, unfinished: bool = False) -> AppTest:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name) / "config.local.yaml"
        if saved is not None:
            self.config.write_text(yaml.safe_dump({"transfer": {"conversations": saved}}), encoding="utf-8")
        env = {"M2S_TEST_CONFIG": str(self.config)}
        if unfinished:
            env["M2S_TEST_UNFINISHED"] = "1"
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        at = AppTest.from_file(str(APP), default_timeout=120)
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    @staticmethod
    def rows(at):
        return [w for w in at.text_input if str(w.key or "").startswith("tr_tables_grid_")]

    @staticmethod
    def save_button(at):
        return next(b for b in at.button if str(b.key or "").startswith("tr_tables_save_"))

    def saved(self) -> dict:
        data = yaml.safe_load(self.config.read_text(encoding="utf-8")) or {}
        return data["transfer"]["conversations"]

    def test_rename_a_child_then_the_root(self):
        at = self.start()
        self.assertTrue(self.save_button(at).disabled)  # nothing to save yet
        self.rows(at)[1].set_value("ConvMessages").run()
        self.assertIn("Kaydedilmemiş değişiklik", " ".join(i.value for i in at.info))
        self.assertEqual(at.session_state["out_tables"][1], "ConversationsMessages")  # not before saving
        self.save_button(at).click().run()
        self.assertEqual(self.saved()["table_names"], {"messages": "ConvMessages"})
        self.assertEqual(at.session_state["out_tables"][1], "ConvMessages")
        self.assertIn("messages=ConvMessages", at.session_state["out_args"])

        self.rows(at)[0].set_value("archive").run()
        self.save_button(at).click().run()
        self.assertEqual(self.saved()["table"], "Archive")
        self.assertEqual(at.text_input(key="tr_table_conversations").value, "Archive")
        self.assertEqual(at.session_state["out_tables"][:3], ["Archive", "ConvMessages", "ArchiveMessagesAttachments"])

    def test_clashing_names_cannot_be_saved(self):
        at = self.start()
        for name in ("ConversationsTags", "conversationstags", "Mongo2SqlCheckpoint"):
            with self.subTest(name=name):
                self.rows(at)[1].set_value(name).run()
                self.assertTrue(at.error, "an error explains the clash")
                self.assertTrue(self.save_button(at).disabled)
                self.save_button(at).click().run()
                self.assertEqual(self.saved()["table_names"], {})
        self.assertEqual(len(set(at.session_state["out_tables"])), len(at.session_state["out_tables"]))

    def test_root_rename_that_would_clash_with_a_saved_name(self):
        at = self.start({"table": "Conversations", "table_names": {"messages": "Archive"}})
        self.rows(at)[0].set_value("Archive").run()
        self.assertTrue(at.error)
        self.assertTrue(self.save_button(at).disabled)

    def test_names_for_tables_not_in_this_plan_are_kept(self):
        at = self.start({"table": "Conversations", "table_names": {"somethingElse[]": "KeepMe"}})
        self.rows(at)[5].set_value("MyTags").run()
        self.save_button(at).click().run()
        self.assertEqual(self.saved()["table_names"], {"somethingElse[]": "KeepMe", "tags": "MyTags"})

    def test_clearing_a_name_goes_back_to_the_generated_one(self):
        at = self.start({"table": "Conversations", "table_names": {"messages": "ConvMessages"}})
        self.assertEqual(self.rows(at)[1].value, "ConvMessages")
        self.rows(at)[1].set_value("").run()
        self.save_button(at).click().run()
        self.assertEqual(self.saved()["table_names"], {})
        self.assertEqual(at.session_state["out_tables"][1], "ConversationsMessages")

    def test_unfinished_load_warns_before_a_rename(self):
        at = self.start(unfinished=True)
        self.rows(at)[1].set_value("ConvMessages").run()
        self.assertIn("yarım kalmış", " ".join(w.value for w in at.warning))


if __name__ == "__main__":
    unittest.main()
