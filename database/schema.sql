-- ============================================================================
-- AI-JobScout Multi-User Supabase Migration Schema
-- Execute this entire script in your Supabase SQL Editor
-- ============================================================================

-- Enable Cryptographic Extension for Password Hashing
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 1. Users Table (Stores user credentials & hashed passwords)
CREATE TABLE IF NOT EXISTS public.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL
);

-- 2. Profiles Table (Maintains user metadata and foreign key consistency)
CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    full_name TEXT DEFAULT '',
    password_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL
);

-- In case profiles already exists with foreign key to auth.users, safely unlink it:
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_id_fkey;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_email_key;
ALTER TABLE public.profiles ADD CONSTRAINT profiles_email_key UNIQUE (email);
ALTER TABLE public.profiles ALTER COLUMN id SET DEFAULT gen_random_uuid();

-- 3. User Telegram Settings
CREATE TABLE IF NOT EXISTS public.user_telegram_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    bot_token TEXT,
    chat_id TEXT,
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    CONSTRAINT unique_user_telegram UNIQUE (user_id)
);

-- 4. User Job Preferences (Locations, Roles, Experience)
CREATE TABLE IF NOT EXISTS public.user_job_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    locations JSONB DEFAULT '[]'::jsonb NOT NULL,
    roles JSONB DEFAULT '[]'::jsonb NOT NULL,
    experience_level TEXT DEFAULT 'fresher' NOT NULL,
    skills JSONB DEFAULT '[]'::jsonb,
    keywords JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    CONSTRAINT unique_user_preferences UNIQUE (user_id)
);

-- 5. User Scheduler Settings
CREATE TABLE IF NOT EXISTS public.user_scheduler_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    report_time TEXT DEFAULT '09:41' NOT NULL, -- Format: HH:MM
    timezone TEXT DEFAULT 'Asia/Kolkata' NOT NULL,
    scrape_interval_hours INT DEFAULT 24 NOT NULL,
    is_enabled BOOLEAN DEFAULT TRUE NOT NULL,
    last_dispatched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    CONSTRAINT unique_user_scheduler UNIQUE (user_id)
);

-- 6. Master Raw Jobs Pool (Shared Scraped Cache)
CREATE TABLE IF NOT EXISTS public.scraped_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    source TEXT NOT NULL, -- 'LinkedIn' | 'Naukri'
    link TEXT NOT NULL,
    experience TEXT,
    description TEXT,
    posted_date TEXT,
    fingerprint TEXT NOT NULL UNIQUE, -- Normalized URL or hash for deduplication
    created_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL
);

-- 7. User Delivered Jobs (Per-User Deduplication History)
CREATE TABLE IF NOT EXISTS public.user_delivered_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    job_id UUID REFERENCES public.scraped_jobs(id) ON DELETE CASCADE,
    job_link TEXT NOT NULL,
    match_score INT,
    delivered_at TIMESTAMPTZ DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL,
    CONSTRAINT unique_user_job UNIQUE (user_id, job_link)
);

-- 8. Trigger to automatically provision records when a user registers in public.users
CREATE OR REPLACE FUNCTION public.handle_custom_user_created()
RETURNS trigger AS $$
BEGIN
  -- Sync user into public.profiles
  INSERT INTO public.profiles (id, email, full_name, password_hash)
  VALUES (new.id, new.email, new.full_name, new.password_hash)
  ON CONFLICT (id) DO UPDATE SET 
    email = EXCLUDED.email, 
    full_name = EXCLUDED.full_name,
    password_hash = EXCLUDED.password_hash;

  -- Auto-provision initial job preferences
  INSERT INTO public.user_job_preferences (user_id, locations, roles, experience_level)
  VALUES (new.id, '[]'::jsonb, '[]'::jsonb, 'fresher')
  ON CONFLICT (user_id) DO NOTHING;

  -- Auto-provision telegram record
  INSERT INTO public.user_telegram_settings (user_id)
  VALUES (new.id)
  ON CONFLICT (user_id) DO NOTHING;

  -- Auto-provision scheduler record
  INSERT INTO public.user_scheduler_settings (user_id, report_time, scrape_interval_hours)
  VALUES (new.id, '09:41', 24)
  ON CONFLICT (user_id) DO NOTHING;

  RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Drop trigger if exists then recreate
DROP TRIGGER IF EXISTS on_custom_user_created ON public.users;
CREATE TRIGGER on_custom_user_created
  AFTER INSERT ON public.users
  FOR EACH ROW EXECUTE PROCEDURE public.handle_custom_user_created();


-- ============================================================================
-- 9. Row Level Security (RLS) Policies
-- Prevents anon-key users from reading other users' data.
-- Service role key (used by background scheduler) bypasses RLS automatically.
-- ============================================================================

-- Users table: only the user themselves can read their own row
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS users_self_access ON public.users;
CREATE POLICY users_self_access ON public.users
  FOR ALL USING (auth.uid() = id OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Profiles table
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS profiles_self_access ON public.profiles;
CREATE POLICY profiles_self_access ON public.profiles
  FOR ALL USING (auth.uid() = id OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Telegram settings: only the user can see their own bot token and chat ID
ALTER TABLE public.user_telegram_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS telegram_self_access ON public.user_telegram_settings;
CREATE POLICY telegram_self_access ON public.user_telegram_settings
  FOR ALL USING (auth.uid() = user_id OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Job preferences
ALTER TABLE public.user_job_preferences ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS preferences_self_access ON public.user_job_preferences;
CREATE POLICY preferences_self_access ON public.user_job_preferences
  FOR ALL USING (auth.uid() = user_id OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Scheduler settings
ALTER TABLE public.user_scheduler_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scheduler_self_access ON public.user_scheduler_settings;
CREATE POLICY scheduler_self_access ON public.user_scheduler_settings
  FOR ALL USING (auth.uid() = user_id OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Scraped jobs: public read for all authenticated users, insert/update via service role
ALTER TABLE public.scraped_jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scraped_jobs_read ON public.scraped_jobs;
CREATE POLICY scraped_jobs_read ON public.scraped_jobs
  FOR SELECT USING (true);
DROP POLICY IF EXISTS scraped_jobs_write ON public.scraped_jobs;
CREATE POLICY scraped_jobs_write ON public.scraped_jobs
  FOR INSERT WITH CHECK (current_setting('request.jwt.claim.role', true) = 'service_role');

-- Delivered jobs: user can read their own, service role can write
ALTER TABLE public.user_delivered_jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS delivered_self_access ON public.user_delivered_jobs;
CREATE POLICY delivered_self_access ON public.user_delivered_jobs
  FOR ALL USING (auth.uid() = user_id OR current_setting('request.jwt.claim.role', true) = 'service_role');
