"""Free domain resolution for TeamAlbert v2 — Phase 3, slice B4.

WHY (A.J. 2026-09-07/08, Phase 3 plan): an account without a website domain
can't be keyed reliably — dedup falls back to name strings, ZoomInfo and
Firecrawl spend fires at the wrong company, and the dashboard shows three
rows for one bank. Every paid enricher wants a domain FIRST. This module
resolves one from FREE signals, cheapest first, and refuses to answer
rather than guess:

    hint URL → account cache → local oracle tables → FDIC BankFind (banks,
    state required) → Clearbit autocomplete → SEC submissions (CIK known) →
    [guess-and-verify: OFF by default, never more than 'low']

Research 2026-09-08 (live):
  * Clearbit Autocomplete is keyless, ~150 ms, cacheable 30 days
    (Cache-Control) and has NO published limits — it could vanish, hence the
    kill-switch (DOMAINS_CLEARBIT_ENABLED) and the long cache. On 8
    queue-typical names it was 4/8 top-1 correct, 0 wrong top-1, and missed
    small RIAs / lenders / a YMCA; "Washington Trust" is ambiguous (RI vs WA
    banks) and needs the state. It was only precise when the returned name
    normalizes to the SAME account_key as the query, or when exactly one
    result is a token-superset of the query — those are the only two accept
    rules (tests/test_domains.py freezes the 8 outcomes).
  * FDIC BankFind is keyless (120/min). NAME filters are exact-token (a
    quoted phrase returns 0), so we send *TOKEN* wildcards per significant
    token; WEBADDR is inconsistently formatted (sometimes carries http://)
    and STALP is required to disambiguate (Washington Trust RI ≠ Washington
    Trust Bank WA).
  * SEC submissions JSON carries `website` / `investorWebsite` (usually
    empty) — only useful when the CIK is already on the record (the 8-K
    scraper stores one per event), so no ticker map is downloaded.
  * ProPublica has no website field; IAPD search has no domain, but the
    monthly feed does — that lives in the oracle tables another slice builds
    (state/oracles.db: bank / ria_firm), which we read if present. Registry
    legal names carry tails the press never uses ("The Washington Trust
    Company, of Westerly"), so oracle/FDIC matching is token-based, not
    string-equal.
  * Guess-and-verify (name → acme.com + HEAD) is unsafe and imprecise: parked
    pages echo the name, Cloudflare challenges pass HEAD, and it hits
    registrant-controlled hosts from the home IP. It ships OFF
    (DOMAINS_GUESS_ENABLED), is never more than 'low', and is never written
    to the firmographic `domain` field.

Rules: no search engines, no Tavily/Firecrawl, no new deps (stdlib
urllib.parse + a small hand-kept public-suffix list — tldextract is not
installed). Every network call: a per-endpoint requests.Session, timeout
(5, 10), one retry on 5xx only, fail-soft — a transport error yields method
'error' and is NOT negative-cached, so one flaky minute never blocks an
account for a week. Nothing here ever raises into the caller.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from src.pipeline.gates import ALL_STATE_CODES, account_key, hq_state_code

try:
    # L2 (review 2026-09-08): the oracles module owns the FDIC endpoint —
    # banks.data.fdic.gov 301s to api.fdic.gov and the redirect hop is
    # metered at 20/min, so both modules must call the SAME host; importing
    # it makes drift impossible (tests/test_domains.py asserts they agree).
    from src.pipeline.oracles import FDIC_INSTITUTIONS_URL as _FDIC_INSTITUTIONS_URL
except ImportError:                      # the oracle slice is optional to this module
    _FDIC_INSTITUTIONS_URL = 'https://api.fdic.gov/banks/institutions'

log = logging.getLogger(__name__)

__all__ = [
    'normalize_host', 'resolve', 'significant_tokens', 'core_tokens', 'is_bank_shaped',
    'is_denylisted', 'DENYLIST', 'RSS_FEED_DOMAINS', 'MULTI_SUFFIX', 'METHODS',
    'DOMAINS_GUESS_ENABLED', 'DOMAINS_CLEARBIT_ENABLED', 'DOMAINS_LIVE_ENABLED',
]

# ── paths / endpoints / knobs ────────────────────────────────────────────────
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
# config.yaml is gitignored; GitHub Actions copies config.example.yaml over it
# and a fresh clone has done neither — so the example is the fallback.
CONFIG_PATHS = (os.path.join(REPO_ROOT, 'config.yaml'),
                os.path.join(REPO_ROOT, 'config.example.yaml'))
# Another slice's monthly-refreshed oracle tables. Optional, read-only, fail-soft.
ORACLES_DB_PATH = os.path.join(REPO_ROOT, 'state', 'oracles.db')

CLEARBIT_URL = 'https://autocomplete.clearbit.com/v1/companies/suggest'
FDIC_URL = _FDIC_INSTITUTIONS_URL       # api.fdic.gov (see the import note above)
FDIC_FIELDS = 'NAME,CITY,STALP,WEBADDR,CERT,NAMEHCR'
SEC_SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK{cik:010d}.json'

TIMEOUT = (5, 10)        # (connect, read) seconds on every live call
RETRY_SLEEP_S = 1.0      # pause before the single 5xx retry
# Politeness floor between calls to one endpoint. FDIC publishes 120/min, SEC
# asks for ≤10/s, Clearbit publishes nothing (so: gentle).
MIN_INTERVAL_S = {'fdic': 0.5, 'sec': 0.15, 'clearbit': 0.25, 'guess': 0.5}

# Result vocabulary for resolve()['method'] (plain data for the caller).
#   None      — clean miss: every rung looked and found nothing (negative-cached)
#   'error'   — a live rung had transport/5xx trouble (NOT negative-cached)
#   'skipped' — live rungs not attempted (negative cache active / live disabled)
METHODS = ('hint_url', 'cache', 'oracle', 'fdic', 'clearbit', 'sec', 'guess',
           'error', 'skipped', None)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


# Guess-and-verify is OFF by default (research 2026-09-08: unsafe + imprecise).
DOMAINS_GUESS_ENABLED = _env_flag('DOMAINS_GUESS_ENABLED', False)
# Kill-switch: Clearbit autocomplete is undocumented and could vanish or start
# rate-limiting without notice.
DOMAINS_CLEARBIT_ENABLED = _env_flag('DOMAINS_CLEARBIT_ENABLED', True)
# Global switch for every network rung (dry runs, outages, tests).
DOMAINS_LIVE_ENABLED = _env_flag('DOMAINS_LIVE_ENABLED', True)


# ── config (fail-soft at import) ─────────────────────────────────────────────
def _load_config(paths: Tuple[str, ...] = CONFIG_PATHS) -> dict:
    """First readable config as a dict, else {} — a missing or malformed
    config must never block import (the module is imported by the enricher)."""
    for path in paths:
        try:
            import yaml
            with open(path, encoding='utf-8') as fh:
                cfg = yaml.safe_load(fh) or {}
            if isinstance(cfg, dict):
                return cfg
        except Exception as e:  # noqa: BLE001 — missing file, bad YAML, no PyYAML
            log.debug('domains: %s unreadable (%s)', path, e)
    return {}


_CFG = _load_config()
# SEC requires a descriptive User-Agent — reuse the string sec_scraper.py uses.
SEC_USER_AGENT = ((_CFG.get('sec_filings') or {}).get('user_agent')
                  or 'TeamAlbert Sales Intelligence (sales-leads@teamalbert.local)')
DEFAULT_USER_AGENT = ((_CFG.get('scraper') or {}).get('user_agent')
                      or 'Mozilla/5.0 (compatible; SalesTerritoryBot/1.0)')
USER_AGENTS = {'clearbit': DEFAULT_USER_AGENT, 'fdic': DEFAULT_USER_AGENT,
               'sec': SEC_USER_AGENT, 'guess': DEFAULT_USER_AGENT}


# ── public-suffix knowledge (small, hand-kept — tldextract is not installed) ─
# Second-level suffixes under which the registrable domain is THREE labels.
MULTI_SUFFIX = frozenset({
    'co.uk', 'org.uk', 'ac.uk', 'gov.uk', 'ltd.uk', 'plc.uk', 'me.uk', 'net.uk',
    'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au',
    'co.nz', 'org.nz', 'net.nz', 'govt.nz',
    'co.in', 'net.in', 'org.in', 'firm.in',
    'com.br', 'net.br', 'org.br',
    'com.mx', 'org.mx',
    'com.sg', 'com.hk', 'com.my', 'com.ph', 'com.tw', 'co.id', 'co.th',
    'co.jp', 'or.jp', 'ne.jp', 'ac.jp', 'go.jp',
    'co.kr', 'or.kr',
    'com.cn', 'net.cn', 'org.cn',
    'co.za', 'org.za',
    'com.tr', 'com.ar', 'com.co', 'com.pe', 'com.ve', 'com.uy', 'com.ec',
    'co.il', 'org.il',
    # Canada: federal + every provincial/territorial second-level, listed
    # explicitly rather than "any two letters .ca" — CIRA sells two-letter
    # .ca names to companies (ir.td.ca must stay td.ca), whereas under .us
    # every two-letter second-level IS a state locality zone.
    'gc.ca', 'on.ca', 'qc.ca', 'bc.ca', 'ab.ca', 'mb.ca', 'sk.ca', 'ns.ca',
    'nb.ca', 'nl.ca', 'pe.ca', 'yk.ca', 'nt.ca', 'nu.ca',
})
_STATE_SUFFIX = re.compile(r'^[a-z]{2}\.us$')          # boston.ma.us  → 3 labels
_K12_SUFFIX = re.compile(r'^k12\.[a-z]{2}\.us$')      # wcpss.k12.nc.us → 4 labels

# ── denylist: registrable domains that are never an account's identity ──────
DENYLIST = frozenset({
    # data aggregators / directories — pages ABOUT a company
    'linkedin.com', 'zoominfo.com', 'crunchbase.com', 'bloomberg.com', 'rocketreach.co',
    'growjo.com', 'leadiq.com', 'dnb.com', 'pitchbook.com', 'cbinsights.com', 'owler.com',
    'craft.co', 'apollo.io', 'lusha.com', 'signalhire.com', 'datanyze.com',
    'opencorporates.com', 'buzzfile.com', 'manta.com', 'bbb.org', 'yelp.com',
    'yellowpages.com', 'mapquest.com', 'kompass.com', 'wikipedia.org', 'wikidata.org',
    # job boards / ATS
    'glassdoor.com', 'indeed.com', 'adzuna.com', 'ziprecruiter.com', 'lever.co',
    'greenhouse.io', 'myworkdayjobs.com', 'icims.com', 'smartrecruiters.com',
    'jobvite.com', 'bamboohr.com', 'workable.com',
    # regulators / filings
    'sec.gov', 'fdic.gov', 'ncua.gov', 'finra.org', 'irs.gov', 'propublica.org',
    'guidestar.org', 'candid.org', 'sedarplus.ca', 'edgar-online.com',
    # wires / news
    'prnewswire.com', 'globenewswire.com', 'businesswire.com', 'newswire.com',
    'newswire.ca', 'accesswire.com', 'einpresswire.com', 'prweb.com', 'webwire.com',
    'pymnts.com', 'techcrunch.com', 'reuters.com', 'finsmes.com', 'pehub.com',
    'buyoutsinsider.com', 'businessinsider.com', 'cnbc.com', 'foxbusiness.com',
    'financialpost.com', 'insurancejournal.com', 'carriermanagement.com',
    'wealthmanagement.com', 'coindesk.com', 'theblock.co', 'decrypt.co',
    'nonprofitquarterly.org', 'thenonprofittimes.com', 'philanthropy.com',
    'associationsnow.com', 'nvca.org', 'pe-insights.com', 'yahoo.com', 'msn.com',
    'forbes.com', 'fortune.com', 'axios.com', 'wsj.com', 'ft.com',
    'theglobeandmail.com', 'apnews.com', 'bizjournals.com', 'patch.com',
    'google.com', 'bing.com', 'duckduckgo.com',
    # social
    'facebook.com', 'x.com', 'twitter.com', 'instagram.com', 'youtube.com',
    'tiktok.com', 'threads.net', 'medium.com', 'substack.com',
    # shorteners / site builders / hosted pages
    'bit.ly', 't.co', 'lnkd.in', 'linktr.ee', 'wixsite.com', 'squarespace.com',
    'godaddysites.com', 'weebly.com', 'wordpress.com', 'blogspot.com', 'github.io',
    'notion.site', 'mailchi.mp', 'eventbrite.com', 'docsend.com',
})
# Host-level entries (the registrable domain alone is not the right cut).
DENY_HOSTS = frozenset({'sites.google.com'})
# Registrar / parking hosts a guessed domain may redirect to (guess rung only).
_PARKING = frozenset({
    'sedoparking.com', 'sedo.com', 'hugedomains.com', 'godaddy.com', 'dan.com',
    'afternic.com', 'bodis.com', 'parkingcrew.net', 'namecheap.com', 'buydomains.com',
    'squadhelp.com', 'undeveloped.com', 'domainmarket.com',
})

# Profile-page shapes on denylisted hosts → alias, never a domain.
#   linkedin.com/company/<slug>   crunchbase.com/organization/<slug>
#   zoominfo.com/c/<slug>/<id>
_PROFILE_PATHS = (
    re.compile(r'^/(?:company|organization|organisation|school)/([^/?#]+)', re.I),
    re.compile(r'^/c/([^/?#]+)/\d+', re.I),
)

_IPV4 = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
_LABEL = re.compile(r'^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?$')
_WWW = re.compile(r'^www\d*\.')


def _parse_host(url_or_host: Any) -> dict:
    """Pure host/registrable-domain split — no denylist, no flags.
    {'host', 'domain', 'labels', 'path', 'reason'}; host/domain None on reject."""
    out: Dict[str, Any] = {'host': None, 'domain': None, 'labels': [], 'path': '', 'reason': None}
    s = str(url_or_host or '').strip()
    if not s or s.lower() == 'nan':
        out['reason'] = 'empty'
        return out
    if '://' not in s:
        s = 'http://' + s.lstrip('/')
    try:
        parts = urlparse(s)
        host = parts.hostname
    except ValueError:
        out['reason'] = 'unparseable'
        return out
    if not host:
        out['reason'] = 'no_host'
        return out
    out['path'] = parts.path or ''
    host = host.lower().strip().rstrip('.')
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        out['reason'] = 'idna'
        return out
    if ':' in host or _IPV4.match(host):
        out['reason'] = 'ip'
        return out
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        out['reason'] = 'localhost'
        return out
    host = _WWW.sub('', host)
    labels = host.split('.')
    if len(labels) < 2:
        out['reason'] = 'no_dot'
        return out
    if any(not _LABEL.match(lb) for lb in labels):
        out['reason'] = 'bad_label'
        return out
    if len(labels[-1]) < 2 or not labels[-1].isalpha():
        out['reason'] = 'bad_tld'
        return out
    n = 2
    if len(labels) >= 3:
        last2 = '.'.join(labels[-2:])
        if last2 in MULTI_SUFFIX or _STATE_SUFFIX.match(last2):
            n = 3
        if len(labels) >= 4 and _K12_SUFFIX.match('.'.join(labels[-3:])):
            n = 4
    out['host'] = host
    out['labels'] = labels
    out['domain'] = '.'.join(labels[-n:])
    return out


def _rss_feed_domains(cfg: dict) -> frozenset:
    """Registrable domains of every sources.rss_feeds URL — a feed host is a
    place we READ about companies, never one of them."""
    out = set()
    try:
        feeds = (cfg.get('sources') or {}).get('rss_feeds') or []
        for feed in feeds:
            url = feed.get('url') if isinstance(feed, dict) else feed
            dom = _parse_host(url)['domain']
            if dom:
                out.add(dom)
    except Exception as e:  # noqa: BLE001 — malformed config is not our problem
        log.debug('domains: rss_feeds unreadable (%s)', e)
    return frozenset(out)


RSS_FEED_DOMAINS = _rss_feed_domains(_CFG)


def is_denylisted(domain: Optional[str], host: Optional[str] = None) -> bool:
    return bool(domain) and (domain in DENYLIST or domain in RSS_FEED_DOMAINS
                             or (host or '') in DENY_HOSTS)


def _profile_alias(domain: str, path: str) -> Optional[str]:
    provider = domain.split('.')[0]
    for rx in _PROFILE_PATHS:
        m = rx.match(path or '')
        if m:
            slug = m.group(1).strip().lower().rstrip('/')
            if slug:
                return f'{provider}:{slug}'
    return None


def _flag_for(domain: str) -> Optional[str]:
    """Flag-not-deny: schools and governments are real identities the gates
    module rejects elsewhere; here we just label them."""
    labels = domain.split('.')
    if len(labels) >= 3 and _K12_SUFFIX.match('.'.join(labels[-3:])):
        return 'k12'
    if labels[-1] in ('gov', 'mil') or domain.endswith(('.gov.uk', '.gc.ca', '.gov.au')):
        return 'gov'
    if labels[-1] == 'edu' or domain.endswith(('.ac.uk', '.edu.au', '.ac.jp')):
        return 'edu'
    return None


def normalize_host(url_or_host: Any) -> dict:
    """URL or bare host → {'host', 'domain', 'alias', 'denied', 'flag', 'reason'}.

    host   — lowercase, IDNA, trailing dot and leading www./www2. stripped
             ('ir.hercrentals.com'); None when rejected (IP literal, no dot,
             localhost, unparseable).
    domain — registrable domain ('hercrentals.com', 'acme.co.uk',
             'wcpss.k12.nc.us'); None when rejected OR denylisted.
    alias  — 'linkedin:<slug>' / 'crunchbase:<slug>' / 'zoominfo:<slug>' for
             a profile page on a denylisted host (never a domain).
    flag   — 'k12' | 'edu' | 'gov' | None (flag-not-deny).
    reason — why domain is None: 'empty' 'ip' 'no_dot' 'localhost' 'idna'
             'bad_label' 'bad_tld' 'denylisted' ...
    """
    p = _parse_host(url_or_host)
    out: Dict[str, Any] = {'host': p['host'], 'domain': None, 'alias': None,
                           'denied': False, 'flag': None, 'reason': p['reason']}
    if not p['host']:
        return out
    if is_denylisted(p['domain'], p['host']):
        out['denied'] = True
        out['reason'] = 'denylisted'
        out['alias'] = _profile_alias(p['domain'], p['path'])
        return out
    out['domain'] = p['domain']
    out['flag'] = _flag_for(p['domain'])
    return out


# ── name tokens ──────────────────────────────────────────────────────────────
_STOP = frozenset({'the', 'a', 'an', 'of', 'and', 'for', 'at', 'in', 'on', 'by',
                   'to', 'de', 'la', 'le', 'du', 'des', 'et'})
# Legal forms carry no identity and registries put them mid-string ("The
# Washington Trust Company, of Westerly") — account_key only strips a trailing
# one, so the identity comparison drops them ANYWHERE.
_LEGAL_FORM = frozenset({
    'inc', 'incorporated', 'corp', 'corporation', 'co', 'cos', 'company', 'llc', 'llp',
    'lp', 'ltd', 'limited', 'plc', 'pllc', 'pc', 'lc', 'sa', 'ag', 'gmbh', 'nv', 'bv',
})
# Charter-type tails on FDIC legal names: 'Bank of X, National Association', 'Y, FSB'.
_CHARTER_TAIL = frozenset({'national', 'association', 'na', 'fsb', 'ssb', 'nb'})
# Corporate vocabulary that never identifies WHICH company ("Capital", "Bank").
_GENERIC = frozenset({
    'inc', 'llc', 'ltd', 'corp', 'corporation', 'co', 'company', 'group', 'holdings',
    'holding', 'partners', 'partner', 'capital', 'bank', 'banks', 'bancorp',
    'bancorporation', 'bancshares', 'bankshares', 'banc', 'financial', 'finance',
    'trust', 'services', 'service', 'solutions', 'international', 'global', 'national',
    'association', 'associates', 'management', 'advisors', 'advisers', 'advisory',
    'wealth', 'investment', 'investments', 'credit', 'union', 'federal', 'savings',
    'mutual', 'insurance', 'agency', 'firm', 'fund', 'funds', 'llp', 'plc', 'na', 'nv',
    'sa', 'ag', 'gmbh', 'bv', 'pllc', 'lp', 'pc', 'ventures', 'enterprises',
    'industries', 'technologies', 'technology', 'systems', 'consulting', 'consultants',
    'limited', 'incorporated', 'worldwide', 'usa', 'america', 'american',
})
# Tokens left OUT of the FDIC NAME:*X* filter — they'd match every charter in
# the state. 'trust' stays in on purpose: Washington Trust ≠ Washington Federal.
_FDIC_FILTER_DROP = frozenset({
    'bank', 'banks', 'banking', 'bancorp', 'bancorporation', 'bancshares', 'bankshares',
    'banc', 'financial', 'holdings', 'holding', 'group', 'federal', 'savings', 'state',
})
_BANK_SHAPE = re.compile(
    r'\b(bank|banks|banking|banc\w*|bankshares|trust|savings|thrift|fsb|credit union)\b')
_CANADA = frozenset({'ON', 'QC', 'NB', 'NS', 'PE', 'NL', 'BC', 'AB', 'MB', 'SK', 'YT', 'NT', 'NU'})
_US_STATES = frozenset(c for c in ALL_STATE_CODES if c not in _CANADA)


_DOTTED_ABBR = re.compile(r'\b(?:[a-z]\.){2,}', re.I)     # 'F.S.B.' 'N.A.' 'L.L.C.'


def core_tokens(name: Any) -> List[str]:
    """Identity tokens: account_key minus connectives, legal forms and charter
    tails, wherever they sit. Keeps 'bank' / 'trust' / 'federal' — they are
    what separates same-city institutions. Dotted abbreviations collapse
    first ('X, F.S.B.' → 'fsb', not 'f' 's' 'b') so the tail rule can see them."""
    s = _DOTTED_ABBR.sub(lambda m: m.group(0).replace('.', ''), str(name or ''))
    return [t for t in account_key(s).split()
            if t not in _STOP and t not in _LEGAL_FORM and t not in _CHARTER_TAIL]


def significant_tokens(name: Any) -> List[str]:
    """account_key tokens minus stopwords and corporate vocabulary; falls back
    to all non-stop tokens when nothing else is left ('Capital Group')."""
    toks = account_key(name).split()
    sig = [t for t in toks if t not in _STOP and t not in _GENERIC]
    return sig or [t for t in toks if t not in _STOP]


def is_bank_shaped(name: Any) -> bool:
    """Bank / trust company / thrift / credit union by name shape."""
    return bool(_BANK_SHAPE.search(str(name or '').lower()))


def _fdic_eligible(name: Any) -> bool:
    # Credit unions are NCUA, not FDIC — a BankFind call would just return 0.
    return is_bank_shaped(name) and 'credit union' not in str(name or '').lower()


def _fdic_tokens(name: Any) -> List[str]:
    core = core_tokens(name)
    toks = [t for t in core if t not in _FDIC_FILTER_DROP and len(t) >= 2]
    return (toks or core)[:4]


def _name_match(qcore: List[str], row_names: List[Any],
                norm_names: List[Any] = ()) -> Optional[str]:
    """How a registry row's name(s) relate to the query's core tokens:
    'exact' when either name form reduces to the same tokens, 'superset' when
    every query token sits somewhere in the row's names (the ', of Westerly'
    / holding-company case) — but only for ≥2-token queries, because
    'Washington' alone must not match everything. None otherwise."""
    pool: set = set()
    for rn in row_names:
        core = core_tokens(rn)
        if core and core == qcore:
            return 'exact'
        pool.update(core)
    for nn in norm_names:
        toks = str(nn or '').split()
        if toks and toks == qcore:
            return 'exact'
        pool.update(toks)
    if len(qcore) >= 2 and set(qcore) <= pool:
        return 'superset'
    return None


def _registrant_id(c: dict) -> Any:
    """What makes two candidate rows the SAME registrant: the registry id
    (oracle `id`, FDIC `cert`), else the (table, name, state, domain) tuple."""
    for k in ('id', 'cert'):
        if c.get(k) is not None:
            return (k, str(c[k]))
    return ('row', c.get('table'), c.get('name'), c.get('state'), c.get('domain'))


def _pick_unique(cands: List[dict]) -> Tuple[Optional[dict], str]:
    """(row, 'exact'|'superset') when the best tier names ONE registrant (or
    several that all publish the same website — one institution under two
    charters); else (None, 'ambiguous'|'no_domain'|'no_match'|'miss').

    M3 (review 2026-09-08): ambiguity is judged over EVERY registrant at the
    best tier, not only those with a website. 'Cornerstone Advisors' with no
    state has exact rows in KS (website), NC and AR (none) — the old rule
    saw one usable domain and answered HIGH for Kansas. Three registrants
    → the caller needs a state. A best tier whose only registrant lists no
    website is 'no_domain': the registrant exists, so a looser superset row
    (a DIFFERENT firm) must not answer for it."""
    for tier in ('exact', 'superset'):
        rows = [c for c in cands if c.get('match') == tier]
        if not rows:
            continue
        ids = {_registrant_id(c) for c in rows}
        with_domain = [c for c in rows if c.get('domain')]
        if len(ids) > 1:
            if len(with_domain) == len(rows) and len({c['domain'] for c in with_domain}) == 1:
                return with_domain[0], tier
            return None, 'ambiguous'
        if with_domain:
            return with_domain[0], tier
        return None, 'no_domain'
    return None, ('no_match' if any(c.get('domain') for c in cands) else 'miss')


def _domain_matches_name(domain: str, name: Any) -> List[str]:
    """Significant name tokens (≥3 chars) found inside the domain's first
    label: 'hercrentals' ⊇ 'herc'. Generic tokens never count — 'bank' inside
    'bankofamerica' says nothing about WHICH bank."""
    compact = re.sub(r'[^a-z0-9]', '', domain.split('.')[0])
    return [t for t in significant_tokens(name) if len(t) >= 3 and t in compact]


def _parse_int(value: Any) -> Optional[int]:
    digits = re.sub(r'\D', '', str(value or ''))
    try:
        n = int(digits) if digits else 0
    except ValueError:
        return None
    return n or None


def _state_from_hints(hints: dict) -> Optional[str]:
    for k in ('state', 'hq_state'):                 # hq_state = the typed column
        explicit = str(hints.get(k) or '').strip().upper()
        if len(explicit) == 2 and explicit in ALL_STATE_CODES:
            return explicit
    return hq_state_code(hints.get('hq'))


def _add(seq: List[str], item: Optional[str]) -> None:
    if item and item not in seq:
        seq.append(item)


_ZOOMINFO_ID = re.compile(r'\d{4,}')       # ZoomInfo company ids are all-digit


def _hint_aliases(hints: dict) -> List[str]:
    """Identifier-shaped hints → aliases: zi (a ZoomInfo profile URL or an
    all-digit company id), linkedin URL, cik, crd. Aliases are for dedup —
    never a domain. M4 (review 2026-09-08): a bare word is NOT an id — the
    enricher once passed the subindustry label in `zi` and every account
    got the alias 'zoominfo:banking'."""
    out: List[str] = []
    zi = hints.get('zi')
    if zi:
        s = str(zi).strip()
        if '/' in s or '.' in s:
            _add(out, normalize_host(s)['alias'])
        elif _ZOOMINFO_ID.fullmatch(s):
            _add(out, f'zoominfo:{s}')
    if hints.get('linkedin'):
        _add(out, normalize_host(str(hints['linkedin']))['alias'])
    for k in ('cik', 'crd'):
        n = _parse_int(hints.get(k))
        if n:
            _add(out, f'{k}:{n}')
    return out


# ── HTTP plumbing: per-endpoint sessions, throttle, retry-on-5xx, fail-soft ──
class _Transport(Exception):
    """Connection/timeout trouble — transient, never negative-cached."""


_SESSIONS: Dict[str, Any] = {}
_LAST_CALL: Dict[str, float] = {}
# Statuses that say "not now", not "no such company" — a Clearbit 403 the day
# it is switched off must not negative-cache every account for 7/30/90 days.
_TRANSIENT_STATUS = frozenset({401, 403, 407, 408, 425, 429})


def _sleep(seconds: float) -> None:      # monkeypatch point for tests
    time.sleep(seconds)


def _new_session(user_agent: str):       # monkeypatch point for tests
    s = requests.Session()
    s.headers.update({'User-Agent': user_agent, 'Accept': 'application/json, */*;q=0.5'})
    return s


def _session_for(endpoint: str):
    if endpoint not in _SESSIONS:
        _SESSIONS[endpoint] = _new_session(USER_AGENTS.get(endpoint, DEFAULT_USER_AGENT))
    return _SESSIONS[endpoint]


def _throttle(endpoint: str) -> None:
    gap = MIN_INTERVAL_S.get(endpoint, 0.0)
    last = _LAST_CALL.get(endpoint)
    if gap and last is not None:
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            _sleep(wait)
    _LAST_CALL[endpoint] = time.monotonic()


def _request(endpoint: str, method: str, url: str, params: Optional[dict] = None,
             allow_redirects: bool = True):
    """One call, one retry on 5xx only. Raises _Transport on connection trouble."""
    session = _session_for(endpoint)
    resp = None
    for attempt in (1, 2):
        _throttle(endpoint)
        try:
            resp = session.request(method, url, params=params, timeout=TIMEOUT,
                                   allow_redirects=allow_redirects)
        except Exception as e:  # noqa: BLE001 — requests raises RequestException,
            # but urllib3/ssl quirks have surfaced others; all are "not now".
            raise _Transport(type(e).__name__) from None
        if resp.status_code >= 500 and attempt == 1:
            _sleep(RETRY_SLEEP_S)
            continue
        break
    return resp


def _get_json(endpoint: str, url: str, params: Optional[dict] = None) -> Tuple[Any, Optional[str]]:
    """(payload, error). error is set only for TRANSIENT trouble — transport,
    5xx after retry, 429/403-class, unparseable body (Cloudflare challenge).
    A plain 4xx (404 CIK, 400 filter) is a definitive miss: (None, None) —
    EXCEPT on the Clearbit endpoint (M7, review 2026-09-08): an undocumented
    keyless API answers "no such company" with 200 + [], so a 404/410 there
    means the endpoint itself is gone or moved; treating it as a miss would
    negative-cache every account for 7/30/90 days the day it is retired."""
    try:
        resp = _request(endpoint, 'GET', url, params)
    except _Transport as e:
        log.debug('domains: %s transport error: %s', endpoint, e)
        return None, f'transport:{e}'
    if resp.status_code >= 500 or resp.status_code in _TRANSIENT_STATUS:
        return None, f'http:{resp.status_code}'
    if endpoint == 'clearbit' and resp.status_code != 200:
        return None, f'http:{resp.status_code}'
    if resp.status_code >= 400:
        return None, None
    try:
        return resp.json(), None
    except ValueError:
        return None, 'badjson'


# ── rungs: each returns (answer | None, error | None, candidates, outcome) ───
def _answer(domain: str, host: Optional[str], confidence: str, evidence: dict) -> dict:
    return {'domain': domain, 'host': host or domain, 'confidence': confidence,
            'evidence': evidence}


_ORACLE_TABLES = (('bank', 'name', 'cert'), ('ria_firm', 'business_name', 'crd'))


def _oracle_rows(conn: sqlite3.Connection, table: str, name_col: str,
                 probe: str, state: Optional[str]) -> list:
    """Candidate rows by the rarest query token; tolerant of an oracles.db
    build without norm_name, silent on a missing table."""
    like = f'%{probe}%'
    tail = ' AND upper(state) = ?' if state else ''
    tail_params: tuple = (state,) if state else ()
    for where, params in (
        (f'(norm_name LIKE ? OR lower({name_col}) LIKE ?)', (like, like)),
        (f'lower({name_col}) LIKE ?', (like,)),
    ):
        try:
            return conn.execute(f'SELECT * FROM {table} WHERE {where}{tail} LIMIT 500',
                                params + tail_params).fetchall()
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower() or 'busy' in str(e).lower():
                raise           # L1: the refresh is writing — transient, not a schema difference
            continue        # column/table not in this build of oracles.db
    return []


def _rung_oracle(name: str, key: str, state: Optional[str]):
    """Local state/oracles.db (another slice's bank / ria_firm tables) by
    identity tokens, narrowed to the hinted state. Read-only; any schema
    difference is a silent miss. Without a state hint the answer must be
    unique across ALL states (Washington Trust → 2 domains → ambiguous)."""
    path = ORACLES_DB_PATH
    if not path or not os.path.isfile(path):
        return None, None, [], 'absent'
    qcore = core_tokens(name)
    if not qcore:
        return None, None, [], 'no_tokens'
    probe = max(qcore, key=len)                      # longest ≈ rarest
    try:
        conn = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=2)
    except sqlite3.Error as e:
        return None, f'oracle:{type(e).__name__}', [], 'unreadable'
    cands: List[dict] = []
    try:
        conn.row_factory = sqlite3.Row
        for table, name_col, id_col in _ORACLE_TABLES:
            for r in _oracle_rows(conn, table, name_col, probe, state):
                cols = r.keys()
                rname = r[name_col] if name_col in cols else None
                norm = r['norm_name'] if 'norm_name' in cols else None
                match = _name_match(qcore, [rname], [norm])
                if not match:
                    continue
                nh = normalize_host(r['website'] if 'website' in cols else None)
                cands.append({
                    'table': table, 'name': rname,
                    'state': (str(r['state']).upper() if 'state' in cols and r['state'] else None),
                    'id': r[id_col] if id_col in cols else None,
                    'domain': nh['domain'], 'host': nh['host'], 'match': match,
                })
    except sqlite3.Error as e:
        return None, f'oracle:{type(e).__name__}', cands, 'unreadable'
    finally:
        conn.close()
    pick, why = _pick_unique(cands)
    if pick:
        return _answer(pick['domain'], pick['host'], 'high',
                       {'table': pick['table'], 'id': pick['id'], 'name': pick['name'],
                        'state': pick['state'], 'match': why}), None, cands, 'hit'
    return None, None, cands, why


def _fdic_rows(data: Any) -> List[dict]:
    rows = []
    for item in ((data or {}).get('data') or []) if isinstance(data, dict) else []:
        if isinstance(item, dict):
            inner = item.get('data')
            rows.append(inner if isinstance(inner, dict) else item)
    return rows


def _rung_fdic(name: str, key: str, state: Optional[str]):
    """FDIC BankFind for bank-shaped names. State is REQUIRED (research
    2026-09-08: same-name banks in different states); NAME uses *TOKEN*
    wildcards because the filter is exact-token."""
    if not _fdic_eligible(name):
        return None, None, [], 'not_bank'
    if not state or state not in _US_STATES:
        return None, None, [], 'no_state'
    toks = _fdic_tokens(name)
    if not toks:
        return None, None, [], 'no_tokens'
    name_filter = ' AND '.join(f'NAME:*{t.upper()}*' for t in toks)
    params = {'filters': f'ACTIVE:1 AND STALP:{state} AND ({name_filter})',
              'fields': FDIC_FIELDS, 'format': 'json', 'limit': 25}
    data, err = _get_json('fdic', FDIC_URL, params)
    if err:
        return None, err, [], 'error'
    qcore = core_tokens(name)
    cands: List[dict] = []
    for r in _fdic_rows(data):
        nh = normalize_host(r.get('WEBADDR'))
        cands.append({
            'name': r.get('NAME'), 'holding_company': r.get('NAMEHCR'),
            'city': r.get('CITY'), 'state': r.get('STALP'), 'cert': r.get('CERT'),
            'domain': nh['domain'], 'host': nh['host'],
            'match': _name_match(qcore, [r.get('NAME'), r.get('NAMEHCR')]),
        })
    pick, why = _pick_unique(cands)
    if pick:
        return _answer(pick['domain'], pick['host'], 'high',
                       {'cert': pick['cert'], 'name': pick['name'], 'city': pick['city'],
                        'state': pick['state'], 'match': why}), None, cands, 'hit'
    return None, None, cands, why


def _rung_clearbit(name: str, key: str, bank: bool, oracle_domains: set):
    """Clearbit autocomplete with the two accept rules research 2026-09-08
    found precise: top-1 normalizes to the query's account_key, or exactly one
    result is a token-superset of the query. Bank-shaped names with several
    results are deferred — only the state (FDIC) can settle them."""
    if not DOMAINS_CLEARBIT_ENABLED:
        return None, None, [], 'disabled'
    data, err = _get_json('clearbit', CLEARBIT_URL, {'query': name})
    if err:
        return None, err, [], 'error'
    cands: List[dict] = []
    for r in (data if isinstance(data, list) else []):
        if not isinstance(r, dict):
            continue
        nh = normalize_host(r.get('domain'))
        cands.append({'name': r.get('name'), 'raw_domain': r.get('domain'),
                      'domain': nh['domain'], 'host': nh['host'],
                      'key': account_key(r.get('name'))})
    usable = [c for c in cands if c['domain']]
    if not usable:
        return None, None, cands, 'miss'
    qtoks = set(key.split())
    pick, why = None, None
    if bank and len(usable) > 1:
        why = 'bank_multi_defer'
    elif usable[0]['key'] == key:
        pick, why = usable[0], 'exact_top1'
    elif len(qtoks) < 2:
        # L5 (review 2026-09-08): one token is contained by anything —
        # 'Herc' ⊂ 'Herc Rentals' ⊂ 'Herc Holdings' — the same guard the
        # registry matcher applies (_name_match).
        why = 'short_query'
    else:
        supers = [c for c in usable if qtoks <= set(c['key'].split())]
        if len(supers) == 1:
            pick, why = supers[0], 'single_superset'
        else:
            why = 'multi_superset' if supers else 'no_match'
    if pick:
        agrees = pick['domain'] in oracle_domains
        return _answer(pick['domain'], pick['host'], 'high' if agrees else 'medium',
                       {'match': why, 'result_name': pick['name'],
                        'n_results': len(cands),
                        'agrees_with_oracle': agrees}), None, cands, 'hit'
    return None, None, cands, why


def _rung_sec(name: str, key: str, hints: dict):
    """SEC submissions JSON — only when a CIK is on the record. `website` is
    populated for some larger filers; `investorWebsite` rarely."""
    cik = _parse_int(hints.get('cik'))
    if cik is None:
        return None, None, [], 'no_cik'
    data, err = _get_json('sec', SEC_SUBMISSIONS_URL.format(cik=cik))
    if err:
        return None, err, [], 'error'
    if not isinstance(data, dict):
        return None, None, [], 'miss'
    sec_name = data.get('name')
    cands: List[dict] = []
    for field in ('website', 'investorWebsite'):
        raw = data.get(field)
        if not raw:
            continue
        nh = normalize_host(raw)
        cands.append({'field': field, 'raw': raw, 'domain': nh['domain'],
                      'host': nh['host'], 'name': sec_name})
    usable = [c for c in cands if c['domain']]
    if not usable:
        return None, None, cands, 'empty'
    c = usable[0]
    # A CIK hint that belongs to a different filer (acquirer vs target) would
    # be a wrong 'high' — demote when the SEC entity name shares nothing.
    agrees = (account_key(sec_name) == key
              or bool(set(significant_tokens(sec_name)) & set(significant_tokens(name))))
    return _answer(c['domain'], c['host'], 'high' if agrees else 'medium',
                   {'cik': cik, 'field': c['field'], 'sec_name': sec_name,
                    'name_agrees': agrees}), None, cands, 'hit'


def _rung_guess(name: str, key: str):
    """OFF by default. name → '<tokens>.com' + HEAD. A resolving, non-parked
    host is only 'low' — parked pages and Cloudflare challenges also answer —
    and it is never written to the firmographic identity."""
    if not DOMAINS_GUESS_ENABLED:
        return None, None, [], 'disabled'
    sig = [t for t in significant_tokens(name) if re.fullmatch(r'[a-z0-9]+', t)]
    joined = ''.join(sig)
    if not 3 <= len(joined) <= 40:
        return None, None, [], 'no_candidate'
    cand = joined + '.com'
    try:
        resp = _request('guess', 'HEAD', f'https://{cand}/', allow_redirects=True)
    except _Transport as e:
        # NXDOMAIN / refused IS the normal "no such site" outcome — a miss, not an error.
        return None, None, [{'candidate': cand, 'outcome': f'transport:{e}'}], 'miss'
    final = normalize_host(getattr(resp, 'url', '') or f'https://{cand}/')
    ok = (resp.status_code < 400 and final['domain'] == cand
          and final['domain'] not in _PARKING)
    cand_ev = {'candidate': cand, 'status': resp.status_code, 'final_domain': final['domain']}
    if ok:
        return _answer(cand, final['host'], 'low',
                       dict(cand_ev, sole_identity=False)), None, [cand_ev], 'hit'
    return None, None, [cand_ev], 'miss'


# ── the ladder ───────────────────────────────────────────────────────────────
def _result(domain: Optional[str], host: Optional[str], method: Optional[str],
            confidence: Optional[str], aliases: List[str], evidence: dict) -> dict:
    return {'domain': domain, 'host': host, 'method': method, 'confidence': confidence,
            'aliases': list(aliases), 'evidence': evidence}


def _persist(cache: Any, key: str, result: dict, raw: Optional[list], now: Optional[datetime]) -> None:
    """Write a hit through the AccountCache. Guesses never become the
    firmographic `domain` (research 2026-09-08: not a sole identity), and
    neither does a hint-only answer (H2, review 2026-09-08): the hint url
    came with the record — possibly from an article-only LLM pass that
    derived it from the company NAME, which satisfies the name-token test by
    construction — so it is returned as evidence for this run, never stored
    as a 365-day identity. Only its aliases (cik/crd/linkedin) are kept."""
    if cache is None or not result['domain']:
        return
    method = result['method']
    if method in ('cache', 'skipped', 'error'):
        return
    if method == 'hint_url':
        if result['aliases']:
            cache.set_firmographics(key, {'aliases': result['aliases']}, now=now)
        return
    if method != 'guess':
        cache.set_firmographics(key, {
            'domain': result['domain'], 'domain_method': method,
            'domain_confidence': result['confidence'],
            'aliases': result['aliases'] or None,
        }, now=now)
        cache.clear_negative(key, 'domain')
    if raw:
        cache.set_search(key, f'domain:{method}',
                         {'results': raw, 'method': method, 'chosen': result['domain']},
                         now=now)


def resolve(name: Any, hints: Optional[dict] = None, *, cache: Any = None,
            now: Optional[datetime] = None) -> dict:
    """Fail-soft wrapper: see _resolve for the ladder. An internal bug must
    degrade to method 'error' (retry later), never abort an enrichment run —
    the same contract cache.py keeps."""
    try:
        return _resolve(name, hints, cache, now)
    except Exception as e:  # noqa: BLE001
        log.warning('domains.resolve failed for %r: %s', str(name)[:80], type(e).__name__)
        return _result(None, None, 'error', None, [],
                       {'key': account_key(name), 'rungs': [], 'candidates': [],
                        'errors': {'internal': type(e).__name__}})


def _resolve(name: Any, hints: Optional[dict], cache: Any, now: Optional[datetime]) -> dict:
    """Resolve a company name to its website domain from free signals.

    hints: url (a COMPANY url — the firmographic `url`, not the article),
           source, hq ('Westerly, RI'), state / hq_state, zi, linkedin, cik, crd.
    cache: an AccountCache (src.pipeline.cache) or None. now: naive UTC.

    Returns {'domain', 'host', 'method', 'confidence', 'aliases', 'evidence'}:
      domain      registrable domain ('washtrust.com') or None
      host        the host it was seen on ('ir.hercrentals.com') or None
      method      one of METHODS — 'error' = transient trouble (retry later),
                  'skipped' = live rungs withheld, None = clean miss
      confidence  'high' | 'medium' | 'low' | None
      aliases     ['linkedin:<slug>', 'cik:123', ...] — dedup keys, never a domain
      evidence    plain-data trail: key, state, rungs tried, candidates, errors

    Ladder (cheapest first; stops at the first confident answer):
      0 hint url   never more than MEDIUM and never persisted (H2, review
                   2026-09-08): it came with the record — it beats every
                   NETWORK rung but yields to the local cache/oracle, and a
                   name-token match is recorded as evidence only (an LLM
                   that invents acme.com from "Acme" passes that test)
      1 cache      fresh firmographic `domain`
      2 oracle     state/oracles.db bank / ria_firm by identity tokens → high
      3 fdic       bank-shaped names, state required → high
      4 clearbit   exact-key top-1 or single superset → medium (high if it
                   agrees with an oracle/FDIC candidate)
      5 sec        CIK on the record → high when `website` is non-empty
      6 guess      only when DOMAINS_GUESS_ENABLED → low, never persisted
    Miss → cache.record_empty(key, 'domain', rung='scrape'); a transport error
    is NOT negative-cached.
    """
    hints = dict(hints or {})
    key = account_key(name)
    ev: Dict[str, Any] = {'key': key, 'rungs': [], 'candidates': [], 'errors': {}}
    aliases = _hint_aliases(hints)
    if not key:
        ev['rungs'].append('empty_name')
        return _result(None, None, None, None, aliases, ev)
    state = _state_from_hints(hints)
    bank = is_bank_shaped(name)
    ev['state'], ev['bank_shaped'] = state, bank
    if hints.get('source'):
        ev['source'] = hints['source']

    def finish(ans: dict, method: str, raw: Optional[list] = None) -> dict:
        ev['answer'] = ans['evidence']
        res = _result(ans['domain'], ans['host'], method, ans['confidence'], aliases, ev)
        try:
            _persist(cache, key, res, raw, now)
        except Exception as e:  # noqa: BLE001 — cache trouble never breaks a resolve
            log.debug('domains: persist failed for %s: %s', key, e)
        return res

    # (0) hint url — free, MEDIUM at most (H2): the name-token match is
    #     evidence, not a promotion, and the answer waits for cache/oracle.
    hint_ans = None
    if hints.get('url'):
        nh = normalize_host(hints['url'])
        _add(aliases, nh['alias'])
        if nh['domain']:
            matched = _domain_matches_name(nh['domain'], name)
            hint_ans = _answer(nh['domain'], nh['host'], 'medium',
                               {'hint_url': str(hints['url']), 'name_match': bool(matched),
                                'matched_tokens': matched, 'flag': nh['flag'],
                                'persisted': False})
            ev['rungs'].append(f'hint_url:{hint_ans["confidence"]}')
        else:
            ev['rungs'].append(f'hint_url:{nh["reason"]}')

    # (1) cache — a fresh domain persisted by an earlier confident rung.
    if cache is not None:
        try:
            fg = cache.get_firmographics(key, now=now) or {}
        except Exception as e:  # noqa: BLE001
            log.debug('domains: cache read failed for %s: %s', key, e)
            fg = {}
        for a in (fg.get('aliases') or []):
            _add(aliases, a)
        if fg.get('domain'):
            ev['rungs'].append('cache:hit')
            ans = _answer(fg['domain'], fg['domain'], fg.get('domain_confidence') or 'medium',
                          {'cached_method': fg.get('domain_method')})
            return finish(ans, 'cache')
        ev['rungs'].append('cache:miss')

    # (2) local oracle tables — free, authoritative, state-aware.
    oracle_domains: set = set()
    ans, err, cands, outcome = _rung_oracle(name, key, state)
    ev['rungs'].append(f'oracle:{outcome}')
    if err:
        # L1 (review 2026-09-08): a locked oracles.db — the monthly refresh
        # is writing — is TRANSIENT: the registry would have answered a
        # minute later. Recording it as a warning let the network miss that
        # followed negative-cache the account for 7/30/90 days, so a bank
        # the oracle knows went unresolved for weeks. It is an error now:
        # the ladder still runs (a Clearbit hit is still a hit) but a miss
        # is retried next run instead of being remembered.
        ev['errors']['oracle'] = err
    if cands:
        ev['candidates'].extend(dict(c, source='oracle') for c in cands)
        oracle_domains |= {c['domain'] for c in cands if c['domain']}
    if ans:
        return finish(ans, 'oracle', [c for c in cands if c['domain']])

    # A medium hint beats spending network calls — it came with the record.
    if hint_ans:
        return finish(hint_ans, 'hint_url')

    # Gate the network rungs.
    if cache is not None:
        try:
            skip = cache.should_skip(key, 'domain', now=now)
        except Exception:  # noqa: BLE001
            skip = False
        if skip:
            ev['rungs'].append('negative_cache:active')
            return _result(None, None, 'skipped', None, aliases, ev)
    if not DOMAINS_LIVE_ENABLED:
        ev['rungs'].append('live:disabled')
        return _result(None, None, 'skipped', None, aliases, ev)

    # (3) FDIC — banks, state required.
    ans, err, cands, outcome = _rung_fdic(name, key, state)
    ev['rungs'].append(f'fdic:{outcome}')
    if err:
        ev['errors']['fdic'] = err
    if cands:
        ev['candidates'].extend(dict(c, source='fdic') for c in cands)
        oracle_domains |= {c['domain'] for c in cands if c['domain']}
    if ans:
        return finish(ans, 'fdic', [c for c in cands if c['domain']])

    # (4) Clearbit autocomplete — one keyless call, medium unless an
    # oracle/FDIC candidate agrees.
    ans, err, cands, outcome = _rung_clearbit(name, key, bank, oracle_domains)
    ev['rungs'].append(f'clearbit:{outcome}')
    if err:
        ev['errors']['clearbit'] = err
    if cands:
        ev['candidates'].extend(dict(c, source='clearbit') for c in cands)
    if ans:
        return finish(ans, 'clearbit', [c for c in cands if c['domain']])

    # (5) SEC submissions — only when the record carries a CIK.
    ans, err, cands, outcome = _rung_sec(name, key, hints)
    ev['rungs'].append(f'sec:{outcome}')
    if err:
        ev['errors']['sec'] = err
    if cands:
        ev['candidates'].extend(dict(c, source='sec') for c in cands)
    if ans:
        return finish(ans, 'sec', [c for c in cands if c['domain']])

    # (6) guess-and-verify — off unless explicitly enabled; never the identity.
    ans, err, cands, outcome = _rung_guess(name, key)
    ev['rungs'].append(f'guess:{outcome}')
    if cands:
        ev['candidates'].extend(dict(c, source='guess') for c in cands)
    if ans:
        return finish(ans, 'guess', cands)

    # Miss. Aliases are still facts worth keeping; a transient error must not
    # start the 7/30/90-day negative ladder.
    if cache is not None:
        try:
            if aliases:
                cache.set_firmographics(key, {'aliases': aliases}, now=now)
            if not ev['errors']:
                cache.record_empty(key, 'domain', rung='scrape', now=now)
        except Exception as e:  # noqa: BLE001
            log.debug('domains: miss bookkeeping failed for %s: %s', key, e)
    method = 'error' if ev['errors'] else None
    return _result(None, None, method, None, aliases, ev)
