#!/usr/bin/env python3
"""
Google Sheets sync for trigger events.
Pushes new events to a Google Sheet for easy access anywhere.
"""

import os
import json
import sqlite3
from datetime import datetime

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False
    print("Google Sheets API not installed. Run: pip install google-api-python-client google-auth")


# Configuration
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
DATABASE_PATH = os.environ.get('DATABASE_PATH', 'trigger_events.db')


def get_sheets_service():
    """Initialize Google Sheets API service."""
    # Check for credentials in environment variable (for GitHub Actions)
    creds_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')

    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        # Fall back to credentials file
        creds_file = os.environ.get('GOOGLE_CREDENTIALS_FILE', 'credentials.json')
        if not os.path.exists(creds_file):
            raise FileNotFoundError(
                f"Credentials file not found: {creds_file}\n"
                "Set GOOGLE_SHEETS_CREDENTIALS env var or provide credentials.json"
            )
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)

    return build('sheets', 'v4', credentials=creds)


def get_events_from_db(since_hours=168):
    """Get events from local SQLite database."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, title, company_name, event_type, description,
               source_url, published_date, discovered_at, lead_status, notes
        FROM events
        ORDER BY discovered_at DESC
    ''')

    events = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return events


def sync_to_sheet(spreadsheet_id: str):
    """Sync all events to Google Sheet."""
    if not SHEETS_AVAILABLE:
        print("Google Sheets API not available")
        return False

    service = get_sheets_service()
    events = get_events_from_db()

    if not events:
        print("No events to sync")
        return True

    # Prepare header row
    headers = ['ID', 'Title', 'Company', 'Event Type', 'Description',
               'Source URL', 'Published Date', 'Discovered At', 'Lead Status', 'Notes']

    # Prepare data rows
    rows = [headers]
    for event in events:
        rows.append([
            event.get('id', ''),
            event.get('title', ''),
            event.get('company_name', ''),
            event.get('event_type', ''),
            event.get('description', '')[:500] if event.get('description') else '',  # Truncate long descriptions
            event.get('source_url', ''),
            event.get('published_date', ''),
            event.get('discovered_at', ''),
            event.get('lead_status', ''),
            event.get('notes', '')
        ])

    # Clear existing data and write new data
    try:
        # Clear the sheet first
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range='Events!A:J'
        ).execute()

        # Write new data
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range='Events!A1',
            valueInputOption='RAW',
            body={'values': rows}
        ).execute()

        print(f"Synced {len(events)} events to Google Sheet")
        return True

    except Exception as e:
        print(f"Error syncing to sheet: {e}")
        return False


def append_event(spreadsheet_id: str, event: dict):
    """Append a single event to the sheet."""
    if not SHEETS_AVAILABLE:
        return False

    service = get_sheets_service()

    row = [[
        event.get('id', ''),
        event.get('title', ''),
        event.get('company_name', ''),
        event.get('event_type', ''),
        event.get('description', '')[:500] if event.get('description') else '',
        event.get('source_url', ''),
        event.get('published_date', ''),
        event.get('discovered_at', datetime.now().isoformat()),
        event.get('lead_status', ''),
        event.get('notes', '')
    ]]

    try:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range='Events!A:J',
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': row}
        ).execute()
        return True
    except Exception as e:
        print(f"Error appending event: {e}")
        return False


def read_from_sheet(spreadsheet_id: str):
    """Read events from Google Sheet (for dashboard)."""
    if not SHEETS_AVAILABLE:
        return []

    service = get_sheets_service()

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range='Events!A:J'
        ).execute()

        values = result.get('values', [])
        if not values or len(values) < 2:
            return []

        headers = values[0]
        events = []
        for row in values[1:]:
            # Pad row to match headers length
            row = row + [''] * (len(headers) - len(row))
            event = dict(zip(headers, row))
            events.append(event)

        return events

    except Exception as e:
        print(f"Error reading from sheet: {e}")
        return []


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Sync trigger events to Google Sheets')
    parser.add_argument('--spreadsheet-id', '-s',
                        default=os.environ.get('GOOGLE_SHEET_ID'),
                        help='Google Sheet ID (or set GOOGLE_SHEET_ID env var)')
    parser.add_argument('--action', '-a', choices=['sync', 'read'], default='sync',
                        help='Action to perform')

    args = parser.parse_args()

    if not args.spreadsheet_id:
        print("Error: Spreadsheet ID required. Use --spreadsheet-id or set GOOGLE_SHEET_ID")
        exit(1)

    if args.action == 'sync':
        success = sync_to_sheet(args.spreadsheet_id)
        exit(0 if success else 1)
    elif args.action == 'read':
        events = read_from_sheet(args.spreadsheet_id)
        print(json.dumps(events, indent=2))
