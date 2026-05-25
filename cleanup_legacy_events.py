#!/usr/bin/env python3
"""
cleanup_legacy_events.py — Retroactively apply current filter rules to events.

Over the life of the project, the territory / industry / company-exclusion
rules have been tightened (mining/steel/oil-gas blocked, mega-banks blocked,
etc.). Events ingested before those rules existed are still in Supabase
polluting the dashboard. This script:

  1. Loads the current config (config.yaml)
  2. Walks every event in Supabase
  3. For each, applies the same matches_industry() + is_public_company()
     logic the live scraper uses
  4. Deletes events that would NOT pass today's filters

Defaults to DRY RUN — shows what would be deleted without touching anything.
Pass --apply to actually delete.

Usage:
    python cleanup_legacy_events.py                # dry-run, all events
    python cleanup_legacy_events.py --apply        # actually delete
    python cleanup_legacy_events.py --limit 100    # only check first 100
"""

import os
import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path
from collections import Counter

# .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / '.env')
except ImportError:
    pass

import yaml

try:
    from supabase import create_client
except ImportError:
    sys.exit("Run: pip install supabase python-dotenv pyyaml")

# Reuse the live scraper's filtering logic
sys.path.insert(0, str(Path(__file__).parent))
from src.scrapers.base import BaseScraper


logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
)
log = logging.getLogger(__name__)


# Thin shim so we can instantiate BaseScraper's filter methods
class _FilterOnlyScraper(BaseScraper):
    def scrape(self):
        return []


def get_supabase():
    url = os.environ.get('SUPABASE_URL')
    key = (os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
           or os.environ.get('SUPABASE_KEY'))
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
    return create_client(url, key)


def load_config():
    cfg_path = Path(__file__).parent / 'config.yaml'
    if not cfg_path.exists():
        sys.exit("config.yaml not found. Run: cp config.example.yaml config.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def classify_event(scraper: BaseScraper, event: dict):
    """Decide whether to KEEP or DROP an event. Returns (keep: bool, reason: str)."""
    title       = event.get('title', '') or ''
    description = event.get('description', '') or ''
    company     = event.get('company_name', '') or ''
    full_text = f'{title} {description} {company}'

    # 1. Industry exclusion (mining, steel, oil & gas, hotel, etc.)
    _matches_target, matches_excluded = scraper.matches_industry(full_text)
    if matches_excluded:
        # Identify which specific keyword matched (best-effort, for reporting)
        text_lower = full_text.lower()
        hit = next(
            (kw for kw in scraper.excluded_industries if kw in text_lower),
            'industry'
        )
        return False, f'excluded_industry: "{hit}"'

    # 2. Mega-cap public company exclusion
    if scraper.is_public_company(full_text):
        text_lower = full_text.lower()
        hit = next(
            (c for c in scraper.excluded_public_companies if c in text_lower),
            'public-co'
        )
        return False, f'excluded_company: "{hit}"'

    return True, ''


def find_title_duplicates(events: list) -> list:
    """Find groups of events that share the same normalized title (syndicated
    press releases). Returns [(title, [events_to_delete])] — keeps the most
    enriched/oldest copy and marks the rest for deletion."""
    from collections import defaultdict
    by_title = defaultdict(list)
    for e in events:
        t = (e.get('title') or '').strip().lower()
        if t:
            by_title[t].append(e)

    drops = []
    for title, evs in by_title.items():
        if len(evs) < 2:
            continue
        # Pick the BEST one to keep:
        #   1. Prefer events that have been enriched (companies_data + grade)
        #   2. Among those, prefer oldest (already had time in DB)
        def quality_score(e):
            has_companies = bool(e.get('companies_data'))
            has_grade = bool(e.get('grade'))
            return (int(has_companies), int(has_grade))
        evs_sorted = sorted(evs, key=lambda e: (
            -quality_score(e)[0],         # most companies_data first
            -quality_score(e)[1],         # then most graded
            (e.get('discovered_at') or '') # then oldest
        ))
        keep = evs_sorted[0]
        to_delete = evs_sorted[1:]
        drops.append((title, keep, to_delete))
    return drops


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true',
                   help='Actually delete (default: dry-run, no changes)')
    p.add_argument('--limit', type=int, default=None,
                   help='Process at most N events (default: all)')
    p.add_argument('--skip-dedup', action='store_true',
                   help='Skip title-based dedup pass (default: dedupe enabled)')
    args = p.parse_args()

    cfg = load_config()
    scraper = _FilterOnlyScraper(cfg)
    client = get_supabase()

    # Fetch all events (full record — needed for dedup quality scoring)
    response = client.table('events').select(
        'id, title, description, company_name, event_type, discovered_at, '
        'companies_data, grade, source_url'
    ).order('discovered_at', desc=True).execute()
    events = response.data or []
    if args.limit:
        events = events[:args.limit]

    if not events:
        log.info('No events in Supabase. Nothing to clean.')
        return

    mode = 'APPLY (will delete)' if args.apply else 'DRY-RUN (no changes)'
    log.info(f'\n=== Cleanup — mode: {mode} ===')
    log.info(f'Loaded {len(events)} events from Supabase\n')

    drops = []
    reason_counts = Counter()

    # ── Pass 1: industry / public-company exclusions ────────────────────
    for ev in events:
        keep, reason = classify_event(scraper, ev)
        if not keep:
            drops.append((ev, reason))
            reason_counts[reason.split(':')[0]] += 1

    drop_ids = {ev['id'] for ev, _ in drops}

    # ── Pass 2: title-based dedup (syndicated press releases) ───────────
    dedup_drops = []
    if not args.skip_dedup:
        survivors = [e for e in events if e['id'] not in drop_ids]
        dup_groups = find_title_duplicates(survivors)
        for title, keep, to_delete in dup_groups:
            for d in to_delete:
                reason = (
                    f'duplicate_title: kept id={keep["id"][:10]}… '
                    f'(more enriched), dropping this copy'
                )
                dedup_drops.append((d, reason))
                reason_counts['duplicate_title'] += 1
        drops.extend(dedup_drops)

    log.info(f'Result: {len(drops)} of {len(events)} events would be dropped')
    if reason_counts:
        log.info('\nReasons:')
        for r, n in reason_counts.most_common():
            log.info(f'  {n:4d}  {r}')

    if drops:
        log.info('\nSample of drops (first 20):')
        for ev, reason in drops[:20]:
            title = (ev.get('title') or '')[:65]
            company = ev.get('company_name') or '?'
            log.info(f'  - [{ev.get("event_type","")[:6]:6}] {company[:24]:24}  '
                     f'{title}  ({reason})')

    if not args.apply:
        log.info(
            f'\nDry-run complete. To actually delete {len(drops)} events:'
            f'\n  python cleanup_legacy_events.py --apply'
        )
        return

    if not drops:
        log.info('Nothing to delete. Done.')
        return

    log.info(f'\nDeleting {len(drops)} events from Supabase...')
    deleted = errors = 0
    for ev, _reason in drops:
        try:
            client.table('events').delete().eq('id', ev['id']).execute()
            deleted += 1
        except Exception as e:
            errors += 1
            log.error(f'  Failed to delete {ev.get("id")}: {e}')

    log.info(f'\nDone. Deleted: {deleted}, Errors: {errors}')


if __name__ == '__main__':
    main()
