#!/usr/bin/env python3
"""
Sales Territory Trigger Event Scraper

Monitors news sources for trigger events (CFO hires, M&A, funding) in your
sales territory and sends alerts.

Usage:
    python -m src.main              # Run once
    python -m src.main --daemon     # Run continuously
    python -m src.main --stats      # Show statistics
"""

import argparse
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import yaml

from .models import TriggerEvent, EventSource
from .database import DatabaseManager
from .alerts import AlertManager
from .scrapers import (
    RSSScraper, GoogleNewsScraper, JobScraper, BingNewsScraper,
    FinSMEsScraper, SECScraper, FormDScraper, AdzunaScraper,
)
from .enrichment import CompanyEnricher
from .pipeline.gates import account_key

# NOTE: Supabase sync is NOT called from here. The GitHub Actions workflow's
# "Sync to Supabase" step (supabase_sync.py) is the single sync path; calling
# it from the scrape cycle was a no-op in CI and a landmine locally.


def _job_posting_dedup_key(event: TriggerEvent) -> str:
    """Composite dedup identity for job postings (Adzuna): the ACCOUNT plus
    the normalized job title. One national posting that Adzuna lists under
    five territory states arrives as five URLs with the same company +
    title — that is ONE open seat, so it must collapse to ONE lead.

    Key = 'job|<account_key(company)>|<normalized title>'. The job title is
    the part after ' hiring: ' in the templated event title; falls back to
    the whole title when the template is absent.
    """
    title = (event.title or '').strip()
    if ' hiring: ' in title:
        job_title = title.split(' hiring: ', 1)[1]
    else:
        job_title = title
    norm_title = re.sub(r'[^a-z0-9]+', ' ', job_title.lower()).strip()
    company = account_key(event.company_name) or account_key(
        title.split(' hiring: ', 1)[0] if ' hiring: ' in title else '')
    return f'job|{company}|{norm_title}'


class TriggerEventMonitor:
    """Main orchestrator for trigger event monitoring."""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        self.db = DatabaseManager(
            self.config.get('scraper', {}).get('database', 'trigger_events.db')
        )
        self.alert_manager = AlertManager(self.config)
        self.enricher = CompanyEnricher(self.config)
        self.running = True

        # Initialize scrapers
        self.scrapers = [
            RSSScraper(self.config),
            GoogleNewsScraper(self.config),
            JobScraper(self.config),
            BingNewsScraper(self.config),
            FinSMEsScraper(self.config),
            SECScraper(self.config),
            FormDScraper(self.config),
            AdzunaScraper(self.config, db=self.db),
        ]

        if self.enricher.enabled:
            print(f"Company enrichment enabled ({self.enricher.provider})")
        else:
            print("Company enrichment disabled (no API key)")

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from YAML file."""
        config_file = Path(config_path)
        if not config_file.exists():
            print(f"Error: Config file not found: {config_path}")
            sys.exit(1)

        with open(config_file) as f:
            return yaml.safe_load(f)

    def run_once(self) -> List[TriggerEvent]:
        """Run a single scrape cycle and return new events."""
        print(f"\n{'='*60}")
        print(f"Starting scrape cycle at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        all_events = []
        new_events = []

        # Get max age setting
        max_age_hours = self.config.get('scraper', {}).get('max_age_hours', 72)
        cutoff_date = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

        # Run all scrapers
        for scraper in self.scrapers:
            scraper_name = type(scraper).__name__
            print(f"\nRunning {scraper_name}...")

            try:
                events = scraper.scrape()
                print(f"  Found {len(events)} potential events")
                all_events.extend(events)

                # Save source statuses
                if hasattr(scraper, 'source_statuses'):
                    for status in scraper.source_statuses:
                        self.db.save_source_status(
                            source_name=status['source_name'],
                            source_type=status['source_type'],
                            status=status['status'],
                            error_message=status.get('error_message'),
                            events_found=status.get('events_found', 0)
                        )
            except Exception as e:
                print(f"  Error: {e}")

        # Filter for new events (not seen before) and recent events only.
        # Three dedup channels, all persistent across runs via SQLite:
        #   1. URL-based — catches scraping the same URL twice
        #   2. Title-based — catches syndicated press releases that appear
        #      at different URLs (e.g. Globe Newswire + Financial Post both
        #      publishing the same release); SEC templated titles exempt
        #   3. Job-posting key (account_key(company) + normalized job title)
        #      — collapses one Adzuna posting fanned out across N states
        old_events_skipped = 0
        title_dupes_skipped = 0
        seen_titles_this_run: set = set()
        seen_job_keys_this_run: set = set()

        for event in all_events:
            # Check if event is too old
            event_date = event.published_date
            if event_date.tzinfo is None:
                event_date = event_date.replace(tzinfo=timezone.utc)

            if event_date < cutoff_date:
                old_events_skipped += 1
                continue

            # URL dedup (persistent across runs)
            if self.db.has_seen_url(event.url):
                continue

            title_key = (event.title or '').strip().lower()
            is_job_posting = (event.source == EventSource.ADZUNA
                              or ' hiring: ' in title_key)

            if is_job_posting:
                # Job postings (Adzuna) dedup on (account_key(company),
                # normalized job title) — NOT on the raw title, and NOT
                # exempt. Adzuna lists one national posting under every
                # territory state with a different redirect_url each, so URL
                # dedup lets 5 copies through; this collapses them to ONE.
                # Persistent across runs via the dedup_keys table.
                job_key = _job_posting_dedup_key(event)
                if job_key in seen_job_keys_this_run:
                    title_dupes_skipped += 1
                    continue
                if self.db.has_recent_dedup_key(job_key, hours=max_age_hours):
                    title_dupes_skipped += 1
                    continue
                seen_job_keys_this_run.add(job_key)
                self.db.mark_dedup_key(job_key)
            else:
                # Title dedup catches syndicated press releases (same real
                # headline republished at different URLs). It must SKIP the
                # machine-templated SEC titles "SEC 8-K Item X — Company":
                # a company filing multiple 8-Ks of the same item type, weeks
                # apart, produces identical titles that are DISTINCT filings,
                # deduped correctly by URL above. Applying title-dedup to
                # them silently drops legitimate filings.
                is_templated = title_key.startswith('sec 8-k')
                if title_key and not is_templated:
                    if title_key in seen_titles_this_run:
                        title_dupes_skipped += 1
                        continue
                    if self.db.has_recent_event_title(title_key, hours=max_age_hours):
                        title_dupes_skipped += 1
                        continue
                    seen_titles_this_run.add(title_key)

            new_events.append(event)
            self.db.mark_url_seen(event.url)
            self.db.save_event(event)

        print(f"\n{'-'*40}")
        print(f"Total potential events: {len(all_events)}")
        print(f"Skipped (older than {max_age_hours}h): {old_events_skipped}")
        print(f"Skipped (duplicate titles / duplicate job postings): {title_dupes_skipped}")
        print(f"New events (not seen before): {len(new_events)}")

        # Send alerts for all new events (no company verification)
        if new_events:
            print(f"\nSending alerts for {len(new_events)} new events...")
            self._send_alerts(new_events)

            # Print summary
            self._print_event_summary(new_events)
        else:
            print("\nNo new events this cycle.")

        # Supabase sync happens in the GitHub Actions workflow step
        # (supabase_sync.py), never from the scrape cycle.
        return new_events

    def _send_alerts(self, events: List[TriggerEvent]):
        """Send alerts and update database."""
        handlers_used = self.alert_manager.send_alerts(events)
        print(f"Alerts sent via {handlers_used} handler(s)")

        # Mark alerts as sent
        for event in events:
            event.alert_sent = True
            self.db.mark_alert_sent(event.id)

    def _print_event_summary(self, events: List[TriggerEvent]):
        """Print a summary of discovered events."""
        print(f"\n{'='*60}")
        print("EVENT SUMMARY (Most Recent First)")
        print(f"{'='*60}")

        # Sort by published date (most recent first), then by relevance
        sorted_events = sorted(events, key=lambda e: (e.published_date, e.relevance_score), reverse=True)

        for i, event in enumerate(sorted_events[:10], 1):  # Top 10
            # Calculate how recent the article is
            now = datetime.now(timezone.utc)
            event_date = event.published_date
            if event_date.tzinfo is None:
                event_date = event_date.replace(tzinfo=timezone.utc)
            age = now - event_date

            if age.total_seconds() < 3600:
                age_str = f"{int(age.total_seconds() / 60)} min ago"
            elif age.total_seconds() < 86400:
                age_str = f"{int(age.total_seconds() / 3600)} hours ago"
            else:
                age_str = f"{int(age.days)} days ago"

            print(f"\n{i}. [{event.event_type.value.upper()}] {event.title[:60]}...")
            print(f"   Published: {event.published_date.strftime('%Y-%m-%d %H:%M')} ({age_str})")
            print(f"   Company: {event.company_name or 'Unknown'}")
            if event.company_employees:
                print(f"   Employees: {event.company_employees}")
            if event.company_revenue:
                print(f"   Revenue: {event.company_revenue}")
            if event.company_industry:
                print(f"   Industry: {event.company_industry}")
            print(f"   Source: {event.source_name or event.source.value}")
            print(f"   Relevance: {event.relevance_score:.0f}%")
            print(f"   URL: {event.url}")

        if len(events) > 10:
            print(f"\n... and {len(events) - 10} more events (see alerts folder)")

    def run_daemon(self):
        """Run continuously as a daemon."""
        check_interval = self.config.get('scraper', {}).get('check_interval', 30)

        print(f"Starting daemon mode (checking every {check_interval} minutes)")
        print("Press Ctrl+C to stop")

        # Set up signal handler for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        while self.running:
            try:
                self.run_once()

                if self.running:
                    print(f"\nNext check in {check_interval} minutes...")
                    # Sleep in small increments to allow for quick shutdown
                    for _ in range(check_interval * 60):
                        if not self.running:
                            break
                        time.sleep(1)

            except Exception as e:
                print(f"Error in daemon cycle: {e}")
                if self.running:
                    print("Retrying in 5 minutes...")
                    time.sleep(300)

        print("\nDaemon stopped gracefully.")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        print("\nReceived shutdown signal...")
        self.running = False

    def show_stats(self):
        """Display database statistics."""
        stats = self.db.get_stats()

        print(f"\n{'='*60}")
        print("TRIGGER EVENT STATISTICS")
        print(f"{'='*60}")
        print(f"\nTotal events tracked: {stats['total_events']}")
        print(f"Total URLs seen: {stats['total_urls_seen']}")
        print(f"Events in last 24 hours: {stats['events_last_24h']}")

        print("\nEvents by type:")
        for event_type, count in stats.get('events_by_type', {}).items():
            print(f"  - {event_type}: {count}")

        # Show recent events
        recent = self.db.get_recent_events(hours=24)
        if recent:
            print(f"\nRecent events (last 24 hours):")
            for event in recent[:5]:
                print(f"  - [{event.event_type.value}] {event.title[:50]}...")

    def cleanup(self, days: int = 30):
        """Clean up old database entries."""
        print(f"Cleaning up entries older than {days} days...")
        self.db.cleanup_old_entries(days)
        print("Cleanup complete.")


def main():
    parser = argparse.ArgumentParser(
        description='Sales Territory Trigger Event Monitor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main                    # Run once
  python -m src.main --daemon           # Run continuously
  python -m src.main --stats            # Show statistics
  python -m src.main --config my.yaml   # Use custom config
  python -m src.main --cleanup 60       # Clean entries older than 60 days
        """
    )

    parser.add_argument(
        '--config', '-c',
        default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )
    parser.add_argument(
        '--daemon', '-d',
        action='store_true',
        help='Run continuously as a daemon'
    )
    parser.add_argument(
        '--stats', '-s',
        action='store_true',
        help='Show statistics and exit'
    )
    parser.add_argument(
        '--cleanup',
        type=int,
        metavar='DAYS',
        help='Clean up entries older than DAYS'
    )

    args = parser.parse_args()

    # Initialize monitor
    monitor = TriggerEventMonitor(args.config)

    if args.stats:
        monitor.show_stats()
    elif args.cleanup:
        monitor.cleanup(args.cleanup)
    elif args.daemon:
        monitor.run_daemon()
    else:
        monitor.run_once()


if __name__ == '__main__':
    main()
