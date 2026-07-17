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


def check_firecrawl():
    """Verify the PRIMARY search backend (local Firecrawl) is reachable.
    This is the one that matters — ~97% of enrichment searches go here."""
    url = os.environ.get('FIRECRAWL_URL', 'http://localhost:3002')
    try:
        # Hit the root — Firecrawl returns 200 with a small JSON banner.
        resp = requests.get(f'{url}/', timeout=8)
        if resp.status_code == 200:
            return PASS, f'Firecrawl reachable at {url} (primary search backend)'
        return WARN, (
            f'Firecrawl at {url} returned HTTP {resp.status_code} — '
            f'enrichment will lean on the Tavily fallback. '
            f'Check: docker compose ps firecrawl-api-1'
        )
    except requests.ConnectionError:
        return FAIL, (
            f'Firecrawl not reachable at {url} — enrichment search backend '
            f'is DOWN. Start it: docker compose up -d firecrawl-api-1'
        )
    except requests.Timeout:
        return WARN, f'Firecrawl slow to respond (>8s) at {url}'
    except Exception as e:
        return WARN, f'Firecrawl check error: {e}'


def check_tavily_fallback():
    """Check the OPTIONAL Tavily fallback. Tavily is only used ~3% of the
    time (when Firecrawl returns empty), so problems here are INFORMATIONAL,
    never a hard failure — a depleted Tavily quota does not break enrichment.

    Returns PASS when healthy or when not configured (it's optional). Returns
    WARN only to surface a depleted/invalid key for awareness, not alarm."""
    key = os.environ.get('TAVILY_API_KEY', '')
    if not key:
        return PASS, 'Tavily fallback not configured (optional — Firecrawl is primary)'
    try:
        resp = requests.post(
            'https://api.tavily.com/search',
            json={'api_key': key, 'query': 'test', 'max_results': 1,
                  'search_depth': 'basic'},
            timeout=10
        )
        if resp.status_code == 200:
            return PASS, 'Tavily fallback healthy (used for ~3% of searches)'
        if resp.status_code in (401, 403):
            return WARN, (
                f'Tavily fallback key invalid (HTTP {resp.status_code}) — '
                f'not urgent; Firecrawl handles ~97% of searches. '
                f'Rotate the key when convenient.'
            )
        if resp.status_code in (429, 432):
            # Quota exhaustion on the free tier is the EXPECTED steady state,
            # not a problem — Firecrawl handles ~97% of searches. Report as
            # PASS so it doesn't trigger a daily alert; the message still
            # carries the info for anyone reading the full report.
            return PASS, (
                f'Tavily fallback quota used up (HTTP {resp.status_code}) — '
                f'expected on free tier, enrichment unaffected (Firecrawl primary)'
            )
        return WARN, f'Tavily fallback HTTP {resp.status_code}: {resp.text[:60]}'
    except requests.Timeout:
        return PASS, 'Tavily fallback slow (>10s) — non-critical, Firecrawl is primary'
    except Exception as e:
        return WARN, f'Tavily fallback check error (non-critical): {e}'


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
        # cleanup_legacy_events.py uses Python logging which goes to stderr —
        # check both streams for the result line
        combined = (r.stdout or '') + '\n' + (r.stderr or '')
        for line in combined.split('\n'):
            if 'would be dropped' in line:
                # Format: "Result: N of M events would be dropped"
                parts = line.replace(':', '').split()
                try:
                    n = int(parts[1])
                except (IndexError, ValueError):
                    continue
                # A small residual of genuine syndication dupes / stray
                # off-target events is normal steady-state — news gets
                # syndicated, and a few items land just outside the scrape-time
                # dedup window. That's routine tidiness, not a health problem,
                # so don't nag weekly about it. Only WARN when noise ACCUMULATES
                # past the threshold, which signals the scrape-time dedup or
                # industry filters have actually regressed.
                CLEANUP_WARN_THRESHOLD = 25
                if n == 0:
                    return PASS, 'No noise in DB (cleanup dry-run clean)'
                if n > CLEANUP_WARN_THRESHOLD:
                    return WARN, (
                        f'{n} noise events accumulating (> {CLEANUP_WARN_THRESHOLD}) '
                        f'— scrape dedup/filters may have regressed. Review, then '
                        f'`python cleanup_legacy_events.py --apply`'
                    )
                return PASS, (
                    f'{n} minor noise events (below {CLEANUP_WARN_THRESHOLD} '
                    f'threshold — routine, optional `cleanup_legacy_events.py --apply`)'
                )
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
        ('Firecrawl (search backend)',  check_firecrawl),
        ('Tavily fallback',             check_tavily_fallback),
        ('llama.cpp (local LLM)',       check_llamacpp),
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
