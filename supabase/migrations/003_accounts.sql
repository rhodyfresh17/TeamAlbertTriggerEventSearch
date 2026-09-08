-- Migration 003: the ACCOUNTS table (Phase 4 slice C1, 2026-09-08)
--
-- WHAT THIS DOES, in plain language:
--   Until now the company a rep would sell into only existed as a by-product
--   of events: every event row carried its own copy of the company's facts,
--   its own grade, and the rep's verdict lived in a small side table keyed by
--   a name the dashboard normalized its own way. This migration creates ONE
--   new table, "accounts", with one row per company: the best-known facts
--   (HQ, subindustry, vertical, revenue band, size, website), ONE grade (the
--   best live trigger's), the trigger it came from, how many events we have
--   seen for it (each counted once — the row remembers the ids it folded in),
--   and the rep's disposition with a required reason.
--
-- SAFE TO RE-RUN: every statement is "IF NOT EXISTS". Running it twice does
--   nothing the second time. Nothing is dropped, no row is deleted, and the
--   events / source_status / account_dispositions tables are NOT touched.
-- CREATES ONE TABLE ONLY, with Row Level Security ENABLED and ZERO policies —
--   the same posture as every other table here: only the service-role key
--   (the Mac-side scripts) can read or write it; the anon key sees nothing.
-- TAKES SECONDS.
--
-- AFTER RUNNING: fill it from the events already in Supabase with
--     venv/bin/python scripts/backfill_accounts.py            (dry run: prints what it WOULD write)
--     venv/bin/python scripts/backfill_accounts.py --apply    (writes ~2,250 rows in batches of 100)
--   The script refuses to run until this table exists, and never touches events.
--
-- Run this in the Supabase SQL Editor (https://app.supabase.com > SQL Editor)

-- ============================================
-- 1. accounts: one row per company (account_key = src.pipeline.gates.account_key(canonical_name))
-- ============================================

CREATE TABLE IF NOT EXISTS public.accounts (
  -- identity
  account_key               TEXT PRIMARY KEY,                    -- gates.account_key(canonical_name): lowercase, suffixes/punctuation stripped
  canonical_name            TEXT,                                -- the company name as enrichment chose it
  aliases                   JSONB,                               -- other spellings seen for the same key, e.g. ["Acme Bancorp, Inc."]
  domain                    TEXT,                                -- website domain (src/pipeline/domains.py)
  domain_method             TEXT,                                -- how the domain was found: clearbit | oracle | hint_url | cache
  -- facts (fill-only; a stronger provenance may overwrite — src/pipeline/accounts.py merge_account)
  hq                        TEXT,
  hq_state                  TEXT,                                -- 2-letter state / province code
  in_territory              TEXT,                                -- in | out | unknown
  zi_subindustry            TEXT,                                -- ZoomInfo subindustry (FY27 taxonomy)
  vertical                  TEXT,                                -- Financial Services | Nonprofits & Organizations | Consumer Services (the LABEL — the events column is in|out|unknown)
  industry                  TEXT,                                -- free-text industry from the extraction
  revenue_segment           TEXT,                                -- LMM | MM | Corp | Enterprise
  size_bucket               TEXT,                                -- 1-10 | 11-50 | 51-200 | 201-500 | 501-1000 | 1001-5000 | 5001-10000 | 10000+
  entity_class              TEXT,                                -- operating | fund_vehicle | spac | political | government | k12 | lodging | greek
  -- research state
  fit_verdict               TEXT,                                -- pass | unverified | staged | decided | fail
  verify_state              TEXT,                                -- verified | researched_ambiguous | staged | decided | not_fit
  enrich_attempts           INTEGER NOT NULL DEFAULT 0,
  retry_after               TIMESTAMPTZ,                         -- researched_ambiguous: do not re-research before this
  classified_by             TEXT,                                -- structured | oracle | search | cache | article
  classification_confidence TEXT,                                -- High | Medium | Low
  firmographics             JSONB,                               -- the chosen company dict: url, linkedin, size, revenue_source, field_sources, registry_source...
  -- the ONE grade (the best live trigger's)
  grade                     TEXT,                                -- A | B | C | D
  numeric_score             INTEGER,
  confidence_level          TEXT,                                -- High | Medium | Low
  hashtags                  JSONB,                               -- ["#NewCFO", ...]
  grade_justification       TEXT,
  graded_event_id           TEXT,                                -- events.id the grade came from
  graded_at                 TIMESTAMPTZ,
  -- triggers
  best_trigger_type         TEXT,                                -- cfo_hire | finance_seat_open | merger_acquisition | funding | expansion | executive_hire | stable_target | other
  best_trigger_at           TIMESTAMPTZ,                         -- the trigger's own date; shelf life = typed.EXPIRY_DAYS from here
  best_trigger_event_id     TEXT,
  event_count               INTEGER NOT NULL DEFAULT 0,          -- distinct events folded into this row, each counted ONCE (see seen_event_ids); tombstoned ones included
  seen_event_ids            JSONB,                               -- the last 50 events.id values merged in, newest last: how a re-processed event is recognised and NOT counted again (review 2026-09-08, Phase 4)
  last_event_at             TIMESTAMPTZ,
  -- the rep's verdict (rep-owned: only set_disposition writes these; merges never do)
  disposition               TEXT,                                -- Picked Up | On Rep TAL | NetSuite Customer | Out of Alignment | Not a Fit
  disposition_reason        TEXT,                                -- wrong_vertical | out_of_territory | too_big | too_small | not_a_trigger | duplicate | existing_customer | other  (required for Not a Fit / Out of Alignment)
  disposition_notes         TEXT,
  disposition_at            TIMESTAMPTZ,
  disposition_by            TEXT,
  -- bookkeeping
  active                    BOOLEAN NOT NULL DEFAULT TRUE,
  first_seen                TIMESTAMPTZ,                         -- earliest discovered_at among its events
  last_seen                 TIMESTAMPTZ,                         -- latest discovered_at among its events
  created_at                TIMESTAMPTZ DEFAULT now(),
  updated_at                TIMESTAMPTZ DEFAULT now()            -- stamped by the app on every write (merge, disposition, backfill); nothing database-side maintains it
);

-- ============================================
-- 2. Row Level Security: enabled, no policies (service role only)
-- ============================================

ALTER TABLE public.accounts ENABLE ROW LEVEL SECURITY;

-- ============================================
-- 3. Indexes (the dashboard's account views and the monitor's mix checks)
-- ============================================

CREATE INDEX IF NOT EXISTS accounts_verify_state_idx   ON public.accounts (verify_state);
CREATE INDEX IF NOT EXISTS accounts_grade_idx          ON public.accounts (grade);
CREATE INDEX IF NOT EXISTS accounts_disposition_idx    ON public.accounts (disposition);
CREATE INDEX IF NOT EXISTS accounts_last_event_at_idx  ON public.accounts (last_event_at DESC);
CREATE INDEX IF NOT EXISTS accounts_hq_state_idx       ON public.accounts (hq_state);

-- ============================================
-- 4. Show what was created (you should see 44 rows, one per column)
-- ============================================

SELECT ordinal_position, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'accounts'
ORDER BY ordinal_position;
