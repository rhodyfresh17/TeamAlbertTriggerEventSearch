"""Phase 3 slice B2 — free structured oracles (src/pipeline/oracles.py and
scripts/refresh_oracles.py).

Fixtures are tiny copies of the real shapes verified live 2026-09-08: a
3-firm IAPD compilation feed (Registered in-territory firm with RAUM /
headcount / website, an ERA, an out-of-territory firm), a 3-bank FDIC
BankFind body (the RI / WA "Washington Trust" pair plus a too-small bank)
and a ProPublica search + organization pair. No network: requests.get is
faked and every live call is asserted.
"""
import gzip
import json
import os
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from scripts import refresh_oracles as ro
from src.pipeline import oracles as o
from src.pipeline.cache import AccountCache

# ── Fixtures ────────────────────────────────────────────────────────────────
IAPD_XML = b'''<?xml version="1.0" encoding="ISO-8859-1"?>
<IAPDFirmSECReport GenOn="2026-09-01">
  <Firms>
    <Firm>
      <Info SECRgnCD="BRO" FirmCrdNb="1001" SECNb="801-123456" BusNm="BEACON HILL CAPITAL ADVISORS" LegalNm="BEACON HILL CAPITAL ADVISORS, LLC" UmbrRgstn="N"/>
      <MainAddr Strt1="1 BEACON ST" City="BOSTON" State="MA" Cntry="United States" PostlCd="02108"/>
      <MailingAddr/>
      <Rgstn FirmType="Registered" St="APPROVED" Dt="2026-08-12"/>
      <NoticeFiled><States RgltrCd="MA" St="FILED" Dt="2026-08-12"/></NoticeFiled>
      <FormInfo>
        <Part1A>
          <Item1 Q1I="Y"><WebAddrs><WebAddr>HTTP://WWW.BEACONHILLCAP.EXAMPLE</WebAddr></WebAddrs></Item1>
          <Item5A TtlEmp="30"/>
          <Item5F Q5F1="Y" Q5F2A="2000000000" Q5F2B="0" Q5F2C="2000000000"/>
        </Part1A>
      </FormInfo>
    </Firm>
    <Firm>
      <Info SECRgnCD="BRO" FirmCrdNb="1002" SECNb="802-120553" BusNm="NUTMEG VENTURES MANAGEMENT" LegalNm="NUTMEG VENTURES MANAGEMENT LLC"/>
      <MainAddr Strt1="1 MAIN ST" City="STAMFORD" State="CT" Cntry="United States" PostlCd="06901"/>
      <Rgstn FirmType="ERA" St="ACTIVE" Dt="2021-02-16"/>
      <FormInfo>
        <Part1A>
          <Item1 Q1I="Y"><WebAddrs><WebAddr>https://www.nutmegvc.example/</WebAddr></WebAddrs></Item1>
        </Part1A>
      </FormInfo>
    </Firm>
    <Firm>
      <Info SECRgnCD="SFRO" FirmCrdNb="1003" SECNb="801-99999" BusNm="GOLDEN STATE ADVISERS" LegalNm="GOLDEN STATE ADVISERS LLC"/>
      <MainAddr Strt1="1 MARKET ST" City="SAN FRANCISCO" State="CA" Cntry="United States" PostlCd="94105"/>
      <Rgstn FirmType="Registered" St="APPROVED" Dt="2019-05-01"/>
      <FormInfo>
        <Part1A>
          <Item1 Q1I="N"><WebAddrs/></Item1>
          <Item5A TtlEmp="120"/>
          <Item5F Q5F1="Y" Q5F2C="9000000000"/>
        </Part1A>
      </FormInfo>
    </Firm>
  </Firms>
</IAPDFirmSECReport>'''

EMPTY_XML = b'''<?xml version="1.0" encoding="ISO-8859-1"?>
<IAPDFirmSECReport GenOn="2026-10-01"><Firms/></IAPDFirmSECReport>'''


def _fdic(name, city, st, cert, asset, web, hcr=None, bkclass='NM'):
    return {'data': {'NAME': name, 'CITY': city, 'STALP': st, 'CERT': cert, 'ASSET': asset,
                     'WEBADDR': web, 'NAMEHCR': hcr, 'BKCLASS': bkclass, 'ACTIVE': 1,
                     'ESTYMD': '01/01/1900', 'ID': str(cert)}, 'score': 0}


FDIC_BODY = {
    'meta': {'total': 3, 'index': {'name': 'institutions_20260904090006',
                                   'createTimestamp': '2026-09-04T11:55:38Z'}},
    'data': [
        _fdic('Washington Trust Bank', 'Spokane', 'WA', 1281, 10536386, 'www.watrust.com',
              'W T B FINANCIAL CORP'),
        _fdic('The Washington Trust Company, of Westerly', 'Westerly', 'RI', 23623, 6551001,
              'www.washtrust.com', 'WASHINGTON TRUST BCORP INC'),
        _fdic('Zorblat Savings Bank', 'Zorblat', 'MA', 90001, 60000,
              'https://www.zorblatsavings.example/', None, 'SB'),
    ],
    'totals': {'count': 3},
}

PP_SEARCH = {
    'total_results': 2,
    'organizations': [
        {'ein': 42103607, 'strein': '04-2103607', 'name': 'Museum Of Fine Arts',
         'city': 'Boston', 'state': 'MA', 'ntee_code': 'A510', 'subseccd': 3, 'score': 130.8},
        {'ein': 861520245, 'strein': '86-1520245',
         'name': 'New Salem Museum And Academy Of Fine Art Inc',
         'city': 'Canton', 'state': 'MA', 'ntee_code': 'A51', 'subseccd': 3, 'score': 82.2},
    ],
    'num_pages': 1, 'cur_page': 0, 'per_page': 25, 'search_query': 'Museum of Fine Arts',
    'selected_state': 'MA', 'api_version': 2,
}
PP_ORG = {
    'organization': {'ein': 42103607, 'name': 'Museum Of Fine Arts', 'city': 'Boston',
                     'state': 'MA', 'ntee_code': 'A510', 'subseccd': None,
                     'revenue_amount': 186630042, 'income_amount': 542008045,
                     'asset_amount': 1584373764, 'tax_period': '2025-06-01'},
    'filings_with_data': [
        {'tax_prd': 202306, 'tax_prd_yr': 2023, 'totrevenue': 99573924,
         'totfuncexpns': 123302516, 'totassetsend': 1382617633},
        {'tax_prd': 202206, 'tax_prd_yr': 2022, 'totrevenue': 141000000,
         'totfuncexpns': 120000000},
    ],
    'filings_without_data': [], 'api_version': 2,
}
# What ProPublica really returns for zero hits: HTTP 404 with THIS body.
PP_ZERO = {'total_results': 0, 'organizations': [], 'num_pages': 0, 'cur_page': 0,
           'per_page': 25, 'search_query': 'Zorblatqx Nonexistent Foundation',
           'selected_state': 'MA', 'api_version': 2}


def _resp(status, body=None, raw=b''):
    ns = SimpleNamespace(status_code=status)
    if body is not None:
        ns.json = lambda: body
    else:
        def _bad():
            raise ValueError('no JSON body')
        ns.json = _bad
    ns.iter_content = lambda chunk_size=1: iter([raw] if raw else [])
    return ns


class FakeHTTP:
    """requests.get stand-in: routes by URL prefix, records every call."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, url, params=None, timeout=None, stream=False, headers=None, **kw):
        self.calls.append((url, dict(params or {})))
        for prefix, answer in self.routes:
            if url.startswith(prefix):
                return answer(url, params) if callable(answer) else answer
        raise AssertionError(f'unexpected HTTP call: {url}')


def _gz(tmp_path, xml=IAPD_XML, name='IA_FIRM_SEC_Feed_09_01_2026.xml.gz'):
    path = tmp_path / name
    with gzip.open(str(path), 'wb') as fh:
        fh.write(xml)
    return str(path)


@pytest.fixture
def db(tmp_path):
    """A refreshed tmp oracles.db (both sources) — the local-adapter fixture."""
    path = str(tmp_path / 'oracles.db')
    o.refresh_ria(path, source=_gz(tmp_path), src_url='https://example.test/feed.gz',
                  now=datetime(2026, 9, 8, 5, 0))
    o.refresh_bank(path, body=FDIC_BODY, now=datetime(2026, 9, 8, 5, 1))
    return path


@pytest.fixture
def cache(tmp_path):
    return AccountCache(str(tmp_path / 'cache.db'))


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(o, 'LIVE_MIN_INTERVAL', 0)
    yield
    o._LIVE['ua'] = None


# ── Refresh ─────────────────────────────────────────────────────────────────
def test_refresh_ria_streams_feed_into_tmp_db(tmp_path):
    path = str(tmp_path / 'oracles.db')
    counts = o.refresh_ria(path, source=_gz(tmp_path), src_url='https://example.test/feed.gz')
    assert counts['total'] == 3 and counts['in_territory'] == 2 and counts['with_website'] == 2
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = {r['crd']: dict(r) for r in conn.execute('SELECT * FROM ria_firm')}
    conn.close()
    beacon = rows[1001]
    assert beacon['business_name'] == 'BEACON HILL CAPITAL ADVISORS'
    assert beacon['legal_name'] == 'BEACON HILL CAPITAL ADVISORS, LLC'
    assert beacon['norm_name'] == 'beacon hill capital advisors'
    assert (beacon['city'], beacon['state'], beacon['country']) == ('BOSTON', 'MA', 'United States')
    assert (beacon['firm_type'], beacon['reg_status'], beacon['reg_date']) == \
        ('Registered', 'APPROVED', '2026-08-12')
    assert beacon['website'] == 'http://www.beaconhillcap.example'
    assert beacon['total_employees'] == 30 and beacon['raum_usd'] == 2_000_000_000
    assert beacon['sec_number'] == '801-123456' and beacon['as_of'] == '2026-09-01'
    era = rows[1002]
    assert era['firm_type'] == 'ERA' and era['raum_usd'] is None and era['total_employees'] is None
    assert rows[1003]['website'] is None and rows[1003]['state'] == 'CA'
    meta = o.meta(path)
    assert meta['ria']['rows'] == 3 and meta['ria']['src_url'] == 'https://example.test/feed.gz'
    assert set(meta) == {'ria'}


def test_refresh_bank_from_body(tmp_path):
    path = str(tmp_path / 'oracles.db')
    counts = o.refresh_bank(path, body=FDIC_BODY)
    assert counts['total'] == 3 and counts['in_territory'] == 2 and counts['with_website'] == 3
    assert counts['as_of'] == '2026-09-04'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute('SELECT * FROM bank WHERE cert = 23623').fetchone())
    conn.close()
    assert row['name'] == 'The Washington Trust Company, of Westerly'
    assert row['norm_name'] == 'washington trust westerly'
    assert (row['city'], row['state'], row['asset_kusd']) == ('Westerly', 'RI', 6551001)
    assert row['holding_co'] == 'WASHINGTON TRUST BCORP INC' and row['active'] == 1
    assert o.meta(path)['bank']['rows'] == 3


def test_refresh_replaces_its_own_tables_only(tmp_path):
    """The RIA-trigger engineer keeps ria_trigger_emitted in the same file:
    a refresh must replace ria_firm / bank in place, never the DB file."""
    path = str(tmp_path / 'oracles.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE ria_trigger_emitted (crd INTEGER PRIMARY KEY, event_id TEXT, '
                 'emitted_at TEXT, reg_date TEXT)')
    conn.execute("INSERT INTO ria_trigger_emitted VALUES (1001, 'ev-abc', '2026-08-05', '2026-08-12')")
    conn.execute('CREATE TABLE bank (cert INTEGER PRIMARY KEY, name TEXT, norm_name TEXT, city TEXT, '
                 'state TEXT, asset_kusd INTEGER, website TEXT, est_date TEXT, holding_co TEXT, '
                 'bkclass TEXT, active INTEGER, as_of TEXT)')
    conn.execute("INSERT INTO bank (cert, name) VALUES (1, 'Stale Bank')")
    conn.commit()
    conn.close()
    inode = os.stat(path).st_ino
    o.refresh_ria(path, source=_gz(tmp_path))
    o.refresh_bank(path, body=FDIC_BODY)
    o.refresh_ria(path, source=_gz(tmp_path))            # twice: replace, not append
    assert os.stat(path).st_ino == inode
    conn = sqlite3.connect(path)
    assert conn.execute('SELECT event_id FROM ria_trigger_emitted WHERE crd = 1001').fetchone() == ('ev-abc',)
    assert conn.execute('SELECT COUNT(*) FROM ria_firm').fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM bank WHERE name = 'Stale Bank'").fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM bank').fetchone()[0] == 3
    assert {r[0] for r in conn.execute('SELECT source FROM oracle_meta')} == {'ria', 'bank'}
    conn.close()


def test_refresh_failure_keeps_previous_table(tmp_path):
    path = str(tmp_path / 'oracles.db')
    o.refresh_ria(path, source=_gz(tmp_path), now=datetime(2026, 9, 1, 5, 0))
    before = o.meta(path)['ria']
    with pytest.raises(RuntimeError):
        o.refresh_ria(path, source=_gz(tmp_path, EMPTY_XML, 'IA_FIRM_SEC_Feed_10_01_2026.xml.gz'),
                      now=datetime(2026, 10, 1, 5, 0))
    conn = sqlite3.connect(path)
    assert conn.execute('SELECT COUNT(*) FROM ria_firm').fetchone()[0] == 3
    conn.close()
    assert o.meta(path)['ria'] == before
    # a bad archive is a failure too, not a silent empty table
    bad = tmp_path / 'IA_FIRM_SEC_Feed_10_01_2026.xml.gz'
    bad.write_bytes(b'not a gzip file')
    with pytest.raises(Exception):
        o.refresh_ria(path, source=str(bad))
    assert o.meta(path)['ria'] == before


def test_refresh_dry_run_writes_nothing(tmp_path):
    path = str(tmp_path / 'oracles.db')
    c = o.refresh_ria(path, source=_gz(tmp_path), dry_run=True)
    assert c == {'source': 'ria', 'total': 3, 'in_territory': 2, 'with_website': 2, 'written': False}
    c = o.refresh_bank(path, body=FDIC_BODY, dry_run=True)
    assert c['total'] == 3 and c['written'] is False
    assert not os.path.exists(path)


def test_iapd_feed_candidates_current_then_previous_month():
    urls = [u for u, _ in o.iapd_feed_candidates(date(2026, 9, 8))]
    assert urls == [
        'https://reports.adviserinfo.sec.gov/reports/CompilationReports/IA_FIRM_SEC_Feed_09_01_2026.xml.gz',
        'https://reports.adviserinfo.sec.gov/reports/CompilationReports/IA_FIRM_SEC_Feed_08_01_2026.xml.gz',
    ]
    assert [m for _, m in o.iapd_feed_candidates(date(2027, 1, 3))] == [date(2027, 1, 1), date(2026, 12, 1)]


def test_download_falls_back_to_previous_month_on_404(tmp_path, monkeypatch):
    gz_bytes = gzip.compress(IAPD_XML)
    http = FakeHTTP(
        ('https://reports.adviserinfo.sec.gov/reports/CompilationReports/IA_FIRM_SEC_Feed_09_01_2026',
         _resp(404)),
        ('https://reports.adviserinfo.sec.gov/reports/CompilationReports/IA_FIRM_SEC_Feed_08_01_2026',
         _resp(200, raw=gz_bytes)),
    )
    monkeypatch.setattr(o.requests, 'get', http)
    path, url, month = o.download_iapd_feed(str(tmp_path / 'dl'), today=date(2026, 9, 8))
    assert month == date(2026, 8, 1) and url.endswith('IA_FIRM_SEC_Feed_08_01_2026.xml.gz')
    assert os.path.basename(path) == 'IA_FIRM_SEC_Feed_08_01_2026.xml.gz'
    assert len(http.calls) == 2 and open(path, 'rb').read() == gz_bytes
    assert not os.path.exists(path + '.part')
    # both months missing → a real error, nothing left on disk
    monkeypatch.setattr(o.requests, 'get', FakeHTTP(('https://', _resp(404))))
    with pytest.raises(RuntimeError):
        o.download_iapd_feed(str(tmp_path / 'dl2'), today=date(2026, 9, 8))


def test_refresh_script_cli(tmp_path, monkeypatch, capsys):
    feed = _gz(tmp_path)
    dbp = str(tmp_path / 'cli.db')
    assert ro.main(['--source', 'ria', '--feed-file', feed, '--db', dbp]) == 0
    out = capsys.readouterr().out
    assert '3 rows (in-territory 2, with website 2)' in out and 'oracle_meta: ria' in out
    assert o.meta(dbp)['ria']['src_url'].endswith('IA_FIRM_SEC_Feed_09_01_2026.xml.gz')
    # --dry-run parses and prints but writes nothing
    dry = str(tmp_path / 'dry.db')
    assert ro.main(['--source', 'ria', '--feed-file', feed, '--db', dry, '--dry-run']) == 0
    assert 'parsed only' in capsys.readouterr().out and not os.path.exists(dry)
    # bank source through a faked FDIC call
    monkeypatch.setattr(o.requests, 'get', FakeHTTP(('https://api.fdic.gov/', _resp(200, FDIC_BODY))))
    assert ro.main(['--source', 'bank', '--db', dbp]) == 0
    assert 'bank (FDIC BankFind, index 2026-09-04): 3 rows' in capsys.readouterr().out
    # a failing source → non-zero exit, the other source still refreshed, DB intact
    monkeypatch.setattr(o.requests, 'get', FakeHTTP(('https://api.fdic.gov/', _resp(503))))
    assert ro.main(['--source', 'all', '--feed-file', feed, '--db', dbp]) == 1
    out = capsys.readouterr().out
    assert 'bank: FAILED' in out and 'previous table kept' in out and 'refresh finished with errors' in out
    assert o.meta(dbp)['bank']['rows'] == 3
    # state/PAUSE → exit 0 and nothing touched
    pause = tmp_path / 'PAUSE'
    pause.write_text('')
    monkeypatch.setattr(ro, 'PAUSE_PATH', str(pause))
    paused = str(tmp_path / 'paused.db')
    assert ro.main(['--source', 'all', '--feed-file', feed, '--db', paused]) == 0
    assert 'PAUSED' in capsys.readouterr().out and not os.path.exists(paused)


# ── Normalization + matching ────────────────────────────────────────────────
def test_normalize_name_rules():
    assert o.normalize_name('The Washington Trust Company, of Westerly') == 'washington trust westerly'
    assert o.normalize_name('Washington Trust Bancorp, Inc.') == 'washington trust bancorp'
    assert o.normalize_name("Smith & Wesson Co.") == 'smith wesson'     # & → and → connective dropped
    assert o.normalize_name('Smith and Wesson Company') == 'smith wesson'
    assert o.normalize_name('Citizens Bank, N.A.') == 'citizens bank national association'
    assert o.normalize_name('Navy FCU') == 'navy federal credit union'
    assert o.normalize_name("Children's Hospital Trust") == 'childrens hospital trust'
    assert o.normalize_name('') == '' and o.normalize_name(None) == ''


def test_ymca_alias_matches_irs_legal_name():
    assert o.normalize_name('YMCA of Greater Boston') == \
        o.normalize_name("Young Men's Christian Association of Greater Boston")
    assert o.score_names(o.normalize_name('YMCA of Greater Boston'),
                         o.normalize_name('Young Mens Christian Association Of Greater Boston Inc'),
                         'MA', 'MA') == 1.0


def test_score_names_geography():
    q = o.normalize_name('Washington Trust')
    ri = o.normalize_name('The Washington Trust Company, of Westerly')
    wa = o.normalize_name('Washington Trust Bank')
    assert o.score_names(q, ri) == o.score_names(q, wa) == pytest.approx(0.9)   # containment
    assert o.score_names(q, ri, 'RI', 'RI') == 1.0
    assert o.score_names(q, wa, 'RI', 'WA') == pytest.approx(0.6)
    assert o.score_names(q, ri, None, 'RI', 'Westerly', 'WESTERLY') == 1.0     # city agrees
    assert o.score_names('washington', ri) < o.MATCH_THRESHOLD                  # one token never contains
    # a middle insertion is a different firm, and a shared state must not rescue it
    weak = o.score_names('boston partners', 'boston millennia partners', 'MA', 'MA')
    assert weak < o.MATCH_THRESHOLD and weak == o.score_names('boston partners', 'boston millennia partners')
    assert o.score_names('citizens bank', 'citizens bank national association', 'RI', 'RI') == pytest.approx(0.9)
    # an affiliate marker on one side only is a different entity (the YMCA realty arm)
    ymca = o.normalize_name('YMCA of Greater Boston')
    assert o.score_names(ymca, o.normalize_name('Ymca Of Greater Boston Realty Corp'), 'MA', 'MA') < o.MATCH_THRESHOLD
    assert o.score_names(ymca, o.normalize_name('Young Mens Christian Association Of Greater Boston Inc'), 'MA', 'MA') == 1.0
    assert o.score_names('museum fine arts', 'museum fine arts foundation') < o.MATCH_THRESHOLD
    assert o.score_names('gates foundation', 'gates foundation') == 1.0        # both sides: no penalty
    assert o.expand_aliases('YMCA of Greater Boston') == 'young mens christian association of greater boston'
    assert o.expand_aliases("Citizens Bank, N.A.") == 'citizens bank, national association'
    assert o.pick_best([{'name': 'a', 'state': 'RI', 'score': 0.9},
                        {'name': 'b', 'state': 'WA', 'score': 0.9}]) is None    # cross-state tie
    # H1 a (review 2026-09-08): a same-state tie between DIFFERENT registrants
    # is refused too — the old rule kept the top one (= the biggest bank).
    assert o.pick_best([{'name': 'a', 'state': 'RI', 'score': 0.9, 'source_id': '1'},
                        {'name': 'b', 'state': 'RI', 'score': 0.9, 'source_id': '2'}]) is None
    assert o.pick_best([{'name': 'a', 'state': 'RI', 'score': 0.9},
                        {'name': 'b', 'state': 'RI', 'score': 0.9}]) is None   # no ids = different
    # …the same registrant scored twice (business + legal name) is not a tie
    assert o.pick_best([{'name': 'a', 'state': 'RI', 'score': 0.9, 'source_id': '1'},
                        {'name': 'a llc', 'state': 'RI', 'score': 0.9, 'source_id': '1'}])['name'] == 'a'
    # H1 b: both clip to 1.0 with the bonus; the RAW score ranks exact first
    assert o.pick_best([{'name': 'contained', 'state': 'VA', 'score': 1.0, 'raw': 0.9, 'source_id': '2'},
                        {'name': 'exact', 'state': 'VA', 'score': 1.0, 'raw': 1.0, 'source_id': '1'}])['name'] == 'exact'
    # H1 d: a difflib-only raw score never earns the geography bonus
    beacon = o.score_detail('beacon hill partners', 'beacon capital partners', 'MA', 'MA')
    assert beacon['channel'] == 'difflib' and 0.83 < beacon['raw'] < 0.85
    assert beacon['score'] == beacon['raw'] < o.MATCH_THRESHOLD
    assert o.score_detail(q, ri, 'RI', 'RI') == {'score': 1.0, 'raw': 0.9, 'channel': 'contain'}
    assert o.score_detail('zorblat bank', 'zorblat bank')['channel'] == 'exact'
    # a difflib score that clears the bar on its own still counts (holding co spelling)
    holdco = o.score_detail('washington trust bancorp', 'washington trust bcorp', 'RI', 'RI')
    assert holdco['channel'] == 'difflib' and holdco['score'] == holdco['raw'] >= o.MATCH_THRESHOLD


def test_name_shapes_and_zi_routing():
    assert o.looks_like_ria('Beacon Hill Capital Advisors') and o.looks_like_ria('Nutmeg Wealth Partners')
    # L4 (review 2026-09-08): 'partners' / 'capital' / 'ventures' / 'management'
    # alone are the WEAK shape — the adapter runs strict (exact, in-state)
    assert o.ria_shape('Nutmeg Ventures Management') == 'weak' and o.ria_shape('MK Capital') == 'weak'
    assert o.ria_shape('Zorblat Law Partners') == 'weak' and not o.looks_like_ria('Zorblat Law Partners')
    assert o.ria_shape('Beacon Hill Capital Advisors') == 'strong'
    assert not o.looks_like_ria('Zorblat Robotics Inc') and o.ria_shape('Zorblat Robotics Inc') is None
    assert o.looks_like_bank('Washington Trust') and o.looks_like_bank('First Zorblat Bancorp')
    assert not o.looks_like_bank('Zorblat Charitable Trust') and not o.looks_like_bank('Navy Federal Credit Union')
    assert o.looks_like_npo('YMCA of Greater Boston') and o.looks_like_npo('Navy Federal Credit Union')
    assert not o.looks_like_npo('Zorblat Foundation LLC')
    assert o.kind_for_zi('Banking', 'Zorblat Bank') == 'bank'
    assert o.kind_for_zi('Banking', 'Zorblat Federal Credit Union') == 'npo'
    assert o.kind_for_zi('Lending & Brokerage') == 'ria' and o.kind_for_zi('Venture Capital & Private Equity') == 'ria'
    assert o.kind_for_zi('Non-Profit Organizations & Charitable Foundations') == 'npo'
    assert o.kind_for_zi('OTHER') is None
    for zi in ('Banking', 'Non-Profit & Charitable Organizations',
               'Non-Profit Organizations & Charitable Foundations', 'Lending & Brokerage',
               'Investment Banking', 'Venture Capital & Private Equity'):
        assert zi in o.SECOND_CHANCE_ZI


# ── Revenue mapping ─────────────────────────────────────────────────────────
def test_revenue_mapping_and_too_small():
    assert o.estimate_bank_revenue(6551001) == round(6551001 * 1000 * 0.055)
    assert o.estimate_bank_revenue(None) is None and o.estimate_bank_revenue(0) is None
    assert o.estimate_ria_revenue(2_000_000_000, 30) == 12_000_000       # min(14M, 12M)
    assert o.estimate_ria_revenue(2_000_000_000, None) == 14_000_000
    assert o.estimate_ria_revenue(None, 5) == 2_000_000
    assert o.estimate_ria_revenue(None, None) is None
    assert [o.revenue_segment(v) for v in (4_000_000, 10_000_000, 15_000_000, 100_000_000, 150_000_000)] \
        == ['LMM', 'LMM', 'MM', 'Corp', 'Enterprise']
    assert o.revenue_segment(None) is None and o.revenue_segment(0) is None
    assert o.too_small(4_999_999) and not o.too_small(5_000_000)
    assert not o.too_small(0) and not o.too_small(None)
    assert o.format_usd(3_300_000) == '$3.3M' and o.format_usd(55_000_000) == '$55M'
    assert o.format_usd(1_200_000_000) == '$1.2B'
    assert o.size_bucket(30) == '1-50' and o.size_bucket(120) == '51-200' and o.size_bucket(0) is None


def test_zi_for_ntee_mapping():
    assert o.zi_for_ntee('A510') == 'Museums & Art Galleries'
    assert o.zi_for_ntee('A61') == 'Performing Arts Theaters'
    assert o.zi_for_ntee('A25') == 'Cultural & Informational Centers'
    assert o.zi_for_ntee('B42') == 'Colleges & Universities'
    assert o.zi_for_ntee('B24') == 'K-12 Schools'
    assert o.zi_for_ntee('B70') == 'Libraries'
    assert o.zi_for_ntee('X20') == 'Religious Organizations'
    assert o.zi_for_ntee('Y40') == 'Membership Organizations'
    # P1 (review 2026-09-08): E2x is UNKNOWN, not a registry-final OTHER — the
    # decile cannot tell a hospital from a community health center
    assert o.zi_for_ntee('E22') is None and o.zi_for_ntee('E21') is None
    assert o.zi_for_ntee('W60') == 'Banking'                     # credit unions
    assert o.zi_for_ntee(None, 6) == 'Membership Organizations'
    assert o.zi_for_ntee('P20') == o.zi_for_ntee(None) == 'Non-Profit & Charitable Organizations'


# ── lookup: local adapters ──────────────────────────────────────────────────
def test_lookup_ria_registered_and_era(db):
    hit = o.lookup('Beacon Hill Capital Advisors', {'kind': 'auto', 'state': 'MA'}, db_path=db, live=False)
    assert hit['source'] == 'sec_iapd' and hit['source_id'] == '1001'
    assert hit['hq'] == 'Boston, MA' and hit['hq_state'] == 'MA' and hit['in_territory'] is True
    assert hit['revenue'] == 'MM' and hit['revenue_amount_usd'] == 12_000_000
    assert 'RAUM $2.0B' in hit['revenue_source'] and '30 employees' in hit['revenue_source']
    assert hit['too_small'] is False
    assert hit['zi_subindustry'] == 'Lending & Brokerage' and hit['size'] == '1-50'
    assert hit['url'] == 'http://www.beaconhillcap.example'
    assert hit['matched_name'] == 'BEACON HILL CAPITAL ADVISORS' and hit['as_of'] == '2026-09-01'
    assert hit['confidence'] >= 0.85 and 1 <= len(hit['candidates']) <= 3
    assert set(hit['candidates'][0]) == {'name', 'city', 'state', 'source_id', 'score'}
    json.dumps(hit)                                       # plain data, cache-safe
    # the legal name (", LLC") and no state hint still resolve
    assert o.lookup('Beacon Hill Capital Advisors, LLC', db_path=db, live=False)['source_id'] == '1001'
    era = o.lookup('Nutmeg Ventures Management', {'state': 'CT'}, db_path=db, live=False)
    assert era['zi_subindustry'] == 'Venture Capital & Private Equity' and era['firm_type'] == 'ERA'
    assert era['revenue'] is None and era['revenue_amount_usd'] is None and era['too_small'] is False
    assert era['url'] == 'https://www.nutmegvc.example'
    out = o.lookup('Golden State Advisers', db_path=db, live=False)
    assert out['hq'] == 'San Francisco, CA' and out['in_territory'] is False
    assert out['revenue'] == 'Corp' and out['revenue_amount_usd'] == 48_000_000   # 120 × $400K < $63M
    # a state hint that contradicts the registry is a miss, not a wrong hit
    assert o.lookup('Golden State Advisers', {'state': 'MA'}, db_path=db, live=False) is None


def test_lookup_bank_washington_trust_disambiguated_by_state(db):
    assert o.lookup('Washington Trust', db_path=db, live=False) is None           # RI vs WA tie
    ri = o.lookup('Washington Trust', {'state': 'RI'}, db_path=db, live=False)
    assert ri['source'] == 'fdic' and ri['source_id'] == '23623'
    assert ri['hq'] == 'Westerly, RI' and ri['url'] == 'https://www.washtrust.com'
    assert ri['zi_subindustry'] == 'Banking' and ri['revenue'] == 'Enterprise'
    assert ri['revenue_amount_usd'] == round(6551001 * 1000 * 0.055) and ri['as_of'] == '2026-09-04'
    wa = o.lookup('Washington Trust', {'state': 'WA'}, db_path=db, live=False)
    assert wa['source_id'] == '1281' and wa['hq'] == 'Spokane, WA' and wa['in_territory'] is False
    # the holding company's name (what the press prints) resolves to the bank
    assert o.lookup('Washington Trust Bancorp, Inc.', {'state': 'RI'}, db_path=db, live=False)['source_id'] == '23623'
    # the city alone disambiguates when the caller has no state
    assert o.lookup('Washington Trust', {'city': 'Westerly'}, db_path=db, live=False)['source_id'] == '23623'
    # explicit kinds only run that adapter
    assert o.lookup('Washington Trust', {'kind': 'ria', 'state': 'RI'}, db_path=db, live=False) is None
    assert o.lookup('Beacon Hill Capital Advisors', {'kind': 'bank', 'state': 'MA'}, db_path=db, live=False) is None


def test_lookup_bank_too_small(db):
    hit = o.lookup('Zorblat Savings Bank', {'state': 'MA'}, db_path=db, live=False)
    assert hit['too_small'] is True and hit['revenue'] == 'LMM'
    assert hit['revenue_amount_usd'] == 3_300_000 and 'FDIC' in hit['revenue_source']
    assert hit['url'] == 'https://www.zorblatsavings.example'
    assert hit['industry'] == 'FDIC-insured savings bank'


def test_lookup_fails_soft_without_db(tmp_path, monkeypatch):
    missing = str(tmp_path / 'nope' / 'oracles.db')
    for name, hint in (('Beacon Hill Capital Advisors', {'state': 'MA'}),
                       ('Washington Trust', {'kind': 'bank', 'state': 'RI'}),
                       ('Zorblat Robotics Inc', None)):
        assert o.lookup(name, hint, db_path=missing, live=False) is None
    assert o.meta(missing) == {} and not os.path.exists(missing)      # readers never create it
    garbage = tmp_path / 'garbage.db'
    garbage.write_bytes(b'this is not a sqlite file at all' * 40)
    assert o.lookup('Beacon Hill Capital Advisors', {'state': 'MA'}, db_path=str(garbage), live=False) is None
    assert o.meta(str(garbage)) == {}
    # a table missing from an otherwise valid DB is a miss too
    partial = str(tmp_path / 'partial.db')
    sqlite3.connect(partial).close()
    assert o.lookup('Washington Trust', {'state': 'RI'}, db_path=partial, live=False) is None
    # garbage hints never raise
    assert o.lookup('', {'state': 'MA'}, db_path=missing) is None
    assert o.lookup('Washington Trust', {'kind': 'nonsense', 'state': 'Rhode Island'}, db_path=missing) is None
    assert o.lookup(None, None, db_path=missing) is None


def test_lookup_auto_never_calls_live_for_unshaped_names(db, cache, monkeypatch):
    monkeypatch.setattr(o.requests, 'get', FakeHTTP())          # any call → AssertionError
    assert o.lookup('Zorblat Robotics Inc', {'state': 'MA'}, db_path=db, cache=cache, live=True) is None
    # bank-shaped with no state: local miss, and the FDIC wildcard needs a state
    assert o.lookup('First Zorblat Bank', {}, db_path=db, cache=cache, live=True) is None


# ── lookup: live adapters (faked HTTP) ──────────────────────────────────────
def test_fdic_live_fallback_filters_and_caches(db, cache, monkeypatch):
    body = {'meta': FDIC_BODY['meta'], 'data': [
        _fdic('First Zorblat Bank', 'Springfield', 'MA', 777, 1_000_000, 'www.firstzorblat.example')]}
    http = FakeHTTP(('https://api.fdic.gov/', _resp(200, body)))
    monkeypatch.setattr(o.requests, 'get', http)
    hit = o.lookup('First Zorblat Bank', {'state': 'MA'}, db_path=db, cache=cache, live=True)
    assert hit['source'] == 'fdic' and hit['source_id'] == '777' and hit['live'] is True
    assert hit['hq'] == 'Springfield, MA' and hit['revenue'] == 'Corp'
    assert hit['revenue_amount_usd'] == 55_000_000 and hit['url'] == 'https://www.firstzorblat.example'
    url, params = http.calls[0]
    assert url == o.FDIC_INSTITUTIONS_URL
    assert params['filters'] == 'NAME:*ZORBLAT* AND NAME:*FIRST* AND NAME:*BANK* AND STALP:MA AND ACTIVE:1'
    # second lookup: served from the AccountCache (kind oracle_fdic), no HTTP
    again = o.lookup('First Zorblat Bank', {'state': 'MA'}, db_path=db, cache=cache, live=True)
    assert again['source_id'] == '777' and len(http.calls) == 1
    assert cache.get_search(o._cache_key('First Zorblat Bank', 'MA'), o.CACHE_KIND_FDIC)
    # holding-company words never go into the wildcard filter
    assert o.fdic_filter_tokens('Washington Trust Bancorp, Inc.') == ['washington', 'trust']


def test_fdic_live_zero_hits_negative_cached_but_errors_are_not(db, cache, monkeypatch):
    http = FakeHTTP(('https://api.fdic.gov/', _resp(200, {'meta': FDIC_BODY['meta'], 'data': []})))
    monkeypatch.setattr(o.requests, 'get', http)
    assert o.lookup('Zorblat National Bank', {'state': 'VT'}, db_path=db, cache=cache, live=True) is None
    key = o._cache_key('Zorblat National Bank', 'VT')
    assert cache.should_skip(key, o.CACHE_KIND_FDIC)
    assert o.lookup('Zorblat National Bank', {'state': 'VT'}, db_path=db, cache=cache, live=True) is None
    assert len(http.calls) == 1                                  # negative cache answered
    # an API failure is not a known-empty
    http2 = FakeHTTP(('https://api.fdic.gov/', _resp(503)))
    monkeypatch.setattr(o.requests, 'get', http2)
    assert o.lookup('Zorblat Savings and Loan', {'state': 'NH'}, db_path=db, cache=cache, live=True) is None
    assert not cache.should_skip(o._cache_key('Zorblat Savings and Loan', 'NH'), o.CACHE_KIND_FDIC)
    assert len(http2.calls) == 1


def test_propublica_404_json_is_a_confirmed_zero(cache, monkeypatch):
    http = FakeHTTP((o.PROPUBLICA_SEARCH_URL, _resp(404, PP_ZERO)))
    monkeypatch.setattr(o.requests, 'get', http)
    assert o.npo_lookup('Zorblatqx Nonexistent Foundation', 'MA', cache=cache) == ('empty', None)
    assert http.calls[0][1] == {'q': 'Zorblatqx Nonexistent Foundation', 'state[id]': 'MA'}
    key = o._npo_cache_key('Zorblatqx Nonexistent Foundation')
    assert cache.should_skip(key, o.CACHE_KIND_PROPUBLICA)
    # negative-cached: the next event pays no HTTP call
    assert o.npo_lookup('Zorblatqx Nonexistent Foundation', 'MA', cache=cache) == ('skipped', None)
    assert len(http.calls) == 1
    # M8 (review 2026-09-08): the negative entry is keyed on the NAME — the
    # same org under another state anchor (or none) costs no further call
    assert o.npo_lookup('Zorblatqx Nonexistent Foundation', 'NY', cache=cache) == ('skipped', None)
    assert o.npo_lookup('Zorblatqx Nonexistent Foundation', None, cache=cache) == ('skipped', None)
    assert len(http.calls) == 1
    # a 404 WITHOUT a JSON body, or a 5xx, is an error: never cached
    for bad in (_resp(404), _resp(500), _resp(503, PP_ZERO)):
        monkeypatch.setattr(o.requests, 'get', FakeHTTP((o.PROPUBLICA_SEARCH_URL, bad)))
        assert o.npo_lookup('Another Zorblat Society', 'MA', cache=cache) == ('error', None)
        assert not cache.should_skip(o._npo_cache_key('Another Zorblat Society'), o.CACHE_KIND_PROPUBLICA)
    # a transport failure is an error too
    def boom(*a, **k):
        raise ConnectionError('offline')
    monkeypatch.setattr(o.requests, 'get', boom)
    assert o.npo_lookup('Another Zorblat Society', 'MA', cache=cache) == ('error', None)
    assert o.lookup('Another Zorblat Society', {'state': 'MA'}, cache=cache) is None    # never raises
    # live disabled → skipped, nothing recorded
    assert o.npo_lookup('Third Zorblat Council', 'MA', cache=cache, live=False) == ('skipped', None)


def test_propublica_hit_uses_state_filter_similarity_gate_and_org_json(cache, monkeypatch):
    http = FakeHTTP((o.PROPUBLICA_SEARCH_URL, _resp(200, PP_SEARCH)),
                    ('https://projects.propublica.org/nonprofits/api/v2/organizations/42103607.json',
                     _resp(200, PP_ORG)))
    monkeypatch.setattr(o.requests, 'get', http)
    hit = o.lookup('Museum of Fine Arts', {'state': 'MA', 'city': 'Boston'}, cache=cache)
    assert hit['source'] == 'propublica' and hit['source_id'] == '42103607' and hit['ein'] == 42103607
    assert hit['hq'] == 'Boston, MA' and hit['zi_subindustry'] == 'Museums & Art Galleries'
    assert hit['revenue'] == 'Corp' and hit['revenue_amount_usd'] == 99573924
    assert hit['revenue_source'] == 'ProPublica Nonprofit Explorer — Form 990 FY2023 total revenue'
    assert hit['as_of'] == '2023' and hit['url'] is None and hit['too_small'] is False
    assert hit['latest_990'] == {'year': 2023, 'total_revenue': 99573924, 'total_expenses': 123302516}
    assert hit['filings'] == 2 and hit['profile_url'].endswith('/organizations/42103607')
    assert hit['industry'] == 'Nonprofit (NTEE A510), 501(c)(3)'
    assert hit['confidence'] >= 0.85 and [c['source_id'] for c in hit['candidates']][0] == '42103607'
    assert http.calls[0][1] == {'q': 'Museum of Fine Arts', 'state[id]': 'MA'}
    assert len(http.calls) == 2
    # cached for the next event of the same account
    again = o.lookup('Museum of Fine Arts', {'state': 'MA'}, cache=cache)
    assert again['source_id'] == '42103607' and len(http.calls) == 2
    # the similarity gate refuses a top hit that is not the org asked about
    http2 = FakeHTTP((o.PROPUBLICA_SEARCH_URL, _resp(200, PP_SEARCH)))
    monkeypatch.setattr(o.requests, 'get', http2)
    assert o.npo_lookup('Zorblat Robotics Foundation', 'MA', cache=cache) == ('empty', None)
    assert len(http2.calls) == 1                     # no organization call for a rejected hit
    # and a state conflict pushes even an exact name below the bar
    assert o.npo_lookup('Museum of Fine Arts', 'VA', cache=cache) == ('empty', None)


def test_propublica_without_state_needs_a_near_exact_name(cache, monkeypatch):
    national = {'total_results': 2, 'organizations': [
        {'ein': 1, 'name': 'Habitat For Humanity International Inc', 'city': 'Americus', 'state': 'GA',
         'ntee_code': 'L20', 'subseccd': 3},
        {'ein': 2, 'name': 'Habitat For Humanity Of Greater Boston Inc', 'city': 'Boston', 'state': 'MA',
         'ntee_code': 'L20', 'subseccd': 3}]}
    http = FakeHTTP((o.PROPUBLICA_SEARCH_URL, _resp(200, national)),
                    ('https://projects.propublica.org/nonprofits/api/v2/organizations/',
                     _resp(200, {'organization': {}, 'filings_with_data': []})))
    monkeypatch.setattr(o.requests, 'get', http)
    assert o.npo_lookup('Habitat for Humanity', None, cache=cache) == ('empty', None)
    assert 'state[id]' not in http.calls[0][1]
    status, hit = o.npo_lookup('Habitat for Humanity of Greater Boston', None, cache=cache)
    assert status == 'hit' and hit['source_id'] == '2' and hit['hq'] == 'Boston, MA'
    # YMCA alias end to end (live shape 2026-09-08): the raw query returns
    # only the realty subsidiaries; the expanded legal form finds the parent.
    realty = {'total_results': 2, 'organizations': [
        {'ein': 271029985, 'name': 'Ymca Of Greater Boston Realty Corp', 'city': 'Boston',
         'state': 'MA', 'ntee_code': None, 'subseccd': 3},
        {'ein': 383854791, 'name': 'Ymca Of Greater Boston Huntington Avenue Realty Corporation',
         'city': 'Boston', 'state': 'MA', 'ntee_code': 'E11', 'subseccd': 3}]}
    parent = {'total_results': 1, 'organizations': [
        {'ein': 42103551, 'name': 'Young Mens Christian Association Of Greater Boston Inc',
         'city': 'Boston', 'state': 'MA', 'ntee_code': 'P270', 'subseccd': 3}]}

    def search(url, params):
        return _resp(200, parent if params['q'].startswith('young mens') else realty)
    http = FakeHTTP((o.PROPUBLICA_SEARCH_URL, search),
                    ('https://projects.propublica.org/nonprofits/api/v2/organizations/42103551.json',
                     _resp(200, {'organization': {'revenue_amount': 80_000_000}, 'filings_with_data': []})))
    monkeypatch.setattr(o.requests, 'get', http)
    hit = o.lookup('YMCA of Greater Boston', {'state': 'MA'}, cache=cache)
    assert hit and hit['source_id'] == '42103551' and hit['confidence'] == 1.0
    assert hit['revenue'] == 'Corp' and hit['too_small'] is False
    assert hit['revenue_source'] == 'ProPublica Nonprofit Explorer — IRS BMF revenue amount'
    assert hit['zi_subindustry'] == 'Non-Profit & Charitable Organizations'
    assert [c[1]['q'] for c in http.calls[:2]] == ['YMCA of Greater Boston',
                                                   'young mens christian association of greater boston']
    assert all(c[1]['state[id]'] == 'MA' for c in http.calls[:2]) and len(http.calls) == 3
    # the realty arm alone (parent absent from both answers) is NOT a hit
    monkeypatch.setattr(o.requests, 'get', FakeHTTP((o.PROPUBLICA_SEARCH_URL, _resp(200, realty))))
    assert o.npo_lookup('YMCA of Greater Boston Association', 'MA', cache=cache) == ('empty', None)


def test_lookup_zi_guess_routes_to_the_registry(db, cache, monkeypatch):
    monkeypatch.setattr(o.requests, 'get', FakeHTTP())
    # the name says nothing; the article's subindustry picks the adapter
    hit = o.lookup('Beacon Hill Capital Advisors', {'zi_guess': 'Lending & Brokerage', 'state': 'MA'},
                   db_path=db, cache=cache)
    assert hit['source'] == 'sec_iapd'
    assert o.lookup('Zorblat Robotics', {'zi_guess': 'Banking', 'state': 'MA'},
                    db_path=db, cache=cache, live=False) is None


def test_parse_iapd_streams_and_skips_malformed_firms():
    xml = IAPD_XML.replace(
        b'<Firm>\n      <Info SECRgnCD="SFRO"',
        b'<Firm><Info FirmCrdNb="not-a-number" BusNm="BROKEN"/></Firm>\n    <Firm>\n      <Info SECRgnCD="SFRO"')
    import io
    rows = list(o.parse_iapd(io.BytesIO(xml)))
    assert [r['crd'] for r in rows] == [1001, 1002, 1003]
    assert all(r['as_of'] == '2026-09-01' for r in rows)


def test_user_agent_comes_from_config():
    o._LIVE['ua'] = None
    ua = o.user_agent()
    assert 'TeamAlbert' in ua and '@' in ua


# ═══════════════════════════════════════════════════════════════════════════
# Review 2026-09-08 fixes — each test fails if its fix is reverted
# ═══════════════════════════════════════════════════════════════════════════
def _ria_row(crd, name, city, state, emp=30, raum=2_000_000_000, firm_type='Registered',
             website=None, legal=None):
    return {'crd': crd, 'business_name': name, 'legal_name': legal or name,
            'norm_name': o.normalize_name(name), 'city': city, 'state': state,
            'country': 'United States', 'firm_type': firm_type, 'reg_status': 'APPROVED',
            'reg_date': '2026-08-12', 'website': website, 'total_employees': emp,
            'raum_usd': raum, 'sec_number': f'801-{crd}', 'as_of': '2026-09-01'}


def _bank_row(cert, name, city, state, asset_kusd=1_000_000, website=None, holding_co=None):
    return {'cert': cert, 'name': name, 'norm_name': o.normalize_name(name), 'city': city,
            'state': state, 'asset_kusd': asset_kusd, 'website': website, 'est_date': '01/01/1900',
            'holding_co': holding_co, 'bkclass': 'NM', 'active': 1, 'as_of': '2026-09-04'}


def _fixture_db(tmp_path, ria=(), bank=(), name='fixture.db'):
    """A tmp oracles.db with exactly these rows (contract schema)."""
    path = str(tmp_path / name)
    o.ensure_schema(path)
    conn = sqlite3.connect(path)
    for row in ria:
        conn.execute(f'INSERT INTO ria_firm ({", ".join(o._RIA_COLUMNS)}) VALUES '
                     f'({", ".join("?" * len(o._RIA_COLUMNS))})', tuple(row[c] for c in o._RIA_COLUMNS))
    for row in bank:
        conn.execute(f'INSERT INTO bank ({", ".join(o._BANK_COLUMNS)}) VALUES '
                     f'({", ".join("?" * len(o._BANK_COLUMNS))})', tuple(row[c] for c in o._BANK_COLUMNS))
    conn.commit()
    conn.close()
    return path


def test_h1a_same_state_tie_between_different_registrants_is_no_hit(tmp_path, caplog):
    """'Community Bank' + NY named Hanover / NorthEast / American Community
    Bank at 1.0 each; the size tie-break picked Hanover and its Enterprise
    revenue tombstoned the event. Three distinctive-token twins in one state
    tie → no hit, and the tie is logged for the audit."""
    import logging
    db = _fixture_db(tmp_path, bank=[
        _bank_row(1, 'Zorblat Community Bank', 'Albany', 'NY', asset_kusd=9_000_000, website='a.example'),
        _bank_row(2, 'Zorblat Community Bank', 'Buffalo', 'NY', asset_kusd=200_000, website='b.example'),
        _bank_row(3, 'The Zorblat Community Bank', 'Utica', 'NY', asset_kusd=90_000, website='c.example'),
    ])
    with caplog.at_level(logging.INFO, logger='src.pipeline.oracles'):
        assert o.lookup('Zorblat Community Bank', {'state': 'NY'}, db_path=db, live=False) is None
    assert 'registrants tie' in caplog.text and 'Zorblat Community Bank [1, NY]' in caplog.text
    # a unique registrant in the state is still a clean hit
    db2 = _fixture_db(tmp_path, bank=[
        _bank_row(1, 'Zorblat Community Bank', 'Albany', 'NY', asset_kusd=9_000_000, website='a.example'),
        _bank_row(4, 'Zorblat Community Bank', 'Hartford', 'CT', asset_kusd=9_000_000, website='d.example'),
    ], name='two.db')
    assert o.lookup('Zorblat Community Bank', {'state': 'NY'}, db_path=db2, live=False)['source_id'] == '1'
    assert o.lookup('Zorblat Community Bank', {}, db_path=db2, live=False) is None      # cross-state tie


def test_h1b_exact_name_beats_a_contained_one_in_the_same_state(tmp_path):
    """'First Bank' + VA lost to 'The First Bank and Trust Company' because
    exact 1.0 + 0.1 and contained 0.9 + 0.1 both clip to 1.0 and the bigger
    bank won the tie. Ranking by the raw score puts the exact name first."""
    db = _fixture_db(tmp_path, bank=[
        _bank_row(10, 'The Zorblat Bank and Trust Company', 'Lebanon', 'VA',
                  asset_kusd=9_000_000, website='big.example'),
        _bank_row(11, 'Zorblat Bank', 'Strasburg', 'VA', asset_kusd=900_000, website='small.example'),
    ])
    hit = o.lookup('Zorblat Bank', {'state': 'VA'}, db_path=db, live=False)
    assert hit['source_id'] == '11' and hit['raw_score'] == 1.0
    assert [c['source_id'] for c in hit['candidates']][:2] == ['11', '10']
    assert o.lookup('Zorblat Bank and Trust', {'state': 'VA'}, db_path=db, live=False)['source_id'] == '10'


def test_h1c_all_generic_names_never_hit_anything(tmp_path, cache, monkeypatch):
    """'Wealth Management' + MA, 'Capital Management' + MA, 'Capital Partners'
    + NY and 'First National Bank' + PA each matched some registrant through
    the state-wide fallback. A name with no distinctive token is no lookup —
    local, FDIC-live or ProPublica."""
    db = _fixture_db(tmp_path,
                     ria=[_ria_row(1, 'SHP WEALTH MANAGEMENT, LLC', 'Boston', 'MA'),
                          _ria_row(2, 'GEODE CAPITAL MANAGEMENT, LLC', 'Boston', 'MA'),
                          _ria_row(3, 'DIAMETER CAPITAL PARTNERS LP', 'New York', 'NY'),
                          _ria_row(4, 'WEALTH MANAGEMENT', 'Boston', 'MA')],      # even the exact name
                     bank=[_bank_row(1, 'First National Bank of Pennsylvania', 'Greenville', 'PA',
                                     asset_kusd=40_000_000, website='fnb.example'),
                           _bank_row(2, 'Farmers and Merchants Bank', 'Lakeland', 'GA'),
                           _bank_row(3, 'Farmers & Merchants Bank', 'Eatonton', 'GA')])
    monkeypatch.setattr(o.requests, 'get', FakeHTTP())          # any live call → AssertionError
    for name, st in (('Wealth Management', 'MA'), ('Capital Management', 'MA'),
                     ('Capital Partners', 'NY'), ('First National Bank', 'PA'),
                     ('Farmers & Merchants Bank', 'GA'), ('Community Bank', 'NY'),
                     ('First Bank', 'VA')):
        assert o._distinctive_tokens(o.normalize_name(name)) == []
        assert o.lookup(name, {'state': st}, db_path=db, cache=cache, live=True) is None, name
    assert o.npo_lookup('Community Foundation', 'MA', cache=cache, live=True) == ('skipped', None)
    assert not cache.should_skip(o._npo_cache_key('Community Foundation'), o.CACHE_KIND_PROPUBLICA)
    # one distinctive token brings the lookup back
    assert o.lookup('Geode Capital Management', {'state': 'MA'}, db_path=db, live=False)['source_id'] == '2'
    assert o.lookup('First National Bank of Pennsylvania', {'state': 'PA'},
                    db_path=db, live=False)['source_id'] == '1'


def test_h1d_difflib_only_similarity_never_gets_the_geography_bonus(tmp_path):
    """'Beacon Hill Partners' + MA hit BEACON CAPITAL PARTNERS at 0.937: a
    0.837 character ratio plus 0.1 for the shared state."""
    db = _fixture_db(tmp_path, ria=[_ria_row(158137, 'BEACON CAPITAL PARTNERS, LLC', 'Boston', 'MA'),
                                    _ria_row(1001, 'BEACON HILL CAPITAL ADVISORS', 'Boston', 'MA')])
    assert o.lookup('Beacon Hill Partners', {'state': 'MA', 'kind': 'ria'}, db_path=db, live=False) is None
    d = o.score_detail(o.normalize_name('Beacon Hill Partners'),
                       o.normalize_name('BEACON CAPITAL PARTNERS, LLC'), 'MA', 'MA')
    assert d['channel'] == 'difflib' and d['score'] < o.MATCH_THRESHOLD < 0.937
    # the containment channel keeps its bonus: 'Beacon Hill Capital Advisors' finds itself with a tail
    assert o.lookup('Beacon Hill Capital Advisors', {'state': 'MA'}, db_path=db, live=False)['source_id'] == '1001'


def test_l4_weak_adviser_shapes_need_an_exact_in_state_registrant(tmp_path):
    """'X Partners' / 'Y Capital' are law firms and realty shops as often as
    advisers: strict mode — a state, and an exact / containment match in it."""
    db = _fixture_db(tmp_path, ria=[
        _ria_row(1, 'ZORBLAT RIDGE CAPITAL PARTNERS', 'Boston', 'MA', website='https://zcp.example'),
        _ria_row(2, 'BEACON HILL CAPITAL ADVISORS', 'Boston', 'MA'),
    ])
    hit = o.lookup('Zorblat Ridge Capital Partners', {'state': 'MA'}, db_path=db, live=False)
    assert hit and hit['source_id'] == '1' and hit['url'] == 'https://zcp.example'
    assert o.lookup('Zorblat Ridge Capital Partners, LLC', {'state': 'MA'}, db_path=db, live=False)['source_id'] == '1'
    assert o.lookup('Zorblat Ridge Capital Partners', {}, db_path=db, live=False) is None      # no state
    assert o.lookup('Zorblat Ridge Capital Partners', {'state': 'CT'}, db_path=db, live=False) is None
    # a one-letter slip is a 0.97 difflib match — refused in strict mode
    # (the slip sits in 'ridge'; the candidate query LIKEs on 'zorblat')
    typo = o.score_detail(o.normalize_name('Zorblat Rigde Capital Partners'), 'zorblat ridge capital partners')
    assert typo['channel'] == 'difflib' and typo['raw'] > o.MATCH_THRESHOLD
    assert o._distinctive_tokens(o.normalize_name('Zorblat Rigde Capital Partners'))[0] == 'zorblat'
    assert o.lookup('Zorblat Rigde Capital Partners', {'state': 'MA'}, db_path=db, live=False) is None
    assert o.lookup('Zorblat Ridge Law Partners', {'state': 'MA'}, db_path=db, live=False) is None
    # the strong shape keeps fuzzy matching: a one-letter typo still resolves
    assert o.lookup('Beacon Hil Capital Advisors', {'state': 'MA'}, db_path=db, live=False)['source_id'] == '2'
    # an explicit kind (the article said adviser) is never strict
    assert o.lookup('Zorblat Rigde Capital Partners', {'state': 'MA', 'kind': 'ria'}, db_path=db, live=False)['source_id'] == '1'


def test_m1_p3_margin_bands_and_the_min_max_rule(tmp_path):
    # P3: ±30% bands around both edges of an ESTIMATE
    assert (o.TOO_SMALL_FLOOR, o.TOO_SMALL_CEIL, o.ENTERPRISE_FLOOR, o.ENTERPRISE_CEIL) == \
        (3_500_000, 6_500_000, 77_000_000, 130_000_000)
    assert o.estimate_band(3_499_999) == ('LMM', True)
    assert o.estimate_band(3_500_000) == (None, False) and o.estimate_band(6_499_999) == (None, False)
    assert o.estimate_band(6_500_000) == ('LMM', False) and o.estimate_band(10_000_000) == ('LMM', False)
    assert o.estimate_band(10_000_001) == ('MM', False) and o.estimate_band(20_000_001) == ('Corp', False)
    assert o.estimate_band(76_999_999) == ('Corp', False)
    assert o.estimate_band(77_000_000) == (None, False) and o.estimate_band(130_000_000) == (None, False)
    assert o.estimate_band(130_000_001) == ('Enterprise', False)
    assert o.estimate_band(None) == (None, False) and o.estimate_band(0) == (None, False)
    # M1: the MIN proxy places the segment, only the MAX proxy may say too_small
    assert o.ria_revenue_proxies(2_000_000_000, 10) == (14_000_000, 4_000_000)
    assert o.estimate_ria_revenue(2_000_000_000, 10) == 4_000_000                 # still the MIN
    band = o.ria_revenue_band(2_000_000_000, 10)
    assert band == {'segment': None, 'too_small': False, 'estimate': 4_000_000, 'estimate_max': 14_000_000}
    assert o.ria_revenue_band(100_000_000, 3)['too_small'] is True                # $0.7M / $1.2M
    assert o.ria_revenue_band(20_000_000_000, 40)['segment'] == 'MM'               # $140M / $16M
    assert o.ria_revenue_band(None, 10) == {'segment': None, 'too_small': False,
                                             'estimate': 4_000_000, 'estimate_max': 4_000_000}
    assert o.ria_revenue_band(None, None)['estimate'] is None
    # a Form 990 figure is a FACT: sharp edges stay
    assert o.too_small(4_999_999) and o.revenue_segment(120_000_000) == 'Enterprise'
    # end to end: 10 staff + $2B RAUM is UNKNOWN, not too_small; banks use the bands
    db = _fixture_db(tmp_path,
                     ria=[_ria_row(1, 'ZORBLAT WEALTH ADVISORS', 'Boston', 'MA', emp=10, raum=2_000_000_000),
                          _ria_row(2, 'TINY ZORBLAT ADVISORS', 'Boston', 'MA', emp=3, raum=100_000_000)],
                     bank=[_bank_row(1, 'Zorblat Savings Bank', 'Salem', 'MA', asset_kusd=60_000),
                           _bank_row(2, 'Zorblat Edge Bank', 'Salem', 'MA', asset_kusd=90_000),
                           _bank_row(3, 'Zorblat Regional Bank', 'Salem', 'MA', asset_kusd=1_600_000),
                           _bank_row(4, 'Zorblat Giant Bank', 'Salem', 'MA', asset_kusd=2_500_000)])
    hit = o.lookup('Zorblat Wealth Advisors', {'state': 'MA'}, db_path=db, live=False)
    assert hit['revenue'] is None and hit['too_small'] is False
    assert hit['revenue_amount_usd'] == 4_000_000 and hit['revenue_estimate_max_usd'] == 14_000_000
    tiny = o.lookup('Tiny Zorblat Advisors', {'state': 'MA'}, db_path=db, live=False)
    assert tiny['too_small'] is True and tiny['revenue'] == 'LMM'
    assert o.lookup('Zorblat Savings Bank', {'state': 'MA'}, db_path=db, live=False)['too_small'] is True   # $3.3M
    edge = o.lookup('Zorblat Edge Bank', {'state': 'MA'}, db_path=db, live=False)                      # $4.95M
    assert edge['revenue'] is None and edge['too_small'] is False and edge['revenue_amount_usd'] == 4_950_000
    assert o.lookup('Zorblat Regional Bank', {'state': 'MA'}, db_path=db, live=False)['revenue'] is None   # $88M
    assert o.lookup('Zorblat Giant Bank', {'state': 'MA'}, db_path=db, live=False)['revenue'] == 'Enterprise'


def test_m8_propublica_second_form_only_when_tokens_change_and_negatives_shared(cache, monkeypatch):
    zero = {'total_results': 0, 'organizations': []}
    http = FakeHTTP((o.PROPUBLICA_SEARCH_URL, _resp(404, zero)))
    monkeypatch.setattr(o.requests, 'get', http)
    # '&' → 'and', a possessive, a period: the token search sees the same query
    for name in ('Boys & Girls Club of Zorblat', "St. Mary's Zorblat Society",
                 'Zorblat Community Foundation, Inc.'):
        http.calls.clear()
        o.npo_lookup(name, 'MA', cache=None)
        assert [c[1]['q'] for c in http.calls] == [name], name
    # normalize_name applies the aliases itself — the literal comparison the
    # review named is always equal, so the alias-free token strings decide
    assert o.normalize_name(o.expand_aliases('YMCA of Zorblat')) == o.normalize_name('YMCA of Zorblat')
    assert o._alias_free_tokens('YMCA of Zorblat') != o._alias_free_tokens(o.expand_aliases('YMCA of Zorblat'))
    for name in ('YMCA of Zorblat', 'Zorblat FCU'):
        http.calls.clear()
        o.npo_lookup(name, 'MA', cache=None)
        assert len(http.calls) == 2 and http.calls[0][1]['q'] == name, name
    # one negative entry per name: the three anchors an event tries cost one call
    http.calls.clear()
    assert o.npo_lookup('Zorblat Rowing Society', None, cache=cache) == ('empty', None)
    assert o.npo_lookup('Zorblat Rowing Society', 'MA', cache=cache) == ('skipped', None)
    assert o.npo_lookup('Zorblat Rowing Society', 'RI', cache=cache) == ('skipped', None)
    assert len(http.calls) == 1
    # a cached HIT in another state is not served for this one, and the miss
    # that follows does not negative-cache a name the registry knows
    hit_body = {'total_results': 1, 'organizations': [
        {'ein': 7, 'name': 'Zorblat Rowing Club', 'city': 'Boston', 'state': 'MA', 'ntee_code': 'N60', 'subseccd': 3}]}
    http2 = FakeHTTP((o.PROPUBLICA_SEARCH_URL, lambda url, params: _resp(200, hit_body)
                      if params.get('state[id]') == 'MA' else _resp(404, zero)),
                     ('https://projects.propublica.org/nonprofits/api/v2/organizations/',
                      _resp(200, {'organization': {}, 'filings_with_data': []})))
    monkeypatch.setattr(o.requests, 'get', http2)
    assert o.npo_lookup('Zorblat Rowing Club', 'MA', cache=cache)[0] == 'hit'
    assert o.npo_lookup('Zorblat Rowing Club', None, cache=cache)[0] == 'hit'      # no state: served
    assert o.npo_lookup('Zorblat Rowing Club', 'VA', cache=cache) == ('empty', None)
    assert not cache.should_skip(o._npo_cache_key('Zorblat Rowing Club'), o.CACHE_KIND_PROPUBLICA)
    assert o.npo_lookup('Zorblat Rowing Club', 'MA', cache=cache)[0] == 'hit'
    assert [c[1].get('state[id]') for c in http2.calls if 'search' in c[0]] == ['MA', 'VA']


def test_l1_refresh_builds_aside_and_never_holds_the_lock_across_the_parse(tmp_path):
    """The old single transaction DELETEd the table first and held the write
    lock for the whole 82 MB parse; readers timed out and enrichment spent a
    search. Now the rows land in ria_firm__incoming with short commits and a
    concurrent connection can both READ the old table and WRITE the ledger
    while the parse is still running."""
    path = str(tmp_path / 'oracles.db')
    o.refresh_ria(path, source=_gz(tmp_path))                        # 3 firms
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE ria_trigger_emitted (crd INTEGER PRIMARY KEY, event_id TEXT, '
                 'emitted_at TEXT, reg_date TEXT)')
    conn.commit()
    conn.close()
    seen = {}

    def rows():
        for i in range(1, 2501):
            if i == 1500:                                            # mid-parse, one batch committed
                other = sqlite3.connect(path, timeout=0.5)
                try:
                    seen['old_count'] = other.execute('SELECT COUNT(*) FROM ria_firm').fetchone()[0]
                    other.execute('BEGIN IMMEDIATE')                  # a writer (the trigger ledger)
                    other.execute("INSERT INTO ria_trigger_emitted VALUES (9, 'ev', '2026-09-02', '2026-08-30')")
                    other.commit()
                    seen['incoming'] = other.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'ria_firm__incoming'").fetchone()[0]
                finally:
                    other.close()
            yield _ria_row(i, f'Firm {i} Advisors', 'Boston', 'MA')
    n = o._replace_table(path, 'ria_firm', o._RIA_COLUMNS, rows(),
                         {'source': 'ria', 'refreshed_at': '2026-09-02T05:00:00', 'src_url': 'x'})
    assert n == 2500 and seen == {'old_count': 3, 'incoming': 1}
    conn = sqlite3.connect(path)
    assert conn.execute('SELECT COUNT(*) FROM ria_firm').fetchone()[0] == 2500
    assert conn.execute('SELECT event_id FROM ria_trigger_emitted WHERE crd = 9').fetchone() == ('ev',)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert 'ria_firm__incoming' not in names and {'ria_firm_state', 'ria_firm_norm'} <= names
    assert conn.execute("SELECT rows FROM oracle_meta WHERE source = 'ria'").fetchone() == (2500,)
    conn.close()
    # a failing parse leaves no incoming table and the previous rows intact
    def broken():
        yield _ria_row(1, 'Only Firm Advisors', 'Boston', 'MA')
        raise ValueError('malformed feed')
    with pytest.raises(ValueError):
        o._replace_table(path, 'ria_firm', o._RIA_COLUMNS, broken(), {'source': 'ria'})
    conn = sqlite3.connect(path)
    assert conn.execute('SELECT COUNT(*) FROM ria_firm').fetchone()[0] == 2500
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%incoming%'").fetchone()[0] == 0
    conn.close()


def test_p1_hospital_ntee_is_unknown_for_the_classifier_to_decide():
    org = {'ein': 1, 'name': 'Zorblat Health System Inc', 'city': 'Boston', 'state': 'MA',
           'ntee_code': 'E22', 'subseccd': 3, 'revenue_amount': 900_000_000}
    best = {'name': org['name'], 'city': 'Boston', 'state': 'MA', 'source_id': '1', 'score': 1.0,
            'raw': 1.0, 'ntee_code': 'E22', 'subseccd': 3}
    hit = o._npo_from_org(org, [], [best], best)
    assert hit['zi_subindustry'] is None and hit['revenue'] == 'Enterprise'
    assert hit['industry'] == 'Nonprofit (NTEE E22), 501(c)(3)' and hit['raw_score'] == 1.0
