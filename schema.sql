-- =============================================================
-- MAYA - Supabase database schema
-- Spec: section 9 (Memory and accounts: Supabase)
-- Run this whole file once in the Supabase SQL Editor of the
-- Maya project (Mumbai region, ap-south-1). Safe to re-run.
-- =============================================================

-- Needed for gen_random_uuid()
create extension if not exists pgcrypto;

-- -------------------------------------------------------------
-- 1. profiles - one row per user
-- -------------------------------------------------------------
create table if not exists public.profiles (
  id                 uuid primary key references auth.users (id) on delete cascade,
  name               text,
  preferred_language text not null default 'hinglish',
  preferences        jsonb not null default '{}'::jsonb,
  created_at         timestamptz not null default now()
);

-- -------------------------------------------------------------
-- 2. memories - the long-term memory
-- -------------------------------------------------------------
create table if not exists public.memories (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users (id) on delete cascade,
  kind         text not null check (kind in ('preference', 'person', 'routine', 'fact', 'mood', 'relationship')),
  content      text not null,
  source       text,
  created_at   timestamptz not null default now(),
  last_used_at timestamptz
);
create index if not exists memories_user_id_idx on public.memories (user_id);

-- -------------------------------------------------------------
-- 3. conversations - rolling recent log
--    (older lines get summarized into memories, then dropped)
-- -------------------------------------------------------------
create table if not exists public.conversations (
  id      uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  role    text not null check (role in ('user', 'maya')),
  text    text not null,
  ts      timestamptz not null default now()
);
create index if not exists conversations_user_ts_idx on public.conversations (user_id, ts desc);

-- -------------------------------------------------------------
-- 4. tasks - ongoing work
-- -------------------------------------------------------------
create table if not exists public.tasks (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users (id) on delete cascade,
  title      text not null,
  status     text not null default 'open' check (status in ('open', 'in_progress', 'done', 'cancelled')),
  due        timestamptz,
  notes      text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists tasks_user_id_idx on public.tasks (user_id);

-- Keep tasks.updated_at fresh on every edit
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists tasks_set_updated_at on public.tasks;
create trigger tasks_set_updated_at
  before update on public.tasks
  for each row execute function public.set_updated_at();

-- Auto-create a profile row when a new user signs up
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, name)
  values (new.id, coalesce(new.raw_user_meta_data->>'name', ''))
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- -------------------------------------------------------------
-- Row Level Security - every table, per spec:
-- a user can select, insert, update and delete only their own
-- rows. No table is ever public. Deleting the auth user wipes
-- all of their rows (on delete cascade above).
-- -------------------------------------------------------------
alter table public.profiles      enable row level security;
alter table public.memories      enable row level security;
alter table public.conversations enable row level security;
alter table public.tasks         enable row level security;

drop policy if exists "own profile only" on public.profiles;
create policy "own profile only" on public.profiles
  for all using (id = auth.uid()) with check (id = auth.uid());

drop policy if exists "own memories only" on public.memories;
create policy "own memories only" on public.memories
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists "own conversations only" on public.conversations;
create policy "own conversations only" on public.conversations
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists "own tasks only" on public.tasks;
create policy "own tasks only" on public.tasks
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- -------------------------------------------------------------
-- AUTH PROVIDERS (manual, one-time clicks in the dashboard -
-- SQL cannot enable these):
--   Authentication > Providers > Email        -> ON (email + password)
--   Authentication > Providers > Google       -> ON (needs a Google
--     Cloud OAuth client ID + secret; the dashboard shows the
--     redirect URL to paste into the Google Cloud console)
-- -------------------------------------------------------------
