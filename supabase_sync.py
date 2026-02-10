#!/usr/bin/env python3
"""
Supabase sync for trigger events.
Pushes events to Supabase PostgreSQL for online dashboard access.
"""

import os
import json
import sqlite3
from datetime import datetime

try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False


def get_supabase_client() -> Client:
    """Initialize Supabase client."""
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_KEY')

    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables required")

    return create_client(url, key)


def get_events_from_db(db_path='trigger_events.db'):
    """Get events from local SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, title, company_name, event_type, description,
               source_url, published_date, discovered_at, lead_status, notes
        FROM events
        ORDER BY discovered_at DESC
    ''')

    events = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return events


def sync_to_supabase():
    """Sync all events to Supabase."""
    if not SUPABASE_AVAILABLE:
        print("Supabase not installed. Run: pip install supabase")
        return False

    client = get_supabase_client()
    events = get_events_from_db()

    if not events:
        print("No events to sync")
        return True

    # Upsert events (insert or update based on id)
    synced = 0
    for event in events:
        try:
            # Clean up data for Supabase
            data = {
                'id': str(event['id']),
                'title': event.get('title', ''),
                'company_name': event.get('company_name', ''),
                'event_type': event.get('event_type', ''),
                'description': (event.get('description', '') or '')[:2000],
                'source_url': event.get('source_url', ''),
                'published_date': event.get('published_date', ''),
                'discovered_at': event.get('discovered_at', datetime.now().isoformat()),
                'lead_status': event.get('lead_status', ''),
                'notes': event.get('notes', '')
            }

            client.table('events').upsert(data, on_conflict='id').execute()
            synced += 1
        except Exception as e:
            print(f"Error syncing event {event.get('id')}: {e}")

    print(f"Synced {synced}/{len(events)} events to Supabase")
    return True


if __name__ == '__main__':
    sync_to_supabase()
