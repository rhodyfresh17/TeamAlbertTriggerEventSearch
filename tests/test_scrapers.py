"""Tests for the trigger event scrapers."""

import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

from src.models import TriggerEvent, EventType, EventSource
from src.scrapers.base import BaseScraper
from src.scrapers.rss_scraper import RSSScraper
from src.scrapers.news_scraper import GoogleNewsScraper


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

        # Should match region
        in_territory, regions = scraper.matches_territory("Company based in New York announces...")
        self.assertTrue(in_territory)
        self.assertIn('New York', regions)

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
        """Test that search queries are built correctly."""
        scraper = GoogleNewsScraper(self.config)
        queries = scraper._build_search_queries()

        # Should have CFO queries
        cfo_queries = [q for q, t in queries if 'CFO' in q]
        self.assertGreater(len(cfo_queries), 0)

        # Should have LinkedIn queries
        linkedin_queries = [q for q, t in queries if 'linkedin.com' in q]
        self.assertGreater(len(linkedin_queries), 0)

        # Should have Crunchbase queries
        crunchbase_queries = [q for q, t in queries if 'crunchbase.com' in q]
        self.assertGreater(len(crunchbase_queries), 0)


class TestTriggerEventModel(unittest.TestCase):
    """Tests for TriggerEvent model."""

    def test_event_creation(self):
        """Test creating a trigger event."""
        event = TriggerEvent(
            id="test-123",
            title="Test CFO Hire",
            event_type=EventType.CFO_HIRE,
            source=EventSource.RSS_FEED,
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


if __name__ == '__main__':
    unittest.main()
