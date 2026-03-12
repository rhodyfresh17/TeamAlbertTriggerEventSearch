"""Database manager for tracking seen events and storing alerts."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from .models import TriggerEvent, EventType, EventSource


class DatabaseManager:
    """SQLite database manager for trigger events."""

    def __init__(self, db_path: str = "trigger_events.db"):
        self.db_path = Path(db_path)
        self._init_database()

    def _init_database(self):
        """Initialize database tables."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Events table
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

            # Add lead_status column if it doesn't exist (migration)
            try:
                cursor.execute('ALTER TABLE events ADD COLUMN lead_status TEXT DEFAULT "new"')
            except sqlite3.OperationalError:
                pass  # Column already exists

            try:
                cursor.execute('ALTER TABLE events ADD COLUMN notes TEXT')
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Seen URLs table (for deduplication)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS seen_urls (
                    url_hash TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    first_seen TEXT NOT NULL
                )
            ''')

            # Source status table (for tracking scraper health)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS source_status (
                    source_name TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    last_check TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    events_found INTEGER DEFAULT 0
                )
            ''')

            # Create indexes
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_events_date
                ON events(published_date)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_events_type
                ON events(event_type)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_events_alert
                ON events(alert_sent)
            ''')

            conn.commit()

    def has_seen_url(self, url: str) -> bool:
        """Check if we've already processed this URL."""
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT 1 FROM seen_urls WHERE url_hash = ?',
                (url_hash,)
            )
            return cursor.fetchone() is not None

    def mark_url_seen(self, url: str):
        """Mark a URL as processed."""
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO seen_urls (url_hash, url, first_seen)
                VALUES (?, ?, ?)
            ''', (url_hash, url, datetime.now().isoformat()))
            conn.commit()

    def save_event(self, event: TriggerEvent):
        """Save a trigger event to the database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO events (
                    id, title, event_type, source, url, published_date,
                    discovered_date, company_name, company_location, description,
                    person_name, person_title, acquirer, target, deal_value,
                    matched_keywords, matched_regions, relevance_score, alert_sent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                event.id,
                event.title,
                event.event_type.value,
                event.source.value,
                event.url,
                event.published_date.isoformat(),
                event.discovered_date.isoformat(),
                event.company_name,
                event.company_location,
                event.description,
                event.person_name,
                event.person_title,
                event.acquirer,
                event.target,
                event.deal_value,
                json.dumps(event.matched_keywords),
                json.dumps(event.matched_regions),
                event.relevance_score,
                1 if event.alert_sent else 0
            ))
            conn.commit()

    def mark_alert_sent(self, event_id: str):
        """Mark an event's alert as sent."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE events SET alert_sent = 1 WHERE id = ?',
                (event_id,)
            )
            conn.commit()

    def get_pending_alerts(self) -> List[TriggerEvent]:
        """Get events that haven't had alerts sent."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM events WHERE alert_sent = 0
                ORDER BY relevance_score DESC, published_date DESC
            ''')
            rows = cursor.fetchall()
            return [self._row_to_event(row) for row in rows]

    def get_recent_events(
        self,
        hours: int = 24,
        event_type: Optional[EventType] = None
    ) -> List[TriggerEvent]:
        """Get recent events within the specified time window."""
        cutoff = datetime.now() - timedelta(hours=hours)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            if event_type:
                cursor.execute('''
                    SELECT * FROM events
                    WHERE discovered_date > ? AND event_type = ?
                    ORDER BY relevance_score DESC, published_date DESC
                ''', (cutoff.isoformat(), event_type.value))
            else:
                cursor.execute('''
                    SELECT * FROM events
                    WHERE discovered_date > ?
                    ORDER BY relevance_score DESC, published_date DESC
                ''', (cutoff.isoformat(),))

            rows = cursor.fetchall()
            return [self._row_to_event(row) for row in rows]

    def _row_to_event(self, row) -> TriggerEvent:
        """Convert database row to TriggerEvent."""
        return TriggerEvent(
            id=row[0],
            title=row[1],
            event_type=EventType(row[2]),
            source=EventSource(row[3]),
            url=row[4],
            published_date=datetime.fromisoformat(row[5]),
            discovered_date=datetime.fromisoformat(row[6]),
            company_name=row[7],
            company_location=row[8],
            description=row[9],
            person_name=row[10],
            person_title=row[11],
            acquirer=row[12],
            target=row[13],
            deal_value=row[14],
            matched_keywords=json.loads(row[15]) if row[15] else [],
            matched_regions=json.loads(row[16]) if row[16] else [],
            relevance_score=row[17] or 0.0,
            alert_sent=bool(row[18])
        )

    def cleanup_old_entries(self, days: int = 30):
        """Remove entries older than specified days."""
        cutoff = datetime.now() - timedelta(days=days)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM events WHERE discovered_date < ?',
                (cutoff.isoformat(),)
            )
            cursor.execute(
                'DELETE FROM seen_urls WHERE first_seen < ?',
                (cutoff.isoformat(),)
            )
            conn.commit()

    def save_source_status(
        self,
        source_name: str,
        source_type: str,
        status: str,
        error_message: str = None,
        events_found: int = 0
    ):
        """Save the status of a scraper source."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO source_status
                (source_name, source_type, last_check, status, error_message, events_found)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                source_name,
                source_type,
                datetime.now().isoformat(),
                status,
                error_message,
                events_found
            ))
            conn.commit()

    def get_source_statuses(self) -> List[dict]:
        """Get all source statuses."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT source_name, source_type, last_check, status, error_message, events_found
                FROM source_status
                ORDER BY source_type, source_name
            ''')
            rows = cursor.fetchall()
            return [
                {
                    'source_name': row[0],
                    'source_type': row[1],
                    'last_check': row[2],
                    'status': row[3],
                    'error_message': row[4],
                    'events_found': row[5]
                }
                for row in rows
            ]

    def get_stats(self) -> dict:
        """Get database statistics."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM events')
            total_events = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM seen_urls')
            total_urls = cursor.fetchone()[0]

            cursor.execute('''
                SELECT event_type, COUNT(*)
                FROM events
                GROUP BY event_type
            ''')
            by_type = dict(cursor.fetchall())

            cursor.execute('''
                SELECT COUNT(*) FROM events
                WHERE discovered_date > ?
            ''', ((datetime.now() - timedelta(hours=24)).isoformat(),))
            last_24h = cursor.fetchone()[0]

            return {
                'total_events': total_events,
                'total_urls_seen': total_urls,
                'events_by_type': by_type,
                'events_last_24h': last_24h
            }
