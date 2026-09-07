"""Deterministic, network-free gates for TeamAlbert v2.

Everything here decides `in` / `out` / `unknown` from FREE signals — company
names, EDGAR SIC codes, Form D fields, HQ strings — BEFORE any LLM call or
web search fires. Policy lives here (A.J.'s exclusions, 2026-09-04/06) so
scrapers, enrichment, cleanup and the dashboard all agree.

Rule: these gates REJECT and ROUTE. They never admit a vertical as confirmed
on their own — confirmation still needs article evidence or research.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# ── Territory ────────────────────────────────────────────────────────────────
# 23 US states + DC + 6 eastern Canadian provinces (FY27 xlsx; DC confirmed).
TERRITORY_STATES = {
    'ME', 'NH', 'VT', 'MA', 'RI', 'CT',
    'NY', 'NJ', 'PA', 'DE', 'MD', 'VA', 'WV', 'DC',
    'NC', 'SC', 'GA', 'FL', 'AL', 'TN', 'KY',
    'OH', 'MI', 'IN',
    'ON', 'QC', 'NB', 'NS', 'PE', 'NL',
}
ALL_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
    'ON', 'QC', 'NB', 'NS', 'PE', 'NL', 'BC', 'AB', 'MB', 'SK', 'YT', 'NT', 'NU',
}
STATE_NAMES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE',
    'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
    'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC',
    'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'district of columbia': 'DC', 'washington dc': 'DC', 'washington, d.c.': 'DC',
    'ontario': 'ON', 'quebec': 'QC', 'québec': 'QC', 'new brunswick': 'NB',
    'nova scotia': 'NS', 'prince edward island': 'PE', 'newfoundland': 'NL',
    'newfoundland and labrador': 'NL', 'british columbia': 'BC',
    'alberta': 'AB', 'manitoba': 'MB', 'saskatchewan': 'SK', 'yukon': 'YT',
}
# Country / region tokens that are noise in an HQ string ("Boston, MA, USA")
_COUNTRY_TOKENS = {
    'usa', 'u.s.', 'u.s.a.', 'us', 'united states', 'united states of america',
    'canada', 'north america',
}
# Whole-word foreign markers → confidently OUT
_FOREIGN = (
    'germany', 'france', 'uk', 'united kingdom', 'england', 'scotland',
    'london', 'india', 'china', 'japan', 'australia', 'israel', 'singapore',
    'switzerland', 'netherlands', 'sweden', 'ireland', 'spain', 'italy',
    'brazil', 'mexico', 'hong kong', 'korea', 'norway', 'denmark', 'finland',
    'belgium', 'austria', 'chile', 'dubai', 'uae', 'luxembourg', 'bermuda',
    'cayman', 'cayman islands', 'europe', 'asia',
)


def hq_territory_status(hq: Optional[str]) -> str:
    """'in' | 'out' | 'unknown' for an HQ string.

    Scans EVERY comma segment (v1 read only the last one, so "Boston, MA,
    USA" came back unknown). Country tokens are stripped; full state names
    resolve through STATE_NAMES so "Seattle, Washington" is confidently OUT.
    """
    if not hq or not str(hq).strip():
        return 'unknown'
    h = str(hq).strip()
    segs = [s.strip() for s in re.split(r'[,/|]', h) if s.strip()]
    segs = [s for s in segs if s.lower().strip('. ') not in _COUNTRY_TOKENS]
    if not segs:
        return 'unknown'
    # Right-to-left: state usually follows the city.
    for seg in reversed(segs):
        s = seg.strip('. ')
        up = s.upper()
        if len(up) == 2 and up.isalpha():
            if up in TERRITORY_STATES:
                return 'in'
            if up in ALL_STATE_CODES:
                return 'out'
        lo = s.lower()
        if lo in STATE_NAMES:
            return 'in' if STATE_NAMES[lo] in TERRITORY_STATES else 'out'
        # "Boston MA" without a comma
        m = re.search(r'\b([A-Za-z]{2})$', s)
        if m and len(s.split()) >= 2:
            code = m.group(1).upper()
            if code in TERRITORY_STATES:
                return 'in'
            if code in ALL_STATE_CODES and code not in ('IN', 'OR', 'ME', 'DE', 'OH', 'HI'):
                # ambiguous English words excluded from the bare-code path
                return 'out'
    lo_all = h.lower()
    for name, code in STATE_NAMES.items():
        if re.search(r'\b' + re.escape(name) + r'\b', lo_all):
            return 'in' if code in TERRITORY_STATES else 'out'
    for f in _FOREIGN:
        if re.search(r'\b' + re.escape(f) + r'\b', lo_all):
            return 'out'
    return 'unknown'


# ── HQ → state/province code ────────────────────────────────────────────────
# Bare 2-letter tails that are also English words ("Portland ME"): only
# trusted when a comma separates them from the city (same list the
# 'Boston MA' path in hq_territory_status refuses).
_AMBIGUOUS_TAILS = {'IN', 'OR', 'ME', 'DE', 'OH', 'HI'}
# Longest names first so "West Virginia" wins over "Virginia" in free text.
_STATE_NAMES_LONGEST_FIRST = sorted(STATE_NAMES.items(), key=lambda kv: -len(kv[0]))


def hq_state_code(hq) -> Optional[str]:
    """'Boston, MA' / 'Boston, Massachusetts' / 'Toronto, ON, Canada' /
    'massachusetts' → 'MA' / 'MA' / 'ON' / 'MA'. None when no state or
    province can be read (city-only, foreign, blank, NaN). Same parsing
    strategy as hq_territory_status(), but returns the CODE — the typed
    `hq_state` column (Phase 2, 2026-09-07). Ported from dashboard.py,
    which keeps its own copy until Phase 4 consolidates."""
    if hq is None or (isinstance(hq, float) and hq != hq):
        return None
    h = str(hq).strip()
    if not h:
        return None
    h = re.sub(r'\bd\.c\.?(?=\W|$)', 'dc', h, flags=re.IGNORECASE)
    segs = [s.strip() for s in re.split(r'[,/|]', h) if s.strip()]
    segs = [s for s in segs if s.lower().strip('. ') not in _COUNTRY_TOKENS]
    # Right-to-left: the state usually follows the city.
    for seg in reversed(segs):
        s = seg.strip('. ')
        up = s.upper()
        if len(up) == 2 and up.isalpha() and up in ALL_STATE_CODES:
            return up
        lo = s.lower()
        if lo in STATE_NAMES:
            return STATE_NAMES[lo]
        m = re.search(r'\b([A-Za-z]{2})$', s)   # "Boston MA" without a comma
        if m and len(s.split()) >= 2:
            code = m.group(1).upper()
            if code in ALL_STATE_CODES and code not in _AMBIGUOUS_TAILS:
                return code
    lo_all = h.lower()
    for name, code in _STATE_NAMES_LONGEST_FIRST:
        if re.search(r'\b' + re.escape(name) + r'\b', lo_all):
            return code
    return None


# ── Entity shape (A.J. 2026-09-04/06 exclusions) ────────────────────────────
_GREEK = ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta',
          'theta', 'iota', 'kappa', 'lambda', 'mu', 'nu', 'xi', 'omicron',
          'pi', 'rho', 'sigma', 'tau', 'upsilon', 'phi', 'chi', 'psi', 'omega')

_NONPROFIT_EXEMPT = ('foundation', 'community', 'charitable', 'charity',
                     'church', 'ministries', 'endowment', 'scholarship',
                     'united way', 'ymca', 'ywca', 'credit union')

_FUND_VEHICLE = (
    r'\bfunds?\b', r'\bl\.?\s?p\.?\s*$', r'\bbdc\b', r'\bmaster\b', r'\bfeeder\b',
    r'\bspv\b', r'\bco-?invest', r'\ba series of\b', r'\bseries [a-z0-9]{1,3}\b',
    r'\bopportunit(y|ies) (fund|partners)',
    # numbered/roman series vehicles: "Cantor Equity Partners II, Inc.", "Fund III LP"
    r'\b(partners|equity|capital|investments?|holdings|opportunit\w*|ventures|growth|income|credit|lending)\b[^,]*\b(ix|iv|v?i{1,3}|x{1,2}i{0,3})\b',
    r'\b(ix|iv|v?i{1,3}|x{1,2}i{0,3})\b\s*[,.]?\s*(l\.?p\.?|llc|ltd|inc\.?)\s*$',
    # credit funds dressed as companies: "CNL Strategic Residential Credit, Inc."
    r'\b(residential|commercial|structured|specialty|private|strategic|senior|direct|opportunistic) credit\b',
    r'\btic general partnership\b', r'\breal assets\b', r'\blending co\b',
    r'\bholdings?,? l\.?p\.?\s*$', r'\binvestments?,? l\.?p\.?\s*$',
    r'\bsecuritization\b', r'\breceivables\b', r'\bcredit (fund|opportunit)',
)
_SPAC = (
    r'\bacquisition(?: (?:[ivx]+|\d+))? (corp|corporation|co|company|inc)\b', r'\bspac\b',
    r'\bblank check\b', r'\bmerger corp\b',
)
_POLITICAL = (
    r'\bfor (congress|senate|governor|mayor|president|assembly|council|sheriff|judge)\b',
    r'\bcampaign\b', r'\bpac\b', r'\bpolitical action\b', r'\bcommittee to elect\b',
    r'\bvictory fund\b', r'\bfriends of .*\b(20\d\d|for (congress|senate|governor|mayor|council))\b',
    r'\b(democratic|republican) (party|committee)\b',
)
_GOVERNMENT = (
    r'\bcity of\b', r'\bcounty of\b', r'\btown of\b', r'\bvillage of\b',
    r'\bstate of\b', r'\bcommonwealth of\b', r'\bdepartment of\b',
    r'\bfirst nations?\b', r'\bsix nations\b', r'\bband council\b', r'\btribal\b', r'\btribe\b',
    r'\bindian band\b', r'\bmohawk council\b', r"\bmi'kmaq\b", r'\bnation of the\b',
    r'\b(housing|transit|port|development|water|sewer|redevelopment|parking) authority\b',
    r'\bpublic works\b', r'\bmunicipal', r'\bcounty government\b',
    r'\b(township|borough|parish) of\b', r'\bsheriff', r'\bpolice department\b',
    r'\bfire district\b', r'\bregional council\b',
)
_K12 = (
    'school district', 'public schools', 'board of education', 'unified school',
    'school corporation', 'county schools', 'city schools', 'school board',
    'department of education', 'charter school', 'charter public school',
    'prep school', 'preparatory school', 'montessori', 'elementary school',
    'middle school', 'high school', 'independent school district', 'isd',
    'community schools', 'consolidated school', 'regional school',
    'school system', 'k-12', 'k12', 'day school', 'academy of', 'christian school',
    'catholic school', 'grammar school', 'primary school', 'school of the',
)
_LODGING = (
    r'\bhotels?\b', r'\blodging\b', r'\bresorts?\b', r'\binn\b', r'\bmotel\b',
    r'\bhospitality\b', r'\bsuites\b', r'\bhostel\b',
)


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def is_non_operating_entity(name: Optional[str], descriptor: str = '') -> Tuple[bool, str]:
    """(True, kind) when the NAME shape says this can never be a NetSuite
    account. kinds: fund_vehicle · spac · political · government · k12 ·
    lodging · greek. Nonprofit-looking names are exempt from the fund test
    ("Community Foundation Fund" is a charity, not a vehicle)."""
    n = f' {(name or "").strip().lower()} '
    if not n.strip():
        return False, ''
    d = (descriptor or '').lower()
    if _any(_SPAC, n) or 'blank check' in d or 'special purpose acquisition' in d:
        return True, 'spac'
    if _any(_POLITICAL, n):
        return True, 'political'
    if _any(_GOVERNMENT, n):
        return True, 'government'
    _ends_school = (re.search(r'\bschools?\s*$', n)
                    and not re.search(r'\b(business|law|medical|medicine|graduate|dental|nursing|'
                                      r'public health|design|management|engineering|divinity|'
                                      r'pharmacy|driving|flight|trade|culinary|beauty|barber|'
                                      r'cosmetology|real estate|technical|vocational|of the arts)\b', n))
    if (any(p in n for p in _K12) or _ends_school) and not any(x in n for x in ('bank', 'insurance', 'capital')):
        # "isd" needs word boundaries; substring pass above is loose for it
        if 'isd' in n and not re.search(r'\bisd\b', n):
            pass
        else:
            return True, 'k12'
    if _any(_LODGING, n) and not any(x in n for x in ('insurance', 'bank', 'capital', 'financial')):
        return True, 'lodging'
    greek_hits = sum(1 for g in _GREEK if re.search(r'\b' + g + r'\b', n))
    if greek_hits >= 2 or re.search(r'\b(fraternity|sorority)\b', n):
        return True, 'greek'
    if not any(x in n for x in _NONPROFIT_EXEMPT):
        if _any(_FUND_VEHICLE, n) or 'pooled investment' in d or 'investment vehicle' in d:
            return True, 'fund_vehicle'
    return False, ''


# ── Company-name sanity ─────────────────────────────────────────────────────
_CORP_VOCAB = {
    'inc', 'llc', 'ltd', 'corp', 'corporation', 'co', 'company', 'group',
    'holdings', 'partners', 'capital', 'bank', 'bancorp', 'financial',
    'insurance', 'credit', 'trust', 'advisors', 'advisers', 'ventures',
    'systems', 'technologies', 'technology', 'labs', 'health', 'energy',
    'foods', 'services', 'solutions', 'industries', 'international', 'global',
    'associates', 'management', 'realty', 'properties', 'brands', 'media',
    'networks', 'logistics', 'mutual', 'federal', 'savings', 'union',
    'foundation', 'institute', 'university', 'college', 'hospital', 'clinic',
    'agency', 'dealers', 'motors', 'auto', 'automotive', 'salon', 'studio',
    'funeral', 'cleaning', 'repair', 'wealth', 'asset', 'assets', 'equity',
    'lending', 'mortgage', 'payments', 'pay', 'fintech', 'securities',
    'investments', 'investors', 'fund', 'partners', 'plc', 'sa', 'ag', 'nv',
    'world', 'center', 'centre', 'clinic', 'church', 'ministries', 'school',
}
_JUNK_NAMES = {'local org', 'unknown', 'unknown company', 'company', 'the company',
               'nan', 'none', 'n/a', 'undisclosed', 'startup', 'a company'}
_VERB_PHRASE = re.compile(
    r'\b(joins|names|appoints|announces|hires|says|said|reports|welcomes|'
    r'taps|promotes|elevates|acquires|raises|secures|closes|launches|'
    r'completes|expands|opens|unveils|sentenced|charged|arrested)\b', re.I)


def looks_like_person_name(name: str) -> bool:
    """Two/three capitalized words with no corporate vocabulary: 'John Smith'."""
    words = (name or '').strip().split()
    if not 2 <= len(words) <= 3:
        return False
    if any(w.lower().strip('.,') in _CORP_VOCAB for w in words):
        return False
    if any(ch.isdigit() or ch in '&-' for ch in name):
        return False
    return all(re.fullmatch(r"[A-Z][a-z'\-]+\.?", w) for w in words)


def is_bad_company_name(name: Optional[str], context: str = '') -> bool:
    """True when the extracted 'company' is not a company: junk placeholder,
    a headline fragment, or (when the article reads like a hire) a person."""
    n = (name or '').strip()
    if not n or n.lower() in _JUNK_NAMES:
        return True
    if n[0] in '"\'“' or n[-1] in ',:;-–—' or n.lower().endswith((' at', ' of', ' to', ' in', ' for', ' with', ' as')):
        return True
    words = n.split()
    if len(words) > 6 and not any(w.lower().strip('.,') in _CORP_VOCAB for w in words):
        return True
    if _VERB_PHRASE.search(n):
        return True
    if looks_like_person_name(n) and context:
        # Name-shape alone is NOT enough ("Weber Shandwick", "Morgan Stanley"
        # look like people). Flag only when the article makes the NAME the
        # subject/object of a hire: "John Smith joins …", "… appoints John Smith".
        esc = re.escape(n)
        ctx = context
        if (re.search(rf'\b{esc}\b\s+(joins|has joined|will join|to join|was appointed|'
                      rf'has been (named|appointed|promoted)|is (named|appointed|promoted)|'
                      rf'named|appointed|promoted|becomes|takes over|tapped)\b', ctx, re.I)
                or re.search(rf'\b(appoints|names|hires|welcomes|taps|promotes|elevates|'
                             rf'adds|announces)\s+{esc}\b', ctx, re.I)):
            return True
    return False


# ── Structured vertical routing (reject / route only) ───────────────────────
# SIC → 'out' | 'vehicle' | 'unknown'. Financial services, nonprofits,
# consumer services and anything ambiguous stay 'unknown' for confirmation.
_SIC_OUT_PREFIXES = (
    '01', '02', '07', '08', '09',            # agriculture/forestry/fishing
    '10', '12', '13', '14',                  # mining, oil & gas
    '15', '16', '17',                        # construction
    *[f'{i:02d}' for i in range(20, 40)],    # manufacturing 20-39
    *[f'{i:02d}' for i in range(40, 50)],    # transport/utilities/telecom
    '50', '51',                              # wholesale
    '52', '53', '54', '56', '57', '58', '59',  # retail (55 = auto dealers stays)
    '80', '81',                              # health services, legal
)
# NOTE: SIC 7370-7379 (software / data processing) is deliberately NOT here —
# fintech and payments companies (Repay Holdings, SurgePays…) carry those codes
# and are in-vertical for A.J. (2026-07-16 false-block; re-confirmed 2026-09-07).
_SIC_OUT_EXACT = {'7011': 'lodging', '8711': 'engineering', '8731': 'research',
                  '8734': 'testing labs', '8741': 'management services'}
_SIC_VEHICLE = {'6770': 'spac', '6722': 'fund', '6726': 'fund'}


def sic_to_verdict(sic: Optional[str]) -> Tuple[str, str]:
    """('out'|'vehicle'|'unknown', reason)."""
    s = (sic or '').strip()
    if not s or not s.isdigit():
        return 'unknown', ''
    if s in _SIC_VEHICLE:
        return 'vehicle', f'SIC {s} ({_SIC_VEHICLE[s]})'
    if s in _SIC_OUT_EXACT:
        return 'out', f'SIC {s} ({_SIC_OUT_EXACT[s]})'
    if s[:2] in _SIC_OUT_PREFIXES:
        return 'out', f'SIC {s} (off-vertical industry)'
    return 'unknown', ''


# Form D industryGroupType → routing. Names exactly as EDGAR emits them.
_FORMD_OUT = {
    'Agriculture', 'Coal Mining', 'Electric Utilities', 'Energy Conservation',
    'Environmental Services', 'Oil and Gas', 'Other Energy', 'Biotechnology',
    'Health Insurance', 'Hospitals and Physicians', 'Pharmaceuticals',
    'Other Health Care', 'Manufacturing', 'Restaurants', 'Retailing',
    'Computers', 'Telecommunications', 'Other Technology',
    'Airlines and Airports', 'Lodging and Conventions',
    'Tourism and Travel Services', 'Other Travel', 'Construction',
    'REITS and Finance',
}
_FORMD_VEHICLE = {'Pooled Investment Fund'}
# Revenue ranges as EDGAR emits them → (segment, action)
_FORMD_REVENUE = {
    'No Revenues': ('pre-revenue', 'too_small'),
    '$1 - $1,000,000': ('micro', 'too_small'),
    '$1,000,001 - $5,000,000': ('small', 'too_small'),
    '$5,000,001 - $25,000,000': ('LMM', 'research'),
    '$25,000,001 - $100,000,000': ('MM', 'research'),
    'Over $100,000,000': ('Enterprise', 'out'),
}
FORMD_MIN_RAISE_WHEN_UNDISCLOSED = 10_000_000  # A.J. 2026-09-04


def formd_to_verdict(group: Optional[str], revenue_range: Optional[str],
                     amount: Optional[float], spac_flag: bool = False) -> Tuple[str, str, str]:
    """(verdict, revenue_segment, reason) where verdict ∈
    'out' | 'vehicle' | 'too_small' | 'unknown' (= worth researching)."""
    g = (group or '').strip()
    if spac_flag:
        return 'vehicle', '', 'Form D business-combination (SPAC) flag'
    if g in _FORMD_VEHICLE:
        return 'vehicle', '', f'Form D industry group {g}'
    if g in _FORMD_OUT:
        return 'out', '', f'Form D industry group {g} (off-vertical)'
    rr = (revenue_range or '').strip()
    if rr in _FORMD_REVENUE:
        seg, action = _FORMD_REVENUE[rr]
        if action == 'out':
            return 'out', seg, f'Form D declared revenue {rr} (Enterprise)'
        if action == 'too_small':
            return 'too_small', seg, f'Form D declared revenue {rr} (below $5M bar)'
        return 'unknown', seg, ''
    # Undisclosed / not applicable / missing → must be raising real money
    try:
        amt = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amt = None
    if amt is not None and amt < FORMD_MIN_RAISE_WHEN_UNDISCLOSED:
        return 'too_small', '', (f'Form D revenue undisclosed and raise ${amt:,.0f} '
                                 f'< ${FORMD_MIN_RAISE_WHEN_UNDISCLOSED:,.0f}')
    return 'unknown', '', ''


# ── Account identity ─────────────────────────────────────────────────────────
_SUFFIXES = (' incorporated', ' corporation', ' company', ' limited', ' inc.',
             ' inc', ' llc', ' l.l.c.', ' ltd.', ' ltd', ' corp.', ' corp',
             ' co.', ' co', ' plc', ' l.p.', ' lp', ' llp', ' pllc', ' pc',
             ' n.a.', ' na', ' s.a.', ' ag', ' gmbh', ' nv', ' bv')


def account_key(name: Optional[str]) -> str:
    """One normalizer for account identity (shared by enrichment, dedup,
    dashboard). Lowercase, strip legal suffixes, punctuation and articles."""
    s = (name or '').lower().strip()
    s = re.sub(r'[®™©]', '', s)
    s = re.sub(r'^(the|a|an)\s+', '', s)
    s = s.replace('&', ' and ')
    changed = True
    while changed and s:
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)].rstrip(' ,.')
                changed = True
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s
