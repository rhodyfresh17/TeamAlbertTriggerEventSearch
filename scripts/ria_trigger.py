#!/usr/bin/env python3
"""New SEC-registered investment advisers → trigger events (v2 Phase 3,
slice B3, 2026-09-08).

WHY: a new SEC registration is a real, DATED trigger. The firm either just
formed or crossed $100M in regulatory assets (the SEC threshold) — a growth
milestone when new systems get bought. Research 2026-09-08: ~1,000 new SEC
registrations per year land in our territory; ~50/yr are in-band. Zero
search spend: every fact comes from the free SEC IAPD monthly feed that
scripts/refresh_oracles.py (another slice) loads into state/oracles.db.

WHAT IT DOES
  1. Reads ria_firm from state/oracles.db (schema = the oracle contract).
     reg_date is stored as the feed's Rgstn@Dt — ISO 'YYYY-MM-DD' in the
     2026 compilation — but parse_reg_date also reads 'MM/DD/YYYY', the
     shape older SEC extracts used (review 2026-09-08, L7).
  2. Keeps Registered firms (ERA = exempt reporting advisers, no RAUM, are
     skipped) whose main office is in TERRITORY_STATES (US states + DC +
     the six eastern provinces; a foreign country column disqualifies),
     whose registration date is within --window-days (default 75) of today,
     and whose revenue proxy is in band — the SAME rule enrichment's
     registry applies (src/pipeline/oracles.py, review 2026-09-08 M1/P3):
       proxies        = RAUM x 0.7% and employees x $400K (whichever exist)
       segment        = from the SMALLER proxy (a $15B-RAUM firm with 40
                        staff is a 40-person firm), through the ±30%
                        margin bands: an estimate under $6.5M or between
                        $77M and $130M is UNKNOWN (segment None — one
                        enrichment search decides), > $130M is Enterprise
       too_small      = only when even the LARGER proxy is under $3.5M —
                        10 staff ($4M) with $2B RAUM ($14M) is not a $4M firm
       enterprise     = the smaller proxy > $130M — skipped: enrichment's
                        fit gate would tombstone it on arrival
       --include-small  also emits too-small and size-unknown firms.
  3. Builds ONE 'expansion' event per firm with the same id rule as the
     scrapers (md5 of "url:title", src/scrapers/base.py) and seeds the typed
     columns the oracle is authoritative for (hq_state, zi_subindustry,
     revenue_segment, expires_at = registration + 90 days, source
     'sec_iapd'). verify_state stays NULL so enrichment picks the row up
     (enriched_at IS NULL) and grades it like any other event.
  4. Dedups twice: against Supabase (one select per batch of 50 ids) and
     against a local ledger table ria_trigger_emitted in the same
     oracles.db — a firm is emitted at most once per registration date, so a
     re-registration years later (new reg_date) is a new trigger. The
     ledger is what makes a WIDE window safe: WINDOW_DAYS = 75 (M5, review
     2026-09-08) because the job runs on the 2nd of the month and the feed
     it reads was compiled on the 1st, covering registrations through the
     PRIOR month end — and when that compilation is not up yet the refresh
     falls back to the month before. A 45-day window on a fallback month
     lost about two weeks of registrations for good; 75 days always spans
     the fallback month plus the month after it, and the ledger dedups the
     overlap.
  5. DRY RUN BY DEFAULT: prints the table of what it WOULD emit and writes
     nothing (the Supabase dedup read is the only network call, and only
     when credentials are present). --apply upserts in batches of 50 with
     on_conflict='id' + ignore_duplicates=True (an existing row is never
     overwritten — enrichment/rep columns are safe) and then records the
     ledger rows for that batch.

Exit codes: 0 = done or nothing to do (including "oracle table not built
yet"); 1 = a real failure (--apply without credentials, a rejected upsert,
an oracle table that does not match the contract).

Runs from run_oracles.sh right after the oracle refresh (monthly, the 2nd
at 05:00 ET, com.teamalbert.oracles.plist). Logs to stdout only.

Usage:
    venv/bin/python scripts/ria_trigger.py                 # dry run (default)
    venv/bin/python scripts/ria_trigger.py --include-small # also sub-$5M firms
    venv/bin/python scripts/ria_trigger.py --window-days 90 --limit 20
    venv/bin/python scripts/ria_trigger.py --apply         # write events + ledger
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.pipeline.gates import STATE_NAMES, TERRITORY_STATES          # noqa: E402
from src.pipeline.oracles import (                                      # noqa: E402
    ENTERPRISE_CEIL, ENTERPRISE_USD, RIA_RAUM_TO_REV, RIA_REV_PER_EMP, TOO_SMALL_FLOOR,
    TOO_SMALL_USD, estimate_band, ria_revenue_band, ria_revenue_proxies,
)
from src.pipeline.typed import (                                        # noqa: E402
    EXPIRY_DAYS, TYPED_EVENT_COLUMNS, expires_at_for, probe_columns,
)
from supabase_sync import (                                             # noqa: E402
    DESCRIPTION_MAX_CHARS, NEVER_SYNC_COLUMNS, SCRAPE_OWNED_COLUMNS,
)

# ── Contract constants ──────────────────────────────────────────────────────
DEFAULT_DB = os.path.join(_ROOT, 'state', 'oracles.db')
FIRM_TABLE = 'ria_firm'
META_TABLE = 'oracle_meta'
LEDGER_TABLE = 'ria_trigger_emitted'

SOURCE = 'sec_iapd'                 # events.source enum value (new; label 'SEC IAPD')
EVENT_TYPE = 'expansion'
SOURCE_URL = 'https://adviserinfo.sec.gov/firm/summary/{crd}'
FIRM_TYPE = 'Registered'            # ERA firms are never emitted
# M5 (review 2026-09-08): the feed loaded on the 2nd may be the PREVIOUS
# month's compilation (404 fallback); 75 days always covers that month and
# the one after it. Nothing else derives from this — the ledger dedups.
WINDOW_DAYS = 75
BATCH_SIZE = 50

# Revenue rule: the SAME constants enrichment's registry uses (never drift).
REVENUE_BAR = TOO_SMALL_USD         # A.J.'s bar ($5M)
ENTERPRISE_ABOVE = ENTERPRISE_USD   # > $100M = Enterprise = out of the NetSuite band
RAUM_REVENUE_RATE = RIA_RAUM_TO_REV # RAUM x 0.7% ≈ advisory fee revenue
REVENUE_PER_EMPLOYEE = RIA_REV_PER_EMP  # cross-check: $400K revenue per head

ZI_SUBINDUSTRY = {'Registered': 'Lending & Brokerage',
                  'ERA': 'Venture Capital & Private Equity'}

# Typed columns this script seeds (the oracle IS the authority for them).
# verify_state is deliberately NOT here — NULL lets enrichment run normally.
SEED_COLUMNS = ('source', 'hq_state', 'zi_subindustry', 'revenue_segment', 'expires_at')
assert set(SEED_COLUMNS) <= set(TYPED_EVENT_COLUMNS), 'seed columns must be typed columns'
assert EXPIRY_DAYS[EVENT_TYPE] == 90, 'expiry contract for expansion events changed'
assert not (set(SEED_COLUMNS) & NEVER_SYNC_COLUMNS)

_US_COUNTRY = {'', 'US', 'USA', 'UNITED STATES', 'UNITED STATES OF AMERICA'}
_CA_COUNTRY = {'CA', 'CAN', 'CANADA'}

# Selection outcomes, in the order they are decided (the summary prints them
# in this order too, so the funnel reads top-down).
REASONS = ('outside_window', 'bad_reg_date', 'not_registered', 'out_of_territory',
           'size_unknown', 'too_small', 'enterprise', 'already_emitted',
           'already_in_supabase', 'size_borderline')


# ── Pure helpers ────────────────────────────────────────────────────────────
def parse_reg_date(raw) -> Optional[date]:
    """SEC feed 'MM/DD/YYYY' (also 'M/D/YYYY', ISO 'YYYY-MM-DD', ISO
    datetimes) → date, or None when unreadable."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def territory_code(state, country=None) -> Optional[str]:
    """2-letter code when the main office is in TERRITORY_STATES, else None.
    Accepts codes ('MA', 'on') and full names ('Massachusetts', 'ONTARIO').
    A country other than the US/Canada disqualifies whatever the state says
    (foreign firms sometimes carry a home-country region in that column)."""
    c = re.sub(r'[^A-Z ]', '', (country or '').strip().upper()).strip()
    if c and c not in _US_COUNTRY and c not in _CA_COUNTRY:
        return None
    s = (state or '').strip()
    if not s:
        return None
    up = s.upper()
    code = up if (len(up) == 2 and up.isalpha()) else STATE_NAMES.get(s.lower())
    return code if code in TERRITORY_STATES else None


def _num(v) -> Optional[float]:
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_money(n) -> str:
    """$2B / $1.2B / $12M / $700K / $950 / n/a — for tables and prose."""
    n = _num(n)
    if n is None:
        return 'n/a'
    for div, suf in ((1e12, 'T'), (1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if abs(n) >= div:
            v = f'{n / div:.1f}'.rstrip('0').rstrip('.')
            return f'${v}{suf}'
    return f'${n:,.0f}'


def revenue_proxies(raum_usd, total_employees) -> Tuple[Optional[float], Optional[float]]:
    """(RAUM x 0.7%, employees x $400K) — each None when not reported;
    tolerant of text numbers."""
    raum, emp = _num(raum_usd), _num(total_employees)
    by_raum, by_emp = ria_revenue_proxies(int(raum) if raum else None,
                                          int(emp) if emp else None)
    return (float(by_raum) if by_raum else None, float(by_emp) if by_emp else None)


def revenue_estimate(raum_usd, total_employees) -> Tuple[Optional[float], str]:
    """(estimate in $, basis sentence). The SMALLER of the two proxies when
    both exist — an RIA with $15B RAUM and 40 staff is a 40-person firm.
    This places the SEGMENT; too_small is decided by the larger proxy in
    classify_firm (M1)."""
    by_raum, by_emp = revenue_proxies(raum_usd, total_employees)
    emp = _num(total_employees)
    if by_raum is not None and by_emp is not None:
        return (min(by_raum, by_emp),
                f'the smaller of RAUM x 0.7% ({fmt_money(by_raum)}) and '
                f'{int(emp)} employees x $400K ({fmt_money(by_emp)})')
    if by_raum is not None:
        return by_raum, 'RAUM x 0.7% (headcount not reported)'
    if by_emp is not None:
        return by_emp, f'{int(emp)} employees x $400K (RAUM not reported)'
    return None, 'no RAUM or headcount reported'


def revenue_segment(estimate) -> Optional[str]:
    """NetSuite up-market taxonomy (CLAUDE.md §2) with the boundaries and
    the ±30% margin bands of oracles.estimate_band (L7 / P3, review
    2026-09-08): LMM <= $10M · MM <= $20M · Corp <= $100M · Enterprise
    > $130M; None when the size is unknown OR the estimate sits inside a
    margin band (< $6.5M, or $77M-$130M) — enrichment searches once then."""
    est = _num(estimate)
    if est is None:
        return None
    return estimate_band(est)[0]


def generate_event_id(url: str, title: str) -> str:
    """Same rule as src/scrapers/base.py BaseScraper.generate_event_id."""
    return hashlib.md5(f'{url}:{title}'.encode()).hexdigest()


def _display_city(city) -> str:
    c = (city or '').strip()
    return c.title() if c.isupper() else c


def _long_date(d: date) -> str:
    return f'{d:%B} {d.day}, {d.year}'


# ── Firm → event ────────────────────────────────────────────────────────────
def classify_firm(firm: dict, now: datetime, window_days: int,
                  include_small: bool) -> dict:
    """Decide ONE firm. Returns the firm dict extended with the parsed
    facts (reg_date ISO, state code, estimate, basis, segment) and a
    `reason` — '' when the firm is a candidate, else one of REASONS."""
    out = dict(firm)
    today = now.date()
    reg = parse_reg_date(firm.get('reg_date'))
    out['reg_date_iso'] = reg.isoformat() if reg else None
    if reg is None:
        out['reason'] = 'bad_reg_date'
        return out
    if not (today - timedelta(days=window_days) <= reg <= today):
        out['reason'] = 'outside_window'
        return out
    if (firm.get('firm_type') or '').strip() != FIRM_TYPE:
        out['reason'] = 'not_registered'
        return out
    code = territory_code(firm.get('state'), firm.get('country'))
    out['state_code'] = code
    if not code:
        out['reason'] = 'out_of_territory'
        return out
    est, basis = revenue_estimate(firm.get('raum_usd'), firm.get('total_employees'))
    # M1 / P3: the ONE rule enrichment's registry applies (oracles.ria_revenue_band):
    # the smaller proxy places the segment through the margin bands, only the
    # larger proxy may say too_small, and a smaller proxy inside/under the
    # small band while the larger clears it is UNKNOWN (segment None).
    raum, emp = _num(firm.get('raum_usd')), _num(firm.get('total_employees'))
    band = ria_revenue_band(int(raum) if raum else None, int(emp) if emp else None)
    out['estimate'] = est
    out['basis'] = basis
    out['segment'] = band['segment']
    if est is None:
        out['reason'] = '' if include_small else 'size_unknown'
        return out
    if band['too_small']:
        out['reason'] = '' if include_small else 'too_small'
        return out
    if band['segment'] is None and not include_small:
        # Proxies disagree (e.g. 4 staff but $792M RAUM) or the estimate sits
        # inside a margin band: the registry calls that UNKNOWN and lets ONE
        # search decide for an account that already has a trigger. For a
        # trigger we MANUFACTURE, both proxies must clear the floor — the
        # M1/P3 rule alone lifted this month's emission from 4 to 40 firms,
        # most of them 3-5-person shops (review 2026-09-08).
        out['reason'] = 'size_borderline'
        return out
    if est > ENTERPRISE_CEIL:
        out['reason'] = 'enterprise'
        return out
    out['reason'] = ''
    return out


def build_description(firm: dict) -> str:
    """Plain-language paragraph. The leading '(ST)' is deliberate: for any
    sec.gov URL enrichment seeds the account's HQ from the first '(XX)' in
    the description (enrichment_scout._structured_seeds), so the state code
    must be the first parenthesised pair. Never contains 'SIC:' or 'Form D'
    (the structured-verdict parser keys on those)."""
    reg = date.fromisoformat(firm['reg_date_iso'])
    name = (firm.get('business_name') or '').strip()
    legal = (firm.get('legal_name') or '').strip()
    code = firm['state_code']
    ftype = (firm.get('firm_type') or FIRM_TYPE).strip()
    raum = _num(firm.get('raum_usd'))
    emp = _num(firm.get('total_employees'))
    est = firm.get('estimate')
    seg = firm.get('segment')

    bits = [f'{name} ({code}) registered with the SEC as an investment adviser on '
            f'{_long_date(reg)}']
    ids = []
    if (firm.get('sec_number') or '').strip():
        ids.append(f'SEC file no. {str(firm["sec_number"]).strip()}')
    if firm.get('crd') is not None:
        ids.append(f'CRD {firm["crd"]}')
    bits[0] += (f' ({"; ".join(ids)}).' if ids else '.')
    if legal:
        bits.append(f'Legal name: {legal}.')
    bits.append(f'Regulatory assets under management: '
                f'{("$" + format(int(raum), ",")) if raum is not None else "not reported"}.')
    bits.append(f'Employees: {int(emp) if emp is not None else "not reported"}.')
    if (firm.get('website') or '').strip():
        bits.append(f'Website: {str(firm["website"]).strip()}.')
    if est is not None:
        bits.append(f'Estimated revenue about {fmt_money(est)} (basis: {firm["basis"]})'
                    + (f' — NetSuite segment {seg}.' if seg else '.'))
    else:
        bits.append(f'Revenue could not be estimated ({firm["basis"]}).')
    bits.append('A new SEC registration means the firm either just formed or crossed '
                '$100M in regulatory assets — a growth milestone when new systems get bought.')
    bits.append(f'Structured facts: hq_state {code}; registration {firm["reg_date_iso"]}; '
                f'firm type {ftype}.')
    return ' '.join(bits)[:DESCRIPTION_MAX_CHARS]


def build_event(firm: dict, now: datetime, seed_columns: Iterable[str] = SEED_COLUMNS) -> dict:
    """The Supabase upsert row for one classified firm. Every row carries
    the SAME key set (PostgREST bulk upserts require it): the nine
    scrape-owned columns + the present seed columns."""
    name = (firm.get('business_name') or '').strip()
    code = firm['state_code']
    url = SOURCE_URL.format(crd=firm['crd'])
    title = (f'New SEC-registered investment adviser: {name} '
             f'({_display_city(firm.get("city"))}, {code})')
    published = f'{firm["reg_date_iso"]}T00:00:00+00:00'
    discovered = now.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    row = {
        'id': generate_event_id(url, title),
        'title': title,
        'company_name': name,
        'event_type': EVENT_TYPE,
        'description': build_description(firm),
        'source_url': url,
        'published_date': published,
        'discovered_at': discovered,
        'matched_regions': json.dumps([code]),   # SEC scraper convention: the code
    }
    seeds = {
        'source': SOURCE,
        'hq_state': code,
        'zi_subindustry': ZI_SUBINDUSTRY.get((firm.get('firm_type') or FIRM_TYPE).strip()),
        'revenue_segment': firm.get('segment'),
        'expires_at': expires_at_for(EVENT_TYPE, published, discovered),
    }
    for col in seed_columns:
        row[col] = seeds[col]
    assert tuple(row)[:len(SCRAPE_OWNED_COLUMNS)] == SCRAPE_OWNED_COLUMNS
    assert not (set(row) & NEVER_SYNC_COLUMNS), 'never write rep/enrichment columns'
    return row


# ── oracles.db access ───────────────────────────────────────────────────────
def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def load_firms(conn: sqlite3.Connection) -> List[dict]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f'''
        SELECT crd, business_name, legal_name, city, state, country, firm_type,
               reg_status, reg_date, website, total_employees, raum_usd, sec_number, as_of
        FROM {FIRM_TABLE}''')
    return [dict(r) for r in cur.fetchall()]


def oracle_meta_line(conn: sqlite3.Connection) -> str:
    """One line describing the feed behind ria_firm (tolerant of an absent
    oracle_meta or a source key spelled differently by the refresh)."""
    if not _table_exists(conn, META_TABLE):
        return 'oracle_meta: (table absent)'
    try:
        rows = conn.execute(f'SELECT source, refreshed_at, rows FROM {META_TABLE}').fetchall()
    except sqlite3.OperationalError as e:
        return f'oracle_meta: unreadable ({e})'
    rows = [tuple(r) for r in rows]
    picked = [r for r in rows if any(k in str(r[0]).lower() for k in ('ria', 'iapd', 'adviser'))] or rows
    if not picked:
        return 'oracle_meta: (empty)'
    return 'oracle_meta: ' + '; '.join(f'{s} refreshed {ts} ({n} rows)' for s, ts, n in picked)


def ensure_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
            crd INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            emitted_at TEXT NOT NULL,
            reg_date TEXT
        )''')
    conn.commit()


def load_ledger(conn: sqlite3.Connection) -> Dict[int, dict]:
    """crd → {event_id, emitted_at, reg_date}. Empty when the ledger table
    has never been created (a dry run never creates it)."""
    if not _table_exists(conn, LEDGER_TABLE):
        return {}
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f'SELECT crd, event_id, emitted_at, reg_date FROM {LEDGER_TABLE}')
    return {int(r['crd']): dict(r) for r in cur.fetchall()}


def write_ledger(conn: sqlite3.Connection, firms: Iterable[dict], now: datetime) -> int:
    """Record (crd → event_id, reg_date). INSERT OR REPLACE so a
    re-registration (new reg_date) replaces the old row."""
    ensure_ledger(conn)
    stamp = now.astimezone(timezone.utc).isoformat()
    rows = [(int(f['crd']), f['event_id'], stamp, f['reg_date_iso']) for f in firms]
    conn.executemany(f'INSERT OR REPLACE INTO {LEDGER_TABLE} (crd, event_id, emitted_at, reg_date) '
                     f'VALUES (?, ?, ?, ?)', rows)
    conn.commit()
    return len(rows)


# ── Supabase ────────────────────────────────────────────────────────────────
def get_client(required: bool):
    """Service-role client from .env, or None when credentials are absent
    (a dry run then simply skips the remote dedup read)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, '.env'))
    except ImportError:
        pass
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ.get('SUPABASE_KEY')
    if not url or not key:
        if required:
            raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) '
                               'are required for --apply')
        return None
    from supabase import create_client
    return create_client(url, key)


def chunked(items: List, size: int) -> Iterable[List]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def existing_event_ids(client, ids: List[str]) -> set:
    """Which of `ids` are already in Supabase — one select per batch."""
    found = set()
    for batch in chunked(list(ids), BATCH_SIZE):
        resp = client.table('events').select('id').in_('id', batch).execute()
        found.update(str(r['id']) for r in (resp.data or []))
    return found


def upsert_events(client, rows: List[dict]) -> None:
    """Batches of 50, on_conflict='id', ignore_duplicates=True — an id that
    already exists is left exactly as it is (never overwrites enrichment or
    rep columns). Raises on the first rejected batch."""
    for batch in chunked(rows, BATCH_SIZE):
        client.table('events').upsert(batch, on_conflict='id', ignore_duplicates=True).execute()


# ── Reporting ───────────────────────────────────────────────────────────────
def _print_table(firms: List[dict], status_of) -> None:
    hdr = (f"{'crd':>8}  {'name':<38} {'city/state':<24} {'reg_date':<10} "
           f"{'RAUM':>7} {'emp':>5} {'est rev':>8} {'segment':<10} status")
    print(hdr)
    print('-' * len(hdr))
    for f in firms:
        loc = f'{_display_city(f.get("city"))}, {f.get("state_code") or f.get("state") or "?"}'
        emp = _num(f.get('total_employees'))
        print(f"{f['crd']:>8}  {(f.get('business_name') or '')[:38]:<38} {loc[:24]:<24} "
              f"{f.get('reg_date_iso') or '?':<10} {fmt_money(f.get('raum_usd')):>7} "
              f"{(str(int(emp)) if emp is not None else 'n/a'):>5} {fmt_money(f.get('estimate')):>8} "
              f"{(f.get('segment') or 'unknown'):<10} {status_of(f)}")


def _summary(counter: Counter, in_scope: int) -> str:
    parts = [f'{k}={counter[k]}' for k in REASONS if counter.get(k)]
    return f'scanned={in_scope}' + (' · ' + ' · '.join(parts) if parts else '')


# ── Orchestration ───────────────────────────────────────────────────────────
def run(db_path: str = DEFAULT_DB, client=None, *, apply: bool = False,
        window_days: int = WINDOW_DAYS, include_small: bool = False,
        limit: Optional[int] = None, now: Optional[datetime] = None) -> int:
    """Select → build → dedup → (apply: upsert + ledger). Returns the exit
    code. `client` is injectable (tests); None means no Supabase access —
    fine for a dry run (remote dedup skipped), fatal for --apply."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    mode = 'APPLY' if apply else 'DRY RUN'
    print(f'RIA trigger ({SOURCE}) — {mode} — {now.isoformat(timespec="seconds")}')

    if not os.path.exists(db_path):
        print(f'Oracle database not found at {db_path} — nothing to do (run '
              f'scripts/refresh_oracles.py first). Exit 0.')
        return 0
    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, FIRM_TABLE):
            print(f'Table {FIRM_TABLE} is not in {db_path} yet — nothing to do (run '
                  f'scripts/refresh_oracles.py --source all first). Exit 0.')
            return 0
        print(oracle_meta_line(conn))
        try:
            firms = load_firms(conn)
        except sqlite3.OperationalError as e:
            print(f'ERROR: {FIRM_TABLE} does not match the oracle contract: {e}')
            return 1
        if apply and client is None:
            print('ERROR: --apply needs a Supabase client (credentials in .env); nothing written.')
            return 1

        since = (now.date() - timedelta(days=window_days)).isoformat()
        print(f'Window: registrations {since}..{now.date().isoformat()} ({window_days} days); '
              f'territory = {len(TERRITORY_STATES)} states/provinces; band = '
              f'{fmt_money(REVENUE_BAR)}..{fmt_money(ENTERPRISE_ABOVE)} revenue proxy '
              f'(±30% margin: too small below {fmt_money(TOO_SMALL_FLOOR)} on the larger proxy, '
              f'Enterprise above {fmt_money(ENTERPRISE_CEIL)})'
              f'{" (--include-small: sub-bar and size-unknown firms included)" if include_small else ""}')

        counter: Counter = Counter()
        candidates: List[dict] = []
        for firm in firms:
            c = classify_firm(firm, now, window_days, include_small)
            if c['reason']:
                counter[c['reason']] += 1
            else:
                candidates.append(c)

        # Local ledger: at most one emission per (crd, reg_date).
        ledger = load_ledger(conn)
        fresh = []
        for c in candidates:
            prior = ledger.get(int(c['crd']))
            if prior and (prior.get('reg_date') or '') == c['reg_date_iso']:
                counter['already_emitted'] += 1
                c['status'] = 'skip: in ledger'
            else:
                fresh.append(c)
        for c in fresh:
            c['event'] = build_event(c, now, SEED_COLUMNS)
            c['event_id'] = c['event']['id']

        # Remote dedup: ids already in Supabase (read-only; one select per batch).
        seen_remote = set()
        if fresh and client is not None:
            try:
                seen_remote = existing_event_ids(client, [c['event_id'] for c in fresh])
            except Exception as e:                       # noqa: BLE001
                if apply:
                    print(f'ERROR: Supabase dedup read failed, nothing written: {e}')
                    return 1
                print(f'WARN: Supabase dedup read failed ({e}) — dry run continues without it')
        elif fresh and client is None:
            print('Note: no Supabase credentials — remote dedup skipped for this dry run')

        to_emit, already_remote = [], []
        for c in fresh:
            if c['event_id'] in seen_remote:
                counter['already_in_supabase'] += 1
                c['status'] = 'skip: already in Supabase'
                already_remote.append(c)
            else:
                to_emit.append(c)
        to_emit.sort(key=lambda c: (c['reg_date_iso'], c.get('estimate') or 0), reverse=True)
        if limit is not None and limit >= 0 and len(to_emit) > limit:
            for c in to_emit[limit:]:
                c['status'] = f'skip: over --limit {limit}'
            deferred = to_emit[limit:]
            to_emit = to_emit[:limit]
        else:
            deferred = []

        # Seed columns: probe once per run so a batch never names a column
        # PostgREST would reject (typed.probe_columns memoises per process).
        seed_cols = SEED_COLUMNS
        if client is not None and to_emit:
            try:
                present = probe_columns(client, 'events', SEED_COLUMNS)
            except Exception:                            # noqa: BLE001
                present = set()
            seed_cols = tuple(c for c in SEED_COLUMNS if c in present)
            missing = [c for c in SEED_COLUMNS if c not in present]
            if missing:
                print(f'WARN: typed columns not live, not sent: {", ".join(missing)}')
                for c in to_emit:
                    c['event'] = build_event(c, now, seed_cols)

        print(f'Selection: {_summary(counter, len(firms))} · to_emit={len(to_emit)}')
        shown = to_emit + deferred + already_remote + [c for c in candidates if c.get('status') == 'skip: in ledger']
        if shown:
            print()
            _print_table(shown, lambda f: f.get('status') or ('emit' if apply else 'would emit'))
            print()

        if not to_emit:
            if apply and already_remote:
                # Rows already in Supabase (ledger lost or never written):
                # record them so the next run does not re-read them.
                n = write_ledger(conn, already_remote, now)
                print(f'Ledger: recorded {n} firm(s) already present in Supabase.')
            print('RIA trigger: nothing to do — 0 new in-band registrations to emit. Exit 0.')
            return 0

        if not apply:
            print(f'DRY RUN — would emit {len(to_emit)} event(s) (source={SOURCE}, '
                  f'event_type={EVENT_TYPE}); nothing written. Re-run with --apply to write.')
            return 0

        emitted = 0
        try:
            for batch in chunked(to_emit, BATCH_SIZE):
                upsert_events(client, [c['event'] for c in batch])
                write_ledger(conn, batch, now)
                emitted += len(batch)
        except Exception as e:                           # noqa: BLE001
            print(f'ERROR: upsert failed after {emitted} row(s): {e}')
            print('Rows already upserted are in the ledger; re-run to continue. Exit 1.')
            return 1
        if already_remote:
            write_ledger(conn, already_remote, now)
        print(f'APPLIED — emitted {emitted} event(s) to Supabase (source={SOURCE}, '
              f'batches of {BATCH_SIZE}, on_conflict=id, ignore_duplicates); '
              f'ledger {LEDGER_TABLE} updated ({emitted + len(already_remote)} row(s)).')
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=DEFAULT_DB, help=f'oracles.db path (default {DEFAULT_DB})')
    ap.add_argument('--window-days', type=int, default=WINDOW_DAYS,
                    help=f'registrations within the last N days (default {WINDOW_DAYS})')
    ap.add_argument('--include-small', action='store_true',
                    help=f'also emit firms whose larger revenue proxy is under '
                         f'{fmt_money(TOO_SMALL_FLOOR)} (the {fmt_money(REVENUE_BAR)} bar less '
                         f'the 30%% estimate margin) and size-unknown firms')
    ap.add_argument('--limit', type=int, default=None, help='emit at most N events')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true', help='print what would be emitted (default)')
    g.add_argument('--apply', action='store_true', help='write the events and the ledger')
    args = ap.parse_args(argv)

    import logging
    import warnings
    warnings.filterwarnings('ignore')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        client = get_client(required=args.apply)
    except Exception as e:                               # noqa: BLE001
        print(f'ERROR: {e}')
        return 1
    return run(args.db, client, apply=args.apply, window_days=args.window_days,
               include_small=args.include_small, limit=args.limit)


if __name__ == '__main__':
    sys.exit(main())
