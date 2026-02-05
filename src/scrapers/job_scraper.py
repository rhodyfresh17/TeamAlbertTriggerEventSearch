"""Job site scraper for finance leadership positions."""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any
from urllib.parse import quote_plus

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


class JobScraper(BaseScraper):
    """Scraper for job postings indicating companies hiring finance leaders."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        # Job-specific config
        job_config = config.get('job_search', {})
        self.enabled = job_config.get('enabled', True)

        # Job titles to search for
        self.job_titles = job_config.get('titles', [
            'CFO',
            'Chief Financial Officer',
            'Controller',
            'Finance Director',
            'Director of Finance',
            'VP Finance',
            'Vice President Finance',
            'Head of Finance',
        ])

        # Location queries for Indeed RSS (state abbreviations work well)
        self.location_queries = job_config.get('locations', [
            'New York, NY',
            'Boston, MA',
            'Philadelphia, PA',
            'New Jersey',
            'Connecticut',
            'Maryland',
            'Virginia',
            'Washington DC',
            'Florida',
            'Georgia',
            'North Carolina',
            'Toronto, ON',
            'Montreal, QC',
        ])

        # Indeed RSS base URL
        self.indeed_rss_base = "https://www.indeed.com/rss"

    def scrape(self) -> List[TriggerEvent]:
        """Scrape job sites for finance leadership positions."""
        if not self.enabled:
            return []

        events = []

        # Scrape Indeed RSS feeds
        events.extend(self._scrape_indeed())

        return events

    def _scrape_indeed(self) -> List[TriggerEvent]:
        """Scrape Indeed RSS feeds for job postings."""
        events = []

        for title in self.job_titles[:4]:  # Limit to avoid rate limiting
            for location in self.location_queries[:5]:  # Limit locations too
                try:
                    feed_events = self._fetch_indeed_feed(title, location)
                    events.extend(feed_events)
                    self.delay_request()
                except Exception as e:
                    print(f"    Error fetching Indeed {title} in {location}: {e}")

        return events

    def _fetch_indeed_feed(self, job_title: str, location: str) -> List[TriggerEvent]:
        """Fetch and parse an Indeed RSS feed."""
        events = []

        # Build Indeed RSS URL
        # Format: https://www.indeed.com/rss?q=CFO&l=New+York,+NY
        query = quote_plus(job_title)
        loc = quote_plus(location)
        url = f"{self.indeed_rss_base}?q={query}&l={loc}&sort=date"

        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code != 200:
                return []

            root = ET.fromstring(response.content)

            # Parse RSS items
            for item in root.findall('.//item'):
                try:
                    event = self._parse_job_item(item, job_title, location)
                    if event:
                        events.append(event)
                except Exception as e:
                    continue

        except Exception as e:
            print(f"    Error parsing Indeed feed: {e}")

        return events

    def _parse_job_item(self, item, search_title: str, search_location: str) -> TriggerEvent:
        """Parse a single job posting from RSS."""
        title_elem = item.find('title')
        link_elem = item.find('link')
        pub_date_elem = item.find('pubDate')
        desc_elem = item.find('description')

        if title_elem is None or link_elem is None:
            return None

        title = title_elem.text or ''
        url = link_elem.text or ''
        description = desc_elem.text if desc_elem is not None else ''

        # Parse publication date
        pub_date = datetime.now()
        if pub_date_elem is not None and pub_date_elem.text:
            try:
                # Indeed uses RFC 2822 format
                from email.utils import parsedate_to_datetime
                pub_date = parsedate_to_datetime(pub_date_elem.text)
            except:
                pass

        # Extract company name from title (format: "Job Title - Company Name - Location")
        company_name = self._extract_company_from_title(title)

        # Skip if it's a public company or excluded
        full_text = f"{title} {description}"
        if self.is_public_company(full_text):
            return None

        # Check for excluded industries
        _, is_excluded = self.matches_industry(full_text)
        if is_excluded:
            return None

        # Check territory match
        matches_territory, matched_regions = self.matches_territory(full_text)

        # For job postings, use the search location as matched region if no explicit match
        if not matched_regions and search_location:
            matched_regions = [search_location]
            matches_territory = True

        # If strict territory matching is on, skip non-matches
        if self.require_territory_match and not matches_territory:
            return None

        # Check for industry match
        matches_ind, _ = self.matches_industry(full_text)

        # Determine matched keywords
        matched_keywords = []
        for job_title in self.job_titles:
            if job_title.lower() in title.lower():
                matched_keywords.append(job_title)

        # Calculate relevance score
        score = self._calculate_job_relevance(
            title=title,
            matched_regions=matched_regions,
            matches_industry=matches_ind,
            matched_keywords=matched_keywords
        )

        # Generate event
        event_id = self.generate_event_id(url, title)

        return TriggerEvent(
            id=event_id,
            title=f"Hiring: {title}",
            event_type=EventType.CFO_HIRE,  # Use CFO_HIRE for finance leadership roles
            source=EventSource.OTHER,
            source_name="Indeed Jobs",
            url=url,
            published_date=pub_date,
            company_name=company_name,
            company_location=search_location,
            description=description[:500] if description else None,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=score
        )

    def _extract_company_from_title(self, title: str) -> str:
        """Extract company name from job title string."""
        # Indeed format: "Job Title - Company Name - Location"
        parts = title.split(' - ')
        if len(parts) >= 2:
            return parts[1].strip()

        # Try other patterns
        match = re.search(r'at\s+([^-]+)', title, re.IGNORECASE)
        if match:
            return match.group(1).strip()

        return None

    def _calculate_job_relevance(
        self,
        title: str,
        matched_regions: List[str],
        matches_industry: bool,
        matched_keywords: List[str]
    ) -> float:
        """Calculate relevance score for a job posting."""
        score = 0.0
        title_lower = title.lower()

        # CFO/Chief Financial Officer is highest value
        if 'cfo' in title_lower or 'chief financial officer' in title_lower:
            score += 45

        # Controller is high value
        elif 'controller' in title_lower:
            score += 40

        # Director/VP of Finance
        elif 'director' in title_lower or 'vp' in title_lower or 'vice president' in title_lower:
            score += 35

        # Head of Finance
        elif 'head of finance' in title_lower:
            score += 35

        # Other finance leadership
        else:
            score += 25

        # Territory match
        score += min(len(matched_regions) * 10, 20)

        # Industry match
        if matches_industry:
            score += 15

        # Keyword matches
        score += min(len(matched_keywords) * 5, 15)

        return min(score, 100)
