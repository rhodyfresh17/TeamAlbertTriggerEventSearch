-- Migration 004: failure streaks on source_status
--
-- WHAT THIS DOES, in plain language:
--   source_status holds ONE row per news/SEC/jobs source, overwritten on
--   every scrape. A reader therefore only ever saw "did the most recent run
--   work?" and could not tell a single failed run from a source that has
--   been down for days. This migration adds two columns the scraper now
--   fills in:
--     consecutive_failures  how many runs in a row the source ended in error
--                           (0 after any run that worked)
--     last_success          when the source last ended a run without an error
--   The health check uses them to alert on a source that STAYS down instead
--   of on whichever run happened to come last.
--
-- SAFE TO RE-RUN: both statements are "IF NOT EXISTS". Running it twice does
--   nothing the second time.
-- ADDS COLUMNS ONLY: no table is dropped, no row is deleted, no data is
--   overwritten, and NO Row Level Security setting or policy is changed
--   (the 001 policies stay exactly as they are).
-- TAKES UNDER A SECOND. No backfill is needed — the next scrape fills the
--   columns; until then they read as "unknown" and every reader copes.
--
-- Run this in the Supabase SQL Editor (https://app.supabase.com > SQL Editor)

ALTER TABLE public.source_status ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER;  -- runs in a row ending in status 'error'; 0 = last run worked; NULL = not measured yet
ALTER TABLE public.source_status ADD COLUMN IF NOT EXISTS last_success TEXT;             -- same naive-ISO text format as last_check; NULL = never succeeded since tracking began

-- ============================================
-- Verify (should list both columns)
-- ============================================
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'source_status'
  AND column_name IN ('consecutive_failures', 'last_success')
ORDER BY column_name;
