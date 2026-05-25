#!/usr/bin/env python3
"""
monitor_health.py — End-to-end health check for TeamAlbertTriggerEventSearch.

Designed to be run periodically by Elon (or as a launchd cron) to catch
issues before A.J. notices them in the dashboard.

Usage:
    python monitor_health.py            # default = --quick (~10s)
    python monitor_health.py --quick    # essential checks only
    python monitor_health.py --daily    # adds source health + flow analysis
    python monitor_health.py --weekly   # adds trend analysis + cleanup dry-run
    python monitor_health.py --json     # machine-readable output

Exit codes:
    0  — all checks PASS, or any WARN
    1  — at least one FAIL (cron/CI can detect)
"""

import os
import sys
import json
import argparse
import sqlite3
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import Counter

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


# ── Individual checks (each returns (status, message)) ───────────────────────

def check_env_creds():
    """Verify required env vars are set (without revealing values)."""
    required = ['SUPABASE_URL', 'TAVILY_API_KEY']
    optional = ['SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_KEY',
                'ADZUNA_APP_ID', 'ADZUNA_APP_KEY']
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


def check_tavily_api():
    """Make a tiny Tavily call to confirm API works + key is valid."""
    key = os.environ.get('TAVILY_API_KEY', '')
    if not key:
        return FAIL, 'TAVILY_API_KEY not set'
    try:
        resp = requests.post(
            'https://api.tavily.com/search',
            json={'api_key': key, 'query': 'test', 'max_results': 1,
                  'search_depth': 'basic'},
            timeout=10
        )
        if resp.status_code == 200:
            return PASS, 'Tavily API responsive (HTTP 200)'
        if resp.status_code == 401 or resp.status_code == 403:
            return FAIL, f'Tavily key invalid (HTTP {resp.status_code}) — rotate or update .env'
        if resp.status_code == 429:
            return WARN, 'Tavily rate-limited (HTTP 429) — quota near/at limit'
        return WARN, f'Tavily HTTP {resp.status_code}: {resp.text[:80]}'
    except requests.Timeout:
        return WARN, 'Tavily timeout (>10s) — service may be slow'
    except Exception as e:
        return FAIL, f'Tavily call failed: {e}'


def check_ollama():
    """Verify Ollama is running and the enrichment model is available."""
    url = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
    model = os.environ.get('OLLAMA_MODEL', 'qwen3-coder:30b')
    try:
        resp = requests.get(f'{url}/api/tags', timeout=5)
        if resp.status_code != 200:
            return FAIL, f'Ollama returned HTTP {resp.status_code}'
        models = [m['name'] for m in resp.json().get('models', [])]
        if model not in models:
            return FAIL, f'Ollama running but model "{model}" not pulled. Available: {models[:3]}'
        return PASS, f'Ollama up, model "{model}" available'
    except requests.ConnectionError:
        return FAIL, f'Ollama not reachable at {url} — is it running? (`brew services start ollama`)'
    except Exception as e:
        return WARN, f'Ollama check error: {e}'


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
    """Has a scrape happened recently? Look at most recent discovered_at."""
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    try:
        result = client.table('events').select('discovered_at').order(
            'discovered_at', desc=True).limit(1).execute()
        if not result.data:
            return WARN, 'No events in DB at all'
        last = result.data[0]['discovered_at']
        dt = datetime.fromisoformat(last.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_hr = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if age_hr > 8:
            return FAIL, f'Last scrape was {age_hr:.1f}h ago — GitHub Actions may be broken (cron is every 4h)'
        if age_hr > 5:
            return WARN, f'Last scrape was {age_hr:.1f}h ago — slightly stale'
        return PASS, f'Last scrape {age_hr:.1f}h ago'
    except Exception as e:
        return WARN, f'Could not check scrape freshness: {e}'


def check_enrichment_lag():
    """Are events sitting unenriched too long?"""
    client = get_supabase()
    if not client:
        return WARN, 'Supabase unavailable — cannot check'
    try:
        # Events that have no enriched_at AND are >5h old → enrichment is lagging
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        result = client.table('events').select(
            'id', count='exact'
        ).is_('enriched_at', 'null').lt('discovered_at', cutoff).execute()
        stale = result.count or 0
        if stale > 10:
            return FAIL, f'{stale} events unenriched after 5+ hours — launchd cron may be broken'
        if stale > 3:
            return WARN, f'{stale} events unenriched after 5+ hours'
        return PASS, f'Enrichment current ({stale} stale events)'
    except Exception as e:
        return WARN, f'Could not check enrichment lag: {e}'


def check_local_sqlite():
    """Verify the local SQLite DB exists and has the expected schema."""
    db_path = Path(__file__).parent / 'trigger_events.db'
    if not db_path.exists():
        return WARN, 'trigger_events.db not present locally (normal if scraper only runs in CI)'
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM events")
            n_events = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM seen_urls")
            n_urls = cursor.fetchone()[0]
        return PASS, f'Local SQLite OK ({n_events} events, {n_urls} seen URLs)'
    except sqlite3.OperationalError as e:
        return WARN, f'SQLite schema issue: {e}'
    except Exception as e:
        return WARN, f'SQLite check failed: {e}'


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


# ── --weekly checks ─────────────────────────────────────────────────────────

def check_cleanup_dryrun():
    """Run cleanup_legacy_events.py in dry-run mode to surface fresh noise."""
    import subprocess
    script = Path(__file__).parent / 'cleanup_legacy_events.py'
    if not script.exists():
        return WARN, 'cleanup_legacy_events.py not present'
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=60,
            cwd=str(script.parent)
        )
        # Parse output for the "would be dropped" line
        for line in r.stdout.split('\n'):
            if 'would be dropped' in line:
                n = int(line.split()[1])
                if n == 0:
                    return PASS, 'No noise in DB (cleanup dry-run clean)'
                if n > 10:
                    return WARN, f'{n} events would be cleanup-dropped — review filters'
                return WARN, f'{n} noise events present — run `python cleanup_legacy_events.py --apply`'
        return WARN, 'Cleanup dry-run produced unexpected output'
    except subprocess.TimeoutExpired:
        return WARN, 'Cleanup dry-run timed out'
    except Exception as e:
        return WARN, f'Cleanup dry-run failed: {e}'


# ── Reporting ───────────────────────────────────────────────────────────────

def run_checks(mode: str):
    """Return list of (check_name, status, message)."""
    checks = [
        ('Environment credentials',     check_env_creds),
        ('Tavily API',                  check_tavily_api),
        ('Ollama (local LLM)',          check_ollama),
        ('Supabase connection',         check_supabase),
        ('Scrape freshness',            check_scrape_freshness),
        ('Enrichment lag',              check_enrichment_lag),
        ('Local SQLite DB',             check_local_sqlite),
        ('launchd enrichment job',      check_launchd_job),
    ]
    if mode in ('daily', 'weekly'):
        checks += [
            ('Source health',           check_source_health),
            ('Event volume trend',      check_event_volume_trend),
        ]
    if mode == 'weekly':
        checks += [
            ('Cleanup dry-run',         check_cleanup_dryrun),
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

    print(f'\n=== Health check ({mode}) — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ===\n')
    name_w = max(len(n) for n, _, _ in results) + 2
    for n, s, m in results:
        print(f'  {s}  {n:<{name_w}}  {m}')
    print()
    print(f'Summary: {passes} pass · {warns} warn · {fails} fail')
    print(f'Overall: {overall}')
    print()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--quick',  action='store_true', help='Essential checks only (default)')
    p.add_argument('--daily',  action='store_true', help='Adds source health + volume trend')
    p.add_argument('--weekly', action='store_true', help='Adds cleanup dry-run')
    p.add_argument('--json',   action='store_true', help='Machine-readable output')
    args = p.parse_args()

    mode = 'weekly' if args.weekly else ('daily' if args.daily else 'quick')

    results = run_checks(mode)
    print_report(results, mode, args.json)

    # Exit non-zero if anything failed (so cron / Elon can detect)
    if any(s == FAIL for _, s, _ in results):
        sys.exit(1)


if __name__ == '__main__':
    main()
