#!/usr/bin/env python3
"""
enrichment_scout.py — Multi-company enrichment for trigger events.

For every event, this script:
  1. Reads the title + description and asks Ollama to identify ALL companies
     involved and their roles (Acquirer/Target, Investor/Portfolio Co., etc.)
  2. Searches Tavily for each company's firmographic data
  3. Uses Ollama to extract: website URL, industry, employee size, HQ, LinkedIn
  4. Writes the full list as a JSONB array to events.companies_data in Supabase

Company firmographics are cached per run so the same company in multiple events
is only searched once.

Prerequisites:
    pip install requests supabase python-dotenv
    Ollama must be running locally with qwen2.5:14b

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
TAVILY_API_KEY = os.environ.get(
    'TAVILY_API_KEY',
    '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'
)
OLLAMA_URL   = os.environ.get('OLLAMA_URL',   'http://localhost:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'qwen2.5:14b')

RATE_LIMIT_SECONDS = 1.2   # between Tavily calls
MIGRATION_SQL = """\
Run this in your Supabase SQL Editor (Project → SQL Editor):

    ALTER TABLE events ADD COLUMN IF NOT EXISTS companies_data JSONB;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS enriched_at    TIMESTAMPTZ;
"""

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)


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
    """Return which enrichment columns already exist."""
    exists = {}
    for col in ('companies_data', 'enriched_at'):
        try:
            client.table('events').select(col).limit(1).execute()
            exists[col] = True
        except Exception:
            exists[col] = False
    return exists


# ── Ollama helpers ────────────────────────────────────────────────────────────

def ollama_json(prompt: str, max_tokens: int = 600) -> dict:
    """Call Ollama and return parsed JSON. Returns {} on failure."""
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


# ── Step 1 — Extract companies from event text ────────────────────────────────

EXTRACT_PROMPT = '''\
You are a business analyst reading a news event. Identify every real company \
(business entity) mentioned and its role in the story.

Event type: {event_type}
Title: {title}
Description: {description}

Return ONLY a JSON object with one key "companies" containing an array. \
Each element has "name" (exact company name) and "role" (their role). \
Use these role labels:
  M&A:          "Acquirer", "Target", "Advisor"
  Funding:      "Portfolio Company", "Lead Investor", "Investor"
  CFO/Exec hire:"Hiring Company", "Previous Employer"
  Other:        "Primary", "Partner", "Mentioned"

Rules:
- Only include real, named companies (not people, governments, or vague terms).
- If only one company is relevant, still return an array of one.
- Maximum 5 companies.
- If no companies can be identified, return {{"companies": []}}.

Example output:
{{"companies": [{{"name": "Acme Corp", "role": "Acquirer"}}, \
{{"name": "Beta Inc", "role": "Target"}}]}}'''


def extract_event_companies(event: dict) -> list:
    """Ask Ollama to identify all companies and their roles in one event."""
    prompt = EXTRACT_PROMPT.format(
        event_type=event.get('event_type', ''),
        title=event.get('title', '')[:200],
        description=(event.get('description', '') or '')[:600],
    )
    data = ollama_json(prompt, max_tokens=400)
    companies = data.get('companies', [])

    # Validate shape
    valid = []
    for c in companies:
        name = (c.get('name') or '').strip()
        role = (c.get('role') or 'Mentioned').strip()
        if name and len(name) > 1 and name.lower() not in (
            'unknown', 'nan', 'none', ''
        ):
            valid.append({'name': name, 'role': role})
    return valid[:5]


# ── Step 2 — Search Tavily for one company ────────────────────────────────────

def tavily_search(company_name: str) -> dict:
    """Return Tavily search results for a company. Returns {} on failure."""
    query = (
        f'"{company_name}" company official website '
        f'headquarters industry employees'
    )
    try:
        resp = requests.post(
            'https://api.tavily.com/search',
            json={
                'api_key':       TAVILY_API_KEY,
                'query':         query,
                'max_results':   5,
                'search_depth':  'basic',
                'include_answer': True,
            },
            timeout=20
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f'  Tavily error for "{company_name}": {e}')
        return {}


# ── Step 3 — Extract firmographics from search results ────────────────────────

FIRMOGRAPHIC_PROMPT = '''\
Extract company firmographic data from the search results below.

Company: "{company_name}"

Search results:
{results_text}

Return ONLY a JSON object (no markdown, no explanation) with these keys \
(null if unknown):
{{
  "url":      "official website URL (https://...) or null",
  "industry": "specific industry, e.g. 'Healthcare Technology', \
'Commercial Banking', 'B2B SaaS', 'Insurance', 'Private Equity', \
'Utilities' — be specific, not generic, or null",
  "size":     "one of: '1-50', '51-200', '201-500', '501-1000', \
'1001-5000', '5000+', or null",
  "hq":       "City, ST format e.g. 'Boston, MA' or 'Toronto, ON', or null",
  "linkedin": "full https://www.linkedin.com/company/... URL or null"
}}'''


def enrich_one_company(company_name: str) -> dict:
    """
    Search Tavily + extract firmographics for one company name.
    Returns a dict with keys: url, industry, size, hq, linkedin (all may be None).
    """
    empty = {'url': None, 'industry': None, 'size': None,
             'hq': None, 'linkedin': None}

    search = tavily_search(company_name)
    if not search.get('results'):
        return empty

    answer  = search.get('answer', '')
    results = search.get('results', [])

    lines = []
    if answer:
        lines.append(f'Summary: {answer}\n')
    for r in results[:5]:
        lines.append(
            f"- {r.get('title','')}\n"
            f"  {r.get('url','')}\n"
            f"  {(r.get('content','') or '')[:300]}\n"
        )

    prompt = FIRMOGRAPHIC_PROMPT.format(
        company_name=company_name,
        results_text='\n'.join(lines).strip()
    )
    data = ollama_json(prompt, max_tokens=400)

    return {
        'url':      data.get('url')      or None,
        'industry': data.get('industry') or None,
        'size':     data.get('size')     or None,
        'hq':       data.get('hq')       or None,
        'linkedin': data.get('linkedin') or None,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def enrich_events(
    limit: int = None,
    re_enrich: bool = False,
    dry_run: bool = False,
):
    client = get_supabase()
    col_exists = check_columns(client)

    if not col_exists.get('companies_data'):
        log.warning('companies_data column missing from Supabase.')
        log.warning(MIGRATION_SQL)
        if not dry_run:
            sys.exit(1)

    # ── Fetch events ──────────────────────────────────────────────────────
    query = client.table('events').select(
        'id, company_name, event_type, title, description'
    )
    if not re_enrich and col_exists.get('enriched_at'):
        query = query.is_('enriched_at', 'null')

    result = query.order('discovered_at', desc=True).execute()
    events = result.data or []

    if limit:
        events = events[:limit]

    if not events:
        log.info('No unenriched events found — nothing to do.')
        return

    tag = 'DRY RUN — ' if dry_run else ''
    log.info(
        f'{tag}Processing {len(events)} event(s) '
        f'with Ollama ({OLLAMA_MODEL}) + Tavily'
    )
    print()

    # Firmographic cache: company_name_lower → {url, industry, size, hq, linkedin}
    firm_cache: dict = {}
    tavily_calls = 0

    ok = fail = 0

    for idx, event in enumerate(events, 1):
        eid   = event['id']
        title = (event.get('title') or '')[:80]
        log.info(f'[{idx}/{len(events)}] {title}')

        # ── 1. Extract companies + roles ──────────────────────────────────
        companies = extract_event_companies(event)

        if not companies:
            # Fall back to the stored company_name if Ollama found nothing
            fallback = (event.get('company_name') or '').strip()
            if fallback and fallback.lower() not in ('unknown', 'unknown company', 'nan'):
                companies = [{'name': fallback, 'role': 'Primary'}]

        if not companies:
            log.info('  No companies identified — skipping')
            if not dry_run and col_exists.get('enriched_at'):
                # Mark as processed so we don't retry forever
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
        enriched_companies = []
        for co in companies:
            name     = co['name']
            role     = co['role']
            cache_key = name.lower().strip()

            if cache_key not in firm_cache:
                log.info(f'  → Searching: {name}')
                if not dry_run:
                    firm_cache[cache_key] = enrich_one_company(name)
                    tavily_calls += 1
                    time.sleep(RATE_LIMIT_SECONDS)
                else:
                    firm_cache[cache_key] = {
                        'url': None, 'industry': None,
                        'size': None, 'hq': None, 'linkedin': None
                    }
            else:
                log.info(f'  → Cached:   {name}')

            firm = firm_cache[cache_key]
            found = [f'{k}: {v}' for k, v in firm.items() if v]
            if found:
                log.info(f'     {" | ".join(found)}')

            enriched_companies.append({
                'name':     name,
                'role':     role,
                'url':      firm.get('url'),
                'industry': firm.get('industry'),
                'size':     firm.get('size'),
                'hq':       firm.get('hq'),
                'linkedin': firm.get('linkedin'),
            })

        # ── 3. Write to Supabase ──────────────────────────────────────────
        if dry_run:
            log.info(f'  Would write {len(enriched_companies)} company record(s)')
            ok += 1
            continue

        payload = {'companies_data': enriched_companies}
        if col_exists.get('enriched_at'):
            payload['enriched_at'] = datetime.utcnow().isoformat()

        try:
            client.table('events').update(payload).eq('id', eid).execute()
            ok += 1
        except Exception as e:
            err = str(e)
            if 'does not exist' in err:
                log.error(f'  Write failed — missing column. {MIGRATION_SQL}')
            else:
                log.error(f'  Supabase write failed: {e}')
            fail += 1

    print()
    log.info(
        f'Done — enriched: {ok}, failed: {fail}, '
        f'Tavily searches: {tavily_calls}, '
        f'cache hits: {sum(1 for _ in firm_cache) - tavily_calls if tavily_calls else 0}'
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
