"""Phase 4 slice C4 (2026-09-08): the v1 orphans are gone and stay gone.

Deleted, with the evidence that nothing live used them:
  src/enrichment.py               Apollo/ZoomInfo CompanyEnricher — main.py only ever printed
                                  its enabled/disabled line; alerts were sent "no company
                                  verification" since v1.
  import_leads.py                 no importer/caller; itself imported sync_db (also deleted)
  sheets_sync.py                  zero references anywhere
  sync_db.py                      referenced only by import_leads.py
  cleanup_legacy_events.py        no caller; the monitor's "Cleanup dry-run" check was retired
                                  2026-09-07 (monitor_health.py comment)
  src/scrapers/bing_scraper.py    BingNewsScraper — disabled, needs a paid key
  src/scrapers/finsmes_scraper.py FinSMEsScraper — permanent 403
  src/scrapers/job_scraper.py     JobScraper ("Google Jobs") — decided by data on 2026-09-08:
                                  0 verified accounts attributable to it in the 28-day window
                                  (Supabase: 70 Google-URL rows, at most 2 JobScraper-shaped —
                                  its items carry no description — both not_fit); the local run
                                  that day "found 10" and saved 0 (age/dedup dropped them all).
                                  Adzuna covers open finance seats with structured company names.

The config sections only those modules read (job_search, sources.bing_news,
sources.finsmes, the four numeric company_filters size keys) went with them.
"""
import ast
import importlib
import importlib.util
import os
import re
import sys
import tempfile

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DELETED_FILES = (
    'src/enrichment.py',
    'import_leads.py',
    'sheets_sync.py',
    'sync_db.py',
    'cleanup_legacy_events.py',
    'src/scrapers/bing_scraper.py',
    'src/scrapers/finsmes_scraper.py',
    'src/scrapers/job_scraper.py',
)
DELETED_MODULES = (
    'src.enrichment',
    'import_leads',
    'sheets_sync',
    'sync_db',
    'cleanup_legacy_events',
    'src.scrapers.bing_scraper',
    'src.scrapers.finsmes_scraper',
    'src.scrapers.job_scraper',
)
DELETED_CLASSES = ('CompanyEnricher', 'JobScraper', 'BingNewsScraper', 'FinSMEsScraper')

# The live scrapers, in registration order (src/main.py TriggerEventMonitor.__init__).
LIVE_SCRAPERS = ['RSSScraper', 'GoogleNewsScraper', 'SECScraper', 'FormDScraper', 'AdzunaScraper']

# Secrets no live module reads any more (enrichment runs on the Mac, never in CI).
DEAD_SECRETS = ('APOLLO', 'ANTHROPIC', 'TAVILY')

ORPHANED_CONFIG_TOP_LEVEL = ('job_search',)
ORPHANED_CONFIG_SOURCES = ('bing_news', 'finsmes')
ORPHANED_COMPANY_FILTER_KEYS = ('min_employees', 'max_employees',
                                'min_revenue_millions', 'max_revenue_millions')


def _path(rel):
    return os.path.join(REPO, rel)


def _read(rel):
    with open(_path(rel), encoding='utf-8') as f:
        return f.read()


# ── 1. The files are gone and cannot come back through the import system ──────

def test_deleted_files_are_gone():
    present = [rel for rel in DELETED_FILES if os.path.exists(_path(rel))]
    assert present == [], f'orphaned modules resurrected: {present}'


def test_deleted_modules_are_not_importable():
    for name in DELETED_MODULES:
        sys.modules.pop(name, None)
        assert importlib.util.find_spec(name) is None, f'{name} is still importable'


def test_deleted_classes_are_not_exported_anywhere_live():
    import src.scrapers as pkg
    import src.main as main
    for cls in DELETED_CLASSES:
        assert not hasattr(pkg, cls), f'src.scrapers still exposes {cls}'
        assert not hasattr(main, cls), f'src.main still exposes {cls}'


# ── 2. src.scrapers exports exactly the live scrapers, and each resolves ──────

def test_every_scrapers_export_resolves():
    import src.scrapers as pkg
    for name in pkg.__all__:
        obj = getattr(pkg, name, None)
        assert obj is not None, f'src.scrapers.__all__ names {name} but it does not resolve'
        assert isinstance(obj, type), f'{name} is not a class'
        assert importlib.import_module(obj.__module__) is not None


def test_scrapers_exports_are_exactly_the_live_set():
    import src.scrapers as pkg
    assert sorted(pkg.__all__) == sorted(LIVE_SCRAPERS)


# ── 3. main.py registers exactly the live scrapers ───────────────────────────

def _registered_scraper_names_from_source():
    """Static read of TriggerEventMonitor.__init__ — catches a registration
    hidden behind a config flag that a runtime instantiation would miss."""
    tree = ast.parse(_read('src/main.py'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'TriggerEventMonitor':
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == '__init__':
                    for stmt in ast.walk(fn):
                        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                                and isinstance(stmt.targets[0], ast.Attribute)
                                and stmt.targets[0].attr == 'scrapers'
                                and isinstance(stmt.value, ast.List)):
                            names = []
                            for elt in stmt.value.elts:
                                assert isinstance(elt, ast.Call), ast.dump(elt)
                                names.append(elt.func.id if isinstance(elt.func, ast.Name) else elt.func.attr)
                            return names
    raise AssertionError('self.scrapers = [...] not found in TriggerEventMonitor.__init__')


def test_main_source_registers_exactly_the_live_scrapers():
    assert _registered_scraper_names_from_source() == LIVE_SCRAPERS


def test_main_instantiates_exactly_the_live_scrapers_and_no_enricher():
    from src.main import TriggerEventMonitor
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {
            'scraper': {'database': os.path.join(tmp, 'scratch.db'),
                        'max_age_hours': 72, 'timeout': 5, 'request_delay': 0},
            'territory': {'regions': [], 'cities': [], 'industries': [],
                          'excluded_industries': [],
                          'company_filters': {'exclude_public_companies': False}},
            'keywords': {'executive_hires': [], 'mergers_acquisitions': [],
                         'funding_events': []},
            'alerts': {'file': {'enabled': False}, 'desktop': {'enabled': False},
                       'email': {'enabled': False}, 'slack': {'enabled': False}},
            'sources': {'rss_feeds': [], 'google_news': {'enabled': False}},
            'adzuna': {'enabled': False},
        }
        config_path = os.path.join(tmp, 'config.yaml')
        with open(config_path, 'w') as f:
            yaml.safe_dump(cfg, f)
        monitor = TriggerEventMonitor(config_path)
        assert [type(s).__name__ for s in monitor.scrapers] == LIVE_SCRAPERS
        assert not hasattr(monitor, 'enricher'), 'scrape-time CompanyEnricher is gone'


# ── 4. The workflow wires no dead secrets ────────────────────────────────────

def _workflow():
    return yaml.safe_load(_read('.github/workflows/scraper.yml'))


def _env_keys_and_secret_refs(node, env_keys, refs):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == 'env' and isinstance(v, dict):
                env_keys.update(str(x) for x in v)
            _env_keys_and_secret_refs(v, env_keys, refs)
    elif isinstance(node, list):
        for v in node:
            _env_keys_and_secret_refs(v, env_keys, refs)
    elif isinstance(node, str):
        refs.update(re.findall(r'secrets\.([A-Za-z0-9_]+)', node))


def test_workflow_references_no_dead_secrets():
    env_keys, refs = set(), set()
    _env_keys_and_secret_refs(_workflow(), env_keys, refs)
    assert refs, 'expected at least one ${{ secrets.* }} reference in scraper.yml'
    for dead in DEAD_SECRETS:
        hits = sorted(x for x in env_keys | refs if dead in x.upper())
        assert hits == [], f'scraper.yml still wires {dead}: {hits}'
    # And nothing slipped in outside YAML structure (e.g. inline shell).
    raw = _read('.github/workflows/scraper.yml')
    assert not re.search(r'(APOLLO|ANTHROPIC|TAVILY)_API_KEY', raw)


def test_workflow_calls_no_deleted_script():
    raw = _read('.github/workflows/scraper.yml')
    for rel in DELETED_FILES:
        assert os.path.basename(rel) not in raw, f'scraper.yml still calls {rel}'


# ── 5. Config carries no sections only the deleted modules read ──────────────

def _configs():
    out = [('config.example.yaml', yaml.safe_load(_read('config.example.yaml')))]
    if os.path.exists(_path('config.yaml')):
        out.append(('config.yaml', yaml.safe_load(_read('config.yaml'))))
    return out


def test_config_has_no_orphaned_sections():
    for name, cfg in _configs():
        for key in ORPHANED_CONFIG_TOP_LEVEL:
            assert key not in cfg, f'{name}: orphaned section {key!r}'
        for key in ORPHANED_CONFIG_SOURCES:
            assert key not in cfg['sources'], f'{name}: orphaned sources.{key}'
        cf = cfg['territory']['company_filters']
        for key in ORPHANED_COMPANY_FILTER_KEYS:
            assert key not in cf, f'{name}: orphaned company_filters.{key} (Apollo-era)'
        # The live parts of company_filters (src/scrapers/base.py) are untouched.
        assert cf['excluded_public_companies'] and cf['public_company_indicators']
        assert cf['target_size_indicators']


# ── 6. No live source references a deleted module ────────────────────────────

_SCAN_SUFFIXES = ('.py', '.sh', '.yml', '.yaml')
_SKIP_DIRS = {'venv', '.git', '__pycache__', '.pytest_cache', 'logs', 'alerts', 'state', 'node_modules'}
_IMPORT_RE = re.compile(
    r'^\s*(?:from\s+(?:src\.)?(?:scrapers\.)?|import\s+(?:src\.)?(?:scrapers\.)?)'
    r'(enrichment|import_leads|sheets_sync|sync_db|cleanup_legacy_events|'
    r'bing_scraper|finsmes_scraper|job_scraper)\b', re.M)
_RELATIVE_IMPORT_RE = re.compile(
    r'^\s*from\s+\.+(?:scrapers\.)?(enrichment|bing_scraper|finsmes_scraper|job_scraper)\b', re.M)
_SCRIPT_CALL_RE = re.compile(
    r'\b(import_leads|sheets_sync|sync_db|cleanup_legacy_events)\.py\b')


def _live_source_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith('.')] \
            if os.path.relpath(root, REPO) != '.' else \
            [d for d in dirs if d not in _SKIP_DIRS and (not d.startswith('.') or d == '.github')]
        for fn in files:
            if fn.endswith(_SCAN_SUFFIXES) and '.bak' not in fn:
                yield os.path.join(root, fn)


def test_no_live_source_imports_a_deleted_module():
    offenders = []
    me = os.path.abspath(__file__)
    for path in _live_source_files():
        if os.path.abspath(path) == me:
            continue
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        rel = os.path.relpath(path, REPO)
        if path.endswith('.py'):
            for m in _IMPORT_RE.finditer(text):
                offenders.append(f'{rel}: {m.group(0).strip()}')
            for m in _RELATIVE_IMPORT_RE.finditer(text):
                offenders.append(f'{rel}: {m.group(0).strip()}')
        else:  # shell / workflow: a `python cleanup_legacy_events.py` style call
            for m in _SCRIPT_CALL_RE.finditer(text):
                offenders.append(f'{rel}: {m.group(0)}')
    assert offenders == [], 'live code still references deleted modules:\n' + '\n'.join(offenders)


# ── 7. EventSource.SEC_IAPD matches the string the RIA trigger writes ─────────

def test_event_source_sec_iapd_matches_ria_trigger():
    from src.models import EventSource
    assert EventSource('sec_iapd') is EventSource.SEC_IAPD
    # scripts/ria_trigger.py writes the string directly (it never imports models);
    # read it as text so the test carries none of that script's Supabase imports.
    m = re.search(r"^SOURCE\s*=\s*['\"]([a-z_]+)['\"]", _read('scripts/ria_trigger.py'), re.M)
    assert m and m.group(1) == EventSource.SEC_IAPD.value
