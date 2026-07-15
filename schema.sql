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


-- ============================================================
-- TradingView signal webhook + AI reviews (SKLZ Pro indicator)
-- ============================================================
create table if not exists public.signals (
    id           bigint generated always as identity primary key,
    received_at  timestamptz not null default now(),
    source       text not null default 'sklz_pro',
    symbol       text not null,
    tf           text default '',
    side         text not null,
    entry        double precision,
    sl           double precision,
    tp1          double precision,
    tp2          double precision,
    rr           double precision,
    mode         text default '',
    method       text default '',
    reason       text default '',
    price        double precision,
    atr          double precision,
    ai_review    text default ''
);
create index if not exists signals_recent_idx on public.signals (received_at desc);
alter table public.signals enable row level security;
-- No public policies: the API (service key) writes; the dashboard reads via JWT.


-- ============================================================
-- TradeGPT — AI trading analyst
-- ============================================================
create table if not exists public.gpt_profiles (
    user_id      uuid primary key references auth.users(id) on delete cascade,
    style        text default 'Day trading',
    markets      text default 'XAUUSD',
    account_size double precision default 10000,
    risk_pct     double precision default 1.0,
    methods      text default 'SMC, price action',
    notes        text default '',
    updated_at   timestamptz default now()
);

create table if not exists public.gpt_analyses (
    id         bigint generated always as identity primary key,
    user_id    uuid not null references auth.users(id) on delete cascade,
    created_at timestamptz not null default now(),
    symbol     text default '',
    timeframe  text default '',
    result     jsonb not null default '{}'::jsonb
);
create index if not exists gpt_analyses_user_idx on public.gpt_analyses (user_id, created_at desc);

alter table public.gpt_profiles enable row level security;
alter table public.gpt_analyses enable row level security;
-- API (service key) mediates all access; users reach their own rows via JWT.


-- ============================================================
-- Subscriptions, affiliate referrals, news (dashboard)
-- ============================================================
create table if not exists public.subscriptions (
    user_id            uuid primary key references auth.users(id) on delete cascade,
    plan               text not null default 'Free',
    active             boolean not null default false,
    current_period_end timestamptz,
    updated_at         timestamptz default now()
);

create table if not exists public.referrals (
    id          bigint generated always as identity primary key,
    referrer_id uuid not null references auth.users(id) on delete cascade,
    email       text,
    event       text default 'signed up',   -- signed up | purchased
    commission  double precision default 0,
    created_at  timestamptz not null default now()
);
create index if not exists referrals_referrer_idx on public.referrals (referrer_id, created_at desc);

create table if not exists public.news (
    id         bigint generated always as identity primary key,
    tag        text default 'NEW',
    title      text not null,
    body       text default '',
    created_at timestamptz not null default now()
);

alter table public.subscriptions enable row level security;
alter table public.referrals     enable row level security;
alter table public.news          enable row level security;


create table if not exists public.orders (
    id         bigint generated always as identity primary key,
    user_id    uuid not null references auth.users(id) on delete cascade,
    product    text not null,
    amount     double precision default 0,
    created_at timestamptz not null default now()
);
create index if not exists orders_user_idx on public.orders (user_id, created_at desc);
alter table public.orders enable row level security;


-- ============================================================
-- TradingView invite-only access tracking
-- ============================================================
create table if not exists public.tv_access (
    id           bigint generated always as identity primary key,
    user_id      uuid not null references auth.users(id) on delete cascade,
    email        text default '',
    tv_username  text not null,
    product      text not null default 'SKLZ Indicator Suite',
    plan         text not null default 'monthly',       -- monthly | lifetime
    status       text not null default 'pending',       -- pending | active | revoked
    requested_at timestamptz not null default now(),
    granted_at   timestamptz,
    expires_at   timestamptz
);
create index if not exists tv_access_user_idx on public.tv_access (user_id, requested_at desc);
alter table public.tv_access enable row level security;


-- billing columns on subscriptions
alter table public.subscriptions add column if not exists stripe_customer_id text;
alter table public.subscriptions add column if not exists stripe_subscription_id text;
alter table public.subscriptions add column if not exists founder boolean not null default false;
