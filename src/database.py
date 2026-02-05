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
                    alert_sent INTEGER DEFAULT 0
                )
            ''')

            # Seen URLs table (for deduplication)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS seen_urls (
                    url_hash TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    first_seen TEXT NOT NULL
                )
            ''')

            # Feedback table for learning user preferences
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    feedback_date TEXT NOT NULL,
                    event_type TEXT,
                    matched_keywords TEXT,
                    matched_regions TEXT,
                    company_name TEXT,
                    FOREIGN KEY (event_id) REFERENCES events(id)
                )
            ''')

            # Learned patterns table for relevance adjustment
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS learned_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_type TEXT NOT NULL,
                    pattern_value TEXT NOT NULL,
                    score_adjustment REAL DEFAULT 0,
                    positive_count INTEGER DEFAULT 0,
                    negative_count INTEGER DEFAULT 0,
                    last_updated TEXT,
                    UNIQUE(pattern_type, pattern_value)
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
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_feedback_event
                ON feedback(event_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_patterns_type
                ON learned_patterns(pattern_type, pattern_value)
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

    def save_feedback(self, event_id: str, rating: int, event: Optional[TriggerEvent] = None):
        """Save user feedback for an event.

        Args:
            event_id: The event ID
            rating: 1 (relevant/good) or -1 (not relevant/bad)
            event: Optional TriggerEvent for storing pattern data
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Get event details if not provided
            if event is None:
                cursor.execute('SELECT * FROM events WHERE id = ?', (event_id,))
                row = cursor.fetchone()
                if row:
                    event = self._row_to_event(row)

            # Save the feedback
            cursor.execute('''
                INSERT INTO feedback (
                    event_id, rating, feedback_date, event_type,
                    matched_keywords, matched_regions, company_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                event_id,
                rating,
                datetime.now().isoformat(),
                event.event_type.value if event else None,
                json.dumps(event.matched_keywords) if event else None,
                json.dumps(event.matched_regions) if event else None,
                event.company_name if event else None
            ))

            # Update learned patterns based on feedback
            if event:
                self._update_learned_patterns(cursor, event, rating)

            conn.commit()

    def _update_learned_patterns(self, cursor, event: TriggerEvent, rating: int):
        """Update learned patterns based on feedback."""
        patterns_to_update = []

        # Learn from event type
        patterns_to_update.append(('event_type', event.event_type.value))

        # Learn from keywords
        for keyword in event.matched_keywords:
            patterns_to_update.append(('keyword', keyword.lower()))

        # Learn from regions
        for region in event.matched_regions:
            patterns_to_update.append(('region', region.lower()))

        # Learn from company name patterns (first word, often indicates industry)
        if event.company_name:
            words = event.company_name.lower().split()
            if words:
                patterns_to_update.append(('company_word', words[0]))

        # Update each pattern
        for pattern_type, pattern_value in patterns_to_update:
            if rating > 0:
                cursor.execute('''
                    INSERT INTO learned_patterns (pattern_type, pattern_value, positive_count, last_updated)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(pattern_type, pattern_value) DO UPDATE SET
                        positive_count = positive_count + 1,
                        score_adjustment = (positive_count + 1.0) / (positive_count + negative_count + 1.0) * 10 - 5,
                        last_updated = ?
                ''', (pattern_type, pattern_value, datetime.now().isoformat(), datetime.now().isoformat()))
            else:
                cursor.execute('''
                    INSERT INTO learned_patterns (pattern_type, pattern_value, negative_count, last_updated)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(pattern_type, pattern_value) DO UPDATE SET
                        negative_count = negative_count + 1,
                        score_adjustment = (positive_count * 1.0) / (positive_count + negative_count + 1.0) * 10 - 5,
                        last_updated = ?
                ''', (pattern_type, pattern_value, datetime.now().isoformat(), datetime.now().isoformat()))

    def get_learned_adjustment(self, event: TriggerEvent) -> float:
        """Get the learned score adjustment for an event based on feedback patterns.

        Returns a value between -10 and +10 to adjust relevance score.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            total_adjustment = 0.0
            pattern_count = 0

            # Check event type
            cursor.execute('''
                SELECT score_adjustment FROM learned_patterns
                WHERE pattern_type = 'event_type' AND pattern_value = ?
            ''', (event.event_type.value,))
            row = cursor.fetchone()
            if row:
                total_adjustment += row[0]
                pattern_count += 1

            # Check keywords
            for keyword in event.matched_keywords:
                cursor.execute('''
                    SELECT score_adjustment FROM learned_patterns
                    WHERE pattern_type = 'keyword' AND pattern_value = ?
                ''', (keyword.lower(),))
                row = cursor.fetchone()
                if row:
                    total_adjustment += row[0]
                    pattern_count += 1

            # Check regions
            for region in event.matched_regions:
                cursor.execute('''
                    SELECT score_adjustment FROM learned_patterns
                    WHERE pattern_type = 'region' AND pattern_value = ?
                ''', (region.lower(),))
                row = cursor.fetchone()
                if row:
                    total_adjustment += row[0]
                    pattern_count += 1

            # Average the adjustments if we found any
            if pattern_count > 0:
                return total_adjustment / pattern_count
            return 0.0

    def get_feedback_stats(self) -> dict:
        """Get feedback statistics."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM feedback WHERE rating > 0')
            positive = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM feedback WHERE rating < 0')
            negative = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM learned_patterns')
            patterns = cursor.fetchone()[0]

            # Top positive patterns
            cursor.execute('''
                SELECT pattern_type, pattern_value, score_adjustment, positive_count, negative_count
                FROM learned_patterns
                ORDER BY score_adjustment DESC
                LIMIT 5
            ''')
            top_positive = cursor.fetchall()

            # Top negative patterns
            cursor.execute('''
                SELECT pattern_type, pattern_value, score_adjustment, positive_count, negative_count
                FROM learned_patterns
                ORDER BY score_adjustment ASC
                LIMIT 5
            ''')
            top_negative = cursor.fetchall()

            return {
                'total_positive': positive,
                'total_negative': negative,
                'total_patterns': patterns,
                'top_positive_patterns': top_positive,
                'top_negative_patterns': top_negative
            }

    def get_unrated_events(self, limit: int = 20) -> List[TriggerEvent]:
        """Get recent events that haven't been rated yet."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT e.* FROM events e
                LEFT JOIN feedback f ON e.id = f.event_id
                WHERE f.id IS NULL
                ORDER BY e.discovered_date DESC
                LIMIT ?
            ''', (limit,))

            rows = cursor.fetchall()
            return [self._row_to_event(row) for row in rows]

    def get_event_by_id(self, event_id: str) -> Optional[TriggerEvent]:
        """Get a single event by ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM events WHERE id = ?', (event_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_event(row)
            return None

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
