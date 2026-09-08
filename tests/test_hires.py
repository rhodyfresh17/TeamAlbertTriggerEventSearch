"""src/pipeline/hires.py — the finance-leader hire SUBJECT detector
(Phase 4 slice C2, 2026-09-08). Table-driven, no network.

The table pins the rule "the role must be the subject of a hire verb (or a
hire noun phrase) — never attribution, an interim / former / outgoing seat,
a board seat, an award or a device controller" and keeps the vocabulary in
step with src/scrapers/base.py."""
import pytest

from src.pipeline import hires
from src.pipeline.hires import (
    FINANCE_ROLES, NEW_CFO_ROLES, NEW_CONTROLLER_ROLES, finance_hire_subject, role_label,
)
from src.scrapers import base as scraper_base


def _role(title, description=''):
    return finance_hire_subject(title, description)['role']


# ── Real hires, every seat ─────────────────────────────────────────────────
@pytest.mark.parametrize('title,role', [
    ('Acme Names Jane Doe CFO', 'cfo'),
    ('Acme Names Jane Doe Chief Financial Officer', 'cfo'),
    ('Acme Appoints Jane Doe as President & Chief Financial Officer', 'cfo'),
    ('Acme appoints Jane Doe, CPA, as Chief Financial and Administrative Officer', 'cfo'),
    ('Jane Doe promoted to CFO at Acme', 'cfo'),
    ('Acme announces CFO transition', 'cfo'),
    ('Acme announces the appointment of Jane Doe as CFO', 'cfo'),
    ('Meet the incoming CFO of Acme', 'cfo'),
    ('Diego Reynoso to join Ingredion as Chief Financial Officer', 'cfo'),
    ('Acme welcomes new CFO Jane Doe', 'cfo'),
    ('Acme Names Jane Doe Corporate Controller', 'controller'),
    ('Acme names Jane Doe controller', 'controller'),
    ('Acme hires Jane Doe as plant controller', 'controller'),
    ('Acme elevates Jane Doe to controller', 'controller'),
    ('Acme names Jane Doe Vice President of Accounting', 'controller'),
    ('Acme taps Jane Doe as VP Accounting', 'controller'),
    ('Acme welcomes Jane Doe as VP of Finance', 'vp_finance'),
    ('Acme promotes Jane Doe to VP Finance', 'vp_finance'),
    ('Acme names Jane Doe head of finance', 'vp_finance'),
    ('Acme hires Jane Doe as Finance Director', 'finance_director'),
    ('Acme names Jane Doe Director of Finance', 'finance_director'),
    ('Acme taps Jane Doe as Chief Accounting Officer', 'cao'),
    ('Acme appoints Jane Doe Treasurer', 'treasurer'),
])
def test_real_hires_name_the_seat(title, role):
    got = finance_hire_subject(title)
    assert got['role'] == role, title
    assert got['kind'] == ('cfo' if role == 'cfo' else 'exec')
    assert got['evidence'] and got['evidence'] in ' '.join(title.split())


# ── Not hires ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('title,description', [
    # attribution — an earnings release quoting the CFO
    ('Acme Reports Second Quarter 2026 Results',
     'BOSTON, Aug. 5, 2026 -- Acme today announced results for the quarter. '
     '"We delivered," said Jane Doe, CFO. Acme also announced a dividend.'),
    ('Acme Holdings to Acquire Beta Services',
     'Acme Holdings today announced a definitive agreement to acquire Beta Services. '
     '"Beta is a natural fit," said Jane Doe, CFO of Acme Holdings.'),
    ('Acme expands into Ohio', 'According to the CFO, the expansion adds 40 jobs.'),
    ('Acme raises $20M', 'Chief Financial Officer John Smith commented on the raise.'),
    ('Acme CFO discusses the energy transition', ''),
    # interim / acting / former / outgoing
    ('Acme appoints Jane Doe as interim CFO', ''),
    ('Acme names Jane Doe acting controller', ''),
    ('Acme appoints Jane Doe as interim CFO to replace departing CFO John Smith', ''),
    ('Former CFO Jane Doe joins Acme board', ''),
    ('Acme announces the retirement of CFO John Smith', 'Smith, the outgoing CFO, will retire.'),
    # board seats
    ('Jane Doe, CFO of Acme, Appointed to Beta Board of Directors', ''),
    ('Acme welcomes CFO Jane Doe to its board', ''),
    ('Acme appoints Jane Doe, CFO of Beta, to its board', ''),
    ('Beta CFO Jane Doe joins Acme board of directors', ''),
    # awards / lists
    ('Acme CFO Jane Doe Named CFO of the Year', ''),
    ('Acme CFO Jane Doe Named to Power 100 List', ''),
    ('Acme names CFO Jane Doe to its 40 Under 40 list', ''),
    # weak words, products, devices, unrelated seats
    ('Acme announces CFO', ''), ('Acme adds CFO', ''), ('Acme CFO appointment', ''),
    ('Acme Announces Launch of Wireless Game Controller', ''),
    ('Acme names Beta its motor controller supplier', ''),
    ('Acme launches new controller for smart homes', ''),
    ('Acme taps new motor controller supplier', ''),
    ('Vermont State Treasurer announces unclaimed property', ''),
    ('Acme announces new CEO', ''),
    # the person's past seat, in a non-finance hire
    ('Acme names Jane Doe CEO', 'Doe previously served as CFO of Beta Corp for six years.'),
    ('Acme names Jane Doe COO', 'Doe joins from Beta, where she spent a decade as controller.'),
    ('Acme names Jane Doe COO', 'Doe brings two decades of experience as CFO of public companies.'),
    ('Acme names Jane Doe COO', 'Doe, who spent 20 years as chief financial officer of Beta, starts May 1.'),
    # attribution after a closing quote — 'added' still counts there (review 2026-09-08, 3e(i))
    ('Acme Reports Q2 Results', '"We delivered on every metric," added Jane Doe, CFO.'),
    ('Acme Reports Q2 Results', '"We delivered," Jane Doe, CFO, added.'),
    # a career adjective directly on the SEAT is the sitting officer (3e(iv))
    ('Acme names Jane Doe CEO as longtime CFO John Smith retires', ''),
    ('Longtime CFO John Smith to retire from Acme', ''),
    # the boss the new hire reports to (no seat named in the title)
    ('Acme strengthens its operations team',
     'Acme today announced Jane Doe as VP Operations. Doe will report to Chief Financial '
     'Officer John Smith.'),
    ('', ''), (None, None),
])
def test_not_hires(title, description):
    got = finance_hire_subject(title, description)
    assert got == {'role': None, 'kind': None, 'evidence': ''}, (title, got)


# ── The seat being FILLED wins over the seats around it ────────────────────
def test_destination_seat_wins_over_the_current_one():
    assert _role('Acme promotes Controller Jane Doe to CFO') == 'cfo'
    assert _role('Acme Names Jane Doe Corporate Controller',
                 'Doe will report to Chief Financial Officer John Smith.') == 'controller'
    assert _role('Acme Names Jane Doe Chief Financial Officer',
                 'Doe succeeds John Smith, who is retiring as CFO after 20 years. She joins '
                 'from Beta where she served as controller.') == 'cfo'
    assert _role('Former Google CFO Jane Doe joins Acme as CFO') == 'cfo'
    assert _role('Acme Names Jane Doe CFO and Board Member') == 'cfo'


def test_successor_to_a_retiring_officer_is_a_hire_into_that_seat():
    assert _role('Acme names Jane Doe to succeed retiring CFO John Smith') == 'cfo'
    assert _role('Acme appoints Jane Doe to replace outgoing controller John Smith') == 'controller'
    # … unless the appointment is itself interim
    assert _role('Acme names interim CFO to replace departing CFO John Smith') is None


def test_description_counts_when_the_title_names_no_seat():
    assert _role('Acme Holdings strengthens its leadership team',
                 'Acme Holdings today announced it has named Jane Doe Corporate Controller, '
                 'effective immediately.') == 'controller'
    # the title decides when it names a seat that is not being filled AND the
    # description names a real hire: the description's hire is still evidence
    assert _role('Acme Reports Q2 Results',
                 'Acme also announced the appointment of Jane Doe as CFO, effective Oct 1.') == 'cfo'


def test_evidence_is_a_short_snippet_of_the_original_text():
    got = finance_hire_subject('Acme Names Jane Doe Corporate Controller')
    assert got['evidence'] == 'Names Jane Doe Corporate Controller'
    long = 'Acme ' + 'x ' * 100 + 'names Jane Doe CFO'
    assert len(finance_hire_subject(long)['evidence']) <= 140


# ── Review 2026-09-08 (Phase 4), 3e: real hires that came back None ────────
# Each shape is a live-row pattern the detector stripped (#NewCFO lost, the
# cfo_hire relabel lost); the fix that admits it is named in the comment.
REVIEW_3E_HIRES = [
    # (i) adds/added were speech verbs
    'Acme Adds Jane Doe as Chief Financial Officer',
    # (ii) years / experience / decades … 'as <role>' blanked the new seat
    'Jane Doe, who brings 20 years of experience, joins Acme as CFO',
    'Acme names Jane Doe, a finance executive with two decades of experience, as Chief Financial Officer',
    # (iii) role-then-speech blanked a headline that goes on to quote someone
    'Acme Appoints Jane Doe CFO, Says Growth Ahead',
    "Acme Names Jane Doe CFO — 'We're thrilled,' says CEO",
    # (iv) former / longtime / veteran windows swallowed the destination seat
    'Former Tyson Foods executive named CFO at Hormel Foods',
    'Acme Names Longtime Executive Jane Doe CFO',
    'Acme Taps Industry Veteran Jane Doe as Chief Financial Officer',
    'Acme welcomes Jane Doe, a 20-year finance veteran, as CFO',
]


@pytest.mark.parametrize('title', REVIEW_3E_HIRES)
def test_review_3e_shapes_are_cfo_hires(title):
    got = finance_hire_subject(title)
    assert got['role'] == 'cfo' and got['kind'] == 'cfo', (title, got)
    assert got['evidence'] and got['evidence'] in ' '.join(title.split())


@pytest.mark.parametrize('title', [t for t in REVIEW_3E_HIRES
                                   if not t.startswith(('Former Tyson', 'Acme Names Longtime',
                                                        'Acme Taps Industry'))])
def test_review_3e_shapes_the_scraper_admits_agree(title):
    # the scraper admitted these six all along — the detector now agrees.
    # (The three former/longtime/veteran-on-the-person shapes are still
    # rejected by base.py's own _NOT_THE_SEAT_RES — a cross-file request in
    # the review report, not asserted here.)
    assert scraper_base.finance_leader_hire_kind(title) == 'cfo'


def test_review_3e_fixes_do_not_admit_the_sitting_officer():
    # (iii): the hire verb must precede the role in the SAME clause
    assert _role('Acme names Jane Doe COO; CFO John Smith said the move strengthens the team') is None
    assert _role('Acme names Jane Doe CEO', '"Jane is the right leader," CFO John Smith said.') is None
    # (iv): the former/outgoing window still blanks a seat it does not span a verb to reach
    assert _role('Acme appoints Jane Doe as interim CFO to replace departing CFO John Smith') is None
    assert _role('Former CFO Jane Doe joins Acme board') is None
    assert _role('Beta veteran Jane Doe named CFO of Acme') == 'cfo'      # the verb IS the hire
    # (ii): the person's own tenure in the seat is still a past seat
    assert _role('Acme names Jane Doe CEO', 'Doe brings 12 years as CFO of Beta to the role.') is None
    assert _role('Acme names Jane Doe CEO', 'Doe spent several years as controller at Beta.') is None
    # (i): 'adds' alone is still not a hire verb
    assert _role('Acme adds CFO') is None


# ── Vocabulary stays in step with the scraper ──────────────────────────────
def test_hire_verbs_match_the_scraper():
    assert hires.HIRE_VERBS == scraper_base.HIRE_VERBS


@pytest.mark.parametrize('title,kind', [
    ('Acme Names Jane Doe CFO', 'cfo'),
    ('Acme Names Jane Doe Corporate Controller', 'exec'),
    ('Acme promotes Jane Doe to VP Finance', 'exec'),
    ('Acme appoints Jane Doe Treasurer', 'exec'),
    ('Acme hires Jane Doe as Finance Director', 'exec'),
    ('Acme taps Jane Doe as Chief Accounting Officer', 'exec'),
    ('Acme Reports Q2 Results', None),
    ('Acme Announces Launch of Wireless Game Controller', None),
    ('Jane Doe, CFO of Acme, Appointed to Beta Board of Directors', None),
    ('Acme CFO Jane Doe Named CFO of the Year', None),
])
def test_kind_agrees_with_the_scrapers_hire_kind(title, kind):
    assert finance_hire_subject(title)['kind'] == kind
    assert scraper_base.finance_leader_hire_kind(title) == kind


def test_interim_is_the_one_deliberate_difference_from_the_scraper():
    # The scraper types an interim appointment as a hire EVENT (a seat did
    # change hands); the grader's #NewCFO needs a permanent seat, so the
    # subject detector says None. Pinned so the divergence stays intentional.
    title = 'Acme appoints Jane Doe as interim CFO'
    assert scraper_base.finance_leader_hire_kind(title) == 'cfo'
    assert finance_hire_subject(title)['role'] is None


def test_rubric_role_sets_are_disjoint_and_cover_the_rubric_seats():
    assert NEW_CFO_ROLES == {'cfo', 'vp_finance', 'finance_director'}
    assert NEW_CONTROLLER_ROLES == {'controller', 'cao'}
    assert not (NEW_CFO_ROLES & NEW_CONTROLLER_ROLES)
    assert set(FINANCE_ROLES) == NEW_CFO_ROLES | NEW_CONTROLLER_ROLES | {'treasurer'}
    assert role_label('cfo') == 'CFO' and role_label(None) == ''
