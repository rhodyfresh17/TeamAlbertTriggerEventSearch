"""FinSMEs scraper for startup funding and M&A news.

FinSMEs (finsmes.com) is a financial news site focused on startup funding rounds,
venture capital, and M&A activity. This scraper fetches their RSS feed with
browser-like headers to avoid 403 blocks.
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from email.utils import parsedate_to_datetime
from requests.exceptions import Timeout, ReadTimeout, ConnectTimeout

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


def strip_html(text: str) -> str:
    """Remove HTML tags and clean up text."""
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', '', text)
    clean = clean.replace('&nbsp;', ' ')
    clean = clean.replace('&amp;', '&')
    clean = clean.replace('&lt;', '<')
    clean = clean.replace('&gt;', '>')
    clean = clean.replace('&quot;', '"')
    clean = clean.replace('&#39;', "'")
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


class FinSMEsScraper(BaseScraper):
    """Scraper for FinSMEs startup funding and M&A news."""

    # FinSMEs WordPress RSS feed
    FEED_URL = "https://www.finsmes.com/feed"

    # Browser-like headers to avoid 403 blocks
    BROWSER_HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        finsmes_config = config.get('sources', {}).get('finsmes', {})
        self.enabled = finsmes_config.get('enabled', True)
        self.feed_url = finsmes_config.get('url', self.FEED_URL)
        self.source_statuses = []

    def scrape(self) -> List[TriggerEvent]:
        """Scrape FinSMEs RSS feed for funding and M&A events."""
        self.source_statuses = []

        if not self.enabled:
            return []

        events, error_msg = self._scrape_feed()

        self.source_statuses.append({
            'source_name': 'FinSMEs',
            'source_type': 'finsmes',
            'status': 'error' if error_msg else 'success',
            'error_message': error_msg,
            'events_found': len(events)
        })

        return events

    def _scrape_feed(self, retry_count: int = 0) -> tuple:
        """Fetch and parse the FinSMEs RSS feed.

        Returns: (events, error_message) - error_message is None on success
        """
        events = []
        max_retries = 1
        error_msg = None

        try:
            # Use browser-like headers to avoid 403
            response = self.session.get(
                self.feed_url,
                timeout=self.timeout,
                headers=self.BROWSER_HEADERS
            )
            response.raise_for_status()

            root = ET.fromstring(response.content)

            # WordPress RSS feeds use standard <item> elements
            items = root.findall('.//item')

            for item in items:
                event = self._process_entry(item)
                if event:
                    events.append(event)

        except (Timeout, ReadTimeout, ConnectTimeout) as e:
            if retry_count < max_retries:
                print(f"Timeout on FinSMEs, waiting 60s and retrying...")
                time.sleep(60)
                return self._scrape_feed(retry_count + 1)
            else:
                error_msg = f"Timeout: {e}"
                print(f"Error fetching FinSMEs feed: {e} (after retry)")

        except Exception as e:
            error_msg = str(e)[:200]
            print(f"Error fetching FinSMEs feed: {e}")

        return events, error_msg

    def _process_entry(self, item: ET.Element) -> Optional[TriggerEvent]:
        """Process a single FinSMEs RSS feed entry."""
        title = self._get_text(item, 'title') or ''
        link = self._get_text(item, 'link') or ''
        raw_description = self._get_text(item, 'description') or ''

        # Also check content:encoded for full article text
        content_encoded = self._get_text(item, '{http://purl.org/rss/1.0/modules/content/}encoded') or ''

        description = strip_html(raw_description)
        full_content = strip_html(content_encoded)

        # Combine for analysis
        full_text = f"{title} {description} {full_content}"

        # FinSMEs primarily covers funding and M&A - detect event type
        event_type = self._detect_finsmes_event_type(title, full_text)

        # If no relevant event type detected, skip
        if not event_type:
            return None

        # Extract funding amount from title/description
        funding_amount = self._extract_funding_amount(full_text)

        # Check territory match
        in_territory, matched_regions = self.matches_territory(full_text)

        # Check target company
        matches_company, company_name = self.matches_target_company(full_text)

        # Skip excluded international locations
        if self.is_excluded_location(full_text):
            return None

        # Check industry match
        matches_target_industry, matches_excluded = self.matches_industry(full_text)
        if matches_excluded:
            return None

        # Skip public companies
        if self.is_public_company(full_text):
            return None

        # Must match territory OR be a target company
        if self.require_territory_match:
            if not (in_territory or matches_company):
                return None

        # Calculate relevance
        relevance = self.calculate_relevance_score(
            event_type,
            matched_regions,
            matches_target_industry,
            matches_company
        )

        # Boost for FinSMEs (direct funding/M&A reporting source)
        relevance = min(relevance + 10, 100)

        # Parse published date
        published = self._parse_date(item)

        # Extract company name from title if not a target company
        extracted_company = company_name or self._extract_finsmes_company(title)
        person_name, person_title = self.extract_person_info(full_text)

        # Get matched keywords
        matched_keywords = self._get_matched_keywords(full_text, event_type)

        # Build description
        desc_text = description[:500] if description else full_content[:500] if full_content else None
        if funding_amount:
            desc_text = f"Funding: {funding_amount} | {desc_text}" if desc_text else f"Funding: {funding_amount}"

        return TriggerEvent(
            id=self.generate_event_id(link, title),
            title=title,
            event_type=event_type,
            source=EventSource.OTHER,
            source_name='FinSMEs',
            url=link,
            published_date=published,
            company_name=extracted_company,
            description=desc_text,
            person_name=person_name,
            person_title=person_title,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=relevance
        )

    def _detect_finsmes_event_type(self, title: str, full_text: str) -> Optional[EventType]:
        """Detect event type with FinSMEs-specific patterns.

        FinSMEs titles typically follow patterns like:
        - "CompanyName Raises $XM in Series A Funding"
        - "CompanyName Completes Acquisition of TargetName"
        - "CompanyName Secures $XM in Funding"
        - "CompanyName Closes $XM Series B Round"
        """
        # First check excluded content
        if self.is_excluded_content(full_text):
            return None

        title_lower = title.lower()
        text_lower = full_text.lower()

        # FinSMEs-specific funding patterns (very common on this site)
        funding_patterns = [
            r'raises?\s+\$[\d,.]+[mk]?\s*(?:million|m\b)',
            r'secures?\s+\$[\d,.]+[mk]?\s*(?:million|m\b)',
            r'closes?\s+\$[\d,.]+[mk]?\s*(?:million|m\b)',
            r'series\s+[a-f]\b',
            r'seed\s+(?:round|funding)',
            r'funding\s+round',
            r'venture\s+(?:capital|funding)',
            r'growth\s+(?:equity|capital|funding)',
            r'in\s+(?:new\s+)?funding',
            r'\$[\d,.]+[mk]?\s*(?:million|m\b)\s*(?:in\s+)?(?:funding|investment|financing|round)',
        ]

        for pattern in funding_patterns:
            if re.search(pattern, title_lower):
                return EventType.FUNDING

        # M&A patterns
        ma_patterns = [
            r'acquires?\b',
            r'acquisition\b',
            r'merger\b',
            r'to\s+acquire\b',
            r'completes?\s+acquisition',
            r'buyout\b',
            r'takeover\b',
        ]

        for pattern in ma_patterns:
            if re.search(pattern, title_lower):
                return EventType.MERGER_ACQUISITION

        # Fall back to base class detection for the full text
        return self.detect_event_type(full_text)

    def _extract_funding_amount(self, text: str) -> Optional[str]:
        """Extract funding amount from text (e.g., '$10M', '$5.2 Million')."""
        patterns = [
            r'(\$[\d,.]+\s*(?:billion|million|B|M)\b)',
            r'(\$[\d,.]+[BbMm]\b)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def _extract_finsmes_company(self, title: str) -> Optional[str]:
        """Extract company name from FinSMEs-style titles.

        FinSMEs titles often start with the company name:
        - "Acme Corp Raises $10M..."
        - "Beta Inc. Completes Acquisition..."
        """
        # Match company name at start of title before action verb
        action_verbs = r'(?:Raises?|Secures?|Closes?|Completes?|Acquires?|Announces?|Launches?|Receives?|Gets?|Lands?|Nabs?|Bags?|Snags?|Wins?)'
        match = re.match(rf'^(.+?)\s+{action_verbs}\b', title)
        if match:
            company = match.group(1).strip()
            # Sanity check: not too long, not too short
            if 2 < len(company) < 80:
                return company

        return self.extract_company_name(title)

    def _get_text(self, elem: ET.Element, tag: str) -> Optional[str]:
        """Get text content of a child element."""
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return None

    def _parse_date(self, item: ET.Element) -> datetime:
        """Parse the published date from a feed entry."""
        for tag in ['pubDate', 'published', 'updated', 'date']:
            date_str = self._get_text(item, tag)
            if date_str:
                try:
                    return parsedate_to_datetime(date_str)
                except Exception:
                    try:
                        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    except Exception:
                        pass
        return datetime.now(timezone.utc)

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

        return matched[:5]
