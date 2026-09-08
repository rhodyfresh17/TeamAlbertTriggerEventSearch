"""scripts/expire_triggers.py — nightly trigger expiry (Phase 4 slice C2,
2026-09-08). A fake Supabase client records every write; the accounts
module is a stub with the C1 contract. No network.

The '# ── Review 2026-09-08 (Phase 4)' block pins 5a (best GRADE vs best
TRIGGER, never a None grade over a live graded event, graded_at = the
event's enriched_at), 5b (accounts linked by best_trigger_event_id), 5d
(the enrichment run lock in --apply mode) and 5f (the wrapper's exit)."""
import copy
import io
import os
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts import expire_triggers as xt
from src.pipeline.runlock import RunLock
from src.pipeline.typed import TYPED_EVENT_COLUMNS, reset_probe_cache

try:                                            # the real module, when it has landed
    from src.pipeline import accounts as _real_accounts
except ImportError:
    _real_accounts = None

NOW = datetime(2026, 9, 8, 6, 30, tzinfo=timezone.utc)
BASE = set(xt.BASE_COLS) | {'enriched_at'}          # no typed columns at all
ALL = BASE | set(TYPED_EVENT_COLUMNS)


def _days_ago(n):
    return (NOW - timedelta(days=n)).isoformat()


# ── Fake client ─────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, client, table):
        self.c, self.table_name = client, table
        self.cols, self.filters, self._update, self._range = None, [], None, None

    def select(self, cols='*'):
        self.cols = [c.strip() for c in cols.split(',')]
        return self

    def limit(self, n):
        return self

    def order(self, col, desc=False):
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def is_(self, col, val):
        self.filters.append(('is', col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(('in', col, list(vals)))
        return self

    def eq(self, col, val):
        self.filters.append(('eq', col, val))
        return self

    def update(self, payload):
        self._update = payload
        return self

    def delete(self):
        raise AssertionError('expiry never hard-deletes')

    def execute(self):
        if self._update is not None:
            key = next(v for op, c, v in self.filters if op == 'eq')
            self.c.updates.append((self.table_name, key, copy.deepcopy(self._update)))
            if self.table_name == 'events':
                for r in self.c.events:
                    if r['id'] == key:
                        r.update(self._update)
            return SimpleNamespace(data=[])
        if self.table_name == 'accounts':
            self.c.account_reads += 1
            rows = list(self.c.accounts)
            for op, col, val in self.filters:
                if op == 'in':
                    rows = [r for r in rows if r.get(col) in val]
            return SimpleNamespace(data=copy.deepcopy(rows))
        missing = [c for c in self.cols if c not in self.c.columns]
        if missing:
            raise Exception(f'column events.{missing[0]} does not exist')
        rows = list(self.c.events)
        for op, col, val in self.filters:
            if op == 'is' and val == 'null':
                rows = [r for r in rows if not r.get(col)]
        rows.sort(key=lambda r: r.get('discovered_at') or '')
        if self._range:
            a, b = self._range
            rows = rows[a:b + 1]
        self.c.reads.append(list(self.filters))
        return SimpleNamespace(data=[{k: v for k, v in r.items() if k in self.cols}
                                     for r in copy.deepcopy(rows)])


class FakeClient:
    def __init__(self, events, columns=ALL, accounts=()):
        self.events = [dict(e) for e in events]
        self.columns = set(columns)
        self.accounts = [dict(a) for a in accounts]
        self.updates, self.reads, self.account_reads = [], [], 0

    def table(self, name):
        return _Q(self, name)

    def event_updates(self):
        return [(k, p) for t, k, p in self.updates if t == 'events']

    def account_updates(self):
        return {k: p for t, k, p in self.updates if t == 'accounts'}


class AccountsStub:
    TRIGGER_PRIORITY = {'cfo_hire': 0, 'finance_seat_open': 1, 'merger_acquisition': 2, 'funding': 3}

    def __init__(self, present=True):
        self.present = present

    def probe_accounts(self, client):
        return self.present


def _row(i, etype, days, **over):
    r = {'id': f'ev{i}', 'event_type': etype, 'title': f'{etype} #{i}', 'company_name': f'Co {i}',
         'published_date': _days_ago(days), 'discovered_at': _days_ago(days),
         'fit': {'verdict': 'pass', 'account_name': f'Co {i}'}, 'grade': 'B', 'numeric_score': 5,
         'confidence_level': 'High', 'hashtags': ['#NewCFO'], 'grade_justification': 'x',
         'blocked_at': None, 'expires_at': None, 'account_key': None, 'verify_state': 'verified'}
    r.update(over)
    return r


@pytest.fixture(autouse=True)
def _probe_reset():
    # Both probe memos: typed's (events columns) and the accounts module's
    # (a POSITIVE answer there is final for the process — a fake must
    # never leave one behind for another test file).
    reset_probe_cache()
    if _real_accounts is not None:
        _real_accounts.reset_probe_cache()
    yield
    reset_probe_cache()
    if _real_accounts is not None:
        _real_accounts.reset_probe_cache()


def _run(client, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        counts = xt.run(client, now=NOW, out=print, **kw)
    return counts, buf.getvalue()


# ── Pure decisions ──────────────────────────────────────────────────────────
def test_expiry_typed_column_wins_then_shelf_life_by_type():
    typed = _row(1, 'cfo_hire', 10, expires_at=_days_ago(1))
    assert xt.is_expired(typed, NOW)                             # typed says yesterday
    assert not xt.is_expired(_row(2, 'cfo_hire', 59), NOW)       # 60d shelf life
    assert xt.is_expired(_row(3, 'cfo_hire', 61), NOW)
    assert not xt.is_expired(_row(4, 'merger_acquisition', 100), NOW)   # 120d
    assert xt.is_expired(_row(5, 'merger_acquisition', 121), NOW)
    assert xt.is_expired(_row(6, 'unknown_type', 61), NOW)       # 'other' → 60d
    assert not xt.is_expired(_row(7, 'stable_target', 300), NOW)
    # grace keeps it a little longer
    assert not xt.is_expired(_row(8, 'cfo_hire', 63), NOW, grace_days=5)
    assert xt.is_expired(_row(8, 'cfo_hire', 66), NOW, grace_days=5)
    # a tombstoned row is never expired again; no date → never expired
    assert not xt.is_expired(_row(9, 'cfo_hire', 400, blocked_at=_days_ago(1)), NOW)
    assert not xt.is_expired(_row(10, 'cfo_hire', 400, published_date=None, discovered_at=None), NOW)
    # discovered_at is the fallback anchor
    assert xt.is_expired(_row(11, 'funding', 0, published_date=None, discovered_at=_days_ago(90)), NOW)


def test_reason_and_payload_write_only_the_tombstone_fields():
    r = _row(1, 'cfo_hire', 75)
    assert xt.expiry_reason(r, NOW) == 'trigger_expired: 75d old cfo_hire'
    pl = xt.tombstone_payload(r, NOW)
    assert set(pl) == {'blocked_at', 'blocked_reason'}
    assert pl['blocked_at'] == NOW.isoformat()
    assert 'verify_state' not in pl and 'enriched_at' not in pl and 'fit_verdict' not in pl


def test_row_account_key_prefers_typed_then_fit_then_company_name():
    assert xt.row_account_key(_row(1, 'cfo_hire', 1, account_key='typed key')) == 'typed key'
    assert xt.row_account_key(_row(1, 'cfo_hire', 1, fit={'account_name': 'Acme Bank, Inc.'})) == 'acme bank'
    assert xt.row_account_key(_row(1, 'cfo_hire', 1, fit='{"account_name": "Beta Corp"}')) == 'beta'
    assert xt.row_account_key(_row(1, 'cfo_hire', 1, fit=None, company_name='Gamma LLC')) == 'gamma'


def test_best_remaining_by_priority_then_recency():
    prio = AccountsStub.TRIGGER_PRIORITY
    old_ma = _row(1, 'merger_acquisition', 100, grade='C')
    new_funding = _row(2, 'funding', 5, grade='B')
    assert xt.best_remaining([new_funding, old_ma], prio)['id'] == 'ev1'     # priority first
    newer_ma = _row(3, 'merger_acquisition', 20, grade='D')
    assert xt.best_remaining([old_ma, newer_ma], prio)['id'] == 'ev3'        # then recency
    assert xt.best_remaining([old_ma, _row(4, 'funding', 1, blocked_at='x')], prio)['id'] == 'ev1'
    assert xt.best_remaining([], prio) is None
    # a list-shaped priority and the default both work
    assert xt.trigger_rank('funding', ['cfo_hire', 'funding']) == 1
    assert xt.trigger_rank('nope', ['cfo_hire']) == 1
    assert xt.trigger_rank('cfo_hire') == 0 and xt.trigger_rank('nope') == 7


def test_account_refresh_payload_from_best_or_cleared():
    best = _row(1, 'merger_acquisition', 20, grade='C', numeric_score=3, hashtags='["#Acquisitions"]')
    pl = xt.account_refresh_payload(best, NOW)
    assert pl['best_trigger_type'] == 'merger_acquisition' and pl['best_trigger_event_id'] == 'ev1'
    assert pl['best_trigger_at'] == _days_ago(20)
    assert pl['grade'] == 'C' and pl['numeric_score'] == 3 and pl['hashtags'] == ['#Acquisitions']
    assert pl['graded_event_id'] == 'ev1' and pl['graded_at'] == NOW.isoformat()
    ungraded = xt.account_refresh_payload(_row(2, 'funding', 1, grade=None), NOW)
    assert ungraded['best_trigger_type'] == 'funding' and ungraded['grade'] is None
    assert ungraded['graded_event_id'] is None and ungraded['graded_at'] is None
    cleared = xt.account_refresh_payload(None, NOW)
    assert set(cleared) == set(xt.ACCOUNT_TRIGGER_FIELDS + xt.ACCOUNT_GRADE_FIELDS)
    assert all(v is None for v in cleared.values()) and 'active' not in cleared


# ── The pass ────────────────────────────────────────────────────────────────
FIXTURE = [
    _row(1, 'cfo_hire', 75, account_key='acme'),                        # expired
    _row(2, 'funding', 30, account_key='acme', grade='B'),               # acme keeps this …
    _row(3, 'merger_acquisition', 90, account_key='acme', grade='C'),    # … and this (better trigger)
    _row(4, 'cfo_hire', 61, account_key='beta'),                         # expired, beta's only trigger
    _row(5, 'merger_acquisition', 130, account_key='gamma', blocked_at=_days_ago(3),
         verify_state='not_fit'),                                        # already tombstoned
    _row(6, 'funding', 10, account_key='delta'),                         # fresh
    _row(7, 'expansion', 100, account_key='eps', expires_at=_days_ago(2)),   # typed expiry
]


def test_dry_run_writes_nothing_and_reports_the_table():
    client = FakeClient(FIXTURE, accounts=[{'account_key': 'acme', 'grade': 'A',
                                            'best_trigger_type': 'cfo_hire'}])
    counts, out = _run(client, accounts_module=AccountsStub())
    assert client.updates == []
    assert counts['expired'] == 3 and counts['tombstoned'] == 0
    assert counts['accounts_affected'] == 3 and counts['accounts_refreshed'] == 1
    assert 'DRY RUN — would tombstone 3 expired trigger(s) across 3 account(s)' in out
    assert 'would tombstone  ev1  trigger_expired: 75d old cfo_hire' in out
    assert 'would tombstone  ev4  trigger_expired: 61d old cfo_hire' in out
    assert 'would tombstone  ev7  trigger_expired: 100d old expansion' in out
    # 5a: the best TRIGGER (M&A ev3) and the best GRADE (funding ev2's B) — not ev3's C
    assert ("would refresh account 'acme': trigger cfo_hire → merger_acquisition, grade A → B  "
            "(grade from ev2, trigger from ev3)") in out
    # before/after by event_type: the already-tombstoned row is not 'active'
    assert 'cfo_hire                     2        2       0' in out
    assert 'merger_acquisition           1        0       1' in out
    assert 'total                        6        3       3' in out


def test_apply_tombstones_only_expired_non_tombstoned_rows_and_refreshes_accounts():
    accounts = [{'account_key': 'acme', 'grade': 'A', 'best_trigger_type': 'cfo_hire', 'active': True},
                {'account_key': 'beta', 'grade': 'B', 'best_trigger_type': 'cfo_hire', 'active': True}]
    client = FakeClient(FIXTURE, accounts=accounts)
    counts, out = _run(client, apply=True, accounts_module=AccountsStub())
    assert counts['tombstoned'] == 3
    ev_updates = dict(client.event_updates())
    assert set(ev_updates) == {'ev1', 'ev4', 'ev7'}
    for eid, pl in ev_updates.items():
        assert set(pl) == {'blocked_at', 'blocked_reason'}
        assert pl['blocked_reason'].startswith('trigger_expired: ')
        assert pl['blocked_at'] == NOW.isoformat()
    # the rows themselves keep their verify_state / grade (only hidden)
    ev1 = next(r for r in client.events if r['id'] == 'ev1')
    assert ev1['verify_state'] == 'verified' and ev1['grade'] == 'B' and ev1['blocked_at']
    # accounts: acme re-selects its best remaining trigger; beta is cleared, not deactivated;
    # eps has no accounts row and is skipped
    acct = client.account_updates()
    assert set(acct) == {'acme', 'beta'}
    assert acct['acme']['best_trigger_type'] == 'merger_acquisition'
    assert acct['acme']['best_trigger_event_id'] == 'ev3'
    assert acct['acme']['grade'] == 'B' and acct['acme']['graded_event_id'] == 'ev2'     # 5a
    assert acct['beta']['best_trigger_type'] is None and acct['beta']['grade'] is None
    assert 'active' not in acct['beta']
    assert counts['accounts_refreshed'] == 1 and counts['accounts_cleared'] == 1
    assert 'APPLIED — tombstoned 3 expired trigger(s); accounts refreshed 1, cleared 1' in out
    # idempotent: a second pass finds nothing
    counts2, out2 = _run(client, apply=True, accounts_module=AccountsStub())
    assert counts2['expired'] == 0 and 'No expired triggers — nothing to do.' in out2


def test_without_the_accounts_table_only_events_are_touched():
    client = FakeClient(FIXTURE, accounts=[{'account_key': 'acme'}])
    counts, out = _run(client, apply=True, accounts_module=AccountsStub(present=False))
    assert counts['tombstoned'] == 3 and client.account_updates() == {}
    assert client.account_reads == 0 and 'accounts table not live' in out
    counts, out = _run(FakeClient(FIXTURE), apply=True, accounts_module=None)
    assert counts['tombstoned'] == 3 and '(accounts table not live)' in out


def test_runs_without_the_typed_columns_and_with_grace():
    legacy = [{k: v for k, v in r.items() if k in BASE} for r in FIXTURE]
    client = FakeClient(legacy, columns=BASE)
    counts, out = _run(client, accounts_module=None)
    assert 'typed columns present: none' in out
    # ev1 (75d cfo_hire), ev4 (61d cfo_hire) and ev7 — whose typed expiry is
    # gone, so its 90d expansion shelf life at 100d decides — expire
    assert counts['expired'] == 3
    assert client.event_updates() == []
    # grace: 10 days keeps ev4 (61 < 70) and ev7 (100 is not > 100); 20 keeps all
    counts, _ = _run(client, grace_days=10, accounts_module=None)
    assert counts['expired'] == 1
    counts, _ = _run(client, grace_days=20, accounts_module=None)
    assert counts['expired'] == 0


def test_real_accounts_module_is_read_only_when_its_probe_says_no(monkeypatch):
    if _real_accounts is None:
        pytest.skip('src/pipeline/accounts.py not present')
    monkeypatch.setattr(_real_accounts, 'probe_accounts', lambda client: False)
    client = FakeClient(FIXTURE, accounts=[{'account_key': 'acme'}])
    counts, out = _run(client, apply=True)               # the default module
    assert counts['tombstoned'] == 3 and client.account_updates() == {}
    assert 'accounts table not live' in out
    # the real priority tuple is understood by the ranker
    assert xt.trigger_rank('cfo_hire', _real_accounts.TRIGGER_PRIORITY) == 0
    assert xt.trigger_rank('funding', _real_accounts.TRIGGER_PRIORITY) > \
        xt.trigger_rank('merger_acquisition', _real_accounts.TRIGGER_PRIORITY)


def test_fetch_pages_and_never_reads_tombstoned_rows():
    many = [_row(i, 'funding', 1) for i in range(1, 1203)] + [_row(9999, 'funding', 1, blocked_at='x')]
    client = FakeClient(many)
    rows = xt.fetch_active_events(client, set(), batch=500)
    assert len(rows) == 1202 and all(not r.get('blocked_at') for r in rows)
    assert len(client.reads) == 3


# ── Review 2026-09-08 (Phase 4): 5a / 5b / 5d / 5f ──────────────────────────
def test_5a_refresh_never_writes_a_none_grade_while_a_graded_live_event_remains():
    prio = AccountsStub.TRIGGER_PRIORITY
    # the freshest, highest-priority remaining row is a QUEUED cfo_hire (not enriched, no grade)
    queued = _row(2, 'cfo_hire', 3, grade=None, enriched_at=None)
    graded = _row(3, 'funding', 40, grade='B', numeric_score=5, enriched_at=_days_ago(39))
    rows = [queued, graded]
    # the trigger prefers the ENRICHED row; the grade is the best live grade
    assert xt.best_remaining(rows, prio)['id'] == 'ev3'
    assert xt.best_graded(rows)['id'] == 'ev3'
    pl = xt.account_refresh_payload(xt.best_remaining(rows, prio), NOW, graded=xt.best_graded(rows))
    assert pl['best_trigger_event_id'] == 'ev3' and pl['grade'] == 'B'
    assert pl['graded_event_id'] == 'ev3' and pl['graded_at'] == _days_ago(39)          # 5c
    # two enriched rows: the trigger and the grade may come from different events
    ma = _row(4, 'merger_acquisition', 10, grade='C', numeric_score=3, enriched_at=_days_ago(9))
    rows = [graded, ma]
    best, g = xt.best_remaining(rows, prio), xt.best_graded(rows)
    assert best['id'] == 'ev4' and g['id'] == 'ev3'
    pl = xt.account_refresh_payload(best, NOW, graded=g)
    assert pl['best_trigger_type'] == 'merger_acquisition' and pl['best_trigger_event_id'] == 'ev4'
    assert pl['grade'] == 'B' and pl['graded_event_id'] == 'ev3' and pl['hashtags'] == ['#NewCFO']
    # only a queued row left: the trigger is real, the old grade is gone (no graded live event)
    pl = xt.account_refresh_payload(xt.best_remaining([queued], prio), NOW, graded=xt.best_graded([queued]))
    assert pl['best_trigger_event_id'] == 'ev2' and pl['grade'] is None and pl['graded_event_id'] is None
    # 'Unable to Grade' is not a grade
    assert xt.best_graded([_row(5, 'funding', 1, grade='Unable to Grade')]) is None
    assert not xt.is_graded({'grade': None}) and xt.is_graded({'grade': 'b'})
    assert xt.is_enriched({'enriched_at': 'x'}) and xt.is_enriched({'grade': 'A'}) and not xt.is_enriched({})


def test_5a_best_graded_ranks_by_grade_then_score_then_recency():
    a_old = _row(1, 'funding', 50, grade='A', numeric_score=8)
    b_new = _row(2, 'cfo_hire', 1, grade='B', numeric_score=7)
    assert xt.best_graded([b_new, a_old])['id'] == 'ev1'                     # grade first
    a_hi = _row(3, 'funding', 60, grade='A', numeric_score=10)
    assert xt.best_graded([a_old, a_hi])['id'] == 'ev3'                      # then score
    a_new = _row(4, 'funding', 5, grade='A', numeric_score=8)
    assert xt.best_graded([a_old, a_new])['id'] == 'ev4'                     # then recency
    assert xt.best_graded([a_old, _row(5, 'funding', 1, grade='A', blocked_at='x')])['id'] == 'ev1'
    assert xt.GRADE_RANK == {'A': 0, 'B': 1, 'C': 2, 'D': 3}


def test_5a_run_keeps_the_grade_of_the_remaining_graded_event(monkeypatch):
    # the expired cfo_hire carried the account's A; a queued cfo_hire and a graded funding remain
    rows = [_row(1, 'cfo_hire', 75, account_key='acme', grade='A', numeric_score=9),
            _row(2, 'cfo_hire', 3, account_key='acme', grade=None, enriched_at=None),
            _row(3, 'funding', 40, account_key='acme', grade='B', numeric_score=5,
                 enriched_at=_days_ago(39))]
    client = FakeClient(rows, accounts=[{'account_key': 'acme', 'grade': 'A', 'best_trigger_type': 'cfo_hire',
                                         'best_trigger_event_id': 'ev1'}])
    counts, out = _run(client, apply=True, accounts_module=AccountsStub())
    pl = client.account_updates()['acme']
    assert pl['best_trigger_event_id'] == 'ev3' and pl['best_trigger_type'] == 'funding'
    assert pl['grade'] == 'B' and pl['graded_event_id'] == 'ev3' and pl['graded_at'] == _days_ago(39)
    assert "grade A → B" in out and counts['accounts_refreshed'] == 1


def test_5b_accounts_whose_best_trigger_is_an_expired_event_are_refreshed_too():
    # 'zeta' was the M&A target of ev1 (touched facts-only): it points at ev1 but ev1's
    # chosen account is acme. It has no live events of its own → cleared, stays active.
    accounts = [{'account_key': 'acme', 'grade': 'A', 'best_trigger_type': 'cfo_hire'},
                {'account_key': 'beta', 'grade': 'B', 'best_trigger_type': 'cfo_hire'},
                {'account_key': 'zeta', 'grade': 'B', 'best_trigger_type': 'cfo_hire',
                 'best_trigger_event_id': 'ev1', 'active': True},
                {'account_key': 'theta', 'grade': 'B', 'best_trigger_type': 'funding',
                 'best_trigger_event_id': 'ev6'}]                     # ev6 is live: untouched
    client = FakeClient(FIXTURE, accounts=accounts)
    counts, out = _run(client, apply=True, accounts_module=AccountsStub())
    acct = client.account_updates()
    assert set(acct) == {'acme', 'beta', 'zeta'}
    assert acct['zeta']['best_trigger_event_id'] is None and acct['zeta']['grade'] is None
    assert 'active' not in acct['zeta']
    assert counts['accounts_affected'] == 4 and counts['accounts_cleared'] == 2
    assert "+ 1 account(s) whose best trigger is an expired event: 'zeta'" in out
    # dry run: read, reported, nothing written
    client = FakeClient(FIXTURE, accounts=accounts)
    counts, out = _run(client, accounts_module=AccountsStub())
    assert client.updates == [] and "would refresh account 'zeta'" in out


def test_5d_apply_yields_when_the_enrichment_lock_is_held(monkeypatch, tmp_path, capsys):
    lock_path = str(tmp_path / 'enrichment.lock')
    monkeypatch.setattr(xt, 'LOCK_PATH', lock_path)
    holder = RunLock(lock_path)
    assert holder.acquire()
    try:
        # main() checks the lock BEFORE reading .env or building a client: nothing else happens
        assert xt.main(['--apply']) == 0
        out = capsys.readouterr().out
        assert 'expiry skipped, nothing written' in out and lock_path in out
        assert xt.take_apply_lock(lock_path) is None
    finally:
        holder.release()
    lock = xt.take_apply_lock(lock_path)
    assert lock is not None
    lock.release()
    assert 'expiry skipped' in xt.__doc__ or 'run lock' in xt.__doc__


def test_5f_run_reverify_wrapper_surfaces_an_expiry_failure():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, 'run_reverify.sh')
    assert subprocess.run(['bash', '-n', path], capture_output=True).returncode == 0
    text = open(path, encoding='utf-8').read()
    assert 'EXPIRE_RC=0' in text                                   # defined even when the script is absent
    assert "Expiry FAILED (exit %s) — see logs/reverify.log" in text
    assert 'if [ "$EXPIRE_RC" -ne 0 ]; then' in text
    assert 'exit $FINAL_RC' in text and 'FINAL_RC=$EXPIRE_RC' in text
    assert 'exit $EXIT_CODE' not in text
    # the failure prefix leads the message and the wrapper exits non-zero when
    # only the expiry failed — exercised on the tail of the script with the
    # two commands stubbed
    tail = text[text.index('SUMMARY='):]
    harness = ('TMP_OUT=$(mktemp); LOG=/dev/null; ALERT_ENV=x; EXIT_CODE=0; EXPIRE_RC=3; '
               'EXPIRE_LINE="tombstoned 2"; echo "Done — enriched: 1" > "$TMP_OUT"; '
               'mattermost_notify() { printf "%s" "$2" > "$MSG_OUT"; }\n' + tail)
    msg_out = os.path.join(root, 'logs', '.test_5f_msg')
    try:
        rc = subprocess.run(['bash', '-c', harness], capture_output=True, text=True,
                            env=dict(os.environ, MSG_OUT=msg_out)).returncode
        posted = open(msg_out, encoding='utf-8').read()
    finally:
        if os.path.exists(msg_out):
            os.remove(msg_out)
    assert rc == 3
    assert posted.startswith('⚠️ Expiry FAILED (exit 3) — see logs/reverify.log\n🔁 Daily re-verify pass')
    assert '⏳ Expiry: tombstoned 2' in posted
