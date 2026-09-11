-- ============================================================
-- D5 — live demo execution scope
-- ============================================================
-- Runner identity as OBSERVED, not as labelled. bot_state is written by
-- the Runner itself on every poll; nothing a browser sends reaches it.
create table if not exists public.bot_state (
    bot_name        text primary key,
    last_account    text,
    last_server     text,
    account_seen_at timestamptz,
    updated_at      timestamptz not null default now()
);
alter table public.bot_state enable row level security;
revoke all on public.bot_state from anon, authenticated;

-- Which demo command belongs to which prospect token, so a run can be
-- watched and rate-limited without the browser naming anything.
alter table public.bot_orders
  add column if not exists demo_token text,
  add column if not exists demo_kind  text;

create index if not exists bot_orders_demo_token
  on public.bot_orders (demo_token, created_at desc)
  where demo_token is not null;

select column_name from information_schema.columns
 where table_name='bot_state' order by ordinal_position;
select column_name from information_schema.columns
 where table_name='bot_orders' and column_name in ('demo_token','demo_kind');
