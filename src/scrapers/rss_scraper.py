"""RSS feed scraper for business news and PR wires."""

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
    # Remove HTML tags
    clean = re.sub(r'<[^>]+>', '', text)
    # Decode common HTML entities
    clean = clean.replace('&nbsp;', ' ')
    clean = clean.replace('&amp;', '&')
    clean = clean.replace('&lt;', '<')
    clean = clean.replace('&gt;', '>')
    clean = clean.replace('&quot;', '"')
    clean = clean.replace('&#39;', "'")
    # Clean up whitespace
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


class RSSScraper(BaseScraper):
    """Scraper for RSS feeds from PR wires and news sources."""

    SOURCE_MAPPING = {
        'business wire': EventSource.BUSINESS_WIRE,
        'pr newswire': EventSource.PR_NEWSWIRE,
        'globe newswire': EventSource.GLOBE_NEWSWIRE,
        'globenewswire': EventSource.GLOBE_NEWSWIRE,
        'wired': EventSource.OTHER,
        'fox business': EventSource.OTHER,
        'cnbc': EventSource.OTHER,
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.feeds = config.get('sources', {}).get('rss_feeds', [])
        self.source_statuses = []  # Track status of each feed

    def scrape(self) -> List[TriggerEvent]:
        """Scrape all configured RSS feeds."""
        events = []
        self.source_statuses = []  # Reset statuses

        for feed_config in self.feeds:
            if not feed_config.get('enabled', True):
                continue

            feed_name = feed_config.get('name', 'Unknown')
            feed_url = feed_config.get('url')

            if not feed_url:
                continue

            feed_events, error_msg = self._scrape_feed(feed_url, feed_name)
            events.extend(feed_events)

            self.source_statuses.append({
                'source_name': feed_name,
                'source_type': 'rss_feed',
                'status': 'error' if error_msg else 'success',
                'error_message': error_msg,
                'events_found': len(feed_events)
            })

            self.delay_request()

        return events

    def _scrape_feed(self, url: str, feed_name: str, retry_count: int = 0) -> tuple:
        """Scrape a single RSS feed with retry on timeout.

        Returns: (events, error_message) - error_message is None on success
        """
        events = []
        max_retries = 1  # Retry once on timeout
        error_msg = None

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            # Parse XML
            root = ET.fromstring(response.content)

            # Handle both RSS and Atom feeds
            items = root.findall('.//item')  # RSS
            if not items:
                # Try Atom format
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                items = root.findall('.//atom:entry', ns)
                if not items:
                    items = root.findall('.//{http://www.w3.org/2005/Atom}entry')

            for item in items:
                event = self._process_entry(item, feed_name)
                if event:
                    events.append(event)

        except (Timeout, ReadTimeout, ConnectTimeout) as e:
            if retry_count < max_retries:
                print(f"Timeout on {feed_name}, waiting 60s and retrying...")
                time.sleep(60)
                return self._scrape_feed(url, feed_name, retry_count + 1)
            else:
                error_msg = f"Timeout: {e}"
                print(f"Error parsing feed {feed_name}: {e} (after retry)")

        except Exception as e:
            error_msg = str(e)[:200]
            print(f"Error parsing feed {feed_name}: {e}")

        return events, error_msg

    def _process_entry(self, item: ET.Element, feed_name: str) -> Optional[TriggerEvent]:
        """Process a single feed entry."""
        # Extract fields (handle both RSS and Atom)
        title = self._get_text(item, 'title') or ''
        link = self._get_text(item, 'link') or self._get_attr(item, 'link', 'href') or ''
        raw_summary = self._get_text(item, 'description') or self._get_text(item, 'summary') or ''

        # Handle Atom content
        if not raw_summary:
            content = item.find('{http://www.w3.org/2005/Atom}content')
            if content is not None and content.text:
                raw_summary = content.text

        # Strip HTML from summary
        summary = strip_html(raw_summary)

        # Combine title and summary for analysis
        full_text = f"{title} {summary}"

        # STEP 1: Check if ANY dateline location is in our territory (HIGHEST PRIORITY)
        # If PR is from our territory, we want to see it - period.
        # Handles multiple locations like "NEW YORK and ARLINGTON, Va."
        dateline_locations = self.extract_dateline_locations(full_text)
        dateline_in_territory = False
        dateline_matched_location = None

        for dateline_city, dateline_state in dateline_locations:
            if dateline_city and dateline_city in self.cities:
                dateline_in_territory = True
                dateline_matched_location = dateline_city
                break
            if dateline_state and dateline_state in self.regions:
                dateline_in_territory = True
                dateline_matched_location = dateline_state
                break

        # Check territory match in body text
        in_territory, matched_regions = self.matches_territory(full_text)

        # Check target company
        matches_company, company_name = self.matches_target_company(full_text)

        # STEP 2: Detect event type (trigger events like M&A, CFO hire, funding)
        event_type = self.detect_event_type(full_text)

        # Track recommendation reasoning for stable targets
        recommendation_reasoning = None

        # Always hard-block excluded industries and public companies
        matches_target_industry, matches_excluded = self.matches_industry(full_text)
        if matches_excluded:
            return None
        if self.is_public_company(full_text):
            return None

        # STEP 3: Apply territory + trigger filtering
        if dateline_in_territory:
            # Dateline is in territory — require a trigger event OR known target company
            if not event_type and not matches_company:
                return None
            if not event_type:
                event_type = EventType.STABLE_TARGET
                extracted_company = company_name or self.extract_company_name(full_text)
                matched_industries = self.get_matched_industries(full_text)
                location_info = dateline_matched_location or "territory"
                recommendation_reasoning = f"PR from {location_info.title()}"
                if extracted_company:
                    recommendation_reasoning = f"Company: {extracted_company} | {recommendation_reasoning}"
                if matched_industries:
                    recommendation_reasoning += f" | Industry: {', '.join(matched_industries[:2])}"
        else:
            # NOT in territory by dateline - apply stricter filtering

            # Skip excluded international locations
            if self.is_excluded_location(full_text):
                return None

            # If no trigger event, skip
            if not event_type:
                is_pe_backed = self._is_pe_backed(full_text)
                if not (matches_company or (is_pe_backed and in_territory)):
                    return None
                event_type = EventType.STABLE_TARGET

            # Must match territory OR be a named target company
            is_pe_backed = self._is_pe_backed(full_text)
            is_ma_event = event_type == EventType.MERGER_ACQUISITION

            if self.require_territory_match:
                if not (in_territory or matches_company):
                    return None

        # Get industry match info (may not be set for dateline-in-territory)
        matched_industries = self.get_matched_industries(full_text)
        matches_target_industry = len(matched_industries) > 0

        # Calculate relevance
        relevance = self.calculate_relevance_score(
            event_type,
            matched_regions,
            matches_target_industry,
            matches_company
        )

        # Boost relevance for dateline matches
        if dateline_in_territory:
            relevance = min(relevance + 20, 100)

        # Determine source
        source = self._determine_source(feed_name)

        # Boost relevance for PR wire sources (direct company announcements)
        if source == EventSource.PR_NEWSWIRE:
            relevance = min(relevance + 15, 100)
        elif source == EventSource.BUSINESS_WIRE:
            relevance = min(relevance + 10, 100)
        elif source == EventSource.GLOBE_NEWSWIRE:
            relevance = min(relevance + 10, 100)

        # Parse published date
        published = self._parse_date(item)

        # Extract company name if not already done
        extracted_company = company_name or self.extract_company_name(full_text)
        person_name, person_title = self.extract_person_info(full_text)

        # Get matched keywords
        matched_keywords = self._get_matched_keywords(full_text, event_type)

        # Build description with reasoning for territory-matched stable targets
        is_stable_target = (event_type == EventType.STABLE_TARGET)
        if is_stable_target and recommendation_reasoning:
            description = f"📋 RECOMMENDATION: {recommendation_reasoning}\n\n{summary[:400] if summary else ''}"
        else:
            description = summary[:500] if summary else None

        return TriggerEvent(
            id=self.generate_event_id(link, title),
            title=title,
            event_type=event_type,
            source=source,
            source_name=feed_name,
            url=link,
            published_date=published,
            company_name=extracted_company,
            description=description,
            person_name=person_name,
            person_title=person_title,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=relevance
        )

    def _get_text(self, elem: ET.Element, tag: str) -> Optional[str]:
        """Get text content of a child element."""
        # Try without namespace
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()

        # Try with Atom namespace
        child = elem.find(f'{{http://www.w3.org/2005/Atom}}{tag}')
        if child is not None and child.text:
            return child.text.strip()

        return None

    def _get_attr(self, elem: ET.Element, tag: str, attr: str) -> Optional[str]:
        """Get attribute of a child element."""
        child = elem.find(tag)
        if child is not None:
            return child.get(attr)

        child = elem.find(f'{{http://www.w3.org/2005/Atom}}{tag}')
        if child is not None:
            return child.get(attr)

        return None

    def _parse_date(self, item: ET.Element) -> datetime:
        """Parse the published date from a feed entry."""
        # Try different date fields
        for tag in ['pubDate', 'published', 'updated', 'date']:
            date_str = self._get_text(item, tag)
            if date_str:
                try:
                    return parsedate_to_datetime(date_str)
                except Exception:
                    try:
                        # Try ISO format
                        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
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

    def _is_pe_backed(self, text: str) -> bool:
        """Check if text indicates a PE-backed company or deal."""
        text_lower = text.lower()
        pe_indicators = [
            'private equity',
            'pe-backed',
            'pe backed',
            '-backed',
            'portfolio company',
            'capital partners',
            'equity partners',
            'investment partners',
            'growth equity',
            'buyout',
            'lbo',
            'leveraged buyout',
            'sponsor-backed',
            'sponsor backed',
            'add-on acquisition',
            'bolt-on acquisition',
            'platform acquisition',
            'tuck-in acquisition',
        ]
        return any(indicator in text_lower for indicator in pe_indicators)

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
