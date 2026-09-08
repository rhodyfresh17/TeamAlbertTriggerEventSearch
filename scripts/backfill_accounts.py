#!/usr/bin/env python3
"""Build the accounts table from the events already in Supabase
(Phase 4 slice C1, 2026-09-08). Run AFTER A.J. has applied
supabase/migrations/003_accounts.sql — the script refuses until the table
exists, and it never touches events.

What it does, per account (= src.pipeline.gates.account_key):
  * pages EVERY event (tombstoned ones included — a tombstoned event still
    tells us the account exists and what it is) ordered by discovered_at;
  * groups them by account_key — the typed column, else the key of
    fit.account_name / company_name (rows that predate Phase 2);
  * builds the row from the NEWEST ENRICHED event's chosen company (the
    companies_data entry fit.account_name names) and merges the rest
    through accounts.merge_account: facts fill-only by provenance, ONE
    grade, ONE best trigger, counts and seen-dates;
  * grades and best triggers come ONLY from live events — not tombstoned,
    not past typed.EXPIRY_DAYS — so an account whose CFO hire expired in
    June is not surfaced as a graded lead today;
  * applies the reps' verdicts from the legacy account_dispositions table
    (mapped by company_name — the dashboard's key differs from
    gates.account_key for some names; the reason code decoded out of
    notes, accounts.decode_legacy_notes), creating a disposition-only row
    when the account has no event at all, starting from the EXISTING
    accounts row when it has no event in this run, and keeping a
    disposition already on the accounts row unless the legacy one is
    NEWER (the same rule as accounts.load_dispositions).

Dry-run by default: prints before/after tables (accounts by verify_state,
grade, disposition), the unmatched dispositions, and two checks — distinct
account keys over events vs rows it would write, and legacy dispositions
vs mapped. `--preflight` runs that same plan BEFORE the table exists (so
the numbers can be checked first; it can never write). `--apply` upserts
full rows in batches of 100 (progress every 100). Zero paid search — no
Tavily, no Firecrawl, no LLM.

Re-runs are safe (review 2026-09-08 (Phase 4)) because --apply:
  * reads the existing rows STRICTLY (accounts.page_rows) and refuses when
    that read fails — accounts.list_accounts returns [] on ANY failure,
    which made a re-run plan as if the table were empty and NULL every
    disposition, grade and fact learned since;
  * sends the rep-owned disposition columns only for rows that carry a
    disposition (full_row), so a row whose verdict this run never saw
    keeps it;
  * refuses --since: a window can never recount event_count (the window
    count was written as the total), and an account with no event in the
    window used to be rebuilt from a minimal row. --since is a dry-run
    preview only; its counts are existing + events the row had not seen;
  * holds the enrichment run lock (state/enrichment.lock — the flock
    enrichment_scout.py takes) for the whole plan + write, so a concurrent
    enrichment run cannot interleave its upserts with the batches.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.pipeline import accounts as A  # noqa: E402
from src.pipeline.gates import account_key, is_bad_company_name  # noqa: E402
from src.pipeline.runlock import RunLock  # noqa: E402
from src.pipeline.typed import expires_at_for, parse_ts, probe_columns  # noqa: E402
from scripts.backfill_typed_columns import _j, chosen_account  # noqa: E402

MIGRATION_SQL = 'supabase/migrations/003_accounts.sql'
BATCH_WRITE = 100
# The SAME lock file enrichment_scout._acquire_run_lock takes (flock; the
# kernel drops it if either side crashes) — review 2026-09-08 (Phase 4).
LOCK_PATH = os.path.join(_ROOT, 'state', 'enrichment.lock')
EVENT_COLS = ('id', 'title', 'company_name', 'event_type', 'published_date', 'discovered_at',
              'enriched_at', 'blocked_at', 'blocked_reason', 'fit', 'companies_data',
              'grade', 'numeric_score', 'confidence_level', 'hashtags', 'grade_justification')
# Typed columns (migration 002) the row builder reads when present. Probed,
# not assumed, so the script also runs against a project without them.
TYPED_COLS = ('account_key', 'fit_verdict', 'verify_state', 'hq_state', 'in_territory',
              'zi_subindustry', 'revenue_segment', 'expires_at', 'enrich_attempts',
              'retry_after', 'classified_by', 'classification_confidence')
# Tombstone reasons that are a verdict on the ACCOUNT (it can never be a
# NetSuite account), as opposed to on the event (expired, board change,
# rep-decided — the rep's verdict arrives via account_dispositions). A
# pre-search tombstone has no fit dict, so this is the only way to know.
ACCOUNT_OUT_REASONS = ('fit_gate', 'structured', 'industry', 'entity_shape', 'aj_exclusion',
                       'formd_too_small', 'excluded_public_company', 'public_school_district')
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ── Pure helpers (tests/test_accounts.py) ───────────────────────────────────
def event_name(ev: dict) -> str:
    fit = _j(ev.get('fit'))
    fit = fit if isinstance(fit, dict) else {}
    return str(fit.get('account_name') or ev.get('company_name') or '').strip()


def event_key(ev: dict) -> str:
    """The account an event belongs to: the typed column, else the key of
    fit.account_name / company_name. '' when the name is junk ('Unknown',
    a headline fragment) — no account is made from it."""
    name = event_name(ev)
    if not name or is_bad_company_name(name):
        return ''
    return str(ev.get('account_key') or '').strip() or account_key(name)


def is_live_event(ev: dict, now: datetime) -> bool:
    """Not tombstoned and not past its shelf life — the only events allowed
    to carry a grade or be an account's best trigger."""
    if ev.get('blocked_at'):
        return False
    exp = parse_ts(ev.get('expires_at')) or parse_ts(
        expires_at_for(ev.get('event_type'), ev.get('published_date'), ev.get('discovered_at')))
    return exp is None or exp >= now


def tombstone_verdict(blocked_reason) -> str:
    """'fail' when the tombstone reason condemns the account itself."""
    r = str(blocked_reason or '').strip().lower()
    return 'fail' if r and r.startswith(ACCOUNT_OUT_REASONS) else ''


def event_contribution(ev: dict, now: datetime):
    """accounts row ONE event contributes (build_account_row on its chosen
    company), or None when the event names no usable company."""
    if not event_key(ev):
        return None
    fit = _j(ev.get('fit'))
    fit = dict(fit) if isinstance(fit, dict) else {}
    cd = _j(ev.get('companies_data')) or []
    company = chosen_account(fit, cd, ev.get('company_name')) or {'name': event_name(ev)}
    if not str(fit.get('verdict') or '').strip() and tombstone_verdict(ev.get('blocked_reason')):
        fit['verdict'] = 'fail'
    try:
        # The stored grade IS this event's grade here (the row was written by
        # the run that graded it) — the one place grading_from_event is right.
        return A.build_account_row(ev, company, fit, grading=A.grading_from_event(ev), now=now,
                                   trigger_live=is_live_event(ev, now))
    except ValueError:
        return None


def group_events(events, now: datetime):
    """({account_key: [(event, contribution), ...] newest first}, Counter of
    skipped events, [diverging]) — `diverging` lists events whose own key
    (typed account_key / company_name) is not the key of the company the
    row is built on. Live, that cannot happen: fit.account_name IS the
    chosen company. It does for pre-v2 rows whose fit has no account_name
    and whose company_name is a headline fragment ('Businesses It
    Acquired', 'How CMS Energy') or a clipped name ('Aecon' vs 'Aecon
    Group'); the row follows the real company (the contract's
    account_key = key(canonical_name)), and the report names the events so
    their typed account_key can be re-derived on the events side."""
    groups, skipped, diverging = defaultdict(list), Counter(), []
    for ev in events:
        c = event_contribution(ev, now)
        if c is None:
            skipped['no usable company name'] += 1
            continue
        ek = event_key(ev)
        if ek != c['account_key']:
            diverging.append({'event_id': ev.get('id'), 'event_key': ek, 'row_key': c['account_key'],
                              'company_name': ev.get('company_name'),
                              'live': is_live_event(ev, now), 'tombstoned': bool(ev.get('blocked_at'))})
        groups[c['account_key']].append((ev, c))
    for items in groups.values():
        items.sort(key=lambda t: parse_ts(t[0].get('discovered_at')) or _EPOCH, reverse=True)
    return groups, skipped, diverging


def account_from_group(items, now: datetime) -> dict:
    """Start from the newest ENRICHED event (it has the chosen company with
    provenance), merge every other event in; the recount of events is the
    group's size, and the row remembers the newest SEEN_EVENT_IDS_MAX ids
    (oldest first) so a later re-process of a recent event is recognised
    and not counted again (review 2026-09-08 (Phase 4))."""
    enriched = [t for t in items if _j(t[0].get('companies_data'))]
    anchor = (enriched or items)[0]
    acc = dict(anchor[1])
    for ev, c in items:
        if ev is anchor[0]:
            continue
        acc.update(A.merge_account(acc, c, now=now, is_new_event=True))
    acc['event_count'] = len(items)
    ids = [str(ev.get('id') or '').strip() for ev, _ in reversed(items)]    # items are newest first
    acc['seen_event_ids'] = [i for i in ids if i][-A.SEEN_EVENT_IDS_MAX:]
    return acc


def plan_accounts(events, legacy_rows, existing_rows, now: datetime, since=None) -> dict:
    """Pure: everything the run would write, plus the report numbers.
    `since` (the --since window; dry-run only) switches event_count from a
    full recount to existing + the ids the row had not seen: a window
    cannot recount (review 2026-09-08 (Phase 4) — the window count used to
    be written as the total).
    Returns {'rows': {key: row}, 'skipped': Counter, 'groups': int,
             'usable_events': int, 'distinct_event_keys': int,
             'diverging': [...], 'mismatch': [keys], 'since': since,
             'dispositions': {...counts + 'unmatched': [entries],
                              'no_reason': int, 'rep_tombstones_without': int}}"""
    now_iso = now.isoformat()
    groups, skipped, diverging = group_events(events, now)
    rows = {k: account_from_group(items, now) for k, items in groups.items()}
    usable_events = sum(len(items) for items in groups.values())

    # Identity check: the accounts the events name (typed key, else derived
    # — an INDEPENDENT count) must be the rows built from them, once the
    # explained divergences (see group_events) are set aside on both sides.
    # Anything left over is an account the grouping lost or invented.
    distinct = {event_key(ev) for ev in events} - {''}
    explained_ev = {d['event_key'] for d in diverging}
    explained_row = {d['row_key'] for d in diverging}
    mismatch = sorted(((distinct - set(rows)) - explained_ev) | ((set(rows) - distinct) - explained_row))

    existing = {r['account_key']: r for r in (existing_rows or []) if r.get('account_key')}
    for key, ex in existing.items():
        if key not in rows:
            continue                                         # a row events no longer name: left as is
        built = rows[key]
        merged = dict(ex)
        merged.update(A.merge_account(ex, built, now=now, is_new_event=None))
        if since is None:
            # every event was read: a full recount (and the full id list)
            # beats existing + the ids the row had not seen
            merged['event_count'] = built['event_count']
            merged['seen_event_ids'] = built.get('seen_event_ids') or []
        rows[key] = merged

    # Reps' verdicts. A disposition already on the accounts row stays unless
    # the legacy row is NEWER — the rep's latest word after a 'Partly saved
    # — written to account_dispositions' receipt; the same rule as
    # accounts.load_dispositions (review 2026-09-08 (Phase 4)).
    d = Counter({'attached': 0, 'disposition_only': 0, 'from_existing': 0, 'kept_from_accounts': 0,
                 'superseded_accounts': 0, 'no_reason': 0})
    mapped = A.map_legacy_dispositions(legacy_rows)
    unmatched = [m for m in mapped if not m['account_key']]
    for m in sorted([m for m in mapped if m['account_key']], key=lambda m: str(m.get('at') or '')):
        key = m['account_key']
        ex = existing.get(key) or {}
        if ex.get('disposition'):
            if not A.is_newer(m.get('at'), ex.get('disposition_at')):
                d['kept_from_accounts'] += 1
                continue
            d['superseded_accounts'] += 1
        row = rows.get(key)
        if row is None:
            if key in existing:
                # No event named this account in this run, but its row
                # exists: start from THAT row — full_row sends every column,
                # and a minimal row would NULL the facts and grade the row
                # already holds (review 2026-09-08 (Phase 4)).
                row = dict(existing[key])
                row['updated_at'] = now_iso
                d['from_existing'] += 1
            else:
                row = {'account_key': key, 'canonical_name': (m['name'] or key)[:200],
                       'event_count': 0, 'active': True, 'updated_at': now_iso}
                d['disposition_only'] += 1
            rows[key] = row
        else:
            d['attached'] += 1
        row.update({'disposition': m['status'], 'disposition_reason': m['reason'],
                    'disposition_notes': m['notes'], 'disposition_at': m['at'] or now_iso,
                    'disposition_by': None})
        if m['status'] in A.REASON_REQUIRED_STATUSES and not m['reason']:
            d['no_reason'] += 1                              # nothing to decode: reported, not faked
    # Events enrichment tombstoned on a rep's word whose account now has no
    # disposition (the rep cleared it since): informational.
    rep_keys = {k for k, items in groups.items()
                if any(str(ev.get('blocked_reason') or '').startswith('rep:') for ev, _ in items)}
    d['rep_tombstones_without'] = sum(1 for k in rep_keys if not rows[k].get('disposition'))
    d['mapped'] = len(mapped) - len(unmatched)
    d['legacy_rows'] = len(mapped)
    return {'rows': rows, 'skipped': skipped, 'groups': len(groups), 'usable_events': usable_events,
            'distinct_event_keys': len(distinct), 'diverging': diverging, 'mismatch': mismatch,
            'since': since, 'dispositions': dict(d, unmatched=unmatched)}


def full_row(row: dict, present, now_iso: str) -> dict:
    """One uniform key set per batch: postgrest sends the UNION of keys for
    a list payload and NULLs the ones a row lacks, so every row in a batch
    must carry every column the batch sends (NOT NULL columns get their
    defaults). The rep-owned DISPOSITION_COLUMNS travel ONLY with a row
    that carries a disposition (its own, kept from the accounts row, or a
    legacy one applied in this run) — review 2026-09-08 (Phase 4): sent as
    None for every other row they NULLed reason / notes / by on rows whose
    verdict this run never saw. Rows therefore come in two key shapes and
    write_rows batches each shape separately."""
    out = {}
    with_dispo = bool(row.get('disposition'))
    for col in A.ACCOUNT_COLUMNS:
        if col == 'created_at' or col not in present:
            continue
        if col in A.DISPOSITION_COLUMNS and not with_dispo:
            continue
        v = row.get(col)
        if v is None:
            v = {'enrich_attempts': 0, 'event_count': 0, 'active': True, 'updated_at': now_iso}.get(col)
        out[col] = v
    return out


def write_rows(svc, rows, batch_size: int = BATCH_WRITE) -> int:
    """Upsert full_row dicts in batches of ONE key shape each (see
    full_row), progress printed per batch. Returns rows written."""
    shapes = {}
    for r in rows:
        shapes.setdefault(tuple(sorted(r)), []).append(r)
    n = 0
    for shape_rows in shapes.values():
        for i in range(0, len(shape_rows), batch_size):
            batch = shape_rows[i:i + batch_size]
            svc.table('accounts').upsert(batch, on_conflict='account_key').execute()
            n += len(batch)
            print(f"  ... {n}/{len(rows)}")
    return n


# ── I/O ─────────────────────────────────────────────────────────────────────
def fetch_events(svc, cols, since, batch: int):
    """Every event (or those discovered since `since`), paged. Ordered by
    discovered_at THEN id — review 2026-09-08 (Phase 4): with no tiebreaker
    two events discovered in the same second could swap across a page
    boundary, one read twice and the other never."""
    rows, off = [], 0
    while True:
        q = svc.table('events').select(','.join(cols))
        if since:
            q = q.gte('discovered_at', since)
        b = (q.order('discovered_at', desc=False).order('id', desc=False)
             .range(off, off + batch - 1).execute().data or [])
        rows += b
        if len(b) < batch:
            break
        off += batch
    return rows


def fetch_existing(svc, present, batch: int):
    """Every accounts row, STRICT — accounts.page_rows raises on failure.
    NOT accounts.list_accounts: that returns [] on ANY failure, which made a
    re-run plan as if the table were empty and, on --apply, NULL every
    disposition and drop every grade / fact learned since (review
    2026-09-08 (Phase 4)). The caller decides what a failure means."""
    cols = [c for c in A.ACCOUNT_COLUMNS if c in present] or ['account_key']
    return A.page_rows(svc, 'accounts', ','.join(cols), batch=batch)


def _counts(rows, col):
    return Counter(str(r.get(col) or 'NULL') for r in rows)


def _print_table(title, before: Counter, after: Counter):
    print(f"\n{title}")
    print(f"  {'value':<28}{'before':>8}{'after':>8}")
    for k in sorted(set(before) | set(after), key=lambda x: (-after[x], x)):
        print(f"  {k:<28}{before[k]:>8}{after[k]:>8}")


def print_report(plan: dict, existing_rows, events, since, dry_run: bool):
    rows = list(plan['rows'].values())
    tomb = sum(1 for e in events if e.get('blocked_at'))
    print(f"events scanned: {len(events)}  (tombstoned {tomb}; since={since or 'the beginning'}; dry_run={dry_run})")
    for what, n in plan['skipped'].items():
        print(f"  skipped {n} event(s): {what}")
    for col in ('verify_state', 'grade', 'disposition'):
        _print_table(f'accounts by {col}', _counts(existing_rows, col), _counts(rows, col))
    ev_rows = [r for r in rows if r.get('event_count')]
    counted = sum(r['event_count'] for r in ev_rows)
    if since:
        print(f"\nCHECK coverage: n/a for a --since window (dry-run preview): event_count = existing + "
              f"events the row had not seen; {plan['usable_events']} usable events in the window, "
              f"sum(event_count) = {counted}")
    else:
        print(f"\nCHECK coverage: {plan['usable_events']} events with a usable company name -> "
              f"{len(ev_rows)} account rows, sum(event_count) = {counted}   "
              f"{'MATCH' if counted == plan['usable_events'] else 'MISMATCH'}")
    div = plan['diverging']
    live_div = sum(1 for x in div if x['live'])
    print(f"CHECK identity: distinct account keys over events {plan['distinct_event_keys']} vs rows {len(ev_rows)} "
          f"-- {len(div)} legacy event(s) explained ({live_div} live), "
          f"{len(plan['mismatch'])} unexplained   {'MATCH' if not plan['mismatch'] else 'MISMATCH'}")
    if plan['mismatch']:
        print(f"  unexplained keys on one side only: {plan['mismatch'][:20]}")
    if div:
        print("  explained (events whose typed/derived key names a headline fragment or clipped name; "
              "the row follows the chosen company) — cross-file: re-derive events.account_key for these ids:")
        for x in div:
            print(f"    {str(x['event_id'])[:12]:<13} {x['event_key']!r:<48} -> {x['row_key']!r:<40} "
                  f"{'LIVE' if x['live'] else 'tombstoned' if x['tombstoned'] else 'expired'}")
    d = plan['dispositions']
    print(f"CHECK legacy dispositions: {d.get('legacy_rows', 0)} rows -> mapped {d.get('mapped', 0)} "
          f"(attached to event accounts {d.get('attached', 0)}, disposition-only rows {d.get('disposition_only', 0)}, "
          f"onto existing rows without events here {d.get('from_existing', 0)}, "
          f"kept accounts' own {d.get('kept_from_accounts', 0)}, newer than accounts' own {d.get('superseded_accounts', 0)})   "
          f"unmatched {len(d['unmatched'])}   {'ALL MAPPED' if not d['unmatched'] else 'UNMATCHED PRESENT'}")
    for m in d['unmatched']:
        print(f"  unmatched: company_key={m['legacy_key']!r} company_name={m['name']!r} — {m['problem']}")
    if d.get('no_reason'):
        print(f"  note: {d['no_reason']} Not-a-Fit / Out-of-Alignment row(s) carry no reason "
              f"(no 'reason=<code>' in their notes; left NULL, not invented)")
    if d.get('rep_tombstones_without'):
        print(f"  note: {d['rep_tombstones_without']} account(s) with rep:<status> tombstones have no disposition "
              f"(the rep cleared it since; nothing re-applied)")
    print(f"\nrows to write: {len(rows)}  (from events {len(ev_rows)}, disposition-only {len(rows) - len(ev_rows)})")


def refuse_args(args):
    """The flag combinations that never run — checked before a client is
    made (main) and again in run(), which tests drive directly."""
    if args.apply and args.preflight:
        return 'REFUSING: --preflight is read-only by definition; drop it to --apply.'
    if args.apply and args.since:
        return ('REFUSING: --since is a dry-run preview only — a window cannot recount event_count, and an '
                'account with no event in the window would be rebuilt from nothing (review 2026-09-08 '
                '(Phase 4)). Drop --since to --apply.')
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write rows (default: dry run)')
    ap.add_argument('--preflight', action='store_true',
                    help='plan against events BEFORE the accounts table exists (read-only; never writes)')
    ap.add_argument('--since', default=None,
                    help='DRY-RUN PREVIEW ONLY: events with discovered_at >= YYYY-MM-DD (refused with --apply)')
    ap.add_argument('--batch', type=int, default=500, help='page size for reads (default 500)')
    args = ap.parse_args(argv)
    msg = refuse_args(args)
    if msg:
        print(msg)
        return 2

    import logging
    import warnings
    warnings.filterwarnings('ignore')
    logging.getLogger('httpx').setLevel(logging.WARNING)   # the column probe 400s are expected
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
    from supabase import create_client

    svc = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
    return run(args, svc)


def run(args, svc, lock_path: str = None, now: datetime = None) -> int:
    """Everything after the client exists (tests inject a fake client, a
    lock file of their own and `now`). --apply holds the enrichment run
    lock for the whole plan + write: the plan is a snapshot of the table,
    and an enrichment run upserting between the read and the batches would
    have its rows overwritten by that snapshot (review 2026-09-08 (Phase 4)).
    A dry run takes no lock — it writes nothing and must never keep the
    launchd enrichment run from starting."""
    msg = refuse_args(args)
    if msg:
        print(msg)
        return 2
    lock = None
    if args.apply:
        lock = RunLock(lock_path or LOCK_PATH)
        if not lock.acquire():
            print(f"REFUSING: an enrichment run is active (pid {lock.holder_pid()}, lock {lock.path}) — its "
                  f"account upserts would interleave with this backfill's batches. Let it finish "
                  f"(tail -f logs/enrichment.log; `touch state/PAUSE` keeps launchd from starting another), "
                  f"then re-run --apply.")
            return 2
    try:
        return _run(args, svc, now=now)
    finally:
        if lock is not None:
            lock.release()


def _run(args, svc, now: datetime = None) -> int:
    table_live = A.probe_accounts(svc)
    if not table_live and not args.preflight:
        print(f"REFUSING: the accounts table is not in Supabase yet.\n"
              f"Paste {MIGRATION_SQL} into the Supabase SQL Editor and run it first, "
              f"then re-run this script (or add --preflight to plan read-only now).")
        return 2
    if table_live:
        present = A.account_columns_present(svc)
        missing = [c for c in A.ACCOUNT_COLUMNS if c not in present]
        if missing:
            print(f"REFUSING: the accounts table is missing columns: {', '.join(missing)}.\n"
                  f"Re-run {MIGRATION_SQL} (it is safe to re-run), then this script.")
            return 2
    else:
        present = set(A.ACCOUNT_COLUMNS)
        print(f"PREFLIGHT: accounts table not created yet — planning against events only, nothing can be written.")

    now = now or datetime.now(timezone.utc)
    typed = probe_columns(svc, 'events', TYPED_COLS)
    events = fetch_events(svc, EVENT_COLS + tuple(c for c in TYPED_COLS if c in typed),
                          args.since, max(1, args.batch))
    try:
        legacy = svc.table('account_dispositions').select('*').execute().data or []
    except Exception as e:
        print(f"  (account_dispositions unreadable — {type(e).__name__}; no rep verdicts applied)")
        legacy = []
    existing = []
    if table_live:
        try:
            existing = fetch_existing(svc, present, max(1, args.batch))
        except Exception as e:
            print(f"  accounts table UNREADABLE — {type(e).__name__}: {str(e)[:200]}")
            if args.apply:
                print("REFUSING: --apply needs the existing rows — a plan that cannot see them treats every "
                      "account as new and NULLs the dispositions, grades and facts the rows hold. "
                      "Fix the read, then re-run.")
                return 2
            print("  (dry-run report below assumes an EMPTY accounts table; --apply would refuse)")
    plan = plan_accounts(events, legacy, existing, now, since=args.since)
    print_report(plan, existing, events, args.since, dry_run=not args.apply)

    if not args.apply:
        print("\nDRY RUN — nothing written. " + (
            f"Run {MIGRATION_SQL}, then re-run with --apply to write." if not table_live
            else "Re-run with --apply to write."))
        return 0
    rows = [full_row(r, present, now.isoformat()) for r in plan['rows'].values()]
    n = write_rows(svc, rows, BATCH_WRITE)
    print(f"\nAPPLIED: {n} account rows upserted")
    return 0


if __name__ == '__main__':
    sys.exit(main())
