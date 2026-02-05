#!/usr/bin/env python3
"""
Feed Health Checker

Validates that all configured RSS feeds are accessible and returning valid content.
Run this periodically to ensure your data sources are still working.

Usage:
    python scripts/check_feeds.py                    # Check all feeds
    python scripts/check_feeds.py --verbose          # Show detailed output
    python scripts/check_feeds.py --config my.yaml   # Use custom config
"""

import argparse
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple
from dataclasses import dataclass
from datetime import datetime

import requests
import yaml


@dataclass
class FeedStatus:
    """Status of a single feed check."""
    name: str
    url: str
    status: str  # 'ok', 'error', 'warning'
    message: str
    response_time_ms: float
    item_count: int = 0


class FeedHealthChecker:
    """Check health of RSS feeds."""

    def __init__(self, config_path: str = "config.example.yaml"):
        self.config = self._load_config(config_path)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; FeedHealthChecker/1.0)'
        })
        self.timeout = 15

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from YAML file."""
        config_file = Path(config_path)
        if not config_file.exists():
            print(f"Error: Config file not found: {config_path}")
            sys.exit(1)

        with open(config_file) as f:
            return yaml.safe_load(f)

    def check_feed(self, name: str, url: str) -> FeedStatus:
        """Check a single RSS feed."""
        start_time = time.time()

        try:
            response = self.session.get(url, timeout=self.timeout)
            response_time = (time.time() - start_time) * 1000

            if response.status_code != 200:
                return FeedStatus(
                    name=name,
                    url=url,
                    status='error',
                    message=f"HTTP {response.status_code}",
                    response_time_ms=response_time
                )

            # Try to parse as XML
            try:
                root = ET.fromstring(response.content)

                # Count items (RSS uses 'item', Atom uses 'entry')
                items = root.findall('.//item')
                if not items:
                    items = root.findall('.//{http://www.w3.org/2005/Atom}entry')

                item_count = len(items)

                if item_count == 0:
                    return FeedStatus(
                        name=name,
                        url=url,
                        status='warning',
                        message="No items found in feed",
                        response_time_ms=response_time,
                        item_count=0
                    )

                return FeedStatus(
                    name=name,
                    url=url,
                    status='ok',
                    message=f"{item_count} items",
                    response_time_ms=response_time,
                    item_count=item_count
                )

            except ET.ParseError as e:
                return FeedStatus(
                    name=name,
                    url=url,
                    status='error',
                    message=f"Invalid XML: {str(e)[:50]}",
                    response_time_ms=response_time
                )

        except requests.Timeout:
            return FeedStatus(
                name=name,
                url=url,
                status='error',
                message="Timeout",
                response_time_ms=self.timeout * 1000
            )
        except requests.RequestException as e:
            return FeedStatus(
                name=name,
                url=url,
                status='error',
                message=str(e)[:50],
                response_time_ms=0
            )

    def check_all_feeds(self, verbose: bool = False) -> List[FeedStatus]:
        """Check all configured feeds."""
        feeds = self.config.get('sources', {}).get('rss_feeds', [])
        results = []

        print(f"\nChecking {len(feeds)} RSS feeds...")
        print("=" * 70)

        for i, feed in enumerate(feeds, 1):
            name = feed.get('name', 'Unknown')
            url = feed.get('url', '')
            enabled = feed.get('enabled', True)

            if not enabled:
                if verbose:
                    print(f"[{i:2d}] {name}: SKIPPED (disabled)")
                continue

            if verbose:
                print(f"[{i:2d}] Checking {name}...", end=" ", flush=True)

            status = self.check_feed(name, url)
            results.append(status)

            if verbose:
                icon = "✓" if status.status == 'ok' else "⚠" if status.status == 'warning' else "✗"
                print(f"{icon} {status.message} ({status.response_time_ms:.0f}ms)")

            # Small delay between requests
            time.sleep(0.5)

        return results

    def print_summary(self, results: List[FeedStatus]):
        """Print summary of feed health check."""
        ok_count = sum(1 for r in results if r.status == 'ok')
        warning_count = sum(1 for r in results if r.status == 'warning')
        error_count = sum(1 for r in results if r.status == 'error')

        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"  Total feeds checked: {len(results)}")
        print(f"  ✓ Healthy:  {ok_count}")
        print(f"  ⚠ Warning:  {warning_count}")
        print(f"  ✗ Errors:   {error_count}")

        if error_count > 0 or warning_count > 0:
            print("\n" + "-" * 70)
            print("ISSUES FOUND:")
            print("-" * 70)

            for r in results:
                if r.status != 'ok':
                    icon = "⚠" if r.status == 'warning' else "✗"
                    print(f"  {icon} {r.name}")
                    print(f"    {r.message}")
                    print(f"    URL: {r.url}")
                    print()

        # Performance stats
        if results:
            response_times = [r.response_time_ms for r in results if r.response_time_ms > 0]
            if response_times:
                avg_time = sum(response_times) / len(response_times)
                max_time = max(response_times)
                print("-" * 70)
                print("PERFORMANCE:")
                print("-" * 70)
                print(f"  Average response time: {avg_time:.0f}ms")
                print(f"  Slowest feed: {max_time:.0f}ms")

                # Find slowest feeds
                slow_feeds = [r for r in results if r.response_time_ms > 5000]
                if slow_feeds:
                    print("\n  Slow feeds (>5s):")
                    for r in slow_feeds:
                        print(f"    - {r.name}: {r.response_time_ms:.0f}ms")

        print("\n" + "=" * 70)
        print(f"Check completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        return error_count == 0

    def export_results(self, results: List[FeedStatus], output_file: str):
        """Export results to a file."""
        with open(output_file, 'w') as f:
            f.write(f"Feed Health Check - {datetime.now().isoformat()}\n")
            f.write("=" * 70 + "\n\n")

            for r in results:
                status_icon = "OK" if r.status == 'ok' else "WARN" if r.status == 'warning' else "ERROR"
                f.write(f"[{status_icon}] {r.name}\n")
                f.write(f"  URL: {r.url}\n")
                f.write(f"  Status: {r.message}\n")
                f.write(f"  Response: {r.response_time_ms:.0f}ms\n")
                if r.item_count > 0:
                    f.write(f"  Items: {r.item_count}\n")
                f.write("\n")

        print(f"\nResults exported to: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Check health of configured RSS feeds',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        '--config', '-c',
        default='config.example.yaml',
        help='Path to configuration file (default: config.example.yaml)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed output for each feed'
    )
    parser.add_argument(
        '--export', '-e',
        metavar='FILE',
        help='Export results to a file'
    )

    args = parser.parse_args()

    checker = FeedHealthChecker(args.config)
    results = checker.check_all_feeds(verbose=args.verbose)

    if args.export:
        checker.export_results(results, args.export)

    success = checker.print_summary(results)

    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
