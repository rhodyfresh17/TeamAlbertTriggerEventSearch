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

# Lead status options
LEAD_STATUSES = [
    "new",
    "reviewing",
    "contacted",
    "interested",
    "not_relevant",
    "closed_won",
    "closed_lost"
]

STATUS_COLORS = {
    "new": "🔵",
    "reviewing": "🟡",
    "contacted": "🟠",
    "interested": "🟢",
    "not_relevant": "⚫",
    "closed_won": "✅",
    "closed_lost": "❌"
}


@st.cache_resource
def get_supabase_client():
    """Get Supabase client."""
    try:
        from supabase import create_client
    except ImportError:
        st.error("Supabase not installed. Run: pip install supabase")
        return None

    # Try Streamlit secrets first, then environment variables
    url = st.secrets.get("SUPABASE_URL") if hasattr(st, 'secrets') and "SUPABASE_URL" in st.secrets else os.environ.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY") if hasattr(st, 'secrets') and "SUPABASE_KEY" in st.secrets else os.environ.get("SUPABASE_KEY")

    if not url or not key:
        return None

    return create_client(url, key)


def load_events(
    event_types: list = None,
    lead_statuses: list = None,
    days: int = 30,
    search: str = None
) -> pd.DataFrame:
    """Load events from Supabase with filters."""
    client = get_supabase_client()
    if not client:
        return pd.DataFrame()

    try:
        # Start query
        query = client.table('events').select('*')

        # Date filter
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        query = query.gte('discovered_at', cutoff_date)

        # Execute query
        response = query.order('discovered_at', desc=True).limit(500).execute()

        if not response.data:
            return pd.DataFrame()

        df = pd.DataFrame(response.data)

        # Apply filters in pandas (more flexible)
        if event_types:
            df = df[df['event_type'].isin(event_types)]

        if lead_statuses:
            df['lead_status'] = df['lead_status'].fillna('new')
            df = df[df['lead_status'].isin(lead_statuses)]

        if search:
            search_lower = search.lower()
            mask = (
                df['title'].str.lower().str.contains(search_lower, na=False) |
                df['company_name'].str.lower().str.contains(search_lower, na=False) |
                df['description'].str.lower().str.contains(search_lower, na=False)
            )
            df = df[mask]

        # Rename columns to match expected format
        df = df.rename(columns={
            'source_url': 'url',
            'discovered_at': 'discovered_date'
        })

        # Add missing columns with defaults
        for col in ['source', 'company_location', 'person_name', 'person_title',
                    'matched_keywords', 'matched_regions', 'relevance_score']:
            if col not in df.columns:
                df[col] = '' if col != 'relevance_score' else 50

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


def get_stats() -> dict:
    """Get dashboard statistics from Supabase."""
    client = get_supabase_client()
    if not client:
        return {"total": 0, "by_type": {}, "by_status": {}, "last_24h": 0, "last_7d": 0}

    try:
        # Get all events
        response = client.table('events').select('event_type, lead_status, discovered_at').execute()

        if not response.data:
            return {"total": 0, "by_type": {}, "by_status": {}, "last_24h": 0, "last_7d": 0}

        df = pd.DataFrame(response.data)

        total = len(df)

        # Events by type
        by_type = df['event_type'].value_counts().to_dict()

        # Events by status
        df['lead_status'] = df['lead_status'].fillna('new')
        by_status = df['lead_status'].value_counts().to_dict()

        # Time-based stats
        df['discovered_at'] = pd.to_datetime(df['discovered_at'])
        now = datetime.now()

        last_24h = len(df[df['discovered_at'] > (now - timedelta(hours=24))])
        last_7d = len(df[df['discovered_at'] > (now - timedelta(days=7))])

        return {
            "total": total,
            "by_type": by_type,
            "by_status": by_status,
            "last_24h": last_24h,
            "last_7d": last_7d
        }

    except Exception as e:
        st.error(f"Error getting stats: {e}")
        return {"total": 0, "by_type": {}, "by_status": {}, "last_24h": 0, "last_7d": 0}


def main():
    st.title("🎯 Sales Trigger Events Dashboard")

    # Check Supabase connection
    client = get_supabase_client()
    if not client:
        st.warning("Supabase not configured")
        st.info("""
        **To connect to Supabase:**

        1. Set environment variables:
           - `SUPABASE_URL` - Your Supabase project URL
           - `SUPABASE_KEY` - Your Supabase anon key

        2. Or add to `.streamlit/secrets.toml`:
           ```
           SUPABASE_URL = "https://your-project.supabase.co"
           SUPABASE_KEY = "your-anon-key"
           ```
        """)
        return

    # Sidebar filters
    st.sidebar.header("Filters")

    # Date range
    days = st.sidebar.slider("Days to show", 1, 90, 30)

    # Event type filter
    event_types = st.sidebar.multiselect(
        "Event Types",
        ["cfo_hire", "executive_hire", "merger_acquisition", "funding", "stable_target", "other"],
        default=["cfo_hire", "executive_hire", "merger_acquisition", "funding", "stable_target"]
    )

    # Lead status filter
    lead_statuses = st.sidebar.multiselect(
        "Lead Status",
        LEAD_STATUSES,
        default=["new", "reviewing", "contacted", "interested"]
    )

    # Search
    search = st.sidebar.text_input("Search", placeholder="Company, title, or keyword...")

    # Stats section
    stats = get_stats()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Events", stats["total"])
    with col2:
        st.metric("Last 24 Hours", stats["last_24h"])
    with col3:
        st.metric("Last 7 Days", stats["last_7d"])
    with col4:
        new_count = stats["by_status"].get("new", 0)
        st.metric("New Leads", new_count)

    st.divider()

    # Load events
    df = load_events(
        event_types=event_types if event_types else None,
        lead_statuses=lead_statuses if lead_statuses else None,
        days=days,
        search=search if search else None
    )

    if df.empty:
        st.info("No events found matching your filters.")
        return

    st.subheader(f"📋 Events ({len(df)} results)")

    # Tabs for different views
    tab1, tab2, tab3 = st.tabs(["Card View", "Table View", "Analytics"])

    with tab1:
        # Card view for detailed review
        for idx, row in df.iterrows():
            status = row.get('lead_status', 'new') or 'new'
            title = str(row.get('title', ''))[:80]
            event_type = str(row.get('event_type', 'other')).upper()

            with st.expander(f"{STATUS_COLORS.get(status, '🔵')} [{event_type}] {title}..."):
                col1, col2 = st.columns([3, 1])

                with col1:
                    st.markdown(f"**Company:** {row.get('company_name') or 'Unknown'}")
                    st.markdown(f"**Source:** {row.get('source', 'N/A')}")
                    st.markdown(f"**Published:** {row.get('published_date', 'N/A')}")

                    relevance = row.get('relevance_score', 50)
                    if relevance:
                        st.markdown(f"**Relevance:** {relevance:.0f}%")

                    desc = row.get('description', '')
                    if desc:
                        st.markdown("**Description:**")
                        st.text(desc[:300] + "..." if len(str(desc)) > 300 else desc)

                    url = row.get('url', '')
                    if url:
                        st.markdown(f"[🔗 View Article]({url})")

                with col2:
                    # Status update
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
                        height=100
                    )

                    if st.button("Save", key=f"save_{row['id']}"):
                        if update_lead_status(row['id'], new_status, notes):
                            st.success("Saved!")
                            st.rerun()

    with tab2:
        # Table view for quick scanning
        display_cols = ['event_type', 'company_name', 'title', 'published_date', 'lead_status']
        available_cols = [c for c in display_cols if c in df.columns]

        display_df = df[available_cols].copy()
        display_df.columns = [c.replace('_', ' ').title() for c in available_cols]

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True
        )

        # Export button
        csv = df.to_csv(index=False)
        st.download_button(
            label="📥 Export to CSV",
            data=csv,
            file_name=f"trigger_events_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv"
        )

    with tab3:
        # Analytics
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Events by Type")
            if 'event_type' in df.columns:
                type_counts = df['event_type'].value_counts()
                st.bar_chart(type_counts)

        with col2:
            st.subheader("Events by Status")
            if 'lead_status' in df.columns:
                status_counts = df['lead_status'].value_counts()
                st.bar_chart(status_counts)

        # Events over time
        st.subheader("Events Over Time")
        if 'published_date' in df.columns:
            try:
                df['date'] = pd.to_datetime(df['published_date']).dt.date
                daily_counts = df.groupby('date').size()
                st.line_chart(daily_counts)
            except:
                st.info("Unable to parse dates for timeline")

        # Top companies
        st.subheader("Top Companies")
        if 'company_name' in df.columns:
            company_counts = df['company_name'].value_counts().head(10)
            st.bar_chart(company_counts)

    # Bulk actions
    st.sidebar.divider()
    st.sidebar.header("Bulk Actions")

    bulk_status = st.sidebar.selectbox(
        "Mark all visible as:",
        [""] + LEAD_STATUSES
    )

    if bulk_status and st.sidebar.button("Apply to All"):
        client = get_supabase_client()
        if client:
            updated = 0
            for event_id in df['id'].tolist():
                try:
                    client.table('events').update({'lead_status': bulk_status}).eq('id', event_id).execute()
                    updated += 1
                except:
                    pass
            st.sidebar.success(f"Updated {updated} events!")
            st.rerun()


if __name__ == "__main__":
    main()
