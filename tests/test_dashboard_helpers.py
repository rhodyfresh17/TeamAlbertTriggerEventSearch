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
        'Expansion',                                                       # sec_iapd's type: not in EVENT_TYPES
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
