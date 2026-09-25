"""
Streamlit script for `tests/test_nesting_choice.py`: the Şema keşfi chooser or
the SQL aktarımı summary (M2S_TEST_PAGE), on a fixture shape without Mongo.
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
from core.inspect import Profile, describe_shape  # noqa: E402
from tests.fixtures import documents  # noqa: E402

COLLECTION = "conversations"
cache = st.session_state.setdefault(services.SHAPE_KEY, {})
if f"{COLLECTION}:{services.PEEK_SAMPLE}" not in cache:
    profile = Profile()
    for doc in documents(200):
        profile.add_document(doc)
    cache[f"{COLLECTION}:{services.PEEK_SAMPLE}"] = describe_shape(profile)

settings = SimpleNamespace(mongo_ready=False, mongo={}, schema="dbo")
page = st.session_state.get("page") or os.environ.get("M2S_TEST_PAGE", "discovery")
if page == "discovery":
    st.session_state["out_nesting"] = services.nesting_card(settings, COLLECTION, "Conversations")
else:
    st.session_state["out_nesting"] = services.nesting_summary(settings, COLLECTION, "Conversations")
