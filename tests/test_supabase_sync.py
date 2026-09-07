"""Tests for supabase_sync.py — the SQLite → Supabase push.

Guards the 2026-09-06 fix: the sync must send ONLY scrape-owned columns
(never lead_status / notes / enrichment fields), must never READ Supabase
state to decide what to send (the unpaginated prefetch that wiped rep work),
must window to recent rows, and must batch upserts of 200 on id.

No network: a fake client records every call and raises on any select().
"""

import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import supabase_sync as ss


# ── Fake Supabase client ─────────────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, calls, table):
        self._calls = calls
        self._table = table

    def upsert(self, data, on_conflict=None, **kw):
        self._calls.append({'table': self._table, 'data': data, 'on_conflict': on_conflict})
        return self

    def select(self, *a, **kw):
        raise AssertionError('sync must never read from Supabase (prefetch was the bug)')

    def execute(self):
        return SimpleNamespace(data=[], count=None)


class FakeClient:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _FakeQuery(self.calls, name)


def _row(i=1, **overrides):
    """A SQLite events row as get_events_from_db() would return it — with the
    rep/enrichment columns deliberately present so the sync has to DROP them."""
    base = {
        'id': f'evt-{i}',
        'title': f'Acme Corp names new CFO {i}',
        'company_name': 'Acme Corp',
        'event_type': 'cfo_hire',
        'description': 'x' * 2500,
        'url': f'https://example.com/{i}',
        'published_date': '2026-09-05T12:00:00',
        'discovered_date': '2026-09-05T13:00:00',
        'matched_regions': '["Boston", "New Hampshire"]',
        # must never reach Supabase:
        'lead_status': 'new',
        'notes': 'rep wrote this',
        'grade': 'A',
        'hashtags': '["#NewCFO"]',
        'enriched_at': '2026-09-05T14:00:00',
    }
    base.update(overrides)
    return base


# ── build_payload ────────────────────────────────────────────────────────────

def test_payload_contains_only_scrape_owned_columns():
    payload = ss.build_payload(_row())
    assert tuple(payload) == ss.SCRAPE_OWNED_COLUMNS
    assert not (set(payload) & ss.NEVER_SYNC_COLUMNS)
    assert 'lead_status' not in payload
    assert 'notes' not in payload
    # column mapping SQLite → Supabase
    assert payload['source_url'] == 'https://example.com/1'
    assert payload['discovered_at'] == '2026-09-05T13:00:00'
    assert len(payload['description']) == ss.DESCRIPTION_MAX_CHARS


@pytest.mark.parametrize('raw, expected', [
    ('["Boston", "New Hampshire"]', ['Boston', 'New Hampshire']),
    (['Maine'], ['Maine']),
    ('[]', []),
    ('', []),
    (None, []),
    ('not json', []),
])
def test_matched_regions_is_json_list(raw, expected):
    payload = ss.build_payload(_row(matched_regions=raw))
    decoded = json.loads(payload['matched_regions'])
    assert isinstance(decoded, list)
    assert decoded == expected


# ── sync_events: batching + never a read ─────────────────────────────────────

def test_sync_events_batches_of_200_on_id_and_never_sends_rep_columns():
    client = FakeClient()
    events = [_row(i) for i in range(450)]

    synced = ss.sync_events(client, events)

    assert synced == 450
    assert [len(c['data']) for c in client.calls] == [200, 200, 50]
    for call in client.calls:
        assert call['table'] == 'events'
        assert call['on_conflict'] == 'id'
        for row in call['data']:
            assert tuple(row) == ss.SCRAPE_OWNED_COLUMNS
            assert 'lead_status' not in row and 'notes' not in row
            assert isinstance(json.loads(row['matched_regions']), list)


def test_failed_batch_falls_back_to_single_rows():
    class FlakyClient(FakeClient):
        def table(self, name):
            calls = self.calls

            class Q(_FakeQuery):
                def execute(self_inner):
                    if isinstance(calls[-1]['data'], list):
                        raise RuntimeError('simulated batch rejection')
                    return SimpleNamespace(data=[], count=None)
            return Q(calls, name)

    client = FlakyClient()
    synced = ss.sync_events(client, [_row(i) for i in range(3)])
    assert synced == 3
    single_row_calls = [c for c in client.calls if isinstance(c['data'], dict)]
    assert len(single_row_calls) == 3
    assert all('lead_status' not in c['data'] for c in single_row_calls)


# ── SQLite window + sync_to_supabase end-to-end with the fake client ─────────

def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute('''
        CREATE TABLE events (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, event_type TEXT NOT NULL,
            source TEXT NOT NULL, url TEXT NOT NULL, published_date TEXT NOT NULL,
            discovered_date TEXT NOT NULL, company_name TEXT, description TEXT,
            matched_regions TEXT, lead_status TEXT DEFAULT 'new', notes TEXT
        )''')
    conn.execute('''
        CREATE TABLE source_status (
            source_name TEXT PRIMARY KEY, source_type TEXT NOT NULL,
            last_check TEXT NOT NULL, status TEXT NOT NULL,
            error_message TEXT, events_found INTEGER DEFAULT 0
        )''')
    now = datetime.now()
    rows = [
        ('fresh', 'Fresh event', 'cfo_hire', 'sec', 'https://e.com/fresh', '2026-09-05',
         (now - timedelta(days=2)).isoformat(), 'Fresh Co', 'd', '["Boston"]', 'REVIEWED', 'keep'),
        ('stale', 'Stale event', 'funding', 'rss', 'https://e.com/stale', '2026-08-01',
         (now - timedelta(days=20)).isoformat(), 'Stale Co', 'd', '[]', 'new', None),
    ]
    conn.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    conn.execute("INSERT INTO source_status VALUES ('sec_8k','sec','2026-09-06T00:00:00','success',NULL,3)")
    conn.commit()
    conn.close()


def test_get_events_window_and_matched_regions_selected(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)

    recent = ss.get_events_from_db(str(db), days=14)
    assert [e['id'] for e in recent] == ['fresh']
    assert recent[0]['matched_regions'] == '["Boston"]'

    everything = ss.get_events_from_db(str(db), days=0)
    assert sorted(e['id'] for e in everything) == ['fresh', 'stale']


def test_sync_to_supabase_with_fake_client_sends_only_recent_scrape_columns(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    client = FakeClient()

    assert ss.sync_to_supabase(db_path=str(db), days=14, client=client) is True

    event_calls = [c for c in client.calls if c['table'] == 'events']
    assert len(event_calls) == 1
    sent = event_calls[0]['data']
    assert [r['id'] for r in sent] == ['fresh']          # stale row outside window
    assert tuple(sent[0]) == ss.SCRAPE_OWNED_COLUMNS
    assert 'lead_status' not in sent[0] and 'notes' not in sent[0]
    assert json.loads(sent[0]['matched_regions']) == ['Boston']
    # source_status sync unchanged
    status_calls = [c for c in client.calls if c['table'] == 'source_status']
    assert len(status_calls) == 1 and status_calls[0]['on_conflict'] == 'source_name'


def test_dry_run_touches_no_client(tmp_path, monkeypatch):
    db = tmp_path / 't.db'
    _make_db(db)

    def _boom():
        raise AssertionError('dry-run must not construct a Supabase client')
    monkeypatch.setattr(ss, 'get_supabase_client', _boom)

    out = io.StringIO()
    with redirect_stdout(out):
        assert ss.sync_to_supabase(db_path=str(db), days=14, dry_run=True) is True
    text = out.getvalue()
    assert 'DRY RUN' in text
    assert ', '.join(ss.SCRAPE_OWNED_COLUMNS) in text
    assert 'Rows to upsert: 1' in text
    assert '"lead_status"' not in text
