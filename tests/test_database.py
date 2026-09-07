"""Tests for src/database.py — the SQLite store the GitHub Actions cache
carries between runs.

The cached file may have been created by ANY earlier schema, so every
migration must be additive and idempotent: an old source_status table
(pre-Phase-2, no counter columns) must gain items_fetched / filtered_out on
the next DatabaseManager() without losing its rows.
"""

import sqlite3

from src.database import DatabaseManager


def _old_schema_db(path):
    """source_status exactly as src/database.py created it before 2026-09-07."""
    conn = sqlite3.connect(str(path))
    conn.execute('''
        CREATE TABLE source_status (
            source_name TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            last_check TEXT NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT,
            events_found INTEGER DEFAULT 0
        )''')
    conn.execute("INSERT INTO source_status VALUES "
                 "('Old Feed','rss_feed','2026-09-01T00:00:00','success',NULL,4)")
    conn.commit()
    conn.close()


def _columns(path, table):
    conn = sqlite3.connect(str(path))
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
    conn.close()
    return cols


def test_counter_columns_added_to_old_schema_and_rows_survive(tmp_path):
    db_path = tmp_path / 'cached.db'
    _old_schema_db(db_path)

    db = DatabaseManager(str(db_path))

    cols = _columns(db_path, 'source_status')
    assert 'items_fetched' in cols and 'filtered_out' in cols
    rows = db.get_source_statuses()
    assert len(rows) == 1
    assert rows[0]['source_name'] == 'Old Feed'
    assert rows[0]['events_found'] == 4
    # pre-migration rows read as "unknown", never a fake 0
    assert rows[0]['items_fetched'] is None
    assert rows[0]['filtered_out'] is None


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / 'cached.db'
    _old_schema_db(db_path)
    DatabaseManager(str(db_path))
    DatabaseManager(str(db_path))  # second open must not raise or duplicate
    cols = _columns(db_path, 'source_status')
    assert cols.count('items_fetched') == 1 and cols.count('filtered_out') == 1
    assert len(DatabaseManager(str(db_path)).get_source_statuses()) == 1


def test_fresh_db_has_counter_columns(tmp_path):
    db_path = tmp_path / 'new.db'
    DatabaseManager(str(db_path))
    cols = _columns(db_path, 'source_status')
    assert cols[-2:] == ['items_fetched', 'filtered_out']


def test_save_source_status_round_trips_counters(tmp_path):
    db = DatabaseManager(str(tmp_path / 'new.db'))
    db.save_source_status('Feed A', 'rss_feed', 'success', events_found=3,
                          items_fetched=50, filtered_out=47)
    db.save_source_status('Feed B', 'rss_feed', 'error', error_message='boom')
    by_name = {r['source_name']: r for r in db.get_source_statuses()}
    assert (by_name['Feed A']['items_fetched'], by_name['Feed A']['filtered_out']) == (50, 47)
    assert by_name['Feed A']['events_found'] == 3
    # a scraper that reports no counters stores NULL, not 0
    assert by_name['Feed B']['items_fetched'] is None
    assert by_name['Feed B']['filtered_out'] is None
    # INSERT OR REPLACE keeps one row per source
    db.save_source_status('Feed A', 'rss_feed', 'success', events_found=0,
                          items_fetched=0, filtered_out=0)
    assert len(db.get_source_statuses()) == 2
    assert {r['source_name']: r['items_fetched'] for r in db.get_source_statuses()}['Feed A'] == 0
