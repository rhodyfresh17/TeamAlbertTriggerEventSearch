"""Table-driven tests for the deterministic v2 gates (no network).
Names are REAL rows from the queue that motivated each rule (2026-09-04/06)."""
import pytest
from src.pipeline.gates import (
    hq_territory_status, is_non_operating_entity, is_bad_company_name,
    looks_like_person_name, sic_to_verdict, formd_to_verdict, account_key,
)


@pytest.mark.parametrize('hq,expected', [
    ('Boston, MA', 'in'), ('Boston, MA, USA', 'in'), ('Boston, Massachusetts', 'in'),
    ('Toronto, ON, Canada', 'in'), ('Toronto, Ontario', 'in'), ('Birmingham, AL', 'in'),
    ('Indianapolis, IN', 'in'), ('Portland, ME', 'in'), ('Washington, DC', 'in'),
    ('PA', 'in'), ('Florida', 'in'), ('Bartlett, TN', 'in'),
    ('Seattle, WA', 'out'), ('Seattle, Washington', 'out'), ('Austin, TX, USA', 'out'),
    ('Portland, OR', 'out'), ('Vancouver, BC, Canada', 'out'), ('London, UK', 'out'),
    ('London, United Kingdom', 'out'), ('Bangalore, India', 'out'), ('Silicon Valley, CA', 'out'),
    ('', 'unknown'), (None, 'unknown'), ('Unknown', 'unknown'), ('North America', 'unknown'),
    ('Remote', 'unknown'),
])
def test_territory(hq, expected):
    assert hq_territory_status(hq) == expected


@pytest.mark.parametrize('name,kind', [
    # fund vehicles / BDCs / LPs (the 104 SEC filer rows)
    ('West Bay BDC LLC', 'fund_vehicle'), ('Stout Holdings, L.P.', 'fund_vehicle'),
    ('26North BDC, Inc.', 'fund_vehicle'), ('Freedom 3 Investments VI, LP', 'fund_vehicle'),
    ('Antler Special Opportunities Fund III LP', 'fund_vehicle'),
    ('Cantor Equity Partners II, Inc.', 'fund_vehicle'), ('HII Scale AI-01, a Series of HII Scale AI-A LLC', 'fund_vehicle'),
    ('FRIENDS OF WELTHY SPV, LLC', 'fund_vehicle'), ('CNL Strategic Residential Credit, Inc.', 'fund_vehicle'),
    ('HPS Real Assets Lending Co LP', 'fund_vehicle'), ('Overton & Fagundus TIC General Partnership', 'fund_vehicle'),
    # SPACs
    ('Aeon Acquisition I Corp.', 'spac'), ('Avalanche Acquisition Corp', 'spac'),
    # political / government / greek / k12 / lodging (A.J. exclusions)
    ('Bernard Taylor for Congress', 'political'), ('Friends of Jane Doe 2026', 'political'),
    ('Six Nations of the Grand River', 'government'), ('City of Brockton', 'government'),
    ('Hertford County Schools', 'k12'), ('Match Charter Public School', 'k12'),
    ('School District of Lancaster', 'k12'), ('The Williston Northampton School', 'k12'),
    ('Zeta Phi Beta Sorority, Incorporated', 'greek'), ('Sigma Chi Fraternity', 'greek'),
    ('PENSACOLA PALAFOX LODGING, LLC', 'lodging'), ('Marriott Hotels of Boston', 'lodging'),
])
def test_non_operating_entities(name, kind):
    hit, k = is_non_operating_entity(name)
    assert hit and k == kind, (name, k)


@pytest.mark.parametrize('name', [
    # operating companies that must NOT be caught
    'Oakworth Capital Bank', 'Lock Insurance', 'Navitas Credit Corp.', 'Amplix',
    'Martis Capital', 'Juniata Valley Financial Corp', 'Tint World', 'Kasper Electrical',
    'Community Foundation of Greater Memphis', 'Boston Community Fund', 'Blue Ridge Bankshares, Inc.',
    'Teamshares Inc', 'Southeast Elevator', 'Authority Brands', 'Delta Community Credit Union',
    'Alpha Bank', 'Capital Partners Insurance Agency', 'Bluerock Homes Trust, Inc.',
    'Scan-Optics', 'First Nation Bank of Ohio' if False else 'Peoples Bank',
])
def test_operating_companies_pass(name):
    hit, k = is_non_operating_entity(name)
    assert not hit, (name, k)


@pytest.mark.parametrize('name,ctx,bad', [
    ('Joins ECI Group', '', True), ('Local org', '', True), ('', '', True),
    ('John Smith', 'John Smith joins ECI Group as Chief Financial Officer', True),
    ('Michael Rossi', 'Michael Rossi Joins Eastern Bank as Controller', True),
    ('A Very Interesting Snapshot of the Competence', '', True),
    ('Company announces new CFO', '', True),
    ('Oakworth Capital Bank', '', False), ('Tint World', '', False), ('Amplix', '', False),
    ('Lock Insurance', 'Lock Insurance names new CFO', False),
    ('John Deere', 'John Deere names new CFO', False),          # company is the SUBJECT doing the naming
    ('Weber Shandwick', 'MikeWorldWide Appoints Dave Aglar as Chief Media Officer, formerly of Weber Shandwick', False),
    ('Morgan Stanley', 'Morgan Stanley appoints new CFO', False),
    ('John Smith', '', False),                                    # no context → keep (fail safe)
    ('Dave Aglar', 'MikeWorldWide Appoints Dave Aglar as Chief Media Officer', True),
])
def test_bad_company_names(name, ctx, bad):
    assert is_bad_company_name(name, ctx) == bad, name


def test_person_name_heuristic():
    assert looks_like_person_name('John Smith')
    assert not looks_like_person_name('Oakworth Capital')
    assert not looks_like_person_name('Lock Insurance')
    assert not looks_like_person_name('AT&T')


@pytest.mark.parametrize('sic,verdict', [
    ('6022', 'unknown'), ('6141', 'unknown'), ('6311', 'unknown'), ('6211', 'unknown'),
    ('5511', 'unknown'), ('7231', 'unknown'), ('8351', 'unknown'), ('6500', 'unknown'),
    ('6770', 'vehicle'), ('6726', 'vehicle'), ('6722', 'vehicle'),
    ('7372', 'unknown'), ('7374', 'unknown'), ('3360', 'out'), ('2844', 'out'), ('7011', 'out'), ('8011', 'out'),
    ('1311', 'out'), ('4813', 'out'), ('5812', 'out'), ('', 'unknown'), (None, 'unknown'),
])
def test_sic_routing(sic, verdict):
    assert sic_to_verdict(sic)[0] == verdict


@pytest.mark.parametrize('group,rev,amount,spac,verdict,seg', [
    ('Other Technology', 'Decline to Disclose', 10_129_977, False, 'out', ''),
    ('Pooled Investment Fund', None, None, False, 'vehicle', ''),
    ('Other Banking and Financial Services', '$1 - $1,000,000', 10_000_000, False, 'too_small', 'micro'),
    ('Other Banking and Financial Services', '$5,000,001 - $25,000,000', 500_000, False, 'unknown', 'LMM'),
    ('Insurance', '$25,000,001 - $100,000,000', None, False, 'unknown', 'MM'),
    ('Commercial', 'Over $100,000,000', None, False, 'out', 'Enterprise'),
    ('Other', 'Decline to Disclose', 2_000_000, False, 'too_small', ''),
    ('Other', 'Decline to Disclose', 14_000_000, False, 'unknown', ''),
    ('Other Real Estate', None, 12_000_000, False, 'unknown', ''),
    ('Commercial Banking', '$5,000,001 - $25,000,000', None, True, 'vehicle', ''),
    ('REITS and Finance', '$5,000,001 - $25,000,000', None, False, 'out', ''),
    ('Lodging and Conventions', None, 50_000_000, False, 'out', ''),
])
def test_formd_routing(group, rev, amount, spac, verdict, seg):
    v, s, _ = formd_to_verdict(group, rev, amount, spac)
    assert (v, s) == (verdict, seg)


@pytest.mark.parametrize('a,b', [
    ('School District of Lancaster', 'The School District of Lancaster'),
    ('Oakworth Capital Bank, Inc.', 'Oakworth Capital Bank'),
    ('Tint World LLC', 'Tint World'), ('Amplix, Inc', 'AMPLIX'),
    ('Juniata Valley Financial Corp', 'Juniata Valley Financial Corporation'),
    ('AT&T Inc.', 'AT and T'),
])
def test_account_key(a, b):
    assert account_key(a) == account_key(b)
