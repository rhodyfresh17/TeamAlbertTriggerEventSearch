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
