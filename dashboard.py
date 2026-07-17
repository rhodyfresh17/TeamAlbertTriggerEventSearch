#!/usr/bin/env python3
"""
Sales Trigger Events Dashboard

Interactive Streamlit dashboard for managing and reviewing trigger event alerts.
Reads from Supabase for online access.

Usage:
    streamlit run dashboard.py
"""

import os
import json
import base64
import urllib.parse
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st


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

REGIONS = {
    "New England": [
        "Maine", "Vermont", "New Hampshire", "Massachusetts", "Rhode Island", "Connecticut",
        "Boston", "Providence", "Hartford", "New Haven", "Stamford", "Bridgeport",
        "Worcester", "Springfield", "Manchester", "Portland", "Burlington", "Greenwich"
    ],
    "Mid-Atlantic": [
        "New York", "New Jersey", "Pennsylvania", "Delaware", "Maryland", "Virginia",
        "West Virginia", "Washington DC", "District of Columbia",
        "NYC", "New York City", "Philadelphia", "Pittsburgh", "Baltimore", "Richmond",
        "Arlington", "Alexandria", "Norfolk", "Newark", "Jersey City", "Trenton",
        "Wilmington", "Annapolis", "Bethesda", "Rockville", "McLean", "Reston",
        "Washington", "Hoboken", "Princeton", "Allentown", "Harrisburg", "Fairfax"
    ],
    "South East": [
        "North Carolina", "South Carolina", "Georgia", "Alabama", "Florida",
        "Tennessee", "Kentucky",
        "Charlotte", "Raleigh", "Durham", "Greensboro", "Atlanta", "Birmingham",
        "Miami", "Tampa", "Orlando", "Jacksonville", "Nashville", "Memphis",
        "Louisville", "Lexington", "Columbia", "Charleston", "Savannah",
        "Fort Lauderdale", "West Palm Beach", "Huntsville", "Knoxville", "Chattanooga"
    ],
    "Rust Belt": [
        "Ohio", "Michigan", "Indiana",
        "Columbus", "Cleveland", "Cincinnati", "Akron", "Toledo", "Dayton",
        "Detroit", "Grand Rapids", "Ann Arbor", "Lansing", "Indianapolis",
        "Fort Wayne", "Southfield", "Troy"
    ],
    "Canada": [
        "Ontario", "Quebec", "New Brunswick", "Newfoundland", "Nova Scotia",
        "Prince Edward Island", "PEI",
        "Toronto", "Montreal", "Ottawa", "Halifax", "Mississauga", "Brampton",
        "Hamilton", "Moncton", "Fredericton", "Quebec City", "Charlottetown",
        "St. John's", "Dartmouth", "Windsor", "London"
    ],
}


def filter_by_region(df: pd.DataFrame, selected_regions: list) -> pd.DataFrame:
    if not selected_regions:
        return df

    keywords = []
    for r in selected_regions:
        keywords.extend([k.lower() for k in REGIONS.get(r, [])])

    def matches(row):
        text = " ".join([
            str(row.get("title", "") or ""),
            str(row.get("description", "") or ""),
            str(row.get("company_name", "") or ""),
            str(row.get("matched_regions", "") or ""),
        ]).lower()
        return any(k in text for k in keywords)

    mask = df.apply(matches, axis=1)
    return df[mask]


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

# Lead status options
LEAD_STATUSES = [
    "NEW",
    "REVIEWED - ON REP TAL",
    "REVIEWED - NetSuite Customer",
    "REVIEWED - Out of Alignment",
    "NOT RELEVANT"
]

STATUS_CONFIG = {
    "NEW": {"icon": "🆕", "class": "status-new", "label": "New"},
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

    url = st.secrets.get("SUPABASE_URL") if hasattr(st, 'secrets') and "SUPABASE_URL" in st.secrets else os.environ.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY") if hasattr(st, 'secrets') and "SUPABASE_KEY" in st.secrets else os.environ.get("SUPABASE_KEY")

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


def load_events(days: int = 30, search: str = None) -> pd.DataFrame:
    """Load all events from Supabase."""
    client = get_supabase_client()
    if not client:
        return pd.DataFrame()

    try:
        query = client.table('events').select('*')
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        query = query.gte('discovered_at', cutoff_date)
        # Hide soft-deleted (industry-blocked) events. They stay in the table
        # so supabase_sync doesn't recreate them via upsert, but the user
        # never sees them. The is_('blocked_at', 'null') filter is omitted
        # if the column doesn't exist yet (pre-migration).
        try:
            query = query.is_('blocked_at', 'null')
        except Exception:
            pass  # column not yet present; will start filtering after migration

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


def _resolve_display_company(row) -> str:
    """Pick the best company name to show in the card header.
    Prefer scrape-time company_name when it's a real value; fall back to the
    primary enriched company (companies_data[0].name) so events with the
    old "?" or null company_name still get a real label."""
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
    grade_colors = {
        'A': '#10b981',  # emerald
        'B': '#3b82f6',  # blue
        'C': '#f59e0b',  # amber
        'D': '#6b7280',  # slate
    }
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
        desc = grade_descriptions[grade]
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
    fit_raw = row.get('fit')
    if isinstance(fit_raw, str) and fit_raw.strip():
        try:
            fit_raw = json.loads(fit_raw)
        except Exception:
            fit_raw = None
    if isinstance(fit_raw, dict) and fit_raw.get('verdict') == 'unverified':
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
                    for co in companies_data:
                        co_name     = _v(co.get('name'))
                        co_role     = _v(co.get('role'))
                        co_url      = _v(co.get('url'))
                        co_industry = _v(co.get('industry'))
                        co_size     = _v(co.get('size'))
                        co_revenue  = _v(co.get('revenue'))
                        co_hq       = _v(co.get('hq'))
                        co_linkedin = _v(co.get('linkedin'))

                        # Name + role header
                        role_html = (
                            f"<span style='font-size:0.72rem;color:rgba(78,140,170,0.9);"
                            f"font-weight:600;margin-left:6px;'>{co_role}</span>"
                            if co_role else ""
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
                current_status = status
                new_status = st.selectbox(
                    "Status",
                    LEAD_STATUSES,
                    index=LEAD_STATUSES.index(current_status) if current_status in LEAD_STATUSES else 0,
                    key=f"{key_prefix}status_{row['id']}",
                    label_visibility="collapsed"
                )

                notes = st.text_area(
                    "Notes",
                    value=row.get('notes') or "",
                    key=f"{key_prefix}notes_{row['id']}",
                    height=80,
                    placeholder="Add notes..."
                )

                if st.button("💾 Save Changes", key=f"{key_prefix}save_{row['id']}", use_container_width=True):
                    if update_lead_status(row['id'], new_status, notes):
                        if new_status == "NOT RELEVANT":
                            st.success("✓ Event removed!")
                        else:
                            st.success("✓ Saved!")
                        st.rerun()


def render_event_section(df, event_type, event_config, lead_filter):
    """Render a section for a specific event type."""
    # Filter by event type
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


_GRADE_RANK = {'A': 0, 'B': 1, None: 2, '': 2, 'C': 3, 'D': 4}


def _grade_rank(g):
    """Sort key: A first, then B, then ungraded (fresh events awaiting
    enrichment shouldn't sink below C/D junk), then C, then D."""
    if g is None or (isinstance(g, float) and g != g):
        return 2
    return _GRADE_RANK.get(str(g).strip().upper(), 2)


def render_work_queue(new_df: pd.DataFrame, top_n: int = 10):
    """The Monday-morning view: ONE ranked list across all event types,
    rolled up per company. Ranking: grade → numeric score → freshness."""
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

    # Rank events: grade, then score desc, then freshness desc
    def _score(r):
        s = r.get('numeric_score')
        if s is None or (isinstance(s, float) and s != s):
            return -1
        try:
            return int(s)
        except Exception:
            return -1

    rows.sort(key=lambda r: (
        _grade_rank(r.get('grade')),
        -_score(r),
        str(r.get('discovered_date') or ''),
    ))
    # For freshness DESC within same grade+score, re-sort stably:
    rows.sort(key=lambda r: (
        _grade_rank(r.get('grade')),
        -_score(r),
    ))

    # Roll up per company — the best-ranked event represents the account
    by_company = {}
    order = []
    for r in rows:
        key = _resolve_display_company(r).strip().lower()
        if key in ('unknown company', ''):
            key = f"__solo_{r.get('id')}"  # don't merge unknowns together
        if key not in by_company:
            by_company[key] = {'top': r, 'others': 0}
            order.append(key)
        else:
            by_company[key]['others'] += 1

    shown = 0
    for key in order:
        if shown >= top_n:
            break
        entry = by_company[key]
        r = entry['top']
        event_config = EVENT_TYPES.get(r.get('event_type'), EVENT_TYPES['other'])
        render_event_card(r, event_config, key_prefix='wq_')
        if entry['others']:
            st.caption(f"    ↳ +{entry['others']} more event(s) for this "
                       f"company in the New Leads tabs below")
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
        with st.popover("🎛️  Filters", use_container_width=True):
            days = st.slider("Time Range (days)", 1, 90, 30, key="flt_days")

            st.markdown("**📍 Region**")
            selected_regions = st.multiselect(
                "Region",
                options=list(REGIONS.keys()),
                default=[],
                placeholder="All regions",
                label_visibility="collapsed",
                key="flt_regions",
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

    with search_col:
        st.markdown('<div class="search-container">', unsafe_allow_html=True)
        search = st.text_input(
            "Search",
            placeholder="🔍 Search by company, title, or keyword...",
            label_visibility="collapsed",
            key="flt_search",
        )
        st.markdown('</div>', unsafe_allow_html=True)

    # Load all events
    df = load_events(days=days, search=search if search else None)

    if df.empty:
        st.info("📭 No events found. Run the scraper to populate data.")
        return

    df = filter_by_region(df, selected_regions)

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

    # Stats
    stats = get_stats(df)
    ma_count = stats["by_type"].get("merger_acquisition", 0)
    cfo_count = stats["by_type"].get("cfo_hire", 0)
    funding_count = stats["by_type"].get("funding", 0)

    # New Finance Leaders = CFO-hire events + any event carrying the
    # #NewController hashtag (Controller hires stay event_type=
    # executive_hire by design — see enrichment_scout._finance_role).
    # This is THE highest-value trigger, so it gets its own card.
    def _has_controller_tag(h):
        if isinstance(h, str):
            try:
                h = json.loads(h)
            except Exception:
                return False
        return isinstance(h, list) and '#NewController' in h
    controller_count = int(df['hashtags'].apply(_has_controller_tag).sum()) \
        if 'hashtags' in df.columns else 0
    finance_leader_count = cfo_count + controller_count

    # Modern metric cards
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        render_metric_card("📊", stats["total"], "Total Events", "#667eea")
    with col2:
        render_metric_card("🆕", stats["new"], "New Leads", "#10b981")
    with col3:
        render_metric_card("💼", finance_leader_count, "New Finance Leaders", "#8b5cf6")
    with col4:
        render_metric_card("🔵", ma_count, "M&A Events", "#3b82f6")
    with col5:
        render_metric_card("💰", funding_count, "Funding", "#f59e0b")

    st.markdown("<br>", unsafe_allow_html=True)

    # Source Status Section
    with st.expander("📡 Source Health Status", expanded=False):
        source_status_df = load_source_statuses()
        render_source_status_table(source_status_df)

    st.markdown("<br>", unsafe_allow_html=True)

    # Split data: new (unclassified) vs classified
    new_df = df[df['lead_status'] == 'NEW']
    classified_df = df[df['lead_status'] != 'NEW']

    # ── 🔥 WORK QUEUE — the answer to "which accounts do I work?" ──────────
    # One ranked list across ALL event types: grade first (A→B→ungraded),
    # then numeric score, then freshness. Rolled up per company so one
    # account with 4 events is one row, not four cards.
    render_work_queue(new_df)

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
        new_funding = len(new_df[new_df['event_type'] == 'funding'])
        new_stable = len(new_df[new_df['event_type'] == 'stable_target'])
        new_exec = len(new_df[new_df['event_type'] == 'executive_hire'])
        new_other = len(new_df[new_df['event_type'] == 'other'])

        tab_ma, tab_cfo, tab_funding, tab_stable, tab_exec, tab_other = st.tabs([
            f"🔵 M&A ({new_ma})",
            f"💼 CFO ({new_cfo})",
            f"💰 Funding ({new_funding})",
            f"🎯 Stable ({new_stable})",
            f"👔 Exec ({new_exec})",
            f"📋 Other ({new_other})"
        ])
        with tab_ma:
            render_event_section(new_df, "merger_acquisition", EVENT_TYPES["merger_acquisition"], None)
        with tab_cfo:
            render_event_section(new_df, "cfo_hire", EVENT_TYPES["cfo_hire"], None)
        with tab_funding:
            render_event_section(new_df, "funding", EVENT_TYPES["funding"], None)
        with tab_stable:
            render_event_section(new_df, "stable_target", EVENT_TYPES["stable_target"], None)
        with tab_exec:
            render_event_section(new_df, "executive_hire", EVENT_TYPES["executive_hire"], None)
        with tab_other:
            render_event_section(new_df, "other", EVENT_TYPES["other"], None)

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
                        event_type = row.get('event_type', 'other')
                        event_config = EVENT_TYPES.get(event_type, EVENT_TYPES['other'])
                        render_event_card(row, event_config)

    st.markdown("<br>", unsafe_allow_html=True)

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
