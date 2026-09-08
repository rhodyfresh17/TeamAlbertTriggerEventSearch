"""Free structured ORACLES for TeamAlbert v2 (Phase 3 slice B2, 2026-09-08).

WHY: search (self-hosted Firecrawl → rationed Tavily) is the scarce resource
in enrichment. Banks, credit unions, registered investment advisers and
nonprofits are REGISTERED entities: a public registry already states where
they are, what they are and roughly how big they are. This module answers
territory / vertical / revenue / url for those account shapes with ZERO
search, from three keyless sources (all verified live, research 2026-09-08):

  * SEC IAPD monthly compilation feed  — every SEC-registered adviser (RIA)
    and exempt reporting adviser (ERA): HQ, registration type/status/date,
    website, headcount, regulatory AUM. 7 MB gz → 82 MB XML, ~23.8K firms,
    published on the 1st of each month. Streamed into the local `ria_firm`
    table by scripts/refresh_oracles.py (xml.etree.iterparse — never loaded
    as one string).
  * FDIC BankFind institutions API      — every FDIC-insured bank (~4.5K
    active) with HQ city/state, total assets, website, holding company.
    Pulled whole into the local `bank` table; a live wildcard search is the
    fallback for a bank the table doesn't have yet.
  * ProPublica Nonprofit Explorer v2    — IRS-registered nonprofits with
    NTEE code and Form 990 revenue. Live only (name + state[id] search,
    then organizations/{ein}.json), cached in the AccountCache.

Contract: `lookup(name, hint)` NEVER raises and returns None on any miss;
readers fail soft when state/oracles.db (gitignored) or a table is absent.
Live calls identify themselves with the SEC User-Agent from config.yaml
(SEC fair-access rules) and are throttled to a few per second.
"""
from __future__ import annotations

import difflib
import gzip
import json
import logging
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from src.pipeline.gates import (ALL_STATE_CODES, TERRITORY_STATES, account_key,
                                hq_state_code)

log = logging.getLogger(__name__)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB_PATH = os.path.join(_REPO, 'state', 'oracles.db')

# ── Sources (URLs verified live, research 2026-09-08) ───────────────────────
SEC_IAPD_FEED_URL = ('https://reports.adviserinfo.sec.gov/reports/CompilationReports/'
                     'IA_FIRM_SEC_Feed_{mm}_{dd}_{yyyy}.xml.gz')
# The documented host banks.data.fdic.gov/api/institutions answers with a
# 301 to this one — and the redirect hop is metered at 20/min while the
# target is the 120/min API, so call the target directly.
FDIC_INSTITUTIONS_URL = 'https://api.fdic.gov/banks/institutions'
FDIC_FIELDS = 'NAME,CITY,STALP,ASSET,WEBADDR,ACTIVE,CERT,ESTYMD,NAMEHCR,BKCLASS'
PROPUBLICA_SEARCH_URL = 'https://projects.propublica.org/nonprofits/api/v2/search.json'
PROPUBLICA_ORG_URL = 'https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json'
HTTP_TIMEOUT = 25
# Minimum gap between live calls: SEC allows 10 req/s, FDIC 120/min,
# ProPublica is unmetered but CDN-cached 24h — 0.25s keeps us far under all.
LIVE_MIN_INTERVAL = 0.25
_FALLBACK_USER_AGENT = 'TeamAlbert Sales Intelligence (sales-leads@teamalbert.local)'

# AccountCache kinds for live results (90d = the cache module's default TTL;
# a registry answer moves slower than a search result).
CACHE_KIND_FDIC = 'oracle_fdic'
CACHE_KIND_PROPUBLICA = 'oracle_propublica'

# ── Revenue mapping (research 2026-09-08) ───────────────────────────────────
# Banks publish ASSETS, not revenue. Gross revenue (interest income +
# non-interest income) of US commercial/savings banks runs ~5-6% of total
# assets at 2026 rate levels (FDIC QBP: NIM ~3.3% + non-interest income
# ~1.5-2% of assets, before funding costs) — 0.055 is the midpoint. It is an
# ESTIMATE, labelled as such in revenue_source.
BANK_ASSET_TO_REV = 0.055
# RIAs publish regulatory AUM and headcount. Advisory fees average ~0.7% of
# RAUM (blended across wealth managers at ~1% and institutional managers
# at ~0.3-0.5%); revenue per employee at a typical RIA is ~$400K. When both
# exist the MIN is used — the more conservative figure — so a firm is never
# pushed into a higher band by the looser of two proxies.
RIA_RAUM_TO_REV = 0.007
RIA_REV_PER_EMP = 400_000
# A.J.'s research bar: below $5M revenue an account is not worth a search
# (the Form D declared-revenue rule in gates.formd_to_verdict uses the same
# line). The caller fails revenue the way that rule does.
TOO_SMALL_USD = 5_000_000
# NetSuite up-market segments (CLAUDE.md §2): LMM <$10M · MM $10-20M ·
# Corp $20-100M · Enterprise >$100M (out of band).
SEGMENT_CUTS = ((10_000_000, 'LMM'), (20_000_000, 'MM'), (100_000_000, 'Corp'))
ENTERPRISE_USD = 100_000_000
# P3 (review 2026-09-08): the bank 5.5%-of-assets and the RIA proxies are
# ±30% ESTIMATES, yet they tombstoned at BOTH edges — a $4.9M estimate is
# as likely a $6M firm as a $4M one, and a $105M one may be an $85M Corp.
# So an estimate only decides OUTSIDE a ±30% margin band around each edge;
# inside the band the registry answers "unknown" (revenue None, too_small
# False) and ONE search decides, exactly as for any account without a
# registry answer. A Form 990 total revenue is a FACT, not an estimate, and
# keeps the sharp edges (too_small / revenue_segment).
ESTIMATE_MARGIN = 0.30
TOO_SMALL_FLOOR = 3_500_000        # estimate <  → too_small even if it is 30% low
TOO_SMALL_CEIL = 6_500_000         # estimate <  → unknown (a search decides)
ENTERPRISE_FLOOR = 77_000_000      # estimate >= → unknown (a search decides)
ENTERPRISE_CEIL = 130_000_000      # estimate >  → Enterprise even if it is 30% high
assert TOO_SMALL_FLOOR == round(TOO_SMALL_USD * (1 - ESTIMATE_MARGIN))
assert TOO_SMALL_CEIL == round(TOO_SMALL_USD * (1 + ESTIMATE_MARGIN))
assert ENTERPRISE_CEIL == round(ENTERPRISE_USD * (1 + ESTIMATE_MARGIN))
assert ENTERPRISE_FLOOR == round(ENTERPRISE_USD / (1 + ESTIMATE_MARGIN), -6)

# ── Matching ────────────────────────────────────────────────────────────────
MATCH_THRESHOLD = 0.85     # score a candidate needs to count as a hit
AMBIGUITY_MARGIN = 0.05    # two hits this close, in different states → no hit
# ProPublica without a state[id] filter (research 2026-09-08): a bare
# "Habitat for Humanity" out-scores every local affiliate with the national
# office (containment 0.9) and would hand the event a Georgia HQ and an
# Enterprise revenue. With no state to anchor on, only a near-exact name
# may count — the registries (local tables) keep the normal bar because
# their cross-state ties are already refused by pick_best.
NPO_NO_STATE_THRESHOLD = 0.95

# ── Schema (state/oracles.db) ───────────────────────────────────────────────
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS ria_firm (
        crd             INTEGER PRIMARY KEY,
        business_name   TEXT,
        legal_name      TEXT,
        norm_name       TEXT,
        city            TEXT,
        state           TEXT,
        country         TEXT,
        firm_type       TEXT,
        reg_status      TEXT,
        reg_date        TEXT,
        website         TEXT,
        total_employees INTEGER,
        raum_usd        INTEGER,
        sec_number      TEXT,
        as_of           TEXT
    )''',
    'CREATE INDEX IF NOT EXISTS ria_firm_state ON ria_firm(state)',
    'CREATE INDEX IF NOT EXISTS ria_firm_norm ON ria_firm(norm_name)',
    '''CREATE TABLE IF NOT EXISTS bank (
        cert        INTEGER PRIMARY KEY,
        name        TEXT,
        norm_name   TEXT,
        city        TEXT,
        state       TEXT,
        asset_kusd  INTEGER,
        website     TEXT,
        est_date    TEXT,
        holding_co  TEXT,
        bkclass     TEXT,
        active      INTEGER,
        as_of       TEXT
    )''',
    'CREATE INDEX IF NOT EXISTS bank_state ON bank(state)',
    'CREATE INDEX IF NOT EXISTS bank_norm ON bank(norm_name)',
    '''CREATE TABLE IF NOT EXISTS oracle_meta (
        source       TEXT PRIMARY KEY,
        refreshed_at TEXT,
        rows         INTEGER,
        src_url      TEXT
    )''',
)

_RIA_COLUMNS = ('crd', 'business_name', 'legal_name', 'norm_name', 'city', 'state',
                'country', 'firm_type', 'reg_status', 'reg_date', 'website',
                'total_employees', 'raum_usd', 'sec_number', 'as_of')
_BANK_COLUMNS = ('cert', 'name', 'norm_name', 'city', 'state', 'asset_kusd', 'website',
                 'est_date', 'holding_co', 'bkclass', 'active', 'as_of')


# ═══════════════════════════════════════════════════════════════════════════
# Name normalization + matching
# ═══════════════════════════════════════════════════════════════════════════

# Abbreviations/aliases folded to one spelling BEFORE tokenizing, so "YMCA of
# Greater Boston" and "Young Men's Christian Association of Greater Boston"
# (how the IRS lists it) are the same string. Order matters: the multi-token
# forms first. Regexes run on the lower-cased name.
_ALIASES = (
    (r"\by\.?\s?m\.?\s?c\.?\s?a\b", 'young mens christian association'),
    (r"\by\.?\s?w\.?\s?c\.?\s?a\b", 'young womens christian association'),
    (r"\bf\.?\s?c\.?\s?u\b", 'federal credit union'),
    (r"\bfed(?:eral)?\s+c\.?\s?u\b", 'federal credit union'),
    (r"\bc\.?\s?u\b(?=\s*$)", 'credit union'),          # trailing "CU" only
    (r"\bn\.\s?a\.?(?=[\s,;)]|$)", 'national association'),
    (r"\bna\b(?=\s*$)", 'national association'),        # "Bank of X NA"
    (r"\bf\.?\s?s\.?\s?b\b", 'federal savings bank'),
    (r"\bs\.?\s?s\.?\s?b\b(?=\s*$)", 'state savings bank'),
    (r"\bb\s*&\s*t\b", 'bank and trust'),
    (r"\bnat(?:'|’)?l\b", 'national'),
    (r"\bassn\b|\bassoc\b|\bass'n\b", 'association'),
    (r"\bsvgs\b|\bsvg\b", 'savings'),
    (r"\bbk\b", 'bank'),
    (r"\bmut\b", 'mutual'),
    (r"\bfdn\b|\bfndn\b", 'foundation'),
    (r"\buniv\b", 'university'),
    (r"\bintl\b|\bint'l\b", 'international'),
    (r"\bmgmt\b|\bmgt\b", 'management'),
    (r"\badvisers?\b|\badv\b", 'advisors'),
    (r"\badvisor\b", 'advisors'),
    (r"\bctr\b|\bcentre\b", 'center'),
    (r"\bsaint\b", 'st'),
    (r"\bcmty\b", 'community'),
    (r"\bco-?op(?:erative)?\b", 'cooperative'),
)
# Legal-form suffixes and connective words carry no identity. They are
# dropped ANYWHERE in the name (FDIC legal names put "Company" mid-string:
# "The Washington Trust Company, of Westerly").
_STOP_TOKENS = {
    'the', 'of', 'and', 'a', 'an',
    'inc', 'incorporated', 'corp', 'corporation', 'co', 'company', 'cos',
    'llc', 'llp', 'lp', 'ltd', 'limited', 'plc', 'pllc', 'pc', 'lc',
    'sa', 'ag', 'gmbh', 'nv', 'bv',
}
# Tokens too common (within these registries) to pick a candidate set by.
_GENERIC_TOKENS = {
    'bank', 'banks', 'banking', 'bancorp', 'bancshares', 'bankshares', 'banc',
    'trust', 'savings', 'national', 'association', 'federal', 'first', 'community',
    'state', 'united', 'american', 'america', 'capital', 'management', 'partners',
    'advisors', 'advisory', 'wealth', 'investment', 'investments', 'investors',
    'group', 'financial', 'finance', 'services', 'fund', 'funds', 'asset', 'assets',
    'new', 'north', 'south', 'east', 'west', 'county', 'city', 'home', 'citizens',
    'peoples', 'farmers', 'merchants', 'security', 'union', 'credit', 'mutual',
    'foundation', 'center', 'institute', 'university', 'college', 'society',
    'council', 'church', 'museum', 'global', 'international', 'private', 'equity',
    'holdings', 'holding', 'family', 'office', 'planning', 'strategies', 'strategic',
    'research', 'us', 'usa', 'greater', 'young', 'mens', 'womens', 'christian',
}

_POSSESSIVE_RE = re.compile(r"(?<=\w)['’]s\b")
_NON_ALNUM_RE = re.compile(r'[^a-z0-9 ]+')
_WS_RE = re.compile(r'\s+')
# Affiliate / sub-entity markers (research 2026-09-08, live): the IRS lists a
# nonprofit's realty arm, foundation, endowment or PTA as its own filer under
# the parent's name — "Ymca Of Greater Boston Realty Corp" ($276K) beside
# "Young Mens Christian Association Of Greater Boston Inc" ($100M+). A marker
# present on ONE side only means a different entity: −0.3, like a state
# conflict. Bank vocabulary (trust, savings, bank) is deliberately absent —
# "Bar Harbor Bank" must still find "Bar Harbor Bank & Trust".
_AFFILIATE_MARKERS = {
    'realty', 'foundation', 'endowment', 'holdings', 'properties', 'housing',
    'auxiliary', 'friends', 'alumni', 'boosters', 'booster', 'fund', 'funds',
    'scholarship', 'chapter', 'pension', 'welfare', 'benefit', 'supporting',
    'support', 'pto', 'pta', 'guild', 'volunteers',
}
# The geography bonus may confirm a near-match, never rescue a weak one:
# "boston partners" vs "boston millennia partners" is 0.75 on the name and a
# shared state must not lift it over the bar. Review 2026-09-08 (H1 d): it
# is granted only when the raw score came from a TOKEN channel (exact,
# containment, Jaccard) — never from the difflib CHARACTER ratio alone.
# "Beacon Hill Partners" vs "BEACON CAPITAL PARTNERS" is 0.837 on characters
# and 0.5 on tokens; +0.1 for a shared state made it a 0.937 "hit" that
# handed the event another firm's revenue and url. A difflib-only score
# must clear MATCH_THRESHOLD on its own.
RAW_BONUS_FLOOR = 0.8
AFFILIATE_PENALTY = 0.3
_BONUS_CHANNELS = ('exact', 'contain', 'jaccard')


def normalize_name(name: Optional[str]) -> str:
    """Casefold; '&'→'and'; aliases; possessives; punctuation; drop legal
    suffixes/connectives ANYWHERE. 'The Washington Trust Company, of
    Westerly' → 'washington trust westerly'."""
    s = (name or '').strip().lower()
    if not s:
        return ''
    s = s.replace('&', ' and ')
    s = _POSSESSIVE_RE.sub('s', s)               # mary's → marys
    for pat, repl in _ALIASES:
        s = re.sub(pat, repl, s)
    s = s.replace("'", '').replace('’', '')
    s = _NON_ALNUM_RE.sub(' ', s)
    toks = [t for t in _WS_RE.split(s) if t and t not in _STOP_TOKENS]
    return ' '.join(toks)


def name_tokens(name: Optional[str]) -> List[str]:
    return normalize_name(name).split()


def expand_aliases(name: Optional[str]) -> str:
    """The name with the abbreviation aliases spelled out but nothing else
    dropped — a second query form for a registry whose search is token-
    based: ProPublica finds 'Young Mens Christian Association of Greater
    Boston' and not 'YMCA of Greater Boston' (live 2026-09-08)."""
    s = (name or '').strip().lower().replace('&', ' and ')
    s = _POSSESSIVE_RE.sub('s', s)
    for pat, repl in _ALIASES:
        s = re.sub(pat, repl, s)
    return _WS_RE.sub(' ', s).strip()


def _contained_at_end(short: List[str], long: List[str]) -> bool:
    """Registry legal names carry TAILS the press drops (', of Westerly',
    'National Association', 'Inc') — never middle insertions: 'boston
    partners' is not 'boston millennia partners'."""
    n = len(short)
    return n >= 2 and (long[:n] == short or long[-n:] == short)


def _distinctive_tokens(norm: str) -> List[str]:
    """Tokens worth pulling a candidate set by — longest (≈ rarest) first.
    Empty for an all-generic name ('First National Bank', 'Wealth
    Management'): review 2026-09-08 (H1 c) — such a name identifies NO
    registrant, and every adapter refuses to look it up rather than match
    whatever the state happens to hold."""
    toks = [t for t in norm.split() if t not in _GENERIC_TOKENS and len(t) >= 3
            and not t.isdigit()]
    return sorted(dict.fromkeys(toks), key=lambda t: (-len(t), t))


def score_detail(query_norm: str, cand_norm: str,
                 hint_state: Optional[str] = None, cand_state: Optional[str] = None,
                 hint_city: Optional[str] = None, cand_city: Optional[str] = None) -> dict:
    """score_names with its working shown: {'score', 'raw', 'channel'}.
    `raw` is the name-only similarity BEFORE the geography bonus / state
    penalty / affiliate penalty; `channel` says which measure produced it —
    'exact' (identical normalized names), 'contain' (head/tail containment),
    'jaccard' (token overlap) or 'difflib' (character ratio). Callers rank
    by `raw` to break ties the bonus hides (review 2026-09-08, H1 b: an
    exact 1.0 and a contained 0.9 both clip to 1.0 after +0.1), refuse
    difflib-only evidence where a registrant must be named exactly (L4),
    and grant the bonus only to the token channels (H1 d)."""
    none = {'score': 0.0, 'raw': 0.0, 'channel': None}
    if not query_norm or not cand_norm:
        return none
    qa, qb = query_norm.split(), cand_norm.split()
    a, b = set(qa), set(qb)
    if not a or not b:
        return none
    if query_norm == cand_norm:
        raw, channel = 1.0, 'exact'
    else:
        jacc = len(a & b) / len(a | b)
        ratio = difflib.SequenceMatcher(None, query_norm, cand_norm).ratio()
        contain = 0.0
        short, long = (qa, qb) if len(qa) <= len(qb) else (qb, qa)
        if _contained_at_end(short, long):
            contain = max(0.0, 1.0 - 0.1 * (len(long) - len(short)))
        # Ties between measures resolve toward the token channels.
        raw, channel = max(((contain, 'contain'), (jacc, 'jaccard'), (ratio, 'difflib')),
                           key=lambda t: (t[0], t[1] != 'difflib'))
    s = raw
    if (a ^ b) & _AFFILIATE_MARKERS:
        s -= AFFILIATE_PENALTY
    bonus_ok = raw >= RAW_BONUS_FLOOR and channel in _BONUS_CHANNELS
    hs = (hint_state or '').strip().upper() or None
    cs = (cand_state or '').strip().upper() or None
    if hs and cs:
        if hs != cs:
            s -= 0.3
        elif bonus_ok:
            s += 0.1
    elif (bonus_ok and hint_city and cand_city
            and normalize_name(hint_city) == normalize_name(cand_city)):
        s += 0.1
    return {'score': max(0.0, min(1.0, s)), 'raw': raw, 'channel': channel}


def score_names(query_norm: str, cand_norm: str,
                hint_state: Optional[str] = None, cand_state: Optional[str] = None,
                hint_city: Optional[str] = None, cand_city: Optional[str] = None) -> float:
    """0-1 similarity of two NORMALIZED names, geography-adjusted:
    max(token-set Jaccard, difflib ratio, containment) + 0.1 when the state
    (or, without a state hint, the city) agrees — only for a raw name score
    of RAW_BONUS_FLOOR or better AND only when that raw score came from a
    token channel (see _BONUS_CHANNELS) — and − 0.3 when the states conflict
    or an affiliate marker sits on one side only. Containment (research
    2026-09-08): registry legal names carry TAILS the press never uses —
    ', of Westerly', 'National Association' — so a query that is the head
    or tail of the candidate (or vice versa) scores 1.0 minus 0.1 per extra
    token, provided the shorter side has at least two tokens ('Washington'
    alone must not match everything)."""
    return score_detail(query_norm, cand_norm, hint_state, cand_state, hint_city, cand_city)['score']


def _raw_of(c: dict) -> float:
    raw = c.get('raw')
    return float(raw if raw is not None else (c.get('score') or 0.0))


def _same_registrant(a: dict, b: dict) -> bool:
    """Two candidates are the SAME registrant only when both carry a
    source_id and it agrees; hand-built candidates without ids are
    different by default (a wrong hit is worse than none)."""
    ia, ib = a.get('source_id'), b.get('source_id')
    return ia is not None and ib is not None and str(ia) == str(ib)


def pick_best(candidates: List[dict], threshold: float = MATCH_THRESHOLD) -> Optional[dict]:
    """Best candidate (each carries 'score', 'raw', 'state', 'source_id') if
    it clears the threshold and is not TIED with a different registrant.
    Ranking is by score, then by the raw name score before the geography
    bonus (review 2026-09-08, H1 b: exact 1.0 beats contained 0.9 although
    both clip to 1.0 with the bonus). Two different source_ids within
    AMBIGUITY_MARGIN in different states ('Washington Trust' is RI and WA
    at once) or on exactly the same score AND raw score in the same state
    (H1 a: 'Community Bank' + NY names three banks; the old size tie-break
    picked the biggest and its revenue tombstoned the event) → no hit, and
    the candidates are logged so the miss can be audited."""
    ranked = sorted(candidates, key=lambda c: (-(c.get('score') or 0.0), -_raw_of(c)))
    if not ranked or (ranked[0].get('score') or 0.0) < threshold:
        return None
    best = ranked[0]
    for other in ranked[1:]:
        if (other.get('score') or 0.0) < threshold:
            break
        if _same_registrant(best, other):
            continue
        if best['score'] - other['score'] < AMBIGUITY_MARGIN and \
                (other.get('state') or '') != (best.get('state') or ''):
            log.info('oracle: ambiguous match %r (%s) vs %r (%s) at %.2f/%.2f — no hit',
                     best.get('name'), best.get('state'), other.get('name'), other.get('state'),
                     best['score'], other['score'])
            return None
        if (abs(best['score'] - other['score']) < 1e-6
                and abs(_raw_of(best) - _raw_of(other)) < 1e-6):
            tied = [c for c in ranked if abs(c.get('score', 0) - best['score']) < 1e-6
                    and abs(_raw_of(c) - _raw_of(best)) < 1e-6]
            log.info('oracle: %d registrants tie at %.2f — no hit: %s', len(tied), best['score'],
                     '; '.join(f'{c.get("name")} [{c.get("source_id")}, {c.get("state")}]'
                               for c in tied[:5]))
            return None
    return best


# ═══════════════════════════════════════════════════════════════════════════
# Name-shape tests (which adapters 'auto' runs)
# ═══════════════════════════════════════════════════════════════════════════

# Two tiers (review 2026-09-08, L4). STRONG tokens name an adviser and get
# the normal fuzzy matching. WEAK tokens — "partners", "capital", "ventures",
# "equity", "management" — are shared with law firms, consultancies and
# real-estate shops ('Zorblat Law Partners', 'Zorblat Capital Realty'); for
# those the RIA adapter runs in STRICT mode: a state hint is required and
# only an exact / containment match in that state counts (never difflib or
# token overlap). ERAs such as "MK Capital" are still found when the state
# is known and the name is the registrant's.
_RIA_STRONG_RE = re.compile(
    r"\b(advis[oe]rs?|advisory|wealth|asset management|capital management|"
    r"investment management|investment counsel|investment(s)?|family office|"
    r"financial planning|portfolio management|fund management)\b", re.I)
_RIA_WEAK_RE = re.compile(r"\b(ventures?|capital|equity|management|partners)\b", re.I)
_BANK_RE = re.compile(
    r"\b(banks?|banking|bancorp(oration)?|bancshares|bankshares|banc|savings|"
    r"savings and loan|s&l|thrift)\b", re.I)
_TRUST_RE = re.compile(r"\btrust\b", re.I)
_NOT_BANK_TRUST_RE = re.compile(
    r"\b(charitable|family|land|conservation|nature|realty|investment|unit|equity|"
    r"income|royalty|living|revocable|foundation|community foundation|reit|"
    r"educational|scholarship|memorial)\s+trust\b", re.I)
_CREDIT_UNION_RE = re.compile(r"\b(credit union|fcu)\b", re.I)
_NPO_RE = re.compile(
    r"\b(foundation|association|society|council|museum|ymca|ywca|church|parish|"
    r"diocese|temple|synagogue|mosque|ministr(y|ies)|mission|universit(y|ies)|college|"
    r"institute|alliance|charit(y|ies|able)|united way|orchestra|symphony|philharmonic|"
    r"ballet|opera|theatre|theater|librar(y|ies)|zoo|aquarium|botanical|arboretum|"
    r"academy|seminary|conservancy|hospice|habitat for humanity|goodwill|"
    r"salvation army|red cross|boys (and|&) girls club|rotary|chamber of commerce|"
    r"coalition|league|federation|fellowship|nonprofit|non-profit|credit union|fcu|"
    r"hospital|health system|medical center|(community|arts|cultural|science|nature|"
    r"senior|learning|resource|health|family|youth|civic|performing arts|wellness|"
    r"crisis|counseling|community health|cancer|children'?s) cent(er|re)s?)\b", re.I)
_FOR_PROFIT_FORM_RE = re.compile(r"\b(llc|l\.l\.c\.|lp|l\.p\.|plc|corp|corporation|ltd|limited)\b\.?", re.I)


def ria_shape(name: str) -> Optional[str]:
    """'strong' (adviser-ish token: fuzzy lookup), 'weak' ('partners' /
    'capital' / 'ventures' / 'equity' / 'management' only: strict lookup —
    state required, exact or containment match, no fuzzy) or None."""
    n = name or ''
    if _RIA_STRONG_RE.search(n):
        return 'strong'
    if _RIA_WEAK_RE.search(n):
        return 'weak'
    return None


def looks_like_ria(name: str) -> bool:
    """True only for the STRONG shape; weak names go through ria_shape()."""
    return ria_shape(name) == 'strong'


def looks_like_bank(name: str) -> bool:
    """Bank / savings / trust company / bancorp — the FDIC universe. Credit
    unions are NCUA-insured, not FDIC: they route to the nonprofit adapter
    (state-chartered CUs file Form 990)."""
    n = name or ''
    if _CREDIT_UNION_RE.search(n):
        return False
    if _BANK_RE.search(n):
        return True
    return bool(_TRUST_RE.search(n) and not _NOT_BANK_TRUST_RE.search(n))


def looks_like_npo(name: str) -> bool:
    n = name or ''
    if _FOR_PROFIT_FORM_RE.search(n) and not _CREDIT_UNION_RE.search(n):
        return False
    return bool(_NPO_RE.search(n))


def kind_for_zi(zi: Optional[str], name: str = '') -> Optional[str]:
    """Adapter kind implied by an article-classified ZoomInfo subindustry —
    the Stage A second chance when the name shape said nothing."""
    z = (zi or '').strip()
    if z == 'Banking':
        return 'npo' if _CREDIT_UNION_RE.search(name or '') else 'bank'
    if z in ('Lending & Brokerage', 'Investment Banking', 'Venture Capital & Private Equity'):
        return 'ria'
    if z in NPO_ZI_SET:
        return 'npo'
    return None


NPO_ZI_SET = {
    'Non-Profit & Charitable Organizations',
    'Non-Profit Organizations & Charitable Foundations',
    'Museums & Art Galleries', 'Performing Arts Theaters',
    'Cultural & Informational Centers', 'Colleges & Universities', 'K-12 Schools',
    'Libraries', 'Religious Organizations', 'Membership Organizations',
    'Zoos & National Parks', 'Childcare', 'Training', 'Blood & Organ Banks',
}
# Subindustries that trigger the Stage A second chance.
SECOND_CHANCE_ZI = {'Banking', 'Lending & Brokerage', 'Investment Banking',
                    'Venture Capital & Private Equity'} | NPO_ZI_SET


# ═══════════════════════════════════════════════════════════════════════════
# Revenue mapping
# ═══════════════════════════════════════════════════════════════════════════

def revenue_segment(usd: Optional[float]) -> Optional[str]:
    if usd is None:
        return None
    try:
        v = float(usd)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    for cut, seg in SEGMENT_CUTS:
        if v <= cut:
            return seg
    return 'Enterprise'


def too_small(usd: Optional[float]) -> bool:
    """True only for a POSITIVE estimate under the $5M bar — 0/None is
    'unknown', never 'too small' (a church with no 990 has no revenue figure,
    not a tiny one)."""
    try:
        return usd is not None and 0 < float(usd) < TOO_SMALL_USD
    except (TypeError, ValueError):
        return False


def estimate_bank_revenue(asset_kusd: Optional[int]) -> Optional[int]:
    """FDIC ASSET is in $ thousands."""
    try:
        if asset_kusd is None or int(asset_kusd) <= 0:
            return None
        return int(round(int(asset_kusd) * 1000 * BANK_ASSET_TO_REV))
    except (TypeError, ValueError):
        return None


def ria_revenue_proxies(raum_usd: Optional[int],
                        total_employees: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    """(RAUM × 0.7%, employees × $400K) — each None when not reported."""
    by_raum = by_emp = None
    try:
        if raum_usd and int(raum_usd) > 0:
            by_raum = int(round(int(raum_usd) * RIA_RAUM_TO_REV))
    except (TypeError, ValueError):
        pass
    try:
        if total_employees and int(total_employees) > 0:
            by_emp = int(total_employees) * RIA_REV_PER_EMP
    except (TypeError, ValueError):
        pass
    return by_raum, by_emp


def estimate_ria_revenue(raum_usd: Optional[int], total_employees: Optional[int]) -> Optional[int]:
    """min(RAUM × 0.7%, employees × $400K) when both exist; whichever exists
    otherwise; None when neither (ERAs report neither). The MIN places the
    SEGMENT; it never decides too_small on its own — see ria_revenue_band."""
    ests = [p for p in ria_revenue_proxies(raum_usd, total_employees) if p]
    return min(ests) if ests else None


def estimate_band(usd: Optional[float]) -> Tuple[Optional[str], bool]:
    """(segment | None, too_small) for a ±30% ESTIMATE (P3, review
    2026-09-08). Sharp only outside the margin bands: < TOO_SMALL_FLOOR →
    too_small; [TOO_SMALL_FLOOR, TOO_SMALL_CEIL) and [ENTERPRISE_FLOOR,
    ENTERPRISE_CEIL] → (None, False), unknown — one search decides; >
    ENTERPRISE_CEIL → Enterprise; in between → the NetSuite segment."""
    try:
        v = float(usd) if usd is not None else None
    except (TypeError, ValueError):
        return None, False
    if v is None or v <= 0:
        return None, False
    if v < TOO_SMALL_FLOOR:
        return revenue_segment(v), True
    if v < TOO_SMALL_CEIL:
        return None, False
    if v > ENTERPRISE_CEIL:
        return 'Enterprise', False
    if v >= ENTERPRISE_FLOOR:
        return None, False
    return revenue_segment(v), False


def ria_revenue_band(raum_usd: Optional[int], total_employees: Optional[int]) -> dict:
    """{'segment', 'too_small', 'estimate', 'estimate_max'} for an adviser.
    M1 (review 2026-09-08): the MIN proxy places the segment (a $15B-RAUM
    firm with 40 staff is a 40-person firm) but too_small is decided by the
    MAX proxy — 10 staff ($4M) with $2B RAUM ($14M) used to be tombstoned on
    the pessimistic figure. too_small only when even the optimistic proxy is
    under TOO_SMALL_FLOOR; a MIN inside the small margin band → unknown."""
    by_raum, by_emp = ria_revenue_proxies(raum_usd, total_employees)
    ests = [p for p in (by_raum, by_emp) if p]
    if not ests:
        return {'segment': None, 'too_small': False, 'estimate': None, 'estimate_max': None}
    lo, hi = min(ests), max(ests)
    if hi < TOO_SMALL_FLOOR:
        return {'segment': revenue_segment(lo), 'too_small': True, 'estimate': lo, 'estimate_max': hi}
    seg, _ = estimate_band(lo)
    if lo < TOO_SMALL_CEIL:
        seg = None                 # the conservative proxy sits in/under the margin: unknown
    return {'segment': seg, 'too_small': False, 'estimate': lo, 'estimate_max': hi}


def format_usd(usd: Optional[float]) -> str:
    if usd is None:
        return '?'
    v = float(usd)
    if v >= 1_000_000_000:
        return f'${v / 1_000_000_000:.1f}B'
    if v >= 1_000_000:
        return f'${v / 1_000_000:.0f}M' if v >= 10_000_000 else f'${v / 1_000_000:.1f}M'
    if v >= 1_000:
        return f'${v / 1_000:.0f}K'
    return f'${v:.0f}'


_SIZE_BUCKETS = ((50, '1-50'), (200, '51-200'), (500, '201-500'), (1000, '501-1000'),
                 (5000, '1001-5000'), (10000, '5001-10000'))


def size_bucket(employees: Optional[int]) -> Optional[str]:
    """Headcount → the closed set FIRMOGRAPHIC_PROMPT uses for `size`."""
    try:
        n = int(employees)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for cut, label in _SIZE_BUCKETS:
        if n <= cut:
            return label
    return '10000+'


# ── NTEE → ZoomInfo subindustry ─────────────────────────────────────────────
def zi_for_ntee(ntee: Optional[str], subseccd: Any = None) -> Optional[str]:
    """NTEE major group/decile → ZI subindustry (task spec 2026-09-08):
    A5x museums · A6x performing arts · other A cultural centers · B2x K-12
    (the label; enrichment's vertical gate fails it — A.J. 2026-09-04, ALL
    K-12 is not a fit) · B4x/B5x colleges · B7x libraries · X religious ·
    Y membership · W6x credit unions → Banking · D5x zoos. E2x (hospitals,
    health systems, FQHCs) → None: policy P1 (review 2026-09-08) — the NTEE
    decile alone cannot tell a hospital (SIC 80, out) from a community
    health center or a health-system foundation, so the registry says
    "unknown" and the article/search classifier decides instead of a
    registry-final OTHER tombstoning the account. 501(c)(6)/(7) with no
    NTEE → Membership. Default → charitable."""
    code = (ntee or '').strip().upper()
    if code:
        major, sub = code[0], code[1:3]
        if major == 'A':
            if sub.startswith('5'):
                return 'Museums & Art Galleries'
            if sub.startswith('6'):
                return 'Performing Arts Theaters'
            return 'Cultural & Informational Centers'
        if major == 'B':
            if sub.startswith('2'):
                return 'K-12 Schools'
            if sub[:1] in ('4', '5'):
                return 'Colleges & Universities'
            if sub.startswith('7'):
                return 'Libraries'
            return 'Non-Profit & Charitable Organizations'
        if major == 'X':
            return 'Religious Organizations'
        if major == 'Y':
            return 'Membership Organizations'
        if major == 'E' and sub.startswith('2'):
            return None
        if major == 'W' and sub.startswith('6'):
            return 'Banking'
        if major == 'D' and sub.startswith('5'):
            return 'Zoos & National Parks'
        if major == 'P' and sub == '33':
            return 'Childcare'
    try:
        if int(subseccd) in (6, 7):
            return 'Membership Organizations'
    except (TypeError, ValueError):
        pass
    return 'Non-Profit & Charitable Organizations'


# ═══════════════════════════════════════════════════════════════════════════
# DB plumbing (fail-soft readers)
# ═══════════════════════════════════════════════════════════════════════════

def ensure_schema(db_path: str = DEFAULT_DB_PATH) -> None:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _open_ro(db_path: str) -> Optional[sqlite3.Connection]:
    """Reader connection, or None when the DB file isn't there (readers never
    create it — that is the refresh script's job)."""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        log.debug('oracles: open %s failed: %s', db_path, e)
        return None


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> List[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:          # missing table, locked, corrupt …
        log.debug('oracles: query failed: %s', e)
        return []


def _local_candidates(conn: sqlite3.Connection, table: str, query_norm: str,
                      state: Optional[str], extra_like_col: Optional[str] = None,
                      limit: int = 400) -> List[sqlite3.Row]:
    """Candidate rows by the rarest query token (LIKE), narrowed by a second
    token when the first is too common. A name made only of generic tokens
    ('First National Bank', 'Wealth Management') yields NO candidates:
    review 2026-09-08 (H1 c) — the old state-wide fallback let 'Wealth
    Management' + MA match any Massachusetts adviser whose legal name ends
    in those words and 'Capital Partners' + NY pick one of a hundred."""
    toks = _distinctive_tokens(query_norm)
    like_cols = ['norm_name'] + ([extra_like_col] if extra_like_col else [])

    def _where(n_tokens: int) -> Tuple[str, tuple]:
        clauses, params = [], []
        for t in toks[:n_tokens]:
            ors = ' OR '.join(f'{c} LIKE ?' for c in like_cols)
            clauses.append(f'({ors})')
            params.extend([f'%{t}%'] * len(like_cols))
        return ' AND '.join(clauses), tuple(params)

    if toks:
        where, params = _where(1)
        rows = _rows(conn, f'SELECT * FROM {table} WHERE {where} LIMIT {limit}', params)
        if len(rows) >= limit and len(toks) > 1:
            where, params = _where(2)
            rows = _rows(conn, f'SELECT * FROM {table} WHERE {where} LIMIT {limit}', params)
        return rows
    log.debug('oracles: %r has no distinctive token — no registry lookup', query_norm)
    return []


def _clean_state(value: Any) -> Optional[str]:
    if not value:
        return None
    s = str(value).strip().upper()
    if len(s) == 2 and s in ALL_STATE_CODES:
        return s
    return hq_state_code(str(value))


def _title_city(city: Optional[str]) -> Optional[str]:
    c = (city or '').strip()
    if not c:
        return None
    return c.title() if (c.isupper() or c.islower()) else c


_DOMESTIC_COUNTRIES = {'', 'us', 'usa', 'united states', 'united states of america',
                       'canada', 'ca', 'can'}


def _hq(city: Optional[str], state: Optional[str], country: Optional[str] = None) -> Optional[str]:
    """'WESTERLY' + 'RI' → 'Westerly, RI'. A foreign registrant (an SEC
    adviser in London files a MainAddr with no US state) keeps its country
    in the string — 'London, United Kingdom' — so gates.hq_territory_status
    reads a confident 'out' instead of 'unknown' (which would cost a search)."""
    city, state = _title_city(city), (state or '').strip().upper() or None
    ctry = (country or '').strip()
    if ctry.lower() in _DOMESTIC_COUNTRIES or (state and state in ALL_STATE_CODES):
        ctry = ''
    parts = [p for p in (city, state, ctry) if p]
    return ', '.join(parts) or None


_SOCIAL_HOSTS = ('linkedin.com', 'facebook.com', 'twitter.com', 'x.com', 'instagram.com',
                 'youtube.com')


def normalize_url(raw: Optional[str]) -> Optional[str]:
    """'HTTP://WWW.EXAMPLE.COM' / 'www.example.com' → 'https://www.example.com'.
    Social profile links are not a company website."""
    s = (raw or '').strip()
    if not s or ' ' in s:
        return None
    m = re.match(r'^(?:(https?)://)?([^/?#]+)(.*)$', s, re.I)
    if not m:
        return None
    scheme, host, rest = (m.group(1) or 'https').lower(), m.group(2).lower(), m.group(3) or ''
    if '.' not in host or any(host == h or host.endswith('.' + h) for h in _SOCIAL_HOSTS):
        return None
    if rest == '/':
        rest = ''
    return f'{scheme}://{host}{rest}'


# ═══════════════════════════════════════════════════════════════════════════
# Live HTTP (single seam — tests replace requests.get)
# ═══════════════════════════════════════════════════════════════════════════

_LIVE = {'last': 0.0, 'ua': None}


def user_agent() -> str:
    """The SEC User-Agent string from config.yaml (sec_filings.user_agent) —
    the SEC asks that automated clients identify themselves — reused for
    every oracle call. Falls back to the same value the example config ships."""
    if _LIVE['ua']:
        return _LIVE['ua']
    ua = None
    for fname in ('config.yaml', 'config.example.yaml'):
        try:
            import yaml
            with open(os.path.join(_REPO, fname)) as fh:
                cfg = yaml.safe_load(fh) or {}
            ua = ((cfg.get('sec_filings') or {}).get('user_agent') or '').strip() or None
        except Exception:
            ua = None
        if ua:
            break
    _LIVE['ua'] = ua or _FALLBACK_USER_AGENT
    return _LIVE['ua']


def _http_get(url: str, params: Optional[dict] = None, timeout: int = HTTP_TIMEOUT,
              stream: bool = False):
    """requests.get with our User-Agent and the live-call throttle. Returns
    the Response, or None on a transport failure (never raises)."""
    wait = LIVE_MIN_INTERVAL - (time.monotonic() - _LIVE['last'])
    if wait > 0:
        time.sleep(wait)
    _LIVE['last'] = time.monotonic()
    try:
        return requests.get(url, params=params, timeout=timeout, stream=stream,
                            headers={'User-Agent': user_agent(), 'Accept': '*/*'})
    except Exception as e:      # ConnectionError, Timeout, SSL — all "no answer"
        log.debug('oracles: GET %s failed: %s', url, e)
        return None


def _json_body(resp) -> Optional[Any]:
    try:
        return resp.json()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Adapters
# ═══════════════════════════════════════════════════════════════════════════

def _candidate(name: str, city: Optional[str], state: Optional[str], source_id: str,
               score: float, row: Any = None, raw: Optional[float] = None,
               channel: Optional[str] = None) -> dict:
    return {'name': name, 'city': _title_city(city), 'state': (state or '').upper() or None,
            'source_id': str(source_id), 'score': round(score, 3),
            'raw': round(score if raw is None else raw, 3), 'channel': channel, 'row': row}


def _top3(cands: List[dict]) -> List[dict]:
    out = []
    for c in sorted(cands, key=lambda c: -(c.get('score') or 0.0))[:3]:
        out.append({k: c.get(k) for k in ('name', 'city', 'state', 'source_id', 'score')})
    return out


def _result(*, kind: str, source: str, source_id: str, matched_name: str, city, state,
            revenue_usd: Optional[float], revenue_source: Optional[str], zi: Optional[str],
            url: Optional[str], as_of: Optional[str], confidence: float, cands: List[dict],
            industry: Optional[str] = None, size: Optional[str] = None,
            extra: Optional[dict] = None, country: Optional[str] = None,
            estimated: bool = False, band: Optional[Tuple[Optional[str], bool]] = None,
            raw: Optional[float] = None) -> dict:
    """`estimated` = the revenue is a proxy (banks, advisers): the segment and
    too_small come from estimate_band's ±30% margin bands (P3) — inside a
    band `revenue` is None (unknown) while revenue_amount_usd / revenue_source
    still document the estimate. A Form 990 figure is a fact and keeps the
    sharp edges. `band` overrides both (the adviser MIN/MAX rule, M1).
    `raw_score` is the name similarity BEFORE the geography bonus."""
    st = (state or '').strip().upper() or None
    if band is not None:
        segment, small = band
    elif estimated:
        segment, small = estimate_band(revenue_usd)
    else:
        segment, small = revenue_segment(revenue_usd), too_small(revenue_usd)
    out = {
        'kind': kind,
        'hq': _hq(city, st, country),
        'hq_state': st,
        'in_territory': (st in TERRITORY_STATES) if st else None,
        'revenue': segment,
        'revenue_amount_usd': int(revenue_usd) if revenue_usd else None,
        'revenue_source': revenue_source if revenue_usd else None,
        'too_small': bool(small),
        'zi_subindustry': zi,
        'industry': industry,
        'size': size,
        'url': url,
        'source': source,
        'source_id': str(source_id),
        'matched_name': matched_name,
        'as_of': as_of,
        'confidence': round(float(confidence), 3),
        'raw_score': round(float(confidence if raw is None else raw), 3),
        'candidates': _top3(cands),
    }
    if extra:
        out.update(extra)
    return out


# ── RIA (local ria_firm table) ──────────────────────────────────────────────

def _ria_lookup(name: str, state: Optional[str], city: Optional[str], db_path: str,
                strict: bool = False) -> Optional[dict]:
    """`strict` (L4, review 2026-09-08): the name is only weakly adviser-
    shaped ('X Partners', 'Y Capital') — a state hint is required and only
    an exact / containment match IN that state may count; difflib and token
    overlap are refused, so a law firm never inherits an adviser's RAUM."""
    if strict and not state:
        return None
    conn = _open_ro(db_path)
    if conn is None:
        return None
    try:
        q = normalize_name(name)
        if not q:
            return None
        rows = _local_candidates(conn, 'ria_firm', q, state, extra_like_col='lower(legal_name)')
        cands = []
        for r in rows:
            d = max(score_detail(q, r['norm_name'] or '', state, r['state'], city, r['city']),
                    score_detail(q, normalize_name(r['legal_name']), state, r['state'], city, r['city']),
                    key=lambda x: (x['score'], x['raw']))
            if strict and (d['channel'] not in ('exact', 'contain')
                           or (r['state'] or '').strip().upper() != state):
                continue
            cands.append(_candidate(r['business_name'] or r['legal_name'], r['city'], r['state'],
                                    r['crd'], d['score'], row=r, raw=d['raw'], channel=d['channel']))
        # Display order inside a score: Registered before ERA, then larger
        # RAUM. pick_best refuses real ties between different CRDs (H1 a) —
        # this ordering never decides a hit.
        cands.sort(key=lambda c: (-(c['score']), -(c['raw']),
                                  0 if c['row']['firm_type'] == 'Registered' else 1,
                                  -(c['row']['raum_usd'] or 0)))
        best = pick_best(cands)
        if best is None:
            return None
        r = best['row']
        band = ria_revenue_band(r['raum_usd'], r['total_employees'])
        rev = band['estimate']
        parts = []
        if r['raum_usd']:
            parts.append(f'RAUM {format_usd(r["raum_usd"])} × {RIA_RAUM_TO_REV:.1%}')
        if r['total_employees']:
            parts.append(f'{r["total_employees"]} employees × {format_usd(RIA_REV_PER_EMP)}')
        rev_src = ('SEC IAPD Form ADV estimate — min(' + ', '.join(parts) + ')') if parts else None
        is_era = (r['firm_type'] or '').upper() == 'ERA'
        zi = 'Venture Capital & Private Equity' if is_era else 'Lending & Brokerage'
        industry = ('Exempt reporting adviser (private fund manager, SEC Form ADV)' if is_era
                    else 'Registered investment adviser (SEC Form ADV)')
        return _result(kind='ria', source='sec_iapd', source_id=r['crd'], matched_name=best['name'],
                       city=r['city'], state=r['state'], revenue_usd=rev, revenue_source=rev_src,
                       zi=zi, url=normalize_url(r['website']), as_of=r['as_of'],
                       confidence=best['score'], cands=cands, industry=industry,
                       size=size_bucket(r['total_employees']), country=r['country'],
                       band=(band['segment'], band['too_small']), raw=best['raw'],
                       extra={'firm_type': r['firm_type'], 'reg_status': r['reg_status'],
                              'reg_date': r['reg_date'], 'raum_usd': r['raum_usd'],
                              'total_employees': r['total_employees'],
                              'sec_number': r['sec_number'],
                              'revenue_estimate_max_usd': band['estimate_max']})
    finally:
        conn.close()


# ── Bank (local bank table, then live FDIC wildcard search) ─────────────────

_BKCLASS_LABEL = {'N': 'national bank', 'NM': 'state bank (Fed non-member)', 'SM': 'state bank (Fed member)',
                  'SB': 'savings bank', 'SA': 'savings association', 'OI': 'insured US branch of a foreign bank'}


def _bank_result(cand: dict, cands: List[dict], as_of: Optional[str], from_live: bool) -> dict:
    r = cand['row']
    rev = estimate_bank_revenue(r['asset_kusd'])
    rev_src = (f'FDIC BankFind estimate — total assets {format_usd((r["asset_kusd"] or 0) * 1000)} '
               f'× {BANK_ASSET_TO_REV:.1%}') if rev else None
    label = _BKCLASS_LABEL.get((r['bkclass'] or '').upper(), 'bank')
    return _result(kind='bank', source='fdic', source_id=r['cert'], matched_name=r['name'],
                   city=r['city'], state=r['state'], revenue_usd=rev, revenue_source=rev_src,
                   zi='Banking', url=normalize_url(r['website']), as_of=as_of,
                   confidence=cand['score'], cands=cands, estimated=True, raw=cand.get('raw'),
                   industry=f'FDIC-insured {label}',
                   extra={'asset_kusd': r['asset_kusd'], 'holding_co': r['holding_co'],
                          'bkclass': r['bkclass'], 'est_date': r['est_date'],
                          'live': from_live})


def _score_bank_rows(q: str, rows: Iterable[Any], state, city) -> List[dict]:
    cands = []
    for r in rows:
        d = score_detail(q, r['norm_name'] or '', state, r['state'], city, r['city'])
        if r['holding_co']:
            # The press names the holding company as often as the bank
            # ("Washington Trust Bancorp"): score it too, take the better.
            d = max(d, score_detail(q, normalize_name(r['holding_co']), state, r['state'], city, r['city']),
                    key=lambda x: (x['score'], x['raw']))
        cands.append(_candidate(r['name'], r['city'], r['state'], r['cert'], d['score'], row=r,
                                raw=d['raw'], channel=d['channel']))
    # Display order only — pick_best refuses ties between different certs
    # (H1 a); asset size never decides a hit any more.
    cands.sort(key=lambda c: (-(c['score']), -(c['raw']), -(c['row']['asset_kusd'] or 0)))
    return cands


def _bank_lookup_local(name: str, state, city, db_path: str) -> Optional[dict]:
    conn = _open_ro(db_path)
    if conn is None:
        return None
    try:
        q = normalize_name(name)
        if not q:
            return None
        rows = _local_candidates(conn, 'bank', q, state, extra_like_col='lower(holding_co)')
        cands = _score_bank_rows(q, rows, state, city)
        best = pick_best(cands)
        return _bank_result(best, cands, best['row']['as_of'], False) if best else None
    finally:
        conn.close()


# Holding-company vocabulary: the press says "Washington Trust Bancorp", the
# FDIC NAME is "The Washington Trust Company, of Westerly" — a *BANCORP*
# wildcard returns nothing and would negative-cache a real bank.
_HOLDCO_TOKENS = {'bancorp', 'bancorporation', 'bancshares', 'bankshares', 'banc',
                  'holdings', 'holding', 'financial', 'group', 'corp', 'corporation'}


def fdic_filter_tokens(name: str, limit: int = 3) -> List[str]:
    """Tokens for a BankFind NAME:*TOKEN* AND … filter (exact-token search,
    research 2026-09-08: quoted phrases return 0). Distinctive tokens first,
    then generic ones ('First National Bank' has nothing else), holding-
    company words never — at most `limit` so a long legal name still hits."""
    toks = [t for t in normalize_name(name).split()
            if t.isalnum() and t not in _HOLDCO_TOKENS]
    distinctive = [t for t in toks if t not in _GENERIC_TOKENS]
    generic = [t for t in toks if t in _GENERIC_TOKENS]
    return (distinctive + generic)[:limit]


def fdic_row_from_api(d: dict, as_of: Optional[str]) -> dict:
    """One BankFind `data[].data` record → a `bank` table row dict."""
    def _int(v):
        try:
            return int(v) if v not in (None, '') else None
        except (TypeError, ValueError):
            return None
    return {
        'cert': _int(d.get('CERT')), 'name': (d.get('NAME') or '').strip(),
        'norm_name': normalize_name(d.get('NAME')), 'city': (d.get('CITY') or '').strip() or None,
        'state': (d.get('STALP') or '').strip().upper() or None, 'asset_kusd': _int(d.get('ASSET')),
        'website': (d.get('WEBADDR') or '').strip() or None, 'est_date': d.get('ESTYMD'),
        'holding_co': (d.get('NAMEHCR') or '').strip() or None, 'bkclass': d.get('BKCLASS'),
        'active': _int(d.get('ACTIVE')), 'as_of': as_of,
    }


def _bank_lookup_live(name: str, state: Optional[str], city, cache, now) -> Optional[dict]:
    """FDIC wildcard search — only when the name is bank-shaped AND the state
    is known (NAME:*TOKEN* per token AND STALP:XX). Zero hits are negative-
    cached; a transport failure is not."""
    if not state:
        return None
    if not _distinctive_tokens(normalize_name(name)):
        return None              # 'First National Bank' + PA names no one (H1 c)
    key = _cache_key(name, state)
    if cache is not None:
        hit = cache.get_search(key, CACHE_KIND_FDIC, now=now)
        if hit:
            row = (hit.get('results') or [{}])[0]
            cands = _score_bank_rows(normalize_name(name), [row], state, city)
            best = pick_best(cands)
            return _bank_result(best, cands, row.get('as_of'), True) if best else None
        if cache.should_skip(key, CACHE_KIND_FDIC, now=now):
            return None
    toks = fdic_filter_tokens(name)
    if not toks:
        return None
    filters = ' AND '.join([f'NAME:*{t.upper()}*' for t in toks] + [f'STALP:{state}', 'ACTIVE:1'])
    resp = _http_get(FDIC_INSTITUTIONS_URL, params={'filters': filters, 'fields': FDIC_FIELDS,
                                                    'limit': 50, 'format': 'json'})
    if resp is None or resp.status_code != 200:
        return None                                   # API trouble: not cached
    body = _json_body(resp) or {}
    as_of = (((body.get('meta') or {}).get('index') or {}).get('createTimestamp') or '')[:10] or None
    rows = [fdic_row_from_api(item.get('data') or {}, as_of) for item in (body.get('data') or [])]
    rows = [r for r in rows if r['cert'] is not None]
    cands = _score_bank_rows(normalize_name(name), rows, state, city)
    best = pick_best(cands)
    if best is None:
        if cache is not None:
            cache.record_empty(key, CACHE_KIND_FDIC, now=now)
        return None
    if cache is not None:
        cache.set_search(key, CACHE_KIND_FDIC, {'results': [dict(best['row'])]}, now=now)
        cache.clear_negative(key, CACHE_KIND_FDIC)
    return _bank_result(best, cands, as_of, True)


def _bank_lookup(name, state, city, db_path, cache, live, now) -> Optional[dict]:
    hit = _bank_lookup_local(name, state, city, db_path)
    if hit:
        return hit
    if live and looks_like_bank(name):
        return _bank_lookup_live(name, state, city, cache, now)
    return None


# ── Nonprofit (live ProPublica) ─────────────────────────────────────────────

def _npo_from_org(org: dict, filings: Optional[list], cands: List[dict], best: dict) -> dict:
    rev, rev_src, yr, latest = None, None, None, None
    f0 = (filings or [None])[0]
    if f0 and f0.get('totrevenue') not in (None, ''):
        try:
            rev = float(f0.get('totrevenue'))
            yr = f0.get('tax_prd_yr') or str(f0.get('tax_prd') or '')[:4]
            rev_src = f'ProPublica Nonprofit Explorer — Form 990 FY{yr} total revenue'
            latest = {'year': yr, 'total_revenue': int(rev),
                      'total_expenses': f0.get('totfuncexpns')}
        except (TypeError, ValueError):
            rev = None
    if rev is None and org.get('revenue_amount') not in (None, ''):
        try:
            rev = float(org.get('revenue_amount'))
            rev_src = 'ProPublica Nonprofit Explorer — IRS BMF revenue amount'
        except (TypeError, ValueError):
            rev = None
    ntee = org.get('ntee_code') or best.get('ntee_code')
    subsec = org.get('subseccd') if org.get('subseccd') is not None else best.get('subseccd')
    zi = zi_for_ntee(ntee, subsec)
    industry = f'Nonprofit (NTEE {ntee})' if ntee else 'Nonprofit organization'
    if subsec not in (None, ''):
        industry += f', 501(c)({subsec})'
    ein = org.get('ein') or best.get('source_id')
    return _result(kind='npo', source='propublica', source_id=ein,
                   matched_name=org.get('name') or best['name'], city=org.get('city') or best['city'],
                   state=org.get('state') or best['state'], revenue_usd=rev, revenue_source=rev_src,
                   zi=zi, url=None, as_of=str(yr) if yr else None, confidence=best['score'],
                   cands=cands, industry=industry, raw=best.get('raw'),
                   extra={'ein': ein, 'ntee_code': ntee, 'subseccd': subsec,
                          'filings': len(filings or []), 'latest_990': latest,
                          'profile_url': f'https://projects.propublica.org/nonprofits/organizations/{ein}'})


def npo_lookup(name: str, state: Optional[str] = None, city: Optional[str] = None, *,
               cache=None, live: bool = True, now: Optional[datetime] = None) -> Tuple[str, Optional[dict]]:
    """ProPublica adapter → (status, hit). status ∈ 'hit' | 'empty' (the API
    answered and nothing matched — cacheable) | 'error' (no usable answer —
    never cached) | 'skipped' (negative-cached / live disabled).

    Two defects this replaces (research 2026-09-08): ProPublica returns HTTP
    404 WITH a valid JSON body for zero hits (raise_for_status made that an
    'API failure' and the org was re-queried on every event), and the top
    hit was taken blind — name-only 'Museum of Fine Arts Boston' matched a
    Virginia museum. Now: state[id] filter when the state is known, and the
    same similarity gate as the registries.

    Cache keying (M8, review 2026-09-08): ONE entry per normalized name —
    the state is a FILTER on a cached hit, not part of the key. Stage A, the
    second chance and the 990 probe ask about the same org under up to
    three state anchors (none / dateline / remembered), and keying on the
    state cost up to six live calls per nonprofit miss. A cached hit in a
    different state than the one asked for is not served (it is a different
    question) and does not negative-cache the name either."""
    key = _npo_cache_key(name)
    st = _clean_state(state)
    other_state_hit = False
    if cache is not None:
        cached = cache.get_search(key, CACHE_KIND_PROPUBLICA, now=now)
        if cached:
            row = (cached.get('results') or [{}])[0]
            hit = row.get('hit')
            if hit:
                if not st or not hit.get('hq_state') or hit.get('hq_state') == st:
                    return 'hit', hit
                other_state_hit = True
        if cache.should_skip(key, CACHE_KIND_PROPUBLICA, now=now):
            return 'skipped', None
    if not live:
        return 'skipped', None
    if not _distinctive_tokens(normalize_name(name)):
        log.debug('oracles: %r has no distinctive token — ProPublica not asked', name)
        return 'skipped', None
    # Two query forms when the aliases change the TOKENS of the name (live
    # 2026-09-08: 'YMCA of Greater Boston' returns only the realty
    # subsidiaries; the IRS name 'Young Mens Christian Association of
    # Greater Boston' is what the token search knows). Candidates merge by
    # EIN. M8: '&'→'and', a possessive or a dropped period ("Boys & Girls
    # Club", "St. Mary's") is NOT a new form — the token search is
    # punctuation-blind and the second call was a wasted duplicate.
    forms = [name.strip()]
    expanded = expand_aliases(name)
    if expanded and _alias_free_tokens(expanded) != _alias_free_tokens(name):
        forms.append(expanded)
    orgs: Dict[Any, dict] = {}
    for form in forms:
        params = {'q': form}
        if st and st in ALL_STATE_CODES and len(st) == 2:
            params['state[id]'] = st
        resp = _http_get(PROPUBLICA_SEARCH_URL, params=params)
        if resp is None or resp.status_code not in (200, 404):
            return 'error', None
        body = _json_body(resp)
        if not isinstance(body, dict):
            return 'error', None                 # a 404 without JSON is a real failure
        for org in (body.get('organizations') or [])[:25]:
            orgs.setdefault(org.get('ein'), org)
    q = normalize_name(name)
    cands = []
    for o in list(orgs.values())[:50]:
        s = score_names(q, normalize_name(o.get('name')), st, o.get('state'), city, o.get('city'))
        c = _candidate(o.get('name') or '', o.get('city'), o.get('state'), o.get('ein'), s, row=o)
        c['ntee_code'], c['subseccd'] = o.get('ntee_code'), o.get('subseccd')
        cands.append(c)
    best = pick_best(cands, threshold=MATCH_THRESHOLD if st else NPO_NO_STATE_THRESHOLD)
    if best is None:
        if cache is not None and not other_state_hit:
            cache.record_empty(key, CACHE_KIND_PROPUBLICA, now=now)
        return 'empty', None
    org, filings = dict(best['row']), []
    detail = _http_get(PROPUBLICA_ORG_URL.format(ein=best['source_id']))
    if detail is not None and detail.status_code == 200:
        d = _json_body(detail) or {}
        if isinstance(d, dict):
            org.update({k: v for k, v in (d.get('organization') or {}).items() if v is not None})
            filings = d.get('filings_with_data') or []
    hit = _npo_from_org(org, filings, cands, best)
    if cache is not None:
        cache.set_search(key, CACHE_KIND_PROPUBLICA, {'results': [{'hit': hit}]}, now=now)
        cache.clear_negative(key, CACHE_KIND_PROPUBLICA)
    return 'hit', hit


def _cache_key(name: str, state: Optional[str]) -> str:
    """AccountCache key for the FDIC wildcard: the shared account_key plus
    the state hint, so the RI and WA 'Washington Trust' never share a cached
    answer (the state is part of that query)."""
    k = account_key(name) or (name or '').strip().lower()
    st = _clean_state(state)
    return f'{k}|{st}' if st else k


def _npo_cache_key(name: str) -> str:
    """ProPublica cache key: the name alone (M8) — the state is a filter."""
    return account_key(name) or (name or '').strip().lower()


def _alias_free_tokens(s: Optional[str]) -> str:
    """normalize_name WITHOUT the alias table — what a token search sees.
    normalize_name(expand_aliases(x)) == normalize_name(x) by construction
    (it applies the same aliases), so the second-form test compares the
    alias-free token strings instead."""
    t = (s or '').strip().lower().replace('&', ' and ')
    t = _POSSESSIVE_RE.sub('s', t).replace("'", '').replace('’', '')
    t = _NON_ALNUM_RE.sub(' ', t)
    return ' '.join(w for w in _WS_RE.split(t) if w and w not in _STOP_TOKENS)


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════

def lookup(name: str, hint: Optional[dict] = None, *, db_path: Optional[str] = None,
           cache=None, live: bool = True, now: Optional[datetime] = None) -> Optional[dict]:
    """Registry answer for one company name, or None. `hint` keys: kind
    ('auto' | 'ria' | 'bank' | 'npo'), state (2-letter), city, zi_guess.
    'auto' runs the adapters whose name-shape test passes, cheapest first
    (RIA and bank are local reads; the FDIC wildcard and ProPublica are live
    calls, cached in `cache` — an AccountCache — for 90 days). Never raises."""
    try:
        h = dict(hint or {})
        kind = (h.get('kind') or 'auto').strip().lower()
        state = _clean_state(h.get('state'))
        city = (h.get('city') or '').strip() or None
        nm = (name or '').strip()
        if not nm or kind not in ('auto', 'ria', 'bank', 'npo'):
            return None
        path = db_path or DEFAULT_DB_PATH
        if kind == 'auto' and h.get('zi_guess'):
            guessed = kind_for_zi(h.get('zi_guess'), nm)
            if guessed:
                kind = guessed
        plan: List[str] = []
        shape = ria_shape(nm)
        strict_ria = kind == 'auto' and shape == 'weak'
        if kind == 'ria' or (kind == 'auto' and shape):
            plan.append('ria')
        if kind == 'bank' or (kind == 'auto' and looks_like_bank(nm)):
            plan.append('bank')
        if kind == 'npo' or (kind == 'auto' and looks_like_npo(nm)) or \
                (kind == 'bank' and _CREDIT_UNION_RE.search(nm)):
            plan.append('npo')
        for adapter in plan:
            try:
                if adapter == 'ria':
                    hit = _ria_lookup(nm, state, city, path, strict=strict_ria)
                elif adapter == 'bank':
                    hit = _bank_lookup(nm, state, city, path, cache, live, now)
                else:
                    hit = npo_lookup(nm, state, city, cache=cache, live=live, now=now)[1]
            except Exception as e:          # an adapter bug must never cost an event
                log.debug('oracles: %s adapter failed for %r: %s', adapter, nm, e)
                hit = None
            if hit:
                log.debug('oracles: %s hit for %r → %s (%.2f)', hit['source'], nm,
                          hit['matched_name'], hit['confidence'])
                return hit
        return None
    except Exception as e:
        log.debug('oracles: lookup failed for %r: %s', name, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Refresh (used by scripts/refresh_oracles.py; pure functions + one writer)
# ═══════════════════════════════════════════════════════════════════════════

def iapd_feed_url(day: date) -> str:
    """The feed is published on the 1st; the URL is dated that day."""
    return SEC_IAPD_FEED_URL.format(mm=f'{day.month:02d}', dd='01', yyyy=day.year)


def iapd_feed_candidates(today: Optional[date] = None) -> List[Tuple[str, date]]:
    """(url, month) for the current month, then the previous one — the
    fallback when the new compilation isn't up yet on refresh day."""
    d = today or date.today()
    this = date(d.year, d.month, 1)
    prev = (this - timedelta(days=1)).replace(day=1)
    return [(iapd_feed_url(this), this), (iapd_feed_url(prev), prev)]


def _local_tag(el) -> str:
    return el.tag.rsplit('}', 1)[-1] if isinstance(el.tag, str) else ''


def parse_iapd(fileobj, as_of: Optional[str] = None) -> Iterator[dict]:
    """Stream Firm elements out of the (decompressed) IAPD XML — iterparse
    with elem.clear() so the 82 MB document never sits in memory. Yields
    one `ria_firm` row dict per firm with a CRD. XML shape verified live
    2026-09-08: Firm/Info@{FirmCrdNb,SECNb,BusNm,LegalNm}, Firm/MainAddr@
    {City,State,Cntry}, Firm/Rgstn@{FirmType,St,Dt}, WebAddr nested under
    FormInfo/Part1A/Item1/WebAddrs, Item5A@TtlEmp, Item5F@Q5F2C (RAUM)."""
    gen_on = as_of
    for event, el in ET.iterparse(fileobj, events=('start', 'end')):
        tag = _local_tag(el)
        if event == 'start':
            if tag == 'IAPDFirmSECReport' and not gen_on:
                gen_on = el.get('GenOn')
            continue
        if tag != 'Firm':
            continue
        try:
            row = _iapd_row(el, gen_on)
        except Exception as e:      # one malformed firm must not sink the refresh
            log.debug('oracles: skipping malformed Firm: %s', e)
            row = None
        el.clear()
        if row:
            yield row


def _iapd_row(firm, as_of: Optional[str]) -> Optional[dict]:
    info = addr = rgstn = None
    ttl_emp = raum = None
    web = None
    for el in firm.iter():
        t = _local_tag(el)
        if t == 'Info' and info is None:
            info = el
        elif t == 'MainAddr' and addr is None:
            addr = el
        elif t == 'Rgstn':
            # Prefer a live registration; one Rgstn per firm in practice.
            if rgstn is None or (el.get('St') or '').upper() in ('APPROVED', 'ACTIVE', 'APPROVED-120'):
                rgstn = el
        elif t == 'Item5A':
            ttl_emp = el.get('TtlEmp')
        elif t == 'Item5F':
            raum = el.get('Q5F2C')
        elif t == 'WebAddr' and web is None:
            web = normalize_url(el.text)
    if info is None:
        return None
    crd = info.get('FirmCrdNb')
    try:
        crd = int(crd)
    except (TypeError, ValueError):
        return None

    def _int(v):
        try:
            return int(float(v)) if v not in (None, '') else None
        except (TypeError, ValueError):
            return None
    bus = (info.get('BusNm') or '').strip()
    legal = (info.get('LegalNm') or '').strip()
    return {
        'crd': crd, 'business_name': bus or legal, 'legal_name': legal or bus,
        'norm_name': normalize_name(bus or legal),
        'city': (addr.get('City') or '').strip() or None if addr is not None else None,
        'state': (addr.get('State') or '').strip().upper() or None if addr is not None else None,
        'country': (addr.get('Cntry') or '').strip() or None if addr is not None else None,
        'firm_type': rgstn.get('FirmType') if rgstn is not None else None,
        'reg_status': rgstn.get('St') if rgstn is not None else None,
        'reg_date': rgstn.get('Dt') if rgstn is not None else None,
        'website': web, 'total_employees': _int(ttl_emp), 'raum_usd': _int(raum),
        'sec_number': info.get('SECNb'), 'as_of': as_of,
    }


def parse_fdic(body: dict) -> Tuple[List[dict], Optional[str]]:
    """BankFind JSON → (rows, as_of). as_of = the index build date."""
    as_of = (((body.get('meta') or {}).get('index') or {}).get('createTimestamp') or '')[:10] or None
    rows = [fdic_row_from_api(item.get('data') or {}, as_of) for item in (body.get('data') or [])]
    return [r for r in rows if r['cert'] is not None and r['name']], as_of


def _create_table_sql(table: str) -> str:
    stmt = next(s for s in SCHEMA if s.lstrip().startswith(f'CREATE TABLE IF NOT EXISTS {table} '))
    return stmt


def _replace_table(db_path: str, table: str, columns: Tuple[str, ...], rows: Iterable[dict],
                   meta: dict) -> int:
    """Replace-all WITHOUT holding the write lock for the whole parse (L1,
    review 2026-09-08). The old one-transaction DELETE + INSERT held SQLite's
    exclusive lock for the entire 82 MB stream: every enrichment reader hit
    its 5 s timeout, read a miss, spent a search — and domains negative-
    cached the account. Now the rows go into `<table>__incoming`, committed
    1,000 at a time (each commit locks for milliseconds; readers keep
    serving the OLD table between them), and the swap — DROP old, RENAME
    new, recreate the indexes, stamp oracle_meta — is ONE short transaction.
    Any failure drops the incoming table; the previous table never changed."""
    ensure_schema(db_path)
    tmp = f'{table}__incoming'
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(f'DROP TABLE IF EXISTS {tmp}')
        conn.execute(_create_table_sql(table).replace(f'IF NOT EXISTS {table} ', f'{tmp} ', 1))
        conn.commit()
        placeholders = ', '.join('?' for _ in columns)
        sql = f'INSERT OR REPLACE INTO {tmp} ({", ".join(columns)}) VALUES ({placeholders})'
        n, batch = 0, []
        for row in rows:
            batch.append(tuple(row.get(c) for c in columns))
            if len(batch) >= 1000:
                conn.executemany(sql, batch)
                conn.commit()
                n += len(batch)
                batch = []
        if batch:
            conn.executemany(sql, batch)
            conn.commit()
            n += len(batch)
        if n == 0:
            raise RuntimeError(f'{table}: refresh produced 0 rows — keeping the previous table')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(f'DROP TABLE IF EXISTS {table}')
        conn.execute(f'ALTER TABLE {tmp} RENAME TO {table}')
        for stmt in SCHEMA:
            if f' ON {table}(' in stmt:
                conn.execute(stmt)
        conn.execute('INSERT OR REPLACE INTO oracle_meta (source, refreshed_at, rows, src_url) '
                     'VALUES (?, ?, ?, ?)',
                     (meta.get('source'), meta.get('refreshed_at'), n, meta.get('src_url')))
        conn.commit()
        return n
    except Exception:
        try:
            conn.rollback()
            conn.execute(f'DROP TABLE IF EXISTS {tmp}')
            conn.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _table_counts(db_path: str, table: str) -> dict:
    conn = _open_ro(db_path)
    if conn is None:
        return {'total': 0, 'in_territory': 0, 'with_website': 0}
    try:
        placeholders = ', '.join('?' for _ in TERRITORY_STATES)
        terr = tuple(sorted(TERRITORY_STATES))
        total = _rows(conn, f'SELECT COUNT(*) AS n FROM {table}', ())
        in_t = _rows(conn, f'SELECT COUNT(*) AS n FROM {table} WHERE state IN ({placeholders})', terr)
        web = _rows(conn, f'SELECT COUNT(*) AS n FROM {table} WHERE website IS NOT NULL AND website != ""', ())
        return {'total': total[0]['n'] if total else 0, 'in_territory': in_t[0]['n'] if in_t else 0,
                'with_website': web[0]['n'] if web else 0}
    finally:
        conn.close()


def refresh_ria(db_path: str = DEFAULT_DB_PATH, source=None, src_url: str = '',
                as_of: Optional[str] = None, dry_run: bool = False,
                now: Optional[datetime] = None) -> dict:
    """Stream an IAPD .xml.gz (`source`: path or open binary file) into
    ria_firm. dry_run parses and counts without writing."""
    opened = None
    try:
        if isinstance(source, (str, bytes, os.PathLike)):
            opened = gzip.open(source, 'rb')
            fileobj = opened
        else:
            fileobj = source
        rows_iter = parse_iapd(fileobj, as_of)
        if dry_run:
            n = terr = web = 0
            for r in rows_iter:
                n += 1
                terr += 1 if r['state'] in TERRITORY_STATES else 0
                web += 1 if r['website'] else 0
            return {'source': 'ria', 'total': n, 'in_territory': terr, 'with_website': web,
                    'written': False}
        stamp = (now or datetime.utcnow()).isoformat(timespec='seconds')
        n = _replace_table(db_path, 'ria_firm', _RIA_COLUMNS, rows_iter,
                           {'source': 'ria', 'refreshed_at': stamp, 'src_url': src_url})
        counts = _table_counts(db_path, 'ria_firm')
        counts.update({'source': 'ria', 'written': True, 'rows': n})
        return counts
    finally:
        if opened is not None:
            opened.close()


def refresh_bank(db_path: str = DEFAULT_DB_PATH, body: Optional[dict] = None,
                 dry_run: bool = False, now: Optional[datetime] = None) -> dict:
    """Pull every active FDIC institution (one call, limit=10000) into
    `bank`. `body` injects an already-fetched JSON document (tests)."""
    src_url = f'{FDIC_INSTITUTIONS_URL}?filters=ACTIVE:1&fields={FDIC_FIELDS}&limit=10000&format=json'
    if body is None:
        resp = _http_get(FDIC_INSTITUTIONS_URL, params={'filters': 'ACTIVE:1', 'fields': FDIC_FIELDS,
                                                        'limit': 10000, 'format': 'json'}, timeout=120)
        if resp is None or resp.status_code != 200:
            raise RuntimeError(f'FDIC institutions fetch failed: '
                               f'{"no response" if resp is None else resp.status_code}')
        body = _json_body(resp)
        if not isinstance(body, dict):
            raise RuntimeError('FDIC institutions fetch returned non-JSON')
    rows, as_of = parse_fdic(body)
    if dry_run:
        return {'source': 'bank', 'total': len(rows),
                'in_territory': sum(1 for r in rows if r['state'] in TERRITORY_STATES),
                'with_website': sum(1 for r in rows if r['website']), 'written': False,
                'as_of': as_of}
    stamp = (now or datetime.utcnow()).isoformat(timespec='seconds')
    n = _replace_table(db_path, 'bank', _BANK_COLUMNS, rows,
                       {'source': 'bank', 'refreshed_at': stamp, 'src_url': src_url})
    counts = _table_counts(db_path, 'bank')
    counts.update({'source': 'bank', 'written': True, 'rows': n, 'as_of': as_of})
    return counts


def download_iapd_feed(dest_dir: str, today: Optional[date] = None) -> Tuple[str, str, date]:
    """Fetch this month's compilation (previous month on a 404) to
    `dest_dir`, streaming to disk. Returns (local_path, url, month)."""
    os.makedirs(dest_dir, exist_ok=True)
    last_err = 'no candidate URL'
    for url, month in iapd_feed_candidates(today):
        resp = _http_get(url, timeout=300, stream=True)
        if resp is None:
            last_err = f'no response from {url}'
            continue
        if resp.status_code == 404:
            last_err = f'404 {url}'
            log.info('oracles: %s not published yet — trying the previous month', url)
            continue
        if resp.status_code != 200:
            last_err = f'{resp.status_code} {url}'
            continue
        path = os.path.join(dest_dir, os.path.basename(url))
        tmp = path + '.part'
        with open(tmp, 'wb') as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
        os.replace(tmp, path)
        return path, url, month
    raise RuntimeError(f'IAPD feed download failed: {last_err}')


def meta(db_path: str = DEFAULT_DB_PATH) -> Dict[str, dict]:
    """{source: {refreshed_at, rows, src_url}} — {} when the DB is absent."""
    conn = _open_ro(db_path)
    if conn is None:
        return {}
    try:
        return {r['source']: dict(r) for r in _rows(conn, 'SELECT * FROM oracle_meta', ())}
    finally:
        conn.close()
