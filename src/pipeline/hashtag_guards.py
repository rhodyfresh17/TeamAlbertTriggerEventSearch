"""Deterministic hashtag guards for the TAL V11 grader (Phase 4 slice C2,
2026-09-08).

WHY. The local LLM picks the rubric hashtags and the code computes the
score and grade (enrichment_scout._compute_v11_grade). The hashtag list is
the one creative step left to the model, and the audit history is one of
STUFFING: #NewCFO on material-agreement 8-Ks, #Funding on a $500K seed,
#100EE from a '51-200' bucket, #Global from a foreign HQ, #FormerUser and
#PrevConvo that no input could possibly evidence. A.J.'s rubric says
"when in doubt, DROP the hashtag"; this table makes the mechanically
checkable half of that rule code, applied AFTER the model's list and
BEFORE the score is computed, so an unsupported tag never reaches a grade.

CONTRACT. HASHTAG_GUARDS = {tag: check(event, account_company, fit,
evidence) -> (ok, why)}. `event` is the event row (title / description /
event_type / source; `companies_data` attached by the caller for the
investor-role test), `account_company` the companies_data record the fit
gates chose as the account (role, zi_subindustry, size, registry_source …),
`fit` the event-level fit dict, `evidence` the research-probe block that was
handed to the grader. A tag without a guard passes through untouched; the
closed-set filter in grade_event still bounds the list. apply_guards()
returns the kept list (order preserved) and one note per strip in the
form '-#Funding (amount $500K < $1M)' — logged once per event by the
caller and written to grading['guard_notes']. A tag kept on a SOURCE-level
exception (the SEC 8-K Item 5.02 path below) adds a note too, in the form
'#NewCFO kept — SEC 5.02 CFO filing', so the audit trail shows why a tag
survived with no hire subject in the stored text; strip_count() tells the
two apart.

NEVER STRICTER THAN THE RUBRIC (review 2026-09-08, Phase 4): the guards
strip tags the model invents without evidence; they do not second-guess
evidence the rubric accepts. The adversarial review over 519 graded events
found seven places where a guard demanded MORE than TAL V11 does — an SEC
5.02 CFO filing whose stored description carries no hire subject, a
verified raise on a non-funding event, a bank holding company outside the
'Holding Companies & Conglomerates' label, a US-HQ account with one foreign
office, a '100+' size bucket, #AssetManagerScale on any AUM figure, and a
funding parser that took the largest figure in the text — each is fixed and pinned in tests/test_hashtag_guards.py.

Pure functions, no network, no enrichment_scout import (that module
imports this one). Python 3.9.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from src.pipeline.gates import ALL_STATE_CODES, hq_state_code
from src.pipeline.hires import (
    NEW_CFO_ROLES, NEW_CONTROLLER_ROLES, finance_hire_subject, role_label,
)

# ── Vocabulary that must stay in step with enrichment_scout ─────────────────
# The nonprofit half of enrichment_scout.ZI_SUBINDUSTRIES (the vertical
# label map); tests/test_hashtag_guards.py asserts the two agree. Duplicated
# because that module imports this one.
NONPROFIT_SUBINDUSTRIES = frozenset({
    'Blood & Organ Banks', 'Childcare', 'Colleges & Universities',
    'Cultural & Informational Centers', 'K-12 Schools', 'Libraries',
    'Membership Organizations', 'Museums & Art Galleries',
    'Non-Profit & Charitable Organizations',
    'Non-Profit Organizations & Charitable Foundations',
    'Performing Arts Theaters', 'Religious Organizations', 'Training',
    'Zoos & National Parks',
})
HOLDCO_SUBINDUSTRY = 'Holding Companies & Conglomerates'
FUNDING_MIN_USD = 1_000_000          # rubric + A.J. 2026-08-14: a micro-raise is not a trigger
FUNDING_LOOKBACK_MONTHS = 18         # rubric: "verified funding within the last 18 months"
FUNDING_VERB_WINDOW = 6              # words between a raise verb and its amount (review 2026-09-08, 3h)
HEADCOUNT_MIN = 100                  # #100EE
# A size bucket verifies 100+ by construction when its LOW bound is ≥ 100:
# '100+', '100-500', '101-250', '201-500' … (review 2026-09-08 (Phase 4),
# 3f — the old 201 floor stripped buckets that already say 100+; the
# rubric's only carve-out is '51-200', whose low bound is 51).
SIZE_BUCKET_MIN = HEADCOUNT_MIN
AUM_SCALE_MIN_USD = 1_000_000_000    # SEC IAPD path: RAUM at or above $1B
# Text path floor (review 2026-09-08 (Phase 4), 3g): the rubric says
# "< $250M usually too early", so a "$5 million AUM" figure in an article
# is not scale evidence; the IAPD path keeps its own $1B bar.
AUM_TEXT_MIN_USD = 250_000_000
LEGACY_FOUNDED_MAX_YEAR = 1996       # founded 30+ years ago (A.J.-approved plan, 2026-09-06)
ACQUIRER_ROLES = ('acquirer', 'primary')
INVESTOR_ROLES = ('investor', 'lead investor')
KEPT_PREFIX = 'kept — '              # a guard's `why` starting with this = a logged keep, not a strip

_NUMBER_WORDS = {
    'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
    'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19,
    'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70,
    'eighty': 80, 'ninety': 90, 'hundred': 100, 'hundreds': 200, 'dozens': 24,
}
_NUM = r'(?:\d[\d,]*|' + '|'.join(_NUMBER_WORDS) + r')'
_QUAL = r'(?:more\s+than|over|nearly|almost|approximately|about|some|roughly|~|\+)?\s*'

LOCATION_NOUNS = (r'(?:locations?|offices?|branch(?:es)?|stores?|campus(?:es)?|facilit(?:y|ies)|'
                  r'clinics?|sites?|dealerships?|showrooms?|restaurants?|plants?|warehouses?|'
                  r'centers?|centres?|practices?|hospitals?|schools?|properties|communities|'
                  r'agencies|outlets?|studios?|salons?|shops?|hubs?|depots?|terminals?|'
                  r'distribution\s+centers?|service\s+centers?)')
ENTITY_NOUNS = (r'(?:subsidiar(?:y|ies)|entities|legal\s+entities|brands?|business\s+units?|'
                r'operating\s+(?:companies|units|subsidiaries|entities)|divisions?|affiliates?|'
                r'portfolio\s+companies|operating\s+brands?|companies\s+under\s+(?:its|the)\s+umbrella)')
_COUNT_RE_CACHE = {}


def _count_re(nouns: str):
    rx = _COUNT_RE_CACHE.get(nouns)
    if rx is None:
        rx = re.compile(r'(?<![\w$])' + _QUAL + r'(' + _NUM + r')\s*\+?\s*(?:(?:new|additional|'
                        r'retail|physical|company-owned|franchised|owned|operated|separate|'
                        r'distinct|wholly[\s-]owned)\s+){0,2}' + nouns + r'(?!\w)', re.IGNORECASE)
        _COUNT_RE_CACHE[nouns] = rx
    return rx


def _to_int(token: str) -> Optional[int]:
    tok = token.lower().replace(',', '')
    if tok in _NUMBER_WORDS:
        return _NUMBER_WORDS[tok]
    try:
        return int(tok)
    except ValueError:
        return None


def max_count_before(nouns: str, text: str) -> int:
    """Largest numeric count that precedes one of `nouns` ('12 locations',
    'twelve offices', 'more than 40 stores', '3+ branches'); 0 when none.
    A year is never a count ('in 2019 offices' is not 2,019 offices)."""
    best = 0
    for m in _count_re(nouns).finditer(text or ''):
        n = _to_int(m.group(1))
        if n is None or 1900 <= n <= 2100 and len(m.group(1)) == 4:
            continue
        best = max(best, n)
    return best


# ── Money ───────────────────────────────────────────────────────────────────
_AMOUNT_RE = re.compile(r'\$\s?([\d][\d,]*(?:\.\d+)?)\s*(billion|bn|million|mm|[bmk])?\b', re.I)
# The words a raise is stated with; the amount next to one of them IS the
# raise ("raises $500K seed to chase a $40 billion market" raised $500K).
_FUND_WORD_RE = re.compile(r'(?<!\w)(?:rais(?:e|es|ed|ing)|round|funding|financing|'
                           r'secur(?:e|es|ed|ing)|clos(?:e|es|ed|ing))(?!\w)', re.I)


def _amount_value(m) -> Optional[float]:
    try:
        val = float(m.group(1).replace(',', ''))
    except ValueError:
        return None
    unit = (m.group(2) or '').lower()
    if unit in ('billion', 'bn', 'b'):
        val *= 1_000_000_000
    elif unit in ('million', 'mm', 'm'):
        val *= 1_000_000
    elif unit == 'k':
        val *= 1_000
    return val


def parse_funding_amount(text: str) -> Optional[float]:
    """The raise stated in the text, in dollars ('$6.8 Million', '$37M',
    '$2,500,000', '$1.2B'). None if nothing parseable. Moved here from
    enrichment_scout (which re-exports it as _parse_funding_amount) so the
    #Funding guard and the search tier read ONE parser.

    Which figure (review 2026-09-08 (Phase 4), 3h): the amount within
    FUNDING_VERB_WINDOW words of a raise word (raise/raised/round/funding/
    financing/secured/closes) wins — the nearest one, ties to the larger —
    because a release names the market it chases and the total raised to
    date next to the round itself; the LARGEST figure in the text is only
    the fallback when no amount sits next to a raise word (a bare '$37M'
    headline). The search tier reads the same figure, so a $500K seed
    stays tier 3 whatever market size the release quotes."""
    text = text or ''
    amounts = []
    for m in _AMOUNT_RE.finditer(text):
        val = _amount_value(m)
        if val is not None:
            amounts.append((m.start(), val))
    if not amounts:
        return None
    starts = [t.start() for t in re.finditer(r'\S+', text)]

    def _tok(pos: int) -> int:
        return max(bisect_right(starts, pos) - 1, 0)

    verbs = [_tok(m.start()) for m in _FUND_WORD_RE.finditer(text)]
    best = None
    for pos, val in amounts:
        dist = min((abs(_tok(pos) - v) for v in verbs), default=None)
        if dist is not None and dist <= FUNDING_VERB_WINDOW:
            key = (dist, -val)
            if best is None or key < best[0]:
                best = (key, val)
    if best is not None:
        return best[1]
    return max(val for _, val in amounts)


def format_usd(amount: Optional[float]) -> str:
    if amount is None:
        return 'n/a'
    if amount >= 1_000_000_000:
        return f'${amount / 1_000_000_000:.1f}B'.replace('.0B', 'B')
    if amount >= 1_000_000:
        return f'${amount / 1_000_000:.1f}M'.replace('.0M', 'M')
    if amount >= 1_000:
        return f'${amount / 1_000:.0f}K'
    return f'${amount:.0f}'


# ── Dates (for the rubric's "within the last 18 months") ────────────────────
def _now() -> datetime:
    """Wall clock, UTC — one function so tests can pin it."""
    return datetime.now(timezone.utc)


def months_ago(now: datetime, months: int) -> datetime:
    """`now` minus a calendar-month count (the day clamped to the month)."""
    y, m = divmod(now.year * 12 + (now.month - 1) - months, 12)
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return now.replace(year=y, month=m, day=min(now.day, last))


_MONTH_NAMES = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                'september', 'october', 'november', 'december')
_MON = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?'
_YEAR = r'(?:19[89]\d|20\d{2})'
_DATE_RES = (
    ('ymd', re.compile(r'(?<![\d$,.])(' + _YEAR + r')[-/](\d{1,2})[-/](\d{1,2})(?!\d)')),
    ('mdy', re.compile(r'(?<!\w)(' + _MON + r')\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(' + _YEAR + r')(?!\d)',
                       re.IGNORECASE)),
    ('dmy', re.compile(r'(?<![\d$,.])(\d{1,2})\s+(' + _MON + r')\s+(' + _YEAR + r')(?!\d)', re.IGNORECASE)),
    ('mdy_num', re.compile(r'(?<![\d$,.])(\d{1,2})/(\d{1,2})/(' + _YEAR + r')(?!\d)')),
    ('my', re.compile(r'(?<!\w)(' + _MON + r')\s+(' + _YEAR + r')(?!\d)', re.IGNORECASE)),
    ('qy', re.compile(r'(?<!\w)q([1-4])\s+(' + _YEAR + r')(?!\d)', re.IGNORECASE)),
    ('y', re.compile(r'(?<![\d$,./-])(' + _YEAR + r')(?![\d,%]|\s*%)')),
)


def _month_num(token: str) -> Optional[int]:
    """'March' / 'Mar' / 'Sept.' → 3 / 3 / 9; None for anything else."""
    tok = token.lower().rstrip('.')
    for i, name in enumerate(_MONTH_NAMES, start=1):
        if name.startswith(tok[:3]) and name.startswith(tok):
            return i
    return None


def _month_end(y: int, m: int) -> datetime:
    """The last instant-day of month `m` of year `y` (UTC midnight)."""
    first_next = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    return first_next - timedelta(days=1)


def dates_in(text: str) -> list:
    """Every date the text states, as aware UTC datetimes, each read at
    its LATEST possible instant ('March 2025' → 2025-03-31, '2024' →
    2024-12-31, 'Q1 2025' → 2025-03-31). Lenient on purpose: an ambiguous
    date must never make a guard stricter than the rubric. Dates more
    than a month in the future are noise (a typo, a target date) and are
    dropped. Formats: 2025-03-12, 2025/03/12 (URLs), March 12, 2025,
    12 March 2025, 03/12/2025, March 2025, Q1 2025, a bare year."""
    out = []
    limit = _now() + timedelta(days=31)
    t = text or ''
    for kind, rx in _DATE_RES:
        # Precise forms first; each match is blanked so a looser form never
        # re-reads its year ('Feb 1, 2025' must not also yield 2025-12-31).
        matches = list(rx.finditer(t))
        t = rx.sub(lambda m: ' ' * (m.end() - m.start()), t)
        for m in matches:
            try:
                if kind == 'ymd':
                    dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                elif kind == 'mdy':
                    dt = datetime(int(m.group(3)), _month_num(m.group(1)) or 0, int(m.group(2)),
                                  tzinfo=timezone.utc)
                elif kind == 'dmy':
                    dt = datetime(int(m.group(3)), _month_num(m.group(2)) or 0, int(m.group(1)),
                                  tzinfo=timezone.utc)
                elif kind == 'mdy_num':
                    dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
                elif kind == 'my':
                    dt = _month_end(int(m.group(2)), _month_num(m.group(1)) or 0)
                elif kind == 'qy':
                    dt = _month_end(int(m.group(2)), int(m.group(1)) * 3)
                else:
                    dt = datetime(int(m.group(1)), 12, 31, tzinfo=timezone.utc)
            except ValueError:
                continue
            if dt <= limit:
                out.append(dt)
    return out


_FUNDING_BLOCK_RE = re.compile(r'FUNDING SEARCH\b.*?(?=\n[ \t]*\n|\Z)', re.S)
_HIT_SPLIT_RE = re.compile(r'\n(?=- )')
# A passage is a RAISE only with raise language in it: "closes $50M
# acquisition of Beta" is a deal value, not funding, even though 'closes'
# anchors the amount ("closes $25M Series B") once the language is there.
_RAISE_CONTEXT_RE = re.compile(r'(?<!\w)(?:rais(?:e|es|ed|ing)|funding|financing|round|'
                               r'series\s+[a-h](?!\w)|seed|secur(?:e|es|ed|ing)|'
                               r'recapitali[sz]ation|capital\s+raise|investment)(?!\w)', re.I)


def funding_evidence(text: str, now: Optional[datetime] = None) -> tuple:
    """(amount, why) — the best verified raise in `text`: a raise-word-
    adjacent amount ≥ FUNDING_MIN_USD whose passage is dated within
    FUNDING_LOOKBACK_MONTHS (an undated passage is accepted — the rubric
    calls the FUNDING SEARCH block authoritative and the guard must not be
    stricter than it). `text` is judged per passage: each '- title | url'
    hit of a FUNDING SEARCH block with its snippet, and the rest of the text
    as one passage. (None, why) when nothing qualifies, `why` naming the
    nearest miss (too small / too old / no amount)."""
    now = now or _now()
    cutoff = months_ago(now, FUNDING_LOOKBACK_MONTHS)
    passages = []
    rest = text or ''
    for m in _FUNDING_BLOCK_RE.finditer(rest):
        body = m.group(0).split('\n', 1)[1] if '\n' in m.group(0) else ''
        passages += [h for h in _HIT_SPLIT_RE.split(body) if h.strip()]
    rest = _FUNDING_BLOCK_RE.sub(' ', rest)
    if rest.strip():
        passages.append(rest)
    best, miss = None, 'no parseable amount'
    for p in passages:
        amt = parse_funding_amount(p)
        if amt is None:
            continue
        if not _RAISE_CONTEXT_RE.search(p):
            miss = f'{format_usd(amt)} is not stated as a raise (no raise/round/funding language)'
            continue
        if amt < FUNDING_MIN_USD:
            miss = f'amount {format_usd(amt)} < {format_usd(FUNDING_MIN_USD)}'
            continue
        dates = dates_in(p)
        latest = max(dates) if dates else None
        if latest is not None and latest < cutoff:
            miss = f'{format_usd(amt)} raise dated {latest.date().isoformat()} is older than {FUNDING_LOOKBACK_MONTHS} months'
            continue
        cand = (amt, f'amount {format_usd(amt)}' + (f' dated {latest.date().isoformat()}' if latest else ' (undated)'))
        if best is None or cand[0] > best[0]:
            best = cand
    return best if best else (None, miss)


# The phrases an AUM figure sits next to. Review 2026-09-08 (Phase 4), 3g:
# "$X in client assets", "assets under advisement", "assets for N families"
# are how RIAs and wealth managers state scale; they were unreadable here.
_AUM_WORDS = (r'(?:aum|aua|raum|assets\s+under\s+(?:management|advisement|administration|supervision)|'
              r'regulatory\s+assets(?:\s+under\s+management)?|managed\s+assets|'
              r'(?:client|clients\'?|advisory|discretionary|fee-based|brokerage|custod(?:y|ied)|'
              r'investment)\s+assets|'
              r'assets\s+(?:of|totaling|total(?:l)?ing)|'
              r'assets\s+(?:for|on\s+behalf\s+of)\s+' + _QUAL + r'[\d,]+\+?\s+'
              r'(?:families|clients|households|investors|institutions|individuals))')
_AUM_RES = (
    re.compile(_AUM_WORDS + r'[^.;]{0,40}?(\$\s?[\d][\d,]*(?:\.\d+)?\s*(?:billion|bn|million|mm|[bmk])?)\b',
               re.IGNORECASE),
    re.compile(r'(\$\s?[\d][\d,]*(?:\.\d+)?\s*(?:billion|bn|million|mm|[bmk])?)\b[^.;]{0,40}?' + _AUM_WORDS,
               re.IGNORECASE),
)


def aum_amount(text: str) -> Optional[float]:
    """The largest AUM / RAUM / AUA figure stated in the text (dollars), or
    None when no dollar figure sits within ~40 chars of an AUM phrase."""
    best = None
    for rx in _AUM_RES:
        for m in rx.finditer(text or ''):
            val = parse_funding_amount(m.group(1))
            if val is not None:
                best = max(best or 0, val)
    return best


# ── Headcount ───────────────────────────────────────────────────────────────
_HEADCOUNT_RES = (
    re.compile(r'(?<![\w$])' + _QUAL + r'(\d[\d,]*)\s*\+?\s*(?:full-time\s+|fulltime\s+|'
               r'ft\s+)?(?:employees|staff(?:\s+members)?|team\s+members|people|workers|'
               r'associates|professionals|headcount|personnel|ftes?|fte)(?!\w)', re.IGNORECASE),
    re.compile(r'(?<!\w)(?:employs|headcount\s+of|workforce\s+of|team\s+of|staff\s+of)\s+'
               + _QUAL + r'(\d[\d,]*)', re.IGNORECASE),
    re.compile(r'(?<![\w$])(\d[\d,]*)-(?:person|employee|member)\b', re.IGNORECASE),
)


def max_headcount(text: str) -> int:
    best = 0
    for rx in _HEADCOUNT_RES:
        for m in rx.finditer(text or ''):
            n = _to_int(m.group(1))
            if n is not None and not (1900 <= n <= 2100 and len(m.group(1)) == 4):
                best = max(best, n)
    return best


def size_lower_bound(size) -> Optional[int]:
    """'201-500' → 201, '1,001-5,000' → 1001, '10,001+' → 10001, '500+' → 500,
    '250 employees' → 250; None when unreadable."""
    m = re.search(r'(\d[\d,]*)', str(size or ''))
    if not m:
        return None
    try:
        return int(m.group(1).replace(',', ''))
    except ValueError:
        return None


# ── Countries (#Global) ─────────────────────────────────────────────────────
# Whole-word country NAMES only — demonyms and cities never count, and the
# US-state homonyms (Georgia) and person-name homonyms (Jordan, Chad) are
# left out on purpose. 'US' / 'UK' count only in their capitalised or
# dotted forms ("join us" is not a country).
COUNTRY_NAMES = {
    'united states': 'US', 'united states of america': 'US', 'usa': 'US',
    'united kingdom': 'UK', 'great britain': 'UK', 'britain': 'UK', 'england': 'UK',
    'scotland': 'UK', 'wales': 'UK', 'northern ireland': 'UK',
    'canada': 'CA', 'mexico': 'MX', 'germany': 'DE', 'france': 'FR', 'spain': 'ES',
    'italy': 'IT', 'netherlands': 'NL', 'the netherlands': 'NL', 'belgium': 'BE',
    'switzerland': 'CH', 'austria': 'AT', 'sweden': 'SE', 'norway': 'NO', 'denmark': 'DK',
    'finland': 'FI', 'ireland': 'IE', 'portugal': 'PT', 'poland': 'PL',
    'czech republic': 'CZ', 'czechia': 'CZ', 'hungary': 'HU', 'greece': 'GR',
    'romania': 'RO', 'ukraine': 'UA', 'turkey': 'TR', 'israel': 'IL',
    'united arab emirates': 'AE', 'saudi arabia': 'SA', 'qatar': 'QA', 'south africa': 'ZA',
    'nigeria': 'NG', 'kenya': 'KE', 'egypt': 'EG', 'morocco': 'MA', 'india': 'IN',
    'china': 'CN', 'japan': 'JP', 'south korea': 'KR', 'korea': 'KR', 'taiwan': 'TW',
    'singapore': 'SG', 'malaysia': 'MY', 'indonesia': 'ID', 'thailand': 'TH',
    'vietnam': 'VN', 'philippines': 'PH', 'pakistan': 'PK', 'bangladesh': 'BD',
    'australia': 'AU', 'new zealand': 'NZ', 'brazil': 'BR', 'argentina': 'AR',
    'chile': 'CL', 'colombia': 'CO', 'peru': 'PE', 'costa rica': 'CR', 'panama': 'PA',
    'dominican republic': 'DO', 'jamaica': 'JM', 'bermuda': 'BM', 'cayman islands': 'KY',
    'luxembourg': 'LU', 'iceland': 'IS', 'estonia': 'EE', 'latvia': 'LV', 'lithuania': 'LT',
    'slovakia': 'SK', 'slovenia': 'SI', 'croatia': 'HR', 'serbia': 'RS', 'bulgaria': 'BG',
    'russia': 'RU', 'kazakhstan': 'KZ', 'hong kong': 'HK', 'sri lanka': 'LK',
    'nepal': 'NP', 'uruguay': 'UY', 'ecuador': 'EC', 'bolivia': 'BO', 'venezuela': 'VE',
    'guatemala': 'GT', 'honduras': 'HN', 'el salvador': 'SV', 'nicaragua': 'NI',
    'ghana': 'GH', 'ethiopia': 'ET', 'tanzania': 'TZ', 'uganda': 'UG', 'rwanda': 'RW',
    'malta': 'MT', 'cyprus': 'CY', 'monaco': 'MC', 'liechtenstein': 'LI',
}
_COUNTRY_RE = re.compile(
    r'(?<!\w)(?:' + '|'.join(sorted((re.escape(n) for n in COUNTRY_NAMES), key=len, reverse=True))
    + r')(?!\w)', re.IGNORECASE)
_US_UK_RE = re.compile(r'(?<![\w.])(U\.S\.A?\.?|USA|US|U\.K\.|UK)(?![\w.])')


def countries_in(text: str) -> set:
    found = {COUNTRY_NAMES[m.group(0).lower()] for m in _COUNTRY_RE.finditer(text or '')}
    for m in _US_UK_RE.finditer(text or ''):
        found.add('UK' if 'K' in m.group(1) else 'US')
    return found


_CA_PROVINCES = frozenset({'ON', 'QC', 'NB', 'NS', 'PE', 'NL', 'BC', 'AB', 'MB', 'SK', 'YT', 'NT', 'NU'})


def home_country(account: dict) -> Optional[str]:
    """The account's own country from its hq ('Boston, MA' → US, 'Toronto,
    ON' → CA, 'London, United Kingdom' → UK, a bare state code → US/CA);
    None when the hq names no state, province or country. Review
    2026-09-08 (Phase 4), 3d: #Global counted only the countries NAMED in
    the text, so a US-HQ bank opening a London office counted one country
    — the HQ is the other one."""
    hq = _s((account or {}).get('hq')) or _s((account or {}).get('hq_state'))
    if not hq.strip():
        return None
    code = hq_state_code(hq)
    if code in _CA_PROVINCES:
        return 'CA'
    if code in ALL_STATE_CODES:
        return 'US'
    found = countries_in(hq)
    return next(iter(found)) if len(found) == 1 else None


# ── Growth (#HyperGrowth) ───────────────────────────────────────────────────
_GROWTH_WORD = (r'(?:growth|grew|grow(?:n|ing|s)?|increase[ds]?|increasing|rise|rose|risen|'
                r'jump(?:ed|s)?|surge[ds]?|up|expansion|expanded|expand(?:s|ing)?|yoy|'
                r'year[\s-]over[\s-]year|cagr|gain(?:s|ed)?|climb(?:ed|s)?|soar(?:ed|s)?|'
                r'record)')
_GROWTH_RES = (
    re.compile(r'(?<!\w)(?:\d[\d,]*(?:\.\d+)?)\s?%' + r'[^.;]{0,50}?(?<!\w)' + _GROWTH_WORD + r'(?!\w)',
               re.IGNORECASE),
    re.compile(r'(?<!\w)' + _GROWTH_WORD + r'(?!\w)[^.;]{0,50}?(?:\d[\d,]*(?:\.\d+)?)\s?%', re.IGNORECASE),
    re.compile(r'(?<![\w$])\d+(?:\.\d+)?x(?!\w)[^.;]{0,50}?(?<!\w)' + _GROWTH_WORD + r'(?!\w)',
               re.IGNORECASE),
    re.compile(r'(?<!\w)' + _GROWTH_WORD + r'(?!\w)[^.;]{0,50}?(?<![\w$])\d+(?:\.\d+)?x(?!\w)',
               re.IGNORECASE),
    re.compile(r'(?<!\w)(?:doubled|tripled|quadrupled|inc\.?\s*5000|fastest[\s-]growing)(?!\w)',
               re.IGNORECASE),
)


def growth_figure(text: str) -> Optional[str]:
    for rx in _GROWTH_RES:
        m = rx.search(text or '')
        if m:
            return re.sub(r'\s+', ' ', m.group(0))[:60]
    return None


# ── Founded (#Legacy) ───────────────────────────────────────────────────────
_FOUNDED_RE = re.compile(r'(?<!\w)(?:founded|established|est\.?|since|incorporated|'
                         r'in\s+business|operating|serving\s+[\w\s]{0,30}?)\s+(?:in\s+)?(1[89]\d{2}|20\d{2})(?!\d)',
                         re.IGNORECASE)
_LEGACY_SYSTEMS = ('quickbooks', 'sage 50', 'sage 100', 'sage 300', 'sage 500', 'dynamics gp',
                   'great plains', 'peachtree', 'mas 90', 'mas 200', 'mas90', 'mas200',
                   'legacy erp', 'legacy accounting', 'legacy system', 'legacy financial system',
                   'excel-based', 'spreadsheet-based')


def founded_year(text: str) -> Optional[int]:
    years = [int(m.group(1)) for m in _FOUNDED_RE.finditer(text or '')]
    return min(years) if years else None


# ── Helpers over the guard inputs ───────────────────────────────────────────
def _s(v) -> str:
    return str(v or '')


def _event_text(event: dict) -> str:
    return f"{_s(event.get('title'))} {_s(event.get('description'))}"


def _all_text(event: dict, account: dict, evidence: str) -> str:
    return f"{_event_text(event)} {_s((account or {}).get('name'))} {_s(evidence)}"


def _zi(fit: dict, account: dict) -> str:
    return _s((fit or {}).get('zi_subindustry') or (account or {}).get('zi_subindustry')).strip()


def _role(account: dict) -> str:
    return _s((account or {}).get('role')).strip().lower()


def _is_nonprofit(fit: dict, account: dict) -> bool:
    if _zi(fit, account) in NONPROFIT_SUBINDUSTRIES:
        return True
    ind = _s((account or {}).get('industry')).lower()
    return 'nonprofit' in ind or 'non-profit' in ind or '501(c)' in ind


def _is_sec_filing(event: dict) -> bool:
    """An EDGAR row (source_url on sec.gov): its stored description is the
    scraper's one-liner ('SEC 8-K filing by X — Item 5.02: … Filing date:
    …'), never the filing text."""
    try:
        host = (urlparse(_s(event.get('source_url'))).hostname or '').lower()
    except ValueError:
        return False
    return host == 'sec.gov' or host.endswith('.sec.gov')


# ── The guards ──────────────────────────────────────────────────────────────
def _new_cfo(event, account, fit, evidence):
    et = _s(event.get('event_type'))
    if et == 'finance_seat_open':
        return False, 'open seat (job posting) is #NewController, never a seated CFO'
    if et == 'cfo_hire' and _is_sec_filing(event):
        # Review 2026-09-08 (Phase 4), 3a: every SEC 8-K Item 5.02 cfo_hire
        # (19/19 live rows) lost #NewCFO because the stored description is
        # the scraper's one-liner with no hire subject. The scraper typed
        # the filing cfo_hire from EDGAR full-text search on the phrase
        # "Chief Financial Officer" in the filing itself (sec_scraper
        # _cfo_adsh_set) — that typing IS the evidence, and the rubric's
        # own rule is "Apply ONLY if event_type=cfo_hire OR …".
        return True, KEPT_PREFIX + 'SEC 5.02 CFO filing'
    subj = finance_hire_subject(_s(event.get('title')), _s(event.get('description')))
    if subj['role'] in NEW_CFO_ROLES:
        return True, f"{role_label(subj['role'])} hire subject: {subj['evidence'][:60]}"
    if subj['role']:
        return False, (f"hire subject is a {role_label(subj['role'])}, not a CFO-equivalent"
                       + (' (use #NewController)' if subj['role'] in NEW_CONTROLLER_ROLES else ''))
    return False, 'no CFO hire subject in the text' + (' (event_type cfo_hire)' if et == 'cfo_hire' else '')


def _new_controller(event, account, fit, evidence):
    et = _s(event.get('event_type'))
    if et == 'finance_seat_open':
        return True, 'open finance seat'
    if et == 'executive_hire' and _is_sec_filing(event):
        # The 3a argument, one seat down: an EDGAR 5.02 typed executive_hire
        # is a filing whose text names a Controller / Chief Accounting
        # Officer (sec_scraper _finance_adsh_set minus the CFO set) — the
        # stored one-liner cannot show it, the scraper's typing can.
        return True, KEPT_PREFIX + 'SEC 5.02 Controller/CAO filing'
    subj = finance_hire_subject(_s(event.get('title')), _s(event.get('description')))
    if subj['role'] in NEW_CONTROLLER_ROLES:
        return True, f"{role_label(subj['role'])} hire subject: {subj['evidence'][:60]}"
    if subj['role'] in NEW_CFO_ROLES:
        return False, f"hire subject is a {role_label(subj['role'])} (CFO-equivalent → #NewCFO)"
    if subj['role']:
        return False, f"hire subject is a {role_label(subj['role'])} — not a rubric seat"
    return False, 'no Controller / VP Accounting / CAO hire subject in the text'


def _acquisitions(event, account, fit, evidence):
    et = _s(event.get('event_type'))
    if et != 'merger_acquisition':
        return False, f'event_type {et or "?"} is not merger_acquisition'
    role = _role(account)
    if role in ACQUIRER_ROLES:
        return True, f'account role {role}'
    return False, f'account role {role or "?"} is not the acquirer'


def _funding(event, account, fit, evidence):
    et = _s(event.get('event_type'))
    if _is_nonprofit(fit, account):
        return False, f'nonprofit ({_zi(fit, account) or "industry"}) — grants are not funding'
    if et == 'funding':
        amt = parse_funding_amount(_event_text(event))
        if amt is None:
            return False, 'no parseable amount in the text'
        if amt < FUNDING_MIN_USD:
            return False, f'amount {format_usd(amt)} < {format_usd(FUNDING_MIN_USD)}'
        return True, f'amount {format_usd(amt)}'
    # Review 2026-09-08 (Phase 4), 3b: the guard demanded event_type
    # 'funding', which the rubric never does ("verified funding within the
    # last 18 months"; the FUNDING SEARCH block is "authoritative") — and
    # made the probe_funding_history spend on non-funding events worthless.
    # A raise ≥ $1M in that block (or the event text) dated within 18 months
    # — undated accepted — is the evidence the rubric asks for.
    amt, why = funding_evidence(f'{_s(evidence)}\n\n{_event_text(event)}')
    if amt is None:
        return False, (f'no verified raise ≥ {format_usd(FUNDING_MIN_USD)} within '
                       f'{FUNDING_LOOKBACK_MONTHS} months in the text or the FUNDING SEARCH '
                       f'evidence (event_type {et or "?"}; {why})')
    return True, f'{why} in the FUNDING SEARCH evidence / text (event_type {et})'


_PE_PHRASES = ('private equity', 'pe-backed', 'pe backed', 'portfolio company', 'portfolio companies',
               'sponsor-backed', 'private-equity')


def _pe_backed(event, account, fit, evidence):
    text = _all_text(event, account, evidence).lower()
    hit = next((p for p in _PE_PHRASES if p in text), None)
    if hit:
        return True, f'"{hit}" in text'
    for c in (event.get('companies_data') or []):
        if isinstance(c, dict) and _s(c.get('role')).strip().lower() in INVESTOR_ROLES:
            return True, f'investor role present ({_s(c.get("name"))[:30]})'
    return False, 'no private-equity / portfolio-company evidence and no investor role'


def _hundred_ee(event, account, fit, evidence):
    size = _s((account or {}).get('size'))
    low = size_lower_bound(size)
    if low is not None and low >= SIZE_BUCKET_MIN:
        return True, f'size bucket {size}'
    n = max_headcount(_all_text(event, account, evidence))
    if n >= HEADCOUNT_MIN:
        return True, f'headcount {n} in evidence'
    if low is not None:
        return False, (f"size bucket '{size}' alone does not verify 100+ (low bound {low} < "
                       f"{SIZE_BUCKET_MIN}) and no headcount ≥ 100 in evidence")
    return False, 'no size bucket and no headcount ≥ 100 in evidence'


def _global(event, account, fit, evidence):
    found = countries_in(_all_text(event, account, evidence))
    home = home_country(account)
    if home:
        found = found | {home}
    if len(found) >= 2:
        return True, f'countries: {", ".join(sorted(found))}' + (f' (HQ {home})' if home else '')
    return False, f'{len(found)} country named incl. the HQ country {home or "?"} (need ≥ 2)'


def _asset_manager_scale(event, account, fit, evidence):
    if _s((account or {}).get('registry_source')) == 'sec_iapd':
        raum = None
        for k in ('raum_usd', 'raum', 'aum_usd', 'aum'):
            try:
                raum = float((account or {}).get(k)) if (account or {}).get(k) is not None else None
            except (TypeError, ValueError):
                raum = None
            if raum is not None:
                break
        if raum is None:
            raum = aum_amount(_all_text(event, account, evidence))
        if raum is not None and raum >= AUM_SCALE_MIN_USD:
            return True, f'SEC IAPD RAUM {format_usd(raum)}'
        return False, f'SEC IAPD RAUM {format_usd(raum)} < {format_usd(AUM_SCALE_MIN_USD)}'
    amt = aum_amount(_all_text(event, account, evidence))
    if amt is not None and amt >= AUM_TEXT_MIN_USD:
        return True, f'AUM figure {format_usd(amt)} in evidence'
    if amt is not None:
        return False, (f'AUM figure {format_usd(amt)} < {format_usd(AUM_TEXT_MIN_USD)} '
                       f'(rubric: under $250M is too early)')
    return False, 'no AUM / RAUM figure in evidence'


def _never(event, account, fit, evidence):
    return False, 'CRM-only fact — no pipeline input can evidence it'


def _franchise(event, account, fit, evidence):
    if 'franchis' in _all_text(event, account, evidence).lower():
        return True, '"franchis…" in text'
    return False, 'no franchise language in text'


def _holdco(event, account, fit, evidence):
    # Review 2026-09-08 (Phase 4), 3c: the rubric ORs three tests — the name
    # or text says Holdings / holding company, the industry is the holdco
    # label, or evidence of ≥ 2 operating subsidiaries. The guard ANDed the
    # first two, which would have stripped 31 of 42 live tags, every bank
    # holding company among them.
    text = _all_text(event, account, evidence)
    m = re.search(r'(?<!\w)(?:holding\s+compan(?:y|ies)|holdings)(?!\w)', text, re.IGNORECASE)
    if m:
        return True, f'"{m.group(0)}" in name/text'
    zi = _zi(fit, account)
    if zi == HOLDCO_SUBINDUSTRY:
        return True, f'zi {zi}'
    n = max_count_before(ENTITY_NOUNS, text)
    if n >= 2:
        return True, f'{n} operating subsidiaries/entities counted in text/evidence'
    return False, (f'no "holding company" / "holdings" in name/text, zi {zi or "?"} is not '
                   f'{HOLDCO_SUBINDUSTRY}, and no subsidiary count ≥ 2 in evidence')


def _locations(event, account, fit, evidence):
    n = max_count_before(LOCATION_NOUNS, _all_text(event, account, evidence))
    if n >= 2:
        return True, f'{n} locations counted in text/evidence'
    return False, 'no numeric location count ≥ 2 in text/evidence'


def _entities(event, account, fit, evidence):
    n = max_count_before(ENTITY_NOUNS, _all_text(event, account, evidence))
    if n >= 2:
        return True, f'{n} entities counted in text/evidence'
    return False, 'no numeric subsidiary / entity count ≥ 2 in text/evidence'


def _hyper_growth(event, account, fit, evidence):
    fig = growth_figure(_all_text(event, account, evidence))
    if fig:
        return True, f'growth figure: {fig}'
    return False, 'no growth figure (%, x, doubled/tripled, Inc. 5000) in text'


def _legacy(event, account, fit, evidence):
    text = _all_text(event, account, evidence)
    low = text.lower()
    sys_hit = next((s for s in _LEGACY_SYSTEMS if s in low), None)
    if sys_hit:
        return True, f'legacy system named: {sys_hit}'
    year = founded_year(text)
    if year is not None and year <= LEGACY_FOUNDED_MAX_YEAR:
        return True, f'founded {year}'
    if year is not None:
        return False, f'founded {year} > {LEGACY_FOUNDED_MAX_YEAR} and no legacy system named'
    return False, 'no founding year ≤ 1996 and no legacy system named'


HASHTAG_GUARDS = {
    '#NewCFO': _new_cfo,
    '#NewController': _new_controller,
    '#Acquisitions': _acquisitions,
    '#Funding': _funding,
    '#PEBacked': _pe_backed,
    '#100EE': _hundred_ee,
    '#Global': _global,
    '#AssetManagerScale': _asset_manager_scale,
    '#FormerUser': _never,
    '#PrevConvo': _never,
    '#Franchisor': _franchise,
    '#Franchisee': _franchise,
    '#HoldCo': _holdco,
    '#Locations': _locations,
    '#Entities': _entities,
    '#HyperGrowth': _hyper_growth,
    '#Legacy': _legacy,
}


def strip_count(notes) -> int:
    """How many of apply_guards' notes are strips ('-#Tag (…)') — a kept
    note ('#Tag kept — …') is an audit line, not a strip."""
    return sum(1 for n in (notes or []) if str(n).startswith('-'))


def apply_guards(hashtags, event: dict, account_company: dict = None, fit: dict = None,
                 evidence: str = '', companies_data=None):
    """(kept_hashtags, notes). Every tag with a guard is checked; a failed
    check strips it and adds '-<tag> (<why>)' to notes; a check that KEEPS
    a tag on a source-level exception (its `why` starts with KEPT_PREFIX)
    adds '<tag> kept — <basis>' so the log shows why a tag with no hire
    subject in the stored text survived. `companies_data` (when given) is
    exposed to the checks as event['companies_data'] on a shallow copy —
    the event row itself is never mutated."""
    ev = dict(event or {})
    if companies_data is not None:
        ev['companies_data'] = companies_data
    account_company = account_company or {}
    fit = fit or {}
    evidence = evidence or ''
    kept, notes = [], []
    for tag in hashtags or []:
        check = HASHTAG_GUARDS.get(tag)
        if check is None:
            kept.append(tag)
            continue
        try:
            ok, why = check(ev, account_company, fit, evidence)
        except Exception as e:      # noqa: BLE001 — a broken guard must never break a grade
            ok, why = True, f'guard error ignored: {type(e).__name__}'
        if ok:
            kept.append(tag)
            if str(why).startswith(KEPT_PREFIX):
                notes.append(f'{tag} {why}')
        else:
            notes.append(f'-{tag} ({why})')
    return kept, notes
