"""
Streamlit script for `tests/test_page_state.py`: the SQL aktarımı Hedef /
Senkron / Tarih cards on a fixture shape, without Mongo or SQL Server. With
`page` set to "other" nothing is drawn, as when the user is on another page.
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

from app.ui import services  # noqa: E402
from app.ui import transfer as page  # noqa: E402
from core.inspect import Profile, describe_shape  # noqa: E402
from tests.fixtures import documents  # noqa: E402

COLLECTION = "conversations"
cache = st.session_state.setdefault(services.SHAPE_KEY, {})
if f"{COLLECTION}:{services.PEEK_SAMPLE}" not in cache:
    profile = Profile()
    for doc in documents(200):
        profile.add_document(doc)
    cache[f"{COLLECTION}:{services.PEEK_SAMPLE}"] = describe_shape(profile)
if "picked" not in st.session_state:
    # As when the collection was chosen on Şema keşfi first.
    services.remember_collection(COLLECTION)
    st.session_state["picked"] = True

settings = SimpleNamespace(
    schema="dbo", mongo_ready=False, mongo={}, mssql={}, sql_ready=False, mssql_password=None
)
if st.session_state.get("page", "transfer") == "transfer":
    options = page._target_card(settings, [COLLECTION, "other"])
    if options.get("collection"):
        options["columns"] = page._prefs(COLLECTION)["columns"]
        page._remember_prefs(COLLECTION, options)
    st.session_state["out"] = {
        key: options.get(key)
        for key in ("collection", "sample", "batch", "allow_null", "mode", "date_filter")
    }
elif st.session_state.get("page") == "discovery":
    from app.ui import discovery

    st.session_state["disc_out"], _ = discovery._pick_collection(settings, [COLLECTION, "other"])
else:
    st.write("Bağlantılar")
