"""Phase 2 enrichment (enrichment_scout.py, M5): classify-then-research,
verify_state / retry semantics, account + negative cache, LLM-unavailable
handling, pagination and the run lock.

No network: llm_json, _firecrawl_search, _tavily_search, _propublica_990,
check_required_keys and get_supabase are all monkeypatched; CACHE_DB_PATH
and _STATE_DIR point at tmp_path. The fake Supabase client records every
.update payload so each write path can be asserted on exactly."""
import copy
import functools
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import enrichment_scout as es  # noqa: E402
from src.pipeline import typed  # noqa: E402
from src.pipeline.cache import AccountCache  # noqa: E402
from src.pipeline.typed import (  # noqa: E402
    TYPED_EVENT_COLUMNS, reset_probe_cache, MAX_ENRICH_ATTEMPTS,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Retry dates are asserted as exact ISO strings against this clock
# (review 2026-09-07: wall-clock windows made the tests time-dependent).
# The ladder's first rung is +7d; +4h is the LLM-outage push. Nothing here
# depends on the third rung, which is being retired.
FIXED_NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
RETRY_7D = '2026-09-14T12:00:00+00:00'
RETRY_4H = '2026-09-07T16:00:00+00:00'

# The real backend helper, captured before the `env` fixture stubs it, for
# the transport-failure tests that need the genuine HTTP handling.
_REAL_FIRECRAWL = es._firecrawl_search

BASE_COLS = {
    'id', 'company_name', 'event_type', 'title', 'description', 'source_url',
    'fit', 'published_date', 'discovered_at', 'companies_data', 'enriched_at',
    'blocked_at', 'blocked_reason', 'grade', 'confidence_level', 'numeric_score',
    'hashtags', 'grade_justification', 'cfo_status', 'research_notes',
}
TYPED_COLS = BASE_COLS | set(TYPED_EVENT_COLUMNS)


# ── Fake Supabase client ────────────────────────────────────────────────────
class _Query:
    def __init__(self, client, table):
        self.client, self.table_name = client, table
        self.filters, self._select, self._update, self._range = [], None, None, None
        self.not_ = self          # .not_.is_(...) — negation ignored by the fake

    def select(self, cols):
        self._select = cols
        return self

    def limit(self, n):
        return self

    def order(self, col, desc=False):
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def is_(self, col, val):
        self.filters.append(('is', col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(('in', col, list(vals)))
        return self

    def or_(self, f):
        self.filters.append(('or', f))
        return self

    def lt(self, col, v):
        self.filters.append(('lt', col, v))
        return self

    def eq(self, col, v):
        self.filters.append(('eq', col, v))
        return self

    def update(self, payload):
        self._update = payload
        return self

    def delete(self):
        self._update = {'__deleted__': True}
        return self

    def execute(self):
        if self._update is not None:
            eid = next(v for (op, c, v) in self.filters if op == 'eq' and c == 'id')
            self.client.updates.append((eid, copy.deepcopy(self._update)))
            return SimpleNamespace(data=[])
        if self.table_name == 'account_dispositions':
            return SimpleNamespace(data=list(self.client.dispositions))
        cols = [c.strip() for c in (self._select or '').split(',') if c.strip()]
        missing = [c for c in cols if c not in self.client.columns]
        if missing:
            raise Exception(f'column events.{missing[0]} does not exist')
        self.client.selects.append(list(self.filters))
        rows = list(self.client.events)
        if self._range:
            a, b = self._range
            rows = rows[a:b + 1]
        return SimpleNamespace(data=[copy.deepcopy(r) for r in rows])


class FakeClient:
    def __init__(self, events, columns, dispositions=None):
        self.events, self.columns = events, set(columns)
        self.dispositions = dispositions or []
        self.updates, self.selects = [], []

    def table(self, name):
        return _Query(self, name)

    def payload_for(self, eid):
        merged = {}
        for e, pl in self.updates:
            if e == eid:
                merged.update(pl)
        return merged


# ── LLM stub ────────────────────────────────────────────────────────────────
class LLMStub:
    """Dispatches on the prompt: canary / extract / article-only
    firmographics / search firmographics / grade. `down` lists the kinds
    that simulate an outage (returns {} and flips LLM_STATE)."""

    def __init__(self, companies=None, article=None, search=None, grade=None, down=()):
        self.answers = {
            'canary': {'ok': True},
            'extract': {'companies': companies or []},
            'article': article or {},
            'search': search or {},
            'grade': grade or {},
        }
        self.down = set(down)
        self.calls = []

    @staticmethod
    def kind(prompt):
        if '{"ok": true}' in prompt:
            return 'canary'
        if prompt.startswith('You are a business analyst'):
            return 'extract'
        if prompt.startswith('Extract firmographic data'):
            article_only = re.search(r'Search results:\n\s*\nCRITICAL', prompt)
            return 'article' if article_only else 'search'
        if prompt.startswith('You are a lead-grading assistant'):
            return 'grade'
        return 'other'

    def __call__(self, prompt, max_tokens=600):
        k = self.kind(prompt)
        self.calls.append(k)
        if k in self.down:
            es.LLM_STATE['unavailable'] = True
            return {}
        es.LLM_STATE['unavailable'] = False
        return copy.deepcopy(self.answers.get(k) or {})

    def count(self, kind):
        return sum(1 for c in self.calls if c == kind)


GRADE_B = {'grade': 'B', 'confidence': 'High', 'numeric_score': 5,
           'hashtags': ['#NewCFO'], 'cfo_status': 'New',
           'grade_justification': '#NewCFO +5 = 5 → B', 'research_notes': []}
HIT = {'results': [{'title': 'Zorblat Robotics', 'url': 'https://zorblat.example',
                    'content': 'Zorblat Robotics — company profile'}]}


# > ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS: an article-only OTHER-High may
# tombstone only when the model had a real article to read. SHORT_DESC is
# the one-line stub that must fall through to Stage B instead.
LONG_DESC = ('BOSTON, MA — Zorblat Robotics Inc, a maker of warehouse robotics and '
             'automation systems for regional distributors, today announced the '
             'appointment of Jane Doe as Chief Financial Officer, effective '
             'immediately. Doe joins from a Boston fintech.')
SHORT_DESC = ('BOSTON, MA — Zorblat Robotics Inc today appointed Jane Doe as Chief '
              'Financial Officer of the company.')
assert len(LONG_DESC) > es.ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS
assert len(SHORT_DESC) <= es.ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS


def _event(**kw):
    ev = {'id': 'ev1', 'company_name': 'Zorblat Robotics Inc',
          'event_type': 'cfo_hire',
          'title': 'Zorblat Robotics Inc names Jane Doe as CFO',
          'description': LONG_DESC,
          'source_url': 'https://www.businesswire.com/news/zorblat-cfo',
          'fit': None, 'published_date': '2026-09-01T12:00:00+00:00',
          'discovered_at': '2026-09-02T00:00:00+00:00'}
    ev.update(kw)
    return ev


COMPANIES = [{'name': 'Zorblat Robotics Inc', 'role': 'Hiring Company',
              'descriptor': 'robotics maker'}]


# ── Environment ─────────────────────────────────────────────────────────────
@pytest.fixture
def env(monkeypatch, tmp_path):
    """Everything an enrich_events() run touches, pointed at tmp_path and
    stubbed. Returns a namespace with the recorders."""
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(es, 'CACHE_DB_PATH', str(tmp_path / 'cache.db'))
    monkeypatch.setattr(es, '_STATE_DIR', str(state))
    monkeypatch.setattr(es, 'check_required_keys', lambda: None)
    monkeypatch.setattr(es, 'SEARCH_BACKEND', 'firecrawl')
    monkeypatch.setattr(es, 'ANTHROPIC_API_KEY', '')
    monkeypatch.setattr(es, 'TAVILY_API_KEY', 'test-key-never-used')
    monkeypatch.setattr(es, 'RATE_LIMIT_SECONDS', 0)
    monkeypatch.setattr(es.time, 'sleep', lambda s: None)
    monkeypatch.setattr(es, '_propublica_990', lambda name: None)
    fc = SimpleNamespace(calls=[], result={})
    tv = SimpleNamespace(calls=[], result={})

    def _fc(name, hint=''):
        fc.calls.append((name, hint))
        return copy.deepcopy(fc.result)

    def _tv(name, hint=''):
        tv.calls.append((name, hint))
        return copy.deepcopy(tv.result)

    monkeypatch.setattr(es, '_firecrawl_search', _fc)
    monkeypatch.setattr(es, '_tavily_search', _tv)
    es._ACCOUNT_CACHE['obj'] = None
    es._breaker.update(streak=0, open_until=0.0)
    es.LLM_STATE.update(unavailable=False, consecutive=0)
    es._BUDGET['obj'] = es.SearchBudget(1)
    es.reset_search_counts()
    reset_probe_cache()
    yield SimpleNamespace(fc=fc, tv=tv, tmp=tmp_path, cache_path=str(tmp_path / 'cache.db'))
    reset_probe_cache()
    es._ACCOUNT_CACHE['obj'] = None
    es.LLM_STATE.update(unavailable=False, consecutive=0)


def _run(monkeypatch, client, llm, **kw):
    monkeypatch.setattr(es, 'get_supabase', lambda: client)
    monkeypatch.setattr(es, 'llm_json', llm)
    es.enrich_events(**kw)
    return client


def _typed_keys(payload):
    return set(payload) & set(TYPED_EVENT_COLUMNS)


# ── 1. Article OTHER + High → tombstone with zero searches ──────────────────
def test_article_other_high_confidence_tombstones_without_search(env, monkeypatch):
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'OTHER', 'industry': 'Robotics',
                                      'hq': 'Boston, MA', 'classification_confidence': 'High'},
                  grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert env.fc.calls == [] and env.tv.calls == []
    assert llm.count('search') == 0 and llm.count('grade') == 0
    pl = client.payload_for('ev1')
    assert pl['blocked_reason'].startswith('fit_gate:')
    assert 'subindustry OTHER' in pl['blocked_reason']
    assert pl['verify_state'] == 'not_fit' and pl['fit_verdict'] == 'fail'
    assert pl['account_key'] == 'zorblat robotics'
    assert pl['retry_after'] is None
    assert pl['companies_data'][0]['fit']['verdict'] == 'fail'
    assert pl['companies_data'][0]['classified_by'] == 'article'
    assert pl['fit']['verdict'] == 'fail'
    assert 'enriched_at' in pl and 'blocked_at' in pl


def test_article_other_rule_can_be_disabled(env, monkeypatch):
    monkeypatch.setattr(es, 'ARTICLE_OTHER_TOMBSTONE_MIN_CONFIDENCE', 'never')
    firm = {'zi_subindustry': 'OTHER', 'classification_confidence': 'High',
            'classified_by': 'article'}
    out, no_search = es._article_other_decision(firm)
    assert out['zi_subindustry'] is None and no_search is False
    # …but a structured SEC 'out' for this company still decides it.
    out, no_search = es._article_other_decision(firm, structured_out=True)
    assert out['zi_subindustry'] == 'OTHER' and no_search is True
    # and a cache-derived OTHER is already researched → final
    out, no_search = es._article_other_decision(dict(firm, classified_by='cache',
                                                     classification_confidence=None))
    assert no_search is True


# ── 2. Article OTHER + Medium → Stage B resolves it → verified ──────────────
def test_article_other_medium_confidence_searches_and_verifies(env, monkeypatch):
    env.fc.result = HIT
    llm = LLMStub(COMPANIES,
                  article={'zi_subindustry': 'OTHER', 'hq': 'Boston, MA',
                           'classification_confidence': 'Medium'},
                  search={'zi_subindustry': 'Banking', 'hq': 'Boston, MA', 'revenue': 'MM',
                          'size': '51-200', 'url': 'https://zorblat.example',
                          'classification_confidence': 'High'},
                  grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert len(env.fc.calls) >= 1            # Stage B fired (tier 1)
    assert env.fc.calls[0][0] == 'Zorblat Robotics Inc'
    pl = client.payload_for('ev1')
    assert 'blocked_at' not in pl and 'enriched_at' in pl
    assert pl['fit']['verdict'] == 'pass'
    assert pl['verify_state'] == 'verified' and pl['fit_verdict'] == 'pass'
    assert pl['zi_subindustry'] == 'Banking' and pl['hq_state'] == 'MA'
    assert pl['in_territory'] == 'in' and pl['vertical'] == 'in'
    assert pl['revenue_segment'] == 'MM'
    assert pl['classified_by'] == 'search' and pl['classification_confidence'] == 'High'
    assert pl['retry_after'] is None and 'enrich_attempts' not in pl
    assert pl['expires_at'].startswith('2026-10-31')   # published +60d (cfo_hire)
    assert pl['grade'] == 'B'
    co = pl['companies_data'][0]
    assert co['classified_by'] == 'search' and co['zi_subindustry'] == 'Banking'


# ── 3. Stage A complete from the account cache → no LLM, no search ──────────
def test_stage_a_complete_from_cache_skips_llm_and_search(env, monkeypatch):
    AccountCache(env.cache_path).set_firmographics('zorblat robotics', {
        'zi_subindustry': 'Banking', 'hq': 'Boston, MA', 'revenue': 'MM',
        'size': '51-200', 'url': 'https://zorblat.example',
        'classification_confidence': 'High'})
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'OTHER',
                                      'classification_confidence': 'High'},
                  grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert llm.count('article') == 0 and llm.count('search') == 0
    # the only searches allowed are the free probes (complexity), never the
    # firmographic lookup
    assert all(hint != 'robotics maker' for _, hint in env.fc.calls)
    pl = client.payload_for('ev1')
    assert pl['verify_state'] == 'verified'
    assert pl['classified_by'] == 'cache'
    assert pl['companies_data'][0]['revenue'] == 'MM'


# ── 4. Tier 3 with unknown vertical → staged, no search, attempts+1 ─────────
def test_tier3_unknown_vertical_is_staged_without_search(env, monkeypatch):
    ev = _event(event_type='funding', title='Zorblat Robotics Inc raises $500,000 seed',
                description='BOSTON, MA — Zorblat Robotics Inc raised $500,000.')
    llm = LLMStub([{'name': 'Zorblat Robotics Inc', 'role': 'Portfolio Company',
                    'descriptor': 'robotics maker'}],
                  article={'zi_subindustry': None, 'hq': 'Boston, MA',
                           'classification_confidence': 'Low'}, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([ev], TYPED_COLS), llm)
    assert env.fc.calls == [] and env.tv.calls == []
    assert llm.count('grade') == 0
    pl = client.payload_for('ev1')
    assert pl['fit']['verdict'] == 'staged'
    assert pl['verify_state'] == 'staged'
    assert pl['enrich_attempts'] == 1
    assert pl['retry_after'] is None
    assert 'enriched_at' in pl                      # not deferred → stamped
    assert pl['fit']['deferred_attempts'] == 0


# ── 5. Search deferred → staged, enriched_at withheld ───────────────────────
def test_deferred_search_withholds_enriched_at(env, monkeypatch):
    monkeypatch.setattr(es, '_scrape_rungs_available', lambda: False)
    ev = _event(event_type='expansion', title='Zorblat Robotics Inc opens new plant',
                description='Zorblat Robotics Inc opens a plant.')
    llm = LLMStub([{'name': 'Zorblat Robotics Inc', 'role': 'Primary', 'descriptor': ''}],
                  article={'zi_subindustry': None, 'hq': None,
                           'classification_confidence': None}, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([ev], TYPED_COLS), llm)
    assert env.fc.calls == []                        # throttled: nothing scraped
    pl = client.payload_for('ev1')
    assert pl['fit']['verdict'] == 'staged'
    assert pl['fit']['deferred_attempts'] == 1
    assert 'enriched_at' not in pl
    # review 2026-09-07: a throttled pass is not a research attempt — the
    # typed counter and the retry date are left exactly as they were.
    assert pl['verify_state'] == 'staged'
    assert 'enrich_attempts' not in pl and 'retry_after' not in pl
    assert es.SEARCH_COUNTS['throttled'] == 1


# ── 6. researched_ambiguous → retry ladder ──────────────────────────────────
def test_researched_ambiguous_first_attempt_retries_in_7_days(env, monkeypatch):
    monkeypatch.setattr(es, 'TAVILY_API_KEY', '')     # scrape-only: no paid rung
    monkeypatch.setattr(es, 'retry_after_for',
                        functools.partial(typed.retry_after_for, now=FIXED_NOW))
    env.fc.result = {}                                # search finds nothing
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'Banking', 'hq': None,
                                      'classification_confidence': 'High'},
                  grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    pl = client.payload_for('ev1')
    assert pl['fit']['verdict'] == 'unverified'
    assert pl['verify_state'] == 'researched_ambiguous'
    assert pl['enrich_attempts'] == 1
    assert pl['retry_after'] == RETRY_7D
    assert 'enriched_at' in pl
    assert pl['grade'] == 'B'
    # Firecrawl was tried (once per lookup, twice as HTTP attempts) and the
    # empty was negative-cached at the scrape rung.
    assert es.SEARCH_COUNTS['firecrawl'] >= 1
    assert es.SEARCH_COUNTS['firecrawl_attempts'] >= 2
    assert AccountCache(env.cache_path).should_skip('zorblat robotics', 'firmographic') is True
    assert AccountCache(env.cache_path).should_skip('zorblat robotics', 'firmographic',
                                                   want_paid=True) is False


def test_researched_ambiguous_third_attempt_clears_retry_after():
    ev = _event(enrich_attempts=2)
    fit = {'verdict': 'unverified', 'account_name': 'Zorblat Robotics Inc',
           'territory': 'unknown', 'revenue': 'unknown', 'vertical': 'in',
           'zi_subindustry': 'Banking', 'reasons': ['territory unverified']}
    enriched = [{'name': 'Zorblat Robotics Inc', 'role': 'Hiring Company',
                 'zi_subindustry': 'Banking', 'classified_by': 'article',
                 'classification_confidence': 'High'}]
    pl = es._final_typed(ev, set(TYPED_EVENT_COLUMNS), fit, {}, enriched, 2)
    assert pl['enrich_attempts'] == MAX_ENRICH_ATTEMPTS == 3
    assert pl['retry_after'] is None
    assert pl['verify_state'] == 'researched_ambiguous'
    # verified rows never carry attempts and get retry_after cleared
    pl = es._final_typed(ev, set(TYPED_EVENT_COLUMNS), dict(fit, verdict='pass'), {}, enriched, 2)
    assert 'enrich_attempts' not in pl and pl['retry_after'] is None
    assert pl['verify_state'] == 'verified'
    assert es._final_typed(ev, set(), fit, {}, enriched, 0) == {}


# ── 7. LLM unavailable ──────────────────────────────────────────────────────
def _fixed_llm_clock(monkeypatch):
    monkeypatch.setattr(es, 'llm_retry_after',
                        functools.partial(typed.llm_retry_after, now=FIXED_NOW))


def test_llm_unavailable_writes_only_retry_and_stops_after_three(env, monkeypatch):
    """Server down for real: the start-up canary passes, three events trip
    'unavailable', the re-run canary fails too → stop after three."""
    _fixed_llm_clock(monkeypatch)
    events = [_event(id=f'ev{i}', enrich_attempts=i) for i in range(1, 6)]
    base = LLMStub(COMPANIES, down={'extract'})
    canaries = []

    def llm(prompt, max_tokens=600):
        if base.kind(prompt) == 'canary':
            canaries.append(1)
            if len(canaries) > 1:             # the re-check: really down now
                es.LLM_STATE['unavailable'] = True
                return {}
        return base(prompt, max_tokens)
    client = _run(monkeypatch, FakeClient(events, TYPED_COLS), llm)
    assert [e for e, _ in client.updates] == ['ev1', 'ev2', 'ev3']   # stopped early
    for eid, pl in client.updates:
        assert set(pl) == {'enrich_attempts', 'retry_after'}
        assert pl['enrich_attempts'] == int(eid[2:]) + 1
        assert pl['retry_after'] == RETRY_4H
    assert base.count('extract') == 3 and len(canaries) == 2
    assert es.LLM_STATE['consecutive'] == 3


def test_llm_unavailable_continues_when_canary_answers(env, monkeypatch, caplog):
    """review 2026-09-07: three consecutive 'unavailable' events on a server
    that answers the canary are bad EVENTS, not an outage — the run goes
    on (they keep their attempts+1 / +4h push) instead of stranding the
    rest of the queue for a cycle."""
    _fixed_llm_clock(monkeypatch)
    events = [_event(id=f'ev{i}', enrich_attempts=i) for i in range(1, 6)]
    llm = LLMStub(COMPANIES, down={'extract'})        # canary always answers
    with caplog.at_level(logging.WARNING):
        client = _run(monkeypatch, FakeClient(events, TYPED_COLS), llm)
    assert [e for e, _ in client.updates] == ['ev1', 'ev2', 'ev3', 'ev4', 'ev5']
    for eid, pl in client.updates:
        assert set(pl) == {'enrich_attempts', 'retry_after'}
        assert pl['enrich_attempts'] == int(eid[2:]) + 1
        assert pl['retry_after'] == RETRY_4H
    assert 'LLM answered the canary — the events themselves are the problem' in caplog.text
    assert llm.count('canary') == 2          # start-up + the one re-check
    assert es.LLM_STATE['consecutive'] == 2  # streak restarted after the canary


def test_llm_unavailable_json_only_writes_nothing(env, monkeypatch):
    llm = LLMStub(COMPANIES, down={'extract'})
    client = _run(monkeypatch, FakeClient([_event(), _event(id='ev2')], BASE_COLS), llm)
    assert client.updates == []


def test_llm_canary_failure_aborts_before_any_event(env, monkeypatch):
    llm = LLMStub(COMPANIES, down={'canary'})
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert client.updates == [] and llm.calls == ['canary']


def test_llm_streak_resets_on_a_good_event(env, monkeypatch):
    """down, good, down, down: no stop — the streak counter resets."""
    seq = iter(['down', 'ok', 'down', 'down'])
    base = LLMStub(COMPANIES, article={'zi_subindustry': 'OTHER',
                                       'classification_confidence': 'High'})

    def llm(prompt, max_tokens=600):
        if base.kind(prompt) == 'extract':
            if next(seq) == 'down':
                es.LLM_STATE['unavailable'] = True
                return {}
        return base(prompt, max_tokens)
    events = [_event(id=f'ev{i}') for i in range(1, 5)]
    client = _run(monkeypatch, FakeClient(events, TYPED_COLS), llm)
    assert [e for e, _ in client.updates] == ['ev1', 'ev2', 'ev3', 'ev4']
    assert client.payload_for('ev2')['verify_state'] == 'not_fit'
    assert set(client.payload_for('ev4')) == {'enrich_attempts', 'retry_after'}


def test_llamacpp_transport_error_flags_unavailable(monkeypatch):
    es.LLM_STATE.update(unavailable=False, consecutive=0)

    def boom(*a, **k):
        raise es.requests.exceptions.ConnectionError('refused')
    monkeypatch.setattr(es.requests, 'post', boom)
    assert es._llamacpp_json('x', 10) == {}
    assert es.LLM_STATE['unavailable'] is True

    es.LLM_STATE['unavailable'] = False
    monkeypatch.setattr(es.requests, 'post', lambda *a, **k: SimpleNamespace(
        status_code=503, raise_for_status=lambda: None, json=lambda: {}))
    assert es._llamacpp_json('x', 10) == {}
    assert es.LLM_STATE['unavailable'] is True

    # a 200 with an unparseable body is a bad answer, not an outage
    es.LLM_STATE['unavailable'] = False
    monkeypatch.setattr(es.requests, 'post', lambda *a, **k: SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {'choices': [{'message': {'content': 'not json at all'}}]}))
    assert es._llamacpp_json('x', 10) == {}
    assert es.LLM_STATE['unavailable'] is False


# ── 8. Negative cache ───────────────────────────────────────────────────────
def test_negative_cache_skips_scrape_but_not_paid(env):
    env.fc.result = {}
    es._BUDGET['obj'] = es.SearchBudget(2)             # scrape-only
    assert es.tavily_search('Ghost Holdings LLC', 'x') == {}
    assert len(env.fc.calls) == 2                       # one lookup = two attempts
    assert es.SEARCH_COUNTS == dict(es.SEARCH_COUNTS, firecrawl=1, firecrawl_attempts=2)

    es.reset_search_counts()
    es._BUDGET['obj'] = es.SearchBudget(2)
    assert es.tavily_search('Ghost Holdings LLC', 'y') == {}   # different hint, same account
    assert len(env.fc.calls) == 2                       # no new Firecrawl call
    assert es.SEARCH_COUNTS['negative_cache'] == 1
    assert es.SEARCH_COUNTS['firecrawl'] == 0
    assert es._BUDGET['obj'].used == 0                  # a known empty costs no budget

    es.reset_search_counts()
    es._BUDGET['obj'] = es.SearchBudget(1)             # paid allowed
    env.tv.result = HIT
    res = es.tavily_search('Ghost Holdings LLC', 'z')
    assert res.get('results')
    assert len(env.fc.calls) == 4 and len(env.tv.calls) == 1
    assert es.SEARCH_COUNTS['negative_cache'] == 0
    # success clears the negative entry and caches the raw results
    cache = AccountCache(env.cache_path)
    assert cache.should_skip('ghost holdings', 'firmographic') is False
    assert cache.get_search('ghost holdings', 'firmographic') == HIT

    es.reset_search_counts()
    es._BUDGET['obj'] = es.SearchBudget(1)
    assert es.tavily_search('Ghost Holdings LLC') == HIT
    assert es.SEARCH_COUNTS['cache'] == 1 and len(env.fc.calls) == 4


def test_paid_empty_records_paid_rung(env):
    env.fc.result = {}
    env.tv.result = {}
    es._BUDGET['obj'] = es.SearchBudget(1)
    assert es.tavily_search('Vapor Ventures Inc', 'x') == {}
    assert len(env.tv.calls) == 1
    cache = AccountCache(env.cache_path)
    assert cache.should_skip('vapor ventures', 'firmographic', want_paid=True) is True


def test_probe_kinds_are_cached_separately(env):
    env.fc.result = HIT
    es._BUDGET['obj'] = es.SearchBudget(1)
    assert es.probe_aum('Zorblat Capital') != ''
    cache = AccountCache(env.cache_path)
    assert cache.get_search('zorblat capital', 'aum') == HIT
    assert cache.get_search('zorblat capital', 'firmographic') is None
    assert cache.get_search('zorblat capital', 'complexity') is None


# ── 9. Typed columns absent → JSON-only ─────────────────────────────────────
def test_typed_columns_absent_keeps_json_only_payloads(env, monkeypatch):
    env.fc.result = HIT
    llm = LLMStub(COMPANIES,
                  article={'zi_subindustry': 'OTHER', 'hq': 'Boston, MA',
                           'classification_confidence': 'Medium'},
                  search={'zi_subindustry': 'Banking', 'hq': 'Boston, MA', 'revenue': 'MM',
                          'size': '51-200', 'url': 'https://zorblat.example',
                          'classification_confidence': 'High'},
                  grade=GRADE_B)
    events = [_event(), _event(id='ev2', title='Zorblat Robotics Inc names Bob to board',
                               event_type='executive_hire',
                               description='Bob joins the board of directors.')]
    client = _run(monkeypatch, FakeClient(events, BASE_COLS), llm)
    for eid, pl in client.updates:
        assert _typed_keys(pl) == set(), (eid, pl)
    assert client.payload_for('ev1')['fit']['verdict'] == 'pass'
    assert 'enriched_at' in client.payload_for('ev1')
    assert client.payload_for('ev2')['blocked_reason'].startswith('board_change_only')
    # the select never asked for the typed columns / retry filter
    assert all('retry_after' not in str(f) for f in client.selects)


def test_check_columns_reports_typed_set(env):
    assert es.check_columns(FakeClient([], TYPED_COLS))['typed'] == set(TYPED_EVENT_COLUMNS)
    reset_probe_cache()
    assert es.check_columns(FakeClient([], BASE_COLS))['typed'] == set()


def test_selection_honours_retry_after_and_attempts(env, monkeypatch):
    llm = LLMStub([])
    client = FakeClient([], TYPED_COLS)
    _run(monkeypatch, client, llm)
    default = client.selects[-1]
    assert ('is', 'enriched_at', 'null') in default
    assert any(op == 'or' and f.startswith('retry_after.is.null,retry_after.lte.')
               for op, *rest in default for f in rest)
    reset_probe_cache()          # probe_columns memoizes per process
    client = FakeClient([], TYPED_COLS)
    _run(monkeypatch, client, llm, re_enrich=True, reverify_unverified=True)
    rev = client.selects[-1]
    assert ('in', 'verify_state', ['staged', 'researched_ambiguous']) in rev
    assert ('lt', 'enrich_attempts', MAX_ENRICH_ATTEMPTS) in rev
    assert ('is', 'enriched_at', 'null') not in rev
    # JSON-only fallback
    reset_probe_cache()
    client = FakeClient([], BASE_COLS)
    _run(monkeypatch, client, llm, re_enrich=True, reverify_unverified=True)
    assert ('in', 'fit->>verdict', ['unverified', 'staged']) in client.selects[-1]


# ── 10. Run lock ────────────────────────────────────────────────────────────
_HOLDER = '''
import sys, os
sys.path.insert(0, %r)
from src.pipeline.runlock import RunLock
lock = RunLock(sys.argv[1])
assert lock.acquire(), "child could not acquire"
print(os.getpid(), flush=True)
sys.stdin.readline()
lock.release()
'''


def test_run_lock_second_process_declines(tmp_path):
    path = str(tmp_path / 'enrichment.lock')
    proc = subprocess.Popen([sys.executable, '-c', _HOLDER % REPO, path],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pid = int(proc.stdout.readline().strip())
    try:
        assert es._acquire_run_lock(path) is None          # → __main__ exits 0
        assert es.RunLock(path).holder_pid() == pid
    finally:
        proc.stdin.write('\n')
        proc.stdin.flush()
        proc.wait(timeout=10)
    lock = es._acquire_run_lock(path)
    assert lock is not None and lock.holder_pid() == os.getpid()
    lock.release()


# ── Pagination ──────────────────────────────────────────────────────────────
class _PagedQuery:
    def __init__(self, n):
        self.rows = [{'id': i} for i in range(n)]
        self.ranges = []

    def order(self, col, desc=False):
        return self

    def range(self, a, b):
        self.ranges.append((a, b))
        self._r = (a, b)
        return self

    def execute(self):
        a, b = self._r
        return SimpleNamespace(data=self.rows[a:b + 1])


def test_fetch_all_pages_until_short_page_and_caps():
    q = _PagedQuery(2500)
    rows = es._fetch_all(q, page=1000)
    assert len(rows) == 2500 and q.ranges == [(0, 999), (1000, 1999), (2000, 2999)]
    q = _PagedQuery(7000)
    assert len(es._fetch_all(q, page=1000, max_rows=5000)) == 5000
    assert q.ranges[-1] == (4000, 4999)
    assert es._fetch_all(_PagedQuery(0)) == []


def test_strip_range_params_handles_real_postgrest_builder():
    """postgrest-py 2.x range() ADDS params; a second page must not stack."""
    postgrest = pytest.importorskip('postgrest')
    c = postgrest.SyncPostgrestClient('http://localhost:1', headers={})
    q = c.from_('events').select('id').order('discovered_at')
    q.range(0, 999)
    es._strip_range_params(q)
    q.range(1000, 1999)
    params = str(q.request.params)
    assert params.count('offset=') == 1 and 'offset=1000' in params


# ── Stage merge rules ───────────────────────────────────────────────────────
def test_merge_search_rules():
    seeds = {'hq': 'MA'}
    a = {'zi_subindustry': 'Banking', 'classification_confidence': 'High',
         'classified_by': 'article', 'hq': 'MA', 'revenue': None, 'size': None, 'url': None}
    # Low search does NOT override a High article classification
    m = es._merge_search(a, {'zi_subindustry': 'Insurance', 'classification_confidence': 'Low',
                             'size': '51-200', 'hq': 'Hartford, CT'}, seeds)
    assert m['zi_subindustry'] == 'Banking' and m['size'] == '51-200'
    assert m['hq'] == 'MA' and m['classified_by'] == 'article'
    # Medium search DOES override (it read the article + results — more evidence)
    m = es._merge_search(a, {'zi_subindustry': 'Insurance', 'classification_confidence': 'Medium'}, seeds)
    assert m['zi_subindustry'] == 'Insurance' and m['classified_by'] == 'search'
    # High search overrides; hq from search when not seeded
    m = es._merge_search(a, {'zi_subindustry': 'Insurance', 'classification_confidence': 'High',
                             'hq': 'Hartford, CT'}, {})
    assert m['zi_subindustry'] == 'Insurance' and m['hq'] == 'Hartford, CT'
    assert m['classified_by'] == 'search' and m['classification_confidence'] == 'High'
    # article Low → any search classification wins
    m = es._merge_search(dict(a, classification_confidence='Low'),
                         {'zi_subindustry': 'Insurance', 'classification_confidence': 'Low'}, seeds)
    assert m['zi_subindustry'] == 'Insurance'
    # deferred propagates
    assert es._merge_search(a, {'deferred': True}, seeds)['deferred'] is True


def test_stage_b_skips_when_nothing_left_to_learn(env, monkeypatch):
    called = []
    monkeypatch.setattr(es, 'enrich_one_company', lambda *a, **k: called.append(a) or {})
    full = {f: 'x' for f in es._STAGE_B_NEEDS}
    es._BUDGET['obj'] = es.SearchBudget(1)
    es._stage_b_company({'name': 'A'}, full, 'ctx', tier=1)
    es._stage_b_company({'name': 'A'}, {'hq': None}, 'ctx', tier=3)
    es._BUDGET['obj'] = es.SearchBudget(3)
    es._stage_b_company({'name': 'A'}, {'hq': None}, 'ctx', tier=2)
    assert called == []
    es._BUDGET['obj'] = es.SearchBudget(1)
    es._stage_b_company({'name': 'A'}, {'hq': None}, 'ctx', tier=1)
    assert len(called) == 1


def test_enrich_one_company_reports_confidence_and_source(env, monkeypatch):
    monkeypatch.setattr(es, 'llm_json', lambda p, max_tokens=600: {
        'zi_subindustry': 'Banking', 'classification_confidence': 'medium', 'hq': 'Boston, MA'})
    out = es.enrich_one_company('Zorblat Bank', 'bank', article_context='ctx', no_search=True)
    assert out['classification_confidence'] == 'Medium' and out['classified_by'] == 'article'
    assert env.fc.calls == []
    monkeypatch.setattr(es, 'llm_json', lambda p, max_tokens=600: {
        'zi_subindustry': 'Banking', 'classification_confidence': 'certain'})
    out = es.enrich_one_company('Zorblat Bank', 'bank', article_context='ctx', no_search=True)
    assert out['classification_confidence'] is None
    # require_search: an empty search returns without an LLM call
    calls = []
    monkeypatch.setattr(es, 'llm_json', lambda p, max_tokens=600: calls.append(p) or {})
    env.fc.result = {}
    es._BUDGET['obj'] = es.SearchBudget(2)
    out = es.enrich_one_company('Zorblat Bank', 'bank', article_context='ctx',
                                no_search=False, require_search=True)
    assert calls == [] and out['classified_by'] == 'search' and out['zi_subindustry'] is None


def test_structured_seeds_apply_before_llm(env, monkeypatch):
    ev = _event(source_url='https://www.sec.gov/Archives/edgar/data/1/formd.htm',
                title='Form D: Zorblat Robotics Inc',
                description='Zorblat Robotics Inc (MA) filed a Form D. Form D industry '
                            'group: Commercial Banking. Declared revenue: '
                            '$5,000,001 - $25,000,000. Total offering: $12,000,000.')
    sv = es._structured_verdict(ev)
    assert sv['verdict'] == 'unknown' and sv['revenue_segment'] == 'LMM'
    seeds = es._structured_seeds(ev, 'Zorblat Robotics Inc', sv)
    assert seeds['hq'] == 'MA'
    assert seeds['revenue'] == 'LMM'
    assert seeds['revenue_source'] == 'SEC Form D declared revenue range'
    assert es._structured_seeds(ev, 'Someone Else', sv) == {}
    llm = LLMStub([], article={'zi_subindustry': 'Banking', 'hq': 'Austin, TX',
                               'revenue': 'Enterprise', 'classification_confidence': 'High'})
    monkeypatch.setattr(es, 'llm_json', llm)
    firm = es._stage_a_company(ev, {'name': 'Zorblat Robotics Inc', 'role': 'Primary'},
                               sv, 'ctx', AccountCache(env.cache_path))
    assert firm['hq'] == 'MA'                      # seed wins over the article
    assert firm['revenue'] == seeds['revenue']
    assert firm['zi_subindustry'] == 'Banking' and firm['classified_by'] == 'article'


# ── regrade --event-type ────────────────────────────────────────────────────
def test_regrade_only_event_type_filter(env, monkeypatch):
    client = FakeClient([], TYPED_COLS)
    monkeypatch.setattr(es, 'get_supabase', lambda: client)
    monkeypatch.setattr(es, 'llm_json', LLMStub([]))
    es.regrade_only_events(limit=5, dry_run=True, event_type='finance_seat_open')
    assert ('eq', 'event_type', 'finance_seat_open') in client.selects[-1]
    reset_probe_cache()
    client = FakeClient([], TYPED_COLS)
    monkeypatch.setattr(es, 'get_supabase', lambda: client)
    es.regrade_only_events(limit=5, dry_run=True)
    assert not any(op == 'eq' and c == 'event_type' for op, c, *_ in client.selects[-1])


# ── Tombstone typed half ────────────────────────────────────────────────────
def test_tombstone_typed_and_soft_delete_merge(env):
    ev = _event()
    typed = es._tombstone_typed(ev, set(TYPED_EVENT_COLUMNS))
    assert typed['verify_state'] == 'not_fit' and typed['fit_verdict'] == 'fail'
    assert typed['account_key'] == 'zorblat robotics'
    assert typed['expires_at'].startswith('2026-10-31')
    assert typed['retry_after'] is None
    assert es._tombstone_typed(ev, set()) == {}
    client = FakeClient([], TYPED_COLS)
    es._soft_delete(client, 'ev1', 'fit_gate: x', extra={'fit': None, 'companies_data': [1]},
                    typed=typed)
    pl = client.payload_for('ev1')
    assert pl['companies_data'] == [1] and 'fit' not in pl
    assert pl['verify_state'] == 'not_fit' and pl['retry_after'] is None


# ═══════════════════════════════════════════════════════════════════════════
# Review 2026-09-07 — adversarial-review fixes, each with its reproduction
# ═══════════════════════════════════════════════════════════════════════════
_EXPANSION = dict(event_type='expansion', title='Zorblat Robotics Inc opens new plant',
                  description='Zorblat Robotics Inc opens a plant.')
_BLANK_ARTICLE = {'zi_subindustry': None, 'hq': None, 'classification_confidence': None}
_PRIMARY = [{'name': 'Zorblat Robotics Inc', 'role': 'Primary', 'descriptor': ''}]


# ── 1. Deferred passes never burn enrich_attempts ───────────────────────────
def test_three_deferred_runs_leave_enrich_attempts_unchanged(env, monkeypatch):
    """A ≥12h search outage (three 4h cycles) must not walk a staged row to
    MAX_ENRICH_ATTEMPTS: only fit.deferred_attempts counts deferrals."""
    monkeypatch.setattr(es, '_scrape_rungs_available', lambda: False)
    llm = LLMStub(_PRIMARY, article=_BLANK_ARTICLE, grade=GRADE_B)
    ev = _event(enrich_attempts=1, verify_state='staged', retry_after=None, **_EXPANSION)
    for n in (1, 2, 3):
        client = _run(monkeypatch, FakeClient([copy.deepcopy(ev)], TYPED_COLS), llm)
        pl = client.payload_for('ev1')
        assert pl['verify_state'] == 'staged' and pl['fit']['verdict'] == 'staged'
        assert 'enrich_attempts' not in pl and 'retry_after' not in pl, (n, pl)
        assert pl['fit']['deferred_attempts'] == n
        assert ('enriched_at' in pl) == (n == 3)     # existing rule: 3rd deferral stamps
        ev['fit'] = pl['fit']                         # what the next cycle reads back
    assert ev['enrich_attempts'] == 1                 # never touched → still < MAX


def test_final_typed_deferred_leaves_attempts_and_retry_alone():
    ev = _event(enrich_attempts=1)
    fit = {'verdict': 'staged', 'account_name': 'Zorblat Robotics Inc',
           'territory': 'unknown', 'revenue': 'unknown', 'vertical': 'unknown',
           'zi_subindustry': None, 'reasons': ['vertical unverified']}
    enriched = [{'name': 'Zorblat Robotics Inc', 'role': 'Hiring Company'}]
    pl = es._final_typed(ev, set(TYPED_EVENT_COLUMNS), fit, {}, enriched, 1, deferred=True)
    assert pl['verify_state'] == 'staged'
    assert 'enrich_attempts' not in pl and 'retry_after' not in pl
    pl = es._final_typed(ev, set(TYPED_EVENT_COLUMNS), fit, {}, enriched, 1, deferred=False)
    assert pl['enrich_attempts'] == 2 and pl['retry_after'] is None   # staged: no ladder
    # a verified row still gets retry_after CLEARED even on a deferred pass
    pl = es._final_typed(ev, set(TYPED_EVENT_COLUMNS), dict(fit, verdict='pass'), {},
                         enriched, 1, deferred=True)
    assert pl['retry_after'] is None and 'enrich_attempts' not in pl


# ── 2. Run cache carries the deferred flag to the second event ──────────────
def test_run_cache_carries_deferred_flag_to_second_event(env, monkeypatch):
    monkeypatch.setattr(es, '_scrape_rungs_available', lambda: False)
    llm = LLMStub(_PRIMARY, article=_BLANK_ARTICLE, grade=GRADE_B)
    events = [_event(id='ev1', **_EXPANSION), _event(id='ev2', **_EXPANSION)]
    client = _run(monkeypatch, FakeClient(events, TYPED_COLS), llm)
    for eid in ('ev1', 'ev2'):
        pl = client.payload_for(eid)
        assert pl['fit']['verdict'] == 'staged' and pl['fit']['deferred_attempts'] == 1
        assert 'enriched_at' not in pl, eid
        assert 'enrich_attempts' not in pl
    assert llm.count('article') == 1          # Stage A ran once, reused for ev2
    assert es.SEARCH_COUNTS['throttled'] == 1  # …and so did the (deferred) lookup


# ── 3. LLM outage semantics ─────────────────────────────────────────────────
def test_llamacpp_read_timeout_is_not_an_outage(monkeypatch):
    es.LLM_STATE.update(unavailable=False, consecutive=0)
    for exc in (es.requests.exceptions.ReadTimeout('read timed out'),
                es.requests.exceptions.Timeout('timed out')):
        def slow(*a, _exc=exc, **k):
            raise _exc
        monkeypatch.setattr(es.requests, 'post', slow)
        assert es._llamacpp_json('x', 10) == {}
        assert es.LLM_STATE['unavailable'] is False      # slow, not down

    # ConnectTimeout is a ConnectionError subclass in requests → outage
    def refused(*a, **k):
        raise es.requests.exceptions.ConnectTimeout('connect timed out')
    monkeypatch.setattr(es.requests, 'post', refused)
    assert es._llamacpp_json('x', 10) == {}
    assert es.LLM_STATE['unavailable'] is True
    es.LLM_STATE.update(unavailable=False, consecutive=0)


# ── 4. Pre-search early exit judges researched facts only ───────────────────
SEARCH_BOSTON = {'zi_subindustry': 'Banking', 'hq': 'Boston, MA', 'revenue': 'MM',
                 'industry': 'Commercial Banking', 'size': '51-200',
                 'url': 'https://zorblat.example', 'classification_confidence': 'High'}


def test_article_only_hq_out_of_territory_is_not_tombstoned_pre_search(env, monkeypatch):
    """A dateline 'London, UK' on a tier-1 event used to fail territory with
    zero searches. Now Stage B runs and the search's hq decides."""
    env.fc.result = HIT
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'Banking', 'hq': 'London, UK',
                                      'classification_confidence': 'High'},
                  search=SEARCH_BOSTON, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert env.fc.calls and env.fc.calls[0][0] == 'Zorblat Robotics Inc'   # Stage B ran
    pl = client.payload_for('ev1')
    assert 'blocked_at' not in pl and pl['verify_state'] == 'verified'
    assert pl['hq_state'] == 'MA' and pl['in_territory'] == 'in'
    co = pl['companies_data'][0]
    assert co['hq'] == 'Boston, MA' and co['field_sources']['hq'] == 'search'


def test_article_only_hq_still_searched_when_search_is_empty(env, monkeypatch):
    """Same dateline, nothing found: the search must still have been TRIED
    (Phase 1 always searched). The article hq may then stand for the
    post-search gates — accepted Phase 1 behaviour, not asserted here."""
    monkeypatch.setattr(es, 'TAVILY_API_KEY', '')
    env.fc.result = {}
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'Banking', 'hq': 'London, UK',
                                      'classification_confidence': 'High'}, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert len(env.fc.calls) >= 1
    pl = client.payload_for('ev1')
    assert pl['companies_data'][0]['field_sources'] == {'zi_subindustry': 'article',
                                                        'hq': 'article'}


def test_article_other_high_short_description_goes_to_stage_b(env, monkeypatch):
    env.fc.result = HIT
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'OTHER', 'hq': 'Boston, MA',
                                      'classification_confidence': 'High'},
                  search=SEARCH_BOSTON, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event(description=SHORT_DESC)], TYPED_COLS), llm)
    assert len(env.fc.calls) >= 1
    pl = client.payload_for('ev1')
    assert 'blocked_at' not in pl and pl['verify_state'] == 'verified'
    assert pl['zi_subindustry'] == 'Banking'
    # the rule itself: length is the gate, structured 'out' ignores it
    firm = {'zi_subindustry': 'OTHER', 'classification_confidence': 'High',
            'classified_by': 'article'}
    assert es._article_other_decision(firm, article_chars=100)[1] is False
    assert es._article_other_decision(firm, article_chars=151)[1] is True
    assert es._article_other_decision(firm)[1] is False                 # unknown length
    assert es._article_other_decision(firm, structured_out=True, article_chars=10)[1] is True


def test_industry_blocklist_pre_search_needs_high_confidence(env, monkeypatch):
    env.fc.result = HIT
    article = {'zi_subindustry': None, 'industry': 'Gold Mining', 'hq': 'Boston, MA',
               'classification_confidence': 'Medium'}
    llm = LLMStub(COMPANIES, article=article, search=SEARCH_BOSTON, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert len(env.fc.calls) >= 1                       # Medium → researched first
    pl = client.payload_for('ev1')
    # the search's industry supersedes the article guess; had the search
    # found no industry, the post-search block would still apply (Phase 1)
    assert 'blocked_at' not in pl and pl['verify_state'] == 'verified'
    assert pl['companies_data'][0]['industry'] == 'Commercial Banking'

    # Fresh account cache for the High case — the run above researched the
    # account and cached 'Commercial Banking', which would rightly pre-empt
    # the article guess on a second look at the same company.
    env.fc.calls.clear()
    reset_probe_cache()
    monkeypatch.setattr(es, 'CACHE_DB_PATH', str(env.tmp / 'cache2.db'))
    es._ACCOUNT_CACHE['obj'] = None
    llm = LLMStub(COMPANIES, article=dict(article, classification_confidence='High'),
                  search=SEARCH_BOSTON, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert env.fc.calls == []                           # High → decided free
    pl = client.payload_for('ev1')
    assert pl['blocked_reason'].startswith('industry: Gold Mining')
    assert pl['verify_state'] == 'not_fit'


def test_article_only_revenue_enterprise_is_not_tombstoned_pre_search(env, monkeypatch):
    env.fc.result = HIT
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'Banking', 'hq': 'Boston, MA',
                                      'revenue': 'Enterprise',
                                      'classification_confidence': 'High'},
                  search=SEARCH_BOSTON, grade=GRADE_B)
    client = _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    assert len(env.fc.calls) >= 1
    pl = client.payload_for('ev1')
    assert 'blocked_at' not in pl and pl['revenue_segment'] == 'MM'    # search supersedes
    assert pl['companies_data'][0]['field_sources']['revenue'] == 'search'


def test_pre_search_view_blanks_article_guesses():
    firm = {'hq': 'London, UK', 'revenue': 'Enterprise', 'revenue_source': None,
            'industry': 'Gold Mining', 'zi_subindustry': 'Banking',
            'classification_confidence': 'Medium',
            '_sources': {'hq': 'article', 'revenue': 'article', 'industry': 'article',
                         'zi_subindustry': 'article'}}
    v = es._pre_search_view(firm)
    assert v['hq'] is None and v['revenue'] is None and v['industry'] is None
    assert v['zi_subindustry'] == 'Banking'
    assert firm['hq'] == 'London, UK'                    # original untouched
    assert es._pre_search_view(dict(firm, classification_confidence='High'))['industry'] \
        == 'Gold Mining'
    v = es._pre_search_view(dict(firm, _sources={'hq': 'seed', 'revenue': 'cache',
                                                 'industry': 'search'}))
    assert (v['hq'], v['revenue'], v['industry']) == ('London, UK', 'Enterprise', 'Gold Mining')


def test_account_cache_never_receives_article_only_hq(env, monkeypatch):
    monkeypatch.setattr(es, 'TAVILY_API_KEY', '')
    env.fc.result = {}                                   # search finds nothing
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'Banking', 'hq': 'Boston, MA',
                                      'revenue': 'MM', 'industry': 'Banking',
                                      'classification_confidence': 'High'},
                  grade=GRADE_B)
    _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    stored = AccountCache(env.cache_path).get_firmographics('zorblat robotics')
    assert stored == {'zi_subindustry': 'Banking', 'classification_confidence': 'High'}

    # …while a search-derived hq / revenue / size / url IS remembered. The
    # empty above negative-cached the account at the SCRAPE rung; a paid-
    # eligible lookup (key back) gets through it, and Firecrawl now hits.
    reset_probe_cache()
    monkeypatch.setattr(es, 'TAVILY_API_KEY', 'test-key-never-used')
    env.fc.result = HIT
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'Banking', 'hq': 'London, UK',
                                      'classification_confidence': 'High'},
                  search=SEARCH_BOSTON, grade=GRADE_B)
    _run(monkeypatch, FakeClient([_event(id='ev2')], TYPED_COLS), llm)
    stored = AccountCache(env.cache_path).get_firmographics('zorblat robotics')
    assert stored['hq'] == 'Boston, MA' and stored['revenue'] == 'MM'
    assert stored['size'] == '51-200' and stored['url'] == 'https://zorblat.example'


def test_remember_firmographics_provenance_gate(env):
    cache = AccountCache(env.cache_path)
    article = {'hq': 'London, UK', 'revenue': 'Enterprise', 'revenue_source': None,
               'industry': 'Gold Mining', 'zi_subindustry': 'Banking',
               'url': 'https://guess.example', 'size': '1-50', 'linkedin': None,
               'classification_confidence': 'Medium',
               '_sources': {f: 'article' for f in es._FIRM_FIELDS}}
    es._remember_firmographics(cache, 'Zorblat Robotics Inc', article)
    assert cache.get_firmographics('zorblat robotics') is None
    es._remember_firmographics(cache, 'Zorblat Robotics Inc',
                               dict(article, classification_confidence='High'))
    assert cache.get_firmographics('zorblat robotics') == {
        'zi_subindustry': 'Banking', 'classification_confidence': 'High'}
    # seed hq + search-derived everything else → all kept; cache values not re-stamped
    researched = dict(article, hq='MA', classification_confidence='Medium',
                      _sources={'hq': 'seed', 'revenue': 'search', 'industry': 'search',
                                'zi_subindustry': 'search', 'url': 'search',
                                'size': 'cache'})
    es._remember_firmographics(cache, 'Other Corp', researched)
    stored = cache.get_firmographics('other')
    assert stored == {'hq': 'MA', 'revenue': 'Enterprise', 'industry': 'Gold Mining',
                      'zi_subindustry': 'Banking', 'url': 'https://guess.example',
                      'classification_confidence': 'Medium'}
    # no provenance at all → nothing persisted
    es._remember_firmographics(cache, 'Third Corp', dict(article, _sources=None))
    assert cache.get_firmographics('third') is None


def test_stage_b_article_only_fields_are_still_needs(env, monkeypatch):
    called = []
    monkeypatch.setattr(es, 'enrich_one_company', lambda *a, **k: called.append(a) or {})
    full = {f: 'x' for f in es._STAGE_B_NEEDS}
    full['classification_confidence'] = 'High'

    def run(**over):
        es._BUDGET['obj'] = es.SearchBudget(1)
        es._stage_b_company({'name': 'A'}, dict(full, **over), 'ctx', tier=1)
        return len(called)
    assert run(_sources={'hq': 'article'}) == 1            # article hq → search
    assert run(_sources={'revenue': 'article'}) == 2       # article revenue → search
    assert run(_sources={'zi_subindustry': 'article'}) == 2   # article zi at High settles
    assert run(_sources={'zi_subindustry': 'article'}, classification_confidence='Medium') == 3
    assert run(_sources={'hq': 'cache', 'revenue': 'seed'}) == 3   # researched → settled


def test_merge_search_provenance_and_article_override():
    a = {'zi_subindustry': 'Banking', 'classification_confidence': 'High',
         'classified_by': 'article', 'hq': 'London, UK', 'revenue': 'Enterprise',
         'revenue_source': None, 'industry': 'Robotics', 'size': None, 'url': None,
         '_sources': {'zi_subindustry': 'article', 'hq': 'article',
                      'revenue': 'article', 'industry': 'article'}}
    got = {'zi_subindustry': 'Banking', 'classification_confidence': 'Medium',
           'hq': 'Boston, MA', 'revenue': 'MM', 'revenue_source': 'https://cb.example',
           'industry': 'Commercial Banking', 'size': '51-200'}
    m = es._merge_search(a, got, {})
    assert (m['hq'], m['revenue'], m['industry']) == ('Boston, MA', 'MM', 'Commercial Banking')
    assert m['revenue_source'] == 'https://cb.example'
    assert m['_sources'] == {'zi_subindustry': 'search', 'hq': 'search', 'revenue': 'search',
                             'revenue_source': 'search', 'industry': 'search', 'size': 'search'}
    assert m['classified_by'] == 'search' and m['classification_confidence'] == 'High'
    # seeded hq / revenue are never overridden by a search
    seeded = dict(a, hq='MA', revenue='LMM',
                  _sources=dict(a['_sources'], hq='seed', revenue='seed'))
    m = es._merge_search(seeded, got, {'hq': 'MA'})
    assert m['hq'] == 'MA' and m['revenue'] == 'LMM'
    assert m['_sources']['hq'] == 'seed' and m['_sources']['revenue'] == 'seed'


def test_merge_search_medium_search_overrides_article_high_name_guess():
    """Live 2026-09-07 (SharonAI Holdings): the article pass guessed
    'Holding Companies & Conglomerates' from the NAME at High confidence;
    the search identified an AI-infrastructure company (OTHER, Medium).
    The search holds more evidence, so it wins — only a Low-confidence
    search defers to an article High."""
    a = {'zi_subindustry': 'Holding Companies & Conglomerates',
         'classification_confidence': 'High', 'classified_by': 'article',
         'hq': None, 'revenue': None, 'industry': 'Holding Company',
         '_sources': {'zi_subindustry': 'article', 'industry': 'article'}}
    got = {'zi_subindustry': 'OTHER', 'classification_confidence': 'Medium',
           'industry': 'AI Infrastructure', 'url': 'https://sharonai.example'}
    m = es._merge_search(a, got, {})
    assert m['zi_subindustry'] == 'OTHER'
    assert m['classified_by'] == 'search' and m['classification_confidence'] == 'Medium'
    assert m['_sources']['zi_subindustry'] == 'search'
    # a Low-confidence search does NOT override an article High
    low = dict(got, classification_confidence='Low')
    m = es._merge_search(a, low, {})
    assert m['zi_subindustry'] == 'Holding Companies & Conglomerates'
    assert m['classification_confidence'] == 'High'


# ── 5. A backend that did not answer is deferred, never 'known empty' ───────
def test_firecrawl_transport_failure_is_deferred_not_empty(env, monkeypatch):
    monkeypatch.setattr(es, '_firecrawl_search', _REAL_FIRECRAWL)

    def boom(*a, **k):
        raise es.requests.exceptions.ConnectionError('refused')
    monkeypatch.setattr(es.requests, 'post', boom)
    es._BUDGET['obj'] = es.SearchBudget(1)
    assert es.tavily_search('Ghost Holdings LLC', 'x') == {'deferred': True}
    assert es._BUDGET['obj'].deferred is True
    cache = AccountCache(env.cache_path)
    assert cache.stats()['negative_cache'] == 0
    assert cache.should_skip('ghost holdings', 'firmographic') is False
    assert env.tv.calls == []                              # never escalates to paid
    assert es._breaker['streak'] == 1                      # counts toward the breaker
    sc = es.SEARCH_COUNTS
    assert (sc['lookups'], sc['firecrawl'], sc['firecrawl_attempts'],
            sc['transport_failed']) == (1, 1, 2, 1)


def test_firecrawl_answer_contract(monkeypatch):
    def resp(status=200, body=None, raise_exc=None):
        def _raise():
            if raise_exc:
                raise raise_exc
        return SimpleNamespace(status_code=status, raise_for_status=_raise,
                               json=lambda: body)
    monkeypatch.setattr(es.requests, 'post', lambda *a, **k: resp(body={'success': False}))
    assert es._firecrawl_search('X') is None
    monkeypatch.setattr(es.requests, 'post', lambda *a, **k: resp(
        status=502, raise_exc=es.requests.exceptions.HTTPError('502')))
    assert es._firecrawl_search('X') is None
    monkeypatch.setattr(es.requests, 'post', lambda *a, **k: resp(body={'success': True, 'data': []}))
    assert es._firecrawl_search('X') == {'answer': '', 'results': []}   # genuine empty
    monkeypatch.setattr(es.requests, 'post', lambda *a, **k: resp(body={
        'success': True, 'data': [{'title': 'T', 'url': 'https://t', 'description': 'd'}]}))
    assert es._firecrawl_search('X')['results'] == [{'title': 'T', 'url': 'https://t',
                                                     'content': 'd'}]
    # Tavily: no key / transport error → None, never {}
    monkeypatch.setattr(es, 'TAVILY_API_KEY', '')
    assert es._tavily_search('X') is None
    monkeypatch.setattr(es, 'TAVILY_API_KEY', 'k')
    monkeypatch.setattr(es.requests, 'post', lambda *a, **k: resp(
        status=500, raise_exc=es.requests.exceptions.HTTPError('500')))
    assert es._tavily_search('X') is None


def test_genuine_empty_answer_still_records_negative(env):
    env.fc.result = {'answer': '', 'results': []}          # Firecrawl ANSWERED: nothing
    env.tv.result = None                                   # paid rung did not answer
    es._BUDGET['obj'] = es.SearchBudget(1)
    assert es.tavily_search('Vapor Ventures Inc', 'x') == {'answer': '', 'results': []}
    cache = AccountCache(env.cache_path)
    assert cache.should_skip('vapor ventures', 'firmographic') is True
    # …at the SCRAPE rung only: the paid rung never answered, so it is not struck out
    assert cache.should_skip('vapor ventures', 'firmographic', want_paid=True) is False
    assert es.SEARCH_COUNTS['transport_failed'] == 1 and es.SEARCH_COUNTS['lookups'] == 1


# ── 6. Stage B: a cached lookup proceeds at zero budget ─────────────────────
def test_stage_b_cached_search_proceeds_when_budget_is_spent(env, monkeypatch):
    called = []
    monkeypatch.setattr(es, 'enrich_one_company', lambda *a, **k: called.append(a[0]) or {})
    AccountCache(env.cache_path).set_search(es._gates_account_key('Third Co'),
                                            'firmographic', HIT)
    spent = es.SearchBudget(1)
    spent.used = spent.max_searches
    es._BUDGET['obj'] = spent
    es._stage_b_company({'name': 'Third Co'}, {'hq': None}, 'ctx', tier=1)
    assert called == ['Third Co']                          # free: served from the cache
    es._stage_b_company({'name': 'Fourth Co'}, {'hq': None}, 'ctx', tier=1)
    assert called == ['Third Co']                          # not cached: budget rule holds
    # end to end: the cached lookup costs no budget inside tavily_search
    es._BUDGET['obj'] = spent
    assert es.tavily_search('Third Co', 'x') == HIT and spent.used == spent.max_searches


# ── 7. Summary counts a Firecrawl→Tavily fallback as ONE lookup ─────────────
def test_lookups_counted_once_per_fallback(env):
    env.fc.result = {}          # Firecrawl answers empty …
    env.tv.result = HIT         # … Tavily fallback hits
    for i in range(10):
        es._BUDGET['obj'] = es.SearchBudget(1)
        es._breaker.update(streak=0, open_until=0.0)   # 6 empties would open it
        assert es.tavily_search(f'Fallback Co {i}', 'x').get('results')
    sc = es.SEARCH_COUNTS
    assert sc['lookups'] == 10
    assert sc['firecrawl'] == 10 and sc['tavily'] == 10 and sc['firecrawl_attempts'] == 20
    # served-without-a-search calls are not lookups
    es._BUDGET['obj'] = es.SearchBudget(1)
    assert es.tavily_search('Fallback Co 0', 'x') == HIT
    assert sc['cache'] == 1 and sc['lookups'] == 10


def test_summary_reports_lookups(env, monkeypatch, caplog):
    env.fc.result = HIT
    llm = LLMStub(COMPANIES, article={'zi_subindustry': 'Banking', 'hq': None,
                                      'classification_confidence': 'High'},
                  search=SEARCH_BOSTON, grade=GRADE_B)
    with caplog.at_level(logging.INFO):
        _run(monkeypatch, FakeClient([_event()], TYPED_COLS), llm)
    m = re.search(r'Searches: (\d+) lookups \(firecrawl:(\d+) ', caplog.text)
    assert m and int(m.group(1)) == es.SEARCH_COUNTS['lookups'] >= 1
    assert int(m.group(2)) == es.SEARCH_COUNTS['firecrawl']


# ── 8. httpx request lines are silenced ─────────────────────────────────────
def test_httpx_logger_is_quiet():
    assert logging.getLogger('httpx').level == logging.WARNING
