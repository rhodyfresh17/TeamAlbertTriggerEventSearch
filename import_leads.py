#!/usr/bin/env python3
"""
Import leads from external sources into the trigger events database.

Usage:
    # From command line with JSON file:
    python import_leads.py leads.json

    # From command line with JSON string:
    python import_leads.py --json '{"title": "Company hires CFO", "company_name": "Acme Corp", ...}'

    # From stdin (pipe from another script):
    echo '{"title": "..."}' | python import_leads.py --stdin

    # As a module from clawdbot:
    from import_leads import import_lead, import_leads_batch
    import_lead({"title": "...", "company_name": "...", ...})
"""

import argparse
import json
import os
import sqlite3
import sys
import hashlib
from datetime import datetime
from pathlib import Path


DB_PATH = "trigger_events.db"


def get_connection():
    """Get database connection and ensure schema exists."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create tables if they don't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            url TEXT NOT NULL,
            published_date TEXT NOT NULL,
            discovered_date TEXT NOT NULL,
            company_name TEXT,
            company_location TEXT,
            description TEXT,
            person_name TEXT,
            person_title TEXT,
            acquirer TEXT,
            target TEXT,
            deal_value TEXT,
            matched_keywords TEXT,
            matched_regions TEXT,
            relevance_score REAL,
            alert_sent INTEGER DEFAULT 0,
            lead_status TEXT DEFAULT 'new',
            notes TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS seen_urls (
            url_hash TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            first_seen TEXT NOT NULL
        )
    ''')

    conn.commit()
    return conn


def generate_id(url: str, title: str) -> str:
    """Generate unique ID for a lead."""
    content = f"{url}:{title}"
    return hashlib.md5(content.encode()).hexdigest()


def import_lead(lead: dict, source: str = "clawdbot") -> bool:
    """
    Import a single lead into the database.

    Args:
        lead: Dictionary with lead data. Required fields:
            - title: str
            - company_name: str (optional but recommended)
            - url: str (optional, will generate one if missing)

            Optional fields:
            - event_type: str (default: "other")
            - description: str
            - company_location: str
            - person_name: str
            - person_title: str
            - relevance_score: float (default: 50.0)
            - published_date: str (ISO format, default: now)
            - lead_status: str (default: "new")
            - notes: str

        source: Source identifier (default: "clawdbot")

    Returns:
        True if imported successfully, False if duplicate or error
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Required field
        title = lead.get("title", "").strip()
        if not title:
            print("Error: 'title' is required")
            return False

        # Generate URL if not provided
        url = lead.get("url", "")
        if not url:
            url = f"https://clawdbot.local/{hashlib.md5(title.encode()).hexdigest()[:8]}"

        # Check if already exists
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cursor.execute('SELECT 1 FROM seen_urls WHERE url_hash = ?', (url_hash,))
        if cursor.fetchone():
            print(f"Skipping duplicate: {title[:50]}...")
            return False

        # Generate ID
        lead_id = generate_id(url, title)

        # Parse dates
        now = datetime.now().isoformat()
        published_date = lead.get("published_date", now)
        discovered_date = lead.get("discovered_date", now)

        # Determine event type
        event_type = lead.get("event_type", "other")
        title_lower = title.lower()
        if not lead.get("event_type"):
            if "cfo" in title_lower or "chief financial" in title_lower:
                event_type = "cfo_hire"
            elif "acqui" in title_lower or "merger" in title_lower or "buyout" in title_lower:
                event_type = "merger_acquisition"
            elif "funding" in title_lower or "raises" in title_lower or "series" in title_lower:
                event_type = "funding"
            elif "hire" in title_lower or "appoint" in title_lower or "named" in title_lower:
                event_type = "executive_hire"

        # Insert into events table
        cursor.execute('''
            INSERT OR REPLACE INTO events (
                id, title, event_type, source, url, published_date, discovered_date,
                company_name, company_location, description, person_name, person_title,
                acquirer, target, deal_value, matched_keywords, matched_regions,
                relevance_score, alert_sent, lead_status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            lead_id,
            title,
            event_type,
            source,
            url,
            published_date,
            discovered_date,
            lead.get("company_name"),
            lead.get("company_location"),
            lead.get("description"),
            lead.get("person_name"),
            lead.get("person_title"),
            lead.get("acquirer"),
            lead.get("target"),
            lead.get("deal_value"),
            json.dumps(lead.get("matched_keywords", [])),
            json.dumps(lead.get("matched_regions", [])),
            lead.get("relevance_score", 50.0),
            0,  # alert_sent
            lead.get("lead_status", "new"),
            lead.get("notes", "")
        ))

        # Mark URL as seen
        cursor.execute('''
            INSERT OR IGNORE INTO seen_urls (url_hash, url, first_seen)
            VALUES (?, ?, ?)
        ''', (url_hash, url, now))

        conn.commit()
        conn.close()

        print(f"Imported: [{event_type}] {title[:60]}...")
        return True

    except Exception as e:
        print(f"Error importing lead: {e}")
        return False


def import_leads_batch(leads: list, source: str = "clawdbot") -> dict:
    """
    Import multiple leads at once.

    Args:
        leads: List of lead dictionaries
        source: Source identifier

    Returns:
        Dictionary with import statistics
    """
    stats = {"total": len(leads), "imported": 0, "skipped": 0, "errors": 0}

    for lead in leads:
        try:
            if import_lead(lead, source):
                stats["imported"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"Error: {e}")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Import leads into the trigger events database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python import_leads.py leads.json
  python import_leads.py leads.json --sync          # Import and sync to S3
  python import_leads.py --json '{"title": "Acme Corp hires new CFO", "company_name": "Acme Corp"}'
  echo '{"title": "..."}' | python import_leads.py --stdin
  cat clawdbot_output.json | python import_leads.py --stdin --sync
        """
    )

    parser.add_argument("file", nargs="?", help="JSON file with leads to import")
    parser.add_argument("--json", "-j", help="JSON string with single lead or array of leads")
    parser.add_argument("--stdin", "-s", action="store_true", help="Read JSON from stdin")
    parser.add_argument("--source", default="clawdbot", help="Source identifier (default: clawdbot)")
    parser.add_argument("--db", default=DB_PATH, help=f"Database path (default: {DB_PATH})")
    parser.add_argument("--sync", action="store_true", help="Sync database to S3 after import")
    parser.add_argument("--bucket", default=None, help="S3 bucket for sync (default: SYNC_BUCKET env var)")

    args = parser.parse_args()

    # Update DB path if custom path provided
    if args.db != DB_PATH:
        globals()['DB_PATH'] = args.db

    leads = []

    # Read from file
    if args.file:
        with open(args.file, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                leads = data
            else:
                leads = [data]

    # Read from JSON argument
    elif args.json:
        data = json.loads(args.json)
        if isinstance(data, list):
            leads = data
        else:
            leads = [data]

    # Read from stdin (supports JSON array or newline-delimited JSON)
    elif args.stdin or not sys.stdin.isatty():
        content = sys.stdin.read().strip()
        try:
            # Try parsing as a single JSON array or object
            data = json.loads(content)
            if isinstance(data, list):
                leads = data
            else:
                leads = [data]
        except json.JSONDecodeError:
            # Try parsing as newline-delimited JSON (NDJSON)
            leads = []
            for line in content.split('\n'):
                line = line.strip()
                if line and line.startswith('{'):
                    try:
                        leads.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            # Also try parsing multiple JSON objects without newlines
            if not leads:
                import re
                objects = re.findall(r'\{[^{}]*\}', content)
                for obj_str in objects:
                    try:
                        leads.append(json.loads(obj_str))
                    except json.JSONDecodeError:
                        pass

    else:
        parser.print_help()
        return

    # Import leads
    if leads:
        stats = import_leads_batch(leads, args.source)
        print(f"\nImport complete: {stats['imported']} imported, {stats['skipped']} skipped, {stats['errors']} errors")

        # Auto-sync to S3 if requested
        if args.sync and stats['imported'] > 0:
            print("\nSyncing to S3...")
            try:
                from sync_db import push_to_s3
                bucket = args.bucket or os.environ.get("SYNC_BUCKET", "trigger-events-sync")
                if push_to_s3(bucket):
                    print("Sync complete!")
                else:
                    print("Sync failed - check AWS credentials")
            except ImportError:
                print("Error: sync_db.py not found. Run sync manually.")


if __name__ == "__main__":
    main()
