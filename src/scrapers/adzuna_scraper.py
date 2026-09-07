"""Adzuna API scraper for finance leadership job postings.

Replaces the bot-blocked Indeed/ZipRecruiter/SimplyHired/Ladders/CFO.com
scrapers with a single structured API source.

Get a free API key at: https://developer.adzuna.com/signup
Free tier: ~100-250 calls/month depending on signup date.

Set credentials via environment variables (recommended) or config.yaml:
    ADZUNA_APP_ID=xxxxxxxx
    ADZUNA_APP_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

Strategy: one `title_only` query per role family per country (us, ca),
territory + title filtering done in-code to minimise API calls.

Call budget — the once-per-day latch (2026-09-06):
    The scraper is invoked every 4 h by GitHub Actions, but Adzuna's free
    tier is ~100-250 calls/month. The old throttle only fired when the
    exact UTC hour matched `run_hours` — GHA cron drifts, so it silently
    never matched and Adzuna was dead for 9 days. Now the scraper keeps a
    persisted latch in the SQLite `kv` table (`adzuna_last_run_date`): the
    first cycle of each UTC day runs, every later cycle that day is skipped.
    Defaults = 3 calls/day ≈ 90/month, inside the free tier.

    `run_hours` is IGNORED unless `latch_mode: hour` is set explicitly AND
    `run_hours` is a non-empty list — then it is an *additional* restriction
    on top of the daily latch (legacy behaviour, not recommended).

Event type: every Adzuna posting is `finance_seat_open` — a company that is
HIRING a CFO/Controller has an open seat; it is not a seated hire
(A.J. 2026-09-06). Title format stays 'Company hiring: <job title>'.
"""

import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Set, Optional, TYPE_CHECKING

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource

if TYPE_CHECKING:  # typing only — avoids a runtime import cycle
    from ..database import DatabaseManager


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

    # Adzuna category slug — restricts results to accounting/finance
    # postings server-side (fewer stemmed "controls engineer" hits).
    CATEGORY = 'accounting-finance-jobs'

    # kv key holding the UTC date (YYYY-MM-DD) of the last successful run.
    LATCH_KEY = 'adzuna_last_run_date'

    def __init__(self, config: Dict[str, Any], db: 'Optional[DatabaseManager]' = None):
        """`db` is the shared DatabaseManager (passed by TriggerEventMonitor).
        With `db=None` there is no persisted latch and the scraper runs on
        every call — only appropriate for tests / one-off manual runs."""
        super().__init__(config)

        adz_cfg = config.get('adzuna', {}) or {}
        self.enabled = adz_cfg.get('enabled', False)
        self.db = db

        # Credentials: prefer env vars (safer for CI), fall back to config
        self.app_id  = adz_cfg.get('app_id')  or os.environ.get('ADZUNA_APP_ID', '')
        self.app_key = adz_cfg.get('app_key') or os.environ.get('ADZUNA_APP_KEY', '')

        # Tunables
        self.countries        = adz_cfg.get('countries', ['us', 'ca'])
        self.titles           = adz_cfg.get('titles', DEFAULT_TITLES)
        self.results_per_page = int(adz_cfg.get('results_per_page', 50))
        self.max_days_old     = int(adz_cfg.get('max_days_old', 14))

        # API-call budget control — see module docstring. The persisted
        # once-per-day latch (kv['adzuna_last_run_date']) is the primary
        # throttle. `run_hours` only applies when `latch_mode: hour` is set
        # explicitly and the list is non-empty; otherwise it is ignored.
        self.latch_mode: str = str(adz_cfg.get('latch_mode', 'daily') or 'daily').lower()
        run_hours = adz_cfg.get('run_hours') or []
        self.run_hours: List[int] = (
            [int(h) for h in run_hours]
            if self.latch_mode == 'hour' and isinstance(run_hours, (list, tuple, set))
            else []
        )

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

        # API-budget throttle 1: persisted once-per-day latch. The first
        # cycle of each UTC day runs; later cycles that day are skipped.
        today = datetime.now(timezone.utc).date().isoformat()
        if self.db is not None:
            last_run = self.db.get_kv(self.LATCH_KEY)
            if last_run == today:
                print(f'  Adzuna: skipped — already ran today ({today} UTC)')
                return []

        # API-budget throttle 2 (legacy, opt-in via latch_mode: hour): only
        # fire at the configured UTC hours in addition to the daily latch.
        if self.run_hours:
            current_utc_hour = datetime.now(timezone.utc).hour
            if current_utc_hour not in self.run_hours:
                print(f'  Adzuna: skipped — current UTC hour '
                      f'{current_utc_hour:02d} not in run_hours '
                      f'{sorted(self.run_hours)} (latch_mode: hour)')
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

        # Set the daily latch only if at least one country call got a
        # response. On a total failure (network / 4xx / 5xx) leave it unset
        # so the next 4-hour cycle retries instead of losing the whole day.
        if self.db is not None and any(
            st['status'] in ('success', 'partial') for st in self.source_statuses
        ):
            self.db.set_kv(self.LATCH_KEY, today)

        return all_events

    # ── Per-country scrape (1 API call per country) ───────────────────────

    def _scrape_country(self, country: str, title_q: str) -> List[TriggerEvent]:
        params = {
            'app_id':           self.app_id,
            'app_key':          self.app_key,
            'results_per_page': self.results_per_page,
            'title_only':       title_q,
            'category':         self.CATEGORY,
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

        # 6. Event type — a job posting is an OPEN finance seat, not a
        #    seated hire (A.J. 2026-09-06). Same label for CFO and
        #    Controller-family titles; the grade (+3) lives downstream.
        event_type = EventType.FINANCE_SEAT_OPEN

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
