"""AccountCache (src/pipeline/cache.py) — TTLs, merge, negative-cache ladder.
All time-based tests inject `now`; nothing sleeps, nothing touches the network."""
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.pipeline.cache import (
    AccountCache, NEGATIVE_BACKOFF_DAYS, KIND_TTL_DAYS, FIELD_TTL_DAYS,
)

T0 = datetime(2026, 9, 7, 12, 0, 0)
HIT = {'results': [{'title': 'Acme Corp', 'url': 'https://acme.example'}]}


@pytest.fixture
def cache(tmp_path):
    return AccountCache(str(tmp_path / 'cache.db'))


# ── schema / coexistence ────────────────────────────────────────────────────

def test_init_creates_tables_and_leaves_other_tables_alone(tmp_path):
    db = tmp_path / 'shared.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT)')
    conn.execute("INSERT INTO events (title) VALUES ('keep me')")
    conn.commit()
    conn.close()

    AccountCache(str(db))
    AccountCache(str(db))  # idempotent

    conn = sqlite3.connect(str(db))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'search_cache', 'account_firmographics', 'negative_cache', 'events'} <= names
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
    conn.close()


# ── search cache TTL per kind ───────────────────────────────────────────────

def test_search_roundtrip_and_miss(cache):
    assert cache.get_search('acme', now=T0) is None
    cache.set_search('acme', 'firmographic', HIT, now=T0)
    assert cache.get_search('acme', now=T0) == HIT
    assert cache.get_search('acme', 'zoominfo', now=T0) is None  # kind is part of the key


@pytest.mark.parametrize('kind', sorted(KIND_TTL_DAYS))
def test_search_ttl_per_kind(cache, kind):
    ttl = KIND_TTL_DAYS[kind]
    cache.set_search('acme', kind, HIT, now=T0)
    assert cache.get_search('acme', kind, now=T0 + timedelta(days=ttl - 1)) == HIT
    assert cache.get_search('acme', kind, now=T0 + timedelta(days=ttl)) is None


def test_search_unknown_kind_defaults_to_90_days(cache):
    cache.set_search('acme', 'mystery', HIT, now=T0)
    assert cache.get_search('acme', 'mystery', now=T0 + timedelta(days=89)) == HIT
    assert cache.get_search('acme', 'mystery', now=T0 + timedelta(days=90)) is None


@pytest.mark.parametrize('empty', [None, {}, {'results': []}, {'answer': 'x'}, 'nope', []])
def test_set_search_ignores_empties(cache, empty):
    cache.set_search('acme', 'firmographic', empty, now=T0)
    assert cache.get_search('acme', now=T0) is None
    assert cache.stats()['search_cache'] == 0


def test_set_search_replaces_existing(cache):
    cache.set_search('acme', 'firmographic', HIT, now=T0)
    newer = {'results': [{'title': 'Acme Corporation'}]}
    cache.set_search('acme', 'firmographic', newer, now=T0 + timedelta(days=80))
    # Re-stamped: fresh well past the original entry's expiry.
    assert cache.get_search('acme', now=T0 + timedelta(days=100)) == newer


# ── firmographics: per-field TTL + merge ────────────────────────────────────

def test_firmographics_miss_when_empty(cache):
    assert cache.get_firmographics('acme', now=T0) is None


def test_firmographics_roundtrip(cache):
    cache.set_firmographics('acme', {'url': 'https://acme.example', 'hq': 'Boston, MA',
                                     'revenue': '$50M'}, now=T0)
    assert cache.get_firmographics('acme', now=T0) == {
        'url': 'https://acme.example', 'hq': 'Boston, MA', 'revenue': '$50M'}


@pytest.mark.parametrize('field', sorted(FIELD_TTL_DAYS))
def test_firmographics_field_ttl(cache, field):
    ttl = FIELD_TTL_DAYS[field]
    cache.set_firmographics('acme', {field: 'value'}, now=T0)
    assert cache.get_firmographics('acme', now=T0 + timedelta(days=ttl - 1)) == {field: 'value'}
    assert cache.get_firmographics('acme', now=T0 + timedelta(days=ttl)) is None


def test_firmographics_expired_fields_drop_individually(cache):
    cache.set_firmographics('acme', {'url': 'https://acme.example', 'revenue': '$50M'}, now=T0)
    later = T0 + timedelta(days=FIELD_TTL_DAYS['revenue'] + 1)
    assert cache.get_firmographics('acme', now=later) == {'url': 'https://acme.example'}


def test_firmographics_merge_keeps_per_field_stamps(cache):
    cache.set_firmographics('acme', {'url': 'https://acme.example', 'revenue': '$50M'}, now=T0)
    t1 = T0 + timedelta(days=60)
    cache.set_firmographics('acme', {'revenue': '$75M', 'size': '200'}, now=t1)
    # revenue re-stamped at t1, url still on its T0 clock, size new at t1.
    got = cache.get_firmographics('acme', now=t1 + timedelta(days=85))
    assert got == {'url': 'https://acme.example', 'revenue': '$75M', 'size': '200'}
    got = cache.get_firmographics('acme', now=t1 + timedelta(days=95))
    assert got == {'url': 'https://acme.example', 'size': '200'}


def test_firmographics_none_does_not_overwrite(cache):
    cache.set_firmographics('acme', {'url': 'https://acme.example', 'revenue': '$50M'}, now=T0)
    cache.set_firmographics('acme', {'url': None, 'revenue': None, 'hq': 'Boston, MA'},
                            now=T0 + timedelta(days=1))
    assert cache.get_firmographics('acme', now=T0 + timedelta(days=2)) == {
        'url': 'https://acme.example', 'revenue': '$50M', 'hq': 'Boston, MA'}


def test_firmographics_all_none_payload_is_noop(cache):
    cache.set_firmographics('acme', {'url': None}, now=T0)
    assert cache.get_firmographics('acme', now=T0) is None
    assert cache.stats()['account_firmographics'] == 0


def test_firmographics_expired_value_can_be_refreshed(cache):
    cache.set_firmographics('acme', {'revenue': '$50M'}, now=T0)
    t1 = T0 + timedelta(days=200)
    assert cache.get_firmographics('acme', now=t1) is None
    cache.set_firmographics('acme', {'revenue': '$60M'}, now=t1)
    assert cache.get_firmographics('acme', now=t1) == {'revenue': '$60M'}


# ── negative cache: ladder + rungs ──────────────────────────────────────────

def test_backoff_ladder_7_30_90_then_caps(cache):
    assert NEGATIVE_BACKOFF_DAYS == (7, 30, 90)
    expected = [7, 30, 90, 90]
    for i, days in enumerate(expected):
        now = T0 + timedelta(days=i * 100)
        got = cache.record_empty('acme', now=now)
        assert got == (now + timedelta(days=days)).isoformat()


def test_should_skip_respects_retry_after(cache):
    assert cache.should_skip('acme', now=T0) is False
    cache.record_empty('acme', now=T0)
    assert cache.should_skip('acme', now=T0) is True
    assert cache.should_skip('acme', now=T0 + timedelta(days=6, hours=23)) is True
    assert cache.should_skip('acme', now=T0 + timedelta(days=7)) is False


def test_should_skip_is_keyed_by_kind(cache):
    cache.record_empty('acme', kind='firmographic', now=T0)
    assert cache.should_skip('acme', kind='firmographic', now=T0) is True
    assert cache.should_skip('acme', kind='aum', now=T0) is False


def test_scrape_empty_does_not_block_paid_attempt(cache):
    cache.record_empty('acme', rung='scrape', now=T0)
    assert cache.should_skip('acme', want_paid=False, now=T0) is True
    assert cache.should_skip('acme', want_paid=True, now=T0) is False


def test_paid_empty_blocks_both(cache):
    cache.record_empty('acme', rung='paid', now=T0)
    assert cache.should_skip('acme', want_paid=False, now=T0) is True
    assert cache.should_skip('acme', want_paid=True, now=T0) is True


def test_paid_rung_is_sticky(cache):
    cache.record_empty('acme', rung='paid', now=T0)
    # A later scrape-only miss must not demote the entry back to 'scrape'.
    cache.record_empty('acme', rung='scrape', now=T0 + timedelta(days=40))
    assert cache.should_skip('acme', want_paid=True, now=T0 + timedelta(days=41)) is True


def test_scrape_then_paid_promotes(cache):
    cache.record_empty('acme', rung='scrape', now=T0)
    assert cache.should_skip('acme', want_paid=True, now=T0) is False
    cache.record_empty('acme', rung='paid', now=T0)
    assert cache.should_skip('acme', want_paid=True, now=T0) is True


def test_clear_negative_resets_ladder(cache):
    cache.record_empty('acme', now=T0)
    cache.record_empty('acme', now=T0)
    cache.clear_negative('acme')
    assert cache.should_skip('acme', now=T0) is False
    # Back to rung one after clearing.
    got = cache.record_empty('acme', now=T0)
    assert got == (T0 + timedelta(days=7)).isoformat()


def test_clear_negative_missing_is_noop(cache):
    cache.clear_negative('nobody')
    assert cache.stats()['negative_cache'] == 0


# ── stats ───────────────────────────────────────────────────────────────────

def test_stats(cache):
    assert cache.stats() == {'search_cache': 0, 'account_firmographics': 0,
                             'negative_cache': 0, 'negative_active': 0}
    cache.set_search('acme', 'firmographic', HIT, now=T0)
    cache.set_search('acme', 'aum', HIT, now=T0)
    cache.set_firmographics('acme', {'url': 'x'}, now=T0)
    cache.record_empty('globex', now=T0)                          # active at T0
    cache.record_empty('initech', now=T0 - timedelta(days=30))    # expired at T0
    s = cache.stats(now=T0)
    assert s == {'search_cache': 2, 'account_firmographics': 1,
                 'negative_cache': 2, 'negative_active': 1}


# ── fail-soft: a broken DB must never raise ─────────────────────────────────

@pytest.fixture
def broken(tmp_path):
    # A directory where the DB file should be: every connect fails.
    d = tmp_path / 'not_a_db.sqlite'
    d.mkdir()
    return AccountCache(str(d))


def test_broken_db_returns_misses_without_raising(broken):
    assert broken.get_search('acme', now=T0) is None
    broken.set_search('acme', 'firmographic', HIT, now=T0)
    assert broken.get_firmographics('acme', now=T0) is None
    broken.set_firmographics('acme', {'url': 'x'}, now=T0)
    assert broken.should_skip('acme', now=T0) is False
    assert broken.record_empty('acme', now=T0) == (T0 + timedelta(days=7)).isoformat()
    broken.clear_negative('acme')
    assert broken.stats() == {'search_cache': 0, 'account_firmographics': 0,
                              'negative_cache': 0, 'negative_active': 0}


def test_corrupt_rows_are_misses(tmp_path):
    db = str(tmp_path / 'c.db')
    cache = AccountCache(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO search_cache VALUES ('acme','firmographic','{not json', ?)",
                 (T0.isoformat(),))
    conn.execute("INSERT INTO account_firmographics VALUES ('acme','[1,2]', ?)",
                 (T0.isoformat(),))
    conn.execute("INSERT INTO negative_cache VALUES ('acme','firmographic',1,'scrape',?,'garbage')",
                 (T0.isoformat(),))
    conn.commit()
    conn.close()
    assert cache.get_search('acme', now=T0) is None
    assert cache.get_firmographics('acme', now=T0) is None
    assert cache.should_skip('acme', now=T0) is False
