"""Job site scraper for finance leadership positions."""

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus, urlencode

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

        # Location queries
        self.location_queries = job_config.get('locations', [
            'New York, NY',
            'Boston, MA',
            'Philadelphia, PA',
            'New Jersey',
            'Connecticut',
            'Maryland',
            'Virginia',
            'Washington DC',
            'North Carolina',
            'Toronto, ON',
            'Montreal, QC',
        ])

        # Job board settings
        self.job_boards = job_config.get('boards', {
            'indeed': True,
            'ziprecruiter': True,
            'simplyhired': True,
            'google_jobs': True,
            'ladders': True,
            'cfo_com': True,
        })

    def scrape(self) -> List[TriggerEvent]:
        """Scrape job sites for finance leadership positions."""
        if not self.enabled:
            return []

        events = []
        results = {}

        print("  Scraping job boards for finance leadership positions...")

        # Scrape each enabled job board
        if self.job_boards.get('indeed', True):
            print("    - Indeed", end="")
            indeed_events = self._scrape_indeed()
            events.extend(indeed_events)
            results['Indeed'] = len(indeed_events)
            print(f" ({len(indeed_events)} found)")

        if self.job_boards.get('ziprecruiter', True):
            print("    - ZipRecruiter", end="")
            zr_events = self._scrape_ziprecruiter()
            events.extend(zr_events)
            results['ZipRecruiter'] = len(zr_events)
            print(f" ({len(zr_events)} found)")

        if self.job_boards.get('simplyhired', True):
            print("    - SimplyHired", end="")
            sh_events = self._scrape_simplyhired()
            events.extend(sh_events)
            results['SimplyHired'] = len(sh_events)
            print(f" ({len(sh_events)} found)")

        if self.job_boards.get('google_jobs', True):
            print("    - Google Jobs", end="")
            gj_events = self._scrape_google_jobs()
            events.extend(gj_events)
            results['Google'] = len(gj_events)
            print(f" ({len(gj_events)} found)")

        if self.job_boards.get('ladders', True):
            print("    - Ladders", end="")
            lad_events = self._scrape_ladders()
            events.extend(lad_events)
            results['Ladders'] = len(lad_events)
            print(f" ({len(lad_events)} found)")

        if self.job_boards.get('cfo_com', True):
            print("    - CFO.com", end="")
            cfo_events = self._scrape_cfo_com()
            events.extend(cfo_events)
            results['CFO.com'] = len(cfo_events)
            print(f" ({len(cfo_events)} found)")

        # Summary
        working = [k for k, v in results.items() if v > 0]
        if working:
            print(f"    Job sources returning results: {', '.join(working)}")
        else:
            print("    Note: No job postings found (RSS feeds may be unavailable)")

        print(f"    Total job postings: {len(events)}")
        return events

    def _scrape_indeed(self) -> List[TriggerEvent]:
        """Scrape Indeed RSS feeds for job postings."""
        events = []
        errors = 0

        for title in self.job_titles[:3]:  # Limit to top 3 titles
            for location in self.location_queries[:4]:  # Limit locations
                try:
                    query = quote_plus(title)
                    loc = quote_plus(location)
                    url = f"https://www.indeed.com/rss?q={query}&l={loc}&sort=date"

                    feed_events = self._fetch_rss_feed(url, "Indeed Jobs", location)
                    events.extend(feed_events)
                    self.delay_request()
                except Exception as e:
                    errors += 1
                    continue

        if errors > 0:
            print(f"      (Indeed: {errors} feed errors)")
        return events

    def _scrape_ziprecruiter(self) -> List[TriggerEvent]:
        """Scrape ZipRecruiter RSS feeds for job postings."""
        events = []

        for title in self.job_titles[:3]:
            for location in self.location_queries[:4]:
                try:
                    query = quote_plus(title)
                    loc = quote_plus(location)
                    url = f"https://www.ziprecruiter.com/jobs-rss?search={query}&location={loc}"

                    feed_events = self._fetch_rss_feed(url, "ZipRecruiter", location)
                    events.extend(feed_events)
                    self.delay_request()
                except Exception as e:
                    continue

        return events

    def _scrape_simplyhired(self) -> List[TriggerEvent]:
        """Scrape SimplyHired RSS feeds for job postings."""
        events = []

        for title in self.job_titles[:3]:
            for location in self.location_queries[:4]:
                try:
                    query = quote_plus(title)
                    loc = quote_plus(location)
                    # SimplyHired RSS format
                    url = f"https://www.simplyhired.com/search/rss?q={query}&l={loc}"

                    feed_events = self._fetch_rss_feed(url, "SimplyHired", location)
                    events.extend(feed_events)
                    self.delay_request()
                except Exception as e:
                    continue

        return events

    def _scrape_google_jobs(self) -> List[TriggerEvent]:
        """Scrape Google for job postings via Google News RSS."""
        events = []

        # Use Google News RSS with job-specific queries
        base_url = "https://news.google.com/rss/search?q="

        for title in self.job_titles[:3]:
            for location in self.location_queries[:3]:
                try:
                    # Search for job postings mentioning these roles
                    query = f'"{title}" hiring OR "now hiring" OR "job opening" "{location}"'
                    encoded_query = quote_plus(query)
                    url = f"{base_url}{encoded_query}&hl=en-US&gl=US&ceid=US:en"

                    response = self.session.get(url, timeout=self.timeout)
                    if response.status_code != 200:
                        continue

                    root = ET.fromstring(response.content)

                    for item in root.findall('.//item'):
                        try:
                            event = self._parse_google_job_item(item, title, location)
                            if event:
                                events.append(event)
                        except Exception:
                            continue

                    self.delay_request()
                except Exception as e:
                    continue

        return events

    def _scrape_ladders(self) -> List[TriggerEvent]:
        """Scrape Ladders RSS feeds for executive jobs ($100K+)."""
        events = []

        # Ladders has category-based RSS feeds
        # Finance/Accounting executive jobs
        ladders_feeds = [
            ("https://www.theladders.com/rss/jobs?category=accounting-finance", "Ladders Finance"),
            ("https://www.theladders.com/rss/jobs?category=executive", "Ladders Executive"),
        ]

        for feed_url, source_name in ladders_feeds:
            try:
                response = self.session.get(feed_url, timeout=self.timeout)
                if response.status_code != 200:
                    continue

                root = ET.fromstring(response.content)

                for item in root.findall('.//item'):
                    try:
                        event = self._parse_ladders_item(item, source_name)
                        if event:
                            events.append(event)
                    except Exception:
                        continue

                self.delay_request()
            except Exception as e:
                continue

        return events

    def _scrape_cfo_com(self) -> List[TriggerEvent]:
        """Scrape CFO.com job listings."""
        events = []

        # CFO.com has a jobs section - try RSS or scrape job listings
        cfo_urls = [
            "https://www.cfo.com/feed/",  # Main RSS - filter for job mentions
        ]

        for feed_url in cfo_urls:
            try:
                response = self.session.get(feed_url, timeout=self.timeout)
                if response.status_code != 200:
                    continue

                root = ET.fromstring(response.content)

                for item in root.findall('.//item'):
                    try:
                        title_elem = item.find('title')
                        if title_elem is None:
                            continue

                        title = title_elem.text or ''
                        title_lower = title.lower()

                        # Filter for job-related articles
                        job_indicators = ['hiring', 'appointed', 'named', 'joins', 'new cfo', 'cfo search']
                        if not any(ind in title_lower for ind in job_indicators):
                            continue

                        event = self._parse_cfo_com_item(item)
                        if event:
                            events.append(event)
                    except Exception:
                        continue

                self.delay_request()
            except Exception as e:
                continue

        return events

    def _fetch_rss_feed(self, url: str, source_name: str, search_location: str) -> List[TriggerEvent]:
        """Fetch and parse a generic job RSS feed."""
        events = []

        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code != 200:
                # Silently skip - many job sites no longer offer RSS
                return []

            root = ET.fromstring(response.content)

            for item in root.findall('.//item'):
                try:
                    event = self._parse_job_item(item, source_name, search_location)
                    if event:
                        events.append(event)
                except Exception:
                    continue

        except ET.ParseError:
            # Invalid XML - RSS feed might not exist
            pass
        except Exception:
            pass

        return events

    def _parse_job_item(self, item, source_name: str, search_location: str) -> Optional[TriggerEvent]:
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

        # Check if title contains any of our target job titles
        title_lower = title.lower()
        if not any(jt.lower() in title_lower for jt in self.job_titles):
            return None

        # Parse publication date
        pub_date = datetime.now()
        if pub_date_elem is not None and pub_date_elem.text:
            try:
                from email.utils import parsedate_to_datetime
                pub_date = parsedate_to_datetime(pub_date_elem.text)
            except:
                pass

        # Extract company name
        company_name = self._extract_company_from_title(title)

        # Skip public companies
        full_text = f"{title} {description}"
        if self.is_public_company(full_text):
            return None

        # Check for excluded industries
        _, is_excluded = self.matches_industry(full_text)
        if is_excluded:
            return None

        # Check territory match
        matches_territory, matched_regions = self.matches_territory(full_text)

        if not matched_regions and search_location:
            matched_regions = [search_location]
            matches_territory = True

        if self.require_territory_match and not matches_territory:
            return None

        # Check for industry match
        matches_ind, _ = self.matches_industry(full_text)

        # Determine matched keywords
        matched_keywords = []
        for job_title in self.job_titles:
            if job_title.lower() in title_lower:
                matched_keywords.append(job_title)

        # Calculate relevance score
        score = self._calculate_job_relevance(
            title=title,
            matched_regions=matched_regions,
            matches_industry=matches_ind,
            matched_keywords=matched_keywords
        )

        event_id = self.generate_event_id(url, title)

        return TriggerEvent(
            id=event_id,
            title=f"Hiring: {title}",
            event_type=EventType.CFO_HIRE,
            source=EventSource.OTHER,
            source_name=source_name,
            url=url,
            published_date=pub_date,
            company_name=company_name,
            company_location=search_location,
            description=description[:500] if description else None,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=score
        )

    def _parse_google_job_item(self, item, search_title: str, search_location: str) -> Optional[TriggerEvent]:
        """Parse a Google News item for job-related content."""
        title_elem = item.find('title')
        link_elem = item.find('link')
        pub_date_elem = item.find('pubDate')

        if title_elem is None or link_elem is None:
            return None

        title = title_elem.text or ''
        url = link_elem.text or ''

        # Must mention hiring or job-related terms
        title_lower = title.lower()
        job_terms = ['hiring', 'hires', 'appointed', 'named', 'joins', 'new cfo', 'new controller']
        if not any(term in title_lower for term in job_terms):
            return None

        # Must mention a target job title
        if not any(jt.lower() in title_lower for jt in self.job_titles):
            return None

        pub_date = datetime.now()
        if pub_date_elem is not None and pub_date_elem.text:
            try:
                from email.utils import parsedate_to_datetime
                pub_date = parsedate_to_datetime(pub_date_elem.text)
            except:
                pass

        company_name = self._extract_company_from_title(title)

        if self.is_public_company(title):
            return None

        matches_territory, matched_regions = self.matches_territory(title)
        if not matched_regions:
            matched_regions = [search_location]

        if self.require_territory_match and not matches_territory and not matched_regions:
            return None

        matched_keywords = [jt for jt in self.job_titles if jt.lower() in title_lower]
        matches_ind, _ = self.matches_industry(title)

        score = self._calculate_job_relevance(
            title=title,
            matched_regions=matched_regions,
            matches_industry=matches_ind,
            matched_keywords=matched_keywords
        )

        event_id = self.generate_event_id(url, title)

        return TriggerEvent(
            id=event_id,
            title=title,
            event_type=EventType.CFO_HIRE,
            source=EventSource.GOOGLE_NEWS,
            source_name="Google Jobs",
            url=url,
            published_date=pub_date,
            company_name=company_name,
            company_location=search_location,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=score
        )

    def _parse_ladders_item(self, item, source_name: str) -> Optional[TriggerEvent]:
        """Parse a Ladders RSS item."""
        title_elem = item.find('title')
        link_elem = item.find('link')
        pub_date_elem = item.find('pubDate')
        desc_elem = item.find('description')

        if title_elem is None or link_elem is None:
            return None

        title = title_elem.text or ''
        url = link_elem.text or ''
        description = desc_elem.text if desc_elem is not None else ''

        # Must match our target job titles
        title_lower = title.lower()
        if not any(jt.lower() in title_lower for jt in self.job_titles):
            return None

        pub_date = datetime.now()
        if pub_date_elem is not None and pub_date_elem.text:
            try:
                from email.utils import parsedate_to_datetime
                pub_date = parsedate_to_datetime(pub_date_elem.text)
            except:
                pass

        company_name = self._extract_company_from_title(title)
        full_text = f"{title} {description}"

        if self.is_public_company(full_text):
            return None

        _, is_excluded = self.matches_industry(full_text)
        if is_excluded:
            return None

        matches_territory, matched_regions = self.matches_territory(full_text)

        if self.require_territory_match and not matches_territory:
            return None

        matches_ind, _ = self.matches_industry(full_text)
        matched_keywords = [jt for jt in self.job_titles if jt.lower() in title_lower]

        score = self._calculate_job_relevance(
            title=title,
            matched_regions=matched_regions,
            matches_industry=matches_ind,
            matched_keywords=matched_keywords
        )

        # Boost score for Ladders (executive-level jobs)
        score = min(score + 10, 100)

        event_id = self.generate_event_id(url, title)

        return TriggerEvent(
            id=event_id,
            title=f"Hiring: {title}",
            event_type=EventType.CFO_HIRE,
            source=EventSource.OTHER,
            source_name=source_name,
            url=url,
            published_date=pub_date,
            company_name=company_name,
            description=description[:500] if description else None,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=score
        )

    def _parse_cfo_com_item(self, item) -> Optional[TriggerEvent]:
        """Parse a CFO.com RSS item."""
        title_elem = item.find('title')
        link_elem = item.find('link')
        pub_date_elem = item.find('pubDate')
        desc_elem = item.find('description')

        if title_elem is None or link_elem is None:
            return None

        title = title_elem.text or ''
        url = link_elem.text or ''
        description = desc_elem.text if desc_elem is not None else ''

        pub_date = datetime.now()
        if pub_date_elem is not None and pub_date_elem.text:
            try:
                from email.utils import parsedate_to_datetime
                pub_date = parsedate_to_datetime(pub_date_elem.text)
            except:
                pass

        company_name = self._extract_company_from_title(title)
        full_text = f"{title} {description}"

        if self.is_public_company(full_text):
            return None

        _, is_excluded = self.matches_industry(full_text)
        if is_excluded:
            return None

        matches_territory, matched_regions = self.matches_territory(full_text)

        if self.require_territory_match and not matches_territory:
            return None

        matches_ind, _ = self.matches_industry(full_text)

        # CFO.com articles are highly relevant
        matched_keywords = ['CFO']
        title_lower = title.lower()
        for jt in self.job_titles:
            if jt.lower() in title_lower and jt not in matched_keywords:
                matched_keywords.append(jt)

        score = self._calculate_job_relevance(
            title=title,
            matched_regions=matched_regions,
            matches_industry=matches_ind,
            matched_keywords=matched_keywords
        )

        # Boost score for CFO.com (very relevant source)
        score = min(score + 10, 100)

        event_id = self.generate_event_id(url, title)

        return TriggerEvent(
            id=event_id,
            title=title,
            event_type=EventType.CFO_HIRE,
            source=EventSource.OTHER,
            source_name="CFO.com",
            url=url,
            published_date=pub_date,
            company_name=company_name,
            description=description[:500] if description else None,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=score
        )

    def _extract_company_from_title(self, title: str) -> Optional[str]:
        """Extract company name from job title string."""
        # Format: "Job Title - Company Name - Location"
        parts = title.split(' - ')
        if len(parts) >= 2:
            return parts[1].strip()

        # Try "at Company" pattern
        match = re.search(r'\bat\s+([A-Z][A-Za-z0-9\s&]+)', title)
        if match:
            return match.group(1).strip()

        # Try "Company hires/appoints" pattern
        match = re.search(r'^([A-Z][A-Za-z0-9\s&]+?)\s+(?:hires|appoints|names)', title)
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
