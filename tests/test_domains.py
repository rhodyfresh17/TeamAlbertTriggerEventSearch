"""Free domain resolution (src/pipeline/domains.py) — Phase 3 slice B4.

Network-free: every live rung is served by a fake requests.Session that
raises on any un-routed URL, so a test can never leak onto the network.
CLEARBIT_LIVE / FDIC_LIVE below are frozen from the 2026-09-08 live sanity
pass (10 calls: 8 Clearbit names, 1 FDIC, 1 SEC) so the accept rules stay
pinned to real API shapes. Time is injected via `now`; nothing sleeps."""
import inspect
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
import requests

import src.pipeline.domains as d
from src.pipeline.cache import AccountCache

T0 = datetime(2026, 9, 8, 12, 0, 0)


# ═══════════════════════════════════════════════════════════════════════════
# Frozen research outcomes (live 2026-09-08) — the 8-name Clearbit table
# ═══════════════════════════════════════════════════════════════════════════
# Verbatim Clearbit autocomplete results (logo is always null). Outcome
# column: what the accept rules must do with them. Amica's returned domain
# (amicahome.com, not amica.com) is recorded as observed, unverified.
CLEARBIT_LIVE = {
    'Washington Trust': [                                    # RI vs WA — deferred
        {'name': 'Washington Trust Bank', 'domain': 'washtrust.com', 'logo': None},
        {'name': 'Washington Trust Bank', 'domain': 'watrust.com', 'logo': None},
        {'name': 'Washington Trust for Historic Preservation', 'domain': 'preservewa.org', 'logo': None},
        {'name': 'Washington Trust Wealth Management', 'domain': 'washtrustwealth.com', 'logo': None},
        {'name': 'Washington Trust Mortgage', 'domain': 'washtrustmortgage.com', 'logo': None},
    ],
    'Cherry Bekaert': [
        {'name': 'Cherry Bekaert', 'domain': 'cbh.com', 'logo': None},
        {'name': 'Cherry Bekaert', 'domain': 'cb.cpa', 'logo': None},
    ],
    'Herc Rentals': [{'name': 'Herc Rentals', 'domain': 'hercrentals.com', 'logo': None}],
    'Amica Mutual Insurance': [
        {'name': 'Amica Mutual Insurance Co.', 'domain': 'amicahome.com', 'logo': None}],
    'Bradley, Foster & Sargent': [],                          # small RIA — miss
    'YMCA of Greater Providence': [
        {'name': 'YMCA of Greater Providence', 'domain': 'ymcagreaterprovidence.org', 'logo': None}],
    'Home Loan Investment Bank': [                            # lender, bank-shaped, one result
        {'name': 'Home Loan Investment Bank', 'domain': 'homeloanbank.com', 'logo': None}],
    'Textron': [
        {'name': 'Textron', 'domain': 'textron.com', 'logo': None},
        {'name': 'Textron Aviation', 'domain': 'txtav.com', 'logo': None},
        {'name': 'Textr Online', 'domain': 'textronline.com', 'logo': None},
        {'name': 'TEXTRONIX LIMITED', 'domain': 'textronix.net', 'logo': None},
        {'name': 'TEXTRONICS Engineering', 'domain': 'textronicsengineering.com', 'logo': None},
    ],
}
CLEARBIT_EXPECTED = {                     # name -> (domain, clearbit outcome tag)
    'Washington Trust': (None, 'bank_multi_defer'),
    'Cherry Bekaert': ('cbh.com', 'hit'),
    'Herc Rentals': ('hercrentals.com', 'hit'),
    'Amica Mutual Insurance': ('amicahome.com', 'hit'),
    'Bradley, Foster & Sargent': (None, 'miss'),
    'YMCA of Greater Providence': ('ymcagreaterprovidence.org', 'hit'),
    'Home Loan Investment Bank': ('homeloanbank.com', 'hit'),
    'Textron': ('textron.com', 'hit'),
}

# FDIC BankFind, filters=ACTIVE:1 AND STALP:RI AND (NAME:*WASHINGTON* AND
# NAME:*TRUST*) — one row (cert 23623). The live WEBADDR normalized to
# washtrust.com; the raw form below carries a scheme + www to exercise the
# normalizer the way real rows do.
FDIC_LIVE = {
    'meta': {'total': 1, 'parameters': {'filters': 'ACTIVE:1 AND STALP:RI AND (NAME:*WASHINGTON* AND NAME:*TRUST*)'}},
    'data': [{'data': {'NAME': 'The Washington Trust Company, of Westerly', 'CITY': 'Westerly',
                       'STALP': 'RI', 'WEBADDR': 'https://www.washtrust.com', 'CERT': 23623,
                       'NAMEHCR': 'WASHINGTON TRUST BCORP INC', 'ID': '23623'}, 'score': 0}],
    'totals': {'count': 1},
}
# The same query against WA would return the other bank (shape reconstructed).
FDIC_WA = {'data': [{'data': {'NAME': 'Washington Trust Bank', 'CITY': 'Spokane', 'STALP': 'WA',
                              'WEBADDR': 'www.watrust.com', 'CERT': 1, 'NAMEHCR': 'W.T.B. FINANCIAL CORPORATION'}}]}
# SEC submissions for Textron (CIK 217346): `website` empty on 2026-09-08.
SEC_TEXTRON_EMPTY = {'cik': '217346', 'name': 'TEXTRON INC', 'website': '', 'investorWebsite': ''}

# The denylist the spec enumerates (sites.google.com is host-level, tested separately).
SPEC_DENYLIST = set("""
linkedin.com zoominfo.com crunchbase.com bloomberg.com rocketreach.co growjo.com leadiq.com dnb.com
pitchbook.com cbinsights.com owler.com craft.co apollo.io lusha.com signalhire.com datanyze.com
opencorporates.com buzzfile.com manta.com bbb.org yelp.com yellowpages.com mapquest.com kompass.com
wikipedia.org wikidata.org glassdoor.com indeed.com adzuna.com ziprecruiter.com lever.co greenhouse.io
myworkdayjobs.com icims.com smartrecruiters.com jobvite.com bamboohr.com workable.com sec.gov fdic.gov
ncua.gov finra.org irs.gov propublica.org guidestar.org candid.org sedarplus.ca edgar-online.com
prnewswire.com globenewswire.com businesswire.com newswire.com newswire.ca accesswire.com
einpresswire.com prweb.com webwire.com pymnts.com techcrunch.com reuters.com finsmes.com pehub.com
buyoutsinsider.com businessinsider.com cnbc.com foxbusiness.com financialpost.com insurancejournal.com
carriermanagement.com wealthmanagement.com coindesk.com theblock.co decrypt.co nonprofitquarterly.org
thenonprofittimes.com philanthropy.com associationsnow.com nvca.org pe-insights.com yahoo.com msn.com
forbes.com fortune.com axios.com wsj.com ft.com theglobeandmail.com apnews.com bizjournals.com patch.com
google.com bing.com duckduckgo.com facebook.com x.com twitter.com instagram.com youtube.com tiktok.com
threads.net medium.com substack.com bit.ly t.co lnkd.in linktr.ee wixsite.com squarespace.com
godaddysites.com weebly.com wordpress.com blogspot.com github.io notion.site mailchi.mp eventbrite.com
docsend.com
""".split())


# ═══════════════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════════════
class FakeResponse:
    def __init__(self, status=200, payload=None, url='', text=''):
        self.status_code = status
        self._payload = payload
        self.url = url
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class FakeSession:
    """Routes by URL substring. A responder is a FakeResponse, an Exception
    to raise, or a callable(url, params). Un-routed URL = test bug."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, timeout=None, allow_redirects=True):
        self.calls.append({'method': method, 'url': url, 'params': dict(params or {})})
        for needle, responder in self.routes.items():
            if needle in url:
                r = responder(url, params) if callable(responder) else responder
                if isinstance(r, BaseException):
                    raise r
                return r
        raise AssertionError(f'un-routed URL in test: {url}')

    def calls_to(self, needle):
        return [c for c in self.calls if needle in c['url']]


CLEARBIT, FDIC, SEC = 'autocomplete.clearbit.com', 'api.fdic.gov', 'data.sec.gov'


@pytest.fixture
def net(monkeypatch, tmp_path):
    """Network-free harness: one fake session for every endpoint, no sleeps,
    live rungs enabled, guess off, oracle DB absent."""
    sess = FakeSession()
    monkeypatch.setattr(d, '_SESSIONS', {})
    monkeypatch.setattr(d, '_LAST_CALL', {})
    monkeypatch.setattr(d, '_new_session', lambda ua: sess)
    monkeypatch.setattr(d, '_sleep', lambda s: None)
    monkeypatch.setattr(d, 'DOMAINS_LIVE_ENABLED', True)
    monkeypatch.setattr(d, 'DOMAINS_CLEARBIT_ENABLED', True)
    monkeypatch.setattr(d, 'DOMAINS_GUESS_ENABLED', False)
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', str(tmp_path / 'no-such-oracles.db'))
    return sess


@pytest.fixture
def cache(tmp_path):
    return AccountCache(str(tmp_path / 'cache.db'))


def clearbit_live(url, params):
    return FakeResponse(200, CLEARBIT_LIVE.get(params['query'], []))


BANK_COLS = ('cert INTEGER PRIMARY KEY, name TEXT, norm_name TEXT, city TEXT, state TEXT, '
             'asset_kusd INTEGER, website TEXT, est_date TEXT, holding_co TEXT, bkclass TEXT, '
             'active INTEGER, as_of TEXT')
RIA_COLS = ('crd INTEGER PRIMARY KEY, business_name TEXT, legal_name TEXT, norm_name TEXT, '
            'city TEXT, state TEXT, country TEXT, firm_type TEXT, reg_status TEXT, reg_date TEXT, '
            'website TEXT, total_employees INTEGER, raum_usd INTEGER, sec_number TEXT, as_of TEXT')


def make_oracles_db(path, with_norm=True):
    """state/oracles.db as src/pipeline/oracles.py lays it out (norm_name is
    ITS normalizer: legal forms dropped anywhere, '&' -> and -> dropped).
    Rows are synthetic in shape; the Washington Trust (washtrust.com /
    watrust.com) and BF&S (bfsfunds.com) websites match the 2026-09-08 oracle
    build. The Home Loan row is invented to exercise charter-tail + URL casing."""
    conn = sqlite3.connect(str(path))
    bank_cols = BANK_COLS if with_norm else BANK_COLS.replace('norm_name TEXT, ', '')
    conn.execute(f'CREATE TABLE bank ({bank_cols})')
    conn.execute(f'CREATE TABLE ria_firm ({RIA_COLS})')
    rows = [
        (23623, 'The Washington Trust Company, of Westerly', 'washington trust westerly', 'Westerly', 'RI',
         7_000_000, 'https://www.washtrust.com', '1800-01-01', 'WASHINGTON TRUST BCORP INC', 'SM', 1, '2026-09'),
        (1, 'Washington Trust Bank', 'washington trust bank', 'Spokane', 'WA',
         11_000_000, 'www.watrust.com', '1902-01-01', 'W.T.B. FINANCIAL CORPORATION', 'SM', 1, '2026-09'),
        (2, 'Home Loan Investment Bank, F.S.B.', 'home loan investment bank fsb', 'Warwick', 'RI',
         300_000, 'HTTP://WWW.HOMELOANBANK.COM/', '1990-01-01', None, 'SB', 1, '2026-09'),
    ]
    if with_norm:
        conn.executemany('INSERT INTO bank VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    else:
        conn.executemany('INSERT INTO bank VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         [r[:2] + r[3:] for r in rows])
    conn.execute('INSERT INTO ria_firm VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (105138, 'Bradley, Foster & Sargent, Inc.', 'BRADLEY, FOSTER & SARGENT, INC.',
                  'bradley foster sargent', 'Hartford', 'CT', 'United States', 'Registered',
                  'APPROVED', '1994-01-01', 'https://www.bfsfunds.com', 40, 5_000_000_000,
                  '801-45786', '2026-09'))
    conn.commit()
    conn.close()
    return str(path)


# ═══════════════════════════════════════════════════════════════════════════
# normalize_host
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize('raw, host, domain', [
    ('https://www.hercrentals.com/', 'hercrentals.com', 'hercrentals.com'),
    ('ir.hercrentals.com', 'ir.hercrentals.com', 'hercrentals.com'),
    ('  HTTPS://WWW2.Acme.CO.UK/about?x=1 ', 'acme.co.uk', 'acme.co.uk'),
    ('acme.com.', 'acme.com', 'acme.com'),                       # trailing dot
    ('http://acme.com:8080/x', 'acme.com', 'acme.com'),          # port dropped
    ('www.city.kingston.on.ca', 'city.kingston.on.ca', 'kingston.on.ca'),
    ('https://www.boston.ma.us/x', 'boston.ma.us', 'boston.ma.us'),
    ('portal.wcpss.k12.nc.us', 'portal.wcpss.k12.nc.us', 'wcpss.k12.nc.us'),
    ('ir.td.ca', 'ir.td.ca', 'td.ca'),                           # 2-letter .ca is a company
    ('bücher.example', 'xn--bcher-kva.example', 'xn--bcher-kva.example'),
    ('shop.acme.com.au', 'shop.acme.com.au', 'acme.com.au'),
])
def test_normalize_host_accepts(raw, host, domain):
    r = d.normalize_host(raw)
    assert (r['host'], r['domain'], r['denied']) == (host, domain, False)


@pytest.mark.parametrize('raw, reason', [
    ('192.168.1.1', 'ip'), ('http://[::1]/', 'ip'), ('localhost', 'localhost'),
    ('http://intranet.local/', 'localhost'), ('acme', 'no_dot'), ('', 'empty'), (None, 'empty'),
    ('nan', 'empty'), ('Washington Trust', 'no_dot'), ('acme.c0m', 'bad_tld'),
])
def test_normalize_host_rejects(raw, reason):
    r = d.normalize_host(raw)
    assert r['host'] is None and r['domain'] is None and r['reason'] == reason


@pytest.mark.parametrize('raw, alias', [
    ('https://www.linkedin.com/company/cherry-bekaert/', 'linkedin:cherry-bekaert'),
    ('linkedin.com/company/Cherry-Bekaert?trk=x', 'linkedin:cherry-bekaert'),
    ('https://www.zoominfo.com/c/cherry-bekaert-llp/12345', 'zoominfo:cherry-bekaert-llp'),
    ('https://www.crunchbase.com/organization/herc-rentals', 'crunchbase:herc-rentals'),
    ('https://www.prnewswire.com/news-releases/x.html', None),     # denied, no slug
    ('https://sites.google.com/view/acme', None),                   # host-level entry
])
def test_denylisted_hosts_yield_alias_never_domain(raw, alias):
    r = d.normalize_host(raw)
    assert r['domain'] is None and r['denied'] is True and r['reason'] == 'denylisted'
    assert r['alias'] == alias
    assert r['host'] is not None


def test_denylist_covers_the_spec_list():
    assert SPEC_DENYLIST <= d.DENYLIST


def test_rss_feed_hosts_are_denylisted(monkeypatch):
    cfg = {'sources': {'rss_feeds': [{'url': 'https://www.example-feed.com/feed/'},
                                     'http://rss.other-paper.co.uk/x.xml', {'url': None}, 7]}}
    assert d._rss_feed_domains(cfg) == frozenset({'example-feed.com', 'other-paper.co.uk'})
    assert d._rss_feed_domains({}) == frozenset() and d._rss_feed_domains({'sources': 3}) == frozenset()
    # The real config (config.yaml or the tracked example) is loaded at import.
    assert 'globenewswire.com' in d.RSS_FEED_DOMAINS
    monkeypatch.setattr(d, 'RSS_FEED_DOMAINS', frozenset({'vermontbiz.com'}))
    assert d.normalize_host('https://vermontbiz.com/some-company')['domain'] is None
    assert d.normalize_host('https://vermontbiz.com/x')['reason'] == 'denylisted'


@pytest.mark.parametrize('raw, flag', [
    ('wcpss.k12.nc.us', 'k12'), ('www.mit.edu', 'edu'), ('www.ri.gov', 'gov'),
    ('www.canada.gc.ca', 'gov'), ('hercrentals.com', None),
])
def test_flag_not_deny(raw, flag):
    r = d.normalize_host(raw)
    assert r['domain'] is not None and r['flag'] == flag


def test_config_loader_is_fail_soft(tmp_path):
    bad = tmp_path / 'bad.yaml'
    bad.write_text('sources: [unclosed', encoding='utf-8')
    assert d._load_config((str(tmp_path / 'missing.yaml'), str(bad))) == {}
    ok = tmp_path / 'ok.yaml'
    ok.write_text('scraper:\n  user_agent: "UA"\n', encoding='utf-8')
    assert d._load_config((str(bad), str(ok))) == {'scraper': {'user_agent': 'UA'}}


# ═══════════════════════════════════════════════════════════════════════════
# tokens / matching helpers
# ═══════════════════════════════════════════════════════════════════════════
def test_core_tokens_drop_legal_forms_anywhere():
    assert d.core_tokens('The Washington Trust Company, of Westerly') == ['washington', 'trust', 'westerly']
    assert d.core_tokens('Bank of America, National Association') == ['bank', 'america']
    assert d.core_tokens('Bradley, Foster & Sargent, Inc.') == ['bradley', 'foster', 'sargent']
    assert d.core_tokens('') == []


def test_core_tokens_collapse_dotted_abbreviations():
    assert d.core_tokens('Home Loan Investment Bank, F.S.B.') == ['home', 'loan', 'investment', 'bank']
    assert d.core_tokens('Bank of America, N.A.') == ['bank', 'america']
    assert d.core_tokens('Acme Widgets, L.L.C.') == ['acme', 'widgets']


def test_name_match_tiers():
    q = d.core_tokens('Washington Trust')
    assert d._name_match(q, ['The Washington Trust Company, of Westerly'], ['washington trust westerly']) == 'superset'
    assert d._name_match(q, ['Washington Trust'], []) == 'exact'
    assert d._name_match(q, [None], ['washington trust']) == 'exact'
    assert d._name_match(q, ['Washington Federal Bank'], []) is None
    # one-token queries never superset-match ('Washington' must not match everything)
    assert d._name_match(['washington'], ['Washington Trust Bank'], []) is None
    assert d._name_match(['citizens', 'financial', 'group'],
                         ['Citizens Bank, National Association', 'CITIZENS FINANCIAL GROUP, INC.']) == 'exact'


def test_pick_unique_never_guesses_between_two_institutions():
    a = {'domain': 'a.com', 'match': 'exact'}
    b = {'domain': 'b.com', 'match': 'exact'}
    s = {'domain': 's.com', 'match': 'superset'}
    assert d._pick_unique([a, b, s]) == (None, 'ambiguous')
    assert d._pick_unique([a, dict(a), s]) == (a, 'exact')
    assert d._pick_unique([s, {'domain': 's.com', 'match': 'superset'}]) == (s, 'superset')
    assert d._pick_unique([{'domain': 'x.com', 'match': None}]) == (None, 'no_match')
    # M3 (review 2026-09-08): ambiguity counts EVERY registrant at the best
    # tier — 'Cornerstone Advisors' had exact rows in KS (website), NC and AR
    # (none) and answered HIGH for Kansas. A lone exact registrant without a
    # website is 'no_domain', never the superset row of a different firm.
    ks = {'id': 114510, 'state': 'KS', 'domain': 'cstonegroup.com', 'match': 'exact'}
    nc = {'id': 300891, 'state': 'NC', 'domain': None, 'match': 'exact'}
    ar = {'id': 325163, 'state': 'AR', 'domain': None, 'match': 'exact'}
    assert d._pick_unique([ks, nc, ar, s]) == (None, 'ambiguous')
    assert d._pick_unique([nc, s]) == (None, 'no_domain')
    assert d._pick_unique([{'domain': None, 'match': 'exact'}]) == (None, 'no_domain')
    # two charters publishing ONE website are one institution
    twin = [{'cert': 1, 'domain': 'x.com', 'match': 'exact'}, {'cert': 2, 'domain': 'x.com', 'match': 'exact'}]
    assert d._pick_unique(twin) == (twin[0], 'exact')


def test_bank_shape_and_fdic_tokens():
    assert d.is_bank_shaped('Washington Trust') and d.is_bank_shaped('Home Loan Investment Bank')
    assert not d.is_bank_shaped('Cherry Bekaert') and not d.is_bank_shaped('Textron')
    assert d._fdic_eligible('Navigant Credit Union') is False       # NCUA, not FDIC
    assert d._fdic_tokens('The Washington Trust Company') == ['washington', 'trust']
    assert d._fdic_tokens('First Federal Savings Bank') == ['first']


def test_state_from_hints_and_aliases():
    assert d._state_from_hints({'hq': 'Westerly, RI'}) == 'RI'
    assert d._state_from_hints({'hq_state': 'ct'}) == 'CT'          # typed column
    assert d._state_from_hints({'state': 'XX', 'hq': 'Toronto, ON, Canada'}) == 'ON'
    assert d._state_from_hints({}) is None
    assert d._hint_aliases({'zi': 'https://www.zoominfo.com/c/acme-inc/99', 'cik': '0000217346',
                            'crd': 105138, 'linkedin': 'https://linkedin.com/company/acme'}) == [
        'zoominfo:acme-inc', 'linkedin:acme', 'cik:217346', 'crd:105138']
    # M4 (review 2026-09-08): a bare word is not a ZoomInfo id — the enricher
    # once passed the subindustry label and every account got 'zoominfo:banking'
    assert d._hint_aliases({'zi': 'Banking'}) == [] and d._hint_aliases({'zi': 'acme-inc'}) == []
    assert d._hint_aliases({'zi': '123456789'}) == ['zoominfo:123456789']
    assert d._hint_aliases({'zi': 'www.zoominfo.com/c/acme-inc/99'}) == ['zoominfo:acme-inc']
    assert d._hint_aliases({}) == []


def test_env_flags_default_off_for_guess(monkeypatch):
    monkeypatch.delenv('DOMAINS_GUESS_ENABLED', raising=False)
    assert d._env_flag('DOMAINS_GUESS_ENABLED', False) is False
    monkeypatch.setenv('DOMAINS_GUESS_ENABLED', 'yes')
    assert d._env_flag('DOMAINS_GUESS_ENABLED', False) is True
    monkeypatch.setenv('DOMAINS_GUESS_ENABLED', '  ')
    assert d._env_flag('DOMAINS_GUESS_ENABLED', False) is False


# ═══════════════════════════════════════════════════════════════════════════
# resolve() — API shape and rung 0/1: hint url, cache
# ═══════════════════════════════════════════════════════════════════════════
def test_resolve_signature_and_result_shape(net):
    sig = inspect.signature(d.resolve)
    assert list(sig.parameters) == ['name', 'hints', 'cache', 'now']
    assert sig.parameters['cache'].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters['now'].kind is inspect.Parameter.KEYWORD_ONLY
    res = d.resolve('Herc Rentals', {'url': 'https://ir.hercrentals.com/news'})
    assert set(res) == {'domain', 'host', 'method', 'confidence', 'aliases', 'evidence'}
    json.dumps(res)                                    # plain data for the caller
    assert d.resolve('', None)['method'] is None and d.resolve(None)['domain'] is None


def test_hint_url_with_name_token_is_medium_evidence_and_never_the_identity(net, cache):
    """H2 (review 2026-09-08): a hint url is MEDIUM at most and is not
    persisted — an article-only LLM pass derives urls from the company name,
    and a name-derived url passes the name-token test by construction."""
    res = d.resolve('Herc Holdings Inc.', {'url': 'https://ir.hercrentals.com/news/x', 'cik': '217346'},
                    cache=cache, now=T0)
    assert (res['domain'], res['host'], res['method'], res['confidence']) == (
        'hercrentals.com', 'ir.hercrentals.com', 'hint_url', 'medium')
    assert res['evidence']['answer']['matched_tokens'] == ['herc']
    assert res['evidence']['answer']['name_match'] is True and res['evidence']['answer']['persisted'] is False
    assert net.calls == []
    fg = cache.get_firmographics('herc holdings', now=T0)
    assert fg == {'aliases': ['cik:217346']}                       # aliases yes, domain never
    assert cache.get_search('herc holdings', 'domain:hint_url', now=T0) is None
    # a later resolve without the hint does not find a cached identity
    net.routes[CLEARBIT] = FakeResponse(200, [])
    later = d.resolve('Herc Holdings Inc.', cache=cache, now=T0)
    assert later['domain'] is None and 'cache:miss' in later['evidence']['rungs']


def test_hint_url_without_name_token_is_medium_but_still_beats_network(net):
    res = d.resolve('Cherry Bekaert', {'url': 'https://www.cbh.com/'})
    assert (res['domain'], res['method'], res['confidence']) == ('cbh.com', 'hint_url', 'medium')
    assert net.calls == []
    assert 'hint_url:medium' in res['evidence']['rungs']


def test_denylisted_hint_becomes_alias_and_ladder_continues(net, cache):
    net.routes[CLEARBIT] = clearbit_live
    res = d.resolve('Cherry Bekaert', {'url': 'https://www.linkedin.com/company/cherry-bekaert/'},
                    cache=cache, now=T0)
    assert res['aliases'] == ['linkedin:cherry-bekaert']
    assert (res['domain'], res['method'], res['confidence']) == ('cbh.com', 'clearbit', 'medium')
    assert 'hint_url:denylisted' in res['evidence']['rungs']
    fg = cache.get_firmographics('cherry bekaert', now=T0)
    assert fg['aliases'] == ['linkedin:cherry-bekaert'] and fg['domain'] == 'cbh.com'


def test_cache_hit_short_circuits(net, cache):
    cache.set_firmographics('cherry bekaert', {'domain': 'cbh.com', 'domain_method': 'clearbit',
                                               'domain_confidence': 'medium'}, now=T0)
    res = d.resolve('Cherry Bekaert LLP', cache=cache, now=T0 + timedelta(days=10))
    assert (res['domain'], res['host'], res['method'], res['confidence']) == (
        'cbh.com', 'cbh.com', 'cache', 'medium')
    assert res['evidence']['answer'] == {'cached_method': 'clearbit'}
    assert net.calls == []
    # stale (> 365d) is a miss -> network again
    net.routes[CLEARBIT] = FakeResponse(200, CLEARBIT_LIVE['Cherry Bekaert'])
    res2 = d.resolve('Cherry Bekaert LLP', cache=cache, now=T0 + timedelta(days=400))
    assert res2['method'] == 'clearbit' and len(net.calls_to(CLEARBIT)) == 1


def test_medium_hint_yields_to_a_fresh_cache_entry(net, cache):
    cache.set_firmographics('cherry bekaert', {'domain': 'cbh.com', 'domain_method': 'oracle',
                                               'domain_confidence': 'high'}, now=T0)
    res = d.resolve('Cherry Bekaert', {'url': 'https://www.cb.cpa/'}, cache=cache, now=T0)
    assert (res['domain'], res['method'], res['confidence']) == ('cbh.com', 'cache', 'high')


# ═══════════════════════════════════════════════════════════════════════════
# rung 2: local oracle tables
# ═══════════════════════════════════════════════════════════════════════════
def test_oracle_bank_hit_needs_the_state_to_split_same_name_banks(net, tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', make_oracles_db(tmp_path / 'oracles.db'))
    ri = d.resolve('Washington Trust', {'hq': 'Westerly, RI'})
    assert (ri['domain'], ri['method'], ri['confidence']) == ('washtrust.com', 'oracle', 'high')
    assert ri['evidence']['answer'] == {'table': 'bank', 'id': 23623, 'match': 'superset',
                                        'name': 'The Washington Trust Company, of Westerly', 'state': 'RI'}
    wa = d.resolve('Washington Trust', {'state': 'WA'})
    assert (wa['domain'], wa['method']) == ('watrust.com', 'oracle')
    assert net.calls == []                             # oracle answers cost nothing
    # No state: two institutions -> ambiguous -> falls through to the network.
    net.routes[CLEARBIT] = clearbit_live
    none = d.resolve('Washington Trust')
    assert none['domain'] is None and 'oracle:ambiguous' in none['evidence']['rungs']
    assert 'clearbit:bank_multi_defer' in none['evidence']['rungs']
    assert [c['source'] for c in none['evidence']['candidates']][:2] == ['oracle', 'oracle']


def test_oracle_ria_hit_by_identity_tokens(net, tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', make_oracles_db(tmp_path / 'oracles.db'))
    res = d.resolve('Bradley Foster & Sargent')
    assert (res['domain'], res['host'], res['method'], res['confidence']) == (
        'bfsfunds.com', 'bfsfunds.com', 'oracle', 'high')
    assert res['evidence']['answer']['table'] == 'ria_firm' and res['evidence']['answer']['id'] == 105138
    assert net.calls == []
    # a hinted state that disagrees is a miss, not a wrong answer (ladder continues)
    net.routes[CLEARBIT] = FakeResponse(200, [])
    miss = d.resolve('Bradley Foster & Sargent', {'state': 'MA'})
    assert miss['domain'] is None and 'oracle:miss' in miss['evidence']['rungs']
    assert len(net.calls_to(CLEARBIT)) == 1


def test_oracle_charter_tail_and_website_normalizing(net, tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', make_oracles_db(tmp_path / 'oracles.db'))
    res = d.resolve('Home Loan Investment Bank', {'hq': 'Warwick, RI'})
    assert (res['domain'], res['method'], res['evidence']['answer']['match']) == (
        'homeloanbank.com', 'oracle', 'exact')


def test_oracle_without_norm_name_column_still_works(net, tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', make_oracles_db(tmp_path / 'old.db', with_norm=False))
    res = d.resolve('Washington Trust', {'state': 'RI'})
    assert (res['domain'], res['method']) == ('washtrust.com', 'oracle')


def test_oracle_absent_or_corrupt_is_fail_soft(net, tmp_path, monkeypatch, cache):
    net.routes[CLEARBIT] = clearbit_live
    absent = d.resolve('Bradley, Foster & Sargent', cache=cache, now=T0)
    assert 'oracle:absent' in absent['evidence']['rungs']
    corrupt = tmp_path / 'corrupt.db'
    corrupt.write_bytes(b'this is not a sqlite file' * 40)
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', str(corrupt))
    fresh = AccountCache(str(tmp_path / 'fresh-cache.db'))
    res = d.resolve('Bradley, Foster & Sargent', cache=fresh, now=T0)
    assert 'oracle:unreadable' in res['evidence']['rungs']
    # L1 (review 2026-09-08): an oracle READ error is transient — the network
    # rungs still run, but the miss is NOT negative-cached for 7/30/90 days
    assert res['method'] == 'error' and res['evidence']['errors'] == {'oracle': 'oracle:DatabaseError'}
    assert 'warnings' not in res['evidence']
    assert fresh.should_skip('bradley foster and sargent', 'domain', now=T0) is False
    assert len(net.calls_to(CLEARBIT)) == 2                       # absent + corrupt: the ladder still ran
    # …and the real case: the monthly refresh holds the write lock
    locked = make_oracles_db(tmp_path / 'locked.db')
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', locked)
    holder = sqlite3.connect(locked)
    holder.execute('BEGIN EXCLUSIVE')
    try:
        res = d.resolve('Bradley, Foster & Sargent', cache=fresh, now=T0)
    finally:
        holder.rollback()
        holder.close()
    assert res['method'] == 'error' and res['evidence']['errors'] == {'oracle': 'oracle:OperationalError'}
    assert fresh.should_skip('bradley foster and sargent', 'domain', now=T0) is False
    # once the lock is gone the registry answers, no Clearbit spent
    net.calls.clear()
    ok = d.resolve('Bradley, Foster & Sargent', cache=fresh, now=T0)
    assert (ok['domain'], ok['method']) == ('bfsfunds.com', 'oracle') and net.calls == []


# ═══════════════════════════════════════════════════════════════════════════
# rung 3: FDIC BankFind
# ═══════════════════════════════════════════════════════════════════════════
def test_fdic_needs_a_us_state(net):
    net.routes[CLEARBIT] = clearbit_live
    res = d.resolve('Washington Trust')
    assert 'fdic:no_state' in res['evidence']['rungs'] and net.calls_to(FDIC) == []
    res = d.resolve('Washington Trust', {'hq': 'Toronto, ON'})
    assert 'fdic:no_state' in res['evidence']['rungs'] and net.calls_to(FDIC) == []
    res = d.resolve('Cherry Bekaert', {'hq': 'Richmond, VA'})
    assert 'fdic:not_bank' in res['evidence']['rungs'] and net.calls_to(FDIC) == []
    res = d.resolve('Navigant Credit Union', {'hq': 'Smithfield, RI'})
    assert 'fdic:not_bank' in res['evidence']['rungs'] and net.calls_to(FDIC) == []


def test_fdic_hit_uses_the_verified_filter_shape(net, cache):
    net.routes[FDIC] = FakeResponse(200, FDIC_LIVE)
    res = d.resolve('Washington Trust', {'hq': 'Westerly, RI'}, cache=cache, now=T0)
    assert (res['domain'], res['host'], res['method'], res['confidence']) == (
        'washtrust.com', 'washtrust.com', 'fdic', 'high')
    assert res['evidence']['answer'] == {'cert': 23623, 'name': 'The Washington Trust Company, of Westerly',
                                         'city': 'Westerly', 'state': 'RI', 'match': 'superset'}
    [call] = net.calls_to(FDIC)
    assert call['params'] == {'filters': 'ACTIVE:1 AND STALP:RI AND (NAME:*WASHINGTON* AND NAME:*TRUST*)',
                              'fields': d.FDIC_FIELDS, 'format': 'json', 'limit': 25}
    assert net.calls_to(CLEARBIT) == []                # stopped at the first confident answer
    assert cache.get_firmographics('washington trust', now=T0) == {
        'domain': 'washtrust.com', 'domain_method': 'fdic', 'domain_confidence': 'high'}
    raw = cache.get_search('washington trust', 'domain:fdic', now=T0)
    assert raw['chosen'] == 'washtrust.com' and raw['results'][0]['cert'] == 23623


def test_fdic_state_picks_the_other_bank(net):
    net.routes[FDIC] = FakeResponse(200, FDIC_WA)
    res = d.resolve('Washington Trust', {'state': 'WA'})
    assert (res['domain'], res['method']) == ('watrust.com', 'fdic')
    assert 'STALP:WA' in net.calls_to(FDIC)[0]['params']['filters']


def test_fdic_ambiguous_rows_fall_through_but_seed_agreement(net):
    two = {'data': [
        {'data': {'NAME': 'Washington Trust Bank', 'STALP': 'RI', 'WEBADDR': 'washtrust.com', 'CERT': 1}},
        {'data': {'NAME': 'Washington Trust Savings Bank', 'STALP': 'RI', 'WEBADDR': 'wtsb.com', 'CERT': 2}},
    ]}
    net.routes[FDIC] = FakeResponse(200, two)
    net.routes[CLEARBIT] = FakeResponse(200, [{'name': 'Washington Trust', 'domain': 'washtrust.com', 'logo': None}])
    res = d.resolve('Washington Trust', {'state': 'RI'})
    assert 'fdic:ambiguous' in res['evidence']['rungs']
    # Clearbit's single exact result agrees with an FDIC candidate -> high
    assert (res['domain'], res['method'], res['confidence']) == ('washtrust.com', 'clearbit', 'high')
    assert res['evidence']['answer']['agrees_with_oracle'] is True


def test_fdic_bad_filter_400_is_a_miss_not_an_error(net):
    net.routes[FDIC] = FakeResponse(400, {'error': 'bad filter'})
    net.routes[CLEARBIT] = FakeResponse(200, [])
    res = d.resolve('Washington Trust', {'state': 'RI'})
    assert 'fdic:miss' in res['evidence']['rungs'] and res['evidence']['errors'] == {}


# ═══════════════════════════════════════════════════════════════════════════
# rung 4: Clearbit accept rules — the frozen 8-name table
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize('name', sorted(CLEARBIT_LIVE))
def test_clearbit_frozen_research_outcomes(net, name):
    net.routes[CLEARBIT] = clearbit_live
    expected_domain, outcome = CLEARBIT_EXPECTED[name]
    res = d.resolve(name)
    assert res['domain'] == expected_domain
    assert f'clearbit:{outcome}' in res['evidence']['rungs']
    if expected_domain:
        assert (res['method'], res['confidence']) == ('clearbit', 'medium')
        assert res['evidence']['answer']['match'] == 'exact_top1'
    else:
        assert res['method'] is None
    [call] = net.calls_to(CLEARBIT)
    assert call['params'] == {'query': name} and call['url'] == d.CLEARBIT_URL


def test_clearbit_single_superset_accepted_multi_rejected(net):
    # L5 (review 2026-09-08): a one-token query is contained by anything —
    # 'Herc' ⊂ 'Herc Rentals' — so single_superset needs ≥ 2 query tokens
    net.routes[CLEARBIT] = FakeResponse(200, CLEARBIT_LIVE['Herc Rentals'])
    res = d.resolve('Herc')
    assert res['domain'] is None and 'clearbit:short_query' in res['evidence']['rungs']
    net.routes[CLEARBIT] = FakeResponse(200, [
        {'name': 'Cherry Bekaert Advisory LLC', 'domain': 'cbh.com', 'logo': None}])
    res = d.resolve('Cherry Bekaert')
    assert (res['domain'], res['evidence']['answer']['match']) == ('cbh.com', 'single_superset')
    net.routes[CLEARBIT] = FakeResponse(200, [
        {'name': 'Cherry Bekaert Advisory LLC', 'domain': 'cbh.com', 'logo': None},
        {'name': 'Cherry Bekaert Wealth', 'domain': 'cbwealth.example', 'logo': None}])
    res = d.resolve('Cherry Bekaert')
    assert res['domain'] is None and 'clearbit:multi_superset' in res['evidence']['rungs']
    net.routes[CLEARBIT] = FakeResponse(200, [{'name': 'Textron Aviation', 'domain': 'txtav.com', 'logo': None}])
    res = d.resolve('Cherry Bekaert')
    assert res['domain'] is None and 'clearbit:no_match' in res['evidence']['rungs']


def test_clearbit_never_picks_between_two_banks_without_state(net):
    net.routes[CLEARBIT] = clearbit_live
    res = d.resolve('Washington Trust')
    assert res['domain'] is None
    names = [c['name'] for c in res['evidence']['candidates'] if c['source'] == 'clearbit']
    assert names[:2] == ['Washington Trust Bank', 'Washington Trust Bank']   # RI vs WA look identical


def test_clearbit_kill_switch(net, monkeypatch):
    monkeypatch.setattr(d, 'DOMAINS_CLEARBIT_ENABLED', False)
    res = d.resolve('Cherry Bekaert')
    assert 'clearbit:disabled' in res['evidence']['rungs'] and net.calls == []


def test_clearbit_denylisted_or_junk_results_are_ignored(net):
    net.routes[CLEARBIT] = FakeResponse(200, [
        {'name': 'Cherry Bekaert', 'domain': 'linkedin.com', 'logo': None}, 'junk', {'name': 'x'}])
    res = d.resolve('Cherry Bekaert')
    assert res['domain'] is None and 'clearbit:miss' in res['evidence']['rungs']


# ═══════════════════════════════════════════════════════════════════════════
# rung 5: SEC submissions (CIK known)
# ═══════════════════════════════════════════════════════════════════════════
def test_sec_only_when_cik_known_and_after_clearbit(net):
    net.routes[CLEARBIT] = FakeResponse(200, [])
    res = d.resolve('Textron')
    assert 'sec:no_cik' in res['evidence']['rungs'] and net.calls_to(SEC) == []
    net.routes[SEC] = FakeResponse(200, dict(SEC_TEXTRON_EMPTY, website='https://www.textron.com'))
    res = d.resolve('Textron', {'cik': '0000217346'})
    assert (res['domain'], res['method'], res['confidence']) == ('textron.com', 'sec', 'high')
    assert res['aliases'] == ['cik:217346']
    assert net.calls_to(SEC)[-1]['url'] == 'https://data.sec.gov/submissions/CIK0000217346.json'
    # Clearbit ran first (spec order); a Clearbit hit would have stopped the ladder.
    net.routes[CLEARBIT] = clearbit_live
    net.calls.clear()
    res = d.resolve('Textron', {'cik': 217346})
    assert res['method'] == 'clearbit' and net.calls_to(SEC) == []


def test_sec_empty_website_is_a_miss_and_404_is_not_an_error(net, cache):
    net.routes[CLEARBIT] = FakeResponse(200, [])
    net.routes[SEC] = FakeResponse(200, SEC_TEXTRON_EMPTY)          # live 2026-09-08 shape
    res = d.resolve('Textron', {'cik': 217346}, cache=cache, now=T0)
    assert res['domain'] is None and 'sec:empty' in res['evidence']['rungs'] and res['method'] is None
    net.routes[SEC] = FakeResponse(404, text='not found')
    res = d.resolve('Textron', {'cik': 999999999}, cache=cache, now=T0 + timedelta(days=8))
    assert 'sec:miss' in res['evidence']['rungs'] and res['evidence']['errors'] == {}


def test_sec_name_disagreement_is_demoted(net):
    net.routes[CLEARBIT] = FakeResponse(200, [])
    net.routes[SEC] = FakeResponse(200, {'name': 'ACQUIRER HOLDINGS CORP', 'website': 'acquirer.example'})
    res = d.resolve('Cherry Bekaert', {'cik': 1})
    assert (res['domain'], res['confidence'], res['evidence']['answer']['name_agrees']) == (
        'acquirer.example', 'medium', False)


# ═══════════════════════════════════════════════════════════════════════════
# negative cache, transport errors, retries
# ═══════════════════════════════════════════════════════════════════════════
def test_clean_miss_negative_caches_and_next_call_is_skipped(net, cache):
    net.routes[CLEARBIT] = clearbit_live
    res = d.resolve('Bradley, Foster & Sargent', {'zi': 'https://www.zoominfo.com/c/bfs/42'},
                    cache=cache, now=T0)
    key = 'bradley foster and sargent'
    assert res['method'] is None and res['domain'] is None
    assert cache.should_skip(key, 'domain', now=T0 + timedelta(days=1)) is True
    assert cache.get_firmographics(key, now=T0) == {'aliases': ['zoominfo:bfs']}
    net.calls.clear()
    again = d.resolve('Bradley, Foster & Sargent', cache=cache, now=T0 + timedelta(days=1))
    assert again['method'] == 'skipped' and net.calls == []
    assert again['aliases'] == ['zoominfo:bfs']                  # aliases survive the miss
    # after the 7-day ladder it looks again
    net.routes[CLEARBIT] = FakeResponse(200, [{'name': 'Bradley Foster & Sargent', 'domain': 'bfsfunds.com', 'logo': None}])
    later = d.resolve('Bradley, Foster & Sargent', cache=cache, now=T0 + timedelta(days=8))
    assert later['method'] == 'clearbit' and cache.should_skip(key, 'domain', now=T0 + timedelta(days=9)) is False


@pytest.mark.parametrize('responder, err', [
    (requests.ConnectionError('boom'), 'transport:ConnectionError'),
    (requests.Timeout('slow'), 'transport:Timeout'),
    (FakeResponse(503, text='down'), 'http:503'),
    (FakeResponse(429, text='slow down'), 'http:429'),
    (FakeResponse(403, text='forbidden'), 'http:403'),          # Clearbit switched off ≠ no such company
    (FakeResponse(404, text='gone'), 'http:404'),               # M7: the endpoint retired ≠ no such company
    (FakeResponse(410, text='gone'), 'http:410'),
    (FakeResponse(204, text=''), 'http:204'),
    (FakeResponse(200, None, text='<html>cloudflare challenge</html>'), 'badjson'),
])
def test_transport_trouble_is_error_and_not_negative_cached(net, cache, responder, err):
    net.routes[CLEARBIT] = responder
    res = d.resolve('Cherry Bekaert', cache=cache, now=T0)
    assert res['method'] == 'error' and res['domain'] is None
    assert res['evidence']['errors'] == {'clearbit': err}
    assert cache.should_skip('cherry bekaert', 'domain', now=T0) is False
    assert cache.stats()['negative_cache'] == 0


def test_one_retry_on_5xx_only(net):
    net.routes[CLEARBIT] = FakeResponse(503)
    d.resolve('Cherry Bekaert')
    assert len(net.calls_to(CLEARBIT)) == 2
    net.calls.clear()
    net.routes[CLEARBIT] = FakeResponse(429)
    d.resolve('Cherry Bekaert')
    assert len(net.calls_to(CLEARBIT)) == 1
    net.calls.clear()
    seen = []

    def flaky(url, params):
        seen.append(1)
        return FakeResponse(502) if len(seen) == 1 else FakeResponse(200, CLEARBIT_LIVE['Cherry Bekaert'])
    net.routes[CLEARBIT] = flaky
    assert d.resolve('Cherry Bekaert')['domain'] == 'cbh.com'


def test_live_switch_off_skips_every_network_rung(net, monkeypatch, cache):
    monkeypatch.setattr(d, 'DOMAINS_LIVE_ENABLED', False)
    res = d.resolve('Cherry Bekaert', cache=cache, now=T0)
    assert res['method'] == 'skipped' and net.calls == []
    assert cache.should_skip('cherry bekaert', 'domain', now=T0) is False   # not a miss either


def test_sessions_carry_per_endpoint_user_agents(monkeypatch):
    monkeypatch.setattr(d, '_SESSIONS', {})
    made = []
    monkeypatch.setattr(d, '_new_session', lambda ua: made.append(ua) or FakeSession())
    assert d._session_for('sec') is d._session_for('sec')
    d._session_for('clearbit')
    assert made == [d.SEC_USER_AGENT, d.DEFAULT_USER_AGENT]
    assert d.SEC_USER_AGENT != d.DEFAULT_USER_AGENT and d.TIMEOUT == (5, 10)


def test_real_session_factory_sets_headers():
    s = d._new_session(d.SEC_USER_AGENT)              # real requests.Session; no network
    try:
        assert s.headers['User-Agent'] == d.SEC_USER_AGENT
        assert 'application/json' in s.headers['Accept']
    finally:
        s.close()


# ═══════════════════════════════════════════════════════════════════════════
# rung 6: guess-and-verify (off by default; never the identity)
# ═══════════════════════════════════════════════════════════════════════════
def test_guess_is_off_by_default(net, cache):
    net.routes[CLEARBIT] = FakeResponse(200, [])
    res = d.resolve('Cherry Bekaert', cache=cache, now=T0)
    assert 'guess:disabled' in res['evidence']['rungs']
    assert all(c['method'] == 'GET' for c in net.calls)


def test_guess_enabled_is_low_and_never_persisted_as_identity(net, cache, monkeypatch):
    monkeypatch.setattr(d, 'DOMAINS_GUESS_ENABLED', True)
    net.routes[CLEARBIT] = FakeResponse(200, [])
    net.routes['https://cherrybekaert.com/'] = FakeResponse(200, url='https://cherrybekaert.com/')
    res = d.resolve('Cherry Bekaert', cache=cache, now=T0)
    assert (res['domain'], res['method'], res['confidence']) == ('cherrybekaert.com', 'guess', 'low')
    assert res['evidence']['answer']['sole_identity'] is False
    head = [c for c in net.calls if c['method'] == 'HEAD']
    assert head == [{'method': 'HEAD', 'url': 'https://cherrybekaert.com/', 'params': {}}]
    assert cache.get_firmographics('cherry bekaert', now=T0) is None          # not the identity
    assert cache.get_search('cherry bekaert', 'domain:guess', now=T0)['chosen'] == 'cherrybekaert.com'
    # parked redirect / NXDOMAIN are misses, not answers
    net.routes['https://cherrybekaert.com/'] = FakeResponse(200, url='https://www.hugedomains.com/domain_profile.cfm?d=cherrybekaert.com')
    assert d.resolve('Cherry Bekaert')['domain'] is None
    net.routes['https://cherrybekaert.com/'] = requests.ConnectionError('NXDOMAIN')
    res = d.resolve('Cherry Bekaert')
    assert res['domain'] is None and res['method'] is None and 'guess:miss' in res['evidence']['rungs']


# ═══════════════════════════════════════════════════════════════════════════
# persistence + fail-soft cache
# ═══════════════════════════════════════════════════════════════════════════
def test_hit_persists_identity_and_raw_payload_and_clears_negative(net, cache):
    cache.record_empty('cherry bekaert', 'domain', rung='scrape', now=T0 - timedelta(days=30))
    net.routes[CLEARBIT] = clearbit_live
    res = d.resolve('Cherry Bekaert', cache=cache, now=T0)
    assert res['method'] == 'clearbit'
    assert cache.get_firmographics('cherry bekaert', now=T0) == {
        'domain': 'cbh.com', 'domain_method': 'clearbit', 'domain_confidence': 'medium'}
    raw = cache.get_search('cherry bekaert', 'domain:clearbit', now=T0)
    assert raw['chosen'] == 'cbh.com' and [r['domain'] for r in raw['results']] == ['cbh.com', 'cb.cpa']
    assert cache.stats()['negative_cache'] == 0


class BrokenCache:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError('cache down')
        return boom


def test_broken_cache_never_breaks_a_resolve(net):
    net.routes[CLEARBIT] = clearbit_live
    res = d.resolve('Cherry Bekaert', cache=BrokenCache(), now=T0)
    assert (res['domain'], res['method']) == ('cbh.com', 'clearbit')
    res = d.resolve('Bradley, Foster & Sargent', cache=BrokenCache(), now=T0)
    assert res['method'] is None


def test_internal_bug_degrades_to_error_not_exception(net, monkeypatch, cache):
    def explode(*a, **k):
        raise KeyError('bug')
    monkeypatch.setattr(d, '_rung_oracle', explode)
    res = d.resolve('Cherry Bekaert', cache=cache, now=T0)
    assert (res['method'], res['domain'], res['evidence']['errors']) == ('error', None, {'internal': 'KeyError'})
    assert cache.should_skip('cherry bekaert', 'domain', now=T0) is False
    json.dumps(res)


def test_evidence_is_plain_json_data(net, tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', make_oracles_db(tmp_path / 'oracles.db'))
    net.routes[FDIC] = FakeResponse(200, FDIC_LIVE)
    net.routes[CLEARBIT] = clearbit_live
    for name, hints in [('Washington Trust', {'hq': 'Westerly, RI', 'source': 'pbn'}),
                        ('Washington Trust', None), ('Textron', {'cik': 217346}),
                        ('Herc Rentals', {'url': 'https://ir.hercrentals.com/'})]:
        res = d.resolve(name, hints)
        json.dumps(res)
        assert res['method'] in d.METHODS and res['confidence'] in ('high', 'medium', 'low', None)


# ═══════════════════════════════════════════════════════════════════════════
# Review 2026-09-08 fixes
# ═══════════════════════════════════════════════════════════════════════════
def test_m3_oracle_ambiguity_counts_registrants_without_websites(net, tmp_path, monkeypatch):
    """The real shape (state/oracles.db, 2026-09-08): 'Cornerstone Advisors'
    is an exact registrant in KS (website), NC and AR (no website)."""
    path = tmp_path / 'oracles.db'
    conn = sqlite3.connect(str(path))
    conn.execute(f'CREATE TABLE ria_firm ({RIA_COLS})')
    conn.execute(f'CREATE TABLE bank ({BANK_COLS})')
    for crd, name, city, st, web in (
            (114510, 'CORNERSTONE ADVISORS', 'Overland Park', 'KS', 'https://www.cstonegroup.com'),
            (300891, 'CORNERSTONE ADVISORS, LLC', 'Charlotte', 'NC', None),
            (325163, 'CORNERSTONE ADVISORS LLC', 'Little Rock', 'AR', None),
            (124947, 'CORNERSTONE WEALTH ADVISORS, INC.', 'Minneapolis', 'MN', 'https://cornerstonewealthadvisors.com')):
        conn.execute('INSERT INTO ria_firm VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (crd, name, name, 'cornerstone advisors' if 'WEALTH' not in name else 'cornerstone wealth advisors',
                      city, st, 'United States', 'Registered', 'APPROVED', '2000-01-01', web, 10, 500_000_000,
                      f'801-{crd}', '2026-09'))
    conn.commit()
    conn.close()
    monkeypatch.setattr(d, 'ORACLES_DB_PATH', str(path))
    net.routes[CLEARBIT] = FakeResponse(200, [])
    none = d.resolve('Cornerstone Advisors')
    assert none['domain'] is None and 'oracle:ambiguous' in none['evidence']['rungs']
    assert sorted(c['state'] for c in none['evidence']['candidates'] if c['match'] == 'exact') == ['AR', 'KS', 'NC']
    ks = d.resolve('Cornerstone Advisors', {'state': 'KS'})
    assert (ks['domain'], ks['method'], ks['confidence'], ks['evidence']['answer']['id']) == (
        'cstonegroup.com', 'oracle', 'high', 114510)
    # the NC registrant exists but lists no website: no_domain, never the MN superset
    nc = d.resolve('Cornerstone Advisors', {'state': 'NC'})
    assert nc['domain'] is None and 'oracle:no_domain' in nc['evidence']['rungs']


def test_l2_fdic_endpoint_is_the_oracles_constant():
    from src.pipeline import oracles as o
    assert d.FDIC_URL == o.FDIC_INSTITUTIONS_URL == 'https://api.fdic.gov/banks/institutions'


def test_m7_clearbit_retirement_is_not_a_miss_for_every_account(net, cache):
    net.routes[CLEARBIT] = FakeResponse(404, text='Not Found')
    for i, name in enumerate(('Cherry Bekaert', 'Herc Rentals', 'Textron')):
        res = d.resolve(name, cache=cache, now=T0)
        assert res['method'] == 'error' and res['evidence']['errors'] == {'clearbit': 'http:404'}
    assert cache.stats()['negative_cache'] == 0
    # FDIC / SEC keep the definitive-miss reading of a plain 4xx
    net.routes[FDIC] = FakeResponse(404, text='Not Found')
    net.routes[CLEARBIT] = FakeResponse(200, [])
    res = d.resolve('Washington Trust', {'state': 'RI'}, cache=cache, now=T0)
    assert 'fdic:miss' in res['evidence']['rungs'] and res['evidence']['errors'] == {}
