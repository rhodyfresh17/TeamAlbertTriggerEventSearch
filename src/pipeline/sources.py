"""Stable, human-readable source labels for yield monitoring (no network).

WHY (2026-09-07): monitor_health measured liveness ("did the cron run") and
said "All clear" for 40 days while 24 of 39 feeds returned nothing. Yield
has to be counted PER SOURCE, and the dashboard scorecard, the health check
and the future accounts table must all bucket a row the same way — so the
bucketing lives here, once.

Two vocabularies meet in this file:
  * events rows  → `source_label(row)`   (typed `source` column when migrated,
                                          else URL-host / title heuristics)
  * source_status rows → `feed_label(...)` / `feed_matches_label(...)`
                                          (per-FEED names like "PR Newswire -
                                          Personnel Announcements" folded onto
                                          the same labels)
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlparse

# Canonical labels (keep these strings stable — they appear in alert text and
# tests, and the owner learns to recognise them).
SEC_8K = 'SEC 8-K'
SEC_FORM_D = 'SEC Form D'
SEC_IAPD = 'SEC IAPD'       # new SEC-registered advisers, scripts/ria_trigger.py (Phase 3, 2026-09-08)
ADZUNA = 'Adzuna'
GOOGLE_NEWS = 'Google News'
PR_NEWSWIRE = 'PR Newswire'
GLOBE_NEWSWIRE = 'GlobeNewswire'
BUSINESS_WIRE = 'Business Wire'
LINKEDIN = 'LinkedIn'
# Phase 3 supply feeds (2026-09-08) — one label per feed so the yield table,
# the went-quiet check and the accounts table bucket them the same way.
PRWEB = 'PRWeb'
HOMECARE_MAG = 'HomeCare Magazine'
PERFORMANCE_BROKERAGE = 'Performance Brokerage'
BODYSHOP_BUSINESS = 'BodyShop Business'
VIRGINIA_BUSINESS = 'Virginia Business'
VERMONT_BUSINESS = 'Vermont Business'
NH_BUSINESS_REVIEW = 'NH Business Review'
PROVIDENCE_BUSINESS_NEWS = 'Providence Business News'
HARTFORD_BUSINESS_JOURNAL = 'Hartford Business Journal'
OTHER = 'other'

HOST_MAX_LEN = 28   # dashboard._scorecard_src delegates here (review 2026-09-08), so the two agree by construction

# Every label a feed can be folded onto. Anything else source_label() returns
# is a bare URL host (or 'other') — one small feed that never got a canonical
# name. Review 2026-09-07: monitor_health lists those as "small feeds quiet"
# but never raises the went-quiet WARN on them alone; canonical labels carry
# the alert.
CANONICAL_LABELS = frozenset({
    SEC_8K, SEC_FORM_D, SEC_IAPD, ADZUNA, GOOGLE_NEWS, PR_NEWSWIRE, GLOBE_NEWSWIRE,
    BUSINESS_WIRE, LINKEDIN,
    PRWEB, HOMECARE_MAG, PERFORMANCE_BROKERAGE, BODYSHOP_BUSINESS,
    VIRGINIA_BUSINESS, VERMONT_BUSINESS, NH_BUSINESS_REVIEW,
    PROVIDENCE_BUSINESS_NEWS, HARTFORD_BUSINESS_JOURNAL,
})

# Typed `source` column values (SQLite events.source enum) → label.
# 'sec_edgar' is split by title (8-K vs Form D) — see source_label().
_TYPED_LABELS = {
    'sec_iapd': SEC_IAPD,       # RIA registrations (adviserinfo.sec.gov) — never an 8-K
    'adzuna': ADZUNA,
    'google_news': GOOGLE_NEWS,
    'pr_newswire': PR_NEWSWIRE,
    'globe_newswire': GLOBE_NEWSWIRE,
    'business_wire': BUSINESS_WIRE,
    'linkedin': LINKEDIN,
}

# URL-host substrings → label. Order matters only where hosts overlap.
_HOST_LABELS = (
    ('adzuna', ADZUNA),
    ('news.google.', GOOGLE_NEWS),
    ('google.', GOOGLE_NEWS),          # Google Alerts + Google Jobs both redirect via google.com/url
    ('prnewswire', PR_NEWSWIRE),
    ('globenewswire', GLOBE_NEWSWIRE),
    ('businesswire', BUSINESS_WIRE),
    ('linkedin', LINKEDIN),
    # Phase 3 feeds (2026-09-08): the release/article hosts
    ('prweb', PRWEB),
    ('homecaremag', HOMECARE_MAG),
    ('performancebrokerageservices', PERFORMANCE_BROKERAGE),
    ('bodyshopbusiness', BODYSHOP_BUSINESS),
    ('virginiabusiness', VIRGINIA_BUSINESS),
    ('vermontbiz', VERMONT_BUSINESS),
    ('nhbr.com', NH_BUSINESS_REVIEW),
    ('pbn.com', PROVIDENCE_BUSINESS_NEWS),
    ('hartfordbusiness', HARTFORD_BUSINESS_JOURNAL),
)

# Feed-name substrings (source_status.source_name, lowercase) → label.
_FEED_LABELS = (
    ('sec form d', SEC_FORM_D),
    ('form d', SEC_FORM_D),
    ('sec 8-k', SEC_8K),
    ('sec edgar', SEC_8K),
    ('iapd', SEC_IAPD),                # "SEC IAPD …": the RIA trigger's own bucket (2026-09-08)
    ('adzuna', ADZUNA),
    ('google', GOOGLE_NEWS),           # "Google News", "Google Jobs", "Google Alert - …"
    ('pr newswire', PR_NEWSWIRE),
    ('globe newswire', GLOBE_NEWSWIRE),
    ('globenewswire', GLOBE_NEWSWIRE),
    ('business wire', BUSINESS_WIRE),
    ('linkedin', LINKEDIN),
    # Phase 3 feeds (2026-09-08). "Globe Newswire - CFO Keyword" and the
    # other GlobeNewswire keyword/subject feeds already fold onto
    # GLOBE_NEWSWIRE via the entries above.
    ('prweb', PRWEB),
    ('homecare magazine', HOMECARE_MAG),
    ('performance brokerage', PERFORMANCE_BROKERAGE),
    ('bodyshop business', BODYSHOP_BUSINESS),
    ('virginia business', VIRGINIA_BUSINESS),
    ('vermont business', VERMONT_BUSINESS),
    ('nh business review', NH_BUSINESS_REVIEW),
    ('providence business news', PROVIDENCE_BUSINESS_NEWS),
    ('hartford business journal', HARTFORD_BUSINESS_JOURNAL),
)

# Words too generic to identify a feed on their own when matching a feed name
# against a URL host ("Business Insider" must match businessinsider.com by its
# full name, not because "business" appears in foxbusiness.com).
_GENERIC_TOKENS = {
    'news', 'the', 'daily', 'business', 'latest', 'insider', 'journal',
    'management', 'magazine', 'times', 'report', 'reports', 'and', 'alert',
    'alerts', 'jobs', 'wire', 'newswire', 'com', 'www', 'industry', 'private',
}


def _host(url: Optional[str]) -> str:
    try:
        return (urlparse(url or '').netloc or '').lower().replace('www.', '')
    except (ValueError, AttributeError):
        return ''


def _sec_label_from_title(title: Optional[str]) -> str:
    """SEC rows carry a stable title prefix ('SEC Form D (Private Capital
    Raise) — X' / 'SEC 8-K Item 5.02 (…) — X'); Form D is the minority, so
    anything not clearly Form D counts as 8-K."""
    return SEC_FORM_D if 'form d' in (title or '').lower() else SEC_8K


def source_label(row: Any) -> str:
    """Bucket an events row into a stable human label.

    Precedence: the typed `source` column (scrape-owned enum, once
    supabase/migrations/002 has run) → URL host → 'other'. 'sec_edgar' and
    sec.gov hosts split 8-K vs Form D by title; adviserinfo.sec.gov (RIA
    registrations) is 'SEC IAPD'. Unknown hosts are returned
    as the bare host (no www., ≤ HOST_MAX_LEN chars) so a feed that never
    got a canonical name still shows up by itself in the yield table.
    """
    get = row.get if hasattr(row, 'get') else (lambda k, d=None: getattr(row, k, d))
    typed = (get('source') or '')
    typed = str(typed).strip().lower() if typed else ''
    title = get('title') or ''
    url = get('source_url') or get('url') or ''

    if typed == 'sec_edgar':
        return _sec_label_from_title(title)
    if typed in _TYPED_LABELS:
        return _TYPED_LABELS[typed]

    host = _host(url)
    # adviserinfo.sec.gov is an sec.gov host too: the RIA rule must run
    # BEFORE the generic 8-K / Form D split, or an untyped new-adviser row
    # would bucket as SEC 8-K (2026-09-08).
    if 'adviserinfo.sec.gov' in host:
        return SEC_IAPD
    if 'sec.gov' in host:
        return _sec_label_from_title(title)
    for needle, label in _HOST_LABELS:
        if needle in host:
            return label
    if typed == 'other' or not typed:
        return host[:HOST_MAX_LEN] or OTHER
    return host[:HOST_MAX_LEN] or typed


def is_canonical_label(label: Optional[str]) -> bool:
    """True for the stable named buckets; False for bare hosts and 'other'."""
    return label in CANONICAL_LABELS


def feed_label(source_name: Optional[str], source_type: Optional[str] = None) -> str:
    """Fold a source_status feed name onto the same label vocabulary.
    Unknown feeds keep their own name (that IS the label the owner knows)."""
    name = (source_name or '').strip()
    low = name.lower()
    if (source_type or '').lower() == 'google_news':
        return GOOGLE_NEWS
    for needle, label in _FEED_LABELS:
        if needle in low:
            return label
    return name or OTHER


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def feed_matches_label(source_name: Optional[str], label: Optional[str],
                       source_type: Optional[str] = None) -> bool:
    """Does this source_status feed feed the given events label?

    Canonical labels compare exactly. Otherwise the events label is a URL
    host ('vcnewsdaily.com', 'news.crunchbase.com') and the feed name is
    prose ('VC News Daily', 'Crunchbase News'): match when the squashed
    name is contained in the squashed host (or vice versa), or when any
    non-generic token of the name (≥ 3 chars) appears in the host.
    """
    if not source_name or not label:
        return False
    canon = feed_label(source_name, source_type)
    if canon == label:
        return True
    if canon != (source_name or '').strip():
        return False          # feed is canonical but the label is a different bucket
    host = label.lower().replace('www.', '')
    host_sq = _norm(host)
    name_sq = _norm(source_name)
    if not host_sq or not name_sq:
        return False
    host_core = _norm(host.rsplit('.', 1)[0]) if '.' in host else host_sq
    if name_sq in host_core or host_core in name_sq:
        return True
    for tok in re.findall(r'[a-z0-9]+', source_name.lower()):
        if len(tok) >= 3 and tok not in _GENERIC_TOKENS and tok in host_core:
            return True
    return False


_FINANCE_LEADER_TYPES = {'cfo_hire', 'finance_seat_open'}


def _hashtag_list(raw: Any) -> list:
    """hashtags arrive as a list (Supabase JSONB), a JSON string (SQLite /
    CSV exports), a bare '#A #B' string, or None."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(h) for h in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith('['):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(h) for h in parsed]
            except ValueError:
                pass
        return [t for t in re.split(r'[\s,]+', s) if t]
    return []


def finance_leader_family(row: Any) -> bool:
    """The best trigger family: a company that just got, or is hiring, a
    finance leader. cfo_hire / finance_seat_open by type, or a Controller
    hire tagged #NewController on any other event type."""
    get = row.get if hasattr(row, 'get') else (lambda k, d=None: getattr(row, k, d))
    etype = str(get('event_type') or '').strip().lower()
    if etype in _FINANCE_LEADER_TYPES:
        return True
    tags = {h.strip().lower() for h in _hashtag_list(get('hashtags'))}
    return '#newcontroller' in tags
