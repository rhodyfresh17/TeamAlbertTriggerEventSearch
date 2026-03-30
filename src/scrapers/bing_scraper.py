"""Bing News API scraper for trigger events.

Complements Google News by surfacing different results for the same queries.
Free tier: 1,000 calls/month.
"""

from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


class BingNewsScraper(BaseScraper):
    """Scraper for Bing News Search API."""

    DEFAULT_ENDPOINT = "https://api.bing.microsoft.com/v7.0/news/search"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        bing_config = config.get('sources', {}).get('bing_news', {})
        self.enabled = bing_config.get('enabled', False)
        self.api_key = bing_config.get('api_key', '')
        self.endpoint = bing_config.get('endpoint', self.DEFAULT_ENDPOINT)
        self.source_statuses = []

    def scrape(self) -> List[TriggerEvent]:
        """Scrape Bing News for trigger events."""
        self.source_statuses = []

        if not self.enabled or not self.api_key:
            return []

        events = []
        errors = 0
        total_queries = 0

        queries = self._build_search_queries()

        for query, event_type_hint, skip_territory_filter in queries:
            total_queries += 1
            try:
                query_events = self._search_query(query, event_type_hint, skip_territory_filter)
                events.extend(query_events)
                self.delay_request()
            except Exception as e:
                errors += 1
                print(f"Error searching Bing News for '{query}': {e}")

        # Track status
        if errors == 0:
            status = 'success'
            error_msg = None
        elif errors < total_queries:
            status = 'partial'
            error_msg = f"{errors}/{total_queries} queries failed"
        else:
            status = 'error'
            error_msg = "All queries failed"

        self.source_statuses.append({
            'source_name': 'Bing News',
            'source_type': 'bing_news',
            'status': status,
            'error_message': error_msg,
            'events_found': len(events)
        })

        return events

    def _build_search_queries(self) -> List[tuple]:
        """Build search queries for Bing News.

        Returns list of (query, event_type_hint, skip_territory_filter) tuples.
        Uses fewer queries than Google News since Bing API has monthly limits.
        """
        queries = []

        # CFO hire queries
        cfo_terms = [
            'CFO appointed',
            'new CFO hired',
            '"Chief Financial Officer" appointed',
            '"names CFO"',
            '"VP Finance" hired',
        ]
        for term in cfo_terms:
            queries.append((term, EventType.CFO_HIRE, False))

        # LinkedIn-specific CFO queries (Bing indexes LinkedIn well)
        linkedin_cfo_queries = [
            ('site:linkedin.com CFO appointed', EventType.CFO_HIRE, True),
            ('site:linkedin.com "excited to announce" CFO', EventType.CFO_HIRE, True),
            ('site:linkedin.com "started a new position" CFO', EventType.CFO_HIRE, True),
            ('site:linkedin.com "Chief Financial Officer"', EventType.CFO_HIRE, True),
            ('site:linkedin.com "happy to share" CFO', EventType.CFO_HIRE, True),
            ('site:linkedin.com "VP of Finance" joined', EventType.CFO_HIRE, True),
            ('site:linkedin.com "Head of Finance" "new role"', EventType.CFO_HIRE, True),
            ('site:linkedin.com/posts CFO appointed', EventType.CFO_HIRE, True),
            ('site:linkedin.com/pulse CFO', EventType.CFO_HIRE, True),
        ]
        queries.extend(linkedin_cfo_queries)

        # M&A queries
        ma_terms = [
            'acquisition announced',
            'company acquired merger',
            '"merger agreement"',
        ]
        for term in ma_terms:
            queries.append((term, EventType.MERGER_ACQUISITION, False))

        # LinkedIn M&A
        queries.append(('site:linkedin.com acquisition announced', EventType.MERGER_ACQUISITION, True))
        queries.append(('site:linkedin.com "pleased to announce" acquisition', EventType.MERGER_ACQUISITION, True))

        # Funding queries
        queries.append(('site:linkedin.com funding round raised', EventType.FUNDING, True))

        # Industry-specific CFO queries
        industries = ['healthcare', 'construction', 'insurance', 'hospitality']
        for industry in industries:
            queries.append((f'{industry} CFO appointed', EventType.CFO_HIRE, False))

        return queries

    def _search_query(
        self,
        query: str,
        event_type_hint: Optional[EventType],
        skip_territory_filter: bool = False
    ) -> List[TriggerEvent]:
        """Search Bing News for a specific query."""
        events = []

        headers = {
            'Ocp-Apim-Subscription-Key': self.api_key,
        }
        params = {
            'q': query,
            'count': 10,
            'mkt': 'en-US',
            'freshness': 'Week',
            'sortBy': 'Date',
        }

        try:
            response = self.session.get(
                self.endpoint,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()

            for article in data.get('value', []):
                event = self._process_article(article, event_type_hint, skip_territory_filter)
                if event:
                    events.append(event)

        except Exception as e:
            print(f"Error querying Bing News API: {e}")

        return events

    def _process_article(
        self,
        article: Dict[str, Any],
        event_type_hint: Optional[EventType],
        skip_territory_filter: bool = False
    ) -> Optional[TriggerEvent]:
        """Process a single Bing News article."""
        title = article.get('name', '')
        url = article.get('url', '')
        description = article.get('description', '')
        source_name = article.get('provider', [{}])[0].get('name', 'Bing News')

        full_text = f"{title} {description}"

        # Detect event type
        event_type = self.detect_event_type(full_text)
        if not event_type:
            event_type = event_type_hint
        if not event_type:
            return None

        # Check territory
        in_territory, matched_regions = self.matches_territory(full_text)

        # Check industry
        matches_target_industry, matches_excluded = self.matches_industry(full_text)
        if matches_excluded:
            return None

        # Skip public companies
        if self.is_public_company(full_text):
            return None

        # Check target company
        matches_company, company_name = self.matches_target_company(full_text)

        # Check excluded locations
        if self.is_excluded_location(full_text):
            return None

        # Territory filtering
        if not skip_territory_filter:
            if self.require_territory_match:
                if not (in_territory or matches_company):
                    return None
            else:
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
        published = self._parse_date(article)

        # Extract info
        extracted_company = company_name or self.extract_company_name(full_text)
        person_name, person_title = self.extract_person_info(full_text)

        # Get matched keywords
        matched_keywords = self._get_matched_keywords(full_text, event_type)

        return TriggerEvent(
            id=self.generate_event_id(url, title),
            title=title,
            event_type=event_type,
            source=EventSource.OTHER,
            source_name=source_name,
            url=url,
            published_date=published,
            company_name=extracted_company,
            description=description[:500] if description else None,
            person_name=person_name,
            person_title=person_title,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=relevance
        )

    def _parse_date(self, article: Dict[str, Any]) -> datetime:
        """Parse date from Bing News article."""
        date_str = article.get('datePublished', '')
        if date_str:
            try:
                return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
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
