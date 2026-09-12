"""Tests for the v2 typed-column layer (src/pipeline/typed.py), the
gates.hq_state_code port and the backfill's pure row→payload function.
No network: a fake client answers probe_columns."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.pipeline import typed
from src.pipeline.gates import hq_state_code
from src.pipeline.typed import (
    EXPIRY_DAYS, MAX_ENRICH_ATTEMPTS, RETRY_BACKOFF_DAYS, TYPED_EVENT_COLUMNS,
    expires_at_for, is_adzuna, llm_retry_after, not_fit_payload, parse_sec_fields,
    probe_columns, reset_probe_cache, retry_after_for, typed_payload, verify_state_for,
)
from scripts.backfill_typed_columns import row_payload

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
ALL = set(TYPED_EVENT_COLUMNS)


# ── verify_state_for ────────────────────────────────────────────────────────
@pytest.mark.parametrize('verdict,blocked,expected', [
    ('pass', False, 'verified'), ('unverified', False, 'researched_ambiguous'),
    ('staged', False, 'staged'), ('decided', False, 'decided'), ('fail', False, 'not_fit'),
    ('PASS', False, 'verified'), (' pass ', False, 'verified'),
    ('pass', True, 'not_fit'), ('unverified', True, 'not_fit'), (None, True, 'not_fit'),
    (None, False, None), ('', False, None), ('unknown', False, None),
])
def test_verify_state_for(verdict, blocked, expected):
    assert verify_state_for(verdict, blocked=blocked) == expected


# ── expires_at_for ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('published,discovered,etype,expected', [
    ('2026-09-01T10:00:00Z', None, 'cfo_hire', '2026-10-31T10:00:00+00:00'),
    ('2026-09-01T10:00:00', None, 'funding', '2026-10-31T10:00:00+00:00'),       # naive
    ('2026-09-01', None, 'merger_acquisition', '2026-12-30T00:00:00+00:00'),    # date-only
    ('2026-09-01T06:00:00-04:00', None, 'expansion', '2026-11-30T10:00:00+00:00'),
    ('2026-09-07T02:54:15.747119', None, 'stable_target', '2027-09-07T02:54:15.747119+00:00'),
    (None, '2026-09-01T00:00:00Z', 'other', '2026-10-31T00:00:00+00:00'),      # fallback
    ('', '2026-09-01', 'finance_seat_open', '2026-10-31T00:00:00+00:00'),
    ('garbage', '2026-09-01', 'executive_hire', '2026-10-31T00:00:00+00:00'),
    ('2026-09-01', None, 'never_seen', '2026-10-31T00:00:00+00:00'),           # unknown type → 'other' 60d
    (None, None, 'cfo_hire', None), ('', '', 'cfo_hire', None), ('garbage', 'nope', 'cfo_hire', None),
])
def test_expires_at_for(published, discovered, etype, expected):
    assert expires_at_for(etype, published, discovered) == expected


def test_expiry_table_matches_contract():
    assert EXPIRY_DAYS == {'cfo_hire': 60, 'finance_seat_open': 60, 'executive_hire': 60,
                           'funding': 60, 'merger_acquisition': 120, 'expansion': 90,
                           'stable_target': 365, 'other': 60}


# ── retry ladder ────────────────────────────────────────────────────────────
@pytest.mark.parametrize('attempts,days', [(0, 7), (1, 7), (2, 30), (3, None), (4, None), (None, 7)])
def test_retry_after_ladder(attempts, days):
    got = retry_after_for('researched_ambiguous', attempts, now=NOW)
    if days is None:
        assert got is None
    else:
        assert got == (NOW + timedelta(days=days)).isoformat()


def test_retry_after_constants():
    """Contract settled at review 2026-09-07: two rungs, then parked. A third
    rung was unreachable (attempt 3 == MAX_ENRICH_ATTEMPTS returns None), so
    the ladder must have exactly MAX_ENRICH_ATTEMPTS - 1 entries."""
    assert RETRY_BACKOFF_DAYS == (7, 30) and MAX_ENRICH_ATTEMPTS == 3
    assert len(RETRY_BACKOFF_DAYS) == MAX_ENRICH_ATTEMPTS - 1
    # every rung is reachable, and the attempt after the last rung parks
    for n, days in enumerate(RETRY_BACKOFF_DAYS, start=1):
        assert retry_after_for('researched_ambiguous', n, now=NOW) == (NOW + timedelta(days=days)).isoformat()
    assert retry_after_for('researched_ambiguous', MAX_ENRICH_ATTEMPTS, now=NOW) is None
    # the per-account search ladder in cache.py is a different contract
    from src.pipeline.cache import NEGATIVE_BACKOFF_DAYS
    assert NEGATIVE_BACKOFF_DAYS == (7, 30, 90)


@pytest.mark.parametrize('state', ['verified', 'staged', 'decided', 'not_fit', None, ''])
def test_retry_after_other_states_none(state):
    assert retry_after_for(state, 1, now=NOW) is None


def test_llm_retry_after():
    assert llm_retry_after(now=NOW) == (NOW + timedelta(hours=4)).isoformat()
    naive = datetime(2026, 9, 7, 12, 0)   # naive now is treated as UTC
    assert llm_retry_after(now=naive) == '2026-09-07T16:00:00+00:00'


# ── typed_payload ───────────────────────────────────────────────────────────
SEC_EVENT = {
    'company_name': 'GS Finance Corp.', 'event_type': 'funding',
    'published_date': '2026-09-04', 'discovered_at': '2026-09-04T12:00:00Z',
    'source_url': 'https://www.sec.gov/Archives/edgar/data/1/000000000126000001/xslFormDX01/primary_doc.xml',
    'title': 'SEC Form D (Private Capital Raise) — GS Finance Corp.',
    'description': ('SEC Form D filed by GS Finance Corp. (NY). Total offering: $2,000,000. '
                    'Form D industry group: Investment Banking. Declared revenue: '
                    '$5,000,001 - $25,000,000. SPAC: yes. SIC: 6211 (SECURITY BROKERS).'),
}
FIT = {'verdict': 'unverified', 'territory': 'in', 'revenue': 'unknown', 'vertical': 'in',
       'zi_subindustry': 'Investment Banking', 'account_name': 'GS Finance Corp.', 'reasons': []}
ACCOUNT = {'name': 'GS Finance Corp.', 'hq': 'New York, NY', 'revenue': 'LMM',
           'zi_subindustry': 'Investment Banking'}


def test_typed_payload_full():
    p = typed_payload(event=SEC_EVENT, fit=FIT, structured={'verdict': 'unknown', 'reason': '',
                      'revenue_segment': 'LMM'}, account=ACCOUNT, verify_state='researched_ambiguous',
                      present=ALL, now=NOW, attempts=1, retry_after='2026-09-14T12:00:00+00:00',
                      classification_confidence='High', classified_by='structured')
    assert p == {
        'account_key': 'gs finance', 'fit_verdict': 'unverified',
        'verify_state': 'researched_ambiguous', 'hq_state': 'NY', 'in_territory': 'in',
        'vertical': 'in', 'zi_subindustry': 'Investment Banking', 'revenue_segment': 'LMM',
        'expires_at': '2026-11-03T00:00:00+00:00',
        'sic': '6211', 'formd_industry_group': 'Investment Banking',
        'formd_revenue_range': '$5,000,001 - $25,000,000', 'formd_offering_amount': 2000000.0,
        'formd_is_spac': True, 'enrich_attempts': 1,
        'retry_after': '2026-09-14T12:00:00+00:00',
        'classification_confidence': 'High', 'classified_by': 'structured',
    }
    assert set(p) <= ALL


def test_typed_payload_filters_to_present():
    present = {'verify_state', 'account_key'}
    p = typed_payload(event=SEC_EVENT, fit=FIT, account=ACCOUNT,
                      verify_state='verified', present=present, now=NOW, attempts=2, retry_after=None)
    assert p == {'verify_state': 'verified', 'account_key': 'gs finance'}
    assert typed_payload(event=SEC_EVENT, fit=FIT, verify_state='verified', present=set()) == {}
    assert typed_payload(event=SEC_EVENT, fit=FIT, verify_state='verified', present=None) == {}


def test_typed_payload_retry_after_sentinel():
    base = dict(event=SEC_EVENT, fit=FIT, account=ACCOUNT, verify_state='verified', present=ALL, now=NOW)
    assert 'retry_after' not in typed_payload(**base)                 # not passed → not written
    assert typed_payload(**base, retry_after=None)['retry_after'] is None   # passed None → clears
    assert typed_payload(**base, retry_after='x')['retry_after'] == 'x'
    assert 'enrich_attempts' not in typed_payload(**base)
    assert typed_payload(**base, attempts=0)['enrich_attempts'] == 0


def test_typed_payload_structured_keys_win_over_description():
    s = {'verdict': 'unknown', 'reason': '', 'revenue_segment': '', 'sic': '7372',
         'industry_group': 'Other', 'revenue_range': 'No Revenues', 'offering_amount': '5', 'spac': False}
    p = typed_payload(event=SEC_EVENT, fit=FIT, structured=s, account={}, verify_state='staged', present=ALL)
    assert (p['sic'], p['formd_industry_group'], p['formd_revenue_range'],
            p['formd_offering_amount'], p['formd_is_spac']) == ('7372', 'Other', 'No Revenues', 5.0, False)


def test_typed_payload_non_sec_and_thin_inputs():
    ev = {'company_name': 'The Acme Widget Co.', 'event_type': 'cfo_hire',
          'published_date': None, 'discovered_at': '2026-09-01T00:00:00Z',
          'source_url': 'https://www.prnewswire.com/x', 'title': 'Acme names CFO', 'description': ''}
    p = typed_payload(event=ev, fit=None, account=None, verify_state=None, present=ALL, now=NOW)
    assert p['account_key'] == 'acme widget'           # falls back to company_name
    # unknowns are DROPPED, not sent as NULL (fill-only, review 2026-09-07)
    for k in ('fit_verdict', 'verify_state', 'hq_state', 'sic', 'formd_is_spac',
              'formd_offering_amount', 'in_territory', 'vertical', 'zi_subindustry',
              'revenue_segment', 'classification_confidence', 'classified_by'):
        assert k not in p, k
    assert p['expires_at'] == '2026-10-31T00:00:00+00:00'
    assert p == {'account_key': 'acme widget', 'expires_at': '2026-10-31T00:00:00+00:00'}
    # decided rows carry 'n/a' dims → not a contract value → not written
    p2 = typed_payload(event=ev, fit={'verdict': 'decided', 'territory': 'n/a', 'vertical': 'n/a'},
                       account={'revenue': 'huge'}, verify_state='decided', present=ALL)
    assert 'in_territory' not in p2 and 'vertical' not in p2 and 'revenue_segment' not in p2
    assert p2['fit_verdict'] == 'decided' and p2['verify_state'] == 'decided'


def test_typed_payload_is_fill_only_for_fact_columns():
    """Review 2026-09-07: a re-enrichment that knows less than the last one
    must not blank what was learned. The rep-decided branch in
    enrichment_scout passes no account and retry_after=None: the payload
    may clear retry_after but must carry NO None-valued fact column, so the
    row's hq_state / zi_subindustry / revenue_segment / sic / formd_* /
    classification_* survive the update."""
    decided = {'verdict': 'decided', 'account_name': 'GS Finance Corp.',
               'territory': 'n/a', 'revenue': 'n/a', 'vertical': 'n/a', 'reasons': ['rep: WON']}
    p = typed_payload(event=SEC_EVENT, fit=decided, structured=None, account=None,
                      verify_state='decided', present=ALL, now=NOW, retry_after=None)
    assert p['retry_after'] is None                      # the ONE intentional None
    assert all(v is not None for k, v in p.items() if k != 'retry_after')
    for k in ('hq_state', 'zi_subindustry', 'revenue_segment', 'classification_confidence',
              'classified_by', 'in_territory', 'vertical'):
        assert k not in p, k
    # SEC facts parsed from the event itself are still filled (they are facts)
    assert p['sic'] == '6211' and p['formd_is_spac'] is True
    assert p['verify_state'] == 'decided' and p['fit_verdict'] == 'decided'
    # a None hq (account present but unreadable) never appears either
    p2 = typed_payload(event=SEC_EVENT, fit=FIT, account={'name': 'GS', 'hq': 'Remote'},
                       verify_state='verified', present=ALL, now=NOW)
    assert 'hq_state' not in p2 and p2['verify_state'] == 'verified'
    # retry_after passed explicitly as None survives even when it is the only key
    assert typed_payload(event={}, fit=None, verify_state=None, present={'retry_after', 'hq_state'},
                         retry_after=None) == {'retry_after': None}


def test_parse_sec_fields_only_for_sec():
    assert parse_sec_fields({'source_url': 'https://x.com', 'description': 'SIC: 1234'})['sic'] is None
    f = parse_sec_fields({'source_url': 'https://www.sec.gov/a', 'title': 'SEC 8-K Item 5.02',
                          'description': 'Something. SIC: 6022 (STATE COMMERCIAL BANKS).'})
    assert f == {'sic': '6022', 'industry_group': None, 'revenue_range': None,
                 'offering_amount': None, 'spac': None}


def test_not_fit_payload():
    assert not_fit_payload(ALL, reason='x', now=NOW) == {'verify_state': 'not_fit', 'fit_verdict': 'fail'}
    assert not_fit_payload({'verify_state'}) == {'verify_state': 'not_fit'}
    assert not_fit_payload(set()) == {}


@pytest.mark.parametrize('url,expected', [
    ('https://www.adzuna.com/details/123', True), ('https://www.adzuna.ca/details/5?utm=x', True),
    ('https://www.sec.gov/x', False), ('https://example.com/?ref=adzuna', False), ('', False), (None, False),
])
def test_is_adzuna(url, expected):
    assert is_adzuna({'source_url': url}) is expected


# ── probe_columns ───────────────────────────────────────────────────────────
class _FakeClient:
    def __init__(self, cols, broken=False):
        self.cols, self.broken, self.calls = set(cols), broken, []

    def table(self, name):
        if self.broken:
            raise RuntimeError('client is dead')
        return _FakeTable(self, name)


class _FakeTable:
    def __init__(self, client, name):
        self.client, self.name, self.col = client, name, None

    def select(self, col):
        self.col = col
        return self

    def limit(self, n):
        return self

    def execute(self):
        self.client.calls.append((self.name, self.col))
        if self.col not in self.client.cols:
            raise Exception(f'column events.{self.col} does not exist')
        return SimpleNamespace(data=[])


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    reset_probe_cache()
    yield
    reset_probe_cache()


def test_probe_columns_reports_present_and_memoizes():
    c = _FakeClient({'id', 'verify_state', 'fit_verdict'})
    got = probe_columns(c, 'events', ('verify_state', 'fit_verdict', 'hq_state'))
    assert got == {'verify_state', 'fit_verdict'}
    n = len(c.calls)
    assert probe_columns(c, 'events', ('verify_state', 'fit_verdict', 'hq_state')) == got
    assert len(c.calls) == n                                   # memoized: no new selects
    probe_columns(c, 'events', ('verify_state', 'id'))         # only the NEW column is probed
    assert len(c.calls) == n + 1
    assert probe_columns(c, 'source_status', ('items_fetched',)) == set()   # per-table cache
    reset_probe_cache()
    probe_columns(c, 'events', ('verify_state',))
    assert len(c.calls) == n + 3


def test_probe_columns_dead_client_is_json_only():
    assert probe_columns(_FakeClient(set(), broken=True), 'events', TYPED_EVENT_COLUMNS) == set()
    assert probe_columns(None, 'events', ('verify_state',)) == set()


# ── gates.hq_state_code ─────────────────────────────────────────────────────
@pytest.mark.parametrize('hq,expected', [
    ('Boston, MA', 'MA'), ('Boston, MA, USA', 'MA'), ('Boston, Massachusetts', 'MA'),
    ('Toronto, Ontario, Canada', 'ON'), ('Toronto, ON, Canada', 'ON'), ('massachusetts', 'MA'),
    ('Boston MA', 'MA'), ('Portland ME', None), ('Portland, ME', 'ME'), ('Indianapolis, IN', 'IN'),
    ('Washington, D.C.', 'DC'), ('Washington DC', 'DC'), ('Charleston, West Virginia', 'WV'),
    ('Seattle, Washington', 'WA'), ('Vancouver, BC, Canada', 'BC'), ('MA', 'MA'), ('pa', 'PA'),
    ('Remote', None), ('London, UK', None), ('North America', None), ('Unknown', None),
    ('', None), (None, None), (float('nan'), None),
])
def test_hq_state_code(hq, expected):
    assert hq_state_code(hq) == expected


# ── backfill row → payload ──────────────────────────────────────────────────
def _row(**over):
    base = dict(SEC_EVENT, id='e1', fit=dict(FIT), blocked_at=None,
                companies_data=[{'name': 'Other Co', 'role': 'Investor', 'hq': 'Austin, TX'},
                                dict(ACCOUNT, role='Portfolio Company')])
    base.update({c: None for c in TYPED_EVENT_COLUMNS})
    base['enrich_attempts'] = 0
    base.update(over)
    return base


def test_backfill_row_payload_fills_only_nulls():
    p = row_payload(_row(), ALL, now=NOW, structured_fn=lambda r: {'verdict': 'unknown', 'reason': '',
                                                                    'revenue_segment': 'LMM'})
    assert p['account_key'] == 'gs finance' and p['hq_state'] == 'NY'      # account = fit.account_name
    assert p['verify_state'] == 'researched_ambiguous' and p['fit_verdict'] == 'unverified'
    assert p['revenue_segment'] == 'LMM' and p['sic'] == '6211' and p['formd_is_spac'] is True
    assert 'enrich_attempts' not in p and 'retry_after' not in p and 'event_type' not in p
    assert 'lead_status' not in p and 'notes' not in p and 'grade' not in p
    # already-filled typed values are never overwritten, None-valued ones never written
    p2 = row_payload(_row(hq_state='MA', verify_state='verified'), ALL, now=NOW)
    assert 'hq_state' not in p2 and 'verify_state' not in p2 and p2['account_key'] == 'gs finance'
    assert all(v is not None for v in p2.values())


def test_backfill_row_payload_adzuna_relabel_and_tombstone():
    adz = _row(source_url='https://www.adzuna.ca/details/1', event_type='cfo_hire',
               title='CFO', description='', fit=None, companies_data=None)
    p = row_payload(adz, ALL, now=NOW)
    assert p['event_type'] == 'finance_seat_open'
    assert p['account_key'] == 'gs finance' and 'verify_state' not in p and 'sic' not in p
    assert row_payload(_row(source_url='https://www.adzuna.com/x', event_type='funding'),
                       ALL, now=NOW).get('event_type') is None
    # a tombstone is not_fit whatever the JSON verdict says
    assert row_payload(_row(blocked_at='2026-09-01T00:00:00'), ALL, now=NOW)['verify_state'] == 'not_fit'
    # nothing to do → {}
    filled = _row(**{c: 'x' for c in TYPED_EVENT_COLUMNS})
    assert row_payload(filled, ALL, now=NOW) == {}
    # absent columns (migration not applied) → only the relabel survives
    assert row_payload(adz, set(), now=NOW) == {'event_type': 'finance_seat_open'}


def test_backfill_structured_fn_failures_are_ignored():
    def boom(r):
        raise RuntimeError('no')
    p = row_payload(_row(), ALL, now=NOW, structured_fn=boom)
    assert p['sic'] == '6211'                  # description parse still fills SEC facts
    assert p['revenue_segment'] == 'LMM'       # from the account dict


# ── 2026-09-11: a timeout is not a missing column ───────────────────────────
class _ProbeClient:
    """select(col) raises `errors[col]` when present, else answers."""
    def __init__(self, errors):
        self.errors, self.calls = errors, []

    def table(self, name):
        client = self

        class _Q:
            def select(self_inner, col):
                client.calls.append(col)
                err = client.errors.get(col)

                class _E:
                    def limit(self_e, n):
                        return self_e

                    def execute(self_e):
                        if err:
                            raise err
                        return type('R', (), {'data': []})()
                return _E()
        return _Q()


def test_is_schema_error_classifies_postgres_markers():
    from src.pipeline.typed import is_schema_error
    assert is_schema_error(Exception("{'message': 'column events.foo does not exist', 'code': '42703'}"))
    assert is_schema_error(Exception('PGRST204: Could not find the foo column'))
    assert is_schema_error(Exception("relation \"public.accounts\" does not exist (42P01)"))
    assert not is_schema_error(TimeoutError('The read operation timed out'))
    assert not is_schema_error(Exception('Server error 502 Bad Gateway'))
    assert not is_schema_error(ConnectionError('connection refused'))


def test_probe_columns_transport_failure_is_absent_for_this_call_only(monkeypatch):
    from src.pipeline import typed
    typed.reset_probe_cache()
    slow = _ProbeClient({'fit': TimeoutError('timed out')})
    assert typed.probe_columns(slow, 'events', ('fit', 'grade')) == {'grade'}
    # not memoized: a healthy client on the next call finds it
    ok = _ProbeClient({})
    assert typed.probe_columns(ok, 'events', ('fit', 'grade')) == {'fit', 'grade'}
    assert 'fit' in ok.calls


def test_probe_columns_schema_error_is_memoized_as_absent():
    from src.pipeline import typed
    typed.reset_probe_cache()
    missing = _ProbeClient({'fit': Exception("column events.fit does not exist (42703)")})
    assert typed.probe_columns(missing, 'events', ('fit', 'grade')) == {'grade'}
    ok = _ProbeClient({})
    assert typed.probe_columns(ok, 'events', ('fit', 'grade')) == {'grade'}   # remembered
    assert 'fit' not in ok.calls


def test_probe_columns_strict_raises_on_transport_only():
    from src.pipeline import typed
    typed.reset_probe_cache()
    slow = _ProbeClient({'fit': TimeoutError('The read operation timed out')})
    with pytest.raises(typed.ProbeUnavailable) as ei:
        typed.probe_columns(slow, 'events', ('grade', 'fit'), strict=True)
    assert 'events.fit' in str(ei.value) and 'timed out' in str(ei.value)
    typed.reset_probe_cache()
    missing = _ProbeClient({'fit': Exception('42703 does not exist')})
    assert typed.probe_columns(missing, 'events', ('grade', 'fit'), strict=True) == {'grade'}
