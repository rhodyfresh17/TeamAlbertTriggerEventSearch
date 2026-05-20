#!/usr/bin/env python3
"""
Supabase sync for trigger events.
Pushes events to Supabase PostgreSQL for online dashboard access.
"""

import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path

# Load .env for local runs (no-op if file absent or dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass

try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False


def get_supabase_client():
    """Initialize Supabase client with service role key for full write access."""
    url = os.environ.get('SUPABASE_URL')
    # Use service role key for sync (bypasses RLS), fall back to SUPABASE_KEY
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ.get('SUPABASE_KEY')

    if not url or not key:
        raise ValueError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) "
            "environment variables required"
        )

    return create_client(url, key)


def get_events_from_db(db_path='trigger_events.db'):
    """Get events from local SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, title, company_name, event_type, description,
               url, published_date, discovered_date, lead_status, notes
        FROM events
        ORDER BY discovered_date DESC
    ''')

    events = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return events


def get_source_statuses_from_db(db_path='trigger_events.db'):
    """Get source statuses from local SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute('''
            SELECT source_name, source_type, last_check, status, error_message, events_found
            FROM source_status
            ORDER BY source_type, source_name
        ''')
        statuses = [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        statuses = []

    conn.close()
    return statuses


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
            # Normalize lead_status (SQLite uses lowercase 'new', dashboard expects 'NEW')
            lead_status = event.get('lead_status', '') or ''
            if lead_status.lower() == 'new' or lead_status == '':
                lead_status = 'NEW'

            # Clean up data for Supabase
            matched_regions = event.get('matched_regions', [])
            if isinstance(matched_regions, str):
                try:
                    matched_regions = json.loads(matched_regions)
                except Exception:
                    matched_regions = []

            data = {
                'id': str(event['id']),
                'title': event.get('title', ''),
                'company_name': event.get('company_name', ''),
                'event_type': event.get('event_type', ''),
                'description': (event.get('description', '') or '')[:2000],
                'source_url': event.get('url', ''),
                'published_date': event.get('published_date', ''),
                'discovered_at': event.get('discovered_date', datetime.now().isoformat()),
                'lead_status': lead_status,
                'notes': event.get('notes', ''),
                'matched_regions': json.dumps(matched_regions)
            }

            client.table('events').upsert(data, on_conflict='id').execute()
            synced += 1
        except Exception as e:
            print(f"Error syncing event {event.get('id')}: {e}")

    print(f"Synced {synced}/{len(events)} events to Supabase")

    # Sync source statuses
    statuses = get_source_statuses_from_db()
    if statuses:
        synced_statuses = 0
        for status in statuses:
            try:
                data = {
                    'source_name': status['source_name'],
                    'source_type': status['source_type'],
                    'last_check': status['last_check'],
                    'status': status['status'],
                    'error_message': status.get('error_message'),
                    'events_found': status.get('events_found', 0)
                }
                client.table('source_status').upsert(data, on_conflict='source_name').execute()
                synced_statuses += 1
            except Exception as e:
                print(f"Error syncing source status {status.get('source_name')}: {e}")
        print(f"Synced {synced_statuses}/{len(statuses)} source statuses to Supabase")

    return True


if __name__ == '__main__':
    sync_to_supabase()
