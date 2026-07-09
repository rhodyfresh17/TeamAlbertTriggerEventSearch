"""SEC EDGAR 8-K scraper for officer changes and M&A events.

Uses SEC's EFTS full-text search API to find 8-K filings with specific
"items" (5.02 officer changes, 2.01 M&A completions, 1.01 material agreements).
For each filing, fetches the filer's business address to filter by territory.

SEC requires a descriptive User-Agent. Be polite: max 10 req/sec.
Docs: https://www.sec.gov/os/accessing-edgar-data
"""

import re
import time
from datetime import datetime, timedelta, timezone, date
from typing import List, Dict, Any, Optional, Set

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource


# SEC EDGAR uses standard 2-letter codes for US states and Canadian provinces
TERRITORY_STATE_CODES: Set[str] = {
    # New England
    'ME', 'NH', 'VT', 'MA', 'RI', 'CT',
    # Mid-Atlantic
    'NY', 'NJ', 'PA', 'DE', 'MD', 'VA', 'WV', 'DC',
    # South East
    'NC', 'SC', 'GA', 'FL', 'AL', 'TN', 'KY',
    # Rust Belt
    'OH', 'MI', 'IN',
    # Canadian provinces
    'A0',  # Newfoundland
    'A1',  # Nova Scotia
    'A2',  # Prince Edward Island
    'A3',  # New Brunswick
    'A4',  # Quebec
    'A5',  # Ontario
    # (BC=A6, AB=A0, etc. — Canadian SEC codes vary; include only territory)
    'ON', 'QC', 'NB', 'NS', 'PE', 'NL',  # in case standard codes appear
}


# 8-K Item codes we care about, mapped to event types
ITEM_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    '5.02': {
        'name':       'Departure/Election of Directors or Officers',
        'event_type': EventType.EXECUTIVE_HIRE,  # may be promoted to CFO_HIRE
    },
    '2.01': {
        'name':       'Completion of Acquisition or Disposition',
        'event_type': EventType.MERGER_ACQUISITION,
    },
    '1.01': {
        'name':       'Entry into a Material Definitive Agreement',
        'event_type': EventType.MERGER_ACQUISITION,
    },
}


class SECScraper(BaseScraper):
    """Scraper for SEC EDGAR 8-K filings (officer changes + M&A)."""

    EFTS_URL = 'https://efts.sec.gov/LATEST/search-index'
    SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        sec_config = config.get('sec_filings', {}) or {}
        self.enabled = sec_config.get('enabled', True)
        self.lookback_days = int(sec_config.get('lookback_days', 7))
        self.max_per_item = int(sec_config.get('max_per_item', 60))

        # Default territory states can be overridden in config
        territory_codes = sec_config.get('territory_state_codes')
        if territory_codes:
            self.territory_codes = {c.upper() for c in territory_codes}
        else:
            self.territory_codes = TERRITORY_STATE_CODES

        # SEC requires a descriptive User-Agent. Use scraper config if available.
        self.sec_user_agent = sec_config.get(
            'user_agent',
            'TeamAlbert Sales Intelligence (sales-leads@teamalbert.local)'
        )
        self.session.headers.update({'User-Agent': self.sec_user_agent})

        # Per-CIK address cache so we don't repeatedly look up the same filer
        # Per-CIK address+SIC cache so we don't repeatedly look up the same
        # filer. Value shape: {'state': str, 'sic': str, 'sic_desc': str}.
        self._cik_info_cache: Dict[str, Dict[str, str]] = {}
        # Backward-compat alias (some code paths read this; safe to keep as
        # the same underlying dict but the values are now dicts, so don't
        # use this directly — call _lookup_filer_info()).
        self._cik_state_cache = self._cik_info_cache  # type: ignore[assignment]

        # Counter for blocked-by-SIC events (for the source_status reporting)
        self._sic_blocked_count: int = 0

        # Set of accession numbers (adsh) for Item 5.02 filings that also
        # mention "Chief Financial Officer" — pre-fetched once per scrape
        # to correctly route CFO-related officer changes to event_type=CFO_HIRE
        # (vs EXECUTIVE_HIRE for non-CFO officer departures/elections).
        self._cfo_adsh_set: set = set()

        # Track source status for the dashboard
        self.source_statuses: List[Dict[str, Any]] = []

    # ── Public entry point ────────────────────────────────────────────────

    def scrape(self) -> List[TriggerEvent]:
        self.source_statuses = []
        if not self.enabled:
            return []

        # Prefetch CFO-related Item 5.02 filing accession numbers so we can
        # correctly classify them at hit-conversion time. One extra EFTS call.
        self._cfo_adsh_set = self._fetch_cfo_filing_adsh_set()

        # Reset per-run counter so the count reflects this scrape only
        self._sic_blocked_count = 0

        all_events: List[TriggerEvent] = []
        for item_code, item_def in ITEM_DEFINITIONS.items():
            source_label = f'SEC 8-K Item {item_code}'
            try:
                events = self._scrape_one_item(item_code, item_def)
                all_events.extend(events)
                self.source_statuses.append({
                    'source_name':   source_label,
                    'source_type':   'sec_edgar',
                    'status':        'success' if events else 'partial',
                    'error_message': None if events else 'No matching filings in territory',
                    'events_found':  len(events),
                })
                print(f'  - {source_label}: {len(events)} in territory')
            except Exception as e:
                self.source_statuses.append({
                    'source_name':   source_label,
                    'source_type':   'sec_edgar',
                    'status':        'error',
                    'error_message': str(e)[:200],
                    'events_found':  0,
                })
                print(f'  - {source_label}: ERROR {e}')

        if self._sic_blocked_count > 0:
            print(f'  - SEC SIC prefilter: blocked {self._sic_blocked_count} '
                  f'off-target filings before enrichment')

        return all_events

    # ── Per-item scrape ───────────────────────────────────────────────────

    def _scrape_one_item(
        self, item_code: str, item_def: Dict[str, Any]
    ) -> List[TriggerEvent]:
        hits = self._search_efts(item_code)
        events: List[TriggerEvent] = []

        for hit in hits[: self.max_per_item]:
            try:
                ev = self._hit_to_event(hit, item_code, item_def)
                if ev:
                    events.append(ev)
            except Exception as e:
                # Don't let one bad filing stop the rest
                print(f'    skipping malformed hit: {e}')
                continue

        return events

    def _fetch_cfo_filing_adsh_set(self) -> set:
        """Pre-fetch the accession numbers of Item 5.02 filings that mention
        'Chief Financial Officer' so we can route them to event_type=CFO_HIRE.

        Paginates through up to MAX_PAGES of results — EFTS returns ~100 hits
        per page, and busy weeks easily exceed that for CFO-related Item 5.02
        filings nationwide.
        """
        startdt = (date.today() - timedelta(days=self.lookback_days)).isoformat()
        enddt   = date.today().isoformat()
        MAX_PAGES = 5  # 500 hits max — covers typical 7-day window with headroom

        adsh_set: set = set()
        try:
            for page in range(MAX_PAGES):
                params = {
                    # EFTS treats quoted phrases as required; space = AND.
                    'q':         '"Chief Financial Officer" "Item 5.02"',
                    'forms':     '8-K',
                    'dateRange': 'custom',
                    'startdt':   startdt,
                    'enddt':     enddt,
                    'from':      page * 100,  # EFTS pagination: 100 per page
                }
                resp = self.session.get(
                    self.EFTS_URL, params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                hits = resp.json().get('hits', {}).get('hits', []) or []
                if not hits:
                    break  # exhausted — stop early
                for h in hits:
                    adsh = (h.get('_source') or {}).get('adsh', '')
                    if adsh:
                        adsh_set.add(adsh)
                self.delay_request()
                if len(hits) < 100:
                    break  # last page (partial) — done
            print(f'  - SEC CFO prefetch: {len(adsh_set)} Item 5.02 filings '
                  f'mention "Chief Financial Officer"')
            return adsh_set
        except Exception as e:
            print(f'  - SEC CFO prefetch failed (defaulting to EXEC for all): {e}')
            return adsh_set  # return whatever we got before the error

    # ── SIC code prefilter ───────────────────────────────────────────────
    # Block off-target industries at SCRAPE time using the filer's SIC code
    # from EDGAR submissions JSON. Stops Pharma/Biotech/Software/Logistics
    # events from going through expensive Tavily+Ollama enrichment only to
    # be deleted by the post-enrichment industry filter.
    #
    # SIC codes per https://www.sec.gov/info/edgar/siccodes.htm
    # Each entry maps the SIC (or SIC range) to a human-readable reason
    # that gets logged when an event is blocked.
    BLOCKED_SIC_CODES: Dict[str, str] = {
        # ── Mining / metals / extractive ──────────────────────────────
        '1000': 'Metal Mining',
        '1040': 'Gold Mining',
        '1044': 'Silver Mining',
        '1090': 'Misc Metal Mining',
        '1311': 'Crude Petroleum & Natural Gas',
        '1381': 'Drilling Oil & Gas Wells',
        '1389': 'Oil & Gas Field Services',
        '1400': 'Mining & Quarrying — Nonmetallic',
        '1422': 'Crushed & Broken Limestone',
        '1623': 'Water/Sewer/Pipeline Construction',
        '2911': 'Petroleum Refining',
        '2990': 'Misc Petroleum Products',
        # ── Heavy industry / metals ───────────────────────────────────
        '3310': 'Steel Works & Blast Furnaces',
        '3312': 'Steel Works',
        '3317': 'Steel Pipe & Tubes',
        '3320': 'Iron & Steel Foundries',
        '3330': 'Primary Nonferrous Metals',
        '3334': 'Primary Aluminum',
        '3341': 'Secondary Smelting & Refining',
        # ── Software / Computer Services ──────────────────────────────
        '7370': 'Computer Services',
        '7371': 'Computer Services — Prepackaged Software',
        '7372': 'Prepackaged Software',
        '7374': 'Computer Processing & Data Preparation',
        '7389': 'Services — Business Services NEC',
        # ── Computer Hardware / Semis ─────────────────────────────────
        '3576': 'Computer Communications Equipment',
        '3577': 'Computer Peripheral Equipment',
        '3674': 'Semiconductors & Related Devices',
        # ── Healthcare / Pharma / Biotech / MedDev ────────────────────
        '2834': 'Pharmaceutical Preparations',
        '2835': 'In Vitro & In Vivo Diagnostic Substances',
        '2836': 'Biological Products (Biotechnology)',
        '3841': 'Surgical & Medical Instruments',
        '3842': 'Orthopedic, Prosthetic, & Surgical Appliances',
        '3845': 'Electromedical & Electrotherapeutic Apparatus',
        '8000': 'Health Services',
        '8050': 'Nursing & Personal Care Facilities',
        '8060': 'Hospitals',
        '8062': 'General Medical & Surgical Hospitals',
        '8071': 'Medical Laboratories',
        '8090': 'Health Services',
        '8731': 'Commercial Physical & Biological Research',
        # ── Logistics / Transportation ────────────────────────────────
        '4011': 'Railroads',
        '4213': 'Trucking',
        '4400': 'Water Transportation',
        '4412': 'Deep Sea Foreign Transportation of Freight',
        '4500': 'Air Transportation',
        '4512': 'Air Transportation — Scheduled',
        '4513': 'Air Couriers',
        '4581': 'Airports',
        '4700': 'Transportation Services',
        '4731': 'Freight Transportation Arrangement',
        # ── Utilities ─────────────────────────────────────────────────
        '4900': 'Electric, Gas, & Sanitary Services',
        '4911': 'Electric Services',
        '4922': 'Natural Gas Transmission',
        '4923': 'Natural Gas Distribution',
        '4924': 'Natural Gas Distribution',
        '4931': 'Electric & Other Services Combined',
        '4932': 'Gas & Other Services Combined',
        '4941': 'Water Supply',
        # ── Hospitality / Entertainment ───────────────────────────────
        '5812': 'Eating Places (Restaurants)',
        '7000': 'Hotels & Other Lodging',
        '7011': 'Hotels & Motels',
        '7990': 'Amusement & Recreation Services',
        '7993': 'Coin-Operated Amusement Devices',
        '7997': 'Membership Sports & Recreation Clubs',
    }

    def _search_efts(self, item_code: str) -> List[Dict[str, Any]]:
        """Query SEC EFTS for 8-K filings tagged with a specific item."""
        startdt = (date.today() - timedelta(days=self.lookback_days)).isoformat()
        enddt   = date.today().isoformat()

        params = {
            'q':         f'"Item {item_code}"',
            'forms':     '8-K',
            'dateRange': 'custom',
            'startdt':   startdt,
            'enddt':     enddt,
        }
        resp = self.session.get(self.EFTS_URL, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        self.delay_request()
        return data.get('hits', {}).get('hits', [])

    # ── Convert one EFTS hit into a TriggerEvent ──────────────────────────

    def _hit_to_event(
        self,
        hit: Dict[str, Any],
        item_code: str,
        item_def: Dict[str, Any],
    ) -> Optional[TriggerEvent]:
        source = hit.get('_source', {}) or {}

        # CIK + display name
        ciks = source.get('ciks') or []
        if not ciks:
            return None
        cik = str(ciks[0])

        display_names = source.get('display_names') or []
        if not display_names:
            return None
        # "ACME CORP  (0001234567) (Filer)" → "ACME CORP"
        company_name = re.split(r'\s*\(', display_names[0])[0].strip()

        # Date filed
        file_date_str = source.get('file_date') or ''
        try:
            published = datetime.fromisoformat(file_date_str).replace(tzinfo=timezone.utc)
        except Exception:
            published = datetime.now(timezone.utc)

        # adsh = accession number, used to construct URL
        adsh = source.get('adsh') or ''
        if not adsh:
            return None
        adsh_clean = adsh.replace('-', '')
        url = (
            f'https://www.sec.gov/Archives/edgar/data/'
            f'{int(cik)}/{adsh_clean}/{adsh}-index.htm'
        )

        # Filer info (state + SIC) — one HTTP call, used for both checks
        filer_info = self._lookup_filer_info(cik)
        state = filer_info.get('state', '')
        if not state:
            return None
        if state.upper() not in self.territory_codes:
            return None

        # SIC code prefilter — blocks off-target industries at scrape time
        # so they never go through expensive Tavily+Ollama enrichment.
        # Aligns with POST_ENRICHMENT_INDUSTRY_BLOCK in enrichment_scout.py.
        sic = filer_info.get('sic', '')
        if sic and sic in self.BLOCKED_SIC_CODES:
            self._sic_blocked_count += 1
            reason = self.BLOCKED_SIC_CODES[sic]
            sic_desc = filer_info.get('sic_desc', reason)
            print(f'    🚫 SIC {sic} ({sic_desc}) — blocked at scrape time: '
                  f'{company_name[:40]}')
            return None

        # Skip industry-excluded targets where applicable (name-based)
        full_text = ' '.join([company_name, source.get('file_type', '')])
        _matches_target, matches_excluded = self.matches_industry(full_text)
        if matches_excluded:
            return None

        # Skip mega-cap public companies (BlackRock, Morgan Stanley, the big
        # banks, etc.) — too large for the NetSuite up-market territory. These
        # pass the industry filter (their industry IS Financial Services, a
        # target) so they need the explicit excluded_public_companies check.
        # Without this they get ingested, enriched, and pollute the dashboard
        # until manually cleaned.
        if self.is_public_company(company_name):
            return None

        # Classify event_type. For Item 5.02 (officer changes), route to
        # CFO_HIRE if the pre-fetched CFO set contains this filing's adsh,
        # else EXECUTIVE_HIRE for other officer changes.
        event_type = item_def['event_type']
        if item_code == '5.02':
            if adsh in self._cfo_adsh_set:
                event_type = EventType.CFO_HIRE
            else:
                event_type = EventType.EXECUTIVE_HIRE

        title = (
            f'SEC 8-K Item {item_code} ({item_def["name"]}) — {company_name}'
        )
        description = (
            f'SEC 8-K filing by {company_name} ({state}) — Item {item_code}: '
            f'{item_def["name"]}. Filing date: {file_date_str}.'
        )

        return TriggerEvent(
            id=self.generate_event_id(url, company_name),
            title=title,
            event_type=event_type,
            source=EventSource.SEC_EDGAR,
            source_name='SEC EDGAR',
            url=url,
            published_date=published,
            company_name=company_name,
            company_location=state,
            description=description,
            relevance_score=75.0,  # SEC filings are high-signal/structured
            matched_regions=[state],
        )

    # ── Helper: look up filer's business state + SIC code ─────────────────

    def _lookup_filer_info(self, cik: str) -> Dict[str, str]:
        """Look up a filer's business state AND SIC code from EDGAR
        submissions JSON. Single HTTP call gives us both. Cached per-run.
        Returns {} on any error.

        Result shape: {'state': 'NY', 'sic': '6020', 'sic_desc': 'Banks'}
        """
        if cik in self._cik_info_cache:
            return self._cik_info_cache[cik]

        try:
            url = self.SUBMISSIONS_URL.format(cik=int(cik))
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            addresses = data.get('addresses') or {}
            business = addresses.get('business') or {}
            info = {
                'state':    (business.get('stateOrCountry') or '').strip().upper(),
                'sic':      str(data.get('sic') or '').strip(),
                'sic_desc': (data.get('sicDescription') or '').strip(),
            }
            self._cik_info_cache[cik] = info
            # SEC rate-limit politeness
            time.sleep(0.12)
            return info
        except Exception:
            self._cik_info_cache[cik] = {}
            return {}

    def _lookup_filer_state(self, cik: str) -> str:
        """Backward-compatible wrapper around _lookup_filer_info."""
        return self._lookup_filer_info(cik).get('state', '')
