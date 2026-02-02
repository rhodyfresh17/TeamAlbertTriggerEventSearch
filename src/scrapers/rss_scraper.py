"""RSS feed scraper for business news and PR wires."""

import feedparser
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from email.utils import parsedate_to_datetime

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


class RSSScraper(BaseScraper):
    """Scraper for RSS feeds from PR wires and news sources."""

    SOURCE_MAPPING = {
        'business wire': EventSource.BUSINESS_WIRE,
        'pr newswire': EventSource.PR_NEWSWIRE,
        'globe newswire': EventSource.GLOBE_NEWSWIRE,
        'globenewswire': EventSource.GLOBE_NEWSWIRE,
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.feeds = config.get('sources', {}).get('rss_feeds', [])

    def scrape(self) -> List[TriggerEvent]:
        """Scrape all configured RSS feeds."""
        events = []

        for feed_config in self.feeds:
            if not feed_config.get('enabled', True):
                continue

            feed_name = feed_config.get('name', 'Unknown')
            feed_url = feed_config.get('url')

            if not feed_url:
                continue

            try:
                feed_events = self._scrape_feed(feed_url, feed_name)
                events.extend(feed_events)
                self.delay_request()
            except Exception as e:
                print(f"Error scraping {feed_name}: {e}")

        return events

    def _scrape_feed(self, url: str, feed_name: str) -> List[TriggerEvent]:
        """Scrape a single RSS feed."""
        events = []

        try:
            feed = feedparser.parse(url)

            if feed.bozo and feed.bozo_exception:
                print(f"Feed parse warning for {feed_name}: {feed.bozo_exception}")

            for entry in feed.entries:
                event = self._process_entry(entry, feed_name)
                if event:
                    events.append(event)

        except Exception as e:
            print(f"Error parsing feed {feed_name}: {e}")

        return events

    def _process_entry(self, entry: dict, feed_name: str) -> Optional[TriggerEvent]:
        """Process a single feed entry."""
        title = entry.get('title', '')
        link = entry.get('link', '')
        summary = entry.get('summary', entry.get('description', ''))

        # Combine title and summary for analysis
        full_text = f"{title} {summary}"

        # Detect event type
        event_type = self.detect_event_type(full_text)
        if not event_type:
            return None

        # Check territory match
        in_territory, matched_regions = self.matches_territory(full_text)

        # Check industry match
        matches_target_industry, matches_excluded = self.matches_industry(full_text)

        # Skip if matches excluded industry
        if matches_excluded:
            return None

        # Skip public companies (we target mid-market private)
        if self.is_public_company(full_text):
            return None

        # Check target company
        matches_company, company_name = self.matches_target_company(full_text)

        # Require either territory match, industry match, or target company
        if not (in_territory or matches_target_industry or matches_company):
            return None

        # Calculate relevance
        relevance = self.calculate_relevance_score(
            event_type,
            matched_regions,
            matches_target_industry,
            matches_company
        )

        # Parse published date
        published = self._parse_date(entry)

        # Extract additional info
        extracted_company = company_name or self.extract_company_name(full_text)
        person_name, person_title = self.extract_person_info(full_text)

        # Determine source
        source = self._determine_source(feed_name)

        # Get matched keywords
        matched_keywords = self._get_matched_keywords(full_text, event_type)

        return TriggerEvent(
            id=self.generate_event_id(link, title),
            title=title,
            event_type=event_type,
            source=source,
            url=link,
            published_date=published,
            company_name=extracted_company,
            description=summary[:500] if summary else None,
            person_name=person_name,
            person_title=person_title,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=relevance
        )

    def _parse_date(self, entry: dict) -> datetime:
        """Parse the published date from a feed entry."""
        # Try different date fields
        for date_field in ['published', 'updated', 'created']:
            date_str = entry.get(date_field)
            if date_str:
                try:
                    return parsedate_to_datetime(date_str)
                except Exception:
                    pass

        # Try parsed versions
        for parsed_field in ['published_parsed', 'updated_parsed', 'created_parsed']:
            parsed = entry.get(parsed_field)
            if parsed:
                try:
                    return datetime(*parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass

        # Default to now
        return datetime.now(timezone.utc)

    def _determine_source(self, feed_name: str) -> EventSource:
        """Determine the event source from feed name."""
        feed_lower = feed_name.lower()

        for name, source in self.SOURCE_MAPPING.items():
            if name in feed_lower:
                return source

        return EventSource.OTHER

    def _get_matched_keywords(self, text: str, event_type: EventType) -> List[str]:
        """Get list of matched keywords."""
        text_lower = text.lower()
        matched = []

        keyword_sets = {
            EventType.CFO_HIRE: self.exec_hire_keywords,
            EventType.EXECUTIVE_HIRE: self.exec_hire_keywords,
            EventType.MERGER_ACQUISITION: self.ma_keywords,
            EventType.FUNDING: self.funding_keywords,
        }

        keywords = keyword_sets.get(event_type, [])
        for kw in keywords:
            if kw in text_lower:
                matched.append(kw)

        return matched[:5]  # Limit to top 5
