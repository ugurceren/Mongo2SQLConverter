"""Mongo2SQLConverter — Streamlit UI."""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.inspect import (  # noqa: E402
    Profile,
    build_plan,
    detect_map_prefixes,
    iter_from_mongo,
    render_ddl,
    render_drdl,
)
from core.mongo import MongoClientWrapper  # noqa: E402
from core.mssql import available_drivers  # noqa: E402
from core.settings import (  # noqa: E402
    LOCAL_CONFIG_PATH,
    load_connection_overrides,
    load_settings,
    save_connection_overrides,
)


def _mongo_from_cfg(mongo_cfg: dict) -> MongoClientWrapper:
    return MongoClientWrapper(
        uri=mongo_cfg.get("uri") or "",
        database=mongo_cfg.get("database") or "",
        username=mongo_cfg.get("username") or None,
        password=mongo_cfg.get("password") or None,
    )


def fetch_collections(mongo_cfg: dict) -> list[str]:
    mongo = _mongo_from_cfg(mongo_cfg)
    try:
        mongo.connect()
        return mongo.list_collections()
    finally:
        mongo.close()

st.set_page_config(page_title="Mongo2SQL Converter", layout="wide")
st.title("Mongo2SQL Converter")
st.caption("Collection profille → DRDL / DDL")

cfg = load_settings()
mongo_cfg = cfg.get("mongodb") or {}
mssql_cfg = cfg.get("mssql") or {}
prof_cfg = cfg.get("profiler") or {}
stored = load_connection_overrides()
mongo_pw = (stored.get("mongodb") or {}).get("password") or mongo_cfg.get("password")

with st.expander("Baglanti ayarlari"):
    with st.form("conn"):
        c1, c2 = st.columns(2)
        with c1:
            m_uri = st.text_input("Mongo URI", value=mongo_cfg.get("uri") or "")
            m_db = st.text_input("Mongo DB", value=mongo_cfg.get("database") or "")
            m_user = st.text_input("Mongo user", value=mongo_cfg.get("username") or "")
            m_pw = st.text_input("Mongo sifre", type="password", placeholder="kayitli" if mongo_pw else "")
        with c2:
            s_server = st.text_input("MSSQL server", value=mssql_cfg.get("server") or "")
            s_db = st.text_input("MSSQL DB", value=mssql_cfg.get("database") or "")
            s_schema = st.text_input("Sema", value=mssql_cfg.get("schema") or "dbo")
            drivers = available_drivers()
            cur = mssql_cfg.get("driver") or drivers[0]
            s_driver = st.selectbox("Surucu", drivers, index=drivers.index(cur) if cur in drivers else 0)
            auth = st.radio("Auth", ["Windows", "SQL Server"], horizontal=True)
        if st.form_submit_button("Kaydet"):
            save_connection_overrides(
                mongodb={"uri": m_uri, "database": m_db, "username": m_user, "password": m_pw or None},
                mssql={
                    "server": s_server,
                    "database": s_db,
                    "schema": s_schema,
                    "driver": s_driver,
                    "trusted_connection": auth == "Windows",
                    "username": "",
                    "password": None,
                },
            )
            st.success(f"Kaydedildi: {LOCAL_CONFIG_PATH.name}")
            st.session_state.pop("collections", None)
            st.rerun()

st.subheader("Collection")
col_a, col_b = st.columns([3, 1])
with col_b:
    load_cols = st.button("Listeyi yukle", use_container_width=True)

if load_cols:
    if not mongo_cfg.get("uri") or not mongo_cfg.get("database"):
        st.error("Once baglanti ayarlarini kaydedin.")
    else:
        try:
            with st.spinner("Collection listesi aliniyor..."):
                st.session_state["collections"] = fetch_collections(mongo_cfg)
            st.success(f"{len(st.session_state['collections'])} collection bulundu.")
        except Exception as exc:
            st.session_state.pop("collections", None)
            st.error(f"Mongo baglantisi basarisiz: {exc}")

collections = st.session_state.get("collections") or []
if collections:
    default = "conversations" if "conversations" in collections else collections[0]
    idx = collections.index(default) if default in collections else 0
    with col_a:
        collection = st.selectbox("Collection", options=collections, index=idx, label_visibility="collapsed")
else:
    collection = None
    with col_a:
        st.info("Once **Listeyi yukle** ile Mongo'daki collection'lari cekin, sonra listeden secin.")

sample = st.number_input("Ornek (0=tam)", min_value=0, value=0, step=500)

if st.button("Semayi cikar", type="primary", disabled=not collection):
    if not mongo_cfg.get("uri"):
        st.error("Once baglanti kaydedin.")
    else:
        mongo = _mongo_from_cfg(mongo_cfg)
        try:
            mongo.connect()
            profile = Profile()
            with st.spinner("Profileniyor..."):
                for doc in iter_from_mongo(mongo, collection, int(sample)):
                    profile.add_document(doc)
            maps = detect_map_prefixes(
                profile,
                int(prof_cfg.get("map_min_keys", 30)),
                float(prof_cfg.get("map_max_fill", 0.2)),
            )
            plan = build_plan(
                profile,
                collection,
                mssql_cfg.get("schema", "dbo"),
                maps,
                float(prof_cfg.get("headroom", 1.5)),
            )
            drdl = render_drdl(plan, mongo_cfg["database"])
            ddl = render_ddl(plan)
            st.session_state.update(plan=plan, drdl=drdl, ddl=ddl)
            st.success(f"{profile.documents} dokuman profillendi")
        except Exception as exc:
            st.error(str(exc))
        finally:
            mongo.close()

if "drdl" in st.session_state:
    name = (st.session_state.get("plan") or {}).get("collection") or collection or "schema"
    t1, t2, t3 = st.tabs(["DRDL", "DDL", "Plan"])
    with t1:
        st.download_button("Indir DRDL", st.session_state["drdl"], f"{name}.drdl")
        st.code(st.session_state["drdl"], language="yaml")
    with t2:
        st.download_button("Indir DDL", st.session_state["ddl"], f"{name}.sql")
        st.code(st.session_state["ddl"], language="sql")
    with t3:
        st.json(st.session_state["plan"])
