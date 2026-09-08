"""src/pipeline/hashtag_guards.py — the declarative hashtag guard table
(Phase 4 slice C2, 2026-09-08). One positive and one negative case per tag,
plain dict inputs, no network.

The '# ── Review 2026-09-08 (Phase 4)' block at the end pins the six
guards the adversarial review found STRICTER than the rubric (3a-3h); each
test fails if its fix is reverted."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import enrichment_scout as es  # noqa: E402
from src.pipeline import hashtag_guards as g  # noqa: E402
from src.pipeline.hashtag_guards import HASHTAG_GUARDS, apply_guards  # noqa: E402


def _ev(**kw):
    ev = {'id': 'ev1', 'event_type': 'cfo_hire', 'title': 'Acme names Jane Doe CFO',
          'description': 'BOSTON, MA — Acme today named Jane Doe Chief Financial Officer.',
          'source_url': 'https://example.test/x'}
    ev.update(kw)
    return ev


ACME = {'name': 'Acme', 'role': 'Hiring Company', 'zi_subindustry': 'Banking', 'size': '51-200'}


def _check(tag, event, account=None, fit=None, evidence='', companies=None):
    kept, notes = apply_guards([tag], event, account if account is not None else ACME,
                               fit or {}, evidence, companies_data=companies)
    return tag in kept, notes


# ── One positive and one negative per tag ──────────────────────────────────
CASES = {
    '#NewCFO': (
        dict(event=_ev()),
        dict(event=_ev(title='Acme Reports Q2 Results',
                       description='"We delivered," said Jane Doe, CFO.')),
        'no CFO hire subject'),
    '#NewController': (
        dict(event=_ev(event_type='executive_hire', title='Acme names Jane Doe Corporate Controller',
                       description='Doe will report to CFO John Smith.')),
        dict(event=_ev()),
        'CFO-equivalent'),
    '#Acquisitions': (
        dict(event=_ev(event_type='merger_acquisition', title='Acme acquires Beta'),
             account=dict(ACME, role='Acquirer')),
        dict(event=_ev(event_type='merger_acquisition', title='Acme acquires Beta'),
             account=dict(ACME, role='Target')),
        'not the acquirer'),
    '#Funding': (
        dict(event=_ev(event_type='funding', title='Acme raises $25 million Series B')),
        dict(event=_ev(event_type='funding', title='Acme raises $500K seed round')),
        '$500K < $1M'),
    '#PEBacked': (
        dict(event=_ev(description='Acme, a portfolio company of Beta Capital, named a CFO.')),
        dict(event=_ev()),
        'no private-equity'),
    '#100EE': (
        dict(event=_ev(), account=dict(ACME, size='201-500')),
        dict(event=_ev(), account=dict(ACME, size='51-200')),
        "'51-200' alone"),
    '#Global': (
        dict(event=_ev(description='Acme has offices in the United States, Canada and Germany.')),
        dict(event=_ev(description='Acme is headquartered in Toronto, Canada.')),
        'need ≥ 2'),
    '#AssetManagerScale': (
        dict(event=_ev(), evidence='AUM SEARCH: Acme manages $1.2 billion in assets under management'),
        dict(event=_ev(), evidence='AUM SEARCH: nothing about assets'),
        'no AUM'),
    '#FormerUser': (None, dict(event=_ev(description='Acme is a former NetSuite customer.')), 'CRM-only'),
    '#PrevConvo': (None, dict(event=_ev(description='We had a demo with Acme last year.')), 'CRM-only'),
    '#Franchisor': (
        dict(event=_ev(description='Acme, a franchisor of 40 salons, named a CFO.')),
        dict(event=_ev()),
        'no franchise'),
    '#Franchisee': (
        dict(event=_ev(description='Acme operates 12 franchised Dunkin locations.')),
        dict(event=_ev()),
        'no franchise'),
    '#HoldCo': (
        dict(event=_ev(title='Acme Holdings names Jane Doe CFO'), fit={'zi_subindustry': 'Banking'}),
        dict(event=_ev(), fit={'zi_subindustry': 'Banking'}),
        'is not Holding Companies'),
    '#Locations': (
        dict(event=_ev(description='Acme operates 14 branches across New England.')),
        dict(event=_ev(description='Acme operates a branch in Boston.')),
        'no numeric location count'),
    '#Entities': (
        dict(event=_ev(), evidence='COMPLEXITY SEARCH: parent of three subsidiaries'),
        dict(event=_ev(), evidence='COMPLEXITY SEARCH: a subsidiary of Beta'),
        'no numeric subsidiary'),
    '#HyperGrowth': (
        dict(event=_ev(description='Acme grew revenue 45% year over year.')),
        dict(event=_ev(description='Acme pays a 7% coupon on its notes.')),
        'no growth figure'),
    '#Legacy': (
        dict(event=_ev(description='Founded in 1985, Acme serves New England.')),
        dict(event=_ev(description='Founded in 2015, Acme serves New England.')),
        'founded 2015'),
}


def test_every_guarded_tag_has_a_case_and_every_rubric_tag_has_a_guard():
    assert set(CASES) == set(HASHTAG_GUARDS)
    assert set(HASHTAG_GUARDS) == set(es.TAL_V11_HASHTAG_POINTS)


@pytest.mark.parametrize('tag', sorted(CASES))
def test_positive_case_keeps_the_tag(tag):
    pos, _neg, _why = CASES[tag]
    if pos is None:
        pytest.skip(f'{tag} is always stripped')
    ok, notes = _check(tag, **pos)
    assert ok, (tag, notes)
    assert notes == []


@pytest.mark.parametrize('tag', sorted(CASES))
def test_negative_case_strips_the_tag_with_a_reason(tag):
    _pos, neg, why = CASES[tag]
    ok, notes = _check(tag, **neg)
    assert not ok, tag
    assert len(notes) == 1 and notes[0].startswith(f'-{tag} (') and why in notes[0], notes


# ── Guard details ───────────────────────────────────────────────────────────
def test_new_cfo_uses_the_rubric_split_not_the_event_type():
    # a cfo_hire label without a CFO hire subject (an earnings release) loses the tag
    ok, notes = _check('#NewCFO', _ev(event_type='cfo_hire', title='Acme Reports Q2 Results',
                                     description='said Jane Doe, CFO'))
    assert not ok and 'event_type cfo_hire' in notes[0]
    # VP Finance / Director of Finance are CFO-equivalents (+5); Controller / CAO are not
    assert _check('#NewCFO', _ev(event_type='executive_hire', title='Acme names Jane Doe VP Finance'))[0]
    assert _check('#NewCFO', _ev(event_type='executive_hire',
                                 title='Acme hires Jane Doe as Director of Finance'))[0]
    ok, notes = _check('#NewCFO', _ev(event_type='executive_hire',
                                     title='Acme taps Jane Doe as Chief Accounting Officer'))
    assert not ok and 'use #NewController' in notes[0]
    assert _check('#NewController', _ev(event_type='executive_hire',
                                        title='Acme taps Jane Doe as Chief Accounting Officer'))[0]
    # a Treasurer earns neither rubric seat
    assert not _check('#NewCFO', _ev(title='Acme appoints Jane Doe Treasurer'))[0]
    assert not _check('#NewController', _ev(title='Acme appoints Jane Doe Treasurer'))[0]


def test_open_finance_seat_is_a_controller_trigger_never_a_new_cfo():
    ev = _ev(event_type='finance_seat_open', title='Chief Financial Officer',
             description='Acme is hiring a CFO in Boston, MA.')
    assert not _check('#NewCFO', ev)[0]
    assert _check('#NewController', ev)[0]


def test_acquisitions_needs_the_ma_event_type_and_the_acquirer_or_primary_role():
    ev = _ev(event_type='merger_acquisition', title='Acme to acquire Beta')
    assert _check('#Acquisitions', ev, dict(ACME, role='Primary'))[0]
    assert not _check('#Acquisitions', _ev(event_type='funding'), dict(ACME, role='Acquirer'))[0]


def test_funding_needs_a_for_profit_and_a_parseable_amount():
    ev = _ev(event_type='funding', title='Acme raises $25M')
    ok, notes = _check('#Funding', ev, fit={'zi_subindustry': 'Religious Organizations'})
    assert not ok and 'nonprofit' in notes[0]
    ok, notes = _check('#Funding', _ev(event_type='funding', title='Acme closes a growth round'))
    assert not ok and 'no parseable amount' in notes[0]
    assert _check('#Funding', _ev(event_type='funding', title='Acme raises $1,000,000'))[0]


def test_pe_backed_accepts_an_investor_role_on_the_event():
    companies = [{'name': 'Acme', 'role': 'Portfolio Company'},
                 {'name': 'Beta Capital', 'role': 'Lead Investor'}]
    assert _check('#PEBacked', _ev(event_type='funding'), companies=companies)[0]
    assert not _check('#PEBacked', _ev(event_type='funding'),
                      companies=[{'name': 'Acme', 'role': 'Portfolio Company'}])[0]


def test_100ee_accepts_a_headcount_figure_in_evidence_but_not_a_small_bucket():
    ev = _ev()
    assert _check('#100EE', ev, dict(ACME, size='51-200'),
                  evidence='ZoomInfo: Acme has 250 employees')[0]
    assert _check('#100EE', ev, dict(ACME, size='51-200'),
                  evidence='RocketReach: a 120-person team')[0]
    assert not _check('#100EE', ev, dict(ACME, size='51-200'), evidence='founded in 2019')[0]
    assert _check('#100EE', ev, dict(ACME, size='1,001-5,000'))[0]
    assert _check('#100EE', ev, dict(ACME, size=None), evidence='employs more than 1,200 people')[0]
    assert not _check('#100EE', ev, dict(ACME, size=None))[0]


def test_global_counts_distinct_countries_only():
    assert g.countries_in('offices in the US, Canada and the U.K.; join us') == {'US', 'CA', 'UK'}
    assert g.countries_in('Boston, London and Paris') == set()
    assert g.countries_in('England and Scotland') == {'UK'}        # one country, two names
    assert not _check('#Global', _ev(description='offices in England and Scotland'))[0]


def test_asset_manager_scale_iapd_needs_1b_raum():
    iapd = dict(ACME, registry_source='sec_iapd')
    ev = _ev(event_type='expansion', title='New SEC-registered investment adviser: Acme Capital',
             description='Regulatory assets under management: $2,000,000,000. Employees: 30.')
    assert _check('#AssetManagerScale', ev, iapd)[0]
    small = _ev(event_type='expansion',
                description='Regulatory assets under management: $250,000,000. Employees: 3.')
    ok, notes = _check('#AssetManagerScale', small, iapd)
    assert not ok and '$250M < $1B' in notes[0]
    assert _check('#AssetManagerScale', small, dict(iapd, raum_usd=3_000_000_000))[0]
    # a non-registry account with the figure in the article text passes too
    assert _check('#AssetManagerScale', _ev(description='Acme, with $850M in AUM, named a CFO.'))[0]


def test_locations_and_entities_read_number_words_and_qualifiers():
    assert _check('#Locations', _ev(description='Acme operates twelve offices.'))[0]
    assert _check('#Locations', _ev(description='more than 40 stores'))[0]
    assert not _check('#Locations', _ev(description='opened in 2019 offices downtown'))[0]
    assert _check('#Entities', _ev(description='Acme is the parent of 5 operating companies.'))[0]
    assert not _check('#Entities', _ev(description='Acme is a wholly-owned subsidiary of Beta.'))[0]


def test_hyper_growth_needs_a_growth_figure_not_any_percentage():
    assert _check('#HyperGrowth', _ev(description='3x revenue growth since 2023'))[0]
    assert _check('#HyperGrowth', _ev(description='Acme doubled its headcount'))[0]
    assert _check('#HyperGrowth', _ev(description='named to the Inc. 5000 list'))[0]
    assert not _check('#HyperGrowth', _ev(description='a 4.5% mortgage rate'))[0]


def test_legacy_accepts_a_named_legacy_system():
    assert _check('#Legacy', _ev(), evidence='Acme runs its books on QuickBooks Enterprise')[0]
    assert not _check('#Legacy', _ev(description='established in 2001'))[0]


# ── apply_guards contract ───────────────────────────────────────────────────
def test_apply_guards_preserves_order_passes_unknown_tags_and_never_mutates_the_event():
    ev = _ev(event_type='funding', title='Acme raises $25M',
             description='BOSTON, MA — Acme closed a $25M Series B led by Beta Ventures.')
    before = dict(ev)
    kept, notes = apply_guards(['#Funding', '#Made-up', '#PrevConvo', '#NewCFO'], ev, ACME, {}, '',
                               companies_data=[{'name': 'Acme', 'role': 'Portfolio Company'}])
    assert kept == ['#Funding', '#Made-up']
    assert [n.split(' ')[0] for n in notes] == ['-#PrevConvo', '-#NewCFO']
    assert ev == before and 'companies_data' not in ev
    assert apply_guards([], ev) == ([], [])
    assert apply_guards(None, None) == ([], [])


def test_a_broken_guard_keeps_the_tag(monkeypatch):
    def boom(*a):
        raise RuntimeError('no')
    monkeypatch.setitem(HASHTAG_GUARDS, '#Funding', boom)
    kept, notes = apply_guards(['#Funding'], _ev(event_type='funding', title='$25M'))
    assert kept == ['#Funding'] and notes == []


def test_nonprofit_vocabulary_matches_enrichment_scout():
    assert g.NONPROFIT_SUBINDUSTRIES == {k for k, v in es.ZI_SUBINDUSTRIES.items()
                                         if v == es.NONPROFIT_VERTICAL}
    assert g.HOLDCO_SUBINDUSTRY in es.ZI_SUBINDUSTRIES
    assert es._parse_funding_amount is g.parse_funding_amount
    assert es._parse_funding_amount('$6.8 Million and $37M') == 37_000_000
    assert g.format_usd(500_000) == '$500K' and g.format_usd(1_000_000) == '$1M'
    assert g.format_usd(2_500_000_000) == '$2.5B' and g.format_usd(None) == 'n/a'


# ── Review 2026-09-08 (Phase 4): guards must never be stricter than the rubric ──
SEC_URL = 'https://www.sec.gov/Archives/edgar/data/1840856/000121390026097712/0001213900-26-097712-index.htm'
SEC_DESC = ('SEC 8-K filing by Acme Corp (DE) — Item 5.02: Departure/Election of Directors or '
            'Officers. Filing date: 2026-09-04. SIC: 7372 (Services-Prepackaged Software).')
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _sec(**kw):
    return _ev(title='SEC 8-K Item 5.02 (Departure/Election of Directors or Officers) — Acme Corp',
               description=SEC_DESC, source_url=SEC_URL, **kw)


def test_3a_sec_502_cfo_filing_keeps_new_cfo_with_a_kept_note():
    # the stored description never carries the hire subject — the scraper's
    # "Chief Financial Officer" full-text typing is the evidence
    kept, notes = apply_guards(['#NewCFO'], _sec(event_type='cfo_hire'), ACME)
    assert kept == ['#NewCFO']
    assert notes == ['#NewCFO kept — SEC 5.02 CFO filing']          # logged as 'guard: …'
    assert g.strip_count(notes) == 0
    # one seat down: a 5.02 typed executive_hire names a Controller / CAO
    kept, notes = apply_guards(['#NewCFO', '#NewController'], _sec(event_type='executive_hire'), ACME)
    assert kept == ['#NewController']
    assert notes[0].startswith('-#NewCFO (') and notes[1] == '#NewController kept — SEC 5.02 Controller/CAO filing'
    assert g.strip_count(notes) == 1
    # the exception is source-level: a non-EDGAR cfo_hire with no hire subject still loses the tag
    ok, notes = _check('#NewCFO', _ev(event_type='cfo_hire', title='Acme Reports Q2 Results',
                                     description='said Jane Doe, CFO'))
    assert not ok and 'event_type cfo_hire' in notes[0]
    # and an EDGAR row typed anything else takes the normal path
    assert not _check('#NewCFO', _sec(event_type='merger_acquisition'))[0]
    assert not _check('#NewController', _sec(event_type='cfo_hire'))[0]
    assert not g._is_sec_filing({'source_url': 'https://sec.gov.example.com/x'})
    assert g._is_sec_filing({'source_url': 'https://www.sec.gov/x'})


FUNDING_BLOCK = ('FUNDING SEARCH ("Acme funding"):\n'
                 '- Acme raises $12M Series A - TechCrunch | https://tc.example/{y}/06/01/acme\n'
                 '  BOSTON — Acme announced June 1, {y} it has raised $12 million in Series A '
                 'funding led by Beta Ventures to chase a $40 billion market.')


def test_3b_funding_on_a_non_funding_event_accepts_a_recent_raise_in_the_evidence(monkeypatch):
    monkeypatch.setattr(g, '_now', lambda: NOW)
    ev = _ev()                                                  # a cfo_hire
    ok, notes = _check('#Funding', ev, evidence=FUNDING_BLOCK.format(y=2025))
    assert ok and notes == []
    # 18 months: a raise dated 2021 is stale, whatever its size
    ok, notes = _check('#Funding', ev, evidence=FUNDING_BLOCK.format(y=2021))
    assert not ok and 'older than 18 months' in notes[0]
    # undated evidence is accepted (the rubric calls the block authoritative)
    ok, _ = _check('#Funding', ev, evidence='FUNDING SEARCH ("Acme funding"):\n'
                   '- Acme secures $8M growth round | https://x.example/a\n  Acme secured $8 million.')
    assert ok
    # the $1M floor and the nonprofit rule still apply
    ok, notes = _check('#Funding', ev, evidence='FUNDING SEARCH ("Acme funding"):\n'
                       '- Acme raises $500K pre-seed | https://x.example/a\n  raised $500K, March 2026.')
    assert not ok and '$500K < $1M' in notes[0]
    ok, notes = _check('#Funding', ev, fit={'zi_subindustry': 'Religious Organizations'},
                       evidence=FUNDING_BLOCK.format(y=2026))
    assert not ok and 'nonprofit' in notes[0]
    # no evidence at all → stripped, with the event_type in the reason
    ok, notes = _check('#Funding', ev)
    assert not ok and 'event_type cfo_hire' in notes[0] and 'FUNDING SEARCH' in notes[0]
    # the event's own text counts too ("fresh off a $20M Series B in June")
    assert _check('#Funding', _ev(description='Acme, which closed a $20M Series B in June 2026, '
                                              'today named Jane Doe CFO.'))[0]
    # a deal value is not a raise: no raise/round/funding language → stripped
    ok, notes = _check('#Funding', _ev(event_type='merger_acquisition',
                                       title='Acme closes $50M acquisition of Beta'))
    assert not ok and 'not stated as a raise' in notes[0]
    ok, notes = _check('#Funding', _ev(), evidence='FUNDING SEARCH ("Acme funding"):\n'
                       '- Acme completes $50M acquisition | https://x.example/a\n  Acme closed the $50 million purchase of Beta on 2026-06-01.')
    assert not ok and 'not stated as a raise' in notes[0]
    # a funding event keeps the pre-review rule: amount in the event text
    assert _check('#Funding', _ev(event_type='funding', title='Acme raises $25M'))[0]
    assert not _check('#Funding', _ev(event_type='funding', title='Acme closes a growth round'),
                      evidence=FUNDING_BLOCK.format(y=2026))[0]


def test_3b_funding_evidence_parses_the_block_per_hit(monkeypatch):
    monkeypatch.setattr(g, '_now', lambda: NOW)
    two = ('FUNDING SEARCH ("Acme funding"):\n'
           '- Acme raised $30M in 2019 | https://x.example/old\n  Acme raised $30 million in 2019.\n'
           '- Acme raises $6M | https://x.example/new\n  Acme raised $6 million on 2026-05-01.\n'
           '\nCOMPLEXITY SEARCH ("Acme locations"):\n- 40 offices | https://x.example/loc')
    assert g.funding_evidence(two) == (6_000_000.0, 'amount $6M dated 2026-05-01')
    assert g.funding_evidence('no money here')[0] is None
    assert g.funding_evidence('')[0] is None
    assert g.months_ago(NOW, 18) == datetime(2025, 3, 8, 12, 0, tzinfo=timezone.utc)
    assert g.months_ago(datetime(2026, 3, 31, tzinfo=timezone.utc), 1).date().isoformat() == '2026-02-28'


def test_3b_dates_in_reads_the_latest_instant_and_never_rereads_a_full_date(monkeypatch):
    monkeypatch.setattr(g, '_now', lambda: NOW)
    got = sorted(d.date().isoformat() for d in g.dates_in(
        'raised Feb 1, 2025 · 2024-06-01 · Sept 2023 · Q1 2025 · in 2019 · 03/04/2026 · '
        '12 March 2024 · $2,024 · 12% · https://tc.example/2025/06/01/acme · due 2031'))
    assert got == ['2019-12-31', '2023-09-30', '2024-03-12', '2024-06-01', '2025-02-01',
                   '2025-03-31', '2025-06-01', '2026-03-04']
    assert g.dates_in('$2,024 raised') == []


def test_3c_holdco_ors_the_rubrics_three_tests():
    # 1. the name / text says Holdings or holding company (a bank holding company too)
    assert _check('#HoldCo', _ev(title='Acme Bancorp names Jane Doe CFO',
                                 description='Acme Bancorp is the bank holding company for Acme Bank.'),
                  fit={'zi_subindustry': 'Banking'})[0]
    assert _check('#HoldCo', _ev(), account=dict(ACME, name='Acme Holdings LLC'))[0]
    # 2. the industry is the holdco label
    assert _check('#HoldCo', _ev(), fit={'zi_subindustry': g.HOLDCO_SUBINDUSTRY})[0]
    # 3. evidence of ≥ 2 operating subsidiaries
    assert _check('#HoldCo', _ev(), fit={'zi_subindustry': 'Banking'},
                  evidence='COMPLEXITY SEARCH: Acme is the parent of three operating subsidiaries')[0]
    # none of the three → stripped, naming all three misses
    ok, notes = _check('#HoldCo', _ev(), fit={'zi_subindustry': 'Banking'},
                       evidence='a wholly-owned subsidiary of Beta')
    assert not ok and 'no "holding company"' in notes[0] and 'no subsidiary count' in notes[0]


def test_3d_global_counts_the_accounts_own_hq_country():
    # a US-HQ bank opening a London office: the text names ONE country, the HQ is the other
    assert _check('#Global', _ev(description='Acme opened an office in London, United Kingdom.'),
                  account=dict(ACME, hq='Boston, MA'))[0]
    # the HQ country never double-counts itself
    ok, notes = _check('#Global', _ev(description='Acme is headquartered in Toronto, Canada.'),
                       account=dict(ACME, hq='Toronto, ON'))
    assert not ok and 'HQ country CA' in notes[0]
    assert not _check('#Global', _ev(description='Acme serves clients across the United States.'),
                      account=dict(ACME, hq='Boston, MA'))[0]
    # an unknown HQ adds nothing
    assert not _check('#Global', _ev(description='offices in Germany'), account=dict(ACME, hq=None))[0]
    assert g.home_country({'hq': 'Boston, MA'}) == 'US' and g.home_country({'hq': 'MA'}) == 'US'
    assert g.home_country({'hq': 'Toronto, ON, Canada'}) == 'CA' and g.home_country({'hq': 'Toronto, Canada'}) == 'CA'
    assert g.home_country({'hq': 'London, United Kingdom'}) == 'UK'
    assert g.home_country({'hq': 'Paris'}) is None and g.home_country({}) is None
    assert g.home_country({'hq_state': 'QC'}) == 'CA'


@pytest.mark.parametrize('size,ok', [
    ('100+', True), ('100-500', True), ('101-250', True), ('100-249', True), ('201-500', True),
    ('1,001-5,000', True), ('51-200', False), ('11-50', False), ('99', False),
])
def test_3f_100ee_accepts_any_bucket_whose_low_bound_is_100(size, ok):
    got, notes = _check('#100EE', _ev(), dict(ACME, size=size))
    assert got is ok, (size, notes)
    if not ok:
        assert f"size bucket '{size}' alone" in notes[0]
    assert g.SIZE_BUCKET_MIN == 100


def test_3g_asset_manager_scale_text_path_needs_250m_and_reads_client_assets():
    ok, notes = _check('#AssetManagerScale', _ev(description='Acme, a $5 million AUM boutique, named a CFO.'))
    assert not ok and '$5M < $250M' in notes[0]
    assert _check('#AssetManagerScale', _ev(description='Acme manages $1.2 billion in client assets.'))[0]
    assert _check('#AssetManagerScale', _ev(description='Acme manages $600 million in assets for 400 families.'))[0]
    assert _check('#AssetManagerScale', _ev(), evidence='AUM SEARCH: assets under advisement of $700M')[0]
    assert _check('#AssetManagerScale', _ev(description='Acme, with $250M in AUM, named a CFO.'))[0]
    assert not _check('#AssetManagerScale', _ev(description='Acme, with $249M in AUM, named a CFO.'))[0]
    # the IAPD path keeps its own $1B bar
    assert not _check('#AssetManagerScale', _ev(description='Regulatory assets under management: $600,000,000.'),
                      dict(ACME, registry_source='sec_iapd'))[0]


def test_3h_parse_funding_amount_prefers_the_verb_adjacent_figure():
    assert g.parse_funding_amount('Acme raises $500K seed to chase a $40 billion market') == 500_000
    assert g.parse_funding_amount('Acme closes $25 million round; total raised to date $60M') == 25_000_000
    assert g.parse_funding_amount('Acme secures $8M in a market worth $2B') == 8_000_000
    # no raise word anywhere → the largest figure, as before
    assert g.parse_funding_amount('$6.8 Million and $37M') == 37_000_000
    assert g.parse_funding_amount('Total offering: $2,500,000') == 2_500_000
    assert g.parse_funding_amount('no money') is None
    # the search tier reads the same figure: a $500K seed is tier 3 whatever market it quotes
    assert es._event_search_tier({'event_type': 'funding',
                                  'title': 'Acme raises $500K seed to chase a $40 billion market'}) == 3
    assert es._event_search_tier({'event_type': 'funding', 'title': 'Acme raises $12M Series A'}) == 1
    # the #Funding guard on a funding event follows it too
    ok, notes = _check('#Funding', _ev(event_type='funding',
                                       title='Acme raises $500K seed to chase a $40 billion market'))
    assert not ok and '$500K < $1M' in notes[0]


def test_apply_guards_kept_notes_are_not_strips():
    kept, notes = apply_guards(['#NewCFO', '#PrevConvo'], _sec(event_type='cfo_hire'), ACME)
    assert kept == ['#NewCFO']
    assert notes == ['#NewCFO kept — SEC 5.02 CFO filing',
                     '-#PrevConvo (CRM-only fact — no pipeline input can evidence it)']
    assert g.strip_count(notes) == 1 and g.strip_count([]) == 0 and g.strip_count(None) == 0
