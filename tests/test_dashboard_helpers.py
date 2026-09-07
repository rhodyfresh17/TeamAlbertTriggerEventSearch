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
    ('https://www.sec.gov/Archives/edgar/x', 'SEC EDGAR'),                 # legacy str
    ('https://www.adzuna.com/jobs/1', 'Adzuna'),
    (None, 'other'),
    ({'source': 'sec_edgar', 'source_url': 'https://example.com/a'}, 'SEC EDGAR'),
    ({'source': 'globe_newswire', 'source_url': None}, 'GlobeNewswire'),
    ({'source': 'other', 'source_url': 'https://news.example.com/a'}, 'news.example.com'),
    ({'source': None, 'source_url': 'https://www.adzuna.com/jobs/1'}, 'Adzuna'),
    ({'source_url': 'https://www.sec.gov/x'}, 'SEC EDGAR'),                 # pre-migration row
    ({}, 'other'),
])
def test_scorecard_src_accepts_url_or_row(arg, expected):
    assert d._scorecard_src(arg) == expected
