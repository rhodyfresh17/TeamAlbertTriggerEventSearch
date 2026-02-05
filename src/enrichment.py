"""Company data enrichment using ZoomInfo API."""

import os
from typing import Dict, Any, Optional
from dataclasses import dataclass

import requests


@dataclass
class CompanyInfo:
    """Enriched company information."""
    name: str
    website: Optional[str] = None
    revenue: Optional[str] = None
    revenue_range: Optional[str] = None
    employee_count: Optional[int] = None
    employee_range: Optional[str] = None
    industry: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    linkedin_url: Optional[str] = None
    founded_year: Optional[int] = None


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

        # Initialize ZoomInfo if configured
        zoominfo_key = enrichment_config.get('zoominfo_api_key') or os.environ.get('ZOOMINFO_API_KEY', '')
        self.zoominfo = ZoomInfoEnricher(zoominfo_key) if zoominfo_key else None

        self.enabled = self.zoominfo and self.zoominfo.enabled

    def enrich(self, company_name: str) -> Optional[CompanyInfo]:
        """Enrich company data using available providers."""
        if not self.enabled or not company_name:
            return None

        # Try ZoomInfo
        if self.zoominfo and self.zoominfo.enabled:
            return self.zoominfo.enrich_company(company_name)

        return None

    def format_for_alert(self, info: Optional[CompanyInfo]) -> str:
        """Format enriched data for alert output."""
        if not info:
            return ""

        lines = []

        if info.website:
            lines.append(f"Website: {info.website}")
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
