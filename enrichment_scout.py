#!/usr/bin/env python3
"""
enrichment_scout.py — Multi-company enrichment for trigger events.

For every event, this script (Phase 2 order, 2026-09-07 — CLASSIFY, then
RESEARCH; every free signal is consumed before a search is spent):
  1. Reads the title + description and asks the local LLM to identify ALL
     companies involved and their roles (Acquirer/Target, Portfolio Co., etc.)
  2. Free gates: board-only, junk names, workable role, rep verdicts,
     structured SEC facts, entity shape — no LLM firmographics, no search.
  3. STAGE A (free): per company, structured seeds (SEC filer state, Form D
     revenue) → account firmographic cache → ONE article-only LLM pass that
     classifies zi_subindustry/hq with a confidence. A High-confidence OTHER
     (or a structured 'out') tombstones the event with ZERO searches.
  4. STAGE B (budgeted): survivors with something left to learn get the
     Firecrawl/Tavily ladder (SearchBudget per tier), merged fill-if-missing.
  5. Fit gates → probes → TAL grading → write companies_data / fit / grade,
     plus the Phase 2 typed columns (verify_state, retry_after, …) when the
     migration has been run — probed at start, JSON-only otherwise.

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
    python enrichment_scout.py                  # Enrich unenriched events (retry_after honoured)
    python enrichment_scout.py --limit 10       # Process up to 10 events
    python enrichment_scout.py --re-enrich      # Re-enrich already-enriched events
    python enrichment_scout.py --re-enrich --reverify-unverified   # staged/ambiguous, ranked, ≤50
    python enrichment_scout.py --regrade-only --event-type finance_seat_open  # free regrade
    python enrichment_scout.py --dry-run        # No searches, no writes (local LLM still runs)

A single-instance run lock (state/enrichment.lock) makes an overlapping
launchd + terminal run exit 0 instead of double-spending the search budget.
"""

import os
import re
import sys
import copy
import json
import inspect
import time
import sqlite3
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import urlparse
import subprocess

import requests

# v2 deterministic gates (2026-09-06) — pure functions, no network. Policy
# (A.J.'s exclusions, SIC/Form D routing, territory parsing) lives there.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.pipeline.gates import (  # noqa: E402
    hq_territory_status as _gates_hq_status, is_non_operating_entity,
    is_bad_company_name, sic_to_verdict, formd_to_verdict,
    account_key as _gates_account_key, hq_state_code as _gates_hq_state_code,
)
# Phase 2 (2026-09-07): account-keyed search/firmographic/negative cache,
# single-instance run lock, and the typed-column layer (probed per run —
# the migration is a manual step A.J. runs later; JSON-only until then).
from src.pipeline.cache import AccountCache  # noqa: E402
from src.pipeline.runlock import RunLock  # noqa: E402
from src.pipeline.typed import (  # noqa: E402
    TYPED_EVENT_COLUMNS, probe_columns, verify_state_for, retry_after_for,
    llm_retry_after, typed_payload, not_fit_payload, MAX_ENRICH_ATTEMPTS,
    LLM_RETRY_HOURS, parse_ts, ProbeUnavailable, is_schema_error,
)
# Phase 3 slice B2 (2026-09-08): free registries — SEC IAPD advisers, FDIC
# banks, ProPublica nonprofits — settle territory / vertical / revenue / url
# for those account shapes with ZERO search (src/pipeline/oracles.py).
from src.pipeline import oracles as _oracles  # noqa: E402
# Phase 3 slice B4 (another engineer's module): free domain resolution. The
# hook below is a no-op until it lands — never a reason enrichment can't run.
try:
    from src.pipeline.domains import resolve as _resolve_domain  # noqa: E402
except ImportError:
    _resolve_domain = None
# Phase 4 slice C2 (2026-09-08): the finance-leader hire SUBJECT detector
# (replaces the substring checks the #NewCFO / #NewController guard and
# _finance_role used) and the declarative hashtag guard table applied to
# the grader's list before the score is computed.
from src.pipeline.hires import finance_hire_subject, NEW_CFO_ROLES, NEW_CONTROLLER_ROLES  # noqa: E402
from src.pipeline.hashtag_guards import apply_guards, parse_funding_amount, strip_count  # noqa: E402
# Phase 4 slice C1 (another engineer's module, written concurrently): the
# accounts table — one grade per account, enrich-once, rep verdicts.
# Guarded import: every hook below is a no-op until the module lands, and
# accounts.probe_accounts() answers False until A.J. runs
# supabase/migrations/003_accounts.sql, so the events path never depends on it.
try:
    from src.pipeline import accounts as _accounts  # noqa: E402
except ImportError:
    _accounts = None

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

# Phase 3 B2 oracles (2026-09-08): the monthly registry tables
# (scripts/refresh_oracles.py → state/oracles.db) and whether the LIVE
# adapters (FDIC wildcard search, ProPublica) may run in this process —
# ORACLES_LIVE_ENABLED=0 keeps a run on the local tables (outage, tests).
# A registry answer below ORACLE_MIN_CONFIDENCE is a candidate, not a fact.
ORACLES_DB_PATH       = os.environ.get('ORACLES_DB_PATH', _oracles.DEFAULT_DB_PATH)
ORACLES_LIVE_ENABLED  = (os.environ.get('ORACLES_LIVE_ENABLED', '1').strip().lower()
                         not in ('0', 'false', 'no', 'off'))
ORACLE_MIN_CONFIDENCE = _oracles.MATCH_THRESHOLD

# Article-first classification (Phase 2, 2026-09-07). When the article-only
# LLM pass says a company is OTHER (off-vertical), how sure must it be before
# the event is tombstoned WITHOUT spending a search? 'High' (default) |
# 'Medium' | 'never' (always research an OTHER). A benchmark decides the
# final value; a structured SEC 'out' verdict tombstones regardless.
# Benchmark 2026-09-07 (120 decided rows): P(out | OTHER-High) = 0.974, so
# 'High' stays. Guard added on review the same day: an article-only OTHER
# may tombstone only when the article gave the model something to read —
# a description LONGER than ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS. A
# one-line stub ("X names Y as CFO") earns High confidence from the name
# alone; those fall through to Stage B. Structured SEC 'out' is unaffected.
ARTICLE_OTHER_TOMBSTONE_MIN_CONFIDENCE = 'High'
ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS = 150

# Enrich ONCE per account (Phase 4 slice C2, 2026-09-08 — plan approved by
# A.J. 2026-09-06). When the accounts table holds a VERIFIED row for the
# chosen company whose firmographics were refreshed within this many days,
# Stage A takes the row's facts (provenance 'account' — settled, like the
# AccountCache) and Stage B / the probes spend nothing; only the NEW
# trigger is graded. 90 days matches the AccountCache's revenue TTL, the
# shortest-lived fit-relevant field.
ACCOUNT_FRESH_DAYS = 90

# Local-LLM availability (Phase 2). llama.cpp being DOWN is not the same as it
# answering badly: a connection error or a 5xx flips 'unavailable' (a read
# timeout does NOT — the shared server is alive, just busy; review
# 2026-09-07); any successful llm_json() clears it. enrich_events() runs a
# canary before touching the queue and re-checks after every event — an
# unavailable event is neither stamped nor tombstoned, only pushed out by
# LLM_RETRY_HOURS. LLM_UNAVAILABLE_STOP_AFTER consecutive ones re-run the
# canary and end the run only if the canary fails too.
LLM_STATE = {'unavailable': False, 'consecutive': 0}
LLM_UNAVAILABLE_STOP_AFTER = 3

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
    """'in' | 'out' | 'unknown'. Delegates to src.pipeline.gates (v2): scans
    EVERY comma segment, so 'Boston, MA, USA' is IN and 'Seattle, Washington'
    is confidently OUT (v1 read only the last segment → 'unknown')."""
    return _gates_hq_status(hq)


# Roles that can carry a WORKABLE account. A fitting company in one of these
# roles keeps the event alive. Previous Employer / Advisor / Partner /
# Mentioned never do — they're context, not accounts to sell into.
WORKABLE_ROLES = [
    'acquirer', 'portfolio company', 'hiring company', 'primary', 'target',
]
# 'investor' / 'lead investor' were removed 2026-09-06: the company that got
# the money is the account; a PE/VC firm is an account only when the trigger
# is about the firm itself (then it is extracted as 'primary'/'hiring company').

# Rep verdicts are a HARD input to the pipeline (v2): these statuses mean
# never research / never surface this account again.
REP_NOT_FIT_STATUSES = {'Not a Fit', 'Out of Alignment', 'NetSuite Customer'}
REP_DECIDED_STATUSES = {'Picked Up', 'On Rep TAL'}


def _excluded_public_companies() -> set:
    """Mega-cap blocklist from config.yaml (territory.company_filters.
    excluded_public_companies) as account keys. Cached; empty if unreadable."""
    cache = getattr(_excluded_public_companies, '_cache', None)
    if cache is not None:
        return cache
    keys = set()
    try:
        import yaml
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
        cfg = yaml.safe_load(open(cfg_path)) or {}
        def _walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == 'excluded_public_companies' and isinstance(v, list):
                        for n in v:
                            if n:
                                keys.add(_gates_account_key(str(n)))
                    else:
                        _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)
        _walk(cfg)
    except Exception:
        pass
    _excluded_public_companies._cache = keys
    return keys


def _is_excluded_public_company(name: str) -> bool:
    k = _gates_account_key(name)
    if not k:
        return False
    ex = _excluded_public_companies()
    return k in ex or any(e and (e == k or k.startswith(e + ' ')) for e in ex)


def _load_rep_dispositions(client) -> dict:
    """{account_key: status} of every rep verdict.

    Phase 4 (2026-09-08): read through accounts.load_dispositions when the
    module is present — it merges the accounts table's disposition column
    with the legacy account_dispositions table, so a verdict recorded on
    either side short-circuits the next event (the acceptance test: a "Not
    a Fit" account's next event never reaches search). Each entry is keyed
    both by the key the module chose and by gates.account_key(name), the
    key _rep_verdict_for looks up with. The legacy read stays as the
    fallback for a missing module or a failing call — never an empty
    verdict set because of a code path."""
    if _accounts is not None:
        try:
            raw = _accounts.load_dispositions(client) or {}
            out = {}
            for key, entry in raw.items():
                status = entry.get('status') if isinstance(entry, dict) else entry
                if not status:
                    continue
                out[key] = status
                name = entry.get('name') if isinstance(entry, dict) else None
                if name:
                    out[_gates_account_key(str(name))] = status
            return out
        except Exception as e:      # noqa: BLE001 — fall back to the legacy table
            log.warning(f'accounts.load_dispositions failed ({e}) — reading account_dispositions')
    try:
        rows = client.table('account_dispositions').select('company_name,status').execute().data or []
        return {_gates_account_key(r.get('company_name') or ''): r.get('status')
                for r in rows if r.get('company_name')}
    except Exception:
        return {}


def _rep_verdict_for(companies: list, dispositions: dict):
    """First (name, status) among workable companies that a rep already decided."""
    if not dispositions:
        return None
    for c in companies:
        if (c.get('role') or '').lower() not in WORKABLE_ROLES:
            continue
        st = dispositions.get(_gates_account_key(c.get('name') or ''))
        if st:
            return c.get('name'), st
    return None


# Review 2026-09-08 (Phase 4): the structured verdict is moving to
# src/pipeline/structured.py (another engineer, a faithful copy of the
# function below) so the golden-set exporter and the scrapers can read it
# without importing this module. Re-exported from there when it has landed;
# the local definition stays as the fallback so this file works before and
# after — the two must stay behaviourally identical (tests pin a sample).
try:
    from src.pipeline.structured import structured_verdict as _structured_verdict  # noqa: E402
except ImportError:
    def _structured_verdict(event: dict) -> dict:
        """Free, deterministic pre-search verdict from structured facts the
        scrapers now embed in the description (SIC code; Form D industry group,
        declared revenue range, offering amount, SPAC flag)."""
        import re as _re
        desc = event.get('description') or ''
        out = {'verdict': 'unknown', 'reason': '', 'revenue_segment': ''}
        if 'sec.gov' not in (event.get('source_url') or ''):
            return out
        m = _re.search(r'SIC:\s*(\d{4})', desc)
        if m:
            v, why = sic_to_verdict(m.group(1))
            if v in ('out', 'vehicle'):
                return {'verdict': v, 'reason': why, 'revenue_segment': ''}
        if 'Form D' in (event.get('title') or ''):
            grp = (_re.search(r'industry group: ([^.]+)\.', desc) or [None, ''])[1]
            rr = (_re.search(r'Declared revenue: ([^.]+?)\.(?:\s|$)', desc) or [None, ''])[1]
            amt = _re.search(r'Total offering: \$([\d,]+)', desc)
            amount = float(amt.group(1).replace(',', '')) if amt else None
            spac = 'SPAC: yes' in desc
            v, seg, why = formd_to_verdict(grp.strip() if grp else None,
                                           rr.strip() if rr else None, amount, spac)
            return {'verdict': v, 'reason': why, 'revenue_segment': seg}
        return out



# P2 (A.J. 2026-09-04, applied review 2026-09-08): ALL K-12 — public,
# private, charter — is not a fit. The vertical gate FAILS the label instead
# of admitting it: a registry-confirmed K-12 org (oracles.zi_for_ntee, NTEE
# B2x) or an article/search classification of 'K-12 Schools' is out, the
# same way OTHER is. The label stays in ZI_SUBINDUSTRIES because
# monitor_health.py and dashboard.py import that dict as the subindustry →
# vertical LABEL map for already-verified rows (tests/test_monitor_health.py
# pins it); the FIT allowlist is ZI_IN_VERTICAL, which excludes it, so no
# gate can read K-12 as in-vertical. gates._K12 still tombstones school-
# NAMED entities on arrival; this catches the ones the registry classifies.
ZI_NOT_A_FIT = frozenset({'K-12 Schools'})
ZI_IN_VERTICAL = frozenset(k for k in ZI_SUBINDUSTRIES if k not in ZI_NOT_A_FIT)


def _is_iapd_event(event: dict) -> bool:
    """A 'New SEC-registered investment adviser' trigger from
    scripts/ria_trigger.py: typed source 'sec_iapd', or — when the typed
    column is not live — the adviserinfo.sec.gov source_url it always sets."""
    if (str((event or {}).get('source') or '')).strip().lower() == 'sec_iapd':
        return True
    try:
        host = (urlparse(str((event or {}).get('source_url') or '')).hostname or '').lower()
    except ValueError:
        return False
    return host == 'adviserinfo.sec.gov' or host.endswith('.adviserinfo.sec.gov')


def _entity_shape(name: str, descriptor: str = '', registry: str = None) -> tuple:
    """gates.is_non_operating_entity with ONE registry exemption (H3 / P4,
    review 2026-09-08): a firm the SEC lists as a Registered adviser is by
    construction the MANAGEMENT COMPANY — the fund vehicles it runs are not
    registrants — so its '... LP' / '... Fund ...' name must not read as a
    fund vehicle (950 of 8,571 in-territory registrants are named that way;
    'CONSTITUTION CAPITAL HORIZON ADVISOR, LP' was tombstoned on arrival).
    Only the fund_vehicle kind is exempt; spac / political / government /
    k12 / lodging / greek still apply. Anything but an sec_iapd registry
    (a news article naming the same firm) keeps the full test."""
    nonop, kind = is_non_operating_entity(name, descriptor)
    if nonop and kind == 'fund_vehicle' and registry == 'sec_iapd':
        return False, ''
    return nonop, kind


def _with_registry(rec: dict, registry: str = None) -> dict:
    """Stamp a companies_data record with the registry its event came from,
    so company_fit (and the re-verify path reading stored records) applies
    the same entity-shape exemption the pre-search gate did."""
    if registry:
        rec['registry_source'] = registry
    return rec


# Public school districts procure through RFPs — dead ends, never workable
# (A.J. 2026-08-09: "we don't like public school districts"). Name-pattern
# gate so it holds regardless of how the ZI classifier labels them.
# NOTE: A.J. 2026-09-04 extended this to ALL K-12 (see ZI_NOT_A_FIT above);
# this name gate remains for clearly-PUBLIC district naming.
_PUBLIC_DISTRICT_PATTERNS = (
    'school district', 'public schools', 'board of education',
    'unified school', 'school corporation', 'county schools',
    'city schools', 'school board', 'department of education',
)


def _is_public_school_district(name: str) -> bool:
    n = f' {(name or "").lower()} '
    return any(p in n for p in _PUBLIC_DISTRICT_PATTERNS)


def company_fit(c: dict) -> dict:
    """Fit for ONE company — the atom of the model (per A.J. 2026-07-17:
    'we should be filtering out at the company level based on industry/
    revenue/geography, not at the event level').

    Returns {'verdict': 'pass'|'fail'|'unverified', 'territory': ...,
             'revenue': ..., 'vertical': ..., 'reasons': [...]}"""
    _name = c.get('name') or ''
    if _is_public_school_district(_name):
        return {'verdict': 'fail', 'territory': 'n/a', 'revenue': 'n/a',
                'vertical': 'out',
                'reasons': ['public school district (RFP procurement — dead end)']}
    _nonop, _kind = _entity_shape(
        _name, c.get('descriptor') or c.get('industry') or '', c.get('registry_source'))
    if _nonop:
        return {'verdict': 'fail', 'territory': 'n/a', 'revenue': 'n/a',
                'vertical': 'out',
                'reasons': [f'entity_shape:{_kind} ({_name[:40]})']}
    if _is_excluded_public_company(_name):
        return {'verdict': 'fail', 'territory': 'n/a', 'revenue': 'out',
                'vertical': 'n/a',
                'reasons': ['excluded_public_company (mega-cap blocklist)']}

    territory = hq_territory_status(c.get('hq') or '')

    rev = (c.get('revenue') or '').strip()
    # `too_small` (Phase 3 B2): a registry estimate under the $5M bar —
    # "oracle_too_small: fdic est $3.3M" — fails revenue the way the Form D
    # declared-revenue rule does (gates.formd_to_verdict), no search spent.
    if c.get('too_small'):
        revenue = 'out'
    elif rev in IN_BAND_REVENUE:
        revenue = 'in'
    elif rev == 'Enterprise':
        revenue = 'out'
    else:
        revenue = 'unknown'

    zi = (c.get('zi_subindustry') or '').strip() or None
    if zi and zi in ZI_NOT_A_FIT:
        vertical = 'out'                        # P2: K-12 is never a fit
    elif zi and zi in ZI_IN_VERTICAL:
        vertical = 'in'
    elif zi and zi.upper() == 'OTHER':
        vertical = 'out'
    else:
        vertical = 'unknown'

    reasons = []
    if territory == 'out':
        reasons.append(f"HQ out of territory ({c.get('hq')})")
    if revenue == 'out':
        reasons.append(str(c['too_small']) if c.get('too_small')
                       else 'revenue Enterprise (>$100M, out of band)')
    if vertical == 'out':
        reasons.append(f'subindustry {zi} (A.J. 2026-09-04: all K-12 not a fit)'
                       if zi in ZI_NOT_A_FIT else 'subindustry OTHER (not a target vertical)')

    if reasons:
        verdict = 'fail'
    elif territory == 'in' and revenue == 'in' and vertical == 'in':
        verdict = 'pass'
    else:
        # v2 'unknown' semantics: an UNKNOWN VERTICAL is 'staged' (hidden by
        # default, retried free) — never shown as a workable account.
        # Unknown territory/revenue with a known vertical is 'unverified'
        # (hidden by default too, visible under the dashboard toggle).
        verdict = 'staged' if vertical == 'unknown' else 'unverified'
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
               or _pick([c for c in workable if c['fit']['verdict'] == 'staged'])
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
# httpx (the Supabase client's transport) logs every request at INFO, so
# probe_columns alone printed 19 'HTTP Request … 400' lines per run while it
# discovers which typed columns exist (review 2026-09-07). Warnings still show.
logging.getLogger('httpx').setLevel(logging.WARNING)
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
            LLM_STATE['unavailable'] = False
            return result
    result = _llamacpp_json(prompt, max_tokens)
    if result:
        LLM_STATE['unavailable'] = False
        return result
    log.warning('  llama.cpp empty/unreachable — falling back to Scout agent')
    result = _scout_json(prompt, max_tokens)
    if result:
        LLM_STATE['unavailable'] = False
    elif LLM_STATE['unavailable']:
        # Scout shares the same local server, so its failure confirms the
        # outage rather than covering it — leave 'unavailable' set.
        log.warning('  Scout fallback also failed — local LLM treated as unavailable')
    return result


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
    """Primary: local llama.cpp OpenAI-compatible endpoint, JSON mode, non-thinking.

    Availability bookkeeping (Phase 2): ONLY a connection error or a 5xx
    marks the server UNAVAILABLE (LLM_STATE) so the run can stop stamping
    rows. A READ timeout is a slow-but-alive shared server (the fleet's
    agents queue on the same :8091) — that is a bad answer for this prompt,
    not an outage, so it returns {} without the flag (review 2026-09-07).
    ConnectTimeout is a ConnectionError subclass in requests and stays an
    outage. A 200 whose body doesn't parse is likewise just a bad answer."""
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
    except requests.exceptions.ConnectionError as e:
        LLM_STATE['unavailable'] = True
        log.warning(f'  llama.cpp unreachable: {e}')
        return {}
    except requests.exceptions.Timeout as e:
        log.warning(f'  llama.cpp timed out (server busy, not down): {e}')
        return {}
    except Exception as e:
        log.warning(f'  llama.cpp error: {e}')
        return {}
    if resp.status_code >= 500:
        LLM_STATE['unavailable'] = True
        log.warning(f'  llama.cpp HTTP {resp.status_code} — server unavailable')
        return {}
    try:
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
    # matched_regions (scraper-owned, synced by supabase_sync) is the state
    # hint for the Phase 3 registry lookups; probed like the rest so a table
    # without it keeps working.
    for col in ('companies_data', 'enriched_at', 'fit', 'matched_regions'):
        try:
            client.table('events').select(col).limit(1).execute()
            exists[col] = True
        except Exception as e:      # noqa: BLE001 — classified below
            if is_schema_error(e):
                exists[col] = False
            else:
                # A timeout is not a missing column. 2026-09-11: a slow
                # Supabase made these probes fail, the run printed the
                # migration SQL for columns that exist and exited 1.
                raise ProbeUnavailable(f'events.{col}: '
                                       f'{" ".join(str(e).split())[:160]}') from e
    if not exists.get('fit'):
        log.warning(
            'fit column missing — fit-gate details (⚠️ verify flags) will '
            'not persist until you run:\n'
            '  ALTER TABLE events ADD COLUMN IF NOT EXISTS fit JSONB;'
        )
    # Phase 2 typed columns (002_v2_typed_columns.sql). A.J. runs the
    # migration by hand later, so every writer filters its typed payload to
    # this set and the JSON-only path is untouched until then.
    exists['typed'] = probe_columns(client, 'events', TYPED_EVENT_COLUMNS, strict=True)
    if exists['typed']:
        log.info(f'typed columns present: {len(exists["typed"])}/{len(TYPED_EVENT_COLUMNS)}')
    else:
        log.info('typed columns absent — JSON-only mode (run 002_v2_typed_columns.sql to enable)')
    return exists


def _soft_delete(client, event_id: str, reason: str, extra: dict = None,
                 typed: dict = None) -> None:
    """Tombstone an event: sets blocked_at (hidden from dashboard, immune to
    supabase_sync resurrection) + enriched_at (skipped by future enrichment).
    `extra` (e.g. companies_data / fit) is persisted too so paid research is
    never thrown away (v1 discarded it for ~52% of tombstones).
    `typed` (Phase 2) is the typed-column half — already filtered to the
    columns that exist (see _tombstone_typed) — merged verbatim, None values
    included, because a None there CLEARS a stale retry_after.
    Falls back to hard DELETE only if the blocked_at column is missing."""
    payload = {
        'blocked_at':     datetime.utcnow().isoformat(),
        'blocked_reason': (reason or '')[:300],
        'enriched_at':    datetime.utcnow().isoformat(),
    }
    for k, v in (extra or {}).items():
        if v is not None:
            payload[k] = v
    payload.update(typed or {})
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


def _firecrawl_search(company_name: str, industry_hint: str = ''):
    """Search via local self-hosted Firecrawl. Returns dict in the Tavily
    response shape so downstream code doesn't change. Free, unlimited,
    private. Default backend.

    Return contract (review 2026-09-07): a dict ONLY when Firecrawl actually
    ANSWERED — a genuine zero-result answer is {'answer': '', 'results': []}.
    None means it did not answer (connection error, timeout, non-2xx,
    success=false) and the caller must treat that like a throttle: the old
    {} on every failure made tavily_search negative-cache the account for
    7/30/90 days, so a 12-hour Firecrawl outage stamped 'known empty' on
    every account it touched."""
    query = _build_search_query(company_name, industry_hint)
    try:
        resp = requests.post(
            f'{FIRECRAWL_URL}/v1/search',
            json={'query': query, 'limit': 6},
            timeout=25  # Firecrawl can be slower than Tavily on first cold-cache
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f'  Firecrawl did not answer for "{company_name}": {e}')
        return None
    if not isinstance(data, dict) or not data.get('success'):
        log.warning(f'  Firecrawl returned success=false for "{company_name}" '
                    f'— treated as no answer, not as empty')
        return None
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


def _tavily_search(company_name: str, industry_hint: str = ''):
    """Search Tavily — kept as fallback when SEARCH_BACKEND='tavily' OR
    Firecrawl is unreachable. Costs quota; use sparingly.

    Same contract as _firecrawl_search (review 2026-09-07): None when Tavily
    did not answer (no key, transport error, non-2xx) — never {} — so a
    failed paid call can't be recorded as a 'paid' negative-cache strike."""
    if not TAVILY_API_KEY:
        return None
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
        data = resp.json()
    except Exception as e:
        log.warning(f'  Tavily did not answer for "{company_name}": {e}')
        return None
    return data if isinstance(data, dict) else None


# ── Persistent search cache (LEGACY) ────────────────────────────────────────
# Superseded by AccountCache (src/pipeline/cache.py) 2026-09-07: the key here
# is name||free-text hint, so the same account rarely hit twice and an empty
# result was re-searched on every event. _cache_get/_cache_set are kept for
# reference/rollback but are NO LONGER CALLED. The 3,469-row legacy
# firmographic_cache table is not migrated — cold start accepted.

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
# NOTE: no Brave rung, ever — the Hermes fleet's search depends on Brave
# and a TeamAlbert consumer would collide with it (A.J. 2026-08-09).
# 'lookups' = tavily_search calls that ENTERED the backend ladder (past the
# cache, the negative cache and the budget) — exactly one per call, so a
# Firecrawl→Tavily fallback is one lookup, not two (review 2026-09-07: the
# old summary summed the rungs and double-counted every fallback).
# 'firecrawl' counts lookups that reached Firecrawl; 'firecrawl_attempts'
# counts HTTP attempts (the empty-retry adds one); 'tavily' counts paid
# attempts; 'transport_failed' = a backend that did not ANSWER (connection
# error / non-2xx / success=false) — deferred, never a known empty.
# 'negative_cache' = lookups answered "known empty" without a search.
# 'oracle' = Stage A registry hits (Phase 3 B2) — facts that cost no search.
# 'account' = Stage A served from a fresh VERIFIED accounts-table row (Phase
# 4 enrich-once) — no LLM, no search, no probe.
SEARCH_COUNTS: Dict[str, int] = {'lookups': 0, 'cache': 0, 'negative_cache': 0,
                                 'firecrawl': 0, 'firecrawl_attempts': 0,
                                 'tavily': 0, 'throttled': 0,
                                 'transport_failed': 0, 'budget_skipped': 0,
                                 'oracle': 0, 'account': 0}


def reset_search_counts() -> None:
    """Zero the SEARCH_COUNTS dict — call at the start of each run."""
    for k in SEARCH_COUNTS:
        SEARCH_COUNTS[k] = 0


_ACCOUNT_CACHE = {'obj': None, 'path': None}


def _account_cache() -> AccountCache:
    """Module-level lazy AccountCache on CACHE_DB_PATH — re-opened when the
    path changes (tests point CACHE_DB_PATH at a temp file)."""
    if _ACCOUNT_CACHE['obj'] is None or _ACCOUNT_CACHE['path'] != CACHE_DB_PATH:
        _ACCOUNT_CACHE['obj'] = AccountCache(CACHE_DB_PATH)
        _ACCOUNT_CACHE['path'] = CACHE_DB_PATH
    return _ACCOUNT_CACHE['obj']


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


# ── Search-spend tiers (2026-08-14) ──────────────────────────────────────────
# 900 Tavily credits/month is the ONLY paid-quality search we have — it must
# be spent on the events most likely to become worked accounts, decided from
# FREE signals before any search fires (A.J.: "how are we making sure only
# the best possible companies are getting searched").
#   Tier 1 — full ladder incl. Tavily: finance-leader events (CFO/Controller
#            — the #1 trigger), M&A, funding raises ≥$1M or unknown size.
#   Tier 2 — scrape-only (Firecrawl, no Tavily): generic executive events
#            and everything else. Empty results stay unverified and get
#            retried on a later pass for free.
#   Tier 3 — NO searches at all: micro-raises (<$1M Form D offerings — too
#            small to be an up-market account). Graded from the filing text.
# The tier is set per-event in the enrich loop via _SEARCH_TIER (single-
# threaded process; default 1 so standalone probe callers keep full access).
_SEARCH_TIER = {'tier': 1}


def _event_search_tier(event: dict) -> int:
    et = (event.get('event_type') or '').strip()
    if et in ('cfo_hire', 'finance_seat_open') or _finance_role(event):
        return 1
    if et == 'merger_acquisition':
        return 1
    if et == 'funding':
        # A.J. 2026-08-14: "even a 1M raise isn't a company growing enough
        # to buy NetSuite" — the Tavily-worthy bar is $10M+.
        amt = _parse_funding_amount(
            f"{event.get('title') or ''} {event.get('description') or ''}")
        if amt is None:
            return 2       # undisclosed — free search only
        if amt >= 10_000_000:
            return 1
        if amt >= 1_000_000:
            return 2
        return 3           # micro-raise — not worth any search
    return 2


# Largest dollar amount in a text ('$6.8 Million', '$37M', '$1.2B'). Phase 4
# (2026-09-08): the parser lives in src/pipeline/hashtag_guards.py so the
# #Funding guard and the search tier read ONE implementation; the old name
# is kept for the callers and tests that use it.
_parse_funding_amount = parse_funding_amount


# ── IP-hygiene throttles (2026-08-09) ────────────────────────────────────────
# The scraping rungs (Firecrawl's Google scrape + SearXNG's engines) all fire
# from ONE residential IP that the whole Hermes fleet shares. Bulk passes ran
# ~600 scraped searches/hour for hours — a bot signature that got the IP
# CAPTCHA'd across engines and broke the fleet agents' web search too
# (A.J. 2026-08-09). Two mechanisms:
#
# 1. HOURLY CAP — cross-process sliding window in the cache DB (launchd
#    cycles + manual bulk passes share it). Over cap → scraping rungs are
#    skipped; API rungs (CSE/Tavily) still run. Bulk passes stretch out
#    instead of burning the IP.
# 2. CIRCUIT BREAKER — consecutive both-scrape-rungs-empty results mean the
#    IP is already being throttled; continuing to fire scrape attempts only
#    deepens the block. Breaker opens for a cool-down and the ladder runs
#    API-rungs-only until it closes.
SCRAPE_HOURLY_CAP = int(os.environ.get('SCRAPE_HOURLY_CAP', '150'))
_BREAKER_THRESHOLD = 6      # consecutive all-scrape-empty searches
_BREAKER_COOLDOWN = 900     # seconds the breaker stays open (15 min)
_breaker = {'streak': 0, 'open_until': 0.0}


def _scrape_budget_ok(record: bool = False) -> bool:
    """Sliding 1-hour window of scraped-search calls, shared across
    processes via the cache DB. Fail-open on any sqlite error."""
    try:
        now = time.time()
        conn = sqlite3.connect(CACHE_DB_PATH, timeout=5)
        cur = conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS scrape_calls (ts REAL)')
        cur.execute('DELETE FROM scrape_calls WHERE ts < ?', (now - 3600,))
        if record:
            cur.execute('INSERT INTO scrape_calls VALUES (?)', (now,))
        cur.execute('SELECT COUNT(*) FROM scrape_calls')
        n = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return n < SCRAPE_HOURLY_CAP
    except Exception:
        return True


_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state')


TRANSPORT_ABORT_EXIT = 2   # distinct from 1 (real failure): "could not reach Supabase"
_TRANSPORT_ABORT_FILE = 'enrichment_transport_aborts'


def _transport_aborts(bump: bool = False, reset: bool = False) -> int:
    """Consecutive runs that ended before doing anything because Supabase
    could not be probed (state/enrichment_transport_aborts). The launchd
    wrapper posts a soft notice on exit 2; monitor_health WARNs when the
    count reaches 2 — one blip self-heals in four hours, two in a row is a
    real outage (2026-09-11)."""
    path = os.path.join(_STATE_DIR, _TRANSPORT_ABORT_FILE)
    try:
        n = int(open(path).read().strip() or 0) if os.path.exists(path) else 0
    except Exception:
        n = 0
    if reset:
        n = 0
    elif bump:
        n += 1
    if bump or reset:
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
            with open(path, 'w') as fh:
                fh.write(str(n))
        except Exception as e:
            log.debug(f'  transport-abort counter not written: {e}')
    return n


def _abort_transport(err: Exception) -> None:
    """Supabase could not be probed: say so plainly, count it, exit 2.
    Nothing was processed and nothing was stamped, so the next cycle simply
    retries. Never print the migration SQL here — the schema is not the
    problem."""
    n = _transport_aborts(bump=True)
    log.error(f'Supabase unreachable or too slow to answer the schema probe '
              f'({err}) — nothing processed, nothing stamped; the next run '
              f'retries (consecutive: {n}). Not a missing column.')
    sys.exit(TRANSPORT_ABORT_EXIT)


def _search_mode_defer() -> bool:
    """monitor_health writes state/search_mode = 'defer' after the Firecrawl
    canary comes back empty twice. In defer mode nothing scrapes and nothing
    pays — events wait (enriched_at stays null) for a free retry."""
    try:
        with open(os.path.join(_STATE_DIR, 'search_mode')) as f:
            return f.read().strip().lower() == 'defer'
    except Exception:
        return False


def _scrape_rungs_available() -> bool:
    """True when it's civil to hit the scraping backends right now."""
    if _search_mode_defer():
        return False
    if time.time() < _breaker['open_until']:
        return False
    return _scrape_budget_ok()


# ── Tavily rationing (v2) ────────────────────────────────────────────────────
TAVILY_DAILY_RATION = int(os.environ.get('TAVILY_DAILY_RATION', '25'))


def _tavily_day_count(increment: bool = False) -> Optional[int]:
    """Today's Tavily calls (UTC day) from the cache DB. None on error so the
    caller can FAIL CLOSED (a budget you can't read is a budget you don't spend)."""
    day = datetime.utcnow().strftime('%Y-%m-%d')
    try:
        conn = sqlite3.connect(CACHE_DB_PATH, timeout=5)
        cur = conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS tavily_daily (day TEXT PRIMARY KEY, calls INTEGER)')
        if increment:
            cur.execute('INSERT INTO tavily_daily (day, calls) VALUES (?, 1) '
                        'ON CONFLICT(day) DO UPDATE SET calls = calls + 1', (day,))
            conn.commit()
        cur.execute('SELECT calls FROM tavily_daily WHERE day = ?', (day,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return None


def _tavily_budget_ok() -> bool:
    """Paid search allowed only when BOTH the monthly budget and the daily
    ration have room. Fails closed when either counter is unreadable."""
    try:
        month = _tavily_month_count()
    except Exception:
        return False
    day = _tavily_day_count()
    if day is None:
        return False
    return month < TAVILY_MONTHLY_BUDGET and day < TAVILY_DAILY_RATION


class SearchBudget:
    """Per-event search allowance (v2). tier1 = 2 searches, paid allowed on
    the firmographic lookup; tier2 = 1 scrape-only search; tier3 = none."""
    def __init__(self, tier: int):
        self.tier = tier
        self.max_searches = {1: 2, 2: 1}.get(tier, 0)
        self.allow_paid = tier == 1
        self.used = 0
        self.deferred = False

    def take(self) -> bool:
        if self.used >= self.max_searches:
            return False
        self.used += 1
        return True

    def exhausted(self) -> bool:
        return self.used >= self.max_searches


_BUDGET = {'obj': SearchBudget(1)}   # default: standalone callers keep full access


def _note_scrape_outcome(got_results: bool) -> None:
    """Feed the circuit breaker after a full scrape-rung attempt."""
    if got_results:
        _breaker['streak'] = 0
        return
    _breaker['streak'] += 1
    if _breaker['streak'] >= _BREAKER_THRESHOLD:
        _breaker['open_until'] = time.time() + _BREAKER_COOLDOWN
        _breaker['streak'] = 0
        log.warning(f'  🔌 Scrape circuit OPEN — {_BREAKER_THRESHOLD} '
                    f'consecutive empty scrape results (IP likely being '
                    f'throttled). Cooling down {_BREAKER_COOLDOWN // 60} min; '
                    f'API rungs only.')


def tavily_search(company_name: str, industry_hint: str = '',
                  paid_ok: bool = True, kind: str = None) -> dict:
    """Public search interface. Despite the legacy name, dispatches to the
    configured SEARCH_BACKEND (firecrawl by default) with persistent caching.
    Function name kept for backwards-compat with the rest of the file.

    v2 semantics: every call is charged against the per-event SearchBudget
    (tier); paid Tavily fires ONLY for tier-1 firmographic lookups
    (`paid_ok=True`), only after a genuine Firecrawl empty, and only while
    the monthly budget AND daily ration have room. When the scrape rungs are
    unavailable (cap / breaker / defer mode) the call returns
    {'deferred': True} so the event waits for a free retry instead of
    escalating to paid quota.

    Phase 2 (2026-09-07): results are keyed on account_key(company_name) +
    `kind` in the AccountCache (hit → no search), then the NEGATIVE cache is
    consulted — a known empty returns {} (NOT deferred: we looked, there is
    nothing) so an unfindable account stops costing a search per event —
    then the budget/rung ladder runs as before. A genuine empty is recorded
    at rung 'scrape' or 'paid' so a free miss never blocks a later paid try.

    Review 2026-09-07: only a backend that actually ANSWERED with zero
    results records an empty. A rung that did not answer (None from the
    backend helper — transport error, non-2xx, success=false) is treated
    like a throttle: it feeds the circuit breaker and the call returns
    {'deferred': True}, so an outage can never poison the negative cache."""
    if kind is None:
        kind = 'zoominfo' if industry_hint == 'zoominfo' else 'firmographic'
    key = _gates_account_key(company_name)
    cache = _account_cache()
    cached = cache.get_search(key, kind) if key else None
    if cached:
        SEARCH_COUNTS['cache'] += 1
        return cached

    budget = _BUDGET['obj']
    paid_allowed = (paid_ok and budget.allow_paid and bool(TAVILY_API_KEY)
                    and not _search_mode_defer())
    if key and cache.should_skip(key, kind, want_paid=paid_allowed):
        SEARCH_COUNTS['negative_cache'] += 1
        return {}
    if not budget.take():
        SEARCH_COUNTS['budget_skipped'] += 1
        return {}
    # One lookup per call that enters the ladder, whatever rungs it climbs
    # (the summary's 'Searches:' figure — review 2026-09-07).
    SEARCH_COUNTS['lookups'] += 1

    def _defer():
        budget.deferred = True
        return {'deferred': True}

    scrape_ran = tavily_ran = False   # a rung ANSWERED (empty or not)
    # Backend dispatch
    if SEARCH_BACKEND == 'tavily':
        if not (paid_allowed and _tavily_budget_ok()):
            return {}
        SEARCH_COUNTS['tavily'] += 1
        results = _tavily_search(company_name, industry_hint)
        if results is None:
            SEARCH_COUNTS['transport_failed'] += 1
            return _defer()
        tavily_ran = True
        if results.get('results'):
            _tavily_month_count(increment=True)
            _tavily_day_count(increment=True)
    elif SEARCH_BACKEND == 'firecrawl':
        results = {}
        scrape_ran = _scrape_rungs_available()
        if scrape_ran:
            SEARCH_COUNTS['firecrawl'] += 1            # one lookup …
            SEARCH_COUNTS['firecrawl_attempts'] += 1   # … one or two attempts
            _scrape_budget_ok(record=True)
            results = _firecrawl_search(company_name, industry_hint)
            if results is None or not results.get('results'):
                # Empty Firecrawl during bulk runs is usually TRANSIENT
                # upstream rate-limiting. One short wait + retry recovers
                # most of them for free.
                time.sleep(2.5)
                SEARCH_COUNTS['firecrawl_attempts'] += 1
                _scrape_budget_ok(record=True)
                results = _firecrawl_search(company_name, industry_hint)
            # NO SearXNG rung — the shared :8888 instance is FLEET
            # infrastructure and enrichment's fallback traffic got its
            # engines suspended twice (outages tracked the enrichment
            # schedule exactly; fleet had to move to Brave 2026-08-14).
            # This pipeline searches only via its own Firecrawl stack and
            # its own Tavily key. Never re-add shared-infra rungs.
            if results is None:
                # Firecrawl did not answer (both attempts). Counts toward
                # the breaker — a dead backend opens it after
                # _BREAKER_THRESHOLD lookups — but is NOT a known empty and
                # never escalates to paid quota: the event waits.
                SEARCH_COUNTS['transport_failed'] += 1
                _note_scrape_outcome(False)
                return _defer()
            _note_scrape_outcome(bool(results.get('results')))
        else:
            SEARCH_COUNTS['throttled'] += 1
        # Tavily fallback — ONLY after a genuine Firecrawl empty (never as a
        # substitute for a throttled scrape), only for tier-1 firmographic
        # lookups, only within the monthly budget AND the daily ration.
        if paid_allowed and scrape_ran and not results.get('results'):
            if _tavily_budget_ok():
                log.info('  → Firecrawl empty, falling back to Tavily')
                SEARCH_COUNTS['tavily'] += 1
                paid = _tavily_search(company_name, industry_hint)
                if paid is None:
                    # The paid rung did not answer; Firecrawl's genuine
                    # empty still stands, at the 'scrape' rung only.
                    SEARCH_COUNTS['transport_failed'] += 1
                else:
                    tavily_ran = True
                    results = paid
                    if results.get('results'):
                        _tavily_month_count(increment=True)
                        _tavily_day_count(increment=True)
            else:
                log.info(f'  → Firecrawl empty; Tavily ration exhausted '
                         f'(month {_tavily_month_count()}/{TAVILY_MONTHLY_BUDGET}, '
                         f'today {_tavily_day_count()}/{TAVILY_DAILY_RATION}) — deferring')
                return _defer()
        if not scrape_ran and not results.get('results'):
            # Throttled/defer mode: this is NOT 'searched and found nothing'.
            # Signal DEFER so the event is retried free on a later run.
            return _defer()
    else:
        log.warning(f'  Unknown SEARCH_BACKEND={SEARCH_BACKEND!r}, defaulting to firecrawl')
        SEARCH_COUNTS['firecrawl'] += 1
        SEARCH_COUNTS['firecrawl_attempts'] += 1
        results = _firecrawl_search(company_name, industry_hint)
        if results is None:
            SEARCH_COUNTS['transport_failed'] += 1
            _note_scrape_outcome(False)
            return _defer()
        scrape_ran = True

    if results.get('results'):
        if key:
            cache.set_search(key, kind, results)
            cache.clear_negative(key, kind)
    elif key and (scrape_ran or tavily_ran):
        # Genuine empty — a rung actually ANSWERED and found nothing.
        # Recorded at the highest rung that struck out; the ladder
        # (7/30/90d) lives in the cache module.
        cache.record_empty(key, kind, rung='paid' if tavily_ran else 'scrape')
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
statements). B2B data-aggregator profile snippets (zoominfo.com, \
rocketreach.co, growjo.com, leadiq.com) ARE valid sources for revenue, \
employee count, and HQ address — e.g. "\\$28.2 million in revenue and 191 \
employees ... located in Birmingham, Alabama" — cite the aggregator URL as \
revenue_source. NEVER guess from employee count or industry alone. If revenue \
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
  "linkedin": "full https://www.linkedin.com/company/... URL or null",
  "classification_confidence": "High|Medium|Low — how sure you are of \
zi_subindustry given the evidence (High only when the company's business is \
explicitly described)"
}}'''


_US_STATE_NAMES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT',
    'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
    'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME',
    'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI',
    'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO',
    'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM',
    'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND',
    'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA',
    'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
    'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY', 'district of columbia': 'DC',
    'ontario': 'ON', 'quebec': 'QC', 'new brunswick': 'NB',
    'nova scotia': 'NS', 'prince edward island': 'PE',
    'newfoundland and labrador': 'NL', 'newfoundland': 'NL',
}


def _parse_hq_size_from_snippets(results: list) -> dict:
    """Deterministically parse HQ city/state and employee count from
    aggregator search snippets — no LLM involved, so no extraction misses.

    Handles the two dominant phrasings:
      ZoomInfo /pic/:  "... is located in 850 X Pkwy Ste 200, Birmingham,
                        Alabama, 35209, United States and has 178 employees"
      RocketReach:     "... located in Birmingham, Alabama with $28.2
                        million in revenue and 191 employees"
    Returns {'hq': 'City, ST' | None, 'size': '178' | '201-500' | None}.
    """
    import re as _re
    hq = size = None
    state_alt = '|'.join(sorted(_US_STATE_NAMES, key=len, reverse=True))
    loc_re = _re.compile(
        r'located in (?:[^,]{1,60}, )*?([A-Z][A-Za-z .\'-]{1,30}), '
        r'(' + state_alt + r')\b', _re.I)
    emp_re = _re.compile(r'(?:has|and|with) ([\d,]+(?:-[\d,]+)?)\+? employees',
                         _re.I)
    for r in results or []:
        text = (r.get('content') or '')
        if not hq:
            m = loc_re.search(text)
            if m:
                city = m.group(1).strip()
                code = _US_STATE_NAMES.get(m.group(2).lower())
                if code and not city.isdigit():
                    hq = f'{city}, {code}'
        if not size:
            m = emp_re.search(text)
            if m:
                size = m.group(1).replace(',', '')
        if hq and size:
            break
    return {'hq': hq, 'size': size}


def enrich_one_company(company_name: str, industry_hint: str = '',
                       article_context: str = '',
                       no_search: bool = False,
                       require_search: bool = False) -> dict:
    """Firmographics for one company. `no_search=True` is the Phase 2 Stage A
    article-only pass (one local LLM call, zero searches); `require_search`
    makes an empty/deferred search return without an LLM call (Stage B —
    the article was already read in Stage A, re-reading it learns nothing).
    Adds `classification_confidence` (High|Medium|Low|None) and
    `classified_by` ('article' | 'search') to the returned dict."""
    classified_by = 'article' if no_search else 'search'
    empty = {'url': None, 'industry': None, 'zi_subindustry': None,
             'size': None, 'revenue': None,
             'revenue_source': None, 'hq': None, 'linkedin': None,
             'classification_confidence': None, 'classified_by': classified_by}

    search = {} if no_search else tavily_search(company_name, industry_hint)
    deferred = bool(search.get('deferred'))
    if deferred:
        search = {}
    if not search.get('results') and (require_search
                                      or not (article_context or '').strip()):
        return dict(empty, deferred=deferred)  # nothing to extract from at all

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

    # ── ZoomInfo-style aggregator probe (2026-08-06) ─────────────────────
    # When the general search left HQ / revenue / size unknown, run ONE
    # targeted follow-up. Search-engine SNIPPETS of public aggregator
    # profiles (zoominfo.com, rocketreach.co, growjo, etc.) carry exactly
    # these fields — e.g. ZoomInfo's employee-directory page snippet gives
    # the full street address + headcount, RocketReach's gives "$28.2
    # million in revenue and 191 employees ... Birmingham, Alabama". Free
    # (rides the normal ladder + cache), no ZoomInfo login, and reads only
    # what the engines publish in their results.
    _probe_results = []
    if (not no_search and data is not None
            and not (data.get('hq') and data.get('revenue')
                     and data.get('size'))):
        probe = tavily_search(company_name, 'zoominfo', paid_ok=False)
        if probe.get('results'):
            _probe_results = probe['results']
            plines = [
                f"- {r.get('title','')}\n"
                f"  {r.get('url','')}\n"
                f"  {(r.get('content','') or '')[:320]}\n"
                for r in probe['results'][:4]
            ]
            prompt2 = FIRMOGRAPHIC_PROMPT.format(
                company_name=company_name,
                industry_hint=industry_hint or 'unknown',
                article_context=(article_context or '(not provided)').strip(),
                results_text=('\n'.join(lines + plines)).strip(),
            )
            data2 = llm_json(prompt2, max_tokens=550)
            if data2:
                # Per-field merge: second pass fills gaps, never overwrites
                # a value the first pass already established.
                for k, v in data2.items():
                    if v and not data.get(k):
                        data[k] = v

    # Deterministic snippet parse — regex beats LLM extraction for the
    # rigid aggregator phrasings ("located in City, State ... N employees").
    # Fill-if-missing only; runs over BOTH search passes' results.
    if data is not None and (not data.get('hq') or not data.get('size')):
        parsed = _parse_hq_size_from_snippets(
            list(search.get('results') or []) + _probe_results)
        if parsed['hq'] and not data.get('hq'):
            data['hq'] = parsed['hq']
        if parsed['size'] and not data.get('size'):
            data['size'] = parsed['size']

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
    conf_raw = str(data.get('classification_confidence') or '').strip().title()
    conf_val = conf_raw if conf_raw in ('High', 'Medium', 'Low') else None

    return {
        'url':            data.get('url')      or None,
        'industry':       data.get('industry') or None,
        'zi_subindustry': zi_val,
        'size':           data.get('size')     or None,
        'revenue':        revenue,
        'revenue_source': revenue_src,
        'hq':             data.get('hq')       or None,
        'linkedin':       data.get('linkedin') or None,
        'classification_confidence': conf_val,
        'classified_by':  classified_by,
        'deferred':       deferred,
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
role is Controller / VP Accounting / Chief Accounting (not CFO-track). \
ALSO apply (+3) when event_type=finance_seat_open — the company is HIRING \
a CFO/Controller (open seat, job posting); never #NewCFO for an open seat.
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


# Finance leadership roles: "new CFOs/Controllers/VPs of Finance are VERY
# high value and probably more valuable than any other trigger" (A.J.).
# Phase 4 (2026-09-08): whether the event IS such a hire is decided by
# src/pipeline/hires.finance_hire_subject (the role must be the subject of
# a hire verb — attribution, interim/former seats, board seats and awards
# never count); the substring lists below survive only for the board-only
# gate, which asks the weaker question "is a finance role mentioned at all".
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
    """The finance seat this event HIRES into: 'cfo' | 'controller' | None.

    Why it matters: the rubric awards #NewCFO(+5) vs #NewController(+3).
    The old code relabeled Controller hires to event_type=cfo_hire, which
    then triggered #NewCFO on regrade — a +5/+3 double-count inflation loop
    (audit 2026-07-16). Only true CFO-equivalents get relabeled.

    Phase 4 (2026-09-08): decided by hires.finance_hire_subject — the role
    must be the SUBJECT of a hire verb, so an earnings release quoting the
    CFO, a board seat or an award is None even when event_type says
    cfo_hire (the scraper's label is an input, not evidence). The rubric's
    split applies: CFO / VP Finance / Head or Director of Finance are
    CFO-equivalents ('cfo'), Controller / VP Accounting / Chief Accounting
    Officer are 'controller'; a Treasurer hire is a finance-leader event
    but neither rubric seat, hence None (as before Phase 4)."""
    subj = finance_hire_subject(event.get('title') or '', event.get('description') or '')
    role = subj.get('role')
    if role in NEW_CFO_ROLES:
        return 'cfo'
    if role in NEW_CONTROLLER_ROLES:
        return 'controller'
    return None


# ── Research probes — the evidence A.J.'s rubric was designed to consume ─────
# All free: funding + AUM ride the existing Firecrawl/Tavily dispatcher (with
# its persistent cache); nonprofit data uses ProPublica's public 990 API.

ASSET_MANAGER_SUBINDUSTRIES = {
    'Venture Capital & Private Equity', 'Investment Banking',
    'Lending & Brokerage',
}
NONPROFIT_VERTICAL = 'Nonprofits & Organizations'


def _probe_search(company_name: str, hint: str, kind: str, cached_only: bool) -> dict:
    """The probes' search call. `cached_only` (Phase 4 enrich-once): serve
    the AccountCache's stored result for this (account, kind) and NEVER
    enter the ladder — a known account's probes were run when it was
    verified (aum / complexity live 180 days there); a miss is simply no
    evidence, not a reason to search again."""
    if not cached_only:
        return tavily_search(company_name, hint, paid_ok=False, kind=kind)
    key = _gates_account_key(company_name)
    hit = _account_cache().get_search(key, kind) if key else None
    if hit:
        SEARCH_COUNTS['cache'] += 1
    return hit or {}


def probe_funding_history(company_name: str, cached_only: bool = False) -> str:
    """Search for funding events (rubric: #Funding = verified within last
    18 months). Returns a compact evidence block ('' if nothing)."""
    try:
        res = _probe_search(company_name, 'funding round investment raised',
                            'funding_history', cached_only)
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


def probe_aum(company_name: str, cached_only: bool = False) -> str:
    """Search for AUM/AUA evidence for asset managers (rubric:
    #AssetManagerScale). Returns a compact evidence block ('' if nothing)."""
    try:
        res = _probe_search(company_name, 'AUM assets under management funds',
                            'aum', cached_only)
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


def probe_complexity(company_name: str, cached_only: bool = False) -> str:
    """Search for COMPLEXITY evidence: multiple locations, subsidiaries/
    brands, multi-country operations, franchising. These are the +2 rubric
    signals (#Locations #Entities #Global #Franchisor/#Franchisee) that
    separate Grade B (5-7) from Grade A (8+) — and before this probe
    existed they were almost never evidenced (audit 2026-08-08: #Locations
    on 2 of 440 events, #Entities on 0). Aggregator snippets leak exactly
    this ("has locations in the United States, Canada, ..."), as do the
    companies' own locations/franchise pages. Returns '' if nothing."""
    try:
        res = _probe_search(company_name, 'locations offices subsidiaries franchise',
                            'complexity', cached_only)
        hits = (res or {}).get('results') or []
        if not hits:
            return ''
        lines = [f'COMPLEXITY SEARCH ("{company_name} locations/'
                 f'subsidiaries/franchise") — evidence for #Locations, '
                 f'#Entities, #Global, #Franchisor/#Franchisee:']
        for r in hits[:4]:
            lines.append(f"- {r.get('title','')} | {r.get('url','')}")
            snippet = (r.get('content') or '')[:220]
            if snippet:
                lines.append(f"  {snippet}")
        return '\n'.join(lines)
    except Exception as e:
        log.debug(f'  Complexity probe failed: {e}')
        return ''


def probe_nonprofit_990(company_name: str) -> str:
    """ProPublica Nonprofit Explorer (free, no key): find the org, pull its
    latest Form 990 financials. This is the rubric's required NPO source —
    990 revenue + filing history evidence complexity and revenue band.
    Returns a compact evidence block ('' if no match).

    Not a web search, so it never touches the SearchBudget — but the block
    rides the AccountCache at kind 'nonprofit_990' (990s are annual) and a
    confirmed no-match is negative-cached so the same org isn't looked up
    on every event."""
    key = _gates_account_key(company_name)
    cache = _account_cache()
    hit = cache.get_search(key, 'nonprofit_990') if key else None
    if hit:
        SEARCH_COUNTS['cache'] += 1
        return ((hit.get('results') or [{}])[0].get('content') or '')
    if key and cache.should_skip(key, 'nonprofit_990'):
        SEARCH_COUNTS['negative_cache'] += 1
        return ''
    block = _propublica_990(company_name)
    if key and block is not None:
        if block:
            cache.set_search(key, 'nonprofit_990', {'results': [{'content': block}]})
        else:
            cache.record_empty(key, 'nonprofit_990')
    return block or ''


def _remembered_state(company_name: str):
    """State code of the account's remembered hq (AccountCache), or None.
    Stage A/B persist a seed / registry / search hq before the grading
    probes run, so this is the same anchor the registry lookups used."""
    try:
        key = _gates_account_key(company_name)
        fg = _account_cache().get_firmographics(key) if key else None
        return _gates_hq_state_code((fg or {}).get('hq'))
    except Exception:
        return None


def _propublica_990(company_name: str):
    """The HTTP half of probe_nonprofit_990: evidence block, '' for a
    confirmed no-match, None when the API itself failed (never cached).

    Phase 3 B2 (research 2026-09-08) — routed through oracles.npo_lookup,
    which fixes two defects of the v1 code: (a) ProPublica answers a
    zero-hit search with HTTP 404 AND a valid JSON body; raise_for_status
    made that an "API failure" (None), never cached, so the same org was
    re-queried on every event; (b) organizations[0] was taken blind — no
    state[id] filter, no name check — so a Boston museum could come back
    as a Virginia one. The adapter filters by the account's remembered
    state, applies the registry similarity gate, and shares the Stage A
    cache (kind oracle_propublica) — a nonprofit the registry pass already
    found costs this probe zero HTTP calls."""
    try:
        status, hit = _oracles.npo_lookup(company_name, _remembered_state(company_name),
                                          cache=_account_cache(), live=ORACLES_LIVE_ENABLED)
    except Exception as e:                      # the adapter never raises; belt and braces
        log.debug(f'  990 probe failed: {e}')
        return None
    if status == 'error':
        return None
    if not hit:
        return ''                               # confirmed zero (or negative-cached)
    lines = [
        f'PROPUBLICA 990 ("{company_name}"):',
        f"- Matched org: {hit.get('matched_name')} (EIN {hit.get('ein')}), "
        f"{hit.get('hq')} | {hit.get('profile_url')}",
    ]
    latest = hit.get('latest_990') or {}
    if latest.get('total_revenue') is not None:
        exp = latest.get('total_expenses')
        line = f"- Latest 990 ({latest.get('year')}): total revenue ${latest['total_revenue']:,}"
        if exp is not None:
            try:
                line += f" · total expenses ${int(exp):,}"
            except (TypeError, ValueError):
                pass
        lines.append(line)
    if hit.get('filings'):
        lines.append(f"- 990 filings on record: {hit['filings']} years")
    if hit.get('ntee_code'):
        lines.append(f"- NTEE {hit['ntee_code']} → {hit.get('zi_subindustry')}")
    return '\n'.join(lines)


def gather_extra_evidence(event: dict, companies_data: list,
                          fit: dict, known_account: bool = False) -> str:
    """Run the rubric's research probes for the PRIMARY company, chosen by
    what the rubric needs for this kind of account. Skips redundant work
    (funding events already carry funding evidence).

    `known_account` (Phase 4 enrich-once): the account came from a fresh
    verified accounts-table row, so the probes read the AccountCache only
    (cached_only) and never search — the 990 API stays (free, not a
    search, cached 365 days by probe_nonprofit_990 itself)."""
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
    # v2: at most ONE web-search probe per account, scrape-only, chosen by
    # what the rubric most needs. (v1 fired up to three, each a separate
    # paid-eligible ladder call.) Nonprofit 990 is a free HTTP API, not a
    # search, so it never counts.
    et = (event.get('event_type') or '')
    if vertical != NONPROFIT_VERTICAL:
        if zi in ASSET_MANAGER_SUBINDUSTRIES:
            b = probe_aum(name, cached_only=known_account)
        elif (et in ('cfo_hire', 'finance_seat_open', 'merger_acquisition', 'funding')
              and fit.get('verdict') == 'pass'):
            b = probe_complexity(name, cached_only=known_account)
        elif et != 'funding':
            b = probe_funding_history(name, cached_only=known_account)
        else:
            b = ''
        if b:
            blocks.append(b)

    return '\n\n'.join(blocks)


def grade_event(event: dict, companies_data: list,
                extra_evidence: str = '', account_name: str = '',
                fit: dict = None) -> dict:
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
    - Phase 4 (2026-09-08): the model's list then passes the declarative
      guard table (src/pipeline/hashtag_guards.HASHTAG_GUARDS) BEFORE the
      score is computed; every strip is logged once and kept in
      'guard_notes'. `fit` (the event-level fit dict) feeds the guards that
      need the account's subindustry; when absent the account record's
      own zi_subindustry is used.
    """
    empty = {
        'grade': None,
        'confidence': None,
        'numeric_score': None,
        'hashtags': [],
        'cfo_status': None,
        'grade_justification': None,
        'research_notes': [],
        'guard_notes': [],
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

    # ── Evidence guards (Phase 4 slice C2, 2026-09-08) ───────────────────
    # The LLM provably fabricates hashtags despite the prompt rules —
    # #NewCFO on material-agreement 8-Ks ("+5 applied as highest-value
    # single trigger", CNL 2026-07-21), #Funding on a $500K seed, #100EE
    # from a '51-200' bucket, #FormerUser that no input could evidence. The
    # substring test that guarded the two finance-leader tags is replaced
    # by the declarative table: every mechanically checkable tag needs its
    # evidence in the event text, the account record or the probe block,
    # or it is stripped BEFORE the score is computed (the rubric's own
    # "when in doubt, DROP the hashtag").
    _et = event.get('event_type')
    if _et == 'finance_seat_open' and '#NewController' not in hashtags:
        # An OPEN finance seat (job posting) is a +3 Controller-equivalent
        # trigger by definition (A.J. 2026-09-06) — added here, not judged;
        # the guard table strips #NewCFO for the same event type.
        hashtags.append('#NewController')
    _acct = (next((c for c in companies_data
                   if (c.get('name') or '').strip() == (account_name or '').strip()), None)
             or pick_primary(companies_data) or {})
    _fit = dict(fit or {})
    if not _fit.get('zi_subindustry'):
        _fit['zi_subindustry'] = _acct.get('zi_subindustry')
    hashtags, guard_notes = apply_guards(hashtags, event, _acct, _fit, extra_evidence,
                                         companies_data=companies_data)
    for _note in guard_notes:
        log.info(f'  guard: {_note}')

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
        'guard_notes': guard_notes,
    }


# ── Phase 2 helpers (2026-09-07): pagination, LLM canary, typed columns ────
# Small and pure where possible so enrich_events() stays a sequence of
# decisions rather than a wall of inline logic — each is unit-tested in
# tests/test_enrichment_v2.py.

_FIRM_FIELDS = ('url', 'industry', 'zi_subindustry', 'size', 'revenue',
                'revenue_source', 'hq', 'linkedin')
# What Stage B is allowed to spend a search on. industry/linkedin/
# revenue_source are nice-to-haves that never change a fit verdict.
_STAGE_B_NEEDS = ('zi_subindustry', 'hq', 'revenue', 'size', 'url')
_CONFIDENCE_RANK = {'Low': 0, 'Medium': 1, 'High': 2}
# EDGAR state-of-incorporation codes for the Canadian provinces we cover.
_SEC_PROVINCE_CODES = {'A3': 'NB', 'A4': 'NL', 'A5': 'NS', 'A6': 'ON',
                       'A7': 'PE', 'A8': 'QC'}


def _strip_range_params(query) -> None:
    """postgrest-py's range() ADDS offset/limit params instead of replacing
    them, so a second page on the same builder would send both. Strip them
    between pages; fake/older builders without the attribute are left alone."""
    try:
        params = query.request.params
        query.request.params = params.remove('offset').remove('limit')
    except Exception:
        pass


def _fetch_all(query, order: str = 'discovered_at', desc: bool = False,
               page: int = 1000, max_rows: int = 5000) -> list:
    """Page through a select with .range(). Supabase caps one select at
    1,000 rows and the old single .execute() silently dropped the queue's
    tail past that; oldest-first ordering plus paging guarantees the tail
    is reached. Stops at the first short page or at max_rows."""
    rows = []
    q = query.order(order, desc=desc)
    start = 0
    while start < max_rows:
        end = min(start + page, max_rows) - 1
        _strip_range_params(q)
        chunk = q.range(start, end).execute().data or []
        rows.extend(chunk)
        if len(chunk) < (end - start + 1):
            break
        start = end + 1
    return rows


def _llm_canary() -> bool:
    """One trivial local-LLM call before any event is touched. False when
    the server is unreachable — the caller then exits with NOTHING stamped,
    so the whole queue is intact for the next launchd cycle."""
    LLM_STATE['unavailable'] = False
    LLM_STATE['consecutive'] = 0
    llm_json('Reply with exactly this JSON object and nothing else: {"ok": true}',
             max_tokens=20)
    return not LLM_STATE['unavailable']


def _prev_attempts(event: dict) -> int:
    """events.enrich_attempts as read (0 when the column isn't there yet)."""
    try:
        return int(event.get('enrich_attempts') or 0)
    except (TypeError, ValueError):
        return 0


def _account_company(enriched: list, fit: dict) -> dict:
    """The company dict the fit gates chose as the account (else primary)."""
    nm = (fit.get('account_name') or '').strip()
    return (next((c for c in enriched if (c.get('name') or '').strip() == nm), None)
            or pick_primary(enriched) or {})


def _tombstone_typed(event: dict, present, fit: dict = None,
                     structured: dict = None, account: dict = None) -> dict:
    """Typed half of a tombstone: verify_state=not_fit/fit_verdict=fail plus
    whatever else is computable from the event alone (account_key,
    expires_at, SEC fields); retry_after is cleared. {} when the migration
    hasn't run."""
    if not present:
        return {}
    pl = typed_payload(event=event, fit=fit, structured=structured, account=account,
                       verify_state='not_fit', present=present, retry_after=None)
    pl.update(not_fit_payload(present))
    return pl


def _final_typed(event: dict, present, fit: dict, structured: dict,
                 enriched: list, attempts_prev: int, deferred: bool = False) -> dict:
    """Typed columns for the staged / unverified / pass write. enrich_attempts
    and the retry ladder advance only for the two 'come back later' states
    (staged, researched_ambiguous); a verified row gets retry_after CLEARED
    so a stale date can't hide it from a future reverify.

    `deferred` (review 2026-09-07): the event's search was throttled or the
    backend never answered, so nothing was actually researched. Then
    enrich_attempts is NOT bumped and retry_after is NOT passed (left as it
    is) — otherwise a ≥12h search outage walks every staged row up to
    MAX_ENRICH_ATTEMPTS and parks it out of --reverify without one real
    attempt. fit.deferred_attempts (JSON side) stays the deferral counter."""
    if not present:
        return {}
    state = verify_state_for(fit.get('verdict'))
    account = _account_company(enriched, fit)
    common = dict(event=event, fit=fit, structured=structured, account=account,
                  verify_state=state, present=present,
                  classification_confidence=account.get('classification_confidence'),
                  classified_by=account.get('classified_by'))
    if state in ('staged', 'researched_ambiguous'):
        if deferred:
            return typed_payload(**common)
        attempts = attempts_prev + 1
        return typed_payload(attempts=attempts,
                             retry_after=retry_after_for(state, attempts), **common)
    return typed_payload(retry_after=None, **common)


def _llm_unavailable_payload(present, attempts_prev: int) -> dict:
    """The ONLY thing written for an event the local LLM couldn't serve:
    bump attempts and push it out LLM_RETRY_HOURS. No enriched_at, no
    tombstone, no fit — nothing that would look like a decision."""
    full = {'enrich_attempts': attempts_prev + 1, 'retry_after': llm_retry_after()}
    return {k: v for k, v in full.items() if k in set(present or ())}


def _llm_unavailable_event(client, eid: str, present, attempts_prev: int,
                           dry_run: bool) -> bool:
    """Bookkeeping for one LLM-unavailable event. Returns True when the run
    should stop: LLM_UNAVAILABLE_STOP_AFTER consecutive such events AND a
    fresh canary that fails too (review 2026-09-07 — three events in a row
    can trip 'unavailable' on a server that is UP, e.g. a 5xx on an
    oversized prompt, and stopping then stranded the whole queue for a
    cycle over three bad rows). Those events keep their attempts+1 /
    retry_after push; a passing canary resets the streak and the run
    continues with the next event."""
    LLM_STATE['consecutive'] += 1
    log.warning(f'  ⛔ Local LLM unavailable — nothing stamped; retry in '
                f'{LLM_RETRY_HOURS}h (consecutive: {LLM_STATE["consecutive"]})')
    if not dry_run and present:
        pl = _llm_unavailable_payload(present, attempts_prev)
        try:
            client.table('events').update(pl).eq('id', eid).execute()
        except Exception as e:
            log.warning(f'  write failed: {e}')
    if LLM_STATE['consecutive'] >= LLM_UNAVAILABLE_STOP_AFTER:
        streak = LLM_STATE['consecutive']
        if _llm_canary():             # success resets the streak + the flag
            log.warning('  LLM answered the canary — the events themselves are '
                        'the problem, continuing')
            return False
        LLM_STATE['consecutive'] = streak
        log.error(f'FAIL: local LLM ({LLAMACPP_URL}) unavailable for {streak} '
                  f'consecutive events and the canary failed too — stopping the '
                  f'run. Nothing further is stamped; the queue is retried next cycle.')
        return True
    return False


# ── Phase 2: classify-then-research stages ──────────────────────────────────

def _placeholder_company(name: str, role: str) -> dict:
    """A never-researched company record (non-workable role, non-operating
    entity) in the same shape as an enriched one so the dashboard chips it."""
    rec = {'name': name, 'role': role}
    rec.update({f: None for f in _FIRM_FIELDS})
    rec['classification_confidence'] = None
    rec['classified_by'] = None
    return rec


def _company_record(name: str, role: str, firm: dict) -> dict:
    """companies_data entry from a stage firm dict (private keys dropped).

    `field_sources` — {field: 'seed'|'cache'|'article'|'search'} for the
    fields that have a value — IS written to Supabase (review 2026-09-07):
    it is a handful of short strings per company, the dashboard reads
    named keys only, and it is the only way to tell a dateline hq from a
    researched one when auditing a row or the tombstone benchmark."""
    rec = _placeholder_company(name, role)
    for f in _FIRM_FIELDS:
        rec[f] = firm.get(f)
    rec['classification_confidence'] = firm.get('classification_confidence')
    rec['classified_by'] = firm.get('classified_by')
    srcs = {f: s for f, s in (firm.get('_sources') or {}).items() if firm.get(f)}
    if srcs:
        rec['field_sources'] = srcs
    if firm.get('deferred'):
        rec['deferred'] = True
    # Phase 3: the registry's under-$5M verdict (company_fit reads it) and
    # the resolved website domain — identity for dedup and paid enrichers.
    if firm.get('too_small'):
        rec['too_small'] = firm['too_small']
    if firm.get('domain'):
        rec['domain'] = firm['domain']
        rec['domain_method'] = firm.get('domain_method')
    return rec


def _industry_hint_for(co: dict) -> str:
    """NEUTRAL disambiguation hint. Never inject industry guesses: the old
    'financial services private equity' hint for M&A/funding events biased
    BOTH the web search AND the ZI classification — every funded startup
    came back 'Venture Capital & Private Equity' (live test 2026-07-16).
    The article's own descriptor is the best disambiguator ("fomo" alone
    is unsearchable; "fomo trading platform" isn't)."""
    descriptor = (co.get('descriptor') or '').strip()
    if descriptor:
        return descriptor
    if (co.get('role') or '').lower() in ('lead investor', 'investor'):
        return 'investment firm'
    return 'company North America'


def _article_context(event: dict) -> str:
    return (f"{(event.get('title') or '')[:200]}\n"
            f"{(event.get('description') or '')[:500]}")


def _structured_seeds(event: dict, name: str, structured: dict) -> dict:
    """Authoritative facts for the FILER company (the event's company_name)
    from the SEC text itself: state of incorporation → hq, Form D declared
    revenue range → revenue band. Applied BEFORE any LLM output and never
    overridden by it (a filing beats an article beats a search)."""
    seeds = {}
    if name.strip().lower() != (event.get('company_name') or '').strip().lower():
        return seeds
    if 'sec.gov' in (event.get('source_url') or ''):
        m = re.search(r'\(([A-Z]\d|[A-Z]{2})\)', event.get('description') or '')
        if m:
            seeds['hq'] = _SEC_PROVINCE_CODES.get(m.group(1), m.group(1))
    seg = (structured or {}).get('revenue_segment')
    if seg in ('LMM', 'MM', 'Corp'):
        seeds['revenue'] = seg
        seeds['revenue_source'] = 'SEC Form D declared revenue range'
    return seeds


def _fill_missing(base: dict, new: dict, fields=_FIRM_FIELDS, source: str = None) -> dict:
    """Copy of `base` with empty firmographic fields filled from `new`.
    `source` (review 2026-09-07) stamps each field it fills into the copy's
    `_sources` provenance map ('seed' | 'oracle' | 'cache' | 'article' | 'search'):
    the pre-search gates, Stage B's needs and the AccountCache write all
    decide by WHERE a value came from, not just whether it is there."""
    out = dict(base)
    srcs = dict(out.get('_sources') or {})
    for f in fields:
        if not out.get(f) and (new or {}).get(f):
            out[f] = new[f]
            if source:
                srcs[f] = source
    if source:
        out['_sources'] = srcs
    return out


def _confidence_meets(conf, minimum) -> bool:
    """Does an article classification confidence clear the tombstone bar?
    minimum 'never' disables the rule outright."""
    if str(minimum or '').lower() == 'never':
        return False
    return _CONFIDENCE_RANK.get(conf or '', -1) >= _CONFIDENCE_RANK.get(minimum, 99)


def _article_other_decision(firm: dict, structured_out: bool = False,
                            article_chars: int = 0) -> tuple:
    """Stage A verdict on an 'OTHER' classification → (firm, no_search).

    OTHER is FINAL (kept — company_fit fails the vertical, no search) when
    it came from the account cache (already researched), the structured SEC
    verdict for this company is 'out', or the article pass is confident
    enough (ARTICLE_OTHER_TOMBSTONE_MIN_CONFIDENCE) AND the article was long
    enough to be evidence — `article_chars` (the description length) above
    ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS (review 2026-09-07; a caller
    that doesn't pass the length gets the conservative 0: no article-only
    tombstone). Otherwise it is only a hunch: reset to None (unknown) so
    Stage B can resolve it. An out-of-vertical LABEL (ZI_NOT_A_FIT, P2:
    'K-12 Schools') is decided by the same rule — it fails the gate like
    OTHER, so it must clear the same evidence bar first."""
    zi = (firm.get('zi_subindustry') or '').strip()
    if zi.upper() != 'OTHER' and zi not in ZI_NOT_A_FIT:
        return firm, False
    final = (structured_out or firm.get('classified_by') in ('cache', 'oracle', 'account')
             or (_confidence_meets(firm.get('classification_confidence'),
                                   ARTICLE_OTHER_TOMBSTONE_MIN_CONFIDENCE)
                 and (article_chars or 0) > ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS))
    if final:
        return firm, True
    return dict(firm, zi_subindustry=None), False


def _event_state_hint(event: dict, seeds: dict):
    """State to anchor a registry lookup on: the SEC-seeded hq state, else
    the ONE state the scraper's territory match named (matched_regions — a
    dateline or body mention; two different states = no anchor, because a
    wrong anchor pushes the right registry row 0.3 below the bar)."""
    st = _gates_hq_state_code(seeds.get('hq')) if seeds.get('hq') else None
    if st:
        return st
    regions = event.get('matched_regions')
    if isinstance(regions, str):
        try:
            regions = json.loads(regions) if regions.strip() else []
        except ValueError:
            regions = []
    if not isinstance(regions, list):
        regions = [regions] if regions else []
    codes = {c for c in (_gates_hq_state_code(str(r)) for r in regions if r) if c}
    return next(iter(codes)) if len(codes) == 1 else None


def _hq_city(hq) -> str:
    """'Westerly, RI' → 'Westerly'; a bare state / blank → ''."""
    head = (str(hq or '').split(',')[0]).strip()
    return '' if not head or _gates_hq_state_code(head) == head.upper() else head


def _oracle_lookup(name: str, hint: dict, cache, live: bool = None):
    """The registry call (seam for tests). oracles.lookup never raises.
    `live` (M9, review 2026-09-08): a dry run must stay on the local tables
    — the FDIC wildcard and ProPublica adapters call out AND write
    search_cache / negative_cache, which a dry run promises not to do."""
    return _oracles.lookup(name, hint, db_path=ORACLES_DB_PATH, cache=cache,
                           live=ORACLES_LIVE_ENABLED if live is None else live)


_ORACLE_FIELDS = ('hq', 'revenue', 'zi_subindustry', 'url', 'size', 'industry')


def _oracle_apply(firm: dict, hit: dict) -> tuple:
    """(copy of `firm` with the registry's facts applied, fields taken).

    A field is taken when it is empty OR known only from the article — a
    registry beats a dateline and a model's guess; seeds, cache and search
    values keep theirs — and is stamped 'oracle' in `_sources`. A revenue
    taken from the registry brings its revenue_source, and when the
    estimate sits under the $5M bar the copy carries `too_small`, the
    reason company_fit fails revenue with (the Form D declared-revenue
    rule's twin). zi from the registry → classified_by 'oracle' at High."""
    out = dict(firm)
    srcs = dict(out.get('_sources') or {})
    took = []

    def replaceable(f):
        return not out.get(f) or srcs.get(f) == 'article'
    for f in _ORACLE_FIELDS:
        v = hit.get(f)
        if v and replaceable(f):
            out[f] = v
            srcs[f] = 'oracle'
            took.append(f)
    if 'revenue' in took:
        out['revenue_source'] = hit.get('revenue_source')
        srcs['revenue_source'] = 'oracle'
        if hit.get('too_small'):
            out['too_small'] = (f'oracle_too_small: {hit.get("source")} est '
                                f'{_oracles.format_usd(hit.get("revenue_amount_usd"))}')
    if 'zi_subindustry' in took:
        out['classified_by'] = 'oracle'
        out['classification_confidence'] = 'High'
    out['_sources'] = srcs
    return out, took


def _stage_a_oracle(name: str, firm: dict, hint: dict, cache, second_chance: bool = False,
                    live: bool = None, anchor_state: str = None):
    """One registry lookup for a Stage A company → (firm, hit | None). Only
    a hit at ORACLE_MIN_CONFIDENCE or better is applied; one log line each.
    `anchor_state` (M2): the lookup ran WITHOUT a state (the only anchor was
    the article's dateline) — the hit counts only if its RAW name score (no
    geography bonus) clears the bar AND its registry state equals the
    dateline's; otherwise the dateline stays an article fact."""
    hit = _oracle_lookup(name, hint, cache, live=live)
    if not hit or (hit.get('confidence') or 0) < ORACLE_MIN_CONFIDENCE:
        return firm, None
    if anchor_state:
        raw = hit.get('raw_score', hit.get('confidence')) or 0
        if raw < ORACLE_MIN_CONFIDENCE or (hit.get('hq_state') or '').upper() != anchor_state:
            log.info(f'  ✗ Registry candidate not confirmed (second chance, dateline anchor '
                     f'{anchor_state}): {name} → {hit.get("matched_name")} · '
                     f'{hit.get("hq") or "hq ?"} · raw {float(raw):.2f} — article hq kept')
            return firm, None
    out, took = _oracle_apply(firm, hit)
    SEARCH_COUNTS['oracle'] += 1
    log.info(f'  ✓ Registry hit ({hit.get("source")}{", second chance" if second_chance else ""}): '
             f'{name} → {hit.get("matched_name")} · {hit.get("hq") or "hq ?"} · '
             f'{hit.get("revenue") or "revenue ?"} · {hit.get("zi_subindustry") or "zi ?"} · '
             f'conf {float(hit.get("confidence") or 0):.2f}'
             + (f' · filled {", ".join(took)}' if took else ' · nothing new'))
    if out.get('too_small'):
        log.info(f'     {out["too_small"]} — below the $5M bar, no search')
    return out, hit


def _stage_a_company(event: dict, co: dict, structured: dict, article_ctx: str,
                     cache, dry_run: bool = False, account_row: dict = None) -> dict:
    """STAGE A — free classification for one workable company:
    structured seeds → account firmographic cache → registry (Phase 3 B2:
    SEC IAPD / FDIC / ProPublica for adviser-, bank- and nonprofit-shaped
    names) → ONE article-only local LLM call (only when zi_subindustry or
    hq is still missing) → a second registry chance when the article's
    subindustry says bank / adviser / nonprofit but the name did not.
    Returns a firm dict carrying classification_confidence / classified_by
    / no_search / _seeds; nothing downstream overrides a seed.

    Order (M6, review 2026-09-08): the AccountCache fills BEFORE the
    registry runs. The cache holds what a SEARCH established for this
    account (hq, revenue, zi, up to 365 days); the registry may only fill
    what is empty or known from the article (_oracle_apply) — with the old
    order a fuzzy registry hit overwrote researched facts and re-stamped
    them 'oracle' for another year. A cached hq also anchors the lookup.
    `dry_run` (M9): the registry stays on the local tables.

    `account_row` (Phase 4 enrich-once, 2026-09-08): a VERIFIED accounts-
    table row for this company, fresh within ACCOUNT_FRESH_DAYS, selected
    by the caller (_known_account). Its facts fill everything the seeds
    left empty with provenance 'account' — settled, the way cache facts
    are — and the company is marked no_search / account_known: no cache
    read, no registry, no article LLM, no Stage B, no probe search. The
    account was researched once; only the new trigger is graded."""
    name = co['name']
    key = _gates_account_key(name)
    firm = {f: None for f in _FIRM_FIELDS}
    seeds = _structured_seeds(event, name, structured)
    firm.update(seeds)
    # Per-field provenance (review 2026-09-07): 'seed' | 'oracle' | 'cache' |
    # 'article' here, 'search' added by _merge_search; 'account' (Phase 4)
    # for facts taken from the accounts table. Decides what the pre-search
    # gates may judge, what Stage B still needs and what the cache keeps.
    firm['_sources'] = {f: 'seed' for f in seeds}
    if seeds.get('hq'):
        log.info(f'     hq seeded from SEC filing: {seeds["hq"]}')
    if seeds.get('revenue'):
        log.info(f'     revenue seeded from Form D declared range: {seeds["revenue"]}')

    if account_row:
        facts = _account_facts(account_row)
        firm = _fill_missing(firm, facts, source='account')
        if facts.get('domain') and not firm.get('domain'):
            firm['domain'] = facts['domain']
            firm['domain_method'] = 'account'
        firm['classification_confidence'] = facts.get('classification_confidence') or 'High'
        firm['classified_by'] = 'account'
        firm['no_search'] = True
        firm['account_known'] = True
        firm['_seeds'] = seeds
        SEARCH_COUNTS['account'] += 1
        log.info(f'  → account known: {name} (verified {_account_verified_on(account_row)})')
        return firm

    conf = by = None
    cached = cache.get_firmographics(key) if key else None
    if cached:
        firm = _fill_missing(firm, cached, source='cache')
        if firm['_sources'].get('zi_subindustry') == 'cache':
            conf, by = cached.get('classification_confidence'), 'cache'
    # Registry anchor: the SEC seed's state, else the cached (researched)
    # hq's, else the ONE state the scraper's territory match named.
    state_hint = ((_gates_hq_state_code(seeds['hq']) if seeds.get('hq') else None)
                  or (_gates_hq_state_code(firm.get('hq'))
                      if firm['_sources'].get('hq') == 'cache' else None)
                  or _event_state_hint(event, seeds))
    live = ORACLES_LIVE_ENABLED and not dry_run
    firm, oracle_hit = _stage_a_oracle(name, firm, {'kind': 'auto', 'state': state_hint}, cache,
                                       live=live)
    if firm['_sources'].get('zi_subindustry') == 'oracle':
        conf, by = 'High', 'oracle'
    if by in ('cache', 'oracle') and firm.get('hq') and firm.get('zi_subindustry'):
        log.info(f'  → {"Registry" if by == "oracle" else "Account cache"}: {name} '
                 f'(zi + hq known — no LLM call)')
    else:
        log.info(f'  → Classifying from article: {name}')
        got = enrich_one_company(name, _industry_hint_for(co),
                                 article_context=article_ctx, no_search=True)
        had_zi = bool(firm.get('zi_subindustry'))
        firm = _fill_missing(firm, got, source='article')
        if not had_zi:
            conf, by = got.get('classification_confidence'), 'article'
        # Second chance: the name looked like nothing ("Beacon Hill Partners"),
        # but the article called it a bank / adviser / nonprofit — the
        # registries can now confirm hq, revenue and url for free.
        if oracle_hit is None and firm.get('zi_subindustry') in _oracles.SECOND_CHANCE_ZI:
            firm, oracle_hit = _stage_a_second_chance(name, firm, state_hint, cache, live)
            if firm.get('classified_by') == 'oracle':
                conf, by = 'High', 'oracle'
    firm['classification_confidence'] = conf
    firm['classified_by'] = by
    structured_out = ((structured or {}).get('verdict') == 'out'
                      and name.strip().lower() ==
                      (event.get('company_name') or '').strip().lower())
    firm, no_search = _article_other_decision(
        firm, structured_out,
        article_chars=len((event.get('description') or '').strip()))
    firm['no_search'] = no_search
    firm['_seeds'] = seeds
    return firm


def _stage_a_second_chance(name: str, firm: dict, state_hint, cache, live: bool):
    """The registry retry after the article pass → (firm, hit | None).

    M2 (review 2026-09-08): when the event itself named a state (SEC seed,
    cached hq, single matched region) that state anchors the lookup as
    before. When the only anchor is the ARTICLE's hq — a dateline, i.e.
    where the release was issued — the lookup runs with NO state and no
    city (no geography bonus at all) and the hit is accepted only if its
    raw name score clears the bar AND the registry puts it in the
    dateline's state. Anchoring on the dateline let a Boston-datelined
    release confirm a same-name Boston firm, replace the article hq with
    the registry's (provenance 'oracle'), and tombstone the event before
    any search — for a company that was never in Boston."""
    zi = firm['zi_subindustry']
    if state_hint:
        hint = {'kind': 'auto', 'zi_guess': zi, 'state': state_hint,
                'city': _hq_city(firm.get('hq'))}
        return _stage_a_oracle(name, firm, hint, cache, second_chance=True, live=live)
    anchor = (_gates_hq_state_code(firm.get('hq'))
              if firm.get('_sources', {}).get('hq') == 'article' else None)
    hint = {'kind': 'auto', 'zi_guess': zi, 'state': None, 'city': None}
    return _stage_a_oracle(name, firm, hint, cache, second_chance=True, live=live,
                           anchor_state=anchor)


def _merge_search(firm_a: dict, got: dict, seeds: dict) -> dict:
    """STAGE B merge. Search values fill gaps; a search-derived
    zi_subindustry overrides the article's unless the search is Low-
    confidence against an article High; hq: seeds > search >
    article. classified_by flips to 'search' when the search changed or
    filled zi/hq.

    Review 2026-09-07: the search pass reads the article too, so for
    revenue / industry its answer SUPERSEDES an article-only value instead
    of sitting behind it (seed and cache values still win), and every
    field the search filled, confirmed or overrode is stamped 'search' in
    `_sources` — that stamp is what lets the AccountCache keep it."""
    merged = _fill_missing(firm_a, got, source='search')
    srcs = dict(merged.get('_sources') or {})
    a_zi, a_conf = firm_a.get('zi_subindustry'), firm_a.get('classification_confidence')
    s_zi, s_conf = got.get('zi_subindustry'), got.get('classification_confidence')
    zi_from_search = False
    if s_zi and s_zi != a_zi:
        # The search pass read the article AND the results, so it holds
        # strictly more evidence: it overrides the article's subindustry
        # unless it is itself Low-confidence against an article High
        # (Low usually means the results were about a different company).
        # Live 2026-09-07: "SharonAI Holdings" — article-High 'Holding
        # Companies' from the NAME lost to nothing while the search had
        # identified an AI-infrastructure company (OTHER): the audit's
        # Holding-Cos leak in miniature.
        if (not a_zi or s_conf in ('High', 'Medium')
                or a_conf in (None, 'Low')):
            merged['zi_subindustry'] = s_zi
            zi_from_search = True
    if s_zi and merged.get('zi_subindustry') == s_zi:
        srcs['zi_subindustry'] = 'search'       # filled, overridden or confirmed
    hq_from_search = False
    if not (seeds or {}).get('hq') and got.get('hq'):
        if got['hq'] != firm_a.get('hq'):
            merged['hq'] = got['hq']
            hq_from_search = True
        srcs['hq'] = 'search'
    for f in ('revenue', 'industry'):
        if got.get(f) and srcs.get(f) == 'article':
            merged[f] = got[f]
            srcs[f] = 'search'
            if f == 'revenue':
                merged['revenue_source'] = got.get('revenue_source')
                srcs['revenue_source'] = 'search'
    if zi_from_search or hq_from_search:
        merged['classified_by'] = 'search'
    if zi_from_search:
        merged['classification_confidence'] = s_conf
    merged['_sources'] = srcs
    merged['deferred'] = bool(got.get('deferred'))
    return merged


def _stage_b_company(co: dict, firm_a: dict, article_ctx: str, tier: int) -> dict:
    """STAGE B — budgeted research for one Stage-A survivor. Skips the search
    when the tier forbids it, nothing fit-relevant is left to learn, or the
    event's SearchBudget is spent — unless the account's firmographic search
    is already in the AccountCache, which tavily_search serves for free
    before it touches the budget (review 2026-09-07: the old order turned a
    cached third company away as 'budget spent').

    A NEED (review 2026-09-07) is a fit-relevant field that is missing OR
    known only from the article: an hq read off a dateline and a revenue
    band the model inferred don't settle territory/revenue — Phase 1 always
    searched, and so does a survivor carrying only article guesses. An
    article zi_subindustry counts as settled only at High confidence (the
    same bar the AccountCache applies)."""
    name = co['name']
    srcs = firm_a.get('_sources') or {}
    conf = firm_a.get('classification_confidence')

    def _settled(f):
        if not firm_a.get(f):
            return False
        if srcs.get(f) != 'article':
            return True
        return f == 'zi_subindustry' and conf == 'High'
    needs = [f for f in _STAGE_B_NEEDS if not _settled(f)]
    budget = _BUDGET['obj']
    if tier == 3:
        return dict(firm_a, deferred=False)
    if not needs:
        log.info(f'  → No search ({name}): nothing left to learn')
        return dict(firm_a, deferred=False)
    if needs == ['size'] and 'oracle' in srcs.values():
        # Phase 3 B2: the registry settled everything fit-relevant (FDIC and
        # ProPublica publish no headcount). `size` never changes a verdict —
        # a search for it alone is quota spent on a nice-to-have.
        log.info(f'  → No search ({name}): registry-settled, only headcount unknown')
        return dict(firm_a, deferred=False)
    if budget.exhausted():
        key = _gates_account_key(name)
        if not (key and _account_cache().get_search(key, 'firmographic')):
            log.info(f'  → No search ({name}): event search budget spent')
            return dict(firm_a, deferred=False)
        log.info(f'  → Budget spent, but the search is cached ({name}) — free lookup')
    log.info(f'  → Searching: {name} (needs {", ".join(needs)})')
    got = enrich_one_company(name, _industry_hint_for(co),
                             article_context=article_ctx, no_search=False,
                             require_search=True)
    time.sleep(RATE_LIMIT_SECONDS)
    return _merge_search(firm_a, got, firm_a.get('_seeds') or {})


def _remember_firmographics(cache, name: str, firm: dict) -> None:
    """Persist the RESEARCHED firmographic fields for the account (per-field
    TTLs live in the cache module; a None never clobbers a stored value).

    Provenance gate (review 2026-09-07): the cache lives up to 365 days and
    every later event of the account starts from it, so an article-only
    guess written here poisoned them all. Per `_sources`:
      * hq / revenue / industry — a structured seed, a registry (Phase 3
        'oracle') or a search, never the article pass;
      * zi_subindustry — registry- or search-derived, or article at High
        confidence (the same bar that lets it tombstone), with its
        confidence alongside;
      * url / linkedin / size — registry or search only;
      * cache-sourced values are not re-stamped (that would extend a TTL
        without new evidence). A firm with no provenance persists nothing."""
    key = _gates_account_key(name)
    if not key:
        return
    srcs = firm.get('_sources') or {}
    conf = firm.get('classification_confidence')
    payload = {}
    for f in _FIRM_FIELDS:
        v, src = firm.get(f), srcs.get(f)
        if not v:
            continue
        if f in ('url', 'linkedin', 'size'):
            keep = src in ('oracle', 'search')
        elif f == 'zi_subindustry':
            keep = src in ('oracle', 'search') or (src == 'article' and conf == 'High')
        else:                       # hq, revenue, revenue_source, industry
            keep = src in ('seed', 'oracle', 'search')
        if keep:
            payload[f] = v
    if 'zi_subindustry' in payload:
        payload['classification_confidence'] = conf
    if payload:
        cache.set_firmographics(key, payload)


def _resolve_company_domain(firm: dict, name: str, cache) -> dict:
    """Phase 3 B4 hook: attach `domain` / `domain_method` from the free
    resolver (src.pipeline.domains.resolve — hint url → cache → oracle
    tables → FDIC → SEC → Clearbit) when the company has none yet. The
    resolver persists a confident answer to the AccountCache itself; a
    cache-served answer reports the method that originally found it.
    Optional module, fail-soft: no resolver or any error → firm untouched."""
    if _resolve_domain is None or firm.get('domain'):
        return firm
    try:
        # H2 (review 2026-09-08): a hint reaches the resolver only when it
        # was RESEARCHED — seed / oracle / search / cache provenance. The
        # article-only LLM pass invents plausible urls from the company
        # name, and a name-derived url passes the resolver's name-token
        # test by construction; it must never become the account's
        # identity. Same for hq (a dateline is not an HQ) and linkedin (an
        # invented profile URL would become a dedup alias). M4: no `zi` —
        # the resolver wants a ZoomInfo URL/id there, not a subindustry
        # label ('zoominfo:banking' was written to every account).
        srcs = firm.get('_sources') or {}
        hints = {k: firm.get(k) for k in ('url', 'hq', 'linkedin')
                 if firm.get(k) and srcs.get(k) in ('seed', 'oracle', 'search', 'cache')}
        res = _resolve_domain(name, hints, cache=cache) or {}
        domain = res.get('domain')
        if not domain:
            return firm
        method = res.get('method')
        if method == 'cache':
            method = (res.get('evidence') or {}).get('cached_method') or 'cache'
        log.info(f'     domain: {domain} [{method}]')
        return dict(firm, domain=domain, domain_method=method)
    except Exception as e:      # noqa: BLE001 — identity is a bonus, never a blocker
        log.debug(f'  domain resolution failed for {name}: {e}')
        return firm


def _pre_search_view(firm: dict) -> dict:
    """The copy of a Stage-A firm dict that the PRE-SEARCH gates may judge
    (review 2026-09-07). Article-only hq / revenue are blanked (unknown): a
    dateline is where the release was issued, not necessarily the HQ, and
    an inferred revenue band is a guess — each was failing tier-1/2 events
    with zero searches. An article-only industry stays only at High
    classification confidence (the blocklist bar). Seed, cache and search
    values pass through; zi_subindustry was already settled by
    _article_other_decision. Stage B still receives the full dict — nothing
    is lost, it is only withheld from the early exit."""
    srcs = firm.get('_sources') or {}
    view = dict(firm)
    for f in ('hq', 'revenue', 'revenue_source'):
        if srcs.get(f) == 'article':
            view[f] = None
    if (srcs.get('industry') == 'article'
            and firm.get('classification_confidence') != 'High'):
        view['industry'] = None
    return view


def _acquire_run_lock(path: str = None):
    """Single-instance guard for __main__: the RunLock, or None when another
    enrichment process holds it (launchd fired while a slow run — or a
    terminal run — is still going; two runs would double-spend the budget)."""
    lock = RunLock(path or os.path.join(_STATE_DIR, 'enrichment.lock'))
    if not lock.acquire():
        log.info(f'another enrichment run is active (pid {lock.holder_pid()}) — exiting')
        return None
    return lock


# ── Phase 4 slice C2 (2026-09-08): the accounts-table hooks ─────────────────
# The accounts table (src/pipeline/accounts.py, slice C1, another engineer)
# makes the ACCOUNT the primary object: one grade per account, research done
# once, rep verdicts on the account. Everything here is a no-op when the
# module is absent or its probe says the table is not live yet (A.J. runs
# 003_accounts.sql later), and any failure inside is logged and swallowed —
# the event write has already happened, and account bookkeeping must never
# fail a run or a row.

# "When were the firmographics last refreshed" = firmographics.researched_at,
# and ONLY that. Review 2026-09-08 (Phase 4), 2a: the enrich-once check used
# to fall through to accounts.updated_at, which EVERY write refreshes — the
# enrich-once pass itself, a facts-only touch, the backfill — so a verified
# account was never re-researched again. _sync_accounts stamps
# researched_at when the chosen company's facts came from research this
# run (seed / registry / structured / search / cache — never 'account',
# the enrich-once provenance); a row without the stamp is NOT fresh.
_ACCOUNT_FRESH_KEYS = ('researched_at',)
# Provenance that counts as RESEARCHED for the enrich-once skip: the same
# set _remember_firmographics lets into the AccountCache (never the article
# pass, never a previous account-row read).
_RESEARCHED_SOURCES = frozenset({'seed', 'structured', 'oracle', 'search', 'cache'})


def _accounts_present(client) -> bool:
    """True when the accounts module is importable AND its (memoized) probe
    finds the table live."""
    if _accounts is None:
        return False
    try:
        return bool(_accounts.probe_accounts(client))
    except Exception as e:      # noqa: BLE001 — the events path never depends on it
        log.debug(f'accounts probe failed: {e}')
        return False


def _account_row(client, key: str):
    """The accounts row for an account_key, or None. Uses accounts.load_account
    when the module offers one, else ONE select on the primary key."""
    if not key or _accounts is None:
        return None
    try:
        loader = getattr(_accounts, 'load_account', None)
        if loader is not None:
            return loader(client, key) or None
        rows = (client.table('accounts').select('*').eq('account_key', key)
                .limit(1).execute().data or [])
        return rows[0] if rows else None
    except Exception as e:      # noqa: BLE001
        log.debug(f'accounts read failed for {key!r}: {e}')
        return None


def _account_firmographics(row: dict) -> dict:
    fm = (row or {}).get('firmographics')
    if isinstance(fm, str):
        try:
            fm = json.loads(fm)
        except ValueError:
            fm = None
    return fm if isinstance(fm, dict) else {}


def _account_fresh_ts(row: dict):
    """When the row's facts were last RESEARCHED (firmographics.researched_at),
    or None — never updated_at (see _ACCOUNT_FRESH_KEYS)."""
    fm = _account_firmographics(row)
    for k in _ACCOUNT_FRESH_KEYS:
        ts = parse_ts(fm.get(k)) if fm.get(k) else None
        if ts:
            return ts
    return None


def _account_verified_on(row: dict) -> str:
    ts = _account_fresh_ts(row or {})
    return ts.date().isoformat() if ts else '?'


def _account_is_researched(row: dict) -> bool:
    """Were the row's fit-relevant facts established by research (seed /
    registry / search / cache), not by the article pass? A post-search fit
    gate can verify on an article-only hq when the search found nothing;
    the AccountCache never persists such a value and neither may the
    enrich-once skip rely on it. Judged from firmographics.field_sources
    and classified_by; a row with neither is not researched."""
    fm = _account_firmographics(row)
    srcs = fm.get('field_sources') if isinstance(fm.get('field_sources'), dict) else {}
    by = str((row or {}).get('classified_by') or fm.get('classified_by') or '').strip().lower()
    zi_src = str(srcs.get('zi_subindustry') or by).strip().lower()
    hq_src = str(srcs.get('hq') or '').strip().lower()
    if zi_src not in _RESEARCHED_SOURCES:
        return False
    return hq_src in _RESEARCHED_SOURCES or (not hq_src and by in _RESEARCHED_SOURCES and bool(row.get('hq_state')))


def _known_account(client, name: str, now: datetime = None):
    """The accounts row that lets Stage A skip research for `name`:
    verify_state 'verified', no rep disposition, researched facts
    (_account_is_researched), refreshed within ACCOUNT_FRESH_DAYS. None for
    unknown / stale / unverified / dispositioned / article-only accounts —
    those research as usual."""
    row = _account_row(client, _gates_account_key(name))
    if not row or str(row.get('verify_state') or '').strip().lower() != 'verified':
        return None
    if row.get('disposition'):
        return None
    if not _account_is_researched(row):
        return None
    ts = _account_fresh_ts(row)
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if (now - ts).days > ACCOUNT_FRESH_DAYS:
        return None
    return row


def _account_facts(row: dict) -> dict:
    """Firmographic fields in the Stage A shape (_FIRM_FIELDS + domain +
    classification_confidence) from an accounts row: the `firmographics`
    JSON when the row carries one, then the typed columns (hq_state → hq —
    a state code is exactly the territory fact the gates read;
    revenue_segment → revenue; domain → url)."""
    facts = {}
    fm = _account_firmographics(row)
    for f in _FIRM_FIELDS + ('domain', 'classification_confidence'):
        if fm.get(f):
            facts[f] = fm[f]
    for f in _FIRM_FIELDS + ('domain', 'classification_confidence'):
        if not facts.get(f) and row.get(f):
            facts[f] = row[f]
    if not facts.get('hq') and row.get('hq_state'):
        facts['hq'] = str(row['hq_state'])
    if not facts.get('revenue') and row.get('revenue_segment') in ('LMM', 'MM', 'Corp', 'Enterprise'):
        facts['revenue'] = row['revenue_segment']
    if not facts.get('size') and row.get('size_bucket'):
        facts['size'] = str(row['size_bucket'])
    if not facts.get('url') and facts.get('domain'):
        facts['url'] = f"https://{facts['domain']}"
    return facts


def _company_is_researched(company: dict) -> bool:
    """Did THIS run establish the company's fit facts by research — a
    structured seed, a registry, a search, the AccountCache — rather than
    the article pass or a previous accounts-row read ('account')? Judged
    from the record's field_sources / classified_by, the same provenance
    vocabulary _account_is_researched reads back from the row."""
    company = company or {}
    srcs = company.get('field_sources') if isinstance(company.get('field_sources'), dict) else {}
    by = str(company.get('classified_by') or '').strip().lower()
    zi_src = str(srcs.get('zi_subindustry') or by).strip().lower()
    hq_src = str(srcs.get('hq') or '').strip().lower()
    return zi_src in _RESEARCHED_SOURCES and hq_src in _RESEARCHED_SOURCES


def _stamp_researched(row: dict, company: dict, now: datetime) -> dict:
    """firmographics.researched_at = now on the account row when the chosen
    company was researched this run (review 2026-09-08 (Phase 4), 2a) —
    the stamp _known_account reads for enrich-once freshness. An
    'account'-sourced company (enrich-once itself) or an article-only one
    leaves the stamp alone, so a skip never refreshes it."""
    if not _company_is_researched(company):
        return row
    fm = row.get('firmographics')
    fm = dict(fm) if isinstance(fm, dict) else {}
    fm['researched_at'] = now.isoformat()
    row['firmographics'] = fm
    return row


def _upsert_takes_is_new_event() -> bool:
    """Does accounts.upsert_account accept is_new_event= (C1 ≥ 2026-09-08)?"""
    try:
        return 'is_new_event' in inspect.signature(_accounts.upsert_account).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def _sync_accounts(client, event: dict, companies: list, fit: dict, grading: dict = None,
                   tombstoned: bool = False, now: datetime = None,
                   is_new_event: bool = None) -> tuple:
    """Account bookkeeping after an event write → (upserted, touched).

    `is_new_event` (review 2026-09-08 (Phase 4), 2c): whether this event
    is being processed for the FIRST time (enrich_attempts was 0 and the
    run is not --re-enrich) — passed through to accounts.upsert_account so
    a re-processed event (a retry, a re-verify, a regrade) does not inflate
    the account's event_count; None lets the module infer it from the ids
    (the pre-2c behaviour) until the accounts owner's seen_event_ids lands.

    ONE GRADE PER ACCOUNT (plan approved by A.J. 2026-09-06; supersedes the
    2026-07-17 per-company / headline rule): the event's grade belongs to
    the CHOSEN account — fit.account_name, picked by apply_fit_gates (pass >
    unverified > staged, by role priority) — and is upserted there;
    accounts.upsert_account replaces a stored grade only when the new one is
    better, a re-grade of the same event, or the old one expired, and never
    touches a rep disposition. Every OTHER workable company whose own fit is
    not 'fail' is touched facts-only: it exists, it was not graded. A
    tombstoned event (fit fail, industry block) touches its chosen company
    facts-only too — the account exists even when this trigger was not
    workable — and never grades it."""
    if _accounts is None or not companies:
        return 0, 0
    now = now or datetime.now(timezone.utc)
    fit = fit or {}
    upserted = touched = 0
    chosen = _account_company(companies, fit)
    chosen_nm = (chosen.get('name') or '').strip()
    _kw = {'now': now}
    if is_new_event is not None and _upsert_takes_is_new_event():
        _kw['is_new_event'] = is_new_event

    def _facts_only(company, cfit):
        # A tombstoned event: the account exists (name, facts, counts, seen
        # dates) but this event is neither its trigger nor its grade —
        # build_account_row(trigger_live=False). touch_secondary is not used
        # here because it refuses a failed fit, and a failed fit IS the
        # fact a tombstone establishes about the account.
        try:
            row = _accounts.build_account_row(event, company, cfit, grading=None, now=now,
                                              trigger_live=False)
        except TypeError:                   # an accounts module without trigger_live
            row = _accounts.build_account_row(event, company, cfit, grading=None, now=now)
        if company is chosen:
            row = _stamp_researched(row, company, now)
        return bool(_accounts.upsert_account(client, row, **_kw))

    try:
        if chosen_nm:
            if tombstoned:
                touched += _facts_only(chosen, chosen.get('fit') or fit)
            else:
                row = _accounts.build_account_row(event, chosen, fit, grading=grading, now=now)
                row = _stamp_researched(row, chosen, now)
                if _accounts.upsert_account(client, row, **_kw):
                    upserted += 1
        for c in companies:
            nm = (c.get('name') or '').strip()
            if not nm or nm == chosen_nm:
                continue
            if (c.get('role') or '').lower() not in WORKABLE_ROLES:
                continue
            if (c.get('fit') or {}).get('verdict') == 'fail':
                continue
            if tombstoned:
                touched += _facts_only(c, c.get('fit') or {})
            elif _accounts.touch_secondary(client, c, event, c.get('fit') or {}, now=now):
                touched += 1
    except Exception as e:      # noqa: BLE001 — bookkeeping never fails the row
        log.warning(f'  accounts bookkeeping failed: {e}')
    return upserted, touched


def _tal_for(grading: dict) -> dict:
    """The compact per-account grade stored on the chosen company record."""
    return {'grade': grading.get('grade'),
            'score': grading.get('numeric_score'),
            'confidence': grading.get('confidence'),
            'hashtags': grading.get('hashtags') or [],
            'justification': grading.get('grade_justification')}


def _attach_account_grade(companies: list, account_name: str, grading: dict) -> None:
    """ONE GRADE PER ACCOUNT: `tal` on the chosen account only; a stale
    secondary grade left by the pre-Phase-4 per-company loop is removed."""
    nm = (account_name or '').strip()
    for c in companies:
        if not isinstance(c, dict):
            continue
        c.pop('tal', None)
        if nm and (c.get('name') or '').strip() == nm and (grading or {}).get('grade'):
            c['tal'] = _tal_for(grading)


# ── Main ──────────────────────────────────────────────────────────────────────

def enrich_events(
    limit: int = None,
    re_enrich: bool = False,
    dry_run: bool = False,
    missing_fit_only: bool = False,
    reverify_unverified: bool = False,
    complexity_sweep: bool = False,
    estimate_only: bool = False,
    confirm_credits: int = None,
):
    check_required_keys()
    client  = get_supabase()
    try:
        col_ok = check_columns(client)
    except ProbeUnavailable as e:
        _abort_transport(e)
    _transport_aborts(reset=True)
    backend = _llm_backend()

    if not col_ok.get('companies_data'):
        # Only reachable when Postgres itself said the column is absent.
        log.warning(f'companies_data column missing. {MIGRATION_SQL}')
        if not dry_run:
            sys.exit(1)

    log.info(f'LLM backend: {backend} '
             f'({"claude-3-5-haiku" if backend == "anthropic" else LLAMACPP_MODEL} '
             f'-> Scout fallback)')

    # ── Fetch events ──────────────────────────────────────────────────────
    # Include source_url so grade_event() can cite the original article in
    # research_notes; published_date/discovered_at feed the typed expires_at.
    typed_cols = col_ok.get('typed') or set()
    cols = ('id, company_name, event_type, title, description, source_url, fit, '
            'published_date, discovered_at')
    retry_cols = [c for c in ('enrich_attempts', 'retry_after', 'verify_state')
                  if c in typed_cols]
    if retry_cols:
        cols += ', ' + ', '.join(retry_cols)
    if col_ok.get('matched_regions'):       # Phase 3: registry state anchor
        cols += ', matched_regions'
    query = client.table('events').select(cols)
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
        if {'verify_state', 'enrich_attempts'} <= typed_cols:
            # Phase 2: typed state machine — rows past MAX_ENRICH_ATTEMPTS
            # are negative-cached (a NEW event for the account is a new row).
            query = (query.in_('verify_state', ['staged', 'researched_ambiguous'])
                          .lt('enrich_attempts', MAX_ENRICH_ATTEMPTS))
        else:
            query = query.in_('fit->>verdict', ['unverified', 'staged'])
    if complexity_sweep and col_ok.get('fit'):
        # A-hunt mode: fit-CONFIRMED Grade-B events with a high-intent
        # trigger type — the only population the complexity probe
        # (#Locations/#Entities/#Global/#Franchisor, +2 each) can promote
        # to Grade A. Firmographic searches ride the 30-day cache, so the
        # marginal cost is ~1 complexity search + regrade per event.
        query = (query.eq('grade', 'B')
                      .eq('fit->>verdict', 'pass')
                      .in_('event_type',
                           ['cfo_hire', 'merger_acquisition', 'funding']))
    if 'retry_after' in typed_cols and (reverify_unverified or not re_enrich):
        # Phase 2 retry semantics: a row parked by the 7/30-day ladder (or
        # the LLM-outage 4h push) is invisible until its retry_after passes.
        # An explicit --re-enrich sweep is a human decision and ignores it.
        now_iso = datetime.now(timezone.utc).isoformat()
        query = query.or_(f'retry_after.is.null,retry_after.lte.{now_iso}')
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
    events = _fetch_all(query, order='discovered_at', desc=False)

    if reverify_unverified:
        # v2: re-verification is RANKED and CAPPED — best trigger types first,
        # fewest unknown dimensions first — never the whole backlog at once.
        _prio = {'cfo_hire': 0, 'finance_seat_open': 1, 'merger_acquisition': 2,
                 'funding': 3}
        def _unknown_dims(ev):
            f = ev.get('fit') or {}
            if isinstance(f, str):
                try:
                    f = json.loads(f)
                except Exception:
                    f = {}
            return sum(1 for r in (f.get('reasons') or []) if 'unverified' in str(r))
        events.sort(key=lambda ev: (_prio.get(ev.get('event_type'), 9), _unknown_dims(ev)))
        if not limit:
            limit = 50
            log.info('Re-verify capped at 50 events/run (ranked by trigger value); '
                     'pass --limit to change')
    if limit:
        events = events[:limit]

    if not events:
        log.info('No unenriched events — nothing to do.')
        return

    if estimate_only or (re_enrich and confirm_credits is not None):
        n = len(events)
        est_searches = n * 2
        est_tavily = int(round(est_searches * 0.3))
        month_used = _tavily_month_count()
        log.info(f'ESTIMATE: {n} events → ~{est_searches} searches → ~{est_tavily} '
                 f'Tavily credits (30% fallback rate); month used {month_used}/'
                 f'{TAVILY_MONTHLY_BUDGET}, daily ration {TAVILY_DAILY_RATION}')
        if estimate_only:
            return
        if confirm_credits is not None and confirm_credits < est_tavily:
            sys.exit(f'Refusing: --confirm-credits {confirm_credits} < estimate '
                     f'{est_tavily}. Re-run with --limit or a higher confirmation.')
    elif re_enrich and len(events) > 50 and not dry_run:
        sys.exit(f'Bulk mode over {len(events)} events needs a pre-flight: run with '
                 f'--estimate, then --confirm-credits N (or --limit ≤ 50).')

    tag = 'DRY RUN — ' if dry_run else ''
    log.info(f'{tag}Processing {len(events)} event(s)')
    print()

    # ── LLM canary (Phase 2) — the local server must answer before any row
    # is touched; otherwise every event would fall back to "no companies →
    # enriched_at stamped" and the queue would be silently marked done.
    if not _llm_canary():
        log.error(f'FAIL: local LLM at {LLAMACPP_URL} is unavailable — nothing '
                  f'processed, nothing stamped. Check the llama.cpp server and re-run.')
        return

    firm_cache: dict = {}          # per-run: same company across events = one pass
    acct_cache = _account_cache()  # persistent, account-keyed (src/pipeline/cache.py)
    reset_search_counts()
    ok = fail = 0
    outcomes = {'verified': 0, 'ambiguous': 0, 'staged': 0, 'not_fit': 0,
                'decided': 0, 'llm_unavailable': 0}
    dispositions = _load_rep_dispositions(client)
    if dispositions:
        log.info(f'Loaded {len(dispositions)} rep account verdict(s) — decided accounts skip research')
    # Phase 4: the accounts table (one grade per account, enrich-once). Probed
    # once per run; False until the module lands AND A.J. runs 003_accounts.sql.
    _acct_on = _accounts_present(client)
    if _acct_on:
        log.info('accounts table present — one grade per account; verified accounts '
                 f'fresh within {ACCOUNT_FRESH_DAYS}d skip research')
    _acct_counts = {'upserted': 0, 'touched': 0}
    _guards_stripped = 0

    _unavail_streak = False
    for idx, event in enumerate(events, 1):
        eid   = event['id']
        title = (event.get('title') or '')[:80]
        etype = event.get('event_type', '')
        log.info(f'[{idx}/{len(events)}] {title}')
        if not _unavail_streak:
            LLM_STATE['consecutive'] = 0
        _unavail_streak = False
        attempts_prev = _prev_attempts(event)
        # First time this event is processed? (2c: re-processing must not
        # inflate the account's event_count.)
        _is_new = attempts_prev == 0 and not re_enrich
        _sv = _structured_verdict(event)   # free; also feeds the typed columns

        # ── 0. Board-only gate (free, before any LLM/search spend) ────────
        # Pure board-of-directors changes are not triggers — tombstone.
        if _board_only_event(event):
            log.info('  🚫 Board-of-directors change only (no finance role) '
                     '— soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid,
                             'board_change_only: director/board appointment, '
                             'no finance-leader role',
                             typed=_tombstone_typed(event, typed_cols, structured=_sv))
            ok += 1
            outcomes['not_fit'] += 1
            continue

        # ── 1. Extract companies + roles ──────────────────────────────────
        companies = extract_event_companies(event)
        if LLM_STATE['unavailable']:
            outcomes['llm_unavailable'] += 1
            _unavail_streak = True
            if _llm_unavailable_event(client, eid, typed_cols, attempts_prev, dry_run):
                break
            continue

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

        # ── v2 pre-search gates (all free; run BEFORE any LLM/search spend) ──
        _ctx_text = f"{event.get('title') or ''} {event.get('description') or ''}"
        _kept = [c for c in companies
                 if not is_bad_company_name(c.get('name') or '', _ctx_text)]
        if len(_kept) < len(companies):
            log.info('  → Dropping junk company name(s): '
                     + ', '.join(c['name'] for c in companies if c not in _kept))
        companies = _kept
        if not companies:
            log.info('  🚫 No real company name extracted — soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid, 'bad_company_name: no real company name extracted',
                             typed=_tombstone_typed(event, typed_cols, structured=_sv))
            ok += 1
            outcomes['not_fit'] += 1
            continue
        if not any((c.get('role') or '').lower() in WORKABLE_ROLES for c in companies):
            log.info('  🚫 No workable-role company (advisors/investors only) — soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid, 'no_workable_account: only advisor/investor/'
                                          'mentioned roles extracted',
                             typed=_tombstone_typed(event, typed_cols, structured=_sv))
            ok += 1
            outcomes['not_fit'] += 1
            continue
        _rep = _rep_verdict_for(companies, dispositions)
        if _rep:
            _rname, _rstatus = _rep
            _mini = [{'name': c['name'], 'role': c['role']} for c in companies]
            if _rstatus in REP_NOT_FIT_STATUSES:
                log.info(f'  🚫 Rep verdict "{_rstatus}" on {_rname} — soft-deleting, no research.')
                if not dry_run:
                    _soft_delete(client, eid, f'rep:{_rstatus} ({_rname[:60]})',
                                 extra={'companies_data': _mini},
                                 typed=_tombstone_typed(
                                     event, typed_cols, structured=_sv,
                                     fit={'verdict': 'fail', 'account_name': _rname}))
                outcomes['not_fit'] += 1
            else:
                log.info(f'  ✋ Rep verdict "{_rstatus}" on {_rname} — decided, no research.')
                if not dry_run:
                    _fit_d = {'verdict': 'decided', 'account_name': _rname,
                              'territory': 'n/a', 'revenue': 'n/a', 'vertical': 'n/a',
                              'reasons': [f'rep: {_rstatus}']}
                    _pl = {'companies_data': _mini, 'fit': _fit_d}
                    if col_ok.get('enriched_at'):
                        _pl['enriched_at'] = datetime.utcnow().isoformat()
                    _pl.update(typed_payload(event=event, fit=_fit_d, structured=_sv,
                                             verify_state='decided', present=typed_cols,
                                             retry_after=None))
                    try:
                        client.table('events').update(_pl).eq('id', eid).execute()
                    except Exception as _e:
                        log.warning(f'  write failed: {_e}')
                outcomes['decided'] += 1
            ok += 1
            continue
        if _sv['verdict'] in ('out', 'vehicle', 'too_small'):
            log.info(f'  🚫 Structured gate ({_sv["verdict"]}): {_sv["reason"]} — soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid, f'structured:{_sv["verdict"]}: {_sv["reason"]}',
                             typed=_tombstone_typed(event, typed_cols, structured=_sv))
            ok += 1
            outcomes['not_fit'] += 1
            continue
        # H3 / P4: an SEC-registered adviser is the management company —
        # its '... LP' name is not a fund vehicle (see _entity_shape).
        _registry = 'sec_iapd' if _is_iapd_event(event) else None
        _workable_ops = [c for c in companies
                         if (c.get('role') or '').lower() in WORKABLE_ROLES
                         and not _entity_shape(c.get('name') or '',
                                               c.get('descriptor') or '', _registry)[0]]
        if not _workable_ops:
            _kinds = {_entity_shape(c.get('name') or '', c.get('descriptor') or '', _registry)[1]
                      for c in companies if (c.get('role') or '').lower() in WORKABLE_ROLES}
            log.info(f'  🚫 Every workable company is a non-operating entity '
                     f'({", ".join(sorted(k for k in _kinds if k))}) — soft-deleting.')
            if not dry_run:
                # Review 2026-09-08 (Phase 4): the names travel with the
                # tombstone so the golden-set exporter can reproduce WHICH
                # name produced the kind (the reason truncates names).
                _mini = [{'name': c.get('name'), 'role': c.get('role'),
                          'descriptor': c.get('descriptor') or ''} for c in companies]
                _soft_delete(client, eid, 'entity_shape:' + ','.join(sorted(k for k in _kinds if k)),
                             extra={'companies_data': _mini},
                             typed=_tombstone_typed(event, typed_cols, structured=_sv))
            ok += 1
            outcomes['not_fit'] += 1
            continue

        # ── 2. CLASSIFY, then RESEARCH (Phase 2, 2026-09-07) ─────────────
        # Stage A is free (structured seeds → account cache → one article-
        # only LLM pass per company) and can END the event on its own; only
        # survivors with something fit-relevant left to learn reach Stage B,
        # where the SearchBudget for the tier is spent.
        tier = _event_search_tier(event)
        _SEARCH_TIER['tier'] = tier
        _BUDGET['obj'] = SearchBudget(tier)
        if tier == 3:
            log.info('  Search tier 3 (micro-raise/minimal) — grading from '
                     'article evidence only, no web searches')
        elif tier == 2:
            log.info('  Search tier 2 — scrape-only (no Tavily spend)')
        _article_ctx = _article_context(event)
        _has_workable = any((c.get('role') or '').lower() in WORKABLE_ROLES
                            for c in companies)

        # ── STAGE A ──
        stage = []      # (company, firm | None) — None = placeholder, never researched
        for co in companies:
            name, role = co['name'], co['role']
            _role_l = (role or '').lower()
            # (a) Non-workable roles (advisors, sellers, ...) can never
            #     become the account — no LLM, no search, when the event
            #     has at least one workable company.
            if _role_l and _role_l not in WORKABLE_ROLES and _has_workable:
                log.info(f'  → Skipping ({role}): {name}')
                stage.append((co, None))
                continue
            # (b) Auto-fail names (public school districts, non-operating
            #     entities) fail the fit gate on name alone.
            _nonop_kind = _entity_shape(name, co.get('descriptor') or '', _registry)[1]
            if _is_public_school_district(name) or _nonop_kind:
                log.info(f'  → Skipping ({_nonop_kind or "public school district"}): {name}')
                stage.append((co, None))
                continue
            run_key = name.lower().strip()
            if run_key in firm_cache:
                # Same company, earlier event this run: reuse its Stage A/B
                # result and force no_search — but KEEP its `deferred` flag
                # (review 2026-09-07: dropping it turned a throttled first
                # lookup into a stamped 'staged' row for the second event).
                log.info(f'  → Cached (this run): {name}')
                firm = dict(firm_cache[run_key], no_search=True)
            else:
                # Phase 4 enrich-once: a fresh VERIFIED accounts row settles
                # the company with zero LLM / search / probe spend.
                _known = _known_account(client, name) if _acct_on else None
                firm = _stage_a_company(event, co, _sv, _article_ctx, acct_cache,
                                        dry_run=dry_run, account_row=_known)
            stage.append((co, firm))
        if LLM_STATE['unavailable']:
            outcomes['llm_unavailable'] += 1
            _unavail_streak = True
            if _llm_unavailable_event(client, eid, typed_cols, attempts_prev, dry_run):
                break
            continue

        # Event-level early exit on FREE evidence: industry blocklist + fit
        # gates over the article-classified companies. Nothing has been
        # searched yet, so a fail here costs zero quota — which is exactly
        # why it may only act on RESEARCHED facts (review 2026-09-07): the
        # gates see _pre_search_view() copies with article-only hq/revenue
        # blanked and an article-only industry kept only at High confidence.
        # What can fail here: the name/entity gates above, the structured
        # SEC verdict, zi OTHER at High confidence (or structured agreement)
        # and a High-confidence blocklisted industry. Everything else goes
        # on to Stage B; the full gates run on the merged data afterwards.
        _probe = [_with_registry(_company_record(c['name'], c['role'], _pre_search_view(f)) if f
                                 else _placeholder_company(c['name'], c['role']), _registry)
                  for c, f in stage]
        _probe = copy.deepcopy(_probe)      # apply_fit_gates attaches c['fit'] in place
        _pri = pick_primary(_probe)
        _blk, _kw = industry_is_blocked(_pri.get('industry') or '')
        _early = apply_fit_gates(_probe)
        if _blk or _early['verdict'] == 'fail':
            if _blk:
                _reason = f'industry: {_pri.get("industry")} (matched "{_kw}")'
                log.info(f'  🚫 Industry "{_pri.get("industry")}" matched "{_kw}" '
                         f'(decided from article + structured facts — no search). '
                         f'Soft-deleting.')
            else:
                _reason = f'fit_gate: {"; ".join(_early["reasons"])}'
                log.info(f'  🚫 Fit gate FAIL — {"; ".join(_early["reasons"])} '
                         f'(decided from article + structured facts — no search). '
                         f'Soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid, _reason,
                             extra={'companies_data': _probe, 'fit': _early},
                             typed=_tombstone_typed(event, typed_cols, fit=_early,
                                                    structured=_sv,
                                                    account=_account_company(_probe, _early)))
                if _acct_on:        # the account exists; facts only, no grade
                    _acct_counts['touched'] += _sync_accounts(
                        client, event, _probe, _early, tombstoned=True, is_new_event=_is_new)[1]
            ok += 1
            outcomes['not_fit'] += 1
            continue

        # ── STAGE B (survivors only; dry-run never searches) ──
        enriched = []
        _stage_firms = {}       # name → firm dict, for the domain hook after the fit gates
        for co, firm in stage:
            name, role = co['name'], co['role']
            if firm is None:
                enriched.append(_with_registry(_placeholder_company(name, role), _registry))
                continue
            if not firm.get('no_search') and not dry_run:
                firm = _stage_b_company(co, firm, _article_ctx, tier)
            elif firm.get('no_search') and firm.get('zi_subindustry') == 'OTHER':
                log.info(f'  → No search ({name}): article says OTHER '
                         f'({firm.get("classification_confidence") or "structured"} confidence)')
            run_key = name.lower().strip()
            firm_cache[run_key] = firm
            if not dry_run:
                _remember_firmographics(acct_cache, name, firm)
            _srcs = firm.get('_sources') or {}      # [seed|oracle|cache|article|search]
            found = [f'{k}: {firm[k]}' + (f' [{_srcs[k]}]' if _srcs.get(k) else '')
                     for k in _FIRM_FIELDS + ('classified_by',) if firm.get(k)]
            if found:
                log.info(f'     {" | ".join(found)}')
            enriched.append(_with_registry(_company_record(name, role, firm), _registry))
            _stage_firms[name] = firm
        if LLM_STATE['unavailable']:
            outcomes['llm_unavailable'] += 1
            _unavail_streak = True
            if _llm_unavailable_event(client, eid, typed_cols, attempts_prev, dry_run):
                break
            continue

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
                             f'industry: {primary.get("industry")} (matched "{kw}")',
                             extra={'companies_data': enriched},
                             typed=_tombstone_typed(event, typed_cols, structured=_sv))
                if _acct_on:
                    _acct_counts['touched'] += _sync_accounts(
                        client, event, enriched, {}, tombstoned=True, is_new_event=_is_new)[1]
            ok += 1  # count as processed (not failed)
            outcomes['not_fit'] += 1
            continue

        # ── 4. FIT GATES (deterministic, post-research) ───────────────────
        # Territory × revenue band × ZI-subindustry allowlist. Confirmed-out
        # on any dimension → soft-delete. Unknowns → keep, cap grade at B,
        # flag for a 10-second rep verification.
        fit = apply_fit_gates(enriched)
        _event_deferred = any(c.get('deferred') for c in enriched)
        for c in enriched:
            c.pop('deferred', None)
        if fit['verdict'] == 'fail':
            log.info(f'  🚫 Fit gate FAIL — {"; ".join(fit["reasons"])}. '
                     f'Soft-deleting.')
            if not dry_run:
                _soft_delete(client, eid, f'fit_gate: {"; ".join(fit["reasons"])}',
                             extra={'companies_data': enriched, 'fit': fit},
                             typed=_tombstone_typed(event, typed_cols, fit=fit,
                                                    structured=_sv,
                                                    account=_account_company(enriched, fit)))
                if _acct_on:
                    _acct_counts['touched'] += _sync_accounts(
                        client, event, enriched, fit, tombstoned=True, is_new_event=_is_new)[1]
            ok += 1
            outcomes['not_fit'] += 1
            continue
        # Phase 3 B4: website domain for the companies that SURVIVED the fit
        # gates — L6 (review 2026-09-08): the hook used to run inside the
        # Stage B loop, so a company about to be tombstoned still spent a
        # Clearbit / FDIC call. A company whose own fit failed (the acquirer
        # of a surviving target) can never be the account and is skipped
        # too; a dry run never resolves (the resolver may call keyless APIs).
        if not dry_run:
            for _rec in enriched:
                _firm = _stage_firms.get(_rec.get('name'))
                if _firm is None or (_rec.get('fit') or {}).get('verdict') == 'fail':
                    continue
                _resolved = _resolve_company_domain(_firm, _rec['name'], acct_cache)
                if _resolved.get('domain'):
                    _rec['domain'] = _resolved['domain']
                    _rec['domain_method'] = _resolved.get('domain_method')
                    firm_cache[_rec['name'].lower().strip()] = _resolved
        if fit['verdict'] == 'staged':
            # Vertical unknown → hidden from reps, no grading spend. If the
            # search was throttled/deferred, leave enriched_at NULL so the
            # next run retries for free (up to 3 attempts).
            _prev = event.get('fit') or {}
            if isinstance(_prev, str):
                try:
                    _prev = json.loads(_prev)
                except Exception:
                    _prev = {}
            attempts = int(_prev.get('deferred_attempts') or 0) + (1 if _event_deferred else 0)
            fit['deferred_attempts'] = attempts
            log.info(f'  ⏸  Staged (vertical unknown{", search deferred" if _event_deferred else ""}) '
                     f'— hidden; attempt {attempts}')
            if not dry_run:
                _pl = {'companies_data': enriched}
                if col_ok.get('fit'):
                    _pl['fit'] = fit
                if col_ok.get('enriched_at') and not (_event_deferred and attempts < 3):
                    _pl['enriched_at'] = datetime.utcnow().isoformat()
                _pl.update(_final_typed(event, typed_cols, fit, _sv, enriched,
                                        attempts_prev, deferred=_event_deferred))
                try:
                    client.table('events').update(_pl).eq('id', eid).execute()
                    if _acct_on:    # the account exists (staged); no grade yet
                        _u, _t = _sync_accounts(client, event, enriched, fit, grading=None,
                                                is_new_event=_is_new)
                        _acct_counts['upserted'] += _u
                        _acct_counts['touched'] += _t
                except Exception as _e:
                    log.warning(f'  write failed: {_e}')
            ok += 1
            outcomes['staged'] += 1
            continue
        if fit['verdict'] == 'unverified':
            log.info(f'  ⚠️  Fit unverified — {"; ".join(fit["reasons"])} '
                     f'(grade capped at B)')

        # ── 5. Research probes + TAL grading (A.J. rubric) ────────────────
        # Phase 4 enrich-once: a company settled from the accounts table
        # gets cache-only probes — the trigger is new, the research is not.
        _known_names = {c['name'] for c, f in stage if f and f.get('account_known')}
        _known_acct = (fit.get('account_name') or '') in _known_names
        extra_evidence = '' if dry_run else gather_extra_evidence(
            event, enriched, fit, known_account=_known_acct)
        log.info(f'  Grading via TAL rubric…')
        grading = grade_event(event, enriched, extra_evidence,
                              account_name=fit.get('account_name') or '', fit=fit)
        if LLM_STATE['unavailable']:
            outcomes['llm_unavailable'] += 1
            _unavail_streak = True
            if _llm_unavailable_event(client, eid, typed_cols, attempts_prev, dry_run):
                break
            continue
        # Unverified-fit cap: an A grade needs confirmed fit. (Agreed with
        # A.J. 2026-07-16: flag, don't hide.)
        if fit['verdict'] == 'unverified' and grading.get('grade') == 'A':
            grading['grade'] = 'B'
            grading['grade_justification'] = (
                '⚠️ Capped A→B: fit unverified '
                f'({"; ".join(r for r in fit["reasons"] if "unverified" in r)}). '
                + (grading.get('grade_justification') or '')
            )[:1000]

        # ── ONE GRADE PER ACCOUNT (Phase 4, plan approved by A.J. 2026-09-06;
        # supersedes the 2026-07-17 per-company grading + headline promotion).
        # The grade belongs to the CHOSEN account — fit.account_name, picked
        # by apply_fit_gates (pass > unverified > staged, by role priority) —
        # and is stored on that company record only; every other company
        # keeps its 'fit' chip and nothing else. The secondary grade_event
        # calls (one LLM pass per extra company) and the "best grade wins the
        # headline" promotion are gone: they produced several grades for one
        # event, let a target's grade relabel the account mid-write, and cost
        # an LLM call per company. A company's grade now lives on the
        # accounts table (_sync_accounts after the write), where the same
        # account seen through many events keeps ONE grade.
        account_nm = (fit.get('account_name') or '').strip()
        _guards_stripped += strip_count(grading.get('guard_notes'))
        _attach_account_grade(enriched, account_nm, grading)

        if grading.get('grade'):
            log.info(
                f'    Grade={grading["grade"]}  '
                f'Score={grading.get("numeric_score")}  '
                f'Hashtags={" ".join(grading["hashtags"]) or "(none)"}'
            )

        # ── 6. Write to Supabase ──────────────────────────────────────────
        if LLM_STATE['unavailable']:      # the grading call itself found the LLM down
            outcomes['llm_unavailable'] += 1
            _unavail_streak = True
            if _llm_unavailable_event(client, eid, typed_cols, attempts_prev, dry_run):
                break
            continue
        outcomes['verified' if fit['verdict'] == 'pass' else 'ambiguous'] += 1
        if dry_run:
            log.info(f'  Would write {len(enriched)} company record(s) + grade '
                     f'(fit={fit["verdict"]}, verify_state={verify_state_for(fit["verdict"])})')
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
        # Phase 2 typed columns (verified: retry_after cleared; researched_
        # ambiguous: attempts+1 and the 7/30-day ladder — unless the search
        # was deferred, which is not an attempt; review 2026-09-07).
        payload.update(_final_typed(event, typed_cols, fit, _sv, enriched,
                                    attempts_prev, deferred=_event_deferred))

        # Upgrade event_type to cfo_hire ONLY for true CFO-equivalent hires.
        # Controller/VP-Accounting hires stay executive_hire — relabeling them
        # caused a #NewCFO(+5) vs #NewController(+3) double-count on regrade.
        current_etype = (event.get('event_type') or '').lower()
        if (current_etype not in ('cfo_hire', 'finance_seat_open')
                and _finance_role(event) == 'cfo'):
            payload['event_type'] = 'cfo_hire'
            log.info(f'    Reclassifying event_type {current_etype!r} → cfo_hire')

        try:
            client.table('events').update(payload).eq('id', eid).execute()
            ok += 1
            if _acct_on:
                # Phase 4: the chosen account carries this event's grade;
                # the other workable, non-failed companies are touched
                # facts-only (they exist; they were not graded).
                _u, _t = _sync_accounts(client, event, enriched, fit,
                                        grading=grading if grading.get('grade') else None,
                                        is_new_event=_is_new)
                _acct_counts['upserted'] += _u
                _acct_counts['touched'] += _t
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
    o = outcomes
    # 'Searches' = lookups that entered the ladder, counted once each; the
    # rung figures are a breakdown, not addends (a Firecrawl→Tavily fallback
    # is ONE lookup — the old sum reported it as two; review 2026-09-07).
    log.info(
        f'Done — enriched: {ok}, failed: {fail}  ·  '
        f'verified:{o["verified"]} ambiguous:{o["ambiguous"]} staged:{o["staged"]} '
        f'not_fit:{o["not_fit"]} decided:{o["decided"]} '
        f'llm_unavailable:{o["llm_unavailable"]}  ·  '
        f'Searches: {sc["lookups"]} lookups '
        f'(firecrawl:{sc["firecrawl"]} firecrawl_attempts:{sc["firecrawl_attempts"]} '
        f'tavily:{sc["tavily"]} throttled:{sc.get("throttled", 0)} '
        f'transport_failed:{sc.get("transport_failed", 0)})  ·  '
        f'Served without a search: cache:{sc["cache"]} '
        f'negative_cache:{sc["negative_cache"]} '
        f'budget_skipped:{sc.get("budget_skipped", 0)} '
        f'oracle:{sc.get("oracle", 0)} '
        f'account:{sc.get("account", 0)}  ·  '
        f'Accounts upserted:{_acct_counts["upserted"]} touched:{_acct_counts["touched"]}  ·  '
        f'Guards stripped:{_guards_stripped}  ·  '
        f'Tavily month {_tavily_month_count()}/{TAVILY_MONTHLY_BUDGET}, '
        f'today {_tavily_day_count()}/{TAVILY_DAILY_RATION}'
    )


# ── Regrade-only mode (free — no Tavily, no firmographic re-fetch) ─────────

def regrade_only_events(limit: int = None, dry_run: bool = False,
                        event_type: str = None):
    """Re-apply ONLY the TAL grading + post-enrichment industry filter +
    event_type reclassification to existing events. Uses each event's existing
    companies_data — does NOT call Tavily and does NOT re-extract firmographics.

    Use this when you only want to apply NEW grading/classification rules to
    historical events without burning Tavily API credits. The local LLM
    (free) is still used for the grading call. `event_type` narrows the
    sweep (Phase 2: regrade finance_seat_open rows after the Adzuna relabel).
    """
    check_required_keys()  # not strictly needed (no Tavily) — but harmless
    client = get_supabase()
    try:
        col_ok = check_columns(client)
    except ProbeUnavailable as e:
        _abort_transport(e)
    _transport_aborts(reset=True)
    typed_cols = col_ok.get('typed') or set()
    _acct_on = _accounts_present(client)        # Phase 4: one grade per account
    _acct_counts = {'upserted': 0, 'touched': 0}
    _guards_stripped = 0

    # Fetch events that already have firmographic data.
    # Oldest-first (same rationale as enrich_events): if a regrade run is
    # interrupted, we've made forward progress on the tail and the next
    # run picks up where we left off.
    query = client.table('events').select(
        'id, company_name, event_type, title, description, '
        'source_url, companies_data, published_date, discovered_at'
    ).not_.is_('companies_data', 'null')
    if event_type:
        query = query.eq('event_type', event_type)
    # Skip tombstoned events (already blocked/dismissed — regrading them is
    # wasted work on rows the dashboard never shows)
    try:
        query = query.is_('blocked_at', 'null')
    except Exception:
        pass
    events = _fetch_all(query, order='discovered_at', desc=False)
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
                             'no finance-leader role',
                             typed=_tombstone_typed(event, typed_cols))
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
                             f'industry: {primary.get("industry")} (matched "{kw}")',
                             typed=_tombstone_typed(event, typed_cols))
            deleted += 1
            continue

        # ── FIT GATES (same policy as enrich_events) ──────────────────────
        # Note: legacy events lack zi_subindustry (added 2026-07-16) — their
        # vertical reads 'unknown', so most legacy events land 'unverified'
        # (kept, capped at B, flagged) rather than 'fail'. Full re-enrichment
        # is what upgrades them to confirmed fit. Records written before the
        # registry stamp existed (H3) get it here from the event itself.
        if _is_iapd_event(event):
            cd = [_with_registry(dict(c), 'sec_iapd') if isinstance(c, dict) else c for c in cd]
        fit = apply_fit_gates(cd)
        if fit['verdict'] == 'fail':
            log.info(f'  🚫 Fit gate FAIL — {"; ".join(fit["reasons"])} '
                     f'→ soft-delete')
            if not dry_run:
                _soft_delete(client, eid, f'fit_gate: {"; ".join(fit["reasons"])}',
                             extra={'companies_data': cd, 'fit': fit},
                             typed=_tombstone_typed(event, typed_cols, fit=fit,
                                                    account=_account_company(cd, fit)))
                if _acct_on:
                    _acct_counts['touched'] += _sync_accounts(
                        client, event, cd, fit, tombstoned=True, is_new_event=False)[1]
            deleted += 1
            continue

        # ── TAL grading (local LLM, free — no search probes in this mode) ──
        grading = grade_event(event, cd, account_name=fit.get('account_name') or '', fit=fit)
        _guards_stripped += strip_count(grading.get('guard_notes'))
        if grading.get('grade') is None:
            # Review 2026-09-08 (Phase 4), 1b: a grading failure used to pop
            # every `tal` chip and then write companies_data anyway, so the
            # account chip lost its grade while the event kept its own.
            # Nothing is written (the pre-Phase-4 behaviour): the event
            # keeps its grade, the chips keep theirs, the next regrade pass
            # redoes this row.
            log.warning('  Grading returned nothing — nothing written (event and account '
                        'chips keep their grade)')
            ok += 1
            continue

        # ONE GRADE PER ACCOUNT (Phase 4, A.J. 2026-09-06 — mirrors
        # enrich_events): the chosen account carries the grade; the
        # per-company secondary grades and the headline promotion are gone,
        # and a stale secondary `tal` from the old loop is dropped here.
        _attach_account_grade(cd, fit.get('account_name') or '', grading)

        if fit['verdict'] == 'unverified' and grading.get('grade') == 'A':
            grading['grade'] = 'B'
            grading['grade_justification'] = (
                '⚠️ Capped A→B: fit unverified '
                f'({"; ".join(r for r in fit["reasons"] if "unverified" in r)}). '
                + (grading.get('grade_justification') or '')
            )[:1000]

        log.info(
            f'  Grade={grading["grade"]}  '
            f'Hashtags={" ".join(grading["hashtags"]) or "(none)"}'
        )

        if dry_run:
            ok += 1
            continue

        # ── Build payload. companies_data IS written now — per-company fit
        # and the chosen account's TAL grade were attached to the dicts. ──
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
        # Phase 2 typed mirror of the fit verdict. attempts/retry_after are
        # left alone: a regrade is not a research attempt.
        if typed_cols:
            _acct = _account_company(cd, fit)
            payload.update(typed_payload(
                event=event, fit=fit, structured=_structured_verdict(event),
                account=_acct, verify_state=verify_state_for(fit.get('verdict')),
                present=typed_cols,
                classification_confidence=_acct.get('classification_confidence'),
                classified_by=_acct.get('classified_by')))

        # event_type reclassification — CFO-equivalents only (Controllers
        # stay executive_hire; see _finance_role for why)
        current_etype = (event.get('event_type') or '').lower()
        if current_etype != 'cfo_hire' and _finance_role(event) == 'cfo':
            payload['event_type'] = 'cfo_hire'
            upgraded += 1
            log.info(f'  Reclassifying event_type {current_etype!r} → cfo_hire')

        try:
            client.table('events').update(payload).eq('id', eid).execute()
            ok += 1
            if _acct_on:        # a regrade is never a new event for the account (2c)
                _u, _t = _sync_accounts(client, event, cd, fit, grading=grading,
                                        is_new_event=False)
                _acct_counts['upserted'] += _u
                _acct_counts['touched'] += _t
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
        f'event_type → cfo_hire: {upgraded}, failed: {fail}  ·  '
        f'Accounts upserted:{_acct_counts["upserted"]} touched:{_acct_counts["touched"]}  ·  '
        f'Guards stripped:{_guards_stripped}'
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
    p.add_argument('--complexity-sweep', action='store_true',
                   help='With --re-enrich: only fit-confirmed Grade-B events '
                        'with a high-intent trigger — runs the complexity '
                        'probe so eligible B accounts can reach Grade A')
    p.add_argument('--estimate',      action='store_true',
                   help='Pre-flight only: count eligible events and print the '
                        'projected search/Tavily cost, then exit')
    p.add_argument('--confirm-credits', type=int, default=None,
                   help='Required for bulk --re-enrich runs over 50 events: the '
                        'number of Tavily credits you accept spending')
    p.add_argument('--event-type',    default=None,
                   help='With --regrade-only: only events of this event_type '
                        '(e.g. finance_seat_open after the Adzuna relabel)')
    p.add_argument('--dry-run',       action='store_true',
                   help='Preview without writing to Supabase')
    args = p.parse_args()

    # Single-instance guard (Phase 2): launchd fires every 4h; a slow run
    # overlapping the next one would double-spend the search budget and race
    # on the same rows. The lock is a kernel flock — released on crash too.
    _lock = _acquire_run_lock()
    if _lock is None:
        sys.exit(0)
    try:
        if args.regrade_only:
            if args.re_enrich:
                sys.exit('Choose one: --regrade-only OR --re-enrich (not both)')
            regrade_only_events(limit=args.limit, dry_run=args.dry_run,
                                event_type=args.event_type)
        else:
            enrich_events(
                limit=args.limit,
                re_enrich=args.re_enrich,
                dry_run=args.dry_run,
                missing_fit_only=args.missing_fit_only,
                reverify_unverified=args.reverify_unverified,
                complexity_sweep=args.complexity_sweep,
                estimate_only=args.estimate,
                confirm_credits=args.confirm_credits,
            )
    finally:
        _lock.release()
