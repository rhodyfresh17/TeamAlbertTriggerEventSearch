"""SEC EDGAR scraper for M&A filings and corporate events."""

import re
import feedparser
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


class SECScraper(BaseScraper):
    """Scraper for SEC EDGAR filings related to M&A and corporate events."""

    # SEC EDGAR RSS feed for 8-K filings (material events)
    SEC_8K_FEED = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom"

    # 8-K item codes related to M&A and leadership
    RELEVANT_ITEMS = {
        '1.01': 'Entry into Material Agreement',
        '2.01': 'Completion of Acquisition or Disposition',
        '5.02': 'Departure/Election of Directors or Officers',
        '8.01': 'Other Events',
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        # Check if SEC feed is enabled
        self.enabled = False
        feeds = config.get('sources', {}).get('rss_feeds', [])
        for feed in feeds:
            if 'sec' in feed.get('name', '').lower():
                self.enabled = feed.get('enabled', True)
                break

    def scrape(self) -> List[TriggerEvent]:
        """Scrape SEC EDGAR for relevant filings."""
        if not self.enabled:
            return []

        events = []

        try:
            feed = feedparser.parse(self.SEC_8K_FEED)

            for entry in feed.entries:
                event = self._process_filing(entry)
                if event:
                    events.append(event)

            self.delay_request()

        except Exception as e:
            print(f"Error scraping SEC EDGAR: {e}")

        return events

    def _process_filing(self, entry: dict) -> Optional[TriggerEvent]:
        """Process a single SEC filing entry."""
        title = entry.get('title', '')
        link = entry.get('link', '')
        summary = entry.get('summary', '')

        # Parse company info from title
        # Format typically: "8-K - COMPANY NAME (0001234567) (Filer)"
        company_match = re.search(r'8-K\s*-\s*(.+?)\s*\(', title)
        company_name = company_match.group(1).strip() if company_match else None

        # Check if this matches our industries (skip excluded)
        full_text = f"{title} {summary} {company_name or ''}"
        matches_target_industry, matches_excluded = self.matches_industry(full_text)

        if matches_excluded:
            return None

        # Check target company
        matches_company, matched_company = self.matches_target_company(full_text)

        # Determine event type from content
        event_type = self._determine_filing_type(summary, title)
        if not event_type:
            return None

        # Check territory (SEC filings often don't have location, so be lenient)
        in_territory, matched_regions = self.matches_territory(full_text)

        # For SEC filings, we're more lenient - accept if it matches event type
        # and either matches industry, company, or territory
        if not (matches_target_industry or matches_company or in_territory):
            # If no direct match, still include high-value M&A events
            if event_type != EventType.MERGER_ACQUISITION:
                return None

        # Calculate relevance
        relevance = self.calculate_relevance_score(
            event_type,
            matched_regions,
            matches_target_industry,
            matches_company
        )

        # Parse date
        published = self._parse_date(entry)

        # Extract deal info if M&A
        acquirer, target, deal_value = self._extract_deal_info(summary)

        return TriggerEvent(
            id=self.generate_event_id(link, title),
            title=f"SEC 8-K: {company_name or 'Unknown Company'}",
            event_type=event_type,
            source=EventSource.SEC_EDGAR,
            url=link,
            published_date=published,
            company_name=matched_company or company_name,
            description=summary[:500] if summary else None,
            acquirer=acquirer,
            target=target,
            deal_value=deal_value,
            matched_keywords=self._get_matched_keywords(full_text, event_type),
            matched_regions=matched_regions,
            relevance_score=relevance
        )

    def _determine_filing_type(self, summary: str, title: str) -> Optional[EventType]:
        """Determine the event type from SEC filing content."""
        text = f"{summary} {title}".lower()

        # Check for acquisition/disposition (Item 2.01)
        acquisition_keywords = [
            'acquisition', 'acquired', 'merger', 'disposition',
            'purchase', 'sale of assets', 'business combination'
        ]
        if any(kw in text for kw in acquisition_keywords):
            return EventType.MERGER_ACQUISITION

        # Check for officer changes (Item 5.02)
        officer_keywords = [
            'cfo', 'chief financial', 'departure', 'appointment',
            'resignation', 'election', 'officer', 'director'
        ]
        if any(kw in text for kw in officer_keywords):
            if 'cfo' in text or 'chief financial' in text:
                return EventType.CFO_HIRE
            return EventType.EXECUTIVE_HIRE

        # Check for material agreements that might indicate M&A
        agreement_keywords = [
            'definitive agreement', 'merger agreement', 'asset purchase',
            'stock purchase', 'material agreement'
        ]
        if any(kw in text for kw in agreement_keywords):
            return EventType.MERGER_ACQUISITION

        return None

    def _extract_deal_info(self, text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract M&A deal information from text."""
        acquirer = None
        target = None
        deal_value = None

        # Try to extract deal value
        value_patterns = [
            r'\$[\d,]+(?:\.\d+)?\s*(?:million|billion|M|B)',
            r'(?:approximately|about|nearly)\s*\$[\d,]+(?:\.\d+)?',
        ]
        for pattern in value_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                deal_value = match.group(0)
                break

        # Try to extract acquirer and target
        acquire_pattern = r'([A-Z][A-Za-z\s&]+)\s+(?:to acquire|acquiring|acquired|will acquire)\s+([A-Z][A-Za-z\s&]+)'
        match = re.search(acquire_pattern, text)
        if match:
            acquirer = match.group(1).strip()
            target = match.group(2).strip()

        return acquirer, target, deal_value

    def _parse_date(self, entry: dict) -> datetime:
        """Parse date from SEC filing entry."""
        # SEC feeds use 'updated' field
        for field in ['updated', 'published']:
            date_str = entry.get(field)
            if date_str:
                try:
                    # SEC uses ISO format
                    return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                except Exception:
                    pass

        return datetime.now(timezone.utc)

    def _get_matched_keywords(self, text: str, event_type: EventType) -> List[str]:
        """Get matched keywords from text."""
        text_lower = text.lower()
        matched = []

        all_keywords = self.ma_keywords + self.exec_hire_keywords
        for kw in all_keywords:
            if kw in text_lower:
                matched.append(kw)

        return matched[:5]
