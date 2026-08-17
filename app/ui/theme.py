"""Application shell: styling, brand block and small layout helpers."""

from __future__ import annotations

from typing import Iterable, Literal, Sequence

import streamlit as st

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
    --m2s-rail: 62px;
}

/* Keep the toolbar mounted: it hosts the button that reopens the sidebar. */
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], footer { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stExpandSidebarButton"] {
    background: rgba(240, 246, 252, 0.06) !important;
    border: 1px solid var(--m2s-border) !important;
    border-radius: 8px !important;
    padding: 0.15rem 0.25rem !important;
}
[data-testid="stExpandSidebarButton"]:hover {
    background: var(--m2s-accent-soft) !important;
    border-color: rgba(76, 141, 255, 0.45) !important;
}

.block-container {
    max-width: 1220px;
    padding-top: 2.4rem;
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
    margin: 0.4rem 0 1.6rem 0;
    max-width: 68ch;
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

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] { border-right: 1px solid var(--m2s-border); }
[data-testid="stSidebar"] .block-container { padding-top: 1.6rem; }
.m2s-brand { display: flex; align-items: center; gap: 0.7rem; margin: 0 0 1.2rem 0; }
.m2s-logo {
    width: 38px; height: 38px; border-radius: 10px;
    background: linear-gradient(140deg, var(--m2s-accent), #7b5cff);
    color: #fff; font-weight: 700; font-size: 0.82rem;
    display: flex; align-items: center; justify-content: center;
    flex: 0 0 38px;
}
.m2s-brand-name { font-weight: 650; font-size: 0.98rem; line-height: 1.15; }
.m2s-brand-sub { color: var(--m2s-muted); font-size: 0.76rem; }

.m2s-side-block { margin-top: 1.3rem; }
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

[data-testid="stSidebarNav"] { padding-top: 0.2rem; }
[data-testid="stSidebarNavLink"] { border-radius: 9px; margin: 2px 0; }
[data-testid="stSidebarNavLink"]:hover { background: rgba(240, 246, 252, 0.05); }
[data-testid="stSidebarNavLink"][aria-current="page"] { background: var(--m2s-accent-soft); }

/* ---------- collapsed sidebar keeps a narrow icon rail ---------- */
[data-testid="stSidebar"][aria-expanded="false"] {
    min-width: var(--m2s-rail) !important;
    max-width: var(--m2s-rail) !important;
    transform: none !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarContent"] {
    padding-left: 0.4rem !important;
    padding-right: 0.4rem !important;
    overflow-x: hidden;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] {
    display: flex !important;
    opacity: 1 !important;
}
[data-testid="stSidebar"][aria-expanded="false"] .m2s-brand,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status { justify-content: center; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-brand-text,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-label,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-status-text,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-foot,
[data-testid="stSidebar"][aria-expanded="false"] .m2s-rail-hide { display: none !important; }
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-block {
    margin-top: 0.7rem; padding-top: 0.7rem;
    border-top: 1px solid var(--m2s-border);
}
/* zero font-size hides the link label; the icon carries its own size */
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavLink"] {
    justify-content: center;
    font-size: 0 !important;
    padding-left: 0.3rem; padding-right: 0.3rem;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavLink"] > span { margin: 0 !important; }

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
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def brand() -> None:
    st.markdown(
        """
        <div class="m2s-brand">
            <div class="m2s-logo">M2S</div>
            <div class="m2s-brand-text">
                <div class="m2s-brand-name">Mongo2SQL</div>
                <div class="m2s-brand-sub">Schema &amp; Transfer Suite</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


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


def page_header(eyebrow: str, title: str, lede: str) -> None:
    st.markdown(
        f'<div class="m2s-eyebrow">{eyebrow}</div>'
        f'<h1 class="m2s-title">{title}</h1>'
        f'<p class="m2s-lede">{lede}</p>',
        unsafe_allow_html=True,
    )


def card_title(title: str, hint: str | None = None) -> None:
    st.markdown(f'<div class="m2s-card-title">{title}</div>', unsafe_allow_html=True)
    if hint:
        st.markdown(f'<p class="m2s-card-hint">{hint}</p>', unsafe_allow_html=True)
