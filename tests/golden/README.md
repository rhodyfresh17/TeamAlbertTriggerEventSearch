# Golden set — `tests/golden/accounts.json`

**Phase 4, 2026-09-08 (hardened after the adversarial review 2026-09-08).**
A few hundred accounts whose fit decision is already settled — by A.J. (rep
verdicts) or by the pipeline (tombstone reasons, verified accounts,
hire-title classifications) — written down once so that the code can be
checked against them forever.

`tests/test_golden.py` re-derives every row's `expected` values using only
the **free** functions (no network, no LLM, no search spend, and no
`enrichment_scout` import — the pure copies live in
`src/pipeline/structured.py`):

| bucket | what the row pins | function under test |
|---|---|---|
| `rep_disposition` | every **not-fit** rep verdict in `account_dispositions` (status only) | `gates.account_key` keeps mapping the name to the same key; the status is one enrichment honours |
| `entity_shape` | a name the pipeline tombstoned as a non-operating entity | `gates.is_non_operating_entity(name)` → `(True, kind)` |
| `structured` | an SEC filing rejected from its SIC / Form D fields | `structured.structured_verdict` on the excerpt → same verdict and reason |
| `hq_out_of_territory` | an HQ string the fit gate read as out of territory | `gates.hq_territory_status(hq) == 'out'`, `gates.hq_state_code(hq)` |
| `verified` | a verified account's HQ and ZoomInfo subindustry | `hq_territory_status(hq) == 'in'`, subindustry in the allowlist |
| `finance_leader_title` | a `cfo_hire` / `executive_hire` title whose **stored event_type** is the ground truth | `src.scrapers.base.finance_leader_hire_kind(title, body[:250])` |
| `tombstone_reason` | `bad_company_name` / `no_workable_account` tombstones | `gates.is_bad_company_name`; no stored role is in `WORKABLE_ROLES` |

If any of those functions changes behaviour, the matching rows fail. That is
the point: a decision that was already made cannot flip silently.

## Where it runs

In GitHub Actions the golden set runs in its **own `tests` job** (together
with `tests/test_gates.py` and `tests/test_config.py`) on every push to
`main` and on `workflow_dispatch` — **never on the 4-hourly schedule**, and
never inside the scrape job. A golden flip means a decision changed with the
code; it must never stop a scheduled scrape or the Supabase sync.
`test_ci_shape` pins that layout. Locally, run it the way CI does:

```
venv/bin/python -m pytest -q -p no:cacheprovider tests/test_golden.py tests/test_gates.py tests/test_config.py
```

## Row shape

```json
{
  "account_key": "acme financial",          // gates.account_key(name) — the account's identity
  "name": "Acme Financial, Inc.",           // company name as stored
  "bucket": "verified",                     // which table above applies
  "source": "sec_iapd",                     // events.source (null for rep rows without an event)
  "event_type": "expansion",
  "title": "…",                             // event title as stored
  "description_excerpt": "…",               // <= 300 chars, emails/phones removed
  "hq": "Franklin, TN",                     // the HQ string the gate evaluated
  "zi_subindustry": "Lending & Brokerage",
  "sic": null,
  "source_url": null,                       // only kept for `structured` rows (the parser keys on sec.gov)
  "expected": {"territory": "in", "hq_state": "TN", "zi_subindustry": "…", "vertical": "Financial Services"},
  "provenance": "machine-seeded 2026-09-08",  // or "rep:<status>"
  "reviewed": false,
  "review_note": null
}
```

`expected` is bucket-specific — see the table. `provenance` says where the
decision came from: `rep:<status>` (a rep's own verdict) or
`machine-seeded <date>` (the pipeline's / the current code's output on that
date).

## The rules

1. **Never edit an `expected` value to make a test pass.** A failing golden
   row means the code's decision changed. Either the change is a bug (fix
   the code) or the new decision is the right one — then mark the row
   `"reviewed": true` and put the reason in `"review_note"` (e.g. `"A.J.
   2026-10-01: charter schools were re-admitted"`), and *only then* update
   `expected`. A reviewed row without a `review_note` fails the schema test.
2. **The exporter never rewrites an `expected` value on its own.**
   Re-running `build_golden_set.py` copies `reviewed: true` rows verbatim;
   an unreviewed row has its *input* fields (title, excerpt, hq, …)
   refreshed from the live data only when its `expected` is unchanged. When
   the current code — or the rep — now says something else, the row is kept
   **exactly as it was** and listed in the summary under
   `expected_changed`, with the old and new values. To accept those changes
   run the exporter with **`--rebase`** — only after a deliberate behaviour
   change, with the printed diff reviewed. A rebased row takes the fresh
   inputs, expected and provenance (`machine-seeded <today>` /
   `rep:<current status>`); reviewed rows are still never touched.
3. **Existing rows are never removed by the exporter** — delete a row by
   hand if it should go. One exception: a rep row whose status is
   `Picked Up` / `On Rep TAL` is dropped on the next run (rule 5).
4. **This file lives in a public repo.** Company names, titles, description
   excerpts, HQ strings and machine verdicts are fine. Rep names, emails,
   phone numbers, notes text or anything personal are not — the exporter
   reads `account_dispositions` as `(company_name, status)` only and scrubs
   every free-text field (`structured.scrub`: emails, and North American
   phone numbers with or without separators); the schema test rejects them
   with the same regexes.
5. **Rep verdicts: not-fit statuses only.** `rep_disposition` rows carry
   `Not a Fit`, `Out of Alignment` or `NetSuite Customer`. `Picked Up` and
   `On Rep TAL` are **never exported and are dropped if present** — the
   repo must not publish which accounts the rep is actively pursuing
   (privacy decision, review 2026-09-08). `test_no_rep_decided_rows_in_the_public_file`
   enforces it.
6. **`finance_leader_title`: the stored `event_type` is the ground truth.**
   `cfo_hire` rows expect `"hire_kind": "cfo"`; `executive_hire` rows expect
   `"exec"`, or `null` when the title carries a strong hire verb but names no
   finance role at all (a CEO / COO / President hire the finance detector
   must stay silent on; a bare SEC 8-K headline pins nothing). A row the
   detector cannot reproduce from the stored title + excerpt is skipped at
   build time, exactly like every other bucket — the detector's current
   output is never pinned as truth. `finance_seat_open` (Adzuna "X hiring:
   Chief Financial Officer" postings) is excluded: the Adzuna scraper types
   those, `finance_leader_hire_kind` never sees them in production, and
   pinning its output on them pinned an inverted signal ("Executive
   Assistant to the CFO" as a CFO hire — the detector bug fixed the same
   day). The exporter prefers rows whose **only** hire signal is a strong
   verb (names / appoints / taps / promotes …), so reverting the verb list
   flips them; `test_finance_leader_bucket_flips_on_a_verb_list_revert`
   requires at least 10 such rows.
7. Add rows by hand whenever a decision is worth pinning: copy a row of the
   same bucket, fill `name` / inputs / `expected`, set `provenance` to
   `rep:<status>` or `machine-seeded <date>`, and let `account_key` be
   `gates.account_key(name)` (the schema test checks it).

## Regenerating / extending

```
venv/bin/python scripts/build_golden_set.py --dry-run                 # summary only, writes nothing
venv/bin/python scripts/build_golden_set.py --out tests/golden/accounts.json
venv/bin/python scripts/build_golden_set.py --rebase                  # accept expected_changed rows (diff reviewed)
venv/bin/python -m pytest -q tests/test_golden.py
```

The exporter is **read-only** against Supabase (service key from `.env`,
`.select()` calls only). It samples each bucket deterministically (hash of
the event id, interleaved across verdict / state / source so no stratum
dominates), verifies every candidate against the free function **at build
time** — a stored decision the current code cannot reproduce from the
stored inputs is skipped and counted in the summary, never written as a
failing row — then merges with the existing file (rules above) and sorts by
`account_key`. Re-running on unchanged data rewrites an identical file.

Targets: every not-fit rep verdict, 40 per sampled bucket
(`--n-per-bucket`), 20 `tombstone_reason` rows. **`--max-new-rows`**
(default 250; `--max-rows` is accepted as an alias) caps the number of
*new sampled rows added per run* — it never caps the file, and rep rows are
exempt, so the file keeps growing as decisions accumulate and every not-fit
rep verdict is always present.

## What could not be seeded (2026-09-08)

- The three tombstones whose reason *starts* with `entity_shape:` carry no
  `companies_data` (that gate fires before research), and their
  `company_name` is the job-board poster, not the entity — so the bucket is
  drawn from `fit_gate:` tombstones that name the entity and its kind.
- `structured:item_1.01 without acquisition language` tombstones are decided
  from the filing's full text, not from anything stored on the row.
- Older `structured:` tombstones whose description predates the embedded
  `SIC: NNNN` / `SPAC: yes.` phrases cannot be reproduced offline.
- `sec_iapd` events are excluded from `entity_shape`: an SEC-registered
  adviser's "… LP" name is the management company (fund-vehicle exemption,
  review 2026-09-08), so the old tombstone is no longer the pipeline's
  decision.
- `bad_company_name` tombstones with a blank name pin nothing and are
  skipped.
- `cfo_hire` / `executive_hire` events whose title the detector cannot type
  (an SEC 8-K Item 5.02 headline, an earnings release quoting the CFO) are
  skipped — the stored type came from the filing text or the body, not from
  the title the test can see.
