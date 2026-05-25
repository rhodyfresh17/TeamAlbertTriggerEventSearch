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
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import yaml

from .models import TriggerEvent
from .database import DatabaseManager
from .alerts import AlertManager
from .scrapers import RSSScraper, GoogleNewsScraper, JobScraper, BingNewsScraper, FinSMEsScraper, SECScraper
from .enrichment import CompanyEnricher

# Import Supabase sync for cloud dashboard
try:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from supabase_sync import sync_to_supabase
    SUPABASE_SYNC_AVAILABLE = True
except ImportError:
    SUPABASE_SYNC_AVAILABLE = False


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

        # Filter for new events (not seen before) and recent events only
        old_events_skipped = 0
        for event in all_events:
            # Check if event is too old
            event_date = event.published_date
            if event_date.tzinfo is None:
                event_date = event_date.replace(tzinfo=timezone.utc)

            if event_date < cutoff_date:
                old_events_skipped += 1
                continue

            if not self.db.has_seen_url(event.url):
                new_events.append(event)
                self.db.mark_url_seen(event.url)
                self.db.save_event(event)

        print(f"\n{'-'*40}")
        print(f"Total potential events: {len(all_events)}")
        print(f"Skipped (older than {max_age_hours}h): {old_events_skipped}")
        print(f"New events (not seen before): {len(new_events)}")

        # Send alerts for all new events (no company verification)
        if new_events:
            print(f"\nSending alerts for {len(new_events)} new events...")
            self._send_alerts(new_events)

            # Print summary
            self._print_event_summary(new_events)
        else:
            print("\nNo new events this cycle.")

        # Sync to Supabase for cloud dashboard
        if SUPABASE_SYNC_AVAILABLE:
            print("\nSyncing to Supabase for cloud dashboard...")
            try:
                sync_to_supabase()
            except Exception as e:
                print(f"Supabase sync failed (dashboard may be stale): {e}")
        else:
            print("\nNote: Supabase sync not available. Run 'pip install supabase' for cloud dashboard.")

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
