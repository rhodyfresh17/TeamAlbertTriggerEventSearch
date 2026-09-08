"""Account-keyed search cache + negative cache for TeamAlbert v2 (M1).

WHY this exists (A.J. 2026-09-07, Phase 2 plan): the Phase-1 cache in
enrichment_scout.py keys raw search results on `name.lower()||industry_hint`
where the hint is free text lifted from an article, so the same company
rarely hits twice, and an account that returned nothing is re-searched on
every new event. This module keys everything on the deterministic
`account_key` from src.pipeline.gates and adds:

  * search_cache          — raw search-results dict per (account_key, kind),
                            TTL per kind (KIND_TTL_DAYS).
  * account_firmographics — one merged payload per account, each field
                            stamped with its own time so slow-moving facts
                            (url, hq) outlive fast-moving ones (revenue).
  * negative_cache        — "we looked and found nothing" with a 7/30/90-day
                            backoff ladder, and a 'scrape' vs 'paid' rung so
                            a free Firecrawl miss never blocks a later Tavily
                            attempt, while a paid miss blocks both.

Rules: no network; short-lived sqlite3 connections (the DB file is shared
with the scraper tables); EVERY method swallows sqlite errors and returns
the miss value — a cache failure must never break an enrichment run.
`now` is injectable (naive UTC, like datetime.utcnow() elsewhere in the repo).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Backoff ladder for negative entries: 1st empty -> 7d, 2nd -> 30d, 3rd+ -> 90d.
NEGATIVE_BACKOFF_DAYS = (7, 30, 90)

# Raw search-result TTL per search kind. Firmographics move slowly; 990s
# are annual filings; AUM/complexity are structural.
KIND_TTL_DAYS = {
    'firmographic':    90,
    'zoominfo':        90,
    'aum':             180,
    'complexity':      180,
    'funding_history': 90,
    'nonprofit_990':   365,
    # Domain resolution (Phase 3 B4, research 2026-09-08): Clearbit is
    # undocumented and cacheable 30d; FDIC/oracle tables refresh monthly;
    # SEC websites basically never move; guesses are low-trust.
    'domain:clearbit': 30,
    'domain:fdic':     90,
    'domain:sec':      365,
    'domain:oracle':   180,
    'domain:guess':    30,
}
DEFAULT_KIND_TTL_DAYS = 90

# Per-field TTL for the merged firmographic payload.
FIELD_TTL_DAYS = {
    'url':            365,
    'industry':       365,
    'zi_subindustry': 365,
    'linkedin':       365,
    'hq':             365,
    'size':           180,
    'revenue':        90,
    'revenue_source': 90,
    # Identity fields from domains.resolve() — as slow-moving as `url`.
    'domain':            365,
    'domain_method':     365,
    'domain_confidence': 365,
    'aliases':           365,
}
DEFAULT_FIELD_TTL_DAYS = 90

_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS search_cache (
        account_key  TEXT NOT NULL,
        kind         TEXT NOT NULL,
        results_json TEXT,
        cached_at    TEXT,
        PRIMARY KEY (account_key, kind)
    )''',
    '''CREATE TABLE IF NOT EXISTS account_firmographics (
        account_key  TEXT PRIMARY KEY,
        payload_json TEXT,
        updated_at   TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS negative_cache (
        account_key  TEXT NOT NULL,
        kind         TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        rung         TEXT,
        last_attempt TEXT,
        retry_after  TEXT,
        PRIMARY KEY (account_key, kind)
    )''',
)


def _utcnow(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.utcnow()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Tolerant ISO parse — a bad timestamp is treated as 'unknown', never raised."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    # Stored values are naive UTC; strip tzinfo if someone wrote an aware one.
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _is_fresh(stamp: Any, ttl_days: int, now: datetime) -> bool:
    ts = _parse_iso(stamp)
    if ts is None:
        return False
    return (now - ts) < timedelta(days=ttl_days)


class AccountCache:
    """Account-keyed cache over a shared SQLite file. Every public method is
    fail-soft: sqlite trouble logs at debug and returns the miss value."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        try:
            conn = self._connect()
            try:
                for stmt in _SCHEMA:
                    conn.execute(stmt)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            # A corrupt/unwritable path degrades to "always miss" — callers keep going.
            log.debug('AccountCache init failed for %s: %s', db_path, e)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _query_one(self, sql: str, params: tuple) -> Optional[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _execute(self, sql: str, params: tuple) -> None:
        conn = self._connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    # ── raw search results ──────────────────────────────────────────────────

    def get_search(self, account_key: str, kind: str = 'firmographic',
                   now: Optional[datetime] = None) -> Optional[dict]:
        """Cached raw search-results dict, or None if missing/stale/unreadable."""
        try:
            row = self._query_one(
                'SELECT results_json, cached_at FROM search_cache '
                'WHERE account_key = ? AND kind = ?', (account_key, kind))
            if not row:
                return None
            ttl = KIND_TTL_DAYS.get(kind, DEFAULT_KIND_TTL_DAYS)
            if not _is_fresh(row['cached_at'], ttl, _utcnow(now)):
                return None
            data = json.loads(row['results_json'] or 'null')
            return data if isinstance(data, dict) else None
        except (sqlite3.Error, ValueError, TypeError) as e:
            log.debug('search_cache get failed (%s/%s): %s', account_key, kind, e)
            return None

    def set_search(self, account_key: str, kind: str, results: Any,
                   now: Optional[datetime] = None) -> None:
        """Store raw search results. Empties are NOT stored here — that is the
        negative cache's job (record_empty), which carries the backoff ladder."""
        if not isinstance(results, dict) or not results.get('results'):
            return
        try:
            self._execute(
                'INSERT OR REPLACE INTO search_cache '
                '(account_key, kind, results_json, cached_at) VALUES (?, ?, ?, ?)',
                (account_key, kind, json.dumps(results), _utcnow(now).isoformat()))
        except (sqlite3.Error, ValueError, TypeError) as e:
            log.debug('search_cache set failed (%s/%s): %s', account_key, kind, e)

    # ── merged firmographics ────────────────────────────────────────────────

    def _load_payload(self, account_key: str) -> Dict[str, dict]:
        row = self._query_one(
            'SELECT payload_json FROM account_firmographics WHERE account_key = ?',
            (account_key,))
        if not row or not row['payload_json']:
            return {}
        data = json.loads(row['payload_json'])
        return data if isinstance(data, dict) else {}

    def get_firmographics(self, account_key: str,
                          now: Optional[datetime] = None) -> Optional[dict]:
        """{field: value} with per-field-expired entries dropped; None if nothing fresh."""
        try:
            payload = self._load_payload(account_key)
            current = _utcnow(now)
            fresh: Dict[str, Any] = {}
            for field, entry in payload.items():
                if not isinstance(entry, dict):
                    continue
                ttl = FIELD_TTL_DAYS.get(field, DEFAULT_FIELD_TTL_DAYS)
                if _is_fresh(entry.get('t'), ttl, current):
                    fresh[field] = entry.get('v')
            return fresh or None
        except (sqlite3.Error, ValueError, TypeError) as e:
            log.debug('firmographics get failed (%s): %s', account_key, e)
            return None

    def set_firmographics(self, account_key: str, payload: dict,
                          now: Optional[datetime] = None) -> None:
        """Merge non-null fields into the stored payload, each stamped with
        its own time. A None in `payload` never clobbers an existing value —
        a later search that couldn't find revenue must not erase the revenue
        an earlier one found."""
        if not isinstance(payload, dict):
            return
        stamp = _utcnow(now).isoformat()
        updates = {k: {'v': v, 't': stamp} for k, v in payload.items() if v is not None}
        if not updates:
            return
        try:
            merged = self._load_payload(account_key)
            merged.update(updates)
            self._execute(
                'INSERT OR REPLACE INTO account_firmographics '
                '(account_key, payload_json, updated_at) VALUES (?, ?, ?)',
                (account_key, json.dumps(merged), stamp))
        except (sqlite3.Error, ValueError, TypeError) as e:
            log.debug('firmographics set failed (%s): %s', account_key, e)

    # ── negative cache ──────────────────────────────────────────────────────

    def should_skip(self, account_key: str, kind: str = 'firmographic',
                    want_paid: bool = False, now: Optional[datetime] = None) -> bool:
        """True iff an active negative entry blocks this attempt.

        A 'scrape' rung (free Firecrawl found nothing) only blocks further
        free attempts — a caller willing to spend Tavily (want_paid=True)
        gets through. A 'paid' rung blocks everything until retry_after."""
        try:
            row = self._query_one(
                'SELECT rung, retry_after FROM negative_cache '
                'WHERE account_key = ? AND kind = ?', (account_key, kind))
            if not row:
                return False
            retry_after = _parse_iso(row['retry_after'])
            if retry_after is None or retry_after <= _utcnow(now):
                return False
            return row['rung'] == 'paid' or not want_paid
        except (sqlite3.Error, ValueError, TypeError) as e:
            log.debug('negative_cache check failed (%s/%s): %s', account_key, kind, e)
            return False

    def record_empty(self, account_key: str, kind: str = 'firmographic',
                     rung: str = 'scrape', now: Optional[datetime] = None) -> str:
        """Bump the attempt counter and push retry_after out along the ladder.
        Rung is sticky upward: once 'paid' has struck out, a later scrape-only
        miss keeps the entry at 'paid'. Returns the new retry_after ISO."""
        current = _utcnow(now)
        attempts = 1
        new_rung = 'paid' if rung == 'paid' else 'scrape'
        try:
            row = self._query_one(
                'SELECT attempts, rung FROM negative_cache '
                'WHERE account_key = ? AND kind = ?', (account_key, kind))
            if row:
                attempts = int(row['attempts'] or 0) + 1
                if row['rung'] == 'paid':
                    new_rung = 'paid'
            days = NEGATIVE_BACKOFF_DAYS[min(attempts - 1, len(NEGATIVE_BACKOFF_DAYS) - 1)]
            retry_after = (current + timedelta(days=days)).isoformat()
            self._execute(
                'INSERT OR REPLACE INTO negative_cache '
                '(account_key, kind, attempts, rung, last_attempt, retry_after) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (account_key, kind, attempts, new_rung, current.isoformat(), retry_after))
            return retry_after
        except (sqlite3.Error, ValueError, TypeError) as e:
            log.debug('negative_cache record failed (%s/%s): %s', account_key, kind, e)
            # Miss value: behave as a first strike so the caller's flow is unchanged.
            return (current + timedelta(days=NEGATIVE_BACKOFF_DAYS[0])).isoformat()

    def clear_negative(self, account_key: str, kind: str = 'firmographic') -> None:
        """Drop the negative entry — call when a search finally finds something."""
        try:
            self._execute('DELETE FROM negative_cache WHERE account_key = ? AND kind = ?',
                          (account_key, kind))
        except sqlite3.Error as e:
            log.debug('negative_cache clear failed (%s/%s): %s', account_key, kind, e)

    # ── observability ───────────────────────────────────────────────────────

    def stats(self, now: Optional[datetime] = None) -> dict:
        """Row counts per table plus how many negative entries are still active."""
        out = {'search_cache': 0, 'account_firmographics': 0,
               'negative_cache': 0, 'negative_active': 0}
        try:
            conn = self._connect()
            try:
                for table in ('search_cache', 'account_firmographics', 'negative_cache'):
                    out[table] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                out['negative_active'] = conn.execute(
                    'SELECT COUNT(*) FROM negative_cache WHERE retry_after > ?',
                    (_utcnow(now).isoformat(),)).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.debug('cache stats failed: %s', e)
        return out
