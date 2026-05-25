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
# Tavily key MUST come from env (.env locally, GitHub Secret in CI).
# We don't fall back to any hardcoded value — that would leak into git history.
TAVILY_API_KEY    = os.environ.get('TAVILY_API_KEY', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
OLLAMA_URL        = os.environ.get('OLLAMA_URL',   'http://localhost:11434')
# Default model — falls back to whatever is locally available. Override with
# `export OLLAMA_MODEL=...` to use a specific model (e.g. qwen2.5:14b).
OLLAMA_MODEL      = os.environ.get('OLLAMA_MODEL', 'qwen3-coder:30b')
CLAUDE_MODEL      = 'claude-3-5-haiku-20241022'

RATE_LIMIT_SECONDS = 1.2

MIGRATION_SQL = (
    "Run in Supabase SQL Editor:\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS companies_data       JSONB;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS enriched_at          TIMESTAMPTZ;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS grade                TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS hashtags             JSONB;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS grade_justification  TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS cfo_status           TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS research_notes       JSONB;"
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
    'smelting',
    # Heavy industry
    'steel', 'aluminum', 'foundry', 'heavy industry', 'industrial manufacturing',
    # Oil & gas
    'oil & gas', 'oil and gas', 'petroleum', 'petrochemical', 'refinery',
    # Out-of-target verticals
    'hotel', 'hospitality', 'restaurant', 'qsr', 'fast food',
    'casino', 'gaming', 'sports betting',
    'engineering firm', 'civil engineering', 'construction company',
    'data center', 'colocation',
    'power generation', 'utilities', 'electric utility',
    'solar farm', 'wind farm',
    'logistics', 'freight', 'trucking', 'supply chain',
]

# Primary-company roles (the actual subject of the event)
PRIMARY_ROLES = {
    'acquirer', 'target', 'portfolio company',
    'hiring company', 'primary',
}


def industry_is_blocked(industry: str):
    """Return (is_blocked, matched_keyword) for an industry string."""
    if not industry:
        return False, ''
    industry_lower = industry.lower()
    for kw in POST_ENRICHMENT_INDUSTRY_BLOCK:
        if kw in industry_lower:
            return True, kw
    return False, ''

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)


# ── LLM abstraction (Anthropic API or Ollama) ─────────────────────────────────

def _llm_backend():
    return 'anthropic' if ANTHROPIC_API_KEY else 'ollama'


def llm_json(prompt: str, max_tokens: int = 600) -> dict:
    """
    Send prompt to the active LLM, return parsed JSON dict.
    Tries Anthropic claude-3-5-haiku if ANTHROPIC_API_KEY is set, else Ollama.
    Returns {} on any failure.
    """
    if _llm_backend() == 'anthropic':
        return _anthropic_json(prompt, max_tokens)
    return _ollama_json(prompt, max_tokens)


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


def _ollama_json(prompt: str, max_tokens: int) -> dict:
    try:
        resp = requests.post(
            f'{OLLAMA_URL}/api/generate',
            json={
                'model':   OLLAMA_MODEL,
                'prompt':  prompt,
                'stream':  False,
                'format':  'json',
                'options': {'temperature': 0.05, 'num_predict': max_tokens},
            },
            timeout=90
        )
        resp.raise_for_status()
        return json.loads(resp.json().get('response', '{}'))
    except Exception as e:
        log.warning(f'  Ollama error: {e}')
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
    """Fail fast with a clear message if required API keys aren't set."""
    if not TAVILY_API_KEY:
        sys.exit(
            "TAVILY_API_KEY not set. Add to .env (local) or GitHub Secrets (CI):\n"
            "  TAVILY_API_KEY=tvly-...\n"
            "Get a free key at https://tavily.com"
        )


def check_columns(client):
    exists = {}
    for col in ('companies_data', 'enriched_at'):
        try:
            client.table('events').select(col).limit(1).execute()
            exists[col] = True
        except Exception:
            exists[col] = False
    return exists


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
- Assign a specific role using these labels:
    M&A events:           "Acquirer", "Target", "Advisor"
    Funding events:       "Portfolio Company", "Lead Investor", "Investor"
    CFO / exec hire:      "Hiring Company", "Previous Employer"
    Other:                "Primary", "Partner", "Mentioned"
- Maximum 5 companies.
- If no named companies can be identified, return {{"companies": []}}.

Return ONLY a JSON object with one key:
{{"companies": [{{"name": "Full Company Name", "role": "Role"}}, ...]}}'''


def extract_event_companies(event: dict) -> list:
    prompt = EXTRACT_PROMPT.format(
        event_type=event.get('event_type', ''),
        title=event.get('title', '')[:220],
        description=(event.get('description', '') or '')[:700],
    )
    data = llm_json(prompt, max_tokens=400)
    raw = data.get('companies', [])
    valid = []
    for c in raw:
        name = (c.get('name') or '').strip()
        role = (c.get('role') or 'Mentioned').strip()
        if name and len(name) > 1 and name.lower() not in (
            'unknown', 'nan', 'none', ''
        ):
            valid.append({'name': name, 'role': role})
    return valid[:5]


# ── Step 2 — Tavily web search ────────────────────────────────────────────────

def tavily_search(company_name: str, industry_hint: str = '') -> dict:
    """Search Tavily. industry_hint steers results toward the right entity.
    Revenue + employee keywords in the query surface firmographic pages
    (Crunchbase, ZoomInfo, Bloomberg, Owler) naturally."""
    hint = f' {industry_hint}' if industry_hint else ''
    query = (
        f'"{company_name}"{hint} company official website headquarters '
        f'employees annual revenue size'
    )
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


# ── Step 3 — Extract firmographics from search results ───────────────────────

FIRMOGRAPHIC_PROMPT = '''\
Extract firmographic data for a specific company from the search results below.

Target company: "{company_name}"
Industry context from the news event: "{industry_hint}"

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
  "industry": "specific industry — be precise, e.g. 'Wealth Management', \
'Healthcare IT', 'Commercial Banking', 'B2B SaaS', 'Private Equity', \
'Insurance', 'Auto Dealer', 'Charitable Foundation' — not generic like \
'Technology' or 'Services' — or null",
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


def enrich_one_company(company_name: str, industry_hint: str = '') -> dict:
    empty = {'url': None, 'industry': None, 'size': None, 'revenue': None,
             'revenue_source': None, 'hq': None, 'linkedin': None}

    search = tavily_search(company_name, industry_hint)
    if not search.get('results'):
        return empty

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
        results_text='\n'.join(lines).strip()
    )
    data = llm_json(prompt, max_tokens=550)

    # Only keep revenue_source if revenue itself was extracted (no point
    # citing a URL for a null revenue)
    revenue       = data.get('revenue')        or None
    revenue_src   = data.get('revenue_source') or None
    if not revenue:
        revenue_src = None

    return {
        'url':            data.get('url')      or None,
        'industry':       data.get('industry') or None,
        'size':           data.get('size')     or None,
        'revenue':        revenue,
        'revenue_source': revenue_src,
        'hq':             data.get('hq')       or None,
        'linkedin':       data.get('linkedin') or None,
    }


# ── Step 4 — TAL grading (TAL V10.2 system, adapted for our flow) ───────────

TAL_GRADING_PROMPT = '''\
You are an AI research analyst for Oracle NetSuite sales applying the TAL V10.2 \
grading system. Be CONSERVATIVE — do NOT inflate hashtags or grades.

EVENT
Title: {title}
Type: {event_type}
URL: {article_url}
Description: {description}

COMPANIES (pre-researched firmographics)
{companies_block}

HASHTAG RULES — apply ONLY when evidence is unambiguous. Each requires \
explicit support from the input above. Never apply by inference. Max 6.

- #NewCFO: event_type is "cfo_hire" OR title explicitly states a new CFO/Chief \
Financial Officer announcement. DO NOT apply for general "officer" departures.
- #Funding: event_type is "funding" AND company is for-profit. DO NOT apply to \
nonprofit grants/donations or to companies BEING acquired.
- #Acquisitions: event_type is "merger_acquisition" AND the company being \
graded has role "Acquirer". DO NOT apply for Target role.
- #PEBacked: a company in the event has role "Lead Investor" or "Investor" AND \
the investor name signals a PE firm (contains "Capital"/"Partners"/"Equity" OR \
matches known names: Bain, KKR, Blackstone, Carlyle, Apollo, TPG, Vista, \
Thoma Bravo, Nautic, EIG, Roark, Hellman & Friedman, Silver Lake). DO NOT \
apply for VC funding alone (General Catalyst, Sequoia, a16z = VC, not PE).
- #HoldCo: name contains "Holdings" OR industry is "Holding Companies & \
Conglomerates" OR description explicitly mentions multiple operating subsidiaries.
- #100EE: firmographic size is 201-500 or larger. NOT 1-50 or 51-200.
- #Locations: explicit mention of 3+ physical locations/stores/branches/offices. \
DO NOT apply for HAVING a HQ city.
- #Entities: explicit mention of multiple legal entities/subsidiaries/EINs. \
DO NOT apply by inference.
- #Global: documented operations in 3+ countries OR HQ outside US/Canada. DO \
NOT apply for a single international partnership.
- #HyperGrowth: documented >50% YoY revenue growth or 100+ headcount expansion \
in <12 months. DO NOT apply for routine funding rounds.
- #Franchisor: company franchises its brand to others. Explicit only.
- #Franchisee: company operates franchised locations. Explicit only.
- #FormerUser: explicitly mentioned as former NetSuite customer. SKIP otherwise.
- #PrevConvo: explicit prior sales conversation. SKIP otherwise.
- #Legacy: explicit mention of legacy ERP (QuickBooks, Sage 50, Dynamics GP). \
SKIP otherwise.

When in doubt, DROP the hashtag.

GRADE — conservative, do NOT inflate:
- A = 3+ hashtags AND 2 triggers (the event itself counts as 1 trigger; a 2nd \
trigger needs another distinct hashtag-worthy signal beyond the base event)
- B = 2 hashtags AND at least 1 trigger
- C = 1 hashtag (a single trigger from event_type alone is C, not B)
- D = 0 hashtags

CFO STATUS:
- "New" if event_type is "cfo_hire"
- "Unable to verify" otherwise

For research_notes, use ONLY URLs that appear in the input above (article URL, \
company URLs, revenue source URLs). NEVER invent sources.

OUTPUT — return ONLY valid JSON (no markdown fences, no preamble):
{{
  "grade": "A|B|C|D",
  "hashtags": ["#X", "#Y"],
  "cfo_status": "New|Unable to verify",
  "grade_justification": "one sentence",
  "research_notes": [
    {{"finding": "what was found", "source_url": "URL from input"}},
    {{"finding": "...", "source_url": "..."}},
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


def grade_event(event: dict, companies_data: list) -> dict:
    """Apply TAL V10.2 grading rules. Returns dict with grade/hashtags/etc.
    On any failure returns an empty-graded record so the rest of the pipeline
    can still write the event."""
    empty = {
        'grade': None,
        'hashtags': [],
        'cfo_status': None,
        'grade_justification': None,
        'research_notes': [],
    }
    if not companies_data:
        return empty

    prompt = TAL_GRADING_PROMPT.format(
        title=event.get('title', ''),
        event_type=event.get('event_type', ''),
        article_url=event.get('source_url') or event.get('url') or '',
        description=(event.get('description') or '')[:600],
        companies_block=_build_companies_block(companies_data),
    )
    data = llm_json(prompt, max_tokens=800)
    if not data:
        return empty

    # Validate + coerce
    grade = (data.get('grade') or '').strip().upper()
    if grade not in ('A', 'B', 'C', 'D'):
        grade = None

    hashtags = data.get('hashtags') or []
    if isinstance(hashtags, str):
        # Defensive: model returned a string instead of array
        hashtags = [h.strip() for h in hashtags.split() if h.strip().startswith('#')]
    hashtags = [h for h in hashtags if isinstance(h, str) and h.startswith('#')][:6]

    notes = data.get('research_notes') or []
    if not isinstance(notes, list):
        notes = []
    notes = [
        {'finding': str(n.get('finding', ''))[:300],
         'source_url': str(n.get('source_url', ''))[:500]}
        for n in notes if isinstance(n, dict) and n.get('finding')
    ][:6]

    return {
        'grade': grade,
        'hashtags': hashtags,
        'cfo_status': (data.get('cfo_status') or '').strip() or None,
        'grade_justification': (data.get('grade_justification') or '').strip() or None,
        'research_notes': notes,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def enrich_events(
    limit: int = None,
    re_enrich: bool = False,
    dry_run: bool = False,
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
             f'({"claude-3-5-haiku" if backend == "anthropic" else OLLAMA_MODEL})')

    # ── Fetch events ──────────────────────────────────────────────────────
    query = client.table('events').select(
        'id, company_name, event_type, title, description'
    )
    if not re_enrich and col_ok.get('enriched_at'):
        query = query.is_('enriched_at', 'null')

    result = query.order('discovered_at', desc=True).execute()
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
    tavily_calls = 0
    ok = fail = 0

    for idx, event in enumerate(events, 1):
        eid   = event['id']
        title = (event.get('title') or '')[:80]
        etype = event.get('event_type', '')
        log.info(f'[{idx}/{len(events)}] {title}')

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

            # Build an industry hint from the event type + role to disambiguate
            hint_parts = []
            if etype in ('MERGER_ACQUISITION', 'FUNDING'):
                hint_parts.append('financial services private equity')
            if etype == 'EXECUTIVE_HIRE':
                hint_parts.append('B2B company')
            # Add the role context too
            if role in ('Acquirer', 'Target', 'Portfolio Company', 'Hiring Company'):
                hint_parts.append('North America')
            industry_hint = ' '.join(hint_parts)

            if cache_key not in firm_cache:
                log.info(f'  → Searching: {name}')
                if not dry_run:
                    firm_cache[cache_key] = enrich_one_company(name, industry_hint)
                    tavily_calls += 1
                    time.sleep(RATE_LIMIT_SECONDS)
                else:
                    firm_cache[cache_key] = {
                        'url': None, 'industry': None, 'size': None,
                        'revenue': None, 'revenue_source': None,
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
        primary = next(
            (c for c in enriched
             if str(c.get('role', '')).lower() in PRIMARY_ROLES),
            enriched[0]
        )
        blocked, kw = industry_is_blocked(primary.get('industry') or '')
        if blocked:
            log.info(
                f'  🚫 Post-enrichment block — industry '
                f'"{primary.get("industry")}" matched "{kw}". Deleting event.'
            )
            if not dry_run:
                try:
                    client.table('events').delete().eq('id', eid).execute()
                except Exception as e:
                    log.error(f'    Delete failed: {e}')
            ok += 1  # count as processed (not failed)
            continue

        # ── 4. TAL V10.2 grading ──────────────────────────────────────────
        log.info(f'  Grading via TAL V10.2…')
        grading = grade_event(event, enriched)
        if grading.get('grade'):
            log.info(
                f'    Grade={grading["grade"]}  '
                f'Hashtags={" ".join(grading["hashtags"]) or "(none)"}'
            )

        # ── 5. Write to Supabase ──────────────────────────────────────────
        if dry_run:
            log.info(f'  Would write {len(enriched)} company record(s) + grade')
            ok += 1
            continue

        payload = {
            'companies_data':      enriched,
            'grade':               grading.get('grade'),
            'hashtags':            grading.get('hashtags') or [],
            'grade_justification': grading.get('grade_justification'),
            'cfo_status':          grading.get('cfo_status'),
            'research_notes':      grading.get('research_notes') or [],
        }
        if col_ok.get('enriched_at'):
            payload['enriched_at'] = datetime.utcnow().isoformat()

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
    log.info(
        f'Done — enriched: {ok}, failed: {fail}, '
        f'Tavily searches: {tavily_calls}'
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    p = argparse.ArgumentParser(
        description='Enrich trigger events with multi-company firmographic data'
    )
    p.add_argument('--limit',     type=int, default=None,
                   help='Max events to process (default: all)')
    p.add_argument('--re-enrich', action='store_true',
                   help='Re-process already-enriched events')
    p.add_argument('--dry-run',   action='store_true',
                   help='Preview without writing to Supabase')
    args = p.parse_args()

    enrich_events(
        limit=args.limit,
        re_enrich=args.re_enrich,
        dry_run=args.dry_run,
    )
