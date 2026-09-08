"""Tests for scripts/ria_trigger.py — new SEC-registered adviser triggers
(v2 Phase 3, slice B3, 2026-09-08).

No network: a fake Supabase client records upserts, answers the per-batch
dedup select and the typed-column probe. The oracle table is built here
from the contract schema (state/oracles.db is produced by another slice).
"""
import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import ria_trigger as rt
from src.pipeline.gates import TERRITORY_STATES
from src.pipeline.typed import TYPED_EVENT_COLUMNS, expires_at_for, reset_probe_cache
from src.scrapers.base import BaseScraper
from supabase_sync import NEVER_SYNC_COLUMNS, SCRAPE_OWNED_COLUMNS

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(days=10)).date()          # 2026-08-29
OLD = (NOW - timedelta(days=200)).date()


def _us(d: date) -> str:
    """SEC feed date shape: MM/DD/YYYY."""
    return f'{d.month:02d}/{d.day:02d}/{d.year}'


# ── Fake Supabase client ─────────────────────────────────────────────────────

class _Query:
    def __init__(self, client, table):
        self._c, self._table = client, table
        self._op = self._cols = self._in = None

    def select(self, cols='*', **kw):
        self._op = 'select'
        self._cols = [c.strip() for c in cols.split(',')] if cols != '*' else ['*']
        return self

    def limit(self, n):
        return self

    def in_(self, col, values):
        self._in = (col, [str(v) for v in values])
        return self

    def upsert(self, data, on_conflict=None, ignore_duplicates=False, **kw):
        self._op = 'upsert'
        self._c.upserts.append({'table': self._table, 'data': data,
                                'on_conflict': on_conflict,
                                'ignore_duplicates': ignore_duplicates})
        return self

    def update(self, *a, **kw):
        raise AssertionError('the trigger must never update existing rows')

    def delete(self, *a, **kw):
        raise AssertionError('the trigger must never delete')

    def execute(self):
        if self._op == 'select':
            missing = [c for c in self._cols if c != '*' and c not in self._c.present]
            if missing:
                raise RuntimeError(f'column events.{missing[0]} does not exist')
            self._c.reads.append({'table': self._table, 'cols': self._cols, 'in': self._in})
            rows = self._c.rows
            if self._in:
                col, vals = self._in
                rows = [r for r in rows if str(r.get(col)) in vals]
            return SimpleNamespace(data=[dict(r) for r in rows], count=None)
        if self._op == 'upsert':
            data = self._c.upserts[-1]['data']
            for r in (data if isinstance(data, list) else [data]):
                if r['id'] not in {x['id'] for x in self._c.rows}:
                    self._c.rows.append(dict(r))
            return SimpleNamespace(data=data, count=None)
        return SimpleNamespace(data=[], count=None)


class FakeClient:
    """`present` = live columns (probe answers); `rows` = events already in
    Supabase (dedup answers; upserts land here too)."""

    def __init__(self, rows=(), present=None):
        self.present = set(SCRAPE_OWNED_COLUMNS) | set(rt.SEED_COLUMNS) if present is None else set(present)
        self.rows = [dict(r) for r in rows]
        self.upserts, self.reads = [], []

    def table(self, name):
        assert name == 'events'
        return _Query(self, name)


# ── Oracle DB fixture (contract schema) ──────────────────────────────────────

FIRM_COLS = ('crd', 'business_name', 'legal_name', 'norm_name', 'city', 'state', 'country',
             'firm_type', 'reg_status', 'reg_date', 'website', 'total_employees', 'raum_usd',
             'sec_number', 'as_of')


def _firm(crd, name, city, state, reg=RECENT, *, firm_type='Registered', employees=30,
          raum=2_000_000_000, country='United States', legal=None, website=None,
          sec_number=None, status='APPROVED'):
    # None → a plausible default; '' stays '' (the "fact missing" case under test)
    return (crd, name, legal or name, name.lower(), city, state, country, firm_type, status,
            _us(reg) if isinstance(reg, date) else reg,
            f'https://www.{name.split()[0].lower()}.example' if website is None else website,
            employees, raum, f'801-{crd}' if sec_number is None else sec_number, '2026-09-01')


def make_db(path, firms, ledger=()):
    conn = sqlite3.connect(str(path))
    conn.execute('''CREATE TABLE ria_firm(
        crd INTEGER PRIMARY KEY, business_name TEXT, legal_name TEXT, norm_name TEXT,
        city TEXT, state TEXT, country TEXT, firm_type TEXT, reg_status TEXT, reg_date TEXT,
        website TEXT, total_employees INTEGER, raum_usd INTEGER, sec_number TEXT, as_of TEXT)''')
    conn.execute('''CREATE TABLE oracle_meta(
        source TEXT PRIMARY KEY, refreshed_at TEXT, rows INTEGER, src_url TEXT)''')
    conn.executemany(f'INSERT INTO ria_firm VALUES ({",".join("?" * len(FIRM_COLS))})', firms)
    conn.execute("INSERT INTO oracle_meta VALUES ('sec_iapd', '2026-09-02T05:01:00', ?, "
                 "'https://www.sec.gov/foia/docs/invafoiashtml')", (len(firms),))
    conn.commit()
    if ledger:
        rt.ensure_ledger(conn)
        conn.executemany(f'INSERT INTO {rt.LEDGER_TABLE} VALUES (?,?,?,?)', ledger)
        conn.commit()
    conn.close()
    return str(path)


# The six contract firms: exactly ONE (crd 1001) must come out.
SIX = [
    _firm(1001, 'Beacon Hill Capital Advisors LLC', 'BOSTON', 'MA',
          employees=30, raum=2_000_000_000, sec_number='801-123456',
          website='https://www.beaconhillcap.example'),                 # in-band, recent, MA
    _firm(1002, 'Tiny Wealth LLC', 'ALBANY', 'NY', employees=3, raum=100_000_000),   # too small
    _firm(1003, 'Golden State Advisers LLC', 'SAN FRANCISCO', 'CA'),   # out of territory
    _firm(1004, 'Nutmeg Ventures Management LLC', 'STAMFORD', 'CT',
          firm_type='ERA', employees=20, raum=None, status='ACTIVE'),  # ERA
    _firm(1005, 'Old Line Asset Management LLC', 'BALTIMORE', 'MD', reg=OLD),  # old registration
    _firm(1006, 'Already Emitted Partners LLC', 'PHILADELPHIA', 'PA'),  # in the ledger
]
LEDGER_1006 = [(1006, 'deadbeef' * 4, '2026-08-05T05:00:00+00:00', RECENT.isoformat())]


@pytest.fixture
def oracle_db(tmp_path):
    reset_probe_cache()
    return make_db(tmp_path / 'oracles.db', SIX, ledger=LEDGER_1006)


def _run(db, client=None, **kw):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = rt.run(db, client, now=NOW, **kw)
    return rc, out.getvalue()


def _ledger(db):
    conn = sqlite3.connect(db)
    try:
        if not rt._table_exists(conn, rt.LEDGER_TABLE):
            return {}
        return {crd: (eid, reg) for crd, eid, _, reg in
                conn.execute(f'SELECT crd, event_id, emitted_at, reg_date FROM {rt.LEDGER_TABLE}')}
    finally:
        conn.close()


# ── Pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    ('08/29/2026', date(2026, 8, 29)), ('8/9/2026', date(2026, 8, 9)),
    ('2026-08-29', date(2026, 8, 29)), ('2026-08-29T00:00:00', date(2026, 8, 29)),
    (date(2026, 8, 29), date(2026, 8, 29)), (datetime(2026, 8, 29, 7), date(2026, 8, 29)),
    ('13/45/2026', None), ('', None), (None, None), ('n/a', None), ('2026-02-30', None),
])
def test_parse_reg_date(raw, expected):
    assert rt.parse_reg_date(raw) == expected


@pytest.mark.parametrize('state, country, expected', [
    ('MA', 'United States', 'MA'), ('ma', 'USA', 'MA'), ('MA', '', 'MA'), ('MA', None, 'MA'),
    ('Massachusetts', 'United States', 'MA'), ('NEW YORK', 'UNITED STATES', 'NY'),
    ('DC', 'United States', 'DC'), ('District of Columbia', 'US', 'DC'),
    ('ON', 'Canada', 'ON'), ('Ontario', 'CANADA', 'ON'), ('QC', 'CA', 'QC'),   # provinces
    ('BC', 'Canada', None), ('CA', 'United States', None), ('TX', 'USA', None),
    ('MA', 'United Kingdom', None),    # foreign country trumps a US-looking region
    ('', 'United States', None), (None, None, None), ('XX', 'United States', None),
])
def test_territory_code(state, country, expected):
    assert rt.territory_code(state, country) == expected


def test_territory_uses_the_shared_gate_constant():
    """Every code in gates.TERRITORY_STATES is accepted; nothing else is."""
    assert all(rt.territory_code(code) == code for code in TERRITORY_STATES)
    assert len(TERRITORY_STATES) == 30
    assert rt.territory_code('IL') is None and rt.territory_code('BC', 'Canada') is None


@pytest.mark.parametrize('raum, emp, est, basis_has', [
    (2_000_000_000, 30, 12_000_000, 'smaller of'),       # min(14M, 12M)
    (2_000_000_000, 100, 14_000_000, 'smaller of'),      # min(14M, 40M)
    (700_000_000, None, 4_900_000, 'headcount not reported'),
    (None, 12, 4_800_000, 'RAUM not reported'),
    (0, 0, None, 'no RAUM or headcount'),
    (None, None, None, 'no RAUM or headcount'),
    ('1500000000', '10', 4_000_000, 'smaller of'),       # tolerant of text numbers
])
def test_revenue_estimate(raum, emp, est, basis_has):
    got, basis = rt.revenue_estimate(raum, emp)
    assert (got is None and est is None) or got == pytest.approx(est)
    assert basis_has in basis


@pytest.mark.parametrize('est, seg', [
    # P3 / L7 (review 2026-09-08): oracles.estimate_band — the same
    # boundaries (<=) and ±30% margin bands enrichment's registry applies
    (3_499_999, 'LMM'), (3_500_000, None), (4_999_999, None), (5_000_000, None), (6_499_999, None),
    (6_500_000, 'LMM'), (9_999_999, 'LMM'), (10_000_000, 'LMM'), (10_000_001, 'MM'),
    (19_999_999, 'MM'), (20_000_000, 'MM'), (20_000_001, 'Corp'), (76_999_999, 'Corp'),
    (77_000_000, None), (100_000_000, None), (130_000_000, None),
    (130_000_001, 'Enterprise'), (None, None), ('bad', None),
])
def test_revenue_segment(est, seg):
    assert rt.revenue_segment(est) == seg


@pytest.mark.parametrize('n, s', [
    (2_000_000_000, '$2B'), (1_234_567_890, '$1.2B'), (12_000_000, '$12M'),
    (700_000, '$700K'), (950, '$950'), (None, 'n/a'), ('bad', 'n/a'),
])
def test_fmt_money(n, s):
    assert rt.fmt_money(n) == s


def test_event_id_rule_matches_the_scrapers():
    url, title = 'https://adviserinfo.sec.gov/firm/summary/1001', 'New SEC-registered investment adviser: X (Boston, MA)'
    assert rt.generate_event_id(url, title) == BaseScraper.generate_event_id(None, url, title)


def test_seed_columns_are_typed_columns_and_never_rep_or_enrichment_owned():
    assert set(rt.SEED_COLUMNS) <= set(TYPED_EVENT_COLUMNS)
    assert not (set(rt.SEED_COLUMNS) & NEVER_SYNC_COLUMNS)
    assert 'verify_state' not in rt.SEED_COLUMNS      # NULL → enrichment runs normally
    assert rt.SOURCE == 'sec_iapd' and rt.EVENT_TYPE == 'expansion'


# ── Selection: exactly the right firm ────────────────────────────────────────

def test_selects_exactly_the_recent_in_territory_in_band_registered_firm(oracle_db):
    client = FakeClient()
    rc, out = _run(oracle_db, client)
    assert rc == 0
    assert 'would emit 1 event' in out
    assert 'Beacon Hill Capital Advisors LLC' in out
    lines = [l for l in out.splitlines() if 'would emit' in l and l.strip().startswith('1')]
    assert len(lines) == 1 and lines[0].strip().startswith('1001')
    # the funnel counts name every other firm once
    assert 'outside_window=1' in out          # 1005
    assert 'not_registered=1' in out          # 1004 ERA
    assert 'out_of_territory=1' in out        # 1003 CA
    assert 'too_small=1' in out               # 1002
    assert 'already_emitted=1' in out         # 1006 ledger
    assert 'to_emit=1' in out


def test_classify_reasons_for_the_six(oracle_db):
    conn = sqlite3.connect(oracle_db)
    firms = {f['crd']: f for f in rt.load_firms(conn)}
    conn.close()
    reasons = {crd: rt.classify_firm(f, NOW, 45, False)['reason'] for crd, f in firms.items()}
    assert reasons == {1001: '', 1002: 'too_small', 1003: 'out_of_territory',
                       1004: 'not_registered', 1005: 'outside_window', 1006: ''}
    c = rt.classify_firm(firms[1001], NOW, 45, False)
    assert (c['state_code'], c['reg_date_iso'], c['estimate'], c['segment']) == \
        ('MA', RECENT.isoformat(), 12_000_000, 'MM')


def test_include_small_adds_the_small_firm(oracle_db):
    rc, out = _run(oracle_db, FakeClient(), include_small=True)
    assert rc == 0
    assert 'would emit 2 event' in out
    assert 'Tiny Wealth LLC' in out and 'too_small' not in out


def test_include_small_also_admits_size_unknown_firms(tmp_path):
    db = make_db(tmp_path / 'o.db', [_firm(1, 'Blank Data Advisors LLC', 'PORTLAND', 'ME',
                                           employees=None, raum=None)])
    rc, out = _run(db, FakeClient())
    assert rc == 0 and 'size_unknown=1' in out and 'nothing to do' in out
    rc, out = _run(db, FakeClient(), include_small=True)
    assert rc == 0 and 'would emit 1 event' in out and 'unknown' in out


def test_enterprise_firms_are_skipped_not_emitted(tmp_path):
    """> $100M on BOTH proxies would be tombstoned by enrichment's revenue
    gate on arrival — the funnel names it instead of emitting it."""
    db = make_db(tmp_path / 'o.db', [_firm(1, 'Giant Asset Managers LLC', 'NEW YORK', 'NY',
                                           employees=400, raum=20_000_000_000)])
    rc, out = _run(db, FakeClient())
    assert rc == 0 and 'enterprise=1' in out and 'nothing to do' in out
    # the min() rule keeps a big-RAUM, small-staff firm IN band
    est, _ = rt.revenue_estimate(20_000_000_000, 40)
    assert est == 16_000_000 and rt.revenue_segment(est) == 'MM'


def test_window_days_is_inclusive_and_future_dates_are_out(tmp_path):
    edge = (NOW - timedelta(days=rt.WINDOW_DAYS)).date()
    just_out = (NOW - timedelta(days=rt.WINDOW_DAYS + 1)).date()
    future = (NOW + timedelta(days=1)).date()
    db = make_db(tmp_path / 'o.db', [
        _firm(1, 'Edge Advisors LLC', 'BOSTON', 'MA', reg=edge),
        _firm(2, 'Just Out Advisors LLC', 'BOSTON', 'MA', reg=just_out),
        _firm(3, 'Future Advisors LLC', 'BOSTON', 'MA', reg=future),
        _firm(4, 'Bad Date Advisors LLC', 'BOSTON', 'MA', reg='n/a'),
    ])
    rc, out = _run(db, FakeClient())
    assert rc == 0 and 'would emit 1 event' in out and 'Edge Advisors' in out
    assert 'outside_window=2' in out and 'bad_reg_date=1' in out
    rc, out = _run(db, FakeClient(), window_days=rt.WINDOW_DAYS + 1)
    assert 'would emit 2 event' in out


def test_canadian_province_in_territory_when_feed_carries_it(tmp_path):
    db = make_db(tmp_path / 'o.db', [
        _firm(1, 'Bay Street Capital Inc', 'TORONTO', 'ON', country='Canada'),
        _firm(2, 'Prairie Capital Inc', 'CALGARY', 'AB', country='Canada'),
        _firm(3, 'Mayfair Capital Ltd', 'LONDON', 'MA', country='United Kingdom'),
    ])
    rc, out = _run(db, FakeClient())
    assert rc == 0 and 'would emit 1 event' in out and 'Bay Street' in out
    assert 'out_of_territory=2' in out


# ── Event shape ──────────────────────────────────────────────────────────────

def _beacon_event(seed_columns=rt.SEED_COLUMNS):
    firm = dict(zip(FIRM_COLS, SIX[0]))
    c = rt.classify_firm(firm, NOW, 45, False)
    assert c['reason'] == ''
    return c, rt.build_event(c, NOW, seed_columns)


def test_event_shape():
    c, ev = _beacon_event()
    url = 'https://adviserinfo.sec.gov/firm/summary/1001'
    title = 'New SEC-registered investment adviser: Beacon Hill Capital Advisors LLC (Boston, MA)'
    assert ev['source_url'] == url and ev['title'] == title
    assert ev['id'] == BaseScraper.generate_event_id(None, url, title)
    assert ev['company_name'] == 'Beacon Hill Capital Advisors LLC'
    assert ev['event_type'] == 'expansion'
    assert ev['published_date'] == '2026-08-29T00:00:00+00:00'
    assert ev['discovered_at'] == '2026-09-08T12:00:00'          # naive UTC, like the scrapers
    assert json.loads(ev['matched_regions']) == ['MA']           # SEC scraper stores the code
    # typed seeds
    assert ev['source'] == 'sec_iapd'
    assert ev['hq_state'] == 'MA'
    assert ev['zi_subindustry'] == 'Lending & Brokerage'
    assert ev['revenue_segment'] == 'MM'
    assert ev['expires_at'] == '2026-11-27T00:00:00+00:00'       # reg_date + 90 days
    assert ev['expires_at'] == expires_at_for('expansion', ev['published_date'])
    assert 'verify_state' not in ev and 'lead_status' not in ev
    # key order/set: the nine scrape-owned columns first, then the seeds
    assert tuple(ev)[:9] == SCRAPE_OWNED_COLUMNS
    assert tuple(ev)[9:] == rt.SEED_COLUMNS
    assert not (set(ev) & NEVER_SYNC_COLUMNS)


def test_description_is_plain_language_with_structured_facts():
    _, ev = _beacon_event()
    d = ev['description']
    assert d.startswith('Beacon Hill Capital Advisors LLC (MA) registered with the SEC as an '
                        'investment adviser on August 29, 2026')
    assert 'SEC file no. 801-123456' in d and 'CRD 1001' in d
    assert 'Legal name: Beacon Hill Capital Advisors LLC.' in d
    assert 'Regulatory assets under management: $2,000,000,000.' in d
    assert 'Employees: 30.' in d
    assert 'Website: https://www.beaconhillcap.example.' in d
    assert 'Estimated revenue about $12M (basis: the smaller of RAUM x 0.7% ($14M) and ' \
           '30 employees x $400K ($12M)) — NetSuite segment MM.' in d
    assert d.endswith('Structured facts: hq_state MA; registration 2026-08-29; firm type Registered.')
    # never trips the EDGAR structured-verdict parser
    assert 'SIC:' not in d and 'Form D' not in d
    # enrichment's sec.gov HQ seed takes the FIRST '(XX)' — it must be the state
    import re
    assert re.search(r'\(([A-Z]\d|[A-Z]{2})\)', d).group(1) == 'MA'
    assert len(d) <= 2000


def test_description_handles_missing_facts(tmp_path):
    firm = dict(zip(FIRM_COLS, _firm(7, 'Sparse Advisors LLC', 'DOVER', 'DE', employees=None,
                                     raum=900_000_000, website='', sec_number='')))
    c = rt.classify_firm(firm, NOW, 45, False)
    ev = rt.build_event(c, NOW)
    d = ev['description']
    assert 'Employees: not reported.' in d and 'Website:' not in d and 'SEC file no.' not in d
    assert '(CRD 7)' in d
    assert 'RAUM x 0.7% (headcount not reported)' in d
    # $6.3M sits inside the ±30% small-margin band (P3): unknown, no segment
    assert ev['revenue_segment'] is None
    assert 'Estimated revenue about $6.3M (basis: RAUM x 0.7% (headcount not reported)).' in d


def test_build_event_honours_a_reduced_seed_column_set():
    _, ev = _beacon_event(seed_columns=('source', 'expires_at'))
    assert tuple(ev)[9:] == ('source', 'expires_at')
    assert 'hq_state' not in ev


def test_rows_in_a_batch_share_one_key_set(tmp_path):
    firms = [_firm(i, f'Firm {i} Advisors LLC', 'BOSTON', 'MA', employees=None if i % 2 else 30,
                   website='' if i % 3 else None) for i in range(1, 8)]
    keys = set()
    for f in firms:
        c = rt.classify_firm(dict(zip(FIRM_COLS, f)), NOW, 45, True)
        keys.add(tuple(rt.build_event(c, NOW)))
    assert len(keys) == 1


# ── Dry run vs apply ─────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(oracle_db):
    client = FakeClient()
    rc, out = _run(oracle_db, client)
    assert rc == 0
    assert client.upserts == []                                   # nothing written remotely
    assert [r['in'][0] for r in client.reads if r['in']] == ['id']   # one dedup read...
    assert _ledger(oracle_db) == {1006: ('deadbeef' * 4, RECENT.isoformat())}   # ...ledger untouched
    assert 'DRY RUN' in out and 'nothing written' in out


def test_dry_run_without_credentials_skips_remote_dedup(oracle_db):
    rc, out = _run(oracle_db, None)
    assert rc == 0 and 'would emit 1 event' in out
    assert 'remote dedup skipped' in out


def test_apply_upserts_with_ignore_duplicates_and_writes_ledger(oracle_db):
    client = FakeClient()
    rc, out = _run(oracle_db, client, apply=True)
    assert rc == 0
    assert 'APPLIED — emitted 1 event' in out
    assert len(client.upserts) == 1
    call = client.upserts[0]
    assert call['table'] == 'events'
    assert call['on_conflict'] == 'id' and call['ignore_duplicates'] is True
    assert isinstance(call['data'], list) and len(call['data']) == 1
    row = call['data'][0]
    assert row['company_name'] == 'Beacon Hill Capital Advisors LLC'
    assert row['source'] == 'sec_iapd' and row['hq_state'] == 'MA'
    # dedup read happened BEFORE the write, against exactly the candidate id
    assert client.reads and client.reads[0]['in'] == ('id', [row['id']])
    # ledger: (crd → event_id, reg_date); the pre-existing 1006 row is kept
    led = _ledger(oracle_db)
    assert led[1001] == (row['id'], RECENT.isoformat())
    assert led[1006] == ('deadbeef' * 4, RECENT.isoformat())

    # second run: nothing to emit, nothing written
    client2 = FakeClient(rows=client.rows)
    rc, out = _run(oracle_db, client2, apply=True)
    assert rc == 0 and 'nothing to do' in out and client2.upserts == []
    assert 'already_emitted=2' in out
    assert _ledger(oracle_db) == led


def test_apply_without_client_is_refused(oracle_db):
    rc, out = _run(oracle_db, None, apply=True)
    assert rc == 1 and 'ERROR' in out and 'nothing written' in out
    assert _ledger(oracle_db) == {1006: ('deadbeef' * 4, RECENT.isoformat())}


def test_ids_already_in_supabase_are_skipped_and_ledgered_on_apply(oracle_db):
    _, ev = _beacon_event()
    client = FakeClient(rows=[{'id': ev['id'], 'title': ev['title']}])
    rc, out = _run(oracle_db, client)
    assert rc == 0 and 'already_in_supabase=1' in out and 'nothing to do' in out
    assert _ledger(oracle_db).get(1001) is None           # dry run never writes the ledger

    rc, out = _run(oracle_db, client, apply=True)
    assert rc == 0 and client.upserts == []
    assert 'recorded 1 firm(s) already present in Supabase' in out
    assert _ledger(oracle_db)[1001] == (ev['id'], RECENT.isoformat())


def test_reregistration_with_a_new_reg_date_is_a_new_trigger(tmp_path):
    older = (NOW - timedelta(days=800)).date()
    db = make_db(tmp_path / 'o.db',
                 [_firm(1006, 'Already Emitted Partners LLC', 'PHILADELPHIA', 'PA')],
                 ledger=[(1006, 'oldevent', '2024-07-01T05:00:00+00:00', older.isoformat())])
    client = FakeClient()
    rc, out = _run(db, client, apply=True)
    assert rc == 0 and 'emitted 1 event' in out
    led = _ledger(db)
    assert led[1006][1] == RECENT.isoformat() and led[1006][0] != 'oldevent'


def test_limit_caps_emission_newest_first(tmp_path):
    firms = [_firm(i, f'Firm {i} Advisors LLC', 'BOSTON', 'MA', reg=(NOW - timedelta(days=i)).date())
             for i in range(1, 6)]
    db = make_db(tmp_path / 'o.db', firms)
    client = FakeClient()
    rc, out = _run(db, client, apply=True, limit=2)
    assert rc == 0 and 'emitted 2 event' in out
    sent = client.upserts[0]['data']
    assert [r['company_name'] for r in sent] == ['Firm 1 Advisors LLC', 'Firm 2 Advisors LLC']
    assert 'over --limit 2' in out
    assert set(_ledger(db)) == {1, 2}                  # deferred firms are NOT ledgered


def test_batches_of_fifty_for_dedup_and_upsert(tmp_path):
    firms = [_firm(i, f'Firm {i} Advisors LLC', 'BOSTON', 'MA') for i in range(1, 121)]
    db = make_db(tmp_path / 'o.db', firms)
    client = FakeClient()
    rc, out = _run(db, client, apply=True)
    assert rc == 0 and 'emitted 120 event' in out
    assert [len(c['data']) for c in client.upserts] == [50, 50, 20]
    dedup_reads = [r for r in client.reads if r['in']]
    assert [len(r['in'][1]) for r in dedup_reads] == [50, 50, 20]
    assert all(c['ignore_duplicates'] and c['on_conflict'] == 'id' for c in client.upserts)
    assert len(_ledger(db)) == 120


def test_failed_upsert_keeps_ledger_consistent(tmp_path):
    firms = [_firm(i, f'Firm {i} Advisors LLC', 'BOSTON', 'MA') for i in range(1, 61)]
    db = make_db(tmp_path / 'o.db', firms)

    class Flaky(FakeClient):
        def table(self, name):
            q = _Query(self, name)
            orig = q.execute

            def execute():
                if q._op == 'upsert' and len(self.upserts) == 2:
                    raise RuntimeError('simulated PostgREST rejection')
                return orig()
            q.execute = execute
            return q

    client = Flaky()
    rc, out = _run(db, client, apply=True)
    assert rc == 1 and 'upsert failed after 50 row(s)' in out
    assert len(_ledger(db)) == 50                    # only the accepted batch is ledgered


def test_missing_seed_columns_are_dropped_with_a_warning(oracle_db):
    reset_probe_cache()
    client = FakeClient(present=set(SCRAPE_OWNED_COLUMNS) | {'source', 'expires_at'})
    rc, out = _run(oracle_db, client, apply=True)
    assert rc == 0 and 'not live, not sent: hq_state, zi_subindustry, revenue_segment' in out
    row = client.upserts[0]['data'][0]
    assert tuple(row)[9:] == ('source', 'expires_at')
    reset_probe_cache()


# ── Oracle table absent / broken ─────────────────────────────────────────────

def test_absent_db_file_prints_message_and_exits_zero(tmp_path):
    rc, out = _run(str(tmp_path / 'nope.db'), FakeClient(), apply=True)
    assert rc == 0 and 'not found' in out and 'nothing to do' in out
    assert not (tmp_path / 'nope.db').exists()        # sqlite must not create it


def test_absent_table_prints_message_and_exits_zero(tmp_path):
    db = tmp_path / 'oracles.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE fdic_bank(cert INTEGER PRIMARY KEY)')
    conn.commit(); conn.close()
    client = FakeClient()
    rc, out = _run(str(db), client, apply=True)
    assert rc == 0 and 'ria_firm is not in' in out and 'nothing to do' in out
    assert client.upserts == [] and client.reads == []


def test_contract_drift_is_a_real_error(tmp_path):
    db = tmp_path / 'oracles.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE ria_firm(crd INTEGER PRIMARY KEY, business_name TEXT)')
    conn.commit(); conn.close()
    rc, out = _run(str(db), FakeClient())
    assert rc == 1 and 'does not match the oracle contract' in out


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_main_defaults_to_dry_run_and_never_builds_a_client_for_apply_less_runs(oracle_db, monkeypatch):
    seen = {}

    def fake_get_client(required):
        seen['required'] = required
        return None
    monkeypatch.setattr(rt, 'get_client', fake_get_client)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = rt.main(['--db', oracle_db, '--window-days', '400'])
    assert rc == 0 and seen == {'required': False}
    text = out.getvalue()
    assert 'DRY RUN' in text and 'would emit' in text
    assert _ledger(oracle_db) == {1006: ('deadbeef' * 4, RECENT.isoformat())}


def test_main_apply_requires_credentials(oracle_db, monkeypatch):
    def fake_get_client(required):
        raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required')
    monkeypatch.setattr(rt, 'get_client', fake_get_client)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = rt.main(['--db', oracle_db, '--apply'])
    assert rc == 1 and 'ERROR' in out.getvalue()


def test_dry_run_and_apply_are_mutually_exclusive(oracle_db):
    with pytest.raises(SystemExit):
        rt.main(['--db', oracle_db, '--dry-run', '--apply'])


# ── Review 2026-09-08 ────────────────────────────────────────────────────────

def test_m5_window_is_75_days_and_spans_a_fallback_month():
    """The job runs on the 2nd; a 404 loads the PREVIOUS month's compilation,
    which stops at the end of the month before that. 45 days from the 2nd
    reached back only to ~the 18th of that month: two weeks of registrations
    were never emitted. 75 days always covers the fallback month plus the
    one after it; the ledger dedups the overlap."""
    assert rt.WINDOW_DAYS == 75
    run_day = date(2026, 10, 2)                                   # a 404 → the 09-01 feed → through 08-31
    oldest_needed = date(2026, 8, 1)                               # the fallback month's first day
    assert run_day - timedelta(days=rt.WINDOW_DAYS) <= oldest_needed
    assert run_day - timedelta(days=45) > oldest_needed            # the old window lost ~2 weeks
    assert 'default 75' in rt.__doc__ and 'MM/DD/YYYY' in rt.__doc__ and "ISO" in rt.__doc__
    assert '1st of' not in rt.__doc__


def test_m1_too_small_only_when_the_larger_proxy_is_under_the_floor(tmp_path):
    """10 staff ($4M) + $2B RAUM ($14M) was 'too_small' on the pessimistic
    proxy. Now: the MIN places the segment (unknown — inside the margin
    band), the MAX decides too_small, and the firm is emitted."""
    db = make_db(tmp_path / 'o.db', [
        _firm(1, 'Ten Staff Advisors LLC', 'BOSTON', 'MA', employees=10, raum=2_000_000_000),
        _firm(2, 'Tiny Zorblat Advisors LLC', 'BOSTON', 'MA', employees=3, raum=100_000_000),  # $1.2M / $0.7M
        _firm(3, 'Solo Zorblat Advisors LLC', 'BOSTON', 'MA', employees=10, raum=None),       # $4M only
        _firm(4, 'Bracket Zorblat Advisors LLC', 'BOSTON', 'MA', employees=300, raum=20_000_000_000),  # $120M / $140M
    ])
    conn = sqlite3.connect(db)
    firms = {f['crd']: f for f in rt.load_firms(conn)}
    conn.close()
    # A manufactured trigger needs BOTH proxies to clear the floor: a segment
    # of None (proxies disagree, or a lone estimate inside a margin band) is
    # 'size_borderline' and not emitted unless --include-small (2026-09-08:
    # the margin rule alone lifted one month's emission from 4 to 40 firms).
    c1 = rt.classify_firm(firms[1], NOW, rt.WINDOW_DAYS, False)
    assert c1['reason'] == 'size_borderline' and c1['estimate'] == 4_000_000 and c1['segment'] is None
    assert rt.revenue_proxies(2_000_000_000, 10) == (14_000_000.0, 4_000_000.0)
    assert rt.classify_firm(firms[2], NOW, rt.WINDOW_DAYS, False)['reason'] == 'too_small'
    c3 = rt.classify_firm(firms[3], NOW, rt.WINDOW_DAYS, False)
    assert c3['reason'] == 'size_borderline' and c3['segment'] is None   # $4M ± 30%: margin band
    c4 = rt.classify_firm(firms[4], NOW, rt.WINDOW_DAYS, False)
    assert c4['reason'] == 'size_borderline' and c4['segment'] is None   # $120M: Enterprise margin band
    assert rt.classify_firm(firms[1], NOW, rt.WINDOW_DAYS, True)['reason'] == ''  # --include-small admits them
    rc, out = _run(db, FakeClient())
    assert rc == 0 and 'nothing to do' in out and 'too_small=1' in out and 'size_borderline=3' in out
    ev = rt.build_event(c1, NOW)
    assert ev['revenue_segment'] is None and 'Estimated revenue about $4M' in ev['description']
    # the constants are the registry's — never a second copy that can drift
    from src.pipeline import oracles as o
    assert (rt.REVENUE_BAR, rt.ENTERPRISE_ABOVE) == (o.TOO_SMALL_USD, o.ENTERPRISE_USD)
    assert (rt.RAUM_REVENUE_RATE, rt.REVENUE_PER_EMPLOYEE) == (o.RIA_RAUM_TO_REV, o.RIA_REV_PER_EMP)
    assert rt.revenue_segment(150_000_000) == o.estimate_band(150_000_000)[0] == 'Enterprise'
