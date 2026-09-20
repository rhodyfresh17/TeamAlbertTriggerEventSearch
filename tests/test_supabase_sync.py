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

# Pinned clock for everything stale-related (review 2026-09-07): _stale_cutoff
# is naive UTC, but the old _status() stamped rows with naive LOCAL
# datetime.now(), so every age was off by the Mac's UTC offset and the tests
# only passed because nothing sat within hours of the cutoff. Every reaper /
# sync test now passes now=NOW instead of racing the wall clock.
NOW = datetime(2026, 9, 7, 12, 0)


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

    assert ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW) is True

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
        assert ss.sync_to_supabase(db_path=str(db), days=14, dry_run=True, now=NOW) is True
    text = out.getvalue()
    assert 'DRY RUN' in text
    assert ', '.join(ss.SCRAPE_OWNED_COLUMNS) in text
    assert 'Rows to upsert: 1' in text
    assert '"lead_status"' not in text


# ═════════════════════════════════════════════════════════════════════════════
# v2 Phase 2 (2026-09-07): typed-column probe, source_status counters, reaper
# ═════════════════════════════════════════════════════════════════════════════
#
# A second fake client: the typed columns land by a hand-run SQL migration, so
# the sync PROBES select(col).limit(1) and treats any exception as "absent".
# This fake models exactly that (raises for unknown columns), records upserts,
# and backs source_status with a small in-memory row store so delete().lt()
# and select().lt() behave like PostgREST.

class _FakeQueryV2:
    def __init__(self, client, table):
        self._c = client
        self._table = table
        self._op = None
        self._cols = None
        self._lt = None
        self._in = None

    def select(self, cols='*', **kw):
        self._op = 'select'
        self._cols = [c.strip() for c in cols.split(',')] if cols != '*' else ['*']
        return self

    def limit(self, n):
        return self

    def lt(self, column, value):
        self._lt = (column, value)
        return self

    def in_(self, column, values):
        self._in = (column, [str(v) for v in values])
        return self

    def delete(self):
        self._op = 'delete'
        return self

    def upsert(self, data, on_conflict=None, **kw):
        self._c.calls.append({'table': self._table, 'data': data, 'on_conflict': on_conflict})
        self._op = 'upsert'
        return self

    def _filtered(self):
        rows = self._c.rows.get(self._table, [])
        if self._lt:
            col, val = self._lt
            rows = [r for r in rows if (r.get(col) or '') < val]
        if self._in:
            col, vals = self._in
            rows = [r for r in rows if str(r.get(col)) in vals]
        return rows

    def execute(self):
        if self._op == 'select':
            missing = [c for c in self._cols
                       if c != '*' and c not in self._c.present.get(self._table, set())]
            if missing:
                raise RuntimeError(f"column {self._table}.{missing[0]} does not exist")
            self._c.reads.append((self._table, self._cols, self._lt))
            return SimpleNamespace(data=list(self._filtered()), count=None)
        if self._op == 'delete':
            gone = self._filtered()
            self._c.delete_filters.append({'in': self._in, 'lt': self._lt})
            self._c.deleted.extend(gone)
            self._c.rows[self._table] = [r for r in self._c.rows.get(self._table, [])
                                         if r not in gone]
            return SimpleNamespace(data=gone, count=None)
        return SimpleNamespace(data=[], count=None)


class FakeClientV2:
    BASE_EVENT_COLS = set(ss.SCRAPE_OWNED_COLUMNS)
    BASE_STATUS_COLS = {'source_name', 'source_type', 'last_check', 'status',
                        'error_message', 'events_found'}

    def __init__(self, typed_columns=(), status_rows=()):
        """`typed_columns` = which of source / items_fetched / filtered_out the
        'live' schema has; `status_rows` seeds the source_status store."""
        typed = set(typed_columns)
        self.present = {
            'events': self.BASE_EVENT_COLS | ({'source'} & typed),
            'source_status': self.BASE_STATUS_COLS
                             | ((set(ss.STATUS_COUNTER_COLUMNS) | set(ss.STATUS_STREAK_COLUMNS)) & typed),
        }
        self.rows = {'source_status': [dict(r) for r in status_rows]}
        self.calls, self.reads, self.deleted, self.delete_filters = [], [], [], []

    def table(self, name):
        return _FakeQueryV2(self, name)


def _status(name, days_old, **extra):
    """A Supabase source_status row `days_old` days before the pinned NOW
    (same naive-UTC ISO shape src/database.py writes)."""
    row = {'source_name': name, 'source_type': 'rss_feed',
           'last_check': (NOW - timedelta(days=days_old)).isoformat(),
           'status': 'success', 'error_message': None, 'events_found': 1}
    row.update(extra)
    return row


# ── build_payload with / without source ─────────────────────────────────────

def test_payload_without_source_is_unchanged_and_with_source_appends_it():
    row = _row(source='sec_edgar')
    plain = ss.build_payload(row)
    assert tuple(plain) == ss.SCRAPE_OWNED_COLUMNS
    assert 'source' not in plain

    typed = ss.build_payload(row, include_source=True)
    assert tuple(typed) == ss.SCRAPE_OWNED_COLUMNS + ('source',)
    assert typed['source'] == 'sec_edgar'
    assert not (set(typed) & ss.NEVER_SYNC_COLUMNS)
    # every row in a batch carries the same keys, missing source included
    assert ss.build_payload(_row(source=None), include_source=True)['source'] is None


def test_source_is_scrape_owned_not_never_sync():
    assert 'source' not in ss.NEVER_SYNC_COLUMNS
    assert 'source' not in ss.SCRAPE_OWNED_COLUMNS  # gated by the probe, not assumed


# ── Adzuna relabel (review 2026-09-07) ───────────────────────────────────────
# The backfill relabels Adzuna cfo_hire / executive_hire rows to
# finance_seat_open in Supabase, but event_type is scrape-owned and the
# Actions SQLite cache still holds the old labels inside the window, so the
# upsert reverted it every 4 hours. The sync now applies the same relabel.

@pytest.mark.parametrize('source,etype,expected', [
    ('adzuna', 'cfo_hire', 'finance_seat_open'),
    ('adzuna', 'executive_hire', 'finance_seat_open'),
    ('adzuna', 'finance_seat_open', 'finance_seat_open'),   # idempotent
    ('adzuna', 'funding', 'funding'),                       # only the two hire labels
    ('ADZUNA ', 'cfo_hire', 'finance_seat_open'),           # enum text, tolerant of case/space
    ('sec_edgar', 'cfo_hire', 'cfo_hire'),                  # never another source
    ('google_news', 'executive_hire', 'executive_hire'),
    ('other', 'cfo_hire', 'cfo_hire'),
    (None, 'cfo_hire', 'cfo_hire'),
])
def test_build_payload_relabels_legacy_adzuna_hire_rows(source, etype, expected):
    row = _row(source=source, event_type=etype)
    assert ss.build_payload(row)['event_type'] == expected
    typed = ss.build_payload(row, include_source=True)
    assert typed['event_type'] == expected and typed['source'] == source
    assert tuple(typed) == ss.SCRAPE_OWNED_COLUMNS + ('source',)   # key set unchanged
    # idempotent: feeding the relabelled value back yields the same label
    assert ss._event_type_for_sync({'source': source, 'event_type': expected}) == expected


def test_sync_sends_relabelled_adzuna_rows_end_to_end(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 ('adz', 'Controller wanted', 'executive_hire', 'adzuna',
                  'https://www.adzuna.com/details/1', '2026-09-05',
                  (datetime.now() - timedelta(days=1)).isoformat(), 'Seat Co', 'd', '["NY"]',
                  'new', None))
    conn.commit(); conn.close()
    client = FakeClientV2()
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    sent = {r['id']: r['event_type']
            for r in [c for c in client.calls if c['table'] == 'events'][0]['data']}
    assert sent == {'adz': 'finance_seat_open', 'fresh': 'cfo_hire'}   # the sec row is untouched


# ── _present_columns probe ───────────────────────────────────────────────────

def test_present_columns_treats_any_exception_as_absent():
    client = FakeClientV2(typed_columns=('items_fetched',))
    assert ss._present_columns(client, 'events', ('source',)) == set()
    assert ss._present_columns(client, 'source_status', ss.STATUS_COUNTER_COLUMNS) == {'items_fetched'}
    # the original fake raises AssertionError on ANY select — still just "absent"
    assert ss._present_columns(FakeClient(), 'events', ('source',)) == set()


def test_get_source_statuses_selects_counters_when_present_and_survives_old_schema(tmp_path):
    old = tmp_path / 'old.db'
    _make_db(old)                       # pre-Phase-2 schema: no counter columns
    rows = ss.get_source_statuses_from_db(str(old))
    assert [r['source_name'] for r in rows] == ['sec_8k']
    assert 'items_fetched' not in rows[0]

    conn = sqlite3.connect(str(old))
    conn.execute('ALTER TABLE source_status ADD COLUMN items_fetched INTEGER')
    conn.execute('ALTER TABLE source_status ADD COLUMN filtered_out INTEGER')
    conn.execute("UPDATE source_status SET items_fetched = 40, filtered_out = 37")
    conn.commit(); conn.close()
    rows = ss.get_source_statuses_from_db(str(old))
    assert (rows[0]['items_fetched'], rows[0]['filtered_out']) == (40, 37)


def test_get_events_from_db_selects_source(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    assert ss.get_events_from_db(str(db), days=14)[0]['source'] == 'sec'


# ── end-to-end: probe absent vs present ──────────────────────────────────────

def test_sync_with_columns_absent_sends_no_source_and_no_counters(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    client = FakeClientV2(typed_columns=())

    assert ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW) is True

    events = [c for c in client.calls if c['table'] == 'events'][0]['data']
    assert tuple(events[0]) == ss.SCRAPE_OWNED_COLUMNS
    assert 'source' not in events[0]
    status = [c for c in client.calls if c['table'] == 'source_status'][0]['data']
    assert 'items_fetched' not in status and 'filtered_out' not in status
    assert status['source_name'] == 'sec_8k'


def test_sync_with_columns_present_sends_source_and_counters(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute('ALTER TABLE source_status ADD COLUMN items_fetched INTEGER')
    conn.execute('ALTER TABLE source_status ADD COLUMN filtered_out INTEGER')
    conn.execute("UPDATE source_status SET items_fetched = 12, filtered_out = 9")
    conn.commit(); conn.close()
    client = FakeClientV2(typed_columns=('source', 'items_fetched', 'filtered_out'))

    assert ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW) is True

    events = [c for c in client.calls if c['table'] == 'events'][0]['data']
    assert tuple(events[0]) == ss.SCRAPE_OWNED_COLUMNS + ('source',)
    assert events[0]['source'] == 'sec'
    assert 'lead_status' not in events[0] and 'notes' not in events[0]
    status = [c for c in client.calls if c['table'] == 'source_status'][0]['data']
    assert (status['items_fetched'], status['filtered_out']) == (12, 9)


def test_sync_with_only_one_counter_present_sends_just_that_one(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    client = FakeClientV2(typed_columns=('filtered_out',))
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    status = [c for c in client.calls if c['table'] == 'source_status'][0]['data']
    assert 'items_fetched' not in status
    assert 'filtered_out' in status and status['filtered_out'] is None  # old local schema


# ── failure streaks (migration 004): probe-then-send, never as NULL ──────────

def _db_with_streaks(path, streak, last_success):
    _make_db(path)
    conn = sqlite3.connect(str(path))
    for col in ('items_fetched INTEGER', 'filtered_out INTEGER',
                'consecutive_failures INTEGER', 'last_success TEXT'):
        conn.execute(f'ALTER TABLE source_status ADD COLUMN {col}')
    conn.execute('UPDATE source_status SET consecutive_failures = ?, last_success = ?',
                 (streak, last_success))
    conn.commit(); conn.close()


def test_get_source_statuses_reads_streaks_and_survives_a_counters_only_schema(tmp_path):
    db = tmp_path / 'new.db'
    _db_with_streaks(db, 3, '2026-09-05T09:00:00')
    row = ss.get_source_statuses_from_db(str(db))[0]
    assert (row['consecutive_failures'], row['last_success']) == (3, '2026-09-05T09:00:00')

    mid = tmp_path / 'mid.db'                         # counters added, streaks not yet
    _make_db(mid)
    conn = sqlite3.connect(str(mid))
    conn.execute('ALTER TABLE source_status ADD COLUMN items_fetched INTEGER')
    conn.execute('ALTER TABLE source_status ADD COLUMN filtered_out INTEGER')
    conn.execute('UPDATE source_status SET items_fetched = 7')
    conn.commit(); conn.close()
    row = ss.get_source_statuses_from_db(str(mid))[0]
    assert row['items_fetched'] == 7 and 'consecutive_failures' not in row


def test_sync_sends_streaks_when_live(tmp_path):
    db = tmp_path / 't.db'
    _db_with_streaks(db, 2, '2026-09-05T09:00:00')
    client = FakeClientV2(typed_columns=('consecutive_failures', 'last_success'))
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    status = [c for c in client.calls if c['table'] == 'source_status'][0]['data']
    assert (status['consecutive_failures'], status['last_success']) == (2, '2026-09-05T09:00:00')
    assert 'items_fetched' not in status              # counters are probed independently


def test_sync_never_sends_a_null_streak_value(tmp_path):
    """A source that has never succeeded locally (or a rebuilt SQLite file)
    must not blank what Supabase already knows."""
    db = tmp_path / 't.db'
    _db_with_streaks(db, 4, None)
    client = FakeClientV2(typed_columns=('consecutive_failures', 'last_success'))
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    status = [c for c in client.calls if c['table'] == 'source_status'][0]['data']
    assert status['consecutive_failures'] == 4
    assert 'last_success' not in status


def test_sync_sends_no_streaks_until_the_columns_are_live(tmp_path):
    db = tmp_path / 't.db'
    _db_with_streaks(db, 2, '2026-09-05T09:00:00')
    client = FakeClientV2(typed_columns=('items_fetched', 'filtered_out'))
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    status = [c for c in client.calls if c['table'] == 'source_status'][0]['data']
    assert 'consecutive_failures' not in status and 'last_success' not in status


# ── reaper ───────────────────────────────────────────────────────────────────

def test_stale_window_is_sixty_days():
    """Review 2026-09-07: age is the reaper's only criterion, so the window
    must be wide enough that a configured-but-quiet feed reads as an outage,
    not as a false positive."""
    assert ss.STALE_STATUS_DAYS == 60
    assert ss._stale_cutoff(now=NOW) == '2026-07-09T12:00:00'


def test_reap_deletes_only_stale_rows():
    client = FakeClientV2(status_rows=[_status('live', 1), _status('retired', 75),
                                       _status('ancient', 400), _status('edge', 59)])
    reaped = ss.reap_stale_source_status(client, now=NOW)            # default = 60d
    assert reaped == 2
    assert sorted(r['source_name'] for r in client.deleted) == ['ancient', 'retired']
    assert sorted(r['source_name'] for r in client.rows['source_status']) == ['edge', 'live']
    # deleted BY NAME with the age guard re-applied — never a bare lt() sweep,
    # so a row refreshed between the select and the delete survives
    (filters,) = client.delete_filters
    assert filters['in'][0] == 'source_name' and sorted(filters['in'][1]) == ['ancient', 'retired']
    assert filters['lt'] == ('last_check', ss._stale_cutoff(now=NOW))


def test_reap_logs_every_reaped_source_name(capsys):
    """The Actions log must show exactly which rows went (review 2026-09-07)."""
    client = FakeClientV2(status_rows=[_status('live', 1), _status('Old Feed', 75),
                                       _status('Dead Feed', 400)])
    assert ss.reap_stale_source_status(client, now=NOW) == 2
    out = capsys.readouterr().out
    assert f"deleting Old Feed (last_check {(NOW - timedelta(days=75)).isoformat()})" in out
    assert f"deleting Dead Feed (last_check {(NOW - timedelta(days=400)).isoformat()})" in out
    assert 'deleting live' not in out
    assert 'Reap: 2 stale source_status row(s)' in out


def test_reap_never_touches_protected_names(capsys):
    """A source the local scraper reported this window is kept whatever the
    Supabase row's age says (its upsert may have failed this run)."""
    client = FakeClientV2(status_rows=[_status('sec_8k', 100), _status('retired', 75)])
    reaped = ss.reap_stale_source_status(client, now=NOW, protect=['sec_8k'])
    assert reaped == 1
    assert [r['source_name'] for r in client.deleted] == ['retired']
    assert [r['source_name'] for r in client.rows['source_status']] == ['sec_8k']
    out = capsys.readouterr().out
    assert 'keeping sec_8k' in out and '1 protected' in out
    # dry run honours it too, and still deletes nothing
    client = FakeClientV2(status_rows=[_status('sec_8k', 100), _status('retired', 75)])
    assert ss.reap_stale_source_status(client, now=NOW, dry_run=True, protect=['sec_8k']) == 1
    assert client.deleted == [] and client.delete_filters == []
    # nothing to reap → no delete call at all
    client = FakeClientV2(status_rows=[_status('sec_8k', 100)])
    assert ss.reap_stale_source_status(client, now=NOW, protect=['sec_8k']) == 0
    assert client.delete_filters == []


def test_reap_dry_run_deletes_nothing_but_lists_candidates(capsys):
    client = FakeClientV2(status_rows=[_status('live', 1), _status('retired', 75)])
    would = ss.reap_stale_source_status(client, dry_run=True, now=NOW)
    assert would == 1
    assert client.deleted == []
    assert len(client.rows['source_status']) == 2
    assert 'would delete retired' in capsys.readouterr().out


def test_reap_swallows_client_errors_and_returns_zero():
    assert ss.reap_stale_source_status(FakeClient(), days=30) == 0  # no .delete on the old fake


def test_sync_reaps_at_end_and_no_reap_skips_it(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    seed = [_status('live', 1), _status('retired', 75)]

    client = FakeClientV2(status_rows=seed)
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    assert [r['source_name'] for r in client.deleted] == ['retired']

    client = FakeClientV2(status_rows=seed)
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, reap=False, now=NOW)
    assert client.deleted == []


def test_sync_protects_sources_the_local_scraper_reported_this_window(tmp_path, capsys):
    """_make_db's local sec_8k row is fresh, but the Supabase copy is 100
    days old (say its upsert never landed). The reaper must leave it alone
    and still reap the genuinely retired feed (review 2026-09-07)."""
    db = tmp_path / 't.db'
    _make_db(db)
    client = FakeClientV2(status_rows=[_status('sec_8k', 100), _status('retired', 75)])
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    assert [r['source_name'] for r in client.deleted] == ['retired']
    assert 'keeping sec_8k' in capsys.readouterr().out
    # a stale LOCAL row protects nothing: it is neither pushed nor a shield
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO source_status VALUES ('retired','rss_feed',?,'success',NULL,0)",
                 ((NOW - timedelta(days=75)).isoformat(),))
    conn.commit(); conn.close()
    client = FakeClientV2(status_rows=[_status('sec_8k', 100), _status('retired', 75)])
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    assert [r['source_name'] for r in client.deleted] == ['retired']


def test_stale_local_statuses_are_not_pushed(tmp_path):
    """A retired feed's frozen SQLite row must not be re-upserted — the reaper
    would only have to delete it again next run."""
    db = tmp_path / 't.db'
    _make_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO source_status VALUES ('Dead Feed','rss_feed',?,'success',NULL,0)",
                 ((NOW - timedelta(days=90)).isoformat(),))
    conn.commit(); conn.close()
    client = FakeClientV2()
    ss.sync_to_supabase(db_path=str(db), days=14, client=client, now=NOW)
    pushed = [c['data']['source_name'] for c in client.calls if c['table'] == 'source_status']
    assert pushed == ['sec_8k']


def test_split_stale_statuses_keeps_rows_without_last_check():
    cutoff = ss._stale_cutoff(30, now=datetime(2026, 9, 7))
    fresh, stale = ss.split_stale_statuses(
        [{'source_name': 'a', 'last_check': '2026-09-01T00:00:00'},
         {'source_name': 'b', 'last_check': '2026-07-01T00:00:00'},
         {'source_name': 'c', 'last_check': None}], cutoff)
    assert [r['source_name'] for r in fresh] == ['a', 'c']
    assert [r['source_name'] for r in stale] == ['b']


def test_dry_run_with_injected_client_lists_reap_but_deletes_nothing(tmp_path):
    db = tmp_path / 't.db'
    _make_db(db)
    client = FakeClientV2(status_rows=[_status('retired', 75)])
    out = io.StringIO()
    with redirect_stdout(out):
        ss.sync_to_supabase(db_path=str(db), days=14, dry_run=True, client=client, now=NOW)
    assert client.deleted == [] and client.calls == [] and client.delete_filters == []
    text = out.getvalue()
    assert 'would delete retired' in text
    assert 'items_fetched' in text          # dry-run documents the probed columns


def test_cli_no_reap_flag(tmp_path, monkeypatch):
    db = tmp_path / 't.db'
    _make_db(db)
    seen = {}

    def _fake_sync(**kw):
        seen.update(kw); return True
    monkeypatch.setattr(ss, 'sync_to_supabase', _fake_sync)
    assert ss.main(['--db', str(db), '--no-reap']) == 0
    assert seen['reap'] is False
    assert ss.main(['--db', str(db)]) == 0
    assert seen['reap'] is True
