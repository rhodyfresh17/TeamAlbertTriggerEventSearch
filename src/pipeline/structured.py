"""Pure helpers the golden set pins: the structured SEC verdict, the
vocabularies enrichment honours, and the public-repo hygiene scrubber
(Phase 4, review 2026-09-08).

WHY THIS MODULE EXISTS: tests/test_golden.py and scripts/build_golden_set.py
used to import the whole Mac-side enrichment_scout for a handful of pure
names. That module also pulls in requests / sqlite3 / the search and LLM
clients, so any future Mac-only top-level import there would have broken the
CI test job, which installs only requests + PyYAML. Everything the golden code
needs now lives here and imports nothing beyond the stdlib and
src.pipeline.gates (tests/test_golden.py refuses an enrichment_scout import in
any of the three golden files).

CANONICAL HOME — enrichment_scout must import from here, never the reverse
(src/pipeline never imports enrichment_scout). The enrichment owner switches
its own definitions to re-imports:

    from src.pipeline.structured import (  # noqa: E402
        structured_verdict as _structured_verdict, pick_primary,
        is_iapd_event as _is_iapd_event,
        ZI_SUBINDUSTRIES, ZI_NOT_A_FIT, ZI_IN_VERTICAL, PRIMARY_ROLE_ORDER,
        WORKABLE_ROLES, REP_NOT_FIT_STATUSES, REP_DECIDED_STATUSES,
    )

Until that lands, tests/test_golden.py::test_structured_agrees_with_enrichment_scout
asserts the two copies agree on six sample descriptions and on every
vocabulary, so they cannot drift apart unnoticed. src/pipeline/accounts.py
carries its own mirror of the vocabularies (ZI_VERTICALS / WORKABLE_ROLES /
REP_*), pinned equal to enrichment_scout by tests/test_accounts.py.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from src.pipeline.gates import formd_to_verdict, sic_to_verdict

# ── vocabularies (copies of enrichment_scout's; see the module docstring) ────
# Source of truth: A.J.'s "FY27 Territories.xlsx" (Subindustries sheet) — the
# 32 ZoomInfo SubIndustries mapped to the 3 NSCorp verticals.
ZI_SUBINDUSTRIES = {
    # ── Financial Services ────────────────────────────────────────────
    'Banking':                                    'Financial Services',
    'Credit Cards & Transaction Processing':      'Financial Services',
    'Debt Collection':                            'Financial Services',
    'Holding Companies & Conglomerates':          'Financial Services',
    'Insurance':                                  'Financial Services',
    'Investment Banking':                         'Financial Services',
    'Lending & Brokerage':                        'Financial Services',
    'Venture Capital & Private Equity':           'Financial Services',
    # ── Nonprofits & Organizations ────────────────────────────────────
    'Blood & Organ Banks':                        'Nonprofits & Organizations',
    'Childcare':                                  'Nonprofits & Organizations',
    'Colleges & Universities':                    'Nonprofits & Organizations',
    'Cultural & Informational Centers':           'Nonprofits & Organizations',
    'K-12 Schools':                               'Nonprofits & Organizations',
    'Libraries':                                  'Nonprofits & Organizations',
    'Membership Organizations':                   'Nonprofits & Organizations',
    'Museums & Art Galleries':                    'Nonprofits & Organizations',
    'Non-Profit & Charitable Organizations':      'Nonprofits & Organizations',
    'Non-Profit Organizations & Charitable Foundations': 'Nonprofits & Organizations',
    'Performing Arts Theaters':                   'Nonprofits & Organizations',
    'Religious Organizations':                    'Nonprofits & Organizations',
    'Training':                                   'Nonprofits & Organizations',
    'Zoos & National Parks':                      'Nonprofits & Organizations',
    # ── Consumer Services ─────────────────────────────────────────────
    'Auctions':                                   'Consumer Services',
    'Automobile Dealers':                         'Consumer Services',
    'Automotive Service & Collision Repair':      'Consumer Services',
    'Barber Shops & Beauty Salons':               'Consumer Services',
    'Cleaning Services':                          'Consumer Services',
    'Consumer Services':                          'Consumer Services',
    'Funeral Homes & Funeral Related Services':   'Consumer Services',
    'Photography Studio':                         'Consumer Services',
    'Real Estate':                                'Consumer Services',
    'Repair Services':                            'Consumer Services',
}
# ALL K-12 (public, private, charter) is not a fit (A.J. 2026-09-04). The
# label stays in ZI_SUBINDUSTRIES as the subindustry → vertical LABEL map;
# the FIT allowlist is ZI_IN_VERTICAL.
ZI_NOT_A_FIT = frozenset({'K-12 Schools'})
ZI_IN_VERTICAL = frozenset(k for k in ZI_SUBINDUSTRIES if k not in ZI_NOT_A_FIT)

# The primary company per event: first role to match wins, in this order.
PRIMARY_ROLE_ORDER = ('acquirer', 'portfolio company', 'hiring company', 'primary', 'target')
# Roles that can carry a WORKABLE account. 'investor' / 'lead investor' were
# removed 2026-09-06: the company that got the money is the account.
WORKABLE_ROLES = ('acquirer', 'portfolio company', 'hiring company', 'primary', 'target')

# Rep verdicts are a HARD input to the pipeline: NOT_FIT statuses mean never
# research / never surface the account again; DECIDED statuses mean the rep
# is working it (fit.verdict = 'decided', no research).
REP_NOT_FIT_STATUSES = frozenset({'Not a Fit', 'Out of Alignment', 'NetSuite Customer'})
REP_DECIDED_STATUSES = frozenset({'Picked Up', 'On Rep TAL'})
REP_STATUSES = REP_NOT_FIT_STATUSES | REP_DECIDED_STATUSES


# ── the structured SEC parser ────────────────────────────────────────────────
# The literals the scrapers embed in an SEC description (sec_scraper.py):
# 'SIC: NNNN (…)', 'Form D industry group: X.', 'Declared revenue: Y.',
# 'Total offering: $N.', 'SPAC: yes.'. src.pipeline.typed.parse_sec_fields
# reads the same strings for the typed columns.
_SIC_RE = re.compile(r'SIC:\s*(\d{4})')
_INDUSTRY_GROUP_RE = re.compile(r'industry group: ([^.]+)\.')
_DECLARED_REVENUE_RE = re.compile(r'Declared revenue: ([^.]+?)\.(?:\s|$)')
_TOTAL_OFFERING_RE = re.compile(r'Total offering: \$([\d,]+)')


def structured_verdict(event: Optional[dict]) -> dict:
    """Free, deterministic pre-search verdict from the structured facts the
    scrapers embed in the description (SIC code; Form D industry group,
    declared revenue range, offering amount, SPAC flag).

    {'verdict': 'out' | 'vehicle' | 'too_small' | 'in' | 'unknown',
     'reason': str, 'revenue_segment': str}

    Faithful copy of enrichment_scout._structured_verdict (review 2026-09-08
    (Phase 4)): only sec.gov events are read; a SIC that is out / a vehicle
    decides first; a Form D title then goes through gates.formd_to_verdict.
    """
    event = event or {}
    desc = event.get('description') or ''
    out = {'verdict': 'unknown', 'reason': '', 'revenue_segment': ''}
    if 'sec.gov' not in (event.get('source_url') or ''):
        return out
    m = _SIC_RE.search(desc)
    if m:
        v, why = sic_to_verdict(m.group(1))
        if v in ('out', 'vehicle'):
            return {'verdict': v, 'reason': why, 'revenue_segment': ''}
    if 'Form D' in (event.get('title') or ''):
        grp = _INDUSTRY_GROUP_RE.search(desc)
        rr = _DECLARED_REVENUE_RE.search(desc)
        amt = _TOTAL_OFFERING_RE.search(desc)
        amount = float(amt.group(1).replace(',', '')) if amt else None
        spac = 'SPAC: yes' in desc
        v, seg, why = formd_to_verdict(grp.group(1).strip() if grp else None,
                                       rr.group(1).strip() if rr else None, amount, spac)
        return {'verdict': v, 'reason': why, 'revenue_segment': seg}
    return out


# ── small pure helpers the golden exporter needs ─────────────────────────────
def pick_primary(companies_data: Optional[list]) -> dict:
    """The primary company per PRIMARY_ROLE_ORDER (first match wins, in
    priority order), falling back to the first listed company. Copy of
    enrichment_scout.pick_primary."""
    if not companies_data:
        return {}
    for role in PRIMARY_ROLE_ORDER:
        for c in companies_data:
            if str(c.get('role', '')).lower() == role:
                return c
    return companies_data[0]


def is_iapd_event(event: Optional[dict]) -> bool:
    """A 'New SEC-registered investment adviser' trigger from
    scripts/ria_trigger.py: typed source 'sec_iapd', or — when the typed
    column is not live — the adviserinfo.sec.gov source_url it always sets.
    Copy of enrichment_scout._is_iapd_event."""
    event = event or {}
    if str(event.get('source') or '').strip().lower() == 'sec_iapd':
        return True
    try:
        host = (urlparse(str(event.get('source_url') or '')).hostname or '').lower()
    except ValueError:
        return False
    return host == 'adviserinfo.sec.gov' or host.endswith('.adviserinfo.sec.gov')


# ── public-repo hygiene ──────────────────────────────────────────────────────
# The golden file lives in a public repo: emails and phone numbers are removed
# from every free-text field by the exporter (scrub) and rejected by the
# schema test with the SAME regexes, so the two can never disagree.
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
# North American numbers with OR WITHOUT separators (review 2026-09-08
# (Phase 4): the old regex required separators, so '8005551234' passed).
# Area code and exchange start with 2-9 (the NANP rule), which is what keeps
# '$22,500,000', '801-136602', 'CRD 342950', '2026-09-08' and SEC accession
# numbers ('0001234567-26-000123') out. A neighbouring digit or hyphen means
# the run is part of a longer identifier, not a phone number.
PHONE_RE = re.compile(
    r'(?<![\w-])(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?[2-9]\d{2}[\s.-]?\d{4}(?![\w-])')
_WS_RE = re.compile(r'\s+')


def scrub(text) -> str:
    """Drop emails and phone numbers, normalize whitespace. Applied to EVERY
    free-text field before it is written to the golden file."""
    s = str(text or '')
    s = EMAIL_RE.sub('', s)
    s = PHONE_RE.sub('', s)
    return _WS_RE.sub(' ', s).strip()
