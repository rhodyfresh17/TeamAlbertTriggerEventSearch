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
TAVILY_API_KEY    = os.environ.get('TAVILY_API_KEY',
    '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')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
OLLAMA_URL        = os.environ.get('OLLAMA_URL',   'http://localhost:11434')
# Default model — falls back to whatever is locally available. Override with
# `export OLLAMA_MODEL=...` to use a specific model (e.g. qwen2.5:14b).
OLLAMA_MODEL      = os.environ.get('OLLAMA_MODEL', 'qwen3-coder:30b')
CLAUDE_MODEL      = 'claude-3-5-haiku-20241022'

RATE_LIMIT_SECONDS = 1.2

MIGRATION_SQL = (
    "Run in Supabase SQL Editor:\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS companies_data JSONB;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS enriched_at    TIMESTAMPTZ;"
)

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
  "revenue":  "STRICT BUCKET. Must be EXACTLY one of these strings: \
'<$5M', '$5M-10M', '$10M-25M', '$25M-50M', '$50M-100M', '$100M-200M', \
'$200M-500M', '$500M-1B', '$1B+', or null. DO NOT return free-form values \
like '$27.9B' or '$50M' — map them to the bucket they fall into \
($27.9B → '$1B+', $50M → '$50M-100M', $18M → '$10M-25M'). Use null if \
revenue is not explicitly stated in the search results.",
  "hq":       "City, ST abbreviation (e.g. 'Boston, MA' or 'Toronto, ON'), \
US/Canada only unless clearly elsewhere — or null",
  "linkedin": "full https://www.linkedin.com/company/... URL or null"
}}'''


def enrich_one_company(company_name: str, industry_hint: str = '') -> dict:
    empty = {'url': None, 'industry': None, 'size': None, 'revenue': None,
             'hq': None, 'linkedin': None}

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
    data = llm_json(prompt, max_tokens=500)

    return {
        'url':      data.get('url')      or None,
        'industry': data.get('industry') or None,
        'size':     data.get('size')     or None,
        'revenue':  data.get('revenue')  or None,
        'hq':       data.get('hq')       or None,
        'linkedin': data.get('linkedin') or None,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def enrich_events(
    limit: int = None,
    re_enrich: bool = False,
    dry_run: bool = False,
):
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
                        'revenue': None, 'hq': None, 'linkedin': None
                    }
            else:
                log.info(f'  → Cached:   {name}')

            firm = firm_cache[cache_key]
            found = [f'{k}: {v}' for k, v in firm.items() if v]
            if found:
                log.info(f'     {" | ".join(found)}')

            enriched.append({
                'name':     name,
                'role':     role,
                'url':      firm.get('url'),
                'industry': firm.get('industry'),
                'size':     firm.get('size'),
                'revenue':  firm.get('revenue'),
                'hq':       firm.get('hq'),
                'linkedin': firm.get('linkedin'),
            })

        # ── 3. Write to Supabase ──────────────────────────────────────────
        if dry_run:
            log.info(f'  Would write {len(enriched)} company record(s)')
            ok += 1
            continue

        payload = {'companies_data': enriched}
        if col_ok.get('enriched_at'):
            payload['enriched_at'] = datetime.utcnow().isoformat()

        try:
            client.table('events').update(payload).eq('id', eid).execute()
            ok += 1
        except Exception as e:
            if 'does not exist' in str(e):
                log.error(f'  Write failed — missing column. {MIGRATION_SQL}')
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
