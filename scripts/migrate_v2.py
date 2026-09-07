#!/usr/bin/env python3
"""v2 queue cleanup — deterministic, ZERO paid search (Phase 1 of the plan).

Dry-run by default: prints a before/after table and every change it WOULD
make. `--apply` writes. Steps:
  1. trigger expiry      → tombstone 'trigger_expired' (CFO/seat/funding 60d, M&A 120d)
  2. re-gate on stored   → entity-shape denylist, A.J. exclusions, mega-cap blocklist,
     companies_data        fixed territory parser, investor roles no longer workable,
                           junk names; rewrites `fit` (pass/unverified/staged) or tombstones
  3. rep verdicts        → decided accounts tombstoned / marked decided
  4. 8-K Item 1.01       → keep only filings whose full text has acquisition language
                           (one EFTS phrase prefetch); else tombstone
  5. SEC structured      → SIC (submissions JSON) + Form D XML (industry group,
     backfill              declared revenue, SPAC flag) for SEC-sourced events with an
                           unknown vertical → tombstone out/vehicle/too_small, seed
                           revenue band for the rest (free EDGAR calls, throttled)
Run from the repo root inside the venv. Never touches Tavily/Firecrawl.
"""
import argparse, json, os, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings('ignore')
from dotenv import load_dotenv
load_dotenv('.env')
import requests
from supabase import create_client
import enrichment_scout as es
from src.pipeline.gates import (is_non_operating_entity, is_bad_company_name,
                                sic_to_verdict, formd_to_verdict, account_key)
from src.pipeline.typed import EXPIRY_DAYS   # one shelf-life table (Phase 2)

UA = {'User-Agent': 'TeamAlbert Sales Intelligence (sales-leads@teamalbert.local)'}
MERGER_PHRASES = ['Agreement and Plan of Merger', 'Stock Purchase Agreement',
                  'Asset Purchase Agreement', 'Membership Interest Purchase Agreement']


def _j(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return None
    return v


def fetch_visible(svc):
    rows, off = [], 0
    while True:
        b = svc.table('events').select(
            'id,title,company_name,event_type,description,source_url,published_date,'
            'discovered_at,fit,companies_data,grade').is_('blocked_at', 'null')\
            .range(off, off + 999).execute().data
        rows += b
        if len(b) < 1000: break
        off += 1000
    return rows


def efts_merger_adsh(days=150):
    """Accession numbers of 8-K 1.01 filings (last `days`) with acquisition language."""
    out = set()
    start = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    end = datetime.utcnow().date().isoformat()
    for phrase in MERGER_PHRASES:
        for page in range(8):
            try:
                r = requests.get('https://efts.sec.gov/LATEST/search-index',
                                 params={'q': f'"{phrase}" "Item 1.01"', 'forms': '8-K',
                                         'dateRange': 'custom', 'startdt': start,
                                         'enddt': end, 'from': page * 100},
                                 headers=UA, timeout=20)
                hits = r.json().get('hits', {}).get('hits', []) or []
            except Exception:
                hits = []
            for h in hits:
                a = (h.get('_source') or {}).get('adsh')
                if a: out.add(a)
            time.sleep(0.3)
            if len(hits) < 100: break
    return out


_sub_cache = {}
def sec_filer_info(cik):
    if cik in _sub_cache: return _sub_cache[cik]
    try:
        j = requests.get(f'https://data.sec.gov/submissions/CIK{int(cik):010d}.json',
                         headers=UA, timeout=12).json()
        info = {'sic': str(j.get('sic') or ''), 'sic_desc': j.get('sicDescription') or '',
                'state': ((j.get('addresses') or {}).get('business') or {}).get('stateOrCountry') or ''}
    except Exception:
        info = {}
    _sub_cache[cik] = info
    time.sleep(0.25)
    return info


def formd_fields(cik, adsh_clean):
    try:
        idx = requests.get(f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh_clean}/index.json',
                           headers=UA, timeout=12).json()
        xml_name = next((f['name'] for f in idx.get('directory', {}).get('item', [])
                         if f['name'].endswith('.xml')), None)
        if not xml_name: return {}
        time.sleep(0.25)
        xml = requests.get(f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh_clean}/{xml_name}',
                           headers=UA, timeout=12).text
        f = dict(re.findall(r'<(revenueRange|industryGroupType|totalOfferingAmount|isBusinessCombinationTransaction)>([^<]*)', xml))
        time.sleep(0.25)
        return {k: v.strip() for k, v in f.items()}
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    ap.add_argument('--skip-sec', action='store_true', help='skip the EDGAR backfill (steps 4-5)')
    args = ap.parse_args()
    svc = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
    rows = fetch_visible(svc)
    dispositions = es._load_rep_dispositions(svc)
    now = datetime.utcnow()
    print(f"visible before: {len(rows)}  (dry_run={not args.apply})\n")

    tombstone, fit_updates = {}, {}      # id -> reason / id -> (fit, companies_data)
    kinds = Counter()

    # ── 1. trigger expiry ────────────────────────────────────────────────
    for r in rows:
        pd = (r.get('published_date') or r.get('discovered_at') or '')[:10]
        try:
            age = (now - datetime.fromisoformat(pd)).days
        except Exception:
            continue
        if age > EXPIRY_DAYS.get(r.get('event_type') or 'other', 60):
            tombstone[r['id']] = f'trigger_expired: {age}d old {r.get("event_type")}'
            kinds['trigger_expired'] += 1

    # ── 2. deterministic re-gate on stored companies ────────────────────
    for r in rows:
        if r['id'] in tombstone: continue
        cd = _j(r.get('companies_data')) or []
        if not isinstance(cd, list) or not cd:
            cd = [{'name': r.get('company_name') or '', 'role': 'primary'}] if r.get('company_name') else []
        ctx = f"{r.get('title') or ''} {r.get('description') or ''}"
        cd = [c for c in cd if isinstance(c, dict) and c.get('name')
              and not is_bad_company_name(c.get('name'), ctx)]
        if not cd:
            tombstone[r['id']] = 'bad_company_name: no real company name'
            kinds['bad_company_name'] += 1; continue
        if not any((c.get('role') or '').lower() in es.WORKABLE_ROLES for c in cd):
            tombstone[r['id']] = 'no_workable_account: investor/advisor-only roles'
            kinds['no_workable_account'] += 1; continue
        rep = es._rep_verdict_for(cd, dispositions)
        if rep:
            name, st = rep
            if st in es.REP_NOT_FIT_STATUSES:
                tombstone[r['id']] = f'rep:{st} ({name[:60]})'; kinds['rep_not_fit'] += 1; continue
            fit_updates[r['id']] = ({'verdict': 'decided', 'account_name': name, 'territory': 'n/a',
                                     'revenue': 'n/a', 'vertical': 'n/a', 'reasons': [f'rep: {st}']}, cd)
            kinds['rep_decided'] += 1; continue
        fit = es.apply_fit_gates(cd)   # attaches c['fit'] per company using v2 rules
        if fit['verdict'] == 'fail':
            tombstone[r['id']] = f'fit_gate: {"; ".join(fit["reasons"])}'[:300]
            k = 'entity_shape' if 'entity_shape' in tombstone[r['id']] else 'fit_gate'
            kinds[k] += 1; continue
        old = _j(r.get('fit')) or {}
        if fit['verdict'] != old.get('verdict') or fit.get('account_name') != old.get('account_name'):
            fit_updates[r['id']] = (fit, cd)
            kinds[f'fit→{fit["verdict"]}'] += 1

    # ── 4. Item 1.01 content gate ────────────────────────────────────────
    if not args.skip_sec:
        merger = efts_merger_adsh()
        print(f"EFTS: {len(merger)} recent 1.01 filings carry acquisition language")
        for r in rows:
            if r['id'] in tombstone: continue
            if not (r.get('title') or '').startswith('SEC 8-K Item 1.01'): continue
            m = re.search(r'/(\d{10}-\d{2}-\d{6})-index', r.get('source_url') or '')
            if m and m.group(1) not in merger:
                tombstone[r['id']] = 'structured:item_1.01 without acquisition language'
                kinds['item_1.01_not_ma'] += 1

    # ── 5. SEC structured backfill for unknown-vertical SEC events ───────
    if not args.skip_sec:
        n_sec = 0
        for r in rows:
            if r['id'] in tombstone: continue
            if 'sec.gov' not in (r.get('source_url') or ''): continue
            cd = (fit_updates.get(r['id']) or (None, _j(r.get('companies_data')) or []))[1]
            acct = next((c for c in cd if isinstance(c, dict) and c.get('name') == r.get('company_name')),
                        cd[0] if cd else {})
            if isinstance(acct, dict) and acct.get('zi_subindustry'): continue
            m = re.search(r'/data/(\d+)/(\d{18})/', r.get('source_url') or '')
            if not m: continue
            cik, adsh_clean = m.group(1), m.group(2)
            n_sec += 1
            info = sec_filer_info(cik)
            v, why = sic_to_verdict(info.get('sic'))
            if v in ('out', 'vehicle'):
                tombstone[r['id']] = f'structured:{v}: {why}'; kinds[f'sic_{v}'] += 1; continue
            if 'Form D' in (r.get('title') or ''):
                f = formd_fields(cik, adsh_clean)
                amt = f.get('totalOfferingAmount')
                try: amt = float(amt) if amt and amt.lower() != 'indefinite' else None
                except Exception: amt = None
                v2, seg, why2 = formd_to_verdict(f.get('industryGroupType'), f.get('revenueRange'),
                                                 amt, f.get('isBusinessCombinationTransaction') == 'true')
                if v2 in ('out', 'vehicle', 'too_small'):
                    tombstone[r['id']] = f'structured:{v2}: {why2}'; kinds[f'formd_{v2}'] += 1; continue
                if seg in ('LMM', 'MM') and isinstance(acct, dict) and not acct.get('revenue'):
                    acct['revenue'] = seg; acct['revenue_source'] = 'SEC Form D declared revenue range'
                    fit = es.apply_fit_gates(cd)
                    fit_updates[r['id']] = (fit, cd); kinds['formd_revenue_seeded'] += 1
        print(f"EDGAR lookups: {n_sec} SEC events with unknown vertical")

    # ── report ───────────────────────────────────────────────────────────
    print("\nCHANGES BY KIND:")
    for k, n in kinds.most_common(): print(f"  {n:>4}  {k}")
    after = len(rows) - len(tombstone)
    verd = Counter()
    for r in rows:
        if r['id'] in tombstone: continue
        f = fit_updates[r['id']][0] if r['id'] in fit_updates else (_j(r.get('fit')) or {})
        verd[f.get('verdict') or 'none'] += 1
    print(f"\nvisible after: {after}  by verdict: {dict(verd)}")
    print(f"tombstones: {len(tombstone)}   fit rewrites: {len(fit_updates)}")
    print("\nSAMPLE tombstones (up to 6 per reason):")
    by_reason = defaultdict(list)
    title_of = {r['id']: (r.get('title') or '') for r in rows}
    for eid, why in tombstone.items():
        by_reason[why.split(':')[0]].append((title_of[eid], why))
    for k, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  [{k}] × {len(items)}")
        for t, why in items[:6]:
            print(f"     - {t[:62]:64s} → {why[:60]}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return
    ts = now.isoformat()
    n = 0
    for eid, why in tombstone.items():
        payload = {'blocked_at': ts, 'blocked_reason': why[:300], 'enriched_at': ts}
        if eid in fit_updates:
            payload['fit'], payload['companies_data'] = fit_updates[eid]
        svc.table('events').update(payload).eq('id', eid).execute(); n += 1
    for eid, (fit, cd) in fit_updates.items():
        if eid in tombstone: continue
        svc.table('events').update({'fit': fit, 'companies_data': cd}).eq('id', eid).execute(); n += 1
    print(f"\nAPPLIED: {n} row updates")


if __name__ == '__main__':
    main()
