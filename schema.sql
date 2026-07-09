-- SKLZ Labs — database schema (Phase 1a: waitlist)
-- Run this in Supabase → SQL Editor → New query → Run.

-- Waitlist: people who want to be notified when copy trading / products launch.
create table if not exists public.waitlist (
    id          uuid primary key default gen_random_uuid(),
    email       text not null unique,
    source      text not null default 'website',
    interest    text not null default 'copy_strategy',
    ip          text,
    created_at  timestamptz not null default now()
);

-- Fast lookups / exports by recency.
create index if not exists waitlist_created_idx on public.waitlist (created_at desc);

-- Row Level Security: lock the table down. The API uses the SECRET key
-- (service role), which bypasses RLS, so no public policy is needed. This
-- ensures nobody with the public/publishable key can read the email list.
alter table public.waitlist enable row level security;

-- (No policies added on purpose → only the service/secret key can touch it.)

-- Sanity check: after running, this should return 0 rows, not an error.
-- select count(*) from public.waitlist;


-- ============================================================
-- Accounts: profiles mirror of auth.users (Phase 1b)
-- ============================================================
-- Supabase Auth stores users in the auth.users table (managed). We keep a
-- public.profiles row per user for our own app data (role, display name,
-- and — later — entitlements/license links).

create table if not exists public.profiles (
    id            uuid primary key references auth.users(id) on delete cascade,
    email         text not null,
    display_name  text,
    role          text not null default 'user',   -- user | creator | affiliate | admin
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create index if not exists profiles_email_idx on public.profiles (email);

-- RLS on. The API uses the secret key (bypasses RLS) for writes. If you later
-- let the browser read profiles directly with the publishable key, add a
-- policy so a user can read only their own row:
alter table public.profiles enable row level security;

-- Example self-read policy (safe to enable now; harmless with secret-key API):
drop policy if exists "profiles self read" on public.profiles;
create policy "profiles self read"
  on public.profiles for select
  using ( auth.uid() = id );

-- keep updated_at fresh
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end; $$;

drop trigger if exists profiles_touch on public.profiles;
create trigger profiles_touch before update on public.profiles
  for each row execute function public.touch_updated_at();


-- ============================================================
-- Bot telemetry: VPS bots phone home; dashboard reads (Phase 2)
-- ============================================================
create table if not exists public.bot_sessions (
    id          uuid primary key default gen_random_uuid(),
    bot_key     text not null,               -- shared secret identity of the bot install
    bot         text not null,               -- trend_ema | smc | ...
    symbol      text not null,
    timeframe   text not null default '',
    mode        text not null default 'paper',
    started_at  timestamptz not null default now(),
    last_seen   timestamptz not null default now(),
    equity      double precision,
    balance     double precision,
    stats       jsonb not null default '{}'::jsonb,
    ai_report   jsonb
);
create index if not exists bot_sessions_seen_idx on public.bot_sessions (last_seen desc);

create table if not exists public.bot_events (
    id          bigint generated always as identity primary key,
    session_id  uuid not null references public.bot_sessions(id) on delete cascade,
    ts          timestamptz not null default now(),
    level       text not null default 'info',
    etype       text not null default '',
    message     text not null,
    data        jsonb not null default '{}'::jsonb
);
create index if not exists bot_events_session_idx on public.bot_events (session_id, id desc);

alter table public.bot_sessions enable row level security;
alter table public.bot_events enable row level security;
-- No public policies: only the API (secret key) reads/writes. Dashboard access
-- goes through the API with the user's JWT.
