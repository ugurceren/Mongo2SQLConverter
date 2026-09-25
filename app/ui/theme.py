"""Application shell: styling, top bar and small layout helpers.

Light and dark come from Streamlit's own theme (`.streamlit/config.toml`). The CSS
below only styles the app's chrome. Each custom color is a `light-dark()` token, so
it follows the `color-scheme` Streamlit sets on the app for the active theme.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from html import escape
from typing import Iterator, Iterable, Literal, Sequence

import streamlit as st

State = Literal["ok", "warn", "err", "saved", "off"]

CSS = """
/* ---------- tokens: light value first, dark second ---------- */
:root {
    --m2s-bg: light-dark(#f6f8fa, #0d1117);
    --m2s-surface: light-dark(rgb(255 255 255 / 0.82), rgb(22 27 34 / 0.78));
    --m2s-surface-strong: light-dark(rgb(246 248 250 / 0.90), rgb(13 17 23 / 0.88));
    --m2s-surface-muted: light-dark(rgb(31 35 40 / 0.04), rgb(240 246 252 / 0.04));
    --m2s-surface-hover: light-dark(rgb(31 35 40 / 0.08), rgb(240 246 252 / 0.09));
    --m2s-line: light-dark(rgb(31 35 40 / 0.14), rgb(148 163 184 / 0.26));
    --m2s-line-strong: light-dark(rgb(31 35 40 / 0.28), rgb(240 246 252 / 0.26));
    --m2s-shadow: light-dark(rgb(31 35 40 / 0.07), rgb(0 0 0 / 0.26));
    --m2s-text: light-dark(#1f2328, #e6edf3);
    --m2s-muted: light-dark(#57606a, #9aa6b4);
    --m2s-accent: light-dark(#0969da, #4c8dff);
    --m2s-accent-text: light-dark(#0550ae, #9cc0ff);
    --m2s-accent-soft: light-dark(rgb(9 105 218 / 0.10), rgb(76 141 255 / 0.14));
    --m2s-accent-line: light-dark(rgb(9 105 218 / 0.45), rgb(76 141 255 / 0.45));
    --m2s-focus: light-dark(#0969da, #79a8ff);
    --m2s-on-hue: light-dark(#ffffff, #041018);

    /* status colors: used for status only */
    --m2s-ok: light-dark(#1a7f37, #3fb950);
    --m2s-ok-ring: light-dark(rgb(26 127 55 / 0.16), rgb(63 185 80 / 0.18));
    --m2s-warn: light-dark(#9a6700, #d29922);
    --m2s-warn-ring: light-dark(rgb(154 103 0 / 0.16), rgb(210 153 34 / 0.18));
    --m2s-err: light-dark(#cf222e, #f85149);
    --m2s-err-ring: light-dark(rgb(207 34 46 / 0.16), rgb(248 81 73 / 0.18));
    --m2s-off: light-dark(#8c959f, #6e7681);

    /* page hues: navigation and wayfinding only */
    --m2s-connections: light-dark(#0969da, #4c8dff);
    --m2s-connections-text: light-dark(#0550ae, #9cc0ff);
    --m2s-connections-soft: light-dark(rgb(9 105 218 / 0.10), rgb(76 141 255 / 0.14));
    --m2s-discovery: light-dark(#0e7490, #22d3ee);
    --m2s-discovery-text: light-dark(#0e7490, #67e8f9);
    --m2s-discovery-soft: light-dark(rgb(14 116 144 / 0.10), rgb(34 211 238 / 0.12));
    --m2s-transfer: light-dark(#7c3aed, #a78bfa);
    --m2s-transfer-text: light-dark(#6d28d9, #c4b5fd);
    --m2s-transfer-soft: light-dark(rgb(124 58 237 / 0.10), rgb(167 139 250 / 0.14));

    --m2s-app-bg:
        radial-gradient(1100px 520px at 8% -8%,
            light-dark(rgb(9 105 218 / 0.08), rgb(76 141 255 / 0.16)), transparent 58%),
        var(--m2s-bg);

    /* type scale */
    --m2s-fs-xs: 0.75rem;
    --m2s-fs-sm: 0.875rem;
    --m2s-fs-md: 1rem;
    --m2s-fs-lg: 1.125rem;
    --m2s-fs-xl: 1.75rem;

    --m2s-rail: 104px;
    --m2s-topbar: 56px;
    --m2s-sidebar: 292px;
    --m2s-nav: 220px;
    --m2s-blur: 16px;
}

/* ---------- page hues: these rules only pick the variables ---------- */
.m2s-step-connections, .m2s-title-connections, .st-key-m2s_next_connections,
.st-key-nav_on_connections, .st-key-nav_off_connections,
.m2s-crumb-page[data-page="connections"] {
    --m2s-page: var(--m2s-connections);
    --m2s-page-text: var(--m2s-connections-text);
    --m2s-page-soft: var(--m2s-connections-soft);
}
.m2s-step-discovery, .m2s-title-discovery, .st-key-m2s_next_discovery,
.st-key-nav_on_discovery, .st-key-nav_off_discovery,
.m2s-crumb-page[data-page="discovery"] {
    --m2s-page: var(--m2s-discovery);
    --m2s-page-text: var(--m2s-discovery-text);
    --m2s-page-soft: var(--m2s-discovery-soft);
}
.m2s-step-transfer, .m2s-title-transfer, .st-key-m2s_next_transfer,
.st-key-nav_on_transfer, .st-key-nav_off_transfer,
.m2s-crumb-page[data-page="transfer"] {
    --m2s-page: var(--m2s-transfer);
    --m2s-page-text: var(--m2s-transfer-text);
    --m2s-page-soft: var(--m2s-transfer-soft);
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
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
section[data-testid="stMain"] {
    background: var(--m2s-app-bg) !important;
    background-attachment: fixed !important;
}
[data-testid="stAppViewContainer"] { height: 100% !important; }
.block-container,
[data-testid="stMainBlockContainer"] { background: transparent !important; }

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
[data-testid="stSidebar"][aria-expanded="true"] [data-testid="stLogoSpacer"] { display: none !important; }

section[data-testid="stMain"] .block-container {
    max-width: 1220px;
    padding-top: calc(var(--m2s-topbar) + 0.85rem);
    padding-bottom: 4rem;
}

/* Visually hidden, still read by screen readers. */
.m2s-sr {
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    padding: 0 !important;
    margin: -1px !important;
    overflow: hidden !important;
    clip: rect(0 0 0 0) !important;
    white-space: nowrap !important;
    border: 0 !important;
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
    box-sizing: border-box;
    background: var(--m2s-surface-strong);
    backdrop-filter: blur(var(--m2s-blur)) saturate(140%);
    -webkit-backdrop-filter: blur(var(--m2s-blur)) saturate(140%);
    border-bottom: 1px solid var(--m2s-line);
    color: var(--m2s-text);
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
    border-right: 1px solid var(--m2s-line);
    min-width: 0;
}
.m2s-topbar.is-collapsed .m2s-topbar-brand {
    flex-basis: var(--m2s-rail);
    width: var(--m2s-rail);
    justify-content: center;
    gap: 0.28rem;
    padding: 0 6px;
}
.m2s-logo {
    width: 32px;
    height: 32px;
    flex: 0 0 32px;
    border-radius: 9px;
    background: linear-gradient(145deg, #4c8dff 0%, #7b5cff 52%, #22d3ee 100%);
    color: #ffffff;
    font-weight: 700;
    font-size: var(--m2s-fs-xs);
    letter-spacing: 0.02em;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 12px var(--m2s-shadow);
}
.m2s-topbar-name {
    font-weight: 700;
    font-size: var(--m2s-fs-sm);
    line-height: 1.1;
    letter-spacing: -0.02em;
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
    padding: 0 14px 0 16px;
}
.m2s-topbar-toggle {
    flex: 0 0 28px;
    width: 28px;
    height: 28px;
    margin: 0 0 0 auto;
    padding: 0;
    border-radius: 7px;
    border: 1px solid var(--m2s-line);
    background: var(--m2s-surface-muted);
    color: var(--m2s-text);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}
.m2s-topbar.is-collapsed .m2s-topbar-toggle { margin: 0; }
.m2s-topbar-toggle:hover {
    background: var(--m2s-accent-soft);
    border-color: var(--m2s-accent-line);
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
.m2s-topbar-crumbs {
    display: flex;
    align-items: center;
    font-size: var(--m2s-fs-sm);
    white-space: nowrap;
    min-width: 0;
}
.m2s-crumb-root,
.m2s-crumb-sep { color: var(--m2s-muted); }
.m2s-crumb-sep { margin: 0 0.45rem; }
.m2s-crumb-page {
    font-weight: 600;
    color: var(--m2s-page-text, var(--m2s-text));
}
.m2s-topbar-actions {
    margin-left: auto;
    display: flex;
    align-items: center;
}
.m2s-theme-dock {
    width: 68px;
    height: 30px;
    padding: 2px;
    box-sizing: border-box;
    display: flex;
    align-items: stretch;
    border-radius: 999px;
    background: var(--m2s-surface-muted);
    border: 1px solid var(--m2s-line);
}
.m2s-theme-btn {
    flex: 1 1 0;
    min-width: 0;
    padding: 0;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: var(--m2s-muted);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
}
.m2s-theme-btn:hover { color: var(--m2s-text); }
.m2s-theme-btn[aria-pressed="true"] {
    background: light-dark(#ffffff, #2b303b);
    color: var(--m2s-text);
    box-shadow: 0 1px 3px var(--m2s-shadow);
}
.m2s-theme-btn svg {
    width: 13px;
    height: 13px;
    display: block;
    stroke: currentColor;
    fill: none;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
}

/* ---------- page header ---------- */
.m2s-title {
    font-size: var(--m2s-fs-xl) !important;
    font-weight: 700 !important;
    line-height: 1.2 !important;
    margin: 0 !important;
    padding: 0.1rem 0 0.1rem 0.85rem !important;
    border-left: 4px solid var(--m2s-page, var(--m2s-accent));
    color: var(--m2s-text);
}
.m2s-lede {
    color: var(--m2s-muted);
    font-size: var(--m2s-fs-md);
    line-height: 1.55;
    margin: 0.55rem 0 1.1rem 0;
    max-width: 68ch;
}

/* ---------- stepper ---------- */
.m2s-stepper {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0.15rem 0 1.4rem 0;
}
.m2s-step {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.3rem 0.8rem 0.3rem 0.35rem;
    border-radius: 999px;
    border: 1px solid var(--m2s-line);
    color: var(--m2s-muted);
    background: var(--m2s-surface-muted);
    font-size: var(--m2s-fs-sm);
    font-weight: 500;
    white-space: nowrap;
}
.m2s-step-n {
    width: 1.35rem;
    height: 1.35rem;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: var(--m2s-fs-xs);
    font-weight: 700;
    background: var(--m2s-surface-hover);
    color: var(--m2s-muted);
}
.m2s-step.done {
    color: var(--m2s-page-text);
    border-color: transparent;
    background: var(--m2s-page-soft);
}
.m2s-step.done .m2s-step-n { background: var(--m2s-page); color: var(--m2s-on-hue); }
.m2s-step.active {
    color: var(--m2s-on-hue);
    border-color: var(--m2s-page);
    background: var(--m2s-page);
    font-weight: 600;
}
.m2s-step.active .m2s-step-n { background: var(--m2s-on-hue); color: var(--m2s-page); }
.m2s-step-line { flex: 1; height: 1px; background: var(--m2s-line); min-width: 0.8rem; }

/* ---------- next step bar ---------- */
[class*="st-key-m2s_next_"] {
    position: relative;
    overflow: hidden;
    margin-top: 1.5rem;
    padding: 1.05rem 1.2rem 1.15rem 1.4rem;
    border: 1px solid var(--m2s-line);
    border-radius: 10px;
    background-color: var(--m2s-surface);
    background-image: linear-gradient(var(--m2s-page-soft), var(--m2s-page-soft));
}
[class*="st-key-m2s_next_"]::before {
    content: "";
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: var(--m2s-page);
}
.m2s-next-kicker {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    font-size: var(--m2s-fs-xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--m2s-muted);
}
.m2s-next-num {
    width: 1.25rem;
    height: 1.25rem;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: var(--m2s-fs-xs);
    font-weight: 700;
    letter-spacing: 0;
    background: var(--m2s-page);
    color: var(--m2s-on-hue);
}
.m2s-next-title {
    font-size: var(--m2s-fs-lg);
    font-weight: 600;
    margin-top: 0.3rem;
    color: var(--m2s-text);
}
.m2s-next-hint {
    font-size: var(--m2s-fs-sm);
    color: var(--m2s-muted);
    margin-top: 0.2rem;
    max-width: 64ch;
}
[class*="st-key-m2s_next_"] [data-testid="stPageLink"] a {
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    height: 42px;
    box-sizing: border-box;
    border: 0 !important;
    border-radius: 10px !important;
    padding: 0 1.05rem !important;
    text-decoration: none !important;
    background: var(--m2s-page) !important;
    color: var(--m2s-on-hue) !important;
    transition: transform 0.14s ease;
}
[class*="st-key-m2s_next_"] [data-testid="stPageLink"] a p {
    margin: 0 !important;
    font-size: var(--m2s-fs-sm) !important;
    font-weight: 600 !important;
    color: inherit !important;
}
[class*="st-key-m2s_next_"] [data-testid="stPageLink"] a [data-testid="stIconMaterial"] {
    color: inherit !important;
    font-size: 1.15rem !important;
}
[class*="st-key-m2s_next_"] [data-testid="stPageLink"] a:hover { transform: translateY(-1px); }

/* Inline jump, used next to a warning rather than as a page footer. */
[class*="st-key-cta_to_"] { max-width: 240px; }
[class*="st-key-cta_to_"] [data-testid="stPageLink"] a {
    display: inline-flex !important;
    align-items: center;
    gap: 0.4rem;
    height: 38px;
    box-sizing: border-box;
    border-radius: 9px !important;
    padding: 0 0.9rem !important;
    text-decoration: none !important;
    border: 1px solid var(--m2s-line-strong) !important;
    background: var(--m2s-surface-muted) !important;
    color: var(--m2s-text) !important;
}
[class*="st-key-cta_to_"] [data-testid="stPageLink"] a p {
    margin: 0 !important;
    font-size: var(--m2s-fs-sm) !important;
    font-weight: 600 !important;
    color: inherit !important;
}
[class*="st-key-cta_to_"] [data-testid="stPageLink"] a:hover { background: var(--m2s-surface-hover) !important; }

/* ---------- cards ---------- */
[data-testid="stVerticalBlock"][class*="st-key-m2s_card_"]:not([class*="st-key-m2s_card_body_"]) {
    border-radius: 10px !important;
    background: var(--m2s-surface) !important;
    border: 1px solid var(--m2s-line) !important;
    position: relative !important;
    box-shadow: 0 10px 28px var(--m2s-shadow) !important;
    overflow: visible !important;
}
[data-testid="stForm"] {
    border: 1px solid var(--m2s-line) !important;
    border-radius: 10px !important;
    background: var(--m2s-surface-muted) !important;
}
[class*="st-key-m2s_card_"] [data-testid="stForm"] {
    background: transparent !important;
    border: 0 !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
}
.m2s-card-kicker {
    font-size: var(--m2s-fs-xs);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--m2s-muted);
    font-weight: 700;
    line-height: 1.2;
    margin: 0.1rem 0 0.3rem 0;
    padding-right: 2.25rem;
}
.m2s-card-title {
    font-size: var(--m2s-fs-lg);
    font-weight: 600;
    line-height: 1.3;
    color: var(--m2s-text);
    margin: 0;
    padding-right: 2.25rem;
}
.m2s-card-hint {
    color: var(--m2s-muted);
    font-size: var(--m2s-fs-sm);
    line-height: 1.5;
    margin: 0 0 0.9rem 0;
}

/* Collapse control pinned to the card's top-right corner. */
[class*="st-key-m2s_foldwrap_"] {
    position: absolute !important;
    top: 8px !important;
    right: 8px !important;
    width: 28px !important;
    min-width: 28px !important;
    max-width: 28px !important;
    height: 28px !important;
    margin: 0 !important;
    padding: 0 !important;
    z-index: 5 !important;
}
[class*="st-key-m2s_foldwrap_"] [data-testid="stVerticalBlock"],
[class*="st-key-m2s_foldwrap_"] .stButton,
[class*="st-key-m2s_fold_"] {
    margin: 0 !important;
    padding: 0 !important;
    width: 28px !important;
    min-width: 28px !important;
}
[class*="st-key-m2s_fold_"] button {
    min-height: 28px !important;
    height: 28px !important;
    max-height: 28px !important;
    width: 28px !important;
    min-width: 28px !important;
    max-width: 28px !important;
    padding: 0 !important;
    border-radius: 7px !important;
    background: var(--m2s-surface-muted) !important;
    border: 1px solid var(--m2s-line) !important;
    color: var(--m2s-muted) !important;
    box-shadow: none !important;
}
[class*="st-key-m2s_fold_"] button:hover {
    background: var(--m2s-accent) !important;
    border-color: var(--m2s-accent) !important;
    color: var(--m2s-on-hue) !important;
}
[class*="st-key-m2s_fold_"] button p {
    font-size: var(--m2s-fs-sm) !important;
    font-weight: 600 !important;
    line-height: 1 !important;
    color: inherit !important;
    margin: 0 !important;
}

/* ---------- nesting option cards ---------- */
.m2s-nest-count {
    font-size: var(--m2s-fs-xs);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--m2s-muted);
    font-weight: 700;
    margin: 0 0 0.35rem 0.15rem;
}
.m2s-table-preview {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--m2s-fs-xs);
    line-height: 1.5;
    color: var(--m2s-text);
    margin: 0.35rem 0 0.15rem 0;
    white-space: pre-wrap;
}
[data-testid="stVerticalBlock"][class*="st-key-nest_on_"],
[data-testid="stVerticalBlock"][class*="st-key-nest_off_"] {
    border-radius: 10px !important;
    background: var(--m2s-surface-muted) !important;
    border: 1px solid var(--m2s-line) !important;
    box-shadow: none !important;
}
[data-testid="stVerticalBlock"][class*="st-key-nest_on_"] {
    background: var(--m2s-accent-soft) !important;
    border-color: var(--m2s-accent) !important;
    box-shadow: 0 0 0 1px var(--m2s-accent) !important;
}

/* ---------- sidebar ---------- */
section[data-testid="stSidebar"] {
    box-sizing: border-box !important;
    background: var(--m2s-surface-strong) !important;
    border-right: 1px solid var(--m2s-line);
    min-width: var(--m2s-sidebar) !important;
    width: var(--m2s-sidebar) !important;
    max-width: var(--m2s-sidebar) !important;
}
section[data-testid="stSidebar"] > div,
[data-testid="stSidebarContent"] {
    width: 100% !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
    background: transparent !important;
}
[data-testid="stSidebar"] .block-container { padding-top: calc(var(--m2s-topbar) + 0.35rem); }
[data-testid="stSidebar"][aria-expanded="true"] [data-testid="stSidebarUserContent"] {
    padding-top: calc(var(--m2s-topbar) + 0.5rem) !important;
}
[data-testid="stSidebarNav"] { display: none !important; }

[class*="st-key-nav_on_"],
[class*="st-key-nav_off_"] {
    margin: 0 0 0.45rem 0;
    width: var(--m2s-nav) !important;
    max-width: 100%;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] {
    width: var(--m2s-nav) !important;
    max-width: 100%;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] a {
    display: flex !important;
    align-items: center;
    gap: 0.6rem;
    box-sizing: border-box !important;
    width: var(--m2s-nav) !important;
    min-width: var(--m2s-nav) !important;
    max-width: var(--m2s-nav) !important;
    height: 44px !important;
    border-radius: 10px !important;
    padding: 0.55rem 0.85rem !important;
    text-decoration: none !important;
    white-space: nowrap;
    color: var(--m2s-muted) !important;
    background: var(--m2s-surface-muted) !important;
    border: 1px solid var(--m2s-line) !important;
    box-shadow: none !important;
    transition: transform 0.14s ease, background-color 0.14s ease !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] p {
    margin: 0 !important;
    font-size: var(--m2s-fs-md) !important;
    font-weight: 500 !important;
    color: inherit !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] [data-testid="stIconMaterial"] {
    font-size: 1.28rem !important;
    color: inherit !important;
}
[class*="st-key-nav_off_"] [data-testid="stPageLink"] a:hover {
    color: var(--m2s-text) !important;
    background: var(--m2s-surface-hover) !important;
    border-color: var(--m2s-line-strong) !important;
    transform: translateX(2px);
}
[class*="st-key-nav_on_"] [data-testid="stPageLink"] a {
    color: var(--m2s-text) !important;
    background: var(--m2s-page-soft) !important;
    border-color: transparent !important;
    box-shadow: inset 3px 0 0 var(--m2s-page) !important;
}
[class*="st-key-nav_on_"] [data-testid="stPageLink"] p { font-weight: 600 !important; }
[class*="st-key-nav_on_"] [data-testid="stPageLink"] [data-testid="stIconMaterial"] {
    color: var(--m2s-page-text) !important;
}

.m2s-side-block { margin-top: 1.15rem; }
.m2s-side-label {
    font-size: var(--m2s-fs-xs);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--m2s-muted);
    font-weight: 700;
    margin-bottom: 0.45rem;
}
.m2s-status {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.2rem 0;
    font-size: var(--m2s-fs-sm);
    color: var(--m2s-text);
}
.m2s-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: 0 0 8px;
    box-sizing: border-box;
}
.m2s-dot.ok { background: var(--m2s-ok); box-shadow: 0 0 0 3px var(--m2s-ok-ring); }
.m2s-dot.warn { background: var(--m2s-warn); box-shadow: 0 0 0 3px var(--m2s-warn-ring); }
.m2s-dot.err { background: var(--m2s-err); box-shadow: 0 0 0 3px var(--m2s-err-ring); }
.m2s-dot.saved { background: transparent; border: 2px solid var(--m2s-muted); }
.m2s-dot.off { background: var(--m2s-off); }
.m2s-status-value { color: var(--m2s-muted); }
.m2s-detail {
    display: grid;
    grid-template-columns: 5.25rem minmax(0, 1fr);
    column-gap: 0.5rem;
    row-gap: 0.2rem;
    align-items: baseline;
    margin: 0.25rem 0 0.85rem 1.05rem;
}
.m2s-detail-row { display: contents; }
.m2s-detail-key {
    color: var(--m2s-muted);
    font-size: var(--m2s-fs-xs);
    font-weight: 500;
}
.m2s-detail-val {
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: var(--m2s-fs-sm);
    line-height: 1.35;
    color: var(--m2s-text);
}
.m2s-side-foot {
    margin-top: 1.4rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--m2s-line);
    color: var(--m2s-muted);
    font-size: var(--m2s-fs-xs);
}
.st-key-m2s_job {
    margin-top: 0.85rem;
    padding-top: 0.55rem;
    border-top: 1px solid var(--m2s-line);
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
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarUserContent"] { padding-top: 0 !important; }
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] { display: none !important; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-label,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-detail,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-foot { display: none !important; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-block { margin-top: 0.5rem; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status {
    justify-content: center;
    width: 52px;
    padding: 0.35rem 0;
}
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status-text,
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a p,
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] [data-testid="stMarkdownContainer"] {
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    padding: 0 !important;
    margin: -1px !important;
    overflow: hidden !important;
    clip: rect(0 0 0 0) !important;
    white-space: nowrap !important;
}
[data-testid="stSidebar"][aria-expanded="false"] .st-key-m2s_job { width: 52px; }
[data-testid="stSidebar"][aria-expanded="false"] .st-key-m2s_job [data-testid="stCaptionContainer"],
[data-testid="stSidebar"][aria-expanded="false"] .st-key-m2s_job [data-testid="stButton"],
[data-testid="stSidebar"][aria-expanded="false"] .st-key-m2s_job .stButton { display: none !important; }
[data-testid="stSidebar"][aria-expanded="false"] [class*="st-key-nav_on_"],
[data-testid="stSidebar"][aria-expanded="false"] [class*="st-key-nav_off_"],
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] {
    width: 52px !important;
    min-width: 52px !important;
    max-width: 52px !important;
    margin-left: 0 !important;
    margin-right: auto !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a {
    justify-content: center !important;
    align-items: center !important;
    position: relative;
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

/* ---------- controls ---------- */
/* Selectboxes fill their column like stretch buttons (same width as "Bağlantıyı dene"). */
[data-testid="stSelectbox"],
[data-testid="stSelectbox"] > div,
[data-testid="stSelectbox"] .react-aria-ComboBox,
[data-testid="stSelectbox"] .react-aria-ComboBox > div[role="group"] {
    width: 100% !important;
    max-width: 100% !important;
    box-sizing: border-box !important;
}
.st-key-mongo_test button,
.st-key-sql_test button {
    background: var(--m2s-accent-soft) !important;
    color: var(--m2s-accent-text) !important;
    border: 1px solid var(--m2s-accent-line) !important;
}
.st-key-mongo_test button p,
.st-key-sql_test button p { color: inherit !important; }
.st-key-mongo_test button:hover,
.st-key-sql_test button:hover {
    background: light-dark(rgb(9 105 218 / 0.16), rgb(76 141 255 / 0.24)) !important;
    border-color: var(--m2s-accent) !important;
}
[data-testid="stMetricLabel"] p {
    font-size: var(--m2s-fs-xs) !important;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-weight: 600 !important;
    color: var(--m2s-muted) !important;
}
.stTabs [data-baseweb="tab-list"] { gap: 0.4rem; }
.stTabs [data-baseweb="tab"] { padding: 0.4rem 0.9rem; }

/* Placeholders are examples ("gri yazı"): muted, never mistaken for a saved value. */
[data-testid="stTextInput"] input::placeholder,
[data-testid="stNumberInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder {
    color: var(--m2s-muted) !important;
    -webkit-text-fill-color: var(--m2s-muted) !important;
    opacity: 0.85 !important;
}
/* Example text leaves the field as soon as it gets focus. */
[data-testid="stTextInput"] input:focus::placeholder,
[data-testid="stNumberInput"] input:focus::placeholder,
[data-testid="stTextArea"] textarea:focus::placeholder {
    color: transparent !important;
    -webkit-text-fill-color: transparent !important;
    opacity: 0 !important;
}

/* Open expander shows the full script so the page grows; click the header to collapse. */
[data-testid="stExpander"] [data-testid="stCode"],
[data-testid="stExpander"] pre {
    max-height: none !important;
    overflow: visible !important;
}
/* Column picker: search + fullscreen sit on the grid's top-right. */
.st-key-tr_cols_grid [data-testid="stElementToolbar"] {
    opacity: 1 !important;
    visibility: visible !important;
    gap: 0.2rem !important;
    padding: 0.18rem !important;
}
.st-key-tr_cols_grid [data-testid="stElementToolbarButton"] {
    width: 2.15rem !important;
    height: 2.15rem !important;
    min-width: 2.15rem !important;
    min-height: 2.15rem !important;
    padding: 0 !important;
    border-radius: 8px !important;
    background: var(--m2s-surface-hover) !important;
    border: 1px solid var(--m2s-line) !important;
    color: var(--m2s-text) !important;
    opacity: 1 !important;
}
.st-key-tr_cols_grid [data-testid="stElementToolbarButton"]:hover {
    background: var(--m2s-accent-soft) !important;
    border-color: var(--m2s-accent-line) !important;
}
.st-key-tr_cols_grid [data-testid="stElementToolbarButtonIcon"],
.st-key-tr_cols_grid [data-testid="stElementToolbarButton"] svg,
.st-key-tr_cols_grid [data-testid="stElementToolbarButton"] span {
    font-size: 1.28rem !important;
    width: 1.28rem !important;
    height: 1.28rem !important;
    line-height: 1 !important;
    color: inherit !important;
    fill: currentColor !important;
}
.st-key-m2s_shell {
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    overflow: hidden !important;
    position: absolute !important;
}

/* ---------- keyboard focus ---------- */
[data-testid="stSidebar"] [data-testid="stPageLink"] a:focus-visible,
[class*="st-key-m2s_next_"] [data-testid="stPageLink"] a:focus-visible,
[class*="st-key-cta_to_"] [data-testid="stPageLink"] a:focus-visible,
[class*="st-key-m2s_fold_"] button:focus-visible,
.m2s-topbar button:focus-visible {
    outline: 2px solid var(--m2s-focus) !important;
    outline-offset: 2px !important;
}

@media (prefers-reduced-motion: reduce) {
    [data-testid="stSidebar"] [data-testid="stPageLink"] a,
    [class*="st-key-m2s_next_"] [data-testid="stPageLink"] a { transition: none !important; }
    [data-testid="stSidebar"] [data-testid="stPageLink"] a:hover,
    [class*="st-key-m2s_next_"] [data-testid="stPageLink"] a:hover { transform: none !important; }
}
@media (prefers-reduced-transparency: reduce) {
    :root {
        --m2s-surface: light-dark(#ffffff, #161b22);
        --m2s-surface-strong: light-dark(#f6f8fa, #0d1117);
        --m2s-app-bg: var(--m2s-bg);
    }
    .m2s-topbar {
        backdrop-filter: none !important;
        -webkit-backdrop-filter: none !important;
    }
}
"""

# No `<` operators in here: SHELL below escapes every `<` so the markup in the strings
# does not look like HTML to Streamlit's sanitizer.
_SHELL_JS = """
(function () {
  var win = window.parent && window.parent !== window ? window.parent : window;
  var doc = win.document;
  var VERSION = "5";
  var prev = win.__m2sShell;
  if (prev && prev.v === VERSION) { prev.mount(); return; }
  if (prev && prev.timer) win.clearInterval(prev.timer);

  var THEME_KEY = "m2s-theme";
  var FIX_KEY = "m2s-theme-fix";
  var PAGES = { connections: "Bağlantılar", discovery: "Şema keşfi", transfer: "SQL aktarımı" };
  var SUBPAGES = ["discovery", "transfer"];
  var ICON_LEFT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>';
  var ICON_RIGHT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg>';
  var ICON_MOON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 14.3A8.4 8.4 0 1 1 9.7 3 7 7 0 0 0 21 14.3z"/></svg>';
  var ICON_SUN = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/>' +
    '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';

  function store(name) { try { return win[name]; } catch (err) { return null; } }
  var local = store("localStorage");
  var session = store("sessionStorage");
  function read(where, key) { try { return where ? where.getItem(key) : null; } catch (err) { return null; } }
  function write(where, key, value) { try { if (where) where.setItem(key, value); } catch (err) {} }

  // The theme Streamlit is showing, as set on the app root.
  function shownScheme() {
    var app = doc.querySelector('[data-testid="stApp"]');
    var scheme = app ? win.getComputedStyle(app).colorScheme || "" : "";
    if (scheme.indexOf("light") !== -1) return "light";
    if (scheme.indexOf("dark") !== -1) return "dark";
    return null;
  }
  function storedChoice() {
    var value = read(local, THEME_KEY);
    return value === "light" || value === "dark" ? value : null;
  }
  // Streamlit caches the theme per path the app was loaded on, so every path gets it.
  function themeKeys() {
    var base = (win.location.pathname || "/").replace(/\\/(discovery|transfer|connections)\\/?$/, "/");
    if (base.charAt(base.length - 1) !== "/") base += "/";
    var keys = {};
    [base, base.length > 1 ? base.slice(0, -1) : ""].concat(
      SUBPAGES.map(function (name) { return base + name; })
    ).forEach(function (path) { if (path) keys["stActiveTheme-" + path + "-v2"] = true; });
    try {
      Object.keys(local || {}).forEach(function (key) {
        if (/^stActiveTheme-.*-v2$/.test(key)) keys[key] = true;
      });
    } catch (err) {}
    return Object.keys(keys);
  }
  function persist(kind) {
    write(local, THEME_KEY, kind);
    var value = JSON.stringify(kind === "light" ? "Light" : "Dark");
    themeKeys().forEach(function (key) { write(local, key, value); });
  }
  // A path entered for the first time may still carry an older theme: fix it once.
  function syncTheme() {
    var choice = storedChoice();
    if (!choice) return false;
    persist(choice);
    var shown = shownScheme();
    if (shown && shown !== choice && read(session, FIX_KEY) !== choice) {
      write(session, FIX_KEY, choice);
      win.location.reload();
      return true;
    }
    return false;
  }
  function choose(kind) {
    persist(kind);
    if (shownScheme() === kind) { reflect(); return; }
    try { if (session) session.removeItem(FIX_KEY); } catch (err) {}
    win.location.reload();
  }
  function reflect() {
    var shown = shownScheme();
    // The top bar lives outside the app root; give it the same color scheme.
    if (shown && doc.documentElement.style.colorScheme !== shown) {
      doc.documentElement.style.colorScheme = shown;
    }
    var mode = shown || storedChoice() || "dark";
    var moon = doc.querySelector(".m2s-topbar .m2s-theme-moon");
    var sun = doc.querySelector(".m2s-topbar .m2s-theme-sun");
    if (moon) moon.setAttribute("aria-pressed", mode === "dark" ? "true" : "false");
    if (sun) sun.setAttribute("aria-pressed", mode === "light" ? "true" : "false");
  }

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
  function toggleSidebar() {
    var native = nativeSidebarButton(sidebarOpen());
    if (native) {
      native.style.pointerEvents = "auto";
      native.click();
    }
    win.setTimeout(tick, 50);
    win.setTimeout(tick, 250);
  }

  function buildBar() {
    var bar = doc.querySelector(".m2s-topbar");
    if (bar && bar.getAttribute("data-v") === VERSION) return bar;
    if (bar) bar.remove();
    bar = doc.createElement("div");
    bar.className = "m2s-topbar";
    bar.setAttribute("data-v", VERSION);
    bar.innerHTML =
      '<div class="m2s-topbar-brand">' +
        '<div class="m2s-logo" aria-hidden="true">M2S</div>' +
        '<div class="m2s-topbar-name">Mongo2SQLConverter</div>' +
        '<button type="button" class="m2s-topbar-toggle"></button>' +
      '</div>' +
      '<div class="m2s-topbar-main">' +
        '<nav class="m2s-topbar-crumbs" aria-label="Konum">' +
          '<span class="m2s-crumb-root">Mongo2SQL</span>' +
          '<span class="m2s-crumb-sep" aria-hidden="true">›</span>' +
          '<span class="m2s-crumb-page" aria-current="page"></span>' +
        '</nav>' +
        '<div class="m2s-topbar-actions">' +
          '<div class="m2s-theme-dock" role="group" aria-label="Tema">' +
            '<button type="button" class="m2s-theme-btn m2s-theme-moon" title="Koyu tema" aria-label="Koyu tema">' +
              ICON_MOON + '</button>' +
            '<button type="button" class="m2s-theme-btn m2s-theme-sun" title="Açık tema" aria-label="Açık tema">' +
              ICON_SUN + '</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    doc.body.appendChild(bar);
    bar.querySelector(".m2s-topbar-toggle").addEventListener("click", toggleSidebar);
    bar.querySelector(".m2s-theme-moon").addEventListener("click", function () { choose("dark"); });
    bar.querySelector(".m2s-theme-sun").addEventListener("click", function () { choose("light"); });
    return bar;
  }
  function syncChrome(bar) {
    var sidebar = doc.querySelector('[data-testid="stSidebar"]');
    var open = sidebarOpen();
    bar.classList.toggle("is-collapsed", !open);
    var toggle = bar.querySelector(".m2s-topbar-toggle");
    var label = open ? "Menüyü daralt" : "Menüyü aç";
    if (toggle.getAttribute("aria-label") !== label) {
      toggle.innerHTML = open ? ICON_LEFT : ICON_RIGHT;
      toggle.setAttribute("title", label);
      toggle.setAttribute("aria-label", label);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    var brand = bar.querySelector(".m2s-topbar-brand");
    if (sidebar && brand) {
      var width = Math.round(sidebar.getBoundingClientRect().width);
      if (width > 8 && brand.style.width !== width + "px") {
        brand.style.flex = "0 0 " + width + "px";
        brand.style.width = width + "px";
        brand.style.minWidth = width + "px";
        brand.style.maxWidth = width + "px";
      }
    }
  }
  function pageFromPath(path) {
    var raw = (path || "").split("?")[0];
    if (/\\/discovery\\/?$/.test(raw)) return "discovery";
    if (/\\/transfer\\/?$/.test(raw)) return "transfer";
    return "connections";
  }
  function updateCrumbs(bar) {
    var key = pageFromPath(win.location.pathname);
    var crumb = bar.querySelector(".m2s-crumb-page");
    if (crumb.getAttribute("data-page") !== key) {
      crumb.textContent = PAGES[key];
      crumb.setAttribute("data-page", key);
    }
  }
  // Icon-only links in the rail keep a name and a tooltip; icon ligatures stay silent.
  function labelNav() {
    var rail = !sidebarOpen();
    var links = doc.querySelectorAll('[data-testid="stSidebar"] [data-testid="stPageLink"] a');
    Array.prototype.forEach.call(links, function (link) {
      var icon = link.querySelector('[data-testid="stIconMaterial"]');
      if (icon && icon.getAttribute("aria-hidden") !== "true") icon.setAttribute("aria-hidden", "true");
      var label = link.querySelector('[data-testid="stMarkdownContainer"]');
      var text = label ? label.textContent.trim() : "";
      if (rail && text) {
        if (link.getAttribute("title") !== text) link.setAttribute("title", text);
      } else if (link.hasAttribute("title")) {
        link.removeAttribute("title");
      }
    });
  }
  function tick() {
    var bar = buildBar();
    syncChrome(bar);
    updateCrumbs(bar);
    labelNav();
    reflect();
  }
  function mount() {
    if (syncTheme()) return;
    tick();
  }
  win.__m2sShell = { v: VERSION, mount: mount, timer: win.setInterval(tick, 400) };
  mount();
})();
"""
# DOMPurify removes a <script> whose text contains markup, so each `<` inside it
# becomes the `\\x3c` string escape; the JS above only uses `<` inside strings.
SHELL = "<script>" + _SHELL_JS.replace("<", "\\x3c") + "</script>"

PAGES: dict[str, object] = {}

STEPS = (
    ("connections", "1", "Bağlantılar"),
    ("discovery", "2", "Şema keşfi"),
    ("transfer", "3", "SQL aktarımı"),
)
# Steps the session has really completed; main.py fills it every run.
STEPS_DONE_KEY = "m2s_steps_done"
_STEPPER_SLOT = "m2s_stepper_slot"

_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*")


def rich(text: str | None) -> str:
    """Escape text for raw HTML, keeping Markdown `code` and **bold** spans."""
    safe = escape(text or "", quote=False)
    safe = _CODE_SPAN.sub(r"<code>\1</code>", safe)
    return _BOLD_SPAN.sub(r"<strong>\1</strong>", safe)


def inject_css() -> None:
    # Style-only HTML goes to Streamlit's event container, so it takes no room on the page.
    st.html(f"<style>{CSS}</style>")


def theme_toggle() -> None:
    """Fixed top bar: logo, breadcrumb and moon/sun. Mounted once per page, no rerun on click."""
    with st.container(key="m2s_shell"):
        st.html(SHELL, unsafe_allow_javascript=True)


def register_pages(pages: dict[str, object]) -> None:
    """Page objects from `st.navigation`, used by in-page CTAs."""
    PAGES.update(pages)


def _stepper_html(active: str, done: Iterable[str]) -> str:
    finished = set(done)
    chips: list[str] = []
    for i, (key, num, label) in enumerate(STEPS):
        if i:
            chips.append('<span class="m2s-step-line"></span>')
        attrs = ""
        if key == active:
            kind = "active"
            attrs = ' aria-current="step"'
            note = '<span class="m2s-sr"> (şu an)</span>'
        elif key in finished:
            kind = "done"
            note = '<span class="m2s-sr"> (tamamlandı)</span>'
        else:
            kind = "todo"
            note = ""
        chips.append(
            f'<div class="m2s-step m2s-step-{key} {kind}"{attrs}>'
            f'<span class="m2s-step-n">{num}</span>{label}{note}</div>'
        )
    return f'<nav class="m2s-stepper" aria-label="Adımlar">{"".join(chips)}</nav>'


def stepper(active: str) -> None:
    """Bağlantılar → Şema keşfi → SQL aktarımı; done steps come from the session."""
    done = frozenset(st.session_state.get(STEPS_DONE_KEY) or ())
    slot = st.empty()
    slot.html(_stepper_html(active, done))
    st.session_state[_STEPPER_SLOT] = (slot, active, done)


def forget_stepper() -> None:
    """Drop the previous run's stepper slot before a page renders."""
    st.session_state.pop(_STEPPER_SLOT, None)


def refresh_stepper() -> None:
    """Redraw the stepper when the page itself finished a step during this run."""
    entry = st.session_state.pop(_STEPPER_SLOT, None)
    if not entry:
        return
    slot, active, drawn = entry
    done = frozenset(st.session_state.get(STEPS_DONE_KEY) or ())
    if done != drawn:
        slot.html(_stepper_html(active, done))


def page_cta(page_key: str, label: str, icon: str, widget_key: str) -> None:
    """Compact inline jump, for use next to a warning."""
    page = PAGES.get(page_key)
    if page is None:
        return
    with st.container(key=widget_key):
        st.page_link(page, label=label, icon=icon, width="stretch")


NEXT_STEPS = {
    "connections": {
        "num": "1",
        "title": "Bağlantılar",
        "label": "Bağlantılara git",
        "icon": ":material/settings_ethernet:",
    },
    "discovery": {
        "num": "2",
        "title": "Şema keşfi",
        "label": "Şema keşfine geç",
        "icon": ":material/schema:",
    },
    "transfer": {
        "num": "3",
        "title": "SQL aktarımı",
        "label": "SQL aktarımına geç",
        "icon": ":material/moving:",
    },
}


def next_step(page_key: str, hint: str, kicker: str = "Sıradaki adım") -> None:
    """
    Footer bar that hands the user to the next page.

    Reads as a closing section of the page rather than a stray button: the step
    number, its name and what it does on the left, one clear action on the right.
    """
    page = PAGES.get(page_key)
    meta = NEXT_STEPS.get(page_key)
    if page is None or meta is None:
        return
    with st.container(key=f"m2s_next_{page_key}"):
        row = st.columns([3.4, 1.35], vertical_alignment="center")
        with row[0]:
            st.markdown(
                f'<div class="m2s-next-kicker">'
                f'<span class="m2s-next-num">{meta["num"]}</span>{escape(kicker)}</div>'
                f'<div class="m2s-next-title">{meta["title"]}</div>'
                f'<div class="m2s-next-hint">{rich(hint)}</div>',
                unsafe_allow_html=True,
            )
        with row[1]:
            st.page_link(page, label=meta["label"], icon=meta["icon"], width="stretch")


def need_connections(blockers: Sequence[str]) -> None:
    st.warning(" ve ".join(blockers) + " eksik. Önce bağlantıları kaydedin.")
    page_cta(
        "connections",
        "Bağlantılara git",
        ":material/settings_ethernet:",
        "cta_to_connections",
    )


def error_with_detail(summary: str, detail: str | None = None) -> None:
    """A short error for people, with the raw driver message folded away."""
    st.error(summary, icon=":material/error:")
    if detail and detail != summary:
        with st.expander("Ayrıntı"):
            st.code(detail, language="text", wrap_lines=True)


def nav_menu(
    items: Sequence[tuple[str, object, str, str]], current: str | None = None
) -> None:
    """Page links at the top of the sidebar; `current` is the active page key."""
    for key, page, icon, label in items:
        state = "on" if key == current else "off"
        with st.container(key=f"nav_{state}_{key}"):
            st.page_link(page, label=label, icon=icon, width="content")


def status_row(label: str, value: str, state: State = "off") -> str:
    """One status line. In the collapsed rail only its dot shows; the text stays readable."""
    shown = escape(value)
    return (
        f'<div class="m2s-status" title="{escape(label)}: {shown}">'
        f'<span class="m2s-dot {state}" aria-hidden="true"></span>'
        f'<span class="m2s-status-text">{escape(label)}'
        f'<span class="m2s-status-value"> · {shown}</span></span></div>'
    )


def status_detail(rows: Iterable[tuple[str, str]]) -> str:
    """Key/value lines under a status row. Hidden while the sidebar is a rail."""
    cells = "".join(
        f'<div class="m2s-detail-row">'
        f'<span class="m2s-detail-key">{escape(key)}</span>'
        f'<span class="m2s-detail-val" title="{escape(value)}">{escape(value)}</span>'
        f"</div>"
        for key, value in rows
    )
    return f'<div class="m2s-detail">{cells}</div>'


def sidebar_block(label: str, rows: Iterable[str]) -> str:
    return (
        f'<div class="m2s-side-block"><div class="m2s-side-label">{escape(label)}</div>'
        + "".join(rows)
        + "</div>"
    )


def sidebar_foot(text: str) -> str:
    return f'<div class="m2s-side-foot">{escape(text)}</div>'


def sidebar_panel(blocks: Sequence[str], slot=None) -> None:
    """Status blocks; `slot` (an `st.empty()`) lets main.py redraw them after the page ran."""
    target = st if slot is None else slot
    target.markdown("".join(blocks), unsafe_allow_html=True)


def page_header(title: str, lede: str, *, step: str) -> None:
    stepper(step)
    st.html(
        f'<h1 class="m2s-title m2s-title-{step}">{escape(title)}</h1>'
        f'<p class="m2s-lede">{rich(lede)}</p>'
    )


@contextmanager
def collapsible_card(
    card_id: str,
    title: str,
    hint: str | None = None,
    *,
    kicker: str | None = None,
    foldable: bool = True,
) -> Iterator[None]:
    """Bordered page block with a small open/collapse control on the title row."""
    safe_id = re.sub(r"[^0-9A-Za-z_]+", "_", card_id)
    collapsed_key = f"m2s_collapsed_{safe_id}"
    collapsed = foldable and bool(st.session_state.get(collapsed_key, False))

    def _toggle_card(key: str = collapsed_key) -> None:
        st.session_state[key] = not bool(st.session_state.get(key, False))

    with st.container(border=True, key=f"m2s_card_{safe_id}"):
        if foldable:
            with st.container(key=f"m2s_foldwrap_{safe_id}"):
                st.button(
                    "▸" if collapsed else "▾",
                    key=f"m2s_fold_{safe_id}",
                    help="Kutuyu aç" if collapsed else "Kutuyu daralt",
                    on_click=_toggle_card,
                    type="secondary",
                    width="content",
                )
        heading = f'<div class="m2s-card-kicker">{escape(kicker)}</div>' if kicker else ""
        st.markdown(
            f'{heading}<div class="m2s-card-title">{escape(title)}</div>',
            unsafe_allow_html=True,
        )
        body_id = f"m2s_card_body_{safe_id}"
        if collapsed:
            st.markdown(
                f"<style>[class*='st-key-{body_id}']{{display:none!important;height:0!important;"
                f"min-height:0!important;overflow:hidden!important;margin:0!important;"
                f"padding:0!important;}}</style>",
                unsafe_allow_html=True,
            )
        with st.container(key=body_id):
            if hint:
                st.markdown(f'<p class="m2s-card-hint">{rich(hint)}</p>', unsafe_allow_html=True)
            yield
