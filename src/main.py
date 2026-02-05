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
from datetime import datetime
from pathlib import Path
from typing import List

import yaml

from .models import TriggerEvent
from .database import DatabaseManager
from .alerts import AlertManager
from .scrapers import RSSScraper, GoogleNewsScraper, JobScraper
from .enrichment import CompanyEnricher


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

        # Run all scrapers
        for scraper in self.scrapers:
            scraper_name = type(scraper).__name__
            print(f"\nRunning {scraper_name}...")

            try:
                events = scraper.scrape()
                print(f"  Found {len(events)} potential events")
                all_events.extend(events)
            except Exception as e:
                print(f"  Error: {e}")

        # Filter for new events (not seen before)
        for event in all_events:
            if not self.db.has_seen_url(event.url):
                # Apply learned feedback adjustment to relevance score
                adjustment = self.db.get_learned_adjustment(event)
                if adjustment != 0:
                    event.relevance_score = max(0, min(100, event.relevance_score + adjustment))

                new_events.append(event)
                self.db.mark_url_seen(event.url)
                self.db.save_event(event)

        print(f"\n{'-'*40}")
        print(f"Total potential events: {len(all_events)}")
        print(f"New events (not seen before): {len(new_events)}")

        # Enrich new events with company data (only high-relevance to conserve API credits)
        # Free tier limit: ~100 credits/month, so only enrich score > 80
        ENRICHMENT_THRESHOLD = 80
        if new_events and self.enricher.enabled:
            high_relevance = [e for e in new_events if e.relevance_score >= ENRICHMENT_THRESHOLD and e.company_name]
            print(f"\nEnriching {len(high_relevance)} high-relevance events (score >= {ENRICHMENT_THRESHOLD}) via {self.enricher.provider}...")
            for event in high_relevance:
                info = self.enricher.enrich(event.company_name)
                if info:
                    event.company_website = info.website
                    event.company_revenue = info.revenue or info.revenue_range
                    event.company_employees = str(info.employee_count) if info.employee_count else info.employee_range
                    event.company_industry = info.industry
                    event.company_linkedin = info.linkedin_url
                    print(f"  Enriched: {event.company_name}")

        # Send alerts for new events
        if new_events:
            print(f"\nSending alerts for {len(new_events)} new events...")
            self._send_alerts(new_events)

            # Print summary
            self._print_event_summary(new_events)
        else:
            print("\nNo new events found this cycle.")

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
        print("EVENT SUMMARY")
        print(f"{'='*60}")

        # Sort by relevance
        sorted_events = sorted(events, key=lambda e: e.relevance_score, reverse=True)

        for i, event in enumerate(sorted_events[:10], 1):  # Top 10
            print(f"\n{i}. [{event.event_type.value.upper()}] {event.title[:60]}...")
            print(f"   Company: {event.company_name or 'Unknown'}")
            print(f"   Source: {event.source.value}")
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

        # Show feedback stats
        feedback_stats = self.db.get_feedback_stats()
        if feedback_stats['total_positive'] > 0 or feedback_stats['total_negative'] > 0:
            print(f"\nFeedback statistics:")
            print(f"  Positive ratings: {feedback_stats['total_positive']}")
            print(f"  Negative ratings: {feedback_stats['total_negative']}")
            print(f"  Learned patterns: {feedback_stats['total_patterns']}")

            if feedback_stats['top_positive_patterns']:
                print(f"\n  Top relevant patterns:")
                for p in feedback_stats['top_positive_patterns'][:3]:
                    print(f"    + {p[0]}: {p[1]} (adj: {p[2]:+.1f})")

            if feedback_stats['top_negative_patterns']:
                print(f"\n  Top irrelevant patterns:")
                for p in feedback_stats['top_negative_patterns'][:3]:
                    print(f"    - {p[0]}: {p[1]} (adj: {p[2]:+.1f})")

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

    def rate_event(self, event_id: str, rating: str):
        """Rate a specific event by ID.

        Args:
            event_id: The event ID (or partial ID)
            rating: 'good', 'bad', '+', '-', '1', or '-1'
        """
        # Normalize rating
        if rating.lower() in ('good', '+', '1', 'relevant', 'yes', 'y'):
            rating_value = 1
            rating_label = "RELEVANT"
        elif rating.lower() in ('bad', '-', '-1', 'irrelevant', 'no', 'n'):
            rating_value = -1
            rating_label = "NOT RELEVANT"
        else:
            print(f"Invalid rating: {rating}")
            print("Use: good/bad, +/-, 1/-1, yes/no")
            return

        # Find event (support partial ID matching)
        event = self.db.get_event_by_id(event_id)
        if not event:
            # Try partial match
            recent = self.db.get_recent_events(hours=168)  # Last week
            matches = [e for e in recent if e.id.startswith(event_id)]
            if len(matches) == 1:
                event = matches[0]
            elif len(matches) > 1:
                print(f"Multiple events match '{event_id}':")
                for e in matches[:5]:
                    print(f"  {e.id[:8]} - {e.title[:50]}...")
                return
            else:
                print(f"Event not found: {event_id}")
                return

        # Save feedback
        self.db.save_feedback(event.id, rating_value, event)
        print(f"Rated event as {rating_label}:")
        print(f"  ID: {event.id[:8]}")
        print(f"  Title: {event.title[:60]}")
        print(f"  Company: {event.company_name or 'Unknown'}")

    def feedback_interactive(self):
        """Interactive feedback mode - rate recent unrated events."""
        events = self.db.get_unrated_events(limit=20)

        if not events:
            print("No unrated events found.")
            return

        print(f"\n{'='*60}")
        print("INTERACTIVE FEEDBACK MODE")
        print(f"{'='*60}")
        print(f"\nFound {len(events)} unrated events.")
        print("For each event, enter: g=good, b=bad, s=skip, q=quit\n")

        rated = 0
        for i, event in enumerate(events, 1):
            print(f"\n[{i}/{len(events)}] {event.event_type.value.upper()}")
            print(f"Title: {event.title[:70]}...")
            print(f"Company: {event.company_name or 'Unknown'}")
            print(f"Location: {', '.join(event.matched_regions) if event.matched_regions else 'Unknown'}")
            print(f"Keywords: {', '.join(event.matched_keywords[:5]) if event.matched_keywords else 'None'}")
            print(f"Score: {event.relevance_score:.0f}% | Source: {event.source_name or event.source.value}")
            print(f"URL: {event.url}")

            while True:
                try:
                    response = input("\nRating [g/b/s/q]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("\nExiting feedback mode.")
                    return

                if response in ('g', 'good', '+', '1', 'y', 'yes'):
                    self.db.save_feedback(event.id, 1, event)
                    print("  -> Marked as RELEVANT")
                    rated += 1
                    break
                elif response in ('b', 'bad', '-', '-1', 'n', 'no'):
                    self.db.save_feedback(event.id, -1, event)
                    print("  -> Marked as NOT RELEVANT")
                    rated += 1
                    break
                elif response in ('s', 'skip', ''):
                    print("  -> Skipped")
                    break
                elif response in ('q', 'quit', 'exit'):
                    print(f"\nFeedback session complete. Rated {rated} events.")
                    return
                else:
                    print("Invalid input. Use: g=good, b=bad, s=skip, q=quit")

        print(f"\nFeedback session complete. Rated {rated} events.")


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
  python -m src.main --feedback         # Interactive feedback mode
  python -m src.main --rate abc123 good # Rate a specific event
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
    parser.add_argument(
        '--feedback', '-f',
        action='store_true',
        help='Interactive feedback mode to rate recent events'
    )
    parser.add_argument(
        '--rate', '-r',
        nargs=2,
        metavar=('EVENT_ID', 'RATING'),
        help='Rate a specific event (e.g., --rate abc123 good)'
    )

    args = parser.parse_args()

    # Initialize monitor
    monitor = TriggerEventMonitor(args.config)

    if args.stats:
        monitor.show_stats()
    elif args.cleanup:
        monitor.cleanup(args.cleanup)
    elif args.feedback:
        monitor.feedback_interactive()
    elif args.rate:
        monitor.rate_event(args.rate[0], args.rate[1])
    elif args.daemon:
        monitor.run_daemon()
    else:
        monitor.run_once()


if __name__ == '__main__':
    main()
