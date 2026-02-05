"""Base scraper class with common functionality."""

import hashlib
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Dict, Any

import requests

from ..models import TriggerEvent, EventType, EventSource


class BaseScraper(ABC):
    """Base class for all scrapers."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': config.get('scraper', {}).get(
                'user_agent',
                'Mozilla/5.0 (compatible; SalesTerritoryBot/1.0)'
            )
        })
        self.timeout = config.get('scraper', {}).get('timeout', 30)
        self.request_delay = config.get('scraper', {}).get('request_delay', 2)

        # Load territory config
        self.territory = config.get('territory', {})
        self.regions = [r.lower() for r in (self.territory.get('regions') or [])]
        self.cities = [c.lower() for c in (self.territory.get('cities') or [])]
        self.target_companies = [c.lower() for c in (self.territory.get('target_companies') or []) if c]
        self.industries = [i.lower() for i in (self.territory.get('industries') or [])]
        self.excluded_industries = [i.lower() for i in (self.territory.get('excluded_industries') or [])]

        # Load keywords
        self.keywords = config.get('keywords', {})
        self.exec_hire_keywords = [k.lower() for k in self.keywords.get('executive_hires', [])]
        self.ma_keywords = [k.lower() for k in self.keywords.get('mergers_acquisitions', [])]
        self.funding_keywords = [k.lower() for k in self.keywords.get('funding_events', [])]

        # Load company filters (for mid-market private companies)
        self.company_filters = self.territory.get('company_filters', {})
        self.exclude_public = self.company_filters.get('exclude_public_companies', True)
        self.public_indicators = [
            i.lower() for i in (self.company_filters.get('public_company_indicators') or [])
        ]
        self.excluded_public_companies = [
            c.lower() for c in (self.company_filters.get('excluded_public_companies') or [])
        ]
        self.target_size_indicators = [
            i.lower() for i in (self.company_filters.get('target_size_indicators') or [])
        ]

    @abstractmethod
    def scrape(self) -> List[TriggerEvent]:
        """Scrape and return list of trigger events."""
        pass

    def generate_event_id(self, url: str, title: str) -> str:
        """Generate unique ID for an event."""
        content = f"{url}:{title}"
        return hashlib.md5(content.encode()).hexdigest()

    def detect_event_type(self, text: str) -> Optional[EventType]:
        """Detect the type of trigger event from text."""
        text_lower = text.lower()

        # Check for CFO specifically first
        cfo_patterns = ['cfo', 'chief financial officer']
        if any(pattern in text_lower for pattern in cfo_patterns):
            return EventType.CFO_HIRE

        # Check for executive hires
        if any(kw in text_lower for kw in self.exec_hire_keywords):
            return EventType.EXECUTIVE_HIRE

        # Check for M&A
        if any(kw in text_lower for kw in self.ma_keywords):
            return EventType.MERGER_ACQUISITION

        # Check for funding
        if any(kw in text_lower for kw in self.funding_keywords):
            return EventType.FUNDING

        return None

    def matches_territory(self, text: str) -> tuple[bool, List[str]]:
        """Check if text mentions locations in our territory."""
        text_lower = text.lower()
        matched = []

        # Check regions
        for region in self.regions:
            if region in text_lower:
                matched.append(region)

        # Check cities
        for city in self.cities:
            if city in text_lower:
                matched.append(city)

        return len(matched) > 0, matched

    def matches_industry(self, text: str) -> tuple[bool, bool]:
        """
        Check if text matches target industries.
        Returns: (matches_target, matches_excluded)
        """
        text_lower = text.lower()

        # Check exclusions first
        for excluded in self.excluded_industries:
            if excluded in text_lower:
                return False, True

        # Check target industries
        for industry in self.industries:
            if industry in text_lower:
                return True, False

        return False, False

    def is_public_company(self, text: str) -> bool:
        """Check if text indicates a public company (to exclude)."""
        if not self.exclude_public:
            return False

        text_lower = text.lower()

        # Check for known large public companies by name
        for company in self.excluded_public_companies:
            if company in text_lower:
                return True

        # Check for public company indicators
        for indicator in self.public_indicators:
            if indicator in text_lower:
                return True

        return False

    def is_target_company_size(self, text: str) -> bool:
        """Check if text indicates a mid-market company (our target)."""
        text_lower = text.lower()
        for indicator in self.target_size_indicators:
            if indicator in text_lower:
                return True
        return False

    def matches_target_company(self, text: str) -> tuple[bool, Optional[str]]:
        """Check if text mentions a target company."""
        text_lower = text.lower()

        for company in self.target_companies:
            if company and company in text_lower:
                return True, company

        return False, None

    def calculate_relevance_score(
        self,
        event_type: EventType,
        matched_regions: List[str],
        matches_industry: bool,
        matches_company: bool
    ) -> float:
        """Calculate relevance score for an event."""
        score = 0.0

        # Event type scoring
        type_scores = {
            EventType.CFO_HIRE: 40,
            EventType.EXECUTIVE_HIRE: 30,
            EventType.MERGER_ACQUISITION: 35,
            EventType.FUNDING: 25,
            EventType.OTHER: 10
        }
        score += type_scores.get(event_type, 10)

        # Territory match scoring
        score += min(len(matched_regions) * 15, 30)

        # Industry match scoring
        if matches_industry:
            score += 20

        # Target company scoring (highest priority)
        if matches_company:
            score += 50

        return min(score, 100)

    def extract_company_name(self, text: str) -> Optional[str]:
        """Try to extract company name from text."""
        # Common patterns for company mentions
        patterns = [
            r'([A-Z][A-Za-z0-9\s&]+(?:Inc\.|Corp\.|LLC|Ltd\.|Co\.))',
            r'([A-Z][A-Za-z0-9\s&]+) (?:announces|appoints|names|hires)',
            r'(?:at|joins|of) ([A-Z][A-Za-z0-9\s&]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                company = match.group(1).strip()
                if len(company) > 3 and len(company) < 100:
                    return company

        return None

    def extract_person_info(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """Extract person name and title from text."""
        # Common title patterns
        title_pattern = r'(?:as|named|appointed|new)\s+((?:Chief\s+)?(?:Financial|Executive|Operating|Technology)\s+Officer|CFO|CEO|COO|CTO|VP\s+\w+|President|Director)'
        title_match = re.search(title_pattern, text, re.IGNORECASE)
        title = title_match.group(1) if title_match else None

        # Name patterns (usually before "as" or "named")
        name_pattern = r'([A-Z][a-z]+\s+[A-Z][a-z]+)(?:\s+(?:as|named|appointed|joins))'
        name_match = re.search(name_pattern, text)
        name = name_match.group(1) if name_match else None

        return name, title

    def delay_request(self):
        """Add delay between requests to be respectful."""
        time.sleep(self.request_delay)
