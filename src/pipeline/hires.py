"""Finance-leader hire SUBJECT detection (Phase 4 slice C2, 2026-09-08).

WHY THIS EXISTS. Until Phase 4 the grader's #NewCFO / #NewController
evidence guard was a substring test ("is 'cfo' anywhere in the title or
description?"). That is satisfied by an earnings release quoting the CFO
("… said Jane Doe, CFO"), by attribution ("according to the CFO"), by an
interim or outgoing officer, by a board seat ("Jane Doe, CFO of Acme,
appointed to Beta's board") and by an award ("named CFO of the Year") —
none of which is a company HIRING a finance leader, which is the single
highest-value NetSuite trigger the +5 / +3 points are for. The rule now:
the role must be the SUBJECT of a hire verb within ~80 characters
(appoints / names / hires / promotes / joins / taps / welcomes / elevates …)
or sit in a hire noun phrase ("new / incoming / as / to <role>",
"appointment of … <role>", "<role> transition", "to succeed the retiring
<role>"). Attribution, past / interim / acting / former / outgoing seats,
board seats, awards and device "controllers" never count.

FALSE STRIPS FIXED (review 2026-09-08, Phase 4, item 3e — real hires the
scraper admits that this detector returned None for): (i) "Acme ADDS Jane
Doe as CFO" was read as attribution because adds/added sat in the speech
verbs — they now count only right after a closing quote; (ii) "brings 20
years of experience, joins Acme as CFO" was blanked as a past seat — only
the "N years as <role>" shape is a past seat now, and no past-seat window
may span a hire verb; (iii) "Appoints Jane Doe CFO, Says Growth Ahead"
was blanked as role-then-speech — not when a hire verb precedes the role
in the same clause; (iv) "Former Tyson Foods executive NAMED CFO" and
"Names LONGTIME executive Jane Doe CFO" were blanked as former/longtime
seats — a former/outgoing window never spans a hire verb, and a career
adjective (longtime / veteran) on the PERSON rather than the seat yields
to a hire verb earlier in the clause. All nine shapes are pinned in
tests/test_hires.py.

VOCABULARY. The role regexes below DUPLICATE src/scrapers/base.py
(_CFO_ROLE / _SUB_CFO_ROLE / _CONTROLLER and the HIRE_VERBS tuple) on
purpose: this module must stay importable without the scraper package
(the scrapers run in GitHub Actions, enrichment on the Mac), so nothing is
imported from src.scrapers here. Keep the two in step — when a role or verb
is added on one side, add it on the other (base.py's comment block above
finance_leader_hire_kind points back here). Two deliberate one-way
extensions on this side: "VP / Vice President of Accounting" is a
Controller-track seat (the grader's #NewController vocabulary since
2026-07), and "Chief Financial and <X> Officer" is a CFO-equivalent title
that private companies use.

ROLE vs KIND vs HASHTAG. `role` is the seat named; `kind` mirrors
base.finance_leader_hire_kind ('cfo' for the CFO seat only, 'exec' for the
seats below it) so the two modules agree on the event TYPE. The HASHTAG
mapping is A.J.'s TAL V11 rubric, which is not the same split: VP Finance /
Head of Finance / Director of Finance are CFO-EQUIVALENTS (#NewCFO, +5),
Controller / VP Accounting / Chief Accounting Officer are #NewController
(+3), and a Treasurer earns neither (the rubric does not list the seat —
widen NEW_CONTROLLER_ROLES if A.J. decides otherwise). Those two sets are
exported here so the guard table and enrichment's _finance_role read ONE
definition.

Pure functions, no network, Python 3.9.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Hire verbs — identical to src/scrapers/base.py HIRE_VERBS ───────────────
# Whole words only ("disappointed" is not "appointed"); a bare 'hire' is
# deliberately absent because it is a substring of "New Hampshire";
# announces / announced / adds / transition / appointment alone are the
# vocabulary of every press release and never make a hire on their own.
HIRE_VERBS = (
    'appoints', 'appointed', 'names', 'named', 'hires', 'hired', 'promotes',
    'promoted', 'joins', 'joined', 'taps', 'tapped', 'welcomes', 'elevates',
    'elevated',
)
_HIRE_VERB = r'(?:' + '|'.join(HIRE_VERBS) + r')'

# ── Role vocabulary — mirrors base.py (see the module docstring) ───────────
_CFO = (r'cfo|chief\s+financ(?:ial|e)\s+officer|finance\s+chief|'
        r'chief\s+financ(?:ial|e)\s+(?:and|&)\s+(?:\w+\s+){1,2}officer')
# "controller" as a device, not a seat: never preceded by these words …
_DEVICE_BEFORE = ''.join(
    rf'(?<!{w}\s)' for w in (
        'game', 'motor', 'traffic', 'flight', 'remote', 'wireless', 'charge',
        'logic', 'domain', 'network', 'memory', 'storage', 'pest', 'speed',
        'lighting', 'drone', 'robot', 'pump', 'solar', 'battery', 'hvac',
        'temperature', 'irrigation', 'gaming',
    ))
# … nor followed by these.
_DEVICE_AFTER = (r'(?!\s+(?:chips?|boards?|units?|modules?|software|firmware|cards?|'
                 r'hubs?|apps?|line|lineup|series|market|products?|technology|'
                 r'systems?|devices?|suppliers?|vendors?)(?!\w))')
_VP = r'(?:[se]?vp|(?:senior\s+|executive\s+)?vice[\s-]president)'
_CONTROLLER = (r'(?:(?:corporate|financial|assistant|division|divisional|plant|group|regional)\s+)?'
               + _DEVICE_BEFORE + r'controller' + _DEVICE_AFTER
               + r'|comptroller|' + _VP + r'[\s,\-–—]+(?:of\s+)?accounting')
_VP_FINANCE = _VP + r'[\s,\-–—]+(?:of\s+)?finance|head\s+of\s+finance'
_FINANCE_DIRECTOR = r'finance\s+director|director\s+of\s+finance'
_CAO = r'chief\s+accounting\s+officer|chief\s+accountant'
_TREASURER = r'treasurer'

# Order = priority when one window names several seats: the CFO seat wins
# ("as President & Chief Financial Officer"), then the CFO-equivalents.
FINANCE_ROLES = ('cfo', 'vp_finance', 'finance_director', 'controller', 'cao', 'treasurer')
_ROLE_ALTS = {
    'cfo': _CFO, 'vp_finance': _VP_FINANCE, 'finance_director': _FINANCE_DIRECTOR,
    'controller': _CONTROLLER, 'cao': _CAO, 'treasurer': _TREASURER,
}
_ANY_ROLE = r'(?:' + '|'.join(_ROLE_ALTS.values()) + r')'
_ROLE_RE = re.compile(
    r'(?<!\w)(?:' + '|'.join(f'(?P<{k}>{v})' for k, v in _ROLE_ALTS.items()) + r')(?!\w)',
    re.IGNORECASE)

# The rubric's hashtag split (TAL V11, A.J. 2026-07-16) — see docstring.
NEW_CFO_ROLES = frozenset({'cfo', 'vp_finance', 'finance_director'})
NEW_CONTROLLER_ROLES = frozenset({'controller', 'cao'})

ROLE_LABELS = {
    'cfo': 'CFO', 'vp_finance': 'VP Finance', 'finance_director': 'Finance Director',
    'controller': 'Controller', 'cao': 'Chief Accounting Officer', 'treasurer': 'Treasurer',
}

# Words allowed between "as"/"to" and the seat ("as the company's new CFO").
_FILLER = (r"(?:(?:the|its|our|a|an|new|permanent|first|next|senior|executive|global|"
           r"group|corporate|company'?s|firm'?s|organization'?s|incoming)\s+){0,3}")
# A window boundary: sentence end, clause separators, headline pipes.
_NB = r'[^.;:|\n]'

# ── Mentions that are NOT the seat being filled (blanked before the scan) ──
# 1. Attribution — the person quoted / cited already holds the seat.
#    'added' / 'adds' are NOT in this list (review 2026-09-08 (Phase 4),
#    3e(i)): "Acme Adds Jane Doe as Chief Financial Officer" is a hire the
#    scraper admits, and it was read as attribution. The attribution use
#    ('"We delivered," added Jane Doe, CFO') always follows a closing quote
#    — that shape is kept, in _QUOTED_ADDED_RE.
_SPEECH = (r'(?:said|says|stated|states|noted|notes|commented|comments|'
           r'explained|explains|remarked|told|tells|according\s+to|quoted|wrote|writes|'
           r'discuss(?:es|ed)?|spoke|speaks|shared|shares|cited|cites)')
_HIRE_VERB_RE = re.compile(r'(?<!\w)' + _HIRE_VERB + r'(?!\w)', re.IGNORECASE)
_QUOTED_ADDED_RE = re.compile(
    r'["“”]\s*,?\s*(?:added|adds)\s+' + _NB + r'{0,70}?' + _ANY_ROLE + r'(?!\w)', re.IGNORECASE)
_ATTRIBUTION_RES = (
    re.compile(r'(?<!\w)' + _SPEECH + r'\s+' + _NB + r'{0,70}?' + _ANY_ROLE + r'(?!\w)',
               re.IGNORECASE),
    _QUOTED_ADDED_RE,
)
# 1b. Role, then a speech verb ("CFO John Smith said") — applied by
#     _blank_unless_hired: skipped when a hire verb precedes the role in the
#     same clause (review 2026-09-08 (Phase 4), 3e(iii): "Acme Appoints Jane
#     Doe CFO, Says Growth Ahead" / "Names Jane Doe CFO — '…,' says CEO" are
#     hires whose headline goes on to quote someone).
_ROLE_THEN_SPEECH_RE = re.compile(
    r'(?<!\w)' + _ANY_ROLE + r'(?!\w)' + _NB + r'{0,60}?(?<!\w)' + _SPEECH + r'(?!\w)',
    re.IGNORECASE)
# 2. The boss the hire reports to ("… will report to Chief Financial
#    Officer John Smith") — the Controller-hire double-count base.
_REPORTS_TO_RE = re.compile(
    r'(?<!\w)report(?:s|ing|ed)?\s+(?:directly\s+)?(?:in\s+)?to\s+(?:the\s+)?'
    r"(?:company'?s\s+|firm'?s\s+)?(?:[^\s;:]+\s+){0,2}?" + _ANY_ROLE + r'(?!\w)',
    re.IGNORECASE)
# 3. Awards / lists — the seat the person already holds.
_AWARD_RES = (
    re.compile(r'(?<!\w)' + _ANY_ROLE + r'\s+of\s+the\s+(?:year|decade|month)(?!\w)', re.IGNORECASE),
    re.compile(r'(?<!\w)(?:top|best|leading|outstanding|rising|award-winning|most\s+admired|'
               r'\d+\s+most\s+\w+)\s+(?:\d+\s+)?' + _ANY_ROLE + r's?(?!\w)', re.IGNORECASE),
    re.compile(r'(?<!\w)' + _ANY_ROLE + r'\s+(?:awards?|honou?rs?)(?!\w)', re.IGNORECASE),
)
# 4. Past, interim, acting, former, outgoing seats — and the person's own
#    career history ("previously served as CFO of Beta").
#    A word that is not a hire verb (the windows below may not SPAN one —
#    review 2026-09-08 (Phase 4), 3e(iv): "Former Tyson Foods executive
#    NAMED CFO at Hormel Foods" is a hire into the CFO seat, and the
#    former/… window used to swallow 'named CFO').
_NOT_VERB_WORD = r'(?!' + _HIRE_VERB + r'(?!\w))[^\s,;:.]+\s+'
_NOT_VERB_CHAR = r'(?:(?!(?<!\w)' + _HIRE_VERB + r'(?!\w))' + _NB + r')'
_PAST_SEAT_RE = re.compile(
    r'(?<!\w)(?:interim|acting|former|ex|previous|previously|past|retired|retiring|'
    r'outgoing|departing|then|late|erstwhile|one-time|onetime)[\s-]+'
    r'(?:' + _NOT_VERB_WORD + r'){0,4}?' + _ANY_ROLE + r'(?!\w)', re.IGNORECASE)
# A career adjective directly on the seat ("longtime CFO John Smith retires")
# is the seat's own tenure — always the sitting officer …
_CAREER_SEAT_RE = re.compile(
    r'(?<!\w)(?:longtime|long-time|veteran)\s+' + _FILLER + _ANY_ROLE + r'(?!\w)', re.IGNORECASE)
# … but on the PERSON ("Names Longtime Executive Jane Doe CFO", "Taps
# Industry Veteran Jane Doe as CFO") it yields to a hire verb earlier in
# the clause (_blank_unless_hired).
_CAREER_PERSON_RE = re.compile(
    r'(?<!\w)(?:longtime|long-time|veteran)[\s-]+(?:' + _NOT_VERB_WORD + r'){1,4}?'
    + _ANY_ROLE + r'(?!\w)', re.IGNORECASE)
# "served / spent / previously … as <role>" — the window may not span a hire
# verb ("whose career spans banking, JOINS Acme as CFO" is the new seat).
_PAST_AS_RE = re.compile(
    r'(?<!\w)(?:served|serves|serving|worked|working|spent|previously|formerly|'
    r'most\s+recently|prior|before|earlier|tenure|stint|career|role|time|'
    r'background|history)(?!\w)' + _NOT_VERB_CHAR + r'{0,60}?(?<!\w)as\s+'
    + _FILLER + _ANY_ROLE + r'(?!\w)', re.IGNORECASE)
# "N years / two decades (of experience) as <role>" — the ONLY years/
# experience shape that is a past seat (review 2026-09-08 (Phase 4),
# 3e(ii): 'years' / 'experience' / 'decades' anywhere in the 60 chars before
# "as CFO" used to blank "brings 20 years of experience, joins Acme as CFO"
# and "a finance executive with two decades of experience, as CFO").
_YEARS_AS_RE = re.compile(
    r'(?<!\w)(?:\d[\d,]*\+?|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
    r'fifteen|twenty|thirty|several|many|numerous)[\s-]+(?:years?|decades?)'
    r"(?:['’]?\s*(?:of\s+)?experience)?\s+as\s+" + _FILLER + _ANY_ROLE + r'(?!\w)',
    re.IGNORECASE)
_PAST_RES = (_PAST_SEAT_RE, _CAREER_SEAT_RE, _PAST_AS_RE, _YEARS_AS_RE)

# ── Hire shapes ─────────────────────────────────────────────────────────────
# a. Somebody hired to SUCCEED / REPLACE the sitting officer — the seat is
#    the one being vacated ("names Jane Doe to succeed retiring CFO John").
_SUCCESSOR_RE = re.compile(
    r'(?<!\w)(?:succeed(?:s|ed|ing)?|replac(?:e|es|ed|ing)|tak(?:e|es|ing)\s+over\s+(?:from|for)|'
    r'assum(?:e|es|ing)\s+the\s+(?:role|position|duties|responsibilities)\s+of|'
    r'fill(?:s|ing)?\s+the\s+(?:role|position|seat|vacancy)\s+(?:of|left\s+by))(?!\w)'
    + _NB + r'{0,60}?(?<!\w)' + _ANY_ROLE + r'(?!\w)', re.IGNORECASE)
# b. Hire noun phrases (base.py _HIRE_PHRASE_RE): stand on their own.
_NOUN_PHRASE_RE = re.compile(
    r'(?<!\w)(?:'
    r'(?:new|incoming|newly[\s-]+(?:appointed|named|hired|promoted))\s+' + _FILLER + _ANY_ROLE + r'|'
    r'as\s+' + _FILLER + _ANY_ROLE + r'|'
    r'to\s+' + _FILLER + r'(?:(?:the\s+)?(?:role|position|post|seat|title)\s+of\s+)?' + _ANY_ROLE + r'|'
    + _ANY_ROLE + r'\s+(?:leadership\s+)?transition|'
    r'appointment\s+of\s+(?:[^\s;:]+\s+){0,6}?(?:as\s+)?' + _FILLER + _ANY_ROLE
    + r')(?!\w)', re.IGNORECASE)
# c. Verb, then the seat within a comma-free run of at most eight words
#    ("names Jane Doe Corporate Controller"; "Appoints Jane Doe, CFO of
#    Beta, to its board" does NOT qualify — the comma-free rule is what
#    keeps board seats out, exactly as in base.py).
_VERB_THEN_ROLE_RE = re.compile(
    r'(?<!\w)' + _HIRE_VERB + r'\s+(?:[^\s,;:.|]+\s+){0,8}?' + _FILLER + _ANY_ROLE + r'(?!\w)',
    re.IGNORECASE)
# d. Verb … as/to <seat> within ~80 chars, commas allowed ("appoints Jane
#    Doe, CPA, as Chief Financial Officer").
_VERB_AS_ROLE_RE = re.compile(
    r'(?<!\w)' + _HIRE_VERB + r'(?!\w)' + _NB + r'{0,80}?(?<!\w)(?:as|to)\s+' + _FILLER
    + r'(?:(?:the\s+)?(?:role|position|post|seat|title)\s+of\s+)?' + _ANY_ROLE + r'(?!\w)',
    re.IGNORECASE)

# What a verb-then-seat match must NOT be followed by: the seat quoted on the
# way to a board chair or an award ("welcomes CFO Jane Doe to its board",
# "names CFO Jane Doe to its 40 Under 40 list"). "CFO and Board Member" is
# still a hire (the seat is joined by 'and', not reached by 'to').
_AFTER_NOT_A_SEAT_RE = re.compile(
    r'(?<!\w)(?:(?:to|on|onto|join(?:s|ed|ing)?)\s+(?:the\s+|its\s+|their\s+|our\s+|[\w\'&.-]+\s+){0,3}?'
    r'board(?!\w)|as\s+(?:an?\s+)?(?:new\s+|independent\s+|non-executive\s+)?'
    r'(?:board\s+member|director(?!\s+of\s+finance)|trustee|member\s+of\s+(?:the|its)\s+board)|'
    r'board\s+seat|elected\s+(?:to|as)|to\s+(?:its|the|this\s+year\'?s|an?)\s+' + _NB + r'{0,30}?'
    r'(?:list|ranking|class|cohort)|awards?|honou?r(?:s|ee)?|\d+\s+under\s+\d+|top\s+\d+|'
    r'power\s+\d+|hall\s+of\s+fame|of\s+the\s+(?:year|decade))(?!\w)', re.IGNORECASE)
# "Launches New Controller for Smart Homes" — a product, not a seat.
_PRODUCT_LAUNCH_RE = re.compile(
    r'(?<!\w)(?:launch(?:es|ed)?|unveil(?:s|ed)?|introduc(?:es|ed)|debuts?|releases?|'
    r'ships?|showcas(?:es|ed)|rolls?\s+out)(?!\w)', re.IGNORECASE)
_INTERIM_RE = re.compile(r'(?<!\w)(?:interim|acting)(?!\w)', re.IGNORECASE)


def _blank(text: str, rx) -> str:
    """Replace every match with spaces of equal length so offsets survive
    (evidence is cut from the original text)."""
    return rx.sub(lambda m: ' ' * (m.end() - m.start()), text)


def _blank_unless_hired(text: str, rx) -> str:
    """_blank, except that a match is left alone when a hire verb precedes
    it in the same clause (back to the last . ; : | or newline): the clause
    is announcing a hire, so the role in it is the seat being filled —
    "Acme Appoints Jane Doe CFO, Says …", "Names Longtime Executive Jane
    Doe CFO" (review 2026-09-08 (Phase 4), 3e(iii)/(iv))."""
    def repl(m):
        start = max((text.rfind(ch, 0, m.start()) for ch in '.;:|\n'), default=-1) + 1
        if _HIRE_VERB_RE.search(text[start:m.start()]):
            return m.group(0)
        return ' ' * (m.end() - m.start())
    return rx.sub(repl, text)


def _role_in(m) -> Optional[str]:
    """The seat a match names — by FINANCE_ROLES priority when a window
    names several."""
    found = {r for r in FINANCE_ROLES if _ROLE_RE_GROUP[r].search(m.group(0))}
    for r in FINANCE_ROLES:
        if r in found:
            return r
    return None


_ROLE_RE_GROUP = {k: re.compile(r'(?<!\w)(?:' + v + r')(?!\w)', re.IGNORECASE)
                  for k, v in _ROLE_ALTS.items()}


def _sentence(text: str, pos: int) -> str:
    start = max((text.rfind(ch, 0, pos) for ch in '.;:|\n'), default=-1) + 1
    ends = [i for i in (text.find(ch, pos) for ch in '.;:|\n') if i != -1]
    end = min(ends) if ends else len(text)
    return text[start:end]


def _evidence(text: str, m) -> str:
    return re.sub(r'\s+', ' ', text[m.start():m.end()]).strip()[:140]


def _scan(raw: str):
    """(role, evidence) for one text, or None."""
    text = re.sub(r'\s+', ' ', raw or '').strip()
    if not text or not _ROLE_RE.search(text):
        return None
    # Attribution, the boss, awards: never the seat being filled.
    t = text
    for rx in _ATTRIBUTION_RES + (_REPORTS_TO_RE,) + _AWARD_RES:
        t = _blank(t, rx)
    t = _blank_unless_hired(t, _ROLE_THEN_SPEECH_RE)
    # a. "… to succeed / replace the (retiring|outgoing|interim) <seat>" —
    #    read BEFORE the past/interim blanking removes the vacated seat.
    #    An interim appointment ("names interim CFO to replace …") is not.
    for m in _SUCCESSOR_RE.finditer(t):
        if _INTERIM_RE.search(_sentence(t, m.start())):
            continue
        role = _role_in(m)
        if role:
            return role, _evidence(text, m)
    for rx in _PAST_RES:
        t = _blank(t, rx)
    t = _blank_unless_hired(t, _CAREER_PERSON_RE)
    if not _ROLE_RE.search(t):
        return None
    # b. Hire noun phrases stand on their own (a board mention elsewhere in
    #    the sentence does not cancel "as CFO").
    for m in _NOUN_PHRASE_RE.finditer(t):
        head = m.group(0).lower()
        if head.startswith(('new', 'incoming', 'newly')):
            before = _sentence(t, m.start())[:m.start() - max(0, t.rfind('.', 0, m.start()) + 1)]
            if _PRODUCT_LAUNCH_RE.search(before):
                continue
        role = _role_in(m)
        if role:
            return role, _evidence(text, m)
    # c/d. A hire verb, then the seat.
    best = None
    for rx in (_VERB_AS_ROLE_RE, _VERB_THEN_ROLE_RE):
        for m in rx.finditer(t):
            role = _role_in(m)
            if not role:
                continue
            if rx is _VERB_THEN_ROLE_RE:
                # The 80 chars after the seat, up to the sentence end: a
                # board chair or an award there means the seat was only
                # quoted on the way ("welcomes CFO Jane Doe to its board").
                after = re.split(r'[.;:|\n]', t[m.end():m.end() + 80])[0]
                if _AFTER_NOT_A_SEAT_RE.search(after):
                    continue
            if best is None or m.start() < best[1].start():
                best = (role, m)
    if best:
        return best[0], _evidence(text, best[1])
    return None


def finance_hire_subject(title: str, description: str = '') -> dict:
    """{'role': cfo|controller|vp_finance|treasurer|finance_director|cao|None,
        'kind': 'cfo'|'exec'|None, 'evidence': str}

    The seat that is the SUBJECT of a hire in the title, else in the
    description (the title decides when both name one). `kind` mirrors
    base.finance_leader_hire_kind; the hashtag split is NEW_CFO_ROLES /
    NEW_CONTROLLER_ROLES.

        'Acme Names Jane Doe CFO'                          → cfo / cfo
        'Acme promotes Jane Doe to Corporate Controller'   → controller / exec
        'Acme Reports Q2 Results' + '… said Jane Doe, CFO' → None
        'Acme appoints Jane Doe as interim CFO'            → None
        'Jane Doe, CFO of Acme, appointed to Beta board'   → None
        'Acme CFO Jane Doe named CFO of the Year'          → None
    """
    for text in (title or '', description or ''):
        hit = _scan(text)
        if hit:
            role, evidence = hit
            return {'role': role, 'kind': 'cfo' if role == 'cfo' else 'exec',
                    'evidence': evidence}
    return {'role': None, 'kind': None, 'evidence': ''}


def role_label(role: Optional[str]) -> str:
    return ROLE_LABELS.get(role or '', '')
