#!/usr/bin/env python3
"""Nightly trigger expiry (Phase 4 slice C2, 2026-09-08).

A trigger has a shelf life (A.J. 2026-09-04: CFO / open seat / funding 60
days, M&A 120, expansion 90, stable_target 365 — src/pipeline/typed.EXPIRY_DAYS).
A lead older than that is not a trigger any more, whatever its grade said
when it was fresh. This script:

  1. tombstones every NON-tombstoned event whose expiry is in the past —
     the typed `expires_at` column when it is filled, else
     published_date (fallback discovered_at) + EXPIRY_DAYS[event_type] —
     with blocked_reason 'trigger_expired: <n>d old <event_type>' and
     blocked_at = now. NOTHING ELSE is written: an expired trigger is not
     "not a fit", so verify_state / fit / grade / companies_data stay as
     they are (the research is kept, the row is merely hidden — enrichment
     and the dashboard already skip blocked rows). No hard deletes, ever.
  2. when the accounts table is live (src/pipeline/accounts.py present and
     its probe says so), refreshes each affected account from its REMAINING
     active events — the best TRIGGER (enriched rows first, then
     accounts.TRIGGER_PRIORITY, then most recent) for best_trigger_*, and
     the best GRADE (GRADE_RANK, then score, then recency) for the grade
     fields, graded_at = that event's enriched_at — or clears them when
     nothing remains (the account stays active: a new trigger tomorrow
     re-grades it). Review 2026-09-08 (Phase 4), 5a: the refresh used to
     take the best trigger's grade, which could be None for a queued
     (un-enriched) event while a graded live event remained, and disagreed
     with accounts.merge_account (best GRADE) — the two writers flapped.
     Affected = the chosen accounts of the expired events PLUS every account
     whose best_trigger_event_id is an expired event (5b: a secondary
     account — an M&A target touched facts-only — points at it too).

Dry-run by default: prints the before/after table by event_type and every
change it WOULD make. `--apply` writes, under the enrichment run lock
(state/enrichment.lock — 5d: never alongside a live enrichment run; exits
0 with a message when it is held). `--days-grace N` keeps a trigger N days
past its expiry (default 0). Zero search, zero LLM, read-only on everything
it does not tombstone. run_reverify.sh runs it with --apply before the
daily re-verify pass so no search is spent on a stale trigger.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.pipeline.gates import account_key  # noqa: E402
from src.pipeline.typed import EXPIRY_DAYS, expires_at_for, parse_ts, probe_columns  # noqa: E402

try:                                            # slice C1, another engineer — optional
    from src.pipeline import accounts as _accounts  # noqa: E402
except ImportError:
    _accounts = None

BASE_COLS = ('id', 'event_type', 'title', 'company_name', 'published_date', 'discovered_at',
             'fit', 'grade', 'numeric_score', 'confidence_level', 'hashtags',
             'grade_justification', 'blocked_at', 'enriched_at')
# Typed columns the script reads when they exist (002 migration); it runs
# without them (expiry from the dates, account key from the JSON / name).
TYPED_COLS = ('expires_at', 'account_key', 'verify_state')
# Fallback ranking when the accounts module does not expose TRIGGER_PRIORITY:
# the v2 re-verify order (finance leader > M&A > funding).
DEFAULT_TRIGGER_PRIORITY = {'cfo_hire': 0, 'finance_seat_open': 1, 'executive_hire': 2,
                            'merger_acquisition': 3, 'funding': 4, 'expansion': 5,
                            'stable_target': 6, 'other': 7}
# The accounts columns a refresh rewrites (C1 table contract). `active` is
# deliberately absent: an account with no live trigger is dormant, not gone.
ACCOUNT_GRADE_FIELDS = ('grade', 'numeric_score', 'confidence_level', 'hashtags',
                        'grade_justification', 'graded_event_id', 'graded_at')
ACCOUNT_TRIGGER_FIELDS = ('best_trigger_type', 'best_trigger_at', 'best_trigger_event_id')
ACCOUNT_COLS = 'account_key,canonical_name,grade,best_trigger_type,best_trigger_event_id,active'
# Grade order — the accounts module's when it exposes one, so the two
# writers rank identically ('Unable to Grade' / None = no grade).
GRADE_RANK = dict(getattr(_accounts, 'GRADE_RANK', None) or {'A': 0, 'B': 1, 'C': 2, 'D': 3})
# 5d: the same lock enrichment_scout takes (state/enrichment.lock).
LOCK_PATH = os.path.join(_ROOT, 'state', 'enrichment.lock')


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


# ── Pure decisions ──────────────────────────────────────────────────────────
def event_anchor(row: dict) -> Optional[datetime]:
    """The date the shelf life counts from: published_date, else discovered_at."""
    return parse_ts(row.get('published_date')) or parse_ts(row.get('discovered_at'))


def event_expiry(row: dict) -> Optional[datetime]:
    """When the trigger expires: the typed expires_at when filled, else the
    anchor date + EXPIRY_DAYS[event_type]. None when no date is usable — such
    a row is never expired by this script (it cannot know)."""
    typed = parse_ts(row.get('expires_at')) if row.get('expires_at') else None
    if typed:
        return typed
    iso = expires_at_for(row.get('event_type'), row.get('published_date'), row.get('discovered_at'))
    return parse_ts(iso) if iso else None


def event_age_days(row: dict, now: Optional[datetime] = None) -> Optional[int]:
    anchor = event_anchor(row)
    return (_now(now) - anchor).days if anchor else None


def is_expired(row: dict, now: Optional[datetime] = None, grace_days: int = 0) -> bool:
    """True for a NON-tombstoned row whose expiry (plus grace) is in the past."""
    if row.get('blocked_at'):
        return False
    exp = event_expiry(row)
    if exp is None:
        return False
    return (_now(now) - exp).total_seconds() > max(0, grace_days) * 86400


def expiry_reason(row: dict, now: Optional[datetime] = None) -> str:
    age = event_age_days(row, now)
    et = str(row.get('event_type') or 'other')
    return f'trigger_expired: {age if age is not None else "?"}d old {et}'


def tombstone_payload(row: dict, now: Optional[datetime] = None) -> dict:
    """blocked_at + blocked_reason ONLY — verify_state, enriched_at, fit,
    grade and the research stay untouched (an expired trigger is not a
    'not_fit' verdict on the account)."""
    return {'blocked_at': _now(now).isoformat(), 'blocked_reason': expiry_reason(row, now)[:300]}


def row_account_key(row: dict) -> str:
    """The typed account_key when present, else the key of the account the
    fit gates chose (fit.account_name), else the event's company_name."""
    if row.get('account_key'):
        return str(row['account_key'])
    fit = _j(row.get('fit')) or {}
    name = (fit.get('account_name') if isinstance(fit, dict) else None) or row.get('company_name') or ''
    return account_key(str(name))


def trigger_rank(event_type, priority=None) -> int:
    """Lower is better. `priority` may be the accounts module's dict
    ({type: rank}) or an ordered list/tuple; unknown types rank last."""
    et = str(event_type or 'other')
    prio = priority if priority is not None else DEFAULT_TRIGGER_PRIORITY
    if isinstance(prio, dict):
        return int(prio.get(et, prio.get('other', len(prio))))
    if isinstance(prio, (list, tuple)):
        return list(prio).index(et) if et in prio else len(prio)
    return DEFAULT_TRIGGER_PRIORITY.get(et, len(DEFAULT_TRIGGER_PRIORITY))


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def is_graded(row: dict) -> bool:
    """Carries a real grade (A-D) — not None, not 'Unable to Grade'."""
    return str((row or {}).get('grade') or '').strip().upper() in GRADE_RANK


def is_enriched(row: dict) -> bool:
    """Has been through enrichment (enriched_at stamped, or graded): its fit
    is known. An un-enriched row is a queued event that may be tombstoned
    tomorrow — never the account's best trigger while an enriched one lives."""
    return bool((row or {}).get('enriched_at')) or is_graded(row)


def best_remaining(events, priority=None) -> Optional[dict]:
    """The account's best live TRIGGER: enriched rows first (5a), then
    highest TRIGGER_PRIORITY, then the most recent anchor date."""
    live = [e for e in events if not e.get('blocked_at')]
    if not live:
        return None
    return min(live, key=lambda e: (0 if is_enriched(e) else 1,
                                    trigger_rank(e.get('event_type'), priority),
                                    -(event_anchor(e) or _EPOCH).timestamp()))


def best_graded(events) -> Optional[dict]:
    """The account's best live GRADE: GRADE_RANK, then numeric_score, then
    the most recent anchor — the order accounts.merge_account keeps, so the
    nightly refresh and the enrichment writer agree (5a). None when no live
    event carries a grade."""
    live = [e for e in events if not e.get('blocked_at') and is_graded(e)]
    if not live:
        return None

    def _score(e):
        try:
            return float(e.get('numeric_score') or 0)
        except (TypeError, ValueError):
            return 0.0
    return min(live, key=lambda e: (GRADE_RANK[str(e.get('grade')).strip().upper()], -_score(e),
                                    -(event_anchor(e) or _EPOCH).timestamp()))


def account_refresh_payload(best: Optional[dict], now: Optional[datetime] = None,
                            graded: Optional[dict] = None) -> dict:
    """What the accounts row becomes: best_trigger_* from `best` (the best
    remaining TRIGGER) and the grade fields from `graded` (the best remaining
    GRADE — defaults to `best` itself when it is graded), graded_at = that
    event's enriched_at (5c). Everything cleared when no live trigger
    remains; the grade fields cleared when no live event is graded — never
    while one is (5a)."""
    if best is None:
        return {k: None for k in ACCOUNT_TRIGGER_FIELDS + ACCOUNT_GRADE_FIELDS}
    anchor = event_anchor(best)
    pl = {
        'best_trigger_type': best.get('event_type'),
        'best_trigger_at': anchor.isoformat() if anchor else None,
        'best_trigger_event_id': best.get('id'),
    }
    g = graded if graded is not None else (best if is_graded(best) else None)
    if g is None or not is_graded(g):
        pl.update({k: None for k in ACCOUNT_GRADE_FIELDS})
        return pl
    hashtags = _j(g.get('hashtags'))
    if isinstance(hashtags, str):
        hashtags = [h for h in hashtags.split() if h.startswith('#')]
    graded_at = parse_ts(g.get('enriched_at')) or _now(now)
    pl.update({
        'grade': str(g.get('grade')).strip().upper(),
        'numeric_score': g.get('numeric_score'),
        'confidence_level': g.get('confidence_level'),
        'hashtags': hashtags if isinstance(hashtags, list) else [],
        'grade_justification': g.get('grade_justification'),
        'graded_event_id': g.get('id'),
        'graded_at': graded_at.isoformat(),
    })
    return pl


def plan(rows, now: Optional[datetime] = None, grace_days: int = 0):
    """(expired rows, {account_key: remaining live rows}, affected keys)."""
    expired = [r for r in rows if is_expired(r, now, grace_days)]
    gone = {r['id'] for r in expired}
    remaining = defaultdict(list)
    for r in rows:
        if r.get('blocked_at') or r['id'] in gone:
            continue
        remaining[row_account_key(r)].append(r)
    affected = sorted({row_account_key(r) for r in expired if row_account_key(r)})
    return expired, remaining, affected


# ── Supabase I/O ────────────────────────────────────────────────────────────
def fetch_active_events(client, present, batch: int = 500) -> list:
    """Every non-tombstoned event, oldest first, paged (Supabase caps a
    select at 1,000 rows)."""
    cols = ','.join(BASE_COLS + tuple(c for c in TYPED_COLS if c in present))
    rows, off = [], 0
    while True:
        page = (client.table('events').select(cols).is_('blocked_at', 'null')
                .order('discovered_at', desc=False).range(off, off + batch - 1)
                .execute().data or [])
        rows += page
        if len(page) < batch:
            break
        off += batch
    return rows


_DEFAULT_MODULE = object()      # sentinel: "use src.pipeline.accounts" ≠ "no module" (None)


def _module(accounts_module):
    return _accounts if accounts_module is _DEFAULT_MODULE else accounts_module


def accounts_live(client, accounts_module=_DEFAULT_MODULE) -> bool:
    mod = _module(accounts_module)
    if mod is None:
        return False
    try:
        return bool(mod.probe_accounts(client))
    except Exception:       # noqa: BLE001 — the events half never depends on the table
        return False


def load_account_rows(client, keys, batch: int = 100) -> dict:
    """{account_key: row} for the keys that exist in the accounts table."""
    out = {}
    keys = [k for k in keys if k]
    for i in range(0, len(keys), batch):
        chunk = keys[i:i + batch]
        rows = (client.table('accounts').select(ACCOUNT_COLS)
                .in_('account_key', chunk).execute().data or [])
        for r in rows:
            out[r['account_key']] = r
    return out


def load_accounts_by_trigger(client, event_ids, batch: int = 100) -> dict:
    """{account_key: row} for every account whose best_trigger_event_id is one
    of `event_ids` (5b): the secondary accounts an expired event was the
    trigger for without being their chosen account — touch_secondary gave
    an M&A target the acquirer's event as its best trigger."""
    out = {}
    ids = [i for i in event_ids if i]
    for i in range(0, len(ids), batch):
        rows = (client.table('accounts').select(ACCOUNT_COLS)
                .in_('best_trigger_event_id', ids[i:i + batch]).execute().data or [])
        for r in rows:
            out[r['account_key']] = r
    return out


# ── Reporting ───────────────────────────────────────────────────────────────
def print_table(rows, expired, out=print):
    before = Counter(str(r.get('event_type') or 'NULL') for r in rows)
    gone = Counter(str(r.get('event_type') or 'NULL') for r in expired)
    out('\nactive events by event_type')
    out(f"  {'event_type':<22}{'before':>8}{'expired':>9}{'after':>8}")
    for k in sorted(before, key=lambda x: (-before[x], x)):
        out(f'  {k:<22}{before[k]:>8}{gone[k]:>9}{before[k] - gone[k]:>8}')
    out(f"  {'total':<22}{sum(before.values()):>8}{sum(gone.values()):>9}"
        f"{sum(before.values()) - sum(gone.values()):>8}")


def run(client, apply: bool = False, grace_days: int = 0, now: Optional[datetime] = None,
        accounts_module=_DEFAULT_MODULE, out=print) -> dict:
    """The whole pass against `client`; returns the counts (for tests and
    the caller). Pure I/O separation: `plan()` decides, this function prints
    and — only with apply — writes. `accounts_module=None` means "no
    accounts layer at all" (tests); the default is src.pipeline.accounts."""
    now = _now(now)
    mod = _module(accounts_module)
    present = probe_columns(client, 'events', TYPED_COLS)
    rows = fetch_active_events(client, present)
    expired, remaining, affected = plan(rows, now, grace_days)
    mode = 'APPLY' if apply else 'DRY RUN'
    out(f'{mode} — {len(rows)} active event(s) read; typed columns present: '
        f'{", ".join(sorted(present)) or "none"}; grace {grace_days}d; '
        f'shelf life {EXPIRY_DAYS}')
    print_table(rows, expired, out)

    for r in expired:
        out(f"  {'tombstone' if apply else 'would tombstone'}  {r['id']}  "
            f"{expiry_reason(r, now)}  · {str(r.get('title') or '')[:60]}")

    counts = {'read': len(rows), 'expired': len(expired), 'tombstoned': 0,
              'accounts_affected': len(affected), 'accounts_refreshed': 0,
              'accounts_cleared': 0, 'accounts_table': False}
    if apply:
        for r in expired:
            client.table('events').update(tombstone_payload(r, now)).eq('id', r['id']).execute()
            counts['tombstoned'] += 1

    # ── accounts refresh (only when the table is live) ──
    if expired and accounts_live(client, mod):
        counts['accounts_table'] = True
        priority = getattr(mod, 'TRIGGER_PRIORITY', None)
        existing = load_account_rows(client, affected)
        # 5b: accounts pointing at an expired event as their best trigger
        # without being its chosen account (secondaries) are refreshed too.
        linked = load_accounts_by_trigger(client, [r['id'] for r in expired])
        extra = sorted(k for k in linked if k not in existing and k not in affected)
        if extra:
            out(f'  + {len(extra)} account(s) whose best trigger is an expired event: '
                + ', '.join(repr(k) for k in extra[:10]) + (' …' if len(extra) > 10 else ''))
        existing.update(linked)
        affected = sorted(set(affected) | set(linked))
        counts['accounts_affected'] = len(affected)
        for key in affected:
            if key not in existing:
                continue
            live_rows = remaining.get(key, [])
            best = best_remaining(live_rows, priority)
            graded = best_graded(live_rows)
            payload = account_refresh_payload(best, now, graded=graded)
            was = existing[key]
            out(f"  {'refresh' if apply else 'would refresh'} account {key!r}: "
                f"trigger {was.get('best_trigger_type')} → {payload['best_trigger_type']}, "
                f"grade {was.get('grade')} → {payload['grade']}"
                + ('' if best else '  (no live trigger left — cleared, stays active)')
                + ('' if best is None or graded is not None else
                   '  (no graded live event — grade cleared)')
                + ('' if graded is None or graded is best else
                   f"  (grade from {graded['id']}, trigger from {best['id']})"))
            if apply:
                client.table('accounts').update(payload).eq('account_key', key).execute()
            counts['accounts_refreshed' if best else 'accounts_cleared'] += 1
    elif affected:
        out(f'  accounts table not live — {len(affected)} affected account(s) not refreshed')

    if not expired:
        out('\nNo expired triggers — nothing to do.')
    elif apply:
        out(f"\nAPPLIED — tombstoned {counts['tombstoned']} expired trigger(s); "
            f"accounts refreshed {counts['accounts_refreshed']}, cleared {counts['accounts_cleared']}"
            + ('' if counts['accounts_table'] else ' (accounts table not live)'))
    else:
        out(f"\nDRY RUN — would tombstone {len(expired)} expired trigger(s) across "
            f"{len(affected)} account(s); nothing written. Re-run with --apply to write.")
    return counts


def take_apply_lock(path: str = None):
    """The enrichment run lock for an --apply pass (5d), or None when another
    enrichment / expiry process holds it. flock-based: released on crash."""
    from src.pipeline.runlock import RunLock
    lock = RunLock(path or LOCK_PATH)
    return lock if lock.acquire() else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='write the tombstones (default: dry run)')
    ap.add_argument('--days-grace', type=int, default=0,
                    help='keep a trigger this many days past its expiry (default 0)')
    args = ap.parse_args(argv)

    lock = None
    if args.apply:
        # 5d: the tombstones and account refreshes race with a live
        # enrichment run on the same rows — take its lock, and step aside
        # (exit 0: the nightly wrapper is not failing, it is yielding).
        lock = take_apply_lock()
        if lock is None:
            print(f'another enrichment run holds {LOCK_PATH} — expiry skipped, nothing written '
                  f'(it runs again with the next pass)')
            return 0
    try:
        import logging
        import warnings
        warnings.filterwarnings('ignore')
        logging.getLogger('httpx').setLevel(logging.WARNING)   # the column probe 400s are expected
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, '.env'))
        from supabase import create_client
        url = os.environ.get('SUPABASE_URL')
        key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ.get('SUPABASE_KEY')
        if not url or not key:
            print('Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env')
            return 2
        client = create_client(url, key)
        run(client, apply=args.apply, grace_days=args.days_grace)
        return 0
    finally:
        if lock is not None:
            lock.release()


if __name__ == '__main__':
    sys.exit(main())
