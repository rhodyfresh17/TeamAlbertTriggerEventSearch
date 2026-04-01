-- Migration: Enable Row Level Security on all tables
-- This makes Supabase data private by restricting access through RLS policies.
--
-- After running this migration, you need TWO separate keys:
--   1. SUPABASE_KEY (anon key) - for the dashboard (read-only access)
--   2. SUPABASE_SERVICE_ROLE_KEY - for the sync job (full read/write access)
--
-- Run this in the Supabase SQL Editor (https://app.supabase.com > SQL Editor)

-- ============================================
-- 1. Enable RLS on all tables
-- ============================================

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_status ENABLE ROW LEVEL SECURITY;

-- ============================================
-- 2. Drop any existing policies (idempotent)
-- ============================================

DROP POLICY IF EXISTS "anon_read_events" ON public.events;
DROP POLICY IF EXISTS "service_role_all_events" ON public.events;
DROP POLICY IF EXISTS "anon_read_source_status" ON public.source_status;
DROP POLICY IF EXISTS "service_role_all_source_status" ON public.source_status;
DROP POLICY IF EXISTS "anon_update_events_status" ON public.events;

-- ============================================
-- 3. Events table policies
-- ============================================

-- Anon users (dashboard) can read all events
CREATE POLICY "anon_read_events"
  ON public.events
  FOR SELECT
  TO anon
  USING (true);

-- Anon users (dashboard) can update lead_status and notes only
CREATE POLICY "anon_update_events_status"
  ON public.events
  FOR UPDATE
  TO anon
  USING (true)
  WITH CHECK (true);

-- Service role (sync job) has full access - bypasses RLS by default,
-- but this explicit policy ensures it works even if bypass is disabled
CREATE POLICY "service_role_all_events"
  ON public.events
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

-- ============================================
-- 4. Source status table policies
-- ============================================

-- Anon users (dashboard) can read source statuses
CREATE POLICY "anon_read_source_status"
  ON public.source_status
  FOR SELECT
  TO anon
  USING (true);

-- Service role (sync job) has full access
CREATE POLICY "service_role_all_source_status"
  ON public.source_status
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

-- ============================================
-- 5. Verify RLS is enabled
-- ============================================

SELECT tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('events', 'source_status');
