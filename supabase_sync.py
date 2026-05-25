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
    # Pre-fetch existing event IDs + their user-set fields from Supabase so
    # we can preserve user state (lead_status / notes / grading) on upsert.
    # WITHOUT this, every 4-hour scrape cycle silently wipes the user's
    # "REVIEWED — NetSuite Customer" markers + notes back to default NEW.
    existing_state: dict = {}
    try:
        existing = client.table('events').select(
            'id, lead_status, notes, grade, hashtags, grade_justification, '
            'cfo_status, research_notes, companies_data, enriched_at'
        ).execute()
        for row in (existing.data or []):
            existing_state[str(row['id'])] = row
    except Exception as e:
        # Some columns may not exist yet (pre-grading deployments); fall back
        # to just lead_status + notes preservation
        try:
            existing = client.table('events').select('id, lead_status, notes').execute()
            for row in (existing.data or []):
                existing_state[str(row['id'])] = row
        except Exception:
            print(f"Warning: could not fetch existing event state ({e}); "
                  f"sync will preserve nothing — user lead_status/notes may be wiped")

    synced = 0
    for event in events:
        try:
            event_id = str(event['id'])
            existing = existing_state.get(event_id) or {}

            # Clean up matched_regions
            matched_regions = event.get('matched_regions', [])
            if isinstance(matched_regions, str):
                try:
                    matched_regions = json.loads(matched_regions)
                except Exception:
                    matched_regions = []

            # Build payload from local SQLite (the source of truth for SCRAPED data)
            data = {
                'id': event_id,
                'title': event.get('title', ''),
                'company_name': event.get('company_name', ''),
                'event_type': event.get('event_type', ''),
                'description': (event.get('description', '') or '')[:2000],
                'source_url': event.get('url', ''),
                'published_date': event.get('published_date', ''),
                'discovered_at': event.get('discovered_date', datetime.now().isoformat()),
                'matched_regions': json.dumps(matched_regions)
            }

            # PRESERVE user-set lead_status. SQLite always carries the default
            # 'new' — only overwrite Supabase if user hasn't touched it.
            existing_status = (existing.get('lead_status') or '').strip()
            if not existing_status or existing_status.lower() == 'new':
                # First sync (no existing row) OR user hasn't classified yet
                # → write NEW so dashboard renders correctly
                data['lead_status'] = 'NEW'
            # else: skip the field entirely so upsert preserves user's "REVIEWED",
            # "NOT RELEVANT", etc.

            # PRESERVE user notes — SQLite typically has no notes; only write
            # if Supabase doesn't already have notes from dashboard edits.
            existing_notes = (existing.get('notes') or '').strip()
            if not existing_notes:
                data['notes'] = event.get('notes', '') or ''

            # PRESERVE Supabase-only enrichment + grading fields. These are
            # written by enrichment_scout.py (which writes directly to
            # Supabase) and don't exist in SQLite. If we upsert without
            # them, we'd wipe all firmographics + grades every scrape.
            # Solution: only include them if they're already present (no-op)
            # — do NOT write them from SQLite (which has no values).
            # We simply don't include these keys in data, so upsert preserves
            # the existing JSONB/grade values via PostgreSQL semantics.

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
