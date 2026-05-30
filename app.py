"""
Auto Pipeline — Streamlit UI.

Run: streamlit run app.py
Or:  double-click run_pipeline.bat

Routes (st.session_state["route"]):
  "landing"  — pick a saved project or create a new one
  "create"   — new-project wizard (folders + per-product briefs)
  "loading"  — first-run auto-prepare progress bar
  "board"    — Pinterest-style board view (default after a project is loaded)
  "detail"   — per-photo detail view (edit prompt, regenerate, pick variant, regrade)
"""

from __future__ import annotations

import base64
import functools
import sys
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import streamlit as st
from PIL import Image
from streamlit_autorefresh import st_autorefresh

# Make `from src.* import ...` work when launched via `streamlit run app.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import manifest as M  # noqa: E402
from src import project as PROJ  # noqa: E402
from src.anthropic_client import AnthropicClient  # noqa: E402
from src.config import load_config  # noqa: E402
from src.gemini_client import GeminiClient  # noqa: E402
from src import pipeline as P  # noqa: E402
from src import worn_params as WP  # noqa: E402
from src.storage import get_storage  # noqa: E402


# ═══ Page setup ══════════════════════════════════════════════════════════

st.set_page_config(
    layout="wide",
    page_title="AI Bucherer · Auto Pipeline",
    page_icon="🎬",
    initial_sidebar_state="expanded",
)

# ─── Design system ────────────────────────────────────────────────────────
# Modeled after Linear / Vercel / Notion: limited color palette, 8-point grid,
# restrained typography hierarchy, subtle borders, accent-only highlights.
# All Streamlit chrome overridden so the app reads as one consistent surface,
# not "form-on-form-on-form".
st.markdown(
    """
    <style>
    :root {
      /* ── Color tokens ── */
      --bg-base: #0E0E10;
      --bg-elev-1: #16161A;
      --bg-elev-2: #1F1F25;
      --bg-elev-3: #2A2A33;

      --border-subtle: rgba(255,255,255,0.06);
      --border: rgba(255,255,255,0.10);
      --border-strong: rgba(255,255,255,0.16);

      --text: #E8E8EA;
      --text-2: #A0A0AB;
      --text-3: #6B6B73;
      --text-disabled: #4B4B53;

      --accent: #D4A857;
      --accent-hover: #E8B968;
      --accent-muted: rgba(212,168,87,0.14);
      --accent-ring: rgba(212,168,87,0.30);

      --success: #4ADE80; --success-bg: rgba(74,222,128,0.10); --success-border: rgba(74,222,128,0.20);
      --warning: #FBBF24; --warning-bg: rgba(251,191,36,0.10); --warning-border: rgba(251,191,36,0.24);
      --error:   #F87171; --error-bg:   rgba(248,113,113,0.10); --error-border:   rgba(248,113,113,0.22);
      --info:    #60A5FA; --info-bg:    rgba(96,165,250,0.10);  --info-border:    rgba(96,165,250,0.22);

      /* ── Spacing (4/8 grid) ── */
      --s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px;
      --s-5: 24px; --s-6: 32px; --s-7: 48px; --s-8: 64px;

      /* ── Radius ── */
      --r-sm: 6px; --r-md: 8px; --r-lg: 12px; --r-xl: 16px;

      /* ── Type ── */
      --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Inter", sans-serif;
      --mono: ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;

      /* ── Motion ── */
      --t-fast: 100ms ease;
      --t: 150ms ease;
    }

    /* ── Base ── */
    html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
      background: var(--bg-base) !important;
      color: var(--text);
      font-family: var(--font);
    }
    .block-container {
      padding-top: var(--s-5) !important;
      padding-bottom: var(--s-7) !important;
      padding-left: 2.5rem !important;
      padding-right: 2.5rem !important;
      max-width: 100% !important;   /* use the full width — the gallery is the focus */
    }

    /* ── Streamlit chrome cleanup ── */
    header[data-testid="stHeader"] { background: transparent; height: 0; }
    button[kind="header"], #MainMenu, footer, [data-testid="stToolbar"] { display: none !important; }
    [data-testid="stDecoration"] { display: none; }
    /* Strip the auto-generated header anchor links that produced "Link to heading" noise */
    [data-testid="stHeaderActionElements"] { display: none !important; }

    /* ── Type scale ── */
    h1, [data-testid="stHeading"] h1 {
      font-size: 28px !important; font-weight: 600 !important;
      line-height: 1.2 !important; letter-spacing: -0.01em !important;
      color: var(--text) !important;
      margin: 0 0 var(--s-2) 0 !important;
    }
    h2, [data-testid="stHeading"] h2 {
      font-size: 22px !important; font-weight: 600 !important;
      line-height: 1.3 !important; color: var(--text) !important;
      margin: var(--s-6) 0 var(--s-3) 0 !important;
    }
    h3, [data-testid="stHeading"] h3 {
      font-size: 16px !important; font-weight: 600 !important;
      line-height: 1.4 !important; color: var(--text) !important;
      margin: var(--s-5) 0 var(--s-2) 0 !important;
    }
    h4, h5, [data-testid="stHeading"] h4, [data-testid="stHeading"] h5 {
      font-size: 11px !important; font-weight: 600 !important;
      color: var(--text-3) !important;
      text-transform: uppercase !important; letter-spacing: 0.06em !important;
      margin: var(--s-4) 0 var(--s-2) 0 !important;
    }
    p, [data-testid="stMarkdownContainer"] p { color: var(--text); line-height: 1.55; font-size: 14px; }
    [data-testid="stCaptionContainer"], small {
      color: var(--text-3) !important; font-size: 12px !important;
    }

    /* ── Inline code: subtle, not bright green ── */
    code, [data-testid="stMarkdownContainer"] code, [data-testid="stCode"] {
      background: var(--bg-elev-2) !important;
      color: var(--text-2) !important;
      border: 1px solid var(--border-subtle);
      border-radius: 4px !important;
      padding: 1px 6px !important;
      font-size: 12px !important;
      font-family: var(--mono) !important;
    }

    /* ── Buttons ── */
    .stButton > button {
      background: transparent !important;
      color: var(--text) !important;
      border: 1px solid var(--border) !important;
      border-radius: var(--r-md) !important;
      font-weight: 500 !important;
      font-size: 13px !important;
      padding: 6px 14px !important;
      height: 34px !important;
      min-height: 34px !important;
      line-height: 1 !important;
      transition: background var(--t), border-color var(--t), color var(--t), transform var(--t-fast) !important;
      box-shadow: none !important;
    }
    /* (Board-controls overrides live further down, near the .board-card rules.) */
    .stButton > button:hover {
      background: var(--bg-elev-2) !important;
      border-color: var(--border-strong) !important;
    }
    .stButton > button:active { transform: translateY(1px); }
    .stButton > button:focus-visible {
      outline: 2px solid var(--accent) !important;
      outline-offset: 2px;
    }
    .stButton > button[kind="primary"] {
      background: var(--accent) !important;
      color: #1A1208 !important;
      border-color: var(--accent) !important;
      font-weight: 600 !important;
    }
    .stButton > button[kind="primary"]:hover {
      background: var(--accent-hover) !important;
      border-color: var(--accent-hover) !important;
    }
    .stButton > button:disabled {
      opacity: 0.35 !important; cursor: not-allowed;
      background: transparent !important;
    }

    /* ── Inputs ── */
    .stTextInput input, .stTextArea textarea,
    .stNumberInput input, [data-baseweb="input"] input,
    [data-baseweb="textarea"] textarea {
      background: var(--bg-elev-1) !important;
      border: 1px solid var(--border) !important;
      border-radius: var(--r-md) !important;
      color: var(--text) !important;
      font-size: 14px !important;
      transition: border-color var(--t), box-shadow var(--t);
    }
    .stTextInput input:focus, .stTextArea textarea:focus,
    .stNumberInput input:focus, [data-baseweb="input"] input:focus,
    [data-baseweb="textarea"] textarea:focus {
      border-color: var(--accent) !important;
      box-shadow: 0 0 0 3px var(--accent-ring) !important;
      outline: none !important;
    }
    .stTextInput input::placeholder, .stTextArea textarea::placeholder,
    [data-baseweb="input"] input::placeholder, [data-baseweb="textarea"] textarea::placeholder {
      color: var(--text-3) !important;
    }
    label, [data-testid="stWidgetLabel"] p {
      color: var(--text-2) !important;
      font-size: 13px !important;
      font-weight: 500 !important;
    }

    /* ── Selectbox ── */
    .stSelectbox [data-baseweb="select"] > div {
      background: var(--bg-elev-1) !important;
      border: 1px solid var(--border) !important;
      border-radius: var(--r-md) !important;
      color: var(--text) !important;
      min-height: 34px !important;
    }
    [data-baseweb="popover"] [role="listbox"] {
      background: var(--bg-elev-2) !important;
      border: 1px solid var(--border) !important;
      border-radius: var(--r-md) !important;
    }
    [data-baseweb="popover"] [role="option"]:hover {
      background: var(--bg-elev-3) !important;
    }

    /* ── Slider ── */
    [data-baseweb="slider"] [role="slider"] {
      background: var(--accent) !important;
      border: 2px solid var(--accent-hover) !important;
      box-shadow: 0 0 0 4px rgba(212,168,87,0.18) !important;
      height: 16px !important; width: 16px !important;
    }
    [data-baseweb="slider"] div[style*="background"] {
      background: var(--bg-elev-3) !important;
    }
    [data-baseweb="slider"] div[role="presentation"] > div:first-child {
      background: var(--accent) !important;
    }

    /* ── Radio (segmented) ── */
    [data-testid="stRadio"] label[role="radio"] {
      background: transparent !important;
      padding: 4px 0 !important;
    }

    /* ── File uploader ── */
    [data-testid="stFileUploaderDropzone"], [data-testid="stFileUploader"] section {
      background: var(--bg-elev-1) !important;
      border: 1px dashed var(--border) !important;
      border-radius: var(--r-md) !important;
      transition: border-color var(--t), background var(--t);
    }
    [data-testid="stFileUploaderDropzone"]:hover {
      border-color: var(--accent) !important;
      background: var(--bg-elev-2) !important;
    }

    /* ── Sidebar ── */
    [data-testid="stSidebar"] {
      background: var(--bg-elev-1) !important;
      border-right: 1px solid var(--border-subtle) !important;
    }
    [data-testid="stSidebar"] .block-container {
      padding: var(--s-5) var(--s-4) var(--s-6) var(--s-4) !important;
    }
    [data-testid="stSidebar"] hr {
      border-color: var(--border-subtle) !important;
      margin: var(--s-4) 0 !important;
    }
    /* Sidebar's nested containers shouldn't have card backgrounds */
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] > div {
      background: transparent !important;
    }

    /* ── Bordered containers (st.container(border=True)) ── */
    [data-testid="stVerticalBlockBorderWrapper"] {
      border: 1px solid var(--border-subtle) !important;
      background: var(--bg-elev-1) !important;
      border-radius: var(--r-lg) !important;
      padding: var(--s-4) !important;
      transition: border-color var(--t), background var(--t);
    }
    [data-testid="stVerticalBlockBorderWrapper"]:hover {
      border-color: var(--border) !important;
    }

    /* ── Expander ── */
    [data-testid="stExpander"] {
      border: 1px solid var(--border-subtle) !important;
      border-radius: var(--r-md) !important;
      background: var(--bg-elev-1) !important;
    }
    [data-testid="stExpander"] summary {
      font-size: 13px !important; color: var(--text-2) !important;
      padding: var(--s-3) var(--s-4) !important;
    }
    [data-testid="stExpander"] summary:hover { color: var(--text) !important; }

    /* ── Metric (compact) ── */
    [data-testid="stMetric"] {
      background: transparent !important; padding: 0 !important;
    }
    [data-testid="stMetricLabel"] p, [data-testid="stMetricLabel"] {
      color: var(--text-3) !important;
      font-size: 11px !important;
      text-transform: uppercase !important;
      letter-spacing: 0.06em !important;
      font-weight: 600 !important;
    }
    [data-testid="stMetricValue"] {
      color: var(--text) !important;
      font-size: 22px !important;
      font-weight: 600 !important;
      line-height: 1.2 !important;
    }

    /* ── Progress bar ── */
    [data-testid="stProgress"] > div > div > div > div {
      background: var(--accent) !important;
    }
    [data-testid="stProgress"] > div > div > div {
      background: var(--bg-elev-3) !important;
      border-radius: 999px !important;
    }

    /* ── Alerts ── */
    [data-testid="stAlert"] {
      border-radius: var(--r-md) !important;
      border: 1px solid var(--border) !important;
      padding: var(--s-3) var(--s-4) !important;
    }
    [data-baseweb="notification"][kind="info"]    { background: var(--info-bg) !important;    border-color: var(--info-border) !important;    color: var(--info) !important; }
    [data-baseweb="notification"][kind="warning"] { background: var(--warning-bg) !important; border-color: var(--warning-border) !important; color: var(--warning) !important; }
    [data-baseweb="notification"][kind="error"]   { background: var(--error-bg) !important;   border-color: var(--error-border) !important;   color: var(--error) !important; }
    [data-baseweb="notification"][kind="success"] { background: var(--success-bg) !important; border-color: var(--success-border) !important; color: var(--success) !important; }

    /* ── Dialog (lightbox / compare) ── */
    div[role="dialog"], div[data-testid="stDialog"] {
      background: var(--bg-elev-1) !important;
      border: 1px solid var(--border) !important;
      border-radius: var(--r-xl) !important;
      max-width: 96vw !important;
      width: 96vw !important;
    }
    div[role="dialog"] [data-testid="stImage"] img,
    div[data-testid="stDialog"] [data-testid="stImage"] img {
      max-height: 82vh !important; height: auto !important;
      width: auto !important; max-width: 100% !important;
      object-fit: contain; margin: 0 auto; display: block;
      border-radius: var(--r-md);
    }

    /* ── Image rounding everywhere ── */
    [data-testid="stImage"] img {
      border-radius: var(--r-md);
    }

    /* ── Toast ── */
    [data-testid="stToast"] {
      background: var(--bg-elev-2) !important;
      color: var(--text) !important;
      border: 1px solid var(--border) !important;
      border-radius: var(--r-md) !important;
    }

    /* ── Scrollbar ── */
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: var(--bg-base); }
    ::-webkit-scrollbar-thumb { background: var(--bg-elev-2); border-radius: 5px; border: 2px solid var(--bg-base); }
    ::-webkit-scrollbar-thumb:hover { background: var(--bg-elev-3); }

    /* ════════════════════════════════════════════════════════════════════ */
    /* App-specific component styles                                        */
    /* ════════════════════════════════════════════════════════════════════ */

    /* ── Badges ── */
    .badge {
      display: inline-flex;
      align-items: center;
      height: 22px;
      padding: 0 8px;
      font-size: 11px;
      font-weight: 500;
      border-radius: 999px;
      background: var(--bg-elev-2);
      color: var(--text-2);
      border: 1px solid var(--border-subtle);
      letter-spacing: 0.01em;
      vertical-align: middle;
      margin-right: 6px;
      white-space: nowrap;
      line-height: 1;
    }
    .badge-worn { background: var(--info-bg); color: var(--info); border-color: var(--info-border); }
    .badge-packshot { background: var(--accent-muted); color: var(--accent); border-color: rgba(212,168,87,0.28); }
    .badge-ready { background: var(--success-bg); color: var(--success); border-color: var(--success-border); }
    .badge-error { background: var(--error-bg); color: var(--error); border-color: var(--error-border); font-weight: 600; }
    .badge-running {
      background: var(--warning-bg); color: var(--warning); border-color: var(--warning-border); font-weight: 600;
    }
    /* Pulse for in-flight badges */
    @keyframes pulse-dot { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
    .badge-running::before {
      content: ""; display: inline-block;
      width: 6px; height: 6px; border-radius: 50%;
      background: currentColor; margin-right: 6px;
      animation: pulse-dot 1.4s ease-in-out infinite;
    }

    /* ── Project tiles (landing) ── */
    .project-tile {
      border: 1px solid var(--border-subtle);
      border-radius: var(--r-lg);
      padding: var(--s-4) var(--s-5);
      background: var(--bg-elev-1);
      transition: background var(--t), border-color var(--t);
    }
    .project-tile:hover {
      background: var(--bg-elev-2);
      border-color: var(--border);
    }

    /* ── Board card toolbar — borderless icon buttons unified with the chip ──
       Visual goal: a single clean toolbar row reading as one element,
       not four boxy outlined buttons. Inspired by the Gemini variant card
       (subtle icons + inline count, minimal chrome).
       Buttons are borderless transparent, hover only adds a soft fill. ── */
    /* Streamlit renders st.container(key="boardctrls-<id>") with a real wrapping
       div whose class contains "st-key-boardctrls-<id>". We target that — a bare
       <div class="board-controls"> emitted via st.markdown does NOT wrap the
       sibling button columns, so its descendant selectors never match. */
    /* Use a descendant combinator (.stButton button, not > button): buttons with
       a help tooltip are nested under an extra stTooltipHoverTarget span, so the
       direct-child selector misses them. */
    div[class*="st-key-boardctrls"] { margin: 8px 0 4px 0; }
    div[class*="st-key-boardctrls"] .stButton button {
      padding: 0 8px !important;
      font-size: 14px !important;
      height: 30px !important;
      min-height: 30px !important;
      border-radius: var(--r-sm) !important;
      color: var(--text-2) !important;
      background: transparent !important;
      border: 1px solid transparent !important;
      box-shadow: none !important;
    }
    div[class*="st-key-boardctrls"] .stButton button:hover {
      color: var(--text) !important;
      background: var(--bg-elev-2) !important;
      border-color: var(--border-subtle) !important;
    }
    div[class*="st-key-boardctrls"] .stButton button:disabled {
      color: var(--text-disabled) !important;
      background: transparent !important;
      border-color: transparent !important;
      opacity: 0.3 !important;
    }
    div[class*="st-key-boardctrls"] .stButton button:focus-visible {
      outline: 2px solid var(--accent) !important;
      outline-offset: -2px;
    }

    /* ── Square board thumbnails — each board card's image is wrapped in
       st.container(key="boardimg-<id>"), which Streamlit renders with a real
       "st-key-boardimg-*" class. Force the <img> to a 1:1 crop so the 4-column
       grid stays tidy regardless of source aspect ratio. */
    div[class*="st-key-boardimg"] img {
      aspect-ratio: 1 / 1 !important;
      object-fit: cover !important;
      width: 100% !important;
      display: block;
      border-radius: var(--r-md);
    }
    /* Exempt zero-size iframes (autorefresh) — never force aspect-ratio on those */
    iframe[width="0"], iframe[height="0"] {
      aspect-ratio: auto !important;
      height: 0 !important;
      width: 0 !important;
    }
    /* Variant indicator chip "2 / 4" — the focal control. Slightly larger
       than icon buttons so it visually anchors the toolbar. */
    .var-chip {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 30px;
      min-width: 64px;
      padding: 0 14px;
      border-radius: 999px;
      background: var(--bg-elev-2);
      color: var(--text);
      font-size: 13px;
      font-weight: 500;
      border: 1px solid var(--border-subtle);
      font-variant-numeric: tabular-nums;
      letter-spacing: 0.01em;
    }
    .var-chip-picked {
      background: var(--success-bg);
      color: var(--success);
      border-color: var(--success-border);
    }
    .var-chip-empty {
      background: transparent;
      color: var(--text-3);
      border-color: var(--border-subtle);
      font-style: italic;
    }
    .var-chip-running {
      background: var(--warning-bg);
      color: var(--warning);
      border-color: var(--warning-border);
    }

    /* ── Tiny utility classes ── */
    .row { display: flex; align-items: center; gap: var(--s-2); }
    .row-wrap { display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap; }
    .muted { color: var(--text-2); }
    .subtle { color: var(--text-3); }
    .nowrap { white-space: nowrap; }
    .stack-1 > * + * { margin-top: var(--s-1) !important; }
    .stack-2 > * + * { margin-top: var(--s-2) !important; }

    /* ── Hide horizontal rules in main content (use spacing instead) ── */
    [data-testid="stAppViewContainer"] hr {
      border: 0;
      border-top: 1px solid var(--border-subtle);
      margin: var(--s-5) 0;
    }

    /* ── Clickable preview images ──
       thumb() wraps each preview in st.container(key="clk-<id>") and places a
       button directly after the image. We absolutely-position that button to
       cover the thumbnail so a click anywhere on the image opens the full-res
       zoom lightbox. The button itself is invisible (transparent text/bg) and
       shows a zoom-in cursor — no more separate "🔍 Full size" control. */
    div[class*="st-key-clk-"] { position: relative; }
    div[class*="st-key-clk-"] [data-testid="stImage"] { cursor: zoom-in; }
    div[class*="st-key-clk-"] .stButton {
      position: absolute; inset: 0; margin: 0; z-index: 6; height: 100%;
    }
    div[class*="st-key-clk-"] .stButton button {
      width: 100%; height: 100%;
      min-height: 0 !important;
      background: transparent !important;
      border: none !important;
      box-shadow: none !important;
      color: transparent !important;
      font-size: 0 !important;        /* hide the label glyph (emoji ignore color) */
      cursor: zoom-in;
      padding: 0 !important;
      transition: background 120ms ease;
    }
    div[class*="st-key-clk-"] .stButton button:hover {
      background: rgba(255,255,255,0.06) !important;
    }
    div[class*="st-key-clk-"] .stButton button:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: -2px;
    }

    /* ── Detail option viewer: cap the image so the WHOLE option fits on screen.
       The image is wrapped in st.container(key="optview-<id>"); fit it to ~72vh
       tall (centered), preserving aspect, instead of filling the full width. */
    div[class*="st-key-optview-"] img {
      max-height: 72vh !important;
      max-width: 100% !important;
      width: auto !important;
      height: auto !important;
      margin: 0 !important;            /* left-anchored — stays put when toggling compare */
      display: block;
      object-fit: contain;
      border-radius: var(--r-md);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ═══ Config + clients ════════════════════════════════════════════════════

cfg = load_config()
ST = get_storage(cfg)


# ── Storage bridges for the UI ───────────────────────────────────────────
# Paths in the manifest fall into two classes:
#   • Storage paths   — source renders, graded outputs, heroes (local disk OR Dropbox)
#   • Local scratch   — Gemini variants (always a real local temp file, even on the
#                       Dropbox backend; see pipeline._workspace_base)
# These helpers resolve either kind to something PIL / st.image can open, and answer
# existence, without the caller needing to know which backend or class a path is.

def _local(path) -> Path:
    """Return a real local file for `path`. Local scratch files are returned as-is;
    Storage paths are materialized (downloaded + cached for the Dropbox backend)."""
    p = str(path)
    if Path(p).exists():
        return Path(p)
    try:
        return Path(ST.materialize(p))
    except Exception:
        return Path(p)


def _path_exists(path) -> bool:
    """Existence check that works for both local scratch and Storage paths.

    NOTE: on the Dropbox backend this is a `files_get_metadata` network round-trip.
    Do NOT call it per-card in the board grid — trust the manifest there (a path
    listed in photo.variants/output_path exists; ingest reconciles dangling ones)
    and use `_exists_cached` for low-churn paths that are probed every rerun."""
    p = str(path)
    if Path(p).exists():
        return True
    try:
        return ST.exists(p)
    except Exception:
        return False


@st.cache_data(show_spinner=False, ttl=120, max_entries=2048)
def _cached_exists(path_str: str) -> bool:
    try:
        return ST.exists(path_str)
    except Exception:
        return False


def _exists_cached(path) -> bool:
    """Existence check with a short cache, for low-churn paths probed on every
    rerun (hero / product-reference images). A local file short-circuits with no
    network; for a Dropbox path the result is memoized so autorefresh ticks don't
    re-hit the API. A just-set hero/ref probes True on its first access (the file
    is written before the rerun that displays it), so there is no stale-absent
    window for content the user just created; the 120s TTL eventually reflects an
    out-of-band deletion."""
    p = str(path)
    if Path(p).exists():
        return True
    return _cached_exists(p)


def _prefetch_materialize(paths) -> None:
    """Warm the Storage download cache for many paths in parallel, so the board's
    first paint fetches images concurrently instead of one-by-one. No-op for the
    local backend, and cheap once warm (each call is then just a local stat that
    returns the cached file)."""
    if getattr(ST, "backend", "local") != "dropbox":
        return
    uniq = list(dict.fromkeys(str(p) for p in paths if p))
    if not uniq:
        return

    def _one(p: str) -> None:
        try:
            if not Path(p).exists():
                ST.materialize(p)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_one, uniq))


# NOTE: the api_key arg is part of the cache key (no leading underscore), so a
# changed secret rebuilds the client automatically — no app reboot needed.
@st.cache_resource(show_spinner=False)
def get_anthropic_client(api_key: str):
    return AnthropicClient(cfg)


@st.cache_resource(show_spinner=False)
def get_gemini_client(api_key: str):
    return GeminiClient(cfg)


# ═══ Route helpers ═══════════════════════════════════════════════════════

def set_route(route: str, **extras):
    st.session_state["route"] = route
    for k, v in extras.items():
        st.session_state[k] = v


def current_route() -> str:
    return st.session_state.get("route", "landing")


# ═══ Access gate (shared password for the public deploy) ═════════════════

def _auth_token() -> str:
    """Unforgeable token for the auth cookie: HMAC of the shared password. The cookie
    can't be faked without the password, and we store no secret in it."""
    import hashlib
    import hmac as _hmac
    return _hmac.new(cfg.app_password.encode("utf-8"), b"tnf-authed", hashlib.sha256).hexdigest()


def _set_auth_cookie(token: str) -> None:
    """Persist the auth token as a 30-day cookie via the parent document (the
    components iframe is same-origin, so window.parent.document.cookie reaches the
    real page). Read back on later loads through st.context.cookies."""
    from streamlit.components.v1 import html as _html
    _html(
        f"<script>try{{window.parent.document.cookie="
        f"'tnf_auth={token}; max-age=2592000; path=/; SameSite=Lax';}}catch(e){{}}</script>",
        height=0,
    )


def require_password() -> None:
    """Gate the whole app behind a shared password when APP_PASSWORD is configured.

    Auth persists across refreshes via a signed cookie (HMAC of the password), read
    natively through st.context.cookies — so a refresh does not bounce the user back
    to the password page. No password set (local dev) → no gate.
    """
    if not cfg.app_password:
        return

    token = _auth_token()

    if st.session_state.get("_authed"):
        # Just logged in this session → persist the cookie once (on a clean run, so the
        # injected JS actually flushes to the client) for future refreshes.
        if st.session_state.pop("__set_auth_cookie", False):
            _set_auth_cookie(token)
        return

    # Returning visitor: a valid cookie skips the prompt entirely (no flash — the
    # cookie is on the request, read synchronously).
    try:
        if st.context.cookies.get("tnf_auth") == token:
            st.session_state["_authed"] = True
            return
    except Exception:
        pass

    import hmac
    _l, mid, _r = st.columns([1, 1.4, 1])
    with mid:
        st.markdown("<div style='height:8vh'></div>", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size:22px;font-weight:600;'>AI Bucherer</div>"
            "<div class='subtle' style='font-size:13px;margin-bottom:16px;'>"
            "Enter the access password to continue.</div>",
            unsafe_allow_html=True,
        )
        with st.form("login", clear_on_submit=False):
            pw = st.text_input("Password", type="password", label_visibility="collapsed",
                               placeholder="Password")
            ok = st.form_submit_button("Unlock", type="primary", use_container_width=True)
        if ok:
            if hmac.compare_digest(pw, cfg.app_password):
                st.session_state["_authed"] = True
                st.session_state["__set_auth_cookie"] = True   # write cookie on next (clean) run
                st.rerun()
            else:
                st.error("Incorrect password.")
    st.stop()


# ═══ Helpers (dialogs, thumbnails, compliance) ═══════════════════════════

def _cb_fs_step(delta: int) -> None:
    """Move the fullscreen lightbox to the next/previous image (wraps around)."""
    imgs = st.session_state.get("fs_images", [])
    if not imgs:
        return
    cur = st.session_state.get("fs_index", 0)
    st.session_state["fs_index"] = (cur + delta) % len(imgs)


def _inject_arrow_key_nav() -> None:
    """Bridge ←/→ keypresses to the lightbox's ◀/▶ buttons.

    The component runs in a same-origin srcdoc iframe, so it can reach the parent
    Streamlit document and click the nav buttons by their glyph. We keep a single
    handler on the parent (removing any prior one) so reruns don't stack listeners.
    If the parent is unreachable (strict CSP), the on-screen ◀/▶ buttons still work.
    """
    from streamlit.components.v1 import html as st_html
    st_html(
        """
        <script>
        (function(){
          try {
            const doc = window.parent.document;
            // Returns true only if a matching nav button exists (i.e. the lightbox
            // is open). We preventDefault ONLY then, so once the dialog closes the
            // lingering handler stops swallowing arrow keys app-wide.
            function clickGlyph(g){
              const bs = doc.querySelectorAll('button');
              for (const b of bs){ if ((b.innerText||'').trim() === g){ b.click(); return true; } }
              return false;
            }
            if (window.parent.__fsKeyHandler){
              doc.removeEventListener('keydown', window.parent.__fsKeyHandler);
            }
            const h = function(e){
              const t = e.target;
              if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
              if (e.key === 'ArrowLeft'){ if (clickGlyph('\\u25C0')) e.preventDefault(); }
              else if (e.key === 'ArrowRight'){ if (clickGlyph('\\u25B6')) e.preventDefault(); }
            };
            window.parent.__fsKeyHandler = h;
            doc.addEventListener('keydown', h);
          } catch (err) { /* cross-origin / CSP — buttons still work */ }
        })();
        </script>
        """,
        height=0,
    )


@st.dialog("Full-size view", width="large")
def _show_fullscreen():
    """Lightbox over `st.session_state['fs_images']` (list of (path, caption)) at index
    `fs_index`. With more than one image, ◀/▶ buttons and the ←/→ arrow keys navigate."""
    imgs = st.session_state.get("fs_images", [])
    if not imgs:
        st.error("No image to show.")
        return
    idx = max(0, min(st.session_state.get("fs_index", 0), len(imgs) - 1))
    image_path, caption = imgs[idx]

    # Read native dimensions (cheap header read) and build a high-res, cached JPEG
    # data URL. max_edge=4096 leaves the 2K source renders at native resolution —
    # a true full-res view — while staying far lighter than a PNG re-encode.
    try:
        with Image.open(_local(image_path)) as _im:
            w, h = _im.size
    except Exception:
        st.error(f"File not found: {image_path}")
        return

    if len(imgs) > 1:
        cprev, cmid, cnext = st.columns([1, 4, 1])
        with cprev:
            st.button("◀", key="fs_prev", use_container_width=True,
                      help="Previous (←)", on_click=_cb_fs_step, args=(-1,))
        with cmid:
            st.markdown(
                f"<div style='text-align:center;font-weight:600;'>"
                f"{caption or Path(image_path).name}"
                f"<span style='color:var(--text-3);font-weight:400;'> · {idx + 1} / {len(imgs)}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with cnext:
            st.button("▶", key="fs_next", use_container_width=True,
                      help="Next (→)", on_click=_cb_fs_step, args=(1,))
    else:
        st.markdown(f"**{caption or Path(image_path).name}**")

    from streamlit.components.v1 import html as st_html
    url = _image_to_data_url(image_path, max_edge=4096, quality=88)
    st_html(render_zoom_image_html(url, w, h, height_px=520), height=536, scrolling=False)

    if len(imgs) > 1:
        _inject_arrow_key_nav()


def _open_fullscreen(images, index: int = 0, title: str = "") -> None:
    """Seed the lightbox session state and open it. `images` is a list of either
    plain paths or (path, caption) tuples."""
    norm = []
    for it in images:
        if isinstance(it, (tuple, list)):
            norm.append((str(it[0]), it[1] if len(it) > 1 else ""))
        else:
            norm.append((str(it), ""))
    st.session_state["fs_images"] = norm
    st.session_state["fs_index"] = max(0, min(index, len(norm) - 1)) if norm else 0
    _show_fullscreen()


@st.dialog("Before / After", width="large")
def _show_comparison(
    before_path: str,
    after_path: str,
    before_label: str = "Before",
    after_label: str = "After",
    title: str = "",
):
    """Fast hover-position compare slider in a dialog.

    Replaces streamlit-image-comparison (which re-encodes both images as PNG and
    pushes a multi-MB payload to the component, taking 3–8s on 2K images). Ours
    reuses the same cached data-URL pipeline as the board, so opening this dialog
    is near-instant — the cached JPEGs are already in memory.
    """
    if not _exists_cached(before_path) or not _exists_cached(after_path):
        st.error(f"Missing image(s):\nbefore: {before_path}\nafter: {after_path}")
        return
    if title:
        st.markdown(f"**{title}**")

    from streamlit.components.v1 import html as st_html
    # max_edge=900 matches the board's compare-slider size and keeps the encode/
    # payload light; full-res 2K is overkill in a 560px-tall modal.
    # render_hover_card_html materializes Storage paths internally.
    html_payload = render_hover_card_html(
        str(before_path), str(after_path), height_px=560, max_edge=900,
    )
    st_html(html_payload, height=576, scrolling=False)

    # Footer caption — pull real dimensions from disk header only (cheap)
    try:
        with Image.open(_local(before_path)) as ib, Image.open(_local(after_path)) as ia:
            bw, bh = ib.size
            aw, ah = ia.size
        st.caption(
            f"**{before_label}** {bw}×{bh}px · "
            f"**{after_label}** {aw}×{ah}px · "
            f"move mouse left↔right to compare"
        )
    except Exception:
        st.caption(f"**{before_label}** ↔ **{after_label}** · move mouse left↔right to compare")


_PACKSHOT_REQUIRED_SECTIONS = (
    "PRIMARY DIRECTIVE", "PRESERVE FROM", "METAL", "BACKGROUND",
    "LIGHTING", "CAMERA", "STYLE",
)
_PACKSHOT_WORD_TARGET = (550, 700)
_WORN_REQUIRED_SECTIONS = (
    "PRIMARY DIRECTIVE", "PRESERVE FROM", "JEWELRY", "GENERATE FRESH",
    "SKIN", "JAW AND CHEEK", "NECK AND THROAT", "DÉCOLLETAGE",
    "Lips", "Hair", "Ears", "Clothing", "Background", "Lighting", "Style",
)
_WORN_WORD_TARGET = (900, 1100)


def _render_prompt_compliance(photo: "M.PhotoState") -> None:
    prompt = photo.prompt or ""
    words = len(prompt.split())
    is_worn = photo.classification == "worn"
    lo, hi = _WORN_WORD_TARGET if is_worn else _PACKSHOT_WORD_TARGET
    required = _WORN_REQUIRED_SECTIONS if is_worn else _PACKSHOT_REQUIRED_SECTIONS
    present = sum(1 for s in required if s in prompt)
    word_ok = lo <= words <= hi
    section_ok = present == len(required)
    word_glyph = "✓" if word_ok else ("↑" if words > hi else "↓")
    sec_glyph = "✓" if section_ok else "⚠"
    word_color = "#1a7f37" if word_ok else ("#9a6700" if abs(words - (lo + hi) / 2) < (hi - lo) else "#cf222e")
    sec_color = "#1a7f37" if section_ok else "#cf222e"
    st.markdown(
        f"<span style='font-size:0.85em;color:#666'>"
        f"<span style='color:{word_color};font-weight:600'>{word_glyph} {words} words</span> "
        f"(target {lo}–{hi}) · "
        f"<span style='color:{sec_color};font-weight:600'>{sec_glyph} {present}/{len(required)} sections</span>"
        f"</span>",
        unsafe_allow_html=True,
    )


def _sync_card_widgets_if_stale(photo: "M.PhotoState") -> None:
    """Push manifest changes into widget session_state BEFORE keyed widgets render."""
    prompt_key = f"prompt_{photo.photo_id}"
    prompt_marker = f"__sync_prompt_{photo.photo_id}"
    if st.session_state.get(prompt_marker) != photo.prompt:
        st.session_state[prompt_key] = photo.prompt or ""
        st.session_state[prompt_marker] = photo.prompt

    class_key = f"class_{photo.photo_id}"
    class_marker = f"__sync_class_{photo.photo_id}"
    if st.session_state.get(class_marker) != photo.classification:
        st.session_state[class_key] = photo.classification or "packshot"
        st.session_state[class_marker] = photo.classification


def thumb(
    image_path: str | Path,
    width: int = 220,
    caption: str | None = None,
    fs_key: str | None = None,
    fs_caption: str | None = None,
    fs_group: list | None = None,
    fs_index: int = 0,
):
    """Clickable preview thumbnail. When `fs_key` is given, the image itself is
    clickable (an invisible overlay button, styled by the `.st-key-clk-*` CSS) and
    opens the full-resolution zoom lightbox — there is no separate "Full size"
    button. When `fs_group` (a list of paths or (path, caption) tuples) is supplied,
    the lightbox opens on that group at `fs_index`, so ◀/▶ and the arrow keys flip
    through the set; otherwise it opens just this image.

    Display uses the cached downscaled JPEG (small, decoded/encoded once) rather
    than re-shipping the full-res 2K image on every rerun — the full-res bytes are
    only fetched when the user actually opens the lightbox."""
    name = Path(str(image_path)).name
    disp = _board_thumb(image_path, max_edge=760)
    if not isinstance(disp, bytes):  # encode failed → file missing/corrupt
        st.caption(f"_(missing: {name})_")
        return

    if not fs_key:
        st.image(disp, width=width, caption=caption)
        return

    with st.container(key=f"clk-{fs_key}"):
        st.image(disp, width=width, caption=caption)
        if st.button("🔍 Open full resolution", key=fs_key, use_container_width=True,
                     help="Click the image to view full resolution · scroll to zoom, "
                          "drag to pan, double-click to reset"):
            if fs_group:
                _open_fullscreen(fs_group, fs_index)
            else:
                _open_fullscreen([(str(image_path), fs_caption or caption or name)], 0)


# ═══ Hover-slider component (board cards) ════════════════════════════════

# Performance-critical: the board re-renders every autorefresh tick (1.5s while any
# background task is running). Without caching, each tick re-decodes + JPEG-encodes
# every visible card's images, which freezes the UI on a 20-photo project.
# We cache by (path, mtime, max_edge) so file edits invalidate naturally.

@st.cache_data(show_spinner=False, max_entries=512)
def _cached_data_url(path_str: str, mtime: float, max_edge: int, quality: int = 82) -> str:
    """Cached version of _image_to_data_url. mtime is the cache key — when the file is
    rewritten (e.g. after grading), the cache entry is invalidated automatically.
    Returns a base64 data URL of the (optionally downscaled) JPEG. A large max_edge
    leaves the image at native resolution — used for the full-res zoom lightbox."""
    try:
        img = Image.open(path_str).convert("RGB")
        if max(img.size) > max_edge:
            scale = max_edge / max(img.size)
            img = img.resize(
                (int(img.size[0] * scale), int(img.size[1] * scale)),
                Image.LANCZOS,
            )
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYGD4DwABBQEAU"
            "h0bygAAAABJRU5ErkJggg=="
        )


def _image_to_data_url(path, max_edge: int = 900, quality: int = 82) -> str:
    """Public wrapper — materializes the path to a local file (no-op for local scratch /
    local backend), then keys the cache on mtime so it invalidates on file rewrite."""
    local = _local(path)
    try:
        mtime = local.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _cached_data_url(str(local), mtime, max_edge, quality)


def _bytes_to_data_url(data: bytes, max_edge: int = 1100, quality: int = 82) -> str:
    """Downscaled JPEG data URL from raw bytes (e.g. a live-graded preview) — for the
    compare slider's overlay when there's no file on disk."""
    try:
        img = Image.open(BytesIO(data)).convert("RGB")
        if max(img.size) > max_edge:
            s = max_edge / max(img.size)
            img = img.resize((int(img.size[0] * s), int(img.size[1] * s)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


@st.cache_data(show_spinner=False, max_entries=512)
def _cached_thumb_bytes(path_str: str, mtime: float, max_edge: int) -> bytes:
    """Downscaled JPEG bytes, cached by (path, mtime, max_edge). Used by the board
    grid so each card ships a small thumbnail instead of a full-res 2K image —
    decoded/encoded once, reused across every rerun (and every autorefresh tick)."""
    try:
        img = Image.open(path_str).convert("RGB")
        if max(img.size) > max_edge:
            scale = max_edge / max(img.size)
            img = img.resize(
                (int(img.size[0] * scale), int(img.size[1] * scale)), Image.LANCZOS
            )
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception:
        return b""


def _encode_thumb(raw: bytes, max_edge: int = 512) -> bytes:
    """Downscale raw image bytes to a small JPEG. Pure CPU, no I/O."""
    img = Image.open(BytesIO(raw)).convert("RGB")
    if max(img.size) > max_edge:
        s = max_edge / max(img.size)
        img = img.resize((int(img.size[0] * s), int(img.size[1] * s)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue()


def _board_thumb(path, max_edge: int = 500):
    """Small JPEG bytes for st.image. Resolves, in order: (1) bytes already produced
    by `_prefetch_thumbs` this session, (2) the persisted `.thumbs` file (warmed by
    prefetch), (3) a last-resort full-res downscale for display. It NEVER uploads or
    does network I/O — all fetching/encoding/uploading happens off the render thread
    in `_prefetch_thumbs`, so the board's render loop stays fast and non-blocking."""
    mf = st.session_state.get("manifest")
    if mf is not None:
        src = str(path)
        tb = st.session_state.get("__thumb_bytes", {})
        if src in tb:
            return tb[src]
        tp = P.thumb_storage_path(ST, mf.output_root, src)
        if tp in st.session_state.get("__thumbs_present", set()) or Path(tp).exists():
            try:
                return _local(tp).read_bytes()
            except Exception:
                pass
    # Last resort (rare): downscale whatever is locally materialized. No upload.
    local = _local(path)
    try:
        mtime = local.stat().st_mtime
    except OSError:
        mtime = 0.0
    data = _cached_thumb_bytes(str(local), mtime, max_edge)
    return data if data else str(local)


def _prefetch_thumbs(display_paths) -> None:
    """Prepare every board tile's thumbnail in ONE parallel batch, off the render
    thread. Learns which thumbs already exist with a single `.thumbs` listing (not N
    per-tile probes); warms those in parallel; for missing ones, downloads the
    full-res image, encodes a thumb, uploads it for next time, and caches the bytes
    in-session — all 8-way parallel. After this, the render loop only reads tiny
    bytes from memory."""
    mf = st.session_state.get("manifest")
    if mf is None:
        return
    present = st.session_state.setdefault("__thumbs_present", set())
    tbytes = st.session_state.setdefault("__thumb_bytes", {})
    out_root = mf.output_root
    pairs = [(str(p), P.thumb_storage_path(ST, out_root, str(p)))
             for p in display_paths if p]
    # Nothing to do if everything is already resolved this session.
    if all((src in tbytes or tp in present) for src, tp in pairs):
        return

    is_dbx = getattr(ST, "backend", "local") == "dropbox"
    if is_dbx:
        # One request fetches the whole pack (all pre-built thumbs); fall back to a
        # folder listing for anything not yet packed (e.g. freshly generated).
        pack = P.read_thumb_pack(ST, out_root)
        if pack:
            for src, tp in pairs:
                if src not in tbytes and ST.name(tp) in pack:
                    tbytes[src] = pack[ST.name(tp)]
        try:
            existing = {ST.name(f) for f in ST.list_files(ST.join(out_root, ".thumbs"))} if any(
                src not in tbytes for src, _ in pairs) else set()
        except Exception:
            existing = set()
        have = lambda tp: ST.name(tp) in existing
    else:
        have = lambda tp: Path(tp).exists()

    todo = [(src, tp) for src, tp in pairs if not (src in tbytes or tp in present)]
    warm = [(src, tp) for src, tp in todo if have(tp)]
    make = [(src, tp) for src, tp in todo if not have(tp)]

    def _warm(args):
        src, tp = args
        try:
            ST.materialize(tp)      # tiny download into the local cache
            return ("present", tp, None)
        except Exception:
            return (None, tp, None)

    def _make(args):
        src, tp = args
        try:
            data = _encode_thumb(ST.read_bytes(src))   # download full-res + downscale
            ST.write_bytes(tp, data)                   # persist for next time
            return ("bytes", src, data)
        except Exception:
            return (None, src, None)

    # Tiny thumb fetches are latency-bound, so over-subscribe the pool a bit.
    with ThreadPoolExecutor(max_workers=12) as ex:
        for kind, key, data in list(ex.map(_warm, warm)) + list(ex.map(_make, make)):
            if kind == "present":
                present.add(key)
            elif kind == "bytes":
                tbytes[key] = data


def _build_all_thumbs(manifest) -> int:
    """One-shot: persist a thumbnail for every current variant + graded output that
    doesn't have one yet. Parallelized, idempotent. Returns the count built."""
    targets: list[str] = []
    for p in manifest.photos.values():
        targets.extend(p.variants or [])
        if p.graded:
            targets.append(p.output_path)
    targets = list(dict.fromkeys(str(t) for t in targets if t))
    present = st.session_state.setdefault("__thumbs_present", set())

    def _one(src: str) -> int:
        tp = P.thumb_storage_path(ST, manifest.output_root, src)
        try:
            if _exists_cached(tp):
                present.add(tp)
                return 0
            tp2 = P.write_thumb(ST, manifest.output_root, src, ST.read_bytes(src))
            if tp2:
                present.add(tp2)
                return 1
        except Exception:
            return 0
        return 0

    built = 0
    if targets:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for n in ex.map(_one, targets):
                built += n
    # Bundle all thumbs into one pack so the next cold open is a single request.
    try:
        P.write_thumb_pack(ST, manifest.output_root)
    except Exception:
        pass
    return built


def _folder_images(folder: str) -> list[str]:
    """Image paths (Storage paths) directly inside `folder`, sorted. Works on both
    the local and Dropbox backends."""
    if not ST.exists(folder):
        return []
    return [
        f for f in ST.list_files(folder)
        if Path(ST.name(f)).suffix.lower() in PROJ.IMAGE_EXTS
    ]


def _render_wizard_thumb_strip(folder: str, total_images: int) -> None:
    """Inline horizontal thumbnails for the project-creation wizard so the user
    can actually see what they're describing. Pure HTML — uses the cached data-URL
    pipeline, so each thumbnail is computed once and reused across all reruns.

    Renders up to 4 thumbs at ~108px square. If the folder has more images, a
    small caption flags the count. A separate "🔍 View all" button sits below the
    strip so the user can pop the full set into the lightbox for closer inspection.
    """
    all_imgs = _folder_images(folder)
    if not all_imgs:
        st.caption("_(no images)_")
        return

    parts = ['<div style="display:flex;gap:6px;margin:6px 0 6px 0;flex-wrap:wrap;">']
    for p in all_imgs[:4]:
        url = _image_to_data_url(p, max_edge=240)
        parts.append(
            f'<img src="{url}" '
            f'style="width:108px;height:108px;object-fit:cover;border-radius:4px;'
            f'border:1px solid rgba(255,255,255,0.18);background:#1a1a1a;" '
            f'title="{ST.name(p)}"/>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)

    if total_images > 4:
        zc1, zc2 = st.columns([2, 1])
        with zc1:
            st.caption(f"+{total_images - 4} more in this folder")
        with zc2:
            if st.button("🔍 View all", key=f"wiz_zoom_{ST.name(folder)}",
                         use_container_width=True,
                         help="Open every image in this folder at full size"):
                _show_folder_gallery(folder)


@st.dialog("Folder gallery", width="large")
def _show_folder_gallery(folder: str):
    """All images in a folder, shown at a comfortable QC size. Used by the wizard
    so the user can inspect everything in archive/dense folders before writing a
    description, without leaving the app."""
    imgs = _folder_images(folder)
    st.markdown(f"**{ST.name(folder)}** · {len(imgs)} images")
    if not imgs:
        st.caption("_(empty)_")
        return
    # Render with cached data URLs — fast even on big folders.
    parts = ['<div style="display:flex;gap:8px;flex-wrap:wrap;">']
    for p in imgs:
        url = _image_to_data_url(p, max_edge=380)
        nm = ST.name(p)
        parts.append(
            f'<div style="text-align:center;">'
            f'<img src="{url}" '
            f'style="width:200px;height:200px;object-fit:cover;border-radius:4px;'
            f'border:1px solid rgba(255,255,255,0.18);background:#1a1a1a;display:block;" '
            f'title="{nm}"/>'
            f'<div style="font-size:11px;color:#888;margin-top:2px;">{nm}</div>'
            f'</div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render_hover_card_html(
    input_path: str,
    overlay_path: str | None,
    height_px: int = 280,
    max_edge: int = 900,
    overlay_url: str | None = None,
    align: str = "center",
) -> str:
    """One self-contained HTML+JS card.
    `input_path` is the bottom image (always the 3D render).
    `overlay_path` is the top image (the variant) — clipped left→right based on mouseX.
    Pass `overlay_url` to supply the top image directly (e.g. a live-graded preview)
    instead of a path. `align` ('center'|'flex-start') anchors the card in the frame.
    """
    input_url = _image_to_data_url(input_path, max_edge=max_edge)
    if overlay_url is None:
        overlay_url = _image_to_data_url(overlay_path, max_edge=max_edge) if overlay_path else None

    # Size the card to the input image's TRUE aspect ratio so the slider shows the
    # real proportions instead of a centre-cropped (object-fit:cover) distortion.
    try:
        with Image.open(_local(input_path)) as _im:
            iw, ih = _im.size
    except Exception:
        iw, ih = 1, 1
    aspect = (iw / ih) if ih else 1.0
    disp_h = float(height_px)
    disp_w = disp_h * aspect
    MAX_W = 700.0  # roughly the content width of a width="large" dialog
    if disp_w > MAX_W:
        disp_w = MAX_W
        disp_h = MAX_W / aspect
    disp_w, disp_h = int(round(disp_w)), int(round(disp_h))

    overlay_html = ""
    if overlay_url:
        overlay_html = f"""
        <div class="overlay" id="ov">
            <img src="{overlay_url}" draggable="false"/>
        </div>
        <div class="divider" id="div"></div>
        """

    return f"""
    <!doctype html>
    <html><head>
    <style>
      html, body {{ margin:0; padding:0; background:transparent; overflow:hidden; }}
      .wrap {{
        display:flex; align-items:center; justify-content:{align};
        width:100%; height:{height_px}px;
      }}
      .card {{
        position:relative;
        width:{disp_w}px;
        height:{disp_h}px;
        max-width:100%;
        border-radius:6px;
        overflow:hidden;
        background:#1a1a1a;
        user-select:none;
      }}
      .card img {{
        position:absolute; inset:0;
        width:100%; height:100%;
        object-fit:contain;
        display:block;
      }}
      .overlay {{
        position:absolute; inset:0;
        clip-path: inset(0 0 0 50%);
        pointer-events:none;
      }}
      .divider {{
        position:absolute; top:0; bottom:0;
        left:50%; width:2px;
        background:rgba(255,255,255,0.85);
        box-shadow:0 0 4px rgba(0,0,0,0.6);
        pointer-events:none;
      }}
      .hint {{
        position:absolute; bottom:6px; left:50%;
        transform:translateX(-50%);
        font:11px/1 system-ui,Segoe UI,Roboto,sans-serif;
        color:rgba(255,255,255,0.9);
        background:rgba(0,0,0,0.45);
        padding:3px 8px; border-radius:99px;
        pointer-events:none;
        opacity:0.85; transition:opacity 200ms ease;
      }}
      .card:hover .hint {{ opacity:0; }}
    </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card" id="card">
          <img class="bg" src="{input_url}" draggable="false"/>
          {overlay_html}
        </div>
      </div>
      <script>
        (function() {{
          const card = document.getElementById('card');
          const ov = document.getElementById('ov');
          const dv = document.getElementById('div');
          if (!ov || !dv) return;  // no overlay → no slider
          function update(x) {{
            const r = card.getBoundingClientRect();
            let pct = ((x - r.left) / r.width) * 100;
            if (pct < 0) pct = 0; if (pct > 100) pct = 100;
            ov.style.clipPath = 'inset(0 0 0 ' + pct + '%)';
            dv.style.left = pct + '%';
          }}
          // Update the divider position only while the mouse is actually moving
          // INSIDE the card. On mouseleave we intentionally do NOTHING — the
          // divider stays where the user left it, so they can scan to the
          // controls or another card without losing their comparison position.
          card.addEventListener('mousemove', e => update(e.clientX));
        }})();
      </script>
    </body></html>
    """


def render_zoom_image_html(url: str, w: int, h: int, height_px: int = 520) -> str:
    """Self-contained pan/zoom viewer for one image, for the full-size lightbox.

    Scroll wheel zooms toward the cursor (1×–8×), drag pans while zoomed, and
    double-click toggles between fit and a 2.5× zoom at the click point. All of it
    is client-side CSS transforms, so interaction is instant — the only cost is the
    one-time (cached) data URL. A single iframe inside the dialog is safe; image-to-
    image navigation is still driven by the parent ◀/▶ buttons + arrow-key bridge."""
    return f"""
    <!doctype html><html><head><style>
      html,body {{ margin:0; padding:0; background:transparent; overflow:hidden; }}
      .vp {{
        position:relative; width:100%; height:{height_px}px;
        background:#0b0b0d; border-radius:8px; overflow:hidden; cursor:zoom-in;
      }}
      .vp.zoomed {{ cursor:grab; }}
      .vp.grabbing {{ cursor:grabbing; }}
      .vp img {{
        position:absolute; left:0; top:0; width:100%; height:100%;
        object-fit:contain; transform-origin:0 0; will-change:transform;
        user-select:none; -webkit-user-drag:none;
      }}
      .hud, .hint {{
        position:absolute; bottom:8px;
        font:12px/1.2 system-ui,Segoe UI,Roboto,sans-serif; color:#fff;
        background:rgba(0,0,0,0.5); padding:4px 9px; border-radius:99px;
        pointer-events:none;
      }}
      .hud {{ left:10px; font-variant-numeric:tabular-nums; }}
      .hint {{ right:10px; color:rgba(255,255,255,0.88); transition:opacity .25s ease; }}
    </style></head><body>
      <div class="vp" id="vp">
        <img id="im" src="{url}" draggable="false"/>
        <div class="hud" id="hud"></div>
        <div class="hint" id="hint">scroll = zoom · drag = pan · double-click = reset</div>
      </div>
      <script>
      (function(){{
        const vp=document.getElementById('vp'), im=document.getElementById('im'), hud=document.getElementById('hud'), hint=document.getElementById('hint');
        const NW={int(w)}, NH={int(h)};
        let scale=1, tx=0, ty=0, drag=false, sx=0, sy=0;
        const clamp=(v,a,b)=> v<a?a:(v>b?b:v);
        function bound(){{
          const r=vp.getBoundingClientRect();
          const maxX=Math.max(0, r.width*scale - r.width), maxY=Math.max(0, r.height*scale - r.height);
          tx=clamp(tx,-maxX,0); ty=clamp(ty,-maxY,0);
        }}
        function apply(){{
          im.style.transform='translate('+tx+'px,'+ty+'px) scale('+scale+')';
          hud.textContent=Math.round(scale*100)+'%  ·  '+NW+'×'+NH+'px';
          vp.classList.toggle('zoomed', scale>1.001);
        }}
        function zoomAt(cx,cy,f){{
          const ns=clamp(scale*f,1,8), k=ns/scale;
          tx=cx-(cx-tx)*k; ty=cy-(cy-ty)*k; scale=ns;
          if(scale<=1.001){{ scale=1; tx=0; ty=0; }}
          bound(); apply();
        }}
        vp.addEventListener('wheel', function(e){{
          e.preventDefault();
          const r=vp.getBoundingClientRect();
          zoomAt(e.clientX-r.left, e.clientY-r.top, e.deltaY<0 ? 1.15 : 1/1.15);
        }}, {{passive:false}});
        vp.addEventListener('mousedown', function(e){{
          if(scale<=1) return; drag=true; sx=e.clientX; sy=e.clientY;
          vp.classList.add('grabbing'); e.preventDefault();
        }});
        window.addEventListener('mousemove', function(e){{
          if(!drag) return; tx+=e.clientX-sx; ty+=e.clientY-sy; sx=e.clientX; sy=e.clientY; bound(); apply();
        }});
        window.addEventListener('mouseup', function(){{ drag=false; vp.classList.remove('grabbing'); }});
        vp.addEventListener('dblclick', function(e){{
          const r=vp.getBoundingClientRect();
          if(scale>1){{ scale=1; tx=0; ty=0; apply(); }}
          else {{ zoomAt(e.clientX-r.left, e.clientY-r.top, 2.5); }}
        }});
        window.addEventListener('keydown', function(e){{
          if(e.key==='+'||e.key==='='){{ zoomAt(vp.clientWidth/2, vp.clientHeight/2, 1.2); e.preventDefault(); }}
          else if(e.key==='-'||e.key==='_'){{ zoomAt(vp.clientWidth/2, vp.clientHeight/2, 1/1.2); e.preventDefault(); }}
          else if(e.key==='0'){{ scale=1; tx=0; ty=0; apply(); e.preventDefault(); }}
        }});
        apply();
        setTimeout(function(){{ hint.style.opacity='0'; }}, 2600);
      }})();
      </script>
    </body></html>
    """


# ═══ State init + sidebar ════════════════════════════════════════════════

def _init_session_state():
    st.session_state.setdefault("route", "landing")
    st.session_state.setdefault("pending_regens", {})
    st.session_state.setdefault("pending_prompts", {})
    st.session_state.setdefault("pending_grades", {})   # photo_id → Future (async grading)
    st.session_state.setdefault("manifest", None)
    st.session_state.setdefault("project", None)
    st.session_state.setdefault("detail_photo_id", None)
    # Per-photo board variant index is stored under flat `vidx::<photo_id>` keys
    # (see _vidx_key / _current_board_variant). No nested dict — Streamlit
    # tracks mutations more reliably with top-level keys.


_init_session_state()


def _load_manifest_for_project(project: PROJ.Project) -> M.Manifest:
    return P.ingest(cfg, project.brand_root, project.output_root, project=project)


def _close_project():
    """Reset to landing — clear all per-project session state, including the
    per-photo board variant indices that use the flat `vidx::` keys."""
    st.session_state["project"] = None
    st.session_state["manifest"] = None
    st.session_state["detail_photo_id"] = None
    st.session_state["pending_regens"] = {}
    st.session_state["pending_prompts"] = {}
    st.session_state["pending_grades"] = {}
    # Drop every per-photo board variant index. Iterate over a list copy so we
    # can mutate the dict-like during iteration.
    for k in [k for k in st.session_state.keys() if str(k).startswith("vidx::")]:
        del st.session_state[k]
    st.query_params.clear()   # so _restore_from_url doesn't re-open the project on rerun
    set_route("landing")
    st.rerun()


def _reap_pending_grades():
    """Pull completed grade futures off pending_grades, surfacing exceptions to last_error."""
    pending = st.session_state.get("pending_grades", {})
    done = [pid for pid, f in pending.items() if f.done()]
    for pid in done:
        try:
            pending[pid].result()
        except Exception:
            pass  # error already on photo.last_error via submit_grade's wrapper
        del pending[pid]


def _sb_section(label: str):
    """Sidebar section header — small uppercase caption."""
    st.markdown(
        f"<div style='font-size:11px;font-weight:600;color:var(--text-3);"
        f"text-transform:uppercase;letter-spacing:0.08em;"
        f"margin:20px 0 10px 0;'>{label}</div>",
        unsafe_allow_html=True,
    )


def render_sidebar():
    """Shared sidebar — visible on board + detail routes.

    Visual rhythm: project identity at the top, then named sections separated by
    small uppercase headers rather than horizontal rules. Less visual noise."""
    project: PROJ.Project | None = st.session_state.get("project")
    manifest: M.Manifest | None = st.session_state.get("manifest")

    with st.sidebar:
        # ── Identity block
        if project:
            brand_name = Path(project.brand_root).name
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:10px;'>"
                f"<div style='font-size:20px;'>📁</div>"
                f"<div style='min-width:0;flex:1;'>"
                f"<div style='font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;"
                f"white-space:nowrap;'>{project.name}</div>"
                f"<div style='font-size:11px;color:var(--text-3);overflow:hidden;text-overflow:ellipsis;"
                f"white-space:nowrap;'>{brand_name}</div>"
                f"</div></div>",
                unsafe_allow_html=True,
            )
            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
            sc1, sc2 = st.columns(2)
            with sc1:
                if st.button("← Projects", use_container_width=True, key="back_to_landing"):
                    _close_project()
            with sc2:
                if st.button("↻ Rescan", use_container_width=True, key="rescan_folder",
                             help="Re-list the brand folder for new/changed renders "
                                  "(normal open uses a cached list for speed)"):
                    st.session_state["manifest"] = P.ingest(
                        cfg, project.brand_root, project.output_root,
                        project=project, force_rescan=True,
                    )
                    st.rerun()
        else:
            st.markdown(
                "<div style='font-size:18px;font-weight:600;'>AI Bucherer</div>"
                "<div style='font-size:11px;color:var(--text-3);'>Auto pipeline</div>",
                unsafe_allow_html=True,
            )

        # ── Generation settings
        _sb_section("Generation")
        n_variants = st.slider(
            "Variants per regenerate", 1, cfg.max_n, cfg.default_n,
            key="n_variants_slider",
            help=f"Each photo generates this many variants from Nano Banana (default {cfg.default_n}).",
        )

        # ── View settings (route-specific)
        if current_route() == "board":
            _sb_section("Board view")
            st.session_state.setdefault("board_card_size", "Medium")
            st.radio(
                "Card size",
                ["Small", "Medium", "Large"],
                index=["Small", "Medium", "Large"].index(st.session_state["board_card_size"]),
                key="board_card_size",
                horizontal=True,
                label_visibility="collapsed",
            )

        # ── Stats
        _sb_section("Stats")
        if manifest:
            n_photos = len(manifest.photos)
            n_graded = sum(1 for p in manifest.photos.values() if p.graded)
            n_ready = sum(1 for p in manifest.photos.values() if p.variants)
            pct = (n_graded / n_photos * 100) if n_photos else 0
            st.markdown(
                f"<div style='display:flex;flex-direction:column;gap:8px;'>"
                f"<div style='display:flex;justify-content:space-between;align-items:baseline;'>"
                f"<span style='font-size:11px;color:var(--text-3);text-transform:uppercase;letter-spacing:0.06em;'>Cost</span>"
                f"<span style='font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;'>${manifest.total_cost_usd:.2f}</span>"
                f"</div>"
                f"<div style='display:flex;justify-content:space-between;align-items:baseline;'>"
                f"<span style='font-size:11px;color:var(--text-3);text-transform:uppercase;letter-spacing:0.06em;'>Graded</span>"
                f"<span style='font-size:14px;font-weight:500;font-variant-numeric:tabular-nums;'>"
                f"<b>{n_graded}</b><span style='color:var(--text-3);'> / {n_photos}</span></span>"
                f"</div>"
                f"<div style='height:4px;background:var(--bg-elev-3);border-radius:2px;overflow:hidden;'>"
                f"<div style='height:100%;width:{pct:.1f}%;background:var(--accent);transition:width 300ms ease;'></div>"
                f"</div>"
                f"<div style='font-size:11px;color:var(--text-3);'>{n_ready} of {n_photos} have variants</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='font-size:12px;color:var(--text-3);'>No project open.</div>",
                unsafe_allow_html=True,
            )

        # Hero
        if manifest:
            st.markdown("---")
            st.header("Hero (grading reference)")
            st.caption(
                "Color reference all graded outputs match against. Without a hero, "
                "grading only normalizes the background."
            )
            hero_upload = st.file_uploader(
                "Upload hero",
                type=["png", "jpg", "jpeg", "webp"],
                key="sidebar_hero_uploader",
            )
            if hero_upload is not None and st.button(
                "📌 Set as hero", key="set_hero_btn", use_container_width=True,
            ):
                hero_target = ST.join(manifest.output_root, ".hero", hero_upload.name)
                ST.write_bytes(hero_target, hero_upload.getvalue())
                manifest.hero_path = hero_target
                manifest.hero_photo_id = None
                M.save(manifest, ST)
                st.success(f"Hero set to {hero_upload.name}")
                st.rerun()

            if manifest.hero_path:
                if _exists_cached(manifest.hero_path):
                    hero_name = ST.name(manifest.hero_path)
                    st.image(str(_local(manifest.hero_path)), width=200, caption=f"Hero: {hero_name}")
                    hc1, hc2 = st.columns(2)
                    with hc1:
                        if st.button("Clear", key="clear_hero_btn", use_container_width=True):
                            manifest.hero_path = None
                            manifest.hero_photo_id = None
                            M.save(manifest, ST)
                            st.rerun()
                    with hc2:
                        if st.button("Re-grade all", key="regrade_all_btn", use_container_width=True,
                                     help="Re-run the harmonizer on every picked variant. Free."):
                            n = P.regrade_all_selected(cfg, manifest)
                            st.success(f"Re-graded {n} photo(s).")
                            st.rerun()
                else:
                    st.warning(f"Hero file missing: {manifest.hero_path}")
            else:
                st.warning("No hero set — grading is background-only.")

        # Project settings (briefs + per-product hero override)
        if project:
            _sb_section("Project settings")

            with st.expander("Product descriptions", expanded=False):
                st.caption(
                    "Ground-truth line Claude reads before its brief. Changes "
                    "apply on next prompt regeneration."
                )
                changed = False
                for pg in project.products:
                    new_desc = st.text_input(
                        f"{pg.folder_name} ({'worn' if pg.is_worn else 'packshot'}) · {pg.n_images} img",
                        value=pg.description,
                        key=f"proj_desc_{pg.folder_name}",
                        placeholder='e.g. "a ring in gold and diamonds"',
                    )
                    if new_desc != pg.description:
                        pg.description = new_desc
                        changed = True
                if changed:
                    PROJ.save_project(cfg, project)
                    if manifest:
                        manifest.product_briefs = project.product_brief_map()
                        M.save(manifest, ST)
                    st.toast("Saved.", icon="💾")

            with st.expander("Per-product hero overrides", expanded=False):
                st.caption(
                    "Each product can use a different grading reference. If no "
                    "override is set, the photo grades against the project-wide hero."
                )
                if not manifest:
                    st.markdown("<div class='subtle'>Open a project to manage overrides.</div>",
                                unsafe_allow_html=True)
                else:
                    for pg in project.products:
                        current_path = manifest.product_heroes.get(pg.folder_name)
                        current_ok = bool(current_path and _exists_cached(current_path))
                        # Per-product card
                        st.markdown(
                            f"<div style='font-size:12px;font-weight:600;color:var(--text);"
                            f"margin:10px 0 4px 0;'>{pg.folder_name}</div>",
                            unsafe_allow_html=True,
                        )
                        if current_ok:
                            ph1, ph2 = st.columns([2, 1])
                            with ph1:
                                # Thumbnail of the current override
                                url = _image_to_data_url(current_path, max_edge=160)
                                st.markdown(
                                    f"<img src='{url}' style='width:100%;max-width:160px;"
                                    f"aspect-ratio:1/1;object-fit:cover;border-radius:6px;"
                                    f"border:1px solid var(--border-subtle);' "
                                    f"title='{ST.name(current_path)}'/>",
                                    unsafe_allow_html=True,
                                )
                            with ph2:
                                if st.button("Clear", key=f"clearhero_{pg.folder_name}",
                                             use_container_width=True,
                                             help="Fall back to the project-wide hero"):
                                    manifest.product_heroes.pop(pg.folder_name, None)
                                    M.save(manifest, ST)
                                    st.rerun()
                        else:
                            st.markdown(
                                "<div class='subtle' style='font-size:11px;margin-bottom:6px;'>"
                                "Using project default. Upload an override:</div>",
                                unsafe_allow_html=True,
                            )

                        up = st.file_uploader(
                            "Upload hero", type=["png", "jpg", "jpeg", "webp"],
                            key=f"heroup_{pg.folder_name}",
                            label_visibility="collapsed",
                        )
                        if up is not None and st.button(
                            "Set as override", key=f"setoverhero_{pg.folder_name}",
                            use_container_width=True,
                        ):
                            target = ST.join(manifest.output_root, ".hero", pg.folder_name, up.name)
                            ST.write_bytes(target, up.getvalue())
                            manifest.product_heroes[pg.folder_name] = target
                            M.save(manifest, ST)
                            st.toast(f"Hero set for {pg.folder_name}", icon="📌")
                            st.rerun()

        # Maintenance — thumbnail pre-build (Rescan lives in the identity block above)
        if project and manifest:
            _sb_section("Maintenance")
            if st.button("⚡ Build thumbnails", use_container_width=True, key="build_thumbs",
                         help="Pre-generate the fast board thumbnails for every current "
                              "visual (makes the next open instant)"):
                with st.spinner("Building thumbnails…"):
                    n = _build_all_thumbs(manifest)
                st.toast(f"Built {n} thumbnail(s).", icon="⚡")
                st.rerun()
            n_hidden = sum(len(p.hidden_variants or []) for p in manifest.photos.values())
            if n_hidden:
                if st.button(f"🗑 Empty trash ({n_hidden})", use_container_width=True,
                             key="empty_trash",
                             help="Permanently delete the variants you've removed "
                                  "(and their graded outputs). Cannot be undone."):
                    with st.spinner("Deleting…"):
                        n = P.purge_hidden(cfg, manifest)
                    st.toast(f"Permanently deleted {n} variant(s).", icon="🗑")
                    st.rerun()

        # Keys + model
        st.markdown("---")
        st.caption(
            f"Model: `{cfg.gemini_model}`  ·  {cfg.gemini_aspect_ratio} · {cfg.gemini_image_size}"
        )
        def _fp(k: str) -> str:
            return f"✓ {len(k)} chars …{k[-4:]}" if k else "✗ missing"
        st.caption(f"ANTHROPIC: {_fp(cfg.anthropic_api_key)}")
        st.caption(f"GEMINI: {_fp(cfg.gemini_api_key)}")
        if not cfg.anthropic_api_key or not cfg.gemini_api_key:
            st.warning("Missing keys — set them in Streamlit Secrets (or `.env` locally).")


# ═══ Route: LANDING ══════════════════════════════════════════════════════

def render_landing_page():
    # ── Header strip: tight, brand-forward, no oversized title
    head_l, head_r = st.columns([5, 2])
    with head_l:
        st.markdown(
            "<div style='display:flex;align-items:baseline;gap:12px;'>"
            "<span style='font-size:22px;font-weight:600;letter-spacing:-0.01em;'>AI Bucherer</span>"
            "<span class='subtle' style='font-size:13px;'>Auto pipeline</span>"
            "</div>"
            "<div class='subtle' style='font-size:13px;margin-top:4px;'>"
            "3D render → Claude prompt → Nano Banana variants → graded output"
            "</div>",
            unsafe_allow_html=True,
        )
    with head_r:
        if st.button("➕  New project", type="primary", use_container_width=True,
                     key="landing_new_project"):
            for k in ("wiz_browse", "wiz_descend", "wiz_manual", "wiz_browse_crumb",
                      "wiz_output", "wiz_name", "wiz_products"):
                st.session_state.pop(k, None)
            set_route("create")
            st.rerun()

    _missing_briefs = [c for c in ("packshot", "worn") if not cfg.get_brief(c).strip()]
    if _missing_briefs:
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        st.error(
            "Missing brief file(s): "
            + ", ".join(f"`prompts/brief_{c}.md`" for c in _missing_briefs)
            + ". Generation is blocked until these exist."
        )

    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)
    st.markdown("#### Projects")

    projects = PROJ.list_projects(cfg)
    if not projects:
        empty = st.container(border=True)
        with empty:
            st.markdown(
                "<div style='text-align:center;padding:24px 0;'>"
                "<div style='font-size:32px;margin-bottom:8px;'>📁</div>"
                "<div style='font-weight:600;margin-bottom:4px;'>No projects yet</div>"
                "<div class='subtle' style='font-size:13px;'>"
                "Click <b>New project</b> to point at a brand folder and start.</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        return

    for s in projects:
        tile = st.container(border=True)
        with tile:
            tc1, tc2, tc3 = st.columns([6, 1.4, 0.6])
            with tc1:
                brand_name = Path(s.brand_root).name
                out_name = Path(s.output_root).name
                updated = s.updated_at[:10] if s.updated_at else "—"
                st.markdown(
                    f"<div style='font-size:15px;font-weight:600;margin-bottom:4px;'>{s.name}</div>"
                    f"<div class='row-wrap' style='font-size:12px;color:var(--text-3);'>"
                    f"<span class='nowrap'>{brand_name}</span>"
                    f"<span style='color:var(--text-disabled);'>→</span>"
                    f"<span class='nowrap'>{out_name}</span>"
                    f"<span style='color:var(--text-disabled);margin:0 4px;'>·</span>"
                    f"<span class='nowrap'>{s.n_products} product folders</span>"
                    f"<span style='color:var(--text-disabled);margin:0 4px;'>·</span>"
                    f"<span class='nowrap'>updated {updated}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            with tc2:
                if st.button("Open →", key=f"open_{s.slug}",
                             type="primary",
                             use_container_width=True):
                    proj = PROJ.load_project(cfg, s.slug)
                    if not proj:
                        st.error("Failed to load project.")
                    else:
                        st.session_state["project"] = proj
                        st.session_state["manifest"] = _load_manifest_for_project(proj)
                        mf = st.session_state["manifest"]
                        pending = any(not p.variants for p in mf.photos.values())
                        set_route("loading" if pending else "board")
                        st.rerun()
            with tc3:
                if st.button("🗑", key=f"del_{s.slug}", use_container_width=True,
                             help="Delete project metadata (does not touch your images)"):
                    PROJ.delete_project(cfg, s.slug)
                    st.rerun()


# ═══ Route: CREATE (new project wizard) ══════════════════════════════════

def _brand_folder_picker(is_dropbox: bool) -> str:
    """File-explorer-style folder browser. Returns the currently-open folder path.

    One click on a folder row enters it. Root/Up buttons navigate outward. The
    current folder is the selection — product cards appear below as soon as it
    contains product image-subfolders. A manual-path expander is the fallback.
    """
    root = "/" if is_dropbox else str(Path.home())
    cur = st.session_state.get("wiz_browse") or root
    try:
        if not ST.is_dir(cur):
            cur = root
    except Exception:
        cur = root
    st.session_state["wiz_browse"] = cur

    # Toolbar: Root / Up / current path
    t1, t2, t3 = st.columns([1, 1, 5])
    with t1:
        if st.button("🏠 Root", use_container_width=True, key="nav_root",
                     disabled=(cur == root)):
            st.session_state["wiz_browse"] = root
            st.rerun()
    with t2:
        at_root = (cur == root) or (ST.parent(cur) == cur)
        if st.button("⬆ Up", use_container_width=True, key="nav_up", disabled=at_root):
            st.session_state["wiz_browse"] = ST.parent(cur)
            st.rerun()
    with t3:
        st.markdown(
            f"<div class='subtle' style='padding-top:8px;'>📂 <code>{cur}</code></div>",
            unsafe_allow_html=True,
        )

    # Scrollable folder pane — one click enters a folder
    subdirs = ST.list_subdirs(cur)
    pane = st.container(height=300, border=True)
    with pane:
        if not subdirs:
            st.caption("No subfolders here. Use ⬆ Up, or pick this folder if your products are inside it.")
        for s in subdirs:
            if st.button(f"📁 {ST.name(s)}", key=f"nav::{s}", use_container_width=True):
                st.session_state["wiz_browse"] = s
                st.rerun()

    with st.expander("Advanced: paste a path"):
        manual = st.text_input(
            "Folder path",
            placeholder="/THENEWFACE/02_PROJECTS/…/Bucherer" if is_dropbox else r"C:\path\to\Bucherer",
            key="wiz_manual",
        ).strip()
        if manual:
            return manual

    return cur


def render_create_project_page():
    st.title("Create new project")

    if st.button("← Cancel", key="create_cancel"):
        set_route("landing")
        st.rerun()

    st.markdown("##### 1) Choose folders")

    _dropbox = getattr(ST, "backend", "local") == "dropbox"
    brand_root = _brand_folder_picker(_dropbox)
    brand_valid = bool(brand_root and ST.exists(brand_root) and ST.is_dir(brand_root))
    if brand_root and not brand_valid:
        st.warning(f"Folder doesn't exist: {brand_root}")

    default_output = ""
    if brand_valid:
        default_output = P.default_output_root(cfg, brand_root)
    output_text = st.text_input(
        "Output folder",
        value=st.session_state.get("wiz_output", default_output),
        placeholder="auto: <brand>_OUT",
        key="wiz_output",
    )
    output_root_text = output_text.strip()

    name_default = ST.name(brand_root) if brand_valid else ""
    name = st.text_input(
        "Project name",
        value=st.session_state.get("wiz_name", name_default),
        placeholder=name_default or "My project",
        key="wiz_name",
    )

    if not brand_valid:
        st.info("Navigate to (or paste) the brand folder to continue.")
        return

    # Discover products on the fly
    discovered = PROJ.discover_product_groups(cfg, brand_root)
    if not discovered:
        st.info(
            "No product image-folders here yet — keep navigating into the folder that "
            "holds one subfolder per product. Expected layout:\n\n"
            "```\nBrand/\n  01_Product/\n    img1.png\n    img2.png\n  01_worn/\n    ...\n```"
        )
        return

    st.markdown(
        f"##### 2) Describe each product · {len(discovered)} folder(s) found"
    )
    st.caption(
        "These descriptions are passed to Claude as the first line of the system prompt "
        "(`The image attached is: \"…\"`) so it doesn't make vision mistakes about jewelry type or material."
    )

    # Preserve any in-progress descriptions across reruns
    existing_descs = st.session_state.get("wiz_products", {})
    for pg in discovered:
        key = f"wiz_desc_{pg.folder_name}"
        if key not in st.session_state:
            st.session_state[key] = existing_descs.get(pg.folder_name, "")

    grid_cols = st.columns(2)
    for i, pg in enumerate(discovered):
        with grid_cols[i % 2]:
            box = st.container(border=True)
            with box:
                badge = "<span class='badge badge-worn'>worn</span>" if pg.is_worn else "<span class='badge'>packshot</span>"
                st.markdown(
                    f"**{pg.folder_name}** · {pg.n_images} img &nbsp; {badge}",
                    unsafe_allow_html=True,
                )
                # Thumbnails so the user can actually see what they're describing.
                # Uses cached data URLs — instant after first load, free on reruns.
                _render_wizard_thumb_strip(ST.join(brand_root, pg.folder_name), pg.n_images)
                st.text_input(
                    "Product description",
                    key=f"wiz_desc_{pg.folder_name}",
                    placeholder=(
                        'e.g. "a worn necklace in gold and diamonds, paperclip pendant, taupe V-neck"'
                        if pg.is_worn
                        else 'e.g. "a ring in gold and diamonds, three asscher stones"'
                    ),
                    label_visibility="collapsed",
                )

    # Validation
    missing = [pg.folder_name for pg in discovered
               if not st.session_state.get(f"wiz_desc_{pg.folder_name}", "").strip()]
    can_create = bool(name.strip()) and not missing

    st.markdown("---")
    cc1, cc2 = st.columns([3, 1])
    with cc1:
        if missing:
            st.caption(
                f"Add a description for: {', '.join(f'`{f}`' for f in missing)}"
            )
        else:
            n_total = sum(pg.n_images for pg in discovered)
            est_cost = n_total * cfg.default_n * cfg.effective_cost_per_image + n_total * 0.01
            st.caption(
                f"Ready. {n_total} image(s) × {cfg.default_n} variants ≈ **${est_cost:.2f}** for the first auto-prepare."
            )
    with cc2:
        if st.button(
            "Create project + generate", type="primary",
            disabled=not can_create, use_container_width=True,
        ):
            # Build Project
            out = output_root_text or P.default_output_root(cfg, brand_root)
            products = []
            for pg in discovered:
                pg.description = st.session_state.get(f"wiz_desc_{pg.folder_name}", "").strip()
                products.append(pg)
            proj = PROJ.Project(
                name=name.strip(),
                brand_root=brand_root,
                output_root=out,
                products=products,
            )
            PROJ.save_project(cfg, proj)
            mf = _load_manifest_for_project(proj)

            st.session_state["project"] = proj
            st.session_state["manifest"] = mf

            # Submit all pending photos in parallel right away
            pending = [p for p in mf.photos.values() if not p.variants]
            pending_dict = st.session_state.setdefault("pending_regens", {})
            for photo in pending:
                fut = P.submit_regenerate(
                    cfg, mf, photo,
                    get_anthropic_client(cfg.anthropic_api_key), get_gemini_client(cfg.gemini_api_key),
                    n=st.session_state.get("n_variants_slider", cfg.default_n),
                )
                pending_dict[photo.photo_id] = fut

            set_route("loading")
            st.rerun()


# ═══ Route: LOADING (auto-prepare progress bar) ══════════════════════════

def render_loading_page():
    project: PROJ.Project | None = st.session_state.get("project")
    manifest: M.Manifest | None = st.session_state.get("manifest")
    if not project or not manifest:
        set_route("landing")
        st.rerun()
        return

    st.title(f"Preparing **{project.name}**")
    st.caption("Generating prompts + variants in parallel. This runs once per project.")

    pending = st.session_state.setdefault("pending_regens", {})

    # Submit any photo that still needs variants and isn't already in flight.
    # The wizard submits before routing here, but the "open existing project"
    # path does not — so the loading page is the single, idempotent place that
    # guarantees pending photos actually get dispatched. Photos that previously
    # errored are left for manual retry on the board (they count as failed below).
    for photo in manifest.photos.values():
        if photo.variants or photo.last_error or photo.photo_id in pending:
            continue
        pending[photo.photo_id] = P.submit_regenerate(
            cfg, manifest, photo,
            get_anthropic_client(cfg.anthropic_api_key),
            get_gemini_client(cfg.gemini_api_key),
            n=st.session_state.get("n_variants_slider", cfg.default_n),
        )

    # Reap any completed futures
    done_ids = [pid for pid, f in pending.items() if f.done()]
    for pid in done_ids:
        try:
            pending[pid].result()
        except Exception:
            pass
        del pending[pid]

    # Status counts
    photos = list(manifest.photos.values())
    n_total = len(photos)
    n_done = sum(1 for p in photos if p.variants)
    n_running = sum(1 for f in pending.values() if not f.done())
    n_failed = sum(1 for p in photos if p.last_error and not p.variants)

    pct = (n_done / n_total) if n_total else 1.0
    st.progress(pct, text=f"{n_done} / {n_total} photos ready · {n_running} in flight")

    if n_failed:
        st.warning(f"{n_failed} photo(s) errored. They'll be shown on the board view so you can retry.")
        with st.expander("Show errors"):
            for p in photos:
                if p.last_error and not p.variants:
                    st.markdown(f"- **{p.photo_id}** — {p.last_error[:200]}")

    # Done?
    if n_running == 0 and n_done + n_failed >= n_total:
        st.success("All set. Loading the board…")
        set_route("board")
        st.rerun()
        return

    # Otherwise keep polling
    st_autorefresh(interval=1500, key="loading_poll", limit=None)
    st.caption("⚙ Working in background — feel free to leave this tab; state is saved.")


# ═══ Route: BOARD (Pinterest-style grid) ═════════════════════════════════

def _board_cols_for_size(size: str) -> int:
    """Cards per row for the chosen thumbnail size. Fewer columns → wider (bigger)
    square thumbnails. Drives both the column layout and the row chunking."""
    return {"Small": 5, "Medium": 4, "Large": 3}.get(size, 4)


def _vidx_key(photo: M.PhotoState) -> str:
    """Session-state key holding the displayed variant index for a photo on the board.
    Flat keys (not a nested dict) avoid any Streamlit edge cases around dict-mutation
    persistence across reruns."""
    return f"vidx::{photo.photo_id}"


def _current_board_variant(photo: M.PhotoState) -> int:
    """Which variant index is currently displayed for this photo on the board.
    Reads from a per-photo flat key in st.session_state.

    Falls back to the photo's selected variant (if any) or 0 when no index is
    stored, or when the stored index is out of range for the current variant list.
    """
    key = _vidx_key(photo)
    if key in st.session_state:
        idx = st.session_state[key]
        if isinstance(idx, int) and 0 <= idx < max(len(photo.variants), 1):
            return idx
    if photo.selected_variant and photo.selected_variant in photo.variants:
        idx = photo.variants.index(photo.selected_variant)
    else:
        idx = 0
    st.session_state[key] = idx
    return idx


def _set_board_variant(photo: M.PhotoState, idx: int) -> None:
    """Write the per-photo displayed variant index. Used by the ◀/▶ click handlers
    and by _handle_select to keep the board card in sync with the picked variant."""
    st.session_state[_vidx_key(photo)] = idx


# ── Module-level button callbacks ────────────────────────────────────────
# Defined at module scope (not inside render_board_card) so each st.button's
# on_click binds to a stable function reference instead of a fresh closure
# per render. Streamlit captures the callback by reference; closure-based
# callbacks created on every rerun risk losing their captured variables.
# We pass photo_id + n_variants via functools.partial to bind them explicitly.

def _cb_variant_step(pid: str, n: int, delta: int) -> None:
    """Advance (delta=+1) or retreat (delta=-1) the displayed variant for `pid`."""
    if n <= 0:
        return
    key = f"vidx::{pid}"
    cur = st.session_state.get(key, 0)
    if not isinstance(cur, int):
        cur = 0
    st.session_state[key] = (cur + delta) % n


def _cb_open_detail(pid: str) -> None:
    """Open the detail view for `pid` (route + selection update)."""
    st.session_state["detail_photo_id"] = pid
    st.session_state["route"] = "detail"


def render_board_card(photo: M.PhotoState):
    """One card on the board: square thumbnail + variant arrows + open button.

    Layout (top-to-bottom):
      • Square 1:1 thumbnail of the current variant / graded output
      • Tight control row (‹  chip  ›  ⤢)  — chip shows "1 / 4" or status
      • Filename + classification + status badges
    """
    n_variants = len(photo.variants)
    idx = _current_board_variant(photo)
    overlay_path = photo.variants[idx] if n_variants > 0 else None

    fut = st.session_state.get("pending_regens", {}).get(photo.photo_id)
    is_regenerating = fut is not None and not fut.done()
    gfut = st.session_state.get("pending_grades", {}).get(photo.photo_id)
    is_grading = gfut is not None and not gfut.done()

    # ── 1) The image (top, dominant)
    # Show the canonical review image: the graded output if saved, otherwise the
    # currently-selected variant, falling back to the raw input. We deliberately
    # use a native st.image (not a per-card components.v1.html iframe): rendering
    # 40+ custom-component iframes on one page wedges the Streamlit session so
    # that NO widget interaction triggers a rerun — every board button silently
    # dies. The input-vs-output compare slider lives in the detail view, where a
    # single component instance is safe. The keyed container lets the CSS below
    # force a square 1:1 thumbnail.
    # `photo.graded` is the manifest's own truth that the output was written — trust
    # it instead of a per-card filesystem/API existence probe (cheap on the board grid).
    # Trust the manifest — no per-card existence probe. On the Dropbox backend a
    # probe per card was 25 sequential `files_get_metadata` network calls every
    # rerun (and every 3s autorefresh tick), which froze the board. A path listed
    # in photo.variants exists (ingest reconciles dangling ones on load); if it
    # somehow can't be fetched, _board_thumb falls back to a placeholder.
    if photo.graded:
        display_img = photo.output_path
    elif overlay_path:
        display_img = overlay_path
    else:
        display_img = photo.input_path

    with st.container(key=f"boardimg-{photo.photo_id}"):
        st.image(_board_thumb(display_img), use_container_width=True)

    # ── 2) Control row — clean toolbar inspired by the Gemini variant card:
    #     ‹  1 / 4  ›        ⤢
    # Chevrons + inline count + fullscreen icon. When the photo has no
    # variants we DROP the arrows entirely (instead of showing disabled
    # squares) and the chip carries the status message ("pending"/"error"/
    # "generating"). The button keys include n_variants so Streamlit
    # discards the stale widget state when the variant count changes —
    # without this, a button rendered initially disabled stays effectively
    # disabled even after variants arrive.
    has_nav = n_variants > 1
    is_picked = bool(photo.selected_variant and photo.selected_variant == overlay_path)

    if is_grading:
        chip_inner = "<span style='font-weight:600;'>● grading</span>"
        chip_class = "var-chip var-chip-running"
    elif is_regenerating:
        chip_inner = "<span style='font-weight:600;'>● generating</span>"
        chip_class = "var-chip var-chip-running"
    elif n_variants:
        mark = " · saved" if is_picked and photo.graded else ""
        chip_inner = f"<b style='font-variant-numeric:tabular-nums;'>{idx + 1}</b>"\
                     f"<span style='color:var(--text-3);margin:0 4px;'>/</span>"\
                     f"<span style='font-variant-numeric:tabular-nums;'>{n_variants}</span>"\
                     f"{mark}"
        chip_class = "var-chip var-chip-picked" if is_picked else "var-chip"
    elif photo.last_error:
        chip_inner = "error"
        chip_class = "var-chip var-chip-empty"
    else:
        chip_inner = "pending"
        chip_class = "var-chip var-chip-empty"

    # Wrap the toolbar in a keyed container so the .st-key-boardctrls CSS above
    # can actually reach the buttons (see the CSS comment for why a markdown div
    # can't). Layout: [‹] [chip] [›]  ······  [⤢]; arrows drop when has_nav is False.
    # The card can compare as soon as there's a rendered image distinct from the
    # raw input (a graded output or a generated variant). display_img already
    # encodes that choice without any extra existence probes.
    can_compare = display_img != photo.input_path

    with st.container(key=f"boardctrls-{photo.photo_id}"):
        if has_nav:
            cPrev, cChip, cNext, cSpacer, cCompare, cOpen = st.columns([1, 3, 1, 0.5, 1, 1])
        else:
            # Arrows hidden; chip takes the space.
            cPrev = cNext = None
            cChip, cSpacer, cCompare, cOpen = st.columns([5, 0.5, 1, 1])

        # State changes go through on_click callbacks, which run before the rerun —
        # the button's return value is intentionally ignored.
        if has_nav and cPrev is not None:
            with cPrev:
                st.button(
                    "‹", key=f"prev_{photo.photo_id}_{n_variants}",
                    use_container_width=True,
                    help="Previous variant",
                    on_click=_cb_variant_step,
                    args=(photo.photo_id, n_variants, -1),
                )
        with cChip:
            st.markdown(
                f"<div style='display:flex;justify-content:center;align-items:center;"
                f"height:30px;'><div class='{chip_class}'>{chip_inner}</div></div>",
                unsafe_allow_html=True,
            )
        if has_nav and cNext is not None:
            with cNext:
                st.button(
                    "›", key=f"next_{photo.photo_id}_{n_variants}",
                    use_container_width=True,
                    help="Next variant",
                    on_click=_cb_variant_step,
                    args=(photo.photo_id, n_variants, +1),
                )
        with cCompare:
            # Calling the @st.dialog directly on click matches the detail page.
            # before = raw input render; after = the canonical display image
            # (graded output if saved, else the current variant).
            if st.button(
                "🆚", key=f"cmp_board_{photo.photo_id}",
                use_container_width=True,
                disabled=not can_compare,
                help="Compare input ↔ result" if can_compare else "Nothing to compare yet",
            ):
                _show_comparison(
                    before_path=photo.input_path,
                    after_path=display_img,
                    before_label="Input (3D render)",
                    after_label="Graded" if photo.graded else "Variant",
                    title=Path(photo.input_path).name,
                )
        with cOpen:
            st.button(
                "⤢", key=f"open_{photo.photo_id}",
                use_container_width=True,
                help="Open detail view",
                on_click=_cb_open_detail,
                args=(photo.photo_id,),
            )

    # ── 3) Filename + badges (compact footer)
    cls = photo.classification or "packshot"
    cls_class = "badge-worn" if cls == "worn" else "badge-packshot"
    badges_html = f"<span class='badge {cls_class}'>{cls}</span>"
    if photo.graded:
        badges_html += "<span class='badge badge-ready'>saved</span>"
    elif photo.last_error and not photo.variants:
        # Hover shows the full error via title attribute
        err_short = (photo.last_error or "").replace('"', "'")[:120]
        badges_html += f"<span class='badge badge-error' title=\"{err_short}\">error</span>"

    fname = Path(photo.input_path).name
    st.markdown(
        f"<div style='display:flex;justify-content:space-between;align-items:center;"
        f"gap:8px;margin-top:6px;font-size:11px;'>"
        f"<span class='subtle' style='overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
        f"min-width:0;flex:1;' title='{fname}'>{fname}</span>"
        f"<span class='row' style='flex-shrink:0;'>{badges_html}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


# ── Gallery tile (one per kept variant) + its callbacks ──────────────────

def _cb_open_detail_variant(pid: str) -> None:
    st.session_state["detail_photo_id"] = pid
    st.session_state["route"] = "detail"


def _cb_hide_variant(pid: str, vpath: str) -> None:
    mf = st.session_state.get("manifest")
    if mf and pid in mf.photos:
        P.hide_variant(cfg, mf, mf.photos[pid], vpath)
        st.session_state["__last_hidden"] = (pid, vpath)
        st.toast("Variant deleted — Undo at the top.", icon="🗑")


def _cb_grade_variant(pid: str, vpath: str, overrides: dict | None = None) -> None:
    mf = st.session_state.get("manifest")
    if not mf or pid not in mf.photos:
        return
    pend = st.session_state.setdefault("pending_grades", {})
    gkey = f"{pid}::{Path(vpath).name}"
    ex = pend.get(gkey)
    if ex is not None and not ex.done():
        return
    pend[gkey] = P.submit_grade_variant(cfg, mf, mf.photos[pid], vpath, overrides=overrides)


def _cb_detail_opt(pid: str, delta: int, n: int) -> None:
    """Cycle the detail viewer's current option (wraps)."""
    key = f"detail_opt::{pid}"
    cur = st.session_state.get(key, 0)
    st.session_state[key] = (cur + delta) % max(n, 1)


def _grade_defaults(manifest: M.Manifest, photo: M.PhotoState) -> dict:
    """Per-product saved grade params, falling back to the classification preset."""
    worn = (photo.classification or "packshot") == "worn"
    base = {"strength": 0 if worn else 70, "whiten": True, "warmth": 0, "gold": 0, "cool": 0}
    base.update(manifest.product_grade_params.get(photo.product) or {})
    return base


def _apply_hero_preset(manifest: M.Manifest, photo: M.PhotoState, has_hero: bool) -> None:
    """The hero IS the grading preset: setting one turns colour-matching on (strength
    up for packshots; worn stays background-only by design), clearing it turns it off.
    Updates the product's saved grade params AND the live slider widget so the panel
    reflects it immediately. Caller saves + reruns."""
    worn = (photo.classification or "packshot") == "worn"
    d = _grade_defaults(manifest, photo)
    d["strength"] = (0 if worn else 70) if has_hero else 0
    manifest.product_grade_params[photo.product] = {
        "strength": int(d["strength"]), "whiten": bool(d["whiten"]),
        "warmth": int(d["warmth"]), "gold": int(d["gold"]), "cool": int(d["cool"]),
    }
    st.session_state[f"gstr_{photo.photo_id}"] = int(d["strength"])  # move the slider now


def _current_grade_params(manifest: M.Manifest, photo: M.PhotoState) -> dict:
    """Live grade params for this photo, read from the slider widget keys (which hold
    the current values at the START of the run, so the preview above the sliders
    reflects them without a one-rerun lag), falling back to the product default."""
    d = _grade_defaults(manifest, photo)
    pid = photo.photo_id
    return {
        "strength": st.session_state.get(f"gstr_{pid}", d["strength"]),
        "warmth": st.session_state.get(f"gwarm_{pid}", d["warmth"]),
        "whiten": st.session_state.get(f"gwhite_{pid}", d["whiten"]),
        "gold": st.session_state.get(f"ggold_{pid}", d["gold"]),
        "cool": st.session_state.get(f"gcool_{pid}", d["cool"]),
    }


@st.cache_data(show_spinner=False, max_entries=64)
def _cached_preview(variant_local: str, mtime: float, classification: str,
                    hero_local: str | None, params_key: tuple, max_edge: int = 1100) -> bytes:
    """Grade a DOWNSCALED copy of the variant in-memory for a fast live preview (not
    saved). Cached by (path, mtime, classification, hero, params) so it only recomputes
    when something actually changes."""
    import dataclasses
    import numpy as np
    from src.grader import grade_image as _gi, Settings, PACKSHOT_PRESET, WORN_PRESET
    img = Image.open(variant_local).convert("RGB")
    if max(img.size) > max_edge:
        s = max_edge / max(img.size)
        img = img.resize((int(img.size[0] * s), int(img.size[1] * s)), Image.LANCZOS)
    buf = BytesIO(); img.save(buf, format="PNG"); src = buf.getvalue()
    hero_rgb = None
    if hero_local:
        try:
            hero_rgb = np.array(Image.open(hero_local).convert("RGB"))
        except Exception:
            hero_rgb = None
    strength, whiten, warmth, gold, cool = params_key
    base = WORN_PRESET if classification == "worn" else PACKSHOT_PRESET
    settings = dataclasses.replace(
        base, strength=strength / 100.0, bg_normalize=bool(whiten),
        bg_warmth=float(warmth), gold_sat=float(gold), diamond_cool=float(cool),
    )
    return _gi(src, classification, hero_rgb=hero_rgb, custom_settings=settings)


def render_gallery_tile(photo: M.PhotoState, variant_path: str) -> None:
    """One gallery tile = one kept variant. Shows the graded output if the variant
    has been graded, else the raw variant. Hover-light controls: open detail, grade,
    delete. (Phase D will refine the affordances/icons.)"""
    disp = photo.display_for(variant_path)
    is_graded = photo.is_graded(variant_path)
    vname = Path(variant_path).name
    vkey = f"{photo.photo_id}::{vname}"

    gkey = f"{photo.photo_id}::{vname}"
    gfut = st.session_state.get("pending_grades", {}).get(gkey)
    is_grading = gfut is not None and not gfut.done()

    with st.container(key=f"boardimg-{vkey}"):
        st.image(_board_thumb(disp), use_container_width=True)

    with st.container(key=f"boardctrls-{vkey}"):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.button("⤢", key=f"open::{vkey}", use_container_width=True,
                      help="Open detail view",
                      on_click=_cb_open_detail_variant, args=(photo.photo_id,))
        with c2:
            if is_grading:
                st.button("…", key=f"grd::{vkey}", use_container_width=True,
                          disabled=True, help="Grading…")
            elif is_graded:
                st.button("✓", key=f"grd::{vkey}", use_container_width=True,
                          help="Graded — click to re-grade against the current hero",
                          on_click=_cb_grade_variant, args=(photo.photo_id, variant_path))
            else:
                st.button("◧", key=f"grd::{vkey}", use_container_width=True,
                          disabled=not cfg.anthropic_api_key and False,  # grading is local
                          help="Grade — colour-match to the hero",
                          on_click=_cb_grade_variant, args=(photo.photo_id, variant_path))
        with c3:
            st.button("✕", key=f"del::{vkey}", use_container_width=True,
                      help="Delete this variant (reversible — Undo at the top)",
                      on_click=_cb_hide_variant, args=(photo.photo_id, variant_path))

    badge = ("<span class='badge badge-ready'>✓ graded</span>" if is_graded
             else f"<span class='subtle' style='font-size:11px;'>{vname}</span>")
    st.markdown(f"<div style='margin-top:2px;'>{badge}</div>", unsafe_allow_html=True)


def render_input_tile(photo: M.PhotoState) -> None:
    """The pinned 3D-render reference at the far left of a carousel row."""
    with st.container(key=f"boardimg-input-{photo.photo_id}"):
        st.image(_board_thumb(photo.input_path), use_container_width=True)
    st.button("⤢ 3D render", key=f"openinput::{photo.photo_id}", use_container_width=True,
              help="Open this render's detail view",
              on_click=_cb_open_detail_variant, args=(photo.photo_id,))


def _cb_carousel(pid: str, delta: int, n_slots: int, n_total: int) -> None:
    key = f"carousel::{pid}"
    cur = st.session_state.get(key, 0)
    max_off = max(0, n_total - n_slots)
    st.session_state[key] = min(max(cur + delta, 0), max_off)


def render_carousel_row(photo: M.PhotoState, cols_per_row: int) -> None:
    """One render's row: [‹] [input 3D render] [variant tiles…] [›]. The variants are
    a horizontal carousel — arrows page through them when there are more than fit."""
    variants = photo.kept_variants
    n_slots = max(1, cols_per_row - 1)            # variant slots beside the pinned input
    off_key = f"carousel::{photo.photo_id}"
    off = min(max(st.session_state.get(off_key, 0), 0), max(0, len(variants) - n_slots))
    window = variants[off:off + n_slots]
    can_prev, can_next = off > 0, off < max(0, len(variants) - n_slots)

    cols = st.columns([0.3] + [1] * (1 + n_slots) + [0.3],
                      gap="small", vertical_alignment="center")
    with cols[0]:
        if can_prev:
            st.button("‹", key=f"cprev::{photo.photo_id}", use_container_width=True,
                      help="Previous variants",
                      on_click=_cb_carousel, args=(photo.photo_id, -1, n_slots, len(variants)))
    with cols[1]:
        render_input_tile(photo)
    for j, v in enumerate(window):
        with cols[2 + j]:
            render_gallery_tile(photo, v)
    with cols[-1]:
        if can_next:
            st.button("›", key=f"cnext::{photo.photo_id}", use_container_width=True,
                      help="More variants",
                      on_click=_cb_carousel, args=(photo.photo_id, 1, n_slots, len(variants)))


def render_board_page():
    project: PROJ.Project | None = st.session_state.get("project")
    manifest: M.Manifest | None = st.session_state.get("manifest")
    if not project or not manifest:
        set_route("landing")
        st.rerun()
        return

    # Reap completed futures so newly-finished photos show their variants/output
    pending = st.session_state.get("pending_regens", {})
    done = [pid for pid, f in pending.items() if f.done()]
    for pid in done:
        try:
            pending[pid].result()
        except Exception:
            pass
        del pending[pid]
    pending_p = st.session_state.get("pending_prompts", {})
    done = [pid for pid, f in pending_p.items() if f.done()]
    for pid in done:
        try:
            pending_p[pid].result()
        except Exception:
            pass
        del pending_p[pid]
    _reap_pending_grades()

    # ── Header: project + key stats + primary action
    n_total = len(manifest.photos)
    n_visuals = sum(len(p.kept_variants) for p in manifest.photos.values())
    n_graded = sum(len(p.graded_variants or {}) for p in manifest.photos.values())
    pending_count = sum(1 for p in manifest.photos.values() if not p.variants)

    h1, h2 = st.columns([5, 2])
    with h1:
        st.markdown(
            f"<div style='font-size:22px;font-weight:600;letter-spacing:-0.01em;'>{project.name}</div>"
            f"<div class='row-wrap' style='margin-top:6px;font-size:12px;color:var(--text-3);'>"
            f"<span><b style='color:var(--text);font-variant-numeric:tabular-nums;'>{n_total}</b> renders</span>"
            f"<span style='color:var(--text-disabled);margin:0 2px;'>·</span>"
            f"<span><b style='color:var(--text);font-variant-numeric:tabular-nums;'>{n_visuals}</b> visuals</span>"
            f"<span style='color:var(--text-disabled);margin:0 2px;'>·</span>"
            f"<span><b style='color:var(--text);font-variant-numeric:tabular-nums;'>{n_graded}</b> graded</span>"
            f"<span style='color:var(--text-disabled);margin:0 2px;'>·</span>"
            f"<span><b style='color:var(--text);font-variant-numeric:tabular-nums;'>${manifest.total_cost_usd:.2f}</b> spent</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    with h2:
        if pending_count:
            label = f"▶  Auto-prepare {pending_count} pending"
            if st.button(label, type="primary", use_container_width=True,
                         help=f"Generate prompts + {st.session_state.get('n_variants_slider', cfg.default_n)} variants "
                              f"per photo for the {pending_count} photo(s) not yet processed."):
                for photo in manifest.photos.values():
                    if photo.variants:
                        continue
                    existing = pending.get(photo.photo_id)
                    if existing is not None and not existing.done():
                        continue
                    fut = P.submit_regenerate(
                        cfg, manifest, photo,
                        get_anthropic_client(cfg.anthropic_api_key), get_gemini_client(cfg.gemini_api_key),
                        n=st.session_state.get("n_variants_slider", cfg.default_n),
                    )
                    pending[photo.photo_id] = fut
                st.rerun()
        else:
            st.markdown(
                "<div style='display:flex;justify-content:flex-end;align-items:center;"
                "height:34px;font-size:13px;color:var(--success);'>"
                "✓ All photos prepared</div>",
                unsafe_allow_html=True,
            )

    # ── Project-wide: re-analyze worn prompts with the current prompt engineering.
    # Existing worn photos cache their analyzer template; this clears+re-runs it so
    # the whole project picks up prompt changes (concise style, soft-pink lips,
    # brand elevation) at once. Prompts only — does not regenerate images.
    worn_photos = [p for p in manifest.photos.values()
                   if (p.classification or "packshot") == "worn"]
    if worn_photos:
        n_worn = len(worn_photos)
        n_worn_running = sum(
            1 for p in worn_photos
            if (pending_p.get(p.photo_id) is not None and not pending_p[p.photo_id].done())
        )
        with st.expander(f"🔄 Refresh worn prompts ({n_worn})", expanded=False):
            st.caption(
                "Re-runs the analyzer on every worn shot with the latest prompt engineering "
                "(concise style, soft-pink lips, Tiffany / Bulgari / Louis Vuitton elevation), "
                "keeping each photo's styling parameters. Updates the prompts only — it does "
                "not regenerate images (regenerate per photo, or Auto-prepare, when ready). "
                "≈ $0.02 per shot."
            )
            if n_worn_running:
                st.caption(f"⚙ {n_worn_running} worn prompt(s) re-analyzing…")
            if st.button(
                f"Re-analyze all {n_worn} worn prompts",
                key="refresh_worn_prompts",
                disabled=(not cfg.anthropic_api_key) or n_worn_running > 0,
                use_container_width=True,
            ):
                pp = st.session_state.setdefault("pending_prompts", {})
                for photo in worn_photos:
                    ex = pp.get(photo.photo_id)
                    if ex is not None and not ex.done():
                        continue
                    pp[photo.photo_id] = P.submit_generate_prompt(
                        cfg, manifest, photo, get_anthropic_client(cfg.anthropic_api_key)
                    )
                st.rerun()

    # Subtle hint about navigation (shown once at the top, not per card)
    st.markdown(
        "<div style='font-size:12px;color:var(--text-3);margin-top:4px;"
        "padding:6px 10px;background:var(--bg-elev-1);border-radius:6px;"
        "border:1px solid var(--border-subtle);display:inline-block;'>"
        "💡 Use <b>‹ ›</b> to flip through variants. "
        "Click <b>⤢</b> to open the full detail view and compare input vs output."
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Project-wide grading reference (hero) ─────────────────────────────
    # Packshots colour-match to this hero (strength 0.7). Per-product overrides
    # (set on a photo's detail page) take precedence for that product.
    with st.expander("🎯 Project grading reference (hero)", expanded=False):
        if manifest.hero_path and _exists_cached(manifest.hero_path):
            st.markdown(f"**Current project hero:** `{ST.name(manifest.hero_path)}`")
            st.image(_board_thumb(manifest.hero_path, max_edge=180), width=180)
        else:
            st.caption("No project hero set — packshots are background-normalised only "
                       "unless a product has its own hero.")
        hu = st.file_uploader(
            "Upload a project hero reference",
            type=["png", "jpg", "jpeg", "webp"],
            key="proj_hero_upload",
        )
        if hu is not None:
            marker = "__proj_heroup_done"
            if st.session_state.get(marker) != hu.file_id:
                tgt = ST.join(manifest.output_root, ".hero", hu.name)
                ST.write_bytes(tgt, hu.getvalue())
                manifest.hero_path = tgt
                manifest.hero_photo_id = None
                M.save(manifest, ST)
                st.session_state[marker] = hu.file_id
                st.success(f"Project hero set: {hu.name}")
                st.rerun()
        if manifest.hero_path and st.button("✖ Clear project hero", key="clear_proj_hero"):
            manifest.hero_path = None
            manifest.hero_photo_id = None
            M.save(manifest, ST)
            st.rerun()

    # ── Group photos by product folder (preserve project order)
    products: dict[str, list[M.PhotoState]] = {}
    for photo in manifest.photos.values():
        products.setdefault(photo.product, []).append(photo)
    # Order by project's product order, then alphabetical for any extras
    ordered_keys: list[str] = []
    if project.products:
        for pg in project.products:
            if pg.folder_name in products:
                ordered_keys.append(pg.folder_name)
    for k in sorted(products.keys()):
        if k not in ordered_keys:
            ordered_keys.append(k)

    card_size = st.session_state.get("board_card_size", "Medium")
    cols_per_row = _board_cols_for_size(card_size)

    # ── Gallery: one tile per KEPT variant, grouped by product. The scroll now
    # reflects the generated/graded visuals (not one card per input render).
    # Undo for the last soft-deleted variant.
    last_hidden = st.session_state.get("__last_hidden")
    if last_hidden:
        uc1, uc2 = st.columns([4, 1])
        with uc2:
            if st.button("↶ Undo delete", key="undo_hide", use_container_width=True):
                pid, vpath = last_hidden
                if pid in manifest.photos:
                    P.unhide_variant(cfg, manifest, manifest.photos[pid], vpath)
                st.session_state.pop("__last_hidden", None)
                st.rerun()

    # Warm persisted thumbnails (≈30 KB each) for every kept visual AND the pinned
    # input renders in one parallel batch — the main lever that makes open fast.
    _prefetch_thumbs(
        [ph.input_path for ph in manifest.photos.values() if ph.kept_variants]
        + [ph.display_for(v) for ph in manifest.photos.values() for v in ph.kept_variants]
    )

    for product_name in ordered_keys:
        photos = sorted(products[product_name], key=lambda p: Path(p.input_path).name.lower())
        pg = project.find_product(product_name) if project else None
        is_worn = (pg.is_worn if pg else (photos[0].classification == "worn" if photos else False))
        tiles = [(ph, v) for ph in photos for v in ph.kept_variants]
        n_graded_p = sum(1 for ph, v in tiles if ph.is_graded(v))
        n_pending_p = sum(1 for ph in photos if not ph.variants)

        cls_class = "badge-worn" if is_worn else "badge-packshot"
        has_override = bool(manifest.product_heroes.get(product_name))
        hero_badge = (
            "<span class='badge' style='background:var(--accent-muted);color:var(--accent);"
            "border-color:rgba(212,168,87,0.28);' "
            "title='This product uses its own grading hero (overrides the project default)'>"
            "★ own hero</span>"
            if has_override else ""
        )
        pend_txt = f" · {n_pending_p} pending" if n_pending_p else ""
        st.markdown(
            f"<div style='margin-top:32px;margin-bottom:10px;'>"
            f"<div class='row-wrap' style='margin-bottom:4px;'>"
            f"<span style='font-size:17px;font-weight:600;'>{product_name}</span>"
            f"<span class='badge {cls_class}'>{'worn' if is_worn else 'packshot'}</span>"
            f"{hero_badge}"
            f"<span class='subtle' style='font-size:12px;font-variant-numeric:tabular-nums;'>"
            f"{len(tiles)} visuals · {n_graded_p} graded{pend_txt}"
            f"</span>"
            f"</div>"
            + (f"<div class='subtle' style='font-size:13px;font-style:italic;'>"
               f"{pg.description}</div>" if pg and pg.description else "")
            + "</div>",
            unsafe_allow_html=True,
        )

        # One carousel row per render that has kept variants: the input 3D render is
        # pinned on the far left, then that render's variants, with ‹ › arrows on
        # each side to page through them.
        rows = [ph for ph in photos if ph.kept_variants]
        if not rows:
            st.markdown(
                "<div class='subtle' style='font-size:13px;padding:8px 0;'>"
                "No visuals yet — use <b>Auto-prepare</b> above to generate.</div>",
                unsafe_allow_html=True,
            )
        for ph in rows:
            render_carousel_row(ph, cols_per_row)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    # ── Autorefresh while any background work is running (regen, prompt, grade)
    # The 1.5s tick is fine when the user is passive; while they're INTERACTING
    # with buttons it can race the click handler. We slow the tick down to 3s
    # which is the longest the user reasonably waits to see new variants
    # appear, while leaving enough headroom that any synchronous handler
    # (◀/▶/⤢/Pick) lands cleanly between ticks.
    pending_g = st.session_state.get("pending_grades", {})
    n_pending = sum(1 for f in pending.values() if not f.done())
    n_pending += sum(1 for f in pending_p.values() if not f.done())
    n_pending += sum(1 for f in pending_g.values() if not f.done())
    if n_pending:
        st_autorefresh(interval=3000, key="board_poll", limit=None)
        st.caption(f"⚙ {n_pending} background task(s) running…")


# ═══ Route: DETAIL (per-photo card; click-from-board) ════════════════════

def _render_product_hero_controls(manifest: M.Manifest, photo: M.PhotoState) -> None:
    """Per-product grading reference (hero) controls, shown on the detail page.

    The active hero for a photo is resolved per-product override → project-wide
    default → none (see pipeline.hero_path_for_photo). This block lets the user
    set the product override from the current graded output, upload an external
    reference, or clear the override so the product falls back to the project hero.
    """
    product = photo.product
    with st.expander("🎯 Grading reference (hero)", expanded=False):
        st.caption(
            "Packshots colour-match to this hero (strength 0.7). Worn shots are "
            "background-normalised only. A per-product hero overrides the project hero."
        )
        per_prod = manifest.product_heroes.get(product)
        if per_prod:
            active = f"product override · `{ST.name(per_prod)}`"
        elif manifest.hero_path:
            active = f"project hero · `{ST.name(manifest.hero_path)}`"
        else:
            active = "none — background-only grading"
        st.markdown(f"**Active for `{product}`:** {active}")

        effective = P.hero_path_for_photo(cfg, manifest, photo)
        if effective and _exists_cached(effective):
            st.image(_board_thumb(effective, max_edge=180), width=180)

        # Most-recent graded output for this photo (if any) → can become the hero.
        graded_out = next(reversed(list(photo.graded_variants.values())), None) if photo.graded_variants else None
        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                f"📌 Use a graded output as hero for this product",
                key=f"prodhero_{photo.photo_id}",
                use_container_width=True,
                disabled=not graded_out,
                help="Grade a variant first" if not graded_out
                     else "Set this product's grading reference to a graded output",
            ):
                name = ST.name(graded_out)
                tgt = ST.join(manifest.output_root, ".hero", "products", product, name)
                ST.write_bytes(tgt, ST.read_bytes(graded_out))
                manifest.product_heroes[product] = tgt
                M.save(manifest, ST)
                st.success(f"Product hero set: {name}")
                st.rerun()
        with c2:
            if st.button(
                "✖ Clear product override",
                key=f"clrhero_{photo.photo_id}",
                use_container_width=True,
                disabled=not per_prod,
                help="Fall back to the project-wide hero" if per_prod else "No override set",
            ):
                manifest.product_heroes.pop(product, None)
                M.save(manifest, ST)
                st.rerun()

        up = st.file_uploader(
            f"Upload a hero reference for `{product}`",
            type=["png", "jpg", "jpeg", "webp"],
            key=f"heroup_{photo.photo_id}",
        )
        if up is not None:
            marker = f"__heroup_done_{photo.photo_id}"
            if st.session_state.get(marker) != up.file_id:
                tgt = ST.join(manifest.output_root, ".hero", "products", product, up.name)
                ST.write_bytes(tgt, up.getvalue())
                manifest.product_heroes[product] = tgt
                M.save(manifest, ST)
                st.session_state[marker] = up.file_id
                st.success(f"Uploaded {up.name} as hero for {product}")
                st.rerun()


def _render_worn_product_ref_controls(manifest: M.Manifest, photo: M.PhotoState) -> None:
    """Worn shots only: pick a generated packshot as the SECOND Gemini reference.

    A product folder is classified as a whole, so the packshot of the same physical
    product usually lives in a different (packshot) folder. We therefore offer every
    graded packshot across the project. The chosen output is fed as Image 2 on the
    next regenerate (see pipeline.generate_variants_for). Falls back to the static
    product-ref files when nothing is picked.
    """
    # Candidate generated packshots: each GRADED output of a packshot-classified photo.
    candidates: list[tuple[M.PhotoState, str]] = []
    for p in manifest.photos.values():
        if (p.classification or "packshot") == "packshot":
            for gpath in (p.graded_variants or {}).values():
                candidates.append((p, gpath))
    candidates.sort(key=lambda t: (t[0].photo_id, t[1]))

    with st.expander("🧩 Product reference (generated packshot)", expanded=False):
        st.caption(
            "Worn renders use a generated packshot of the product as the second "
            "reference so the jewellery stays accurate. Grade a packshot first, then "
            "pick it here. Without a pick, the static product-ref files are used."
        )
        if not candidates:
            st.info("No graded packshots yet — grade a packshot photo, then pick it here.")
            return

        def _cand_label(p: M.PhotoState, gpath: str) -> str:
            # Lead with the product description so the user picks by what the product
            # IS, not by an opaque timestamp filename.
            desc = (manifest.product_briefs.get(p.product) or "").strip()
            name = ST.name(gpath)
            if desc:
                short = desc if len(desc) <= 70 else desc[:69].rstrip() + "…"
                return f"{short}  ·  {p.product}/{name}"
            return f"{p.product}/{name}"

        labels = ["— none (use static product refs) —"] + [_cand_label(p, g) for p, g in candidates]
        paths = [None] + [g for _p, g in candidates]
        try:
            cur_idx = paths.index(photo.product_ref_path) if photo.product_ref_path in paths else 0
        except ValueError:
            cur_idx = 0
        choice = st.selectbox(
            "Generated packshot to reference",
            options=list(range(len(labels))),
            index=cur_idx,
            format_func=lambda i: labels[i],
            key=f"wornref_{photo.photo_id}",
        )
        chosen = paths[choice]
        if chosen != photo.product_ref_path:
            photo.product_ref_path = chosen
            M.save(manifest, ST)

        if photo.product_ref_path and _exists_cached(photo.product_ref_path):
            st.image(_board_thumb(photo.product_ref_path, max_edge=180), width=180)
            st.caption("This packshot is fed as Image 2 on the next ⟳ Regenerate.")


def _render_worn_styling_controls(manifest: M.Manifest, photo: M.PhotoState) -> None:
    """Worn shots only: pick the six styling parameters (skin, makeup, hair, wardrobe).

    These fill the {PLACEHOLDER}s in the render's analyzed prompt template. They are
    randomized at auto-prepare and editable here; changing them re-assembles the
    prompt (no re-analysis, no analyzer API cost) and regenerates variants.
    """
    thumbs = WP.thumbs_dir(cfg.root / "prompts")
    current = WP.normalize(photo.worn_params)

    with st.expander("🎨 Styling parameters (worn render)", expanded=False):
        st.caption(
            "Skin, makeup, hair, and wardrobe are parametrized — these fill the "
            "render's analyzed prompt. Randomized on auto-prepare; edit and click "
            "**Apply & Regenerate**. Changing them never re-analyzes the render."
        )

        new_sel: dict[str, str] = {}
        for param in WP.PARAMS:
            keys = WP.option_keys(param)
            cur = current[param]
            idx = keys.index(cur) if cur in keys else 0
            csel, cthumb = st.columns([3, 1])
            with csel:
                chosen = st.selectbox(
                    WP.PARAM_LABELS[param],
                    options=keys,
                    index=idx,
                    format_func=lambda k, p=param: WP.label(p, k),
                    key=f"wp_{param}_{photo.photo_id}",
                )
            with cthumb:
                tf = WP.thumb_filename(param, chosen)
                tp = thumbs / tf if tf else None
                if tp and tp.exists():
                    st.image(str(tp), width=58)
            new_sel[param] = chosen

        changed = new_sel != current

        b1, b2 = st.columns(2)
        with b1:
            if st.button("🎲 Randomize", key=f"wp_rand_{photo.photo_id}",
                         use_container_width=True,
                         help="Pick a fresh random combination"):
                rnd = WP.random_params()
                photo.worn_params = rnd
                photo.prompt = None  # force re-assembly from the cached template
                # Sync the selectbox widget state so the dropdowns reflect the new pick.
                for p in WP.PARAMS:
                    st.session_state[f"wp_{p}_{photo.photo_id}"] = rnd[p]
                M.save(manifest, ST)
                st.rerun()
        with b2:
            if st.button("✨ Apply & Regenerate", key=f"wp_apply_{photo.photo_id}",
                         type="primary", use_container_width=True,
                         disabled=(not cfg.gemini_api_key),
                         help="Re-assemble the prompt with these parameters and regenerate variants"):
                photo.worn_params = new_sel
                photo.prompt = None  # force re-assembly (template stays cached → no analyzer call)
                M.save(manifest, ST)
                _handle_regenerate(photo)

        if changed:
            st.caption("⚠ Unsaved styling changes — click **Apply & Regenerate** to use them.")


def _save_image_to_renders(manifest: M.Manifest, photo: M.PhotoState, src, stage: str) -> str:
    """Copy an image (input / variant / graded — any stage) into the product's render
    folder on storage: `<output_root>/<product>/renders/`. Returns the destination path.
    The filename keeps the source stem + stage + timestamp so nothing is overwritten."""
    from datetime import datetime
    data = _local(src).read_bytes()
    stem = Path(photo.input_path).stem
    ext = (Path(str(src)).suffix or ".png").lower()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{stem}__{stage}__{ts}{ext}"
    dest = ST.join(manifest.output_root, photo.product, "renders", name)
    ST.write_bytes(dest, data)
    return dest


def _save_button(manifest: M.Manifest, photo: M.PhotoState, src, stage: str, key: str) -> None:
    """A 💾 Save control that drops the given image into the product's render folder."""
    if st.button("💾 Save", key=key, use_container_width=True,
                 help=f"Save this {stage} image to the product's render folder"):
        try:
            dest = _save_image_to_renders(manifest, photo, src, stage)
            st.success(f"Saved → {ST.name(dest)}")
        except Exception as e:
            st.error(f"Save failed: {e}")


def render_photo_card(photo: M.PhotoState):
    """The full per-image lifecycle UI — moved behind a click on the board."""
    _sync_card_widgets_if_stale(photo)

    manifest: M.Manifest = st.session_state["manifest"]
    pending = st.session_state.get("pending_regens", {})
    fut = pending.get(photo.photo_id)
    if fut is not None and fut.done():
        try:
            fut.result()
        except Exception:
            pass
        del pending[photo.photo_id]
        _sync_card_widgets_if_stale(photo)
    is_regenerating = (fut is not None) and not fut.done()

    prompt_pending = st.session_state.get("pending_prompts", {})
    pfut = prompt_pending.get(photo.photo_id)
    if pfut is not None and pfut.done():
        try:
            pfut.result()
        except Exception:
            pass
        del prompt_pending[photo.photo_id]
        _sync_card_widgets_if_stale(photo)
    is_generating_prompt = (pfut is not None) and not pfut.done()

    # Grading futures — same reap pattern so the "Saved" badge appears as soon as the
    # background grade completes, without freezing the UI thread during the grade itself.
    grade_pending = st.session_state.get("pending_grades", {})
    gfut = grade_pending.get(photo.photo_id)
    if gfut is not None and gfut.done():
        try:
            gfut.result()
        except Exception:
            pass
        del grade_pending[photo.photo_id]
    is_grading = (gfut is not None) and not gfut.done()

    stem = Path(photo.input_path).stem

    # Warm this photo's images in parallel so the detail view's first paint doesn't
    # download input + variants + graded outputs one-by-one on the Dropbox backend.
    _prefetch_materialize(
        [photo.input_path]
        + list(photo.kept_variants)
        + list((photo.graded_variants or {}).values())
    )

    n_variants = st.session_state.get("n_variants_slider", cfg.default_n)

    container = st.container(border=True)
    with container:
        # Header
        h1, h2, h3, h4 = st.columns([4, 2, 2, 2])
        with h1:
            st.markdown(f"**{Path(photo.input_path).name}**")
            st.caption(f"`{photo.product}` · ${photo.cost_usd:.3f} spent")
        with h2:
            current_class = photo.classification or "packshot"
            new_class = st.selectbox(
                "Classification",
                ["packshot", "worn"],
                index=0 if current_class == "packshot" else 1,
                key=f"class_{photo.photo_id}",
                label_visibility="collapsed",
            )
            if new_class != current_class:
                photo.classification = new_class
                photo.user_overrode_classification = True
                M.save(manifest, ST)
        with h3:
            if is_grading:
                st.markdown("<span class='badge badge-running'>● Grading…</span>", unsafe_allow_html=True)
            elif is_regenerating:
                st.markdown("<span class='badge badge-running'>● Regenerating…</span>", unsafe_allow_html=True)
            elif is_generating_prompt:
                st.markdown("<span class='badge badge-running'>● Generating prompt…</span>", unsafe_allow_html=True)
            elif photo.graded:
                st.markdown("<span class='badge badge-ready'>✓ Saved</span>", unsafe_allow_html=True)
            elif photo.variants:
                st.markdown("<span class='badge'>● Pick one</span>", unsafe_allow_html=True)
            elif photo.prompt:
                st.markdown("<span class='badge'>● Prompt ready</span>", unsafe_allow_html=True)
            else:
                st.markdown("<span class='badge'>○ Pending</span>", unsafe_allow_html=True)
        with h4:
            if st.button("⟳ Regenerate", key=f"regen_top_{photo.photo_id}",
                         disabled=(not cfg.gemini_api_key) or is_regenerating,
                         use_container_width=True,
                         help="Already regenerating…" if is_regenerating else "Regenerate variants"):
                _handle_regenerate(photo, n_variants=n_variants)

        if photo.last_error:
            st.markdown(
                f"<span class='badge badge-error'>Error: {photo.last_error[:200]}</span>",
                unsafe_allow_html=True,
            )

        # ── Full-size option viewer: one variant at a time, keyboard ← → nav,
        #    compare-to-input, and a grade panel with parameters below.
        kept = photo.kept_variants
        if kept:
            opt_key = f"detail_opt::{photo.photo_id}"
            idx = min(max(st.session_state.get(opt_key, 0), 0), len(kept) - 1)
            st.session_state[opt_key] = idx
            vp = kept[idx]
            graded = photo.is_graded(vp)
            disp = photo.display_for(vp)
            gkey = f"{photo.photo_id}::{Path(vp).name}"
            gf = st.session_state.get("pending_grades", {}).get(gkey)
            v_grading = gf is not None and not gf.done()

            hero_resolved = P.hero_path_for_photo(cfg, manifest, photo)

            nav_l, nav_m, nav_r = st.columns([1, 8, 1], vertical_alignment="center")
            with nav_l:
                st.button("◀", key=f"optprev_{photo.photo_id}", use_container_width=True,
                          disabled=len(kept) < 2, help="Previous option (←)",
                          on_click=_cb_detail_opt, args=(photo.photo_id, -1, len(kept)))
            with nav_m:
                st.markdown(
                    f"<div style='text-align:center;font-weight:600;'>Option {idx + 1} / {len(kept)}"
                    f"<span class='subtle' style='font-weight:400;'>  ·  {Path(vp).name}"
                    f"{'  ·  ✓ graded' if graded else ''}</span></div>",
                    unsafe_allow_html=True,
                )
            with nav_r:
                st.button("▶", key=f"optnext_{photo.photo_id}", use_container_width=True,
                          disabled=len(kept) < 2, help="Next option (→)",
                          on_click=_cb_detail_opt, args=(photo.photo_id, 1, len(kept)))

            # Image LEFT, grade controls RIGHT — so the AD tunes and previews together.
            params = _current_grade_params(manifest, photo)
            img_col, grade_col = st.columns([2, 1.25], gap="large")

            with img_col:
                vc1, vc2 = st.columns(2)
                with vc1:
                    preview = st.checkbox("👁 Preview grade", key=f"prev_toggle_{photo.photo_id}",
                                          help="Show the current grade live on this image "
                                               "(not saved — use Apply to save the graded version)")
                with vc2:
                    compare = st.toggle("Compare with 3D render", key=f"cmp_toggle_{photo.photo_id}",
                                        help="Slide between the input render and this option")
                # Live-graded bytes when Preview is on (reused by both the plain view
                # and the compare overlay so they always show the same thing).
                preview_bytes = None
                if preview:
                    try:
                        loc = _local(vp)
                        mt = loc.stat().st_mtime
                        hero_loc = str(_local(hero_resolved)) if hero_resolved else None
                        pk = (int(params["strength"]), 1 if params["whiten"] else 0,
                              int(params["warmth"]), int(params["gold"]), int(params["cool"]))
                        with st.spinner("Previewing grade…"):
                            preview_bytes = _cached_preview(str(loc), mt,
                                                            photo.classification or "packshot", hero_loc, pk)
                    except Exception:
                        preview_bytes = None

                with st.container(key=f"optview-{photo.photo_id}"):
                    if compare:
                        from streamlit.components.v1 import html as _html
                        # When previewing, compare the GRADED preview against the render;
                        # else compare the saved/raw option. Left-anchored so the image
                        # stays put when toggling.
                        ov_url = _bytes_to_data_url(preview_bytes, 1100) if preview_bytes else None
                        _html(render_hover_card_html(
                                  str(photo.input_path),
                                  None if ov_url else str(disp),
                                  height_px=620, max_edge=1100,
                                  overlay_url=ov_url, align="flex-start"),
                              height=636, scrolling=False)
                    elif preview_bytes is not None:
                        st.image(preview_bytes)
                    else:
                        try:
                            st.image(str(_local(disp)))
                        except Exception:
                            st.caption("_(image unavailable)_")
                z1, z2 = st.columns(2)
                with z1:
                    if st.button("🔍 Zoom", key=f"optzoom_{photo.photo_id}", use_container_width=True,
                                 help="Open full-resolution with pan/zoom"):
                        _open_fullscreen(
                            [(str(photo.display_for(v)), Path(v).name) for v in kept], idx)
                with z2:
                    st.button("✕ Delete", key=f"optdel_{photo.photo_id}", use_container_width=True,
                              help="Delete this option (reversible — Undo on the board)",
                              on_click=_cb_hide_variant, args=(photo.photo_id, vp))
                _inject_arrow_key_nav()  # ← / → flip options (clicks the ◀ ▶ buttons above)

            with grade_col:
                # ── Grade panel — hero (the implied preset) first, then parameters;
                #    both feed the live preview on the left + Apply (save). Stacked
                #    single-column since this side panel is narrow.
                st.markdown("##### 🎚 Grade")
                st.markdown("**1 · Reference (hero)**")
                if hero_resolved and _exists_cached(hero_resolved):
                    st.image(_board_thumb(hero_resolved, max_edge=200), width=132)
                    st.caption(f"`{ST.name(hero_resolved)}` — colour-matched to this. "
                               f"The parameters are the preset it implies.")
                else:
                    st.caption("No hero — set one to colour-match the renders to it; "
                               "without one, grading only normalises the background.")
                hb1, hb2 = st.columns(2)
                with hb1:
                    if st.button("📌 Use this", key=f"usehero_{photo.photo_id}",
                                 use_container_width=True,
                                 help="Set this product's hero to the option on the left"):
                        name = ST.name(disp)
                        tgt = ST.join(manifest.output_root, ".hero", "products", photo.product, name)
                        ST.write_bytes(tgt, ST.read_bytes(disp))
                        manifest.product_heroes[photo.product] = tgt
                        _apply_hero_preset(manifest, photo, True)
                        M.save(manifest, ST)
                        st.rerun()
                with hb2:
                    if hero_resolved and st.button("✖ Clear", key=f"clrhero2_{photo.photo_id}",
                                                   use_container_width=True,
                                                   help="Remove this product's hero override"):
                        manifest.product_heroes.pop(photo.product, None)
                        _apply_hero_preset(manifest, photo, False)
                        M.save(manifest, ST)
                        st.rerun()
                hup = st.file_uploader("Upload a hero reference",
                                       type=["png", "jpg", "jpeg", "webp"],
                                       key=f"herofile_{photo.photo_id}",
                                       label_visibility="collapsed")
                if hup is not None:
                    _m = f"__herofile_done_{photo.photo_id}"
                    if st.session_state.get(_m) != hup.file_id:
                        tgt = ST.join(manifest.output_root, ".hero", "products",
                                      photo.product, hup.name)
                        ST.write_bytes(tgt, hup.getvalue())
                        manifest.product_heroes[photo.product] = tgt
                        _apply_hero_preset(manifest, photo, True)
                        st.session_state[_m] = hup.file_id
                        M.save(manifest, ST)
                        st.rerun()

                st.markdown("**2 · Parameters**")
                d = _grade_defaults(manifest, photo)
                st.slider("Colour-match strength", 0, 100, int(d["strength"]),
                          key=f"gstr_{photo.photo_id}",
                          help="How strongly to match the hero's colour (0 = none).")
                st.slider("Background warmth", -10, 10, int(d["warmth"]),
                          key=f"gwarm_{photo.photo_id}")
                st.slider("Gold saturation", -30, 30, int(d["gold"]),
                          key=f"ggold_{photo.photo_id}", help="Boost the warmth/richness of gold.")
                st.slider("Diamond cool", 0, 20, int(d["cool"]),
                          key=f"gcool_{photo.photo_id}", help="Cool down bright highlights (diamonds).")
                st.checkbox("Whiten background", value=bool(d["whiten"]),
                            key=f"gwhite_{photo.photo_id}")
                p = _current_grade_params(manifest, photo)
                if st.button("✓ Re-apply grade (save)" if graded else "Apply grade (save)",
                             key=f"applygrade_{photo.photo_id}", type="primary",
                             use_container_width=True, disabled=v_grading):
                    overrides = {
                        "strength": p["strength"] / 100.0,
                        "bg_normalize": bool(p["whiten"]),
                        "bg_warmth": float(p["warmth"]),
                        "gold_sat": float(p["gold"]),
                        "diamond_cool": float(p["cool"]),
                    }
                    manifest.product_grade_params[photo.product] = {
                        "strength": int(p["strength"]), "whiten": bool(p["whiten"]),
                        "warmth": int(p["warmth"]), "gold": int(p["gold"]), "cool": int(p["cool"]),
                    }
                    M.save(manifest, ST)
                    _cb_grade_variant(photo.photo_id, vp, overrides)
                    st.rerun()
                st.caption("Tick **👁 Preview grade** to see changes live; **Apply** saves the graded version.")
                if v_grading:
                    st.caption("⚙ Grading…")
        elif photo.variants:
            st.caption("_(all options deleted — Undo on the board, or Regenerate)_")
        else:
            st.caption("_(no options yet — click Regenerate)_")

        # ── Styling & notes (below the viewer; hero lives in the grade panel above) ──
        if (photo.classification or "packshot") == "worn":
            _render_worn_styling_controls(manifest, photo)
            _render_worn_product_ref_controls(manifest, photo)
        new_notes = st.text_input(
            "Brief notes (per-photo override; product description from project is used otherwise)",
            value=photo.brief_notes or "",
            key=f"notes_{photo.photo_id}",
            placeholder=(
                "e.g. 'Skyline ring — three asscher diamonds, central larger'"
                if photo.classification != "worn"
                else "e.g. 'Necklace worn — paperclip pendant, hand at throat, taupe V-neck'"
            ),
        )
        if new_notes != (photo.brief_notes or ""):
            photo.brief_notes = new_notes
            M.save(manifest, ST)

        # Prompt
        st.markdown("##### Prompt sent to Nano Banana")
        new_prompt = st.text_area(
            "prompt",
            value=photo.prompt or "",
            height=220,
            key=f"prompt_{photo.photo_id}",
            label_visibility="collapsed",
            placeholder="(empty — click Generate prompt below)",
        )
        if new_prompt != (photo.prompt or ""):
            photo.prompt = new_prompt
            M.save(manifest, ST)
        if photo.prompt:
            _render_prompt_compliance(photo)

        # ── Refine prompt with Claude (comment + optional reference image) ──
        with st.expander("💬 Refine the prompt with feedback", expanded=False):
            st.caption("Tell Claude what to change and it rewrites the prompt (worn shots "
                       "keep their styling dials + 1:1 framing). Then Regenerate. An "
                       "uploaded image guides Claude's wording only — it is not sent to "
                       "Nano Banana.")
            fb = st.text_area(
                "What should change?", key=f"refinefb_{photo.photo_id}", height=90,
                placeholder="e.g. 'tighter crop on the ear', 'warmer studio light', "
                            "'hand resting lower on the jaw', 'thinner hoop'",
                label_visibility="collapsed",
            )
            rimgs = st.file_uploader("Reference image(s) — optional",
                                     type=["png", "jpg", "jpeg", "webp"],
                                     accept_multiple_files=True,
                                     key=f"refineimg_{photo.photo_id}")
            if st.button("💬 Refine prompt", key=f"refinebtn_{photo.photo_id}",
                         type="primary", use_container_width=True,
                         disabled=(not cfg.anthropic_api_key) or not fb.strip()
                                  or is_generating_prompt):
                ref_paths = []
                for up in (rimgs or []):
                    rp = ST.join(manifest.output_root, ".refine_refs", photo.product, stem, up.name)
                    ST.write_bytes(rp, up.getvalue())
                    ref_paths.append(rp)
                pending_p = st.session_state.setdefault("pending_prompts", {})
                pending_p[photo.photo_id] = P.submit_refine_prompt(
                    cfg, manifest, photo, get_anthropic_client(cfg.anthropic_api_key),
                    fb.strip(), ref_paths)
                st.rerun()

        # Actions
        a1, a2, a3 = st.columns([1, 1, 2])
        with a1:
            if st.button("🧠 (Re)generate prompt", key=f"genprompt_{photo.photo_id}",
                         disabled=(not cfg.anthropic_api_key) or is_generating_prompt,
                         use_container_width=True):
                try:
                    pending_p = st.session_state.setdefault("pending_prompts", {})
                    existing = pending_p.get(photo.photo_id)
                    if existing is not None and not existing.done():
                        st.toast(f"Prompt already generating for {photo.photo_id}.", icon="⏳")
                    else:
                        pfut = P.submit_generate_prompt(cfg, manifest, photo, get_anthropic_client(cfg.anthropic_api_key))
                        pending_p[photo.photo_id] = pfut
                        st.rerun()
                except Exception as e:
                    st.error(str(e))
        with a2:
            if st.button("⟳ Regenerate variants", key=f"regen_{photo.photo_id}",
                         disabled=(not cfg.gemini_api_key) or (not photo.prompt) or is_regenerating,
                         use_container_width=True):
                _handle_regenerate(photo, n_variants=n_variants)
        with a3:
            with st.expander("⤓ Swap reference image", expanded=False):
                ref_upload = st.file_uploader(
                    "Upload a different reference image",
                    type=["png", "jpg", "jpeg", "webp"],
                    key=f"swapref_{photo.photo_id}",
                )
                if ref_upload and st.button("Apply + regenerate",
                                            key=f"applyref_{photo.photo_id}",
                                            use_container_width=True):
                    tmp_path = ST.join(
                        manifest.output_root, ".swap_refs", photo.product, stem, ref_upload.name
                    )
                    ST.write_bytes(tmp_path, ref_upload.getvalue())
                    _handle_regenerate(photo, reference_override=tmp_path, n_variants=n_variants)


def _handle_regenerate(
    photo: M.PhotoState,
    reference_override: Path | None = None,
    n_variants: int | None = None,
):
    manifest: M.Manifest = st.session_state["manifest"]
    try:
        pending = st.session_state.setdefault("pending_regens", {})
        existing = pending.get(photo.photo_id)
        if existing is not None and not existing.done():
            st.toast(f"Already regenerating {photo.photo_id}.", icon="⏳")
            return
        fut = P.submit_regenerate(
            cfg, manifest, photo,
            get_anthropic_client(cfg.anthropic_api_key), get_gemini_client(cfg.gemini_api_key),
            reference_override=reference_override,
            n=n_variants or st.session_state.get("n_variants_slider", cfg.default_n),
        )
        pending[photo.photo_id] = fut
        st.rerun()
    except Exception as e:
        st.error(f"Regenerate failed: {e}")


def _handle_select(photo: M.PhotoState, variant_path: str):
    """Enqueue a grade in the background pool. UI returns immediately; badge shows
    progress; autorefresh reaps the future and updates the board card to show the
    graded output."""
    manifest: M.Manifest = st.session_state["manifest"]
    pending = st.session_state.setdefault("pending_grades", {})
    existing = pending.get(photo.photo_id)
    if existing is not None and not existing.done():
        st.toast(f"Already grading {photo.photo_id}…", icon="⏳")
        return
    try:
        fut = P.submit_grade(cfg, manifest, photo, variant_path)
        pending[photo.photo_id] = fut
        # Optimistically update the board's displayed variant to track the user's pick
        if variant_path in photo.variants:
            _set_board_variant(photo, photo.variants.index(variant_path))
        st.rerun()
    except Exception as e:
        st.error(f"Grading failed to start: {e}")


def render_detail_page():
    project: PROJ.Project | None = st.session_state.get("project")
    manifest: M.Manifest | None = st.session_state.get("manifest")
    photo_id: str | None = st.session_state.get("detail_photo_id")
    if not project or not manifest or not photo_id or photo_id not in manifest.photos:
        set_route("board")
        st.rerun()
        return
    photo = manifest.photos[photo_id]

    nav_l, nav_t, nav_r = st.columns([1, 6, 1])
    with nav_l:
        if st.button("◀ Board", use_container_width=True, key="back_to_board"):
            set_route("board")
            st.rerun()
    with nav_t:
        st.markdown(f"### {photo_id}")
    with nav_r:
        # Prev/next within the same product folder
        same_product = [p for p in manifest.photos.values() if p.product == photo.product]
        same_product.sort(key=lambda p: Path(p.input_path).name.lower())
        cur_idx = next((i for i, p in enumerate(same_product) if p.photo_id == photo.photo_id), 0)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("‹ prev", use_container_width=True,
                         disabled=(cur_idx == 0), key="detail_prev"):
                st.session_state["detail_photo_id"] = same_product[cur_idx - 1].photo_id
                st.rerun()
        with c2:
            if st.button("next ›", use_container_width=True,
                         disabled=(cur_idx >= len(same_product) - 1), key="detail_next"):
                st.session_state["detail_photo_id"] = same_product[cur_idx + 1].photo_id
                st.rerun()

    render_photo_card(photo)

    # Refresh ticker while pending work runs (regen / prompt / grade)
    _pending = st.session_state.get("pending_regens", {})
    _pending_p = st.session_state.get("pending_prompts", {})
    _pending_g = st.session_state.get("pending_grades", {})
    if (
        any(not f.done() for f in _pending.values())
        or any(not f.done() for f in _pending_p.values())
        or any(not f.done() for f in _pending_g.values())
    ):
        st_autorefresh(interval=1500, key="detail_poll", limit=None)


# ═══ URL state (refresh restores the same view) ══════════════════════════

def _restore_from_url() -> None:
    """On a fresh page load (e.g. browser refresh) the session is empty, so rebuild
    it from the URL query params and land on the same board/detail view instead of
    bouncing to the landing page."""
    if st.session_state.get("project") is not None:
        return  # state already present this session
    slug = st.query_params.get("project")
    if not slug:
        return
    try:
        proj = PROJ.load_project(cfg, slug)
        if not proj:
            return
        st.session_state["project"] = proj
        st.session_state["manifest"] = _load_manifest_for_project(proj)
        route = st.query_params.get("route") or "board"
        st.session_state["route"] = route if route in ("board", "detail") else "board"
        photo = st.query_params.get("photo")
        if photo and st.session_state["route"] == "detail":
            st.session_state["detail_photo_id"] = photo
    except Exception:
        # Bad/stale URL → fall back to landing cleanly.
        st.query_params.clear()


def _sync_url() -> None:
    """Reflect the current view in the URL so a refresh can restore it."""
    proj = st.session_state.get("project")
    route = current_route()
    if proj is not None and route in ("board", "detail"):
        desired = {"project": proj.slug, "route": route}
        if route == "detail" and st.session_state.get("detail_photo_id"):
            desired["photo"] = st.session_state["detail_photo_id"]
        if dict(st.query_params) != desired:
            st.query_params.clear()
            st.query_params.update(desired)
    elif dict(st.query_params):
        st.query_params.clear()


# ═══ Main dispatch ═══════════════════════════════════════════════════════

require_password()
_restore_from_url()

route = current_route()

if route in ("board", "detail"):
    render_sidebar()

if route == "landing":
    render_landing_page()
elif route == "create":
    render_create_project_page()
elif route == "loading":
    render_loading_page()
elif route == "board":
    render_board_page()
elif route == "detail":
    render_detail_page()
else:
    set_route("landing")
    st.rerun()

_sync_url()
