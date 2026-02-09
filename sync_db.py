#!/usr/bin/env python3
"""
Sync trigger_events.db to/from S3 for sharing between EC2 and local.

Setup:
    1. Create S3 bucket: aws s3 mb s3://your-bucket-name
    2. Set environment variable: export SYNC_BUCKET=your-bucket-name
    3. Ensure AWS credentials are configured (aws configure)

Usage:
    # Push local database to S3
    python sync_db.py push

    # Pull database from S3 to local
    python sync_db.py pull

    # Auto-sync (pull, merge, push) - best for bidirectional sync
    python sync_db.py sync
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


DB_PATH = "trigger_events.db"
DEFAULT_BUCKET = os.environ.get("SYNC_BUCKET", "trigger-events-sync")
S3_KEY = "trigger_events.db"


def run_aws_command(args: list) -> tuple[bool, str]:
    """Run an AWS CLI command and return success status and output."""
    try:
        result = subprocess.run(
            ["aws"] + args,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr
    except FileNotFoundError:
        return False, "AWS CLI not installed. Run: pip install awscli && aws configure"
    except subprocess.TimeoutExpired:
        return False, "AWS command timed out"
    except Exception as e:
        return False, str(e)


def push_to_s3(bucket: str = DEFAULT_BUCKET) -> bool:
    """Push local database to S3."""
    if not Path(DB_PATH).exists():
        print(f"Error: Local database {DB_PATH} not found")
        return False

    s3_path = f"s3://{bucket}/{S3_KEY}"
    print(f"Pushing {DB_PATH} to {s3_path}...")

    success, output = run_aws_command(["s3", "cp", DB_PATH, s3_path])
    if success:
        print(f"Successfully pushed to S3")
        return True
    else:
        print(f"Error pushing to S3: {output}")
        return False


def pull_from_s3(bucket: str = DEFAULT_BUCKET) -> bool:
    """Pull database from S3 to local."""
    s3_path = f"s3://{bucket}/{S3_KEY}"
    print(f"Pulling {s3_path} to {DB_PATH}...")

    success, output = run_aws_command(["s3", "cp", s3_path, DB_PATH])
    if success:
        print(f"Successfully pulled from S3")
        return True
    else:
        print(f"Error pulling from S3: {output}")
        return False


def merge_databases(local_db: str, remote_db: str) -> int:
    """Merge remote database into local, keeping all unique records."""
    if not Path(local_db).exists():
        # No local DB, just use remote
        return 0

    if not Path(remote_db).exists():
        print("No remote database to merge")
        return 0

    conn_local = sqlite3.connect(local_db)
    conn_remote = sqlite3.connect(remote_db)

    cursor_local = conn_local.cursor()
    cursor_remote = conn_remote.cursor()

    merged_count = 0

    try:
        # Get all events from remote
        cursor_remote.execute("SELECT * FROM events")
        remote_events = cursor_remote.fetchall()

        # Get column names
        cursor_remote.execute("PRAGMA table_info(events)")
        columns = [col[1] for col in cursor_remote.fetchall()]

        # Insert remote events that don't exist locally
        for event in remote_events:
            event_id = event[0]  # id is first column
            cursor_local.execute("SELECT 1 FROM events WHERE id = ?", (event_id,))
            if not cursor_local.fetchone():
                placeholders = ",".join(["?" for _ in columns])
                cursor_local.execute(
                    f"INSERT INTO events ({','.join(columns)}) VALUES ({placeholders})",
                    event
                )
                merged_count += 1

        # Merge seen_urls
        cursor_remote.execute("SELECT * FROM seen_urls")
        remote_urls = cursor_remote.fetchall()

        for url_row in remote_urls:
            url_hash = url_row[0]
            cursor_local.execute("SELECT 1 FROM seen_urls WHERE url_hash = ?", (url_hash,))
            if not cursor_local.fetchone():
                cursor_local.execute(
                    "INSERT INTO seen_urls (url_hash, url, first_seen) VALUES (?, ?, ?)",
                    url_row
                )

        conn_local.commit()

    finally:
        conn_local.close()
        conn_remote.close()

    return merged_count


def sync_databases(bucket: str = DEFAULT_BUCKET) -> bool:
    """
    Bidirectional sync: pull from S3, merge with local, push back.
    This preserves changes from both EC2 and local.
    """
    print("Starting bidirectional sync...")

    # Download remote to temp file
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        remote_tmp = tmp.name

    s3_path = f"s3://{bucket}/{S3_KEY}"
    success, _ = run_aws_command(["s3", "cp", s3_path, remote_tmp])

    if success:
        # Merge remote into local
        merged = merge_databases(DB_PATH, remote_tmp)
        print(f"Merged {merged} new events from S3")

        # Clean up temp file
        Path(remote_tmp).unlink(missing_ok=True)
    else:
        print("No remote database found, will create new one")
        Path(remote_tmp).unlink(missing_ok=True)

    # Push merged database back to S3
    if Path(DB_PATH).exists():
        return push_to_s3(bucket)
    else:
        print("No local database to push")
        return False


def show_status(bucket: str = DEFAULT_BUCKET) -> None:
    """Show sync status and database info."""
    print(f"Sync bucket: {bucket}")
    print(f"Local database: {DB_PATH}")
    print()

    # Local status
    if Path(DB_PATH).exists():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events")
        local_count = cursor.fetchone()[0]
        cursor.execute("SELECT MAX(discovered_date) FROM events")
        latest = cursor.fetchone()[0]
        conn.close()
        print(f"Local: {local_count} events, latest: {latest}")
    else:
        print("Local: No database")

    # Remote status
    s3_path = f"s3://{bucket}/{S3_KEY}"
    success, output = run_aws_command(["s3", "ls", s3_path])
    if success:
        print(f"Remote: {output.strip()}")
    else:
        print("Remote: No database in S3")


def main():
    parser = argparse.ArgumentParser(
        description="Sync trigger events database to/from S3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sync_db.py push              # Upload local DB to S3
  python sync_db.py pull              # Download DB from S3
  python sync_db.py sync              # Bidirectional merge sync
  python sync_db.py status            # Show sync status

  SYNC_BUCKET=my-bucket python sync_db.py push  # Use custom bucket
        """
    )

    parser.add_argument(
        "action",
        choices=["push", "pull", "sync", "status"],
        help="Sync action to perform"
    )
    parser.add_argument(
        "--bucket", "-b",
        default=DEFAULT_BUCKET,
        help=f"S3 bucket name (default: {DEFAULT_BUCKET} or SYNC_BUCKET env var)"
    )

    args = parser.parse_args()

    if args.action == "push":
        success = push_to_s3(args.bucket)
    elif args.action == "pull":
        success = pull_from_s3(args.bucket)
    elif args.action == "sync":
        success = sync_databases(args.bucket)
    elif args.action == "status":
        show_status(args.bucket)
        success = True

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
