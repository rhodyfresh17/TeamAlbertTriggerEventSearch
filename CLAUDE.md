# CLAUDE.md — Team Albert Sales Intelligence

You are an AI agent (Elon, or any successor) inheriting this codebase. This document is your complete onboarding. Read it end-to-end before making changes.

> 📎 **Note on filename**: This file is also accessible as `.hermes.md` (symlink) so it loads automatically when Hermes agents enter the directory. Hermes loads project context in priority order: `.hermes.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`. Editing either filename updates both (they're the same file). **Your global SOUL.md (Hermes identity, in `HERMES_HOME`) loads independently — this file does NOT override it.**

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
2. **Enriches** each event with firmographic data (Tavily + local Ollama) — extracts companies involved, industry, size, revenue, HQ, LinkedIn
3. **Grades** each event with TAL V10.2 (A/B/C/D fit score for NetSuite Up-Market)
4. **Surfaces** results on a Streamlit Cloud dashboard at https://teamalbertfy27leads.streamlit.app/ — password-protected, filterable by region, revenue segment, grade

User: **A.J. Albert** — NetSuite Up-Market Sales rep on Team Albert. Non-technical. Depends on you to write code, run commands, and explain in plain language. **Always offer local-only verification (PASS/FAIL, `${VAR}`) rather than asking him to paste secrets in chat.**

---

## 1. Architecture (data flow)

```
┌──────────────────────────────────────────────────────────────────────┐
│  GitHub Actions cron (every 4 hours, `.github/workflows/scraper.yml`) │
│  ───────────────────────────────────────────────────────────────────  │
│  1. python -m src.main                                               │
│     ├── RSSScraper        (PR Newswire, VC News Daily, etc.)        │
│     ├── GoogleNewsScraper                                            │
│     ├── JobScraper        (Google Jobs only — others bot-blocked)   │
│     ├── BingNewsScraper   (disabled, no API key)                    │
│     ├── FinSMEsScraper    (disabled, permanent 403)                 │
│     ├── SECScraper        (EFTS API, 8-K Items 5.02 / 2.01 / 1.01) │
│     └── AdzunaScraper     (free-tier API, once/day at noon UTC)     │
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
│  Per unenriched event:                                                │
│   1. LLM extracts companies + roles (Ollama qwen3-coder:30b)          │
│   2. Tavily search per unique company name                            │
│   3. LLM extracts firmographics → companies_data JSONB                │
│   4. POST-ENRICHMENT industry filter — DELETE if industry blocked     │
│      (catches mining leaks the scrape-time text filter misses)        │
│   5. TAL V10.2 grading → grade/hashtags/justification/cfo_status      │
│   6. Finance leadership override → min Grade B                        │
│   7. event_type reclassification (executive_hire → cfo_hire if        │
│      CFO/Controller/VP Finance detected in text)                      │
│   8. Writes directly to Supabase                                      │
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

### TAL V10.2 grading rules (in `enrichment_scout.py` → `TAL_GRADING_PROMPT`)

- **A** — 3+ hashtags AND 2 triggers
- **B** — 2 hashtags AND 1 trigger
- **C** — 1 hashtag
- **D** — 0 hashtags
- **Finance leadership override** — CFO/Controller/VP Finance/Head of Finance/Director of Finance hire → **minimum Grade B** (enforced both in prompt + in code defense-in-depth via `_has_finance_leadership_trigger()`)

Hashtag allowlist (max 6 per event):
`#HyperGrowth #100EE #Locations #Entities #HoldCo #Global #Franchisor #Franchisee #Funding #PEBacked #Acquisitions #FormerUser #NewCFO #PrevConvo #Legacy`

Hashtag definitions are STRICT (see prompt) — there's a history of the LLM stuffing hashtags to inflate grades. Don't loosen the definitions without A.J.'s approval.

---

## 3. Key files (in dependency order)

### Configuration
- **`config.example.yaml`** ← edit this; gitignored `config.yaml` is generated via `cp`. Holds territory, keywords, RSS feeds, excluded industries, mega-bank exclusions, Adzuna/SEC settings.
- **`.env`** (gitignored) — local secrets: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, TAVILY_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY
- **`.streamlit/secrets.toml`** (gitignored) — Streamlit Cloud secrets: SUPABASE_URL, SUPABASE_KEY (anon), DASHBOARD_PASSWORD
- **`.github/workflows/scraper.yml`** — cron schedule + Actions secrets wiring
- **`requirements.txt`** — Python deps

### Scrapers (`src/scrapers/`)
- **`base.py`** — `BaseScraper` parent class. **`extract_company_name()`** (40+ verb patterns, case-insensitive) and **`matches_industry()`** live here. Both used heavily downstream.
- **`rss_scraper.py`** — handles all RSS feeds in `config.sources.rss_feeds`
- **`sec_scraper.py`** — SEC EDGAR EFTS search. Item 5.02 (officer changes), 2.01 (M&A completion), 1.01 (material agreements). **Pre-fetches CFO-related accession numbers in one extra EFTS call, paginated to 5 pages.**
- **`adzuna_scraper.py`** — Adzuna jobs API. Throttled to noon UTC only to stay under free tier (~60 calls/month).
- **`job_scraper.py`** — Google Jobs (other boards disabled — Indeed/ZipRecruiter/SimplyHired/Ladders/CFO.com all bot-blocked)
- **`news_scraper.py`** — Google News
- **`bing_scraper.py`** — Bing News (disabled — needs paid API key)
- **`finsmes_scraper.py`** — FinSMEs (disabled — permanent 403)

### Pipeline orchestration
- **`src/main.py`** — `TriggerEventMonitor` orchestrates the scrape cycle. Two-pass dedup (URL → recent title) lives here. Wires all scrapers.
- **`src/database.py`** — SQLite manager. `has_seen_url()`, `mark_url_seen()`, `has_recent_event_title()` (the title dedup added in commit `c606bca`).
- **`src/models.py`** — `TriggerEvent` dataclass, `EventType` + `EventSource` enums.

### Enrichment + grading
- **`enrichment_scout.py`** — THE most important file outside the scraper. Reads unenriched events from Supabase, runs the 4-step pipeline (extract → search → firmographics → grade), writes back. **Has THREE modes:**
  - default — enrich only new events
  - `--re-enrich` — full re-pull (calls Tavily, costs quota)
  - `--regrade-only` — re-apply grading + industry filter + event_type reclassification using EXISTING companies_data (NO Tavily calls, free)
- **`run_enrichment.sh`** + **`~/Library/LaunchAgents/com.teamalbert.enrichment.plist`** — launchd wrapper that fires enrichment every 4 hours on the Mac.

### Sync + dashboard
- **`supabase_sync.py`** — pushes SQLite scraped events to Supabase. **Critical**: preserves user-set `lead_status` and `notes` (the bug it had previously was silently overwriting them every cycle — see commit `14157c2`).
- **`dashboard.py`** — Streamlit UI. ~1300 lines. Reads from Supabase, renders event cards by category tab, handles filtering + bulk actions. Filters live in an `st.popover` (NOT the sidebar — sidebar toggle was unreliable).

### Maintenance scripts
- **`cleanup_legacy_events.py`** — retroactively apply current filter rules + dedup to existing Supabase events. Dry-run by default; `--apply` to delete.
- **`import_leads.py`** — manual import of prospect lists (for `stable_target` event type)
- **`sheets_sync.py`** — alternative Google Sheets sync (rarely used)
- **`sync_db.py`** — S3 sync for SQLite (rarely used; GitHub Actions cache handles this normally)
- **`scripts/check_feeds.py`** — debug utility for feed health

---

## 4. Credentials map

**🔒 NEVER ask A.J. to paste secrets into chat. NEVER print/log secret values. Always offer local-only verification (PASS/FAIL, length/prefix only, `${VAR}` references).**

| Secret | Where it lives | Purpose |
|---|---|---|
| `SUPABASE_URL` | `.env`, Streamlit secrets, GitHub Secrets | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | `.env`, GitHub Secrets | Server-side writes (bypasses RLS) |
| `SUPABASE_KEY` (anon) | Streamlit secrets only | Dashboard reads |
| `TAVILY_API_KEY` | `.env`, GitHub Secrets (optional) | Web search for company enrichment. Was leaked in git history (commit `ac17b5b`), rotated in commit `536b57d`. Never re-hardcode a fallback. |
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | `.env`, GitHub Secrets | Adzuna jobs API (free tier ~100-250 calls/month) |
| `ANTHROPIC_API_KEY` | GitHub Secrets (optional) | Cloud-based LLM for enrichment fallback. If unset, enrichment uses local Ollama. |
| `DASHBOARD_PASSWORD` | Streamlit secrets | Dashboard login |
| `EMAIL_PASSWORD` + `SENDER_EMAIL` | GitHub Secrets | Email alerts (legacy — currently unused) |

---

## 4b. Ongoing health monitoring (Elon's primary maintenance job)

A single script — `monitor_health.py` — runs end-to-end diagnostics. Three modes:

| Mode | Runtime | What it checks |
|---|---|---|
| `--quick` *(default)* | ~10s | env creds, Tavily API, Ollama, Supabase reachable, scrape freshness, enrichment lag, local SQLite, launchd job loaded |
| `--daily` | ~30s | all of the above + source health (productive vs silent feeds) + 7-day-vs-prior volume trend |
| `--weekly` | ~60s | all of the above + cleanup_legacy_events.py dry-run (catches new noise patterns) |

Each check returns 🟢 PASS / 🟡 WARN / 🔴 FAIL with a one-liner. **Exit code is non-zero if any FAIL**, so cron and Elon can detect failures programmatically.

```bash
# Run from Mac terminal or inside Elon's container
python monitor_health.py            # quick
python monitor_health.py --daily
python monitor_health.py --weekly
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
| **Health check (weekly — ~60s)** | `python monitor_health.py --weekly` |
| Manual scrape cycle (locally, mirrors GitHub Actions) | `python -m src.main` |
| Enrich only NEW events | `python enrichment_scout.py` |
| Re-grade ALL events (free, no Tavily) | `python enrichment_scout.py --regrade-only` |
| Full re-enrich (uses Tavily quota!) | `python enrichment_scout.py --re-enrich` |
| Cleanup industry leaks + dupes (dry-run) | `python cleanup_legacy_events.py` |
| Cleanup — actually delete | `python cleanup_legacy_events.py --apply` |
| Run dashboard locally | `streamlit run dashboard.py` |
| Check launchd job is loaded | `launchctl list \| grep teamalbert` |
| Reload launchd job | `launchctl unload ~/Library/LaunchAgents/com.teamalbert.enrichment.plist && launchctl load ~/Library/LaunchAgents/com.teamalbert.enrichment.plist` |
| Tail enrichment log | `tail -f logs/enrichment.log` |
| Sync config.example → config.yaml | `cp config.example.yaml config.yaml` |

---

## 6. Known issues, gotchas, and "we've been here before"

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

**Persistent SQLite cache** layered on top: same company name within 30 days
doesn't re-search. Roughly 30-50% reduction in actual search calls when
companies recur across events. Cache key = `(company_name + industry_hint).lower()`,
stored in `trigger_events.db` → `firmographic_cache` table.

**Auto-fallback chain**: if Firecrawl returns empty AND Tavily key is set,
falls back to Tavily for that one call (logged). If neither responds, the
event gets empty companies_data and stays unenriched.

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
- **Hermes gateway is messaging-only** — port 8084 on `hermes-sales` container is for Telegram/Discord, not an HTTP API. Use Ollama (localhost:11434) directly for LLM calls, not the Hermes gateway.
- **X/Twitter monitoring** is not viable on free tier. X killed the free API in 2023. Public Nitter/RSSHub instances are unreliable. If A.J. revisits, options are $200/mo X Basic API or Apify scrapers ($20-100/mo).
- **Indeed/ZipRecruiter/SimplyHired/Ladders/CFO.com** are all bot-blocked. The scraper code is left in `job_scraper.py` for reference but disabled in config. Adzuna replaces them.
- **BusinessWire RSS** now requires a registered channel ID — the legacy URL returns 0 items. If A.J. wants BW back, he must sign up free at services.businesswire.com and add the generated URL to config.

### User preferences (from MEMORY.md)
- **DC IS in territory** (not in xlsx but A.J.'s actual coverage)
- **Crypto-native businesses ARE good fits** (NetSuite + Cryptio integration). Don't block crypto feeds.
- **A.J. has a lead-scoring prompt** (the TAL V10.2 in this repo — already integrated). If he mentions a new prompt, ask for it before assuming.

---

## 7. Recent change history (current state as of today's last commit)

Today's session (commit `14157c2` and back, in chronological order):

| Commit | What |
|---|---|
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
- cleanup_legacy_events.py
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
| Medium | **Consumer Services feed gap** | Auto Dealers, Real Estate, Personal Care, Repair Services — no dedicated feeds yet. Possibilities: Automotive News, GlobeSt (real estate), Cleanlink Daily. |
| Medium | **Daily digest email** | Morning email to A.J./team with top fresh Grade A+B leads. Email creds already in GitHub Secrets. |
| Medium | **Adzuna recruiter blacklist** | Vaco, Robert Half, Korn Ferry, Heidrick & Struggles, JM Search, McCracken Alliance — they post "Hiring: CFO" on behalf of unnamed clients, creating noise. Wait for ~1 week of production data before blocking. |
| Low | **Dashboard polish** | Kanban/pipeline view, hot-lead badges, saved filter presets per user, mobile responsive. |
| Low | **CLAUDE.md** has stale Canadian SEC state-code comments in `sec_scraper.py:31-38`. The codes A0-A5 are likely wrong (real EDGAR mapping differs); the standard codes ON/QC/NB/NS/PE/NL handle Canadian filings anyway. Worth cleaning up. |

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
├── cleanup_legacy_events.py           # retroactive cleanup
├── import_leads.py                    # manual lead import
├── sheets_sync.py                     # alt Google Sheets sync (rarely used)
├── sync_db.py                         # alt S3 SQLite sync (rarely used)
├── run_enrichment.sh                  # launchd wrapper for enrichment
├── run_health_check.sh                # launchd wrapper for monitor_health.py
├── scripts/
│   └── check_feeds.py                 # feed health debug tool
├── src/
│   ├── __init__.py
│   ├── main.py                        # scrape orchestration
│   ├── database.py                    # SQLite manager
│   ├── models.py                      # TriggerEvent, EventType, EventSource
│   ├── alerts.py                      # email/file alert handlers
│   ├── enrichment.py                  # (legacy enrichment, unused now)
│   ├── performance/                   # async, caching, rate-limiting helpers
│   └── scrapers/
│       ├── __init__.py
│       ├── base.py                    # BaseScraper + extract_company_name
│       ├── rss_scraper.py
│       ├── sec_scraper.py             # SEC EDGAR EFTS
│       ├── adzuna_scraper.py          # Adzuna jobs API
│       ├── job_scraper.py             # Google Jobs
│       ├── news_scraper.py            # Google News
│       ├── bing_scraper.py            # (disabled)
│       └── finsmes_scraper.py         # (disabled)
├── tests/                             # minimal — not the focus
├── logs/                              # gitignored — enrichment.log etc.
├── alerts/                            # gitignored — text alert files
└── venv/                              # gitignored
```

---

**End of handoff. Welcome aboard.**
