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
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS research_notes       JSONB;\n"
    "  -- TAL V11 (added 2026-06-09):\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS confidence_level     TEXT;\n"
    "  ALTER TABLE events ADD COLUMN IF NOT EXISTS numeric_score        INTEGER;"
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
    # ── Tech/SaaS — off-target per user FY27, added 2026-06-09 ─────────
    'b2b saas', 'saas', 'software-as-a-service', 'software development',
    'software company', 'application software', 'enterprise software',
    'computer software', 'cloud software',
    # ── Healthcare/Pharma — off-target per user FY27, added 2026-06-09 ─
    # Note: NOT blocking bare 'healthcare' to avoid false-match on
    # 'Healthcare Foundation' / nonprofit charitable orgs which ARE target.
    'healthcare services', 'healthcare it', 'health it',
    'hospitals and health care', 'hospitals',
    'biotechnology', 'biotech',
    'pharmaceuticals', 'pharmaceutical',
    'medical devices', 'medical equipment',
    'senior living', 'assisted living',
    'life sciences tools', 'life sciences services',
    'clinical research',
    # Hardware/embedded (off-target)
    'computer hardware', 'computer hardware manufacturing',
    'embedded hardware', 'embedded systems',
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


# ── Step 4 — TAL grading (TAL V11 system, adapted for our pipeline flow) ────
#
# V11 replaces V10.2's "count hashtags + triggers" with a POINT-BASED scoring
# rubric. Each hashtag has explicit points; sum = numeric_score; score maps
# to grade (A=8+, B=5-7, C=2-4, D=0-1). New fields: confidence_level,
# numeric_score. New hashtag: #NewController.
#
# Important behavior change: solo CFO hires drop from B → C under V11 (vs
# V10.2 which auto-bumped to B). The previous code-side "finance leadership
# override" is REMOVED — scoring handles it naturally and more nuanced
# (CFO + 100EE = 6 = B, CFO + funding = 7 = B, CFO + PE + funding = 10 = A).

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

CORE RULES
- Prefer evidence over assumptions.
- If a fact cannot be verified from the input above, treat it as missing.
- Missing evidence lowers confidence but does not block grading.
- Use "Unable to Grade" ONLY if the company cannot be reasonably identified.
- Evaluate the PRIMARY company (Acquirer / Hiring Company / Portfolio Company \
/ Primary role). Do not inherit attributes across companies in this event.

HASHTAGS — use ONLY these and ONLY when evidence supports them. Each has \
a fixed point value. Sum all applicable points = numeric_score.

HIGH-INTENT TRIGGERS:
- **#NewCFO (+4)** — CFO or CFO-equivalent (Chief Financial Officer, VP \
Finance, Head of Finance, Director of Finance, Chief Financial) hired \
within last 18 months. Apply if event_type=cfo_hire OR title/description \
states a new CFO/VP Finance/Director Finance hire. NOT for Controllers — \
use #NewController instead.
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
- **#HyperGrowth (+2)** — Documented rapid growth, major hiring, Inc. 5000, \
or expansion. DO NOT apply for routine funding rounds.
- **#100EE (+2)** — Firmographic size 201-500 or larger. NOT 1-50 or 51-200.
- **#Locations (+2)** — Verified 3+ physical locations/offices/branches. \
DO NOT apply for HAVING a single HQ city.
- **#Entities (+2)** — Verified multiple subsidiaries/brands/legal entities.
- **#HoldCo (+2)** — Name contains "Holdings" OR industry is "Holding \
Companies & Conglomerates" OR description mentions multiple operating \
subsidiaries.
- **#Global (+2)** — Operations in 3+ countries OR HQ outside US/Canada.
- **#Franchisor (+2)** — Company franchises its brand to others.
- **#Franchisee (+2)** — Company operates franchised locations.
- **#Legacy (+2)** — Verified legacy ERP in use (QuickBooks, Sage 50, \
Dynamics GP, etc.). SKIP unless EXPLICITLY mentioned.

When in doubt, DROP the hashtag.

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
to the grade.

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
    'controller',
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


def grade_event(event: dict, companies_data: list) -> dict:
    """Apply TAL V11 grading rules. Returns dict with grade/hashtags/confidence/
    numeric_score/etc. On any failure returns an empty-graded record so the
    rest of the pipeline can still write the event.

    V11 NOTES:
    - Point-based scoring (vs V10.2's hashtag counting). LLM computes the sum.
    - No code-side finance leadership override — the scoring rubric handles
      it (CFO + 100EE = 6 = B, etc.).
    - New fields: confidence (High/Medium/Low), numeric_score (int).
    - Hashtag list is no longer capped at 6 — V11 says "use as many as
      evidence supports" — but defense-in-depth, we still cap at 8 to prevent
      runaway output."""
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

    prompt = TAL_GRADING_PROMPT.format(
        title=event.get('title', ''),
        event_type=event.get('event_type', ''),
        article_url=event.get('source_url') or event.get('url') or '',
        description=(event.get('description') or '')[:600],
        companies_block=_build_companies_block(companies_data),
    )
    data = llm_json(prompt, max_tokens=900)  # +100 for new fields
    if not data:
        return empty

    # ── Validate + coerce ────────────────────────────────────────────────
    grade_raw = (data.get('grade') or '').strip()
    # Accept the V11 letter grades + "Unable to Grade"
    if grade_raw.upper() in ('A', 'B', 'C', 'D'):
        grade = grade_raw.upper()
    elif grade_raw.lower() == 'unable to grade':
        grade = 'Unable to Grade'
    else:
        grade = None

    confidence = (data.get('confidence') or '').strip().title()  # "High"/"Medium"/"Low"
    if confidence not in ('High', 'Medium', 'Low'):
        confidence = None

    # numeric_score should be an integer >= 0
    try:
        numeric_score = int(data.get('numeric_score') or 0)
        if numeric_score < 0:
            numeric_score = None
    except (TypeError, ValueError):
        numeric_score = None

    hashtags = data.get('hashtags') or []
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in hashtags.split() if h.strip().startswith('#')]
    # Cap at 8 (defensive; V11 allows more but our schema doesn't need more)
    hashtags = [h for h in hashtags if isinstance(h, str) and h.startswith('#')][:8]

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
    # Include source_url so grade_event() can cite the original article in
    # research_notes. Without it, the TAL prompt receives empty article_url
    # and the LLM has no primary source to reference.
    query = client.table('events').select(
        'id, company_name, event_type, title, description, source_url'
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

            # Build an industry hint from the event type + role to disambiguate.
            # event_type is stored LOWERCASE per src/models.py (EventType enum
            # values are 'merger_acquisition', 'funding', 'cfo_hire', etc.) —
            # this check was previously wrong-cased so hints were never applied.
            etype_l = (etype or '').lower()
            hint_parts = []
            if etype_l in ('merger_acquisition', 'funding'):
                hint_parts.append('financial services private equity')
            if etype_l in ('executive_hire', 'cfo_hire'):
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
            'confidence_level':    grading.get('confidence'),
            'numeric_score':       grading.get('numeric_score'),
            'hashtags':            grading.get('hashtags') or [],
            'grade_justification': grading.get('grade_justification'),
            'cfo_status':          grading.get('cfo_status'),
            'research_notes':      grading.get('research_notes') or [],
        }
        if col_ok.get('enriched_at'):
            payload['enriched_at'] = datetime.utcnow().isoformat()

        # Upgrade event_type to cfo_hire if we detected finance leadership in
        # the text. Never downgrades — only changes executive_hire/other → cfo_hire.
        # This backfills correct classification on existing events when re-enriched,
        # and ensures dashboard CFO tab populates correctly.
        current_etype = (event.get('event_type') or '').lower()
        if (current_etype != 'cfo_hire'
                and _has_finance_leadership_trigger(event)):
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
    log.info(
        f'Done — enriched: {ok}, failed: {fail}, '
        f'Tavily searches: {tavily_calls}'
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

    # Fetch events that already have firmographic data
    query = client.table('events').select(
        'id, company_name, event_type, title, description, '
        'source_url, companies_data'
    ).not_.is_('companies_data', 'null')
    result = query.order('discovered_at', desc=True).execute()
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
        primary = next(
            (c for c in cd
             if str(c.get('role', '')).lower() in PRIMARY_ROLES),
            cd[0]
        )
        blocked, kw = industry_is_blocked(primary.get('industry') or '')
        if blocked:
            log.info(
                f'  🚫 Industry "{primary.get("industry")}" matched "{kw}" '
                f'→ delete'
            )
            if not dry_run:
                try:
                    client.table('events').delete().eq('id', eid).execute()
                    deleted += 1
                except Exception as e:
                    log.error(f'  Delete failed: {e}')
                    fail += 1
            else:
                deleted += 1
            continue

        # ── TAL grading (Ollama only, free) ───────────────────────────────
        grading = grade_event(event, cd)
        if grading.get('grade'):
            log.info(
                f'  Grade={grading["grade"]}  '
                f'Hashtags={" ".join(grading["hashtags"]) or "(none)"}'
            )

        if dry_run:
            ok += 1
            continue

        # ── Build payload (don't touch companies_data — we didn't change it) ─
        payload = {
            'grade':               grading.get('grade'),
            'confidence_level':    grading.get('confidence'),
            'numeric_score':       grading.get('numeric_score'),
            'hashtags':            grading.get('hashtags') or [],
            'grade_justification': grading.get('grade_justification'),
            'cfo_status':          grading.get('cfo_status'),
            'research_notes':      grading.get('research_notes') or [],
        }

        # event_type reclassification: only upgrade to cfo_hire when warranted
        current_etype = (event.get('event_type') or '').lower()
        if (current_etype != 'cfo_hire'
                and _has_finance_leadership_trigger(event)):
            payload['event_type'] = 'cfo_hire'
            upgraded += 1
            log.info(f'  Reclassifying event_type {current_etype!r} → cfo_hire')

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
    log.info('Tavily API calls used: 0  (regrade-only mode)')


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
        )
