"""Tests for the accounts layer (src/pipeline/accounts.py, Phase 4 slice C1,
2026-09-08), migration 003 and scripts/backfill_accounts.py's pure planner.

No network: a fake Supabase client backs every client helper and records
its writes, so the dual-write / no-op / fail-soft paths are asserted on
exactly. Vocabularies copied into accounts.py (roles, rep statuses, the
ZI → vertical map, the dashboard's legacy key) are pinned against their
originals so the copies cannot drift."""
import copy
import logging
import os
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.pipeline import accounts as A  # noqa: E402
from src.pipeline import typed  # noqa: E402
from src.pipeline.accounts import (  # noqa: E402
    ACCOUNT_COLUMNS, ACCOUNT_STATUSES, DISPOSITION_REASONS, DISPOSITION_REASON_LABELS,
    GRADE_RANK, REP_DECIDED, REP_NOT_FIT, TRIGGER_PRIORITY, account_key,
    best_trigger_expired, build_account_row, entity_class_of, legacy_company_key,
    legacy_key_matches, load_dispositions, map_legacy_dispositions, merge_account,
    normalize_reason, normalize_status, probe_accounts, set_disposition, size_bucket_of,
    touch_secondary, upsert_account, vertical_of,
)
from scripts import backfill_accounts as bf  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ── Fake Supabase client ────────────────────────────────────────────────────
class _Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.filters, self._cols, self._op, self._payload = [], '*', 'select', None
        self._limit, self._range, self._negate, self._on_conflict = None, None, False, None

    @property
    def not_(self):
        self._negate = True
        return self

    def select(self, cols='*'):
        self._cols, self._op = cols, 'select'
        return self

    def eq(self, col, v):
        self.filters.append(('eq', col, v, self._negate)); self._negate = False
        return self

    def in_(self, col, vals):
        self.filters.append(('in', col, list(vals), self._negate)); self._negate = False
        return self

    def is_(self, col, v):
        self.filters.append(('is', col, v, self._negate)); self._negate = False
        return self

    def gte(self, col, v):
        self.filters.append(('gte', col, v, False))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self.filters.append(('order', col, desc, False))      # recorded, ignored by _match
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def upsert(self, payload, on_conflict='', **kw):
        self._op, self._payload, self._on_conflict = 'upsert', payload, on_conflict
        return self

    def update(self, payload):
        self._op, self._payload = 'update', payload
        return self

    def delete(self):
        self._op = 'delete'
        return self

    def _match(self, row):
        for op, col, v, neg in self.filters:
            if op == 'eq':
                ok = row.get(col) == v
            elif op == 'in':
                ok = row.get(col) in v
            elif op == 'is':
                ok = row.get(col) is None if v in ('null', None) else bool(row.get(col)) is bool(v)
            elif op == 'gte':
                ok = str(row.get(col) or '') >= str(v)
            else:
                ok = True
            if neg:
                ok = not ok
            if not ok:
                return False
        return True

    def execute(self):
        c = self.client
        if self.table_name in c.broken:
            raise Exception(f'simulated failure on {self.table_name}')
        if self.table_name not in c.tables:
            raise Exception(f"Could not find the table 'public.{self.table_name}' in the schema cache")
        rows = c.tables[self.table_name]
        c.calls.append((self.table_name, self._op, copy.deepcopy(self._payload), list(self.filters)))
        if self._op == 'select':
            cols = [x.strip() for x in self._cols.split(',') if x.strip()]
            declared = c.columns.get(self.table_name)
            if declared is not None and cols != ['*']:
                bad = [x for x in cols if x not in declared]
                if bad:
                    raise Exception(f'column {self.table_name}.{bad[0]} does not exist')
            out = [copy.deepcopy(r) for r in rows if self._match(r)]
            if self._range:
                a, b = self._range
                out = out[a:b + 1]
            if self._limit is not None:
                out = out[:self._limit]
            return SimpleNamespace(data=out)
        if self._op == 'upsert':
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            keyset = {tuple(sorted(p)) for p in payloads}
            assert len(keyset) == 1, 'postgrest needs one key set per batch'
            key = self._on_conflict
            for p in payloads:
                hit = next((r for r in rows if r.get(key) == p.get(key)), None)
                if hit is None:
                    rows.append(copy.deepcopy(p))
                else:
                    hit.update(copy.deepcopy(p))
            return SimpleNamespace(data=copy.deepcopy(payloads))
        if self._op == 'update':
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(copy.deepcopy(self._payload))
            return SimpleNamespace(data=copy.deepcopy(hit))
        if self._op == 'delete':
            keep = [r for r in rows if not self._match(r)]
            gone = [r for r in rows if self._match(r)]
            rows[:] = keep
            return SimpleNamespace(data=gone)
        raise AssertionError(self._op)


class FakeClient:
    """tables: {name: [rows]}; columns: {name: declared columns} (a select
    naming another column raises, like PostgREST); broken: tables whose
    every call raises."""
    def __init__(self, tables=None, columns=None, broken=()):
        self.tables = {k: list(v) for k, v in (tables or {}).items()}
        self.columns = dict(columns or {})
        self.broken = set(broken)
        self.calls = []

    def table(self, name):
        return _Query(self, name)

    def writes(self, table=None, op=None):
        return [(t, o, p) for t, o, p, _ in self.calls
                if o != 'select' and (table is None or t == table) and (op is None or o == op)]


def client_with_accounts(accounts=(), legacy=()):
    return FakeClient({'accounts': list(accounts), 'account_dispositions': list(legacy)},
                      columns={'accounts': set(ACCOUNT_COLUMNS)})


def client_without_accounts(legacy=()):
    return FakeClient({'account_dispositions': list(legacy)})


@pytest.fixture(autouse=True)
def _fresh_probe():
    A.reset_probe_cache()
    typed.reset_probe_cache()
    yield
    A.reset_probe_cache()
    typed.reset_probe_cache()


# ── Fixtures: an event, its chosen company, a secondary company ────────────
EVENT = {
    'id': 'e1', 'company_name': 'Acme Bancorp, Inc.', 'event_type': 'cfo_hire',
    'published_date': '2026-09-01', 'discovered_at': '2026-09-02T10:00:00',
    'enriched_at': '2026-09-02T11:00:00+00:00', 'grade': 'B', 'numeric_score': 7,
    'confidence_level': 'High', 'hashtags': ['#NewCFO'], 'grade_justification': 'math',
    'hq_state': 'MA', 'zi_subindustry': 'Banking', 'revenue_segment': 'MM',
    'classified_by': 'search', 'classification_confidence': 'High', 'enrich_attempts': 1,
    'retry_after': None,
}
COMPANY = {
    'name': 'Acme Bancorp', 'role': 'Hiring Company', 'hq': 'Boston, MA',
    'zi_subindustry': 'Banking', 'industry': 'Banking', 'size': '51-200', 'revenue': 'MM',
    'url': 'https://acme.com', 'linkedin': None, 'revenue_source': 'LinkedIn',
    'domain': 'acme.com', 'domain_method': 'clearbit', 'classified_by': 'search',
    'classification_confidence': 'High',
    'field_sources': {'hq': 'search', 'zi_subindustry': 'search', 'revenue': 'article', 'size': 'article'},
    'fit': {'verdict': 'pass', 'territory': 'in', 'revenue': 'in', 'vertical': 'in', 'reasons': []},
    'tal': {'grade': 'B', 'score': 7},
}
FIT = {'verdict': 'pass', 'territory': 'in', 'revenue': 'in', 'vertical': 'in',
       'zi_subindustry': 'Banking', 'account_name': 'Acme Bancorp', 'primary_name': 'Acme Bancorp',
       'reasons': []}
TARGET = {
    'name': 'Beta Credit Union', 'role': 'Target', 'hq': 'Portland, Maine',
    'zi_subindustry': 'Banking', 'industry': 'Credit unions', 'size': None, 'revenue': None,
    'url': None, 'linkedin': None,
    'fit': {'verdict': 'unverified', 'territory': 'in', 'revenue': 'unknown', 'vertical': 'in',
            'reasons': ['revenue unverified']},
}
GRADING = {'grade': 'B', 'numeric_score': 7, 'confidence': 'High', 'hashtags': ['#NewCFO'],
           'grade_justification': 'math'}


def base_row(**over):
    row = build_account_row(EVENT, COMPANY, FIT, grading=GRADING, now=NOW)
    row.update(over)
    return row


# ── Vocabularies pinned to their originals ──────────────────────────────────
def test_vocabularies_match_enrichment_scout():
    import enrichment_scout as es
    assert A.ZI_VERTICALS == es.ZI_SUBINDUSTRIES
    assert tuple(A.WORKABLE_ROLES) == tuple(es.WORKABLE_ROLES)
    assert REP_NOT_FIT == es.REP_NOT_FIT_STATUSES
    assert REP_DECIDED == es.REP_DECIDED_STATUSES
    assert set(ACCOUNT_STATUSES) == REP_NOT_FIT | REP_DECIDED


def test_vocabularies_match_dashboard():
    warnings.simplefilter('ignore')
    for name in ('streamlit', 'streamlit.runtime',
                 'streamlit.runtime.scriptrunner_utils.script_run_context',
                 'streamlit.runtime.caching.cache_data_api'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    import dashboard as d
    assert list(ACCOUNT_STATUSES) == list(d.ACCOUNT_STATUSES)
    for name in ('Agfa-Gevaert', 'SFA, LLC dba Swamp Fox Agency', 'CNL Strategic Residential Credit, Inc.',
                 'Long Island Select Healthcare Inc.', 'Acme Bancorp, Inc.', 'The Beta Co.', ''):
        # the v1 normalizer that wrote today's legacy rows, kept by the dashboard as its fallback
        assert legacy_company_key(name) == d._legacy_account_key(name), name
        # and the dashboard's live lookups now use the pipeline's ONE key
        assert d._account_key(name) == account_key(name), name
    for zi in list(A.ZI_VERTICALS) + ['OTHER', None, '']:
        assert (vertical_of(zi) or d.UNKNOWN_VERTICAL) == d.vertical_of(zi)


def test_contract_constants():
    assert ACCOUNT_STATUSES == ('Picked Up', 'On Rep TAL', 'NetSuite Customer', 'Out of Alignment', 'Not a Fit')
    assert DISPOSITION_REASONS == ('wrong_vertical', 'out_of_territory', 'too_big', 'too_small',
                                   'not_a_trigger', 'duplicate', 'existing_customer', 'other')
    assert list(DISPOSITION_REASON_LABELS.values()) == ['Wrong vertical', 'Out of territory', 'Too big',
                                                        'Too small', 'Not a real trigger', 'Duplicate',
                                                        'Already a customer', 'Other']
    assert TRIGGER_PRIORITY == ('cfo_hire', 'finance_seat_open', 'merger_acquisition', 'funding',
                                'expansion', 'executive_hire', 'stable_target', 'other')
    assert GRADE_RANK == {'A': 0, 'B': 1, 'C': 2, 'D': 3}
    assert A.REASON_REQUIRED_STATUSES == {'Not a Fit', 'Out of Alignment'}
    assert len(ACCOUNT_COLUMNS) == 44 and ACCOUNT_COLUMNS[0] == 'account_key'
    assert ACCOUNT_COLUMNS.index('seen_event_ids') == ACCOUNT_COLUMNS.index('event_count') + 1
    assert A.SEEN_EVENT_IDS_MAX == 50 and A.LEGACY_REASON_PREFIX == 'reason='
    assert set(A.DISPOSITION_COLUMNS) | set(A.GRADE_COLUMNS) | set(A.TRIGGER_COLUMNS) <= set(ACCOUNT_COLUMNS)
    assert A.account_key is account_key                     # re-export of gates.account_key


def test_migration_sql_matches_column_inventory():
    sql = open(os.path.join(REPO, 'supabase', 'migrations', '003_accounts.sql')).read()
    body = sql.split('CREATE TABLE IF NOT EXISTS public.accounts (', 1)[1].split('\n);', 1)[0]
    cols = [m.group(1) for m in re.finditer(r'^\s*([a-z_]+)\s+(TEXT|JSONB|INTEGER|TIMESTAMPTZ|BOOLEAN)\b',
                                            body, re.MULTILINE)]
    assert cols == list(ACCOUNT_COLUMNS)
    assert 'ALTER TABLE public.accounts ENABLE ROW LEVEL SECURITY' in sql
    assert 'CREATE POLICY' not in sql                       # service role only
    assert sql.count('CREATE TABLE') == 1 and 'DROP' not in sql.upper().replace('DROPPED', '')
    for stmt in re.findall(r'^(CREATE (?:TABLE|INDEX)[^\n]*)', sql, re.MULTILINE):
        assert 'IF NOT EXISTS' in stmt, stmt
    for idx in ('verify_state', 'grade', 'disposition', 'last_event_at DESC', 'hq_state'):
        assert f'ON public.accounts ({idx})' in sql
    assert 'information_schema.columns' in sql and "table_name = 'accounts'" in sql
    # no database-side automation: the app stamps updated_at on every write
    # (review 2026-09-08 (Phase 4): this used to be '... or True' — a tautology
    # kept because the updated_at comment contained the word "trigger")
    assert not re.search(r'CREATE\s+(OR\s+REPLACE\s+)?(TRIGGER|FUNCTION)', sql, re.IGNORECASE)
    assert re.search(r'^\s*seen_event_ids\s+JSONB', body, re.MULTILINE)
    assert f'you should see {len(ACCOUNT_COLUMNS)} rows' in sql


# ── Small helpers ───────────────────────────────────────────────────────────
@pytest.mark.parametrize('size,bucket', [
    ('1-50', '11-50'), ('51-200', '51-200'), ('1,001-5,000', '1001-5000'), ('10000+', '10000+'),
    ('10,000+', '10000+'), ('5001-10000', '5001-10000'), ('2-10', '1-10'), ('11-20', '11-50'),
    ('101-200', '51-200'), ('500 employees', '201-500'), ('5000+', '5001-10000'),
    (None, None), ('', None), ('unknown', None), ('null', None),
])
def test_size_bucket_of(size, bucket):
    assert size_bucket_of(size) == bucket


@pytest.mark.parametrize('name,descriptor,registry,expected', [
    ('Acme Bancorp', '', None, 'operating'),
    ('Cantor Equity Partners II, Inc.', '', None, 'fund_vehicle'),
    ('Constitution Capital Horizon Advisor, LP', '', 'sec_iapd', 'operating'),   # registry exemption
    ('Constitution Capital Horizon Advisor, LP', '', None, 'fund_vehicle'),
    ('Springfield Acquisition Corp', '', 'sec_iapd', 'spac'),                     # only fund_vehicle is exempt
    ('City of Boston', '', None, 'government'),
    ('Lincoln Public Schools', '', None, 'k12'),
    ('', '', None, 'operating'),
])
def test_entity_class_of(name, descriptor, registry, expected):
    assert entity_class_of(name, descriptor, registry) == expected


def test_vertical_of():
    assert vertical_of('Banking') == 'Financial Services'
    assert vertical_of(' K-12 Schools ') == 'Nonprofits & Organizations'
    assert vertical_of('Real Estate') == 'Consumer Services'
    assert vertical_of('OTHER') is None and vertical_of(None) is None and vertical_of('') is None
    assert A.VERTICALS == ('Financial Services', 'Nonprofits & Organizations', 'Consumer Services')


def test_normalizers():
    assert normalize_status('Picked Up') == 'Picked Up' and normalize_status(' not a fit ') == 'Not a Fit'
    assert normalize_status(None) is None and normalize_status('') is None and normalize_status('—') is None
    with pytest.raises(ValueError):
        normalize_status('Maybe')
    assert normalize_reason('wrong_vertical') == 'wrong_vertical'
    assert normalize_reason('Wrong vertical') == 'wrong_vertical'
    assert normalize_reason('Already a customer') == 'existing_customer'
    assert normalize_reason('Not a real trigger') == 'not_a_trigger'
    assert normalize_reason(None) is None and normalize_reason('') is None
    with pytest.raises(ValueError):
        normalize_reason('because')


def test_best_trigger_expired():
    assert best_trigger_expired({'best_trigger_type': 'cfo_hire', 'best_trigger_at': '2026-06-01'}, NOW)
    assert not best_trigger_expired({'best_trigger_type': 'cfo_hire', 'best_trigger_at': '2026-08-01'}, NOW)
    assert not best_trigger_expired({'best_trigger_type': 'merger_acquisition', 'best_trigger_at': '2026-06-01'}, NOW)
    assert best_trigger_expired({'best_trigger_type': 'cfo_hire', 'graded_at': '2026-01-01'}, NOW)   # fallback
    assert not best_trigger_expired({'best_trigger_type': 'cfo_hire'}, NOW)                         # undated: never
    assert not best_trigger_expired(None, NOW)


# ── build_account_row ───────────────────────────────────────────────────────
def test_build_row_operating_account_shape():
    row = build_account_row(EVENT, COMPANY, FIT, grading=GRADING, now=NOW)
    assert None not in row.values()                            # fill-only: no None facts, ever
    assert set(row) <= set(ACCOUNT_COLUMNS)
    assert not set(row) & set(A.DISPOSITION_COLUMNS)           # rep-owned, never emitted here
    assert row['account_key'] == 'acme bancorp' == account_key(row['canonical_name'])
    assert row['canonical_name'] == 'Acme Bancorp' and row['aliases'] == ['Acme Bancorp, Inc.']
    assert row['entity_class'] == 'operating'
    assert (row['hq'], row['hq_state'], row['in_territory']) == ('Boston, MA', 'MA', 'in')
    assert (row['zi_subindustry'], row['vertical'], row['industry']) == ('Banking', 'Financial Services', 'Banking')
    assert (row['revenue_segment'], row['size_bucket']) == ('MM', '51-200')
    assert (row['domain'], row['domain_method']) == ('acme.com', 'clearbit')
    assert (row['fit_verdict'], row['verify_state']) == ('pass', 'verified')
    assert (row['classified_by'], row['classification_confidence'], row['enrich_attempts']) == ('search', 'High', 1)
    assert 'retry_after' not in row
    assert (row['grade'], row['numeric_score'], row['confidence_level'], row['hashtags'],
            row['grade_justification']) == ('B', 7, 'High', ['#NewCFO'], 'math')
    assert (row['graded_event_id'], row['graded_at']) == ('e1', '2026-09-02T11:00:00+00:00')
    assert (row['best_trigger_type'], row['best_trigger_at'], row['best_trigger_event_id']) == \
        ('cfo_hire', '2026-09-01T00:00:00+00:00', 'e1')
    assert row['event_count'] == 1 and row['active'] is True
    assert row['first_seen'] == row['last_seen'] == '2026-09-02T10:00:00+00:00'
    assert row['last_event_at'] == '2026-09-01T00:00:00+00:00'
    firm = row['firmographics']
    assert firm['url'] == 'https://acme.com' and firm['revenue_source'] == 'LinkedIn'
    assert firm['field_sources'] == COMPANY['field_sources']
    assert not {'name', 'role', 'fit', 'tal', 'linkedin'} & set(firm)   # per-event keys / Nones dropped


def test_build_row_fund_vehicle_entity_class_and_fail():
    co = {'name': 'Cantor Equity Partners II, Inc.', 'role': 'Primary', 'hq': 'New York, NY',
          'fit': {'verdict': 'fail', 'territory': 'n/a', 'revenue': 'n/a', 'vertical': 'out',
                  'reasons': ['entity_shape:fund_vehicle']}}
    fit = {'verdict': 'fail', 'territory': 'n/a', 'revenue': 'n/a', 'vertical': 'out',
           'account_name': 'Cantor Equity Partners II, Inc.'}
    ev = dict(EVENT, id='e9', company_name='Cantor Equity Partners II, Inc.', grade=None, hq_state=None,
              zi_subindustry=None, revenue_segment=None, classified_by=None, classification_confidence=None)
    row = build_account_row(ev, co, fit, now=NOW)
    assert row['entity_class'] == 'fund_vehicle'
    assert (row['fit_verdict'], row['verify_state']) == ('fail', 'not_fit')
    assert row['hq_state'] == 'NY' and row['in_territory'] == 'in'      # territory read from the HQ, 'n/a' ignored
    assert 'vertical' not in row and 'zi_subindustry' not in row
    assert 'grade' not in row and 'graded_event_id' not in row
    assert None not in row.values()


def test_build_row_vertical_mapping_and_unknowns():
    for zi, label in (('Real Estate', 'Consumer Services'), ('Libraries', 'Nonprofits & Organizations')):
        row = build_account_row(EVENT, dict(COMPANY, zi_subindustry=zi), FIT, grading=GRADING, now=NOW)
        assert row['vertical'] == label
    row = build_account_row(dict(EVENT, zi_subindustry=None), dict(COMPANY, zi_subindustry='OTHER'),
                            dict(FIT, zi_subindustry=None), grading=GRADING, now=NOW)
    assert row['zi_subindustry'] == 'OTHER' and 'vertical' not in row
    row = build_account_row(dict(EVENT, zi_subindustry=None), dict(COMPANY, zi_subindustry=None),
                            dict(FIT, zi_subindustry=None), grading=GRADING, now=NOW)
    assert 'zi_subindustry' not in row and 'vertical' not in row


def test_build_row_secondary_company_uses_its_own_fit_and_no_event_fallbacks():
    row = build_account_row(EVENT, TARGET, FIT, grading={}, now=NOW)
    assert row['account_key'] == 'beta credit union' and row['canonical_name'] == 'Beta Credit Union'
    assert row['aliases'] == []                               # the event's company_name is another account
    assert (row['fit_verdict'], row['verify_state']) == ('unverified', 'researched_ambiguous')
    assert (row['hq_state'], row['in_territory']) == ('ME', 'in')
    assert row['zi_subindustry'] == 'Banking' and row['vertical'] == 'Financial Services'
    # event-level typed columns describe the chosen account, not this one
    assert 'revenue_segment' not in row and 'classified_by' not in row and 'size_bucket' not in row
    assert row['enrich_attempts'] == 0
    assert 'grade' not in row and 'graded_event_id' not in row     # grading={} → facts + trigger only
    assert (row['best_trigger_type'], row['best_trigger_event_id']) == ('cfo_hire', 'e1')


def test_build_row_grading_shapes():
    # None / {} → NO grade, even though the event row carries one (a row read
    # before enrichment may hold a previous run's grade — enrichment passes
    # None for "no grade yet")
    for g in (None, {}):
        row = build_account_row(EVENT, COMPANY, FIT, grading=g, now=NOW)
        assert not set(row) & set(A.GRADE_COLUMNS) and row['best_trigger_event_id'] == 'e1'
    # the backfill asks for the event's own columns explicitly
    row = build_account_row(EVENT, COMPANY, FIT, grading=A.grading_from_event(EVENT), now=NOW)
    assert (row['grade'], row['numeric_score'], row['confidence_level']) == ('B', 7, 'High')
    # the per-company `tal` shape
    row = build_account_row(EVENT, COMPANY, FIT, grading={'grade': 'A', 'score': '9', 'confidence': 'medium',
                                                          'hashtags': '["#x"]', 'justification': 'j'}, now=NOW)
    assert (row['grade'], row['numeric_score'], row['confidence_level'], row['hashtags'],
            row['grade_justification']) == ('A', 9, 'Medium', ['#x'], 'j')
    # 'Unable to Grade' is not a grade; nor is a blank; grade columns then absent
    for g in ({'grade': 'Unable to Grade', 'numeric_score': 0}, {'grade': None}, {}):
        row = build_account_row(EVENT, COMPANY, FIT, grading=g, now=NOW)
        assert not set(row) & set(A.GRADE_COLUMNS), g
    row = build_account_row(dict(EVENT, grade='Unable to Grade'), COMPANY, FIT,
                            grading=A.grading_from_event(dict(EVENT, grade='Unable to Grade')), now=NOW)
    assert 'grade' not in row
    assert A.grading_from_event(None) == {'grade': None, 'numeric_score': None, 'confidence_level': None,
                                          'hashtags': None, 'grade_justification': None}


def test_build_row_trigger_live_false_keeps_account_but_no_trigger_or_grade():
    row = build_account_row(EVENT, COMPANY, FIT, grading=GRADING, now=NOW, trigger_live=False)
    assert not set(row) & (set(A.GRADE_COLUMNS) | set(A.TRIGGER_COLUMNS))
    assert row['event_count'] == 1 and row['verify_state'] == 'verified' and row['hq'] == 'Boston, MA'
    assert row['last_event_at'] == '2026-09-01T00:00:00+00:00'


def test_build_row_fallbacks_and_junk():
    # no company dict at all → keyed on fit.account_name / company_name; dates fall back to now
    row = build_account_row({'id': 'x', 'company_name': 'Acme Bancorp, Inc.', 'event_type': 'funding'}, {}, {}, now=NOW)
    assert row['account_key'] == 'acme bancorp' and row['canonical_name'] == 'Acme Bancorp, Inc.'
    assert row['first_seen'] == NOW_ISO and row['best_trigger_at'] == NOW_ISO and row['best_trigger_type'] == 'funding'
    assert 'verify_state' not in row and 'fit_verdict' not in row
    # the LLM's literal 'null' revenue is not a fact; an unknown event_type is 'other'
    row = build_account_row(dict(EVENT, event_type='weird', revenue_segment=None), dict(COMPANY, revenue='null'), FIT, now=NOW)
    assert 'revenue_segment' not in row and row['best_trigger_type'] == 'other'
    with pytest.raises(ValueError):
        build_account_row({'id': 'x'}, {}, {}, now=NOW)


# ── merge_account ───────────────────────────────────────────────────────────
def test_merge_new_account_is_the_incoming_row_minus_dispositions():
    inc = base_row(disposition='Not a Fit', disposition_reason='too_big')
    out = merge_account(None, inc, now=NOW)
    assert 'disposition' not in out and 'disposition_reason' not in out
    assert out['updated_at'] == NOW_ISO and out['active'] is True
    assert {k: v for k, v in out.items() if k not in ('updated_at', 'active')} == \
        {k: v for k, v in base_row().items() if k != 'active'}
    assert merge_account({}, inc, now=NOW)['account_key'] == 'acme bancorp'


def test_merge_facts_fill_only_and_provenance():
    ex = base_row()
    ex.update({'hq': 'Boston, MA', 'size_bucket': '51-200'})
    del ex['industry']
    # weaker (article) HQ cannot displace the search one; empty industry fills
    inc = build_account_row(dict(EVENT, id='e2'), dict(COMPANY, hq='Toronto, ON', industry='Banks',
                                                       field_sources={'hq': 'article'}), FIT, grading={}, now=NOW)
    out = merge_account(ex, inc, now=NOW)
    assert 'hq' not in out and 'hq_state' not in out and 'in_territory' not in out
    assert out['industry'] == 'Banks'
    # stronger (oracle) HQ replaces hq AND the state/territory read from it
    inc = build_account_row(dict(EVENT, id='e3'), dict(COMPANY, hq='Seattle, WA', field_sources={'hq': 'oracle'},
                                                       fit=dict(COMPANY['fit'], territory='out')),
                            FIT, grading={}, now=NOW)
    out = merge_account(ex, inc, now=NOW)
    assert (out['hq'], out['hq_state'], out['in_territory']) == ('Seattle, WA', 'WA', 'out')
    assert out['firmographics']['hq'] == 'Seattle, WA' and out['firmographics']['field_sources']['hq'] == 'oracle'
    # equal provenance: existing wins; unknown incoming never overwrites; stored 'unknown' is fillable
    inc = build_account_row(dict(EVENT, id='e4'), dict(COMPANY, hq='Denver, CO'), FIT, grading={}, now=NOW)
    assert 'hq' not in merge_account(ex, inc, now=NOW)
    ex2 = dict(ex, in_territory='unknown')
    assert merge_account(ex2, base_row(), now=NOW)['in_territory'] == 'in'
    assert 'canonical_name' not in merge_account(ex, base_row(canonical_name='ACME BANCORP'), now=NOW)
    assert 'in_territory' not in merge_account(ex, base_row(in_territory='unknown'), now=NOW)


def test_merge_never_emits_none_or_touches_dispositions():
    ex = base_row(disposition='Picked Up', disposition_reason=None, disposition_at='2026-08-01', active=False)
    inc = base_row(disposition='Not a Fit', disposition_reason='too_big', disposition_notes='x',
                   disposition_at=NOW_ISO, disposition_by='rep', active=True)
    inc['hq'] = None
    out = merge_account(ex, inc, now=NOW)
    assert not set(out) & set(A.DISPOSITION_COLUMNS) and 'active' not in out and 'created_at' not in out
    assert None not in out.values()


def test_merge_verify_state_never_downgrades():
    ex = base_row()                                                       # verified / pass
    staged = build_account_row(dict(EVENT, id='e2', grade=None), dict(COMPANY, zi_subindustry=None,
                               fit={'verdict': 'staged', 'territory': 'in', 'revenue': 'in', 'vertical': 'unknown'}),
                               dict(FIT, verdict='staged'), grading={}, now=NOW)
    out = merge_account(ex, staged, now=NOW)
    assert 'verify_state' not in out and 'fit_verdict' not in out
    ambiguous = base_row(verify_state='researched_ambiguous', fit_verdict='unverified', retry_after='2026-09-15T00:00:00+00:00')
    out = merge_account(ex, ambiguous, now=NOW)
    assert 'verify_state' not in out and 'retry_after' not in out
    # upgrades move, and carry verdict + retry schedule
    out = merge_account(dict(ex, verify_state='staged', fit_verdict='staged'), ambiguous, now=NOW)
    assert (out['verify_state'], out['fit_verdict'], out['retry_after']) == \
        ('researched_ambiguous', 'unverified', '2026-09-15T00:00:00+00:00')
    out = merge_account(dict(ex, verify_state='researched_ambiguous', fit_verdict='unverified'), base_row(), now=NOW)
    assert (out['verify_state'], out['fit_verdict']) == ('verified', 'pass')
    # a confirmed OUT beats an unknown, but not a confirmed IN
    out = merge_account(dict(ex, verify_state='staged', fit_verdict='staged'),
                        base_row(verify_state='not_fit', fit_verdict='fail'), now=NOW)
    assert (out['verify_state'], out['fit_verdict']) == ('not_fit', 'fail')
    assert 'verify_state' not in merge_account(ex, base_row(verify_state='not_fit', fit_verdict='fail'), now=NOW)
    # same rung, newer retry schedule
    ex_amb = dict(ex, verify_state='researched_ambiguous', fit_verdict='unverified', retry_after='2026-09-10T00:00:00+00:00')
    assert merge_account(ex_amb, ambiguous, now=NOW)['retry_after'] == '2026-09-15T00:00:00+00:00'
    # empty state fills; attempts take the max
    out = merge_account({k: v for k, v in ex.items() if k not in ('verify_state', 'fit_verdict')},
                        base_row(enrich_attempts=3), now=NOW)
    assert (out['verify_state'], out['fit_verdict'], out['enrich_attempts']) == ('verified', 'pass', 3)


def _graded(eid, grade, etype='cfo_hire', published='2026-09-01', score=7):
    ev = dict(EVENT, id=eid, event_type=etype, published_date=published, grade=grade, numeric_score=score)
    return build_account_row(ev, COMPANY, FIT, grading={'grade': grade, 'numeric_score': score,
                                                        'confidence': 'High', 'hashtags': ['#x'],
                                                        'grade_justification': f'why {grade}'}, now=NOW)


def test_merge_grade_only_when_better_regrade_or_expired():
    ex = _graded('e1', 'B')
    assert 'grade' not in merge_account(ex, _graded('e2', 'C', etype='funding'), now=NOW)   # worse: keep
    assert 'grade' not in merge_account(ex, _graded('e2', 'B', etype='funding'), now=NOW)   # equal: keep
    out = merge_account(ex, _graded('e2', 'A', etype='funding', score=9), now=NOW)          # better: take, with its bookkeeping
    assert (out['grade'], out['numeric_score'], out['graded_event_id'], out['grade_justification']) == ('A', 9, 'e2', 'why A')
    out = merge_account(ex, _graded('e1', 'D', score=2), now=NOW)                           # same event re-graded: take
    assert (out['grade'], out['numeric_score']) == ('D', 2)
    stale = dict(ex, best_trigger_at='2026-05-01T00:00:00+00:00', graded_at='2026-05-01T00:00:00+00:00')
    out = merge_account(stale, _graded('e2', 'C', etype='funding'), now=NOW)                # expired best trigger: take
    assert out['grade'] == 'C' and out['graded_event_id'] == 'e2'
    ungraded = build_account_row(dict(EVENT, id='e3', grade=None), COMPANY, FIT, grading={}, now=NOW)
    assert 'grade' not in merge_account(ex, ungraded, now=NOW)                              # nothing incoming: keep
    out = merge_account({k: v for k, v in ex.items() if k not in A.GRADE_COLUMNS}, _graded('e2', 'D'), now=NOW)
    assert out['grade'] == 'D'                                                              # empty: fill


def test_merge_best_trigger_priority_then_recency_then_expiry():
    ex = _graded('e1', 'B', etype='funding', published='2026-09-01')
    out = merge_account(ex, _graded('e2', 'C', etype='cfo_hire', published='2026-08-20'), now=NOW)
    assert (out['best_trigger_type'], out['best_trigger_event_id']) == ('cfo_hire', 'e2')       # higher priority, older
    out = merge_account(ex, _graded('e2', 'C', etype='executive_hire', published='2026-09-05'), now=NOW)
    assert 'best_trigger_type' not in out                                                     # lower priority, newer
    out = merge_account(ex, _graded('e2', 'C', etype='funding', published='2026-09-05'), now=NOW)
    assert (out['best_trigger_at'], out['best_trigger_event_id']) == ('2026-09-05T00:00:00+00:00', 'e2')  # same, newer
    out = merge_account(ex, _graded('e2', 'C', etype='funding', published='2026-08-25'), now=NOW)
    assert 'best_trigger_event_id' not in out                                                 # same, older
    stale = _graded('e1', 'B', etype='cfo_hire', published='2026-05-01')
    out = merge_account(stale, _graded('e2', 'C', etype='stable_target', published='2026-09-01'), now=NOW)
    assert out['best_trigger_type'] == 'stable_target'                                        # expired yields to live
    out = merge_account(ex, _graded('e2', 'A', etype='cfo_hire', published='2026-03-01'), now=NOW)
    assert 'best_trigger_type' not in out                                                     # expired incoming never displaces a live one
    out = merge_account(ex, _graded('e1', 'B', etype='cfo_hire', published='2026-09-01'), now=NOW)
    assert out['best_trigger_type'] == 'cfo_hire'                                             # same event refreshed in place
    bare = {k: v for k, v in ex.items() if k not in A.TRIGGER_COLUMNS}
    assert merge_account(bare, _graded('e2', 'C', etype='expansion'), now=NOW)['best_trigger_type'] == 'expansion'


def test_merge_event_count_and_dates():
    ex = base_row()
    newer = build_account_row(dict(EVENT, id='e2', published_date='2026-09-05', discovered_at='2026-09-06T08:00:00'),
                              COMPANY, FIT, grading={}, now=NOW)
    out = merge_account(ex, newer, now=NOW)
    assert out['event_count'] == 2
    assert out['last_event_at'] == '2026-09-05T00:00:00+00:00' and out['last_seen'] == '2026-09-06T08:00:00+00:00'
    assert 'first_seen' not in out
    older = build_account_row(dict(EVENT, id='e0', published_date='2026-01-05', discovered_at='2026-01-06T08:00:00'),
                              COMPANY, FIT, grading={}, now=NOW, trigger_live=False)
    out = merge_account(ex, older, now=NOW)
    assert out['first_seen'] == '2026-01-06T08:00:00+00:00' and 'last_seen' not in out and 'last_event_at' not in out
    # re-processing the graded / best-trigger event must not double count
    assert 'event_count' not in merge_account(ex, base_row(), now=NOW)
    assert 'event_count' not in merge_account(ex, newer, now=NOW, is_new_event=False)
    assert merge_account(ex, base_row(), now=NOW, is_new_event=True)['event_count'] == 2
    assert merge_account(dict(ex, event_count=None), newer, now=NOW)['event_count'] == 1
    # review 2026-09-08 (Phase 4): a THIRD event that is neither the graded
    # nor the best-trigger event, re-processed a week later — the old
    # "new = id not in (graded, best trigger)" rule counted it twice
    acc = dict(ex)
    acc.update(merge_account(acc, newer, now=NOW))                        # e2: best trigger now, count 2
    third = build_account_row(dict(EVENT, id='e3', event_type='executive_hire', published_date='2026-09-03',
                                   discovered_at='2026-09-04T08:00:00'), COMPANY, FIT, grading={}, now=NOW)
    acc.update(merge_account(acc, third, now=NOW))
    assert acc['event_count'] == 3 and acc['seen_event_ids'] == ['e1', 'e2', 'e3']
    assert acc['best_trigger_event_id'] == 'e2' and acc['graded_event_id'] == 'e1'   # e3 is neither
    again = merge_account(acc, third, now=NOW + timedelta(days=7))
    assert 'event_count' not in again and 'seen_event_ids' not in again
    # the override still works both ways
    assert merge_account(acc, third, now=NOW, is_new_event=True)['event_count'] == 4
    assert 'event_count' not in merge_account(acc, base_row(), now=NOW, is_new_event=False)


def test_merge_aliases_and_firmographics():
    ex = base_row()
    inc = build_account_row(dict(EVENT, id='e2', company_name='ACME Bancorp Inc'),
                            dict(COMPANY, name='Acme Bancorp Inc.', linkedin='https://li.com/acme', url=None,
                                 field_sources={'linkedin': 'search'}), FIT, grading={}, now=NOW)
    out = merge_account(ex, inc, now=NOW)
    assert out['aliases'] == ['Acme Bancorp, Inc.', 'Acme Bancorp Inc.', 'ACME Bancorp Inc']
    firm = out['firmographics']
    assert firm['linkedin'] == 'https://li.com/acme' and firm['url'] == 'https://acme.com'   # fill, never null
    assert firm['field_sources']['linkedin'] == 'search' and firm['field_sources']['hq'] == 'search'
    assert 'firmographics' not in merge_account(ex, base_row(), now=NOW)                    # nothing new → not rewritten


# ── probing + upsert_account ────────────────────────────────────────────────
def test_probe_absent_upsert_is_noop_false():
    c = client_without_accounts()
    assert probe_accounts(c) is False
    assert upsert_account(c, base_row(), now=NOW) is False
    assert c.writes() == []
    assert A.account_columns_present(c) == set()
    assert probe_accounts(None) is False and upsert_account(None, base_row()) is False


def test_probe_memoized_and_reset():
    c = client_without_accounts()
    assert probe_accounts(c) is False
    n = len(c.calls)
    assert probe_accounts(c) is False and len(c.calls) == n                 # memoized: no second probe
    A._probe['accounts'] = (False, A._probe['accounts'][1] - A.PROBE_NEGATIVE_TTL_S - 1)
    c.tables['accounts'] = []
    assert probe_accounts(c) is True                                        # negative answer expired → re-probed
    A.reset_probe_cache()
    c2 = client_with_accounts()
    assert probe_accounts(c2) is True
    m = len(c2.calls)
    assert probe_accounts(c2) is True and len(c2.calls) == m
    assert A.account_columns_present(c2) == set(ACCOUNT_COLUMNS)
    assert len(c2.calls) == m + 1                                          # one all-columns select
    assert A.account_columns_present(c2) == set(ACCOUNT_COLUMNS) and len(c2.calls) == m + 1


def test_columns_present_falls_back_to_per_column_probe():
    c = client_with_accounts()
    c.columns['accounts'] = set(ACCOUNT_COLUMNS) - {'disposition_by'}     # a partial table
    assert A.account_columns_present(c) == set(ACCOUNT_COLUMNS) - {'disposition_by'}


def test_upsert_writes_merged_payload_only_existing_columns():
    c = client_with_accounts()
    assert upsert_account(c, base_row(), now=NOW) is True
    (t, op, payload), = c.writes()
    assert (t, op) == ('accounts', 'upsert') and payload['account_key'] == 'acme bancorp'
    assert payload['grade'] == 'B' and payload['updated_at'] == NOW_ISO and payload['active'] is True
    stored = c.tables['accounts'][0]
    # a second event: partial payload — only what changed, on top of the stored row
    inc = build_account_row(dict(EVENT, id='e2', published_date='2026-09-05'), COMPANY, FIT,
                            grading=dict(GRADING, grade='A', numeric_score=9), now=NOW)
    assert upsert_account(c, inc, now=NOW + timedelta(hours=1)) is True
    _, _, payload = c.writes()[-1]
    assert set(payload) == {'account_key', 'grade', 'numeric_score', 'graded_event_id', 'best_trigger_at',
                            'best_trigger_event_id', 'event_count', 'seen_event_ids', 'last_event_at', 'updated_at'}
    assert stored['event_count'] == 2 and stored['grade'] == 'A' and stored['hq'] == 'Boston, MA'
    assert stored['seen_event_ids'] == ['e1', 'e2']
    # the same second event re-processed: nothing to count, no id to add
    assert upsert_account(c, inc, now=NOW + timedelta(hours=2)) is True
    _, _, payload = c.writes()[-1]
    assert 'event_count' not in payload and 'seen_event_ids' not in payload and stored['event_count'] == 2
    # `present` narrows the columns written
    c2 = client_with_accounts()
    assert upsert_account(c2, base_row(), present={'account_key', 'grade'}, now=NOW) is True
    assert set(c2.writes()[0][2]) == {'account_key', 'grade'}


def test_upsert_fail_soft(caplog):
    c = client_with_accounts()
    probe_accounts(c)
    A.account_columns_present(c)
    c.broken.add('accounts')
    with caplog.at_level(logging.WARNING):
        assert upsert_account(c, base_row(), now=NOW) is False
    assert 'accounts upsert failed' in caplog.text
    assert upsert_account(c, {'canonical_name': 'no key'}, now=NOW) is False


def test_touch_secondary():
    c = client_with_accounts()
    assert touch_secondary(c, TARGET, EVENT, FIT, now=NOW) is True
    row = c.tables['accounts'][0]
    assert row['account_key'] == 'beta credit union' and 'grade' not in row
    assert row['verify_state'] == 'researched_ambiguous' and row['best_trigger_event_id'] == 'e1'
    # not a workable role, not a failed fit, not nameless
    assert touch_secondary(c, dict(TARGET, role='Advisor'), EVENT, FIT, now=NOW) is False
    assert touch_secondary(c, dict(TARGET, fit={'verdict': 'fail'}), EVENT, FIT, now=NOW) is False
    assert touch_secondary(c, dict(TARGET, name=''), EVENT, FIT, now=NOW) is False
    assert len(c.tables['accounts']) == 1
    # the chosen account itself, when grading returned nothing: facts only, still no grade
    assert touch_secondary(c, COMPANY, EVENT, FIT, now=NOW) is True
    acme = next(r for r in c.tables['accounts'] if r['account_key'] == 'acme bancorp')
    assert 'grade' not in acme and acme['verify_state'] == 'verified' and acme['hq'] == 'Boston, MA'
    # ... and a later graded upsert fills the grade without touching the facts
    assert upsert_account(c, base_row(), now=NOW) is True
    assert acme['grade'] == 'B' and acme['event_count'] == 1
    assert touch_secondary(client_without_accounts(), TARGET, EVENT, FIT, now=NOW) is False


def test_load_account():
    c = client_with_accounts([base_row()])
    row = A.load_account(c, 'acme bancorp')
    assert row['canonical_name'] == 'Acme Bancorp' and row['grade'] == 'B'
    assert A.load_account(c, 'nobody') is None and A.load_account(c, '') is None
    assert A.load_account(None, 'acme bancorp') is None
    A.reset_probe_cache()
    assert A.load_account(client_without_accounts(), 'acme bancorp') is None


# ── legacy bridge ───────────────────────────────────────────────────────────
LEGACY = [
    {'company_key': 'cnl strategic residential credit', 'company_name': 'CNL Strategic Residential Credit, Inc.',
     'status': 'Picked Up', 'notes': None, 'updated_at': '2026-07-22T01:12:45+00:00'},
    {'company_key': 'agfa-gevaert', 'company_name': 'Agfa-Gevaert', 'status': 'Out of Alignment',
     'notes': None, 'updated_at': '2026-08-27T18:40:47+00:00'},
    {'company_key': 'sfa, llc dba swamp fox agency', 'company_name': 'SFA, LLC dba Swamp Fox Agency',
     'status': 'Picked Up', 'notes': None, 'updated_at': '2026-08-26T22:44:01+00:00'},
    {'company_key': 'palm beach north chamber', 'company_name': 'Palm Beach North Chamber',
     'status': 'Not a Fit', 'notes': 'chamber of commerce', 'updated_at': '2026-08-26T22:45:05+00:00'},
]


def test_legacy_key_matches():
    assert legacy_key_matches('Agfa-Gevaert', 'agfa-gevaert')                       # dashboard key
    assert legacy_key_matches('Agfa-Gevaert', 'agfa gevaert')                       # gates key
    assert legacy_key_matches('SFA, LLC dba Swamp Fox Agency', 'sfa, llc dba swamp fox agency')
    assert legacy_key_matches('CNL Strategic Residential Credit, Inc.', 'cnl strategic residential credit')
    assert not legacy_key_matches('Agfa-Gevaert', 'cleanaway')
    assert not legacy_key_matches('', 'x') and not legacy_key_matches('Acme', '')
    assert legacy_company_key('Acme Bancorp, Inc.') == 'acme bancorp' == account_key('Acme Bancorp, Inc.')
    assert legacy_company_key('Agfa-Gevaert') == 'agfa-gevaert' != account_key('Agfa-Gevaert')


def test_map_legacy_dispositions():
    rows = LEGACY + [{'company_key': 'zzz', 'company_name': None, 'status': 'Bogus'},
                     {'company_key': 'ghost co', 'company_name': '', 'status': 'Picked Up'},
                     {'company_key': '', 'company_name': '', 'status': 'Picked Up'},
                     {'company_key': 'x', 'company_name': 'X Co', 'status': ''}]
    m = map_legacy_dispositions(rows)
    by = {e['legacy_key']: e for e in m}
    assert by['cnl strategic residential credit']['match'] == 'exact'
    assert by['agfa-gevaert'] == {'account_key': 'agfa gevaert', 'legacy_key': 'agfa-gevaert', 'name': 'Agfa-Gevaert',
                                  'status': 'Out of Alignment', 'reason': None, 'notes': None,
                                  'at': '2026-08-27T18:40:47+00:00', 'match': 'renormalized', 'problem': None}
    assert by['sfa, llc dba swamp fox agency']['account_key'] == 'sfa llc dba swamp fox agency'
    assert by['palm beach north chamber']['notes'] == 'chamber of commerce'
    assert by['zzz']['match'] == 'unmatched' and 'unknown account status' in by['zzz']['problem']
    assert by['ghost co']['match'] == 'legacy_key' and by['ghost co']['account_key'] == 'ghost'   # ' co' is a suffix
    assert by['']['match'] == 'unmatched' and by['x']['problem'] == 'blank status'
    assert map_legacy_dispositions(None) == [] and map_legacy_dispositions([None]) [0]['match'] == 'unmatched'
    # review 2026-09-08 (Phase 4): the reason code rides in notes and is decoded
    # back out — a legacy row never loses the reason a rep entered
    enc = [{'company_key': 'e1', 'company_name': 'Enc One', 'status': 'Not a Fit', 'notes': 'reason=too_big | call in Q4'},
           {'company_key': 'e2', 'company_name': 'Enc Two', 'status': 'Not a Fit', 'notes': 'reason=wrong_vertical'},
           {'company_key': 'e3', 'company_name': 'Enc Three', 'status': 'Picked Up', 'notes': 'plain note | with pipe'},
           {'company_key': 'e4', 'company_name': 'Enc Four', 'status': 'Not a Fit', 'notes': 'reason=bogus | keep me'},
           {'company_key': 'e5', 'company_name': 'Enc Five', 'status': 'Not a Fit', 'notes': 'reason=Too small | a | b'}]
    by = {e['legacy_key']: e for e in map_legacy_dispositions(enc)}
    assert (by['e1']['reason'], by['e1']['notes']) == ('too_big', 'call in Q4')
    assert (by['e2']['reason'], by['e2']['notes']) == ('wrong_vertical', None)
    assert (by['e3']['reason'], by['e3']['notes']) == (None, 'plain note | with pipe')
    assert (by['e4']['reason'], by['e4']['notes']) == (None, 'reason=bogus | keep me')   # unknown code: text kept whole
    assert (by['e5']['reason'], by['e5']['notes']) == ('too_small', 'a | b')             # label accepted; ' | ' inside notes survives


def test_load_dispositions_precedence():
    accounts = [
        {'account_key': 'agfa gevaert', 'canonical_name': 'Agfa-Gevaert NV', 'disposition': 'Not a Fit',
         'disposition_reason': 'wrong_vertical', 'disposition_notes': 'imaging', 'disposition_at': NOW_ISO,
         'disposition_by': 'aj'},
        {'account_key': 'new co', 'canonical_name': 'New Co', 'disposition': 'On Rep TAL',
         'disposition_reason': None, 'disposition_notes': None, 'disposition_at': NOW_ISO, 'disposition_by': None},
        {'account_key': 'undecided', 'canonical_name': 'Undecided', 'disposition': None},
    ]
    d = load_dispositions(client_with_accounts(accounts, LEGACY))
    assert d['agfa gevaert'] == {'status': 'Not a Fit', 'reason': 'wrong_vertical', 'notes': 'imaging',
                                 'name': 'Agfa-Gevaert NV', 'at': NOW_ISO, 'by': 'aj', 'source': 'accounts'}
    assert d['new co']['status'] == 'On Rep TAL'
    assert d['cnl strategic residential credit'] == {'status': 'Picked Up', 'reason': None, 'notes': None,
                                                     'name': 'CNL Strategic Residential Credit, Inc.',
                                                     'at': '2026-07-22T01:12:45+00:00', 'by': None,
                                                     'source': 'account_dispositions'}
    assert d['palm beach north chamber']['notes'] == 'chamber of commerce'
    assert 'undecided' not in d and len(d) == 5
    # table absent → legacy only; both absent → {}
    d2 = load_dispositions(client_without_accounts(LEGACY))
    assert set(d2) == {'cnl strategic residential credit', 'agfa gevaert', 'sfa llc dba swamp fox agency',
                       'palm beach north chamber'} and d2['agfa gevaert']['status'] == 'Out of Alignment'
    assert load_dispositions(FakeClient()) == {} and load_dispositions(None) == {}
    # two legacy spellings of one account: the newer row wins
    dup = [{'company_key': 'agfa gevaert', 'company_name': 'Agfa Gevaert', 'status': 'Picked Up', 'updated_at': '2026-09-01'},
           {'company_key': 'agfa-gevaert', 'company_name': 'Agfa-Gevaert', 'status': 'Not a Fit', 'updated_at': '2026-08-01'}]
    assert load_dispositions(client_without_accounts(dup))['agfa gevaert']['status'] == 'Picked Up'
    # the legacy reason rides in notes and comes back decoded
    enc = [{'company_key': 'enc', 'company_name': 'Enc Co', 'status': 'Not a Fit',
            'notes': 'reason=too_big | call in Q4', 'updated_at': '2026-09-01'}]
    got = load_dispositions(client_without_accounts(enc))['enc']
    assert (got['status'], got['reason'], got['notes']) == ('Not a Fit', 'too_big', 'call in Q4')


def test_load_dispositions_newer_legacy_row_beats_older_accounts_row():
    """review 2026-09-08 (Phase 4): after 'Partly saved — written to
    account_dispositions' the legacy row is the rep's LATEST verdict; the
    stale accounts row must not mask it. Accounts still win a tie and any
    undated collision."""
    acct = {'account_key': 'acme', 'canonical_name': 'Acme', 'disposition': 'Picked Up',
            'disposition_reason': None, 'disposition_notes': None,
            'disposition_at': '2026-09-01T00:00:00+00:00', 'disposition_by': 'aj'}
    newer = {'company_key': 'acme', 'company_name': 'Acme', 'status': 'Not a Fit',
             'notes': 'reason=too_big | went with SAP', 'updated_at': '2026-09-05T00:00:00+00:00'}
    got = load_dispositions(client_with_accounts([acct], [newer]))['acme']
    assert got == {'status': 'Not a Fit', 'reason': 'too_big', 'notes': 'went with SAP', 'name': 'Acme',
                   'at': '2026-09-05T00:00:00+00:00', 'by': None, 'source': 'account_dispositions'}
    A.reset_probe_cache()
    older = dict(newer, updated_at='2026-08-01T00:00:00+00:00')
    assert load_dispositions(client_with_accounts([acct], [older]))['acme']['source'] == 'accounts'
    A.reset_probe_cache()
    tie = dict(newer, updated_at='2026-09-01T00:00:00+00:00')
    assert load_dispositions(client_with_accounts([acct], [tie]))['acme']['status'] == 'Picked Up'
    A.reset_probe_cache()
    undated = dict(newer, updated_at=None)
    assert load_dispositions(client_with_accounts([acct], [undated]))['acme']['status'] == 'Picked Up'
    A.reset_probe_cache()
    assert load_dispositions(client_with_accounts([dict(acct, disposition_at=None)], [newer]))['acme']['status'] == 'Picked Up'
    assert A.is_newer('2026-09-05', '2026-09-01') and not A.is_newer('2026-09-01', '2026-09-01')
    assert not A.is_newer(None, '2026-09-01') and not A.is_newer('2026-09-05', 'garbage')


def test_set_disposition_validation():
    c = client_with_accounts()
    assert set_disposition(None, 'Acme', 'Picked Up') == 'NOT saved — no database connection'
    assert set_disposition(c, '', 'Picked Up') == 'NOT saved — no company name'
    assert set_disposition(c, '???', 'Picked Up').startswith("NOT saved — couldn't derive")
    assert set_disposition(c, 'Acme', 'Maybe').startswith('NOT saved — unknown account status')
    msg = set_disposition(c, 'Acme', 'Not a Fit')
    assert msg.startswith("NOT saved — 'Not a Fit' needs a reason") and 'Wrong vertical' in msg
    assert set_disposition(c, 'Acme', 'Out of Alignment', reason='').startswith('NOT saved')
    assert set_disposition(c, 'Acme', 'Picked Up', reason='because').startswith('NOT saved — unknown disposition reason')
    assert c.writes() == []


def test_set_disposition_dual_write_and_clear():
    # the v1 dashboard's row for Agfa sits under its own spelling of the key
    c = client_with_accounts([base_row(disposition=None)],
                             legacy=[{'company_key': 'agfa-gevaert', 'company_name': 'Agfa-Gevaert',
                                      'status': 'Out of Alignment', 'notes': None, 'updated_at': '2026-08-27'}])
    msg = set_disposition(c, 'Agfa-Gevaert', 'Not a Fit', reason='Wrong vertical', notes='imaging', by='aj', now=NOW)
    assert msg == 'Saved: Agfa-Gevaert → Not a Fit (Wrong vertical)'
    acct = next(r for r in c.tables['accounts'] if r['account_key'] == 'agfa gevaert')
    assert acct == {'account_key': 'agfa gevaert', 'canonical_name': 'Agfa-Gevaert', 'disposition': 'Not a Fit',
                    'disposition_reason': 'wrong_vertical', 'disposition_notes': 'imaging',
                    'disposition_at': NOW_ISO, 'disposition_by': 'aj',
                    'updated_at': NOW_ISO}                                      # disposition-only row, named, stamped
    legacy = c.tables['account_dispositions']
    # pipeline key; v1 row gone; the reason rides in notes (review 2026-09-08
    # (Phase 4): a notes-only write lost it until migration 003 ran)
    assert legacy == [{'company_key': 'agfa gevaert', 'company_name': 'Agfa-Gevaert', 'status': 'Not a Fit',
                       'notes': 'reason=wrong_vertical | imaging', 'updated_at': NOW_ISO}]
    assert A.map_legacy_dispositions(legacy)[0]['reason'] == 'wrong_vertical'     # and comes back out
    # an existing account keeps its facts; only the trio moves; the legacy row updates in place
    c.tables['account_dispositions'].append({'company_key': 'acme bancorp', 'company_name': 'Acme', 'status': 'Picked Up'})
    msg = set_disposition(c, 'Acme Bancorp, Inc.', 'On Rep TAL', now=NOW)
    assert msg == 'Saved: Acme Bancorp, Inc. → On Rep TAL'
    acme = next(r for r in c.tables['accounts'] if r['account_key'] == 'acme bancorp')
    assert acme['disposition'] == 'On Rep TAL' and acme['disposition_reason'] is None and acme['grade'] == 'B'
    assert acme['canonical_name'] == 'Acme Bancorp' and acme['updated_at'] == NOW_ISO
    assert [r['company_key'] for r in legacy] == ['agfa gevaert', 'acme bancorp']
    assert next(r for r in legacy if r['company_key'] == 'acme bancorp')['status'] == 'On Rep TAL'
    # clear: legacy rows under either spelling go; accounts trio nulled in place; no phantom row
    legacy.append({'company_key': 'agfa-gevaert', 'company_name': 'Agfa-Gevaert', 'status': 'Picked Up'})
    assert set_disposition(c, 'Agfa-Gevaert', None, now=NOW) == 'Cleared account status for Agfa-Gevaert'
    assert set_disposition(c, 'Nobody Co', '—', now=NOW) == 'Cleared account status for Nobody Co'
    assert acct['disposition'] is None and acct['disposition_reason'] is None and acct['disposition_at'] is None
    assert acct['updated_at'] == NOW_ISO
    assert [r['company_key'] for r in legacy] == ['acme bancorp']
    assert {r['account_key'] for r in c.tables['accounts']} == {'acme bancorp', 'agfa gevaert'}


def test_set_disposition_clears_every_legacy_spelling():
    """review 2026-09-08 (Phase 4): delete removed only {v1_key, key}; a row
    under a THIRD spelling survived a clear, and load_dispositions — which
    maps legacy rows by NAME — resurrected its stale status."""
    spellings = [
        {'company_key': 'agfa gevaert', 'company_name': 'Agfa Gevaert', 'status': 'Picked Up', 'updated_at': '2026-07-01'},
        {'company_key': 'agfa-gevaert', 'company_name': 'Agfa-Gevaert', 'status': 'On Rep TAL', 'updated_at': '2026-07-02'},
        {'company_key': 'agfa-gevaert n.v.', 'company_name': 'Agfa-Gevaert', 'status': 'Not a Fit',
         'notes': 'reason=too_big', 'updated_at': '2026-09-07'},                # neither key spelling: matched by NAME
        {'company_key': 'cleanaway', 'company_name': 'Cleanaway', 'status': 'Picked Up', 'updated_at': '2026-07-03'},
    ]
    c = client_with_accounts(legacy=spellings)
    assert set(load_dispositions(c)) == {'agfa gevaert', 'cleanaway'}
    assert load_dispositions(c)['agfa gevaert']['status'] == 'Not a Fit'         # the stale third spelling, newest
    # write: ONE legacy row for the account remains, under the pipeline key
    assert set_disposition(c, 'Agfa-Gevaert', 'Picked Up', notes='call back', now=NOW).startswith('Saved')
    assert sorted((r['company_key'], r['status']) for r in c.tables['account_dispositions']) == \
        [('agfa gevaert', 'Picked Up'), ('cleanaway', 'Picked Up')]
    assert next(r for r in c.tables['account_dispositions'] if r['company_key'] == 'agfa gevaert')['notes'] == 'call back'
    assert load_dispositions(c)['agfa gevaert']['status'] == 'Picked Up'
    # clear: every spelling goes, the unrelated row stays, nothing resurrects
    c.tables['account_dispositions'].append({'company_key': 'agfa-gevaert n.v.', 'company_name': 'Agfa-Gevaert',
                                             'status': 'Not a Fit', 'updated_at': '2026-09-07'})
    assert set_disposition(c, 'Agfa-Gevaert', None, now=NOW) == 'Cleared account status for Agfa-Gevaert'
    assert [r['company_key'] for r in c.tables['account_dispositions']] == ['cleanaway']
    assert set(load_dispositions(c)) == {'cleanaway'}
    assert A._legacy_keys_for(c, 'Agfa-Gevaert', 'agfa gevaert') == {'agfa gevaert', 'agfa-gevaert'}


def test_set_disposition_without_accounts_table_and_partial_failures():
    c = client_without_accounts()
    assert set_disposition(c, 'Acme', 'Picked Up', now=NOW) == 'Saved: Acme → Picked Up'
    assert c.tables['account_dispositions'] == [{'company_key': 'acme', 'company_name': 'Acme', 'status': 'Picked Up',
                                                 'notes': None, 'updated_at': NOW_ISO}]
    assert set_disposition(c, 'Acme', 'Picked Up', notes='hello').startswith('Saved') \
        and c.tables['account_dispositions'][0]['notes'] == 'hello'
    # no accounts table yet: the legacy row is the ONLY copy of the reason,
    # so it is encoded into notes (review 2026-09-08 (Phase 4))
    assert set_disposition(c, 'Acme', 'Not a Fit', reason='Too big', notes='went | with SAP', now=NOW).startswith('Saved')
    assert c.tables['account_dispositions'][0]['notes'] == 'reason=too_big | went | with SAP'
    assert load_dispositions(c)['acme'] == {'status': 'Not a Fit', 'reason': 'too_big', 'notes': 'went | with SAP',
                                            'name': 'Acme', 'at': NOW_ISO, 'by': None,
                                            'source': 'account_dispositions'}
    # (the probe is memoized per PROCESS — one client per process in real
    #  life — so a new fake client needs the cache reset)
    A.reset_probe_cache()
    both_broken = FakeClient({'accounts': [], 'account_dispositions': []},
                             columns={'accounts': set(ACCOUNT_COLUMNS)}, broken={'account_dispositions'})
    msg = set_disposition(both_broken, 'Acme', 'Picked Up', now=NOW)
    assert msg.startswith('Partly saved — written to accounts, but account_dispositions:')
    assert both_broken.tables['accounts'][0]['disposition'] == 'Picked Up'
    A.reset_probe_cache()
    dead = FakeClient({'accounts': [], 'account_dispositions': []}, columns={'accounts': set(ACCOUNT_COLUMNS)},
                      broken={'account_dispositions', 'accounts'})
    assert set_disposition(dead, 'Acme', 'Picked Up', now=NOW).startswith('NOT saved — account_dispositions:')


# ── backfill planner ────────────────────────────────────────────────────────
def _ev(eid, name, etype, published, discovered, *, fit=None, cd=None, grade=None, blocked=None,
        reason=None, key=None, **extra):
    ev = {'id': eid, 'company_name': name, 'event_type': etype, 'published_date': published,
          'discovered_at': discovered, 'enriched_at': discovered, 'fit': fit, 'companies_data': cd,
          'grade': grade, 'numeric_score': 5 if grade else None, 'confidence_level': 'High' if grade else None,
          'hashtags': ['#x'] if grade else None, 'grade_justification': 'j' if grade else None,
          'blocked_at': blocked, 'blocked_reason': reason, 'account_key': key}
    ev.update(extra)
    return ev


def _fit(name, verdict='pass'):
    return {'verdict': verdict, 'territory': 'in', 'revenue': 'in', 'vertical': 'in',
            'zi_subindustry': 'Banking', 'account_name': name}


def _cd(name, hq='Boston, MA', **over):
    c = {'name': name, 'role': 'Hiring Company', 'hq': hq, 'zi_subindustry': 'Banking', 'industry': 'Banking',
         'size': '51-200', 'revenue': 'MM', 'url': f'https://{account_key(name).replace(" ", "")}.com',
         'fit': {'verdict': 'pass', 'territory': 'in', 'revenue': 'in', 'vertical': 'in'}}
    c.update(over)
    return [c]


FIXTURE_EVENTS = [
    # Acme: an old expired CFO hire (graded A), a live funding (graded C), a tombstoned expired event with an older HQ
    _ev('a1', 'Acme Bancorp', 'cfo_hire', '2026-05-01', '2026-05-02T10:00:00', fit=_fit('Acme Bancorp'),
        cd=_cd('Acme Bancorp'), grade='A', key='acme bancorp', expires_at='2026-06-30T00:00:00+00:00'),
    _ev('a2', 'Acme Bancorp, Inc.', 'funding', '2026-09-01', '2026-09-02T10:00:00', fit=_fit('Acme Bancorp'),
        cd=_cd('Acme Bancorp', size='201-500'), grade='C', key='acme bancorp', expires_at='2026-10-31T00:00:00+00:00'),
    _ev('a3', 'Acme Bancorp', 'executive_hire', '2026-04-01', '2026-04-02T10:00:00', fit=_fit('Acme Bancorp'),
        cd=_cd('Acme Bancorp', hq='Denver, CO'), grade='B', blocked='2026-06-01', reason='trigger_expired', key='acme bancorp'),
    # Beta: typed key NULL, grouped via fit.account_name; company_name differs
    _ev('b1', 'Beta Holdings LLC', 'merger_acquisition', '2026-08-20', '2026-08-21T10:00:00', fit=_fit('Beta Credit Union'),
        cd=[dict(_cd('Beta Credit Union', role='Target')[0]), {'name': 'Beta Holdings LLC', 'role': 'Acquirer'}], grade='B'),
    # Gamma: pre-search tombstone (no fit) — the account exists and is OUT
    _ev('g1', 'Gamma Manufacturing', 'funding', '2026-08-01', '2026-08-02T10:00:00', blocked='2026-08-02',
        reason='structured:out: SIC 3559 (off-vertical industry)'),
    # Delta: rep tombstone whose disposition was since cleared; expired-by-date trigger with no expires_at column
    _ev('d1', 'Delta Bank', 'cfo_hire', '2026-03-01', '2026-03-02T10:00:00', fit=_fit('Delta Bank'), cd=_cd('Delta Bank'),
        grade='B', blocked='2026-08-01', reason='rep:Not a Fit'),
    # junk names never make an account
    _ev('j1', 'Unknown', 'funding', '2026-09-01', '2026-09-02T10:00:00'),
    _ev('j2', None, 'funding', '2026-09-01', '2026-09-02T10:00:00'),
    # an M&A about Agfa (legacy disposition exists under the dashboard's key)
    _ev('f1', 'Agfa-Gevaert', 'merger_acquisition', '2026-09-03', '2026-09-04T10:00:00', fit=_fit('Agfa-Gevaert', 'unverified'),
        cd=_cd('Agfa-Gevaert', hq='Mortsel, Belgium'), grade='C', key='agfa gevaert'),
    # a pre-v2 row: headline-fragment company_name, fit without account_name, the real company in companies_data
    _ev('h1', 'Businesses It Acquired', 'merger_acquisition', '2026-06-01', '2026-06-02T10:00:00',
        fit={'verdict': 'fail', 'territory': 'in', 'revenue': 'out', 'vertical': 'in'},
        cd=_cd('Acrisure', revenue='Enterprise', fit={'verdict': 'fail', 'territory': 'in', 'revenue': 'out', 'vertical': 'in'}),
        blocked='2026-06-02', reason='fit_gate: revenue Enterprise', key='businesses it acquired'),
]


def test_backfill_event_helpers():
    assert bf.event_key(FIXTURE_EVENTS[0]) == 'acme bancorp'
    assert bf.event_key(FIXTURE_EVENTS[3]) == 'beta credit union'          # typed NULL → fit.account_name
    assert bf.event_key(FIXTURE_EVENTS[6]) == '' and bf.event_key(FIXTURE_EVENTS[7]) == ''
    assert bf.event_key(FIXTURE_EVENTS[9]) == 'businesses it acquired'    # the typed column, as stored
    assert bf.event_contribution(FIXTURE_EVENTS[9], NOW)['account_key'] == 'acrisure'   # the row, as keyed
    groups, skipped, diverging = bf.group_events(FIXTURE_EVENTS, NOW)
    assert set(groups) == {'acme bancorp', 'beta credit union', 'gamma manufacturing', 'delta bank',
                           'agfa gevaert', 'acrisure'}
    assert [e['id'] for e, _ in groups['acme bancorp']] == ['a2', 'a1', 'a3']   # newest first
    assert skipped == {'no usable company name': 2} and [x['event_id'] for x in diverging] == ['h1']
    assert bf.is_live_event(FIXTURE_EVENTS[1], NOW) and not bf.is_live_event(FIXTURE_EVENTS[0], NOW)
    assert not bf.is_live_event(FIXTURE_EVENTS[2], NOW) and not bf.is_live_event(FIXTURE_EVENTS[5], NOW)
    assert bf.tombstone_verdict('structured:out: SIC 3559') == 'fail'
    assert bf.tombstone_verdict('fit_gate:territory') == 'fail' and bf.tombstone_verdict('industry:hotel') == 'fail'
    assert bf.tombstone_verdict('trigger_expired') == '' and bf.tombstone_verdict('rep:Not a Fit') == ''
    assert bf.tombstone_verdict(None) == ''
    assert bf.event_contribution(FIXTURE_EVENTS[6], NOW) is None


def test_backfill_plan_grouping_and_row_selection():
    plan = bf.plan_accounts(FIXTURE_EVENTS, LEGACY, [], NOW)
    rows = plan['rows']
    assert plan['skipped'] == {'no usable company name': 2}
    assert plan['usable_events'] == 8 and plan['groups'] == 6
    assert plan['distinct_event_keys'] == 6 and plan['mismatch'] == []
    # the legacy fragment is explained, not lost: its row is the real company
    assert plan['diverging'] == [{'event_id': 'h1', 'event_key': 'businesses it acquired', 'row_key': 'acrisure',
                                  'company_name': 'Businesses It Acquired', 'live': False, 'tombstoned': True}]
    assert 'businesses it acquired' not in rows and rows['acrisure']['verify_state'] == 'not_fit'
    assert rows['acrisure']['revenue_segment'] == 'Enterprise' and rows['acrisure']['canonical_name'] == 'Acrisure'
    assert sum(r['event_count'] for r in rows.values()) == 8
    acme = rows['acme bancorp']
    assert acme['event_count'] == 3 and acme['canonical_name'] == 'Acme Bancorp'
    assert acme['seen_event_ids'] == ['a3', 'a1', 'a2']                          # oldest first, newest last
    assert acme['aliases'] == ['Acme Bancorp, Inc.']
    assert (acme['grade'], acme['graded_event_id']) == ('C', 'a2')            # the A is expired, the B tombstoned
    assert (acme['best_trigger_type'], acme['best_trigger_event_id']) == ('funding', 'a2')
    assert acme['hq'] == 'Boston, MA' and acme['size_bucket'] == '201-500'     # newest enriched event anchors the facts
    assert acme['first_seen'] == '2026-04-02T10:00:00+00:00' and acme['last_seen'] == '2026-09-02T10:00:00+00:00'
    assert acme['verify_state'] == 'verified' and 'disposition' not in acme
    beta = rows['beta credit union']
    assert beta['canonical_name'] == 'Beta Credit Union' and beta['grade'] == 'B' and beta['event_count'] == 1
    assert beta['best_trigger_type'] == 'merger_acquisition' and beta['aliases'] == []
    gamma = rows['gamma manufacturing']
    assert (gamma['fit_verdict'], gamma['verify_state']) == ('fail', 'not_fit') and 'grade' not in gamma
    assert 'best_trigger_type' not in gamma and gamma['event_count'] == 1
    delta = rows['delta bank']
    assert 'grade' not in delta and 'best_trigger_type' not in delta and 'disposition' not in delta
    assert delta['verify_state'] == 'verified'                                   # research persisted on the tombstone
    # legacy dispositions: attached where the account has events, disposition-only otherwise
    d = plan['dispositions']
    assert (d['legacy_rows'], d['mapped'], d['attached'], d['disposition_only'], d['unmatched']) == (4, 4, 1, 3, [])
    assert rows['agfa gevaert']['disposition'] == 'Out of Alignment' and rows['agfa gevaert']['disposition_reason'] is None
    assert rows['agfa gevaert']['grade'] == 'C'
    only = rows['cnl strategic residential credit']
    assert only == {'account_key': 'cnl strategic residential credit', 'canonical_name': 'CNL Strategic Residential Credit, Inc.',
                    'event_count': 0, 'active': True, 'updated_at': NOW_ISO, 'disposition': 'Picked Up',
                    'disposition_reason': None, 'disposition_notes': None,
                    'disposition_at': '2026-07-22T01:12:45+00:00', 'disposition_by': None}
    assert d['no_reason'] == 2 and d['rep_tombstones_without'] == 1
    assert d['from_existing'] == 0 and d['superseded_accounts'] == 0 and plan['since'] is None
    assert len(rows) == 9
    for r in rows.values():
        assert r['account_key'] == account_key(r.get('canonical_name')) and set(r) <= set(ACCOUNT_COLUMNS)
    # the explanation was needed: the fragment IS an event-side key and is NOT a row
    assert 'businesses it acquired' in {bf.event_key(e) for e in FIXTURE_EVENTS}
    assert 'businesses it acquired' not in rows


def test_backfill_plan_respects_existing_accounts():
    existing = [{'account_key': 'agfa gevaert', 'canonical_name': 'Agfa-Gevaert', 'hq': 'Mortsel, Belgium',
                 'disposition': 'Picked Up', 'disposition_reason': None, 'grade': 'A', 'graded_event_id': 'old',
                 'best_trigger_type': 'cfo_hire', 'best_trigger_at': '2026-09-01T00:00:00+00:00',
                 'best_trigger_event_id': 'old', 'event_count': 7, 'verify_state': 'verified',
                 'firmographics': {'hq': 'Mortsel, Belgium', 'field_sources': {'hq': 'oracle'}}}]
    plan = bf.plan_accounts(FIXTURE_EVENTS, LEGACY + [{'company_key': 'zzz', 'company_name': None, 'status': 'Bogus'}],
                            existing, NOW)
    agfa = plan['rows']['agfa gevaert']
    assert agfa['disposition'] == 'Picked Up'                                    # accounts win over the legacy row
    assert agfa['grade'] == 'A' and agfa['best_trigger_type'] == 'cfo_hire'     # merge rules, not a blind overwrite
    assert agfa['event_count'] == 1                                              # a full recount, not 7 + 1
    assert agfa['verify_state'] == 'verified'
    d = plan['dispositions']
    assert d['kept_from_accounts'] == 1 and d['attached'] == 0 and len(d['unmatched']) == 1
    assert d['unmatched'][0]['legacy_key'] == 'zzz'


def test_backfill_plan_legacy_reason_is_carried_and_newer_legacy_supersedes_accounts():
    """review 2026-09-08 (Phase 4): the reason decoded from the legacy notes
    lands in disposition_reason (it used to be written NULL), and a legacy
    row NEWER than the accounts row's disposition replaces it — the same
    rule as accounts.load_dispositions; a tie / undated keeps accounts'."""
    legacy = [{'company_key': 'agfa-gevaert', 'company_name': 'Agfa-Gevaert', 'status': 'Not a Fit',
               'notes': 'reason=too_big | went with SAP', 'updated_at': '2026-09-05T00:00:00+00:00'}]
    plan = bf.plan_accounts(FIXTURE_EVENTS, legacy, [], NOW)
    agfa = plan['rows']['agfa gevaert']
    assert (agfa['disposition'], agfa['disposition_reason'], agfa['disposition_notes']) == \
        ('Not a Fit', 'too_big', 'went with SAP')
    assert plan['dispositions']['no_reason'] == 0
    older = [{'account_key': 'agfa gevaert', 'canonical_name': 'Agfa-Gevaert', 'disposition': 'Picked Up',
              'disposition_reason': None, 'disposition_at': '2026-09-01T00:00:00+00:00', 'event_count': 1}]
    plan = bf.plan_accounts(FIXTURE_EVENTS, legacy, older, NOW)
    d = plan['dispositions']
    assert plan['rows']['agfa gevaert']['disposition'] == 'Not a Fit' and d['superseded_accounts'] == 1
    assert d['kept_from_accounts'] == 0 and d['attached'] == 1
    newer = [dict(older[0], disposition_at='2026-09-06T00:00:00+00:00')]
    plan = bf.plan_accounts(FIXTURE_EVENTS, legacy, newer, NOW)
    assert plan['rows']['agfa gevaert']['disposition'] == 'Picked Up' and plan['dispositions']['kept_from_accounts'] == 1
    undated = [dict(older[0], disposition_at=None)]
    assert bf.plan_accounts(FIXTURE_EVENTS, legacy, undated, NOW)['rows']['agfa gevaert']['disposition'] == 'Picked Up'


def _window(since):
    return [e for e in FIXTURE_EVENTS if str(e.get('discovered_at') or '') >= since]


def test_backfill_plan_since_window_starts_from_existing_and_never_recounts():
    """review 2026-09-08 (Phase 4): with --since, an existing row with no
    event in the window used to get a MINIMAL row (every fact NULLed by
    full_row) when a legacy disposition named it, and an existing row with
    events in the window got the WINDOW count written as its total."""
    since = '2026-09-01'
    window = _window(since)
    assert {e['id'] for e in window} == {'a2', 'j1', 'j2', 'f1'}
    existing = [
        {'account_key': 'delta bank', 'canonical_name': 'Delta Bank', 'hq': 'Portland, ME', 'hq_state': 'ME',
         'grade': 'B', 'graded_event_id': 'd1', 'verify_state': 'verified', 'event_count': 4,
         'seen_event_ids': ['x1', 'x2', 'x3', 'd1'], 'firmographics': {'hq': 'Portland, ME'}},
        {'account_key': 'acme bancorp', 'canonical_name': 'Acme Bancorp', 'hq': 'Boston, MA', 'event_count': 5,
         'seen_event_ids': ['a1', 'a3'], 'verify_state': 'verified', 'grade': 'A', 'graded_event_id': 'a1',
         'best_trigger_type': 'cfo_hire', 'best_trigger_at': '2026-08-20T00:00:00+00:00', 'best_trigger_event_id': 'a1'},
    ]
    legacy = [{'company_key': 'delta bank', 'company_name': 'Delta Bank', 'status': 'Not a Fit',
               'notes': 'reason=too_small', 'updated_at': '2026-09-06'}]
    plan = bf.plan_accounts(window, legacy, existing, NOW, since=since)
    assert plan['since'] == since
    delta = plan['rows']['delta bank']                                             # no event in the window
    assert (delta['hq'], delta['grade'], delta['event_count'], delta['seen_event_ids']) == \
        ('Portland, ME', 'B', 4, ['x1', 'x2', 'x3', 'd1'])                          # started from the existing row
    assert (delta['disposition'], delta['disposition_reason']) == ('Not a Fit', 'too_small')
    assert plan['dispositions']['from_existing'] == 1 and plan['dispositions']['disposition_only'] == 0
    acme = plan['rows']['acme bancorp']                                            # one NEW event in the window
    assert acme['event_count'] == 6 and acme['seen_event_ids'] == ['a1', 'a3', 'a2']  # existing + unseen, not 1
    assert acme['hq'] == 'Boston, MA' and acme['grade'] == 'A'                     # merge rules, nothing rebuilt
    # the same window re-run: a2 is now seen — nothing is counted twice
    plan2 = bf.plan_accounts(window, legacy, list(plan['rows'].values()), NOW, since=since)
    assert plan2['rows']['acme bancorp']['event_count'] == 6
    # without --since the full history is read and the count is a true recount
    full = bf.plan_accounts(FIXTURE_EVENTS, legacy, existing, NOW)
    assert full['rows']['acme bancorp']['event_count'] == 3
    assert full['rows']['acme bancorp']['seen_event_ids'] == ['a3', 'a1', 'a2']
    assert full['rows']['delta bank']['event_count'] == 1 and full['rows']['delta bank']['hq'] == 'Portland, ME'


def test_backfill_full_row_uniform_keys_and_no_disposition_columns_unless_carried():
    present = set(ACCOUNT_COLUMNS)
    # review 2026-09-08 (Phase 4): a row without a disposition never sends the
    # rep-owned columns — sent as None they NULLed reason / notes / by
    r = bf.full_row({'account_key': 'x', 'grade': 'A'}, present, NOW_ISO)
    assert list(r) == [c for c in ACCOUNT_COLUMNS if c != 'created_at' and c not in A.DISPOSITION_COLUMNS]
    assert r['event_count'] == 0 and r['enrich_attempts'] == 0 and r['active'] is True and r['updated_at'] == NOW_ISO
    assert r['hq'] is None and r['grade'] == 'A' and r['seen_event_ids'] is None
    with_d = bf.full_row({'account_key': 'y', 'disposition': 'Picked Up'}, present, NOW_ISO)
    assert list(with_d) == [c for c in ACCOUNT_COLUMNS if c != 'created_at']
    assert with_d['disposition'] == 'Picked Up' and with_d['disposition_reason'] is None
    assert set(bf.full_row({'account_key': 'x'}, {'account_key', 'grade'}, NOW_ISO)) == {'account_key', 'grade'}
    # write_rows batches the two key shapes separately (the fake asserts one
    # key set per batch, exactly like postgrest's union-of-keys behaviour)
    c = client_with_accounts()
    rows = [bf.full_row({'account_key': k, 'disposition': 'Picked Up' if i % 2 else None}, present, NOW_ISO)
            for i, k in enumerate('abcde')]
    assert bf.write_rows(c, rows, batch_size=2) == 5
    batches = [p for _, op, p in c.writes('accounts', 'upsert')]
    assert [len(b) for b in batches] == [2, 1, 2] and all(len({tuple(sorted(r)) for r in b}) == 1 for b in batches)
    assert {r['account_key'] for r in c.tables['accounts']} == set('abcde')
    assert 'disposition' not in c.tables['accounts'][0] and c.tables['accounts'][-1]['disposition'] == 'Picked Up'


# ── backfill run(): the guards around --apply (review 2026-09-08 (Phase 4)) ──
def _args(**over):
    a = {'apply': False, 'preflight': False, 'since': None, 'batch': 500}
    a.update(over)
    return SimpleNamespace(**a)


def _backfill_client(accounts=(), legacy=LEGACY, events=FIXTURE_EVENTS):
    return FakeClient({'accounts': list(accounts), 'account_dispositions': list(legacy), 'events': list(events)},
                      columns={'accounts': set(ACCOUNT_COLUMNS)})


def test_backfill_refuses_since_with_apply(tmp_path, capsys):
    c = _backfill_client()
    assert bf.refuse_args(_args(apply=True, since='2026-09-01')).startswith('REFUSING: --since is a dry-run preview only')
    assert bf.refuse_args(_args(apply=True, preflight=True)).startswith('REFUSING: --preflight')
    assert bf.refuse_args(_args(since='2026-09-01')) is None and bf.refuse_args(_args(apply=True)) is None
    assert bf.run(_args(apply=True, since='2026-09-01'), c, lock_path=str(tmp_path / 'lock')) == 2
    assert c.calls == [] and 'REFUSING: --since' in capsys.readouterr().out            # nothing read, nothing written
    assert bf.main(['--apply', '--since', '2026-09-01']) == 2                           # and main never builds a client
    assert bf.run(_args(since='2026-09-01'), c, now=NOW) == 0                           # the dry-run preview still works
    out = capsys.readouterr().out
    assert 'CHECK coverage: n/a for a --since window' in out and 'DRY RUN' in out
    assert c.writes() == []


def test_backfill_refuses_apply_when_the_existing_read_fails(tmp_path, capsys):
    """accounts.list_accounts returns [] on ANY failure; the backfill must
    not plan (and NULL every disposition) off that silence."""
    c = _backfill_client(accounts=[base_row(disposition='Not a Fit', disposition_reason='too_big')])
    probe_accounts(c)
    A.account_columns_present(c)                    # the probes succeed, the paged read then fails
    c.broken.add('accounts')
    assert bf.run(_args(apply=True), c, lock_path=str(tmp_path / 'lock'), now=NOW) == 2
    out = capsys.readouterr().out
    assert 'accounts table UNREADABLE' in out and 'REFUSING: --apply needs the existing rows' in out
    assert c.writes() == []
    assert bf.run(_args(), c, now=NOW) == 0                                                # a dry run still reports
    out = capsys.readouterr().out
    assert 'assumes an EMPTY accounts table; --apply would refuse' in out and 'DRY RUN' in out
    with pytest.raises(Exception):
        bf.fetch_existing(c, set(ACCOUNT_COLUMNS), 500)                                   # strict, unlike list_accounts
    assert A.list_accounts(c) == []


def test_backfill_apply_takes_the_enrichment_lock(tmp_path, capsys):
    from src.pipeline.runlock import RunLock
    lock_path = str(tmp_path / 'enrichment.lock')
    c = _backfill_client()
    held = RunLock(lock_path)
    assert held.acquire()
    try:
        assert bf.run(_args(apply=True), c, lock_path=lock_path, now=NOW) == 2
        out = capsys.readouterr().out
        assert 'REFUSING: an enrichment run is active' in out and str(os.getpid()) in out
        assert c.writes() == []
        assert bf.run(_args(), c, lock_path=lock_path, now=NOW) == 0                      # a dry run needs no lock
        assert c.writes() == []
    finally:
        held.release()
    assert bf.run(_args(apply=True), c, lock_path=lock_path, now=NOW) == 0
    assert 'APPLIED: 9 account rows upserted' in capsys.readouterr().out
    assert RunLock(lock_path).acquire()                                                   # released after the write
    assert bf.LOCK_PATH.endswith(os.path.join('state', 'enrichment.lock'))                # the same file enrichment takes


def test_backfill_apply_end_to_end_and_rerun_keeps_dispositions(tmp_path, capsys):
    """A first --apply, then a SECOND over the rows it wrote: every
    disposition (with its reason) survives, rows without one never carry
    the disposition keys, counts do not drift — the re-run that used to
    NULL reasons and double count."""
    existing = [dict(base_row(), account_key='beta credit union', canonical_name='Beta Credit Union',
                     disposition='Not a Fit', disposition_reason='too_big', disposition_notes='x',
                     disposition_at='2026-09-01T00:00:00+00:00', disposition_by='aj')]
    c = _backfill_client(accounts=existing)
    lock = str(tmp_path / 'lock')
    assert bf.run(_args(apply=True), c, lock_path=lock, now=NOW) == 0
    out = capsys.readouterr().out
    assert 'APPLIED: 9 account rows upserted' in out and 'kept accounts\' own 0' in out
    by = {r['account_key']: r for r in c.tables['accounts']}
    assert len(by) == 9
    beta = by['beta credit union']
    assert (beta['disposition'], beta['disposition_reason'], beta['disposition_notes'], beta['disposition_by']) == \
        ('Not a Fit', 'too_big', 'x', 'aj')                                               # never NULLed
    assert beta['grade'] == 'B' and beta['event_count'] == 1
    assert 'disposition' not in by['gamma manufacturing'] and 'disposition_reason' not in by['acme bancorp']
    assert by['acme bancorp']['event_count'] == 3 and by['acme bancorp']['seen_event_ids'] == ['a3', 'a1', 'a2']
    assert by['agfa gevaert']['disposition'] == 'Out of Alignment'
    assert by['cnl strategic residential credit']['event_count'] == 0
    snapshot = copy.deepcopy(c.tables['accounts'])
    # the re-run: identical rows (updated_at aside), no drift
    assert bf.run(_args(apply=True), c, lock_path=lock, now=NOW + timedelta(days=1)) == 0
    by2 = {r['account_key']: r for r in c.tables['accounts']}
    for r in snapshot:
        got = dict(by2[r['account_key']])
        r, got = dict(r), got
        r.pop('updated_at'), got.pop('updated_at')
        assert got == r, r['account_key']
    assert by2['beta credit union']['disposition_reason'] == 'too_big'
    assert by2['acme bancorp']['event_count'] == 3


def test_backfill_fetch_events_pages_with_a_tiebreaker():
    """review 2026-09-08 (Phase 4): paging on discovered_at alone lets two
    events discovered in the same second swap across a page boundary."""
    c = _backfill_client()
    rows = bf.fetch_events(c, ('id', 'discovered_at'), '2026-09-01', 2)
    assert {r['id'] for r in rows} == {'a2', 'j1', 'j2', 'f1'}
    for table, _op, _p, filters in c.calls:
        if table == 'events':
            orders = [f for f in filters if f[0] == 'order']
            assert orders == [('order', 'discovered_at', False, False), ('order', 'id', False, False)]
            assert ('gte', 'discovered_at', '2026-09-01', False) in filters
    assert sum(1 for t, *_ in c.calls if t == 'events') == 3         # two full pages of 2, then the empty page that ends the loop
