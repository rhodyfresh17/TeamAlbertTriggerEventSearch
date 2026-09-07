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

v2 Phase 2 additions (2026-09-07) — typed columns arrive by a SQL migration
A.J. runs by hand, so this script PROBES for them and degrades gracefully:

  * events.source (scrape-owned enum text) is sent only when the live column
    exists; every row in a batch still carries the same key set.
  * source_status.items_fetched / filtered_out (raw candidates vs. gate drops,
    so "feed returned 0" and "all filtered" are distinguishable) are sent only
    when present.
  * Stale source_status rows (last_check older than STALE_STATUS_DAYS = 60)
    are skipped on push and reaped from Supabase at the end of each run
    (--no-reap to skip). Age is the ONLY criterion the reaper has — it
    cannot tell a retired feed from a configured one whose scraper stopped
    saving a status (review 2026-09-07) — so the window is wide, every
    reaped source_name is printed to the Actions log, and any source the
    CURRENT local SQLite reported within the window is protected whatever
    the Supabase row's age says.
  * Adzuna rows still labelled cfo_hire / executive_hire in the Actions
    SQLite cache are sent as finance_seat_open (see build_payload), so the
    4-hourly upsert stops reverting scripts/backfill_typed_columns.py.

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
    python supabase_sync.py --no-reap       # sync but leave stale source_status rows alone
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timedelta, timezone
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

# Scrape-owned column that is NOT yet in the live table (typed-column
# migration, v2 Phase 2 — A.J. runs the SQL by hand). SQLite always has it
# (events.source, an EventSource enum value); it is sent only after
# _present_columns() sees it live, so the payload never names a column
# PostgREST would reject. Scrape-owned like the rest — never listed in
# NEVER_SYNC_COLUMNS, but never assumed present either.
SOURCE_COLUMN = 'source'

# source_status counters from the same migration; same probe-then-send rule.
STATUS_COUNTER_COLUMNS = ('items_fetched', 'filtered_out')

# Stale source_status rows: a feed removed from config is never saved again,
# so its last_check freezes; after this many days it is dropped from the push
# and reaped from Supabase (see reap_stale_source_status). 60, not 30
# (review 2026-09-07): the reaper judges by age alone, and a still-configured
# feed whose scraper took a silent skip path also stops refreshing its row —
# two months of silence is an outage worth surfacing, one month can just be
# a quiet feed.
STALE_STATUS_DAYS = 60

# Adzuna job posts are an OPEN finance seat, not a hire. The scraper has
# emitted finance_seat_open since v2 Phase 1 and scripts/backfill_typed_columns.py
# relabels the old Supabase rows — but event_type is scrape-owned, and the
# GitHub Actions SQLite cache still holds pre-Phase-1 labels for rows inside
# the sync window, so every 4-hour upsert put cfo_hire / executive_hire back
# (review 2026-09-07). Relabelling here too makes the upsert agree with the
# backfill. Idempotent (finance_seat_open maps to itself); no other source
# is touched. Duplicated rather than imported from src/ on purpose: the
# Actions sync step installs only requests, PyYAML and supabase.
ADZUNA_SOURCE = 'adzuna'                      # EventSource.ADZUNA.value (src/models.py)
SEAT_RELABEL_FROM = ('cfo_hire', 'executive_hire')
SEAT_LABEL = 'finance_seat_open'

# Columns that exist in Supabase but are owned by reps (dashboard) or by
# enrichment_scout.py. Listed so the guard below can assert they never leak
# into a payload, whatever future refactors do to build_payload(). `source`
# is deliberately absent: it is scrape-owned (see SOURCE_COLUMN).
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
               url, published_date, discovered_date, matched_regions, source
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

    base_cols = 'source_name, source_type, last_check, status, error_message, events_found'
    try:
        cursor.execute(f'''
            SELECT {base_cols}, items_fetched, filtered_out
            FROM source_status
            ORDER BY source_type, source_name
        ''')
        statuses = [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        # Pre-Phase-2 SQLite (no counter columns) or no table at all. The
        # counters simply read as absent; the rest of the sync is unchanged.
        try:
            cursor.execute(f'''
                SELECT {base_cols}
                FROM source_status
                ORDER BY source_type, source_name
            ''')
            statuses = [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
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


def _event_type_for_sync(event: dict) -> str:
    """event_type to send: the SQLite value, except that an Adzuna row still
    carrying a hire label goes up as finance_seat_open (SEAT_RELABEL_FROM —
    legacy cache rows; the current scraper already emits the seat label).
    Keyed on the SQLite `source` enum, which every row has, not on the URL."""
    etype = event.get('event_type') or ''
    source = (event.get(SOURCE_COLUMN) or '').strip().lower()
    if source == ADZUNA_SOURCE and etype in SEAT_RELABEL_FROM:
        return SEAT_LABEL
    return etype


def build_payload(event: dict, include_source: bool = False) -> dict:
    """Map one SQLite row → the Supabase upsert payload.

    Pure function, no I/O. Emits EXACTLY the SCRAPE_OWNED_COLUMNS keys (plus
    `source` when include_source) — bulk upserts through PostgREST require
    every row to carry the same key set, and the guard at the end makes a
    rep/enrichment column leak impossible. The caller decides include_source
    ONCE per run from _present_columns(), so a batch is never mixed.
    event_type passes through _event_type_for_sync (the Adzuna relabel)."""
    payload = {
        'id':              str(event['id']),
        'title':           event.get('title') or '',
        'company_name':    event.get('company_name') or '',
        'event_type':      _event_type_for_sync(event),
        'description':     (event.get('description') or '')[:DESCRIPTION_MAX_CHARS],
        'source_url':      event.get('url') or '',
        'published_date':  event.get('published_date') or '',
        'discovered_at':   event.get('discovered_date') or datetime.now().isoformat(),
        'matched_regions': _matched_regions_json(event.get('matched_regions')),
    }
    if include_source:
        payload[SOURCE_COLUMN] = event.get(SOURCE_COLUMN) or None
    expected = SCRAPE_OWNED_COLUMNS + ((SOURCE_COLUMN,) if include_source else ())
    leaked = set(payload) & NEVER_SYNC_COLUMNS
    assert not leaked, f'sync payload must never carry rep/enrichment columns: {sorted(leaked)}'
    assert tuple(payload) == expected, 'payload keys drifted from SCRAPE_OWNED_COLUMNS'
    return payload


def _present_columns(client, table: str, cols: Iterable[str]) -> set:
    """Which of `cols` exist on the live `table`. Probed with a
    select(col).limit(1) each; ANY exception (PostgREST 42703 "column does
    not exist", network, auth) counts as absent, so a probe can only ever
    make the sync send LESS. Self-contained on purpose: the GitHub Actions
    sync step installs only `supabase`, never src.pipeline."""
    present = set()
    for col in cols:
        try:
            client.table(table).select(col).limit(1).execute()
            present.add(col)
        except Exception:
            pass
    return present


def chunked(items: List, size: int) -> Iterable[List]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def sync_events(client, events: List[dict], batch_size: int = BATCH_SIZE,
                include_source: bool = False) -> int:
    """Upsert events in batches. Returns the number of rows accepted.
    A failed batch is retried row-by-row so one bad row cannot sink 199 good ones.
    `include_source` is decided once by the caller (see _present_columns)."""
    synced = 0
    payloads = [build_payload(e, include_source=include_source) for e in events]
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


def sync_source_statuses(client, statuses: List[dict],
                         counter_columns: Iterable[str] = ()) -> int:
    """Upsert source_status rows (per-row, keyed on source_name).

    `counter_columns` = the subset of STATUS_COUNTER_COLUMNS the caller found
    live; those keys are added to every row (None when the local SQLite
    predates the counters). With none present the payload is unchanged."""
    counters = tuple(counter_columns)
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
            for col in counters:
                data[col] = status.get(col)
            client.table('source_status').upsert(data, on_conflict='source_name').execute()
            synced += 1
        except Exception as e:
            print(f"Error syncing source status {status.get('source_name')}: {e}")
    return synced


def _stale_cutoff(days: int = STALE_STATUS_DAYS, now: Optional[datetime] = None) -> str:
    """ISO cutoff for stale source_status rows. Naive UTC, because that is
    the format src/database.py writes into last_check (datetime.now() on the
    UTC Actions runner) and what the live column holds — the comparison is
    a plain string/timestamp `<` either way."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - timedelta(days=days)).isoformat()


def split_stale_statuses(statuses: List[dict], cutoff: str):
    """(fresh, stale) by last_check < cutoff. A row with no last_check is
    treated as fresh — better one odd row on the dashboard than a lost one."""
    fresh, stale = [], []
    for st in statuses:
        last = st.get('last_check') or ''
        (stale if last and last < cutoff else fresh).append(st)
    return fresh, stale


def reap_stale_source_status(client, days: int = STALE_STATUS_DAYS,
                             dry_run: bool = False,
                             now: Optional[datetime] = None,
                             protect: Iterable[str] = ()) -> int:
    """Delete Supabase source_status rows whose last_check is older than
    `days`, except any whose source_name is in `protect`. Returns the number
    of rows deleted (or, in dry_run, the number that WOULD be).

    WHY: a feed removed from config never gets a new status, so its row
    freezes at its last check and sits in the dashboard's Source Health
    table + monitor_health's silent-source counts forever (25 of 66 live
    rows were >100 days old on 2026-09-07).

    WHAT IT CANNOT KNOW (review 2026-09-07): age is the only criterion. A
    feed still in config whose scraper took a silent skip path (never saved
    a status) freezes its row exactly like a retired one, and this function
    reaps it too. Three mitigations, none a guarantee: STALE_STATUS_DAYS is
    60 (two months of silence is an outage worth noticing, not a quiet
    feed); every reaped source_name is printed, so the Actions log shows
    exactly what went; and `protect` — the names the CURRENT local SQLite
    reported within the window — is never reaped whatever the Supabase
    row's age (an upsert that failed this run would otherwise leave a live
    feed's old row to the reaper).

    This is the ONLY delete in the file, and it is safe because
    source_status holds nothing a rep or enrichment wrote — no events, no
    lead_status, no notes — just per-source health that the very next
    successful save of a still-configured source re-creates by upsert.

    Mechanics: select the candidates first (the log and the protect check
    need names), then delete BY NAME with the age filter re-applied, so a
    row refreshed between the two calls survives."""
    cutoff = _stale_cutoff(days, now)
    protected = {str(n) for n in protect}
    try:
        resp = (client.table('source_status').select('source_name,last_check')
                .lt('last_check', cutoff).execute())
        rows = resp.data or []
        victims = [r for r in rows if str(r.get('source_name')) not in protected]
        kept = [r for r in rows if str(r.get('source_name')) in protected]
        label = 'DRY RUN reap' if dry_run else 'Reap'
        print(f"{label}: {len(victims)} stale source_status row(s) with last_check < {cutoff} "
              f"({days}d)" + (f", {len(kept)} protected" if kept else ''))
        for row in kept:
            print(f"  keeping {row.get('source_name')} (last_check {row.get('last_check')}) "
                  f"— the local scraper reported it this window")
        for row in victims:
            print(f"  {'would delete' if dry_run else 'deleting'} {row.get('source_name')} "
                  f"(last_check {row.get('last_check')})")
        if dry_run or not victims:
            return len(victims)
        names = [str(r.get('source_name')) for r in victims]
        resp = (client.table('source_status').delete()
                .in_('source_name', names).lt('last_check', cutoff).execute())
        return len(resp.data or [])
    except Exception as e:
        print(f"Error reaping stale source_status rows: {e}")
        return 0


def print_dry_run(events: List[dict], window: int, stale: List[dict] = (),
                  now: Optional[datetime] = None) -> None:
    """Describe what a real run WOULD send. Touches no network."""
    scope = f'last {window} days' if window > 0 else 'ALL rows (no window)'
    print("DRY RUN — no network calls made")
    print(f"Window:         {scope}")
    print(f"Rows to upsert: {len(events)}  (batches of {BATCH_SIZE}, on_conflict='id')")
    print(f"Columns sent:   {', '.join(SCRAPE_OWNED_COLUMNS)}")
    print(f"  + if live:    {SOURCE_COLUMN} (events); "
          f"{', '.join(STATUS_COUNTER_COLUMNS)} (source_status) — probed per run")
    print(f"Never sent:     {', '.join(sorted(NEVER_SYNC_COLUMNS))}")
    if events:
        print("Sample payload (first row):")
        print(json.dumps(build_payload(events[0]), indent=2))
    else:
        print("Sample payload:  (no rows in window — nothing would be sent)")
    cutoff = _stale_cutoff(now=now)
    print(f"Stale reap:     source_status rows with last_check < {cutoff} "
          f"({STALE_STATUS_DAYS}d) are skipped on push and deleted from Supabase "
          f"(--no-reap keeps them; names the local scraper reported this window are never reaped)")
    if stale:
        print(f"  {len(stale)} local SQLite status row(s) are already stale and would not be pushed:")
        for st in stale:
            print(f"    {st.get('source_name')} (last_check {st.get('last_check')})")


def sync_to_supabase(db_path: str = 'trigger_events.db',
                     days: Optional[int] = None,
                     dry_run: bool = False,
                     client=None,
                     reap: bool = True,
                     now: Optional[datetime] = None) -> bool:
    """Sync recent scraped events + source statuses to Supabase, then reap
    stale source_status rows (unless reap=False / --no-reap).

    Run by the GitHub Actions sync step; the CLI passes --days / --dry-run /
    --no-reap. `client` is injectable for tests, and so is `now` (naive UTC;
    the stale cutoff and the reaper share it, so a test can pin the clock
    instead of racing datetime.now — review 2026-09-07). A dry run makes no
    network calls unless a client was injected, in which case it also lists
    what the reaper would delete."""
    window = _window_days(days)
    events = get_events_from_db(db_path, days=window)
    statuses = get_source_statuses_from_db(db_path)
    stale_cutoff = _stale_cutoff(now=now)
    fresh_statuses, stale_statuses = split_stale_statuses(statuses, stale_cutoff)
    # Names the local scraper reported within the window are never reaped,
    # whatever the Supabase row's age says (review 2026-09-07).
    protect = [s['source_name'] for s in fresh_statuses if s.get('source_name')]

    if dry_run:
        print_dry_run(events, window, stale_statuses, now=now)
        if client is not None and reap:
            reap_stale_source_status(client, dry_run=True, now=now, protect=protect)
        return True

    if client is None:
        if not SUPABASE_AVAILABLE:
            print("Supabase not installed. Run: pip install supabase")
            return False
        client = get_supabase_client()

    # One probe per run decides the optional typed columns for EVERY row, so
    # a batch never mixes key sets (PostgREST rejects that).
    include_source = SOURCE_COLUMN in _present_columns(client, 'events', (SOURCE_COLUMN,))
    counter_columns = [c for c in STATUS_COUNTER_COLUMNS
                       if c in _present_columns(client, 'source_status', STATUS_COUNTER_COLUMNS)]
    print(f"Live typed columns: events.source={'yes' if include_source else 'no'}; "
          f"source_status counters={', '.join(counter_columns) or 'none'}")

    if not events:
        print(f"No events discovered in the last {window} days — nothing to upsert")
    else:
        synced = sync_events(client, events, include_source=include_source)
        print(f"Synced {synced}/{len(events)} events (last {window} days) to Supabase")

    if stale_statuses:
        # Never re-push a retired feed's frozen status — the reaper below
        # would only have to delete it again.
        print(f"Skipped {len(stale_statuses)} stale local source status row(s) "
              f"(last_check < {stale_cutoff})")
    if fresh_statuses:
        synced_statuses = sync_source_statuses(client, fresh_statuses, counter_columns)
        print(f"Synced {synced_statuses}/{len(fresh_statuses)} source statuses to Supabase")

    if reap:
        reaped = reap_stale_source_status(client, now=now, protect=protect)
        print(f"Reaped {reaped} stale source_status row(s) from Supabase "
              f"(last_check < {stale_cutoff})")

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
    p.add_argument('--no-reap', action='store_true',
                   help=f'Do not delete source_status rows with last_check older than '
                        f'{STALE_STATUS_DAYS} days')
    args = p.parse_args(argv)
    ok = sync_to_supabase(db_path=args.db, days=args.days, dry_run=args.dry_run,
                          reap=not args.no_reap)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
