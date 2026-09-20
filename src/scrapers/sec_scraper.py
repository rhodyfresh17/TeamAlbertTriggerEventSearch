"""SEC EDGAR 8-K scraper for officer changes and M&A events.

Uses SEC's EFTS full-text search API to find 8-K filings with specific
"items" (5.02 officer changes, 2.01 M&A completions, 1.01 material agreements).
For each filing, fetches the filer's business address to filter by territory.

SEC requires a descriptive User-Agent. Be polite: max 10 req/sec.
Docs: https://www.sec.gov/os/accessing-edgar-data
"""

import html
import re
import time

import requests
from datetime import datetime, timedelta, timezone, date
from typing import List, Dict, Any, Optional, Set, Tuple

from .base import BaseScraper
from ..models import TriggerEvent, EventType, EventSource
from ..pipeline.gates import (
    is_non_operating_entity, formd_to_verdict, sic_to_verdict,
)


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
    # Canadian provinces — EDGAR's OFFICIAL state codes
    # (https://www.sec.gov/submit-filings/filer-support-resources/edgar-state-country-codes):
    #   A0=Alberta A1=British Columbia A2=Manitoba A3=New Brunswick
    #   A4=Newfoundland & Labrador A5=Nova Scotia A6=Ontario
    #   A7=Prince Edward Island A8=Quebec A9=Saskatchewan B0=Yukon
    # Territory = NB, NL, NS, ON, PE, QC only. The previous mapping here was
    # mislabeled/shifted: it admitted A0/A1/A2 (Alberta/BC/Manitoba — OUT of
    # territory) and OMITTED A6/A7/A8 (Ontario/PEI/Quebec — the two biggest
    # in-territory provinces were silently dropped for months).
    'A3',  # New Brunswick
    'A4',  # Newfoundland and Labrador
    'A5',  # Nova Scotia
    'A6',  # Ontario
    'A7',  # Prince Edward Island
    'A8',  # Quebec
    'ON', 'QC', 'NB', 'NS', 'PE', 'NL',  # defensive: if plain codes ever appear
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

# Item 1.01 content gate (v2, 2026-09-07). "Entry into a Material Definitive
# Agreement" covers credit facilities, leases, employment contracts and
# supply deals — 140 of 349 visible "M&A" triggers had zero acquisition
# language. A 1.01 filing is kept ONLY when its full text contains one of
# these definitive-agreement phrases (exact-phrase EFTS search).
MA_AGREEMENT_PHRASES = (
    'Agreement and Plan of Merger',
    'Stock Purchase Agreement',
    'Asset Purchase Agreement',
    'Membership Interest Purchase Agreement',
)


# ── EFTS resilience ─────────────────────────────────────────────────────────
# A full-text search request can fail transiently (5xx, 429, a dropped
# connection, an error page served as HTML). Every EFTS call goes through
# SECScraper._efts_get: a few tries with a short pause, then a breaker, so a
# real upstream outage costs ONE exhausted call per run instead of one per
# query. A skipped run loses nothing: the next run re-reads the same lookback
# window and URL dedup drops the repeats.
EFTS_MAX_TRIES = 3
EFTS_BACKOFF_SECONDS = (2, 6)     # pause before try 2 and before try 3
EFTS_BREAKER_SECONDS = 600        # after an exhausted call, skip EFTS this long


class EFTSUnavailable(RuntimeError):
    """SEC full-text search could not be used this run: retries exhausted,
    the breaker is open, or an item's content gate could not be built."""


class SECScraper(BaseScraper):
    """Scraper for SEC EDGAR 8-K filings (officer changes + M&A)."""

    EFTS_URL = 'https://efts.sec.gov/LATEST/search-index'
    SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'

    # Shared by every SEC scraper in the process (FormDScraper subclasses this
    # class and never rebinds it): once one query exhausts its retries, the
    # remaining queries of the run skip the network. Time-based so a
    # long-lived process (--daemon) tries again on a later cycle.
    _efts_breaker: Dict[str, Any] = {'open_until': 0.0, 'reason': ''}

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

        # Sets of accession numbers (adsh) for Item 5.02 filings, pre-fetched
        # once per scrape via EFTS full-text search:
        #   _cfo_adsh_set     — mention "Chief Financial Officer" → CFO_HIRE
        #   _finance_adsh_set — mention ANY finance-leader role (CFO,
        #                       Controller, Chief Accounting Officer).
        # 5.02 filings OUTSIDE the finance set are board-of-directors
        # elections / CEO changes / other officer noise — NOT NetSuite
        # triggers (A.J. 2026-07-21) — and are skipped entirely.
        self._cfo_adsh_set: set = set()
        self._finance_adsh_set: set = set()
        self._finance_prefetch_ok: bool = False
        self._board_skipped_count: int = 0
        # Item 1.01 content gate: accession numbers whose full text
        # contains a definitive M&A agreement phrase (MA_AGREEMENT_PHRASES).
        # Fails OPEN (keep all) when the prefetch comes back empty.
        self._ma_adsh_set: set = set()
        self._ma_prefetch_ok: bool = False
        self._ma_skipped_count: int = 0
        # item_code → why its content gate could not be built this run. An
        # item listed here is skipped (status 'error'), never ingested ungated.
        self._prefetch_error: Dict[str, str] = {}
        # gates.py policy counters (per kind / per verdict), reset per run
        self._entity_skipped: Dict[str, int] = {}
        self._sic_verdict_skipped: Dict[str, int] = {}
        self._formd_verdict_skipped: Dict[str, int] = {}

        # Track source status for the dashboard
        self.source_statuses: List[Dict[str, Any]] = []

    # ── Public entry point ────────────────────────────────────────────────

    def scrape(self) -> List[TriggerEvent]:
        self.source_statuses = []
        if not self.enabled:
            return []

        # Prefetch finance-leader Item 5.02 accession numbers (3 extra EFTS
        # calls, throttled). CFO set routes to CFO_HIRE; the wider finance
        # set is the KEEP filter — 5.02 filings outside it (board elections,
        # CEO changes) are dropped at ingestion.
        #
        # A prefetch that FAILS (request error after retries) is different
        # from one that answers with nothing. A failed or half-built set
        # would mistype filings (a CFO change filed as a generic officer
        # change) or wave every filing through ungated, and a saved event is
        # never re-typed — so the item is skipped this run instead and
        # reported as an error. The next run re-reads the same window.
        self._prefetch_error = {}
        self._cfo_adsh_set, self._finance_adsh_set = set(), set()
        try:
            self._cfo_adsh_set = self._fetch_phrase_adsh_set('Chief Financial Officer')
            self._finance_adsh_set = (
                self._cfo_adsh_set
                | self._fetch_phrase_adsh_set('Chief Accounting Officer')
                | self._fetch_phrase_adsh_set('Controller')
            )
        except Exception as e:
            self._prefetch_error['5.02'] = str(e)
            self._cfo_adsh_set, self._finance_adsh_set = set(), set()
            print(f'  - SEC 5.02 finance prefetch failed — item skipped this run: {e}')
        # Fail open ONLY on an empty answer: if the prefetch succeeded but
        # returned nothing, keep the old ingest-everything behavior rather
        # than silently dropping ALL 5.02 filings. A real 7-day window always
        # has finance-related 5.02 filings in territory.
        self._finance_prefetch_ok = bool(self._finance_adsh_set)

        # Item 1.01 content gate: union of filings whose full text carries a
        # definitive M&A agreement phrase. Same rules as above.
        self._ma_adsh_set = set()
        try:
            for phrase in MA_AGREEMENT_PHRASES:
                self._ma_adsh_set |= self._fetch_phrase_adsh_set(phrase, item_code='1.01')
        except Exception as e:
            self._prefetch_error['1.01'] = str(e)
            self._ma_adsh_set = set()
            print(f'  - SEC 1.01 M&A prefetch failed — item skipped this run: {e}')
        self._ma_prefetch_ok = bool(self._ma_adsh_set)

        # Reset per-run counters so the counts reflect this scrape only
        self._reset_gate_counters()

        all_events: List[TriggerEvent] = []
        for item_code, item_def in ITEM_DEFINITIONS.items():
            source_label = f'SEC 8-K Item {item_code}'
            try:
                if item_code in self._prefetch_error:
                    raise EFTSUnavailable(
                        'content gate not built, item skipped this run — '
                        f'{self._prefetch_error[item_code]}')
                events, items_fetched = self._scrape_one_item(item_code, item_def)
                all_events.extend(events)
                # items_fetched = filings EFTS returned for this item (after
                # locationCodes, before the finance/M&A content gates) so
                # "fetched 0" and "all filtered" are distinguishable
                # (v2 Phase 2, 2026-09-07).
                self.source_statuses.append({
                    'source_name':   source_label,
                    'source_type':   'sec_edgar',
                    'status':        'success' if events else 'partial',
                    'error_message': None if events else 'No matching filings in territory',
                    'events_found':  len(events),
                    'items_fetched': items_fetched,
                    'filtered_out':  max(items_fetched - len(events), 0),
                })
                print(f'  - {source_label}: {len(events)} in territory '
                      f'({items_fetched} fetched)')
            except Exception as e:
                self.source_statuses.append({
                    'source_name':   source_label,
                    'source_type':   'sec_edgar',
                    'status':        'error',
                    'error_message': str(e)[:200],
                    'events_found':  0,
                    'items_fetched': 0,
                    'filtered_out':  0,
                })
                print(f'  - {source_label}: ERROR {e}')

        if self._board_skipped_count > 0:
            print(f'  - SEC 5.02 finance filter: skipped '
                  f'{self._board_skipped_count} board/CEO-only officer '
                  f'filings (no finance-leader mention)')
        if self._ma_skipped_count > 0:
            print(f'  - SEC 1.01 M&A content gate: skipped '
                  f'{self._ma_skipped_count} material-agreement filings '
                  f'with no merger/purchase-agreement language')
        self._print_gate_summary('SEC 8-K')

        return all_events

    # ── gates.py policy helpers (shared by 8-K and Form D) ────────────────

    def _reset_gate_counters(self) -> None:
        self._sic_blocked_count = 0
        self._board_skipped_count = 0
        self._ma_skipped_count = 0
        self._entity_skipped = {}
        self._sic_verdict_skipped = {}
        self._formd_verdict_skipped = {}

    def _skip_filer_by_name(self, company_name: str) -> bool:
        """Entity-shape gate on the filer NAME (free, before any HTTP):
        fund vehicles, SPACs, governments, K-12, lodging, political, greek
        (A.J. 2026-09-04/06 exclusions in gates.is_non_operating_entity).
        True → skip. Counted per kind for the run summary."""
        hit, kind = is_non_operating_entity(company_name)
        if hit:
            self._entity_skipped[kind] = self._entity_skipped.get(kind, 0) + 1
            return True
        return False

    def _skip_filer_by_sic(self, sic: str, sic_desc: str, company_name: str) -> bool:
        """SIC gate: gates.sic_to_verdict ('out' / 'vehicle' → skip, counted
        per verdict), then the legacy BLOCKED_SIC_CODES list as a belt.
        'unknown' SICs (financial services, nonprofits, ambiguous) pass —
        vertical fit is confirmed post-research, never here."""
        if not sic:
            return False
        verdict, reason = sic_to_verdict(sic)
        if verdict in ('out', 'vehicle'):
            self._sic_verdict_skipped[verdict] = self._sic_verdict_skipped.get(verdict, 0) + 1
            print(f'    🚫 {reason} — {verdict} at scrape time: {company_name[:40]}')
            return True
        if sic in self.BLOCKED_SIC_CODES:
            self._sic_blocked_count += 1
            desc = sic_desc or self.BLOCKED_SIC_CODES[sic]
            print(f'    🚫 SIC {sic} ({desc}) — blocked at scrape time: '
                  f'{company_name[:40]}')
            return True
        return False

    @staticmethod
    def _sic_phrase(filer_info: Dict[str, str]) -> str:
        """'SIC: 6022 (STATE COMMERCIAL BANKS).' when the SIC is known, else
        ''. Downstream parses the literal 'SIC: NNNN' — keep the shape."""
        sic = (filer_info.get('sic') or '').strip()
        if not sic:
            return ''
        desc = (filer_info.get('sic_desc') or '').strip()
        return f'SIC: {sic} ({desc}).' if desc else f'SIC: {sic}.'

    def _print_gate_summary(self, label: str) -> None:
        if self._entity_skipped:
            total = sum(self._entity_skipped.values())
            detail = ', '.join(f'{k} {v}' for k, v in sorted(self._entity_skipped.items()))
            print(f'  - {label} entity-shape gate: skipped {total} filers by name ({detail})')
        if self._sic_verdict_skipped:
            total = sum(self._sic_verdict_skipped.values())
            detail = ', '.join(f'{k} {v}' for k, v in sorted(self._sic_verdict_skipped.items()))
            print(f'  - {label} SIC verdict gate: skipped {total} filers ({detail})')
        if self._sic_blocked_count > 0:
            print(f'  - {label} legacy SIC prefilter: blocked {self._sic_blocked_count} '
                  f'off-target filings before enrichment')
        if self._formd_verdict_skipped:
            total = sum(self._formd_verdict_skipped.values())
            detail = ', '.join(f'{k} {v}' for k, v in sorted(self._formd_verdict_skipped.items()))
            print(f'  - {label} Form D verdict gate: skipped {total} raises ({detail})')

    # ── Per-item scrape ───────────────────────────────────────────────────

    def _scrape_one_item(
        self, item_code: str, item_def: Dict[str, Any]
    ) -> Tuple[List[TriggerEvent], int]:
        """Returns (events, items_fetched) — items_fetched is the raw EFTS
        hit count for the item, before any content gate."""
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

        return events, len(hits)

    def _fetch_phrase_adsh_set(self, phrase: str, item_code: str = '5.02') -> set:
        """Pre-fetch the accession numbers of 8-K filings tagged with
        `item_code` whose full text contains `phrase` (exact-phrase EFTS
        full-text search), restricted to territory via locationCodes so the
        set is the same population `_search_efts` returns.

        Paginates through up to MAX_PAGES of results — EFTS returns ~100 hits
        per page, and busy weeks can exceed that for "Controller" mentions
        in Item 5.02 filings even within territory.
        """
        startdt = (date.today() - timedelta(days=self.lookback_days)).isoformat()
        enddt   = date.today().isoformat()
        MAX_PAGES = 5  # 500 hits max — covers typical 7-day window with headroom

        adsh_set: set = set()
        for page in range(MAX_PAGES):
            params = {
                # EFTS treats quoted phrases as required; space = AND.
                'q':         f'"{phrase}" "Item {item_code}"',
                'forms':     '8-K',
                'dateRange': 'custom',
                'startdt':   startdt,
                'enddt':     enddt,
                'locationCodes': ','.join(sorted(self.territory_codes)),
                'from':      page * 100,  # EFTS pagination: 100 per page
            }
            # Raises when the request cannot be completed — the caller skips
            # the item rather than gate it on a partial set (see scrape()).
            hits = self._efts_get(params).get('hits', {}).get('hits', []) or []
            if not hits:
                break  # exhausted — stop early
            for h in hits:
                adsh = (h.get('_source') or {}).get('adsh', '')
                if adsh:
                    adsh_set.add(adsh)
            self.delay_request()
            if len(hits) < 100:
                break  # last page (partial) — done
        print(f'  - SEC {item_code} prefetch: {len(adsh_set)} filings mention '
              f'"{phrase}"')
        return adsh_set

    # ── One EFTS request, with retries and a per-run breaker ──────────────

    @classmethod
    def reset_efts_breaker(cls) -> None:
        """Close the shared breaker (tests; a fresh process starts closed)."""
        SECScraper._efts_breaker.update(open_until=0.0, reason='')

    def _efts_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """One EFTS search request → parsed JSON.

        Retries what is plausibly transient — HTTP 5xx / 429, connection and
        timeout errors, a body that is not JSON — up to EFTS_MAX_TRIES with
        EFTS_BACKOFF_SECONDS between tries. Any other 4xx is OUR request
        being wrong: raised at once, no retry, breaker untouched.

        When the tries run out the shared breaker opens for
        EFTS_BREAKER_SECONDS and every later call raises EFTSUnavailable
        without touching the network, which bounds what an upstream outage
        can cost one scrape job.
        """
        breaker = SECScraper._efts_breaker
        if time.monotonic() < breaker['open_until']:
            raise EFTSUnavailable(
                f'SEC search skipped — unavailable earlier this run ({breaker["reason"]})')

        last = 'no response'
        for attempt in range(1, EFTS_MAX_TRIES + 1):
            try:
                resp = self.session.get(self.EFTS_URL, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                code = getattr(getattr(e, 'response', None), 'status_code', None)
                if isinstance(code, int) and code < 500 and code != 429:
                    raise
                last = f'HTTP {code}' if code else 'HTTP error'
            except requests.RequestException as e:
                last = type(e).__name__          # connection reset, timeout, truncated body…
            except ValueError:
                last = 'response was not JSON'   # an error page served with a 200
            if attempt < EFTS_MAX_TRIES:
                pause = EFTS_BACKOFF_SECONDS[min(attempt - 1, len(EFTS_BACKOFF_SECONDS) - 1)]
                print(f'    SEC search: {last} — retrying in {pause}s '
                      f'(try {attempt + 1} of {EFTS_MAX_TRIES})')
                time.sleep(pause)

        reason = f'{last} after {EFTS_MAX_TRIES} tries'
        breaker.update(open_until=time.monotonic() + EFTS_BREAKER_SECONDS, reason=reason)
        raise EFTSUnavailable(f'SEC search unavailable this run ({reason})')

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
        # ── Software / Computer Services: REMOVED (2026-07-16) ────────
        # 7370/7371/7372/7374 are AMBIGUOUS — fintech, payments, and
        # insurtech companies (target Financial Services subverticals)
        # frequently file under these codes. 7389 "Services NEC" is a
        # wastebasket that already false-blocked Repay Holdings (a
        # payments processor = target). Vertical fit for these is now
        # decided post-research by the ZI-subindustry gate, which sees
        # the company's actual business. Only unambiguous never-fit
        # codes belong in this scrape-time list.
        # ── Computer Hardware / Semis (unambiguous never-fit) ─────────
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
        """Query SEC EFTS for 8-K filings tagged with a specific item.

        Territory-filtered server-side via locationCodes (as FormDScraper
        does) and paginated with `from` in pages of 100 up to
        `max_per_item` hits — one nationwide page used to yield ~30
        in-territory filings; every hit is now in territory.
        """
        startdt = (date.today() - timedelta(days=self.lookback_days)).isoformat()
        enddt   = date.today().isoformat()
        loc = ','.join(sorted(self.territory_codes))
        pages = max(1, (self.max_per_item + 99) // 100)

        all_hits: List[Dict[str, Any]] = []
        for page in range(pages):
            params = {
                'q':         f'"Item {item_code}"',
                'forms':     '8-K',
                'dateRange': 'custom',
                'startdt':   startdt,
                'enddt':     enddt,
                'locationCodes': loc,
                'from':      page * 100,
            }
            hits = self._efts_get(params).get('hits', {}).get('hits', []) or []
            self.delay_request()
            if not hits:
                break
            all_hits.extend(hits)
            if len(hits) < 100 or len(all_hits) >= self.max_per_item:
                break
        return all_hits

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

        # Entity-shape gate on the filer name — free, before any HTTP
        if self._skip_filer_by_name(company_name):
            return None

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

        # SIC gate — gates.sic_to_verdict (policy) + legacy BLOCKED_SIC_CODES
        # belt. Off-vertical / vehicle filers never reach enrichment.
        sic = filer_info.get('sic', '')
        if self._skip_filer_by_sic(sic, filer_info.get('sic_desc', ''), company_name):
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

        # Classify event_type. For Item 5.02 (officer changes): CFO mentions
        # route to CFO_HIRE; other finance-leader mentions (Controller /
        # Chief Accounting Officer) stay EXECUTIVE_HIRE; filings mentioning
        # NO finance-leader role are board elections / CEO changes / other
        # officer noise — not NetSuite triggers — and are dropped.
        event_type = item_def['event_type']
        if item_code == '5.02':
            if adsh in self._cfo_adsh_set:
                event_type = EventType.CFO_HIRE
            elif adsh in self._finance_adsh_set:
                event_type = EventType.EXECUTIVE_HIRE
            elif self._finance_prefetch_ok:
                self._board_skipped_count += 1
                return None
            else:
                # Prefetch answered with nothing — fail open. (A prefetch that
                # FAILED never gets here: scrape() skips the item.)
                event_type = EventType.EXECUTIVE_HIRE

        # Item 1.01 content gate: a material-definitive-agreement filing is
        # M&A only when its full text carries a merger / purchase-agreement
        # phrase (see MA_AGREEMENT_PHRASES). Fail open if the prefetch was
        # empty, mirroring the 5.02 finance filter.
        if item_code == '1.01' and self._ma_prefetch_ok and adsh not in self._ma_adsh_set:
            self._ma_skipped_count += 1
            return None

        title = (
            f'SEC 8-K Item {item_code} ({item_def["name"]}) — {company_name}'
        )
        description = (
            f'SEC 8-K filing by {company_name} ({state}) — Item {item_code}: '
            f'{item_def["name"]}. Filing date: {file_date_str}.'
        )
        sic_phrase = self._sic_phrase(filer_info)
        if sic_phrase:
            description = f'{description} {sic_phrase}'

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


class FormDScraper(SECScraper):
    """SEC Form D — private exempt-offering filings (Reg D capital raises).

    Why: private LMM/MM companies raising money are prime NetSuite funding
    triggers, but most never issue a press release — Form D is the only
    public footprint. EFTS filters server-side to territory states via
    locationCodes and exposes exemption items, letting us drop pooled
    investment-fund vehicles (hedge/PE/VC funds, items 3C/3C.1) before any
    lookup. Name patterns + SIC catch the stragglers.
    """

    FUND_VEHICLE_ITEMS = {'3C', '3C.1'}
    # lower-case substring match against the filer name
    FUND_NAME_PATTERNS = (
        ' fund', 'fund l', 'fund,', 'fund i', 'fund v', 'fund x',
        'partners lp', 'partners, lp', 'partners l.p', 'holdings spv',
        ' spv', 'capital i', 'investments l', 'a series of', 'series 0',
        'lending co l', 'real assets', 'acquisition co l', 'feeder l',
        'co-invest', 'coinvest',
    )
    # Investment offices / trusts / blank-check SPACs — vehicles, not
    # operating companies (BLOCKED_SIC_CODES doesn't cover these because
    # Financial Services IS a target vertical for 8-K events).
    FUND_SIC_CODES = {'6722', '6726', '6770'}

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        fd_cfg = config.get('form_d', {}) or {}
        self.enabled = fd_cfg.get('enabled', True)
        # Short lookback: the scraper runs every ~4h and URL-dedup drops
        # repeats, so 3 days gives ample overlap without page-limit risk.
        self.lookback_days = int(fd_cfg.get('lookback_days', 3))
        self.max_results = int(fd_cfg.get('max_results', 300))
        self.fetch_details = bool(fd_cfg.get('fetch_details', True))
        # HTTP budget: each surviving candidate costs 1-3 SEC requests
        # (filer info + index.json + XML). Cap per run so the whole scrape
        # stays inside the GHA job window; newest filings get priority and
        # the 4-hourly cadence + URL dedup pick up the tail next cycle.
        self.max_lookups = int(fd_cfg.get('max_lookups', 50))
        # SEC allows 10 req/s with a proper User-Agent — the polite-crawl
        # delay used for news sites would take minutes here.
        self.sec_sleep = float(fd_cfg.get('request_sleep', 0.25))

    def scrape(self) -> List[TriggerEvent]:
        self.source_statuses = []
        if not self.enabled:
            return []

        label = 'SEC Form D (private raises)'
        events: List[TriggerEvent] = []
        skipped_funds = 0
        items_fetched = 0  # Form D filings fetched from EFTS, pre-filter
        self._reset_gate_counters()
        try:
            startdt = (date.today() - timedelta(days=self.lookback_days)).isoformat()
            enddt = date.today().isoformat()
            loc = ','.join(sorted(self.territory_codes))

            # Phase 1 — collect hits and apply the FREE filters (no HTTP)
            candidates = []
            for page in range(max(1, self.max_results // 100)):
                params = {
                    'q': '', 'forms': 'D', 'dateRange': 'custom',
                    'startdt': startdt, 'enddt': enddt,
                    'locationCodes': loc, 'from': page * 100,
                }
                hits = self._efts_get(params).get('hits', {}).get('hits', []) or []
                if not hits:
                    break
                items_fetched += len(hits)
                for hit in hits:
                    cand, is_fund = self._formd_cheap_filter(hit)
                    if is_fund:
                        skipped_funds += 1
                    elif cand:
                        candidates.append(cand)
                time.sleep(self.sec_sleep)
                if len(hits) < 100:
                    break

            # Phase 2 — newest first, capped HTTP budget
            candidates.sort(key=lambda c: c['file_date'], reverse=True)
            for cand in candidates[:self.max_lookups]:
                ev, is_fund = self._formd_finalize(cand)
                if is_fund:
                    skipped_funds += 1
                elif ev:
                    events.append(ev)

            self.source_statuses.append({
                'source_name':   label,
                'source_type':   'sec_edgar',
                'status':        'success' if events else 'partial',
                'error_message': None if events else 'No operating-company Form Ds in territory',
                'events_found':  len(events),
                'items_fetched': items_fetched,
                'filtered_out':  max(items_fetched - len(events), 0),
            })
            print(f'  - {label}: {len(events)} operating-company raises in '
                  f'territory ({items_fetched} fetched, {skipped_funds} '
                  f'fund vehicles skipped)')
            self._print_gate_summary(label)
        except Exception as e:
            self.source_statuses.append({
                'source_name':   label,
                'source_type':   'sec_edgar',
                'status':        'error',
                'error_message': str(e)[:200],
                'events_found':  0,
                'items_fetched': 0,
                'filtered_out':  0,
            })
            print(f'  - {label}: ERROR {e}')
        return events

    def _formd_cheap_filter(self, hit):
        """Free filters only (no HTTP). Returns (candidate_or_None, is_fund)."""
        source = hit.get('_source', {}) or {}
        if (source.get('file_type') or '') != 'D':
            return None, False  # skip D/A amendments — not a NEW raise

        items = set(source.get('items') or [])
        if items & self.FUND_VEHICLE_ITEMS:
            return None, True  # pooled investment fund (Inv. Co. Act 3(c))

        ciks = source.get('ciks') or []
        display_names = source.get('display_names') or []
        adsh = source.get('adsh') or ''
        if not (ciks and display_names and adsh):
            return None, False
        company_name = re.split(r'\s*\(', display_names[0])[0].strip()

        # Entity-shape gate (gates.py policy) — funds, SPACs, governments,
        # K-12, lodging, political, greek. FUND_NAME_PATTERNS stays as a
        # second belt for the fund shapes the gate's regexes don't cover.
        if self._skip_filer_by_name(company_name):
            return None, False
        low = f' {company_name.lower()} '
        if any(p in low for p in self.FUND_NAME_PATTERNS):
            return None, True

        state = (source.get('biz_states') or [''])[0] or ''
        if not state or state.upper() not in self.territory_codes:
            return None, False  # defensive — locationCodes should ensure this

        if self.is_public_company(company_name):
            return None, False

        return {
            'cik': str(ciks[0]),
            'company_name': company_name,
            'adsh': adsh,
            'state': state,
            'file_date': source.get('file_date') or '',
        }, False

    def _formd_finalize(self, cand):
        """HTTP phase: filer SIC gates + offering details → TriggerEvent.
        Returns (event_or_None, is_fund_vehicle)."""
        cik, company_name = cand['cik'], cand['company_name']
        state, adsh = cand['state'], cand['adsh']
        file_date_str = cand['file_date']

        filer_info = self._lookup_filer_info(cik)
        time.sleep(self.sec_sleep)
        sic = filer_info.get('sic', '')
        if sic and sic in self.FUND_SIC_CODES:
            return None, True
        if self._skip_filer_by_sic(sic, filer_info.get('sic_desc', ''), company_name):
            return None, False

        try:
            published = datetime.fromisoformat(file_date_str).replace(tzinfo=timezone.utc)
        except Exception:
            published = datetime.now(timezone.utc)

        adsh_clean = adsh.replace('-', '')
        url = (f'https://www.sec.gov/Archives/edgar/data/'
               f'{int(cik)}/{adsh_clean}/{adsh}-index.htm')

        details: Dict[str, Any] = {}
        if self.fetch_details:
            details = self._formd_details(cik, adsh_clean)
            if details.get('is_fund'):
                return None, True
        amount_txt   = details.get('amount') or ''
        industry_grp = details.get('industry') or ''
        revenue_rng  = details.get('revenue_range') or ''
        spac_flag    = bool(details.get('spac'))
        entity_type  = details.get('entity_type') or ''

        # Structured verdict (gates.formd_to_verdict): industry group,
        # declared revenue bar (≥$5M, or undisclosed with a ≥$10M raise),
        # SPAC flag. 'unknown' = worth researching → keep.
        verdict, _segment, reason = formd_to_verdict(
            industry_grp, revenue_rng, details.get('amount_float'), spac_flag
        )
        if verdict in ('out', 'vehicle', 'too_small'):
            self._formd_verdict_skipped[verdict] = self._formd_verdict_skipped.get(verdict, 0) + 1
            print(f'    ⏭  Form D {verdict}: {company_name[:40]} — {reason}')
            return None, False

        # Downstream parses these literal phrases — keep the shapes:
        #   'Total offering: $N.' · 'Form D industry group: X.' ·
        #   'Declared revenue: Y.' · 'SPAC: yes.' · 'SIC: NNNN'
        desc_bits = [f'SEC Form D filed by {company_name} ({state}) — '
                     f'private capital raise (Reg D exempt offering).']
        if amount_txt:
            desc_bits.append(f'Total offering: {amount_txt}.')
        if industry_grp:
            desc_bits.append(f'Form D industry group: {industry_grp}.')
        if revenue_rng:
            desc_bits.append(f'Declared revenue: {revenue_rng}.')
        if spac_flag:
            desc_bits.append('SPAC: yes.')
        if entity_type:
            desc_bits.append(f'Entity type: {entity_type}.')
        desc_bits.append(f'Filing date: {file_date_str}.')
        sic_phrase = self._sic_phrase(filer_info)
        if sic_phrase:
            desc_bits.append(sic_phrase)

        return TriggerEvent(
            id=self.generate_event_id(url, company_name),
            title=f'SEC Form D (Private Capital Raise) — {company_name}',
            event_type=EventType.FUNDING,
            source=EventSource.SEC_EDGAR,
            source_name='SEC EDGAR',
            url=url,
            published_date=published,
            company_name=company_name,
            company_location=state,
            description=' '.join(desc_bits),
            relevance_score=80.0,
            matched_regions=[state],
        ), False

    _FORMD_EMPTY: Dict[str, Any] = {
        'amount': '', 'amount_float': None, 'industry': '', 'revenue_range': '',
        'spac': False, 'entity_type': '', 'is_fund': False,
    }

    def _formd_details(self, cik: str, adsh_clean: str) -> Dict[str, Any]:
        """Fetch the Form D primary XML and parse the typed fields:

            amount / amount_float   <totalOfferingAmount>  ('Indefinite' → None)
            industry                <industryGroupType>
            revenue_range           <revenueRange>          (issuerSize)
            spac                    <isBusinessCombinationTransaction>
            entity_type             <entityType>            (primaryIssuer)
            is_fund                 <investmentFundInfo> present / pooled fund

        Best-effort: any failure returns the blank dict rather than dropping
        the event (formd_to_verdict treats blanks as 'unknown')."""
        out: Dict[str, Any] = dict(self._FORMD_EMPTY)
        try:
            idx = self.session.get(
                f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/'
                f'{adsh_clean}/index.json', timeout=self.timeout)
            idx.raise_for_status()
            xml_name = next(
                (f['name'] for f in idx.json().get('directory', {}).get('item', [])
                 if f.get('name', '').endswith('.xml')
                 and 'primary' in f.get('name', '').lower()), None)
            if not xml_name:
                return out
            time.sleep(self.sec_sleep)
            xml = self.session.get(
                f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/'
                f'{adsh_clean}/{xml_name}', timeout=self.timeout).text

            if '<investmentFundInfo>' in xml or 'Pooled Investment Fund' in xml:
                out['is_fund'] = True
                return out

            def tag(name: str) -> str:
                m = re.search(rf'<{name}>([^<]*)</{name}>', xml)
                return html.unescape(m.group(1)).strip() if m else ''

            raw = tag('totalOfferingAmount')
            if raw:
                if raw.lower() == 'indefinite':
                    out['amount'] = 'Indefinite'
                else:
                    try:
                        out['amount_float'] = float(raw)
                        out['amount'] = f'${int(float(raw)):,}'
                    except Exception:
                        out['amount'] = raw
            out['industry'] = tag('industryGroupType')
            out['revenue_range'] = tag('revenueRange')
            out['spac'] = tag('isBusinessCombinationTransaction').lower() == 'true'
            out['entity_type'] = tag('entityType')
            time.sleep(self.sec_sleep)
            return out
        except Exception:
            return out
