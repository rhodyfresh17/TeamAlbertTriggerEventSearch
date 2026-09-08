#!/usr/bin/env python3
"""
Sales Trigger Events Dashboard

Interactive Streamlit dashboard for managing and reviewing trigger event alerts.
Reads from Supabase for online access.

Usage:
    streamlit run dashboard.py
"""

import os
import re
import json
import base64
import urllib.parse
import pandas as pd
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import streamlit as st

# Shared territory vocabulary — ONE source of truth for state/province codes
# and names across scrapers, enrichment and this UI (src/pipeline/gates.py).
# The app runs from the repo root on Streamlit Cloud, so `src.` resolves.
from src.pipeline.gates import ALL_STATE_CODES, STATE_NAMES, TERRITORY_STATES
from src.pipeline.gates import account_key as _gates_account_key   # the module-absent lookups (review 2026-09-08, Phase 4)
# Phase 3 supply visibility (2026-09-08): the Weekly Scorecard buckets rows
# exactly as monitor_health.py does (same source labels, same finance-leader
# family), so the dashboard and the Monday health check never disagree.
from src.pipeline.sources import OTHER as OTHER_SOURCE, finance_leader_family, source_label
from src.pipeline.typed import parse_ts, verify_state_for
# The FY27 vertical taxonomy (32 ZoomInfo subindustries → 3 verticals) is
# defined ONCE, in the enrichment engine. The import is guarded because this
# app must still render if that module ever fails to import on Streamlit
# Cloud — the Supply section then says so and shows every account as
# 'Unknown' (visible), rather than silently mis-bucketing with a stale copy.
try:
    from enrichment_scout import ZI_SUBINDUSTRIES, NONPROFIT_VERTICAL
except Exception:  # noqa: BLE001 — any import failure, not only ImportError
    ZI_SUBINDUSTRIES, NONPROFIT_VERTICAL = {}, 'Nonprofits & Organizations'
# Phase 4 2026-09-08: accounts as the primary object. src/pipeline/accounts.py
# owns the ONE account normalizer (= gates.account_key), the disposition
# vocabulary (statuses + reason codes) and the accounts-table reads/writes.
# It ships in the same push as this file but is written separately, and this
# app must keep rendering exactly as before if it is missing or fails to
# import on Streamlit Cloud — every use below checks `_accounts is None` and
# takes the Phase 1-3 (legacy) path. Same guard shape as the taxonomy import
# above: any failure, not only ImportError, or one bad line there would
# take the whole dashboard down.
try:
    from src.pipeline import accounts as _accounts
except Exception:  # noqa: BLE001
    _accounts = None


def get_logo_base64() -> str:
    logo_path = Path("assets/logo.png")
    if logo_path.exists():
        return base64.b64encode(logo_path.read_bytes()).decode()
    return ""

# Page config
try:
    from PIL import Image as PILImage
    _favicon = PILImage.open("assets/logo.png")
except Exception:
    _favicon = "🎯"

st.set_page_config(
    page_title="Team Albert | Sales Intelligence",
    page_icon=_favicon,
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for modern UI
st.markdown("""
<style>
    /* Import modern font */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    /* ── Theme tokens (dark mode — matches config.toml base="dark") ── */
    :root {
        --text-primary:       rgba(255,255,255,0.92);
        --text-secondary:     rgba(255,255,255,0.65);
        --text-muted:         rgba(255,255,255,0.40);
        --border-color:       rgba(255,255,255,0.12);
        --card-bg:            rgba(255,255,255,0.06);
        --card-border:        rgba(255,255,255,0.10);
        --card-shadow:        0 4px 20px rgba(0,0,0,0.35);
        --section-count-bg:   rgba(255,255,255,0.12);
        --section-count-text: rgba(255,255,255,0.65);
        --sidebar-title:      rgba(255,255,255,0.50);
        --sidebar-caption:    rgba(255,255,255,0.32);
    }

    /* Hide Streamlit branding clutter — but KEEP the header + toolbar visible
       so the sidebar collapse/expand toggle (which lives in the toolbar in
       current Streamlit versions) remains accessible. */
    #MainMenu {display: none;}
    footer {display: none;}
    [data-testid="stDeployButton"] {display: none;}

    /* Transparent header so it doesn't show a visible bar at the top, but
       still occupies its space so the sidebar toggle has somewhere to live. */
    [data-testid="stHeader"] {
        background: transparent !important;
    }

    /* Force the sidebar expand button visible regardless of Streamlit version —
       covers the various testid names used across releases. */
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"],
    [data-testid="stSidebarNavCollapseButton"],
    [data-testid="stSidebarHeader"] button,
    button[kind="header"],
    button[kind="headerNoPadding"] {
        visibility: visible !important;
        opacity: 1 !important;
        display: flex !important;
        z-index: 999999 !important;
    }

    /* Global */
    .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    /* Team Albert header */
    .main-header {
        background: linear-gradient(135deg, #1a3a4a 0%, #2d6080 60%, #1a3a4a 100%);
        padding: 1.75rem 2.5rem;
        border-radius: 16px;
        margin-bottom: 2rem;
        box-shadow: 0 10px 40px rgba(0,0,0,0.4);
        border: 1px solid rgba(78,140,170,0.3);
    }
    .header-inner {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1.75rem;
    }
    .header-logo {
        height: 160px;
        width: auto;
        opacity: 0.95;
        flex-shrink: 0;
        mix-blend-mode: screen;
    }
    .header-text { display: flex; flex-direction: column; gap: 0.3rem; }
    .header-title {
        color: white;
        font-size: 1.9rem;
        font-weight: 700;
        margin: 0;
        letter-spacing: 0.5px;
        line-height: 1.1;
    }
    .header-subtitle {
        color: #c9a84c;
        font-size: 0.8rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 2px;
        margin: 0;
    }
    .header-tagline {
        color: rgba(255,255,255,0.6);
        font-size: 0.875rem;
        margin: 0;
    }

    /* Metric cards */
    .metric-card {
        background: var(--card-bg);
        border-radius: 16px;
        padding: 1.5rem;
        box-shadow: var(--card-shadow);
        border: 1px solid var(--card-border);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .metric-card:hover { transform: translateY(-2px); box-shadow: 0 8px 30px rgba(0,0,0,0.15); }
    .metric-icon { width: 48px; height: 48px; border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 1.5rem; margin-bottom: 1rem; }
    .metric-value { font-size: 2rem; font-weight: 700; color: var(--text-primary); line-height: 1; }
    .metric-label { font-size: 0.875rem; color: var(--text-secondary); margin-top: 0.5rem; font-weight: 500; }

    /* Section headers */
    .section-header {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        margin: 1.5rem 0 1rem;
        padding-bottom: 0.75rem;
        border-bottom: 2px solid var(--border-color);
    }
    .section-header h2 { font-size: 1.25rem; font-weight: 600; color: var(--text-primary); margin: 0; }
    .section-count {
        background: var(--section-count-bg);
        color: var(--section-count-text);
        padding: 0.25rem 0.75rem;
        border-radius: 50px;
        font-size: 0.8rem;
        font-weight: 600;
    }

    /* Event cards */
    .event-card-inner { padding: 0.25rem 0; }
    .event-card-header { display: flex; align-items: flex-start; gap: 1rem; }

    .event-type-badge {
        padding: 0.35rem 0.75rem;
        border-radius: 50px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        white-space: nowrap;
    }
    .badge-ma      { background: #dbeafe; color: #1e40af; }
    .badge-cfo     { background: #d1fae5; color: #065f46; }
    .badge-funding { background: #fef3c7; color: #92400e; }
    .badge-stable  { background: #ffedd5; color: #9a3412; }
    .badge-exec    { background: #ede9fe; color: #5b21b6; }
    .badge-seat    { background: #ccfbf1; color: #115e59; }
    .badge-expansion { background: #fce7f3; color: #9d174d; }
    .badge-other   { background: #f3f4f6; color: #374151; }

    .status-badge { padding: 0.25rem 0.6rem; border-radius: 50px; font-size: 0.7rem; font-weight: 600; text-transform: uppercase; }
    .status-new          { background: #dbeafe; color: #1e40af; }
    .status-reviewed     { background: #d1fae5; color: #065f46; }
    .status-customer     { background: #fef3c7; color: #92400e; }
    .status-out          { background: #fee2e2; color: #991b1b; }
    .status-not-relevant { background: #f3f4f6; color: #6b7280; }

    .event-title   { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0.5rem 0; line-height: 1.4; }
    .event-company { display: flex; align-items: center; gap: 0.5rem; color: var(--text-secondary); font-size: 0.875rem; }
    .event-meta    { display: flex; gap: 1rem; margin-top: 0.75rem; font-size: 0.8rem; color: var(--text-muted); }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1a3a4a 0%, #152f3d 100%) !important;
        border-right: 1px solid rgba(78,140,170,0.2);
    }
    section[data-testid="stSidebar"] .sidebar-section-title {
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #c9a84c !important;
        margin: 1rem 0 0.5rem;
    }

    /* Divider */
    hr { border: none; height: 1px; background: var(--border-color); margin: 1.5rem 0; }

    /* Buttons */
    .stButton > button { border-radius: 8px; font-weight: 500; transition: all 0.2s ease; }
    .stButton > button:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }

    /* Expanders */
    .streamlit-expanderHeader { font-weight: 500; font-size: 0.9rem; }

</style>
""", unsafe_allow_html=True)

# ── JSONB helpers ────────────────────────────────────────────────────────────
# Supabase returns JSONB columns as dicts/lists, but pandas turns NULLs into
# NaN and older rows may carry JSON *strings*. Every reader goes through here.
def _parse_json_field(val, default):
    """NaN/None/JSON-string tolerant unwrap of a JSONB column value."""
    if val is None or (isinstance(val, float) and val != val):
        return default
    if isinstance(val, str):
        try:
            return json.loads(val) if val.strip() else default
        except Exception:
            return default
    return val


# Roles that mark the company an event is ABOUT, in priority order.
_PRIMARY_ROLES = ('acquirer', 'portfolio company', 'hiring company',
                  'primary', 'target')


def _account_company(row) -> Optional[dict]:
    """The companies_data entry for the event's ACCOUNT: the fit-gate-chosen
    account (fit.account_name) when present, else the first company in a
    primary role, else the first company. None when nothing is enriched."""
    cd = _parse_json_field(row.get('companies_data'), [])
    if not isinstance(cd, list):
        return None
    cd = [c for c in cd if isinstance(c, dict)]
    if not cd:
        return None
    fit = _parse_json_field(row.get('fit'), None)
    acct = (str(fit.get('account_name') or '').strip().lower()
            if isinstance(fit, dict) else '')
    if acct:
        for c in cd:
            if str(c.get('name') or '').strip().lower() == acct:
                return c
    for role in _PRIMARY_ROLES:
        for c in cd:
            if str(c.get('role') or '').strip().lower() == role:
                return c
    return cd[0]


# ── Verification filter (fit.verdict) ────────────────────────────────────────
# A.J. 2026-09-04: reps see VERIFIED accounts by default (vertical AND
# territory confirmed). The "Show unverified accounts" toggle adds the two
# undecided states; rejected/decided rows never render.
#   pass        → verified                       (default view)
#   unverified  → researched, not fully confirmed (toggle)
#   staged      → never researched / vertical unknown (toggle)
#   fail        → fit gate rejected               (never)
#   decided     → a rep already dispositioned it  (never)
# A row with no `fit` at all was never researched, so it counts as 'staged'.
VERIFIED_VERDICTS = frozenset({'pass'})
UNVERIFIED_VERDICTS = frozenset({'unverified', 'staged'})


def fit_verdict(row) -> str:
    """Normalized fit.verdict for a row (Series or dict); missing → 'staged'."""
    fit = _parse_json_field(row.get('fit'), None)
    if not isinstance(fit, dict):
        return 'staged'
    v = str(fit.get('verdict') or '').strip().lower()
    return v or 'staged'


def split_by_verdict(df: pd.DataFrame, show_unverified: bool):
    """→ (kept_df, n_unverified). Toggle OFF keeps verdict == 'pass' only;
    ON also keeps 'unverified' + 'staged'. 'fail' / 'decided' are dropped
    either way. n_unverified is the count of unverified+staged rows in `df`
    — the number the toggle hides (OFF) or reveals (ON)."""
    if df.empty:
        return df, 0
    verdicts = df.apply(fit_verdict, axis=1)
    is_verified = verdicts.isin(VERIFIED_VERDICTS)
    is_unverified = verdicts.isin(UNVERIFIED_VERDICTS)
    keep = (is_verified | is_unverified) if show_unverified else is_verified
    return df[keep], int(is_unverified.sum())


# ── Territory filter (structured) ────────────────────────────────────────────
# v1 substring-matched region/city keywords against article text, so
# "Manchester" hit Manchester UK, "Columbia" hit British Columbia, and
# "Windsor" hit half of England. v2 derives ONE HQ state/province code per
# event — from the enriched ACCOUNT company's `hq`, falling back to the
# scraper's `matched_regions` — and filters on that code.
_HQ_COUNTRY_TOKENS = {
    'usa', 'u.s.', 'u.s.a.', 'us', 'united states', 'united states of america',
    'canada', 'north america',
}
# Bare 2-letter tails that are also English words ("Portland ME"): only
# trusted when a comma separates them from the city (mirrors gates.py).
_HQ_AMBIGUOUS_TAILS = {'IN', 'OR', 'ME', 'DE', 'OH', 'HI'}
# Longest names first so "West Virginia" wins over "Virginia" in free text.
_STATE_NAMES_LONGEST_FIRST = sorted(STATE_NAMES.items(), key=lambda kv: -len(kv[0]))


def _pretty_state_name(n: str) -> str:
    return ' '.join(w if w in ('of', 'and') else w.capitalize() for w in n.split())


_CODE_TO_NAME = {}
for _n, _c in STATE_NAMES.items():
    _CODE_TO_NAME.setdefault(_c, _pretty_state_name(_n))


def hq_state_code(hq) -> Optional[str]:
    """'Boston, MA' / 'Boston, Massachusetts' / 'Toronto, ON, Canada' /
    'massachusetts' → 'MA' / 'MA' / 'ON' / 'MA'. None when no state or
    province can be read (city-only, foreign, blank, NaN). Same parsing
    strategy as gates.hq_territory_status(), but returns the CODE."""
    if hq is None or (isinstance(hq, float) and hq != hq):
        return None
    h = str(hq).strip()
    if not h:
        return None
    h = re.sub(r'\bd\.c\.?(?=\W|$)', 'dc', h, flags=re.IGNORECASE)
    segs = [s.strip() for s in re.split(r'[,/|]', h) if s.strip()]
    segs = [s for s in segs if s.lower().strip('. ') not in _HQ_COUNTRY_TOKENS]
    # Right-to-left: the state usually follows the city.
    for seg in reversed(segs):
        s = seg.strip('. ')
        up = s.upper()
        if len(up) == 2 and up.isalpha() and up in ALL_STATE_CODES:
            return up
        lo = s.lower()
        if lo in STATE_NAMES:
            return STATE_NAMES[lo]
        m = re.search(r'\b([A-Za-z]{2})$', s)   # "Boston MA" without a comma
        if m and len(s.split()) >= 2:
            code = m.group(1).upper()
            if code in ALL_STATE_CODES and code not in _HQ_AMBIGUOUS_TAILS:
                return code
    lo_all = h.lower()
    for name, code in _STATE_NAMES_LONGEST_FIRST:
        if re.search(r'\b' + re.escape(name) + r'\b', lo_all):
            return code
    return None


def event_state_code(row) -> Optional[str]:
    """HQ state/province code for an event. The typed `hq_state` column
    wins when the row carries one (the enricher already parsed it — v2
    Phase 2); then the account company's `hq`; then the scraper's
    matched_regions (SEC/Adzuna store a code, news feeds store lower-case
    state names and city names — cities yield None)."""
    typed = row.get('hq_state') if hasattr(row, 'get') else None
    if isinstance(typed, str) and typed.strip():
        return typed.strip().upper()
    co = _account_company(row)
    if co:
        code = hq_state_code(co.get('hq'))
        if code:
            return code
    raw = row.get('matched_regions')
    regions = _parse_json_field(raw, None)
    if regions is None and isinstance(raw, str):      # legacy comma-joined
        regions = [r.strip() for r in raw.split(',')]
    if isinstance(regions, list):
        for r in regions:
            code = hq_state_code(r)
            if code:
                return code
    return None


def annotate_hq_state(df: pd.DataFrame) -> pd.DataFrame:
    """Add a `_hq_state` column (code or None) — computed once per load and
    shared by the territory multiselect options and the filter."""
    df = df.copy()
    if df.empty:
        df['_hq_state'] = pd.Series(dtype=object)
    else:
        df['_hq_state'] = df.apply(event_state_code, axis=1)
    return df


def territory_label(code: str) -> str:
    name = _CODE_TO_NAME.get(code)
    return f"{code} · {name}" if name else str(code)


def territory_options(df: pd.DataFrame) -> list:
    """Codes actually present in df['_hq_state'], in-territory first, then
    alphabetical — so the multiselect never lists states with zero rows."""
    if df.empty or '_hq_state' not in df.columns:
        return []
    codes = {c for c in df['_hq_state'].dropna().unique() if c}
    return sorted(codes, key=lambda c: (c not in TERRITORY_STATES, c))


def filter_by_territory(df: pd.DataFrame, selected_codes: list) -> pd.DataFrame:
    """Keep events whose HQ code is in `selected_codes` (empty = no filter)."""
    if not selected_codes or df.empty or '_hq_state' not in df.columns:
        return df
    return df[df['_hq_state'].isin(set(selected_codes))]


# 4-segment NetSuite sales taxonomy, ordered low → high.
#   LMM  (Lower Mid-Market):  $0-$10M
#   MM   (Mid-Market):        $10M-$20M
#   Corp (Corporate):         $20M-$100M
#   Enterprise:               $100M+
REVENUE_BANDS = ['LMM', 'MM', 'Corp', 'Enterprise']

# Human-readable description for the chip tooltip and sidebar help
BAND_RANGE = {
    'LMM':        '$0-$10M',
    'MM':         '$10M-$20M',
    'Corp':       '$20M-$100M',
    'Enterprise': '$100M+',
}

# Map LEGACY buckets (from earlier enrichment runs) → current 4 segments.
# Used so events enriched against older schemas still filter correctly
# without forcing a full re-enrichment.
LEGACY_BUCKET_MAP = {
    # Original 7-bucket schema
    '<$10M':       'LMM',
    '$10M-50M':    'MM',   # straddles MM + Corp; conservative pick
    '$50M-100M':   'Corp',
    '$100M-200M':  'Enterprise',
    '$200M-500M':  'Enterprise',
    '$500M-1B':    'Enterprise',
    '$1B+':        'Enterprise',
    # Granular 9-bucket schema (briefly used between commits ac17b5b..db6402c)
    '<$5M':        'LMM',
    '$5M-10M':     'LMM',
    '$10M-25M':    'MM',   # straddles MM + Corp; conservative pick
    '$25M-50M':    'Corp',
}

# Quick presets for the sidebar
REVENUE_PRESETS = {
    'NetSuite Up-Market ($0-$100M)': ['LMM', 'MM', 'Corp'],
    'LMM only (<$10M)':              ['LMM'],
    'MM only ($10M-$20M)':           ['MM'],
    'Corp only ($20M-$100M)':        ['Corp'],
    'Under $20M (LMM + MM)':         ['LMM', 'MM'],
    'Enterprise ($100M+)':           ['Enterprise'],
    'All segments':                   list(REVENUE_BANDS),
}


def _coerce_revenue_band(raw) -> str:
    """Map any revenue string (canonical segment, legacy bucket, or free-form
    dollar figure like '$27.9B') to one of the 4 current segments. Returns ''
    if not parseable. Defense-in-depth for LLM deviations + legacy data."""
    import re
    if not raw or not isinstance(raw, str):
        return ''
    s = raw.strip()
    # Already a canonical segment
    if s in REVENUE_BANDS:
        return s
    # Legacy bucket from older enrichment runs
    if s in LEGACY_BUCKET_MAP:
        return LEGACY_BUCKET_MAP[s]
    # Try to parse a single dollar figure like "$27.9B", "$50M", "18M"
    m = re.search(r'\$?\s*(\d+(?:\.\d+)?)\s*([MBK])', s, re.IGNORECASE)
    if not m:
        return ''
    val, unit = float(m.group(1)), m.group(2).upper()
    millions = val * (1000 if unit == 'B' else (0.001 if unit == 'K' else 1))
    if millions < 10:    return 'LMM'
    if millions < 20:    return 'MM'
    if millions < 100:   return 'Corp'
    return 'Enterprise'


def _band_idx(b) -> int:
    """Return ordinal index of a revenue segment, or -1 if not recognised.
    Tolerates legacy buckets + free-form strings via _coerce_revenue_band."""
    if not b:
        return -1
    canonical = b if b in REVENUE_BANDS else _coerce_revenue_band(b)
    try:
        return REVENUE_BANDS.index(canonical)
    except (ValueError, AttributeError):
        return -1


def filter_by_grades(
    df: pd.DataFrame,
    allowed_grades: list,
    include_ungraded: bool = True,
) -> pd.DataFrame:
    """Keep events whose TAL grade is in `allowed_grades`.
    Events with no grade are kept iff include_ungraded=True (so fresh
    events don't disappear before grading runs)."""
    if not allowed_grades:
        return df
    allowed_set = {g.upper() for g in allowed_grades}

    def keep(row):
        g = row.get('grade')
        if g is None or (isinstance(g, float) and g != g):
            return include_ungraded
        g_str = str(g).strip().upper()
        if not g_str or g_str in ('NONE', 'NAN'):
            return include_ungraded
        return g_str in allowed_set

    return df[df.apply(keep, axis=1)]


def filter_by_revenue_bands(
    df: pd.DataFrame,
    allowed_bands: list,
    include_unknown: bool = True,
) -> pd.DataFrame:
    """Keep events whose primary company has revenue in `allowed_bands`.
    Events with unknown revenue are kept iff `include_unknown=True`."""
    if not allowed_bands:
        return df  # No filter applied
    allowed_set = set(allowed_bands)

    # Primary roles we care about (the actual subject of the event)
    primary_roles = {
        'acquirer', 'target', 'portfolio company',
        'hiring company', 'primary',
    }

    def keep(row):
        cd = row.get('companies_data')
        # NaN-safe unwrap
        if cd is None or (isinstance(cd, float) and cd != cd):
            return include_unknown
        if isinstance(cd, str):
            try:
                import json as _json
                cd = _json.loads(cd) if cd.strip() else []
            except Exception:
                return include_unknown
        if not isinstance(cd, list) or not cd:
            return include_unknown

        # Find primary company; fall back to first company in the list
        primary = next(
            (c for c in cd
             if str(c.get('role', '')).lower() in primary_roles),
            cd[0]
        )
        raw_rev = primary.get('revenue') or ''
        if not raw_rev:
            return include_unknown
        canonical = raw_rev if raw_rev in REVENUE_BANDS else _coerce_revenue_band(raw_rev)
        if not canonical:
            return include_unknown
        return canonical in allowed_set

    return df[df.apply(keep, axis=1)]


# Event type configurations with modern colors
EVENT_TYPES = {
    "merger_acquisition": {
        "label": "M&A",
        "full_label": "Mergers & Acquisitions",
        "color": "#3b82f6",
        "gradient": "linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%)",
        "icon": "🔵",
        "badge_class": "badge-ma",
        "bg_color": "#dbeafe"
    },
    "cfo_hire": {
        "label": "CFO",
        "full_label": "CFO Hires",
        "color": "#10b981",
        "gradient": "linear-gradient(135deg, #10b981 0%, #059669 100%)",
        "icon": "💼",
        "badge_class": "badge-cfo",
        "bg_color": "#d1fae5"
    },
    # Adzuna job postings: a company HIRING a CFO/Controller (v2, replaces
    # cfo_hire/executive_hire for Adzuna events). A distinct trigger — the
    # seat is OPEN, nobody has been named — graded +3 (A.J. 2026-09-06).
    "finance_seat_open": {
        "label": "Open Seat",
        "full_label": "Open Finance Seats",
        "color": "#14b8a6",
        "gradient": "linear-gradient(135deg, #14b8a6 0%, #0d9488 100%)",
        "icon": "🪑",
        "badge_class": "badge-seat",
        "bg_color": "#ccfbf1"
    },
    "funding": {
        "label": "Funding",
        "full_label": "PE/VC Funding",
        "color": "#f59e0b",
        "gradient": "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)",
        "icon": "💰",
        "badge_class": "badge-funding",
        "bg_color": "#fef3c7"
    },
    "stable_target": {
        "label": "Stable",
        "full_label": "Stable Targets",
        "color": "#f97316",
        "gradient": "linear-gradient(135deg, #f97316 0%, #ea580c 100%)",
        "icon": "🎯",
        "badge_class": "badge-stable",
        "bg_color": "#ffedd5"
    },
    "executive_hire": {
        "label": "Exec",
        "full_label": "Executive Hires",
        "color": "#8b5cf6",
        "gradient": "linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)",
        "icon": "👔",
        "badge_class": "badge-exec",
        "bg_color": "#ede9fe"
    },
    # Phase 4 2026-09-08: sec_iapd new-adviser registrations and plant /
    # office openings (scripts/ria_trigger.py writes event_type='expansion').
    # Without an entry every one of them rendered as a "📋 Other" card, and
    # the New Leads tab maths (`_tabbed` in main) needs each real type listed
    # here — a type in this map MUST also have a tab, or it renders nowhere.
    # NOT a finance-leader type: FINANCE_LEADER_EVENT_TYPES is unchanged.
    "expansion": {
        "label": "Expansion",
        "full_label": "Expansion / New registration",
        "color": "#ec4899",
        "gradient": "linear-gradient(135deg, #ec4899 0%, #db2777 100%)",
        "icon": "🌱",
        "badge_class": "badge-expansion",
        "bg_color": "#fce7f3"
    },
    "other": {
        "label": "Other",
        "full_label": "Other Events",
        "color": "#6b7280",
        "gradient": "linear-gradient(135deg, #6b7280 0%, #4b5563 100%)",
        "icon": "📋",
        "badge_class": "badge-other",
        "bg_color": "#f3f4f6"
    }
}


def event_config_for(event_type) -> dict:
    """EVENT_TYPES entry for a row's event_type. Unknown / None / NaN →
    the 'other' config, so a new or legacy type can never crash a card."""
    if event_type is None or (isinstance(event_type, float) and event_type != event_type):
        return EVENT_TYPES['other']
    return EVENT_TYPES.get(str(event_type), EVENT_TYPES['other'])


# Lead status options
LEAD_STATUSES = [
    "NEW",
    "REVIEWED - Picked Up",
    "REVIEWED - ON REP TAL",
    "REVIEWED - NetSuite Customer",
    "REVIEWED - Out of Alignment",
    "NOT RELEVANT"
]

STATUS_CONFIG = {
    "NEW": {"icon": "🆕", "class": "status-new", "label": "New"},
    "REVIEWED - Picked Up": {"icon": "✅", "class": "status-customer", "label": "Picked Up"},
    "REVIEWED - ON REP TAL": {"icon": "🟠", "class": "status-reviewed", "label": "On TAL"},
    "REVIEWED - NetSuite Customer": {"icon": "💼", "class": "status-customer", "label": "Customer"},
    "REVIEWED - Out of Alignment": {"icon": "❌", "class": "status-out", "label": "Out"},
    "NOT RELEVANT": {"icon": "🚫", "class": "status-not-relevant", "label": "Not Relevant"}
}

# Backwards compatibility
STATUS_ICONS = {k: v["icon"] for k, v in STATUS_CONFIG.items()}


@st.cache_resource
def get_supabase_client():
    """Get Supabase client."""
    try:
        from supabase import create_client
    except ImportError:
        st.error("Supabase not installed. Run: pip install supabase")
        return None

    def _secret(name):
        if hasattr(st, 'secrets') and name in st.secrets:
            return st.secrets.get(name)
        return os.environ.get(name)

    url = _secret("SUPABASE_URL")
    # Service-role key preferred: it bypasses Row-Level Security, so the
    # dashboard keeps working after RLS is enabled on the tables. This app
    # runs server-side only — the key is never exposed to viewers' browsers.
    key = _secret("SUPABASE_SERVICE_ROLE_KEY") or _secret("SUPABASE_KEY")

    if not url or not key:
        return None

    return create_client(url, key)


def load_source_statuses() -> pd.DataFrame:
    """Load source statuses from Supabase."""
    client = get_supabase_client()
    if not client:
        return pd.DataFrame()

    try:
        response = client.table('source_status').select('*').order('source_type').order('source_name').execute()

        if not response.data:
            return pd.DataFrame()

        return pd.DataFrame(response.data)

    except Exception as e:
        # Table might not exist yet
        return pd.DataFrame()


# ── Typed columns (v2 Phase 2, 2026-09-07) ──────────────────────────────────
# The v2 migration adds typed columns (verify_state, fit_verdict, hq_state,
# source, expires_at, ...) so the verification filter can run server-side
# instead of parsing every row's `fit` JSONB after a 10k-row download. A.J.
# runs that migration by hand, later — so every reader PROBES for the
# columns and behaves exactly as before when they're absent.
TYPED_PROBE_COLUMNS = ('verify_state', 'fit_verdict', 'hq_state', 'source',
                       'expires_at')

# verify_state values split_by_verdict shows with the toggle ON. pass →
# verified, unverified → researched_ambiguous, staged → staged; 'fail' /
# 'decided' (not_fit / decided) are dropped either way. Rows the enricher
# hasn't reached yet have verify_state NULL — the client-side pass treats
# those (fit=None) as 'staged', so the server-side filter must keep NULLs
# too or fresh events would vanish the moment the migration lands.
VERIFIED_STATES = ('verified',)
UNVERIFIED_STATES = ('researched_ambiguous', 'staged')


def _probe_typed_columns(client, columns=TYPED_PROBE_COLUMNS) -> set:
    """Which of `columns` exist on `events`. One cheap select per column;
    a column that errors is absent, a client that errors yields the empty
    set (= legacy behaviour everywhere)."""
    present = set()
    if client is None:
        return present
    for col in columns:
        try:
            client.table('events').select(col).limit(1).execute()
            present.add(col)
        except Exception:
            continue
    return present


# cache_data, NOT cache_resource: the resource cache survives secret
# rotations and kept serving a stale client on 2026-09-04. An hour is
# plenty — the migration is a one-off, and a stale "absent" only costs the
# legacy (still correct) code path until the TTL expires.
@st.cache_data(ttl=3600)
def typed_columns_present() -> set:
    try:
        return _probe_typed_columns(get_supabase_client())
    except Exception:
        return set()


def _states_or_filter(states) -> str:
    """PostgREST `or=` expression: verify_state in `states` OR NULL (the
    not-yet-enriched rows split_by_verdict treats as 'staged')."""
    return "verify_state.in.({}),verify_state.is.null".format(','.join(states))


# The three server-side verification expressions (review 2026-09-07).
# verify_state is written by enrichment, and an enrichment process that was
# already running when the migration landed (its column probe cached
# "absent") keeps writing fit.verdict='pass' with verify_state NULL. A bare
# verify_state.eq.verified would hide such a row from the default view until
# its next enrichment, so NULL-state rows are judged by the JSON verdict:
#   verified  = state verified, OR state NULL with fit.verdict = pass
#   hidden    = state researched_ambiguous / staged, OR state NULL with
#               anything but a pass verdict. fit->>verdict.is.null covers
#               both a NULL fit and a fit without a verdict key (the JSON
#               path of a NULL fit is NULL too); a plain fit.is.null would
#               miss the second, and SQL's NULL <> 'pass' is not true.
#   toggle ON = verified ∪ hidden = state in (verified, researched_ambiguous,
#               staged) OR state NULL — the union collapses to the plain
#               state list, so that expression is unchanged.
# Validated read-only against the live project on 2026-09-07 with existing
# columns standing in (grade / blocked_at / fit): nested and()/or() inside
# or=, in.(...) and the fit->>verdict path all parse, and count(verified) +
# count(hidden) == count(toggle ON) over a 30-day window (1 + 288 == 289).
VERIFIED_FILTER = ("verify_state.eq.{},and(verify_state.is.null,fit->>verdict.eq.pass)"
                   .format(VERIFIED_STATES[0]))
HIDDEN_FILTER = ("verify_state.in.({}),and(verify_state.is.null,"
                 "or(fit->>verdict.is.null,fit->>verdict.neq.pass))"
                 .format(','.join(UNVERIFIED_STATES)))
TOGGLE_ON_FILTER = _states_or_filter(VERIFIED_STATES + UNVERIFIED_STATES)


def build_events_query(client, days: int, verified_only, present, now=None):
    """Pure query builder for load_events / count_hidden_unverified.

    verified_only=None → no server-side verification filter (legacy path,
    also the only option until 'verify_state' exists). True → VERIFIED_FILTER
    (verified rows, plus NULL-state rows whose fit verdict is pass); False →
    TOGGLE_ON_FILTER, everything split_by_verdict shows with the toggle ON
    (verified + unverified + not-yet-enriched). `present` is the set from
    typed_columns_present(); `now` is injectable for tests."""
    now = now or datetime.now()
    query = client.table('events').select('*')
    cutoff_date = (now - timedelta(days=days)).isoformat()
    query = query.gte('discovered_at', cutoff_date)
    # Hide soft-deleted (industry-blocked) events. They stay in the table
    # so supabase_sync doesn't recreate them via upsert, but the user
    # never sees them. The is_('blocked_at', 'null') filter is omitted
    # if the column doesn't exist yet (pre-migration).
    try:
        query = query.is_('blocked_at', 'null')
    except Exception:
        pass  # column not yet present; will start filtering after migration
    if verified_only is not None and 'verify_state' in present:
        query = query.or_(VERIFIED_FILTER if verified_only else TOGGLE_ON_FILTER)
    # 'expires_at' is deliberately NOT filtered here even when present:
    # Phase 4 decides how expired triggers are displayed (hidden, dimmed,
    # or a separate tab). Filtering now would silently drop rows.
    return query


def count_hidden_unverified(days: int, present, client=None, now=None) -> int:
    """How many rows in the window the verified-only default hides — the
    "N unverified hidden" caption needs it once load_events stops
    downloading those rows. HIDDEN_FILTER is the exact complement of
    VERIFIED_FILTER within the toggle-ON set, so shown + hidden = toggle ON.
    The count is WINDOW-WIDE (whole time range, every territory / grade);
    unverified_caption says so. 0 when the typed column is absent (the
    client-side split_by_verdict pass counts them from the frame then) or
    when anything fails — the caption is informational, never fatal."""
    if 'verify_state' not in present:
        return 0
    client = client or get_supabase_client()
    if not client:
        return 0
    now = now or datetime.now()
    try:
        cutoff_date = (now - timedelta(days=days)).isoformat()
        q = client.table('events').select('id', count='exact')
        q = q.gte('discovered_at', cutoff_date).is_('blocked_at', 'null')
        q = q.or_(HIDDEN_FILTER)
        resp = q.execute()
        return int(getattr(resp, 'count', None) or 0)
    except Exception:
        return 0


def unverified_caption(n: int, show_unverified: bool, days: int, window_wide: bool) -> str:
    """Text under the verification toggle. ON: `n` unverified rows are in
    the frame on screen (view-relative — territory / revenue / grade filters
    already applied). OFF: `n` were hidden — view-relative when counted from
    the downloaded frame (legacy path), but WINDOW-WIDE when it came from
    count_hidden_unverified: those rows were never downloaded, so the count
    spans the whole time range and every territory / grade. Review
    2026-09-07: say which, or the OFF and ON numbers look like they should
    agree and don't."""
    if show_unverified:
        return f"{n:,} unverified shown"
    if window_wide:
        return f"{n:,} unverified hidden (whole {days}-day window)"
    return f"{n:,} unverified hidden"


def load_events(days: int = 30, search: str = None,
                verified_only=None) -> pd.DataFrame:
    """Load all events from Supabase. verified_only=None keeps the legacy
    "download everything, filter client-side" path; True/False push the
    verification filter into the query once `verify_state` exists (Phase 2,
    2026-09-07)."""
    client = get_supabase_client()
    if not client:
        return pd.DataFrame()

    try:
        present = typed_columns_present() if verified_only is not None else set()
        query = build_events_query(client, days, verified_only, present)

        # Paginate — Supabase caps single responses at 1000 rows, which the
        # DB has now outgrown. A flat .limit(1000) silently dropped the
        # oldest rows in the window (audit 2026-07-16).
        rows = []
        page = 0
        while True:
            resp = query.order('discovered_at', desc=True).range(
                page * 1000, page * 1000 + 999).execute()
            rows.extend(resp.data or [])
            if not resp.data or len(resp.data) < 1000 or page >= 9:
                break  # 10k-row sanity ceiling
            page += 1

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        if search:
            search_lower = search.lower()
            mask = (
                df['title'].str.lower().str.contains(search_lower, na=False) |
                df['company_name'].str.lower().str.contains(search_lower, na=False) |
                df['description'].str.lower().str.contains(search_lower, na=False)
            )
            df = df[mask]

        df = df.rename(columns={
            'source_url': 'url',
            'discovered_at': 'discovered_date'
        })

        df['lead_status'] = df['lead_status'].fillna('NEW')

        return df

    except Exception as e:
        st.error(f"Error loading events: {e}")
        return pd.DataFrame()


def update_lead_status(event_id: str, status: str, notes: str = None):
    """Update lead status for an event in Supabase.

    NOT RELEVANT = SOFT-delete (set blocked_at), never a hard DELETE.
    Hard-deleting was a zombie loop: supabase_sync upserts every SQLite
    event each 4-hour cycle, so a deleted row was re-created as NEW and
    reps had to re-dismiss the same lead over and over (audit 2026-07-16).
    The tombstone row keeps the upsert from resurrecting it."""
    client = get_supabase_client()
    if not client:
        return False

    try:
        if status == "NOT RELEVANT":
            data = {
                'blocked_at': datetime.now().isoformat(),
                'blocked_reason': 'dismissed by rep (NOT RELEVANT)',
                'lead_status': 'NOT RELEVANT',
            }
            if notes:
                data['notes'] = notes
            client.table('events').update(data).eq('id', event_id).execute()
        else:
            data = {'lead_status': status}
            if notes is not None:
                data['notes'] = notes
            client.table('events').update(data).eq('id', event_id).execute()
        return True
    except Exception as e:
        st.error(f"Error updating status: {e}")
        return False


# TAL grade pill colours — one table for the event card and the account
# strip under a Work Queue row (Phase 4 2026-09-08), so the two never drift.
GRADE_COLORS = {
    'A': '#10b981',  # emerald
    'B': '#3b82f6',  # blue
    'C': '#f59e0b',  # amber
    'D': '#6b7280',  # slate
}


def _resolve_display_company(row) -> str:
    """Pick the best company name to show in the card header.
    1st choice: the fit-gate-chosen ACCOUNT (the workable company — may be
    the target or the PE firm, not the headline company). Then scrape-time
    company_name, then the primary enriched company."""
    f = row.get('fit')
    if isinstance(f, str) and f.strip():
        try:
            f = json.loads(f)
        except Exception:
            f = None
    if isinstance(f, dict):
        acct = str(f.get('account_name') or '').strip()
        if acct:
            return acct
    scraped = str(row.get('company_name') or '').strip()
    bad = {'', '?', 'unknown', 'unknown company', 'nan', 'none', 'n/a'}
    if scraped and scraped.lower() not in bad:
        return scraped

    cd = row.get('companies_data')
    if cd is None or (isinstance(cd, float) and cd != cd):
        return 'Unknown Company'
    if isinstance(cd, str):
        try:
            import json as _json
            cd = _json.loads(cd) if cd.strip() else []
        except Exception:
            return 'Unknown Company'
    if not isinstance(cd, list) or not cd:
        return 'Unknown Company'

    primary_roles = {
        'acquirer', 'portfolio company',
        'hiring company', 'primary', 'target',
    }
    primary = next(
        (c for c in cd if str(c.get('role','')).lower() in primary_roles),
        cd[0]
    )
    name = (primary.get('name') or '').strip()
    return name if name else 'Unknown Company'


def render_event_card(row, event_config, key_prefix: str = ''):
    """Render a single event card with modern styling."""
    status = row.get('lead_status', 'NEW') or 'NEW'
    title = str(row.get('title', ''))[:100]
    company = _resolve_display_company(row)
    published = row.get('published_date', '')

    # Format date. Guard NaN (float) — it's truthy and used to render "📅 nan".
    date_display = ''
    if published is not None and not (isinstance(published, float) and published != published):
        try:
            if isinstance(published, str):
                dt = datetime.fromisoformat(published.replace('Z', '+00:00'))
            else:
                dt = published
            date_display = dt.strftime('%b %d, %Y')
        except Exception:
            s = str(published)[:10]
            date_display = '' if s in ('nan', 'NaT', 'None') else s

    status_cfg = STATUS_CONFIG.get(status, STATUS_CONFIG["NEW"])
    badge_class = event_config.get('badge_class', 'badge-other')

    # TAL grade badge — solid colored pill, prominent. A=green, B=blue,
    # C=amber, D=grey. White text on solid bg + drop shadow for visibility.
    grade_raw = row.get('grade')
    grade = str(grade_raw).strip().upper() if grade_raw else ''
    grade_colors = GRADE_COLORS
    # V11 grade descriptions (point-based scoring)
    grade_descriptions = {
        'A': 'Grade A — Hot lead (score 8+ with high-intent trigger)',
        'B': 'Grade B — Strong lead (score 5-7)',
        'C': 'Grade C — Warm lead (score 2-4)',
        'D': 'Grade D — Cold lead (score 0-1)',
    }

    # V11 fields — score + confidence shown in tooltip; NaN-safe.
    score_raw = row.get('numeric_score')
    try:
        score = int(score_raw) if score_raw is not None and (
            not isinstance(score_raw, float) or score_raw == score_raw
        ) else None
    except (TypeError, ValueError):
        score = None
    conf_raw = row.get('confidence_level')
    conf = (str(conf_raw).strip() if conf_raw and (
        not isinstance(conf_raw, float) or conf_raw == conf_raw
    ) else None)

    grade_html = ""
    if grade in grade_colors:
        color = grade_colors[grade]
        _acct_for_tip = _resolve_display_company(row)
        desc = f"TAL grade for {_acct_for_tip} — " + grade_descriptions[grade]
        # Append V11 metadata to the tooltip if present
        if score is not None:
            desc += f"  ·  Score: {score}"
        if conf:
            desc += f"  ·  Confidence: {conf}"
        grade_html = (
            f'<span title="{desc}" '
            f'style="display:inline-flex;align-items:center;justify-content:center;'
            f'padding:0.35rem 0.75rem;border-radius:6px;'
            f'background:{color};color:#ffffff;'
            f'font-weight:800;font-size:0.82rem;letter-spacing:0.08em;'
            f'box-shadow:0 2px 6px rgba(0,0,0,0.25);'
            f'margin-right:0.55rem;text-transform:uppercase;'
            f'cursor:help;">Grade {grade}</span>'
        )
        # Small score chip next to the grade if V11 data is present
        if score is not None:
            score_color = "rgba(255,255,255,0.85)"
            grade_html += (
                f'<span title="Numeric score (TAL V11). Hashtag points sum to {score}." '
                f'style="display:inline-flex;align-items:center;justify-content:center;'
                f'padding:0.25rem 0.5rem;border-radius:4px;'
                f'background:rgba(78,140,170,0.15);color:{score_color};'
                f'font-size:0.72rem;font-weight:700;'
                f'margin-right:0.4rem;cursor:help;">'
                f'Score {score}</span>'
            )
        if conf:
            conf_colors_chip = {
                'High':   ('#10b981', 'rgba(16,185,129,0.15)'),
                'Medium': ('#f59e0b', 'rgba(245,158,11,0.15)'),
                'Low':    ('#6b7280', 'rgba(107,114,128,0.18)'),
            }
            fg, bg = conf_colors_chip.get(conf.title(), ('#9ca3af', 'rgba(156,163,175,0.15)'))
            grade_html += (
                f'<span title="Grading confidence (TAL V11)" '
                f'style="display:inline-flex;align-items:center;justify-content:center;'
                f'padding:0.25rem 0.5rem;border-radius:4px;'
                f'background:{bg};color:{fg};'
                f'font-size:0.72rem;font-weight:700;'
                f'margin-right:0.4rem;cursor:help;">'
                f'{conf}</span>'
            )

    # Hashtag chips (max 6, from companies_data grading step)
    hashtags_raw = row.get('hashtags') or []
    if isinstance(hashtags_raw, str):
        try:
            hashtags_raw = json.loads(hashtags_raw) if hashtags_raw.strip() else []
        except Exception:
            hashtags_raw = []
    if isinstance(hashtags_raw, float) and hashtags_raw != hashtags_raw:  # NaN
        hashtags_raw = []
    hashtags_html = ""
    if hashtags_raw and isinstance(hashtags_raw, list):
        chip_spans = "".join(
            f'<span style="font-size:0.68rem;background:rgba(78,140,170,0.18);'
            f'color:#9cd0e6;padding:0.15rem 0.45rem;border-radius:4px;'
            f'margin:0 0.2rem 0.2rem 0;display:inline-block;">{h}</span>'
            for h in hashtags_raw
        )
        hashtags_html = (
            f'<div style="margin-top:0.4rem;">{chip_spans}</div>'
        )

    # ⚠️ Fit-verification flag — fit gates couldn't confirm territory/
    # revenue/vertical. Rep can usually resolve in a 10-second LinkedIn
    # check. (Policy per A.J. 2026-07-16: flag unknowns, don't hide them.)
    fit_html = ""
    fit_raw = _parse_json_field(row.get('fit'), None)
    if fit_verdict(row) == 'staged':
        # v2: never researched / vertical unknown. Only visible when the
        # "Show unverified accounts" toggle is ON.
        fit_html = (
            f'<span title="Not yet researched — vertical and territory are '
            f'unconfirmed. Hidden from the default (verified-only) view." '
            f'style="display:inline-flex;align-items:center;'
            f'padding:0.25rem 0.55rem;border-radius:6px;'
            f'background:rgba(156,163,175,0.18);color:#d1d5db;'
            f'font-size:0.7rem;font-weight:700;margin-left:0.4rem;'
            f'cursor:help;">🕒 NOT YET RESEARCHED</span>'
        )
    elif isinstance(fit_raw, dict) and fit_raw.get('verdict') == 'unverified':
        _unk_dims = [d for d in ('territory', 'revenue', 'vertical')
                     if fit_raw.get(d) == 'unknown']
        if _unk_dims == ['revenue']:
            # Softer flag (A.J. 2026-07-18): territory + vertical ARE
            # confirmed — only the revenue band needs a quick eyeball.
            fit_html = (
                f'<span title="Territory and vertical CONFIRMED — only the '
                f'revenue band is unconfirmed (usually a 5-second check)" '
                f'style="display:inline-flex;align-items:center;'
                f'padding:0.25rem 0.55rem;border-radius:6px;'
                f'background:rgba(96,165,250,0.16);color:#60a5fa;'
                f'font-size:0.7rem;font-weight:700;margin-left:0.4rem;'
                f'cursor:help;">💵 CONFIRM REVENUE</span>'
            )
        else:
            unk = [r for r in (fit_raw.get('reasons') or []) if 'unverified' in r]
            tip = 'Fit not fully confirmed: ' + ('; '.join(unk) or 'verify manually')
            fit_html = (
                f'<span title="{tip}" '
                f'style="display:inline-flex;align-items:center;'
                f'padding:0.25rem 0.55rem;border-radius:6px;'
                f'background:rgba(245,158,11,0.18);color:#fbbf24;'
                f'font-size:0.7rem;font-weight:700;margin-left:0.4rem;'
                f'cursor:help;">⚠️ VERIFY FIT</span>'
            )

    # Aging indicator — how long has this sat in the queue?
    age_html = ""
    disc = row.get('discovered_date')
    if disc is not None and not (isinstance(disc, float) and disc != disc):
        try:
            ddt = datetime.fromisoformat(str(disc).replace('Z', '+00:00'))
            if ddt.tzinfo is not None:
                ddt = ddt.replace(tzinfo=None)
            age_days = (datetime.now() - ddt).days
            if age_days >= 1:
                color = '#f87171' if age_days > 14 else (
                    '#fbbf24' if age_days > 7 else 'var(--text-muted)')
                age_html = (f'<span style="color:{color};" '
                            f'title="Days since discovered">⏳ {age_days}d</span>')
        except Exception:
            pass

    # Unified card: header + expander in one container.
    # IMPORTANT: the HTML is assembled as ONE continuous line. Streamlit
    # renders st.markdown with markdown rules even when unsafe_allow_html
    # is on — an indented line after a blank/whitespace-only line becomes a
    # literal CODE BLOCK. A conditionally-empty placeholder (e.g. age_html
    # for a <1-day-old event) on its own indented template line produced
    # exactly that: raw </div> + chip HTML rendering as code (2026-07-17).
    card_html = (
        f'<div class="event-card-inner">'
        f'<div class="event-card-header">'
        f'{grade_html}<span class="event-type-badge {badge_class}">{event_config["icon"]} {event_config["label"]}</span> '
        f'<span class="status-badge {status_cfg["class"]}">{status_cfg["label"]}</span>{fit_html}'
        f'</div>'
        f'<div class="event-title">{title}</div>'
        f'<div class="event-company"><span>🏢</span> <span>{company}</span></div>'
        f'<div class="event-meta"><span>📅 {date_display}</span> {age_html}</div>'
        f'{hashtags_html}'
        f'</div>'
    )
    with st.container(border=True):
        st.markdown(card_html, unsafe_allow_html=True)

        with st.expander("📝 Details & Actions"):
            col1, col2 = st.columns([2, 1])

            with col1:
                desc = row.get('description', '')
                if desc:
                    st.markdown("**Description**")
                    st.caption(str(desc)[:500] + "..." if len(str(desc)) > 500 else str(desc))

                # ── TAL Grade analysis ────────────────────────────────────
                gj = row.get('grade_justification')
                if gj and isinstance(gj, str) and gj.strip():
                    cfo_s = row.get('cfo_status')
                    cfo_disp = (
                        f"  ·  <span style='color:rgba(255,255,255,0.55);'>"
                        f"CFO: {cfo_s}</span>"
                        if cfo_s and isinstance(cfo_s, str) and cfo_s.strip()
                        else ""
                    )
                    st.markdown(
                        "<div style='margin:10px 0 4px;font-size:0.72rem;"
                        "font-weight:600;color:rgba(255,255,255,0.45);"
                        "letter-spacing:0.08em;text-transform:uppercase;'>"
                        f"TAL Grade {row.get('grade','')}"
                        f"{cfo_disp}</div>",
                        unsafe_allow_html=True
                    )
                    st.markdown(
                        f"<div style='font-size:0.83rem;color:rgba(255,255,255,0.72);"
                        f"font-style:italic;margin-bottom:8px;'>{gj}</div>",
                        unsafe_allow_html=True
                    )

                # ── Research notes (with citations) ───────────────────────
                rn_raw = row.get('research_notes')
                try:
                    if rn_raw is None or (isinstance(rn_raw, float) and rn_raw != rn_raw):
                        notes = []
                    elif isinstance(rn_raw, str):
                        notes = json.loads(rn_raw) if rn_raw.strip() else []
                    elif isinstance(rn_raw, list):
                        notes = rn_raw
                    else:
                        notes = []
                except Exception:
                    notes = []

                if notes:
                    st.markdown(
                        "<div style='margin:10px 0 4px;font-size:0.72rem;"
                        "font-weight:600;color:rgba(255,255,255,0.45);"
                        "letter-spacing:0.08em;text-transform:uppercase;'>"
                        "Research Notes</div>",
                        unsafe_allow_html=True
                    )
                    for n in notes[:5]:
                        if not isinstance(n, dict): continue
                        finding = (n.get('finding') or '').strip()
                        src = (n.get('source_url') or '').strip()
                        if not finding: continue
                        if src:
                            from urllib.parse import urlparse as _up
                            try:
                                domain = _up(src).netloc.replace('www.','') or src[:30]
                            except Exception:
                                domain = src[:30]
                            st.markdown(
                                f"<div style='font-size:0.8rem;color:rgba(255,255,255,0.78);"
                                f"margin:3px 0;'>• {finding} "
                                f"<a href='{src}' target='_blank' style='color:#9cd0e6;"
                                f"text-decoration:none;font-size:0.72rem;'>"
                                f"[{domain}]</a></div>",
                                unsafe_allow_html=True
                            )
                        else:
                            st.markdown(
                                f"<div style='font-size:0.8rem;color:rgba(255,255,255,0.78);"
                                f"margin:3px 0;'>• {finding}</div>",
                                unsafe_allow_html=True
                            )

                # ── Company Intel (multi-company enrichment) ──────────────
                _raw = row.get('companies_data')
                # Guard against pandas NaN, None, empty string
                try:
                    if _raw is None or (isinstance(_raw, float) and _raw != _raw):
                        companies_data = []
                    elif isinstance(_raw, str):
                        companies_data = json.loads(_raw) if _raw.strip() else []
                    elif isinstance(_raw, list):
                        companies_data = _raw
                    else:
                        companies_data = []
                except Exception:
                    companies_data = []

                def _v(val):
                    """Return None for any nullish value."""
                    s = str(val).strip() if val is not None else ''
                    return s if s and s.lower() not in ('none','null','nan','') else None

                if companies_data:
                    st.markdown(
                        "<div style='margin:10px 0 6px;font-size:0.72rem;"
                        "font-weight:600;color:rgba(255,255,255,0.45);"
                        "letter-spacing:0.08em;text-transform:uppercase;'>"
                        "Companies Involved</div>",
                        unsafe_allow_html=True
                    )
                    acct_dispos = st.session_state.get('acct_dispos')
                    for _co_idx, co in enumerate(companies_data):
                        co_name     = _v(co.get('name'))
                        co_role     = _v(co.get('role'))
                        co_url      = _v(co.get('url'))
                        co_industry = _v(co.get('industry'))
                        co_size     = _v(co.get('size'))
                        co_revenue  = _v(co.get('revenue'))
                        co_hq       = _v(co.get('hq'))
                        co_linkedin = _v(co.get('linkedin'))

                        # Name + role header + per-company FIT chip
                        role_html = (
                            f"<span style='font-size:0.72rem;color:rgba(78,140,170,0.9);"
                            f"font-weight:600;margin-left:6px;'>{co_role}</span>"
                            if co_role else ""
                        )
                        co_fit = co.get('fit') if isinstance(co.get('fit'), dict) else None
                        if co_fit:
                            _v_map = {'pass':   ('✓ FIT', '#10b981'),
                                      'fail':   ('✗ NOT A FIT', '#f87171'),
                                      'unverified': ('⚠ VERIFY', '#fbbf24'),
                                      'staged': ('🕒 NOT RESEARCHED', '#d1d5db')}
                            _txt, _clr = _v_map.get(co_fit.get('verdict'), (None, None))
                            if co_fit.get('verdict') == 'unverified':
                                _co_unk = [d for d in ('territory', 'revenue', 'vertical')
                                           if co_fit.get(d) == 'unknown']
                                if _co_unk == ['revenue']:
                                    _txt, _clr = ('💵 REVENUE?', '#60a5fa')
                            if _txt:
                                import html as _h2
                                _reasons = co_fit.get('reasons') or []
                                _tip = _h2.escape('; '.join(_reasons) or 'all dimensions confirmed', quote=True)
                                role_html += (
                                    f"<span title='{_tip}' style='font-size:0.66rem;"
                                    f"color:{_clr};border:1px solid {_clr};border-radius:4px;"
                                    f"padding:0.05rem 0.35rem;margin-left:8px;cursor:help;"
                                    f"font-weight:700;'>{_txt}</span>"
                                )
                                # Inline compact reason — visible, not tooltip-only
                                # (per A.J. 2026-07-17). HQ/revenue already show
                                # in the chips row, so keep these terse.
                                _short = []
                                _unconfirmed = []
                                for _r in _reasons:
                                    if _r.startswith('HQ out of territory'):
                                        _short.append('out of territory')
                                    elif _r.startswith('revenue Enterprise'):
                                        _short.append('>$100M revenue')
                                    elif _r.startswith('subindustry OTHER'):
                                        _short.append('off-vertical industry')
                                    elif _r.endswith('unverified'):
                                        _unconfirmed.append(_r.replace(' unverified', ''))
                                if _unconfirmed:
                                    _short.append("couldn't confirm " + ', '.join(_unconfirmed))
                                if _short:
                                    role_html += (
                                        f"<span style='font-size:0.7rem;"
                                        f"color:rgba(255,255,255,0.45);margin-left:7px;"
                                        f"font-style:italic;'>{_h2.escape(' · '.join(_short))}</span>"
                                    )
                        # Per-company TAL grade chip (the grade belongs to the
                        # ACCOUNT — each workable company carries its own)
                        co_tal = co.get('tal') if isinstance(co.get('tal'), dict) else None
                        if co_tal and co_tal.get('grade') in ('A', 'B', 'C', 'D'):
                            _g = co_tal['grade']
                            _gc = {'A': '#10b981', 'B': '#3b82f6',
                                   'C': '#f59e0b', 'D': '#6b7280'}[_g]
                            _sc = co_tal.get('score')
                            _sc_txt = f" · {_sc}" if _sc is not None else ""
                            role_html += (
                                f"<span title='TAL grade for this account' "
                                f"style='font-size:0.66rem;background:{_gc};"
                                f"color:#fff;border-radius:4px;padding:0.08rem 0.4rem;"
                                f"margin-left:8px;font-weight:800;'>{_g}{_sc_txt}</span>"
                            )
                        st.markdown(
                            f"<div style='margin:4px 0 2px;'>"
                            f"<span style='font-size:0.9rem;font-weight:600;"
                            f"color:rgba(255,255,255,0.88);'>{co_name or '—'}</span>"
                            f"{role_html}</div>",
                            unsafe_allow_html=True
                        )

                        # Chips row — each chip is (text, tooltip). The
                        # revenue chip gets a tooltip showing the segment's
                        # dollar range AND the source URL the LLM cited.
                        import html as _html
                        from urllib.parse import urlparse as _urlparse

                        chips = []  # list of (display_text, tooltip)
                        if co_industry: chips.append((f"🏭 {co_industry}", ''))
                        if co_size:     chips.append((f"👥 {co_size}", ''))
                        if co_revenue:
                            seg = _coerce_revenue_band(co_revenue) or co_revenue
                            rev_idx = _band_idx(seg)
                            in_band = 0 <= rev_idx <= 2  # LMM, MM, Corp
                            rev_emoji = '💵' if in_band else '🏛️'

                            # Build tooltip: range + source citation
                            tooltip_parts = []
                            range_txt = BAND_RANGE.get(seg, '')
                            if range_txt:
                                tooltip_parts.append(f"{seg} = {range_txt}")
                            src = _v(co.get('revenue_source'))
                            if src:
                                try:
                                    domain = _urlparse(src).netloc.replace('www.', '') or src
                                except Exception:
                                    domain = src
                                tooltip_parts.append(f"Source: {domain}")
                            elif co_revenue and co_revenue != seg:
                                # Show original raw value if it differed (e.g. "$27.9B" → "Enterprise")
                                tooltip_parts.append(f"Reported: {co_revenue}")
                            tooltip = ' · '.join(tooltip_parts)

                            chips.append((f"{rev_emoji} {seg}", tooltip))
                        if co_hq:       chips.append((f"📍 {co_hq}", ''))

                        if chips:
                            rendered = []
                            for txt, tip in chips:
                                style = "font-size:0.78rem;color:rgba(255,255,255,0.65);"
                                if tip:
                                    style += "cursor:help;border-bottom:1px dotted rgba(255,255,255,0.35);"
                                title_attr = (
                                    f' title="{_html.escape(tip, quote=True)}"'
                                    if tip else ''
                                )
                                rendered.append(
                                    f"<span style='{style}'{title_attr}>{txt}</span>"
                                )
                            st.markdown(
                                "  <span style='color:rgba(255,255,255,0.35);'>·</span>  ".join(rendered),
                                unsafe_allow_html=True
                            )

                        # Per-company links
                        g_query = urllib.parse.quote((co_name or '') + ' company')
                        li_search = (
                            f"https://www.linkedin.com/search/results/companies/?"
                            f"keywords={urllib.parse.quote(co_name or '')}"
                        )
                        co_buttons = []
                        if co_url:      co_buttons.append(("🌐 Website",  co_url))
                        if co_linkedin: co_buttons.append(("💼 LinkedIn", co_linkedin))
                        elif co_name:   co_buttons.append(("💼 LinkedIn", li_search))
                        if co_name:     co_buttons.append(("🔍 Google",   f"https://www.google.com/search?q={g_query}"))

                        if co_buttons:
                            btn_cols = st.columns(len(co_buttons))
                            for bcol, (blabel, bhref) in zip(btn_cols, co_buttons):
                                with bcol:
                                    st.link_button(blabel, bhref, use_container_width=True)

                        # ── Per-ACCOUNT disposition (follows the company
                        # across every event it appears in). Auto-saves on
                        # change; any status = "decided" and the account's
                        # events drop out of the active queue on next refresh.
                        # Confirmed NOT-A-FIT companies get NO selector — the
                        # fit gate already dispositioned them (A.J. 2026-07-17);
                        # a human re-verdict would be redundant clutter.
                        if (co_name and acct_dispos is not None
                                and not (co_fit and co_fit.get('verdict') == 'fail')):
                            wkey = f"{key_prefix}acct_{row['id']}_{_co_idx}"
                            cur = _dispo_for(acct_dispos, co_name) or {}
                            cur_status, cur_reason = cur.get('status'), cur.get('reason')
                            opts = ['—'] + ACCOUNT_STATUSES
                            # Phase 4 2026-09-08: status + reason (required
                            # for Not a Fit / Out of Alignment) + notes, all
                            # auto-saving through one callback. The widget's
                            # own state wins once it exists — a 'Not a Fit'
                            # the callback REFUSED (no reason yet) must stay
                            # selected so the rep can add the reason, not
                            # snap back to the saved row.
                            pending = st.session_state.get(
                                wkey, cur_status if cur_status in opts else '—')
                            needs_reason = pending in REASON_REQUIRED_STATUSES
                            sel_cols = st.columns([1, 1.3, 1.3] if needs_reason else [1, 2])
                            with sel_cols[0]:
                                why = (f" · {reason_label(cur_reason)}"
                                       if cur_reason and not needs_reason else '')
                                st.caption(f"Account status — {co_name[:30]}{why}")
                            with sel_cols[1]:
                                st.selectbox(
                                    f"acct status {co_name}",
                                    opts,
                                    index=opts.index(cur_status) if cur_status in opts else 0,
                                    key=wkey,
                                    label_visibility="collapsed",
                                    on_change=_on_account_dispo_change,
                                    args=(wkey, co_name),
                                )
                            if needs_reason:
                                r_opts = [''] + list(DISPOSITION_REASONS)
                                with sel_cols[2]:
                                    st.selectbox(
                                        f"acct reason {co_name}",
                                        r_opts,
                                        index=r_opts.index(cur_reason) if cur_reason in r_opts else 0,
                                        format_func=lambda c: reason_label(c) if c else '— reason (required) —',
                                        key=wkey + '_reason',
                                        label_visibility="collapsed",
                                        on_change=_on_account_dispo_change,
                                        args=(wkey, co_name),
                                    )
                                if not (st.session_state.get(wkey + '_reason') or cur_reason):
                                    st.caption("⚠️ Pick a reason — the status is not saved until you do.")
                            if pending != '—':
                                st.text_input(
                                    f"acct notes {co_name}",
                                    value=cur.get('notes') or '',
                                    key=wkey + '_notes',
                                    label_visibility="collapsed",
                                    placeholder="Disposition notes (optional) — Enter to save",
                                    on_change=_on_account_dispo_change,
                                    args=(wkey, co_name),
                                )

                        st.markdown(
                            "<div style='border-top:1px solid rgba(255,255,255,0.07);"
                            "margin:8px 0 6px;'></div>",
                            unsafe_allow_html=True
                        )

                # ── Article link (always shown) ───────────────────────────
                url = row.get('url', '')
                company = str(row.get('company_name') or '').strip()
                company = '' if company.lower() in ('nan', 'none', 'unknown company') else company

                # Fallback links when enrichment hasn't run yet
                li_url = (
                    f"https://www.linkedin.com/search/results/companies/?keywords="
                    f"{urllib.parse.quote(company)}" if company else ""
                )
                g_url = (
                    f"https://www.google.com/search?q={urllib.parse.quote(company + ' company')}"
                    if company else ""
                )

                fallback_buttons = []
                if url:    fallback_buttons.append(("🔗 Source Article", url))
                # Only show generic links if no enrichment data yet
                if not companies_data:
                    if li_url: fallback_buttons.append(("💼 LinkedIn", li_url))
                    if g_url:  fallback_buttons.append(("🔍 Google",   g_url))
                elif url:
                    pass  # article link already in fallback_buttons above

                if fallback_buttons:
                    fb_cols = st.columns(len(fallback_buttons))
                    for fcol, (flabel, fhref) in zip(fb_cols, fallback_buttons):
                        with fcol:
                            st.link_button(flabel, fhref, use_container_width=True)

            with col2:
                # Event-level controls, slimmed 2026-07-17: account dispositions
                # (per-company selectors on the left) are THE workflow now —
                # the old event Status dropdown duplicated the same choices.
                # The only event-level verdict with distinct meaning survives:
                # killing a junk ARTICLE (bad match, syndication noise,
                # irrelevant story) without passing judgment on the company.
                notes = st.text_area(
                    "Notes",
                    value=row.get('notes') or "",
                    key=f"{key_prefix}notes_{row['id']}",
                    height=80,
                    placeholder="Add notes..."
                )

                if st.button("💾 Save note", key=f"{key_prefix}save_{row['id']}",
                             use_container_width=True):
                    client = get_supabase_client()
                    if client:
                        try:
                            client.table('events').update(
                                {'notes': notes}).eq('id', row['id']).execute()
                            st.success("✓ Saved!")
                        except Exception as e:
                            st.error(f"Save failed: {e}")

                st.caption("Junk article? (kills this event only — "
                           "doesn't judge the company)")
                if st.button("🚫 Not Relevant — this event",
                             key=f"{key_prefix}nr_{row['id']}",
                             use_container_width=True):
                    if update_lead_status(row['id'], "NOT RELEVANT", notes):
                        st.success("✓ Event removed!")
                        st.rerun()


def render_event_section(df, event_type, event_config, lead_filter,
                         include_unknown_types: bool = False):
    """Render a section for a specific event type. With
    include_unknown_types=True (the "Other" tab) the section also absorbs
    any event_type that has no EVENT_TYPES entry, so a new or legacy type
    always lands somewhere instead of vanishing from every tab."""
    # Filter by event type
    if include_unknown_types:
        known_others = [t for t in EVENT_TYPES if t != event_type]
        type_df = df[~df['event_type'].isin(known_others)]
    else:
        type_df = df[df['event_type'] == event_type]

    # Apply lead status filter
    if lead_filter:
        type_df = type_df[type_df['lead_status'].isin(lead_filter)]

    # Modern section header
    full_label = event_config.get('full_label', event_config['label'])
    st.markdown(f"""
        <div class="section-header">
            <span style="font-size: 1.5rem;">{event_config['icon']}</span>
            <h2>{full_label}</h2>
            <span class="section-count">{len(type_df)}</span>
        </div>
    """, unsafe_allow_html=True)

    if type_df.empty:
        st.info(f"No {full_label.lower()} found matching your filters.")
        return

    # Render each event card
    for idx, row in type_df.iterrows():
        render_event_card(row, event_config)


def render_source_status_table(df: pd.DataFrame):
    """Render the source status table with colored indicators."""
    if df.empty:
        st.info("No source status data available. Run the scraper to populate.")
        return

    # Group by source type
    source_types = {
        'rss_feed': 'RSS Feeds',
        'google_news': 'Google News',
        'job_board': 'Job Boards'
    }

    # Calculate summary stats
    total_sources = len(df)
    success_count = len(df[df['status'] == 'success'])
    error_count = len(df[df['status'] == 'error'])
    partial_count = len(df[df['status'] == 'partial'])

    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Sources", total_sources)
    with col2:
        st.metric("Working", success_count, delta=None)
    with col3:
        st.metric("Partial", partial_count, delta=None)
    with col4:
        st.metric("Failed", error_count, delta=None if error_count == 0 else f"-{error_count}")

    # Status indicator function
    def get_status_indicator(status):
        if status == 'success':
            return '🟢'
        elif status == 'partial':
            return '🟡'
        else:
            return '🔴'

    # Create tabs for each source type
    type_list = sorted(df['source_type'].unique())
    tab_names = [source_types.get(t, t.replace('_', ' ').title()) for t in type_list]
    tabs = st.tabs(tab_names)

    for tab, source_type in zip(tabs, type_list):
        with tab:
            type_df = df[df['source_type'] == source_type].copy()

            # Format the data for display
            display_data = []
            for _, row in type_df.iterrows():
                status_icon = get_status_indicator(row['status'])
                last_check = row.get('last_check', '')
                if last_check:
                    try:
                        dt = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                        last_check = dt.strftime('%Y-%m-%d %H:%M')
                    except:
                        pass

                display_data.append({
                    'Status': status_icon,
                    'Source': row['source_name'],
                    'Events': row.get('events_found', 0),
                    'Last Check': last_check,
                    'Error': row.get('error_message', '') or ''
                })

            display_df = pd.DataFrame(display_data)

            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    'Status': st.column_config.TextColumn('Status', width='small'),
                    'Source': st.column_config.TextColumn('Source', width='medium'),
                    'Events': st.column_config.NumberColumn('Events', width='small'),
                    'Last Check': st.column_config.TextColumn('Last Check', width='medium'),
                    'Error': st.column_config.TextColumn('Error', width='large')
                }
            )


def get_stats(df) -> dict:
    """Get dashboard statistics."""
    if df.empty:
        return {"total": 0, "by_type": {}, "new": 0}

    return {
        "total": len(df),
        "by_type": df['event_type'].value_counts().to_dict(),
        "new": len(df[df['lead_status'] == 'NEW'])
    }


# ── Account-level dispositions ────────────────────────────────────────────
# The disposition object is the COMPANY (account), not the event: one M&A
# event surfaces 2+ companies, each deserving its own verdict, and a verdict
# follows the company across every event it appears in. Any disposition
# means "decided — get it out of my queue" (per A.J. 2026-07-17), so events
# whose primary company is dispositioned disappear from the active views.
# Vocabulary (Phase 4 2026-09-08): src/pipeline/accounts.py owns it. The
# literals here are the fallback while that module is absent and MUST stay
# equal to accounts.ACCOUNT_STATUSES / DISPOSITION_REASONS — the tests
# compare the two whenever the module is importable.
_FALLBACK_ACCOUNT_STATUSES = ('Picked Up', 'On Rep TAL', 'NetSuite Customer',
                              'Out of Alignment', 'Not a Fit')
_FALLBACK_DISPOSITION_REASONS = ('wrong_vertical', 'out_of_territory', 'too_big', 'too_small',
                                 'not_a_trigger', 'duplicate', 'existing_customer', 'other')
ACCOUNT_STATUSES = list(getattr(_accounts, 'ACCOUNT_STATUSES', None) or _FALLBACK_ACCOUNT_STATUSES)
DISPOSITION_REASONS = tuple(getattr(_accounts, 'DISPOSITION_REASONS', None)
                            or _FALLBACK_DISPOSITION_REASONS)
# Human labels for the reason codes (the code is what gets stored; the
# pivot in the Scorecard and the golden set are built on it).
_FALLBACK_DISPOSITION_REASON_LABELS = {
    'wrong_vertical': 'Wrong vertical',
    'out_of_territory': 'Out of territory',
    'too_big': 'Too big',
    'too_small': 'Too small',
    'not_a_trigger': 'Not a real trigger',
    'duplicate': 'Duplicate',
    'existing_customer': 'Already a customer',
    'other': 'Other',
}
DISPOSITION_REASON_LABELS = dict(getattr(_accounts, 'DISPOSITION_REASON_LABELS', None)
                                 or _FALLBACK_DISPOSITION_REASON_LABELS)
# A dismissal is a tuning signal only when it says WHY (A.J.: "dismissals
# require a reason code") — these two statuses are refused without one.
_FALLBACK_REASON_REQUIRED_STATUSES = frozenset({'Not a Fit', 'Out of Alignment'})
REASON_REQUIRED_STATUSES = frozenset(getattr(_accounts, 'REASON_REQUIRED_STATUSES', None)
                                     or _FALLBACK_REASON_REQUIRED_STATUSES)
ACCOUNT_DISPO_MIGRATION_SQL = (
    "create table if not exists account_dispositions (\n"
    "  company_key text primary key,\n"
    "  company_name text,\n"
    "  status text not null,\n"
    "  notes text,\n"
    "  updated_at timestamptz default now()\n"
    ");"
)


def reason_label(code) -> str:
    """Human label for a disposition reason code. An unknown code reads as
    itself (a new code in the accounts module never blanks the UI)."""
    c = str(code or '').strip()
    if not c or c.lower() in ('none', 'nan'):
        return ''
    return DISPOSITION_REASON_LABELS.get(c) or c.replace('_', ' ').capitalize()


def disposition_error(status, reason) -> Optional[str]:
    """Why a (status, reason) pair can't be saved, or None when it can.
    Pure — the receipt banner shows this text verbatim. Clearing (no
    status / '—') is always allowed; an unknown status or reason code is
    refused (typo-proof), and Not a Fit / Out of Alignment need a reason."""
    s = str(status or '').strip()
    if not s or s == '—':
        return None
    if s not in ACCOUNT_STATUSES:
        return f"'{s}' is not an account status ({', '.join(ACCOUNT_STATUSES)})"
    r = str(reason or '').strip()
    if r and r not in DISPOSITION_REASONS:
        return f"'{r}' is not a disposition reason ({', '.join(DISPOSITION_REASONS)})"
    if s in REASON_REQUIRED_STATUSES and not r:
        return (f"'{s}' needs a reason — pick one next to the status "
                f"({', '.join(reason_label(c) for c in DISPOSITION_REASONS)})")
    return None


def _legacy_account_key(name) -> str:
    """The v1 normalizer (2026-07-17), kept verbatim: every key in the
    legacy account_dispositions table was written with it, so while the
    accounts module is absent this is what keeps those rows matching."""
    s = str(name or '').strip().lower()
    # Strip common suffixes so "Acme Inc." and "Acme" collide intentionally
    for suf in (', inc.', ', inc', ' inc.', ' inc', ', llc', ' llc',
                ', ltd.', ' ltd.', ' ltd', ' corp.', ' corp', ' co.', ' company'):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s.strip(' .,')


def _account_key(name) -> str:
    """Normalize a company name into a stable account key.

    Phase 4 2026-09-08: delegates to accounts.account_key — which IS
    src.pipeline.gates.account_key, the ONE normalizer enrichment, the
    typed `account_key` column, the accounts table and its backfill all
    use — so this file's lookups hit the keys the pipeline writes. The v1
    normalizer is the fallback only while that module is absent (its keys
    are what the legacy table holds); the two are never mixed in one run."""
    if _accounts is not None:
        return _accounts.account_key(name)
    return _legacy_account_key(name)


# Scorecard query (Phase 3 2026-09-08). ONE cheap paginated read feeds the
# 7d-vs-prior-7d metrics, the Supply pivot and the 28-day vertical mix: the
# typed columns are short strings, so 28 days is ~1.1k rows × 16 columns —
# no JSONB blob. `verdict_json` is fit->>verdict selected as a scalar (a
# PostgREST JSON-path alias, validated read-only 2026-09-08) so a row an
# in-flight enricher wrote with verify_state NULL is still judged by its
# verdict without downloading `fit`.
SCORECARD_DAYS = 28
SCORECARD_LEGACY_COLUMNS = ('id,discovered_at,blocked_at,blocked_reason,grade,'
                            'source_url,title,event_type,hashtags')
SCORECARD_TYPED_COLUMNS = (SCORECARD_LEGACY_COLUMNS +
                           ',source,verify_state,fit_verdict,zi_subindustry,'
                           'classified_by,account_key,verdict_json:fit->>verdict')


def _load_scorecard_rows(client, present, now=None, days=SCORECARD_DAYS):
    """Pure loader behind load_scorecard_events (client + probed column set
    injected so tests can drive it). Typed select first once the migration
    has landed; the probe can be an hour stale, so a typed select that fails
    anyway falls back to the legacy column list instead of blanking the
    scorecard. Pages are ORDERED (discovered_at, id) — a total order — so
    concurrent enrichment UPDATEs can't reshuffle rows between pages
    (review 2026-09-07, same fix as monitor_health._fetch_recent_events)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    selects = [SCORECARD_LEGACY_COLUMNS]
    if {'source', 'verify_state'} <= set(present or ()):
        selects.insert(0, SCORECARD_TYPED_COLUMNS)
    for sel in selects:
        try:
            rows, off = [], 0
            while True:
                q = (client.table('events').select(sel).gte('discovered_at', since)
                     .order('discovered_at').order('id').range(off, off + 999).execute())
                page = q.data or []
                rows += page
                if len(page) < 1000:
                    return rows
                off += 1000
        except Exception:
            continue
    return []


@st.cache_data(ttl=900)
def load_scorecard_events():
    """Last SCORECARD_DAYS days of pipeline activity (visible AND tombstoned)
    for the Weekly Scorecard — one cheap query, cached 15 min."""
    client = get_supabase_client()
    if not client:
        return []
    return _load_scorecard_rows(client, typed_columns_present())


def _scorecard_src(row_or_url):
    """Bucket an event into the ONE source vocabulary — src.pipeline.sources
    .source_label, the same function the Supply pivot, the yield table and
    monitor_health use. Accepts a row dict (typed `source` preferred, then
    URL host + title) or, for older callers, a bare source_url string.

    Review 2026-09-08: this used to carry its own label table ('SEC EDGAR',
    a bare host for every Phase 3 feed) under a comment claiming it agreed
    with the pivot below it. It did not: the same expander said 'SEC EDGAR'
    in "New events by source" and 'SEC 8-K' / 'SEC Form D' /
    'NH Business Review' in the supply pivot. Delegating is the only way
    the two stay equal — there is no second table to drift.
    """
    if hasattr(row_or_url, 'get'):
        return source_label(row_or_url)
    return source_label({'source_url': row_or_url})


# ── Supply visibility (Phase 3 2026-09-08) ───────────────────────────────────
# Phase 3 adds supply — press-release personnel feeds, regional business
# journals, the fixed Google News scraper, the sec_iapd adviser source, and
# free oracles that verify banks / RIAs / nonprofits without a search. Its
# acceptance bar (the plan): finance-leader (CFO / Controller) triggers
# ≥ 30% of new intake with no single source above 40% of them, nonprofits
# verified without search > 50%, and a per-vertical mix report every week.
# Everything below is a pure function over plain dicts so tests drive it
# with synthetic rows. monitor_health.py measures the same numbers on
# Mondays; both sides bucket with src.pipeline.sources and the same vertical
# and provenance rules, so the numbers agree.
SUPPLY_PIVOT_DAYS = 7
SUPPLY_TOP_SOURCES = 6              # pivot columns before the 'other' fold
FINANCE_LEADER_TARGET_PCT = 30      # of survivors (Phase 3 plan)
TOP_SOURCE_CEILING_PCT = 40         # of finance-leader survivors (= monitor_health.CONCENTRATION_WARN_PCT)
NONPROFIT_NO_SEARCH_TARGET_PCT = 50
# Fewer nonprofits WITH provenance than this and the share is arithmetic,
# not a signal — one searched account painted the line red at "0% (0 of 1)"
# (review 2026-09-08). Same floor as monitor_health.CONCENTRATION_MIN_ROWS.
SHARE_JUDGE_MIN_N = 5
# classified_by values that mean "verified without spending a search":
# 'structured' (SEC SIC / Form D fields) and 'oracle' (Phase 3 registries:
# SEC IAPD advisers, FDIC banks, nonprofits). 'article' is free too (one
# local LLM read) but it is a model's guess, not a registry hit, so it stays
# out — this number tracks the oracles. 'cache' is OUT as well (review
# 2026-09-08): enrichment_scout stamps 'cache' when the account cache
# supplies the subindustry, i.e. it REPLAYS the classification stored the
# first time the account was researched — usually by a search — so counting
# it made the "without search" share climb with every repeat event for a
# known account, not with the oracles. monitor_health.NO_SEARCH_CLASSIFIERS
# is the same set; the shared-thresholds test keeps them equal.
NO_SEARCH_CLASSIFIERS = frozenset({'oracle', 'structured'})
UNKNOWN_VERTICAL = 'Unknown'
# Taxonomy order = the order the FY27 sheet lists the verticals. The literal
# fallback only keeps the mix table's shape when the taxonomy import failed
# (see the guarded import at the top) — every account is 'Unknown' then.
VERTICAL_ORDER = (list(dict.fromkeys(ZI_SUBINDUSTRIES.values())) or
                  ['Financial Services', 'Nonprofits & Organizations', 'Consumer Services'])
FINANCE_LEADER_ROLLUP = 'Finance leader (all)'
ALL_SURVIVORS = 'All survivors'
# The family sub-rows are mutually exclusive with each other AND with the
# per-type rows, so sub-rows + per-type rows add up to ALL_SURVIVORS and the
# roll-up is exactly its three sub-rows. A #NewController row keeps its own
# event_type (executive_hire by design) but is counted here, not there.
_FAMILY_ROWS = (('cfo_hire', '↳ CFO hire'),
                ('finance_seat_open', '↳ Open finance seat'),
                ('controller_tag', '↳ Controller hire (#NewController)'))
_TRIGGER_ORDER = ('merger_acquisition', 'funding', 'executive_hire', 'expansion',
                  'stable_target')


def _clean_str(v) -> str:
    """str(v).strip().lower(); '' for None / NaN (pandas rows carry NaN)."""
    if v is None or (isinstance(v, float) and v != v):
        return ''
    return str(v).strip().lower()


def _aware(now) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def vertical_of(zi) -> str:
    """ZoomInfo subindustry → NSCorp vertical. Anything outside the FY27
    taxonomy (None, 'OTHER', a legacy free-text industry) → 'Unknown'."""
    if zi is None or (isinstance(zi, float) and zi != zi):
        return UNKNOWN_VERTICAL
    return ZI_SUBINDUSTRIES.get(str(zi).strip(), UNKNOWN_VERTICAL)


def is_verified_row(row) -> bool:
    """verify_state == 'verified'. A row with NO verify_state (written by an
    enricher whose column probe was stale — review 2026-09-07) is judged by
    its verdict instead: the typed fit_verdict, else the `verdict_json`
    alias the scorecard query selects from fit->>verdict, else the fit blob
    when a caller has it. The client-side twin of VERIFIED_FILTER."""
    state = _clean_str(row.get('verify_state'))
    if state:
        return state == 'verified'
    verdict = _clean_str(row.get('fit_verdict')) or _clean_str(row.get('verdict_json'))
    if not verdict:
        fit = _parse_json_field(row.get('fit'), None)
        verdict = _clean_str(fit.get('verdict')) if isinstance(fit, dict) else ''
    return verdict == 'pass'


def classified_by_of(row) -> Optional[str]:
    """How the account's classification was reached: the typed column when
    populated, else the companies_data account entry's classified_by. The
    typed column is FILL-ONLY from that entry, so they agree wherever both
    exist (1,124 of 1,124 rows in the window on 2026-09-08) — which is why
    the scorecard query selects the column and skips the JSONB blob. None
    when neither knows (rows verified before Phase 2 recorded provenance)."""
    v = _clean_str(row.get('classified_by'))
    if v:
        return v
    if row.get('companies_data') is None:
        return None
    acct = _account_company(row) or {}
    return _clean_str(acct.get('classified_by')) or None


def _trigger_key(row) -> tuple:
    """('fl', sub-row) for the finance-leader family, ('', event_type) for
    everything else — one key per survivor, so rows never double count."""
    etype = _clean_str(row.get('event_type')) or 'other'
    if etype in ('cfo_hire', 'finance_seat_open'):
        return ('fl', etype)
    if finance_leader_family(row):
        return ('fl', 'controller_tag')
    return ('', etype)


def _trigger_label(etype: str) -> str:
    """Card vocabulary when the type has one; a type without an EVENT_TYPES
    entry reads as itself instead of vanishing into 'Other'."""
    cfg = EVENT_TYPES.get(etype)
    return cfg['full_label'] if cfg else etype.replace('_', ' ').capitalize()


def supply_pivot(rows, now=None, days=SUPPLY_PIVOT_DAYS,
                 top_sources=SUPPLY_TOP_SOURCES) -> dict:
    """Survivors (blocked_at NULL) by trigger × source, last `days` vs the
    `days` before. → {'sources': [top labels…, 'other'], 'rows': [{'trigger',
    'key', 'cells': {source: (recent, prior)}, 'total': (recent, prior)}…],
    'survival': {'recent': (survivors, rows), 'prior': (survivors, rows)},
    'days'}. Rows: the finance-leader roll-up, its three sub-rows, one row
    per other trigger type present, then ALL_SURVIVORS. Tombstoned rows
    never enter a cell but do count in 'survival', so the rate says how much
    of raw intake the gates let through. Columns are the top sources by
    survivors over both windows; the rest (and the literal 'other' label)
    fold into 'other'."""
    now = _aware(now)
    recent_start = now - timedelta(days=days)
    prior_start = now - timedelta(days=2 * days)
    survival = {'recent': [0, 0], 'prior': [0, 0]}
    cells, src_totals = {}, Counter()
    for r in rows:
        dt = parse_ts(r.get('discovered_at'))
        if dt is None or dt < prior_start:
            continue
        period = 'recent' if dt >= recent_start else 'prior'
        survival[period][1] += 1
        if r.get('blocked_at'):
            continue
        survival[period][0] += 1
        src = source_label(r)
        cell = cells.setdefault((_trigger_key(r), src), [0, 0])
        cell[0 if period == 'recent' else 1] += 1
        src_totals[src] += 1
    ranked = sorted((s for s in src_totals if s != OTHER_SOURCE),
                    key=lambda s: (-src_totals[s], s))
    columns = ranked[:top_sources] + [OTHER_SOURCE]

    def _sum(pred):
        out = {c: [0, 0] for c in columns}
        for (tkey, src), (rc, pr) in cells.items():
            if pred(tkey):
                col = out[src if src in out else OTHER_SOURCE]
                col[0] += rc
                col[1] += pr
        return {c: tuple(v) for c, v in out.items()}

    def _row(label, key, pred):
        c = _sum(pred)
        return {'trigger': label, 'key': key, 'cells': c,
                'total': (sum(v[0] for v in c.values()), sum(v[1] for v in c.values()))}

    out_rows = [_row(FINANCE_LEADER_ROLLUP, 'finance_leader', lambda k: k[0] == 'fl')]
    out_rows += [_row(label, sub, lambda k, sub=sub: k == ('fl', sub))
                 for sub, label in _FAMILY_ROWS]
    present = {tk[1] for tk, _ in cells if tk[0] == ''}
    ordered = [t for t in _TRIGGER_ORDER if t in present]
    ordered += sorted(t for t in present if t not in _TRIGGER_ORDER and t != 'other')
    if 'other' in present:
        ordered.append('other')
    out_rows += [_row(_trigger_label(t), t, lambda k, t=t: k == ('', t)) for t in ordered]
    out_rows.append(_row(ALL_SURVIVORS, 'all', lambda k: True))
    return {'sources': columns, 'rows': out_rows, 'days': days,
            'survival': {p: tuple(v) for p, v in survival.items()}}


def vertical_mix(rows, now=None, days=SCORECARD_DAYS) -> dict:
    """Verified ACCOUNTS (not events) by vertical over the last `days`: one
    account per account_key (fallback: the event id), its newest verified
    event deciding subindustry and provenance. → {'rows': [{'vertical',
    'accounts', 'share_pct', 'provenance_n', 'no_search_n', 'no_search_pct'
    (None when no provenance)}…] in taxonomy order (+ 'Unknown' when any),
    'total', 'provenance_n', 'no_search_n', 'days'}. no_search_pct is over
    the accounts WITH provenance: rows verified before Phase 2 recorded it
    carry None and would otherwise read as "searched"."""
    now = _aware(now)
    start = now - timedelta(days=days)
    newest = {}
    for r in rows:
        if r.get('blocked_at') or not is_verified_row(r):
            continue
        dt = parse_ts(r.get('discovered_at'))
        if dt is None or dt < start:
            continue
        key = _clean_str(r.get('account_key')) or 'id:{}'.format(r.get('id') or id(r))
        if key not in newest or dt > newest[key][0]:
            newest[key] = (dt, r)
    counts, known, no_search = Counter(), Counter(), Counter()
    for _, r in newest.values():
        v = vertical_of(r.get('zi_subindustry'))
        counts[v] += 1
        cb = classified_by_of(r)
        if cb:
            known[v] += 1
            if cb in NO_SEARCH_CLASSIFIERS:
                no_search[v] += 1
    total = sum(counts.values())
    order = list(VERTICAL_ORDER) + ([UNKNOWN_VERTICAL] if counts[UNKNOWN_VERTICAL] else [])
    out = [{'vertical': v, 'accounts': counts[v],
            'share_pct': counts[v] / total * 100 if total else 0.0,
            'provenance_n': known[v], 'no_search_n': no_search[v],
            'no_search_pct': no_search[v] / known[v] * 100 if known[v] else None}
           for v in order]
    return {'rows': out, 'total': total, 'days': days,
            'provenance_n': sum(known.values()), 'no_search_n': sum(no_search.values())}


def supply_headline(rows, now=None, days=SCORECARD_DAYS) -> dict:
    """The two Phase 3 numbers: finance-leader share of survivors, and the
    top source's share OF the finance-leader survivors (the single point of
    failure monitor_health's concentration check watches)."""
    now = _aware(now)
    start = now - timedelta(days=days)
    surv = []
    for r in rows:
        if r.get('blocked_at'):
            continue
        dt = parse_ts(r.get('discovered_at'))
        if dt is not None and dt >= start:
            surv.append(r)
    fam = [r for r in surv if finance_leader_family(r)]
    top = Counter(source_label(r) for r in fam).most_common(1)
    return {'days': days, 'survivors': len(surv), 'family': len(fam),
            'family_pct': len(fam) / len(surv) * 100 if surv else None,
            'top_source': top[0][0] if top else None,
            'top_source_pct': top[0][1] / len(fam) * 100 if fam else None}


# ── Supply rendering ─────────────────────────────────────────────────────────

def _pair_text(pair) -> str:
    return '—' if tuple(pair) == (0, 0) else f'{pair[0]} / {pair[1]}'


def _rate_text(pair) -> str:
    survivors, total = pair
    return f'{survivors / total * 100:.0f}% ({survivors} of {total})' if total else 'n/a (no rows)'


def supply_pivot_frame(pivot: dict) -> pd.DataFrame:
    cols = ['Trigger'] + list(pivot['sources']) + ['Total']
    data = []
    for row in pivot['rows']:
        rec = {'Trigger': row['trigger'], 'Total': _pair_text(row['total'])}
        for s in pivot['sources']:
            rec[s] = _pair_text(row['cells'].get(s, (0, 0)))
        data.append(rec)
    return pd.DataFrame(data, columns=cols)


def _no_search_text(r: dict) -> str:
    if not r['provenance_n']:
        return 'n/a — no provenance recorded'
    return f"{r['no_search_pct']:.0f}% ({r['no_search_n']} of {r['provenance_n']} with provenance)"


def judge_no_search_share(row: dict) -> Optional[bool]:
    """True / False against NONPROFIT_NO_SEARCH_TARGET_PCT, or None ("too
    few to judge", drawn grey) when fewer than SHARE_JUDGE_MIN_N accounts
    carry provenance — 0 of 1 is not a red number (review 2026-09-08)."""
    if row.get('no_search_pct') is None or (row.get('provenance_n') or 0) < SHARE_JUDGE_MIN_N:
        return None
    return row['no_search_pct'] > NONPROFIT_NO_SEARCH_TARGET_PCT


def vertical_mix_frame(mix: dict) -> pd.DataFrame:
    n_col = f"Verified accounts ({mix['days']}d)"
    data = [{'Vertical': r['vertical'], n_col: r['accounts'],
             'Share': f"{r['share_pct']:.0f}%",
             'Verified without search': _no_search_text(r)} for r in mix['rows']]
    total = {'vertical': 'All verticals', 'provenance_n': mix['provenance_n'],
             'no_search_n': mix['no_search_n'],
             'no_search_pct': (mix['no_search_n'] / mix['provenance_n'] * 100
                               if mix['provenance_n'] else None)}
    data.append({'Vertical': total['vertical'], n_col: mix['total'],
                 'Share': '100%' if mix['total'] else '—',
                 'Verified without search': _no_search_text(total)})
    return pd.DataFrame(data, columns=['Vertical', n_col, 'Share', 'Verified without search'])


def _threshold_line(label: str, value: str, ok, hint: str):
    """One line, the number colored by its threshold (grey = no data)."""
    color = '#6b7280' if ok is None else ('#10b981' if ok else '#ef4444')
    st.markdown(f"{label}: <span style='color:{color};font-weight:600'>{value}</span> — {hint}",
                unsafe_allow_html=True)


def render_supply_section(rows, now=None):
    """The Supply block of the Weekly Scorecard (Phase 3 2026-09-08)."""
    now = _aware(now)
    st.markdown("**Supply — where the triggers come from**")
    if not ZI_SUBINDUSTRIES:
        st.warning("Vertical taxonomy unavailable (enrichment_scout.ZI_SUBINDUSTRIES failed "
                   "to import) — every account below shows as Unknown.")
    if rows and 'verify_state' not in rows[0]:
        # The typed select failed and the legacy column list was used (see
        # _load_scorecard_rows): the pivot still works off URL hosts, but no
        # row can be judged verified, so say so instead of showing 0 accounts.
        st.caption("Verification columns not in this query (legacy select) — the vertical "
                   "mix below counts nothing until migration 002 has run.")

    pivot = supply_pivot(rows, now)
    st.caption(f"Survivors by trigger × source — each cell is last {pivot['days']} days / "
               f"prior {pivot['days']} days (blocked rows excluded; the roll-up = its ↳ sub-rows).")
    st.dataframe(supply_pivot_frame(pivot), use_container_width=True, hide_index=True)
    st.caption(f"Survival rate (rows that got past every gate): last {pivot['days']}d "
               f"{_rate_text(pivot['survival']['recent'])} · prior {pivot['days']}d "
               f"{_rate_text(pivot['survival']['prior'])}.")

    head = supply_headline(rows, now)
    fam_ok = None if head['family_pct'] is None else head['family_pct'] >= FINANCE_LEADER_TARGET_PCT
    fam_val = ('n/a' if head['family_pct'] is None else
               f"{head['family_pct']:.0f}% ({head['family']} of {head['survivors']} survivors)")
    _threshold_line(f"Finance-leader share of intake ({head['days']}d)", fam_val, fam_ok,
                    f"target ≥ {FINANCE_LEADER_TARGET_PCT}%")
    top_ok = None if head['top_source_pct'] is None else head['top_source_pct'] <= TOP_SOURCE_CEILING_PCT
    top_val = ('n/a' if head['top_source_pct'] is None else
               f"{head['top_source']} {head['top_source_pct']:.0f}%")
    _threshold_line(f"Top source's share of finance-leader triggers ({head['days']}d)", top_val,
                    top_ok, f"ceiling {TOP_SOURCE_CEILING_PCT}% — above it, one dead feed "
                    f"takes the best trigger with it")

    mix = vertical_mix(rows, now)
    st.caption(f"Verified accounts by vertical (last {mix['days']} days; one row per account, "
               f"its newest verified event decides). 'Without search' = classified_by in "
               f"{', '.join(sorted(NO_SEARCH_CLASSIFIERS))}, over accounts whose provenance "
               f"was recorded.")
    st.dataframe(vertical_mix_frame(mix), use_container_width=True, hide_index=True)
    np_row = next((r for r in mix['rows'] if r['vertical'] == NONPROFIT_VERTICAL), None)
    if np_row is not None:
        np_ok = judge_no_search_share(np_row)
        hint = f"target > {NONPROFIT_NO_SEARCH_TARGET_PCT}%"
        if np_ok is None and np_row['provenance_n']:
            hint += (f"; too few to judge ({np_row['provenance_n']} with provenance, "
                     f"needs {SHARE_JUDGE_MIN_N})")
        _threshold_line(f"Nonprofits verified without search ({mix['days']}d)",
                        _no_search_text(np_row), np_ok, hint)


SCORECARD_WEEK_DAYS = 7
# "Auto-removed as noise (7d)" counts this week's tombstones among rows
# discovered within this many days — the two-week query the card was built
# over (see scorecard_week_buckets).
NOISE_CARD_DISCOVERY_DAYS = 2 * SCORECARD_WEEK_DAYS


def scorecard_week_buckets(rows, now=None) -> dict:
    """The Weekly Scorecard's headline lists over the scorecard rows:
    {'this_wk': discovered in the last 7d, 'last_wk': the 7d before,
    'tomb_wk': tombstoned in the last 7d AND discovered within the last
    NOISE_CARD_DISCOVERY_DAYS}.

    WHY the discovery clause (review 2026-09-08): the "Auto-removed as
    noise (7d)" card was built when the scorecard query spanned 14 days, so
    it always meant "this week's tombstones among recent intake". When the
    shared query grew to SCORECARD_DAYS (28) for the Supply section, the
    unfiltered count silently widened to 3-4-week-old rows swept by the
    re-verify pass — live it jumped 204 → 326 without the filter changing.
    Filtering here keeps the card's meaning whatever the query spans.
    Unparseable timestamps drop out of every bucket, as before."""
    now = _aware(now)
    wk_ago = now - timedelta(days=SCORECARD_WEEK_DAYS)
    wk2_ago = now - timedelta(days=2 * SCORECARD_WEEK_DAYS)
    discovered_cut = now - timedelta(days=NOISE_CARD_DISCOVERY_DAYS)
    out = {'this_wk': [], 'last_wk': [], 'tomb_wk': []}
    for r in rows:
        disc = parse_ts(r.get('discovered_at'))
        if disc is None:
            continue
        if disc >= wk_ago:
            out['this_wk'].append(r)
        elif disc >= wk2_ago:
            out['last_wk'].append(r)
        tomb = parse_ts(r.get('blocked_at')) if r.get('blocked_at') else None
        if tomb is not None and tomb >= wk_ago and disc >= discovered_cut:
            out['tomb_wk'].append(r)
    return out


# ── "Why events were removed" (Phase 4 2026-09-08) ──────────────────────────
# Every tombstone writer stamps blocked_reason with a machine prefix before
# the first colon ('fit_gate: HQ out of territory', 'structured:sic_out: …',
# 'rep:Not a Fit (Acme)', 'trigger_expired: 70d old cfo_hire' …). The two
# rep dismissals this file writes ('dismissed by rep (NOT RELEVANT)' and the
# bulk form) carry no colon, so they are mapped by hand. Labels are the
# only table; the pivot and the "Noise removed" list both read it.
TOMBSTONE_REASON_LABELS = {
    'fit_gate': 'Failed fit gate (territory/revenue/vertical)',
    'industry': 'Blocked industry',
    'board_change_only': 'Board-of-directors change only',
    'rep': 'Dismissed by a rep (event, or account not a fit)',
    'entity_shape': 'Entity shape (fund / trust / SPAC / public body)',
    'structured': 'Structured data (SIC / Form D fields)',
    'no_workable_account': 'No workable account (advisor / investor only)',
    'bad_company_name': 'No real company name',
    'trigger_expired': 'Trigger expired (shelf life)',
    'oracle_too_small': 'Too small (registry revenue estimate)',
    'other': 'Other / unlabelled',
}
REMOVAL_TOP_SUBINDUSTRIES = 8
REMOVAL_OTHER_SUBINDUSTRY = 'other'      # known subindustries outside the top N
REMOVAL_UNKNOWN_SUBINDUSTRY = 'unknown'  # removed before classification (no zi_subindustry)


def tombstone_reason_prefix(reason) -> str:
    """blocked_reason → its machine prefix (lower-case, before the first
    colon); the colon-less rep dismissals → 'rep'; blank → 'other'."""
    s = _clean_str(reason)
    if not s:
        return 'other'
    if 'by rep' in s:
        return 'rep'
    return s.split(':', 1)[0].strip() or 'other'


def tombstone_reason_label(prefix: str) -> str:
    return TOMBSTONE_REASON_LABELS.get(prefix) or prefix


def removal_subindustry(zi) -> str:
    """Pivot bucket for a row's zi_subindustry: the name itself, or
    'unknown' for NULL / blank / the LLM's out-of-taxonomy 'OTHER' (which
    says nothing about the account and would masquerade as a subindustry)."""
    s = _clean_str(zi)
    if not s or s in ('other', 'none', 'nan', 'null'):
        return REMOVAL_UNKNOWN_SUBINDUSTRY
    return str(zi).strip()


def removal_pivot(rows, days=SCORECARD_WEEK_DAYS, now=None,
                  discovery_days=NOISE_CARD_DISCOVERY_DAYS,
                  top_subindustries=REMOVAL_TOP_SUBINDUSTRIES) -> dict:
    """Tombstones of the last `days` by reason prefix × source label ×
    ZoomInfo subindustry. The row set is the "Auto-removed as noise" card's
    (tombstoned within `days`, discovered within `discovery_days`) so the
    pivot's total is that card's number — one expander, one count (the
    2026-09-08 review's rule). discovery_days=None lifts the discovery
    clause (every removal in the window, re-verify sweeps included).
    → {'days', 'total',
       'subindustries': [top N by removals…, 'other'?, 'unknown'?],
       'rows': [{'reason', 'source', 'cells': {bucket: n}, 'total': n}…]
                (count desc, then reason, source),
       'by_reason' / 'by_source' / 'by_subindustry': {label: n} (count desc)}.
    Rows with no subindustry sit in their own 'unknown' column — most
    entity_shape / bad_company_name tombstones die before classification
    and would otherwise hide inside 'other'."""
    now = _aware(now)
    start = now - timedelta(days=days)
    disc_start = (now - timedelta(days=discovery_days)) if discovery_days is not None else None
    triples, reasons, sources, subs = Counter(), Counter(), Counter(), Counter()
    for r in rows:
        if not r.get('blocked_at'):
            continue
        tomb = parse_ts(r.get('blocked_at'))
        if tomb is None or tomb < start:
            continue
        if disc_start is not None:
            disc = parse_ts(r.get('discovered_at'))
            if disc is None or disc < disc_start:
                continue
        key = (tombstone_reason_prefix(r.get('blocked_reason')), source_label(r),
               removal_subindustry(r.get('zi_subindustry')))
        triples[key] += 1
        reasons[key[0]] += 1
        sources[key[1]] += 1
        subs[key[2]] += 1
    known = sorted((s for s in subs if s != REMOVAL_UNKNOWN_SUBINDUSTRY),
                   key=lambda s: (-subs[s], s))
    columns = known[:top_subindustries]
    if len(known) > top_subindustries:
        columns.append(REMOVAL_OTHER_SUBINDUSTRY)
    if subs[REMOVAL_UNKNOWN_SUBINDUSTRY]:
        columns.append(REMOVAL_UNKNOWN_SUBINDUSTRY)
    cells = {}
    for (reason, src, sub), n in triples.items():
        col = sub if sub in columns else REMOVAL_OTHER_SUBINDUSTRY
        row = cells.setdefault((reason, src), {c: 0 for c in columns})
        row[col] += n
    out_rows = [{'reason': k[0], 'source': k[1], 'cells': c, 'total': sum(c.values())}
                for k, c in cells.items()]
    out_rows.sort(key=lambda r: (-r['total'], r['reason'], r['source']))
    return {'days': days, 'total': sum(triples.values()), 'subindustries': columns,
            'rows': out_rows,
            'by_reason': dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            'by_source': dict(sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))),
            'by_subindustry': dict(sorted(subs.items(), key=lambda kv: (-kv[1], kv[0])))}


_REMOVAL_COLUMN_TITLES = {REMOVAL_OTHER_SUBINDUSTRY: 'Other subindustries',
                          REMOVAL_UNKNOWN_SUBINDUSTRY: 'Unknown / not classified'}


def removal_pivot_frame(pivot: dict) -> pd.DataFrame:
    """One row per (reason, source), one column per subindustry bucket, a
    Total column and an 'All reasons' total row."""
    subs = list(pivot['subindustries'])
    titles = [_REMOVAL_COLUMN_TITLES.get(s, s) for s in subs]
    cols = ['Reason', 'Source'] + titles + ['Total']
    data = []
    for row in pivot['rows']:
        rec = {'Reason': tombstone_reason_label(row['reason']), 'Source': row['source'],
               'Total': row['total']}
        for s, t in zip(subs, titles):
            rec[t] = row['cells'].get(s, 0)
        data.append(rec)
    if data:
        total = {'Reason': 'All reasons', 'Source': '', 'Total': pivot['total']}
        for s, t in zip(subs, titles):
            total[t] = sum(row['cells'].get(s, 0) for row in pivot['rows'])
        data.append(total)
    return pd.DataFrame(data, columns=cols)


def render_removal_section(tomb_rows, now=None):
    """The "Why events were removed" block of the Weekly Scorecard. `tomb_rows`
    is the card's bucket (scorecard_week_buckets()['tomb_wk']) — already the
    right rows, so the pivot is passed the discovery clause off."""
    piv = removal_pivot(tomb_rows, now=now, discovery_days=None)
    st.markdown("**Why events were removed (7d)**")
    if not piv['total']:
        st.caption("No events were removed in the last 7 days.")
        return
    st.caption(
        f"{piv['total']} removed — the same rows as 'Auto-removed as noise' — by reason × source, "
        f"split by ZoomInfo subindustry (top {REMOVAL_TOP_SUBINDUSTRIES}, the rest under "
        f"'Other subindustries'; 'Unknown' = removed before the account was classified). "
        f"A reason that dominates one source is a scraper to tune; one that dominates one "
        f"subindustry is a gate to check.")
    st.dataframe(removal_pivot_frame(piv), use_container_width=True, hide_index=True)


def render_weekly_scorecard(df, acct_dispos):
    """Trailing 7 days vs the 7 before: what came in, what got removed and
    why, what the team picked up. Turns rep behavior into tuning signal."""
    rows = load_scorecard_events()
    now = datetime.now(timezone.utc)
    wk_ago = now - timedelta(days=SCORECARD_WEEK_DAYS)
    buckets = scorecard_week_buckets(rows, now)
    this_wk, last_wk, tomb_wk = buckets['this_wk'], buckets['last_wk'], buckets['tomb_wk']

    def _recent(d):
        """Disposition touched in the last 7d (updated_at is a naive local
        timestamp — parse_ts reads it as UTC, close enough for a week)."""
        ts = parse_ts(d.get('updated_at'))
        return ts is not None and ts >= wk_ago

    with st.expander("📊 Weekly Scorecard — pipeline health & team activity",
                     expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("New events (7d)", len(this_wk),
                  delta=len(this_wk) - len(last_wk))
        c2.metric("Auto-removed as noise (7d)", len(tomb_wk),
                  help=f"Tombstoned in the last 7 days, among events discovered in the last "
                       f"{NOISE_CARD_DISCOVERY_DAYS} days (the card's meaning since it was "
                       f"built; the scorecard query itself spans {SCORECARD_DAYS} days).")
        picked = [d for d in (acct_dispos or {}).values()
                  if d.get('status') == 'Picked Up' and _recent(d)]
        c3.metric("Accounts picked up (7d)", len(picked))
        decided = [d for d in (acct_dispos or {}).values() if _recent(d)]
        c4.metric("Accounts dispositioned (7d)", len(decided))

        # The dashboard view itself — the SAME filtered frame the metric
        # cards, Work Queue and tabs render from (verification toggle incl.).
        if df is not None and not df.empty:
            _vd = df.apply(fit_verdict, axis=1)
            st.caption(
                f"Current view (after all filters): {len(df):,} event(s) — "
                f"{int(_vd.isin(VERIFIED_VERDICTS).sum()):,} verified, "
                f"{int(_vd.isin(UNVERIFIED_VERDICTS).sum()):,} unverified.")

        colA, colB = st.columns(2)
        with colA:
            st.caption("New events by source (7d)")
            src_c = {}
            for r in this_wk:
                s = _scorecard_src(r)
                src_c[s] = src_c.get(s, 0) + 1
            for s, n in sorted(src_c.items(), key=lambda kv: -kv[1])[:8]:
                st.markdown(f"- **{s}** — {n}")
            if not src_c:
                st.markdown("*none yet*")
        with colB:
            st.caption("Noise removed, by reason (7d)")
            # Same prefix rule + label table as the removal pivot below
            # (Phase 4 2026-09-08) — one vocabulary in this expander.
            reason_c = Counter(tombstone_reason_prefix(r.get('blocked_reason')) for r in tomb_wk)
            for k, n in sorted(reason_c.items(), key=lambda kv: (-kv[1], kv[0]))[:6]:
                st.markdown(f"- **{tombstone_reason_label(k)}** — {n}")
            if not reason_c:
                st.markdown("*none this week*")

        st.caption(
            "Reading this: 'New events' is raw intake; 'noise removed' is the "
            "filter doing its job (high is GOOD); pickups are the ground truth "
            "— if a source never produces a pickup, tell Claude to tune it.")

        # Phase 4 2026-09-08: why events were removed, by reason × source ×
        # subindustry. Its own try, like the supply block: a pivot bug must
        # never take the pickup numbers above down with it.
        try:
            render_removal_section(tomb_wk, now)
        except Exception as _rm_err:
            st.caption(f"(removal pivot unavailable: {_rm_err})")

        # Phase 3 supply visibility (2026-09-08) — its own try so a supply
        # bug can never take the pickup numbers above down with it.
        try:
            render_supply_section(rows, now)
        except Exception as _sup_err:
            st.caption(f"(supply section unavailable: {_sup_err})")


# The legacy account_dispositions table has no reason column: the code
# rides in `notes` as 'reason=<code> | <notes>'. The encoding is OWNED by
# accounts.encode_legacy_notes / decode_legacy_notes and this file uses
# those whenever the module is importable — review 2026-09-08 (Phase 4):
# this pair started here, used only on the module-absent path, while the
# module's own legacy write stored notes alone, so every reason a rep
# entered before migration 003 was lost. The copies below run ONLY while
# the module is absent (the same Phase 4 encoding, so the two paths read
# each other's rows).
_LEGACY_REASON_PREFIX = 'reason='


def _legacy_notes(reason, notes) -> Optional[str]:
    """notes column value for the legacy table: 'reason=<code> | <notes>'."""
    encode = getattr(_accounts, 'encode_legacy_notes', None)
    if encode is not None:
        return encode(reason, notes)
    parts = []
    if reason:
        parts.append(f'{_LEGACY_REASON_PREFIX}{reason}')
    if notes:
        parts.append(str(notes).strip())
    return ' | '.join(parts) or None


def _split_legacy_notes(notes):
    """→ (reason or None, notes or None) — the inverse of _legacy_notes.
    Notes written before Phase 4 carry no prefix and come back untouched."""
    decode = getattr(_accounts, 'decode_legacy_notes', None)
    if decode is not None:
        return decode(notes)
    s = str(notes or '').strip()
    if not s or (isinstance(notes, float) and notes != notes):
        return None, None
    if not s.startswith(_LEGACY_REASON_PREFIX):
        return None, s
    head, sep, rest = s[len(_LEGACY_REASON_PREFIX):].partition(' | ')
    return (head.strip() or None), (rest.strip() or None)


def _dispo_keys(name) -> list:
    """The keys a company may sit under in the dispositions map: this
    file's key first and — while the accounts module is absent — the
    pipeline's key (gates.account_key) as well. accounts.set_disposition
    writes the legacy row under THAT key, so a v1-keyed lookup alone would
    hide every verdict the module recorded the moment this file fell back
    (review 2026-09-08 (Phase 4)). With the module present the two keys are
    the same function, so there is exactly one."""
    keys = [_account_key(name)]
    if _accounts is None:
        try:
            alt = _gates_account_key(name)
        except Exception:  # noqa: BLE001 — a lookup helper never takes a card down
            alt = ''
        if alt and alt not in keys:
            keys.append(alt)
    return [k for k in keys if k]


def _dispo_for(acct_dispos, name) -> Optional[dict]:
    """The disposition record for a company name under any of its keys
    (_dispo_keys), or None."""
    if not acct_dispos or not name:
        return None
    for k in _dispo_keys(name):
        rec = acct_dispos.get(k)
        if rec:
            return rec
    return None


def _dispo_record(key: str, rec) -> dict:
    """accounts.load_dispositions value ({status, reason, notes, name, at})
    → the dict shape the rest of this file reads: the legacy table's
    company_key / company_name / status / notes / updated_at, plus 'reason'."""
    rec = rec if isinstance(rec, dict) else {}
    return {'company_key': key,
            'company_name': rec.get('name') or rec.get('company_name') or key,
            'status': rec.get('status'),
            'reason': rec.get('reason') or None,
            'notes': rec.get('notes') or None,
            'updated_at': rec.get('at') or rec.get('updated_at')}


def _load_legacy_dispositions(client) -> dict:
    """The legacy table as-is, with the reason code split back out of notes."""
    rows = client.table('account_dispositions').select('*').execute().data or []
    out = {}
    for r in rows:
        reason, notes = _split_legacy_notes(r.get('notes'))
        r = dict(r)
        r['reason'], r['notes'] = reason, notes
        out[r['company_key']] = r
    return out


def load_account_dispositions():
    """Return {account_key: {company_key, company_name, status, reason,
    notes, updated_at}} or None when the legacy table hasn't been migrated.

    Phase 4 2026-09-08: accounts.load_dispositions (the accounts table
    merged with the legacy account_dispositions) when that module is
    importable, the legacy table alone otherwise — the same shape either
    way, plus 'reason'. Keys are re-derived from the name with _account_key
    so a legacy row written by the v1 normalizer still matches this file's
    lookups (the module and this file normalize identically, so for rows
    the module keyed itself this is a no-op). A module failure falls back
    to the legacy read: the module may be ahead of the database."""
    client = get_supabase_client()
    if not client:
        return None
    if _accounts is not None:
        try:
            out = {}
            for key, rec in (_accounts.load_dispositions(client) or {}).items():
                name = rec.get('name') if isinstance(rec, dict) else None
                k = (_account_key(name) if name else '') or key
                out[k] = _dispo_record(k, rec)
            if out:
                return out
            # {} means "no verdicts yet" OR "neither table is readable" — the
            # module swallows both. Only the legacy read below can tell, and
            # the migration banner (None) depends on the difference (review
            # 2026-09-08 (Phase 4): it never showed on the module path).
        except Exception:
            pass
    try:
        return _load_legacy_dispositions(client)
    except Exception:
        return None  # table missing — feature dormant until migration


def _receipt_text(receipt, fallback: str) -> str:
    """The banner line. accounts.set_disposition returns 'Saved: …' /
    'Cleared …' on success and 'NOT saved — …' / 'Partly saved — …' (one
    table written, the other failed) otherwise — it never raises for a
    refused write — and main() colours the banner by the first character,
    so the mark is added here. A blank receipt reads as success (the call
    returned without raising)."""
    text = str(receipt or '').strip()
    if text[:1] in ('✅', '❌'):
        return text
    if not text:
        return f'✅ {fallback}'
    ok = text.lower().startswith(('saved', 'cleared'))
    return f"{'✅' if ok else '❌'} {text}"


def set_account_disposition(company_name: str, status, reason=None, notes=None):
    """Upsert (or clear, when status falsy/'—') a company's disposition.

    Runs inside widget on_change callbacks, where st.error output can be
    silently dropped — so the outcome (success OR failure) is stashed in
    session_state and rendered as a banner on the next rerun instead.

    Phase 4 2026-09-08: validates FIRST (disposition_error — a Not a Fit /
    Out of Alignment without a reason is refused and the banner says why;
    nothing is written), then writes through accounts.set_disposition,
    which keeps the accounts table AND the legacy table in step, or the
    legacy table alone while that module is absent (reason folded into
    notes, see _legacy_notes). Clearing goes through the module too —
    status None — so both tables forget the account together."""
    client = get_supabase_client()
    if not client or not company_name:
        st.session_state['_dispo_receipt'] = (
            "❌ Account status NOT saved — no database connection")
        return
    key = _account_key(company_name)
    if not key:
        st.session_state['_dispo_receipt'] = (
            f"❌ Account status NOT saved — couldn't derive a key from '{company_name}'")
        return
    clearing = not status or status == '—'
    err = disposition_error(status, reason)
    if err:
        st.session_state['_dispo_receipt'] = (
            f"❌ Account status NOT saved for {company_name} — {err}")
        return
    reason = (str(reason).strip() or None) if (reason and not clearing) else None
    notes = (str(notes).strip() or None) if (notes and not clearing) else None
    try:
        if _accounts is not None:
            receipt = _accounts.set_disposition(client, company_name, None if clearing else status,
                                                reason=reason, notes=notes)
            fallback = (f"Cleared account status for {company_name}" if clearing
                        else f"Saved: {company_name} → {status}")
            st.session_state['_dispo_receipt'] = _receipt_text(receipt, fallback)
            return
        if clearing:
            # every key this file may find the row under (_dispo_keys): a
            # row the accounts module wrote sits under the pipeline's key
            client.table('account_dispositions').delete().in_(
                'company_key', sorted(set(_dispo_keys(company_name)))).execute()
            st.session_state['_dispo_receipt'] = f"✅ Cleared account status for {company_name}"
        else:
            client.table('account_dispositions').upsert({
                'company_key': key,
                'company_name': str(company_name)[:200],
                'status': status,
                'notes': _legacy_notes(reason, notes),
                'updated_at': datetime.now().isoformat(),
            }, on_conflict='company_key').execute()
            why = f" ({reason_label(reason)})" if reason else ''
            st.session_state['_dispo_receipt'] = f"✅ Saved: {company_name} → {status}{why}"
    except Exception as e:
        st.session_state['_dispo_receipt'] = (
            f"❌ Account status NOT saved — {type(e).__name__}: {str(e)[:300]}")


def _on_account_dispo_change(widget_key: str, company_name: str):
    """on_change callback shared by the status, reason and notes widgets of
    one account control — writes immediately, no save button. `widget_key`
    is the status widget's key; the reason and notes widgets hang off it
    (see render_event_card), so whichever fired, all three are read. The
    reason picker only renders for REASON_REQUIRED_STATUSES, so a reason
    arriving with any other status is the previous status's — stale — and
    is dropped rather than stored against 'Picked Up'."""
    status = st.session_state.get(widget_key)
    reason = st.session_state.get(widget_key + '_reason')
    if status not in REASON_REQUIRED_STATUSES:
        reason = None
    set_account_disposition(company_name, status, reason=reason,
                            notes=st.session_state.get(widget_key + '_notes'))


# Event types that ARE a finance-leader trigger on their own.
FINANCE_LEADER_EVENT_TYPES = frozenset({'cfo_hire', 'finance_seat_open'})


def _finance_leader_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean mask: rows that represent a finance-leader trigger — a
    CFO-hire event, an OPEN finance seat (Adzuna: company hiring a
    CFO/Controller), OR any event tagged #NewController (Controller hires
    stay event_type=executive_hire by design). Shared by the "Finance
    Leader Triggers" metric card and its drill-down focus so the number
    and the list always match."""
    if df.empty:
        return pd.Series(dtype=bool)
    is_cfo = df['event_type'].isin(FINANCE_LEADER_EVENT_TYPES)

    def _has_controller_tag(h):
        if isinstance(h, str):
            try:
                h = json.loads(h)
            except Exception:
                return False
        return isinstance(h, list) and '#NewController' in h

    has_ctrl = df['hashtags'].apply(_has_controller_tag) \
        if 'hashtags' in df.columns else pd.Series(False, index=df.index)
    return is_cfo | has_ctrl


_GRADE_RANK = {'A': 0, 'B': 1, None: 2, '': 2, 'C': 3, 'D': 4}


def _grade_rank(g):
    """Sort key: A first, then B, then ungraded (fresh events awaiting
    enrichment shouldn't sink below C/D junk), then C, then D."""
    if g is None or (isinstance(g, float) and g != g):
        return 2
    return _GRADE_RANK.get(str(g).strip().upper(), 2)


def _score_of(r) -> int:
    """numeric_score as int; missing/NaN/garbage → -1 (ranks last)."""
    s = r.get('numeric_score')
    if s is None or (isinstance(s, float) and s != s):
        return -1
    try:
        return int(s)
    except Exception:
        return -1


def _epoch(v) -> float:
    """ISO date/datetime string or pandas Timestamp → epoch seconds.
    Missing / unparseable → 0.0, so an unknown date ranks OLDEST."""
    if v is None or (isinstance(v, float) and v != v):
        return 0.0
    try:
        s = v if not isinstance(v, str) else v.strip()
        if isinstance(s, str) and (not s or s.lower() in ('nan', 'nat', 'none')):
            return 0.0
        ts = pd.to_datetime(s, utc=True, errors='coerce')
        return 0.0 if pd.isna(ts) else float(ts.timestamp())
    except Exception:
        return 0.0


def work_queue_sort_key(r) -> tuple:
    """Work Queue ranking key: grade (A → B → ungraded → C → D), then
    numeric score DESC, then freshness DESC — the NEWEST published_date
    wins a tie (discovered_date when the article carries no date).
    v1 sorted the tie-break ascending, so the oldest story led."""
    ts = _epoch(r.get('published_date')) or _epoch(r.get('discovered_date'))
    return (_grade_rank(r.get('grade')), -_score_of(r), -ts)


# ── Account cards (Phase 4 2026-09-08) ──────────────────────────────────────
# The Work Queue row is the ACCOUNT: its best event is the card, and under
# it sits the account strip — grade / verification / best trigger / event
# count from the accounts table (migration 003) when it exists, from the
# event otherwise — and the trigger history: every live event for the same
# account_key in the loaded window. Pure helpers below; rendering is thin.
ACCOUNTS_SELECT = ('account_key,canonical_name,grade,numeric_score,verify_state,'
                   'best_trigger_type,best_trigger_at,best_trigger_event_id,event_count,'
                   'last_event_at,disposition,disposition_reason,hashtags,zi_subindustry,'
                   'revenue_segment,hq_state,active')
ACCOUNTS_KEY_CHUNK = 100     # keys per in.(…) filter — keeps the request URL short
HISTORY_MAX_ROWS = 6         # lines shown before "+k more"


def _probe_accounts_table(client) -> bool:
    """Does the accounts table exist? accounts.probe_accounts when the
    module is importable, else one cheap select; any failure → False (the
    event-derived summary, i.e. today's behaviour)."""
    if client is None:
        return False
    if _accounts is not None:
        try:
            return bool(_accounts.probe_accounts(client))
        except Exception:
            return False
    try:
        client.table('accounts').select('account_key').limit(1).execute()
        return True
    except Exception:
        return False


# One probe an hour, like typed_columns_present: the table lands when A.J.
# runs migration 003, and a stale False only costs the event-derived
# summary until the TTL expires.
@st.cache_data(ttl=3600)
def accounts_table_present() -> bool:
    try:
        return _probe_accounts_table(get_supabase_client())
    except Exception:
        return False


def _load_accounts_by_key(client, keys, chunk=ACCOUNTS_KEY_CHUNK) -> dict:
    """{account_key: accounts row} for `keys` (deduped, blanks dropped),
    `chunk` keys per request. Any failure → {} — the cards then read from
    their events, never half from each."""
    keys = list(dict.fromkeys(k for k in keys if k))
    out = {}
    for i in range(0, len(keys), chunk):
        try:
            resp = (client.table('accounts').select(ACCOUNTS_SELECT)
                    .in_('account_key', keys[i:i + chunk]).execute())
        except Exception:
            return {}
        for r in resp.data or []:
            if isinstance(r, dict) and r.get('account_key'):
                out[r['account_key']] = r
    return out


def load_accounts_for(keys) -> dict:
    """Accounts rows for the keys on screen; {} until the table exists."""
    if not keys or not accounts_table_present():
        return {}
    client = get_supabase_client()
    if not client:
        return {}
    return _load_accounts_by_key(client, keys)


def event_account_key(row) -> str:
    """The account an event belongs to. The typed `account_key` column when
    enrichment filled it AND the accounts module is present (both are
    gates.account_key, so they agree); otherwise _account_key of the
    display company — one normalizer per run, never two. '' for an unknown
    company, so unknowns never merge or share a history."""
    k = _clean_str(row.get('account_key'))
    if k and _accounts is not None:
        return k
    name = _resolve_display_company(row)
    if not name or name.strip().lower() in ('', 'unknown company'):
        return ''
    return _account_key(name)


def annotate_account_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Add an `_account_key` column (event_account_key per row), computed
    ONCE per frame: trigger_history is called per Work Queue row, and with
    the category focus on that is up to 500 rows × a 10k-row window."""
    df = df.copy()
    if df.empty:
        df['_account_key'] = pd.Series(dtype=object)
    else:
        df['_account_key'] = df.apply(event_account_key, axis=1)
    return df


def _is_tombstoned(row) -> bool:
    return _clean_str(row.get('blocked_at')) not in ('', 'none', 'nat', 'null')


def _event_when(row):
    """(epoch, 'YYYY-MM-DD') of the event: published_date, else the
    discovery timestamp — the Work Queue's freshness rule."""
    for field in ('published_date', 'discovered_date', 'discovered_at'):
        ts = _epoch(row.get(field))
        if ts:
            return ts, datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
    return 0.0, ''


def trigger_history(df, account_key: str) -> list:
    """Every live (not tombstoned) event in `df` for `account_key`, newest
    first → [{'id', 'date', 'ts', 'event_type', 'label', 'icon', 'grade',
    'lead_status', 'title'}]. Uses the `_account_key` column when the frame
    was annotated (annotate_account_keys), deriving it otherwise. Empty for
    a blank key: unknown companies have no shared history."""
    if not account_key or df is None or len(df) == 0:
        return []
    if isinstance(df, pd.DataFrame):
        keys = (df['_account_key'] if '_account_key' in df.columns
                else df.apply(event_account_key, axis=1))
        rows = df[keys == account_key].to_dict('records')
    else:
        rows = [r for r in df if event_account_key(r) == account_key]
    out = []
    for r in rows:
        if _is_tombstoned(r):
            continue
        ts, date = _event_when(r)
        cfg = event_config_for(r.get('event_type'))
        g = r.get('grade')
        out.append({'id': r.get('id'), 'date': date, 'ts': ts,
                    'event_type': _clean_str(r.get('event_type')) or 'other',
                    'label': cfg['label'], 'icon': cfg['icon'],
                    'grade': str(g).strip().upper() if _clean_str(g) else '',
                    'lead_status': str(r.get('lead_status') or 'NEW'),
                    'title': str(r.get('title') or '')[:90]})
    out.sort(key=lambda h: -h['ts'])
    return out


def _present(v) -> bool:
    return v is not None and not (isinstance(v, float) and v != v) and str(v).strip() != ''


def account_summary(row, accounts_row=None, history=None) -> dict:
    """What the account strip shows. The accounts row (migration 003) wins
    for grade / numeric_score / verify_state / best trigger / event_count
    wherever it carries a value; the event — the Work Queue's top row for
    the account — fills the rest, so the strip reads the same before the
    table exists, from that one event's point of view. event_count falls
    back to the history length (the live events in the window)."""
    acc = accounts_row if isinstance(accounts_row, dict) else {}

    def pick(field, event_value):
        v = acc.get(field)
        return v if _present(v) else event_value

    name = acc.get('canonical_name') if _present(acc.get('canonical_name')) else _resolve_display_company(row)
    ts, date = _event_when(row)
    grade = pick('grade', row.get('grade'))
    state = pick('verify_state',
                 _clean_str(row.get('verify_state')) or verify_state_for(fit_verdict(row)) or 'staged')
    best_type = pick('best_trigger_type', row.get('event_type'))
    best_at = pick('best_trigger_at', date)
    count = acc.get('event_count')
    if not _present(count):
        count = len(history) if history is not None else 1
    try:
        count = int(float(count))
    except (TypeError, ValueError):
        count = 0
    hashtags = _parse_json_field(pick('hashtags', row.get('hashtags')), [])
    return {'name': name,
            'account_key': (_clean_str(acc.get('account_key')) or event_account_key(row)),
            'grade': str(grade).strip().upper() if _present(grade) else '',
            'numeric_score': _score_of({'numeric_score': pick('numeric_score', row.get('numeric_score'))}),
            'verify_state': _clean_str(state) or 'staged',
            'best_trigger_type': _clean_str(best_type) or 'other',
            'best_trigger_label': _trigger_label(_clean_str(best_type) or 'other'),
            'best_trigger_at': str(best_at)[:10] if _present(best_at) else '',
            'event_count': count,
            'hashtags': hashtags if isinstance(hashtags, list) else [],
            'from_accounts_table': bool(acc)}


_VERIFY_STATE_TEXT = {'verified': ('✓ verified', '#10b981'),
                      'researched_ambiguous': ('⚠ verify fit', '#fbbf24'),
                      'staged': ('🕒 not researched', '#d1d5db'),
                      'decided': ('decided', '#9ca3af'),
                      'not_fit': ('✗ not a fit', '#f87171')}


def _grade_pill(grade: str, small: bool = True) -> str:
    if grade not in GRADE_COLORS:
        return ('<span style="font-size:0.66rem;color:rgba(255,255,255,0.45);">ungraded</span>'
                if small else '')
    return (f'<span style="background:{GRADE_COLORS[grade]};color:#fff;border-radius:4px;'
            f'padding:0.05rem 0.4rem;font-size:0.66rem;font-weight:800;">{grade}</span>')


def account_history_html(summary: dict, history: list, others_new: int = 0,
                         max_rows: int = HISTORY_MAX_ROWS) -> str:
    """The account strip as ONE continuous HTML string (an indented line
    after a blank one renders as a code block — see render_event_card)."""
    import html as _h
    txt, clr = _VERIFY_STATE_TEXT.get(summary.get('verify_state'), (summary.get('verify_state') or '', '#9ca3af'))
    facts = [f'<span style="font-weight:600;color:rgba(255,255,255,0.8);">🗂 {_h.escape(str(summary.get("name") or ""))}</span>',
             _grade_pill(summary.get('grade') or ''),
             f'<span style="color:{clr};">{_h.escape(txt)}</span>']
    if summary.get('best_trigger_label'):
        when = f' ({summary["best_trigger_at"]})' if summary.get('best_trigger_at') else ''
        facts.append(f'best trigger: {_h.escape(summary["best_trigger_label"])}{when}')
    n = summary.get('event_count') or 0
    facts.append(f'{n} event{"s" if n != 1 else ""}' +
                 (' (accounts table)' if summary.get('from_accounts_table') else ' in window'))
    head = ' <span style="color:rgba(255,255,255,0.3);">·</span> '.join(facts)
    lines = []
    for h in history[:max_rows]:
        lines.append(
            f'<div style="margin:2px 0 0 1.2rem;">'
            f'<span style="color:rgba(255,255,255,0.5);">{h["date"] or "no date"}</span> · '
            f'{h["icon"]} {_h.escape(h["label"])} · {_grade_pill(h["grade"])} · '
            f'<span style="color:rgba(255,255,255,0.65);">{_h.escape(h["title"])}</span>'
            f'</div>')
    extra = len(history) - max_rows
    if extra > 0:
        lines.append(f'<div style="margin:2px 0 0 1.2rem;color:rgba(255,255,255,0.45);">… +{extra} more</div>')
    if others_new:
        lines.append(f'<div style="margin:2px 0 0 1.2rem;color:rgba(255,255,255,0.45);">↳ +{others_new} more NEW '
                     f'event{"s" if others_new != 1 else ""} for this account in the New Leads tabs below</div>')
    return (f'<div style="font-size:0.74rem;color:rgba(255,255,255,0.7);margin:-0.3rem 0 0.9rem 0.4rem;">'
            f'<div>{head}</div>{"".join(lines)}</div>')


def render_account_history(summary: dict, history: list, others_new: int = 0):
    st.markdown(account_history_html(summary, history, others_new), unsafe_allow_html=True)


def render_work_queue(new_df: pd.DataFrame, top_n: int = 10, history_df=None):
    """The Monday-morning view: ONE ranked list across all event types,
    rolled up per ACCOUNT. Ranking: grade → numeric score → freshness.
    `history_df` (Phase 4 2026-09-08) is the whole loaded window — every
    lead status, verification toggle applied — so an account's trigger
    history shows the events a rep already classified too; it defaults to
    `new_df`."""
    st.markdown("""
        <div class="section-header">
            <span style="font-size: 1.5rem;">🔥</span>
            <h2>Work Queue</h2>
            <span class="section-count" title="Top accounts across all event types, ranked by grade → score → freshness">ranked</span>
        </div>
    """, unsafe_allow_html=True)

    if new_df.empty:
        st.info("Queue clear — no new leads awaiting review.")
        return

    rows = new_df.to_dict('records')

    # Rank events: grade, then score DESC, then published date DESC
    # (newest first). One sort, one key — see work_queue_sort_key.
    rows.sort(key=work_queue_sort_key)

    # Roll up per account — the best-ranked event represents it. Keyed by
    # event_account_key (Phase 4): the typed account_key / the ONE
    # normalizer, so "Acme Inc." and "Acme" are one row, and the same key
    # the trigger history and the accounts table use.
    by_company = {}
    order = []
    for r in rows:
        key = event_account_key(r)
        if not key:
            key = f"__solo_{r.get('id')}"  # don't merge unknowns together
        if key not in by_company:
            by_company[key] = {'top': r, 'others': 0}
            order.append(key)
        else:
            by_company[key]['others'] += 1

    shown_keys = [k for k in order[:top_n] if not k.startswith('__solo_')]
    accounts_rows = load_accounts_for(shown_keys)
    hist = annotate_account_keys(history_df if history_df is not None else new_df)

    shown = 0
    for key in order:
        if shown >= top_n:
            break
        entry = by_company[key]
        r = entry['top']
        event_config = event_config_for(r.get('event_type'))
        render_event_card(r, event_config, key_prefix='wq_')
        acct_key = '' if key.startswith('__solo_') else key
        history = trigger_history(hist, acct_key)
        render_account_history(account_summary(r, accounts_rows.get(acct_key), history),
                               history, others_new=entry['others'])
        shown += 1

    remaining = len(order) - shown
    if remaining > 0:
        st.caption(f"…{remaining} more account(s) in the New Leads tabs below "
                   f"(this queue shows the top {top_n}).")


def render_metric_card(icon: str, value: int, label: str, color: str, gradient: str = None):
    """Render a modern metric card."""
    bg_gradient = gradient or f"linear-gradient(135deg, {color}20 0%, {color}10 100%)"
    st.markdown(f"""
        <div class="metric-card">
            <div class="metric-icon" style="background: {bg_gradient};">
                {icon}
            </div>
            <div class="metric-value">{value:,}</div>
            <div class="metric-label">{label}</div>
        </div>
    """, unsafe_allow_html=True)


def check_password() -> bool:
    """Returns True if the user entered the correct password."""
    if st.session_state.get("authenticated"):
        return True

    logo_b64 = get_logo_base64()
    logo_html = f'<img src="data:image/png;base64,{logo_b64}" class="header-logo">' if logo_b64 else ""
    st.markdown(f"""
        <div class="main-header">
            <div class="header-inner">
                <div class="header-text">
                    <p class="header-subtitle">NetSuite Up-Market Sales</p>
                    <h1 class="header-title">Team Albert</h1>
                    <p class="header-tagline">Sales trigger events — East Coast &amp; Eastern Canada</p>
                </div>
                {logo_html}
            </div>
        </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        password = st.text_input("Team Password", type="password", placeholder="Enter password...")
        if st.button("Sign In", use_container_width=True):
            expected = st.secrets.get("DASHBOARD_PASSWORD", "")
            if password == expected and expected:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")

    return False


def main():
    if not check_password():
        return

    # Header
    logo_b64 = get_logo_base64()
    logo_html = f'<img src="data:image/png;base64,{logo_b64}" class="header-logo">' if logo_b64 else ""
    st.markdown(f"""
        <div class="main-header">
            <div class="header-inner">
                <div class="header-text">
                    <p class="header-subtitle">NetSuite Up-Market Sales</p>
                    <h1 class="header-title">Team Albert</h1>
                    <p class="header-tagline">Sales trigger events — East Coast &amp; Eastern Canada</p>
                </div>
                {logo_html}
            </div>
        </div>
    """, unsafe_allow_html=True)

    # Check Supabase connection
    client = get_supabase_client()
    if not client:
        st.warning("⚠️ Supabase not configured")
        st.info("""
        **To connect to Supabase:**

        Add to Streamlit secrets:
        ```
        SUPABASE_URL = "https://your-project.supabase.co"
        SUPABASE_KEY = "your-anon-key"  # Use the anon key (not service_role)
        ```
        """)
        return

    # ── Filters + Search row ────────────────────────────────────────────
    # Filters live in a popover instead of the sidebar, so they're always
    # accessible regardless of sidebar collapse state. Search stays inline
    # next to the filter button.
    filter_col, search_col = st.columns([1, 5])

    with filter_col:
        # The popover is a container we re-enter after the load: the time
        # range must render BEFORE load_events (it drives the query), while
        # the territory options and the "N unverified hidden" caption need
        # the loaded frame. Streamlit allows writing into a container later
        # in the script; the widgets still appear in call order.
        filters_pop = st.popover("🎛️  Filters", use_container_width=True)
        with filters_pop:
            days = st.slider("Time Range (days)", 1, 90, 30, key="flt_days")

    with search_col:
        st.markdown('<div class="search-container">', unsafe_allow_html=True)
        search = st.text_input(
            "Search",
            placeholder="🔍 Search by company, title, or keyword...",
            label_visibility="collapsed",
            key="flt_search",
        )
        st.markdown('</div>', unsafe_allow_html=True)

    # Receipt from the last account-status change (set in the on_change
    # callback, where direct st.error/success output can be swallowed)
    _receipt = st.session_state.pop('_dispo_receipt', None)
    if _receipt:
        (st.success if _receipt.startswith('✅') else st.error)(_receipt)

    # Load all events. The verification toggle widget renders later in the
    # sidebar, but its state is already in session_state on every rerun
    # (False on the very first run = the verified-only default), so the
    # server-side filter can use it now. Phase 2, 2026-09-07.
    show_unverified = bool(st.session_state.get('flt_show_unverified', False))
    typed_present = typed_columns_present()
    df = load_events(days=days, search=search if search else None,
                     verified_only=not show_unverified)

    if df.empty:
        # Distinguish "your search matched nothing" from "the database read
        # itself returned nothing" (usually an RLS/key problem, not real data).
        try:
            client = get_supabase_client()
            total = client.table('events').select('id', count='exact').limit(1).execute().count if client else 0
        except Exception:
            total = 0
        if total:
            st.info("📭 No events match the current search/time window. Try clearing filters.")
            return
        def _secret_val(name):
            if hasattr(st, 'secrets') and name in st.secrets:
                return st.secrets.get(name)
            return os.environ.get(name)
        if not _secret_val("SUPABASE_SERVICE_ROLE_KEY"):
            st.error(
                "🔑 No events loaded — the SUPABASE_SERVICE_ROLE_KEY secret is missing. "
                "The database now requires the admin key for all access. "
                "Add it in Streamlit Cloud → Settings → Secrets, then reboot the app."
            )
        else:
            st.error(
                "🔑 No events loaded, but the admin key IS configured — the app is "
                "likely still holding an old database connection from before the key "
                "was added. Reboot the app: share.streamlit.io → ⋮ → Reboot. "
                "If it persists after reboot, re-copy the secret key from Supabase "
                "(Project Settings → API Keys → Secret keys)."
            )
        return

    # One HQ state/province code per event (account hq → matched_regions)
    df = annotate_hq_state(df)

    with filters_pop:
        # ── Territory (structured: HQ state/province code) ────────────
        st.markdown("**📍 Territory (HQ state / province)**")
        _terr_codes = territory_options(df)
        # Prune a stale selection (a code that left the time window)
        # before the widget renders, so Streamlit never sees a value
        # outside its options.
        _prev_terr = st.session_state.get('flt_territory')
        if isinstance(_prev_terr, list):
            st.session_state['flt_territory'] = [
                c for c in _prev_terr if c in _terr_codes]
        selected_states = st.multiselect(
            "Territory",
            options=_terr_codes,
            format_func=territory_label,
            placeholder="All states / provinces",
            help=(
                "HQ state/province of the account (from enrichment; the "
                "scraper's dateline as fallback). Lists only codes present "
                "in the current time window."
            ),
            label_visibility="collapsed",
            key="flt_territory",
        )

        # Revenue segment filter — 4 NetSuite sales tiers:
        #   LMM  (<$10M)   ·  MM   ($10-$20M)
        #   Corp ($20-100M)  ·  Enterprise (>$100M)
        st.markdown("**💵 Revenue Segment**")
        preset = st.selectbox(
            "Preset",
            options=list(REVENUE_PRESETS.keys()),
            index=0,  # NetSuite Up-Market ($0-$100M)
            help="Quick presets. Use the multiselect below to fine-tune.",
            label_visibility="collapsed",
            key="flt_preset",
        )
        default_bands = REVENUE_PRESETS[preset]

        selected_bands = st.multiselect(
            "Segments to include",
            options=REVENUE_BANDS,
            default=default_bands,
            placeholder="Select segments…",
            help=(
                "LMM = Lower Mid-Market (<$10M)  ·  "
                "MM = Mid-Market ($10M-$20M)  ·  "
                "Corp = Corporate ($20M-$100M)  ·  "
                "Enterprise (>$100M)"
            ),
            label_visibility="collapsed",
            key=f"flt_bands_{preset}",  # Reset multiselect when preset changes
        )

        include_unknown = st.checkbox(
            "Also include companies with unknown revenue",
            value=True,
            help="Most newly-discovered leads don't have revenue data yet. Keep this ON to surface them; turn OFF to see only confirmed sized companies.",
            key="flt_include_unknown",
        )

        # ── TAL Grade filter ──────────────────────────────────────────
        st.markdown("**🎯 TAL Grade**")
        selected_grades = st.multiselect(
            "Grades to include",
            options=['A', 'B', 'C', 'D'],
            default=['A', 'B'],
            placeholder="Select grades…",
            help=(
                "Point-based TAL rubric: A = score 8+ with a high-intent "
                "trigger (hottest)  ·  B = 5-7  ·  C = 2-4  ·  D = 0-1. "
                "High-intent: NewCFO +5, NewController/Funding/PEBacked/"
                "Acquisitions +3. Complexity signals +2 each."
            ),
            label_visibility="collapsed",
            key="flt_grades",
        )
        include_ungraded = st.checkbox(
            "Also show ungraded events",
            value=True,
            help="Events scraped before grading was enabled, or where grading is still pending. Keep ON to avoid hiding fresh events.",
            key="flt_include_ungraded",
        )

        # ── Verification (A.J. 2026-09-04: verified-only by default) ──
        st.markdown("**✅ Verification**")
        show_unverified = st.toggle(
            "Show unverified accounts",
            value=False,
            help=(
                "OFF (default): only accounts whose vertical AND territory "
                "are confirmed. ON: also show accounts still awaiting "
                "verification or not yet researched. Rejected and "
                "already-dispositioned accounts never show."
            ),
            key="flt_show_unverified",
        )
        # Filled in below once the frame is filtered (count is view-relative)
        unverified_slot = st.empty()

    df = filter_by_territory(df, selected_states)

    df = filter_by_revenue_bands(
        df,
        allowed_bands=selected_bands,
        include_unknown=include_unknown,
    )

    df = filter_by_grades(
        df,
        allowed_grades=selected_grades,
        include_ungraded=include_ungraded,
    )

    # Verification LAST so the caption states exactly what the toggle hides
    # or reveals under the other filters. Everything below — metric cards,
    # Work Queue, category tabs, Classified Leads, Scorecard's view line,
    # the export — renders from this one frame.
    # Second, idempotent pass: with the typed column present the query
    # already applied this filter, so it changes nothing — but it keeps
    # the page correct when the probe is stale or a row's fit and
    # verify_state disagree.
    df, n_unverified = split_by_verdict(df, show_unverified)
    window_wide = False
    if not show_unverified and 'verify_state' in typed_present:
        # The hidden rows were never downloaded — count them server-side.
        # This count is window-wide (not narrowed by the territory /
        # revenue / grade filters above), unlike the legacy frame count,
        # and the caption says so (review 2026-09-07).
        n_unverified = count_hidden_unverified(days, typed_present)
        window_wide = True
    unverified_slot.caption(unverified_caption(n_unverified, show_unverified, days, window_wide))

    # Stats
    stats = get_stats(df)
    ma_count = stats["by_type"].get("merger_acquisition", 0)
    cfo_count = stats["by_type"].get("cfo_hire", 0)
    funding_count = stats["by_type"].get("funding", 0)

    # Finance Leader Triggers = CFO-hire events + open finance seats
    # (Adzuna postings, event_type=finance_seat_open) + any event carrying
    # the #NewController hashtag (Controller hires stay event_type=
    # executive_hire by design — see enrichment_scout._finance_role).
    # This is THE highest-value trigger family, so it gets its own card.
    fl_mask = _finance_leader_mask(df)
    finance_leader_count = int(fl_mask.sum())

    # Modern metric cards — the three CATEGORY cards are drillable: the
    # "Work these →" button under each focuses the entire page (Work Queue
    # + tabs) on that category so a rep can grind through the whole list.
    focus = st.session_state.get('category_focus')

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        render_metric_card("📊", stats["total"], "Total Events", "#667eea")
    with col2:
        render_metric_card("🆕", stats["new"], "New Leads", "#10b981")
    with col3:
        render_metric_card("💼", finance_leader_count, "Finance Leader Triggers", "#8b5cf6")
        if st.button("Work these →", key="focus_fl", use_container_width=True,
                     type="primary" if focus == 'finance' else "secondary"):
            st.session_state['category_focus'] = None if focus == 'finance' else 'finance'
            st.rerun()
    with col4:
        render_metric_card("🔵", ma_count, "M&A Events", "#3b82f6")
        if st.button("Work these →", key="focus_ma", use_container_width=True,
                     type="primary" if focus == 'ma' else "secondary"):
            st.session_state['category_focus'] = None if focus == 'ma' else 'ma'
            st.rerun()
    with col5:
        render_metric_card("💰", funding_count, "Funding", "#f59e0b")
        if st.button("Work these →", key="focus_funding", use_container_width=True,
                     type="primary" if focus == 'funding' else "secondary"):
            st.session_state['category_focus'] = None if focus == 'funding' else 'funding'
            st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    # Source Status Section
    with st.expander("📡 Source Health Status", expanded=False):
        source_status_df = load_source_statuses()
        render_source_status_table(source_status_df)

    st.markdown("<br>", unsafe_allow_html=True)

    # Split data: new (unclassified) vs classified
    new_df = df[df['lead_status'] == 'NEW']
    classified_df = df[df['lead_status'] != 'NEW']

    # ── Account dispositions: any dispositioned company = decided → its
    # events leave the active queue (per A.J.: "once a company is marked
    # Picked Up it should disappear from view").
    acct_dispos = load_account_dispositions()
    st.session_state['acct_dispos'] = acct_dispos
    if acct_dispos is None:
        st.warning(
            "**Account dispositions need a one-time migration** — run this in "
            "Supabase SQL Editor to enable per-company statuses:\n"
            f"```sql\n{ACCOUNT_DISPO_MIGRATION_SQL}\n```"
        )
    elif acct_dispos and not new_df.empty:

        def _event_decided(r):
            # Hidden when the company the event is ABOUT is dispositioned —
            # check both the display company and the enriched primary.
            names = {_resolve_display_company(r)}
            cd = r.get('companies_data')
            if isinstance(cd, str):
                try:
                    cd = json.loads(cd)
                except Exception:
                    cd = []
            if isinstance(cd, list) and cd:
                for role in ('acquirer', 'portfolio company', 'hiring company',
                             'primary', 'target'):
                    m = next((c for c in cd
                              if str(c.get('role', '')).lower() == role), None)
                    if m:
                        names.add(m.get('name') or '')
                        break
            return any(_dispo_for(acct_dispos, n) for n in names if n)

        decided_mask = new_df.apply(_event_decided, axis=1)
        hidden_n = int(decided_mask.sum())
        new_df = new_df[~decided_mask]
        if hidden_n:
            st.caption(f"♻️ {hidden_n} event(s) hidden — their accounts are "
                       f"already dispositioned (see the Dispositioned "
                       f"Accounts panel at the bottom).")

    # ── Category focus (from the metric-card buttons) ──────────────────────
    if focus:
        labels = {'finance': '💼 Finance Leader Triggers',
                  'ma': '🔵 M&A Events', 'funding': '💰 Funding'}
        if focus == 'finance':
            new_df = new_df[_finance_leader_mask(new_df)]
        elif focus == 'ma':
            new_df = new_df[new_df['event_type'] == 'merger_acquisition']
        elif focus == 'funding':
            new_df = new_df[new_df['event_type'] == 'funding']
        fc1, fc2 = st.columns([5, 1])
        with fc1:
            st.info(f"Focused on **{labels[focus]}** — {len(new_df)} new "
                    f"lead(s), ranked below. Click ✕ to see everything again.")
        with fc2:
            if st.button("✕ Clear", key="clear_focus", use_container_width=True):
                st.session_state['category_focus'] = None
                st.rerun()

    # ── 🔥 WORK QUEUE — the answer to "which accounts do I work?" ──────────
    # One ranked list across ALL event types: grade first (A→B→ungraded),
    # then numeric score, then freshness. Rolled up per company so one
    # account with 4 events is one row, not four cards.
    # When a category focus is active, show the WHOLE ranked list — the rep
    # is working through it, not previewing it.
    render_work_queue(new_df, top_n=(500 if focus else 10), history_df=df)

    # ── New Leads Section ──
    new_count = len(new_df)
    st.markdown(f"""
        <div class="section-header">
            <span style="font-size: 1.5rem;">🆕</span>
            <h2>New Leads</h2>
            <span class="section-count">{new_count}</span>
        </div>
    """, unsafe_allow_html=True)

    if new_df.empty:
        st.info("No new leads to review. Nice work!")
    else:
        new_ma = len(new_df[new_df['event_type'] == 'merger_acquisition'])
        new_cfo = len(new_df[new_df['event_type'] == 'cfo_hire'])
        new_seat = len(new_df[new_df['event_type'] == 'finance_seat_open'])
        new_funding = len(new_df[new_df['event_type'] == 'funding'])
        new_stable = len(new_df[new_df['event_type'] == 'stable_target'])
        new_exec = len(new_df[new_df['event_type'] == 'executive_hire'])
        new_expansion = len(new_df[new_df['event_type'] == 'expansion'])
        # "Other" absorbs every type without a tab of its own (incl. unknown)
        _tabbed = [t for t in EVENT_TYPES if t != 'other']
        new_other = int((~new_df['event_type'].isin(_tabbed)).sum())

        # Phase 4 2026-09-08: an Expansion tab — 'expansion' joined
        # EVENT_TYPES (so `_tabbed` counts it out of "Other"), and a type in
        # that map without a tab renders nowhere.
        (tab_ma, tab_cfo, tab_seat, tab_funding, tab_stable, tab_exec,
         tab_expansion, tab_other) = st.tabs([
            f"🔵 M&A ({new_ma})",
            f"💼 CFO ({new_cfo})",
            f"🪑 Open Seat ({new_seat})",
            f"💰 Funding ({new_funding})",
            f"🎯 Stable ({new_stable})",
            f"👔 Exec ({new_exec})",
            f"🌱 Expansion ({new_expansion})",
            f"📋 Other ({new_other})"
        ])
        with tab_ma:
            render_event_section(new_df, "merger_acquisition", EVENT_TYPES["merger_acquisition"], None)
        with tab_cfo:
            render_event_section(new_df, "cfo_hire", EVENT_TYPES["cfo_hire"], None)
        with tab_seat:
            render_event_section(new_df, "finance_seat_open", EVENT_TYPES["finance_seat_open"], None)
        with tab_funding:
            render_event_section(new_df, "funding", EVENT_TYPES["funding"], None)
        with tab_stable:
            render_event_section(new_df, "stable_target", EVENT_TYPES["stable_target"], None)
        with tab_exec:
            render_event_section(new_df, "executive_hire", EVENT_TYPES["executive_hire"], None)
        with tab_expansion:
            render_event_section(new_df, "expansion", EVENT_TYPES["expansion"], None)
        with tab_other:
            render_event_section(new_df, "other", EVENT_TYPES["other"], None,
                                 include_unknown_types=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Classified Leads Section ──
    classified_count = len(classified_df)
    st.markdown(f"""
        <div class="section-header">
            <span style="font-size: 1.5rem;">📋</span>
            <h2>Classified Leads</h2>
            <span class="section-count">{classified_count}</span>
        </div>
    """, unsafe_allow_html=True)

    if classified_df.empty:
        st.info("No classified leads yet. Review new leads above to classify them.")
    else:
        # Build tabs for each classification that has events
        classified_statuses = [s for s in LEAD_STATUSES if s != "NEW" and s != "NOT RELEVANT"]
        status_tabs = []
        status_keys = []
        for s in classified_statuses:
            count = len(classified_df[classified_df['lead_status'] == s])
            if count > 0:
                cfg = STATUS_CONFIG.get(s, {"icon": "📋", "label": s})
                status_tabs.append(f"{cfg['icon']} {cfg['label']} ({count})")
                status_keys.append(s)

        if not status_tabs:
            st.info("No classified leads yet.")
        else:
            tabs = st.tabs(status_tabs)
            for tab, status_key in zip(tabs, status_keys):
                with tab:
                    status_df = classified_df[classified_df['lead_status'] == status_key]
                    for idx, row in status_df.iterrows():
                        event_config = event_config_for(row.get('event_type'))
                        render_event_card(row, event_config)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Weekly Scorecard ───────────────────────────────────────────────────
    try:
        render_weekly_scorecard(df, acct_dispos)
    except Exception as _sc_err:
        st.caption(f"(scorecard unavailable: {_sc_err})")

    # ── Dispositioned Accounts (decided — hidden from active views) ────────
    if acct_dispos:
        with st.expander(f"♻️ Dispositioned Accounts ({len(acct_dispos)})",
                         expanded=False):
            st.caption("Every event for these companies is hidden from the "
                       "queue. Clear a row to bring the account back.")
            rows = sorted(acct_dispos.values(),
                          key=lambda r: r.get('updated_at') or '', reverse=True)
            for r in rows[:200]:
                c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
                with c1:
                    st.markdown(f"**{r.get('company_name') or r['company_key']}**")
                    if r.get('notes'):
                        st.caption(str(r['notes'])[:120])
                with c2:
                    # Phase 4 2026-09-08: the reason code next to the status
                    why = f" · *{reason_label(r['reason'])}*" if r.get('reason') else ''
                    st.markdown((r.get('status') or '') + why)
                with c3:
                    st.caption((r.get('updated_at') or '')[:10])
                with c4:
                    if st.button("✕ Clear", key=f"clr_acct_{r['company_key']}",
                                 use_container_width=True):
                        set_account_disposition(r.get('company_name')
                                                or r['company_key'], None)
                        st.rerun()

    # ── All Events Table ──
    with st.expander("📊 All Events Table", expanded=False):
        display_cols = ['event_type', 'company_name', 'title', 'published_date', 'lead_status']
        available_cols = [c for c in display_cols if c in df.columns]

        display_df = df[available_cols].copy()
        display_df.columns = ['Type', 'Company', 'Title', 'Published', 'Status']

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                'Type': st.column_config.TextColumn('Type', width='small'),
                'Company': st.column_config.TextColumn('Company', width='medium'),
                'Title': st.column_config.TextColumn('Title', width='large'),
                'Published': st.column_config.TextColumn('Published', width='small'),
                'Status': st.column_config.TextColumn('Status', width='medium')
            }
        )

        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            csv = df.to_csv(index=False)
            st.download_button(
                label="📥 Export CSV",
                data=csv,
                file_name=f"trigger_events_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                use_container_width=True
            )

    # Sidebar bulk actions
    st.sidebar.markdown("---")
    st.sidebar.markdown('<p class="sidebar-section-title">⚡ Bulk Actions</p>', unsafe_allow_html=True)
    st.sidebar.markdown('<p style="font-size:0.8rem;color:var(--sidebar-caption);margin:0">Apply to all NEW leads:</p>', unsafe_allow_html=True)

    bulk_status = st.sidebar.selectbox(
        "Mark all new as:",
        ["Select status..."] + LEAD_STATUSES,
        label_visibility="collapsed"
    )

    if bulk_status and bulk_status != "Select status..." and st.sidebar.button("✓ Apply to All New", use_container_width=True):
        client = get_supabase_client()
        if client:
            updated = failed = 0
            for event_id in new_df['id'].tolist():
                try:
                    # Same soft-delete rule as update_lead_status — a hard
                    # DELETE gets resurrected by the next supabase_sync upsert.
                    if bulk_status == "NOT RELEVANT":
                        client.table('events').update({
                            'blocked_at': datetime.now().isoformat(),
                            'blocked_reason': 'bulk-dismissed by rep (NOT RELEVANT)',
                            'lead_status': 'NOT RELEVANT',
                        }).eq('id', event_id).execute()
                    else:
                        client.table('events').update({'lead_status': bulk_status}).eq('id', event_id).execute()
                    updated += 1
                except Exception:
                    failed += 1
            if failed:
                st.sidebar.warning(f"✓ Updated {updated}, ✗ failed {failed}")
            else:
                st.sidebar.success(f"✓ Updated {updated} events!")
            st.rerun()

    # Sidebar footer
    st.sidebar.markdown("---")
    st.sidebar.markdown('<p style="font-size:0.75rem;color:var(--sidebar-caption);text-align:center;margin:0">🎯 Sales Trigger Events</p>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
