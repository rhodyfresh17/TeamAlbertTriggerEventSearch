"""Yield-monitoring checks in monitor_health.py, driven by synthetic rows
(no network — Supabase fetches are monkeypatched, STATE_DIR is a scratch
dir). The acceptance test from the 2026-09-07 plan is
test_source_yield_names_quiet_feed: a feed that used to produce and now
yields nothing must raise an alert, which the old liveness-only check never
did. The review 2026-09-07 additions cover the quiet-feed memory, the
Poisson-safe quiet bar, the weekly change-driven concentration check,
per-label fetched-vs-filtered grouping, the live cache tables and ordered
paging.

Live comparison: to run the real checks against the project WITHOUT moving
the Monday baselines under state/ (every weekly check compares with what the
last run wrote), use

    venv/bin/python monitor_health.py --weekly --no-state

(review 2026-09-08: a manual --weekly used to overwrite that baseline, so
the next cron run compared against the wrong week).
"""
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import monitor_health as mh
from src.pipeline import sources

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Every check that remembers something writes under STATE_DIR; point it
    at a scratch dir so no test ever touches the repo's state/."""
    d = tmp_path / 'state'
    monkeypatch.setattr(mh, 'STATE_DIR', d)
    return d


def _row(days_ago, url, title='x', blocked=False, etype='funding', hashtags=None, now=NOW, **extra):
    r = {
        'discovered_at': (now - timedelta(days=days_ago)).isoformat(),
        'blocked_at': (now - timedelta(days=days_ago)).isoformat() if blocked else None,
        'blocked_reason': 'fit_gate:territory' if blocked else None,
        'source_url': url, 'title': title, 'event_type': etype, 'hashtags': hashtags,
    }
    r.update(extra)
    return r


ADZ = 'https://www.adzuna.com/details/1'
SEC8K = 'https://www.sec.gov/Archives/edgar/data/1/a'
PRN = 'https://www.prnewswire.com/news/a'
GN = 'https://www.google.com/url?rct=j&url=https://x.com'
PEHUB = 'https://www.pehub.com/a'


def _fresh_status(**over):
    r = {'source_name': 'Adzuna (US)', 'source_type': 'job_board', 'status': 'success',
         'events_found': 0, 'last_check': (NOW - timedelta(hours=3)).isoformat()}
    r.update(over)
    return r


@pytest.fixture
def online(monkeypatch):
    """Pretend a Supabase client exists so checks don't short-circuit."""
    monkeypatch.setattr(mh, 'get_supabase', lambda: object())


def _events(monkeypatch, rows):
    monkeypatch.setattr(mh, '_fetch_recent_events', lambda days=28, client=None: rows)


def _quiet_file():
    return json.loads((mh.STATE_DIR / mh.QUIET_STATE_FILE).read_text())


def _mix_file():
    return json.loads((mh.STATE_DIR / mh.MIX_STATE_FILE).read_text())


def _seed_mix(top_source, share_pct, checked_at='2026-08-31T11:00:00+00:00'):
    mh._state_write_json(mh.MIX_STATE_FILE, {
        'top_source': top_source, 'share_pct': share_pct, 'checked_at': checked_at})


# ── (e) source labels ────────────────────────────────────────────────────────

@pytest.mark.parametrize('row,label', [
    ({'source_url': SEC8K, 'title': 'SEC 8-K Item 5.02 (Departure/Election) — Acme Inc'}, 'SEC 8-K'),
    ({'source_url': SEC8K, 'title': 'SEC Form D (Private Capital Raise) — Acme LLC'}, 'SEC Form D'),
    ({'source_url': ADZ, 'title': 'Acme hiring: Corporate Controller'}, 'Adzuna'),
    ({'source_url': 'https://www.adzuna.ca/details/2', 'title': 'x'}, 'Adzuna'),
    ({'source_url': GN, 'title': 'Acme Names New CFO'}, 'Google News'),
    ({'source_url': 'https://news.google.com/rss/articles/CBM', 'title': 'x'}, 'Google News'),
    ({'source_url': PRN, 'title': 'x'}, 'PR Newswire'),
    ({'source_url': 'https://www.globenewswire.com/news/1', 'title': 'x'}, 'GlobeNewswire'),
    ({'source_url': 'https://www.businesswire.com/news/1', 'title': 'x'}, 'Business Wire'),
    ({'source_url': 'https://www.vcnewsdaily.com/a/b', 'title': 'x'}, 'vcnewsdaily.com'),
    ({'source_url': None, 'title': None}, 'other'),
    # typed `source` column wins over the URL heuristic once migrated
    ({'source': 'adzuna', 'source_url': GN, 'title': 'x'}, 'Adzuna'),
    ({'source': 'sec_edgar', 'source_url': '', 'title': 'SEC Form D (Private Capital Raise) — Z'}, 'SEC Form D'),
    ({'source': 'sec_edgar', 'source_url': '', 'title': 'SEC 8-K Item 1.01 — Z'}, 'SEC 8-K'),
    ({'source': 'pr_newswire', 'source_url': '', 'title': 'x'}, 'PR Newswire'),
    ({'source': 'other', 'source_url': PEHUB, 'title': 'x'}, 'pehub.com'),
])
def test_source_label(row, label):
    assert sources.source_label(row) == label


def test_source_label_host_is_capped():
    row = {'source_url': 'https://' + 'a' * 40 + '.com/x', 'title': 'x'}
    assert len(sources.source_label(row)) == sources.HOST_MAX_LEN


@pytest.mark.parametrize('label,expected', [
    ('Adzuna', True), ('SEC 8-K', True), ('SEC Form D', True), ('Google News', True),
    ('LinkedIn', True), ('pehub.com', False), ('other', False), ('', False), (None, False),
])
def test_is_canonical_label(label, expected):
    assert sources.is_canonical_label(label) is expected


def test_canonical_labels_cover_every_typed_and_feed_label():
    """Whatever the folding tables can produce must be a canonical label —
    otherwise a real feed would be treated as a 'small feed' and never WARN."""
    produced = set(sources._TYPED_LABELS.values()) | {lab for _, lab in sources._HOST_LABELS}
    produced |= {lab for _, lab in sources._FEED_LABELS}
    assert produced <= sources.CANONICAL_LABELS


@pytest.mark.parametrize('feed,label,expected', [
    ('SEC 8-K Item 5.02', 'SEC 8-K', True),
    ('SEC Form D (private raises)', 'SEC Form D', True),
    ('Adzuna (CA)', 'Adzuna', True),
    ('Adzuna (CA)', 'SEC 8-K', False),
    ('Google Alert - CFO Hires (Northeast)', 'Google News', True),
    ('PR Newswire - Personnel Announcements', 'PR Newswire', True),
    ('VC News Daily', 'vcnewsdaily.com', True),
    ('Crunchbase News', 'news.crunchbase.com', True),
    ('Nonprofit Times', 'thenonprofittimes.com', True),
    ('Business Insider', 'foxbusiness.com', False),
    ('Wealth Management', 'insurancejournal.com', False),
])
def test_feed_matches_label(feed, label, expected):
    assert sources.feed_matches_label(feed, label) is expected


@pytest.mark.parametrize('row,expected', [
    ({'event_type': 'cfo_hire'}, True),
    ({'event_type': 'finance_seat_open'}, True),
    ({'event_type': 'executive_hire', 'hashtags': ['#NewController', '#100EE']}, True),
    ({'event_type': 'executive_hire', 'hashtags': '["#NewController"]'}, True),
    ({'event_type': 'executive_hire', 'hashtags': '#Legacy #NewController'}, True),
    ({'event_type': 'executive_hire', 'hashtags': None}, False),
    ({'event_type': 'funding', 'hashtags': ['#Funding']}, False),
    ({'event_type': 'CFO_HIRE'}, True),
])
def test_finance_leader_family(row, expected):
    assert sources.finance_leader_family(row) is expected


# ── (a) source yield ─────────────────────────────────────────────────────────

def _adzuna_prior(n, start=9):
    """n Adzuna survivors spread over the prior 21d (days start..start+n-1)."""
    return [_row(d, ADZ, 'Acme hiring: Controller', etype='finance_seat_open')
            for d in range(start, start + n)]


SEC_RECENT = [_row(d, SEC8K, 'SEC 8-K Item 5.02 — Co') for d in (1, 2, 3, 10, 15)]


def test_source_yield_names_quiet_feed(monkeypatch, online):
    """Acceptance test: a feed with 14 survivors in the prior 21d and 0 in the
    last 7d is a yield alert that names it (and only it)."""
    _events(monkeypatch, _adzuna_prior(14) + SEC_RECENT)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.WARN
    assert msg.startswith('1 source(s) went quiet — used to produce, 0 survivors in the last 7 days: '
                          'Adzuna (14 in the prior 21d). Check the feed')
    assert 'SEC 8-K' not in msg


@pytest.mark.parametrize('prior_n,expected', [
    (4, mh.PASS),     # λ = 4/3 per 7d → an empty week happens ~26% of the time: luck, not signal
    (11, mh.PASS),    # one below the bar
    (12, mh.WARN),    # λ = 4 per 7d → P(0) = e^-4 ≈ 1.8%: an empty week is evidence
])
def test_source_yield_quiet_bar_is_poisson_safe(monkeypatch, online, prior_n, expected):
    assert mh.QUIET_MIN_PRIOR == 12
    _events(monkeypatch, _adzuna_prior(prior_n) + SEC_RECENT)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == expected
    assert ('went quiet' in msg) is (expected == mh.WARN)
    assert (mh.STATE_DIR / mh.QUIET_STATE_FILE).exists() is (expected == mh.WARN)
    if expected == mh.PASS:      # under the bar it is still listed — context, not an alert
        assert msg.endswith(f' · small feeds quiet: Adzuna ({prior_n}/21d)')


def test_source_yield_bare_host_never_warns_on_its_own(monkeypatch, online):
    """A bare-host bucket with a big baseline and 0 recent is listed, not alarmed."""
    rows = [_row(d, PEHUB) for d in range(8, 28)] + SEC_RECENT            # pehub.com: 20 prior, 0 recent
    rows += [_row(d, 'https://www.vcnewsdaily.com/a') for d in (9, 12, 20)]  # 3 prior: the listing bar
    rows += [_row(d, 'https://www.axios.com/a') for d in (9, 12)]           # 2 prior: below it
    _events(monkeypatch, rows)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.PASS
    assert msg.startswith('3 survivors/7d across 1 sources')
    assert msg.endswith(' · small feeds quiet: pehub.com (20/21d), vcnewsdaily.com (3/21d)')
    assert 'axios' not in msg
    assert not (mh.STATE_DIR / mh.QUIET_STATE_FILE).exists()   # nothing to remember


def test_source_yield_small_feeds_listed_on_warn_too(monkeypatch, online):
    rows = _adzuna_prior(14) + SEC_RECENT + [_row(d, PEHUB) for d in (9, 12, 20)]
    _events(monkeypatch, rows)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.WARN
    assert 'Adzuna (14 in the prior 21d)' in msg
    assert msg.endswith(' · small feeds quiet: pehub.com (3/21d)')
    assert list(_quiet_file()) == ['Adzuna']                    # bare hosts are never recorded


def test_source_yield_quiet_persists_until_recovery(monkeypatch, online):
    """The failure this rewrite exists to catch: without memory a dead feed's
    baseline drops to 0 after 28 days and the check would PASS while the feed
    stayed dead. Three simulated daily runs against one state dir."""
    # Run 1 (day 0): Adzuna had 14 survivors, newest 9 days ago, none this week.
    _events(monkeypatch, _adzuna_prior(14) + SEC_RECENT)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.WARN and 'Adzuna (14 in the prior 21d)' in msg
    assert _quiet_file() == {'Adzuna': {'since': '2026-08-29', 'prior': 14}}   # its last survivor's day

    # Run 2 (day +20): every Adzuna row has aged out of the 28d window — prior
    # is now 0 — yet the alert must keep naming it.
    later = NOW + timedelta(days=20)
    _events(monkeypatch, [_row(d, SEC8K, 'SEC 8-K Item 5.02 — Co', now=later) for d in (1, 2, 3, 10)])
    status, msg = mh.check_source_yield(now=later)
    assert status == mh.WARN
    assert '1 source(s) went quiet' in msg
    assert 'Adzuna (still quiet since 2026-08-29, was 14/21d)' in msg
    assert _quiet_file() == {'Adzuna': {'since': '2026-08-29', 'prior': 14}}   # untouched

    # Run 3 (day +21): one Adzuna survivor this week → recovered, entry cleared.
    later = NOW + timedelta(days=21)
    rows = [_row(d, SEC8K, 'SEC 8-K Item 5.02 — Co', now=later) for d in (1, 2, 3, 10)]
    rows += [_row(2, ADZ, 'Acme hiring: Controller', now=later)]
    _events(monkeypatch, rows)
    status, msg = mh.check_source_yield(now=later)
    assert status == mh.PASS
    assert msg.startswith('4 survivors/7d across 2 sources')
    assert msg.endswith(' · recovered: Adzuna (quiet since 2026-08-29)')
    assert _quiet_file() == {}

    # Run 4: nothing on record, nothing quiet → plain PASS, file left alone.
    status, msg = mh.check_source_yield(now=later)
    assert status == mh.PASS and 'recovered' not in msg


def test_source_yield_manual_clear_and_rearm(monkeypatch, online):
    """Docstring contract: a human may delete the file; the feed is re-added
    only while it still shows ≥ QUIET_MIN_PRIOR prior survivors and 0 recent."""
    _events(monkeypatch, _adzuna_prior(14) + SEC_RECENT)
    assert mh.check_source_yield(now=NOW)[0] == mh.WARN
    (mh.STATE_DIR / mh.QUIET_STATE_FILE).unlink()
    assert mh.check_source_yield(now=NOW)[0] == mh.WARN                 # still ≥ 12 prior: re-armed
    assert 'Adzuna' in _quiet_file()
    (mh.STATE_DIR / mh.QUIET_STATE_FILE).unlink()
    _events(monkeypatch, _adzuna_prior(5) + SEC_RECENT)                 # rows aging out: below the bar
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.PASS and 'went quiet' not in msg
    assert msg.endswith(' · small feeds quiet: Adzuna (5/21d)')           # listed, not alarmed
    assert not (mh.STATE_DIR / mh.QUIET_STATE_FILE).exists()             # and never re-recorded


def test_source_yield_tolerates_corrupt_state(monkeypatch, online):
    mh.STATE_DIR.mkdir()
    (mh.STATE_DIR / mh.QUIET_STATE_FILE).write_text('{not json')
    _events(monkeypatch, _adzuna_prior(14) + SEC_RECENT)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.WARN and 'Adzuna (14 in the prior 21d)' in msg
    assert _quiet_file() == {'Adzuna': {'since': '2026-08-29', 'prior': 14}}   # rewritten cleanly
    # a hand-edited entry with missing fields still reports, with '?' placeholders
    (mh.STATE_DIR / mh.QUIET_STATE_FILE).write_text('{"Adzuna": {}}')
    _events(monkeypatch, SEC_RECENT)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.WARN and 'Adzuna (still quiet since ?, was ?/21d)' in msg


def test_source_yield_pass_message(monkeypatch, online):
    rows = [_row(d, SEC8K, 'SEC 8-K Item 5.02 — Co') for d in (1, 2, 3, 4, 10, 15)]
    rows += [_row(d, ADZ, 'x hiring: Controller') for d in (1, 5, 12)]
    rows += [_row(d, PRN) for d in (2, 20)]
    rows += [_row(d, GN, blocked=True) for d in (1, 2, 3)]         # blocked rows never count as survivors
    rows += [_row(20, PEHUB)]                                       # 1 prior survivor: below every bar
    _events(monkeypatch, rows)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.PASS
    assert msg.startswith('7 survivors/7d across 3 sources')
    assert 'top: SEC 8-K 4, Adzuna 2, PR Newswire 1' in msg
    assert msg.endswith('survival rate 80% of 15 rows')


def test_source_yield_fails_when_nothing_survives_but_cron_ran(monkeypatch, online):
    rows = [_row(d, SEC8K, 'SEC 8-K — Co') for d in (10, 12)]
    rows += [_row(d, SEC8K, 'SEC 8-K — Co', blocked=True) for d in (1, 2)]
    _events(monkeypatch, rows)
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: [_fresh_status()])
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.FAIL
    assert 'nothing gets through' in msg


def test_source_yield_warns_not_fails_when_cron_is_dead(monkeypatch, online):
    rows = [_row(d, SEC8K, 'SEC 8-K — Co') for d in (10, 12)]
    _events(monkeypatch, rows)
    stale = _fresh_status(last_check=(NOW - timedelta(days=3)).isoformat())
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: [stale])
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.WARN
    assert 'Scrape freshness' in msg


def test_source_yield_offline(monkeypatch):
    monkeypatch.setattr(mh, 'get_supabase', lambda: None)
    status, msg = mh.check_source_yield(now=NOW)
    assert status == mh.WARN and 'Supabase' in msg


# ── (b) concentration (weekly, change-driven) ────────────────────────────────

def _fam_rows(adzuna_n, other_n):
    rows = [_row(i % 20 + 1, ADZ, 'Co hiring: CFO', etype='finance_seat_open') for i in range(adzuna_n)]
    urls = [SEC8K, GN, PRN]
    rows += [_row(i % 20 + 1, urls[i % 3], 'SEC 8-K Item 5.02 — Co', etype='cfo_hire') for i in range(other_n)]
    return rows


def test_concentration_first_run_warns_and_records(monkeypatch, online):
    rows = _fam_rows(10, 4) + [_row(3, SEC8K, 'SEC Form D (Private Capital Raise) — Z', etype='funding')] * 30
    _events(monkeypatch, rows)
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.WARN
    assert '71% come from one source (Adzuna)' in msg
    assert 'best trigger goes dark' in msg
    assert msg.endswith('14 events/28d across 4 sources; first weekly measurement')
    assert _mix_file() == {'top_source': 'Adzuna', 'share_pct': 71.4, 'checked_at': NOW.isoformat()}


def test_concentration_unchanged_mix_is_informational(monkeypatch, online):
    _seed_mix('Adzuna', 71.4)
    _events(monkeypatch, _fam_rows(10, 4))
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.PASS
    assert msg.startswith('Finance-leader mix unchanged: Adzuna 71% · SEC 8-K 14% · '
                          'Google News 7% · PR Newswire 7% — 14 events/28d')
    assert 'Adzuna still above 40%' in msg
    assert _mix_file()['checked_at'] == NOW.isoformat()          # baseline rolls forward every week


def test_concentration_small_drift_stays_quiet(monkeypatch, online):
    _seed_mix('Adzuna', 60.0)                                    # 60 → 71: 11 points, under the 15 bar
    _events(monkeypatch, _fam_rows(10, 4))
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.PASS and msg.startswith('Finance-leader mix unchanged')


def test_concentration_flip_warns_once(monkeypatch, online):
    _seed_mix('Google News', 55.0)
    _events(monkeypatch, _fam_rows(10, 4))
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.WARN
    assert msg.endswith('top source flipped from Google News (55%) since 2026-08-31')
    # the new mix is now the baseline: the same picture next week is PASS
    status, msg = mh.check_trigger_source_concentration(now=NOW + timedelta(days=7))
    assert status == mh.PASS and msg.startswith('Finance-leader mix unchanged')


def test_concentration_big_shift_warns(monkeypatch, online):
    _seed_mix('Adzuna', 50.0)                                    # 50 → 71: 21 points
    _events(monkeypatch, _fam_rows(10, 4))
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.WARN
    assert msg.endswith('share moved 50% → 71% since 2026-08-31')


def test_concentration_crossing_the_bar_warns(monkeypatch, online):
    _seed_mix('Adzuna', 35.0)                                    # 35 → 45: 10 points but newly over 40
    _events(monkeypatch, _fam_rows(9, 11))
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.WARN
    assert msg.endswith('crossed the 40% bar (35% → 45%) since 2026-08-31')


def test_concentration_passes_at_35_pct(monkeypatch, online):
    _seed_mix('Adzuna', 71.4)                                    # a previous WARN does not linger
    _events(monkeypatch, _fam_rows(7, 13))
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.PASS
    assert 'Adzuna 35%' in msg and '20 events/28d' in msg and 'none above 40%' in msg
    assert _mix_file()['share_pct'] == 35.0


def test_concentration_too_few_rows_leaves_state_alone(monkeypatch, online):
    _events(monkeypatch, _fam_rows(4, 0) + [_row(1, PRN)] * 50)
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.PASS and 'too few finance-leader events to judge (4)' in msg
    assert not (mh.STATE_DIR / mh.MIX_STATE_FILE).exists()


def test_concentration_ignores_blocked_rows(monkeypatch, online):
    _events(monkeypatch, _fam_rows(2, 5) + [_row(1, ADZ, etype='cfo_hire', blocked=True)] * 20)
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.PASS


def test_concentration_corrupt_state_counts_as_first_run(monkeypatch, online):
    mh.STATE_DIR.mkdir()
    (mh.STATE_DIR / mh.MIX_STATE_FILE).write_text('[1, 2]')
    _events(monkeypatch, _fam_rows(10, 4))
    status, msg = mh.check_trigger_source_concentration(now=NOW)
    assert status == mh.WARN and msg.endswith('first weekly measurement')
    assert _mix_file()['top_source'] == 'Adzuna'


# ── (c) fetched vs filtered ──────────────────────────────────────────────────

def test_fetched_vs_filtered_absent_column_notes_migration(monkeypatch):
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: [_fresh_status()])
    status, msg = mh.check_fetched_vs_filtered(now=NOW)
    assert status == mh.PASS
    assert 'items_fetched not yet migrated' in msg
    assert '002_v2_typed_columns.sql' in msg


def test_fetched_zero_on_formerly_producing_feed_warns(monkeypatch):
    status_rows = [
        _fresh_status(source_name='Adzuna (US)', items_fetched=0, events_found=0),
        _fresh_status(source_name='SEC 8-K Item 5.02', source_type='sec_edgar', items_fetched=40, events_found=9),
        _fresh_status(source_name='CoinDesk', items_fetched=30, events_found=0),      # all filtered, never produced
        _fresh_status(source_name='Hotel Dive', items_fetched=0, events_found=0,
                      last_check=(NOW - timedelta(days=100)).isoformat()),           # retired feed: ignored
    ]
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: status_rows)
    rows = [_row(d, ADZ, 'Co hiring: Controller') for d in (9, 12, 15)]
    rows += [_row(1, SEC8K, 'SEC 8-K Item 5.02 — Co')]
    _events(monkeypatch, rows)
    status, msg = mh.check_fetched_vs_filtered(now=NOW)
    assert status == mh.WARN
    assert 'likely dead, not filtered: Adzuna [Adzuna (US)]. ' in msg
    assert 'CoinDesk' not in msg.split('. ')[0]
    assert '1 producing · 1 all filtered · 1 fetched 0 (of 3 feeds' in msg
    assert '1 retired/stale feeds ignored' in msg


def test_fetched_zero_on_never_producing_feed_passes(monkeypatch):
    status_rows = [
        _fresh_status(source_name='Hotel Dive', items_fetched=0, events_found=0),
        _fresh_status(source_name='SEC 8-K Item 5.02', source_type='sec_edgar', items_fetched=40, events_found=9),
        _fresh_status(source_name='NVCA', items_fetched=None, events_found=0),        # pre-migration row
    ]
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: status_rows)
    _events(monkeypatch, [_row(1, SEC8K, 'SEC 8-K Item 5.02 — Co')])
    status, msg = mh.check_fetched_vs_filtered(now=NOW)
    assert status == mh.PASS
    assert '1 producing · 0 all filtered · 1 fetched 0 (of 3 feeds in the latest run; 1 not yet measured)' == msg


GOOGLE_ALERTS = ('Google Alert - CFO Hires (Northeast)', 'Google Alert - Controller Hires')


def _google_status(fetched_a, fetched_b):
    return [
        _fresh_status(source_name=GOOGLE_ALERTS[0], source_type='rss', items_fetched=fetched_a,
                      events_found=0),
        _fresh_status(source_name=GOOGLE_ALERTS[1], source_type='rss', items_fetched=fetched_b,
                      events_found=2 if fetched_b else 0),
        _fresh_status(source_name='SEC 8-K Item 5.02', source_type='sec_edgar', items_fetched=40, events_found=9),
    ]


GOOGLE_ROWS = [_row(d, GN, 'Acme Names New CFO') for d in (1, 3, 9)] + [_row(1, SEC8K, 'SEC 8-K Item 5.02 — Co')]


def test_fetched_one_empty_google_alert_among_siblings_is_informational(monkeypatch):
    """Both alerts fold onto 'Google News', which has survivors. One alert at 0
    while its sibling fetched is not a dead source — list it, don't alarm."""
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: _google_status(0, 12))
    _events(monkeypatch, GOOGLE_ROWS)
    status, msg = mh.check_fetched_vs_filtered(now=NOW)
    assert status == mh.PASS
    assert msg == ('2 producing · 0 all filtered · 1 fetched 0 (of 3 feeds in the latest run) '
                   '· fetched 0 while a sibling feed under the same label fetched: '
                   'Google Alert - CFO Hires (Northeast)')


def test_fetched_every_google_alert_empty_is_dead(monkeypatch):
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: _google_status(0, 0))
    _events(monkeypatch, GOOGLE_ROWS)
    status, msg = mh.check_fetched_vs_filtered(now=NOW)
    assert status == mh.WARN
    assert msg.startswith('1 source(s) returned NOTHING this run but produced survivors in the last 28 days '
                          '— likely dead, not filtered: Google News [Google Alert - CFO Hires (Northeast), '
                          'Google Alert - Controller Hires]. 1 producing · 0 all filtered · 2 fetched 0')
    assert 'sibling' not in msg


def test_fetched_vs_filtered_no_rows(monkeypatch):
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: [])
    assert mh.check_fetched_vs_filtered(now=NOW)[0] == mh.WARN
    monkeypatch.setattr(mh, '_fetch_source_status', lambda client=None: None)
    assert mh.check_fetched_vs_filtered(now=NOW)[0] == mh.WARN


# ── (d) local sqlite ─────────────────────────────────────────────────────────

def _enrichment_db(path, legacy_rows=0):
    """The tables enrichment_scout creates today (AccountCache + tavily_usage);
    `legacy_rows` adds the pre-2026-09-07 firmographic_cache table."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute('CREATE TABLE events (id TEXT)')                 # empty on purpose: expected on the Mac
        conn.execute('CREATE TABLE search_cache (account_key TEXT, kind TEXT)')
        conn.executemany('INSERT INTO search_cache VALUES (?, ?)', [('a', 'firmographic'), ('b', 'firmographic')])
        conn.execute('CREATE TABLE account_firmographics (account_key TEXT)')
        conn.execute("INSERT INTO account_firmographics VALUES ('a')")
        conn.execute('CREATE TABLE negative_cache (account_key TEXT, kind TEXT)')
        conn.execute('CREATE TABLE tavily_usage (month TEXT, calls INT)')
        conn.execute("INSERT INTO tavily_usage VALUES ('2026-09', 3)")
        if legacy_rows:
            conn.execute('CREATE TABLE firmographic_cache (k TEXT)')
            conn.executemany('INSERT INTO firmographic_cache VALUES (?)', [(str(i),) for i in range(legacy_rows)])


def test_local_sqlite_pass_with_live_cache_tables(tmp_path):
    assert mh.ENRICHMENT_TABLES == ('search_cache', 'account_firmographics', 'negative_cache', 'tavily_usage')
    db = tmp_path / 'trigger_events.db'
    _enrichment_db(db)
    status, msg = mh.check_local_sqlite(db_path=db)
    assert status == mh.PASS
    assert msg.startswith('Enrichment cache OK (2 cached searches · 1 account profiles · '
                          '0 negative-cached · 1 Tavily counter rows) — scrape DB lives in the '
                          'GitHub Actions cache by design')
    assert 'firmographic_cache' not in msg and '0 events' not in msg


def test_local_sqlite_mentions_legacy_table_only_when_present(tmp_path):
    db = tmp_path / 'trigger_events.db'
    _enrichment_db(db, legacy_rows=3)
    status, msg = mh.check_local_sqlite(db_path=db)
    assert status == mh.PASS
    assert '1 Tavily counter rows; legacy firmographic_cache still present with 3 rows, no longer read)' in msg


def test_local_sqlite_old_layout_warns_with_missing_live_tables(tmp_path):
    """A DB with only the legacy layout — the case the old check called healthy."""
    db = tmp_path / 'trigger_events.db'
    with sqlite3.connect(str(db)) as conn:
        conn.execute('CREATE TABLE firmographic_cache (k TEXT)')
        conn.execute('CREATE TABLE tavily_usage (month TEXT, calls INT)')
    status, msg = mh.check_local_sqlite(db_path=db)
    assert status == mh.WARN
    assert 'missing enrichment table(s): search_cache, account_firmographics, negative_cache —' in msg
    assert 'tavily_usage' not in msg.split('—')[0]


def test_local_sqlite_empty_file_warns(tmp_path):
    db = tmp_path / 'trigger_events.db'
    db.write_bytes(b'')
    status, msg = mh.check_local_sqlite(db_path=db)
    assert status == mh.WARN
    assert 'search_cache' in msg and 'tavily_usage' in msg


def test_local_sqlite_missing_file_warns(tmp_path):
    status, msg = mh.check_local_sqlite(db_path=tmp_path / 'nope.db')
    assert status == mh.WARN and 'not present' in msg


# ── retry backlog (typed columns) ───────────────────────────────────────────

class _FakeQuery:
    """Records the filter chain; `count` comes from the client by chain shape."""
    def __init__(self, client):
        self.client, self.chain = client, []

    def __getattr__(self, name):
        def _f(*a, **kw):
            self.chain.append((name, a))
            return self
        return _f

    def execute(self):
        return self.client.answer(self.chain)


class _FakeClient:
    def __init__(self, typed=True, counts=None):
        self.typed, self.counts = typed, counts or {}

    def table(self, name):
        return _FakeQuery(self)

    def answer(self, chain):
        names = [c[0] for c in chain]
        if ('select', ('verify_state',)) in chain and not self.typed:
            raise RuntimeError('column events.verify_state does not exist')

        class R:
            data, count = [], 0
        r = R()
        if 'or_' in names:
            r.count = self.counts.get('due', 0)
        elif 'gt' in names:
            r.count = self.counts.get('waiting', 0)
        elif 'gte' in names:
            r.count = self.counts.get('cached', 0)
        return r


def test_retry_backlog_pre_migration(monkeypatch):
    monkeypatch.setattr(mh, 'get_supabase', lambda: _FakeClient(typed=False))
    status, msg = mh.check_retry_backlog(now=NOW)
    assert status == mh.PASS and 'typed columns not migrated' in msg


def test_retry_backlog_counts(monkeypatch):
    monkeypatch.setattr(mh, 'get_supabase',
                        lambda: _FakeClient(counts={'due': 4, 'waiting': 11, 'cached': 2}))
    status, msg = mh.check_retry_backlog(now=NOW)
    assert status == mh.PASS
    assert 'Retry backlog: 4 due now · 11 waiting on backoff · 2 negative-cached after 3 attempts' in msg


# ── wiring ───────────────────────────────────────────────────────────────────

def test_run_checks_wiring():
    names = lambda mode: [n for n, _ in _check_list(mode)]  # noqa: E731
    daily, weekly = names('daily'), names('weekly')
    for n in ('Source yield (7d vs prior 21d)', 'Fetched vs filtered', 'Retry backlog'):
        assert n in daily and n in weekly
        assert daily.index(n) > daily.index('Event volume trend')
    # Monday only (review 2026-09-07): a daily WARN it cannot clear until Phase 3 is noise
    assert 'Finance-leader source mix' in weekly
    assert 'Finance-leader source mix' not in daily
    assert 'Cleanup dry-run' not in weekly
    assert not hasattr(mh, 'check_cleanup_dryrun')
    assert all(n not in names('quick') for n in ('Retry backlog', 'Fetched vs filtered'))


def _check_list(mode):
    """Read run_checks' list without executing the checks."""
    import inspect, textwrap
    src = inspect.getsource(mh.run_checks).split('results = []')[0]
    src = textwrap.dedent(src.split('"""Return list of (check_name, status, message)."""')[1])
    ns = dict(vars(mh))
    ns['mode'] = mode
    exec(src, ns)
    return ns['checks']


# ── paging ───────────────────────────────────────────────────────────────────

class _ChainQuery:
    """Records every builder call; `data` is whatever the factory returns."""
    def __init__(self, sink, data):
        self.sink, self.data, self.chain = sink, data, []

    def __getattr__(self, name):
        def _f(*a, **kw):
            self.chain.append((name, a))
            return self
        return _f

    def execute(self):
        self.sink.append(list(self.chain))

        class R:
            data = self.data
        return R()


def test_fetch_recent_events_retries_without_source_column():
    calls = []

    class Q:
        def __init__(self, cols):
            self.cols = cols

        def __getattr__(self, name):
            return lambda *a, **kw: self

        def execute(self):
            calls.append(self.cols)
            if 'source' in self.cols.split(','):
                raise RuntimeError('column events.source does not exist')

            class R:
                data = [{'source_url': ADZ}]
            return R()

    class C:
        def table(self, name):
            class T:
                def select(self_inner, cols):
                    return Q(cols)
            return T()

    rows = mh._fetch_recent_events(days=28, client=C())
    assert rows == [{'source_url': ADZ}]
    assert calls[0].endswith(',source') and 'source' not in calls[1].split(',')


def test_fetch_recent_events_orders_by_discovered_at_then_id_before_paging():
    """Review 2026-09-07: .range() without ORDER BY let concurrent enrichment
    UPDATEs reshuffle rows between pages (overlaps / skips)."""
    chains = []

    class C:
        def table(self, name):
            return _ChainQuery(chains, data=[])

    assert mh._fetch_recent_events(days=28, client=C()) == []
    names = [n for n, _ in chains[0]]
    assert names == ['select', 'gte', 'order', 'order', 'range']
    assert [a for n, a in chains[0] if n == 'order'] == [('discovered_at',), ('id',)]
    assert [a for n, a in chains[0] if n == 'range'] == [(0, 999)]


# ── Phase 3 supply checks (2026-09-08): vertical mix + finance-leader share ──
FS, NP, CS = 'Financial Services', 'Nonprofits & Organizations', 'Consumer Services'


def _vrow(days_ago, zi, key, cb=None, state='verified', blocked=False, now=NOW):
    """One row of the verified-accounts select (VERIFIED_COLUMNS)."""
    when = (now - timedelta(days=days_ago)).isoformat()
    return {'id': f'{key}-{days_ago}', 'discovered_at': when,
            'blocked_at': when if blocked else None, 'account_key': key,
            'verify_state': state, 'zi_subindustry': zi, 'classified_by': cb}


def _vrows(n, zi, prefix, start=1, cb=None):
    """`n` distinct verified accounts spread over 20 days from `start` days
    ago (start=1 → the last 28d window, start=29 → the prior one)."""
    return [_vrow(start + i % 20, zi, f'{prefix}{i}', cb) for i in range(n)]


def _verified(monkeypatch, rows):
    seen = []
    monkeypatch.setattr(mh, '_fetch_verified_accounts',
                        lambda days, client=None: seen.append(days) or rows)
    return seen


def _vmix_file():
    return json.loads((mh.STATE_DIR / mh.VERTICAL_MIX_STATE_FILE).read_text())


def _seed_vmix(mix, checked_at='2026-08-31T11:00:00+00:00'):
    """mix = {vertical: pct} as the last weekly run would have written it."""
    mh._state_write_json(mh.VERTICAL_MIX_STATE_FILE, {
        'checked_at': checked_at, 'total': 100, 'prior_total': 100,
        'mix': {v: {'n': round(p), 'pct': p} for v, p in mix.items()}})


def test_vertical_mix_dark_vertical_warns(monkeypatch, online):
    rows = _vrows(10, 'Banking', 'fs') + _vrows(4, 'Libraries', 'np')                 # last 28d
    rows += _vrows(10, 'Insurance', 'pfs', start=29) + _vrows(6, 'Real Estate', 'pcs', start=29)
    seen = _verified(monkeypatch, rows)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.WARN
    assert seen == [56]                                          # 28d + the prior 28d, one read
    assert msg.startswith('Consumer Services went dark — 0 verified accounts in the last 28d, '
                          'was 6 in the prior 28d · mix: Financial Services 10 (71%) · '
                          'Nonprofits & Organizations 4 (29%) · Consumer Services 0 (0%) — '
                          '14 verified accounts/28d (prior 28d: 16)')
    assert _vmix_file()['mix'][CS] == {'n': 0, 'pct': 0.0}      # remembered for next week's "was"
    assert _vmix_file()['dark'] == {CS: {'since': '2026-08-09', 'prior': 6}}   # newest prior CS account


def test_vertical_mix_healthy_passes_with_mix_line_and_persists_state(monkeypatch, online):
    rows = _vrows(10, 'Banking', 'fs') + _vrows(5, 'K-12 Schools', 'np') + _vrows(5, 'Real Estate', 'cs')
    rows += _vrows(8, 'Banking', 'pfs', start=29)
    _verified(monkeypatch, rows)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS
    assert msg == ('Verified accounts by vertical (28d): Financial Services 10 (50%) · '
                   'Nonprofits & Organizations 5 (25%) · Consumer Services 5 (25%) — '
                   '20 verified accounts/28d (prior 28d: 8) · nonprofits verified without '
                   'search: n/a (no provenance recorded yet)')
    assert _vmix_file() == {
        'checked_at': NOW.isoformat(), 'total': 20, 'prior_total': 8,
        'mix': {FS: {'n': 10, 'pct': 50.0}, NP: {'n': 5, 'pct': 25.0}, CS: {'n': 5, 'pct': 25.0}}}


def test_vertical_mix_thin_after_healthy_warns_once(monkeypatch, online):
    _seed_vmix({FS: 60.0, NP: 22.0, CS: 18.0})
    rows = _vrows(20, 'Banking', 'fs') + _vrows(9, 'Libraries', 'np') + _vrows(1, 'Real Estate', 'cs')
    _verified(monkeypatch, rows)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.WARN
    assert msg.startswith('Consumer Services is thin — 3% of verified accounts '
                          '(was 18% at the last weekly run, 2026-08-31) · mix: ')
    # the thin share is now the baseline: the same picture next week is context, not an alert
    status, msg = mh.check_vertical_mix(now=NOW + timedelta(days=7))
    assert status == mh.PASS
    assert msg.endswith(' · thin: Consumer Services 3%')


def test_vertical_mix_thin_on_first_run_is_context_not_alert(monkeypatch, online):
    rows = _vrows(20, 'Banking', 'fs') + _vrows(9, 'Libraries', 'np') + _vrows(1, 'Real Estate', 'cs')
    _verified(monkeypatch, rows)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS and msg.endswith(' · thin: Consumer Services 3%')


def test_vertical_mix_nonprofit_no_search_share_and_unknown_bucket(monkeypatch, online):
    rows = (_vrows(4, 'Banking', 'fs')
            + _vrows(2, 'Non-Profit & Charitable Organizations', 'np-oracle', cb='oracle')
            + _vrows(1, 'Museums & Art Galleries', 'np-search', cb='search')
            + _vrows(1, 'Membership Organizations', 'np-none')          # no provenance: outside the share
            + [_vrow(2, 'OTHER', 'mystery', cb='cache'), _vrow(3, None, 'nan-co')])
    _verified(monkeypatch, rows)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS
    assert ('Financial Services 4 (40%) · Nonprofits & Organizations 4 (40%) · '
            'Consumer Services 0 (0%) · Unknown 2 — 10 verified accounts/28d') in msg
    assert 'nonprofits verified without search: 67% (2 of 3 with provenance; target > 50%)' in msg


def test_vertical_mix_one_account_per_key_newest_event_decides(monkeypatch, online):
    rows = [_vrow(10, 'Banking', 'acme'), _vrow(2, 'Real Estate', 'acme'),   # re-verified, moved vertical
            _vrow(3, 'Banking', 'other-co'),
            _vrow(4, 'Banking', 'blocked-co', blocked=True)]                  # tombstoned: out
    _verified(monkeypatch, rows)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS
    assert ('Financial Services 1 (50%) · Nonprofits & Organizations 0 (0%) · '
            'Consumer Services 1 (50%) — 2 verified accounts/28d') in msg


def test_vertical_mix_not_measurable_before_migration(monkeypatch, online):
    _verified(monkeypatch, None)
    assert mh.check_vertical_mix(now=NOW) == (
        mh.PASS, 'Vertical mix not measurable yet (typed columns not migrated)')
    assert not (mh.STATE_DIR / mh.VERTICAL_MIX_STATE_FILE).exists()


def test_vertical_mix_offline(monkeypatch):
    monkeypatch.setattr(mh, 'get_supabase', lambda: None)
    assert mh.check_vertical_mix(now=NOW) == (mh.WARN, 'Supabase unavailable — cannot check')


def test_vertical_mix_nothing_verified_leaves_state_alone(monkeypatch, online):
    _verified(monkeypatch, [])
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS
    assert msg.startswith('Verified accounts by vertical (28d): Financial Services 0 · '
                          'Nonprofits & Organizations 0 · Consumer Services 0 — 0 verified accounts/28d')
    assert not (mh.STATE_DIR / mh.VERTICAL_MIX_STATE_FILE).exists()


def test_vertical_mix_corrupt_state_counts_as_no_memory(monkeypatch, online):
    mh.STATE_DIR.mkdir()
    (mh.STATE_DIR / mh.VERTICAL_MIX_STATE_FILE).write_text('{not json')
    rows = _vrows(20, 'Banking', 'fs') + _vrows(9, 'Libraries', 'np') + _vrows(1, 'Real Estate', 'cs')
    _verified(monkeypatch, rows)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS and 'thin: Consumer Services 3%' in msg
    assert _vmix_file()['total'] == 30


def test_fetch_verified_accounts_filters_server_side_and_pages_in_order():
    chains = []

    class C:
        def table(self, name):
            return _ChainQuery(chains, data=[])

    assert mh._fetch_verified_accounts(56, client=C()) == []
    probe, page = chains
    assert [n for n, _ in probe] == ['select', 'limit']                          # typed-column probe
    assert [n for n, _ in page] == ['select', 'gte', 'or_', 'order', 'order', 'range']
    assert [a for n, a in page if n == 'select'] == [(mh.VERIFIED_COLUMNS,)]
    assert [a for n, a in page if n == 'or_'] == [(mh.VERIFIED_FILTER,)]
    assert [a for n, a in page if n == 'order'] == [('discovered_at',), ('id',)]
    assert [a for n, a in page if n == 'range'] == [(0, 999)]


def test_fetch_verified_accounts_none_before_migration():
    class Q:
        def __getattr__(self, name):
            return lambda *a, **kw: self

        def execute(self):
            raise RuntimeError('column events.verify_state does not exist')

    class C:
        def table(self, name):
            return Q()

    assert mh._fetch_verified_accounts(56, client=C()) is None


# finance-leader share of intake ----------------------------------------------

def _share_file():
    return json.loads((mh.STATE_DIR / mh.FINANCE_SHARE_STATE_FILE).read_text())


def _seed_share(share_pct, checked_at='2026-08-31T11:00:00+00:00'):
    mh._state_write_json(mh.FINANCE_SHARE_STATE_FILE, {
        'share_pct': share_pct, 'family_n': 0, 'survivors_n': 0, 'checked_at': checked_at})


def _intake(fam_n, other_n):
    """fam_n finance-leader survivors (Adzuna open seats) + other_n funding rows."""
    rows = [_row(i % 20 + 1, ADZ, 'Co hiring: CFO', etype='finance_seat_open') for i in range(fam_n)]
    rows += [_row(i % 20 + 1, SEC8K, 'SEC Form D (Private Capital Raise) — Z') for i in range(other_n)]
    return rows


def test_finance_share_collapse_after_target_warns(monkeypatch, online):
    _seed_share(35.0)
    _events(monkeypatch, _intake(3, 22))                                         # 12%
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.WARN
    assert msg.startswith('Finance-leader triggers are 12% of survivors (3 of 25 in 28d; '
                          'target ≥ 30%) — fell from 35% on 2026-08-31, the last run on target')
    assert 'check "Source yield"' in msg
    # a pre-mark file (2026-09-08) whose own share was on target IS the mark
    assert _share_file() == {'share_pct': 12.0, 'family_n': 3, 'survivors_n': 25,
                             'checked_at': NOW.isoformat(),
                             'last_on_target': {'pct': 35.0, 'checked_at': '2026-08-31T11:00:00+00:00'}}


def test_finance_share_first_run_below_target_is_informational(monkeypatch, online):
    _events(monkeypatch, _intake(11, 39))                                        # 22%
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS
    assert msg == ('Finance-leader triggers are 22% of survivors (11 of 50 in 28d; target ≥ 30%) '
                   '— below target; Phase 3 supply is what moves it')
    assert _share_file() == {'share_pct': 22.0, 'family_n': 11, 'survivors_n': 50,
                             'checked_at': NOW.isoformat()}


def test_finance_share_on_target_says_was(monkeypatch, online):
    _seed_share(30.0)
    _events(monkeypatch, _intake(7, 13))                                         # 35%
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS
    assert msg.endswith('— on target (was 30% on 2026-08-31)')


def test_finance_share_low_without_a_prior_run_at_target_stays_pass(monkeypatch, online):
    _seed_share(20.0)                                                            # never reached 30: nothing collapsed
    _events(monkeypatch, _intake(3, 22))
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS
    assert msg.endswith('— below target (was 20% on 2026-08-31); Phase 3 supply is what moves it')


def test_finance_share_ignores_blocked_rows_and_handles_no_survivors(monkeypatch, online):
    _events(monkeypatch, [_row(1, ADZ, etype='finance_seat_open', blocked=True)] * 10)
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS and msg.startswith('No survivors in the last 28 days to measure')
    assert not (mh.STATE_DIR / mh.FINANCE_SHARE_STATE_FILE).exists()


def test_finance_share_corrupt_state_counts_as_first_run(monkeypatch, online):
    mh.STATE_DIR.mkdir()
    (mh.STATE_DIR / mh.FINANCE_SHARE_STATE_FILE).write_text('[1, 2]')
    _events(monkeypatch, _intake(3, 22))
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS and 'below target;' in msg
    assert _share_file()['share_pct'] == 12.0


def test_finance_share_offline(monkeypatch):
    monkeypatch.setattr(mh, 'get_supabase', lambda: None)
    assert mh.check_finance_leader_share(now=NOW) == (mh.WARN, 'Supabase unavailable — cannot check')


def test_run_checks_wiring_phase3_supply():
    names = lambda mode: [n for n, _ in _check_list(mode)]  # noqa: E731
    weekly = names('weekly')
    for n in ('Finance-leader share of intake', 'Vertical mix (28d vs prior 28d)'):
        assert n in weekly and n not in names('daily') and n not in names('quick')
        assert weekly.index(n) > weekly.index('Finance-leader source mix')


# ── review 2026-09-08: finance-share high-water mark ─────────────────────────

def test_finance_share_two_week_slide_warns_from_the_high_water_mark(monkeypatch, online):
    """32% → 27% → 11% never warned: 11% was only ever compared with the
    immediately previous run (27%). The most recent run at/above target is
    the baseline now, kept in the state file as last_on_target."""
    _events(monkeypatch, _intake(8, 17))                                           # 32%: on target
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS and msg.endswith('— on target')
    assert _share_file()['last_on_target'] == {'pct': 32.0, 'checked_at': NOW.isoformat()}
    _events(monkeypatch, _intake(8, 22))                                           # 27%: below, quiet
    status, msg = mh.check_finance_leader_share(now=NOW + timedelta(days=7))
    assert status == mh.PASS
    assert msg.endswith('— below target (was 32% on 2026-09-07); Phase 3 supply is what moves it')
    assert _share_file()['last_on_target'] == {'pct': 32.0, 'checked_at': NOW.isoformat()}  # carried
    _events(monkeypatch, _intake(3, 24))                                           # 11%: collapsed
    status, msg = mh.check_finance_leader_share(now=NOW + timedelta(days=14))
    assert status == mh.WARN
    assert msg.startswith('Finance-leader triggers are 11% of survivors (3 of 27 in 28d; target ≥ 30%) '
                          '— fell from 32% on 2026-09-07, the last run on target; the best trigger')
    assert _share_file()['share_pct'] == 11.1


def _seed_mark(pct, age_days, share_pct=20.0):
    mh._state_write_json(mh.FINANCE_SHARE_STATE_FILE, {
        'share_pct': share_pct, 'family_n': 0, 'survivors_n': 0,
        'checked_at': '2026-08-31T11:00:00+00:00',
        'last_on_target': {'pct': pct, 'checked_at': (NOW - timedelta(days=age_days)).isoformat()}})


@pytest.mark.parametrize('age_days,expected', [(42, mh.WARN), (43, mh.PASS)])
def test_finance_share_mark_arms_the_warn_for_six_weeks(monkeypatch, online, age_days, expected):
    _seed_mark(32.0, age_days)
    _events(monkeypatch, _intake(3, 24))                                           # 11%
    assert mh.check_finance_leader_share(now=NOW)[0] == expected


def test_finance_share_stale_mark_is_informational(monkeypatch, online):
    _seed_mark(32.0, 49)                                                           # 2026-07-20
    _events(monkeypatch, _intake(3, 24))
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS
    assert msg.endswith('— below target (was 20% on 2026-08-31; last on target 32% on 2026-07-20, '
                        'over 6 weeks ago); Phase 3 supply is what moves it')
    assert _share_file()['last_on_target']['pct'] == 32.0                          # still carried


def test_finance_share_on_target_refreshes_the_mark(monkeypatch, online):
    _seed_mark(30.0, 35)
    _events(monkeypatch, _intake(7, 13))                                           # 35%
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.PASS and msg.endswith('— on target (was 20% on 2026-08-31)')
    assert _share_file()['last_on_target'] == {'pct': 35.0, 'checked_at': NOW.isoformat()}


def test_finance_share_corrupt_mark_is_ignored(monkeypatch, online):
    for bad in ('garbage', {'pct': 'x', 'checked_at': '2026-08-31T11:00:00+00:00'},
                {'pct': 32.0, 'checked_at': 'never'}):
        mh._state_write_json(mh.FINANCE_SHARE_STATE_FILE, {
            'share_pct': 20.0, 'checked_at': '2026-08-31T11:00:00+00:00', 'last_on_target': bad})
        _events(monkeypatch, _intake(3, 24))
        status, msg = mh.check_finance_leader_share(now=NOW)
        assert status == mh.PASS and msg.endswith('(was 20% on 2026-08-31); Phase 3 supply is what moves it')
        assert 'last_on_target' not in _share_file()


# ── review 2026-09-08: dark-vertical memory (the quiet_sources.json pattern) ─

def test_vertical_mix_dark_warns_once_then_is_context_until_recovery(monkeypatch, online):
    """"Went dark" repeated every Monday while the prior-28d window drained,
    then went silent for good. Now: WARN once, "still dark since" context
    until ≥ 1 verified account, "recovered" once, cleared."""
    live = _vrows(10, 'Banking', 'fs') + _vrows(4, 'Libraries', 'np')
    _verified(monkeypatch, live + _vrows(6, 'Real Estate', 'pcs', start=29))
    assert mh.check_vertical_mix(now=NOW)[0] == mh.WARN                              # once
    assert _vmix_file()['dark'] == {CS: {'since': '2026-08-09', 'prior': 6}}
    # next Monday the prior window still holds the 6 — the old check WARNed again
    status, msg = mh.check_vertical_mix(now=NOW + timedelta(days=7))
    assert status == mh.PASS
    assert msg.endswith(' · Consumer Services still dark since 2026-08-09 (was 6/28d)')
    assert _vmix_file()['dark'] == {CS: {'since': '2026-08-09', 'prior': 6}}         # unchanged
    # a month on, CS has drained out of the prior window too (0 vs 0) — the
    # old check went silent; FS/NP keep producing so nothing else is dark
    later = NOW + timedelta(days=30)
    _verified(monkeypatch, live + [_vrow(1 + i, 'Banking', f'fs2-{i}', now=later) for i in range(10)]
              + [_vrow(1 + i, 'Libraries', f'np2-{i}', now=later) for i in range(4)])
    status, msg = mh.check_vertical_mix(now=later)
    assert status == mh.PASS
    assert 'Consumer Services 0 (0%) — 14 verified accounts/28d (prior 28d: 14)' in msg
    assert msg.endswith(' · Consumer Services still dark since 2026-08-09 (was 6/28d)')
    # recovery: one verified account → "recovered" once, then nothing
    _verified(monkeypatch, live + [_vrow(2, 'Real Estate', 'cs-back')])
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS
    assert msg.endswith(' · recovered: Consumer Services (dark since 2026-08-09)')
    assert 'dark' not in _vmix_file()
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS and 'dark' not in msg and 'recovered' not in msg


def test_vertical_mix_dark_is_recorded_even_when_nothing_verified_now(monkeypatch, online):
    _verified(monkeypatch, _vrows(6, 'Real Estate', 'pcs', start=29))              # 0 now, 6 prior
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.WARN and msg.startswith('Consumer Services went dark')
    assert _vmix_file() == {'dark': {CS: {'since': '2026-08-09', 'prior': 6}}}      # no mix to remember
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS and 'still dark since 2026-08-09' in msg


def test_vertical_mix_manual_clear_rearms_while_the_prior_window_holds(monkeypatch, online):
    rows = _vrows(10, 'Banking', 'fs') + _vrows(4, 'Libraries', 'np') + _vrows(6, 'Real Estate', 'pcs', start=29)
    _verified(monkeypatch, rows)
    assert mh.check_vertical_mix(now=NOW)[0] == mh.WARN
    f = _vmix_file()
    del f['dark']
    mh._state_write_json(mh.VERTICAL_MIX_STATE_FILE, f)
    assert mh.check_vertical_mix(now=NOW)[0] == mh.WARN                              # re-armed


def test_vertical_mix_tolerates_corrupt_dark_record(monkeypatch, online):
    mh._state_write_json(mh.VERTICAL_MIX_STATE_FILE,
                         {'dark': {CS: 'garbage', 'Bogus': {'since': '2026-01-01', 'prior': 9}}})
    _verified(monkeypatch, _vrows(10, 'Banking', 'fs') + _vrows(4, 'Libraries', 'np'))
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS
    assert msg.endswith(' · Consumer Services still dark since ? (was ?/28d)')
    assert _vmix_file()['dark'] == {CS: 'garbage'}                                   # unknown vertical dropped
    mh._state_write_json(mh.VERTICAL_MIX_STATE_FILE, {'dark': 'nonsense'})
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.PASS and 'dark' not in msg


# ── review 2026-09-08: --no-state ────────────────────────────────────────────

def test_no_state_flag_reads_but_never_writes(monkeypatch, online):
    """A manual --weekly used to overwrite the Monday baselines."""
    _seed_share(35.0)
    monkeypatch.setattr(mh, 'STATE_READ_ONLY', True)
    _events(monkeypatch, _intake(3, 22))
    status, msg = mh.check_finance_leader_share(now=NOW)
    assert status == mh.WARN and 'fell from 35% on 2026-08-31' in msg            # memory still read …
    assert _share_file()['share_pct'] == 35.0                                    # … never rewritten
    _events(monkeypatch, _adzuna_prior(14) + SEC_RECENT)
    assert mh.check_source_yield(now=NOW)[0] == mh.WARN
    assert not (mh.STATE_DIR / mh.QUIET_STATE_FILE).exists()
    _verified(monkeypatch, _vrows(6, 'Real Estate', 'pcs', start=29))
    assert mh.check_vertical_mix(now=NOW)[0] == mh.WARN
    assert not (mh.STATE_DIR / mh.VERTICAL_MIX_STATE_FILE).exists()
    mh._state_write('search_mode', 'defer')
    assert not (mh.STATE_DIR / 'search_mode').exists()


def test_no_state_flag_parses_and_defaults_off():
    assert mh.parse_args(['--weekly', '--no-state']).no_state is True
    assert mh.parse_args(['--weekly']).no_state is False
    assert mh.STATE_READ_ONLY is False                                           # the cron's default
    assert '--no-state' in mh.__doc__


# ── review 2026-09-08: the typed-column probe tells "absent" from "failed" ───

class _RaisingClient:
    def __init__(self, exc):
        self.exc = exc

    def table(self, name):
        exc = self.exc

        class Q:
            def __getattr__(self, name):
                return lambda *a, **kw: self

            def execute(self):
                raise exc
        return Q()


@pytest.mark.parametrize('text', [
    "{'code': '42703', 'details': None, 'message': 'column events.verify_state does not exist'}",
    'column events.verify_state does not exist',
])
def test_probe_absent_column_means_not_migrated(text):
    assert mh._fetch_verified_accounts(56, client=_RaisingClient(RuntimeError(text))) is None


def test_probe_other_failures_raise_probe_failed():
    with pytest.raises(mh.ProbeFailed, match='HTTP 503 upstream timeout'):
        mh._fetch_verified_accounts(56, client=_RaisingClient(RuntimeError('HTTP 503\nupstream timeout')))


def test_vertical_mix_probe_failure_warns_instead_of_not_measurable(monkeypatch, online):
    def boom(days, client=None):
        raise mh.ProbeFailed('HTTP 503 upstream timeout')
    monkeypatch.setattr(mh, '_fetch_verified_accounts', boom)
    status, msg = mh.check_vertical_mix(now=NOW)
    assert status == mh.WARN
    assert msg.startswith('Vertical mix probe failed: HTTP 503 upstream timeout')
    assert 'not measurable' not in msg


def test_retry_backlog_probe_failure_warns(monkeypatch):
    monkeypatch.setattr(mh, 'get_supabase', lambda: _RaisingClient(RuntimeError('HTTP 503')))
    status, msg = mh.check_retry_backlog(now=NOW)
    assert status == mh.WARN and msg.startswith('Retry backlog probe failed: HTTP 503')


# ── review 2026-09-08: run_oracles.sh Mattermost summary ─────────────────────
# Lives here because the wrapper belongs to the same monitor/dashboard review
# (no test module of its own). The samples are the REAL print lines of
# scripts/refresh_oracles.py and scripts/ria_trigger.py.

ORACLES_SH = Path(mh.PROJECT_DIR) / 'run_oracles.sh'

REFRESH_OK = """\
/Users/x/venv/lib/python3.9/site-packages/urllib3/__init__.py:35: NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+
  warnings.warn(
05:00:01  oracle refresh: ria, bank → /Users/x/TeamAlbertTriggerEventSearch/state/oracles.db
05:00:14  ria: downloaded IA_FIRM_SEC_Feed_09_01_2026.xml.gz (7.1 MB, 13s) — feed month 2026-09
05:01:02  ria_firm (SEC IAPD advisers): 23,812 rows (in-territory 4,102, with website 20,915) — written in 48.0s
05:01:05  bank (FDIC BankFind, index 2026-09-01): 4,512 rows (in-territory 812, with website 4,311) — written in 3.1s
05:01:05  oracle_meta: bank refreshed 2026-09-02T09:01:05+00:00 (4512 rows)
05:01:05  oracle_meta: ria refreshed 2026-09-02T09:01:02+00:00 (23812 rows)
05:01:05  refresh done
"""
TRIGGER_OK = """\
RIA trigger (sec_iapd) — APPLY — 2026-09-02T09:01:06+00:00
oracle_meta: ria refreshed 2026-09-02T09:01:02+00:00 (23812 rows)
Window: registrations 2026-07-19..2026-09-02 (45 days); territory = 14 states/provinces; band = $5M..$500M revenue proxy
Selection: scanned=23812 · outside_window=23640 · out_of_territory=120 · below_bar=40 · already_emitted=9 · to_emit=3

     crd  name                                   city/state               reg_date      RAUM   emp  est rev segment    status
--------------------------------------------------------------------------------------------------------------------------
  123456  Narrows Capital Advisors LLC           Boston, MA               2026-08-20   $120M     4    $1.2M small      emit
  123457  Errors & Omissions Advisory LLC        Hartford, CT             2026-08-18    $80M     3  $800.0K small      emit

APPLIED — emitted 3 event(s) to Supabase (source=sec_iapd, batches of 50, on_conflict=id, ignore_duplicates); ledger ria_trigger_emitted updated (3 row(s)).
"""
REFRESH_FAIL = """\
05:00:01  oracle refresh: ria, bank → /Users/x/TeamAlbertTriggerEventSearch/state/oracles.db
05:00:31  ria: FAILED — HTTPError: 503 Server Error: Service Unavailable for url: https://reports.adviserinfo.sec.gov/x.xml.gz (previous table kept)
05:00:34  bank (FDIC BankFind, index 2026-09-01): 4,512 rows (in-territory 812, with website 4,311) — written in 3.1s
05:00:34  oracle_meta: bank refreshed 2026-09-02T09:00:34+00:00 (4512 rows)
05:00:34  oracle_meta: ria refreshed 2026-08-02T09:01:02+00:00 (23650 rows)
05:00:34  refresh finished with errors: ria
"""
TRIGGER_NOTHING = """\
RIA trigger (sec_iapd) — APPLY — 2026-09-02T09:00:35+00:00
oracle_meta: ria refreshed 2026-08-02T09:01:02+00:00 (23650 rows)
Window: registrations 2026-07-19..2026-09-02 (45 days); territory = 14 states/provinces; band = $5M..$500M revenue proxy
Selection: scanned=23650 · outside_window=23600 · already_emitted=50 · to_emit=0
RIA trigger: nothing to do — 0 new in-band registrations to emit. Exit 0.
"""
TRIGGER_CRASH = """\
RIA trigger (sec_iapd) — APPLY — 2026-09-02T09:00:35+00:00
Traceback (most recent call last):
  File "/Users/x/scripts/ria_trigger.py", line 657, in <module>
    sys.exit(main())
requests.exceptions.ConnectionError: HTTPSConnectionPool(host='abc.supabase.co', port=443): Max retries exceeded
"""


def _oracle_summary(text):
    """Run the script's oracle_summary() shell function over `text`."""
    m = re.search(r'^oracle_summary\(\) \{\n.*?^\}', ORACLES_SH.read_text(), re.S | re.M)
    assert m, 'oracle_summary() is not defined in run_oracles.sh'
    r = subprocess.run(['bash', '-c', m.group(0) + '\noracle_summary'], input=text,
                       capture_output=True, text=True, check=True)
    return r.stdout.splitlines()


def test_run_oracles_sh_parses():
    assert subprocess.run(['bash', '-n', str(ORACLES_SH)], capture_output=True).returncode == 0


def test_oracle_summary_keeps_counts_selection_and_result_and_drops_meta():
    lines = _oracle_summary(REFRESH_OK + TRIGGER_OK)
    assert lines == [
        'ria_firm (SEC IAPD advisers): 23,812 rows (in-territory 4,102, with website 20,915) — written in 48.0s',
        'bank (FDIC BankFind, index 2026-09-01): 4,512 rows (in-territory 812, with website 4,311) — written in 3.1s',
        'Selection: scanned=23812 · outside_window=23640 · out_of_territory=120 · below_bar=40 · '
        'already_emitted=9 · to_emit=3',
        'APPLIED — emitted 3 event(s) to Supabase (source=sec_iapd, batches of 50, on_conflict=id, '
        'ignore_duplicates); ledger ria_trigger_emitted updated (3 row(s)).',
    ]
    assert len(lines) <= 6


def test_oracle_summary_failure_line_survives_and_leads():
    """The old `grep … | tail -4` pushed 'ria: FAILED — …' out behind the
    oracle_meta lines; the owner saw a failure post with no reason in it."""
    lines = _oracle_summary(REFRESH_FAIL + TRIGGER_NOTHING)
    assert lines[0].startswith('ria: FAILED — HTTPError: 503 Server Error')
    assert lines[1:] == [
        'bank (FDIC BankFind, index 2026-09-01): 4,512 rows (in-territory 812, with website 4,311) — written in 3.1s',
        'Selection: scanned=23650 · outside_window=23600 · already_emitted=50 · to_emit=0',
        'RIA trigger: nothing to do — 0 new in-band registrations to emit. Exit 0.',
    ]
    assert not any('oracle_meta' in ln for ln in lines)
    crash = _oracle_summary(REFRESH_OK + TRIGGER_CRASH)
    assert crash[-1].startswith('requests.exceptions.ConnectionError: HTTPSConnectionPool')


def test_oracle_summary_dry_run_and_size_cap():
    assert _oracle_summary('DRY RUN — would emit 3 event(s) (source=sec_iapd); nothing written.\n') == [
        'DRY RUN — would emit 3 event(s) (source=sec_iapd); nothing written.']
    assert len(_oracle_summary('ERROR: x\n' * 20)) == 6
    assert len(_oracle_summary('Selection: ' + 'x' * 500 + '\n')[0]) == 300


def test_run_oracles_failure_post_says_rerun_and_log_is_trimmed():
    src = ORACLES_SH.read_text()
    assert 're-run run_oracles.sh before next month — the ledger makes it idempotent' in src
    assert 'tail -2000 "$LOG"' in src and '-gt 3000' in src                     # like run_reverify.sh
    assert 'SUMMARY="$(oracle_summary < "$TMP_OUT")"' in src                    # the function is what runs


# ── 2026-09-11: consecutive enrichment transport aborts ─────────────────────
def test_enrichment_transport_check_levels(state_dir):
    assert mh.check_enrichment_transport()[0] == mh.PASS            # no file yet
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / mh.TRANSPORT_ABORT_FILE).write_text('1')
    status, msg = mh.check_enrichment_transport()
    assert status == mh.PASS and 'retries' in msg
    (state_dir / mh.TRANSPORT_ABORT_FILE).write_text('2')
    status, msg = mh.check_enrichment_transport()
    assert status == mh.WARN and '2 consecutive' in msg and 'not a missing column' in msg
    (state_dir / mh.TRANSPORT_ABORT_FILE).write_text('0')
    assert mh.check_enrichment_transport()[0] == mh.PASS


def test_run_enrichment_sh_soft_notice_on_exit_2():
    src = (Path(mh.PROJECT_DIR) / 'run_enrichment.sh').read_text()
    assert 'if [ "$RC" -eq 2 ]; then' in src
    assert 'skipped this cycle' in src and 'retries in 4h' in src
    assert 'elif [ "$RC" -ne 0 ]; then' in src                    # real failures still go red
    assert 'FAILED (exit %s)' in src
    import subprocess
    assert subprocess.run(['bash', '-n', str(Path(mh.PROJECT_DIR) / 'run_enrichment.sh')]).returncode == 0
