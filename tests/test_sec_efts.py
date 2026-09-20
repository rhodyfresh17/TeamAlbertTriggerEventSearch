"""SEC full-text search (EFTS) resilience: retries, the shared per-run
breaker, and the rule that an item whose content gate could not be built is
skipped rather than ingested ungated. No network — session.get is mocked and
time.sleep is recorded, never waited on.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.models import EventType
from src.scrapers import sec_scraper
from src.scrapers.sec_scraper import (
    EFTS_BACKOFF_SECONDS, EFTS_MAX_TRIES, EFTSUnavailable, FormDScraper,
    ITEM_DEFINITIONS, SECScraper,
)

CONFIG = {
    'territory': {'regions': [], 'cities': [], 'industries': [], 'excluded_industries': [],
                  'company_filters': {'exclude_public_companies': False}},
    'keywords': {'executive_hires': ['CFO'], 'mergers_acquisitions': [], 'funding_events': []},
    'scraper': {'timeout': 5, 'request_delay': 0},
    'sec_filings': {'enabled': True},
    'form_d': {'enabled': True, 'max_results': 100, 'request_sleep': 0},
}


@pytest.fixture(autouse=True)
def closed_breaker():
    """The breaker is process-wide; no test may leak an open one."""
    SECScraper.reset_efts_breaker()
    yield
    SECScraper.reset_efts_breaker()


@pytest.fixture
def sleeps(monkeypatch):
    """Record every pause instead of waiting it out."""
    waited = []
    monkeypatch.setattr(sec_scraper.time, 'sleep', lambda s: waited.append(s))
    return waited


def _resp(status=200, payload=None, bad_json=False):
    r = MagicMock()
    r.status_code = status
    if status >= 400:
        err = requests.HTTPError(f'{status} Error')
        err.response = r
        r.raise_for_status.side_effect = err
    if bad_json:
        r.json.side_effect = ValueError('not json')
    else:
        r.json.return_value = payload if payload is not None else {'hits': {'hits': []}}
    return r


def _backoffs(waited):
    return [s for s in waited if s]      # drop the zero-length polite delays


# ── _efts_get: what is retried, what is not ──────────────────────────────────

def test_5xx_is_retried_then_succeeds(sleeps):
    scraper = SECScraper(CONFIG)
    ok = {'hits': {'hits': [{'_source': {'adsh': 'a-1'}}]}}
    with patch.object(scraper.session, 'get',
                      side_effect=[_resp(500), _resp(503), _resp(200, ok)]) as get:
        assert scraper._efts_get({'q': 'x'}) == ok
    assert get.call_count == 3
    assert _backoffs(sleeps) == list(EFTS_BACKOFF_SECONDS)
    assert SECScraper._efts_breaker['open_until'] == 0.0


def test_429_connection_errors_and_bad_json_are_retried(sleeps):
    scraper = SECScraper(CONFIG)
    ok = {'hits': {'hits': []}}
    for first in (_resp(429), requests.ConnectionError('reset'), requests.Timeout('slow'),
                  _resp(200, bad_json=True)):
        with patch.object(scraper.session, 'get', side_effect=[first, _resp(200, ok)]) as get:
            assert scraper._efts_get({'q': 'x'}) == ok
        assert get.call_count == 2


def test_other_4xx_is_our_bug_raised_at_once(sleeps):
    scraper = SECScraper(CONFIG)
    with patch.object(scraper.session, 'get', return_value=_resp(400)) as get:
        with pytest.raises(requests.HTTPError):
            scraper._efts_get({'q': 'x'})
    assert get.call_count == 1
    assert _backoffs(sleeps) == []
    assert SECScraper._efts_breaker['open_until'] == 0.0      # breaker untouched


def test_non_request_errors_are_not_swallowed(sleeps):
    scraper = SECScraper(CONFIG)
    with patch.object(scraper.session, 'get', side_effect=RuntimeError('bug')) as get:
        with pytest.raises(RuntimeError):
            scraper._efts_get({'q': 'x'})
    assert get.call_count == 1


def test_exhausted_tries_open_a_breaker_shared_with_form_d(sleeps):
    eight_k, form_d = SECScraper(CONFIG), FormDScraper(CONFIG)
    with patch.object(eight_k.session, 'get', return_value=_resp(500)) as get:
        with pytest.raises(EFTSUnavailable) as exc:
            eight_k._efts_get({'q': 'x'})
    assert get.call_count == EFTS_MAX_TRIES
    assert f'HTTP 500 after {EFTS_MAX_TRIES} tries' in str(exc.value)

    # every later call — on ANY SEC scraper — skips the network
    for scraper in (eight_k, form_d):
        with patch.object(scraper.session, 'get') as get:
            with pytest.raises(EFTSUnavailable) as exc:
                scraper._efts_get({'q': 'y'})
            assert get.call_count == 0
        assert 'unavailable earlier this run' in str(exc.value)


def test_breaker_closes_again_after_its_window(sleeps):
    scraper = SECScraper(CONFIG)
    SECScraper._efts_breaker.update(open_until=sec_scraper.time.monotonic() - 1, reason='old')
    with patch.object(scraper.session, 'get', return_value=_resp(200)) as get:
        scraper._efts_get({'q': 'x'})
    assert get.call_count == 1


# ── scrape(): an outage costs one exhausted call and reports every feed ──────

def test_outage_reports_every_sec_feed_as_error_after_one_exhausted_call(sleeps):
    eight_k, form_d = SECScraper(CONFIG), FormDScraper(CONFIG)
    calls = []

    def down(*a, **kw):
        calls.append(1)
        return _resp(500)

    with patch.object(eight_k.session, 'get', side_effect=down), \
         patch.object(form_d.session, 'get', side_effect=down):
        assert eight_k.scrape() == []
        assert form_d.scrape() == []

    assert len(calls) == EFTS_MAX_TRIES                 # not tries × queries
    statuses = eight_k.source_statuses + form_d.source_statuses
    assert len(statuses) == len(ITEM_DEFINITIONS) + 1
    for st in statuses:
        assert st['status'] == 'error'
        assert st['items_fetched'] == 0 and st['events_found'] == 0
        assert 'SEC search' in st['error_message']


def test_one_transient_failure_does_not_cost_the_feed(sleeps):
    form_d = FormDScraper(CONFIG)
    hits = {'hits': {'hits': [{'_source': {'file_type': 'D'}} for _ in range(3)]}}
    with patch.object(form_d.session, 'get', side_effect=[_resp(500), _resp(200, hits)]):
        form_d.scrape()
    status = form_d.source_statuses[0]
    assert status['status'] != 'error'
    assert status['items_fetched'] == 3


# ── content gates: a FAILED prefetch skips the item; an EMPTY one fails open ─

def test_failed_prefetch_skips_the_item_instead_of_ingesting_it_ungated(sleeps):
    scraper = SECScraper(CONFIG)

    def prefetch(phrase, item_code='5.02'):
        if item_code == '5.02':
            raise EFTSUnavailable('SEC search unavailable this run (HTTP 500 after 3 tries)')
        return {'ma-1'}

    searched = []

    def search(item_code):
        searched.append(item_code)
        return [{'_source': {'adsh': 'x-1'}}]

    with patch.object(scraper, '_fetch_phrase_adsh_set', side_effect=prefetch), \
         patch.object(scraper, '_search_efts', side_effect=search), \
         patch.object(scraper, '_hit_to_event', return_value=None):
        scraper.scrape()

    by_name = {s['source_name']: s for s in scraper.source_statuses}
    skipped = by_name['SEC 8-K Item 5.02']
    assert skipped['status'] == 'error'
    assert 'content gate not built' in skipped['error_message']
    assert '5.02' not in searched                       # never fetched, so never ingested
    assert sorted(searched) == ['1.01', '2.01']
    assert by_name['SEC 8-K Item 2.01']['status'] != 'error'
    assert by_name['SEC 8-K Item 1.01']['status'] != 'error'


def test_failed_ma_prefetch_skips_only_item_101(sleeps):
    scraper = SECScraper(CONFIG)

    def prefetch(phrase, item_code='5.02'):
        if item_code == '1.01':
            raise requests.HTTPError('400 Error')
        return {'cfo-1'}

    with patch.object(scraper, '_fetch_phrase_adsh_set', side_effect=prefetch), \
         patch.object(scraper, '_search_efts', return_value=[]), \
         patch.object(scraper, '_hit_to_event', return_value=None):
        scraper.scrape()
    by_name = {s['source_name']: s for s in scraper.source_statuses}
    assert by_name['SEC 8-K Item 1.01']['status'] == 'error'
    assert by_name['SEC 8-K Item 5.02']['status'] != 'error'
    assert by_name['SEC 8-K Item 2.01']['status'] != 'error'


def test_empty_but_successful_prefetch_still_fails_open(sleeps):
    scraper = SECScraper(CONFIG)
    hit = {'_source': {'ciks': ['1234'], 'display_names': ['Acme Bancorp  (0001234) (Filer)'],
                       'file_date': '2026-09-01', 'adsh': '0001234-26-000001', 'file_type': '8-K'}}
    with patch.object(scraper, '_fetch_phrase_adsh_set', return_value=set()), \
         patch.object(scraper, '_search_efts', side_effect=lambda item: [hit] if item == '5.02' else []), \
         patch.object(scraper, '_lookup_filer_info',
                      return_value={'state': 'MA', 'sic': '6022', 'sic_desc': 'State Commercial Banks'}):
        events = scraper.scrape()
    assert scraper._prefetch_error == {}
    assert [e.event_type for e in events] == [EventType.EXECUTIVE_HIRE]
