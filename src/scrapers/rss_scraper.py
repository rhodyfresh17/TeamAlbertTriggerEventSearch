"""RSS feed scraper for business news and PR wires."""

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from email.utils import parsedate_to_datetime
from html.entities import name2codepoint
from requests.exceptions import Timeout, ReadTimeout, ConnectTimeout

from .base import BaseScraper, finance_leader_hire_kind
from ..models import TriggerEvent, EventType, EventSource


# Seconds to wait before the single retry of a timed-out feed. Was 60s: with
# 45 feeds and 69 Google News queries in one GitHub Actions job under
# timeout-minutes: 15, one hung feed cost 30s + 60s + 30s of the budget
# (review 2026-09-08). A feed that is still down 15s later is reported as
# an error and the run moves on.
RETRY_SLEEP_SECONDS = 15


# Standard URIs for common RSS namespace prefixes. Many feeds use these
# prefixes (esp. media:) in element tags but forget to declare xmlns:PREFIX
# on the root <rss> element, which makes Python's strict xml.etree parser
# throw "unbound prefix". We inject the missing declarations before parsing.
_KNOWN_NS_URIS = {
    'media':   'http://search.yahoo.com/mrss/',
    'content': 'http://purl.org/rss/1.0/modules/content/',
    'dc':      'http://purl.org/dc/elements/1.1/',
    'atom':    'http://www.w3.org/2005/Atom',
    'wfw':     'http://wellformedweb.org/CommentAPI/',
    'sy':      'http://purl.org/rss/1.0/modules/syndication/',
    'slash':   'http://purl.org/rss/1.0/modules/slash/',
    'georss':  'http://www.georss.org/georss',
    'geo':     'http://www.w3.org/2003/01/geo/wgs84_pos#',
    'gd':      'http://schemas.google.com/g/2005',
    'thr':     'http://purl.org/syndication/thread/1.0',
    'itunes':  'http://www.itunes.com/dtds/podcast-1.0.dtd',
}


def sanitize_feed_xml(raw: bytes) -> bytes:
    """Inject any namespace prefixes that are USED in the feed but not
    DECLARED on the root element, so strict xml.etree parsing doesn't fail
    with 'unbound prefix'. Returns the raw bytes unchanged if nothing needs
    fixing or the root <rss>/<feed> tag can't be located.

    This is a targeted fix for the common real-world bug where a feed emits
    e.g. <media:content ...> without xmlns:media on the root (Chronicle of
    Philanthropy and many WordPress feeds do this)."""
    try:
        text = raw.decode('utf-8', errors='replace')
    except Exception:
        return raw

    # Prefixes actually used in element tags: <media:content>, <dc:creator>...
    used = set(re.findall(r'<([A-Za-z][\w-]*):', text))
    # Prefixes already declared anywhere in the doc
    declared = set(re.findall(r'xmlns:([A-Za-z][\w-]*)\s*=', text))
    missing = [p for p in used if p not in declared]
    if not missing:
        return raw

    # Find the opening root tag (<rss ...> or <feed ...>) to inject into
    m = re.search(r'<(rss|feed)\b[^>]*?>', text)
    if not m:
        return raw
    root_tag = m.group(0)

    # Build xmlns declarations for each missing prefix (known URI, or a
    # harmless placeholder so parsing succeeds — we don't consume these tags)
    injections = ''.join(
        f' xmlns:{p}="{_KNOWN_NS_URIS.get(p, f"urn:ns:{p}")}"'
        for p in missing
    )
    # Insert right before the closing '>' of the root tag
    patched_root = root_tag[:-1] + injections + '>'
    text = text.replace(root_tag, patched_root, 1)
    return text.encode('utf-8')


# Other mistakes publishers ship that a strict XML parser rejects outright: a
# bare "&" (typically inside an image URL, "?w=457&quality=82"), an HTML-only
# entity XML does not define (&nbsp;, &rsquo;), and control characters XML 1.0
# forbids. repair_feed_xml fixes those plus the undeclared namespace prefixes
# above. It runs ONLY after a strict parse has failed, so a well-formed feed is
# never rewritten, and it copies CDATA sections and comments untouched — a
# bare "&" is legal inside them.
_XML_PREDEFINED_ENTITIES = {b'amp', b'lt', b'gt', b'quot', b'apos'}
_UNTOUCHABLE_RE = re.compile(rb'(<!\[CDATA\[.*?\]\]>|<!--.*?-->)', re.S)
_NAMED_ENTITY_RE = re.compile(rb'&([A-Za-z][A-Za-z0-9]*);')
_BARE_AMP_RE = re.compile(rb'&(?!(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z_:][\w.:-]*);)')
_XML_ILLEGAL_CHARS_RE = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _named_entity(match) -> bytes:
    name = match.group(1)
    if name in _XML_PREDEFINED_ENTITIES:
        return match.group(0)
    codepoint = name2codepoint.get(name.decode('ascii'))
    if codepoint:
        return b'&#%d;' % codepoint           # &nbsp; → &#160;
    return b'&amp;' + name + b';'             # not an entity anywhere: keep it as text


def _repair_markup(segment: bytes) -> bytes:
    segment = _XML_ILLEGAL_CHARS_RE.sub(b'', segment)
    segment = _NAMED_ENTITY_RE.sub(_named_entity, segment)
    return _BARE_AMP_RE.sub(b'&amp;', segment)


def repair_feed_xml(raw: bytes) -> bytes:
    """Best-effort repair of a feed the strict parser rejected: declare
    missing namespace prefixes (sanitize_feed_xml), then — outside CDATA
    sections and comments — drop XML-forbidden control characters, turn
    HTML-only named entities into numeric references, and escape every "&"
    that does not start a valid reference. Works on bytes with ASCII-only
    edits, so the feed's declared encoding is respected."""
    raw = sanitize_feed_xml(raw)
    # split() with one capture group alternates: markup, untouchable, markup…
    parts = _UNTOUCHABLE_RE.split(raw)
    return b''.join(part if i % 2 else _repair_markup(part) for i, part in enumerate(parts))


def strip_html(text: str) -> str:
    """Remove HTML tags and clean up text."""
    if not text:
        return ""
    # Remove HTML tags
    clean = re.sub(r'<[^>]+>', '', text)
    # Decode common HTML entities
    clean = clean.replace('&nbsp;', ' ')
    clean = clean.replace('&amp;', '&')
    clean = clean.replace('&lt;', '<')
    clean = clean.replace('&gt;', '>')
    clean = clean.replace('&quot;', '"')
    clean = clean.replace('&#39;', "'")
    # Clean up whitespace
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


class RSSScraper(BaseScraper):
    """Scraper for RSS feeds from PR wires and news sources."""

    SOURCE_MAPPING = {
        'business wire': EventSource.BUSINESS_WIRE,
        'pr newswire': EventSource.PR_NEWSWIRE,
        'globe newswire': EventSource.GLOBE_NEWSWIRE,
        'globenewswire': EventSource.GLOBE_NEWSWIRE,
        'wired': EventSource.OTHER,
        'fox business': EventSource.OTHER,
        'cnbc': EventSource.OTHER,
        # PRWeb (Cision's small-business wire, added 2026-09-08) deliberately
        # stays OTHER: no wire relevance boost, and pipeline.sources gives it
        # its own 'PRWeb' label instead of folding it into PR Newswire.
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.feeds = config.get('sources', {}).get('rss_feeds', [])
        self.source_statuses = []  # Track status of each feed

    def scrape(self) -> List[TriggerEvent]:
        """Scrape all configured RSS feeds."""
        events = []
        self.source_statuses = []  # Reset statuses

        for feed_config in self.feeds:
            if not feed_config.get('enabled', True):
                continue

            feed_name = feed_config.get('name', 'Unknown')
            feed_url = feed_config.get('url')

            if not feed_url:
                continue

            feed_events, error_msg, items_fetched = self._scrape_feed(feed_url, feed_name, feed_config)
            events.extend(feed_events)

            # items_fetched = entries parsed from the feed XML, before any
            # territory/content gate (v2 Phase 2, 2026-09-07) — so the
            # dashboard can tell "feed returned 0" from "all filtered".
            self.source_statuses.append({
                'source_name': feed_name,
                'source_type': 'rss_feed',
                'status': 'error' if error_msg else 'success',
                'error_message': error_msg,
                'events_found': len(feed_events),
                'items_fetched': items_fetched,
                'filtered_out': max(items_fetched - len(feed_events), 0),
            })

            self.delay_request()

        return events

    def _scrape_feed(self, url: str, feed_name: str,
                     feed_config: Optional[Dict[str, Any]] = None,
                     retry_count: int = 0) -> tuple:
        """Scrape a single RSS feed with retry on timeout.

        feed_config is the rss_feeds entry (v2 Phase 3, 2026-09-08): its
        optional `default_region` is handed to _process_entry.

        Returns: (events, error_message, items_fetched) - error_message is
        None on success; items_fetched is the number of feed entries parsed
        (0 on any error path).
        """
        events = []
        max_retries = 1  # Retry once on timeout
        error_msg = None
        items_fetched = 0
        default_region = (feed_config or {}).get('default_region')

        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            # Parse XML strictly first. A feed the strict parser rejects is
            # repaired once (repair_feed_xml: undeclared namespace prefixes,
            # a bare "&", HTML-only entities, forbidden control characters)
            # and parsed again; if that also fails, the second error is the
            # feed's error for this run.
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as pe:
                root = ET.fromstring(repair_feed_xml(response.content))
                print(f"  - {feed_name}: repaired malformed feed XML ({pe})")

            # Handle both RSS and Atom feeds
            items = root.findall('.//item')  # RSS
            if not items:
                # Try Atom format
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                items = root.findall('.//atom:entry', ns)
                if not items:
                    items = root.findall('.//{http://www.w3.org/2005/Atom}entry')

            items_fetched = len(items)
            for item in items:
                event = self._process_entry(item, feed_name, default_region)
                if event:
                    events.append(event)

        except (Timeout, ReadTimeout, ConnectTimeout) as e:
            if retry_count < max_retries:
                print(f"Timeout on {feed_name}, waiting {RETRY_SLEEP_SECONDS}s and retrying...")
                time.sleep(RETRY_SLEEP_SECONDS)
                return self._scrape_feed(url, feed_name, feed_config, retry_count + 1)
            else:
                error_msg = f"Timeout: {e}"
                print(f"Error parsing feed {feed_name}: {e} (after retry)")

        except Exception as e:
            error_msg = str(e)[:200]
            print(f"Error parsing feed {feed_name}: {e}")

        if error_msg:
            items_fetched = 0
        return events, error_msg, items_fetched

    def _process_entry(self, item: ET.Element, feed_name: str,
                       default_region: Optional[str] = None) -> Optional[TriggerEvent]:
        """Process a single feed entry.

        default_region (rss_feeds entry, optional): the feed's home state,
        an ADMISSION-ONLY hint used when the text places the story nowhere
        — see the default block below.
        """
        # Extract fields (handle both RSS and Atom)
        title = self._get_text(item, 'title') or ''
        link = self._get_text(item, 'link') or self._get_attr(item, 'link', 'href') or ''
        raw_summary = self._get_text(item, 'description') or self._get_text(item, 'summary') or ''

        # Handle Atom content
        if not raw_summary:
            content = item.find('{http://www.w3.org/2005/Atom}content')
            if content is not None and content.text:
                raw_summary = content.text

        # Strip HTML from summary
        summary = strip_html(raw_summary)

        # Combine title and summary for analysis
        full_text = f"{title} {summary}"

        # STEP 1: Check if ANY dateline location is in our territory (HIGHEST PRIORITY)
        # If PR is from our territory, we want to see it - period.
        # Handles multiple locations like "NEW YORK and ARLINGTON, Va."
        dateline_locations = self.extract_dateline_locations(full_text)
        dateline_in_territory = False
        dateline_matched_location = None

        for dateline_city, dateline_state in dateline_locations:
            if dateline_city and dateline_city in self.cities:
                dateline_in_territory = True
                dateline_matched_location = dateline_city
                break
            if dateline_state and dateline_state in self.regions:
                dateline_in_territory = True
                dateline_matched_location = dateline_state
                break

        # Check territory match in body text
        in_territory, matched_regions = self.matches_territory(full_text)

        # A dateline that resolves to a state/province outside the territory
        # is KNOWN out, not unknown ("DENVER, Colo.", "CALGARY, Alberta"):
        # it vetoes both the feed default below and the unknown-territory
        # admission further down.
        dateline_known_out = any(
            state and state not in self.regions
            for _city, state in dateline_locations
        )
        # A dateline that names ANY place we did not match — an out-of-
        # territory state ("TOPEKA, Kan."), a province, or a bare foreign
        # city ("MANILA") — places the story somewhere, so a feed default
        # must not stand in for it (review 2026-09-08).
        dateline_elsewhere = dateline_known_out or (
            bool(dateline_locations) and not dateline_in_territory)

        # Feed-level default_region (v2 Phase 3, research 2026-09-08): a
        # regional business journal writes "Bedford-based" and "CT", never
        # "New Hampshire" / "Connecticut", so its own-state stories carry no
        # state token — NH Business Review's "Hometown Financial Group to
        # acquire Primary Bank" (a Bedford NH bank) died at the territory
        # gate. When the text places the story NOWHERE (no in-territory hit,
        # no dateline elsewhere, no excluded location) the feed's home
        # region ADMITS the story past the territory gate — and does nothing
        # else (review 2026-09-08): matched_regions stays empty, so the
        # default earns no relevance points and never reaches enrichment's
        # oracle anchor (_event_state_hint) or the dashboard's HQ column as
        # if the text had named the state; a real trigger is still required
        # (no PE-backed stable target on a defaulted region); the industry /
        # public hard blocks still apply; the enrichment HQ gate re-verifies.
        region_default = (default_region or '').strip().lower()
        region_defaulted = bool(
            not in_territory and region_default and region_default in self.regions
            and not dateline_elsewhere and not self.is_excluded_location(full_text)
        )

        # Check target company
        matches_company, company_name = self.matches_target_company(full_text)

        # STEP 2: Detect event type (trigger events like M&A, CFO hire, funding)
        # — the hire type is decided from the title (review 2026-09-08)
        event_type = self.detect_event_type(full_text, title=title)

        # Track recommendation reasoning for stable targets
        recommendation_reasoning = None

        # Always hard-block excluded industries and public companies (the
        # "Fortune 500" indicators count in the title only)
        matches_target_industry, matches_excluded = self.matches_industry(full_text)
        if matches_excluded:
            return None
        if (self.is_public_company(full_text, title=title)
                or self._has_stock_ticker_category(item)):
            return None

        # STEP 3: Apply territory + trigger filtering
        if dateline_in_territory:
            # Dateline is in territory — require a trigger event OR known target company
            if not event_type and not matches_company:
                return None
            if not event_type:
                event_type = EventType.STABLE_TARGET
                extracted_company = company_name or self.extract_company_name(full_text)
                matched_industries = self.get_matched_industries(full_text)
                location_info = dateline_matched_location or "territory"
                recommendation_reasoning = f"PR from {location_info.title()}"
                if extracted_company:
                    recommendation_reasoning = f"Company: {extracted_company} | {recommendation_reasoning}"
                if matched_industries:
                    recommendation_reasoning += f" | Industry: {', '.join(matched_industries[:2])}"
        else:
            # NOT in territory by dateline - apply stricter filtering

            # Skip excluded (out-of-territory) locations — but ONLY when the
            # body carries no in-territory city/state signal. An in-territory
            # match always wins (v2 ordering contract, see
            # base.py::matches_territory); a false admit is caught by the
            # enrichment HQ gate, a false reject here is lost forever.
            if not in_territory and self.is_excluded_location(full_text):
                return None

            # If no trigger event, skip. in_territory here is the REAL text
            # match — a feed default never makes a PE-backed "portfolio
            # company opens office" story a stable target (review 2026-09-08).
            if not event_type:
                is_pe_backed = self._is_pe_backed(full_text)
                if not (matches_company or (is_pe_backed and in_territory)):
                    return None
                event_type = EventType.STABLE_TARGET

            # Must match territory OR be a named target company
            is_pe_backed = self._is_pe_backed(full_text)
            is_ma_event = event_type == EventType.MERGER_ACQUISITION

            if self.require_territory_match:
                if not (in_territory or matches_company or region_defaulted):
                    # UNKNOWN territory — no dateline, no in-territory word, no
                    # excluded location (the excluded-location check above
                    # already returned) — used to be a hard reject, which
                    # killed every GlobeNewswire CFO hire: GlobeNewswire RSS
                    # carries no datelines at all (research 2026-09-08). Admit
                    # the finance-leader hires ONLY — a hire whose TITLE names
                    # a finance-leader seat in a hire shape — and let
                    # enrichment verify territory: a false admit costs one
                    # local-LLM pass, a false reject is lost forever. A
                    # dateline that resolves to an out-of-territory state is
                    # KNOWN out, not unknown. M&A / funding are never admitted
                    # this way (volume).
                    if dateline_known_out or not self._is_finance_leader_hire(event_type, title):
                        return None

        # Get industry match info (may not be set for dateline-in-territory)
        matched_industries = self.get_matched_industries(full_text)
        matches_target_industry = len(matched_industries) > 0

        # Calculate relevance
        relevance = self.calculate_relevance_score(
            event_type,
            matched_regions,
            matches_target_industry,
            matches_company
        )

        # Boost relevance for dateline matches
        if dateline_in_territory:
            relevance = min(relevance + 20, 100)

        # Determine source
        source = self._determine_source(feed_name)

        # Boost relevance for PR wire sources (direct company announcements)
        if source == EventSource.PR_NEWSWIRE:
            relevance = min(relevance + 15, 100)
        elif source == EventSource.BUSINESS_WIRE:
            relevance = min(relevance + 10, 100)
        elif source == EventSource.GLOBE_NEWSWIRE:
            relevance = min(relevance + 10, 100)

        # Parse published date
        published = self._parse_date(item)

        # Extract company name if not already done
        extracted_company = company_name or self.extract_company_name(full_text)
        person_name, person_title = self.extract_person_info(full_text)

        # Get matched keywords
        matched_keywords = self._get_matched_keywords(full_text, event_type)

        # Build description with reasoning for territory-matched stable targets
        is_stable_target = (event_type == EventType.STABLE_TARGET)
        if is_stable_target and recommendation_reasoning:
            description = f"📋 RECOMMENDATION: {recommendation_reasoning}\n\n{summary[:400] if summary else ''}"
        else:
            description = summary[:500] if summary else None

        return TriggerEvent(
            id=self.generate_event_id(link, title),
            title=title,
            event_type=event_type,
            source=source,
            source_name=feed_name,
            url=link,
            published_date=published,
            company_name=extracted_company,
            description=description,
            person_name=person_name,
            person_title=person_title,
            matched_keywords=matched_keywords,
            matched_regions=matched_regions,
            relevance_score=relevance
        )

    def _has_stock_ticker_category(self, item: ET.Element) -> bool:
        """GlobeNewswire tags listed issuers with
        <category domain=".../rss/stock">Nasdaq:CARG</category> and keeps the
        ticker OUT of the title and description, so the "(NASDAQ:" substring
        indicators never see it (research 2026-09-08: 9 of the 20 items in the
        CFO keyword feed, every one a listed company). Honour
        exclude_public_companies here too — otherwise the unknown-territory
        admission below would hand enrichment a stream of public-company CFO
        hires."""
        if not self.exclude_public:
            return False
        for cat in item.findall('category'):
            if '/rss/stock' in (cat.get('domain') or '').lower():
                return True
        return False

    @staticmethod
    def _is_finance_leader_hire(event_type: Optional[EventType], title: str) -> bool:
        """The events worth admitting without a known territory: a hire
        whose TITLE names a finance-leader seat in a hire shape — the same
        title-based test that typed the event (base.finance_leader_hire_kind
        on the title alone, review 2026-09-08). Never on the body alone: an
        event typed from the body's head ("… strengthens its leadership
        team" + "has named Jane Doe Controller") needs a known territory."""
        return (event_type in (EventType.CFO_HIRE, EventType.EXECUTIVE_HIRE)
                and finance_leader_hire_kind(title or '') is not None)

    def _get_text(self, elem: ET.Element, tag: str) -> Optional[str]:
        """Get text content of a child element."""
        # Try without namespace
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()

        # Try with Atom namespace
        child = elem.find(f'{{http://www.w3.org/2005/Atom}}{tag}')
        if child is not None and child.text:
            return child.text.strip()

        return None

    def _get_attr(self, elem: ET.Element, tag: str, attr: str) -> Optional[str]:
        """Get attribute of a child element."""
        child = elem.find(tag)
        if child is not None:
            return child.get(attr)

        child = elem.find(f'{{http://www.w3.org/2005/Atom}}{tag}')
        if child is not None:
            return child.get(attr)

        return None

    def _parse_date(self, item: ET.Element) -> datetime:
        """Parse the published date from a feed entry."""
        # Try different date fields
        for tag in ['pubDate', 'published', 'updated', 'date']:
            date_str = self._get_text(item, tag)
            if date_str:
                try:
                    return parsedate_to_datetime(date_str)
                except Exception:
                    try:
                        # Try ISO format
                        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    except Exception:
                        pass

        # Default to now
        return datetime.now(timezone.utc)

    def _determine_source(self, feed_name: str) -> EventSource:
        """Determine the event source from feed name."""
        feed_lower = feed_name.lower()

        for name, source in self.SOURCE_MAPPING.items():
            if name in feed_lower:
                return source

        return EventSource.OTHER

    def _is_pe_backed(self, text: str) -> bool:
        """Check if text indicates a PE-backed company or deal."""
        text_lower = text.lower()
        pe_indicators = [
            'private equity',
            'pe-backed',
            'pe backed',
            '-backed',
            'portfolio company',
            'capital partners',
            'equity partners',
            'investment partners',
            'growth equity',
            'buyout',
            'lbo',
            'leveraged buyout',
            'sponsor-backed',
            'sponsor backed',
            'add-on acquisition',
            'bolt-on acquisition',
            'platform acquisition',
            'tuck-in acquisition',
        ]
        return any(indicator in text_lower for indicator in pe_indicators)

    def _get_matched_keywords(self, text: str, event_type: EventType) -> List[str]:
        """Get list of matched keywords."""
        text_lower = text.lower()
        matched = []

        keyword_sets = {
            EventType.CFO_HIRE: self.exec_hire_keywords,
            EventType.EXECUTIVE_HIRE: self.exec_hire_keywords,
            EventType.MERGER_ACQUISITION: self.ma_keywords,
            EventType.FUNDING: self.funding_keywords,
        }

        keywords = keyword_sets.get(event_type, [])
        for kw in keywords:
            if kw in text_lower:
                matched.append(kw)

        return matched[:5]  # Limit to top 5
