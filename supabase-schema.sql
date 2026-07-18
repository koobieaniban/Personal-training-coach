-- ── Hyrox Coach — Supabase Schema ─────────────────────────────────────────────
-- Run this entire file in Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- Profiles (one per user)
CREATE TABLE IF NOT EXISTS public.profiles (
  id            UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  name          TEXT,
  email         TEXT,
  goal_time_min INTEGER DEFAULT 90,
  age           INTEGER,
  race_date     DATE DEFAULT '2026-07-05',
  plan_start    DATE DEFAULT '2026-05-18',
  sync_method   TEXT DEFAULT 'manual',
  intervals_icu_id  TEXT,
  intervals_icu_key TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Training state (completions + pace adjustments per user)
CREATE TABLE IF NOT EXISTS public.training_state (
  user_id    UUID PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  completions JSONB DEFAULT '{}',
  pace_adj    JSONB DEFAULT '{}',
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Session feedback (one row per user per session date)
CREATE TABLE IF NOT EXISTS public.session_feedback (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  session_date         DATE NOT NULL,
  session_title        TEXT,
  overall_feel         INTEGER,
  pace_feel            INTEGER,
  knee_pain            INTEGER,
  notes                TEXT,
  splits               JSONB,
  actual_run_pace_sec  NUMERIC,
  actual_station_sec   JSONB,
  station_rpe          JSONB,
  created_at           TIMESTAMPTZ DEFAULT NOW(),
  updated_at           TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, session_date)
);

-- Workout data (from Garmin/Intervals.icu/file upload/screenshot)
CREATE TABLE IF NOT EXISTS public.workout_data (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  session_date DATE NOT NULL,
  source       TEXT, -- 'intervals_icu' | 'fit_file' | 'tcx_file' | 'gpx_file' | 'screenshot' | 'manual'
  raw_data     JSONB,
  avg_pace_sec NUMERIC,
  avg_hr       NUMERIC,
  distance_m   NUMERIC,
  laps         JSONB,
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, session_date)
);

-- ── Profile column additions (safe to re-run) ────────────────────────────────
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS race_category     TEXT;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS training_level    TEXT DEFAULT 'intermediate';
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS sessions_per_week INTEGER;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS training_days     TEXT;
ALTER TABLE public.session_feedback ADD COLUMN IF NOT EXISTS station_weights JSONB;

-- ── Plan switcher + running plan columns (safe to re-run) ───────────────────
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS active_plan        TEXT DEFAULT 'hyrox';
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS race_distance      TEXT;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS goal_time_sec      INTEGER;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS estimated_lthr     INTEGER;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS lthr_source        TEXT;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS running_race_date  DATE;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS running_plan_start DATE;
ALTER TABLE public.training_state ADD COLUMN IF NOT EXISTS running_completions JSONB DEFAULT '{}';
ALTER TABLE public.training_state ADD COLUMN IF NOT EXISTS running_pace_adj    JSONB DEFAULT '{}';

-- ── Running baseline (progressive pacing) ───────────────────────────────────
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS baseline_pace_sec INTEGER;

-- ── Hyrox 10K running baseline (drives the block-periodized run-pace ramp) ───
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS hyrox_10k_sec INTEGER;

-- ── Hybrid (general-fitness, no-race) plan columns (safe to re-run) ─────────
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS hybrid_days       INTEGER;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS hybrid_5k_sec     INTEGER;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS hybrid_start      DATE;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS hybrid_equipment  TEXT DEFAULT 'full_gym';
ALTER TABLE public.training_state ADD COLUMN IF NOT EXISTS hybrid_completions  JSONB DEFAULT '{}';
ALTER TABLE public.training_state ADD COLUMN IF NOT EXISTS hybrid_pace_adj     JSONB DEFAULT '{}';
ALTER TABLE public.training_state ADD COLUMN IF NOT EXISTS hybrid_strength_log JSONB DEFAULT '{}';

-- ── Garmin credentials (encrypted at rest, service-role access only) ─────────
-- RLS is enabled but NO user-facing policies — only Railway service_role key can read/write.
CREATE TABLE IF NOT EXISTS public.garmin_credentials (
  user_id              UUID PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  garmin_email         TEXT        NOT NULL,
  garmin_password_enc  TEXT        NOT NULL,  -- Fernet-encrypted; key lives only in Railway env
  garmin_tokens_enc    TEXT,                  -- optional cached OAuth tokens
  last_synced_at       TIMESTAMPTZ,
  sync_enabled         BOOLEAN     DEFAULT TRUE,
  created_at           TIMESTAMPTZ DEFAULT NOW(),
  updated_at           TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE public.garmin_credentials ENABLE ROW LEVEL SECURITY;
-- Intentionally no RLS policies — service_role bypasses RLS, anon/authenticated are denied.

-- ── Row Level Security ────────────────────────────────────────────────────────

ALTER TABLE public.profiles        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.training_state  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.session_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workout_data    ENABLE ROW LEVEL SECURITY;

-- Drop existing policies before recreating (safe to re-run)
DROP POLICY IF EXISTS "Own profile select"  ON public.profiles;
DROP POLICY IF EXISTS "Own profile insert"  ON public.profiles;
DROP POLICY IF EXISTS "Own profile update"  ON public.profiles;
DROP POLICY IF EXISTS "Own training state"  ON public.training_state;
DROP POLICY IF EXISTS "Own feedback"        ON public.session_feedback;
DROP POLICY IF EXISTS "Own workout data"    ON public.workout_data;

-- Profiles
CREATE POLICY "Own profile select" ON public.profiles FOR SELECT USING (auth.uid() = id);
CREATE POLICY "Own profile insert" ON public.profiles FOR INSERT WITH CHECK (auth.uid() = id);
CREATE POLICY "Own profile update" ON public.profiles FOR UPDATE USING (auth.uid() = id);

-- Training state
CREATE POLICY "Own training state" ON public.training_state FOR ALL USING (auth.uid() = user_id);

-- Session feedback
CREATE POLICY "Own feedback" ON public.session_feedback FOR ALL USING (auth.uid() = user_id);

-- Workout data
CREATE POLICY "Own workout data" ON public.workout_data FOR ALL USING (auth.uid() = user_id);

-- ── Grant table access to authenticated users ────────────────────────────────

GRANT SELECT, INSERT, UPDATE, DELETE ON public.profiles         TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.training_state   TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.session_feedback TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.workout_data     TO authenticated;

-- ── Auto-create profile on signup ────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO public.profiles (id, email, name)
  VALUES (new.id, new.email, COALESCE(new.raw_user_meta_data->>'name', split_part(new.email, '@', 1)));

  INSERT INTO public.training_state (user_id)
  VALUES (new.id);

  RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
