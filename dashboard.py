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

# Event type configurations with colors
EVENT_TYPES = {
    "merger_acquisition": {
        "label": "Mergers & Acquisitions",
        "color": "#1E90FF",  # Blue
        "icon": "🔵",
        "bg_color": "#E6F3FF"
    },
    "cfo_hire": {
        "label": "CFO Hires",
        "color": "#28A745",  # Green
        "icon": "🟢",
        "bg_color": "#E8F5E9"
    },
    "funding": {
        "label": "PE/VC Funding",
        "color": "#FFD700",  # Gold
        "icon": "🟡",
        "bg_color": "#FFF8E1"
    },
    "stable_target": {
        "label": "Stable Targets",
        "color": "#FF8C00",  # Orange
        "icon": "🟠",
        "bg_color": "#FFF3E0"
    },
    "executive_hire": {
        "label": "Executive Hires",
        "color": "#9370DB",  # Purple
        "icon": "🟣",
        "bg_color": "#F3E5F5"
    },
    "other": {
        "label": "Other Events",
        "color": "#6C757D",  # Gray
        "icon": "⚪",
        "bg_color": "#F5F5F5"
    }
}

# Lead status options
LEAD_STATUSES = [
    "NEW",
    "REVIEWED - ON REP TAL",
    "REVIEWED - NetSuite Customer",
    "REVIEWED - Out of Alignment"
]

STATUS_ICONS = {
    "NEW": "🆕",
    "REVIEWED - ON REP TAL": "🟠",
    "REVIEWED - NetSuite Customer": "💼",
    "REVIEWED - Out of Alignment": "❌"
}


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
    """Render a single event card with color styling."""
    status = row.get('lead_status', 'new') or 'new'
    title = str(row.get('title', ''))[:100]
    company = row.get('company_name') or 'Unknown Company'

    # Card with colored left border
    st.markdown(f"""
        <div style="
            border-left: 4px solid {event_config['color']};
            background-color: {event_config['bg_color']};
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 10px;
        ">
            <div style="font-weight: bold; font-size: 16px; color: #333;">
                {STATUS_ICONS.get(status, '🔵')} {title}
            </div>
            <div style="color: #666; margin-top: 5px;">
                🏢 {company}
            </div>
        </div>
    """, unsafe_allow_html=True)

    with st.expander("View Details & Update Status"):
        col1, col2 = st.columns([3, 1])

        with col1:
            st.markdown(f"**Company:** {company}")

            published = row.get('published_date', 'N/A')
            if published and published != 'N/A':
                st.markdown(f"**Published:** {published}")

            desc = row.get('description', '')
            if desc:
                st.markdown("**Description:**")
                st.text(str(desc)[:400] + "..." if len(str(desc)) > 400 else str(desc))

            url = row.get('url', '')
            if url:
                st.markdown(f"[🔗 View Source Article]({url})")

        with col2:
            current_status = status
            new_status = st.selectbox(
                "Status",
                LEAD_STATUSES,
                index=LEAD_STATUSES.index(current_status) if current_status in LEAD_STATUSES else 0,
                key=f"status_{row['id']}"
            )

            notes = st.text_area(
                "Notes",
                value=row.get('notes') or "",
                key=f"notes_{row['id']}",
                height=80
            )

            if st.button("💾 Save", key=f"save_{row['id']}"):
                if update_lead_status(row['id'], new_status, notes):
                    st.success("Saved!")
                    st.rerun()


def render_event_section(df, event_type, event_config, lead_filter):
    """Render a section for a specific event type."""
    # Filter by event type
    type_df = df[df['event_type'] == event_type]

    # Apply lead status filter
    if lead_filter:
        type_df = type_df[type_df['lead_status'].isin(lead_filter)]

    # Header with color
    st.markdown(f"""
        <h2 style="
            color: {event_config['color']};
            border-bottom: 3px solid {event_config['color']};
            padding-bottom: 10px;
            margin-top: 20px;
        ">
            {event_config['icon']} {event_config['label']} ({len(type_df)})
        </h2>
    """, unsafe_allow_html=True)

    if type_df.empty:
        st.info(f"No {event_config['label'].lower()} found matching your filters.")
        return

    # Render each event card
    for idx, row in type_df.iterrows():
        render_event_card(row, event_config)


def get_stats(df) -> dict:
    """Get dashboard statistics."""
    if df.empty:
        return {"total": 0, "by_type": {}, "new": 0}

    return {
        "total": len(df),
        "by_type": df['event_type'].value_counts().to_dict(),
        "new": len(df[df['lead_status'] == 'NEW'])
    }


def main():
    # Header
    st.markdown("""
        <h1 style="text-align: center; color: #333;">
            🎯 Sales Trigger Events Dashboard
        </h1>
    """, unsafe_allow_html=True)

    # Check Supabase connection
    client = get_supabase_client()
    if not client:
        st.warning("Supabase not configured")
        st.info("""
        **To connect to Supabase:**

        Add to Streamlit secrets:
        ```
        SUPABASE_URL = "https://your-project.supabase.co"
        SUPABASE_KEY = "your-anon-key"
        ```
        """)
        return

    # Sidebar filters
    st.sidebar.header("🔍 Filters")

    days = st.sidebar.slider("Days to show", 1, 90, 30)

    lead_filter = st.sidebar.multiselect(
        "Lead Status",
        LEAD_STATUSES,
        default=["NEW"]
    )

    # Prominent search bar in main content area
    search = st.text_input(
        "🔍 Search Events",
        placeholder="Search by company name, title, or keyword...",
        help="Filter events by company name, title, or description"
    )

    # Load all events
    df = load_events(days=days, search=search if search else None)

    if df.empty:
        st.info("No events found. Run the scraper to populate data.")
        return

    # Stats row
    stats = get_stats(df)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("📊 Total Events", stats["total"])
    with col2:
        st.metric("🆕 New Leads", stats["new"])
    with col3:
        ma_count = stats["by_type"].get("merger_acquisition", 0)
        st.metric("🔵 M&A", ma_count)
    with col4:
        cfo_count = stats["by_type"].get("cfo_hire", 0)
        st.metric("🟢 CFO Hires", cfo_count)

    st.divider()

    # Create tabs for each event type
    tab_ma, tab_cfo, tab_funding, tab_stable, tab_exec, tab_other, tab_all = st.tabs([
        "🔵 M&A",
        "🟢 CFO Hires",
        "🟡 PE/VC Funding",
        "🟠 Stable Targets",
        "🟣 Exec Hires",
        "⚪ Other",
        "📋 All Events"
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
            <h2 style="border-bottom: 2px solid #333; padding-bottom: 10px;">
                📋 All Events
            </h2>
        """, unsafe_allow_html=True)

        # Apply lead filter
        filtered_df = df[df['lead_status'].isin(lead_filter)] if lead_filter else df

        # Table view
        display_cols = ['event_type', 'company_name', 'title', 'published_date', 'lead_status']
        available_cols = [c for c in display_cols if c in filtered_df.columns]

        display_df = filtered_df[available_cols].copy()
        display_df.columns = ['Type', 'Company', 'Title', 'Published', 'Status']

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True
        )

        # Export
        csv = df.to_csv(index=False)
        st.download_button(
            label="📥 Export All to CSV",
            data=csv,
            file_name=f"trigger_events_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv"
        )

    # Sidebar bulk actions
    st.sidebar.divider()
    st.sidebar.header("⚡ Bulk Actions")

    bulk_status = st.sidebar.selectbox(
        "Mark visible as:",
        [""] + LEAD_STATUSES
    )

    if bulk_status and st.sidebar.button("Apply to All Visible"):
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
            st.sidebar.success(f"Updated {updated} events!")
            st.rerun()


if __name__ == "__main__":
    main()
