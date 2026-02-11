"""Base scraper class with common functionality."""

import hashlib
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Dict, Any

import requests

from ..models import TriggerEvent, EventType, EventSource

# State abbreviation mapping for dateline parsing
STATE_ABBREVS = {
    'al': 'alabama', 'ak': 'alaska', 'az': 'arizona', 'ar': 'arkansas',
    'ca': 'california', 'co': 'colorado', 'ct': 'connecticut', 'de': 'delaware',
    'fl': 'florida', 'ga': 'georgia', 'hi': 'hawaii', 'id': 'idaho',
    'il': 'illinois', 'in': 'indiana', 'ia': 'iowa', 'ks': 'kansas',
    'ky': 'kentucky', 'la': 'louisiana', 'me': 'maine', 'md': 'maryland',
    'ma': 'massachusetts', 'mass': 'massachusetts', 'mi': 'michigan',
    'mn': 'minnesota', 'ms': 'mississippi', 'mo': 'missouri', 'mt': 'montana',
    'ne': 'nebraska', 'nv': 'nevada', 'nh': 'new hampshire', 'nj': 'new jersey',
    'nm': 'new mexico', 'ny': 'new york', 'nc': 'north carolina',
    'nd': 'north dakota', 'oh': 'ohio', 'ok': 'oklahoma', 'or': 'oregon',
    'pa': 'pennsylvania', 'ri': 'rhode island', 'sc': 'south carolina',
    'sd': 'south dakota', 'tn': 'tennessee', 'tx': 'texas', 'ut': 'utah',
    'vt': 'vermont', 'va': 'virginia', 'wa': 'washington', 'wv': 'west virginia',
    'wi': 'wisconsin', 'wy': 'wyoming', 'dc': 'washington dc',
    # Canadian provinces
    'on': 'ontario', 'ont': 'ontario', 'qc': 'quebec', 'que': 'quebec',
    'bc': 'british columbia', 'ab': 'alberta', 'mb': 'manitoba',
    'sk': 'saskatchewan', 'ns': 'nova scotia', 'nb': 'new brunswick',
    'nl': 'newfoundland', 'pe': 'prince edward island',
}


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

        # Load geographic exclusions (international locations to filter out)
        self.excluded_locations = [
            loc.lower() for loc in (self.territory.get('excluded_locations') or [])
        ]

        # Load content exclusions (irrelevant content types)
        self.excluded_content = [
            c.lower() for c in (self.territory.get('excluded_content') or [])
        ]

        # Require territory match (stricter filtering)
        self.require_territory_match = self.territory.get('require_territory_match', True)

    @abstractmethod
    def scrape(self) -> List[TriggerEvent]:
        """Scrape and return list of trigger events."""
        pass

    def generate_event_id(self, url: str, title: str) -> str:
        """Generate unique ID for an event."""
        content = f"{url}:{title}"
        return hashlib.md5(content.encode()).hexdigest()

    def is_excluded_content(self, text: str) -> bool:
        """Check if text contains excluded content types (concerts, sports, etc.)."""
        text_lower = text.lower()
        for excluded in self.excluded_content:
            if excluded in text_lower:
                return True
        return False

    def detect_event_type(self, text: str) -> Optional[EventType]:
        """Detect the type of trigger event from text."""
        text_lower = text.lower()

        # First check if this is excluded content (concerts, sports, etc.)
        if self.is_excluded_content(text):
            return None

        # Check for CFO specifically first
        cfo_patterns = ['cfo', 'chief financial officer']
        if any(pattern in text_lower for pattern in cfo_patterns):
            # Make sure it's about hiring, not just mentioning CFO
            hire_indicators = ['named', 'appointed', 'hired', 'joins', 'new cfo', 'promoted', 'announces']
            if any(ind in text_lower for ind in hire_indicators):
                return EventType.CFO_HIRE

        # Check for executive hires
        if any(kw in text_lower for kw in self.exec_hire_keywords):
            hire_indicators = ['named', 'appointed', 'hired', 'joins', 'promoted', 'announces', 'welcomes']
            if any(ind in text_lower for ind in hire_indicators):
                return EventType.EXECUTIVE_HIRE

        # Check for M&A
        if any(kw in text_lower for kw in self.ma_keywords):
            return EventType.MERGER_ACQUISITION

        # Check for funding - require stronger signals
        funding_strong = ['series a', 'series b', 'series c', 'series d', 'funding round',
                          'raises $', 'raised $', 'secures $', 'secured $', 'investment round',
                          'venture capital', 'private equity', 'seed funding', 'seed round']
        if any(kw in text_lower for kw in funding_strong):
            return EventType.FUNDING

        return None

    def is_excluded_location(self, text: str) -> bool:
        """Check if text mentions an excluded international location."""
        text_lower = text.lower()
        for location in self.excluded_locations:
            if location in text_lower:
                return True
        return False

    def extract_dateline_location(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """
        Extract city and state from PR newswire-style dateline.
        For backwards compatibility, returns first location found.
        Use extract_dateline_locations() for multiple locations.
        """
        locations = self.extract_dateline_locations(text)
        if locations:
            return locations[0]
        return None, None

    def extract_dateline_locations(self, text: str) -> List[tuple[Optional[str], Optional[str]]]:
        """
        Extract ALL cities and states from PR newswire-style dateline.
        Handles multiple locations like "NEW YORK and ARLINGTON, Va."

        Examples:
            "ARLINGTON, Va., Feb. 10" -> [("arlington", "virginia")]
            "NEW YORK and BOSTON, Feb. 10" -> [("new york", None), ("boston", None)]
            "NEW YORK and ARLINGTON, Va., Feb. 10" -> [("new york", None), ("arlington", "virginia")]
            "CHICAGO, IL and RICHMOND, Va., Feb. 10" -> [("chicago", "illinois"), ("richmond", "virginia")]
        """
        locations = []
        text_stripped = text.strip()

        # First, extract the dateline portion (before the date)
        # Match everything before a month abbreviation
        dateline_match = re.match(r'^(.+?)(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', text_stripped)
        if not dateline_match:
            return locations

        dateline_portion = dateline_match.group(1).strip()

        # Split by " and " or " AND " to get individual location segments
        # Also handle "&" and "/"
        segments = re.split(r'\s+and\s+|\s+AND\s+|\s*&\s*|\s*/\s*', dateline_portion)

        for segment in segments:
            segment = segment.strip().rstrip(',').rstrip('-').strip()
            if not segment:
                continue

            # Pattern: CITY, State (e.g., "ARLINGTON, Va." or "CHICAGO, IL" or "CHARLOTTE, N.C.")
            # Handle state abbreviations with periods like "N.C.", "N.Y.", "D.C."
            city_state_pattern = r'^([A-Z][A-Z\s]+),\s*([A-Za-z]\.?[A-Za-z]?\.?)$'
            match = re.match(city_state_pattern, segment)
            if match:
                city = match.group(1).strip().lower()
                state_abbrev = match.group(2).strip().lower().replace('.', '')
                state = STATE_ABBREVS.get(state_abbrev, state_abbrev)
                locations.append((city, state))
                continue

            # Pattern: Just CITY (e.g., "NEW YORK" or "BOSTON")
            # Must be all caps to be a dateline city
            if segment.isupper() or (segment.replace(' ', '').isupper()):
                city = segment.lower()
                # Check if it might be a state abbreviation at the end
                locations.append((city, None))

        return locations

    def matches_territory(self, text: str) -> tuple[bool, List[str]]:
        """Check if text mentions locations in our territory."""
        text_lower = text.lower()
        matched = []

        # First check if it's an excluded location (international)
        if self.is_excluded_location(text):
            return False, []

        # Check dateline location first (e.g., "ARLINGTON, Va., Feb. 10, 2026")
        dateline_city, dateline_state = self.extract_dateline_location(text)
        if dateline_city or dateline_state:
            # Check if dateline city matches our cities
            if dateline_city and dateline_city in self.cities:
                matched.append(dateline_city)

            # Check if dateline state matches our regions
            if dateline_state and dateline_state in self.regions:
                matched.append(dateline_state)

            # If dateline matched, return early with high confidence
            if matched:
                return True, matched

        # Check regions in full text
        for region in self.regions:
            if region in text_lower:
                matched.append(region)

        # Check cities in full text
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

    def detect_stable_target_potential(self, text: str) -> tuple[bool, List[str]]:
        """
        Detect if an article mentions a company that fits our criteria
        even without a specific trigger event. Returns (is_potential, reasons).

        Looks for positive company signals like:
        - Growth, expansion, new locations
        - Awards, recognition
        - New products/services/contracts
        - Partnership announcements
        - Leadership mentions
        - Industry feature articles
        """
        text_lower = text.lower()
        reasons = []

        # Positive company signals that indicate a company worth tracking
        growth_signals = [
            ('expands', 'Company expansion mentioned'),
            ('expansion', 'Company expansion mentioned'),
            ('growth', 'Company growth mentioned'),
            ('growing', 'Company growth mentioned'),
            ('new location', 'New location/facility announced'),
            ('new facility', 'New facility announced'),
            ('opens new', 'New opening announced'),
            ('grand opening', 'New opening announced'),
            ('relocating', 'Company relocation mentioned'),
            ('headquarters', 'Headquarters mentioned'),
        ]

        award_signals = [
            ('award', 'Company received award/recognition'),
            ('winner', 'Company received award/recognition'),
            ('recognized', 'Company recognized'),
            ('named top', 'Company named as top performer'),
            ('best of', 'Company named as top performer'),
            ('excellence', 'Company excellence recognized'),
            ('certification', 'Company certification mentioned'),
            ('certified', 'Company certification mentioned'),
        ]

        business_signals = [
            ('new contract', 'New contract announced'),
            ('wins contract', 'Contract win announced'),
            ('awarded contract', 'Contract award announced'),
            ('partnership', 'Partnership announced'),
            ('partners with', 'Partnership announced'),
            ('strategic alliance', 'Strategic alliance announced'),
            ('collaboration', 'Business collaboration mentioned'),
            ('new product', 'New product launched'),
            ('launches', 'New launch announced'),
            ('introduces', 'New introduction announced'),
            ('unveils', 'New unveiling announced'),
            ('new service', 'New service announced'),
        ]

        leadership_signals = [
            ('ceo', 'CEO/leadership mentioned'),
            ('chief executive', 'Leadership mentioned'),
            ('founder', 'Founder mentioned'),
            ('president', 'President mentioned'),
            ('leadership', 'Leadership mentioned'),
            ('executive team', 'Executive team mentioned'),
        ]

        industry_signals = [
            ('industry leader', 'Company positioned as industry leader'),
            ('market leader', 'Company positioned as market leader'),
            ('leading provider', 'Company positioned as leading provider'),
            ('top provider', 'Company positioned as top provider'),
            ('fastest growing', 'Fast growth company'),
            ('inc. 5000', 'Inc. 5000 company'),
            ('inc 5000', 'Inc. 5000 company'),
        ]

        all_signals = growth_signals + award_signals + business_signals + leadership_signals + industry_signals

        for keyword, reason in all_signals:
            if keyword in text_lower and reason not in reasons:
                reasons.append(reason)

        # Must have at least one positive signal
        if not reasons:
            return False, []

        return True, reasons

    def generate_stable_target_reasoning(
        self,
        company_name: Optional[str],
        matched_regions: List[str],
        matched_industries: List[str],
        positive_signals: List[str],
        is_target_size: bool
    ) -> str:
        """Generate a reasoning explanation for why this company is recommended."""
        parts = []

        if company_name:
            parts.append(f"Company: {company_name}")

        if matched_regions:
            parts.append(f"Territory match: {', '.join(matched_regions[:3])}")

        if matched_industries:
            parts.append(f"Industry match: {', '.join(matched_industries[:3])}")

        if is_target_size:
            parts.append("Appears to be mid-market/private company")

        if positive_signals:
            parts.append(f"Signals: {'; '.join(positive_signals[:4])}")

        return " | ".join(parts) if parts else "Matches territory and industry criteria"

    def get_matched_industries(self, text: str) -> List[str]:
        """Get list of matched industry keywords."""
        text_lower = text.lower()
        matched = []
        for industry in self.industries:
            if industry in text_lower:
                matched.append(industry)
        return matched[:5]  # Limit to top 5
