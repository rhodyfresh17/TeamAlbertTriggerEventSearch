#!/usr/bin/env python3
"""
monitor_health.py — End-to-end health check for TeamAlbertTriggerEventSearch.

Designed to be run periodically as a launchd cron (com.teamalbert.healthcheck)
to catch issues before A.J. notices them in the dashboard. Scout reads the
resulting logs/health_alerts.log and summarises it Mondays.

Usage:
    python monitor_health.py            # default = --quick (~10s)
    python monitor_health.py --quick    # essential checks only
    python monitor_health.py --daily    # adds source health + yield checks
    python monitor_health.py --weekly   # daily set + Monday-only checks: finance-leader source
                                        #   mix + share of intake, vertical mix (Phase 3 2026-09-08);
                                        #   the all-clear heartbeat is in the wrapper
    python monitor_health.py --json     # machine-readable output
    python monitor_health.py --weekly --no-state
                                        # manual / diagnostic run: READ the state files,
                                        #   never write them (see "Manual runs" below)

Exit codes:
    0  — all checks PASS, or any WARN
    1  — at least one FAIL (cron/CI can detect)

Cost rule (2026-09-06): this script never spends a paid credit. Tavily is
judged from the local `tavily_usage` counter, never by calling the API.
Firecrawl is judged by a single free local search (the "usefulness canary").

Yield rule (2026-09-07): the daily checks measure YIELD (did anything survive
the gates, per source) — not just liveness (did the cron run). Liveness said
"All clear" for 40 days while 24 of 39 feeds returned nothing and the best
trigger (new finance leader) was 60% single-sourced to a feed that had been
dead for 9 days. Every WARN here is posted to Mattermost by
run_health_check.sh, so a check must not WARN for a condition that is
expected every day — that is noise the owner learns to ignore.

Manual runs (review 2026-09-08): the weekly checks compare against what the
LAST run wrote under state/ — the Monday baseline. A --weekly run by hand
would overwrite that baseline (and the daily state: search_mode, the
rep-state count, the quiet-feed memory), so any manual or diagnostic run
passes --no-state: every check still READS its state file, so the memory
still shapes the verdicts, but nothing under state/ is written.

State files (state/, gitignored — created on first run):
    state/search_mode              'ok' | 'defer' — enrichment_scout.py reads this;
                                   'defer' after 2 consecutive empty Firecrawl canaries
    state/firecrawl_empty_streak   consecutive empty canaries (int)
    state/lead_status_nonnew.txt   previous run's count of rep-set lead_status rows
    state/quiet_sources.json       {label: {"since": "YYYY-MM-DD", "prior": n}} — feeds that
                                   went quiet; kept until they recover (check_source_yield)
    state/finance_leader_mix.json  {top_source, share_pct, checked_at} from the last weekly
                                   run (check_trigger_source_concentration)
    state/vertical_mix.json        {checked_at, total, prior_total, mix: {vertical: {n, pct}},
                                   dark: {vertical: {since, prior}}} from the last weekly run;
                                   `dark` lists verticals that went dark, kept until they
                                   recover (check_vertical_mix, Phase 3 2026-09-08 / review 2026-09-08)
    state/finance_leader_share.json {share_pct, family_n, survivors_n, checked_at,
                                   last_on_target: {pct, checked_at}} from the last weekly run;
                                   `last_on_target` is the high-water mark — the most recent
                                   run at/above the target (check_finance_leader_share)
"""

import os
import sys
import json
import argparse
import calendar
import sqlite3
import re
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.pipeline.sources import (  # noqa: E402
    source_label, feed_label, feed_matches_label, finance_leader_family,
    is_canonical_label,
)
# Phase 3 (2026-09-08): the FY27 vertical taxonomy is defined once, in the
# enrichment engine; import it rather than copy it. Guarded so a broken
# engine module degrades check_vertical_mix to a WARN that names the cause
# instead of taking every other check down with it.
try:
    from enrichment_scout import ZI_SUBINDUSTRIES, NONPROFIT_VERTICAL  # noqa: E402
except Exception:  # noqa: BLE001 — any import failure, not only ImportError
    ZI_SUBINDUSTRIES, NONPROFIT_VERTICAL = {}, 'Nonprofits & Organizations'

# Load .env
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


# ── Status constants ─────────────────────────────────────────────────────────
PASS = '🟢 PASS'
WARN = '🟡 WARN'
FAIL = '🔴 FAIL'

# Symbols stripped for --json mode
PLAIN = {PASS: 'pass', WARN: 'warn', FAIL: 'fail'}

PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / 'trigger_events.db'   # same file enrichment_scout.py's CACHE_DB_PATH defaults to
STATE_DIR = PROJECT_DIR / 'state'
# --no-state (review 2026-09-08): True turns every _state_write into a no-op
# so a manual run can never move the baselines the cron compares against.
# Set from main(); tests set it directly.
STATE_READ_ONLY = False


# ── Tiny state store (plain text files, one value each) ──────────────────────

def _state_read(name: str):
    """Return the stripped contents of state/<name>, or None if absent/unreadable."""
    try:
        return (STATE_DIR / name).read_text().strip()
    except (FileNotFoundError, OSError):
        return None


def _state_write(name: str, value) -> None:
    if STATE_READ_ONLY:         # --no-state: read the memory, never rewrite it
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / name).write_text(f'{value}\n')


def _state_int(name: str, default: int = 0) -> int:
    raw = _state_read(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _state_json(name: str):
    """Parsed JSON from state/<name>, or None when the file is absent or does
    not parse. A hand-edited file that no longer parses counts as "no memory"
    rather than crashing the check (the owner is told to edit these files)."""
    raw = _state_read(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _state_write_json(name: str, obj) -> None:
    """Pretty, key-sorted so a human can read and edit the file by hand."""
    _state_write(name, json.dumps(obj, indent=2, sort_keys=True))


# ── Individual checks (each returns (status, message)) ───────────────────────

def check_env_creds():
    """Verify required env vars are set (without revealing values)."""
    # Firecrawl (local, free) is the PRIMARY search backend — it has no API
    # key (it's a local container), so the only hard requirement is Supabase.
    # TAVILY_API_KEY is OPTIONAL — it's just the ~3% fallback when Firecrawl
    # returns empty. Adzuna keys are optional (job-board source).
    required = ['SUPABASE_URL']
    optional = ['SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_KEY',
                'TAVILY_API_KEY', 'ADZUNA_APP_ID', 'ADZUNA_APP_KEY']
    missing_req = [k for k in required if not os.environ.get(k)]
    if missing_req:
        return FAIL, f'Missing required env vars: {", ".join(missing_req)}'
    # Need at least one Supabase key
    if not (os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
            or os.environ.get('SUPABASE_KEY')):
        return FAIL, 'Need either SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY'
    missing_opt = [k for k in optional if not os.environ.get(k)]
    if missing_opt:
        return WARN, f'Optional env vars unset: {", ".join(missing_opt)}'
    return PASS, 'All env vars set'


FIRECRAWL_CANARY_QUERY = 'Bank of America headquarters'
FIRECRAWL_DEFER_AFTER = 2   # consecutive empty canaries before state/search_mode = defer


def check_firecrawl_canary():
    """USEFULNESS canary for the PRIMARY search backend (local Firecrawl).

    A liveness ping (GET /) said "up" while Firecrawl's search — DuckDuckGo
    from the Mac's single home IP — was returning empty ~70% of the time, and
    every empty answer escalated an event to paid Tavily. So we ask it a
    question any working search engine answers: {"query": "Bank of America
    headquarters", "limit": 3}. One free local call, no paid credit.

    PASS  ≥1 result → state/search_mode = ok, empty streak reset.
    WARN  0 results → empty streak +1; after FIRECRAWL_DEFER_AFTER in a row,
          state/search_mode = defer (enrichment_scout.py reads this and holds
          off paid escalation until a canary succeeds again).
    FAIL  unreachable → search backend DOWN; streak/mode left unchanged
          (down ≠ throttled — enrichment already refuses to run without it).
    """
    url = os.environ.get('FIRECRAWL_URL', 'http://localhost:3002')
    try:
        resp = requests.post(
            f'{url}/v1/search',
            json={'query': FIRECRAWL_CANARY_QUERY, 'limit': 3},
            timeout=25,
        )
    except requests.ConnectionError:
        return FAIL, (
            f'Firecrawl not reachable at {url} — enrichment search backend '
            f'is DOWN. Start it: docker compose up -d firecrawl-api-1 '
            f'(state/search_mode unchanged)'
        )
    except requests.Timeout:
        return WARN, f'Firecrawl canary timed out (>25s) at {url} — could not judge search usefulness'
    except Exception as e:
        return WARN, f'Firecrawl canary error: {e}'

    n_results, note = 0, ''
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if body.get('success', True):
            n_results = len(body.get('data') or [])
        else:
            note = f' (success=false: {str(body.get("error", ""))[:60]})'
    else:
        note = f' (HTTP {resp.status_code})'

    if n_results >= 1:
        _state_write('search_mode', 'ok')
        _state_write('firecrawl_empty_streak', 0)
        return PASS, (
            f'Firecrawl search useful — {n_results} result(s) for '
            f'"{FIRECRAWL_CANARY_QUERY}"; state/search_mode=ok'
        )

    streak = _state_int('firecrawl_empty_streak') + 1
    _state_write('firecrawl_empty_streak', streak)
    if streak >= FIRECRAWL_DEFER_AFTER:
        _state_write('search_mode', 'defer')
        mode_note = ('state/search_mode=defer → enrichment holds off paid Tavily '
                     'escalation until a canary succeeds')
    else:
        mode = _state_read('search_mode') or 'ok'
        _state_write('search_mode', mode)   # make sure the file exists for enrichment_scout
        mode_note = f'state/search_mode={mode} (unchanged)'
    return WARN, (
        f'Firecrawl search returned 0 results for "{FIRECRAWL_CANARY_QUERY}"{note} '
        f'— {streak} consecutive empty canar{"y" if streak == 1 else "ies"} '
        f'(IP throttling likely); {mode_note}'
    )


TAVILY_WARN_PCT = 60
TAVILY_FAIL_PCT = 85
TAVILY_PROJECTION_MIN_DAYS = 3   # a run-rate projection on day 1-2 is noise, not signal


def check_tavily_budget():
    """Tavily spend vs monthly budget, read from the LOCAL counter — never a
    live API call. The old probe spent one paid credit every day just to ask
    "are you there?", and reported PASS on HTTP 429 (quota gone), which is
    exactly the condition this check exists to catch.

    Source: `tavily_usage` in trigger_events.db, written by
    enrichment_scout._tavily_month_count() on every paid call (month key is
    UTC, same as SQLite's strftime('now')). Budget: TAVILY_MONTHLY_BUDGET env
    (default 900 — same constant enrichment_scout uses to stop escalating).

    PASS  < 60% of budget
    WARN  60-85%, or run-rate projection over budget in the first 2 days
    FAIL  ≥ 85%, or linear projection (used / days elapsed × days in month)
          exceeds the budget from day 3 onward
    """
    try:
        budget = int(os.environ.get('TAVILY_MONTHLY_BUDGET', '900') or 900)
    except ValueError:
        budget = 900
    if budget <= 0:
        return WARN, f'TAVILY_MONTHLY_BUDGET={budget!r} is not a usable budget'
    if not DB_PATH.exists():
        return WARN, 'trigger_events.db not present — cannot read the tavily_usage counter'
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT calls FROM tavily_usage WHERE month = strftime('%Y-%m','now')"
            ).fetchone()
        used = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError as e:
        return WARN, f'Could not read tavily_usage counter ({e}) — spend unverified'

    now = datetime.now(timezone.utc)
    days_elapsed = now.day
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    projected = round(used / days_elapsed * days_in_month)
    pct = used / budget * 100
    msg = (f'Tavily {used}/{budget} used ({pct:.0f}%) · day {days_elapsed}/{days_in_month} '
           f'· projected {projected}/month')

    if pct >= TAVILY_FAIL_PCT:
        return FAIL, msg + (f' — ≥{TAVILY_FAIL_PCT}% of budget; enrichment stops escalating '
                            f'at the cap, events stay unenriched')
    if projected > budget:
        if days_elapsed >= TAVILY_PROJECTION_MIN_DAYS:
            return FAIL, msg + ' — run-rate will blow the monthly budget; find what is escalating'
        return WARN, msg + ' — run-rate over budget, but too early in the month to call'
    if pct >= TAVILY_WARN_PCT:
        return WARN, msg + f' — past {TAVILY_WARN_PCT}% of budget'
    return PASS, msg


def check_llamacpp():
    """Verify the shared local llama.cpp server (Qwen3.6, serves the whole fleet) is up."""
    url = os.environ.get('LLAMACPP_URL', 'http://localhost:8091')
    model = os.environ.get('LLAMACPP_MODEL', 'qwen3.6')
    try:
        resp = requests.get(f'{url}/v1/models', timeout=10)
        if resp.status_code != 200:
            return FAIL, f'llama.cpp returned HTTP {resp.status_code}'
        ids = [m.get('id') for m in resp.json().get('data', [])]
        if model not in ids:
            return WARN, f'llama.cpp up but model "{model}" not listed. Available: {ids[:3]}'
        return PASS, f'llama.cpp up, model "{model}" serving'
    except requests.ConnectionError:
        return FAIL, f'llama.cpp not reachable at {url} — is the llamacpp-hermes launchd service running?'
    except Exception as e:
        return WARN, f'llama.cpp check error: {e}'


def get_supabase():
    if not SUPABASE_AVAILABLE:
        return None
    url = os.environ.get('SUPABASE_URL')
    key = (os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
           or os.environ.get('SUPABASE_KEY'))
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def check_supabase():
    """Verify Supabase is reachable and the events table responds."""
    client = get_supabase()
    if not client:
        return FAIL, 'Cannot construct Supabase client (missing creds or SDK)'
    try:
        result = client.table('events').select('id', count='exact').limit(1).execute()
        return PASS, f'Supabase reachable, {result.count} events in DB'
    except Exception as e:
        return FAIL, f'Supabase query failed: {e}'


def check_scrape_freshness():
    """Is the scraper PIPELINE alive? Measured by when the cron last RAN,
    not when it last found an event.

    The GitHub Actions cron fires every 4h regardless of day. The right signal
    for "is the pipeline broken" is source_status.last_check (updated on every
    run, even when 0 events are found) — NOT events.discovered_at (which only
    moves when something new is found).

    These two diverge every weekend: the cron keeps running, but SEC EDGAR is
    closed and press wires are quiet, so no new events appear for 30-50h. The
    old version measured discovered_at and needed fragile day-of-week thresholds
    to avoid false weekend alarms. This version measures the cron itself, so
    it's day-of-week independent and doesn't false-fire on quiet weekends.

    Event-discovery age is reported as a secondary, informational note only —
    a long drought is surfaced for awareness but never alarms (covered properly
    by check_event_volume_trend + check_source_health)."""
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    now = datetime.now(timezone.utc)

    def _age_hours(ts: str):
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 3600

    try:
        # PRIMARY: when did the cron last run? (source_status.last_check)
        ss = client.table('source_status').select('last_check').order(
            'last_check', desc=True).limit(1).execute()
        cron_age = _age_hours(ss.data[0]['last_check']) if ss.data else None

        # SECONDARY (informational): when was the last new event discovered?
        ev = client.table('events').select('discovered_at').order(
            'discovered_at', desc=True).limit(1).execute()
        event_age = _age_hours(ev.data[0]['discovered_at']) if ev.data else None

        # Build the secondary note about event-discovery age
        if event_age is None:
            event_note = 'no events in DB'
        elif event_age < 24:
            event_note = f'last new event {event_age:.1f}h ago'
        else:
            event_note = (
                f'last new event {event_age:.0f}h ago '
                f'(normal on weekends — sources quiet)'
            )

        # The cron is the alarm signal. It runs every 4h.
        if cron_age is None:
            return WARN, (
                'No source_status rows — cannot confirm cron ran. '
                'Has the scraper run at least once? '
                f'({event_note})'
            )
        if cron_age > 10:
            return FAIL, (
                f'Scraper cron last ran {cron_age:.1f}h ago (expected every 4h) '
                f'— GitHub Actions likely broken. Check the Actions tab. '
                f'({event_note})'
            )
        if cron_age > 6:
            return WARN, (
                f'Scraper cron last ran {cron_age:.1f}h ago — may have missed '
                f'a cycle (expected every 4h). ({event_note})'
            )
        return PASS, (
            f'Cron healthy — last ran {cron_age:.1f}h ago; {event_note}'
        )
    except Exception as e:
        return WARN, f'Could not check scrape freshness: {e}'


def check_enrichment_lag():
    """Are events sitting unenriched too long?"""
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    try:
        # Events that have no enriched_at AND are >5h old → enrichment is lagging.
        # Exclude soft-deleted (blocked_at IS NOT NULL) events — those are
        # intentionally left in the table as tombstones to prevent supabase_sync
        # from re-creating deleted rows, but they don't represent enrichment work
        # to be done. Filter only applies if blocked_at column exists.
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        q = client.table('events').select(
            'id', count='exact'
        ).is_('enriched_at', 'null').lt('discovered_at', cutoff)
        try:
            q = q.is_('blocked_at', 'null')
        except Exception:
            pass  # column not yet present (pre-migration)
        result = q.execute()
        stale = result.count or 0
        if stale > 10:
            return FAIL, f'{stale} events unenriched after 5+ hours — launchd cron may be broken'
        if stale > 3:
            return WARN, f'{stale} events unenriched after 5+ hours'
        return PASS, f'Enrichment current ({stale} stale events)'
    except Exception as e:
        return WARN, f'Could not check enrichment lag: {e}'


REP_STATE_DROP_FAIL_PCT = 20
REP_STATE_FILE = 'lead_status_nonnew.txt'


def check_rep_state_intact():
    """Sanity check that rep work is not being wiped.

    Counts events whose lead_status a rep has set (anything but 'NEW'; NULL
    counts as untouched) and compares with the count stored from the previous
    run in state/lead_status_nonnew.txt. Reps only ever ADD statuses, so a
    drop of more than REP_STATE_DROP_FAIL_PCT% means something is overwriting
    them — the class of bug supabase_sync.py had until 2026-09-06 (an
    unpaginated prefetch reset every row past 1,000 to NEW each cycle; only 20
    of 2,774 survived).

    On FAIL the baseline is deliberately NOT overwritten, so the alarm keeps
    firing until the count recovers or a human resets the file.
    """
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    try:
        r = client.table('events').select('id', count='exact').neq(
            'lead_status', 'NEW').limit(1).execute()
        now_n = r.count or 0
    except Exception as e:
        return WARN, f'Could not count rep-set lead_status rows: {e}'

    prev_raw = _state_read(REP_STATE_FILE)
    prev = int(prev_raw) if prev_raw is not None and prev_raw.isdigit() else None
    if prev is None:
        _state_write(REP_STATE_FILE, now_n)
        return PASS, f'{now_n} events carry a rep-set lead_status — baseline recorded (first run)'

    if prev > 0 and now_n < prev * (1 - REP_STATE_DROP_FAIL_PCT / 100):
        drop = (prev - now_n) / prev * 100
        return FAIL, (
            f'Rep-set lead_status count DROPPED {prev} → {now_n} ({drop:.0f}%) since last run '
            f'— something is overwriting rep work (check supabase_sync.py payload + any '
            f'bulk script). Baseline kept; once explained, reset with '
            f'`echo {now_n} > state/{REP_STATE_FILE}`'
        )
    _state_write(REP_STATE_FILE, now_n)
    return PASS, f'{now_n} events carry a rep-set lead_status (previous run: {prev})'


# The LIVE enrichment cache is AccountCache (src/pipeline/cache.py, 2026-09-07):
# search_cache / account_firmographics / negative_cache, plus the Tavily month
# counter. The legacy `firmographic_cache` table is no longer created or read
# (enrichment_scout keeps _cache_get/_cache_set for rollback only), so
# requiring it here meant a recreated trigger_events.db would WARN forever
# (review 2026-09-07). It is mentioned only if it happens to still exist.
ENRICHMENT_TABLES = ('search_cache', 'account_firmographics', 'negative_cache', 'tavily_usage')
LEGACY_CACHE_TABLE = 'firmographic_cache'


def check_local_sqlite(db_path=None):
    """The local trigger_events.db is the ENRICHMENT cache + Tavily counters,
    nothing more. The scrape DB (dedup history, the rows supabase_sync pushes)
    lives in the GitHub Actions cache (trigger-events-db-v2-*) by design, so
    the local events table is empty on this Mac every single day.

    Until 2026-09-07 this check WARNed on that empty table, which posted a
    junk alert to Mattermost daily. Now: PASS when the file and the enrichment
    tables are present; WARN only when the file or those tables are missing
    (then check_tavily_budget is blind and the cache is gone). Never WARN
    for 0 events."""
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return WARN, (
            f'{path.name} not present — enrichment cache + Tavily counter missing '
            f'(enrichment_scout.py recreates it on its next run; the Tavily budget '
            f'check is blind until then)'
        )
    try:
        with sqlite3.connect(str(path)) as conn:
            present = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in ENRICHMENT_TABLES if t not in present]
            if missing:
                return WARN, (
                    f'{path.name} is missing enrichment table(s): {", ".join(missing)} '
                    f'— has enrichment_scout.py run on this machine since the '
                    f'AccountCache change (2026-09-07)?'
                )
            n = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                 for t in ENRICHMENT_TABLES}
            n_legacy = (conn.execute(f'SELECT COUNT(*) FROM {LEGACY_CACHE_TABLE}').fetchone()[0]
                        if LEGACY_CACHE_TABLE in present else None)
    except sqlite3.OperationalError as e:
        return WARN, f'SQLite schema issue: {e}'
    except Exception as e:
        return WARN, f'SQLite check failed: {e}'
    legacy = (f'; legacy {LEGACY_CACHE_TABLE} still present with {n_legacy} rows, no longer read'
              if n_legacy is not None else '')
    return PASS, (
        f'Enrichment cache OK ({n["search_cache"]} cached searches · '
        f'{n["account_firmographics"]} account profiles · {n["negative_cache"]} negative-cached · '
        f'{n["tavily_usage"]} Tavily counter rows{legacy}) '
        f'— scrape DB lives in the GitHub Actions cache by design'
    )


TRANSPORT_ABORT_WARN_AT = 2
TRANSPORT_ABORT_FILE = 'enrichment_transport_aborts'


def check_enrichment_transport():
    """Consecutive enrichment cycles that ended at the schema probe because
    Supabase could not be reached (enrichment_scout exits 2 and counts them
    in state/enrichment_transport_aborts; a normal run resets the count).
    One is a blip that already retried; two in a row is an outage worth a
    human (2026-09-11)."""
    n = _state_int(TRANSPORT_ABORT_FILE, 0)
    if n >= TRANSPORT_ABORT_WARN_AT:
        return WARN, (f'Enrichment could not reach Supabase for {n} consecutive '
                      f'cycles (~{4 * n}h) — probe timeouts, not a missing column. '
                      f'Check Supabase status / the Mac\'s network; the job keeps retrying every 4h.')
    if n == 1:
        return PASS, 'One enrichment cycle skipped on a Supabase timeout — it retries next cycle'
    return PASS, 'Enrichment reached Supabase on its last run'


def check_launchd_job():
    """Verify the enrichment launchd job is loaded (Mac-only)."""
    if sys.platform != 'darwin':
        return WARN, 'Not on macOS — launchd check skipped'
    import subprocess
    try:
        r = subprocess.run(
            ['launchctl', 'list'],
            capture_output=True, text=True, timeout=5
        )
        if 'com.teamalbert.enrichment' in r.stdout:
            return PASS, 'launchd enrichment job loaded'
        return FAIL, 'launchd enrichment job NOT loaded — re-load with `launchctl load ~/Library/LaunchAgents/com.teamalbert.enrichment.plist`'
    except FileNotFoundError:
        return WARN, 'launchctl not available'
    except Exception as e:
        return WARN, f'launchd check failed: {e}'


# ── --daily checks ──────────────────────────────────────────────────────────

def check_source_health():
    """How many sources are productive vs silent?"""
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    try:
        ss = client.table('source_status').select('*').execute()
        if not ss.data:
            return WARN, 'No source_status rows — has scraper ever run?'
        productive = [s for s in ss.data if (s.get('events_found') or 0) > 0]
        silent = [s for s in ss.data if (s.get('events_found') or 0) == 0
                  and s.get('status') == 'success']
        errored = [s for s in ss.data if s.get('status') == 'error']
        msg = f'{len(productive)} producing · {len(silent)} silent · {len(errored)} errored (of {len(ss.data)} total)'
        if len(errored) > 5:
            return FAIL, msg + ' — too many errored sources'
        if len(productive) < 2:
            return WARN, msg + ' — very few productive sources'
        return PASS, msg
    except Exception as e:
        return WARN, f'Could not check source health: {e}'


def check_event_volume_trend():
    """Compare last 7 days to prior 7 days."""
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    try:
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        prior_cutoff  = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        recent = client.table('events').select('id', count='exact').gte(
            'discovered_at', recent_cutoff).execute()
        prior = client.table('events').select('id', count='exact').gte(
            'discovered_at', prior_cutoff).lt('discovered_at', recent_cutoff).execute()
        r_n = recent.count or 0
        p_n = prior.count or 0
        if p_n == 0 and r_n == 0:
            return WARN, 'No events in last 14 days at all'
        change = ((r_n - p_n) / max(p_n, 1)) * 100 if p_n else 100
        msg = f'last 7d: {r_n} · prior 7d: {p_n} · change: {change:+.0f}%'
        if r_n < 10:
            return WARN, msg + ' — low volume'
        if change < -50:
            return WARN, msg + ' — volume dropped sharply'
        return PASS, msg
    except Exception as e:
        return WARN, f'Could not compute trend: {e}'


# ── Yield checks (daily + weekly, 2026-09-07) ───────────────────────────────
#
# "Survivor" = an events row with blocked_at NULL: it got past every scrape-
# time and enrichment gate and is (or was) something a rep could see. Counting
# survivors PER SOURCE over a recent window vs a prior window is what tells a
# dead feed from a quiet weekend — liveness (source_status.last_check) cannot.

YIELD_WINDOW_DAYS = 28        # total look-back for every yield check
YIELD_RECENT_DAYS = 7         # "now" window; the remaining 21d is the "before" baseline
# "Went quiet" bar (review 2026-09-07). Survivors arrive roughly Poisson, so a
# feed averaging P survivors/21d expects P/3 in a 7-day window and reads empty
# with probability e^-(P/3). The old bar of 3 (λ = 1/7d → P(0) = e^-1 ≈ 37%;
# a 4/21d feed ≈ 26%) flapped on every thin feed. At 12/21d the feed expects
# 4/7d and P(0) = e^-4 ≈ 1.8% — an empty week is evidence, not luck.
QUIET_MIN_PRIOR = 12
# Below the WARN bar a quiet bucket is LISTED as "small feeds quiet" (context
# in the PASS/WARN text, never an alert of its own) once its prior count
# reaches this: bare-host buckets (a URL host that never got a canonical
# label) at any size, and canonical feeds under QUIET_MIN_PRIOR.
SMALL_FEED_MIN_PRIOR = 3
QUIET_STATE_FILE = 'quiet_sources.json'   # {label: {"since": "YYYY-MM-DD", "prior": n}}
CRON_RAN_MAX_HOURS = 10       # same threshold check_scrape_freshness FAILs at
FEED_ACTIVE_HOURS = 48        # source_status rows older than this are retired feeds, not "the latest run"
CONCENTRATION_WARN_PCT = 40   # one source carrying more than this share of the best trigger = single point of failure
CONCENTRATION_MIN_ROWS = 5    # below this a share is arithmetic noise, not a signal
# Change-driven (review 2026-09-07): with Adzuna the only live finance-leader
# feed, the top share sat above CONCENTRATION_WARN_PCT in every reachable
# state (Adzuna 67% on 2026-09-07), so a WARN every run was noise. The
# review's other "reachable state" — Google News at 70% once Adzuna ages out
# — never was one: the Google News scraper had produced next to nothing
# since 2026-02-05 (2 news.google.com rows in seven months — a scraper bug,
# fixed Phase 3 2026-09-08); the 'Google News' rows in the window are the
# three Google Alerts RSS feeds (google.com/url redirects) carrying the same
# typed source and label. Phase 3 also adds press-release personnel feeds,
# regional business journals and the sec_iapd source, so the mix WILL move
# over the coming weeks — the change-driven rule is what makes each shift
# alert once instead of every Monday. WARN only when the top source flips,
# its share moves more than this many points since the previous weekly run,
# it newly crosses the bar, or on the first run ever; otherwise PASS with
# the mix.
CONCENTRATION_SHIFT_PTS = 15
MIX_STATE_FILE = 'finance_leader_mix.json'   # {top_source, share_pct, checked_at}
MIX_SHOW_MAX = 6              # sources listed in the "mix unchanged" line before "+n more"
MAX_ENRICH_ATTEMPTS = 3       # after this a researched_ambiguous row is negative-cached (contract, 2026-09-07)
RETRY_STATE = 'researched_ambiguous'
TYPED_COLUMNS_NOTE = '(items_fetched not yet migrated — run supabase/migrations/002_v2_typed_columns.sql)'
EVENT_COLUMNS = 'discovered_at,blocked_at,blocked_reason,source_url,title,event_type,hashtags'


def _parse_ts(ts):
    """Aware UTC datetime from a Supabase timestamp string, or None. Postgres
    emits either naive ISO or +00:00; fractional seconds can exceed the six
    digits fromisoformat accepts on 3.9."""
    if not ts:
        return None
    try:
        s = str(ts).replace('Z', '+00:00')
        s = re.sub(r'(\.\d{6})\d+', r'\1', s)
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fetch_recent_events(days=YIELD_WINDOW_DAYS, client=None):
    """Every events row discovered in the last `days`, paginated (PostgREST
    caps a page at 1,000 — the unpaginated read is the class of bug that reset
    rep statuses in supabase_sync until 2026-09-06). Asks for the typed
    `source` column first and falls back to the URL-only select when the
    migration hasn't run. Module-level so tests can monkeypatch it."""
    client = client or get_supabase()
    if not client:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _page_all(columns):
        rows, off = [], 0
        while True:
            # ORDER BY (discovered_at, id) — a total order (review 2026-09-07).
            # Without it Postgres returns pages in physical order, which the
            # enrichment UPDATEs running concurrently can reshuffle between
            # requests, so pages overlapped or skipped rows.
            q = (client.table('events').select(columns).gte('discovered_at', since)
                 .order('discovered_at').order('id').range(off, off + 999).execute())
            page = q.data or []
            rows += page
            if len(page) < 1000:
                return rows
            off += 1000

    try:
        return _page_all(EVENT_COLUMNS + ',source')
    except Exception:
        return _page_all(EVENT_COLUMNS)   # pre-migration: no `source` column


def _fetch_source_status(client=None):
    """All source_status rows (one per feed). Module-level for tests."""
    client = client or get_supabase()
    if not client:
        return None
    return client.table('source_status').select('*').execute().data or []


def _cron_age_hours(status_rows, now):
    """Hours since the scraper cron last touched ANY feed, or None."""
    ages = [(now - dt).total_seconds() / 3600
            for dt in (_parse_ts(r.get('last_check')) for r in (status_rows or []))
            if dt is not None]
    return min(ages) if ages else None


def _survivors(rows):
    return [r for r in rows if not r.get('blocked_at')]


def _yield_by_source(rows, now):
    """{label: {'recent': n, 'prior': n, 'last': datetime|None}} over survivors
    in the yield window; 'last' is the newest survivor (a feed's "quiet since")."""
    recent_cut = now - timedelta(days=YIELD_RECENT_DAYS)
    out = {}
    for r in _survivors(rows):
        dt = _parse_ts(r.get('discovered_at'))
        if dt is None:
            continue
        bucket = out.setdefault(source_label(r), {'recent': 0, 'prior': 0, 'last': None})
        bucket['recent' if dt >= recent_cut else 'prior'] += 1
        if bucket['last'] is None or dt > bucket['last']:
            bucket['last'] = dt
    return out


def _quiet_entry(entry):
    """(since, prior) from a state/quiet_sources.json entry — tolerant of hand
    edits: anything unreadable shows as '?' rather than crashing the check."""
    if not isinstance(entry, dict):
        return '?', '?'
    since, prior = entry.get('since'), entry.get('prior')
    return (str(since) if since else '?'), (prior if prior is not None else '?')


def _fmt_top(counter_like, n=3, suffix=''):
    top = sorted(counter_like, key=lambda kv: (-kv[1], kv[0]))[:n]
    return ', '.join(f'{k} {v}{suffix}' for k, v in top)


def check_source_yield(now=None):
    """Per source: survivors in the last 7d vs the prior 21d.

    WARN  a canonical source with ≥ QUIET_MIN_PRIOR survivors in the baseline
          and 0 in the last 7d ("went quiet" — a disabled or silently dead
          feed), plus every label already on record in
          state/quiet_sources.json that still shows 0 recent survivors
          ("still quiet since <date>"). The record is what keeps a dead feed
          visible: after 28 days its baseline drops to 0 and without memory
          the check would PASS while the feed stayed dead — Adzuna, dead
          since 2026-08-26, would have vanished from every check around
          2026-09-23 (review 2026-09-07). An entry clears itself the day its
          label shows ≥ 1 survivor in the recent window ("recovered").
          Bare-host buckets (no canonical label) never raise this WARN on
          their own; they — and canonical feeds under QUIET_MIN_PRIOR — are
          listed as "small feeds quiet" for context once they have
          ≥ SMALL_FEED_MIN_PRIOR prior survivors and 0 recent.
    FAIL  0 survivors in the last 7d across ALL sources while the cron ran —
          the whole pipeline is producing nothing a rep can see.
    PASS  survivors/7d, source count, top three, and the survival rate of
          everything discovered in the window.

    Manual clear: delete a label from state/quiet_sources.json (or the whole
    file) to drop it — e.g. a feed retired on purpose. It is re-added only
    while the feed still shows ≥ QUIET_MIN_PRIOR prior survivors and 0
    recent, so a deliberately retired feed re-arms until its rows age out of
    the 28-day window; clear it after that, or accept the repeat until then.
    """
    now = now or datetime.now(timezone.utc)
    if not get_supabase():
        return WARN, 'Supabase unavailable — cannot measure yield'
    try:
        rows = _fetch_recent_events(YIELD_WINDOW_DAYS)
    except Exception as e:
        return WARN, f'Could not read recent events: {e}'
    if not rows:
        return WARN, f'No events discovered in the last {YIELD_WINDOW_DAYS} days at all'

    by_src = _yield_by_source(rows, now)
    recent_total = sum(b['recent'] for b in by_src.values())
    prior_days = YIELD_WINDOW_DAYS - YIELD_RECENT_DAYS
    if recent_total == 0:
        try:
            cron_age = _cron_age_hours(_fetch_source_status(), now)
        except Exception:
            cron_age = None
        if cron_age is not None and cron_age <= CRON_RAN_MAX_HOURS:
            return FAIL, (
                f'0 events survived the gates in the last {YIELD_RECENT_DAYS} days even '
                f'though the scraper ran {cron_age:.1f}h ago — the feeds are running but '
                f'nothing gets through (prior {prior_days}d: '
                f'{sum(b["prior"] for b in by_src.values())} survivors)'
            )
        return WARN, (
            f'0 events survived the gates in the last {YIELD_RECENT_DAYS} days and the '
            f'scraper cron is not running — see "Scrape freshness"'
        )

    # Quiet-feed memory (review 2026-09-07): labels on record stay WARN until
    # they show a recent survivor; canonical labels that just went quiet are
    # added with the date of their last survivor. The file is rewritten only
    # when something changed, so its mtime means something.
    loaded = _state_json(QUIET_STATE_FILE)
    on_record = dict(loaded) if isinstance(loaded, dict) else {}
    recovered = sorted(k for k in on_record if by_src.get(k, {}).get('recent', 0) >= 1)
    still_quiet = sorted(k for k in on_record if k not in recovered)
    newly_quiet, small_quiet = [], []
    for label, b in sorted(by_src.items()):
        if b['recent'] or label in on_record:
            continue
        if is_canonical_label(label) and b['prior'] >= QUIET_MIN_PRIOR:
            newly_quiet.append(label)
        elif b['prior'] >= SMALL_FEED_MIN_PRIOR:
            small_quiet.append(label)
    if recovered or newly_quiet:
        record = {k: v for k, v in on_record.items() if k not in recovered}
        for label in newly_quiet:
            record[label] = {'since': (by_src[label]['last'] or now).date().isoformat(),
                             'prior': by_src[label]['prior']}
        _state_write_json(QUIET_STATE_FILE, record)

    extras = ''
    if recovered:
        extras += ' · recovered: ' + ', '.join(
            f'{k} (quiet since {_quiet_entry(on_record[k])[0]})' for k in recovered)
    if small_quiet:
        extras += ' · small feeds quiet: ' + ', '.join(
            f'{k} ({by_src[k]["prior"]}/{prior_days}d)' for k in small_quiet)

    if newly_quiet or still_quiet:
        detail = [f'{k} ({by_src[k]["prior"]} in the prior {prior_days}d)' for k in newly_quiet]
        for k in still_quiet:
            since, prior = _quiet_entry(on_record[k])
            detail.append(f'{k} (still quiet since {since}, was {prior}/{prior_days}d)')
        return WARN, (
            f'{len(detail)} source(s) went quiet — used to produce, 0 survivors in the '
            f'last {YIELD_RECENT_DAYS} days: {"; ".join(detail)}. Check the feed before '
            f'the best trigger goes dark{extras}'
        )

    active = {k: b['recent'] for k, b in by_src.items() if b['recent'] > 0}
    rate = len(_survivors(rows)) / len(rows) * 100
    return PASS, (
        f'{recent_total} survivors/{YIELD_RECENT_DAYS}d across {len(active)} sources '
        f'(top: {_fmt_top(active.items())}) · survival rate {rate:.0f}% of {len(rows)} rows'
        f'{extras}'
    )


def check_fetched_vs_filtered(now=None):
    """Was a feed EMPTY, or did the gates drop everything it fetched?

    Needs source_status.items_fetched (typed-column migration). Until then
    this PASSes with a pointer to the migration instead of nagging daily.
    Only feeds the cron touched in the last FEED_ACTIVE_HOURS count as "the
    latest run" — source_status keeps rows for feeds retired months ago.

    Judged per LABEL, not per feed (review 2026-09-07): every Google Alert
    RSS feed folds onto 'Google News', and one alert legitimately fetching 0
    overnight is not a dead source while its siblings fetched. A label is
    dead only when EVERY fresh feed under it fetched 0.

    WARN  a label whose fresh feeds ALL fetched 0 this run but that had
          survivors in the 28d yield window (a formerly-producing source
          returning nothing = dead feed; distinct from "fetched plenty, all
          filtered", which is a gate-tuning question).
    PASS  producing / all-filtered / fetched-0 counts; a feed that fetched 0
          while a sibling under its label fetched is listed as information."""
    now = now or datetime.now(timezone.utc)
    try:
        status_rows = _fetch_source_status()
    except Exception as e:
        return WARN, f'Could not read source_status: {e}'
    if status_rows is None:
        return WARN, 'Supabase unavailable — cannot check'
    if not status_rows:
        return WARN, 'No source_status rows — has the scraper ever run?'
    if not any('items_fetched' in r for r in status_rows):
        return PASS, f'Fetched-vs-filtered not measurable yet {TYPED_COLUMNS_NOTE}'

    active_cut = now - timedelta(hours=FEED_ACTIVE_HOURS)
    latest = [r for r in status_rows
              if (_parse_ts(r.get('last_check')) or datetime.min.replace(tzinfo=timezone.utc)) >= active_cut]
    stale = len(status_rows) - len(latest)

    fetched0, filtered, producing, unknown = [], [], [], 0
    groups = {}               # feed_label → fresh feeds with a measured items_fetched
    for r in latest:
        fetched = r.get('items_fetched')
        if fetched is None:
            unknown += 1          # row written before the column existed
            continue
        kept = r.get('events_found') or 0
        groups.setdefault(feed_label(r.get('source_name'), r.get('source_type')), []).append(r)
        if fetched == 0:
            fetched0.append(r)
        elif kept == 0:
            filtered.append(r)
        else:
            producing.append(r)

    try:
        yielded = {k for k, b in _yield_by_source(_fetch_recent_events(YIELD_WINDOW_DAYS), now).items()
                   if b['recent'] + b['prior'] > 0}
    except Exception:
        yielded = set()

    dead, empty_with_siblings = [], []
    for label, feeds in sorted(groups.items()):
        names = sorted(r.get('source_name') or '?' for r in feeds)
        empties = sorted(r.get('source_name') or '?' for r in feeds if r.get('items_fetched') == 0)
        if not empties:
            continue
        if len(empties) < len(feeds):
            empty_with_siblings += empties     # a sibling fetched: the source is alive
            continue
        if any(feed_matches_label(r.get('source_name'), lab, r.get('source_type'))
               for r in feeds for lab in yielded):
            dead.append(label if names == [label] else f'{label} [{", ".join(names)}]')

    counts = (f'{len(producing)} producing · {len(filtered)} all filtered · '
              f'{len(fetched0)} fetched 0 (of {len(latest)} feeds in the latest run'
              + (f'; {unknown} not yet measured' if unknown else '')
              + (f'; {stale} retired/stale feeds ignored' if stale else '') + ')')
    info = (f' · fetched 0 while a sibling feed under the same label fetched: '
            f'{", ".join(empty_with_siblings)}' if empty_with_siblings else '')
    if dead:
        return WARN, (
            f'{len(dead)} source(s) returned NOTHING this run but produced survivors in the '
            f'last {YIELD_WINDOW_DAYS} days — likely dead, not filtered: {", ".join(dead)}. '
            f'{counts}{info}'
        )
    return PASS, counts + info


def check_trigger_source_concentration(now=None):
    """How much of the best trigger (a company getting or hiring a finance
    leader) rides on ONE source? Adzuna was 60% of it and silently dead for
    9 days before anyone noticed (audit 2026-09-06).

    Weekly only and change-driven (review 2026-09-07): until Phase 3 adds
    independent finance-leader sources the top share sits above
    CONCENTRATION_WARN_PCT in every reachable state, so a daily WARN was
    noise the owner would learn to ignore. The previous weekly mix is kept
    in state/finance_leader_mix.json ({top_source, share_pct, checked_at})
    and rewritten on every measurable run, so a change alerts ONCE.

    WARN  first measurement ever, the top source flipped, its share moved
          more than CONCENTRATION_SHIFT_PTS points since the last weekly
          run, or it newly crossed the bar.
    PASS  mix unchanged (breakdown shown), no source above the bar, or too
          few finance-leader events to judge (state left untouched)."""
    now = now or datetime.now(timezone.utc)
    if not get_supabase():
        return WARN, 'Supabase unavailable — cannot check'
    try:
        rows = _fetch_recent_events(YIELD_WINDOW_DAYS)
    except Exception as e:
        return WARN, f'Could not read recent events: {e}'
    fam = [r for r in _survivors(rows) if finance_leader_family(r)]
    n = len(fam)
    if n < CONCENTRATION_MIN_ROWS:
        return PASS, f'too few finance-leader events to judge ({n})'
    shares = Counter(source_label(r) for r in fam)
    top_src, top_n = shares.most_common(1)[0]
    top_pct = top_n / n * 100
    pct_items = [(k, round(v / n * 100)) for k, v in shares.items()]

    prev = _state_json(MIX_STATE_FILE)
    prev = prev if isinstance(prev, dict) else None
    _state_write_json(MIX_STATE_FILE, {
        'top_source': top_src, 'share_pct': round(top_pct, 1), 'checked_at': now.isoformat(),
    })

    if top_pct <= CONCENTRATION_WARN_PCT:
        return PASS, (
            f'Finance-leader triggers spread across {len(shares)} sources '
            f'(top: {_fmt_top(pct_items, suffix="%")}) — {n} events/{YIELD_WINDOW_DAYS}d, '
            f'none above {CONCENTRATION_WARN_PCT}%'
        )

    if prev is None:
        why = 'first weekly measurement'
    else:
        prev_src = prev.get('top_source')
        try:
            prev_pct = float(prev.get('share_pct'))
        except (TypeError, ValueError):
            prev_pct = None
        prev_when = str(prev.get('checked_at') or '')[:10] or 'the last weekly run'
        prev_pct_s = f'{prev_pct:.0f}%' if prev_pct is not None else '?%'
        if prev_src != top_src:
            why = f'top source flipped from {prev_src or "?"} ({prev_pct_s}) since {prev_when}'
        elif prev_pct is None or abs(top_pct - prev_pct) > CONCENTRATION_SHIFT_PTS:
            why = f'share moved {prev_pct_s} → {top_pct:.0f}% since {prev_when}'
        elif prev_pct <= CONCENTRATION_WARN_PCT:
            why = (f'crossed the {CONCENTRATION_WARN_PCT}% bar '
                   f'({prev_pct_s} → {top_pct:.0f}%) since {prev_when}')
        else:
            why = None
    if why is None:
        ranked = sorted(pct_items, key=lambda kv: (-kv[1], kv[0]))
        mix = ' · '.join(f'{k} {v}%' for k, v in ranked[:MIX_SHOW_MAX])
        if len(ranked) > MIX_SHOW_MAX:
            mix += f' · +{len(ranked) - MIX_SHOW_MAX} more'
        return PASS, (
            f'Finance-leader mix unchanged: {mix} — {n} events/{YIELD_WINDOW_DAYS}d; '
            f'{top_src} still above {CONCENTRATION_WARN_PCT}% (Phase 3 adds independent sources)'
        )
    return WARN, (
        f'Finance-leader triggers: {top_pct:.0f}% come from one source ({top_src}) — '
        f'if it stalls, the best trigger goes dark (Phase 3 adds independent sources). '
        f'{n} events/{YIELD_WINDOW_DAYS}d across {len(shares)} sources; {why}'
    )


# ── Phase 3 supply checks (weekly, 2026-09-08) ──────────────────────────────
#
# Phase 3 adds supply — press-release personnel feeds, regional business
# journals, the fixed Google News scraper, the sec_iapd adviser source, and
# free oracles that verify banks / RIAs / nonprofits without a search. The
# plan's bar: finance-leader triggers ≥ 30% of new intake with no source
# above 40% of them; nonprofits verified without search > 50%; a
# per-vertical mix report every week. The two checks below ARE that weekly
# report: plain language, informational PASS text in the normal case, WARN
# only for a CHANGE the owner should act on (a vertical that went dark, a
# share that collapsed) — every WARN is posted to Mattermost. dashboard.py
# renders the same numbers (Weekly Scorecard → Supply); both sides bucket
# with src.pipeline.sources and the rules below, so they agree.
VERTICAL_WINDOW_DAYS = 28
VERTICAL_DARK_MIN_PRIOR = 5     # ≥ this many verified accounts in the prior 28d and 0 now = "went dark"
VERTICAL_THIN_PCT = 5           # under this share of verified accounts a vertical is "thin"
VERTICAL_WAS_HEALTHY_PCT = 15   # thin WARNs (not just informs) when the last weekly run had it above this
VERTICAL_MIX_STATE_FILE = 'vertical_mix.json'   # {checked_at, total, prior_total, mix: {vertical: {n, pct}}, dark: {vertical: {since, prior}}}
UNKNOWN_VERTICAL = 'Unknown'
# classified_by values that mean "verified without spending a search":
# 'structured' (SEC SIC / Form D fields) and 'oracle' (Phase 3 registries).
# 'article' is free too but it is a model's guess, not a registry hit —
# this number tracks the oracles. 'cache' is out as well (review 2026-09-08):
# enrichment_scout stamps it when the account cache supplies the
# subindustry, i.e. a REPLAY of the classification stored the first time
# the account was researched — usually by a search — so counting it inflated
# the share with every repeat event for a known account. Same set as
# dashboard.NO_SEARCH_CLASSIFIERS (the shared-thresholds test keeps them equal).
NO_SEARCH_CLASSIFIERS = frozenset({'oracle', 'structured'})
NONPROFIT_NO_SEARCH_TARGET_PCT = 50
FINANCE_SHARE_TARGET_PCT = 30   # finance-leader share of survivors the plan aims for
FINANCE_SHARE_WARN_PCT = 15     # WARN only under this while the high-water mark is fresh
# High-water mark (review 2026-09-08): the WARN used to need the IMMEDIATELY
# previous run at/above the target, so a two-week slide 32% → 27% → 11% never
# warned — 27% was the only baseline 11% was compared with. The most recent
# run at/above FINANCE_SHARE_TARGET_PCT is kept in the state file as
# last_on_target and arms the WARN for this many days (6 weeks).
FINANCE_SHARE_MARK_MAX_AGE_DAYS = 42
FINANCE_SHARE_STATE_FILE = 'finance_leader_share.json'   # {share_pct, family_n, survivors_n, checked_at, last_on_target: {pct, checked_at}}
# Verified = verify_state 'verified', or a NULL state with fit.verdict pass
# (an enricher whose column probe was stale — review 2026-09-07). The same
# PostgREST expression as dashboard.VERIFIED_FILTER, validated read-only on
# the live project 2026-09-07; a literal here because the cron must not
# import the Streamlit app.
VERIFIED_FILTER = 'verify_state.eq.verified,and(verify_state.is.null,fit->>verdict.eq.pass)'
VERIFIED_COLUMNS = 'id,discovered_at,blocked_at,account_key,verify_state,zi_subindustry,classified_by'


def vertical_of(zi) -> str:
    """ZoomInfo subindustry → NSCorp vertical; anything outside the FY27
    taxonomy (None, 'OTHER', legacy free text) → 'Unknown'."""
    if not zi:
        return UNKNOWN_VERTICAL
    return ZI_SUBINDUSTRIES.get(str(zi).strip(), UNKNOWN_VERTICAL)


class ProbeFailed(RuntimeError):
    """The typed-column probe failed for a reason OTHER than the column being
    absent (network, 5xx, auth): a transient the check must report, not mask
    as "not measurable yet (typed columns not migrated)" (review 2026-09-08)."""


def _typed_columns_present(client) -> bool:
    """One cheap select of events.verify_state. False when Postgres says the
    column is absent — SQLSTATE 42703 in the error text, or PostgREST's
    "… does not exist" message that carries it. Anything else raises
    ProbeFailed with a short error so the caller can WARN."""
    try:
        client.table('events').select('verify_state').limit(1).execute()
        return True
    except Exception as e:      # noqa: BLE001 — classified below
        text = str(e)
        if '42703' in text or 'does not exist' in text:
            return False
        raise ProbeFailed(' '.join(text.split())[:160] or type(e).__name__) from e


def _fetch_verified_accounts(days, client=None):
    """Verified rows discovered in the last `days` — filtered server-side
    (≈ 90 rows for 56 days instead of ≈ 2,300), paginated and ordered like
    _fetch_recent_events. None when the typed columns are not migrated, so
    the check can say so instead of failing; ProbeFailed when the probe
    itself failed (the caller WARNs). Module-level for tests."""
    client = client or get_supabase()
    if not client:
        return None
    if not _typed_columns_present(client):
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows, off = [], 0
    while True:
        q = (client.table('events').select(VERIFIED_COLUMNS).gte('discovered_at', since)
             .or_(VERIFIED_FILTER).order('discovered_at').order('id')
             .range(off, off + 999).execute())
        page = q.data or []
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def _accounts_by_period(rows, now, days=VERTICAL_WINDOW_DAYS):
    """({key: row} for the last `days`, {key: row} for the `days` before) —
    one entry per account_key (fallback: the event id), the newest verified
    event deciding its subindustry and provenance. Blocked rows are out."""
    recent_start = now - timedelta(days=days)
    prior_start = now - timedelta(days=2 * days)
    cur, prev = {}, {}
    for r in rows:
        if r.get('blocked_at'):
            continue
        dt = _parse_ts(r.get('discovered_at'))
        if dt is None or dt < prior_start:
            continue
        bucket = cur if dt >= recent_start else prev
        key = (str(r.get('account_key') or '').strip().lower()
               or 'id:{}'.format(r.get('id') or id(r)))
        if key not in bucket or dt > bucket[key][0]:
            bucket[key] = (dt, r)
    return ({k: v[1] for k, v in cur.items()}, {k: v[1] for k, v in prev.items()})


def _mix_counts(accounts):
    """(accounts by vertical, of those with provenance, of those verified
    without search) — three Counters over {key: row}."""
    counts, known, no_search = Counter(), Counter(), Counter()
    for r in accounts.values():
        v = vertical_of(r.get('zi_subindustry'))
        counts[v] += 1
        cb = str(r.get('classified_by') or '').strip().lower()
        if cb:
            known[v] += 1
            if cb in NO_SEARCH_CLASSIFIERS:
                no_search[v] += 1
    return counts, known, no_search


def _newest_by_vertical(accounts):
    """{vertical: datetime of its newest verified event} over {key: row} —
    the "dark since" date is the day a vertical last produced, not the
    Monday the check noticed."""
    out = {}
    for r in accounts.values():
        dt = _parse_ts(r.get('discovered_at'))
        if dt is None:
            continue
        v = vertical_of(r.get('zi_subindustry'))
        if v not in out or dt > out[v]:
            out[v] = dt
    return out


def _mix_line(counts, total, verticals):
    parts = [f'{v} {counts[v]} ({counts[v] / total * 100:.0f}%)' if total else f'{v} 0'
             for v in verticals]
    if counts[UNKNOWN_VERTICAL]:
        parts.append(f'{UNKNOWN_VERTICAL} {counts[UNKNOWN_VERTICAL]}')
    return ' · '.join(parts)


def check_vertical_mix(now=None):
    """Verified ACCOUNTS per vertical, last 28d vs the prior 28d (Phase 3
    2026-09-08) — the weekly mix report the plan asks for, plus the one
    nonprofit number it scores (verified without search, target > 50%).

    WARN  ONCE when a vertical goes dark — ≥ VERTICAL_DARK_MIN_PRIOR verified
          accounts in the prior 28d and 0 in the last 28d ("Consumer
          Services went dark") — or when a thin vertical (< VERTICAL_THIN_PCT
          of verified accounts while the others have some) was above
          VERTICAL_WAS_HEALTHY_PCT at the last weekly run ("was 18%").
    PASS  the mix line — a thin vertical is named in it as context, not as
          an alert; a vertical already on the dark record is listed as
          "still dark since <date>" until it recovers (≥ 1 verified account
          in the current window), then "recovered" once and the entry
          clears — or "not measurable yet" before the typed columns exist.

    Dark memory (review 2026-09-08; the quiet_sources.json pattern of
    check_source_yield): without it "went dark" repeated every Monday while
    the prior-28d window drained, then went silent for good once it had —
    after eight weeks a dark vertical looked healthy. The record lives in
    state/vertical_mix.json under 'dark' ({vertical: {since, prior}}, since
    = the date of the vertical's last verified account) and is rewritten
    only when something changed. Manual clear: delete the vertical from
    that map; it re-arms only while the prior window still holds
    ≥ VERTICAL_DARK_MIN_PRIOR accounts. The mix itself is rewritten on every
    run that had something to count, so the check can say what a share WAS.
    """
    now = now or datetime.now(timezone.utc)
    if not get_supabase():
        return WARN, 'Supabase unavailable — cannot check'
    try:
        rows = _fetch_verified_accounts(2 * VERTICAL_WINDOW_DAYS)
    except ProbeFailed as e:
        return WARN, f'Vertical mix probe failed: {e} — a transient, not the migration; retried next run'
    except Exception as e:
        return WARN, f'Could not read verified accounts: {e}'
    if rows is None:
        return PASS, 'Vertical mix not measurable yet (typed columns not migrated)'
    if not ZI_SUBINDUSTRIES:
        return WARN, ('Vertical taxonomy unavailable (enrichment_scout.ZI_SUBINDUSTRIES failed '
                      'to import) — cannot bucket verified accounts')
    verticals = list(dict.fromkeys(ZI_SUBINDUSTRIES.values()))
    cur, prev = _accounts_by_period(rows, now)
    counts, known, no_search = _mix_counts(cur)
    prev_counts = _mix_counts(prev)[0]
    total = sum(counts.values())
    pct = {v: (counts[v] / total * 100 if total else 0.0) for v in verticals}

    last = _state_json(VERTICAL_MIX_STATE_FILE)
    last = last if isinstance(last, dict) else {}
    last_mix = last.get('mix') if isinstance(last.get('mix'), dict) else {}
    last_when = str(last.get('checked_at') or '')[:10] or 'the last weekly run'
    on_record = last.get('dark') if isinstance(last.get('dark'), dict) else {}
    on_record = {v: e for v, e in on_record.items() if v in verticals}

    # Dark memory: on record → "still dark" context until ≥ 1 verified account
    # shows up ("recovered", then cleared); newly dark → WARN once and record.
    recovered = [v for v in verticals if v in on_record and counts[v] >= 1]
    still_dark = [v for v in verticals if v in on_record and counts[v] == 0]
    dark = [v for v in verticals if v not in on_record
            and prev_counts[v] >= VERTICAL_DARK_MIN_PRIOR and counts[v] == 0]
    record = {v: e for v, e in on_record.items() if v not in recovered}
    if dark:
        last_seen = _newest_by_vertical(prev)
        for v in dark:
            record[v] = {'since': (last_seen.get(v) or now).date().isoformat(),
                         'prior': prev_counts[v]}

    state = {k: last[k] for k in ('checked_at', 'total', 'prior_total', 'mix') if k in last}
    if total:   # nothing to remember about the mix when nothing verified
        state.update({
            'checked_at': now.isoformat(), 'total': total, 'prior_total': len(prev),
            'mix': {v: {'n': counts[v], 'pct': round(pct[v], 1)} for v in verticals},
        })
    if record:
        state['dark'] = record
    if total or dark or recovered:
        _state_write_json(VERTICAL_MIX_STATE_FILE, state)

    def _last_pct(v):
        entry = last_mix.get(v)
        try:
            return float(entry.get('pct')) if isinstance(entry, dict) else None
        except (TypeError, ValueError):
            return None

    unlit = set(dark) | set(still_dark)
    thin = [v for v in verticals if total and v not in unlit and pct[v] < VERTICAL_THIN_PCT
            and any(counts[o] for o in verticals if o != v)]
    thin_warn = [v for v in thin if (_last_pct(v) or 0) > VERTICAL_WAS_HEALTHY_PCT]

    mix = (f'{_mix_line(counts, total, verticals)} — {total} verified accounts/'
           f'{VERTICAL_WINDOW_DAYS}d (prior {VERTICAL_WINDOW_DAYS}d: {len(prev)})')
    np_known, np_free = known[NONPROFIT_VERTICAL], no_search[NONPROFIT_VERTICAL]
    if np_known:
        mix += (f' · nonprofits verified without search: {np_free / np_known * 100:.0f}% '
                f'({np_free} of {np_known} with provenance; target > '
                f'{NONPROFIT_NO_SEARCH_TARGET_PCT}%)')
    else:
        mix += ' · nonprofits verified without search: n/a (no provenance recorded yet)'
    if recovered:
        mix += ' · recovered: ' + ', '.join(
            f'{v} (dark since {_quiet_entry(on_record[v])[0]})' for v in recovered)
    for v in still_dark:
        since, prior = _quiet_entry(on_record[v])
        mix += f' · {v} still dark since {since} (was {prior}/{VERTICAL_WINDOW_DAYS}d)'

    problems = [f'{v} went dark — 0 verified accounts in the last {VERTICAL_WINDOW_DAYS}d, '
                f'was {prev_counts[v]} in the prior {VERTICAL_WINDOW_DAYS}d' for v in dark]
    problems += [f'{v} is thin — {pct[v]:.0f}% of verified accounts (was {_last_pct(v):.0f}% '
                 f'at the last weekly run, {last_when})' for v in thin_warn]
    if problems:
        return WARN, f'{"; ".join(problems)} · mix: {mix}'
    info = [v for v in thin if v not in thin_warn]
    if info:
        mix += ' · thin: ' + ', '.join(f'{v} {pct[v]:.0f}%' for v in info)
    return PASS, f'Verified accounts by vertical ({VERTICAL_WINDOW_DAYS}d): {mix}'


def _on_target_mark(prev):
    """{'pct', 'checked_at' (aware)} of the most recent run at/above the
    target, from the state file, or None. Tolerant of hand edits. A file
    written before the mark existed (2026-09-08) whose own share_pct was on
    target IS the mark — the first run after the upgrade must not forget
    that last Monday was fine."""
    mark = prev.get('last_on_target')
    if not isinstance(mark, dict):
        try:
            if float(prev.get('share_pct')) < FINANCE_SHARE_TARGET_PCT:
                return None
        except (TypeError, ValueError):
            return None
        mark = {'pct': prev.get('share_pct'), 'checked_at': prev.get('checked_at')}
    try:
        pct = float(mark.get('pct'))
    except (TypeError, ValueError):
        return None
    when = _parse_ts(mark.get('checked_at'))
    return {'pct': pct, 'checked_at': when} if when else None


def check_finance_leader_share(now=None):
    """Finance-leader triggers (cfo_hire / finance_seat_open / #NewController)
    as a share of survivors in the last 28d, against the Phase 3 target
    FINANCE_SHARE_TARGET_PCT (2026-09-08). Informational by design — the
    share sits below target until the new supply lands — so it WARNs only
    for a collapse: under FINANCE_SHARE_WARN_PCT while the high-water mark
    (the most recent run at/above the target, kept in
    state/finance_leader_share.json as last_on_target) is at most
    FINANCE_SHARE_MARK_MAX_AGE_DAYS old. Review 2026-09-08: comparing with
    the immediately previous run let a two-week slide 32% → 27% → 11% pass
    in silence. The WARN repeats on each weekly run while the mark is fresh
    and the share stays collapsed; it stops when the share recovers or the
    mark ages out. The rest of the file is rewritten on every measurable
    run; the mark is carried forward until a run at/above target replaces it.
    """
    now = now or datetime.now(timezone.utc)
    if not get_supabase():
        return WARN, 'Supabase unavailable — cannot check'
    try:
        rows = _fetch_recent_events(YIELD_WINDOW_DAYS)
    except Exception as e:
        return WARN, f'Could not read recent events: {e}'
    surv = _survivors(rows)
    n = len(surv)
    if n == 0:
        return PASS, (f'No survivors in the last {YIELD_WINDOW_DAYS} days to measure '
                      f'(see "Source yield")')
    fam_n = sum(1 for r in surv if finance_leader_family(r))
    share = fam_n / n * 100

    prev = _state_json(FINANCE_SHARE_STATE_FILE)
    prev = prev if isinstance(prev, dict) else {}
    try:
        prev_pct = float(prev['share_pct']) if 'share_pct' in prev else None
    except (TypeError, ValueError):
        prev_pct = None
    prev_when = str(prev.get('checked_at') or '')[:10] or 'the last weekly run'
    mark = _on_target_mark(prev)
    if share >= FINANCE_SHARE_TARGET_PCT:
        mark = {'pct': share, 'checked_at': now}
    state = {'share_pct': round(share, 1), 'family_n': fam_n, 'survivors_n': n,
             'checked_at': now.isoformat()}
    if mark:
        state['last_on_target'] = {'pct': round(mark['pct'], 1),
                                   'checked_at': mark['checked_at'].isoformat()}
    _state_write_json(FINANCE_SHARE_STATE_FILE, state)

    msg = (f'Finance-leader triggers are {share:.0f}% of survivors ({fam_n} of {n} in '
           f'{YIELD_WINDOW_DAYS}d; target ≥ {FINANCE_SHARE_TARGET_PCT}%)')
    notes = [f'was {prev_pct:.0f}% on {prev_when}'] if prev_pct is not None else []
    if share >= FINANCE_SHARE_TARGET_PCT:
        return PASS, f'{msg} — on target' + (f' ({notes[0]})' if notes else '')
    mark_when = mark['checked_at'].date().isoformat() if mark else None
    mark_fresh = mark is not None and (now - mark['checked_at']).days <= FINANCE_SHARE_MARK_MAX_AGE_DAYS
    if share < FINANCE_SHARE_WARN_PCT and mark_fresh:
        return WARN, (f'{msg} — fell from {mark["pct"]:.0f}% on {mark_when}, the last run on '
                      f'target; the best trigger is drying up — check "Source yield" and '
                      f'"Fetched vs filtered" for the feed that stopped')
    if mark and mark_when != prev_when:      # the mark is older than the last run: say so
        notes.append(f'last on target {mark["pct"]:.0f}% on {mark_when}'
                     + ('' if mark_fresh else
                        f', over {FINANCE_SHARE_MARK_MAX_AGE_DAYS // 7} weeks ago'))
    was = f' ({"; ".join(notes)})' if notes else ''
    return PASS, f'{msg} — below target{was}; Phase 3 supply is what moves it'


def _count(query):
    r = query.limit(1).execute()
    return r.count or 0


def check_retry_backlog(now=None):
    """Informational: how many researched-ambiguous rows are due for a retry,
    how many are waiting on their backoff, how many are negative-cached
    (enrich_attempts ≥ MAX_ENRICH_ATTEMPTS). Reads the typed columns only;
    before the migration it PASSes quietly."""
    now = now or datetime.now(timezone.utc)
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    try:
        if not _typed_columns_present(client):
            return PASS, 'Retry backlog not measurable yet (typed columns not migrated)'
    except ProbeFailed as e:
        return WARN, f'Retry backlog probe failed: {e} — a transient, not the migration; retried next run'
    try:
        iso = now.isoformat()
        base = lambda: client.table('events').select('id', count='exact').is_('blocked_at', 'null')  # noqa: E731
        due = _count(base().eq('verify_state', RETRY_STATE)
                     .or_(f'retry_after.is.null,retry_after.lte.{iso}')
                     .lt('enrich_attempts', MAX_ENRICH_ATTEMPTS))
        waiting = _count(base().gt('retry_after', iso))
        cached = _count(base().gte('enrich_attempts', MAX_ENRICH_ATTEMPTS))
    except Exception as e:
        return WARN, f'Could not count the retry backlog: {e}'
    return PASS, (
        f'Retry backlog: {due} due now · {waiting} waiting on backoff · '
        f'{cached} negative-cached after {MAX_ENRICH_ATTEMPTS} attempts (retried only when a '
        f'new event for the account arrives)'
    )


# ── Reporting ───────────────────────────────────────────────────────────────

def run_checks(mode: str):
    """Return list of (check_name, status, message)."""
    checks = [
        ('Environment credentials',     check_env_creds),
        ('Firecrawl search canary',     check_firecrawl_canary),
        ('Tavily budget',               check_tavily_budget),
        ('llama.cpp (local LLM)',       check_llamacpp),
        ('Supabase connection',         check_supabase),
        ('Scrape freshness',            check_scrape_freshness),
        ('Enrichment lag',              check_enrichment_lag),
        ('Rep state intact',            check_rep_state_intact),
        ('Local SQLite DB',             check_local_sqlite),
        ('launchd enrichment job',      check_launchd_job),
        ('Enrichment ↔ Supabase',       check_enrichment_transport),
    ]
    if mode in ('daily', 'weekly'):
        checks += [
            ('Source health',           check_source_health),
            ('Event volume trend',      check_event_volume_trend),
            # Yield, not liveness (2026-09-07). The weekly-only "Cleanup dry-run"
            # check was replaced by these: cleanup_legacy_events.py is a v1
            # leftover deleted in Phase 4, and its WARN fired on every run.
            ('Source yield (7d vs prior 21d)',  check_source_yield),
            ('Fetched vs filtered',             check_fetched_vs_filtered),
            ('Retry backlog',                   check_retry_backlog),
        ]
    if mode == 'weekly':
        # Monday only (review 2026-09-07): the mix cannot pass the bar until
        # Phase 3, and it is change-driven, so once a week is the right cadence.
        checks += [
            ('Finance-leader source mix',       check_trigger_source_concentration),
            # Phase 3 supply report (2026-09-08): is the new supply producing
            # the RIGHT mix — enough finance-leader triggers, every vertical
            # still fed? Informational unless something collapsed.
            ('Finance-leader share of intake',  check_finance_leader_share),
            ('Vertical mix (28d vs prior 28d)', check_vertical_mix),
        ]
    results = []
    for name, fn in checks:
        try:
            status, msg = fn()
        except Exception as e:
            status, msg = FAIL, f'check crashed: {e}'
        results.append((name, status, msg))
    return results


def print_report(results, mode: str, json_mode: bool):
    if json_mode:
        out = {
            'mode':      mode,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'state_read_only': STATE_READ_ONLY,
            'checks':    [{'name': n, 'status': PLAIN[s], 'message': m}
                          for n, s, m in results],
        }
        out['overall'] = 'fail' if any(s == FAIL for _, s, _ in results) else (
                         'warn' if any(s == WARN for _, s, _ in results) else 'pass')
        print(json.dumps(out, indent=2))
        return

    fails = sum(1 for _, s, _ in results if s == FAIL)
    warns = sum(1 for _, s, _ in results if s == WARN)
    passes = sum(1 for _, s, _ in results if s == PASS)
    overall = FAIL if fails else (WARN if warns else PASS)

    note = ' · state read-only (--no-state)' if STATE_READ_ONLY else ''
    print(f'\n=== Health check ({mode}) — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ==={note}\n')
    name_w = max(len(n) for n, _, _ in results) + 2
    for n, s, m in results:
        print(f'  {s}  {n:<{name_w}}  {m}')
    print()
    print(f'Summary: {passes} pass · {warns} warn · {fails} fail')
    print(f'Overall: {overall}')
    print()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--quick',  action='store_true', help='Essential checks only (default)')
    p.add_argument('--daily',  action='store_true', help='Adds source health, volume trend + yield checks')
    p.add_argument('--weekly', action='store_true', help='--daily checks + Monday-only: finance-leader mix + share of intake, vertical mix')
    p.add_argument('--json',   action='store_true', help='Machine-readable output')
    p.add_argument('--no-state', action='store_true',
                   help='Read the state/ files but never write them — for manual --weekly runs, '
                        'so the baselines the Monday cron compares against are not overwritten')
    return p.parse_args(argv)


def main(argv=None):
    global STATE_READ_ONLY
    args = parse_args(argv)
    STATE_READ_ONLY = bool(args.no_state)

    mode = 'weekly' if args.weekly else ('daily' if args.daily else 'quick')

    results = run_checks(mode)
    print_report(results, mode, args.json)

    # Exit non-zero if anything failed (so cron / the reading agent can detect)
    if any(s == FAIL for _, s, _ in results):
        sys.exit(1)


if __name__ == '__main__':
    main()
