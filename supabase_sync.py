#!/usr/bin/env python3
"""
Supabase sync for trigger events — SQLite (scraper, GitHub Actions) → Supabase (dashboard).

DESIGN (v2 Phase 1 "Sync", 2026-09-06) — the sync owns ONLY scrape-time columns.

  * It sends an explicit column list (SCRAPE_OWNED_COLUMNS) and nothing else.
  * It never reads Supabase state and never sends any column a rep owns
    (lead_status, notes) or enrichment owns (grade, hashtags, fit,
    companies_data, enriched_at, blocked_*). PostgreSQL upsert semantics do the
    preserving: a column absent from the payload is left untouched on conflict.
  * Only rows discovered in the last N days are pushed (default 14, --days /
    SUPABASE_SYNC_WINDOW_DAYS). Older rows are already in Supabase and are
    owned by reps + enrichment from then on.
  * Rows go up in batches of 200 with on_conflict='id'.

WHY: the previous version PREFETCHED every Supabase row to decide whether to
send lead_status='NEW'. PostgREST caps an unfiltered select at 1,000 rows, so
every row past the first 1,000 looked "missing" and had its lead_status reset
to NEW every 4-hour cycle — silently wiping rep work (0 lead_status changes
survived since Aug 1; 20 non-NEW rows of 2,774 remained). Paginating would have
fixed the symptom; not sending the column fixes the class of bug.

New rows get lead_status from the Supabase column DEFAULT ('NEW' — verified via
the PostgREST OpenAPI on 2026-09-06) and the dashboard also fillna('NEW')s.

Usage:
    python supabase_sync.py                 # sync last 14 days
    python supabase_sync.py --days 30       # wider window (e.g. after a cache miss)
    python supabase_sync.py --dry-run       # print columns + row count + one sample; NO network
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

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


# ── Column ownership ─────────────────────────────────────────────────────────
# The ONLY columns this script may write. Every one of them is set by the
# scraper at discovery time and exists in the Supabase `events` table
# (verified against the live schema 2026-09-06). Add a column here only if it
# is (a) scrape-time data and (b) already present in Supabase.
SCRAPE_OWNED_COLUMNS = (
    'id',
    'title',
    'company_name',
    'event_type',
    'description',
    'source_url',        # ← SQLite `url`
    'published_date',
    'discovered_at',     # ← SQLite `discovered_date`
    'matched_regions',   # JSON-encoded list (Supabase column is text)
)

# Columns that exist in Supabase but are owned by reps (dashboard) or by
# enrichment_scout.py. Listed so the guard below can assert they never leak
# into a payload, whatever future refactors do to build_payload().
NEVER_SYNC_COLUMNS = frozenset({
    'lead_status', 'notes',                       # rep-owned (dashboard)
    'grade', 'hashtags', 'fit', 'companies_data',  # enrichment-owned
    'enriched_at', 'blocked_at', 'blocked_reason',
    'grade_justification', 'cfo_status', 'research_notes',
    'numeric_score', 'confidence_level',
})

DEFAULT_WINDOW_DAYS = 14
BATCH_SIZE = 200
DESCRIPTION_MAX_CHARS = 2000


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


def _window_days(days: Optional[int]) -> int:
    """Resolve the sync window: explicit arg → env → default. <=0 means no window."""
    if days is not None:
        return int(days)
    raw = os.environ.get('SUPABASE_SYNC_WINDOW_DAYS', '')
    try:
        return int(raw) if raw.strip() else DEFAULT_WINDOW_DAYS
    except ValueError:
        return DEFAULT_WINDOW_DAYS


def get_events_from_db(db_path: str = 'trigger_events.db',
                       days: Optional[int] = None) -> List[dict]:
    """Get scrape-owned event columns from the local SQLite database,
    restricted to rows discovered in the last `days` days (<=0 = all rows).

    `discovered_date` is stored by src/database.py as datetime.isoformat()
    (naive, scraper-local clock — UTC on GitHub Actions), so the cutoff is
    computed the same way and compared as an ISO string.
    """
    window = _window_days(days)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    sql = '''
        SELECT id, title, company_name, event_type, description,
               url, published_date, discovered_date, matched_regions
        FROM events
    '''
    params: tuple = ()
    if window > 0:
        cutoff = (datetime.now() - timedelta(days=window)).isoformat()
        sql += ' WHERE discovered_date >= ?'
        params = (cutoff,)
    sql += ' ORDER BY discovered_date DESC'

    cursor.execute(sql, params)
    events = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return events


def get_source_statuses_from_db(db_path: str = 'trigger_events.db') -> List[dict]:
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


def _matched_regions_json(value) -> str:
    """Normalise SQLite's matched_regions (JSON text, list, or empty) to a
    JSON-encoded list string. Anything unparseable becomes '[]'."""
    regions = value
    if isinstance(regions, str):
        try:
            regions = json.loads(regions) if regions.strip() else []
        except (ValueError, TypeError):
            regions = []
    if regions is None:
        regions = []
    if not isinstance(regions, list):
        regions = [regions]
    return json.dumps([str(r) for r in regions])


def build_payload(event: dict) -> dict:
    """Map one SQLite row → the Supabase upsert payload.

    Pure function, no I/O. Emits EXACTLY the SCRAPE_OWNED_COLUMNS keys — bulk
    upserts through PostgREST require every row to carry the same key set, and
    the guard at the end makes a rep/enrichment column leak impossible."""
    payload = {
        'id':              str(event['id']),
        'title':           event.get('title') or '',
        'company_name':    event.get('company_name') or '',
        'event_type':      event.get('event_type') or '',
        'description':     (event.get('description') or '')[:DESCRIPTION_MAX_CHARS],
        'source_url':      event.get('url') or '',
        'published_date':  event.get('published_date') or '',
        'discovered_at':   event.get('discovered_date') or datetime.now().isoformat(),
        'matched_regions': _matched_regions_json(event.get('matched_regions')),
    }
    leaked = set(payload) & NEVER_SYNC_COLUMNS
    assert not leaked, f'sync payload must never carry rep/enrichment columns: {sorted(leaked)}'
    assert tuple(payload) == SCRAPE_OWNED_COLUMNS, 'payload keys drifted from SCRAPE_OWNED_COLUMNS'
    return payload


def chunked(items: List, size: int) -> Iterable[List]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def sync_events(client, events: List[dict], batch_size: int = BATCH_SIZE) -> int:
    """Upsert events in batches. Returns the number of rows accepted.
    A failed batch is retried row-by-row so one bad row cannot sink 199 good ones."""
    synced = 0
    payloads = [build_payload(e) for e in events]
    for n, batch in enumerate(chunked(payloads, batch_size), start=1):
        try:
            client.table('events').upsert(batch, on_conflict='id').execute()
            synced += len(batch)
        except Exception as e:
            print(f"Batch {n} ({len(batch)} rows) failed: {e} — retrying rows individually")
            for row in batch:
                try:
                    client.table('events').upsert(row, on_conflict='id').execute()
                    synced += 1
                except Exception as e_row:
                    print(f"Error syncing event {row['id']}: {e_row}")
    return synced


def sync_source_statuses(client, statuses: List[dict]) -> int:
    """Upsert source_status rows (unchanged behaviour — per-row, keyed on source_name)."""
    synced = 0
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
            synced += 1
        except Exception as e:
            print(f"Error syncing source status {status.get('source_name')}: {e}")
    return synced


def print_dry_run(events: List[dict], window: int) -> None:
    """Describe what a real run WOULD send. Touches no network."""
    scope = f'last {window} days' if window > 0 else 'ALL rows (no window)'
    print("DRY RUN — no network calls made")
    print(f"Window:         {scope}")
    print(f"Rows to upsert: {len(events)}  (batches of {BATCH_SIZE}, on_conflict='id')")
    print(f"Columns sent:   {', '.join(SCRAPE_OWNED_COLUMNS)}")
    print(f"Never sent:     {', '.join(sorted(NEVER_SYNC_COLUMNS))}")
    if events:
        print("Sample payload (first row):")
        print(json.dumps(build_payload(events[0]), indent=2))
    else:
        print("Sample payload:  (no rows in window — nothing would be sent)")


def sync_to_supabase(db_path: str = 'trigger_events.db',
                     days: Optional[int] = None,
                     dry_run: bool = False,
                     client=None) -> bool:
    """Sync recent scraped events + source statuses to Supabase.

    Called with no arguments by src/main.py after each scrape; the CLI passes
    --days / --dry-run. `client` is injectable for tests."""
    window = _window_days(days)
    events = get_events_from_db(db_path, days=window)

    if dry_run:
        print_dry_run(events, window)
        return True

    if client is None:
        if not SUPABASE_AVAILABLE:
            print("Supabase not installed. Run: pip install supabase")
            return False
        client = get_supabase_client()

    if not events:
        print(f"No events discovered in the last {window} days — nothing to upsert")
    else:
        synced = sync_events(client, events)
        print(f"Synced {synced}/{len(events)} events (last {window} days) to Supabase")

    statuses = get_source_statuses_from_db(db_path)
    if statuses:
        synced_statuses = sync_source_statuses(client, statuses)
        print(f"Synced {synced_statuses}/{len(statuses)} source statuses to Supabase")

    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--days', type=int, default=None,
                   help=f'Only sync rows discovered in the last N days '
                        f'(default {DEFAULT_WINDOW_DAYS}, env SUPABASE_SYNC_WINDOW_DAYS; 0 = all rows)')
    p.add_argument('--db', default='trigger_events.db', help='Path to the SQLite database')
    p.add_argument('--dry-run', action='store_true',
                   help='Print the column list, row count and one sample payload; no network')
    args = p.parse_args(argv)
    ok = sync_to_supabase(db_path=args.db, days=args.days, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
