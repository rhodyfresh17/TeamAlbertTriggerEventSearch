"""Adzuna API scraper for finance leadership job postings.

Replaces the bot-blocked Indeed/ZipRecruiter/SimplyHired/Ladders/CFO.com
scrapers with a single structured API source.

Get a free API key at: https://developer.adzuna.com/signup
Free tier: ~100-250 calls/month depending on signup date.

Set credentials via environment variables (recommended) or config.yaml:
    ADZUNA_APP_ID=xxxxxxxx
    ADZUNA_APP_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

Strategy: ONE broad search per country (us, ca) per scrape cycle.
Territory + title filtering done in-code to minimise API calls.
At 6 cycles/day × 2 countries = 360 calls/month. Most reps fit in free tier;
heavy users can either upgrade Adzuna plan, drop cycle frequency, or restrict
to one country.
"""

import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Set, Optional, Iterable

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


# Territory states / provinces — from FY27 xlsx. Used to filter Adzuna's
# returned job postings by location.area[1] (state or province).
US_TERRITORY_STATES: Set[str] = {
    'Alabama', 'Connecticut', 'Delaware', 'Florida', 'Georgia',
    'Indiana', 'Kentucky', 'Maine', 'Maryland', 'Massachusetts',
    'Michigan', 'New Hampshire', 'New Jersey', 'New York',
    'North Carolina', 'Ohio', 'Pennsylvania', 'Rhode Island',
    'South Carolina', 'Tennessee', 'Vermont', 'Virginia',
    'West Virginia', 'District of Columbia', 'Washington DC',
}

CA_TERRITORY_PROVINCES: Set[str] = {
    'New Brunswick',
    'Newfoundland and Labrador', 'Newfoundland', 'Labrador',
    'Nova Scotia',
    'Ontario',
    'Prince Edward Island',
    'Quebec', 'Québec',
}

DEFAULT_TITLES: List[str] = [
    'CFO', 'Chief Financial Officer',
    'VP Finance', 'Vice President Finance', 'VP of Finance',
    'Controller', 'Corporate Controller',
    'Finance Director', 'Director of Finance',
    'Head of Finance',
    'Chief Accounting Officer', 'VP Accounting',
]

# "Controller" is a heavily-overloaded job title — these are NOT finance
# roles and must not become trigger events.
NON_FINANCE_CONTROLLER_TITLES: List[str] = [
    'air traffic', 'document controller', 'quality controller',
    'inventory controller', 'stock controller', 'traffic controller',
    'controls engineer', 'controller technician', 'motion controller',
    'production controller', 'material controller', 'credit controller',
    'pasteuriz', 'machine controller', 'process controller',
]

# Staffing/recruiting agencies post on behalf of ANONYMOUS end-clients —
# there is no account to work, so the posting is noise. Blacklist by
# company display-name substring (case-insensitive).
RECRUITER_NAME_PATTERNS: List[str] = [
    'robert half', 'vaco', 'kforce', 'randstad', 'aston carter',
    'michael page', 'lhh', 'addison group', 'beacon hill', 'jobot',
    'cybercoders', 'creative financial staffing', 'korn ferry',
    'heidrick', 'spencer stuart', 'staffing', 'recruit', 'headhunt',
    'executive search', 'search partners', 'search group', 'talent',
    'personnel', 'workforce', 'employment', 'placement',
]


class AdzunaScraper(BaseScraper):
    """Scraper for Adzuna job board API (finance leadership postings)."""

    BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        adz_cfg = config.get('adzuna', {}) or {}
        self.enabled = adz_cfg.get('enabled', False)

        # Credentials: prefer env vars (safer for CI), fall back to config
        self.app_id  = adz_cfg.get('app_id')  or os.environ.get('ADZUNA_APP_ID', '')
        self.app_key = adz_cfg.get('app_key') or os.environ.get('ADZUNA_APP_KEY', '')

        # Tunables
        self.countries        = adz_cfg.get('countries', ['us', 'ca'])
        self.titles           = adz_cfg.get('titles', DEFAULT_TITLES)
        self.results_per_page = int(adz_cfg.get('results_per_page', 50))
        self.max_days_old     = int(adz_cfg.get('max_days_old', 14))

        # API-call budget control. The scraper runs every 4 hours, but Adzuna's
        # free tier only allows ~100-250 calls/month. `run_hours` lists which
        # UTC hours Adzuna should actually fire (e.g. [12] = once/day at noon UTC
        # = 60 calls/month for US+CA = fits 100 free tier).
        # Set to null/empty to run every cycle.
        self.run_hours: Optional[Iterable[int]] = adz_cfg.get('run_hours', [12])

        # title_only query terms — one API call each (see scrape() for the
        # call-budget math). 'controller' also stems to Corporate Controller;
        # 'cfo' catches CFO / Chief Financial Officer postings.
        self.title_queries: List[str] = adz_cfg.get('title_queries',
                                                    ['controller', 'cfo'])

        # Override territory sets from config if provided
        self.us_states = set(adz_cfg.get('us_states', US_TERRITORY_STATES))
        self.ca_provinces = set(adz_cfg.get('ca_provinces', CA_TERRITORY_PROVINCES))

        self.source_statuses: List[Dict[str, Any]] = []

    # ── Entry point ───────────────────────────────────────────────────────

    def scrape(self) -> List[TriggerEvent]:
        self.source_statuses = []

        if not self.enabled:
            return []
        if not self.app_id or not self.app_key:
            print('  Adzuna: skipped — set ADZUNA_APP_ID and ADZUNA_APP_KEY '
                  '(env or config)')
            self.source_statuses.append({
                'source_name':   'Adzuna',
                'source_type':   'job_board',
                'status':        'error',
                'error_message': 'Missing API credentials',
                'events_found':  0,
            })
            return []

        # API-budget throttle: only fire at configured UTC hours
        if self.run_hours:
            current_utc_hour = datetime.now(timezone.utc).hour
            if current_utc_hour not in self.run_hours:
                print(f'  Adzuna: skipped — current UTC hour '
                      f'{current_utc_hour:02d} not in run_hours '
                      f'{sorted(self.run_hours)}')
                return []

        # Query strategy (changed 2026-08-09): what_or matched ANY loose
        # word across title+description — with sort_by=date the page filled
        # with non-finance noise and real postings never surfaced. Now we
        # run one title_only query per role family; Adzuna stems (matching
        # "controls"/"control"), and the strict in-code title check drops
        # the stemmed noise.
        # Budget: US runs all queries daily; CA alternates one query/day.
        # Defaults = 3 calls/day ≈ 90/month — inside the ~100 free tier.
        day_idx = datetime.now(timezone.utc).timetuple().tm_yday

        all_events: List[TriggerEvent] = []
        for country in self.countries:
            queries = (self.title_queries if country == 'us'
                       else [self.title_queries[day_idx % len(self.title_queries)]])
            label = f'Adzuna ({country.upper()})'
            try:
                events = []
                for q in queries:
                    events.extend(self._scrape_country(country, q))
                all_events.extend(events)
                self.source_statuses.append({
                    'source_name':   label,
                    'source_type':   'job_board',
                    'status':        'success' if events else 'partial',
                    'error_message': None if events else 'No jobs matched territory',
                    'events_found':  len(events),
                })
                print(f'  - {label}: {len(events)} in-territory '
                      f'finance-leadership jobs')
            except Exception as e:
                self.source_statuses.append({
                    'source_name':   label,
                    'source_type':   'job_board',
                    'status':        'error',
                    'error_message': str(e)[:200],
                    'events_found':  0,
                })
                print(f'  - {label}: ERROR {e}')

        return all_events

    # ── Per-country scrape (1 API call per country) ───────────────────────

    def _scrape_country(self, country: str, title_q: str) -> List[TriggerEvent]:
        params = {
            'app_id':           self.app_id,
            'app_key':          self.app_key,
            'results_per_page': self.results_per_page,
            'title_only':       title_q,
            'max_days_old':     self.max_days_old,
            # Newest first — the default (relevance) resurfaces the same
            # "best-matching" postings every day and NEW postings never
            # make the one page we fetch. A trigger tool wants recency.
            'sort_by':          'date',
            'content-type':     'application/json',
        }
        url = self.BASE_URL.format(country=country)
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        self.delay_request()

        target = self.us_states if country == 'us' else self.ca_provinces
        events: List[TriggerEvent] = []
        for job in data.get('results', []):
            try:
                ev = self._job_to_event(job, country, target)
                if ev:
                    events.append(ev)
            except Exception as e:
                # Don't let one malformed job stop the rest
                continue

        return events

    # ── Convert a single Adzuna job → TriggerEvent ────────────────────────

    def _job_to_event(
        self,
        job: Dict[str, Any],
        country: str,
        target_states: Set[str],
    ) -> Optional[TriggerEvent]:
        # 1. Territory filter via location.area[1]
        #    Adzuna area structure: [Country, State, City, ...]
        area = (job.get('location') or {}).get('area') or []
        if len(area) < 2:
            return None
        state_or_province = (area[1] or '').strip()
        if state_or_province not in target_states:
            return None

        # 2. Title sanity check — Adzuna's what_or can be loose; verify
        #    the returned title actually mentions a target role, and is not
        #    an overloaded non-finance "controller" (air traffic, QA, ...)
        title = (job.get('title') or '').strip()
        title_lower = title.lower()
        if not any(t.lower() in title_lower for t in self.titles):
            return None
        if any(p in title_lower for p in NON_FINANCE_CONTROLLER_TITLES):
            return None

        # 3. URL is the dedup + click target
        url = (job.get('redirect_url') or '').strip()
        if not url:
            return None

        # 4. Industry exclusion — drop mining/steel/oil-gas/etc. jobs
        company_name = ((job.get('company') or {}).get('display_name') or '').strip()

        # 4b. No company = no account to work; recruiter posting = the real
        #     employer is anonymous. Both are dead ends for a sales tool.
        if not company_name:
            return None
        if any(p in company_name.lower() for p in RECRUITER_NAME_PATTERNS):
            return None
        description  = (job.get('description') or '')[:600]
        full_text = f'{title} {company_name} {description}'
        _matches_target, matches_excluded = self.matches_industry(full_text)
        if matches_excluded:
            return None

        # 5. Parse published date
        try:
            created = job.get('created', '')
            published = datetime.fromisoformat(created.replace('Z', '+00:00'))
        except Exception:
            published = datetime.now(timezone.utc)

        # 6. Classify event type — CFO_HIRE if title mentions CFO/CFO-equivalent
        is_cfo = ('cfo' in title_lower
                  or 'chief financial' in title_lower)
        event_type = EventType.CFO_HIRE if is_cfo else EventType.EXECUTIVE_HIRE

        location_str = ((job.get('location') or {}).get('display_name') or '').strip()

        return TriggerEvent(
            id=self.generate_event_id(url, company_name or title),
            title=f'{company_name or "Company"} hiring: {title}',
            event_type=event_type,
            source=EventSource.ADZUNA,
            source_name='Adzuna',
            url=url,
            published_date=published,
            company_name=company_name or None,
            company_location=location_str or None,
            description=description,
            relevance_score=72.0,  # Direct hiring signal — high
            matched_regions=[state_or_province],
        )
