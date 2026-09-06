"""
Mesh theme for Streamlit apps.

The palette the devondoes.dev estate uses — the platform, the admin portal,
the finance dashboard and the drill. This file was the BI Console theme, which
is devontroedel.com's identity; the movie tracker lives on movies.devondoes.dev
and belongs with the others.

Every rule below already went through the tokens, so the move is a retarget of
:root and nothing else.
Usage:
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "bi-console-ds"))
    from streamlit_theme import apply_theme
    apply_theme(st)
"""

import streamlit as st

_CSS = """
<style>
/* ── Fonts (an @import must precede every other rule, or browsers drop it) ── */
@import url('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=Bricolage+Grotesque:opsz,wght@12..96,400..800&family=JetBrains+Mono:wght@400;500;700&display=swap');

/* ── Tokens (BI Console palette — kept in sync with the design system) ────── */
:root {
  --color-bg:           #0B0D12;
  --color-bg-elevated:  #171C26;
  --color-surface:      #141821;
  --color-border:       #262C38;
  --color-text:         #E8E4DC;
  --color-text-body:    #D3CEC4;
  --color-text-muted:   #9AA1AE;
  --color-text-faint:   #6B7280;
  --color-text-dark:    #3A4150;
  --color-accent:       #C86A3A;
  --color-accent-hover: #E08A56;
  --color-accent-light: #E08A56;
  --color-accent-soft:  rgba(200,106,58,0.12);
  /* Yellower than the accent on purpose: copper is already orange, so an
     orange "needs attention" would read as decoration rather than a signal. */
  --color-amber:        #E3B341;
  --color-amber-bg:     rgba(227,179,65,0.12);
  --font-sans:    'Inter Tight', system-ui, sans-serif;
  --font-display: 'Bricolage Grotesque', system-ui, sans-serif;
  --font-mono:    'JetBrains Mono', monospace;
  --radius:    12px;
  --radius-sm: 10px;
}

/* ── App shell ──────────────────────────────────────────────────────────── */
.stApp {
  background-color: var(--color-bg);
  /* 64px terminal grid, fixed so it doesn't scroll with content */
  background-image:
    linear-gradient(rgba(255,255,255,0.022) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.022) 1px, transparent 1px);
  background-size: 64px 64px;
  background-attachment: fixed;
  color: var(--color-text-body);
  font-family: var(--font-sans);
}
.stMarkdown, [data-testid="stMarkdownContainer"], .stApp p, .stApp li {
  font-family: var(--font-sans);
}

/* Subtle radial glow */
.stApp::before {
  content: '';
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(60rem 40rem at 50% -10%, rgba(45,212,191,0.07), transparent 70%),
    radial-gradient(40rem 30rem at 100% 0%, rgba(240,168,104,0.05), transparent 60%);
}

/* ── Headings ───────────────────────────────────────────────────────────── */
h1, h2, h3, h4 {
  font-family: var(--font-display) !important;
  color: var(--color-text) !important;
  letter-spacing: -0.02em !important;
}

/* ── Sidebar ────────────────────────────────────────────────────────────── */
[data-testid="stSidebar"] {
  background: var(--color-bg-elevated) !important;
  border-right: 1px solid var(--color-border) !important;
}

/* ── Metric cards ───────────────────────────────────────────────────────── */
[data-testid="stMetric"] {
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  padding: 16px 20px;
}

[data-testid="stMetricLabel"] {
  font-family: var(--font-mono) !important;
  font-size: 11px !important;
  letter-spacing: 0.06em !important;
  color: var(--color-text-faint) !important;
  text-transform: uppercase;
}

[data-testid="stMetricValue"] {
  font-family: var(--font-display) !important;
  color: var(--color-accent-hover) !important;
  font-size: 1.75rem !important;
}

[data-testid="stMetricDelta"] svg { display: none; }

/* ── Tabs ───────────────────────────────────────────────────────────────── */
[data-testid="stTabs"] [role="tab"] {
  font-family: var(--font-mono) !important;
  font-size: 13px !important;
  color: var(--color-text-muted) !important;
}

[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  color: var(--color-accent-hover) !important;
  border-bottom-color: var(--color-accent-hover) !important;
}

/* ── Dataframes / tables ────────────────────────────────────────────────── */
[data-testid="stDataFrame"] {
  border: 1px solid var(--color-border) !important;
  border-radius: var(--radius) !important;
  overflow: hidden;
}

/* ── Buttons ────────────────────────────────────────────────────────────── */
[data-testid="stButton"] button[kind="primary"] {
  background: var(--color-accent-hover) !important;
  color: var(--color-bg) !important;
  border: none !important;
  border-radius: var(--radius-sm) !important;
  font-family: var(--font-mono) !important;
  font-weight: 600 !important;
}

[data-testid="stButton"] button[kind="secondary"] {
  background: transparent !important;
  color: var(--color-accent-hover) !important;
  border: 1px solid var(--color-accent) !important;
  border-radius: var(--radius-sm) !important;
  font-family: var(--font-mono) !important;
}

/* ── Inputs / selects ───────────────────────────────────────────────────── */
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] select,
[data-testid="stNumberInput"] input {
  background: var(--color-surface) !important;
  border: 1px solid var(--color-border) !important;
  border-radius: var(--radius-sm) !important;
  color: var(--color-text) !important;
  font-family: var(--font-mono) !important;
}

[data-testid="stTextInput"] input:focus,
[data-testid="stSelectbox"] select:focus,
[data-testid="stNumberInput"] input:focus {
  border-color: var(--color-accent) !important;
  box-shadow: 0 0 0 2px var(--color-accent-soft) !important;
}

/* ── Expander ───────────────────────────────────────────────────────────── */
[data-testid="stExpander"] {
  background: var(--color-bg-elevated) !important;
  border: 1px solid var(--color-border) !important;
  border-radius: var(--radius) !important;
}

/* ── Divider ────────────────────────────────────────────────────────────── */
hr {
  border-color: var(--color-border) !important;
}

/* ── Code blocks ────────────────────────────────────────────────────────── */
code {
  font-family: var(--font-mono) !important;
  background: var(--color-surface) !important;
  border-radius: 5px;
  padding: 0.15em 0.4em;
  color: var(--color-accent-light) !important;
  font-size: 0.875em !important;
}
</style>
"""


def apply_theme(st_module=None) -> None:
    """Inject the BI Console theme into a Streamlit app."""
    target = st_module or st
    target.markdown(_CSS, unsafe_allow_html=True)
