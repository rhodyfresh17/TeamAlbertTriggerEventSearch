"""Base scraper class with common functionality."""

import hashlib
import re
import unicodedata
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Dict, Any

import requests

from ..models import TriggerEvent, EventType, EventSource
from ..pipeline.gates import STATE_NAMES as _GATES_STATE_NAMES

# State abbreviation mapping for dateline parsing (postal codes + AP-style
# wire abbreviations: "Va.", "N.C.", "Mass.", "Fla.", "Calif.", "W.Va.").
# Keys are lower-case with dots/spaces stripped; values are the lower-case
# full name, which is what territory.regions is compared against.
STATE_ABBREVS = {
    'al': 'alabama', 'ak': 'alaska', 'az': 'arizona', 'ar': 'arkansas',
    'ca': 'california', 'co': 'colorado', 'ct': 'connecticut', 'de': 'delaware',
    'fl': 'florida', 'ga': 'georgia', 'hi': 'hawaii', 'id': 'idaho',
    'il': 'illinois', 'in': 'indiana', 'ia': 'iowa', 'ks': 'kansas',
    'ky': 'kentucky', 'la': 'louisiana', 'me': 'maine', 'md': 'maryland',
    'ma': 'massachusetts', 'mass': 'massachusetts', 'mi': 'michigan',
    'mn': 'minnesota', 'ms': 'mississippi', 'mo': 'missouri', 'mt': 'montana',
    'ne': 'nebraska', 'nv': 'nevada', 'nh': 'new hampshire', 'nj': 'new jersey',
    'nm': 'new mexico', 'ny': 'new york', 'nc': 'north carolina',
    'nd': 'north dakota', 'oh': 'ohio', 'ok': 'oklahoma', 'or': 'oregon',
    'pa': 'pennsylvania', 'ri': 'rhode island', 'sc': 'south carolina',
    'sd': 'south dakota', 'tn': 'tennessee', 'tx': 'texas', 'ut': 'utah',
    'vt': 'vermont', 'va': 'virginia', 'wa': 'washington', 'wv': 'west virginia',
    'wi': 'wisconsin', 'wy': 'wyoming', 'dc': 'washington dc',
    # Canadian provinces
    'on': 'ontario', 'ont': 'ontario', 'qc': 'quebec', 'que': 'quebec',
    'bc': 'british columbia', 'ab': 'alberta', 'mb': 'manitoba',
    'sk': 'saskatchewan', 'ns': 'nova scotia', 'nb': 'new brunswick',
    'nl': 'newfoundland', 'pe': 'prince edward island',
    # AP / PR-wire style state abbreviations (only ever consulted in the
    # "CITY, State, Month DD" slot, so short keys like 'ind'/'del' are safe)
    'ala': 'alabama', 'ariz': 'arizona', 'ark': 'arkansas', 'calif': 'california',
    'colo': 'colorado', 'conn': 'connecticut', 'del': 'delaware', 'fla': 'florida',
    'ill': 'illinois', 'ind': 'indiana', 'kan': 'kansas', 'kans': 'kansas',
    'mich': 'michigan', 'minn': 'minnesota', 'miss': 'mississippi',
    'mont': 'montana', 'neb': 'nebraska', 'nebr': 'nebraska', 'nev': 'nevada',
    'okla': 'oklahoma', 'ore': 'oregon', 'oreg': 'oregon', 'penn': 'pennsylvania',
    'tenn': 'tennessee', 'tex': 'texas', 'wash': 'washington',
    'wva': 'west virginia', 'wis': 'wisconsin', 'wisc': 'wisconsin',
    'wyo': 'wyoming', 'alta': 'alberta', 'sask': 'saskatchewan',
    'nfld': 'newfoundland',
}

# Canonical lower-case full name per state/province code, derived from
# gates.STATE_NAMES (first name listed for a code wins: 'quebec' over
# 'québec', 'district of columbia' over 'washington dc').
_CODE_TO_NAME: Dict[str, str] = {}
for _name, _code in _GATES_STATE_NAMES.items():
    _CODE_TO_NAME.setdefault(_code, _name)

# Dateline parsing pieces. A wire dateline is "<LOCATION(S)>, <Month> <DD>,
# <YYYY> /PRNewswire/ --": the date is the anchor, the location(s) sit in
# the ~120 chars before it. Month must be word-boundary-preceded and
# followed by a day number, so "Decrypt", "Augusta", "Marketing" and bare
# "March 2026" are not anchors.
_MONTH_ANCHOR = re.compile(
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?'
    r'\s+\d{1,2}(?:st|nd|rd|th)?\b'
)
_LOC_SPLIT = re.compile(r'\s+and\s+|\s+AND\s+|\s*&\s*|\s*/\s*')
# ALL-CAPS city run at the end of a segment: "NEW YORK", "ST. LOUIS",
# "WINSTON-SALEM", "MONTRÉAL". Tokens need ≥2 chars so a stray "A" is not a city.
_CAPS_TOKEN = r"[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ\.'’\-]+"
_CAPS_RUN_END = re.compile(rf"({_CAPS_TOKEN}(?:\s+{_CAPS_TOKEN}){{0,4}})\s*$")
# Capitalized run (Title Case OR caps) — only trusted when a validated state
# follows it ("Arlington, Virginia, Feb. 10" — GlobeNewswire style).
_TITLE_TOKEN = r"[A-ZÀ-ÖØ-Þ][\w\.'’\-]*"
_TITLE_RUN_END = re.compile(rf"({_TITLE_TOKEN}(?:\s+{_TITLE_TOKEN}){{0,4}})\s*$")


def resolve_state_token(token: Optional[str]) -> Optional[str]:
    """Map a dateline state token to its lower-case full name, or None when
    it is not a state/province.

        'Va.' → 'virginia' · 'N.C.' → 'north carolina' · 'Mass.' →
        'massachusetts' · 'Maine' → 'maine' · 'Ontario' → 'ontario' ·
        'D.C.' → 'washington dc' · 'Québec' → 'quebec'
    """
    t = re.sub(r'\s+', ' ', (token or '').strip().lower())
    if not t:
        return None
    key = t.replace('.', '').replace(' ', '')       # 'n.c.' → 'nc', 'w. va.' → 'wva'
    if key in STATE_ABBREVS:
        return STATE_ABBREVS[key]
    name = t.replace('.', '').strip()
    code = _GATES_STATE_NAMES.get(name)
    if code:
        return _CODE_TO_NAME.get(code, name)
    return None


# Trailing corporate suffixes of an exclusion term accept their long form:
# whole-word matching (research 2026-09-08) had silently lost "Barrick Gold
# Corporation" for "gold corp", "Acme Resources Incorporated" for
# "resources inc", "Acme Mining Limited" for "mining ltd" (review 2026-09-08).
_CORPORATE_SUFFIXES = {
    'corp': r'corp(?:oration)?',
    'inc': r'inc(?:orporated)?',
    'ltd': r'(?:ltd|limited)',
    'co': r'co(?:mpany)?',
}


def _compile_whole_word(terms: List[str], plural: bool = False) -> List[tuple]:
    """[(term, compiled_regex)] — whole-word, case-insensitive. Lookarounds
    instead of \b so terms ending in punctuation ("St. Louis") still work.

    The words of a multi-word term may be separated by any whitespace or by
    NONE: the bank spells itself "JPMorganChase" in its own headlines, which
    the "JPMorgan Chase" entry missed while whole-word "JPMorgan" was blocked
    by the trailing "C" (review 2026-09-08). Concatenated forms of city and
    industry terms ("datacenter") are the same thing, never a new word.

    plural=True also accepts the plain English plural of the last word
    ("hotel" → hotels, "data center" → data centers, "refinery" →
    refineries) so an exclusion list written in the singular keeps its
    recall — but never a different word: "mining" no longer fires inside
    "determining"/"examining", "apple" inside "pineapple" (research
    2026-09-08). A trailing corporate suffix (_CORPORATE_SUFFIXES) is
    expanded to its long form instead of pluralised."""
    out = []
    for term in terms:
        term = (term or '').strip()
        if not term:
            continue
        words = term.split()
        last = words[-1]
        if plural and last.lower() in _CORPORATE_SUFFIXES:
            tail = _CORPORATE_SUFFIXES[last.lower()]
        else:
            tail = re.escape(last)
            if plural:
                if last[-1].lower() == 'y' and last[-2:-1].lower() not in 'aeiou':
                    tail = tail[:-1] + r'(?:y|ies)'
                else:
                    tail += r'(?:e?s)?'
        pattern = r'\s*'.join([re.escape(w) for w in words[:-1]] + [tail])
        out.append((term, re.compile(r'(?<!\w)' + pattern + r'(?!\w)',
                                     re.IGNORECASE)))
    return out


# ── Hire typing — decided from the TITLE (review 2026-09-08) ─────────────
#
# Phase 3 typed "any role mention + any of 24 indicator words" over the whole
# text, so an earnings release ("… today announced … said Jane Doe, CFO")
# became a cfo_hire and was admitted with unknown territory, a product launch
# ("Announces Launch of Wireless Game Controller") an executive_hire, and
# "Names Jane Doe Corporate Controller … will report to Chief Financial
# Officer John Smith" a cfo_hire — the exact #NewCFO / #NewController
# double-count the grader is built to prevent. The rules now:
#
#   * a finance-leader hire needs a finance ROLE in the title AND either a
#     STRONG hire verb in the title (HIRE_VERBS) or a hire NOUN PHRASE next
#     to the role ("new CFO", "incoming CFO", "as CFO", "to CFO", "CFO
#     transition", "appointment of … CFO", "names Jane Doe CFO");
#   * announced / announces / adds / transition / appointment alone never
#     make a hire — they are the vocabulary of every release;
#   * the role in the title decides the type: CFO / chief financial officer
#     → CFO_HIRE; controller, VP finance, treasurer, finance director, head
#     of finance, chief accounting officer → EXECUTIVE_HIRE even when the
#     body mentions the CFO (#NewController +3, never #NewCFO +5);
#   * the bare word "controller" is a finance role only after a finance
#     qualifier (corporate / financial / assistant / division / plant /
#     group / regional) or inside a hire-verb pattern ("names X controller",
#     "as controller") — never a game / motor / traffic controller;
#   * the body is consulted only when the title names no role at all, and
#     then only its head (BODY_HEAD_CHARS) and only in the verb-then-role
#     shape ("has named Jane Doe Controller", "joined Acme as CFO").
#
# Whole words throughout ("disappointed" is not "appointed"); a bare 'hire'
# is deliberately absent because it is a substring of "New Hampshire".
HIRE_VERBS = (
    'appoints', 'appointed', 'names', 'named', 'hires', 'hired', 'promotes',
    'promoted', 'joins', 'joined', 'taps', 'tapped', 'welcomes', 'elevates',
    'elevated',
)
_HIRE_VERB = r'(?:' + '|'.join(HIRE_VERBS) + r')'
_HIRE_VERB_RE = re.compile(r'(?<!\w)' + _HIRE_VERB + r'(?!\w)', re.IGNORECASE)

# Wire bodies open with a dateline and a boilerplate clause before the news
# ("ARLINGTON, Va., Feb. 10, 2026 /PRNewswire/ -- Acme, a leading provider
# of widgets, today announced the appointment of …"), so the head window
# is a little over the ~200 chars the review asked for.
BODY_HEAD_CHARS = 250

_CFO_ROLE = r'(?:cfo|chief\s+financ(?:ial|e)\s+officer|finance\s+chief)'
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
                 r'systems?|devices?)(?!\w))')
_CONTROLLER = _DEVICE_BEFORE + r'controller' + _DEVICE_AFTER
_SUB_CFO_ROLE = (
    r'(?:corporate|financial|assistant|division|divisional|plant|group|regional)'
    r'\s+controller|comptroller|treasurer|'
    r'(?:[se]?vp|(?:senior |executive )?vice[ -]president)[\s,\-–—]+(?:of\s+)?finance|'
    r'finance\s+director|director\s+of\s+finance|head\s+of\s+finance|'
    r'chief\s+accounting\s+officer'
)
_ANY_FINANCE_ROLE = r'(?:' + _CFO_ROLE + r'|' + _SUB_CFO_ROLE + r'|' + _CONTROLLER + r')'
# Words allowed between "as"/"to" and the role: "as the company's new CFO".
_FILLER = (r"(?:(?:the|its|our|a|an|new|interim|acting|incoming|permanent|first|"
           r"next|senior|executive|global|group|corporate|company'?s|firm'?s)\s+){0,3}")

CFO_ROLE = re.compile(r'(?<!\w)' + _CFO_ROLE + r'(?!\w)', re.IGNORECASE)
# Finance-leader roles BELOW the CFO seat (a hire into one is EXECUTIVE_HIRE,
# never CFO_HIRE). Bare "controller" is deliberately absent — see
# _CONTROLLER_HIRED_RE.
FINANCE_LEADER_TITLE = re.compile(r'(?<!\w)(?:' + _SUB_CFO_ROLE + r')(?!\w)', re.IGNORECASE)
# Bare "controller" inside a hire pattern: a hire verb, "as" or "to", then at
# most four words, then the seat ("names Jane Doe Controller", "promoted to
# controller", "joins as controller").
_CONTROLLER_HIRED_RE = re.compile(
    r'(?<!\w)(?:' + _HIRE_VERB + r'|as|to)\s+(?:[^\s;:]+\s+){0,4}?' + _CONTROLLER + r'(?!\w)',
    re.IGNORECASE)
# The hire noun phrases, plus the verb-then-role shape without a comma in
# between ("Appoints Jane Doe, CFO of Beta, to its board" does NOT qualify —
# the comma-free rule is what keeps board seats out).
_HIRE_PHRASE_RE = re.compile(
    r'(?<!\w)(?:'
    r'(?:new|incoming)\s+' + _ANY_FINANCE_ROLE + r'|'
    r'as\s+' + _FILLER + _ANY_FINANCE_ROLE + r'|'
    r'to\s+' + _FILLER + r'(?:(?:the\s+)?(?:role|position|post|seat|title)\s+of\s+)?'
    + _ANY_FINANCE_ROLE + r'|'
    + _ANY_FINANCE_ROLE + r'\s+(?:leadership\s+)?transition|'
    r'appointment\s+of\s+(?:[^\s;:]+\s+){0,6}?(?:as\s+)?' + _FILLER + _ANY_FINANCE_ROLE + r'|'
    + _HIRE_VERB + r'\s+(?:[^\s,;:]+\s+){0,5}?(?:(?:as|to)\s+)?' + _FILLER + _ANY_FINANCE_ROLE
    + r')(?!\w)',
    re.IGNORECASE)
# A title about a board seat, an award or a speaking slot quotes the role the
# person ALREADY holds ("Jane Doe, CFO of Acme, Appointed to Beta Board",
# "Acme CFO Named CFO of the Year"): a strong verb alone is not a hire there,
# a hire noun phrase still is ("Appoints Jane Doe as CFO and Board Member").
_NOT_A_SEAT_RE = re.compile(
    r'(?<!\w)(?:board\s+of\s+(?:directors|trustees|advisors|advisers|governors|managers)|'
    r'board\s+members?|advisory\s+board|(?:to|joins?|joined)\s+(?:the\s+|its\s+|their\s+)?board|'
    r'of\s+the\s+(?:year|decade)|top\s+\d+|\d+\s+under\s+\d+|power\s+\d+|hall\s+of\s+fame|'
    r'honou?r(?:s|ed|ee|ees)?|awards?|awarded|panel(?:ist)?s?|keynote|webinar|podcast)(?!\w)',
    re.IGNORECASE)
# Role mentions that are NOT the seat being filled, blanked before the scan:
# the person's past seat ("Former Fortune 50 Chief Accounting Officer Patti
# Humble Joins …"), the boss ("… Corporate Controller. Doe will report to
# Chief Financial Officer John Smith") and an award ("Named CFO of the Year").
_NOT_THE_SEAT_RES = (
    re.compile(r'(?<!\w)report(?:s|ing|ed)?\s+(?:directly\s+)?to\s+(?:the\s+)?'
               r"(?:company'?s\s+)?(?:[^\s;:]+\s+){0,2}?" + _ANY_FINANCE_ROLE + r'(?!\w)',
               re.IGNORECASE),
    re.compile(r'(?<!\w)(?:former|ex|previous|past|retired|outgoing|veteran|longtime|'
               r'long-time|then)[\s\-]+(?:[^\s,;:]+\s+){0,4}?' + _ANY_FINANCE_ROLE + r'(?!\w)',
               re.IGNORECASE),
    re.compile(r'(?<!\w)(?:top|best|leading|outstanding|rising|award-winning)\s+(?:\d+\s+)?'
               + _ANY_FINANCE_ROLE + r's?(?!\w)|(?<!\w)' + _ANY_FINANCE_ROLE
               + r'\s+of\s+the\s+(?:year|decade)(?!\w)', re.IGNORECASE),
    # A SUPPORT role "to" the seat is not the seat (review 2026-09-08 (Phase
    # 4)): "Names Jane Doe Executive Assistant to the CFO", "EA to CFO",
    # "Assistant to the CFO Internship", "HR Coordinator and Admin Assistant
    # to the CFO", "Senior Advisor to the CFO" all typed 'cfo' because the
    # 'to <role>' clause of _HIRE_PHRASE_RE fired before anything blanked the
    # phrase — seven Adzuna EA postings were pinned in the golden set as CFO
    # hires. Blanked from the support noun through the role only, so
    # "Promotes Jane Doe from Assistant to the CFO to Chief Financial Officer"
    # still types on the seat that remains.
    re.compile(r'(?<!\w)(?:assistants?|ea|pa|coordinators?|interns?|internships?|'
               r'secretar(?:y|ies)|chief\s+of\s+staff|aides?|advis[eo]rs?|deputy|deputies|'
               r'liaisons?|support)\s+(?:reporting\s+)?to\s+(?:the\s+)?(?:office\s+of\s+the\s+)?'
               + _FILLER + _ANY_FINANCE_ROLE + r'(?!\w)', re.IGNORECASE),
)
# Device controllers blanked before the CONFIGURED keyword scan of the generic
# executive-hire path, which substring-matches "Controller".
_DEVICE_CONTROLLER_RE = re.compile(
    r'(?<!\w)(?:game|gaming|motor|traffic|flight|remote|wireless|charge|logic|domain|'
    r'network|memory|storage|pest|speed|lighting|drone|robot|pump|solar|battery|hvac|'
    r'temperature|irrigation)\s+controllers?(?!\w)|'
    r'(?<!\w)controllers?\s+(?:chips?|boards?|units?|modules?|software|firmware|cards?|'
    r'hubs?|apps?|line|lineup|series|market|products?|technology|systems?|devices?)(?!\w)',
    re.IGNORECASE)
# "New controller" as a product, not a seat ("Launches New Controller for
# Smart Homes") — guards the generic path's "new <role>" indicator only.
_PRODUCT_LAUNCH_RE = re.compile(
    r'(?<!\w)(?:launch(?:es|ed)?|unveil(?:s|ed)?|introduc(?:es|ed)|debuts?|releases?|'
    r'ships?|showcas(?:es|ed)|rolls?\s+out)(?!\w)', re.IGNORECASE)


def has_hire_indicator(text: str) -> bool:
    """True when the text carries a STRONG hire verb (HIRE_VERBS) as a whole
    word. announced / announces / adds / transition / appointment are not
    indicators — an earnings release "today announced" too."""
    if not text:
        return False
    return bool(_HIRE_VERB_RE.search(text))


def _hire_window(text: str) -> str:
    """The text with the role mentions that are not the seat being filled
    blanked out (_NOT_THE_SEAT_RES)."""
    for rx in _NOT_THE_SEAT_RES:
        text = rx.sub(' ', text)
    return text


def _finance_role_kind(window: str) -> Optional[str]:
    """'cfo' | 'exec' | None — the finance seat the window names. The CFO
    seat wins when both appear ("as President & Chief Financial Officer")."""
    if CFO_ROLE.search(window):
        return 'cfo'
    if FINANCE_LEADER_TITLE.search(window) or _CONTROLLER_HIRED_RE.search(window):
        return 'exec'
    return None


def finance_leader_hire_kind(title: str, body: str = '') -> Optional[str]:
    """'cfo' | 'exec' | None — is this a seated finance-leader hire, and into
    which seat? Decided from the TITLE (rules in the comment block above);
    the head of the body counts only when the title names no finance role,
    and only in the verb-then-role / noun-phrase shape. rss_scraper's
    unknown-territory admission calls this on the title alone.

        'Acme Names Jane Doe CFO'                                → 'cfo'
        'Acme Names Jane Doe Corporate Controller' (+ CFO body)  → 'exec'
        'Acme Reports Q2 Results' + '… said Jane Doe, CFO'       → None
        'Acme Announces Launch of Wireless Game Controller'      → None
    """
    window = _hire_window(title or '')
    kind = _finance_role_kind(window)
    if kind:
        if _HIRE_PHRASE_RE.search(window):
            return kind
        if has_hire_indicator(window) and not _NOT_A_SEAT_RE.search(title or ''):
            return kind
        return None
    head = _hire_window((body or '')[:BODY_HEAD_CHARS])
    kind = _finance_role_kind(head)
    if kind and _HIRE_PHRASE_RE.search(head):
        return kind
    return None


# Public-company indicators that count only in the TITLE (see __init__).
# Lower-case, compared against the configured public_company_indicators.
TITLE_ONLY_PUBLIC_INDICATORS = ('fortune 500', 'fortune 100')

# Product/platform phrases that carry a mega-cap's name without the release
# being about that company (see is_public_company). Lower-case.
_PLATFORM_PHRASES = (
    'oracle netsuite', 'netsuite by oracle', 'amazon web services',
    'microsoft azure', 'microsoft dynamics', 'microsoft 365', 'google cloud',
    'google workspace',
)


class BaseScraper(ABC):
    """Base class for all scrapers."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': config.get('scraper', {}).get(
                'user_agent',
                'Mozilla/5.0 (compatible; SalesTerritoryBot/1.0)'
            )
        })
        self.timeout = config.get('scraper', {}).get('timeout', 30)
        self.request_delay = config.get('scraper', {}).get('request_delay', 2)

        # Load territory config
        self.territory = config.get('territory', {})
        self.regions = [r.lower() for r in (self.territory.get('regions') or [])]
        self.cities = [c.lower() for c in (self.territory.get('cities') or [])]
        # Whole-word matchers for the body-text scan ("Reston" must not hit
        # "Preston", "Dover" must not hit "Andover").
        self._region_res = _compile_whole_word(self.regions)
        self._city_res = _compile_whole_word(self.cities)
        self.target_companies = [c.lower() for c in (self.territory.get('target_companies') or []) if c]
        self.industries = [i.lower() for i in (self.territory.get('industries') or [])]
        self.excluded_industries = [i.lower() for i in (self.territory.get('excluded_industries') or [])]
        # Whole-word (+ plural) — substring matching killed "determining" /
        # "examining" as "mining" and Vermont Business Magazine's in-territory
        # triggers with it (research 2026-09-08). Target industries stay
        # substring-matched: they only add relevance, never reject.
        self._excluded_industry_res = _compile_whole_word(self.excluded_industries, plural=True)

        # Load keywords
        self.keywords = config.get('keywords', {})
        self.exec_hire_keywords = [k.lower() for k in self.keywords.get('executive_hires', [])]
        self.ma_keywords = [k.lower() for k in self.keywords.get('mergers_acquisitions', [])]
        self.funding_keywords = [k.lower() for k in self.keywords.get('funding_events', [])]
        # The generic executive-hire path needs a ROLE keyword: the config
        # lists hire-signal phrases ("appoints", "named as", "promoted to")
        # under executive_hires too, and a keyword that IS the verb made
        # "appoints Beta as software vendor" an executive hire (review
        # 2026-09-08). Keywords whose first word is a hire verb are
        # indicators, never roles.
        self._role_keywords = [
            kw for kw in self.exec_hire_keywords
            if kw.split() and not _HIRE_VERB_RE.match(kw.split()[0])
        ]
        # Generic executive-hire indicators built from the configured roles
        # (review 2026-09-08): "new <role>" and "<hire verb> … <role>". The
        # bare "controller" keyword takes the device guards — "new controller
        # chip" is not a seat.
        role_alt = '|'.join(
            _CONTROLLER if kw == 'controller' else re.escape(kw)
            for kw in self._role_keywords
        ) or r'(?!x)x'                                  # no keywords: never matches
        self._new_role_re = re.compile(r'(?<!\w)new\s+(?:' + role_alt + r')(?!\w)',
                                       re.IGNORECASE)
        self._verb_then_role_re = re.compile(
            r'(?<!\w)' + _HIRE_VERB + r'\s+(?:[^\s,;:]+\s+){0,5}?(?:(?:as|to)\s+)?'
            + _FILLER + r'(?:' + role_alt + r')(?!\w)', re.IGNORECASE)

        # Load company filters (for mid-market private companies)
        self.company_filters = self.territory.get('company_filters', {})
        self.exclude_public = self.company_filters.get('exclude_public_companies', True)
        self.public_indicators = [
            i.lower() for i in (self.company_filters.get('public_company_indicators') or [])
        ]
        # "Fortune 500" / "Fortune 100" fire from the TITLE only (review
        # 2026-09-08): a private company's release calls its new CFO a
        # "Fortune 500 executive" (Lotlinx, live 2026-09-08) — that is the
        # hire's résumé, not the company's listing. Tickers and exchange
        # phrases stay body-wide.
        self._title_only_public_indicators = [
            i for i in self.public_indicators if i in TITLE_ONLY_PUBLIC_INDICATORS
        ]
        self._body_public_indicators = [
            i for i in self.public_indicators if i not in TITLE_ONLY_PUBLIC_INDICATORS
        ]
        self.excluded_public_companies = [
            c.lower() for c in (self.company_filters.get('excluded_public_companies') or [])
        ]
        # Whole words/phrases only — "apple" must not fire inside "pineapple",
        # "us bank" inside "various banks" (research 2026-09-08). The ticker
        # indicators below stay substrings ("(NYSE:") — that policy stands.
        self._excluded_public_company_res = _compile_whole_word(self.excluded_public_companies)
        self.target_size_indicators = [
            i.lower() for i in (self.company_filters.get('target_size_indicators') or [])
        ]

        # Load geographic exclusions (international locations to filter out)
        self.excluded_locations = [
            loc.lower() for loc in (self.territory.get('excluded_locations') or [])
        ]
        # Whole-word only — "India" must not match "Indianapolis", "UK" must
        # not match "Duke"/"Milwaukee". Entries of ≤3 chars ("UK", "US",
        # "UAE") get NO substring path at all; they match solely as whole
        # words (config.example.yaml drops them anyway — too ambiguous).
        self._excluded_location_res = _compile_whole_word(self.excluded_locations)

        # Load content exclusions (irrelevant content types)
        self.excluded_content = [
            c.lower() for c in (self.territory.get('excluded_content') or [])
        ]

        # Require territory match (stricter filtering)
        self.require_territory_match = self.territory.get('require_territory_match', True)

    @abstractmethod
    def scrape(self) -> List[TriggerEvent]:
        """Scrape and return list of trigger events."""
        pass

    def generate_event_id(self, url: str, title: str) -> str:
        """Generate unique ID for an event."""
        content = f"{url}:{title}"
        return hashlib.md5(content.encode()).hexdigest()

    def is_excluded_content(self, text: str) -> bool:
        """Check if text contains excluded content types (concerts, sports, etc.)."""
        text_lower = text.lower()
        for excluded in self.excluded_content:
            if excluded in text_lower:
                return True
        return False

    def detect_event_type(self, text: str, title: Optional[str] = None) -> Optional[EventType]:
        """Detect the type of trigger event from text.

        `title` is the headline when the caller has one (text is then
        "<title> <body>"); without it the whole text is read as the
        headline. The hire type is decided from the title — see the hire
        typing block above finance_leader_hire_kind (review 2026-09-08).
        """
        text_lower = text.lower()

        # First check if this is excluded content (concerts, sports, etc.)
        if self.is_excluded_content(text):
            return None

        if title is None:
            title, body = text, ''
        else:
            body = text[len(title):] if text.startswith(title) else text

        # Finance-leader hires first: a role mention alone is not a hire —
        # an earnings release quoting the CFO, an M&A release quoting the
        # CFO, a launch of a "game controller" must not type as one. The
        # role in the TITLE decides the seat: a Controller reporting to the
        # CFO is EXECUTIVE_HIRE (#NewController +3), never CFO_HIRE (+5).
        kind = finance_leader_hire_kind(title, body)
        if kind == 'cfo':
            return EventType.CFO_HIRE
        if kind == 'exec':
            return EventType.EXECUTIVE_HIRE

        # Generic executive hires (President / CEO / COO …): a configured
        # ROLE keyword anywhere in the text, as before Phase 3, but the
        # indicator must be a strong hire verb or "new <role>" — and it must
        # sit in the title, or in the head of the body when the title names
        # no role (review 2026-09-08). The scan skips role mentions that are
        # not a seat being filled ("Former Fortune 50 Chief Accounting
        # Officer … Joins the Alliance") and device controllers.
        keyword_text = _DEVICE_CONTROLLER_RE.sub(' ', _hire_window(text).lower())
        if (any(kw in keyword_text for kw in self._role_keywords)
                and self._generic_hire_signal(title, body)):
            return EventType.EXECUTIVE_HIRE

        # Check for M&A
        if any(kw in text_lower for kw in self.ma_keywords):
            return EventType.MERGER_ACQUISITION

        # Check for funding - require stronger signals
        funding_strong = ['series a', 'series b', 'series c', 'series d', 'funding round',
                          'raises $', 'raised $', 'secures $', 'secured $', 'investment round',
                          'venture capital', 'private equity', 'seed funding', 'seed round']
        if any(kw in text_lower for kw in funding_strong):
            return EventType.FUNDING

        return None

    def _generic_hire_signal(self, title: str, body: str) -> bool:
        """Hire indicator for the generic executive-hire path. In the title
        (when it names a configured role): a strong hire verb, or "new
        <role>" outside a product launch; a board / award title needs the
        verb-then-role shape. In the head of the body (title names no
        role): verb-then-role or "new <role>" only."""
        raw_title = title or ''
        title = _hire_window(raw_title)               # blank "CFO of the Year" etc.
        title_lower = _DEVICE_CONTROLLER_RE.sub(' ', title.lower())
        if any(kw in title_lower for kw in self._role_keywords):
            if self._new_role_re.search(title) and not _PRODUCT_LAUNCH_RE.search(title):
                return True
            if not has_hire_indicator(title):
                return False
            if _NOT_A_SEAT_RE.search(raw_title):
                return bool(self._verb_then_role_re.search(title))
            return True
        head = _hire_window((body or '')[:BODY_HEAD_CHARS])
        if self._verb_then_role_re.search(head):
            return True
        return bool(self._new_role_re.search(head) and not _PRODUCT_LAUNCH_RE.search(head))

    def is_excluded_location(self, text: str) -> bool:
        """True when the text mentions an excluded (out-of-territory) location
        as a WHOLE WORD. Substring matching was the bug that darkened Indiana
        ("india" ⊂ Indianapolis) and Duke/Milwaukee ("uk").

        Ordering contract: an in-territory signal always wins. Callers
        (matches_territory / territory_status / the scrapers) consult this
        ONLY when there is no dateline, city or state hit — a false reject
        at scrape time is lost forever, a false admit is caught by the
        enrichment HQ gate.
        """
        if not text:
            return False
        return any(rx.search(text) for _term, rx in self._excluded_location_res)

    def extract_dateline_location(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """
        Extract city and state from PR newswire-style dateline.
        For backwards compatibility, returns first location found.
        Use extract_dateline_locations() for multiple locations.
        """
        locations = self.extract_dateline_locations(text)
        if locations:
            return locations[0]
        return None, None

    def extract_dateline_locations(self, text: str) -> List[tuple[Optional[str], Optional[str]]]:
        """
        Extract ALL cities and states from a PR-wire-style dateline.
        Handles multiple locations like "NEW YORK and ARLINGTON, Va."

        The date ("Feb. 10, 2026") is the anchor; the location(s) are the
        segments immediately before it, so the dateline is found whether it
        starts the text or follows a headline ("Acme Names CFO ARLINGTON,
        Va., Feb. 10, 2026 /PRNewswire/"). States resolve from postal codes,
        AP abbreviations AND full names (gates.STATE_NAMES); cities are
        returned lower-case, trimmed to a known territory city when headline
        words are glued to the front ("CFO ARLINGTON" → "arlington").

        Examples:
            "ARLINGTON, Va., Feb. 10, 2026"          -> [("arlington", "virginia")]
            "PORTLAND, Maine, Feb. 10, 2026"          -> [("portland", "maine")]
            "INDIANAPOLIS, Feb. 10, 2026"             -> [("indianapolis", None)]
            "NEW YORK and BOSTON, Feb. 10"            -> [("new york", None), ("boston", None)]
            "NEW YORK and ARLINGTON, Va., Feb. 10"    -> [("new york", None), ("arlington", "virginia")]
            "CHICAGO, IL and RICHMOND, Va., Feb. 10"  -> [("chicago", "illinois"), ("richmond", "virginia")]
            "Arlington, Virginia, Feb. 10, 2026"      -> [("arlington", "virginia")]   (GlobeNewswire)
        """
        locations: List[tuple[Optional[str], Optional[str]]] = []
        if not text:
            return locations

        seen = set()
        # Up to 3 date anchors: a headline like "…to Report Results on Feb. 10"
        # precedes the real dateline; the first anchor that yields a location
        # IS the dateline, later "CITY, State, Month DD" mentions are body text.
        for n, anchor in enumerate(_MONTH_ANCHOR.finditer(text)):
            if n >= 3:
                break
            window = text[max(0, anchor.start() - 120):anchor.start()].strip()
            if not window:
                continue
            for segment in _LOC_SPLIT.split(window):
                segment = segment.strip().strip(',-–—').strip()
                if not segment:
                    continue
                loc = self._parse_dateline_segment(segment)
                if loc and loc not in seen:
                    seen.add(loc)
                    locations.append(loc)
            if locations:
                break

        return locations

    def _parse_dateline_segment(self, segment: str) -> Optional[tuple[Optional[str], Optional[str]]]:
        """One dateline segment → (city, state) or None.

        "CITY, State": split on the LAST comma; the right side must validate
        as a state (postal / AP / full name). Then the city is the trailing
        capitalized run on the left (Title Case allowed here because the
        state vouches for it). Without a valid state the segment must END
        in an ALL-CAPS run ("INDIANAPOLIS", "ST. LOUIS") to count as a city.
        """
        if ',' in segment:
            left, right = segment.rsplit(',', 1)
            state = resolve_state_token(right)
            if state:
                m = _TITLE_RUN_END.search(left.strip())
                city = self._canonical_city(m.group(1)) if m else None
                return city, state
        m = _CAPS_RUN_END.search(segment)
        if m:
            return self._canonical_city(m.group(1)), None
        return None

    def _canonical_city(self, run: str) -> str:
        """Lower-case a capitalized run and trim glued headline words: the
        longest token-suffix that is a known territory city wins
        ("CFO ARLINGTON" → "arlington", "NEW YORK" → "new york"). Accents
        are folded so "MONTRÉAL" / "QUÉBEC CITY" meet the config's
        unaccented "montreal" / "quebec city"."""
        lowered = run.replace('’', "'").lower()
        folded = unicodedata.normalize('NFKD', lowered).encode('ascii', 'ignore').decode()
        for variant in (lowered, folded):
            toks = variant.split()
            for i in range(len(toks)):
                cand = ' '.join(toks[i:])
                if cand in self.cities:
                    return cand
        return ' '.join(folded.split()) or ' '.join(lowered.split())

    def matches_territory(self, text: str) -> tuple[bool, List[str]]:
        """Does the text place the story in our territory? → (bool, matches)

        Order matters (audit 2026-09-06: Indiana, Maine and "Duke…" were
        dark because the exclusion list ran FIRST):
          1. dateline city/state          → in territory, return early
          2. whole-word body scan          → in territory
          3. nothing found                 → not in territory
        The excluded-location list is deliberately NOT consulted here: with
        no in-territory signal the answer is already False, and with one it
        must not veto. Callers that want the out/unknown distinction use
        territory_status(); scrapers apply is_excluded_location() only in
        the no-signal branch.
        """
        matched: List[str] = []

        # 1. Dateline (e.g. "ARLINGTON, Va., Feb. 10, 2026") — highest confidence
        for dateline_city, dateline_state in self.extract_dateline_locations(text):
            if dateline_city and dateline_city in self.cities:
                matched.append(dateline_city)
            if dateline_state and dateline_state in self.regions:
                matched.append(dateline_state)
        if matched:
            return True, matched

        # 2. Whole-word scan of the full text for regions and cities
        for region, rx in self._region_res:
            if rx.search(text):
                matched.append(region)
        for city, rx in self._city_res:
            if rx.search(text):
                matched.append(city)
        if matched:
            return True, matched

        return False, []

    def territory_status(self, text: str) -> str:
        """'in' | 'out' | 'unknown' — 'out' only when there is NO in-territory
        signal and an excluded location is mentioned as a whole word."""
        in_territory, _ = self.matches_territory(text)
        if in_territory:
            return 'in'
        if self.is_excluded_location(text):
            return 'out'
        return 'unknown'

    def matches_industry(self, text: str) -> tuple[bool, bool]:
        """
        Check if text matches target industries.
        Returns: (matches_target, matches_excluded)
        """
        text_lower = text.lower()

        # Check exclusions first — whole words (+ plural), see __init__
        if any(rx.search(text) for _term, rx in self._excluded_industry_res):
            return False, True

        # Check target industries
        for industry in self.industries:
            if industry in text_lower:
                return True, False

        return False, False

    def is_public_company(self, text: str, title: Optional[str] = None) -> bool:
        """Check if text indicates a public company (to exclude).

        `title` is the headline when the caller has one: the title-only
        indicators ("Fortune 500" / "Fortune 100") are looked for there and
        nowhere else. Without a title they are looked for in the text, as
        before (review 2026-09-08)."""
        if not self.exclude_public:
            return False

        text_lower = text.lower()
        head_lower = text_lower if title is None else title.lower()
        if any(indicator in head_lower for indicator in self._title_only_public_indicators):
            return True

        # Check for known large public companies by NAME (whole words). A
        # platform phrase that embeds a mega-cap's name is not that company:
        # an "Oracle NetSuite partner" is a NetSuite partner — NetSuite being
        # what the team sells, that mention must never exclude a release
        # (research 2026-09-08). Those phrases are blanked before the scan.
        name_text = text
        for phrase in _PLATFORM_PHRASES:
            if phrase in text_lower:
                name_text = re.sub(re.escape(phrase), ' ', name_text, flags=re.IGNORECASE)
        if any(rx.search(name_text) for _term, rx in self._excluded_public_company_res):
            return True

        # Check for public company indicators (tickers, exchange phrases —
        # substrings, body-wide)
        for indicator in self._body_public_indicators:
            if indicator in text_lower:
                return True

        return False

    def is_target_company_size(self, text: str) -> bool:
        """Check if text indicates a mid-market company (our target)."""
        text_lower = text.lower()
        for indicator in self.target_size_indicators:
            if indicator in text_lower:
                return True
        return False

    def matches_target_company(self, text: str) -> tuple[bool, Optional[str]]:
        """Check if text mentions a target company."""
        text_lower = text.lower()

        for company in self.target_companies:
            if company and company in text_lower:
                return True, company

        return False, None

    def calculate_relevance_score(
        self,
        event_type: EventType,
        matched_regions: List[str],
        matches_industry: bool,
        matches_company: bool
    ) -> float:
        """Calculate relevance score for an event."""
        score = 0.0

        # Event type scoring
        type_scores = {
            EventType.CFO_HIRE: 40,
            EventType.EXECUTIVE_HIRE: 30,
            EventType.MERGER_ACQUISITION: 35,
            EventType.FUNDING: 25,
            EventType.OTHER: 10
        }
        score += type_scores.get(event_type, 10)

        # Territory match scoring
        score += min(len(matched_regions) * 15, 30)

        # Industry match scoring
        if matches_industry:
            score += 20

        # Target company scoring (highest priority)
        if matches_company:
            score += 50

        return min(score, 100)

    def extract_company_name(self, text: str) -> Optional[str]:
        """Extract the primary company name from a news title/text.

        Designed for the common shapes:
          - Funding rounds:  "Blink Grabs $17M Financing Round"
          - PE-backed M&A:   "Nautic-backed Integrated Home Care Services
                              scoops up Dina Care"  (returns the active company)
          - Exec hires:      "MikeWorldWide Appoints Dave Aglar as CIO"
          - SEC-style:       "Acme Corp Announces..."
          - With prefix:     "Deals & Moves: Beacon Pointe Acquires..."

        Returns None for roundups, all-caps datelines, and other false positives.
        """
        if not text:
            return None

        # 1. Strip common headline prefixes that hide the actual subject
        cleaned = text
        prefixes_to_strip = [
            r'^Deals?\s*(?:&|and)\s*Moves?:\s*',
            r'^Today\'s\s+\w+:\s*',
            r'^(?:Updated|Update|Exclusive|Breaking|Just\s+In):\s*',
            r'^\d+\.\s+',                # numbered list items
            r'^[A-Z]{3,}:\s*',           # "ATLANTA:" datelines
        ]
        for p in prefixes_to_strip:
            cleaned = re.sub(p, '', cleaned, flags=re.IGNORECASE)

        # 2. Bail on roundup / digest headlines (no single subject company)
        if re.match(
            r'^\d+\s+(?:Press|Releases|Stories|Headlines|Hires|Deals|Moves)\b',
            cleaned, re.IGNORECASE
        ):
            return None

        # 3. Verbs that signal a company is the active subject.
        # Case-insensitive (inline scoped flag) so we catch both "Grabs" and
        # "grabs" — VC News Daily uses Title Case, but other sources mix it.
        # The company portion of the pattern keeps required leading capital
        # via [A-Z] so we don't false-match common words.
        funding_verbs = (
            r'(?i:grabs?|secures?|raises?|receives?|pulls?\s+in|closes?|lands?|'
            r'completes?|nabs?|scoops?\s+up|snags?|snaps?\s+up|bags?|picks?\s+up|'
            r'hauls?\s+in|racks?\s+up|wraps?|tops?\s+off|gets?|acquires?|buys?|'
            r'merges?\s+with|announces?|names?|appoints?|hires?|welcomes?|adds?|'
            r'brings?\s+on|adopts?|files?|reports?|reveals?|unveils?|launches?|'
            r'forms?|joins?|bets?|inks?|taps|promotes?|elevates?|selects?)'
        )

        company_chars = r"[A-Z][\w\s&\.\-'’]"  # caps-start, then letters/space/punct

        # Preprocess: strip "{PE}-backed " prefix so the active company
        # becomes the leading subject. Handles "EIG-backed MidOcean racks up..."
        cleaned = re.sub(
            rf"^{company_chars}{{1,40}}?-backed\s+",
            '',
            cleaned,
        )

        patterns = [
            # "{Company} <funding_verb> ..." — optional "to " before the verb
            # handles "White Cap to acquire X" (was capturing "White Cap to")
            (rf"^({company_chars}{{1,60}}?)\s+(?:to\s+)?{funding_verbs}\b", 1),
            # Corporate suffix anywhere in text
            (
                r"\b("
                r"[A-Z][\w&\.\-'’]+(?:\s+[A-Z][\w&\.\-'’]+){0,5}"
                r"(?:\s+(?:Inc\.?|Corp\.?|LLC|Ltd\.?|Co\.?|Holdings|Group|"
                r"Partners|Capital|Ventures|Bank|Trust|Foundation|"
                r"Healthcare|Health|Energy|Technologies|Tech|Solutions))"
                r")\b",
                1,
            ),
            # Legacy: "{Company} announces|appoints|..." (case-insensitive)
            (rf"({company_chars}{{1,60}}) (?:announces?|appoints?|names?|hires?)", 1),
            # "at/joins/of {Company}"
            (rf"(?:at|joins|of) ({company_chars}{{1,60}}?)(?:\.|,|$|\s+for\s+|\s+as\s+)", 1),
        ]

        for pattern, group_idx in patterns:
            match = re.search(pattern, cleaned)
            if not match:
                continue

            company = match.group(group_idx).strip()
            # Strip trailing punctuation
            company = re.sub(r'[,;:\.\s]+$', '', company)
            # Strip "the " prefix
            company = re.sub(r'^[Tt]he\s+', '', company)

            # Sanity checks
            if not (2 < len(company) < 80):
                continue
            # Reject all-caps datelines like "NEW YORK", "ATLANTA"
            if company.isupper() and len(company.split()) <= 3:
                continue
            # Reject common false positives
            if company.lower() in {
                'the', 'today', 'breaking', 'news', 'press', 'press release',
                'new york', 'boston', 'chicago', 'los angeles', 'san francisco',
                'company', 'companies', 'corp', 'inc', 'group', 'partners',
                'this week', 'this morning', 'this year',
            }:
                continue
            # Reject if mostly digits (e.g. "5 Million")
            if sum(c.isdigit() for c in company) > len(company) / 2:
                continue

            return company

        return None

    def extract_person_info(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """Extract person name and title from text."""
        # Common title patterns
        title_pattern = r'(?:as|named|appointed|new)\s+((?:Chief\s+)?(?:Financial|Executive|Operating|Technology)\s+Officer|CFO|CEO|COO|CTO|VP\s+\w+|President|Director)'
        title_match = re.search(title_pattern, text, re.IGNORECASE)
        title = title_match.group(1) if title_match else None

        # Name patterns (usually before "as" or "named")
        name_pattern = r'([A-Z][a-z]+\s+[A-Z][a-z]+)(?:\s+(?:as|named|appointed|joins))'
        name_match = re.search(name_pattern, text)
        name = name_match.group(1) if name_match else None

        return name, title

    def delay_request(self):
        """Add delay between requests to be respectful."""
        time.sleep(self.request_delay)

    def detect_stable_target_potential(self, text: str) -> tuple[bool, List[str]]:
        """
        Detect if an article mentions a company that fits our criteria
        even without a specific trigger event. Returns (is_potential, reasons).

        Looks for positive company signals like:
        - Growth, expansion, new locations
        - Awards, recognition
        - New products/services/contracts
        - Partnership announcements
        - Leadership mentions
        - Industry feature articles
        """
        text_lower = text.lower()
        reasons = []

        # Positive company signals that indicate a company worth tracking
        growth_signals = [
            ('expands', 'Company expansion mentioned'),
            ('expansion', 'Company expansion mentioned'),
            ('growth', 'Company growth mentioned'),
            ('growing', 'Company growth mentioned'),
            ('new location', 'New location/facility announced'),
            ('new facility', 'New facility announced'),
            ('opens new', 'New opening announced'),
            ('grand opening', 'New opening announced'),
            ('relocating', 'Company relocation mentioned'),
            ('headquarters', 'Headquarters mentioned'),
        ]

        award_signals = [
            ('award', 'Company received award/recognition'),
            ('winner', 'Company received award/recognition'),
            ('recognized', 'Company recognized'),
            ('named top', 'Company named as top performer'),
            ('best of', 'Company named as top performer'),
            ('excellence', 'Company excellence recognized'),
            ('certification', 'Company certification mentioned'),
            ('certified', 'Company certification mentioned'),
        ]

        business_signals = [
            ('new contract', 'New contract announced'),
            ('wins contract', 'Contract win announced'),
            ('awarded contract', 'Contract award announced'),
            ('partnership', 'Partnership announced'),
            ('partners with', 'Partnership announced'),
            ('strategic alliance', 'Strategic alliance announced'),
            ('collaboration', 'Business collaboration mentioned'),
            ('new product', 'New product launched'),
            ('launches', 'New launch announced'),
            ('introduces', 'New introduction announced'),
            ('unveils', 'New unveiling announced'),
            ('new service', 'New service announced'),
        ]

        leadership_signals = [
            ('ceo', 'CEO/leadership mentioned'),
            ('chief executive', 'Leadership mentioned'),
            ('founder', 'Founder mentioned'),
            ('president', 'President mentioned'),
            ('leadership', 'Leadership mentioned'),
            ('executive team', 'Executive team mentioned'),
        ]

        industry_signals = [
            ('industry leader', 'Company positioned as industry leader'),
            ('market leader', 'Company positioned as market leader'),
            ('leading provider', 'Company positioned as leading provider'),
            ('top provider', 'Company positioned as top provider'),
            ('fastest growing', 'Fast growth company'),
            ('inc. 5000', 'Inc. 5000 company'),
            ('inc 5000', 'Inc. 5000 company'),
        ]

        all_signals = growth_signals + award_signals + business_signals + leadership_signals + industry_signals

        for keyword, reason in all_signals:
            if keyword in text_lower and reason not in reasons:
                reasons.append(reason)

        # Must have at least one positive signal
        if not reasons:
            return False, []

        return True, reasons

    def generate_stable_target_reasoning(
        self,
        company_name: Optional[str],
        matched_regions: List[str],
        matched_industries: List[str],
        positive_signals: List[str],
        is_target_size: bool
    ) -> str:
        """Generate a reasoning explanation for why this company is recommended."""
        parts = []

        if company_name:
            parts.append(f"Company: {company_name}")

        if matched_regions:
            parts.append(f"Territory match: {', '.join(matched_regions[:3])}")

        if matched_industries:
            parts.append(f"Industry match: {', '.join(matched_industries[:3])}")

        if is_target_size:
            parts.append("Appears to be mid-market/private company")

        if positive_signals:
            parts.append(f"Signals: {'; '.join(positive_signals[:4])}")

        return " | ".join(parts) if parts else "Matches territory and industry criteria"

    def get_matched_industries(self, text: str) -> List[str]:
        """Get list of matched industry keywords."""
        text_lower = text.lower()
        matched = []
        for industry in self.industries:
            if industry in text_lower:
                matched.append(industry)
        return matched[:5]  # Limit to top 5
