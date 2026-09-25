"""
Streamlit script for `tests/test_table_names.py`: the transfer page's Kök tablo
input and "Tablo adları" card on a fixture plan, without Mongo or SQL Server.
Preferences go to the file named by M2S_TEST_CONFIG, never config.local.yaml.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

import core.settings as settings_module  # noqa: E402

settings_module.LOCAL_CONFIG_PATH = Path(os.environ["M2S_TEST_CONFIG"])

from app.ui import transfer as page  # noqa: E402
from core.inspect import sql_table_ident  # noqa: E402
from core.settings import load_transfer_prefs  # noqa: E402
from core.transfer import plan_tables, retarget_plan  # noqa: E402
from tests.fixtures import documents, plan_for  # noqa: E402

COLLECTION = "conversations"
if "fixture_plan" not in st.session_state:
    st.session_state["fixture_plan"] = plan_for(documents(200), "deep")[0]

prefs = load_transfer_prefs(COLLECTION)
st.session_state[page.PREFS_STAMP] = COLLECTION
page._sync_table_widget(COLLECTION, prefs)
table = st.text_input("Kök tablo", key=page._table_widget_key(COLLECTION))
options = {
    "collection": COLLECTION,
    "schema": "dbo",
    "table": sql_table_ident(table) if (table or "").strip() else "",
    "table_names": dict(prefs.get("table_names") or {}),
    "date_filter": prefs["date_filter"],
    "columns": prefs["columns"],
    "nesting": "deep",
    "sample": 5000,
    "batch": 2000,
    "allow_null": True,
    "schedule_mode": "auto",
    "unfinished": bool(os.environ.get("M2S_TEST_UNFINISHED")),
}
plan = retarget_plan(st.session_state["fixture_plan"], schema="dbo", root_table=options["table"])
selected = page._tables_card(SimpleNamespace(mssql={"database": "app"}), options, plan)
page._remember_prefs(COLLECTION, options)
st.session_state["out_tables"] = plan_tables(selected)
st.session_state["out_args"] = page._scheduler_args(COLLECTION, "auto", options["table"], options["table_names"])
