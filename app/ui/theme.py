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
    margin: 0.4rem 0 1.1rem 0;
    max-width: 68ch;
}

/* ---------- stepper ---------- */
.m2s-stepper {
    display: flex; align-items: center; gap: 0.4rem;
    margin: 0 0 1.45rem 0;
}
.m2s-step {
    display: flex; align-items: center; gap: 0.45rem;
    padding: 0.32rem 0.72rem;
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
.m2s-step.active {
    color: #d7e8ff;
    border-color: rgba(76, 141, 255, 0.55);
    background: var(--m2s-accent-soft);
}
.m2s-step.active .m2s-step-n { background: var(--m2s-accent); color: #041018; }
.m2s-step.done { color: #9ee3a8; border-color: rgba(63, 185, 80, 0.35); }
.m2s-step.done .m2s-step-n { background: rgba(63, 185, 80, 0.28); color: #b6f0be; }
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
    margin: 0 0 1rem 0; padding: 0.1rem 0.05rem 1rem 0.05rem;
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
    color: #f0d4ff !important;
    background: linear-gradient(135deg, rgba(167, 139, 250, 0.44), rgba(232, 121, 249, 0.16)) !important;
    border: 1px solid #c084fc !important;
    box-shadow: inset 3px 0 0 #c084fc, 0 8px 20px rgba(167, 139, 250, 0.18);
}
.st-key-nav_conn [data-testid="stPageLink"] a:hover,
.st-key-nav_conn [data-testid="stPageLink"] a[aria-current="page"] {
    background: linear-gradient(135deg, rgba(167, 139, 250, 0.62), rgba(232, 121, 249, 0.26)) !important;
    box-shadow: inset 3px 0 0 #e9d5ff, 0 0 24px rgba(167, 139, 250, 0.44);
}
.st-key-nav_conn [data-testid="stIconMaterial"],
.st-key-nav_conn [data-testid="stPageLink"] p { color: #e9b8ff !important; }

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
[data-testid="stSidebar"][aria-expanded="false"] .m2s-logo {
    width: 36px; height: 36px; flex-basis: 36px; font-size: 0.62rem;
}
[data-testid="stSidebar"][aria-expanded="false"] .m2s-side-block {
    margin-top: 0.7rem; padding-top: 0.7rem;
    border-top: 1px solid var(--m2s-border);
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a {
    justify-content: center !important;
    font-size: 0 !important;
    padding: 0.55rem 0.3rem !important;
}
[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stPageLink"] a span {
    display: none !important;
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
.st-key-disc_database_drdl button {
    min-height: 3rem !important;
    font-size: 1.05rem !important;
    font-weight: 750 !important;
    letter-spacing: 0.02em;
    background: linear-gradient(135deg, #4c8dff, #22d3ee) !important;
    color: #041018 !important;
    border: 0 !important;
    box-shadow: 0 8px 22px rgba(34, 211, 238, 0.28);
}
.st-key-disc_database_drdl button:hover {
    filter: brightness(1.08);
    box-shadow: 0 10px 28px rgba(34, 211, 238, 0.4);
}
.st-key-disc_database_drdl button:disabled {
    opacity: 0.45 !important;
    box-shadow: none !important;
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

PAGES: dict[str, object] = {}

STEPS = (
    ("connections", "1", "Bağlantılar"),
    ("discovery", "2", "Şema keşfi"),
    ("transfer", "3", "SQL aktarımı"),
)


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


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
            f'<div class="m2s-step {kind}">'
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
