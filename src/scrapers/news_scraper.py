"""Google News RSS scraper for trigger events."""

import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from email.utils import parsedate_to_datetime

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


class GoogleNewsScraper(BaseScraper):
    """Scraper for Google News RSS feeds."""

    BASE_URL = "https://news.google.com/rss/search?q="

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        google_config = config.get('sources', {}).get('google_news', {})
        self.enabled = google_config.get('enabled', True)
        self.base_url = google_config.get('base_url', self.BASE_URL)
        self.source_statuses = []  # Track status

    def scrape(self) -> List[TriggerEvent]:
        """Scrape Google News for trigger events in territory."""
        self.source_statuses = []  # Reset statuses

        if not self.enabled:
            return []

        events = []
        errors = 0
        total_queries = 0
        # Raw result items parsed across all queries, before the article
        # classifier / territory gate (v2 Phase 2, 2026-09-07). Bumped by
        # _scrape_query so the per-query error path leaves it untouched.
        self._items_fetched = 0

        # Build search queries for different event types
        queries = self._build_search_queries()

        for query, event_type_hint in queries:
            total_queries += 1
            try:
                feed_events = self._scrape_query(query, event_type_hint)
                events.extend(feed_events)
                self.delay_request()
            except Exception as e:
                errors += 1
                print(f"Error scraping Google News for '{query}': {e}")

        # Track Google News as a single source
        if errors == 0:
            status = 'success'
            error_msg = None
        elif errors < total_queries:
            status = 'partial'
            error_msg = f"{errors}/{total_queries} queries failed"
        else:
            status = 'error'
            error_msg = "All queries failed"

        items_fetched = 0 if status == 'error' else self._items_fetched
        self.source_statuses.append({
            'source_name': 'Google News',
            'source_type': 'google_news',
            'status': status,
            'error_message': error_msg,
            'events_found': len(events),
            'items_fetched': items_fetched,
            'filtered_out': max(items_fetched - len(events), 0),
        })

        return events

    def _build_search_queries(self) -> List[tuple[str, Optional[EventType]]]:
        """Build search queries combining keywords with territory.

        Returns list of (query, event_type_hint) tuples. Every query goes
        through the territory filter — the old `skip_territory_filter`
        bypass (LinkedIn / PE queries) is gone (v2, 2026-09-06): reps see
        in-territory accounts only, and out-of-territory hits only cost
        research downstream.

        The hint is informational: `_process_entry` requires
        `detect_event_type()` to positively classify the article and never
        falls back to the hint.
        """
        queries = []

        # Key regions to search (limit to avoid too many requests)
        key_regions = ['New York', 'Boston', 'Toronto', 'Philadelphia', 'Charlotte']

        # CFO hire queries
        cfo_terms = ['CFO appointed', 'new CFO', 'names CFO', 'CFO hire']
        for term in cfo_terms:
            queries.append((term, EventType.CFO_HIRE))

        # M&A queries with region
        ma_terms = ['acquisition announced', 'company acquired', 'merger agreement']
        for term in ma_terms:
            for region in key_regions[:3]:  # Limit regions
                queries.append((f'{term} {region}', EventType.MERGER_ACQUISITION))

        # Industry-specific queries — TARGET verticals only (Financial Services,
        # Nonprofits, Consumer Services). Previously queried healthcare/hospital/
        # construction/restaurant-franchise, all of which are blocked downstream —
        # pure wasted fetch + filter cycles (audit 2026-07-16).
        industries = ['insurance', 'credit union', 'wealth management',
                      'private equity', 'nonprofit', 'foundation',
                      'auto dealership', 'real estate brokerage']
        for industry in industries:
            queries.append((f'{industry} CFO', EventType.CFO_HIRE))
            queries.append((f'{industry} acquisition', EventType.MERGER_ACQUISITION))
        # New-Controller trigger — a stated top trigger with no query until now
        queries.append(('new controller appointed', EventType.CFO_HIRE))
        queries.append(('"VP of Finance" appointed', EventType.CFO_HIRE))

        # Crunchbase-sourced news (funding rounds, acquisitions)
        crunchbase_queries = [
            ('site:crunchbase.com series funding', EventType.FUNDING),
            ('site:crunchbase.com acquisition', EventType.MERGER_ACQUISITION),
            ('site:news.crunchbase.com raises', EventType.FUNDING),
            ('site:news.crunchbase.com acquired', EventType.MERGER_ACQUISITION),
        ]
        queries.extend(crunchbase_queries)

        # Private equity portfolio company moves
        pe_queries = [
            ('"private equity" "portfolio company" CFO', EventType.CFO_HIRE),
            ('"PE-backed" CFO appointed', EventType.CFO_HIRE),
            ('"platform company" CFO', EventType.CFO_HIRE),
            ('"add-on acquisition"', EventType.MERGER_ACQUISITION),
            ('"bolt-on acquisition"', EventType.MERGER_ACQUISITION),
        ]
        queries.extend(pe_queries)

        # Companies in transition (interim/fractional = opportunity)
        transition_queries = [
            ('"interim CFO"', EventType.CFO_HIRE),
            ('"fractional CFO"', EventType.CFO_HIRE),
            ('"acting CFO"', EventType.CFO_HIRE),
            ('"CFO transition"', EventType.CFO_HIRE),
            ('"CFO search"', EventType.CFO_HIRE),
        ]
        queries.extend(transition_queries)

        return queries

    def _scrape_query(
        self,
        query: str,
        event_type_hint: Optional[EventType],
    ) -> List[TriggerEvent]:
        """Scrape Google News for a specific query."""
        events = []

        # Build URL
        encoded_query = urllib.parse.quote(query)
        url = f"{self.base_url}{encoded_query}&hl=en-US&gl=US&ceid=US:en"

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            root = ET.fromstring(response.content)
            items = root.findall('.//item')[:10]  # Limit entries per query
            self._items_fetched = getattr(self, '_items_fetched', 0) + len(items)

            for item in items:
                event = self._process_entry(item, event_type_hint)
                if event:
                    events.append(event)

        except Exception as e:
            print(f"Error parsing Google News feed: {e}")

        return events

    def _process_entry(
        self,
        item: ET.Element,
        event_type_hint: Optional[EventType],
    ) -> Optional[TriggerEvent]:
        """Process a single news entry."""
        title_elem = item.find('title')
        link_elem = item.find('link')
        desc_elem = item.find('description')

        title = title_elem.text if title_elem is not None else ''
        link = link_elem.text if link_elem is not None else ''
        summary = desc_elem.text if desc_elem is not None else ''

        # Google News titles often have source appended
        # Format: "Article Title - Source Name"
        source_name = None
        if ' - ' in title:
            parts = title.rsplit(' - ', 1)
            if len(parts) == 2:
                title = parts[0]
                source_name = parts[1]

        full_text = f"{title} {summary}"

        # Detect event type from the article itself. The query hint is NOT a
        # fallback (v2, 2026-09-06): an article that does not read as a hire /
        # M&A / funding event is not a trigger, whatever query found it.
        # (detect_event_type returns a single type, so there is no tie for
        # the hint to break; it stays in the signature for that purpose.)
        event_type = self.detect_event_type(full_text)
        if not event_type:
            return None

        # Check territory
        in_territory, matched_regions = self.matches_territory(full_text)

        # Check industry
        matches_target_industry, matches_excluded = self.matches_industry(full_text)

        if matches_excluded:
            return None

        # Skip public companies (we target mid-market private)
        if self.is_public_company(full_text):
            return None

        # Check target company
        matches_company, company_name = self.matches_target_company(full_text)

        # Check for excluded international locations
        if self.is_excluded_location(full_text):
            return None

        # TERRITORY FILTERING (applies to every query — no bypass)
        # STRICT FILTERING: Require territory match OR target company
        # Industry alone is NOT sufficient (avoids international companies)
        if self.require_territory_match:
            if not (in_territory or matches_company):
                return None
        else:
            # Fallback to looser filtering if disabled
            if not (in_territory or matches_target_industry or matches_company):
                return None

        # Calculate relevance
        relevance = self.calculate_relevance_score(
            event_type,
            matched_regions,
            matches_target_industry,
            matches_company
        )

        # Parse date
        published = self._parse_date(item)

        # Extract info
        extracted_company = company_name or self.extract_company_name(full_text)
        person_name, person_title = self.extract_person_info(full_text)

        # Get matched keywords
        matched_keywords = self._get_matched_keywords(full_text, event_type)

        return TriggerEvent(
            id=self.generate_event_id(link, title),
            title=title,
            event_type=event_type,
            source=EventSource.GOOGLE_NEWS,
            source_name=source_name or "Google News",
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

    def _parse_date(self, item: ET.Element) -> datetime:
        """Parse date from news entry."""
        pub_date = item.find('pubDate')
        if pub_date is not None and pub_date.text:
            try:
                return parsedate_to_datetime(pub_date.text)
            except Exception:
                pass

        return datetime.now(timezone.utc)

    def _get_matched_keywords(self, text: str, event_type: EventType) -> List[str]:
        """Get matched keywords."""
        text_lower = text.lower()
        matched = []

        keyword_map = {
            EventType.CFO_HIRE: self.exec_hire_keywords,
            EventType.EXECUTIVE_HIRE: self.exec_hire_keywords,
            EventType.MERGER_ACQUISITION: self.ma_keywords,
            EventType.FUNDING: self.funding_keywords,
        }

        keywords = keyword_map.get(event_type, [])
        for kw in keywords:
            if kw in text_lower:
                matched.append(kw)

        return matched[:5]
