#!/usr/bin/env python3
"""
Sales Trigger Events Dashboard

Interactive Streamlit dashboard for managing and reviewing trigger event alerts.

Usage:
    streamlit run dashboard.py
"""

import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

# Page config
st.set_page_config(
    page_title="Sales Trigger Events",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Database path
DB_PATH = "trigger_events.db"

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


def get_connection():
    """Get database connection."""
    return sqlite3.connect(DB_PATH)


def load_events(
    event_types: list = None,
    lead_statuses: list = None,
    days: int = 30,
    search: str = None
) -> pd.DataFrame:
    """Load events from database with filters."""
    conn = get_connection()

    query = """
        SELECT
            id,
            title,
            event_type,
            source,
            url,
            published_date,
            discovered_date,
            company_name,
            company_location,
            description,
            person_name,
            person_title,
            matched_keywords,
            matched_regions,
            relevance_score,
            COALESCE(lead_status, 'new') as lead_status,
            notes
        FROM events
        WHERE discovered_date > ?
    """

    params = [(datetime.now() - timedelta(days=days)).isoformat()]

    if event_types:
        placeholders = ','.join(['?' for _ in event_types])
        query += f" AND event_type IN ({placeholders})"
        params.extend(event_types)

    if lead_statuses:
        placeholders = ','.join(['?' for _ in lead_statuses])
        query += f" AND COALESCE(lead_status, 'new') IN ({placeholders})"
        params.extend(lead_statuses)

    if search:
        query += " AND (title LIKE ? OR company_name LIKE ? OR description LIKE ?)"
        search_pattern = f"%{search}%"
        params.extend([search_pattern, search_pattern, search_pattern])

    query += " ORDER BY published_date DESC"

    df = pd.read_sql_query(query, conn, params=params)
    conn.close()

    return df


def update_lead_status(event_id: str, status: str, notes: str = None):
    """Update lead status for an event."""
    conn = get_connection()
    cursor = conn.cursor()

    if notes is not None:
        cursor.execute(
            "UPDATE events SET lead_status = ?, notes = ? WHERE id = ?",
            (status, notes, event_id)
        )
    else:
        cursor.execute(
            "UPDATE events SET lead_status = ? WHERE id = ?",
            (status, event_id)
        )

    conn.commit()
    conn.close()


def get_stats() -> dict:
    """Get dashboard statistics."""
    conn = get_connection()
    cursor = conn.cursor()

    # Total events
    cursor.execute("SELECT COUNT(*) FROM events")
    total = cursor.fetchone()[0]

    # Events by type
    cursor.execute("""
        SELECT event_type, COUNT(*)
        FROM events
        GROUP BY event_type
    """)
    by_type = dict(cursor.fetchall())

    # Events by status
    cursor.execute("""
        SELECT COALESCE(lead_status, 'new'), COUNT(*)
        FROM events
        GROUP BY COALESCE(lead_status, 'new')
    """)
    by_status = dict(cursor.fetchall())

    # Last 24 hours
    cursor.execute("""
        SELECT COUNT(*) FROM events
        WHERE discovered_date > ?
    """, ((datetime.now() - timedelta(hours=24)).isoformat(),))
    last_24h = cursor.fetchone()[0]

    # Last 7 days
    cursor.execute("""
        SELECT COUNT(*) FROM events
        WHERE discovered_date > ?
    """, ((datetime.now() - timedelta(days=7)).isoformat(),))
    last_7d = cursor.fetchone()[0]

    conn.close()

    return {
        "total": total,
        "by_type": by_type,
        "by_status": by_status,
        "last_24h": last_24h,
        "last_7d": last_7d
    }


def main():
    st.title("🎯 Sales Trigger Events Dashboard")

    # Check if database exists
    if not Path(DB_PATH).exists():
        st.warning(f"Database not found at {DB_PATH}")
        st.info("Run the scraper first to generate events: `python -m src.main`")
        return

    # Sidebar filters
    st.sidebar.header("Filters")

    # Date range
    days = st.sidebar.slider("Days to show", 1, 90, 30)

    # Event type filter
    event_types = st.sidebar.multiselect(
        "Event Types",
        ["cfo_hire", "executive_hire", "merger_acquisition", "funding", "other"],
        default=["cfo_hire", "executive_hire", "merger_acquisition", "funding"]
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
            with st.expander(
                f"{STATUS_COLORS.get(row['lead_status'], '🔵')} "
                f"[{row['event_type'].upper()}] {row['title'][:80]}..."
            ):
                col1, col2 = st.columns([3, 1])

                with col1:
                    st.markdown(f"**Company:** {row['company_name'] or 'Unknown'}")
                    st.markdown(f"**Source:** {row['source']}")
                    st.markdown(f"**Published:** {row['published_date']}")
                    st.markdown(f"**Relevance:** {row['relevance_score']:.0f}%")

                    if row['description']:
                        st.markdown("**Description:**")
                        st.text(row['description'][:300] + "..." if len(str(row['description'])) > 300 else row['description'])

                    st.markdown(f"[🔗 View Article]({row['url']})")

                with col2:
                    # Status update
                    current_status = row['lead_status'] or 'new'
                    new_status = st.selectbox(
                        "Status",
                        LEAD_STATUSES,
                        index=LEAD_STATUSES.index(current_status),
                        key=f"status_{row['id']}"
                    )

                    notes = st.text_area(
                        "Notes",
                        value=row['notes'] or "",
                        key=f"notes_{row['id']}",
                        height=100
                    )

                    if st.button("Save", key=f"save_{row['id']}"):
                        update_lead_status(row['id'], new_status, notes)
                        st.success("Saved!")
                        st.rerun()

    with tab2:
        # Table view for quick scanning
        display_df = df[[
            'event_type', 'company_name', 'title', 'published_date',
            'relevance_score', 'lead_status', 'source'
        ]].copy()

        display_df.columns = [
            'Type', 'Company', 'Title', 'Published',
            'Relevance', 'Status', 'Source'
        ]

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
            type_counts = df['event_type'].value_counts()
            st.bar_chart(type_counts)

        with col2:
            st.subheader("Events by Status")
            status_counts = df['lead_status'].value_counts()
            st.bar_chart(status_counts)

        # Events over time
        st.subheader("Events Over Time")
        df['date'] = pd.to_datetime(df['published_date']).dt.date
        daily_counts = df.groupby('date').size()
        st.line_chart(daily_counts)

        # Top companies
        st.subheader("Top Companies")
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
        conn = get_connection()
        cursor = conn.cursor()

        for event_id in df['id'].tolist():
            cursor.execute(
                "UPDATE events SET lead_status = ? WHERE id = ?",
                (bulk_status, event_id)
            )

        conn.commit()
        conn.close()
        st.sidebar.success(f"Updated {len(df)} events!")
        st.rerun()


if __name__ == "__main__":
    main()
