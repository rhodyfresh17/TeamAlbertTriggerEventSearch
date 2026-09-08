"""config.example.yaml is what GitHub Actions copies to config.yaml on every
run — a YAML error there breaks every scrape (2026-09-08 03:02 UTC run failed
on a mis-indented list item). Parse it in CI so that can never ship again."""
import os
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    with open(os.path.join(REPO, name)) as f:
        return yaml.safe_load(f)


def test_config_example_parses_and_has_blocklist():
    cfg = _load('config.example.yaml')
    assert isinstance(cfg, dict) and cfg
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == 'excluded_public_companies' and isinstance(v, list):
                    found.extend(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(cfg)
    assert len(found) >= 20, 'mega-cap blocklist missing or mis-indented'
    assert 'GS Finance Corp' in found


def test_local_config_parses_if_present():
    path = os.path.join(REPO, 'config.yaml')
    if os.path.exists(path):
        assert isinstance(_load('config.yaml'), dict)
