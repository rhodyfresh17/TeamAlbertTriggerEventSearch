"""Company data enrichment using Apollo.io or ZoomInfo API."""

import os
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

import requests

# Try to import performance cache
try:
    from .performance.cache import FileCache
    CACHE_AVAILABLE = True
except ImportError:
    CACHE_AVAILABLE = False


@dataclass
class CompanyInfo:
    """Enriched company information."""
    name: str
    website: Optional[str] = None
    revenue: Optional[str] = None
    revenue_range: Optional[str] = None
    revenue_millions: Optional[float] = None  # Parsed revenue in millions
    employee_count: Optional[int] = None
    employee_range: Optional[str] = None
    industry: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    linkedin_url: Optional[str] = None
    founded_year: Optional[int] = None
    is_public: bool = False  # Whether company is publicly traded
    stock_symbol: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for caching."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'CompanyInfo':
        """Create from dictionary."""
        return cls(**data)


class ApolloEnricher:
    """Enrich company data using Apollo.io API."""

    BASE_URL = "https://api.apollo.io/v1/organizations/enrich"

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_dir: str = ".cache/apollo",
        cache_ttl: float = 86400 * 7,  # 7 days default (company data changes slowly)
    ):
        self.api_key = api_key or os.environ.get('APOLLO_API_KEY', '')
        self.enabled = bool(self.api_key)

        # In-memory cache (existing behavior)
        self.cache: Dict[str, CompanyInfo] = {}

        # Persistent file cache for cost savings
        self._file_cache = None
        self._cache_ttl = cache_ttl
        if CACHE_AVAILABLE:
            try:
                self._file_cache = FileCache(
                    cache_dir=cache_dir,
                    default_ttl=cache_ttl
                )
            except Exception as e:
                print(f"Warning: Could not initialize file cache: {e}")

    def enrich_company(self, company_name: str) -> Optional[CompanyInfo]:
        """Look up company information by name."""
        if not self.enabled:
            return None

        # Check in-memory cache first
        cache_key = company_name.lower().strip()
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Check persistent file cache (saves API calls)
        if self._file_cache:
            cached_data = self._file_cache.get(f"apollo:{cache_key}")
            if cached_data:
                info = CompanyInfo.from_dict(cached_data)
                self.cache[cache_key] = info  # Populate memory cache
                return info

        try:
            headers = {
                'Content-Type': 'application/json',
                'Cache-Control': 'no-cache'
            }

            # Apollo API requires POST with api_key in body
            payload = {
                'api_key': self.api_key,
                'name': company_name
            }

            response = requests.post(
                self.BASE_URL,
                headers=headers,
                json=payload,
                timeout=10
            )

            if response.status_code != 200:
                print(f"Apollo API error: {response.status_code} - {response.text[:200]}")
                return None

            data = response.json()
            org = data.get('organization')

            if not org:
                return None

            # Check if company is public (Apollo provides this)
            is_public = org.get('publicly_traded_exchange') is not None
            stock_symbol = org.get('publicly_traded_symbol')

            info = CompanyInfo(
                name=org.get('name', company_name),
                website=org.get('website_url'),
                revenue=self._format_revenue(org.get('estimated_annual_revenue')),
                revenue_range=org.get('revenue_range'),
                revenue_millions=self._parse_revenue_millions(org.get('estimated_annual_revenue'), org.get('revenue_range')),
                employee_count=org.get('estimated_num_employees'),
                employee_range=org.get('employee_count_range'),
                industry=org.get('industry'),
                location=self._format_location(org),
                description=org.get('short_description'),
                linkedin_url=org.get('linkedin_url'),
                founded_year=org.get('founded_year'),
                is_public=is_public,
                stock_symbol=stock_symbol
            )

            # Cache the result (memory and file)
            self.cache[cache_key] = info
            if self._file_cache:
                self._file_cache.set(f"apollo:{cache_key}", info.to_dict())
            return info

        except Exception as e:
            print(f"Error enriching company {company_name}: {e}")
            return None

    def get_cache_stats(self) -> dict:
        """Get cache statistics for monitoring."""
        stats = {
            'memory_cache_size': len(self.cache),
            'file_cache': None
        }
        if self._file_cache:
            stats['file_cache'] = self._file_cache.stats()
        return stats

    def _format_revenue(self, revenue: Optional[str]) -> Optional[str]:
        """Format revenue string."""
        if not revenue:
            return None
        # Apollo returns strings like "$10M - $50M"
        return revenue

    def _parse_revenue_millions(self, revenue: Optional[str], revenue_range: Optional[str]) -> Optional[float]:
        """Parse revenue into millions as a float for filtering."""
        import re

        text = revenue or revenue_range
        if not text:
            return None

        # Try to extract numbers with M/B suffix
        # Patterns: "$10M", "$1.5B", "$10M - $50M", "10 million", etc.
        text = text.upper().replace(',', '').replace('$', '')

        # Look for billion
        match = re.search(r'([\d.]+)\s*B', text)
        if match:
            return float(match.group(1)) * 1000  # Convert to millions

        # Look for million
        match = re.search(r'([\d.]+)\s*M', text)
        if match:
            return float(match.group(1))

        # Try plain number (assume it's in dollars)
        match = re.search(r'([\d.]+)', text)
        if match:
            val = float(match.group(1))
            if val > 1000:  # Likely in thousands or raw dollars
                return val / 1_000_000
            return val

        return None

    def _format_location(self, data: Dict[str, Any]) -> Optional[str]:
        """Format location from company data."""
        parts = []
        if data.get('city'):
            parts.append(data['city'])
        if data.get('state'):
            parts.append(data['state'])
        if data.get('country') and data.get('country') != 'United States':
            parts.append(data['country'])

        return ', '.join(parts) if parts else None


class ZoomInfoEnricher:
    """Enrich company data using ZoomInfo API."""

    BASE_URL = "https://api.zoominfo.com/search/company"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get('ZOOMINFO_API_KEY', '')
        self.enabled = bool(self.api_key)
        self.cache: Dict[str, CompanyInfo] = {}

    def enrich_company(self, company_name: str) -> Optional[CompanyInfo]:
        """Look up company information by name."""
        if not self.enabled:
            return None

        # Check cache first
        cache_key = company_name.lower().strip()
        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            # ZoomInfo API request
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }

            payload = {
                'companyName': company_name,
                'maxResults': 1
            }

            response = requests.post(
                self.BASE_URL,
                headers=headers,
                json=payload,
                timeout=10
            )

            if response.status_code != 200:
                print(f"ZoomInfo API error: {response.status_code}")
                return None

            data = response.json()

            if not data.get('data') or len(data['data']) == 0:
                return None

            company_data = data['data'][0]

            info = CompanyInfo(
                name=company_data.get('companyName', company_name),
                website=company_data.get('website'),
                revenue=self._format_revenue(company_data.get('revenue')),
                revenue_range=company_data.get('revenueRange'),
                employee_count=company_data.get('employeeCount'),
                employee_range=company_data.get('employeeRange'),
                industry=company_data.get('industry'),
                location=self._format_location(company_data),
                description=company_data.get('companyDescription'),
                linkedin_url=company_data.get('linkedInUrl'),
                founded_year=company_data.get('foundedYear')
            )

            # Cache the result
            self.cache[cache_key] = info
            return info

        except Exception as e:
            print(f"Error enriching company {company_name}: {e}")
            return None

    def _format_revenue(self, revenue: Optional[float]) -> Optional[str]:
        """Format revenue as readable string."""
        if revenue is None:
            return None

        if revenue >= 1_000_000_000:
            return f"${revenue / 1_000_000_000:.1f}B"
        elif revenue >= 1_000_000:
            return f"${revenue / 1_000_000:.1f}M"
        elif revenue >= 1_000:
            return f"${revenue / 1_000:.1f}K"
        else:
            return f"${revenue:.0f}"

    def _format_location(self, data: Dict[str, Any]) -> Optional[str]:
        """Format location from company data."""
        parts = []
        if data.get('city'):
            parts.append(data['city'])
        if data.get('state'):
            parts.append(data['state'])
        if data.get('country') and data.get('country') != 'United States':
            parts.append(data['country'])

        return ', '.join(parts) if parts else None


class CompanyEnricher:
    """Main enrichment service that can use multiple providers."""

    def __init__(self, config: Dict[str, Any]):
        enrichment_config = config.get('enrichment', {})

        # Initialize Apollo.io (preferred provider)
        apollo_key = enrichment_config.get('apollo_api_key') or os.environ.get('APOLLO_API_KEY', '')
        self.apollo = ApolloEnricher(apollo_key) if apollo_key else None

        # Initialize ZoomInfo as fallback
        zoominfo_key = enrichment_config.get('zoominfo_api_key') or os.environ.get('ZOOMINFO_API_KEY', '')
        self.zoominfo = ZoomInfoEnricher(zoominfo_key) if zoominfo_key else None

        self.enabled = (self.apollo and self.apollo.enabled) or (self.zoominfo and self.zoominfo.enabled)

        if self.apollo and self.apollo.enabled:
            self.provider = "Apollo.io"
        elif self.zoominfo and self.zoominfo.enabled:
            self.provider = "ZoomInfo"
        else:
            self.provider = None

    def enrich(self, company_name: str) -> Optional[CompanyInfo]:
        """Enrich company data using available providers."""
        if not self.enabled or not company_name:
            return None

        # Try Apollo first (preferred)
        if self.apollo and self.apollo.enabled:
            result = self.apollo.enrich_company(company_name)
            if result:
                return result

        # Fall back to ZoomInfo
        if self.zoominfo and self.zoominfo.enabled:
            return self.zoominfo.enrich_company(company_name)

        return None

    def verify_company(self, info: CompanyInfo, config: Dict[str, Any]) -> tuple[bool, str]:
        """
        Verify if a company meets target criteria.

        Returns: (is_valid, reason)
        """
        filters = config.get('territory', {}).get('company_filters', {})
        min_employees = filters.get('min_employees', 20)
        max_employees = filters.get('max_employees', 2000)
        min_revenue = filters.get('min_revenue_millions', 20)
        max_revenue = filters.get('max_revenue_millions', 500)
        exclude_public = filters.get('exclude_public_companies', True)

        # Check if public company
        if exclude_public and info.is_public:
            return False, f"Public company ({info.stock_symbol or 'publicly traded'})"

        # Check employee count
        if info.employee_count:
            if info.employee_count > max_employees:
                return False, f"Too large ({info.employee_count:,} employees, max {max_employees:,})"
            if info.employee_count < min_employees:
                return False, f"Too small ({info.employee_count:,} employees, min {min_employees:,})"

        # Check revenue
        if info.revenue_millions:
            if info.revenue_millions > max_revenue:
                return False, f"Revenue too high (${info.revenue_millions:.0f}M, max ${max_revenue}M)"
            if info.revenue_millions < min_revenue:
                return False, f"Revenue too low (${info.revenue_millions:.0f}M, min ${min_revenue}M)"

        return True, "Meets criteria"

    def format_for_alert(self, info: Optional[CompanyInfo]) -> str:
        """Format enriched data for alert output."""
        if not info:
            return ""

        lines = []

        if info.website:
            lines.append(f"Website: {info.website}")
        if info.is_public:
            lines.append(f"Status: PUBLIC ({info.stock_symbol})" if info.stock_symbol else "Status: PUBLIC")
        else:
            lines.append("Status: Private")
        if info.revenue or info.revenue_range:
            lines.append(f"Revenue: {info.revenue or info.revenue_range}")
        if info.employee_count or info.employee_range:
            emp = info.employee_count or info.employee_range
            lines.append(f"Employees: {emp}")
        if info.industry:
            lines.append(f"Industry: {info.industry}")
        if info.location:
            lines.append(f"HQ: {info.location}")
        if info.linkedin_url:
            lines.append(f"LinkedIn: {info.linkedin_url}")

        return '\n'.join(lines)
