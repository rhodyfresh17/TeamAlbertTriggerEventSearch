-- Migration 002: v2 typed columns (Phase 2, 2026-09-07)
--
-- WHAT THIS DOES, in plain language:
--   Until now everything the enrichment learned about an event lived inside
--   two JSON blobs ("fit" and "companies_data"). The dashboard had to unpack
--   JSON on every read and Supabase could not index or filter on any of it.
--   This migration adds ordinary columns next to the JSON (account key,
--   verdict, HQ state, expiry date, retry bookkeeping, SEC facts...) and
--   fills the ones that can be derived in SQL from data already in the row.
--
-- SAFE TO RE-RUN: every statement is "IF NOT EXISTS" and the backfill only
--   touches rows where the new column is still NULL. Running it twice does
--   nothing the second time.
-- ADDS COLUMNS ONLY: no table is dropped, no row is deleted, no data is
--   overwritten, and NO Row Level Security setting or policy is changed
--   (the 001 policies stay exactly as they are).
-- TAKES SECONDS on a table of a few thousand rows.
--
-- AFTER RUNNING: the Python-only values (hq_state, revenue_segment,
--   account_key, sic / formd_* and the Adzuna "finance_seat_open" relabel)
--   are filled by:   venv/bin/python scripts/backfill_typed_columns.py --apply
--   (dry-run first without --apply to see the before/after counts).
--
-- Run this in the Supabase SQL Editor (https://app.supabase.com > SQL Editor)

-- ============================================
-- 1. events: add the typed columns
-- ============================================

ALTER TABLE public.events ADD COLUMN IF NOT EXISTS source TEXT;                      -- sec_edgar | adzuna | google_news | pr_newswire | globe_newswire | business_wire | linkedin | other
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS account_key TEXT;                 -- normalized account identity (gates.account_key)
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS fit_verdict TEXT;                 -- pass | unverified | staged | decided | fail
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS verify_state TEXT;                -- verified | researched_ambiguous | staged | decided | not_fit
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS hq_state TEXT;                    -- 2-letter state / province code
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS in_territory TEXT;                -- in | out | unknown
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS vertical TEXT;                    -- in | out | unknown
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS zi_subindustry TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS revenue_segment TEXT;             -- LMM | MM | Corp | Enterprise
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;           -- trigger shelf life
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS sic TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS formd_industry_group TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS formd_revenue_range TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS formd_offering_amount NUMERIC;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS formd_is_spac BOOLEAN;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS enrich_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ;          -- do not re-process before this
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS classification_confidence TEXT;   -- High | Medium | Low
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS classified_by TEXT;               -- structured | cache | article | search

-- ============================================
-- 2. source_status: raw-vs-kept counters
-- ============================================

ALTER TABLE public.source_status ADD COLUMN IF NOT EXISTS items_fetched INTEGER;     -- raw candidates the source returned
ALTER TABLE public.source_status ADD COLUMN IF NOT EXISTS filtered_out INTEGER;      -- dropped by the scrape-time gates (events_found stays = kept)

-- ============================================
-- 3. Indexes (the dashboard / enrichment filters)
-- ============================================

CREATE INDEX IF NOT EXISTS events_verify_state_idx   ON public.events (verify_state);
CREATE INDEX IF NOT EXISTS events_fit_verdict_idx    ON public.events (fit_verdict);
CREATE INDEX IF NOT EXISTS events_account_key_idx    ON public.events (account_key);
CREATE INDEX IF NOT EXISTS events_discovered_at_idx  ON public.events (discovered_at DESC);
CREATE INDEX IF NOT EXISTS events_retry_after_idx    ON public.events (retry_after);
CREATE INDEX IF NOT EXISTS events_source_idx         ON public.events (source);
-- Visible rows only (tombstones carry blocked_at) — the dashboard's main scan
CREATE INDEX IF NOT EXISTS events_visible_discovered_at_idx
  ON public.events (discovered_at DESC)
  WHERE blocked_at IS NULL;

-- ============================================
-- 4. One-shot backfill from the JSON already in the row (NULLs only)
-- ============================================

-- 4a. fit → fit_verdict / in_territory / vertical / zi_subindustry
UPDATE public.events
SET fit_verdict = NULLIF(lower(fit->>'verdict'), '')
WHERE fit_verdict IS NULL AND fit IS NOT NULL;

UPDATE public.events
SET in_territory = fit->>'territory'
WHERE in_territory IS NULL AND (fit->>'territory') IN ('in', 'out', 'unknown');

UPDATE public.events
SET vertical = fit->>'vertical'
WHERE vertical IS NULL AND (fit->>'vertical') IN ('in', 'out', 'unknown');

UPDATE public.events
SET zi_subindustry = NULLIF(fit->>'zi_subindustry', '')
WHERE zi_subindustry IS NULL AND fit IS NOT NULL;

-- 4b. verify_state (tombstones are not_fit whatever the verdict says)
UPDATE public.events
SET verify_state = CASE
      WHEN blocked_at IS NOT NULL                THEN 'not_fit'
      WHEN lower(fit->>'verdict') = 'pass'       THEN 'verified'
      WHEN lower(fit->>'verdict') = 'unverified' THEN 'researched_ambiguous'
      WHEN lower(fit->>'verdict') = 'staged'     THEN 'staged'
      WHEN lower(fit->>'verdict') = 'decided'    THEN 'decided'
      WHEN lower(fit->>'verdict') = 'fail'       THEN 'not_fit'
      ELSE NULL
    END
WHERE verify_state IS NULL;

-- 4c. source from the URL. sec.gov is 'sec_edgar' whether 8-K or Form D —
--     `source` is the scrape enum; readers tell Form D apart by the title.
UPDATE public.events
SET source = CASE
      WHEN source_url ILIKE '%sec.gov%' AND title ILIKE '%form d%'        THEN 'sec_edgar'   -- Form D: same enum, title tells it apart
      WHEN source_url ILIKE '%sec.gov%'                                   THEN 'sec_edgar'
      WHEN source_url ILIKE '%adzuna%'                                    THEN 'adzuna'
      WHEN source_url ILIKE '%news.google%'                               THEN 'google_news'
      WHEN source_url ILIKE '%google.com/url%'                            THEN 'google_news'  -- the Google News scraper stores redirect links
      WHEN source_url ILIKE '%prnewswire%'                                THEN 'pr_newswire'
      WHEN source_url ILIKE '%globenewswire%'                             THEN 'globe_newswire'
      WHEN source_url ILIKE '%businesswire%'                              THEN 'business_wire'
      ELSE 'other'
    END
WHERE source IS NULL;

-- 4d. expires_at = published_date (fallback discovered_at) + shelf life
--     (same table as src/pipeline/typed.py EXPIRY_DAYS)
-- (published_date / discovered_at are ISO strings; the regex guard skips
--  blanks or junk so one odd row cannot abort the whole statement)
UPDATE public.events
SET expires_at = COALESCE(
      CASE WHEN published_date::text ~ '^\d{4}-\d{2}-\d{2}' THEN published_date::text::timestamptz END,
      CASE WHEN discovered_at::text  ~ '^\d{4}-\d{2}-\d{2}' THEN discovered_at::text::timestamptz  END)
    + CASE event_type
        WHEN 'cfo_hire'           THEN INTERVAL '60 days'
        WHEN 'finance_seat_open'  THEN INTERVAL '60 days'
        WHEN 'executive_hire'     THEN INTERVAL '60 days'
        WHEN 'funding'            THEN INTERVAL '60 days'
        WHEN 'merger_acquisition' THEN INTERVAL '120 days'
        WHEN 'expansion'          THEN INTERVAL '90 days'
        WHEN 'stable_target'      THEN INTERVAL '365 days'
        ELSE                           INTERVAL '60 days'
      END
WHERE expires_at IS NULL
  AND (published_date::text ~ '^\d{4}-\d{2}-\d{2}' OR discovered_at::text ~ '^\d{4}-\d{2}-\d{2}');

-- ============================================
-- 5. Show what was added (you should see 21 rows)
-- ============================================

SELECT table_name, column_name, data_type, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
    (table_name = 'events' AND column_name IN (
      'source', 'account_key', 'fit_verdict', 'verify_state', 'hq_state',
      'in_territory', 'vertical', 'zi_subindustry', 'revenue_segment',
      'expires_at', 'sic', 'formd_industry_group', 'formd_revenue_range',
      'formd_offering_amount', 'formd_is_spac', 'enrich_attempts',
      'retry_after', 'classification_confidence', 'classified_by'))
    OR
    (table_name = 'source_status' AND column_name IN ('items_fetched', 'filtered_out'))
  )
ORDER BY table_name, column_name;
