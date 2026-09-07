#!/usr/bin/env python3
"""Backfill the v2 typed columns from the JSON already on each event
(Phase 2, 2026-09-07). Run AFTER A.J. has applied
supabase/migrations/002_v2_typed_columns.sql — the SQL fills what SQL can
(verdict, states, source, expiry); this script fills the Python-only values:

  account_key · hq_state · revenue_segment · sic / formd_* · plus the same
  fit-derived columns for any row the SQL left NULL

and relabels Adzuna job posts (an OPEN finance seat, not a hire) from
cfo_hire / executive_hire to finance_seat_open — those rows predate Phase 1's
label and the dashboard's Open Seat tab keys off it.

Dry-run by default: prints before/after counts and the number of rows it
WOULD update. `--apply` writes, one update per row, progress every 100.
Never overwrites a non-NULL typed value; never touches lead_status / notes /
grade / blocked_*; enrich_attempts stays at its default 0.
Zero paid search — no Tavily, no Firecrawl, no LLM.
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.pipeline.typed import (  # noqa: E402
    TYPED_EVENT_COLUMNS, is_adzuna, probe_columns, typed_payload, verify_state_for,
)

MIGRATION_SQL = 'supabase/migrations/002_v2_typed_columns.sql'
BASE_COLS = ('id', 'title', 'company_name', 'event_type', 'description', 'source_url',
             'published_date', 'discovered_at', 'fit', 'companies_data', 'blocked_at')
# Adzuna rows scraped before Phase 1 carry the hire labels; the seat is open,
# nobody was hired (A.J. 2026-09-06). supabase_sync.build_payload applies the
# SAME relabel on every push (review 2026-09-07): event_type is scrape-owned
# and the Actions SQLite cache still holds the old labels inside the sync
# window, so without it the 4-hourly upsert undid this backfill. Keep the
# two in step (the sync cannot import src/, hence the duplicate constants).
SEAT_RELABEL_FROM = ('cfo_hire', 'executive_hire')
SEAT_LABEL = 'finance_seat_open'


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def chosen_account(fit: dict, companies_data, company_name) -> dict:
    """The company dict enrichment picked as the account: fit.account_name
    by name, else the event's company_name, else the first company."""
    cd = [c for c in (companies_data or []) if isinstance(c, dict)]
    if not cd:
        return {}
    for want in ((fit or {}).get('account_name'), company_name):
        if want:
            hit = next((c for c in cd if (c.get('name') or '').strip() == str(want).strip()), None)
            if hit:
                return hit
    return cd[0]


def _structured(row: dict, structured_fn):
    if structured_fn is None or 'sec.gov' not in (row.get('source_url') or ''):
        return None
    try:
        return structured_fn(row)
    except Exception:
        return None


def row_payload(row: dict, present, now=None, structured_fn=None) -> dict:
    """Pure: the update for ONE row — typed values that are currently NULL,
    plus the Adzuna relabel. {} when nothing needs writing."""
    fit = _j(row.get('fit')) or {}
    if not isinstance(fit, dict):
        fit = {}
    cd = _j(row.get('companies_data')) or []
    account = chosen_account(fit, cd, row.get('company_name'))
    verify_state = verify_state_for(fit.get('verdict'), blocked=bool(row.get('blocked_at')))
    full = typed_payload(event=row, fit=fit, structured=_structured(row, structured_fn),
                         account=account, verify_state=verify_state, present=present, now=now)
    payload = {k: v for k, v in full.items() if v is not None and row.get(k) is None}
    if is_adzuna(row) and row.get('event_type') in SEAT_RELABEL_FROM:
        payload['event_type'] = SEAT_LABEL
    return payload


def fetch_rows(svc, present, since: str, batch: int):
    cols = ','.join(BASE_COLS + tuple(sorted(present)))
    rows, off = [], 0
    while True:
        b = (svc.table('events').select(cols).gte('discovered_at', since)
             .order('discovered_at', desc=False).range(off, off + batch - 1)
             .execute().data or [])
        rows += b
        if len(b) < batch:
            break
        off += batch
    return rows


def _counts(rows, payloads):
    """(verify_state Counter, event_type Counter) before and after."""
    before_vs, after_vs, before_et, after_et = Counter(), Counter(), Counter(), Counter()
    for r in rows:
        p = payloads.get(r['id'], {})
        before_vs[r.get('verify_state') or 'NULL'] += 1
        after_vs[p.get('verify_state') or r.get('verify_state') or 'NULL'] += 1
        before_et[r.get('event_type') or 'NULL'] += 1
        after_et[p.get('event_type') or r.get('event_type') or 'NULL'] += 1
    return before_vs, after_vs, before_et, after_et


def _print_table(title, before: Counter, after: Counter):
    print(f"\n{title}")
    print(f"  {'value':<24}{'before':>8}{'after':>8}")
    for k in sorted(set(before) | set(after), key=lambda x: (-after[x], x)):
        print(f"  {k:<24}{before[k]:>8}{after[k]:>8}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    ap.add_argument('--since', default='2026-06-01', help='discovered_at >= YYYY-MM-DD (default 2026-06-01)')
    ap.add_argument('--batch', type=int, default=100, help='page size for reads (default 100)')
    args = ap.parse_args(argv)

    import logging
    import warnings
    warnings.filterwarnings('ignore')
    logging.getLogger('httpx').setLevel(logging.WARNING)   # the column probe 400s are expected
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
    from supabase import create_client
    import enrichment_scout as es   # _structured_verdict; module import is side-effect-light

    svc = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
    present = probe_columns(svc, 'events', TYPED_EVENT_COLUMNS)
    missing = [c for c in TYPED_EVENT_COLUMNS if c not in present]
    if missing:
        print(f"REFUSING: the typed columns are not in Supabase yet "
              f"(missing: {', '.join(missing)}).\n"
              f"Paste {MIGRATION_SQL} into the Supabase SQL Editor and run it first, "
              f"then re-run this script.")
        return 2

    now = datetime.now(timezone.utc)
    rows = fetch_rows(svc, present, args.since, max(1, args.batch))
    payloads = {}
    for r in rows:
        p = row_payload(r, present, now=now, structured_fn=es._structured_verdict)
        if p:
            payloads[r['id']] = p
    print(f"events since {args.since}: {len(rows)}   (dry_run={not args.apply})")

    b_vs, a_vs, b_et, a_et = _counts(rows, payloads)
    _print_table('verify_state', b_vs, a_vs)
    _print_table('event_type', b_et, a_et)
    fills = Counter(k for p in payloads.values() for k in p)
    print("\ncolumns filled (rows):")
    for k, n in fills.most_common():
        print(f"  {n:>5}  {k}")
    print(f"\nrows to update: {len(payloads)} of {len(rows)}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0
    n = 0
    for eid, p in payloads.items():
        svc.table('events').update(p).eq('id', eid).execute()
        n += 1
        if n % 100 == 0:
            print(f"  ... {n}/{len(payloads)}")
    print(f"\nAPPLIED: {n} row updates")
    return 0


if __name__ == '__main__':
    sys.exit(main())
