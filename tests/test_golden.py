"""GOLDEN SET — tests/golden/accounts.json (Phase 4 slice C3, 2026-09-08;
hardened after the adversarial review 2026-09-08 (Phase 4)).

Every row is a decision A.J. or the pipeline already made (a rep verdict, a
tombstone reason, a verified account, a hire-title classification). These
tests re-derive each row's `expected` values with the FREE functions only —
gates.py, src.pipeline.structured.structured_verdict (a pure regex parser),
src.scrapers.base.finance_leader_hire_kind — with no network and no LLM, so
a change to a gate / detector / parser can never silently flip a decision.

This module runs in CI (a separate `tests` job that never touches the
scheduled scrape — test_ci_shape pins that) with only requests + PyYAML
installed, so it must never import enrichment_scout at module level
(test_golden_code_never_imports_enrichment_scout pins THAT). The one test
that compares the pure copy with enrichment_scout imports it under
importorskip.

Reviewed rows (`reviewed: true`) and machine-seeded rows are asserted with
the SAME strictness: machine-seeded rows encode current behaviour, and a
behaviour change must update the file deliberately (see
tests/golden/README.md — never edit an expected value to make a test pass;
fix the code, or mark the row reviewed with a reason, or `--rebase` after a
reviewed behaviour change).

Regenerate / extend with:  venv/bin/python scripts/build_golden_set.py
"""
import importlib.util
import json
import os
import re
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from src.pipeline.gates import (  # noqa: E402
    account_key, hq_state_code, hq_territory_status, is_bad_company_name,
    is_non_operating_entity,
)
from src.pipeline.structured import (  # noqa: E402
    EMAIL_RE, PHONE_RE, PRIMARY_ROLE_ORDER, REP_DECIDED_STATUSES, REP_NOT_FIT_STATUSES,
    WORKABLE_ROLES, ZI_IN_VERTICAL, ZI_NOT_A_FIT, ZI_SUBINDUSTRIES, is_iapd_event,
    pick_primary, scrub, structured_verdict,
)
from src.scrapers.base import BODY_HEAD_CHARS, HIRE_VERBS, finance_leader_hire_kind  # noqa: E402

GOLDEN_PATH = os.path.join(REPO, 'tests', 'golden', 'accounts.json')
README_PATH = os.path.join(REPO, 'tests', 'golden', 'README.md')
WORKFLOW_PATH = os.path.join(REPO, '.github', 'workflows', 'scraper.yml')
EXPORTER_PATH = os.path.join(REPO, 'scripts', 'build_golden_set.py')
STRUCTURED_PATH = os.path.join(REPO, 'src', 'pipeline', 'structured.py')
BUCKETS = ('rep_disposition', 'entity_shape', 'structured', 'hq_out_of_territory',
           'verified', 'finance_leader_title', 'tombstone_reason')
REQUIRED_KEYS = ('account_key', 'name', 'bucket', 'source', 'event_type', 'title',
                 'description_excerpt', 'hq', 'zi_subindustry', 'sic', 'expected',
                 'provenance', 'reviewed')
EXCERPT_CHARS = 300
MIN_ROWS = 50                     # a truncated / emptied file must fail loudly, not pass vacuously
# The finance_leader_title bucket must not be a tautology (review 2026-09-08
# (Phase 4): 0 of 40 rows flipped when the hire-verb list was reverted). At
# least this many rows must flip when the strong verbs are blanked.
MIN_VERB_ONLY_ROWS = 10
# Only NOT-FIT rep statuses may appear in the public file (privacy decision,
# review 2026-09-08 (Phase 4)): 'Picked Up' / 'On Rep TAL' would publish the
# accounts the rep is actively pursuing.
PUBLIC_REP_STATUSES = frozenset(REP_NOT_FIT_STATUSES)
HIRE_EVENT_TYPES = ('cfo_hire', 'executive_hire')
HIRE_KIND_BY_EVENT_TYPE = {'cfo_hire': ('cfo',), 'executive_hire': ('exec', None)}
_PROVENANCE_RE = re.compile(r'^(rep:(' + '|'.join(re.escape(s) for s in sorted(PUBLIC_REP_STATUSES))
                            + r')|machine-seeded \d{4}-\d{2}-\d{2})$')
_STRONG_VERB_RE = re.compile(r'(?<!\w)(?:' + '|'.join(HIRE_VERBS) + r')(?!\w)', re.IGNORECASE)
_IMPORTS_ENRICHMENT_RE = re.compile(r'^\s*(?:import\s+enrichment_scout\b|from\s+enrichment_scout\b)', re.M)
RULE = ('never edit an expected value to make this pass — fix the code, or mark the '
        'row reviewed with a reason (tests/golden/README.md)')


def _load_exporter():
    """scripts/ is not a package; load the exporter by path. Its top-level
    imports are pure (gates / typed / structured / base) — Supabase and
    dotenv are imported lazily inside connect() — so this works in CI."""
    spec = importlib.util.spec_from_file_location('build_golden_set', EXPORTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bgs = _load_exporter()


def _load():
    if not os.path.exists(GOLDEN_PATH):
        return []
    with open(GOLDEN_PATH, encoding='utf-8') as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


ROWS = _load()


def _rows(bucket):
    return [r for r in ROWS if isinstance(r, dict) and r.get('bucket') == bucket]


def _id(r):
    return str(r.get('name') or (r.get('title') or '')[:40] or r.get('account_key') or '?')


def _param(bucket):
    rows = _rows(bucket)
    return pytest.mark.parametrize('row', rows, ids=[_id(r) for r in rows])


def _why(row, what):
    return f'{row.get("bucket")} / {_id(row)!r}: {what}. {RULE}'


def _strings(value):
    """Every string inside a row (nested dicts/lists included)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Belt and braces: nothing in this module may touch the network."""
    import requests

    def _refuse(*a, **k):
        raise AssertionError('golden tests must not use the network')
    monkeypatch.setattr(requests.sessions.Session, 'request', _refuse)
    monkeypatch.setattr(requests, 'request', _refuse)
    monkeypatch.setattr(requests, 'get', _refuse)
    monkeypatch.setattr(requests, 'post', _refuse)


# ── file-level ──────────────────────────────────────────────────────────────
def test_golden_file_present_and_nonempty():
    assert os.path.exists(GOLDEN_PATH), (
        f'{GOLDEN_PATH} is missing — regenerate with scripts/build_golden_set.py')
    assert os.path.exists(README_PATH), 'tests/golden/README.md documents the rules; keep it'
    assert len(ROWS) >= MIN_ROWS, f'golden set has {len(ROWS)} rows (< {MIN_ROWS}) — truncated?'


def test_bucket_coverage():
    counts = {b: len(_rows(b)) for b in BUCKETS}
    empty = [b for b, n in counts.items() if n == 0]
    assert not empty, f'buckets with no rows: {empty} ({counts})'
    unknown = sorted({r.get('bucket') for r in ROWS if isinstance(r, dict)} - set(BUCKETS))
    assert not unknown, f'unknown bucket names: {unknown}'


def test_schema():
    """Shape, provenance tags, public-repo hygiene, identity, uniqueness —
    every violation is listed so one run shows them all."""
    problems, seen = [], set()
    for i, r in enumerate(ROWS):
        tag = f'row {i} ({_id(r) if isinstance(r, dict) else "?"})'
        if not isinstance(r, dict):
            problems.append(f'{tag}: not an object')
            continue
        missing = [k for k in REQUIRED_KEYS if k not in r]
        if missing:
            problems.append(f'{tag}: missing keys {missing}')
        if r.get('bucket') not in BUCKETS:
            problems.append(f'{tag}: bucket {r.get("bucket")!r} not in {BUCKETS}')
        if not isinstance(r.get('expected'), dict) or not r['expected']:
            problems.append(f'{tag}: expected must be a non-empty object')
        if not _PROVENANCE_RE.match(str(r.get('provenance') or '')):
            problems.append(f'{tag}: provenance {r.get("provenance")!r} must be '
                            f'"rep:<not-fit status>" or "machine-seeded YYYY-MM-DD"')
        if not isinstance(r.get('reviewed'), bool):
            problems.append(f'{tag}: reviewed must be true/false')
        if r.get('reviewed') is True and not str(r.get('review_note') or '').strip():
            problems.append(f'{tag}: a reviewed row needs a review_note saying why')
        if not isinstance(r.get('name'), str):
            problems.append(f'{tag}: name must be a string')
        ex = r.get('description_excerpt')
        if not isinstance(ex, str) or len(ex) > EXCERPT_CHARS:
            problems.append(f'{tag}: description_excerpt must be a string of <= {EXCERPT_CHARS} chars')
        for s in _strings(r):
            if EMAIL_RE.search(s):
                problems.append(f'{tag}: contains an email address')
                break
            if PHONE_RE.search(s):
                problems.append(f'{tag}: contains a phone number')
                break
        if r.get('account_key') != account_key(r.get('name')):
            problems.append(f'{tag}: account_key {r.get("account_key")!r} != '
                            f'gates.account_key({r.get("name")!r}) = {account_key(r.get("name"))!r}')
        key = (r.get('bucket'), r.get('account_key'), r.get('title'))
        if key in seen:
            problems.append(f'{tag}: duplicate (bucket, account_key, title)')
        seen.add(key)
    assert not problems, '\n'.join(problems)


def test_no_rep_decided_rows_in_the_public_file():
    """Privacy (review 2026-09-08 (Phase 4)): the repo must not publish which
    accounts the rep is actively pursuing — no row may carry 'Picked Up' /
    'On Rep TAL' in any field."""
    leaks = [_id(r) for r in ROWS if isinstance(r, dict) and bgs.is_rep_decided_row(r)]
    assert not leaks, f'rep-decided rows in the public golden file: {leaks}'
    for r in ROWS:
        for s in _strings(r):
            assert s not in REP_DECIDED_STATUSES, f'{_id(r)}: carries the status {s!r}'


# ── bucket (a): rep dispositions ────────────────────────────────────────────
@_param('rep_disposition')
def test_rep_disposition(row):
    status = row['expected'].get('rep_status')
    assert status in PUBLIC_REP_STATUSES, _why(
        row, f'status {status!r} is not a not-fit status enrichment honours (decided statuses stay private)')
    assert row['provenance'] == f'rep:{status}', _why(row, 'provenance must be rep:<status>')
    # the rep's verdict is keyed on gates.account_key — the name must keep mapping to it
    assert account_key(row['name']) == row['account_key'], _why(row, 'account_key drifted')


# ── bucket (b): entity shape ────────────────────────────────────────────────
@_param('entity_shape')
def test_entity_shape(row):
    hit, kind = is_non_operating_entity(row['name'])
    assert (hit, kind) == (True, row['expected']['kind']), _why(
        row, f'is_non_operating_entity → {(hit, kind)}, expected (True, {row["expected"]["kind"]!r})')


# ── bucket (c): structured SEC verdict ──────────────────────────────────────
@_param('structured')
def test_structured(row):
    got = structured_verdict({'description': row['description_excerpt'],
                              'title': row.get('title') or '',
                              'source_url': row.get('source_url') or ''})
    exp = row['expected']
    assert got.get('verdict') == exp['verdict'], _why(row, f'verdict {got.get("verdict")!r} != {exp["verdict"]!r}')
    assert (got.get('reason') or '') == (exp.get('reason') or ''), _why(
        row, f'reason {got.get("reason")!r} != {exp.get("reason")!r}')
    assert (got.get('revenue_segment') or None) == (exp.get('revenue_segment') or None), _why(
        row, f'revenue_segment {got.get("revenue_segment")!r} != {exp.get("revenue_segment")!r}')


# ── bucket (d): HQ out of territory ─────────────────────────────────────────
@_param('hq_out_of_territory')
def test_hq_out_of_territory(row):
    assert hq_territory_status(row['hq']) == 'out', _why(
        row, f'hq_territory_status({row["hq"]!r}) = {hq_territory_status(row["hq"])!r}, expected out')
    assert hq_state_code(row['hq']) == row['expected'].get('hq_state'), _why(
        row, f'hq_state_code({row["hq"]!r}) = {hq_state_code(row["hq"])!r} != {row["expected"].get("hq_state")!r}')


# ── bucket (e): verified accounts ───────────────────────────────────────────
@_param('verified')
def test_verified(row):
    exp, zi = row['expected'], row.get('zi_subindustry')
    assert hq_territory_status(row['hq']) == 'in', _why(
        row, f'hq_territory_status({row["hq"]!r}) = {hq_territory_status(row["hq"])!r}, expected in')
    assert hq_state_code(row['hq']) == exp.get('hq_state'), _why(
        row, f'hq_state_code({row["hq"]!r}) = {hq_state_code(row["hq"])!r} != {exp.get("hq_state")!r}')
    assert zi == exp.get('zi_subindustry'), _why(row, 'zi_subindustry column and expected disagree')
    assert zi in ZI_SUBINDUSTRIES, _why(row, f'{zi!r} is not a ZoomInfo subindustry label')
    assert zi in ZI_IN_VERTICAL, _why(row, f'{zi!r} is not a fit (ZI_NOT_A_FIT) — a verified row cannot carry it')
    assert ZI_SUBINDUSTRIES[zi] == exp.get('vertical'), _why(
        row, f'vertical {ZI_SUBINDUSTRIES[zi]!r} != {exp.get("vertical")!r}')


# ── bucket (f): finance-leader hire titles ──────────────────────────────────
@_param('finance_leader_title')
def test_finance_leader_title(row):
    """The stored event_type is the ground truth (review 2026-09-08 (Phase
    4)): cfo_hire → 'cfo'; executive_hire → 'exec', or None for a
    non-finance executive hire. finance_seat_open (Adzuna postings) is not a
    title the detector types in production and may not appear here."""
    et, want = row.get('event_type'), row['expected'].get('hire_kind')
    assert et in HIRE_EVENT_TYPES, _why(row, f'event_type {et!r} is not a title-typed hire (finance_seat_open is excluded)')
    assert want in HIRE_KIND_BY_EVENT_TYPE[et], _why(
        row, f'expected hire_kind {want!r} contradicts the stored event_type {et!r}')
    got = finance_leader_hire_kind(row.get('title') or '', (row.get('description_excerpt') or '')[:BODY_HEAD_CHARS])
    assert got == want, _why(row, f'finance_leader_hire_kind → {got!r}, expected {want!r}')


def test_finance_leader_bucket_flips_on_a_verb_list_revert():
    """Soundness, not correctness: enough rows must depend on the strong
    hire verbs (names / appoints / taps / promotes …) that reverting the verb
    list would fail the golden set. Blanking the verbs must flip at least
    MIN_VERB_ONLY_ROWS rows."""
    rows = _rows('finance_leader_title')
    flipped = [
        _id(r) for r in rows
        if finance_leader_hire_kind(_STRONG_VERB_RE.sub(' ', r.get('title') or ''),
                                    _STRONG_VERB_RE.sub(' ', (r.get('description_excerpt') or '')[:BODY_HEAD_CHARS]))
        != r['expected'].get('hire_kind')
    ]
    assert len(flipped) >= MIN_VERB_ONLY_ROWS, (
        f'only {len(flipped)} of {len(rows)} finance_leader_title rows depend on the hire-verb list '
        f'(need >= {MIN_VERB_ONLY_ROWS}) — the bucket would not notice a verb revert; re-export')


# ── bucket (g): tombstone reasons ───────────────────────────────────────────
@_param('tombstone_reason')
def test_tombstone_reason(row):
    prefix = row['expected'].get('reason_prefix')
    if prefix == 'bad_company_name':
        # a blank name is rejected trivially and pins nothing (review 2026-09-08 (Phase 4))
        assert (row.get('name') or '').strip(), _why(row, 'bad_company_name rows need a non-blank name')
        ctx = f'{row.get("title") or ""} {row.get("description_excerpt") or ""}'
        assert is_bad_company_name(row['name'], ctx) is True, _why(
            row, f'is_bad_company_name({row["name"]!r}) is now False')
    elif prefix == 'no_workable_account':
        roles = row['expected'].get('roles') or []
        assert roles, _why(row, 'no roles recorded')
        workable = [r for r in roles if str(r).lower() in WORKABLE_ROLES]
        assert not workable, _why(row, f'roles {workable} are workable now (WORKABLE_ROLES changed?)')
    else:
        pytest.fail(_why(row, f'unknown reason_prefix {prefix!r}'))


# ── import weight: the golden code must stay CI-light ───────────────────────
def test_golden_code_never_imports_enrichment_scout():
    """review 2026-09-08 (Phase 4): the golden test, the exporter and the
    pure helper module must not import the Mac-side enrichment_scout — a
    future Mac-only top-level import there would break the CI test job. The
    one comparison test below imports it under importorskip only."""
    for path in (os.path.abspath(__file__), EXPORTER_PATH, STRUCTURED_PATH):
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        hits = _IMPORTS_ENRICHMENT_RE.findall(src)
        assert not hits, f'{os.path.relpath(path, REPO)} imports enrichment_scout at module level: {hits}'


SAMPLE_SEC_EVENTS = (
    {'title': 'SEC 8-K Item 5.02 (Departure/Election of Directors or Officers) — ACME GOLD CORP',
     'source_url': 'https://www.sec.gov/Archives/edgar/data/1/0001-26-1.htm',
     'description': 'Acme Gold Corp filed an 8-K. SIC: 1040 (Gold and Silver Ores).'},
    {'title': 'SEC Form D — ACME ACQUISITION CORP II',
     'source_url': 'https://www.sec.gov/Archives/edgar/data/2/0002-26-2.htm',
     'description': 'SIC: 6770 (Blank Checks). Total offering: $50,000,000. SPAC: yes.'},
    {'title': 'SEC Form D — ACME CREDIT FUND III LP',
     'source_url': 'https://www.sec.gov/Archives/edgar/data/3/0003-26-3.htm',
     'description': 'Form D industry group: Pooled Investment Fund. Total offering: $25,000,000.'},
    {'title': 'SEC Form D — ACME SOFTWARE INC',
     'source_url': 'https://www.sec.gov/Archives/edgar/data/4/0004-26-4.htm',
     'description': 'Form D industry group: Other Technology. Declared revenue: $1 - $1,000,000. '
                    'Total offering: $2,500,000.'},
    {'title': 'SEC Form D — ACME LENDING LLC',
     'source_url': 'https://www.sec.gov/Archives/edgar/data/5/0005-26-5.htm',
     'description': 'Form D industry group: Commercial Banking. Declared revenue: $5,000,001 - '
                    '$25,000,000. Total offering: $12,000,000.'},
    {'title': 'Acme Names Jane Doe CFO',
     'source_url': 'https://www.prnewswire.com/news-releases/acme-1.html',
     'description': 'SIC: 6770 mentioned outside sec.gov is ignored. Form D industry group: x.'},
)


def test_structured_agrees_with_enrichment_scout():
    """While enrichment_scout still carries its own copies, the pure module
    and it must agree on six sample descriptions and on every vocabulary
    (the enrichment owner switches to a re-import from
    src.pipeline.structured; this test then compares a value with itself)."""
    es = pytest.importorskip('enrichment_scout')
    verdicts = set()
    for ev in SAMPLE_SEC_EVENTS:
        ours, theirs = structured_verdict(dict(ev)), es._structured_verdict(dict(ev))
        assert ours == theirs, f'{ev["title"]}: structured={ours} enrichment={theirs}'
        verdicts.add(ours['verdict'])
    assert len(verdicts) >= 3, f'samples exercise too few branches: {verdicts}'
    assert dict(es.ZI_SUBINDUSTRIES) == dict(ZI_SUBINDUSTRIES)
    assert set(es.ZI_IN_VERTICAL) == set(ZI_IN_VERTICAL)
    assert set(es.ZI_NOT_A_FIT) == set(ZI_NOT_A_FIT)
    assert list(es.WORKABLE_ROLES) == list(WORKABLE_ROLES)
    assert list(es.PRIMARY_ROLE_ORDER) == list(PRIMARY_ROLE_ORDER)
    assert set(es.REP_NOT_FIT_STATUSES) == set(REP_NOT_FIT_STATUSES)
    assert set(es.REP_DECIDED_STATUSES) == set(REP_DECIDED_STATUSES)
    cd = [{'name': 'Beta', 'role': 'target'}, {'name': 'Acme', 'role': 'acquirer'}]
    assert es.pick_primary(cd) == pick_primary(cd) == {'name': 'Acme', 'role': 'acquirer'}
    for ev in ({'source': 'sec_iapd'}, {'source_url': 'https://adviserinfo.sec.gov/firm/summary/1'},
               {'source': 'prnewswire', 'source_url': 'https://www.sec.gov/x'}, {}):
        assert es._is_iapd_event(ev) == is_iapd_event(ev), ev


# ── CI shape ────────────────────────────────────────────────────────────────
def test_ci_shape():
    """review 2026-09-08 (Phase 4): the golden/gate/config tests run in a
    SEPARATE `tests` job that is skipped on the schedule and needs nothing;
    the scrape job never runs pytest and keeps its pre-Phase-4 steps, so a
    golden flip can never stop a scheduled scrape or the Supabase sync."""
    import yaml
    with open(WORKFLOW_PATH, encoding='utf-8') as fh:
        wf = yaml.safe_load(fh)
    on = wf.get('on', wf.get(True))            # PyYAML reads the bare key `on` as True
    assert 'schedule' in on and 'workflow_dispatch' in on and 'push' in on
    jobs = wf['jobs']
    assert set(jobs) == {'tests', 'scrape'}
    tests, scrape = jobs['tests'], jobs['scrape']
    assert 'needs' not in tests and 'needs' not in scrape
    assert str(tests.get('if', '')).replace(' ', '') == "github.event_name!='schedule'"
    # a push runs only the tests — the scrape keeps schedule + workflow_dispatch, as it always had
    assert str(scrape.get('if', '')).replace(' ', '') == "github.event_name!='push'"
    uses = [s['uses'].split('@')[0] for s in tests['steps'] if s.get('uses')]
    assert uses[:2] == ['actions/checkout', 'actions/setup-python']
    runs = ' '.join(s.get('run') or '' for s in tests['steps'])
    assert 'pytest>=8,<10' in runs and 'requests' in runs and 'PyYAML' in runs
    assert re.search(r'python -m pytest .*tests/test_golden\.py tests/test_gates\.py tests/test_config\.py', runs)
    scrape_runs = ' '.join(s.get('run') or '' for s in scrape['steps'])
    assert 'pytest' not in scrape_runs, 'the scrape job must never run the test suite'
    names = [s.get('name') or s['uses'].split('@')[0] for s in scrape['steps']]
    # 'Configure email' removed 2026-09-08 (A.J.): Mattermost is the fleet's only
    # notification pathway, so the workflow no longer injects SMTP secrets. This
    # assertion is deliberately exact — a step appearing or vanishing unnoticed is
    # how the config.example.yaml indentation break went unseen for a full day.
    assert names == ['actions/checkout', 'actions/setup-python', 'Cache database', 'Install dependencies',
                     'Setup config', 'Verify module loads', 'Run scraper',
                     'Show database status', 'Sync to Supabase', 'actions/upload-artifact']
    assert not any('mail' in n.lower() for n in names), 'no email step may return'


# ── the exporter itself (no Supabase: a tiny in-memory query builder) ───────
class _Query:
    """Just enough of the supabase-py builder for the exporter's reads:
    select / order / range / execute plus the eq / in_ / like filters."""

    def __init__(self, rows):
        self.rows = list(rows)

    def select(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def range(self, a, b):
        self.rows = self.rows[a:b + 1]
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)

    def eq(self, col, v):
        self.rows = [r for r in self.rows if r.get(col) == v]
        return self

    def in_(self, col, vals):
        self.rows = [r for r in self.rows if r.get(col) in vals]
        return self

    def like(self, col, pat):
        rx = re.compile('^' + '.*'.join(re.escape(p) for p in pat.split('%')) + '$', re.S)
        self.rows = [r for r in self.rows if rx.match(str(r.get(col) or ''))]
        return self


class _Svc:
    def __init__(self, **tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.get(name, []))


def _ev(i, **kw):
    base = {'id': i, 'company_name': kw.pop('company_name', f'Company {i}'), 'title': '',
            'description': '', 'source': 'pr_newswire', 'source_url': f'https://example.test/{i}',
            'event_type': None, 'blocked_reason': None, 'verify_state': None, 'fit_verdict': None,
            'hq_state': None, 'zi_subindustry': None, 'sic': None, 'fit': None, 'companies_data': None}
    base.update(kw)
    return base


def test_exporter_tolerates_a_rep_row_without_an_event():
    """(a) hq_of / zi_of raised AttributeError on ev=None (review 2026-09-08 (Phase 4))."""
    assert bgs.hq_of(None) == '' and bgs.zi_of(None) is None and bgs.name_of(None) == ''
    row = bgs.make_row(None, 'rep_disposition', 'Acme Financial, Inc.', {'rep_status': 'Not a Fit'},
                       'rep:Not a Fit')
    assert (row['account_key'], row['hq'], row['zi_subindustry'], row['sic']) == ('acme financial', None, None, None)
    svc = _Svc(account_dispositions=[{'company_name': 'Acme Financial, Inc.', 'status': 'Not a Fit'}], events=[])
    tally = bgs.Tally()
    out = bgs.build_rep_dispositions(svc, '2026-09-08', tally)
    assert [r['name'] for r in out] == ['Acme Financial, Inc.'] and tally.notes['no event on file for context'] == 1


def test_exporter_rep_bucket_keeps_decided_statuses_private():
    """(g) only not-fit statuses are exported; Picked Up / On Rep TAL never are."""
    svc = _Svc(account_dispositions=[
        {'company_name': 'Alpha Co', 'status': 'Not a Fit'},
        {'company_name': 'Beta Co', 'status': 'Picked Up'},
        {'company_name': 'Gamma Co', 'status': 'On Rep TAL'},
        {'company_name': 'Delta Co', 'status': 'Out of Alignment'},
        {'company_name': 'Epsilon Co', 'status': 'NetSuite Customer'},
        {'company_name': 'Zeta Co', 'status': 'Maybe Later'},
    ], events=[])
    tally = bgs.Tally()
    out = bgs.build_rep_dispositions(svc, '2026-09-08', tally)
    assert sorted(r['name'] for r in out) == ['Alpha Co', 'Delta Co', 'Epsilon Co']
    assert all(r['provenance'] == f"rep:{r['expected']['rep_status']}" for r in out)
    assert sum(k for why, k in tally.skipped.items() if 'rep-decided' in why) == 2


def _fresh(bucket, name, expected, title=None, **extra):
    r = bgs.make_row(None, bucket, name, expected, extra.pop('provenance', 'machine-seeded 2026-09-08'))
    r['title'] = title
    for k, v in extra.items():
        r[k] = v
    return r


def test_exporter_merge_never_rewrites_expected_without_rebase():
    """(d) a changed expected is kept verbatim and reported; --rebase overwrites it."""
    old = dict(_fresh('entity_shape', 'Acme Fund III LP', {'non_operating': True, 'kind': 'fund_vehicle'},
                      description_excerpt='old excerpt', provenance='machine-seeded 2026-09-01'))
    new = _fresh('entity_shape', 'Acme Fund III LP', {'non_operating': True, 'kind': 'spac'},
                 description_excerpt='new excerpt')
    same = _fresh('entity_shape', 'Beta Fund LP', {'non_operating': True, 'kind': 'fund_vehicle'},
                  description_excerpt='refreshed excerpt')
    old_same = dict(same, description_excerpt='old excerpt', provenance='machine-seeded 2026-09-01')
    rows, stats, report = bgs.merge([old, old_same], {'entity_shape': [new, same]}, {'entity_shape': 40}, 250)
    by = {r['name']: r for r in rows}
    assert by['Acme Fund III LP']['expected'] == {'non_operating': True, 'kind': 'fund_vehicle'}
    assert by['Acme Fund III LP']['description_excerpt'] == 'old excerpt'      # inputs kept with the expected
    assert stats['expected_changed'] == 1 and len(report['expected_changed']) == 1
    assert 'Acme Fund III LP' in report['expected_changed'][0]
    # an unchanged expected still refreshes the inputs and keeps the provenance date
    assert by['Beta Fund LP']['description_excerpt'] == 'refreshed excerpt'
    assert by['Beta Fund LP']['provenance'] == 'machine-seeded 2026-09-01' and stats['refreshed'] == 1
    rows, stats, report = bgs.merge([old], {'entity_shape': [new]}, {'entity_shape': 40}, 250, rebase=True)
    assert rows[0]['expected'] == {'non_operating': True, 'kind': 'spac'}
    assert rows[0]['description_excerpt'] == 'new excerpt'
    assert rows[0]['provenance'] == 'machine-seeded 2026-09-08' and stats['rebased'] == 1
    # reviewed rows are never touched, rebase or not
    reviewed = dict(old, reviewed=True, review_note='A.J.: keep')
    rows, stats, _ = bgs.merge([reviewed], {'entity_shape': [new]}, {'entity_shape': 40}, 250, rebase=True)
    assert rows[0]['expected']['kind'] == 'fund_vehicle' and stats['kept'] == 1


def test_exporter_merge_derives_rep_provenance_from_the_fresh_status():
    """(b) refreshing expected.rep_status used to keep the stale provenance."""
    old = dict(_fresh('rep_disposition', 'Acme Co', {'rep_status': 'Not a Fit'}, provenance='rep:Not a Fit'))
    new = _fresh('rep_disposition', 'Acme Co', {'rep_status': 'Out of Alignment'}, provenance='rep:Out of Alignment')
    rows, stats, report = bgs.merge([old], {'rep_disposition': [new]}, {'rep_disposition': None}, 250)
    assert rows[0]['expected'] == {'rep_status': 'Not a Fit'} and rows[0]['provenance'] == 'rep:Not a Fit'
    assert stats['expected_changed'] == 1
    rows, stats, report = bgs.merge([old], {'rep_disposition': [new]}, {'rep_disposition': None}, 250, rebase=True)
    assert rows[0]['expected'] == {'rep_status': 'Out of Alignment'}
    assert rows[0]['provenance'] == 'rep:Out of Alignment'
    # even a stale provenance on an unchanged row is corrected from the status
    stale = dict(old, provenance='rep:Out of Alignment')
    rows, _, _ = bgs.merge([stale], {'rep_disposition': [dict(old)]}, {'rep_disposition': None}, 250)
    assert rows[0]['provenance'] == 'rep:Not a Fit'


def test_exporter_merge_caps_new_sampled_rows_per_run_and_exempts_rep_rows():
    """(c) --max-new-rows caps rows ADDED this run, never the file, and every
    not-fit rep verdict is added regardless."""
    existing = [dict(_fresh('verified', f'Old {i}', {'territory': 'in'})) for i in range(300)]
    fresh = {
        'rep_disposition': [_fresh('rep_disposition', f'Rep {i}', {'rep_status': 'Not a Fit'},
                                   provenance='rep:Not a Fit') for i in range(30)],
        'verified': [_fresh('verified', f'New {i}', {'territory': 'in'}) for i in range(10)],
        'entity_shape': [_fresh('entity_shape', f'Fund {i}', {'kind': 'fund_vehicle'}) for i in range(10)],
    }
    rows, stats, _ = bgs.merge(existing, fresh, {'rep_disposition': None, 'verified': 40, 'entity_shape': 40}, 5)
    buckets = {b: sum(1 for r in rows if r['bucket'] == b) for b in ('rep_disposition', 'verified', 'entity_shape')}
    assert len(rows) == 300 + 30 + 5                      # the 300 old rows never count against the cap
    assert buckets['rep_disposition'] == 30 and stats['added_sampled'] == 5 and stats['cap_hit'] >= 1
    assert buckets['verified'] + buckets['entity_shape'] == 300 + 5


def test_exporter_merge_drops_rep_decided_rows_already_in_the_file():
    """(g) a row exported before the privacy rule is removed on the next run."""
    picked = dict(_fresh('rep_disposition', 'Hot Prospect LLC', {'rep_status': 'Picked Up'}, provenance='rep:Picked Up'))
    tal = dict(_fresh('rep_disposition', 'Warm Prospect LLC', {'rep_status': 'On Rep TAL'}, provenance='rep:On Rep TAL'))
    keep = dict(_fresh('rep_disposition', 'Cold Co', {'rep_status': 'Not a Fit'}, provenance='rep:Not a Fit'))
    rows, stats, _ = bgs.merge([picked, tal, keep], {'rep_disposition': []}, {'rep_disposition': None}, 250)
    assert [r['name'] for r in rows] == ['Cold Co'] and stats['privacy_dropped'] == 2
    assert bgs.is_rep_decided_row(picked) and bgs.is_rep_decided_row(tal) and not bgs.is_rep_decided_row(keep)


def test_scrub_removes_phone_numbers_without_separators():
    """(e) the old phone regex needed separators: '8005551234' passed."""
    for s in ('8005551234', '18005551234', '+1 (800) 555-1234', '800.555.1234', '800 555 1234'):
        assert scrub(f'call {s} today') == 'call today', s
        assert PHONE_RE.search(s), s
    # identifiers that are NOT phone numbers survive (accession numbers, dollar amounts, CRDs, dates)
    for s in ('$22,500,000', '801-136602', 'CRD 342950', '2026-09-08', '0001234567-26-000123',
              'SIC: 6770', 'Total offering: $2,500,000.'):
        assert scrub(s) == s and not PHONE_RE.search(s), s


def test_exporter_skips_blank_names_in_bad_company_name():
    """(f) an empty name is rejected trivially and pins nothing."""
    svc = _Svc(events=[
        _ev(1, company_name='', title='Acme names CFO', blocked_reason='bad_company_name: blank'),
        _ev(2, company_name='   ', title='Beta names CFO', blocked_reason='bad_company_name: blank'),
        _ev(3, company_name='Jane Doe', title='Jane Doe joins Acme as CFO', blocked_reason='bad_company_name: person'),
    ])
    tally = bgs.Tally()
    out = bgs.build_tombstone_reasons(svc, '2026-09-08', tally, 20)
    assert all((r['name'] or '').strip() for r in out)
    assert tally.skipped['bad_company_name: blank name'] == 2


def test_exporter_finance_bucket_pins_the_stored_event_type():
    """(4) finance_seat_open is out; the stored event_type is the ground
    truth; a row the detector disagrees with is skipped; verb-only rows come
    first; an EA-to-the-CFO title never becomes a 'cfo' row."""
    assert bgs.HIRE_EVENT_TYPES == ('cfo_hire', 'executive_hire')
    svc = _Svc(events=[
        _ev(1, event_type='cfo_hire', title='Acme Names Jane Doe CFO'),                        # verb-only
        _ev(2, event_type='cfo_hire', title='Beta Appoints Jane Doe as CFO'),                  # phrase ("as CFO")
        _ev(3, event_type='cfo_hire', title='Gamma Reports Q2 Results; CFO comments'),         # detector: None → skipped
        _ev(4, event_type='executive_hire', title='Delta Names Jane Doe Corporate Controller'),  # verb-only exec
        _ev(5, event_type='executive_hire', title='Epsilon Names John Roe Chief Executive Officer'),  # negative
        _ev(6, event_type='executive_hire', title='Zeta Names Jane Doe CFO'),                  # cfo ≠ exec → skipped
        _ev(7, event_type='finance_seat_open', title='Eta hiring: Executive Assistant to the CFO', source='adzuna'),
        _ev(8, event_type='finance_seat_open', title='Theta hiring: Chief Financial Officer', source='adzuna'),
        _ev(9, event_type='cfo_hire', title='Iota Names Jane Doe Executive Assistant to the CFO'),  # detector None → skipped
    ])
    tally = bgs.Tally()
    out = bgs.build_finance_leader_titles(svc, '2026-09-08', tally, 40)
    got = {r['title']: r['expected']['hire_kind'] for r in out}
    assert got == {'Acme Names Jane Doe CFO': 'cfo', 'Beta Appoints Jane Doe as CFO': 'cfo',
                   'Delta Names Jane Doe Corporate Controller': 'exec',
                   'Epsilon Names John Roe Chief Executive Officer': None}
    assert all(r['event_type'] in bgs.HIRE_EVENT_TYPES for r in out)
    # verb-only rows come first (cfo / exec interleaved in hash order), the phrase row next, the negative last
    titles = [r['title'] for r in out]
    assert set(titles[:2]) == {'Acme Names Jane Doe CFO', 'Delta Names Jane Doe Corporate Controller'}
    assert titles[2:] == ['Beta Appoints Jane Doe as CFO', 'Epsilon Names John Roe Chief Executive Officer']
    assert tally.candidates == 7                       # the two Adzuna rows were never fetched
    assert sum(k for why, k in tally.skipped.items() if 'detector says' in why) == 3
    assert bgs.verb_is_only_signal('Acme Names Jane Doe CFO', '', 'cfo')
    assert not bgs.verb_is_only_signal('Beta Appoints Jane Doe as CFO', '', 'cfo')
