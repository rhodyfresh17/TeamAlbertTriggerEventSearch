#!/usr/bin/env python3
"""
enrichment_scout.py — Multi-company enrichment for trigger events.

For every event, this script:
  1. Reads the title + description and asks an LLM to identify ALL companies
     involved and their roles (Acquirer/Target, Investor/Portfolio Co., etc.)
  2. Searches Tavily for each company's firmographic data
  3. Uses an LLM to extract: website URL, industry, employee size, HQ, LinkedIn
  4. Writes the full list as a JSONB array to events.companies_data in Supabase

LLM backend (auto-selected):
  - If ANTHROPIC_API_KEY is set → uses claude-3-5-haiku (fast, cloud, works in CI)
  - Otherwise → uses Ollama qwen2.5:14b (local)

Company firmographics are cached per run so the same company in multiple events
is only searched once.

Prerequisites:
    pip install requests supabase python-dotenv
    Ollama must be running locally (or ANTHROPIC_API_KEY set for cloud)

Supabase migration (run once in SQL Editor before first use):
    ALTER TABLE events ADD COLUMN IF NOT EXISTS companies_data JSONB;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS enriched_at    TIMESTAMPTZ;

Usage:
    python enrichment_scout.py                  # Enrich all unenriched events
    python enrichment_scout.py --limit 10       # Process up to 10 events
    python enrichment_scout.py --re-enrich      # Re-enrich already-enriched events
    python enrichment_scout.py --dry-run        # Preview without writing to Supabase
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict
import subprocess

import requests

# ── .env ─────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass

try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
# Web-search backend. Switched from Tavily to local Firecrawl 2026-06-09 after
# hitting Tavily free-tier quota. Firecrawl is self-hosted on the user's Mac
# Studio (already running for Scout), so unlimited / free / private.
# Tavily kept as optional fallback when SEARCH_BACKEND='tavily' or Firecrawl unreachable.
SEARCH_BACKEND    = os.environ.get('SEARCH_BACKEND', 'firecrawl').lower()
FIRECRAWL_URL     = os.environ.get('FIRECRAWL_URL', 'http://localhost:3002')
SEARXNG_URL       = os.environ.get('SEARXNG_URL', 'http://localhost:8888')
TAVILY_API_KEY    = os.environ.get('TAVILY_API_KEY', '')  # fallback only now
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
CLAUDE_MODEL      = 'claude-3-5-haiku-20241022'  # optional cloud path — unused unless key set
# LLM primary = the shared local llama.cpp server (Qwen3.6, non-thinking, OpenAI /v1)
# that serves the whole Hermes fleet on :8091 (migrated 2026-07-11). Replaces the old
# Ollama qwen3-coder path — that was a SECOND ~25GB model reloading 6x/day, which
# thrashed RAM. Now one shared model, no per-job reload. Fallback = Scout agent (llm_json).
LLAMACPP_URL      = os.environ.get('LLAMACPP_URL',   'http://localhost:8091')
LLAMACPP_MODEL    = os.environ.get('LLAMACPP_MODEL', 'qwen3.6')
SCOUT_CONTAINER   = os.environ.get('SCOUT_CONTAINER', 'hermes-sales')

# Persistent firmographic-search cache (SQLite). Same company name within
# CACHE_TTL_DAYS doesn't re-search — saves time + quota for repeat companies.
CACHE_DB_PATH      = os.environ.get('CACHE_DB_PATH', 'trigger_events.db')
CACHE_TTL_DAYS     = int(os.environ.get('CACHE_TTL_DAYS', '30'))

RATE_LIMIT_SECONDS = 1.2

MIGRATION_SQL = (
    "Run in Supabase SQL Editor:\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS companies_data       JSONB;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS enriched_at          TIMESTAMPTZ;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS grade                TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS hashtags             JSONB;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS grade_justification  TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS cfo_status           TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS research_notes       JSONB;\n"
    "  -- TAL V11 (added 2026-06-09):\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS confidence_level     TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS numeric_score        INTEGER;\n"
    "  -- Fit gates (added 2026-07-16):\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS fit                  JSONB;"
)

# Post-enrichment industry exclusions — applied AFTER firmographic extraction
# to catch industries that slipped past the scrape-time text-only filter
# (e.g. "Chilean Cobalt Corp." doesn't say "Mining" in its title but its
# discovered industry was "Critical Minerals Exploration"). Substring-matched
# against the discovered industry string of the PRIMARY company.
POST_ENRICHMENT_INDUSTRY_BLOCK = [
    # Mining / metals / extractive
    'mining', 'mineral', 'minerals', 'ore', 'metals industry', 'rare earth',
    'cobalt', 'copper mining', 'gold mining', 'silver mining', 'uranium',
    'lithium mining', 'extractive', 'exploration', 'drilling', 'refining',
    'smelting', 'non-ferrous metals',
    # Heavy industry
    'steel', 'aluminum', 'foundry', 'heavy industry', 'industrial manufacturing',
    # Oil & gas / energy adjacent
    'oil & gas', 'oil and gas', 'petroleum', 'petrochemical', 'refinery',
    'lng', 'lng development',
    # Out-of-target verticals
    'hotel', 'hospitality', 'restaurant', 'qsr', 'fast food',
    'casino', 'gaming', 'sports betting',
    'engineering firm', 'civil engineering', 'construction company',
    'home construction', 'residential construction', 'construction & engineering',
    'data center', 'colocation',
    'power generation', 'utilities', 'electric utility',
    'solar farm', 'wind farm',
    # Logistics / delivery
    'logistics', 'freight', 'trucking', 'supply chain',
    'last-mile delivery', 'delivery services',
    # ── SaaS/Software + broad-healthcare blocks REMOVED 2026-07-16 ──────
    # They contradicted TARGET subverticals: fintech/insurtech/crypto
    # companies enrich to "SaaS"/"Financial Software" (Financial Services
    # targets), and FQHC/behavioral-health/hospice nonprofits enrich to
    # "Hospitals and Health Care" (Nonprofit targets). Vertical fit is now
    # decided by the ZI-subindustry ALLOWLIST gate (apply_fit_gates below) —
    # this blocklist survives only as a fast-path for unambiguous never-fits.
    'biotechnology', 'biotech',
    'pharmaceuticals', 'pharmaceutical',
    'medical devices', 'medical equipment',
    'life sciences tools', 'life sciences services',
    'clinical research',
    # Hardware/embedded (off-target)
    'computer hardware', 'computer hardware manufacturing',
    'embedded hardware', 'embedded systems',
]

# Primary-company role PRIORITY — ordered. The first matching role is the
# company the event is "about" (and the account a rep would work). Target
# is deliberately LAST: on M&A the acquirer/platform is the NetSuite
# opportunity (consolidation pain); the target is only primary when no
# better role exists. This ordered list replaces the old unordered set,
# which let "first company in the list" win arbitrarily.
PRIMARY_ROLE_ORDER = [
    'acquirer', 'portfolio company', 'hiring company', 'primary', 'target',
]
PRIMARY_ROLES = set(PRIMARY_ROLE_ORDER)  # membership checks elsewhere


def pick_primary(companies_data: list) -> dict:
    """Return the primary company per PRIMARY_ROLE_ORDER (first match wins,
    in priority order), falling back to the first listed company."""
    if not companies_data:
        return {}
    for role in PRIMARY_ROLE_ORDER:
        for c in companies_data:
            if str(c.get('role', '')).lower() == role:
                return c
    return companies_data[0]


def industry_is_blocked(industry: str):
    """Return (is_blocked, matched_keyword) for an industry string."""
    if not industry:
        return False, ''
    industry_lower = industry.lower()
    for kw in POST_ENRICHMENT_INDUSTRY_BLOCK:
        if kw in industry_lower:
            return True, kw
    return False, ''


# ── ZoomInfo subindustry taxonomy + FIT GATES ─────────────────────────────────
# Source of truth: A.J.'s "FY27 Territories.xlsx" (Subindustries sheet) —
# the 32 ZoomInfo SubIndustries mapped to the 3 NSCorp verticals. The
# firmographic LLM classifies each company into EXACTLY one of these (or
# "OTHER"), and the vertical gate is exact membership — replacing fuzzy
# free-text blocklists with a closed-set allowlist.
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

# In-band revenue segments (NetSuite up-market sweet spot). Enterprise
# (>$100M) is out of band per A.J.
IN_BAND_REVENUE = {'LMM', 'MM', 'Corp'}

# Territory — 23 US states + DC + 6 eastern Canadian provinces (FY27 xlsx;
# DC confirmed in-territory by A.J. even though absent from the sheet).
TERRITORY_STATES = {
    'ME', 'NH', 'VT', 'MA', 'RI', 'CT',                       # New England
    'NY', 'NJ', 'PA', 'DE', 'MD', 'VA', 'WV', 'DC',           # Mid-Atlantic
    'NC', 'SC', 'GA', 'FL', 'AL', 'TN', 'KY',                 # Southeast
    'OH', 'MI', 'IN',                                          # Rust Belt
    'ON', 'QC', 'NB', 'NS', 'PE', 'NL',                        # Canada (east)
}
_TERRITORY_NAME_TO_CODE = {
    'maine': 'ME', 'new hampshire': 'NH', 'vermont': 'VT',
    'massachusetts': 'MA', 'rhode island': 'RI', 'connecticut': 'CT',
    'new york': 'NY', 'new jersey': 'NJ', 'pennsylvania': 'PA',
    'delaware': 'DE', 'maryland': 'MD', 'virginia': 'VA',
    'west virginia': 'WV', 'washington dc': 'DC', 'district of columbia': 'DC',
    'north carolina': 'NC', 'south carolina': 'SC', 'georgia': 'GA',
    'florida': 'FL', 'alabama': 'AL', 'tennessee': 'TN', 'kentucky': 'KY',
    'ohio': 'OH', 'michigan': 'MI', 'indiana': 'IN',
    'ontario': 'ON', 'quebec': 'QC', 'québec': 'QC', 'new brunswick': 'NB',
    'nova scotia': 'NS', 'prince edward island': 'PE',
    'newfoundland': 'NL', 'newfoundland and labrador': 'NL',
}
# US state/CA province codes that are definitively OUTSIDE territory —
# used to distinguish "confirmed out" from "can't tell".
_ALL_STATE_CODES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
    'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV',
    'NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN',
    'TX','UT','VT','VA','WA','WV','WI','WY','DC',
    'ON','QC','NB','NS','PE','NL','BC','AB','MB','SK','YT','NT','NU',
}


def hq_territory_status(hq: str) -> str:
    """Classify a researched HQ string: 'in' | 'out' | 'unknown'.

    Handles 'City, ST', bare state/province names, and full-name tails
    ('Boston, Massachusetts'). Foreign or non-territory locations that are
    clearly identifiable → 'out'; unparseable/missing → 'unknown'."""
    if not hq or not str(hq).strip():
        return 'unknown'
    h = str(hq).strip()
    tail = h.split(',')[-1].strip() if ',' in h else h
    tail_up = tail.upper()
    # 2-letter code path
    if tail_up in TERRITORY_STATES:
        return 'in'
    if tail_up in _ALL_STATE_CODES:
        return 'out'
    # Full-name path
    tail_lo = tail.lower()
    if tail_lo in _TERRITORY_NAME_TO_CODE:
        return 'in'
    h_lo = h.lower()
    if h_lo in _TERRITORY_NAME_TO_CODE:
        return 'in'
    # Recognizable foreign markers → confidently OUT
    FOREIGN = ('germany', 'france', 'uk', 'united kingdom', 'england',
               'india', 'china', 'japan', 'australia', 'israel', 'singapore',
               'switzerland', 'netherlands', 'sweden', 'ireland', 'spain',
               'italy', 'brazil', 'mexico', 'hong kong', 'korea', 'norway',
               'denmark', 'finland', 'belgium', 'austria', 'chile',
               'british columbia', 'alberta', 'manitoba', 'saskatchewan')
    if any(f in h_lo for f in FOREIGN):
        return 'out'
    return 'unknown'


# Roles that can carry a WORKABLE account. A fitting company in one of these
# roles keeps the event alive. Previous Employer / Advisor / Partner /
# Mentioned never do — they're context, not accounts to sell into.
WORKABLE_ROLES = [
    'acquirer', 'portfolio company', 'hiring company', 'primary', 'target',
    'lead investor', 'investor',
]


def company_fit(c: dict) -> dict:
    """Fit for ONE company — the atom of the model (per A.J. 2026-07-17:
    'we should be filtering out at the company level based on industry/
    revenue/geography, not at the event level').

    Returns {'verdict': 'pass'|'fail'|'unverified', 'territory': ...,
             'revenue': ..., 'vertical': ..., 'reasons': [...]}"""
    territory = hq_territory_status(c.get('hq') or '')

    rev = (c.get('revenue') or '').strip()
    if rev in IN_BAND_REVENUE:
        revenue = 'in'
    elif rev == 'Enterprise':
        revenue = 'out'
    else:
        revenue = 'unknown'

    zi = (c.get('zi_subindustry') or '').strip() or None
    if zi and zi in ZI_SUBINDUSTRIES:
        vertical = 'in'
    elif zi and zi.upper() == 'OTHER':
        vertical = 'out'
    else:
        vertical = 'unknown'

    reasons = []
    if territory == 'out':
        reasons.append(f"HQ out of territory ({c.get('hq')})")
    if revenue == 'out':
        reasons.append('revenue Enterprise (>$100M, out of band)')
    if vertical == 'out':
        reasons.append('subindustry OTHER (not a target vertical)')

    if reasons:
        verdict = 'fail'
    elif territory == 'in' and revenue == 'in' and vertical == 'in':
        verdict = 'pass'
    else:
        verdict = 'unverified'
        for dim, val in (('territory', territory), ('revenue', revenue),
                         ('vertical', vertical)):
            if val == 'unknown':
                reasons.append(f'{dim} unverified')

    return {'verdict': verdict, 'territory': territory, 'revenue': revenue,
            'vertical': vertical, 'reasons': reasons}


def apply_fit_gates(companies_data: list) -> dict:
    """COMPANY-LEVEL fit gates (redesigned 2026-07-17).

    1. Every company gets its own fit verdict, stored on the company dict
       itself (c['fit']) so the dashboard can chip each one.
    2. The event's ACCOUNT = the best-fitting workable-role company:
       fit-passing companies first (by WORKABLE_ROLES priority), then
       unverified ones. Confirmed-out companies are never the account.
    3. Event verdict:
       - 'pass'       → the chosen account passes all three dimensions
       - 'fail'       → EVERY workable-role company confirmed-out (nothing
                        here is sellable — only then does the event die).
                        "Enterprise BigCo acquires Boston MM target" now
                        SURVIVES on the target instead of dying on the
                        acquirer.
       - 'unverified' → otherwise (kept, grade capped at B, ⚠️ flagged)

    Returns the same summary shape callers already use, plus account_name.
    """
    # Per-company fit, attached in place (flows into companies_data JSONB)
    for c in companies_data:
        c['fit'] = company_fit(c)

    role_rank = {r: i for i, r in enumerate(WORKABLE_ROLES)}
    workable = [c for c in companies_data
                if str(c.get('role', '')).lower() in role_rank]

    def _pick(cands):
        return min(cands, key=lambda c: role_rank[str(c.get('role', '')).lower()]) \
            if cands else None

    account = (_pick([c for c in workable if c['fit']['verdict'] == 'pass'])
               or _pick([c for c in workable if c['fit']['verdict'] == 'unverified'])
               or _pick(workable)
               or (companies_data[0] if companies_data else None))

    if account is None:
        return {'verdict': 'unverified', 'territory': 'unknown',
                'revenue': 'unknown', 'vertical': 'unknown',
                'zi_subindustry': None, 'primary_name': None,
                'account_name': None, 'reasons': ['no companies identified']}

    afit = account.get('fit') or company_fit(account)
    if workable and all(c['fit']['verdict'] == 'fail' for c in workable):
        verdict = 'fail'
        reasons = [f"{c.get('name')}: {'; '.join(c['fit']['reasons'])}"
                   for c in workable]
    else:
        verdict = afit['verdict']
        reasons = list(afit['reasons'])

    return {
        'verdict': verdict,
        'territory': afit['territory'],
        'revenue': afit['revenue'],
        'vertical': afit['vertical'],
        'zi_subindustry': (account.get('zi_subindustry') or None),
        'primary_name': account.get('name'),
        'account_name': account.get('name'),
        'reasons': reasons,
    }

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)


# ── LLM abstraction: local llama.cpp primary, Scout agent fallback ────────────
# Primary = the shared local llama.cpp server (Qwen3.6, non-thinking, OpenAI /v1)
# serving the whole Hermes fleet on :8091 — ONE model, no per-job reloads.
# Fallback = the Scout Hermes agent via docker exec: it uses whatever Scout is
# configured for — local today (so the fallback is cosmetic while they share the
# server), cloud Grok in future (then a real independent failover). The old
# Anthropic-vs-Ollama switch is retired; _anthropic_json pre-empts ONLY if
# ANTHROPIC_API_KEY is set (normally it is not).

def _llm_backend():
    """Label for logging only."""
    return 'anthropic' if ANTHROPIC_API_KEY else 'llamacpp'


def llm_json(prompt: str, max_tokens: int = 600) -> dict:
    """
    Send prompt to the LLM, return parsed JSON dict.
    Order: Anthropic (only if key set) -> local llama.cpp -> Scout agent fallback.
    Returns {} if all paths fail.
    """
    if ANTHROPIC_API_KEY:
        result = _anthropic_json(prompt, max_tokens)
        if result:
            return result
    result = _llamacpp_json(prompt, max_tokens)
    if result:
        return result
    log.warning('  llama.cpp empty/unreachable — falling back to Scout agent')
    return _scout_json(prompt, max_tokens)


def _anthropic_json(prompt: str, max_tokens: int) -> dict:
    system = (
        "You are a precise data extraction assistant. "
        "Always respond with valid JSON only — no markdown fences, no explanation."
    )
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      CLAUDE_MODEL,
                'max_tokens': max_tokens,
                'system':     system,
                'messages':   [{'role': 'user', 'content': prompt}],
            },
            timeout=30
        )
        resp.raise_for_status()
        text = resp.json()['content'][0]['text'].strip()
        # Strip any accidental markdown fences
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        log.warning(f'  Anthropic error: {e}')
        return {}


def _loads_json_object(text: str) -> dict:
    """Extract + parse the outermost {...} from an LLM reply. Qwen often wraps JSON in
    ```markdown fences``` or adds prose, so a bare json.loads() fails — grab the object."""
    if not text:
        return {}
    s, e = text.find('{'), text.rfind('}')
    if s == -1 or e <= s:
        return {}
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return {}


def _llamacpp_json(prompt: str, max_tokens: int) -> dict:
    """Primary: local llama.cpp OpenAI-compatible endpoint, JSON mode, non-thinking."""
    try:
        resp = requests.post(
            f'{LLAMACPP_URL}/v1/chat/completions',
            json={
                'model':           LLAMACPP_MODEL,
                'messages':        [{'role': 'user', 'content': prompt}],
                'temperature':     0.05,
                'max_tokens':      max_tokens,
                'response_format': {'type': 'json_object'},
            },
            timeout=90
        )
        resp.raise_for_status()
        return _loads_json_object(resp.json()['choices'][0]['message']['content'])
    except Exception as e:
        log.warning(f'  llama.cpp error: {e}')
        return {}


def _scout_json(prompt: str, max_tokens: int) -> dict:
    """Fallback: ask the Scout Hermes agent via docker exec (uses whatever model Scout
    runs). Cosmetic while Scout shares the local server; a real cloud failover once
    Scout moves to Grok. Best-effort JSON extraction from the agent's reply."""
    try:
        wrapped = prompt + "\n\nRespond with a single valid JSON object only — no markdown, no prose."
        proc = subprocess.run(
            ['docker', 'exec', '-e', 'HERMES_HOME=/opt/data', SCOUT_CONTAINER,
             '/opt/hermes/.venv/bin/hermes', 'chat', '-q', wrapped, '-Q'],
            capture_output=True, text=True, timeout=180
        )
        result = _loads_json_object(proc.stdout or '')
        if not result:
            log.warning('  Scout fallback returned no parseable JSON')
        return result
    except Exception as e:
        log.warning(f'  Scout fallback error: {e}')
        return {}


# ── Supabase ──────────────────────────────────────────────────────────────────

def get_supabase():
    if not SUPABASE_AVAILABLE:
        sys.exit("supabase package not installed. Run: pip install supabase")
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or os.environ.get('SUPABASE_KEY')
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
    return create_client(url, key)


def check_required_keys():
    """Fail fast with a clear message if the configured search backend isn't
    reachable. Firecrawl (default) checked at runtime per-call; Tavily checked
    here if explicitly selected."""
    if SEARCH_BACKEND == 'tavily' and not TAVILY_API_KEY:
        sys.exit(
            "SEARCH_BACKEND=tavily but TAVILY_API_KEY is empty.\n"
            "Either:\n"
            "  - Add TAVILY_API_KEY to .env (get one at https://tavily.com), OR\n"
            "  - Set SEARCH_BACKEND=firecrawl (default, uses local Firecrawl)"
        )
    if SEARCH_BACKEND == 'firecrawl':
        # Quick health probe — fail early if Firecrawl isn't running
        try:
            r = requests.get(f'{FIRECRAWL_URL}/', timeout=5)
            if r.status_code >= 400:
                sys.exit(
                    f"Firecrawl at {FIRECRAWL_URL} returned HTTP {r.status_code}. "
                    f"Is the firecrawl-api container running? "
                    f"  docker compose ps firecrawl-api-1"
                )
        except Exception as e:
            sys.exit(
                f"Cannot reach Firecrawl at {FIRECRAWL_URL}: {e}\n"
                f"  Check: docker compose ps firecrawl-api-1\n"
                f"  Or set SEARCH_BACKEND=tavily to use Tavily instead."
            )


def check_columns(client):
    exists = {}
    for col in ('companies_data', 'enriched_at', 'fit'):
        try:
            client.table('events').select(col).limit(1).execute()
            exists[col] = True
        except Exception:
            exists[col] = False
    if not exists.get('fit'):
        log.warning(
            'fit column missing — fit-gate details (⚠️ verify flags) will '
            'not persist until you run:\n'
            '  ALTER TABLE events ADD COLUMN IF NOT EXISTS fit JSONB;'
        )
    return exists


def _soft_delete(client, event_id: str, reason: str) -> None:
    """Tombstone an event: sets blocked_at (hidden from dashboard, immune to
    supabase_sync resurrection) + enriched_at (skipped by future enrichment).
    Falls back to hard DELETE only if the blocked_at column is missing."""
    payload = {
        'blocked_at':     datetime.utcnow().isoformat(),
        'blocked_reason': (reason or '')[:300],
        'enriched_at':    datetime.utcnow().isoformat(),
    }
    try:
        client.table('events').update(payload).eq('id', event_id).execute()
    except Exception as e:
        if 'does not exist' in str(e):
            log.warning(
                '    blocked_at column missing — falling back to hard DELETE '
                '(will re-appear on next sync until you run the migration SQL).'
            )
            try:
                client.table('events').delete().eq('id', event_id).execute()
            except Exception as e2:
                log.error(f'    Hard-delete fallback also failed: {e2}')
        else:
            log.error(f'    Soft-delete failed: {e}')


# ── Step 1 — Extract companies + roles from event text ───────────────────────

EXTRACT_PROMPT = '''\
You are a business analyst reading a news event. Identify every real, named \
company (business entity) mentioned and its role in the story.

Event type: {event_type}
Title: {title}
Description: {description}

Rules:
- Use the FULL official company name as it appears in the text (e.g. \
"Bluespring Wealth Partners" not just "Bluespring"; "NextEra Energy" not "NextEra").
- Only include real, named businesses — not people, government bodies, or vague terms.
- NEVER include the news outlet, publication, wire service, or website that \
PUBLISHED the story (e.g. "appeared first on PYMNTS.com", "reports TechCrunch", \
"— The Globe and Mail"). Publishers are not participants in the event.
- Assign a specific role using these labels:
    M&A events:           "Acquirer", "Target", "Advisor"
    Funding events:       "Portfolio Company", "Lead Investor", "Investor"
    CFO / exec hire:      "Hiring Company", "Previous Employer"
    Other:                "Primary", "Partner", "Mentioned"
- Maximum 5 companies.
- If no named companies can be identified, return {{"companies": []}}.

Return ONLY a JSON object with one key. Each company gets a "descriptor" —
2-4 words from the article describing what it does (e.g. "trading platform",
"venture capital firm", "insurance brokerage"); "" if the article doesn't say:
{{"companies": [{{"name": "Full Company Name", "role": "Role", "descriptor": "what it does"}}, ...]}}'''


# News outlets/wire services that occasionally leak into company extraction
# ("appeared first on PYMNTS.com", "— The Globe and Mail"). Deterministic
# backstop behind the prompt rule; the source-domain check below catches the
# general case even for outlets not on this list.
_PUBLISHER_NAMES = {
    'pymnts', 'pymnts.com', 'techcrunch', 'reuters', 'bloomberg news',
    'business wire', 'businesswire', 'pr newswire', 'prnewswire',
    'globe newswire', 'globenewswire', 'the globe and mail', 'globe and mail',
    'yahoo finance', 'yahoo news', 'google news', 'associated press',
    'vc news daily', 'crunchbase news', 'axios', 'forbes', 'fortune',
    'the wall street journal', 'wall street journal', 'financial times',
    'financial post', 'cnbc', 'fox business', 'business insider',
    'insurance journal', 'wealthmanagement.com', 'pehub', 'buyouts',
}


def _is_publisher(name: str, source_url: str) -> bool:
    """True when an extracted 'company' is actually the article's publisher.
    Two checks: (1) known-outlet name list; (2) name ≈ the article's own
    domain (generic — catches any outlet: 'PYMNTS.com' on a pymnts.com URL)."""
    n = (name or '').strip().lower().rstrip('.')
    if not n:
        return False
    if n in _PUBLISHER_NAMES:
        return True
    try:
        from urllib.parse import urlparse
        host = (urlparse(source_url or '').netloc or '').lower()
        host = host[4:] if host.startswith('www.') else host
        if host:
            stem = host.rsplit('.', 1)[0]          # pymnts.com → pymnts
            n_stem = n[:-4] if n.endswith('.com') else n
            # Space/punct-insensitive: "Johnson City Press" vs
            # johnsoncitypress.com; "The Globe and Mail" vs theglobeandmail
            squish = lambda s: ''.join(ch for ch in s if ch.isalnum())
            if n_stem and (n_stem == stem or n_stem == host or n == host
                           or (squish(n_stem) and squish(n_stem) == squish(stem))):
                return True
    except Exception:
        pass
    return False


def extract_event_companies(event: dict) -> list:
    prompt = EXTRACT_PROMPT.format(
        event_type=event.get('event_type', ''),
        title=event.get('title', '')[:220],
        description=(event.get('description', '') or '')[:700],
    )
    data = llm_json(prompt, max_tokens=400)
    raw = data.get('companies', [])
    src_url = event.get('source_url') or event.get('url') or ''
    valid = []
    for c in raw:
        name = (c.get('name') or '').strip()
        role = (c.get('role') or 'Mentioned').strip()
        if not name or len(name) <= 1 or name.lower() in (
            'unknown', 'nan', 'none', ''
        ):
            continue
        if _is_publisher(name, src_url):
            log.info(f'    (dropping publisher "{name}" from companies)')
            continue
        desc = (c.get('descriptor') or '').strip()[:60]
        valid.append({'name': name, 'role': role, 'descriptor': desc})
    return valid[:5]


# ── Step 2 — Web search (Firecrawl primary, Tavily fallback, persistent cache) ─

def _build_search_query(company_name: str, industry_hint: str = '') -> str:
    """Build the firmographic search query — shared by all backends so results
    are equivalent regardless of which provider answers."""
    hint = f' {industry_hint}' if industry_hint else ''
    return (
        f'"{company_name}"{hint} company official website headquarters '
        f'employees annual revenue size'
    )


def _firecrawl_search(company_name: str, industry_hint: str = '') -> dict:
    """Search via local self-hosted Firecrawl. Returns dict in the Tavily
    response shape so downstream code doesn't change. Free, unlimited,
    private. Default backend."""
    query = _build_search_query(company_name, industry_hint)
    try:
        resp = requests.post(
            f'{FIRECRAWL_URL}/v1/search',
            json={'query': query, 'limit': 6},
            timeout=25  # Firecrawl can be slower than Tavily on first cold-cache
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get('success'):
            log.warning(f'  Firecrawl returned success=false for "{company_name}"')
            return {}
        # Adapt Firecrawl response → Tavily-shaped envelope
        results = data.get('data') or []
        return {
            'answer':  '',  # Firecrawl doesn't summarize like Tavily; leave blank
            'results': [
                {
                    'title':   r.get('title', ''),
                    'url':     r.get('url', ''),
                    # Firecrawl uses 'description'; Tavily extractor reads 'content'
                    'content': (r.get('description') or '')[:400],
                }
                for r in results
            ]
        }
    except Exception as e:
        log.warning(f'  Firecrawl error for "{company_name}": {e}')
        return {}


def _searxng_search(company_name: str, industry_hint: str = '') -> dict:
    """Search via the local SearXNG metasearch instance (the same one the
    Hermes fleet uses, :8888). Free, no API quota. Independent of
    Firecrawl's own search path, and rotates across multiple upstream
    engines — when Firecrawl's backend is burst-throttled, one of
    SearXNG's engines is often still answering. Returns the Tavily
    envelope shape."""
    query = _build_search_query(company_name, industry_hint)
    try:
        resp = requests.get(
            f'{SEARXNG_URL}/search',
            params={'q': query, 'format': 'json'},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get('results') or []
        return {
            'answer': '',
            'results': [
                {
                    'title':   r.get('title', ''),
                    'url':     r.get('url', ''),
                    'content': (r.get('content') or '')[:400],
                }
                for r in results[:6]
            ],
        }
    except Exception as e:
        log.warning(f'  SearXNG error for "{company_name}": {e}')
        return {}


def _google_cse_search(company_name: str, industry_hint: str = '') -> dict:
    """Google Custom Search JSON API — dormant until GOOGLE_CSE_KEY +
    GOOGLE_CSE_CX are set in the environment. Free tier: 100 queries/DAY
    (resets daily — unlike Tavily's monthly cliff, a burned day only costs
    that day). Server-side quota, so it keeps working when the Mac's IP is
    CAPTCHA'd by the scraping backends. Over-quota returns HTTP 429 →
    empty dict → dispatcher moves on."""
    key = os.environ.get('GOOGLE_CSE_KEY')
    cx  = os.environ.get('GOOGLE_CSE_CX')
    if not key or not cx:
        return {}
    query = _build_search_query(company_name, industry_hint)
    try:
        resp = requests.get(
            'https://www.googleapis.com/customsearch/v1',
            params={'key': key, 'cx': cx, 'q': query, 'num': 6},
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get('items') or []
        return {
            'answer': '',
            'results': [
                {
                    'title':   i.get('title', ''),
                    'url':     i.get('link', ''),
                    'content': (i.get('snippet') or '')[:400],
                }
                for i in items
            ],
        }
    except Exception as e:
        log.warning(f'  Google CSE error for "{company_name}": {e}')
        return {}


def _tavily_search(company_name: str, industry_hint: str = '') -> dict:
    """Search Tavily — kept as fallback when SEARCH_BACKEND='tavily' OR
    Firecrawl is unreachable. Costs quota; use sparingly."""
    if not TAVILY_API_KEY:
        return {}
    query = _build_search_query(company_name, industry_hint)
    try:
        resp = requests.post(
            'https://api.tavily.com/search',
            json={
                'api_key':        TAVILY_API_KEY,
                'query':          query,
                'max_results':    6,
                'search_depth':   'basic',
                'include_answer': True,
            },
            timeout=20
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f'  Tavily error for "{company_name}": {e}')
        return {}


# ── Persistent search cache ─────────────────────────────────────────────────

def _cache_key(company_name: str, industry_hint: str = '') -> str:
    """Normalized cache key — case + whitespace insensitive."""
    return f'{(company_name or "").strip().lower()}||{(industry_hint or "").strip().lower()}'


def _cache_get(company_name: str, industry_hint: str = '') -> Optional[dict]:
    """Look up cached search results. Returns None if not cached or stale."""
    try:
        import sqlite3
        conn = sqlite3.connect(CACHE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS firmographic_cache (
                cache_key TEXT PRIMARY KEY,
                results_json TEXT,
                cached_at TEXT
            )
        ''')
        cur.execute(
            'SELECT results_json, cached_at FROM firmographic_cache WHERE cache_key = ?',
            (_cache_key(company_name, industry_hint),)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        cached_at = datetime.fromisoformat(row['cached_at'])
        if (datetime.utcnow() - cached_at).days >= CACHE_TTL_DAYS:
            return None  # stale
        return json.loads(row['results_json'])
    except Exception as e:
        log.debug(f'  Cache lookup failed: {e}')
        return None


def _cache_set(company_name: str, industry_hint: str, results: dict) -> None:
    """Persist search results to the cache."""
    if not results or not results.get('results'):
        return  # don't cache empty/failed results
    try:
        import sqlite3
        conn = sqlite3.connect(CACHE_DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS firmographic_cache (
                cache_key TEXT PRIMARY KEY,
                results_json TEXT,
                cached_at TEXT
            )
        ''')
        cur.execute(
            'INSERT OR REPLACE INTO firmographic_cache '
            '(cache_key, results_json, cached_at) VALUES (?, ?, ?)',
            (
                _cache_key(company_name, industry_hint),
                json.dumps(results),
                datetime.utcnow().isoformat(),
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f'  Cache store failed: {e}')


# Per-run counters for the actual backend each search hit. Reset to 0
# at the start of every enrich_events() / regrade_only_events() run.
# The main loop reads these when printing the final summary.
SEARCH_COUNTS: Dict[str, int] = {'cache': 0, 'firecrawl': 0, 'searxng': 0,
                                 'google_cse': 0, 'tavily': 0}


def reset_search_counts() -> None:
    """Zero the SEARCH_COUNTS dict — call at the start of each run."""
    for k in SEARCH_COUNTS:
        SEARCH_COUNTS[k] = 0


# Monthly Tavily budget guard — free tier is 1000 calls/month. The 2026-07-16
# overnight bulk run fired 1616 fallback calls and exhausted the quota mid-run
# (late calls returned empty → thinner enrichment). We now stop falling back
# once the month's budget is nearly spent, preserving headroom for the
# genuinely-needed lookups later in the month.
TAVILY_MONTHLY_BUDGET = int(os.environ.get('TAVILY_MONTHLY_BUDGET', '900'))


def _tavily_month_count(increment: bool = False) -> int:
    """Read (and optionally increment) this calendar month's Tavily call
    count, persisted in the local cache DB so it survives restarts."""
    month = datetime.utcnow().strftime('%Y-%m')
    try:
        conn = sqlite3.connect(CACHE_DB_PATH)
        cur = conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS tavily_usage '
                    '(month TEXT PRIMARY KEY, calls INTEGER)')
        if increment:
            cur.execute('INSERT INTO tavily_usage (month, calls) VALUES (?, 1) '
                        'ON CONFLICT(month) DO UPDATE SET calls = calls + 1',
                        (month,))
            conn.commit()
        cur.execute('SELECT calls FROM tavily_usage WHERE month = ?', (month,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0  # fail open — guard is best-effort


def tavily_search(company_name: str, industry_hint: str = '') -> dict:
    """Public search interface. Despite the legacy name, dispatches to the
    configured SEARCH_BACKEND (firecrawl by default) with persistent caching.
    Function name kept for backwards-compat with the rest of the file."""
    # Cache check first — saves cost regardless of backend
    cached = _cache_get(company_name, industry_hint)
    if cached:
        SEARCH_COUNTS['cache'] += 1
        return cached

    # Backend dispatch
    if SEARCH_BACKEND == 'tavily':
        SEARCH_COUNTS['tavily'] += 1
        _tavily_month_count(increment=True)
        results = _tavily_search(company_name, industry_hint)
    elif SEARCH_BACKEND == 'firecrawl':
        SEARCH_COUNTS['firecrawl'] += 1
        results = _firecrawl_search(company_name, industry_hint)
        if not results or not results.get('results'):
            # Empty Firecrawl during bulk runs is usually TRANSIENT upstream
            # rate-limiting (its search backend throttling a burst). One
            # short wait + retry recovers most of them for free — verified:
            # the same queries succeed seconds later.
            time.sleep(2.5)
            SEARCH_COUNTS['firecrawl'] += 1
            results = _firecrawl_search(company_name, industry_hint)
        # SearXNG fallback — free, quota-less, different upstream engines.
        # Sits BEFORE Tavily so paid quota is only touched when both local
        # backends come up empty.
        if not results or not results.get('results'):
            log.info('  → Firecrawl empty, falling back to SearXNG')
            SEARCH_COUNTS['searxng'] += 1
            results = _searxng_search(company_name, industry_hint)
        # Google CSE fallback — dormant until GOOGLE_CSE_KEY/GOOGLE_CSE_CX
        # are configured. API-quota based (100/day, daily reset), so it
        # works even when the Mac's IP is throttled by every scraper.
        if ((not results or not results.get('results'))
                and os.environ.get('GOOGLE_CSE_KEY')):
            log.info('  → SearXNG empty too, falling back to Google CSE')
            SEARCH_COUNTS['google_cse'] += 1
            results = _google_cse_search(company_name, industry_hint)
        # Tavily fallback — only if configured AND monthly budget remains
        if (not results or not results.get('results')) and TAVILY_API_KEY:
            used = _tavily_month_count()
            if used >= TAVILY_MONTHLY_BUDGET:
                log.info(f'  → Firecrawl empty; Tavily budget spent '
                         f'({used}/{TAVILY_MONTHLY_BUDGET} this month) — skipping fallback')
            else:
                log.info('  → Firecrawl empty, falling back to Tavily')
                SEARCH_COUNTS['tavily'] += 1
                _tavily_month_count(increment=True)
                results = _tavily_search(company_name, industry_hint)
    else:
        log.warning(f'  Unknown SEARCH_BACKEND={SEARCH_BACKEND!r}, defaulting to firecrawl')
        SEARCH_COUNTS['firecrawl'] += 1
        results = _firecrawl_search(company_name, industry_hint)

    if results and results.get('results'):
        _cache_set(company_name, industry_hint, results)
    return results


# ── Step 3 — Extract firmographics from search results ───────────────────────

FIRMOGRAPHIC_PROMPT = '''\
Extract firmographic data for a specific company from the search results below.

Target company: "{company_name}"
Industry context from the news event: "{industry_hint}"

THE ARTICLE ITSELF (primary source — a press release dateline like
"NEW YORK, NY" is VALID evidence for HQ, and the article's description
of what the company does is VALID evidence for industry/zi_subindustry
classification. Use it, especially when search results are thin):
{article_context}

Search results:
{results_text}

CRITICAL: Only extract data that clearly matches "{company_name}" in the context \
of "{industry_hint}". If the results describe a different company with a similar \
name (wrong country, wrong industry, different sector), return null for ALL \
fields rather than guessing.

For REVENUE, be especially careful — only extract if a source explicitly states \
revenue (Crunchbase, Bloomberg, IPO filings, press releases, official company \
statements). NEVER guess from employee count or industry alone. If revenue \
is not explicitly stated, return null.

Return ONLY a JSON object (no markdown, no explanation) with these keys \
(null if unknown or ambiguous):
{{
  "url":      "official website URL (https://...) or null",
  "industry": "Industry describing what the company OPERATES IN — not its role \
in any transaction. Examples of operating industries: 'Wealth Management', \
'Commercial Banking', 'Insurance', 'Auto Dealer', 'Charitable Foundation', \
'Museum', 'Auto Repair', 'Real Estate Brokerage'. CRITICAL — only return \
'Private Equity' / 'Venture Capital' / 'Investment Banking' if the company \
ITSELF is a PE/VC/IB firm whose primary business is investing or advising. \
An acquirer or investor in a deal is NOT a PE firm unless their core \
business is investing — an electronics company that acquires another \
electronics company has industry 'Electronics' (or null), NOT 'Private \
Equity'. Be precise (not generic like 'Technology' or 'Services'). Return \
null if uncertain.",
  "zi_subindustry": "Classify the company into EXACTLY ONE of the following \
ZoomInfo subindustries, or 'OTHER' if none genuinely fits. Do NOT force a \
fit — a software company is OTHER, a manufacturer is OTHER, a biotech is \
OTHER. Choose from: 'Banking', 'Credit Cards & Transaction Processing', \
'Debt Collection', 'Holding Companies & Conglomerates', 'Insurance', \
'Investment Banking', 'Lending & Brokerage', 'Venture Capital & Private \
Equity', 'Blood & Organ Banks', 'Childcare', 'Colleges & Universities', \
'Cultural & Informational Centers', 'K-12 Schools', 'Libraries', \
'Membership Organizations', 'Museums & Art Galleries', 'Non-Profit & \
Charitable Organizations', 'Non-Profit Organizations & Charitable \
Foundations', 'Performing Arts Theaters', 'Religious Organizations', \
'Training', 'Zoos & National Parks', 'Auctions', 'Automobile Dealers', \
'Automotive Service & Collision Repair', 'Barber Shops & Beauty Salons', \
'Cleaning Services', 'Consumer Services', 'Funeral Homes & Funeral Related \
Services', 'Photography Studio', 'Real Estate', 'Repair Services'. \
Guidance: fintech/payments → 'Credit Cards & Transaction Processing'; \
RIA/wealth/asset managers/family offices → 'Investment Banking' or \
'Venture Capital & Private Equity' as fits; mortgage/consumer lenders → \
'Lending & Brokerage'; credit unions → 'Banking'; charities/foundations → \
one of the Non-Profit options. CRITICAL: 'Venture Capital & Private \
Equity' applies ONLY when the company ITSELF is an investment firm that \
manages funds and invests in other companies. A startup that RAISED \
venture funding is NOT 'Venture Capital & Private Equity' — classify it \
by what it actually sells (a funded healthcare startup is OTHER, a funded \
robotics company is OTHER, a funded insurtech is 'Insurance'). Same for \
acquirers: an operating company that acquires another is classified by \
its OWN business, not as PE. Use null ONLY if the company cannot be \
identified at all.",
  "size":     "one of: '1-50', '51-200', '201-500', '501-1000', \
'1001-5000', '5001-10000', '10000+', or null",
  "revenue":  "STRICT SEGMENT. Must be EXACTLY one of: \
'LMM' (Lower Mid-Market, <$10M), 'MM' (Mid-Market, $10M-$20M), \
'Corp' (Corporate, $20M-$100M), 'Enterprise' (>$100M), or null. \
DO NOT return dollar amounts — map them to the segment they fall into: \
$5M → 'LMM', $15M → 'MM', $50M → 'Corp', $500M → 'Enterprise', \
$1.5B → 'Enterprise'. Use null if revenue is not explicitly stated.",
  "revenue_source": "The full URL of the search result where the revenue \
figure was found (e.g. 'https://www.crunchbase.com/organization/acme'). \
Must be one of the URLs in the search results above. null if revenue is null.",
  "hq":       "City, ST abbreviation (e.g. 'Boston, MA' or 'Toronto, ON'), \
US/Canada only unless clearly elsewhere — or null",
  "linkedin": "full https://www.linkedin.com/company/... URL or null"
}}'''


def enrich_one_company(company_name: str, industry_hint: str = '',
                       article_context: str = '') -> dict:
    empty = {'url': None, 'industry': None, 'zi_subindustry': None,
             'size': None, 'revenue': None,
             'revenue_source': None, 'hq': None, 'linkedin': None}

    search = tavily_search(company_name, industry_hint)
    if not search.get('results') and not (article_context or '').strip():
        return empty  # nothing to extract from at all

    lines = []
    if search.get('answer'):
        lines.append(f"Summary: {search['answer']}\n")
    for r in search.get('results', [])[:5]:
        lines.append(
            f"- {r.get('title','')}\n"
            f"  {r.get('url','')}\n"
            f"  {(r.get('content','') or '')[:320]}\n"
        )

    prompt = FIRMOGRAPHIC_PROMPT.format(
        company_name=company_name,
        industry_hint=industry_hint or 'unknown',
        article_context=(article_context or '(not provided)').strip(),
        results_text='\n'.join(lines).strip()
    )
    data = llm_json(prompt, max_tokens=550)

    # Only keep revenue_source if revenue itself was extracted (no point
    # citing a URL for a null revenue)
    revenue       = data.get('revenue')        or None
    revenue_src   = data.get('revenue_source') or None
    if not revenue:
        revenue_src = None

    # Validate the ZI classification against the closed set — anything the
    # model invents outside the taxonomy is coerced to None (unknown), and
    # 'OTHER' is preserved as an explicit out-of-vertical verdict.
    zi_raw = (data.get('zi_subindustry') or '').strip()
    if zi_raw in ZI_SUBINDUSTRIES or zi_raw.upper() == 'OTHER':
        zi_val = 'OTHER' if zi_raw.upper() == 'OTHER' else zi_raw
    else:
        zi_val = None

    return {
        'url':            data.get('url')      or None,
        'industry':       data.get('industry') or None,
        'zi_subindustry': zi_val,
        'size':           data.get('size')     or None,
        'revenue':        revenue,
        'revenue_source': revenue_src,
        'hq':             data.get('hq')       or None,
        'linkedin':       data.get('linkedin') or None,
    }


# ── Step 4 — TAL grading (TAL V11 system, adapted for our pipeline flow) ────
#
# V11 replaces V10.2's "count hashtags + triggers" with a POINT-BASED scoring
# rubric. Each hashtag has explicit points; sum = numeric_score; score maps
# to grade (A=8+, B=5-7, C=2-4, D=0-1). New fields: confidence_level,
# numeric_score. New hashtag: #NewController.
#
# Implementation philosophy: LLM picks the HASHTAGS (creative judgment task),
# code computes SCORE + GRADE (deterministic math). The LLM is unreliable
# at arithmetic — it routinely uses wrong point values or misapplies grade
# thresholds. By separating these, we get reliable correctness on the
# scoring even when the model gets tired/confused.
#
# Solo #NewCFO = 5 points = Grade B (intentional — A.J.: "CFOs are HUGE").

# Point values per V11 — single source of truth for code + prompt consistency
TAL_V11_HASHTAG_POINTS = {
    # HIGH-INTENT TRIGGERS
    '#NewCFO':        5,
    '#NewController': 3,
    '#Funding':       3,
    '#PEBacked':      3,
    '#Acquisitions':  3,
    '#FormerUser':    3,
    '#PrevConvo':     3,
    # COMPLEXITY SIGNALS
    '#HyperGrowth':       2,
    '#100EE':             2,
    '#Locations':         2,
    '#Entities':          2,
    '#HoldCo':            2,
    '#Global':            2,
    '#Franchisor':        2,
    '#Franchisee':        2,
    '#Legacy':            2,
    '#AssetManagerScale': 2,   # added 2026-07-16 per A.J.'s latest TAL rubric
}

HIGH_INTENT_HASHTAGS = {
    '#NewCFO', '#NewController', '#Funding', '#PEBacked',
    '#Acquisitions', '#FormerUser', '#PrevConvo',
}


def _compute_v11_grade(hashtags: list, confidence: str):
    """Deterministically compute (numeric_score, grade) from hashtag list per
    V11 rules. Overrides the LLM's own score/grade — the LLM is unreliable
    at arithmetic and threshold-application.

    Returns (score: int, grade: str).

    Grade rules per V11:
      1. Score = sum of hashtag points.
      2. Grade A requires (high-intent trigger present) AND (score 8+).
      3. Without any high-intent trigger, grade cannot exceed C — UNLESS
         complexity-only score is 8+ (high-complexity exception → B).
      4. Low confidence caps grade at C regardless of score.
    """
    if not hashtags:
        return 0, 'D'

    score = sum(TAL_V11_HASHTAG_POINTS.get(h, 0) for h in hashtags)
    has_high_intent = any(h in HIGH_INTENT_HASHTAGS for h in hashtags)
    complexity_score = sum(
        TAL_V11_HASHTAG_POINTS.get(h, 0)
        for h in hashtags if h not in HIGH_INTENT_HASHTAGS
    )

    if has_high_intent:
        if score >= 8:   grade = 'A'
        elif score >= 5: grade = 'B'
        elif score >= 2: grade = 'C'
        else:            grade = 'D'
    else:
        # No high-intent trigger: standard mapping caps at C, except
        # high-complexity exception (8+ complexity → B)
        if complexity_score >= 8: grade = 'B'
        elif score >= 2:          grade = 'C'
        else:                     grade = 'D'

    # Low-confidence cap (rule 4). Unparseable/missing confidence is treated
    # as Low — previously None slipped past the exact-'Low' check and junk-
    # confidence events could keep Grade A (audit 2026-07-16).
    if confidence not in ('High', 'Medium') and grade in ('A', 'B'):
        grade = 'C'

    return score, grade

TAL_GRADING_PROMPT = '''\
You are a lead-grading assistant for Oracle NetSuite sales applying TAL V11. \
Be CONSERVATIVE and evidence-driven — never invent missing information.

EVENT
Title: {title}
Type: {event_type}
URL: {article_url}
Description: {description}

COMPANIES (pre-researched firmographics)
{companies_block}

ADDITIONAL RESEARCH EVIDENCE (funding history / nonprofit 990 / AUM probes —
may be empty; treat as authoritative when present, esp. for #Funding
recency, #AssetManagerScale, and nonprofit revenue):
{extra_evidence}

CORE RULES
- Prefer evidence over assumptions.
- If a fact cannot be verified from the input above, treat it as missing.
- Missing evidence lowers confidence but does not block grading.
- Use "Unable to Grade" ONLY if the company cannot be reasonably identified.
- Evaluate THIS account: **{account_name}** — the workable company chosen by \
the fit gates. Grade ONLY this company. Do not inherit attributes from the \
other companies in this event.

HASHTAGS — use ONLY these and ONLY when evidence supports them. Each has \
a fixed point value. Sum all applicable points = numeric_score.

HIGH-INTENT TRIGGERS:
- **#NewCFO (+5)** — CFO or CFO-equivalent (Chief Financial Officer, VP \
Finance, Head of Finance, Director of Finance, Chief Financial) hired \
within last 18 months. Apply ONLY if event_type=cfo_hire OR title/description \
states a new CFO/VP Finance/Director Finance hire. NOT for Controllers — \
use #NewController instead. NEVER apply to M&A deals, material agreements, \
funding rounds, or Board of Directors changes — those events do NOT imply \
a new CFO, and there is no such thing as a "highest-value trigger applied \
by default". Board of Directors appointments/elections/departures are NOT \
finance-leader hires: directors are not involved in ERP decisions, so a \
board change earns NEITHER #NewCFO nor #NewController. \
NOTE: a solo #NewCFO (no other hashtag) = 5 points = Grade B — this is \
intentional: a new CFO is the single highest-value NetSuite sales trigger.
- **#NewController (+3)** — Controller, VP Accounting, or Chief Accounting \
Officer hired within last 18 months. Use this INSTEAD of #NewCFO when the \
role is Controller / VP Accounting / Chief Accounting (not CFO-track).
- **#Funding (+3)** — Verified funding/financing/recapitalization within \
last 18 months for a FOR-PROFIT company. Apply if event_type=funding. DO \
NOT apply to nonprofit grants/donations or companies BEING acquired.
- **#PEBacked (+3)** — Verified PE ownership/sponsorship. The investor \
must be a PE firm (Bain, KKR, Blackstone, Carlyle, Apollo, TPG, Vista, \
Thoma Bravo, Nautic, EIG, Roark, Hellman & Friedman, Silver Lake, etc.) — \
NOT a VC firm (General Catalyst, Sequoia, a16z, etc. = VC, not PE).
- **#Acquisitions (+3)** — Acquisition activity within last 36 months. \
Apply if event_type=merger_acquisition AND the company being graded has \
role "Acquirer". DO NOT apply for Target role.
- **#FormerUser (+3)** — Verified former Oracle/NetSuite customer. SKIP \
unless EXPLICITLY mentioned in the input.
- **#PrevConvo (+3)** — Verified prior sales conversation/demo/opportunity. \
SKIP unless EXPLICITLY mentioned in the input.

COMPLEXITY SIGNALS:
- **#HyperGrowth (+2)** — Documented rapid growth, major hiring, strong \
YoY growth, Inc. 5000, or major expansion. DO NOT apply for routine \
funding rounds.
- **#100EE (+2)** — VERIFIED 100+ employees. Firmographic size buckets \
'201-500' and larger qualify automatically. A '51-200' bucket alone does \
NOT verify 100+ — apply only if other evidence (headcount figure, \
LinkedIn count) confirms ≥100.
- **#Locations (+2)** — Verified multiple offices, stores, branches, \
campuses, or facilities. DO NOT apply for HAVING a single HQ city.
- **#Entities (+2)** — Verified multiple subsidiaries/brands/legal \
entities/business units.
- **#HoldCo (+2)** — Holding company, parent company, platform company, \
or multi-brand operator (name contains "Holdings", industry is "Holding \
Companies & Conglomerates", or evidence of multiple operating \
subsidiaries).
- **#Global (+2)** — Verified operations or offices in MULTIPLE COUNTRIES. \
An out-of-US/Canada HQ alone is NOT #Global (territory fit is handled \
elsewhere — do not award points for foreign HQ).
- **#Franchisor (+2)** — Company sells or operates franchises.
- **#Franchisee (+2)** — Company operates under another franchise brand.
- **#Legacy (+2)** — Verified legacy ERP/accounting system in use \
(QuickBooks, Sage 50, Dynamics GP, etc.). SKIP unless EXPLICITLY mentioned.
- **#AssetManagerScale (+2)** — Verified asset-manager scale. For PE \
firms: requires $1B+ AUM/AUA AND 2+ funds. For VC, RIA, wealth manager, \
family office, REIT, or other asset managers, use AUM/AUA directionally: \
<$250M usually too early; $250M-$500M needs clear complexity; $500M-$1B \
needs other supporting signals; $1B+ is defensible; $5B+ is strong. More \
funds/entities/vehicles/portfolio investments improves fit. Evidence \
REQUIRED (AUM figure with source) — never infer scale from brand fame.

When in doubt, DROP the hashtag. Use as many approved hashtags as the \
evidence supports — there is no maximum. Company-owned websites do NOT \
count as independent validation (they can evidence locations/entities \
facts, but confidence "High" requires at least one non-company source).

GRADE MAPPING (based on numeric_score):
- **A = 8+**
- **B = 5-7**
- **C = 2-4**
- **D = 0-1**

GRADE RULES (these can OVERRIDE the score mapping):
1. Grade A REQUIRES at least one verified high-intent trigger AND score 8+.
2. Without any high-intent trigger, grade cannot exceed C — EXCEPT: if \
complexity-only score is 8+, grade can be B (high-complexity exception).
3. Low confidence cannot exceed C regardless of score.

CONFIDENCE LEVEL:
- **High** — strong evidence from multiple reliable sources in the input.
- **Medium** — partial evidence or moderate estimation required.
- **Low** — weak/conflicting evidence, limited validation, or CFO status \
not verifiable.

CFO STATUS:
- "New" if event_type=cfo_hire OR title/description mentions hiring a \
CFO/Controller/VP Finance/Director Finance/Chief Accounting.
- "Unable to verify" otherwise.

For research_notes, use ONLY URLs that appear in the input above (article \
URL, company URLs, revenue_source URLs). NEVER invent sources. Cap total \
research_notes content at <1000 characters.

Cap grade_justification at <1000 characters. The justification must show \
the math: which hashtags applied, points each, total score, how that maps \
to the grade. Write it as a CLEAN final summary (2-4 sentences) — NEVER \
include deliberation, self-correction, or phrases like "Wait", "Re-reading \
the rule", "Why B?". Decide first, then write the justification once.

OUTPUT — return ONLY valid JSON (no markdown fences, no preamble):
{{
  "grade": "A|B|C|D|Unable to Grade",
  "confidence": "High|Medium|Low",
  "numeric_score": <integer sum of hashtag points>,
  "hashtags": ["#X", "#Y", ...],
  "cfo_status": "New|Unable to verify",
  "grade_justification": "<1000 chars — show the math (which hashtags + points + total + grade rule applied)",
  "research_notes": [
    {{"finding": "what was found", "source_url": "URL from input"}},
    {{"finding": "...", "source_url": "..."}}
  ]
}}'''


def _build_companies_block(companies_data: list) -> str:
    """Format the enriched companies into a structured block for the prompt."""
    lines = []
    for c in companies_data:
        lines.append(
            f"  - Name: {c.get('name')}\n"
            f"    Role: {c.get('role')}\n"
            f"    URL: {c.get('url')}\n"
            f"    Industry: {c.get('industry')}\n"
            f"    Size: {c.get('size')}, Revenue: {c.get('revenue')}, "
            f"HQ: {c.get('hq')}\n"
            f"    Revenue Source: {c.get('revenue_source') or 'n/a'}"
        )
    return '\n'.join(lines)


# Finance leadership roles that should always trigger a minimum Grade B per
# user requirement: "new CFOs/Controllers/VPs of Finance are VERY high value
# and probably more valuable than any other trigger".
FINANCE_LEADERSHIP_PATTERNS = [
    'cfo', 'chief financial officer', 'chief financial',
    'controller', 'corporate controller',
    'vp accounting', 'vp of accounting', 'vice president of accounting',
    'vp finance', 'vp of finance', 'vice president finance',
    'vice president of finance', 'head of finance',
    'director of finance', 'finance director',
    'chief accounting officer', 'chief accountant',
]


def _has_finance_leadership_trigger(event: dict) -> bool:
    """True if the event title or description indicates hiring a finance
    leadership role (CFO, Controller, VP Finance, etc.)."""
    text = ' '.join([
        (event.get('title') or ''),
        (event.get('description') or ''),
    ]).lower()
    if not text.strip():
        return False
    if event.get('event_type') == 'cfo_hire':
        return True
    # Check for finance leadership keywords combined with hiring verbs
    HIRE_VERBS = ('appoints', 'names', 'hires', 'welcomes', 'taps',
                  'joins as', 'promoted to', 'elevated to', 'hiring')
    has_hire = any(v in text for v in HIRE_VERBS) or 'hire' in text
    has_role = any(p in text for p in FINANCE_LEADERSHIP_PATTERNS)
    return has_hire and has_role


_CONTROLLER_PATTERNS = ('controller', 'vp accounting', 'vp of accounting',
                        'vice president of accounting', 'corporate controller')
_CFO_EQUIV_PATTERNS = ('cfo', 'chief financial officer', 'chief financial',
                       'vp finance', 'vp of finance', 'vice president finance',
                       'vice president of finance', 'head of finance',
                       'director of finance', 'finance director',
                       'chief accounting officer', 'chief accountant')

# Board-of-directors changes are NOT triggers (A.J. 2026-07-21: directors
# aren't involved in ERP decisions like CFOs/Controllers are).
_BOARD_PATTERNS = ('board of directors', 'to the board', 'to its board',
                   'board member', 'board seat', 'joins board',
                   'joins the board', 'named to board', 'elected director',
                   'board appointment', 'board chair')


def _board_only_event(event: dict) -> bool:
    """True when an executive_hire event is purely a board-of-directors
    change — no CFO/Controller/finance-leader involvement. These are noise:
    directors don't drive ERP decisions, so the event gets tombstoned
    instead of enriched/graded."""
    if event.get('event_type') != 'executive_hire':
        return False
    text = ' '.join([(event.get('title') or ''),
                     (event.get('description') or '')]).lower()
    if not any(p in text for p in _BOARD_PATTERNS):
        return False
    return not (any(p in text for p in _CFO_EQUIV_PATTERNS)
                or any(p in text for p in _CONTROLLER_PATTERNS))


def _finance_role(event: dict):
    """Distinguish the finance role being hired: 'cfo' | 'controller' | None.

    Why it matters: the rubric awards #NewCFO(+5) vs #NewController(+3).
    The old code relabeled Controller hires to event_type=cfo_hire, which
    then triggered #NewCFO on regrade — a +5/+3 double-count inflation loop
    (audit 2026-07-16). Only true CFO-equivalents get relabeled now."""
    if not _has_finance_leadership_trigger(event):
        return None
    text = ' '.join([(event.get('title') or ''),
                     (event.get('description') or '')]).lower()
    # Controller checked FIRST — "Controller" text must not fall through to
    # cfo via the broader CFO-equivalent patterns.
    if any(p in text for p in _CONTROLLER_PATTERNS):
        # A release can mention both ("Controller promoted to CFO") — the
        # destination role wins.
        if any(p in text for p in ('as cfo', 'new cfo', 'to cfo',
                                   'chief financial officer')):
            return 'cfo'
        return 'controller'
    if event.get('event_type') == 'cfo_hire':
        return 'cfo'
    if any(p in text for p in _CFO_EQUIV_PATTERNS):
        return 'cfo'
    return None


# ── Research probes — the evidence A.J.'s rubric was designed to consume ─────
# All free: funding + AUM ride the existing Firecrawl/Tavily dispatcher (with
# its persistent cache); nonprofit data uses ProPublica's public 990 API.

ASSET_MANAGER_SUBINDUSTRIES = {
    'Venture Capital & Private Equity', 'Investment Banking',
    'Lending & Brokerage',
}
NONPROFIT_VERTICAL = 'Nonprofits & Organizations'


def probe_funding_history(company_name: str) -> str:
    """Search for funding events (rubric: #Funding = verified within last
    18 months). Returns a compact evidence block ('' if nothing)."""
    try:
        res = tavily_search(company_name, 'funding round investment raised')
        hits = (res or {}).get('results') or []
        if not hits:
            return ''
        lines = [f'FUNDING SEARCH ("{company_name} funding"):']
        for r in hits[:3]:
            lines.append(f"- {r.get('title','')} | {r.get('url','')}")
            snippet = (r.get('content') or '')[:200]
            if snippet:
                lines.append(f"  {snippet}")
        return '\n'.join(lines)
    except Exception as e:
        log.debug(f'  funding probe failed: {e}')
        return ''


def probe_aum(company_name: str) -> str:
    """Search for AUM/AUA evidence for asset managers (rubric:
    #AssetManagerScale). Returns a compact evidence block ('' if nothing)."""
    try:
        res = tavily_search(company_name, 'AUM assets under management funds')
        hits = (res or {}).get('results') or []
        if not hits:
            return ''
        lines = [f'AUM SEARCH ("{company_name} assets under management"):']
        for r in hits[:3]:
            lines.append(f"- {r.get('title','')} | {r.get('url','')}")
            snippet = (r.get('content') or '')[:200]
            if snippet:
                lines.append(f"  {snippet}")
        return '\n'.join(lines)
    except Exception as e:
        log.debug(f'  AUM probe failed: {e}')
        return ''


def probe_nonprofit_990(company_name: str) -> str:
    """ProPublica Nonprofit Explorer (free, no key): find the org, pull its
    latest Form 990 financials. This is the rubric's required NPO source —
    990 revenue + filing history evidence complexity and revenue band.
    Returns a compact evidence block ('' if no match)."""
    try:
        s = requests.get(
            'https://projects.propublica.org/nonprofits/api/v2/search.json',
            params={'q': company_name}, timeout=12)
        s.raise_for_status()
        orgs = (s.json() or {}).get('organizations') or []
        if not orgs:
            return ''
        org = orgs[0]
        ein = org.get('ein')
        lines = [
            f'PROPUBLICA 990 ("{company_name}"):',
            f"- Matched org: {org.get('name')} (EIN {ein}), "
            f"{org.get('city')}, {org.get('state')} | "
            f"https://projects.propublica.org/nonprofits/organizations/{ein}",
        ]
        try:
            d = requests.get(
                f'https://projects.propublica.org/nonprofits/api/v2/'
                f'organizations/{ein}.json', timeout=12)
            d.raise_for_status()
            filings = (d.json() or {}).get('filings_with_data') or []
            if filings:
                f0 = filings[0]
                rev = f0.get('totrevenue')
                exp = f0.get('totfuncexpns')
                yr = f0.get('tax_prd_yr')
                lines.append(
                    f"- Latest 990 ({yr}): total revenue ${rev:,} · "
                    f"total expenses ${exp:,}" if rev is not None else
                    f"- Latest filing year: {yr}")
                lines.append(f"- 990 filings on record: {len(filings)} years")
        except Exception:
            pass
        return '\n'.join(lines)
    except Exception as e:
        log.debug(f'  990 probe failed: {e}')
        return ''


def gather_extra_evidence(event: dict, companies_data: list,
                          fit: dict) -> str:
    """Run the rubric's research probes for the PRIMARY company, chosen by
    what the rubric needs for this kind of account. Skips redundant work
    (funding events already carry funding evidence)."""
    account_name = (fit.get('account_name') or '').strip()
    primary = next((c for c in companies_data
                    if (c.get('name') or '').strip() == account_name), None) \
        or pick_primary(companies_data)
    name = (primary.get('name') or '').strip()
    if not name:
        return ''
    blocks = []
    zi = fit.get('zi_subindustry') or ''
    vertical = ZI_SUBINDUSTRIES.get(zi, '')

    # Nonprofits → 990 (revenue + complexity evidence, per rubric NPO rules)
    if vertical == NONPROFIT_VERTICAL:
        b = probe_nonprofit_990(name)
        if b:
            blocks.append(b)
    else:
        # For-profits: funding lookback (skip when the event IS the funding
        # announcement — that evidence is already in the title/description)
        if (event.get('event_type') or '') != 'funding':
            b = probe_funding_history(name)
            if b:
                blocks.append(b)

    # Asset managers → AUM/AUA for #AssetManagerScale
    if zi in ASSET_MANAGER_SUBINDUSTRIES:
        b = probe_aum(name)
        if b:
            blocks.append(b)

    return '\n\n'.join(blocks)


def grade_event(event: dict, companies_data: list,
                extra_evidence: str = '', account_name: str = '') -> dict:
    """Apply TAL grading rules (A.J.'s latest rubric, 2026-07-16). Returns
    dict with grade/hashtags/confidence/numeric_score/etc. On any failure
    returns an empty-graded record so the pipeline can still write the event.

    NOTES:
    - Point-based scoring; LLM picks hashtags, code computes score + grade.
    - extra_evidence: optional research-probe results (funding lookback,
      ProPublica 990, AUM search) injected into the prompt so the rubric
      has the evidence it was designed to consume.
    - No hashtag cap — rubric says "use as many as evidence supports"
      (17 valid hashtags exist; the closed-set filter below is the guard).
    """
    empty = {
        'grade': None,
        'confidence': None,
        'numeric_score': None,
        'hashtags': [],
        'cfo_status': None,
        'grade_justification': None,
        'research_notes': [],
    }
    if not companies_data:
        return empty

    if not account_name:
        account_name = (pick_primary(companies_data) or {}).get('name') or 'the primary company'
    prompt = TAL_GRADING_PROMPT.format(
        title=event.get('title', ''),
        event_type=event.get('event_type', ''),
        article_url=event.get('source_url') or event.get('url') or '',
        description=(event.get('description') or '')[:600],
        companies_block=_build_companies_block(companies_data),
        extra_evidence=extra_evidence.strip() or '(none)',
        account_name=account_name,
    )
    data = llm_json(prompt, max_tokens=900)  # +100 for new fields
    if not data:
        return empty

    # ── Validate + coerce ────────────────────────────────────────────────
    # Note: we OVERRIDE the LLM's grade and numeric_score below using
    # deterministic computation from the hashtags. The LLM is unreliable
    # at arithmetic — it routinely uses wrong point values or applies
    # wrong grade thresholds. Hashtag selection is creative work (LLM's
    # strength); scoring + grading is mechanical (better in code).

    confidence = (data.get('confidence') or '').strip().title()  # "High"/"Medium"/"Low"
    if confidence not in ('High', 'Medium', 'Low'):
        confidence = None

    # Take the LLM's "Unable to Grade" signal if present — preserves the
    # path for unidentifiable companies; otherwise we'll compute the grade
    # from hashtags below.
    grade_raw = (data.get('grade') or '').strip()
    llm_says_unable = grade_raw.lower() == 'unable to grade'

    hashtags = data.get('hashtags') or []
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in hashtags.split() if h.strip().startswith('#')]
    # Keep only valid rubric hashtags — silently drop unknown/invented ones.
    # No count cap (rubric: "use as many approved hashtags as evidence
    # supports"); the closed set itself bounds the list at 17.
    seen = set()
    hashtags = [
        h for h in hashtags
        if isinstance(h, str) and h in TAL_V11_HASHTAG_POINTS
        and not (h in seen or seen.add(h))
    ]

    # ── Evidence guard: finance-leader triggers need finance-leader text ──
    # The LLM provably fabricates #NewCFO on non-CFO events despite the
    # prompt rules (e.g. "+5 applied as highest-value single trigger for
    # material definitive agreement events", CNL 2026-07-21). The research
    # probes never return CFO facts, so the only legitimate evidence source
    # for these two hashtags is the event itself: its type, title, or
    # description must state the role, or the tag is stripped.
    _evid_text = ' '.join([(event.get('title') or ''),
                           (event.get('description') or '')]).lower()
    if '#NewCFO' in hashtags and not (
            event.get('event_type') == 'cfo_hire'
            or any(p in _evid_text for p in _CFO_EQUIV_PATTERNS)):
        hashtags.remove('#NewCFO')
    if '#NewController' in hashtags and not any(
            p in _evid_text for p in _CONTROLLER_PATTERNS):
        hashtags.remove('#NewController')

    # ── DETERMINISTIC scoring + grade (overrides LLM math) ──────────────
    if llm_says_unable:
        grade = 'Unable to Grade'
        numeric_score = 0
    else:
        numeric_score, grade = _compute_v11_grade(hashtags, confidence)

    notes = data.get('research_notes') or []
    if not isinstance(notes, list):
        notes = []
    # V11 caps total notes content at 1000 chars — enforce per-note + total
    cleaned_notes = []
    total_chars = 0
    for n in notes:
        if not isinstance(n, dict) or not n.get('finding'):
            continue
        finding = str(n.get('finding', ''))[:300]
        source_url = str(n.get('source_url', ''))[:500]
        if total_chars + len(finding) > 1000:
            break
        total_chars += len(finding)
        cleaned_notes.append({'finding': finding, 'source_url': source_url})
        if len(cleaned_notes) >= 8:
            break
    notes = cleaned_notes

    cfo_status = (data.get('cfo_status') or '').strip() or None
    # Cap justification at 1000 chars per V11
    justification = (data.get('grade_justification') or '').strip()[:1000] or None

    return {
        'grade': grade,
        'confidence': confidence,
        'numeric_score': numeric_score,
        'hashtags': hashtags,
        'cfo_status': cfo_status,
        'grade_justification': justification,
        'research_notes': notes,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def enrich_events(
    limit: int = None,
    re_enrich: bool = False,
    dry_run: bool = False,
    missing_fit_only: bool = False,
    reverify_unverified: bool = False,
):
    check_required_keys()
    client  = get_supabase()
    col_ok  = check_columns(client)
    backend = _llm_backend()

    if not col_ok.get('companies_data'):
        log.warning(f'companies_data column missing. {MIGRATION_SQL}')
        if not dry_run:
            sys.exit(1)

    log.info(f'LLM backend: {backend} '
             f'({"claude-3-5-haiku" if backend == "anthropic" else LLAMACPP_MODEL} '
             f'-> Scout fallback)')

    # ── Fetch events ──────────────────────────────────────────────────────
    # Include source_url so grade_event() can cite the original article in
    # research_notes. Without it, the TAL prompt receives empty article_url
    # and the LLM has no primary source to reference.
    query = client.table('events').select(
        'id, company_name, event_type, title, description, source_url'
    )
    if not re_enrich and col_ok.get('enriched_at'):
        query = query.is_('enriched_at', 'null')
    if missing_fit_only and col_ok.get('fit'):
        # Catch-up mode: only events the fit-gate rollout hasn't touched yet
        # (e.g. rows beyond a prior run's 1000-row page cap). Idempotent.
        query = query.is_('fit', 'null')
    if reverify_unverified and col_ok.get('fit'):
        # Re-verify mode: only events whose fit gates couldn't confirm
        # territory/revenue/vertical (the ⚠️ VERIFY FIT population). Re-runs
        # the full research → gates → grade pipeline; events that CONFIRM
        # get uncapped (A becomes reachable) or tombstoned if confirmed-out;
        # still-unknown stay flagged. Run when the search backend is healthy
        # (e.g. after burst throttling subsided).
        query = query.eq('fit->>verdict', 'unverified')
    # NEVER process tombstoned events — they're already decided (industry/
    # fit-gate blocked or rep-dismissed). Re-enriching them is pure waste
    # (~15s each) and re-grades rows the dashboard will never show.
    try:
        query = query.is_('blocked_at', 'null')
    except Exception:
        pass

    # Process OLDEST first. When the queue grows beyond per-run capacity,
    # newest-first ordering pushes old events further back each cycle until
    # they rot indefinitely (e.g. events stuck for 9 days). Oldest-first
    # guarantees forward progress on the queue's tail. Trade-off: freshest
    # events take a bit longer to appear graded on the dashboard, but
    # they're visible (just ungraded) much sooner regardless.
    result = query.order('discovered_at', desc=False).execute()
    events = result.data or []
    if limit:
        events = events[:limit]

    if not events:
        log.info('No unenriched events — nothing to do.')
        return

    tag = 'DRY RUN — ' if dry_run else ''
    log.info(f'{tag}Processing {len(events)} event(s)')
    print()

    firm_cache: dict = {}
    reset_search_counts()
    ok = fail = 0

    for idx, event in enumerate(events, 1):
        eid   = event['id']
        title = (event.get('title') or '')[:80]
        etype = event.get('event_type', '')
        log.info(f'[{idx}/{len(events)}] {title}')

        # ── 0. Board-only gate (free, before any LLM/search spend) ────────
        # Pure board-of-directors changes are not triggers — tombstone.
        if _board_only_event(event):
            log.info('  🚫 Board-of-directors change only (no finance role) '
                     '— soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid,
                             'board_change_only: director/board appointment, '
                             'no finance-leader role')
            ok += 1
            continue

        # ── 1. Extract companies + roles ──────────────────────────────────
        companies = extract_event_companies(event)

        if not companies:
            fallback = (event.get('company_name') or '').strip()
            if fallback and fallback.lower() not in (
                'unknown', 'unknown company', 'nan'
            ):
                companies = [{'name': fallback, 'role': 'Primary'}]

        if not companies:
            log.info('  No companies identified — marking processed')
            if not dry_run and col_ok.get('enriched_at'):
                try:
                    client.table('events').update(
                        {'enriched_at': datetime.utcnow().isoformat()}
                    ).eq('id', eid).execute()
                except Exception:
                    pass
            ok += 1
            continue

        co_summary = ', '.join(f'{c["name"]} ({c["role"]})' for c in companies)
        log.info(f'  Companies: {co_summary}')

        # ── 2. Enrich each company ────────────────────────────────────────
        enriched = []
        for co in companies:
            name      = co['name']
            role      = co['role']
            cache_key = name.lower().strip()

            # Build a NEUTRAL disambiguation hint. Never inject industry
            # guesses: the old 'financial services private equity' hint for
            # M&A/funding events biased BOTH the web search AND the ZI
            # classification — every funded startup came back classified
            # 'Venture Capital & Private Equity' (live test 2026-07-16).
            # Role-based context only:
            role_l = (role or '').lower()
            descriptor = (co.get('descriptor') or '').strip()
            if descriptor:
                # The article's own words about what this company does — the
                # best disambiguator, esp. for generic names ("fomo" alone is
                # unsearchable; "fomo trading platform" isn't).
                industry_hint = descriptor
            elif role_l in ('lead investor', 'investor'):
                industry_hint = 'investment firm'
            else:
                industry_hint = 'company North America'

            if cache_key not in firm_cache:
                log.info(f'  → Searching: {name}')
                if not dry_run:
                    _article_ctx = (
                        f"{(event.get('title') or '')[:200]}\n"
                        f"{(event.get('description') or '')[:500]}"
                    )
                    firm_cache[cache_key] = enrich_one_company(
                        name, industry_hint, article_context=_article_ctx)
                    time.sleep(RATE_LIMIT_SECONDS)
                else:
                    firm_cache[cache_key] = {
                        'url': None, 'industry': None, 'zi_subindustry': None,
                        'size': None, 'revenue': None, 'revenue_source': None,
                        'hq': None, 'linkedin': None
                    }
            else:
                log.info(f'  → Cached:   {name}')

            firm = firm_cache[cache_key]
            found = [f'{k}: {v}' for k, v in firm.items() if v]
            if found:
                log.info(f'     {" | ".join(found)}')

            enriched.append({
                'name':           name,
                'role':           role,
                'url':            firm.get('url'),
                'industry':       firm.get('industry'),
                'zi_subindustry': firm.get('zi_subindustry'),
                'size':           firm.get('size'),
                'revenue':        firm.get('revenue'),
                'revenue_source': firm.get('revenue_source'),
                'hq':             firm.get('hq'),
                'linkedin':       firm.get('linkedin'),
            })

        # ── 3. Post-enrichment industry filter ────────────────────────────
        # Now that we know the discovered industry, re-apply exclusions.
        # Catches mining/steel/oil that slipped past the scrape-time text
        # filter (e.g. "Chilean Cobalt Corp." → industry "Critical Minerals
        # Exploration" → blocked here).
        primary = pick_primary(enriched)
        blocked, kw = industry_is_blocked(primary.get('industry') or '')
        if blocked:
            log.info(
                f'  🚫 Post-enrichment block — industry '
                f'"{primary.get("industry")}" matched "{kw}". Soft-deleting.'
            )
            if not dry_run:
                _soft_delete(client, eid,
                             f'industry: {primary.get("industry")} (matched "{kw}")')
            ok += 1  # count as processed (not failed)
            continue

        # ── 4. FIT GATES (deterministic, post-research) ───────────────────
        # Territory × revenue band × ZI-subindustry allowlist. Confirmed-out
        # on any dimension → soft-delete. Unknowns → keep, cap grade at B,
        # flag for a 10-second rep verification.
        fit = apply_fit_gates(enriched)
        if fit['verdict'] == 'fail':
            log.info(f'  🚫 Fit gate FAIL — {"; ".join(fit["reasons"])}. '
                     f'Soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid, f'fit_gate: {"; ".join(fit["reasons"])}')
            ok += 1
            continue
        if fit['verdict'] == 'unverified':
            log.info(f'  ⚠️  Fit unverified — {"; ".join(fit["reasons"])} '
                     f'(grade capped at B)')

        # ── 5. Research probes + TAL grading (A.J. rubric) ────────────────
        extra_evidence = '' if dry_run else gather_extra_evidence(event, enriched, fit)
        log.info(f'  Grading via TAL rubric…')
        grading = grade_event(event, enriched, extra_evidence,
                              account_name=fit.get('account_name') or '')

        # Unverified-fit cap: an A grade needs confirmed fit. (Agreed with
        # A.J. 2026-07-16: flag, don't hide.)
        if fit['verdict'] == 'unverified' and grading.get('grade') == 'A':
            grading['grade'] = 'B'
            grading['grade_justification'] = (
                '⚠️ Capped A→B: fit unverified '
                f'({"; ".join(r for r in fit["reasons"] if "unverified" in r)}). '
                + (grading.get('grade_justification') or '')
            )[:1000]

        # ── Per-COMPANY grades (the grade belongs to the account, per A.J.
        # 2026-07-17). The chosen account carries the event's headline grade;
        # every OTHER workable, non-failed company gets its own grade too —
        # a PE deal with two fitting investors yields a grade for each.
        # Stored compactly on each company dict → companies_data JSONB.
        account_nm = (fit.get('account_name') or '').strip()
        if not dry_run and grading.get('grade'):
            for c in enriched:
                cname = (c.get('name') or '').strip()
                cfit = c.get('fit') or {}
                if not cname:
                    continue
                if cname == account_nm:
                    c['tal'] = {'grade': grading.get('grade'),
                                'score': grading.get('numeric_score'),
                                'confidence': grading.get('confidence'),
                                'hashtags': grading.get('hashtags') or [],
                                'justification': grading.get('grade_justification')}
                    continue
                if (str(c.get('role', '')).lower() in
                        [r for r in WORKABLE_ROLES]
                        and cfit.get('verdict') in ('pass', 'unverified')):
                    g2 = grade_event(event, enriched, extra_evidence,
                                     account_name=cname)
                    if cfit.get('verdict') == 'unverified' and g2.get('grade') == 'A':
                        g2['grade'] = 'B'
                    if g2.get('grade'):
                        c['tal'] = {'grade': g2.get('grade'),
                                    'score': g2.get('numeric_score'),
                                    'confidence': g2.get('confidence'),
                                    'hashtags': g2.get('hashtags') or [],
                                    'justification': g2.get('grade_justification')}
                        log.info(f'    Secondary account {cname[:30]}: '
                                 f'Grade={g2["grade"]} Score={g2.get("numeric_score")}')

            # ── Headline = BEST-graded company (A.J. 2026-07-17: "the grade
            # at the top should indicate the highest grade of the companies
            # within that event"). Grade/score/hashtags/attribution all
            # promote together so the pill and the account name agree.
            _grank = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
            _best = None
            for c in enriched:
                _t = c.get('tal') or {}
                if _t.get('grade') in _grank:
                    _k = (_grank[_t['grade']], -(_t.get('score') or 0))
                    if _best is None or _k < _best[0]:
                        _best = (_k, c)
            if _best is not None:
                _bc = _best[1]
                if (_bc.get('name') or '').strip() != account_nm:
                    _bt = _bc['tal']
                    log.info(f'    Headline promoted to best account: '
                             f'{_bc.get("name")} (Grade {_bt["grade"]})')
                    grading['grade'] = _bt['grade']
                    grading['numeric_score'] = _bt.get('score')
                    grading['confidence'] = _bt.get('confidence')
                    if _bt.get('hashtags'):
                        grading['hashtags'] = _bt['hashtags']
                    if _bt.get('justification'):
                        grading['grade_justification'] = _bt['justification']
                    _bf = _bc.get('fit') or {}
                    fit['account_name'] = _bc.get('name')
                    fit['primary_name'] = _bc.get('name')
                    fit['zi_subindustry'] = _bc.get('zi_subindustry')
                    for _dim in ('territory', 'revenue', 'vertical'):
                        if _bf.get(_dim):
                            fit[_dim] = _bf[_dim]
                    if _bf.get('verdict'):
                        fit['verdict'] = _bf['verdict']
                        fit['reasons'] = list(_bf.get('reasons') or [])

        if grading.get('grade'):
            log.info(
                f'    Grade={grading["grade"]}  '
                f'Score={grading.get("numeric_score")}  '
                f'Hashtags={" ".join(grading["hashtags"]) or "(none)"}'
            )

        # ── 6. Write to Supabase ──────────────────────────────────────────
        if dry_run:
            log.info(f'  Would write {len(enriched)} company record(s) + grade '
                     f'(fit={fit["verdict"]})')
            ok += 1
            continue

        payload = {'companies_data': enriched}
        if grading.get('grade') is not None:
            payload.update({
                'grade':               grading.get('grade'),
                'confidence_level':    grading.get('confidence'),
                'numeric_score':       grading.get('numeric_score'),
                'hashtags':            grading.get('hashtags') or [],
                'grade_justification': grading.get('grade_justification'),
                'cfo_status':          grading.get('cfo_status'),
                'research_notes':      grading.get('research_notes') or [],
            })
        else:
            # Grading LLM failed entirely — keep firmographics but DON'T
            # overwrite any existing grade with None (re-enrich runs were
            # nulling good grades on transient LLM failures).
            log.warning('  Grading returned nothing — keeping existing grade')
        if col_ok.get('enriched_at'):
            payload['enriched_at'] = datetime.utcnow().isoformat()
        if col_ok.get('fit'):
            payload['fit'] = fit

        # Upgrade event_type to cfo_hire ONLY for true CFO-equivalent hires.
        # Controller/VP-Accounting hires stay executive_hire — relabeling them
        # caused a #NewCFO(+5) vs #NewController(+3) double-count on regrade.
        current_etype = (event.get('event_type') or '').lower()
        if current_etype != 'cfo_hire' and _finance_role(event) == 'cfo':
            payload['event_type'] = 'cfo_hire'
            log.info(f'    Reclassifying event_type {current_etype!r} → cfo_hire')

        try:
            client.table('events').update(payload).eq('id', eid).execute()
            ok += 1
        except Exception as e:
            if 'does not exist' in str(e):
                # New grading columns may not be present yet — retry without them
                log.warning(
                    f'  Some columns missing — retrying with firmographics only. '
                    f'{MIGRATION_SQL}'
                )
                try:
                    minimal = {'companies_data': enriched}
                    if col_ok.get('enriched_at'):
                        minimal['enriched_at'] = datetime.utcnow().isoformat()
                    client.table('events').update(minimal).eq('id', eid).execute()
                    ok += 1
                except Exception as e2:
                    log.error(f'  Supabase write failed: {e2}')
                    fail += 1
            else:
                log.error(f'  Supabase write failed: {e}')
                fail += 1

    print()
    sc = SEARCH_COUNTS
    log.info(
        f'Done — enriched: {ok}, failed: {fail}  ·  '
        f'Searches: {sum(sc.values())} '
        f'(cache:{sc["cache"]} firecrawl:{sc["firecrawl"]} '
        f'searxng:{sc["searxng"]} cse:{sc["google_cse"]} tavily:{sc["tavily"]})'
    )


# ── Regrade-only mode (free — no Tavily, no firmographic re-fetch) ─────────

def regrade_only_events(limit: int = None, dry_run: bool = False):
    """Re-apply ONLY the TAL grading + post-enrichment industry filter +
    event_type reclassification to existing events. Uses each event's existing
    companies_data — does NOT call Tavily and does NOT re-extract firmographics.

    Use this when you only want to apply NEW grading/classification rules to
    historical events without burning Tavily API credits. Ollama (local, free)
    is still used for the grading LLM call.
    """
    check_required_keys()  # not strictly needed (no Tavily) — but harmless
    client = get_supabase()
    col_ok = check_columns(client)

    # Fetch events that already have firmographic data.
    # Oldest-first (same rationale as enrich_events): if a regrade run is
    # interrupted, we've made forward progress on the tail and the next
    # run picks up where we left off.
    query = client.table('events').select(
        'id, company_name, event_type, title, description, '
        'source_url, companies_data'
    ).not_.is_('companies_data', 'null')
    # Skip tombstoned events (already blocked/dismissed — regrading them is
    # wasted work on rows the dashboard never shows)
    try:
        query = query.is_('blocked_at', 'null')
    except Exception:
        pass
    result = query.order('discovered_at', desc=False).execute()
    events = result.data or []
    if limit:
        events = events[:limit]

    if not events:
        log.info('No events with existing companies_data — nothing to regrade.')
        return

    tag = 'DRY RUN — ' if dry_run else ''
    log.info(
        f'{tag}Regrading {len(events)} event(s) using existing firmographics. '
        f'NO Tavily calls (free).'
    )
    print()

    ok = deleted = upgraded = fail = 0

    for idx, event in enumerate(events, 1):
        eid   = event['id']
        title = (event.get('title') or '')[:80]
        log.info(f'[{idx}/{len(events)}] {title}')

        # Board-only gate — same rule as the enrich path
        if _board_only_event(event):
            log.info('  🚫 Board-of-directors change only (no finance role) '
                     '— soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid,
                             'board_change_only: director/board appointment, '
                             'no finance-leader role')
            deleted += 1
            continue

        # Unwrap companies_data
        cd = event.get('companies_data')
        if isinstance(cd, str):
            try:
                cd = json.loads(cd)
            except Exception:
                cd = []
        if not cd or not isinstance(cd, list):
            log.info('  (empty companies_data — skipping)')
            continue

        # ── Post-enrichment industry filter ───────────────────────────────
        primary = pick_primary(cd)
        blocked, kw = industry_is_blocked(primary.get('industry') or '')
        if blocked:
            log.info(
                f'  🚫 Industry "{primary.get("industry")}" matched "{kw}" '
                f'→ soft-delete'
            )
            if not dry_run:
                _soft_delete(client, eid,
                             f'industry: {primary.get("industry")} (matched "{kw}")')
            deleted += 1
            continue

        # ── FIT GATES (same policy as enrich_events) ──────────────────────
        # Note: legacy events lack zi_subindustry (added 2026-07-16) — their
        # vertical reads 'unknown', so most legacy events land 'unverified'
        # (kept, capped at B, flagged) rather than 'fail'. Full re-enrichment
        # is what upgrades them to confirmed fit.
        fit = apply_fit_gates(cd)
        if fit['verdict'] == 'fail':
            log.info(f'  🚫 Fit gate FAIL — {"; ".join(fit["reasons"])} '
                     f'→ soft-delete')
            if not dry_run:
                _soft_delete(client, eid, f'fit_gate: {"; ".join(fit["reasons"])}')
            deleted += 1
            continue

        # ── TAL grading (local LLM, free — no search probes in this mode) ──
        grading = grade_event(event, cd, account_name=fit.get('account_name') or '')

        # Per-company grades for other workable, non-failed companies
        # (mirrors enrich_events — the grade belongs to the account)
        _acct_nm = (fit.get('account_name') or '').strip()
        if not dry_run and grading.get('grade'):
            for _c in cd:
                _cn = (_c.get('name') or '').strip()
                _cf = _c.get('fit') or {}
                if not _cn:
                    continue
                if _cn == _acct_nm:
                    _c['tal'] = {'grade': grading.get('grade'),
                                 'score': grading.get('numeric_score'),
                                 'confidence': grading.get('confidence')}
                    continue
                if (str(_c.get('role', '')).lower() in WORKABLE_ROLES
                        and _cf.get('verdict') in ('pass', 'unverified')):
                    _g2 = grade_event(event, cd, account_name=_cn)
                    if _cf.get('verdict') == 'unverified' and _g2.get('grade') == 'A':
                        _g2['grade'] = 'B'
                    if _g2.get('grade'):
                        _c['tal'] = {'grade': _g2.get('grade'),
                                     'score': _g2.get('numeric_score'),
                                     'confidence': _g2.get('confidence'),
                                     'hashtags': _g2.get('hashtags') or [],
                                     'justification': _g2.get('grade_justification')}
            # store headline account's tal with promotion fields too
            for _c in cd:
                if (_c.get('name') or '').strip() == _acct_nm and grading.get('grade'):
                    _c['tal'] = {'grade': grading.get('grade'),
                                 'score': grading.get('numeric_score'),
                                 'confidence': grading.get('confidence'),
                                 'hashtags': grading.get('hashtags') or [],
                                 'justification': grading.get('grade_justification')}
            # Headline = best-graded company (mirrors enrich_events)
            _grank = {'A': 0, 'B': 1, 'C': 2, 'D': 3}
            _best = None
            for _c in cd:
                _t = _c.get('tal') or {}
                if _t.get('grade') in _grank:
                    _k = (_grank[_t['grade']], -(_t.get('score') or 0))
                    if _best is None or _k < _best[0]:
                        _best = (_k, _c)
            if _best is not None:
                _bc = _best[1]
                if (_bc.get('name') or '').strip() != _acct_nm:
                    _bt = _bc['tal']
                    grading['grade'] = _bt['grade']
                    grading['numeric_score'] = _bt.get('score')
                    grading['confidence'] = _bt.get('confidence')
                    if _bt.get('hashtags'):
                        grading['hashtags'] = _bt['hashtags']
                    if _bt.get('justification'):
                        grading['grade_justification'] = _bt['justification']
                    _bf = _bc.get('fit') or {}
                    fit['account_name'] = _bc.get('name')
                    fit['primary_name'] = _bc.get('name')
                    fit['zi_subindustry'] = _bc.get('zi_subindustry')
                    for _dim in ('territory', 'revenue', 'vertical'):
                        if _bf.get(_dim):
                            fit[_dim] = _bf[_dim]
                    if _bf.get('verdict'):
                        fit['verdict'] = _bf['verdict']
                        fit['reasons'] = list(_bf.get('reasons') or [])

        if fit['verdict'] == 'unverified' and grading.get('grade') == 'A':
            grading['grade'] = 'B'
            grading['grade_justification'] = (
                '⚠️ Capped A→B: fit unverified '
                f'({"; ".join(r for r in fit["reasons"] if "unverified" in r)}). '
                + (grading.get('grade_justification') or '')
            )[:1000]

        if grading.get('grade'):
            log.info(
                f'  Grade={grading["grade"]}  '
                f'Hashtags={" ".join(grading["hashtags"]) or "(none)"}'
            )

        if dry_run:
            ok += 1
            continue

        # ── Build payload. companies_data IS written now — per-company fit
        # and per-company TAL grades were attached to the company dicts. ──
        if grading.get('grade') is None:
            log.warning('  Grading returned nothing — keeping existing grade')
            payload = {'companies_data': cd}
        else:
            payload = {
                'companies_data':      cd,
                'grade':               grading.get('grade'),
                'confidence_level':    grading.get('confidence'),
                'numeric_score':       grading.get('numeric_score'),
                'hashtags':            grading.get('hashtags') or [],
                'grade_justification': grading.get('grade_justification'),
                'cfo_status':          grading.get('cfo_status'),
                'research_notes':      grading.get('research_notes') or [],
            }
        if col_ok.get('fit'):
            payload['fit'] = fit

        # event_type reclassification — CFO-equivalents only (Controllers
        # stay executive_hire; see _finance_role for why)
        current_etype = (event.get('event_type') or '').lower()
        if current_etype != 'cfo_hire' and _finance_role(event) == 'cfo':
            payload['event_type'] = 'cfo_hire'
            upgraded += 1
            log.info(f'  Reclassifying event_type {current_etype!r} → cfo_hire')

        if not payload:
            ok += 1  # nothing to write (grading failed, no reclass) — skip
            continue

        try:
            client.table('events').update(payload).eq('id', eid).execute()
            ok += 1
        except Exception as e:
            if 'does not exist' in str(e):
                log.error(
                    f'  Write failed — schema not migrated yet. {MIGRATION_SQL}'
                )
            else:
                log.error(f'  Write failed: {e}')
            fail += 1

    print()
    log.info(
        f'Done — regraded: {ok}, deleted (industry block): {deleted}, '
        f'event_type → cfo_hire: {upgraded}, failed: {fail}'
    )
    log.info('Search API calls (Firecrawl/Tavily): 0  (regrade-only mode)')


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Enrich trigger events with multi-company firmographic data'
    )
    p.add_argument('--limit',         type=int, default=None,
                   help='Max events to process (default: all)')
    p.add_argument('--re-enrich',     action='store_true',
                   help='Full re-enrichment (calls Tavily for firmographics + re-grade). '
                        'Costs Tavily quota.')
    p.add_argument('--regrade-only',  action='store_true',
                   help='Re-apply grading rules + industry filter + event_type '
                        'reclassification using EXISTING firmographic data. '
                        'NO Tavily calls (free, Ollama-only).')
    p.add_argument('--missing-fit-only', action='store_true',
                   help='With --re-enrich: only process events that have no '
                        'fit data yet (catch-up after a capped backlog run)')
    p.add_argument('--reverify-unverified', action='store_true',
                   help='With --re-enrich: only re-process events whose fit '
                        'is unverified (the ⚠️ VERIFY FIT population) — '
                        'fresh research to confirm or refute fit')
    p.add_argument('--dry-run',       action='store_true',
                   help='Preview without writing to Supabase')
    args = p.parse_args()

    if args.regrade_only:
        if args.re_enrich:
            sys.exit('Choose one: --regrade-only OR --re-enrich (not both)')
        regrade_only_events(limit=args.limit, dry_run=args.dry_run)
    else:
        enrich_events(
            limit=args.limit,
            re_enrich=args.re_enrich,
            dry_run=args.dry_run,
            missing_fit_only=args.missing_fit_only,
            reverify_unverified=args.reverify_unverified,
        )
