#!/usr/bin/env python3
"""
Sales Trigger Events Dashboard

Interactive Streamlit dashboard for managing and reviewing trigger event alerts.
Reads from Supabase for online access.

Usage:
    streamlit run dashboard.py
"""

import os
import pandas as pd
from datetime import datetime, timedelta

import streamlit as st

# Page config
st.set_page_config(
    page_title="Sales Trigger Events",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for modern UI
st.markdown("""
<style>
    /* Import modern font */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    /* Hide Streamlit branding */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}

    /* Global styles */
    .stApp {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    /* Modern header */
    .main-header {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 2rem 2.5rem;
        border-radius: 16px;
        margin-bottom: 2rem;
        box-shadow: 0 10px 40px rgba(102, 126, 234, 0.3);
    }

    .main-header h1 {
        color: white;
        font-size: 2rem;
        font-weight: 700;
        margin: 0;
        letter-spacing: -0.5px;
    }

    .main-header p {
        color: rgba(255,255,255,0.85);
        font-size: 1rem;
        margin-top: 0.5rem;
    }

    /* Modern metric cards */
    .metric-card {
        background: white;
        border-radius: 16px;
        padding: 1.5rem;
        box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        border: 1px solid rgba(0,0,0,0.05);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }

    .metric-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 30px rgba(0,0,0,0.12);
    }

    .metric-icon {
        width: 48px;
        height: 48px;
        border-radius: 12px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.5rem;
        margin-bottom: 1rem;
    }

    .metric-value {
        font-size: 2rem;
        font-weight: 700;
        color: #1a1a2e;
        line-height: 1;
    }

    .metric-label {
        font-size: 0.875rem;
        color: #6b7280;
        margin-top: 0.5rem;
        font-weight: 500;
    }

    .metric-trend {
        font-size: 0.75rem;
        margin-top: 0.5rem;
        padding: 0.25rem 0.5rem;
        border-radius: 6px;
        display: inline-block;
    }

    .trend-up {
        background: #d1fae5;
        color: #065f46;
    }

    .trend-down {
        background: #fee2e2;
        color: #991b1b;
    }

    /* Event cards */
    .event-card {
        background: white;
        border-radius: 12px;
        padding: 1.25rem;
        margin-bottom: 1rem;
        box-shadow: 0 2px 12px rgba(0,0,0,0.06);
        border: 1px solid rgba(0,0,0,0.06);
        transition: all 0.2s ease;
    }

    .event-card:hover {
        box-shadow: 0 8px 25px rgba(0,0,0,0.1);
        transform: translateY(-1px);
    }

    .event-card-header {
        display: flex;
        align-items: flex-start;
        gap: 1rem;
    }

    .event-type-badge {
        padding: 0.35rem 0.75rem;
        border-radius: 50px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        white-space: nowrap;
    }

    .badge-ma { background: #dbeafe; color: #1e40af; }
    .badge-cfo { background: #d1fae5; color: #065f46; }
    .badge-funding { background: #fef3c7; color: #92400e; }
    .badge-stable { background: #ffedd5; color: #9a3412; }
    .badge-exec { background: #ede9fe; color: #5b21b6; }
    .badge-other { background: #f3f4f6; color: #374151; }

    .status-badge {
        padding: 0.25rem 0.6rem;
        border-radius: 50px;
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
    }

    .status-new { background: #dbeafe; color: #1e40af; }
    .status-reviewed { background: #d1fae5; color: #065f46; }
    .status-customer { background: #fef3c7; color: #92400e; }
    .status-out { background: #fee2e2; color: #991b1b; }

    .event-title {
        font-size: 1rem;
        font-weight: 600;
        color: #1a1a2e;
        margin: 0.5rem 0;
        line-height: 1.4;
    }

    .event-company {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        color: #6b7280;
        font-size: 0.875rem;
    }

    .event-meta {
        display: flex;
        gap: 1rem;
        margin-top: 0.75rem;
        font-size: 0.8rem;
        color: #9ca3af;
    }

    /* Modern search bar */
    .search-container {
        background: white;
        border-radius: 12px;
        padding: 0.5rem;
        box-shadow: 0 2px 12px rgba(0,0,0,0.06);
        margin-bottom: 1.5rem;
    }

    /* Pill navigation */
    .pill-nav {
        display: flex;
        gap: 0.5rem;
        padding: 0.5rem;
        background: #f8fafc;
        border-radius: 12px;
        margin-bottom: 1.5rem;
        flex-wrap: wrap;
    }

    .pill-btn {
        padding: 0.6rem 1.2rem;
        border-radius: 8px;
        font-size: 0.875rem;
        font-weight: 500;
        cursor: pointer;
        border: none;
        transition: all 0.2s ease;
        background: transparent;
        color: #64748b;
    }

    .pill-btn:hover {
        background: white;
        color: #1a1a2e;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    }

    .pill-btn.active {
        background: white;
        color: #667eea;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }

    /* Section headers */
    .section-header {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        margin: 1.5rem 0 1rem;
        padding-bottom: 0.75rem;
        border-bottom: 2px solid #f1f5f9;
    }

    .section-header h2 {
        font-size: 1.25rem;
        font-weight: 600;
        color: #1a1a2e;
        margin: 0;
    }

    .section-count {
        background: #f1f5f9;
        padding: 0.25rem 0.75rem;
        border-radius: 50px;
        font-size: 0.8rem;
        font-weight: 600;
        color: #64748b;
    }

    /* Sidebar styling */
    .css-1d391kg {
        background: #fafbfc;
    }

    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #fafbfc 0%, #f1f5f9 100%);
    }

    /* Better buttons */
    .stButton > button {
        border-radius: 8px;
        font-weight: 500;
        transition: all 0.2s ease;
    }

    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }

    /* Better expanders */
    .streamlit-expanderHeader {
        font-weight: 500;
        font-size: 0.9rem;
    }

    /* Divider override */
    hr {
        border: none;
        height: 1px;
        background: #e5e7eb;
        margin: 1.5rem 0;
    }
</style>
""", unsafe_allow_html=True)

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
    "REVIEWED - Out of Alignment"
]

STATUS_CONFIG = {
    "NEW": {"icon": "🆕", "class": "status-new", "label": "New"},
    "REVIEWED - ON REP TAL": {"icon": "🟠", "class": "status-reviewed", "label": "On TAL"},
    "REVIEWED - NetSuite Customer": {"icon": "💼", "class": "status-customer", "label": "Customer"},
    "REVIEWED - Out of Alignment": {"icon": "❌", "class": "status-out", "label": "Out"}
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
        response = query.order('discovered_at', desc=True).limit(1000).execute()

        if not response.data:
            return pd.DataFrame()

        df = pd.DataFrame(response.data)

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
    """Update lead status for an event in Supabase."""
    client = get_supabase_client()
    if not client:
        return False

    try:
        data = {'lead_status': status}
        if notes is not None:
            data['notes'] = notes

        client.table('events').update(data).eq('id', event_id).execute()
        return True
    except Exception as e:
        st.error(f"Error updating status: {e}")
        return False


def render_event_card(row, event_config):
    """Render a single event card with modern styling."""
    status = row.get('lead_status', 'NEW') or 'NEW'
    title = str(row.get('title', ''))[:100]
    company = row.get('company_name') or 'Unknown Company'
    published = row.get('published_date', '')

    # Format date
    date_display = ''
    if published:
        try:
            if isinstance(published, str):
                dt = datetime.fromisoformat(published.replace('Z', '+00:00'))
            else:
                dt = published
            date_display = dt.strftime('%b %d, %Y')
        except:
            date_display = str(published)[:10]

    status_cfg = STATUS_CONFIG.get(status, STATUS_CONFIG["NEW"])
    badge_class = event_config.get('badge_class', 'badge-other')

    # Modern card
    st.markdown(f"""
        <div class="event-card">
            <div class="event-card-header">
                <span class="event-type-badge {badge_class}">{event_config['icon']} {event_config['label']}</span>
                <span class="status-badge {status_cfg['class']}">{status_cfg['label']}</span>
            </div>
            <div class="event-title">{title}</div>
            <div class="event-company">
                <span>🏢</span>
                <span>{company}</span>
            </div>
            <div class="event-meta">
                <span>📅 {date_display}</span>
            </div>
        </div>
    """, unsafe_allow_html=True)

    with st.expander("📝 Details & Actions"):
        col1, col2 = st.columns([2, 1])

        with col1:
            desc = row.get('description', '')
            if desc:
                st.markdown("**Description**")
                st.caption(str(desc)[:500] + "..." if len(str(desc)) > 500 else str(desc))

            url = row.get('url', '')
            if url:
                st.link_button("🔗 View Source", url, use_container_width=False)

        with col2:
            current_status = status
            new_status = st.selectbox(
                "Status",
                LEAD_STATUSES,
                index=LEAD_STATUSES.index(current_status) if current_status in LEAD_STATUSES else 0,
                key=f"status_{row['id']}",
                label_visibility="collapsed"
            )

            notes = st.text_area(
                "Notes",
                value=row.get('notes') or "",
                key=f"notes_{row['id']}",
                height=80,
                placeholder="Add notes..."
            )

            if st.button("💾 Save Changes", key=f"save_{row['id']}", use_container_width=True):
                if update_lead_status(row['id'], new_status, notes):
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


def main():
    # Modern gradient header
    st.markdown("""
        <div class="main-header">
            <h1>🎯 Sales Trigger Events</h1>
            <p>Track M&A, executive hires, and funding events in your territory</p>
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
        SUPABASE_KEY = "your-anon-key"
        ```
        """)
        return

    # Sidebar filters with modern styling
    st.sidebar.markdown("### 🎛️ Filters")

    days = st.sidebar.slider("Time Range (days)", 1, 90, 30)

    st.sidebar.markdown("**Lead Status**")
    lead_filter = st.sidebar.multiselect(
        "Filter by status",
        LEAD_STATUSES,
        default=["NEW"],
        label_visibility="collapsed"
    )

    # Modern search bar
    st.markdown('<div class="search-container">', unsafe_allow_html=True)
    search = st.text_input(
        "Search",
        placeholder="🔍 Search by company, title, or keyword...",
        label_visibility="collapsed"
    )
    st.markdown('</div>', unsafe_allow_html=True)

    # Load all events
    df = load_events(days=days, search=search if search else None)

    if df.empty:
        st.info("📭 No events found. Run the scraper to populate data.")
        return

    # Stats
    stats = get_stats(df)
    ma_count = stats["by_type"].get("merger_acquisition", 0)
    cfo_count = stats["by_type"].get("cfo_hire", 0)
    funding_count = stats["by_type"].get("funding", 0)

    # Modern metric cards
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        render_metric_card("📊", stats["total"], "Total Events", "#667eea")
    with col2:
        render_metric_card("🆕", stats["new"], "New Leads", "#10b981")
    with col3:
        render_metric_card("🔵", ma_count, "M&A Events", "#3b82f6")
    with col4:
        render_metric_card("💰", funding_count, "Funding", "#f59e0b")

    st.markdown("<br>", unsafe_allow_html=True)

    # Source Status Section
    with st.expander("📡 Source Health Status", expanded=False):
        source_status_df = load_source_statuses()
        render_source_status_table(source_status_df)

    st.markdown("<br>", unsafe_allow_html=True)

    # Modern pill-style tabs
    tab_ma, tab_cfo, tab_funding, tab_stable, tab_exec, tab_other, tab_all = st.tabs([
        f"🔵 M&A ({ma_count})",
        f"💼 CFO ({cfo_count})",
        f"💰 Funding ({funding_count})",
        f"🎯 Stable ({stats['by_type'].get('stable_target', 0)})",
        f"👔 Exec ({stats['by_type'].get('executive_hire', 0)})",
        f"📋 Other ({stats['by_type'].get('other', 0)})",
        "📊 All"
    ])

    with tab_ma:
        render_event_section(df, "merger_acquisition", EVENT_TYPES["merger_acquisition"], lead_filter)

    with tab_cfo:
        render_event_section(df, "cfo_hire", EVENT_TYPES["cfo_hire"], lead_filter)

    with tab_funding:
        render_event_section(df, "funding", EVENT_TYPES["funding"], lead_filter)

    with tab_stable:
        render_event_section(df, "stable_target", EVENT_TYPES["stable_target"], lead_filter)

    with tab_exec:
        render_event_section(df, "executive_hire", EVENT_TYPES["executive_hire"], lead_filter)

    with tab_other:
        render_event_section(df, "other", EVENT_TYPES["other"], lead_filter)

    with tab_all:
        st.markdown("""
            <div class="section-header">
                <span style="font-size: 1.5rem;">📊</span>
                <h2>All Events</h2>
            </div>
        """, unsafe_allow_html=True)

        # Apply lead filter
        filtered_df = df[df['lead_status'].isin(lead_filter)] if lead_filter else df

        # Table view with modern styling
        display_cols = ['event_type', 'company_name', 'title', 'published_date', 'lead_status']
        available_cols = [c for c in display_cols if c in filtered_df.columns]

        display_df = filtered_df[available_cols].copy()
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

        # Export button with modern styling
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
    st.sidebar.markdown("### ⚡ Bulk Actions")

    bulk_status = st.sidebar.selectbox(
        "Mark visible as:",
        ["Select status..."] + LEAD_STATUSES,
        label_visibility="collapsed"
    )

    if bulk_status and bulk_status != "Select status..." and st.sidebar.button("✓ Apply to Visible", use_container_width=True):
        client = get_supabase_client()
        if client and lead_filter:
            visible_df = df[df['lead_status'].isin(lead_filter)]
            updated = 0
            for event_id in visible_df['id'].tolist():
                try:
                    client.table('events').update({'lead_status': bulk_status}).eq('id', event_id).execute()
                    updated += 1
                except:
                    pass
            st.sidebar.success(f"✓ Updated {updated} events!")
            st.rerun()

    # Sidebar footer
    st.sidebar.markdown("---")
    st.sidebar.caption("🎯 Sales Trigger Events Dashboard")


if __name__ == "__main__":
    main()
