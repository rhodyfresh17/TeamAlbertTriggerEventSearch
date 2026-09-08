#!/usr/bin/env python3
"""Refresh the free oracle tables in state/oracles.db (Phase 3 slice B2).

    venv/bin/python scripts/refresh_oracles.py --source all
    venv/bin/python scripts/refresh_oracles.py --source ria --dry-run
    venv/bin/python scripts/refresh_oracles.py --source ria --feed-file /path/IA_FIRM_SEC_Feed_09_01_2026.xml.gz

WHY (A.J. 2026-09-07/08, Phase 3 plan): search is the scarce resource in
enrichment. Two public registries answer territory / vertical / revenue /
url for whole account shapes with ZERO search, but only if they sit on
disk — so this script loads them monthly (run_oracles.sh, the 2nd of the
month 05:00 ET, right before scripts/ria_trigger.py which reads the same
tables; M5, review 2026-09-08: the SEC publishes on the 1st, so the 2nd
is the first day the current compilation is normally up):

  ria   SEC IAPD adviser compilation feed — every SEC-registered adviser
        and exempt reporting adviser (~23.8K firms). Published on the 1st
        (URL dated MM_01_YYYY) as a 7 MB .xml.gz (82 MB of XML) covering
        registrations through the prior month end; research 2026-09-08.
        Downloaded for the CURRENT month, previous month on a 404 (a late
        publication), streamed with xml.etree.iterparse into `ria_firm` —
        never loaded as one string. oracle_meta.source = 'ria'.
  bank  FDIC BankFind institutions API — every active FDIC-insured bank
        (~4.5K) in ONE call (limit=10000, keyless, 120/min) → `bank`.
        oracle_meta.source = 'bank'.

Contract with the other readers of state/oracles.db (the RIA-trigger
engineer's ledger table ria_trigger_emitted lives in the same file): each
source REPLACES ITS OWN TABLE and never deletes or recreates the file. The
rows are built aside in `<table>__incoming` in 1,000-row commits and
swapped in with one short DROP + RENAME transaction (L1, review
2026-09-08) — enrichment readers keep answering from the previous table
throughout instead of timing out behind a parse-long write lock. A failed
refresh drops the incoming table and the previous table stays; the exit
code is non-zero so run_oracles.sh alerts.

Honors state/PAUSE (exit 0, nothing touched). Memory stays bounded: the
feed streams to disk, then streams through the parser 1,000 rows a batch.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from src.pipeline import oracles  # noqa: E402

PAUSE_PATH = os.path.join(_ROOT, 'state', 'PAUSE')
SOURCES = ('ria', 'bank')


def _log(msg: str) -> None:
    print(f'{datetime.now().strftime("%H:%M:%S")}  {msg}', flush=True)


def _counts_line(label: str, c: dict) -> str:
    return (f'{label}: {c.get("total", 0):,} rows (in-territory {c.get("in_territory", 0):,}, '
            f'with website {c.get("with_website", 0):,})')


def _feed_month_from_name(path: str) -> str:
    """'IA_FIRM_SEC_Feed_09_01_2026.xml.gz' → the SEC URL that file came from
    (so oracle_meta.src_url is right even for a --feed-file run)."""
    base = os.path.basename(path)
    try:
        stem = base.replace('IA_FIRM_SEC_Feed_', '').split('.')[0]      # 09_01_2026
        mm, dd, yyyy = stem.split('_')
        return oracles.iapd_feed_url(date(int(yyyy), int(mm), 1))
    except Exception:
        return f'file://{os.path.abspath(path)}'


def refresh_ria(db_path: str, dry_run: bool, feed_file: str = None,
                today: date = None) -> dict:
    """Download (or reuse) the feed and load ria_firm. Returns the counts."""
    tmp_dir = None
    try:
        if feed_file:
            path, url = feed_file, _feed_month_from_name(feed_file)
            _log(f'ria: using local feed {feed_file}')
        else:
            tmp_dir = tempfile.mkdtemp(prefix='iapd_')
            t0 = time.monotonic()
            path, url, month = oracles.download_iapd_feed(tmp_dir, today=today)
            size_mb = os.path.getsize(path) / 1e6
            _log(f'ria: downloaded {os.path.basename(path)} ({size_mb:.1f} MB, '
                 f'{time.monotonic() - t0:.0f}s) — feed month {month:%Y-%m}')
            if today and (month.year, month.month) != (today.year, today.month):
                _log('ria: NOTE this month\'s compilation was not published yet — '
                     'loaded the previous month')
        t0 = time.monotonic()
        counts = oracles.refresh_ria(db_path, source=path, src_url=url, dry_run=dry_run)
        counts['seconds'] = round(time.monotonic() - t0, 1)
        return counts
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def refresh_bank(db_path: str, dry_run: bool) -> dict:
    t0 = time.monotonic()
    counts = oracles.refresh_bank(db_path, dry_run=dry_run)
    counts['seconds'] = round(time.monotonic() - t0, 1)
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Refresh the free oracle tables (SEC IAPD advisers, FDIC banks).')
    ap.add_argument('--source', choices=SOURCES + ('all',), default='all')
    ap.add_argument('--dry-run', action='store_true',
                    help='download/parse and print the counts without writing the DB')
    ap.add_argument('--db', default=oracles.DEFAULT_DB_PATH,
                    help=f'oracles.db path (default {oracles.DEFAULT_DB_PATH})')
    ap.add_argument('--feed-file', default=None,
                    help='use this local IA_FIRM_SEC_Feed_MM_DD_YYYY.xml.gz instead of downloading')
    args = ap.parse_args(argv)

    if os.path.exists(PAUSE_PATH):
        _log(f'PAUSED — {PAUSE_PATH} present; nothing refreshed (exit 0)')
        return 0

    wanted = list(SOURCES) if args.source == 'all' else [args.source]
    tag = 'DRY RUN — ' if args.dry_run else ''
    _log(f'{tag}oracle refresh: {", ".join(wanted)} → {args.db}')
    failures = []
    for src in wanted:
        try:
            if src == 'ria':
                c = refresh_ria(args.db, args.dry_run, feed_file=args.feed_file,
                                today=date.today())
                label = 'ria_firm (SEC IAPD advisers)'
            else:
                c = refresh_bank(args.db, args.dry_run)
                label = f'bank (FDIC BankFind, index {c.get("as_of") or "?"})'
            _log(_counts_line(label, c) + (' — parsed only, not written' if args.dry_run
                                          else f' — written in {c.get("seconds")}s'))
        except Exception as e:      # noqa: BLE001 — report, keep going, exit non-zero
            failures.append(src)
            _log(f'{src}: FAILED — {type(e).__name__}: {str(e)[:300]} '
                 f'(previous table kept)')
    if not args.dry_run:
        for src, m in sorted(oracles.meta(args.db).items()):
            _log(f'oracle_meta: {src} refreshed {m.get("refreshed_at")} ({m.get("rows")} rows)')
    if failures:
        _log(f'refresh finished with errors: {", ".join(failures)}')
        return 1
    _log('refresh done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
