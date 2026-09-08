"""Accounts layer for TeamAlbert v2 (Phase 4 slice C1, 2026-09-08).

Until now the ACCOUNT — the company a rep would actually sell into — only
existed as a by-product of events: every event row re-derived its own
facts, grades and fit, and a rep's verdict lived in a side table keyed by
a name the dashboard normalized its own way. Phase 4 gives the account a
row of its own (supabase/migrations/003_accounts.sql) that:

  * carries the best-known FACTS about the company (HQ, subindustry,
    vertical, revenue band, size, domain, entity class) — fill-only, and
    only ever overwritten by a stronger provenance;
  * carries ONE grade — the best live trigger's — and the trigger it came
    from, so the dashboard ranks accounts, not events;
  * owns the rep's disposition (status + required reason), which enrichment
    reads as a hard input and which no merge ever touches.

This module is the ONE place that knows the column names, the vocabularies
and the merge rules, so enrichment_scout.py (writer), dashboard.py (reader
+ disposition writer), scripts/backfill_accounts.py (rebuild) and the
monitor agree. Everything decision-shaped is a pure function with `now`
injected; the client helpers are thin and fail-soft.

Contract (A.J. runs the migration by hand, later): every reader/writer
PROBES for the table first and degrades — `upsert_account` is a no-op
returning False, `load_dispositions` falls back to the legacy
`account_dispositions` table — so nothing here can break a run before the
SQL has been applied.

No third-party imports (same rule as typed.py: GitHub Actions 3.11 and the
Mac venv 3.9 both import this).
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from src.pipeline.gates import (
    account_key, hq_state_code, hq_territory_status, is_bad_company_name,
    is_non_operating_entity,
)
from src.pipeline.typed import (
    EXPIRY_DAYS, REVENUE_SEGMENTS, parse_ts, probe_columns, verify_state_for,
)

__all__ = [
    'account_key', 'ACCOUNT_STATUSES', 'REP_NOT_FIT', 'REP_DECIDED',
    'DISPOSITION_REASONS', 'DISPOSITION_REASON_LABELS', 'REASON_REQUIRED_STATUSES',
    'TRIGGER_PRIORITY', 'GRADE_RANK', 'ACCOUNT_COLUMNS', 'ZI_VERTICALS', 'VERTICALS',
    'SIZE_BUCKETS', 'WORKABLE_ROLES', 'PROVENANCE_RANK', 'VERIFY_STATE_RANK',
    'SEEN_EVENT_IDS_MAX', 'LEGACY_REASON_PREFIX',
    'vertical_of', 'size_bucket_of', 'entity_class_of', 'best_trigger_expired',
    'probe_accounts', 'account_columns_present', 'reset_probe_cache',
    'build_account_row', 'merge_account', 'upsert_account', 'touch_secondary', 'list_accounts',
    'page_rows', 'load_account', 'grading_from_event',
    'load_dispositions', 'set_disposition', 'normalize_status', 'normalize_reason',
    'legacy_company_key', 'legacy_key_matches', 'map_legacy_dispositions',
    'encode_legacy_notes', 'decode_legacy_notes', 'is_newer',
]

log = logging.getLogger(__name__)

# ── Vocabularies (THE CONTRACT, Phase 4 2026-09-08 — do not rename) ─────────
# Same five statuses the dashboard has offered since 2026-07-17
# (dashboard.ACCOUNT_STATUSES) and the same two partitions enrichment_scout
# gates on (REP_NOT_FIT_STATUSES / REP_DECIDED_STATUSES). Kept as tuples /
# frozensets here so the three files can be pinned equal by a test.
ACCOUNT_STATUSES = ('Picked Up', 'On Rep TAL', 'NetSuite Customer',
                    'Out of Alignment', 'Not a Fit')
REP_NOT_FIT = frozenset({'Not a Fit', 'Out of Alignment', 'NetSuite Customer'})
REP_DECIDED = frozenset({'Picked Up', 'On Rep TAL'})

# A "no" from a rep has to say WHY (Phase 4): the reason is what lets the
# gates learn (wrong_vertical → the subindustry map; out_of_territory → the
# HQ parser) instead of just hiding the account. Codes are stored; labels
# are what the dashboard shows. Required for Not a Fit / Out of Alignment;
# optional (but welcome) for the rest.
DISPOSITION_REASONS = ('wrong_vertical', 'out_of_territory', 'too_big', 'too_small',
                       'not_a_trigger', 'duplicate', 'existing_customer', 'other')
DISPOSITION_REASON_LABELS = {
    'wrong_vertical': 'Wrong vertical', 'out_of_territory': 'Out of territory',
    'too_big': 'Too big', 'too_small': 'Too small', 'not_a_trigger': 'Not a real trigger',
    'duplicate': 'Duplicate', 'existing_customer': 'Already a customer', 'other': 'Other',
}
REASON_REQUIRED_STATUSES = frozenset({'Not a Fit', 'Out of Alignment'})

# Which trigger an account should be surfaced under when it has several
# (lower index = better). A CFO hire is the strongest NetSuite signal
# (consolidation pain arrives with the new finance leader); an open seat
# is next; M&A / funding are money-in-motion; 'stable_target' is a
# no-trigger account we still know about.
TRIGGER_PRIORITY = ('cfo_hire', 'finance_seat_open', 'merger_acquisition', 'funding',
                    'expansion', 'executive_hire', 'stable_target', 'other')
GRADE_RANK = {'A': 0, 'B': 1, 'C': 2, 'D': 3}       # lower = better; 'Unable to Grade' = no grade

# Mirrors enrichment_scout.WORKABLE_ROLES (a fitting company in one of
# these roles IS an account; previous employer / advisor / investor are
# context). Duplicated because src/pipeline must not import
# enrichment_scout (requests/yaml/dotenv); tests/test_accounts.py pins
# the two equal.
WORKABLE_ROLES = ('acquirer', 'portfolio company', 'hiring company', 'primary', 'target')

# Provenance strength for a firmographic fact (contract order: seed /
# oracle / structured > search > cache > article). `field_sources` on a
# companies_data entry carries one of these per field; a company's
# `classified_by` carries the same vocabulary for its subindustry. Unknown
# provenance (pre-Phase-2 rows never recorded it) ranks with 'article':
# the weakest KNOWN source, so a recorded search can replace it but an
# unrecorded article extraction cannot flap it.
PROVENANCE_RANK = {'seed': 3, 'oracle': 3, 'structured': 3, 'search': 2, 'cache': 1, 'article': 0,
                   # domain_method values (src/pipeline/domains.py) stand in as the
                   # domain's provenance: a Clearbit lookup is a search, a URL
                   # hint taken from the article is an article.
                   'clearbit': 2, 'hint_url': 0}

# How much we KNOW about an account, best first. verified (confirmed in)
# and not_fit (confirmed out) both beat the two unknown states; a later
# event that came back staged / researched_ambiguous (a throttled search,
# a re-enrichment with less context) must never demote a confirmed
# account. 'decided' is a rep verdict, not research — the disposition
# column carries it — so it only ever fills an empty state.
VERIFY_STATE_RANK = {'verified': 4, 'not_fit': 3, 'researched_ambiguous': 2, 'staged': 1, 'decided': 0}
FIT_VERDICT_RANK = {'pass': 4, 'fail': 3, 'unverified': 2, 'staged': 1, 'decided': 0}

# ZoomInfo subindustry → NSCorp vertical LABEL. This is enrichment_scout.
# ZI_SUBINDUSTRIES (A.J.'s "FY27 Territories.xlsx", Subindustries sheet),
# the same map dashboard.vertical_of and monitor_health.vertical_of read —
# replicated here (Phase 4 2026-09-08) because the accounts row stores the
# label and src/pipeline cannot import enrichment_scout. NOTE the events
# table's typed `vertical` column is in|out|unknown (the fit gate's
# dimension); the accounts column is the LABEL. tests/test_accounts.py
# pins this dict equal to enrichment_scout.ZI_SUBINDUSTRIES so the copies
# cannot drift.
ZI_VERTICALS = {
    # ── Financial Services ────────────────────────────────────────────
    'Banking':                                    'Financial Services',
    'Credit Cards & Transaction Processing':      'Financial Services',
    'Debt Collection':                            'Financial Services',
    'Holding Companies & Conglomerates':          'Financial Services',
    'Insurance':                                  'Financial Services',
    'Investment Banking':                         'Financial Services',
    'Lending & Brokerage':                        'Financial Services',
    'Venture Capital & Private Equity':           'Financial Services',
    # ── Nonprofits & Organizations ────────────────────────────────────
    'Blood & Organ Banks':                        'Nonprofits & Organizations',
    'Childcare':                                  'Nonprofits & Organizations',
    'Colleges & Universities':                    'Nonprofits & Organizations',
    'Cultural & Informational Centers':           'Nonprofits & Organizations',
    'K-12 Schools':                               'Nonprofits & Organizations',
    'Libraries':                                  'Nonprofits & Organizations',
    'Membership Organizations':                   'Nonprofits & Organizations',
    'Museums & Art Galleries':                    'Nonprofits & Organizations',
    'Non-Profit & Charitable Organizations':      'Nonprofits & Organizations',
    'Non-Profit Organizations & Charitable Foundations': 'Nonprofits & Organizations',
    'Performing Arts Theaters':                   'Nonprofits & Organizations',
    'Religious Organizations':                    'Nonprofits & Organizations',
    'Training':                                   'Nonprofits & Organizations',
    'Zoos & National Parks':                      'Nonprofits & Organizations',
    # ── Consumer Services ─────────────────────────────────────────────
    'Auctions':                                   'Consumer Services',
    'Automobile Dealers':                         'Consumer Services',
    'Automotive Service & Collision Repair':      'Consumer Services',
    'Barber Shops & Beauty Salons':               'Consumer Services',
    'Cleaning Services':                          'Consumer Services',
    'Consumer Services':                          'Consumer Services',
    'Funeral Homes & Funeral Related Services':   'Consumer Services',
    'Photography Studio':                         'Consumer Services',
    'Real Estate':                                'Consumer Services',
    'Repair Services':                            'Consumer Services',
}
VERTICALS = tuple(dict.fromkeys(ZI_VERTICALS.values()))

# Closed vocabulary for the headcount column. The LLM emits ranges in a
# dozen spellings ('1-50', '11-50', '1,001-5,000', '500 employees'); the
# raw string stays in firmographics.size, the column holds the bucket
# the range's UPPER bound falls in, so the dashboard can filter on it.
SIZE_BUCKETS = ('1-10', '11-50', '51-200', '201-500', '501-1000', '1001-5000',
                '5001-10000', '10000+')
_SIZE_UPPER = ((10, '1-10'), (50, '11-50'), (200, '51-200'), (500, '201-500'),
               (1000, '501-1000'), (5000, '1001-5000'), (10000, '5001-10000'))

# ── Column inventory (mirrors 003_accounts.sql, table order) ────────────────
ACCOUNT_COLUMNS = (
    'account_key', 'canonical_name', 'aliases', 'domain', 'domain_method',
    'hq', 'hq_state', 'in_territory', 'zi_subindustry', 'vertical', 'industry',
    'revenue_segment', 'size_bucket', 'entity_class',
    'fit_verdict', 'verify_state', 'enrich_attempts', 'retry_after',
    'classified_by', 'classification_confidence', 'firmographics',
    'grade', 'numeric_score', 'confidence_level', 'hashtags', 'grade_justification',
    'graded_event_id', 'graded_at',
    'best_trigger_type', 'best_trigger_at', 'best_trigger_event_id', 'event_count', 'seen_event_ids',
    'last_event_at',
    'disposition', 'disposition_reason', 'disposition_notes', 'disposition_at', 'disposition_by',
    'active', 'first_seen', 'last_seen', 'created_at', 'updated_at',
)
DISPOSITION_COLUMNS = ('disposition', 'disposition_reason', 'disposition_notes',
                       'disposition_at', 'disposition_by')
# How many event ids a row remembers (JSONB `seen_event_ids`, newest last).
# review 2026-09-08 (Phase 4): event_count used to infer "new" from the
# graded / best-trigger ids alone, so any re-processed third event was
# counted twice and a tombstoned facts-only row (no trigger id) never.
# Fifty covers every account seen so far (max ~10 events) many times over
# while keeping the column small; an id older than the window is the only
# way to double count, and only on a re-process.
SEEN_EVENT_IDS_MAX = 50
# The legacy account_dispositions table has no reason column: the code
# rides inside `notes` as 'reason=<code> | <notes>' (encode_legacy_notes /
# decode_legacy_notes). review 2026-09-08 (Phase 4): until A.J. runs
# migration 003 that row is the ONLY copy of a rep's verdict, and the
# notes-only write dropped every reason entered before then.
LEGACY_REASON_PREFIX = 'reason='
GRADE_COLUMNS = ('grade', 'numeric_score', 'confidence_level', 'hashtags',
                 'grade_justification', 'graded_event_id', 'graded_at')
TRIGGER_COLUMNS = ('best_trigger_type', 'best_trigger_at', 'best_trigger_event_id')
# Fact columns grouped by the firm field whose provenance decides them: a
# stronger `hq` replaces hq AND the state / territory read from it; a
# stronger subindustry replaces the vertical label and the classifier
# attribution that came with it.
FACT_GROUPS = (
    ('hq', ('hq', 'hq_state', 'in_territory')),
    ('zi_subindustry', ('zi_subindustry', 'vertical', 'classified_by', 'classification_confidence')),
    ('revenue', ('revenue_segment',)),
    ('size', ('size_bucket',)),
    ('domain', ('domain', 'domain_method')),
    ('industry', ('industry',)),
)
# Facts with no provenance story: first value wins (fill-only).
FILL_ONLY_COLUMNS = ('canonical_name', 'entity_class')
# Per-event keys of a companies_data entry that are NOT facts about the
# company and never belong in firmographics (a company is 'target' in one
# event and 'acquirer' in the next; fit / tal are that event's verdicts).
_PER_EVENT_COMPANY_KEYS = frozenset({'name', 'role', 'fit', 'tal', 'deferred'})
_EMPTY_STRINGS = frozenset({'', 'unknown', 'null', 'none', 'n/a', 'nan'})

# Column probe memo. A NEGATIVE answer expires (PROBE_NEGATIVE_TTL_S) so a
# long-lived dashboard process notices the table after A.J. runs the SQL,
# and a transient network blip cannot park it in legacy mode for good; a
# positive answer is final for the process (tables do not disappear).
PROBE_NEGATIVE_TTL_S = 600
_probe = {}      # 'accounts' -> (present: bool, monotonic timestamp)
_columns = {}    # 'accounts' -> set of present columns


# ── Small helpers ───────────────────────────────────────────────────────────
def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    return _as_utc(now) if now is not None else datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return _as_utc(dt).isoformat()


def _clean(v) -> str:
    """str(v).strip(); '' for None / NaN / the LLM's literal 'null'."""
    if v is None or (isinstance(v, float) and v != v):
        return ''
    s = str(v).strip()
    return '' if s.lower() in ('null', 'none', 'nan') else s


def _lower(v) -> str:
    return _clean(v).lower()


def _empty(v) -> bool:
    """A value that carries no fact: None, blank, 'unknown', an empty list/dict."""
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() in _EMPTY_STRINGS
    if isinstance(v, (list, dict, tuple, set)):
        return not v
    if isinstance(v, float) and v != v:
        return True
    return False


def _norm_dim(v) -> Optional[str]:
    """fit territory/vertical → in|out|unknown; 'n/a' (decided / failed rows) → None."""
    s = _lower(v)
    return s if s in ('in', 'out', 'unknown') else None


def _prov(source) -> int:
    return PROVENANCE_RANK.get(_lower(source), PROVENANCE_RANK['article'])


def _priority(trigger_type) -> int:
    t = _lower(trigger_type)
    return TRIGGER_PRIORITY.index(t) if t in TRIGGER_PRIORITY else TRIGGER_PRIORITY.index('other')


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _str_list(v) -> List[str]:
    if isinstance(v, str):
        s = v.strip()
        if s.startswith('['):                        # a JSONB list that arrived as text
            try:
                v = json.loads(s)
            except ValueError:
                v = [s]
        else:
            v = [s]
    if isinstance(v, (dict, set, tuple)):
        v = list(v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def vertical_of(zi) -> Optional[str]:
    """ZoomInfo subindustry → vertical LABEL, or None when the subindustry is
    missing / 'OTHER' / outside the FY27 taxonomy. (dashboard.vertical_of
    returns the display string 'Unknown' for that case; the accounts row
    stores nothing so a later known value can fill it.)"""
    return ZI_VERTICALS.get(_clean(zi)) or None


def size_bucket_of(size) -> Optional[str]:
    """'1-50' → '11-50', '1,001-5,000' → '1001-5000', '10000+' → '10000+',
    '500 employees' → '201-500'. None when no number can be read."""
    s = _clean(size).replace(',', '').lower()
    if not s:
        return None
    nums = [int(n) for n in re.findall(r'\d+', s)]
    if not nums:
        return None
    n = max(nums)
    if '+' in s or 'more' in s or 'over' in s or '>' in s:
        n += 1                       # '5000+' is above the 1001-5000 bucket
    for upper, bucket in _SIZE_UPPER:
        if n <= upper:
            return bucket
    return '10000+'


def entity_class_of(name, descriptor: str = '', registry_source=None) -> str:
    """'operating' or a gates.is_non_operating_entity kind. Carries the ONE
    registry exemption enrichment_scout._entity_shape applies (review
    2026-09-08): an SEC-registered adviser is the management company by
    construction, so its '... LP' / '... Fund' name is not a fund vehicle."""
    nonop, kind = is_non_operating_entity(_clean(name), _clean(descriptor))
    if nonop and kind == 'fund_vehicle' and _lower(registry_source) == 'sec_iapd':
        return 'operating'
    return kind if nonop else 'operating'


def _trigger_expired(trigger_type, trigger_at, now: Optional[datetime]) -> bool:
    at = parse_ts(trigger_at)
    if at is None:
        return False
    days = EXPIRY_DAYS.get(_lower(trigger_type), EXPIRY_DAYS['other'])
    return at + timedelta(days=days) < _now(now)


def best_trigger_expired(row: Optional[dict], now: Optional[datetime] = None) -> bool:
    """True when the account's best trigger is past its shelf life
    (typed.EXPIRY_DAYS from best_trigger_at; graded_at when the trigger date
    is missing). False for an account with no dated trigger — an undated
    trigger is not silently retired."""
    row = row or {}
    at = row.get('best_trigger_at') or row.get('graded_at')
    return _trigger_expired(row.get('best_trigger_type'), at, now)


# ── Vocabulary normalizers ──────────────────────────────────────────────────
def normalize_status(status) -> Optional[str]:
    """Canonical ACCOUNT_STATUSES spelling; None when the value means
    'clear' (falsy, '—'); ValueError for an unknown non-empty string."""
    s = _clean(status)
    if not s or s in ('—', '-', '--'):
        return None
    for st in ACCOUNT_STATUSES:
        if s.lower() == st.lower():
            return st
    raise ValueError(f"unknown account status {status!r} (choose one of: {', '.join(ACCOUNT_STATUSES)})")


def normalize_reason(reason) -> Optional[str]:
    """Reason CODE for a code or its human label ('Wrong vertical' →
    'wrong_vertical'); None for blank; ValueError for anything else."""
    s = _clean(reason)
    if not s:
        return None
    low = s.lower().replace(' ', '_').replace('-', '_')
    if low in DISPOSITION_REASONS:
        return low
    for code, label in DISPOSITION_REASON_LABELS.items():
        if s.lower() == label.lower():
            return code
    raise ValueError(f"unknown disposition reason {reason!r} (choose one of: "
                     f"{', '.join(DISPOSITION_REASON_LABELS.values())})")


# ── Legacy account_dispositions bridge ──────────────────────────────────────
def legacy_company_key(name) -> str:
    """The v1 dashboard's key for account_dispositions.company_key — a
    verbatim mirror of dashboard._legacy_account_key (one suffix stripped,
    commas and hyphens kept), the normalizer that WROTE the rows the table
    holds today. It differs from gates.account_key for names like
    'Agfa-Gevaert' ('agfa-gevaert' vs 'agfa gevaert') and 'SFA, LLC dba …'
    (comma kept vs stripped), which is why legacy rows are mapped by
    company_name, never by key, and why set_disposition deletes the
    v1-spelled row when it writes the pipeline-keyed one."""
    s = str(name or '').strip().lower()
    for suf in (', inc.', ', inc', ' inc.', ' inc', ', llc', ' llc',
                ', ltd.', ' ltd.', ' ltd', ' corp.', ' corp', ' co.', ' company'):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s.strip(' .,')


def legacy_key_matches(name, legacy_key) -> bool:
    """Does a legacy account_dispositions.company_key belong to `name`?
    True when it IS gates.account_key(name), re-normalizes to it, or is the
    v1 dashboard's own key for the name (legacy_company_key)."""
    k = account_key(name)
    lk = _lower(legacy_key)
    if not k or not lk:
        return False
    return lk == k or account_key(lk) == k or lk == legacy_company_key(name)


def encode_legacy_notes(reason, notes) -> Optional[str]:
    """`notes` value for the legacy account_dispositions row: 'reason=<code>
    | <notes>' — the code first, then the rep's free text, or either alone;
    None when both are blank. Lives here (not only in the dashboard's
    module-absent fallback, where it started) because review 2026-09-08
    (Phase 4) found set_disposition's legacy write stored notes only, so a
    reason entered before migration 003 runs was gone by the time the
    backfill looked for it."""
    parts = []
    rs = _clean(reason)
    if rs:
        parts.append(f'{LEGACY_REASON_PREFIX}{rs}')
    ns = _clean(notes)
    if ns:
        parts.append(ns)
    return ' | '.join(parts) or None


def decode_legacy_notes(notes) -> Tuple[Optional[str], Optional[str]]:
    """→ (reason code or None, notes or None): the inverse of
    encode_legacy_notes. Only the FIRST ' | ' after the prefix separates
    code from text, so a note that itself contains ' | ' survives intact;
    a value without the prefix (every note written before Phase 4) comes
    back untouched as notes. No vocabulary check here — the caller decides
    what an unknown code means (map_legacy_dispositions keeps the text)."""
    s = _clean(notes)
    if not s:
        return None, None
    if not s.startswith(LEGACY_REASON_PREFIX):
        return None, s
    head, _sep, rest = s[len(LEGACY_REASON_PREFIX):].partition(' | ')
    return (head.strip() or None), (rest.strip() or None)


def map_legacy_dispositions(rows: Optional[Iterable[dict]]) -> List[dict]:
    """account_dispositions rows → [{'account_key', 'legacy_key', 'name',
    'status', 'reason', 'notes', 'at', 'match', 'problem'}]. The account_key
    is derived from company_name (the dashboard's key may not equal
    gates.account_key); 'reason' is the code decoded out of notes
    (decode_legacy_notes — an unknown code is not a reason: the text stays
    in notes whole); 'match' says how the legacy key related ('exact' |
    'renormalized' | 'name' | 'legacy_key' — no name, the key itself
    re-normalized) or 'unmatched' (account_key None, 'problem' says why).
    Callers report the unmatched ones rather than dropping them silently."""
    out = []
    for r in rows or []:
        r = r or {}
        name = _clean(r.get('company_name'))
        lk = _lower(r.get('company_key'))
        raw_notes = _clean(r.get('notes'))
        reason, notes = decode_legacy_notes(raw_notes)
        if reason:
            try:
                reason = normalize_reason(reason)
            except ValueError:
                reason, notes = None, raw_notes            # not a code we know: keep the text whole
        entry = {'account_key': None, 'legacy_key': lk, 'name': name or None,
                 'status': None, 'reason': reason, 'notes': notes or None,
                 'at': r.get('updated_at'), 'match': 'unmatched', 'problem': None}
        try:
            entry['status'] = normalize_status(r.get('status'))
        except ValueError as e:
            entry['problem'] = str(e)
            out.append(entry)
            continue
        if not entry['status']:
            entry['problem'] = 'blank status'
        elif name and account_key(name):
            k = account_key(name)
            entry['account_key'] = k
            entry['match'] = ('exact' if lk == k
                              else 'renormalized' if account_key(lk) == k
                              else 'name')
        elif lk and account_key(lk):
            entry['account_key'] = account_key(lk)
            entry['name'] = entry['name'] or lk
            entry['match'] = 'legacy_key'
        else:
            entry['problem'] = 'no company_name and no usable company_key'
        out.append(entry)
    return out


# ── Column probing ──────────────────────────────────────────────────────────
def reset_probe_cache() -> None:
    _probe.clear()
    _columns.clear()


def probe_accounts(client) -> bool:
    """Does the accounts table exist? One cheap select, memoized for the
    process (negative answers expire, see PROBE_NEGATIVE_TTL_S). Any
    exception — missing table, no client, network — is False: callers then
    stay on the legacy path rather than crash a run."""
    hit = _probe.get('accounts')
    if hit is not None:
        present, at = hit
        if present or (time.monotonic() - at) < PROBE_NEGATIVE_TTL_S:
            return present
    present = False
    if client is not None:
        try:
            client.table('accounts').select('account_key').limit(1).execute()
            present = True
        except Exception:
            present = False
    _probe['accounts'] = (present, time.monotonic())
    return present


def account_columns_present(client) -> set:
    """Columns of ACCOUNT_COLUMNS that exist. Fast path: one select naming
    every column (the migration creates them all at once); if that fails
    (a partial table), fall back to typed.probe_columns one by one. Empty
    set when the table is absent."""
    if 'accounts' in _columns:
        return set(_columns['accounts'])
    if not probe_accounts(client):
        return set()
    try:
        client.table('accounts').select(','.join(ACCOUNT_COLUMNS)).limit(1).execute()
        cols = set(ACCOUNT_COLUMNS)
    except Exception:
        cols = set(probe_columns(client, 'accounts', ACCOUNT_COLUMNS))
    _columns['accounts'] = cols
    return set(cols)


# ── Row building ────────────────────────────────────────────────────────────
def _norm_grading(g) -> dict:
    """One grade shape from the three that exist: enrichment's grading dict
    ('confidence', 'numeric_score', 'grade_justification'), the per-company
    `tal` dict ('score', 'justification') and the event's own columns
    ('confidence_level')."""
    g = g if isinstance(g, dict) else {}
    gs = _clean(g.get('grade')).upper()
    grade = gs if gs in GRADE_RANK else None             # 'Unable to Grade' is not a grade
    score = g.get('numeric_score', g.get('score'))
    return {
        'grade': grade,
        'numeric_score': _int(score, None) if score not in (None, '') else None,
        'confidence_level': _clean(g.get('confidence_level') or g.get('confidence')).title() or None,
        'hashtags': _str_list(g.get('hashtags')),
        'grade_justification': _clean(g.get('grade_justification') or g.get('justification')) or None,
    }


def grading_from_event(event: dict) -> dict:
    """The grade an events row already carries, in build_account_row's
    `grading` shape — for the backfill, which has no live grading dict.
    Live callers must NOT use this: an event row read before enrichment
    may hold the grade of a previous run."""
    event = event or {}
    return {'grade': event.get('grade'), 'numeric_score': event.get('numeric_score'),
            'confidence_level': event.get('confidence_level'),
            'hashtags': event.get('hashtags'),
            'grade_justification': event.get('grade_justification')}


def _firmographics_of(company: dict) -> dict:
    """The company dict minus its per-event keys and empty values — what
    the account remembers about the firm (url, linkedin, size, revenue,
    revenue_source, hq, industry, zi_subindustry, field_sources,
    registry_source, domain, classified_by, too_small...)."""
    out = {}
    for k, v in (company or {}).items():
        if k in _PER_EVENT_COMPANY_KEYS or _empty(v):
            continue
        out[k] = copy.deepcopy(v)
    srcs = out.get('field_sources')
    if isinstance(srcs, dict):
        srcs = {f: s for f, s in srcs.items() if not _empty(out.get(f)) and _clean(s)}
        if srcs:
            out['field_sources'] = srcs
        else:
            out.pop('field_sources', None)
    elif srcs is not None:
        out.pop('field_sources', None)
    return out


_RESEARCHED_SOURCES = ('seed', 'oracle', 'structured', 'search', 'cache')


def _stamp_researched(firm: dict, company: dict, event: dict) -> dict:
    """Give the firmographics a `researched_at` when the company's facts came
    from a researched source (never 'article'/'account'), using the event's
    enriched_at. Enrichment stamps `now` on its own writes; the backfill has
    only the historical stamp — without it every backfilled account would be
    researched once more (review 2026-09-08 (Phase 4))."""
    if not isinstance(firm, dict) or firm.get('researched_at'):
        return firm
    srcs = company.get('field_sources') if isinstance(company.get('field_sources'), dict) else {}
    provenance = set(str(v) for v in srcs.values()) | {str(company.get('classified_by') or '')}
    if not (provenance & set(_RESEARCHED_SOURCES)):
        return firm
    stamp = _clean(event.get('enriched_at'))
    if stamp:
        firm = dict(firm)
        firm['researched_at'] = stamp
    return firm


def _field_sources(row: dict) -> dict:
    """Provenance per firm field for an account row: firmographics.
    field_sources, with classified_by standing in for the subindustry when
    the LLM classification recorded no source of its own."""
    row = row or {}
    firm = row.get('firmographics') if isinstance(row.get('firmographics'), dict) else {}
    srcs = dict(firm.get('field_sources') or {}) if isinstance(firm.get('field_sources'), dict) else {}
    by = _lower(row.get('classified_by') or firm.get('classified_by'))
    if by and not srcs.get('zi_subindustry'):
        srcs['zi_subindustry'] = by
    method = _lower(row.get('domain_method') or firm.get('domain_method'))
    if method and not srcs.get('domain'):
        srcs['domain'] = method
    return srcs


def build_account_row(event: dict, company: dict, fit: Optional[dict], grading=None,
                      now: Optional[datetime] = None, *, trigger_live: bool = True) -> dict:
    """The accounts row ONE event contributes for ONE company.

    `event`  — the events row (typed columns welcome; the JSON is not read).
    `company`— the companies_data entry (name, role, hq, zi_subindustry,
               industry, size, revenue, url, linkedin, domain, domain_method,
               classified_by, classification_confidence, field_sources,
               registry_source, fit, too_small). Its own `fit` wins over the
               event-level `fit` for the verdict / dimensions, so a
               secondary company is judged on ITS fit, not the account's.
    `fit`    — the event's fit dict (verdict, territory, revenue, vertical,
               zi_subindustry, account_name).
    `grading`— {'grade','numeric_score','confidence','hashtags',
               'grade_justification'} (enrichment's dict; the `tal` shape
               and the event-column shape are accepted too). None or {} =
               NO grade — facts and trigger only (a staged event, a failed
               grading, touch_secondary). The event's own grade columns are
               never read implicitly: an event row fetched before
               enrichment may carry a previous run's grade. The backfill
               passes grading_from_event(event) on purpose.
    `trigger_live` — False when the event is tombstoned or expired: the
               account still exists (name, facts, counts, seen dates) but
               the event is neither its best trigger nor its grade.

    FILL-ONLY: a fact whose value is unknown is simply absent from the
    dict, never None — merge_account / the upsert must never null a
    learned fact. Only set_disposition ever writes None (to clear).
    Event-level typed fallbacks (hq_state, zi_subindustry, revenue_segment,
    classified_by...) are used ONLY when this company IS the event's
    account: those columns describe the chosen account, not a secondary.
    """
    event = event or {}
    company = company or {}
    fit = fit if isinstance(fit, dict) else {}
    now_dt = _now(now)

    name = _clean(company.get('name')) or _clean(fit.get('account_name')) or _clean(event.get('company_name'))
    key = account_key(name)
    if not key:
        raise ValueError('build_account_row: no company name to key the account on')
    is_event_account = account_key(_clean(fit.get('account_name')) or _clean(event.get('company_name'))) == key

    cfit = company.get('fit') if isinstance(company.get('fit'), dict) else {}
    verdict = _lower(cfit.get('verdict')) or _lower(fit.get('verdict')) or None
    hq = _clean(company.get('hq')) or None
    hq_state = hq_state_code(hq) or (_clean(event.get('hq_state')).upper() if is_event_account else '') or None
    in_territory = (_norm_dim(cfit.get('territory')) or _norm_dim(fit.get('territory') if is_event_account else None)
                    or (hq_territory_status(hq) if hq else None)
                    or (_norm_dim(event.get('in_territory')) if is_event_account else None))
    zi = (_clean(company.get('zi_subindustry'))
          or (is_event_account and (_clean(fit.get('zi_subindustry')) or _clean(event.get('zi_subindustry'))))
          or None)
    revenue = _clean(company.get('revenue'))
    revenue_segment = (revenue if revenue in REVENUE_SEGMENTS
                       else (event.get('revenue_segment') if is_event_account
                             and event.get('revenue_segment') in REVENUE_SEGMENTS else None))
    classified_by = _lower(company.get('classified_by')) or (is_event_account and _lower(event.get('classified_by'))) or None
    confidence = (_clean(company.get('classification_confidence'))
                  or (is_event_account and _clean(event.get('classification_confidence'))) or None)
    domain = _lower(company.get('domain')) or None

    aliases = []
    for alt in (_clean(event.get('company_name')), _clean(fit.get('account_name'))):
        if alt and alt != name and account_key(alt) == key and not is_bad_company_name(alt) and alt not in aliases:
            aliases.append(alt)

    discovered = parse_ts(event.get('discovered_at')) or now_dt
    trigger_at = parse_ts(event.get('published_date')) or discovered

    row = {
        'account_key': key,
        'canonical_name': name[:200],
        'aliases': aliases,
        'domain': domain,
        'domain_method': (_lower(company.get('domain_method')) or None) if domain else None,
        'hq': hq,
        'hq_state': hq_state,
        'in_territory': in_territory,
        'zi_subindustry': zi,
        'vertical': vertical_of(zi),
        'industry': _clean(company.get('industry')) or None,
        'revenue_segment': revenue_segment,
        'size_bucket': size_bucket_of(company.get('size')),
        'entity_class': entity_class_of(name, company.get('descriptor') or company.get('industry') or '',
                                        company.get('registry_source')),
        'fit_verdict': verdict,
        'verify_state': verify_state_for(verdict),
        'enrich_attempts': _int(event.get('enrich_attempts')) if is_event_account else 0,
        'retry_after': (_clean(event.get('retry_after')) or None) if is_event_account else None,
        'classified_by': classified_by,
        'classification_confidence': confidence,
        'firmographics': _stamp_researched(_firmographics_of(company), company, event),
        'event_count': 1,
        # The id this row folds in, live or tombstoned — merge_account counts
        # an id once, ever (review 2026-09-08 (Phase 4)).
        'seen_event_ids': [_clean(event.get('id'))] if _clean(event.get('id')) else None,
        'last_event_at': _iso(trigger_at),
        'active': True,
        'first_seen': _iso(discovered),
        'last_seen': _iso(discovered),
    }
    if trigger_live and _clean(event.get('id')):
        etype = _lower(event.get('event_type'))
        row['best_trigger_type'] = etype if etype in TRIGGER_PRIORITY else 'other'
        row['best_trigger_at'] = _iso(trigger_at)
        row['best_trigger_event_id'] = _clean(event.get('id'))
    if trigger_live and grading:
        g = _norm_grading(grading)
        if g['grade']:
            row.update({'grade': g['grade'], 'numeric_score': g['numeric_score'],
                        'confidence_level': g['confidence_level'], 'hashtags': g['hashtags'],
                        'grade_justification': g['grade_justification'],
                        'graded_event_id': _clean(event.get('id')) or None,
                        'graded_at': _iso(parse_ts(event.get('enriched_at')) or now_dt)})
    return {k: v for k, v in row.items() if v is not None}


# ── Merge ───────────────────────────────────────────────────────────────────
def _merge_firmographics(existing, incoming) -> Tuple[dict, bool]:
    ex = dict(existing) if isinstance(existing, dict) else {}
    inc = dict(incoming) if isinstance(incoming, dict) else {}
    ex_src = dict(ex.get('field_sources') or {}) if isinstance(ex.get('field_sources'), dict) else {}
    inc_src = dict(inc.get('field_sources') or {}) if isinstance(inc.get('field_sources'), dict) else {}
    out, out_src, changed = dict(ex), dict(ex_src), False
    for k, v in inc.items():
        if k == 'field_sources' or _empty(v):
            continue
        cur = out.get(k)
        if k == 'researched_at':
            # A timestamp, not a fact: enrich-once (enrichment_scout) reads
            # ONLY this stamp for the 90-day freshness rule, so a re-research
            # must always move it forward (review 2026-09-08 (Phase 4)).
            later = _later(cur, v)
            if later != cur:
                out[k] = later
                changed = True
            continue
        if _empty(cur) or (v != cur and _prov(inc_src.get(k)) > _prov(ex_src.get(k))):
            if v != cur:
                out[k] = copy.deepcopy(v)
                changed = True
            if inc_src.get(k):
                out_src[k] = inc_src[k]
            else:
                out_src.pop(k, None)             # the value changed; its old label no longer applies
        elif v == cur and inc_src.get(k) and not ex_src.get(k):
            out_src[k] = inc_src[k]              # same value, now with a recorded source
    if out_src != ex_src:
        changed = True
    if out_src:
        out['field_sources'] = out_src
    else:
        out.pop('field_sources', None)
    return out, changed


def _merge_aliases(existing: dict, incoming: dict) -> List[str]:
    canon = _clean(existing.get('canonical_name')) or _clean(incoming.get('canonical_name'))
    seen = []
    for alt in (_str_list(existing.get('aliases')) + [_clean(incoming.get('canonical_name'))]
                + _str_list(incoming.get('aliases'))):
        if alt and alt != canon and alt not in seen:
            seen.append(alt)
    return seen


def _later(a, b) -> Optional[str]:
    """ISO of the later of two timestamps (either may be missing)."""
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None and tb is None:
        return None
    if ta is None or (tb is not None and tb > ta):
        return _iso(tb)
    return _iso(ta)


def _earlier(a, b) -> Optional[str]:
    ta, tb = parse_ts(a), parse_ts(b)
    if ta is None and tb is None:
        return None
    if ta is None or (tb is not None and tb < ta):
        return _iso(tb)
    return _iso(ta)


def merge_account(existing: Optional[dict], incoming: dict, now: Optional[datetime] = None,
                  is_new_event: Optional[bool] = None) -> dict:
    """The fields to WRITE when `incoming` (a build_account_row dict) meets
    the stored `existing` row. Returns account_key + only what changed +
    updated_at; the whole incoming row (minus dispositions) when there is
    no existing row.

    Rules (Phase 4 2026-09-08):
      facts     fill-only; an incoming value replaces a stored one only when
                its provenance is stronger (PROVENANCE_RANK, read from
                firmographics.field_sources / classified_by). Stored
                'unknown' counts as empty.
      state     verify_state / fit_verdict move only UP VERIFY_STATE_RANK —
                a verified account is never demoted because a later event
                came back staged; retry_after / enrich_attempts follow the
                winning side.
      grade     incoming replaces when the stored grade is empty, the
                incoming grade ranks better, the SAME event is being
                re-graded, or the stored best trigger has expired
                (typed.EXPIRY_DAYS from best_trigger_at). graded_event_id /
                graded_at travel with it.
      trigger   incoming becomes best_trigger when it is higher
                TRIGGER_PRIORITY, or same priority and newer; an expired
                stored trigger yields to any live one, and an expired
                incoming never displaces a live one. The same event id
                refreshes in place.
      counts    event_count counts each event id ONCE, ever: the row keeps
                the ids it has folded in (`seen_event_ids`, newest last,
                bounded to SEEN_EVENT_IDS_MAX; the graded / best-trigger
                ids count as seen too, for rows written before the column).
                An incoming id already seen adds nothing — a re-processed
                event, live or tombstoned, never double counts (review
                2026-09-08 (Phase 4): the old "new = neither the graded nor
                the best-trigger id" rule double-counted every re-processed
                third event and never counted a tombstoned facts-only row).
                `is_new_event` overrides: True counts (at least) one, False
                counts nothing; the ids are recorded either way.
                last_event_at / last_seen take the max, first_seen the min.
      dispo     NEVER touched (rep-owned): any disposition key on
                `incoming` is dropped. `active` and created_at likewise.
    """
    inc = {k: v for k, v in (incoming or {}).items()
           if k not in DISPOSITION_COLUMNS and k not in ('created_at', 'updated_at', 'active')}
    now_iso = _iso(_now(now))
    if not existing or not existing.get('account_key'):
        out = {k: v for k, v in inc.items() if v is not None}
        if 'active' in (incoming or {}):
            out['active'] = bool(incoming['active'])
        out['updated_at'] = now_iso
        return out

    ex = existing
    out = {'account_key': ex.get('account_key')}

    # ── facts, by provenance group ──
    ex_src, inc_src = _field_sources(ex), _field_sources(inc)
    for pkey, cols in FACT_GROUPS:
        stronger = _prov(inc_src.get(pkey)) > _prov(ex_src.get(pkey))
        for col in cols:
            iv = inc.get(col)
            if _empty(iv):
                continue
            ev = ex.get(col)
            if _empty(ev) or (iv != ev and stronger):
                out[col] = iv
    for col in FILL_ONLY_COLUMNS:
        if not _empty(inc.get(col)) and _empty(ex.get(col)):
            out[col] = inc[col]
    firm, changed = _merge_firmographics(ex.get('firmographics'), inc.get('firmographics'))
    if changed:
        out['firmographics'] = firm
    aliases = _merge_aliases(ex, inc)
    if aliases != _str_list(ex.get('aliases')):
        out['aliases'] = aliases

    # ── verify_state / fit_verdict ──
    ex_vs, in_vs = _lower(ex.get('verify_state')), _lower(inc.get('verify_state'))
    if in_vs and (not ex_vs or VERIFY_STATE_RANK.get(in_vs, -1) > VERIFY_STATE_RANK.get(ex_vs, -1)):
        out['verify_state'] = in_vs
        if _clean(inc.get('fit_verdict')):
            out['fit_verdict'] = _lower(inc['fit_verdict'])
        if _clean(inc.get('retry_after')):
            out['retry_after'] = inc['retry_after']
    elif in_vs and in_vs == ex_vs and _clean(inc.get('retry_after')) and inc['retry_after'] != ex.get('retry_after'):
        out['retry_after'] = inc['retry_after']          # same rung, newer schedule
    if _empty(ex.get('fit_verdict')) and _clean(inc.get('fit_verdict')) and 'fit_verdict' not in out:
        out['fit_verdict'] = _lower(inc['fit_verdict'])
    if _int(inc.get('enrich_attempts')) > _int(ex.get('enrich_attempts')):
        out['enrich_attempts'] = _int(inc.get('enrich_attempts'))

    # ── grade ──
    ex_g, in_g = _clean(ex.get('grade')).upper(), _clean(inc.get('grade')).upper()
    if in_g in GRADE_RANK:
        regrade = bool(_clean(inc.get('graded_event_id'))) and _clean(inc.get('graded_event_id')) == _clean(ex.get('graded_event_id'))
        take = (ex_g not in GRADE_RANK or GRADE_RANK[in_g] < GRADE_RANK[ex_g]
                or regrade or best_trigger_expired(ex, now))
        if take:
            for col in GRADE_COLUMNS:
                if col in inc and inc[col] is not None and inc[col] != ex.get(col):
                    out[col] = inc[col]

    # ── best trigger ──
    in_t, in_at, in_tid = _lower(inc.get('best_trigger_type')), inc.get('best_trigger_at'), _clean(inc.get('best_trigger_event_id'))
    if in_t or in_tid:
        ex_t, ex_at, ex_tid = _lower(ex.get('best_trigger_type')), ex.get('best_trigger_at'), _clean(ex.get('best_trigger_event_id'))
        ex_dead, in_dead = best_trigger_expired(ex, now), _trigger_expired(in_t, in_at, now)
        if not ex_t and not ex_tid:
            replace = True
        elif in_tid and in_tid == ex_tid:
            replace = True                                   # same event, refreshed
        elif in_dead and not ex_dead:
            replace = False
        elif ex_dead and not in_dead:
            replace = True
        else:
            pi, pe = _priority(in_t), _priority(ex_t)
            ta, te = parse_ts(in_at), parse_ts(ex_at)
            replace = pi < pe or (pi == pe and ta is not None and (te is None or ta > te))
        if replace:
            for col in TRIGGER_COLUMNS:
                if not _empty(inc.get(col)) and inc.get(col) != ex.get(col):
                    out[col] = inc[col]

    # ── counts and dates ──
    ex_seen = _str_list(ex.get('seen_event_ids'))
    known = set(ex_seen) | {_clean(ex.get('graded_event_id')), _clean(ex.get('best_trigger_event_id'))}
    inc_ids: List[str] = []
    for eid in _str_list(inc.get('seen_event_ids')) + [_clean(inc.get('best_trigger_event_id')),
                                                        _clean(inc.get('graded_event_id'))]:
        if eid and eid not in inc_ids:
            inc_ids.append(eid)
    fresh = [eid for eid in inc_ids if eid not in known]
    if fresh:
        out['seen_event_ids'] = (ex_seen + fresh)[-SEEN_EVENT_IDS_MAX:]
    if is_new_event is None:
        added = len(fresh)
    elif is_new_event:
        added = max(1, len(fresh))
    else:
        added = 0
    if added:
        out['event_count'] = _int(ex.get('event_count')) + added
    for col, pick in (('last_event_at', _later), ('last_seen', _later), ('first_seen', _earlier)):
        v = pick(ex.get(col), inc.get(col))
        if v is not None and parse_ts(v) != parse_ts(ex.get(col)):
            out[col] = v
    out['updated_at'] = now_iso
    return out


# ── Client helpers (thin, fail-soft) ────────────────────────────────────────
def _get_account(client, key: str, cols: Iterable[str]) -> Optional[dict]:
    cols = sorted(set(cols) | {'account_key'})
    data = (client.table('accounts').select(','.join(cols))
            .eq('account_key', key).limit(1).execute().data or [])
    return data[0] if data else None


def page_rows(client, table: str, cols: str, batch: int = 500, filters=None, order: str = 'account_key') -> List[dict]:
    """Every row of a small table, a fresh builder per page (postgrest-py's
    range() ADDS offset/limit params, so a builder cannot be reused).
    STRICT: any failure raises. list_accounts wraps it fail-soft for the
    live writers; the backfill must call THIS one — review 2026-09-08
    (Phase 4): a read that quietly returned [] made a re-run treat every
    existing row as absent and NULL its dispositions on --apply."""
    rows, off = [], 0
    while True:
        q = client.table(table).select(cols)
        if filters is not None:
            q = filters(q)
        b = q.order(order, desc=False).range(off, off + batch - 1).execute().data or []
        rows += b
        if len(b) < batch:
            break
        off += batch
    return rows


def list_accounts(client, cols=None, batch: int = 500, filters=None) -> List[dict]:
    """Every accounts row (or those passing `filters(query)`), paged; [] when
    the table is absent or unreadable — FAIL-SOFT, so a caller that must
    tell "no rows" from "could not read" (the backfill) uses page_rows.
    `cols` defaults to the columns that exist."""
    if client is None or not probe_accounts(client):
        return []
    present = account_columns_present(client)
    want = [c for c in (cols or ACCOUNT_COLUMNS) if c in present] or ['account_key']
    try:
        return page_rows(client, 'accounts', ','.join(want), batch=batch, filters=filters)
    except Exception as e:
        log.warning(f'accounts read failed: {type(e).__name__}: {str(e)[:200]}')
        return []


def upsert_account(client, row: dict, *, present=None, now: Optional[datetime] = None,
                   is_new_event: Optional[bool] = None) -> bool:
    """Read the stored row, merge_account, upsert the result on
    account_key — only the columns that exist (probe). False, and a
    warning, on any failure or when the table is absent; the caller's
    event write is never at stake."""
    key = _clean((row or {}).get('account_key'))
    if not key or client is None:
        return False
    if not probe_accounts(client):
        return False
    present = set(present) if present else account_columns_present(client)
    if not present:
        return False
    try:
        existing = _get_account(client, key, present)
        merged = merge_account(existing, row, now=now, is_new_event=is_new_event)
        payload = {k: v for k, v in merged.items() if k in present}
        payload['account_key'] = key
        client.table('accounts').upsert(payload, on_conflict='account_key').execute()
        return True
    except Exception as e:
        log.warning(f'accounts upsert failed for {key!r}: {type(e).__name__}: {str(e)[:200]}')
        return False


def touch_secondary(client, company: dict, event: dict, fit: Optional[dict],
                    now: Optional[datetime] = None) -> bool:
    """Facts-only account row for a workable, non-failed company: the M&A
    target when the acquirer got the grade, or the chosen account itself
    when grading returned nothing. Writes facts and the trigger, NEVER a
    grade, so it is safe on any company the event names; the merge is
    fill-only, so touching the graded account too changes nothing. Skips
    non-workable roles, failed fits and nameless entries."""
    company = company or {}
    name = _clean(company.get('name'))
    if not name or _lower(company.get('role')) not in WORKABLE_ROLES:
        return False
    cfit = company.get('fit') if isinstance(company.get('fit'), dict) else {}
    if _lower(cfit.get('verdict')) == 'fail':
        return False
    fit = fit if isinstance(fit, dict) else {}
    try:
        row = build_account_row(event, company, fit, grading={}, now=now)
    except ValueError:
        return False
    return upsert_account(client, row, now=now)


def load_account(client, key) -> Optional[dict]:
    """The stored accounts row for an account_key (every present column),
    or None — when there is no such row, the table is absent, or the read
    fails. Enrichment reads it to skip research on an already-verified
    account; the dashboard to show one account's facts."""
    key = _clean(key)
    if not key or client is None or not probe_accounts(client):
        return None
    try:
        return _get_account(client, key, account_columns_present(client))
    except Exception as e:
        log.warning(f'accounts read failed for {key!r}: {type(e).__name__}: {str(e)[:200]}')
        return None


def is_newer(a, b) -> bool:
    """True only when BOTH timestamps parse and a > b — the collision rule
    load_dispositions and the backfill share (undated / tie → not newer)."""
    ta, tb = parse_ts(a), parse_ts(b)
    return ta is not None and tb is not None and ta > tb


def load_dispositions(client) -> dict:
    """{account_key: {'status', 'reason', 'notes', 'name', 'at', 'by',
    'source'}} — the accounts table (rows with a disposition) MERGED over
    the legacy account_dispositions rows (mapped by company_name; the
    reason decoded out of notes, see decode_legacy_notes). On a key
    collision the NEWER verdict wins, the accounts row on a tie or when
    either side is undated — review 2026-09-08 (Phase 4): after a 'Partly
    saved — written to account_dispositions' receipt the legacy row IS the
    rep's latest word, and an older accounts row must not mask it. Either
    source may be missing; {} when both are unreadable."""
    out = {}
    if client is None:
        return out
    try:
        legacy = client.table('account_dispositions').select('*').execute().data or []
    except Exception:
        legacy = []
    mapped = [m for m in map_legacy_dispositions(legacy) if m['account_key'] and m['status']]
    for m in sorted(mapped, key=lambda m: _clean(m.get('at'))):        # newest wins a key collision
        out[m['account_key']] = {'status': m['status'], 'reason': m.get('reason'), 'notes': m.get('notes'),
                                 'name': m.get('name'), 'at': m.get('at'), 'by': None,
                                 'source': 'account_dispositions'}
    if probe_accounts(client):
        try:
            rows = page_rows(client, 'accounts',
                             'account_key,canonical_name,disposition,disposition_reason,'
                             'disposition_notes,disposition_at,disposition_by',
                             filters=lambda q: q.not_.is_('disposition', 'null'))
        except Exception as e:
            log.warning(f'accounts disposition read failed: {type(e).__name__}: {str(e)[:200]}')
            rows = []
        for r in rows:
            if _clean(r.get('disposition')) and _clean(r.get('account_key')):
                cur = out.get(r['account_key'])
                if cur is not None and is_newer(cur.get('at'), r.get('disposition_at')):
                    continue                                 # the legacy row is the newer verdict
                out[r['account_key']] = {'status': r['disposition'], 'reason': r.get('disposition_reason'),
                                         'notes': r.get('disposition_notes'), 'name': r.get('canonical_name'),
                                         'at': r.get('disposition_at'), 'by': r.get('disposition_by'),
                                         'source': 'accounts'}
    return out


def _legacy_keys_for(client, name: str, key: str) -> set:
    """Every account_dispositions.company_key that belongs to `name`: the
    pipeline's key, the v1 dashboard's spelling, and ANY other row whose
    key re-normalizes to `key` or whose company_name does. review
    2026-09-08 (Phase 4): the clear path deleted only {v1_key, key}, so a
    row under a third spelling (an older normalizer, a hand edit) survived,
    and load_dispositions — which maps legacy rows by NAME — resurrected
    its stale status on the next read. One small-table read; if it fails
    the two derivable spellings are still returned."""
    keys = {key, legacy_company_key(name)}
    try:
        rows = client.table('account_dispositions').select('company_key,company_name').execute().data or []
    except Exception:
        return keys
    for r in rows:
        r = r or {}
        lk = _lower(r.get('company_key'))
        if lk and (legacy_key_matches(name, lk) or account_key(_clean(r.get('company_name'))) == key):
            keys.add(lk)
    return keys


def set_disposition(client, name, status, reason=None, notes=None, by=None,
                    now: Optional[datetime] = None) -> str:
    """Record (or clear, when `status` is falsy / '—') a rep's verdict on an
    account. Validates the vocabulary and the reason rule, then writes BOTH
    the accounts row (when the table exists) AND the legacy
    account_dispositions row, so the dashboard and enrichment — both of
    which key their lookups off company_name through gates.account_key —
    stay consistent during the transition. The legacy row is written under
    the pipeline's key with the reason folded into notes; every other row
    for the same account (the v1 dashboard's spelling, or any other key /
    name that normalizes to this account — _legacy_keys_for) is removed so
    one account never has two legacy rows, and a clear removes them all.
    Returns a receipt: starts with 'Saved:' / 'Cleared' on success,
    'NOT saved' (or 'Partly saved') otherwise."""
    name = _clean(name)
    if client is None:
        return 'NOT saved — no database connection'
    if not name:
        return 'NOT saved — no company name'
    key = account_key(name)
    if not key:
        return f"NOT saved — couldn't derive an account key from '{name}'"
    try:
        st = normalize_status(status)
        rs = normalize_reason(reason)
    except ValueError as e:
        return f'NOT saved — {e}'
    if st in REASON_REQUIRED_STATUSES and not rs:
        return (f"NOT saved — '{st}' needs a reason "
                f"({' / '.join(DISPOSITION_REASON_LABELS.values())})")
    now_iso = _iso(_now(now))
    notes_s = (_clean(notes)[:2000] or None) if st else None
    by_s = (_clean(by)[:100] or None) if st else None
    trio = {'disposition': st, 'disposition_reason': rs if st else None,
            'disposition_notes': notes_s, 'disposition_at': now_iso if st else None,
            'disposition_by': by_s}

    wrote_accounts, errors = False, []
    if probe_accounts(client):
        try:
            present = account_columns_present(client)
            payload = {k: v for k, v in trio.items() if k in present}
            if 'updated_at' in present:
                payload['updated_at'] = now_iso          # the app stamps it on EVERY write (003_accounts.sql)
            if st:
                payload['account_key'] = key
                if 'canonical_name' in present and not _get_account(client, key, ('account_key',)):
                    payload['canonical_name'] = name[:200]  # a disposition-only row: the rep knew it first
                client.table('accounts').upsert(payload, on_conflict='account_key').execute()
            else:
                # Clearing updates in place only — never insert a nameless
                # row just to hold five NULLs.
                client.table('accounts').update(payload).eq('account_key', key).execute()
            wrote_accounts = True
        except Exception as e:
            errors.append(f'accounts: {type(e).__name__}: {str(e)[:200]}')
    # Legacy row, always: enrichment_scout._load_rep_dispositions and the
    # dashboard both read company_name from this table until it retires.
    # The reason rides inside notes (encode_legacy_notes) — until migration
    # 003 runs this row is the only copy of it — and every OTHER spelling of
    # the account is removed (write) or every spelling is (clear), so one
    # account never has two legacy rows to disagree. review 2026-09-08 (Phase 4).
    try:
        stale = _legacy_keys_for(client, name, key)
        if st:
            client.table('account_dispositions').upsert(
                {'company_key': key, 'company_name': name[:200], 'status': st,
                 'notes': encode_legacy_notes(rs, notes_s), 'updated_at': now_iso},
                on_conflict='company_key').execute()
            stale.discard(key)
        else:
            stale.add(key)
        if stale:
            client.table('account_dispositions').delete().in_('company_key', sorted(stale)).execute()
        wrote_legacy = True
    except Exception as e:
        wrote_legacy = False
        errors.append(f'account_dispositions: {type(e).__name__}: {str(e)[:200]}')

    if errors:
        if wrote_accounts or wrote_legacy:
            where = 'accounts' if wrote_accounts else 'account_dispositions'
            return f"Partly saved — written to {where}, but {'; '.join(errors)}"
        return f"NOT saved — {'; '.join(errors)}"
    if not st:
        return f'Cleared account status for {name}'
    tail = f' ({DISPOSITION_REASON_LABELS[rs]})' if rs else ''
    return f'Saved: {name} → {st}{tail}'
