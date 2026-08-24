"""Application shell: styling, top bar and small layout helpers."""

from __future__ import annotations

from typing import Iterable, Literal, Sequence

import re
import streamlit as st
import streamlit.components.v1 as components

State = Literal["ok", "warn", "off"]

CSS = """
<style>
:root {
    --m2s-accent: #4c8dff;
    --m2s-accent-soft: rgba(76, 141, 255, 0.14);
    --m2s-border: rgba(240, 246, 252, 0.10);
    --m2s-muted: #8b98a9;
    --m2s-ok: #3fb950;
    --m2s-warn: #d29922;
    --m2s-off: #6e7681;
    --m2s-rail: 104px;
    --m2s-topbar: 56px;
    --m2s-sidebar: 248px;
    --m2s-nav: 176px;
}

/* Keep the toolbar mounted: it hosts the button that reopens the sidebar. */
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], footer { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stHeader"] {
    background: transparent !important;
    height: 0 !important;
    min-height: 0 !important;
    border: 0 !important;
    pointer-events: none !important;
    overflow: visible !important;
}
.stApp {
    top: 0 !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 0 !important;
    height: auto !important;
    max-height: none !important;
    box-sizing: border-box !important;
}
[data-testid="stAppViewContainer"] {
    height: 100% !important;
}

/* Collapse / expand live in the top bar; keep Streamlit's buttons for JS clicks. */
[data-testid="stSidebar"][aria-expanded="true"] [data-testid="stSidebarHeader"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stExpandSidebarButton"],
[data-testid="stExpandSidebarButton"] button,
[data-testid="collapsedControl"] {
    position: fixed !important;
    left: -9999px !important;
    top: 0 !important;
    width: 1px !important;
    height: 1px !important;
    min-height: 0 !important;
    overflow: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
}
[data-testid="stSidebar"][aria-expanded="true"] [data-testid="stLogoSpacer"] {
    display: none !important;
}

section[data-testid="stMain"] .block-container {
    max-width: 1220px;
    padding-top: calc(var(--m2s-topbar) + 0.85rem);
    padding-bottom: 4rem;
}

/* ---------- top bar ---------- */
.m2s-topbar {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    height: var(--m2s-topbar);
    z-index: 1000020;
    display: flex;
    align-items: stretch;
    padding: 0;
    box-sizing: border-box;
    background: #161b22;
    border-bottom: 1px solid rgba(240, 246, 252, 0.10);
    color: #e6edf3;
    pointer-events: auto;
}
.m2s-topbar-brand {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    flex: 0 0 var(--m2s-sidebar);
    width: var(--m2s-sidebar);
    box-sizing: border-box;
    padding: 0 8px 0 12px;
    background: #0d1117;
    border-right: 1px solid rgba(240, 246, 252, 0.10);
    min-width: 0;
}
.m2s-topbar.is-collapsed .m2s-topbar-brand {
    flex-basis: var(--m2s-rail);
    width: var(--m2s-rail);
    justify-content: center;
    gap: 0.28rem;
    padding: 0 6px;
}
.m2s-topbar .m2s-logo {
    width: 32px;
    height: 32px;
    flex: 0 0 32px;
    border-radius: 9px;
    font-size: 0.62rem;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 0 1px rgba(123, 92, 255, 0.35), 0 6px 16px rgba(76, 141, 255, 0.32);
}
.m2s-topbar.is-collapsed .m2s-logo {
    width: 34px;
    height: 34px;
    flex: 0 0 34px;
    font-size: 0.58rem;
    border-radius: 9px;
}
.m2s-topbar-name {
    font-weight: 750;
    font-size: 0.86rem;
    line-height: 1.1;
    letter-spacing: -0.03em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
}
.m2s-topbar.is-collapsed .m2s-topbar-name { display: none; }
.m2s-topbar-main {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0 14px 0 12px;
}
.m2s-topbar-toggle {
    flex: 0 0 28px;
    width: 28px;
    height: 28px;
    margin: 0;
    padding: 0;
    border-radius: 7px;
    border: 1px solid rgba(240, 246, 252, 0.12);
    background: rgba(240, 246, 252, 0.06);
    color: #e6edf3;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}
.m2s-topbar-toggle:hover {
    background: var(--m2s-accent-soft);
    border-color: rgba(76, 141, 255, 0.45);
}
.m2s-topbar-toggle svg {
    width: 16px;
    height: 16px;
    display: block;
    stroke: currentColor;
    fill: none;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
}
.m2s-topbar-toggle-sep {
    width: 1px;
    height: 22px;
    margin: 0 0.55rem 0 0.35rem;
    background: rgba(240, 246, 252, 0.12);
    flex: 0 0 1px;
}
.m2s-topbar-crumbs {
    display: flex;
    align-items: center;
    font-size: 0.86rem;
    white-space: nowrap;
    min-width: 0;
}
.m2s-crumb-root,
.m2s-crumb-sep {
    color: var(--m2s-muted);
    font-weight: 500;
}
.m2s-crumb-sep { margin: 0 0.45rem; }
.m2s-crumb-page {
    font-weight: 750;
    color: #e6edf3;
}
.m2s-crumb-page[data-page="connections"] { color: #3fb950; }
.m2s-crumb-page[data-page="discovery"] { color: #22d3ee; }
.m2s-crumb-page[data-page="transfer"] { color: #fb923c; }
.m2s-topbar-actions {
    margin-left: auto;
    display: flex;
    align-items: center;
}

/* ---------- page header ---------- */
.m2s-eyebrow {
    font-size: 0.72rem;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--m2s-muted);
    margin-bottom: 0.35rem;
}
.m2s-title {
    font-size: 1.65rem;
    font-weight: 650;
    line-height: 1.2;
    margin: 0;
    padding-left: 0.85rem;
    border-left: 4px solid var(--m2s-accent);
}
.m2s-title-connections { border-left-color: #3fb950; }
.m2s-title-discovery { border-left-color: #22d3ee; }
.m2s-title-transfer { border-left-color: #fb923c; }
.m2s-lede {
    color: var(--m2s-muted);
    font-size: 0.93rem;
    margin: 0.4rem 0 1.1rem 0;
    max-width: 68ch;
}

/* ---------- stepper ---------- */
.m2s-stepper {
    display: flex; align-items: center; gap: 0.4rem;
    margin: 0.15rem 0 1.45rem 0;
    overflow: visible;
}
.m2s-step {
    display: flex; align-items: center; gap: 0.45rem;
    padding: 0.38rem 0.78rem;
    border-radius: 999px;
    border: 1px solid var(--m2s-border);
    color: var(--m2s-muted);
    background: rgba(240, 246, 252, 0.04);
    font-size: 0.78rem;
    font-weight: 600;
    white-space: nowrap;
    opacity: 0.58;
}
.m2s-step-n {
    width: 1.15rem; height: 1.15rem; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 0.68rem; font-weight: 750;
    background: rgba(240, 246, 252, 0.08);
    color: var(--m2s-muted);
}
.m2s-step-now {
    font-size: 0.62rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-weight: 800;
    padding: 0.08rem 0.4rem;
    border-radius: 999px;
    background: rgba(4, 16, 24, 0.35);
}
.m2s-step.done { opacity: 0.82; }
.m2s-step-connections.done {
    color: #9ee3a8;
    border-color: rgba(63, 185, 80, 0.35);
    background: rgba(63, 185, 80, 0.10);
}
.m2s-step-connections.done .m2s-step-n { background: rgba(63, 185, 80, 0.28); color: #b6f0be; }
.m2s-step-connections.active {
    opacity: 1;
    color: #041018;
    border-color: #3fb950;
    background: #3fb950;
    font-weight: 800;
    font-size: 0.86rem;
    padding: 0.48rem 0.95rem;
    box-shadow: 0 0 0 1px rgba(63, 185, 80, 0.45), 0 0 22px rgba(63, 185, 80, 0.35);
}
.m2s-step-connections.active .m2s-step-n { background: #041018; color: #9ee3a8; }
.m2s-step-connections.active .m2s-step-now { background: rgba(4, 16, 24, 0.28); color: #041018; }
.m2s-step-discovery.done {
    color: #7af0ff;
    border-color: rgba(34, 211, 238, 0.35);
    background: rgba(34, 211, 238, 0.10);
}
.m2s-step-discovery.done .m2s-step-n { background: rgba(34, 211, 238, 0.28); color: #b8f7ff; }
.m2s-step-discovery.active {
    opacity: 1;
    color: #041018;
    border-color: #22d3ee;
    background: #22d3ee;
    font-weight: 800;
    font-size: 0.86rem;
    padding: 0.48rem 0.95rem;
    box-shadow: 0 0 0 1px rgba(34, 211, 238, 0.45), 0 0 22px rgba(34, 211, 238, 0.35);
}
.m2s-step-discovery.active .m2s-step-n { background: #041018; color: #7af0ff; }
.m2s-step-discovery.active .m2s-step-now { background: rgba(4, 16, 24, 0.28); color: #041018; }
.m2s-step-transfer.done {
    color: #ffd08a;
    border-color: rgba(251, 146, 60, 0.35);
    background: rgba(251, 146, 60, 0.10);
}
.m2s-step-transfer.done .m2s-step-n { background: rgba(251, 146, 60, 0.28); color: #ffe0b0; }
.m2s-step-transfer.active {
    opacity: 1;
    color: #041018;
    border-color: #fb923c;
    background: #fb923c;
    font-weight: 800;
    font-size: 0.86rem;
    padding: 0.48rem 0.95rem;
    box-shadow: 0 0 0 1px rgba(251, 146, 60, 0.45), 0 0 22px rgba(251, 146, 60, 0.35);
}
.m2s-step-transfer.active .m2s-step-n { background: #041018; color: #ffd08a; }
.m2s-step-transfer.active .m2s-step-now { background: rgba(4, 16, 24, 0.28); color: #041018; }
.m2s-step-line {
    flex: 1; height: 1px; background: var(--m2s-border); min-width: 0.8rem;
}

.st-key-cta_conn, .st-key-cta_disc, .st-key-cta_sql,
.st-key-cta_to_connections, .st-key-cta_to_discovery, .st-key-cta_to_transfer {
    max-width: 280px;
}
.st-key-cta_conn [data-testid="stPageLink"] a,
.st-key-cta_disc [data-testid="stPageLink"] a,
.st-key-cta_sql [data-testid="stPageLink"] a,
.st-key-cta_to_connections [data-testid="stPageLink"] a,
.st-key-cta_to_discovery [data-testid="stPageLink"] a,
.st-key-cta_to_transfer [data-testid="stPageLink"] a {
    display: inline-flex !important;
    align-items: center;
    gap: 0.4rem;
    border-radius: 8px !important;
    padding: 0.55rem 0.95rem !important;
    font-weight: 650 !important;
    text-decoration: none !important;
    border: 1px solid rgba(76, 141, 255, 0.45) !important;
    background: var(--m2s-accent-soft) !important;
    color: #dbe8ff !important;
}
.st-key-cta_sql [data-testid="stPageLink"] a,
.st-key-cta_to_transfer [data-testid="stPageLink"] a {
    border-color: rgba(251, 146, 60, 0.5) !important;
    background: linear-gradient(135deg, rgba(251, 146, 60, 0.28), rgba(245, 158, 11, 0.12)) !important;
    color: #ffd08a !important;
}

/* ---------- nesting option cards ---------- */
.m2s-nest-title {
    font-size: 1.08rem;
    font-weight: 720;
    letter-spacing: -0.02em;
    margin: 0 0 0.4rem 0;
}
.m2s-nest-count {
    font-size: 0.72rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--m2s-muted);
    font-weight: 700;
    margin: 0 0 0.35rem 0.15rem;
}
.m2s-table-preview {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.78rem;
    line-height: 1.45;
    color: #c8d1dc;
    margin: 0.35rem 0 0.15rem 0;
    white-space: pre-wrap;
}
[class*="st-key-nest_on_"] [data-testid="stVerticalBlockBorderWrapper"] {
    border-color: rgba(76, 141, 255, 0.65) !important;
    box-shadow: 0 0 0 1px rgba(76, 141, 255, 0.22);
}

/* ---------- cards ---------- */
[data-testid="stVerticalBlockBorderWrapper"],
[data-testid="stMain"] [data-testid="stLayoutWrapper"] > [data-testid="stVerticalBlock"] {
    border-radius: 12px;
    border-color: var(--m2s-border) !important;
}
.m2s-card-title {
    font-size: 0.74rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--m2s-muted);
    font-weight: 600;
    margin-bottom: 0.15rem;
}
.m2s-card-hint {
    color: var(--m2s-muted);
    font-size: 0.85rem;
    margin: 0 0 0.9rem 0;
}
.m2s-section-kicker {
    font-size: 0.72rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #7af0ff;
    font-weight: 700;
    margin-bottom: 0.2rem;
}
.m2s-section-title {
    font-size: 1.45rem;
    font-weight: 750;
    letter-spacing: -0.03em;
    line-height: 1.15;
    margin: 0 0 0.35rem 0;
}

/* ---------- sidebar ---------- */
section[data-testid="stSidebar"] {
    box-sizing: border-box !important;
    background-color: #0d1117 !important;
    border-right: 1px solid rgba(240, 246, 252, 0.10);
    min-width: var(--m2s-sidebar) !important;
    width: var(--m2s-sidebar) !important;
    max-width: var(--m2s-sidebar) !important;
}
section[data-testid="stSidebar"] > div,
[data-testid="stSidebarContent"] {
    width: 100% !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
}
[data-testid="stSidebar"] .block-container { padding-top: calc(var(--m2s-topbar) + 0.35rem); }
[data-testid="stSidebar"][aria-expanded="true"] [data-testid="stSidebarUserContent"] {
    padding-top: calc(var(--m2s-topbar) + 0.5rem) !important;
}
[data-testid="stSidebarNav"] { display: none !important; }

.m2s-logo {
    width: 46px; height: 46px; border-radius: 13px;
    background: linear-gradient(145deg, #4c8dff 0%, #7b5cff 52%, #22d3ee 100%);
    color: #fff; font-weight: 800; font-size: 0.78rem; letter-spacing: 0.04em;
    display: flex; align-items: center; justify-content: center;
    flex: 0 0 46px;
    box-shadow: 0 0 0 1px rgba(123, 92, 255, 0.35), 0 8px 22px rgba(76, 141, 255, 0.38);
}
.st-key-nav_schema, .st-key-nav_sql, .st-key-nav_conn,
.st-key-nav_schema_on, .st-key-nav_sql_on, .st-key-nav_conn_on,
.st-key-nav_schema_off, .st-key-nav_sql_off, .st-key-nav_conn_off {
    margin: 0 0 0.45rem 0;
    width: var(--m2s-nav) !important;
    max-width: 100%;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] {
    width: var(--m2s-nav) !important;
    max-width: 100%;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a,
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] {
    display: flex !important; align-items: center; gap: 0.55rem;
    box-sizing: border-box !important;
    width: var(--m2s-nav) !important;
    min-width: var(--m2s-nav) !important;
    max-width: var(--m2s-nav) !important;
    height: 44px !important;
    border-radius: 10px !important;
    padding: 0.55rem 0.85rem !important;
    font-weight: 700 !important;
    font-size: 0.92rem !important;
    letter-spacing: -0.01em;
    text-decoration: none !important;
    white-space: nowrap;
    transition: transform 0.14s ease, box-shadow 0.14s ease, filter 0.14s ease !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover,
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"]:hover {
    transform: translateX(3px);
    filter: none;
}
.st-key-nav_schema_off [data-testid="stPageLink"] a:hover,
.st-key-nav_sql_off [data-testid="stPageLink"] a:hover,
.st-key-nav_conn_off [data-testid="stPageLink"] a:hover {
    color: #e6edf3 !important;
    background: rgba(240, 246, 252, 0.10) !important;
    border-color: rgba(240, 246, 252, 0.22) !important;
}
.st-key-nav_schema_off [data-testid="stPageLink"] a:hover [data-testid="stIconMaterial"],
.st-key-nav_schema_off [data-testid="stPageLink"] a:hover p,
.st-key-nav_sql_off [data-testid="stPageLink"] a:hover [data-testid="stIconMaterial"],
.st-key-nav_sql_off [data-testid="stPageLink"] a:hover p,
.st-key-nav_conn_off [data-testid="stPageLink"] a:hover [data-testid="stIconMaterial"],
.st-key-nav_conn_off [data-testid="stPageLink"] a:hover p { color: #e6edf3 !important; }
[data-testid="stSidebar"] [data-testid="stPageLink"] [data-testid="stIconMaterial"] {
    font-size: 1.28rem !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] p {
    margin: 0 !important;
}

.st-key-nav_schema_off [data-testid="stPageLink"] a,
.st-key-nav_sql_off [data-testid="stPageLink"] a,
.st-key-nav_conn_off [data-testid="stPageLink"] a,
.st-key-nav_schema [data-testid="stPageLink"] a,
.st-key-nav_sql [data-testid="stPageLink"] a,
.st-key-nav_conn [data-testid="stPageLink"] a {
    color: #9aa6b4 !important;
    background: rgba(240, 246, 252, 0.04) !important;
    border: 1px solid var(--m2s-border) !important;
    box-shadow: none !important;
    font-weight: 600 !important;
}
.st-key-nav_schema_off [data-testid="stIconMaterial"],
.st-key-nav_schema_off [data-testid="stPageLink"] p,
.st-key-nav_sql_off [data-testid="stIconMaterial"],
.st-key-nav_sql_off [data-testid="stPageLink"] p,
.st-key-nav_conn_off [data-testid="stIconMaterial"],
.st-key-nav_conn_off [data-testid="stPageLink"] p,
.st-key-nav_schema [data-testid="stIconMaterial"],
.st-key-nav_schema [data-testid="stPageLink"] p,
.st-key-nav_sql [data-testid="stIconMaterial"],
.st-key-nav_sql [data-testid="stPageLink"] p,
.st-key-nav_conn [data-testid="stIconMaterial"],
.st-key-nav_conn [data-testid="stPageLink"] p { color: #9aa6b4 !important; }

.st-key-nav_schema_on [data-testid="stPageLink"] a,
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] {
    color: #041018 !important;
    background: linear-gradient(135deg, #22d3ee, #67e8f9) !important;
    border: 1px solid #67e8f9 !important;
    box-shadow: inset 4px 0 0 #ecfeff, 0 0 0 1px rgba(34, 211, 238, 0.35), 0 10px 24px rgba(34, 211, 238, 0.32) !important;
    font-weight: 800 !important;
}
.st-key-nav_schema_on [data-testid="stIconMaterial"],
.st-key-nav_schema_on [data-testid="stPageLink"] p,
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] [data-testid="stIconMaterial"],
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] p { color: #041018 !important; }

.st-key-nav_sql_on [data-testid="stPageLink"] a,
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] {
    color: #041018 !important;
    background: linear-gradient(135deg, #fb923c, #fdba74) !important;
    border: 1px solid #fdba74 !important;
    box-shadow: inset 4px 0 0 #fff7ed, 0 0 0 1px rgba(251, 146, 60, 0.35), 0 10px 24px rgba(251, 146, 60, 0.32) !important;
    font-weight: 800 !important;
}
.st-key-nav_sql_on [data-testid="stIconMaterial"],
.st-key-nav_sql_on [data-testid="stPageLink"] p,
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] [data-testid="stIconMaterial"],
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] p { color: #041018 !important; }

.st-key-nav_conn_on [data-testid="stPageLink"] a,
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] {
    color: #041018 !important;
    background: linear-gradient(135deg, #3fb950, #9ee3a8) !important;
    border: 1px solid #9ee3a8 !important;
    box-shadow: inset 4px 0 0 #f0fdf4, 0 0 0 1px rgba(63, 185, 80, 0.35), 0 10px 24px rgba(63, 185, 80, 0.32) !important;
    font-weight: 800 !important;
}
.st-key-nav_conn_on [data-testid="stIconMaterial"],
.st-key-nav_conn_on [data-testid="stPageLink"] p,
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] [data-testid="stIconMaterial"],
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] p { color: #041018 !important; }

.m2s-side-block { margin-top: 1.15rem; }
.m2s-side-label {
    font-size: 0.7rem; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--m2s-muted); font-weight: 600; margin-bottom: 0.5rem;
}
.m2s-status { display: flex; align-items: center; gap: 0.55rem; padding: 0.28rem 0; font-size: 0.85rem; }
.m2s-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 8px; }
.m2s-dot.ok { background: var(--m2s-ok); box-shadow: 0 0 0 3px rgba(63, 185, 80, 0.16); }
.m2s-dot.warn { background: var(--m2s-warn); box-shadow: 0 0 0 3px rgba(210, 153, 34, 0.16); }
.m2s-dot.off { background: var(--m2s-off); }
.m2s-status-text .m2s-status-value { color: var(--m2s-muted); }
.m2s-side-foot {
    margin-top: 1.6rem; padding-top: 0.8rem;
    border-top: 1px solid var(--m2s-border);
    color: var(--m2s-muted); font-size: 0.76rem;
}

/* ---------- collapsed sidebar keeps a narrow icon rail ---------- */
[data-testid="stSidebar"][aria-expanded="false"] {
    min-width: var(--m2s-rail) !important;
    max-width: var(--m2s-rail) !important;
    width: var(--m2s-rail) !important;
    transform: none !important;
    overflow: visible !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarHeader"] {
    position: absolute !important;
    top: 0 !important;
    left: 0 !important;
    width: 0 !important;
    height: 0 !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    pointer-events: none !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarContent"] {
    padding-top: calc(var(--m2s-topbar) + 0.5rem) !important;
    padding-left: 0.55rem !important;
    padding-right: 0.55rem !important;
    overflow-x: visible !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarUserContent"] {
    padding-top: 0 !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] {
    display: none !important;
}
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status { display: none !important; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-label,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status-text,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-foot,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-rail-hide { display: none !important; }
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_schema,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_sql,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_conn,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_schema_on,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_sql_on,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_conn_on,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_schema_off,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_sql_off,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_conn_off,
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] {
    width: 52px !important;
    min-width: 52px !important;
    max-width: 52px !important;
    margin-left: 0 !important;
    margin-right: auto !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a,
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink-NavLink"] {
    justify-content: center !important;
    align-items: center !important;
    width: 52px !important;
    min-width: 52px !important;
    max-width: 52px !important;
    height: 52px !important;
    min-height: 52px !important;
    padding: 0 !important;
    margin: 0 !important;
    gap: 0 !important;
    border-radius: 12px !important;
    transform: none !important;
    overflow: visible !important;
    font-size: inherit !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a:hover {
    transform: none !important;
    filter: none;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a p,
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] [data-testid="stMarkdownContainer"],
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a > span:last-child {
    display: none !important;
    width: 0 !important;
    height: 0 !important;
    overflow: hidden !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a > span:first-child,
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] [data-testid="stIconMaterial"],
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] [data-testid="stIcon"] {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    width: 28px !important;
    min-width: 28px !important;
    height: 28px !important;
    min-height: 28px !important;
    font-size: 28px !important;
    line-height: 28px !important;
    opacity: 1 !important;
    visibility: visible !important;
    overflow: visible !important;
}
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_conn_on [data-testid="stPageLink"] a,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_schema_on [data-testid="stPageLink"] a,
[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_sql_on [data-testid="stPageLink"] a {
    transform: none !important;
    box-shadow: none !important;
}

/* ---------- controls ---------- */
.stButton button, .stDownloadButton button, [data-testid="stFormSubmitButton"] button {
    border-radius: 8px;
    font-weight: 550;
}
.st-key-mongo_test button,
.st-key-sql_test button {
    background-color: var(--m2s-accent-soft) !important;
    color: #cfe0ff !important;
    border: 1px solid rgba(76, 141, 255, 0.45) !important;
}
.st-key-mongo_test button:hover,
.st-key-sql_test button:hover {
    background-color: rgba(76, 141, 255, 0.24) !important;
    border-color: rgba(76, 141, 255, 0.7) !important;
}
.m2s-theme-dock {
    position: relative !important;
    top: auto !important;
    right: auto !important;
    z-index: 1;
    width: 68px;
    max-width: 68px;
    height: 30px;
    margin: 0;
    padding: 2px;
    box-sizing: border-box;
    display: flex;
    align-items: stretch;
    gap: 0;
    border-radius: 999px;
    background: #1e2229;
    border: 1px solid #2b303b;
    pointer-events: auto;
}
.m2s-theme-btn {
    flex: 1 1 0;
    min-width: 0;
    height: 26px;
    padding: 0;
    border: 0;
    border-radius: 999px;
    background: transparent;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
}
.m2s-theme-btn svg {
    width: 13px;
    height: 13px;
    display: block;
    stroke: #888da8;
    fill: none;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
}
.m2s-theme-dock[data-mode="dark"] .m2s-theme-moon {
    background: #2b303b;
}
.m2s-theme-dock[data-mode="dark"] .m2s-theme-moon svg { stroke: #ffffff; }
.m2s-theme-dock[data-mode="light"] {
    background: #f0f2f5;
    border-color: #d8dce2;
}
.m2s-theme-dock[data-mode="light"] .m2s-theme-sun {
    background: #ffffff;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.10);
}
.m2s-theme-dock[data-mode="light"] .m2s-theme-moon svg,
.m2s-theme-dock[data-mode="light"] .m2s-theme-sun svg { stroke: #333333; }
.stElementContainer:has(.m2s-theme-dock),
.stHtml:has(.m2s-theme-dock),
.stHtml:has(.m2s-theme-boot),
.stElementContainer:has(.m2s-theme-boot) {
    height: 0 !important;
    min-height: 0 !important;
    max-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    border: 0 !important;
    position: absolute !important;
    pointer-events: none !important;
}

.st-key-disc_database_drdl button {
    background: linear-gradient(135deg, #4c8dff, #22d3ee) !important;
    color: #041018 !important;
    border: 0 !important;
}

[data-testid="stMetricLabel"] p {
    font-size: 0.74rem !important;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--m2s-muted) !important;
}
[data-testid="stMetricValue"] { font-size: 1.32rem; }

.stTabs [data-baseweb="tab-list"] { gap: 0.4rem; }
.stTabs [data-baseweb="tab"] { padding: 0.4rem 0.9rem; }

[data-testid="stTextInput"] input::placeholder,
[data-testid="stNumberInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder,
[data-baseweb="input"] input::placeholder,
[data-baseweb="textarea"] textarea::placeholder {
    color: var(--m2s-muted) !important;
    -webkit-text-fill-color: var(--m2s-muted) !important;
    opacity: 0.55 !important;
    font-weight: 400 !important;
}
[data-testid="stTextInput"] input:focus::placeholder,
[data-testid="stNumberInput"] input:focus::placeholder,
[data-testid="stTextArea"] textarea:focus::placeholder,
[data-baseweb="input"] input:focus::placeholder,
[data-baseweb="textarea"] textarea:focus::placeholder,
[data-testid="stTextInput"]:focus-within input::placeholder,
[data-testid="stNumberInput"]:focus-within input::placeholder,
[data-testid="stTextArea"]:focus-within textarea::placeholder,
input:focus::placeholder,
textarea:focus::placeholder {
    color: transparent !important;
    -webkit-text-fill-color: transparent !important;
    opacity: 0 !important;
}

code, pre, .stCode { font-size: 0.82rem; }
iframe[height="0"], iframe[height="1"] { display: none !important; }
</style>
"""

LIGHT_CSS = """
<style>
:root {
    --m2s-accent: #0969da;
    --m2s-accent-soft: rgba(9, 105, 218, 0.12);
    --m2s-border: #c5ccd4;
    --m2s-muted: #57606a;
    --m2s-ok: #1a7f37;
    --m2s-warn: #9a6700;
    --m2s-off: #8c959f;
}

.stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background-color: #f4f6f8 !important;
    color: #1f2328 !important;
    color-scheme: light;
}
[data-testid="stHeader"] { background: transparent !important; }

.m2s-topbar {
    background: #f6f8fa;
    border-bottom-color: rgba(31, 35, 40, 0.12);
    color: #1f2328;
}
.m2s-topbar-brand {
    background: #ffffff;
    border-right-color: rgba(31, 35, 40, 0.12);
}
.m2s-topbar-toggle-sep { background: rgba(31, 35, 40, 0.12); }
.m2s-topbar-toggle {
    border-color: rgba(31, 35, 40, 0.16);
    background: #ffffff;
    color: #1f2328;
}
.m2s-topbar-toggle:hover {
    background: var(--m2s-accent-soft);
    border-color: rgba(9, 105, 218, 0.45);
}
.m2s-topbar-name { color: #1f2328; }
.m2s-crumb-root,
.m2s-crumb-sep { color: #57606a; }
.m2s-crumb-page { color: #1f2328; }
.m2s-crumb-page[data-page="connections"] { color: #16a34a; }
.m2s-crumb-page[data-page="discovery"] { color: #0e7490; }
.m2s-crumb-page[data-page="transfer"] { color: #c2410c; }
section[data-testid="stSidebar"] {
    background-color: #ffffff !important;
    color: #1f2328 !important;
    border-right-color: var(--m2s-border) !important;
}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] span {
    color: #1f2328 !important;
}

.m2s-title, .m2s-section-title { color: #1f2328; }
[data-testid="stVerticalBlockBorderWrapper"],
[data-testid="stMain"] [data-testid="stLayoutWrapper"] > [data-testid="stVerticalBlock"] {
    border: 1px solid #8c959f !important;
    background-color: #ffffff !important;
    box-shadow: 0 1px 2px rgba(31, 35, 40, 0.08);
}
[data-testid="stForm"] {
    border: 1px solid #8c959f !important;
}
.m2s-title-connections { border-left-color: #16a34a; }
.m2s-title-discovery { border-left-color: #0e7490; }
.m2s-title-transfer { border-left-color: #c2410c; }
.m2s-section-kicker { color: #0550ae; }
.m2s-step {
    color: var(--m2s-muted);
    background: #f4f6f8;
    border-color: var(--m2s-border);
    opacity: 0.7;
}
.m2s-step-n { background: rgba(31, 35, 40, 0.08); color: var(--m2s-muted); }
.m2s-step-now { background: rgba(255, 255, 255, 0.45); }
.m2s-step-connections.done {
    color: #14532d;
    border-color: #86efac;
    background: #f0fdf4;
}
.m2s-step-connections.done .m2s-step-n { background: rgba(22, 163, 74, 0.18); color: #14532d; }
.m2s-step-connections.active {
    color: #fff;
    border-color: #15803d;
    background: #16a34a;
    box-shadow: 0 4px 14px rgba(22, 163, 74, 0.28);
}
.m2s-step-connections.active .m2s-step-n { background: #14532d; color: #fff; }
.m2s-step-connections.active .m2s-step-now { background: rgba(255, 255, 255, 0.28); color: #fff; }
.m2s-step-discovery.done {
    color: #0f4c5c;
    border-color: #67e8f9;
    background: #ecfeff;
}
.m2s-step-discovery.done .m2s-step-n { background: rgba(14, 116, 144, 0.16); color: #0f4c5c; }
.m2s-step-discovery.active {
    color: #fff;
    border-color: #155e75;
    background: #0e7490;
    box-shadow: 0 4px 14px rgba(14, 116, 144, 0.28);
}
.m2s-step-discovery.active .m2s-step-n { background: #164e63; color: #fff; }
.m2s-step-discovery.active .m2s-step-now { background: rgba(255, 255, 255, 0.28); color: #fff; }
.m2s-step-transfer.done {
    color: #7c2d12;
    border-color: #fdba74;
    background: #fff7ed;
}
.m2s-step-transfer.done .m2s-step-n { background: rgba(194, 65, 12, 0.14); color: #7c2d12; }
.m2s-step-transfer.active {
    color: #fff;
    border-color: #9a3412;
    background: #c2410c;
    box-shadow: 0 4px 14px rgba(194, 65, 12, 0.28);
}
.m2s-step-transfer.active .m2s-step-n { background: #7c2d12; color: #fff; }
.m2s-step-transfer.active .m2s-step-now { background: rgba(255, 255, 255, 0.28); color: #fff; }
.m2s-table-preview { color: #424a53; }

.st-key-nav_schema_off [data-testid="stPageLink"] a,
.st-key-nav_sql_off [data-testid="stPageLink"] a,
.st-key-nav_conn_off [data-testid="stPageLink"] a,
.st-key-nav_schema [data-testid="stPageLink"] a,
.st-key-nav_sql [data-testid="stPageLink"] a,
.st-key-nav_conn [data-testid="stPageLink"] a {
    color: #57606a !important;
    background: #f4f6f8 !important;
    border: 1px solid var(--m2s-border) !important;
    box-shadow: none !important;
    font-weight: 600 !important;
}
.st-key-nav_schema_off [data-testid="stIconMaterial"],
.st-key-nav_schema_off [data-testid="stPageLink"] p,
.st-key-nav_sql_off [data-testid="stIconMaterial"],
.st-key-nav_sql_off [data-testid="stPageLink"] p,
.st-key-nav_conn_off [data-testid="stIconMaterial"],
.st-key-nav_conn_off [data-testid="stPageLink"] p,
.st-key-nav_schema [data-testid="stIconMaterial"],
.st-key-nav_schema [data-testid="stPageLink"] p,
.st-key-nav_sql [data-testid="stIconMaterial"],
.st-key-nav_sql [data-testid="stPageLink"] p,
.st-key-nav_conn [data-testid="stIconMaterial"],
.st-key-nav_conn [data-testid="stPageLink"] p { color: #57606a !important; }

.st-key-nav_schema_on [data-testid="stPageLink"] a,
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] {
    color: #fff !important;
    background: #0e7490 !important;
    border: 1px solid #155e75 !important;
    box-shadow: inset 4px 0 0 #67e8f9, 0 4px 14px rgba(14, 116, 144, 0.22) !important;
    font-weight: 800 !important;
}
.st-key-nav_schema_on [data-testid="stIconMaterial"],
.st-key-nav_schema_on [data-testid="stPageLink"] p,
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] [data-testid="stIconMaterial"],
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] p { color: #fff !important; }

.st-key-nav_sql_on [data-testid="stPageLink"] a,
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] {
    color: #fff !important;
    background: #c2410c !important;
    border: 1px solid #9a3412 !important;
    box-shadow: inset 4px 0 0 #fdba74, 0 4px 14px rgba(194, 65, 12, 0.22) !important;
    font-weight: 800 !important;
}
.st-key-nav_sql_on [data-testid="stIconMaterial"],
.st-key-nav_sql_on [data-testid="stPageLink"] p,
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] [data-testid="stIconMaterial"],
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] p { color: #fff !important; }

.st-key-nav_conn_on [data-testid="stPageLink"] a,
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] {
    color: #fff !important;
    background: #16a34a !important;
    border: 1px solid #15803d !important;
    box-shadow: inset 4px 0 0 #86efac, 0 4px 14px rgba(22, 163, 74, 0.22) !important;
    font-weight: 800 !important;
}
.st-key-nav_conn_on [data-testid="stIconMaterial"],
.st-key-nav_conn_on [data-testid="stPageLink"] p,
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] [data-testid="stIconMaterial"],
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] p { color: #fff !important; }

.st-key-nav_schema_off [data-testid="stPageLink"] a:hover,
.st-key-nav_sql_off [data-testid="stPageLink"] a:hover,
.st-key-nav_conn_off [data-testid="stPageLink"] a:hover {
    color: #1f2328 !important;
    background: #e8ecf0 !important;
    border-color: rgba(31, 35, 40, 0.22) !important;
}
.st-key-nav_schema_off [data-testid="stPageLink"] a:hover [data-testid="stIconMaterial"],
.st-key-nav_schema_off [data-testid="stPageLink"] a:hover p,
.st-key-nav_sql_off [data-testid="stPageLink"] a:hover [data-testid="stIconMaterial"],
.st-key-nav_sql_off [data-testid="stPageLink"] a:hover p,
.st-key-nav_conn_off [data-testid="stPageLink"] a:hover [data-testid="stIconMaterial"],
.st-key-nav_conn_off [data-testid="stPageLink"] a:hover p { color: #1f2328 !important; }

.st-key-cta_conn [data-testid="stPageLink"] a,
.st-key-cta_disc [data-testid="stPageLink"] a,
.st-key-cta_to_connections [data-testid="stPageLink"] a,
.st-key-cta_to_discovery [data-testid="stPageLink"] a {
    color: #0a3069 !important;
    background: #dbeafe !important;
    border-color: #2563eb !important;
}
.st-key-cta_sql [data-testid="stPageLink"] a,
.st-key-cta_to_transfer [data-testid="stPageLink"] a {
    color: #7c2d12 !important;
    background: #ffedd5 !important;
    border-color: #c2410c !important;
}

.stButton button {
    color: #1f2328 !important;
    background: #ffffff !important;
    border: 1px solid rgba(31, 35, 40, 0.18) !important;
}
button[kind="primary"], button[data-testid="stBaseButton-primary"] {
    background: #0969da !important;
    color: #ffffff !important;
    border: 0 !important;
}
.st-key-disc_database_drdl button {
    background: linear-gradient(135deg, #2f81f7, #0891b2) !important;
    color: #041018 !important;
    border: 0 !important;
}
.st-key-mongo_test button,
.st-key-sql_test button {
    background: #dbeafe !important;
    color: #0a3069 !important;
    border: 1px solid #2563eb !important;
}

[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea,
[data-baseweb="input"] input,
[data-baseweb="textarea"] textarea {
    color: #1f2328 !important;
    -webkit-text-fill-color: #1f2328 !important;
    background-color: #ffffff !important;
}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stNumberInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder,
[data-baseweb="input"] input::placeholder,
[data-baseweb="textarea"] textarea::placeholder {
    color: #8c959f !important;
    -webkit-text-fill-color: #8c959f !important;
    opacity: 0.7 !important;
    font-weight: 400 !important;
}
[data-testid="stTextInput"] input:focus::placeholder,
[data-testid="stNumberInput"] input:focus::placeholder,
[data-testid="stTextArea"] textarea:focus::placeholder,
[data-baseweb="input"] input:focus::placeholder,
[data-baseweb="textarea"] textarea:focus::placeholder,
[data-testid="stTextInput"]:focus-within input::placeholder,
[data-testid="stNumberInput"]:focus-within input::placeholder,
[data-testid="stTextArea"]:focus-within textarea::placeholder {
    color: transparent !important;
    -webkit-text-fill-color: transparent !important;
    opacity: 0 !important;
}
[data-baseweb="select"] > div,
[data-baseweb="base-input"] {
    background-color: #ffffff !important;
    color: #1f2328 !important;
}
[data-testid="stWidgetLabel"] p,
[data-testid="stMarkdownContainer"] p,
.stCaption, [data-testid="stCaptionContainer"] {
    color: #1f2328 !important;
}
[data-testid="stCaptionContainer"] { color: #57606a !important; }
[data-testid="stMetricValue"] { color: #1f2328 !important; }
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stExpandSidebarButton"] {
    background: #ffffff !important;
    border: 1px solid rgba(31, 35, 40, 0.22) !important;
    color: #1f2328 !important;
    box-shadow: 0 1px 2px rgba(31, 35, 40, 0.12);
}
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stExpandSidebarButton"] {
    background: #ffffff !important;
    border: 1px solid #d8dce2 !important;
    color: #1f2328 !important;
}
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stExpandSidebarButton"]:hover {
    background: var(--m2s-accent-soft) !important;
    border-color: rgba(9, 105, 218, 0.45) !important;
}
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"],
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"] {
    color: #1f2328 !important;
    -webkit-text-fill-color: #1f2328 !important;
}
</style>
"""

PAGES: dict[str, object] = {}

STEPS = (
    ("connections", "1", "Bağlantılar"),
    ("discovery", "2", "Şema keşfi"),
    ("transfer", "3", "SQL aktarımı"),
)


def _light_css_scoped() -> str:
    raw = LIGHT_CSS.replace("<style>", "").replace("</style>", "").strip()
    chunks: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]+)\}", raw):
        selectors = match.group(1).strip()
        body = match.group(2)
        if selectors.startswith(":root"):
            chunks.append(f'html[data-m2s-theme="light"] {{{body}}}')
            continue
        parts = [part.strip() for part in selectors.split(",") if part.strip()]
        scoped = ", ".join(f'html[data-m2s-theme="light"] {part}' for part in parts)
        chunks.append(f"{scoped} {{{body}}}")
    return "\n".join(chunks)


def inject_css() -> None:
    extra = f"<style>{_light_css_scoped()}</style>"
    st.markdown(CSS + extra, unsafe_allow_html=True)


def theme_toggle() -> None:
    """Sabit üst şerit: logo, breadcrumb ve ay/güneş. Tıklama Streamlit rerun yapmaz."""
    components.html(
        """
<!-- m2s-shell v3: toggle beside name, compact nav -->
<script>
(function () {
  var PH_CLEAR = 1;
  var win = window.parent && window.parent !== window ? window.parent : window;
  var doc = win.document;
  var store = win.localStorage;
  var session = win.sessionStorage;
  var PAGES = {
    connections: "Bağlantılar",
    discovery: "Şema keşfi",
    transfer: "SQL aktarımı"
  };
  function apply(kind) {
    win.__m2sTheme = kind;
    doc.documentElement.setAttribute("data-m2s-theme", kind);
    var dock = doc.querySelector(".m2s-theme-dock");
    if (dock) dock.setAttribute("data-mode", kind);
  }
  function pageFromPath(path) {
    var raw = (path || "").split("?")[0];
    if (raw.indexOf("/discovery") !== -1) return "discovery";
    if (raw.indexOf("/transfer") !== -1) return "transfer";
    return "connections";
  }
  function updateCrumbs() {
    var key = pageFromPath(win.location.pathname);
    var el = doc.querySelector(".m2s-crumb-page");
    if (!el) return;
    el.textContent = PAGES[key];
    el.setAttribute("data-page", key);
  }
  function watchPlaceholders() {
    if (win.__m2sPhWatch) return;
    win.__m2sPhWatch = true;
    doc.addEventListener("focusin", function (ev) {
      var el = ev.target;
      if (!el || (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA")) return;
      if (!el.placeholder) return;
      el.dataset.m2sPh = el.placeholder;
      el.placeholder = "";
    }, true);
    doc.addEventListener("focusout", function (ev) {
      var el = ev.target;
      if (!el || !el.dataset || !el.dataset.m2sPh) return;
      if (!el.value) el.placeholder = el.dataset.m2sPh;
    }, true);
  }
  function watchLocation() {
    if (win.__m2sTopbarWatch) return;
    win.__m2sTopbarWatch = true;
    var push = win.history.pushState;
    var replace = win.history.replaceState;
    win.history.pushState = function () {
      push.apply(this, arguments);
      updateCrumbs();
    };
    win.history.replaceState = function () {
      replace.apply(this, arguments);
      updateCrumbs();
    };
    win.addEventListener("popstate", updateCrumbs);
    setInterval(updateCrumbs, 400);
  }
  function forceNativeDefaults() {
    var encoded = JSON.stringify("Dark");
    var needReload = false;
    try {
      Object.keys(store).forEach(function (key) {
        if (key.startsWith("stActiveTheme-") && store.getItem(key) !== encoded) {
          store.setItem(key, encoded);
          needReload = true;
        }
      });
      ["/", "/connections", "/discovery", "/transfer"].forEach(function (path) {
        var key = "stActiveTheme-" + path + "-v2";
        if (store.getItem(key) !== encoded) {
          store.setItem(key, encoded);
          needReload = true;
        }
      });
      if (needReload && session.getItem("m2s-boot-reload") !== "1") {
        session.setItem("m2s-boot-reload", "1");
        win.location.reload();
        return true;
      }
    } catch (err) {}
    return false;
  }
  var ICON_LEFT = '<svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>';
  var ICON_RIGHT = '<svg viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>';
  function sidebarOpen() {
    var sidebar = doc.querySelector('[data-testid="stSidebar"]');
    return !sidebar || sidebar.getAttribute("aria-expanded") !== "false";
  }
  function nativeSidebarButton(open) {
    if (open) {
      return doc.querySelector('[data-testid="stSidebarCollapseButton"] button')
        || doc.querySelector('[data-testid="stSidebarCollapseButton"]');
    }
    return doc.querySelector('[data-testid="stExpandSidebarButton"] button')
      || doc.querySelector('[data-testid="stExpandSidebarButton"]')
      || doc.querySelector('[data-testid="collapsedControl"] button')
      || doc.querySelector('[data-testid="stSidebarCollapsedControl"] button');
  }
  var bar = doc.querySelector(".m2s-topbar");
  if (bar && !bar.querySelector(".m2s-topbar-brand .m2s-topbar-toggle")) {
    bar.remove();
    bar = null;
  }
  if (!bar) {
    bar = doc.createElement("div");
    bar.className = "m2s-topbar";
    bar.innerHTML =
      '<div class="m2s-topbar-brand">' +
        '<div class="m2s-logo">M2S</div>' +
        '<div class="m2s-topbar-name">Mongo2SQLConverter</div>' +
        '<button type="button" class="m2s-topbar-toggle" title="Menüyü daralt" aria-label="Menüyü daralt">' +
          ICON_LEFT +
        '</button>' +
      '</div>' +
      '<div class="m2s-topbar-main">' +
        '<nav class="m2s-topbar-crumbs" aria-label="Konum">' +
          '<span class="m2s-crumb-root">Mongo2SQL</span>' +
          '<span class="m2s-crumb-sep">&gt;</span>' +
          '<span class="m2s-crumb-page" data-page="connections">Bağlantılar</span>' +
        '</nav>' +
        '<div class="m2s-topbar-actions"></div>' +
      '</div>';
    doc.body.appendChild(bar);
  }
  function syncChrome() {
    var sidebar = doc.querySelector('[data-testid="stSidebar"]');
    var open = !sidebar || sidebar.getAttribute("aria-expanded") !== "false";
    bar.classList.toggle("is-collapsed", !open);
    var name = bar.querySelector(".m2s-topbar-name");
    if (name) name.textContent = open ? "Mongo2SQLConverter" : "M2S";
    var toggle = bar.querySelector(".m2s-topbar-toggle");
    if (toggle) {
      toggle.innerHTML = open ? ICON_LEFT : ICON_RIGHT;
      toggle.setAttribute("title", open ? "Menüyü daralt" : "Menüyü aç");
      toggle.setAttribute("aria-label", open ? "Menüyü daralt" : "Menüyü aç");
    }
    var brand = bar.querySelector(".m2s-topbar-brand");
    if (sidebar && brand) {
      var w = Math.round(sidebar.getBoundingClientRect().width);
      if (w > 8) {
        brand.style.flex = "0 0 " + w + "px";
        brand.style.width = w + "px";
        brand.style.minWidth = w + "px";
        brand.style.maxWidth = w + "px";
      }
    }
  }
  var toggleBtn = bar.querySelector(".m2s-topbar-toggle");
  if (toggleBtn && !toggleBtn.dataset.bound) {
    toggleBtn.dataset.bound = "1";
    toggleBtn.addEventListener("click", function () {
      var open = sidebarOpen();
      var native = nativeSidebarButton(open);
      if (native) {
        native.style.pointerEvents = "auto";
        native.click();
      }
      setTimeout(syncChrome, 50);
      setTimeout(syncChrome, 250);
    });
  }
  if (!win.__m2sSideWatch) {
    win.__m2sSideWatch = true;
    setInterval(syncChrome, 300);
  }
  syncChrome();
  var actions = bar.querySelector(".m2s-topbar-actions");
  var dock = doc.querySelector(".m2s-theme-dock");
  if (!dock) {
    dock = doc.createElement("div");
    dock.className = "m2s-theme-dock";
    dock.innerHTML =
      '<button type="button" class="m2s-theme-btn m2s-theme-moon" title="Koyu tema" aria-label="Koyu tema">' +
      '<svg viewBox="0 0 24 24"><path d="M21 14.3A8.4 8.4 0 1 1 9.7 3 7 7 0 0 0 21 14.3z"/></svg></button>' +
      '<button type="button" class="m2s-theme-btn m2s-theme-sun" title="Açık tema" aria-label="Açık tema">' +
      '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/>' +
      '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg></button>';
  }
  if (!dock.dataset.bound) {
    dock.dataset.bound = "1";
    dock.querySelector(".m2s-theme-moon").addEventListener("click", function () { apply("dark"); });
    dock.querySelector(".m2s-theme-sun").addEventListener("click", function () { apply("light"); });
  }
  if (actions && dock.parentNode !== actions) actions.appendChild(dock);
  updateCrumbs();
  watchLocation();
  watchPlaceholders();
  if (!win.__m2sBooted) {
    win.__m2sBooted = true;
    apply("dark");
    if (forceNativeDefaults()) return;
  } else {
    apply(win.__m2sTheme || "dark");
  }
})();
</script>
        """,
        height=1,
        scrolling=False,
    )


def register_pages(pages: dict[str, object]) -> None:
    """Page objects from `st.navigation`, used by in-page CTAs."""
    PAGES.update(pages)


def stepper(active: str) -> None:
    """Baglantilar → Sema kesfi → SQL aktarimi, with the active step highlighted."""
    order = [key for key, _, _ in STEPS]
    active_at = order.index(active) if active in order else 0
    chips: list[str] = []
    for i, (key, num, label) in enumerate(STEPS):
        if i:
            chips.append('<span class="m2s-step-line"></span>')
        if key == active:
            kind = "active"
            now = '<span class="m2s-step-now">şu an</span>'
        elif i < active_at:
            kind = "done"
            now = ""
        else:
            kind = "todo"
            now = ""
        chips.append(
            f'<div class="m2s-step m2s-step-{key} {kind}">'
            f'<span class="m2s-step-n">{num}</span>{label}{now}</div>'
        )
    st.markdown(f'<div class="m2s-stepper">{"".join(chips)}</div>', unsafe_allow_html=True)


def page_cta(page_key: str, label: str, icon: str, widget_key: str) -> None:
    page = PAGES.get(page_key)
    if page is None:
        return
    with st.container(key=widget_key):
        st.page_link(page, label=label, icon=icon, width="stretch")


def need_connections(blockers: Sequence[str]) -> None:
    st.warning(" ve ".join(blockers) + " eksik. Önce bağlantıları kaydedin.")
    page_cta(
        "connections",
        "Bağlantılara git",
        ":material/settings_ethernet:",
        "cta_to_connections",
    )


def nav_menu(
    items: Sequence[tuple[str, object, str, str]], current_key: str | None = None
) -> None:
    """Colored page links at the top of the sidebar."""
    for key, page, icon, label in items:
        state = "on" if current_key == key else "off"
        with st.container(key=f"{key}_{state}"):
            st.page_link(page, label=label, icon=icon, width="content")


def status_row(label: str, value: str, state: State = "off") -> str:
    """One status line. Collapsed to just its dot when the sidebar is a rail."""
    return (
        f'<div class="m2s-status" title="{label}: {value}">'
        f'<span class="m2s-dot {state}"></span>'
        f'<span class="m2s-status-text">{label}'
        f'<span class="m2s-status-value"> · {value}</span></span></div>'
    )


def sidebar_block(label: str, rows: Iterable[str], show_in_rail: bool = True) -> str:
    classes = "m2s-side-block" if show_in_rail else "m2s-side-block m2s-rail-hide"
    return (
        f'<div class="{classes}"><div class="m2s-side-label">{label}</div>'
        + "".join(rows)
        + "</div>"
    )


def sidebar_foot(text: str) -> str:
    return f'<div class="m2s-side-foot">{text}</div>'


def sidebar_panel(blocks: Sequence[str]) -> None:
    st.markdown("".join(blocks), unsafe_allow_html=True)


def page_header(eyebrow: str, title: str, lede: str, *, step: str | None = None) -> None:
    if step:
        stepper(step)
    elif eyebrow:
        st.markdown(f'<div class="m2s-eyebrow">{eyebrow}</div>', unsafe_allow_html=True)
    title_cls = f"m2s-title m2s-title-{step}" if step else "m2s-title"
    st.markdown(
        f'<h1 class="{title_cls}">{title}</h1>'
        f'<p class="m2s-lede">{lede}</p>',
        unsafe_allow_html=True,
    )


def section_heading(kicker: str, title: str, hint: str | None = None) -> None:
    st.markdown(
        f'<div class="m2s-section-kicker">{kicker}</div>'
        f'<h2 class="m2s-section-title">{title}</h2>',
        unsafe_allow_html=True,
    )
    if hint:
        st.markdown(f'<p class="m2s-card-hint">{hint}</p>', unsafe_allow_html=True)


def card_title(title: str, hint: str | None = None) -> None:
    st.markdown(f'<div class="m2s-card-title">{title}</div>', unsafe_allow_html=True)
    if hint:
        st.markdown(f'<p class="m2s-card-hint">{hint}</p>', unsafe_allow_html=True)
