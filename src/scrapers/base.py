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


def _compile_whole_word(terms: List[str]) -> List[tuple]:
    """[(term, compiled_regex)] — whole-word, case-insensitive. Lookarounds
    instead of \b so terms ending in punctuation ("St. Louis") still work."""
    out = []
    for term in terms:
        term = (term or '').strip()
        if not term:
            continue
        out.append((term, re.compile(r'(?<!\w)' + re.escape(term) + r'(?!\w)',
                                     re.IGNORECASE)))
    return out


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

        # Load keywords
        self.keywords = config.get('keywords', {})
        self.exec_hire_keywords = [k.lower() for k in self.keywords.get('executive_hires', [])]
        self.ma_keywords = [k.lower() for k in self.keywords.get('mergers_acquisitions', [])]
        self.funding_keywords = [k.lower() for k in self.keywords.get('funding_events', [])]

        # Load company filters (for mid-market private companies)
        self.company_filters = self.territory.get('company_filters', {})
        self.exclude_public = self.company_filters.get('exclude_public_companies', True)
        self.public_indicators = [
            i.lower() for i in (self.company_filters.get('public_company_indicators') or [])
        ]
        self.excluded_public_companies = [
            c.lower() for c in (self.company_filters.get('excluded_public_companies') or [])
        ]
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

    def detect_event_type(self, text: str) -> Optional[EventType]:
        """Detect the type of trigger event from text."""
        text_lower = text.lower()

        # First check if this is excluded content (concerts, sports, etc.)
        if self.is_excluded_content(text):
            return None

        # Check for CFO specifically first
        cfo_patterns = ['cfo', 'chief financial officer']
        if any(pattern in text_lower for pattern in cfo_patterns):
            # Make sure it's about hiring, not just mentioning CFO
            hire_indicators = ['named', 'appointed', 'hired', 'joins', 'new cfo', 'promoted', 'announces']
            if any(ind in text_lower for ind in hire_indicators):
                return EventType.CFO_HIRE

        # Check for executive hires
        if any(kw in text_lower for kw in self.exec_hire_keywords):
            hire_indicators = ['named', 'appointed', 'hired', 'joins', 'promoted', 'announces', 'welcomes']
            if any(ind in text_lower for ind in hire_indicators):
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

        # Check exclusions first
        for excluded in self.excluded_industries:
            if excluded in text_lower:
                return False, True

        # Check target industries
        for industry in self.industries:
            if industry in text_lower:
                return True, False

        return False, False

    def is_public_company(self, text: str) -> bool:
        """Check if text indicates a public company (to exclude)."""
        if not self.exclude_public:
            return False

        text_lower = text.lower()

        # Check for known large public companies by name
        for company in self.excluded_public_companies:
            if company in text_lower:
                return True

        # Check for public company indicators
        for indicator in self.public_indicators:
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
