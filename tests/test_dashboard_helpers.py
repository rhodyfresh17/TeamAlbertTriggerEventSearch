"""Pure-helper tests for dashboard.py (v2 Phase 1 — what reps see).

dashboard.py is a Streamlit script; importing it outside `streamlit run`
works in "bare mode" but logs a warning per st.* call at module level, so
those loggers are silenced BEFORE the import. Nothing here touches
Supabase or needs a Streamlit runtime — every function under test is a
module-level pure function over dicts / DataFrames.
"""
import logging
import math
import warnings

import pandas as pd
import pytest

warnings.simplefilter('ignore')
for _name in ('streamlit', 'streamlit.runtime',
              'streamlit.runtime.scriptrunner_utils.script_run_context',
              'streamlit.runtime.caching.cache_data_api'):
    logging.getLogger(_name).setLevel(logging.CRITICAL)

import dashboard as d  # noqa: E402  (after the logger silencing on purpose)

NAN = float('nan')


# ── fit.verdict → verified / unverified split ────────────────────────────────
@pytest.mark.parametrize('fit,expected', [
    ({'verdict': 'pass'}, 'pass'),
    ({'verdict': 'unverified'}, 'unverified'),
    ({'verdict': 'staged'}, 'staged'),
    ({'verdict': 'fail'}, 'fail'),
    ({'verdict': 'decided'}, 'decided'),
    ({'verdict': ' PASS '}, 'pass'),                   # normalized
    ('{"verdict": "pass"}', 'pass'),                    # JSON string (legacy)
    ('not json', 'staged'),
    ({}, 'staged'),                                     # fit without a verdict
    (None, 'staged'),                                   # never enriched
    (NAN, 'staged'),                                    # pandas NULL
])
def test_fit_verdict(fit, expected):
    assert d.fit_verdict({'fit': fit}) == expected
    assert d.fit_verdict(pd.Series({'fit': fit})) == expected


def _verdict_frame():
    return pd.DataFrame([
        {'id': 'p1', 'fit': {'verdict': 'pass'}},
        {'id': 'u1', 'fit': {'verdict': 'unverified'}},
        {'id': 's1', 'fit': {'verdict': 'staged'}},
        {'id': 'f1', 'fit': {'verdict': 'fail'}},
        {'id': 'd1', 'fit': {'verdict': 'decided'}},
        {'id': 'n1', 'fit': None},                      # never researched
        {'id': 'p2', 'fit': '{"verdict": "pass"}'},
    ])


def test_split_by_verdict_default_is_verified_only():
    kept, n_unverified = d.split_by_verdict(_verdict_frame(), show_unverified=False)
    assert list(kept['id']) == ['p1', 'p2']
    # unverified + staged + never-researched are what the toggle hides
    assert n_unverified == 3


def test_split_by_verdict_toggle_adds_unverified_and_staged_never_fail_or_decided():
    kept, n_unverified = d.split_by_verdict(_verdict_frame(), show_unverified=True)
    assert list(kept['id']) == ['p1', 'u1', 's1', 'n1', 'p2']
    assert 'f1' not in set(kept['id']) and 'd1' not in set(kept['id'])
    assert n_unverified == 3          # same number, now "shown" not "hidden"


def test_split_by_verdict_empty_frame():
    kept, n = d.split_by_verdict(pd.DataFrame(), show_unverified=False)
    assert kept.empty and n == 0


def test_split_by_verdict_no_fit_column_hides_everything_by_default():
    df = pd.DataFrame([{'id': 1}, {'id': 2}])
    kept, n = d.split_by_verdict(df, show_unverified=False)
    assert kept.empty and n == 2
    kept, n = d.split_by_verdict(df, show_unverified=True)
    assert len(kept) == 2


def test_verdict_sets_are_disjoint_and_exclude_fail_decided():
    assert not (d.VERIFIED_VERDICTS & d.UNVERIFIED_VERDICTS)
    for v in ('fail', 'decided'):
        assert v not in d.VERIFIED_VERDICTS and v not in d.UNVERIFIED_VERDICTS


# ── HQ string → state/province code ──────────────────────────────────────────
@pytest.mark.parametrize('hq,code', [
    ('Boston, MA', 'MA'), ('Boston, MA, USA', 'MA'), ('Boston, Massachusetts', 'MA'),
    ('Toronto, ON, Canada', 'ON'), ('Toronto, Ontario', 'ON'), ('Québec City, Québec', 'QC'),
    ('Indianapolis, IN', 'IN'), ('Portland, ME', 'ME'), ('Portland, OR', 'OR'),
    ('Washington, DC', 'DC'), ('Washington, D.C.', 'DC'), ('Washington DC', 'DC'),
    ('Charleston, West Virginia', 'WV'), ('Charleston West Virginia', 'WV'),  # longest name wins
    ('New York, NY, USA', 'NY'), ('Greater New York Area', 'NY'), ('Boston MA', 'MA'),
    ('Seattle, WA', 'WA'), ('Seattle, Washington', 'WA'), ('Austin, TX, USA', 'TX'),
    ('Vancouver, BC, Canada', 'BC'),
    # scraper matched_regions shapes
    ('massachusetts', 'MA'), ('MA', 'MA'), ('ohio', 'OH'),
    # nothing readable
    ('boston', None), ('London, UK', None), ('Bangalore, India', None),
    ('Remote', None), ('North America', None), ('', None), (None, None), (NAN, None),
    ('Portland ME', None),   # ambiguous bare tail (also an English word) is not trusted
])
def test_hq_state_code(hq, code):
    assert d.hq_state_code(hq) == code


def test_hq_state_code_agrees_with_gates_territory_status():
    """The dashboard code and gates' in/out must never disagree on a
    readable HQ: code in TERRITORY_STATES ⇔ gates says 'in'."""
    from src.pipeline.gates import hq_territory_status, TERRITORY_STATES
    for hq in ('Boston, MA', 'Toronto, ON, Canada', 'Indianapolis, IN', 'Portland, ME',
               'Seattle, WA', 'Austin, TX', 'Vancouver, BC', 'Birmingham, AL'):
        code = d.hq_state_code(hq)
        assert code is not None
        assert (code in TERRITORY_STATES) == (hq_territory_status(hq) == 'in'), hq


# ── event → state code (account hq first, matched_regions fallback) ──────────
def _cd():
    return [{'name': 'Acme Corp', 'role': 'target', 'hq': 'Austin, TX'},
            {'name': 'Buyer Co', 'role': 'acquirer', 'hq': 'Boston, MA'}]


def test_event_state_code_prefers_fit_account_over_primary_role():
    row = {'companies_data': _cd(), 'fit': {'verdict': 'pass', 'account_name': 'Acme Corp'}}
    assert d.event_state_code(row) == 'TX'


def test_event_state_code_falls_back_to_primary_role_order_when_no_account():
    # acquirer outranks target in _PRIMARY_ROLES
    row = {'companies_data': _cd(), 'fit': NAN}
    assert d.event_state_code(row) == 'MA'


def test_event_state_code_accepts_json_strings():
    row = {'companies_data': '[{"name":"X","role":"hiring company","hq":"Toronto, Ontario"}]',
           'fit': '{"verdict":"unverified"}'}
    assert d.event_state_code(row) == 'ON'


@pytest.mark.parametrize('regions,code', [
    (['massachusetts', 'boston'], 'MA'),     # news feed: lower-case names + cities
    (['boston', 'worcester'], None),         # cities only → unknown
    (['NY'], 'NY'),                          # SEC / Adzuna: a code
    ('ohio, columbus', 'OH'),                # legacy comma-joined string
    ('["florida"]', 'FL'),                   # JSON-string list
    ([], None), (None, None), (NAN, None),
])
def test_event_state_code_matched_regions_fallback(regions, code):
    row = {'companies_data': NAN, 'fit': NAN, 'matched_regions': regions}
    assert d.event_state_code(pd.Series(row)) == code


def test_event_state_code_account_hq_unreadable_falls_through_to_regions():
    row = {'companies_data': [{'name': 'X', 'role': 'primary', 'hq': 'Remote'}],
           'fit': None, 'matched_regions': ['VT']}
    assert d.event_state_code(row) == 'VT'


def test_annotate_and_territory_options_and_filter():
    df = pd.DataFrame([
        {'id': 1, 'companies_data': _cd(), 'fit': {'account_name': 'Acme Corp'}},
        {'id': 2, 'companies_data': _cd(), 'fit': None},
        {'id': 3, 'companies_data': NAN, 'fit': NAN, 'matched_regions': ['ontario']},
        {'id': 4, 'companies_data': NAN, 'fit': NAN, 'matched_regions': ['boston']},
    ])
    df = d.annotate_hq_state(df)
    assert list(df['_hq_state']) == ['TX', 'MA', 'ON', None]
    # only codes present; in-territory first, then alphabetical
    assert d.territory_options(df) == ['MA', 'ON', 'TX']
    assert list(d.filter_by_territory(df, ['MA', 'ON'])['id']) == [2, 3]
    assert d.filter_by_territory(df, []) is df                    # no-op
    assert d.filter_by_territory(df, ['CA']).empty


def test_annotate_hq_state_empty_frame():
    out = d.annotate_hq_state(pd.DataFrame())
    assert '_hq_state' in out.columns and out.empty


def test_territory_label_and_options_on_empty():
    assert d.territory_label('MA') == 'MA · Massachusetts'
    assert d.territory_label('DC') == 'DC · District of Columbia'
    assert d.territory_label('NU') == 'NU'          # code without a name entry
    assert d.territory_options(pd.DataFrame()) == []


# ── Work Queue ranking key ───────────────────────────────────────────────────
def _r(id_, grade, score, published, discovered=None):
    return {'id': id_, 'grade': grade, 'numeric_score': score,
            'published_date': published, 'discovered_date': discovered}


def test_work_queue_tie_break_is_newest_first():
    rows = [_r('old', 'A', 8, '2026-08-01T00:00:00+00:00'),
            _r('mid', 'A', 8, '2026-08-20'),
            _r('new', 'A', 8, '2026-09-05T09:30:00')]
    assert [r['id'] for r in sorted(rows, key=d.work_queue_sort_key)] == ['new', 'mid', 'old']


def test_work_queue_grade_then_score_then_freshness():
    rows = [_r('b_hi', 'B', 9, '2026-09-07'),
            _r('a_lo', 'A', 5, '2026-09-07'),
            _r('a_hi_old', 'A', 8, '2026-08-01'),
            _r('a_hi_new', 'A', 8, '2026-09-01'),
            _r('ungraded', NAN, NAN, '2026-09-07'),
            _r('c', 'C', 4, '2026-09-07'),
            _r('d', 'D', 0, '2026-09-07')]
    order = [r['id'] for r in sorted(rows, key=d.work_queue_sort_key)]
    assert order == ['a_hi_new', 'a_hi_old', 'a_lo', 'b_hi', 'ungraded', 'c', 'd']


def test_work_queue_missing_published_falls_back_to_discovered_then_oldest():
    rows = [_r('no_dates', 'A', 8, None, None),
            _r('disc_only', 'A', 8, NAN, '2026-09-06T10:00:00'),
            _r('pub', 'A', 8, '2026-09-01', '2026-09-07')]
    order = [r['id'] for r in sorted(rows, key=d.work_queue_sort_key)]
    assert order == ['disc_only', 'pub', 'no_dates']


@pytest.mark.parametrize('bad', ['nan', 'NaT', 'None', '', 'not a date', None, NAN])
def test_epoch_garbage_is_zero(bad):
    assert d._epoch(bad) == 0.0


def test_epoch_accepts_timestamp_objects():
    assert d._epoch(pd.Timestamp('2026-09-01', tz='UTC')) > 0


@pytest.mark.parametrize('raw,expected', [(8, 8), ('7', 7), (NAN, -1), (None, -1), ('x', -1)])
def test_score_of(raw, expected):
    assert d._score_of({'numeric_score': raw}) == expected


# ── finance_seat_open + unknown event types ──────────────────────────────────
def test_finance_seat_open_is_configured_everywhere():
    cfg = d.EVENT_TYPES['finance_seat_open']
    assert cfg['icon'] == '🪑'
    assert cfg['full_label'] == 'Open Finance Seats'
    for key in ('label', 'full_label', 'color', 'gradient', 'icon', 'badge_class', 'bg_color'):
        assert cfg.get(key), key
    assert 'finance_seat_open' in d.FINANCE_LEADER_EVENT_TYPES


def test_finance_leader_mask_includes_open_seats_and_controller_tag():
    df = pd.DataFrame({
        'event_type': ['cfo_hire', 'finance_seat_open', 'funding', 'executive_hire', 'executive_hire'],
        'hashtags': [None, None, None, ['#NewController'], '["#Funding"]'],
    })
    assert list(d._finance_leader_mask(df)) == [True, True, False, True, False]
    assert d._finance_leader_mask(pd.DataFrame()).empty


@pytest.mark.parametrize('et', [None, NAN, 'bogus_type', '', 'CFO_HIRE', 42])
def test_event_config_for_never_crashes(et):
    cfg = d.event_config_for(et)
    assert cfg is d.EVENT_TYPES['other']
    assert cfg['icon'] and cfg['label'] and cfg['badge_class']


def test_event_config_for_known_types():
    for et, cfg in d.EVENT_TYPES.items():
        assert d.event_config_for(et) is cfg


# ── JSONB unwrap helper ──────────────────────────────────────────────────────
@pytest.mark.parametrize('val,expected', [
    (None, 'dflt'), (NAN, 'dflt'), ('', 'dflt'), ('   ', 'dflt'), ('{bad', 'dflt'),
    ('{"a": 1}', {'a': 1}), ('[1, 2]', [1, 2]), ({'a': 1}, {'a': 1}), ([1], [1]),
])
def test_parse_json_field(val, expected):
    assert d._parse_json_field(val, 'dflt') == expected


# ── v2 Phase 2: typed columns read server-side (2026-09-07) ─────────────────
class _FakeQuery:
    """Chainable stand-in for a PostgREST query builder: every method
    records (name, args, kwargs) and returns self, so a test can assert
    exactly which filters were applied. `execute()` returns a canned
    response or raises when the fake is told the column is missing."""

    def __init__(self, calls, missing=(), count=None):
        self.calls, self._missing, self._count = calls, set(missing), count
        self._selected = None

    def __getattr__(self, name):
        def _rec(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == 'select':
                self._selected = args[0] if args else None
            return self
        return _rec

    def execute(self):
        self.calls.append(('execute', (), {}))
        if self._selected in self._missing:
            raise RuntimeError('column "%s" does not exist' % self._selected)
        return type('Resp', (), {'data': [], 'count': self._count})()


class _FakeClient:
    def __init__(self, missing=(), count=None):
        self.calls, self._missing, self._count = [], missing, count

    def table(self, name):
        self.calls.append(('table', (name,), {}))
        return _FakeQuery(self.calls, self._missing, self._count)


def _names(calls):
    return [c[0] for c in calls]


def _call(calls, name):
    return next(c for c in calls if c[0] == name)


NOW = pd.Timestamp('2026-09-07T12:00:00').to_pydatetime()


def test_probe_typed_columns_reports_only_existing_columns():
    client = _FakeClient(missing={'hq_state', 'expires_at'})
    present = d._probe_typed_columns(client)
    assert present == {'verify_state', 'fit_verdict', 'source'}
    # one select(col).limit(1).execute() per probed column, nothing else
    assert _names(client.calls).count('select') == len(d.TYPED_PROBE_COLUMNS)
    assert all(c[1] == (1,) for c in client.calls if c[0] == 'limit')


def test_probe_typed_columns_no_client_is_empty():
    assert d._probe_typed_columns(None) == set()
    assert d._probe_typed_columns(_FakeClient(missing=set(d.TYPED_PROBE_COLUMNS))) == set()


def test_build_events_query_legacy_when_no_typed_columns():
    client = _FakeClient()
    d.build_events_query(client, days=30, verified_only=True, present=set(), now=NOW)
    names = _names(client.calls)
    assert names[:3] == ['table', 'select', 'gte']
    assert _call(client.calls, 'table')[1] == ('events',)
    assert _call(client.calls, 'select')[1] == ('*',)
    assert _call(client.calls, 'gte')[1] == ('discovered_at', '2026-08-08T12:00:00')
    assert _call(client.calls, 'is_')[1] == ('blocked_at', 'null')
    assert 'eq' not in names and 'in_' not in names and 'or_' not in names


def test_build_events_query_none_means_no_verify_filter_even_when_present():
    client = _FakeClient()
    d.build_events_query(client, 30, None, {'verify_state'}, now=NOW)
    names = _names(client.calls)
    assert 'eq' not in names and 'or_' not in names


def test_build_events_query_verified_only_admits_null_state_with_pass_verdict():
    """verified_only=True must also admit rows an in-flight enricher wrote
    with fit.verdict='pass' but verify_state NULL (review 2026-09-07) — a
    bare eq('verify_state','verified') hid them from the default view."""
    client = _FakeClient()
    d.build_events_query(client, 7, True, {'verify_state', 'expires_at'}, now=NOW)
    (expr,) = _call(client.calls, 'or_')[1]
    assert expr == d.VERIFIED_FILTER == (
        "verify_state.eq.verified,and(verify_state.is.null,fit->>verdict.eq.pass)")
    assert 'eq' not in _names(client.calls)
    # expires_at is present but deliberately NOT filtered (Phase 4 decides)
    assert not any('expires_at' in str(c[1]) for c in client.calls)


def test_build_events_query_toggle_on_mirrors_split_by_verdict():
    """Toggle ON shows pass + unverified + staged (+ never-enriched rows,
    which fit_verdict maps to 'staged' and verify_state leaves NULL)."""
    client = _FakeClient()
    d.build_events_query(client, 7, False, {'verify_state'}, now=NOW)
    (expr,) = _call(client.calls, 'or_')[1]
    assert expr == d.TOGGLE_ON_FILTER == (
        "verify_state.in.(verified,researched_ambiguous,staged),verify_state.is.null")
    assert 'eq' not in _names(client.calls)
    # the state list is the typed twin of the client-side verdict sets
    assert set(d.VERIFIED_STATES + d.UNVERIFIED_STATES) == {
        'verified', 'researched_ambiguous', 'staged'}
    assert d.UNVERIFIED_VERDICTS == {'unverified', 'staged'}   # unchanged


def test_count_hidden_unverified_is_the_complement():
    client = _FakeClient(count=42)
    n = d.count_hidden_unverified(30, {'verify_state'}, client=client, now=NOW)
    assert n == 42
    assert _call(client.calls, 'select')[1:] == (('id',), {'count': 'exact'})
    assert _call(client.calls, 'gte')[1] == ('discovered_at', '2026-08-08T12:00:00')
    assert _call(client.calls, 'is_')[1] == ('blocked_at', 'null')
    (expr,) = _call(client.calls, 'or_')[1]
    assert expr == d.HIDDEN_FILTER == (
        "verify_state.in.(researched_ambiguous,staged),"
        "and(verify_state.is.null,or(fit->>verdict.is.null,fit->>verdict.neq.pass))")
    assert 'verify_state.eq.verified' not in expr and '(verified' not in expr   # never the shown rows


# A minimal evaluator for the PostgREST logic subset the three filters use
# (top-level comma = OR, and(...) / or(...) nesting, eq / neq / in / is on a
# column or the fit->>verdict JSON path, with SQL NULL semantics: a NULL
# cell satisfies only `is.null`). It lets the partition property be checked
# against the STRINGS, not against a Python restatement of them.
def _pg_eval(expr: str, row: dict) -> bool:
    def split_top(s):
        parts, depth, cur = [], 0, ''
        for ch in s:
            depth += (ch == '(') - (ch == ')')
            if ch == ',' and depth == 0:
                parts.append(cur); cur = ''
            else:
                cur += ch
        return parts + [cur]

    def ev(term):
        term = term.strip()
        if term.startswith('and(') or term.startswith('or('):
            fn = all if term.startswith('and(') else any
            return fn(ev(t) for t in split_top(term[term.index('(') + 1:-1]))
        col, op, val = term.split('.', 2)
        if col == 'fit->>verdict':
            fit = row['fit']
            cell = fit.get('verdict') if isinstance(fit, dict) else None
        else:
            cell = row[col]
        if op == 'is':
            return cell is None and val == 'null'
        if cell is None:
            return False                       # NULL = / <> / IN anything → not true
        return {'eq': cell == val, 'neq': cell != val,
                'in': cell in val[1:-1].split(',')}[op]
    return any(ev(t) for t in split_top(expr))


def test_verification_filters_partition_the_toggle_on_set():
    """VERIFIED ∪ HIDDEN == TOGGLE ON and VERIFIED ∩ HIDDEN == ∅ over every
    (verify_state, fit) shape that occurs — the identity the live check
    confirmed on 2026-09-07 (1 + 288 == 289 over a 30-day window)."""
    states = ('verified', 'researched_ambiguous', 'staged', 'decided', 'not_fit', None)
    fits = (None, {}, {'verdict': 'pass'}, {'verdict': 'unverified'}, {'verdict': 'staged'},
            {'verdict': 'fail'}, {'verdict': 'decided'})
    for s in states:
        for f in fits:
            row = {'verify_state': s, 'fit': f}
            v, h, t = (_pg_eval(e, row) for e in (d.VERIFIED_FILTER, d.HIDDEN_FILTER, d.TOGGLE_ON_FILTER))
            assert not (v and h), row
            assert (v or h) == t, row
    # the in-flight shapes the review named
    assert _pg_eval(d.VERIFIED_FILTER, {'verify_state': None, 'fit': {'verdict': 'pass'}})
    assert _pg_eval(d.HIDDEN_FILTER, {'verify_state': None, 'fit': None})
    assert _pg_eval(d.HIDDEN_FILTER, {'verify_state': None, 'fit': {}})        # no verdict key
    assert _pg_eval(d.HIDDEN_FILTER, {'verify_state': None, 'fit': {'verdict': 'fail'}})
    assert not _pg_eval(d.VERIFIED_FILTER, {'verify_state': 'decided', 'fit': {'verdict': 'pass'}})
    assert not _pg_eval(d.TOGGLE_ON_FILTER, {'verify_state': 'not_fit', 'fit': None})


def test_unverified_caption_says_what_it_counts():
    """Review 2026-09-07: the server-side hidden count spans the whole
    window, the toggle-ON count is the frame on screen — the caption must
    say which."""
    assert d.unverified_caption(3, True, 30, False) == '3 unverified shown'
    assert d.unverified_caption(3, True, 30, True) == '3 unverified shown'     # ON is never window-wide
    assert d.unverified_caption(1234, False, 30, False) == '1,234 unverified hidden'
    assert d.unverified_caption(1234, False, 7, True) == '1,234 unverified hidden (whole 7-day window)'


# ── load_events wiring (review 2026-09-07) ───────────────────────────────────
# Every test above calls build_events_query directly, so they would all pass
# if load_events stopped calling it. These two go through load_events.

def _load_events_fn():
    # load_events is a plain function today; should it ever gain
    # @st.cache_data, bypass the cache so the fake client is what runs.
    return getattr(d.load_events, '__wrapped__', d.load_events)


def test_load_events_wires_verify_filter_when_column_present(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    monkeypatch.setattr(d, 'typed_columns_present', lambda: {'verify_state'})
    df = _load_events_fn()(days=30, verified_only=True)
    assert df.empty                                        # the fake returns no rows
    names = _names(client.calls)
    assert _call(client.calls, 'table')[1] == ('events',)
    assert _call(client.calls, 'or_')[1] == (d.VERIFIED_FILTER,)
    assert names.index('or_') < names.index('order') < names.index('range') < names.index('execute')
    # toggle ON takes the union expression through the same path
    client = _FakeClient()
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    _load_events_fn()(days=30, verified_only=False)
    assert _call(client.calls, 'or_')[1] == (d.TOGGLE_ON_FILTER,)


def test_load_events_applies_no_verify_filter_when_column_absent(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    monkeypatch.setattr(d, 'typed_columns_present', lambda: set())
    df = _load_events_fn()(days=30, verified_only=True)
    assert df.empty
    names = _names(client.calls)
    assert 'or_' not in names and 'eq' not in names and 'in_' not in names
    assert 'gte' in names and 'execute' in names            # the query itself still ran
    assert not any('verify_state' in str(c[1]) for c in client.calls)


def test_count_hidden_unverified_degrades_to_zero():
    client = _FakeClient(count=42)
    assert d.count_hidden_unverified(30, set(), client=client, now=NOW) == 0
    assert client.calls == []            # no query without the typed column
    assert d.count_hidden_unverified(30, {'verify_state'},
                                     client=_FakeClient(missing={'id'}), now=NOW) == 0
    assert d.count_hidden_unverified(30, {'verify_state'},
                                     client=_FakeClient(count=None), now=NOW) == 0


def test_split_by_verdict_is_idempotent_on_prefiltered_frames():
    """Belt-and-braces: the client-side pass over rows the server already
    filtered must return the same frame."""
    verified = pd.DataFrame([
        {'id': 'p1', 'fit': {'verdict': 'pass'}, 'verify_state': 'verified'},
        {'id': 'p2', 'fit': '{"verdict": "pass"}', 'verify_state': 'verified'},
    ])
    kept, n = d.split_by_verdict(verified, show_unverified=False)
    assert list(kept['id']) == ['p1', 'p2'] and n == 0
    shown = pd.DataFrame([
        {'id': 'p1', 'fit': {'verdict': 'pass'}, 'verify_state': 'verified'},
        {'id': 'u1', 'fit': {'verdict': 'unverified'}, 'verify_state': 'researched_ambiguous'},
        {'id': 'n1', 'fit': None, 'verify_state': None},
    ])
    kept, n = d.split_by_verdict(shown, show_unverified=True)
    assert list(kept['id']) == ['p1', 'u1', 'n1'] and n == 2


@pytest.mark.parametrize('row,expected', [
    ({'hq_state': 'MA', 'fit': {'account_name': 'X', 'companies': [{'name': 'X', 'hq': 'Austin, TX'}]}}, 'MA'),
    ({'hq_state': ' on ', 'matched_regions': ['TX']}, 'ON'),
    ({'hq_state': '', 'matched_regions': ['TX']}, 'TX'),      # empty → parse
    ({'hq_state': None, 'matched_regions': ['TX']}, 'TX'),
    ({'hq_state': NAN, 'matched_regions': ['TX']}, 'TX'),     # pandas NULL
    ({'matched_regions': ['TX']}, 'TX'),                      # pre-migration row
])
def test_event_state_code_prefers_typed_hq_state(row, expected):
    assert d.event_state_code(row) == expected
    assert d.event_state_code(pd.Series(row)) == expected


@pytest.mark.parametrize('arg,expected', [
    ('https://www.sec.gov/Archives/edgar/x', 'SEC 8-K'),                   # legacy str; no title → 8-K
    ('https://www.adzuna.com/jobs/1', 'Adzuna'),
    (None, 'other'),
    ({'source': 'sec_edgar', 'source_url': 'https://example.com/a'}, 'SEC 8-K'),
    ({'source': 'sec_edgar', 'source_url': None,
      'title': 'SEC Form D (Private Capital Raise) — Co'}, 'SEC Form D'),  # split by title, like the pivot
    ({'source': 'globe_newswire', 'source_url': None}, 'GlobeNewswire'),
    ({'source': 'other', 'source_url': 'https://news.example.com/a'}, 'news.example.com'),
    ({'source': 'other', 'source_url': 'https://www.nhbr.com/x'}, 'NH Business Review'),   # Phase 3 feed: named
    ({'source': None, 'source_url': 'https://www.adzuna.com/jobs/1'}, 'Adzuna'),
    ({'source_url': 'https://www.sec.gov/x'}, 'SEC 8-K'),                   # pre-migration row
    ({}, 'other'),
])
def test_scorecard_src_accepts_url_or_row(arg, expected):
    assert d._scorecard_src(arg) == expected


# ── Phase 3 supply visibility (2026-09-08) ───────────────────────────────────
# Weekly Scorecard → Supply: pure aggregations over the one scorecard query.
# Synthetic rows only; every expected number below is hand-computed from them.
from datetime import datetime, timedelta, timezone  # noqa: E402

import monitor_health as mh  # noqa: E402  (the Monday check must agree with the dashboard)

SNOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
FS, NP, CS = 'Financial Services', 'Nonprofits & Organizations', 'Consumer Services'
ADZ_URL = 'https://www.adzuna.com/details/1'
SEC_URL = 'https://www.sec.gov/Archives/edgar/data/1/a'
PRN_URL = 'https://www.prnewswire.com/news/a'
IAPD_URL = 'https://adviserinfo.sec.gov/firm/summary/123456'
_seq = [0]


def _srow(days_ago, source=None, url=None, etype='funding', title='x', blocked=False,
          hashtags=None, **extra):
    """One row of the typed scorecard select, `days_ago` before SNOW."""
    _seq[0] += 1
    when = (SNOW - timedelta(days=days_ago)).isoformat()
    r = {'id': f'e{_seq[0]}', 'discovered_at': when,
         'blocked_at': when if blocked else None,
         'blocked_reason': 'fit_gate:territory' if blocked else None,
         'grade': 'B', 'source': source, 'source_url': url, 'title': title,
         'event_type': etype, 'hashtags': hashtags,
         'verify_state': None, 'fit_verdict': None, 'verdict_json': None,
         'zi_subindustry': None, 'classified_by': None, 'account_key': None}
    r.update(extra)
    return r


@pytest.mark.parametrize('arg,expected', [
    ({'source': 'sec_iapd', 'source_url': IAPD_URL}, 'SEC IAPD'),
    ({'source': 'sec_iapd', 'source_url': None}, 'SEC IAPD'),
    (IAPD_URL, 'SEC IAPD'),                                    # legacy str
    ({'source_url': IAPD_URL}, 'SEC IAPD'),                    # pre-migration / stale-probe row
    ({'source': None, 'source_url': 'https://www.sec.gov/cgi-bin/browse-edgar'}, 'SEC 8-K'),
])
def test_scorecard_src_sec_iapd_sits_before_the_edgar_fallback(arg, expected):
    assert d._scorecard_src(arg) == expected


def test_scorecard_src_is_the_supply_pivot_vocabulary():
    """Review 2026-09-08: "New events by source" and the supply pivot share
    one expander and used two label tables ('SEC EDGAR' vs 'SEC 8-K' /
    'SEC Form D'; a bare host vs 'NH Business Review'). One vocabulary now:
    _scorecard_src IS src.pipeline.sources.source_label."""
    from src.pipeline.sources import source_label
    rows = _pivot_rows() + [_srow(1, 'other', 'https://www.nhbr.com/news/1'),
                            _srow(1, None, 'https://www.pbn.com/a'),
                            _srow(1, 'sec_iapd', IAPD_URL)]
    assert [d._scorecard_src(r) for r in rows] == [source_label(r) for r in rows]
    assert {d._scorecard_src(r) for r in rows} >= {'SEC 8-K', 'SEC Form D', 'NH Business Review',
                                                    'Providence Business News', 'SEC IAPD'}
    for url in (SEC_URL, ADZ_URL, IAPD_URL, 'https://www.nhbr.com/x', None):   # str signature kept
        assert d._scorecard_src(url) == source_label({'source_url': url})
    assert not hasattr(d, '_SOURCE_LABELS')                                    # no second table to drift


def test_vertical_taxonomy_and_thresholds_are_shared_with_enrichment_and_monitor():
    """One taxonomy, one set of bars: the dashboard's Supply section and the
    Monday health check must never disagree about what they measure."""
    from enrichment_scout import ZI_SUBINDUSTRIES, NONPROFIT_VERTICAL
    assert d.ZI_SUBINDUSTRIES is ZI_SUBINDUSTRIES and mh.ZI_SUBINDUSTRIES is ZI_SUBINDUSTRIES
    assert set(ZI_SUBINDUSTRIES.values()) == {FS, NP, CS}
    assert d.VERTICAL_ORDER == [FS, NP, CS]
    assert d.NONPROFIT_VERTICAL == mh.NONPROFIT_VERTICAL == NONPROFIT_VERTICAL == NP
    assert d.NO_SEARCH_CLASSIFIERS == mh.NO_SEARCH_CLASSIFIERS == {'oracle', 'structured'}
    assert 'cache' not in d.NO_SEARCH_CLASSIFIERS       # a cache hit replays a search (review 2026-09-08)
    assert d.SHARE_JUDGE_MIN_N == mh.CONCENTRATION_MIN_ROWS == 5
    assert d.TOP_SOURCE_CEILING_PCT == mh.CONCENTRATION_WARN_PCT == 40
    assert d.FINANCE_LEADER_TARGET_PCT == mh.FINANCE_SHARE_TARGET_PCT == 30
    assert d.NONPROFIT_NO_SEARCH_TARGET_PCT == mh.NONPROFIT_NO_SEARCH_TARGET_PCT == 50
    assert d.SCORECARD_DAYS == mh.VERTICAL_WINDOW_DAYS == mh.YIELD_WINDOW_DAYS == 28
    assert mh.VERIFIED_FILTER == d.VERIFIED_FILTER
    assert d.UNKNOWN_VERTICAL == mh.UNKNOWN_VERTICAL


@pytest.mark.parametrize('zi,expected', [
    ('Banking', FS), ('Venture Capital & Private Equity', FS),
    ('Non-Profit & Charitable Organizations', NP), ('Libraries', NP),
    ('Real Estate', CS), ('Automobile Dealers', CS),
    ('  Banking ', FS),                       # whitespace from a hand edit
    ('OTHER', d.UNKNOWN_VERTICAL),            # the LLM's out-of-taxonomy answer
    ('Software', d.UNKNOWN_VERTICAL),         # legacy free-text industry
    ('', d.UNKNOWN_VERTICAL), (None, d.UNKNOWN_VERTICAL), (NAN, d.UNKNOWN_VERTICAL),
])
def test_vertical_of(zi, expected):
    assert d.vertical_of(zi) == expected
    assert mh.vertical_of(zi) == expected


@pytest.mark.parametrize('row,expected', [
    ({'verify_state': 'verified'}, True),
    ({'verify_state': ' Verified '}, True),
    ({'verify_state': 'not_fit', 'fit_verdict': 'pass'}, False),          # a state always wins
    ({'verify_state': 'researched_ambiguous', 'verdict_json': 'pass'}, False),
    ({'verify_state': None, 'fit_verdict': 'pass'}, True),                 # stale-probe enricher
    ({'verify_state': NAN, 'fit_verdict': 'pass'}, True),
    ({'verify_state': None, 'verdict_json': 'pass'}, True),                # the query's fit->>verdict alias
    ({'verify_state': None, 'fit': {'verdict': 'pass'}}, True),            # callers holding the blob
    ({'verify_state': None, 'fit': '{"verdict": "pass"}'}, True),
    ({'verify_state': None, 'fit_verdict': 'unverified', 'verdict_json': 'pass'}, False),
    ({'verify_state': None, 'fit': None}, False),
    ({}, False),
])
def test_is_verified_row(row, expected):
    assert d.is_verified_row(row) is expected


@pytest.mark.parametrize('row,expected', [
    ({'classified_by': 'oracle'}, 'oracle'),
    ({'classified_by': ' Cache '}, 'cache'),
    ({'classified_by': None,
      'companies_data': [{'name': 'A', 'role': 'primary', 'classified_by': 'structured'}]}, 'structured'),
    ({'classified_by': NAN, 'companies_data': [{'name': 'A', 'classified_by': 'search'}]}, 'search'),
    ({'classified_by': 'article', 'companies_data': [{'name': 'A', 'classified_by': 'search'}]}, 'article'),
    ({'classified_by': None, 'companies_data': [{'name': 'A'}]}, None),
    ({'classified_by': None, 'companies_data': None}, None),
    ({'classified_by': None}, None),                                       # the scorecard query: no blob
    ({}, None),
])
def test_classified_by_of(row, expected):
    assert d.classified_by_of(row) == expected


# (a) trigger × source pivot ------------------------------------------------

def _pivot_rows():
    """Recent window = 1–6 days before SNOW, prior = 8–13; plus one blocked
    row (survival only) and one row older than both windows (ignored)."""
    rows = [_srow(n, 'adzuna', ADZ_URL, 'finance_seat_open') for n in (1, 2, 3)]
    rows += [_srow(n, 'adzuna', ADZ_URL, 'finance_seat_open') for n in (8, 9)]
    rows.append(_srow(4, 'sec_edgar', SEC_URL, 'cfo_hire', title='SEC 8-K Item 5.02 — Co'))
    rows.append(_srow(5, 'pr_newswire', PRN_URL, 'executive_hire', hashtags=['#NewController']))
    rows.append(_srow(10, 'pr_newswire', PRN_URL, 'executive_hire'))
    rows += [_srow(n, 'sec_edgar', SEC_URL, 'funding', title='SEC Form D (Private Capital Raise) — Co')
             for n in (2, 6, 11)]
    rows.append(_srow(3, 'other', 'https://newadvisers.example/ria/1', 'expansion'))
    rows.append(_srow(1, 'adzuna', ADZ_URL, 'finance_seat_open', blocked=True))
    rows.append(_srow(20, 'adzuna', ADZ_URL, 'finance_seat_open'))
    return rows


def test_supply_pivot_rows_columns_and_cells():
    piv = d.supply_pivot(_pivot_rows(), now=SNOW, top_sources=2)
    assert piv['days'] == 7
    assert piv['sources'] == ['Adzuna', 'SEC Form D', 'other']          # top 2 by survivors, then the fold
    assert piv['survival'] == {'recent': (8, 9), 'prior': (4, 4)}        # blocked row counts here only
    by_key = {r['key']: r for r in piv['rows']}
    assert [r['key'] for r in piv['rows']] == ['finance_leader', 'cfo_hire', 'finance_seat_open',
                                               'controller_tag', 'funding', 'executive_hire',
                                               'expansion', 'all']
    assert [r['trigger'] for r in piv['rows']] == [
        d.FINANCE_LEADER_ROLLUP, '↳ CFO hire', '↳ Open finance seat',
        '↳ Controller hire (#NewController)', 'PE/VC Funding', 'Executive Hires',
        'Expansion / New registration',                                    # sec_iapd's type: its own card since Phase 4
        d.ALL_SURVIVORS]
    assert by_key['finance_leader']['cells'] == {'Adzuna': (3, 2), 'SEC Form D': (0, 0), 'other': (2, 0)}
    assert by_key['finance_leader']['total'] == (5, 2)
    assert by_key['cfo_hire']['total'] == (1, 0)
    assert by_key['finance_seat_open']['cells']['Adzuna'] == (3, 2)
    assert by_key['controller_tag']['total'] == (1, 0)                     # the tagged executive_hire
    assert by_key['executive_hire']['total'] == (0, 1)                     # … is NOT also counted here
    assert by_key['funding']['cells'] == {'Adzuna': (0, 0), 'SEC Form D': (2, 1), 'other': (0, 0)}
    assert by_key['expansion']['cells']['other'] == (1, 0)
    assert by_key['all']['cells'] == {'Adzuna': (3, 2), 'SEC Form D': (2, 1), 'other': (3, 1)}
    assert by_key['all']['total'] == (8, 4)


def test_supply_pivot_rollup_is_its_subrows_and_rows_partition_survivors():
    piv = d.supply_pivot(_pivot_rows(), now=SNOW, top_sources=2)
    rows = {r['key']: r for r in piv['rows']}
    subs = ('cfo_hire', 'finance_seat_open', 'controller_tag')
    for src in piv['sources']:
        assert rows['finance_leader']['cells'][src] == tuple(
            sum(rows[k]['cells'][src][i] for k in subs) for i in (0, 1))
        assert rows['all']['cells'][src] == tuple(
            sum(rows[k]['cells'][src][i] for k in subs + ('funding', 'executive_hire', 'expansion'))
            for i in (0, 1))


def test_supply_pivot_default_columns_rank_by_survivors_then_name():
    piv = d.supply_pivot(_pivot_rows(), now=SNOW)
    assert piv['sources'] == ['Adzuna', 'SEC Form D', 'PR Newswire', 'SEC 8-K',
                              'newadvisers.example', 'other']


def test_supply_pivot_empty():
    piv = d.supply_pivot([], now=SNOW)
    assert piv['sources'] == ['other']
    assert [r['key'] for r in piv['rows']] == ['finance_leader', 'cfo_hire', 'finance_seat_open',
                                               'controller_tag', 'all']
    assert all(r['total'] == (0, 0) for r in piv['rows'])
    assert piv['survival'] == {'recent': (0, 0), 'prior': (0, 0)}


def test_supply_pivot_frame_layout():
    frame = d.supply_pivot_frame(d.supply_pivot(_pivot_rows(), now=SNOW, top_sources=2))
    assert list(frame.columns) == ['Trigger', 'Adzuna', 'SEC Form D', 'other', 'Total']
    assert list(frame.iloc[0]) == [d.FINANCE_LEADER_ROLLUP, '3 / 2', '—', '2 / 0', '5 / 2']
    assert list(frame.iloc[-1]) == [d.ALL_SURVIVORS, '3 / 2', '2 / 1', '3 / 1', '8 / 4']


# (b) vertical mix of verified accounts -------------------------------------

def _mix_rows():
    v = {'verify_state': 'verified'}
    return [
        _srow(2, zi_subindustry='Banking', account_key='acme', classified_by='oracle', **v),
        _srow(10, zi_subindustry='Insurance', account_key='acme', **v),                 # older twin: loses
        _srow(3, zi_subindustry='Non-Profit & Charitable Organizations',
              account_key='helping-hands', classified_by='oracle', **v),
        _srow(4, zi_subindustry='Museums & Art Galleries', account_key='city-museum',
              classified_by='search', **v),
        _srow(5, zi_subindustry='Membership Organizations', account_key='foodbank', **v),   # no provenance
        _srow(6, zi_subindustry='Automobile Dealers', account_key='joes-auto', fit_verdict='pass'),
        _srow(9, zi_subindustry='Real Estate', verdict_json='pass'),                    # NULL state, alias, no key
        _srow(11, zi_subindustry='Real Estate', account_key='cd-co',
              companies_data=[{'name': 'CD Co', 'role': 'primary', 'classified_by': 'structured'}], **v),
        _srow(7, zi_subindustry='OTHER', account_key='mystery', classified_by='cache', **v),
        _srow(8, zi_subindustry=NAN, account_key='nan-co', **v),
        _srow(1, zi_subindustry='Banking', account_key='blocked-co', blocked=True, **v),
        _srow(1, zi_subindustry='Banking', account_key='not-fit-co', verify_state='not_fit',
              fit_verdict='pass'),
        _srow(40, zi_subindustry='Banking', account_key='old-co', **v),
    ]


def test_vertical_mix_counts_accounts_shares_and_no_search_share():
    mix = d.vertical_mix(_mix_rows(), now=SNOW)
    assert mix['days'] == 28 and mix['total'] == 9
    assert [r['vertical'] for r in mix['rows']] == [FS, NP, CS, d.UNKNOWN_VERTICAL]
    rows = {r['vertical']: r for r in mix['rows']}
    assert rows[FS]['accounts'] == 1                       # acme once, newest event decides
    assert rows[NP]['accounts'] == 3
    assert rows[CS]['accounts'] == 3                       # typed verdict, alias verdict, verified state
    assert rows[d.UNKNOWN_VERTICAL]['accounts'] == 2       # 'OTHER' and a NaN subindustry
    assert [round(r['share_pct'], 1) for r in mix['rows']] == [11.1, 33.3, 33.3, 22.2]
    assert (rows[NP]['provenance_n'], rows[NP]['no_search_n'], rows[NP]['no_search_pct']) == (2, 1, 50.0)
    assert (rows[FS]['provenance_n'], rows[FS]['no_search_pct']) == (1, 100.0)
    assert (rows[CS]['provenance_n'], rows[CS]['no_search_n']) == (1, 1)    # via companies_data
    assert (rows[d.UNKNOWN_VERTICAL]['provenance_n'],
            rows[d.UNKNOWN_VERTICAL]['no_search_n']) == (1, 0)              # 'cache' has provenance, not a free verify
    assert mix['provenance_n'] == 5 and mix['no_search_n'] == 3


def test_vertical_mix_cache_hit_is_not_verified_without_search():
    """Review 2026-09-08: classified_by 'cache' replays the classification
    the account cache stored on the account's first (usually searched)
    enrichment — counting it made the share climb with repeat events."""
    v = {'verify_state': 'verified'}
    rows = [_srow(1, zi_subindustry='Libraries', account_key=f'np{i}', classified_by='cache', **v)
            for i in range(5)]
    rows.append(_srow(1, zi_subindustry='Libraries', account_key='np-oracle', classified_by='oracle', **v))
    np_row = d.vertical_mix(rows, now=SNOW)['rows'][1]
    assert np_row['vertical'] == NP
    assert (np_row['provenance_n'], np_row['no_search_n']) == (6, 1)
    assert round(np_row['no_search_pct'], 1) == 16.7


def test_vertical_mix_no_unknown_row_and_none_share_without_provenance():
    mix = d.vertical_mix([_srow(2, zi_subindustry='Libraries', account_key='lib',
                                verify_state='verified')], now=SNOW)
    assert [r['vertical'] for r in mix['rows']] == [FS, NP, CS]
    np_row = mix['rows'][1]
    assert (np_row['accounts'], np_row['share_pct'], np_row['no_search_pct']) == (1, 100.0, None)
    assert d.vertical_mix([], now=SNOW)['total'] == 0


def test_vertical_mix_frame_texts():
    frame = d.vertical_mix_frame(d.vertical_mix(_mix_rows(), now=SNOW))
    assert list(frame.columns) == ['Vertical', 'Verified accounts (28d)', 'Share',
                                   'Verified without search']
    assert list(frame['Vertical']) == [FS, NP, CS, d.UNKNOWN_VERTICAL, 'All verticals']
    by = {r['Vertical']: r for _, r in frame.iterrows()}
    assert by[NP]['Verified without search'] == '50% (1 of 2 with provenance)'
    assert by[NP]['Share'] == '33%'
    assert list(frame.iloc[-1]) == ['All verticals', 9, '100%', '60% (3 of 5 with provenance)']
    lone = d.vertical_mix_frame(d.vertical_mix(
        [_srow(2, zi_subindustry='Libraries', account_key='lib', verify_state='verified')], now=SNOW))
    assert lone.iloc[1]['Verified without search'] == 'n/a — no provenance recorded'


# (c) the two headline numbers ----------------------------------------------

def test_supply_headline_shares():
    rows = [_srow(n, 'adzuna', ADZ_URL, 'finance_seat_open') for n in (1, 5, 20)]
    rows.append(_srow(3, 'sec_edgar', SEC_URL, 'cfo_hire', title='SEC 8-K Item 5.02 — Co'))
    rows += [_srow(n, 'sec_edgar', SEC_URL, 'funding', title='SEC Form D (Private Capital Raise) — Co')
             for n in range(2, 8)]
    rows.append(_srow(2, 'adzuna', ADZ_URL, 'finance_seat_open', blocked=True))   # tombstoned: out
    rows.append(_srow(30, 'adzuna', ADZ_URL, 'finance_seat_open'))               # outside 28d: out
    head = d.supply_headline(rows, now=SNOW)
    assert (head['days'], head['survivors'], head['family']) == (28, 10, 4)
    assert head['family_pct'] == 40.0
    assert (head['top_source'], head['top_source_pct']) == ('Adzuna', 75.0)
    assert d.supply_headline([], now=SNOW) == {'days': 28, 'survivors': 0, 'family': 0,
                                               'family_pct': None, 'top_source': None,
                                               'top_source_pct': None}


def test_render_supply_section_runs_in_bare_mode():
    """Smoke: the render path (st.* calls are no-ops outside `streamlit run`)."""
    d.render_supply_section(_pivot_rows() + _mix_rows(), SNOW)
    d.render_supply_section([], SNOW)
    d.render_supply_section([{'discovered_at': SNOW.isoformat(), 'source_url': ADZ_URL}], SNOW)  # legacy select


# (d) the nonprofit line needs enough accounts to judge (review 2026-09-08) --

@pytest.mark.parametrize('row,expected', [
    ({'provenance_n': 1, 'no_search_pct': 0.0}, None),        # n=1 turned the line red
    ({'provenance_n': 4, 'no_search_pct': 100.0}, None),
    ({'provenance_n': 0, 'no_search_pct': None}, None),
    ({'provenance_n': 5, 'no_search_pct': 40.0}, False),
    ({'provenance_n': 5, 'no_search_pct': 50.0}, False),      # the target is "> 50%"
    ({'provenance_n': 5, 'no_search_pct': 60.0}, True),
])
def test_judge_no_search_share_needs_five_with_provenance(row, expected):
    assert d.judge_no_search_share(row) is expected


def _threshold_lines(monkeypatch):
    lines = []
    monkeypatch.setattr(d, '_threshold_line',
                        lambda label, value, ok, hint: lines.append((label, value, ok, hint)))
    return lines


def test_render_supply_section_greys_the_nonprofit_line_when_too_few(monkeypatch):
    lines = _threshold_lines(monkeypatch)
    v = {'verify_state': 'verified'}
    d.render_supply_section([_srow(1, zi_subindustry='Libraries', account_key='lib',
                                   classified_by='search', **v)], SNOW)
    label, value, ok, hint = next(ln for ln in lines if ln[0].startswith('Nonprofits verified without search'))
    assert value == '0% (0 of 1 with provenance)' and ok is None
    assert hint == 'target > 50%; too few to judge (1 with provenance, needs 5)'
    lines.clear()
    d.render_supply_section([_srow(1, zi_subindustry='Libraries', account_key=f'lib{i}',
                                   classified_by='search', **v) for i in range(5)], SNOW)
    label, value, ok, hint = next(ln for ln in lines if ln[0].startswith('Nonprofits'))
    assert (value, ok, hint) == ('0% (0 of 5 with provenance)', False, 'target > 50%')   # five: judged, red


# (e) the scorecard's week buckets (review 2026-09-08) --------------------------

def test_scorecard_week_buckets_noise_card_keeps_its_14d_discovery_window():
    """"Auto-removed as noise (7d)" was built over a 14-day query; when the
    shared query grew to 28 days it silently counted 3-4-week-old rows swept
    by re-verify passes (live: 204 → 326). Tombstoned this week AND
    discovered within 14 days is the card's meaning."""
    assert d.SCORECARD_DAYS > d.NOISE_CARD_DISCOVERY_DAYS == 14     # the query is wider than the card
    fresh_tomb = _srow(3, blocked=True)                              # discovered + tombstoned 3d ago
    old_tomb = _srow(20, blocked=True)                               # discovered 20d ago …
    old_tomb['blocked_at'] = (SNOW - timedelta(days=2)).isoformat()  # … swept 2d ago: NOT this week's noise
    edge_tomb = _srow(13, blocked=True)                              # discovered 13d ago (inside 14d) …
    edge_tomb['blocked_at'] = (SNOW - timedelta(days=1)).isoformat()  # … tombstoned yesterday: counts
    stale_tomb = _srow(5, blocked=True)
    stale_tomb['blocked_at'] = (SNOW - timedelta(days=9)).isoformat()   # tombstoned LAST week: not counted
    live_new, live_last, live_old = _srow(2), _srow(9), _srow(21)
    bad = _srow(1)
    bad['discovered_at'] = 'not a date'                              # drops out of every bucket
    b = d.scorecard_week_buckets([fresh_tomb, old_tomb, edge_tomb, stale_tomb,
                                  live_new, live_last, live_old, bad], now=SNOW)
    assert b['this_wk'] == [fresh_tomb, stale_tomb, live_new]
    assert b['last_wk'] == [edge_tomb, live_last]
    assert b['tomb_wk'] == [fresh_tomb, edge_tomb]
    naive = d.scorecard_week_buckets([fresh_tomb], now=SNOW.replace(tzinfo=None))   # utcnow()-style caller
    assert naive['tomb_wk'] == [fresh_tomb]


def test_render_weekly_scorecard_calls_supply_section_and_week_buckets(monkeypatch):
    """Nothing asserted that the Supply block is wired into the scorecard
    (review 2026-09-08) — the call sits in a try/except that would hide a
    dropped line forever. Bare mode: st.* calls render nothing."""
    rows = _pivot_rows()
    monkeypatch.setattr(d, 'load_scorecard_events', lambda: rows)
    supply_calls, bucket_calls = [], []
    monkeypatch.setattr(d, 'render_supply_section',
                        lambda r, now=None: supply_calls.append((r, now)))
    real_buckets = d.scorecard_week_buckets
    monkeypatch.setattr(d, 'scorecard_week_buckets',
                        lambda r, now=None: bucket_calls.append(r) or real_buckets(r, now))
    d.render_weekly_scorecard(None, {})
    assert len(supply_calls) == 1 and supply_calls[0][0] is rows
    assert supply_calls[0][1].tzinfo is not None                     # one aware `now` for the whole card
    assert bucket_calls == [rows]


# the one scorecard query -----------------------------------------------------

def test_load_scorecard_rows_typed_select_first_with_ordered_paging():
    client = _FakeClient()
    assert d._load_scorecard_rows(client, {'source', 'verify_state'}, now=SNOW) == []
    assert _names(client.calls) == ['table', 'select', 'gte', 'order', 'order', 'range', 'execute']
    assert _call(client.calls, 'select')[1] == (d.SCORECARD_TYPED_COLUMNS,)
    assert 'verdict_json:fit->>verdict' in d.SCORECARD_TYPED_COLUMNS    # scalar alias, no JSONB blob
    assert 'companies_data' not in d.SCORECARD_TYPED_COLUMNS
    assert _call(client.calls, 'gte')[1] == ('discovered_at', (SNOW - timedelta(days=28)).isoformat())
    assert [c[1] for c in client.calls if c[0] == 'order'] == [('discovered_at',), ('id',)]
    assert _call(client.calls, 'range')[1] == (0, 999)


def test_load_scorecard_rows_falls_back_to_legacy_when_typed_select_fails():
    client = _FakeClient(missing={d.SCORECARD_TYPED_COLUMNS})           # the probe was stale
    assert d._load_scorecard_rows(client, {'source', 'verify_state'}) == []
    assert [c[1][0] for c in client.calls if c[0] == 'select'] == [
        d.SCORECARD_TYPED_COLUMNS, d.SCORECARD_LEGACY_COLUMNS]


def test_load_scorecard_rows_legacy_only_until_both_typed_columns_exist():
    for present in (set(), {'source'}, {'verify_state'}):
        client = _FakeClient()
        d._load_scorecard_rows(client, present)
        assert [c[1][0] for c in client.calls if c[0] == 'select'] == [d.SCORECARD_LEGACY_COLUMNS]


def test_load_scorecard_rows_pages_past_1000():
    ranges = []

    class Q:
        def __getattr__(self, name):
            def _f(*a, **kw):
                if name == 'range':
                    ranges.append(a)
                return self
            return _f

        def execute(self):
            n = 1000 if len(ranges) == 1 else 5
            return type('R', (), {'data': [{'id': i} for i in range(n)]})()

    class C:
        def table(self, name):
            return Q()

    assert len(d._load_scorecard_rows(C(), {'source', 'verify_state'})) == 1005
    assert ranges == [(0, 999), (1000, 1999)]


# ── Phase 4 (2026-09-08): accounts as the primary object ─────────────────────
# The dashboard talks to src/pipeline/accounts.py through a guarded import
# (`d._accounts`, None when the module is missing). Every test below pins
# `d._accounts` to a stub with EXACTLY the Phase 4 API, or to None for the
# legacy path, so it is deterministic whether or not the real module is
# importable. Nothing touches Supabase: clients are fakes, session_state is
# a dict.
import inspect  # noqa: E402

from src.pipeline.gates import account_key as gates_account_key  # noqa: E402


class _StubAccounts:
    """Stand-in for src.pipeline.accounts; every call is recorded."""
    ACCOUNT_STATUSES = ('Picked Up', 'On Rep TAL', 'NetSuite Customer', 'Out of Alignment', 'Not a Fit')
    DISPOSITION_REASONS = ('wrong_vertical', 'out_of_territory', 'too_big', 'too_small',
                           'not_a_trigger', 'duplicate', 'existing_customer', 'other')
    DISPOSITION_REASON_LABELS = dict(d._FALLBACK_DISPOSITION_REASON_LABELS)
    REASON_REQUIRED_STATUSES = frozenset({'Not a Fit', 'Out of Alignment'})
    REP_NOT_FIT = frozenset({'Not a Fit', 'Out of Alignment', 'NetSuite Customer'})
    REP_DECIDED = frozenset({'Picked Up', 'On Rep TAL'})
    TRIGGER_PRIORITY = ('cfo_hire', 'finance_seat_open', 'merger_acquisition', 'funding',
                        'expansion', 'executive_hire', 'stable_target', 'other')

    def __init__(self, present=True, dispositions=None, receipt=None, raise_on=None):
        self.present, self.dispositions = present, dict(dispositions or {})
        self.receipt, self.raise_on, self.calls = receipt, raise_on, []

    @staticmethod
    def account_key(name):
        return gates_account_key(name)

    def probe_accounts(self, client):
        self.calls.append(('probe',))
        if self.raise_on == 'probe':
            raise RuntimeError('probe boom')
        return self.present

    def load_dispositions(self, client):
        self.calls.append(('load',))
        if self.raise_on == 'load':
            raise RuntimeError('load boom')
        return dict(self.dispositions)

    def set_disposition(self, client, name, status, reason=None, notes=None, by=None, now=None):
        self.calls.append(('set', name, status, reason, notes))
        if self.raise_on == 'set':
            raise RuntimeError('set boom')
        if self.receipt is not None:
            return self.receipt
        if status is None:
            return f'Cleared account status for {name}'
        return f'Saved: {name} → {status}' + (f' ({reason})' if reason else '')

    # review 2026-09-08 (Phase 4): the legacy-notes encoding is owned by the
    # module; the dashboard delegates and keeps its copies for the absent path
    def encode_legacy_notes(self, reason, notes):
        self.calls.append(('encode', reason, notes))
        parts = ([f'reason={reason}'] if reason else []) + ([str(notes).strip()] if notes else [])
        return ' | '.join(parts) or None

    def decode_legacy_notes(self, notes):
        self.calls.append(('decode', notes))
        s = str(notes or '').strip()
        if not s or (isinstance(notes, float) and notes != notes):
            return None, None
        if not s.startswith('reason='):
            return None, s
        head, _sep, rest = s[len('reason='):].partition(' | ')
        return (head.strip() or None), (rest.strip() or None)


class _RowsClient:
    """A client whose every query returns `rows` (or raises `error`)."""

    def __init__(self, rows=(), error=None):
        self.rows, self.error, self.calls = list(rows), error, []

    def table(self, name):
        self.calls.append(('table', (name,), {}))
        client = self

        class Q:
            def __getattr__(q, attr):
                def _f(*a, **kw):
                    client.calls.append((attr, a, kw))
                    return q
                return _f

            def execute(q):
                client.calls.append(('execute', (), {}))
                if client.error:
                    raise client.error
                return type('R', (), {'data': list(client.rows), 'count': None})()
        return Q()


@pytest.fixture
def state(monkeypatch):
    """A plain dict standing in for st.session_state (bare mode logs a
    warning per access and the receipt is all the code under test stores)."""
    store = {}
    monkeypatch.setattr(d.st, 'session_state', store)
    return store


def _stub(monkeypatch, **kw):
    stub = _StubAccounts(**kw)
    monkeypatch.setattr(d, '_accounts', stub)
    return stub


# (1) one normalizer ----------------------------------------------------------

KEY_NAMES = [
    'Acme, Inc.', 'Acme Inc', 'ACME INC.', 'The Acme Company', 'Acme Corp.', 'Acme Corporation',
    'Acme Co.', 'Acme LLC', 'Acme, L.L.C.', 'Acme Ltd.', 'Acme Limited', 'Acme Holdings, L.P.',
    'Acme Partners LLP', 'Acme PLC', 'Acme Bank, N.A.', 'Acme Bank NA', 'Smith & Wesson',
    'Agfa-Gevaert', "O'Reilly Auto Parts", "Ben & Jerry's Homemade, Inc.", 'A1 Storage',
    'An Apple a Day, LLC', 'Acme®', 'Acme™ Robotics', 'SFA, LLC dba Acme', 'Acme  Double   Space',
    '  Acme trailing  ', 'Café Olé S.A.', 'Zorblat GmbH', 'Acme (Boston) Inc.', 'Acme/Beta Co',
    'Acme, Inc., Inc.', 'St. Mary\'s Hospital, Inc.', '', None,
]


def test_account_key_delegates_to_the_one_normalizer(monkeypatch):
    """With the accounts module present, dashboard._account_key IS
    gates.account_key — enrichment, the typed column, the accounts table
    and this file must derive the same key from the same name."""
    assert len(KEY_NAMES) >= 30
    _stub(monkeypatch)
    for name in KEY_NAMES:
        assert d._account_key(name) == gates_account_key(name), name
    # and through the real module, when it is importable
    real = pytest.importorskip('src.pipeline.accounts')
    monkeypatch.setattr(d, '_accounts', real)
    for name in KEY_NAMES:
        assert d._account_key(name) == gates_account_key(name) == real.account_key(name), name


def test_account_key_fallback_is_the_v1_normalizer_when_module_absent(monkeypatch):
    """Without the module the v1 normalizer runs — its keys are what the
    legacy account_dispositions table holds — and it deliberately differs
    from gates.account_key on hyphens, ampersands, articles and commas."""
    monkeypatch.setattr(d, '_accounts', None)
    for name in KEY_NAMES:
        assert d._account_key(name) == d._legacy_account_key(name), name
    assert d._account_key('Acme, Inc.') == 'acme'
    assert d._account_key('The Acme Company') == 'the acme'          # article kept, one suffix stripped
    assert d._account_key('Agfa-Gevaert') == 'agfa-gevaert'           # gates: 'agfa gevaert'
    assert d._account_key('Smith & Wesson') == 'smith & wesson'       # gates: 'smith and wesson'
    assert gates_account_key('Agfa-Gevaert') == 'agfa gevaert'
    assert d._account_key(None) == '' and d._account_key('') == ''


def test_fallback_vocabulary_matches_the_accounts_module():
    """The literals this file falls back on MUST equal the module's, or the
    two paths would offer reps different words."""
    real = pytest.importorskip('src.pipeline.accounts')
    assert d._FALLBACK_ACCOUNT_STATUSES == tuple(real.ACCOUNT_STATUSES)
    assert d._FALLBACK_DISPOSITION_REASONS == tuple(real.DISPOSITION_REASONS)
    assert d._FALLBACK_DISPOSITION_REASON_LABELS == dict(real.DISPOSITION_REASON_LABELS)
    assert d._FALLBACK_REASON_REQUIRED_STATUSES == frozenset(real.REASON_REQUIRED_STATUSES)
    assert list(d.ACCOUNT_STATUSES) == list(real.ACCOUNT_STATUSES)
    assert d.REASON_REQUIRED_STATUSES <= set(d.ACCOUNT_STATUSES)
    # every trigger the module ranks has a card config (incl. 'expansion')
    assert set(real.TRIGGER_PRIORITY) <= set(d.EVENT_TYPES)


def test_reason_label():
    assert d.reason_label('wrong_vertical') == 'Wrong vertical'
    assert d.reason_label('existing_customer') == 'Already a customer'
    assert d.reason_label('brand_new_code') == 'Brand new code'         # never blank for an unknown code
    assert d.reason_label('') == d.reason_label(None) == d.reason_label(NAN) == ''


# (2) dispositions: validation, receipts, module / legacy paths ---------------

@pytest.mark.parametrize('status,reason,expected', [
    (None, None, None), ('', None, None), ('—', None, None),            # clearing is always fine
    ('Picked Up', None, None), ('Picked Up', 'too_big', None),          # optional elsewhere
    ('NetSuite Customer', 'existing_customer', None),
    ('Not a Fit', 'wrong_vertical', None), ('Out of Alignment', 'other', None),
    ('Not a Fit', None, "'Not a Fit' needs a reason"),
    ('Not a Fit', '', "'Not a Fit' needs a reason"),
    ('Out of Alignment', '  ', "'Out of Alignment' needs a reason"),
    ('Bogus', None, "'Bogus' is not an account status"),
    ('Not a Fit', 'bogus', "'bogus' is not a disposition reason"),
])
def test_disposition_error_rules(status, reason, expected):
    err = d.disposition_error(status, reason)
    if expected is None:
        assert err is None
    else:
        assert err is not None and err.startswith(expected), err


def test_set_disposition_refuses_without_reason_and_writes_nothing(monkeypatch, state):
    stub = _stub(monkeypatch)
    client = _FakeClient()
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    d.set_account_disposition('Acme Inc.', 'Not a Fit')
    receipt = state['_dispo_receipt']
    assert receipt.startswith('❌') and 'Acme Inc.' in receipt and "'Not a Fit' needs a reason" in receipt
    assert 'Wrong vertical' in receipt                                    # the banner lists the choices
    assert stub.calls == [] and client.calls == []                        # nothing written anywhere
    d.set_account_disposition('Acme Inc.', 'Out of Alignment', reason='')
    assert state['_dispo_receipt'].startswith('❌') and stub.calls == []
    d.set_account_disposition('Acme Inc.', 'Not a Fit', reason='nonsense')
    assert 'is not a disposition reason' in state['_dispo_receipt'] and stub.calls == []


def test_set_disposition_writes_through_the_module_with_reason_and_notes(monkeypatch, state):
    stub = _stub(monkeypatch)
    client = _FakeClient()
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    d.set_account_disposition('Acme Inc.', 'Not a Fit', reason='wrong_vertical', notes='  call back  ')
    assert stub.calls == [('set', 'Acme Inc.', 'Not a Fit', 'wrong_vertical', 'call back')]
    assert state['_dispo_receipt'] == '✅ Saved: Acme Inc. → Not a Fit (wrong_vertical)'
    assert client.calls == []                     # the module owns both tables; no direct legacy write
    d.set_account_disposition('Acme Inc.', 'Picked Up')                 # no reason needed
    assert stub.calls[-1] == ('set', 'Acme Inc.', 'Picked Up', None, None)
    assert state['_dispo_receipt'] == '✅ Saved: Acme Inc. → Picked Up'


def test_set_disposition_clears_through_the_module(monkeypatch, state):
    stub = _stub(monkeypatch)
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _FakeClient())
    for clear in ('—', None, ''):
        d.set_account_disposition('Acme Inc.', clear, reason='too_big', notes='stale')
        assert stub.calls[-1] == ('set', 'Acme Inc.', None, None, None)   # status None = clear; extras dropped
        assert state['_dispo_receipt'] == '✅ Cleared account status for Acme Inc.'


@pytest.mark.parametrize('receipt,expected', [
    ('NOT saved — accounts: APIError: boom', '❌ NOT saved — accounts: APIError: boom'),
    ('Partly saved — written to accounts, but account_dispositions: x', '❌ Partly saved — written to accounts, but account_dispositions: x'),
    ("NOT saved — 'Not a Fit' needs a reason (Wrong vertical / …)", "❌ NOT saved — 'Not a Fit' needs a reason (Wrong vertical / …)"),
    ('Saved: Acme Inc. → Picked Up', '✅ Saved: Acme Inc. → Picked Up'),
    ('Cleared account status for Acme Inc.', '✅ Cleared account status for Acme Inc.'),
    ('✅ already marked', '✅ already marked'),
    ('', '✅ Saved: Acme Inc. → Picked Up'),                             # blank receipt = returned without raising
])
def test_set_disposition_colours_the_module_receipt_by_its_verdict(monkeypatch, state, receipt, expected):
    """accounts.set_disposition never raises for a refused write — it
    returns 'NOT saved — …' / 'Partly saved — …' — so the banner colour
    must come from the text, not from the absence of an exception."""
    _stub(monkeypatch, receipt=receipt)
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _FakeClient())
    d.set_account_disposition('Acme Inc.', 'Picked Up')
    assert state['_dispo_receipt'] == expected


def test_set_disposition_module_exception_is_a_red_receipt(monkeypatch, state):
    _stub(monkeypatch, raise_on='set')
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _FakeClient())
    d.set_account_disposition('Acme Inc.', 'Picked Up')
    assert state['_dispo_receipt'].startswith('❌') and 'RuntimeError' in state['_dispo_receipt']


def test_set_disposition_no_client_or_name(monkeypatch, state):
    _stub(monkeypatch)
    monkeypatch.setattr(d, 'get_supabase_client', lambda: None)
    d.set_account_disposition('Acme Inc.', 'Picked Up')
    assert state['_dispo_receipt'] == '❌ Account status NOT saved — no database connection'
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _FakeClient())
    d.set_account_disposition('', 'Picked Up')
    assert state['_dispo_receipt'] == '❌ Account status NOT saved — no database connection'
    d.set_account_disposition('®', 'Picked Up')                          # normalizes to nothing
    assert "couldn't derive a key" in state['_dispo_receipt']


def test_set_disposition_legacy_path_when_module_absent(monkeypatch, state):
    """No module: the legacy table alone, under the v1 key, the reason
    folded into notes (the table has no reason column) — and the reason
    rule still applies."""
    monkeypatch.setattr(d, '_accounts', None)
    client = _FakeClient()
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    d.set_account_disposition('Acme, Inc.', 'Not a Fit')
    assert state['_dispo_receipt'].startswith('❌') and client.calls == []
    d.set_account_disposition('Acme, Inc.', 'Not a Fit', reason='wrong_vertical', notes='call back')
    assert _call(client.calls, 'table')[1] == ('account_dispositions',)
    (payload,), kw = _call(client.calls, 'upsert')[1:]
    assert kw == {'on_conflict': 'company_key'}
    assert payload['company_key'] == 'acme' == d._legacy_account_key('Acme, Inc.')
    assert payload['company_name'] == 'Acme, Inc.' and payload['status'] == 'Not a Fit'
    assert payload['notes'] == 'reason=wrong_vertical | call back'
    assert state['_dispo_receipt'] == '✅ Saved: Acme, Inc. → Not a Fit (Wrong vertical)'
    client.calls.clear()
    d.set_account_disposition('Acme, Inc.', 'Picked Up')
    (payload,), _ = _call(client.calls, 'upsert')[1:]
    assert payload['notes'] is None
    client.calls.clear()
    d.set_account_disposition('Acme, Inc.', '—')
    assert _names(client.calls)[:4] == ['table', 'delete', 'in_', 'execute']
    assert _call(client.calls, 'in_')[1] == ('company_key', ['acme'])            # v1 key == pipeline key here
    assert state['_dispo_receipt'] == '✅ Cleared account status for Acme, Inc.'
    client.calls.clear()
    d.set_account_disposition('Agfa-Gevaert', None)                              # the two keys differ: both go
    assert _call(client.calls, 'in_')[1] == ('company_key', ['agfa gevaert', 'agfa-gevaert'])


LEGACY_NOTES_CASES = [
    ('too_big', 'call in Q4', 'reason=too_big | call in Q4', ('too_big', 'call in Q4')),
    ('too_big', None, 'reason=too_big', ('too_big', None)),
    ('too_big', 'a | b', 'reason=too_big | a | b', ('too_big', 'a | b')),          # ' | ' inside the notes survives
    (None, 'plain note', 'plain note', (None, 'plain note')),
    (None, 'plain | with pipe', 'plain | with pipe', (None, 'plain | with pipe')),   # no prefix: untouched
    (None, None, None, (None, None)),
]


@pytest.mark.parametrize('reason,notes,encoded,decoded', LEGACY_NOTES_CASES)
def test_legacy_notes_round_trip_fallback_copy(monkeypatch, reason, notes, encoded, decoded):
    """The module-absent copies (the only path that runs them since review
    2026-09-08 (Phase 4))."""
    monkeypatch.setattr(d, '_accounts', None)
    assert d._legacy_notes(reason, notes) == encoded
    assert d._split_legacy_notes(encoded) == decoded
    assert d._split_legacy_notes(NAN) == (None, None)


@pytest.mark.parametrize('reason,notes,encoded,decoded', LEGACY_NOTES_CASES)
def test_legacy_notes_delegate_to_the_module_when_present(monkeypatch, reason, notes, encoded, decoded):
    stub = _stub(monkeypatch)
    assert d._legacy_notes(reason, notes) == encoded
    assert d._split_legacy_notes(encoded) == decoded
    assert stub.calls == [('encode', reason, notes), ('decode', encoded)]
    # and the REAL module reads what the fallback copy wrote, and vice versa
    real = pytest.importorskip('src.pipeline.accounts')
    monkeypatch.setattr(d, '_accounts', real)
    assert d._legacy_notes(reason, notes) == encoded == real.encode_legacy_notes(reason, notes)
    assert d._split_legacy_notes(encoded) == decoded == real.decode_legacy_notes(encoded)


def test_stub_matches_the_real_accounts_module_signatures():
    """_StubAccounts pins EXACTLY the Phase 4 API: every method the dashboard
    calls must take the same parameters (name, kind, default) as the real
    module's function, minus the stub's `self`."""
    real = pytest.importorskip('src.pipeline.accounts')

    def shape(fn, drop_self=False):
        params = list(inspect.signature(fn).parameters.values())
        if drop_self:
            assert params and params[0].name == 'self'
            params = params[1:]
        return [(p.name, p.kind, p.default) for p in params]

    for name in ('set_disposition', 'load_dispositions', 'probe_accounts',
                 'encode_legacy_notes', 'decode_legacy_notes'):
        assert shape(getattr(_StubAccounts, name), drop_self=True) == shape(getattr(real, name)), name
    assert shape(_StubAccounts.account_key) == shape(real.account_key) == [('name', inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty)]
    # the dashboard's own calls fit those signatures
    src = inspect.getsource(d.set_account_disposition)
    assert '_accounts.set_disposition(client, company_name, None if clearing else status,' in src
    assert 'reason=reason, notes=notes)' in src


def test_set_disposition_end_to_end_through_the_real_module(monkeypatch, state):
    """dashboard.set_account_disposition → the REAL accounts module → a fake
    Supabase client: the reason lands on the accounts row AND inside the
    legacy row's notes, load_account_dispositions reads it back in this
    file's shape, and a clear empties both tables."""
    real = pytest.importorskip('src.pipeline.accounts')
    from tests.test_accounts import client_with_accounts
    real.reset_probe_cache()
    client = client_with_accounts(legacy=[{'company_key': 'agfa-gevaert', 'company_name': 'Agfa-Gevaert',
                                           'status': 'Picked Up', 'notes': None, 'updated_at': '2026-08-01'}])
    monkeypatch.setattr(d, '_accounts', real)
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    try:
        d.set_account_disposition('Agfa-Gevaert', 'Not a Fit', reason='wrong_vertical', notes='imaging')
        assert state['_dispo_receipt'] == '✅ Saved: Agfa-Gevaert → Not a Fit (Wrong vertical)'
        acct, = client.tables['accounts']
        assert (acct['account_key'], acct['disposition'], acct['disposition_reason'], acct['disposition_notes']) == \
            ('agfa gevaert', 'Not a Fit', 'wrong_vertical', 'imaging')
        legacy, = client.tables['account_dispositions']                     # the v1-keyed row is gone
        assert (legacy['company_key'], legacy['status'], legacy['notes']) == \
            ('agfa gevaert', 'Not a Fit', 'reason=wrong_vertical | imaging')
        out = d.load_account_dispositions()
        assert out['agfa gevaert'] == {'company_key': 'agfa gevaert', 'company_name': 'Agfa-Gevaert',
                                       'status': 'Not a Fit', 'reason': 'wrong_vertical', 'notes': 'imaging',
                                       'updated_at': acct['disposition_at']}
        assert d._dispo_for(out, 'Cleanaway') is None and d._dispo_for(out, 'Agfa-Gevaert')['reason'] == 'wrong_vertical'
        # the module-absent fallback still SEES the module-written row (pipeline key)
        monkeypatch.setattr(d, '_accounts', None)
        fb = d.load_account_dispositions()
        assert set(fb) == {'agfa gevaert'} and fb['agfa gevaert']['reason'] == 'wrong_vertical'
        assert d._account_key('Agfa-Gevaert') == 'agfa-gevaert' and d._dispo_for(fb, 'Agfa-Gevaert') is fb['agfa gevaert']
        monkeypatch.setattr(d, '_accounts', real)
        d.set_account_disposition('Agfa-Gevaert', '—')
        assert state['_dispo_receipt'] == '✅ Cleared account status for Agfa-Gevaert'
        assert client.tables['account_dispositions'] == [] and acct['disposition'] is None
        assert d.load_account_dispositions() == {}
    finally:
        real.reset_probe_cache()


def test_load_account_dispositions_is_none_when_module_is_empty_and_legacy_table_missing(monkeypatch):
    """review 2026-09-08 (Phase 4): the module returns {} both for "no
    verdicts yet" and "neither table readable"; only the second must show
    the migration banner (None)."""
    _stub(monkeypatch, dispositions={})
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _RowsClient(error=RuntimeError('no table')))
    assert d.load_account_dispositions() is None
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _RowsClient([]))
    assert d.load_account_dispositions() == {}
    rows = [{'company_key': 'acme', 'company_name': 'Acme Inc.', 'status': 'Picked Up', 'notes': None, 'updated_at': None}]
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _RowsClient(rows))
    assert d.load_account_dispositions()['acme']['status'] == 'Picked Up'  # the legacy read fills what the module missed


def test_fallback_lookups_use_both_the_v1_and_the_pipeline_key(monkeypatch):
    """review 2026-09-08 (Phase 4): rows accounts.set_disposition wrote sit
    under the pipeline key; a v1-keyed lookup alone hid them the moment the
    dashboard fell back to the module-absent path."""
    monkeypatch.setattr(d, '_accounts', None)
    assert d._dispo_keys('Agfa-Gevaert') == ['agfa-gevaert', 'agfa gevaert']
    assert d._dispo_keys('Acme, Inc.') == ['acme'] and d._dispo_keys('') == []
    acct = {'agfa gevaert': {'company_key': 'agfa gevaert', 'status': 'Not a Fit'},
            'smith & wesson': {'company_key': 'smith & wesson', 'status': 'Picked Up'}}
    assert d._dispo_for(acct, 'Agfa-Gevaert')['status'] == 'Not a Fit'          # module-written (pipeline key)
    assert d._dispo_for(acct, 'Smith & Wesson')['status'] == 'Picked Up'         # v1-written (v1 key)
    assert d._dispo_for(acct, 'Cleanaway') is None and d._dispo_for({}, 'Agfa-Gevaert') is None
    assert d._dispo_for(acct, None) is None
    _stub(monkeypatch)
    assert d._dispo_keys('Agfa-Gevaert') == ['agfa gevaert']                     # one normalizer: one key


def test_on_account_dispo_change_reads_status_reason_and_notes(monkeypatch, state):
    stub = _stub(monkeypatch)
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _FakeClient())
    state.update({'wq_acct_e1_0': 'Not a Fit', 'wq_acct_e1_0_reason': 'too_big',
                  'wq_acct_e1_0_notes': 'n'})
    d._on_account_dispo_change('wq_acct_e1_0', 'Acme')
    assert stub.calls == [('set', 'Acme', 'Not a Fit', 'too_big', 'n')]
    state.clear()
    state['k'] = 'Not a Fit'                                             # reason widget not rendered yet
    d._on_account_dispo_change('k', 'Acme')
    assert len(stub.calls) == 1                                          # refused before the module is reached
    assert state['_dispo_receipt'].startswith('❌') and "'Not a Fit' needs a reason" in state['_dispo_receipt']
    state['k_reason'] = 'wrong_vertical'                                 # the rep picks one → saved
    d._on_account_dispo_change('k', 'Acme')
    assert stub.calls[-1] == ('set', 'Acme', 'Not a Fit', 'wrong_vertical', None)
    assert state['_dispo_receipt'].startswith('✅')
    state['k'] = 'Picked Up'                                             # status moves on; the old reason is stale
    d._on_account_dispo_change('k', 'Acme')
    assert stub.calls[-1] == ('set', 'Acme', 'Picked Up', None, None)
    state['k'] = '—'                                                     # clearing drops reason + notes too
    state['k_notes'] = 'old'
    d._on_account_dispo_change('k', 'Acme')
    assert stub.calls[-1] == ('set', 'Acme', None, None, None)


def test_load_account_dispositions_uses_the_module_and_keeps_the_legacy_shape(monkeypatch):
    stub = _stub(monkeypatch, dispositions={
        'acme': {'status': 'Not a Fit', 'reason': 'too_big', 'notes': 'x', 'name': 'Acme Inc.',
                 'at': '2026-09-08T10:00:00', 'by': None, 'source': 'accounts'},
        'stale-key': {'status': 'Picked Up', 'reason': None, 'notes': None, 'name': 'Old Key, Co.',
                      'at': None},
        'nameless': {'status': 'On Rep TAL'},
    })
    client = _FakeClient()
    monkeypatch.setattr(d, 'get_supabase_client', lambda: client)
    out = d.load_account_dispositions()
    assert stub.calls == [('load',)] and client.calls == []             # the module reads, not this file
    assert out['acme'] == {'company_key': 'acme', 'company_name': 'Acme Inc.', 'status': 'Not a Fit',
                           'reason': 'too_big', 'notes': 'x', 'updated_at': '2026-09-08T10:00:00'}
    # a row keyed by something other than the normalizer of its name is re-keyed from the name
    assert 'stale-key' not in out and out['old key']['company_key'] == 'old key'
    assert out['old key']['status'] == 'Picked Up' and out['old key']['reason'] is None
    assert out['nameless'] == {'company_key': 'nameless', 'company_name': 'nameless',
                               'status': 'On Rep TAL', 'reason': None, 'notes': None,
                               'updated_at': None}
    # the rest of the dashboard reads these keys: decided-account hiding + the 7d pickup metric
    assert d._account_key('Acme Inc.') in set(out)
    assert [r for r in out.values() if r.get('status') == 'Picked Up' and r.get('updated_at')] == []


def test_load_account_dispositions_legacy_path(monkeypatch):
    monkeypatch.setattr(d, '_accounts', None)
    rows = [{'company_key': 'acme', 'company_name': 'Acme Inc.', 'status': 'Not a Fit',
             'notes': 'reason=too_big | call back', 'updated_at': '2026-09-01'},
            {'company_key': 'beta', 'company_name': 'Beta', 'status': 'Picked Up',
             'notes': 'plain', 'updated_at': None}]
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _RowsClient(rows))
    out = d.load_account_dispositions()
    assert set(out) == {'acme', 'beta'}
    assert (out['acme']['reason'], out['acme']['notes'], out['acme']['status']) == ('too_big', 'call back', 'Not a Fit')
    assert (out['beta']['reason'], out['beta']['notes']) == (None, 'plain')
    assert out['acme']['company_name'] == 'Acme Inc.' and out['acme']['updated_at'] == '2026-09-01'
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _RowsClient(error=RuntimeError('no table')))
    assert d.load_account_dispositions() is None                         # migration warning path, as before
    monkeypatch.setattr(d, 'get_supabase_client', lambda: None)
    assert d.load_account_dispositions() is None


def test_load_account_dispositions_falls_back_to_legacy_when_the_module_read_fails(monkeypatch):
    _stub(monkeypatch, raise_on='load')
    rows = [{'company_key': 'acme', 'company_name': 'Acme Inc.', 'status': 'Picked Up',
             'notes': None, 'updated_at': None}]
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _RowsClient(rows))
    assert d.load_account_dispositions()['acme']['status'] == 'Picked Up'


# (3) account cards: trigger history + account summary ----------------------

def _ev(id_, company, etype, grade, published, discovered=None, blocked=None, **extra):
    r = {'id': id_, 'company_name': company, 'event_type': etype, 'grade': grade,
         'published_date': published, 'discovered_date': discovered or published,
         'title': f'{company} {etype}', 'lead_status': 'NEW', 'fit': None,
         'companies_data': None, 'blocked_at': blocked}
    r.update(extra)
    return r


def _history_frame():
    return pd.DataFrame([
        _ev('e1', 'Acme Inc.', 'cfo_hire', 'A', '2026-09-05'),
        _ev('e2', 'Acme', 'funding', 'b', '2026-08-20', lead_status='REVIEWED - Picked Up'),
        _ev('e3', 'Acme', 'expansion', None, None, '2026-09-07T10:00:00'),
        _ev('e4', 'Acme', 'executive_hire', 'C', '2026-09-01', blocked='2026-09-02T00:00:00'),   # tombstoned
        _ev('e5', 'Acme', 'stable_target', 'D', '2026-09-01', blocked=NAN),                      # NaN = live
        _ev('e6', 'Zed Widgets', 'expansion', 'C', '2026-09-03'),
        _ev('e7', None, 'funding', 'A', '2026-09-06'),                                           # unknown company
    ])


def test_event_account_key_prefers_the_typed_column_only_with_the_module(monkeypatch):
    _stub(monkeypatch)
    assert d.event_account_key({'account_key': 'typed co', 'company_name': 'Display Co'}) == 'typed co'
    assert d.event_account_key({'account_key': NAN, 'company_name': 'Acme, Inc.'}) == 'acme'
    assert d.event_account_key({'company_name': None, 'fit': None, 'companies_data': None}) == ''
    assert d.event_account_key({'company_name': 'Unknown Company'}) == ''
    monkeypatch.setattr(d, '_accounts', None)
    # v1 normalizer for everything — the typed key (gates spelling) is ignored so one run never mixes two
    assert d.event_account_key({'account_key': 'agfa gevaert', 'company_name': 'Agfa-Gevaert'}) == 'agfa-gevaert'


def test_trigger_history_newest_first_without_tombstones_or_other_accounts(monkeypatch):
    _stub(monkeypatch)
    df = _history_frame()
    hist = d.trigger_history(df, 'acme')
    assert [h['id'] for h in hist] == ['e3', 'e1', 'e5', 'e2']            # discovered-date fallback ranks first
    assert 'e4' not in {h['id'] for h in hist}                             # blocked_at set → out
    assert [h['date'] for h in hist] == ['2026-09-07', '2026-09-05', '2026-09-01', '2026-08-20']
    assert [h['label'] for h in hist] == ['Expansion', 'CFO', 'Stable', 'Funding']
    assert hist[0]['icon'] == '🌱' and hist[0]['grade'] == '' and hist[3]['grade'] == 'B'
    assert hist[3]['lead_status'] == 'REVIEWED - Picked Up'               # classified events stay in the history
    assert hist[1]['title'] == 'Acme Inc. cfo_hire'
    annotated = d.annotate_account_keys(df)
    assert list(annotated['_account_key']) == ['acme', 'acme', 'acme', 'acme', 'acme', 'zed widgets', '']
    assert [h['id'] for h in d.trigger_history(annotated, 'acme')] == [h['id'] for h in hist]
    assert [h['id'] for h in d.trigger_history(df.to_dict('records'), 'zed widgets')] == ['e6']
    assert d.trigger_history(df, '') == [] and d.trigger_history(pd.DataFrame(), 'acme') == []
    assert d.trigger_history(None, 'acme') == []
    empty = d.annotate_account_keys(pd.DataFrame())
    assert '_account_key' in empty.columns and empty.empty


def test_account_summary_accounts_row_beats_the_event(monkeypatch):
    _stub(monkeypatch)
    row = _ev('e1', 'Acme Inc.', 'cfo_hire', 'A', '2026-09-05', numeric_score=8,
              fit={'verdict': 'pass'}, hashtags=['#NewCFO'])
    hist = d.trigger_history(_history_frame(), 'acme')
    ev = d.account_summary(row, None, hist)
    assert ev == {'name': 'Acme Inc.', 'account_key': 'acme', 'grade': 'A', 'numeric_score': 8,
                  'verify_state': 'verified', 'best_trigger_type': 'cfo_hire',
                  'best_trigger_label': 'CFO Hires', 'best_trigger_at': '2026-09-05',
                  'event_count': 4, 'hashtags': ['#NewCFO'], 'from_accounts_table': False}
    acc = {'account_key': 'acme', 'canonical_name': 'Acme', 'grade': 'B', 'numeric_score': 6,
           'verify_state': 'researched_ambiguous', 'best_trigger_type': 'funding',
           'best_trigger_at': '2026-08-20T00:00:00+00:00', 'event_count': 7,
           'hashtags': '["#Funding"]', 'disposition': None}
    got = d.account_summary(row, acc, hist)
    assert got == {'name': 'Acme', 'account_key': 'acme', 'grade': 'B', 'numeric_score': 6,
                   'verify_state': 'researched_ambiguous', 'best_trigger_type': 'funding',
                   'best_trigger_label': 'PE/VC Funding', 'best_trigger_at': '2026-08-20',
                   'event_count': 7, 'hashtags': ['#Funding'], 'from_accounts_table': True}
    # blank / NaN account fields fall back to the event, field by field
    sparse = {'account_key': 'acme', 'grade': '', 'verify_state': NAN, 'best_trigger_type': None,
              'best_trigger_at': None, 'event_count': None, 'canonical_name': ' '}
    part = d.account_summary(row, sparse, hist)
    assert (part['name'], part['grade'], part['verify_state'], part['best_trigger_type'],
            part['best_trigger_at'], part['event_count'], part['from_accounts_table']) == (
        'Acme Inc.', 'A', 'verified', 'cfo_hire', '2026-09-05', 4, True)


def test_account_summary_event_fallbacks(monkeypatch):
    _stub(monkeypatch)
    row = _ev('e9', 'Zed Co', 'expansion', None, None, '2026-09-07T10:00:00', verify_state='verified')
    s = d.account_summary(row, None)
    assert (s['grade'], s['numeric_score'], s['verify_state'], s['event_count']) == ('', -1, 'verified', 1)
    assert (s['best_trigger_label'], s['best_trigger_at']) == ('Expansion / New registration', '2026-09-07')
    staged = d.account_summary(_ev('e9', 'Zed Co', 'bogus', 'A', '2026-09-07'), None, [])
    assert (staged['verify_state'], staged['best_trigger_type'], staged['best_trigger_label'],
            staged['event_count']) == ('staged', 'bogus', 'Bogus', 0)
    assert d.account_summary(row, {'account_key': 'zed co', 'event_count': 7.0})['event_count'] == 7
    assert d.account_summary(row, {'account_key': 'zed co', 'event_count': '3'})['event_count'] == 3
    assert d.account_summary(row, {'account_key': 'zed co', 'event_count': 'x'})['event_count'] == 0
    assert d.account_summary(pd.Series(row), {}, None)['from_accounts_table'] is False


def test_account_history_html_is_one_continuous_string(monkeypatch):
    _stub(monkeypatch)
    hist = d.trigger_history(_history_frame(), 'acme')
    s = d.account_summary(_ev('e1', 'Acme <Inc>', 'cfo_hire', 'A', '2026-09-05'), None, hist)
    html = d.account_history_html(s, hist, others_new=2, max_rows=2)
    assert '\n' not in html                                               # markdown code-block trap
    assert 'Acme &lt;Inc&gt;' in html and '🗂' in html
    assert html.count('margin:2px 0 0 1.2rem') == 4                       # 2 rows + "+2 more" + "+2 NEW"
    assert '… +2 more' in html and '+2 more NEW events' in html and '4 events in window' in html
    acc_html = d.account_history_html(dict(s, from_accounts_table=True, event_count=1), [], 0)
    assert '1 event (accounts table)' in acc_html and 'margin:2px' not in acc_html


# the accounts table: probe once, degrade to the events when absent ----------

def test_probe_accounts_table_paths(monkeypatch):
    stub = _stub(monkeypatch, present=True)
    assert d._probe_accounts_table(_FakeClient()) is True and stub.calls == [('probe',)]
    assert d._probe_accounts_table(None) is False
    _stub(monkeypatch, present=False)
    assert d._probe_accounts_table(_FakeClient()) is False
    _stub(monkeypatch, raise_on='probe')
    assert d._probe_accounts_table(_FakeClient()) is False
    monkeypatch.setattr(d, '_accounts', None)
    client = _FakeClient()
    assert d._probe_accounts_table(client) is True
    assert _names(client.calls) == ['table', 'select', 'limit', 'execute']
    assert _call(client.calls, 'table')[1] == ('accounts',)
    assert d._probe_accounts_table(_FakeClient(missing={'account_key'})) is False


def test_load_accounts_by_key_chunks_dedupes_and_degrades():
    rows = [{'account_key': 'acme', 'grade': 'A'}, {'account_key': '', 'grade': 'B'}, 'junk']
    client = _RowsClient(rows)
    out = d._load_accounts_by_key(client, ['acme', 'acme', '', None, 'beta'], chunk=1)
    assert out == {'acme': {'account_key': 'acme', 'grade': 'A'}}
    ins = [c for c in client.calls if c[0] == 'in_']
    assert [c[1] for c in ins] == [('account_key', ['acme']), ('account_key', ['beta'])]
    assert all(c[1] == (d.ACCOUNTS_SELECT,) for c in client.calls if c[0] == 'select')
    assert d._load_accounts_by_key(_RowsClient(rows), []) == {}
    assert d._load_accounts_by_key(_RowsClient(error=RuntimeError('x')), ['acme']) == {}
    assert len([c for c in _RowsClient(rows).calls]) == 0


def test_load_accounts_for_never_queries_without_the_table(monkeypatch):
    class Boom:
        def table(self, name):
            raise AssertionError('must not query')
    monkeypatch.setattr(d, 'accounts_table_present', lambda: False)
    monkeypatch.setattr(d, 'get_supabase_client', lambda: Boom())
    assert d.load_accounts_for(['acme']) == {}
    monkeypatch.setattr(d, 'accounts_table_present', lambda: True)
    assert d.load_accounts_for([]) == {}
    monkeypatch.setattr(d, 'get_supabase_client', lambda: _RowsClient([{'account_key': 'acme', 'grade': 'B'}]))
    assert d.load_accounts_for(['acme'])['acme']['grade'] == 'B'


def _queue_run(monkeypatch, accounts_rows, top_n=10):
    """render_work_queue in bare mode with the card + strip captured."""
    cards, strips = [], []
    monkeypatch.setattr(d, 'render_event_card', lambda r, cfg, key_prefix='': cards.append((r['id'], key_prefix)))
    monkeypatch.setattr(d, 'render_account_history',
                        lambda s, h, others_new=0: strips.append((s, [x['id'] for x in h], others_new)))
    monkeypatch.setattr(d, 'load_accounts_for', lambda keys: {k: v for k, v in accounts_rows.items() if k in keys})
    df = _history_frame()
    live = df[df['blocked_at'].isna()]
    new = live[live['lead_status'] == 'NEW']
    d.render_work_queue(new, top_n=top_n, history_df=live)
    return cards, strips


def test_work_queue_rolls_up_per_account_key_and_shows_the_history(monkeypatch):
    _stub(monkeypatch)
    cards, strips = _queue_run(monkeypatch, {})
    assert cards == [('e7', 'wq_'), ('e1', 'wq_'), ('e6', 'wq_')]          # A (newest) → A → C; one card per account
    by = {s['account_key']: (s, ids, others) for s, ids, others in strips}
    acme, ids, others = by['acme']
    assert ids == ['e3', 'e1', 'e5', 'e2'] and others == 2                 # e3 + e5 are NEW too; e2 is classified
    assert (acme['from_accounts_table'], acme['event_count'], acme['grade']) == (False, 4, 'A')
    assert by['']  [1] == [] and by[''][2] == 0                            # the unknown company: no shared history
    assert by['zed widgets'][1] == ['e6']


def test_work_queue_uses_the_accounts_row_when_the_table_is_present(monkeypatch):
    _stub(monkeypatch)
    acc = {'acme': {'account_key': 'acme', 'canonical_name': 'Acme', 'grade': 'B',
                    'verify_state': 'verified', 'best_trigger_type': 'funding',
                    'best_trigger_at': '2026-08-20', 'event_count': 9}}
    cards, strips = _queue_run(monkeypatch, acc)
    acme = next(s for s, _, _ in strips if s['account_key'] == 'acme')
    assert (acme['from_accounts_table'], acme['grade'], acme['verify_state'],
            acme['best_trigger_label'], acme['event_count']) == (True, 'B', 'verified', 'PE/VC Funding', 9)
    monkeypatch.setattr(d, '_accounts', None)                              # legacy path: same wiring
    cards, strips = _queue_run(monkeypatch, {})
    assert [c[0] for c in cards] == ['e7', 'e1', 'e6']
    assert all(not s['from_accounts_table'] for s, _, _ in strips)
    cards, strips = _queue_run(monkeypatch, {}, top_n=1)
    assert [c[0] for c in cards] == ['e7'] and len(strips) == 1


def test_work_queue_history_df_defaults_to_the_queue_frame(monkeypatch):
    _stub(monkeypatch)
    strips = []
    monkeypatch.setattr(d, 'render_event_card', lambda r, cfg, key_prefix='': None)
    monkeypatch.setattr(d, 'render_account_history', lambda s, h, others_new=0: strips.append([x['id'] for x in h]))
    monkeypatch.setattr(d, 'load_accounts_for', lambda keys: {})
    df = _history_frame()
    new = df[df['blocked_at'].isna() & (df['lead_status'] == 'NEW')]
    d.render_work_queue(new, top_n=2)
    assert strips == [[], ['e3', 'e1', 'e5']]                              # e7 (unknown co) first; e2 (classified) is not in new_df
    d.render_work_queue(pd.DataFrame(), top_n=1)                           # "queue clear" path still fine


# (4) Scorecard: why events were removed --------------------------------------

@pytest.mark.parametrize('reason,prefix', [
    ('fit_gate: HQ out of territory (Austin, TX)', 'fit_gate'),
    ('structured:sic_out: SIC 1311 crude petroleum', 'structured'),
    ('entity_shape:fund', 'entity_shape'),
    ('industry: Mining (matched "mining")', 'industry'),
    ('no_workable_account: only advisor/investor roles', 'no_workable_account'),
    ('bad_company_name: no real company name extracted', 'bad_company_name'),
    ('board_change_only: director/board appointment', 'board_change_only'),
    ('rep:Not a Fit (Acme Inc.)', 'rep'),
    ('dismissed by rep (NOT RELEVANT)', 'rep'),
    ('bulk-dismissed by rep (NOT RELEVANT)', 'rep'),
    ('trigger_expired: 70d old cfo_hire', 'trigger_expired'),
    ('oracle_too_small: fdic est $3.3M', 'oracle_too_small'),
    ('Industry: Steel', 'industry'),
    ('', 'other'), (None, 'other'), (NAN, 'other'), (':', 'other'),
])
def test_tombstone_reason_prefix(reason, prefix):
    assert d.tombstone_reason_prefix(reason) == prefix
    assert d.tombstone_reason_label(prefix) != ''


def test_tombstone_reason_labels_cover_every_writer_prefix():
    for p in ('entity_shape', 'structured', 'fit_gate', 'industry', 'no_workable_account',
              'bad_company_name', 'board_change_only', 'rep', 'trigger_expired',
              'oracle_too_small', 'other'):
        assert p in d.TOMBSTONE_REASON_LABELS, p
    assert d.tombstone_reason_label('fit_gate') == 'Failed fit gate (territory/revenue/vertical)'   # unchanged text
    assert d.tombstone_reason_label('board_change_only') == 'Board-of-directors change only'
    assert d.tombstone_reason_label('brand_new') == 'brand_new'            # never blank


@pytest.mark.parametrize('zi,bucket', [
    ('Banking', 'Banking'), ('  Banking ', 'Banking'), ('OTHER', 'unknown'), ('other', 'unknown'),
    ('', 'unknown'), (None, 'unknown'), (NAN, 'unknown'), ('None', 'unknown'),
])
def test_removal_subindustry(zi, bucket):
    assert d.removal_subindustry(zi) == bucket


def _tomb(days_ago, reason, source, url, zi, discovered_days_ago=None, **extra):
    r = _srow(discovered_days_ago if discovered_days_ago is not None else days_ago, source, url,
              blocked=True, blocked_reason=reason, zi_subindustry=zi, **extra)
    r['blocked_at'] = (SNOW - timedelta(days=days_ago)).isoformat()
    return r


def _removal_rows():
    rows = [_tomb(1, 'fit_gate: HQ out of territory', 'adzuna', ADZ_URL, 'Banking') for _ in range(3)]
    rows += [_tomb(2, 'fit_gate: revenue Enterprise', 'adzuna', ADZ_URL, 'Insurance') for _ in range(2)]
    rows.append(_tomb(2, 'fit_gate: x', 'sec_edgar', SEC_URL, 'Banking', title='SEC 8-K Item 5.02 — Co'))
    rows.append(_tomb(3, 'entity_shape:fund', 'sec_edgar', SEC_URL, None, title='SEC Form D (Private Capital Raise) — Co'))
    rows.append(_tomb(3, 'bad_company_name: none', 'pr_newswire', PRN_URL, 'OTHER'))
    rows.append(_tomb(4, 'dismissed by rep (NOT RELEVANT)', 'adzuna', ADZ_URL, 'Libraries'))
    rows.append(_tomb(5, 'trigger_expired: 70d old', 'adzuna', ADZ_URL, 'Real Estate'))
    rows.append(_tomb(6, 'structured:sic_out: x', 'sec_edgar', SEC_URL, 'Museums & Art Galleries',
                      title='SEC 8-K Item 5.02 — Co'))
    rows.append(_tomb(2, 'trigger_expired: 90d old', 'adzuna', ADZ_URL, 'Banking', discovered_days_ago=20))  # swept old row
    rows.append(_tomb(9, 'fit_gate: y', 'adzuna', ADZ_URL, 'Banking'))                                    # last week
    rows.append(_srow(1, 'adzuna', ADZ_URL))                                                              # survivor
    return rows


def test_removal_pivot_shapes_and_counts():
    piv = d.removal_pivot(_removal_rows(), now=SNOW, top_subindustries=3)
    assert piv['days'] == 7 and piv['total'] == 11                        # the swept 20d-old row and last week's are out
    assert piv['subindustries'] == ['Banking', 'Insurance', 'Libraries', 'other', 'unknown']
    assert list(piv['by_reason'].items()) == [('fit_gate', 6), ('bad_company_name', 1), ('entity_shape', 1),
                                              ('rep', 1), ('structured', 1), ('trigger_expired', 1)]
    assert piv['by_source'] == {'Adzuna': 7, 'SEC 8-K': 2, 'PR Newswire': 1, 'SEC Form D': 1}
    assert piv['by_subindustry'] == {'Banking': 4, 'Insurance': 2, 'unknown': 2, 'Libraries': 1,
                                     'Museums & Art Galleries': 1, 'Real Estate': 1}
    first = piv['rows'][0]
    assert (first['reason'], first['source'], first['total']) == ('fit_gate', 'Adzuna', 5)
    assert first['cells'] == {'Banking': 3, 'Insurance': 2, 'Libraries': 0, 'other': 0, 'unknown': 0}
    by = {(r['reason'], r['source']): r for r in piv['rows']}
    assert by[('entity_shape', 'SEC Form D')]['cells']['unknown'] == 1     # NULL subindustry
    assert by[('bad_company_name', 'PR Newswire')]['cells']['unknown'] == 1  # 'OTHER' is not a subindustry
    assert by[('structured', 'SEC 8-K')]['cells']['other'] == 1           # outside the top 3 → folded
    assert by[('trigger_expired', 'Adzuna')]['cells']['other'] == 1
    assert by[('rep', 'Adzuna')]['cells']['Libraries'] == 1
    assert sum(r['total'] for r in piv['rows']) == piv['total']
    assert [r['total'] for r in piv['rows']] == sorted((r['total'] for r in piv['rows']), reverse=True)
    for r in piv['rows']:
        assert set(r['cells']) == set(piv['subindustries']) and sum(r['cells'].values()) == r['total']


def test_removal_pivot_default_columns_and_discovery_clause():
    rows = _removal_rows()
    piv = d.removal_pivot(rows, now=SNOW)
    assert piv['subindustries'] == ['Banking', 'Insurance', 'Libraries', 'Museums & Art Galleries',
                                    'Real Estate', 'unknown']              # ≤ 8 known: no 'other' column
    swept = d.removal_pivot(rows, now=SNOW, discovery_days=None)
    assert swept['total'] == 12 and swept['by_reason']['trigger_expired'] == 2
    # the noise card's bucket and the pivot agree by construction
    tomb_wk = d.scorecard_week_buckets(rows, now=SNOW)['tomb_wk']
    assert d.removal_pivot(tomb_wk, now=SNOW, discovery_days=None)['total'] == len(tomb_wk) == piv['total']
    assert d.removal_pivot([], now=SNOW) == {'days': 7, 'total': 0, 'subindustries': [], 'rows': [],
                                             'by_reason': {}, 'by_source': {}, 'by_subindustry': {}}
    naive = d.removal_pivot(rows, now=SNOW.replace(tzinfo=None))
    assert naive['total'] == piv['total']


def test_removal_pivot_frame_layout():
    frame = d.removal_pivot_frame(d.removal_pivot(_removal_rows(), now=SNOW, top_subindustries=3))
    assert list(frame.columns) == ['Reason', 'Source', 'Banking', 'Insurance', 'Libraries',
                                   'Other subindustries', 'Unknown / not classified', 'Total']
    assert list(frame.iloc[0]) == ['Failed fit gate (territory/revenue/vertical)', 'Adzuna', 3, 2, 0, 0, 0, 5]
    assert list(frame.iloc[-1]) == ['All reasons', '', 4, 2, 1, 2, 2, 11]
    assert len(frame) == len(set((r['reason'], r['source']) for r in
                                 d.removal_pivot(_removal_rows(), now=SNOW, top_subindustries=3)['rows'])) + 1
    empty = d.removal_pivot_frame(d.removal_pivot([], now=SNOW))
    assert list(empty.columns) == ['Reason', 'Source', 'Total'] and empty.empty


def test_render_weekly_scorecard_feeds_the_removal_section_the_noise_bucket(monkeypatch):
    rows = _removal_rows()
    monkeypatch.setattr(d, 'load_scorecard_events', lambda: rows)
    monkeypatch.setattr(d, 'render_supply_section', lambda r, now=None: None)
    calls = []
    monkeypatch.setattr(d, 'render_removal_section', lambda tomb, now=None: calls.append((tomb, now)))
    d.render_weekly_scorecard(None, {})
    assert len(calls) == 1
    tomb, now = calls[0]
    assert now.tzinfo is not None
    assert tomb == d.scorecard_week_buckets(rows, now)['tomb_wk']
    d.render_removal_section(rows, SNOW)                                   # bare mode smoke
    d.render_removal_section([], SNOW)


# (5) the 'expansion' event type ---------------------------------------------

def test_expansion_is_configured_everywhere():
    cfg = d.EVENT_TYPES['expansion']
    assert cfg['icon'] == '🌱' and cfg['label'] == 'Expansion'
    assert cfg['full_label'] == 'Expansion / New registration'
    assert cfg['badge_class'] == 'badge-expansion'
    for key in ('label', 'full_label', 'color', 'gradient', 'icon', 'badge_class', 'bg_color'):
        assert cfg.get(key), key
    assert d.event_config_for('expansion') is cfg
    assert d._trigger_label('expansion') == 'Expansion / New registration'
    assert 'expansion' in d._TRIGGER_ORDER
    src = inspect.getsource(d)
    assert '.badge-expansion {' in src                                     # the CSS class the badge uses
    # every card type except the catch-all has a New Leads tab — a type in
    # EVENT_TYPES without one renders nowhere (counted out of "Other")
    main_src = inspect.getsource(d.main)
    for etype in d.EVENT_TYPES:
        if etype != 'other':
            assert f'render_event_section(new_df, "{etype}"' in main_src, etype
    assert 'tab_expansion' in main_src and '🌱 Expansion (' in main_src


def test_expansion_leaves_the_finance_leader_family_alone():
    assert d.FINANCE_LEADER_EVENT_TYPES == {'cfo_hire', 'finance_seat_open'}
    df = pd.DataFrame({'event_type': ['expansion', 'expansion', 'cfo_hire'],
                       'hashtags': [None, ['#NewController'], None]})
    assert list(d._finance_leader_mask(df)) == [False, True, True]
    assert d._trigger_key({'event_type': 'expansion'}) == ('', 'expansion')
    assert d._trigger_key({'event_type': 'expansion', 'hashtags': ['#NewController']}) == ('fl', 'controller_tag')


def test_other_tab_still_absorbs_unknown_types_but_not_expansion():
    df = pd.DataFrame([{'id': 1, 'event_type': 'expansion', 'lead_status': 'NEW'},
                       {'id': 2, 'event_type': 'mystery', 'lead_status': 'NEW'},
                       {'id': 3, 'event_type': 'other', 'lead_status': 'NEW'}])
    tabbed = [t for t in d.EVENT_TYPES if t != 'other']
    assert 'expansion' in tabbed
    assert int((~df['event_type'].isin(tabbed)).sum()) == 2                # mystery + other, not expansion
    known_others = [t for t in d.EVENT_TYPES if t != 'other']
    assert list(df[~df['event_type'].isin(known_others)]['id']) == [2, 3]   # the "Other" section's filter
    assert list(df[df['event_type'] == 'expansion']['id']) == [1]


def test_grade_colors_are_shared_by_card_and_strip():
    assert set(d.GRADE_COLORS) == {'A', 'B', 'C', 'D'}
    assert d._grade_pill('A').startswith('<span style="background:#10b981')
    assert 'ungraded' in d._grade_pill('') and d._grade_pill('', small=False) == ''
    assert 'grade_colors = GRADE_COLORS' in inspect.getsource(d.render_event_card)
