"""Typed-column layer for TeamAlbert v2 (Phase 2, 2026-09-07).

Until now every enrichment fact lived inside two JSONB blobs (`fit`,
`companies_data`), so the dashboard and cleanup scripts re-parsed JSON on
every read and Supabase could not index or filter any of it. Phase 2 adds
real columns (supabase/migrations/002_v2_typed_columns.sql) that mirror the
JSON. This module is the ONE place that knows the column names, the state
machine and the date arithmetic, so enrichment, backfill, sync and the
dashboard agree.

Contract (A.J. runs the migration by hand, later): every writer/reader
probes for the columns first and degrades to JSON-only when they are absent.
`typed_payload()` therefore RETURNS ONLY keys that exist, so callers can
merge it into any update payload without a try/except around the write.
It is also FILL-ONLY for fact columns (review 2026-09-07): a key whose
computed value is None is dropped, never sent — a re-enrichment that holds
less than the last one (the rep-decided branch passes no account at all)
must not overwrite a learned hq_state / SIC / classification with NULL.
`retry_after` is the one exception: passed explicitly as None it IS sent,
because clearing a stale retry date is intentional.

No third-party imports: this runs in GitHub Actions (3.11, requests+PyYAML)
and the Mac venv (3.9).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from src.pipeline.gates import account_key, hq_state_code

# ── Column inventory (mirrors 002_v2_typed_columns.sql) ─────────────────────
TYPED_EVENT_COLUMNS = (
    'source', 'account_key', 'fit_verdict', 'verify_state', 'hq_state',
    'in_territory', 'vertical', 'zi_subindustry', 'revenue_segment',
    'expires_at', 'sic', 'formd_industry_group', 'formd_revenue_range',
    'formd_offering_amount', 'formd_is_spac', 'enrich_attempts',
    'retry_after', 'classification_confidence', 'classified_by',
)
TYPED_SOURCE_STATUS_COLUMNS = ('items_fetched', 'filtered_out')

# Trigger shelf life (migrate_v2 Phase 1 numbers, A.J. 2026-09-04): a CFO
# hire is stale after two months, an M&A takes longer to shake out.
EXPIRY_DAYS = {'cfo_hire': 60, 'finance_seat_open': 60, 'executive_hire': 60,
               'funding': 60, 'merger_acquisition': 120, 'expansion': 90,
               'stable_target': 365, 'other': 60}
# researched_ambiguous rows are re-researched on a widening ladder, then
# negative-cached: a fresh event for the account is a fresh row anyway.
# Contract (settled at review 2026-09-07): attempt 1 → +7d, attempt 2 →
# +30d, attempt 3 → None (parked until a new event for the account
# arrives). The ladder therefore has MAX_ENRICH_ATTEMPTS - 1 rungs — a
# third rung (the old 90) could never be reached, because the attempt that
# would have used it is the one that negative-caches. This is NOT
# src/pipeline/cache.py's NEGATIVE_BACKOFF_DAYS (7, 30, 90): that ladder
# times per-account search empties and keeps its 90.
RETRY_BACKOFF_DAYS = (7, 30)
MAX_ENRICH_ATTEMPTS = 3
assert len(RETRY_BACKOFF_DAYS) == MAX_ENRICH_ATTEMPTS - 1, 'every retry rung must be reachable'
LLM_RETRY_HOURS = 4          # local llama.cpp unreachable → try again later, not never

VERIFY_STATES = ('verified', 'researched_ambiguous', 'staged', 'decided', 'not_fit')
REVENUE_SEGMENTS = ('LMM', 'MM', 'Corp', 'Enterprise')
_VERDICT_TO_STATE = {'pass': 'verified', 'unverified': 'researched_ambiguous',
                     'staged': 'staged', 'decided': 'decided', 'fail': 'not_fit'}

_UNSET = object()   # sentinel: "argument not passed" ≠ "passed None"


# ── Column probing ──────────────────────────────────────────────────────────
_probe_cache = {}   # table -> {column: present}


def reset_probe_cache() -> None:
    _probe_cache.clear()


class ProbeUnavailable(RuntimeError):
    """The schema probe could not get an ANSWER — timeout, network, 5xx,
    auth — as opposed to Postgres saying the column does not exist. The two
    must never be confused: on 2026-09-11 a slow Supabase made every probe
    time out, the run concluded "companies_data column missing", printed the
    migration SQL and exited 1, and the alert pipe woke A.J. for a column that
    has existed since June. Callers that act on 'absent' (enrichment exits;
    dashboards switch to legacy paths) either pass strict=True and handle this
    exception, or accept that a transport failure reads as 'absent for this
    call only' (never memoized)."""


# Postgres / PostgREST markers that mean "the schema really lacks this":
# 42703 undefined column, 42P01 undefined table, PGRST204 unknown column in a
# write payload, and PostgREST's own "... does not exist" phrasing.
_SCHEMA_ERROR_MARKERS = ('42703', '42P01', 'PGRST204', 'does not exist')


def is_schema_error(exc) -> bool:
    """True when the exception says the column/table is absent (a fact about
    the schema); False for timeouts, connection errors, 5xx, auth failures."""
    text = str(exc)
    return any(m in text for m in _SCHEMA_ERROR_MARKERS)


def _short_error(exc) -> str:
    return ' '.join(str(exc).split())[:160] or type(exc).__name__


def probe_columns(client, table: str, columns, *, strict: bool = False) -> set:
    """Set of `columns` that exist on `table`. One cheap select per column,
    memoized per table for the life of the process (the migration is a
    one-time manual step, so re-probing every row would be waste).

    Only a schema error (is_schema_error) is memoized as absent. A transport
    failure is NOT a fact about the schema: with strict=True it raises
    ProbeUnavailable; without, the column counts as absent for this call only
    and the next probe asks again (2026-09-11)."""
    known = _probe_cache.setdefault(table, {})
    present = set()
    for col in columns:
        if col not in known:
            try:
                q = client.table(table)
            except Exception as e:      # no client / dead client
                if strict:
                    raise ProbeUnavailable(_short_error(e)) from e
                return set()            # JSON-only mode for this call
            try:
                q.select(col).limit(1).execute()
                known[col] = True
            except Exception as e:      # noqa: BLE001 — classified below
                if is_schema_error(e):
                    known[col] = False
                elif strict:
                    raise ProbeUnavailable(f'{table}.{col}: {_short_error(e)}') from e
                else:
                    continue            # absent for THIS call; never memoized
        if known.get(col):
            present.add(col)
    return present


# ── State machine ───────────────────────────────────────────────────────────
def verify_state_for(fit_verdict: Optional[str], blocked: bool = False) -> Optional[str]:
    """fit verdict → verify_state. A tombstone (blocked_at set) is not_fit
    whatever the verdict says; an unknown/missing verdict maps to None so the
    row stays 'not yet enriched' rather than being mislabelled."""
    if blocked:
        return 'not_fit'
    return _VERDICT_TO_STATE.get(str(fit_verdict or '').strip().lower())


# ── Dates ───────────────────────────────────────────────────────────────────
def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    return _as_utc(now) if now is not None else datetime.now(timezone.utc)


def parse_ts(value) -> Optional[datetime]:
    """Tolerant timestamp parser → aware UTC datetime, or None.
    Accepts datetimes, ISO strings with 'Z' / offsets / microseconds, naive
    ISO strings and bare 'YYYY-MM-DD' (Supabase and the scrapers emit all of
    these). Python 3.9's fromisoformat rejects 'Z', hence the rewrite."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith('Z') or s.endswith('z'):
        s = s[:-1] + '+00:00'
    s = re.sub(r'([+-]\d{2})$', r'\1:00', s)          # '+00' → '+00:00'
    for cand in (s, s.replace(' ', 'T', 1)):
        try:
            return _as_utc(datetime.fromisoformat(cand))
        except ValueError:
            pass
    m = re.match(r'(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?))?', s)
    if not m:
        return None
    try:
        return _as_utc(datetime.fromisoformat(m.group(1) + ('T' + m.group(2) if m.group(2) else '')))
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return _as_utc(dt).isoformat()


def expires_at_for(event_type: Optional[str], published_date, discovered_at=None,
                   now: Optional[datetime] = None) -> Optional[str]:
    """published_date (fallback discovered_at) + EXPIRY_DAYS[event_type] as an
    ISO string; None when neither date is usable. `now` is accepted for
    signature symmetry with the other helpers (unused: expiry is anchored on
    the event's own date, never on when we computed it)."""
    base = parse_ts(published_date) or parse_ts(discovered_at)
    if base is None:
        return None
    days = EXPIRY_DAYS.get(str(event_type or '').strip().lower(), EXPIRY_DAYS['other'])
    return _iso(base + timedelta(days=days))


def retry_after_for(verify_state: Optional[str], attempts, now: Optional[datetime] = None) -> Optional[str]:
    """When to look at a researched_ambiguous row again. `attempts` is the
    count AFTER this attempt: 1st → +7d, 2nd → +30d (RETRY_BACKOFF_DAYS),
    3rd → None — negative-cached until a new event for the account arrives
    (the caller's `enrich_attempts < MAX_ENRICH_ATTEMPTS` select keeps it
    parked). Every other state → None."""
    if verify_state != 'researched_ambiguous':
        return None
    try:
        n = int(attempts or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= MAX_ENRICH_ATTEMPTS:
        return None
    days = RETRY_BACKOFF_DAYS[min(max(n - 1, 0), len(RETRY_BACKOFF_DAYS) - 1)]
    return _iso(_now(now) + timedelta(days=days))


def llm_retry_after(now: Optional[datetime] = None) -> str:
    return _iso(_now(now) + timedelta(hours=LLM_RETRY_HOURS))


# ── SEC structured fields ───────────────────────────────────────────────────
# The scrapers embed these literals in the description (sec_scraper.py
# 'SIC: NNNN', 'Form D industry group: X.', 'Declared revenue: Y.',
# 'Total offering: $N.', 'SPAC: yes.'); enrichment_scout._structured_verdict
# parses the same strings for its verdict. Parsed here too so the typed
# columns can be filled even when a caller only has the event row.
def parse_sec_fields(event: dict) -> dict:
    """{'sic', 'industry_group', 'revenue_range', 'offering_amount', 'spac'}
    (missing → None) for sec.gov events; all-None otherwise."""
    out = {'sic': None, 'industry_group': None, 'revenue_range': None,
           'offering_amount': None, 'spac': None}
    if 'sec.gov' not in (event.get('source_url') or ''):
        return out
    desc = event.get('description') or ''
    m = re.search(r'SIC:\s*(\d{4})', desc)
    if m:
        out['sic'] = m.group(1)
    if 'Form D' in (event.get('title') or ''):
        grp = re.search(r'industry group: ([^.]+)\.', desc)
        rr = re.search(r'Declared revenue: ([^.]+?)\.(?:\s|$)', desc)
        amt = re.search(r'Total offering: \$([\d,]+)', desc)
        out['industry_group'] = grp.group(1).strip() if grp else None
        out['revenue_range'] = rr.group(1).strip() if rr else None
        out['offering_amount'] = float(amt.group(1).replace(',', '')) if amt else None
        out['spac'] = 'SPAC: yes' in desc
    return out


def _sec_typed(event: dict, structured: Optional[dict]) -> dict:
    """Typed SIC / Form D columns: explicit keys on `structured` win (a
    future _structured_verdict may expose them), else parse the description."""
    parsed = parse_sec_fields(event or {})
    s = structured or {}
    def pick(key):
        v = s.get(key)
        return v if v not in (None, '') else parsed.get(key)
    amount = pick('offering_amount')
    try:
        amount = float(amount) if amount not in (None, '') else None
    except (TypeError, ValueError):
        amount = None
    spac = pick('spac')
    return {
        'sic': pick('sic') or None,
        'formd_industry_group': pick('industry_group') or None,
        'formd_revenue_range': pick('revenue_range') or None,
        'formd_offering_amount': amount,
        'formd_is_spac': bool(spac) if spac is not None else None,
    }


# ── Payload builders ────────────────────────────────────────────────────────
def _norm_dim(v) -> Optional[str]:
    """fit territory/vertical → in|out|unknown (fit uses 'n/a' for decided rows)."""
    s = str(v or '').strip().lower()
    return s if s in ('in', 'out', 'unknown') else None


def _revenue_segment(account: Optional[dict], structured: Optional[dict]) -> Optional[str]:
    for cand in ((account or {}).get('revenue'), (structured or {}).get('revenue_segment')):
        if cand in REVENUE_SEGMENTS:
            return cand
    return None


def typed_payload(*, event: dict, fit: Optional[dict], structured: Optional[dict] = None,
                  account: Optional[dict] = None, verify_state: Optional[str],
                  present, now: Optional[datetime] = None, attempts=None,
                  retry_after=_UNSET, classification_confidence=None,
                  classified_by=None) -> dict:
    """Typed columns computed from what enrichment already holds. Only keys
    named in `present` (from probe_columns) are returned — an empty set
    yields {} so the JSON-only path is untouched.

    FILL-ONLY (review 2026-09-07): a fact column whose value came out None
    is dropped, not sent as NULL. Before this, every key was emitted, so a
    re-enrichment with less context than the last one (the rep-decided
    branch passes no account; a regrade may lack SEC/structured data)
    overwrote a learned hq_state / zi_subindustry / revenue_segment / sic /
    formd_* / classification_confidence / classified_by with NULL. The same
    rule covers verify_state / fit_verdict / in_territory / vertical /
    account_key / expires_at — an unknown never replaces a known.

    enrich_attempts is included only when `attempts` is given; retry_after
    only when the caller passes it explicitly, and it is the ONE key that
    survives as None (None then CLEARS the column — a verified row must not
    keep a stale retry date)."""
    event = event or {}
    fit = fit or {}
    account = account or {}
    name = fit.get('account_name') or event.get('company_name') or ''
    full = {
        'account_key': account_key(name) or None,
        'fit_verdict': (str(fit.get('verdict') or '').strip().lower() or None),
        'verify_state': verify_state,
        'hq_state': hq_state_code(account.get('hq')),
        'in_territory': _norm_dim(fit.get('territory')),
        'vertical': _norm_dim(fit.get('vertical')),
        'zi_subindustry': (fit.get('zi_subindustry') or account.get('zi_subindustry') or None),
        'revenue_segment': _revenue_segment(account, structured),
        'expires_at': expires_at_for(event.get('event_type'), event.get('published_date'),
                                     event.get('discovered_at'), now=now),
        'classification_confidence': classification_confidence,
        'classified_by': classified_by,
    }
    full.update(_sec_typed(event, structured))
    if attempts is not None:
        full['enrich_attempts'] = int(attempts)
    if retry_after is not _UNSET:
        full['retry_after'] = retry_after
    present = set(present or ())
    return {k: v for k, v in full.items()
            if k in present and (v is not None or k == 'retry_after')}


def not_fit_payload(present, reason: Optional[str] = None,
                    now: Optional[datetime] = None) -> dict:
    """Typed half of a tombstone (_soft_delete): the row is out, whatever
    the JSON said. `reason`/`now` are accepted so callers can pass what they
    have; the columns themselves carry no reason (blocked_reason does)."""
    present = set(present or ())
    full = {'verify_state': 'not_fit', 'fit_verdict': 'fail'}
    return {k: v for k, v in full.items() if k in present}


def is_adzuna(event: dict) -> bool:
    """Adzuna job posts (an open finance seat, not a hire) are told apart by
    their URL host — the scrape's `source` enum is not in Supabase yet."""
    try:
        host = urlparse((event or {}).get('source_url') or '').netloc.lower()
    except Exception:
        return False
    return 'adzuna' in host
