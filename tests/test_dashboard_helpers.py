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
