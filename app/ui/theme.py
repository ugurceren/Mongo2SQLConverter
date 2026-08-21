"""Application shell: styling, brand block and small layout helpers."""

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
    --m2s-rail: 76px;
}

/* Keep the toolbar mounted: it hosts the button that reopens the sidebar. */
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], footer { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stHeader"] {
    background: transparent !important;
}

/* Collapse control sits on the right of the open sidebar header. */
[data-testid="stSidebar"][aria-expanded="true"] [data-testid="stSidebarHeader"] {
    position: absolute !important;
    top: 12px !important;
    right: 10px !important;
    left: auto !important;
    width: auto !important;
    height: 44px !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    display: flex !important;
    justify-content: flex-end !important;
    align-items: center !important;
    z-index: 6;
    background: transparent !important;
    overflow: visible !important;
}
[data-testid="stSidebar"][aria-expanded="true"] [data-testid="stLogoSpacer"] {
    display: none !important;
}
[data-testid="stSidebarCollapseButton"] {
    position: relative !important;
    left: auto !important;
    right: auto !important;
    top: auto !important;
    transform: none !important;
    display: flex !important;
    justify-content: flex-end;
    width: auto !important;
    margin: 0 !important;
}
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapseButton"] button {
    visibility: visible !important;
    opacity: 1 !important;
}
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stExpandSidebarButton"] {
    position: relative !important;
    inset: auto !important;
    width: 44px !important;
    height: 44px !important;
    min-height: 44px !important;
    padding: 0 !important;
    border-radius: 12px !important;
    background: rgba(240, 246, 252, 0.10) !important;
    border: 1px solid var(--m2s-border) !important;
    color: #e6edf3 !important;
}
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] {
    color: #e6edf3 !important;
    -webkit-text-fill-color: #e6edf3 !important;
    font-size: 22px !important;
}
[data-testid="stSidebarCollapseButton"] button:hover,
[data-testid="stExpandSidebarButton"]:hover {
    background: var(--m2s-accent-soft) !important;
    border-color: rgba(76, 141, 255, 0.45) !important;
}
/* Header stacking is below the rail; lift it so the reopen button sits on top. */
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stHeader"] {
    z-index: 1000004 !important;
    pointer-events: none !important;
    overflow: visible !important;
}
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stToolbar"],
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stHeader"] * {
    pointer-events: none !important;
}
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stExpandSidebarButton"] {
    position: fixed !important;
    left: 16px !important;
    top: 10px !important;
    z-index: 1000005 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    pointer-events: auto !important;
    visibility: visible !important;
    opacity: 1 !important;
    background: #2b303b !important;
    border: 1px solid #3d4450 !important;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
}
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) [data-testid="stExpandSidebarButton"] * {
    pointer-events: auto !important;
}

section[data-testid="stMain"] .block-container {
    max-width: 1220px;
    padding-top: 3.2rem;
    padding-bottom: 4rem;
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
}
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
    font-size: 0.78rem;
    font-weight: 650;
    white-space: nowrap;
}
.m2s-step-n {
    width: 1.15rem; height: 1.15rem; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 0.68rem; font-weight: 750;
    background: rgba(240, 246, 252, 0.08);
}
.m2s-step-connections {
    color: #9ee3a8;
    border-color: rgba(63, 185, 80, 0.5);
    background: rgba(63, 185, 80, 0.12);
}
.m2s-step-connections .m2s-step-n { background: rgba(63, 185, 80, 0.32); color: #b6f0be; }
.m2s-step-connections.active {
    color: #b6f0be;
    border-color: #3fb950;
    background: rgba(63, 185, 80, 0.24);
    box-shadow: 0 0 16px rgba(63, 185, 80, 0.22);
}
.m2s-step-connections.active .m2s-step-n { background: #3fb950; color: #041018; }
.m2s-step-discovery {
    color: #7af0ff;
    border-color: rgba(34, 211, 238, 0.5);
    background: rgba(34, 211, 238, 0.12);
}
.m2s-step-discovery .m2s-step-n { background: rgba(34, 211, 238, 0.32); color: #b8f7ff; }
.m2s-step-discovery.active {
    color: #b8f7ff;
    border-color: #22d3ee;
    background: rgba(34, 211, 238, 0.24);
    box-shadow: 0 0 16px rgba(34, 211, 238, 0.22);
}
.m2s-step-discovery.active .m2s-step-n { background: #22d3ee; color: #041018; }
.m2s-step-transfer {
    color: #ffd08a;
    border-color: rgba(251, 146, 60, 0.5);
    background: rgba(251, 146, 60, 0.12);
}
.m2s-step-transfer .m2s-step-n { background: rgba(251, 146, 60, 0.32); color: #ffe0b0; }
.m2s-step-transfer.active {
    color: #ffe0b0;
    border-color: #fb923c;
    background: rgba(251, 146, 60, 0.24);
    box-shadow: 0 0 16px rgba(251, 146, 60, 0.22);
}
.m2s-step-transfer.active .m2s-step-n { background: #fb923c; color: #041018; }
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
[data-testid="stVerticalBlockBorderWrapper"] {
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
    border-right: 1px solid var(--m2s-border);
    min-width: 272px !important;
}
[data-testid="stSidebar"] .block-container { padding-top: 0.85rem; }
[data-testid="stSidebarNav"] { display: none !important; }

.m2s-brand {
    display: flex; align-items: center; gap: 0.75rem;
    margin: 0 0 1rem 0; padding: 0.1rem 3.2rem 1rem 0.05rem;
    border-bottom: 1px solid var(--m2s-border);
}
.m2s-logo {
    width: 46px; height: 46px; border-radius: 13px;
    background: linear-gradient(145deg, #4c8dff 0%, #7b5cff 52%, #22d3ee 100%);
    color: #fff; font-weight: 800; font-size: 0.78rem; letter-spacing: 0.04em;
    display: flex; align-items: center; justify-content: center;
    flex: 0 0 46px;
    box-shadow: 0 0 0 1px rgba(123, 92, 255, 0.35), 0 8px 22px rgba(76, 141, 255, 0.38);
}
.m2s-brand-name {
    font-weight: 750; font-size: 1.14rem; line-height: 1.1;
    letter-spacing: -0.03em;
}
.m2s-brand-sub { color: var(--m2s-muted); font-size: 0.75rem; margin-top: 0.12rem; }

.st-key-nav_schema, .st-key-nav_sql, .st-key-nav_conn { margin: 0 0 0.48rem 0; }
[data-testid="stSidebar"] [data-testid="stPageLink"] a,
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] {
    display: flex !important; align-items: center; gap: 0.7rem;
    border-radius: 12px !important;
    padding: 0.78rem 0.9rem !important;
    font-weight: 700 !important;
    font-size: 0.95rem !important;
    letter-spacing: -0.01em;
    text-decoration: none !important;
    transition: transform 0.14s ease, box-shadow 0.14s ease, filter 0.14s ease !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover,
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"]:hover {
    transform: translateX(3px);
    filter: brightness(1.12);
}
[data-testid="stSidebar"] [data-testid="stPageLink"] [data-testid="stIconMaterial"] {
    font-size: 1.35rem !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] p {
    margin: 0 !important;
}

.st-key-nav_schema [data-testid="stPageLink"] a {
    color: #b8f7ff !important;
    background: linear-gradient(135deg, rgba(34, 211, 238, 0.40), rgba(76, 141, 255, 0.16)) !important;
    border: 1px solid #22d3ee !important;
    box-shadow: inset 3px 0 0 #22d3ee, 0 8px 20px rgba(34, 211, 238, 0.18);
}
.st-key-nav_schema [data-testid="stPageLink"] a:hover,
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] {
    background: linear-gradient(135deg, rgba(34, 211, 238, 0.58), rgba(76, 141, 255, 0.26)) !important;
    box-shadow: inset 3px 0 0 #67e8f9, 0 0 24px rgba(34, 211, 238, 0.42);
}
.st-key-nav_schema [data-testid="stIconMaterial"],
.st-key-nav_schema [data-testid="stPageLink"] p { color: #7af0ff !important; }

.st-key-nav_sql [data-testid="stPageLink"] a {
    color: #ffe0b0 !important;
    background: linear-gradient(135deg, rgba(251, 146, 60, 0.42), rgba(245, 158, 11, 0.16)) !important;
    border: 1px solid #fb923c !important;
    box-shadow: inset 3px 0 0 #fb923c, 0 8px 20px rgba(251, 146, 60, 0.18);
}
.st-key-nav_sql [data-testid="stPageLink"] a:hover,
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] {
    background: linear-gradient(135deg, rgba(251, 146, 60, 0.60), rgba(245, 158, 11, 0.26)) !important;
    box-shadow: inset 3px 0 0 #fdba74, 0 0 24px rgba(251, 146, 60, 0.42);
}
.st-key-nav_sql [data-testid="stIconMaterial"],
.st-key-nav_sql [data-testid="stPageLink"] p { color: #ffd08a !important; }

.st-key-nav_conn [data-testid="stPageLink"] a {
    color: #b6f0be !important;
    background: linear-gradient(135deg, rgba(63, 185, 80, 0.42), rgba(34, 211, 238, 0.10)) !important;
    border: 1px solid #3fb950 !important;
    box-shadow: inset 3px 0 0 #3fb950, 0 8px 20px rgba(63, 185, 80, 0.18);
}
.st-key-nav_conn [data-testid="stPageLink"] a:hover,
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] {
    background: linear-gradient(135deg, rgba(63, 185, 80, 0.60), rgba(34, 211, 238, 0.16)) !important;
    box-shadow: inset 3px 0 0 #9ee3a8, 0 0 24px rgba(63, 185, 80, 0.42);
}
.st-key-nav_conn [data-testid="stIconMaterial"],
.st-key-nav_conn [data-testid="stPageLink"] p { color: #9ee3a8 !important; }

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
    transform: none !important;
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
    padding-top: 0 !important;
    padding-left: 0.4rem !important;
    padding-right: 0.4rem !important;
    overflow-x: hidden;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarUserContent"] {
    padding-top: 0 !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] {
    display: none !important;
}
[data-testid="stSidebar"][aria-expanded="false"] .m2s-brand,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status { justify-content: center; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-brand {
    padding: 0.1rem 0.05rem 1rem 0.05rem;
    min-height: 65px;
}
[data-testid="stSidebar"][aria-expanded="false"] .m2s-brand-text,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-label,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status-text,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-foot,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-rail-hide { display: none !important; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-logo {
    width: 46px; height: 46px; flex-basis: 46px; font-size: 0.78rem;
    visibility: hidden;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a {
    justify-content: center !important;
    align-items: center !important;
    font-size: 0 !important;
    width: 48px !important;
    height: 59px !important;
    min-height: 59px !important;
    padding: 0 !important;
    margin: 0 auto;
    border-radius: 14px !important;
    transform: none !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a:hover {
    transform: none !important;
    filter: brightness(1.12);
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a p {
    display: none !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] [data-testid="stIconMaterial"] {
    display: inline-flex !important;
    font-size: 1.5rem !important;
    line-height: 1 !important;
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
    position: fixed !important;
    top: 10px !important;
    right: 12px !important;
    z-index: 1000010;
    width: var(--m2s-rail);
    max-width: var(--m2s-rail);
    height: 36px;
    margin: 0;
    padding: 3px;
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
    height: 28px;
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
    width: 14px;
    height: 14px;
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

code, pre, .stCode { font-size: 0.82rem; }
iframe[height="0"] { display: none !important; }
</style>
"""

LIGHT_CSS = """
<style>
:root {
    --m2s-accent: #0969da;
    --m2s-accent-soft: rgba(9, 105, 218, 0.12);
    --m2s-border: rgba(31, 35, 40, 0.14);
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

.m2s-brand-name { color: #1f2328; }
.m2s-title, .m2s-section-title { color: #1f2328; }
.m2s-section-kicker { color: #0550ae; }
.m2s-step-connections {
    color: #14532d;
    border-color: #16a34a;
    background: #dcfce7;
}
.m2s-step-connections .m2s-step-n { background: rgba(22, 163, 74, 0.22); color: #14532d; }
.m2s-step-connections.active {
    color: #14532d;
    border-color: #15803d;
    background: #bbf7d0;
    box-shadow: none;
}
.m2s-step-connections.active .m2s-step-n { background: #16a34a; color: #fff; }
.m2s-step-discovery {
    color: #0f4c5c;
    border-color: #0e7490;
    background: #d9f6fb;
}
.m2s-step-discovery .m2s-step-n { background: rgba(14, 116, 144, 0.2); color: #0f4c5c; }
.m2s-step-discovery.active {
    color: #0f4c5c;
    border-color: #155e75;
    background: #c5eef6;
    box-shadow: none;
}
.m2s-step-discovery.active .m2s-step-n { background: #0e7490; color: #fff; }
.m2s-step-transfer {
    color: #7c2d12;
    border-color: #c2410c;
    background: #ffedd5;
}
.m2s-step-transfer .m2s-step-n { background: rgba(194, 65, 12, 0.18); color: #7c2d12; }
.m2s-step-transfer.active {
    color: #7c2d12;
    border-color: #9a3412;
    background: #fed7aa;
    box-shadow: none;
}
.m2s-step-transfer.active .m2s-step-n { background: #c2410c; color: #fff; }
.m2s-table-preview { color: #424a53; }

.st-key-nav_schema [data-testid="stPageLink"] a {
    color: #0f4c5c !important;
    background: linear-gradient(135deg, #d9f6fb, #e8f4ff) !important;
    border: 1px solid #0e7490 !important;
    box-shadow: inset 3px 0 0 #0e7490, 0 4px 12px rgba(14, 116, 144, 0.12);
}
.st-key-nav_schema [data-testid="stPageLink"] a:hover,
.st-key-nav_schema [data-testid="stPageLink"] a[aria-current="page"] {
    background: linear-gradient(135deg, #c5eef6, #dbeafe) !important;
    box-shadow: inset 3px 0 0 #155e75, 0 0 0 1px rgba(14, 116, 144, 0.25);
}
.st-key-nav_schema [data-testid="stIconMaterial"],
.st-key-nav_schema [data-testid="stPageLink"] p { color: #0f4c5c !important; }

.st-key-nav_sql [data-testid="stPageLink"] a {
    color: #7c2d12 !important;
    background: linear-gradient(135deg, #ffedd5, #fef3c7) !important;
    border: 1px solid #c2410c !important;
    box-shadow: inset 3px 0 0 #c2410c, 0 4px 12px rgba(194, 65, 12, 0.12);
}
.st-key-nav_sql [data-testid="stPageLink"] a:hover,
.st-key-nav_sql [data-testid="stPageLink"] a[aria-current="page"] {
    background: linear-gradient(135deg, #fed7aa, #fde68a) !important;
    box-shadow: inset 3px 0 0 #9a3412, 0 0 0 1px rgba(194, 65, 12, 0.25);
}
.st-key-nav_sql [data-testid="stIconMaterial"],
.st-key-nav_sql [data-testid="stPageLink"] p { color: #7c2d12 !important; }

.st-key-nav_conn [data-testid="stPageLink"] a {
    color: #14532d !important;
    background: linear-gradient(135deg, #dcfce7, #ecfccb) !important;
    border: 1px solid #16a34a !important;
    box-shadow: inset 3px 0 0 #16a34a, 0 4px 12px rgba(22, 163, 74, 0.12);
}
.st-key-nav_conn [data-testid="stPageLink"] a:hover,
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] {
    background: linear-gradient(135deg, #bbf7d0, #d9f99d) !important;
    box-shadow: inset 3px 0 0 #15803d, 0 0 0 1px rgba(22, 163, 74, 0.25);
}
.st-key-nav_conn [data-testid="stIconMaterial"],
.st-key-nav_conn [data-testid="stPageLink"] p { color: #14532d !important; }

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
    """Sağ üstte ay / güneş; tıklama Streamlit rerun yapmaz."""
    components.html(
        """
<script>
(function () {
  var win = window.parent && window.parent !== window ? window.parent : window;
  var doc = win.document;
  var store = win.localStorage;
  var session = win.sessionStorage;
  function apply(kind) {
    win.__m2sTheme = kind;
    doc.documentElement.setAttribute("data-m2s-theme", kind);
    var dock = doc.querySelector(".m2s-theme-dock");
    if (dock) dock.setAttribute("data-mode", kind);
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
      if (store.getItem("stSidebarCollapsed-") === "true") {
        store.setItem("stSidebarCollapsed-", "false");
        needReload = true;
      }
      if (needReload && session.getItem("m2s-boot-reload") !== "1") {
        session.setItem("m2s-boot-reload", "1");
        win.location.reload();
        return true;
      }
    } catch (err) {}
    return false;
  }
  function expandSidebar() {
    try { store.setItem("stSidebarCollapsed-", "false"); } catch (err) {}
    var tries = 0;
    var timer = setInterval(function () {
      var sidebar = doc.querySelector('[data-testid="stSidebar"]');
      var open = sidebar && sidebar.getAttribute("aria-expanded") === "true";
      if (open || ++tries > 25) {
        clearInterval(timer);
        return;
      }
      var btn = doc.querySelector('[data-testid="stExpandSidebarButton"]');
      if (btn) btn.click();
    }, 80);
  }
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
    doc.body.appendChild(dock);
    dock.querySelector(".m2s-theme-moon").addEventListener("click", function () { apply("dark"); });
    dock.querySelector(".m2s-theme-sun").addEventListener("click", function () { apply("light"); });
  }
  if (!win.__m2sBooted) {
    win.__m2sBooted = true;
    apply("dark");
    if (forceNativeDefaults()) return;
    expandSidebar();
  } else {
    apply(win.__m2sTheme || "dark");
  }
})();
</script>
        """,
        height=0,
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
        kind = "active" if key == active else ("done" if i < active_at else "")
        chips.append(
            f'<div class="m2s-step m2s-step-{key} {kind}">'
            f'<span class="m2s-step-n">{num}</span>{label}</div>'
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


def brand() -> None:
    st.markdown(
        """
        <div class="m2s-brand">
            <div class="m2s-logo">M2S</div>
            <div class="m2s-brand-text">
                <div class="m2s-brand-name">Mongo2SQL</div>
                <div class="m2s-brand-sub">Şema ve aktarım</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def nav_menu(items: Sequence[tuple[str, object, str, str]]) -> None:
    """Colored page links that sit directly under the brand block."""
    for key, page, icon, label in items:
        with st.container(key=key):
            st.page_link(page, label=label, icon=icon, width="stretch")


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
    st.markdown(
        f'<h1 class="m2s-title">{title}</h1>'
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
