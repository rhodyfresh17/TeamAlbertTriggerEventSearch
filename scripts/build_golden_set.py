#!/usr/bin/env python3
"""Build / refresh the GOLDEN SET — tests/golden/accounts.json
(Phase 4 slice C3, 2026-09-08; hardened after the adversarial review
2026-09-08 (Phase 4)).

WHY: the free gates (src/pipeline/gates.py), the structured SEC parser
(src/pipeline/structured.structured_verdict) and the finance-leader title
detector (src/scrapers/base.finance_leader_hire_kind) decide thousands of
accounts a month with zero search spend. A change to any of them must never
SILENTLY flip a decision A.J. or the pipeline already made. This exporter
samples those decisions from the live `events` / `account_dispositions`
tables into one human-readable JSON file; tests/test_golden.py re-derives
every row's expected values with the free functions only (no network, no
LLM, and — review 2026-09-08 (Phase 4) — no enrichment_scout import, so a
Mac-only import there can never break the CI test job).

    venv/bin/python scripts/build_golden_set.py --dry-run        # summary only
    venv/bin/python scripts/build_golden_set.py --out tests/golden/accounts.json
    venv/bin/python scripts/build_golden_set.py --rebase         # accept changed expected values

READ-ONLY: the only Supabase calls here are .select(). Nothing is written to
the database, ever.

PUBLIC REPO: the file may carry company names, titles, description excerpts,
HQ strings and machine verdicts. It must NEVER carry rep names, emails,
phone numbers, notes text or anything personal — `account_dispositions` is
read as (company_name, status) only (its `notes` column is never selected)
and every free-text field goes through structured.scrub(). Rep rows are
exported for the NOT-FIT statuses only ('Not a Fit', 'Out of Alignment',
'NetSuite Customer'): 'Picked Up' / 'On Rep TAL' would publish which
accounts the rep is actively pursuing, so they are never written, and an
existing row carrying one is dropped on the next run (review 2026-09-08
(Phase 4), privacy decision).

IDEMPOTENT and CONSERVATIVE: re-running against unchanged data rewrites an
identical file. Rows are keyed on (bucket, account_key, title). Existing rows
are KEPT (a golden set only grows unless a human deletes a row — the privacy
rule above is the one exception); a row marked `reviewed: true` is copied
verbatim; an unreviewed row has its INPUT fields refreshed from the live data
only when its `expected` is unchanged. When the current code (or the rep)
now says something else, the row is kept EXACTLY as it was and listed under
`expected_changed` in the summary — a golden set that rewrote its own answers
would pin nothing (review 2026-09-08 (Phase 4)). `--rebase` overwrites those
rows (inputs + expected + provenance) and is meant for a deliberate behaviour
change whose diff has been reviewed. New rows are added up to the per-bucket
target and `--max-new-rows` per run; rep rows are exempt from that cap
(every not-fit rep verdict, always).

CHECKED AT BUILD TIME: every event-derived row is verified against the free
function before it is written — a stored decision the current code cannot
reproduce from the stored inputs is skipped and counted in the summary
(never written as a failing row).

Buckets (the `bucket` field) and what `expected` holds:
  rep_disposition       every NOT-FIT account_dispositions row    {rep_status}
  entity_shape          tombstones carrying entity_shape:<kind>
                        (`entity_shape:` and `fit_gate: … entity_shape:`)
                                                           {non_operating, kind}
  structured            `structured:<verdict>: <why>` tombstones
                                                           {verdict, reason}
  hq_out_of_territory   `fit_gate: … HQ out of territory (<hq>)`
                                                           {territory:'out', hq_state}
  verified              verify_state = verified            {territory:'in', hq_state,
                                                            zi_subindustry, vertical}
  finance_leader_title  cfo_hire / executive_hire titles. The STORED event_type
                        is the ground truth (cfo_hire → 'cfo', executive_hire →
                        'exec', or None for a non-finance executive hire — a
                        strong verb, no finance role in the title); a row the detector
                        disagrees with is skipped, like every other bucket. Rows
                        whose ONLY hire signal is a strong verb (names /
                        appoints / taps / promotes …) are preferred, so a
                        verb-list revert flips them. finance_seat_open (Adzuna
                        postings) is NOT a hire the detector sees in production
                        and is excluded (review 2026-09-08 (Phase 4)).
                                                           {hire_kind: cfo|exec|null}
  tombstone_reason      bad_company_name / no_workable_account tombstones
                                                           {reason_prefix, …}
"""
import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.pipeline.gates import (  # noqa: E402
    account_key, hq_state_code, hq_territory_status, is_bad_company_name,
    is_non_operating_entity,
)
# Pure copies of the enrichment vocabularies + the structured parser + the
# public-repo scrubber (review 2026-09-08 (Phase 4)): NEVER import
# enrichment_scout here — tests/test_golden.py scans this file for it.
from src.pipeline.structured import (  # noqa: E402
    REP_DECIDED_STATUSES, REP_NOT_FIT_STATUSES, WORKABLE_ROLES, ZI_IN_VERTICAL,
    ZI_SUBINDUSTRIES, is_iapd_event, pick_primary, scrub, structured_verdict,
)
from src.pipeline.typed import parse_sec_fields  # noqa: E402
from src.scrapers.base import BODY_HEAD_CHARS, HIRE_VERBS, finance_leader_hire_kind  # noqa: E402

DEFAULT_OUT = os.path.join('tests', 'golden', 'accounts.json')
EXCERPT_CHARS = 300          # description_excerpt cap (>= BODY_HEAD_CHARS=250 the detector reads)
DEFAULT_N_PER_BUCKET = 40
DEFAULT_N_TOMBSTONE_REASON = 20
DEFAULT_MAX_NEW_ROWS = 250   # NEW sampled rows per run (rep rows exempt) — review 2026-09-08 (Phase 4)
# finance_seat_open is deliberately absent (review 2026-09-08 (Phase 4)): an
# Adzuna "X hiring: Chief Financial Officer" posting is typed by the Adzuna
# scraper, never by finance_leader_hire_kind, so pinning the detector's output
# on those titles pinned an inverted signal (EA-to-the-CFO postings as 'cfo').
HIRE_EVENT_TYPES = ('cfo_hire', 'executive_hire')
HIRE_KIND_BY_EVENT_TYPE = {'cfo_hire': 'cfo', 'executive_hire': 'exec'}
BUCKETS = ('rep_disposition', 'entity_shape', 'structured', 'hq_out_of_territory',
           'verified', 'finance_leader_title', 'tombstone_reason')
# Every field, in the order it is written — A.J. reads this file by eye.
ROW_FIELDS = ('account_key', 'name', 'bucket', 'source', 'event_type', 'title',
              'description_excerpt', 'hq', 'zi_subindustry', 'sic', 'source_url',
              'expected', 'provenance', 'reviewed', 'review_note')
EVENT_COLS = ('id', 'company_name', 'title', 'description', 'source', 'source_url',
              'event_type', 'blocked_reason', 'verify_state', 'fit_verdict', 'hq_state',
              'zi_subindustry', 'sic', 'fit', 'companies_data')
# Only these two columns of account_dispositions are ever read (never `notes`).
DISPOSITION_COLS = 'company_name,status'

_WS_RE = re.compile(r'\s+')


# ── helpers ─────────────────────────────────────────────────────────────────
def _j(v):
    """JSONB columns arrive as dict/list; older rows may hold a JSON string."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def excerpt(text, limit: int = EXCERPT_CHARS) -> str:
    return scrub(text)[:limit]


_SEC_SENTENCE_RE = re.compile(
    r'(?:Total offering: [^.]+\.|Form D industry group: [^.]+\.|'
    r'Declared revenue: [^.]+?\.(?=\s|$)|SPAC: yes\.|SIC: \d{4}(?: \([^)]*\))?\.?)')


def sec_excerpt_candidates(desc: str):
    """Excerpts to try for an SEC row, best first: the plain 300-char head,
    then the structured sentences only (for the rare long description
    whose 'SIC: NNNN' tail falls outside the head)."""
    head = excerpt(desc)
    yield head
    bits = _SEC_SENTENCE_RE.findall(scrub(desc))
    if bits:
        yield ' '.join(bits)[:EXCERPT_CHARS]


def stable_order(rows, salt: str):
    """Deterministic pseudo-random order (sha1 of the event id) so the same
    data always yields the same sample, spread across sources/dates."""
    return sorted(rows, key=lambda r: hashlib.sha1(f'{salt}:{r.get("id")}'.encode()).hexdigest())


def round_robin(groups: 'OrderedDict', n: int):
    """Interleave groups so every stratum (verdict, state, source…) is
    represented before any one of them dominates the sample."""
    out, queues = [], [list(g) for g in groups.values()]
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n:
                out.append(q.pop(0))
    return out


def companies(row) -> list:
    return [c for c in (_j((row or {}).get('companies_data')) or []) if isinstance(c, dict)]


def fit_of(row) -> dict:
    f = _j((row or {}).get('fit'))
    return f if isinstance(f, dict) else {}


def account_record(row) -> dict:
    """The company dict enrichment chose as the account: fit.account_name by
    name, else the primary-role pick, else the first record (mirrors
    enrichment_scout._account_company / backfill.chosen_account)."""
    row = row or {}
    cd = companies(row)
    if not cd:
        return {}
    want = (fit_of(row).get('account_name') or row.get('company_name') or '').strip()
    if want:
        hit = next((c for c in cd if (c.get('name') or '').strip() == want), None)
        if hit:
            return hit
    try:
        return pick_primary(cd) or cd[0]
    except Exception:
        return cd[0]


def name_of(row) -> str:
    row = row or {}
    return scrub(fit_of(row).get('account_name') or account_record(row).get('name')
                 or row.get('company_name') or '')


def hq_of(row, rec=None) -> str:
    # A rep row may have no event on file for context: ev=None used to raise
    # AttributeError here (review 2026-09-08 (Phase 4)).
    row = row or {}
    rec = rec if rec is not None else account_record(row)
    return scrub(rec.get('hq') or row.get('hq_state') or '')


def zi_of(row, rec=None):
    row = row or {}
    rec = rec if rec is not None else account_record(row)
    z = (rec.get('zi_subindustry') or row.get('zi_subindustry')
         or fit_of(row).get('zi_subindustry') or '')
    return scrub(z) or None


def sic_of(row):
    row = row or {}
    return row.get('sic') or parse_sec_fields(row).get('sic') or None


def is_iapd(row) -> bool:
    try:
        return is_iapd_event(row)
    except Exception:
        return False


def make_row(row, bucket: str, name: str, expected: dict, provenance: str,
             hq=None, zi=None, keep_url: bool = False) -> dict:
    rec = account_record(row) if row else {}
    r = OrderedDict()
    r['account_key'] = account_key(name)
    r['name'] = name
    r['bucket'] = bucket
    r['source'] = (row or {}).get('source') or None
    r['event_type'] = (row or {}).get('event_type') or None
    r['title'] = scrub((row or {}).get('title')) or None
    r['description_excerpt'] = excerpt((row or {}).get('description')) if row else ''
    r['hq'] = (hq if hq is not None else hq_of(row, rec)) or None
    r['zi_subindustry'] = zi if zi is not None else zi_of(row, rec)
    r['sic'] = sic_of(row) if row else None
    # source_url is an INPUT only for the structured parser (it keys on
    # 'sec.gov'); every other bucket leaves it out to keep the file lean.
    r['source_url'] = ((row or {}).get('source_url') or None) if keep_url else None
    r['expected'] = expected
    r['provenance'] = provenance
    r['reviewed'] = False
    r['review_note'] = None
    return r


def row_key(r) -> tuple:
    return (r.get('bucket') or '', r.get('account_key') or '', r.get('title') or '')


# ── Supabase (read-only) ────────────────────────────────────────────────────
def connect():
    import logging
    import warnings
    warnings.filterwarnings('ignore')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
    from supabase import create_client
    url, key = os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        sys.exit('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing from .env — nothing read.')
    return create_client(url, key)


def fetch_events(svc, where, batch: int = 500) -> list:
    """Paginated SELECT over `events` (order by id + .range) — never a write.
    `where` applies the filter to the query builder."""
    cols = ','.join(EVENT_COLS)
    rows, off = [], 0
    while True:
        q = where(svc.table('events').select(cols)).order('id').range(off, off + batch - 1)
        b = q.execute().data or []
        rows += b
        if len(b) < batch:
            break
        off += batch
    return rows


# ── bucket builders ─────────────────────────────────────────────────────────
class Tally:
    """Per-bucket bookkeeping for the summary."""
    def __init__(self):
        self.candidates = 0
        self.reproduced = 0
        self.skipped = Counter()
        self.notes = Counter()


def build_rep_dispositions(svc, seed: str, tally: Tally) -> list:
    """Bucket (a): every NOT-FIT rep verdict. Only (company_name, status) is
    read — the table's `notes` column never leaves Supabase. PRIVACY (review
    2026-09-08 (Phase 4)): 'Picked Up' / 'On Rep TAL' rows are never
    exported — the public repo must not say which accounts the rep is
    actively pursuing."""
    rows = svc.table('account_dispositions').select(DISPOSITION_COLS).execute().data or []
    out = []
    for d in rows:
        tally.candidates += 1
        name, status = scrub(d.get('company_name')), scrub(d.get('status'))
        if not name or not status:
            tally.skipped['blank name/status'] += 1
            continue
        if status in REP_DECIDED_STATUSES:
            tally.skipped['rep-decided status (Picked Up / On Rep TAL) — kept out of the public file'] += 1
            continue
        if status not in REP_NOT_FIT_STATUSES:
            tally.skipped[f'status {status!r} is not one enrichment honours'] += 1
            continue
        key = account_key(name)
        # One event for context (title / excerpt / hq / zi), if any exists.
        ev = None
        try:
            hits = (svc.table('events').select(','.join(EVENT_COLS)).eq('account_key', key)
                    .order('id').range(0, 0).execute().data or [])
            ev = hits[0] if hits else None
        except Exception:
            ev = None
        r = make_row(ev, 'rep_disposition', name, {'rep_status': status}, f'rep:{status}')
        if ev is None:
            tally.notes['no event on file for context'] += 1
        tally.reproduced += 1
        out.append(r)
    return out


_ENTITY_RE = re.compile(r'entity_shape:([a-z0-9_]+)(?: \(([^)]*)\))?')


def build_entity_shape(svc, seed: str, tally: Tally, n: int) -> list:
    """Bucket (b): the kind in the tombstone reason must be reproduced by
    is_non_operating_entity(name) on the FULL name from companies_data
    (the reason truncates names to 40 chars). Pure `entity_shape:` rows
    predate persisted companies_data, so most cannot be seeded; `fit_gate:`
    rows carry `<name>: entity_shape:<kind> (<name>)` per company."""
    rows = (fetch_events(svc, lambda q: q.like('blocked_reason', 'entity_shape:%'))
            + fetch_events(svc, lambda q: q.like('blocked_reason', 'fit_gate:%entity_shape:%'))
            + fetch_events(svc, lambda q: q.like('blocked_reason', 'fit_gate:%aj_exclusion%')))
    seen, groups = set(), OrderedDict()
    for row in stable_order(rows, 'entity_shape'):
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        tally.candidates += 1
        if is_iapd(row):
            # H3/P4 (review 2026-09-08): an SEC-registered adviser's '… LP'
            # name is the management company, exempt from fund_vehicle —
            # the stored tombstone is no longer the pipeline's decision.
            tally.skipped['sec_iapd (fund_vehicle exemption)'] += 1
            continue
        reason = row.get('blocked_reason') or ''
        wanted = [(m.group(1), m.group(2) or '') for m in _ENTITY_RE.finditer(reason)]
        if reason.startswith('entity_shape:'):
            wanted = [(k, '') for k in reason[len('entity_shape:'):].split(',') if k]
        cands = [(c.get('name') or '').strip() for c in companies(row)]
        cands += [(row.get('company_name') or '').strip()]
        picked = None
        for kind, stub in wanted:
            for nm in cands:
                if not nm or (stub and not nm.startswith(stub.strip())):
                    continue
                hit, k = is_non_operating_entity(nm)
                if hit and k == kind:
                    picked = (nm, kind)
                    break
            if picked:
                break
        if not picked:
            tally.skipped['stored name does not reproduce the kind'
                          + (' (no companies_data)' if not companies(row) else '')] += 1
            continue
        nm, kind = picked
        rec = next((c for c in companies(row) if (c.get('name') or '').strip() == nm), {})
        r = make_row(row, 'entity_shape', scrub(nm),
                     {'non_operating': True, 'kind': kind}, f'machine-seeded {seed}',
                     hq=hq_of(row, rec) if rec else None, zi=zi_of(row, rec) if rec else None)
        groups.setdefault(kind, []).append(r)
        tally.reproduced += 1
    return sample(groups, n)


_STRUCTURED_RE = re.compile(r'^structured:(out|vehicle|too_small):\s*(.*)$')


def build_structured(svc, seed: str, tally: Tally, n: int) -> list:
    """Bucket (c): the stored verdict must come back from
    structured_verdict() on the EXCERPT (the test cannot see the full
    description). The reason text is recorded as the CURRENT output."""
    rows = fetch_events(svc, lambda q: q.like('blocked_reason', 'structured:%'))
    groups = OrderedDict()
    for row in stable_order(rows, 'structured'):
        tally.candidates += 1
        m = _STRUCTURED_RE.match(row.get('blocked_reason') or '')
        if not m:
            # e.g. 'structured:item_1.01 without acquisition language' — decided
            # from the filing's full text, not from the description: no free
            # function can reproduce it offline.
            tally.skipped['not a SIC / Form D verdict (item_1.01 text rule)'] += 1
            continue
        verdict = m.group(1)
        chosen = None
        for ex in sec_excerpt_candidates(row.get('description') or ''):
            sv = structured_verdict({'description': ex, 'title': row.get('title') or '',
                                     'source_url': row.get('source_url') or ''})
            if sv.get('verdict') == verdict:
                chosen = (ex, sv)
                break
        if not chosen:
            # older tombstones whose description predates the embedded
            # 'SPAC: yes.' / 'SIC: NNNN' phrases — the verdict came from
            # filing data the row no longer carries.
            tally.skipped['description lacks the SIC / SPAC phrase the verdict used'] += 1
            continue
        ex, sv = chosen
        if scrub(sv.get('reason')) != scrub(m.group(2)):
            tally.notes['reason text drifted since tombstoning (current text recorded)'] += 1
        name = name_of(row)
        if not name:
            tally.skipped['no account name'] += 1
            continue
        r = make_row(row, 'structured', name,
                     {'verdict': verdict, 'reason': scrub(sv.get('reason')),
                      'revenue_segment': sv.get('revenue_segment') or None},
                     f'machine-seeded {seed}', keep_url=True)
        r['description_excerpt'] = ex
        groups.setdefault(verdict, []).append(r)
        tally.reproduced += 1
    return sample(groups, n)


_HQ_OUT_RE = re.compile(r'(?:(?:^fit_gate:\s*|;\s*)([^;:]+?):\s*)?HQ out of territory \(([^)]*)\)')


def build_hq_out(svc, seed: str, tally: Tally, n: int) -> list:
    """Bucket (d): the HQ the gate evaluated is embedded in the reason —
    `<name>: HQ out of territory (<hq>)` — and is preferred over the stored
    companies_data record (which can be stale: fit was sometimes persisted
    from an earlier pass with hq '')."""
    rows = fetch_events(svc, lambda q: q.like('blocked_reason', 'fit_gate:%HQ out of territory%'))
    groups = OrderedDict()
    for row in stable_order(rows, 'hq_out'):
        tally.candidates += 1
        reason = row.get('blocked_reason') or ''
        m = _HQ_OUT_RE.search(reason)
        rec = account_record(row)
        name = scrub((m.group(1) if m and m.group(1) else '') or name_of(row))
        hq = scrub(m.group(2)) if m else ''
        if not hq or hq.lower() in ('none', 'null', ''):
            hq = hq_of(row, rec)
        if not name or not hq:
            tally.skipped['no name/hq recoverable'] += 1
            continue
        if hq_territory_status(hq) != 'out':
            tally.skipped['stored hq no longer reads as out'] += 1
            continue
        if m and m.group(1):
            # the reason names the company; use ITS record for zi (if any)
            rec = next((c for c in companies(row)
                        if (c.get('name') or '').strip() == name), rec)
        state = hq_state_code(hq)
        r = make_row(row, 'hq_out_of_territory', name,
                     {'territory': 'out', 'hq_state': state},
                     f'machine-seeded {seed}', hq=hq, zi=zi_of(row, rec))
        groups.setdefault(state or 'foreign', []).append(r)
        tally.reproduced += 1
    return sample(groups, n)


def build_verified(svc, seed: str, tally: Tally, n: int) -> list:
    """Bucket (e): a verified account's stored HQ must read 'in' and its
    subindustry must sit in the ZI allowlist (and never in ZI_NOT_A_FIT)."""
    rows = fetch_events(svc, lambda q: q.eq('verify_state', 'verified'))
    groups = OrderedDict()
    for row in stable_order(rows, 'verified'):
        tally.candidates += 1
        rec = account_record(row)
        name, hq, zi = name_of(row), hq_of(row, rec), zi_of(row, rec)
        if not name:
            tally.skipped['no account name'] += 1
            continue
        if hq_territory_status(hq) != 'in':
            tally.skipped['stored hq does not read as in'] += 1
            continue
        if zi not in ZI_IN_VERTICAL:
            tally.skipped['zi not in allowlist'] += 1
            continue
        r = make_row(row, 'verified', name,
                     {'territory': 'in', 'hq_state': hq_state_code(hq),
                      'zi_subindustry': zi, 'vertical': ZI_SUBINDUSTRIES[zi]},
                     f'machine-seeded {seed}', hq=hq, zi=zi)
        groups.setdefault(row.get('source') or 'other', []).append(r)
        tally.reproduced += 1
    return sample(groups, n)


# The strong hire verbs the detector accepts, as a whole-word regex, so the
# exporter can ask "would this row flip if the verb list were reverted?".
_STRONG_VERB_RE = re.compile(r'(?<!\w)(?:' + '|'.join(HIRE_VERBS) + r')(?!\w)', re.IGNORECASE)
# Any finance-ish word: a title with none of these is a clean NEGATIVE (a
# CEO / COO / President hire the finance detector must stay silent on).
_FINANCE_WORD_RE = re.compile(
    r'(?<!\w)(?:cfo|chief\s+financ\w*|financ\w*|controller|comptroller|treasurer|'
    r'treasury|accounting)(?!\w)', re.IGNORECASE)


def verb_is_only_signal(title: str, head: str, truth) -> bool:
    """True when blanking the strong hire verbs flips the detector's answer:
    a verb-list revert would flip this row, which is what makes it worth
    pinning (review 2026-09-08 (Phase 4): 0 of 40 rows flipped before)."""
    return finance_leader_hire_kind(_STRONG_VERB_RE.sub(' ', title),
                                    _STRONG_VERB_RE.sub(' ', head)) != truth


def build_finance_leader_titles(svc, seed: str, tally: Tally, n: int) -> list:
    """Bucket (f): the STORED event_type is the ground truth (cfo_hire →
    'cfo', executive_hire → 'exec'); a row finance_leader_hire_kind(title,
    body[:250]) disagrees with is skipped, exactly as the other buckets skip
    a decision the code cannot reproduce. An executive_hire whose title
    carries a strong hire verb but names no finance role at all is a
    NEGATIVE (expected None). Strata, in preference order: verb-only (a
    revert of the verb list flips it), phrase (a hire noun phrase carries
    it), negative."""
    rows = fetch_events(svc, lambda q: q.in_('event_type', list(HIRE_EVENT_TYPES)))
    strata = OrderedDict((s, OrderedDict()) for s in ('verb-only', 'phrase', 'negative'))
    for row in stable_order(rows, 'finance_leader'):
        tally.candidates += 1
        title = scrub(row.get('title'))
        if not title:
            tally.skipped['blank title'] += 1
            continue
        et = row.get('event_type')
        truth = HIRE_KIND_BY_EVENT_TYPE.get(et)
        if truth is None:
            tally.skipped[f'event_type {et!r} is not a title-typed hire'] += 1
            continue
        head = excerpt(row.get('description'))[:BODY_HEAD_CHARS]
        got = finance_leader_hire_kind(title, head)
        if got == truth:
            stratum = 'verb-only' if verb_is_only_signal(title, head, truth) else 'phrase'
            expected = {'hire_kind': truth}
        elif (et == 'executive_hire' and got is None and _STRONG_VERB_RE.search(title)
              and not _FINANCE_WORD_RE.search(title)):
            # a strong verb + a non-finance seat ("Promotes X to VP of Aircraft
            # Management"): the shape a loosened role regex would flip. A bare
            # SEC 8-K headline is None trivially and pins nothing.
            stratum, expected = 'negative', {'hire_kind': None}
        else:
            tally.skipped[f'{et}: detector says {got!r}, the stored type needs {truth!r}'] += 1
            continue
        r = make_row(row, 'finance_leader_title', name_of(row), expected, f'machine-seeded {seed}')
        strata[stratum].setdefault((et, expected['hire_kind'] or 'none'), []).append(r)
        tally.notes[f'{et} -> {expected["hire_kind"]} ({stratum})'] += 1
        tally.reproduced += 1
    quotas = {'verb-only': n - n // 4 - n // 8, 'phrase': n // 4, 'negative': n // 8}
    return sample_quota(strata, quotas, n)


def build_tombstone_reasons(svc, seed: str, tally: Tally, n: int) -> list:
    """Bucket (g): bad_company_name → is_bad_company_name(name, title+excerpt)
    must be True; no_workable_account → none of the stored roles may be in
    WORKABLE_ROLES (investor roles were dropped 2026-09-06 — re-adding one
    flips these rows, which is the point)."""
    bad = fetch_events(svc, lambda q: q.like('blocked_reason', 'bad_company_name:%'))
    nwa = fetch_events(svc, lambda q: q.like('blocked_reason', 'no_workable_account%'))
    groups = OrderedDict([('bad_company_name', []), ('no_workable_account', [])])
    for row in stable_order(bad, 'bad_name'):
        tally.candidates += 1
        name = scrub(row.get('company_name'))
        if not name:
            # An empty name is rejected trivially and pins nothing (review
            # 2026-09-08 (Phase 4)): never a golden row.
            tally.skipped['bad_company_name: blank name'] += 1
            continue
        ctx = f'{scrub(row.get("title"))} {excerpt(row.get("description"))}'
        if not is_bad_company_name(name, ctx):
            tally.skipped['bad_company_name: name now passes the gate'] += 1
            continue
        r = make_row(row, 'tombstone_reason', name,
                     {'reason_prefix': 'bad_company_name', 'bad_company_name': True},
                     f'machine-seeded {seed}')
        groups['bad_company_name'].append(r)
        tally.reproduced += 1
    for row in stable_order(nwa, 'no_workable'):
        tally.candidates += 1
        roles = [scrub(c.get('role')) for c in companies(row) if c.get('role')]
        if not roles:
            tally.skipped['no_workable_account: no roles stored'] += 1
            continue
        if any(rl.lower() in WORKABLE_ROLES for rl in roles):
            tally.skipped['no_workable_account: a stored role is workable now'] += 1
            continue
        name = name_of(row)
        if not name:
            tally.skipped['no_workable_account: no name'] += 1
            continue
        r = make_row(row, 'tombstone_reason', name,
                     {'reason_prefix': 'no_workable_account', 'roles': roles, 'workable': False},
                     f'machine-seeded {seed}')
        groups['no_workable_account'].append(r)
        tally.reproduced += 1
    return sample(groups, n)


def sample(groups: 'OrderedDict', n: int) -> list:
    """Interleave every stratum, then keep the first n distinct accounts."""
    return dedup(round_robin(groups, sum(len(g) for g in groups.values())), n)


def sample_quota(strata: 'OrderedDict', quotas: dict, n: int) -> list:
    """Priority sampling (review 2026-09-08 (Phase 4)): strata are filled to
    their quota in order, each interleaving its own groups (cfo / exec both
    appear); any shortfall flows to the next stratum in priority order. One
    row per account (or per title when the name is blank)."""
    picked, seen = [], set()
    pools = OrderedDict((name, round_robin(groups, sum(len(g) for g in groups.values())))
                        for name, groups in strata.items())

    def take(name, k):
        taken = 0
        for r in pools[name]:
            if taken >= k or len(picked) >= n:
                return
            key = r['account_key'] or ('title:' + (r.get('title') or ''))
            if key in seen:
                continue
            seen.add(key)
            picked.append(r)
            taken += 1

    for name in pools:
        take(name, quotas.get(name, n))
    for name in pools:              # shortfall: fill from the next stratum
        take(name, n)
    return picked


def dedup(rows: list, n: int) -> list:
    """One row per account (or per title when the name is blank), first n."""
    out, seen = [], set()
    for r in rows:
        k = r['account_key'] or ('title:' + (r.get('title') or ''))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= n:
            break
    return out


# ── merge with the existing file ────────────────────────────────────────────
def load_existing(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        sys.exit(f'{path} is not a JSON list — refusing to overwrite it.')
    return data


def is_rep_decided_row(r) -> bool:
    """A row that would publish an account the rep is actively pursuing
    ('Picked Up' / 'On Rep TAL') — never allowed in the public file."""
    exp = r.get('expected') if isinstance(r.get('expected'), dict) else {}
    prov = str(r.get('provenance') or '')
    return (exp.get('rep_status') in REP_DECIDED_STATUSES
            or prov in {f'rep:{s}' for s in REP_DECIDED_STATUSES})


def with_rep_provenance(r):
    """A rep row's provenance is derived from its (current) status — the
    old code refreshed expected.rep_status but kept the stale provenance
    (review 2026-09-08 (Phase 4))."""
    if r.get('bucket') == 'rep_disposition':
        st = (r.get('expected') or {}).get('rep_status')
        if st:
            r['provenance'] = f'rep:{st}'
    return r


def _label(old, new) -> str:
    return (f"{old.get('bucket')} / {old.get('name') or old.get('title') or old.get('account_key')!r}: "
            f"{json.dumps(old.get('expected'), ensure_ascii=False)} -> "
            f"{json.dumps(new.get('expected'), ensure_ascii=False)}")


def merge(existing: list, fresh_by_bucket: dict, targets: dict, max_new: int, rebase: bool = False):
    """Existing rows are kept (reviewed ones verbatim; unreviewed ones have
    their input fields refreshed only when `expected` is unchanged — a
    changed `expected` is kept as it was and reported, or overwritten under
    --rebase); rep-decided rows are dropped (privacy); fresh rows fill each
    bucket up to its target, at most `max_new` sampled rows per run (rep
    rows exempt). Returns (rows, stats, report)."""
    fresh = {row_key(r): r for rows in fresh_by_bucket.values() for r in rows}
    merged, stats = OrderedDict(), Counter()
    report = {'expected_changed': [], 'rebased': []}
    for old in existing:
        k = row_key(old)
        if is_rep_decided_row(old):
            stats['privacy_dropped'] += 1
            continue
        if old.get('reviewed') is True or k not in fresh:
            merged[k] = old
            stats['kept'] += 1
            continue
        new = fresh[k]
        if new.get('expected') != old.get('expected'):
            if rebase:
                row = OrderedDict(new)
                for f in ('reviewed', 'review_note'):
                    if f in old:
                        row[f] = old[f]
                merged[k] = with_rep_provenance(row)
                stats['rebased'] += 1
                report['rebased'].append(_label(old, new))
            else:
                merged[k] = old                     # verbatim: inputs AND expected
                stats['expected_changed'] += 1
                report['expected_changed'].append(_label(old, new))
            continue
        row = OrderedDict(new)
        for f in ('provenance', 'reviewed', 'review_note'):
            if f in old:
                row[f] = old[f]
        merged[k] = with_rep_provenance(row)
        stats['refreshed'] += 1
    per_bucket = Counter(r.get('bucket') for r in merged.values())
    for bucket, rows in fresh_by_bucket.items():
        target = targets.get(bucket)
        exempt = bucket == 'rep_disposition'        # every not-fit rep verdict, always
        for r in rows:
            k = row_key(r)
            if k in merged:
                continue
            if target is not None and per_bucket[bucket] >= target:
                break
            if not exempt and stats['added_sampled'] >= max_new:
                stats['cap_hit'] += 1
                break
            merged[k] = with_rep_provenance(OrderedDict(r))
            per_bucket[bucket] += 1
            stats['added'] += 1
            if not exempt:
                stats['added_sampled'] += 1
    rows = sorted(merged.values(),
                  key=lambda r: (r.get('account_key') or '', r.get('bucket') or '', r.get('title') or ''))
    return [normalize(r) for r in rows], stats, report


def normalize(r) -> 'OrderedDict':
    """Fixed key order; unknown keys a human added are kept at the end."""
    out = OrderedDict()
    for f in ROW_FIELDS:
        out[f] = r.get(f)
    for k, v in r.items():
        if k not in out:
            out[k] = v
    if out['reviewed'] is None:
        out['reviewed'] = False
    return out


def write(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
        fh.write('\n')


def print_summary(tallies: dict, rows: list, stats: Counter, report: dict, path: str,
                  dry_run: bool, rebase: bool) -> None:
    print('\nGOLDEN SET — bucket summary')
    print(f"  {'bucket':<22}{'candidates':>11}{'reproduced':>11}{'in file':>9}")
    per_bucket = Counter(r.get('bucket') for r in rows)
    for b in BUCKETS:
        t = tallies.get(b, Tally())
        print(f'  {b:<22}{t.candidates:>11}{t.reproduced:>11}{per_bucket.get(b, 0):>9}')
    for b in BUCKETS:
        t = tallies.get(b)
        if not t:
            continue
        for why, k in sorted(t.skipped.items()):
            print(f'    skipped  {b}: {k} x {why}')
        for why, k in sorted(t.notes.items()):
            print(f'    note     {b}: {k} x {why}')
    print(f"  rows: {len(rows)}  (kept {stats['kept']}, refreshed {stats['refreshed']}, "
          f"rebased {stats['rebased']}, added {stats['added']}, cap hits {stats['cap_hit']}, "
          f"privacy-dropped {stats['privacy_dropped']})")
    if report['expected_changed']:
        print(f"  expected_changed: {len(report['expected_changed'])} row(s) KEPT AS THEY WERE — "
              f"the current output differs. Fix the code, or re-run with --rebase after "
              f"reviewing the diff:")
        for line in report['expected_changed']:
            print(f'    {line}')
    if report['rebased']:
        print(f"  rebased: {len(report['rebased'])} row(s) overwritten (--rebase):")
        for line in report['rebased']:
            print(f'    {line}')
    if dry_run:
        print(f'  DRY RUN — {path} not written')
    else:
        print(f'  wrote {path} ({os.path.getsize(path):,} bytes)')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=DEFAULT_OUT, help=f'golden file (default {DEFAULT_OUT})')
    ap.add_argument('--n-per-bucket', type=int, default=DEFAULT_N_PER_BUCKET,
                    help='target rows for the sampled buckets (default 40)')
    ap.add_argument('--n-tombstone-reason', type=int, default=DEFAULT_N_TOMBSTONE_REASON,
                    help='target rows for the tombstone_reason bucket (default 20)')
    ap.add_argument('--max-new-rows', '--max-rows', dest='max_new_rows', type=int,
                    default=DEFAULT_MAX_NEW_ROWS,
                    help='cap on NEW sampled rows added per run; rep rows are exempt (default 250)')
    ap.add_argument('--rebase', action='store_true',
                    help='overwrite unreviewed rows whose expected value changed (inputs + '
                         'expected + provenance). Default: keep them verbatim and list them '
                         'as expected_changed')
    ap.add_argument('--seed-date', default=date.today().isoformat(),
                    help='date stamped into machine-seeded provenance (default today)')
    ap.add_argument('--dry-run', action='store_true', help='print the summary, write nothing')
    args = ap.parse_args(argv)

    out = args.out if os.path.isabs(args.out) else os.path.join(_ROOT, args.out)
    svc = connect()
    n, seed = max(1, args.n_per_bucket), args.seed_date
    tallies = {b: Tally() for b in BUCKETS}
    fresh = OrderedDict()
    fresh['rep_disposition'] = build_rep_dispositions(svc, seed, tallies['rep_disposition'])
    fresh['entity_shape'] = build_entity_shape(svc, seed, tallies['entity_shape'], n)
    fresh['structured'] = build_structured(svc, seed, tallies['structured'], n)
    fresh['hq_out_of_territory'] = build_hq_out(svc, seed, tallies['hq_out_of_territory'], n)
    fresh['verified'] = build_verified(svc, seed, tallies['verified'], n)
    fresh['finance_leader_title'] = build_finance_leader_titles(svc, seed, tallies['finance_leader_title'], n)
    fresh['tombstone_reason'] = build_tombstone_reasons(svc, seed, tallies['tombstone_reason'],
                                                        max(1, args.n_tombstone_reason))
    targets = {b: n for b in BUCKETS}
    targets['rep_disposition'] = None            # every not-fit rep verdict, always
    targets['tombstone_reason'] = max(1, args.n_tombstone_reason)

    existing = load_existing(out)
    rows, stats, report = merge(existing, fresh, targets, max(0, args.max_new_rows), rebase=args.rebase)
    if not args.dry_run:
        write(out, rows)
    print_summary(tallies, rows, stats, report, out, args.dry_run, args.rebase)
    return 0


if __name__ == '__main__':
    sys.exit(main())
