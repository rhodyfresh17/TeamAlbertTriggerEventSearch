"""Yield-monitoring checks in monitor_health.py, driven by synthetic rows
(no network — Supabase fetches are monkeypatched, STATE_DIR is a scratch
dir). The acceptance test from the 2026-09-07 plan is
test_source_yield_names_quiet_feed: a feed that used to produce and now
yields nothing must raise an alert, which the old liveness-only check never
did. The review 2026-09-07 additions cover the quiet-feed memory, the
Poisson-safe quiet bar, the weekly change-driven concentration check,
per-label fetched-vs-filtered grouping, the live cache tables and ordered
paging."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

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
