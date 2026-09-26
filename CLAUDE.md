# CLAUDE.md — Team Albert Sales Intelligence

> ⚠️ **OWNERSHIP CHANGED 2026-08-15 — this engine belongs to SCOUT (`hermes-sales`), not Elon.**
> The repo is mounted at `/projects/TeamAlbertTriggerEventSearch` in **Scout's** container; it is
> **no longer mounted in Elon's**. The Monday "Lead Sourcing Engine Status Check" cron runs on
> Scout and delivers to **`#scout-engine`**, as do the host script's pass/fail alerts. Rationale:
> it is a sales engine, and the Monday job judges lead quality (territory drift, duplicates, ICP
> fit) — the ICP definition lives in Scout's vault, and Elon never had it.
>
> **Sections §A and §4b below still say "Elon" throughout and have not been rewritten.** Read
> "Elon" as "the owning Hermes agent" = Scout, and `hermes-elon` as `hermes-sales`, until those
> sections are revised. Everything else in this file is current. `.hermes.md` — the brief that
> actually auto-loads into the cron — IS up to date.

You are an AI agent (Scout, or any successor) inheriting this codebase. This document is your complete onboarding. Read it end-to-end before making changes.

> 📎 **Note on filename**: `.hermes.md` is what auto-loads when a Hermes agent enters this directory (priority order: `.hermes.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`). It **used to be a symlink to this file, and is NOT any more** — it was split into a separate, short brief on 2026-08-11 because Hermes truncates every context source at 20,000 chars and silently drops the middle, which was discarding 59% of this file on every cron run. **They are two files now: editing one does NOT update the other.** Keep `.hermes.md` short and this file complete. **Your global SOUL.md (Hermes identity, in `HERMES_HOME`) loads independently — this file does NOT override it.**

---

## ⚠️ READ THIS FIRST — two rules that prevent the most common mistakes

**RULE 1 — ALWAYS activate the venv before running ANY Python script.**
Every Python script in this repo (enrichment_scout.py, monitor_health.py,
supabase_sync.py, scripts/*.py, etc.) depends on packages installed in the
project's virtual environment (`venv/`), NOT the system Python. The deps include
`yaml` (pyyaml), `supabase`, `python-dotenv`, `requests`, `streamlit`, etc.

If you run `python3 some_script.py` directly you'll get
`ModuleNotFoundError: No module named 'yaml'` (or supabase, etc.). The fix is
NOT to pip-install anything — the venv already has everything. The fix is to
activate the venv first:

```bash
cd ~/Shared/AI-BOTS/TeamAlbertTriggerEventSearch   # or /projects/TeamAlbertTriggerEventSearch in a container
source venv/bin/activate
python3 enrichment_scout.py --dry-run --limit 2
```

NEVER tell the user to `pip install` packages into their system Python — it
pollutes their Mac and is unnecessary. The venv is the answer 100% of the time.
(These scripts run on the MAC, where the venv lives — not inside a Hermes
container, which has no venv and no Mac-side deps. See §4b for why Hermes agents
read logs but don't execute these scripts.)

**RULE 2 — `--apply` DELETES. It is never a "test".**
Every maintenance script (`scripts/migrate_v2.py`, `scripts/backfill_*.py`,
`scripts/expire_triggers.py`, `scripts/ria_trigger.py`) with no flags is a DRY RUN (safe —
shows what would change, writes nothing). Adding `--apply` actually writes to Supabase
(tombstones, upserts, relabels). `--limit 100 --apply` changes up to 100 rows — that is NOT a
"test", it is a real change. The only safe "test" is the no-flag dry run.
Never label any `--apply` command as a test.

---

## §A. ONE-TIME SETUP FOR HERMES AGENTS (Elon — read this first)

Skip this section if you're a Claude agent (you already have filesystem access). For Hermes agents (Elon and successors), three things need to be in place once before you can review/maintain this repo:

### A.1. Repo must be volume-mounted into your container

You live in Docker; your container can't see the host filesystem unless the user adds a volume mount. The required mount in `docker-compose.yml`:

```yaml
hermes-elon:                       # (or whatever container name)
  volumes:
    # ... existing mounts ...
    - ~/Shared/AI-BOTS/TeamAlbertTriggerEventSearch:/projects/TeamAlbertTriggerEventSearch:rw
```

After editing docker-compose.yml, the user runs:

```bash
cd ~/Shared/AI-BOTS
docker compose up -d hermes-elon   # recreates the container with the new mount
```

Verify from inside Elon: `ls /projects/TeamAlbertTriggerEventSearch/CLAUDE.md` — should exist.

### A.2. Required Hermes skills (likely already bundled — verify only)

The skills Elon needs for code review are **bundled by default** with the Hermes image and enabled out of the box. No `install` needed. Verify from the host:

```bash
docker exec hermes-elon /opt/hermes/.venv/bin/hermes skills list 2>&1 | \
  grep -E 'codebase-inspection|github-code-review|github-pr-workflow|systematic-debugging'
```

Each should print a row with `builtin │ builtin │ enabled`. If ANY are missing or disabled, then (and only then) run:

```bash
docker exec hermes-elon /opt/hermes/.venv/bin/hermes skills install <name>
```

Note: from outside the container, `hermes` is not on `$PATH` — always use the full path `/opt/hermes/.venv/bin/hermes`. When Elon runs commands from inside his own chat interface (port 9185), the path is set up correctly already.

### A.3. Optional — bake project context into Elon's SOUL.md

If Elon will spend significant time on this repo, add a paragraph to his global SOUL.md (in his `HERMES_HOME`) so he knows the project exists across all sessions:

```
You are also the long-term maintainer of TeamAlbertTriggerEventSearch
(/projects/TeamAlbertTriggerEventSearch). When working in that directory,
read CLAUDE.md / .hermes.md first for full project context. Conduct weekly
code reviews using the playbook in section §8 of that file. The user
(A.J. Albert) is non-technical — explain trade-offs in plain language and
never ask him to paste secrets into chat.
```

If Elon's role is broader and he should only pay attention to this repo when explicitly asked, skip this — pasting the weekly prompt (see §A.4) is enough.

### A.4. Paste-ready prompt for weekly code review

Whenever A.J. wants the weekly review done, he pastes this into Elon's chat:

```
Weekly code review of /projects/TeamAlbertTriggerEventSearch.

1. cd /projects/TeamAlbertTriggerEventSearch
2. git pull origin main
3. git log --oneline --since="7 days ago"  → identify commits to review
4. Read CLAUDE.md §8 (Code review playbook) for the focused review approach
5. Execute the review on the changed files
6. Report findings as 🔴 BUG / 🟡 RISK / ⚪ NIT, capped at 500 words

If no commits in 7 days OR no real findings, say so plainly — do not
manufacture work.
```

Elon will: pull the repo, walk recent diffs, apply the §8 playbook, and report.

---

## 0. The 60-second elevator pitch

This is a **sales lead intelligence tool** for A.J. Albert's NetSuite Up-Market Sales team. It:

1. **Scrapes** news/SEC/job sources every 4 hours (via GitHub Actions cron)
2. **Enriches** each event with firmographic data (search-spend tiers → own Firecrawl stack → own Tavily key; LLM = shared local llama.cpp Qwen3.6 at :8091) — extracts companies involved, industry, size, revenue, HQ, LinkedIn
3. **Gates** each event on researched fit (territory × revenue band × ZI subindustry), then **grades** survivors with A.J.'s point-based TAL rubric (A/B/C/D + score + confidence)
4. **Surfaces** results on a Streamlit Cloud dashboard at https://teamalbertfy27leads.streamlit.app/ — password-protected, filterable by region, revenue segment, grade

User: **A.J. Albert** — NetSuite Up-Market Sales rep on Team Albert. Non-technical. Depends on you to write code, run commands, and explain in plain language. **Always offer local-only verification (PASS/FAIL, `${VAR}`) rather than asking him to paste secrets in chat.**

---

## 0b. v2 (2026-09-07) — cheap-first pipeline, hard exclusions, rationed spend

Ground-up redesign after the 2026-09-04→06 audit (6-lens, 199-agent adversarial
review; plan file `~/.claude/plans/now-with-the-new-radiant-balloon.md`).

**STATUS 2026-09-08 — ALL FOUR PHASES ARE LIVE.** Commits `747d54d` (Phase 1), `488db2d`
(Phase 2), `a57924d` (config fix), `c584331` (daily re-verify), `b3bdfb9` (Phase 3), `4a5f24b`
(Phase 4) are pushed; migrations 001–003 have been run in Supabase (typed columns 2026-09-07,
`accounts` 2026-09-08, both backfilled); the four Mac jobs are loaded (`enrichment` 6×/day,
`reverify` 06:30 = expiry + ranked re-verify, `healthcheck` 07:00, `oracles` 2nd 05:00). The
v2 plan is COMPLETE — the job now is to operate, tune thresholds from evidence, and grow the
golden set with A.J. See §0b-Phase 4 "Operating posture". Phase 1 shipped
2026-09-07. **Read this before touching enrichment, scrapers, sync or the dashboard.**

**Governing rules**
1. Every stage yields `in` / `out` / `unknown`. **`unknown` is never shown to reps by
   default and never triggers paid spend on its own.**
2. Free, deterministic gates run BEFORE any LLM or web search: `src/pipeline/gates.py`
   (pure functions, `tests/test_gates.py` seeded with real queue names).
3. A rep's verdict is a hard input: `account_dispositions` is read at the top of every
   enrichment run — Not a Fit / Out of Alignment / NetSuite Customer → tombstone `rep:<status>`
   with no research; Picked Up / On Rep TAL → `fit.verdict='decided'`, no research.
4. Tombstones persist the research they had (`_soft_delete(..., extra=)`), and every reason
   uses one vocabulary: `entity_shape:<kind>` · `aj_exclusion` (via entity_shape) ·
   `fit_gate:<dims>` · `structured:<verdict>: <why>` · `formd_too_small` ·
   `board_change_only` · `no_workable_account` · `bad_company_name` · `trigger_expired` ·
   `rep:<status>` · `industry:<kw>`.

**A.J.'s exclusions (binding, 2026-09-04/06)** — `is_non_operating_entity()` kinds:
`fund_vehicle` (LPs, BDCs, "Fund III", SPVs, credit funds — the PE/VC FIRM stays in),
`spac`, `political`, `government` (→ Gov team), `k12` (ALL K-12 incl. charter/private),
`lodging` (→ Hospitality team), `greek`. Financial Services is IN broadly (crypto and
non-hotel real estate are NOT excluded). Investor roles are no longer workable accounts —
the company that got the money is the account.

**`fit.verdict` values**: `pass` (vertical AND territory confirmed; revenue may be a chip) ·
`unverified` (vertical known, territory/revenue unknown — hidden by default, visible under the
dashboard toggle) · `staged` (vertical unknown — hidden; retried free; `fit.deferred_attempts`
counts throttled searches, enriched_at stays NULL until 3 attempts) · `decided` (rep verdict) ·
`fail` (tombstoned). Dashboard default = **verified only** ("Show unverified accounts" toggle).

**Structured pre-gates** (zero search): SEC descriptions now carry `SIC: NNNN (…)`; Form D
descriptions carry `Form D industry group: X.` `Declared revenue: <range>.` `Total offering:
$N.` `SPAC: yes.` — parsed by `_structured_verdict()` → `sic_to_verdict` / `formd_to_verdict`.
Form D research bar (A.J.): declared revenue ≥ $5M, or undisclosed with a ≥ $10M raise; declared
revenue seeds the revenue band (LMM/MM) for the filer. 8-K Item 1.01 is kept only when the
filing's full text has a definitive-agreement phrase (`MA_AGREEMENT_PHRASES`); 5.02 uses
`locationCodes` (territory server-side).

**Search spend**: `SearchBudget(tier)` per event — tier 1 (CFO/Controller hires,
`finance_seat_open`, M&A, funding ≥ $10M) = 2 searches, paid allowed on the firmographic
lookup only; tier 2 = 1 scrape-only; tier 3 (raise < $1M) = none. Probes are scrape-only and
at most ONE per account. Tavily fires only after a genuine Firecrawl empty, within the monthly
budget (`TAVILY_MONTHLY_BUDGET` 900) AND the daily ration (`TAVILY_DAILY_RATION` 25),
charged on success, fail-closed. **Throttled = DEFER** (`{'deferred': True}`), never escalate.
`state/search_mode=defer` (written by monitor_health's Firecrawl canary) pauses all searching.
Bulk modes: `--estimate` pre-flight; `--re-enrich` over 50 events requires `--confirm-credits N`;
`--reverify-unverified` is ranked (cfo > seat-open > M&A > funding, fewest unknown dims) and
capped at 50/run.

**`finance_seat_open`** (Adzuna postings = a company HIRING a CFO/Controller): its own event
type, +3 via #NewController, never #NewCFO. Adzuna runs on a persisted once-per-day latch
(`kv` table) and dedups postings on `(account_key, title)` so a 5-state posting is one lead.

**Sync** sends only 9 scrape-owned columns (never lead_status/notes), last 14 days, batches of
200 — the unpaginated prefetch that reset rep statuses is gone. **Monitor** reads the Tavily
counter (never spends a credit), runs a Firecrawl usefulness canary, and fails when rep-set
statuses drop >20%.

**Queue cleanup**: `scripts/migrate_v2.py` (dry-run default, `--apply`; `--skip-sec` skips EDGAR)
re-gates the existing queue with zero paid search. Phases 2–4 (classify-then-research reorder,
typed columns, yield monitoring, supply sources, accounts table) are in the plan file.

### Phase 2 (2026-09-07) — classify-then-research, typed columns, yield monitoring

**Enrichment order is now** extract → free gates (unchanged) → **STAGE A (free)**: structured
SEC seeds → `AccountCache` firmographics → ONE article-only local-LLM pass that also returns
`classification_confidence` (High/Medium/Low) → **early exit** (tombstone with ZERO searches)
only on: structured SEC verdict, entity/name gates, `zi_subindustry=OTHER` at **High**
confidence (`ARTICLE_OTHER_TOMBSTONE_MIN_CONFIDENCE='High'`, and the description must be >150
chars — `ARTICLE_TOMBSTONE_MIN_DESCRIPTION_CHARS`), or the industry blocklist at High.
Article-only HQ/revenue are hints, never grounds for a pre-search tombstone, and are never
persisted to the AccountCache. → **STAGE B (budgeted)**: search only survivors, only for
fields still unknown (`needs`), under the tier `SearchBudget`; a cached lookup costs no
budget. → fit gates → probes → grade → write. Benchmark 2026-09-07 on 120 already-decided
rows: P(out | OTHER-High) = 0.974, recall 0.62 — Medium is NOT safe (0.94) — do not lower it.

**`verify_state`** (typed column, mirrors `fit.verdict`): `verified` (pass) ·
`researched_ambiguous` (searched, still unknown; retried on a ladder: attempt 1 → +7d,
attempt 2 → +30d, attempt 3 → stop = negative-cached until a NEW event arrives;
`RETRY_BACKOFF_DAYS=(7, 30)`, `MAX_ENRICH_ATTEMPTS=3`) · `staged` (vertical unknown, not yet
searched; retried free) · `decided` (rep) · `not_fit` (tombstoned). **Deferred/throttled
passes never count as attempts** (only `fit.deferred_attempts` moves). Local LLM unreachable
(ConnectionError/5xx — a slow Timeout is just a bad answer) → nothing stamped, `enrich_attempts+1`,
`retry_after=+4h`; three consecutive → re-run the canary, stop only if it fails too.

**Typed columns** (`src/pipeline/typed.py`, contract in its docstring): `source`, `account_key`,
`fit_verdict`, `verify_state`, `hq_state`, `in_territory`, `vertical`, `zi_subindustry`,
`revenue_segment`, `expires_at`, `sic`, `formd_*`, `enrich_attempts`, `retry_after`,
`classification_confidence`, `classified_by`; `source_status.items_fetched/filtered_out`.
**They exist live since 2026-09-07** (A.J. ran `supabase/migrations/002_v2_typed_columns.sql` in the
Supabase SQL Editor**, then `venv/bin/python scripts/backfill_typed_columns.py` (dry-run) and
`--apply` (fills NULLs only; relabels legacy Adzuna rows to `finance_seat_open`). Every
writer/reader probes (`typed.probe_columns`) and runs JSON-only until then; `typed_payload`
is fill-only (never writes NULL over a learned fact; `retry_after=None` is the one explicit
clear). Enrichment selection honors `retry_after`/`enrich_attempts` when the columns exist.

**Caches** (`src/pipeline/cache.py`, in `trigger_events.db`): `search_cache` keyed on
`(account_key, kind)` with per-kind TTL (firmographic/zoominfo 90d, aum/complexity 180d,
nonprofit_990 365d), `account_firmographics` with per-field TTL (hq/industry/url 365d, size
180d, revenue 90d), and `negative_cache` (known-empty accounts, backoff 7/30/90d, rung-aware:
a scrape-only empty never blocks a later paid attempt). A Firecrawl transport failure is a
DEFER, never a known-empty. The legacy `firmographic_cache` table is dead (not migrated).
Counters: `lookups` (once per search), `firecrawl_attempts` (HTTP calls), `negative_cache`.

**Daily re-verify** (A.J. 2026-09-07): `run_reverify.sh` via `com.teamalbert.reverify.plist` at 06:30 ET runs the ranked, capped re-verify pass and posts the summary line to Mattermost #scout-engine (engine tier). **Run safety**: `state/enrichment.lock` (flock; a second run exits 0) and `state/PAUSE`
(`touch state/PAUSE` makes `run_enrichment.sh` skip runs — used while editing/migrating;
delete it to resume). httpx request logging is silenced.

**Monitoring (yield, not liveness)** — daily: `Source yield` (survivors per source, 7d vs prior
21d; "went quiet" needs ≥12 prior survivors (Poisson: P(0|4/wk)=1.8%) and is REMEMBERED in
`state/quiet_sources.json` until the feed recovers — delete the entry to silence by hand),
`Fetched vs filtered` (needs `items_fetched`; a label is dead only when EVERY feed under it
fetched 0), `Retry backlog` (typed columns only). Weekly: `Finance-leader source mix` (WARN
only when the top source flips or moves >15 pts; baseline `state/finance_leader_mix.json`).
`Local SQLite` now checks the AccountCache tables. The cleanup dry-run check is gone.

**Source health judges persistence, per upstream (2026-09-20).** `source_status` is one row per
feed, overwritten every run — "errored in the latest run" cannot tell one failed run from a dead
source, and the old rule (`> 5 errored rows = FAIL`) counted the four SEC feeds, which share one
search endpoint, as four failures. Now the scraper records `consecutive_failures` / `last_success`
per feed (`src/database.py` → synced when the live columns exist,
`supabase/migrations/004_source_status_streaks.sql`), and `check_source_health` groups errored
feeds by upstream (`_upstream`: every `sec_edgar` feed = "SEC search"; otherwise the feed label):
one failed run = a PASS note; ≥ `SOURCE_WARN_STREAK` (2) runs in a row = WARN (a feed that never
produced stays a WARN — fix or disable it); ≥ `SOURCE_FAIL_STREAK` (3) on an upstream with
survivors in the 28d window = FAIL; more than `SOURCE_FAIL_UPSTREAMS` (5) different upstreams in
one run = FAIL (that is our side). Only feeds touched in the last 48h are judged. Before
migration 004 streaks read as unknown and the text names the migration. `Fetched vs filtered`
skips rows whose run ended in `error` — a failed fetch is not an empty feed.

**Sync**: sends `events.source` and the counters only when the live columns exist (probe per
run); legacy Adzuna rows are relabeled `finance_seat_open` at sync time (idempotent); stale
`source_status` rows (>60d, never a name the scraper reported this window) are reaped with
their names logged (`--no-reap` keeps them). **Dashboard**: server-side `verify_state` filter
when the column exists (NULL + `fit.verdict=pass` counts as verified — in-flight rows during
the migration), legacy client-side path otherwise; hidden-count caption is window-wide.

### Phase 3 (2026-09-08) — SUPPLY: more finance-leader triggers, free oracles, new sources

**Why the finance-leader trigger was starved (research 2026-09-08, all live-verified):** the
"PR Newswire Personnel" feed URL was the all-news firehose (a duplicate); the hire detector
only knew *named/appointed/hired/joins* so "X **Appoints** Y as CFO" / "**Names** new Controller"
never matched; GlobeNewswire has no datelines so every CFO item died on "territory unknown";
and **Google News had produced zero events since 2026-02-05** — every item embeds a
news.google.com link and "Google" sat on the substring-matched public-company blocklist.

**Scrape side (GitHub Actions):** whole-word matching for excluded public companies and
excluded industries (ticker indicators stay substring; bare "Pipeline" narrowed to oil/gas
phrases); hire indicators widened; finance-leader roles (controller, VP finance, treasurer,
finance director, head of finance, CAO) → `executive_hire` only with a hire indicator (CFO →
`cfo_hire`; never relabel controllers — #NewController +3 vs #NewCFO +5); unknown-territory
admission ONLY for finance-leader hires (enrichment verifies HQ; false rejects are lost
forever); Google News: `strip_html` before gates, excluded-location only without an
in-territory signal, region-grouped queries (≤12 terms, `when:7d`, 8 state groups: CFO hires,
dealership M&A, home-care M&A) — the state group ADMITS the item only (`matched_regions`
stays empty; the HQ gate decides territory); feed-level `default_region` for regional
journals works the same way (admission only, vetoed by any dateline elsewhere, never a
relevance boost, never a `stable_target` on its own). Hire TYPE is decided from the TITLE
(finance role + strong verb or "new/as/incoming <role>"); earnings releases and product news
that merely mention a CFO are not hires.
**Feeds added** (`config.example.yaml` = `config.yaml`): real PRN Personnel URL, GlobeNewswire
CFO / Chief Financial Officer keyword feeds + Management Changes, PRWeb, HomeCare Magazine,
Performance Brokerage (dealer transactions), BodyShop Business, Virginia Business, Vermont
Business Magazine, NH Business Review, Providence Business News, Hartford Business Journal.
DO NOT ADD: GlobeNewswire subjectcode/24 (class-action spam), Business Wire (registered channel
only), EIN Presswire / ACCESS Newswire (gated), Automotive News / McKnight's / Kerrigan /
bizjournals.com (blocked/404), Funeral Business Advisor (domain gone); funeral-home queries
have no yield anywhere. Fixtures: `tests/fixtures/*.xml` (one real fetch each).

**Free oracles (`src/pipeline/oracles.py`, tables in `state/oracles.db`, refreshed by
`scripts/refresh_oracles.py --source all` — monthly via `run_oracles.sh` /
`com.teamalbert.oracles.plist`, 2nd of the month 05:00 ET):** SEC IAPD adviser feed
(23,797 RIAs; state, RAUM, headcount, website; revenue ≈ min(RAUM×0.7%, employees×$400K)),
FDIC active banks (4,235; state, assets, website; revenue ≈ assets×5.5%; call
`api.fdic.gov` directly — the documented host redirects and the hop is metered 20/min),
ProPublica nonprofits (state-filtered search + similarity gate ≥0.85; **zero hits = HTTP 404
with a JSON body**, treated as a confirmed miss and negative-cached; alias expansion
YMCA↔"Young Mens Christian Association"; no website, no officers). `lookup(name, hint)` runs in
Stage A after the structured seeds and before the article LLM (provenance `oracle`,
`classified_by='oracle'`), plus a second chance when the article says banking/nonprofit/RIA.
An oracle estimate < $5M = `too_small` → revenue out (same rule as Form D). **990 Part VII
officer diffs are NOT a trigger** (median 16–18 month lag) — decided, don't re-propose.

**New trigger source `sec_iapd`:** `scripts/ria_trigger.py` (dry-run default, `--apply`)
emits `expansion` events "New SEC-registered investment adviser" for in-territory, in-band
(≥$5M est.) registrations of the last 45 days (~50/yr), seeded with hq_state / zi / revenue,
`verify_state` NULL so enrichment grades them; per-firm ledger `ria_trigger_emitted` in
`state/oracles.db` (refresh never drops it). Label 'SEC IAPD' (`adviserinfo.sec.gov` before
the generic sec.gov rule in `sources.py` and the dashboard).

**Domains (`src/pipeline/domains.py`):** `resolve(name, hints)` ladder hint_url → cache →
local oracle tables → FDIC → Clearbit autocomplete (keyless; accept only an exact
account-key match or a single token-superset; bank-shaped names need the state) → SEC
submissions (CIK) → guess (OFF: `DOMAINS_GUESS_ENABLED`, unsafe). Aggregator/wire/social
denylist; profile URLs become aliases (`linkedin:<slug>`), never identities. Transient errors
are never negative-cached. Stored on the company record (`domain`, `domain_method`) and in
the AccountCache; a typed `domain` column waits for the Phase 4 accounts table.

**Supply visibility:** Weekly Scorecard "Supply" section (trigger × source pivot 7d vs prior
7d, verified accounts per vertical over 28d with the verified-without-search share, finance-
leader share vs the 30% target, top-source share vs the 40% ceiling). Weekly monitor checks
`Vertical mix` (WARN ONCE when a vertical with ≥5 verified goes to 0 — remembered as
"still dark since <date>" in `state/vertical_mix.json` until it recovers — or drops under 5%
from >15%) and `Finance-leader share` (WARN when <15% while the last on-target reading, a
high-water mark kept in `state/finance_leader_share.json`, is ≤6 weeks old). Manual runs:
add `--no-state` so they never overwrite the Monday baselines.
First reading 2026-09-08: finance-leader share 32%; verified 28d = FS 65% · Nonprofits 18% ·
Consumer Services 18%; without-search 0% (provenance starts now).

### Phase 4 (2026-09-08) — accounts as the primary object

**`accounts` table** (`supabase/migrations/003_accounts.sql` — A.J. pastes it into the SQL
Editor, then `venv/bin/python scripts/backfill_accounts.py` (dry-run) and `--apply`; RLS ON,
zero policies, service role only): one row per `account_key` (`src/pipeline/gates.account_key`,
the ONE normalizer — the dashboard delegates to it): canonical name, aliases, domain, HQ/state,
vertical/subindustry, revenue segment, entity class, fit/verify state, firmographics, ONE grade
(+ which event earned it), best trigger, event count, and the rep's disposition WITH a reason
code. Module `src/pipeline/accounts.py` (fill-only merge: facts never downgraded, verified never
demoted, grade replaced only when better / re-graded / expired; dispositions are rep-owned and
never touched by enrichment). The table went live 2026-09-08 (003 run + backfill: 2,292 rows, all 8
rep verdicts carried over); every reader/writer still probes and degrades to
the Phase 1-3 behaviour.

**One grade per account** (plan, supersedes the 2026-07-17 per-company/headline rule): the
event's grade belongs to the chosen account (fit.account_name); the secondary-company grading
loop and "headline promoted to best account" are gone; other workable companies get a facts-only
account row. **Enrich once per account**: a verified account row fresher than 90 days is used
as-is (provenance `account`) — the new trigger is graded, nothing is re-researched.
Rep verdicts come from `accounts.disposition` (legacy `account_dispositions` merged in);
a Not-a-Fit account's next event never reaches search.

**Hashtag guards** (`src/pipeline/hashtag_guards.py`, applied before the deterministic score;
NEVER stricter than the TAL rubric — they strip tags the model invents without evidence):
#NewCFO/#NewController need a finance-hire SUBJECT (`src/pipeline/hires.finance_hire_subject`:
role within ~80 chars of a hire verb; never attribution, interim, former, board seats or awards)
— except SEC 8-K 5.02 filings, whose summary names no person: the scraper's phrase-typed
`cfo_hire`/`executive_hire` IS the evidence and the tag is kept; #Acquisitions needs the
acquirer/primary role on an M&A event; #Funding needs a ≥$1M non-nonprofit raise on a funding
event OR a ≥$1M raise within 18 months in the FUNDING SEARCH evidence (amount read next to the
raise verb, never the largest figure in the text); #HoldCo = holding words OR the holding-co
subindustry OR ≥2 subsidiaries (rubric OR); #100EE needs a size bucket whose low bound is ≥100 or
a numeric ≥100; #Global ≥2 countries counting the account's own; #AssetManagerScale needs ≥$250M
in text or the SEC adviser registry; #FormerUser/#PrevConvo are always stripped. Stripped tags
are logged as `guard: -#Tag (why)` and kept exceptions as `guard: #Tag kept — …`.

**Trigger expiry**: `scripts/expire_triggers.py` (dry-run default) runs `--apply` inside
`run_reverify.sh` before the 06:30 re-verify pass (takes `state/enrichment.lock`) — expired
triggers become `trigger_expired` tombstones (research kept; NOT `not_fit`) and the account's
best trigger (priority, recency, enriched rows first) and best remaining GRADE are recomputed —
never a NULL grade while a graded live event remains. Enrich-once freshness reads ONLY
`firmographics.researched_at` (stamped on researched writes; never on `account`/article).

**Golden set** (`tests/golden/accounts.json`, `tests/test_golden.py`, exporter
`scripts/build_golden_set.py`): ~250 reason-coded rows (rep verdicts + machine-seeded samples per
tombstone reason + verified accounts + hire titles) asserted in CI against the FREE functions
only. Rule: never edit an expected value to make a test pass — fix the code, or mark the row
`reviewed: true` with a reason. A.J. reviews/extends the file over time.

**Deleted** (orphaned since v1): `src/enrichment.py`, `import_leads.py`, `sheets_sync.py`,
`sync_db.py`, `cleanup_legacy_events.py`, `src/scrapers/bing_scraper.py`,
`src/scrapers/finsmes_scraper.py`, and `src/scrapers/job_scraper.py` ("Google Jobs": 2 events in
28 days, both rejected; Adzuna covers open finance seats). `job_search`/`bing_news`/`finsmes`
config sections removed. `EventSource.SEC_IAPD` added. CI now runs the golden set + gate +
config tests on every scrape.

**Dashboard**: account cards show the trigger history (every live event for the account),
dismissals require a reason code (wrong vertical · out of territory · too big · too small · not a
real trigger · duplicate · already a customer · other), the Scorecard gets a "why events were
removed" pivot (reason × source × subindustry), and `expansion` events render with their own card.

**Operating posture (from 2026-09-09).** Nothing needs a human on a schedule. What to read:
- `#scout-engine` daily: the 06:30 re-verify post (expiry line + `verified:N ambiguous:N staged:N
  not_fit:N`), the 07:00 health check (WARN/FAIL only; "Source yield … went quiet" is REAL — it
  repeats until the feed recovers; "Source health … DOWN 3+ runs in a row" is REAL too, while a
  source that failed a single run never alerts; Adzuna/Google News flags from Sept 8 should clear as the
  Phase 3 scrapers run), Mondays the weekly lines (finance-leader share vs 30%, vertical mix,
  finance-leader source mix on change only).
- The first weeks after 2026-09-08 carry a supply surge (first Phase 3 scrape: 112 events,
  52 `cfo_hire` vs ~10/day before). Expect more `not_fit` and `staged` for a while; that is
  the free gates working, not noise. Firecrawl/DuckDuckGo throttling — not Tavily budget — is
  the search bottleneck; staged rows are retried free.
- A.J.'s two feedback channels into the tool: dismiss accounts WITH a reason code (dashboard),
  and mark rows in `tests/golden/accounts.json` `reviewed: true` with a note.
- Known follow-ups (not blockers): `src/scrapers/base.py` still rejects "Former X executive
  named CFO" / "Longtime … CFO" / "Taps Industry Veteran … as CFO" titles (hires.py handles
  them — mirror `_NOT_THE_SEAT_RES`); `_board_only_event` is still substring-based;
  `scripts/backfill_typed_columns.py` should import `structured_verdict` from
  `src/pipeline/structured.py`; the post-fit email digest (Phase 2 offer) is unbuilt; DOL 5500 /
  NCUA / CRA oracles and FDIC structure-change triggers are researched but unbuilt.

## 1. Architecture (data flow)

```
┌──────────────────────────────────────────────────────────────────────┐
│  GitHub Actions cron (every 4 hours, `.github/workflows/scraper.yml`) │
│  ───────────────────────────────────────────────────────────────────  │
│  1. python -m src.main                                               │
│     ├── RSSScraper        (PR Newswire, VC News Daily, etc.)        │
│     ├── GoogleNewsScraper                                            │
│     │   (JobScraper / BingNews / FinSMEs deleted in Phase 4)        │
│     ├── SECScraper        (EFTS API, 8-K Items 5.02 / 2.01 / 1.01) │
│     ├── FormDScraper      (private raises, territory-filtered)      │
│     └── AdzunaScraper     (title_only queries, 3 calls/day)         │
│                                                                       │
│     Two-pass dedup (URL hash → recent title match) before write       │
│     Industry exclusion check (mining/steel/oil/hospitality/etc.)      │
│     → SQLite (trigger_events.db, CACHED between Actions runs)         │
│                                                                       │
│  2. python3 supabase_sync.py                                          │
│     Upserts SQLite events to Supabase                                 │
│     **PRESERVES user-set lead_status + notes**                        │
│     **Does NOT touch grade/hashtags** (enrichment writes those direct)│
└──────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Mac launchd cron — `~/Library/LaunchAgents/com.teamalbert.enrichment.plist` │
│  ─────────────────────────────────────────────────────────────────  │
│  Fires at :30 past 0/4/8/12/16/20 local Eastern time                  │
│  → run_enrichment.sh → python enrichment_scout.py                     │
│                                                                       │
│  Per unenriched event (REDESIGNED 2026-07-16 — "filter on researched  │
│  evidence BEFORE grading"):                                           │
│   1. LLM extracts companies + roles (shared llama.cpp Qwen3.6 :8091)  │
│   2. Firecrawl search per unique company (Tavily fallback if empty)   │
│   3. LLM extracts firmographics → companies_data JSONB — INCLUDING    │
│      zi_subindustry (closed-set pick from the 32 ZoomInfo             │
│      subindustries in ZI_SUBINDUSTRIES, or 'OTHER')                   │
│   4. Fast industry blocklist (unambiguous never-fits: mining, pharma…)│
│   5. FIT GATES (apply_fit_gates — deterministic code):                │
│        territory (researched HQ) × revenue band (LMM/MM/Corp) ×       │
│        vertical (ZI allowlist). Confirmed-out on ANY dim →            │
│        soft-delete. Unknowns → keep + cap grade at B + ⚠️ flag        │
│        persisted to `fit` JSONB column.                               │
│   6. Research probes (free): funding-lookback search; ProPublica 990  │
│      API for nonprofits; AUM search for asset managers                │
│   7. TAL grading (A.J.'s point rubric — see §2) w/ probe evidence     │
│   8. event_type reclassification — CFO-EQUIVALENTS ONLY relabel to    │
│      cfo_hire (_finance_role); Controllers stay executive_hire        │
│      (prevents #NewCFO/#NewController double-count)                   │
│   9. Writes directly to Supabase                                      │
└──────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Streamlit Cloud dashboard — dashboard.py                             │
│  ───────────────────────────────────────────────────────────────────  │
│  Reads from Supabase (no writes except lead_status/notes updates)     │
│  Password-gated via st.secrets["DASHBOARD_PASSWORD"]                  │
│  Auto-deploys on git push to main                                     │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. The business model

### Territory (FY27)
Source of truth: `/Users/andrewalbertbase/Downloads/FY27 Territories.xlsx` (kept locally — not in repo).

- **23 US states** (Northeast + Mid-Atlantic + Southeast + Rust Belt): AL, CT, DE, FL, GA, IN, KY, ME, MD, MA, MI, NH, NJ, NY, NC, OH, PA, RI, SC, TN, VT, VA, WV
- **DC** included per A.J. (not in official xlsx but his actual coverage)
- **6 Canadian provinces**: NB, NL, NS, ON, PE, QC

### Target industries — 3 NSCorp verticals × 32 ZoomInfo subindustries

| Industry | Subindustries (examples) |
|---|---|
| **Financial Services** | Banking · Credit Cards & Transaction Processing · Debt Collection · Holding Companies · Insurance · Investment Banking · Lending & Brokerage · VC & PE |
| **Nonprofits & Organizations** | Charitable Foundations · Cultural & Arts (Museums, Theaters, Libraries, Zoos) · Educational Institutions (Colleges, K-12) · Membership Orgs (Religious, Associations) |
| **Consumer Services** | Auctions · Auto Dealers · Auto Repair · Barber/Salon · Cleaning · Funeral Homes · Photography · Real Estate · Repair Services |

⚠️ **Gotcha — "Banking" was previously EXCLUDED in config**, silently dropping legitimate Banking-vertical leads for who-knows-how-long. Fixed in commit `16529a1`. If you ever see Banking-related events not appearing, check `territory.excluded_industries` doesn't list "Bank" or "Banking" again.

### Revenue segments (NetSuite Up-Market sales taxonomy)

| Code | Range | Notes |
|---|---|---|
| **LMM** | <$10M | Lower mid-market |
| **MM** | $10M-$20M | Mid-market |
| **Corp** | $20M-$100M | Corporate |
| **Enterprise** | $100M+ | Out of NetSuite up-market band — usually on Oracle/SAP |

Default dashboard filter shows LMM + MM + Corp (the up-market sweet spot, $0-$100M).

### TAL grading rules (A.J.'s point rubric, aligned 2026-07-16 — in `enrichment_scout.py` → `TAL_GRADING_PROMPT` + `_compute_v11_grade`)

Point-based. LLM picks evidence-backed hashtags; CODE computes score + grade
(LLMs are unreliable at arithmetic). Source of truth for the rubric: A.J.'s
"TAL Lead Grader" custom-GPT prompt — if he shares a newer version, diff and
realign.

**High-intent triggers:** #NewCFO +5 · #NewController +3 · #Funding +3 (18mo,
for-profit) · #PEBacked +3 · #Acquisitions +3 (36mo, Acquirer role) ·
#FormerUser +3 · #PrevConvo +3 (last two need CRM data — dormant in pipeline)

**Complexity signals (+2 each):** #HyperGrowth · #100EE (verified 100+
employees) · #Locations · #Entities · #HoldCo · #Global (multi-COUNTRY ops —
NEVER for foreign HQ) · #Franchisor · #Franchisee · #Legacy ·
#AssetManagerScale (PE: $1B+ AUM & 2+ funds; VC/RIA/REIT directional tiers)

**Grades:** A = 8+ AND ≥1 high-intent trigger · B = 5-7 · C = 2-4 · D = 0-1.
Complexity-only 8+ (no trigger) → B. Low/unparseable confidence → cap C.
Fit 'unverified' → cap B (⚠️ VERIFY FIT chip on dashboard). Solo #NewCFO = 5
= B by design. No hashtag-count cap ("as many as evidence supports").

**Finance-tag evidence guard (2026-07-21):** the LLM provably fabricates
#NewCFO on non-CFO events ("+5 applied as highest-value single trigger for
material definitive agreement events" — CNL). Deterministic backstop in the
grading parser: #NewCFO requires event_type=cfo_hire OR a CFO-equivalent
phrase in title/description; #NewController requires a Controller phrase.
Otherwise the tag is stripped and score/grade recomputed. 26 inflated
grades corrected in the one-off cleanup (incl. one fake A).

**Board changes are NOT triggers (A.J. 2026-07-21):** directors aren't
involved in ERP decisions. Three layers: (1) SEC scraper only ingests Item
5.02 filings whose full text mentions a finance-leader role — CFO set →
cfo_hire, Controller/Chief Accounting set → executive_hire, everything else
(board elections, CEO changes) skipped at the source (fails open only
when the EFTS prefetch ANSWERS empty; a prefetch that FAILS skips the item
for that run — see §3 sec_scraper); (2) `_board_only_event()` gate tombstones
board-only executive_hire events in both enrich + regrade paths
(`board_change_only` reason); (3) prompt rule.

**Complexity probe (2026-08-09):** `probe_complexity()` — one cached
ladder search (locations/subsidiaries/franchise) for high-intent event
types with non-failed fit. Feeds #Locations/#Entities/#Global/#Franchisor
evidence into grading — the +2 signals that bridge B (5-7) to A (8+);
before it, #Locations appeared on 2/440 events and #Entities on 0.
`_parse_hq_size_from_snippets()` deterministically regexes HQ + headcount
from aggregator snippet phrasings (fill-if-missing, both search passes).
Dashboard has a "📊 Weekly Scorecard" expander (intake by source, noise
removed by reason, pickups 7d-vs-prior) — read it before tuning sources.

**Public school districts = automatic fail** (A.J. 2026-08-09: RFP
procurement dead ends). Name-pattern gate in `company_fit()`
(`_is_public_school_district`) — private/charter schools stay in-vertical.

FIT comes BEFORE grading: `apply_fit_gates` (territory × revenue × ZI
vertical) soft-deletes confirmed-out events, so grades only rank workable
accounts. Hashtag definitions are STRICT — history of LLM stuffing. Don't
loosen without A.J.

---

## 3. Key files (in dependency order)

### Configuration
- **`config.example.yaml`** ← edit this; gitignored `config.yaml` is generated via `cp`. Holds territory, keywords, RSS feeds, excluded industries, mega-bank exclusions, Adzuna/SEC settings.
- **`.env`** (gitignored) — local secrets: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, TAVILY_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY
- **`.streamlit/secrets.toml`** (gitignored) — Streamlit Cloud secrets: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (new-style `sb_secret_` admin key; added 2026-07-21), SUPABASE_KEY (legacy anon — now useless, RLS locks it out), DASHBOARD_PASSWORD
- **`.github/workflows/scraper.yml`** — cron schedule + Actions secrets wiring
- **`requirements.txt`** — Python deps

### Scrapers (`src/scrapers/`)
- **`base.py`** — `BaseScraper` parent class. **`extract_company_name()`** (40+ verb patterns, case-insensitive) and **`matches_industry()`** live here. Both used heavily downstream.
- **`rss_scraper.py`** — handles all RSS feeds in `config.sources.rss_feeds`. Each feed is parsed strictly first; one the strict parser rejects is repaired once by `repair_feed_xml` (undeclared namespace prefixes, a bare `&` — e.g. inside an image URL — HTML-only entities like `&nbsp;`, XML-forbidden control characters; CDATA and comments untouched) and parsed again. The Actions log prints `<feed>: repaired malformed feed XML (…)` when that happens; a feed that still fails reports the second error. Tests: `TestFeedXmlRepair` in `tests/test_scrapers.py`.
- **`sec_scraper.py`** — SEC EDGAR EFTS search. Item 5.02 (officer changes), 2.01 (M&A completion), 1.01 (material agreements). **Pre-fetches CFO-related accession numbers in one extra EFTS call, paginated to 5 pages.** Every EFTS request (8-K searches, the phrase prefetches, Form D pages) goes through `_efts_get`: `EFTS_MAX_TRIES` (3) with `EFTS_BACKOFF_SECONDS` on 5xx / 429 / connection errors / a non-JSON body; any other 4xx raises at once. Exhausted tries open a breaker shared by `SECScraper` and `FormDScraper` for `EFTS_BREAKER_SECONDS`, so an upstream outage costs one exhausted call per run and every SEC feed reports `error` ("SEC search unavailable this run"). A prefetch that FAILS skips its item for the run (a half-built CFO set would mistype filings, and a saved event is never re-typed); the next run re-reads the same lookback window, so nothing is lost. Tests: `tests/test_sec_efts.py`.
- **`adzuna_scraper.py`** — Adzuna jobs API. Throttled to noon UTC; since
  2026-08-09 runs `title_only` queries ('controller','cfo') with
  sort_by=date (~3 calls/day ≈ 90/mo) — the old what_or matched loose
  words in descriptions and never surfaced new postings. In-code strict
  title filter + non-finance-"controller" exclusions (air traffic, pest
  control...) + staffing-agency blacklist + no-company-name drop.
- **`sec_scraper.py :: FormDScraper`** — SEC Form D private capital raises
  (added 2026-08-09). EFTS forms=D + server-side territory filter via
  locationCodes; pooled-fund vehicles dropped via exemption items 3C/3C.1,
  name patterns, SIC 6722/6726/6770; offering amount + industry group
  parsed from primary XML; HTTP budget capped (newest-first,
  `form_d.max_lookups`/run). Catches private in-territory companies that
  never hit the news wires → event_type=funding.
- **`news_scraper.py`** — Google News

### Pipeline orchestration
- **`src/main.py`** — `TriggerEventMonitor` orchestrates the scrape cycle. Two-pass dedup (URL → recent title) lives here. Wires all scrapers.
- **`src/database.py`** — SQLite manager. `has_seen_url()`, `mark_url_seen()`, `has_recent_event_title()` (the title dedup added in commit `c606bca`).
- **`src/models.py`** — `TriggerEvent` dataclass, `EventType` + `EventSource` enums.

### Enrichment + grading
- **`enrichment_scout.py`** — THE most important file outside the scraper. Reads unenriched events from Supabase, runs the 4-step pipeline (extract → search → firmographics → grade), writes back. **Has THREE modes:**
  - default — enrich only new events
  - `--re-enrich` — full re-pull (hits the configured search backend = Firecrawl by default; Tavily quota only consumed on fallbacks, currently ~3% of searches)
  - `--regrade-only` — re-apply grading + industry filter + event_type reclassification using EXISTING companies_data (NO search-API calls, free)
- **`run_enrichment.sh`** + **`~/Library/LaunchAgents/com.teamalbert.enrichment.plist`** — launchd wrapper that fires enrichment every 4 hours on the Mac.

### Sync + dashboard
- **`supabase_sync.py`** — pushes SQLite scraped events to Supabase. **Critical**: preserves user-set `lead_status` and `notes` (the bug it had previously was silently overwriting them every cycle — see commit `14157c2`).
- **`dashboard.py`** — Streamlit UI. ~1300 lines. Reads from Supabase, renders event cards by category tab, handles filtering + bulk actions. Filters live in an `st.popover` (NOT the sidebar — sidebar toggle was unreliable).

### Maintenance scripts
- **`scripts/`** — `migrate_v2.py` (queue re-gate), `backfill_typed_columns.py`, `backfill_accounts.py`, `refresh_oracles.py`, `ria_trigger.py`, `expire_triggers.py`, `build_golden_set.py`, `check_feeds.py` — all dry-run by default (`--apply` writes)
- **`scripts/check_feeds.py`** — debug utility for feed health

---

## 4. Credentials map

**🔒 NEVER ask A.J. to paste secrets into chat. NEVER print/log secret values. Always offer local-only verification (PASS/FAIL, length/prefix only, `${VAR}` references).**

| Secret | Where it lives | Purpose |
|---|---|---|
| `SUPABASE_URL` | `.env`, Streamlit secrets, GitHub Secrets | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | `.env`, GitHub Secrets, Streamlit secrets | ALL reads/writes everywhere (bypasses RLS). Streamlit copy is the new-style `sb_secret_` key; `.env`/GHA copies are legacy JWTs — both work. |
| `SUPABASE_KEY` (anon) | `.env`, Streamlit secrets | **Dead since 2026-07-21 RLS lockdown** — kept only as the negative probe for RLS verification (should always see 0 rows). |

**Supabase RLS posture (since 2026-07-21):** RLS is ENABLED on all `public` tables (`events`, `account_dispositions`, `source_status`) with **zero policies** — the anon key can neither read nor write anything; every component uses the service-role key. Two standing rules: (1) any NEW table must get `alter table public.<name> enable row level security;` right after creation or Supabase's security emails resume; (2) never create permissive policies (`for select using (true)` etc.) — the original setup had such policies dormant on `events`/`source_status`, and enabling RLS woke them up until we dropped all policies. Verify anytime with the anon-key probe (expect 0 rows/APIError on all tables).
| `TAVILY_API_KEY` | `.env` ONLY (Mac side; removed from GitHub Secrets 2026-09-06) | Web search fallback for enrichment. Was leaked in git history (commit `ac17b5b`), rotated in commit `536b57d`. Never re-hardcode a fallback. |
| *(no other search keys)* | — | ⛔ Brave = RESERVED for the Hermes fleet (never add here). Google CSE = closed to new customers (dead). SearXNG :8888 = fleet infra (severed). Only vetted future candidate if the scorecard shows sustained `throttled` starvation: SerpAPI free 100/mo — ask A.J. first. |
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | `.env`, GitHub Secrets | Adzuna jobs API (free tier ~100-250 calls/month) |
| `ANTHROPIC_API_KEY` | not set anywhere (removed from GitHub Secrets 2026-09-06) | Optional cloud LLM pre-empt in enrichment_scout; the shared local llama.cpp (Qwen3.6, :8091) is the LLM. |
| `DASHBOARD_PASSWORD` | Streamlit secrets | Dashboard login |
| `EMAIL_PASSWORD` + `SENDER_EMAIL` + `ALERT_RECIPIENT` | GitHub Secrets | Email alerts from the scrape job (recipient = A.J. since 2026-09-07; the address is never committed) |

---

## 4b. Ongoing health monitoring (Elon's primary maintenance job)

A single script — `monitor_health.py` — runs end-to-end diagnostics. Three modes:

| Mode | Runtime | What it checks |
|---|---|---|
| `--quick` *(default)* | ~10s | env creds, Tavily budget counter (local — never spends a credit), Firecrawl usefulness canary (→ `state/search_mode`), rep-state intact, local LLM (llama.cpp :8091), Supabase reachable, scrape freshness, enrichment lag, local SQLite (checks the AccountCache tables; the scrape DB lives in the GHA cache by design), launchd job loaded |
| `--daily` | ~30s | all of the above + **source health** (failure STREAKS per upstream — one failed run is a note, 2 in a row WARN, 3 in a row on a producing source FAIL; see §0b) + 7-day-vs-prior volume trend + **source yield** (survivors per source 7d vs prior 21d, quiet feeds remembered in `state/quiet_sources.json`) + fetched-vs-filtered + retry backlog |
| `--weekly` | ~60s | all of the above + **finance-leader source mix** (WARNs only on a change vs `state/finance_leader_mix.json`) |

Each check returns 🟢 PASS / 🟡 WARN / 🔴 FAIL with a one-liner. **Exit code is non-zero if any FAIL**, so cron and Elon can detect failures programmatically.

```bash
# Run from Mac terminal or inside Elon's container
python monitor_health.py            # quick
python monitor_health.py --daily
python monitor_health.py --weekly --no-state   # manual weekly runs keep the Monday baselines
python monitor_health.py --json     # machine-readable
```

### Monitoring architecture — IMPORTANT for any agent doing maintenance

The monitoring is split between two execution environments by design:

1. **Mac launchd cron runs `monitor_health.py`** — fires daily at 7am Eastern
   via `~/Library/LaunchAgents/com.teamalbert.healthcheck.plist`. The script
   needs the Mac's Python venv (which has `supabase`, `dotenv`, `requests`
   installed) AND access to Ollama at `localhost:11434`. Monday runs use
   `--weekly`, other days use `--daily`. The wrapper `run_health_check.sh`
   chooses mode based on day-of-week.

2. **Elon (running inside his Hermes container) reads the alerts log** —
   he does NOT run the health check himself. His container doesn't have
   the right Python deps installed, and `localhost` inside a container
   does NOT resolve to the Mac's Ollama. Instead, Elon reads:
     `/projects/TeamAlbertTriggerEventSearch/logs/health_alerts.log`
   and reports/escalates based on what he finds there.

### What Elon's role looks like day-to-day

**Daily** (whenever asked, or on his own schedule):
```
1. cd /projects/TeamAlbertTriggerEventSearch
2. tail -30 logs/health_alerts.log    # see recent monitoring output
3. If most recent entries show "All clear" → report "system healthy"
4. If recent entries show 🟡 or 🔴 → summarize WHAT'S wrong + the
   recommended fix (already in the alerts log). Optionally git pull
   first to check for any commits that might address it.
```

**Weekly** (Monday morning, after the launchd cron has run --weekly):
```
1. cd /projects/TeamAlbertTriggerEventSearch && git pull
2. tail -50 logs/health_alerts.log   # this week's monitoring history
3. git log --oneline --since="7 days ago"   # this week's commits
4. Conduct code review per §8 playbook on the recent commits
5. Combined report: health status + code review findings
```

### Log file layout

| File | Owner | Purpose |
|---|---|---|
| `logs/health_alerts.log` | launchd writes, Elon reads | Concise alert summary — one entry per check run. "All clear" or 🟡/🔴 + actionable details. |
| `logs/health_check_runtime.log` | launchd writes | Full verbose output of each health check run — for debugging when alerts log shows something unexpected |
| `logs/healthcheck_launchd.log` | launchd writes | launchd's own stdout/stderr — only relevant if launchd itself fails |
| `logs/enrichment.log` | launchd writes (different cron) | The enrichment cron's output — Elon can read for context |
| `logs/enrichment_launchd.log` | launchd writes | enrichment launchd's stdout/stderr |

### How Elon notifies A.J. of failures

For MVP, when running interactively in Hermes chat, Elon reports findings
directly to A.J. in the conversation. For autonomous monitoring without
an active chat, future options (none picked yet):

1. **Hermes messaging platform** — if Telegram/Slack is configured for
   Elon, he can DM A.J. when health_alerts.log has new 🔴 entries
2. **Dashboard widget** — surface `health_alerts.log` content inside
   the Streamlit dashboard so A.J. sees alerts when he visits
3. **GitHub Issues** — Elon uses `github-pr-workflow` skill to open
   issues when something breaks

### Why Mac runs the check, not Elon

Important — this came up during initial setup. Elon (Hermes container)
cannot run `monitor_health.py` directly because:

- The container's Python doesn't have `supabase`, `dotenv` installed
- `localhost` inside the container does NOT resolve to the Mac's Ollama
  (would need `host.docker.internal:11434` instead)
- The Mac's `~/.env` isn't readable from inside the container by default
- Installing deps in the container survives only until restart

Keeping the EXECUTION on the Mac (native env, real Ollama, real venv)
and SYNTHESIS on Elon (reads logs, summarizes, escalates) is the
clean separation. Don't try to "fix" this by installing supabase
inside Elon's container — the architecture is intentional.

---

## 5. Common operations cheat sheet

```bash
cd /Users/andrewalbertbase/Shared/AI-BOTS/TeamAlbertTriggerEventSearch
source venv/bin/activate
```

| Task | Command |
|---|---|
| **Health check (quick — ~10s)** | `python monitor_health.py` |
| **Health check (daily — ~30s)** | `python monitor_health.py --daily` |
| **Health check (weekly — ~60s)** | `python monitor_health.py --weekly --no-state` (manual runs must not overwrite the Monday baselines) |
| Manual scrape cycle (locally, mirrors GitHub Actions) | `python -m src.main` |
| Enrich only NEW events | `python enrichment_scout.py` |
| Re-grade ALL events (free, no search API) | `python enrichment_scout.py --regrade-only` |
| Re-grade one event type (free) | `python enrichment_scout.py --regrade-only --event-type finance_seat_open` |
| **Pause / resume the launchd enrichment runs** | `touch state/PAUSE` … `rm state/PAUSE` (wrapper skips runs while the file exists) |
| **Typed-column migration (one-time, A.J.)** | paste `supabase/migrations/002_v2_typed_columns.sql` into Supabase → SQL Editor → Run; then `python scripts/backfill_typed_columns.py` (dry-run) and `--apply` |
| Re-verify hidden accounts (ranked, capped 50, honors retry_after) | `python enrichment_scout.py --re-enrich --reverify-unverified` |
| Daily re-verify job (06:30 ET, `com.teamalbert.reverify.plist` → `run_reverify.sh`, posts to #scout-engine) | `tail -f logs/reverify.log` · pause with `touch state/PAUSE` |
| Refresh the free oracle tables (SEC advisers + FDIC banks; monthly job does this) | `python scripts/refresh_oracles.py --source all` |
| **Accounts table (one-time, A.J.)** | paste `supabase/migrations/003_accounts.sql` into Supabase → SQL Editor → Run; then `python scripts/backfill_accounts.py` (dry-run / `--preflight`) and `--apply` (takes `state/enrichment.lock`; `--since` is preview-only and refuses `--apply`; refuses if it cannot read the existing rows) |
| **Failure-streak columns (one-time, A.J.)** | paste `supabase/migrations/004_source_status_streaks.sql` into Supabase → SQL Editor → Run. No backfill: the next scrape fills them; until then Source health says "streaks not measurable yet" and still counts per upstream |
| Expire stale triggers (nightly job does this) | `python scripts/expire_triggers.py` (dry-run) / `--apply` |
| Regenerate the golden set (then review the diff — never to make a test pass) | `python scripts/build_golden_set.py --out tests/golden/accounts.json` |
| New-adviser trigger events (dry-run default) | `python scripts/ria_trigger.py` / `--apply` (monthly job: `run_oracles.sh`, log `logs/oracles.log`) |
| Local scrape cycle with the new feeds (writes local SQLite + alerts/ only) | `python -m src.main` (one cycle; `--daemon` loops) |
| Re-gate the queue with zero paid search | `python scripts/migrate_v2.py` (dry-run) / `--apply` |
| Full re-enrich (hits Firecrawl by default; Tavily only on fallbacks ~3%) | `python enrichment_scout.py --re-enrich` |
| Run dashboard locally | `streamlit run dashboard.py` |
| Check launchd job is loaded | `launchctl list \| grep teamalbert` |
| Reload launchd job | `launchctl unload ~/Library/LaunchAgents/com.teamalbert.enrichment.plist && launchctl load ~/Library/LaunchAgents/com.teamalbert.enrichment.plist` |
| Tail enrichment log | `tail -f logs/enrichment.log` |
| Sync config.example → config.yaml | `cp config.example.yaml config.yaml` |

---

## 6. Known issues, gotchas, and "we've been here before"

**A Supabase timeout is NOT a missing column (2026-09-11).** The 20:30 enrichment run's schema
probes timed out, the code read that as "companies_data column missing", printed the migration
SQL and exited 1 → red alert for a column that has existed since June. Now: schema errors (42703 /
42P01 / PGRST204 / "does not exist") are the only thing that reads as absent; any other probe
failure exits **2** ("Supabase unreachable or too slow — nothing processed, retries in 4h"),
`run_enrichment.sh` posts a soft ⚠️ notice instead of 🔴 FAILED, `state/enrichment_transport_aborts`
counts consecutive skips (a normal run resets it), and the health check line "Enrichment ↔
Supabase" WARNs at 2. If you ever see the migration SQL in an alert again, the column really is
gone — check Supabase before doing anything else.

**One failed run is NOT a dead source (2026-09-20).** `source_status` keeps only the latest run
per feed, and the daily check reads whichever run came last. A transient upstream error on that
one run used to read as "N errored sources" (and, in `Fetched vs filtered`, as "likely dead") —
with several feeds behind one endpoint it crossed the FAIL bar on its own. Rules now: the SEC
scraper retries EFTS and skips cleanly when it stays down; the monitor alerts on failure STREAKS
per upstream (§0b); a failed fetch is never reported as an empty feed. If Source health says
"DOWN 3+ runs in a row", that one is real. **New feeds must be verified from the CI runner, not
only from a workstation** — a publisher's bot protection can refuse datacenter addresses while
answering any other (two feeds refused this way are `enabled: false` in the config;
`tests/test_scrapers.py::REFUSED_BY_PUBLISHER` pins them).

### Architectural quirks
- **`config.yaml` is gitignored** — always edit `config.example.yaml`, then `cp` locally. GitHub Actions does this `cp` automatically in the workflow.
- **SQLite is cached between GitHub Actions runs** via `actions/cache@v4` with key `trigger-events-db-v2-*`. URL + title dedup history lives there. If the cache expires (24h TTL), the next run starts with empty dedup history — some duplicates may slip through. Rare.
- **enriched_at and grade fields live ONLY in Supabase**, never in SQLite. `supabase_sync.py` must NOT write them or you'll wipe enrichment every cycle (this bug existed — see commit `14157c2`).
- **Two scrape paths for CFO events**: SEC scraper classifies at scrape time using the pre-fetched CFO adsh set; enrichment_scout.py also reclassifies via `_has_finance_leadership_trigger()` as a backstop (catches non-SEC sources).

### Data quality
- **Title-based dedup uses EXACT normalized match** — different outlets with slight title variations slip through. Don't add fuzzy matching without A.J.'s approval — risks dropping real distinct events.
- **post-enrichment industry filter is MORE aggressive than scrape-time** because we have the structured `industry` field by then. Mining/cobalt/steel/etc. events get DELETED at this stage if they slipped past the title-only scrape filter.
- **Finance leadership override is two-layer** — once in the prompt (so LLM produces consistent justifications), once in code (so it can't be ignored). When changing one, change both.
- **The dashboard's pandas reads from Supabase return NaN for missing JSONB fields**. Always guard with `if isinstance(x, float) and x != x:` or `_v()` helper. There's a history of NaN-related bugs.

### Web-search backend: Firecrawl primary, Tavily fallback (changed 2026-06-09)

This app now uses **local self-hosted Firecrawl** (`http://localhost:3002`)
as the primary firmographic-search backend. Tavily is kept as an optional
fallback. Configured via `SEARCH_BACKEND` env var: `firecrawl` (default) or
`tavily`.

**Why we switched from Tavily**: Tavily free tier = 1,000 searches/month and
we were hitting the cap. Firecrawl is already running on A.J.'s Mac Studio
(for Scout), self-hosted, no quota.

**Persistent SQLite cache** layered on top (Phase 2, 2026-09-07 — `src/pipeline/cache.py`):
`search_cache` keyed on `(account_key, kind)` with per-kind TTL (90d firmographic), plus
`account_firmographics` (per-field TTL) and `negative_cache` (known-empty accounts, 7/30/90d
backoff). The pre-Phase-2 `firmographic_cache` table (name+hint key, 30d) is no longer read.

**ZoomInfo-style aggregator probe (2026-08-06)**: when the general search
leaves hq/revenue/size unknown, `enrich_one_company` fires ONE follow-up
ladder search with hint `'zoominfo'` and re-extracts from the combined
results (per-field merge, gaps only — never overwrites). Search-engine
SNIPPETS of public aggregator profiles carry the missing fields free of
charge: zoominfo.com `/pic/` pages → street address + headcount;
rocketreach.co → "$X million in revenue and N employees ... City, State".
No ZoomInfo login, no page scraping — reads only what engines publish in
results. Context: ZI's official MCP server + Claude connector require a
PAID ZI subscription (A.J.'s NetSuite seat is Oracle's — can't be used);
ZoomInfo Lite free tier = 10 credits/month, manual-only, and its
Community Edition upgrade harvests contact data (declined).

**Auto-fallback chain (final form 2026-08-14)**: Firecrawl → 2.5s retry →
(NO other rungs — every candidate is off-limits: **SearXNG :8888 is FLEET
infrastructure** — enrichment's fallback traffic suspended its engines
twice, outages tracking the enrichment schedule exactly, forcing the
fleet onto Brave; **Brave is RESERVED for the fleet**; **Google CSE is
CLOSED to new customers** [403 always, shutdown 2027]. This pipeline
searches ONLY via its own Firecrawl stack and its own Tavily key. Never
re-add a shared-infra or fleet-dependent rung.) →
**Tavily** (budget-guarded, `TAVILY_MONTHLY_BUDGET`). If everything is
empty, the event stays unenriched/flagged and gets retried on a later pass.
Per-run usage printed in the summary line (cache/firecrawl/tavily/
throttled). KNOWN CONSTRAINT: Firecrawl's search scrapes Google from the
Mac's single home IP — heavy bulk runs get it CAPTCHA'd/throttled, which
is what the hourly cap + circuit breaker exist to prevent.
The API-quota rungs (CSE, Tavily) are immune to IP reputation; that's why
they sit last as the true safety net, and why bulk passes should stay
paced rather than parallelized.

**Search-spend tiers (2026-08-14)** — Tavily's 900/mo is rationed by
`_event_search_tier()` from FREE pre-search signals: Tier 1 (finance-leader
events, M&A, funding ≥$10M — A.J. 2026-08-14: "even a 1M raise isn't a
company growing enough to buy NetSuite") = full ladder incl. Tavily;
Tier 2 (generic executive/other, funding $1M–$10M or undisclosed) =
Firecrawl-only, empties stay unverified for a later free retry; Tier 3
(raises <$1M) = NO searches, graded from the filing/article text alone.
Amounts parsed from titles AND descriptions ($6.8 Million / $37M / $1.2B /
Total offering: $2,500,000 all handled). Tavily rung checks the
tier; tier 3 passes `no_search=True` through `enrich_one_company`.

**IP-hygiene throttles (2026-08-09 — bulk bursts got the home IP blocked
and broke the Hermes fleet's SearXNG too):**
- `SCRAPE_HOURLY_CAP` (default 150) — cross-process sliding-window cap on
  scraped searches (Firecrawl), persisted in the cache DB
  (`scrape_calls` table) so launchd cycles + manual passes share it. Over
  cap → scrape rungs skipped, API rungs still run, counted as `throttled`
  in the run summary. Bulk passes stretch out instead of burning the IP.
- Circuit breaker — 6 consecutive all-scrape-rungs-empty searches = the
  IP is being throttled; firing more scrapes deepens the block. Opens for
  15 min (API rungs only), closes on the next scrape success.
- Search avoidance: non-workable-role companies (advisors/sellers) and
  auto-fail names (school districts) never get searched; SEC-sourced
  events seed the filer's HQ from the filing's own state code.
Never "fix" a slow bulk pass by raising SCRAPE_HOURLY_CAP into bot
territory — convert the backlog across multiple nights or add API quota
(Google CSE keys) instead.

**Important context**:
- **Scout (the `hermes-sales` Hermes agent)** uses Firecrawl directly for
  its own open-ended sales research — separate from this app's pipeline.
  Both apps now share the Firecrawl backend but for different workloads.
- **The old "use ONLY Tavily for this app" guidance is OBSOLETE**.

**Why the distinction matters:**
- **Scout (the `hermes-sales` Hermes agent)** runs open-ended sales research
  for individual prospects — it switched to **Firecrawl** in May 2026 after
  a Tavily key rotation broke its env var. That's a Scout-specific choice
  and only affects Scout.
- **This app** does bulk firmographic enrichment (~150-300 search calls per
  scrape cycle). The search-then-summarise pattern is exactly what Tavily
  is built for; Firecrawl is built for "I already know the URL, scrape this
  page." Our pipeline doesn't have URLs up front — we discover them via
  search. So Tavily is the right fit here even when other agents use
  Firecrawl.

**Resilience benefit:** If Tavily has an outage, Scout still works (Firecrawl).
If Firecrawl has an outage, this app still works (Tavily). Don't collapse
the two — keep them independent.

**Key rotation gotcha:** Tavily keys were once hardcoded across BOTH this
app and Scout's docker-compose.yml. Rotating the key broke Scout silently.
If you rotate again in the future, also update the `TAVILY_API_KEY` env
var on any Hermes container that uses Tavily — or accept that those agents
will stop working on web search until updated.

### Other dead ends / things that don't work
- **Hermes gateway is messaging-only** — port 8084 on `hermes-sales` container is for messaging, not an HTTP API. Use the shared llama.cpp server (localhost:8091, OpenAI /v1) for LLM calls, not the Hermes gateway.
- **X/Twitter monitoring** is not viable on free tier. X killed the free API in 2023. Public Nitter/RSSHub instances are unreliable. If A.J. revisits, options are $200/mo X Basic API or Apify scrapers ($20-100/mo).
- **Indeed/ZipRecruiter/SimplyHired/Ladders/CFO.com** are all bot-blocked; the Google Jobs scraper that replaced them was deleted in Phase 4 (0 verified accounts in 28 days) — Adzuna is the open-seat source. Adzuna replaces them.
- **BusinessWire RSS** now requires a registered channel ID — the legacy URL returns 0 items. If A.J. wants BW back, he must sign up free at services.businesswire.com and add the generated URL to config.

### User preferences (from MEMORY.md)
- **DC IS in territory** (not in xlsx but A.J.'s actual coverage)
- **Crypto-native businesses ARE good fits** (NetSuite + Cryptio integration). Don't block crypto feeds.
- **A.J.'s TAL rubric is the grading source of truth** (his "TAL Lead Grader" custom-GPT prompt, aligned into the pipeline 2026-07-16). If he shares a newer version, diff it against TAL_GRADING_PROMPT + TAL_V11_HASHTAG_POINTS and realign.

---

## 7. Recent change history (current state as of today's last commit)

Newest first (v2 phases on top; the older rows are the v1 history):

| Commit | What |
|---|---|
| 2026-09-26 | Feed XML repair: a feed the strict parser rejects (a publisher shipped a bare `&` in image URLs, which failed the whole feed every run) is repaired once and re-parsed — `rss_scraper.repair_feed_xml` |
| 2026-09-20 | Source health on persistence: EFTS retry + shared breaker (`_efts_get`), failed prefetch skips its item, `consecutive_failures` / `last_success` per feed (migration 004), per-upstream alerting, `Fetched vs filtered` ignores errored rows, two publisher-refused feeds disabled |
| `4a5f24b` | v2 Phase 4 (2026-09-08): accounts table + backfill, one grade per account, hashtag guards, hire-subject detection, nightly expiry, golden set in CI, orphans deleted — see §0b |
| `b3bdfb9` | v2 Phase 3 (2026-09-08): finance-leader feeds + detector fixes, Google News revived, free oracles, sec_iapd trigger, domains, supply scorecard — see §0b |
| `a57924d` / `c584331` | config.example.yaml indentation fix (had failed two Actions runs) · daily re-verify job |
| `488db2d` | Classify-then-research reorder, typed columns (+migration 002, backfill), AccountCache + negative cache, run lock + PAUSE, LLM-outage handling, yield monitoring, sync source column/relabel/reaper, dashboard server-side verify_state filter — see §0b |
| `747d54d` | v2 Phase 1: cheap-first gates, hard exclusions, rationed search, honest 'unknown' (2026-09-07) |
| `6cd73c5` | Rebuilt SEC 8-K scraper using EFTS search API + added PR Newswire Personnel/M&A feeds |
| `83cb1ca` | Expanded mega-bank exclusion list + added `cleanup_legacy_events.py` |
| `55edb64` | gitignored logs/ |
| `ba5ce3a` | Removed 8 dead RSS feeds |
| `ac17b5b` | Enrichment v2: revenue extraction + $200M dashboard filter |
| `db6402c` | Scalable revenue band filter — multiselect + presets |
| `55df34b` | 4-segment revenue taxonomy (LMM/MM/Corp/Ent) + source citation tooltips |
| `8ee573d` | Fixed extract_company_name() — no more "?" entries from funding/M&A headlines |
| `16529a1` | Critical fix: territory filter was blocking Banking (a target subindustry) |
| `1bb5861` | Adzuna job scraper replaces 5 broken HTML scrapers |
| `f774fda`, `de9f146`, `3252b68`, `9eaa7ed` | Sidebar collapse bug saga — ended with filters moved to inline `st.popover` |
| `536b57d` | Security: removed hardcoded Tavily key fallback |
| `4dbf29a` | TAL V10.2 grading + post-enrichment industry filter |
| `6f7e5ce` | Finance leadership → min Grade B + larger badge |
| `cb570ca` | SEC 8-K Item 5.02 now correctly routes CFO changes to CFO_HIRE tab |
| `e35d8de` | `--regrade-only` mode (re-grade without burning Tavily quota) |
| `c606bca` | Cobalt/lithium keywords + title-based dedup for syndicated press releases |
| `14157c2` | **4 bugs fixed from code review** — supabase_sync was clobbering user state, etype case mismatch, missing source_url in select, SEC CFO prefetch capped at 100 |

The full session is documented in detail across commits — read commit messages for context on any change. Each is self-explanatory.

---

## 8. Code review playbook

When asked to review the codebase (weekly or otherwise), use this approach. It surfaced 4 real bugs on the first run today.

**Prompt template for code review (spawn a fresh agent or do it yourself):**

```
Review the repo at /Users/andrewalbertbase/Shared/AI-BOTS/TeamAlbertTriggerEventSearch
for correctness bugs introduced since the last review.

Priority files (most-modified, highest blast radius):
- enrichment_scout.py
- dashboard.py
- src/scrapers/sec_scraper.py
- src/scrapers/base.py
- src/scrapers/adzuna_scraper.py
- src/main.py
- src/database.py
- config.example.yaml
- supabase_sync.py

LOOK FOR (high-confidence only):
- Correctness bugs (wrong logic, off-by-one, missing cases)
- Integration mismatches (shape A vs shape B between functions/scrapers/dashboard)
- NaN/None safety — pandas reads from Supabase JSONB often return NaN
- Stale comments that contradict the code
- Dead code from refactors
- Schema mismatches (Supabase column added but never read, or read but never written)
- Error handling that hides real bugs
- Security issues (re-introduced hardcoded secrets, unsafe SQL, etc.)
- Case-sensitivity bugs (event_type is LOWERCASE in storage but sometimes checked uppercase — common foot-gun)

SKIP (don't waste tokens on):
- Code style / naming
- Hypothetical scaling (this is at ~80 events, not 80k)
- Test coverage gaps (no test suite is intentional for MVP)
- Documentation completeness
- Performance micro-optimizations

FORMAT:
🔴 BUG — will cause incorrect behavior
🟡 RISK — could cause issue under conditions
⚪ NIT — worth knowing but minor

Each: file:line — one-sentence description — suggested fix
Cap report at 500 words. If you find nothing real, say so — don't manufacture findings.
```

After review:
1. Fix the 🔴 BUGs immediately
2. Address 🟡 RISKs unless they're truly low-probability
3. Skip NITs unless they have very high ROI

---

## 9. Backlog (parked items for future sessions)

These came up during today's session but were deferred. Surface them when relevant — don't auto-implement without A.J.'s approval.

| Priority | Item | Notes |
|---|---|---|
| Medium | **Post-fit email digest** (Phase 2 offer to A.J.) | Today's alert email is the raw pre-fit scrape stream (~50% noise). A Mac-side digest of fit-confirmed Grade A/B accounts would replace it. Needs A.J.'s go-ahead. |
| Medium | **More oracles** | DOL Form 5500 (state + NAICS for consumer services), NCUA credit unions, CRA T3010 (eastern Canada charities) — researched 2026-09-08, feasible as local SQLite tables like `state/oracles.db`. FDIC `/history` structure changes as a bank trigger. |
| Medium | **Adzuna recruiter blacklist** | Vaco, Robert Half, Korn Ferry, Heidrick & Struggles, JM Search, McCracken Alliance post "Hiring: CFO" for unnamed clients. Judge from the accounts table after a few weeks. |
| Low | **Fetch path for publisher-refused feeds** | Some trade publishers answer the hosted CI runner with HTTP 403 while serving non-datacenter addresses (HomeCare Magazine, Private Equity Insights — both `enabled: false`). A fetch path outside the CI runner for a config-flagged subset could revive them, and possibly some Phase 3 "blocked" rejects (re-test those from the runner first). Worth it only if Consumer Services stays the thinnest vertical. |
| Low | **Hire-detector consolidation** | `src/scrapers/base.py` and `src/pipeline/hires.py` keep two regex sets; base.py still rejects "Former X named CFO" shapes. One shared module in `src/pipeline/` (importable by CI) would end the drift. |
| Low | **Dashboard polish** | Kanban/pipeline view, hot-lead badges, saved filter presets per user, mobile responsive. |
| Low | `sec_scraper.py:31-38` Canadian SEC state-code comments (A0–A5) look wrong; the standard ON/QC/NB/NS/PE/NL codes handle Canadian filings anyway. |
---

## 10. When in doubt

- **A.J. is non-technical** — explain trade-offs in plain language, offer recommendations (don't dump options on him).
- **Stop and warn before risky actions** — if a change could leak data, drop leads, or cost real $ on APIs, STOP and offer a local-only alternative first.
- **Never amend commits** — always create new ones. Pre-commit hooks failing? Investigate; never `--no-verify`.
- **Test before shipping** — for non-trivial changes, run a small dry-run / spot-check against real Supabase data before committing.
- **Commit messages** — conventional commits style (`feat:`, `fix:`, `chore:`, `security:`). End with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` (or whatever model you are). Body should explain the WHY, not just the WHAT.

---

## 11. Project file tree (top-level)

```
TeamAlbertTriggerEventSearch/
├── CLAUDE.md                          # ← you are here
├── README.md
├── requirements.txt
├── config.example.yaml                # source of truth — edit this
├── config.yaml                        # gitignored — generated via cp
├── .env                               # gitignored — local secrets
├── .gitignore
├── assets/
│   └── logo.png                       # Team Albert branding
├── .streamlit/
│   ├── config.toml                    # dark theme
│   └── secrets.toml                   # gitignored — Streamlit Cloud secrets
├── .github/workflows/
│   └── scraper.yml                    # 4-hour cron + optional enrichment in CI
├── dashboard.py                       # Streamlit UI
├── enrichment_scout.py                # enrichment + grading
├── monitor_health.py                  # end-to-end health check (Elon runs)
├── supabase_sync.py                   # SQLite → Supabase
├── run_enrichment.sh                  # launchd wrapper for enrichment (state/PAUSE skips)
├── run_health_check.sh                # launchd wrapper for monitor_health.py
├── run_reverify.sh                    # 06:30 ET: expire_triggers --apply, then ranked re-verify
├── run_oracles.sh                     # 2nd of month 05:00 ET: refresh_oracles + ria_trigger --apply
├── scripts/                           # ALL dry-run by default; --apply writes
│   ├── migrate_v2.py                  # queue re-gate (Phase 1)
│   ├── backfill_typed_columns.py      # after migration 002
│   ├── backfill_accounts.py           # after migration 003
│   ├── refresh_oracles.py             # SEC IAPD + FDIC → state/oracles.db
│   ├── ria_trigger.py                 # new SEC-registered advisers → expansion events
│   ├── expire_triggers.py             # nightly trigger expiry
│   ├── build_golden_set.py            # exports tests/golden/accounts.json
│   └── check_feeds.py                 # feed health debug tool
├── supabase/migrations/               # 001 RLS · 002 typed columns · 003 accounts · 004 source_status streaks (A.J. runs by hand)
├── src/
│   ├── __init__.py
│   ├── main.py                        # scrape orchestration
│   ├── database.py                    # SQLite manager
│   ├── models.py                      # TriggerEvent, EventType, EventSource
│   ├── alerts.py                      # email/file alert handlers
│   ├── pipeline/                      # gates, typed, cache, oracles, domains, accounts, hires, hashtag_guards, sources, runlock
│   ├── performance/                   # async, caching, rate-limiting helpers
│   └── scrapers/
│       ├── __init__.py
│       ├── base.py                    # BaseScraper + extract_company_name
│       ├── rss_scraper.py
│       ├── sec_scraper.py             # SEC EDGAR EFTS
│       ├── adzuna_scraper.py          # Adzuna jobs API
│       └── news_scraper.py            # Google News (region-grouped queries)
├── tests/                             # ~1,600 tests; tests/golden/accounts.json = the reason-coded golden set
├── logs/                              # gitignored — enrichment.log etc.
├── alerts/                            # gitignored — text alert files
└── venv/                              # gitignored
```

---

**End of handoff. Welcome aboard.**
