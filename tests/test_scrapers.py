"""Tests for the trigger event scrapers."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

import yaml

from src.models import TriggerEvent, EventType, EventSource
from src.scrapers.base import BaseScraper
from src.scrapers.rss_scraper import RSSScraper
from src.scrapers.news_scraper import GoogleNewsScraper
from src.scrapers.adzuna_scraper import AdzunaScraper
from src.database import DatabaseManager


class TestBaseScraper(unittest.TestCase):
    """Tests for BaseScraper functionality."""

    def setUp(self):
        """Set up test config."""
        self.config = {
            'territory': {
                'regions': ['New York', 'Massachusetts', 'Boston'],
                'cities': ['NYC', 'Boston'],
                'industries': ['Healthcare', 'Hospital', 'Insurance'],
                'excluded_industries': ['Bank', 'Banking'],
                'company_filters': {
                    'exclude_public_companies': True,
                    'public_company_indicators': ['NYSE', 'NASDAQ', 'publicly traded'],
                    'excluded_public_companies': ['Boston Scientific', 'Johnson & Johnson'],
                }
            },
            'keywords': {
                'executive_hires': ['CFO', 'Chief Financial Officer', 'named CFO'],
                'mergers_acquisitions': ['acquisition', 'acquired', 'merger'],
                'funding_events': ['series A', 'funding round', 'raises'],
            },
            'scraper': {
                'timeout': 30,
                'request_delay': 1,
            }
        }

    def test_detect_cfo_hire(self):
        """Test CFO hire detection."""
        scraper = RSSScraper(self.config)

        # Should detect CFO hire
        text = "Acme Corp names John Smith as new CFO"
        event_type = scraper.detect_event_type(text)
        self.assertEqual(event_type, EventType.CFO_HIRE)

        # Should detect Chief Financial Officer
        text = "Jane Doe appointed Chief Financial Officer at HealthCo"
        event_type = scraper.detect_event_type(text)
        self.assertEqual(event_type, EventType.CFO_HIRE)

    def test_detect_acquisition(self):
        """Test M&A detection."""
        scraper = RSSScraper(self.config)

        text = "TechCorp announces acquisition of StartupXYZ"
        event_type = scraper.detect_event_type(text)
        self.assertEqual(event_type, EventType.MERGER_ACQUISITION)

    def test_detect_funding(self):
        """Test funding event detection."""
        scraper = RSSScraper(self.config)

        text = "HealthStart raises $50M in Series A funding"
        event_type = scraper.detect_event_type(text)
        self.assertEqual(event_type, EventType.FUNDING)

    def test_territory_matching(self):
        """Test territory matching."""
        scraper = RSSScraper(self.config)

        # Should match region (matched_regions are returned lowercased —
        # BaseScraper lowercases the configured regions at init)
        in_territory, regions = scraper.matches_territory("Company based in New York announces...")
        self.assertTrue(in_territory)
        self.assertIn('new york', regions)

        # Should match city
        in_territory, regions = scraper.matches_territory("Boston-based startup raises funds")
        self.assertTrue(in_territory)

        # Should not match
        in_territory, regions = scraper.matches_territory("California company expands")
        self.assertFalse(in_territory)

    def test_industry_matching(self):
        """Test industry matching."""
        scraper = RSSScraper(self.config)

        # Should match target industry
        matches, excluded = scraper.matches_industry("Healthcare provider announces new CFO")
        self.assertTrue(matches)
        self.assertFalse(excluded)

        # Should detect excluded industry
        matches, excluded = scraper.matches_industry("First National Bank appoints new CFO")
        self.assertTrue(excluded)

    def test_public_company_filtering(self):
        """Test public company filtering."""
        scraper = RSSScraper(self.config)

        # Should detect public company indicators
        self.assertTrue(scraper.is_public_company("Company listed on NYSE announces..."))
        self.assertTrue(scraper.is_public_company("NASDAQ: ACME reports earnings"))

        # Should detect known public companies
        self.assertTrue(scraper.is_public_company("Boston Scientific acquires startup"))
        self.assertTrue(scraper.is_public_company("Johnson & Johnson announces new division"))

        # Should not flag private companies
        self.assertFalse(scraper.is_public_company("Private healthcare company expands"))

    def test_company_name_extraction(self):
        """Test company name extraction."""
        scraper = RSSScraper(self.config)

        # Should extract company name
        name = scraper.extract_company_name("Acme Healthcare Inc. announces new CFO")
        self.assertIsNotNone(name)
        self.assertIn("Acme", name)

    def test_relevance_scoring(self):
        """Test relevance score calculation."""
        scraper = RSSScraper(self.config)

        # CFO hire with territory match should score high
        score = scraper.calculate_relevance_score(
            EventType.CFO_HIRE,
            matched_regions=['New York'],
            matches_industry=True,
            matches_company=False
        )
        self.assertGreater(score, 50)

        # Event with target company match should score very high
        score = scraper.calculate_relevance_score(
            EventType.CFO_HIRE,
            matched_regions=['Boston'],
            matches_industry=True,
            matches_company=True
        )
        self.assertGreater(score, 90)


class TestRSSScraper(unittest.TestCase):
    """Tests for RSS scraper."""

    def setUp(self):
        """Set up test config."""
        self.config = {
            'territory': {
                'regions': ['New York'],
                'cities': ['NYC'],
                'industries': ['Healthcare'],
                'excluded_industries': ['Bank'],
                'company_filters': {
                    'exclude_public_companies': False,
                }
            },
            'keywords': {
                'executive_hires': ['CFO'],
                'mergers_acquisitions': ['acquisition'],
                'funding_events': ['funding'],
            },
            'sources': {
                'rss_feeds': [
                    {'name': 'Test Feed', 'url': 'https://example.com/feed', 'enabled': True}
                ]
            },
            'scraper': {
                'timeout': 30,
                'request_delay': 0,
            }
        }

    def test_scraper_initialization(self):
        """Test scraper initializes correctly."""
        scraper = RSSScraper(self.config)
        self.assertEqual(len(scraper.feeds), 1)
        self.assertEqual(scraper.feeds[0]['name'], 'Test Feed')

    @patch('requests.Session.get')
    def test_scrape_handles_network_error(self, mock_get):
        """Test scraper handles network errors gracefully."""
        mock_get.side_effect = Exception("Network error")

        scraper = RSSScraper(self.config)
        events = scraper.scrape()

        # Should return empty list, not raise
        self.assertEqual(events, [])


class TestGoogleNewsScraper(unittest.TestCase):
    """Tests for Google News scraper."""

    def setUp(self):
        """Set up test config."""
        self.config = {
            'territory': {
                'regions': ['New York', 'Boston', 'Toronto'],
                'cities': [],
                'industries': ['Healthcare', 'Insurance'],
                'excluded_industries': [],
                'company_filters': {
                    'exclude_public_companies': False,
                }
            },
            'keywords': {
                'executive_hires': ['CFO'],
                'mergers_acquisitions': ['acquisition'],
                'funding_events': ['funding'],
            },
            'sources': {
                'google_news': {'enabled': True}
            },
            'scraper': {
                'timeout': 30,
                'request_delay': 0,
            }
        }

    def test_builds_search_queries(self):
        """Test that search queries are built correctly (v2 query set)."""
        scraper = GoogleNewsScraper(self.config)
        queries = scraper._build_search_queries()

        # Every entry is a (query, event_type_hint) pair — the old
        # skip_territory_filter third element is gone
        self.assertTrue(all(len(q) == 2 for q in queries))
        for query, hint in queries:
            self.assertIsInstance(query, str)
            self.assertIn(hint, (EventType.CFO_HIRE, EventType.MERGER_ACQUISITION,
                                 EventType.FUNDING))

        # Should have CFO queries
        cfo_queries = [q for q, t in queries if 'CFO' in q]
        self.assertGreater(len(cfo_queries), 0)

        # Should have Crunchbase queries
        crunchbase_queries = [q for q, t in queries if 'crunchbase.com' in q]
        self.assertGreater(len(crunchbase_queries), 0)

        # Deleted 2026-09-06: LinkedIn (territory bypass), TechCrunch, and
        # the OTHER-typed expansion / launch queries
        self.assertEqual([q for q, t in queries if 'linkedin.com' in q], [])
        self.assertEqual([q for q, t in queries if 'techcrunch.com' in q], [])
        self.assertEqual([q for q, t in queries if t == EventType.OTHER], [])

    def test_process_entry_requires_detected_event_type(self):
        """The query hint is no longer a fallback: an article that does not
        read as a trigger is dropped even when the query implied a type."""
        import xml.etree.ElementTree as ET
        scraper = GoogleNewsScraper(self.config)
        item = ET.fromstring(
            '<item><title>Boston company opens new office - Local News</title>'
            '<link>https://example.com/a</link>'
            '<description>Boston firm expands footprint downtown.</description>'
            '</item>'
        )
        self.assertIsNone(scraper._process_entry(item, EventType.CFO_HIRE))


class TestTriggerEventModel(unittest.TestCase):
    """Tests for TriggerEvent model."""

    def test_event_creation(self):
        """Test creating a trigger event."""
        event = TriggerEvent(
            id="test-123",
            title="Test CFO Hire",
            event_type=EventType.CFO_HIRE,
            source=EventSource.PR_NEWSWIRE,
            url="https://example.com/news",
            published_date=datetime.now(timezone.utc),
            company_name="Test Corp",
            relevance_score=85.0
        )

        self.assertEqual(event.id, "test-123")
        self.assertEqual(event.event_type, EventType.CFO_HIRE)
        self.assertEqual(event.relevance_score, 85.0)

    def test_event_enrichment_fields(self):
        """Test event enrichment fields."""
        event = TriggerEvent(
            id="test-456",
            title="Test Event",
            event_type=EventType.FUNDING,
            source=EventSource.GOOGLE_NEWS,
            url="https://example.com",
            published_date=datetime.now(timezone.utc),
            company_website="https://testcorp.com",
            company_revenue="$50M",
            company_employees="200",
        )

        self.assertEqual(event.company_website, "https://testcorp.com")
        self.assertEqual(event.company_revenue, "$50M")
        self.assertEqual(event.company_employees, "200")

    def test_finance_seat_open_is_valid_event_type(self):
        """finance_seat_open — an open CFO/Controller seat (job posting) —
        is a first-class EventType, distinct from a seated cfo_hire."""
        self.assertIs(EventType('finance_seat_open'), EventType.FINANCE_SEAT_OPEN)
        self.assertNotEqual(EventType.FINANCE_SEAT_OPEN, EventType.CFO_HIRE)
        # Round-trips through the storage representation
        ev = TriggerEvent(
            id="seat-1", title="Acme Corp hiring: Controller",
            event_type=EventType.FINANCE_SEAT_OPEN, source=EventSource.ADZUNA,
            url="https://example.com/job", published_date=datetime.now(timezone.utc),
        )
        self.assertEqual(TriggerEvent.from_dict(ev.to_dict()).event_type,
                         EventType.FINANCE_SEAT_OPEN)


ADZUNA_TEST_CONFIG = {
    'territory': {'regions': [], 'cities': [], 'industries': [],
                  'excluded_industries': ['mining'],
                  'company_filters': {'exclude_public_companies': False}},
    'keywords': {'executive_hires': ['CFO'], 'mergers_acquisitions': [],
                 'funding_events': []},
    'scraper': {'timeout': 5, 'request_delay': 0},
    'adzuna': {'enabled': True, 'app_id': 'test-id', 'app_key': 'test-key',
               'countries': ['us'], 'run_hours': [12]},
}


def _adzuna_job(company: str, title: str, state: str, url: str) -> dict:
    return {
        'title': title,
        'company': {'display_name': company},
        'location': {'area': ['US', state, 'Some City'],
                     'display_name': f'Some City, {state}'},
        'redirect_url': url,
        'description': f'{company} seeks a {title}.',
        'created': datetime.now(timezone.utc).isoformat(),
    }


class TestAdzunaScraper(unittest.TestCase):
    """Adzuna: event typing and the persisted once-per-day latch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(os.path.join(self.tmp.name, 'scratch.db'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_posting_becomes_finance_seat_open(self):
        scraper = AdzunaScraper(ADZUNA_TEST_CONFIG)
        job = _adzuna_job('Acme, Inc.', 'Corporate Controller', 'New York',
                          'https://adzuna.example/j/1')
        ev = scraper._job_to_event(job, 'us', scraper.us_states)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, EventType.FINANCE_SEAT_OPEN)
        self.assertEqual(ev.source, EventSource.ADZUNA)
        self.assertEqual(ev.title, 'Acme, Inc. hiring: Corporate Controller')
        # A CFO posting is ALSO an open seat, not a cfo_hire
        job = _adzuna_job('Acme, Inc.', 'Chief Financial Officer', 'Ohio',
                          'https://adzuna.example/j/2')
        self.assertEqual(scraper._job_to_event(job, 'us', scraper.us_states).event_type,
                         EventType.FINANCE_SEAT_OPEN)

    def test_daily_latch_runs_once_per_utc_day(self):
        """With a db, the first scrape() of the day hits the API; the next
        call the same day is skipped. run_hours is ignored (no latch_mode:
        hour), so the drifting-cron hour mismatch can never starve Adzuna."""
        scraper = AdzunaScraper(ADZUNA_TEST_CONFIG, db=self.db)
        self.assertEqual(scraper.run_hours, [])  # ignored without latch_mode: hour
        with patch.object(AdzunaScraper, '_scrape_country', return_value=[]) as api:
            scraper.scrape()
            self.assertEqual(api.call_count, len(scraper.title_queries))
            self.assertEqual(self.db.get_kv(AdzunaScraper.LATCH_KEY),
                             datetime.now(timezone.utc).date().isoformat())
            scraper.scrape()
            self.assertEqual(api.call_count, len(scraper.title_queries),
                             'second call the same day must not hit the API')
        # Without a db there is no latch: every call runs
        free = AdzunaScraper(ADZUNA_TEST_CONFIG)
        with patch.object(AdzunaScraper, '_scrape_country', return_value=[]) as api:
            free.scrape(); free.scrape()
            self.assertEqual(api.call_count, 2 * len(free.title_queries))

    def test_latch_not_set_when_every_call_fails(self):
        scraper = AdzunaScraper(ADZUNA_TEST_CONFIG, db=self.db)
        with patch.object(AdzunaScraper, '_scrape_country', side_effect=RuntimeError('429')):
            scraper.scrape()
        self.assertIsNone(self.db.get_kv(AdzunaScraper.LATCH_KEY),
                          'a failed day must be retried next cycle')

    def test_query_params_include_finance_category(self):
        scraper = AdzunaScraper(ADZUNA_TEST_CONFIG)
        resp = MagicMock(); resp.json.return_value = {'results': []}
        with patch.object(scraper.session, 'get', return_value=resp) as get:
            scraper._scrape_country('us', 'controller')
        self.assertEqual(get.call_args.kwargs['params']['category'],
                         'accounting-finance-jobs')


class TestJobPostingDedup(unittest.TestCase):
    """One national Adzuna posting listed under N territory states must
    collapse to ONE lead (composite key: account_key + job title)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = {
            'scraper': {'database': os.path.join(self.tmp.name, 'scratch.db'),
                        'max_age_hours': 72, 'timeout': 5, 'request_delay': 0},
            'territory': {'regions': [], 'cities': [], 'industries': [],
                          'excluded_industries': [],
                          'company_filters': {'exclude_public_companies': False}},
            'keywords': {'executive_hires': [], 'mergers_acquisitions': [],
                         'funding_events': []},
            'alerts': {'file': {'enabled': False}, 'desktop': {'enabled': False},
                       'email': {'enabled': False}, 'slack': {'enabled': False}},
            'sources': {'rss_feeds': [], 'google_news': {'enabled': False}},
            'adzuna': {'enabled': False},
        }
        self.config_path = os.path.join(self.tmp.name, 'config.yaml')
        with open(self.config_path, 'w') as f:
            yaml.safe_dump(cfg, f)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _posting(company, title, state, url):
        return TriggerEvent(
            id=url, title=f'{company} hiring: {title}',
            event_type=EventType.FINANCE_SEAT_OPEN, source=EventSource.ADZUNA,
            url=url, published_date=datetime.now(timezone.utc),
            company_name=company, company_location=f'Somewhere, {state}',
            matched_regions=[state],
        )

    def _monitor(self, events):
        from src.main import TriggerEventMonitor
        monitor = TriggerEventMonitor(self.config_path)
        fake = MagicMock(); fake.scrape.return_value = events; fake.source_statuses = []
        monitor.scrapers = [fake]
        monitor.alert_manager.send_alerts = MagicMock(return_value=0)
        return monitor

    def test_same_posting_across_states_collapses_to_one(self):
        ny = self._posting('Acme, Inc.', 'Corporate Controller', 'New York',
                           'https://adzuna.example/j/ny')
        ma = self._posting('Acme Inc', 'Corporate Controller', 'Massachusetts',
                           'https://adzuna.example/j/ma')
        # Different job title at the same company is a DIFFERENT open seat
        cfo = self._posting('Acme Inc', 'Chief Financial Officer', 'Ohio',
                            'https://adzuna.example/j/oh')
        new_events = self._monitor([ny, ma, cfo]).run_once()
        self.assertEqual([e.url for e in new_events],
                         ['https://adzuna.example/j/ny', 'https://adzuna.example/j/oh'])

    def test_dedup_persists_across_runs(self):
        first = self._monitor([self._posting('Acme Inc', 'Controller', 'Ohio',
                                             'https://adzuna.example/j/1')]).run_once()
        self.assertEqual(len(first), 1)
        # Next run (same SQLite file) — the same seat under another state
        second = self._monitor([self._posting('Acme, Inc.', 'Controller', 'Maine',
                                              'https://adzuna.example/j/2')]).run_once()
        self.assertEqual(second, [])

    def test_sec_templated_titles_stay_exempt_from_title_dedup(self):
        """Two distinct 8-K filings with identical templated titles are both
        kept (the SEC exemption is unchanged)."""
        def sec(url):
            return TriggerEvent(
                id=url, title='SEC 8-K Item 5.02 — Acme Corp',
                event_type=EventType.CFO_HIRE, source=EventSource.SEC_EDGAR,
                url=url, published_date=datetime.now(timezone.utc), company_name='Acme Corp',
            )
        new_events = self._monitor([sec('https://sec.example/1'), sec('https://sec.example/2')]).run_once()
        self.assertEqual(len(new_events), 2)


if __name__ == '__main__':
    unittest.main()


# ═════════════════════════════════════════════════════════════════════════════
# v2 Phase 2 (2026-09-07): per-source counters — items_fetched (raw candidates
# BEFORE any territory/content gate) and filtered_out = fetched - kept — so
# the dashboard can tell "feed returned 0" apart from "everything filtered".
# ═════════════════════════════════════════════════════════════════════════════

def _assert_counters(test, status, expected_fetched=None):
    test.assertIn('items_fetched', status)
    test.assertIn('filtered_out', status)
    test.assertGreaterEqual(status['items_fetched'], status['events_found'])
    test.assertEqual(status['filtered_out'],
                     max(status['items_fetched'] - status['events_found'], 0))
    if expected_fetched is not None:
        test.assertEqual(status['items_fetched'], expected_fetched)


def _xml_response(body: str):
    resp = MagicMock()
    resp.content = body.encode('utf-8')
    resp.raise_for_status = MagicMock()
    return resp


_RSS_TWO_ITEMS = (
    '<rss><channel>'
    '<item><title>Acme Mining opens pit</title><link>https://e.com/1</link>'
    '<description>Nothing relevant here.</description></item>'
    '<item><title>NYC firm names CFO</title><link>https://e.com/2</link>'
    '<description>A New York company appointed a CFO.</description></item>'
    '</channel></rss>'
)


class TestRSSScraperCounters(unittest.TestCase):
    def setUp(self):
        self.config = {
            'territory': {'regions': ['New York'], 'cities': ['NYC'], 'industries': [],
                          'excluded_industries': ['Mining'],
                          'company_filters': {'exclude_public_companies': False}},
            'keywords': {'executive_hires': ['CFO'], 'mergers_acquisitions': [],
                         'funding_events': []},
            'sources': {'rss_feeds': [{'name': 'Test Feed', 'url': 'https://e.com/feed'}]},
            'scraper': {'timeout': 5, 'request_delay': 0},
        }

    def test_items_fetched_counts_entries_before_gates(self):
        scraper = RSSScraper(self.config)
        with patch.object(scraper.session, 'get', return_value=_xml_response(_RSS_TWO_ITEMS)):
            scraper.scrape()
        self.assertEqual(len(scraper.source_statuses), 1)
        status = scraper.source_statuses[0]
        _assert_counters(self, status, expected_fetched=2)
        self.assertLess(status['events_found'], 2, 'the mining item must be gated out')

    def test_error_path_reports_zero_fetched(self):
        scraper = RSSScraper(self.config)
        with patch.object(scraper.session, 'get', side_effect=Exception('boom')):
            scraper.scrape()
        status = scraper.source_statuses[0]
        self.assertEqual(status['status'], 'error')
        _assert_counters(self, status, expected_fetched=0)


class TestGoogleNewsScraperCounters(unittest.TestCase):
    def setUp(self):
        self.config = {
            'territory': {'regions': ['New York'], 'cities': [], 'industries': [],
                          'excluded_industries': [],
                          'company_filters': {'exclude_public_companies': False}},
            'keywords': {'executive_hires': ['CFO'], 'mergers_acquisitions': [],
                         'funding_events': []},
            'sources': {'google_news': {'enabled': True}},
            'scraper': {'timeout': 5, 'request_delay': 0},
        }

    def test_items_fetched_sums_result_items_across_queries(self):
        scraper = GoogleNewsScraper(self.config)
        n_queries = len(scraper._build_search_queries())
        body = ('<rss><channel>'
                '<item><title>Town opens park - Local</title><link>https://e.com/a</link>'
                '<description>No trigger.</description></item>'
                '</channel></rss>')
        with patch.object(scraper.session, 'get', return_value=_xml_response(body)):
            scraper.scrape()
        status = scraper.source_statuses[0]
        _assert_counters(self, status, expected_fetched=n_queries)
        self.assertEqual(status['events_found'], 0)

    def test_all_queries_failing_reports_zero_fetched(self):
        scraper = GoogleNewsScraper(self.config)
        with patch.object(scraper, '_scrape_query', side_effect=RuntimeError('503')):
            scraper.scrape()
        status = scraper.source_statuses[0]
        self.assertEqual(status['status'], 'error')
        _assert_counters(self, status, expected_fetched=0)


class TestAdzunaScraperCounters(unittest.TestCase):
    def test_items_fetched_counts_api_results_over_queries(self):
        scraper = AdzunaScraper(ADZUNA_TEST_CONFIG)
        results = [
            _adzuna_job('Acme, Inc.', 'Corporate Controller', 'New York', 'https://a.example/1'),
            _adzuna_job('Beta LLC', 'Air Traffic Controller', 'Ohio', 'https://a.example/2'),
            _adzuna_job('Gamma Co', 'Controller', 'California', 'https://a.example/3'),
        ]
        resp = MagicMock(); resp.json.return_value = {'results': results}
        with patch.object(scraper.session, 'get', return_value=resp):
            scraper.scrape()
        status = scraper.source_statuses[0]
        self.assertEqual(status['source_name'], 'Adzuna (US)')
        _assert_counters(self, status, expected_fetched=3 * len(scraper.title_queries))
        self.assertLess(status['events_found'], status['items_fetched'])

    def test_error_path_reports_zero_fetched(self):
        scraper = AdzunaScraper(ADZUNA_TEST_CONFIG)
        with patch.object(AdzunaScraper, '_scrape_country', side_effect=RuntimeError('429')):
            scraper.scrape()
        _assert_counters(self, scraper.source_statuses[0], expected_fetched=0)


SEC_TEST_CONFIG = {
    'territory': {'regions': [], 'cities': [], 'industries': [], 'excluded_industries': [],
                  'company_filters': {'exclude_public_companies': False}},
    'keywords': {'executive_hires': ['CFO'], 'mergers_acquisitions': [], 'funding_events': []},
    'scraper': {'timeout': 5, 'request_delay': 0},
    'sec_filings': {'enabled': True},
    'form_d': {'enabled': True, 'max_results': 100, 'request_sleep': 0},
}


class TestSECScraperCounters(unittest.TestCase):
    def test_8k_items_fetched_is_raw_efts_hit_count(self):
        from src.scrapers.sec_scraper import SECScraper, ITEM_DEFINITIONS
        scraper = SECScraper(SEC_TEST_CONFIG)
        hits = [{'_source': {'adsh': f'000-{i}'}} for i in range(5)]
        with patch.object(scraper, '_fetch_phrase_adsh_set', return_value=set()), \
             patch.object(scraper, '_search_efts', return_value=hits), \
             patch.object(scraper, '_hit_to_event', return_value=None):
            scraper.scrape()
        self.assertEqual(len(scraper.source_statuses), len(ITEM_DEFINITIONS))
        for status in scraper.source_statuses:
            _assert_counters(self, status, expected_fetched=5)
            self.assertEqual(status['events_found'], 0)
            self.assertEqual(status['filtered_out'], 5)

    def test_8k_error_path_reports_zero_fetched(self):
        from src.scrapers.sec_scraper import SECScraper
        scraper = SECScraper(SEC_TEST_CONFIG)
        with patch.object(scraper, '_fetch_phrase_adsh_set', return_value=set()), \
             patch.object(scraper, '_search_efts', side_effect=RuntimeError('EFTS down')):
            scraper.scrape()
        for status in scraper.source_statuses:
            self.assertEqual(status['status'], 'error')
            _assert_counters(self, status, expected_fetched=0)

    def test_form_d_items_fetched_counts_feed_hits(self):
        from src.scrapers.sec_scraper import FormDScraper
        scraper = FormDScraper(SEC_TEST_CONFIG)
        # 3 hits, none of which survive the free filter (no ciks/names/adsh)
        hits = [{'_source': {'file_type': 'D'}} for _ in range(3)]
        resp = MagicMock(); resp.json.return_value = {'hits': {'hits': hits}}
        resp.raise_for_status = MagicMock()
        with patch.object(scraper.session, 'get', return_value=resp):
            scraper.scrape()
        status = scraper.source_statuses[0]
        self.assertEqual(status['source_type'], 'sec_edgar')
        _assert_counters(self, status, expected_fetched=3)
        self.assertEqual(status['events_found'], 0)

    def test_form_d_error_path_reports_zero_fetched(self):
        from src.scrapers.sec_scraper import FormDScraper
        scraper = FormDScraper(SEC_TEST_CONFIG)
        with patch.object(scraper.session, 'get', side_effect=RuntimeError('EFTS down')):
            scraper.scrape()
        status = scraper.source_statuses[0]
        self.assertEqual(status['status'], 'error')
        _assert_counters(self, status, expected_fetched=0)


class TestMainPassesCountersThrough(unittest.TestCase):
    """run_once must persist items_fetched / filtered_out and print the
    per-source 'fetched N · kept K · filtered F' line for the Actions log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = {
            'scraper': {'database': os.path.join(self.tmp.name, 'scratch.db'),
                        'max_age_hours': 72, 'timeout': 5, 'request_delay': 0},
            'territory': {'regions': [], 'cities': [], 'industries': [],
                          'excluded_industries': [],
                          'company_filters': {'exclude_public_companies': False}},
            'keywords': {'executive_hires': [], 'mergers_acquisitions': [],
                         'funding_events': []},
            'alerts': {'file': {'enabled': False}, 'desktop': {'enabled': False},
                       'email': {'enabled': False}, 'slack': {'enabled': False}},
            'sources': {'rss_feeds': [], 'google_news': {'enabled': False}},
            'adzuna': {'enabled': False},
        }
        self.config_path = os.path.join(self.tmp.name, 'config.yaml')
        with open(self.config_path, 'w') as f:
            yaml.safe_dump(cfg, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_counters_saved_and_summarised(self):
        import io
        from contextlib import redirect_stdout
        from src.main import TriggerEventMonitor, _format_source_counters
        monitor = TriggerEventMonitor(self.config_path)
        fake = MagicMock(); fake.scrape.return_value = []
        fake.source_statuses = [
            {'source_name': 'Counted', 'source_type': 'rss_feed', 'status': 'success',
             'error_message': None, 'events_found': 2, 'items_fetched': 30, 'filtered_out': 28},
            {'source_name': 'Legacy', 'source_type': 'job_board', 'status': 'success',
             'error_message': None, 'events_found': 1},
        ]
        monitor.scrapers = [fake]
        out = io.StringIO()
        with redirect_stdout(out):
            monitor.run_once()
        rows = {r['source_name']: r for r in monitor.db.get_source_statuses()}
        self.assertEqual((rows['Counted']['items_fetched'], rows['Counted']['filtered_out']), (30, 28))
        self.assertIsNone(rows['Legacy']['items_fetched'])
        text = out.getvalue()
        self.assertIn('Counted: fetched 30 · kept 2 · filtered 28', text)
        self.assertIn('Legacy: fetched ? · kept 1 · filtered ?', text)
        self.assertEqual(_format_source_counters(None, 0, None), 'fetched ? · kept 0 · filtered ?')


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3 slice B1 — scrape-side supply (research 2026-09-08)
#
# tests/fixtures/*.xml are trimmed single-fetch snapshots of the real feeds
# (5-7 items each) so the tests below meet the XML shapes the scrapers meet:
# GlobeNewswire's <category domain=".../rss/stock"> tickers and dateline-free
# descriptions, Google News' HTML descriptions with the embedded
# news.google.com link, PR Newswire personnel datelines, and the WordPress /
# Drupal feeds of the regional business journals.
# ═══════════════════════════════════════════════════════════════════════════

import xml.etree.ElementTree as ET

from src.pipeline import sources
from src.scrapers.base import FINANCE_LEADER_TITLE, has_hire_indicator
from src.scrapers.news_scraper import MAX_QUERY_TERMS, RECENCY, query_term_count
from src.scrapers.rss_scraper import sanitize_feed_xml, strip_html

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(REPO_ROOT, 'tests', 'fixtures')

PRN_PERSONNEL_URL = ('https://www.prnewswire.com/rss/general-business-latest-news/'
                     'personnel-announcements-list.rss')
PRN_FIREHOSE_URL = 'https://www.prnewswire.com/rss/personnel-announcements-list.rss'

# (feed name, url, default_region, pipeline.sources label) — F1 / F8 / F9
PHASE3_FEEDS = [
    ('PR Newswire - Personnel Announcements', PRN_PERSONNEL_URL, None, 'PR Newswire'),
    ('Globe Newswire - CFO Keyword',
     'https://www.globenewswire.com/RssFeed/keyword/CFO/feedTitle/GlobeNewswire%20-%20CFO',
     None, 'GlobeNewswire'),
    ('Globe Newswire - Chief Financial Officer Keyword',
     'https://www.globenewswire.com/RssFeed/keyword/chief%20financial%20officer/feedTitle/'
     'GlobeNewswire%20-%20Chief%20Financial%20Officer', None, 'GlobeNewswire'),
    ('Globe Newswire - Management Changes',
     'https://www.globenewswire.com/RssFeed/subject/mgc/feedTitle/'
     'GlobeNewswire%20-%20Management%20Changes', None, 'GlobeNewswire'),
    ('PRWeb - All News', 'https://www.prweb.com/rss/news-releases-list.rss', None, 'PRWeb'),
    ('HomeCare Magazine', 'https://www.homecaremag.com/rss.xml', None, 'HomeCare Magazine'),
    ('Performance Brokerage Services - Dealer Transactions',
     'https://performancebrokerageservices.com/feed/', None, 'Performance Brokerage'),
    ('BodyShop Business', 'https://www.bodyshopbusiness.com/feed/', None, 'BodyShop Business'),
    ('Virginia Business', 'https://virginiabusiness.com/feed/', 'Virginia', 'Virginia Business'),
    ('Vermont Business Magazine', 'https://vermontbiz.com/rss.xml', 'Vermont', 'Vermont Business'),
    ('NH Business Review', 'https://www.nhbr.com/feed/', 'New Hampshire', 'NH Business Review'),
    ('Providence Business News', 'https://pbn.com/feed/', 'Rhode Island',
     'Providence Business News'),
    ('Hartford Business Journal', 'https://hartfordbusiness.com/feed/', 'Connecticut',
     'Hartford Business Journal'),
]


# Feeds that stay in the config DISABLED: the publisher refuses the hosted CI
# runner (HTTP 403 on every run), so an enabled entry only ever reports an
# error. Flipping one back on needs a fetch path that is not refused — this
# set is what makes that a deliberate change instead of a stray edit.
REFUSED_BY_PUBLISHER = {'HomeCare Magazine', 'Private Equity Insights'}


def _phase3_config():
    """A compact stand-in for config.example.yaml: the real territory shape,
    the finance-leader keyword list, a mega-cap blocklist — and no bank
    exclusion, because the NH Business Review fixture's deal IS a bank.
    The pipeline entries mirror the production narrowing (oil / gas
    pipeline, pipeline operator, midstream — never bare "Pipeline", which
    killed "sales pipeline"; review 2026-09-08)."""
    return {
        'territory': {
            'regions': ['New York', 'Massachusetts', 'Connecticut', 'New Hampshire',
                        'Vermont', 'Florida', 'Georgia', 'Alabama', 'Virginia', 'Ohio',
                        'Michigan', 'Indiana'],
            'cities': ['Boston', 'NYC', 'Burlington'],
            'industries': ['Healthcare', 'Nonprofit', 'Insurance'],
            'excluded_industries': ['Mining', 'Gold Mining', 'Hotel', 'Oil pipeline',
                                    'Gas pipeline', 'Pipeline operator', 'Midstream',
                                    'Gold Corp', 'Resources Inc', 'Lithium Corp', 'Mining Ltd'],
            'excluded_locations': ['California', 'Illinois', 'Texas', 'London', 'Chicago',
                                   'Kansas', 'Alberta', 'British Columbia'],
            'require_territory_match': True,
            'company_filters': {
                'exclude_public_companies': True,
                'public_company_indicators': ['(NYSE:', '(NASDAQ:', 'publicly traded',
                                              'Fortune 500', 'Fortune 100'],
                'excluded_public_companies': ['Google', 'Apple', 'Amazon', 'Oracle', 'US Bank',
                                              'JPMorgan', 'JPMorgan Chase', 'Citigroup',
                                              'Citizens Financial Group'],
            },
        },
        'keywords': {
            'executive_hires': ['CFO', 'Chief Financial Officer', 'Controller', 'VP of Finance',
                                'Vice President of Finance', 'Treasurer',
                                'Chief Accounting Officer', 'CEO', 'Chief Executive Officer',
                                'President'],
            'mergers_acquisitions': ['acquisition', 'acquired', 'acquires', 'to acquire',
                                     'merger', 'purchased by'],
            'funding_events': ['series A', 'funding round', 'raises'],
        },
        'sources': {'rss_feeds': [], 'google_news': {'enabled': True}},
        'scraper': {'timeout': 5, 'request_delay': 0},
    }


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


def _fixture_response(name):
    resp = MagicMock()
    resp.content = _fixture_bytes(name)
    resp.raise_for_status = MagicMock()
    return resp


def _fixture_items(name):
    return ET.fromstring(sanitize_feed_xml(_fixture_bytes(name))).findall('.//item')


def _item_titled(name, needle):
    for item in _fixture_items(name):
        if needle in (item.findtext('title') or ''):
            return item
    raise AssertionError(f'{needle!r} is not in fixture {name}')


def _scrape_fixture(scraper, fixture, feed_name, feed_config=None):
    """Run one fixture through RSSScraper._scrape_feed → (events, items_fetched)."""
    with patch.object(scraper.session, 'get', return_value=_fixture_response(fixture)):
        events, error, fetched = scraper._scrape_feed('https://fixture.test/feed', feed_name,
                                                      feed_config)
    if error:
        raise AssertionError(f'{fixture}: {error}')
    return events, fetched


def _rss_with(title, description=''):
    """A one-item RSS body (no dateline, no location unless the text has one)."""
    return _xml_response(
        '<rss><channel><item>'
        f'<title>{title}</title><link>https://fixture.test/item</link>'
        f'<description>{description}</description>'
        '</item></channel></rss>'
    )


class TestFinanceLeaderHireDetection(unittest.TestCase):
    """F2: the common wire headline shapes type correctly, and finance-leader
    seats below the CFO stay EXECUTIVE_HIRE — the Mac-side grader awards
    #NewController +3 vs #NewCFO +5, and relabeling them cfo_hire
    double-counted in Phase 1."""

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())

    def _type(self, text):
        return self.scraper.detect_event_type(text)

    def test_appoints_as_chief_financial_officer_is_cfo_hire(self):
        for text in ('Acme Holdings Appoints Jane Doe as Chief Financial Officer',
                     'Acme Holdings Names New CFO',
                     'Acme Corp announces CFO transition',
                     'Acme hires Jane Doe as CFO',
                     'Acme taps Jane Doe as finance chief'):
            self.assertEqual(self._type(text), EventType.CFO_HIRE, text)

    def test_names_controller_is_executive_hire(self):
        self.assertEqual(self._type('Acme names Jane Doe Controller'), EventType.EXECUTIVE_HIRE)

    def test_announced_appointment_of_vp_finance_is_executive_hire(self):
        self.assertEqual(self._type('Acme announced the appointment of Jane Doe as VP of Finance'),
                         EventType.EXECUTIVE_HIRE)

    def test_other_finance_leader_seats_are_executive_hire_never_cfo(self):
        for text in ('Acme promotes John Smith to Treasurer',
                     'Acme hires Jane Doe as Vice President of Finance',
                     'Acme taps Jane Doe as finance director',
                     'Acme adds Jane Doe as chief accounting officer',
                     'Jane Doe joined Acme as head of finance',
                     'Acme Foundation names Jane Doe director of finance'):
            self.assertEqual(self._type(text), EventType.EXECUTIVE_HIRE, text)
            self.assertIsNotNone(FINANCE_LEADER_TITLE.search(text), text)

    def test_board_seat_without_finance_role_stays_as_before(self):
        # A real PR Newswire personnel item (tests/fixtures/prn_personnel.xml)
        board = ('Renowned Systems Safety Expert Professor Najmedin Meshkati, PhD, '
                 'Appointed to ECRI Board of Trustees')
        self.assertIsNone(self._type(board))
        self.assertIsNone(FINANCE_LEADER_TITLE.search(board))
        with open(os.path.join(REPO_ROOT, 'config.example.yaml')) as f:
            production = RSSScraper(yaml.safe_load(f))
        self.assertNotEqual(production.detect_event_type(board), EventType.CFO_HIRE)

    def test_role_mention_without_a_hire_indicator_is_not_a_hire(self):
        self.assertIsNone(self._type('Acme CFO comments on quarterly results'))
        self.assertIsNone(self._type('The controller was determining the outcome'))
        # … including with the vocabulary every release carries (review
        # 2026-09-08: "announced" alone used to make these hires)
        self.assertIsNone(self._type('Acme today announced results; CFO Jane Doe commented'))
        self.assertIsNone(self._type('Acme announces its CFO will present at the conference'))
        self.assertIsNone(self._type('Acme adds two locations; CFO cites growth'))
        self.assertIsNone(self._type('Acme CFO discusses the energy transition'))
        self.assertFalse(has_hire_indicator('shareholders were disappointed'))   # whole words
        self.assertTrue(has_hire_indicator('Acme appoints'))
        for weak in ('Acme announces', 'Acme announced', 'Acme adds', 'the transition',
                     'the appointment'):
            self.assertFalse(has_hire_indicator(weak), weak)

    def test_every_globenewswire_cfo_fixture_item_types_as_cfo_hire(self):
        for item in _fixture_items('globenewswire_cfo.xml'):
            text = f"{item.findtext('title')} {strip_html(item.findtext('description') or '')}"
            self.assertEqual(self._type(text), EventType.CFO_HIRE, item.findtext('title'))


class TestUnknownTerritoryFinanceHireAdmission(unittest.TestCase):
    """F3: GlobeNewswire RSS carries no datelines, so every CFO hire from it
    had UNKNOWN territory and died at require_territory_match. Finance-leader
    hires are now admitted with unknown territory for enrichment to verify;
    M&A / funding never are."""

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())

    def _kept(self, title, description=''):
        with patch.object(self.scraper.session, 'get', return_value=_rss_with(title, description)):
            events, error, fetched = self.scraper._scrape_feed(
                'https://fixture.test/feed', 'Globe Newswire - CFO Keyword')
        self.assertIsNone(error)
        self.assertEqual(fetched, 1)
        return events

    def test_globenewswire_fixture_private_cfo_hires_are_kept_with_unknown_territory(self):
        events, fetched = _scrape_fixture(self.scraper, 'globenewswire_cfo.xml',
                                          'Globe Newswire - CFO Keyword')
        self.assertEqual(fetched, 7)
        kept = {e.title: e for e in events}
        for title in ('U.S. Oral Surgery Management Names Jennifer Ellis CFO',
                      'Leap Distributors Levels Up: Names Dave Ascani CFO'):
            self.assertIn(title, kept)
            self.assertEqual(kept[title].event_type, EventType.CFO_HIRE)
            self.assertEqual(kept[title].matched_regions, [])     # unknown → enrichment verifies
            self.assertEqual(kept[title].source, EventSource.GLOBE_NEWSWIRE)
        # Listed issuers carry <category domain=".../rss/stock">Nasdaq:CARG</category>
        # and never a "(NASDAQ:" in the text — still dropped as public companies.
        for title in ('CarGurus Appoints Matthew Mandel as Chief Financial Officer',
                      'Teladoc Health Appoints Michael Grasher as Chief Financial Officer'):
            self.assertNotIn(title, kept)

    def test_unknown_territory_ma_and_funding_are_still_dropped(self):
        self.assertEqual(self._kept('Acme Holdings acquires Beta Services',
                                    'Acme Holdings has acquired Beta Services.'), [])
        self.assertEqual(self._kept('Acme raises Series A funding round',
                                    'Acme raises a Series A funding round.'), [])
        # … even when the release quotes the CFO (review 2026-09-08: the
        # quote used to retype the deal as a cfo_hire and admit it)
        ma = ('Acme Holdings today announced it has entered into a definitive agreement '
              'to acquire Beta Services. "Beta is a natural fit," said Jane Doe, CFO of '
              'Acme Holdings. The transaction is expected to close in Q4.')
        self.assertEqual(self.scraper.detect_event_type(
            f'Acme Holdings to Acquire Beta Services {ma}',
            title='Acme Holdings to Acquire Beta Services'), EventType.MERGER_ACQUISITION)
        self.assertEqual(self._kept('Acme Holdings to Acquire Beta Services', ma), [])
        # an earnings release quoting the CFO is not a hire at all
        results = ('Acme Holdings today announced results for the second quarter. '
                   '"Revenue grew 12%," said Jane Doe, CFO. Acme also announced a dividend.')
        self.assertIsNone(self.scraper.detect_event_type(
            f'Acme Reports Q2 Results {results}', title='Acme Reports Q2 Results'))
        self.assertEqual(self._kept('Acme Reports Q2 Results', results), [])

    def test_unknown_territory_executive_hire_needs_a_finance_role_in_the_title(self):
        kept = self._kept('Acme Holdings names Jane Doe Controller',
                          'Acme Holdings has named Jane Doe Controller.')
        self.assertEqual([e.event_type for e in kept], [EventType.EXECUTIVE_HIRE])
        # the role only in the body is not enough
        self.assertEqual(self._kept('Acme Holdings strengthens its leadership team',
                                    'Acme Holdings has named Jane Doe Controller.'), [])
        # a non-finance executive hire is not admitted
        self.assertEqual(self._kept('Acme Holdings names Jane Doe Chief Executive Officer',
                                    'Acme Holdings has named Jane Doe CEO.'), [])

    def test_known_out_of_territory_is_not_unknown(self):
        title = 'Acme Appoints Jane Doe as Chief Financial Officer'
        # positive control: the same hire with no location at all is admitted
        self.assertEqual(len(self._kept(title, 'Acme today appointed Jane Doe as CFO.')), 1)
        # a dateline that resolves to a non-territory state is KNOWN out
        self.assertEqual(self._kept(
            title, 'DENVER, Colorado, Feb. 10, 2026 -- Acme today appointed Jane Doe as CFO.'), [])
        # an excluded location word is KNOWN out
        self.assertEqual(self._kept('London-based Acme names Jane Doe CFO',
                                    'Acme has named Jane Doe CFO.'), [])


class TestGoogleNewsPublicCompanyGate(unittest.TestCase):
    """F4: every Google News description embeds
    <a href="https://news.google.com/rss/articles/…"> and 'Google' is on
    excluded_public_companies — read raw, 100% of items were rejected as
    public companies (zero Google News events since 2026-02-05)."""

    def setUp(self):
        self.scraper = GoogleNewsScraper(_phase3_config())

    def test_embedded_news_google_link_no_longer_rejects_the_item(self):
        item = _item_titled('google_news_cfo.xml',
                            'Boys & Girls Homes names new chief financial officer')
        raw = item.findtext('description')
        self.assertIn('news.google.com', raw)
        self.assertTrue(self.scraper.is_public_company(raw))           # what the old gate saw
        self.assertFalse(self.scraper.is_public_company(strip_html(raw)))
        event = self.scraper._process_entry(item, EventType.CFO_HIRE, ['florida', 'georgia', 'alabama'])
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, EventType.CFO_HIRE)
        self.assertEqual(event.title, 'Boys & Girls Homes names new chief financial officer')
        self.assertNotIn('news.google.com', event.description or '')

    def test_mega_cap_names_match_whole_words_only(self):
        public = self.scraper.is_public_company
        self.assertFalse(public('Oracle NetSuite partner Acme names CFO'))    # platform phrase
        self.assertFalse(public('Pineapple Growers Cooperative names CFO'))   # not Apple
        self.assertFalse(public('Various banks reported results'))            # not US Bank
        self.assertTrue(public('Acme Corp (NASDAQ: ACME) names CFO'))         # ticker indicator stays
        self.assertTrue(public('Amazon names new CFO'))
        self.assertTrue(public('US Bank names new CFO'))
        self.assertTrue(public('Acme, an Oracle partner, names CFO'))         # bare Oracle still counts


class TestExcludedIndustryWholeWord(unittest.TestCase):
    """F5: excluded_industries were substring-matched — 'mining' fired on
    'determining' / 'examining'."""

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())

    def test_determining_no_longer_matches_mining(self):
        self.assertEqual(self.scraper.matches_industry(
            'Acme is determining the outcome of the finance review'), (False, False))
        self.assertEqual(self.scraper.matches_industry('Examining the books at Acme'), (False, False))

    def test_an_actual_mining_company_is_still_excluded(self):
        self.assertEqual(self.scraper.matches_industry('Acme Mining Corp appoints CFO'), (False, True))
        self.assertEqual(self.scraper.matches_industry('Gold mining company names controller'),
                         (False, True))
        self.assertEqual(self.scraper.matches_industry('Marriott Hotels names CFO'), (False, True))

    def test_vermont_business_fixture_item_with_examining_is_not_excluded(self):
        items = [i for i in _fixture_items('vermontbiz.xml')
                 if 'Examining' in ET.tostring(i, encoding='unicode')]
        self.assertTrue(items, 'fixture lost the "Examining" item')
        for item in items:
            text = f"{item.findtext('title')} {strip_html(item.findtext('description') or '')}"
            self.assertFalse(self.scraper.matches_industry(text)[1], item.findtext('title'))


class TestFeedDefaultRegion(unittest.TestCase):
    """F7: regional journals write 'Bedford-based', never 'New Hampshire', so
    their own-state stories carried no territory signal. rss_feeds[].default_region
    stands in ONLY when the text places the story nowhere — no dateline
    boost, trigger still required, hard blocks unchanged."""

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())

    def test_default_region_admits_the_nhbr_bank_deal(self):
        bank = 'Hometown Financial Group to acquire Primary Bank'
        merrimack = ('Merrimack Anheuser-Busch Property purchased by '
                     'United Therapeutics Corporation')
        without, fetched = _scrape_fixture(self.scraper, 'nhbr.xml', 'NH Business Review')
        self.assertEqual(fetched, 5)
        self.assertEqual([e.title for e in without], [merrimack])
        with_default, _ = _scrape_fixture(self.scraper, 'nhbr.xml', 'NH Business Review',
                                          {'default_region': 'New Hampshire'})
        kept = {e.title: e for e in with_default}
        self.assertEqual(set(kept), {merrimack, bank})
        event = kept[bank]
        self.assertEqual(event.event_type, EventType.MERGER_ACQUISITION)
        # ADMISSION ONLY (review 2026-09-08): the default never lands in
        # matched_regions — downstream would read it as a real match (the
        # dashboard's HQ column, enrichment's oracle anchor) — and earns no
        # territory points and no dateline boost.
        self.assertEqual(event.matched_regions, [])
        self.assertEqual(event.relevance_score, self.scraper.calculate_relevance_score(
            EventType.MERGER_ACQUISITION, [], False, False))
        self.assertEqual(event.source, EventSource.OTHER)

    def test_default_region_changes_nothing_for_stories_that_name_their_state(self):
        plain, fetched = _scrape_fixture(self.scraper, 'vermontbiz.xml', 'Vermont Business Magazine')
        self.assertEqual(fetched, 5)
        self.assertTrue(plain, 'the Vermont fixture should yield an in-territory trigger')
        defaulted, _ = _scrape_fixture(self.scraper, 'vermontbiz.xml', 'Vermont Business Magazine',
                                       {'default_region': 'Vermont'})
        self.assertEqual([(e.title, e.matched_regions) for e in plain],
                         [(e.title, e.matched_regions) for e in defaulted])
        for event in plain:
            self.assertIn('vermont', event.matched_regions)

    def test_default_region_never_overrides_a_known_location_or_the_hard_blocks(self):
        def kept(title, description, feed_config={'default_region': 'New Hampshire'}):
            with patch.object(self.scraper.session, 'get',
                              return_value=_rss_with(title, description)):
                events, _error, _n = self.scraper._scrape_feed(
                    'https://fixture.test/feed', 'NH Business Review', feed_config)
            return events

        # an excluded location keeps the story out
        self.assertEqual(kept('Acme to acquire Beta', 'London-based Acme will acquire Beta.'), [])
        # a place in the text wins over the default
        [event] = kept('Acme to acquire Beta', 'Boston-based Acme will acquire Beta.')
        self.assertEqual(event.matched_regions, ['boston'])
        # the default admits the placeless story — with NO matched_regions
        # (review 2026-09-08: admission only, never a hint downstream)
        [event] = kept('Acme to acquire Beta', 'Bedford-based Acme will acquire Beta.')
        self.assertEqual(event.matched_regions, [])
        self.assertEqual(event.relevance_score, self.scraper.calculate_relevance_score(
            EventType.MERGER_ACQUISITION, [], False, False))
        # … but a trigger is still required, and the hard blocks still apply
        self.assertEqual(kept('Acme opens a new office', 'Acme opened an office in Bedford.'), [])
        self.assertEqual(kept('Acme Mining to acquire Beta', 'Acme Mining will acquire Beta.'), [])
        self.assertEqual(kept('Acme (NASDAQ: ACME) to acquire Beta', 'Acme will acquire Beta.'), [])
        # a default_region outside the territory list is ignored
        self.assertEqual(kept('Acme to acquire Beta', 'Acme will acquire Beta.',
                              {'default_region': 'Narnia'}), [])
        self.assertEqual(kept('Acme to acquire Beta', 'Acme will acquire Beta.', None), [])

    def test_default_region_never_overrides_a_dateline_elsewhere(self):
        """Review 2026-09-08: a wire dateline that resolves to a non-territory
        state, a province or a bare foreign city — none on
        excluded_locations — placed the story SOMEWHERE, yet the default
        made it a New Hampshire deal."""
        def kept(description):
            with patch.object(self.scraper.session, 'get',
                              return_value=_rss_with('Acme to acquire Beta', description)):
                events, _error, _n = self.scraper._scrape_feed(
                    'https://fixture.test/feed', 'NH Business Review',
                    {'default_region': 'New Hampshire'})
            return events
        # positive control — the same deal with no place at all is admitted
        self.assertEqual(len(kept('Acme today agreed to acquire Beta for $10 million.')), 1)
        for dateline in ('TOPEKA, Kan., Sept. 8, 2026 /PRNewswire/ --',
                         'CALGARY, Alberta, Sept. 8, 2026 /CNW/ --',
                         'MANILA, Sept. 8, 2026 /PRNewswire/ --',
                         'DENVER, Colorado, Sept. 8, 2026 --'):
            self.assertEqual(kept(f'{dateline} Acme today agreed to acquire Beta.'), [],
                             dateline)
        # an in-territory dateline elsewhere in the same text still wins
        [event] = kept('BOSTON, Sept. 8, 2026 -- Acme today agreed to acquire Beta.')
        self.assertEqual(event.matched_regions, ['boston'])

    def test_default_region_never_makes_a_pe_backed_story_a_stable_target(self):
        """Review 2026-09-08: the default set in_territory, which switched on
        the PE-backed stable-target path — a mere 'portfolio company opens
        office' became a stable_target alert. A real trigger is required."""
        def kept(title, description):
            with patch.object(self.scraper.session, 'get',
                              return_value=_rss_with(title, description)):
                events, _error, _n = self.scraper._scrape_feed(
                    'https://fixture.test/feed', 'NH Business Review',
                    {'default_region': 'New Hampshire'})
            return events
        self.assertEqual(kept('Acme, a portfolio company of Beta Capital Partners, opens new office',
                              'The PE-backed firm opened an office in Bedford.'), [])
        # the same story naming its state IS the (pre-existing) stable-target path
        [event] = kept('Acme, a portfolio company of Beta Capital Partners, opens new office',
                       'The PE-backed firm opened an office in Bedford, New Hampshire.')
        self.assertEqual(event.event_type, EventType.STABLE_TARGET)
        self.assertEqual(event.matched_regions, ['new hampshire'])


class TestRegionGroupedGoogleNewsQueries(unittest.TestCase):
    """F6: region-grouped CFO / dealership / home-care queries, every query
    short and dated (past ~32 words Google silently drops `when:`), and the
    query's state group as a territory HINT for articles whose text names no
    place — the enrichment HQ gate re-verifies the headquarters."""

    EXPECTED_GROUPS = (
        ('Florida', 'Georgia', 'Alabama'),
        ('North Carolina', 'South Carolina', 'Tennessee', 'Kentucky'),
        ('Virginia', 'Maryland', 'West Virginia', 'Delaware'),
        ('Pennsylvania', 'New Jersey', 'New York'),
        ('Massachusetts', 'Connecticut', 'Rhode Island'),
        ('Maine', 'New Hampshire', 'Vermont'),
        ('Ohio', 'Michigan', 'Indiana'),
        ('Ontario', 'Quebec', 'Nova Scotia', 'New Brunswick'),
    )

    def setUp(self):
        self.scraper = GoogleNewsScraper(_phase3_config())
        self.queries = self.scraper._build_search_queries()

    def test_every_query_is_short_and_dated(self):
        # "words" = search terms as Google counts a query: a quoted phrase is
        # one term, OR / parentheses are operators, `when:7d` is one term.
        self.assertEqual(MAX_QUERY_TERMS, 12)
        self.assertEqual(RECENCY, 'when:7d')
        for query, _hint in self.queries:
            self.assertIn('when:7d', query, query)
            self.assertLessEqual(query_term_count(query), MAX_QUERY_TERMS, query)
            self.assertLess(len(query.split()), 32, query)        # Google's hard cap
        self.assertEqual(query_term_count(
            'CFO (names OR appoints OR appointed) (Florida OR Georgia OR Alabama) when:7d'), 8)
        self.assertEqual(query_term_count(
            '("home care" OR "home health") (acquires OR acquired) '
            '("North Carolina" OR Tennessee) when:7d'), 7)

    def test_region_groups_cover_the_territory_with_three_query_shapes(self):
        self.assertEqual(tuple(GoogleNewsScraper.REGION_GROUPS), self.EXPECTED_GROUPS)
        by_query = dict(self.queries)
        hints = self.scraper._region_hints
        self.assertEqual(len(hints), 8 * 3)
        for group in self.EXPECTED_GROUPS:
            states = ' OR '.join(f'"{s}"' if ' ' in s else s for s in group)
            shapes = {
                f'CFO (names OR appoints OR appointed) ({states}) when:7d': EventType.CFO_HIRE,
                f'dealership (acquires OR acquired) ({states}) when:7d': EventType.MERGER_ACQUISITION,
                f'("home care" OR "home health") (acquires OR acquired) ({states}) when:7d':
                    EventType.MERGER_ACQUISITION,
            }
            for query, hint in shapes.items():
                self.assertEqual(by_query.get(query), hint, query)
                self.assertEqual(hints[query], [s.lower() for s in group])
        self.assertEqual([q for q, _ in self.queries if 'funeral' in q.lower()], [])

    @staticmethod
    def _item(headline):
        # the exact Google News shape: HTML description with the article
        # link, the publisher trailing both title and description
        return ET.fromstring(
            f'<item><title>{headline} - Local Paper</title>'
            '<link>https://news.google.com/rss/articles/x</link>'
            '<description>&lt;a href="https://news.google.com/rss/articles/x"&gt;'
            f'{headline}&lt;/a&gt;&amp;nbsp;&amp;nbsp;'
            '&lt;font color="#6f6f6f"&gt;Local Paper&lt;/font&gt;</description></item>')

    def test_state_hint_admits_the_placeless_item_and_never_lands_in_matched_regions(self):
        item = self._item
        hint = ['florida', 'georgia', 'alabama']
        base_score = self.scraper.calculate_relevance_score(EventType.CFO_HIRE, [], False, False)
        # no place in the text → the hint admits the item; matched_regions
        # stays EMPTY (review 2026-09-08: a 3-state list there showed its
        # first state as the HQ on the dashboard and anchored enrichment's
        # oracle lookup on a guess) and it earns no territory points
        event = self.scraper._process_entry(item('Acme Holdings names new CFO'), EventType.CFO_HIRE, hint)
        self.assertIsNotNone(event)
        self.assertEqual(event.matched_regions, [])
        self.assertEqual(event.relevance_score, base_score)
        # the same item without a hint is unknown territory → dropped
        self.assertIsNone(self.scraper._process_entry(item('Acme Holdings names new CFO'),
                                                      EventType.CFO_HIRE))
        # a place in the text wins over the hint (and scores)
        event = self.scraper._process_entry(item('Boston Acme names new CFO'), EventType.CFO_HIRE, hint)
        self.assertEqual(event.matched_regions, ['boston'])
        self.assertGreater(event.relevance_score, base_score)
        # an excluded location with no in-territory signal is dropped before the hint
        self.assertIsNone(self.scraper._process_entry(item('Chicago Acme names new CFO'),
                                                      EventType.CFO_HIRE, hint))
        # … but an in-territory signal alongside it is kept (F6a ordering)
        event = self.scraper._process_entry(item('Chicago Acme names new CFO for its Florida unit'),
                                            EventType.CFO_HIRE)
        self.assertEqual(event.matched_regions, ['florida'])

    def test_state_hint_does_not_admit_an_item_that_names_a_non_territory_place(self):
        """Review 2026-09-08: 'Wichita, Kansas dealership acquires rival' was
        kept as ['ohio', 'michigan', 'indiana'] — Kansas (and the western
        provinces) were neither territory nor excluded. They are excluded
        now, in the production config too."""
        hint = ['ohio', 'michigan', 'indiana']
        for headline in ('Wichita, Kansas dealership acquires rival',
                         'Calgary, Alberta dealership acquires rival',
                         'Vancouver, British Columbia home care agency acquired'):
            self.assertIsNone(self.scraper._process_entry(self._item(headline),
                                                          EventType.MERGER_ACQUISITION, hint),
                              headline)
        # positive control: the same shape with no place is admitted
        event = self.scraper._process_entry(self._item('Family dealership acquires rival'),
                                            EventType.MERGER_ACQUISITION, hint)
        self.assertEqual((event.event_type, event.matched_regions),
                         (EventType.MERGER_ACQUISITION, []))
        with open(os.path.join(REPO_ROOT, 'config.example.yaml')) as f:
            production = yaml.safe_load(f)
        excluded = {loc.lower() for loc in production['territory']['excluded_locations']}
        for place in ('alberta', 'british columbia', 'manitoba', 'saskatchewan', 'yukon',
                      'kansas'):
            self.assertIn(place, excluded, place)
        scraper = GoogleNewsScraper(production)
        self.assertTrue(scraper.is_excluded_location('a British Columbia agency'))
        self.assertTrue(scraper.is_excluded_location('Wichita, Kansas'))
        [(_term, rx)] = _compile_whole_word(['Kansas'])
        self.assertIsNone(rx.search('an Arkansas agency'))                    # whole word

    def test_scrape_query_threads_the_hint_of_the_query_that_found_the_item(self):
        query = ('CFO (names OR appoints OR appointed) '
                 '(Massachusetts OR Connecticut OR "Rhode Island") when:7d')
        self.assertIn(query, self.scraper._region_hints)
        with patch.object(self.scraper.session, 'get',
                          return_value=_fixture_response('google_news_cfo.xml')):
            events = self.scraper._scrape_query(query, EventType.CFO_HIRE)
        titles = {e.title for e in events}
        self.assertIn('Boys & Girls Homes names new chief financial officer', titles)
        self.assertNotIn('Woman killed, Rhode Island man injured in Times Square knife attack',
                         titles)                                       # no trigger
        for event in events:
            self.assertEqual(event.event_type, EventType.CFO_HIRE)
            self.assertEqual(event.source, EventSource.GOOGLE_NEWS)
            # the hint admitted the placeless items; none carries the state
            # group as if the text had named it (review 2026-09-08)
            self.assertNotIn('massachusetts', event.matched_regions)
        self.assertTrue([e for e in events if e.matched_regions == []])


class TestPhase3FixturesParse(unittest.TestCase):
    """The snapshots parse the way the live feeds do."""

    def test_each_fixture_parses_to_its_item_count(self):
        for name, count in (('prn_personnel.xml', 7), ('globenewswire_cfo.xml', 7),
                            ('google_news_cfo.xml', 7), ('nhbr.xml', 5), ('vermontbiz.xml', 5)):
            self.assertEqual(len(_fixture_items(name)), count, name)
            self.assertLessEqual(len(_fixture_bytes(name)), 40 * 1024, name)

    def test_prn_personnel_datelined_in_territory_hire_is_kept(self):
        events, fetched = _scrape_fixture(RSSScraper(_phase3_config()), 'prn_personnel.xml',
                                          'PR Newswire - Personnel Announcements')
        self.assertEqual(fetched, 7)
        memorial = [e for e in events
                    if e.title.startswith('Memorial Healthcare System Names Shane Strum')]
        self.assertEqual(len(memorial), 1)
        self.assertEqual(memorial[0].event_type, EventType.EXECUTIVE_HIRE)
        self.assertIn('florida', memorial[0].matched_regions)          # HOLLYWOOD, Fla. dateline
        self.assertEqual(memorial[0].source, EventSource.PR_NEWSWIRE)


class TestPhase3FeedConfig(unittest.TestCase):
    """F1 / F8: config.example.yaml (what CI copies to config.yaml) carries the
    real PR Newswire personnel feed and each Phase 3 feed exactly once."""

    @staticmethod
    def _config(name):
        with open(os.path.join(REPO_ROOT, name)) as f:
            return yaml.safe_load(f)

    def _assert_feeds(self, cfg):
        feeds = cfg['sources']['rss_feeds']
        names = [f['name'] for f in feeds]
        urls = [f['url'] for f in feeds]
        by_name = {f['name']: f for f in feeds}
        for name, url, default_region, _label in PHASE3_FEEDS:
            self.assertEqual(names.count(name), 1, name)
            self.assertEqual(urls.count(url), 1, url)
            self.assertEqual(by_name[name]['url'], url, name)
            self.assertEqual(by_name[name].get('enabled', True),
                             name not in REFUSED_BY_PUBLISHER, name)
            self.assertEqual(by_name[name].get('default_region'), default_region, name)
        for name in REFUSED_BY_PUBLISHER:
            self.assertEqual(names.count(name), 1, name)   # kept, not deleted
            self.assertIs(by_name[name].get('enabled', True), False, name)
        self.assertNotIn(PRN_FIREHOSE_URL, urls)          # the all-news firehose is gone
        regions = {r.lower() for r in cfg['territory']['regions']}
        for feed in feeds:
            if feed.get('default_region'):
                self.assertIn(feed['default_region'].lower(), regions, feed['name'])

    def test_example_config_has_each_new_feed_exactly_once(self):
        self._assert_feeds(self._config('config.example.yaml'))

    def test_local_config_mirrors_the_example_feeds_if_present(self):
        if not os.path.exists(os.path.join(REPO_ROOT, 'config.yaml')):
            self.skipTest('no local config.yaml (CI copies the example)')
        self._assert_feeds(self._config('config.yaml'))

    def test_example_keywords_cover_the_finance_leader_seats(self):
        keywords = {k.lower() for k in self._config('config.example.yaml')['keywords']['executive_hires']}
        for seat in ('controller', 'vp of finance', 'vice president of finance', 'treasurer',
                     'finance director', 'director of finance', 'head of finance',
                     'chief accounting officer'):
            self.assertIn(seat, keywords)


class TestPhase3SourceLabels(unittest.TestCase):
    """F9: every new feed folds onto a stable pipeline.sources label (the yield
    table, the went-quiet check and the accounts table bucket rows the same
    way), and the RIA trigger's typed source 'sec_iapd' has its own bucket."""

    def test_feed_label_for_each_new_feed(self):
        for name, _url, _region, label in PHASE3_FEEDS:
            self.assertEqual(sources.feed_label(name, 'rss_feed'), label, name)
            self.assertTrue(sources.is_canonical_label(label), label)
            self.assertTrue(sources.feed_matches_label(name, label, 'rss_feed'), name)

    def test_rss_source_enum_for_the_new_feeds(self):
        scraper = RSSScraper(_phase3_config())
        self.assertEqual(scraper._determine_source('Globe Newswire - CFO Keyword'),
                         EventSource.GLOBE_NEWSWIRE)
        self.assertEqual(scraper._determine_source('Globe Newswire - Management Changes'),
                         EventSource.GLOBE_NEWSWIRE)
        # PRWeb is not PR Newswire: no wire relevance boost, its own label
        self.assertEqual(scraper._determine_source('PRWeb - All News'), EventSource.OTHER)
        self.assertEqual(scraper._determine_source('NH Business Review'), EventSource.OTHER)

    def test_event_rows_from_the_new_feeds_label_by_host(self):
        self.assertEqual(sources.source_label(
            {'source': 'other', 'source_url': 'https://www.nhbr.com/hometown-financial-group/',
             'title': 'x'}), 'NH Business Review')
        self.assertEqual(sources.source_label(
            {'source': 'other', 'source_url': 'https://www.prweb.com/releases/acme-names-cfo.html',
             'title': 'x'}), 'PRWeb')
        self.assertEqual(sources.source_label(
            {'source': 'globe_newswire', 'source_url': '', 'title': 'x'}), 'GlobeNewswire')

    def test_sec_iapd_rows_are_labelled_sec_iapd(self):
        adviser = 'https://adviserinfo.sec.gov/firm/summary/1001'
        title = 'New SEC-registered investment adviser: Acme Wealth (Boston, MA)'
        self.assertEqual(sources.source_label(
            {'source': 'sec_iapd', 'source_url': adviser, 'title': title}), 'SEC IAPD')
        # the host rule wins over the generic sec.gov → 8-K split, even untyped
        self.assertEqual(sources.source_label({'source_url': adviser, 'title': title}), 'SEC IAPD')
        self.assertEqual(sources.source_label(
            {'source_url': 'https://www.sec.gov/Archives/edgar/data/1/0001.htm',
             'title': 'SEC 8-K Item 5.02 (Departure/Election) — Acme Inc'}), 'SEC 8-K')
        self.assertTrue(sources.is_canonical_label('SEC IAPD'))
        self.assertEqual(sources.feed_label('SEC IAPD new advisers'), 'SEC IAPD')


# ═══════════════════════════════════════════════════════════════════════════
# Review 2026-09-08 — adversarial pass over Phase 3 (scrape side)
#
# The hire type is decided from the TITLE; feed defaults and Google News
# state-group hints ADMIT an item and never land in matched_regions; the
# whole-word blocklists keep their long forms; "Fortune 500" counts in the
# title only; a hung feed retries after 15s, not 60s.
# ═══════════════════════════════════════════════════════════════════════════

from requests.exceptions import Timeout as _Timeout

from src.scrapers import rss_scraper as _rss_module
from src.scrapers.base import (BODY_HEAD_CHARS, TITLE_ONLY_PUBLIC_INDICATORS,
                               _compile_whole_word, finance_leader_hire_kind)


def _production_config():
    with open(os.path.join(REPO_ROOT, 'config.example.yaml')) as f:
        return yaml.safe_load(f)


def _scrape_one(scraper, title, description, feed_name='Globe Newswire - CFO Keyword',
                feed_config=None):
    """One dateline-free item through RSSScraper._scrape_feed → events."""
    with patch.object(scraper.session, 'get', return_value=_rss_with(title, description)):
        events, error, fetched = scraper._scrape_feed('https://fixture.test/feed', feed_name,
                                                      feed_config)
    if error:
        raise AssertionError(error)
    assert fetched == 1
    return events


class TestHireTypeDecidedFromTitle(unittest.TestCase):
    """HIGH: Phase 3 typed "any role mention + any of 24 indicator words"
    over the whole text — earnings releases and product news became hires,
    and a Controller hire whose body named the CFO became a cfo_hire (the
    #NewCFO / #NewController double-count the grader is built to prevent).
    The type is now decided from the title (base.finance_leader_hire_kind)."""

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())
        self.production = RSSScraper(_production_config())

    def _type(self, title, body='', scraper=None):
        return (scraper or self.scraper).detect_event_type(f'{title} {body}', title=title)

    def test_earnings_release_quoting_the_cfo_is_not_a_hire(self):
        title = 'Acme Reports Second Quarter 2026 Results'
        body = ('BOSTON, Aug. 5, 2026 -- Acme today announced results for the quarter. '
                '"We delivered," said Jane Doe, CFO. Acme also announced a dividend.')
        self.assertIsNone(self._type(title, body))
        self.assertIsNone(self._type(title, body, self.production))
        self.assertIsNone(finance_leader_hire_kind(title, body))
        # and it is not admitted with unknown territory either
        self.assertEqual(_scrape_one(self.scraper, title, body), [])

    def test_product_and_business_news_mentioning_a_role_are_not_hires(self):
        for title in ('Acme Announces Launch of Wireless Game Controller',
                      'Acme adds two locations; CFO cites growth',
                      'Acme CFO discusses the energy transition',
                      'Vermont State Treasurer announces unclaimed property',
                      'Acme taps new motor controller supplier',
                      'Acme launches new controller for smart homes',
                      'Acme announces CFO', 'Acme announced its CFO', 'Acme adds CFO',
                      'Acme CFO appointment'):
            self.assertIsNone(finance_leader_hire_kind(title), title)
            self.assertIsNone(self._type(title), title)
            self.assertIsNone(self._type(title, '', self.production), title)

    def test_ma_release_quoting_the_cfo_is_an_acquisition(self):
        title = 'Acme Holdings to Acquire Beta Services'
        body = ('Acme Holdings today announced a definitive agreement to acquire Beta '
                'Services. "Beta is a natural fit," said Jane Doe, CFO of Acme Holdings.')
        self.assertEqual(self._type(title, body), EventType.MERGER_ACQUISITION)
        self.assertEqual(self._type(title, body, self.production), EventType.MERGER_ACQUISITION)

    def test_the_role_in_the_title_decides_the_seat(self):
        # the double-count base: a Controller hire whose body names the CFO
        title = 'Acme Names Jane Doe Corporate Controller'
        body = 'Doe will report to Chief Financial Officer John Smith.'
        self.assertEqual(self._type(title, body), EventType.EXECUTIVE_HIRE)
        self.assertEqual(self._type(title, body, self.production), EventType.EXECUTIVE_HIRE)
        self.assertEqual(finance_leader_hire_kind(title, body), 'exec')
        cfo_body = 'She succeeds CFO John Smith, who was named CFO in 2019.'
        for title in ('Acme Names Jane Doe Controller', 'Acme promotes Jane Doe to VP Finance',
                      'Acme appoints Jane Doe Treasurer', 'Acme hires Jane Doe as Finance Director',
                      'Acme names Jane Doe head of finance',
                      'Acme taps Jane Doe as Chief Accounting Officer'):
            self.assertEqual(self._type(title, cfo_body), EventType.EXECUTIVE_HIRE, title)
            self.assertEqual(finance_leader_hire_kind(title, cfo_body), 'exec', title)
        # CFO in the title → cfo_hire, whatever the body says
        self.assertEqual(self._type('Acme Appoints Jane Doe as President & Chief Financial Officer',
                                    'Doe was previously corporate controller at Beta.'),
                         EventType.CFO_HIRE)
        self.assertEqual(finance_leader_hire_kind('Acme Names New CFO',
                                                  'The controller reports to her.'), 'cfo')

    def test_strong_verbs_and_hire_noun_phrases_make_a_hire_weak_words_do_not(self):
        for title, want in (('Acme announces CFO transition', 'cfo'),
                            ('Acme announces the appointment of Jane Doe as CFO', 'cfo'),
                            ('Meet the incoming CFO of Acme', 'cfo'),
                            ('Jane Doe promoted to CFO at Acme', 'cfo'),
                            ('Diego Reynoso to join Ingredion as Chief Financial Officer', 'cfo'),
                            ('Acme welcomes Jane Doe as VP of Finance', 'exec'),
                            ('Acme elevates Jane Doe to controller', 'exec'),
                            ('Acme announces appointment of Jane Doe as Corporate Controller',
                             'exec')):
            self.assertEqual(finance_leader_hire_kind(title), want, title)
            self.assertEqual(self._type(title),
                             EventType.CFO_HIRE if want == 'cfo' else EventType.EXECUTIVE_HIRE,
                             title)

    def test_bare_controller_is_a_finance_role_only_in_a_finance_context(self):
        for title in ('Acme Announces Launch of Wireless Game Controller',
                      'Acme names Beta its motor controller supplier',
                      'Acme appoints Beta as traffic controller software vendor'):
            self.assertIsNone(finance_leader_hire_kind(title), title)
            self.assertIsNone(self._type(title, '', self.production), title)
        for title in ('Acme names Jane Doe controller', 'Acme appoints Jane Doe as controller',
                      'Acme promotes Jane Doe to controller',
                      'Acme names Jane Doe assistant controller',
                      'Acme hires Jane Doe as plant controller'):
            self.assertEqual(finance_leader_hire_kind(title), 'exec', title)
            self.assertEqual(self._type(title), EventType.EXECUTIVE_HIRE, title)

    def test_body_head_is_consulted_only_when_the_title_names_no_role(self):
        title = 'Acme Holdings strengthens its leadership team'
        body = ('Acme Holdings today announced it has named Jane Doe Corporate Controller, '
                'effective immediately.')
        self.assertEqual(self._type(title, body), EventType.EXECUTIVE_HIRE)
        self.assertEqual(finance_leader_hire_kind(title, body), 'exec')
        self.assertIsNone(finance_leader_hire_kind(title))          # the title alone: no
        # a role beyond the head of the body does not count
        self.assertIsNone(self._type(title, 'x ' * BODY_HEAD_CHARS + body))
        # a title that names the role without a hire is decided by the title
        self.assertIsNone(self._type('Acme CFO discusses the energy transition',
                                     'Acme today named Jane Doe CFO.'))

    def test_unknown_territory_admission_uses_the_same_title_based_test(self):
        # typed from the head of the body → not admitted without a territory
        self.assertEqual(_scrape_one(
            self.scraper, 'Acme Holdings strengthens its leadership team',
            'Acme Holdings today announced it has named Jane Doe Corporate Controller.'), [])
        self.assertEqual(_scrape_one(
            self.scraper, 'Acme Holdings strengthens its leadership team',
            'Acme Holdings today announced it has named Jane Doe Chief Financial Officer.'), [])
        # typed from the title → admitted, as EXECUTIVE_HIRE, territory unknown
        [event] = _scrape_one(self.scraper, 'Acme Holdings names Jane Doe Corporate Controller',
                              'Doe will report to Chief Financial Officer John Smith.')
        self.assertEqual((event.event_type, event.matched_regions), (EventType.EXECUTIVE_HIRE, []))
        # the very same test on both sides of the gate
        admit = RSSScraper._is_finance_leader_hire
        self.assertTrue(admit(EventType.EXECUTIVE_HIRE,
                              'Acme Holdings names Jane Doe Corporate Controller'))
        self.assertTrue(admit(EventType.CFO_HIRE, 'Acme Holdings Names New CFO'))
        self.assertFalse(admit(EventType.EXECUTIVE_HIRE,
                               'Acme Holdings strengthens its leadership team'))
        self.assertFalse(admit(EventType.CFO_HIRE, 'Acme Reports Q2 Results'))
        self.assertFalse(admit(EventType.MERGER_ACQUISITION, 'Acme Holdings Names New CFO'))

    def test_board_seats_awards_and_past_roles_are_not_hires(self):
        for title in ('Jane Doe, CFO of Acme, Appointed to Beta Board of Directors',
                      'Acme CFO Jane Doe Named CFO of the Year',
                      'Public Company Director and Former Fortune 50 Chief Accounting Officer '
                      'Patti Humble Joins the Exceptional Women Alliance (EWA)',
                      'Acme CFO Jane Doe Named to Power 100 List'):
            self.assertIsNone(finance_leader_hire_kind(title), title)
            self.assertIsNone(self._type(title), title)
            self.assertIsNone(self._type(title, '', self.production), title)
        # a real hire that also mentions the board, or the past seat, still is one
        self.assertEqual(finance_leader_hire_kind('Acme Names Jane Doe CFO and Board Member'), 'cfo')
        self.assertEqual(finance_leader_hire_kind('Former Google CFO Jane Doe joins Acme as CFO'),
                         'cfo')

    def test_generic_executive_hire_path_needs_a_strong_verb_or_new_role(self):
        # as before Phase 3: President / CEO plus a strong verb
        self.assertEqual(self._type('Memorial Healthcare System Names Shane Strum President & CEO'),
                         EventType.EXECUTIVE_HIRE)
        self.assertEqual(self._type('Acme announces new CEO'), EventType.EXECUTIVE_HIRE)
        # the weak words are not enough any more
        self.assertIsNone(self._type('Acme announces CEO will keynote the summit'))
        self.assertIsNone(self._type('Acme President announces expansion'))
        # a strong verb in the body does not reach a title that names the role without one
        self.assertIsNone(self._type('Acme CEO on the future of widgets',
                                     'The CEO joined Acme in 2019.'))
        # never a finance-leader hire, so never admitted with unknown territory
        self.assertIsNone(finance_leader_hire_kind('Acme announces new CEO'))
        # the production keyword list carries hire-signal PHRASES ("appoints",
        # "named as") under executive_hires — a verb keyword is not a role
        for title in ('Acme appoints Beta as exclusive distributor',
                      'Acme Appoints Beta as Investment Banker for the Sale'):
            self.assertIsNone(self._type(title, '', self.production), title)
        self.assertNotIn('appoints', self.production._role_keywords)
        self.assertIn('president', self.production._role_keywords)


class TestFortune500IndicatorIsTitleOnly(unittest.TestCase):
    """LOW: "Fortune 500" / "Fortune 100" dropped private CFO hires whose bio
    says "Fortune 500 executive" (live: the Lotlinx item in the GlobeNewswire
    fixture). Those two indicators count in the title only; tickers and
    exchange phrases stay body-wide."""

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())

    def test_lotlinx_style_private_cfo_hire_is_kept(self):
        title = 'Lotlinx Appoints George D. King, III as President & Chief Financial Officer'
        body = strip_html(_item_titled('globenewswire_cfo.xml', 'Lotlinx').findtext('description'))
        self.assertIn('Fortune 500', body)
        self.assertTrue(self.scraper.is_public_company(f'{title} {body}'))         # no title: as before
        self.assertFalse(self.scraper.is_public_company(f'{title} {body}', title=title))
        events, _ = _scrape_fixture(self.scraper, 'globenewswire_cfo.xml',
                                    'Globe Newswire - CFO Keyword')
        kept = {e.title: e for e in events}
        self.assertIn(title, kept)
        self.assertEqual(kept[title].event_type, EventType.CFO_HIRE)

    def test_fortune_500_in_the_title_and_tickers_anywhere_still_exclude(self):
        public = self.scraper.is_public_company
        self.assertTrue(public('Fortune 500 Acme names CFO Doe.', title='Fortune 500 Acme names CFO'))
        self.assertTrue(public('Acme names CFO. Acme (NASDAQ: ACME) is listed.', title='Acme names CFO'))
        self.assertEqual(TITLE_ONLY_PUBLIC_INDICATORS, ('fortune 500', 'fortune 100'))
        indicators = {i.lower() for i in
                      _production_config()['territory']['company_filters']['public_company_indicators']}
        self.assertTrue({'fortune 500', 'fortune 100', '(nyse:', '(nasdaq:'} <= indicators)
        production = RSSScraper(_production_config())
        self.assertFalse(production.is_public_company(
            'Acme names CFO. Veteran Fortune 500 executive joins.', title='Acme names CFO'))


class TestBlocklistWholeWordLongForms(unittest.TestCase):
    """MEDIUM-LOW: whole-word matching (research 2026-09-08) lost the one-word
    brand "JPMorganChase" and the long corporate suffixes ("Barrick Gold
    Corporation" for "gold corp"); bare "Pipeline" is gone from the config
    but was still in the test config."""

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())
        self.production = RSSScraper(_production_config())

    def test_jpmorganchase_one_word_brand_is_public_citizens_financial_services_is_not(self):
        for scraper in (self.scraper, self.production):
            self.assertTrue(scraper.is_public_company('JPMorganChase names new CFO'))
            self.assertTrue(scraper.is_public_company('JPMorgan Chase names new CFO'))
            self.assertFalse(scraper.is_public_company(
                'Citizens Financial Services names new CFO'))     # small PA bank, not Citi / CFG
            self.assertTrue(scraper.is_public_company('Citizens Financial Group names new CFO'))
        self.assertIn('JPMorganChase',
                      _production_config()['territory']['company_filters']['excluded_public_companies'])
        [(_term, rx)] = _compile_whole_word(['JPMorgan Chase'])
        self.assertTrue(rx.search('JPMorganChase'))
        self.assertTrue(rx.search('JPMorgan  Chase'))
        self.assertIsNone(rx.search('JPMorgan Chasers'))
        # multi-word locations compile the same way
        [(_term, rx)] = _compile_whole_word(['British Columbia'])
        self.assertTrue(rx.search('a British Columbia agency'))
        self.assertIsNone(rx.search('British Columbian'))

    def test_long_corporate_suffixes_still_match_excluded_industries(self):
        for text in ('Barrick Gold Corporation appoints CFO',
                     'Acme Resources Incorporated names CFO',
                     'Acme Lithium Corporation appoints CFO',
                     'Acme Mining Limited names controller',
                     'Acme Gold Corp. appoints CFO'):
            for scraper in (self.scraper, self.production):
                self.assertTrue(scraper.matches_industry(text)[1], text)
        # the suffix must be the corporate one, whole word
        for text in ('Acme Golden Retrievers Inc names CFO', 'Acme Corporate Resources names CFO'):
            self.assertFalse(self.production.matches_industry(text)[1], text)
        [(_term, rx)] = _compile_whole_word(['Mining Ltd'], plural=True)
        self.assertTrue(rx.search('Acme Mining Limited'))
        self.assertIsNone(rx.search('Acme Mining Ltdx'))

    def test_sales_pipeline_is_not_an_excluded_industry_in_production(self):
        self.assertFalse(self.production.matches_industry(
            'Acme names CFO to build its sales pipeline')[1])
        for text in ('Acme, a gas pipeline operator, names CFO',
                     'Acme, an oil pipeline company, names CFO',
                     'Acme Midstream names CFO'):
            self.assertTrue(self.production.matches_industry(text)[1], text)
        self.assertNotIn('pipeline',
                         {i.lower() for i in _production_config()['territory']['excluded_industries']})
        self.assertNotIn('Pipeline', _phase3_config()['territory']['excluded_industries'])


from src.scrapers.rss_scraper import repair_feed_xml  # noqa: E402

# A feed with the mistakes publishers actually ship (all synthetic): a bare
# "&" inside an image URL attribute, HTML-only entities, a forbidden control
# character, a word joined by "&" — plus a CDATA section and a comment, where
# a bare "&" is legal and must survive untouched.
_MALFORMED_FEED = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<rss version="2.0"><channel><title>Example Wire</title>\n'
    b'<!-- built by a CMS & friends -->\n'
    b'<item><title>Acme Bank names new treasurer</title><link>https://example.test/a</link>\n'
    b'<enclosure url="https://example.test/a.png?w=457&quality=82" type="image/png"/>\n'
    b'<description><![CDATA[<img src="https://example.test/a.png?w=457&quality=82"> Q&A]]></description>\n'
    b'</item>\n'
    b'<item><title>Beta Credit Union&rsquo;s controller&nbsp;retires</title>'
    b'<link>https://example.test/b</link>\n'
    b'<description>AT&T and R&D\x0b notes &bogus; &amp; &#8217; &#x2019; &lt;done&gt;</description>\n'
    b'</item></channel></rss>'
)


class TestFeedXmlRepair(unittest.TestCase):
    """A publisher's malformed feed (a bare "&" in an image URL) made the
    strict parser reject the WHOLE feed, run after run, until the publisher
    fixed it. repair_feed_xml repairs the common mistakes once, and only
    after the strict parse has failed."""

    def _parsed(self):
        return ET.fromstring(repair_feed_xml(_MALFORMED_FEED))

    def test_fixture_really_breaks_the_strict_parser(self):
        with self.assertRaises(ET.ParseError):
            ET.fromstring(_MALFORMED_FEED)

    def test_repair_parses_and_keeps_every_value(self):
        items = self._parsed().findall('.//item')
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].find('enclosure').get('url'),
                         'https://example.test/a.png?w=457&quality=82')
        self.assertTrue(items[0].findtext('description').endswith('?w=457&quality=82"> Q&A'))
        self.assertEqual(items[1].findtext('title'),
                         'Beta Credit Union’s controller retires')
        self.assertEqual(items[1].findtext('description'),
                         'AT&T and R&D notes &bogus; & ’ ’ <done>')

    def test_valid_references_and_well_formed_feeds_are_left_alone(self):
        for body in (b'<rss><channel><title>A &amp; B &#38; C &#x26; &lt;D&gt;</title></channel></rss>',
                     _RSS_TWO_ITEMS.encode('utf-8')):
            self.assertEqual(repair_feed_xml(body), body)

    def test_repair_also_declares_missing_namespaces(self):
        body = (b'<rss><channel><item><title>x</title>'
                b'<media:content url="https://example.test/c.jpg?a=1&b=2"/></item></channel></rss>')
        root = ET.fromstring(repair_feed_xml(body))
        content = root.find('.//{http://search.yahoo.com/mrss/}content')
        self.assertEqual(content.get('url'), 'https://example.test/c.jpg?a=1&b=2')

    def test_scrape_feed_recovers_the_malformed_feed(self):
        scraper = RSSScraper(_phase3_config())
        resp = MagicMock(); resp.content = _MALFORMED_FEED; resp.raise_for_status = MagicMock()
        with patch.object(scraper.session, 'get', return_value=resp):
            _events, error, fetched = scraper._scrape_feed('https://fixture.test/feed', 'Example Wire')
        self.assertIsNone(error)
        self.assertEqual(fetched, 2)

    def test_scrape_feed_never_rewrites_a_well_formed_feed(self):
        scraper = RSSScraper(_phase3_config())
        with patch.object(scraper.session, 'get', return_value=_xml_response(_RSS_TWO_ITEMS)), \
             patch.object(_rss_module, 'repair_feed_xml', side_effect=AssertionError('repair ran')):
            _events, error, fetched = scraper._scrape_feed('https://fixture.test/feed', 'Clean Feed')
        self.assertIsNone(error)
        self.assertEqual(fetched, 2)

    def test_scrape_feed_still_reports_a_feed_it_cannot_repair(self):
        scraper = RSSScraper(_phase3_config())
        page = '<html><body><p>Access denied<br></p></body></html>'   # an error page, not a feed
        with patch.object(scraper.session, 'get', return_value=_xml_response(page)):
            events, error, fetched = scraper._scrape_feed('https://fixture.test/feed', 'Broken Feed')
        self.assertEqual((events, fetched), ([], 0))
        self.assertIn('mismatched tag', error)


class TestFeedRetrySleep(unittest.TestCase):
    """LOW: 45 feeds + 69 Google queries under timeout-minutes: 15 — a hung
    feed cost 30s + 60s sleep + 30s. The retry now waits 15s."""

    def test_timed_out_feed_retries_once_after_15s(self):
        self.assertEqual(_rss_module.RETRY_SLEEP_SECONDS, 15)
        scraper = RSSScraper(_phase3_config())
        with patch.object(scraper.session, 'get', side_effect=_Timeout('slow')) as get, \
             patch.object(_rss_module.time, 'sleep') as sleep:
            events, error, fetched = scraper._scrape_feed('https://fixture.test/feed', 'Slow Feed')
        self.assertEqual((events, fetched), ([], 0))
        self.assertIn('Timeout', error)
        self.assertEqual(get.call_count, 2)                      # one retry, as before
        sleep.assert_called_once_with(15)


class TestSupportRoleToTheSeatIsNotAHire(unittest.TestCase):
    """review 2026-09-08 (Phase 4): the 'to <role>' clause of
    base._HIRE_PHRASE_RE fired before anything blanked "assistant … to the
    CFO", so Adzuna postings for an EA / intern / coordinator supporting the
    CFO typed as cfo_hire — and seven of them were pinned in the golden set as
    'cfo'. The support phrase is now blanked by _NOT_THE_SEAT_RES (title and
    body head alike); a real promotion "to CFO" still types."""

    SUPPORT_TITLES = (
        'Acme Names Jane Doe Executive Assistant to the CFO',
        'Six Nations hiring: 145-26-1 EA to CFO',
        'Orange Bowl hiring: 2026-27 Assistant to the CFO Internship',
        'Williston hiring: Human Resources Coordinator and Admin Assistant to the CFO',
        'Laborie Medical Technologies Corp hiring: Executive Assistant to CFO & CPO',
        'Acme Appoints Jane Doe Senior Advisor to the CFO',
        'Acme Hires Jane Doe as Chief of Staff to the Chief Financial Officer',
        'Acme Names Jane Doe Executive Assistant to the Corporate Controller',
    )

    def setUp(self):
        self.scraper = RSSScraper(_phase3_config())
        self.production = RSSScraper(_production_config())

    def test_support_role_to_the_seat_is_not_the_seat(self):
        for title in self.SUPPORT_TITLES:
            self.assertIsNone(finance_leader_hire_kind(title), title)
            # a body that names the seat the role supports does not rescue it
            self.assertIsNone(finance_leader_hire_kind(
                title, 'The role supports the Chief Financial Officer and the finance team.'), title)
            for scraper in (self.scraper, self.production):
                self.assertNotIn(scraper.detect_event_type(title, title=title),
                                 (EventType.CFO_HIRE, EventType.EXECUTIVE_HIRE), title)

    def test_support_role_in_the_body_head_is_not_a_hire_either(self):
        title = 'Acme Announces Team Expansion'
        body = ('BOSTON, Sept. 8, 2026 /PRNewswire/ -- Acme today announced it has hired '
                'Jane Doe as Executive Assistant to the CFO, effective immediately.')
        self.assertIsNone(finance_leader_hire_kind(title, body))
        self.assertIsNone(self.scraper.detect_event_type(f'{title} {body}', title=title))

    def test_real_promotions_and_appointments_still_type(self):
        for title, want in (('Jane Doe promoted to CFO at Acme', 'cfo'),
                            ('Acme Promotes Jane Doe to Chief Financial Officer', 'cfo'),
                            # the support phrase is blanked, the seat that remains still types
                            ('Acme Promotes Jane Doe from Executive Assistant to the CFO '
                             'to Chief Financial Officer', 'cfo'),
                            ('Acme Names Jane Doe Successor to CFO John Smith', 'cfo'),
                            ('Acme Promotes Jane Doe to Corporate Controller', 'exec'),
                            ('Acme Names Jane Doe Assistant Controller', 'exec')):
            self.assertEqual(finance_leader_hire_kind(title), want, title)
        self.assertEqual(self.scraper.detect_event_type('Jane Doe promoted to CFO at Acme',
                                                        title='Jane Doe promoted to CFO at Acme'),
                         EventType.CFO_HIRE)
        self.assertEqual(self.production.detect_event_type(
            'Acme Promotes Jane Doe to Corporate Controller',
            title='Acme Promotes Jane Doe to Corporate Controller'), EventType.EXECUTIVE_HIRE)
